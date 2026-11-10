# -*- coding: utf-8 -*-
"""imports.67: LiveTV subtitles follow the channel -- the Kodi glue in services.py.

On LiveTV (imported) channels, subtitles you had turned on were off again as
soon as a replay started: the add-on's RESTART pop-up (imports.62) and Kodi's
"Play next programme automatically" each open a replay, a new video for Kodi,
and Kodi starts a video it has no saved settings for with its default
(subtitles off on this TV). The add-on never touched LiveTV subtitles:
_onPlay's non-PseudoTV branch only read BUILTIN.isSubtitle() and returned.

imports.67 remembers the on/off per LiveTV channel and applies it whenever
that channel plays. The decisions live in livesubs_helpers (tested in
test_imports67_livesubs_helpers.py); services.py only reads Kodi, acts and
keeps the memory file:
  - Player._restoreLiveSubtitles, last in _onPlay's LiveTV branch (Kodi's
    onAVStarted, on the @threadit worker): resolves the channel from a fresh
    channel list (_liveSubsChannels), applies its choice, re-checks once after
    RECHECK_S (comparing Kodi's channel labels), waits APPLY_WAIT_S after a
    re-apply, then settles the playback: its epoch, key, labels, playing file
    (_liveSubsFile), whether it has a track, seed and baseline;
  - Player._showLiveSubtitles, every switch: "on" re-selects the current
    track (JSON-RPC Player.GetProperties, then setSubtitleStream), the flag
    only when Kodi lists no track; "off" is the flag;
  - Player._chkLiveSubtitles, on Monitor._chkIdle's ~1 s not-Custom tick (a
    subtitle toggle fires no player event): only for the settled labels and
    file, applies the choice once to a track that appears late, else saves a
    change you make;
  - _loadLiveSubs / _saveLiveSubs / _liveSubsPath: cache/livetv_subtitles.json
    through FileAccess (setJSON is crash-safe, imports.59 S3).

services.py can't be imported under the test stubs, so (as in
test_imports65_channel_rules.py) the real methods are compiled out of the
source with their decorators dropped and run on a fake player. Kodi is a
dict (the six reads _liveSubsInfo makes, the playing file, the subtitle
tracks JSON-RPC lists); a switch lands at once or, queued, at the next wait
or tick; the monitor's waitForAbort runs whatever happens in Kodi during a
wait; the channel list is the fake service's tasks.getChannels; and the
memory file is written for real (the real FileAccess) into a tmp folder. The
structure (where the calls sit, who writes what) is checked with ast.
"""
import ast
import json
import os
import types

import pytest

import livesubs_helpers as lh
import xbmc
from globals import FileAccess   # first: fileaccess can't be the first addon module imported (circular)

HERE      = os.path.dirname(os.path.abspath(__file__))
ADDON_LIB = os.path.join(os.path.dirname(HERE), 'resources', 'lib')


def _src(name):
    with open(os.path.join(ADDON_LIB, name), encoding='utf-8') as f: return f.read()


SERVICES = _src('services.py')
S_TREE   = ast.parse(SERVICES, feature_version=(3, 8))


def _class(cls):
    for node in S_TREE.body:
        if isinstance(node, ast.ClassDef) and node.name == cls: return node
    raise AssertionError('class %s not found' % cls)


def _method(cls, name):
    for item in _class(cls).body:
        if isinstance(item, ast.FunctionDef) and item.name == name: return item
    raise AssertionError('%s.%s not found' % (cls, name))


def _function(cls, name, **globals_):
    """The real method from services.py as a plain function (decorators dropped), with only the given globals."""
    node = ast.parse(ast.get_source_segment(SERVICES, _method(cls, name))).body[0]
    node.decorator_list = []
    namespace = dict(globals_)
    # The add-on's own source, compiled from the repo: the pattern of the imports.60-.65 glue tests.
    exec(compile(ast.Module(body=[node], type_ignores=[]), 'services.py', 'exec'), namespace)  # noqa: S102
    return namespace[name]


def _owner_map():
    parents = {child: node for node in ast.walk(S_TREE) for child in ast.iter_child_nodes(node)}

    def owner(n):
        names = []
        while n in parents:
            n = parents[n]
            if isinstance(n, (ast.FunctionDef, ast.ClassDef)): names.append(n.name)
        return tuple(reversed(names))
    return owner


def _calls(node, attr):
    """The calls of a method named `attr` (self.x(...), module.x(...)) under `node`."""
    return [n for n in ast.walk(node) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == attr]


# --- the channels and what Kodi shows ------------------------------------------------------------

NEOX      = 'NEOX@movistarplus'                                  # Neox, 8
WARNER    = 'TNT@movistarplus'                                   # Warner TV, 12
CN        = 'CartoonNetwork.us@epgbest_46477'                    # Cartoon Network, 303 (imported)
CN_CUSTOM = '3330322e436172746f6f6e204e657477@PseudoTV_Live'     # an own (Custom) "Cartoon Network", 304

CHANNELS = [
    {'id': NEOX,      'type': 'import', 'number': 8,   'name': 'Neox',            'enabled': True},
    {'id': WARNER,    'type': 'import', 'number': 12,  'name': 'Warner TV',       'enabled': True},
    {'id': CN,        'type': 'import', 'number': 303, 'name': 'Cartoon Network', 'enabled': True},
    {'id': CN_CUSTOM, 'type': 'Custom', 'number': 304, 'name': 'Cartoon Network', 'enabled': True},
]

# Kodi's labels: Pvr.IsPlayingTv (live), Pvr.IsPlayingEpgTag (a replay), VideoPlayer.ChannelNumberLabel,
# VideoPlayer.ChannelName -- the same live and in a replay (measured 2026-11-10 on the Neox replay).
WARNER_LIVE   = {'live': True,  'epgtag': False, 'number': '12',  'name': 'Warner TV'}
WARNER_REPLAY = {'live': False, 'epgtag': True,  'number': '12',  'name': 'Warner TV'}
NEOX_LIVE     = {'live': True,  'epgtag': False, 'number': '8',   'name': 'Neox'}
NEOX_REPLAY   = {'live': False, 'epgtag': True,  'number': '8',   'name': 'Neox'}
CN_LIVE       = {'live': True,  'epgtag': False, 'number': '303', 'name': 'Cartoon Network'}
CUSTOM_CN     = {'live': True,  'epgtag': False, 'number': '304', 'name': 'Cartoon Network'}
LIBRARY       = {'live': False, 'epgtag': False, 'number': '',    'name': ''}

INFO_READS = ['Pvr.IsPlayingTv', 'Pvr.IsPlayingEpgTag', 'VideoPlayer.ChannelNumberLabel', 'VideoPlayer.ChannelName',
              'VideoPlayer.SubtitlesEnabled', 'VideoPlayer.HasSubtitles']

LIVE_METHODS = ('_liveSubsPath', '_loadLiveSubs', '_saveLiveSubs', '_liveSubsInfo', '_liveSubsFile',
                '_liveSubsChannels', '_waitChannelPreview', '_showLiveSubtitles', '_restoreLiveSubtitles',
                '_chkLiveSubtitles')

# What reaches Kodi's player: "on" re-selects the current track (Kodi lists tracks 0 and 1, 1 current);
# "off" is the flag; FLAG_ON only when Kodi lists no track.
CURRENT_TRACK = 1
ON      = ('setSubtitleStream', CURRENT_TRACK)
OFF     = ('showSubtitles', False)
FLAG_ON = ('showSubtitles', True)
TRACKS_READ = {'method': 'Player.GetProperties',
               'params': {'playerid': 1, 'properties': ['subtitles', 'currentsubtitle']}}


def playing_file(kodi_state, epoch):
    """What getPlayingFile gives: one file per live channel (one video for Kodi), a new one per replay."""
    if kodi_state.get('epgtag'): return 'pvr://guide/0071/replay-%d.epg' % epoch
    if kodi_state.get('live'):   return 'pvr://channels/tv/All channels/pvr.iptvsimple_%s.pvr' % kodi_state['number']
    return '/media/library/film-%d.mkv' % epoch


class FakeKodi(object):
    """The xbmc module the compiled methods see: the stub's log levels, and the reads answered from
    `state` (what Kodi shows now). `on_read` runs before a read is answered (a playback event landing
    mid-read); `fail` makes every read raise. A switch from the add-on lands at once, or with `queued`
    when Kodi's player gets to it: at the next wait or tick (flush)."""
    LOGDEBUG, LOGINFO, LOGWARNING, LOGERROR = xbmc.LOGDEBUG, xbmc.LOGINFO, xbmc.LOGWARNING, xbmc.LOGERROR
    CONDS  = {'Pvr.IsPlayingTv': 'live', 'Pvr.IsPlayingEpgTag': 'epgtag',
              'VideoPlayer.SubtitlesEnabled': 'subs', 'VideoPlayer.HasSubtitles': 'has',
              'Player.ChannelPreviewActive': 'preview'}
    LABELS = {'VideoPlayer.ChannelNumberLabel': 'number', 'VideoPlayer.ChannelName': 'name'}

    def __init__(self):
        self.state = dict(LIBRARY, subs=False, has=True, playing=True, file=playing_file(LIBRARY, 0),
                          tracks=[0, 1], track=CURRENT_TRACK, preview=False)
        self.reads, self.on_read, self.fail = [], None, None
        self.queued, self.pending = False, []

    def _read(self, table, name):
        self.reads.append(name)
        if self.fail is not None: raise self.fail
        if self.on_read is not None: self.on_read(name)
        return self.state[table[name]]

    def getCondVisibility(self, condition): return bool(self._read(self.CONDS, condition))
    def getInfoLabel(self, label): return self._read(self.LABELS, label)

    def apply(self, change):
        if self.queued: self.pending.append(change)
        else: self.state.update(change)

    def flush(self):
        pending, self.pending = self.pending, []
        for change in pending: self.state.update(change)


