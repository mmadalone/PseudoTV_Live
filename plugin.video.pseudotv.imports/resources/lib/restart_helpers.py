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

# madteevee (imports fork, imports.62): "Restart from the beginning" for shows
# that are already running, on Custom (PseudoTV) channels and on LiveTV channels
# whose show on now can be replayed (Movistar+). Three ways in, one action:
#   B  the Restart pop-up, offered after a tune into a running show
#      (overlay.Replay, services.Player.toggleReplay);
#   C  a remote button: NotifyAll(pseudotv, restart) restarts what plays
#      (services.Monitor.onNotification);
#   D  'Restart from beginning' in the guide's context menu
#      (context_restart.py sends the same notification with the guide path).
#
# Import-light pure module (same pattern as playstate_helpers.py /
# replay_helpers.py): no Kodi imports, no addon imports, no I/O, no clock
# reads (callers pass `now`), Python 3.8 syntax. services.py reads Kodi and
# passes the facts in; every decision is made here and tested without stubs.
#
# Restart routes:
# - Custom: Player.seekTime(0) on the playing episode (no re-tune). When it
#   ends, the channel returns to live as it always does.
# - LiveTV: JSON-RPC Player.Open {broadcastid}, Kodi's own 'Play programme'
#   path (PlayEpgTag), only after PVR.GetBroadcastIsPlayable says yes.
#   Kodi's guide 'Play programme' on a Custom show on now keeps joining
#   mid-way (2026-05-11); plugin.py is not involved here.
import calendar
import json
import re
import time

# One notification for the remote button and the guide entry. The sender is
# short because the UC Remote 3 Kodi integration limits a custom command to 64
# characters; REMOTE_COMMAND is 59.
NOTIFY_SENDER  = 'pseudotv'
NOTIFY_MESSAGE = 'restart'
NOTIFY_METHOD  = 'Other.' + NOTIFY_MESSAGE      # what xbmc.Monitor.onNotification receives
REMOTE_COMMAND = 'JSONRPC.NotifyAll {"sender":"pseudotv","message":"restart"}'
KEYMAP_ACTION  = 'NotifyAll(pseudotv,restart)'

CUSTOM_SUFFIX      = '@PseudoTV_Live'           # '@' + constants.PSEUDOTV_SLUG (test-checked)
REPLAY_MAX_PERCENT = 95                         # settings.xml Replay_Percentage maximum
# The pop-up waits for this: full-screen video playing, Kodi's channel info,
# the channel preview and any modal dialog gone.
WINDOW_READY = ('Window.IsActive(fullscreenvideo) + Player.Playing + !Player.ShowInfo'
                ' + !Player.ChannelPreviewActive + !System.HasActiveModalDialog')
WATCH_POLL    = 0.5     # s between checks after Player.Open {broadcastid}
WATCH_GRACE   = 10.0    # s: live still playing / nothing playing -> the replay did not start
WATCH_TIMEOUT = 30.0    # s: stop watching
ERROR_SETTLE  = 2.0     # s after the request before a playback error leads to a re-tune
PENDING_TTL   = 30.0    # s: a Custom guide restart waits this long for its tune
STOP_WAIT     = 5.0     # s: wait for a stop before re-tuning the same Custom channel
SETTLE_TRIES  = 3       # decisions tried while a zap settles ('tune in progress') ...
SETTLE_WAIT   = 1.0     # ... this many s apart; then 'Restart Unavailable...'
POPUP_POLL    = 0.05    # s between the pop-up's checks (keys are handled during the wait)
# Keys pressed while the pop-up is up that go on to the video once it has closed
# (Kodi action id -> builtin Action() name): Up/Down, Info and the number keys (channel
# number entry; Kodi's PVR ignores them while a Python dialog is on top). OK restarts,
# Back only closes it, channel +/- are handled in overlay.Replay, other keys only close it.
POPUP_PASS_ON = dict([(3, 'Up'), (4, 'Down'), (11, 'Info')] + [(58 + n, 'Number%d' % n) for n in range(10)])

# decide() actions
SEEK_START     = 'seek_start'      # Player.seekTime(0) on what plays
OPEN_BROADCAST = 'open_broadcast'  # Player.Open {broadcastid} (Kodi's 'Play programme')
TUNE_CUSTOM    = 'tune_custom'     # Player.Open {channelid}, then seek to 0 when it plays
RETUNE_CUSTOM  = 'retune_custom'   # Kodi plays that channel with another programme: stop first
LOOKUP_LIVE    = 'lookup_live'     # a live channel plays: read its show on now, decide again
UNAVAILABLE    = 'unavailable'     # note 30154 'Restart Unavailable...'
IGNORE         = 'ignore'          # log only (a zap is in progress / the show changed)

