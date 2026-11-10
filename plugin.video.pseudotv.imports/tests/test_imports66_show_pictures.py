# -*- coding: utf-8 -*-
"""imports.66: show pictures in the guide -- the Kodi side.

TV3, À Punt and MTV showed a "BesTivi / Get it on Google Play" advert as the
picture of every show: epg.best answers every picture link of these channels
with that same image, and Kodi falls back to the channel logo only when a show
has no picture at all (CFileItem(CPVREpgInfoTag), xbmc/FileItem.cpp, Kodi 21).

imports.66 gives every imported channel a Pictures setting (Auto / Show / Hide,
stored as record['show_pictures'] = 'on' / 'off', absent = Auto) and every
import an automatic check (on unless switched off) that samples up to 8 of each
channel's show pictures about once a day. The rule (revision 2026-11-10) is
"most", not "all": a channel is a placeholder when the most common picture
covers at least 5 of the pictures read (all of them when fewer were read),
real when it covers 2 fewer, and one short keeps the previous result (a
buffer). The decisions live in picture_helpers
(test_imports66_picture_helpers.py); this file tests where they meet the
add-on:

  - Imports.syncAll end to end, on the imports.58 harness (a fake channel
    store, playlist and guide; fetchSource and the picture download replaced):
    which channels lose their show pictures, the results file
    (CACHE_LOC/show_pictures.json), how many pictures are downloaded (real 6,
    placeholder 5, a shared link once), mixed channels and the buffer, small
    channels, and every guide path -- fresh, 304 from the cached copy, guide
    down, nothing matched, replay links merge failed, skipped by the refresh
    interval;
  - Imports._checkShowPictures on its own: which channels, in which order,
    the time budget and Kodi closing;
  - Imports._pictureFingerprint against a fake streamed `requests` response,
    and the shared User-Agent (_fetchLogo's is unchanged);
  - loadPictureVerdicts / _savePictureVerdicts;
  - server.py (POST /imports/channel/edit.json, GET /imports.json) and
    manager.html, by source scan (project convention, test_imports40), plus
    the two endpoints run without a socket.

Picture links use the RFC 5737 documentation ranges.
"""
import ast
import calendar
import collections
import copy
import hashlib
import io
import json
import os
import re
import sys
import time

import pytest
import requests

HERE       = os.path.dirname(os.path.abspath(__file__))
ADDON_ROOT = os.path.dirname(HERE)
LIB        = os.path.join(ADDON_ROOT, 'resources', 'lib')
HTML_PATH  = os.path.join(ADDON_ROOT, 'remotes', 'manager.html')
SERVER_PY  = os.path.join(LIB, 'server.py')
if LIB not in sys.path:
    sys.path.insert(0, LIB)

import imports as imports_mod   # noqa: E402
import picture_helpers          # noqa: E402
import replay_helpers           # noqa: E402
from imports import Imports     # noqa: E402


IMPORT_ID = 'imp'
M3U_URL   = 'https://example.invalid/list.m3u'
EPG_URL   = 'https://example.invalid/guide.xml'
PICS      = 'http://192.0.2.20/epg'
CUSTOM_ID   = '3330312e582d46696c65732e3c6d6435@PseudoTV_Live'     # an own (Custom) channel
CUSTOM_ICON = 'http://192.0.2.30/custom/saturday-morning.jpg'
RATING_ICON = 'http://192.0.2.40/ratings/12.png'
H     = 3600
START = calendar.timegm((2099, 1, 1, 0, 0, 0))
TITLES = ('Morning News', 'Cooking Today', 'The Quiz', 'Wild Coasts', 'Late Film',
          'Night Talk', 'Street Food', 'Space Report', 'Old Masters', 'Last Call')

# imports.66: the shows of a ten-show channel whose pictures the check takes,
# in reading order -- picture_helpers.SAMPLES (8), spread from the first show
# to the last (pick_samples).
SAMPLED = (0, 1, 2, 3, 5, 6, 7, 9)
# Downloads before a channel's result is settled (picture_helpers.settled),
# with 8 samples: every picture different -> 6; every picture one image -> 5.
REAL_READS = 6
PLACEHOLDER_READS = 5

# The import's channels (guide ids; the records are '<id>@imp'):
#   P -- every show has its own picture link, and every link serves the same
#        image (the epg.best case): a placeholder;
#   R -- real pictures, a different one per show;
#   S -- every show shares ONE picture link: a placeholder, downloaded once;
#   X -- real pictures, a different one per show (set to Hide in some tests).
CHANNELS = ('P', 'R', 'S', 'X')


def _ts(epoch):
    return time.strftime('%Y%m%d%H%M%S', time.gmtime(epoch)) + ' +0000'


def _pic(ch, i):
    """Show i's picture link on guide channel `ch`."""
    if ch == 'S':
        return '%s/S/all.jpg' % (PICS,)
    return '%s/%s/%d.jpg' % (PICS, ch, i)


def _served(url):
    """What each picture link serves, as a fingerprint: P's links all the
    same advert, S's single link a filler image, every other link its own
    picture."""
    if url.startswith(PICS + '/P/'):
        return 'advert'
    if url == _pic('S', 0):
        return 'filler'
    return 'own:' + url


def _serving(**adverts):
    """imports.66: `_served`, except that the listed shows of the named
    channels serve the advert too -- _serving(M=(0, 2)) makes shows 0 and 2
    of channel M one image (a mixed channel)."""
    links = {_pic(ch, i) for ch, shows in adverts.items() for i in shows}

    def served(url):
        return 'advert' if url in links else _served(url)
    return served


def _guide(channels=CHANNELS, shows=None, titles=None):
    """The provider's guide: per channel, differently titled shows six hours
    apart, each with its picture; the first show also carries a rating with
    the rating's own icon. Each channel has len(TITLES) (10) shows titled
    from TITLES, unless `shows` ({channel: count}) gives fewer or `titles`
    ({channel: titles}) its own titles."""
    out = ['<?xml version="1.0" encoding="UTF-8"?>', '<tv>']
    for ch in channels:
        out.append('<channel id="%s"><display-name>Chan %s</display-name></channel>' % (ch, ch))
    for ch in channels:
        names = (titles or {}).get(ch, TITLES)
        for i in range((shows or {}).get(ch, len(names))):
            start = START + i * 6 * H
            rating = ('<rating system="ES"><value>12</value><icon src="%s"/></rating>' % (RATING_ICON,)) if i == 0 else ''
            out.append('<programme start="%s" stop="%s" channel="%s"><title lang="es">%s</title>'
                       '<icon src="%s"/>%s</programme>' % (_ts(start), _ts(start + H), ch, names[i], _pic(ch, i), rating))
    out.append('</tv>')
    return '\n'.join(out).encode('utf-8')


def _fresh(content):
    """A guide download that returns `content`."""
    return {'status': 200, 'content': content}


GUIDE = _guide()

# Parses fine, but none of its ids or names belong to the import's channels.
UNMATCHED_GUIDE = b'''<?xml version="1.0" encoding="UTF-8"?>
<tv>
<channel id="zz"><display-name>Somebody Else</display-name></channel>
<programme start="20990101000000 +0000" stop="20990101010000 +0000" channel="zz"><title>Elsewhere</title><icon src="http://192.0.2.20/epg/zz/0.jpg"/></programme>
</tv>'''

FRESH        = {'status': 200, 'content': GUIDE}
NOT_MODIFIED = {'status': 304}
DOWN         = {'status': 0, 'error': 'Temporary failure in name resolution'}
UNMATCHED    = {'status': 200, 'content': UNMATCHED_GUIDE}

# The import's playlist, for a first sync (every channel new).
PLAYLIST = {'status': 200, 'content': ('#EXTM3U\n' + ''.join(
    '#EXTINF:-1 tvg-id="%s" tvg-name="Chan %s" group-title="IMP",Chan %s\nplugin://plugin.video.example/?id=%s\n'
    % (ch, ch, ch, ch) for ch in CHANNELS)).encode('utf-8')}


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Per-test cache folder (the results file and the guide cache land in
    tmp_path), inert imports.43 disk check, logo caching off, fixed retry
    settings, fresh self-heal throttle."""
    monkeypatch.setattr(imports_mod, 'CACHE_LOC', str(tmp_path))
    monkeypatch.setattr(imports_mod, '_isDiskMissing', lambda iid, scope: False)
    real_getBool = imports_mod.SETTINGS.getSettingBool
    real_getInt  = imports_mod.SETTINGS.getSettingInt

    def fake_getBool(key, *a):
        if key == 'Imports_Cache_Logos':
            return False
        return real_getBool(key, *a)

    def fake_getInt(key, *a):
        if key == 'Imports_Failure_Retry_Curve':
            return 1
        if key == 'Imports_Failure_Abandon_Hours':
            return 0
        return real_getInt(key, *a)
    monkeypatch.setattr(imports_mod.SETTINGS, 'getSettingBool', fake_getBool)
    monkeypatch.setattr(imports_mod.SETTINGS, 'getSettingInt', fake_getInt)
    imports_mod._GUIDE_SELFHEAL_LAST.clear()
    yield
    imports_mod._GUIDE_SELFHEAL_LAST.clear()


# ================================================================ harness

class _Store(object):
    """Holds the very dicts it is given, like Channels.channelDATA (the
    imports.58 harness)."""
    def __init__(self, imports_list, channels_list):
        self.imports  = list(imports_list)
        self.channels = list(channels_list)

    def getImports(self):
        return list(self.imports)

    def getChannels(self):
        return list(self.channels)

    def setChannels(self, channels=None, modified_ids=None, deleted_ids=None):
        if channels is not None:
            self.channels = list(channels)
        return True

    def setImports(self, data):
        self.imports = list(data)
        return True


class _M3U(object):
    def __init__(self):
        self.stations = {}

    def addStation(self, s):
        self.stations[s.get('id')] = s

    def delStation(self, s):
        self.stations.pop(s.get('id'), None)


class _XMLTV(object):
    def __init__(self, channels=None, programmes=None):
        self.XMLTVDATA = {'channels': list(channels or []), 'programmes': list(programmes or []),
                          'recordings': []}


class _Clock(object):
    """Stands in for imports.py's `time` module: the real one, except that
    monotonic() reads a clock the test moves (each picture download takes
    `per_download` seconds)."""
    def __init__(self, per_download):
        self.now = 0.0
        self.per_download = per_download

    def monotonic(self):
        return self.now

    def __getattr__(self, name):
        return getattr(time, name)


class _Service(object):
    """Kodi asks to stop once `after` pictures have been downloaded."""
    def __init__(self, downloads, after):
        self.downloads = downloads
        self.after = after

    def _interrupt(self):
        return len(self.downloads) >= self.after


class _Run(object):
    def __init__(self):
        self.fetches  = []    # fetchSource calls (playlist / guide)
        self.fp_calls = []    # picture downloads, in order
        self.saves    = []    # each write of the results file
        self.logs     = []    # (message, level)
        self.m3u      = _M3U()
        self.result   = None


def _record(ch, iid=IMPORT_ID, **extra):
    cid = '%s@%s' % (ch, iid)
    n = CHANNELS.index(ch) + 1 if ch in CHANNELS else 50 + ord(ch[0]) % 40
    url = 'plugin://plugin.video.example/?id=%s' % (ch,)
    rec = {'id': cid, 'epg_id': ch, 'name': 'Chan %s' % (ch,), 'import_source': iid,
           'type': 'import', 'enabled': True, 'number': n, 'assigned_number': n,
           'is_orphan': False, 'unallocated': False, 'source_url': url, 'url': url,
           'group': ['IMP'], 'logo': '', 'operator_overrides': []}
    rec.update(extra)
    return rec


def _custom_record():
    """An own (Custom) channel. Its record even carries a stray 'off' (the
    edit endpoint doesn't check the channel type): imported channels only are
    ever touched."""
    return {'id': CUSTOM_ID, 'type': 'Custom', 'name': 'Saturday Morning', 'number': 100,
            'path': ['special://profile/playlists/video/cartoons.xsp'], 'logo': '',
            'show_pictures': 'off', 'operator_overrides': ['show_pictures']}


def _cfg(**over):
    cfg = {'id': IMPORT_ID, 'name': 'IMP', 'enabled': True,
           'm3u_url': M3U_URL, 'epg_url': EPG_URL, 'start_num': 1,
           'refresh_interval_min': 600,
           'last_sync_at': int(time.time()) - 11 * H,      # the refresh interval has passed
           'tombstones': []}
    cfg.update(over)
    return cfg


def _store(modes=None, detect=None, records=None, record_extra=None, **cfg_over):
    """The import with its four channels plus the Custom channel. `modes`
    ({'X': 'off', ...}) sets channels' Pictures setting the way the dashboard
    stores it; `detect` sets the import's automatic check switch (None = never
    set, which means on); `record_extra` adds fields to every imported
    channel's record."""
    if records is None:
        records = []
        for ch in CHANNELS:
            mode = (modes or {}).get(ch)
            if mode:
                records.append(_record(ch, show_pictures=mode, operator_overrides=['show_pictures'],
                                       **(record_extra or {})))
            else:
                records.append(_record(ch, **(record_extra or {})))
        records.append(_custom_record())
    cfg = _cfg(**cfg_over)
    if detect is not None:
        cfg['detect_placeholder_pictures'] = detect
    return _Store([cfg], records)


