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

# madteevee (imports fork, imports.60): "Rollback watched" (Rollback_Watched,
# channel rule 53) as an exact undo of what Kodi itself recorded.
#
# Import-light pure module (same pattern as replay_helpers.py /
# filter_helpers.py): no Kodi imports, no addon imports, Python 3.8 syntax.
# Kodi I/O is injected, so the tests run every decision without Kodi stubs.
#
# How it works:
# - When a PseudoTV programme starts and Kodi plays the programme's own file
#   (catch-up clicks, catchup="vod" channels, playlist entries that play their
#   own file), the file's
#   watched count, last-played date and resume point are read from Kodi
#   (Files.GetFileDetails) and kept in memory: the snapshot.
# - When that programme ends (stop, natural end, next programme, or a
#   hand-over to non-PseudoTV playback), Kodi has already saved its own marks
#   for the file (Kodi 21 saves on file close, before Python hears about the
#   stop). The file is read again and only the fields that changed are put
#   back, with Files.SetFileDetails: the same per-file rows Kodi's save wrote.
# - Live PseudoTV programmes play as pvr://...pvr items. Kodi records nothing
#   for live TV (SaveFileStateJob: `if (!item.IsLiveTV())`), so they get no
#   snapshot and cost no JSON-RPC.
# - Only library movies, episodes and music videos are handled; other files
#   are skipped quietly.
#
# Replaces jsonrpc.quePlaycount, which sent the guide-build play count plus
# resume {0,0} on every programme change (erasing resume points).

import json
import threading

# Stage switch. True: the write is sent. False: everything runs except the final
# write, which is logged as "would send <request>" instead (the imports.60
# stage-1 test build ran that way).
PLAYSTATE_WRITES     = True
PLAYSTATE_PROPERTIES = ('playcount', 'lastplayed', 'resume')
SNAPSHOT_CAP         = 16
LIBRARY_TYPES        = ('movie', 'episode', 'musicvideo')

# Log levels passed to the injected log(msg, level); the caller maps them.
DEBUG = 'debug'
INFO  = 'info'
ERROR = 'error'


def library_ref(details):
    """(type, id) for a library movie / episode / music video, else None."""
    if not isinstance(details, dict): return None
    kind, dbid = details.get('type'), details.get('id')
    if kind not in LIBRARY_TYPES: return None
    if isinstance(dbid, bool) or not isinstance(dbid, int) or dbid < 1: return None
    return (kind, dbid)


def response_error(response):
    """Short text when a JSON-RPC response carries an error or no result, else ''."""
    if not isinstance(response, dict): return 'no response'
    result = response.get('result')
    if isinstance(result, dict) and result.get('error'): return json.dumps(result.get('error'), sort_keys=True)
    if response.get('error'): return json.dumps(response.get('error'), sort_keys=True)
    if result is None: return 'no result'
    return ''


def parse_playstate(response):
    """Playstate from a Files.GetFileDetails response, or None.

    {'ref': (type, id), 'playcount': int, 'lastplayed': str,
     'resume': {'position': float, 'total': float}}. None for errors and for
    files that are not a library movie / episode / music video."""
    if response_error(response): return None
    result = response.get('result')
    if not isinstance(result, dict): return None
    details = result.get('filedetails')
    ref = library_ref(details)
    if ref is None: return None
    resume = details.get('resume') or {}
    if not isinstance(resume, dict): return None
    try:
        return {'ref'       : ref,
                'playcount' : int(details.get('playcount') or 0),
                'lastplayed': str(details.get('lastplayed') or ''),
                'resume'    : {'position': float(resume.get('position') or 0.0),
                               'total'   : float(resume.get('total') or 0.0)}}
    except (TypeError, ValueError): return None


def _resume_point(state):
    """(position, total) when the state has a resume point, else None."""
    resume = state['resume']
    if resume['position'] > 0: return (resume['position'], resume['total'])
    return None


