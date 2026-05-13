"""Tests for per-channel logo caching (step 5).

Exercises Imports._logoCachePath / _logoExtForUrl / _cacheLogo /
_evictOrphanLogos / validators round-trip. Uses tmp_path + monkeypatched
imports.CACHE_LOC so the real addon_data is untouched.
"""
import json
import os

import pytest

from imports import Imports


def _stub_fetch_logo(status, content=b'', etag=None, last_modified=None,
                    content_type=None, error=None):
    canned = {
        'status': status, 'content': content, 'etag': etag,
        'last_modified': last_modified, 'content_type': content_type,
        'error': error,
    }
    def _fn(*args, **kwargs):
        return dict(canned)
    return _fn


# ============================================================ helpers


def test_logoCachePath_uses_cache_loc(tmp_path, monkeypatch):
    monkeypatch.setattr('imports.CACHE_LOC', str(tmp_path))
    imp = Imports()
    p = imp._logoCachePath('TVE@movistarplus', 'png')
    assert p == os.path.join(str(tmp_path), 'logos', 'TVE@movistarplus.png')


def test_logoExtForUrl_prefers_content_type(tmp_path, monkeypatch):
    monkeypatch.setattr('imports.CACHE_LOC', str(tmp_path))
    imp = Imports()
    assert imp._logoExtForUrl('https://x/y.bogus', 'image/png') == 'png'
    assert imp._logoExtForUrl('https://x/y.unknown', 'image/jpeg; charset=binary') == 'jpg'
    assert imp._logoExtForUrl('https://x/y.png', None) == 'png'
    assert imp._logoExtForUrl('https://x/y', None) == 'img'


def test_validators_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr('imports.CACHE_LOC', str(tmp_path))
    imp = Imports()
    payload = {'foo@imp': {'etag': '"abc"', 'last_modified': 'Mon, 01 Jan 2026 00:00:00 GMT'}}
    imp._logoSaveValidators(payload)
    assert imp._logoLoadValidators() == payload


def test_validators_load_returns_empty_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr('imports.CACHE_LOC', str(tmp_path))
    imp = Imports()
    assert imp._logoLoadValidators() == {}


# ============================================================ _cacheLogo modes


def test_cacheLogo_mode0_fetches_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr('imports.CACHE_LOC', str(tmp_path))
    imp = Imports()
    monkeypatch.setattr(imp, '_fetchLogo',
                        _stub_fetch_logo(200, b'PNG_BYTES', etag='"e"',
                                         last_modified='lm',
                                         content_type='image/png'))
    validators = {}
    result = imp._cacheLogo('chA@s1', 'https://example.com/logo.png', 0, validators)
    assert result == os.path.join(str(tmp_path), 'logos', 'chA@s1.png')
    with open(result, 'rb') as f:
        assert f.read() == b'PNG_BYTES'
    assert validators['chA@s1']['etag'] == '"e"'


def test_cacheLogo_mode0_skips_when_present(tmp_path, monkeypatch):
    monkeypatch.setattr('imports.CACHE_LOC', str(tmp_path))
    imp = Imports()
    # Pre-create a cached file
    logos_dir = tmp_path / 'logos'
    logos_dir.mkdir()
    existing = logos_dir / 'chB@s1.png'
    existing.write_bytes(b'OLD_BYTES')
    # Stub fetch should NOT be called — fail it loudly if it is
    def _fail_fetch(*a, **k):
        raise AssertionError('fetch should not be called in mode 0 when cached')
    monkeypatch.setattr(imp, '_fetchLogo', _fail_fetch)
    validators = {}
    result = imp._cacheLogo('chB@s1', 'https://example.com/logo.png', 0, validators)
    assert result == str(existing)
    # Bytes untouched
    assert existing.read_bytes() == b'OLD_BYTES'


def test_cacheLogo_mode1_conditional_get_304_keeps_existing(tmp_path, monkeypatch):
    monkeypatch.setattr('imports.CACHE_LOC', str(tmp_path))
    imp = Imports()
    logos_dir = tmp_path / 'logos'
    logos_dir.mkdir()
    existing = logos_dir / 'chC@s1.jpg'
    existing.write_bytes(b'JPEG_BYTES')
    monkeypatch.setattr(imp, '_fetchLogo', _stub_fetch_logo(304))
    validators = {'chC@s1': {'etag': '"prev"', 'last_modified': 'old'}}
    result = imp._cacheLogo('chC@s1', 'https://example.com/logo.jpg', 1, validators)
    assert result == str(existing)
    # Validators preserved (304 → no update)
    assert validators['chC@s1']['etag'] == '"prev"'


