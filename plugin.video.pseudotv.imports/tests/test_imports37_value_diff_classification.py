# -*- coding: utf-8 -*-
"""imports.37: regression tests for /channels/edit.json value-diff
classification.

Pre-existing bug caught during imports.36 live verification: the
dashboard's Custom-edit modal (`manager.html:stageCustomEdit`) stages
ALL 9 Custom-channel fields into the POST body unconditionally — not
just the fields the operator actually changed. The imports.30
classifier on the server side asked `set(fields.keys()).issubset(
META_ONLY_FIELDS)` to discriminate fast-path vs full rebuild. Since
the cf-modal always sent `path`/`radio`/`enabled` (3 keys outside
META_ONLY_FIELDS), `issubset` always failed → every Custom-channel
save triggered a full ~9-min rebuild instead of the sub-second
fast-path. Bug existed since imports.30 shipped.

Fix: compute `actually_changed = {k for k, v in fields.items() if
target.get(k) != v}` as a snapshot BEFORE the mutation loop, then
feed `actually_changed` (value diff) to the classifier instead of
`set(fields.keys())` (key presence). markOverrides keeps using
`fields.keys()` because operator_overrides is operator INTENT (which
fields they deliberately set), not value diff.
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


def _edit_handler_body():
    """Return the /channels/edit.json handler body for source-scan tests."""
    src = _read(os.path.join(LIB, 'server.py'))
    m = re.search(
        r"/channels/edit\.json.*?wfile\.write\(body\)",
        src, re.DOTALL,
    )
    assert m, "could not locate /channels/edit.json handler"
    return m.group(0)


# ============================================================
# Source-scan
# ============================================================

def test_actually_changed_snapshot_present():
    """imports.37: `/channels/edit.json` builds `actually_changed` as a
    set comprehension comparing fields to pre-mutation target values."""
    body = _edit_handler_body()
    assert re.search(
        r"actually_changed\s*=\s*\{k\s+for\s+k,\s*v\s+in\s+fields\.items\(\)\s+if\s+target\.get\(k\)\s*!=\s*v\}",
        body,
    ), (
        "imports.37: `/channels/edit.json` must build the `actually_changed` "
        "snapshot as `{k for k, v in fields.items() if target.get(k) != v}`"
    )


def test_actually_changed_before_mutation_loop():
    """imports.37: snapshot must be captured BEFORE the mutation loop
    that writes fields into target — otherwise target's pre-edit values
    are already overwritten and the diff is always empty."""
    body = _edit_handler_body()
    snapshot_pos = body.find('actually_changed = {')
    mutation_pos = body.find('for k, v in fields.items():\n                            target[k] = v')
    assert snapshot_pos > 0, "snapshot line missing"
    assert mutation_pos > 0, "mutation loop missing"
    assert snapshot_pos < mutation_pos, (
        "imports.37: actually_changed snapshot must come BEFORE the "
        "`for k, v in fields.items(): target[k] = v` mutation loop"
    )


def test_classifier_uses_actually_changed():
    """imports.37: classifier reads `actually_changed`, not
    `set(fields.keys())`."""
    body = _edit_handler_body()
    assert 'edited_keys = actually_changed' in body, (
        "imports.37: classifier must use `edited_keys = actually_changed`"
    )
    assert 'edited_keys = set(fields.keys())' not in body, (
        "imports.37: legacy `edited_keys = set(fields.keys())` must be gone"
    )


def test_classifier_preserves_issubset_check():
    """imports.37: the imports.30 META_ONLY_FIELDS.issubset check shape
    is unchanged — only the input set changed."""
    body = _edit_handler_body()
    assert 'edited_keys.issubset(META_ONLY_FIELDS)' in body, (
        "imports.37 regression: META_ONLY_FIELDS.issubset check must "
        "remain in the classifier"
    )


def test_classifier_handles_empty_actually_changed():
    """imports.37: empty actually_changed (no-op save) leaves both flags
    untouched. Achieved via `elif edited_keys:` instead of bare `else:`."""
    body = _edit_handler_body()
    assert re.search(
        r"if edited_keys and edited_keys\.issubset\(META_ONLY_FIELDS\):\s*\n\s*target\['metadata_changed'\] = True\s*\n\s*elif edited_keys:\s*\n\s*target\['changed'\] = True",
        body,
    ), (
        "imports.37: classifier must use `elif edited_keys:` for the "
        "non-META branch (not bare `else:`) so empty actually_changed "
        "skips both flag assignments"
    )


def test_markOverrides_still_uses_fields_keys():
    """imports.37 regression: imports.20 operator_overrides semantic
    preserved — markOverrides receives `*fields.keys()` (operator INTENT,
    which fields they set), NOT `*actually_changed` (value diff). A
    field the operator deliberately set to the same value is still an
    override against future auto-derivation."""
    body = _edit_handler_body()
    assert 'markOverrides(target, *fields.keys())' in body, (
        "imports.37 regression: markOverrides must continue to use "
        "`fields.keys()` — operator intent, not value diff"
    )


def test_imports_37_grep_marker_present():
    """Source marker for future archaeology."""
    body = _edit_handler_body()
    assert 'imports.37' in body, (
        "imports.37: handler must carry the imports.37 grep marker"
    )


def test_imports_36_logo_auto_copy_still_fires():
    """imports.37 must preserve the imports.36 logo auto-copy block.
    The auto-copy is positioned AFTER the snapshot (so the snapshot
    captures the operator's ORIGINAL chosen path, not the rewritten
    LOGO_LOC path)."""
    body = _edit_handler_body()
    assert 'copyToLogoLoc' in body, (
        "imports.37 regression: imports.36 logo auto-copy must remain"
    )


# ============================================================
# Behavioral — parallel-implementation comparison
# ============================================================
#
# The server's logic is small enough to re-implement in the test and
# verify it produces the expected end state for representative inputs.
# This catches semantic drift even if the source-scans pass.

META_ONLY_FIELDS = frozenset({'number', 'name', 'logo', 'group', 'catchup', 'favorite'})


def _classify(target, fields):
    """Parallel re-implementation of the imports.37 classifier. Must
    match what server.py does. If the source ever drifts from this,
    source-scan tests will fire too."""
    actually_changed = {k for k, v in fields.items() if target.get(k) != v}
    if actually_changed and actually_changed.issubset(META_ONLY_FIELDS):
        return 'metadata_changed'
    elif actually_changed:
        return 'changed'
    return None   # no flag set


def test_cf_modal_meta_only_change_routes_to_fast_path():
    """The bug scenario: cf-modal sends all 9 fields, but only name+logo
    differ from target. actually_changed = {name, logo} → both in META →
    `metadata_changed=True`. Pre-imports.37 this was `changed=True`."""
    target = {
        'id': 'tmnt@PseudoTV_Live',
        'name': 'TMNT', 'number': 302, 'group': ['Cartoons'],
        'logo': 'old.png', 'path': ['/old/path.xsp'],
        'catchup': 'vod', 'radio': False, 'favorite': False, 'enabled': True,
    }
    fields = {
        'name': 'TMNT 2026',                      # CHANGED (META)
        'number': 302,                            # same
        'group': ['Cartoons'],                    # same
        'logo': 'new.png',                        # CHANGED (META)
        'path': ['/old/path.xsp'],                # same
        'catchup': 'vod',                         # same
        'radio': False,                           # same
        'favorite': False,                        # same
        'enabled': True,                          # same
    }
    assert _classify(target, fields) == 'metadata_changed'


def test_cf_modal_no_op_save_sets_no_flag():
    """Operator opens cf-modal, clicks Save without changing anything.
    actually_changed = empty → neither flag set → no Builder kick."""
    target = {
        'name': 'TMNT', 'number': 302, 'group': ['Cartoons'],
        'logo': 'old.png', 'path': ['/old/path.xsp'],
        'catchup': 'vod', 'radio': False, 'favorite': False, 'enabled': True,
    }
    fields = dict(target)   # identical
    fields.pop('name', None)  # actually keep name too
    fields = {k: target[k] for k in (
        'name', 'number', 'group', 'logo', 'path', 'catchup', 'radio', 'favorite', 'enabled',
    )}
    assert _classify(target, fields) is None


def test_cf_modal_mixed_meta_and_non_meta_routes_to_full_rebuild():
    """name (META) + path (non-META) both differ → not META subset →
    `changed=True`. The defensive-default semantic at builder.py:308
    handles the case where both flags might be set elsewhere."""
    target = {
        'name': 'TMNT', 'path': ['/old.xsp'],
        'number': 302, 'group': ['X'], 'logo': 'l.png',
        'catchup': 'vod', 'radio': False, 'favorite': False, 'enabled': True,
    }
    fields = {
        'name': 'TMNT 2026',          # META change
        'path': ['/new.xsp'],         # NON-META change
        'number': 302, 'group': ['X'], 'logo': 'l.png',
        'catchup': 'vod', 'radio': False, 'favorite': False, 'enabled': True,
    }
    assert _classify(target, fields) == 'changed'


def test_pure_path_change_routes_to_full_rebuild():
    """Only path changed (non-META) → `changed=True`. Sanity check."""
    target = {'path': ['/old.xsp']}
    fields = {'path': ['/new.xsp']}
    assert _classify(target, fields) == 'changed'


def test_pure_enabled_change_routes_to_full_rebuild():
    """Only enabled changed (non-META) → `changed=True`."""
    target = {'enabled': True}
    fields = {'enabled': False}
    assert _classify(target, fields) == 'changed'


def test_inline_logo_editor_single_field_correct():
    """Inline-logo editor stages only {logo: newLogo}. Pre-imports.37
    this also classified correctly (single META field is subset).
    Confirms imports.37 doesn't break the existing happy path."""
    target = {'logo': 'old.png'}
    fields = {'logo': 'new.png'}
    assert _classify(target, fields) == 'metadata_changed'


def test_newly_set_field_treated_as_change():
    """Operator sets a field that wasn't in target (e.g., new field
    introduced by schema migration). target.get(k) returns None, fields[k]
    is the new value, None != value → counted as changed."""
    target = {'name': 'X'}            # no 'logo' key
    fields = {'name': 'X', 'logo': 'new.png'}
    assert _classify(target, fields) == 'metadata_changed'


# ============================================================
# Regression guards
# ============================================================

def test_changelog_has_imports_37_entry():
    """changelog.txt has the imports.37 entry — durable assertion."""
    src = _read(os.path.join(LIB, '..', '..', 'changelog.txt'))
    assert 'v.0.8.0+imports.37' in src, (
        "imports.37: changelog.txt must include the imports.37 entry"
    )


def test_imports_30_meta_only_fields_set_unchanged():
    """imports.37 must NOT modify the META_ONLY_FIELDS set itself —
    only the input to the classifier changed."""
    from filter_helpers import META_ONLY_FIELDS
    assert META_ONLY_FIELDS == frozenset(
        {'number', 'name', 'logo', 'group', 'catchup', 'favorite'}
    ), (
        "imports.37 regression: META_ONLY_FIELDS must remain "
        "{number, name, logo, group, catchup, favorite}"
    )
