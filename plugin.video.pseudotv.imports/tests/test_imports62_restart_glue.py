# -*- coding: utf-8 -*-
"""imports.62: the Kodi glue of 'restart from the beginning'.

services.py and overlay.py can't be imported under the test stubs, so (as in
test_imports60_playstate_glue.py) their structure is checked with ast, and
the restart methods are compiled out of services.py and run against fakes:
a stand-in Player whose restart methods are the real ones. The decisions
themselves are tested in test_imports62_restart_helpers.py. Rule 54, the
jsonrpc reads and context_restart.py run for real under the stubs.
"""
import ast
import json
import os
import sys
import threading
import time
import types
import xml.etree.ElementTree as ET

import pytest

import restart_helpers as rh

HERE      = os.path.dirname(os.path.abspath(__file__))
ADDON     = os.path.dirname(HERE)
ADDON_LIB = os.path.join(ADDON, 'resources', 'lib')


def _src(name):
    with open(os.path.join(ADDON_LIB, name), encoding='utf-8') as f: return f.read()


SERVICES = _src('services.py')
S_TREE   = ast.parse(SERVICES, feature_version=(3, 8))
OVERLAY  = _src('overlay.py')
O_TREE   = ast.parse(OVERLAY, feature_version=(3, 8))


def _class(tree, cls):
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == cls: return node
    raise AssertionError('class %s not found' % cls)


def _method(tree, cls, name):
    for item in _class(tree, cls).body:
        if isinstance(item, ast.FunctionDef) and item.name == name: return item
    raise AssertionError('%s.%s not found' % (cls, name))


def _body(src, tree, cls, name):
    return ast.get_source_segment(src, _method(tree, cls, name))


def _function(src, tree, cls, name, **globals_):
    """The real method as a plain function (decorators dropped), with only the given globals."""
    node = ast.parse(_body(src, tree, cls, name)).body[0]
    node.decorator_list = []
    namespace = dict(globals_)
    exec(compile(ast.Module(body=[node], type_ignores=[]), cls + '.py', 'exec'), namespace)
    return namespace[name]


def _code_lines(text):
    return [line.split('#')[0].strip() for line in text.splitlines() if line.split('#')[0].strip()]


def _index(lines, needle, start=0):
    for i in range(start, len(lines)):
        if needle in lines[i]: return i
    raise AssertionError('%r not found' % needle)


def _decorators(tree, cls, name):
    return [ast.unparse(d) for d in _method(tree, cls, name).decorator_list]


# --- services.py structure -------------------------------------------------------------

def test_services_imports_restart_helpers():
    assert any(isinstance(n, ast.Import) and [a.name for a in n.names] == ['restart_helpers'] for n in S_TREE.body)


def test_new_player_state_is_per_instance():
    init = _code_lines(_body(SERVICES, S_TREE, 'Player', '__init__'))
    for line in ('self._play_epoch     = 0', 'self._retune         = False',
                 'self._pendingRestart = None', 'self._restart_lock   = Lock()'):
        assert line in init
    class_level = {t.id for n in _class(S_TREE, 'Player').body if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)}
    assert not class_level & {'_play_epoch', '_retune', '_pendingRestart', '_restart_lock'}


@pytest.mark.parametrize('name', ['onPlayBackStarted', 'onPlayBackError', 'onPlayBackEnded', 'onPlayBackStopped'])
def test_every_playback_change_moves_the_epoch(name):
    assert sum('self._play_epoch += 1' in line for line in _code_lines(_body(SERVICES, S_TREE, 'Player', name))) == 1


def test_a_stop_marks_the_next_tune_as_a_retune():
    assert 'self._retune      = True' in _code_lines(_body(SERVICES, S_TREE, 'Player', 'onPlayBackStopped'))


def test_stops_and_errors_are_counted_apart():
    assert 'self._play_stops += 1' in _code_lines(_body(SERVICES, S_TREE, 'Player', 'onPlayBackStopped'))
    assert 'self._play_errors += 1' in _code_lines(_body(SERVICES, S_TREE, 'Player', 'onPlayBackError'))
    init = _code_lines(_body(SERVICES, S_TREE, 'Player', '__init__'))
    assert 'self._play_stops     = 0' in init and 'self._play_errors    = 0' in init
    for name in ('onPlayBackStarted', 'onPlayBackEnded', 'onAVStarted'):
        text = _body(SERVICES, S_TREE, 'Player', name)
        assert '_play_stops' not in text and '_play_errors' not in text


def test_the_popup_is_read_once_when_closing_it():
    lines = _code_lines(_body(SERVICES, S_TREE, 'Player', '_closeReplay'))
    assert lines[1:] == ['replay = self.replay', 'if replay is not None: replay.closing = True']
    for name in ('toggleReplay', '_runRestart'):
        assert 'self.replay.closing' not in _body(SERVICES, S_TREE, 'Player', name)


def test_av_started_counts_after_the_stale_event_filter():
    lines = _code_lines(_body(SERVICES, S_TREE, 'Player', 'onAVStarted'))
    stale = _index(lines, 'return')
    stamp = _index(lines, 'self._play_started = time.time()')
    epoch = _index(lines, 'self._play_epoch  += 1')
    call  = _index(lines, 'self._onPlay(')
    assert stale < stamp < epoch < call
    assert lines[call] == "self._onPlay(self.pendingItem.get('item',{}), self._play_epoch)"


