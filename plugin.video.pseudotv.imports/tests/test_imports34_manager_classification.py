# -*- coding: utf-8 -*-
"""imports.34: regression tests for Channel Manager metadata-only fast-path
classification.

Before imports.34, every persisted edit from the in-Kodi Channel Manager
(`resources/lib/manager.py`) routed through Builder's slow full-rebuild
path. imports.30 added the metadata-only fast-path
(`__renderMetadataOnly` at builder.py:313) and wired it into the
dashboard's `/channels/edit.json` via the `META_ONLY_FIELDS.issubset`
classification — but the in-Kodi Channel Manager was left to fall back
on imports.29's drift-detection (M3U-vs-channels.json sitem comparison
in `tasks._filterChannelsNeedingBuild`), which works but has up-to-5-min
latency (one chkChannels cycle). imports.34 sets the rebuild flag at the
edit source so the fast-path fires on the next chkChanged tick (~3s).

Five coordinated changes in manager.py:
  A. Import META_ONLY_FIELDS from filter_helpers (single source of truth).
  B. itemInput: classify by key — META → metadata_changed, else → changed.
  C. switchLogo: explicit metadata_changed=True (no saveChannelItems call).
  D. moveChannel: explicit metadata_changed=True before saveChannelItems.
  E. saveChannelItems: policy shift — preserve caller-set flag, default
     to changed=True only when neither flag is set.

The imports.29 drift detection stays as a safety-net fallback.
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


# ============================================================
# A. META_ONLY_FIELDS import (source-scan)
# ============================================================

def test_manager_imports_META_ONLY_FIELDS_from_filter_helpers():
    """manager.py must import META_ONLY_FIELDS from filter_helpers —
    single source of truth. Duplicating the set inline would be a
    drift footgun (the imports.30 file lists exactly the fields that
    are safe for the fast-path; manager.py + server.py must agree)."""
    src = _read(os.path.join(LIB, 'manager.py'))
    assert re.search(
        r"from\s+filter_helpers\s+import\s+META_ONLY_FIELDS",
        src,
    ), (
        "imports.34: manager.py must `from filter_helpers import "
        "META_ONLY_FIELDS` (mirrors server.py:25 single-source-of-truth)"
    )


# ============================================================
# B. itemInput classification (source-scan)
# ============================================================

def test_itemInput_classifies_by_key():
    """itemInput's per-field edit must classify by key — META keys set
    metadata_changed=True, non-META keys set changed=True, only when
    value != retval (no flag mutation on no-op dialogs)."""
    src = _read(os.path.join(LIB, 'manager.py'))
    # Locate the itemInput function body
    m = re.search(
        r"def itemInput\(.*?\n        return citem\n",
        src, re.DOTALL,
    )
    assert m, "could not locate itemInput function"
    body = m.group(0)
    # Must check `if value != retval:` somewhere inside the body
    # (the gate for any flag mutation)
    assert 'if value != retval:' in body, (
        "imports.34: itemInput must gate flag-set on `if value != retval:`"
    )
    # Must use key in META_ONLY_FIELDS to discriminate
    assert 'key in META_ONLY_FIELDS' in body, (
        "imports.34: itemInput must use `key in META_ONLY_FIELDS` to "
        "discriminate fast-path vs full-rebuild"
    )
    # Must set metadata_changed=True in the META branch
    assert "citem['metadata_changed'] = True" in body, (
        "imports.34: itemInput must set `citem['metadata_changed'] = True` "
        "for META keys"
    )
    # Must set changed=True in the non-META branch
    assert "citem['changed'] = True" in body, (
        "imports.34: itemInput must set `citem['changed'] = True` for "
        "non-META keys"
    )


def test_itemInput_drops_legacy_value_neq_retval_bool_set():
    """imports.34 intentionally drops the prior
    `citem['changed'] = value != retval` line (which set changed to a
    BOOL based on the diff, including False on no-change). The new
    classification gates on `if value != retval:` and only sets flags
    when an actual change occurred. The literal `value != retval` is
    still used for `madeItemchange` and the gate — but NOT as a direct
    bool assignment to citem['changed']."""
    src = _read(os.path.join(LIB, 'manager.py'))
    m = re.search(
        r"def itemInput\(.*?\n        return citem\n",
        src, re.DOTALL,
    )
    assert m, "could not locate itemInput function"
    body = m.group(0)
    # The exact phrase `citem['changed']    = value != retval` (the legacy
    # assignment) must be GONE.
    assert "citem['changed']    = value != retval" not in body, (
        "imports.34: itemInput must NOT contain the legacy "
        "`citem['changed']    = value != retval` direct-bool assignment "
        "(the new classification gates on `if value != retval:` and sets "
        "True explicitly)"
    )


def test_itemInput_grep_marker_present():
    """Source-grep marker for future archaeology — quote imports.34 in
    the itemInput rationale comment block."""
    src = _read(os.path.join(LIB, 'manager.py'))
    m = re.search(
        r"def itemInput\(.*?\n        return citem\n",
        src, re.DOTALL,
    )
    assert m, "could not locate itemInput function"
    body = m.group(0)
    assert 'imports.34' in body, (
        "imports.34: itemInput must carry the imports.34 grep marker"
    )


# ============================================================
# C. switchLogo (source-scan)
# ============================================================

def test_switchLogo_sets_metadata_changed():
    """switchLogo must set `channelData['metadata_changed'] = True`
    after the logo assignment. switchLogo doesn't go through
    saveChannelItems, so the policy change in saveChannelItems
    (step E) wouldn't reach this path."""
    src = _read(os.path.join(LIB, 'manager.py'))
    m = re.search(
        r"def switchLogo\(.*?(?=\n    def )",
        src, re.DOTALL,
    )
    assert m, "could not locate switchLogo function"
    body = m.group(0)
    assert "channelData['metadata_changed'] = True" in body, (
        "imports.34: switchLogo must set `channelData['metadata_changed'] "
        "= True` (logo is in META_ONLY_FIELDS)"
    )


