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


def _same_title(a, b):
    """Same show title: equal once normalised, or one extends the other."""
    ta, tb = _norm_title(a), _norm_title(b)
    return bool(ta and tb) and (ta == tb or ta.startswith(tb) or tb.startswith(ta))


def _number(digits):
    return int(digits) if digits else None


def _episode(programme):
    """(season, episode) of the programme's season/episode tag, each an int
    or None; None when it has neither. The title's first colour label that
    is only such a tag comes first (davidmuma), else the start of the
    sub-title (the export)."""
    for m in _EPISODE_LABEL.finditer(_title_text(programme)):
        if m.group(1) or m.group(2):
            return _number(m.group(1)), _number(m.group(2))
    m = _EPISODE_SUB.match(_first_text(programme, 'sub-title'))
    if m:
        return _number(m.group(1)), _number(m.group(2) or m.group(3))
    return None


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


def merge_replay_guide(fresh, links, previous, days_by_channel, now):
    """Publishable programme list for one import. Returns (programmes, stats).

    fresh    -- the guide source's matched programmes (descriptions,
                pictures...), channel ids already namespaced.
    links    -- the exporter's programmes (each with a replay link), same ids.
    previous -- this import's programmes as loaded from pseudotv.xml.
    days_by_channel -- {channel id: replay days} for live link-based channels
                (link_replay_days).
    now      -- epoch seconds.

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
               title (_kept_details);
            3. for a programme without details of its own (every L
               programme; an H programme published titles-only): the fresh
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
    """
    stats = {'links_in': 0, 'history_in': 0, 'history_kept': 0, 'linked': 0,
             'with_content': 0, 'plain': 0, 'fresh_kept': 0, 'fresh_dropped': 0,
             'channels': 0, 'kept_details': 0, 'overlap_details': 0}
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

        rows = []
        for start, stop, p, from_export in timeline:
            source = content.get(start)
            if source is None and from_export:
                source = _kept_details(p, start, kept_by_link, kept_by_start)
                if source is not None:
                    stats['kept_details'] += 1
            if source is None and not _has_details(p):
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
