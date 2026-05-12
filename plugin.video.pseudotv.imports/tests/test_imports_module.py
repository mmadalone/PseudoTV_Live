"""Unit tests for the imports module (live-imports skeleton).

Step 2 scope: helpers and parser wrappers. Sync orchestration methods
are stubs that raise NotImplementedError — they're tested here to
confirm the contract (raises with intelligible message), then will be
re-tested when implemented in step 3.
"""
import os

import pytest

from imports import (
    Imports,
    validate_source_url,
    fallback_channel_id,
    namespaced_channel_id,
    is_remote_url,
    ALLOWED_M3U_SCHEMES,
    ALLOWED_EPG_SCHEMES,
)
from conftest import FIXTURES_DIR


def _load_fixture(name):
    with open(os.path.join(FIXTURES_DIR, name), 'rb') as f:
        return f.read()


# ---------------------------------------------------------------- URL validation


def test_validate_url_accepts_http():
    assert validate_source_url('http://example.com/m3u') == 'http://example.com/m3u'


def test_validate_url_accepts_https():
    assert validate_source_url('https://example.com/m3u') == 'https://example.com/m3u'


def test_validate_url_accepts_special_path():
    p = 'special://userdata/addon_data/plugin.video.foo/channels.m3u8'
    assert validate_source_url(p) == p


def test_validate_url_rejects_file_scheme():
    with pytest.raises(ValueError, match='unsupported scheme'):
        validate_source_url('file:///etc/passwd')


def test_validate_url_rejects_ftp_scheme():
    with pytest.raises(ValueError, match='unsupported scheme'):
        validate_source_url('ftp://example.com/m3u')


def test_validate_url_rejects_empty():
    with pytest.raises(ValueError, match='empty'):
        validate_source_url('')


def test_validate_url_rejects_special_without_double_slash():
    with pytest.raises(ValueError, match='special://'):
        validate_source_url('special:relative/path')


def test_allowlists_share_same_default_set():
    # Both are http/https/special at v1; no scheme in one but not the other.
    assert ALLOWED_M3U_SCHEMES == ALLOWED_EPG_SCHEMES


# ---------------------------------------------------------------- ID helpers


def test_fallback_id_is_stable_md5_prefix():
    a = fallback_channel_id('plugin://x.y.z/?action=play&id=ABC')
    b = fallback_channel_id('plugin://x.y.z/?action=play&id=ABC')
    assert a == b
    assert len(a) == 12


def test_fallback_id_differs_per_url():
    a = fallback_channel_id('plugin://a/')
    b = fallback_channel_id('plugin://b/')
    assert a != b


def test_fallback_id_empty_url_returns_empty():
    assert fallback_channel_id('') == ''


def test_namespaced_id_uses_tvg_id_when_present():
    cid = namespaced_channel_id('TVE', 'movistarplus')
    assert cid == 'TVE@movistarplus'


def test_namespaced_id_falls_back_to_url_hash_when_no_tvg_id():
    cid = namespaced_channel_id('', 'movistarplus', source_url='plugin://x/?id=Y')
    head, tail = cid.split('@')
    assert tail == 'movistarplus'
    assert len(head) == 12  # md5 prefix


def test_namespaced_id_sanitizes_unsafe_chars():
    # Source tvg-id contains spaces and special chars → replaced with _.
    cid = namespaced_channel_id('TVE HD/Plus', 'movistarplus')
    assert ' ' not in cid
    assert '/' not in cid
    assert cid == 'TVE_HD_Plus@movistarplus'


def test_namespaced_id_sanitizes_import_id_too():
    cid = namespaced_channel_id('TVE', 'movistar+plus')
    assert cid == 'TVE@movistar_plus'


def test_namespaced_id_requires_import_id():
    with pytest.raises(ValueError, match='import_id'):
        namespaced_channel_id('TVE', '')


def test_namespaced_id_with_no_tvg_id_and_no_url_raises():
    with pytest.raises(ValueError):
        namespaced_channel_id('', 'movistarplus')


# ---------------------------------------------------------------- is_remote_url


def test_is_remote_url_true_for_http():
    assert is_remote_url('http://example.com/x') is True


def test_is_remote_url_true_for_https():
    assert is_remote_url('https://example.com/x') is True


def test_is_remote_url_false_for_special():
    assert is_remote_url('special://userdata/x') is False


def test_is_remote_url_false_for_absolute_path():
    assert is_remote_url('/var/lib/x') is False


def test_is_remote_url_false_for_empty():
    assert is_remote_url('') is False
    assert is_remote_url(None) is False