class Files(object):
    """FileAccess for the compiled methods: the real one (crash-safe setJSON included), counting the
    memory file writes. fail_write 'false' / 'raise' and fail_read make it fail; on_write runs while
    a write is in progress (a slow disk)."""

    def __init__(self):
        self.writes, self.fail_write, self.fail_read, self.on_write = [], None, None, None

    def translatePath(self, path): return FileAccess.translatePath(path)

    def getJSON(self, path):
        if self.fail_read is not None: raise self.fail_read
        return FileAccess.getJSON(path)

    def setJSON(self, path, data):
        self.writes.append(dict(data))
        if self.on_write is not None: self.on_write(data)
        if self.fail_write == 'false': return False
        if self.fail_write == 'raise': raise OSError('No space left on device')
        return FileAccess.setJSON(path, data)


class Wait(object):
    """Player.monitor: waitForAbort records the wait, lets Kodi's player apply what was queued, runs
    `during` (what happens in Kodi meanwhile) and answers `abort`."""

    def __init__(self, kodi):
        self.kodi, self.waits, self.during, self.abort = kodi, [], None, False

    def waitForAbort(self, timeout=None):
        self.waits.append(timeout)
        self.kodi.flush()
        if self.during is not None: self.during()
        return self.abort


class Tasks(object):
    """service.tasks: getChannels is the fresh read of channels.json (`channels`); `during` runs while
    it reads, `fail` makes it raise."""

    def __init__(self, channels):
        self.channels, self.reads, self.during, self.fail = channels, 0, None, None

    def getChannels(self):
        self.reads += 1
        if self.during is not None: self.during()
        if self.fail is not None: raise self.fail
        return self.channels


class RPC(object):
    """Player.jsonRPC: sendJSON records the request and answers Player.GetProperties from Kodi's tracks
    (`answer` replaces that answer when a test sets it; `fail` makes it raise)."""
    FROM_KODI = object()

    def __init__(self, kodi, calls):
        self.kodi, self.calls, self.sent, self.delays = kodi, calls, [], []
        self.answer, self.fail, self.during = self.FROM_KODI, None, None

    def sendJSON(self, param, delay=True):
        self.sent.append(json.loads(json.dumps(param)))
        self.delays.append(delay)
        self.calls.append(('sendJSON', param.get('method')))
        if self.during is not None: self.during()
        if self.fail is not None: raise self.fail
        if self.answer is not self.FROM_KODI: return self.answer
        tracks = [{'index': i, 'language': 'spa', 'name': 'Spanish', 'codec': 'webvtt', 'isdefault': i == 0,
                   'isforced': False, 'isimpaired': False} for i in self.kodi.state['tracks']]
        current = [t for t in tracks if t['index'] == self.kodi.state['track']]
        return {'id': 'plugin.video.pseudotv.imports', 'jsonrpc': '2.0',
                'result': {'subtitles': tracks, 'currentsubtitle': current[0] if current else None}}


class PlayerBase(object):
    """What the compiled methods use from services.Player besides themselves."""

    def __init__(self, kodi, channels):
        self.kodi = kodi
        self.service = types.SimpleNamespace(channels=channels, tasks=Tasks(channels))
        self.monitor, self._play_epoch = Wait(kodi), 0
        self.logged, self.calls, self.switches, self.on_switch = [], [], [], None
        self.jsonRPC, self.file_fail = RPC(kodi, self.calls), None
        self.playingItem, self._retune, self.lastSubState = {}, False, False
        self._liveSubs, self._liveSubsState = {}, None

    def log(self, msg, level=xbmc.LOGDEBUG): self.logged.append((msg, level))

    def _switch(self, call, change):
        self.switches.append(call)
        self.calls.append(call)
        self.kodi.apply(change)
        if self.on_switch is not None: self.on_switch(call)

    def showSubtitles(self, visible): self._switch(('showSubtitles', visible), {'subs': bool(visible)})

    # Kodi's Player.setSubtitleStream selects the track and makes subtitles visible.
    def setSubtitleStream(self, index): self._switch(('setSubtitleStream', index), {'subs': True, 'track': index})

    def getPlayingFile(self):
        if self.file_fail is not None: raise self.file_fail
        return self.kodi.state['file']

    def isPlayingVideo(self): return bool(self.kodi.state['playing'])

    # the rest of _onPlay's non-PseudoTV branch
    def toggleInfo(self, state=False): self.calls.append(('toggleInfo', state))
    def toggleBackground(self, state=False): self.calls.append(('toggleBackground', state))
    def toggleOverlay(self, state=False): self.calls.append(('toggleOverlay', state))
    def _syncPlaystate(self, oldInfo=None, newInfo=None, allow=False): self.calls.append(('syncPlaystate', allow))
    def _releaseChannelRules(self): self.calls.append(('releaseChannelRules', None))
    def toggleReplay(self, state=False, epoch=None): self.calls.append(('toggleReplay', state, epoch))

    def infos(self): return [msg for msg, level in self.logged if level == xbmc.LOGINFO]
    def warnings(self): return [msg for msg, level in self.logged if level == xbmc.LOGWARNING]


class Rig(object):
    """A fake player whose imports.67 methods (and _onPlay) are the real ones, its memory file in
    `tmp_path` (written there first when `memory` or `raw` is given). `channels` is both the list the
    service loaded at start and what channels.json holds now (tests change either)."""

    def __init__(self, tmp_path, memory=None, raw=None, channels=None):
        self.path = tmp_path / lh.MEMORY_FILE
        if raw is not None: self.path.write_text(raw, encoding='utf-8')
        elif memory is not None: self.path.write_text(json.dumps(memory), encoding='utf-8')
        self.kodi, self.files = FakeKodi(), Files()
        g = dict(livesubs_helpers=lh, xbmc=self.kodi, FileAccess=self.files, os=os, CACHE_LOC=str(tmp_path),
                 BUILTIN=types.SimpleNamespace(isSubtitle=lambda: self.kodi.state['subs']),
                 SETTINGS=types.SimpleNamespace(getSettingInt=lambda key: {'Replay_Percentage': 5}[key]))
        ns = {name: _function('Player', name, **g) for name in LIVE_METHODS + ('_onPlay',)}
        self.player = type('FakePlayer', (PlayerBase,), ns)(self.kodi, CHANNELS if channels is None else channels)
        # as Player.__init__ does (test_init_loads_the_memory_once checks those two lines)
        self.player._liveSubs      = self.player._loadLiveSubs()
        self.player._liveSubsState = None

    def saved(self):
        """The memory file as JSON (None when there is none)."""
        if not self.path.exists(): return None
        with open(str(self.path), encoding='utf-8') as fh: return json.load(fh)


def tune(rig, kodi_state, subs, has=True, file=None):
    """A new playback: onPlayBackStarted + onAVStarted bump the epoch, Kodi shows `kodi_state` (and
    its file), and _onPlay's LiveTV branch ends with the restore (its waits run Wait.during)."""
    rig.player._play_epoch += 2
    epoch = rig.player._play_epoch
    rig.kodi.state.update(kodi_state, subs=subs, has=has, playing=True,
                          file=playing_file(kodi_state, epoch) if file is None else file)
    rig.player._restoreLiveSubtitles(epoch)
    return epoch


def tick(rig, times=1):
    """Monitor._chkIdle's not-Custom tick, about once a second (time enough for Kodi's player to
    apply what was queued)."""
    for _ in range(times):
        rig.kodi.flush()
        rig.player._chkLiveSubtitles()


def settled(rig, epoch, key, kodi_state, has=True, seed=False, baseline=True):
    """The state a settled playback leaves for the monitor (the file: what Kodi plays now)."""
    return {'epoch': epoch, 'key': key, 'labels': (kodi_state['number'], kodi_state['name']),
            'file': rig.kodi.state['file'], 'has': has, 'seed': seed, 'baseline': baseline}


def once(action):
    """`action` the first time only (something Kodi does once, during the first wait)."""
    done = []

    def run(*args):
        if not done:
            done.append(True)
            action(*args)
    return run


# === structure: where the calls sit, who writes what ==============================================

def test_services_imports_livesubs_helpers():
    assert any(isinstance(n, ast.Import) and [a.name for a in n.names] == ['livesubs_helpers'] for n in S_TREE.body)


def test_init_loads_the_memory_once_per_instance():
    init = [ast.unparse(n) for n in ast.walk(_method('Player', '__init__')) if isinstance(n, ast.Assign)]
    assert 'self._liveSubs = self._loadLiveSubs()' in init
    assert 'self._liveSubsState = None' in init
    class_level = {t.id for n in _class('Player').body if isinstance(n, ast.Assign) for t in n.targets
                   if isinstance(t, ast.Name)}
    assert not class_level & {'_liveSubs', '_liveSubsState'}


def test_onplay_livetv_branch_ends_with_the_restore():
    """Last before the early return (it waits RECHECK_S), after the Restart pop-up is scheduled, with
    the playback's own epoch; the only call site, so a Custom channel never reaches it."""
    onplay = _method('Player', '_onPlay')
    branches = [n for n in ast.walk(onplay) if isinstance(n, ast.If)
                and ast.unparse(n.test) == "not playingItem.get('isPseudoTV')"]
    assert len(branches) == 1
    stmts = [ast.unparse(s) for s in branches[0].body]
    assert stmts[-1] == 'return' and stmts[-2] == 'self._restoreLiveSubtitles(epoch)'
    assert stmts[-3] == "self.toggleReplay(bool(SETTINGS.getSettingInt('Replay_Percentage')), epoch)"
    assert len(_calls(S_TREE, '_restoreLiveSubtitles')) == 1
    assert [ast.unparse(d) for d in onplay.decorator_list] == ['threadit']


