"""imports.59 (S8) — one full re-read of every playlist.

An unchanged playlist answers 304 and syncOne reuses the stored channel
records, which still hold the values derived before this update (the blanked
Movistar+ catch-up, no source_name). PLAYLIST_REREAD_REV makes each import
read its playlist in full once: while its stored playlist_rev is older,
fetchSource gets no validators. The rev is stored only after a full read
parsed into channels; the zero-channel and parse-failure paths leave it
unset, so the next sync tries again. No gate bypass: the gate tests
(test_imports25/27/28/43/56) run unchanged.
"""
import time

import pytest

import imports as imports_mod
from imports import Imports, PLAYLIST_REREAD_REV, _playlistRev


IID     = 'imp'
M3U_URL = 'https://example.invalid/list.m3u'

PLAYLIST = (b'#EXTM3U\n'
            b'#EXTINF:-1 tvg-id="c1" tvg-name="Chan 1" tvg-chno="1",Chan 1\n'
            b'plugin://plugin.video.example/?id=c1\n')
EMPTY_PLAYLIST = b'#EXTM3U\n'


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(imports_mod, 'CACHE_LOC', str(tmp_path))
    monkeypatch.setattr(imports_mod, '_isDiskMissing', lambda iid, scope: False)
    imports_mod._GUIDE_SELFHEAL_LAST.clear()


def _cfg(**over):
    cfg = {'id': IID, 'name': 'IMP', 'enabled': True, 'm3u_url': M3U_URL,
           'etag': '"old"', 'last_modified': 'Mon, 02 Nov 2026 10:00:00 GMT',
           'start_num': 1, 'tombstones': [], 'respect_source_numbers': True}
    cfg.update(over)
    return cfg


def _existing():
    url = 'plugin://plugin.video.example/?id=c1'
    return [{'id': 'c1@imp', 'epg_id': 'c1', 'name': 'Chan 1', 'import_source': IID, 'type': 'import',
             'enabled': True, 'number': 1, 'assigned_number': 1, 'is_orphan': False, 'unallocated': False,
             'source_url': url, 'url': url, 'group': ['IMP'], 'logo': '', 'operator_overrides': []}]


def _syncOne(monkeypatch, cfg, response):
    calls = []
    def fetch(url, etag=None, last_modified=None, **kw):
        calls.append((url, etag, last_modified))
        out = {'status': 0, 'content': None, 'etag': None, 'last_modified': None, 'error': None}
        out.update(response)
        return out
    imp = Imports()
    monkeypatch.setattr(imp, 'fetchSource', fetch)
    return imp.syncOne(cfg, _existing()), calls


def test_outdated_rev_reads_in_full_and_stores_the_rev(monkeypatch):
    res, calls = _syncOne(monkeypatch, _cfg(), {'status': 200, 'content': PLAYLIST, 'etag': '"new"'})
    assert calls == [(M3U_URL, None, None)], 'no validators: a full read'
    assert res['status'] == 'ok'
    assert res['updated_cfg']['playlist_rev'] == PLAYLIST_REREAD_REV
    assert res['updated_cfg']['etag'] == '"new"'


@pytest.mark.parametrize('stored', [None, 0, '0', 'junk', -3])
def test_missing_or_old_rev_counts_as_outdated(monkeypatch, stored):
    cfg = _cfg(playlist_rev=stored) if stored is not None else _cfg()
    _res, calls = _syncOne(monkeypatch, cfg, {'status': 304})
    assert calls[0][1:] == (None, None)


def test_current_rev_passes_the_stored_validators(monkeypatch):
    cfg = _cfg(playlist_rev=PLAYLIST_REREAD_REV)
    res, calls = _syncOne(monkeypatch, cfg, {'status': 304})
    assert calls == [(M3U_URL, '"old"', 'Mon, 02 Nov 2026 10:00:00 GMT')]
    assert res['status'] == 'unchanged'
    assert res['updated_cfg']['playlist_rev'] == PLAYLIST_REREAD_REV


def test_zero_channel_read_leaves_the_rev_unset(monkeypatch):
    res, _calls = _syncOne(monkeypatch, _cfg(), {'status': 200, 'content': EMPTY_PLAYLIST})
    assert res['updated_cfg']['last_status'] == 'warning'
    assert 'playlist_rev' not in res['updated_cfg']


def test_parse_failure_leaves_the_rev_unset(monkeypatch):
    res, _calls = _syncOne(monkeypatch, _cfg(), {'status': 200, 'content': b'not a playlist'})
    assert res['status'] == 'failed'
    assert 'playlist_rev' not in res['updated_cfg']


def test_failed_fetch_leaves_the_rev_unset(monkeypatch):
    res, _calls = _syncOne(monkeypatch, _cfg(), {'status': 0, 'error': 'down'})
    assert res['status'] == 'failed'
    assert 'playlist_rev' not in res['updated_cfg']


def test_rev_survives_through_syncall_and_the_next_sync_uses_validators(monkeypatch):
    """Through syncAll: the first sync stores the rev (and the validators it
    got), the second passes those validators again."""
    class _Ch:
        def __init__(self, imports_list, channels_list):
            self._i, self._c = list(imports_list), list(channels_list)
        def getImports(self):  return list(self._i)
        def getChannels(self): return list(self._c)
        def setChannels(self, channels=None, modified_ids=None, deleted_ids=None):
            if channels is not None: self._c = list(channels)
            return True
        def setImports(self, data):
            self._i = list(data)
            return True

    calls = []
    def fetch(self, url, etag=None, last_modified=None, **kw):
        calls.append((etag, last_modified))
        if etag == '"fresh"':
            return {'status': 304, 'content': None, 'etag': None, 'last_modified': None, 'error': None}
        return {'status': 200, 'content': PLAYLIST, 'etag': '"fresh"', 'last_modified': None, 'error': None}
    monkeypatch.setattr(Imports, 'fetchSource', fetch)
    ch = _Ch([_cfg(last_sync_at=int(time.time()) - 3600)], _existing())
    Imports(channels=ch, m3u=None, xmltv=None).syncAll()
    assert ch.getImports()[0]['playlist_rev'] == PLAYLIST_REREAD_REV
    Imports(channels=ch, m3u=None, xmltv=None).syncAll()
    assert calls[0] == (None, None)
    assert calls[1][0] == '"fresh"'


def test_playlist_rev_reader():
    assert _playlistRev({}) == 0
    assert _playlistRev({'playlist_rev': 1}) == 1
    assert _playlistRev({'playlist_rev': '2'}) == 2
    assert _playlistRev({'playlist_rev': [1]}) == 0