# ---------------------------------------------------------------- Imports class


def test_imports_constructs_with_no_collaborators():
    # All four collaborators optional — supports unit-test isolation.
    imp = Imports()
    assert imp.channels is None
    assert imp.m3u is None
    assert imp.xmltv is None
    assert imp.service is None


def test_imports_constructs_with_collaborators():
    imp = Imports(channels='c', m3u='m', xmltv='x', service='s')
    assert imp.channels == 'c'
    assert imp.m3u == 'm'
    assert imp.xmltv == 'x'
    assert imp.service == 's'


# ---------------------------------------------------------------- parseM3U


def test_parseM3U_returns_namespaced_channel_records():
    imp = Imports()
    raw = _load_fixture('sample_movistarplus.m3u8')
    out = imp.parseM3U(raw, 'movistarplus')
    assert len(out) == 5

    # First channel: TVE → TVE@movistarplus
    first = out[0]
    assert first['id'] == 'TVE@movistarplus'
    assert first['epg_id'] == 'TVE'
    assert first['import_source'] == 'movistarplus'
    assert first['source_tvg_chno'] == 1
    assert first['name'] == 'LA 1'


def test_parseM3U_preserves_source_url_for_playback():
    imp = Imports()
    raw = _load_fixture('sample_movistarplus.m3u8')
    out = imp.parseM3U(raw, 'movistarplus')
    assert out[0]['source_url'].startswith('plugin://plugin.video.movistarplus/')


def test_parseM3U_preserves_catchup_attributes_verbatim():
    imp = Imports()
    raw = _load_fixture('sample_movistarplus.m3u8')
    out = imp.parseM3U(raw, 'movistarplus')
    cuatro = next(c for c in out if c['name'] == 'Cuatro')
    assert cuatro['catchup'] == 'vod'
    assert 'catchup' in cuatro['catchup-source']


def test_parseM3U_ignores_unknown_id_collisions_across_imports():
    # Same M3U imported under two different import_ids must produce
    # disjoint channel ID namespaces.
    imp = Imports()
    raw = _load_fixture('sample_movistarplus.m3u8')
    a = imp.parseM3U(raw, 'movistar_a')
    b = imp.parseM3U(raw, 'movistar_b')
    assert {c['id'] for c in a}.isdisjoint({c['id'] for c in b})


def test_parseM3U_skips_entries_without_url():
    imp = Imports()
    raw = _load_fixture('malformed_missing_url.m3u')
    out = imp.parseM3U(raw, 'test')
    assert len(out) == 1
    assert out[0]['name'] == 'GoodOne'


def test_parseM3U_filters_duplicate_tvg_ids():
    imp = Imports()
    raw = _load_fixture('duplicates.m3u')
    out = imp.parseM3U(raw, 'test')
    assert len(out) == 2
    names = [c['name'] for c in out]
    assert names == ['First', 'Unique']


# ---------------------------------------------------------------- parseXMLTV


def test_parseXMLTV_yields_channels_and_programmes():
    imp = Imports()
    items = list(imp.parseXMLTV(_load_fixture('sample_xmltv.xml')))
    channels = [d for k, d in items if k == 'channel']
    programmes = [d for k, d in items if k == 'programme']
    assert len(channels) == 3
    assert len(programmes) == 5


def test_parseXMLTV_passes_filter_through():
    imp = Imports()
    items = list(imp.parseXMLTV(_load_fixture('sample_xmltv.xml'),
                                channel_id_filter={'TVE'}))
    progs = [d for k, d in items if k == 'programme']
    assert all(p['channel'] == 'TVE' for p in progs)
    assert len(progs) == 2


def test_parseXMLTV_handles_gzip():
    imp = Imports()
    items = list(imp.parseXMLTV(_load_fixture('sample_xmltv.xml.gz')))
    channels = [d for k, d in items if k == 'channel']
    assert len(channels) == 3


# ---------------------------------------------------------------- skeleton stubs


# ---------------------------------------------------------------- reconcile (step 3)


def _ch(cid, name='X', number=None, **extra):
    """Helper: construct a channel record dict for tests."""
    base = {
        'id'             : cid,
        'name'           : name,
        'type'           : extra.pop('type', 'import'),
        'import_source'  : extra.pop('import_source', 'src'),
        'epg_id'         : extra.pop('epg_id', cid.split('@')[0]),
        'enabled'        : True,
        'operator_overrides': extra.pop('operator_overrides', []),
    }
    if number is not None:
        base['number'] = number
    base.update(extra)
    return base


