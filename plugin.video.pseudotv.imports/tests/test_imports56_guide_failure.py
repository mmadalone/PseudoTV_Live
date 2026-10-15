"""imports.56 — a failed guide download no longer wipes an import's guide.

Pre-imports.56, when an import's channel list loaded but its guide did not
(download error, unusable body, 304 with an unreadable cache), syncOne left
epg_pairs=[] while epg_id_map was populated, and syncAll's XML branch pruned
the import's whole slice of pseudotv.xml and appended nothing. The
refresh_interval_min gate then kept it gone for up to 10 h. Live case:
Movistar+ (local M3U from the Movistar+ addon, guide from GitHub) vanished
from the guide whenever the internet dropped during its sync.

Covered here:
  - syncOne falls back to the cached guide, flags keep-previous when there
    is none, never overwrites a good cache with an unusable body, and tracks
    epg_fail_count / last_error;
  - syncAll's XML branch keeps the previous slice on keep-previous and on
    zero matches, still prunes disabled channels and removed guide sources;
  - the gate retries guide failures on the failure-retry curve;
  - the guide self-heal forces a sync when an import's guide is missing,
    with its guards and throttle;
  - RENDER_ONLY_KICK runs a normal gated cycle.
"""
import os
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
LIB  = os.path.join(os.path.dirname(HERE), 'resources', 'lib')
if LIB not in sys.path:
    sys.path.insert(0, LIB)

import imports as imports_mod
from imports import Imports, RENDER_ONLY_KICK, FAIL_COUNT_CAP


IMPORT_ID = 'imp'
M3U_URL   = 'https://example.invalid/list.m3u'
EPG_URL   = 'https://example.invalid/guide.xml'

GOOD_GUIDE = b'''<?xml version="1.0" encoding="UTF-8"?>
<tv>
<channel id="c1"><display-name>Chan 1</display-name></channel>
<channel id="c2"><display-name>Chan 2</display-name></channel>
<programme start="20990101000000 +0000" stop="20990101010000 +0000" channel="c1"><title>New One</title></programme>
<programme start="20990101000000 +0000" stop="20990101010000 +0000" channel="c2"><title>New Two</title></programme>
</tv>'''