def _custom_programme():
    return {'channel': CUSTOM_ID, 'start': _ts(START), 'stop': _ts(START + H),
            'title': [('Cartoons', '')], 'icon': [{'src': CUSTOM_ICON}]}


def _xmltv(with_import_slice=False):
    """Guide data as loaded from disk: the Custom channel's show (with its
    picture) and, optionally, the import's slice from an earlier sync
    (namespaced ids, show pictures included)."""
    channels = [{'id': CUSTOM_ID, 'display-name': [('Saturday Morning', '')]}]
    programmes = [_custom_programme()]
    if with_import_slice:
        for kind, item in Imports().parseXMLTV(GUIDE):
            if kind == 'channel':
                item['id'] = '%s@%s' % (item['id'], IMPORT_ID)
                channels.append(item)
            else:
                item['channel'] = '%s@%s' % (item['channel'], IMPORT_ID)
                programmes.append(item)
    return _XMLTV(channels=channels, programmes=programmes)


def _sync(monkeypatch, store, xmltv, guide=None, force_scope='all', served=_served,
          clock=None, stop_after=None, patch=None, playlist=None, real_download=False):
    """One syncAll. The playlist answers `playlist` (default 304: the stored
    records are reused), the guide answers `guide` (FRESH by default), and
    each picture link serves `served(url)` -- recorded, never downloaded
    (real_download=True keeps the add-on's own download step; the test then
    fakes requests.get). `force_scope='all'` is the dashboard's refresh-all
    kick; None = a routine cycle."""
    run = _Run()
    service = _Service(run.fp_calls, stop_after) if stop_after is not None else None
    importer = Imports(channels=store, m3u=run.m3u, xmltv=xmltv, service=service)
    answer = FRESH if guide is None else guide

    def _fetch(url, etag=None, last_modified=None, **kw):
        run.fetches.append(url)
        out = {'status': 0, 'content': None, 'etag': None, 'last_modified': None, 'error': None}
        if url == M3U_URL:
            out.update(playlist or {'status': 304})
        elif url == EPG_URL:
            out.update(answer)
        else:
            raise AssertionError('unexpected fetch: %r' % (url,))
        return out

    def _fingerprint(url):
        run.fp_calls.append(url)
        if clock is not None:
            clock.now += clock.per_download
        return served(url)

    real_save = importer._savePictureVerdicts

    def _save(verdicts):
        run.saves.append(copy.deepcopy(verdicts))
        return real_save(verdicts)

    def _log(msg, level=None):
        run.logs.append((msg, level))

    monkeypatch.setattr(importer, 'fetchSource', _fetch)
    if not real_download:
        monkeypatch.setattr(importer, '_pictureFingerprint', _fingerprint)
    monkeypatch.setattr(importer, '_savePictureVerdicts', _save)
    monkeypatch.setattr(importer, 'log', _log)
    if patch is not None:
        patch(importer)
    run.result = importer.syncAll(force_scope=force_scope)
    return run


def _cid(ch):
    return ch if '@' in ch else '%s@%s' % (ch, IMPORT_ID)


def _shows(xmltv, ch):
    return [p for p in xmltv.XMLTVDATA['programmes'] if p.get('channel') == _cid(ch)]


def _pictures(xmltv, ch):
    """The channel's show pictures in the guide about to be published: one
    entry per show, its picture link or None."""
    return [picture_helpers.picture_src(p) or None for p in _shows(xmltv, ch)]


def _all_pictures(ch, n=len(TITLES)):
    return [_pic(ch, i) for i in range(n)]


def _no_pictures(xmltv, ch, n=len(TITLES)):
    """Every show of the channel (`n` of them) is still there, without a
    picture of its own."""
    shows = _shows(xmltv, ch)
    return len(shows) == n and all('icon' not in p for p in shows)


def _sampled(ch, k=len(SAMPLED)):
    """The picture links of channel `ch`'s first `k` samples, in reading
    order (a ten-show channel)."""
    return [_pic(ch, i) for i in SAMPLED[:k]]


def _calls(run, ch):
    prefix = '%s/%s/' % (PICS, ch)
    return [u for u in run.fp_calls if u.startswith(prefix)]


def _results_path(tmp_path):
    return tmp_path / picture_helpers.VERDICTS_FILE


def _results(tmp_path):
    with open(str(_results_path(tmp_path)), encoding='utf-8') as fh:
        return json.load(fh)


def _seed_results(tmp_path, data):
    _results_path(tmp_path).write_text(json.dumps(data), encoding='utf-8')


def _verdict(placeholder, age_s, samples=4):
    return {'placeholder': placeholder, 'checked_at': int(time.time()) - age_s, 'samples': samples}


def _messages(run, needle):
    return [m for m, _level in run.logs if needle in m]


# ================================================================ syncAll: a full sync (plan tests 7, 8)

def test_placeholder_channels_lose_their_show_pictures_real_ones_keep_them(monkeypatch, tmp_path):
    store, xmltv = _store(modes={'X': 'off'}), _xmltv()
    t0 = int(time.time())
    run = _sync(monkeypatch, store, xmltv)
    t1 = int(time.time())
    assert _no_pictures(xmltv, 'P')                         # one image behind every link
    assert _no_pictures(xmltv, 'S')                         # one link for every show
    assert _pictures(xmltv, 'R') == _all_pictures('R')      # real pictures stay
    assert _no_pictures(xmltv, 'X')                         # Hide, though its pictures are real
    assert _pictures(xmltv, CUSTOM_ID) == [CUSTOM_ICON]     # own channels are never touched
    results = _results(tmp_path)
    # imports.66: 'samples' = the pictures read before the result was settled
    assert results['P@imp'] == {'placeholder': True, 'checked_at': results['P@imp']['checked_at'],
                                'samples': PLACEHOLDER_READS}
    assert results['S@imp'] == {'placeholder': True, 'checked_at': results['S@imp']['checked_at'],
                                'samples': PLACEHOLDER_READS}
    assert results['R@imp'] == {'placeholder': False, 'checked_at': results['R@imp']['checked_at'],
                                'samples': REAL_READS}
    assert all(t0 <= v['checked_at'] <= t1 for v in results.values())
    assert CUSTOM_ID not in results
    assert len(run.saves) == 1                              # written once per sync


def test_only_the_shows_own_picture_goes(monkeypatch):
    """A rating's icon is the rating's, not the show's picture; titles, times
    and everything else on a hidden show stay."""
    xmltv = _xmltv()
    _sync(monkeypatch, _store(), xmltv)
    first = _shows(xmltv, 'P')[0]
    assert 'icon' not in first
    assert first['rating'][0]['icon'] == [{'src': RATING_ICON}]
    assert first['title'] == [(TITLES[0], 'es')]
    assert first['start'] == _ts(START)


def test_downloads_real_pictures_cost_six_a_placeholder_five_a_shared_link_one(monkeypatch):
    """imports.66: 8 samples, spread from a channel's first show to its last;
    reading stops as soon as the samples not read yet can't change the
    result (picture_helpers.settled)."""
    xmltv = _xmltv()
    run = _sync(monkeypatch, _store(), xmltv)
    assert picture_helpers.pick_samples(_shows(xmltv, 'R'), 'R@imp') == _sampled('R')   # first show to last
    assert _calls(run, 'R') == _sampled('R', REAL_READS)           # 6 different: can't reach 5 alike
    assert _calls(run, 'X') == _sampled('X', REAL_READS)
    assert _calls(run, 'P') == _sampled('P', PLACEHOLDER_READS)    # 5 alike: placeholder whatever follows
    assert _calls(run, 'S') == [_pic('S', 0)]                      # one shared link: read once
    assert max(collections.Counter(run.fp_calls).values()) == 1    # no link is read twice in a sync
    assert len(run.fp_calls) == 2 * REAL_READS + PLACEHOLDER_READS + 1


def test_the_next_sync_downloads_nothing_and_leaves_the_file_alone(monkeypatch, tmp_path):
    store, xmltv = _store(modes={'X': 'off'}), _xmltv()
    _sync(monkeypatch, store, xmltv)
    before = _results_path(tmp_path).read_bytes()
    run = _sync(monkeypatch, store, xmltv)                  # e.g. the next dashboard Save's refresh
    assert run.fp_calls == []
    assert run.saves == []
    assert _results_path(tmp_path).read_bytes() == before
    assert _no_pictures(xmltv, 'P') and _no_pictures(xmltv, 'S') and _no_pictures(xmltv, 'X')
    assert _pictures(xmltv, 'R') == _all_pictures('R')


def test_show_beats_a_placeholder_result(monkeypatch, tmp_path):
    _seed_results(tmp_path, {'P@imp': _verdict(True, H)})
    xmltv = _xmltv()
    _sync(monkeypatch, _store(modes={'P': 'on'}), xmltv)
    assert _pictures(xmltv, 'P') == _all_pictures('P')
    assert _no_pictures(xmltv, 'S')                         # Auto channels still follow the check


def test_new_channels_are_checked_on_their_first_sync(monkeypatch, tmp_path):
    """A new import: the playlist is read in full and every channel is new
    (no stored record yet)."""
    store, xmltv = _store(records=[_custom_record()]), _xmltv()
    run = _sync(monkeypatch, store, xmltv, playlist=PLAYLIST)
    assert {c['id'] for c in store.channels if c.get('type') == 'import'} == {_cid(ch) for ch in CHANNELS}
    assert _no_pictures(xmltv, 'P') and _no_pictures(xmltv, 'S')
    assert _pictures(xmltv, 'R') == _all_pictures('R')
    assert _pictures(xmltv, 'X') == _all_pictures('X')
    assert {cid: v['placeholder'] for cid, v in _results(tmp_path).items()} == {
        'P@imp': True, 'R@imp': False, 'S@imp': True, 'X@imp': False}
    assert len(_calls(run, 'R')) == REAL_READS