def test_reconcile_classifies_new_refreshed_orphans():
    imp = Imports()
    parsed = [_ch('A@s'), _ch('B@s'), _ch('C@s')]
    existing = [_ch('B@s', name='B old'), _ch('Z@s', name='Z removed from source')]
    new, refreshed, orphans = imp.reconcile(parsed, existing, {})
    assert {c['id'] for c in new}       == {'A@s', 'C@s'}
    assert {p[0]['id'] for p in refreshed} == {'B@s'}
    assert {c['id'] for c in orphans}   == {'Z@s'}


def test_reconcile_filters_tombstoned_new_channels():
    imp = Imports()
    parsed = [_ch('A@s'), _ch('B@s')]
    existing = []
    new, _r, _o = imp.reconcile(parsed, existing, {'tombstones': ['B@s']})
    assert {c['id'] for c in new} == {'A@s'}


def test_reconcile_empty_inputs_yield_empty_outputs():
    imp = Imports()
    new, refreshed, orphans = imp.reconcile([], [], {})
    assert new == [] and refreshed == [] and orphans == []


# ---------------------------------------------------------------- applyRefresh


def test_applyRefresh_updates_source_url_always():
    imp = Imports()
    existing = _ch('A@s', name='A')
    existing['source_url'] = 'OLD_URL'
    parsed = _ch('A@s', name='A new')
    parsed['source_url']   = 'NEW_URL'
    imp.applyRefresh(existing, parsed)
    assert existing['source_url'] == 'NEW_URL'


def test_applyRefresh_overwrites_name_unless_in_overrides():
    imp = Imports()
    e1 = _ch('A@s', name='A old')
    p1 = _ch('A@s', name='A NEW')
    imp.applyRefresh(e1, p1)
    assert e1['name'] == 'A NEW'

    e2 = _ch('A@s', name='Operator picked', operator_overrides=['name'])
    p2 = _ch('A@s', name='Source name')
    imp.applyRefresh(e2, p2)
    assert e2['name'] == 'Operator picked'  # override wins


def test_applyRefresh_clears_orphan_flag_when_source_returns_channel():
    imp = Imports()
    existing = _ch('A@s', is_orphan=True)
    parsed = _ch('A@s')
    imp.applyRefresh(existing, parsed)
    assert existing['is_orphan'] is False


# ---------------------------------------------------------------- cascadeAllocate


def test_cascade_allocates_new_channels_from_start_num():
    imp = Imports()
    cfg = {'id': 'src', 'enabled': True, 'respect_source_numbers': False, 'start_num': 100}
    parsed = [_ch('A@src'), _ch('B@src'), _ch('C@src')]
    result = imp.cascadeAllocate([(cfg, parsed)], [])
    assert result == {'A@src': 100, 'B@src': 101, 'C@src': 102}


def test_cascade_respects_source_numbers_when_configured():
    imp = Imports()
    cfg = {'id': 'src', 'enabled': True, 'respect_source_numbers': True, 'start_num': 1}
    parsed = [
        _ch('A@src', source_tvg_chno=5),
        _ch('B@src', source_tvg_chno=3),
        _ch('C@src', source_tvg_chno=7),
    ]
    result = imp.cascadeAllocate([(cfg, parsed)], [])
    assert result == {'A@src': 5, 'B@src': 3, 'C@src': 7}


def test_cascade_pseudotv_built_channels_are_hard_pins():
    imp = Imports()
    existing = [
        {'id': 'pseudo700', 'name': 'P', 'type': '', 'number': 700},
        {'id': 'pseudo701', 'name': 'P', 'type': '', 'number': 701},
    ]
    cfg = {'id': 'src', 'enabled': True, 'respect_source_numbers': True, 'start_num': 1}
    parsed = [_ch('CONFLICT@src', source_tvg_chno=700)]
    result = imp.cascadeAllocate([(cfg, parsed)], existing)
    # Hard pin at 700 → CONFLICT cascades to 702 (701 also pinned)
    assert result.get('CONFLICT@src') == 702


def test_cascade_sticky_preserved_across_cycles():
    imp = Imports()
    # Existing channel was previously allocated 105
    existing = [_ch('A@src', name='A', assigned_number=105, type='import',
                    import_source='src')]
    cfg = {'id': 'src', 'enabled': True, 'respect_source_numbers': False, 'start_num': 100}
    parsed = [_ch('A@src'), _ch('B@src')]  # A is sticky; B is new
    result = imp.cascadeAllocate([(cfg, parsed)], existing)
    assert result.get('A@src') == 105  # sticky preserved
    assert result.get('B@src') == 100  # B is new — gets start_num


