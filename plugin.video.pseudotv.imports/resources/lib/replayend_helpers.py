#   Copyright (C) 2025 Lunatixz
#
# This file is part of PseudoTV Live.
#
# PseudoTV Live is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# PseudoTV Live is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with PseudoTV Live.  If not, see <http://www.gnu.org/licenses/>.

# -*- coding: utf-8 -*-

# madteevee (imports fork, imports.70): LiveTV replays frozen at their end move on.
#
# Import-light pure module (same pattern as restart_helpers.py /
# livesubs_helpers.py): it imports only clock_helpers (the add-on's time
# conversions) and restart_helpers (the Custom-plot check), does no I/O and
# reads no clock (callers pass `now`), Python 3.8 syntax. services.py reads
# Kodi and passes the facts in; every decision is made here and tested
# without Kodi stubs.
#
# Why: Movistar+ serves a show that is still on air as a live stream with its
# own window, which need not match the guide entry (2026-11-15: an end about
# ten minutes after the guide end; 2026-11-19: a 69-minute stream inside a
# 3 h 05 guide entry, ending a minute before the guide end, and one declared
# an hour past its guide end); a finished show comes as a recording.
# inputstream.adaptive never ends the last period of a live stream ("Assume
# that if the period is the last, it never ends"), even after the provider
# turned it into a recording, so Kodi never sees the end: the player stalls
# on the last second ("stream stalled") and Kodi's "Play next programme
# automatically", which runs only on a natural end, never fires. 2026-11-15:
# a RESTART replay froze at 1:40:53 of 1:40:54 for 1 h 26 min.
#
# The watcher (services.Player._chkReplayEnd, on Monitor._chkIdle's tick):
#   * arm, once per playback, ARM_DELAY_S after a replay started: is its show
#     the one on air (programme)? A finished show's replay is a recording
#     that ends by itself: left to Kodi;
#   * detect (stall_step): position and length stood still for STALL_S at
#     the end of the stream, nothing paused or seeking. The guide's end time
#     plays no part (the user's rule of 2026-11-19: 20 s after it freezes);
#     the move waits for full-screen video and no recent key press (ready);
#   * move on as Kodi does at a natural end: the replay's own Kodi flag
#     (AUTOPLAY_PROPERTY, autoplay_on) says whether Kodi would go on; then
#     the live channel while the guide still lists the show as on air
#     (on_air), else the next guide entry (next_broadcast: Kodi's
#     GetNextAutoplayItem rule) or the live channel; a stop when the flag is
#     off or nothing opens (after_open picks the fallback).
import clock_helpers
import restart_helpers

# s after a replay started before its one lookup runs: onPlayBackStarted /
# onAVStarted have settled and quick zaps have passed by.
ARM_DELAY_S = 5.0
# s that the position AND the length must stand still: above normal buffering
# (Kodi's caching timer is 5 s) and a manifest refresh at the live edge, short
# enough that the frozen picture doesn't linger.
STALL_S     = 20.0
# s: a paused or stalled clock reads the same value; normal play moves about
# 1 s per tick.
POS_EPS     = 0.25
# s: "at its end" means length - position <= this (observed: 1 s;
# inputstream.adaptive stops at the start of the last segment).
END_SLACK_S = 10.0
# s: ticks come 1-1.5 s apart. A longer gap (a busy box, a suspend, a clock
# change) or a clock going back starts the count again.
TICK_GAP_S  = 5.0
# s without a key press before the add-on acts (BUILTIN.getIdle: whole s).
QUIET_S     = 10
# s: a position more than this before the end (a seek back) starts a new pass.
REARM_S     = 60.0
# A move that could not run (the RESTART lock busy, the viewer in a menu, a
# transient error) gets one more try RETRY_S later; never a loop.
MAX_FIRES   = 2
RETRY_S     = 60.0

# Kodi conditions, read with xbmc.getCondVisibility (never logged). The '|' in
# BUSY is Kodi's OR inside the text, not a Python operator (the no-'|' scanner
# checks operators only). Paused (also a CEC standby that pauses), seeking or
# fast-forwarding / rewinding: the count starts again.
BUSY       ='Player.Paused | Player.Seeking | Player.Forwarding | Player.Rewinding'
# Full-screen video with no dialog on top: Kodi's VideoOSD is a modal dialog
# (CGUIDialog's default modality), so an open OSD waits too, as the guide, a
# menu and the channel preview do.
ACT_WINDOW = 'Window.IsActive(fullscreenvideo) + !System.HasActiveModalDialog + !Player.ChannelPreviewActive'
# Kodi's own flag on the playing replay: CPVRPlaybackState::StartPlayback sets
# it when "Play next programme automatically" is on ('Play programme',
# Player.Open, Kodi's own next show), always for the guide menu's "Play
# programmes from here" and never for its "Play only this programme"
# (PVRContextMenus.cpp); its natural end (GetNextAutoplayItem) reads nothing
# else.
AUTOPLAY_PROPERTY = 'epg_playlist_item'


