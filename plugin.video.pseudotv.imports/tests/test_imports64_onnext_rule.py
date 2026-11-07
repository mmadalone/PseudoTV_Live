# -*- coding: utf-8 -*-
"""imports.64: channel rule 2, 'Show OnNext', works.

Before: runAction read and wrote player.onNextMode, which does not exist
(the player's attribute is OnNextMode), so the rule raised AttributeError on
every tune and never applied; the stop wrote a lost value (runActions
re-creates the rules on every call, the rule 53 finding of imports.60) to that
same wrong name. Its option lists were out of step: the third option was
labelled 'Select Diffuse Color' and its mode list sat one slot further on, so
picking a mode showed an empty list. Its own 'Show OnNext' switch was never
applied. A channel change sends no stop, so a rule channel's values carried
over to the next channel.

The rule runs for real under the stubs (as in test_imports60_playstate_glue.py);
services._onPlay, which can't be imported, is read with ast.
"""
import ast
import os

import pytest

HERE      = os.path.dirname(os.path.abspath(__file__))
ADDON_LIB = os.path.join(os.path.dirname(HERE), 'resources', 'lib')

GLOBAL_MODE, GLOBAL_POS = 2, 'Auto'


def _src(name):
    with open(os.path.join(ADDON_LIB, name), encoding='utf-8') as f: return f.read()


class _Player(object):
    def __init__(self):
        self.OnNextMode     = GLOBAL_MODE
        self.onNextPosition = GLOBAL_POS


@pytest.fixture
def rules(monkeypatch):
    import rules as module
    real_int, real_str = module.SETTINGS.getSettingInt, module.SETTINGS.getSetting
    monkeypatch.setattr(module.SETTINGS, 'getSettingInt', lambda key: GLOBAL_MODE if key == 'OnNext_Mode' else real_int(key))
    monkeypatch.setattr(module.SETTINGS, 'getSetting', lambda key: GLOBAL_POS if key == 'OnNext_Position_XY' else real_str(key))
    return module


def _run(rules, values):
    citem  = {'id': 'x@PseudoTV_Live', 'name': 'X', 'rules': {'2': {'values': values}}}
    player = _Player()
    run    = rules.RulesList([citem]).runActions
    return citem, player, run


def test_the_option_lists_are_in_step(rules):
    rule = rules.ShowOnNext()
    assert rule.myId == 2
    assert len(rule.optionLabels) == len(rule.optionValues) == len(rule.optionDescriptions) == len(rule.selectBoxOptions) == 3
    assert rule.optionLabels == [rules.LANGUAGE(30045), rules.LANGUAGE(32229), rules.LANGUAGE(30196)]
    assert isinstance(rule.selectBoxOptions[2], dict) and sorted(rule.selectBoxOptions[2].values()) == [0, 1, 2, 3, 4]
    assert rule.optionValues == [True, GLOBAL_POS, GLOBAL_MODE]


def _settings():
    import types
    return types.SimpleNamespace(getSettingBool=lambda key: False,
                                 getSettingInt=lambda key: GLOBAL_MODE if key == 'OnNext_Mode' else 0,
                                 getSetting=lambda key: GLOBAL_POS if key == 'OnNext_Position_XY' else '')


def _next_channel(rules, rs, start):
    """A channel without rule 2, tuned next: what the player publishes for it."""
    other = {'id': 'y@PseudoTV_Live', 'name': 'Y'}
    state = rs.RuleState(start)
    rules.RulesList([other]).runActions(rules.RULES_ACTION_PLAYER_START, other, {}, inherited=state)
    return rs.effective(start, state.overrides(), rs.FIELDS)


@pytest.mark.parametrize('mode, pos', [(1, '(10,20)'), (3, 'Auto'), (4, '(300,600)')])
def test_tune_applies_the_rule_stop_leaves_it_and_the_next_channel_gets_the_settings(rules, mode, pos):
    """imports.65: the stop does nothing (it used to restore a lost value, then the settings);
    each tune starts from the settings (rulestate_helpers), so nothing carries over."""
    import rulestate_helpers as rs
    start = dict(rs.baseline(_settings()), lastSubState=False)
    citem, _player, run = _run(rules, {'0': True, '1': pos, '2': mode})
    state = rs.RuleState(start)
    run(rules.RULES_ACTION_PLAYER_START, citem, {}, inherited=state)
    assert (state.OnNextMode, state.onNextPosition) == (mode, pos)
    rules.RulesList([citem]).runActions(rules.RULES_ACTION_PLAYER_STOP, citem, {}, inherited=state)   # fresh rules, as live
    assert (state.OnNextMode, state.onNextPosition) == (mode, pos)
    assert state.overrides() == {'OnNextMode': mode, 'onNextPosition': pos}
    nxt = _next_channel(rules, rs, start)
    assert (nxt['OnNextMode'], nxt['onNextPosition']) == (GLOBAL_MODE, GLOBAL_POS)


