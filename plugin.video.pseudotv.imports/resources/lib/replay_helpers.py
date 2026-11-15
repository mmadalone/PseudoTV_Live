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

# madteevee (imports fork, imports.59): replay links from a playlist add-on's
# own guide export, merged into that import's guide.
#
# Import-light pure module (same pattern as filter_helpers.py / logo_links.py):
# no Kodi imports, no addon imports except clock_helpers (imports.63, itself
# standard library only), no I/O, Python 3.8 syntax. imports.py, xmltvs.py,
# tasks.py and server.py share one definition and the tests run it without
# Kodi stubs.
#
# Background: Movistar+ (plugin.video.movistarplus) declares link-based
# catch-up in its playlist (catchup="default" catchup-source="{catchup-id}")
# and writes one replay link per show into its own guide export (epg.xml next
# to channels.m3u8). The rich guide (descriptions, pictures...) comes from the
# import's normal guide source; the links come only from that export, whose
# file starts at its own day's 00:00. Older shows exist only in our previous
# pseudotv.xml, so linked history has to be kept there.
#
# The one on/off switch for every replay-link feature is link_replay_days():
# an import uses link replay only while at least one of its live channels has
# link-based catch-up after all catch-up settings are applied. Restoring a
# non-link catch-up row turns the links read, the merge, the kept history, the
# per-channel replay offer, the dashboard messages, the daily export and the
# new-file trigger off together.
#
# imports.70 adds three things here, still pure: the plain guide's episode tags
# (_plain_episode, the last fallback of _episode), the re-describe after a guide
# address change (merge_replay_guide's redescribe) and the guard against a stale
# export (stale_after, stale_export_filter, fill_gaps). not_started is the one
# "not started yet" rule they share: the show on air counts as aired.

import re
import unicodedata
from bisect import bisect_left, bisect_right
from operator import itemgetter

# imports.63: the XMLTV time parser lives in clock_helpers (the add-on's one set
# of time conversions). Re-exported here, the same object, for imports.py,
# server.py, xmltvs.py and the tests.
from clock_helpers import xmltv_to_epoch

# Longest replay history kept for a channel whose replay needs per-show links.
REPLAY_HISTORY_MAX_DAYS = 7

# Add-ons that export their own playlist + guide file into their addon_data
# folder, the guide carrying a replay link (catchup-id) on every show. A code
# table like imports.py KNOWN_CATCHUP_DEFAULTS, not a setting. Read-only: the
# spec dicts are shared, callers must not modify them.
#   name     -- shown in the dashboard messages
#   playlist -- the playlist file name the import points at
#   guide    -- the guide export written next to it (the links file)
#   action   -- the builtin that makes the add-on write a fresh export
#               (what its own settings button and start-up service run)
#   setting  -- the add-on's own on/off setting for its exports
#   catchup  -- (catchup, catchup-source) the add-on is verified to declare
KNOWN_EXPORTERS = {
    'plugin.video.movistarplus': {
        'name':     'Movistar+',
        'playlist': 'channels.m3u8',
        'guide':    'epg.xml',
        'action':   'RunPlugin(plugin://plugin.video.movistarplus/?action=export_epg_now)',
        'setting':  'export_epg',
        'catchup':  ('default', '{catchup-id}'),
    },
}

# Daily export refresh (S13). export_due() makes the time decision; the
# thresholds live here so tasks.py's early-out checks use the same numbers.
EXPORT_CHECK_INTERVAL_S = 5 * 60      # evaluate at most every 5 minutes
EXPORT_MIN_IDLE_S       = 30 * 60     # no input for 30 minutes
EXPORT_MIN_UPTIME_S     = 15 * 60     # the service has run for 15 minutes
EXPORT_MIN_FILE_AGE_S   = 20 * 3600   # the exporter's guide file is 20 h old
EXPORT_MIN_REFIRE_S     = 20 * 3600   # not fired for that import in 20 h

# imports.70 (c): a guide export is too old to trust for the shows that start
# more than this long after it was written (stale_after, stale_export_filter).
# Longer than EXPORT_MIN_FILE_AGE_S, so the daily refresh (S13) asks the add-on
# for a new file well before.
LINKS_STALE_AFTER_S     = 36 * 3600

_ADDON_DATA_RE = re.compile(r'(?:^|/)addon_data/([^/]+)/([^/]+)$')

# Keys that belong to the time slot and its replay link, not to the show.
_SLOT_KEYS = ('start', 'stop', 'channel', 'catchup-id')

# Kodi label markup davidmuma-style guides put in titles: colour-labelled
# season/episode tags (removed with their text) and bold/italic tags.
_LABELS = re.compile(r'\[COLOR [^\]]*\].*?\[/COLOR\]|\[/?[BI]\]', re.S)
_NON_ALNUM = re.compile(r'[^a-z0-9]+')

