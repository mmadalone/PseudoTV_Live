"""imports.60 (D3): the Kodi glue for Rollback_Watched, and channel rule 53.

services.py can't be imported under the test stubs (overlay.py needs
xbmcgui.ControlList), so its glue is checked by reading the source with ast:
where the snapshot/restore calls sit in the player events. The two methods that
decide something (the live-TV gate _playstateFile and the settings switch
_chkRollbackSwitch) are compiled out of services.py and run against fakes. The
tracker's decisions are tested in test_imports60_playstate.py. Rule 53 runs
for real under the stubs.
"""
import ast
import os
import re
import threading
import types

import pytest

from constants import VFS_TYPES

HERE      = os.path.dirname(os.path.abspath(__file__))
ADDON_LIB = os.path.join(os.path.dirname(HERE), 'resources', 'lib')
SYNC_NEW  = 'self._syncPlaystate(oldInfo, playingItem, bool(self.rollbackPlaycount))'


def _src(name):
    with open(os.path.join(ADDON_LIB, name), encoding='utf-8') as f: return f.read()


SERVICES = _src('services.py')
TREE     = ast.parse(SERVICES, feature_version=(3, 8))


def _method(cls, name):
    for node in TREE.body:
        if isinstance(node, ast.ClassDef) and node.name == cls:
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == name: return item
    raise AssertionError('%s.%s not found' % (cls, name))


def _body(cls, name):
    return ast.get_source_segment(SERVICES, _method(cls, name))


def _function(cls, name, **globals_):
    """The real method from services.py as a plain function (decorators dropped),
    with only the given module globals."""
    node = ast.parse(_body(cls, name)).body[0]
    node.decorator_list = []
    namespace = dict(globals_)
    exec(compile(ast.Module(body=[node], type_ignores=[]), 'services.py', 'exec'), namespace)
    return namespace[name]


def _parents(tree):
    return {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}


def _code_lines(text):
    """Source lines without comments, stripped."""
    return [line.split('#')[0].strip() for line in text.splitlines() if line.split('#')[0].strip()]


def _index(lines, needle, start=0):
    for i in range(start, len(lines)):
        if needle in lines[i]: return i
    raise AssertionError('%r not found' % needle)


def test_services_imports_the_helpers_module():
    assert any(isinstance(n, ast.Import) and any(a.name == 'playstate_helpers' for a in n.names) for n in TREE.body)


def test_tracker_is_created_per_player_with_kodi_io():
    calls = [n.value for n in ast.walk(_method('Player', '__init__')) if isinstance(n, ast.Assign)
             and any(isinstance(t, ast.Attribute) and t.attr == 'playstate' for t in n.targets)]
    assert len(calls) == 1 and isinstance(calls[0], ast.Call)
    assert ast.unparse(calls[0].func) == 'playstate_helpers.PlaystateTracker'
    keywords = {k.arg: ast.unparse(k.value) for k in calls[0].keywords}
    # No `writes=`: the stage switch lives in playstate_helpers (PLAYSTATE_WRITES).
    assert keywords == {'read_details': 'self._readPlaystateDetails', 'playing_file': 'self._playstateFile',
                        'send': 'self.jsonRPC.sendJSON', 'log': 'self._playstateLog'}
    # An instance attribute, not a class-level one shared by every Player.
    player = next(n for n in TREE.body if isinstance(n, ast.ClassDef) and n.name == 'Player')
    assert not any(isinstance(n, ast.Assign) and any(getattr(t, 'id', '') == 'playstate' for t in n.targets) for n in player.body)


def test_sync_runs_in_its_own_thread_and_only_calls_the_tracker():
    node = _method('Player', '_syncPlaystate')
    assert [d.id for d in node.decorator_list if isinstance(d, ast.Name)] == ['threadit']
    assert [a.arg for a in node.args.args] == ['self', 'oldInfo', 'newInfo', 'allow']
    assert all(d is None or (isinstance(d, ast.Constant) and d.value in (None, False)) for d in node.args.defaults)
    assert _code_lines(_body('Player', '_syncPlaystate'))[-1] == 'self.playstate.transition(oldInfo, newInfo, allow)'


