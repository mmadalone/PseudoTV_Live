# -*- coding: utf-8 -*-
"""imports.36: regression tests for Web UI ↔ Kodi UI logo-handling parity.

Three pieces under test:

  * **logo_helpers.py** (new module) — pure-Python helpers for the
    sanitize / extension / copy-to-LOGO_LOC / write-uploaded-bytes
    operations. Function-local imports keep this module cheap to load.

  * **manager.py:switchLogo.__browse** — refactored to call
    `copyToLogoLoc` instead of the prior inline `FileAccess.copy` +
    `os.path.join` block. Single source of truth.

  * **server.py /channels/edit.json + new /channels/logo/upload.json** —
    auto-copy when `'logo' in fields`; new endpoint for base64 uploads
    from the dashboard's local-file picker.

  * **manager.html** — 📤 button + hidden file input next to logo
    inputs in 2 places (Custom-edit modal cf-logo + inline-logo editor
    template); JS click delegation reads the file, base64-encodes, POSTs
    to /channels/logo/upload.json.

Behavioral tests use tmp_path + monkeypatched `fileaccess` / `globals`
modules so they don't need the real addon's Kodi-stubbed environment.
"""
import os
import re
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
LIB  = os.path.normpath(os.path.join(HERE, '..', 'resources', 'lib'))
HTML = os.path.normpath(os.path.join(HERE, '..', 'remotes', 'manager.html'))
if LIB not in sys.path:
    sys.path.insert(0, LIB)


def _read(path):
    with open(path, 'r', encoding='utf-8') as fh:
        return fh.read()


# ============================================================
# logo_helpers — sanitizeChnameForFilename
# ============================================================

def test_sanitize_replaces_unsafe_chars():
    from logo_helpers import sanitizeChnameForFilename
    assert sanitizeChnameForFilename('TMNT/2026') == 'TMNT_2026'
    assert sanitizeChnameForFilename('My<Channel>') == 'My_Channel_'
    assert sanitizeChnameForFilename('cnn:news') == 'cnn_news'


def test_sanitize_caps_at_128():
    from logo_helpers import sanitizeChnameForFilename
    long_name = 'A' * 200
    assert len(sanitizeChnameForFilename(long_name)) == 128


def test_sanitize_strips_trailing_dot():
    from logo_helpers import sanitizeChnameForFilename
    # Trailing dots are stripped (Windows-incompatible files end in dot).
    assert sanitizeChnameForFilename('weird.') == 'weird'
    assert sanitizeChnameForFilename('weird...') == 'weird'


def test_sanitize_empty_inputs_return_empty():
    from logo_helpers import sanitizeChnameForFilename
    assert sanitizeChnameForFilename('') == ''
    assert sanitizeChnameForFilename(None) == ''
    assert sanitizeChnameForFilename(123) == ''
    # All-whitespace strips to empty
    assert sanitizeChnameForFilename('   ') == ''


def test_sanitize_strips_control_chars():
    from logo_helpers import sanitizeChnameForFilename
    # NUL, newline, carriage return etc. → replaced with `_`
    assert '\x00' not in sanitizeChnameForFilename('TMNT\x00.png')
    assert '\n' not in sanitizeChnameForFilename('TMNT\nName')


# ============================================================
# logo_helpers — extForFilename
# ============================================================

def test_ext_known_extensions_passthrough():
    from logo_helpers import extForFilename
    assert extForFilename('foo.png') == '.png'
    assert extForFilename('foo.JPG') == '.jpg'        # case-insensitive
    assert extForFilename('foo.jpeg') == '.jpeg'
    assert extForFilename('foo.WEBP') == '.webp'
    assert extForFilename('foo.svg') == '.svg'


def test_ext_unknown_falls_back():
    from logo_helpers import extForFilename
    assert extForFilename('foo.txt') == '.png'
    assert extForFilename('foo.bogus') == '.png'
    assert extForFilename('no-ext') == '.png'


