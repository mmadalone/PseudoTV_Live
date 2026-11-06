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


@pytest.mark.parametrize('mode, pos', [(1, '(10,20)'), (3, 'Auto'), (4, '(300,600)')])
def test_tune_applies_the_rule_and_stop_restores_the_settings(rules, mode, pos):
    citem, player, run = _run(rules, {'0': True, '1': pos, '2': mode})
    run(rules.RULES_ACTION_PLAYER_START, citem, {}, inherited=player)
    assert (player.OnNextMode, player.onNextPosition) == (mode, pos)
    run(rules.RULES_ACTION_PLAYER_STOP, citem, {}, inherited=player)
    assert (player.OnNextMode, player.onNextPosition) == (GLOBAL_MODE, GLOBAL_POS)   # not the lost [] of a re-created rule
    assert not hasattr(player, 'onNextMode')


def test_the_rule_switch_off_means_no_up_next(rules):
    citem, player, run = _run(rules, {'0': False, '1': '(10,20)', '2': 2})
    run(rules.RULES_ACTION_PLAYER_START, citem, {}, inherited=player)
    assert player.OnNextMode == 0 and player.onNextPosition == '(10,20)'
    run(rules.RULES_ACTION_PLAYER_STOP, citem, {}, inherited=player)
    assert player.OnNextMode == GLOBAL_MODE


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


def test_a_new_channel_starts_from_the_settings_before_its_rules_run():
    """A channel change sends no stop: without this reset the previous channel's
    rule values stayed on the next channel."""
    lines = _onplay_lines()
    new   = next(i for i, line in enumerate(lines) if line.startswith('if newChan:'))
    mode  = lines.index("self.OnNextMode = SETTINGS.getSettingInt('OnNext_Mode')")
    pos   = lines.index('self.onNextPosition = SETTINGS.getSetting("OnNext_Position_XY")')
    rules = next(i for i, line in enumerate(lines) if 'RulesList(' in line)
    start = next(i for i, line in enumerate(lines) if 'RULES_ACTION_PLAYER_START' in line)
    assert new < mode < rules < start and new < pos < rules
    assert sum("self.OnNextMode = SETTINGS.getSettingInt('OnNext_Mode')" == line for line in lines) == 1