def test_onplay_triggers():
    node = _method(S_TREE, 'Player', '_onPlay')
    assert [a.arg for a in node.args.args] == ['self', 'playingItem', 'epoch']
    assert isinstance(node.args.defaults[-1], ast.Constant) and node.args.defaults[-1].value is None
    lines = _code_lines(_body(SERVICES, S_TREE, 'Player', '_onPlay'))
    # LiveTV: after the hand-over restore, before the early return; global setting, not rule 54
    start  = _index(lines, "if not playingItem.get('isPseudoTV'):")
    sync   = _index(lines, "if oldInfo.get('isPseudoTV'): self._syncPlaystate(oldInfo)", start)
    live   = _index(lines, "self.toggleReplay(bool(SETTINGS.getSettingInt('Replay_Percentage')), epoch)", start)
    ret    = _index(lines, 'return', start)
    assert sync < live < ret and lines[live - 1] == 'self._retune = False'
    # Custom: the retune flag is read once; rule 54 can't leak into the next channel
    assert _index(lines, 'retune, self._retune = self._retune, False') > ret
    assert _index(lines, "self.replayPercentage = SETTINGS.getSettingInt('Replay_Percentage')") + 1 == _index(lines, 'RulesList(')
    assert sum(line == 'self.toggleReplay(bool(self.replayPercentage), epoch)' for line in lines) == 1
    assert sum(line == 'if retune: self.toggleReplay(bool(self.replayPercentage), epoch)' for line in lines) == 1
    assert _index(lines, 'if retune: self.toggleReplay(') > _index(lines, 'self.toggleInfo(self.infoOnChange)')
    assert _index(lines, 'self._restartPending(playingItem)') + 1 == _index(lines, 'self.jsonRPC._setRuntime(')
    assert "'epoch'" not in ''.join(lines) and '"epoch"' not in ''.join(lines)


def test_toggle_replay_always_releases_the_dialog():
    assert _decorators(S_TREE, 'Player', 'toggleReplay') == ['debounceit(restart_helpers.POPUP_DELAY)']
    node  = _method(S_TREE, 'Player', 'toggleReplay')
    tries = [n for n in ast.walk(node) if isinstance(n, ast.Try)]
    assert len(tries) == 1
    final = [ast.unparse(s) for s in tries[0].finalbody]
    assert 'self.replay = None' in final
    body  = [ast.unparse(s) for s in tries[0].body]
    assert body[0] == 'dialog.doModal()'
    lines = _code_lines(_body(SERVICES, S_TREE, 'Player', 'toggleReplay'))
    assert _index(lines, 'self.replay, chosen = dialog, False') < _index(lines, 'dialog.doModal()') < _index(lines, 'self.requestRestart(')
    assert 'epoch != self._play_epoch' in ''.join(lines)


def test_popup_starts_soon_after_a_tune():
    # imports.63: the pop-up starts waiting for Kodi's channel info to close about a second
    # after the tune (it waited the 5 s OSD timer first, so it came about 5 s after the info).
    assert isinstance(rh.POPUP_DELAY, float) and 0 < rh.POPUP_DELAY <= 2
    assert 'OSD_TIMER' not in ''.join(_decorators(S_TREE, 'Player', 'toggleReplay'))
    # the window wait still bounds the pop-up by the OSD timer and Kodi's channel info
    assert '!Player.ShowInfo' in rh.WINDOW_READY and '!Player.ChannelPreviewActive' in rh.WINDOW_READY
    assert [ast.unparse(d) for d in _method(S_TREE, 'Player', '_waitRestartWindow').args.defaults] == ['OSD_TIMER * 2']


def test_request_restart_is_threaded_and_one_at_a_time():
    assert _decorators(S_TREE, 'Player', 'requestRestart') == ['threadit']
    lines = _code_lines(_body(SERVICES, S_TREE, 'Player', 'requestRestart'))
    assert 'if not self._restart_lock.acquire(False):' in lines
    assert 'finally: self._restart_lock.release()' in lines


# --- the restart methods, run against fakes --------------------------------------------

RESTART_METHODS = ('_closeReplay', '_restartWait', '_restartOffer', '_restartLiveTarget', '_restartGuideTarget', '_waitRestartWindow',
                   '_restartTuneTs', '_restartFacts', 'requestRestart', '_runRestart', '_restartSeek',
                   '_restartOpen', '_restartTune', '_restartPending', '_restartNote', 'toggleReplay')

BASE  = int(time.time()) // 60 * 60
START = BASE - 600              # the show on now started 10 minutes ago
END   = BASE + 3000


def ts(sec):
    return time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(sec))


def decode(plot):
    """Stand-in for Globals._decodePlot: Custom plots carry an item= payload."""
    return json.loads(plot.split('item=', 1)[1]) if 'item=' in plot else {}


LIVE_PATH    = 'pvr://guide/0042/%s.epg' % ts(START)
LIVE_DETAILS = {'hasarchive': True, 'broadcastnow': {'broadcastid': 777, 'starttime': ts(START), 'endtime': ts(END),
                                                     'plot': 'A show', 'filenameandpath': LIVE_PATH}}
CUSTOM_PATH  = 'pvr://guide/0007/%s.epg' % ts(START)
CUSTOM_ROW   = {'channelid': 7, 'hasarchive': True,
                'broadcastnow': {'broadcastid': 55, 'starttime': ts(START), 'endtime': ts(END),
                                 'plot': 'x item=' + json.dumps({'citem': {'id': 'abc@PseudoTV_Live'}, 'start': START}),
                                 'filenameandpath': CUSTOM_PATH}}


