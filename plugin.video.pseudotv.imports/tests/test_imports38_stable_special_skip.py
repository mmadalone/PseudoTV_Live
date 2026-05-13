# -*- coding: utf-8 -*-
"""imports.38: regression tests for the `copyToLogoLoc` stable-special://
pass-through.

Pre-imports.38, picking a logo from a stable Kodi-managed location
(e.g., `special://home/addons/<logo-pack>/resources/logo.png`) caused
`copyToLogoLoc` to copy the file into `cache/logos/<chname>.<ext>` —
renaming it on the way. Two issues: wasted disk + a duplicate, and the
destination filename inherited the channel name (which could contain
spaces, breaking downstream Kodi image cache binding).

Fix: new `STABLE_SPECIAL_PREFIXES` tuple at module top + a new skip
block in `copyToLogoLoc` that returns `source_path` verbatim when it
starts with any of those prefixes. `special://temp/` is intentionally
NOT included — temp paths are genuinely transient and should be copied
for resilience.
"""
import os
import re
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
LIB  = os.path.normpath(os.path.join(HERE, '..', 'resources', 'lib'))
if LIB not in sys.path:
    sys.path.insert(0, LIB)


def _read(path):
    with open(path, 'r', encoding='utf-8') as fh:
        return fh.read()


# ============================================================
# Test scaffolding (mirrors imports.36 test pattern)
# ============================================================

class _FakeFileAccess:
    def __init__(self, abs_loc, existing=None, copy_returns=True):
        self._abs_loc = abs_loc
        self._existing = set(existing or [])
        self._copy_returns = copy_returns
        self.copies = []
    def translatePath(self, path):
        if path == 'special://logo_loc':
            return self._abs_loc
        if path.startswith('special://'):
            return path.replace('special://profile', '/fake/profile').replace('special://home', '/fake/home')
        return path
    def exists(self, path):
        return path in self._existing or (os.path.exists(path) if path.startswith('/') else False)
    def copy(self, src, dest):
        self.copies.append((src, dest))
        if self._copy_returns:
            try:
                import shutil
                shutil.copy(src, dest)
            except Exception:
                pass
        return self._copy_returns


def _install_helper_fakes(monkeypatch, abs_loc, existing=None, copy_returns=True):
    fake_fa = _FakeFileAccess(abs_loc, existing=existing, copy_returns=copy_returns)
    fa_mod = types.ModuleType('fileaccess')
    fa_mod.FileAccess = fake_fa
    monkeypatch.setitem(sys.modules, 'fileaccess', fa_mod)

    g_mod = types.ModuleType('globals')
    g_mod.LOGO_LOC = 'special://logo_loc'
    g_mod.ADDON_ID = 'plugin.video.pseudotv.imports'
    monkeypatch.setitem(sys.modules, 'globals', g_mod)
    return fake_fa


# ============================================================
# Source-scan
# ============================================================

def test_stable_special_prefixes_constant_defined():
    """imports.38: module top defines `STABLE_SPECIAL_PREFIXES` as a tuple."""
    src = _read(os.path.join(LIB, 'logo_helpers.py'))
    assert re.search(
        r"^STABLE_SPECIAL_PREFIXES\s*=\s*\(",
        src, re.MULTILINE,
    ), (
        "imports.38: STABLE_SPECIAL_PREFIXES must be defined at module top as a tuple"
    )


def test_stable_special_prefixes_includes_required_namespaces():
    """imports.38: tuple includes the four documented Kodi-managed
    stable namespaces (home / xbmc / profile / userdata) at minimum."""
    from logo_helpers import STABLE_SPECIAL_PREFIXES
    required = ('special://home/', 'special://xbmc/', 'special://profile/', 'special://userdata/')
    for prefix in required:
        assert prefix in STABLE_SPECIAL_PREFIXES, (
            "imports.38: STABLE_SPECIAL_PREFIXES must include %r" % prefix
        )