def test_chkidle_reads_live_subtitles_before_the_transition():
    chk = _method('Monitor', '_chkIdle')
    ifs = [n for n in ast.walk(chk) if isinstance(n, ast.If) and ast.unparse(n.test) == 'self.player.isPlayingPseudoTV()']
    assert len(ifs) == 1
    other = [ast.unparse(s) for s in ifs[0].orelse]
    at = other.index('self.player._chkLiveSubtitles()')
    assert other[at + 1] == 'self.player._chkTransition()'
    assert 'self.player._chkLiveSubtitles()' not in [ast.unparse(s) for s in ifs[0].body]
    assert len(_calls(S_TREE, '_chkLiveSubtitles')) == 1


def test_liveSubsInfo_reads_kodi_directly():
    """xbmc.getCondVisibility / getInfoLabel, not BUILTIN.getInfoBool / getInfoLabel (they log every call
    at debug level, and this runs about once a second during LiveTV)."""
    node = _method('Player', '_liveSubsInfo')
    assert sorted({ast.unparse(n.func) for n in ast.walk(node) if isinstance(n, ast.Call)}) == \
        ['xbmc.getCondVisibility', 'xbmc.getInfoLabel']
    assert 'BUILTIN' not in ast.unparse(node)


def test_liveSubsInfo_reads_the_six_labels(tmp_path):
    rig = Rig(tmp_path)
    rig.kodi.state.update(WARNER_REPLAY, subs=True, has=False)
    rig.kodi.reads[:] = []
    assert rig.player._liveSubsInfo() == {'live': False, 'epgtag': True, 'number': '12', 'name': 'Warner TV',
                                          'current': True, 'has': False}
    assert sorted(rig.kodi.reads) == sorted(INFO_READS)


def test_the_memory_has_one_writer():
    """The monitor thread (_chkLiveSubtitles) is the only writer of the memory and its file after
    __init__; the play thread only reads it. Nothing changes the dict in place (copy on write: the
    play thread reads it without a lock)."""
    owner = _owner_map()
    stores = [n for n in ast.walk(S_TREE) if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Store)]
    assert sorted({owner(n) for n in stores if n.attr == '_liveSubs'}) == \
        [('Player', '__init__'), ('Player', '_chkLiveSubtitles')]
    assert sorted({owner(n) for n in stores if n.attr == '_liveSubsState'}) == \
        [('Player', '__init__'), ('Player', '_chkLiveSubtitles'), ('Player', '_restoreLiveSubtitles')]
    saves = [owner(n) for n in _calls(S_TREE, '_saveLiveSubs')]
    assert saves == [('Player', '_chkLiveSubtitles')]
    memory = [n for n in ast.walk(S_TREE) if isinstance(n, ast.Attribute) and n.attr == '_liveSubs']
    in_place = [n for n in ast.walk(S_TREE)
                if (isinstance(n, ast.Subscript) and n.value in memory and not isinstance(n.ctx, ast.Load))
                or (isinstance(n, ast.Attribute) and n.value in memory and n.attr != 'get')]
    assert in_place == []


def test_the_methods_run_inline_and_never_raise():
    """No decorator (the restore runs on _onPlay's worker in epoch order, the record on the monitor
    tick); no bare except; the restore and the record are wrapped whole (_showLiveSubtitles raises
    into them)."""
    for name in LIVE_METHODS:
        node = _method('Player', name)
        assert node.decorator_list == [], name
        for handler in [n for n in ast.walk(node) if isinstance(n, ast.ExceptHandler)]:
            assert handler.type is not None and ast.unparse(handler.type) == 'Exception', name
    for name in ('_restoreLiveSubtitles', '_chkLiveSubtitles'):
        body = _method('Player', name).body
        assert len(body) == 1 and isinstance(body[0], ast.Try), name


def test_every_livetv_switch_goes_through_showLiveSubtitles():
    """The restore, its re-apply and the late-track apply all switch through _showLiveSubtitles; it is
    the only imports.67 method that calls Kodi's player switches."""
    for name in LIVE_METHODS:
        node = _method('Player', name)
        direct = _calls(node, 'showSubtitles') + _calls(node, 'setSubtitleStream')
        assert bool(direct) == (name == '_showLiveSubtitles'), name
    owner = _owner_map()
    assert sorted(owner(n) for n in _calls(S_TREE, '_showLiveSubtitles')) == \
        [('Player', '_chkLiveSubtitles'), ('Player', '_restoreLiveSubtitles'), ('Player', '_restoreLiveSubtitles')]


def test_the_key_is_resolved_once_per_playback_from_a_fresh_list():
    """The restore resolves the key from _liveSubsChannels (once); the re-check compares labels and
    the record never resolves a key: it uses the settled one."""
    restore, chk = _method('Player', '_restoreLiveSubtitles'), _method('Player', '_chkLiveSubtitles')
    assert len(_calls(restore, 'channel_key')) == 1 and len(_calls(restore, '_liveSubsChannels')) == 1
    assert _calls(chk, 'channel_key') == [] and _calls(chk, '_liveSubsChannels') == []
    for node in (restore, chk):
        assert 'self.service.channels' not in ast.unparse(node)
    assert 'self.service.tasks.getChannels()' in ast.unparse(_method('Player', '_liveSubsChannels'))


# === _showLiveSubtitles: how a switch reaches Kodi ================================================

@pytest.mark.parametrize('current', [0, 1])
def test_on_reselects_the_current_track(tmp_path, current):
    """A restore to "on" reads Kodi's tracks and re-selects the current one (setSubtitleStream reopens a
    track Kodi disabled at the start, and shows it); never only the flag."""
    rig = Rig(tmp_path, memory={WARNER: True})
    rig.kodi.state['track'] = current
    tune(rig, WARNER_REPLAY, subs=False)
    assert rig.player.jsonRPC.sent == [TRACKS_READ]
    assert rig.player.switches == [('setSubtitleStream', current)]
    assert FLAG_ON not in rig.player.calls
    assert rig.kodi.state['subs'] is True and rig.kodi.state['track'] == current


def test_on_without_a_current_track_selects_the_first(tmp_path):
    rig = Rig(tmp_path, memory={WARNER: True})
    rig.kodi.state.update(tracks=[2, 3], track=None)
    tune(rig, WARNER_REPLAY, subs=False)
    assert rig.player.switches == [('setSubtitleStream', 2)]


@pytest.mark.parametrize('answer', [
    {'id': 'plugin.video.pseudotv.imports', 'jsonrpc': '2.0', 'result': {'subtitles': [], 'currentsubtitle': None}},
    {'result': {'error': {'code': -32100, 'message': 'Failed to execute method.'}}},
    {},
    None,
], ids=['no-tracks', 'error', 'no-result', 'no-answer'])
def test_on_falls_back_to_the_flag_when_kodi_lists_no_track(tmp_path, answer):
    rig = Rig(tmp_path, memory={WARNER: True})
    rig.player.jsonRPC.answer = answer
    tune(rig, WARNER_REPLAY, subs=False)
    assert rig.player.jsonRPC.sent == [TRACKS_READ]
    assert rig.player.switches == [FLAG_ON]
    assert rig.player.warnings() == []


def test_off_is_the_flag(tmp_path):
    rig = Rig(tmp_path, memory={WARNER: False})
    tune(rig, WARNER_LIVE, subs=True)
    assert rig.player.switches == [OFF]
    assert rig.player.jsonRPC.sent == []


# === restore: a LiveTV channel starts playing =====================================================

def test_a_replay_of_a_channel_saved_on_gets_subtitles_on(tmp_path):
    """The RESTART pop-up / Kodi's next show: a new video, subtitles off -> on, settled after the wait."""
    rig = Rig(tmp_path, memory={WARNER: True})
    epoch = tune(rig, WARNER_REPLAY, subs=False)
    assert rig.player.switches == [ON]
    assert rig.player.monitor.waits == [lh.RECHECK_S]
    assert rig.player._liveSubsState == settled(rig, epoch, WARNER, WARNER_REPLAY)
    assert rig.player.infos() == ['live subtitles: %s restored on' % WARNER]
    assert rig.files.writes == []                    # the restore never writes


@pytest.mark.parametrize('saved', [True, False])
def test_a_live_tune_gets_the_saved_choice(tmp_path, saved):
    rig = Rig(tmp_path, memory={NEOX: saved})
    epoch = tune(rig, NEOX_LIVE, subs=not saved)
    assert rig.player.switches == [ON if saved else OFF]
    assert rig.player._liveSubsState == settled(rig, epoch, NEOX, NEOX_LIVE, baseline=saved)


@pytest.mark.parametrize('state', [WARNER_LIVE, WARNER_REPLAY], ids=['live', 'replay'])
@pytest.mark.parametrize('saved', [True, False])
def test_a_choice_already_in_place_changes_nothing(tmp_path, saved, state):
    rig = Rig(tmp_path, memory={WARNER: saved})
    tune(rig, state, subs=saved)
    assert rig.player.switches == [] and rig.player.jsonRPC.sent == [] and rig.player.infos() == []
    assert rig.player._liveSubsState['baseline'] is saved


@pytest.mark.parametrize('subs', [True, False])
@pytest.mark.parametrize('state', [WARNER_LIVE, WARNER_REPLAY], ids=['live', 'replay'])
def test_no_saved_choice_changes_nothing(tmp_path, state, subs):
    rig = Rig(tmp_path, memory={NEOX: True})          # another channel's choice
    epoch = tune(rig, state, subs=subs)
    assert rig.player.switches == []
    assert rig.player._liveSubsState == settled(rig, epoch, WARNER, state, seed=state['live'], baseline=subs)


def test_restore_never_turns_on_without_a_track(tmp_path):
    rig = Rig(tmp_path, memory={WARNER: True})
    epoch = tune(rig, WARNER_REPLAY, subs=False, has=False)
    assert rig.player.switches == [] and rig.player.jsonRPC.sent == []
    assert rig.player._liveSubsState == settled(rig, epoch, WARNER, WARNER_REPLAY, has=False, baseline=False)