def restore_request(file, before, after):
    """The Files.SetFileDetails request that turns `after` back into `before`.

    None when either state is missing, they belong to different library items,
    or nothing changed. Only changed fields are sent; never 'runtime'.
    Kodi rules this follows (Kodi 21 FileOperations.cpp SetFileDetails):
    - a valid lastplayed without a playcount forces playcount to 1, so the
      playcount is always sent together with the date;
    - playcount 0 + lastplayed '' clears both;
    - Kodi cannot store a playcount > 0 without a date (an empty date becomes
      "now"), so in that one case the date is left as Kodi has it;
    - resume position 0 clears the resume point."""
    if not file or not before or not after: return None
    if before.get('ref') is None or before.get('ref') != after.get('ref'): return None
    params = {}
    if after['playcount'] != before['playcount'] or after['lastplayed'] != before['lastplayed']:
        params['playcount'] = before['playcount']
        if before['lastplayed'] or before['playcount'] == 0:
            params['lastplayed'] = before['lastplayed']
    resume_before = _resume_point(before)
    if _resume_point(after) != resume_before:
        if resume_before: params['resume'] = {'position': resume_before[0], 'total': resume_before[1]}
        else:             params['resume'] = {'position': 0.0, 'total': 0.0}
    if not params: return None
    request = {'file': file, 'media': 'video'}
    request.update(params)
    return {'method': 'Files.SetFileDetails', 'params': request}


def programme_key(info):
    """Owner key of one played programme: a re-click of the same programme
    (new tune timestamp) or the next playlist entry (new start) is a new owner."""
    info  = info or {}
    fitem = info.get('fitem') or {}
    return (info.get('chid'), fitem.get('start'), fitem.get('file'), info.get('_tune_ts'))


def programme_file(info):
    return ((info or {}).get('fitem') or {}).get('file') or None


def describe(state):
    if not state: return 'none'
    resume = state['resume']
    return '%s %s, playcount %s, lastplayed "%s", resume %s/%s' % (state['ref'][0], state['ref'][1], state['playcount'],
                                                                   state['lastplayed'], resume['position'], resume['total'])


