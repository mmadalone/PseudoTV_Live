"""imports.59 (S9) — link-based replay is offered only while the show on air
has a link.

IPTV Simple offers every started show within catchup-days without checking
for a link, and plays an unfilled '{catchup-id}' ("Playback failed") when the
show has none. syncAll now decides per live link-based channel (catch-up
source holding {catchup-id}, or 'vod' with no source) from the guide about
to be published: the show on air decides; with none on air, the latest show
that has started; with no started show, replay is off. Off publishes
catchup="" and no catchup-source / catchup-days. The channel record keeps the
playlist's values, so replay comes back by itself. The eager start-up render
(renderFromPersistedState) applies the same rule to copies of the records.
"""
import time

import pytest

import imports as imports_mod
import renderers
from imports import Imports


IID     = 'imp'
M3U_URL = 'https://example.invalid/list.m3u'
EPG_URL = 'https://example.invalid/guide.xml'
NOW     = int(time.time())
LINKED  = ('default', '{catchup-id}', '7')


def _ts(epoch):
    return time.strftime('%Y%m%d%H%M%S', time.gmtime(epoch)) + ' +0000'


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


class _MockM3U:
    def __init__(self):
        self.added = []
        self.M3UDATA = {'stations': [], 'recordings': []}
        self.saved = None
    def addStation(self, sitem): self.added.append(sitem)
    def delStation(self, sitem): pass
    def _save(self):
        self.saved = [dict(s) for s in self.M3UDATA['stations']]
        return True


class _X:
    def __init__(self, programmes=None):
        self.XMLTVDATA = {'channels': [], 'programmes': list(programmes or []), 'recordings': []}


def _record(tvg, catchup='default', source='{catchup-id}', days='7', **over):
    cid = '%s@%s' % (tvg, IID)
    url = 'plugin://plugin.video.example/?id=%s' % tvg
    rec = {'id': cid, 'epg_id': tvg, 'name': 'Chan %s' % tvg, 'source_name': 'Chan %s' % tvg,
           'import_source': IID, 'type': 'import', 'enabled': True, 'number': 1, 'assigned_number': 1,
           'is_orphan': False, 'unallocated': False, 'source_url': url, 'url': url, 'group': ['IMP'],
           'logo': '', 'catchup': catchup, 'catchup-source': source, 'catchup-days': days,
           'operator_overrides': []}
    rec.update(over)
    return rec


def _prog(channel, start, stop, link=''):
    return {'channel': channel, 'start': _ts(start), 'stop': _ts(stop), 'catchup-id': link,
            'title': [('Show', '')]}


def _guide_xml(*progs):
    """Guide source carrying the given programmes (source-side ids)."""
    body = ''.join('<channel id="%s"><display-name>%s</display-name></channel>' % (t, t)
                   for t in sorted({p['channel'] for p in progs}))
    for p in progs:
        body += ('<programme start="%s" stop="%s" channel="%s"%s><title>Show</title></programme>'
                 % (p['start'], p['stop'], p['channel'],
                    (' catchup-id="%s"' % p['catchup-id']) if p['catchup-id'] else ''))
    return ('<?xml version="1.0" encoding="UTF-8"?><tv>%s</tv>' % body).encode()


def _sync(monkeypatch, records, guide):
    cfg = {'id': IID, 'name': 'IMP', 'enabled': True, 'm3u_url': M3U_URL, 'epg_url': EPG_URL,
           'start_num': 1, 'tombstones': [], 'playlist_rev': 1}
    def fetch(self, url, etag=None, last_modified=None, **kw):
        out = {'status': 0, 'content': None, 'etag': None, 'last_modified': None, 'error': None}
        if url == M3U_URL:
            out['status'] = 304
        elif url == EPG_URL:
            out.update(status=200, content=guide)
        return out
    monkeypatch.setattr(Imports, 'fetchSource', fetch)
    channels = _MockChannels([cfg], records)
    m3u = _MockM3U()
    Imports(channels=channels, m3u=m3u, xmltv=_X()).syncAll()
    stations = {s['id']: s for s in m3u.added}
    saved = {c['id']: c for c in channels.getChannels()}
    return stations, saved


def _catchup(d):
    return (d.get('catchup'), d.get('catchup-source'), d.get('catchup-days'))


# ---------------------------------------------------------------- through syncAll


def test_linked_show_on_air_keeps_replay(monkeypatch):
    guide = _guide_xml(_prog('A', NOW - 1800, NOW + 1800, link='plugin://x/?id=1'))
    stations, saved = _sync(monkeypatch, [_record('A')], guide)
    assert _catchup(stations['A@imp']) == LINKED
    assert _catchup(saved['A@imp']) == LINKED


def test_no_link_on_air_turns_replay_off_and_record_keeps_values(monkeypatch):
    guide = _guide_xml(_prog('A', NOW - 5400, NOW - 1800, link='plugin://x/?id=1'),
                       _prog('A', NOW - 1800, NOW + 1800))
    stations, saved = _sync(monkeypatch, [_record('A')], guide)
    assert _catchup(stations['A@imp']) == ('', '', '')
    assert _catchup(saved['A@imp']) == LINKED, 'the record keeps the playlist values'


