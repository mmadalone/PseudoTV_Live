"""Regression tests for imports.43 settings.xml + strings.po exposure.

imports.43 ships two operator-visible settings in resources/settings.xml,
both `type="integer"` enums mirroring the imports.28 RETRY_CURVE_BY_CODE
pattern:

  - `Imports_Boot_Strategy`         — 0=disk_presence (default) / 1=eager_render / 2=force_sync
  - `Imports_Disk_Presence_Scope`   — 0=main_m3u (default) / 1=per_import_epg / 2=both

Each setting's option labels are stored as strings.po entries in the
33950-33959 range. The dashboard's Settings tab (manager.html) reads
settings.xml directly via the existing form generator (imports.40 / Step 6
precedent), so these settings auto-appear there with no JavaScript change.

Source-scan style mirrors test_imports22/.23/.27/.28 — read settings.xml
and strings.po as text and assert the expected XML structures + string
IDs are present. Catches accidental removal / renumbering / default-value
regression.

Plan: /home/madalone/.claude/plans/declarative-stirring-rainbow.md
"""
import os
import re


HERE = os.path.dirname(os.path.abspath(__file__))
ADDON_ROOT = os.path.dirname(HERE)
RESOURCES = os.path.join(ADDON_ROOT, 'resources')
SETTINGS_XML = os.path.join(RESOURCES, 'settings.xml')
STRINGS_PO = os.path.join(RESOURCES, 'language', 'resource.language.en_gb', 'strings.po')


def _read(path):
    with open(path) as f:
        return f.read()


def test_settings_xml_has_imports_boot_strategy_setting():
    """resources/settings.xml must contain a <setting id="Imports_Boot_Strategy">
    block. Catches accidental removal."""
    src = _read(SETTINGS_XML)
    assert 'id="Imports_Boot_Strategy"' in src, (
        "settings.xml missing `Imports_Boot_Strategy` setting — imports.43 "
        "operator knob for boot-time render strategy disappeared.")


def test_settings_xml_imports_boot_strategy_is_integer_with_three_options():
    """Setting must be type="integer" with three <option> children
    (codes 0/1/2). Matches imports.28 RETRY_CURVE_BY_CODE pattern."""
    src = _read(SETTINGS_XML)
    # Extract just this setting's <setting>...</setting> block
    m = re.search(r'<setting id="Imports_Boot_Strategy"[^>]*>(.*?)</setting>',
                  src, re.DOTALL)
    assert m is not None, "Imports_Boot_Strategy <setting> block not parseable"
    body = m.group(0)
    assert 'type="integer"' in body, (
        "Imports_Boot_Strategy must be type=\"integer\" (codes mapped to "
        "strings via BOOT_STRATEGY_BY_CODE in imports.py, mirroring "
        "imports.28's Imports_Failure_Retry_Curve pattern).")
    # Three options for the three strategies
    options = re.findall(r'<option label="\d+">(\d+)</option>', body)
    assert sorted(options) == ['0', '1', '2'], (
        "Imports_Boot_Strategy must expose exactly three options (0/1/2 "
        "→ disk_presence/eager_render/force_sync); got: %s" % options)


def test_settings_xml_imports_boot_strategy_default_is_disk_presence():
    """Default value MUST be 0 (disk_presence) — the least-invasive option,
    preserving pre-imports.43 boot-path behavior for unmodified operator
    installs."""
    src = _read(SETTINGS_XML)
    m = re.search(r'<setting id="Imports_Boot_Strategy"[^>]*>(.*?)</setting>',
                  src, re.DOTALL)
    assert m is not None
    body = m.group(0)
    assert '<default>0</default>' in body, (
        "Imports_Boot_Strategy default must be 0 (disk_presence) — any "
        "other default would change boot behavior for every existing "
        "operator on upgrade, surprising them.")


def test_settings_xml_has_imports_disk_presence_scope_setting():
    """resources/settings.xml must contain a <setting id="Imports_Disk_Presence_Scope">
    block."""
    src = _read(SETTINGS_XML)
    assert 'id="Imports_Disk_Presence_Scope"' in src, (
        "settings.xml missing `Imports_Disk_Presence_Scope` setting — "
        "imports.43 operator knob for gate disk-presence check granularity "
        "disappeared.")


def test_settings_xml_imports_disk_presence_scope_is_integer_with_three_options():
    """Setting must be type="integer" with three <option> children
    (codes 0/1/2)."""
    src = _read(SETTINGS_XML)
    m = re.search(r'<setting id="Imports_Disk_Presence_Scope"[^>]*>(.*?)</setting>',
                  src, re.DOTALL)
    assert m is not None, "Imports_Disk_Presence_Scope <setting> block not parseable"
    body = m.group(0)
    assert 'type="integer"' in body
    options = re.findall(r'<option label="\d+">(\d+)</option>', body)
    assert sorted(options) == ['0', '1', '2'], (
        "Imports_Disk_Presence_Scope must expose exactly three options "
        "(0/1/2 → main_m3u/per_import_epg/both); got: %s" % options)


def test_settings_xml_imports_disk_presence_scope_default_is_main_m3u():
    """Default value MUST be 0 (main_m3u) — the cheapest check (one stat
    per cycle) and the case that actually fired in the imports.40 cleanup
    incident operators are likely to hit."""
    src = _read(SETTINGS_XML)
    m = re.search(r'<setting id="Imports_Disk_Presence_Scope"[^>]*>(.*?)</setting>',
                  src, re.DOTALL)
    assert m is not None
    body = m.group(0)
    assert '<default>0</default>' in body, (
        "Imports_Disk_Presence_Scope default must be 0 (main_m3u) — single "
        "stat call per gate cycle, catches the actual post-cleanup case.")


def test_strings_po_has_imports43_string_ids():
    """The 10 imports.43 string IDs (#33950-#33959) must be present in
    strings.po with non-empty msgid values. Catches accidental deletion
    or renumbering when subsequent imports.NN cycles allocate new IDs."""
    src = _read(STRINGS_PO)
    expected_ids = [
        '#33950',  # "Live imports — boot strategy"
        '#33951',  # help for boot strategy
        '#33952',  # "Disk-presence check only"
        '#33953',  # "Eager render from channels.json (no HTTP)"
        '#33954',  # "Force full sync (HTTP at every boot)"
        '#33955',  # "Live imports — disk-presence check scope"
        '#33956',  # help for disk-presence scope
        '#33957',  # "Main M3U file only"
        '#33958',  # "Per-import EPG cache only"
        '#33959',  # "Both main M3U and per-import EPG"
    ]
    for sid in expected_ids:
        assert 'msgctxt "%s"' % sid in src, (
            "strings.po missing imports.43 string ID `msgctxt \"%s\"` — "
            "operator's Kodi settings dialog will show a blank label for "
            "the affected enum option." % sid)