_GUIDE_PATH = re.compile(r'^pvr://guide/\d+/\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.epg$')


def _int(value):
    if isinstance(value, bool): return None
    try: return int(value)
    except (TypeError, ValueError): return None


def epg_utc_to_epoch(text):
    """Kodi JSON-RPC broadcast time 'YYYY-MM-DD HH:MM:SS' (UTC) -> epoch seconds, None if empty/invalid."""
    if not isinstance(text, str) or not text.strip(): return None
    try: return calendar.timegm(time.strptime(text.strip(), '%Y-%m-%d %H:%M:%S'))
    except ValueError: return None


def clamp_percent(value):
    """Replay_Percentage / rule 54 value -> 0..95; anything unusable -> 0 (off)."""
    value = _int(value)
    if value is None or value <= 0: return 0
    return min(REPLAY_MAX_PERCENT, value)


def offer_progress(elapsed, duration, threshold, max_progress):
    """The pop-up rule (the original Replay rule): threshold% <= progress < Seek_Threshold%."""
    threshold = clamp_percent(threshold)
    if threshold <= 0: return False
    try: elapsed, duration, max_progress = float(elapsed), float(duration), float(max_progress)
    except (TypeError, ValueError): return False
    if duration <= 0 or elapsed < 0 or elapsed >= duration: return False
    progress = elapsed * 100.0 / duration
    return threshold <= progress < max_progress


def custom_chid(decoded_plot):
    """Channel id of a Custom (PseudoTV) guide entry from its decoded plot, else None."""
    citem = (decoded_plot or {}).get('citem') if isinstance(decoded_plot, dict) else None
    chid  = citem.get('id') if isinstance(citem, dict) else None
    return chid if isinstance(chid, str) and CUSTOM_SUFFIX in chid else None


def classify_playing(raw):
    """Raw Kodi facts -> the playing snapshot decide() reads.
    raw: video (Player.isPlayingVideo), item (Player.GetItem 'item', properties []),
    pvr_live (Pvr.IsPlayingTv), pvr_epgtag (Pvr.IsPlayingEpgTag), seekable
    (Player.SeekEnabled), pseudotv (Player.playingItem when isPlayingPseudoTV, else
    None), tune_ts (the playing ListItem's sysInfo _tune_ts, None when unreadable)."""
    raw  = raw or {}
    item = raw.get('item') if isinstance(raw.get('item'), dict) else {}
    seekable = bool(raw.get('seekable'))
    if not raw.get('video'): return {'kind': 'none'}
    info = raw.get('pseudotv')
    if isinstance(info, dict) and info.get('isPseudoTV'):
        fitem = info.get('fitem') or {}
        cached, now_ts = info.get('_tune_ts'), raw.get('tune_ts')
        return {'kind': 'custom', 'chid': info.get('chid'), 'start': _int(fitem.get('start')),
                'isfiller': info.get('isfiller', True), 'radio': bool(info.get('radio', False)),
                # the service's programme is the one Kodi plays (not a zap still in progress)
                'tune_ok': bool(cached) and cached == now_ts, 'seekable': seekable,
                'channelid': _int(item.get('id')) if item.get('type') == 'channel' else None}
    if raw.get('pvr_epgtag'):
        # Player.GetItem 'id' of a playing guide entry is its broadcastid (EPG DatabaseID)
        return {'kind': 'replay', 'broadcastid': _int(item.get('id')), 'seekable': seekable}
    if raw.get('pvr_live') and item.get('type') == 'channel' and (_int(item.get('id')) or 0) > 0:
        return {'kind': 'livetv', 'channelid': _int(item.get('id')), 'seekable': seekable}
    return {'kind': 'other'}


def custom_offer(info, elapsed, duration, threshold, max_progress):
    """Pop-up check for a Custom programme (Player.playingItem). A missing filler flag
    counts as filler (same default as the old toggleReplay); never radio."""
    if not isinstance(info, dict) or not info.get('isPseudoTV'): return False
    if info.get('isfiller', True) or info.get('radio', False): return False
    return offer_progress(elapsed, duration, threshold, max_progress)


def custom_target(info):
    """The Custom programme that plays now (Player.playingItem) as a restart target."""
    if not isinstance(info, dict) or not info.get('isPseudoTV'): return None
    start = _int((info.get('fitem') or {}).get('start'))
    if not info.get('chid') or start is None: return None
    return {'kind': 'custom', 'chid': info.get('chid'), 'start': start}


