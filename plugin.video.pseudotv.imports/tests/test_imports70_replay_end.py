# -*- coding: utf-8 -*-
"""imports.70: LiveTV replays frozen at their end move on -- the Kodi glue in services.py.

A replay opened while its show is on air (the RESTART pop-up, the guide's 'Play
programme', Kodi's own next show) is a growing live stream on Movistar+, and
inputstream.adaptive never ends its last period: on 2026-11-15 one froze on its
last second for 1 h 26 min. services.py now watches for that and does what Kodi
does at a natural end. The decisions live in replayend_helpers (tested in
test_imports70_replayend_helpers.py); services.py only reads Kodi and acts:
  - Player._chkReplayEnd, Monitor._chkIdle's ~1 s tick right after
    _chkTransition: the only writer of _replayEndTrack, Kodi reads only
    (_replayEndSample), one ERROR line per playback on a failure (written even with debug off);
  - Player._lookupReplayEnd (@threadit), once per replay 5 s after it
    started: Player.GetItem + PVR.GetChannels, the only writer of
    _replayEndFacts;
  - Player._moveReplayEnd (@threadit): the RESTART lock without waiting, then
    _runReplayEnd: Kodi's own flag (_replayEndAutoplay), Kodi's next-show rule
    (jsonrpc.getPVRBroadcastTimes), the last full-screen check, and
    _replayEndOpen (imports.62's watch_outcome) with its fallbacks.

services.py can't be imported under the test stubs, so (as in
test_imports67_livetv_subtitles.py, whose helpers this reuses) the real
methods are compiled out of the source with their decorators dropped: the two
workers run inline, in the tick that starts them. Kodi is a model on the
test's clock: Movistar+'s growing window (it opens 174 s before the guide start
and closes 720 s after the guide end, as measured), the player's position and
length, pause, seeks, the guide, Player.Open (switching in place, as
VideoPlayer does, or refused / failing in the ways watch_outcome tells apart),
a natural end with Kodi's own next show, and the time passing while the
add-on waits (service._shutdown). The structure (where the calls sit, who
writes what) is checked with ast.
"""
import ast
import calendar
import json
import os
import time
import types
import xml.etree.ElementTree as ET

import pytest

import replayend_helpers as reh
import restart_helpers as rh
from test_imports67_livetv_subtitles import SERVICES, S_TREE, _calls, _class, _function, _method, _owner_map

HERE  = os.path.dirname(os.path.abspath(__file__))
ADDON = os.path.dirname(HERE)

NEW_METHODS = ('_chkReplayEnd', '_replayEndSample', '_lookupReplayEnd', '_moveReplayEnd', '_runReplayEnd',
               '_replayEndOpen', '_replayEndAutoplay')


def utc(epoch):
    return time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(epoch))


def decode(plot):
    """Stand-in for Globals._decodePlot: Custom plots carry an item= payload."""
    if 'item=' not in plot: return {}
    return json.loads(plot.split('item=', 1)[1])


CUSTOM_PLOT = 'Plot [COLOR item=%s' % json.dumps({'citem': {'id': 'abc@PseudoTV_Live'}, 'start': 0})

# The 2026-11-15 case (the clock time is illustrative; the offsets are measured): an 86-minute show
# whose window opens 174 s before its guide start and closes 720 s after its guide end.
S = calendar.timegm((2026, 11, 15, 15, 0, 0, 0, 0, 0))
E = S + 86 * 60
LEAD, TAIL = 174, 720
CID, OTHER_CID = 47, 12
PREV, BID, NEXT, LATER, OTHER = 90209, 90210, 90211, 90212, 555


def shows(next_start=E):
    """The guide: (broadcastid, channelid, start, end, plot)."""
    return [(PREV, CID, S - 3600, S, 'The previous show'),
            (BID, CID, S, E, 'Servicio Secreto'),
            (NEXT, CID, next_start, next_start + 3600, 'The next show'),
            (LATER, CID, next_start + 3600, next_start + 7200, 'A later show'),
            (OTHER, OTHER_CID, S - 1800, E + 9000, 'Another channel')]


class Lock(object):
    """Player._restart_lock: a blocking acquire while it is held would wait for the restart (a
    deadlock in a test), so it raises instead; every acquire is recorded."""

    def __init__(self):
        self.held, self.acquires = False, []

    def acquire(self, blocking=True, timeout=-1):
        self.acquires.append(blocking)
        if self.held:
            if blocking: raise AssertionError('a blocking acquire would wait for the restart')
            return False
        self.held = True
        return True

    def release(self):
        if not self.held: raise RuntimeError('release unlocked lock')
        self.held = False