# imports.59 (S10.3 rule 3): season/episode tags, to tell episodes of one
# show apart. davidmuma puts them in the title as a colour label whose whole
# content is the tag:
#   'Alerta Cobra [COLOR tomato]T22 E9[/COLOR] [COLOR goldenrod]El asesino de reyes[/COLOR]'
#   'La Chaparra [COLOR tomato]E2[/COLOR]'
# The export starts its sub-title with it: 'T22 E9 "El asesino de reyes"',
# 'T1 "Episodio 2"'. Sports seasons are written 'T2025/2026'; the first year
# is the season number. Each optional space run follows a required T or E
# part, so there is one way to match: linear time even on a broken title
# with long space runs (three adjacent optional '\s*' backtracked cubically).
_EPISODE_LABEL = re.compile(r'\[COLOR [^\]]*\]\s*(?:T(\d+)(?:/\d+)?\s*)?(?:E(\d+)\s*)?\[/COLOR\]')
_EPISODE_SUB = re.compile(r'\s*(?:T(\d+)(?:/\d+)?\s*)?(?:E(\d+)\b|"Episodio (\d+)")')

# imports.70: davidmuma's plain guide (since 2026-11-07: no colour labels, no
# sub-titles) ends the show's part of the title with the tag, before an
# optional ' · <episode name>': 'Alerta Cobra T23 E12 · Sangre y agua',
# 'Valle Salvaje T4 E103', 'Horizonte Diarios E40', 'D Corazón T4'. Only the
# text before the first SPACED middle dot is read (Catalan 'Col·lapse' has an
# unspaced one; episode names can hold T/E-like text), a show name must come
# before the tag, and each number has at most 6 digits. One way to match:
# linear time.
_EPISODE_TAIL = re.compile(r'\S\s+(?:T(\d{1,6})(?:/\d{1,6})?(?:\s+E(\d{1,6}))?|E(\d{1,6}))$')
_NAME_SEP = ' \u00b7 '     # a spaced middle dot (U+00B7), written as an escape
# imports.70: what comes before the tag must be a show name, not a season tag:
# in 'T4 E103' or 'T10 E224' on its own, _EPISODE_TAIL's \S is the season's
# last digit, and the episode number alone would be read.
_SEASON_TAG = re.compile(r'^T\d{1,6}(?:/\d{1,6})?$')

_BY_START = itemgetter(0)
_BY_SPAN  = itemgetter(0, 1)


def exporter_for(import_cfg):
    """(addon_id, spec, links_path) when the import's playlist is a known
    exporter's own playlist file, else None.

    Same precedence as Imports.syncOne: m3u_url wins over m3u_path. Only a
    local path matches -- special://... or a plain filesystem path ending in
    .../addon_data/<addon id>/<spec playlist>. Remote playlists (http, smb,
    any other scheme) never match. links_path is the spec's guide file in the
    same folder, spelled the same way as the playlist path."""
    cfg = import_cfg if isinstance(import_cfg, dict) else {}
    source = cfg.get('m3u_url') or cfg.get('m3u_path') or ''
    if not isinstance(source, str):
        return None
    source = source.strip()
    if '://' in source and not source.lower().startswith('special://'):
        return None
    m = _ADDON_DATA_RE.search(source)
    if not m:
        return None
    spec = KNOWN_EXPORTERS.get(m.group(1))
    if not spec or m.group(2) != spec['playlist']:
        return None
    return m.group(1), spec, source[:source.rfind('/') + 1] + spec['guide']


def _text(value):
    return value if isinstance(value, str) else ''


def is_link_catchup(record):
    """True when the channel's replay needs a per-show link: its catch-up
    source holds {catchup-id}, or catch-up 'vod' with no source (IPTV Simple
    then uses {catchup-id} itself)."""
    if not isinstance(record, dict):
        return False
    src = _text(record.get('catchup-source'))
    if '{catchup-id}' in src:
        return True
    return _text(record.get('catchup')) == 'vod' and not src


def replay_days(record):
    """Days of linked history to keep for a live import channel whose replay
    needs links: its catchup-days, at most REPLAY_HISTORY_MAX_DAYS, and the
    maximum when missing, 0 or negative. 0 for every other record (not an
    import, disabled, orphan, unallocated, or not link-based)."""
    if not isinstance(record, dict):
        return 0
    if (record.get('type') != 'import' or not record.get('enabled', True)
            or record.get('is_orphan') or record.get('unallocated')):
        return 0
    if not is_link_catchup(record):
        return 0
    try:
        days = int(str(record.get('catchup-days') or '').strip())
    except ValueError:
        days = 0
    if days <= 0:
        days = REPLAY_HISTORY_MAX_DAYS
    return min(days, REPLAY_HISTORY_MAX_DAYS)


def link_replay_days(records):
    """{channel id: replay days} for the live records whose replay needs
    per-show links (replay_days > 0). An empty result means the import does
    not use link-based replay, which switches every replay-link feature off."""
    out = {}
    for rec in records or ():
        days = replay_days(rec)
        cid = rec.get('id') if days > 0 else None
        if isinstance(cid, str) and cid:
            out[cid] = days
    return out


