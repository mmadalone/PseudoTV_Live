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

# madteevee (imports fork, imports.66): show pictures in the guide.
#
# Import-light pure module (same pattern as replay_helpers.py /
# rules_helpers.py): no Kodi imports, no I/O, Python 3.8 syntax; standard
# library plus replay_helpers (import-light; it also re-exports
# clock_helpers' XMLTV time parser).
# imports.py and server.py share one definition and the tests run it without
# Kodi stubs.
#
# Background: some guide providers have no picture for a show and answer its
# picture link with one filler image instead (epg.best serves the same
# "BesTivi / Get it on Google Play" PNG for every show on its Spanish
# channels). Kodi shows a programme's <icon> in the guide's info panel
# ($INFO[ListItem.Icon], Estuary MyPVRGuide.xml) and falls back to the
# channel logo only when the programme has none (CFileItem(CPVREpgInfoTag),
# xbmc/FileItem.cpp, Kodi 21). CPVREpgInfoTag::Update copies the icon path,
# so a show re-delivered without one loses the old picture.
#
# Two controls:
#   * per channel: record['show_pictures'] = 'on' | 'off'; absent = 'auto'.
#     Set only through the dashboard (POST /imports/channel/edit.json, which
#     validates with normalize_mode); 'auto' removes the field.
#   * per import: import_cfg['detect_placeholder_pictures']; absent = on
#     (detect_enabled). While on, syncAll samples up to SAMPLES of each
#     channel's show pictures about once a day (MAX_AGE_S) and records whether
#     most of them are one identical image (judge).
# The rule is "most", not "all": a provider's filler stands in only for the
# shows it has no picture for, so the channels it hits are mixed. On
# 2026-11-08 epg.best's advert was the picture of 43 of TV3's 50 titles, 18 of
# MTV's 22 and 41 of À Punt's 42; an "all samples identical" rule missed TV3
# and MTV whenever one real picture was sampled. On channels with real
# pictures the most common one covered at most 3 of 30 titles (59 channels,
# 2026-11-10): in 8 samples, far below PLACEHOLDER_AT (5 of SAMPLES = 8).
# An 'auto' channel hides its pictures only while its import's check is on
# and the channel's last verdict says placeholder. 'on' and 'off' always win.
# Hiding = removing the programme's own top-level 'icon' (strip_pictures);
# nothing else on the programme changes.
#
# Verdicts file: CACHE_LOC/VERDICTS_FILE. One writer (Imports.syncAll),
# readers syncAll and server.py (GET /imports.json); always read through
# valid_verdicts. Format:
#   {channel_id: {"placeholder": true | false,
#                 "checked_at": <epoch seconds, int>,
#                 "samples": <good samples compared, int>}}
# A "can't tell" result (too few shows, failed downloads) is never written:
# the previous verdict stays and the channel is retried at the next sync.

import math
import unicodedata

# xmltv_to_epoch comes through replay_helpers, which re-exports
# clock_helpers' parser (the imports.63 one-clock rule lets only
# replay_helpers / restart_helpers import names from clock_helpers).
from replay_helpers import strip_labels, title_text, xmltv_to_epoch

# Per-channel setting values (record['show_pictures']; absent = 'auto').
MODES = ('auto', 'on', 'off')

# Pictures sampled per channel check: at most SAMPLES distinct shows.
# Placeholder when the most common picture covers at least PLACEHOLDER_AT of
# the pictures read -- or all of them when fewer were read, never fewer than
# MIN_SAMPLES. Real when it covers at least 2 fewer than that. In between (one
# fewer) the previous result stands: a buffer, so a mixed channel doesn't
# flip between logo and filler from one day's sample to the next.
SAMPLES = 8
PLACEHOLDER_AT = 5
MIN_SAMPLES = 3

# Bytes of each picture compared (the first FP_BYTES; different pictures
# differ long before that).
FP_BYTES = 65536

# A channel's verdict is re-checked once it is this old.
MAX_AGE_S = 24 * 3600

# Seconds of picture checks per import per sync; channels left over are
# checked at a later sync.
BUDGET_S = 15

# Verdicts file name, under the add-on's cache folder (CACHE_LOC).
VERDICTS_FILE = 'show_pictures.json'

_OFF_TEXT = frozenset(('false', '0', 'no', 'off'))