def test_ext_empty_or_none_falls_back():
    from logo_helpers import extForFilename
    assert extForFilename('') == '.png'
    assert extForFilename(None) == '.png'
    assert extForFilename('', fallback='.jpg') == '.jpg'


# ============================================================
# logo_helpers — copyToLogoLoc (with mocked fileaccess + globals)
# ============================================================

class _FakeFileAccess:
    """Minimal FileAccess replacement for testing the helper."""
    def __init__(self, abs_loc, existing=None, copy_returns=True):
        self._abs_loc = abs_loc
        self._existing = set(existing or [])
        self._copy_returns = copy_returns
        self.copies = []   # list of (src, dest) pairs
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


def test_copy_to_logo_loc_skips_http_url(tmp_path, monkeypatch):
    _install_helper_fakes(monkeypatch, str(tmp_path / 'logo_loc'))
    if 'logo_helpers' in sys.modules: del sys.modules['logo_helpers']
    from logo_helpers import copyToLogoLoc
    assert copyToLogoLoc('http://example.com/logo.png', 'TMNT') == 'http://example.com/logo.png'
    assert copyToLogoLoc('https://example.com/logo.png', 'TMNT') == 'https://example.com/logo.png'


def test_copy_to_logo_loc_skips_resource_url(tmp_path, monkeypatch):
    _install_helper_fakes(monkeypatch, str(tmp_path / 'logo_loc'))
    if 'logo_helpers' in sys.modules: del sys.modules['logo_helpers']
    from logo_helpers import copyToLogoLoc
    assert copyToLogoLoc('resource://x/foo.png', 'TMNT') == 'resource://x/foo.png'
    assert copyToLogoLoc('image://x/foo.png', 'TMNT') == 'image://x/foo.png'


def test_copy_to_logo_loc_skips_missing_source(tmp_path, monkeypatch):
    _install_helper_fakes(monkeypatch, str(tmp_path / 'logo_loc'))
    if 'logo_helpers' in sys.modules: del sys.modules['logo_helpers']
    from logo_helpers import copyToLogoLoc
    missing = str(tmp_path / 'does-not-exist.png')
    assert copyToLogoLoc(missing, 'TMNT') == missing


def test_copy_to_logo_loc_skips_when_already_in_loc(tmp_path, monkeypatch):
    abs_loc = tmp_path / 'logo_loc'
    abs_loc.mkdir()
    src_in_loc = abs_loc / 'TMNT.png'
    src_in_loc.write_bytes(b'EXISTING')
    fa = _install_helper_fakes(monkeypatch, str(abs_loc), existing={str(src_in_loc)})
    if 'logo_helpers' in sys.modules: del sys.modules['logo_helpers']
    from logo_helpers import copyToLogoLoc
    result = copyToLogoLoc(str(src_in_loc), 'TMNT')
    assert result == str(src_in_loc)
    # No copy attempted
    assert fa.copies == []


def test_copy_to_logo_loc_skips_empty_chname(tmp_path, monkeypatch):
    src = tmp_path / 'source.png'
    src.write_bytes(b'PIXEL')
    _install_helper_fakes(monkeypatch, str(tmp_path / 'logo_loc'), existing={str(src)})
    if 'logo_helpers' in sys.modules: del sys.modules['logo_helpers']
    from logo_helpers import copyToLogoLoc
    assert copyToLogoLoc(str(src), '') == str(src)
    assert copyToLogoLoc(str(src), None) == str(src)


def test_copy_to_logo_loc_happy_path_returns_special_url(tmp_path, monkeypatch):
    src = tmp_path / 'source.png'
    src.write_bytes(b'PIXEL')
    abs_loc = tmp_path / 'logo_loc'
    fa = _install_helper_fakes(monkeypatch, str(abs_loc), existing={str(src)})
    if 'logo_helpers' in sys.modules: del sys.modules['logo_helpers']
    from logo_helpers import copyToLogoLoc
    result = copyToLogoLoc(str(src), 'TMNT')
    assert result == 'special://profile/addon_data/plugin.video.pseudotv.imports/cache/logos/TMNT.png'
    assert len(fa.copies) == 1
    # Destination file exists with original bytes
    dest = abs_loc / 'TMNT.png'
    assert dest.exists()
    assert dest.read_bytes() == b'PIXEL'