# madteevee (imports fork, imports.61): a rebuild of one of the user's own
# channels keeps the programmes that have already started. Those channels are
# catchup="vod" with no catchup-days (IPTV Simple's "Channel K"): a guide entry
# can be replayed only while pseudotv.xml still holds it with its catchup-id,
# and Kodi keeps showing past entries from its own copy of the guide. Before,
# a rebuild deleted the whole channel guide, aired programmes included. Times
# are compared as parsed epochs (xmltv_to_epoch), never as text, because the
# programmes carry a ' +HHMM' suffix. Used by XMLTVS.trimBroadcast and
# XMLTVS.resumeAfter (imports.63: Builder._rebuildStart cuts at the real moment
# the new schedule is laid).

def split_for_rebuild(programmes, channel_id, cut):
    """Split the guide for the rebuild of one channel at `cut` (epoch seconds).

    Returns (programmes, kept, removed). Every other channel's programmes and
    every non-dict item are returned unchanged, in their original order. The
    programmes of `channel_id` whose start is before `cut` stay untouched,
    including the one airing at `cut`. Its other programmes are dropped, and so
    is any programme of it whose start or stop can't be parsed (the old full
    clear dropped those too)."""
    out, kept, removed = [], 0, 0
    for programme in programmes or ():
        if not isinstance(programme, dict) or programme.get('channel') != channel_id:
            out.append(programme)
            continue
        start = xmltv_to_epoch(programme.get('start'))
        stop = xmltv_to_epoch(programme.get('stop'))
        if start is not None and stop is not None and start < cut:
            out.append(programme)
            kept += 1
        else:
            removed += 1
    return out, kept, removed


def rebuild_resume(programmes, channel_id, cut, floor=None):
    """Where the new schedule of a rebuilt channel starts (epoch seconds). The
    programme on air at `cut` keeps its full slot: the new schedule starts at
    its stop, the latest stop of the channel's programmes that started before
    `cut`. With nothing on air at `cut` it starts at the later of `floor` and
    that latest stop (`floor` alone when there is none). `floor` defaults to
    `cut` itself, which is the imports.61 rule. imports.63: the Builder cuts at
    the real now and passes the local hour floor, so a guide that ran out
    neither leaves a hole inside the current hour nor overlaps. Programmes that
    can't be parsed are ignored."""
    if floor is None:
        floor = cut
    last = None
    for programme in programmes or ():
        if not isinstance(programme, dict) or programme.get('channel') != channel_id:
            continue
        start = xmltv_to_epoch(programme.get('start'))
        stop = xmltv_to_epoch(programme.get('stop'))
        if start is None or stop is None or start >= cut:
            continue
        if last is None or stop > last:
            last = stop
    if last is not None and last > cut:
        return last
    return floor if last is None else max(floor, last)


def guide_bounds(programmes, channel_ids):
    """{channel id: (first start, last stop)} in epoch seconds for the channels
    in `channel_ids`, in one pass over the guide (imports.63: XMLTVS.loadStopTimes
    and hasProgrammes compare times, not text). Non-dict items, non-text channel
    values and programmes whose start or stop can't be parsed are skipped; a
    channel without a parseable programme is left out."""
    wanted = set(cid for cid in (channel_ids or ()) if isinstance(cid, str))
    bounds = {}
    for programme in programmes or ():
        if not isinstance(programme, dict):
            continue
        channel = programme.get('channel')
        if not isinstance(channel, str) or channel not in wanted:
            continue
        start = xmltv_to_epoch(programme.get('start'))
        stop = xmltv_to_epoch(programme.get('stop'))
        if start is None or stop is None:
            continue
        first_last = bounds.get(channel)
        if first_last is None:
            bounds[channel] = (start, stop)
        else:
            bounds[channel] = (min(first_last[0], start), max(first_last[1], stop))
    return bounds


def _first_text(programme, key):
    """The first text of a programme field: a plain string, or the first
    (text, lang) pair as the XMLTV reader gives it; '' for anything else."""
    value = programme.get(key) or []
    if isinstance(value, str):
        return value
    first = value[0] if isinstance(value, (list, tuple)) and value else ''
    if isinstance(first, (list, tuple)):
        first = first[0] if first else ''
    return _text(first)


def _title_text(programme):
    return _first_text(programme, 'title')


def _norm_title(programme):
    """Title for comparison: Kodi colour labels (davidmuma appends season /
    episode labels) removed, then case, accents and punctuation ignored."""
    text = _LABELS.sub(' ', _title_text(programme))
    text = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode('ascii').casefold()
    return _NON_ALNUM.sub(' ', text).strip()


# imports.66: public names for picture_helpers (the same function objects,
# the same label pattern): norm_title; the first title's text; and that text
# with Kodi colour / bold / italic labels removed.
norm_title = _norm_title
title_text = _title_text