def test_gate_compares_the_playing_items_own_path():
    gate = '\n'.join(_code_lines(_body('Player', '_playstateFile')))
    assert 'self.getPlayingItem().getPath() == file' in gate
    assert 'getPlayingFile(' not in gate


class _GatePlayer(object):
    """What the gate asks of xbmc.Player: isPlayingVideo, getPlayingItem().getPath()."""
    def __init__(self, path, video=True, stopped=False):
        self.path, self.video, self.stopped, self.logged = path, video, stopped, []
    def isPlayingVideo(self): return self.video
    def getPlayingItem(self):
        if self.stopped: raise RuntimeError('Kodi is not playing any media file')
        return types.SimpleNamespace(getPath=lambda: self.path)
    def log(self, msg, level=0): self.logged.append(msg)


EPISODE  = '/media/tv/Show/Season 1/Show - S01E02.mkv'
LIVE_PVR = 'pvr://channels/tv/All channels/pvr.iptvsimple_0/50002.pvr'
PLUGIN   = 'plugin://plugin.video.youtube/play/?video_id=x'


@pytest.mark.parametrize('playing, info, expected', [
    (_GatePlayer(EPISODE), {'fitem': {'file': EPISODE}}, EPISODE),                  # catch-up / vod: its own file
    (_GatePlayer(LIVE_PVR), {'fitem': {'file': EPISODE}}, None),                    # live programme: the pvr item plays
    (_GatePlayer(EPISODE, video=False), {'fitem': {'file': EPISODE}}, None),        # not video
    (_GatePlayer(EPISODE), {'fitem': {'file': EPISODE}, 'radio': True}, None),      # radio channel
    (_GatePlayer(PLUGIN), {'fitem': {'file': PLUGIN}}, None),                       # plugin:// is never looked up
    (_GatePlayer(EPISODE, stopped=True), {'fitem': {'file': EPISODE}}, None),       # stopped between the two calls
    (_GatePlayer(EPISODE), {'fitem': {'file': '/media/tv/Other.mkv'}}, None),        # another file plays
    (_GatePlayer(EPISODE), {}, None), (_GatePlayer(EPISODE), None, None),           # no programme file
], ids=['own-file', 'live-pvr', 'not-video', 'radio', 'plugin', 'stopped', 'other-file', 'no-fitem', 'none'])
def test_gate_runs_for_real(playing, info, expected):
    gate = _function('Player', '_playstateFile', VFS_TYPES=VFS_TYPES)
    assert gate(playing, info) == expected
    assert bool(playing.logged) == playing.stopped


def test_reads_are_fresh_and_logs_are_mapped():
    calls = [n for n in ast.walk(_method('Player', '_readPlaystateDetails')) if isinstance(n, ast.Call)
             and ast.unparse(n.func) == 'self.jsonRPC.getFileDetails']
    assert len(calls) == 1
    assert [ast.unparse(a) for a in calls[0].args] == ['file', "'video'", 'list(playstate_helpers.PLAYSTATE_PROPERTIES)']
    assert {k.arg: ast.unparse(k.value) for k in calls[0].keywords} == {'cache': 'False', 'delay': 'False'}
    log = _body('Player', '_playstateLog')
    assert 'playstate_helpers.INFO:xbmc.LOGINFO' in log.replace(' ', '')
    assert 'playstate_helpers.ERROR:xbmc.LOGERROR' in log.replace(' ', '')


def test_handover_to_other_playback_restores_the_last_pseudotv_programme():
    lines = _code_lines(_body('Player', '_onPlay'))
    start = _index(lines, "if not playingItem.get('isPseudoTV'):")
    old   = _index(lines, 'oldInfo = self.playingItem', start)
    new   = _index(lines, 'self.playingItem = playingItem', start)
    sync  = _index(lines, "if oldInfo.get('isPseudoTV'): self._syncPlaystate(oldInfo)", start)
    ret   = _index(lines, 'return', start)
    assert start < old < new < sync < ret