class Kodi(object):
    """What Kodi plays and shows, on the rig's clock. `playing` is None or {'kind': 'replay' |
    'live', 'bid', 'cid', 't_open', 'offset', 'paused_at', 'vod', 'w0', 'w1', 'length', 'props'}.
    `behave[(kind, id)]` is what Player.Open does: 'ok' (switches in place), 'refuse' (InvalidParams:
    Player.Open says no), 'ignore' (says OK, nothing happens), 'error' (the open fails with a playback
    error), 'gone' (nothing plays, no error), 'other' (another channel plays: the viewer zapped). What
    Player.Open starts happens the next time the add-on waits (Kodi posts it), or already during
    playerOpen's own RPC_Delay pause after Kodi's reply when `rpc_delay` is set."""

    def __init__(self, rig, guide):
        self.rig, self.guide = rig, list(guide)
        self.playing, self.pending, self.behave = None, [], {}
        self.window, self.key_at = True, rig.t - 9999          # no key pressed for a long time
        self.autoplay, self.open_delay, self.rpc_delay = True, 0.0, 0.0

    def show(self, bid):
        return [s for s in self.guide if s[0] == bid][0]

    def on_now(self, cid, t):
        now = [s for s in self.guide if s[1] == cid and s[2] <= t < s[3]]
        return now[0] if now else None

    def _start(self, item):
        self.playing = dict(item, t_open=self.rig.t, offset=0.0, paused_at=None)
        self.rig.player._play_epoch += 2                      # onPlayBackStarted + onAVStarted

    def play_show(self, bid, flag=None):
        """PlayEpgTag: a show on air plays as Movistar+'s growing window, a finished one as a
        recording. Kodi sets its flag from "Play next programme automatically"."""
        _, cid, start, end, _ = self.show(bid)
        flag = self.autoplay if flag is None else flag
        self._start({'kind': 'replay', 'bid': bid, 'cid': cid, 'w0': start - LEAD, 'w1': end + TAIL,
                     'vod': not (start <= self.rig.t < end), 'length': float(end - start + LEAD + TAIL),
                     'props': {reh.AUTOPLAY_PROPERTY: 'true'} if flag else {}})

    def play_live(self, cid):
        self._start({'kind': 'live', 'bid': None, 'cid': cid, 'vod': False, 'props': {}})

    def times(self):
        """(position, length) as the player reports them; it stops one second before the end of
        what a growing window holds."""
        p = self.playing
        if p is None: raise RuntimeError('Kodi is not playing any media file')
        t = self.rig.t
        played = (p['paused_at'] if p['paused_at'] is not None else t) - p['t_open'] + p['offset']
        if p['kind'] == 'live': return float(played), float(played)
        if p['vod']: return float(min(played, p['length'])), p['length']
        total = min(t, p['w1']) - p['w0']
        return float(min(played, total - 1)), float(total)

    def seek(self, pos):
        """The player jumps to `pos` and plays on from there."""
        p = self.playing
        now = p['paused_at'] if p['paused_at'] is not None else self.rig.t
        p['offset'] = pos - (now - p['t_open'])

    def pause(self):
        self.playing['paused_at'] = self.rig.t

    def resume(self):
        p = self.playing
        p['offset'] -= self.rig.t - p['paused_at']
        p['paused_at'] = None

    def press_key(self):
        self.key_at = self.rig.t

    def idle(self):
        """Whole s since the last key press (xbmc.getGlobalIdleTime): it grows with the clock."""
        return int(self.rig.t - self.key_at)

    def gone(self, stops=0, errors=0):
        self.playing = None
        self.rig.player._play_epoch += 1
        self.rig.player._play_stops += stops
        self.rig.player._play_errors += errors

    def natural_end(self):
        """Kodi's natural end: onPlayBackEnded, then its own next show (GetNextAutoplayItem) when
        the replay's flag is set."""
        p = self.playing
        self.gone()
        if p['props'].get(reh.AUTOPLAY_PROPERTY) == 'true':
            _, cid, start, end, _ = self.show(p['bid'])
            rows = [{'broadcastid': s[0], 'starttime': utc(s[2]), 'endtime': utc(s[3])} for s in self.guide if s[1] == cid]
            nxt = reh.next_broadcast(rows, start, end, float('inf'))
            if nxt: self.play_show(nxt['broadcastid'])
            else:   self.play_live(cid)
            self.rig.kodi_autoplays.append(self.rig.t)

    def player_open(self, params):
        item = params.get('item') or {}
        kind, value = ('broadcast', item['broadcastid']) if 'broadcastid' in item else ('channel', item.get('channelid'))
        self.rig.t += self.open_delay                         # RPC_Delay / a busy JSON-RPC lock
        behaviour = self.behave.get((kind, value), 'ok')
        if behaviour == 'refuse': return False
        if behaviour == 'ok':
            self.pending.append(lambda: self.play_show(value) if kind == 'broadcast' else self.play_live(value))
        elif behaviour == 'error': self.pending.append(lambda: self.gone(errors=1))
        elif behaviour == 'gone':  self.pending.append(self.gone)
        elif behaviour == 'other': self.pending.append(lambda: self.play_live(OTHER_CID))
        if self.rpc_delay: self.advance(self.rpc_delay)       # sendJSON's RPC_Delay pause: Kodi acts meanwhile
        return True

    def advance(self, dt):
        self.rig.t += dt
        pending, self.pending = self.pending, []
        for change in pending: change()
        p = self.playing
        if p is not None and p['vod'] and p['paused_at'] is None and self.times()[0] >= p['length']:
            self.natural_end()


class RPC(object):
    """Player.jsonRPC: the reads and Player.Open the new methods use, recorded with the time;
    `rig.hooks[name]` runs during that call (something happening in Kodi meanwhile)."""

    def __init__(self, rig):
        self.rig, self.calls = rig, []

    def _call(self, name, *args):
        self.calls.append((name,) + args)
        self.rig.rpc_times.append((name, self.rig.t))
        hook = self.rig.hooks.get(name)
        if hook is not None: hook()

    def getPlayerItemRef(self):
        self._call('GetItem')
        p = self.rig.kodi.playing
        if p is None: return {}
        if p['kind'] == 'live': return {'type': 'channel', 'id': p['cid'], 'label': 'Mega'}
        return {'type': 'unknown', 'id': self.rig.item_id or p['bid'], 'label': 'A show'}

    def getPVRChannelsNow(self):
        self._call('GetChannels')
        rows = []
        for cid in (OTHER_CID, CID):
            show = self.rig.kodi.on_now(cid, self.rig.t)
            bnow = {} if show is None else {'broadcastid': show[0], 'starttime': utc(show[2]), 'endtime': utc(show[3]),
                                            'plot': show[4], 'title': 'show %s' % show[0]}
            rows.append({'channelid': cid, 'hasarchive': True, 'broadcastnow': bnow})
        return rows

    def getPVRBroadcastTimes(self, channelid):
        self._call('GetBroadcasts', channelid)
        if self.rig.broadcasts_fail is not None: raise self.rig.broadcasts_fail
        return [{'broadcastid': s[0], 'label': 'show %s' % s[0], 'starttime': utc(s[2]), 'endtime': utc(s[3])}
                for s in self.rig.kodi.guide if s[1] == channelid]

    def playerOpen(self, params=None):
        self._call('Open', json.loads(json.dumps(params)))
        return self.rig.kodi.player_open(params or {})


class FakeXbmc(object):
    """The xbmc module the compiled methods see: Kodi's conditions answered from the model, keyed by
    the exact strings (anything else is a mistake)."""
    LOGDEBUG, LOGINFO, LOGWARNING, LOGERROR = 0, 1, 2, 3

    def __init__(self, rig):
        self.rig, self.reads = rig, []

    def getCondVisibility(self, condition):
        self.reads.append(condition)
        hook = self.rig.hooks.get(condition)
        if hook is not None: hook()
        p = self.rig.kodi.playing
        if condition == 'Pvr.IsPlayingEpgTag': return p is not None and p['kind'] == 'replay'
        if condition == 'Pvr.IsPlayingTv': return p is not None and p['kind'] == 'live'
        if condition == reh.BUSY: return p is not None and p['paused_at'] is not None
        if condition == reh.ACT_WINDOW: return bool(self.rig.kodi.window)
        raise AssertionError('unexpected condition %r' % condition)


class FakeBuiltin(object):
    """BUILTIN: only the reads the new methods may use (getInfoBool / getInfoLabel log every call
    at debug level, so they don't exist here)."""

    def __init__(self, rig):
        self.rig = rig

    def getIdle(self):
        hook = self.rig.hooks.get('getIdle')
        if hook is not None: hook()
        return self.rig.kodi.idle()

    def isBusyDialog(self):
        return False


class Service(object):
    """Player.service: _shutdown(wait) is the add-on waiting; Kodi goes on meanwhile."""

    def __init__(self, rig):
        self.rig = rig

    def _shutdown(self, wait=0):
        self.rig.kodi.advance(wait)
        return self.rig.closing_at is not None and self.rig.t >= self.rig.closing_at


class PlayerBase(object):
    """What the compiled methods use from services.Player besides themselves."""

    def __init__(self, rig):
        self.rig = rig
        self._play_epoch, self._play_stops, self._play_errors = 0, 0, 0
        self._restart_lock = Lock()
        self.jsonRPC, self.service = RPC(rig), Service(rig)
        self.logged, self.moves, self.lookups = [], [], []
        # as Player.__init__ does, last (test_state_is_per_instance_and_set_last_in_init checks it)
        self._replayEndFacts = None
        self._replayEndTrack = None

    def log(self, msg, level=0):
        self.logged.append((msg, level, self.rig.t))

    def isPlaying(self):
        return self.rig.kodi.playing is not None

    def getTime(self):
        return self.rig.kodi.times()[0]

    def getTotalTime(self):
        return self.rig.kodi.times()[1]

    def getPlayingItem(self):
        p = self.rig.kodi.playing
        if p is None: raise RuntimeError('Kodi is not playing any media file')
        props = dict(p['props'])
        return types.SimpleNamespace(getProperty=lambda key: props.get(key, ''))

    def stop(self):
        self.rig.stops.append(self.rig.t)
        if self.rig.kodi.playing is not None: self.rig.kodi.gone(stops=1)

    def at_level(self, level):
        return [m for m, lv, _ in self.logged if lv == level]