def _id(value):
    """A Kodi id: a positive int (never a bool or a string), else None."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1: return None
    return value


def _num(value):
    """A player time in s: an int or a float (never a bool or a string), else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)): return None
    return float(value)


def playing_broadcast(item):
    """Player.GetItem 'item' (properties []) -> the playing guide entry's broadcast id, or None.
    Kodi reports a playing guide entry (a replay) with its EPG database id as 'id'; a live
    channel is type 'channel' with its channel id, which is never a broadcast."""
    if not isinstance(item, dict) or item.get('type') == 'channel': return None
    return _id(item.get('id'))


def programme(channels, broadcastid, now, decode_plot=None):
    """PVR.GetChannels (broadcastnow) rows -> the show `broadcastid` when it is on air at `now`
    ({'channelid','broadcastid','start','end','endtime'}; 'endtime' is Kodi's UTC text, for log
    lines only), else None: not any channel's show on now (a finished show), bad times, or a
    Custom channel's show (decode_plot, Globals._decodePlot: Custom channels have their own
    player, never this watcher)."""
    bid = _id(broadcastid)
    if bid is None: return None
    for channel in channels or []:
        bnow = channel.get('broadcastnow') if isinstance(channel, dict) else None
        if not isinstance(bnow, dict) or _id(bnow.get('broadcastid')) != bid: continue
        cid   = _id(channel.get('channelid'))
        start = clock_helpers.epg_utc_to_epoch(bnow.get('starttime'))
        end   = clock_helpers.epg_utc_to_epoch(bnow.get('endtime'))
        if cid is None or start is None or end is None or not (start <= now < end): return None
        if decode_plot is not None and restart_helpers.custom_chid(decode_plot(bnow.get('plot') or '')): return None
        return {'channelid': cid, 'broadcastid': bid, 'start': start, 'end': end, 'endtime': bnow.get('endtime')}
    return None


def new_track(epoch, now):
    """The tick's count for a new playback `epoch`, first seen at `now`."""
    return {'epoch': epoch, 'seen': now, 'lookup': False, 'tick': None, 'pos': None, 'total': None,
            'since': None, 'fires': 0, 'fired_at': None, 'deferred': False}


def ready(window, idle):
    """The add-on may act: full-screen video with no dialog (ACT_WINDOW) and no key pressed for
    QUIET_S (idle: whole s, BUILTIN.getIdle). One rule for the tick and the move's last check."""
    idle = _num(idle)
    return bool(window) and idle is not None and idle >= QUIET_S


def stall_step(track, sample, now):
    """One tick -> (the new track, due). Never changes `track`.
    sample: pos / total (the player's position and length in s, None when unknown), busy (BUSY),
    window (ACT_WINDOW), idle (s since the last key press).
    Due once position and length stood still (POS_EPS) at the end of the stream (END_SLACK_S) for
    STALL_S, at most MAX_FIRES times RETRY_S apart per pass; when otherwise due but not ready(), it
    waits ('deferred') and is due the moment the viewer is back. The stream alone decides: the
    guide's end time plays no part (imports.70's first build also waited for it + 60 s; the user's
    rule of 2026-11-19 is 20 s after it freezes). Where to go is the move's question (on_air,
    next_broadcast)."""
    t      = dict(track)
    sample = sample if isinstance(sample, dict) else {}
    last, last_pos, last_total = t.get('tick'), t.get('pos'), t.get('total')
    pos, total = _num(sample.get('pos')), _num(sample.get('total'))
    t['tick'], t['pos'], t['total'] = now, pos, total
    if pos is not None and total is not None and total - pos > REARM_S:
        t['fires'], t['deferred'] = 0, False                      # a seek back: a new pass
    still = (last is not None and 0 <= now - last <= TICK_GAP_S
             and None not in (last_pos, last_total, pos, total)
             and abs(pos - last_pos) <= POS_EPS and abs(total - last_total) <= POS_EPS
             and not sample.get('busy'))
    if not still:
        t['since'] = None
        return t, False
    if t.get('since') is None: t['since'] = last                  # when the stuck values were first seen
    fires, fired_at = t.get('fires') or 0, t.get('fired_at')
    due = (total > 0 and total - pos <= END_SLACK_S
           and now - t['since'] >= STALL_S
           and fires < MAX_FIRES
           and (fires == 0 or (fired_at is not None and now - fired_at >= RETRY_S)))
    if not due: return t, False
    if not ready(sample.get('window'), sample.get('idle')):
        t['deferred'] = True
        return t, False
    t['fires'], t['fired_at'], t['deferred'] = fires + 1, now, False
    return t, True