def test_cascade_operator_pinned_imports_are_hard_pins():
    imp = Imports()
    existing = [_ch('A@src', name='A', number=50, type='import',
                    import_source='src', operator_overrides=['number'])]
    cfg = {'id': 'src2', 'enabled': True, 'respect_source_numbers': True, 'start_num': 1}
    parsed = [_ch('B@src2', source_tvg_chno=50)]  # would collide with operator pin
    result = imp.cascadeAllocate([(cfg, parsed)], existing)
    assert result.get('B@src2') == 51  # cascaded past pin at 50


def test_cascade_priority_order_winner_takes_desired_number():
    imp = Imports()
    cfg_high = {'id': 'high', 'enabled': True, 'respect_source_numbers': True, 'start_num': 1}
    cfg_low  = {'id': 'low',  'enabled': True, 'respect_source_numbers': True, 'start_num': 1}
    parsed_high = [_ch('A@high', source_tvg_chno=3), _ch('B@high', source_tvg_chno=4)]
    parsed_low  = [_ch('X@low',  source_tvg_chno=3), _ch('Y@low',  source_tvg_chno=4)]
    # high listed FIRST in priority
    result = imp.cascadeAllocate([(cfg_high, parsed_high), (cfg_low, parsed_low)], [])
    assert result['A@high'] == 3  # winner
    assert result['B@high'] == 4  # winner
    assert result['X@low']  == 5  # cascaded past 3,4
    assert result['Y@low']  == 6  # cascaded past 3,4,5


def test_cascade_disabled_imports_not_allocated():
    imp = Imports()
    cfg = {'id': 'src', 'enabled': False, 'respect_source_numbers': False, 'start_num': 1}
    parsed = [_ch('A@src')]
    result = imp.cascadeAllocate([(cfg, parsed)], [])
    assert 'A@src' not in result


# ---------------------------------------------------------------- fetchSource (local)


def test_fetchSource_local_file_returns_content_first_call(tmp_path):
    imp = Imports()
    f = tmp_path / 'sample.m3u'
    f.write_bytes(b'#EXTM3U\n#EXTINF:-1,X\nhttp://x\n')
    result = imp.fetchSource(str(f))
    assert result['status'] == 200
    assert result['content'] == b'#EXTM3U\n#EXTINF:-1,X\nhttp://x\n'
    assert result['error'] is None
    assert result['last_modified'] is not None


def test_fetchSource_local_file_returns_304_when_unchanged(tmp_path):
    imp = Imports()
    f = tmp_path / 'sample.m3u'
    f.write_bytes(b'#EXTM3U\n')
    first = imp.fetchSource(str(f))
    assert first['status'] == 200
    second = imp.fetchSource(str(f), last_modified=first['last_modified'])
    assert second['status'] == 304
    assert second['content'] is None


def test_fetchSource_rejects_disallowed_scheme():
    imp = Imports()
    result = imp.fetchSource('file:///etc/passwd')
    assert result['status'] == 0
    assert 'unsupported' in (result['error'] or '').lower()


def test_fetchSource_local_missing_file_returns_error():
    imp = Imports()
    result = imp.fetchSource('/tmp/this-definitely-does-not-exist-12345.m3u')
    assert result['status'] == 0
    assert result['content'] is None


# ---------------------------------------------------------------- syncAll integration


class _MockChannels:
    """Minimal Channels surface for syncAll integration test."""
    def __init__(self, imports_list, channels_list):
        self._imports  = imports_list
        self._channels = channels_list
        self.set_calls_channels = []
        self.set_calls_imports  = []
    def getImports(self):  return list(self._imports)
    def getChannels(self): return list(self._channels)
    def setChannels(self, channels=None, modified_ids=None):
        self.set_calls_channels.append((channels, modified_ids))
        if channels is not None: self._channels = channels
        return True
    def setImports(self, data):
        self.set_calls_imports.append(data)
        self._imports = data
        return True


class _MockM3U:
    def __init__(self): self.added = []
    def addStation(self, sitem): self.added.append(sitem)


class _MockXMLTV:
    """Mock for XMLTVS — exposes the XMLTVDATA dict that syncAll appends to.

    Step 3 syncAll bypasses XMLTVS.addChannel/addProgram (which expect PseudoTV's
    internal scheduling format) and directly mutates XMLTVDATA['channels'] and
    XMLTVDATA['programmes']. This mock surfaces the same shape so behavior tests
    can assert the writes happened correctly.
    """
    def __init__(self):
        self.XMLTVDATA = {'channels': [], 'programmes': [], 'recordings': []}
    @property
    def channels(self):   return self.XMLTVDATA['channels']
    @property
    def programmes(self): return self.XMLTVDATA['programmes']


