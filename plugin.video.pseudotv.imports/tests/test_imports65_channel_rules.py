# -*- coding: utf-8 -*-
"""imports.65: channel player rules follow the channel.

The values a Custom channel's player rules can set (Show OnNext 2, Enable
Overlay 50, Force Subtitles 51, Trakt 52, Rollback 53, Restart Button 54,
Show OSD on change 55) always equal the settings plus the rules of the
channel being watched (rulestate_helpers). Before:
- they carried over to the next channel (a channel change sends no stop);
- each rule's Stop step restored a lost value ([]: runActions re-creates the
  rules on every call), switching things off on every channel;
- Stop then the same channel took the same-channel branch, so the rules were
  not applied again (smart subtitles on 300/305/308 stopped working);
- every settings save (the add-on's own ZeroConf_Status / Update_Status
  saves too) put the globals back over the channel's rules.

The rules run for real under the stubs (RulesList); services.py can't be
imported, so its methods are compiled out of the source with their
decorators dropped (the pattern of test_imports62_restart_glue.py) and run
against a fake player with a real Lock and the real rulestate_helpers.
Rule 55's own wiring (DisableOnChange.copy()) is tested apart, in
test_imports65_rule55.py: everything here holds with or without that fix.
"""
import ast
import os
import random
import threading
import types

import pytest

import rulestate_helpers as rs
import xbmc

HERE      = os.path.dirname(os.path.abspath(__file__))
ADDON_LIB = os.path.join(os.path.dirname(HERE), 'resources', 'lib')
SYNC_NEW  = 'self._syncPlaystate(oldInfo, playingItem, bool(self.rollbackPlaycount))'


def _src(name):
    with open(os.path.join(ADDON_LIB, name), encoding='utf-8') as f: return f.read()


SERVICES = _src('services.py')
S_TREE   = ast.parse(SERVICES, feature_version=(3, 8))
RULES    = _src('rules.py')
R_TREE   = ast.parse(RULES, feature_version=(3, 8))


def _class(tree, cls):
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == cls: return node
    raise AssertionError('class %s not found' % cls)


def _method(cls, name):
    for item in _class(S_TREE, cls).body:
        if isinstance(item, ast.FunctionDef) and item.name == name: return item
    raise AssertionError('%s.%s not found' % (cls, name))


def _body(cls, name):
    return ast.get_source_segment(SERVICES, _method(cls, name))


def _function(cls, name, **globals_):
    """The real method from services.py as a plain function (decorators dropped)."""
    node = ast.parse(_body(cls, name)).body[0]
    node.decorator_list = []
    namespace = dict(globals_)
    exec(compile(ast.Module(body=[node], type_ignores=[]), 'services.py', 'exec'), namespace)
    return namespace[name]


def _code_lines(text):
    return [line.split('#')[0].strip() for line in text.splitlines() if line.split('#')[0].strip()]


def _index(lines, needle, start=0):
    for i in range(start, len(lines)):
        if needle in lines[i]: return i
    raise AssertionError('%r not found' % needle)


@pytest.fixture(scope='module')
def R():
    import rules
    return rules


# --- fakes -------------------------------------------------------------------------------------

SETTINGS_DEFAULTS = {
    'Overlay_Enable': True, 'Enable_OnInfo': False, 'Disable_Trakt': False, 'Rollback_Watched': True,
    'Replay_Percentage': 5, 'OnNext_Mode': 2, 'OnNext_Position_XY': 'Auto',
    'Seek_Tolerance': 55, 'Seek_Threshold': 97, 'Idle_Timer': 0, 'Run_While_Playing': True,
}


class FakeSettings(object):
    def __init__(self, **values):
        self.values = dict(SETTINGS_DEFAULTS, **values)
        self.reads  = []

    def _read(self, reader, key):
        self.reads.append((reader, key))
        return self.values[key]

    def getSettingBool(self, key): return self._read('getSettingBool', key)
    def getSettingInt(self, key):  return self._read('getSettingInt', key)
    def getSetting(self, key):     return self._read('getSetting', key)


class FakeBuiltin(object):
    def __init__(self): self.subtitle, self.calls = False, []
    def isSubtitle(self): return self.subtitle
    def executebuiltin(self, cmd): self.calls.append(cmd)
    def executewindow(self, cmd): self.calls.append(cmd)


class FakeProperties(object):
    def __init__(self): self.trakt = []
    def setTrakt(self, state): self.trakt.append(state)


PLAYER_METHODS = ('_applyChannelRules', '_releaseChannelRules', '_refreshChannelRules', '_rollbackSwitched',
                  '_runActions', '_onPlay')