def test_restore_turns_off_even_without_a_track(tmp_path):
    rig = Rig(tmp_path, memory={WARNER: False})
    tune(rig, WARNER_LIVE, subs=True, has=False)
    assert rig.player.switches == [OFF]


def test_the_first_off_comes_before_the_wait(tmp_path):
    """Off is applied at once, not left to the re-check."""
    rig = Rig(tmp_path, memory={WARNER: False})
    at_wait = []
    rig.player.monitor.during = lambda: at_wait.append(list(rig.player.switches))
    tune(rig, WARNER_LIVE, subs=True)
    assert at_wait == [[OFF]]
    assert rig.player.switches == [OFF] and rig.player.monitor.waits == [lh.RECHECK_S]
    assert rig.player.infos() == ['live subtitles: %s restored off' % WARNER]


def test_the_recheck_reapplies_once_when_kodi_puts_its_setting_back(tmp_path):
    """The one start-up order not confirmed in Kodi's code: Kodi's per-video setting landing after
    onAVStarted. The re-check puts the choice back, once, then waits for it to land."""
    rig = Rig(tmp_path, memory={WARNER: True})
    rig.player.monitor.during = once(lambda: rig.kodi.state.update(subs=False))
    epoch = tune(rig, WARNER_REPLAY, subs=False)
    assert rig.player.switches == [ON, ON] and rig.player.jsonRPC.sent == [TRACKS_READ] * 2
    assert rig.player.monitor.waits == [lh.RECHECK_S, lh.APPLY_WAIT_S]
    assert rig.player.infos() == ['live subtitles: %s restored on' % WARNER,
                                  'live subtitles: %s re-applied on' % WARNER]
    assert rig.player._liveSubsState == settled(rig, epoch, WARNER, WARNER_REPLAY)


def test_the_recheck_reapplies_off_when_kodi_puts_its_on_back(tmp_path):
    """Review finding: saved off, Kodi's own live setting (on) lands again during the wait. Off is
    applied twice, the settled baseline is off, and the ticks save nothing."""
    rig = Rig(tmp_path, memory={WARNER: False})
    rig.kodi.queued = True
    rig.player.monitor.during = once(lambda: rig.kodi.state.update(subs=True))
    epoch = tune(rig, WARNER_LIVE, subs=True)
    assert rig.player.switches == [OFF, OFF]
    assert rig.player.monitor.waits == [lh.RECHECK_S, lh.APPLY_WAIT_S]
    assert rig.player.infos() == ['live subtitles: %s restored off' % WARNER,
                                  'live subtitles: %s re-applied off' % WARNER]
    assert rig.player._liveSubsState == settled(rig, epoch, WARNER, WARNER_LIVE, baseline=False)
    tick(rig, 3)
    assert rig.files.writes == [] and rig.player._liveSubs == {WARNER: False}
    assert rig.kodi.state['subs'] is False


def test_the_reapply_goes_through_showLiveSubtitles_and_waits_before_reading(tmp_path):
    """Kodi queues a switch: the re-apply (through _showLiveSubtitles, as the restore) is followed by
    APPLY_WAIT_S, and the state that settles the baseline is read after it, not before it landed."""
    rig = Rig(tmp_path, memory={WARNER: True})
    rig.kodi.queued = True
    asked = []

    def show(on, epoch=None):
        asked.append(on)
        rig.kodi.apply({'subs': bool(on)})
        return True
    rig.player._showLiveSubtitles = show
    rig.player.monitor.during = once(lambda: rig.kodi.state.update(subs=False))
    rig.kodi.reads[:] = []
    epoch = tune(rig, WARNER_REPLAY, subs=False)
    assert asked == [True, True]
    assert rig.player.monitor.waits == [lh.RECHECK_S, lh.APPLY_WAIT_S]
    assert len(rig.kodi.reads) == 3 * len(INFO_READS) + 1      # start, re-check, after the re-apply
    assert rig.kodi.reads.count('Player.ChannelPreviewActive') == 1   # + the re-check's preview read
    assert rig.player._liveSubsState == settled(rig, epoch, WARNER, WARNER_REPLAY, baseline=True)


def test_the_recheck_does_nothing_when_the_restore_holds(tmp_path):
    rig = Rig(tmp_path, memory={WARNER: True})
    tune(rig, WARNER_REPLAY, subs=False)
    assert rig.player.switches == [ON] and rig.player.monitor.waits == [lh.RECHECK_S]
    assert not [msg for msg in rig.player.infos() if 're-applied' in msg]


def test_a_track_that_arrives_during_the_wait_still_gets_on(tmp_path):
    rig = Rig(tmp_path, memory={WARNER: True})
    rig.player.monitor.during = once(lambda: rig.kodi.state.update(has=True))
    epoch = tune(rig, WARNER_REPLAY, subs=False, has=False)
    assert rig.player.switches == [ON]
    assert rig.player.infos() == ['live subtitles: %s re-applied on' % WARNER]
    assert rig.player._liveSubsState == settled(rig, epoch, WARNER, WARNER_REPLAY, has=True, baseline=True)


def test_a_newer_playback_during_the_wait_ends_the_restore(tmp_path):
    """Another epoch (a zap, Kodi's next show, a Stop) during the wait: no re-apply, not settled."""
    rig = Rig(tmp_path, memory={WARNER: True})

    def newer():
        rig.player._play_epoch += 1
        rig.kodi.state.update(subs=False)
    rig.player.monitor.during = newer
    tune(rig, WARNER_REPLAY, subs=False)
    assert rig.player.switches == [ON]
    assert rig.player._liveSubsState is None


def test_a_newer_playback_during_the_reapply_is_not_settled(tmp_path):
    rig = Rig(tmp_path, memory={WARNER: True})
    rig.player.monitor.during = once(lambda: rig.kodi.state.update(subs=False))
    rig.player.on_switch = lambda call: (len(rig.player.switches) == 2 and setattr(rig.player, '_play_epoch',
                                                                                   rig.player._play_epoch + 1))
    tune(rig, WARNER_REPLAY, subs=False)
    assert rig.player.switches == [ON, ON]
    assert rig.player.monitor.waits == [lh.RECHECK_S, lh.APPLY_WAIT_S]
    assert rig.player._liveSubsState is None


def test_the_epoch_is_read_again_right_before_the_first_switch(tmp_path):
    """A newer playback starting while the channel list is read (just before the first apply): no
    switch, no wait, not settled. For "on" the track list is still read (read-only) -- the epoch is
    checked after that read, right before the switch."""
    rig = Rig(tmp_path, memory={WARNER: True})

    def newer(): rig.player._play_epoch += 1
    rig.player.service.tasks.during = newer
    tune(rig, WARNER_REPLAY, subs=False)
    assert rig.player.switches == [] and rig.player.jsonRPC.sent == [TRACKS_READ]
    assert rig.player.monitor.waits == [] and rig.player._liveSubsState is None


def test_the_epoch_is_read_again_right_before_the_reapply(tmp_path):
    """A newer playback starting while the re-check reads Kodi: no second switch, not settled."""
    rig = Rig(tmp_path, memory={WARNER: True})
    rig.player.monitor.during = once(lambda: rig.kodi.state.update(subs=False))
    seen = []

    def event(name):
        if name == 'VideoPlayer.HasSubtitles':
            seen.append(name)
            if len(seen) == 2: rig.player._play_epoch += 1     # the re-check's read
    rig.kodi.on_read = event
    tune(rig, WARNER_REPLAY, subs=False)
    assert rig.player.switches == [ON] and rig.player.jsonRPC.sent == [TRACKS_READ, TRACKS_READ]
    assert rig.player.monitor.waits == [lh.RECHECK_S]
    assert rig.player._liveSubsState is None


def test_abort_during_the_wait_returns(tmp_path):
    rig = Rig(tmp_path, memory={WARNER: True})
    rig.player.monitor.abort = True
    rig.player.monitor.during = lambda: rig.kodi.state.update(subs=False)
    tune(rig, WARNER_REPLAY, subs=False)
    assert rig.player.switches == [ON] and len(rig.kodi.reads) == len(INFO_READS)
    assert rig.player._liveSubsState is None


@pytest.mark.parametrize('meanwhile', [NEOX_REPLAY, LIBRARY, dict(WARNER_REPLAY, name=''),
                                       dict(WARNER_REPLAY, number='13')],
                         ids=['another-channel', 'not-livetv', 'no-name', 'other-number'])
def test_what_plays_changed_during_the_wait_ends_the_restore(tmp_path, meanwhile):
    """Kodi shows something else before the epoch moved (a zap whose events are still queued)."""
    rig = Rig(tmp_path, memory={WARNER: True, NEOX: False})
    rig.player.monitor.during = lambda: rig.kodi.state.update(meanwhile, subs=False)
    tune(rig, WARNER_REPLAY, subs=False)
    assert rig.player.switches == [ON]
    assert rig.player._liveSubsState is None


def test_the_recheck_needs_livetv_not_only_the_same_labels(tmp_path):
    """Non-PVR playback with Warner TV's labels left over during the wait: same labels, but not
    LiveTV -- no re-apply, not settled."""
    rig = Rig(tmp_path, memory={WARNER: True})
    rig.player.monitor.during = lambda: rig.kodi.state.update(dict(LIBRARY, number='12', name='Warner TV'),
                                                              subs=False)
    tune(rig, WARNER_REPLAY, subs=False)
    assert rig.player.switches == [ON] and rig.player.monitor.waits == [lh.RECHECK_S]
    assert rig.player._liveSubsState is None


def test_the_recheck_compares_the_labels_not_a_new_key(tmp_path):
    """channels.json changing during the wait doesn't matter: the channel list is read once per
    restore, and the re-check compares Kodi's labels."""
    rig = Rig(tmp_path, memory={WARNER: True})

    def meanwhile():
        rig.player.service.tasks.channels = []                 # now Warner TV would key as 'name:Warner TV'
        rig.kodi.state.update(subs=False)
    rig.player.monitor.during = once(meanwhile)
    epoch = tune(rig, WARNER_REPLAY, subs=False)
    assert rig.player.switches == [ON, ON]
    assert rig.player.service.tasks.reads == 1
    assert rig.player._liveSubsState == settled(rig, epoch, WARNER, WARNER_REPLAY)