def test_vod_without_source_counts_as_link_based(monkeypatch):
    guide = _guide_xml(_prog('A', NOW - 1800, NOW + 1800))
    stations, _saved = _sync(monkeypatch, [_record('A', catchup='vod', source='', days='')], guide)
    assert stations['A@imp']['catchup'] == ''


def test_channels_without_link_replay_are_untouched(monkeypatch):
    """Plain 'default' (append mode) and no catch-up publish as recorded,
    whatever the guide carries."""
    guide = _guide_xml(_prog('A', NOW - 1800, NOW + 1800), _prog('B', NOW - 1800, NOW + 1800))
    records = [_record('A', catchup='default', source='', days='5'),
               _record('B', catchup='', source='', days='', number=2, assigned_number=2)]
    stations, _saved = _sync(monkeypatch, records, guide)
    assert _catchup(stations['A@imp']) == ('default', '', '5')
    assert _catchup(stations['B@imp']) == ('', '', '')


# ---------------------------------------------------------------- the rule itself


def _offers(records, programmes):
    return Imports(xmltv=_X(programmes))._replayOffers(records)


def test_no_show_on_air_the_latest_started_decides():
    rec = _record('A')
    linked_last = [_prog('A@imp', NOW - 7200, NOW - 3600), _prog('A@imp', NOW - 3600, NOW - 600, link='l')]
    plain_last = [_prog('A@imp', NOW - 7200, NOW - 3600, link='l'), _prog('A@imp', NOW - 3600, NOW - 600)]
    assert _offers([rec], linked_last) == {'A@imp': True}
    assert _offers([rec], plain_last) == {'A@imp': False}


def test_future_shows_dont_count_and_no_programmes_is_off():
    rec = _record('A')
    assert _offers([rec], [_prog('A@imp', NOW + 600, NOW + 3600, link='l')]) == {'A@imp': False}
    assert _offers([rec], []) == {'A@imp': False}


def test_only_live_link_based_import_records_are_decided():
    records = [_record('A'),
               _record('B', catchup='default', source='', days='5'),
               _record('C', enabled=False),
               _record('D', is_orphan=True),
               _record('E', unallocated=True),
               dict(_record('F'), type='Custom')]
    assert set(_offers(records, [])) == {'A@imp'}


def test_no_guide_data_decides_nothing():
    assert Imports()._replayOffers([_record('A')]) == {}


def test_channel_to_station_blanks_only_when_off():
    imp = Imports()
    rec = _record('A')
    assert _catchup(imp._channelToStation(rec)) == LINKED
    assert _catchup(imp._channelToStation(rec, replay_links=True)) == LINKED
    assert _catchup(imp._channelToStation(rec, replay_links=None)) == LINKED
    assert _catchup(imp._channelToStation(rec, replay_links=False)) == ('', '', '')
    assert _catchup(rec) == LINKED


def test_rendered_line_has_no_catchup_attributes_when_off():
    station = Imports()._channelToStation(_record('A'), replay_links=False)
    out = renderers.render_m3u({'stations': [station], 'recordings': []})
    line = [ln for ln in out.splitlines() if ln.startswith('#EXTINF')][0]
    assert 'catchup=""' in line
    assert 'catchup-source' not in line and 'catchup-days' not in line
    on = renderers.render_m3u({'stations': [Imports()._channelToStation(_record('A'))], 'recordings': []})
    assert 'catchup="default"' in on and 'catchup-source="{catchup-id}"' in on and 'catchup-days="7"' in on


# ---------------------------------------------------------------- eager start-up render


def test_eager_render_applies_the_rule_to_copies(monkeypatch):
    monkeypatch.setattr(imports_mod, '_isDiskMissing', lambda import_id=None, scope=None, **kw: True)
    on, off, plain = _record('A'), _record('B'), _record('C', catchup='default', source='', days='5')
    records = [on, off, plain]
    before = [dict(r) for r in records]
    guide = _X([_prog('A@imp', NOW - 1800, NOW + 1800, link='l'), _prog('B@imp', NOW - 1800, NOW + 1800)])
    m3u = _MockM3U()
    imp = Imports(channels=_MockChannels([], records), m3u=m3u, xmltv=guide)
    assert imp.renderFromPersistedState() == 3
    saved = {s['id']: s for s in m3u.saved}
    assert _catchup(saved['A@imp']) == LINKED
    assert _catchup(saved['B@imp']) == ('', '', '')
    assert _catchup(saved['C@imp']) == ('default', '', '5')
    assert records == before, 'channel records are never changed'


def test_eager_render_without_guide_data_publishes_records_as_they_are(monkeypatch):
    monkeypatch.setattr(imports_mod, '_isDiskMissing', lambda import_id=None, scope=None, **kw: True)
    m3u = _MockM3U()
    imp = Imports(channels=_MockChannels([], [_record('A')]), m3u=m3u)
    assert imp.renderFromPersistedState() == 1
    assert _catchup(m3u.saved[0]) == LINKED