def normalize_mode(value):
    """A per-channel setting as sent by the dashboard -> 'auto', 'on' or
    'off'. None and '' mean 'auto'; text is matched without case or
    surrounding spaces. Anything else (other text, numbers, booleans) raises
    ValueError -- the HTTP boundary answers it with 400."""
    if value is None:
        return 'auto'
    if isinstance(value, str):
        text = value.strip().lower()
        if not text:
            return 'auto'
        if text in MODES:
            return text
    raise ValueError("show_pictures must be one of 'auto', 'on', 'off' (got %r)" % (value,))


def _number(value):
    """A finite int/float that isn't a bool, else None (json reads Infinity
    and NaN, which int() and comparisons can't use)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def record_mode(record):
    """The setting stored on a channel record; a value that isn't valid
    (hand-edited file), or a record that isn't a dict, reads as 'auto'
    rather than failing a sync."""
    if not isinstance(record, dict):
        return 'auto'
    try:
        return normalize_mode(record.get('show_pictures'))
    except ValueError:
        return 'auto'


def detect_enabled(import_cfg):
    """The import's automatic check switch. Missing (or None / '', or an
    import entry that isn't a dict) = on. A bool is itself; a number is on
    unless 0; text is off only for 'false', '0', 'no' or 'off' (any case)."""
    if not isinstance(import_cfg, dict):
        return True
    value = import_cfg.get('detect_placeholder_pictures')
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        text = value.strip().lower()
        return not text or text not in _OFF_TEXT
    return True


def _title_key(programme):
    """A show's title for telling shows apart: the first title's text
    (replay_helpers.title_text), Kodi colour / bold / italic labels removed
    (strip_labels, as norm_title does), accents dropped (NFKD, combining
    marks removed), case-folded, and every run of characters other than
    letters and digits -- in any script -- turned into one space. For Latin
    titles this matches norm_title; unlike it (ASCII only), a title in
    Chinese, Cyrillic, Greek, Arabic... keeps its letters, so it is neither
    skipped nor merged with another title that shares only its digits. ''
    (no usable title) for labels or punctuation only."""
    text = unicodedata.normalize('NFKD', strip_labels(title_text(programme)))
    text = ''.join(ch for ch in text if not unicodedata.combining(ch)).casefold()
    return ' '.join(''.join(ch if ch.isalnum() else ' ' for ch in text).split())


def picture_src(programme):
    """The programme's own picture link (its first top-level icon), or ''."""
    icons = programme.get('icon') if isinstance(programme, dict) else None
    if not isinstance(icons, list) or not icons:
        return ''
    first = icons[0]
    src = first.get('src') if isinstance(first, dict) else None
    return src.strip() if isinstance(src, str) else ''