def test_stable_special_prefixes_excludes_temp():
    """imports.38 regression guard: special://temp/ must NOT be in the
    stable list — temp paths are genuinely transient and should be
    copied for resilience."""
    from logo_helpers import STABLE_SPECIAL_PREFIXES
    assert 'special://temp/' not in STABLE_SPECIAL_PREFIXES, (
        "imports.38: STABLE_SPECIAL_PREFIXES must NOT include special://temp/ "
        "— temp dir is transient, files there should be copied"
    )


def test_copy_to_logo_loc_uses_startswith_check():
    """imports.38: `copyToLogoLoc` body uses
    `source_path.startswith(STABLE_SPECIAL_PREFIXES)`."""
    src = _read(os.path.join(LIB, 'logo_helpers.py'))
    # Locate the copyToLogoLoc function body
    m = re.search(
        r"def copyToLogoLoc\(.*?(?=^def |\Z)",
        src, re.DOTALL | re.MULTILINE,
    )
    assert m, "could not locate copyToLogoLoc"
    body = m.group(0)
    assert 'source_path.startswith(STABLE_SPECIAL_PREFIXES)' in body, (
        "imports.38: copyToLogoLoc must check "
        "`source_path.startswith(STABLE_SPECIAL_PREFIXES)`"
    )


def test_imports_38_grep_marker_present():
    """Grep marker for future archaeology."""
    src = _read(os.path.join(LIB, 'logo_helpers.py'))
    assert 'imports.38' in src, (
        "imports.38: logo_helpers.py must carry the imports.38 grep marker"
    )


def test_existing_skip_rules_preserved():
    """imports.38 regression: imports.36 existing skip clauses
    (http/https/resource/image, LOGO_LOC, missing-source) all still
    present and unchanged."""
    src = _read(os.path.join(LIB, 'logo_helpers.py'))
    m = re.search(
        r"def copyToLogoLoc\(.*?(?=^def |\Z)",
        src, re.DOTALL | re.MULTILINE,
    )
    body = m.group(0)
    assert "source_path.startswith(('http://', 'https://', 'resource://', 'image://'))" in body, (
        "imports.36 URL/resource/image skip must be preserved"
    )
    assert 'abs_src.startswith(abs_loc)' in body, (
        "imports.36 LOGO_LOC skip must be preserved"
    )
    assert 'FileAccess.exists(source_path)' in body, (
        "imports.36 missing-source skip must be preserved"
    )


def test_changelog_has_imports_38_entry():
    """changelog.txt has the imports.38 entry — durable assertion."""
    src = _read(os.path.join(LIB, '..', '..', 'changelog.txt'))
    assert 'v.0.8.0+imports.38' in src, (
        "imports.38: changelog.txt must include the imports.38 entry"
    )


# ============================================================
# Unit / behavioral (with mocked fileaccess + globals)
# ============================================================

def test_special_home_addons_path_returned_verbatim(tmp_path, monkeypatch):
    """imports.38: special://home/addons/<addon>/resources/logo.png
    is returned verbatim (no copy attempted). This is the exact scenario
    the operator hit during imports.37 live verify."""
    fa = _install_helper_fakes(monkeypatch, str(tmp_path / 'logo_loc'))
    if 'logo_helpers' in sys.modules: del sys.modules['logo_helpers']
    from logo_helpers import copyToLogoLoc
    src_path = 'special://home/addons/resource.images.pseudotv.logos.madteevee/resources/TMNT.png'
    result = copyToLogoLoc(src_path, 'TMNT 2026')
    assert result == src_path, (
        "imports.38: special://home/... paths must pass through unchanged"
    )
    assert fa.copies == [], "no copy should have been attempted"


def test_special_xbmc_path_returned_verbatim(tmp_path, monkeypatch):
    fa = _install_helper_fakes(monkeypatch, str(tmp_path / 'logo_loc'))
    if 'logo_helpers' in sys.modules: del sys.modules['logo_helpers']
    from logo_helpers import copyToLogoLoc
    src_path = 'special://xbmc/media/icons/some-icon.png'
    result = copyToLogoLoc(src_path, 'TMNT')
    assert result == src_path
    assert fa.copies == []