class Rig(object):
    """A fake player whose imports.70 methods are the real ones, on Kodi's model and the test's
    clock (it starts 10 minutes into the frozen show, when the 2026-11-15 RESTART came)."""

    def __init__(self, guide=None, t=S + 600):
        self.t, self.closing_at, self.item_id, self.broadcasts_fail = float(t), None, None, None
        self.hooks, self.rpc_times, self.stops, self.kodi_autoplays = {}, [], [], []
        self.before_move = None
        self.kodi = Kodi(self, shows() if guide is None else guide)
        self.xbmc = FakeXbmc(self)
        g = dict(xbmc=self.xbmc, BUILTIN=FakeBuiltin(self), replayend_helpers=reh, restart_helpers=rh,
                 clock_helpers=types.SimpleNamespace(now=lambda: self.t),
                 Globals=types.SimpleNamespace(_decodePlot=decode))
        ns = {name: _function('Player', name, **g) for name in NEW_METHODS}
        real_lookup, real_move = ns['_lookupReplayEnd'], ns['_moveReplayEnd']
        rig = self

        def _lookupReplayEnd(player, epoch):
            player.lookups.append((rig.t, epoch))
            return real_lookup(player, epoch)

        def _moveReplayEnd(player, facts, epoch):
            player.moves.append((rig.t, dict(facts), epoch))
            if rig.before_move is not None: rig.before_move()
            return real_move(player, facts, epoch)

        ns.update(_lookupReplayEnd=_lookupReplayEnd, _moveReplayEnd=_moveReplayEnd)
        self.player = type('FakePlayer', (PlayerBase,), ns)(self)

    def tick(self, times=1):
        """Monitor._chkIdle's not-Custom tick, about once a second."""
        for _ in range(times):
            self.kodi.advance(1.0)
            self.player._chkReplayEnd()

    def tick_until(self, t):
        while self.t < t: self.tick()

    def restart(self, bid=BID, flag=None):
        """The RESTART pop-up's Player.Open {broadcastid} of the show on now (or the guide's 'Play
        programme' on it)."""
        self.kodi.play_show(bid, flag)
        return self.t

    def opens(self):
        return [c[1] for c in self.player.jsonRPC.calls if c[0] == 'Open']

    def open_times(self):
        return [t for name, t in self.rpc_times if name == 'Open']

    def lines(self):
        return [m for m in self.player.at_level(FakeXbmc.LOGINFO) if m.startswith('replay end:')]

    def logged_at(self, line):
        return [t for m, _, t in self.player.logged if m == line]

    def warnings(self):
        return self.player.at_level(FakeXbmc.LOGWARNING)

    def errors(self):
        return self.player.at_level(FakeXbmc.LOGERROR)


def broadcast(bid):
    return {'item': {'broadcastid': bid}}


def channel(cid):
    return {'item': {'channelid': cid}}


def to_the_live_edge(rig, flag=None):
    """A replay opened 100 s before the guide end (the lookup arms it), then the viewer at the live
    edge: it freezes when the window closes. Returns that time, the first tick that reads the
    stuck position."""
    rig.restart(flag=flag)
    rig.tick(8)
    rig.kodi.seek(rig.kodi.times()[1] - 1)
    return E + TAIL


@pytest.fixture
def rig():
    return Rig(t=E - 100)


def moved_on(rig):
    """The line of the tick that decided to move on."""
    return [m for m in rig.lines() if m.endswith(': moving on')]


# === end to end ====================================================================================

def test_the_2026_11_15_case_moves_on_to_the_next_show():
    """RESTART 10 minutes into the show; the viewer watches from the start and the player stands
    still at 6053 of 6054 s: the next show opens exactly 20 s later, once, and is watched in turn."""
    rig = Rig()
    t_open = rig.restart()
    stuck = t_open + 6053
    rig.tick_until(stuck + 300)
    assert rig.player.lookups[0] == (t_open + 1 + reh.ARM_DELAY_S, 2)
    assert rig.opens() == [broadcast(NEXT)] and rig.open_times() == [stuck + reh.STALL_S]
    assert rig.kodi.playing['bid'] == NEXT and rig.stops == []
    assert rig.lines() == [
        'replay end: watching broadcast %s on channel %s, show ends %s UTC' % (BID, CID, utc(E)),
        'replay end: broadcast %s stood still at 6053 of 6054 s, %d s after its show ended (%s UTC): moving on'
        % (BID, stuck + reh.STALL_S - E, utc(E)),
        'replay end: broadcast %s: playing broadcast %s' % (BID, NEXT),
        'replay end: broadcast %s: started' % NEXT,
        'replay end: watching broadcast %s on channel %s, show ends %s UTC' % (NEXT, CID, utc(E + 3600))]
    assert not [m for m in rig.player.logged if '://' in m[0]]
    assert rig.warnings() == [] and rig.errors() == []


def test_a_finished_shows_replay_is_left_to_kodi_and_kodis_next_show_is_watched():
    """A recording ends by itself and Kodi opens the next show (the one on air): the add-on never
    acts on the recording, and watches the show Kodi landed on."""
    rig = Rig()
    rig.restart(PREV)
    rig.tick(10)
    assert rig.lines() == ['replay end: broadcast %s is not a LiveTV show on air: left to Kodi' % PREV]
    assert rig.player._replayEndFacts is None
    rig.kodi.seek(rig.kodi.times()[1] - 30)
    rig.tick(60)
    assert len(rig.kodi_autoplays) == 1 and rig.opens() == [] and rig.player.moves == []
    assert rig.kodi.playing['bid'] == BID and rig.player._replayEndFacts['broadcastid'] == BID
    assert rig.lines()[-1] == 'replay end: watching broadcast %s on channel %s, show ends %s UTC' % (BID, CID, utc(E))


def test_the_live_edge_while_the_window_grows_never_moves(rig):
    to_the_live_edge(rig)
    rig.tick_until(E + TAIL - 1)
    assert rig.opens() == [] and rig.player.moves == []
    assert not [m for m in rig.lines() if 'still' in m]


def test_a_freeze_at_the_live_edge_moves_on_twenty_seconds_after_the_window_closed(rig):
    stuck = to_the_live_edge(rig)
    rig.tick_until(stuck + 60)
    assert rig.opens() == [broadcast(NEXT)] and rig.open_times() == [stuck + reh.STALL_S]


def test_a_stall_during_the_show_waits_for_the_guide_end_and_a_minute():
    """imports.70 review (break T15): the tick passes the show's END to stall_step. A viewer at the
    live edge of a show still on air whose stream stands still mid-show (a network drop, the
    provider's manifest stalling): not 'at its end' before the guide end + 60 s, then it moves on
    at once. Every other freeze here comes after that time, so only this one tells end from start."""
    rig = Rig(t=E - 400)
    rig.restart()
    rig.tick(8)                                               # the lookup arms it
    assert rig.player._replayEndFacts['end'] == E
    rig.kodi.seek(rig.kodi.times()[1] - 1)                    # the live edge
    rig.kodi.playing['w1'] = E - 300                          # the window stops growing mid-show
    rig.tick_until(E + reh.END_GRACE_S - 1)
    assert rig.opens() == [] and rig.player.moves == []
    rig.tick_until(E + reh.END_GRACE_S + 30)
    assert rig.open_times() == [E + reh.END_GRACE_S] and rig.opens() == [broadcast(NEXT)]