def strip_labels(text):
    """imports.66: `text` with Kodi colour labels (and their contents, e.g.
    davidmuma's season/episode tags) and [B]/[I] markers replaced by a
    space -- the same removal norm_title does."""
    return _LABELS.sub(' ', text)


def _same_title(a, b):
    """Same show title: equal once normalised, or one extends the other."""
    ta, tb = _norm_title(a), _norm_title(b)
    return bool(ta and tb) and (ta == tb or ta.startswith(tb) or tb.startswith(ta))


def _number(digits):
    # imports.70: at most 9 digits. int() refuses more than 4,300 (Python
    # 3.11+), so one broken guide title stopped the whole replay merge.
    return int(digits) if digits and len(digits) <= 9 else None


def _plain_episode(programme):
    """imports.70: (season, episode) of davidmuma's plain tag at the end of the
    show's part of the title (_EPISODE_TAIL), each an int or None; None when
    there is none, and when the text before the tag is empty or is itself a
    season tag (_SEASON_TAG: 'T4 E103' alone has no show name). Reads its own
    label-free copy of the title: the shared title helpers (_LABELS,
    strip_labels, _norm_title, title_text, _same_title) are unchanged."""
    text = _LABELS.sub(' ', _title_text(programme)).split(_NAME_SEP, 1)[0].rstrip()
    m = _EPISODE_TAIL.search(text)
    if not m:
        return None
    # The match starts at the last character of the show name (\S), so the
    # name is everything up to and including it. It is never empty with this
    # pattern; 'not name' keeps the rule explicit should the pattern change.
    name = text[:m.start() + 1].strip()
    if not name or _SEASON_TAG.match(name):
        return None
    return _number(m.group(1)), _number(m.group(2) or m.group(3))


def _episode(programme):
    """(season, episode) of the programme's season/episode tag, each an int
    or None; None when it has neither. The title's first colour label that
    is only such a tag comes first (davidmuma), else the start of the
    sub-title (the export), else a plain tag at the end of the title's show
    part (imports.70, _plain_episode)."""
    for m in _EPISODE_LABEL.finditer(_title_text(programme)):
        if m.group(1) or m.group(2):
            return _number(m.group(1)), _number(m.group(2))
    m = _EPISODE_SUB.match(_first_text(programme, 'sub-title'))
    if m:
        return _number(m.group(1)), _number(m.group(2) or m.group(3))
    return _plain_episode(programme)


def _episodes_agree(a, b):
    """Compare two _episode tags. False when a part present on both differs
    (season or episode). True when both carry an episode number and every
    part present on both is equal. None otherwise: either tag is None, or
    only the seasons can be compared -- a season alone doesn't tell two
    episodes of it apart, so rule 3 then needs its one-to-one check."""
    if a is None or b is None:
        return None
    for x, y in zip(a, b):
        if x is not None and y is not None and x != y:
            return False
    if a[1] is None or b[1] is None:
        return None
    return True


def _has_details(programme):
    """True when the programme carries the guide source's details (a
    description); the export's own entries have a title and sub-title only."""
    for item in programme.get('desc') or ():
        text = item[0] if isinstance(item, (list, tuple)) and item else item
        if isinstance(text, str) and text.strip():
            return True
    return False


def _span(programme):
    """(start, stop) epoch seconds, or None when either is unparseable or the
    programme has no length (half-open interval [start, stop))."""
    start = xmltv_to_epoch(programme.get('start'))
    stop = xmltv_to_epoch(programme.get('stop'))
    if start is None or stop is None or stop <= start:
        return None
    return start, stop


def _has_link(programme):
    link = programme.get('catchup-id')
    return isinstance(link, str) and bool(link.strip())


def _overlaps(slots, starts, start, stop):
    """slots: sorted, non-overlapping (start, stop, ...) tuples; starts: their
    start times. True when [start, stop) overlaps one of them."""
    i = bisect_left(starts, stop)
    return i > 0 and slots[i - 1][1] > start


def _non_overlapping(slots):
    """Sort by (start, stop); keep the first of any overlapping or repeated
    slots. Stable, so for identical spans the earlier entry in `slots` wins."""
    slots.sort(key=_BY_SPAN)
    kept = []
    for slot in slots:
        if kept and slot[0] < kept[-1][1]:
            continue
        kept.append(slot)
    return kept


def _kept_details(programme, start, kept_by_link, kept_by_start):
    """The already-published show (with details) that is the same show as
    the export entry `programme`: it matches on at least two of link, start
    time and title. None when there is no such show."""
    for k_start, kept in kept_by_link.get(programme.get('catchup-id'), ()):
        if k_start == start or _same_title(kept, programme):
            return kept
    for kept in kept_by_start.get(start, ()):
        if _same_title(kept, programme):
            return kept
    return None


def _interval_index(items):
    """Lookup index for _overlapping: (items sorted by start, their start
    times, the longest length). items: (start, stop, ...) tuples, which may
    overlap each other."""
    items = sorted(items, key=_BY_START)
    longest = max((item[1] - item[0] for item in items), default=0)
    return items, [item[0] for item in items], longest