def test_switchLogo_grep_marker_present():
    src = _read(os.path.join(LIB, 'manager.py'))
    m = re.search(
        r"def switchLogo\(.*?(?=\n    def )",
        src, re.DOTALL,
    )
    assert m, "could not locate switchLogo function"
    body = m.group(0)
    assert 'imports.34' in body, (
        "imports.34: switchLogo must carry the imports.34 grep marker"
    )


# ============================================================
# D. moveChannel (source-scan)
# ============================================================

def test_moveChannel_sets_metadata_changed_before_saveChannelItems():
    """moveChannel must set `citem['metadata_changed'] = True` BEFORE
    the saveChannelItems call so the imports.34 policy in
    saveChannelItems preserves it (the policy defaults to
    changed=True when neither flag is set — moveChannel must
    explicitly opt-in to metadata_changed)."""
    src = _read(os.path.join(LIB, 'manager.py'))
    m = re.search(
        r"def moveChannel\(.*?(?=\n    def )",
        src, re.DOTALL,
    )
    assert m, "could not locate moveChannel function"
    body = m.group(0)
    assert "citem['metadata_changed'] = True" in body, (
        "imports.34: moveChannel must set `citem['metadata_changed'] = True`"
    )
    # Ordering: metadata_changed=True must appear BEFORE saveChannelItems
    meta_pos = body.find("citem['metadata_changed'] = True")
    save_pos = body.find('self.saveChannelItems(citem)')
    assert meta_pos > 0, "moveChannel missing metadata_changed flag-set"
    assert save_pos > 0, "moveChannel missing saveChannelItems call"
    assert meta_pos < save_pos, (
        "imports.34: moveChannel's metadata_changed=True must be set "
        "BEFORE saveChannelItems (otherwise the policy would default to "
        "changed=True)"
    )


# ============================================================
# E. saveChannelItems policy (source-scan)
# ============================================================

def test_saveChannelItems_preserves_caller_set_flag():
    """saveChannelItems must use the `preserve set flag, else default
    to changed=True` policy. The legacy unconditional
    `citem['changed'] = True` is gone."""
    src = _read(os.path.join(LIB, 'manager.py'))
    m = re.search(
        r"def saveChannelItems\(.*?return citem\n",
        src, re.DOTALL,
    )
    assert m, "could not locate saveChannelItems function"
    body = m.group(0)
    # The policy guard
    assert re.search(
        r"if\s+not\s+\(citem\.get\('changed'\)\s+or\s+citem\.get\('metadata_changed'\)\):",
        body,
    ), (
        "imports.34: saveChannelItems must use `if not (citem.get('changed') "
        "or citem.get('metadata_changed')): citem['changed'] = True` policy"
    )


def test_saveChannelItems_grep_marker_present():
    src = _read(os.path.join(LIB, 'manager.py'))
    m = re.search(
        r"def saveChannelItems\(.*?return citem\n",
        src, re.DOTALL,
    )
    assert m, "could not locate saveChannelItems function"
    body = m.group(0)
    assert 'imports.34' in body, (
        "imports.34: saveChannelItems must carry the imports.34 grep marker"
    )


# ============================================================
# Behavioral — META_ONLY_FIELDS reachability invariant
# ============================================================

