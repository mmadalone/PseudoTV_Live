"""imports.59 (S3) — atomic, honest JSON writes (channels.json and friends).

FileAccess.getJSON reads without the lock while the old setJSON truncated
the file and rewrote it in place, so a reader could catch a half-written
channels.json (getJSON then returns {} -- "no channels, no imports"). And
dumpJSON returns '' on a serialisation error, which setJSON wrote over the
file while still returning True.

Now: serialise first (same indent/separators, errors raise); unserialisable
or empty data returns False before the file is touched; local files are
written to a temp file in the same folder, flushed + fsynced and os.replace'd
onto the real path inside the existing FileLock; True only after the replace.
Paths that don't translate to a local file keep the in-place VFS write.

The temp file is per writer, <file>.<pid>.<thread id>.tmp: FileLock only
serialises threads of one Python interpreter, so the service and a Kodi
script (Channel Manager, context menu) saving channels.json at the same
instant would otherwise share <file>.tmp and could publish each other's
half-written file.
"""
import json
import os
import threading

import pytest

from globals import FileAccess   # first: fileaccess can't be the first addon module imported (circular)
import fileaccess


DATA = {'channels': [{'id': 'a@mp', 'number': 1, 'name': 'Ñandú', 'enabled': True}],
        'imports': [{'id': 'mp', 'tombstones': ['x@mp'], 'links_mtime_ns': 1793642891620000000}],
        'uuid': 'u'}


@pytest.fixture
def target(tmp_path):
    path = tmp_path / 'channels.json'
    path.write_text('{"old": true}')
    return path


def _leftovers(path):
    return sorted(n for n in os.listdir(str(path.parent)) if n.endswith('.tmp'))


def test_write_goes_through_tmp_and_replace(target, monkeypatch):
    calls = []
    real_replace = os.replace

    def spy(src, dst):
        calls.append((src, dst))
        return real_replace(src, dst)
    monkeypatch.setattr(os, 'replace', spy)
    assert FileAccess.setJSON(str(target), DATA) is True
    tmp = '%s.%d.%d.tmp' % (target, os.getpid(), threading.get_ident())
    assert calls == [(tmp, str(target))], 'a temp file of its own, in the same folder'
    assert _leftovers(target) == []
    assert not os.path.exists(str(target.parent / 'channels.lock')), 'FileLock released its lock file'