def _overlapping(index, start, stop):
    """The items of an _interval_index that overlap [start, stop). An item
    that ends after `start` started after start - longest, so bisect finds
    the range to check."""
    items, starts, longest = index
    lo = bisect_right(starts, start - longest)
    hi = bisect_left(starts, stop)
    return [item for item in items[lo:hi] if item[1] > start]


def _overlap_details(programme, start, stop, fresh_index, slot_index):
    """Rule 3 of merge_replay_guide: the guide source's programme with the
    details for the time slot [start, stop) of `programme` when the guide
    source has the same show a few minutes off. None when there is no single
    clear match.

    Candidates: the fresh programmes that overlap the slot and have the same
    title (_same_title). One whose tag agrees with the slot's on the episode
    number wins, when it is the only one. With no such tag, the only
    candidate is used when its tag can't be compared or matches on the
    season alone (never when it disagrees) and it overlaps no other slot
    whose show has the same title (one-to-one).
    fresh_index / slot_index: _interval_index of the channel's fresh
    (start, stop, programme) tuples / of its timeline slots."""
    candidates = []
    for item in _overlapping(fresh_index, start, stop):
        if _same_title(item[2], programme):
            candidates.append(item)
    if not candidates:
        return None
    tags = _episode(programme)
    agreeing = []
    for item in candidates:
        if _episodes_agree(tags, _episode(item[2])) is True:
            agreeing.append(item)
    if agreeing:
        return agreeing[0][2] if len(agreeing) == 1 else None
    if len(candidates) != 1:
        return None
    only = candidates[0]
    if _episodes_agree(tags, _episode(only[2])) is not None:
        return None
    for slot in _overlapping(slot_index, only[0], only[1]):
        if slot[0] != start and _same_title(slot[2], only[2]):
            return None
    return only[2]


def _with_slot(details, slot_programme, channel):
    """A copy of `details` (a show's content) placed in the time slot and
    replay link of `slot_programme`. Neither input is modified."""
    out = {}
    for key, value in details.items():
        if key not in _SLOT_KEYS:
            out[key] = value
    out['start'] = slot_programme['start']
    out['stop'] = slot_programme['stop']
    out['channel'] = channel
    out['catchup-id'] = slot_programme['catchup-id']
    return out


def not_started(start, cut):
    """imports.70: the one 'not started yet' rule of the guide re-describe
    (merge_replay_guide's redescribe; cut = now), the stale-export guard
    (stale_export_filter) and its gap fill (fill_gaps; cut = the moment the
    file became too old). True when the show's start (epoch seconds) is after
    `cut`: the show on air at `cut` (start <= cut < stop) counts as started,
    i.e. aired."""
    return start > cut


def stale_after(links_mtime_ns, now):
    """imports.70 (c): the moment (epoch seconds) from which the exporter's
    guide file is too old to trust -- its modification time (st_mtime_ns as
    Imports._readLinks stores it: a decimal string, or an int) plus
    LINKS_STALE_AFTER_S -- once `now` has reached it. None while the file is
    younger, and when its time is unknown (missing, not a whole number, not
    positive, or too large to be a time)."""
    if isinstance(links_mtime_ns, bool):
        return None
    try:
        mtime_ns = int(str(links_mtime_ns).strip())
        cut = mtime_ns / 1e9 + LINKS_STALE_AFTER_S
    except (TypeError, ValueError, OverflowError):
        # OverflowError: a number of hundreds of digits (a damaged stored
        # value) can't become a float; it is no time either.
        return None
    if mtime_ns <= 0:
        return None
    return cut if now >= cut else None


def _covered(spans):
    """imports.70: the time a channel's guide programmes cover, as sorted,
    separate (start, stop) stretches: overlapping or touching spans joined.
    Returns (stretches, their starts)."""
    out = []
    for start, stop in sorted(spans):
        if out and start <= out[-1][1]:
            if stop > out[-1][1]:
                out[-1] = (out[-1][0], stop)
        else:
            out.append((start, stop))
    return out, [s[0] for s in out]


def _inside(covered, span):
    """imports.70: True when [start, stop) lies within one stretch of
    _covered() (None or no stretches: False)."""
    if not covered:
        return False
    stretches, starts = covered
    i = bisect_right(starts, span[0]) - 1
    return i >= 0 and stretches[i][1] >= span[1]