def test_a_channel_whose_import_is_no_longer_listed_counts_as_check_off(monkeypatch, tmp_path):
    """Records of an import that isn't in the import list any more: Hide
    still hides, but there is no switch saying the check is on."""
    _seed_results(tmp_path, {'Q@old': _verdict(True, H)})
    records = [_record(ch) for ch in CHANNELS] + [
        _record('Q', iid='old'), _record('W', iid='old', show_pictures='off', operator_overrides=['show_pictures'])]
    xmltv = _xmltv()
    xmltv.XMLTVDATA['programmes'] += _progs('Q@old', _links('Q')) + _progs('W@old', _links('W'))
    _sync(monkeypatch, _store(records=records), xmltv)
    assert _pictures(xmltv, 'Q@old') == _links('Q')
    assert _shows(xmltv, 'W@old') and all('icon' not in p for p in _shows(xmltv, 'W@old'))
    assert _results(tmp_path)['Q@old']['placeholder'] is True     # still present: kept


def test_hide_strips_real_pictures_even_with_the_check_switched_off(monkeypatch, tmp_path):
    xmltv = _xmltv()
    run = _sync(monkeypatch, _store(modes={'R': 'off'}, detect=False), xmltv)
    assert run.fp_calls == []                                # switch off: nothing downloaded
    assert _no_pictures(xmltv, 'R')
    assert _pictures(xmltv, 'P') == _all_pictures('P')      # never checked: pictures stay
    assert not _results_path(tmp_path).exists()


# ================================================================ syncAll: the "most" rule (plan revision 2026-11-10)

# imports.66: show titles in scripts norm_title can't key (it keeps ASCII only).
NON_LATIN = ('Новости', 'Καλημέρα', '新闻联播', 'ニュース', '뉴스', 'الأخبار', 'חדשות', 'Погода', 'Ταινία', '天气预报')


def test_a_mixed_channel_where_most_samples_are_one_image_loses_its_pictures(monkeypatch, tmp_path):
    """A provider's filler stands in only for the shows it has no picture
    for, so the channels it hits are mixed (on 2026-11-08 epg.best's advert
    was the picture of 43 of TV3's 50 titles). M has 10 shows, 2 with a real
    picture; 6 of its 8 samples are the advert. "All samples alike" called
    it real; "most" hides it -- every one of its shows, the two with a real
    picture included -- while R, in the same guide, keeps its pictures."""
    served = _serving(M=(0, 2, 3, 4, 5, 7, 8, 9))
    assert [served(u) for u in _sampled('M')].count('advert') == 6
    xmltv = _xmltv()
    run = _sync(monkeypatch, _store(records=[_record('M'), _record('R'), _custom_record()]), xmltv,
                guide=_fresh(_guide(('M', 'R'))), served=served)
    assert _calls(run, 'M') == _sampled('M', 7)          # settled on the 5th advert, the 7th picture
    assert _calls(run, 'R') == _sampled('R', REAL_READS)
    results = _results(tmp_path)
    assert (results['M@imp']['placeholder'], results['M@imp']['samples']) == (True, 7)
    assert results['R@imp']['placeholder'] is False
    assert _no_pictures(xmltv, 'M')
    assert _pictures(xmltv, 'R') == _all_pictures('R')
    assert _pictures(xmltv, CUSTOM_ID) == [CUSTOM_ICON]


@pytest.mark.parametrize('was_placeholder', [True, False], ids=['was-placeholder', 'was-real'])
def test_four_of_eight_alike_keeps_the_previous_result(monkeypatch, tmp_path, was_placeholder):
    """The buffer: 4 of 8 alike is one short of placeholder (5) and one more
    than real (3 at most), so the previous result stands -- a mixed channel
    doesn't flip between logo and filler from one day's sample to the next.
    B's result is two days old (due). The new check reads all 8 (a result in
    the buffer is never settled early) and records the previous result with
    today's date: a placeholder stays stripped, a real channel keeps its
    pictures."""
    _seed_results(tmp_path, {'B@imp': _verdict(was_placeholder, 2 * 24 * H, samples=8)})
    served = _serving(B=(0, 2, 5, 7))                    # the 1st, 3rd, 5th and 7th sample
    assert [served(u) for u in _sampled('B')].count('advert') == 4
    xmltv = _xmltv()
    t0 = int(time.time())
    run = _sync(monkeypatch, _store(records=[_record('B'), _custom_record()]), xmltv,
                guide=_fresh(_guide(('B',))), served=served)
    assert _calls(run, 'B') == _sampled('B')
    result = _results(tmp_path)['B@imp']
    assert result['placeholder'] is was_placeholder
    assert result['checked_at'] >= t0 and result['samples'] == picture_helpers.SAMPLES
    if was_placeholder:
        assert _no_pictures(xmltv, 'B')
    else:
        assert _pictures(xmltv, 'B') == _all_pictures('B')


@pytest.mark.parametrize('alike, reads', [(3, 8), (2, 7), (1, REAL_READS)], ids=['3-alike', '2-alike', 'all-different'])
def test_a_placeholder_channel_with_three_or_fewer_alike_turns_real(monkeypatch, tmp_path, alike, reads):
    """P was a placeholder (two days ago, so due). Today its shows have real
    pictures (epg.best dropped its advert on 2026-11-09), a few sharing one
    series picture: with at most 3 of 8 alike -- out of the buffer -- P is
    real again and its pictures come back."""
    _seed_results(tmp_path, {'P@imp': _verdict(True, 2 * 24 * H, samples=PLACEHOLDER_READS)})
    same = set(_sampled('P', alike))                     # the first `alike` samples: one series picture

    def served(url):
        if url.startswith(PICS + '/P/'):
            return 'series' if url in same else 'own:' + url
        return _served(url)
    xmltv = _xmltv()
    t0 = int(time.time())
    run = _sync(monkeypatch, _store(), xmltv, served=served)
    assert _calls(run, 'P') == _sampled('P', reads)
    result = _results(tmp_path)['P@imp']
    assert result['placeholder'] is False and result['checked_at'] >= t0
    assert _pictures(xmltv, 'P') == _all_pictures('P')
    assert _no_pictures(xmltv, 'S')                      # S, all one link, is still a placeholder


def test_shows_titled_in_a_non_latin_script_are_sampled(monkeypatch, tmp_path):
    """norm_title keeps ASCII letters only, so these titles come out empty
    and a channel titled in Cyrillic, Greek, Chinese... was never sampled.
    Such a title is keyed on its own letters now: N is checked like any
    other channel and its one-image pictures go."""
    assert all(replay_helpers.norm_title({'title': [(t, 'es')]}) == '' for t in NON_LATIN)    # the premise
    xmltv = _xmltv()
    run = _sync(monkeypatch, _store(records=[_record('N'), _record('R'), _custom_record()]), xmltv,
                guide=_fresh(_guide(('N', 'R'), titles={'N': NON_LATIN})),
                served=_serving(N=range(len(NON_LATIN))))
    assert _calls(run, 'N') == _sampled('N', PLACEHOLDER_READS)     # one per title, spread as usual
    assert _results(tmp_path)['N@imp']['placeholder'] is True
    assert _no_pictures(xmltv, 'N')
    assert [p['title'] for p in _shows(xmltv, 'N')] == [[(t, 'es')] for t in NON_LATIN]   # titles untouched
    assert _pictures(xmltv, 'R') == _all_pictures('R')


def test_small_channels_with_fewer_than_eight_shows(monkeypatch, tmp_path):
    """A channel with fewer than 8 shows gives fewer samples, and when fewer
    than 5 pictures can be read all of them must be alike:
      F -- 4 shows, all one image: placeholder, after all 4;
      G -- 4 shows, 3 alike: one short (the buffer), no previous result: real;
      K -- 6 shows, all one image: placeholder, after 5;
      J -- 2 shows: too few to tell (MIN_SAMPLES 3), nothing downloaded."""
    chans = ('F', 'G', 'K', 'J')
    xmltv = _xmltv()
    run = _sync(monkeypatch, _store(records=[_record(ch) for ch in chans] + [_custom_record()]), xmltv,
                guide=_fresh(_guide(chans, shows={'F': 4, 'G': 4, 'K': 6, 'J': 2})),
                served=_serving(F=range(4), G=range(3), K=range(6), J=range(2)))
    assert _calls(run, 'F') == _all_pictures('F', 4)
    assert _calls(run, 'G') == _all_pictures('G', 4)
    assert _calls(run, 'K') == _all_pictures('K', 5)
    assert _calls(run, 'J') == []
    assert {cid: (v['placeholder'], v['samples']) for cid, v in _results(tmp_path).items()} == {
        'F@imp': (True, 4), 'G@imp': (False, 4), 'K@imp': (True, 5)}
    assert _no_pictures(xmltv, 'F', n=4) and _no_pictures(xmltv, 'K', n=6)
    assert _pictures(xmltv, 'G') == _all_pictures('G', 4)
    assert _pictures(xmltv, 'J') == _all_pictures('J', 2)


# ================================================================ syncAll: pictures coming back (plan test 9)

def test_switching_the_check_off_brings_a_flagged_channels_pictures_back(monkeypatch, tmp_path):
    _seed_results(tmp_path, {'P@imp': _verdict(True, H)})
    xmltv = _xmltv()
    run = _sync(monkeypatch, _store(detect=False), xmltv)
    assert run.fp_calls == []
    assert _pictures(xmltv, 'P') == _all_pictures('P')
    assert _results(tmp_path)['P@imp']['placeholder'] is True    # the result stays on file


@pytest.mark.parametrize('second_guide', [FRESH, NOT_MODIFIED], ids=['fresh-guide', 'guide-304-from-cache'])
def test_hide_then_auto_with_the_check_off_brings_the_pictures_back(monkeypatch, tmp_path, second_guide):
    store, xmltv = _store(modes={'X': 'off'}), _xmltv()
    _sync(monkeypatch, store, xmltv)
    assert _no_pictures(xmltv, 'X') and _no_pictures(xmltv, 'P')
    assert (tmp_path / ('epg_%s.xml' % IMPORT_ID)).exists()      # the guide's cached copy
    # The dashboard: X back to Auto (field and override removed, as
    # edit.json does) and the import's check switched off; then its kick.
    x = next(c for c in store.channels if c['id'] == 'X@imp')
    x.pop('show_pictures')
    x['operator_overrides'].remove('show_pictures')
    store.imports[0]['detect_placeholder_pictures'] = False
    run = _sync(monkeypatch, store, xmltv, guide=second_guide, force_scope='all')
    if second_guide is NOT_MODIFIED:
        assert _messages(run, 'EPG 304 hydrated from cache')
    assert run.fp_calls == []
    for ch in CHANNELS:
        assert _pictures(xmltv, ch) == _all_pictures(ch), ch
    assert _pictures(xmltv, CUSTOM_ID) == [CUSTOM_ICON]


# ================================================================ syncAll: guide paths without fresh data (plan tests 10, 11)