def test_paused_at_the_end_moves_on_twenty_seconds_after_the_resume(rig):
    stuck = to_the_live_edge(rig)
    rig.tick_until(stuck + 5)
    rig.kodi.pause()
    rig.tick(300)
    assert rig.opens() == []
    rig.kodi.resume()
    resumed = rig.t
    rig.tick(40)
    assert rig.open_times() == [resumed + reh.STALL_S] and rig.opens() == [broadcast(NEXT)]


def test_in_the_guide_it_waits_then_moves_on_once_back_in_full_screen(rig):
    stuck = to_the_live_edge(rig)
    rig.kodi.window = False                                   # the TV guide, a menu or the OSD
    rig.tick_until(stuck + 300)
    assert rig.opens() == [] and rig.player.moves == []
    waiting = 'replay end: broadcast %s stands still at its end; waiting for full-screen video and no key press' % BID
    assert rig.logged_at(waiting) == [stuck + reh.STALL_S]
    rig.kodi.window = True
    rig.kodi.press_key()                                      # back to the video with a key
    back = rig.t
    rig.tick(30)
    assert rig.open_times() == [back + reh.QUIET_S] and rig.opens() == [broadcast(NEXT)]


def test_with_kodis_flag_off_it_stops_like_a_natural_end(rig):
    """'Play next programme automatically' was off when the replay started, or the guide menu's 'Play
    only this programme' opened it (Kodi sets no flag either way): Kodi would stop. The line names
    both, since the add-on only sees the flag."""
    stuck = to_the_live_edge(rig, flag=False)
    rig.tick_until(stuck + 60)
    assert rig.opens() == [] and rig.stops == [stuck + reh.STALL_S] and rig.kodi.playing is None
    assert rig.lines()[-1] == (
        'replay end: broadcast %s: Kodi would stop at its end (no epg_playlist_item: "Play next programme automatically"'
        ' was off when it started, or "Play only this programme" opened it): stopping' % BID)
    assert 'GetBroadcasts' not in [c[0] for c in rig.player.jsonRPC.calls]


def _last_entry(guide):
    return [s for s in guide if s[0] not in (NEXT, LATER)]


def _end_moved(guide):
    return [(b, c, st, en + 60 if b == BID else en, p) for b, c, st, en, p in guide]


@pytest.mark.parametrize('change', [_last_entry, _end_moved], ids=['last-entry', 'guide-changed'])
def test_without_a_matching_next_entry_it_plays_the_live_channel(rig, change):
    """Kodi's rule: the frozen show is the guide's last entry, or the guide changed its times since
    the lookup: the live channel."""
    stuck = to_the_live_edge(rig)
    rig.kodi.guide = change(rig.kodi.guide)
    rig.tick_until(stuck + 40)
    assert rig.opens() == [channel(CID)] and rig.kodi.playing['kind'] == 'live'
    assert rig.lines()[-2:] == ['replay end: broadcast %s: playing channel %s' % (BID, CID),
                                'replay end: channel %s: started' % CID]


def test_a_next_entry_that_has_not_started_means_live():
    rig = Rig(guide=shows(next_start=E + TAIL + 3000), t=E - 100)
    stuck = to_the_live_edge(rig)
    rig.tick_until(stuck + 40)
    assert rig.opens() == [channel(CID)]


def test_a_next_entry_that_started_after_a_gap_is_played():
    """imports.70 review (break T12): 'has the next entry started' is judged at the move, not at the
    frozen show's end. The guide has a 5-minute gap after it; by the move the next entry has started,
    so Kodi's rule plays it, not the live channel."""
    rig = Rig(guide=shows(next_start=E + 300), t=E - 100)
    stuck = to_the_live_edge(rig)
    rig.tick_until(stuck + 40)
    assert rig.opens() == [broadcast(NEXT)] and rig.kodi.playing['bid'] == NEXT


@pytest.mark.parametrize('behaviour, outcome, live_after', [
    ('refuse', None, 0.0),                       # Player.Open says no: live at once
    ('ignore', 'refused', rh.WATCH_GRACE),       # it said OK but nothing changed for 10 s
    ('error', 'retune', rh.ERROR_SETTLE),        # it failed to open with a playback error
])
def test_when_the_next_show_wont_open_it_tries_the_live_channel(rig, behaviour, outcome, live_after):
    stuck = to_the_live_edge(rig)
    rig.kodi.behave[('broadcast', NEXT)] = behaviour
    rig.tick_until(stuck + 60)
    assert rig.opens() == [broadcast(NEXT), channel(CID)] and rig.kodi.playing['kind'] == 'live'
    first, second = rig.open_times()
    assert first == stuck + reh.STALL_S and second == first + live_after
    if outcome: assert 'replay end: broadcast %s: %s' % (NEXT, outcome) in rig.lines()
    else:       assert 'replay end: Player.Open refused broadcast %s' % NEXT in rig.lines()
    assert rig.stops == [] and rig.lines()[-1] == 'replay end: channel %s: started' % CID


def test_a_next_show_failing_during_player_opens_pause_still_falls_back_to_live(rig):
    """imports.70 review: playerOpen waits RPC_Delay (1 s) after Kodi's reply, and Kodi may already
    fail the next show meanwhile. The error still counts (the counters are read before Player.Open),
    so live is tried, as when the error comes later."""
    stuck = to_the_live_edge(rig)
    rig.kodi.behave[('broadcast', NEXT)] = 'error'
    rig.kodi.rpc_delay = 1.0
    rig.tick_until(stuck + 60)
    assert rig.opens() == [broadcast(NEXT), channel(CID)] and rig.kodi.playing['kind'] == 'live'
    first, second = rig.open_times()
    assert second == first + 1.0 + rh.ERROR_SETTLE                  # the error settled 2 s after the reply
    assert 'replay end: broadcast %s: retune' % NEXT in rig.lines() and rig.stops == []
    assert rig.lines()[-1] == 'replay end: channel %s: started' % CID


def test_a_key_pressed_during_player_opens_pause_counts_as_the_viewer_taking_over(rig):
    """The key check starts before Player.Open too: a key pressed while it ran, with nothing playing
    after a failed next show, leaves it to the viewer (no live channel forced on)."""
    stuck = to_the_live_edge(rig)
    rig.kodi.behave[('broadcast', NEXT)] = 'gone'
    rig.kodi.rpc_delay = 1.0
    rig.hooks['Open'] = rig.kodi.press_key
    rig.tick_until(stuck + 60)
    assert rig.opens() == [broadcast(NEXT)] and rig.stops == []
    assert rig.lines()[-1] == 'replay end: broadcast %s: gave_up' % NEXT


@pytest.mark.parametrize('behaviour', ['refuse', 'ignore'])
def test_when_live_wont_open_either_it_stops_the_frozen_replay(rig, behaviour):
    stuck = to_the_live_edge(rig)
    rig.kodi.behave[('broadcast', NEXT)] = behaviour
    rig.kodi.behave[('channel', CID)] = behaviour
    rig.tick_until(stuck + 60)
    assert rig.opens() == [broadcast(NEXT), channel(CID)] and len(rig.stops) == 1 and rig.kodi.playing is None
    assert rig.lines()[-1] == 'replay end: broadcast %s: the live channel did not open either: stopping' % BID
    rig.tick(300)
    assert len(rig.opens()) == 2 and len(rig.stops) == 1