def stale_export_filter(fresh, links, previous, days_by_channel, cut):
    """imports.70 (c): merge_replay_guide's inputs with the schedule of a stale
    exporter file taken out where the guide source covers it. Returns
    (links, previous, stats); none of the inputs is modified.

    cut -- stale_after(): the moment the file became too old to trust.

    Only linked programmes of channels in days_by_channel are judged, and
    only those that had not started at `cut` (not_started) and whose whole
    time the guide source's programmes of that channel cover (one stretch of
    back-to-back or overlapping programmes, _covered/_inside). Everything
    else passes through as the same object: programmes that started by
    `cut`, those the guide source doesn't cover (a channel only the export
    has, a gap, the edges of the guide source's days), other channels, and
    entries without a link or a usable time.

    An export programme (`links`) stays when a guide-source programme agrees
    with it: the same channel, start and stop, the same title (_same_title)
    and an episode tag that doesn't contradict (_episodes_agree is not
    False). Otherwise it is left out.

    An already-published programme (`previous`) is judged by THIS export's
    listing of its link, never by its own title or tag: rule 1 gave it the
    guide source's details when it was published, so comparing it with the
    guide source would always agree. It keeps its link only when the export
    lists that link (same channel) and that listing stayed. Otherwise --
    the listing gave way, or the export no longer lists the link -- it stays
    in `previous` as a copy without its replay link: still a details source
    for rule 2, no longer a time slot. The guide source's programmes then
    fill that time, without links.

    `cut` is fixed for a given file, so a show's outcome doesn't change when
    it airs.

    stats: export_yielded / export_agreeing, history_yielded /
    history_listed, uncovered (not-started programmes kept because the
    guide source doesn't cover them)."""
    stats = {'export_yielded': 0, 'export_agreeing': 0, 'history_yielded': 0,
             'history_listed': 0, 'uncovered': 0}
    twins, spans = {}, {}
    for p in fresh or ():
        ch = p.get('channel')
        if ch in days_by_channel:
            span = _span(p)
            if span is not None:
                twins.setdefault((ch, span[0], span[1]), []).append(p)
                spans.setdefault(ch, []).append(span)
    cover = {ch: _covered(s) for ch, s in spans.items()}

    def _judged(p):
        # (span, None) when the guard leaves p alone (no usable time, or
        # started by the cut); else 'uncovered' or 'covered'.
        span = _span(p)
        if span is None or not not_started(span[0], cut):
            return span, None
        if not _inside(cover.get(p.get('channel')), span):
            return span, 'uncovered'
        return span, 'covered'

    out_links, kept, gave_way = [], set(), set()
    for p in links or ():
        if p.get('channel') in days_by_channel and _has_link(p):
            span, verdict = _judged(p)
            key = (p.get('channel'), p['catchup-id'])
            if verdict == 'covered':
                tags = _episode(p)
                verdict = 'yielded'
                for twin in twins.get((p.get('channel'), span[0], span[1]), ()):
                    if _same_title(twin, p) and _episodes_agree(tags, _episode(twin)) is not False:
                        verdict = 'agreeing'
                        break
            if verdict == 'yielded':
                gave_way.add(key)
                stats['export_yielded'] += 1
                continue
            kept.add(key)
            if verdict == 'agreeing':
                stats['export_agreeing'] += 1
            elif verdict == 'uncovered':
                stats['uncovered'] += 1
        out_links.append(p)
    out_previous = []
    for p in previous or ():
        if p.get('channel') in days_by_channel and _has_link(p):
            verdict = _judged(p)[1]
            if verdict == 'uncovered':
                stats['uncovered'] += 1
            elif verdict == 'covered':
                key = (p.get('channel'), p['catchup-id'])
                if key in kept and key not in gave_way:
                    stats['history_listed'] += 1
                else:
                    stats['history_yielded'] += 1
                    p = {k: v for k, v in p.items() if k != 'catchup-id'}
        out_previous.append(p)
    return out_links, out_previous, stats


def fill_gaps(programmes, fresh, channel_ids, cut):
    """imports.70 (c), after a guarded merge only: the time a guide-source
    programme of a link channel covers that no published programme covers
    any more -- the merge dropped it because it overlaps a slot the guard
    kept (the show on air at `cut`, a slot the guide source doesn't fully
    cover) while the slots next to that one gave way -- is published as a
    copy of that programme without a link, for that time only. Each copy's
    start and stop are existing time texts (its neighbour's stop or start,
    or its own), so no time is computed. Only time after `cut` is filled
    (not_started): before it the guard changed nothing. Returns (programmes
    plus the copies, number of copies); the inputs are not modified."""
    out = list(programmes or ())
    published = set(id(p) for p in out)
    spans = {}
    for p in out:
        ch = p.get('channel')
        span = _span(p) if ch in channel_ids else None
        if span is not None:
            spans.setdefault(ch, []).append((span[0], span[1], p['start'], p['stop']))
    index = {ch: _interval_index(items) for ch, items in spans.items()}
    added = {}
    dropped = []
    for p in fresh or ():
        if id(p) in published or p.get('channel') not in channel_ids:
            continue
        span = _span(p)
        if span is not None and not_started(span[1], cut):
            dropped.append((span[0], span[1], p))
    dropped.sort(key=_BY_SPAN)
    for start, stop, p in dropped:
        ch = p.get('channel')
        near = _overlapping(index[ch], start, stop) if ch in index else []
        near += [a for a in added.get(ch, ()) if a[0] < stop and a[1] > start]
        at, at_text = start, p['start']
        pieces = []
        for s, e, s_text, e_text in sorted(near, key=_BY_SPAN):
            if s > at:
                pieces.append((at, s, at_text, s_text))
            if e > at:
                at, at_text = e, e_text
        if at < stop:
            pieces.append((at, stop, at_text, p['stop']))
        for piece in pieces:
            if not_started(piece[0], cut):
                part = {k: v for k, v in p.items() if k != 'catchup-id'}
                part['start'], part['stop'] = piece[2], piece[3]
                out.append(part)
                added.setdefault(ch, []).append(piece)
    return out, sum(len(a) for a in added.values())