class Rig(object):
    """A stand-in services.Player whose restart methods are the real ones."""

    def __init__(self, **state):
        self.state = {'video': False, 'pseudotv': False, 'bools': {}, 'item': {}, 'tune_ts': None, 'busy': False,
                      'idle': 999, 'ready': True, 'choose': False, 'played': 600, 'duration': 1800}
        self.state.update(state)
        self.calls, self.logs, self.notes, self.dialogs = [], [], [], []
        self.playingItem = self.state.get('info') or {}
        self.replayPercentage, self.maxProgress, self.replay = 5, 98, None
        self._restart_lock, self._pendingRestart, self._play_epoch = threading.Lock(), None, 1
        self._play_stops, self._play_errors = 0, 0
        self.t0, self.t = time.time(), 0.0
        rig = self

        class JSONRPC(object):
            def getPlayerItemRef(self):
                rig.calls.append('GetItem')
                return dict(rig.state['item'])

            def getPVRChannelInfo(self, channelid, properties=('hasarchive',)):
                rig.calls.append(('GetChannelDetails', channelid, tuple(properties)))
                details = rig.state.get('details', LIVE_DETAILS)
                return details if 'broadcastnow' in properties else {'hasarchive': details.get('hasarchive')}

            def getPVRChannelsNow(self):
                rig.calls.append('GetChannels')
                return rig.state.get('channels', [])

            def isBroadcastPlayable(self, bid):
                rig.calls.append(('IsPlayable', bid))
                return rig.state.get('playable', True)

            def playerOpen(self, params={}):
                rig.calls.append(('Open', params))
                rig.t += rig.state.get('open_delay', 0)        # RPC_Delay pause, a busy JSON-RPC lock
                return rig.state.get('open_ok', True)

        class Builtin(object):
            def getInfoBool(self, key, param='Library'): return rig.state['bools'].get('%s.%s' % (param, key), False)
            def isBusyDialog(self): return rig.state['busy']
            def getIdle(self):                 # whole seconds since the last key press
                key = rig.state.get('key_at')
                return int(rig.t - key) if key is not None else int(rig.state['idle'] + rig.t)
            def executewindow(self, key, *args, **kwargs): rig.calls.append(('window', key))

        class Dialog(object):
            def notificationDialog(self, message, show=None, **kwargs): rig.notes.append((message, show))

        class Service(object):
            def _shutdown(self, wait=0):
                rig.t += wait
                if rig.state.get('on_poll'): rig.state['on_poll'](rig)
                return rig.t > 120

        class Replay(object):
            def __init__(self, *args, **kwargs):
                self.args, self.kwargs, self.restart, self.closing = args, kwargs, False, False
                rig.dialogs.append(self)

            def doModal(self):
                if rig.state.get('modal_raises'): raise RuntimeError('dialog failed')
                rig.state['replay_seen'] = rig.replay is self
                self.restart = rig.state['choose']

        class ListItem(object):
            def getProperty(self, key):
                return json.dumps({'_tune_ts': rig.state['tune_ts']}) if key == 'sysInfo' and rig.state['tune_ts'] else ''

        self._ListItem = ListItem
        self.jsonRPC, self.service = JSONRPC(), Service()
        g = dict(restart_helpers=rh, BUILTIN=Builtin(), DIALOG=Dialog(), LANGUAGE=lambda i: 'L%s' % i,
                 SETTINGS=types.SimpleNamespace(getSettingInt=lambda key: 5),
                 Globals=types.SimpleNamespace(_decodePlot=decode),
                 FileAccess=types.SimpleNamespace(_decodeString=lambda s: json.loads(s) if s else ''),
                 xbmc=types.SimpleNamespace(LOGDEBUG=0, LOGINFO=1, LOGWARNING=2, LOGERROR=3,
                                            getCondVisibility=lambda cond: rig.state['ready'] and cond == rh.WINDOW_READY),
                 time=types.SimpleNamespace(time=lambda: rig.t0 + rig.t),
                 OSD_TIMER=5, Replay=Replay, REPLAY_XML='restart.xml', ADDON_PATH='/addon')
        for name in RESTART_METHODS:
            setattr(self, name, types.MethodType(_function(SERVICES, S_TREE, 'Player', name, **g), self))

    # the rest of the Player the restart methods use
    def log(self, msg, level=0): self.logs.append((level, msg))
    def isPlayingPseudoTV(self): return self.state['pseudotv']
    def isPlayingVideo(self): return self.state['video']
    def isPlaying(self): return self.state['video']
    def getPlayedTime(self): return self.state['played']
    def getPlayerTime(self): return self.state['duration']
    def seekTime(self, sec):
        if self.state.get('seek_raises'): raise RuntimeError('nothing plays')
        self.calls.append(('seek', sec))
    def stop(self):
        self.calls.append('stop')
        self.state['video'] = False
    def getPlayingItem(self):
        if not self.state['video']: raise RuntimeError('Kodi is not playing any media file')
        return self._ListItem()

    def run(self, request):
        self.requestRestart(request)
        return self

    def actions(self):
        return [m for level, m in self.logs if m.startswith('requestRestart, source')]

    def outcomes(self):
        return [m.split(': ', 1)[1] for level, m in self.logs if m.startswith('_restartOpen, broadcast')]