# Parses fine, but none of its ids or names belong to the import's channels.
UNMATCHED_GUIDE = b'''<?xml version="1.0" encoding="UTF-8"?>
<tv>
<channel id="zz"><display-name>Somebody Else</display-name></channel>
<programme start="20990101000000 +0000" stop="20990101010000 +0000" channel="zz"><title>Elsewhere</title></programme>
</tv>'''


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Per-test cache dir, inert imports.43 disk-presence check, fresh
    self-heal throttle."""
    monkeypatch.setattr(imports_mod, 'CACHE_LOC', str(tmp_path))
    monkeypatch.setattr(imports_mod, '_isDiskMissing', lambda iid, scope: False)
    imports_mod._GUIDE_SELFHEAL_LAST.clear()
    yield
    imports_mod._GUIDE_SELFHEAL_LAST.clear()


def _patch_settings(monkeypatch, curve_code=1):
    """Pin the retry curve and neutralize jitter (mirrors test_imports28)."""
    real_getInt = imports_mod.SETTINGS.getSettingInt
    def fake_getInt(key, default=0):
        if key == 'Imports_Failure_Retry_Curve':   return curve_code
        if key == 'Imports_Failure_Abandon_Hours': return 0
        try:    return real_getInt(key, default)
        except TypeError: return real_getInt(key)
    monkeypatch.setattr(imports_mod.SETTINGS, 'getSettingInt', fake_getInt)
    monkeypatch.setattr(imports_mod.random, 'randint', lambda a, b: 0)


# ---------------------------------------------------------------- fixtures

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
    def addStation(self, sitem): self.added.append(sitem)
    def delStation(self, sitem): pass


class _MockXMLTV:
    def __init__(self, channels=None, programmes=None):
        self.XMLTVDATA = {'channels': list(channels or []),
                          'programmes': list(programmes or []),
                          'recordings': []}


def _channel(n, enabled=True):
    cid = 'c%d@%s' % (n, IMPORT_ID)
    url = 'plugin://plugin.video.example/?id=c%d' % n
    return {'id': cid, 'epg_id': 'c%d' % n, 'name': 'Chan %d' % n,
            'import_source': IMPORT_ID, 'type': 'import', 'enabled': enabled,
            'number': n, 'assigned_number': n, 'is_orphan': False,
            'unallocated': False, 'source_url': url, 'url': url,
            'group': ['IMP'], 'logo': ''}


def _import_cfg(**over):
    cfg = {'id': IMPORT_ID, 'name': 'IMP', 'enabled': True,
           'm3u_url': M3U_URL, 'epg_url': EPG_URL, 'start_num': 1,
           'refresh_interval_min': 600,
           'last_sync_at': int(time.time()) - 11 * 3600,   # gate open
           'tombstones': [], 'respect_source_numbers': True,
           'rediscover_deleted': False}
    cfg.update(over)
    return cfg


def _fetch_stub(epg_response):
    """M3U answers 304 (channels preserved from existing records); the
    guide answers `epg_response`."""
    calls = []
    def _fn(url, etag=None, last_modified=None, **kw):
        calls.append(url)
        base = {'status': 0, 'content': None, 'etag': None,
                'last_modified': None, 'error': None}
        if url == M3U_URL:
            base['status'] = 304
            return base
        if url == EPG_URL:
            base.update(epg_response)
            return base
        raise AssertionError('unexpected fetch: %r' % (url,))
    _fn.calls = calls
    return _fn


DOWN    = {'status': 0, 'error': 'Temporary failure in name resolution'}
FRESH   = {'status': 200, 'content': GOOD_GUIDE, 'etag': '"fresh"'}
NOT_MOD = {'status': 304}


def _prior_xmltv():
    """Guide data as loaded from disk: the import's old slice plus another
    import's entries that must never be touched."""
    return _MockXMLTV(
        channels=[{'id': 'c1@imp', 'display-name': [('Chan 1', '')]},
                  {'id': 'c2@imp', 'display-name': [('Chan 2', '')]},
                  {'id': 'x@other', 'display-name': [('Other', '')]}],
        programmes=[{'channel': 'c1@imp', 'start': '20990101000000 +0000',
                     'stop': '20990101010000 +0000', 'title': [('Old One', '')]},
                    {'channel': 'c2@imp', 'start': '20990101000000 +0000',
                     'stop': '20990101010000 +0000', 'title': [('Old Two', '')]},
                    {'channel': 'x@other', 'start': '20990101000000 +0000',
                     'stop': '20990101010000 +0000', 'title': [('Other', '')]}])


def _titles(xmltv, channel_id):
    return sorted(p['title'][0][0] for p in xmltv.XMLTVDATA['programmes']
                  if p.get('channel') == channel_id)


def _channel_ids(xmltv):
    return sorted(c['id'] for c in xmltv.XMLTVDATA['channels'])


def _seed_cache(tmp_path, content=GOOD_GUIDE):
    (tmp_path / ('epg_%s.xml' % IMPORT_ID)).write_bytes(content)


def _run_sync(monkeypatch, *, epg_response, cfg=None, channels=None,
              xmltv=None, force_scope=None):
    imp_cfg  = cfg or _import_cfg()
    chans    = channels if channels is not None else [_channel(1), _channel(2)]
    xmltv    = xmltv if xmltv is not None else _prior_xmltv()
    importer = Imports(channels=_MockChannels([imp_cfg], chans), m3u=_MockM3U(), xmltv=xmltv)
    stub     = _fetch_stub(epg_response)
    monkeypatch.setattr(importer, 'fetchSource', stub)
    results  = importer.syncAll(force_scope=force_scope)
    return results, xmltv, importer, stub


# ================================================================ syncOne