def test_guide_down_no_downloads_saved_results_and_a_new_hide_still_apply(monkeypatch, tmp_path):
    """The guide download fails and there is no cached copy: the previous
    guide is kept. Nothing is checked (no fresh data -- P is due for a
    re-check, but there is nothing to sample), the saved results still apply
    and keep their date, and a Hide set while the guide is down strips the
    kept shows."""
    seeded = {'P@imp': _verdict(True, 2 * 24 * H), 'R@imp': _verdict(False, H, samples=REAL_READS)}
    _seed_results(tmp_path, seeded)
    xmltv = _xmltv(with_import_slice=True)
    run = _sync(monkeypatch, _store(modes={'R': 'off'}), xmltv, guide=DOWN)
    assert _messages(run, 'keeping the previous guide')
    assert run.fp_calls == []
    assert _no_pictures(xmltv, 'P')                          # saved result
    assert _no_pictures(xmltv, 'R')                          # new Hide
    assert _pictures(xmltv, 'S') == _all_pictures('S')      # never judged
    assert _pictures(xmltv, 'X') == _all_pictures('X')
    assert _pictures(xmltv, CUSTOM_ID) == [CUSTOM_ICON]
    assert run.saves == []
    assert _results(tmp_path) == seeded


def test_nothing_matched_keeps_the_guide_and_downloads_nothing(monkeypatch, tmp_path):
    seeded = {'P@imp': _verdict(True, 2 * 24 * H)}            # due, but no fresh shows to sample
    _seed_results(tmp_path, seeded)
    xmltv = _xmltv(with_import_slice=True)
    run = _sync(monkeypatch, _store(), xmltv, guide=UNMATCHED)
    assert _messages(run, 'no guide entries matched')
    assert run.fp_calls == []
    assert _no_pictures(xmltv, 'P')
    assert _pictures(xmltv, 'R') == _all_pictures('R')
    assert _results(tmp_path) == seeded


def test_links_merge_failed_no_downloads_saved_results_and_a_new_hide_still_apply(monkeypatch, tmp_path):
    """The third keep-previous path (imports.59 S10.3): the import's replay
    links are merged into its fresh guide, the merge fails, and the import
    keeps its previous guide. As on the other two paths, nothing is checked
    (P is due, but the fresh guide is thrown away), while the saved results
    and a Hide set since still apply to the kept shows.

    To get there, the import's channels need link-based replay (catch-up
    'vod' without a source, replay_helpers.is_link_catchup) and its playlist
    has to count as a known exporter's own file; syncAll is then handed an
    empty links read, and the merge raises (as in test_imports59_replay_links_
    sync's merge-failure test)."""
    seeded = {'P@imp': _verdict(True, 2 * 24 * H), 'R@imp': _verdict(False, H, samples=REAL_READS)}
    _seed_results(tmp_path, seeded)
    merges = []

    def _merge_fails(*a, **k):
        merges.append(a[0])
        raise RuntimeError('simulated merge bug')

    def patch(importer):
        monkeypatch.setattr(imports_mod.replay_helpers, 'exporter_for',
                            lambda cfg: ('plugin.video.example', {'name': 'Example'}, 'unused'))
        monkeypatch.setattr(importer, '_readLinks', lambda *a, **k: {'pairs': [], 'warning': None})
        monkeypatch.setattr(importer, '_mergeReplayLinks', _merge_fails)

    xmltv = _xmltv(with_import_slice=True)
    run = _sync(monkeypatch, _store(modes={'R': 'off'}, record_extra={'catchup': 'vod'}), xmltv, patch=patch)
    assert merges == [IMPORT_ID]                             # the fresh guide's merge, which failed
    assert _messages(run, 'replay links merge failed')
    assert run.fp_calls == []
    for ch in CHANNELS:
        assert len(_shows(xmltv, ch)) == len(TITLES), ch    # the kept guide is published
    assert _no_pictures(xmltv, 'P')                          # saved result
    assert _no_pictures(xmltv, 'R')                          # new Hide
    assert _pictures(xmltv, 'S') == _all_pictures('S')      # never judged
    assert _pictures(xmltv, 'X') == _all_pictures('X')
    assert _pictures(xmltv, CUSTOM_ID) == [CUSTOM_ICON]
    assert run.saves == []
    assert _results(tmp_path) == seeded


def test_an_import_skipped_by_its_refresh_interval_is_still_stripped(monkeypatch, tmp_path):
    _seed_results(tmp_path, {'P@imp': _verdict(True, H)})
    store = _store(modes={'R': 'off'}, last_sync_at=int(time.time()) - 5 * 60)
    xmltv = _xmltv(with_import_slice=True)
    run = _sync(monkeypatch, store, xmltv, force_scope=None)     # a routine cycle
    assert run.result[IMPORT_ID]['status'] == 'skipped'
    assert run.fetches == [] and run.fp_calls == []
    assert _no_pictures(xmltv, 'P') and _no_pictures(xmltv, 'R')
    assert _pictures(xmltv, 'S') == _all_pictures('S')
    assert _pictures(xmltv, 'X') == _all_pictures('X')
    assert run.saves == []


def test_a_failing_check_never_costs_the_import_its_guide(monkeypatch, tmp_path):
    _seed_results(tmp_path, {'P@imp': _verdict(True, H)})

    def _boom(*a, **k):
        raise RuntimeError('check exploded')

    xmltv = _xmltv()
    run = _sync(monkeypatch, _store(), xmltv,
                patch=lambda importer: monkeypatch.setattr(importer, '_checkShowPictures', _boom))
    assert _messages(run, 'show pictures check failed')
    for ch in CHANNELS:
        assert len(_shows(xmltv, ch)) == len(TITLES), ch       # the fresh guide is published
    assert _no_pictures(xmltv, 'P')                          # saved results still apply
    assert _pictures(xmltv, 'R') == _all_pictures('R')


@pytest.mark.parametrize('crash', ['download-raises-after-the-first-channel', 'check-raises-after-recording'])
def test_a_check_that_crashes_after_recording_still_saves_what_it_recorded(monkeypatch, tmp_path, crash):
    """A check that records results and then fails reaches syncAll's catch:
    the import keeps its fresh guide, the results recorded before the
    failure are saved (once) and applied, and channels not reached keep
    their pictures.
      - the picture download raises on R, after P (first in checking order)
        was recorded: only P is on file;
      - the check raises after recording every channel: all four are."""
    def _raises_on_r(url):
        if url.startswith(PICS + '/R/'):
            raise RuntimeError('picture host exploded')
        return _served(url)

    def _check_crashes_at_the_end(importer):
        real = importer._checkShowPictures

        def _check_then_crash(*a, **k):
            real(*a, **k)
            raise RuntimeError('check exploded after recording')
        monkeypatch.setattr(importer, '_checkShowPictures', _check_then_crash)

    if crash == 'download-raises-after-the-first-channel':
        served, patch = _raises_on_r, None
    else:
        served, patch = _served, _check_crashes_at_the_end
    xmltv = _xmltv()
    t0 = int(time.time())
    run = _sync(monkeypatch, _store(), xmltv, served=served, patch=patch)
    assert _messages(run, 'show pictures check failed')
    results = _results(tmp_path)
    expected = {'P@imp': True} if crash.startswith('download') else {
        'P@imp': True, 'R@imp': False, 'S@imp': True, 'X@imp': False}
    assert {cid: v['placeholder'] for cid, v in results.items()} == expected
    assert all(v['checked_at'] >= t0 for v in results.values())
    assert len(run.saves) == 1
    for ch in CHANNELS:
        assert len(_shows(xmltv, ch)) == len(TITLES), ch       # the fresh guide is published
    assert _no_pictures(xmltv, 'P')
    assert _pictures(xmltv, 'R') == _all_pictures('R')
    assert _pictures(xmltv, 'X') == _all_pictures('X')
    if crash.startswith('download'):
        assert _pictures(xmltv, 'S') == _all_pictures('S')  # never reached: not judged
    else:
        assert _no_pictures(xmltv, 'S')
    assert _pictures(xmltv, CUSTOM_ID) == [CUSTOM_ICON]


# ================================================================ syncAll: failures, time limit, Kodi closing (plan test 12)

@pytest.mark.parametrize('readable', [0, 2], ids=['none-readable', 'two-readable'])
@pytest.mark.parametrize('was_placeholder', [True, False], ids=['was-placeholder', 'was-real'])
def test_fewer_than_three_readable_pictures_keep_the_previous_result_and_its_date(monkeypatch, tmp_path,
                                                                                   readable, was_placeholder):
    """Fewer than MIN_SAMPLES (3) of P's 8 pictures can be read (the two that
    can are the same image): "can't tell". Every sample is tried, nothing is
    recorded -- the previous result and its date stay, so the channel is
    retried at the next sync -- and the previous result still applies."""
    previous = _verdict(was_placeholder, 2 * 24 * H, samples=PLACEHOLDER_READS)   # due for a re-check
    _seed_results(tmp_path, {'P@imp': previous})
    readable_links = set(_sampled('P', readable))

    def served(url):
        if url.startswith(PICS + '/P/'):
            return 'advert' if url in readable_links else None
        return _served(url)

    xmltv = _xmltv()
    run = _sync(monkeypatch, _store(), xmltv, served=served)
    assert _calls(run, 'P') == _sampled('P')                     # it tried every sample
    assert _results(tmp_path)['P@imp'] == previous               # unchanged, checked_at too
    if was_placeholder:
        assert _no_pictures(xmltv, 'P')
    else:
        assert _pictures(xmltv, 'P') == _all_pictures('P')
    assert _messages(run, 'too few readable pictures')


@pytest.mark.parametrize('status, content_type', [(200, 'text/html; charset=utf-8'), (404, 'image/png'),
                                                  (503, 'text/html')])
def test_an_outage_page_behind_every_link_is_never_a_placeholder(monkeypatch, tmp_path, status, content_type):
    """The picture host answers every link with the same error page. With the
    add-on's own download step, each answer is refused, so no channel is
    judged: every channel keeps its pictures and the saved result stays."""
    previous = _verdict(False, 2 * 24 * H, samples=2)
    _seed_results(tmp_path, {'R@imp': previous})
    asked = []

    def get(url, **kwargs):
        asked.append(url)
        return _Response(status=status, content_type=content_type, body=b'<html>Service Unavailable</html>')
    monkeypatch.setattr(requests, 'get', get)
    xmltv = _xmltv()
    run = _sync(monkeypatch, _store(), xmltv, real_download=True)
    # every sample of every channel was tried (S's one shared link once)
    assert len(asked) == 3 * picture_helpers.SAMPLES + 1
    assert _results(tmp_path) == {'R@imp': previous}
    assert run.saves == []
    for ch in CHANNELS:
        assert _pictures(xmltv, ch) == _all_pictures(ch), ch


@pytest.mark.parametrize('ch, failed, reads, placeholder, samples', [
    ('P', (1,), 6, True, 5),               # one advert lost: settled on the 5th advert read
    ('P', (0, 1, 2, 3, 4), 8, True, 3),    # only 3 read: all 3 alike is enough
    ('R', (0,), 6, False, 5),              # real: 5 different of the 7 that can still be read
], ids=['placeholder-one-failed', 'placeholder-only-three-read', 'real-one-failed'])
def test_failed_samples_are_skipped_the_pictures_read_decide(monkeypatch, tmp_path, ch, failed, reads,
                                                             placeholder, samples):
    """A sample that can't be read doesn't count. The rule then applies to
    the pictures that were read: placeholder when the most common one covers
    5 of them -- or all of them when fewer than 5 could be read -- real when
    it covers 2 fewer; 'samples' records how many were read."""
    lost = {_pic(ch, SAMPLED[i]) for i in failed}

    def served(url):
        return None if url in lost else _served(url)

    xmltv = _xmltv()
    run = _sync(monkeypatch, _store(), xmltv, served=served)
    assert _calls(run, ch) == _sampled(ch, reads)
    result = _results(tmp_path)[_cid(ch)]
    assert (result['placeholder'], result['samples']) == (placeholder, samples)
    if placeholder:
        assert _no_pictures(xmltv, ch)
    else:
        assert _pictures(xmltv, ch) == _all_pictures(ch)