def custom_info(start=START, chid='abc@PseudoTV_Live', tune_ts=1.5):
    return {'isPseudoTV': True, 'chid': chid, 'fitem': {'start': start}, 'isfiller': False, '_tune_ts': tune_ts}


def live(**kw):
    state = dict(video=True, bools={'Pvr.IsPlayingTv': True}, item={'type': 'channel', 'id': 42})
    state.update(kw)
    return state


def custom(**kw):
    state = dict(video=True, pseudotv=True, info=custom_info(), tune_ts=1.5, bools={'Player.SeekEnabled': True})
    state.update(kw)
    return state


def opens(rig):
    return [c for c in rig.calls if isinstance(c, tuple) and c[0] == 'Open']


def test_remote_on_a_custom_channel_jumps_to_the_start_in_place():
    rig = Rig(**custom()).run({'source': 'remote'})
    assert ('seek', 0) in rig.calls and not opens(rig) and not rig.notes
    assert 'action = seek_start' in rig.actions()[0]


def test_remote_during_a_zap_waits_for_it_to_settle():
    def settle(rig): rig.state['tune_ts'] = 1.5
    rig = Rig(**custom(tune_ts=9.9, on_poll=settle)).run({'source': 'remote'})
    assert ('seek', 0) in rig.calls and not rig.notes and rig.t == rh.SETTLE_WAIT


def test_remote_during_a_zap_that_never_settles_says_unavailable():
    rig = Rig(**custom(tune_ts=9.9)).run({'source': 'remote'})
    assert ('seek', 0) not in rig.calls and rig.notes == [('L30154', True)]
    assert 'action = unavailable' in rig.actions()[0] and rig.t == rh.SETTLE_TRIES * rh.SETTLE_WAIT


def test_remote_on_a_filler_or_unseekable_programme_says_unavailable():
    rig = Rig(**custom(info=dict(custom_info(), isfiller=True))).run({'source': 'remote'})
    assert rig.notes == [('L30154', True)] and ('seek', 0) not in rig.calls
    rig = Rig(**custom(bools={})).run({'source': 'remote'})
    assert rig.notes == [('L30154', True)] and ('seek', 0) not in rig.calls


def test_remote_on_live_tv_opens_the_broadcast_and_sees_it_start():
    def start_replay(rig):
        rig.state['bools'] = {'Pvr.IsPlayingEpgTag': True}
        rig.state['item'] = {'type': 'unknown', 'id': 777}
    rig = Rig(**live(on_poll=start_replay)).run({'source': 'remote'})
    assert opens(rig) == [('Open', {'item': {'broadcastid': 777}})] and rig.outcomes() == ['started'] and not rig.notes
    reads = [c for c in rig.calls if isinstance(c, tuple) and c[0] == 'GetChannelDetails']
    assert reads == [('GetChannelDetails', 42, ('hasarchive',)), ('GetChannelDetails', 42, ('broadcastnow', 'hasarchive'))]
    assert ('IsPlayable', 777) in rig.calls


def test_remote_on_live_tv_refused_keeps_live_and_says_so():
    rig = Rig(**live()).run({'source': 'remote'})
    assert rig.outcomes() == ['refused'] and rig.notes == [('L30154', True)] and len(opens(rig)) == 1


def _fail_open(rig):
    if rig.state['video']:
        rig.state['video'] = False
        rig.state['bools'] = {}
        rig._play_errors += 1              # Kodi: onPlayBackError (the replay failed to open)


def test_remote_on_live_tv_tunes_the_channel_again_when_the_replay_fails_to_open():
    rig = Rig(**live(on_poll=_fail_open)).run({'source': 'remote'})
    assert opens(rig) == [('Open', {'item': {'broadcastid': 777}}), ('Open', {'item': {'channelid': 42}})]
    assert rig.outcomes() == ['retune'] and rig.notes == [('L30154', True)] and rig.t == rh.ERROR_SETTLE


def test_remote_on_live_tv_gone_without_an_error_is_not_tuned_again():
    def gone(rig):
        rig.state['video'] = False
        rig.state['bools'] = {}
    rig = Rig(**live(on_poll=gone)).run({'source': 'remote'})
    assert opens(rig) == [('Open', {'item': {'broadcastid': 777}})] and rig.outcomes() == ['failed'] and rig.notes


def test_remote_on_live_tv_respects_a_stop_from_a_remote_app():
    def stop(rig):
        if rig.state['video']:
            rig.state['video'] = False
            rig._play_stops += 1           # Player.Stop over JSON-RPC: the idle time is not reset
    rig = Rig(**live(on_poll=stop)).run({'source': 'remote'})
    assert opens(rig) == [('Open', {'item': {'broadcastid': 777}})] and rig.outcomes() == ['gave_up'] and not rig.notes


def test_remote_on_live_tv_never_retunes_after_a_key_press():
    def fail_then_key(rig):
        _fail_open(rig)
        rig.state.setdefault('key_at', rig.t)   # the viewer pressed a key meanwhile
    rig = Rig(**live(on_poll=fail_then_key)).run({'source': 'remote'})
    assert opens(rig) == [('Open', {'item': {'broadcastid': 777}})] and rig.outcomes() == ['gave_up'] and not rig.notes