def autoplay_on(value):
    """Kodi's 'epg_playlist_item' as Python reads it (ListItem.getProperty: CVariant::asString,
    'true' when set) -> whether Kodi would go on at a natural end. Only 'true' counts."""
    return isinstance(value, str) and value.strip().lower() == 'true'


def on_air(end, now):
    """The guide still lists the watched show as on air at `now`: its guide end (epoch, from the
    lookup) has not come. A stream's window need not match its guide entry (2026-11-19: one ended
    a minute before the guide end, one was declared an hour past it), so a replay can freeze while
    its show is on air; the move then goes to the live channel. An unusable end counts as over."""
    end = _num(end)
    return end is not None and now < end


def next_broadcast(broadcasts, start, end, now):
    """PVR.GetBroadcasts rows of the channel (broadcastid, starttime, endtime) -> the entry to play
    after the frozen replay of [start, end) ({'broadcastid','start','end'}), or None for the live
    channel. While the guide still lists that show as on air (on_air) -> live, whatever the rows
    say (the user's rule of 2026-11-19). Once it is over, Kodi's GetNextAutoplayItem
    (PVRPlaybackState.cpp): the entries in start order, the first one with exactly that start and
    end that isn't the last, then the one after it; no match (the guide changed) or the last entry
    -> live. One more deliberate difference from Kodi: an entry that hasn't started at `now` ->
    live too ('vod' catch-up reports even future entries as playable)."""
    if on_air(end, now): return None
    rows = []
    for row in broadcasts or []:
        if not isinstance(row, dict): continue
        bid = _id(row.get('broadcastid'))
        s   = clock_helpers.epg_utc_to_epoch(row.get('starttime'))
        e   = clock_helpers.epg_utc_to_epoch(row.get('endtime'))
        if bid is None or s is None or e is None: continue
        rows.append((s, e, bid))
    rows.sort()
    for i in range(len(rows) - 1):
        if rows[i][0] == start and rows[i][1] == end:
            s, e, bid = rows[i + 1]
            if s > now: return None
            return {'broadcastid': bid, 'start': s, 'end': e}
    return None


def after_open(kind, outcome):
    """What follows an open of the next show (kind 'broadcast') or the live channel ('channel'),
    from its outcome (services.Player._replayEndOpen: 'not_opened' or a
    restart_helpers.watch_outcome result): 'live' (try the live channel once), 'stop' (end the
    frozen replay, as its natural end would) or None (done: it started, the viewer took over, it
    went without an error, or Kodi is closing). On Kodi 21 'retune' never comes: no playback error
    reaches Python (FORK_NOTES, Limits)."""
    if kind == 'broadcast' and outcome in ('not_opened', 'refused', 'retune'): return 'live'
    if kind == 'channel' and outcome in ('not_opened', 'refused'): return 'stop'
    return None


def target_text(target):
    """('broadcast', id) / ('channel', id) in plain text for a log line."""
    kind, value = target
    return ('the next show, broadcast %s' if kind == 'broadcast' else 'live channel %s') % (value,)


# imports.70: services.Player._replayEndOpen's result words in plain text, for its log line.
# 'started' is Kodi reporting the target as the playing item, which it does before the stream is
# open; 'gave_up' is also how a start that failed arrives on Kodi 21 (a stop: no playback error
# reaches Python).
OUTCOME_NOTES = {'started':    'Kodi switched to it',
                 'other':      'something else plays now; left alone',
                 'refused':    'it did not start; the frozen replay still plays',
                 'retune':     'it failed with a playback error',
                 'failed':     'it did not start and nothing plays',
                 'gave_up':    ('left alone: playback stopped (a Stop key or a failed start), a key was pressed,'
                                ' or still loading after 30 s'),
                 'not_opened': 'Kodi refused the request',
                 'shutdown':   'Kodi is closing'}


def outcome_note(outcome):
    """'word (what it means)'; the bare word when unknown."""
    note = OUTCOME_NOTES.get(outcome)
    return '%s (%s)' % (outcome, note) if note else '%s' % (outcome,)