class Rig(object):
    """A fake player whose rule methods (and _onPlay) are the real ones from services.py."""

    def __init__(self, R, settings=None, rules_list=None, player_base=object):
        self.settings   = settings or FakeSettings()
        self.builtin    = FakeBuiltin()
        self.properties = FakeProperties()
        self.started    = []          # (citem id) for every PLAYER_START runActions call
        rig = self

        class SpyRulesList(rules_list or R.RulesList):
            def runActions(self, action, citem=None, parameter=None, inherited=None):
                citem = {} if citem is None else citem
                if action == R.RULES_ACTION_PLAYER_START: rig.started.append(citem.get('id'))
                return super(SpyRulesList, self).runActions(action, citem, parameter, inherited)

        g = dict(RulesList=SpyRulesList, rulestate_helpers=rs, SETTINGS=self.settings, xbmc=xbmc,
                 RULES_ACTION_PLAYER_START=R.RULES_ACTION_PLAYER_START, BUILTIN=self.builtin,
                 PROPERTIES=self.properties, random=random)
        ns = {name: _function('Player', name, **g) for name in PLAYER_METHODS}
        self.player = type('FakePlayer', (FakePlayerBase, player_base), ns)(self.settings)
        self.monitor = types.SimpleNamespace(player=self.player, rollbackSwitch=self.settings.values['Rollback_Watched'],
                                             log=lambda msg, level=xbmc.LOGDEBUG: None)
        self.updatePlayerSettings = _function('Monitor', '_updatePlayerSettings', SETTINGS=self.settings)
        self.chkRollbackSwitch    = _function('Monitor', '_chkRollbackSwitch', SETTINGS=self.settings)

    def values(self, fields=rs.FIELDS):
        return {f: getattr(self.player, f) for f in fields}


class FakePlayerBase(object):
    def __init__(self, settings):
        # the class defaults of services.Player: the settings at start-up, smartSubsMode 'legacy'
        for field, value in rs.baseline(settings).items(): object.__setattr__(self, field, value)
        object.__setattr__(self, 'lastSubState', False)
        for name, value in (('runActions', None), ('playingItem', {}), ('_retune', False), ('_rules_lock', threading.Lock()),
                            ('_rulesOverrides', {}), ('_rulesBaseline', None), ('logged', []), ('calls', []),
                            ('jsonRPC', types.SimpleNamespace(_setRuntime=lambda *a: None))):
            object.__setattr__(self, name, value)

    def log(self, msg, level=xbmc.LOGDEBUG): self.logged.append((msg, level))
    def toggleInfo(self, state=False): self.calls.append(('toggleInfo', state))
    def toggleBackground(self, state=False): self.calls.append(('toggleBackground', state))
    def toggleOverlay(self, state=False): self.calls.append(('toggleOverlay', state))
    def toggleReplay(self, state=False, epoch=None): self.calls.append(('toggleReplay', state))
    def setSubtitles(self, state=True):
        self.calls.append(('setSubtitles', self.smartSubsMode))
        self.calls.append(('subState', state))   # the on/off value rule 51 legacy mode forces
    def _syncPlaystate(self, oldInfo=None, newInfo=None, allow=False): self.calls.append(('sync', allow))
    def _restartPending(self, playingItem): self.calls.append(('restartPending', None))
    def _restoreLiveSubtitles(self, epoch): self.calls.append(('restoreLiveSubtitles', epoch)) # imports.67

    def infos(self):
        return [msg for msg, level in self.logged if level == xbmc.LOGINFO]


def _item(chid, rules=None):
    citem = {'id': chid, 'name': chid}
    if rules: citem['rules'] = rules
    return {'isPseudoTV': True, 'chid': chid, 'citem': citem, 'fitem': {}}


SMART  = {'51': {'values': {'0': False, '1': 'smart'}}}
LEGACY_ON  = {'51': {'values': {'0': True, '1': 'legacy'}}}
LEGACY_OFF = {'51': {'values': {'0': False, '1': 'legacy'}}}
ALL_A  = {'2': {'values': {'0': False, '1': '(1,2)', '2': 3}}, '50': {'values': {'0': False}},
          '51': {'values': {'0': False, '1': 'smart'}}, '52': {'values': {'0': True}}, '53': {'values': {'0': False}},
          '54': {'values': {'0': 50}}, '55': {'values': {'0': True}}}


# --- rulestate_helpers -------------------------------------------------------------------------

