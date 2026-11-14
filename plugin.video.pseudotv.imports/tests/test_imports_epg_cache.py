"""Tests for per-import EPG cache files (step 5).

Covers Imports._epgCacheWrite / _epgCacheRead and the syncOne 200/304
behavior that uses them, plus Channels.setImports orphan-cache cleanup
(imports.69: on the real Channels, after a successful write only).

The cache lives at CACHE_LOC/epg_<import_id>.xml. Each test monkeypatches
CACHE_LOC to a tmp_path-rooted dir so the real addon_data is untouched.
"""
import gzip
import os

import pytest

from imports import Imports
from conftest import FIXTURES_DIR


def _load_fixture(name):
    with open(os.path.join(FIXTURES_DIR, name), 'rb') as f:
        return f.read()


def _stub_fetch(status, content=None, etag=None, last_modified=None):
    """Build a fetchSource stub that returns canned values regardless of caller args."""
    canned = {'status': status, 'content': content,
              'etag': etag, 'last_modified': last_modified, 'error': None}
    def _fn(*args, **kwargs):
        return dict(canned)
    return _fn


def _epg_only_cfg(import_id, **overrides):
    """Minimal EPG-only import_cfg (no m3u_url → reconcile gets [])."""
    base = {
        'id'      : import_id,
        'enabled' : True,
        'epg_url' : 'https://example.com/epg.xml',
    }
    base.update(overrides)
    return base


# ============================================================ helpers


def test_epgCacheWrite_creates_file_with_exact_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr('imports.CACHE_LOC', str(tmp_path))
    imp = Imports()
    payload = b'<tv><channel id="x"/></tv>'
    assert imp._epgCacheWrite('movistarplus', payload) is True
    cache_path = os.path.join(str(tmp_path), 'epg_movistarplus.xml')
    assert os.path.exists(cache_path)
    with open(cache_path, 'rb') as f:
        assert f.read() == payload


def test_epgCacheWrite_lazy_creates_cache_dir(tmp_path, monkeypatch):
    nested = tmp_path / 'does_not_exist_yet'
    monkeypatch.setattr('imports.CACHE_LOC', str(nested))
    imp = Imports()
    assert imp._epgCacheWrite('foo', b'<tv/>') is True
    assert (nested / 'epg_foo.xml').exists()


def test_epgCacheRead_returns_none_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr('imports.CACHE_LOC', str(tmp_path))
    imp = Imports()
    assert imp._epgCacheRead('absent') is None


def test_epgCacheRead_returns_none_when_corrupt(tmp_path, monkeypatch):
    monkeypatch.setattr('imports.CACHE_LOC', str(tmp_path))
    imp = Imports()
    # Truncated/garbage cache file → parseExternalSource yields 0 items
    (tmp_path / 'epg_garbage.xml').write_bytes(b'<tv><channel id="x"')
    assert imp._epgCacheRead('garbage') is None


# ============================================================ syncOne 200 path


def test_syncOne_200_writes_cache_verbatim(tmp_path, monkeypatch):
    monkeypatch.setattr('imports.CACHE_LOC', str(tmp_path))
    imp = Imports()
    content = _load_fixture('sample_xmltv.xml')
    monkeypatch.setattr(imp, 'fetchSource',
                        _stub_fetch(200, content,
                                    etag='"abc-etag"',
                                    last_modified='Tue, 01 Jan 2026 00:00:00 GMT'))
    cfg = _epg_only_cfg('test200')
    result = imp.syncOne(cfg, existing_channels=[])
    # Cache file written with verbatim fetched bytes
    cache_path = os.path.join(str(tmp_path), 'epg_test200.xml')
    assert os.path.exists(cache_path)
    with open(cache_path, 'rb') as f:
        assert f.read() == content
    # Pairs parsed inline + validators persisted
    assert len(result['epg_pairs']) > 0
    assert result['updated_cfg'].get('epg_etag')          == '"abc-etag"'
    assert result['updated_cfg'].get('epg_last_modified') == 'Tue, 01 Jan 2026 00:00:00 GMT'


# ============================================================ syncOne 304 path