def test_cacheLogo_mode1_200_overwrites_and_updates_validators(tmp_path, monkeypatch):
    monkeypatch.setattr('imports.CACHE_LOC', str(tmp_path))
    imp = Imports()
    logos_dir = tmp_path / 'logos'
    logos_dir.mkdir()
    existing = logos_dir / 'chD@s1.png'
    existing.write_bytes(b'OLD')
    monkeypatch.setattr(imp, '_fetchLogo',
                        _stub_fetch_logo(200, b'NEW_PNG', etag='"new"',
                                         last_modified='now',
                                         content_type='image/png'))
    validators = {'chD@s1': {'etag': '"old"', 'last_modified': 'then'}}
    result = imp._cacheLogo('chD@s1', 'https://example.com/logo.png', 1, validators)
    assert result == str(existing)
    assert existing.read_bytes() == b'NEW_PNG'
    assert validators['chD@s1']['etag'] == '"new"'


def test_cacheLogo_mode2_always_fetches(tmp_path, monkeypatch):
    monkeypatch.setattr('imports.CACHE_LOC', str(tmp_path))
    imp = Imports()
    logos_dir = tmp_path / 'logos'
    logos_dir.mkdir()
    existing = logos_dir / 'chE@s1.png'
    existing.write_bytes(b'OLD')
    calls = []
    def _record_fetch(url, etag=None, last_modified=None, **kw):
        calls.append({'etag': etag, 'last_modified': last_modified})
        return {'status': 200, 'content': b'FRESH', 'etag': '"x"',
                'last_modified': 'y', 'content_type': 'image/png',
                'error': None}
    monkeypatch.setattr(imp, '_fetchLogo', _record_fetch)
    # Mode 2 should NOT send validators (always full fetch)
    validators = {'chE@s1': {'etag': '"prev"', 'last_modified': 'old'}}
    result = imp._cacheLogo('chE@s1', 'https://example.com/logo.png', 2, validators)
    assert result == str(existing)
    assert existing.read_bytes() == b'FRESH'
    assert calls and calls[0]['etag'] is None
    assert calls[0]['last_modified'] is None


def test_cacheLogo_failure_returns_existing(tmp_path, monkeypatch):
    monkeypatch.setattr('imports.CACHE_LOC', str(tmp_path))
    imp = Imports()
    logos_dir = tmp_path / 'logos'
    logos_dir.mkdir()
    existing = logos_dir / 'chF@s1.png'
    existing.write_bytes(b'KEEP')
    monkeypatch.setattr(imp, '_fetchLogo',
                        _stub_fetch_logo(0, b'', error='network error'))
    validators = {}
    # Mode 1 forces a fetch even when present; failure should return existing
    result = imp._cacheLogo('chF@s1', 'https://example.com/logo.png', 1, validators)
    assert result == str(existing)
    # Bytes untouched
    assert existing.read_bytes() == b'KEEP'
    # No validators added (no successful fetch)
    assert validators == {}


def test_cacheLogo_local_url_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr('imports.CACHE_LOC', str(tmp_path))
    imp = Imports()
    # _fetchLogo should NOT be called for local URLs
    def _fail_fetch(*a, **k):
        raise AssertionError('fetch should not be called for local URLs')
    monkeypatch.setattr(imp, '_fetchLogo', _fail_fetch)
    # Various local-style references
    for src in ('special://userdata/x.png', '/abs/path.png', '', None):
        assert imp._cacheLogo('chG@s1', src, 0, {}) is None


def test_cacheLogo_extension_change_drops_old_file(tmp_path, monkeypatch):
    monkeypatch.setattr('imports.CACHE_LOC', str(tmp_path))
    imp = Imports()
    logos_dir = tmp_path / 'logos'
    logos_dir.mkdir()
    old = logos_dir / 'chH@s1.png'
    old.write_bytes(b'OLD')
    # New fetch comes back as JPEG (different ext)
    monkeypatch.setattr(imp, '_fetchLogo',
                        _stub_fetch_logo(200, b'NEW_JPG', etag=None, last_modified=None,
                                         content_type='image/jpeg'))
    validators = {}
    result = imp._cacheLogo('chH@s1', 'https://example.com/logo.png', 1, validators)
    assert result.endswith('.jpg')
    assert not old.exists()  # old extension removed
    with open(result, 'rb') as f:
        assert f.read() == b'NEW_JPG'


# ============================================================ orphan eviction