def test_baseline_reads_each_setting():
    settings = FakeSettings()
    base = rs.baseline(settings)
    assert sorted(settings.reads) == sorted([
        ('getSettingBool', 'Overlay_Enable'), ('getSettingBool', 'Enable_OnInfo'), ('getSettingBool', 'Disable_Trakt'),
        ('getSettingBool', 'Rollback_Watched'), ('getSettingInt', 'Replay_Percentage'), ('getSettingInt', 'OnNext_Mode'),
        ('getSetting', 'OnNext_Position_XY')])
    assert base == {'enableOverlay': True, 'infoOnChange': False, 'disableTrakt': False, 'rollbackPlaycount': True,
                    'replayPercentage': 5, 'OnNextMode': 2, 'onNextPosition': 'Auto', 'smartSubsMode': 'legacy'}
    assert set(base) == set(rs.FIELDS) and rs.TUNE_FIELDS == rs.FIELDS + ('lastSubState',)


def test_rulestate_records_only_what_the_rules_write():
    start = dict(rs.baseline(FakeSettings()), lastSubState=False)
    state = rs.RuleState(start)
    assert {f: getattr(state, f) for f in rs.TUNE_FIELDS} == start and state.overrides() == {}
    state.replayPercentage = 40
    state.replayPercentage = 50
    state.smartSubsMode = 'smart'
    assert state.overrides() == {'replayPercentage': 50, 'smartSubsMode': 'smart'}
    with pytest.raises(AttributeError):
        state.minDuration = 1          # not a rule value: a new rule writing it must fail loudly
    with pytest.raises(AttributeError):
        state.notAField = 1
    copy = state.overrides()
    copy['enableOverlay'] = False
    assert 'enableOverlay' not in state.overrides()
    assert not hasattr(state, '__dict__')


def test_effective_layers_overrides_on_the_settings():
    base = rs.baseline(FakeSettings())
    got  = rs.effective(base, {'replayPercentage': 50, 'smartSubsMode': 'smart'}, rs.FIELDS)
    assert got == dict(base, replayPercentage=50, smartSubsMode='smart')
    assert rs.effective(base, {}, rs.FIELDS) == base
    assert rs.effective(dict(base, lastSubState=False), {'lastSubState': True}, rs.TUNE_FIELDS)['lastSubState'] is True
    assert rs.effective(dict(base, lastSubState=True), {}, rs.TUNE_FIELDS)['lastSubState'] is True
    assert 'lastSubState' not in rs.effective(base, {'lastSubState': True}, rs.FIELDS)
    target = types.SimpleNamespace()
    rs.publish(got, target, rs.FIELDS)
    assert vars(target) == got


# --- the table matches the code --------------------------------------------------------------

def _is_player_start(test):
    return (isinstance(test, ast.Compare) and isinstance(test.left, ast.Name) and test.left.id == 'actionid'
            and len(test.comparators) == 1 and isinstance(test.comparators[0], ast.Name)
            and test.comparators[0].id == 'RULES_ACTION_PLAYER_START')


def test_table_matches_what_start_rules_write():
    """Every attribute a PLAYER_START branch assigns on player/inherited has a row, and every row is written."""
    written = set()
    for node in ast.walk(R_TREE):
        if isinstance(node, ast.If) and _is_player_start(node.test):
            for stmt in node.body:
                for n in ast.walk(stmt):
                    if (isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Store) and isinstance(n.value, ast.Name)
                            and n.value.id in ('player', 'inherited')):
                        written.add(n.attr)
    assert written == set(rs.TUNE_FIELDS)


def test_player_class_defaults_match_the_table():
    defaults = {}
    for node in _class(S_TREE, 'Player').body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            defaults[node.targets[0].id] = node.value
    for attr, key, reader in rs.SETTING_FIELDS:
        call = defaults[attr]
        assert isinstance(call, ast.Call) and ast.unparse(call.func) == 'SETTINGS.%s' % reader
        assert [a.value for a in call.args] == [key]
    for attr, value in rs.FIXED:
        assert isinstance(defaults[attr], ast.Constant) and defaults[attr].value == value


def test_only_the_player_writers_assign_rule_values():
    """Outside the class defaults, no direct write of a rule value is left in services.py except the
    Rollback switch's one in _rollbackSwitched (the publish helpers use setattr)."""
    parents = {child: node for node in ast.walk(S_TREE) for child in ast.iter_child_nodes(node)}

    def owner(n):
        names = []
        while n in parents:
            n = parents[n]
            if isinstance(n, (ast.FunctionDef, ast.ClassDef)): names.append(n.name)
        return tuple(reversed(names))
    writes = [owner(n) for n in ast.walk(S_TREE) if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Store)
              and n.attr in rs.FIELDS and ast.unparse(n.value) in ('self', 'self.player')]
    assert writes == [('Player', '_rollbackSwitched')]


