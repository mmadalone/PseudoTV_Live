"""Regression test for the configurable auto-sync interval (imports.23).

Background: the chkImports daemon's auto-sync interval was hardcoded at
300 s (5 min) at tasks.py:71 since the imports daemon was introduced.
imports.23 exposes it as the operator-visible setting
`Imports_Sync_Interval_Minutes` (spinner with presets:
Disabled / 5 / 15 / 30 / 60 / 120 / 240 / 480 min; default 5).

The setting must:
- Exist in settings.xml under the "Live imports" group with the expected
  8 options (sentinel 0 = "Disabled" reuses the existing #30021 string).
- Have label/help strings in strings.po (#33924, #33925).
- Be read at the top of each outer-loop iteration in
  tasks.py:_startImportsThread (live config changes take effect next cycle).
- Gate the wait-loop's time-based exit on `interval_secs > 0` (so value 0
  means manual-only).
- Preserve the existing kick-handling path so manual refresh works
  regardless of the interval setting.

Source-scan style mirrors test_imports22_pvrrefresh_gate.py +
test_http_server_close.py — driving the actual daemon from pytest is
impractical (needs Kodi Monitor stub, threading, settings DB).

Plan: /home/madalone/.claude/plans/dig-into-c-do-typed-kettle.md (imports.23)
"""
import os
import re


HERE = os.path.dirname(os.path.abspath(__file__))
ADDON_ROOT = os.path.dirname(HERE)
LIB = os.path.join(ADDON_ROOT, 'resources', 'lib')
RESOURCES = os.path.join(ADDON_ROOT, 'resources')


def _read(path):
    with open(path) as f:
        return f.read()


def test_settings_xml_has_imports_sync_interval_minutes():
    """settings.xml must contain the new Imports_Sync_Interval_Minutes
    spinner with all 8 expected options (0/5/15/30/60/120/240/480) and the
    0-value option labeled with the existing #30021 ('Disabled') string."""
    src = _read(os.path.join(RESOURCES, 'settings.xml'))

    # Locate the setting block
    pat = re.compile(
        r'<setting id="Imports_Sync_Interval_Minutes"[^>]*>(.*?)</setting>',
        re.DOTALL,
    )
    m = pat.search(src)
    assert m is not None, (
        "settings.xml missing <setting id='Imports_Sync_Interval_Minutes'> — "
        "imports.23 setting regressed."
    )
    block = m.group(0)

    # Required attributes
    assert 'type="integer"' in block, "setting must be type=integer"
    assert '<level>0</level>' in block, "setting must be level=0 (Basic visibility)"
    assert '<default>5</default>' in block, "setting must default to 5 (preserves prior 300s behavior)"

    # Required options — each <option label="...">VALUE</option>
    for label_id, value in (
        ('30021', '0'),   # reused "Disabled" string
        ('33926', '5'),
        ('33927', '15'),
        ('33928', '30'),
        ('33929', '60'),
        ('33930', '120'),
        ('33931', '240'),
        ('33932', '480'),
    ):
        needle = '<option label="%s">%s</option>' % (label_id, value)
        assert needle in block, (
            "settings.xml Imports_Sync_Interval_Minutes missing option %s. "
            "All 8 options (0/5/15/30/60/120/240/480) must be present; the "
            "0 option's label must be 30021 (the addon's existing 'Disabled' "
            "string — do NOT add a duplicate)." % needle
        )

    # Spinner control (so Kodi-native renders a dropdown, webui auto-renders
    # a <select> via constraints/options branch)
    assert '<control type="spinner"' in block, (
        "setting must use <control type='spinner'> so Kodi-native renders a "
        "dropdown; the webui auto-renders <select> from constraints/options "
        "regardless of control type."
    )