def test_new_channel_syncs_after_the_start_rules_and_before_ui_work():
    lines = _code_lines(_body('Player', '_onPlay'))
    # imports.65: the start rules run (and their values are published) in _applyChannelRules
    rules = _index(lines, 'self.playingItem = self._applyChannelRules(playingItem)')
    sync  = _index(lines, SYNC_NEW, rules)
    assert sync == rules + 1
    assert sync < _index(lines, 'self.setSubtitles(', rules) and sync < _index(lines, 'self.toggleOverlay(self.enableOverlay)', rules)


def test_same_channel_syncs_right_after_the_programme_is_set():
    lines = _code_lines(_body('Player', '_onPlay'))
    branch = _index(lines, 'else:', _index(lines, 'self.toggleOverlay(self.enableOverlay)'))
    assert lines[branch + 1] == 'self.playingItem = playingItem'
    assert lines[branch + 2] == SYNC_NEW
    assert sum(SYNC_NEW in line for line in lines) == 2


def test_programme_end_and_stop_restore():
    change = _code_lines(_body('Player', '_onChange'))
    assert _index(change, "if playingItem.get('isPseudoTV'): self._syncPlaystate(playingItem)") < _index(change, 'PlayMedia')
    stop = _code_lines(_body('Player', '_onStop'))
    assert _index(stop, "if playingItem.get('isPseudoTV'): self._syncPlaystate(playingItem)") < _index(stop, 'RULES_ACTION_PLAYER_STOP')


def _sync_guard(name):
    """The `if playingItem.get('isPseudoTV'): self._syncPlaystate(playingItem)` statement."""
    node = _method('Player', name)
    hits = [n for n in ast.walk(node) if isinstance(n, ast.If)
            and ast.unparse(n) == "if playingItem.get('isPseudoTV'):\n    self._syncPlaystate(playingItem)"]
    assert len(hits) == 1
    return node, hits[0]


def test_end_restore_is_not_nested_under_other_conditions():
    """At a natural end the restore runs for every PseudoTV programme (playlist or
    not); at a stop for every PseudoTV programme Kodi reported."""
    node, guard = _sync_guard('_onChange')
    assert guard in node.body
    node, guard = _sync_guard('_onStop')
    parent = _parents(node)[guard]
    assert isinstance(parent, ast.If) and ast.unparse(parent.test) == 'playingItem' and parent in node.body


# --- the Rollback watched switch ------------------------------------------------------

def test_settings_change_applies_the_switch_at_once_and_debounces_the_rest():
    entry = _method('Monitor', 'onSettingsChanged')
    assert entry.decorator_list == []
    assert _code_lines(_body('Monitor', 'onSettingsChanged')) == [
        'def onSettingsChanged(self):', 'self._chkRollbackSwitch()', 'self._onSettingsChanged()']
    debounced = _method('Monitor', '_onSettingsChanged')
    assert [ast.unparse(d) for d in debounced.decorator_list] == ['debounceit(15)']
    lines = _code_lines(_body('Monitor', '_onSettingsChanged'))
    assert 'self.service._que(self._updatePlayerSettings)' in lines and 'self.service._que(self._updateServiceSettings)' in lines
    init = _code_lines(_body('Monitor', '__init__'))
    assert init.index("self.rollbackSwitch = SETTINGS.getSettingBool('Rollback_Watched')") > init.index('self.player  = Player(self,service)')


def test_debounced_refresh_no_longer_cancels():
    """A cancel on every settings refresh dropped a rule-53 channel's pending
    rollback whenever any setting was saved while the main switch was off."""
    assert not any('clear(' in line or '_cancelPlaystate' in line for line in _code_lines(_body('Monitor', '_updatePlayerSettings')))


def test_cancel_runs_in_its_own_thread():
    node = _method('Player', '_cancelPlaystate')
    assert [d.id for d in node.decorator_list if isinstance(d, ast.Name)] == ['threadit']
    assert _code_lines(_body('Player', '_cancelPlaystate'))[-1] == 'self.playstate.clear()'


class _SwitchPlayer(object):
    """The player side of the switch: imports.65's real _rollbackSwitched, under a real lock."""
    _rollbackSwitched = _function('Player', '_rollbackSwitched')

    def __init__(self, value, overrides=None):
        self.rollbackPlaycount = value
        self._rulesOverrides   = dict(overrides or {})
        self._rules_lock       = threading.Lock()
        self.cancels           = 0

    def _cancelPlaystate(self):
        self.cancels += 1


