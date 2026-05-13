"""Regression tests for the imports.26 polish-bundle (4 architectural
follow-ups deferred from imports.25 → imports.26).

#4 — Legacy `remotes/imports.html` deletion (302-redirected dead code).
#2 — Live-imports settings promoted to top-level "Live Imports" category.
#1 — Kodi-native "Refresh imports now" button in the settings dialog.
#5 — Per-import "next sync in Nm" countdown display in the dashboard.

Source-scan style mirrors test_imports22 / .23 / .24 / .25. No behavioral
test infra needed — these changes are cosmetic / wiring.

Plan: /home/madalone/.claude/plans/dig-into-c-do-typed-kettle.md (imports.26)
"""
import os
import re


HERE = os.path.dirname(os.path.abspath(__file__))
ADDON_ROOT = os.path.dirname(HERE)
LIB = os.path.join(ADDON_ROOT, 'resources', 'lib')
RESOURCES = os.path.join(ADDON_ROOT, 'resources')
REMOTES = os.path.join(ADDON_ROOT, 'remotes')
LANG_GB = os.path.join(RESOURCES, 'language', 'resource.language.en_gb', 'strings.po')


def _read(path):
    with open(path) as f:
        return f.read()


# ----------------------------------------------------------------------
# #4: imports.html deletion + dead-constant cleanup
# ----------------------------------------------------------------------

def test_legacy_imports_html_file_is_deleted():
    """remotes/imports.html must be removed. The 302-redirect handler at
    server.py:1560-1566 doesn't read the file (just emits a Location
    header), so the file is dead code. Deleted in imports.26."""
    path = os.path.join(REMOTES, 'imports.html')
    assert not os.path.exists(path), (
        "remotes/imports.html still exists — imports.26 cleanup regressed. "
        "The file is 302-redirected dead code; delete it. Stale bookmarks "
        "continue to work via the redirect at server.py:1560-1566."
    )


def test_dead_imports_constants_removed():
    """constants.py must not contain IMPORTSFLE or IMPORTSPATH. Both were
    defined but never referenced elsewhere in the codebase (grep-verified
    pre-imports.26). They became dead constants when imports.html was
    deleted."""
    src = _read(os.path.join(LIB, 'constants.py'))
    # Allow the names in comments (the imports.26 cleanup comment may
    # reference them historically), but no actual definitions.
    code_lines = [ln for ln in src.splitlines() if not ln.lstrip().startswith('#')]
    for needle in ('IMPORTSFLE', 'IMPORTSPATH'):
        bad = [ln for ln in code_lines if needle in ln]
        assert not bad, (
            "constants.py still defines %s — imports.26 cleanup regressed. "
            "Offending line(s): %s" % (needle, bad[:2])
        )


# ----------------------------------------------------------------------
# #2: Live Imports promoted to top-level category
# ----------------------------------------------------------------------

def test_imports_category_exists_at_top_level():
    """settings.xml must have a `<category id="imports" label="33933">`
    block as a top-level category (sibling of `channels`, `options`, etc.)."""
    src = _read(os.path.join(RESOURCES, 'settings.xml'))
    assert re.search(
        r'<category\s+id="imports"\s+label="33933"',
        src,
    ) is not None, (
        "settings.xml missing the new <category id=\"imports\" label=\"33933\"> "
        "block — imports.26 category promotion regressed. Should sit between "
        "<category id=\"channels\"> and <category id=\"options\">."
    )


def test_imports_category_contains_all_moved_settings():
    """The new imports category must contain all 5 originally-moved settings
    plus the new Refresh button. Test guards against accidental dropping
    during a future refactor."""
    src = _read(os.path.join(RESOURCES, 'settings.xml'))
    # Locate the imports category block
    m = re.search(
        r'<category\s+id="imports"[\s\S]*?</category>',
        src,
    )
    assert m is not None, "imports category block not found"
    block = m.group(0)

    expected_settings = (
        'Imports_Sync_Interval_Minutes',
        'Refresh_Imports_Now',
        'Imports_PVR_Scan_After_Sync',
        'Imports_Cache_Logos',
        'Imports_Logo_Refresh',
        'Imports_Library_Walk_Toast',
    )
    for sid in expected_settings:
        assert 'id="%s"' % sid in block, (
            "settings.xml imports category missing setting '%s'. All 6 "
            "settings (5 moved + 1 new Refresh button) must live in the "
            "new top-level imports category." % sid
        )

    # Negative check: the old channels-group-4 should be gone, OR if it
    # still exists, it should NOT contain these imports settings.
    channels_m = re.search(
        r'<category\s+id="channels"[\s\S]*?</category>',
        src,
    )
    if channels_m:
        ch_block = channels_m.group(0)
        for sid in ('Imports_Cache_Logos', 'Imports_Sync_Interval_Minutes'):
            assert 'id="%s"' % sid not in ch_block, (
                "settings.xml channels category still contains '%s' — "
                "imports.26 reorg regressed. The setting should be in the "
                "new imports category only." % sid
            )