def test_the_replay_watch_starts_when_player_open_returns():
    rig = Rig(**live(open_delay=12)).run({'source': 'remote'})       # a slow JSON-RPC lock before the open returned
    assert rig.outcomes() == ['refused'] and rig.t >= 12 + rh.WATCH_GRACE


def test_remote_on_a_channel_without_replays_reads_no_show_on_now():
    rig = Rig(**live(details={'hasarchive': False})).run({'source': 'remote'})
    assert rig.notes == [('L30154', True)] and not opens(rig)
    assert all('broadcastnow' not in c[2] for c in rig.calls if isinstance(c, tuple) and c[0] == 'GetChannelDetails')


def test_remote_with_nothing_playing_says_unavailable():
    rig = Rig().run({'source': 'remote'})
    assert rig.notes == [('L30154', True)] and not opens(rig) and 'GetItem' not in rig.calls


def test_remote_on_a_playing_replay_jumps_to_its_start():
    rig = Rig(video=True, bools={'Pvr.IsPlayingEpgTag': True, 'Player.SeekEnabled': True},
              item={'type': 'unknown', 'id': 777}).run({'source': 'remote'})
    assert ('seek', 0) in rig.calls and not opens(rig) and not rig.notes


def test_a_failed_seek_is_logged_not_raised():
    rig = Rig(**custom(seek_raises=True)).run({'source': 'remote'})
    assert any('_restartSeek, failed!' in m for _, m in rig.logs) and not rig.notes


def test_guide_entry_on_a_custom_channel_that_is_not_playing_tunes_then_jumps_to_the_start():
    rig = Rig(channels=[CUSTOM_ROW]).run({'source': 'guide', 'path': CUSTOM_PATH})
    assert opens(rig) == [('Open', {'item': {'channelid': 7}})] and rig._pendingRestart['chid'] == 'abc@PseudoTV_Live'
    rig.state.update(video=True, bools={'Player.SeekEnabled': True})
    rig._restartPending(custom_info(chid='other@PseudoTV_Live'))          # another channel: keep waiting
    assert rig._pendingRestart is not None and ('seek', 0) not in rig.calls
    rig._restartPending(custom_info())
    assert ('seek', 0) in rig.calls and rig._pendingRestart is None


def test_a_pending_guide_restart_expires():
    rig = Rig(channels=[CUSTOM_ROW]).run({'source': 'guide', 'path': CUSTOM_PATH})
    rig.t += rh.PENDING_TTL + 1
    rig._restartPending(custom_info(start=START + 1800))
    assert rig._pendingRestart is None and ('seek', 0) not in rig.calls


def test_guide_entry_when_kodi_plays_that_channel_with_another_programme_stops_first():
    rig = Rig(**custom(channels=[CUSTOM_ROW], info=custom_info(start=START - 1800),
                       bools={'Pvr.IsPlayingTv': True}, item={'type': 'channel', 'id': 7})).run(
        {'source': 'guide', 'path': CUSTOM_PATH})
    assert rig.calls.index('stop') < rig.calls.index(opens(rig)[0]) and opens(rig) == [('Open', {'item': {'channelid': 7}})]


def test_guide_entry_on_the_custom_programme_that_plays_jumps_to_the_start_in_full_screen():
    rig = Rig(**custom(channels=[CUSTOM_ROW])).run({'source': 'guide', 'path': CUSTOM_PATH})
    assert ('seek', 0) in rig.calls and ('window', 'ActivateWindow(fullscreenvideo)') in rig.calls and not opens(rig)


def test_guide_entry_tune_failure_clears_the_pending_restart():
    rig = Rig(channels=[CUSTOM_ROW], open_ok=False).run({'source': 'guide', 'path': CUSTOM_PATH})
    assert rig._pendingRestart is None and rig.notes == [('L30154', True)]


def test_guide_entry_on_live_tv_while_another_channel_plays_refused_says_so():
    row = dict(LIVE_DETAILS, channelid=42)
    rig = Rig(**live(item={'type': 'channel', 'id': 43}, channels=[row])).run({'source': 'guide', 'path': LIVE_PATH})
    assert opens(rig) == [('Open', {'item': {'broadcastid': 777}})] and rig.outcomes() == ['refused'] and rig.notes


def test_guide_entry_on_live_tv_with_nothing_playing_never_tunes_a_channel():
    row = dict(LIVE_DETAILS, channelid=42)
    rig = Rig(channels=[row]).run({'source': 'guide', 'path': LIVE_PATH})
    assert opens(rig) == [('Open', {'item': {'broadcastid': 777}})] and rig.outcomes() == ['failed'] and rig.notes


def test_guide_entry_that_is_no_longer_on_now_says_unavailable():
    rig = Rig(channels=[CUSTOM_ROW]).run({'source': 'guide', 'path': 'pvr://guide/0007/2001-01-01 00:00:00.epg'})
    assert rig.notes == [('L30154', True)] and not opens(rig)


def test_a_second_request_while_one_runs_is_dropped():
    rig = Rig()
    rig._restart_lock.acquire()
    rig.requestRestart({'source': 'remote'})
    assert not rig.notes and any('already running' in m for _, m in rig.logs)


def test_a_restart_closes_an_open_popup():
    rig = Rig(**custom())
    rig.replay = types.SimpleNamespace(closing=False)
    dialog = rig.replay
    rig.run({'source': 'remote'})
    assert dialog.closing is True