@pytest.mark.parametrize('away', ['window', 'key'])
def test_the_last_resort_stop_waits_for_the_viewer_like_the_flag_off_stop(rig, away):
    """imports.70 review (break T19): the next show refused, live ignored, and the viewer opens the
    guide (or presses a key) while the add-on watches live: nothing is stopped under them. The second
    attempt a minute later, back in full-screen video, tries again and then stops."""
    stuck = to_the_live_edge(rig)
    rig.kodi.behave[('broadcast', NEXT)] = 'refuse'
    rig.kodi.behave[('channel', CID)] = 'ignore'

    def viewer_during_the_live_watch():
        if len(rig.opens()) == 2:
            if away == 'window': rig.kodi.window = False
            else: rig.kodi.press_key()
            rig.hooks['GetItem'] = None
    rig.hooks['GetItem'] = viewer_during_the_live_watch
    rig.tick_until(stuck + reh.STALL_S + 15)
    assert rig.opens() == [broadcast(NEXT), channel(CID)] and rig.stops == [] and rig.kodi.playing['bid'] == BID
    assert rig.lines()[-1] == ('replay end: broadcast %s: the live channel did not open either; not in full-screen'
                               ' video or a key was pressed: not stopping now' % BID)
    rig.kodi.window = True
    rig.tick_until(stuck + 200)
    second = stuck + reh.STALL_S + reh.RETRY_S
    assert rig.open_times() == [stuck + reh.STALL_S] * 2 + [second] * 2
    assert rig.stops == [second + rh.WATCH_GRACE] and rig.kodi.playing is None


def test_a_next_show_gone_without_an_error_is_left_alone(rig):
    """As imports.62: nothing plays and nothing failed loudly; no live channel is forced on."""
    stuck = to_the_live_edge(rig)
    rig.kodi.behave[('broadcast', NEXT)] = 'gone'
    rig.tick_until(stuck + 60)
    assert rig.opens() == [broadcast(NEXT)] and rig.stops == []
    assert rig.lines()[-1] == 'replay end: broadcast %s: failed' % NEXT


def test_after_a_failed_next_show_and_a_refused_live_channel_nothing_is_stopped(rig):
    """The error ended the frozen replay already (a new playback number): nothing else is stopped."""
    stuck = to_the_live_edge(rig)
    rig.kodi.behave[('broadcast', NEXT)] = 'error'
    rig.kodi.behave[('channel', CID)] = 'refuse'
    rig.tick_until(stuck + 60)
    assert rig.opens() == [broadcast(NEXT), channel(CID)] and rig.stops == []


def test_a_key_pressed_while_the_next_show_was_refused_leaves_live_for_later(rig):
    stuck = to_the_live_edge(rig)
    rig.kodi.behave[('broadcast', NEXT)] = 'ignore'

    def key_during_the_watch():
        if rig.opens() and rig.kodi.key_at < stuck: rig.kodi.press_key()
    rig.hooks['GetItem'] = key_during_the_watch
    rig.tick_until(stuck + reh.STALL_S)
    assert rig.opens() == [broadcast(NEXT)] and rig.stops == []
    assert rig.lines()[-1] == \
        'replay end: broadcast %s: the next show did not open; not trying the live channel now' % BID


def test_a_running_restart_blocks_the_move_and_it_tries_again_a_minute_later(rig):
    stuck = to_the_live_edge(rig)
    rig.player._restart_lock.held = True                      # a RESTART is running
    rig.tick_until(stuck + reh.STALL_S + 30)
    assert rig.opens() == [] and rig.lines()[-1] == 'replay end: a restart or an earlier move is running; not moving on now'
    rig.player._restart_lock.held = False
    rig.tick_until(stuck + 120)
    assert rig.open_times() == [stuck + reh.STALL_S + reh.RETRY_S]
    assert rig.player._restart_lock.acquires == [False, False] and rig.player._restart_lock.held is False


def test_the_lock_is_released_when_the_move_fails(rig):
    stuck = to_the_live_edge(rig)
    rig.broadcasts_fail = RuntimeError('JSON-RPC went away')
    rig.tick_until(stuck + reh.STALL_S)
    assert rig.player._restart_lock.held is False and rig.opens() == []
    assert rig.errors() == ['_moveReplayEnd, failed! JSON-RPC went away']


def test_a_new_playback_before_the_worker_runs_stops_the_move(rig):
    stuck = to_the_live_edge(rig)

    def zap(): rig.player._play_epoch += 1
    rig.before_move = zap
    rig.tick_until(stuck + reh.STALL_S)
    assert len(rig.player.moves) == 1 and rig.opens() == [] and rig.stops == []
    assert rig.lines()[-1] == 'replay end: broadcast %s no longer plays; nothing to do' % BID
    assert [c[0] for c in rig.player.jsonRPC.calls] == ['GetItem', 'GetChannels']        # the lookup's only


@pytest.mark.parametrize('during', ['GetItem', 'GetBroadcasts', reh.ACT_WINDOW, 'getIdle'])
def test_a_new_playback_during_the_moves_reads_stops_it(rig, during):
    """The playback number is checked after each read, and right before acting."""
    stuck = to_the_live_edge(rig)
    rig.tick_until(stuck + reh.STALL_S - 1)

    def zap():
        if rig.player.moves:
            rig.player._play_epoch += 1
            rig.hooks[during] = None
    rig.hooks[during] = zap
    rig.tick(1)
    assert len(rig.player.moves) == 1 and rig.opens() == [] and rig.stops == []
    if during == 'GetItem':                                    # stopped right after that read
        assert [c[0] for c in rig.player.jsonRPC.calls][2:] == ['GetItem']


def test_a_new_playback_during_the_reads_of_a_flag_off_stop_stops_nothing(rig):
    stuck = to_the_live_edge(rig, flag=False)
    rig.tick_until(stuck + reh.STALL_S - 1)

    def zap():
        if rig.player.moves:
            rig.player._play_epoch += 1
            rig.hooks['getIdle'] = None
    rig.hooks['getIdle'] = zap
    rig.tick(1)
    assert len(rig.player.moves) == 1 and rig.stops == []


def test_a_finished_show_frozen_at_its_end_after_a_watched_one_is_left_alone(rig):
    """The previous playback was watched; the next one (a finished show's recording) stands still at
    its end too: its lookup armed nothing, so the previous playback's show never counts for it."""
    to_the_live_edge(rig)
    assert rig.player._replayEndFacts['broadcastid'] == BID
    rig.kodi.play_show(PREV)                                    # a recording ...
    rig.tick(8)
    rig.kodi.playing.update(vod=False, w0=rig.t - 4000, w1=rig.t - 600)   # ... that stalls at its end
    rig.kodi.seek(rig.kodi.times()[1] - 1)
    rig.tick(300)
    assert rig.player.moves == [] and rig.opens() == []
    assert 'replay end: broadcast %s is not a LiveTV show on air: left to Kodi' % PREV in rig.lines()