def merge_replay_guide(fresh, links, previous, days_by_channel, now, redescribe=False):
    """Publishable programme list for one import. Returns (programmes, stats).

    fresh    -- the guide source's matched programmes (descriptions,
                pictures...), channel ids already namespaced.
    links    -- the exporter's programmes (each with a replay link), same ids.
    previous -- this import's programmes as loaded from pseudotv.xml.
    days_by_channel -- {channel id: replay days} for live link-based channels
                (link_replay_days).
    now      -- epoch seconds.
    redescribe -- imports.70 (a): True on the first sync that uses a new
                guide address (syncOne's epg_source_changed). A slot that
                hasn't started (not_started(start, now): the show on air
                counts as started) and whose whole time the new guide's
                programmes of that channel cover (_covered/_inside) is
                re-described: it takes only the new guide's details (rules 1
                and 3). Rule 1 is unchanged for every slot; every other slot
                follows today's rules. Slots, times and links never change.

    Per channel in days_by_channel that has links (fresh or kept):
      L = this sync's export programmes for the channel; duplicates and
          overlaps removed, the first kept.
      H = linked programmes already published that overlap no L programme.
      T = L + H within the channel's replay window (stop after now - days).
          Each T programme keeps its own start, stop and link. Its details
          come from the first match of:
            1. the fresh programme of that channel starting at the same second;
            2. for an L programme only: the already-published show with
               details that matches it on at least two of link, start and
               title (_kept_details) -- except a not-started slot inside the
               new guide's coverage during a re-describe;
            3. for a programme without details of its own (every L
               programme; an H programme published titles-only) -- and,
               during a re-describe, for a not-started slot inside the new
               guide's coverage even with details: the fresh
               programme of that channel that overlaps it with the same
               title -- the guide source has the show a few minutes off --
               when its episode number singles it out, or, with no episode
               number to compare, when it is the only one and matches no
               other slot (_overlap_details);
            4. its own entry (H: what was published; L: the export's title).
      Fresh programmes that overlap no T programme are published unchanged,
      without a link (a fresh programme used by rule 3 overlaps its slot, so
      it is not published twice); fresh programmes with an unparseable time
      are kept unchanged too.
    Every other channel's fresh programmes pass through untouched, in their
    original order. All comparisons use half-open epoch intervals, so DST
    changes are safe. None of the inputs is modified.

    Each timeline slot is a (start, stop, programme, from_export) tuple:
    from_export is True for an L slot (this sync's export) and False for an H
    slot (kept history); only L slots may take kept details (rule 2). The
    origin is internal -- the returned programmes carry no extra keys.

    stats: kept_details / overlap_details count the slots that got their
    details from rule 2 / rule 3; both are included in with_content.
    redescribe_slots (imports.70) counts the slots handled in re-describe
    mode (not started, inside the new guide's coverage). It is not the
    number of shows whose description changed: rule 1 gives most of them
    the same details either way.
    """
    stats = {'links_in': 0, 'history_in': 0, 'history_kept': 0, 'linked': 0,
             'with_content': 0, 'plain': 0, 'fresh_kept': 0, 'fresh_dropped': 0,
             'channels': 0, 'kept_details': 0, 'overlap_details': 0,
             'redescribe_slots': 0}
    link_slots = {}
    for p in links or ():
        ch = p.get('channel')
        if ch in days_by_channel and _has_link(p):
            span = _span(p)
            if span:
                link_slots.setdefault(ch, []).append((span[0], span[1], p, True))
                stats['links_in'] += 1
    prev_slots = {}
    prev_details = {}
    for p in previous or ():
        ch = p.get('channel')
        if ch not in days_by_channel:
            continue
        if _has_details(p):
            prev_details.setdefault(ch, []).append(p)
        if _has_link(p):
            span = _span(p)
            if span:
                prev_slots.setdefault(ch, []).append((span[0], span[1], p, False))
                stats['history_in'] += 1

    merge_ids = set(link_slots) | set(prev_slots)
    out = []
    fresh_by_channel = {}
    for p in fresh or ():
        ch = p.get('channel')
        if ch in merge_ids:
            fresh_by_channel.setdefault(ch, []).append(p)
        else:
            out.append(p)

    for ch in sorted(merge_ids):
        stats['channels'] += 1
        window_start = now - days_by_channel[ch] * 86400
        new_slots = _non_overlapping(link_slots.get(ch, []))
        new_starts = [s[0] for s in new_slots]
        timeline = list(new_slots)
        for slot in prev_slots.get(ch, ()):
            if not _overlaps(new_slots, new_starts, slot[0], slot[1]):
                timeline.append(slot)
                stats['history_kept'] += 1
        timeline = _non_overlapping([s for s in timeline if s[1] > window_start])
        starts = [s[0] for s in timeline]

        content = {}
        undated = []
        dated = []
        for p in fresh_by_channel.get(ch, ()):
            span = _span(p)
            if span is None:
                undated.append(p)
                continue
            content.setdefault(span[0], p)
            dated.append((span[0], span[1], p))
        # Details already published for a show the guide source no longer
        # covers (its window moved on): looked up by link and by start.
        kept_by_link = {}
        kept_by_start = {}
        for p in prev_details.get(ch, ()):
            span = _span(p)
            if span is None:
                continue
            if _has_link(p):
                kept_by_link.setdefault(p['catchup-id'], []).append((span[0], p))
            kept_by_start.setdefault(span[0], []).append(p)

        # Rule 3 lookups: the channel's dated fresh programmes and its slots.
        fresh_index = _interval_index(dated)
        slot_index = _interval_index(timeline)
        # imports.70 (a): the time the new guide covers on this channel.
        covered = _covered([(s[0], s[1]) for s in dated]) if redescribe else None

        rows = []
        for start, stop, p, from_export in timeline:
            # imports.70 (a): after a guide address change, a show that hasn't
            # started and whose time the new guide covers takes only the new
            # guide's details (rules 1 and 3). Elsewhere today's rules apply.
            fresh_only = (redescribe and not_started(start, now)
                          and _inside(covered, (start, stop)))
            if fresh_only:
                stats['redescribe_slots'] += 1
            source = content.get(start)
            if source is None and from_export and not fresh_only:       # rule 2: not when fresh_only
                source = _kept_details(p, start, kept_by_link, kept_by_start)
                if source is not None:
                    stats['kept_details'] += 1
            if source is None and (fresh_only or not _has_details(p)):  # rule 3: fresh_only even with details
                source = _overlap_details(p, start, stop, fresh_index, slot_index)
                if source is not None:
                    stats['overlap_details'] += 1
            if source is not None:
                rows.append((start, _with_slot(source, p, ch)))
                stats['with_content'] += 1
            else:
                rows.append((start, p))
                stats['plain'] += 1
            stats['linked'] += 1
        for start, stop, p in dated:
            if _overlaps(timeline, starts, start, stop):
                stats['fresh_dropped'] += 1
            else:
                rows.append((start, p))
                stats['fresh_kept'] += 1
        rows.sort(key=_BY_START)
        out.extend(r[1] for r in rows)
        out.extend(undated)
        stats['fresh_kept'] += len(undated)
    return out, stats