def test_library_playback_is_never_touched(tmp_path):
    """Not Pvr.IsPlayingTv / Pvr.IsPlayingEpgTag: nothing, even with channel labels left over."""
    rig = Rig(tmp_path, memory={WARNER: True})
    for state in (LIBRARY, dict(LIBRARY, number='12', name='Warner TV')):
        tune(rig, state, subs=False)
    assert rig.player.switches == [] and rig.player.monitor.waits == [] and rig.player.jsonRPC.sent == []
    assert rig.player._liveSubsState is None and rig.player.service.tasks.reads == 0
    rig.kodi.state['subs'] = True
    tick(rig, 3)
    assert rig.saved() == {WARNER: True} and rig.files.writes == []


def test_no_channel_name_does_nothing(tmp_path):
    rig = Rig(tmp_path, memory={'name:': True})
    tune(rig, dict(WARNER_LIVE, name='  '), subs=False)
    assert rig.player.switches == [] and rig.player.monitor.waits == [] and rig.player._liveSubsState is None


def test_a_channel_the_addon_doesnt_import_is_remembered_by_its_name(tmp_path):
    """Another PVR source's channel: 'name:<name>', whatever number Kodi gives it."""
    rig = Rig(tmp_path)
    tune(rig, {'live': False, 'epgtag': True, 'number': '77', 'name': 'Canal Sur'}, subs=False)
    rig.kodi.state['subs'] = True
    tick(rig)
    assert rig.saved() == {'name:Canal Sur': True}
    tune(rig, {'live': True, 'epgtag': False, 'number': '78', 'name': 'Canal Sur'}, subs=False)   # Kodi renumbered
    assert rig.player.switches == [ON]


CN_ES = 'CartoonNetwork.es@movistarplus'


def test_two_listed_namesakes_are_told_apart_only_by_kodis_number(tmp_path):
    """Two listed imported "Cartoon Network": the one whose number is Kodi's number label; when
    neither is, nothing is applied, settled or remembered."""
    channels = CHANNELS + [{'id': CN_ES, 'type': 'import', 'number': 310, 'name': 'Cartoon Network',
                            'enabled': True}]
    rig = Rig(tmp_path, memory={CN: True, CN_ES: False}, channels=channels)
    epoch = tune(rig, CN_LIVE, subs=False)
    assert rig.player.switches == [ON]
    assert rig.player._liveSubsState == settled(rig, epoch, CN, CN_LIVE)
    tune(rig, dict(CN_LIVE, number='57'), subs=True)                        # Kodi's own running count
    assert rig.player.switches == [ON] and rig.player.monitor.waits == [lh.RECHECK_S]
    assert rig.player._liveSubsState is None
    rig.kodi.state['subs'] = False
    tick(rig, 2)
    assert rig.files.writes == []


def test_a_new_playback_cancels_the_old_settled_state_at_once(tmp_path):
    """While the new playback's restore waits, the monitor records nothing (yours or Kodi's change)."""
    rig = Rig(tmp_path, memory={WARNER: True})
    tune(rig, WARNER_LIVE, subs=True)
    assert rig.player._liveSubsState is not None
    during = []

    def meanwhile():
        during.append(rig.player._liveSubsState)
        rig.kodi.state.update(subs=False)
        tick(rig, 2)
    rig.player.monitor.during = once(meanwhile)
    tune(rig, WARNER_REPLAY, subs=False)
    assert during == [None]                                   # cleared at once, not only made stale
    assert rig.files.writes == [] and rig.saved() == {WARNER: True}


def test_a_late_restore_of_an_older_playback_leaves_the_newer_one_alone(tmp_path):
    """_onPlay runs on a @threadit worker per playback. An older playback's worker that reaches the
    restore only after a newer playback settled (a slow PseudoTV hand-over in _syncPlaystate, say)
    must neither touch Kodi nor cancel the newer playback's recording."""
    rig = Rig(tmp_path, memory={WARNER: True})
    rig.player._play_epoch = 5
    older = rig.player._play_epoch
    tune(rig, WARNER_REPLAY, subs=False)                      # the newer playback, settled
    settled_state = rig.player._liveSubsState
    before = (list(rig.player.switches), len(rig.kodi.reads), list(rig.player.monitor.waits),
              len(rig.player.jsonRPC.sent), rig.player.service.tasks.reads)
    rig.player._restoreLiveSubtitles(older)
    assert rig.player._liveSubsState is settled_state
    assert (list(rig.player.switches), len(rig.kodi.reads), list(rig.player.monitor.waits),
            len(rig.player.jsonRPC.sent), rig.player.service.tasks.reads) == before
    rig.kodi.state['subs'] = False                            # you switch them off in the newer one
    tick(rig)
    assert rig.saved() == {WARNER: False}


@pytest.mark.parametrize('state, memory, seed', [
    (WARNER_LIVE, {}, True), (WARNER_LIVE, {WARNER: True}, False),
    (WARNER_REPLAY, {}, False), (WARNER_REPLAY, {WARNER: False}, False),
], ids=['live-new', 'live-known', 'replay-new', 'replay-known'])
def test_only_a_first_live_tune_seeds(tmp_path, state, memory, seed):
    rig = Rig(tmp_path, memory=memory)
    tune(rig, state, subs=memory.get(WARNER, True))
    assert rig.player._liveSubsState['seed'] is seed


# --- which channel list ---------------------------------------------------------------------------

CLAN      = 'CLAN@movistarplus'
CLAN_ROW  = {'id': CLAN, 'type': 'import', 'number': 20, 'name': 'Clan', 'enabled': True}
CLAN_LIVE = {'live': True, 'epgtag': False, 'number': '20', 'name': 'Clan'}


def test_a_channel_imported_after_the_start_resolves_to_its_id(tmp_path):
    """The restore reads channels.json fresh (tasks.getChannels), not the list the service loaded at
    start: a channel imported since then keys by its id, not by its name."""
    rig = Rig(tmp_path)                                         # service.channels: the start list, no Clan
    rig.player.service.tasks.channels = CHANNELS + [CLAN_ROW]
    epoch = tune(rig, CLAN_LIVE, subs=True)
    assert rig.player._liveSubsState == settled(rig, epoch, CLAN, CLAN_LIVE, seed=True)
    assert rig.player.service.tasks.reads == 1
    tick(rig)
    assert rig.saved() == {CLAN: True} and rig.player.warnings() == []
    tune(rig, NEOX_LIVE, subs=False)
    assert rig.player.service.tasks.reads == 2                  # once per LiveTV tune


def test_without_a_fresh_list_the_services_list_is_used_with_a_warning(tmp_path):
    rig = Rig(tmp_path)
    rig.player.service.tasks.channels = CHANNELS + [CLAN_ROW]
    rig.player.service.tasks.fail = OSError('channels.json: Input/output error')
    epoch = tune(rig, CLAN_LIVE, subs=True)
    assert rig.player._liveSubsState['key'] == 'name:Clan'      # the start list has no Clan
    epoch = tune(rig, WARNER_LIVE, subs=True)
    assert rig.player._liveSubsState == settled(rig, epoch, WARNER, WARNER_LIVE, seed=True)
    assert rig.player.warnings() == ['_liveSubsChannels, channel list unavailable: '
                                     'channels.json: Input/output error'] * 2


@pytest.mark.parametrize('broken', ['none', 'unavailable'])
def test_no_channel_list_still_works(tmp_path, broken):
    """No channel list at all (channels.json empty, or unreadable with no list loaded at start): the
    key falls back to the name; a failed read is logged, nothing raises."""
    rig = Rig(tmp_path)
    rig.player.service.tasks.channels = None
    if broken == 'unavailable':
        rig.player.service.tasks.fail = OSError('Input/output error')
        rig.player.service.channels = None
    tune(rig, WARNER_REPLAY, subs=False)
    rig.kodi.state['subs'] = True
    tick(rig)
    assert rig.saved() == {'name:Warner TV': True}
    assert len(rig.player.warnings()) == (1 if broken == 'unavailable' else 0)


# === record: while a LiveTV channel plays =========================================================

def test_nothing_is_recorded_before_the_playback_is_settled(tmp_path):
    rig = Rig(tmp_path)
    rig.kodi.state.update(WARNER_LIVE, subs=True)
    tick(rig, 3)
    assert rig.kodi.reads == [] and rig.files.writes == [] and rig.saved() is None


def test_nothing_is_recorded_for_another_epoch(tmp_path):
    rig = Rig(tmp_path, memory={WARNER: True})
    tune(rig, WARNER_REPLAY, subs=False)
    rig.player._play_epoch += 1                                # onPlayBackEnded: Kodi's next show is coming
    rig.kodi.reads[:] = []
    rig.kodi.state['subs'] = False
    tick(rig, 2)
    assert rig.kodi.reads == [] and rig.files.writes == []


def test_nothing_is_recorded_when_no_video_plays(tmp_path):
    rig = Rig(tmp_path, memory={WARNER: True})
    tune(rig, WARNER_LIVE, subs=True)
    rig.kodi.reads[:] = []
    rig.kodi.state.update(playing=False, subs=False)
    tick(rig, 2)
    assert rig.kodi.reads == [] and rig.files.writes == []


@pytest.mark.parametrize('now', [NEOX_LIVE, CUSTOM_CN, LIBRARY, dict(WARNER_LIVE, name=''),
                                 dict(WARNER_LIVE, number='13'), dict(LIBRARY, number='12', name='Warner TV')],
                         ids=['zap', 'custom-namesake', 'not-livetv', 'no-name', 'other-number', 'labels-left-over'])
