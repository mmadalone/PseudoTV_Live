"""imports.59 (S10, R2C-6) — linked replay history survives the guide-load
age trim, end to end.

Movistar+'s guide export starts at its own day's 00:00, so older linked shows
exist only in our pseudotv.xml. Every writer loads that file through
XMLTVS.cleanSelf -> cleanProgrammes, whose Min_Days trim (3 on madteevee;
the test stubs give 1, so the test sets xmltvs.MIN_GUIDEDAYS to 3) would
delete them. The chain tested here:

  1. a day-1 syncAll merges the export's links into the davidmuma guide;
     render_xmltv writes it, with a Custom channel alongside;
  2. a real XMLTVS(writable=False) loads it, with a real Channels reading a
     channels.json fixture, at a time when day 1 is past Min_Days but inside
     the 7-day replay window;
  3. a day-2 syncAll runs on that loaded guide with an export that no longer
     lists day 1;
  4. day-1 linked shows keep their links and details;
  5. a Custom or unlinked show of the same age is trimmed.

Times are relative to now (the data is dated back rather than the clock moved
forward): day 1 is 4 days before the load. With Min_Days 3 a 2-day-old show
is not trimmed at all, so step 5 needs a show older than 3 days.
"""
import json
import time

import pytest

import imports as imports_mod
import renderers
import xmltvs
from imports import Imports


IID     = 'mp'
EPG_URL = 'https://example.invalid/rich.xml'
DAY     = 86400
NOW     = int(time.time()) // 3600 * 3600
D1      = NOW - 4 * DAY
CUSTOM  = 'c' * 32 + '@PseudoTV_Live'

D1_SLOTS  = [(D1 + 3600 * i, D1 + 3600 * (i + 1)) for i in range(3)]
NOW_SLOTS = [(NOW - 3600, NOW), (NOW, NOW + 3600)]


def _ts(epoch):
    return time.strftime('%Y%m%d%H%M%S', time.gmtime(epoch)) + ' +0000'


def _export(slots, tag):
    progs = ''.join('<programme start="%s" stop="%s" channel="C1" catchup-id="plugin://x/?id=%s-%d&amp;t=u7d">'
                    '<title>Show %d</title><sub-title></sub-title></programme>' % (_ts(s), _ts(e), tag, s, s)
                    for s, e in slots)
    return ('<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE tv SYSTEM "xmltv.dtd"><tv>'
            '<channel id="C1"><display-name>Chan One</display-name></channel>' + progs + '</tv>').encode()


def _rich(slots, extra=''):
    progs = ''.join('<programme start="%s" stop="%s" channel="Chan One HD"><title>Show %d [COLOR orange](T1)[/COLOR]'
                    '</title><desc>Rich %d</desc></programme>' % (_ts(s), _ts(e), s, s) for s, e in slots)
    return ('<?xml version="1.0" encoding="UTF-8"?><tv><channel id="Chan One HD"><display-name>Chan One</display-name>'
            '</channel>' + progs + extra + '</tv>').encode()


UNLINKED_D1 = ('<programme start="%s" stop="%s" channel="Chan One HD"><title>Unlinked old</title>'
               '<desc>No link for this one</desc></programme>' % (_ts(D1 + 10 * 3600), _ts(D1 + 11 * 3600)))


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


class _M3U:
    def __init__(self): self.added = []
    def addStation(self, s): self.added.append(s)
    def delStation(self, s): pass
    def delRecording(self, s): pass


class _X:
    def __init__(self, channels=None, programmes=None):
        self.XMLTVDATA = {'channels': list(channels or []), 'programmes': list(programmes or []), 'recordings': []}


def _record():
    return {'id': 'C1@mp', 'epg_id': 'C1', 'name': 'Chan One', 'source_name': 'Chan One',
            'import_source': IID, 'type': 'import', 'enabled': True, 'number': 2, 'assigned_number': 2,
            'is_orphan': False, 'unallocated': False, 'source_url': 'plugin://x/?live=C1',
            'url': 'plugin://x/?live=C1', 'group': ['MP'], 'logo': '', 'catchup': 'default',
            'catchup-source': '{catchup-id}', 'catchup-days': '7', 'operator_overrides': []}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(imports_mod, 'CACHE_LOC', str(tmp_path))
    monkeypatch.setattr(imports_mod, '_isDiskMissing', lambda iid, scope: False)
    imports_mod._GUIDE_SELFHEAL_LAST.clear()
    monkeypatch.setattr(xmltvs, 'MIN_GUIDEDAYS', 3)
    folder = tmp_path / 'addon_data' / 'plugin.video.movistarplus'
    folder.mkdir(parents=True)
    (folder / 'channels.m3u8').write_text('#EXTM3U\n')
    cfg = {'id': IID, 'name': 'MP', 'enabled': True, 'm3u_path': str(folder / 'channels.m3u8'),
           'epg_url': EPG_URL, 'start_num': 1, 'tombstones': [], 'playlist_rev': 1,
           'refresh_interval_min': 600, 'last_sync_at': int(time.time()) - 11 * 3600}
    # channels.json fixture, read by a real Channels inside XMLTVS.cleanSelf
    chans_json = tmp_path / 'channels.json'
    chans_json.write_text(json.dumps({'uuid': 'u', 'imports': [cfg], 'channels': [
        {'id': CUSTOM, 'number': 1, 'name': 'My Custom', 'path': ['/x'], 'enabled': True},
        _record()]}))
    import channels as channels_module
    real_channels = channels_module.Channels

    class _FixtureChannels(real_channels):
        def __init__(self, file=None, writable=False):
            super().__init__(file=str(chans_json), writable=writable)
    monkeypatch.setattr(channels_module, 'Channels', _FixtureChannels)
    return tmp_path, folder, cfg


