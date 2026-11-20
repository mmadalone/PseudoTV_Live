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
test_imports67_livetv_subtitles.py, whose ast helpers this reuses) the real
methods are compiled out of the source with their decorators dropped: the two
workers run inline, in the tick that starts them. Kodi is a model on the
test's clock: Movistar+'s growing window (it opens 174 s before the guide start
and closes 720 s after the guide end, as measured), the player's position and
length, pause, seeks, the guide, Player.Open (switching in place, as
VideoPlayer does, or refused / failing in the ways watch_outcome tells apart),
a natural end with Kodi's own next show, and the time passing while the
add-on waits. The structure (where the calls sit, who writes what) is checked
with ast.

The repair after the TV test of 2026-11-19 (the watcher never acted: its move
began with service._shutdown(0), which Kodi turns into a wait without end, and
it stood there holding the RESTART lock): Player.service is kodi_faithful's
FaithfulService, the REAL Service._shutdown / _aborted over a monitor with
Kodi's rules, so a wait Kodi would never end fails the test (ForeverWait, which
the add-on's `except Exception` can't swallow). The first build's fake
_shutdown(wait=0) returned at once, and 78 tests passed over the bug. After
every test (the autouse check below): no such wait happened, and the RESTART
lock is free unless the test itself holds it. Every call into the rig counts
against a loop that never waits (kodi_faithful.Standstill): such a loop fails
within SPIN_CAP calls instead of recording calls until the memory is full.