def test_nothing_is_recorded_when_what_plays_changed(tmp_path, now):
    """Kodi already shows another channel (or no LiveTV, even with the channel's labels left over)
    while the epoch hasn't moved yet: the labels differ from the settled ones (same file here)."""
    rig = Rig(tmp_path, memory={WARNER: True, NEOX: True})
    tune(rig, WARNER_LIVE, subs=True)
    state = rig.player._liveSubsState
    rig.kodi.state.update(now, subs=False)
    tick(rig, 2)
    assert rig.files.writes == [] and rig.player._liveSubs == {WARNER: True, NEOX: True}
    assert rig.player._liveSubsState is state


@pytest.mark.parametrize('memory', [{WARNER: True}, {}], ids=['saved', 'seed-pending'])
def test_nothing_is_recorded_when_kodi_already_shows_the_next_video(tmp_path, memory):
    """A RESTART replay of the same channel starts (Kodi's default: off) a moment before its
    onPlayBackStarted reaches the add-on: same labels, same epoch, another file -- not your choice."""
    rig = Rig(tmp_path, memory=memory)
    tune(rig, WARNER_LIVE, subs=True)
    state = rig.player._liveSubsState
    replay = 'pvr://guide/0071/restart.epg'
    rig.kodi.state.update(WARNER_REPLAY, subs=False, file=replay)
    tick(rig, 3)
    assert rig.files.writes == [] and rig.player._liveSubs == memory
    assert rig.player._liveSubsState is state
    tune(rig, WARNER_REPLAY, subs=False, file=replay)          # its events arrive: the replay's own restore
    assert rig.player.switches == ([ON] if memory else [])


def test_a_new_playback_starting_while_the_labels_are_read_is_not_recorded(tmp_path):
    """A RESTART replay of the same channel has the same key: the epoch is read again after Kodi."""
    rig = Rig(tmp_path, memory={WARNER: True})
    tune(rig, WARNER_LIVE, subs=True)
    rig.kodi.state['subs'] = False

    def event(name):
        if name == 'VideoPlayer.HasSubtitles': rig.player._play_epoch += 1
    rig.kodi.on_read = event
    tick(rig)
    assert rig.files.writes == [] and rig.player._liveSubs == {WARNER: True}


def test_a_toggle_on_and_off_is_saved(tmp_path):
    rig = Rig(tmp_path)
    epoch = tune(rig, WARNER_REPLAY, subs=False)
    tick(rig, 3)
    assert rig.files.writes == []                             # a replay without memory: no change, nothing
    rig.kodi.state['subs'] = True                             # you switch them on
    tick(rig)
    assert rig.saved() == {WARNER: True}
    assert rig.player._liveSubsState == settled(rig, epoch, WARNER, WARNER_REPLAY, baseline=True)
    assert rig.player.infos()[-1] == 'live subtitles: %s saved on' % WARNER
    tick(rig, 3)
    assert len(rig.files.writes) == 1
    rig.kodi.state['subs'] = False                            # and off again
    tick(rig)
    assert rig.saved() == {WARNER: False}
    assert rig.player._liveSubsState['baseline'] is False
    assert rig.player.infos()[-1] == 'live subtitles: %s saved off' % WARNER
    tick(rig, 3)
    assert len(rig.files.writes) == 2


def test_no_track_saves_nothing(tmp_path):
    """A show without subtitles doesn't erase "on"."""
    rig = Rig(tmp_path, memory={WARNER: True})
    tune(rig, WARNER_LIVE, subs=True)
    rig.kodi.state.update(subs=False, has=False)
    tick(rig, 3)
    assert rig.files.writes == [] and rig.player._liveSubs == {WARNER: True}


def test_a_first_live_tune_saves_its_starting_state_once(tmp_path):
    """Kodi's own saved setting for the live channel (Warner TV: on) is saved on the first tick, so
    a RESTART straight after carries it over."""
    rig = Rig(tmp_path)
    tune(rig, WARNER_LIVE, subs=True)
    assert rig.player.switches == []
    tick(rig)
    assert rig.saved() == {WARNER: True} and len(rig.files.writes) == 1
    assert rig.player._liveSubsState['seed'] is False and rig.player._liveSubsState['baseline'] is True
    tick(rig, 5)
    assert len(rig.files.writes) == 1
    tune(rig, WARNER_REPLAY, subs=False)                      # the RESTART pop-up
    assert rig.player.switches == [ON]


def test_a_first_live_tune_without_a_track_seeds_when_the_track_comes(tmp_path):
    """The tick that sees the track first only settles it (has, baseline); the next one seeds."""
    rig = Rig(tmp_path)
    epoch = tune(rig, NEOX_LIVE, subs=False, has=False)
    tick(rig, 2)
    assert rig.files.writes == []
    rig.kodi.state['has'] = True
    tick(rig)
    assert rig.files.writes == [] and rig.player.switches == []
    assert rig.player._liveSubsState == settled(rig, epoch, NEOX, NEOX_LIVE, has=True, seed=True, baseline=False)
    tick(rig)
    assert rig.saved() == {NEOX: False}


def test_a_replay_without_memory_saves_only_a_change(tmp_path):
    rig = Rig(tmp_path)
    tune(rig, NEOX_REPLAY, subs=False)
    tick(rig, 4)
    assert rig.saved() is None and rig.files.writes == []
    rig.kodi.state['subs'] = True
    tick(rig, 2)
    assert rig.saved() == {NEOX: True} and len(rig.files.writes) == 1


def test_the_memory_is_copied_on_write(tmp_path):
    """The play thread reads the memory without a lock: a recorded change is a new dict."""
    rig = Rig(tmp_path, memory={NEOX: False})
    tune(rig, WARNER_REPLAY, subs=False)
    before = rig.player._liveSubs
    rig.kodi.state['subs'] = True
    tick(rig)
    assert before == {NEOX: False} and rig.player._liveSubs == {NEOX: False, WARNER: True}
    assert rig.player._liveSubs is not before


def test_custom_playback_after_livetv_is_never_recorded(tmp_path):
    """A Custom channel tune bumps the epoch and never restores: the settled LiveTV state is stale, so
    a toggle on the Custom "Cartoon Network" (304) never lands on the imported one (303)."""
    rig = Rig(tmp_path, memory={CN: False})
    tune(rig, CN_LIVE, subs=False)
    rig.player._play_epoch += 2                                # the Custom tune: _onPlay's PseudoTV path
    rig.kodi.state.update(CUSTOM_CN, subs=True)
    tick(rig, 3)
    assert rig.files.writes == [] and rig.player._liveSubs == {CN: False}


def test_a_slow_save_never_overwrites_a_newer_playbacks_state(tmp_path):
    """Review finding: the write stalls while a newer playback (a zap to Neox live) settles; the old
    playback's state must not be put back over it, or the newer one would never record."""
    rig = Rig(tmp_path)
    tune(rig, WARNER_REPLAY, subs=False)
    rig.kodi.state['subs'] = True                             # you switch them on
    newer = []

    def stall(data):
        rig.files.on_write = None
        newer.append((tune(rig, NEOX_LIVE, subs=False), rig.player._liveSubsState))
    rig.files.on_write = stall
    tick(rig)
    assert rig.saved() == {WARNER: True}
    (epoch, state), = newer
    assert rig.player._liveSubsState is state
    assert state == settled(rig, epoch, NEOX, NEOX_LIVE, seed=True, baseline=False)
    tick(rig)                                                 # the newer playback records: its seed
    assert rig.saved() == {WARNER: True, NEOX: False}


# --- a subtitle track that appears after the playback settled -------------------------------------

@pytest.mark.parametrize('saved, shown, switch', [(True, False, ON), (False, True, OFF)], ids=['on', 'off-forced'])
def test_a_late_track_gets_the_channels_choice_once(tmp_path, saved, shown, switch):
    """Settled without a track; the track appears (Kodi showing it or not): the channel's choice is
    applied once through _showLiveSubtitles, nothing is saved, has/baseline follow; later ticks save
    only a real change of yours."""
    rig = Rig(tmp_path, memory={WARNER: saved})
    rig.kodi.queued = True                                     # Kodi's player applies it by the next tick
    epoch = tune(rig, WARNER_REPLAY, subs=False, has=False)
    assert rig.player.switches == []
    assert rig.player._liveSubsState == settled(rig, epoch, WARNER, WARNER_REPLAY, has=False, baseline=False)
    tick(rig, 2)                                               # no track yet
    assert rig.player.switches == [] and rig.files.writes == []
    rig.kodi.state.update(has=True, subs=shown)                # the track appears
    tick(rig)
    assert rig.player.switches == [switch]
    assert rig.player.jsonRPC.sent == ([TRACKS_READ] if saved else [])
    assert rig.player.infos()[-1] == 'live subtitles: %s restored %s (track appeared)' % (WARNER,
                                                                                       'on' if saved else 'off')
    assert rig.player._liveSubsState == settled(rig, epoch, WARNER, WARNER_REPLAY, has=True, baseline=saved)
    assert rig.files.writes == []
    tick(rig, 3)
    assert rig.player.switches == [switch] and rig.files.writes == []
    assert rig.kodi.state['subs'] is saved and rig.player._liveSubs == {WARNER: saved}
    rig.kodi.state['subs'] = not saved                         # you switch them
    tick(rig, 2)
    assert rig.saved() == {WARNER: not saved} and len(rig.files.writes) == 1


@pytest.mark.parametrize('shown', [False, True])
def test_a_late_track_without_a_saved_choice_applies_and_saves_nothing(tmp_path, shown):
    rig = Rig(tmp_path)
    epoch = tune(rig, WARNER_REPLAY, subs=False, has=False)
    rig.kodi.state.update(has=True, subs=shown)
    tick(rig, 3)
    assert rig.player.switches == [] and rig.files.writes == [] and rig.saved() is None
    assert rig.player._liveSubsState == settled(rig, epoch, WARNER, WARNER_REPLAY, has=True, baseline=shown)
    rig.kodi.state['subs'] = not shown                         # you switch them
    tick(rig)
    assert rig.saved() == {WARNER: not shown}