def test_no_time_left_nothing_is_judged_and_nothing_is_written(monkeypatch, tmp_path):
    monkeypatch.setattr(picture_helpers, 'BUDGET_S', 0)
    xmltv = _xmltv()
    run = _sync(monkeypatch, _store(), xmltv)
    assert run.fp_calls == []
    assert not _results_path(tmp_path).exists()
    assert run.saves == []
    line = _messages(run, '[%s] show pictures' % (IMPORT_ID,))
    assert len(line) == 1 and '4 left' in line[0]
    for ch in CHANNELS:
        assert _pictures(xmltv, ch) == _all_pictures(ch), ch


def test_a_slow_picture_host_leaves_channels_for_the_next_sync(monkeypatch, tmp_path):
    """Each download takes 2 s against the 15 s budget. The budget is checked
    before a channel starts and a started channel finishes: P (5 downloads,
    10 s), then R, started at 10 s, finishes its 6 at 22 s -- past the
    budget. S and X aren't started: left, nothing recorded, their pictures
    untouched. The next sync checks S (its one shared link) and X, and
    nothing else again."""
    clock = _Clock(per_download=2.0)
    monkeypatch.setattr(imports_mod, 'time', clock)
    store, xmltv = _store(), _xmltv()
    first = _sync(monkeypatch, store, xmltv, clock=clock)
    assert first.fp_calls == _sampled('P', PLACEHOLDER_READS) + _sampled('R', REAL_READS)
    assert set(_results(tmp_path)) == {'P@imp', 'R@imp'}
    assert _messages(first, '2 left')
    assert _no_pictures(xmltv, 'P')
    assert _pictures(xmltv, 'X') == _all_pictures('X')
    second = _sync(monkeypatch, store, xmltv, clock=clock)
    assert second.fp_calls == [_pic('S', 0)] + _sampled('X', REAL_READS)
    assert set(_results(tmp_path)) == {'P@imp', 'R@imp', 'S@imp', 'X@imp'}
    assert _results(tmp_path)['X@imp']['placeholder'] is False


def test_kodi_closing_stops_the_check(monkeypatch, tmp_path):
    """Kodi asks to stop once 7 pictures are in. P's result was settled on
    its 5th (the downloads stop there), so P is recorded; R had 2 of its
    pictures read -- far from settled -- and is cut off: nothing recorded
    for it, and S and X are not reached."""
    run = _sync(monkeypatch, _store(), _xmltv(), stop_after=PLACEHOLDER_READS + 2)
    assert run.fp_calls == _sampled('P', PLACEHOLDER_READS) + _sampled('R', 2)
    assert set(_results(tmp_path)) == {'P@imp'}
    assert _messages(run, '3 left')


@pytest.mark.parametrize('cut', ['kodi-closes-after-3-different', 'hard-stop-in-the-buffer'])
def test_a_channel_cut_off_before_its_result_is_settled_is_not_recorded(monkeypatch, tmp_path, cut):
    """A channel cut off mid-check (Kodi closing or the 15 s budget) is
    recorded only when the samples not read can no longer change its result
    (picture_helpers.settled) -- never on what judge would say about the
    pictures read so far:
      - 3 different pictures read, then Kodi closes: judge would say real
        and bring the pictures back, but 5 of the 8 are the advert;
      - 7 read with 4 alike (4.5 s per download), then the hard stop at
        twice the budget: in the buffer, judge would keep "placeholder" and
        refresh its date.
    Either way B is left for a later sync: its previous result and date
    stay, and it stays stripped."""
    previous = _verdict(True, 2 * 24 * H, samples=PLACEHOLDER_READS)
    _seed_results(tmp_path, {'B@imp': previous})
    clock = stop_after = None
    if cut == 'kodi-closes-after-3-different':
        served = _serving(B=(3, 5, 6, 7, 9))                # samples 4-8: the advert
        stop_after, read = 3, 3
    else:
        served = _serving(B=(0, 2, 5, 7))                   # 4 of 8 alike: the buffer
        clock, read = _Clock(per_download=4.5), 7
        monkeypatch.setattr(imports_mod, 'time', clock)
    fps = [served(u) for u in _sampled('B', read)]
    assert picture_helpers.judge(fps, True) is (cut == 'hard-stop-in-the-buffer')       # what a partial judge says
    assert picture_helpers.settled(fps, picture_helpers.SAMPLES - read, True) is None
    xmltv = _xmltv()
    run = _sync(monkeypatch, _store(records=[_record('B'), _custom_record()]), xmltv,
                guide=_fresh(_guide(('B',))), served=served, clock=clock, stop_after=stop_after)
    assert run.fp_calls == _sampled('B', read)
    assert _results(tmp_path) == {'B@imp': previous}
    assert run.saves == []
    assert _messages(run, '1 left')
    assert _no_pictures(xmltv, 'B')


# ================================================================ syncAll: the results file (plan test 13)

def test_results_younger_than_a_day_are_not_checked_again(monkeypatch, tmp_path):
    _seed_results(tmp_path, {_cid(ch): _verdict(ch in 'PS', 23 * H) for ch in CHANNELS})
    run = _sync(monkeypatch, _store(), _xmltv())
    assert run.fp_calls == []
    assert run.saves == []


def test_results_a_day_old_are_checked_again(monkeypatch, tmp_path):
    old = _verdict(True, picture_helpers.MAX_AGE_S + 60)
    _seed_results(tmp_path, {'P@imp': old})
    t0 = int(time.time())
    run = _sync(monkeypatch, _store(), _xmltv())
    assert len(_calls(run, 'P')) == PLACEHOLDER_READS
    assert _results(tmp_path)['P@imp']['checked_at'] >= t0


def test_results_of_channels_no_longer_present_are_dropped(monkeypatch, tmp_path):
    _seed_results(tmp_path, {'Gone@imp': _verdict(True, H), 'P@imp': _verdict(True, H)})
    run = _sync(monkeypatch, _store(), _xmltv())
    results = _results(tmp_path)
    assert 'Gone@imp' not in results
    assert results['P@imp']['placeholder'] is True
    assert _messages(run, 'no longer present')


def test_an_empty_channel_list_never_wipes_the_results(monkeypatch, tmp_path):
    seeded = {'P@imp': _verdict(True, H)}
    _seed_results(tmp_path, seeded)
    run = _sync(monkeypatch, _store(records=[]), _xmltv())
    assert run.saves == []
    assert _results(tmp_path) == seeded


def test_a_damaged_results_file_reads_as_not_checked_yet(monkeypatch, tmp_path):
    _results_path(tmp_path).write_text('{"P@imp": {"placeholder": tr', encoding='utf-8')
    xmltv = _xmltv()
    _sync(monkeypatch, _store(), xmltv)
    assert _results(tmp_path)['P@imp']['placeholder'] is True     # checked as new, file rewritten whole
    assert _no_pictures(xmltv, 'P')


# ================================================================ _checkShowPictures on its own

def _progs(cid, links):
    """One show per link, differently titled, six hours apart."""
    out = []
    for i, link in enumerate(links):
        start = START + i * 6 * H
        out.append({'channel': cid, 'start': _ts(start), 'stop': _ts(start + H),
                    'title': [(TITLES[i], 'es')], 'icon': [{'src': link}]})
    return out


def _links(ch, n=4, same=False):
    return ['http://192.0.2.21/%s/%d.jpg' % (ch, 0 if same else i) for i in range(n)]


def _table(*placeholder_channels):
    """Picture links under /<ch>/ serve one image for the given channels and
    a different one per link otherwise."""
    def served(url):
        ch = url.split('/')[3]
        return 'same:' + ch if ch in placeholder_channels else 'own:' + url
    return served


def _check(monkeypatch, records, programmes, verdicts, served, cfg=None, clock=None, stop_after=None, now=None):
    calls = []
    service = _Service(calls, stop_after) if stop_after is not None else None
    importer = Imports(service=service)

    def _fingerprint(url):
        calls.append(url)
        if clock is not None:
            clock.now += clock.per_download
        return served(url)

    logs = []

    def _log(msg, level=None):
        logs.append(msg)

    monkeypatch.setattr(importer, '_pictureFingerprint', _fingerprint)
    monkeypatch.setattr(importer, 'log', _log)
    summary = importer._checkShowPictures(IMPORT_ID, _cfg() if cfg is None else cfg, programmes,
                                          {r['id']: r for r in records}, verdicts,
                                          int(time.time()) if now is None else now)
    return summary, calls, logs


def test_check_takes_the_imports_live_due_channels_with_enough_pictures(monkeypatch):
    records = [_record('A'), _record('H'),
               _record('B', enabled=False), _record('C', is_orphan=True), _record('D', unallocated=True),
               _record('E', iid='zzz'), _record('F'), _record('G')]
    programmes = []
    for rec in records:
        programmes += _progs(rec['id'], _links(rec['id'].split('@')[0], n=2 if rec['id'] == 'G@imp' else 4))
    verdicts = {'F@imp': _verdict(False, H, samples=2)}
    summary, calls, _logs = _check(monkeypatch, records, programmes, verdicts, _table('H'))
    assert {u.split('/')[3] for u in calls} == {'A', 'H'}
    assert summary['checked'] == 2 and summary['placeholder'] == ['H@imp'] and summary['left'] == 0
    assert set(verdicts) == {'A@imp', 'F@imp', 'H@imp'}


def test_check_summary(monkeypatch):
    """Four shows a channel (a small channel, fewer than 8 samples): real
    pictures are settled after 3 downloads; with fewer than 5 pictures to
    read, a placeholder needs all 4 alike."""
    records = [_record('A'), _record('H')]
    programmes = _progs('A@imp', _links('A')) + _progs('H@imp', _links('H'))
    now = int(time.time())
    verdicts = {}
    summary, calls, _logs = _check(monkeypatch, records, programmes, verdicts, _table('H'), now=now)
    assert set(summary) == {'checked', 'placeholder', 'left', 'changed', 'seconds'}
    assert summary['checked'] == 2 and summary['left'] == 0 and summary['changed'] is True
    assert summary['placeholder'] == ['H@imp']
    assert summary['seconds'] >= 0
    assert calls == _links('A', n=3) + _links('H')
    assert verdicts == {'A@imp': {'placeholder': False, 'checked_at': now, 'samples': 3},
                        'H@imp': {'placeholder': True, 'checked_at': now, 'samples': 4}}


def test_check_of_ten_show_channels(monkeypatch):
    """Ten shows a channel: 8 samples; real settles after 6, one image after 5."""
    records = [_record('A'), _record('H')]
    programmes = _progs('A@imp', _links('A', n=10)) + _progs('H@imp', _links('H', n=10))
    verdicts = {}
    summary, calls, _logs = _check(monkeypatch, records, programmes, verdicts, _table('H'))
    spread = [_links('A', n=10)[i] for i in SAMPLED]
    assert calls == spread[:REAL_READS] + [_links('H', n=10)[i] for i in SAMPLED[:PLACEHOLDER_READS]]
    assert {cid: (v['placeholder'], v['samples']) for cid, v in verdicts.items()} == {
        'A@imp': (False, REAL_READS), 'H@imp': (True, PLACEHOLDER_READS)}
    assert summary['checked'] == 2 and summary['placeholder'] == ['H@imp']