def _sync(monkeypatch, folder, cfg, xmltv, export, rich):
    (folder / 'epg.xml').write_bytes(export)
    real_fetch = Imports.fetchSource
    def stub(self, url, etag=None, last_modified=None, **kw):
        base = {'status': 0, 'content': None, 'etag': None, 'last_modified': None, 'error': None}
        if url.endswith('channels.m3u8'):
            base['status'] = 304
            return base
        if url == EPG_URL:
            base.update(status=200, content=rich)
            return base
        return real_fetch(self, url, etag=etag, last_modified=last_modified)
    monkeypatch.setattr(Imports, 'fetchSource', stub)
    res = Imports(channels=_Ch([cfg], [_record()]), m3u=_M3U(), xmltv=xmltv).syncAll()
    assert res[IID]['status'] == 'unchanged'
    assert res[IID]['updated_cfg'].get('last_error') is None
    return res


def _write(tmp_path, xmltv):
    xmltv.XMLTVDATA['data'] = {'date': time.strftime('%Y%m%d%H%M%S', time.gmtime(NOW))}
    rendered = renderers.render_xmltv(xmltv.XMLTVDATA)
    assert rendered is not None
    path = tmp_path / 'pseudotv.xml'
    path.write_bytes(rendered)
    return path


def _load(path):
    return xmltvs.XMLTVS(file=str(path), writable=False, m3u=_M3U())


def _shows(xmltv, channel):
    return {p['start']: p for p in xmltv.XMLTVDATA['programmes'] if p['channel'] == channel}


def test_linked_history_survives_load_trim_and_a_day2_sync(env, monkeypatch):
    tmp_path, folder, cfg = env
    custom_chan = {'id': CUSTOM, 'display-name': [('My Custom', '')]}
    custom_old = {'channel': CUSTOM, 'start': _ts(D1), 'stop': _ts(D1 + 3600), 'title': [('Custom old', '')],
                  'desc': [('Plain plot', '')]}
    custom_now = {'channel': CUSTOM, 'start': _ts(NOW), 'stop': _ts(NOW + 3600), 'title': [('Custom now', '')],
                  'desc': [('Plain plot', '')]}

    # 1. day 1: export covering day 1 and today, davidmuma likewise (+ one unlinked old show)
    day1 = _X(channels=[custom_chan], programmes=[custom_old, custom_now])
    _sync(monkeypatch, folder, cfg, day1, _export(D1_SLOTS + NOW_SLOTS, 'd1'),
          _rich(D1_SLOTS + NOW_SLOTS, extra=UNLINKED_D1))
    shows = _shows(day1, 'C1@mp')
    assert all(shows[_ts(s)]['catchup-id'].startswith('plugin://x/?id=d1-') for s, _e in D1_SLOTS + NOW_SLOTS)
    assert shows[_ts(D1 + 10 * 3600)]['catchup-id'] == ''                 # the unlinked old show
    path = _write(tmp_path, day1)

    # 2. load: day 1 is 4 days old -- past Min_Days (3), inside the 7-day replay window
    loaded = _load(path)
    shows = _shows(loaded, 'C1@mp')
    for s, _e in D1_SLOTS:
        assert shows[_ts(s)]['catchup-id'] == 'plugin://x/?id=d1-%d&t=u7d' % s
        assert shows[_ts(s)]['desc'][0][0] == 'Rich %d' % s
    assert _ts(D1 + 10 * 3600) not in shows, 'unlinked show of the same age trimmed'
    custom = _shows(loaded, CUSTOM)
    assert _ts(D1) not in custom, 'Custom show of the same age trimmed'
    assert _ts(NOW) in custom

    # 3. day 2: the export no longer lists day 1, davidmuma moved on as well
    _sync(monkeypatch, folder, cfg, loaded, _export(NOW_SLOTS, 'd2'), _rich(NOW_SLOTS))
    shows = _shows(loaded, 'C1@mp')
    # 4. day-1 linked shows keep their links and details
    for s, _e in D1_SLOTS:
        assert shows[_ts(s)]['catchup-id'] == 'plugin://x/?id=d1-%d&t=u7d' % s
        assert shows[_ts(s)]['desc'][0][0] == 'Rich %d' % s
    for s, _e in NOW_SLOTS:
        assert shows[_ts(s)]['catchup-id'] == 'plugin://x/?id=d2-%d&t=u7d' % s
        assert shows[_ts(s)]['desc'][0][0] == 'Rich %d' % s
    # 5. still gone
    assert _ts(D1 + 10 * 3600) not in shows
    assert _ts(D1) not in _shows(loaded, CUSTOM)

    # and the next writer's load keeps them again
    reloaded = _load(_write(tmp_path, loaded))
    shows = _shows(reloaded, 'C1@mp')
    assert sorted(shows) == sorted(_ts(s) for s, _e in D1_SLOTS + NOW_SLOTS)
    assert all(p['catchup-id'] for p in shows.values())