def channels_with_links(programmes, channel_ids, now):
    """{channel id: bool} for each id in channel_ids. The programme on air
    decides; with none on air, the latest one that has started. True when it
    carries a replay link. Channels with no started programme map to False.
    Epoch times."""
    best = {}
    for p in programmes or ():
        ch = p.get('channel')
        if ch not in channel_ids:
            continue
        span = _span(p)
        if span is None or span[0] > now:
            continue
        key = (1 if span[1] > now else 0, span[0])
        cur = best.get(ch)
        if cur is None or key > cur[0]:
            best[ch] = (key, _has_link(p))
    return {ch: (best[ch][1] if ch in best else False) for ch in channel_ids}


def _at_least(value, minimum):
    """True when value is a number >= minimum (None or junk -> False)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return value >= minimum


def export_due(file_age_s, uptime_s, has_media, screensaver_on, idle_s, since_last_fire_s):
    """True when an exporter add-on should write a fresh export now (S13).

    All must hold: nothing loaded in the player (has_media False), the
    screensaver on, no input for EXPORT_MIN_IDLE_S, the service up for
    EXPORT_MIN_UPTIME_S, the exporter's guide file at least
    EXPORT_MIN_FILE_AGE_S old, and not fired for that import within
    EXPORT_MIN_REFIRE_S (since_last_fire_s None = never fired). A file age of
    None (the file is missing or can't be stat'ed) passes the file-age check:
    a missing export is due as well, and the other checks -- the refire one
    included -- still apply, so it is requested at most once per
    EXPORT_MIN_REFIRE_S."""
    if has_media or not screensaver_on:
        return False
    if not _at_least(idle_s, EXPORT_MIN_IDLE_S):
        return False
    if not _at_least(uptime_s, EXPORT_MIN_UPTIME_S):
        return False
    if file_age_s is not None and not _at_least(file_age_s, EXPORT_MIN_FILE_AGE_S):
        return False
    if since_last_fire_s is not None and not _at_least(since_last_fire_s, EXPORT_MIN_REFIRE_S):
        return False
    return True