def test_another_broadcast_playing_stops_the_move(rig):
    stuck = to_the_live_edge(rig)
    rig.tick_until(stuck + reh.STALL_S - 1)
    rig.item_id = PREV                                        # Kodi reports another guide entry
    rig.tick(1)
    assert len(rig.player.moves) == 1 and rig.opens() == [] and rig.stops == []


@pytest.mark.parametrize('away', ['window', 'key'])
def test_the_last_check_before_acting_waits_for_the_viewer(rig, away):
    """The viewer opens the guide (or presses a key) between the tick and the move: no move now;
    the second attempt a minute later acts."""
    stuck = to_the_live_edge(rig)

    def leave():
        if away == 'window': rig.kodi.window = False
        else: rig.kodi.press_key()
        rig.hooks['GetBroadcasts'] = None
    rig.hooks['GetBroadcasts'] = leave
    rig.tick_until(stuck + reh.STALL_S + 5)
    assert rig.opens() == [] and len(rig.player.moves) == 1
    assert rig.lines()[-1] == ('replay end: broadcast %s: not in full-screen video, a key was pressed or another video'
                               ' plays; not moving on now' % BID)
    rig.kodi.window = True
    rig.tick_until(stuck + 120)
    assert rig.open_times() == [stuck + reh.STALL_S + reh.RETRY_S]


def test_a_flag_off_stop_also_waits_for_the_viewer(rig):
    stuck = to_the_live_edge(rig, flag=False)

    def leave():
        if rig.player.moves: rig.kodi.window = False
    rig.hooks['GetItem'] = leave
    rig.tick_until(stuck + reh.STALL_S + 5)
    assert rig.stops == [] and len(rig.player.moves) == 1 and rig.kodi.playing is not None


def test_a_natural_end_before_twenty_seconds_is_left_to_kodi(rig):
    stuck = to_the_live_edge(rig)
    rig.tick_until(stuck + 10)
    rig.kodi.playing.update(vod=True, length=rig.kodi.times()[0])   # the stream ends after all
    rig.tick(60)
    assert len(rig.kodi_autoplays) == 1 and rig.opens() == [] and rig.player.moves == []


def test_a_seek_back_after_a_pass_allows_a_second_move(rig):
    stuck = to_the_live_edge(rig)
    rig.player._restart_lock.held = True                      # both attempts of the first pass blocked
    rig.tick_until(stuck + 300)
    assert len(rig.player.moves) == 2 and rig.opens() == []
    rig.player._restart_lock.held = False
    rig.kodi.seek(rig.kodi.times()[0] - 120)                  # two minutes back
    back = rig.t
    rig.tick(150)
    assert rig.open_times() == [back + 120 + reh.STALL_S] and rig.opens() == [broadcast(NEXT)]


def test_a_custom_channels_show_is_never_watched():
    guide = [(b, c, st, en, CUSTOM_PLOT if b == BID else p) for b, c, st, en, p in shows()]
    rig = Rig(guide=guide, t=E - 100)
    to_the_live_edge(rig)
    rig.tick_until(E + TAIL + 300)
    assert rig.player._replayEndFacts is None and rig.opens() == [] and rig.player.moves == []
    assert rig.lines() == ['replay end: broadcast %s is not a LiveTV show on air: left to Kodi' % BID]


def test_kodi_closing_ends_the_watch_without_falling_back(rig):
    stuck = to_the_live_edge(rig)
    rig.kodi.behave[('broadcast', NEXT)] = 'ignore'
    rig.closing_at = stuck + reh.STALL_S + 3
    rig.tick_until(stuck + reh.STALL_S)
    assert rig.opens() == [broadcast(NEXT)] and rig.stops == []
    assert rig.lines()[-1] == 'replay end: broadcast %s: playing broadcast %s' % (BID, NEXT)
    assert rig.t == stuck + reh.STALL_S + 3


def test_kodi_closing_before_the_move_does_nothing(rig):
    stuck = to_the_live_edge(rig)
    rig.closing_at = stuck
    rig.tick_until(stuck + reh.STALL_S)
    assert len(rig.player.moves) == 1 and rig.opens() == [] and rig.stops == []
    assert [c[0] for c in rig.player.jsonRPC.calls] == ['GetItem', 'GetChannels']


def test_a_failing_tick_logs_one_error_per_playback(rig):
    to_the_live_edge(rig)

    def broken(): raise RuntimeError('Kodi went away')
    rig.hooks['getIdle'] = broken
    rig.tick(100)
    assert rig.errors() == ['_chkReplayEnd, failed: Kodi went away'] and rig.warnings() == []
    assert rig.player._replayEndTrack == {'epoch': rig.player._play_epoch, 'failed': True}
    rig.kodi.play_show(NEXT)                                  # a new playback: watched again
    rig.tick(100)
    assert rig.errors() == ['_chkReplayEnd, failed: Kodi went away'] * 2
    assert rig.opens() == [] and rig.player.moves == []


def test_the_watch_starts_when_player_open_returns(rig):
    """A slow Player.Open (RPC_Delay, a busy JSON-RPC lock): 'refused' only 10 s after it returned."""
    stuck = to_the_live_edge(rig)
    rig.kodi.behave[('broadcast', NEXT)] = 'ignore'
    rig.kodi.open_delay = 12.0
    rig.tick_until(stuck + reh.STALL_S)
    refused = rig.logged_at('replay end: broadcast %s: refused' % NEXT)
    assert refused == [stuck + reh.STALL_S + 12.0 + rh.WATCH_GRACE]


def test_one_lookup_per_playback_five_seconds_after_it_started(rig):
    t_open = rig.restart()
    rig.tick(40)
    assert rig.player.lookups == [(t_open + 1 + 5, 2)]                   # 5 s after the first tick saw it
    assert rig.player.jsonRPC.calls == [('GetItem',), ('GetChannels',)]
    assert rig.player._replayEndFacts == {'epoch': 2, 'channelid': CID, 'broadcastid': BID, 'start': S, 'end': E,
                                          'endtime': utc(E)}


def test_a_clock_going_back_never_holds_the_lookup(rig):
    """A clock change (NTP stepping the clock back) right after the replay started: the lookup still
    runs at the next tick instead of an hour later."""
    t_open = rig.restart()
    rig.tick(2)
    rig.t -= 3600
    rig.tick(1)
    assert rig.player.lookups == [(t_open + 3 - 3600, 2)] and rig.player._replayEndFacts['broadcastid'] == BID


def test_a_live_channel_that_fails_to_open_is_not_tuned_again(rig):
    """After the next show was refused, live fails with an error: nothing plays, nothing is stopped,
    and the outcome is 'failed' (a live channel is never 'tuned again')."""
    stuck = to_the_live_edge(rig)
    rig.kodi.behave[('broadcast', NEXT)] = 'refuse'
    rig.kodi.behave[('channel', CID)] = 'error'
    rig.tick_until(stuck + 60)
    assert rig.opens() == [broadcast(NEXT), channel(CID)] and rig.stops == [] and rig.kodi.playing is None
    assert rig.lines()[-1] == 'replay end: channel %s: failed' % CID