def test_strings_po_has_imports_sync_interval_labels():
    """strings.po must contain the new label (#33924), help (#33925), and the
    seven option labels (#33926-#33932)."""
    src = _read(os.path.join(
        RESOURCES, 'language', 'resource.language.en_gb', 'strings.po'
    ))

    expected = {
        '33924': 'Auto-sync interval',
        '33925': 'How often the imports daemon',  # partial match of help text
        '33926': '5 minutes',
        '33927': '15 minutes',
        '33928': '30 minutes',
        '33929': '60 minutes',
        '33930': '120 minutes',  # partial — "(2 hours)" suffix optional
        '33931': '240 minutes',
        '33932': '480 minutes',
    }
    for sid, needle in expected.items():
        ctx = 'msgctxt "#%s"' % sid
        assert ctx in src, "strings.po missing msgctxt #%s" % sid
        # Find the msgid immediately after this msgctxt
        idx = src.find(ctx)
        tail = src[idx:idx + 600]
        assert needle in tail, (
            "strings.po #%s msgid should contain %r; check the imports.23 "
            "string additions." % (sid, needle)
        )


def test_tasks_py_reads_setting_and_gates_wait_loop():
    """tasks.py _startImportsThread must:
       1. Read Imports_Sync_Interval_Minutes (live, per outer-loop iteration).
       2. Gate the wait-loop's time-based exit on `interval_secs > 0`
          (so value 0 = Disabled = manual-only).
       3. Preserve the kick-handling path so manual refresh works regardless.
    """
    src = _read(os.path.join(LIB, 'tasks.py'))

    # Anchor on the function
    start = src.find('def _startImportsThread(')
    assert start != -1, "tasks.py:_startImportsThread not found"
    # End at the function's closing — find the next top-level method-def at
    # the same indent (4 spaces + 'def ')
    end = src.find('\n    def ', start + 1)
    assert end != -1, "couldn't locate end of _startImportsThread"
    body = src[start:end]

    # (1) Reads the setting
    assert "SETTINGS.getSettingInt('Imports_Sync_Interval_Minutes')" in body, (
        "_startImportsThread must call SETTINGS.getSettingInt("
        "'Imports_Sync_Interval_Minutes') — imports.23 live-read regressed."
    )

    # (2) Gates the wait-loop on interval_secs > 0
    assert re.search(
        r'interval_secs\s*>\s*0\s+and\s+elapsed\s*>=\s*interval_secs',
        body,
    ), (
        "_startImportsThread wait-loop must gate time-based exit on "
        "`interval_secs > 0 and elapsed >= interval_secs` so the Disabled "
        "value (0) keeps the loop in kick-only mode."
    )

    # (3) Kick handling still present
    assert "PROPERTIES.getEXTProperty('chkImports.kick'" in body, (
        "_startImportsThread lost the kick-polling code path — manual "
        "refresh would break."
    )
    assert "PROPERTIES.clrEXTProperty('chkImports.kick')" in body, (
        "_startImportsThread lost the kick-clear after consume — manual "
        "refresh would loop forever."
    )

    # (4) The kick branch must sit OUTSIDE the `interval_secs > 0` gate —
    # i.e., the kick check must be reachable when interval_secs == 0. If a
    # future refactor accidentally nests the kick handling inside an
    # `if interval_secs > 0:` block, Disabled mode would silently drop
    # manual refreshes — the worst-case regression for this feature.
    # Approximate check: find the kick `getEXTProperty` call and the prior
    # `if interval_secs > 0` guard; the kick must NOT appear strictly
    # inside that guard (i.e., the break-on-interval branch).
    kick_pos = body.find("PROPERTIES.getEXTProperty('chkImports.kick'")
    guard_pos = body.find("interval_secs > 0 and elapsed >= interval_secs")
    assert kick_pos != -1 and guard_pos != -1
    # Crude heuristic: the kick poll should appear AFTER the time-exit guard
    # within the wait-loop body (matches the imports.23 layout where the
    # time-exit `break` is checked first, then the kick `try` block
    # follows). If kick_pos < guard_pos, the kick is BEFORE the time-exit
    # check — fine too, but flag for a manual look. If kick_pos > guard_pos
    # we're in the expected layout.
    assert kick_pos > guard_pos, (
        "Layout drifted: the kick-poll block must follow the time-exit "
        "check in the wait-loop. If you reordered intentionally, verify "
        "kick handling still fires when interval_secs == 0 and update "
        "this assertion."
    )
