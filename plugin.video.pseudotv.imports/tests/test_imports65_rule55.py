# -*- coding: utf-8 -*-
"""imports.65 (separate change): channel rule 55, 'Show OSD on change', is itself.

DisableOnChange.copy() returned DisableOverlay(), and every rule list is built
from copy() (RulesList.loadRules, RulesList.allRules). So rule 55 acted on the
overlay switch (as a second rule 50), the rule lists showed #50 twice, and the
dashboard's catalog (rules_helpers._normalizeRules, built from allRules())
rejected 55 as an unknown rule. Upstream made the same one-line fix. Only this
file depends on it; test_imports65_channel_rules.py holds either way.
"""
import pytest

import rulestate_helpers as rs


@pytest.fixture(scope='module')
def R():
    import rules
    return rules


def test_copy_is_a_rule55(R):
    copy = R.DisableOnChange().copy()
    assert isinstance(copy, R.DisableOnChange) and copy.myId == 55


def test_load_rules_stores_rule55_as_itself(R):
    citem = {'id': 'x@PseudoTV_Live', 'name': 'X', 'rules': {'55': {'values': {'0': True}}}}
    loaded = R.RulesList([citem]).loadRules([citem])[citem['id']]
    assert list(loaded) == [55]
    assert isinstance(loaded[55], R.DisableOnChange) and loaded[55].myId == 55 and loaded[55].optionValues == [True]


@pytest.mark.parametrize('value', [True, False])
def test_start_sets_the_info_popup_not_the_overlay(R, value):
    citem = {'id': 'x@PseudoTV_Live', 'name': 'X', 'rules': {'55': {'values': {'0': value}}}}
    base  = {'enableOverlay': not value, 'infoOnChange': not value, 'disableTrakt': False, 'rollbackPlaycount': True,
             'replayPercentage': 5, 'OnNextMode': 2, 'onNextPosition': 'Auto', 'smartSubsMode': 'legacy', 'lastSubState': False}
    state = rs.RuleState(base)
    R.RulesList([citem]).runActions(R.RULES_ACTION_PLAYER_START, citem, {}, inherited=state)
    assert state.overrides() == {'infoOnChange': value}
    assert state.enableOverlay is (not value)


def test_each_rule_is_listed_once(R):
    ids = [rule.myId for rule in R.RulesList().allRules()]
    assert len(ids) == len(set(ids))
    assert ids.count(50) == 1 and ids.count(55) == 1


def test_the_dashboard_catalog_accepts_rule55(R):
    from rules_helpers import _normalizeRules
    catalog = {rule.myId: rule for rule in R.RulesList().allRules()}
    normalized, rejected = _normalizeRules({'55': {'values': {'0': True}}, '50': {'values': {'0': False}}}, catalog=catalog)
    assert rejected == []
    assert normalized == {55: {'values': {0: True}}, 50: {'values': {0: False}}}