def test_failed_download_falls_back_to_cached_guide(monkeypatch, tmp_path):
    _seed_cache(tmp_path)
    imp = Imports()
    monkeypatch.setattr(imp, 'fetchSource', _fetch_stub(DOWN))
    res = imp.syncOne(_import_cfg(), [_channel(1), _channel(2)])
    assert res['status'] == 'unchanged'           # channel list was fine
    assert res['epg_pairs'] == list(imp.parseXMLTV(GOOD_GUIDE))
    assert not res.get('epg_keep_previous')
    assert res['epg_error'].startswith('guide download failed')
    assert res['updated_cfg']['epg_fail_count'] == 1
    assert 'using the cached guide' in res['updated_cfg']['last_error']


def test_failed_download_without_cache_keeps_previous(monkeypatch):
    imp = Imports()
    monkeypatch.setattr(imp, 'fetchSource', _fetch_stub(DOWN))
    res = imp.syncOne(_import_cfg(), [_channel(1), _channel(2)])
    assert res['epg_pairs'] == []
    assert res['epg_keep_previous'] is True
    assert 'kept the previous guide' in res['updated_cfg']['last_error']


def test_unusable_200_body_never_overwrites_good_cache(monkeypatch, tmp_path):
    _seed_cache(tmp_path)
    imp = Imports()
    monkeypatch.setattr(imp, 'fetchSource', _fetch_stub(
        {'status': 200, 'content': b'<html>Service Unavailable</html>', 'etag': '"bad"'}))
    res = imp.syncOne(_import_cfg(epg_etag='"good"'), [_channel(1), _channel(2)])
    assert (tmp_path / 'epg_imp.xml').read_bytes() == GOOD_GUIDE
    assert res['updated_cfg']['epg_etag'] == '"good"'   # validators not advanced
    assert res['epg_pairs'] == list(imp.parseXMLTV(GOOD_GUIDE))
    assert res['epg_error'] == 'guide download contained no guide data'


def test_304_with_unreadable_cache_keeps_previous(monkeypatch, tmp_path):
    (tmp_path / 'epg_imp.xml').write_bytes(b'<tv><chan')   # truncated
    imp = Imports()
    monkeypatch.setattr(imp, 'fetchSource', _fetch_stub(NOT_MOD))
    res = imp.syncOne(_import_cfg(epg_etag='"e"', epg_last_modified='x'),
                      [_channel(1), _channel(2)])
    assert res['epg_keep_previous'] is True
    assert res['updated_cfg']['epg_etag'] is None        # pre-.56 behaviour kept
    assert res['updated_cfg']['epg_last_modified'] is None


def test_good_guide_resets_epg_fail_count(monkeypatch):
    imp = Imports()
    monkeypatch.setattr(imp, 'fetchSource', _fetch_stub(FRESH))
    res = imp.syncOne(_import_cfg(epg_fail_count=3), [_channel(1), _channel(2)])
    assert res['updated_cfg']['epg_fail_count'] == 0
    assert res['updated_cfg']['last_error'] is None
    assert not res.get('epg_error')


def test_epg_fail_count_is_capped(monkeypatch):
    imp = Imports()
    monkeypatch.setattr(imp, 'fetchSource', _fetch_stub(DOWN))
    res = imp.syncOne(_import_cfg(epg_fail_count=FAIL_COUNT_CAP), [_channel(1)])
    assert res['updated_cfg']['epg_fail_count'] == FAIL_COUNT_CAP


def test_import_without_failure_history_gets_no_new_field(monkeypatch):
    """No epg_fail_count key is added on a clean sync of an import that never
    had one — keeps channels.json diffs quiet."""
    imp = Imports()
    monkeypatch.setattr(imp, 'fetchSource', _fetch_stub(FRESH))
    res = imp.syncOne(_import_cfg(), [_channel(1)])
    assert 'epg_fail_count' not in res['updated_cfg']


# ================================================================ syncAll XML branch