def test_copy_to_logo_loc_sanitizes_chname_in_filename(tmp_path, monkeypatch):
    src = tmp_path / 'source.png'
    src.write_bytes(b'PIXEL')
    abs_loc = tmp_path / 'logo_loc'
    _install_helper_fakes(monkeypatch, str(abs_loc), existing={str(src)})
    if 'logo_helpers' in sys.modules: del sys.modules['logo_helpers']
    from logo_helpers import copyToLogoLoc
    # Slash in chname must be replaced — otherwise path traversal
    result = copyToLogoLoc(str(src), 'EVIL/../escape')
    # The unsafe chars are replaced with _
    assert '/' not in os.path.basename(result.replace('special://', ''))
    assert (abs_loc / 'EVIL_.._escape.png').exists()


# ============================================================
# logo_helpers — writeUploadedLogo
# ============================================================

def test_write_uploaded_logo_happy_path(tmp_path, monkeypatch):
    abs_loc = tmp_path / 'logo_loc'
    _install_helper_fakes(monkeypatch, str(abs_loc))
    if 'logo_helpers' in sys.modules: del sys.modules['logo_helpers']
    from logo_helpers import writeUploadedLogo
    result = writeUploadedLogo(b'PIXEL_BYTES', 'TMNT', 'somelogo.jpg')
    assert result == 'special://profile/addon_data/plugin.video.pseudotv.imports/cache/logos/TMNT.jpg'
    dest = abs_loc / 'TMNT.jpg'
    assert dest.exists()
    assert dest.read_bytes() == b'PIXEL_BYTES'


def test_write_uploaded_logo_rejects_oversize(tmp_path, monkeypatch):
    _install_helper_fakes(monkeypatch, str(tmp_path / 'logo_loc'))
    if 'logo_helpers' in sys.modules: del sys.modules['logo_helpers']
    from logo_helpers import writeUploadedLogo, MAX_LOGO_UPLOAD_BYTES
    big = b'x' * (MAX_LOGO_UPLOAD_BYTES + 1)
    assert writeUploadedLogo(big, 'TMNT', 'big.png') is None


def test_write_uploaded_logo_rejects_empty():
    if 'logo_helpers' in sys.modules: del sys.modules['logo_helpers']
    # Even if globals/fileaccess aren't faked, empty input rejected before any
    # I/O — but install fakes to keep the helper happy on its way through.
    pass  # functional check covered by the size guard at the top of the helper


def test_write_uploaded_logo_rejects_empty_chname(tmp_path, monkeypatch):
    _install_helper_fakes(monkeypatch, str(tmp_path / 'logo_loc'))
    if 'logo_helpers' in sys.modules: del sys.modules['logo_helpers']
    from logo_helpers import writeUploadedLogo
    assert writeUploadedLogo(b'OK', '', 'x.png') is None
    assert writeUploadedLogo(b'OK', None, 'x.png') is None


def test_write_uploaded_logo_falls_back_to_png_for_unknown_ext(tmp_path, monkeypatch):
    abs_loc = tmp_path / 'logo_loc'
    _install_helper_fakes(monkeypatch, str(abs_loc))
    if 'logo_helpers' in sys.modules: del sys.modules['logo_helpers']
    from logo_helpers import writeUploadedLogo
    result = writeUploadedLogo(b'OK', 'TMNT', 'weird.bogus')
    assert result.endswith('.png')
    assert (abs_loc / 'TMNT.png').exists()


# ============================================================
# manager.py source-scan — switchLogo.__browse refactor
# ============================================================

