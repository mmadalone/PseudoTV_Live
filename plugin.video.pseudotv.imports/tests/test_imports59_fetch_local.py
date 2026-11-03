"""imports.59 (S4) — read Movistar+'s local files safely (it rewrites them
in place).

Imports._fetchLocal now reads a path that translates to a local file with
plain Python file I/O: _localFileState (os.stat on the translated path,
returning (size, mtime_ns)) before and after, open(..., 'rb') in between --
never xbmcvfs.Stat, which once wedged the imports thread on another add-on's
special:// file during playback. Kodi's VFS reads only a path that doesn't
translate to a local file. The playlist validator stays str(st_mtime), now
with sub-second precision; the result also carries 'mtime_ns'. A read during
which the size or mtime_ns changed is a failure marked transient
('file changed while reading; will retry'): validators untouched, no
channel orphaned, retried next sync.
"""
import os

import pytest

import imports as imports_mod
import xbmcvfs
from fileaccess import FileAccess
from imports import Imports


CONTENT = b'#EXTM3U\n#EXTINF:-1 tvg-id="c1" tvg-name="Chan 1",Chan 1\nplugin://plugin.video.example/?id=c1\n'


@pytest.fixture
def no_vfs(monkeypatch):
    """Any Kodi VFS call for a local file fails the test."""
    def boom(*a, **k):
        raise AssertionError('Kodi VFS used for a local file')
    monkeypatch.setattr(xbmcvfs, 'Stat', boom)
    monkeypatch.setattr(imports_mod.xbmcvfs, 'Stat', boom)
    monkeypatch.setattr(FileAccess, 'exists', staticmethod(boom))
    monkeypatch.setattr(FileAccess, 'stream', staticmethod(boom))


@pytest.fixture
def special(monkeypatch, tmp_path):
    """special://userdata/ -> tmp_path (the stub maps it into the repo)."""
    real = FileAccess.translatePath
    def translate(path):
        if path.startswith('special://userdata/'):
            return str(tmp_path / path[len('special://userdata/'):])
        return real(path)
    monkeypatch.setattr(FileAccess, 'translatePath', staticmethod(translate))
    return tmp_path


def test_plain_path_reads_with_os_stat(tmp_path, no_vfs):
    f = tmp_path / 'list.m3u'
    f.write_bytes(CONTENT)
    first = Imports().fetchSource(str(f))
    assert first['status'] == 200 and first['content'] == CONTENT and first['error'] is None
    assert first['mtime_ns'] == os.stat(str(f)).st_mtime_ns
    assert 'transient' not in first
    second = Imports().fetchSource(str(f), last_modified=first['last_modified'])
    assert second['status'] == 304 and second['content'] is None
    assert second['mtime_ns'] == first['mtime_ns']


def test_special_path_reads_with_os_stat(special, no_vfs):
    folder = special / 'addon_data' / 'plugin.video.movistarplus'
    folder.mkdir(parents=True)
    (folder / 'channels.m3u8').write_bytes(CONTENT)
    res = Imports().fetchSource('special://userdata/addon_data/plugin.video.movistarplus/channels.m3u8')
    assert res['status'] == 200 and res['content'] == CONTENT
    assert res['mtime_ns'] == os.stat(str(folder / 'channels.m3u8')).st_mtime_ns
    state = Imports._localFileState('special://userdata/addon_data/plugin.video.movistarplus/channels.m3u8')
    assert state == (len(CONTENT), res['mtime_ns'])


def test_validator_has_sub_second_precision(tmp_path, no_vfs):
    f = tmp_path / 'list.m3u'
    f.write_bytes(CONTENT)
    os.utime(str(f), ns=(1793642891620000000, 1793642891620000000))
    res = Imports().fetchSource(str(f))
    assert float(res['last_modified']) == pytest.approx(1793642891.62)
    # a whole-second validator stored by the xbmcvfs.Stat era reads in full once ...
    again = Imports().fetchSource(str(f), last_modified='1793642891.0')
    assert again['status'] == 200
    # ... and the new one answers 304
    assert Imports().fetchSource(str(f), last_modified=res['last_modified'])['status'] == 304