class _SwitchMonitor(object):
    def __init__(self, before, player_value, overrides=None):
        self.rollbackSwitch = before
        self.player = _SwitchPlayer(player_value, overrides)
    def log(self, msg, level=0): pass


def _switch_check(now):
    settings = types.SimpleNamespace(getSettingBool=lambda key: {'Rollback_Watched': now}[key])
    return _function('Monitor', '_chkRollbackSwitch', SETTINGS=settings)


@pytest.mark.parametrize('before, now, player_value, after_value, cancels', [
    (True, False, True, False, 1),      # switched off: cancel the pending rollback at once
    (False, True, False, True, 0),      # switched on: the next programme is tracked
    (True, True, False, False, 0),      # other setting saved: rule 53's value for this channel stays
    (False, False, True, True, 0),      # (and nothing is cancelled)
], ids=['off', 'on', 'unchanged-on', 'unchanged-off'])
def test_switch_runs_for_real(before, now, player_value, after_value, cancels):
    """A channel without rule 53 (imports.65: through the real Player._rollbackSwitched)."""
    check = _switch_check(now)
    monitor = _SwitchMonitor(before, player_value)
    check(monitor)
    assert monitor.player.rollbackPlaycount is after_value
    assert monitor.player.cancels == cancels
    assert monitor.rollbackSwitch is now


def test_old_rollback_and_duration_calls_are_gone_from_the_player():
    assert not any('quePlaycount' in line for line in _code_lines(SERVICES))
    assert re.search(r"_setRuntime\(playingItem\.get\('fitem',\{\}\),playingItem\.get\('fitem',\{\}\)\.get\('runtime'\)\)", SERVICES)


def test_tune_timestamp_still_stamped_for_the_programme_key():
    assert "'_tune_ts':time.time()" in _src('default.py')


@pytest.mark.parametrize('name', ['services.py', 'jsonrpc.py', 'rules.py', 'builder.py', 'playstate_helpers.py',
                                  os.path.join('parsers', 'MP4Parser.py'), 'rulestate_helpers.py'])
def test_changed_files_pass_the_ast_python38_check(name):
    """Best effort only: ast's feature_version rejects some newer syntax (e.g.
    `match`) but not all of it (3.12 f-string quote reuse, parenthesised `with`
    groups, dict `|`). There is no Python 3.8 run in CI."""
    ast.parse(_src(name), feature_version=(3, 8))


# --- rule 53 (per-channel Rollback Playcount) -----------------------------------------

@pytest.mark.parametrize('setting, rule_value', [(True, False), (False, True)])
def test_rule53_stop_leaves_the_value_and_the_next_channel_gets_the_global(setting, rule_value):
    """runActions re-creates the rules on every call, so the stop step used to
    restore a lost value ([]), switching rollback off on every channel.
    imports.65: the stop does nothing; each tune starts from the settings."""
    import rules
    import rulestate_helpers as rs
    settings = types.SimpleNamespace(getSettingBool=lambda key: setting if key == 'Rollback_Watched' else False,
                                     getSettingInt=lambda key: 0, getSetting=lambda key: '')
    start = dict(rs.baseline(settings), lastSubState=False)
    citem = {'id': 'x@PseudoTV_Live', 'name': 'X', 'rules': {'53': {'values': {'0': rule_value}}}}
    state = rs.RuleState(start)
    rules.RulesList([citem]).runActions(rules.RULES_ACTION_PLAYER_START, citem, {}, inherited=state)
    assert state.rollbackPlaycount is rule_value
    rules.RulesList([citem]).runActions(rules.RULES_ACTION_PLAYER_STOP, citem, {}, inherited=state)   # fresh rules, as live
    assert state.rollbackPlaycount is rule_value and state.overrides() == {'rollbackPlaycount': rule_value}
    other = {'id': 'y@PseudoTV_Live', 'name': 'Y'}
    nxt   = rs.RuleState(start)
    rules.RulesList([other]).runActions(rules.RULES_ACTION_PLAYER_START, other, {}, inherited=nxt)
    assert rs.effective(start, nxt.overrides(), rs.FIELDS)['rollbackPlaycount'] is setting