def test_imports_category_label_string_exists():
    """strings.po must have #33933 = 'Live Imports' for the new category
    label."""
    src = _read(LANG_GB)
    assert 'msgctxt "#33933"' in src, "strings.po missing #33933 label entry"
    # The msgid is on the line immediately after the msgctxt; quote a span
    # that includes the next line.
    idx = src.find('msgctxt "#33933"')
    span = src[idx:idx + 100]
    assert 'Live Imports' in span, (
        "strings.po #33933 should be 'Live Imports' (matching the existing "
        "33910 group label wording, capitalized for top-level UX)."
    )


# ----------------------------------------------------------------------
# #1: Kodi-native "Refresh imports now" button + Python handler
# ----------------------------------------------------------------------

def test_refresh_imports_action_setting_exists():
    """settings.xml must have a Refresh_Imports_Now action setting with
    a RunScript pointing at utilities.py:Refresh_Imports dispatch."""
    src = _read(os.path.join(RESOURCES, 'settings.xml'))
    m = re.search(
        r'<setting\s+id="Refresh_Imports_Now"\s+type="action"[\s\S]*?</setting>',
        src,
    )
    assert m is not None, (
        "settings.xml missing Refresh_Imports_Now action setting — "
        "imports.26 Kodi-native Refresh button regressed."
    )
    block = m.group(0)

    # Must invoke utilities.py with the Refresh_Imports param
    assert 'utilities.py, Refresh_Imports' in block, (
        "Refresh_Imports_Now action setting must RunScript "
        "utilities.py with the Refresh_Imports param."
    )

    # Must close the settings dialog on click (so the toast is visible)
    assert '<close>true</close>' in block, (
        "Refresh_Imports_Now should set <close>true</close> in its "
        "control so the dialog closes after the click and the operator "
        "sees the 'Refresh queued' toast."
    )

    # Should be level=0 (Basic visibility)
    assert '<level>0</level>' in block, (
        "Refresh_Imports_Now should be level=0 (Basic) — the operator "
        "shouldn't need to unlock 'Advanced' view to see the button."
    )


def test_utilities_py_dispatches_refresh_imports():
    """utilities.py:_run must dispatch param == 'Refresh_Imports' to the
    new _runRefreshImports handler, and the handler must set the
    chkImports.kick property (mirroring server.py:1472)."""
    src = _read(os.path.join(LIB, 'utilities.py'))

    # Dispatch case
    assert re.search(
        r"param\s*==\s*['\"]Refresh_Imports['\"]",
        src,
    ) is not None, (
        "utilities.py:_run is missing the `elif param == 'Refresh_Imports':` "
        "dispatch case — the Kodi-native button would not fire any handler."
    )

    # Handler method exists
    assert re.search(
        r"def _runRefreshImports\s*\(",
        src,
    ) is not None, (
        "utilities.py missing _runRefreshImports method — handler regressed."
    )

    # Handler sets the kick property
    assert re.search(
        r"PROPERTIES\.setEXTProperty\s*\(\s*['\"]chkImports\.kick['\"]\s*,\s*['\"]all['\"]\s*\)",
        src,
    ) is not None, (
        "_runRefreshImports must set chkImports.kick = 'all' to trigger "
        "the daemon's force-sync path (mirrors server.py:1472)."
    )


# ----------------------------------------------------------------------
# #5: dashboard countdown display
# ----------------------------------------------------------------------

def test_manager_html_has_fmtUntil_and_countdown_wiring():
    """manager.html must define a `fmtUntil(epoch)` helper (forward
    counterpart to fmtAge) AND statusBadge() must reference both
    refresh_interval_min and the new helper to render the countdown."""
    src = _read(os.path.join(REMOTES, 'manager.html'))

    # fmtUntil helper exists
    assert 'function fmtUntil(' in src, (
        "manager.html missing fmtUntil(epoch) helper — imports.26 countdown "
        "display wiring regressed."
    )

    # statusBadge() uses it + reads refresh_interval_min
    badge_m = re.search(
        r'function statusBadge\([\s\S]*?\n    \}',
        src,
    )
    assert badge_m is not None, "statusBadge() function not found in manager.html"
    badge_body = badge_m.group(0)

    for needle, why in (
        ('refresh_interval_min', "must read imp.refresh_interval_min"),
        ('fmtUntil(',            "must call fmtUntil() to format the countdown"),
        ('next',                 "must include the 'next ...' string in the rendered HTML"),
    ):
        assert needle in badge_body, (
            "statusBadge() missing %r — %s." % (needle, why)
        )

    # The fmtUntil helper must handle the "due now" / past-epoch case
    until_m = re.search(
        r'function fmtUntil\([\s\S]*?\n    \}',
        src,
    )
    assert until_m is not None, "fmtUntil() body not found"
    until_body = until_m.group(0)
    assert 'due now' in until_body, (
        "fmtUntil() must return 'due now' when secs <= 0 — handles the "
        "rare race where the gate has released but the daemon hasn't fired."
    )