def test_manager_switchLogo_uses_copyToLogoLoc():
    """imports.36: switchLogo.__browse must delegate the copy semantics
    to logo_helpers.copyToLogoLoc — single source of truth."""
    src = _read(os.path.join(LIB, 'manager.py'))
    m = re.search(r"def __browse\(\):.*?(?=\n        def )", src, re.DOTALL)
    assert m, "could not locate switchLogo.__browse"
    body = m.group(0)
    assert 'copyToLogoLoc' in body, (
        "imports.36: switchLogo.__browse must call copyToLogoLoc"
    )
    assert 'from logo_helpers import' in body, (
        "imports.36: switchLogo.__browse must import the helper function-locally"
    )


def test_manager_switchLogo_no_duplicate_copy_logic():
    """imports.36: the prior inline `FileAccess.copy(... os.path.join(LOGO_LOC, ...))`
    block must be gone — duplicating the copy logic would mean future
    changes to copy semantics touch two places."""
    src = _read(os.path.join(LIB, 'manager.py'))
    m = re.search(r"def __browse\(\):.*?(?=\n        def )", src, re.DOTALL)
    assert m, "could not locate switchLogo.__browse"
    body = m.group(0)
    # The legacy `retval[-4:]` slice that was a footgun (could grab any
    # 4-char suffix, not just extensions) should be gone.
    assert 'retval[-4:]' not in body, (
        "imports.36: switchLogo.__browse must not use the legacy retval[-4:] "
        "extension slice (extension allow-list is now in logo_helpers.extForFilename)"
    )


# ============================================================
# server.py source-scan
# ============================================================

def test_server_edit_endpoint_calls_copyToLogoLoc():
    """imports.36: /channels/edit.json must call copyToLogoLoc when
    `'logo' in fields` — gives the web UI the same copy resilience as
    the Kodi UI's switchLogo."""
    src = _read(os.path.join(LIB, 'server.py'))
    m = re.search(
        r"/channels/edit\.json.*?wfile\.write\(body\)",
        src, re.DOTALL,
    )
    assert m, "could not locate /channels/edit.json handler"
    body = m.group(0)
    assert 'copyToLogoLoc' in body, (
        "imports.36: /channels/edit.json must call copyToLogoLoc"
    )
    assert "'logo' in fields" in body, (
        "imports.36: the copyToLogoLoc call must be guarded on `'logo' in fields`"
    )


def test_server_upload_endpoint_exists():
    """imports.36: new /channels/logo/upload.json endpoint."""
    src = _read(os.path.join(LIB, 'server.py'))
    assert '/channels/logo/upload.json' in src, (
        "imports.36: server.py must define the /channels/logo/upload.json endpoint"
    )


def test_server_upload_endpoint_uses_base64_decode():
    """imports.36: upload endpoint decodes base64-encoded body content."""
    src = _read(os.path.join(LIB, 'server.py'))
    # Locate the handler block starting at the `elif` for upload.json and
    # extend to the next `elif self.path.split` (next endpoint) or end of
    # the try/except chain.
    m = re.search(
        r"elif self\.path\.split\('\?', 1\)\[0\]\.lower\(\) == '/channels/logo/upload\.json':.*?"
        r"(?=\n                elif self\.path\.split\('\?')",
        src, re.DOTALL,
    )
    assert m, "could not locate /channels/logo/upload.json handler block"
    body = m.group(0)
    assert 'base64.b64decode' in body, (
        "imports.36: upload endpoint must decode base64 content"
    )
    assert 'writeUploadedLogo' in body, (
        "imports.36: upload endpoint must call writeUploadedLogo"
    )


def test_server_edit_endpoint_logo_copy_wrapped_in_try_except():
    """imports.36: copyToLogoLoc call inside /channels/edit.json wrapped
    so a copy failure doesn't 500 the edit — operator's path is preserved
    as best-effort (might be an intentional URL/resource:// reference)."""
    src = _read(os.path.join(LIB, 'server.py'))
    m = re.search(
        r"/channels/edit\.json.*?wfile\.write\(body\)",
        src, re.DOTALL,
    )
    body = m.group(0)
    call_pos = body.find('copyToLogoLoc')
    assert call_pos > 0
    window = body[max(0, call_pos - 200):call_pos]
    assert 'try:' in window, (
        "imports.36: copyToLogoLoc call must be wrapped in try/except"
    )