def test_evictOrphanLogos_removes_inactive_keeps_active(tmp_path, monkeypatch):
    monkeypatch.setattr('imports.CACHE_LOC', str(tmp_path))
    imp = Imports()
    logos_dir = tmp_path / 'logos'
    logos_dir.mkdir()
    (logos_dir / 'keep1@s1.png').write_bytes(b'A')
    (logos_dir / 'keep2@s1.jpg').write_bytes(b'B')
    (logos_dir / 'remove_me@s1.png').write_bytes(b'C')
    (logos_dir / '.validators.json').write_text('{}')  # leading dot, must be skipped
    removed = imp._evictOrphanLogos({'keep1@s1', 'keep2@s1'})
    assert removed == 1
    assert (logos_dir / 'keep1@s1.png').exists()
    assert (logos_dir / 'keep2@s1.jpg').exists()
    assert not (logos_dir / 'remove_me@s1.png').exists()
    # Hidden file untouched
    assert (logos_dir / '.validators.json').exists()


def test_evictOrphanLogos_safe_when_dir_missing(tmp_path, monkeypatch):
    nested = tmp_path / 'doesnotexist'
    monkeypatch.setattr('imports.CACHE_LOC', str(nested))
    imp = Imports()
    assert imp._evictOrphanLogos({'whatever'}) == 0


# ============================================================ imports.35 — keep files referenced by channels.json


class _FakeChannels:
    """Minimal stand-in for the Channels class — `_evictOrphanLogos` only
    needs `getChannels()`. Imports(channels=None) default is None, so we
    inject a fake explicitly for the imports.35 tests."""
    def __init__(self, channels=None, raise_on_get=False):
        self._channels = channels or []
        self._raise = raise_on_get
    def getChannels(self):
        if self._raise:
            raise RuntimeError("channels layer down")
        return self._channels


def _stub_channels(imp, channels, raise_on_get=False):
    """Helper: attach a fake Channels wrapper that returns controlled records."""
    imp.channels = _FakeChannels(channels=channels, raise_on_get=raise_on_get)


def test_evictOrphanLogos_keeps_operator_upload_referenced_by_chname(tmp_path, monkeypatch):
    """imports.35 — operator-uploaded custom logo (filename = chname like
    'TMNT.png', NO @<import_id> suffix) survives eviction when a channel
    record references it. This is the core bug fix: pre-imports.35
    Custom-channel uploads via switchLogo __browse got wiped on every
    syncAll cycle."""
    monkeypatch.setattr('imports.CACHE_LOC', str(tmp_path))
    imp = Imports()
    logos_dir = tmp_path / 'logos'
    logos_dir.mkdir()
    (logos_dir / 'TMNT.png').write_bytes(b'OPERATOR_UPLOAD')
    (logos_dir / 'TVLand.us@epgbest_46477.png').write_bytes(b'IMPORT_CACHE')
    _stub_channels(imp, [
        {'id': 'abc@PseudoTV_Live',
         'logo': 'special://profile/addon_data/plugin.video.pseudotv.imports/cache/logos/TMNT.png'},
        {'id': 'TVLand.us@epgbest_46477',
         'logo': 'special://profile/addon_data/plugin.video.pseudotv.imports/cache/logos/TVLand.us@epgbest_46477.png'},
    ])
    removed = imp._evictOrphanLogos({'TVLand.us@epgbest_46477'})  # active = imports only
    assert removed == 0, "Both files must survive (one via active set, one via referenced_basenames)"
    assert (logos_dir / 'TMNT.png').exists()
    assert (logos_dir / 'TVLand.us@epgbest_46477.png').exists()


def test_evictOrphanLogos_evicts_truly_orphan_file(tmp_path, monkeypatch):
    """imports.35 — a file in cache/logos/ with no channel reference AND
    no active id match still gets deleted. The new check ADDS protection;
    it doesn't suppress legitimate eviction."""
    monkeypatch.setattr('imports.CACHE_LOC', str(tmp_path))
    imp = Imports()
    logos_dir = tmp_path / 'logos'
    logos_dir.mkdir()
    (logos_dir / 'truly_orphan.png').write_bytes(b'NOBODY_OWNS_ME')
    _stub_channels(imp, [])
    removed = imp._evictOrphanLogos(set())
    assert removed == 1
    assert not (logos_dir / 'truly_orphan.png').exists()