def test_failed_download_keeps_previous_guide_in_file(monkeypatch):
    _, xmltv, _, _ = _run_sync(monkeypatch, epg_response=DOWN)
    assert _channel_ids(xmltv) == ['c1@imp', 'c2@imp', 'x@other']
    assert _titles(xmltv, 'c1@imp') == ['Old One']
    assert _titles(xmltv, 'c2@imp') == ['Old Two']
    assert _titles(xmltv, 'x@other') == ['Other']


def test_failed_download_with_cache_rebuilds_from_cache(monkeypatch, tmp_path):
    _seed_cache(tmp_path)
    _, xmltv, _, _ = _run_sync(monkeypatch, epg_response=DOWN)
    assert _titles(xmltv, 'c1@imp') == ['New One']
    assert _titles(xmltv, 'c2@imp') == ['New Two']
    assert _titles(xmltv, 'x@other') == ['Other']


def test_keep_previous_still_drops_channels_no_longer_live(monkeypatch):
    _, xmltv, _, _ = _run_sync(monkeypatch, epg_response=DOWN,
                               channels=[_channel(1), _channel(2, enabled=False)])
    assert _channel_ids(xmltv) == ['c1@imp', 'x@other']
    assert _titles(xmltv, 'c1@imp') == ['Old One']
    assert _titles(xmltv, 'c2@imp') == []


def test_fresh_guide_replaces_previous_slice(monkeypatch):
    _, xmltv, _, _ = _run_sync(monkeypatch, epg_response=FRESH)
    assert _titles(xmltv, 'c1@imp') == ['New One']
    assert _titles(xmltv, 'c2@imp') == ['New Two']
    assert _titles(xmltv, 'x@other') == ['Other']
    assert _channel_ids(xmltv) == ['c1@imp', 'c2@imp', 'x@other']


def test_unmatched_guide_keeps_previous_slice(monkeypatch):
    _, xmltv, _, _ = _run_sync(monkeypatch, epg_response={
        'status': 200, 'content': UNMATCHED_GUIDE, 'etag': '"u"'})
    assert _titles(xmltv, 'c1@imp') == ['Old One']
    assert _titles(xmltv, 'c2@imp') == ['Old Two']


def test_all_channels_disabled_still_prunes(monkeypatch):
    _, xmltv, _, _ = _run_sync(monkeypatch, epg_response=FRESH,
                               channels=[_channel(1, enabled=False),
                                         _channel(2, enabled=False)])
    assert _channel_ids(xmltv) == ['x@other']
    assert _titles(xmltv, 'c1@imp') == [] and _titles(xmltv, 'c2@imp') == []


def test_removed_guide_source_still_prunes(monkeypatch):
    _, xmltv, _, _ = _run_sync(monkeypatch, epg_response=FRESH,
                               cfg=_import_cfg(epg_url=''))
    assert _channel_ids(xmltv) == ['x@other']


# ================================================================ gate: guide retry

def test_guide_failure_retries_on_failure_curve(monkeypatch):
    _patch_settings(monkeypatch, curve_code=1)   # aggressive: 60 s first retry
    cfg = _import_cfg(last_sync_at=int(time.time()) - 30 * 60, epg_fail_count=1)
    results, _, _, stub = _run_sync(monkeypatch, epg_response=DOWN, cfg=cfg)
    assert results[IMPORT_ID]['status'] != 'skipped'
    assert EPG_URL in stub.calls
    assert results[IMPORT_ID]['updated_cfg']['epg_fail_count'] == 2


def test_without_guide_failure_normal_interval_applies(monkeypatch):
    _patch_settings(monkeypatch, curve_code=1)
    cfg = _import_cfg(last_sync_at=int(time.time()) - 30 * 60)
    results, _, _, stub = _run_sync(monkeypatch, epg_response=DOWN, cfg=cfg)
    assert results[IMPORT_ID]['status'] == 'skipped'
    assert stub.calls == []


