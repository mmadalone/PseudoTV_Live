# -*- coding: utf-8 -*-
"""imports.35: source-scan regression tests for the `_evictOrphanLogos`
referenced-basenames protection.

Bug: operator-uploaded custom logos (added via manager.py:switchLogo.__browse,
filename = `<chname>.<ext>` like `cache/logos/TMNT.png`) were deleted by
`Imports.syncAll`'s post-cycle `_evictOrphanLogos` call, which only knew
about IMPORT channel ids in its "keep" set. Net effect: every syncAll
cycle wiped operator-uploaded Custom-channel logos. imports.34's fast-
path made the timing visible.

Fix: `_evictOrphanLogos` now also cross-references `channels.json` logo
fields and preserves any file currently referenced.

Behavioral coverage lives in `tests/test_imports_logo_cache.py` (extends
the existing eviction tests). This file holds the static source-scan
guards that prevent future regression of the fix's structure.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LIB  = os.path.normpath(os.path.join(HERE, '..', 'resources', 'lib'))
if LIB not in sys.path:
    sys.path.insert(0, LIB)


def _read(path):
    with open(path, 'r', encoding='utf-8') as fh:
        return fh.read()


def _evict_body():
    """Return just the body of `_evictOrphanLogos` so subsequent grep
    checks can't accidentally match content in adjacent functions."""
    src = _read(os.path.join(LIB, 'imports.py'))
    m = re.search(
        r"def _evictOrphanLogos\(.*?(?=\n    def )",
        src, re.DOTALL,
    )
    assert m, "could not locate _evictOrphanLogos in imports.py"
    return m.group(0)


def test_evictOrphanLogos_carries_imports_35_marker():
    """Grep marker so future archaeologists locate the rationale."""
    body = _evict_body()
    assert 'imports.35' in body, (
        "imports.35: _evictOrphanLogos must carry the imports.35 grep marker"
    )


def test_evictOrphanLogos_builds_referenced_basenames_from_channels():
    """The fix builds a `referenced_basenames` set from
    `self.channels.getChannels()` — this is the cross-reference that
    catches operator-uploaded customs (filename = chname, not chid)."""
    body = _evict_body()
    assert 'referenced_basenames' in body, (
        "imports.35: _evictOrphanLogos must build a `referenced_basenames` set"
    )
    assert 'self.channels.getChannels()' in body, (
        "imports.35: _evictOrphanLogos must read `self.channels.getChannels()` "
        "to build the cross-reference"
    )
    assert 'os.path.basename' in body, (
        "imports.35: _evictOrphanLogos must use os.path.basename to extract "
        "filenames from full logo paths"
    )


def test_evictOrphanLogos_filters_logos_by_cache_dir_path():
    """The cross-ref must only protect files that ACTUALLY live in
    cache/logos/. Logos pointing elsewhere (e.g.,
    `special://home/addons/.../TVLand.png`) must NOT add their basename
    to the protected set — those files aren't in our cache directory
    so the basename match would be coincidental."""
    body = _evict_body()
    assert "'cache/logos/' in logo" in body, (
        "imports.35: _evictOrphanLogos must filter referenced_basenames "
        "on `'cache/logos/' in logo` so paths outside cache/logos/ don't "
        "spuriously protect arbitrary files"
    )


def test_evictOrphanLogos_skips_when_in_referenced_basenames():
    """The eviction loop must consult `referenced_basenames` before
    unlinking — this is the actual fix."""
    body = _evict_body()
    assert 'name in referenced_basenames' in body, (
        "imports.35: the eviction loop must check `name in referenced_basenames` "
        "and skip the unlink when true"
    )


def test_evictOrphanLogos_preserves_active_id_check():
    """imports.35 must preserve the existing active_channel_ids skip —
    import cache files for active import ids still get kept."""
    body = _evict_body()
    assert 'cid in active' in body or 'cid not in active' in body, (
        "imports.35 regression guard: _evictOrphanLogos must still check "
        "the active_channel_ids set"
    )


def test_evictOrphanLogos_preserves_dotfile_skip():
    """imports.35 must preserve the dotfile + .tmp skip (protects
    `.validators.json` etc.)."""
    body = _evict_body()
    assert "name.startswith('.')" in body, (
        "imports.35 regression guard: _evictOrphanLogos must still skip "
        "dotfiles"
    )
    assert "name.endswith('.tmp')" in body, (
        "imports.35 regression guard: _evictOrphanLogos must still skip "
        ".tmp staging files"
    )


def test_evictOrphanLogos_channels_cross_ref_wrapped_in_try_except():
    """A `channels.getChannels()` failure must NOT abort eviction — the
    cross-ref build is wrapped in try/except so a channels-layer
    exception falls back to today's behavior (no protection added, but
    eviction proceeds for the active_id-vs-stem check)."""
    body = _evict_body()
    # The try/except wrapping the cross-ref build
    assert re.search(
        r"try:\s*\n\s*for ch in \(self\.channels\.getChannels\(\) or \[\]\):",
        body,
    ), (
        "imports.35: the referenced_basenames build (the `for ch in "
        "self.channels.getChannels()` loop) must be wrapped in try/except"
    )


def test_changelog_has_imports_35_entry():
    """changelog.txt must include the imports.35 entry — durable
    assertion across future cycle bumps (not pinned to addon.xml
    version string, which would break on the next cycle)."""
    src = _read(os.path.join(LIB, '..', '..', 'changelog.txt'))
    assert 'v.0.8.0+imports.35' in src, (
        "imports.35: changelog.txt must include the imports.35 entry"
    )