def test_a_late_track_apply_never_overwrites_a_newer_playbacks_state(tmp_path):
    rig = Rig(tmp_path, memory={WARNER: True})
    tune(rig, WARNER_REPLAY, subs=False, has=False)
    newer = []

    def zap(call):
        rig.player.on_switch = None
        newer.append((tune(rig, NEOX_LIVE, subs=False), rig.player._liveSubsState))
    rig.player.on_switch = zap
    rig.kodi.state['has'] = True
    tick(rig)
    (epoch, state), = newer
    assert rig.player._liveSubsState is state
    assert state == settled(rig, epoch, NEOX, NEOX_LIVE, seed=True, baseline=False)
    tick(rig)
    assert rig.saved() == {WARNER: True, NEOX: False}


# === the memory file ============================================================================

def test_the_path_is_built_on_every_call(tmp_path):
    rig = Rig(tmp_path)
    assert rig.player._liveSubsPath() == os.path.join(str(tmp_path), lh.MEMORY_FILE)
    moved = tmp_path / 'moved'
    type(rig.player)._liveSubsPath.__globals__['CACHE_LOC'] = str(moved)
    assert rig.player._liveSubsPath() == os.path.join(str(moved), lh.MEMORY_FILE)


def test_the_memory_survives_a_kodi_restart(tmp_path):
    rig = Rig(tmp_path)
    tune(rig, WARNER_REPLAY, subs=False)
    rig.kodi.state['subs'] = True
    tick(rig)
    tune(rig, NEOX_REPLAY, subs=True)
    rig.kodi.state['subs'] = False
    tick(rig)
    again = Rig(tmp_path)                                      # a new service: __init__ loads the file
    assert again.player._liveSubs == {WARNER: True, NEOX: False}
    tune(again, WARNER_REPLAY, subs=False)
    tune(again, NEOX_LIVE, subs=True)
    assert again.player.switches == [ON, OFF]


@pytest.mark.parametrize('raw, loaded', [
    ('{"TNT@movistarplus": tru', {}),
    ('[true, false]', {}),
    ('"on"', {}),
    ('null', {}),
    ('', {}),
    ('{"TNT@movistarplus": "on", "": true, "NEOX@movistarplus": false, "name:Warner TV": 1}', {NEOX: False}),
], ids=['truncated', 'list', 'text', 'null', 'empty', 'bad-entries'])
def test_a_damaged_or_unusable_file_loads_what_it_can(tmp_path, raw, loaded):
    rig = Rig(tmp_path, raw=raw)
    assert rig.player._liveSubs == loaded
    tune(rig, WARNER_REPLAY, subs=False)
    rig.kodi.state['subs'] = True
    tick(rig)
    assert rig.saved() == dict(loaded, **{WARNER: True})         # the next save writes a good file


def test_a_missing_file_loads_empty(tmp_path):
    rig = Rig(tmp_path)
    assert rig.player._liveSubs == {} and rig.player.warnings() == []


def test_a_load_failure_is_logged_and_loads_empty(tmp_path):
    rig = Rig(tmp_path, memory={WARNER: True})
    rig.files.fail_read = OSError('Input/output error')
    assert rig.player._loadLiveSubs() == {}
    assert len(rig.player.warnings()) == 1 and 'Input/output error' in rig.player.warnings()[0]


@pytest.mark.parametrize('failure', ['false', 'raise'])
def test_a_save_failure_is_logged_and_never_raises(tmp_path, failure):
    """The file keeps its previous content; the memory in RAM still applies."""
    rig = Rig(tmp_path, memory={NEOX: True})
    rig.files.fail_write = failure
    assert rig.player._saveLiveSubs({WARNER: True}) is False
    tune(rig, WARNER_REPLAY, subs=False)
    rig.kodi.state['subs'] = True
    tick(rig)
    warnings = rig.player.warnings()
    assert len(warnings) == 2 and all(msg.startswith('_saveLiveSubs, could not write') for msg in warnings)
    assert rig.saved() == {NEOX: True}
    assert rig.player._liveSubs == {NEOX: True, WARNER: True}
    tune(rig, WARNER_REPLAY, subs=False)                      # Kodi's next show
    assert rig.player.switches == [ON]


def test_a_save_returns_true_when_written(tmp_path):
    rig = Rig(tmp_path)
    assert rig.player._saveLiveSubs({WARNER: True}) is True and rig.saved() == {WARNER: True}


# === errors never escape ==========================================================================

def test_a_kodi_failure_in_the_restore_is_logged(tmp_path):
    rig = Rig(tmp_path, memory={WARNER: True})
    rig.kodi.fail = RuntimeError('player gone')
    tune(rig, WARNER_REPLAY, subs=False)
    assert rig.player.warnings() == ['_restoreLiveSubtitles, failed: player gone']
    assert rig.player._liveSubsState is None and rig.player.switches == []


def test_a_failing_switch_in_the_restore_is_logged(tmp_path):
    rig = Rig(tmp_path, memory={WARNER: True})

    def broken(call): raise RuntimeError('no player')
    rig.player.on_switch = broken
    tune(rig, WARNER_REPLAY, subs=False)
    assert rig.player.warnings() == ['_restoreLiveSubtitles, failed: no player']
    assert rig.player._liveSubsState is None


def test_a_failing_track_read_in_the_restore_is_logged(tmp_path):
    rig = Rig(tmp_path, memory={WARNER: True})
    rig.player.jsonRPC.fail = RuntimeError('JSON-RPC unavailable')
    tune(rig, WARNER_REPLAY, subs=False)
    assert rig.player.warnings() == ['_restoreLiveSubtitles, failed: JSON-RPC unavailable']
    assert rig.player.switches == [] and rig.player._liveSubsState is None


def test_a_kodi_failure_in_the_record_is_logged(tmp_path):
    rig = Rig(tmp_path, memory={WARNER: True})
    tune(rig, WARNER_LIVE, subs=True)
    rig.kodi.fail = RuntimeError('player gone')
    rig.kodi.state['subs'] = False
    tick(rig)
    assert rig.player.warnings() == ['_chkLiveSubtitles, failed: player gone']
    assert rig.files.writes == [] and rig.player._liveSubs == {WARNER: True}


def test_a_failing_isPlayingVideo_in_the_record_is_logged(tmp_path):
    rig = Rig(tmp_path, memory={WARNER: True})
    tune(rig, WARNER_LIVE, subs=True)

    def broken(): raise RuntimeError('no player')
    rig.player.isPlayingVideo = broken
    tick(rig)
    assert rig.player.warnings() == ['_chkLiveSubtitles, failed: no player']


@pytest.mark.parametrize('playing, expected', [
    ('pvr://guide/0071/replay-3.epg', 'pvr://guide/0071/replay-3.epg'), (None, ''), ('', ''),
    (RuntimeError('Kodi is not playing any media file'), ''),
], ids=['file', 'none', 'empty', 'raises'])
def test_the_playing_file_reads_empty_when_kodi_cant_say(tmp_path, playing, expected):
    rig = Rig(tmp_path)
    if isinstance(playing, Exception): rig.player.file_fail = playing
    else: rig.kodi.state['file'] = playing
    assert rig.player._liveSubsFile() == expected
    assert rig.player.warnings() == []


# === the wiring, run for real ====================================================================

def test_onplay_livetv_branch_restores_after_scheduling_the_popup(tmp_path):
    """The real _onPlay on a replay (no sysInfo: playingItem {}): the Restart pop-up is scheduled,
    then the channel's choice is applied and the playback settled with _onPlay's own epoch."""
    rig = Rig(tmp_path, memory={WARNER: True})
    rig.kodi.state.update(WARNER_REPLAY, subs=False, has=True, file=playing_file(WARNER_REPLAY, 9))
    rig.player._play_epoch = 9
    rig.player._onPlay({}, 9)
    assert rig.player.calls[-3:] == [('toggleReplay', True, 9), ('sendJSON', 'Player.GetProperties'), ON]
    assert rig.player._liveSubsState == settled(rig, 9, WARNER, WARNER_REPLAY)
    assert rig.player.lastSubState is False                    # read before the restore, as before imports.67


def test_onplay_library_playback_is_left_alone(tmp_path):
    rig = Rig(tmp_path, memory={WARNER: True})
    rig.kodi.state.update(LIBRARY, subs=False)
    rig.player._play_epoch = 3
    rig.player._onPlay({'file': '/movies/x.mkv'}, 3)
    assert ('toggleReplay', True, 3) in rig.player.calls and rig.player.switches == []
    assert rig.player.monitor.waits == [] and rig.player._liveSubsState is None


def _run_chkidle(pseudotv, ticks):
    """The real Monitor._chkIdle for `ticks` loop turns (then the service shuts down)."""
    calls = []
    answers = iter([False] * (2 * ticks) + [True])
    player = types.SimpleNamespace(isPlayingPseudoTV=lambda: pseudotv, _onIdle=lambda: calls.append('onIdle'),
                                   _chkLiveSubtitles=lambda: calls.append('chkLiveSubtitles'),
                                   _chkTransition=lambda: calls.append('chkTransition'))
    monitor = types.SimpleNamespace(isRunning=False, idleTime=0, isIdle=False, abortRequested=lambda: False,
                                    service=types.SimpleNamespace(_shutdown=lambda wait: next(answers)),
                                    player=player, log=lambda msg, level=None: None)
    chk = _function('Monitor', '_chkIdle', BUILTIN=types.SimpleNamespace(getIdle=lambda: 0), OSD_TIMER=5)
    chk(monitor)
    assert monitor.isRunning is False
    return calls


def test_chkidle_reads_live_subtitles_on_every_not_custom_tick():
    assert _run_chkidle(False, 3) == ['chkLiveSubtitles', 'chkTransition'] * 3