@pytest.mark.parametrize('kind', ['broadcast', 'channel'])
def test_another_channel_playing_after_an_open_is_left_alone(rig, kind):
    """The viewer zapped while Kodi opened it: 'other', and nothing more is done."""
    stuck = to_the_live_edge(rig)
    if kind == 'channel': rig.kodi.behave[('broadcast', NEXT)] = 'refuse'
    rig.kodi.behave[(kind, NEXT if kind == 'broadcast' else CID)] = 'other'
    rig.tick_until(stuck + 60)
    assert rig.kodi.playing['cid'] == OTHER_CID and rig.stops == []
    assert rig.lines()[-1] == 'replay end: %s %s: other' % (kind, NEXT if kind == 'broadcast' else CID)
    assert len(rig.opens()) == (1 if kind == 'broadcast' else 2)


def test_the_tick_reads_no_jsonrpc_until_the_move(rig):
    stuck = to_the_live_edge(rig)
    rig.tick_until(stuck + reh.STALL_S - 1)
    assert [name for name, _ in rig.rpc_times] == ['GetItem', 'GetChannels']
    rig.tick(1)
    assert [name for name, _ in rig.rpc_times][2:] == ['GetItem', 'GetBroadcasts', 'Open', 'GetItem']


def test_live_channels_and_nothing_playing_are_never_looked_up(rig):
    rig.kodi.play_live(CID)
    rig.tick(300)
    rig.kodi.gone()
    rig.tick(30)
    assert rig.player.lookups == [] and rig.player._replayEndTrack is None and rig.player.jsonRPC.calls == []
    assert set(rig.xbmc.reads) == {'Pvr.IsPlayingEpgTag'}


def test_the_track_is_cleared_when_the_replay_ends(rig):
    to_the_live_edge(rig)
    assert rig.player._replayEndTrack['epoch'] == rig.player._play_epoch
    rig.kodi.play_live(CID)
    rig.tick(1)
    assert rig.player._replayEndTrack is None


def test_a_new_playback_during_the_lookup_publishes_nothing(rig):
    rig.restart()

    def zap():
        rig.player._play_epoch += 2
        rig.hooks['GetChannels'] = None
    rig.hooks['GetChannels'] = zap
    rig.tick(8)
    assert rig.player._replayEndFacts is None and rig.lines() == []


def test_a_new_playback_before_the_lookups_channel_read_publishes_nothing(rig):
    rig.restart()

    def zap():
        rig.player._play_epoch += 2
        rig.hooks['GetItem'] = None
    rig.hooks['GetItem'] = zap
    rig.tick(8)
    assert rig.player._replayEndFacts is None and rig.lines() == []
    assert rig.player.jsonRPC.calls == [('GetItem',)]


def test_a_new_playback_during_the_ticks_reads_is_not_counted(rig):
    """The sample of the tick that would have fired came from the next playback: nothing stored."""
    stuck = to_the_live_edge(rig)
    rig.tick_until(stuck + reh.STALL_S - 1)
    track = rig.player._replayEndTrack

    def zap():
        rig.player._play_epoch += 2
        rig.hooks[reh.ACT_WINDOW] = None
    rig.hooks[reh.ACT_WINDOW] = zap
    rig.tick(1)
    assert rig.player.moves == [] and rig.player._replayEndTrack is track


def test_a_lookup_that_finds_no_guide_entry_watches_nothing(rig):
    rig.restart()
    rig.item_id = -1                                          # Kodi: no id
    rig.tick(8)
    assert rig.lines() == ['replay end: no guide entry plays; nothing to watch']
    assert rig.player.jsonRPC.calls == [('GetItem',)]


def test_a_failing_lookup_is_logged_and_watches_nothing(rig):
    rig.restart()

    def broken(): raise RuntimeError('JSON-RPC went away')
    rig.hooks['GetChannels'] = broken
    rig.tick(8)
    rig.hooks['GetChannels'] = None
    rig.kodi.seek(rig.kodi.times()[1] - 1)
    rig.tick_until(E + TAIL + 300)
    assert rig.errors() == ['_lookupReplayEnd, failed: JSON-RPC went away'] and rig.warnings() == []
    assert rig.player._replayEndFacts is None and rig.opens() == [] and len(rig.player.lookups) == 1


def test_the_autoplay_flag_is_read_from_the_playing_item(rig):
    rig.restart()
    assert rig.player._replayEndAutoplay() == 'true'
    rig.kodi.playing['props'] = {}
    assert rig.player._replayEndAutoplay() == ''
    rig.kodi.playing = None
    assert rig.player._replayEndAutoplay() == ''


def test_the_sample_reads(rig):
    t_open = rig.restart()
    rig.kodi.press_key()
    rig.xbmc.reads[:] = []
    rig.t += 3
    assert rig.player._replayEndSample() == {'pos': 3.0, 'total': float(t_open + 3 - (S - LEAD)), 'busy': False,
                                             'window': True, 'idle': 3}
    assert rig.xbmc.reads == [reh.BUSY, reh.ACT_WINDOW]
    rig.kodi.pause()
    assert rig.player._replayEndSample()['busy'] is True
    rig.kodi.playing = None
    sample = rig.player._replayEndSample()
    assert (sample['pos'], sample['total']) == (None, None)


# === structure: where the calls sit, who writes what ==============================================

def test_services_imports_replayend_helpers():
    assert any(isinstance(n, ast.Import) and [a.name for a in n.names] == ['replayend_helpers'] for n in S_TREE.body)


def test_state_is_per_instance_and_set_last_in_init():
    body = [ast.unparse(s) for s in _method('Player', '__init__').body]
    assert body[-2:] == ['self._replayEndFacts = None', 'self._replayEndTrack = None']
    class_level = {t.id for n in _class('Player').body if isinstance(n, ast.Assign) for t in n.targets
                   if isinstance(t, ast.Name)}
    assert not class_level & {'_replayEndFacts', '_replayEndTrack'}


def test_the_tick_runs_right_after_the_transition_supervisor():
    chk = _method('Monitor', '_chkIdle')
    ifs = [n for n in ast.walk(chk) if isinstance(n, ast.If) and ast.unparse(n.test) == 'self.player.isPlayingPseudoTV()']
    assert len(ifs) == 1
    other = [ast.unparse(s) for s in ifs[0].orelse]
    at = other.index('self.player._chkTransition()')
    assert other[at + 1] == 'self.player._chkReplayEnd()'
    assert 'self.player._chkReplayEnd()' not in [ast.unparse(s) for s in ifs[0].body]
    assert len(_calls(S_TREE, '_chkReplayEnd')) == 1


def test_decorators():
    for name in NEW_METHODS:
        expected = ['threadit'] if name in ('_lookupReplayEnd', '_moveReplayEnd') else []
        assert [ast.unparse(d) for d in _method('Player', name).decorator_list] == expected, name


def test_the_move_takes_the_restart_lock_without_waiting():
    node = _method('Player', '_moveReplayEnd')
    assert ast.unparse(node.body[0]).startswith('if not self._restart_lock.acquire(False):')
    tries = [s for s in node.body if isinstance(s, ast.Try)]
    assert len(tries) == 1 and [ast.unparse(s) for s in tries[0].finalbody] == ['self._restart_lock.release()']
    assert [ast.unparse(s) for s in tries[0].body] == ['self._runReplayEnd(facts, epoch)']
    mine = [c for c in _calls(S_TREE, 'acquire') if ast.unparse(c.func) == 'self._restart_lock.acquire']
    assert len(mine) == 2 and all([ast.unparse(a) for a in c.args] == ['False'] and not c.keywords for c in mine)