def test_special_profile_path_returned_verbatim(tmp_path, monkeypatch):
    fa = _install_helper_fakes(monkeypatch, str(tmp_path / 'logo_loc'))
    if 'logo_helpers' in sys.modules: del sys.modules['logo_helpers']
    from logo_helpers import copyToLogoLoc
    src_path = 'special://profile/icons/foo.png'
    result = copyToLogoLoc(src_path, 'TMNT')
    assert result == src_path
    assert fa.copies == []


def test_special_userdata_path_returned_verbatim(tmp_path, monkeypatch):
    fa = _install_helper_fakes(monkeypatch, str(tmp_path / 'logo_loc'))
    if 'logo_helpers' in sys.modules: del sys.modules['logo_helpers']
    from logo_helpers import copyToLogoLoc
    src_path = 'special://userdata/sources.png'
    result = copyToLogoLoc(src_path, 'TMNT')
    assert result == src_path
    assert fa.copies == []


def test_special_masterprofile_path_returned_verbatim(tmp_path, monkeypatch):
    fa = _install_helper_fakes(monkeypatch, str(tmp_path / 'logo_loc'))
    if 'logo_helpers' in sys.modules: del sys.modules['logo_helpers']
    from logo_helpers import copyToLogoLoc
    src_path = 'special://masterprofile/icons/foo.png'
    result = copyToLogoLoc(src_path, 'TMNT')
    assert result == src_path
    assert fa.copies == []


def test_special_temp_path_NOT_skipped(tmp_path, monkeypatch):
    """imports.38: special://temp/ paths must NOT be in the stable
    skip list. They should proceed through the rest of copyToLogoLoc's
    logic (which will then hit the FileAccess.exists check OR copy)."""
    src_file = tmp_path / 'temp_logo.png'
    src_file.write_bytes(b'TEMP_LOGO_BYTES')
    abs_loc = tmp_path / 'logo_loc'
    fake_fa = _FakeFileAccess(str(abs_loc), existing={'special://temp/temp_logo.png', str(src_file)})
    # Make translatePath('special://temp/temp_logo.png') resolve to the actual src_file
    def _translate(path):
        if path == 'special://logo_loc':
            return str(abs_loc)
        if path == 'special://temp/temp_logo.png':
            return str(src_file)
        return path
    fake_fa.translatePath = _translate
    fa_mod = types.ModuleType('fileaccess')
    fa_mod.FileAccess = fake_fa
    monkeypatch.setitem(sys.modules, 'fileaccess', fa_mod)
    g_mod = types.ModuleType('globals')
    g_mod.LOGO_LOC = 'special://logo_loc'
    g_mod.ADDON_ID = 'plugin.video.pseudotv.imports'
    monkeypatch.setitem(sys.modules, 'globals', g_mod)
    if 'logo_helpers' in sys.modules: del sys.modules['logo_helpers']
    from logo_helpers import copyToLogoLoc
    result = copyToLogoLoc('special://temp/temp_logo.png', 'TMNT')
    # special://temp/ NOT in stable list, source exists → copy happens
    assert result.startswith('special://profile/addon_data/'), (
        "special://temp/ paths should NOT be skipped — they should be "
        "copied into LOGO_LOC (transient source needs resilience)"
    )
    assert len(fake_fa.copies) == 1, (
        "copy must actually happen for special://temp/ sources"
    )


def test_raw_filesystem_path_still_copied(tmp_path, monkeypatch):
    """imports.38 regression: raw filesystem paths (/mnt/, /media/,
    /tmp/, /home/user/) must still be copied — they don't match any
    skip rule."""
    src_file = tmp_path / 'usb_logo.png'
    src_file.write_bytes(b'USB_LOGO')
    abs_loc = tmp_path / 'logo_loc'
    fa = _install_helper_fakes(monkeypatch, str(abs_loc), existing={str(src_file)})
    if 'logo_helpers' in sys.modules: del sys.modules['logo_helpers']
    from logo_helpers import copyToLogoLoc
    result = copyToLogoLoc(str(src_file), 'TMNT')
    assert result.startswith('special://profile/addon_data/'), (
        "raw filesystem paths should still copy + return special:// path"
    )
    assert len(fa.copies) == 1


