# -*- coding: utf-8 -*-
"""imports.69: the Save fix -- channels_lock, the lock around every channels.json write.

Channels.commit holds CHANNELS_LOCK for one strict fresh read, the merge and
the atomic write, so no other service thread writes channels.json in between
(before imports.69 a running sync wrote back the import list it read before a
dashboard Save). These are the lock's own contracts, after test_writer_lock.py:
  1. two threads never overlap inside held();
  2. re-entrant from the same thread (RLock);
  3. a timeout raises ChannelsLockTimeout and logs one ERROR line naming the
     caller; the wait is CHANNELS_TIMEOUT as it is when held() is called;
  4. an exception inside the block releases the lock;
  5. the one-file kill switch (_NoLock) keeps held() working.
The commit-level checks (busy -> nothing written, the getMYUUID call outside
the lock, ...) are in test_imports69_channels_commit.py with Channels.commit.
"""
import ast
import os
import threading
import time

import pytest

import channels_lock as cl
from channels_lock import ChannelsLockTimeout, held

HERE = os.path.dirname(os.path.abspath(__file__))
LIB  = os.path.normpath(os.path.join(HERE, '..', 'resources', 'lib'))


def _src():
    with open(os.path.join(LIB, 'channels_lock.py'), encoding='utf-8') as f:
        return f.read()


def test_module_hygiene():
    src  = _src()
    tree = ast.parse(src, feature_version=(3, 8))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module)
    assert names == {'threading', 'xbmc', 'logger', 'sys'}       # xbmc/logger behind try/except, as writer_lock
    assert 'madteevee (imports fork, imports.69)' in src
    assert 'noqa: S110' in src
    assert cl.CHANNELS_TIMEOUT == 15.0 and isinstance(cl.CHANNELS_LOCK, type(threading.RLock()))


def test_two_threads_never_overlap():
    intervals, guard = [], threading.Lock()
    barrier = threading.Barrier(2)

    def critical():
        barrier.wait()
        with held('test.serialize'):
            enter = time.monotonic()
            time.sleep(0.05)
            leave = time.monotonic()
        with guard:
            intervals.append((enter, leave))

    threads = [threading.Thread(target=critical) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert not any(t.is_alive() for t in threads) and len(intervals) == 2
    first, second = sorted(intervals)
    assert second[0] >= first[1] - 0.001, (first, second)


def test_re_entrant_from_the_same_thread():
    done = []

    def nested():
        with held('outer'):
            with held('inner'):
                done.append('inner')
            done.append('outer')

    t = threading.Thread(target=nested)
    t.start()
    t.join(timeout=2)
    assert not t.is_alive() and done == ['inner', 'outer']


def _hold_in_thread():
    ready, release = threading.Event(), threading.Event()

    def hold():
        with held('test.holder'):
            ready.set()
            release.wait(timeout=5)

    t = threading.Thread(target=hold, daemon=True)
    t.start()
    assert ready.wait(timeout=2)
    return t, release


def test_timeout_raises_and_logs_one_error(monkeypatch):
    lines = []
    monkeypatch.setattr(cl, '_log', lambda msg, level=None: lines.append((msg, level)))
    monkeypatch.setattr(cl, 'CHANNELS_TIMEOUT', 0.2)            # read when held() is called
    holder, release = _hold_in_thread()
    try:
        t0 = time.monotonic()
        with pytest.raises(ChannelsLockTimeout) as err:
            with held('saveImportRequest'):
                pytest.fail('entered a held lock')
        assert 0.15 < time.monotonic() - t0 < 1.5
        assert err.value.args == ('saveImportRequest',)
    finally:
        release.set()
        holder.join(timeout=2)
    assert lines == [('channels_lock: TIMEOUT (0.2s) waiting to save channels.json for saveImportRequest '
                      '-- nothing written', cl._LOG_ERROR)]


def test_explicit_timeout_wins(monkeypatch):
    monkeypatch.setattr(cl, '_log', lambda msg, level=None: None)
    holder, release = _hold_in_thread()
    try:
        t0 = time.monotonic()
        with pytest.raises(ChannelsLockTimeout):
            with held('x', timeout=0.1):
                pass
        assert time.monotonic() - t0 < 1.0
    finally:
        release.set()
        holder.join(timeout=2)


def test_exception_inside_releases_the_lock():
    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        with held('test.exception'):
            raise Boom()
    got = []

    def again():
        try:
            with held('test.again', timeout=0.5):
                got.append(True)
        except ChannelsLockTimeout:
            got.append(False)

    t = threading.Thread(target=again)
    t.start()
    t.join(timeout=2)
    assert got == [True]


def test_kill_switch_object(monkeypatch):
    """With _NoLock in place every held() enters at once; nothing else changes."""
    monkeypatch.setattr(cl, 'CHANNELS_LOCK', cl._NoLock())
    inside = []
    barrier = threading.Barrier(2)

    def enter():
        with held('test.kill_switch', timeout=0.1):
            inside.append(1)
            barrier.wait(timeout=2)                  # both inside together: no serialisation

    threads = [threading.Thread(target=enter) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert inside == [1, 1]


def test_exit_releases_the_lock_it_took(monkeypatch):
    """Swapping CHANNELS_LOCK while it is held (a test, the kill switch at a
    reload) never releases a lock that wasn't taken."""
    real = cl.CHANNELS_LOCK
    with held('test.swap'):
        monkeypatch.setattr(cl, 'CHANNELS_LOCK', cl._NoLock())
    got = []

    def other_thread():                              # an RLock re-enters from its own thread
        got.append(real.acquire(blocking=False))
        if got[-1]:
            real.release()

    t = threading.Thread(target=other_thread)
    t.start()
    t.join(timeout=2)
    assert got == [True]