def test_single_writers():
    owner  = _owner_map()
    stores = [n for n in ast.walk(S_TREE) if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Store)]
    assert sorted({owner(n) for n in stores if n.attr == '_replayEndFacts'}) == \
        [('Player', '__init__'), ('Player', '_lookupReplayEnd')]
    assert sorted({owner(n) for n in stores if n.attr == '_replayEndTrack'}) == \
        [('Player', '__init__'), ('Player', '_chkReplayEnd')]
    for attr in ('_replayEndFacts', '_replayEndTrack'):           # replaced whole, never changed in place
        refs = [n for n in ast.walk(S_TREE) if isinstance(n, ast.Attribute) and n.attr == attr]
        in_place = [n for n in ast.walk(S_TREE)
                    if (isinstance(n, ast.Subscript) and n.value in refs and not isinstance(n.ctx, ast.Load))
                    or (isinstance(n, ast.Attribute) and n.value in refs)]
        assert in_place == [], attr


def test_the_tick_makes_no_jsonrpc_call_and_no_logged_read():
    """About once a second: Kodi's own reads, directly (BUILTIN.getInfoBool / getInfoLabel log
    every call at debug level); the JSON-RPC reads run on the two workers."""
    for name in ('_chkReplayEnd', '_replayEndSample'):
        text = ast.unparse(_method('Player', name))
        assert 'jsonRPC' not in text and 'sendJSON' not in text, name
        assert 'getInfoBool' not in text and 'getInfoLabel' not in text and 'isBusyDialog' not in text, name
    sample = _method('Player', '_replayEndSample')
    assert sorted(ast.unparse(c.func) for c in ast.walk(sample) if isinstance(c, ast.Call)) == \
        ['BUILTIN.getIdle', 'self.getTime', 'self.getTotalTime', 'xbmc.getCondVisibility', 'xbmc.getCondVisibility']


def test_the_new_code_never_reads_the_playing_file():
    """Movistar+ stream links carry tokens: no file or path is read, so none can be logged."""
    for name in NEW_METHODS:
        text = ast.unparse(_method('Player', name))
        for needle in ('getPlayingFile', 'getPlayerFile', '_liveSubsFile', 'Filenameandpath', 'getPath', "'file'",
                       'dynpath', 'getPlayerItem(', 'getplayingItem', 'getInfoLabel'):
            assert needle not in text, (name, needle)


def test_the_new_code_writes_no_setting_and_shows_nothing():
    for name in NEW_METHODS:
        text = ast.unparse(_method('Player', name))
        for needle in ('setSetting', 'SETTINGS', 'DIALOG', 'LANGUAGE', '_restartNote', 'executebuiltin', 'time.time'):
            assert needle not in text, (name, needle)


def test_the_tick_and_the_lookup_are_wrapped_whole():
    tick = _method('Player', '_chkReplayEnd').body
    assert [type(s).__name__ for s in tick] == ['Assign', 'Try'] and ast.unparse(tick[0]) == 'epoch = self._play_epoch'
    assert ast.unparse(tick[1].handlers[0].body[0]) == "self._replayEndTrack = {'epoch': epoch, 'failed': True}"
    lookup = _method('Player', '_lookupReplayEnd').body
    assert len(lookup) == 1 and isinstance(lookup[0], ast.Try)
    for name in NEW_METHODS:
        for handler in [n for n in ast.walk(_method('Player', name)) if isinstance(n, ast.ExceptHandler)]:
            assert handler.type is not None and ast.unparse(handler.type) == 'Exception', name


def test_the_open_is_imports62s_watch_timed_from_when_player_open_returned():
    """The clock starts when Player.Open returned; the key and event counters are read before it
    (imports.70 review: playerOpen's RPC_Delay pause comes after Kodi's reply, and Kodi may act
    meanwhile)."""
    node  = _method('Player', '_replayEndOpen')
    text  = ast.unparse(node)
    assert 'restart_helpers.watch_outcome(' in text and 'self.service._shutdown(restart_helpers.WATCH_POLL)' in text
    lines   = [ast.unparse(s) for s in node.body]
    opened  = [i for i, line in enumerate(lines) if 'self.jsonRPC.playerOpen(' in line]
    counted = [i for i, line in enumerate(lines) if line.startswith('idle0, stops, errors = ')]
    timed   = [i for i, line in enumerate(lines) if line.startswith('t0 = ')]
    assert len(opened) == 1 and len(counted) == 1 and len(timed) == 1 and counted[0] < opened[0] < timed[0]
    assert lines[counted[0]] == 'idle0, stops, errors = (BUILTIN.getIdle(), self._play_stops, self._play_errors)'
    assert lines[timed[0]] == 't0 = clock_helpers.now()'


def test_the_new_methods_are_python38_and_free_of_bitor():
    from test_imports63_clock import py39_api_leaks
    for name in ('services.py', 'jsonrpc.py'):
        with open(os.path.join(ADDON, 'resources', 'lib', name), encoding='utf-8') as fh:
            src = fh.read()
        ast.parse(src, feature_version=(3, 8))
        assert py39_api_leaks(src) == [], name
    for name in NEW_METHODS:
        node = _method('Player', name)
        assert [n for n in ast.walk(node) if isinstance(n, (ast.BinOp, ast.AugAssign)) and isinstance(n.op, ast.BitOr)] == []
        assert 'imports.70' in ast.get_source_segment(SERVICES, node), name


# === jsonrpc.getPVRBroadcastTimes =================================================================

@pytest.fixture
def rpc(monkeypatch):
    from jsonrpc import JSONRPC
    sent, replies = [], []
    client = JSONRPC(service=types.SimpleNamespace())

    def send(param, delay=True):
        sent.append((json.loads(json.dumps(param)), delay))
        return replies.pop(0) if replies else {}
    monkeypatch.setattr(client, 'sendJSON', send)
    return types.SimpleNamespace(client=client, sent=sent, replies=replies)


def test_getPVRBroadcastTimes_sends_exactly_the_planned_request(rpc):
    """Times only (Kodi adds each row's broadcastid and label): never 'file' (signed links), plot or
    artwork, and no RPC_Delay pause (its caller acts on it at once)."""
    rows = [{'broadcastid': BID, 'label': 'x', 'starttime': utc(S), 'endtime': utc(E)}]
    rpc.replies.append({'id': 1, 'jsonrpc': '2.0', 'result': {'broadcasts': rows, 'limits': {'start': 0, 'end': 1, 'total': 1}}})
    assert rpc.client.getPVRBroadcastTimes(CID) == rows
    assert rpc.sent == [({'method': 'PVR.GetBroadcasts', 'params': {'channelid': CID, 'properties': ['starttime', 'endtime']}},
                         False)]


@pytest.mark.parametrize('reply', [{}, None, {'result': None}, {'result': {'error': {'code': -32602}}},
                                   {'result': {'broadcasts': None}}])
def test_getPVRBroadcastTimes_survives_errors(rpc, reply):
    rpc.replies.append(reply)
    assert rpc.client.getPVRBroadcastTimes(CID) == []


# === docs ==========================================================================================

def test_the_addon_news_fits_kodis_limit():
    news = ET.parse(os.path.join(ADDON, 'addon.xml')).getroot().find('.//news').text
    assert len(news) <= 1500
    latest = news.split('v0.8.0+imports.69')[0]
    assert latest.startswith('v0.8.0+imports.70') and 'freezes at its end' in latest