def test_existing_http_skip_preserved(tmp_path, monkeypatch):
    """imports.38 regression: imports.36 http/https/resource/image
    pass-through preserved verbatim."""
    fa = _install_helper_fakes(monkeypatch, str(tmp_path / 'logo_loc'))
    if 'logo_helpers' in sys.modules: del sys.modules['logo_helpers']
    from logo_helpers import copyToLogoLoc
    for src in (
        'http://example.com/foo.png',
        'https://example.com/foo.png',
        'resource://x/foo.png',
        'image://x/foo.png',
    ):
        assert copyToLogoLoc(src, 'TMNT') == src, (
            "%r should still pass through unchanged (imports.36 rule)" % src
        )
    assert fa.copies == []


def test_existing_logo_loc_skip_preserved(tmp_path, monkeypatch):
    """imports.38 regression: files already inside LOGO_LOC still
    pass through (imports.36 idempotent skip)."""
    abs_loc = tmp_path / 'logo_loc'
    abs_loc.mkdir()
    inside_file = abs_loc / 'TMNT.png'
    inside_file.write_bytes(b'EXISTING')
    # Use a special://profile path that resolves into abs_loc — and
    # NOT into one of the stable special://home/xbmc/userdata/master*
    # prefixes (those would short-circuit before the LOGO_LOC check).
    # But special://profile/ IS now in STABLE_SPECIAL_PREFIXES via
    # imports.38, so this path falls into the new skip too. Either way,
    # the result is "passed through verbatim, no copy" — the operator-
    # visible behavior. The TEST asserts the no-copy invariant, not
    # which specific clause caught it.
    fake_fa = _FakeFileAccess(str(abs_loc), existing={str(inside_file)})
    # Map special path → abs path
    def _translate(p):
        if p == 'special://logo_loc':
            return str(abs_loc)
        if p == 'special://logo_loc/TMNT.png':
            return str(inside_file)
        if p.startswith('special://'):
            return p.replace('special://', '/fake/')
        return p
    fake_fa.translatePath = _translate
    fa_mod = types.ModuleType('fileaccess')
    fa_mod.FileAccess = fake_fa
    monkeypatch.setitem(sys.modules, 'fileaccess', fa_mod)
    g_mod = types.ModuleType('globals')
    g_mod.LOGO_LOC = 'special://logo_loc'
    g_mod.ADDON_ID = 'plugin.video.pseudotv.imports'
    monkeypatch.setitem(sys.modules, 'globals', g_mod)
    if 'logo_helpers' in sys.modules: del sys.modules['logo_helpers']
    from logo_helpers import copyToLogoLoc
    result = copyToLogoLoc('special://logo_loc/TMNT.png', 'TMNT')
    # The source path is returned unchanged. No copy.
    assert result == 'special://logo_loc/TMNT.png'
    assert fake_fa.copies == []


def test_writeUploadedLogo_unaffected(tmp_path, monkeypatch):
    """imports.38 regression: `writeUploadedLogo` doesn't have a source
    path to skip — uploaded bytes always land in LOGO_LOC. Unaffected
    by the imports.38 skip-rule change."""
    abs_loc = tmp_path / 'logo_loc'
    _install_helper_fakes(monkeypatch, str(abs_loc))
    if 'logo_helpers' in sys.modules: del sys.modules['logo_helpers']
    from logo_helpers import writeUploadedLogo
    result = writeUploadedLogo(b'PIXEL', 'TMNT', 'src.png')
    # Still writes into LOGO_LOC with chname-based filename — same as imports.36.
    assert result == 'special://profile/addon_data/plugin.video.pseudotv.imports/cache/logos/TMNT.png'
    assert (abs_loc / 'TMNT.png').exists()