def pick_samples(programmes, channel_id, n=SAMPLES):
    """Up to `n` picture links of `channel_id`'s shows to compare: one per
    distinct show title (_title_key), from each title's earliest airing,
    spread evenly over the channel's guide from its first show to its last
    -- so a run of one series can't fill every sample. Shows without a
    picture, a title or a readable start time are skipped. Links are not
    de-duplicated: shows that share one link count as identical pictures."""
    firsts = {}
    for programme in programmes or ():
        if not isinstance(programme, dict) or programme.get('channel') != channel_id:
            continue
        src = picture_src(programme)
        if not src:
            continue
        title = _title_key(programme)
        if not title:
            continue
        start = xmltv_to_epoch(programme.get('start'))
        if start is None:
            continue
        kept = firsts.get(title)
        if kept is None or (start, src) < kept:
            firsts[title] = (start, src)
    ordered = sorted(firsts.values())
    if n <= 0 or not ordered:
        return []
    if len(ordered) <= n:
        return [src for _start, src in ordered]
    if n == 1:
        return [ordered[0][1]]
    last = len(ordered) - 1
    return [ordered[(i * last) // (n - 1)][1] for i in range(n)]


def _region(read, top):
    """Where `top` (the size of the largest group of identical pictures)
    falls for `read` pictures: 'unknown' (fewer than MIN_SAMPLES read),
    'placeholder', 'real', or 'buffer' (one short of placeholder)."""
    if read < MIN_SAMPLES:
        return 'unknown'
    need = min(PLACEHOLDER_AT, read)
    if top >= need:
        return 'placeholder'
    if top <= need - 2:
        return 'real'
    return 'buffer'


def _counts(fingerprints):
    """(pictures read, size of the largest identical group); failed reads
    (None / '') don't count."""
    groups = {}
    for fp in fingerprints:
        if fp:
            groups[fp] = groups.get(fp, 0) + 1
    return sum(groups.values()), max(groups.values()) if groups else 0


def judge(fingerprints, previous=None):
    """A channel's result from its sampled pictures' fingerprints; failed
    reads (None / '') are ignored. True = placeholder (the most common
    picture covers at least PLACEHOLDER_AT of those read, or all of them when
    fewer were read); False = real (it covers at least 2 fewer); in the
    buffer between, the previous result (`previous`: True, False or None for
    none yet -> False). None when fewer than MIN_SAMPLES were read: can't
    tell, the previous result should stand."""
    region = _region(*_counts(fingerprints))
    if region == 'unknown':
        return None
    if region == 'buffer':
        return previous is True
    return region == 'placeholder'


def settled(fingerprints, remaining, previous=None):
    """The result judge will give for these fingerprints whatever the
    `remaining` samples not read yet turn out to be (the same picture as the
    largest group, new pictures, or failed reads), or None while they can
    still change it -- so a check can stop reading early. Placeholder is
    certain when even `remaining` new pictures keep it there; real is certain
    when even `remaining` copies of the largest group keep it real, and it is
    real already. A result in the buffer is never settled early."""
    read, top = _counts(fingerprints)
    remaining = max(0, int(remaining))
    if read < MIN_SAMPLES:
        return None
    if _region(read + remaining, top) == 'placeholder':
        return True
    if _region(read, top) == 'real' and _region(read + remaining, top + remaining) == 'real':
        return False
    return None


def make_verdict(placeholder, now, samples):
    """One verdicts-file entry."""
    return {'placeholder': bool(placeholder), 'checked_at': int(now), 'samples': int(samples)}


def valid_verdicts(data):
    """The usable entries of a loaded verdicts file ({} for anything that
    isn't a dict). An entry needs a text channel id, a bool 'placeholder'
    and a finite numeric 'checked_at'; anything else is dropped, so one bad
    entry never costs the others."""
    out = {}
    if not isinstance(data, dict):
        return out
    for cid, verdict in data.items():
        if not isinstance(cid, str) or not cid or not isinstance(verdict, dict):
            continue
        at = _number(verdict.get('checked_at'))
        if not isinstance(verdict.get('placeholder'), bool):
            continue
        if at is None:
            continue
        samples = verdict.get('samples')
        if isinstance(samples, bool) or not isinstance(samples, int):
            samples = 0
        out[cid] = {'placeholder': verdict['placeholder'], 'checked_at': int(at), 'samples': samples}
    return out


def check_due(verdict, now, max_age=MAX_AGE_S):
    """True when a channel's pictures should be checked: no usable verdict,
    or one at least `max_age` seconds old, or dated in the future (the clock
    was moved back)."""
    if not isinstance(verdict, dict):
        return True
    at = _number(verdict.get('checked_at'))
    if at is None:
        return True
    age = now - at
    return age < 0 or age >= max_age


def due_order(channel_ids, verdicts):
    """Channel ids in checking order: never checked first, then the oldest
    check; ties by id, so the order is stable."""
    def _key(cid):
        verdict = verdicts.get(cid) if isinstance(verdicts, dict) else None
        at = _number(verdict.get('checked_at')) if isinstance(verdict, dict) else None
        if at is None:
            return (0, 0, cid)
        return (1, at, cid)
    return sorted(channel_ids, key=_key)


def pictures_hidden(record, verdict, detect_on):
    """Whether a channel's show pictures are removed from the guide:
    'off' -> always; 'on' -> never; 'auto' -> only while the import's check
    is on and the channel's last verdict says placeholder."""
    mode = record_mode(record)
    if mode == 'off':
        return True
    if mode == 'on':
        return False
    return bool(detect_on) and isinstance(verdict, dict) and verdict.get('placeholder') is True


def strip_pictures(programmes, hidden_ids):
    """Remove the programme's own top-level 'icon' from every programme whose
    channel is in `hidden_ids`, in place. Returns how many lost a picture
    (an empty icon list, which the XMLTV reader makes for an <icon> without
    a src, is removed too but not counted). Everything else on a programme
    (ratings' own icons included) is kept."""
    if not hidden_ids:
        return 0
    removed = 0
    for programme in programmes or ():
        if isinstance(programme, dict) and programme.get('channel') in hidden_ids and 'icon' in programme:
            if programme.pop('icon'):
                removed += 1
    return removed