def test_check_order_never_checked_first_then_the_oldest(monkeypatch):
    records = [_record('H'), _record('I'), _record('J')]
    programmes = _progs('H@imp', _links('H')) + _progs('I@imp', _links('I')) + _progs('J@imp', _links('J'))
    verdicts = {'H@imp': _verdict(False, 3 * 24 * H), 'I@imp': _verdict(False, 2 * 24 * H)}
    _summary, calls, _logs = _check(monkeypatch, records, programmes, verdicts, _table())
    assert [u.split('/')[3] for u in calls] == ['J'] * 3 + ['H'] * 3 + ['I'] * 3     # 4 real pictures: 3 reads


def test_check_switched_off_does_nothing(monkeypatch):
    records = [_record('H')]
    verdicts = {}
    summary, calls, logs = _check(monkeypatch, records, _progs('H@imp', _links('H')), verdicts, _table('H'),
                                  cfg=_cfg(detect_placeholder_pictures=False))
    assert calls == [] and verdicts == {} and logs == []
    assert summary == {'checked': 0, 'placeholder': [], 'left': 0, 'changed': False, 'seconds': 0.0}


def test_check_with_no_time_left_leaves_every_candidate(monkeypatch):
    monkeypatch.setattr(picture_helpers, 'BUDGET_S', 0)
    records = [_record('A'), _record('H')]
    verdicts = {}
    summary, calls, logs = _check(monkeypatch, records, _progs('A@imp', _links('A')) + _progs('H@imp', _links('H')),
                                  verdicts, _table('H'))
    assert calls == [] and verdicts == {}
    assert summary['checked'] == 0 and summary['left'] == 2 and summary['changed'] is False
    assert any('2 left' in m for m in logs)


def _two_channels(monkeypatch, per_download):
    clock = _Clock(per_download=per_download)
    monkeypatch.setattr(imports_mod, 'time', clock)
    verdicts = {}
    summary, calls, _logs = _check(monkeypatch, [_record('A'), _record('H')],
                                   _progs('A@imp', _links('A', n=10)) + _progs('H@imp', _links('H', n=10)),
                                   verdicts, _table('H'), clock=clock)
    return summary, [u.split('/')[3] for u in calls], verdicts


def test_check_finishes_a_started_channel_past_the_budget(monkeypatch):
    """2 s per download, 15 s budget. A (ten shows, real) is settled after 6
    downloads (12 s); H starts at 12 s, inside the budget, and finishes its
    5 identical pictures at 22 s: both recorded."""
    summary, calls, verdicts = _two_channels(monkeypatch, 2.0)
    assert calls == ['A'] * REAL_READS + ['H'] * PLACEHOLDER_READS
    assert verdicts['A@imp']['placeholder'] is False and verdicts['H@imp']['placeholder'] is True
    assert summary['checked'] == 2 and summary['left'] == 0


def test_check_starts_no_channel_once_the_budget_is_spent(monkeypatch):
    """2.5 s per download: A's 6 downloads end exactly at 15 s, so H isn't
    started -- left for a later sync, nothing recorded."""
    summary, calls, verdicts = _two_channels(monkeypatch, 2.5)
    assert calls == ['A'] * REAL_READS
    assert set(verdicts) == {'A@imp'}
    assert summary['checked'] == 1 and summary['left'] == 1


def test_a_slow_host_still_gets_one_channel_judged_every_sync(monkeypatch):
    """3 s per download: only 5 downloads fit in the budget, one short of a
    real channel's 6. Checking the budget before every download would cut
    A off unjudged at every sync (and A, still never checked, would go first
    again next time); a started channel finishes, so A is judged."""
    summary, calls, verdicts = _two_channels(monkeypatch, 3.0)
    assert calls == ['A'] * REAL_READS
    assert verdicts['A@imp']['placeholder'] is False
    assert summary['checked'] == 1 and summary['left'] == 1


def test_check_hard_stop_cuts_a_channel_off(monkeypatch):
    """6 s per download: A's 5th download ends at 30 s, twice the budget, so
    its 6th isn't started. 5 different pictures don't settle a result: A is
    cut off and left with nothing recorded, H not reached."""
    summary, calls, verdicts = _two_channels(monkeypatch, 6.0)
    assert calls == ['A'] * 5
    assert verdicts == {}
    assert summary['checked'] == 0 and summary['left'] == 2


def test_check_takes_a_channel_with_exactly_min_samples_pictures(monkeypatch):
    """The smallest checkable channel: exactly MIN_SAMPLES (3) shows with
    pictures, all the same image -> placeholder, on 3 downloads."""
    verdicts = {}
    summary, calls, _logs = _check(monkeypatch, [_record('H')], _progs('H@imp', _links('H', n=3)),
                                   verdicts, _table('H'))
    assert calls == _links('H', n=3)
    assert (verdicts['H@imp']['placeholder'], verdicts['H@imp']['samples']) == (True, 3)
    assert summary['checked'] == 1


def test_too_few_pictured_shows_drop_a_due_placeholder_result(monkeypatch):
    """A due channel with fewer than 3 shows with pictures can't be judged
    from its guide (epg.best removed its advert on 2026-11-09 and left
    À Punt 1 pictured title, MTV 2). Its old placeholder result is dropped,
    so its pictures show again; a real result (which hides nothing) and a
    placeholder result not yet due stay. No downloads."""
    real = _verdict(False, 25 * H)
    young = _verdict(True, 23 * H)
    verdicts = {'H@imp': _verdict(True, 25 * H), 'K@imp': real, 'J@imp': young}
    records = [_record('H'), _record('K'), _record('J')]
    progs = (_progs('H@imp', _links('H', n=2)) + _progs('K@imp', _links('K', n=1))
             + _progs('J@imp', _links('J', n=2)))
    summary, calls, logs = _check(monkeypatch, records, progs, verdicts, _table('H'))
    assert calls == []
    assert verdicts == {'K@imp': real, 'J@imp': young}
    assert summary['changed'] is True and summary['checked'] == 0 and summary['left'] == 0
    assert [m for m in logs if 'placeholder result dropped' in m and 'H@imp' in m]


def test_a_placeholder_channel_left_with_too_few_pictures_shows_them_again(monkeypatch, tmp_path):
    """End to end: P was recorded placeholder more than a day ago; its fresh
    guide now has only 2 shows with pictures. The sync drops the result and
    publishes those 2 pictures."""
    _seed_results(tmp_path, {'P@imp': _verdict(True, picture_helpers.MAX_AGE_S + 60)})
    xmltv = _xmltv()
    run = _sync(monkeypatch, _store(), xmltv, guide=_fresh(_guide(shows={'P': 2})))
    assert _calls(run, 'P') == []
    assert _pictures(xmltv, 'P') == _all_pictures('P', n=2)
    assert 'P@imp' not in _results(tmp_path)


def test_check_stops_when_kodi_closes(monkeypatch):
    """Kodi asks to stop after 7 downloads: A (ten shows, real) was settled
    on its 6th and is recorded; H, cut off after 1, is left."""
    records = [_record('A'), _record('H')]
    verdicts = {}
    summary, calls, _logs = _check(monkeypatch, records,
                                   _progs('A@imp', _links('A', n=10)) + _progs('H@imp', _links('H', n=10)),
                                   verdicts, _table('H'), stop_after=REAL_READS + 1)
    assert [u.split('/')[3] for u in calls] == ['A'] * REAL_READS + ['H']
    assert set(verdicts) == {'A@imp'}
    assert summary['checked'] == 1 and summary['left'] == 1


def test_check_cant_tell_keeps_the_previous_result(monkeypatch):
    previous = _verdict(True, 2 * 24 * H)
    verdicts = {'H@imp': previous}
    summary, calls, logs = _check(monkeypatch, [_record('H')], _progs('H@imp', _links('H')), verdicts,
                                  lambda url: None)
    assert len(calls) == 4
    assert verdicts == {'H@imp': previous}
    assert summary['changed'] is False and summary['checked'] == 0 and summary['left'] == 0
    assert any('too few readable pictures' in m for m in logs)


def test_check_skips_channels_with_too_few_pictures(monkeypatch):
    verdicts = {}
    summary, calls, logs = _check(monkeypatch, [_record('H')], _progs('H@imp', _links('H', n=2)), verdicts,
                                  _table('H'))
    assert calls == [] and verdicts == {} and logs == []
    assert summary['checked'] == 0 and summary['left'] == 0


def test_check_reads_a_link_once_across_channels(monkeypatch):
    """K's shows use H's picture links: they are read once, for H."""
    links = _links('H')
    verdicts = {}
    _summary, calls, _logs = _check(monkeypatch, [_record('H'), _record('K')],
                                    _progs('H@imp', links) + _progs('K@imp', links), verdicts, _table('H'))
    assert calls == links
    assert verdicts['H@imp']['placeholder'] is True and verdicts['K@imp']['placeholder'] is True


def test_check_without_a_service_or_its_interrupt(monkeypatch):
    importer = Imports(service=object())                 # no _interrupt: never asked
    calls = []

    def _fingerprint(url):
        calls.append(url)
        return _table('H')(url)

    monkeypatch.setattr(importer, '_pictureFingerprint', _fingerprint)
    verdicts = {}
    summary = importer._checkShowPictures(IMPORT_ID, _cfg(), _progs('H@imp', _links('H')),
                                          {'H@imp': _record('H')}, verdicts, int(time.time()))
    assert summary['checked'] == 1 and verdicts['H@imp']['placeholder'] is True


# ================================================================ _pictureFingerprint (the download)

LINK = 'http://192.0.2.20/epg/P/0.jpg'
BIG = b''.join(hashlib.sha256(b'%d' % i).digest() for i in range(6400))    # 204,800 bytes
OLD_USER_AGENT = 'PseudoTV-imports/%s (Kodi addon; https://github.com/mmadalone/PseudoTV_Live)'


class _Response(object):
    """A streamed requests response: a context manager that serves `body`
    through iter_content and records what was read and whether it closed.
    `chunk` fixes the chunk size whatever is asked (servers and decoders don't
    always honour it); `fail_after` breaks the connection after that many
    bytes."""
    def __init__(self, status=200, content_type='image/png', body=b'', chunk=None, fail_after=None):
        self.status_code = status
        self.headers = requests.structures.CaseInsensitiveDict()
        if content_type is not None:
            self.headers['Content-Type'] = content_type
        self.body = body
        self.chunk = chunk
        self.fail_after = fail_after
        self.served = 0
        self.entered = False
        self.exited = False

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, *exc):
        self.exited = True
        return False

    def iter_content(self, chunk_size=1, decode_unicode=False):
        size = self.chunk or chunk_size
        for i in range(0, len(self.body), size):
            if self.fail_after is not None and self.served >= self.fail_after:
                raise requests.exceptions.ChunkedEncodingError('Connection broken: IncompleteRead')
            piece = self.body[i:i + size]
            self.served += len(piece)
            yield piece


def _get(monkeypatch, answer):
    """requests.get answering `answer` (a _Response, or an exception to
    raise); returns the recorded (url, kwargs) calls."""
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        if isinstance(answer, BaseException):
            raise answer
        return answer
    monkeypatch.setattr(requests, 'get', get)
    return calls


def _fingerprint(monkeypatch, answer, url=LINK):
    importer = Imports()
    logs = []

    def _log(msg, level=None):
        logs.append(msg)
    monkeypatch.setattr(importer, 'log', _log)
    calls = _get(monkeypatch, answer)
    return importer._pictureFingerprint(url), calls, logs


