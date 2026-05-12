"""Unit tests for the operator_overrides helpers (imports.20).

Tests `channels.markOverrides` and `channels.unmarkOverrides` — pure dict
mutation helpers that record which fields the operator manually set on a
channel record. The Builder._verify and Manager.setLogo paths read this
list and skip auto re-derivation for marked fields, preventing operator
edits from being silently overwritten on the next rebuild.

These tests are pure (no Kodi runtime, no disk I/O). conftest.py adds
resources/lib to sys.path; xbmc stubs live in project_root/tests/.
"""
import pytest

from channels import markOverrides, unmarkOverrides


# ---------- markOverrides ----------

def test_mark_single_key_on_fresh_citem():
    citem = {'id': 'X', 'name': 'Foo'}
    result = markOverrides(citem, 'logo')
    assert result == ['logo']
    assert citem['operator_overrides'] == ['logo']


def test_mark_single_key_with_existing_overrides():
    citem = {'id': 'X', 'operator_overrides': ['number']}
    markOverrides(citem, 'logo')
    assert citem['operator_overrides'] == ['logo', 'number']


def test_mark_is_idempotent():
    citem = {'id': 'X'}
    markOverrides(citem, 'logo')
    markOverrides(citem, 'logo')
    markOverrides(citem, 'logo')
    assert citem['operator_overrides'] == ['logo']


def test_mark_variadic_multiple_keys_in_one_call():
    citem = {'id': 'X'}
    markOverrides(citem, 'logo', 'name', 'group')
    assert citem['operator_overrides'] == ['group', 'logo', 'name']


def test_mark_preserves_existing_when_adding_new():
    citem = {'id': 'X', 'operator_overrides': ['number', 'url']}
    markOverrides(citem, 'logo')
    assert citem['operator_overrides'] == ['logo', 'number', 'url']


def test_mark_returns_sorted_list():
    citem = {'id': 'X'}
    result = markOverrides(citem, 'zoo', 'apple', 'middle')
    assert result == ['apple', 'middle', 'zoo']
    assert result == sorted(result)


def test_mark_handles_operator_overrides_none():
    citem = {'id': 'X', 'operator_overrides': None}
    markOverrides(citem, 'logo')
    assert citem['operator_overrides'] == ['logo']


def test_mark_handles_missing_operator_overrides_key():
    citem = {'id': 'X'}
    assert 'operator_overrides' not in citem
    markOverrides(citem, 'logo')
    assert citem['operator_overrides'] == ['logo']


def test_mark_no_keys_creates_empty_overrides():
    citem = {'id': 'X'}
    result = markOverrides(citem)
    assert result == []
    assert citem['operator_overrides'] == []


def test_mark_dedupes_against_existing():
    citem = {'id': 'X', 'operator_overrides': ['logo']}
    markOverrides(citem, 'logo', 'name')
    assert citem['operator_overrides'] == ['logo', 'name']


# ---------- unmarkOverrides ----------

def test_unmark_removes_single_key():
    citem = {'id': 'X', 'operator_overrides': ['logo', 'name']}
    result = unmarkOverrides(citem, 'logo')
    assert result == ['name']
    assert citem['operator_overrides'] == ['name']


def test_unmark_no_op_on_missing_key():
    citem = {'id': 'X', 'operator_overrides': ['name']}
    unmarkOverrides(citem, 'logo')
    assert citem['operator_overrides'] == ['name']


def test_unmark_variadic():
    citem = {'id': 'X', 'operator_overrides': ['logo', 'name', 'number', 'url']}
    unmarkOverrides(citem, 'logo', 'number')
    assert citem['operator_overrides'] == ['name', 'url']


def test_unmark_handles_operator_overrides_none():
    citem = {'id': 'X', 'operator_overrides': None}
    result = unmarkOverrides(citem, 'logo')
    assert result == []
    assert citem['operator_overrides'] == []


def test_unmark_handles_missing_operator_overrides_key():
    citem = {'id': 'X'}
    result = unmarkOverrides(citem, 'logo')
    assert result == []
    assert citem['operator_overrides'] == []


def test_unmark_all_keys_leaves_empty_list():
    citem = {'id': 'X', 'operator_overrides': ['logo']}
    unmarkOverrides(citem, 'logo')
    assert citem['operator_overrides'] == []


# ---------- round-trip ----------

def test_mark_then_unmark_round_trip():
    citem = {'id': 'X'}
    markOverrides(citem, 'logo', 'name')
    assert citem['operator_overrides'] == ['logo', 'name']
    unmarkOverrides(citem, 'logo')
    assert citem['operator_overrides'] == ['name']
    unmarkOverrides(citem, 'name')
    assert citem['operator_overrides'] == []
    markOverrides(citem, 'logo')
    assert citem['operator_overrides'] == ['logo']


# ---------- Builder guard semantics (source-scan, mirrors test_overlay_lock.py style) ----------

def test_builder_verify_respects_operator_overrides():
    """Source-scan: builder.py:_verify must guard getLogo with an
    operator_overrides check. Mirrors test_overlay_lock.py's source-scan
    style — driving Builder._verify in pytest would require a Kodi
    window/PVR runtime."""
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    builder_src = open(os.path.join(os.path.dirname(here), 'resources', 'lib', 'builder.py')).read()
    # The guard must appear BEFORE the getLogo assignment in _verify.
    guard_idx = builder_src.find("'logo' not in (citem.get('operator_overrides') or [])")
    logo_idx  = builder_src.find("citem['logo'] = self.resources.getLogo")
    assert guard_idx != -1, "builder.py missing operator_overrides guard"
    assert logo_idx  != -1, "builder.py getLogo assignment missing"
    assert guard_idx < logo_idx, "operator_overrides guard must precede getLogo"


def test_manager_setlogo_respects_operator_overrides():
    """Source-scan: manager.py:setLogo must early-return when 'logo' is in
    operator_overrides — covers the __validateName(force=True) rename path
    that would otherwise wipe an operator-selected logo."""
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    manager_src = open(os.path.join(os.path.dirname(here), 'resources', 'lib', 'manager.py')).read()
    # The guard must appear inside setLogo, before name resolution.
    setlogo_idx = manager_src.find("def setLogo(self, name=None, citem={}, force=False):")
    assert setlogo_idx != -1, "manager.py setLogo signature missing"
    # Find the guard within ~600 chars after the def.
    body = manager_src[setlogo_idx:setlogo_idx + 800]
    assert "'logo' in (citem.get('operator_overrides') or [])" in body, \
        "manager.py setLogo missing operator_overrides early-return guard"