def test_syncAll_end_to_end_with_movistarplus_fixture():
    # Configure a single import pointing at the MovistarPlus M3U fixture +
    # the synthetic XMLTV fixture. No existing channels (fresh install).
    m3u_path = os.path.join(FIXTURES_DIR, 'sample_movistarplus.m3u8')
    epg_path = os.path.join(FIXTURES_DIR, 'sample_xmltv.xml')

    import_cfg = {
        'id'                    : 'movistarplus',
        'name'                  : 'MovistarPlus',
        'enabled'               : True,
        'm3u_path'              : m3u_path,
        'epg_path'              : epg_path,
        'respect_source_numbers': False,
        'start_num'             : 800,
    }
    mock_channels = _MockChannels([import_cfg], [])
    mock_m3u      = _MockM3U()
    mock_xmltv    = _MockXMLTV()

    imp = Imports(channels=mock_channels, m3u=mock_m3u, xmltv=mock_xmltv)
    results = imp.syncAll()

    # Verify per-import result
    assert 'movistarplus' in results
    assert results['movistarplus']['status'] == 'ok'
    assert len(results['movistarplus']['new']) == 5  # 5 channels in fixture
    assert len(results['movistarplus']['orphans']) == 0

    # Verify channels were persisted to mock channels.json
    assert mock_channels.set_calls_channels, 'setChannels was never called'
    persisted = mock_channels._channels
    imported = [c for c in persisted if c.get('type') == 'import']
    assert len(imported) == 5
    assert {c['name'] for c in imported} == {'LA 1', 'LA 2', 'Antena 3', 'Cuatro', 'Telecinco'}

    # Verify cascade allocation: start_num=800 → 800, 801, 802, 803, 804
    numbers = sorted(c['number'] for c in imported)
    assert numbers == [800, 801, 802, 803, 804]

    # Verify imported config persisted with last_status/last_sync_at
    assert mock_channels.set_calls_imports, 'setImports was never called'
    persisted_cfg = mock_channels._imports[0]
    assert persisted_cfg['last_status'] == 'ok'
    assert persisted_cfg['last_sync_at'] is not None
    assert persisted_cfg['last_modified'] is not None  # mtime captured

    # Verify M3U received namespaced stations
    assert len(mock_m3u.added) == 5
    station_ids = {s['id'] for s in mock_m3u.added}
    assert station_ids == {'TVE@movistarplus', 'LA2@movistarplus', 'A3@movistarplus',
                           'C4@movistarplus', 'T5@movistarplus'}

    # Verify XMLTV received namespaced channels (id rewritten from source TVE → TVE@movistarplus)
    xmltv_ids = {c['id'] for c in mock_xmltv.channels}
    assert 'TVE@movistarplus' in xmltv_ids
    # No source-side ids should leak into the merged xmltv
    assert 'TVE' not in xmltv_ids
    # Programmes whose channel was matched also rewritten
    prog_chans = {p['channel'] for p in mock_xmltv.programmes}
    # ORPHAN_NOT_IN_M3U fixture programme has no matching channel → filtered out
    assert all('@movistarplus' in c for c in prog_chans)


def test_syncAll_second_call_returns_unchanged_via_mtime_check():
    # Calling syncAll twice on the same source (no changes) → second call
    # should yield 304 / 'unchanged' and not re-allocate numbers.
    m3u_path = os.path.join(FIXTURES_DIR, 'sample_movistarplus.m3u8')

    import_cfg = {
        'id'                    : 'movistarplus',
        'name'                  : 'MovistarPlus',
        'enabled'               : True,
        'm3u_path'              : m3u_path,
        'respect_source_numbers': False,
        'start_num'             : 800,
    }
    mock_channels = _MockChannels([import_cfg], [])
    mock_m3u      = _MockM3U()
    mock_xmltv    = _MockXMLTV()

    Imports(channels=mock_channels, m3u=mock_m3u, xmltv=mock_xmltv).syncAll()
    # Second pass — same imports config (with last_modified now set), same fixture
    second = Imports(channels=mock_channels, m3u=mock_m3u, xmltv=mock_xmltv).syncAll()
    assert second['movistarplus']['status'] == 'unchanged'


def test_log_helper_does_not_crash_when_globals_unavailable():
    # The `log` helper should degrade silently if `log` (from globals) isn't
    # bound, e.g. during unit-test import-time when stubs may not provide it.
    imp = Imports()
    imp.log('test message')  # must not raise