def test_fingerprint_is_the_sha256_of_exactly_the_first_fp_bytes(monkeypatch):
    response = _Response(body=BIG)
    fp, calls, _logs = _fingerprint(monkeypatch, response)
    assert fp == hashlib.sha256(BIG[:picture_helpers.FP_BYTES]).hexdigest()
    assert response.served < len(BIG)                       # stopped reading early
    assert response.entered and response.exited             # used as a context manager: closed
    assert len(calls) == 1


def test_fingerprint_cuts_at_fp_bytes_whatever_the_chunk_sizes(monkeypatch):
    response = _Response(body=BIG, chunk=10000)             # 7 chunks overshoot 64 KB
    fp, _calls, _logs = _fingerprint(monkeypatch, response)
    assert fp == hashlib.sha256(BIG[:picture_helpers.FP_BYTES]).hexdigest()
    assert response.served == 70000


def test_fingerprint_of_a_picture_smaller_than_fp_bytes(monkeypatch):
    body = BIG[:1000]
    fp, _calls, _logs = _fingerprint(monkeypatch, _Response(body=body))
    assert fp == hashlib.sha256(body).hexdigest()


@pytest.mark.parametrize('content_type', ['image/png', 'image/jpeg', 'IMAGE/WEBP', 'image/png; charset=binary'])
def test_fingerprint_takes_any_image_type(monkeypatch, content_type):
    fp, _calls, _logs = _fingerprint(monkeypatch, _Response(content_type=content_type, body=BIG[:500]))
    assert fp == hashlib.sha256(BIG[:500]).hexdigest()


@pytest.mark.parametrize('content_type', ['text/html; charset=utf-8', 'application/octet-stream', 'text/plain', '', None])
def test_fingerprint_refuses_what_is_not_an_image(monkeypatch, content_type):
    """An error page served for every link must never look like a placeholder."""
    response = _Response(content_type=content_type, body=BIG)
    fp, _calls, _logs = _fingerprint(monkeypatch, response)
    assert fp is None
    assert response.served == 0 and response.exited


@pytest.mark.parametrize('status', [204, 206, 304, 403, 404, 500, 503])
def test_fingerprint_needs_http_200(monkeypatch, status):
    response = _Response(status=status, body=BIG)
    fp, _calls, _logs = _fingerprint(monkeypatch, response)
    assert fp is None
    assert response.served == 0 and response.exited


def test_fingerprint_of_an_empty_body_is_none(monkeypatch):
    response = _Response(body=b'')
    fp, _calls, _logs = _fingerprint(monkeypatch, response)
    assert fp is None and response.exited


@pytest.mark.parametrize('error', [
    requests.exceptions.ConnectionError(
        "HTTPConnectionPool(host='192.0.2.20', port=80): Max retries exceeded with url: /epg/P/0.jpg?auth=PRIVATE-VALUE"),
    requests.exceptions.ConnectTimeout('connect timed out: http://192.0.2.20/epg/P/0.jpg?auth=PRIVATE-VALUE'),
    requests.exceptions.ReadTimeout('read timed out'),
    ValueError('bad link'),
], ids=['refused', 'connect-timeout', 'read-timeout', 'other'])
def test_fingerprint_failure_is_none_and_logs_only_the_host(monkeypatch, error):
    fp, _calls, logs = _fingerprint(monkeypatch, error, url=LINK + '?auth=PRIVATE-VALUE')
    assert fp is None
    assert len(logs) == 1
    assert '192.0.2.20' in logs[0] and error.__class__.__name__ in logs[0]
    assert 'PRIVATE-VALUE' not in logs[0] and '/epg/' not in logs[0]


def test_fingerprint_connection_broken_while_reading_is_none_and_closed(monkeypatch):
    response = _Response(body=BIG, fail_after=16384)
    fp, _calls, _logs = _fingerprint(monkeypatch, response)
    assert fp is None
    assert response.exited


def test_fingerprint_request_is_streamed_with_timeouts_and_the_addons_user_agent(monkeypatch):
    _fp, calls, _logs = _fingerprint(monkeypatch, _Response(body=BIG[:100]))
    url, kwargs = calls[0]
    assert url == LINK
    assert kwargs['headers'] == {'User-Agent': imports_mod._HTTP_USER_AGENT}
    assert kwargs['stream'] is True
    assert kwargs['timeout'] == (5, 10)


def test_the_user_agent_is_the_one_fetch_logo_always_sent(monkeypatch):
    """plan test 14: moving it into a constant changed nothing."""
    assert imports_mod._HTTP_USER_AGENT == OLD_USER_AGENT % (imports_mod.ADDON_VERSION,)
    seen = []

    class _LogoResponse(object):
        status_code = 200
        content = b'PNG'
        headers = {'Content-Type': 'image/png'}

    def get(url, headers=None, timeout=None, stream=None):
        seen.append(dict(headers or {}))
        return _LogoResponse()
    monkeypatch.setattr(requests, 'get', get)
    out = Imports()._fetchLogo('http://192.0.2.50/logos/tv3.png')
    assert out['status'] == 200
    assert seen == [{'User-Agent': OLD_USER_AGENT % (imports_mod.ADDON_VERSION,)}]


# ================================================================ the results file: load / save

def test_load_without_a_file_is_empty(tmp_path):
    assert not _results_path(tmp_path).exists()
    assert imports_mod.loadPictureVerdicts() == {}


@pytest.mark.parametrize('content', ['{"P@imp": {"placeholder": tr', '[]', '"text"', '', 'null'])
def test_load_of_a_damaged_file_is_empty(tmp_path, content):
    _results_path(tmp_path).write_text(content, encoding='utf-8')
    assert imports_mod.loadPictureVerdicts() == {}


def test_load_keeps_only_well_formed_entries(tmp_path):
    good = {'placeholder': True, 'checked_at': 1762600000, 'samples': 4}
    _seed_results(tmp_path, {'P@imp': good,
                             'R@imp': {'placeholder': 'yes', 'checked_at': 1762600000},
                             'S@imp': {'placeholder': False},
                             'X@imp': 'placeholder'})
    assert imports_mod.loadPictureVerdicts() == {'P@imp': good}


def test_load_never_raises(monkeypatch):
    def _boom(path):
        raise OSError('disk gone')
    monkeypatch.setattr(imports_mod.FileAccess, 'getJSON', _boom)
    assert imports_mod.loadPictureVerdicts() == {}


def test_save_then_load_round_trip(tmp_path):
    verdicts = {'P@imp': {'placeholder': True, 'checked_at': 1762600000, 'samples': 4},
                'R@imp': {'placeholder': False, 'checked_at': 1762600001, 'samples': 2}}
    assert Imports()._savePictureVerdicts(verdicts) is True
    assert _results(tmp_path) == verdicts
    assert imports_mod.loadPictureVerdicts() == verdicts


def test_save_follows_cache_loc_at_call_time(monkeypatch, tmp_path):
    other = tmp_path / 'elsewhere'
    other.mkdir()
    monkeypatch.setattr(imports_mod, 'CACHE_LOC', str(other))
    assert Imports()._savePictureVerdicts({'P@imp': _verdict(True, 0)}) is True
    assert (other / picture_helpers.VERDICTS_FILE).exists()
    assert not _results_path(tmp_path).exists()


@pytest.mark.parametrize('failure', ['returns-false', 'raises'])
def test_a_failed_save_is_false_and_keeps_the_old_file(monkeypatch, tmp_path, failure):
    seeded = {'P@imp': _verdict(True, H)}
    _seed_results(tmp_path, seeded)

    def _set(path, data):
        if failure == 'raises':
            raise OSError('read-only file system')
        return False
    monkeypatch.setattr(imports_mod.FileAccess, 'setJSON', _set)
    assert Imports()._savePictureVerdicts({'R@imp': _verdict(False, 0)}) is False
    assert _results(tmp_path) == seeded


# ================================================================ server.py (source scan, test_imports40 convention)

def _read(path):
    with open(path, 'r', encoding='utf-8') as fh:
        return fh.read()


def _block(src, start_re, end_re):
    m = re.search(start_re, src)
    assert m, 'start not found: %s' % (start_re,)
    end = re.search(end_re, src[m.end():])
    return src[m.start(): m.end() + (end.start() if end else len(src))]


def _edit_json_block():
    return _block(_read(SERVER_PY),
                  r"elif self\.path\.split\('\?', 1\)\[0\]\.lower\(\) == '/imports/channel/edit\.json':",
                  r"\n\s+elif self\.path\.split\('\?', 1\)\[0\]\.lower\(\) ==")


def _imports_json_block():
    return _block(_read(SERVER_PY),
                  r"elif bare_path\.lower\(\) == '/imports\.json':",
                  r"\n\s+elif bare_path\.lower\(\) ==")


def test_server_imports_picture_helpers_at_module_level():
    tree = ast.parse(_read(SERVER_PY))
    assert any(isinstance(n, ast.Import) and any(a.name == 'picture_helpers' for a in n.names) for n in tree.body)


def test_edit_json_allows_show_pictures():
    m = re.search(r'ALLOWED_FIELDS\s*=\s*\{([^}]*)\}', _edit_json_block())
    assert m
    assert {f.strip().strip("'\"") for f in m.group(1).split(',')} == {'number', 'enabled', 'name', 'logo', 'show_pictures'}


def test_edit_json_validates_show_pictures_before_loading_channels():
    block = _edit_json_block()
    check = block.index("fields['show_pictures'] = picture_helpers.normalize_mode(fields['show_pictures'])")
    load = block.index('Channels(writable=True)')
    assert check < load
    between = block[check:load]
    assert 'except ValueError:' in between
    assert re.search(r'self\.send_error\(400, "show_pictures must be auto, on or off"\)\s*return', between)


def test_edit_json_auto_removes_the_field_and_its_override():
    assert re.search(r"if v == 'auto':\s*c\.pop\('show_pictures', None\)\s*overrides\.discard\('show_pictures'\)",
                     _edit_json_block())


def test_edit_json_show_and_hide_are_stored_and_marked():
    assert re.search(r"else:\s*c\['show_pictures'\] = v\s*overrides\.add\('show_pictures'\)\s*continue",
                     _edit_json_block())


def test_imports_json_sends_the_setting_and_the_check_result():
    block = _imports_json_block()
    assert re.search(r"'show_pictures'\s*:\s*picture_helpers\.record_mode\(c\)", block)
    assert re.search(r"'pictures_check'\s*:\s*verdicts\.get\(c\.get\('id'\)\)", block)


def test_imports_json_reads_the_results_once_per_request():
    block = _imports_json_block()
    assert block.count('loadPictureVerdicts()') == 1
    assert block.index('loadPictureVerdicts()') < block.index('for c in ch_list:')
    assert re.search(r'except Exception as _e:.*?verdicts = \{\}', block, flags=re.DOTALL)


# ================================================================ server.py endpoints, run without a socket

@pytest.fixture
def server_env(monkeypatch):
    """server.MyHandler with fake Channels and PROPERTIES; requests come from
    the loopback address, which needs no credentials."""
    import server
    state = {'channels': [], 'imports': [], 'built': 0, 'saved': [], 'kicks': []}

    class _Channels(object):
        def __init__(self, writable=False):
            state['built'] += 1

        def getChannels(self):
            return copy.deepcopy(state['channels'])

        def getImports(self):
            return copy.deepcopy(state['imports'])

        def setChannels(self, channels=None, modified_ids=None, deleted_ids=None):
            state['saved'].append(copy.deepcopy(channels))
            return True

    class _Properties(object):
        def setEXTProperty(self, key, value):
            state['kicks'].append((key, value))

        def setPropTimer(self, key):
            state['kicks'].append((key, None))

    monkeypatch.setattr(server, 'Channels', _Channels)
    monkeypatch.setattr(server, 'PROPERTIES', _Properties())
    # The stub's getMYUUID opens a self-closing dialog whose timer thread
    # keeps the test process from exiting.
    monkeypatch.setattr(server.SETTINGS, 'getMYUUID', lambda: 'uuid-test')
    return server, state