def livetv_target(channelid, details, now, decode_plot=None, threshold=0, max_progress=100, offer=False):
    """PVR.GetChannelDetails 'channeldetails' (broadcastnow + hasarchive) of the live channel
    -> restart target or None. offer=True applies the pop-up rule (B); C and D are explicit."""
    channelid = _int(channelid)
    if channelid is None or channelid < 1 or not isinstance(details, dict): return None
    if details.get('hasarchive') is not True: return None
    bnow = details.get('broadcastnow')
    if not isinstance(bnow, dict): return None
    if decode_plot is not None and custom_chid(decode_plot(bnow.get('plot') or '')): return None   # Custom: never the LiveTV route
    broadcastid = _int(bnow.get('broadcastid'))
    start, end  = epg_utc_to_epoch(bnow.get('starttime')), epg_utc_to_epoch(bnow.get('endtime'))
    if broadcastid is None or broadcastid < 1 or start is None or end is None: return None
    # vod catch-up reports even FUTURE entries as playable (iptvsimple ignores catchup-days
    # for vod), so 'on now' is checked here, never left to PVR.GetBroadcastIsPlayable
    if not (start <= now < end): return None
    if offer and not offer_progress(now - start, end - start, threshold, max_progress): return None
    return {'kind': 'livetv', 'channelid': channelid, 'broadcastid': broadcastid, 'start': start, 'end': end}


def guide_target(channels, path, now, decode_plot):
    """D: the guide entry's path -> the channel whose show on now has exactly that path."""
    if not isinstance(path, str) or not _GUIDE_PATH.match(path): return None
    for channel in channels or []:
        bnow = channel.get('broadcastnow') if isinstance(channel, dict) else None
        if not isinstance(bnow, dict) or bnow.get('filenameandpath') != path: continue
        channelid, broadcastid = _int(channel.get('channelid')), _int(bnow.get('broadcastid'))
        start, end = epg_utc_to_epoch(bnow.get('starttime')), epg_utc_to_epoch(bnow.get('endtime'))
        if not channelid or not broadcastid or start is None or end is None or not (start <= now < end): return None
        decoded = decode_plot(bnow.get('plot') or '') or {}
        chid = custom_chid(decoded)
        if chid:
            fstart = _int(decoded.get('start'))
            if fstart is None: return None
            return {'kind': 'custom', 'chid': chid, 'start': fstart, 'channelid': channelid,
                    'broadcastid': broadcastid, 'end': end}
        if channel.get('hasarchive') is not True: return None
        return {'kind': 'livetv', 'channelid': channelid, 'broadcastid': broadcastid, 'start': start, 'end': end}
    return None


def decide(playing, target, now, guide=False):
    """(action, reason). guide=False: restart what plays (B pop-up, C remote); the target, when
    given, must still be what plays. guide=True: restart that guide entry (D)."""
    playing = playing or {}
    kind = playing.get('kind')
    if target is None:
        if guide: return UNAVAILABLE, 'guide entry not found or not on now'
        if kind == 'custom':
            target = {'kind': 'custom', 'chid': playing.get('chid'), 'start': playing.get('start')}
        elif kind == 'replay':
            return (SEEK_START, 'replay plays') if playing.get('seekable') else (UNAVAILABLE, 'replay cannot seek')
        elif kind == 'livetv':
            return LOOKUP_LIVE, 'live channel plays'
        else:
            return UNAVAILABLE, 'nothing restartable plays'
    if not isinstance(target, dict): return UNAVAILABLE, 'no target'
    if target.get('kind') == 'custom':
        same = kind == 'custom' and playing.get('chid') == target.get('chid') and playing.get('start') == target.get('start')
        if guide and not (target.get('end') and now < target['end']): return UNAVAILABLE, 'show no longer on now'
        if same:
            if not playing.get('tune_ok'):        return IGNORE, 'tune in progress'
            if playing.get('isfiller', True):     return UNAVAILABLE, 'filler'
            if playing.get('radio'):              return UNAVAILABLE, 'radio'
            if not playing.get('seekable'):       return UNAVAILABLE, 'cannot seek'
            return SEEK_START, 'custom programme plays'
        if not guide: return IGNORE, 'programme changed'
        if playing.get('channelid') and playing.get('channelid') == target.get('channelid'):
            # Kodi plays this very channel (another programme): Player.Open {channelid}
            # would only switch to full screen (SwitchToChannel), so stop first.
            return RETUNE_CUSTOM, 'same channel, another programme plays'
        return TUNE_CUSTOM, 'custom channel not playing'
    if target.get('kind') == 'livetv':
        if not target.get('playable'): return UNAVAILABLE, 'not playable'
        if kind == 'replay' and playing.get('broadcastid') == target.get('broadcastid'):
            # Player.Open on the entry that already plays only switches to full screen
            return (SEEK_START, 'this replay plays') if playing.get('seekable') else (UNAVAILABLE, 'replay cannot seek')
        if not guide and not (kind == 'livetv' and playing.get('channelid') == target.get('channelid')):
            return IGNORE, 'channel changed'
        if not (target.get('start', 0) <= now < target.get('end', 0)): return UNAVAILABLE, 'show no longer on now'
        return OPEN_BROADCAST, 'live show'
    return UNAVAILABLE, 'unknown target'