@pytest.mark.parametrize('bad', ['not-a-number', ['1.0']])
def test_invalid_stored_validator_is_logged_and_the_file_read_in_full(tmp_path, monkeypatch, no_vfs, bad):
    """No silent except: an unusable stored last_modified is logged (debug)
    with the path and the value, then the file is read in full."""
    f = tmp_path / 'list.m3u'
    f.write_bytes(CONTENT)
    lines = []
    monkeypatch.setattr(Imports, 'log', lambda self, msg, level=None: lines.append((msg, level)))
    res = Imports().fetchSource(str(f), last_modified=bad)
    assert res['status'] == 200 and res['content'] == CONTENT
    debug = [m for m, lvl in lines if lvl == imports_mod.xbmc.LOGDEBUG and 'last_modified' in m]
    assert len(debug) == 1 and str(f) in debug[0] and repr(bad) in debug[0]


def test_missing_file(tmp_path, no_vfs):
    res = Imports().fetchSource(str(tmp_path / 'nope.m3u'))
    assert res['status'] == 0 and res['content'] is None and res['mtime_ns'] is None
    assert 'local source not found' in res['error']


def test_stable_unreadable_file_carries_its_mtime(tmp_path, no_vfs):
    d = tmp_path / 'epg.xml'
    d.mkdir()                                   # stat works, open() fails
    res = Imports().fetchSource(str(d))
    assert res['status'] == 0 and res['content'] is None
    assert res['mtime_ns'] == os.stat(str(d)).st_mtime_ns
    assert 'transient' not in res and res['error'].startswith('local fetch failed')


def _flaky_state(monkeypatch, change):
    real = Imports._localFileState
    calls = {'n': 0}
    def flaky(path):
        st = real(path)
        calls['n'] += 1
        return st if calls['n'] % 2 else change(st)
    monkeypatch.setattr(Imports, '_localFileState', staticmethod(flaky))


@pytest.mark.parametrize('change', [lambda st: (st[0] + 1, st[1]), lambda st: (st[0], st[1] + 1), lambda st: None])
def test_file_changing_during_the_read_is_transient(tmp_path, monkeypatch, no_vfs, change):
    f = tmp_path / 'list.m3u'
    f.write_bytes(CONTENT)
    _flaky_state(monkeypatch, change)
    res = Imports().fetchSource(str(f))
    assert res['status'] == 0 and res['content'] is None
    assert res['transient'] is True and res['mtime_ns'] is None
    assert res['error'] == 'file changed while reading; will retry'


def test_transient_playlist_read_keeps_validators_and_orphans_nothing(tmp_path, monkeypatch, no_vfs):
    f = tmp_path / 'list.m3u'
    f.write_bytes(CONTENT)
    _flaky_state(monkeypatch, lambda st: (st[0], st[1] + 1))
    url = 'plugin://plugin.video.example/?id=c1'
    existing = [{'id': 'c1@imp', 'epg_id': 'c1', 'name': 'Chan 1', 'import_source': 'imp', 'type': 'import',
                 'enabled': True, 'number': 1, 'is_orphan': False, 'source_url': url, 'url': url}]
    cfg = {'id': 'imp', 'enabled': True, 'm3u_path': str(f), 'playlist_rev': 1,
           'etag': None, 'last_modified': '1000.0'}
    res = Imports().syncOne(cfg, existing)
    assert res['status'] == 'failed'
    assert res['updated_cfg']['last_error'] == 'file changed while reading; will retry'
    assert res['updated_cfg']['last_modified'] == '1000.0', 'validators untouched'
    assert res['orphans'] == [] and res['parsed'] == []
    assert existing[0]['is_orphan'] is False


def test_path_that_doesnt_translate_to_a_local_file_stays_on_kodis_vfs(monkeypatch):
    monkeypatch.setattr(FileAccess, 'translatePath', staticmethod(lambda p: 'smb://server/share/list.m3u'))
    seen = []
    def vfs(self, path, last_modified):
        seen.append((path, last_modified))
        return {'status': 304, 'content': None, 'etag': None, 'last_modified': last_modified, 'error': None}
    monkeypatch.setattr(Imports, '_fetchLocalVFS', vfs)
    res = Imports().fetchSource('special://userdata/list.m3u', last_modified='5')
    assert seen == [('special://userdata/list.m3u', '5')]
    assert res['status'] == 304
    assert Imports._localFileState('special://userdata/list.m3u') is None


def test_local_real_path():
    assert Imports._localRealPath('/abs/x.m3u') == '/abs/x.m3u'
    assert Imports._localRealPath('relative/x.m3u') == 'relative/x.m3u'
    assert Imports._localRealPath('/media/user@host/x.m3u') == '/media/user@host/x.m3u', 'plain paths are used as given'
    assert Imports._localRealPath('') is None
    assert Imports._localRealPath(None) is None
