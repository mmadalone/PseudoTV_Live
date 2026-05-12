"""Unit tests for writer_lock.

Covers the four contracts from the C#10 plan
(/home/madalone/.claude/plans/misty-shimmying-liskov.md):

  1. Two threads acquiring the same lock serialize (no overlap).
  2. RLock re-entrant from same thread (no deadlock).
  3. Timeout raises WriterTimeout (doesn't silently hang).
  4. Exception inside critical section releases the lock.

Tests stay in-process; no Kodi runtime required. The conftest at
plugin.video.pseudotv.imports/tests/conftest.py adds resources/lib
plus project_root/tests (Kodi API stubs) to sys.path.
"""
import threading
import time

import pytest

from writer_lock import WRITER_LOCK, WriterTimeout, held


# ---------------------------------------------------------------- 1. serialization


def test_writer_lock_serializes_two_threads_writing_same_file():
    """Two threads each enter `with held(...)` and sleep 50ms inside.
    Assert their intervals don't overlap — the second thread's enter
    timestamp must be >= the first thread's exit timestamp."""
    intervals = []      # list of (enter_ts, exit_ts)
    lock      = threading.Lock()    # protects `intervals` only — not under test

    def critical():
        with held(ctx='test.serialize'):
            t_enter = time.monotonic()
            time.sleep(0.05)
            t_exit  = time.monotonic()
        with lock:
            intervals.append((t_enter, t_exit))

    t1 = threading.Thread(target=critical, name='t1')
    t2 = threading.Thread(target=critical, name='t2')
    t1.start(); t2.start()
    t1.join(timeout=5); t2.join(timeout=5)
    assert not t1.is_alive() and not t2.is_alive()
    assert len(intervals) == 2

    a, b = sorted(intervals)   # sort by enter timestamp
    # Second thread's enter must be at-or-after first thread's exit.
    # Allow a small epsilon for scheduling jitter (sub-ms is fine; we
    # just want to assert "no real overlap").
    assert b[0] >= a[1] - 0.001, (
        'critical sections overlapped: thread1=%r thread2=%r' % (a, b))


# ---------------------------------------------------------------- 2. re-entrancy


def test_writer_lock_re_entrant_from_same_thread():
    """Same thread acquires WRITER_LOCK twice. Must not deadlock.
    RLock contract — protects callers like Builder.__setStation
    that hold the lock at the call-site AND have inner methods that
    re-acquire (e.g. Tasks._writeAtomic called from Imports.syncAll
    while a parent critical section also holds the lock)."""
    completed = []

    def doubly_nested():
        with held(ctx='test.reentrant.outer'):
            with held(ctx='test.reentrant.inner'):
                completed.append('inner')
            completed.append('outer')

    # Run on a thread so a hypothetical deadlock would surface as a
    # join timeout rather than freezing pytest.
    t = threading.Thread(target=doubly_nested, name='reentrant')
    t.start()
    t.join(timeout=2.0)
    assert not t.is_alive(), 'thread deadlocked on re-entrant acquire'
    assert completed == ['inner', 'outer']


# ---------------------------------------------------------------- 3. timeout


def test_writer_lock_timeout_raises_writer_timeout():
    """Thread A holds WRITER_LOCK for 2 seconds. Thread B tries to
    acquire with timeout=0.3. Assert WriterTimeout raised within
    ~1s — never silently hangs."""
    holder_ready = threading.Event()
    holder_done  = threading.Event()

    def hold():
        with held(ctx='test.timeout.holder'):
            holder_ready.set()
            holder_done.wait(timeout=3.0)   # released on completion

    t_holder = threading.Thread(target=hold, name='holder', daemon=True)
    t_holder.start()
    assert holder_ready.wait(timeout=1.0), 'holder thread did not acquire'

    # Now the lock is held. Try to acquire with a short timeout.
    t0 = time.monotonic()
    with pytest.raises(WriterTimeout):
        with held(ctx='test.timeout.waiter', timeout=0.3):
            pytest.fail('should have raised WriterTimeout instead of entering')
    elapsed = time.monotonic() - t0
    assert 0.25 < elapsed < 1.5, (
        'WriterTimeout took %.3fs, expected ~0.3s' % elapsed)

    # Release the holder so the test doesn't leak the lock to later tests.
    holder_done.set()
    t_holder.join(timeout=2.0)
    assert not t_holder.is_alive()


# ---------------------------------------------------------------- 4. exception releases lock


def test_writer_lock_release_on_exception_in_critical_section():
    """An exception raised inside `with held(...)` must release the
    lock — otherwise a single buggy critical section poisons all
    subsequent writes until process restart. Verify by acquiring,
    raising, then re-acquiring with a short timeout from a different
    thread (would block forever if the lock leaked)."""

    class _Sentinel(Exception):
        pass

    with pytest.raises(_Sentinel):
        with held(ctx='test.exception'):
            raise _Sentinel('simulated critical-section failure')

    # Lock should now be released. A short-timeout acquire from a fresh
    # thread proves the release without hanging if it's broken.
    acquired = []

    def reacquire():
        try:
            with held(ctx='test.exception.reacquire', timeout=0.5):
                acquired.append(True)
        except WriterTimeout:
            acquired.append(False)

    t = threading.Thread(target=reacquire, name='reacquire')
    t.start()
    t.join(timeout=2.0)
    assert not t.is_alive()
    assert acquired == [True], 'lock was not released after exception'