def test_syncOne_304_with_cache_hydrates_epg_pairs(tmp_path, monkeypatch):
    monkeypatch.setattr('imports.CACHE_LOC', str(tmp_path))
    imp = Imports()
    # Pre-populate the cache file with a known-good fixture
    cache_bytes = _load_fixture('sample_xmltv.xml')
    (tmp_path / 'epg_hydrate304.xml').write_bytes(cache_bytes)
    monkeypatch.setattr(imp, 'fetchSource', _stub_fetch(304))
    # Operator's stored validators (must be honored — no force-fetch)
    cfg = _epg_only_cfg('hydrate304',
                       epg_etag='"prev-etag"',
                       epg_last_modified='Mon, 31 Dec 2025 23:00:00 GMT')
    result = imp.syncOne(cfg, existing_channels=[])
    # Pairs hydrated from disk match a fresh in-memory parse of same bytes
    expected = list(imp.parseXMLTV(cache_bytes))
    assert result['epg_pairs'] == expected
    assert len(result['epg_pairs']) > 0
    # 304 must NOT clear validators when cache is healthy
    assert result['updated_cfg'].get('epg_etag')          == '"prev-etag"'
    assert result['updated_cfg'].get('epg_last_modified') == 'Mon, 31 Dec 2025 23:00:00 GMT'


def test_syncOne_304_missing_cache_clears_validators(tmp_path, monkeypatch):
    monkeypatch.setattr('imports.CACHE_LOC', str(tmp_path))
    imp = Imports()
    monkeypatch.setattr(imp, 'fetchSource', _stub_fetch(304))
    cfg = _epg_only_cfg('missing304',
                       epg_etag='"orphan-etag"',
                       epg_last_modified='Mon, 31 Dec 2025 23:00:00 GMT')
    result = imp.syncOne(cfg, existing_channels=[])
    assert result['epg_pairs'] == []
    # Validators cleared so next cycle force-fetches
    assert result['updated_cfg']['epg_etag']          is None
    assert result['updated_cfg']['epg_last_modified'] is None


def test_syncOne_304_corrupt_cache_clears_validators(tmp_path, monkeypatch):
    monkeypatch.setattr('imports.CACHE_LOC', str(tmp_path))
    imp = Imports()
    (tmp_path / 'epg_corrupt304.xml').write_bytes(b'<tv><chan')  # truncated
    monkeypatch.setattr(imp, 'fetchSource', _stub_fetch(304))
    cfg = _epg_only_cfg('corrupt304',
                       epg_etag='"stale"',
                       epg_last_modified='Mon, 31 Dec 2025 23:00:00 GMT')
    result = imp.syncOne(cfg, existing_channels=[])
    assert result['epg_pairs'] == []
    assert result['updated_cfg']['epg_etag']          is None
    assert result['updated_cfg']['epg_last_modified'] is None


# ============================================================ gzip round-trip


def test_gzipped_cache_round_trip(tmp_path, monkeypatch):
    """parser auto-detects gzip magic bytes — caching the gzipped response
    verbatim is valid and re-readable."""
    monkeypatch.setattr('imports.CACHE_LOC', str(tmp_path))
    imp = Imports()
    gz_bytes = _load_fixture('sample_xmltv.xml.gz')
    # Sanity: this fixture really is gzipped
    assert gz_bytes[:2] == b'\x1f\x8b'
    # Round-trip via syncOne 200 (writes verbatim) → 304 (reads + auto-decompress)
    monkeypatch.setattr(imp, 'fetchSource',
                        _stub_fetch(200, gz_bytes, etag='"g"', last_modified='x'))
    r1 = imp.syncOne(_epg_only_cfg('gz_test'), existing_channels=[])
    assert len(r1['epg_pairs']) > 0
    # Now simulate next cycle returning 304 — cache should hydrate identically
    monkeypatch.setattr(imp, 'fetchSource', _stub_fetch(304))
    r2 = imp.syncOne(_epg_only_cfg('gz_test',
                                   epg_etag='"g"', epg_last_modified='x'),
                     existing_channels=[])
    assert r2['epg_pairs'] == r1['epg_pairs']


# ============================================================ cross-import isolation