def watch_outcome(state):
    """After Player.Open {broadcastid} returned OK, polled every WATCH_POLL s:
    'started' | 'other' | 'refused' | 'retune' | 'failed' | 'gave_up' | 'wait'.
    state: elapsed (s since Player.Open returned), replay (the target broadcast plays as
    a replay), unchanged (what played before the request still plays), playing (anything
    plays), busy (busy dialog), idle / idle0 (whole s since the last key press, now / when
    Player.Open returned), stopped / errored (a playback stop / error event since
    Player.Open), can_retune (a live channel played before the request: it can be tuned
    again).
    Kodi does not tell Python that a replay was refused, so this is inferred, on the safe
    side. Kodi's player reports exactly one of stopped / error / ended when playback goes
    (VideoPlayer::OnExit), and it switches to the replay without a stop. A stop means the
    viewer stopped it (a key or a remote app; JSON-RPC Player.Stop doesn't reset the idle
    time), so it is left alone; only a playback error re-tunes the channel, and never
    after a key press or over a busy dialog."""
    state   = state or {}
    elapsed = float(state.get('elapsed') or 0)
    if state.get('replay'): return 'started'
    playing, busy, unchanged = bool(state.get('playing')), bool(state.get('busy')), bool(state.get('unchanged'))
    if playing and not unchanged: return 'other'                                   # the viewer zapped / something else plays
    if not playing and state.get('stopped'): return 'gave_up'                      # the viewer stopped it
    # no key pressed since Player.Open: the idle time only grows (whole seconds, so
    # floor(a + b) >= floor(a) + floor(b) makes this exact when no key was pressed)
    quiet = float(state.get('idle') or 0) >= float(state.get('idle0') or 0) + int(elapsed)
    if not playing and not busy:
        if not quiet: return 'gave_up'                                            # the viewer took over: leave it to them
        if state.get('errored') and elapsed >= ERROR_SETTLE:
            return 'retune' if state.get('can_retune') else 'failed'               # the replay failed to open
    if elapsed >= WATCH_GRACE and not busy:
        if unchanged: return 'refused'                                            # refused before the player opened; it goes on
        if not playing: return 'failed'                                           # gone without an error: no re-tune
    if elapsed >= WATCH_TIMEOUT: return 'refused' if unchanged else 'gave_up'      # still loading / busy: stop watching
    return 'wait'


def pending_match(pending, info, now):
    """D on a Custom channel that did not play: seek to 0 once its tune of that programme plays."""
    if not isinstance(pending, dict) or not isinstance(info, dict) or not info.get('isPseudoTV'): return False
    if info.get('isfiller', True): return False
    try: age = float(now) - float(pending.get('ts'))
    except (TypeError, ValueError): return False
    if age < 0 or age > PENDING_TTL: return False
    return info.get('chid') == pending.get('chid') and _int((info.get('fitem') or {}).get('start')) == pending.get('start')


def pending_expired(pending, now):
    try: return float(now) - float((pending or {}).get('ts')) > PENDING_TTL
    except (TypeError, ValueError): return True


def popup_done(epoch_then, epoch_now, now, end=None):
    """The pop-up closes itself when playback changed or (LiveTV) its show ended."""
    if epoch_then != epoch_now: return True
    return bool(end) and now >= end


def guide_request(path):
    """context_restart.py: the guide entry's own path -> NotifyAll data, None if not a guide entry."""
    return {'path': path} if isinstance(path, str) and _GUIDE_PATH.match(path) else None


def parse_notification(sender, method, data):
    """Monitor.onNotification -> None (not ours, or unusable: ignored) or a request:
    {'path': None} restart what plays (C), {'path': '<guide path>'} restart that entry (D)."""
    if sender != NOTIFY_SENDER or method != NOTIFY_METHOD: return None
    if data in (None, '', 'null'): return {'path': None}
    try: value = json.loads(data)
    except (TypeError, ValueError): return None
    if isinstance(value, list) and len(value) == 1: value = value[0]     # jsonrpc.notifyAll wraps data in a list
    if value is None or value == {}: return {'path': None}
    if not isinstance(value, dict): return None
    return guide_request(value.get('path'))