def test_META_ONLY_FIELDS_reachable_from_itemInput():
    """Per manager.py:341 (`if key in ["number","type","logo","id",
    "catchup"]: return`), the in-Kodi Channel Manager UI excludes
    number/logo/catchup from itemInput. So META keys reachable from
    itemInput are: name, group, favorite. The other META fields
    (number, logo, catchup) have their own handlers (moveChannel,
    switchLogo, no UI). This test pins those expectations."""
    from filter_helpers import META_ONLY_FIELDS
    reachable_from_itemInput = {'name', 'group', 'favorite'}
    not_reachable_from_itemInput = {'number', 'logo', 'catchup'}
    # All "reachable" keys must be in META_ONLY_FIELDS
    for k in reachable_from_itemInput:
        assert k in META_ONLY_FIELDS, (
            "%r is supposed to be reachable from itemInput AND in "
            "META_ONLY_FIELDS — invariant broken" % k
        )
    # All "not reachable" keys are also in META_ONLY_FIELDS but get
    # set via switchLogo (logo) / moveChannel (number) / not at all
    # (catchup, since not in Channel Manager UI today).
    for k in not_reachable_from_itemInput:
        assert k in META_ONLY_FIELDS, (
            "%r is supposed to be in META_ONLY_FIELDS — invariant broken" % k
        )
    # Non-META keys reachable from itemInput: path/rules/radio/enabled/changed
    non_meta_itemInput = {'path', 'rules', 'radio', 'enabled', 'changed'}
    for k in non_meta_itemInput:
        assert k not in META_ONLY_FIELDS, (
            "%r is reachable from itemInput as non-META — must NOT be "
            "in META_ONLY_FIELDS (would route to fast-path incorrectly)" % k
        )


def test_classification_per_key():
    """Pure-data check: for each itemInput-reachable key, verify the
    classification rule produces the expected target flag."""
    from filter_helpers import META_ONLY_FIELDS
    expectations = {
        # META → metadata_changed
        'name':     'metadata_changed',
        'group':    'metadata_changed',
        'favorite': 'metadata_changed',
        # non-META → changed
        'path':     'changed',
        'rules':    'changed',
        'radio':    'changed',
        'enabled':  'changed',
        'changed':  'changed',  # toggling the changed field itself; not META
    }
    for key, expected_flag in expectations.items():
        if key in META_ONLY_FIELDS:
            actual = 'metadata_changed'
        else:
            actual = 'changed'
        assert actual == expected_flag, (
            "key=%r: expected route to %r, got %r" % (key, expected_flag, actual)
        )


def test_multi_edit_accumulation_full_rebuild_wins():
    """Worked example: rename then path edit → both flags True →
    Builder.__hasMetadataOnlyChange's defensive default
    (`metadata_changed AND NOT changed`) returns False → full rebuild
    fires. This is the safe behavior for mixed-edit sessions."""
    citem = {'id': 'TEST', 'number': 1, 'name': 'X'}
    # Step 1: rename (META)
    citem['metadata_changed'] = True
    # Step 2: path edit (non-META) on the same citem
    citem['changed'] = True
    # Both flags now True
    assert citem.get('metadata_changed') is True
    assert citem.get('changed') is True
    # Builder.__hasMetadataOnlyChange's defensive default:
    has_meta_only = bool(citem.get('metadata_changed')) and not bool(citem.get('changed'))
    assert has_meta_only is False, (
        "Mixed-flag citem must NOT trigger fast-path — defensive default "
        "at builder.py:308 ensures changed=True wins → full rebuild fires"
    )


def test_single_meta_edit_routes_to_fast_path():
    """Only META edit → only metadata_changed=True → fast-path fires."""
    citem = {'id': 'TEST', 'number': 1, 'name': 'X'}
    citem['metadata_changed'] = True
    has_meta_only = bool(citem.get('metadata_changed')) and not bool(citem.get('changed'))
    assert has_meta_only is True, (
        "Pure META edit must trigger fast-path (only metadata_changed set)"
    )


def test_single_non_meta_edit_routes_to_full_rebuild():
    """Only non-META edit → only changed=True → full rebuild fires."""
    citem = {'id': 'TEST', 'number': 1, 'name': 'X'}
    citem['changed'] = True
    has_meta_only = bool(citem.get('metadata_changed')) and not bool(citem.get('changed'))
    assert has_meta_only is False, (
        "Pure non-META edit must trigger full rebuild (changed=True)"
    )


# ============================================================
# Regression guards
# ============================================================

def test_addChannels_still_sets_changed_True():
    """imports.34 must NOT touch the new-channel-add path — new channels
    have no programmes and need a full build, not metadata-only."""
    src = _read(os.path.join(LIB, 'manager.py'))
    m = re.search(
        r"def _addChannels\(.*?(?=\n\s+def )",
        src, re.DOTALL,
    )
    assert m, "could not locate _addChannels function"
    body = m.group(0)
    assert '"changed"' in body and 'True' in body, (
        "imports.34: _addChannels must still produce citem dicts with "
        "changed=True for newly-added channels (full build needed)"
    )