def test_popup_targets():
    rig = Rig(**custom())
    assert rig._restartOffer() == {'kind': 'custom', 'chid': 'abc@PseudoTV_Live', 'start': START}
    assert Rig(**custom(played=60))._restartOffer() is None                            # 3%: below 5%
    assert Rig(**custom(played=1780))._restartOffer() is None                          # 99%: above Seek_Threshold
    assert Rig(**custom(info=dict(custom_info(), isfiller=True)))._restartOffer() is None
    target = Rig(**live())._restartOffer()
    assert target['broadcastid'] == 777 and target['playable'] is True
    assert Rig(**live(playable=False))._restartOffer() is None
    assert Rig(**live(details={'hasarchive': False}))._restartOffer() is None
    assert Rig(**live(item={'type': 'unknown', 'id': 777}))._restartOffer() is None   # a replay, not live
    zapped = Rig(**live(item={'type': 'channel', 'id': 43})).run({'source': 'popup', 'target': target})
    assert not opens(zapped) and 'action = unavailable' in zapped.actions()[0] and zapped.notes == [('L30154', True)]


def test_popup_ok_restarts_through_the_shared_action():
    rig = Rig(**custom(choose=True))
    rig.toggleReplay(True, 1)
    assert len(rig.dialogs) == 1 and rig.state['replay_seen'] is True and rig.replay is None
    assert rig.dialogs[0].kwargs == {'player': rig, 'epoch': 1, 'end': None}
    assert ('seek', 0) in rig.calls
    rig = Rig(**live(choose=True, on_poll=lambda r: r.state.update(bools={'Pvr.IsPlayingEpgTag': True},
                                                                   item={'type': 'unknown', 'id': 777})))
    rig.toggleReplay(True, 1)
    assert rig.dialogs[0].kwargs['end'] == END and opens(rig) == [('Open', {'item': {'broadcastid': 777}})]


def test_popup_dismissed_does_nothing_and_can_show_again():
    rig = Rig(**custom())
    rig.toggleReplay(True, 1)
    rig.toggleReplay(True, 1)
    assert len(rig.dialogs) == 2 and rig.replay is None and ('seek', 0) not in rig.calls


def test_popup_not_offered_for_old_playback_a_busy_screen_or_while_one_is_open():
    rig = Rig(**custom(choose=True))
    rig.toggleReplay(True, 0)                       # playback changed since the tune
    assert not rig.dialogs
    rig = Rig(**custom(choose=True, ready=False))   # channel info / a dialog never went away
    rig.toggleReplay(True, 1)
    assert not rig.dialogs and rig.t >= 10
    rig = Rig(**custom(choose=True))
    rig.replay = types.SimpleNamespace(closing=False)
    rig.toggleReplay(True, 1)
    assert not rig.dialogs
    rig = Rig(**custom(choose=True))
    rig.toggleReplay(False, 1)
    assert not rig.dialogs and not rig.calls


def test_window_wait_stops_when_playback_changes():
    def zap(rig): rig._play_epoch += 1
    rig = Rig(ready=False, on_poll=zap)
    assert rig._waitRestartWindow(1) is False and rig.t == 0.25
    assert Rig(ready=True)._waitRestartWindow(1) is True


def test_popup_close_request_reaches_the_open_dialog():
    rig = Rig()
    rig.replay = types.SimpleNamespace(closing=False)
    rig.toggleReplay(False)
    assert rig.replay.closing is True


def test_popup_failure_still_releases_the_dialog():
    rig = Rig(**custom(choose=True, modal_raises=True))
    with pytest.raises(RuntimeError):
        rig.toggleReplay(True, 1)
    assert rig.replay is None and ('seek', 0) not in rig.calls


# --- overlay.Replay ------------------------------------------------------------------------

def test_replay_is_a_passive_view():
    init = _body(OVERLAY, O_TREE, 'Replay', '__init__')
    assert 'self.service = self.player.service' in [ast.unparse(s) for s in _method(O_TREE, 'Replay', '__init__').body]
    assert 'doModal' not in init and 'getPlayerProgress' not in init
    on_init = _method(O_TREE, 'Replay', 'onInit')
    assert len([n for n in ast.walk(on_init) if isinstance(n, ast.While)]) == 1
    text = ast.unparse(on_init)
    assert 'self.service._shutdown(restart_helpers.POPUP_POLL)' in text and 'restart_helpers.popup_done(' in text
    assert 'isPlayingPseudoTV' not in text
    assert 'AlarmClock' not in ast.unparse(_class(O_TREE, 'Replay'))                          # code, not comments
    assert not [n for n in _class(O_TREE, 'Replay').body if isinstance(n, ast.Assign)]       # no class-level state


@pytest.fixture
def on_action():
    sent = []

    def timerit(method):
        return lambda wait, *args: sent.append((wait,) + args)

    def make(live_tv=True):
        g = dict(ACTION_SELECT_ITEM=[7, 135], ACTION_PAGE_UP=5, ACTION_PAGE_DOWN=6, restart_helpers=rh,
                 timerit=timerit, BUILTIN=types.SimpleNamespace(executebuiltin=lambda *a: None,
                                                                getInfoBool=lambda key, param='Library': live_tv and (key, param) == ('IsPlayingTv', 'Pvr')))
        return _function(OVERLAY, O_TREE, 'Replay', 'onAction', **g)

    def press(action_id, focus=40001, live_tv=True):
        view = types.SimpleNamespace(closing=False, restart=False, log=lambda *a: None, getFocusId=lambda: focus)
        make(live_tv)(view, types.SimpleNamespace(getId=lambda: action_id))
        return view
    press.sent = sent
    return press