def test_the_rule_switch_off_means_no_up_next(rules):
    import rulestate_helpers as rs
    start = dict(rs.baseline(_settings()), lastSubState=False)
    citem, _player, run = _run(rules, {'0': False, '1': '(10,20)', '2': 2})
    state = rs.RuleState(start)
    run(rules.RULES_ACTION_PLAYER_START, citem, {}, inherited=state)
    assert state.OnNextMode == 0 and state.onNextPosition == '(10,20)'
    rules.RulesList([citem]).runActions(rules.RULES_ACTION_PLAYER_STOP, citem, {}, inherited=state)
    assert state.OnNextMode == 0
    assert _next_channel(rules, rs, start)['OnNextMode'] == GLOBAL_MODE


@pytest.mark.parametrize('saved, mode', [('1', 1), ('4', 4), ('0', 0), (' 2 ', 2), ('text', GLOBAL_MODE), (None, GLOBAL_MODE), ([], GLOBAL_MODE)])
def test_a_mode_saved_as_text_by_the_dashboard_still_works(rules, saved, mode):
    """manager.html saves a <select> as el.value (text); the overlay compares numbers."""
    citem, player, run = _run(rules, {'0': True, '2': saved})
    run(rules.RULES_ACTION_PLAYER_START, citem, {}, inherited=player)
    assert player.OnNextMode == mode and isinstance(player.OnNextMode, int)


def test_a_tune_no_longer_raises(rules):
    """The old line read player.onNextMode: AttributeError on every tune."""
    citem, player, run = _run(rules, {'0': True, '2': 1})
    rule = rules.RulesList([citem]).loadRules([citem])[citem['id']][2]
    rule.runAction(rules.RULES_ACTION_PLAYER_START, citem, {}, player)   # called directly: no runActions safety net
    assert player.OnNextMode == 1


@pytest.mark.parametrize('name', ['rules.py', 'services.py', 'overlay.py', 'onnext_helpers.py'])
def test_nothing_uses_the_wrong_attribute_name(name):
    tree = ast.parse(_src(name), feature_version=(3, 8))
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr == 'onNextMode']


def _onplay_lines():
    source = _src('services.py')
    tree   = ast.parse(source, feature_version=(3, 8))
    player = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Player')
    method = next(n for n in player.body if isinstance(n, ast.FunctionDef) and n.name == '_onPlay')
    text   = ast.get_source_segment(source, method)
    return [' '.join(line.split('#')[0].split()) for line in text.splitlines() if line.split('#')[0].strip()]


def _method_lines(name):
    source = _src('services.py')
    tree   = ast.parse(source, feature_version=(3, 8))
    player = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Player')
    method = next(n for n in player.body if isinstance(n, ast.FunctionDef) and n.name == name)
    text   = ast.get_source_segment(source, method)
    return [' '.join(line.split('#')[0].split()) for line in text.splitlines() if line.split('#')[0].strip()]


def test_a_new_channel_starts_from_the_settings_before_its_rules_run():
    """A channel change sends no stop: without this reset the previous channel's
    rule values stayed on the next channel. imports.65: the reset is the scratch
    RuleState the rules run on, built from the settings (rule 2's two settings
    are in the table), in _applyChannelRules, the first step of a new channel."""
    import rulestate_helpers as rs
    lines = _onplay_lines()
    new   = next(i for i, line in enumerate(lines) if line.startswith('if newChan:'))
    assert lines[new + 1] == 'self.playingItem = self._applyChannelRules(playingItem)'
    apply = _method_lines('_applyChannelRules')
    state = next(i for i, line in enumerate(apply) if line.startswith('state = rulestate_helpers.RuleState(dict(rulestate_helpers.baseline(SETTINGS)'))
    rules = next(i for i, line in enumerate(apply) if 'RulesList(' in line)
    start = next(i for i, line in enumerate(apply) if 'RULES_ACTION_PLAYER_START' in line)
    assert rules < start and state < start and 'inherited=state' in apply[start]
    assert ('OnNextMode', 'OnNext_Mode', 'getSettingInt') in rs.SETTING_FIELDS
    assert ('onNextPosition', 'OnNext_Position_XY', 'getSetting') in rs.SETTING_FIELDS
