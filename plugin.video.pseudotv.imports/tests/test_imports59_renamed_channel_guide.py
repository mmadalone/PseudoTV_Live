"""imports.59 (S18) — a channel you renamed keeps its guide when the
playlist is unchanged.

davidmuma's channel ids are display names ('Canal Uno HD'), so imported
channels are matched by normalized name. On an unchanged playlist (304)
syncAll builds its name map from the STORED records, whose `name` is the
operator's after a rename -- the channel lost its guide on every 304 sync.
parseM3U now stores the playlist's own name as `source_name`, applyRefresh
keeps it current (even with 'name' overridden), and the name map also tries
it -- after every operator name and tvg-id, so no match made today changes.
render_m3u never emits it.
"""
import time

import pytest

import imports as imports_mod
import renderers
from imports import Imports


IID      = 'imp'
M3U_URL  = 'https://example.invalid/list.m3u'
EPG_URL  = 'https://example.invalid/guide.xml'


def _guide(*channel_names):
    chans = ''.join('<channel id="%s"><display-name>%s</display-name></channel>' % (n, n)
                    for n in channel_names)
    progs = ''.join('<programme start="20990101000000 +0000" stop="20990101010000 +0000" channel="%s">'
                    '<title>Show on %s</title></programme>' % (n, n) for n in channel_names)
    return ('<?xml version="1.0" encoding="UTF-8"?><tv>%s%s</tv>' % (chans, progs)).encode()


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(imports_mod, 'CACHE_LOC', str(tmp_path))
    monkeypatch.setattr(imports_mod, '_isDiskMissing', lambda iid, scope: False)
    imports_mod._GUIDE_SELFHEAL_LAST.clear()


class _MockChannels:
    def __init__(self, imports_list, channels_list):
        self._imports  = list(imports_list)
        self._channels = list(channels_list)
    def getImports(self):  return list(self._imports)
    def getChannels(self): return list(self._channels)
    def setChannels(self, channels=None, modified_ids=None, deleted_ids=None):
        if channels is not None: self._channels = list(channels)
        return True
    def setImports(self, data):
        self._imports = list(data)
        return True


class _MockXMLTV:
    def __init__(self):
        self.XMLTVDATA = {'channels': [], 'programmes': [], 'recordings': []}


def _record(tvg, name, source_name=None, overrides=()):
    cid = '%s@%s' % (tvg, IID)
    url = 'plugin://plugin.video.example/?id=%s' % tvg
    rec = {'id': cid, 'epg_id': tvg, 'name': name, 'import_source': IID, 'type': 'import',
           'enabled': True, 'number': 1, 'assigned_number': 1, 'is_orphan': False, 'unallocated': False,
           'source_url': url, 'url': url, 'group': ['IMP'], 'logo': '', 'catchup': '',
           'operator_overrides': list(overrides)}
    if source_name is not None:
        rec['source_name'] = source_name
    return rec


def _sync_304(monkeypatch, records, guide):
    cfg = {'id': IID, 'name': 'IMP', 'enabled': True, 'm3u_url': M3U_URL, 'epg_url': EPG_URL,
           'start_num': 1, 'tombstones': [], 'playlist_rev': 1,
           'last_sync_at': int(time.time()) - 11 * 3600, 'refresh_interval_min': 600}
    def fetch(self, url, etag=None, last_modified=None, **kw):
        out = {'status': 0, 'content': None, 'etag': None, 'last_modified': None, 'error': None}
        if url == M3U_URL:
            out['status'] = 304
        elif url == EPG_URL:
            out.update(status=200, content=guide)
        return out
    monkeypatch.setattr(Imports, 'fetchSource', fetch)
    xmltv = _MockXMLTV()
    res = Imports(channels=_MockChannels([cfg], records), m3u=None, xmltv=xmltv).syncAll()
    assert res[IID]['status'] == 'unchanged'
    return xmltv


def _guide_of(xmltv, cid):
    return sorted(p['title'][0][0] for p in xmltv.XMLTVDATA['programmes'] if p['channel'] == cid)


def test_renamed_channel_keeps_its_guide_on_an_unchanged_playlist(monkeypatch):
    records = [_record('CH1', 'Mi Canal', source_name='Canal Uno', overrides=['name'])]
    xmltv = _sync_304(monkeypatch, records, _guide('Canal Uno HD'))
    assert _guide_of(xmltv, 'CH1@imp') == ['Show on Canal Uno HD']
    assert [c['id'] for c in xmltv.XMLTVDATA['channels']] == ['CH1@imp']


def test_record_without_source_name_behaves_as_before(monkeypatch):
    """Records saved before the update (no source_name until the S8 re-read)
    match as today: by their stored name only."""
    records = [_record('CH1', 'Mi Canal', overrides=['name'])]
    xmltv = _sync_304(monkeypatch, records, _guide('Canal Uno HD'))
    assert _guide_of(xmltv, 'CH1@imp') == []


def test_matches_made_today_keep_their_channel(monkeypatch):
    """A's playlist name equals B's name: B keeps its guide (operator names
    and tvg-ids are tried before any playlist name)."""
    a = _record('A', 'Renamed A', source_name='Canal Dos', overrides=['name'])
    b = _record('B', 'Canal Dos', source_name='Canal Dos')
    xmltv = _sync_304(monkeypatch, [a, b], _guide('Canal Dos', 'Renamed A'))
    assert _guide_of(xmltv, 'B@imp') == ['Show on Canal Dos']
    assert _guide_of(xmltv, 'A@imp') == ['Show on Renamed A']


def test_operator_name_still_wins_a_collision(monkeypatch):
    """Both channels claim 'Canal Uno': the first channel's name owns it, as
    before imports.59."""
    a = _record('A', 'Canal Uno', source_name='Canal Uno')
    b = _record('B', 'Other', source_name='Canal Uno', overrides=['name'])
    xmltv = _sync_304(monkeypatch, [a, b], _guide('Canal Uno'))
    assert _guide_of(xmltv, 'A@imp') == ['Show on Canal Uno']
    assert _guide_of(xmltv, 'B@imp') == []


def test_parsem3u_stores_source_name():
    raw = (b'#EXTM3U\n#EXTINF:-1 tvg-id="CH1" tvg-name="Canal Uno",Label Uno\nplugin://x/?id=1\n'
           b'#EXTINF:-1 tvg-id="CH2",Label Dos\nplugin://x/?id=2\n')
    out = Imports().parseM3U(raw, IID)
    assert [(c['name'], c['source_name']) for c in out] == [('Canal Uno', 'Canal Uno'), ('Label Dos', 'Label Dos')]


def test_applyrefresh_refreshes_source_name_even_when_name_is_overridden():
    existing = _record('CH1', 'Mi Canal', source_name='Canal Uno', overrides=['name'])
    parsed = dict(_record('CH1', 'Canal Uno Nuevo', source_name='Canal Uno Nuevo'))
    Imports().applyRefresh(existing, parsed)
    assert existing['name'] == 'Mi Canal'
    assert existing['source_name'] == 'Canal Uno Nuevo'


def test_source_name_never_reaches_the_playlist():
    station = Imports()._channelToStation(_record('CH1', 'Mi Canal', source_name='Canal Uno'))
    out = renderers.render_m3u({'stations': [dict(station, source_name='Canal Uno')], 'recordings': []})
    assert 'source_name' not in out and 'source-name' not in out
    assert 'tvg-name="Mi Canal"' in out
