# -*- coding: utf-8 -*-
"""imports.40: behavioral tests for rules_helpers._normalizeRules.

The validation block at server.py:924-942 (imports.39 baseline) is now
extracted to `rules_helpers._normalizeRules(rules, catalog=None)`. These
tests exercise the helper directly with an injected catalog dict so
they don't need the live RulesList (and the Kodi context it pulls in).

`/api/channels/rules.json` POST + `/channels/add.json` widening both
delegate to this function — behavior change here must show up in both
endpoints, hence the helper is the canonical source of truth.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LIB  = os.path.normpath(os.path.join(HERE, '..', 'resources', 'lib'))
if LIB not in sys.path:
    sys.path.insert(0, LIB)


# Tiny dummy rule object with `myId` attribute, matching the shape returned
# by RulesList().allRules() at rules.py:142-146 — the helper reads `r.myId`
# to key its catalog dict, but tests pass a catalog directly so no rule
# instance is needed; we use int keys directly.
DUMMY_CATALOG = {1: object(), 2: object(), 50: object(), 500: object()}


def test_empty_dict_returns_empty_pair():
    from rules_helpers import _normalizeRules
    normalized, rejected = _normalizeRules({}, catalog=DUMMY_CATALOG)
    assert normalized == {}
    assert rejected == []


def test_none_input_returns_empty_pair():
    from rules_helpers import _normalizeRules
    normalized, rejected = _normalizeRules(None, catalog=DUMMY_CATALOG)
    assert normalized == {}
    assert rejected == []


def test_non_dict_input_returns_empty_pair():
    from rules_helpers import _normalizeRules
    normalized, rejected = _normalizeRules([1, 2, 3], catalog=DUMMY_CATALOG)
    assert normalized == {}
    assert rejected == []


def test_string_rule_id_coerced_to_int():
    from rules_helpers import _normalizeRules
    # JSON wire format → keys arrive as str; helper must coerce.
    normalized, rejected = _normalizeRules({'1': {'values': {}}}, catalog=DUMMY_CATALOG)
    assert 1 in normalized
    assert normalized[1] == {'values': {}}
    assert rejected == []


def test_int_rule_id_passes_through():
    from rules_helpers import _normalizeRules
    normalized, rejected = _normalizeRules({2: {'values': {}}}, catalog=DUMMY_CATALOG)
    assert 2 in normalized
    assert rejected == []


def test_unknown_rule_id_rejected():
    from rules_helpers import _normalizeRules
    normalized, rejected = _normalizeRules({99: {'values': {}}}, catalog=DUMMY_CATALOG)
    assert normalized == {}
    assert len(rejected) == 1
    assert rejected[0]['rule_id'] == 99
    assert 'unknown' in rejected[0]['reason'].lower()


def test_non_int_rule_id_rejected():
    from rules_helpers import _normalizeRules
    normalized, rejected = _normalizeRules({'abc': {'values': {}}}, catalog=DUMMY_CATALOG)
    assert normalized == {}
    assert len(rejected) == 1
    assert rejected[0]['rule_id'] == 'abc'
    assert 'must be int' in rejected[0]['reason']


def test_non_dict_block_rejected():
    from rules_helpers import _normalizeRules
    normalized, rejected = _normalizeRules({1: 'not a dict'}, catalog=DUMMY_CATALOG)
    assert normalized == {}
    assert len(rejected) == 1
    assert rejected[0]['rule_id'] == 1
    assert 'object' in rejected[0]['reason'] or 'dict' in rejected[0]['reason'].lower()


def test_idx_coerced_to_int():
    from rules_helpers import _normalizeRules
    # JSON wire format → idx arrives as str too.
    normalized, rejected = _normalizeRules(
        {'50': {'values': {'0': True, '1': 'abc'}}},
        catalog=DUMMY_CATALOG,
    )
    assert 50 in normalized
    assert normalized[50]['values'] == {0: True, 1: 'abc'}
    assert rejected == []


def test_non_int_idx_rejected_but_other_idx_preserved():
    from rules_helpers import _normalizeRules
    normalized, rejected = _normalizeRules(
        {1: {'values': {'0': True, 'bogus': 42, '2': 'ok'}}},
        catalog=DUMMY_CATALOG,
    )
    assert 1 in normalized
    assert 0 in normalized[1]['values']
    assert 2 in normalized[1]['values']
    assert 'bogus' not in normalized[1]['values']
    # Rejection report includes the rule_id + bad idx
    bogus = [r for r in rejected if r.get('idx') == 'bogus']
    assert len(bogus) == 1
    assert bogus[0]['rule_id'] == 1


def test_missing_values_key_treated_as_empty():
    from rules_helpers import _normalizeRules
    normalized, rejected = _normalizeRules({1: {}}, catalog=DUMMY_CATALOG)
    assert normalized[1] == {'values': {}}
    assert rejected == []


def test_non_dict_values_treated_as_empty():
    from rules_helpers import _normalizeRules
    # values=[1,2,3] (a list) is invalid; helper coerces to empty dict.
    normalized, rejected = _normalizeRules({1: {'values': [1, 2, 3]}}, catalog=DUMMY_CATALOG)
    assert normalized[1] == {'values': {}}
    assert rejected == []


def test_multiple_rules_mixed_valid_and_invalid():
    from rules_helpers import _normalizeRules
    payload = {
        '1':    {'values': {'0': True}},        # valid
        '99':   {'values': {}},                  # unknown rule id
        '50':   'not a dict',                    # bad block
        'abc':  {'values': {}},                  # non-int rule id
        500:    {'values': {'5': 'ok'}},        # valid (int key)
    }
    normalized, rejected = _normalizeRules(payload, catalog=DUMMY_CATALOG)
    assert set(normalized.keys()) == {1, 500}
    assert normalized[1]['values'] == {0: True}
    assert normalized[500]['values'] == {5: 'ok'}
    # Three rejections: 99 unknown, 50 bad block, abc non-int.
    assert len(rejected) == 3
    rids = {r['rule_id'] for r in rejected}
    assert rids == {99, 50, 'abc'}


def test_helper_uses_RulesList_when_catalog_None():
    """When catalog is None, helper imports RulesList and uses it. We assert
    via monkeypatching `sys.modules['rules']` to a stub that exposes
    `RulesList().allRules()` returning a known set."""
    import types
    import importlib
    import sys as _sys
    fake_rules = types.ModuleType('rules')
    class _Rule:
        def __init__(self, mid):
            self.myId = mid
    class FakeRulesList:
        def allRules(self_inner):
            return [_Rule(7), _Rule(8)]
    fake_rules.RulesList = FakeRulesList
    # Save + replace
    saved = _sys.modules.get('rules')
    _sys.modules['rules'] = fake_rules
    try:
        # Re-import rules_helpers fresh to ensure no leftover catalog cache.
        if 'rules_helpers' in _sys.modules:
            del _sys.modules['rules_helpers']
        import rules_helpers
        normalized, rejected = rules_helpers._normalizeRules(
            {'7': {'values': {}}, '9': {'values': {}}},  # 9 unknown
        )
        assert set(normalized.keys()) == {7}
        assert any(r.get('rule_id') == 9 for r in rejected)
    finally:
        if saved is None:
            _sys.modules.pop('rules', None)
        else:
            _sys.modules['rules'] = saved
        # Force rules_helpers re-import for subsequent tests (clean state)
        if 'rules_helpers' in _sys.modules:
            del _sys.modules['rules_helpers']