# --- rules.py: the player rules do nothing at Stop -------------------------------------------

PLAYER_RULES = {
    '2':          ('ShowOnNext',        {'0': True, '1': '(1,2)', '2': 3}, {'OnNextMode': 3, 'onNextPosition': '(1,2)'}),
    '50':         ('DisableOverlay',    {'0': False},                      {'enableOverlay': False}),
    '51':         ('ForceSubtitles',    {'0': False, '1': 'smart'},        {'smartSubsMode': 'smart'}),
    '51-legacy':  ('ForceSubtitles',    {'0': True, '1': 'legacy'},        {'smartSubsMode': 'legacy', 'lastSubState': True}),
    '52':         ('DisableTrakt',      {'0': True},                       {'disableTrakt': True}),
    '53':         ('RollbackPlaycount', {'0': False},                      {'rollbackPlaycount': False}),
    '54':         ('DisableRestart',    {'0': 50},                         {'replayPercentage': 50}),
    '55':         ('DisableOnChange',   {'0': True},                       {'infoOnChange': True}),
}


@pytest.mark.parametrize('key', sorted(PLAYER_RULES))
def test_player_rules_do_nothing_at_stop(R, key):
    name, values, expected = PLAYER_RULES[key]
    rule = getattr(R, name)()
    assert R.RULES_ACTION_PLAYER_START in rule.actions and R.RULES_ACTION_PLAYER_STOP not in rule.actions
    start = dict(rs.baseline(FakeSettings()), lastSubState=False)
    for idx, value in values.items(): rule.optionValues[int(idx)] = value
    state = rs.RuleState(start)
    rule.runAction(R.RULES_ACTION_PLAYER_START, {'id': 'x'}, {}, state)     # the class's own START
    assert state.overrides() == expected
    # As live: START then STOP through RulesList, each call with freshly loaded rules.
    citem = {'id': 'x@PseudoTV_Live', 'name': 'X', 'rules': {key.split('-')[0]: {'values': values}}}
    state = rs.RuleState(start)
    R.RulesList([citem]).runActions(R.RULES_ACTION_PLAYER_START, citem, {}, inherited=state)
    after_start, written = {f: getattr(state, f) for f in rs.TUNE_FIELDS}, state.overrides()
    assert written
    R.RulesList([citem]).runActions(R.RULES_ACTION_PLAYER_STOP, citem, {}, inherited=state)
    assert {f: getattr(state, f) for f in rs.TUNE_FIELDS} == after_start and state.overrides() == written
    assert [] not in after_start.values()


@pytest.mark.parametrize('name', sorted(set(name for name, _values, _expected in PLAYER_RULES.values())))
def test_player_rules_have_no_stop_step_and_no_snapshot(name):
    """The Stop steps and the storedValues snapshots that fed them are gone (not just unlisted)."""
    run = next(n for n in _class(R_TREE, name).body if isinstance(n, ast.FunctionDef) and n.name == 'runAction')
    text = ast.get_source_segment(RULES, run)
    assert 'RULES_ACTION_PLAYER_STOP' not in text
    assert not [n for n in ast.walk(run) if isinstance(n, ast.Attribute) and n.attr == 'storedValues']


def test_pause_rule_keeps_its_stop(R):
    assert R.RULES_ACTION_PLAYER_STOP in R.PauseRule().actions


# --- the tune ------------------------------------------------------------------------------------

def test_channel_change_starts_from_the_settings(R):
    rig = Rig(R)
    base = rs.baseline(rig.settings)
    rig.player._applyChannelRules(_item('A', ALL_A))
    assert (rig.player.OnNextMode, rig.player.onNextPosition, rig.player.smartSubsMode, rig.player.disableTrakt,
            rig.player.rollbackPlaycount, rig.player.replayPercentage) == (0, '(1,2)', 'smart', True, False, 50)
    rig.player._applyChannelRules(_item('B'))
    assert rig.values() == base and rig.player.smartSubsMode == 'legacy'
    assert rig.player._rulesOverrides == {} and rig.player._rulesBaseline == base
    # rule 51 legacy mode: its on/off value is published with the rest (TUNE_FIELDS)
    rig.player._applyChannelRules(_item('C', LEGACY_ON))
    assert rig.values(rs.TUNE_FIELDS) == dict(base, lastSubState=True)
    assert rig.started == ['A', 'B', 'C']


def test_tune_logs_what_the_channel_set(R):
    rig = Rig(R)
    rig.player._applyChannelRules(_item('300', SMART))
    assert rig.player.infos() == ["[300] channel rules: {'smartSubsMode': 'smart'}"]
    rig.player._applyChannelRules(_item('B'))
    assert len(rig.player.infos()) == 1