def _handler(server, path, body=None):
    h = server.MyHandler.__new__(server.MyHandler)
    raw = json.dumps(body).encode('utf-8') if body is not None else b''
    h.path = path
    h.headers = {'content-length': str(len(raw))}
    h.rfile = io.BytesIO(raw)
    h.wfile = io.BytesIO()
    h.client_address = ('127.0.0.1', 50000)
    h.statuses, h.errors, h.logs = [], [], []
    h.send_response = lambda code, msg=None: h.statuses.append(code)
    h.send_header = lambda *a, **k: None
    h.end_headers = lambda: None
    h.send_error = lambda code, msg=None: h.errors.append((code, msg))
    h.log = lambda msg, level=None: h.logs.append((msg, level))
    return h


def _edit(server, fields, channel_id='P@imp', **extra):
    body = {'uuid': 'x', 'channel_id': channel_id, 'fields': fields}
    body.update(extra)
    h = _handler(server, '/imports/channel/edit.json', body)
    h.do_POST()
    return h


@pytest.mark.parametrize('sent, stored', [('off', 'off'), ('on', 'on'), (' OFF ', 'off'), ('On', 'on')])
def test_endpoint_edit_stores_show_and_hide_with_an_override(server_env, sent, stored):
    server, state = server_env
    state['channels'] = [_record('P', operator_overrides=['logo'])]
    h = _edit(server, {'show_pictures': sent})
    assert h.errors == [] and h.statuses == [200]
    saved = state['saved'][-1][0]
    assert saved['show_pictures'] == stored
    assert saved['operator_overrides'] == ['logo', 'show_pictures']
    assert state['kicks'] == [('chkImports.kick', 'all')]


@pytest.mark.parametrize('sent', ['auto', ' AUTO ', '', None])
def test_endpoint_edit_auto_removes_the_field_and_its_override(server_env, sent):
    server, state = server_env
    state['channels'] = [_record('P', show_pictures='off', operator_overrides=['logo', 'show_pictures'])]
    h = _edit(server, {'show_pictures': sent})
    assert h.errors == [] and h.statuses == [200]
    saved = state['saved'][-1][0]
    assert 'show_pictures' not in saved
    assert saved['operator_overrides'] == ['logo']


@pytest.mark.parametrize('sent', ['hide', 'show', 'yes', 1, 0, True, [], {'mode': 'off'}])
def test_endpoint_edit_refuses_a_bad_value_before_loading_anything(server_env, sent):
    server, state = server_env
    state['channels'] = [_record('P', show_pictures='off', operator_overrides=['show_pictures'])]
    h = _edit(server, {'name': 'Renamed', 'show_pictures': sent})
    assert h.errors == [(400, 'show_pictures must be auto, on or off')]
    assert h.statuses == []
    assert state['built'] == 0 and state['saved'] == [] and state['kicks'] == []


def test_endpoint_imports_json_sends_setting_and_result(server_env, tmp_path):
    server, state = server_env
    result = {'placeholder': True, 'checked_at': 1762600000, 'samples': 4}
    _seed_results(tmp_path, {'P@imp': result, 'R@imp': {'placeholder': 'yes', 'checked_at': 1}})
    state['channels'] = [_record('P'), _record('R', show_pictures='off'), _record('X', show_pictures='bogus'),
                         _custom_record()]
    h = _handler(server, '/imports.json')
    h.do_GET()
    assert h.errors == [] and h.statuses == [200]
    channels = {c['id']: c for c in json.loads(h.wfile.getvalue().decode('utf-8'))['channels']}
    assert channels['P@imp']['show_pictures'] == 'auto' and channels['P@imp']['pictures_check'] == result
    assert channels['R@imp']['show_pictures'] == 'off' and channels['R@imp']['pictures_check'] is None
    assert channels['X@imp']['show_pictures'] == 'auto'                   # a damaged value reads as Auto
    assert channels[CUSTOM_ID]['pictures_check'] is None


def test_the_dashboard_reads_what_the_sync_wrote(server_env, monkeypatch, tmp_path):
    """One format, one writer (syncAll) and one reader (GET /imports.json)."""
    server, state = server_env
    store = _store(modes={'X': 'off'})
    _sync(monkeypatch, store, _xmltv())
    written = _results(tmp_path)
    state['channels'] = store.channels
    h = _handler(server, '/imports.json')
    h.do_GET()
    channels = {c['id']: c for c in json.loads(h.wfile.getvalue().decode('utf-8'))['channels']}
    for ch in CHANNELS:
        assert channels[_cid(ch)]['pictures_check'] == written.get(_cid(ch)), ch
    assert channels['P@imp']['pictures_check']['placeholder'] is True
    assert channels['X@imp']['show_pictures'] == 'off' and channels['P@imp']['show_pictures'] == 'auto'


# ================================================================ manager.html (source scan)

def _html():
    return _read(HTML_PATH)


def _js_function(name):
    """manager.html's top-level function `name`, from its `function` line to
    its closing brace (the page's script is indented four spaces)."""
    src = _html()
    m = re.search(r'\n    (?:async )?function %s\(' % (re.escape(name),), src)
    assert m, 'function %s not found in manager.html' % (name,)
    end = src.find('\n    }\n', m.end())
    assert end != -1
    return src[m.start():end + 6]


def test_html_row_has_the_pictures_selector_after_the_logo_button():
    row = _js_function('renderImportRow')
    assert row.index('data-action="edit-imp-logo"') < row.index('${picturesControl(c, imp)}')
    control = _js_function('picturesControl')
    assert '<select class="pic-select" data-action="setpictures" data-cid="${escapeHtml(c.id)}" data-original="${escapeHtml(mode)}"' in control
    for value, label in (('auto', 'Pictures: Auto'), ('on', 'Pictures: Show'), ('off', 'Pictures: Hide')):
        assert "opt('%s', '%s')" % (value, label) in control


def test_html_badge_only_when_the_check_hides_an_auto_channel():
    control = _js_function('picturesControl')
    assert re.search(r"if \(mode !== 'auto' \|\| imp\.detect_placeholder_pictures === false \|\| !check "
                     r"\|\| check\.placeholder !== true\) return select;", control)
    assert 'placeholder → logo' in control


def test_html_selector_stages_into_the_imports_edit_pool():
    stage = _js_function('_stagePictures')
    assert 'PENDING_IMP_EDITS[cid] = Object.assign(existing, {show_pictures: sel.value});' in stage
    assert 'if (sel.value === sel.dataset.original)' in stage
    assert 'delete existing.show_pictures;' in stage
    change = _js_function('_onPicturesChange')
    assert '_stagePictures(sel);' in change and 'updateGlobalSave();' in change
    assert "$('imports-list')?.addEventListener('change', _onPicturesChange);" in _html()


def test_html_escape_puts_the_saved_value_back():
    keydown = _js_function('_onChannelKeydown')
    assert re.search(r"if \(e\.key === 'Escape'\) \{\s*sel\.value = sel\.dataset\.original;\s*_stagePictures\(sel\);",
                     keydown)


def test_html_rerender_restores_a_staged_choice():
    render = _js_function('renderImports')
    assert "if ('show_pictures' in edits) {" in render
    assert 'select[data-action="setpictures"]' in render
    assert 'sel.value = edits.show_pictures;' in render


def test_html_global_save_sends_the_pool_then_one_refresh_all():
    save = _js_function('globalSave')
    assert 'for (const cid of Object.keys(PENDING_IMP_EDITS)) {' in save
    assert '_editImport(cid, PENDING_IMP_EDITS[cid])' in save
    assert re.search(r'if \(.*Object\.keys\(PENDING_IMP_EDITS\)\.length\) \{\s*try \{\s*await fetch\(`http://\$\{REMOTE_HOST\}/imports/refresh\.json`',
                     save)


def test_html_edit_import_window_has_the_tick_box():
    src = _html()
    modal = src[src.index('id="import-modal-overlay"'):src.index('id="import-save-btn"')]
    assert '<input type="checkbox" id="f-detect-pictures" checked />' in modal


def test_html_open_ticks_the_box_unless_the_import_says_false():
    opened = _js_function('openImportModal')
    assert "$('f-detect-pictures').checked = imp ? imp.detect_placeholder_pictures !== false : true;" in opened
    assert "$('f-detect-pictures').dataset.original = imp ? String($('f-detect-pictures').checked) : '';" in opened


def test_html_form_sends_the_switch():
    assert "detect_placeholder_pictures: $('f-detect-pictures').checked," in _js_function('gatherImportForm')


def test_html_save_refreshes_the_import_when_the_switch_changed():
    save = _js_function('saveImport')
    assert re.search(r"const detectChanged = IMP_EDITING_INDEX >= 0 && detectOpened !== ''\s*"
                     r"&& detectOpened !== String\(item\.detect_placeholder_pictures\);", save)
    assert 'const saved = await postImports(list);' in save
    assert 'if (saved && detectChanged) await refreshOne(item.id);' in save


def _js_code(src):
    """`src` without its whole-line // comments, so a scan never matches the
    comments' wording."""
    return re.sub(r'(?m)^[ \t]*//.*$', '', src)


def test_html_save_import_decides_from_the_opened_value_then_saves_then_refreshes():
    """imports.66: saveImport's order. Whether the switch changed is worked
    out from the value captured when the window opened (data-original, set
    once, in openImportModal, after the box is ticked) -- before the save,
    which closes the window and reloads the list. Then the save; and
    refreshOne(item.id) only after it, only when it went through (postImports
    answers true), and only for an existing import whose switch changed."""
    save = _js_code(_js_function('saveImport'))
    steps = ['const item = gatherImportForm();',
             "const detectOpened = $('f-detect-pictures').dataset.original;",
             'const detectChanged = ',
             'const saved = await postImports(list);',
             'if (saved && detectChanged) await refreshOne(item.id);']
    at = [save.index(step) for step in steps]
    assert at == sorted(at), 'saveImport steps out of order'
    assert save.count('postImports(') == 1 and save.count('refreshOne(') == 1
    changed = save[at[2]:save.index(';', at[2]) + 1]
    assert re.fullmatch(r"const detectChanged = IMP_EDITING_INDEX >= 0 && detectOpened !== ''\s*"
                        r"&& detectOpened !== String\(item\.detect_placeholder_pictures\);", changed)
    # the box itself is read once, by gatherImportForm (item) -- never compared live
    assert "$('f-detect-pictures').checked" not in save
    # the captured value: written once in the page, in openImportModal, after the tick
    src = _js_code(_html())
    assert src.count("$('f-detect-pictures').dataset.original =") == 1
    opened = _js_code(_js_function('openImportModal'))
    assert (opened.index("$('f-detect-pictures').checked = ")
            < opened.index("$('f-detect-pictures').dataset.original = "))


def test_html_post_imports_answers_true_or_false():
    post = _js_function('postImports')
    assert post.count('return false;') == 2           # HTTP error, network error
    assert post.count('return true;') == 1
    assert not re.search(r'return;', post)


def test_html_help_line_mentions_pictures():
    assert 'Pictures (Auto / Show / Hide) sets whether the TV guide shows the provider\'s show pictures.' in _js_function('renderImportRow')