def test_evictOrphanLogos_no_double_count_on_active_AND_referenced(tmp_path, monkeypatch):
    """imports.35 — file whose chid is in active set AND its basename is
    referenced by a channel's logo field gets kept exactly once. Both
    checks are skip-on-match; neither tries to count or unlink twice."""
    monkeypatch.setattr('imports.CACHE_LOC', str(tmp_path))
    imp = Imports()
    logos_dir = tmp_path / 'logos'
    logos_dir.mkdir()
    (logos_dir / 'TVLand.us@epgbest_46477.png').write_bytes(b'IMPORT_CACHE')
    _stub_channels(imp, [
        {'id': 'TVLand.us@epgbest_46477',
         'logo': 'special://profile/addon_data/plugin.video.pseudotv.imports/cache/logos/TVLand.us@epgbest_46477.png'},
    ])
    removed = imp._evictOrphanLogos({'TVLand.us@epgbest_46477'})
    assert removed == 0
    assert (logos_dir / 'TVLand.us@epgbest_46477.png').exists()


def test_evictOrphanLogos_handles_logo_field_outside_cache_dir(tmp_path, monkeypatch):
    """imports.35 — `referenced_basenames` build filters on
    `'cache/logos/' in logo`. A channel record pointing at a logo file
    OUTSIDE cache/logos/ (e.g., `special://home/addons/.../TVLand.png`)
    does NOT add `TVLand.png` to the protected set. A file named
    `TVLand.png` happening to sit in cache/logos/ then gets evicted as
    a true orphan (correct — no record points at it)."""
    monkeypatch.setattr('imports.CACHE_LOC', str(tmp_path))
    imp = Imports()
    logos_dir = tmp_path / 'logos'
    logos_dir.mkdir()
    (logos_dir / 'TVLand.png').write_bytes(b'ORPHAN_IN_CACHE')
    _stub_channels(imp, [
        # Logo references a file OUTSIDE cache/logos/
        {'id': 'TVLand.us@epgbest_46477',
         'logo': 'special://home/addons/resource.images.pseudotv.logos.madteevee/resources/TVLand.png'},
    ])
    removed = imp._evictOrphanLogos(set())
    assert removed == 1
    assert not (logos_dir / 'TVLand.png').exists()


def test_evictOrphanLogos_handles_None_logo_field(tmp_path, monkeypatch):
    """imports.35 — channel record with `'logo': None` (or missing) doesn't
    crash the cross-ref build. `logo or ''` handles None gracefully."""
    monkeypatch.setattr('imports.CACHE_LOC', str(tmp_path))
    imp = Imports()
    logos_dir = tmp_path / 'logos'
    logos_dir.mkdir()
    (logos_dir / 'TMNT.png').write_bytes(b'OPERATOR_UPLOAD')
    _stub_channels(imp, [
        {'id': 'no_logo@PseudoTV_Live', 'logo': None},
        {'id': 'no_logo_key@PseudoTV_Live'},  # no 'logo' key at all
        {'id': 'real@PseudoTV_Live',
         'logo': 'special://profile/addon_data/plugin.video.pseudotv.imports/cache/logos/TMNT.png'},
    ])
    removed = imp._evictOrphanLogos(set())
    assert removed == 0  # TMNT.png protected via referenced_basenames
    assert (logos_dir / 'TMNT.png').exists()


def test_evictOrphanLogos_handles_channels_exception(tmp_path, monkeypatch):
    """imports.35 — if channels.getChannels() raises, the cross-ref build
    falls back to today's behavior (no referenced_basenames, just active
    set). No regression vs pre-imports.35."""
    monkeypatch.setattr('imports.CACHE_LOC', str(tmp_path))
    imp = Imports()
    logos_dir = tmp_path / 'logos'
    logos_dir.mkdir()
    (logos_dir / 'TMNT.png').write_bytes(b'OPERATOR_UPLOAD')

    _stub_channels(imp, None, raise_on_get=True)

    # Without referenced_basenames protection, TMNT.png (not in active set)
    # falls through to eviction — matches pre-imports.35 behavior.
    removed = imp._evictOrphanLogos(set())
    assert removed == 1
    assert not (logos_dir / 'TMNT.png').exists()


def test_evictOrphanLogos_keeps_import_with_active_id_after_imports_35(tmp_path, monkeypatch):
    """imports.35 regression — the existing active_channel_ids skip still
    works after the patch. Empty channels list (cross-ref produces empty
    referenced_basenames). Verify the original behavior is preserved
    when only the active set is in play."""
    monkeypatch.setattr('imports.CACHE_LOC', str(tmp_path))
    imp = Imports()
    logos_dir = tmp_path / 'logos'
    logos_dir.mkdir()
    (logos_dir / 'keep@s1.png').write_bytes(b'A')
    (logos_dir / 'remove@s1.png').write_bytes(b'B')
    _stub_channels(imp, [])  # no records → empty referenced_basenames
    removed = imp._evictOrphanLogos({'keep@s1'})
    assert removed == 1
    assert (logos_dir / 'keep@s1.png').exists()
    assert not (logos_dir / 'remove@s1.png').exists()