def test_savelocked_round_trip_of_export_style_entries_is_byte_identical(env, monkeypatch):
    """XMLTVS._saveLocked writes a guide holding export-style entries (+0000
    times, plugin:// replay links with '&', kept history), a real XMLTVS loads
    it back and _saveLocked writes it again: the two files are byte-identical."""
    tmp_path, folder, cfg = env
    monkeypatch.setattr(xmltvs.XMLTVS, 'buildGenres', lambda self, *a, **k: True)
    header = {'date': '20990101000000', 'generator-info-name': 'g', 'generator-info-url': 'u',
              'source-info-name': 's', 'source-info-url': 'v'}
    link = 'plugin://plugin.video.movistarplus/?action=play&stype=u7d&id=%d'
    progs = [
        {'channel': 'C1@mp', 'start': _ts(D1), 'stop': _ts(D1 + 3600), 'catchup-id': link % 1,
         'title': [('Informe [COLOR orange](T1)[/COLOR]', '')], 'sub-title': [('', '')],
         'desc': [('Kept <details> & more', '')]},
        {'channel': 'C1@mp', 'start': _ts(NOW), 'stop': _ts(NOW + 3600), 'catchup-id': link % 2,
         'title': [('Noticias', '')], 'sub-title': [('Episodio 2', '')]},
        {'channel': 'C1@mp', 'start': _ts(NOW + 3600), 'stop': _ts(NOW + 7200), 'catchup-id': '',
         'title': [('Sin enlace', '')], 'desc': [('Plain', '')]},
    ]
    first = tmp_path / 'first.xml'
    x = xmltvs.XMLTVS.__new__(xmltvs.XMLTVS)
    x.m3u = _M3U()
    x.writable = True
    x.XMLTVFile = str(first)
    x.XMLTVDATA = {'data': dict(header),
                   'channels': [{'id': 'C1@mp', 'display-name': [('Chan One', '')],
                                 'icon': [{'src': 'http://logo.invalid/c1.png?a=1&b=2'}]}],
                   'recordings': [], 'programmes': progs}
    x._saveLocked(reset=False)
    written = first.read_bytes()
    assert written.count(b'&amp;stype=u7d') == 2

    loaded = _load(first)
    assert [p['catchup-id'] for p in loaded.XMLTVDATA['programmes']] == [link % 1, link % 2, '']
    second = tmp_path / 'second.xml'
    loaded.XMLTVFile = str(second)
    loaded.writable = True
    # The <tv> header isn't read back (xmltv.read_data returns {}, so a load
    # always starts a fresh header, and saves normally regenerate it);
    # give both saves the same header so the guide content is what's compared.
    loaded.XMLTVDATA['data'] = dict(header)
    loaded._saveLocked(reset=False)
    assert second.read_bytes() == written


def test_history_older_than_the_window_is_trimmed(env, monkeypatch):
    """Linked shows older than the channel's replay window (7 days) go."""
    tmp_path, folder, cfg = env
    old = NOW - 8 * DAY
    day1 = _X()
    _sync(monkeypatch, folder, cfg, day1, _export([(old, old + 3600)] + NOW_SLOTS, 'd1'), _rich(NOW_SLOTS))
    # the merge already drops links older than the window
    assert _ts(old) not in _shows(day1, 'C1@mp')
    # an old linked show already in the file is trimmed at load
    day1.XMLTVDATA['programmes'].append({'channel': 'C1@mp', 'start': _ts(old), 'stop': _ts(old + 3600),
                                          'catchup-id': 'plugin://x/?id=old', 'title': [('Old', '')]})
    loaded = _load(_write(tmp_path, day1))
    assert _ts(old) not in _shows(loaded, 'C1@mp')
    assert sorted(_shows(loaded, 'C1@mp')) == sorted(_ts(s) for s, _e in NOW_SLOTS)