class Recording(object):
    """Logs every write of a rule value with whether _rules_lock was held."""
    def __setattr__(self, name, value):
        if name in rs.TUNE_FIELDS:
            lock = self.__dict__.get('_rules_lock')
            self.__dict__.setdefault('writes', []).append((name, value, bool(lock and lock.locked())))
        object.__setattr__(self, name, value)


def test_rules_never_show_a_half_applied_state(R):
    rig = Rig(R, player_base=Recording)
    rule50 = {'50': {'values': {'0': False}}, '53': {'values': {'0': False}}}
    rig.player._applyChannelRules(_item('X', rule50))
    rig.settings.values['Replay_Percentage'] = 10
    rig.player._refreshChannelRules()
    assert rig.player._rollbackSwitched(True) is False
    writes = rig.player.__dict__['writes']
    assert writes and all(locked for _name, _value, locked in writes)
    assert ('enableOverlay', True) not in [(n, v) for n, v, _locked in writes]
    assert ('rollbackPlaycount', True) not in [(n, v) for n, v, _locked in writes]
    assert rig.player.enableOverlay is False and rig.player.rollbackPlaycount is False


@pytest.fixture
def blocking_rule54(R, monkeypatch):
    """Rule 54's START waits for `release` (after setting `entered`)."""
    entered, release = threading.Event(), threading.Event()
    real = R.DisableRestart.runAction

    def run(self, actionid, citem, parameter, player):
        if actionid == R.RULES_ACTION_PLAYER_START:
            entered.set()
            release.wait(10)
        return real(self, actionid, citem, parameter, player)
    monkeypatch.setattr(R.DisableRestart, 'runAction', run)
    yield entered, release
    release.set()


def _thread(fn, *args):
    t = threading.Thread(target=fn, args=args)
    t.daemon = True
    t.start()
    return t


def test_rules_run_outside_the_lock(R, blocking_rule54):
    entered, release = blocking_rule54
    rig = Rig(R)
    tune = _thread(rig.player._applyChannelRules, _item('X', {'54': {'values': {'0': 50}}}))
    try:
        assert entered.wait(5)
        other = _thread(lambda: (rig.player._refreshChannelRules(), rig.player._rollbackSwitched(False)))
        other.join(5)
        assert not other.is_alive()       # neither waited for the rule run
    finally:
        release.set()
    tune.join(5)
    assert not tune.is_alive() and rig.player.replayPercentage == 50


def test_tune_publishes_the_settings_of_publish_time(R, blocking_rule54):
    entered, release = blocking_rule54
    rig = Rig(R)
    tune = _thread(rig.player._applyChannelRules, _item('X', {'54': {'values': {'0': 50}}}))
    try:
        assert entered.wait(5)
        rig.settings.values['Overlay_Enable'] = False      # saved while the rules run
        rig.settings.values['Replay_Percentage'] = 10
    finally:
        release.set()
    tune.join(5)
    assert rig.player.enableOverlay is False and rig.player.replayPercentage == 50
    assert rig.player._rulesBaseline['enableOverlay'] is False


class BlockingPublish(object):
    """The tune's publish stops at rollbackPlaycount (mid-way) until released."""
    def __setattr__(self, name, value):
        gate = self.__dict__.get('gate')
        if gate and name == 'rollbackPlaycount' and threading.current_thread() is gate['thread']:
            gate['entered'].set()
            gate['release'].wait(10)
        object.__setattr__(self, name, value)


def test_publish_is_atomic(R):
    rig = Rig(R, player_base=BlockingPublish)
    gate = {'entered': threading.Event(), 'release': threading.Event(), 'thread': None}
    item = _item('X', {'54': {'values': {'0': 50}}, '51': {'values': {'0': False, '1': 'smart'}}})
    tune = threading.Thread(target=rig.player._applyChannelRules, args=(item,))
    tune.daemon = True
    gate['thread'] = tune
    rig.player.__dict__['gate'] = gate
    tune.start()
    try:
        assert gate['entered'].wait(5)
        rig.settings.values['Overlay_Enable'] = False
        rig.settings.values['OnNext_Mode'] = 1
        refresh = _thread(rig.player._refreshChannelRules)
        refresh.join(0.3)
        assert refresh.is_alive()          # waits for the tune's publish to finish
    finally:
        gate['release'].set()
    tune.join(5)
    refresh.join(5)
    assert not tune.is_alive() and not refresh.is_alive()
    expected = dict(rs.baseline(rig.settings), replayPercentage=50, smartSubsMode='smart')
    assert rig.values() == expected