def test_disabled_retry_curve_keeps_normal_interval(monkeypatch):
    _patch_settings(monkeypatch, curve_code=0)   # 'disabled'
    cfg = _import_cfg(last_sync_at=int(time.time()) - 30 * 60, epg_fail_count=1)
    results, _, _, _ = _run_sync(monkeypatch, epg_response=DOWN, cfg=cfg)
    assert results[IMPORT_ID]['status'] == 'skipped'


# ================================================================ self-heal

def _recent_cfg(**over):
    return _import_cfg(last_sync_at=int(time.time()) - 5 * 60, **over)


def _xmltv_without_import():
    return _MockXMLTV(channels=[{'id': 'x@other'}],
                      programmes=[{'channel': 'x@other', 'start': '20990101000000 +0000',
                                   'stop': '20990101010000 +0000', 'title': [('Other', '')]}])


def test_missing_guide_forces_sync_and_restores_it_from_cache(monkeypatch, tmp_path):
    _seed_cache(tmp_path)
    results, xmltv, _, _ = _run_sync(monkeypatch, epg_response=NOT_MOD,
                                     cfg=_recent_cfg(epg_etag='"e"'),
                                     xmltv=_xmltv_without_import())
    assert results[IMPORT_ID]['status'] != 'skipped'
    assert _titles(xmltv, 'c1@imp') == ['New One']
    assert _titles(xmltv, 'c2@imp') == ['New Two']


def test_missing_guide_self_heal_works_while_source_is_down(monkeypatch, tmp_path):
    _seed_cache(tmp_path)
    _, xmltv, _, _ = _run_sync(monkeypatch, epg_response=DOWN,
                               cfg=_recent_cfg(), xmltv=_xmltv_without_import())
    assert _titles(xmltv, 'c1@imp') == ['New One']


def test_self_heal_is_throttled(monkeypatch, tmp_path):
    _seed_cache(tmp_path)
    first, _, _, _ = _run_sync(monkeypatch, epg_response=NOT_MOD,
                               cfg=_recent_cfg(), xmltv=_xmltv_without_import())
    second, _, _, _ = _run_sync(monkeypatch, epg_response=NOT_MOD,
                                cfg=_recent_cfg(), xmltv=_xmltv_without_import())
    assert first[IMPORT_ID]['status'] != 'skipped'
    assert second[IMPORT_ID]['status'] == 'skipped'


def test_present_guide_does_not_force(monkeypatch, tmp_path):
    _seed_cache(tmp_path)
    results, _, _, _ = _run_sync(monkeypatch, epg_response=NOT_MOD, cfg=_recent_cfg())
    assert results[IMPORT_ID]['status'] == 'skipped'


def test_no_cached_guide_does_not_force(monkeypatch):
    results, _, _, _ = _run_sync(monkeypatch, epg_response=NOT_MOD,
                                 cfg=_recent_cfg(), xmltv=_xmltv_without_import())
    assert results[IMPORT_ID]['status'] == 'skipped'


def test_no_live_channels_does_not_force(monkeypatch, tmp_path):
    _seed_cache(tmp_path)
    results, _, _, _ = _run_sync(monkeypatch, epg_response=NOT_MOD, cfg=_recent_cfg(),
                                 channels=[_channel(1, enabled=False)],
                                 xmltv=_xmltv_without_import())
    assert results[IMPORT_ID]['status'] == 'skipped'


def test_import_without_guide_source_does_not_force(monkeypatch, tmp_path):
    _seed_cache(tmp_path)
    results, _, _, _ = _run_sync(monkeypatch, epg_response=NOT_MOD,
                                 cfg=_recent_cfg(epg_url=''),
                                 xmltv=_xmltv_without_import())
    assert results[IMPORT_ID]['status'] == 'skipped'


# ================================================================ render-only kick

def test_render_only_kick_does_not_bypass_gate(monkeypatch, tmp_path):
    _seed_cache(tmp_path)
    results, _, _, stub = _run_sync(monkeypatch, epg_response=NOT_MOD,
                                    cfg=_recent_cfg(), force_scope=RENDER_ONLY_KICK)
    assert results[IMPORT_ID]['status'] == 'skipped'
    assert stub.calls == []