def test_clearChannel_unchanged_blank_template():
    """imports.34 must NOT touch clearChannel — it produces a blank
    tmpItem (no flags set) which falls into saveChannelItems' default
    branch (changed=True). Correct behavior — wiped channel needs to
    start fresh."""
    src = _read(os.path.join(LIB, 'manager.py'))
    m = re.search(
        r"def clearChannel\(.*?(?=\n    def )",
        src, re.DOTALL,
    )
    assert m, "could not locate clearChannel function"
    body = m.group(0)
    # tmpItem from newChannel.copy() — has no metadata_changed key
    assert 'tmpItem = self.newChannel.copy()' in body, (
        "imports.34: clearChannel must still use newChannel.copy() for "
        "the blank template"
    )
    # No metadata_changed set in clearChannel
    assert "tmpItem['metadata_changed']" not in body, (
        "imports.34: clearChannel must NOT set metadata_changed on the "
        "blank template — falls into saveChannelItems policy default "
        "(changed=True, correct)"
    )


def test_builder_hasMetadataOnlyChange_defensive_default_preserved():
    """imports.30 defensive default at builder.py:308 — `metadata_changed
    AND NOT changed`. This is what makes the multi-edit `changed wins`
    behavior in imports.34 correct. If this guard ever loosens to
    `metadata_changed OR ...`, the imports.34 multi-edit semantics
    would break (mixed edits would incorrectly fast-path)."""
    src = _read(os.path.join(LIB, 'builder.py'))
    # The function signature + the guard expression
    m = re.search(
        r"def __hasMetadataOnlyChange\(.*?\n            return state",
        src, re.DOTALL,
    )
    assert m, "could not locate __hasMetadataOnlyChange in builder.py"
    body = m.group(0)
    # Must use AND NOT idiom (not OR)
    assert (
        "and not bool(citem.get('changed', False))" in body
        or "and not citem.get('changed'" in body
    ), (
        "imports.34 regression guard: builder.py:__hasMetadataOnlyChange "
        "must keep its defensive `metadata_changed AND NOT changed` "
        "default — imports.34 relies on this for multi-flag mixed-edit "
        "safety (changed=True wins → full rebuild fires)"
    )


def test_markOverrides_preserved_at_existing_callsites():
    """imports.34 must preserve all imports.20 markOverrides calls in
    manager.py. Operator-edited fields stay marked in operator_overrides
    so Builder._verify / Manager.setLogo don't re-derive on next
    rebuild. The 2 manager.py callsites are itemInput (per-field) and
    switchLogo (logo)."""
    src = _read(os.path.join(LIB, 'manager.py'))
    # itemInput callsite
    assert 'markOverrides(citem, key)' in src, (
        "imports.34: itemInput's imports.20 markOverrides call must be "
        "preserved"
    )
    # switchLogo callsite
    assert "markOverrides(channelData, 'logo')" in src, (
        "imports.34: switchLogo's imports.20 markOverrides call must be "
        "preserved"
    )


def test_drift_detection_fallback_still_present():
    """imports.29 drift detection in tasks._filterChannelsNeedingBuild
    stays alive as the safety-net fallback. If imports.34's classification
    has any gap, drift still catches it via M3U-vs-channels.json sitem
    comparison (just with up-to-5-min latency)."""
    src = _read(os.path.join(LIB, 'tasks.py'))
    assert '_renderStateDrift' in src, (
        "imports.34 regression guard: imports.29's _renderStateDrift "
        "drift detection must remain in tasks.py — it's the safety-net "
        "fallback when imports.34's per-edit classification has any gap"
    )


def test_server_classifies_with_META_ONLY_FIELDS_unchanged():
    """imports.34 must NOT touch server.py's /channels/edit.json
    classification (imports.30, line 658). The dashboard already
    classifies correctly; imports.34 is parity for the in-Kodi UI."""
    src = _read(os.path.join(LIB, 'server.py'))
    assert 'edited_keys.issubset(META_ONLY_FIELDS)' in src, (
        "imports.34 regression guard: server.py /channels/edit.json must "
        "preserve imports.30's `edited_keys.issubset(META_ONLY_FIELDS)` "
        "classification"
    )


# ============================================================
# Changelog entry (durable across cycle bumps)
# ============================================================

def test_changelog_has_imports_34_entry():
    """changelog.txt must include the imports.34 entry — durable
    assertion that survives future cycle bumps. NOTE: deliberately
    not asserting `addon.xml version="0.8.0+imports.34"` here — that
    was a footgun in imports.33's tests (broke when imports.34 bumped
    the version). The changelog entry is the durable record."""
    src = _read(os.path.join(LIB, '..', '..', 'changelog.txt'))
    assert 'v.0.8.0+imports.34' in src, (
        "imports.34: changelog.txt must include the imports.34 entry"
    )