# --- settings saves and the Rollback switch ----------------------------------------------------

def _refresh(rig):
    rig.started[:] = []
    rig.updatePlayerSettings(rig.monitor)


def test_settings_refresh(R):
    rig = Rig(R)
    rig.player._applyChannelRules(_item('308', {'54': {'values': {'0': 50}}, '51': {'values': {'0': False, '1': 'smart'}}}))
    applied = rig.values()
    infos = len(rig.player.infos())
    # (a) a save that changes none of the rule settings (ZeroConf_Status, Update_Status, the debug switch)
    rig.settings.values.update({'Seek_Tolerance': 30, 'Idle_Timer': 3})
    _refresh(rig)
    assert rig.values() == applied and len(rig.player.infos()) == infos and rig.started == []
    assert (rig.player.minDuration, rig.player.maxProgress, rig.player.sleepTime, rig.player.runWhilePlaying) == (30, 97, 3, True)
    # (b) the user changes two of them while the channel plays: the channel keeps its own (Q1 = a)
    rig.settings.values.update({'Replay_Percentage': 10, 'OnNext_Mode': 1})
    _refresh(rig)
    assert rig.player.replayPercentage == 50 and rig.player.smartSubsMode == 'smart' and rig.player.OnNextMode == 1
    assert rig.player.infos()[infos:] == ["_refreshChannelRules, settings changed: ['OnNextMode', 'replayPercentage']; "
                                          "channel keeps: ['replayPercentage', 'smartSubsMode']"]
    assert rig.started == []
    # (c) after a hand-over: the settings alone
    rig.player._releaseChannelRules()
    _refresh(rig)
    assert rig.values() == rs.baseline(rig.settings) and rig.player.replayPercentage == 10
    assert rig.player.smartSubsMode == 'legacy' and rig.started == []


def test_refresh_before_any_tune_is_the_old_refresh(R):
    rig = Rig(R)
    rig.settings.values.update({'Overlay_Enable': False, 'Replay_Percentage': 25})
    assert rig.player._refreshChannelRules() is False        # nothing published before: nothing to compare
    assert rig.values() == rs.baseline(rig.settings)


@pytest.mark.parametrize('before, now, value, after, cancels', [
    (True, False, True, False, 1), (False, True, False, True, 0), (True, True, False, False, 0), (False, False, True, True, 0),
], ids=['off', 'on', 'unchanged-on', 'unchanged-off'])
def test_rollback_switch_on_a_channel_without_rule53(R, before, now, value, after, cancels):
    rig = Rig(R)
    rig.player._applyChannelRules(_item('A', SMART))
    rig.player.rollbackPlaycount, rig.monitor.rollbackSwitch = value, before
    rig.settings.values['Rollback_Watched'] = now
    cancelled = []
    rig.player._cancelPlaystate = lambda: cancelled.append(1)
    rig.chkRollbackSwitch(rig.monitor)
    assert rig.player.rollbackPlaycount is after and len(cancelled) == cancels and rig.monitor.rollbackSwitch is now


def test_rollback_switch_keeps_a_channels_own_rule53(R):
    rig = Rig(R)
    cancelled = []
    rig.player._cancelPlaystate = lambda: cancelled.append(1)
    rig.player._applyChannelRules(_item('A', {'53': {'values': {'0': True}}}))
    rig.settings.values['Rollback_Watched'] = False
    rig.chkRollbackSwitch(rig.monitor)
    assert rig.player.rollbackPlaycount is True and cancelled == []
    # a rule-53 'off' channel, the switch flipped off then on, then the refresh: never the global value
    rig.settings.values['Rollback_Watched'] = True
    rig.monitor.rollbackSwitch = True
    rig.player._applyChannelRules(_item('B', {'53': {'values': {'0': False}}}))
    rig.settings.values['Rollback_Watched'] = False
    rig.chkRollbackSwitch(rig.monitor)
    assert rig.player.rollbackPlaycount is False
    rig.settings.values['Rollback_Watched'] = True
    rig.chkRollbackSwitch(rig.monitor)
    assert rig.player.rollbackPlaycount is False
    _refresh(rig)
    assert rig.player.rollbackPlaycount is False


# --- the hand-over -------------------------------------------------------------------------------