def test_replay_keys(on_action):
    view = on_action(7)
    assert view.restart is True and view.closing is True and on_action.sent == []
    assert on_action(7, focus=0).restart is False
    on_action(3); on_action(4)
    assert on_action.sent == [(0.5, 'Action(Up)'), (0.5, 'Action(Down)')]
    del on_action.sent[:]
    on_action(5); on_action(6)
    assert on_action.sent == [(0.5, 'Action(ChannelUp)'), (0.5, 'Action(ChannelDown)')]
    del on_action.sent[:]
    on_action(5, live_tv=False)
    for key in (92, 10, 184, 1, 2, 117):  # Back, Previous menu, ChannelUp, Left, Right, Context menu: only close it
        assert on_action(key).closing is True
    assert on_action.sent == []
    on_action(11); on_action(59); on_action(64)
    assert on_action.sent == [(0.5, 'Action(Info)'), (0.5, 'Action(Number1)'), (0.5, 'Action(Number6)')]


# --- Monitor.onNotification -------------------------------------------------------------------

@pytest.mark.parametrize('sender, method, data, source', [
    ('pseudotv', 'Other.restart', 'null', 'remote'),
    ('pseudotv', 'Other.restart', json.dumps({'path': LIVE_PATH}), 'guide'),
    ('plugin.video.pseudotv.imports', 'Other.restart', 'null', None),
    ('pseudotv', 'Other.upnext_data', 'null', None),
    ('xbmc', 'Player.OnPlay', '{}', None),
])
def test_on_notification(sender, method, data, source):
    fn = _function(SERVICES, S_TREE, 'Monitor', 'onNotification', restart_helpers=rh)
    requests, logged = [], []
    monitor = types.SimpleNamespace(log=logged.append, player=types.SimpleNamespace(requestRestart=requests.append))
    fn(monitor, sender, method, data)
    assert len(logged) == 1
    if source is None: assert requests == []
    else: assert [r['source'] for r in requests] == [source]


# --- rule 54 ('Restart Button') ----------------------------------------------------------------

class _Player(object):
    replayPercentage = None


def test_rule54_stop_restores_the_global_value(monkeypatch):
    """runActions re-creates the rules on every call, so the stop step used to restore a lost
    value ([]), switching the pop-up off on every channel after one stop on a rule-54 channel."""
    import rules
    real = rules.SETTINGS.getSettingInt
    monkeypatch.setattr(rules.SETTINGS, 'getSettingInt', lambda key: 5 if key == 'Replay_Percentage' else real(key))
    citem  = {'id': 'x@PseudoTV_Live', 'name': 'X', 'rules': {'54': {'values': {'0': 50}}}}
    player = _Player()
    player.replayPercentage = 5
    run = rules.RulesList([citem]).runActions
    run(rules.RULES_ACTION_PLAYER_START, citem, {}, inherited=player)
    assert player.replayPercentage == 50
    run(rules.RULES_ACTION_PLAYER_STOP, citem, {}, inherited=player)
    assert player.replayPercentage == 5


# --- jsonrpc reads -----------------------------------------------------------------------------

@pytest.fixture
def rpc(monkeypatch):
    from jsonrpc import JSONRPC
    sent, replies = [], {}
    client = JSONRPC(service=types.SimpleNamespace())

    def send(param, delay=True):
        sent.append((param, delay))
        return replies.get(param['method'], {})
    monkeypatch.setattr(client, 'sendJSON', send)
    return types.SimpleNamespace(client=client, sent=sent, replies=replies)