class _NoLock(object):
    """FileLock as another interpreter sees it: no exclusion at all."""
    def __init__(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_concurrent_writers_each_publish_a_complete_file(target, monkeypatch):
    """Two writers inside setJSON at once (as the service and a Kodi script
    can be): both temp files are written before either is replaced. Each
    writer publishes its own complete content and reports True."""
    monkeypatch.setattr(fileaccess, 'FileLock', _NoLock)
    barrier = threading.Barrier(2, timeout=10)
    real_fsync = os.fsync

    def both_written(fd):
        barrier.wait()
        return real_fsync(fd)
    monkeypatch.setattr(os, 'fsync', both_written)
    published = []
    real_replace = os.replace

    def spy(src, dst):
        with open(src, 'rb') as f:
            published.append(f.read())
        return real_replace(src, dst)
    monkeypatch.setattr(os, 'replace', spy)
    data_a = {'channels': [{'id': 'a@mp', 'name': 'A' * 5000}]}
    data_b = {'channels': [{'id': 'b@mp', 'name': 'B' * 300}]}
    results = {}

    def write(key, data):
        results[key] = FileAccess.setJSON(str(target), data)
    threads = [threading.Thread(target=write, args=('a', data_a)),
               threading.Thread(target=write, args=('b', data_b))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert results == {'a': True, 'b': True}
    expected = sorted(FileAccess.dumpJSON(d, idnt=4, sortkey=False).encode('utf-8') for d in (data_a, data_b))
    assert sorted(published) == expected, 'each writer published its own complete file'
    assert json.loads(target.read_text()) in (data_a, data_b)
    assert _leftovers(target) == []


def test_content_and_format_round_trip(target):
    assert FileAccess.setJSON(str(target), DATA) is True
    raw = target.read_bytes()
    assert json.loads(raw.decode('utf-8')) == DATA
    assert raw == FileAccess.dumpJSON(DATA, idnt=4, sortkey=False).encode('utf-8'), \
        'same bytes as the old writer (indent 4, compact separators, ASCII escapes)'
    assert FileAccess.getJSON(str(target)) == DATA


def test_text_data_is_written_as_is_like_dumpjson(target):
    assert FileAccess.setJSON(str(target), '{"pre": "serialised"}') is True
    assert target.read_text() == '{"pre": "serialised"}'


def test_unserialisable_data_keeps_old_file_and_returns_false(target):
    before = target.read_bytes()
    assert FileAccess.setJSON(str(target), {'bad': {1, 2}}) is False
    assert target.read_bytes() == before
    assert _leftovers(target) == []


@pytest.mark.parametrize('data', ['', b''])
def test_empty_data_keeps_old_file_and_returns_false(target, data):
    before = target.read_bytes()
    assert FileAccess.setJSON(str(target), data) is False
    assert target.read_bytes() == before


def test_write_failure_keeps_old_file_returns_false_and_cleans_tmp(target, monkeypatch):
    before = target.read_bytes()

    def fail(src, dst):
        raise OSError('simulated disk failure')
    monkeypatch.setattr(os, 'replace', fail)
    assert FileAccess.setJSON(str(target), DATA) is False
    assert target.read_bytes() == before
    assert _leftovers(target) == [], 'the half-done .tmp is removed'


def test_fsync_unavailable_still_writes_atomically(target, monkeypatch):
    def no_fsync(fd):
        raise OSError('fsync not supported')
    monkeypatch.setattr(os, 'fsync', no_fsync)
    assert FileAccess.setJSON(str(target), DATA) is True
    assert json.loads(target.read_text()) == DATA


def test_missing_folder_is_created(tmp_path):
    path = tmp_path / 'new' / 'dir' / 'servers.json'
    assert FileAccess.setJSON(str(path), {'servers': {}}) is True
    assert json.loads(path.read_text()) == {'servers': {}}


def test_non_local_path_keeps_in_place_vfs_write(monkeypatch):
    written = []

    class _FakeVFS(object):
        def write(self, buf): written.append(buf)
        def close(self): written.append('closed')

    class _NoLock(object):
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(fileaccess, 'FileLock', _NoLock)
    monkeypatch.setattr(FileAccess, 'open', staticmethod(lambda f, m, encoding=None: _FakeVFS()))
    assert FileAccess.setJSON('smb://nas/share/channels.json', DATA) is True
    assert written[0] == FileAccess.dumpJSON(DATA, idnt=4).encode('utf-8') and written[-1] == 'closed'
    assert FileAccess.setJSON('smb://nas/share/channels.json', {'bad': {1}}) is False
    assert len(written) == 2, 'refused before touching the file'


def test_non_local_write_failure_returns_false(monkeypatch):
    class _NoLock(object):
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def broken(f, m, encoding=None):
        raise IOError('share offline')
    monkeypatch.setattr(fileaccess, 'FileLock', _NoLock)
    monkeypatch.setattr(FileAccess, 'open', staticmethod(broken))
    assert FileAccess.setJSON('smb://nas/share/channels.json', DATA) is False

    class _RefusingVFS(object):
        def write(self, buf): return False          # xbmcvfs.File.write reports failure with False
        def close(self): pass
    monkeypatch.setattr(FileAccess, 'open', staticmethod(lambda f, m, encoding=None: _RefusingVFS()))
    assert FileAccess.setJSON('smb://nas/share/channels.json', DATA) is False


def test_local_path_detection(tmp_path):
    local = str(tmp_path / 'channels.json')
    assert FileAccess._localPath(local) == local
    assert FileAccess._localPath('smb://nas/share/x.json') is None
    assert FileAccess._localPath('nfs://nas/share/x.json') is None
    assert FileAccess._localPath('relative.json') is None