def test_cross_import_isolation_200_and_304(tmp_path, monkeypatch):
    """A 200 fetch for one import and a 304 (cache-hit) for another in
    succession must each produce non-empty epg_pairs — neither cycle
    contaminates the other."""
    monkeypatch.setattr('imports.CACHE_LOC', str(tmp_path))
    imp = Imports()
    content = _load_fixture('sample_xmltv.xml')
    # Pre-seed cache for import B (will return 304)
    (tmp_path / 'epg_b.xml').write_bytes(content)

    # First cycle: A returns 200 (writes its own cache)
    monkeypatch.setattr(imp, 'fetchSource', _stub_fetch(200, content,
                                                       etag='"a"', last_modified='y'))
    res_a = imp.syncOne(_epg_only_cfg('a'), existing_channels=[])

    # Second cycle: B returns 304 (hydrates from pre-seeded cache)
    monkeypatch.setattr(imp, 'fetchSource', _stub_fetch(304))
    res_b = imp.syncOne(_epg_only_cfg('b', epg_etag='"b"', epg_last_modified='z'),
                        existing_channels=[])

    # Both imports have epg_pairs; both validators preserved/persisted
    assert len(res_a['epg_pairs']) > 0
    assert len(res_b['epg_pairs']) > 0
    assert res_a['updated_cfg'].get('epg_etag') == '"a"'
    assert res_b['updated_cfg'].get('epg_etag') == '"b"'
    # Both cache files exist
    assert (tmp_path / 'epg_a.xml').exists()
    assert (tmp_path / 'epg_b.xml').exists()


# ============================================================ setImports cleanup


# imports.69: setImports is one locked commit on the file as it is now, and the
# guide caches of imports no longer listed go only after a successful write.
# The fake instance these tests used had no commit, so they run on the REAL
# Channels against a channels.json in tmp_path, with channels.CACHE_LOC
# pointed at a tmp folder.


def _real_channels(tmp_path, monkeypatch, initial_imports):
    import json
    import channels as channels_module
    cache = tmp_path / 'cache'
    cache.mkdir()
    monkeypatch.setattr(channels_module, 'CACHE_LOC', str(cache))
    path = tmp_path / 'channels.json'
    path.write_text(json.dumps({'uuid': 'u', 'channels': [], 'imports': list(initial_imports)}))
    return channels_module.Channels(file=str(path), writable=True), path, cache


def test_setImports_deletes_orphan_caches(tmp_path, monkeypatch):
    initial = [{'id': 'keep1', 'enabled': True},
               {'id': 'remove_me', 'enabled': True},
               {'id': 'keep2', 'enabled': True}]
    ch, _path, cache = _real_channels(tmp_path, monkeypatch, initial)
    # Three pre-existing cache files
    for iid in ('keep1', 'remove_me', 'keep2'):
        (cache / ('epg_%s.xml' % iid)).write_bytes(b'<tv/>')
    new = [{'id': 'keep1', 'enabled': True},
           {'id': 'keep2', 'enabled': True}]
    assert ch.setImports(new) is True
    assert not (cache / 'epg_remove_me.xml').exists()
    assert (cache / 'epg_keep1.xml').exists()
    assert (cache / 'epg_keep2.xml').exists()


def test_setImports_keeps_disabled_caches(tmp_path, monkeypatch):
    """Disabling an import keeps its cache (operator may re-enable)."""
    ch, _path, cache = _real_channels(tmp_path, monkeypatch, [{'id': 'toggle', 'enabled': True}])
    (cache / 'epg_toggle.xml').write_bytes(b'<tv/>')
    assert ch.setImports([{'id': 'toggle', 'enabled': False}]) is True
    assert (cache / 'epg_toggle.xml').exists()


@pytest.mark.parametrize('how', ['returns False', 'raises'])
def test_setImports_write_failed_keeps_the_cache(tmp_path, monkeypatch, how):
    """imports.69: before, the removed import's cache was unlinked even when
    the save failed, so the import stayed listed without its guide."""
    import channels as channels_module
    ch, path, cache = _real_channels(tmp_path, monkeypatch, [{'id': 'keep', 'enabled': True},
                                                             {'id': 'remove_me', 'enabled': True}])
    (cache / 'epg_remove_me.xml').write_bytes(b'<tv/>')
    before = path.read_bytes()

    def failing(file, data):
        if how == 'raises':
            raise Exception('FileLock: Timeout occurred.')
        return False
    monkeypatch.setattr(channels_module.FileAccess, 'setJSON', failing)
    assert ch.setImports([{'id': 'keep', 'enabled': True}]) is False
    assert (cache / 'epg_remove_me.xml').exists(), 'nothing written, so the cache stays'
    assert path.read_bytes() == before