def _keys(value):
    if isinstance(value, dict):
        for k, v in value.items():
            yield k
            yield from _keys(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _keys(v)
            if isinstance(v, str): yield v


def test_jsonrpc_reads_send_exactly_the_planned_requests(rpc):
    rpc.replies.update({'Player.GetItem': {'result': {'item': {'type': 'channel', 'id': 42, 'label': 'X'}}},
                        'PVR.GetChannelDetails': {'result': {'channeldetails': {'channelid': 42, 'hasarchive': True}}},
                        'PVR.GetChannels': {'result': {'channels': [{'channelid': 42}]}},
                        'PVR.GetBroadcastIsPlayable': {'result': True}})
    c = rpc.client
    assert c.getPlayerItemRef() == {'type': 'channel', 'id': 42, 'label': 'X'}
    assert c.getPVRChannelInfo(42) == {'channelid': 42, 'hasarchive': True}
    assert c.getPVRChannelInfo(42, ('broadcastnow', 'hasarchive'))['hasarchive'] is True
    assert c.getPVRChannelsNow() == [{'channelid': 42}]
    assert c.isBroadcastPlayable(777) is True
    assert rpc.sent == [
        ({'method': 'Player.GetItem', 'params': {'playerid': 1, 'properties': []}}, False),
        ({'method': 'PVR.GetChannelDetails', 'params': {'channelid': 42, 'properties': ['hasarchive']}}, False),
        ({'method': 'PVR.GetChannelDetails', 'params': {'channelid': 42, 'properties': ['broadcastnow', 'hasarchive']}}, False),
        ({'method': 'PVR.GetChannels', 'params': {'channelgroupid': 'alltv', 'properties': ['broadcastnow', 'hasarchive']}}, False),
        ({'method': 'PVR.GetBroadcastIsPlayable', 'params': {'broadcastid': 777}}, False)]
    for param, _ in rpc.sent:
        assert not set(_keys(param)) & {'file', 'dynpath', 'mediapath', 'List.Fields.All'}


def test_jsonrpc_reads_survive_errors_and_empty_replies(rpc):
    rpc.replies.update({'Player.GetItem': {'result': {'error': {'code': -32100}}},
                        'PVR.GetBroadcastIsPlayable': {'result': {'error': {'code': -32602}}}})
    c = rpc.client
    assert c.getPlayerItemRef() == {} and c.getPVRChannelInfo(42) == {} and c.getPVRChannelsNow() == []
    assert c.isBroadcastPlayable(777) is False


# --- settings, strings, addon.xml, context_restart.py ----------------------------------------

def test_restart_percentage_defaults_to_5():
    root = ET.parse(os.path.join(ADDON, 'resources', 'settings.xml')).getroot()
    setting = root.find(".//setting[@id='Replay_Percentage']")
    assert setting.findtext('default') == '5'
    assert (setting.findtext('constraints/minimum'), setting.findtext('constraints/step'), setting.findtext('constraints/maximum')) == ('0', '5', '95')
    assert setting.get('label') == '30153' and setting.get('help') == '33153'


def _strings():
    with open(os.path.join(ADDON, 'resources', 'language', 'resource.language.en_gb', 'strings.po'), encoding='utf-8') as f:
        text = f.read()
    out = {}
    for block in text.split('\n\n'):
        ctx = [l for l in block.splitlines() if l.startswith('msgctxt "#')]
        mid = [l for l in block.splitlines() if l.startswith('msgid "')]
        if ctx and mid: out[ctx[0][len('msgctxt "#'):-1]] = mid[0][len('msgid "'):-1]
    return out


def test_strings():
    strings = _strings()
    assert strings['30909'] == 'Restart from beginning'
    assert '5% Default' in strings['33153'] and 'LiveTV' in strings['33153']
    assert strings['30154'] == 'Restart Unavailable...'


def test_addon_xml_guide_entry():
    root = ET.parse(os.path.join(ADDON, 'addon.xml')).getroot()
    # imports.63: any later imports version keeps the guide entry (was pinned to .62)
    prefix, _, number = root.get('version').rpartition('.')
    assert prefix == '0.8.0+imports' and number.isdigit() and int(number) >= 62
    main = root.find("./extension[@point='kodi.context.item']/menu[@id='kodi.core.main']")
    top = main.findall('./item')
    assert len(top) == 1
    item = top[0]
    assert item.get('library') == 'resources/lib/context_restart.py' and item.get('args') is None
    assert item.findtext('label') == '30909'
    assert item.findtext('visible') == ('[Window.IsVisible(tvguide) | Window.IsVisible(pvrchannelguide)]'
                                        ' + ListItem.InProgress + ListItem.IsPlayable')
    assert os.path.isfile(os.path.join(ADDON, item.get('library')))
    old = [(i.get('library'), i.get('args'), i.findtext('label')) for i in main.findall('./menu/item')]
    assert len(old) == 9 and ('resources/lib/context_record.py', 'add', '30115') in old


@pytest.fixture
def context_restart(monkeypatch):
    import context_restart as module
    sent = []
    monkeypatch.setattr(module.xbmc, 'executeJSONRPC', lambda request: sent.append(json.loads(request)) or '{}')
    module.sent = sent
    return module


def test_context_restart_sends_the_guide_path(context_restart, monkeypatch):
    monkeypatch.setattr(sys, 'listitem', types.SimpleNamespace(getPath=lambda: LIVE_PATH), raising=False)
    context_restart._run()
    assert context_restart.sent == [{'jsonrpc': '2.0', 'id': 1, 'method': 'JSONRPC.NotifyAll',
                                     'params': {'sender': 'pseudotv', 'message': 'restart', 'data': {'path': LIVE_PATH}}}]
    assert rh.parse_notification('pseudotv', 'Other.restart', json.dumps(context_restart.sent[0]['params']['data'])) == {'path': LIVE_PATH}


@pytest.mark.parametrize('path', ['pvr://channels/tv/All channels@-1/50002@pvr.iptvsimple_1.pvr',
                                  'pvr://recordings/tv/active/x.pvr', '', None])
def test_context_restart_ignores_other_items(context_restart, monkeypatch, path):
    monkeypatch.setattr(sys, 'listitem', types.SimpleNamespace(getPath=lambda: path), raising=False)
    context_restart._run()
    assert context_restart.sent == []


def test_context_restart_without_a_listitem(context_restart, monkeypatch):
    monkeypatch.delattr(sys, 'listitem', raising=False)
    context_restart._run()
    assert context_restart.sent == []


def test_context_restart_is_light():
    tree  = ast.parse(_src('context_restart.py'), feature_version=(3, 8))
    names = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    names |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert names == {'json', 'sys', 'kodi_six', 'restart_helpers', 'logger'}


@pytest.mark.parametrize('name', ['services.py', 'overlay.py', 'jsonrpc.py', 'rules.py', 'constants.py',
                                  'restart_helpers.py', 'context_restart.py'])
def test_changed_files_pass_the_ast_python38_check(name):
    """Best effort only (ast's feature_version doesn't reject every newer syntax)."""
    ast.parse(_src(name), feature_version=(3, 8))