def test_handover_releases(R):
    rig = Rig(R)
    rig.player._applyChannelRules(_item('300', ALL_A))
    rig.player._releaseChannelRules()
    base = rs.baseline(rig.settings)
    assert rig.values() == base and rig.player._rulesOverrides == {} and rig.player._rulesBaseline == base
    assert ('_releaseChannelRules, back to the settings', xbmc.LOGDEBUG) in rig.player.logged
    lines = _code_lines(_body('Player', '_onPlay'))
    start = _index(lines, "if not playingItem.get('isPseudoTV'):")
    sync  = _index(lines, "if oldInfo.get('isPseudoTV'): self._syncPlaystate(oldInfo)", start)
    rel   = _index(lines, 'self._releaseChannelRules()', start)
    live  = _index(lines, "self.toggleReplay(bool(SETTINGS.getSettingInt('Replay_Percentage')), epoch)", start)
    ret   = _index(lines, 'return', start)
    assert sync < rel < live < ret and lines[live - 1] == 'self._retune = False'
    assert sum('self._releaseChannelRules()' in line for line in lines) == 1


# --- _onPlay ---------------------------------------------------------------------------------------

def _play(rig, item, retune=False):
    rig.player._retune = retune
    rig.player.calls[:] = []
    rig.player._onPlay(item, 7)


def test_onplay_runs_rules_on_every_real_tune(R):
    rig = Rig(R)
    # (c) a different channel: START runs
    _play(rig, _item('300', SMART))
    assert rig.started == ['300'] and rig.player.smartSubsMode == 'smart'
    assert ('setSubtitles', 'smart') in rig.player.calls and ('toggleReplay', True) in rig.player.calls
    # (b) a programme change on the same channel: no START; the smart re-check still runs
    _play(rig, _item('300', SMART))
    assert rig.started == ['300'] and rig.player.calls.count(('setSubtitles', 'smart')) == 1
    assert not any(c[0] == 'toggleReplay' for c in rig.player.calls)
    # (a) Stop, then the same channel: START runs again, the plan is applied, the pop-up offered once
    rig.player.smartSubsMode = 'legacy'      # whatever the stop left
    _play(rig, _item('300', SMART), retune=True)
    assert rig.started == ['300', '300'] and rig.player.smartSubsMode == 'smart'
    assert ('setSubtitles', 'smart') in rig.player.calls and rig.player.calls.count(('toggleReplay', True)) == 1
    assert rig.player._retune is False
    # (d) LiveTV: released to the settings
    _play(rig, {'chid': 'live'})
    assert rig.player._rulesOverrides == {} and rig.player.smartSubsMode == 'legacy'
    assert rig.values() == rs.baseline(rig.settings)
    # (e) 300 -> LiveTV -> a channel without rules: 'smart' does not come back
    _play(rig, _item('300', SMART))
    _play(rig, {'chid': 'live'})
    _play(rig, _item('310'))
    assert rig.player.smartSubsMode == 'legacy' and rig.started[-1] == '310'
    _play(rig, _item('310'))                     # its next programme
    assert ('setSubtitles', 'smart') not in rig.player.calls


def test_onplay_forces_the_rules_subtitle_state(R):
    # rule 51 legacy mode: the channel's on/off value reaches setSubtitles; the next
    # channel starts again from Kodi's own subtitle state
    rig = Rig(R)
    rig.builtin.subtitle = False
    _play(rig, _item('X', LEGACY_ON))
    assert rig.player.lastSubState is True and ('subState', True) in rig.player.calls
    _play(rig, _item('Y'))
    assert rig.player.lastSubState is False and ('subState', False) in rig.player.calls
    rig.builtin.subtitle = True
    _play(rig, _item('Z', LEGACY_OFF))
    assert rig.player.lastSubState is False and ('subState', False) in rig.player.calls
    _play(rig, _item('Y'))
    assert rig.player.lastSubState is True and ('subState', True) in rig.player.calls
    assert rig.started == ['X', 'Y', 'Z', 'Y']


def test_onplay_wiring():
    lines = _code_lines(_body('Player', '_onPlay'))
    flag  = _index(lines, 'retune, self._retune = self._retune, False')
    new   = _index(lines, 'newChan = retune or ', flag)
    assert lines[new] == "newChan = retune or oldInfo.get('chid',random.random()) != playingItem.get('chid')"
    branch = _index(lines, 'if newChan:', new)
    assert lines[branch + 1] == 'self.playingItem = self._applyChannelRules(playingItem)'
    assert lines[branch + 2] == SYNC_NEW
    assert not any('if retune: self.toggleReplay' in line for line in lines)
    assert sum(line == 'self.toggleReplay(bool(self.replayPercentage), epoch)' for line in lines) == 1
    keys  = [key for _attr, key, _reader in rs.SETTING_FIELDS]
    reads = [line for line in lines if 'SETTINGS.' in line and any(key in line for key in keys)]
    assert reads == ["self.toggleReplay(bool(SETTINGS.getSettingInt('Replay_Percentage')), epoch)"]
    assert 'RulesList(' not in ''.join(lines) and 'RULES_ACTION_PLAYER_START' not in ''.join(lines)


