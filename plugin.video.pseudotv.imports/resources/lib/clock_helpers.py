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

# madteevee (imports fork, imports.63): one clock and one set of time
# conversions for the whole add-on.
#
# Import-light pure module (same pattern as replay_helpers.py /
# restart_helpers.py / playstate_helpers.py): standard library only (calendar,
# datetime, time), no Kodi imports, no add-on imports, no I/O, Python 3.8
# syntax and APIs (no zoneinfo, no datetime.UTC). now() is the only function
# that reads the clock; every other function takes the instant from its
# caller, so the tests pin the clock and the time zone independently.
# Callers write `import clock_helpers` and call clock_helpers.now() (a module
# attribute that a test can replace), never `from clock_helpers import now`.
#
# The rules:
# - An instant is a Unix epoch from time.time(): the same number Kodi's PVR,
#   IPTV Simple and every guide payload use. The old getUTCstamp() (the epoch
#   minus the UTC offset: 1 h slow in winter, 2 h in summer in Madrid) is gone.
# - Local time appears only at the edges: the XMLTV text the add-on writes,
#   the text it shows, and the local hour a schedule starts on.
# - Each instant is converted with ITS OWN UTC offset, the one in force at
#   that instant, never the offset of "now".
# - XMLTV text without an offset is UTC (XMLTV DTD; IPTV Simple reads it the
#   same way).
# - Kodi JSON-RPC broadcast times ('YYYY-MM-DD HH:MM:SS') are UTC.
import calendar
import datetime
import time

XMLTV_FORMAT        = '%Y%m%d%H%M%S'   # == constants.DTFORMAT (test-checked)
GUIDE_FILL_BUFFER_S = 12 * 3600        # a guide is extended once it ends before guide_days minus this
_UTC                = datetime.timezone.utc


def now():
    """The add-on's one clock: Unix epoch seconds (float, time.time())."""
    return time.time()


def local_floor(epoch, minutes=60):
    """The instant (int epoch) of the local wall-clock floor of `epoch` to a
    multiple of `minutes`, which must be an int that divides 60: the top of the
    local hour for 60, :00 or :30 for 30. It uses the UTC offset in force at
    `epoch` itself (tm_gmtoff), so the result never depends on earlier
    conversions: in the repeated hour after the change to winter time,
    mktime() of a naive local time could pick either of the two instants."""
    if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes <= 0 or 60 % minutes:
        raise ValueError('minutes must be an int that divides 60, got %r' % (minutes,))
    e = int(epoch)
    return e - ((e + time.localtime(e).tm_gmtoff) % (minutes * 60))


def xmltv_time(epoch):
    """XMLTV time text of an instant: its local digits and the UTC offset in
    force at that instant, 'YYYYmmddHHMMSS +HHMM' (XMLTV DTD). Start and stop
    are formatted one by one, so a programme written before a change of summer
    time but airing after it carries the offset of its own airing. The digits
    are those of datetime.fromtimestamp, as before imports.63 (microseconds
    rounded the same way); for whole seconds xmltv_to_epoch(xmltv_time(e)) == e."""
    return datetime.datetime.fromtimestamp(epoch, _UTC).astimezone().strftime(XMLTV_FORMAT + ' %z')


def xmltv_to_epoch(s):
    """XMLTV time ('YYYYmmddHHMMSS +HHMM') -> epoch seconds (int), or None.

    calendar.timegm minus the offset. No offset = UTC (XMLTV DTD; IPTV Simple
    reads it the same way; before imports.63 this read it as local time).
    None for anything else: not text, not 14 digits, an impossible date, or an
    offset that isn't +HHMM / -HHMM. Moved from replay_helpers in imports.63;
    replay_helpers re-exports it."""
    if not isinstance(s, str):
        return None
    s = s.strip()
    base = s[:14]
    if len(base) != 14 or not base.isdigit():
        return None
    fields = (int(base[0:4]), int(base[4:6]), int(base[6:8]),
              int(base[8:10]), int(base[10:12]), int(base[12:14]))
    try:
        datetime.datetime(*fields)
    except ValueError:
        return None
    rest = s[14:].strip()
    if not rest:
        return calendar.timegm(fields + (0, 0, 0))
    if len(rest) < 5 or rest[0] not in '+-' or not rest[1:5].isdigit():
        return None
    offset = int(rest[1:3]) * 3600 + int(rest[3:5]) * 60
    if rest[0] == '-':
        offset = -offset
    return calendar.timegm(fields + (0, 0, 0)) - offset


def epg_utc_to_epoch(text):
    """Kodi JSON-RPC broadcast time 'YYYY-MM-DD HH:MM:SS' (UTC) -> epoch seconds,
    None if empty/invalid. time.strptime, not datetime.strptime (Kodi's embedded
    Python can fail on the latter, see globals.strpTime). Moved unchanged from
    restart_helpers in imports.63; restart_helpers re-exports it."""
    if not isinstance(text, str) or not text.strip(): return None
    try: return calendar.timegm(time.strptime(text.strip(), '%Y-%m-%d %H:%M:%S'))
    except ValueError: return None


def build_window(now_epoch, guide_days):
    """(anchor, threshold) of one build pass at `now_epoch`.
    Builder.buildChannels and Tasks._filterChannelsNeedingBuild both use it, so
    the filter never queues a channel that the Builder then declines.
      anchor    -- local_floor(now_epoch, 60), an int: where the schedule of a
                   new or empty channel starts (the loadStopTimes fallback).
      threshold -- now_epoch + guide_days days - GUIDE_FILL_BUFFER_S: a channel
                   whose guide ends at or before it gets more programmes."""
    return local_floor(now_epoch, 60), now_epoch + guide_days * 86400 - GUIDE_FILL_BUFFER_S


def join_offset(start, stop, now_epoch):
    """Seconds since `start` (int) when the programme [start, stop) is on air at
    `now_epoch` (start <= now < stop, all epochs; values are cut to whole
    seconds), else None. None also for missing, non-numeric or non-positive
    values. The one join rule of Plugin.playLive, Plugin.playPlaylist and
    Plugin._getPVRItems; Seek_Tolerance and Seek_Threshold stay in
    Plugin._setResume."""
    try:
        start, stop, now_epoch = int(start), int(stop), int(now_epoch)
    except (TypeError, ValueError, OverflowError):
        return None
    if start <= 0 or stop <= 0 or not (start <= now_epoch < stop):
        return None
    return now_epoch - start


def plot_times_match(payload, file):
    """Whether the start/stop of a decoded guide payload (a programme's fitem)
    belong to the requested `file`: the payload's own file -- 'originalfile',
    else 'file', the value its catch-up id carries (xmltvs.getProgramItem) -- is
    exactly `file`. default.py decodes the FOCUSED ListItem.Plot; for a 'Play
    programme' sent by JSON-RPC, a widget or the in-player guide that can be
    another programme's payload, whose times must not decide a join."""
    if not isinstance(payload, dict) or not isinstance(file, str) or not file:
        return False
    own = payload.get('originalfile') or payload.get('file')
    return isinstance(own, str) and own == file