def test_chkidle_never_reads_live_subtitles_while_a_custom_channel_plays():
    assert 'chkLiveSubtitles' not in _run_chkidle(True, 3)


# === MTV-style: an enabled channel and a disabled one with its name ================================

MTV     = 'MTV.us@epgbest_46310'                                 # MTV, listed in Kodi
MTV_OFF = 'MTV@movistarplus'                                     # an older MTV import, disabled
MTV_CHANNELS = CHANNELS + [
    {'id': MTV_OFF, 'type': 'import', 'number': 52,  'name': 'MTV', 'enabled': False},
    {'id': MTV,     'type': 'import', 'number': 410, 'name': 'MTV', 'enabled': True},
]
MTV_LIVE   = {'live': True,  'epgtag': False, 'number': '58', 'name': 'MTV'}   # Kodi's own number
MTV_REPLAY = {'live': False, 'epgtag': True,  'number': '58', 'name': 'MTV'}


def test_the_mtv_case(tmp_path):
    """Only listed channels count: the disabled namesake doesn't make "MTV" ambiguous, and Kodi's
    number (its own count, neither entry's) isn't needed. The enabled one's id, live and in replays."""
    rig = Rig(tmp_path, channels=MTV_CHANNELS)
    epoch = tune(rig, MTV_LIVE, subs=True)                     # Kodi's own setting for the live channel: on
    assert rig.player._liveSubsState == settled(rig, epoch, MTV, MTV_LIVE, seed=True)
    tick(rig, 2)
    assert rig.saved() == {MTV: True}
    tune(rig, MTV_REPLAY, subs=False)                          # the RESTART pop-up
    assert rig.player.switches == [ON]
    rig.kodi.state['subs'] = False                             # you switch them off in the replay
    tick(rig)
    assert rig.saved() == {MTV: False}
    tune(rig, MTV_LIVE, subs=True)                             # back to live: off
    assert rig.player.switches == [ON, OFF]
    tick(rig, 2)
    assert rig.saved() == {MTV: False} and len(rig.files.writes) == 2
    assert rig.player.warnings() == []


# === the 2026-11-10 case, end to end ===============================================================

def test_the_2026_11_10_case(tmp_path):
    rig = Rig(tmp_path)                                         # imports.67's first start: no file yet
    # 15:46:21 Warner TV live: Kodi's own saved setting for the live channel is on
    tune(rig, WARNER_LIVE, subs=True)
    tick(rig, 3)
    assert rig.saved() == {WARNER: True}
    # 15:46:29 the RESTART pop-up opens the replay: a new video for Kodi, subtitles off -> on
    tune(rig, WARNER_REPLAY, subs=False)
    tick(rig, 3)
    # 17:20:12 the replay ends; 17:20:14 Kodi opens the next show by itself, subtitles off -> on
    rig.player._play_epoch += 1
    tune(rig, WARNER_REPLAY, subs=False)
    tick(rig, 3)
    assert rig.player.switches == [ON, ON]
    # 17:49:05 a Neox replay: you switch subtitles on, then off
    tune(rig, NEOX_REPLAY, subs=False)
    rig.kodi.state['subs'] = True
    tick(rig, 2)
    rig.kodi.state['subs'] = False
    tick(rig, 2)
    assert rig.saved() == {WARNER: True, NEOX: False}
    # Neox live (Kodi's own setting for it: on) -> off; Warner TV live keeps on
    tune(rig, NEOX_LIVE, subs=True)
    tune(rig, WARNER_LIVE, subs=False)
    assert rig.player.switches == [ON, ON, OFF, ON]
    tick(rig, 3)
    assert rig.saved() == {WARNER: True, NEOX: False}
    assert len(rig.files.writes) == 3                          # the seed, Neox on, Neox off
    assert [msg for msg in rig.player.infos() if 'saved' in msg] == [
        'live subtitles: %s saved on' % WARNER, 'live subtitles: %s saved on' % NEOX,
        'live subtitles: %s saved off' % NEOX]
    assert rig.player.warnings() == []



# === review round 3 (2026-11-10): the track read, late tracks, the channel preview ==================

def test_the_track_read_skips_the_rpc_pause(tmp_path):
    """The read before an "on" switch is acted on at once: no RPC_Delay pause (delay=False), which
    would leave a second in which a zap goes unnoticed."""
    rig = Rig(tmp_path, memory={WARNER: True})
    tune(rig, WARNER_REPLAY, subs=False)
    assert rig.player.switches == [ON] and rig.player.jsonRPC.delays == [False]


def test_a_zap_during_the_track_read_cancels_the_switch(tmp_path):
    """A newer playback starting while the track list is read: nothing switched (the read came
    back for the old channel), not settled."""
    rig = Rig(tmp_path, memory={WARNER: True})

    def newer(): rig.player._play_epoch += 1
    rig.player.jsonRPC.during = newer
    tune(rig, WARNER_REPLAY, subs=False)
    assert rig.player.switches == [] and rig.player.jsonRPC.sent == [TRACKS_READ]
    assert rig.player._liveSubsState is None


def test_off_checks_the_epoch_right_before_the_switch(tmp_path):
    rig = Rig(tmp_path, memory={WARNER: False})

    def newer(): rig.player._play_epoch += 1
    rig.player.service.tasks.during = newer
    tune(rig, WARNER_REPLAY, subs=True)
    assert rig.player.switches == [] and rig.player._liveSubsState is None


@pytest.mark.parametrize('now', ['other-channel', 'next-video'])
def test_a_late_track_is_applied_only_to_the_settled_channel_and_video(tmp_path, now):
    """A playback settled without a subtitle track: when Kodi already shows another channel or the
    next video (before the add-on hears of it), a track there gets nothing of this channel's choice
    and nothing is saved."""
    rig = Rig(tmp_path, memory={WARNER: True})
    tune(rig, WARNER_LIVE, subs=False, has=False)
    state = rig.player._liveSubsState
    assert state is not None and state['has'] is False
    if now == 'other-channel':
        rig.kodi.state.update(NEOX_LIVE, has=True, subs=False, file=playing_file(NEOX_LIVE, 99))
    else:
        rig.kodi.state.update(has=True, subs=False, file='pvr://guide/0071/next-show.epg')
    tick(rig, 2)
    assert rig.player.switches == [] and rig.files.writes == []
    assert rig.saved() == {WARNER: True} and rig.player._liveSubsState is state


def test_a_late_track_apply_waits_before_reading_the_state(tmp_path):
    """Kodi queues the switch: the late-track path waits APPLY_WAIT_S and settles the baseline on
    the state it left, so the next ticks don't save Kodi's old "off" over the channel's "on"."""
    rig = Rig(tmp_path, memory={WARNER: True})
    tune(rig, WARNER_REPLAY, subs=False, has=False)
    rig.kodi.queued = True
    rig.kodi.state.update(has=True)
    waits = len(rig.player.monitor.waits)
    tick(rig)
    assert rig.player.switches == [ON]
    assert rig.player.monitor.waits[waits:] == [lh.APPLY_WAIT_S]
    assert rig.player._liveSubsState['has'] is True and rig.player._liveSubsState['baseline'] is True
    tick(rig, 3)
    assert rig.files.writes == [] and rig.saved() == {WARNER: True}


def test_a_late_track_apply_that_fails_stops_recording_this_playback(tmp_path):
    """The switch to a late track fails (JSON-RPC down): one warning, not retried every tick, and
    nothing recorded for this playback (Kodi's "off" would be saved over the channel's "on")."""
    rig = Rig(tmp_path, memory={WARNER: True})
    tune(rig, WARNER_REPLAY, subs=False, has=False)
    rig.kodi.state.update(has=True)
    rig.player.jsonRPC.fail = RuntimeError('rpc down')
    tick(rig, 3)
    assert rig.player._liveSubsState is None
    assert len([w for w in rig.player.warnings() if 'could not be applied' in w]) == 1
    assert rig.player.jsonRPC.sent.count(TRACKS_READ) == 1
    assert rig.files.writes == [] and rig.saved() == {WARNER: True}


def _preview(rig, on, labels=None):
    rig.kodi.state.update(preview=on, **(labels or {}))


def test_the_recheck_waits_for_the_channel_preview_to_end(tmp_path):
    """Up/Down during live TV previews another channel (its labels) while this one plays: the
    re-check waits until the preview ends and settles on the playing channel."""
    rig = Rig(tmp_path, memory={WARNER: True})
    steps = []

    def during():
        steps.append(None)
        if len(steps) == 1: _preview(rig, True, {'number': NEOX_LIVE['number'], 'name': NEOX_LIVE['name']})
        elif len(steps) == 3: _preview(rig, False, {'number': WARNER_REPLAY['number'], 'name': WARNER_REPLAY['name']})
    rig.player.monitor.during = during
    epoch = tune(rig, WARNER_REPLAY, subs=False)
    assert rig.player.monitor.waits == [lh.RECHECK_S, 0.25, 0.25]
    assert rig.player._liveSubsState == settled(rig, epoch, WARNER, WARNER_REPLAY, baseline=True)


def test_a_preview_that_never_ends_leaves_the_playback_unsettled(tmp_path):
    rig = Rig(tmp_path, memory={WARNER: True})
    rig.player.monitor.during = once(lambda: _preview(rig, True))
    tune(rig, WARNER_REPLAY, subs=False)
    assert rig.player.monitor.waits.count(0.25) == 40           # 10 s in 0.25 s steps
    assert rig.player._liveSubsState is None


def test_nothing_is_recorded_while_the_channel_preview_shows(tmp_path):
    rig = Rig(tmp_path, memory={WARNER: True})
    tune(rig, WARNER_REPLAY, subs=False)
    _preview(rig, True)
    rig.kodi.state.update(subs=False)                            # the preview's labels and state
    tick(rig, 2)
    assert rig.files.writes == []
    _preview(rig, False)
    tick(rig)
    assert rig.saved() == {WARNER: False}                        # your change, once the preview ended