class PlaystateTracker(object):
    """Snapshots and restores, one atomic step per player event.

    Injected Kodi I/O:
    - read_details(file) -> Files.GetFileDetails response, read fresh (not cached);
    - playing_file(info) -> the programme's file when Kodi is playing exactly
      that file now, else None (live PVR items, other files, nothing playing);
    - send(request)      -> JSON-RPC response;
    - log(msg, level)    -> level is DEBUG / INFO / ERROR from this module.
    writes=None uses PLAYSTATE_WRITES at each event."""

    def __init__(self, read_details, playing_file, send, log, writes=None, cap=SNAPSHOT_CAP):
        self._read_details = read_details
        self._playing_file = playing_file
        self._send         = send
        self._log          = log
        self._writes       = writes
        self._cap          = cap
        self._lock         = threading.Lock()
        self._store        = {} # playing file -> {'key': owner programme_key, 'state': snapshot, 'tick': last use}
        self._tick         = 0


    def pending(self):
        with self._lock: return len(self._store)


    def clear(self):
        """Cancel every pending rollback (the switch was turned off)."""
        with self._lock:
            count = len(self._store)
            self._store.clear()
        if count: self._log('PlaystateTracker: Rollback watched is off, %s pending rollback(s) cancelled' % (count), INFO)


    def transition(self, old_info=None, new_info=None, allow=False):
        """One player event: `old_info` ended (or is ending), `new_info` started
        (None for stop / natural end / hand-over). `allow` is the switch value
        for the new programme, decided at its start."""
        old_info = old_info or {}
        new_info = new_info or {}
        old_key, old_file = programme_key(old_info), programme_file(old_info)
        new_key = programme_key(new_info)
        with self._lock:
            try:
                new_file = self._playing_file(new_info) if (allow and new_info) else None
                entry = self._store.get(old_file) if old_file else None
                if entry is not None and entry['key'] != old_key: entry = None # not ours: someone else owns it now
                if entry is not None and new_file and new_file == old_file:
                    # The same file continues under the new programme: keep the
                    # original snapshot, restore at the end of the new one.
                    entry['key']  = new_key
                    entry['tick'] = self._next_tick()
                    self._log('PlaystateTracker: same file continues, snapshot carried over: %s' % (new_file), INFO)
                    return
                restore_state = self._store.pop(old_file)['state'] if entry is not None else None
            except Exception as e:
                self._log('PlaystateTracker: transition failed! %s' % (e), ERROR)
                return
            if new_file:
                # New snapshot first (the earlier the read, the safer), then the restore.
                try: self._snapshot(new_info, new_file, new_key)
                except Exception as e: self._log('PlaystateTracker: snapshot of %s failed! %s' % (new_file, e), ERROR)
            if restore_state is not None:
                try: self._restore(old_file, restore_state)
                except Exception as e: self._log('PlaystateTracker: rollback of %s failed! %s' % (old_file, e), ERROR)


    def _next_tick(self):
        self._tick += 1
        return self._tick


    def _snapshot(self, new_info, new_file, new_key):
        current = self._store.get(new_file)
        if current is not None:
            if current['key'] == new_key: return # this programme already has its snapshot
            # A snapshot owned by a programme whose end was never processed.
            # It can't be matched to anything any more: dropped (a missed
            # rollback, never a wrong one).
            self._store.pop(new_file, None)
            self._log('PlaystateTracker: dropped an older snapshot of %s owned by another programme' % (new_file), INFO)
        response = self._read_details(new_file)
        state    = parse_playstate(response)
        if state is None:
            error = response_error(response)
            if error: self._log('PlaystateTracker: no snapshot, Kodi could not read %s: %s' % (new_file, error), INFO)
            else:     self._log('PlaystateTracker: no snapshot, not a library movie/episode/music video: %s' % (new_file), INFO)
            return
        if self._playing_file(new_info) != new_file:
            self._log('PlaystateTracker: snapshot discarded, playback moved on before the read finished: %s' % (new_file), INFO)
            return
        self._store[new_file] = {'key': new_key, 'state': state, 'tick': self._next_tick()}
        while len(self._store) > self._cap:
            oldest = min(self._store, key=lambda f: self._store[f]['tick'])
            self._store.pop(oldest)
            self._log('PlaystateTracker: snapshot cap reached, dropped the oldest: %s' % (oldest), INFO)
        self._log('PlaystateTracker: snapshot %s: %s' % (new_file, describe(state)), INFO)


    def _restore(self, file, before):
        after = parse_playstate(self._read_details(file))
        if after is None:
            self._log('PlaystateTracker: nothing restored, could not re-read %s (before: %s)' % (file, describe(before)), ERROR)
            return
        if after['ref'] != before['ref']:
            self._log('PlaystateTracker: nothing restored, %s now belongs to a different library item (before: %s, now: %s)' % (file, describe(before), describe(after)), ERROR)
            return
        request = restore_request(file, before, after)
        if request is None:
            self._log('PlaystateTracker: no change to roll back: %s (%s)' % (file, describe(before)), INFO)
            return
        text   = json.dumps(request, sort_keys=True)
        writes = PLAYSTATE_WRITES if self._writes is None else self._writes
        if not writes:
            self._log('PlaystateTracker: would send %s (Kodi now: %s)' % (text, describe(after)), INFO)
            return
        response = self._send(request)
        if isinstance(response, dict) and response.get('result') == 'OK':
            self._log('PlaystateTracker: restored %s (was: %s)' % (text, describe(after)), INFO)
        else:
            self._log('PlaystateTracker: rollback rejected by Kodi: %s, response = %s' % (text, json.dumps(response, sort_keys=True, default=str)), ERROR)