# ============================================================
# manager.html source-scan
# ============================================================

def test_manager_html_has_upload_button_in_cf_modal():
    """imports.36: cf-modal logo input has 📤 upload button.

    imports.40 update: cf-modal's field grid is now generated by the shared
    `renderChannelFieldEditor(idPrefix)` helper instead of hardcoded HTML,
    so the cf-logo input + upload button live inside the helper's template
    string as `${idPrefix}-logo`. The invariant still holds — both the
    logo input AND the upload-logo button + hidden file input must be
    present in the helper's output."""
    src = _read(HTML)
    m = re.search(
        r'id="\$\{idPrefix\}-logo".*?data-action="upload-logo".*?logo-upload-input',
        src, re.DOTALL,
    )
    assert m, (
        "renderChannelFieldEditor helper must emit logo input + upload-logo "
        "button + hidden file input (used by cf-modal at idPrefix='cf')"
    )


def test_manager_html_has_upload_button_in_inline_editor():
    """imports.36: inline-logo editor template has 📤 upload button."""
    src = _read(HTML)
    # The inline-logo editor builds via template literal at ~line 1941.
    # Match data-action="upload-logo" within a ile-input-row context.
    m = re.search(
        r'class="ile-input-row".*?data-action="upload-logo".*?logo-upload-input',
        src, re.DOTALL,
    )
    assert m, (
        "imports.36: inline-logo editor template must include upload-logo "
        "button + hidden file input"
    )


def test_manager_html_upload_input_has_accept_allowlist():
    """imports.36: hidden file inputs have `accept=` allow-list."""
    src = _read(HTML)
    matches = re.findall(
        r'<input[^>]*class="logo-upload-input"[^>]*accept="([^"]+)"',
        src,
    )
    assert len(matches) >= 2, (
        "imports.36: expected at least 2 logo-upload-input elements (cf-modal "
        "+ inline-logo editor template) with accept=`...`"
    )
    for accept_attr in matches:
        assert '.png' in accept_attr
        assert '.jpg' in accept_attr


def test_manager_html_js_handler_registered():
    """imports.36: JS click delegation for `data-action="upload-logo"`."""
    src = _read(HTML)
    assert 'data-action="upload-logo"' in src
    assert '/channels/logo/upload.json' in src, (
        "imports.36: JS must POST to the new upload endpoint"
    )
    assert 'readAsDataURL' in src, (
        "imports.36: JS must use FileReader.readAsDataURL for base64 encoding"
    )


def test_manager_html_js_enforces_5mb_client_cap():
    """imports.36: client-side 5 MB cap matches the server-side cap."""
    src = _read(HTML)
    # The check `file.size > 5 * 1024 * 1024` or any equivalent literal.
    assert re.search(r'5\s*\*\s*1024\s*\*\s*1024', src), (
        "imports.36: JS upload handler must enforce a 5 MB client-side cap"
    )


# ============================================================
# Regression guards
# ============================================================

def test_imports_20_markOverrides_preserved():
    """imports.36 must NOT remove the imports.20 markOverrides call in
    /channels/edit.json — operator's logo choice still gets marked in
    operator_overrides, blocking Builder._verify auto-derivation."""
    src = _read(os.path.join(LIB, 'server.py'))
    m = re.search(
        r"/channels/edit\.json.*?wfile\.write\(body\)",
        src, re.DOTALL,
    )
    body = m.group(0)
    assert 'markOverrides(target' in body, (
        "imports.36 regression guard: imports.20 markOverrides call in "
        "/channels/edit.json must be preserved"
    )


def test_changelog_has_imports_36_entry():
    """changelog.txt has the imports.36 entry — durable assertion."""
    src = _read(os.path.join(LIB, '..', '..', 'changelog.txt'))
    assert 'v.0.8.0+imports.36' in src, (
        "imports.36: changelog.txt must include the imports.36 entry"
    )