def test_writers_hold_only_the_lock_for_in_process_work():
    """Inside _rules_lock: settings reads, dict work and attribute writes. No rule run, UI, JSON-RPC or sleep."""
    for name in ('_applyChannelRules', '_releaseChannelRules', '_refreshChannelRules', '_rollbackSwitched'):
        withs = [n for n in ast.walk(_method('Player', name)) if isinstance(n, ast.With)]
        assert len(withs) == 1 and ast.unparse(withs[0].items[0].context_expr) == 'self._rules_lock'
        body = '\n'.join(ast.unparse(s) for s in withs[0].body)
        for banned in ('runActions', 'jsonRPC', 'toggle', 'sleep', 'waitForAbort', 'DIALOG', 'BUILTIN', '_lock', 'log('):
            assert banned not in body, (name, banned)
    apply = _code_lines(_body('Player', '_applyChannelRules'))
    assert _index(apply, 'RULES_ACTION_PLAYER_START') < _index(apply, 'with self._rules_lock:')
    assert 'inherited=state' in apply[_index(apply, 'RULES_ACTION_PLAYER_START')]


def test_monitor_uses_the_player_writers():
    update = _code_lines(_body('Monitor', '_updatePlayerSettings'))
    assert update[-1] == 'self.player._refreshChannelRules()'
    assert sum(line.startswith('self.player.') for line in update) == 5
    check = _code_lines(_body('Monitor', '_chkRollbackSwitch'))
    assert 'effective = self.player._rollbackSwitched(state)' in check
    assert check[-1] == 'if not state and not effective: self.player._cancelPlaystate()'


def test_player_state_is_per_instance():
    init = _code_lines(_body('Player', '__init__'))
    for line in ('self._rules_lock     = Lock()', 'self._rulesOverrides = {}', 'self._rulesBaseline  = None'):
        assert line in init
    class_level = {t.id for n in _class(S_TREE, 'Player').body if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)}
    assert not class_level & {'_rules_lock', '_rulesOverrides', '_rulesBaseline'}
    assert any(isinstance(n, ast.Import) and [a.name for a in n.names] == ['rulestate_helpers'] for n in S_TREE.body)


def test_rulestate_helpers_imports_nothing_and_parses_as_python38():
    src  = _src('rulestate_helpers.py')
    tree = ast.parse(src, feature_version=(3, 8))
    assert not [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert 'madteevee (imports fork, imports.65)' in src


# --- mutation-test additions (imports.65) -------------------------------------------------------

def test_rulestate_error_names_the_missing_value():
    """runActions logs the exception text as an ERROR: it must say which value the table is missing."""
    state = rs.RuleState(dict(rs.baseline(FakeSettings()), lastSubState=False))
    with pytest.raises(AttributeError, match="RuleState has no rule value 'minDuration'"):
        state.minDuration = 1
    assert state.overrides() == {}


def test_settings_refresh_leaves_the_tune_subtitle_state_alone(R):
    """lastSubState is Kodi's subtitle state read at the tune (or rule 51 legacy's value): a settings
    save neither re-reads Kodi's subtitle state nor publishes it."""
    rig = Rig(R)
    rig.builtin.subtitle = False
    _play(rig, _item('A'))
    _play(rig, _item('B', LEGACY_ON))
    assert rig.player.lastSubState is True
    _play(rig, _item('A'))
    assert rig.player.lastSubState is False
    rig.builtin.subtitle = True              # subtitles turned on while A plays
    rig.settings.values['Replay_Percentage'] = 10
    _refresh(rig)
    assert rig.player.lastSubState is False and rig.player.replayPercentage == 10
    rig.player._releaseChannelRules()
    assert rig.player.lastSubState is False


def test_each_tune_uses_the_tuned_channels_own_rules(R):
    """Every tune builds the rule list from the channel being tuned, so a channel whose rules were
    removed since its last tune gets none (no rule list kept from an earlier tune)."""
    rig = Rig(R)
    _play(rig, _item('A', SMART))
    assert rig.player.smartSubsMode == 'smart'
    _play(rig, _item('A'), retune=True)      # Stop, its rules removed, the same channel again
    assert rig.player.smartSubsMode == 'legacy' and rig.player._rulesOverrides == {}
    _play(rig, _item('B', {'54': {'values': {'0': 50}}}))
    _play(rig, _item('A'))
    assert rig.player.replayPercentage == 5 and rig.player._rulesOverrides == {}