The repair, step 2 (the user's decisions of 2026-11-19):
  - the new rule: due 20 s after the stream froze, with no wait for the guide's end time; the move
    plays the live channel while the guide lists the show as on air, else the next entry (its
    clock read after the guide read). The TV test's Mega replay runs here on a 'declared' stream
    model that reproduces the recording (a fixed declared length, the position about 38 s behind
    the clock, the length corrected near the end): E1-E10;
  - the always-on lines: every 'replay end:' line goes through Player.notice -> logger.notice, both
    real, as do Player.log -> logger.log, over Kodi's log levels (KODI_LOG) and the box's settings
    (Debug_Enable off): `rig.kodi_log` is what kodi.log would hold. The autouse check below also
    holds every always-on line to its exact wording (only digits and UTC times vary), once each,
    and at most 8 per attempt: A1-A12;
  - Kodi's own timing (Kodi.lag): its cached PVR flags, the player's times and the playback
    callbacks can keep the old values for a moment after a switch (F1-F3), and a getter called
    while nothing plays writes Kodi's own ERROR line (Player.cpp, PythonSwig.cpp.template).

After the review of the repair (the fix stage): the move's last check before acting reads what
plays (a zap without a key press whose callbacks are late is left alone); Kodi 21's failed start
('failed_start': the target shows as playing, then a stop with no key); the tick running during
the move's waits, as Kodi's idle thread does (interleave); and the tests the review found missing
(a freeze long before the guide end, the target line before Player.Open, the type and flag checks
of 'started', the re-check before the last-resort stop).
"""
import ast
import calendar
import json
import math
import os
import re
import threading
import time
import traceback
import types
import xml.etree.ElementTree as ET

import pytest

import kodi_faithful
import replayend_helpers as reh
import restart_helpers as rh
from kodi_faithful import (KODI_LOG, FaithfulMonitor, FaithfulService, ForeverWait, Standstill, compile_function,
                           compile_method)
from test_imports67_livetv_subtitles import SERVICES, S_TREE, _calls, _class, _method, _owner_map

HERE  = os.path.dirname(os.path.abspath(__file__))
ADDON = os.path.dirname(HERE)

NEW_METHODS = ('_chkReplayEnd', '_replayEndSample', '_lookupReplayEnd', '_moveReplayEnd', '_runReplayEnd',
               '_replayEndOpen', '_replayEndAutoplay')
# imports.70 (the repair): Player.log and Player.notice run for real too, over the real logger.
LOG_METHODS = ('log', 'notice')

# What kodi.log shows before an add-on line (logger.log / logger.notice: ADDON_ID-ADDON_VERSION-).
ADDON_ID      = 'plugin.video.pseudotv.imports'
ADDON_VERSION = ET.parse(os.path.join(ADDON, 'addon.xml')).getroot().get('version')
PREFIX        = '%s-%s-' % (ADDON_ID, ADDON_VERSION)


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


# The 2026-11-19 Mega case (the TV test: README and the 1 Hz recording monitor.log in
# ~/.claude/plans/never-synced/replay-end-build/tvtest-2026-11-19/), with the rig's ids (Mega was
# channel 424, broadcast 883321, the next entry 883322). Guide 13:55-17:00 UTC (14:55-18:00 local),
# the next entry 17:00-18:45. Player.Open at 16:51:58 UTC; the stream's window opened at 15:50:00
# and declared 4140 s from the start (69 minutes: one episode of the 3 h 05 entry); the position ran
# 37.7 s behind the clock (the seek to its end was clamped there); at window + 4156 s the length
# became 4134 s, and from 16:59:32 the player stood at 4133.5 of 4134.0 s (0.5 s before the end).
def T(hhmmss, day=19):
    """An instant of 2026-11-<day> given as UTC 'HH:MM:SS' (local time on the box is UTC + 1)."""
    h, m, s = (float(x) for x in hhmmss.split(':'))
    return calendar.timegm((2026, 11, day, 0, 0, 0)) + h * 3600 + m * 60 + s


MEGA_START, MEGA_END = T('13:55:00'), T('17:00:00')
MEGA_OPEN = T('16:51:58')
MEGA_SEEK = T('16:52:24')                                     # the operator's seek to the end (clamped)
MEGA_STREAM = {'w0': T('15:50:00'), 'declared': 4140.0, 'final': 4134.0, 'final_at': T('15:50:00') + 4156,
               'lag': 37.7, 'stop': 0.5}
MEGA_FROZEN_AT = T('16:59:31')                                # the first sample within 0.25 s of 4133.5
MEGA_DUE = MEGA_FROZEN_AT + reh.STALL_S                       # 16:59:51: 9 s before the guide end


def mega_shows(end=MEGA_END, next_start=None):
    """The Mega guide: the frozen entry ends at `end`, the next one starts at `next_start` (its end
    by default) and runs 1 h 45."""
    next_start = end if next_start is None else next_start
    return [(PREV, CID, MEGA_START - 3600, MEGA_START, 'The previous show'),
            (BID, CID, MEGA_START, end, 'Vida bajo cero'),
            (NEXT, CID, next_start, next_start + 6300, 'The next show'),
            (LATER, CID, next_start + 6300, next_start + 9900, 'A later show'),
            (OTHER, OTHER_CID, MEGA_START - 1800, end + 9000, 'Another channel')]


# Player._restart_lock: kodi_faithful.RestartLock (a blocking acquire while it is held raises ForeverWait;
# the imports.62 rig uses it too).
Lock = kodi_faithful.RestartLock


class Kodi(object):
    """What Kodi plays and shows, on the rig's clock. `playing` is None or {'kind': 'replay' |
    'live', 'bid', 'cid', 't_open', 'offset', 'paused_at', 'vod', 'w0', 'w1', 'length', 'stream',
    'props'}. `behave[(kind, id)]` is what Player.Open does: 'ok' (switches in place), 'refuse'
    (InvalidParams: Player.Open says no), 'ignore' (says OK, nothing happens), 'error' (the open fails
    with a playback error: an onPlayBackError, which Kodi 21 never sends to Python; the add-on's
    handling of it is kept and tested, FORK_NOTES Limits), 'failed_start' (Kodi 21's failed start:
    the target shows as playing for `fail_after` s, then a stop with no key), 'gone' (nothing plays,
    no error), 'other' (another channel plays: the viewer zapped). What Player.Open starts happens the
    next time the add-on waits (Kodi posts it), or already during playerOpen's own RPC_Delay pause
    after Kodi's reply when `rpc_delay` is set.
    imports.70 (the repair): `streams[bid]` plays a show on air as a declared stream (MEGA_STREAM's
    keys) instead of the growing window; `lag` holds Kodi's own delays after a switch of what plays
    (all 0 by default): 'pvr' (the cached Pvr.IsPlayingEpgTag / Pvr.IsPlayingTv flags), 'times'
    (getTime / getTotalTime keep the old item's values) and 'epoch' (the playback callbacks, and with
    them the playback number, come that much later); `timed` holds changes due at a time."""

    def __init__(self, rig, guide):
        self.rig, self.guide = rig, list(guide)
        self.playing, self.pending, self.behave = None, [], {}
        self.window, self.key_at = True, rig.t - 9999          # no key pressed for a long time
        self.autoplay, self.open_delay, self.rpc_delay = True, 0.0, 0.0
        self.fail_after = 0.3                                 # s a 'failed_start' target shows as playing
        self.streams, self.lag, self.switch, self.timed = {}, {'pvr': 0.0, 'times': 0.0, 'epoch': 0.0}, None, []

    def show(self, bid):
        return [s for s in self.guide if s[0] == bid][0]

    def on_now(self, cid, t):
        now = [s for s in self.guide if s[1] == cid and s[2] <= t < s[3]]
        return now[0] if now else None

    def at(self, t, change):
        """`change` happens in Kodi at the rig time `t` (inside the add-on's wait that reaches it)."""
        self.timed.append((t, change))

    def _switch(self, item):
        """What plays changes to `item` (or nothing), now; what played before is kept for the lags."""
        old, times = self.playing, None
        if old is not None:
            try: times = self._times(old)
            except RuntimeError: times = None
        self.switch = {'at': self.rig.t, 'prev': old, 'times': times}
        self.playing = item

    def _callbacks(self, epochs, stops=0, errors=0):
        """The player's callbacks move the playback number and the stop / error counters: at once,
        or lag['epoch'] s later (Kodi delivers them on the thread that created the Player)."""
        player = self.rig.player

        def deliver():
            player._play_epoch += epochs
            player._play_stops += stops
            player._play_errors += errors
        if self.lag['epoch'] > 0: self.at(self.rig.t + self.lag['epoch'], deliver)
        else: deliver()

    def _start(self, item):
        self._switch(dict(item, t_open=self.rig.t, offset=0.0, paused_at=None))
        self._callbacks(2)                                    # onPlayBackStarted + onAVStarted

    def play_show(self, bid, flag=None):
        """PlayEpgTag: a show on air plays as Movistar+'s growing window (or as its declared stream,
        `streams`), a finished one as a recording. Kodi sets its flag from "Play next programme
        automatically"."""
        _, cid, start, end, _ = self.show(bid)
        flag = self.autoplay if flag is None else flag
        on_air = start <= self.rig.t < end
        self._start({'kind': 'replay', 'bid': bid, 'cid': cid, 'w0': start - LEAD, 'w1': end + TAIL,
                     'vod': not on_air, 'length': float(end - start + LEAD + TAIL),
                     'stream': self.streams.get(bid) if on_air else None,
                     'props': {reh.AUTOPLAY_PROPERTY: 'true'} if flag else {}})

    def play_live(self, cid):
        self._start({'kind': 'live', 'bid': None, 'cid': cid, 'vod': False, 'props': {}})

    def lagging(self, what):
        """Within lag[what] s after the last switch (never when that lag is 0, nor on a clock that
        went back past the switch)."""
        sw = self.switch
        return sw is not None and self.lag[what] > 0 and sw['at'] <= self.rig.t < sw['at'] + self.lag[what]

    def flagged(self):
        """What Kodi's cached PVR flags describe: the item before the last switch for lag['pvr'] s."""
        return self.switch['prev'] if self.lagging('pvr') else self.playing

    def times(self):
        """(position, length) as the player reports them; right after a switch the old item's for
        lag['times'] s."""
        p = self.playing
        if p is None: raise RuntimeError('Kodi is not playing any media file')
        if self.lagging('times') and self.switch['times'] is not None: return self.switch['times']
        return self._times(p)

    def _times(self, p):
        """A growing window: the player stops one second before the end of what it holds. A declared
        stream (2026-11-19): a fixed length (corrected once, near its end), the position at most the
        live edge (`lag` s behind the clock) and `stop` s short of the final length."""
        t = self.rig.t
        played = (p['paused_at'] if p['paused_at'] is not None else t) - p['t_open'] + p['offset']
        if p['kind'] == 'live': return float(played), float(played)
        if p['vod']: return float(min(played, p['length'])), p['length']
        s = p.get('stream')
        if s is not None:
            total = s['declared'] if t < s['final_at'] else s['final']
            return float(min(played, t - s['w0'] - s['lag'], s['final'] - s['stop'])), float(total)
        total = min(t, p['w1']) - p['w0']
        return float(min(played, total - 1)), float(total)

    def seek(self, pos):
        """The player jumps to `pos` and plays on from there; in a declared stream no further than
        its live edge (Kodi clamps it: 2026-11-19, a seek to the end landed 38 s behind the clock)."""
        p = self.playing
        now = p['paused_at'] if p['paused_at'] is not None else self.rig.t
        s = p.get('stream')
        if s is not None: pos = min(pos, self.rig.t - s['w0'] - s['lag'])
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
        self._switch(None)
        self._callbacks(1, stops, errors)

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
        elif behaviour == 'failed_start': self.pending.append(lambda: self._failed_start(kind, value))
        if self.rpc_delay: self.advance(self.rpc_delay)       # sendJSON's RPC_Delay pause: Kodi acts meanwhile
        return True

    def _failed_start(self, kind, value):
        """Kodi 21's failed start: VideoPlayer reports the target as playing (onPlayBackStarted)
        before it opens the stream; the open fails, Kodi shows "Playback failed", and Python gets a
        stop, never onPlayBackError (ApplicationPlayerCallback.cpp:196-203). No key is pressed."""
        if kind == 'broadcast': self.play_show(value)
        else: self.play_live(value)
        self.at(self.rig.t + self.fail_after, lambda: self.gone(stops=1))

    def advance(self, dt):
        self.rig.t += dt
        pending, self.pending = self.pending, []
        for change in pending: change()
        due = sorted([c for c in self.timed if c[0] <= self.rig.t], key=lambda c: c[0])
        if due:
            self.timed = [c for c in self.timed if c[0] > self.rig.t]
            for _at, change in due: change()
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
        self.rig.note(name)
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
    """The xbmc module the compiled methods (and the real logger) see: Kodi's conditions answered
    from the model, keyed by the exact strings (anything else is a mistake); Kodi's log levels
    (KODI_LOG) and its log, which is `rig.kodi_log` [(text, level, rig time)]. The log itself is
    not counted: every way to it (logger.log, logger.notice, a Kodi getter) is."""
    LOGDEBUG, LOGINFO, LOGWARNING = KODI_LOG['LOGDEBUG'], KODI_LOG['LOGINFO'], KODI_LOG['LOGWARNING']
    LOGERROR, LOGFATAL, LOGNONE = KODI_LOG['LOGERROR'], KODI_LOG['LOGFATAL'], KODI_LOG['LOGNONE']

    def __init__(self, rig):
        self.rig, self.reads = rig, []

    def log(self, msg, level=KODI_LOG['LOGDEBUG']):
        self.rig.kodi_log.append((msg, level, self.rig.t))

    def getCondVisibility(self, condition):
        self.reads.append(condition)
        self.rig.note(condition)
        hook = self.rig.hooks.get(condition)
        if hook is not None: hook()
        p = self.rig.kodi.playing
        cached = self.rig.kodi.flagged()                      # Kodi's PVR flags: cached, they may lag
        if condition == 'Pvr.IsPlayingEpgTag': return cached is not None and cached['kind'] == 'replay'
        if condition == 'Pvr.IsPlayingTv': return cached is not None and cached['kind'] == 'live'
        if condition == reh.BUSY: return p is not None and p['paused_at'] is not None
        if condition == reh.ACT_WINDOW: return bool(self.rig.kodi.window)
        raise AssertionError('unexpected condition %r' % condition)


class FakeBuiltin(object):
    """BUILTIN: only the reads the new methods may use (getInfoBool / getInfoLabel log every call
    at debug level, so they don't exist here)."""

    def __init__(self, rig):
        self.rig = rig

    def getIdle(self):
        self.rig.note('getIdle')
        hook = self.rig.hooks.get('getIdle')
        if hook is not None: hook()
        return self.rig.kodi.idle()

    def isBusyDialog(self):
        self.rig.note('isBusyDialog')
        return False


class RigSettings(object):
    """REAL_SETTINGS as the real logger.log reads it: the add-on's debug settings, the box's by
    default (Debug_Enable off, Debug_Level 0)."""

    def __init__(self, rig):
        self.rig = rig

    def getSetting(self, key):
        return {'Debug_Enable': self.rig.debug_enable, 'Debug_Level': self.rig.debug_level}[key]


class PlayerBase(object):
    """What the compiled methods use from services.Player besides themselves."""

    def __init__(self, rig):
        self.rig = rig
        self._play_epoch, self._play_stops, self._play_errors = 0, 0, 0
        self._restart_lock = rig.lock
        self.jsonRPC = RPC(rig)
        # imports.70 (the repair): the REAL Service._shutdown / _aborted over a monitor with Kodi's
        # rules. While the add-on waits, the rig's clock moves and Kodi goes on (kodi.advance); a
        # wait Kodi would never end raises ForeverWait. Kodi closes at `rig.closing_at`; the
        # add-on's own shutdown flag is `rig.pending_shutdown`.
        # The monitor counts its own calls on the rig's standstill guard and is traced (rig.traced).
        self.service = FaithfulService(advance=rig.kodi.advance, clock=lambda: rig.t, closing_at=lambda: rig.closing_at,
                                       pending=rig.pending, violations=rig.violations, on_call=rig.traced, gate=rig.gate,
                                       standstill=rig.standstill)
        self.monitor = self.service.monitor                   # as services.py wires it: Service.monitor is the Player's
        self.moves, self.lookups = [], []
        # as Player.__init__ does, last (test_state_is_per_instance_and_set_last_in_init checks it)
        self._replayEndFacts = None
        self._replayEndTrack = None

    def _kodi_raises(self, error):
        """A Player getter called while nothing plays: Kodi writes its own ERROR line before the
        exception reaches Python (interfaces/python/PythonSwig.cpp.template:229-236)."""
        self.rig.kodi_log.append(('EXCEPTION: %s' % error, FakeXbmc.LOGERROR, self.rig.t))
        raise error

    def isPlaying(self):
        self.rig.note('isPlaying')
        return self.rig.kodi.playing is not None

    def getTime(self):
        self.rig.note('getTime')
        try: return self.rig.kodi.times()[0]
        except RuntimeError as e: self._kodi_raises(e)

    def getTotalTime(self):
        self.rig.note('getTotalTime')
        try: return self.rig.kodi.times()[1]
        except RuntimeError as e: self._kodi_raises(e)

    def getPlayingItem(self):
        self.rig.note('getPlayingItem')
        hook = self.rig.hooks.get('getPlayingItem')
        if hook is not None: hook()
        p = self.rig.kodi.playing
        if p is None: self._kodi_raises(RuntimeError('Kodi is not playing any item'))      # Player.cpp:383-384
        props = dict(p['props'])
        return types.SimpleNamespace(getProperty=lambda key: props.get(key, ''))

    # Monitor._chkIdle's other calls on its not-Custom tick (the idle-loop test runs the real loop).
    def isPlayingPseudoTV(self):
        self.rig.note('isPlayingPseudoTV')
        return False

    def _chkLiveSubtitles(self):
        self.rig.note('_chkLiveSubtitles')

    def _chkTransition(self):
        self.rig.note('_chkTransition')

    def stop(self):
        self.rig.stops.append(self.rig.t)
        self.rig.note('stop')
        if self.rig.kodi.playing is not None: self.rig.kodi.gone(stops=1)

    def _runRestart(self, request):
        """imports.62's restart itself, recorded (the real requestRestart around it takes and
        gives back the RESTART lock); `rig.during_restart` runs while it holds the lock."""
        self.rig.standstill('_runRestart')
        self.rig.restarts.append((self.rig.t, dict(request)))
        if self.rig.during_restart is not None: self.rig.during_restart()


RIGS = []                                                     # every Rig of the running test (the autouse check)


class Rig(object):
    """A fake player whose imports.70 methods are the real ones, on Kodi's model and the test's
    clock (it starts 10 minutes into the frozen show, when the 2026-11-15 RESTART came). Also real:
    Player.service's _shutdown / _aborted (kodi_faithful) and imports.62's requestRestart, the
    other taker of the RESTART lock (its _runRestart is PlayerBase's recorder).
    `threads`: the move and the restart run on real threads through the add-on's own @threadit, as
    in Kodi (`rig.threads`; the default drops the decorators, so they run inline). `lock`: the
    RESTART lock to use (default: the recording Lock above). `gate`: a threading.Event; with it a
    wait Kodi would never end blocks, as on the TV, until the test sets it (FaithfulMonitor).
    imports.70 (the repair): the log chain is real too. Player.log / Player.notice (and the
    service's Service.log) run over the real logger.log / logger.notice with Kodi's levels and the
    add-on's debug settings (`debug_enable`, `debug_level`: the box's 'false' and '0'), writing to
    `kodi_log`; `log_calls` lists every logger.log call [(event, level, time)], `notices` every
    logger.notice call [(event, time)]."""

    def __init__(self, guide=None, t=S + 600, threads=False, lock=None, gate=None):
        self.t, self.closing_at, self.item_id, self.broadcasts_fail = float(t), None, None, None
        self.hooks, self.rpc_times, self.stops, self.kodi_autoplays = {}, [], [], []
        self.before_move = None
        self.kodi_log, self.log_calls, self.notices = [], [], []
        self.debug_enable, self.debug_level = 'false', '0'
        # imports.70 (the repair): what Kodi would never do (kodi_faithful), the add-on's own
        # shutdown flag, the restarts that ran, and the call sweep (note()). `standstill` counts
        # every call into the rig: a loop that never waits fails within SPIN_CAP calls
        # (kodi_faithful.Standstill), where it would record its calls for ever.
        self.violations, self.pending_shutdown, self.gate = [], False, gate
        self.standstill = Standstill(lambda: self.t, self.violations)
        self.restarts, self.during_restart, self.threads = [], None, []
        self.tracing, self.trace, self.traces, self.at_call = False, None, [], None
        self.lock = Lock(self.violations, self.standstill) if lock is None else lock
        self.kodi = Kodi(self, shows() if guide is None else guide)
        self.xbmc = FakeXbmc(self)
        rig = self
        # The add-on's logger, the real functions, over Kodi's levels and the add-on's settings.
        lg = dict(xbmc=self.xbmc, REAL_SETTINGS=RigSettings(self), ADDON_ID=ADDON_ID, ADDON_VERSION=ADDON_VERSION,
                  traceback=traceback)
        # logger.py's own namespace: notice sees the module's log, as in the add-on.
        real_log = compile_function('logger.py', 'log', **lg)
        real_notice = compile_function('logger.py', 'notice', log=real_log, **lg)

        def log(event, level=FakeXbmc.LOGDEBUG):
            rig.note('log')
            rig.log_calls.append((event, level, rig.t))
            return real_log(event, level)

        def notice(event):
            rig.note('notice')
            rig.notices.append((event, rig.t))
            return real_notice(event)
        g = dict(xbmc=self.xbmc, BUILTIN=FakeBuiltin(self), replayend_helpers=reh, restart_helpers=rh,
                 clock_helpers=types.SimpleNamespace(now=self.now),
                 Globals=types.SimpleNamespace(_decodePlot=decode), log=log, notice=notice)
        # compile_method keeps each method's compiled code (the sweeps build hundreds of rigs) and
        # its real line numbers in services.py.
        ns = {name: compile_method('services.py', 'Player', name, **g) for name in NEW_METHODS + LOG_METHODS}
        real_lookup, real_move = ns['_lookupReplayEnd'], ns['_moveReplayEnd']
        real_restart = compile_method('services.py', 'Player', 'requestRestart', **g)

        def _lookupReplayEnd(player, epoch):
            player.lookups.append((rig.t, epoch))
            return real_lookup(player, epoch)

        def _moveReplayEnd(player, facts, epoch):
            player.moves.append((rig.t, dict(facts), epoch))
            if rig.before_move is not None: rig.before_move()
            if not rig.tracing: return real_move(player, facts, epoch)
            rig.trace = []
            try: return real_move(player, facts, epoch)
            finally:
                rig.traces.append(rig.trace)
                rig.trace = None

        def requestRestart(player, request=None):
            return real_restart(player, request)

        if threads:
            from globals import threadit                      # pool.py's own decorator: a real thread per call

            def on_a_thread(method):
                threaded = threadit(method)

                def start(*args):
                    thread = threaded(*args)
                    rig.threads.append(thread)
                    return thread
                return start
            _moveReplayEnd, requestRestart = on_a_thread(_moveReplayEnd), on_a_thread(requestRestart)

        ns.update(_lookupReplayEnd=_lookupReplayEnd, _moveReplayEnd=_moveReplayEnd, requestRestart=requestRestart)
        # Named Player, as the real class: its name starts every line the add-on writes ('Player: ...').
        self.player = type('Player', (PlayerBase,), ns)(self)
        # The service's own line (_shutdown's bookkeeping) through the real Service.log as well, so
        # kodi_log holds it exactly when Kodi's log would (debug logging only).
        service, recorded = self.player.service, self.player.service.log
        service_log = compile_method('services.py', 'Service', 'log', **g)

        def service_log_line(msg, level=FakeXbmc.LOGDEBUG):
            recorded(msg, level)                              # FaithfulService.logged, as part 1's tests read it
            return service_log(service, msg, level)
        service.log = service_log_line
        RIGS.append(self)

    def note(self, name):
        """Every call the compiled methods make into Kodi (JSON-RPC, conditions, the idle time, the
        player) and into the add-on's own log and shutdown flag passes here: counted by the rig's
        standstill guard, then traced (traced()). The monitor's calls are traced directly: the
        monitor counts them itself."""
        self.standstill(name)
        self.traced(name)

    def traced(self, name):
        """While a move is traced (`tracing`) the calls are listed, one list per move in `traces`,
        and `at_call` = (n, action) runs `action` during the n-th one, once: it raises (that call
        failing) or changes Kodi (it starts to close)."""
        if self.trace is None: return
        self.trace.append(name)
        if self.at_call is not None and len(self.trace) == self.at_call[0]:
            action, self.at_call = self.at_call[1], None
            action()

    def pending(self):
        """PROPERTIES.isPendingShutdown(): the add-on's own shutdown flag."""
        self.note('isPendingShutdown')
        return self.pending_shutdown

    def now(self):
        """clock_helpers.now(): counted (a loop that only reads the clock would turn for ever on
        this one), not traced: the sweeps make Kodi calls fail, and reading the clock can't."""
        self.standstill('clock_helpers.now()')
        return self.t

    def lock_leaked(self):
        """The RESTART lock was taken by the code under test and not given back."""
        lock = self.player._restart_lock
        return lock.leaked() if isinstance(lock, Lock) else lock.locked()

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

    def always_on(self):
        """The watcher's always-on lines in kodi.log: [(the text after 'Player: ', time)]."""
        head = PREFIX + 'Player: replay end:'
        return [(m[len(PREFIX + 'Player: '):], t) for m, lv, t in self.kodi_log if lv == FakeXbmc.LOGINFO and m.startswith(head)]

    def lines(self):
        return [m for m, _ in self.always_on()]

    def logged_at(self, line):
        return [t for m, t in self.always_on() if m == line]

    def _player_log(self, level):
        """What the watcher handed to Player.log at `level` (written or not): the messages."""
        return [m[len('Player: '):] for m, lv, _ in self.log_calls if lv == level and m.startswith('Player: ')]

    def warnings(self):
        return self._player_log(FakeXbmc.LOGWARNING)

    def errors(self):
        return self._player_log(FakeXbmc.LOGERROR)

    def kodi_errors(self):
        """kodi.log's ERROR lines: the add-on's (with their traceback) and Kodi's own 'EXCEPTION: ...'."""
        return [m for m, lv, _ in self.kodi_log if lv >= FakeXbmc.LOGERROR]

    def run_idle_loop(self, until):
        """Monitor._chkIdle, the real loop of services.py, over the faithful service: while no Custom
        channel plays it waits 0.5 s, ticks (_chkLiveSubtitles, _chkTransition, _chkReplayEnd), waits
        0.5 s, ...; Kodi starts to close at `until`, which ends it."""
        chk = compile_method('services.py', 'Monitor', '_chkIdle', BUILTIN=FakeBuiltin(self), OSD_TIMER=60)
        rig, said = self, []

        def log(msg, level=FakeXbmc.LOGDEBUG):
            rig.note('Monitor.log')
            said.append(msg)
        monitor = types.SimpleNamespace(isRunning=False, service=self.player.service, player=self.player,
                                        abortRequested=self.player.monitor.abortRequested, log=log)
        self.closing_at = until
        chk(monitor)
        return monitor, said


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


def about_to_move(flag=None, **rig_options):
    """A rig whose replay stands frozen at the live edge, one tick before the move is due
    -> (rig, the time it is due). The 12 minutes in which the window still grows are jumped over
    (the tick's count restarts after such a gap, as after a suspend), so the freeze and the move
    come at the same times as in to_the_live_edge: well after the guide end, whatever the rule."""
    rig = Rig(t=E - 100, **rig_options)
    stuck = to_the_live_edge(rig, flag=flag)
    rig.t = stuck - 3.0
    rig.tick_until(stuck + reh.STALL_S - 1)
    assert rig.player.moves == [] and rig.player._replayEndTrack['since'] == stuck
    return rig, stuck + reh.STALL_S


# imports.70 (the repair): every always-on line, word for word; only ids, counts and UTC times vary.
_ID, _UTC = r'\d+', r'\d{4}-\d\d-\d\d \d\d:\d\d:\d\d'
_TARGET = r'(?:the next show, broadcast \d+|live channel \d+)'
_WHY = '(?:%s)' % '|'.join([re.escape('its guide entry is over'),
                            re.escape('the guide lists its show as on air until ') + _UTC + re.escape(' UTC'),
                            re.escape('its guide entry is over and no next entry has started (or the guide changed)')])
_NOTE = '(?:%s)' % '|'.join(re.escape(reh.outcome_note(word)) for word in sorted(reh.OUTCOME_NOTES))


def _line(text, **parts):
    """An anchored pattern: `text` word for word, with {id}, {n}, {utc}, {why}, {target}, {note}."""
    out = re.escape(text)
    for name, pattern in dict(id=_ID, n=_ID, utc=_UTC, why=_WHY, target=_TARGET, note=_NOTE, **parts).items():
        out = out.replace(re.escape('{%s}' % name), pattern)
    return re.compile(out)


ALWAYS_ON = {
    'watching':    _line('replay end: watching broadcast {id} on channel {id}; its guide entry ends {utc} UTC'),
    'not on air':  _line('replay end: not watching broadcast {id}: not a LiveTV show on air now: left to Kodi'),
    'no entry':    _line('replay end: not watching: Kodi reports no guide entry playing'),
    'waiting':     _line('replay end: broadcast {id} is frozen at its end ({n} of {n} s); waiting for full-screen video with'
                         ' no menu open and {n} s without a key press'),
    'moving on':   _line('replay end: broadcast {id} stood still for {n} s at {n} of {n} s: moving on (attempt {n} of {n})'),
    'lock busy':   _line('replay end: broadcast {id}: not moving on now: a restart or an earlier move is still running'),
    'gone':        _line('replay end: broadcast {id}: not moving on: it no longer plays'),
    'not ready':   _line('replay end: broadcast {id}: not moving on now: not in full-screen video, a key was pressed or'
                         ' another video plays'),
    'flag off':    _line('replay end: broadcast {id}: stopping, as Kodi does at the end of a replay without its next-show'
                         ' flag ("Play next programme automatically" was off when it started, or "Play only this'
                         ' programme" opened it)'),
    'target':      _line('replay end: broadcast {id}: {why}: opening {target}'),
    'result':      _line('replay end: broadcast {id}: {target}: {note}'),
    'no live now': _line('replay end: broadcast {id}: the next show did not open; not trying the live channel now (it no'
                         ' longer plays, not in full-screen video, or a key was pressed)'),
    'no stop now': _line('replay end: broadcast {id}: the live channel did not open; not stopping now (not in full-screen'
                         ' video, or a key was pressed)'),
    'last stop':   _line('replay end: broadcast {id}: the live channel did not open: stopping'),
}
# The most always-on lines in a row of one broadcast between two starts: a lookup's line (then at
# most a 'waiting' line) or an attempt's 'moving on' line (then at most a 'waiting' line before
# the next attempt, or the target, two open results and a stop or 'not stopping now': 5). A typical
# move writes 4 lines in all (A1), a replay with one attempt at most 7 (the test below); two failing
# attempts of one replay can write up to 13 (code-design C's volume note), 8 at most per attempt.
MAX_LINES_PER_ATTEMPT = 8
_FIRST_ID = re.compile(r'^replay end: (?:not watching broadcast|watching broadcast|broadcast) (\d+)')


def check_lines(built):
    """imports.70 (the repair), A10 and the cap: every line the watcher hands to logger.notice is
    one of ALWAYS_ON as Kodi's log writes it (INFO, the add-on's prefix, 'Player: '), nothing
    URL-like, no line twice at one instant, at most MAX_LINES_PER_ATTEMPT per attempt."""
    for event, _t in built.notices:
        assert event.startswith('Player: replay end: '), 'an always-on line from outside the watcher: %r' % (event,)
    seen, counts = set(), {}
    for text, t in built.always_on():
        assert any(p.fullmatch(text) for p in ALWAYS_ON.values()), 'not an always-on line: %r' % (text,)
        assert '://' not in text and (text, t) not in seen, 'URL-like or written twice: %r' % (text,)
        seen.add((text, t))
        found = _FIRST_ID.match(text)
        key = found.group(1) if found else None
        if 'watching' in text or ALWAYS_ON['moving on'].fullmatch(text): counts[key] = 0
        counts[key] = counts.get(key, 0) + 1
        assert counts[key] <= MAX_LINES_PER_ATTEMPT, 'more than %d lines in one attempt: %r' % (MAX_LINES_PER_ATTEMPT, text)
    for text, _level, _t in built.kodi_log:
        assert not (text.startswith(PREFIX) and '://' in text), 'URL-like: %r' % (text,)


def check_rigs(rigs):
    """imports.70 (the repair): what must hold for every rig a test built. No wait Kodi would never
    end was asked for (also one swallowed by an `except`), and the RESTART lock is free unless the
    test itself holds it. 2026-11-19: the move stood in such a wait, holding the lock. And the
    always-on lines are the ones designed (check_lines)."""
    for built in rigs:
        assert built.violations == [], 'Kodi would wait forever: %s' % built.violations
        assert not built.lock_leaked(), 'the RESTART lock was not given back'
        check_lines(built)


@pytest.fixture(autouse=True)
def kodi_faithful_checks():
    """check_rigs after every test of this file."""
    del RIGS[:]
    yield
    rigs = RIGS[:]
    del RIGS[:]
    check_rigs(rigs)


@pytest.fixture
def rig():
    return Rig(t=E - 100)


def moved_on(rig):
    """The line of the tick that decided to move on."""
    return [m for m in rig.lines() if ': moving on (attempt ' in m]


# imports.70 (the repair): the always-on lines as the tests expect them (the A tests below spell
# out every word of the main ones; ALWAYS_ON holds them all).
def watching_line(bid, end, cid=CID):
    return 'replay end: watching broadcast %s on channel %s; its guide entry ends %s UTC' % (bid, cid, utc(end))


def not_on_air_line(bid):
    return 'replay end: not watching broadcast %s: not a LiveTV show on air now: left to Kodi' % bid


NO_ENTRY_LINE = 'replay end: not watching: Kodi reports no guide entry playing'


def waiting_line(pos, total, bid=BID):
    return ('replay end: broadcast %s is frozen at its end (%d of %d s); waiting for full-screen video with no menu'
            ' open and %d s without a key press' % (bid, pos, total, reh.QUIET_S))


def moving_on_line(held, pos, total, attempt=1, bid=BID):
    return ('replay end: broadcast %s stood still for %d s at %d of %d s: moving on (attempt %d of %d)'
            % (bid, held, pos, total, attempt, reh.MAX_FIRES))


OVER = 'its guide entry is over'
NOTHING_NEXT = 'its guide entry is over and no next entry has started (or the guide changed)'


def on_air_why(end):
    return 'the guide lists its show as on air until %s UTC' % utc(end)


def _target_words(target):
    kind, value = target
    return ('the next show, broadcast %s' if kind == 'broadcast' else 'live channel %s') % value


def target_line(why, target, bid=BID):
    return 'replay end: broadcast %s: %s: opening %s' % (bid, why, _target_words(target))


def result_line(target, outcome, bid=BID):
    return 'replay end: broadcast %s: %s: %s' % (bid, _target_words(target), reh.outcome_note(outcome))


def not_ready_line(bid=BID):
    return ('replay end: broadcast %s: not moving on now: not in full-screen video, a key was pressed or another video'
            ' plays' % bid)


def flag_off_line(bid=BID):
    return ('replay end: broadcast %s: stopping, as Kodi does at the end of a replay without its next-show flag ("Play'
            ' next programme automatically" was off when it started, or "Play only this programme" opened it)' % bid)


def lock_busy_line(bid=BID):
    return 'replay end: broadcast %s: not moving on now: a restart or an earlier move is still running' % bid


def gone_line(bid=BID):
    return 'replay end: broadcast %s: not moving on: it no longer plays' % bid


def no_live_now_line(bid=BID):
    return ('replay end: broadcast %s: the next show did not open; not trying the live channel now (it no longer plays,'
            ' not in full-screen video, or a key was pressed)' % bid)


def no_stop_now_line(bid=BID):
    return ('replay end: broadcast %s: the live channel did not open; not stopping now (not in full-screen video, or a'
            ' key was pressed)' % bid)


def last_stop_line(bid=BID):
    return 'replay end: broadcast %s: the live channel did not open: stopping' % bid


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
    assert rig.lines() == [watching_line(BID, E), moving_on_line(reh.STALL_S, 6053, 6054),
                           target_line(OVER, ('broadcast', NEXT)), result_line(('broadcast', NEXT), 'started'),
                           watching_line(NEXT, E + 3600)]
    assert not [m for m, _, _ in rig.kodi_log if '://' in m]
    assert rig.warnings() == [] and rig.errors() == [] and rig.kodi_errors() == []


def test_a_finished_shows_replay_is_left_to_kodi_and_kodis_next_show_is_watched():
    """A recording ends by itself and Kodi opens the next show (the one on air): the add-on never
    acts on the recording, and watches the show Kodi landed on."""
    rig = Rig()
    rig.restart(PREV)
    rig.tick(10)
    assert rig.lines() == [not_on_air_line(PREV)]
    assert rig.player._replayEndFacts is None
    rig.kodi.seek(rig.kodi.times()[1] - 30)
    rig.tick(60)
    assert len(rig.kodi_autoplays) == 1 and rig.opens() == [] and rig.player.moves == []
    assert rig.kodi.playing['bid'] == BID and rig.player._replayEndFacts['broadcastid'] == BID
    assert rig.lines()[-1] == watching_line(BID, E)


def test_the_live_edge_while_the_window_grows_never_moves(rig):
    to_the_live_edge(rig)
    rig.tick_until(E + TAIL - 1)
    assert rig.opens() == [] and rig.player.moves == []
    assert not [m for m in rig.lines() if 'still' in m]


def test_a_freeze_at_the_live_edge_moves_on_twenty_seconds_after_the_window_closed(rig):
    stuck = to_the_live_edge(rig)
    rig.tick_until(stuck + 60)
    assert rig.opens() == [broadcast(NEXT)] and rig.open_times() == [stuck + reh.STALL_S]


def test_a_freeze_during_the_show_moves_to_the_live_channel_twenty_seconds_later():
    """E3. imports.70 (the repair; the user's rule of 2026-11-19): a viewer at the live edge of a
    show still on air whose stream stands still mid-show (it ended early, as Mega's did that
    evening, or the provider's manifest stalled): 20 s later, with no wait for the guide end, and
    since the guide still lists the show as on air, the live channel. (The first build waited for
    the guide end + 60 s and then opened the next show: review break T15 pinned that rule.)"""
    rig = Rig(t=E - 400)
    rig.restart()
    rig.tick(8)                                               # the lookup arms it
    assert rig.player._replayEndFacts['end'] == E
    rig.kodi.seek(rig.kodi.times()[1] - 1)                    # the live edge
    rig.kodi.playing['w1'] = E - 300                          # the window stops growing mid-show
    rig.tick_until(E - 300 + reh.STALL_S - 1)
    assert rig.opens() == [] and rig.player.moves == []
    rig.tick_until(E + 30)
    assert rig.open_times() == [E - 300 + reh.STALL_S] and rig.opens() == [channel(CID)]
    assert rig.kodi.playing['kind'] == 'live' and rig.stops == []
    assert rig.lines()[-2:] == [target_line(on_air_why(E), ('channel', CID)), result_line(('channel', CID), 'started')]


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
    waiting = waiting_line(6053, 6054)
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
    assert rig.lines()[-1] == flag_off_line()
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
    assert rig.lines()[-2:] == [target_line(NOTHING_NEXT, ('channel', CID)), result_line(('channel', CID), 'started')]


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
    assert result_line(('broadcast', NEXT), outcome or 'not_opened') in rig.lines()
    assert rig.stops == [] and rig.lines()[-1] == result_line(('channel', CID), 'started')


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
    assert result_line(('broadcast', NEXT), 'retune') in rig.lines() and rig.stops == []
    assert rig.lines()[-1] == result_line(('channel', CID), 'started')


def test_a_key_pressed_during_player_opens_pause_counts_as_the_viewer_taking_over(rig):
    """The key check starts before Player.Open too: a key pressed while it ran, with nothing playing
    after a failed next show, leaves it to the viewer (no live channel forced on)."""
    stuck = to_the_live_edge(rig)
    rig.kodi.behave[('broadcast', NEXT)] = 'gone'
    rig.kodi.rpc_delay = 1.0
    rig.hooks['Open'] = rig.kodi.press_key
    rig.tick_until(stuck + 60)
    assert rig.opens() == [broadcast(NEXT)] and rig.stops == []
    assert rig.lines()[-1] == result_line(('broadcast', NEXT), 'gave_up')


@pytest.mark.parametrize('behaviour', ['refuse', 'ignore'])
def test_when_live_wont_open_either_it_stops_the_frozen_replay(rig, behaviour):
    stuck = to_the_live_edge(rig)
    rig.kodi.behave[('broadcast', NEXT)] = behaviour
    rig.kodi.behave[('channel', CID)] = behaviour
    rig.tick_until(stuck + 60)
    assert rig.opens() == [broadcast(NEXT), channel(CID)] and len(rig.stops) == 1 and rig.kodi.playing is None
    assert rig.lines()[-1] == last_stop_line()
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
    assert rig.lines()[-1] == no_stop_now_line()
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
    assert rig.lines()[-1] == result_line(('broadcast', NEXT), 'failed')


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
    assert rig.lines()[-1] == no_live_now_line()


def test_a_running_restart_blocks_the_move_and_it_tries_again_a_minute_later(rig):
    stuck = to_the_live_edge(rig)
    rig.player._restart_lock.held = True                      # a RESTART is running
    rig.tick_until(stuck + reh.STALL_S + 30)
    assert rig.opens() == [] and rig.lines()[-1] == lock_busy_line()
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
    assert rig.lines()[-1] == gone_line()
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
    assert not_on_air_line(PREV) in rig.lines()


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
    assert rig.lines()[-1] == not_ready_line()
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
    assert rig.lines() == [not_on_air_line(BID)]


def test_kodi_closing_ends_the_watch_without_falling_back(rig):
    """The watch waits through the real Service._shutdown, WATCH_POLL at a time; the wait in which
    Kodi starts to close ends there and ends the move: no fallback, nothing stopped."""
    stuck = to_the_live_edge(rig)
    rig.kodi.behave[('broadcast', NEXT)] = 'ignore'
    rig.closing_at = stuck + reh.STALL_S + 3
    rig.tick_until(stuck + reh.STALL_S)
    assert rig.opens() == [broadcast(NEXT)] and rig.stops == []
    assert rig.lines()[-1] == target_line(OVER, ('broadcast', NEXT))
    assert rig.t == stuck + reh.STALL_S + 3
    assert rig.player.monitor.waits == [rh.WATCH_POLL] * 6
    assert rig.player.service.logged == [('_shutdown, pendingShutdown = True, wait = %s' % rh.WATCH_POLL, 0)]


def test_kodi_closing_before_the_move_does_nothing(rig):
    """The move's first question, "is Kodi closing?", is asked without waiting (Service._aborted:
    one read of Kodi's flag, no time passes, no bookkeeping). TV test 2026-11-19: the first build
    asked with _shutdown(0), which Kodi turns into a wait without end."""
    stuck = to_the_live_edge(rig)
    rig.closing_at = stuck
    rig.tick_until(stuck + reh.STALL_S)
    assert len(rig.player.moves) == 1 and rig.opens() == [] and rig.stops == []
    assert [c[0] for c in rig.player.jsonRPC.calls] == ['GetItem', 'GetChannels']
    assert rig.player.monitor.waits == [] and rig.player.monitor.checks == 1 and rig.t == stuck + reh.STALL_S
    assert rig.player.service.pendingShutdown is False and rig.player.service.logged == []


def test_the_addons_own_shutdown_flag_before_the_move_does_nothing(rig):
    """The other signal of Service._aborted: the add-on's own pending-shutdown flag, with Kodi
    still running. Asked without waiting too."""
    stuck = to_the_live_edge(rig)
    rig.tick_until(stuck + reh.STALL_S - 1)
    rig.pending_shutdown = True
    rig.tick(1)
    assert len(rig.player.moves) == 1 and rig.opens() == [] and rig.stops == []
    assert [c[0] for c in rig.player.jsonRPC.calls] == ['GetItem', 'GetChannels']
    assert rig.player.monitor.waits == [] and rig.t == stuck + reh.STALL_S
    rig.pending_shutdown = False
    rig.tick_until(stuck + reh.STALL_S + reh.RETRY_S)             # the second attempt, a minute later, moves
    assert rig.open_times() == [stuck + reh.STALL_S + reh.RETRY_S] and rig.opens() == [broadcast(NEXT)]


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
    refused = rig.logged_at(result_line(('broadcast', NEXT), 'refused'))
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
    assert rig.lines()[-1] == result_line(('channel', CID), 'failed')


@pytest.mark.parametrize('kind', ['broadcast', 'channel'])
def test_another_channel_playing_after_an_open_is_left_alone(rig, kind):
    """The viewer zapped while Kodi opened it: 'other', and nothing more is done."""
    stuck = to_the_live_edge(rig)
    if kind == 'channel': rig.kodi.behave[('broadcast', NEXT)] = 'refuse'
    rig.kodi.behave[(kind, NEXT if kind == 'broadcast' else CID)] = 'other'
    rig.tick_until(stuck + 60)
    assert rig.kodi.playing['cid'] == OTHER_CID and rig.stops == []
    assert rig.lines()[-1] == result_line((kind, NEXT if kind == 'broadcast' else CID), 'other')
    assert len(rig.opens()) == (1 if kind == 'broadcast' else 2)


def test_the_tick_reads_no_jsonrpc_until_the_move(rig):
    stuck = to_the_live_edge(rig)
    rig.tick_until(stuck + reh.STALL_S - 1)
    assert [name for name, _ in rig.rpc_times] == ['GetItem', 'GetChannels']
    rig.tick(1)
    # the move: what plays, the guide, what plays once more right before acting (the fix stage), the
    # open, and the watch's first poll
    assert [name for name, _ in rig.rpc_times][2:] == ['GetItem', 'GetBroadcasts', 'GetItem', 'Open', 'GetItem']


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
    assert rig.lines() == [NO_ENTRY_LINE]
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


# === the repair of 2026-11-19, step 2: the new rule on the TV test's Mega replay ===================
# The user's rule: 20 s after the picture froze, with no wait for the guide's end time; the live
# channel while the guide lists the show as on air, else the next entry (Kodi's rule) or live; with
# Kodi's flag off, a stop. The Mega replay of that evening on the declared stream model (MEGA_*).

def mega_rig(end=MEGA_END, next_start=None, flag=None, **rig_options):
    """The TV test of 2026-11-19 on the rig, up to the operator's seek: Mega's replay opened at
    16:51:58 UTC and played from the start of its stream; at 16:52:24 the seek to its end, which
    Kodi clamped to the live edge (3706.3 s). The player then stays at the live edge and freezes at
    16:59:31-32 (MEGA_FROZEN_AT). `end`: the frozen entry's guide end; `next_start`: the next one's."""
    rig = Rig(guide=mega_shows(end, next_start), t=MEGA_OPEN, **rig_options)
    rig.kodi.streams[BID] = MEGA_STREAM
    rig.restart(flag=flag)
    rig.tick_until(MEGA_SEEK)
    rig.kodi.seek(rig.kodi.times()[1] - 20)
    return rig


def test_the_declared_model_reproduces_the_recording():
    """The stream model of the Mega tests against the recording itself (the samples embedded in
    test_imports70_replayend_helpers.py): the first sample of each second within 0.25 s while it
    moves, the frozen 4133.5 of 4134.0 s exactly, and the length corrected at 16:59:16."""
    from test_imports70_replayend_helpers import MEGA_BASE, MEGA_FROZEN, MEGA_MOVING
    rig = mega_rig()
    rig.kodi.window = False                                   # in a menu: no move, the stream stays frozen
    assert abs(rig.kodi.times()[0] - 3706.3) < 1e-6 and rig.kodi.times()[1] == 4140.0
    for s, pos, total in MEGA_MOVING[1] + MEGA_FROZEN:
        rig.tick_until(MEGA_BASE + s)
        now_pos, now_total = rig.kodi.times()
        assert now_total == total and abs(now_pos - pos) <= (0.25 if s < 32 else 0), (s, now_pos, pos)
    assert MEGA_BASE + 31 == MEGA_FROZEN_AT and rig.kodi.times() == (4133.5, 4134.0)


def test_the_mega_2026_11_19_case_moves_to_the_live_channel_before_the_guide_end():
    """E1. The TV test of 2026-11-19 with the repair: the replay is watched from 6 s after it
    opened; it freezes at 16:59:31-32, and at 16:59:51, 20 s later and 9 s before its guide end, the
    live channel opens: the guide still lists the show as on air. Live plays, nothing is stopped,
    and the RESTART lock is free (the first build stood in its first line holding it)."""
    rig = mega_rig()
    assert rig.player.lookups == [(MEGA_OPEN + 1 + reh.ARM_DELAY_S, 2)]
    rig.tick_until(MEGA_DUE + 30)
    assert rig.open_times() == [MEGA_DUE] and MEGA_DUE < MEGA_END
    assert rig.opens() == [channel(CID)] and rig.kodi.playing['kind'] == 'live' and rig.kodi.playing['cid'] == CID
    assert rig.stops == [] and len(rig.player.moves) == 1 and not rig.lock.locked() and rig.errors() == []


def test_the_same_freeze_five_minutes_after_the_guide_end_plays_the_next_show():
    """E2. The same stream with the guide entry ending at 16:54:31 (a viewer behind live whose
    replay freezes five minutes after its guide end): at the same moment the next entry, which has
    started, opens as a replay and is watched in turn."""
    end = T('16:54:31')
    rig = mega_rig(end=end)
    rig.tick_until(MEGA_DUE + 30)
    assert rig.open_times() == [MEGA_DUE] and rig.opens() == [broadcast(NEXT)] and rig.kodi.playing['bid'] == NEXT
    assert rig.stops == [] and rig.lines()[-1] == watching_line(NEXT, end + 6300)


def test_with_kodis_flag_off_it_stops_before_the_guide_end_too():
    """E4. Kodi's flag off (its "Play next programme automatically" was off when the replay started):
    Kodi would stop at a natural end, so the frozen replay stops, 20 s after it froze, guide end or
    not; the guide isn't read."""
    rig = mega_rig(flag=False)
    rig.tick_until(MEGA_DUE + 30)
    assert rig.opens() == [] and rig.stops == [MEGA_DUE] and rig.kodi.playing is None
    assert rig.lines()[-1] == flag_off_line() and 'GetBroadcasts' not in [c[0] for c in rig.player.jsonRPC.calls]


@pytest.mark.parametrize('when', ['before the move', 'during the guide read'])
def test_the_guide_end_decides_at_the_moment_of_the_move(when):
    """E5. Due one second before the guide end (16:59:51, the entry ending at 16:59:52), and the move
    runs 2 s later: before it starts, or while it reads the guide. The clock is read after the guide
    read, so the entry is over by then and the next one (from 16:59:52) plays; the target choice
    reads the clock once, between the guide read and the target's line."""
    rig = mega_rig(end=MEGA_DUE + 1)
    rig.tick_until(MEGA_DUE - 1)
    if when == 'before the move': rig.before_move = lambda: rig.kodi.advance(2.0)
    else: rig.hooks['GetBroadcasts'] = lambda: rig.kodi.advance(2.0)
    clock = _compiled_globals(rig)['clock_helpers']
    real_now = clock.now

    def now():
        rig.traced('clock')
        return real_now()
    clock.now = now
    rig.tracing = True
    rig.tick(1)
    assert [t for t, _facts, _epoch in rig.player.moves] == [MEGA_DUE]
    assert rig.opens() == [broadcast(NEXT)] and rig.open_times() == [MEGA_DUE + 2]
    trace = rig.traces[0]
    read, line = trace.index('GetBroadcasts'), trace.index('notice')
    assert trace[read + 1:line].count('clock') == 1 and 'clock' not in trace[:read]
    assert rig.lines()[-2] == target_line(OVER, ('broadcast', NEXT))


def test_a_second_attempt_after_the_guide_end_plays_the_next_show():
    """E6. The first attempt (16:59:51, the show still on air) finds a RESTART running and does
    nothing; the second, a minute later, comes after the guide end: the next show, not the live
    channel the first one would have opened. The target is chosen at each attempt."""
    rig = mega_rig()
    rig.tick_until(MEGA_DUE - 1)
    rig.lock.held = True
    rig.tick(1)
    rig.lock.held = False
    assert rig.lines()[-1] == lock_busy_line() and rig.opens() == []
    rig.tick_until(MEGA_DUE + reh.RETRY_S + 10)
    assert rig.open_times() == [MEGA_DUE + reh.RETRY_S] and rig.opens() == [broadcast(NEXT)]
    assert rig.kodi.playing['bid'] == NEXT


def test_an_overlapping_next_entry_still_means_live_while_the_show_is_on():
    """E7. The guide's next entry starts at 16:59:00, a minute before the frozen one ends (Kodi trims
    such overlaps, so this is belt and braces): while the guide still lists the frozen show as on
    air, the live channel opens, though Kodi's next-show rule alone would give the next entry."""
    rig = mega_rig(next_start=T('16:59:00'))
    rig.tick_until(MEGA_DUE + 30)
    assert rig.opens() == [channel(CID)] and rig.kodi.playing['kind'] == 'live'


@pytest.mark.parametrize('behaviour, outcome, stopped_after', [('refuse', 'not_opened', 0.0), ('ignore', 'refused', rh.WATCH_GRACE)])
def test_live_refused_before_the_guide_end_stops_the_frozen_replay(behaviour, outcome, stopped_after):
    """E8. The live channel is the first target (the show is still on air) and doesn't open: the
    frozen replay is stopped, as when nothing opens after a next show (2026-11-15 decision); the
    next show is never tried."""
    rig = mega_rig()
    rig.kodi.behave[('channel', CID)] = behaviour
    rig.tick_until(MEGA_DUE + 30)
    assert rig.opens() == [channel(CID)] and rig.stops == [MEGA_DUE + stopped_after] and rig.kodi.playing is None
    assert rig.lines()[-3:] == [target_line(on_air_why(MEGA_END), ('channel', CID)), result_line(('channel', CID), outcome),
                                last_stop_line()]


def test_the_mega_case_through_the_real_idle_loop():
    """E10. The same evening through Monitor._chkIdle itself (services.py), over the faithful
    service: it waits 0.5 s, ticks, waits 0.5 s, so its ticks fall on the half second. The freeze
    reads at 16:59:31.5-32.5 and the live channel opens 20 s later; every wait the loop and the move
    make is 0.5 s; Kodi closing at 17:00:30 ends the loop."""
    rig = Rig(guide=mega_shows(), t=MEGA_OPEN)
    rig.kodi.streams[BID] = MEGA_STREAM
    rig.restart()
    rig.kodi.at(MEGA_SEEK, lambda: rig.kodi.seek(rig.kodi.times()[1] - 20))
    monitor, said = rig.run_idle_loop(until=T('17:00:30'))
    assert rig.opens() == [channel(CID)] and rig.open_times() == [MEGA_DUE + 0.5]
    assert rig.kodi.playing['kind'] == 'live' and rig.stops == []
    assert set(rig.player.monitor.waits) == {0.5} and len(rig.player.monitor.waits) > 2 * 500
    assert monitor.isRunning is False and said[-1] == '_chkIdle, shutdown!' and rig.t == T('17:00:30')
    assert rig.lines()[-1] == result_line(('channel', CID), 'started')


# --- the always-on lines (A1-A12): what kodi.log holds on a box without debug logging -------------

def test_a_full_move_writes_these_lines_with_debug_off():
    """A1. The Mega move, as Kodi's log holds it with the add-on's debug logging off (the box): four
    lines, each once, at INFO, with the add-on's prefix, at these times; nothing else."""
    rig = mega_rig()
    rig.tick_until(MEGA_DUE + 30)
    head = PREFIX + 'Player: '
    assert rig.kodi_log == [
        (head + 'replay end: watching broadcast 90210 on channel 47; its guide entry ends 2026-11-19 17:00:00 UTC',
         FakeXbmc.LOGINFO, MEGA_OPEN + 6),
        (head + 'replay end: broadcast 90210 stood still for 20 s at 4133 of 4134 s: moving on (attempt 1 of 2)',
         FakeXbmc.LOGINFO, MEGA_DUE),
        (head + 'replay end: broadcast 90210: the guide lists its show as on air until 2026-11-19 17:00:00 UTC:'
         ' opening live channel 47', FakeXbmc.LOGINFO, MEGA_DUE),
        (head + 'replay end: broadcast 90210: live channel 47: started (Kodi switched to it)',
         FakeXbmc.LOGINFO, MEGA_DUE + rh.WATCH_POLL)]
    assert FakeXbmc.LOGINFO == 1 and PREFIX == 'plugin.video.pseudotv.imports-0.8.0+imports.70-'


def test_the_lines_of_a_move_to_the_next_show():
    """A2. The same freeze after the guide end: the next show's lines, then its own 'watching'."""
    end = T('16:54:31')
    rig = mega_rig(end=end)
    rig.tick_until(MEGA_DUE + 30)
    assert rig.lines() == [watching_line(BID, end), moving_on_line(20, 4133, 4134),
                           'replay end: broadcast 90210: its guide entry is over: opening the next show, broadcast 90211',
                           'replay end: broadcast 90210: the next show, broadcast 90211: started (Kodi switched to it)',
                           watching_line(NEXT, end + 6300)]
    # NEXT's lookup: the tick after the switch (MEGA_DUE + 1.5) first sees its playback, 5 s later
    assert [t for _, t in rig.always_on()] == [MEGA_OPEN + 6, MEGA_DUE, MEGA_DUE, MEGA_DUE + 0.5,
                                               MEGA_DUE + 1.5 + reh.ARM_DELAY_S]


def test_the_lines_of_a_flag_off_stop():
    """A3."""
    rig = mega_rig(flag=False)
    rig.tick_until(MEGA_DUE + 30)
    assert rig.lines() == [watching_line(BID, MEGA_END), moving_on_line(20, 4133, 4134), flag_off_line()]
    assert rig.lines()[-1] == ('replay end: broadcast 90210: stopping, as Kodi does at the end of a replay without its'
                               ' next-show flag ("Play next programme automatically" was off when it started, or "Play'
                               ' only this programme" opened it)')


def test_in_the_guide_one_waiting_line_in_ten_minutes():
    """A4. The viewer is in the guide when it freezes and stays there 10 minutes: one 'waiting'
    line, not one a tick; back in full-screen video with a key press, it moves 10 s later. The guide
    end has passed by then, so the next show."""
    rig = mega_rig()
    rig.tick_until(MEGA_FROZEN_AT - 5)
    rig.kodi.window = False
    rig.tick_until(MEGA_DUE + 600)
    assert rig.lines() == [watching_line(BID, MEGA_END), waiting_line(4133, 4134)]
    assert rig.lines()[-1] == ('replay end: broadcast 90210 is frozen at its end (4133 of 4134 s); waiting for'
                               ' full-screen video with no menu open and 10 s without a key press')
    assert rig.logged_at(rig.lines()[-1]) == [MEGA_DUE]
    rig.kodi.window = True
    rig.kodi.press_key()
    back = rig.t
    rig.tick_until(back + 20)
    held = back + reh.QUIET_S - MEGA_FROZEN_AT
    assert rig.lines()[2:] == [moving_on_line(held, 4133, 4134), target_line(OVER, ('broadcast', NEXT)),
                               result_line(('broadcast', NEXT), 'started'), watching_line(NEXT, MEGA_END + 6300)]


def test_the_lines_when_a_restart_holds_the_lock():
    """A5. The first attempt finds the lock busy; the second, a minute later, moves."""
    rig = mega_rig()
    rig.tick_until(MEGA_DUE - 1)
    rig.lock.held = True
    rig.tick(1)
    rig.lock.held = False
    rig.tick_until(MEGA_DUE + reh.RETRY_S + 10)
    assert rig.lines()[1:6] == [moving_on_line(20, 4133, 4134), lock_busy_line(),
                                moving_on_line(80, 4133, 4134, attempt=2), target_line(OVER, ('broadcast', NEXT)),
                                result_line(('broadcast', NEXT), 'started')]


@pytest.mark.parametrize('what', ['a finished show', 'no guide entry'])
def test_one_line_for_a_replay_that_is_not_watched(what):
    """A6. Every replay leaves one line within about 10 s of its start, also one the watcher leaves
    to Kodi: so a later freeze of an unwatched replay is told apart from a watcher that never ran."""
    rig = Rig()
    if what == 'a finished show': rig.restart(PREV)
    else:
        rig.item_id = -1
        rig.restart()
    rig.tick(60)
    assert rig.lines() == [not_on_air_line(PREV) if what == 'a finished show' else NO_ENTRY_LINE]


def test_the_line_when_the_replay_no_longer_plays():
    """A7."""
    rig = mega_rig()

    def zap(): rig.player._play_epoch += 1
    rig.before_move = zap
    rig.tick_until(MEGA_DUE)
    assert rig.lines() == [watching_line(BID, MEGA_END), moving_on_line(20, 4133, 4134), gone_line()]


@pytest.mark.parametrize('debug', ['off', 'on'])
def test_debug_only_lines_stay_out_of_the_log_with_debug_off(debug):
    """A8 / A9. Kodi closes during the watch of the next show: the service's '_shutdown' line is a
    debug line, written only with the add-on's debug logging on; the always-on lines are written in
    both cases, each exactly once (notice never goes through log)."""
    rig, due = about_to_move()
    rig.debug_enable = 'true' if debug == 'on' else 'false'
    rig.kodi.behave[('broadcast', NEXT)] = 'ignore'
    rig.closing_at = due + 3
    rig.tick(1)
    always = [(m, lv) for m, lv, _ in rig.kodi_log if m.startswith(PREFIX + 'Player: replay end:')]
    assert len(always) == len(set(always)) == 3 and {lv for _, lv in always} == {FakeXbmc.LOGINFO}
    shutdown = [m for m, lv, _ in rig.kodi_log if 'FaithfulService: _shutdown, pendingShutdown = True' in m]
    assert rig.player.service.logged == [('_shutdown, pendingShutdown = True, wait = %s' % rh.WATCH_POLL, 0)]
    if debug == 'off':
        assert shutdown == [] and {lv for _, lv, _ in rig.kodi_log} == {FakeXbmc.LOGINFO}
    else:
        assert len(shutdown) == 1 and rig.kodi_log[-1] == (shutdown[0], FakeXbmc.LOGDEBUG, due + 3)


def test_an_error_line_is_written_with_debug_off_with_its_traceback():
    """A8. A failure still reaches Kodi's log without debug logging: one ERROR line with the
    traceback, after the always-on lines; nothing at DEBUG or WARNING."""
    rig, due = about_to_move()
    rig.broadcasts_fail = RuntimeError('JSON-RPC went away')
    rig.tick(1)
    errors = rig.kodi_errors()
    assert len(errors) == 1 and errors[0].startswith(PREFIX + 'Player: _moveReplayEnd, failed! JSON-RPC went away\nTraceback')
    assert {lv for _, lv, _ in rig.kodi_log} == {FakeXbmc.LOGINFO, FakeXbmc.LOGERROR}
    assert not [m for m in rig.lines() if 'Traceback' in m or '\n' in m]


@pytest.mark.parametrize('zone', ['Europe/Madrid', 'Asia/Kolkata', 'America/New_York'])
def test_the_lines_are_the_same_in_every_time_zone(tz, zone):
    """A11. Ids, seconds and UTC times only: the same lines wherever the box is."""
    tz(zone)
    rig = mega_rig()
    rig.tick_until(MEGA_DUE + 30)
    assert rig.kodi_log == [(PREFIX + 'Player: ' + line, FakeXbmc.LOGINFO, t) for line, t in zip(
        [watching_line(BID, MEGA_END), moving_on_line(20, 4133, 4134), target_line(on_air_why(MEGA_END), ('channel', CID)),
         result_line(('channel', CID), 'started')], [MEGA_OPEN + 6, MEGA_DUE, MEGA_DUE, MEGA_DUE + 0.5])]
    assert utc(MEGA_END) == '2026-11-19 17:00:00'


def test_one_attempt_writes_at_most_eight_lines():
    """The worst single attempt: the viewer in a menu first, the next show refused, the live channel
    refused, the stop: 7 lines with the lookup's (the autouse check holds every attempt to 8)."""
    rig, due = about_to_move()
    rig.kodi.behave[('broadcast', NEXT)] = rig.kodi.behave[('channel', CID)] = 'refuse'
    rig.kodi.window = False
    rig.tick_until(due + 30)
    rig.kodi.window = True
    rig.tick_until(due + 60)
    assert rig.lines() == [watching_line(BID, E), waiting_line(6053, 6054), moving_on_line(51, 6053, 6054),
                           target_line(OVER, ('broadcast', NEXT)), result_line(('broadcast', NEXT), 'not_opened'),
                           result_line(('channel', CID), 'not_opened'), last_stop_line()]
    assert len(rig.lines()) <= MAX_LINES_PER_ATTEMPT


def test_the_line_check_after_every_test_catches_what_it_should():
    """The autouse check's line rules themselves (check_lines)."""
    rig = Rig()
    good = PREFIX + 'Player: ' + NO_ENTRY_LINE
    for bad in ['Player: replay end: playing broadcast 1 now', 'Player: replay end: watching broadcast x on channel 47;'
                ' its guide entry ends 2026-11-19 17:00:00 UTC', 'Player: replay end: broadcast 1: live channel 2: started']:
        rig.kodi_log[:] = [(PREFIX + bad, FakeXbmc.LOGINFO, 0.0)]
        with pytest.raises(AssertionError, match='not an always-on line'):
            check_lines(rig)
    rig.kodi_log[:] = [(good, FakeXbmc.LOGINFO, 0.0)] * 2
    with pytest.raises(AssertionError, match='twice'):
        check_lines(rig)
    rig.kodi_log[:] = [(PREFIX + 'Player: ' + gone_line(), FakeXbmc.LOGINFO, float(t)) for t in range(9)]
    with pytest.raises(AssertionError, match='more than 8 lines'):
        check_lines(rig)
    rig.kodi_log[:] = [(PREFIX + 'Player: _chkReplayEnd, failed: see a:' + '//b', FakeXbmc.LOGERROR, 0.0)]   # URL-like
    with pytest.raises(AssertionError, match='URL-like'):
        check_lines(rig)
    rig.kodi_log[:], rig.notices[:] = [], [('Player: requestRestart, ignoring', 0.0)]
    with pytest.raises(AssertionError, match='from outside the watcher'):
        check_lines(rig)
    rig.notices[:] = []
    every_kind = [watching_line(BID, E), waiting_line(1, 2), moving_on_line(20, 4133, 4134), lock_busy_line(),
                  moving_on_line(80, 4133, 4134, 2), target_line(OVER, ('broadcast', NEXT)),
                  result_line(('broadcast', NEXT), 'refused'), result_line(('channel', CID), 'gave_up'),
                  no_live_now_line(), no_stop_now_line(), moving_on_line(20, 1, 2), not_ready_line(), flag_off_line(),
                  last_stop_line(), not_on_air_line(PREV), NO_ENTRY_LINE, moving_on_line(20, 1, 2), gone_line(),
                  target_line(NOTHING_NEXT, ('channel', CID)), target_line(on_air_why(E), ('channel', CID))]
    rig.kodi_log[:] = [(PREFIX + 'Player: ' + line, FakeXbmc.LOGINFO, float(t)) for t, line in enumerate(every_kind)]
    check_lines(rig)
    assert {kind for kind, p in ALWAYS_ON.items() for line in every_kind if p.fullmatch(line)} == set(ALWAYS_ON)
    rig.kodi_log[:] = []


def test_the_check_after_every_test_holds_the_lines():
    """The autouse check (check_rigs) runs check_lines on every rig a test built."""
    rig = Rig()
    check_rigs([rig])
    rig.kodi_log.append((PREFIX + 'Player: replay end: broadcast 1: playing channel 2', FakeXbmc.LOGINFO, rig.t))
    with pytest.raises(AssertionError, match='not an always-on line'):
        check_rigs([rig])
    del rig.kodi_log[:]


def test_the_rig_logs_at_kodis_levels(rig):
    """The rig's Kodi has Kodi 21's log levels (KODI_LOG), not the repo-root stub's Kodi 18 numbers,
    under which a WARNING (3 there) would pass logger.log's `level >= 3` gate and look always
    written. With debug logging off (the box): DEBUG, INFO and WARNING lines of Player.log are not
    written, an ERROR line is, with its traceback."""
    assert (FakeXbmc.LOGDEBUG, FakeXbmc.LOGINFO, FakeXbmc.LOGWARNING, FakeXbmc.LOGERROR) == (0, 1, 2, 3)
    assert {k: getattr(FakeXbmc, k) for k in KODI_LOG} == KODI_LOG
    for level in (FakeXbmc.LOGDEBUG, FakeXbmc.LOGINFO, FakeXbmc.LOGWARNING):
        rig.player.log('a line', level)
    assert rig.kodi_log == []
    rig.player.log('a failure', FakeXbmc.LOGERROR)
    assert len(rig.kodi_log) == 1 and rig.kodi_log[0][0].startswith(PREFIX + 'Player: a failure\n')
    assert rig.kodi_log[0][1] == FakeXbmc.LOGERROR == 3


def test_only_the_watcher_writes_always_on_lines():
    """A12. logger.notice has one caller in the add-on, Player.notice, and Player.notice only the
    watcher's methods. Its lines carry ids, seconds and times only: no exception text (an error is
    an ERROR line through log), no title, label, plot, file or path."""
    owner = _owner_map()
    lib = os.path.join(ADDON, 'resources', 'lib')
    for name in sorted(os.listdir(lib)):
        if not name.endswith('.py') or name == 'services.py': continue
        with open(os.path.join(lib, name), encoding='utf-8') as fh: text = fh.read()
        tree = ast.parse(text)
        assert not [c for c in ast.walk(tree) if isinstance(c, ast.Call) and ast.unparse(c.func).endswith('notice')], name
        assert name == 'logger.py' or 'import notice' not in text, name
    calls = [c for c in ast.walk(S_TREE) if isinstance(c, ast.Call)]
    assert [owner(c) for c in calls if ast.unparse(c.func) == 'notice'] == [('Player', 'notice')]
    mine = [c for c in calls if ast.unparse(c.func) == 'self.notice']
    assert sorted({owner(c)[1] for c in mine}) == ['_chkReplayEnd', '_lookupReplayEnd', '_moveReplayEnd', '_replayEndOpen',
                                                   '_runReplayEnd']
    allowed_names = {'facts', 'new', 'now', 'bid', 'why', 'target', 'before', 'outcome', 'replayend_helpers'}
    allowed_keys = {'broadcastid', 'channelid', 'endtime', 'pos', 'total', 'since', 'fires', 'not_opened', 1}
    for c in mine:
        names = {n.id for n in ast.walk(c) if isinstance(n, ast.Name)}
        keys = {n.slice.value if isinstance(n.slice, ast.Constant) else ast.unparse(n.slice)
                for n in ast.walk(c) if isinstance(n, ast.Subscript)}
        keys.update(a.value for n in ast.walk(c) if isinstance(n, ast.Call) and ast.unparse(n.func).endswith('.get')
                    for a in n.args if isinstance(a, ast.Constant))
        assert names - {'self'} <= allowed_names and keys <= allowed_keys, (ast.unparse(c), names, keys)
        text = ast.unparse(c)
        assert 'replay end: ' in text and not [w for w in ('label', 'title', 'plot', 'file', 'path', 'item') if "'%s'" % w in text]
    errors = [c for c in calls if ast.unparse(c.func) == 'self.log' and owner(c)[1] in NEW_METHODS]
    assert sorted(ast.unparse(c.args[1]) for c in errors) == ['xbmc.LOGERROR'] * 3          # the watcher's only log() lines


# --- Kodi's own timing after a switch (F1-F3) --------------------------------------------------

def test_kodis_pvr_flag_lag_still_counts_the_live_channel_as_started():
    """F1. Kodi refreshes Pvr.IsPlayingTv about twice a second, after the playing item: on
    2026-11-19 at 18:03:48 local Player.GetItem already reported channel 424 while the flag still
    said 'replay'. The watch's first poll falls in that gap (the flag lags 0.6 s here); the live
    channel counts as started from Player.GetItem alone. (The first build asked the flag too: its
    line said 'other' for a move that worked.)"""
    rig = mega_rig()
    rig.kodi.lag['pvr'] = 0.6
    flags = []

    def poll():
        if rig.opens():
            flags.append((rig.t, rig.kodi.playing['kind'], rig.kodi.flagged()['kind']))
    rig.hooks['GetItem'] = poll
    rig.tick_until(MEGA_DUE + 30)
    assert flags[0] == (MEGA_DUE + rh.WATCH_POLL, 'live', 'replay')          # the poll saw the lag
    assert rig.lines()[-1] == result_line(('channel', CID), 'started') and rig.kodi.playing['kind'] == 'live'


@pytest.mark.parametrize('callbacks', [0.3, 2.5], ids=['callbacks-0.3s', 'callbacks-2.5s'])
def test_stale_player_times_after_the_watchers_own_switch_are_not_a_freeze(callbacks):
    """F2. After the move's own switch to the next show the player still reports the frozen times
    for a moment (2026-11-19 17:52:00 local: one sample with the new id and the old position; here
    3 s), and the playback callbacks come later (0.3 s, or 2.5 s so a tick reads the old times
    under the old playback number). Neither is a second freeze: no second move, and the new show
    is watched."""
    end = T('16:54:31')
    rig = mega_rig(end=end)
    rig.kodi.lag.update(times=3.0, epoch=callbacks)
    rig.tick_until(MEGA_DUE)
    assert rig.kodi.playing['bid'] == NEXT and rig.kodi.times() == (4133.5, 4134.0)     # stale
    rig.tick_until(MEGA_DUE + 90)
    assert rig.opens() == [broadcast(NEXT)] and len(rig.player.moves) == 1 and len(moved_on(rig)) == 1
    assert rig.lines()[-1] == watching_line(NEXT, end + 6300) and rig.kodi.playing['bid'] == NEXT


def test_playback_ending_between_isplaying_and_gettime_fails_nothing(rig):
    """D1's other half: isPlaying() and getTime() are separate calls into Kodi, and the playback can
    end between them (isPlaying says yes, then getTime raises; Kodi writes its own ERROR line). The
    sample then reads nothing, and the tick neither fails nor parks the playback: the try stays."""
    stuck = to_the_live_edge(rig)
    rig.tick_until(stuck + 5)
    asked = []

    def answered_then_gone():
        playing = PlayerBase.isPlaying(rig.player)
        if playing and not asked: rig.kodi.playing = None             # Kodi stops right after answering
        asked.append(playing)
        return playing
    rig.player.isPlaying = answered_then_gone
    rig.tick(1)
    assert asked == [True] and rig.kodi_errors() == ['EXCEPTION: Kodi is not playing any media file']
    assert rig.errors() == [] and not rig.player._replayEndTrack.get('failed')
    assert (rig.player._replayEndTrack['pos'], rig.player._replayEndTrack['total']) == (None, None)


def test_the_rig_writes_kodis_own_error_line_for_a_getter_with_nothing_playing(rig):
    """The rig itself, as Kodi behaves (what D1 avoids and F3 relies on): with nothing playing,
    getTime / getTotalTime / getPlayingItem raise, and Kodi writes 'EXCEPTION: ...' at ERROR first
    (interfaces/legacy/Player.cpp, PythonSwig.cpp.template); isPlaying() never raises."""
    rig.restart()
    rig.kodi.playing = None
    for getter, what in (('getTime', 'any media file'), ('getTotalTime', 'any media file'), ('getPlayingItem', 'any item')):
        with pytest.raises(RuntimeError):
            getattr(rig.player, getter)()
        assert rig.kodi_log[-1] == ('EXCEPTION: Kodi is not playing %s' % what, FakeXbmc.LOGERROR, rig.t)
    assert rig.player.isPlaying() is False and len(rig.kodi_errors()) == 3


def test_a_tick_during_the_flag_lag_after_a_stop_reads_nothing_and_does_not_fail():
    """F3. The viewer stops a watched replay; Kodi's cached Pvr.IsPlayingEpgTag still says 'replay'
    for 0.6 s and the stop callback comes 0.3 s late, so the next tick still counts this playback
    with nothing playing. It asks isPlaying() first: no getTime / getTotalTime, so no ERROR line of
    Kodi's own ('EXCEPTION: ...'), and nothing fails."""
    rig = mega_rig()
    rig.tick_until(T('16:55:00'))
    rig.kodi.lag.update(pvr=0.6, epoch=0.3)
    epoch = rig.player._play_epoch
    rig.kodi.pending.append(lambda: rig.kodi.gone(stops=1))           # lands in the next tick's wait
    rig.tick(1)
    assert rig.kodi.playing is None and rig.player._play_epoch == epoch and rig.kodi.flagged()['kind'] == 'replay'
    assert rig.player._replayEndTrack['epoch'] == epoch and rig.player._replayEndTrack['pos'] is None
    rig.tick(2)
    assert rig.player._replayEndTrack is None and rig.player._play_epoch == epoch + 1
    assert rig.kodi_errors() == [] and rig.errors() == [] and rig.opens() == [] and rig.player.moves == []


# === the repair of 2026-11-19: the RESTART lock after every way out of the move ===================
# The move takes Player._restart_lock, which RESTART (imports.62) takes too, both without waiting.
# On the TV the first build's move never left its first line and never gave the lock back: every
# RESTART was refused until Kodi restarted. These tests use freezes well after the guide end
# (to_the_live_edge / about_to_move), where the old and the new wait rule act at the same time.

def _away_during_the_live_watch(rig):
    def viewer():
        if len(rig.opens()) == 2:
            rig.kodi.window = False
            rig.hooks['GetItem'] = None
    rig.hooks['GetItem'] = viewer


def _key_during_player_open(rig):
    rig.kodi.rpc_delay = 1.0
    rig.hooks['Open'] = rig.kodi.press_key


# name: (Kodi's flag, what happens, the Player.Open calls, stops, what plays afterwards, the move's JSON-RPC reads)
WAYS_OUT = {
    '01 the lock is busy':
        (None, lambda rig, due: setattr(rig.lock, 'held', True), [], 0, BID, []),
    '02 Kodi is closing':
        (None, lambda rig, due: setattr(rig, 'closing_at', rig.t), [], 0, BID, []),
    "03 the add-on's own shutdown flag":
        (None, lambda rig, due: setattr(rig, 'pending_shutdown', True), [], 0, BID, []),
    '04 it no longer plays':
        (None, lambda rig, due: setattr(rig, 'item_id', PREV), [], 0, BID, ['GetItem']),
    '05 the viewer is not ready':
        (None, lambda rig, due: rig.hooks.update(GetBroadcasts=lambda: setattr(rig.kodi, 'window', False)), [], 0, BID,
         ['GetItem', 'GetBroadcasts']),
    '06 the flag is off: stop':
        (False, lambda rig, due: None, [], 1, None, ['GetItem', 'GetItem']),
    '07 the next show starts':
        (None, lambda rig, due: None, [broadcast(NEXT)], 0, NEXT, None),
    '08a something else plays':
        (None, lambda rig, due: rig.kodi.behave.update({('broadcast', NEXT): 'other'}), [broadcast(NEXT)], 0, 'other', None),
    '08b the viewer took over':
        (None, lambda rig, due: (rig.kodi.behave.update({('broadcast', NEXT): 'gone'}), _key_during_player_open(rig)),
         [broadcast(NEXT)], 0, None, None),
    '08c it went without an error':
        (None, lambda rig, due: rig.kodi.behave.update({('broadcast', NEXT): 'gone'}), [broadcast(NEXT)], 0, None, None),
    '09 Kodi closes during the watch':
        (None, lambda rig, due: (rig.kodi.behave.update({('broadcast', NEXT): 'ignore'}), setattr(rig, 'closing_at', due + 3)),
         [broadcast(NEXT)], 0, BID, None),
    '10 next refused, live starts':
        (None, lambda rig, due: rig.kodi.behave.update({('broadcast', NEXT): 'refuse'}), [broadcast(NEXT), channel(CID)], 0,
         'live', None),
    '11 live refused, not stopping now':
        (None, lambda rig, due: (rig.kodi.behave.update({('broadcast', NEXT): 'refuse', ('channel', CID): 'ignore'}),
                                 _away_during_the_live_watch(rig)), [broadcast(NEXT), channel(CID)], 0, BID, None),
    '12 live refused, stop':
        (None, lambda rig, due: rig.kodi.behave.update({('broadcast', NEXT): 'refuse', ('channel', CID): 'refuse'}),
         [broadcast(NEXT), channel(CID)], 1, None, None),
}


def _plays(rig):
    p = rig.kodi.playing
    if p is None: return None
    if p['kind'] == 'live': return 'live' if p['cid'] == CID else 'other'
    return p['bid']


@pytest.mark.parametrize('way', sorted(WAYS_OUT))
def test_the_restart_lock_is_free_after_every_way_out(way):
    """Every way the move can end: it took the lock once, without waiting, and gave it back once
    (a lock someone else holds is left alone), and a RESTART pressed afterwards runs."""
    flag, happens, opens, stops, plays, reads = WAYS_OUT[way]
    rig, due = about_to_move(flag=flag)
    happens(rig, due)
    rig.tick(1)
    assert len(rig.player.moves) == 1 and rig.player.moves[0][0] == due
    assert rig.opens() == opens and len(rig.stops) == stops and _plays(rig) == plays
    if reads is not None: assert [c[0] for c in rig.player.jsonRPC.calls][2:] == reads
    busy = way.startswith('01')
    assert rig.lock.acquires == [False] and rig.lock.held is busy
    assert (rig.lock.taken, rig.lock.released) == ((0, 0) if busy else (1, 1))
    assert rig.violations == [] and rig.errors() == []
    rig.lock.held = False                                     # (01: whoever held it is done)
    rig.player.requestRestart({'source': 'remote'})
    assert [request for _t, request in rig.restarts] == [{'source': 'remote'}]
    assert rig.lock.held is False and rig.lock.taken == rig.lock.released


def _both(behaviour):
    def setup(rig):
        rig.kodi.behave[('broadcast', NEXT)] = rig.kodi.behave[('channel', CID)] = behaviour
    return setup


def _the_last_guide_entry(rig):
    rig.kodi.guide = _last_entry(rig.kodi.guide)


# name: (Kodi's flag, the setup)
SCENARIOS = {'next-works': (None, None), 'next-and-live-refused-stop': (None, _both('refuse')),
             'next-and-live-ignored-stop': (None, _both('ignore')), 'flag-off-stop': (False, None),
             'live': (None, _the_last_guide_entry)}


def scenario_rig(name):
    """A rig of that scenario, one tick before its move, with the move's calls traced."""
    flag, setup = SCENARIOS[name]
    rig, due = about_to_move(flag=flag)
    if setup is not None: setup(rig)
    rig.tracing = True
    return rig, due


def move_calls(name):
    """Every call one undisturbed move of that scenario makes, in order: JSON-RPC, Kodi's
    conditions, the idle time, the player, the monitor, the add-on's shutdown flag, the log. A
    call added to the move later is covered by the sweeps without a change here."""
    rig, _due = scenario_rig(name)
    rig.tick(1)
    assert len(rig.traces) == 1 and rig.traces[0][:2] == ['abortRequested', 'isPendingShutdown']
    return rig.traces[0]


def _raising(error):
    """An action for Rig.at_call: that call fails with `error`."""
    def fail():
        raise error
    return fail


def _closing(rig, seen):
    """An action for Rig.at_call: Kodi starts to close during that call; `seen` gets the time and
    what had gone out to Kodi by then."""
    def close():
        rig.closing_at = rig.t
        seen.update(t=rig.t, opens=len(rig.opens()), stops=len(rig.stops))
    return close


def test_the_sweeps_cover_the_whole_move():
    """What the two sweeps below walk through: every kind of call, the watch included. imports.70
    (the repair, step 2): the move's lines are 'notice' calls (always written); the live channel's
    start is read from Player.GetItem alone, without the cached Pvr.IsPlayingTv (one call fewer per
    poll of a live watch: 19 -> 18 and 305 -> 285 calls). After the review (the fix stage): the last
    check before acting reads what plays (frozen(): the PVR flag and Player.GetItem, two calls more
    in every scenario that acts: 9 -> 11, 18 -> 20, 285 -> 287, 25 -> 27, 19 -> 21)."""
    seen = {name: move_calls(name) for name in SCENARIOS}
    wait = 'waitForAbort'                                         # the watch's poll, through the real Service._shutdown
    assert set(seen['next-works']) == {'abortRequested', 'isPendingShutdown', 'Pvr.IsPlayingEpgTag', 'GetItem',
                                       'getPlayingItem', 'GetBroadcasts', reh.ACT_WINDOW, 'getIdle', 'notice', 'Open',
                                       wait, 'isPlaying', 'isBusyDialog'}
    assert seen['next-works'].count('Open') == 1 and seen['live'].count('Open') == 1
    assert not [name for name in SCENARIOS if 'Pvr.IsPlayingTv' in seen[name]] and seen['flag-off-stop'][-1] == 'stop'
    assert not [name for name in SCENARIOS if 'log' in seen[name]]   # a move that works writes no debug line
    assert seen['next-and-live-refused-stop'].count('Open') == 2 and seen['next-and-live-refused-stop'][-1] == 'stop'
    polls = int(rh.WATCH_GRACE / rh.WATCH_POLL)
    assert seen['next-and-live-ignored-stop'].count(wait) == 2 * polls
    assert [len(seen[name]) for name in sorted(SCENARIOS)] == [11, 20, 287, 27, 21]


@pytest.mark.parametrize('failure', [RuntimeError, KeyError, ValueError])
@pytest.mark.parametrize('scenario', sorted(SCENARIOS))
def test_the_restart_lock_is_free_after_a_failure_at_any_call(scenario, failure):
    """Each call of the move fails in turn (three kinds of exception): the lock is free afterwards,
    at most 2 opens and 1 stop were tried, at most one ERROR line was written; and the second
    attempt a minute later leaves nothing behind either."""
    calls = move_calls(scenario)
    for k in range(1, len(calls) + 1):
        rig, due = scenario_rig(scenario)
        rig.at_call = (k, _raising(failure('call %d (%s) failed' % (k, calls[k - 1]))))
        rig.tick(1)
        what = (scenario, k, calls[k - 1])
        assert rig.at_call is None and len(rig.player.moves) == 1, what
        assert rig.lock.held is False and (rig.lock.taken, rig.lock.released) == (1, 1), what
        assert len(rig.opens()) <= 2 and len(rig.stops) <= 1 and len(rig.errors()) <= 1, what
        assert all(m.startswith('_moveReplayEnd, failed!') for m in rig.errors()) and rig.violations == [], what
        # reported once, except the flag read, whose failure counts as "flag off" (_replayEndAutoplay)
        if calls[k - 1] != 'getPlayingItem': assert len(rig.errors()) == 1, what
        rig.tick(int(reh.RETRY_S) + 30)
        assert rig.lock.held is False and rig.lock.taken == rig.lock.released and rig.violations == [], what
        assert len(rig.player.moves) <= 2 and len(rig.opens()) <= 4 and len(rig.stops) <= 2 and len(rig.errors()) <= 1, what


@pytest.mark.parametrize('scenario', sorted(SCENARIOS))
def test_the_restart_lock_is_free_when_kodi_ends_the_thread_at_any_call(scenario):
    """At shutdown Kodi ends an add-on's threads by raising SystemExit in them, wherever they are
    (xbmc/interfaces/python/PythonInvoker.cpp: 'Raise a SystemExit exception in python threads').
    That is no Exception, so the move's `except` never sees it: the `finally` gives the lock back."""
    calls = move_calls(scenario)
    for k in range(1, len(calls) + 1):
        rig, due = scenario_rig(scenario)
        rig.at_call = (k, _raising(SystemExit(0)))
        with pytest.raises(SystemExit):
            rig.tick(1)
        what = (scenario, k, calls[k - 1])
        assert rig.lock.held is False and (rig.lock.taken, rig.lock.released) == (1, 1), what
        assert rig.errors() == [] and len(rig.opens()) <= 2 and len(rig.stops) <= 1, what


@pytest.mark.parametrize('scenario', sorted(SCENARIOS))
def test_kodi_closing_at_any_call_ends_the_move_within_one_poll(scenario):
    """Kodi starts to close during each call of the move in turn: the move ends without another
    wait (the clock moves at most one WATCH_POLL further), the lock is free, nothing fails. When
    Kodi answers the opens (it takes them, or lets them pass), at most one more open or stop goes
    out to the closing Kodi: the watch after it sees the shutdown. When Kodi refuses the opens
    outright there is no watch, so no wait and no shutdown check between the steps: the usual
    bounds (2 opens, 1 stop) hold."""
    calls = move_calls(scenario)
    answered = scenario != 'next-and-live-refused-stop'
    for k in range(1, len(calls) + 1):
        rig, due = scenario_rig(scenario)
        closing = {}
        rig.at_call = (k, _closing(rig, closing))
        rig.tick(1)
        what = (scenario, k, calls[k - 1])
        assert closing and rig.t - closing['t'] <= rh.WATCH_POLL, what
        assert rig.lock.held is False and (rig.lock.taken, rig.lock.released) == (1, 1), what
        assert rig.errors() == [] and rig.violations == [], what
        after = len(rig.opens()) - closing['opens'] + len(rig.stops) - closing['stops']
        assert len(rig.opens()) <= 2 and len(rig.stops) <= 1 and after <= (1 if answered else 3), what
        if calls[k - 1] == 'abortRequested' and k == 1: assert rig.opens() == [] and rig.stops == [], what
        waits = rig.player.monitor.waits
        assert all(w == rh.WATCH_POLL for w in waits), what


def test_restart_works_after_a_move(rig):
    """The real requestRestart (imports.62) takes the same lock after a whole move."""
    stuck = to_the_live_edge(rig)
    rig.tick_until(stuck + reh.STALL_S + 5)
    assert rig.opens() == [broadcast(NEXT)] and rig.kodi.playing['bid'] == NEXT
    rig.player.requestRestart({'source': 'remote'})
    assert rig.restarts == [(rig.t, {'source': 'remote'})]
    assert rig.lock.acquires == [False, False] and (rig.lock.taken, rig.lock.released) == (2, 2) and rig.lock.held is False
    assert rig.errors() == []


def test_restart_pressed_during_a_move_is_ignored_and_works_afterwards(rig):
    """While the move watches its open it holds the lock: a RESTART pressed then is dropped, as a
    second restart is (no note); once the move is over the next press runs."""
    stuck = to_the_live_edge(rig)
    rig.kodi.behave[('broadcast', NEXT)] = 'ignore'           # the watch runs its 10 s
    during = []

    def press_restart():
        if rig.opens() and not during:
            rig.player.requestRestart({'source': 'remote'})
            during.append((rig.lock.held, list(rig.restarts), rig.lock.taken, rig.lock.released))
    rig.hooks['GetItem'] = press_restart
    rig.tick_until(stuck + reh.STALL_S)
    assert during == [(True, [], 1, 0)]                       # the move still held it; the press did nothing
    assert rig.opens() == [broadcast(NEXT), channel(CID)] and rig.kodi.playing['kind'] == 'live'
    assert rig.lock.held is False and (rig.lock.taken, rig.lock.released) == (1, 1)
    rig.player.requestRestart({'source': 'remote'})
    assert [request for _t, request in rig.restarts] == [{'source': 'remote'}]
    assert rig.lock.acquires == [False, False, False] and (rig.lock.taken, rig.lock.released) == (2, 2)
    assert rig.errors() == []


def test_a_move_due_during_a_restart_waits_for_the_second_attempt(rig):
    """A RESTART is running (it holds the lock) when the move comes due: that attempt does nothing
    and gives nothing back; the second one, a minute later, moves."""
    stuck = to_the_live_edge(rig)
    due = stuck + reh.STALL_S
    rig.tick_until(due - 2)
    rig.during_restart = lambda: rig.tick_until(due + 5)      # the idle tick goes on while the restart runs
    rig.player.requestRestart({'source': 'popup'})
    assert len(rig.restarts) == 1 and rig.t == due + 5
    assert [t for t, _facts, _epoch in rig.player.moves] == [due] and rig.opens() == [] and rig.stops == []
    assert rig.lock.acquires == [False, False] and (rig.lock.taken, rig.lock.released) == (1, 1) and rig.lock.held is False
    rig.during_restart = None
    rig.tick_until(due + reh.RETRY_S + 5)
    assert rig.open_times() == [due + reh.RETRY_S] and rig.opens() == [broadcast(NEXT)]
    assert rig.lock.acquires == [False, False, False] and (rig.lock.taken, rig.lock.released) == (2, 2)
    assert rig.errors() == []


def test_the_rigs_lock_refuses_a_wait_for_the_restart(rig):
    """The rig's own lock: taking it with a wait while it is held (what a blocking acquire in the
    move or in requestRestart would do) is a ForeverWait, recorded like a refused Kodi wait."""
    rig.lock.held = True
    assert rig.lock.acquire(False) is False
    for blocking in ((), (True,), (True, 5)):
        with pytest.raises(ForeverWait):
            rig.lock.acquire(*blocking)
    assert rig.lock.acquires == [False, True, True, True] and len(rig.violations) == 3
    assert kodi_faithful.provoked() == rig.violations and not rig.lock.leaked()
    del rig.violations[:]
    rig.lock.held = False
    assert rig.lock.acquire() is True and rig.lock.leaked() and rig.lock_leaked()
    rig.lock.release()
    assert not rig.lock.leaked() and (rig.lock.taken, rig.lock.released) == (1, 1)
    with pytest.raises(RuntimeError):
        rig.lock.release()


def test_the_check_after_every_test_catches_a_forever_wait_and_a_kept_lock():
    """The autouse check itself: a recorded wait Kodi would never end, or a RESTART lock the code
    under test took and kept, fails it; a lock the test holds itself does not."""
    rig = Rig(t=E - 100)
    check_rigs([rig])
    rig.violations.append('waitForAbort(0)')
    with pytest.raises(AssertionError, match='Kodi would wait forever'):
        check_rigs([rig])
    del rig.violations[:]
    rig.lock.acquire(False)
    with pytest.raises(AssertionError, match='not given back'):
        check_rigs([rig])
    rig.lock.release()
    rig.lock.held = True                                      # the test's own hold: fine
    check_rigs([rig])
    rig.lock.held = False
    real = Rig(t=E - 100, lock=threading.Lock())
    real.lock.acquire()
    with pytest.raises(AssertionError, match='not given back'):
        check_rigs([real])
    real.lock.release()
    check_rigs([real])
    assert RIGS == [rig, real]                                # every rig a test builds is on the list


def _compiled_globals(rig):
    """What the compiled methods see as their module globals (BUILTIN, clock_helpers, ...)."""
    return type(rig.player).__dict__['_chkReplayEnd'].__globals__


# Every call the compiled methods can make into the rig, by the name it is traced under (a
# condition read under 'getCondVisibility'; the clock, the lock and the restart recorder are
# counted but not traced): name -> a function of the rig that makes one such call.
RIG_CALLS = {
    'GetItem':           lambda rig: rig.player.jsonRPC.getPlayerItemRef,
    'GetChannels':       lambda rig: rig.player.jsonRPC.getPVRChannelsNow,
    'GetBroadcasts':     lambda rig: lambda: rig.player.jsonRPC.getPVRBroadcastTimes(CID),
    'Open':              lambda rig: lambda: rig.player.jsonRPC.playerOpen(broadcast(NEXT)),
    'getCondVisibility': lambda rig: lambda: rig.xbmc.getCondVisibility(reh.ACT_WINDOW),
    'getIdle':           lambda rig: _compiled_globals(rig)['BUILTIN'].getIdle,
    'isBusyDialog':      lambda rig: _compiled_globals(rig)['BUILTIN'].isBusyDialog,
    'log':               lambda rig: lambda: rig.player.log('a line'),
    'notice':            lambda rig: lambda: rig.player.notice(NO_ENTRY_LINE),
    'isPlaying':         lambda rig: rig.player.isPlaying,
    'getTime':           lambda rig: rig.player.getTime,
    'getTotalTime':      lambda rig: rig.player.getTotalTime,
    'getPlayingItem':    lambda rig: rig.player.getPlayingItem,
    'stop':              lambda rig: rig.player.stop,
    'isPendingShutdown': lambda rig: rig.pending,
    'abortRequested':    lambda rig: rig.player.monitor.abortRequested,
    'clock':             lambda rig: _compiled_globals(rig)['clock_helpers'].now,
    'lock':              lambda rig: lambda: rig.player._restart_lock.acquire(False),
    'restart':           lambda rig: lambda: rig.player._runRestart({'source': 'remote'}),
}


def test_every_call_into_the_rig_counts_against_a_loop_that_never_waits():
    """The rig's standstill guard (kodi_faithful.Standstill) sees every call the compiled methods
    can make into the rig: each one, made more than SPIN_CAP times in a row at one instant of the
    rig's clock, is refused on the last (ForeverWait, recorded), and time passing starts the count
    again. So a loop whose wait the rig never sees fails within SPIN_CAP calls, whatever it calls,
    where it would record calls for ever (2026-11-19 23:25: about 45 MB a second, until this
    machine froze). The calls a move makes (the sweeps' traces) are all on the list."""
    cap = kodi_faithful.SPIN_CAP
    assert 100 <= cap <= 10000                                # this test loops that often
    traced = set()
    for scenario in SCENARIOS: traced.update(move_calls(scenario))
    conditions = {reh.ACT_WINDOW, reh.BUSY, 'Pvr.IsPlayingEpgTag', 'Pvr.IsPlayingTv'}
    assert traced - conditions - {'waitForAbort'} <= set(RIG_CALLS)       # waitForAbort: the contract tests
    for name in sorted(RIG_CALLS):
        rig = Rig(t=E - 100)
        rig.restart()
        call = RIG_CALLS[name](rig)
        rig.t += 0.25                                         # a new instant, reached without a call
        for _ in range(cap): call()
        rig.t += 0.25                                         # time passed: the count starts again
        for _ in range(cap): call()
        assert rig.violations == [] and rig.standstill.count == cap, name
        with pytest.raises(ForeverWait, match='calls in a row while no time passed'):
            call()
        assert len(rig.violations) == 1 and kodi_faithful.provoked() == rig.violations, name
        del rig.violations[:]
        if rig.lock.held: rig.lock.release()
        del rig.notices[:], rig.kodi_log[:]                   # 'notice' wrote the same line 2 * SPIN_CAP times


class _WayOut(BaseException):
    """A test's own way out of a loop the standstill guard should have ended (after WAY_OUT turns,
    whatever SPIN_CAP says)."""


WAY_OUT = 20000


def test_a_watch_whose_waits_let_no_time_pass_fails_within_spin_cap_calls():
    """2026-11-19 23:25: a deliberately broken stand-in, a service whose waits let no time pass,
    made the move's watch turn for ever on the rig's clock while the rig recorded every call: about
    45 MB a second, until RAM and swap were full and this machine froze. The same loop through the
    real move: Kodi's clock stands still in every wait (the monitor moves it no more) and the next
    show never starts. The standstill guard ends it within SPIN_CAP calls into the rig: the move
    fails loudly (ForeverWait passes its `except Exception`), its `finally` gives the RESTART lock
    back, the refusal is on the record, and only a few hundred calls were recorded."""
    cap = kodi_faithful.SPIN_CAP
    assert 100 <= cap <= 10000
    rig, due = about_to_move()
    rig.kodi.behave[('broadcast', NEXT)] = 'ignore'
    rig.player.monitor._advance = lambda seconds: None
    polls = []

    def way_out():
        polls.append(rig.t)
        if len(polls) > WAY_OUT: raise _WayOut('the standstill guard let the watch turn on')
    rig.hooks['GetItem'] = way_out
    with pytest.raises(ForeverWait, match='calls in a row while no time passed'):
        rig.tick(1)
    assert rig.t == due and len(rig.player.moves) == 1 and rig.opens() == [broadcast(NEXT)] and rig.stops == []
    assert 0 < len(polls) < cap // 4 and len(rig.player.jsonRPC.calls) < cap // 2 and len(rig.xbmc.reads) < cap // 2
    assert rig.lock.held is False and (rig.lock.taken, rig.lock.released) == (1, 1) and rig.errors() == []
    assert len(rig.violations) == 1 and kodi_faithful.provoked() == rig.violations
    del rig.violations[:]


# How long a real thread gets to finish work that takes it a few ms (the box may be busy).
REAL_THREAD_S = 5.0


@pytest.fixture
def kodi_gate():
    """For the test with real threads: the gate a wait Kodi would never end stands at
    (FaithfulMonitor). It is opened afterwards (Kodi closing), so a thread that got stuck there, as
    on the TV, ends with its test."""
    gate = threading.Event()
    yield gate
    gate.set()


def test_the_2026_11_19_hang_is_gone(kodi_gate):
    """The TV failure, with what it had: a real thread through the add-on's own @threadit, a real
    threading.Lock, and a Kodi that really blocks on a wait it never ends. The first build's move
    stood in service._shutdown(0) with the RESTART lock held (this test then fails after
    REAL_THREAD_S: thread alive, lock held, the RESTART press refused). Repaired: the move ends,
    the next show plays, the lock is free and a RESTART pressed afterwards runs."""
    rig, due = about_to_move(threads=True, lock=threading.Lock(), gate=kodi_gate)
    try:
        rig.tick(1)                                           # the tick starts the move on its own thread
        assert rig.t >= due and len(rig.threads) == 1 and rig.threads[0].name.startswith('threadit.')
        rig.threads[0].join(REAL_THREAD_S)
        assert not rig.threads[0].is_alive(), 'the move still waits: %s' % rig.violations
        assert not rig.lock.locked() and rig.violations == []
        assert rig.opens() == [broadcast(NEXT)] and rig.kodi.playing['bid'] == NEXT and rig.errors() == []
        rig.player.requestRestart({'source': 'remote'})
        assert len(rig.threads) == 2
        rig.threads[1].join(REAL_THREAD_S)
        assert not rig.threads[1].is_alive()
        assert [request for _t, request in rig.restarts] == [{'source': 'remote'}] and not rig.lock.locked()
    finally:
        kodi_gate.set()
        for thread in rig.threads: thread.join(REAL_THREAD_S)


def test_the_rig_runs_the_real_shutdown_over_the_faithful_monitor(rig):
    """The wiring: Player.service's _shutdown and _aborted are the code of services.py, and its
    monitor has Kodi's rules. A fake service that answers at once on any wait must not come back:
    it let the wait Kodi never ends pass every test."""
    service = rig.player.service
    for name in ('_shutdown', '_aborted'):
        code = getattr(service, name).__func__.__code__
        assert (os.path.basename(code.co_filename), code.co_name) == ('services.py', name)
    assert isinstance(service, FaithfulService) and isinstance(service.monitor, FaithfulMonitor)
    assert rig.player.monitor is service.monitor and service.monitor.standstill is rig.standstill
    t0 = rig.t
    assert service._shutdown(rh.WATCH_POLL) is False and rig.t == t0 + rh.WATCH_POLL
    assert service.monitor.waits == [rh.WATCH_POLL]
    rig.closing_at = rig.t
    assert service._aborted() is True and service._shutdown(rh.WATCH_POLL) is True and rig.t == t0 + rh.WATCH_POLL
    for name in ('_chkReplayEnd', '_replayEndSample', '_runReplayEnd', '_replayEndOpen', '_replayEndAutoplay'):
        code = type(rig.player).__dict__[name].__code__       # the Player's methods: the real ones, at their real lines
        assert (os.path.basename(code.co_filename), code.co_name, code.co_firstlineno) == \
            ('services.py', name, _method('Player', name).lineno)


# === after the review of the repair (the fix stage) ===============================================
# Each test below passes on this code and fails when the part it names is undone or changed as its
# docstring says (each such break was run alone on a scratch copy).

@pytest.mark.parametrize('flag, during', [(None, 'getPlayingItem'), (None, 'GetBroadcasts'), (None, reh.ACT_WINDOW),
                                          (None, 'getIdle'), (False, 'getPlayingItem'), (False, reh.ACT_WINDOW),
                                          (False, 'getIdle')],
                         ids=['flag-read', 'guide-read', 'window-read', 'idle-read', 'flag-off-flag-read',
                              'flag-off-window-read', 'flag-off-idle-read'])
def test_a_zap_without_a_key_whose_callbacks_are_late_is_left_alone(rig, flag, during):
    """Kodi swaps its playing item at once, but the playback number moves only when the service's
    main thread runs the callbacks (between its 100 ms wait slices; 0.1 s here), and its PVR flags
    lag too (0.5 s). A zap made without a key press (a phone app, JSON-RPC) during the move's reads
    before acting: the last check reads what plays (frozen(): Player.GetItem), so the viewer's
    channel is neither opened over nor stopped (a flag read from the new item says off). Without
    that read the move trusted the playback number there and did one or the other."""
    stuck = to_the_live_edge(rig, flag=flag)
    rig.tick_until(stuck + reh.STALL_S - 1)
    rig.kodi.lag.update(pvr=0.5, epoch=0.1)

    def zap():
        if rig.player.moves:
            rig.hooks[during] = None
            rig.kodi.play_live(OTHER_CID)
    rig.hooks[during] = zap
    rig.tick(1)
    assert len(rig.player.moves) == 1 and rig.opens() == [] and rig.stops == []
    assert rig.kodi.playing['cid'] == OTHER_CID and rig.lines()[-1] == gone_line()


@pytest.mark.parametrize('before_end', [3600, 5000])
def test_a_freeze_long_before_the_guide_end_moves_to_the_live_channel(before_end):
    """The user's rule of 2026-11-19 has no guide-end gate, also far from the guide end (a Mega-type
    stream is one episode of a guide entry of several hours): a stream that stops an hour (or 83
    minutes, just after the show began) before its guide end moves to the live channel 20 s after
    it froze. A gate put back into the move after its own clock read passed every other test."""
    stops_at = E - before_end
    rig = Rig(t=stops_at - 150)
    rig.restart()
    rig.tick(8)
    rig.kodi.seek(rig.kodi.times()[1] - 1)                    # the live edge of the growing window ...
    rig.kodi.playing['w1'] = stops_at                         # ... which stops growing there
    rig.tick_until(stops_at + reh.STALL_S + 5)
    assert rig.open_times() == [stops_at + reh.STALL_S] and rig.opens() == [channel(CID)]
    assert rig.kodi.playing['kind'] == 'live' and rig.lines()[-2] == target_line(on_air_why(E), ('channel', CID))


def test_the_target_line_is_in_kodis_log_before_player_open_is_sent():
    """Code-design C: the target line is written before Player.Open, so a move that blocks there
    (Kodi's PIN or 'play recording?' question) still leaves its decision in the log. Written after
    Player.Open returned, the lines kept their order and times and every other test passed."""
    rig = mega_rig()
    seen = []
    rig.hooks['Open'] = lambda: seen.append(list(rig.lines()))
    rig.tick_until(MEGA_DUE + 30)
    assert len(seen) == 1 and seen[0][-1] == target_line(on_air_why(MEGA_END), ('channel', CID))


def test_a_replay_carrying_the_channels_id_is_not_the_live_channel_started():
    """D2: the live channel counts as started from Player.GetItem's type 'channel' and its id. An
    item of another type that carries the same number (guide entries and channels have ids of their
    own, so this is rare) is something else: 'other'. Without the type check it read 'started'."""
    rig, _due = about_to_move()
    rig.kodi.guide = _last_entry(rig.kodi.guide)                # no next entry: the target is live
    rig.kodi.behave[('channel', CID)] = 'ignore'
    rig.hooks['Open'] = lambda: setattr(rig, 'item_id', CID)    # the replay now reports the channel's id
    rig.tick(1)
    assert rig.opens() == [channel(CID)] and rig.lines()[-1] == result_line(('channel', CID), 'other')


def test_a_live_channel_carrying_the_next_shows_id_is_not_the_next_show_started():
    """The next show counts as started only while Kodi plays a guide entry (Pvr.IsPlayingEpgTag)
    with that id; a live channel whose id is the same number is something else: 'other'. Without
    the flag only a call count noticed."""
    rig, _due = about_to_move()
    rig.kodi.behave[('broadcast', NEXT)] = 'ignore'
    rig.hooks['Open'] = lambda: rig.kodi.pending.append(lambda: rig.kodi.play_live(NEXT))
    rig.tick(1)
    assert rig.opens() == [broadcast(NEXT)] and rig.lines()[-1] == result_line(('broadcast', NEXT), 'other')


def test_a_zap_whose_callbacks_are_late_is_not_stopped_by_the_last_resort_stop():
    """The last-resort stop re-reads what plays first (frozen(): Kodi's PVR flag and Player.GetItem).
    A zap from a remote app (no key) right after the live channel's watch said 'refused', whose
    callbacks Kodi has not delivered yet (the playback number still names the frozen replay): not
    stopped. Before, only a call count pinned that re-check."""
    rig, _due = about_to_move()
    rig.kodi.behave[('broadcast', NEXT)] = 'refuse'
    rig.kodi.behave[('channel', CID)] = 'ignore'
    real_notice = rig.player.notice

    def notice(msg):
        out = real_notice(msg)
        if msg == result_line(('channel', CID), 'refused'):
            rig.kodi.lag.update(pvr=0.5, epoch=5.0)
            rig.kodi.play_live(OTHER_CID)
        return out
    rig.player.notice = notice
    rig.tick(1)
    assert rig.opens() == [broadcast(NEXT), channel(CID)] and rig.stops == [] and rig.kodi.playing['cid'] == OTHER_CID


@pytest.mark.parametrize('target', ['the next show', 'the live channel'])
@pytest.mark.parametrize('fails_after, outcome', [(0.3, 'gave_up'), (2.0, 'started')],
                         ids=['fails-before-the-first-poll', 'fails-after-it'])
def test_kodi_21s_failed_start_ends_the_move_with_nothing_playing(target, fails_after, outcome):
    """Kodi 21's failed start (FORK_NOTES, Kodi facts and Limits; its own later plan): the target
    shows as the playing item first, then its stream fails to open, Kodi shows "Playback failed"
    and Python gets a stop, with no key and no onPlayBackError. When it fails within playerOpen's
    RPC_Delay pause and the first 0.5 s poll the watch reads the stop ('gave_up'); later it has
    already read 'started'. Either way the move ends there, as documented: no fallback, no stop of
    its own, nothing plays, the RESTART lock is free, and no second attempt (nothing plays)."""
    rig, _due = about_to_move()
    if target == 'the live channel': rig.kodi.guide = _last_entry(rig.kodi.guide)
    kind, value = ('broadcast', NEXT) if target == 'the next show' else ('channel', CID)
    rig.kodi.behave[(kind, value)] = 'failed_start'
    rig.kodi.rpc_delay, rig.kodi.fail_after = 1.0, fails_after          # RPC_Delay's default: 1 s
    rig.tick(1)
    assert rig.opens() == [broadcast(value) if kind == 'broadcast' else channel(value)]
    assert rig.lines()[-1] == result_line((kind, value), outcome)
    rig.tick(int(reh.RETRY_S) + 30)
    assert len(rig.opens()) == 1 and rig.stops == [] and rig.kodi.playing is None and len(rig.player.moves) == 1
    assert not rig.lock.locked() and rig.errors() == []


# --- the tick during the move's waits ----------------------------------------------------------------
# In Kodi, Monitor._chkIdle keeps ticking on its own thread while the move (@threadit) waits; the rig
# runs the move inline, in the tick that starts it. interleave() makes the move's waits run the tick
# at every whole second they cross, as the idle thread would, while the move still holds the lock:
# every way out and every sweep scenario ends as it does inline.

def interleave(rig):
    """The move's waits (the faithful monitor's) run the tick at each whole second they cross, one
    level deep. -> {'ticks': how many ran so}."""
    monitor = rig.player.service.monitor
    advance, state = monitor._advance, {'depth': 0, 'ticks': 0}

    def advance_with_ticks(seconds):
        end = rig.t + seconds
        while math.floor(rig.t) + 1 <= end:
            advance(math.floor(rig.t) + 1 - rig.t)
            if state['depth'] == 0:
                state['depth'] += 1
                state['ticks'] += 1
                try: rig.player._chkReplayEnd()
                finally: state['depth'] -= 1
        if end > rig.t: advance(end - rig.t)
    monitor._advance = advance_with_ticks
    return state


def _ended(rig):
    """How a move ended: the opens, the stops, what plays, the lock, refused waits, ERROR lines."""
    return (rig.opens(), list(rig.stops), _plays(rig), rig.lock.held, rig.lock.taken == rig.lock.released,
            list(rig.violations), rig.errors())


def _way_out_rig(way, nested):
    flag, happens, _opens, _stops, _plays_after, _reads = WAYS_OUT[way]
    rig, due = about_to_move(flag=flag)
    happens(rig, due)
    state = interleave(rig) if nested else None
    rig.tick(1)
    rig.tick(int(reh.RETRY_S) + 30)                               # and the second attempt's minute
    return rig, state


@pytest.mark.parametrize('way', sorted(WAYS_OUT))
def test_every_way_out_ends_the_same_with_ticks_during_the_move(way):
    inline, _ = _way_out_rig(way, False)
    nested, state = _way_out_rig(way, True)
    assert _ended(nested) == _ended(inline), (way, state)


def _scenario_rig_nested(name, nested):
    flag, setup = SCENARIOS[name]
    rig, _due = about_to_move(flag=flag)
    if setup is not None: setup(rig)
    state = interleave(rig) if nested else None
    rig.tick(1)
    rig.tick(int(reh.RETRY_S) + 30)
    return rig, state


@pytest.mark.parametrize('name', sorted(SCENARIOS))
def test_every_sweep_scenario_ends_the_same_with_ticks_during_the_move(name):
    inline, _ = _scenario_rig_nested(name, False)
    nested, state = _scenario_rig_nested(name, True)
    assert _ended(nested) == _ended(inline), (name, state)
    if name == 'next-and-live-ignored-stop': assert state['ticks'] >= 15    # two 10 s watches: the ticks ran


def test_the_mega_case_and_the_2026_11_15_case_end_the_same_with_ticks_during_the_move():
    inline, nested = mega_rig(), mega_rig()
    state = interleave(nested)
    for built in (inline, nested): built.tick_until(MEGA_DUE + 120)
    assert _ended(nested) == _ended(inline) and nested.open_times() == inline.open_times() == [MEGA_DUE], state
    assert nested.lines() == inline.lines()
    inline, nested = Rig(), Rig()
    state = interleave(nested)
    for built in (inline, nested):
        t_open = built.restart()
        built.tick_until(t_open + 6053 + 300)
    assert _ended(nested) == _ended(inline) and nested.lines() == inline.lines(), state


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


def test_the_move_asks_whether_kodi_is_closing_without_waiting():
    """The repair of 2026-11-19: the move's first step is Service._aborted(), which never waits.
    The first build's _shutdown(0) is gone, and the only wait the watcher makes is the watch's
    WATCH_POLL (through the real Service._shutdown); it never calls Kodi's waitForAbort itself."""
    steps = [s for s in _method('Player', '_runReplayEnd').body if not isinstance(s, ast.FunctionDef)]
    assert isinstance(steps[0], ast.Assign)                       # bid, cid = ...: no Kodi call before the question
    assert ast.unparse(steps[1]) == 'if self.service._aborted():\n    return'
    for name in NEW_METHODS:
        calls = [ast.unparse(c) for c in ast.walk(_method('Player', name)) if isinstance(c, ast.Call)]
        waits = [c for c in calls if '_shutdown' in c.split('(')[0] or 'waitForAbort' in c.split('(')[0]]
        assert waits == (['self.service._shutdown(restart_helpers.WATCH_POLL)'] if name == '_replayEndOpen' else []), name
        asked = [c for c in calls if c == 'self.service._aborted()']
        assert len(asked) == (1 if name == '_runReplayEnd' else 0), name


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
        ['BUILTIN.getIdle', 'self.getTime', 'self.getTotalTime', 'self.isPlaying', 'xbmc.getCondVisibility',
         'xbmc.getCondVisibility']


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
    changed = [('Player', name) for name in NEW_METHODS] + [('Service', '_shutdown'), ('Service', '_aborted')]
    for cls, name in changed:
        node = _method(cls, name)
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
