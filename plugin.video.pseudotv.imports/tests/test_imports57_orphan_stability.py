"""imports.57 — channels the source dropped stay orphaned across 304 cycles.

Pre-imports.57 syncOne's 304 path handed EVERY stored record of the import
(orphans included) to reconcile as "parsed"; applyRefresh then cleared
is_orphan, so a channel the source had dropped came back on the next routine
(304) sync and left again on the next full (200) read. Live: 11 Movistar+
channels — outside the account's package, or gone from Movistar's catalogue —
flipped in and out of the channel list.

These tests drive the real syncOne/syncAll across several cycles against a
channels store that persists between them, the way chkImports does.
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
LIB  = os.path.join(os.path.dirname(HERE), 'resources', 'lib')
if LIB not in sys.path:
    sys.path.insert(0, LIB)

import imports as imports_mod
from imports import Imports


M3U_URL = 'https://example.invalid/list.m3u'

BOTH = b'''#EXTM3U
#EXTINF:-1 tvg-id="A" tvg-name="Chan A",Chan A
plugin://plugin.video.example/?id=A
#EXTINF:-1 tvg-id="B" tvg-name="Chan B",Chan B
plugin://plugin.video.example/?id=B
'''

ONLY_A = b'''#EXTM3U
#EXTINF:-1 tvg-id="A" tvg-name="Chan A",Chan A
plugin://plugin.video.example/?id=A
'''


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(imports_mod, 'CACHE_LOC', str(tmp_path))
    monkeypatch.setattr(imports_mod, '_isDiskMissing', lambda iid, scope: False)
    imports_mod._GUIDE_SELFHEAL_LAST.clear()


class _Store:
    """channels.json stand-in that persists across cycles."""
    def __init__(self):
        self.imports = [{'id': 'imp', 'name': 'IMP', 'enabled': True,
                         'm3u_url': M3U_URL, 'epg_url': '', 'start_num': 1,
                         'refresh_interval_min': 0, 'tombstones': []}]
        self.channels = []
    def getImports(self):  return [dict(i) for i in self.imports]
    def getChannels(self): return [dict(c) for c in self.channels]
    def setChannels(self, channels=None, modified_ids=None, deleted_ids=None):
        if channels is not None: self.channels = [dict(c) for c in channels]
        return True
    def setImports(self, data):
        self.imports = [dict(i) for i in data]
        return True


class _M3U:
    def __init__(self):
        self.stations = {}
    def addStation(self, s): self.stations[s.get('id')] = s
    def delStation(self, s): self.stations.pop(s.get('id'), None)


class _XMLTV:
    def __init__(self):
        self.XMLTVDATA = {'channels': [], 'programmes': [], 'recordings': []}


def _cycle(monkeypatch, store, response):
    """One chkImports-style cycle: fresh Imports + M3U, real syncAll."""
    m3u = _M3U()
    importer = Imports(channels=store, m3u=m3u, xmltv=_XMLTV())
    def _fetch(url, etag=None, last_modified=None, **kw):
        base = {'status': 0, 'content': None, 'etag': None, 'last_modified': None, 'error': None}
        base.update(response)
        return base
    monkeypatch.setattr(importer, 'fetchSource', _fetch)
    results = importer.syncAll()
    return results['imp'], m3u


def _orphan(store, cid):
    return {c['id']: c for c in store.channels}[cid].get('is_orphan')


def test_dropped_channel_stays_orphaned_through_304_cycles(monkeypatch):
    store = _Store()
    _cycle(monkeypatch, store, {'status': 200, 'content': BOTH, 'last_modified': 't1'})
    assert _orphan(store, 'B@imp') is False

    _cycle(monkeypatch, store, {'status': 200, 'content': ONLY_A, 'last_modified': 't2'})
    assert _orphan(store, 'B@imp') is True

    for _ in range(2):   # routine syncs: source unchanged since the last full read
        res, m3u = _cycle(monkeypatch, store, {'status': 304})
        assert res['status'] == 'unchanged'
        assert _orphan(store, 'B@imp') is True
        assert 'B@imp' not in m3u.stations          # stays out of the channel list
        assert _orphan(store, 'A@imp') is False
        assert 'A@imp' in m3u.stations


def test_channel_returns_when_the_source_lists_it_again(monkeypatch):
    store = _Store()
    _cycle(monkeypatch, store, {'status': 200, 'content': BOTH, 'last_modified': 't1'})
    _cycle(monkeypatch, store, {'status': 200, 'content': ONLY_A, 'last_modified': 't2'})
    _cycle(monkeypatch, store, {'status': 304})
    res, m3u = _cycle(monkeypatch, store, {'status': 200, 'content': BOTH, 'last_modified': 't3'})
    assert _orphan(store, 'B@imp') is False
    assert 'B@imp' in m3u.stations


def test_304_parsed_list_excludes_orphans(monkeypatch):
    importer = Imports()
    monkeypatch.setattr(importer, 'fetchSource', lambda *a, **k: {
        'status': 304, 'content': None, 'etag': None, 'last_modified': None, 'error': None})
    existing = [
        {'id': 'A@imp', 'epg_id': 'A', 'import_source': 'imp', 'is_orphan': False},
        {'id': 'B@imp', 'epg_id': 'B', 'import_source': 'imp', 'is_orphan': True},
        {'id': 'X@other', 'epg_id': 'X', 'import_source': 'other', 'is_orphan': False},
    ]
    res = importer.syncOne({'id': 'imp', 'enabled': True, 'm3u_url': M3U_URL}, existing)
    assert [c['id'] for c in res['parsed']] == ['A@imp']
    assert res['epg_id_map'] == {'A': 'A@imp'}
    assert [c['id'] for c in res['orphans']] == ['B@imp']
    assert [e['id'] for e, _ in res['refreshed']] == ['A@imp']
