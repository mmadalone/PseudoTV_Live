# -*- coding: utf-8 -*-
"""imports.70 (c): the guard against a stale Movistar+ export (variant B, 36 h).

Movistar+ writes its guide file with the replay links once a day. The merge
turned every show of that file into a fixed time slot that beat the guide
source (davidmuma), so when the file got old its planned schedule -- 12-hour
placeholders included -- stayed on the TV for its whole week.

imports.70: once the file is LINKS_STALE_AFTER_S (36 h) old, the moment it
turned that old is the cut (stale_after). Before the merge,
stale_export_filter takes out the linked shows that had not started at the
cut (not_started: the show on air counts as aired) and whose whole time the
guide source covers on that channel (joined stretches, _covered/_inside).
An export show stays only when the guide source lists it the same: channel,
start, stop, title, and no clashing episode. A show already published is
judged by this export's listing of its link (never by its own title: rule 1
gave it the guide's details), and when it gives way it stays as a copy
without its link (a details source, no longer a slot). Squirrel-like
channels, gaps and the guide's edges keep the export's schedule. After the
merge, fill_gaps publishes the rest of a guide show that a kept slot cut
short, without a link, so the hand-over leaves no hole.

Sections (pure: no Kodi stubs):
  - the threshold, stale_after, not_started, _covered/_inside;
  - the filter: upcoming vs on air, agreeing, coverage, published copies;
  - the gap fill;
  - whole guarded merges: a 12-hour placeholder, a fixed cut, inputs;
  - the re-describe (a) and the guard (c) in one sync;
  - time zones and mixed offsets.
Sections (Imports.syncAll, on the imports70_sync_rig harness: the export
aged with os.utime, every time built from the moment the test starts):
  - the guard in a real sync: 40 h, an on-air show that gives way (S9),
    two syncs, recovery after a new file, (a) and (c) together, 30 h, the
    keep-previous path, a failed guide download and its saved copy's age;
  - the "went stale" trigger in syncAll's gate, with the hourly sync while
    the file stays stale (the user's choice of 2026-11-14), and its units:
    _linksWentStale, _linksFileState, _staleCut;
  - the dashboard message from 36 h (_ageText, LINKS_MSG_STALE) and its
    place in the order of the file messages.
  - Python 3.8 checks of the changed files.
All data is synthetic.
"""
import ast
import os
import time

import pytest
import xbmc
import xbmcvfs

import imports as imports_mod
import imports70_sync_rig as sr
import replay_helpers as rh
from imports import Imports
from replay_helpers import xmltv_to_epoch as E


HERE = os.path.dirname(os.path.abspath(__file__))
LIB  = os.path.normpath(os.path.join(HERE, '..', 'resources', 'lib'))

CH   = 'C1@mp'
SQ   = 'C2@mp'          # Squirrel-like: the export has it, the guide source doesn't
NOW  = 1800000000       # 2027-01-15 08:00 UTC -- fixed so tests are deterministic
H    = 3600
M    = 60
CUT  = NOW - 30 * M     # the export turned 36 h old half an hour before this sync
DAYS = {CH: 7, SQ: 7}


def _src(name):
    with open(os.path.join(LIB, name), encoding='utf-8') as f:
        return f.read()


def _ts(epoch, offset_min=0):
    """XMLTV time for `epoch`, written with a +HHMM / -HHMM offset."""
    sign = '+' if offset_min >= 0 else '-'
    off = abs(offset_min)
    return (time.strftime('%Y%m%d%H%M%S', time.gmtime(epoch + offset_min * 60))
            + ' %s%02d%02d' % (sign, off // 60, off % 60))


def _prog(start, stop, title='Show', ch=CH, link=None, desc=None, sub=None, offset_min=0, **extra):
    p = {'channel': ch, 'start': _ts(start, offset_min), 'stop': _ts(stop, offset_min),
         'title': [(title, 'es')]}
    if link is not None:
        p['catchup-id'] = link
    if desc is not None:
        p['desc'] = [(desc, 'es')]
    if sub is not None:
        p['sub-title'] = [(sub, 'es')]
    p.update(extra)
    return p


def _filter(fresh=(), links=(), previous=(), cut=CUT, days=None):
    return rh.stale_export_filter(list(fresh), list(links), list(previous), DAYS if days is None else days, cut)


def _guarded(fresh=(), links=(), previous=(), cut=CUT, now=NOW, days=None, redescribe=False):
    """The guarded fresh-path sequence of the sync (imports.70): the filter on
    the merge's inputs, the merge, then the gap fill on its output."""
    days = DAYS if days is None else days
    fresh = list(fresh)
    links, previous, guard = rh.stale_export_filter(fresh, list(links), list(previous), days, cut)
    out, stats = rh.merge_replay_guide(fresh, links, previous, days, now, redescribe=redescribe)
    out, filled = rh.fill_gaps(out, fresh, days, cut)
    return out, stats, guard, filled


def _title(p):
    return p['title'][0][0]


def _desc(p):
    return p['desc'][0][0] if p.get('desc') else None


def _links(out):
    return [p['catchup-id'] for p in sorted(out, key=lambda p: E(p['start'])) if p.get('catchup-id')]


def _holes(out, ch, start, stop):
    """The stretches of [start, stop) that no published programme of `ch` covers."""
    spans = sorted((E(p['start']), E(p['stop'])) for p in out if p.get('channel') == ch)
    holes, at = [], start
    for s, e in spans:
        if s > at and at < stop:
            holes.append((at, min(s, stop)))
        at = max(at, e)
    if at < stop:
        holes.append((at, stop))
    return holes


def _overlaps(out, ch):
    spans = sorted((E(p['start']), E(p['stop'])) for p in out if p.get('channel') == ch)
    return [(a, b) for a, b in zip(spans, spans[1:]) if b[0] < a[1]]


NO_GUARD = {'export_yielded': 0, 'export_agreeing': 0, 'history_yielded': 0, 'history_listed': 0,
            'uncovered': 0}


def _stats(**over):
    out = dict(NO_GUARD)
    out.update(over)
    return out


# ================================================================ the threshold and the cut


def test_the_threshold():
    """36 h, longer than the daily refresh's 20 h (S13 asks Movistar+ for a
    new file well before the guard switches on). Not part of the S13 tuple."""
    assert rh.LINKS_STALE_AFTER_S == 36 * 3600
    assert rh.LINKS_STALE_AFTER_S > rh.EXPORT_MIN_FILE_AGE_S


T_FILE  = 1790000000            # the file's modification time
NS_FILE = T_FILE * 10 ** 9      # as Imports._readLinks stores it (st_mtime_ns)
T_CUT   = T_FILE + 36 * 3600


@pytest.mark.parametrize('value', [str(NS_FILE), NS_FILE, ' %d ' % NS_FILE], ids=['str', 'int', 'spaces'])
def test_stale_after(value):
    assert rh.stale_after(value, T_CUT) == T_CUT                 # the boundary: now == cut
    assert rh.stale_after(value, T_CUT + 5 * H) == T_CUT         # the same cut at later syncs
    assert rh.stale_after(value, T_CUT - 1) is None              # younger than 36 h
    assert rh.stale_after(value, T_FILE) is None


def test_stale_after_keeps_the_fraction_of_a_second():
    assert rh.stale_after(str(NS_FILE + 500000000), T_CUT + 1) == T_CUT + 0.5


@pytest.mark.parametrize('value', [None, '', 'abc', True, False, '0', 0, '-5', -5, '1.7e18', 1.7e18,
                                   '12 34', '9' * 400, [], {}])
def test_stale_after_unknown_time(value):
    """Missing, not a whole number, not positive, or too large to be a time
    (a damaged stored value must not stop the merge): the guard stays off."""
    assert rh.stale_after(value, T_CUT + 400 * 86400) is None


def test_not_started_on_air_counts_as_aired():
    assert rh.not_started(CUT + 1, CUT) is True
    assert rh.not_started(CUT, CUT) is False
    assert rh.not_started(CUT - 1, CUT) is False


def test_covered_and_inside():
    cov = rh._covered([(10, 20), (20, 30), (25, 27), (40, 50), (5, 12)])
    assert cov == ([(5, 30), (40, 50)], [5, 40]), 'overlapping and touching spans are joined'
    for span in ((5, 30), (12, 28), (40, 50), (41, 49)):
        assert rh._inside(cov, span), span
    for span in ((29, 41), (0, 6), (45, 51), (30, 40), (60, 70)):
        assert not rh._inside(cov, span), span
    assert not rh._inside(None, (5, 6))
    assert not rh._inside(rh._covered([]), (5, 6))


# ================================================================ the filter


def test_an_upcoming_export_slot_gives_way_one_on_air_at_the_cut_is_kept():
    fresh = [_prog(CUT - 2 * H, CUT + 30 * M, 'Guia uno'), _prog(CUT + 30 * M, CUT + 3 * H, 'Guia dos')]
    links = [_prog(CUT - H, CUT + 30 * M, 'On air', link='a'),      # on air at the cut
             _prog(CUT + 30 * M, CUT + H, 'From the cut', link='b'),
             _prog(CUT + H, CUT + 2 * H, 'Upcoming', link='c')]
    out_links, out_prev, stats = _filter(fresh, links)
    assert [p['catchup-id'] for p in out_links] == ['a']
    assert out_links[0] is links[0]
    assert stats == _stats(export_yielded=2)


def test_a_slot_starting_at_the_cut_has_started():
    fresh = [_prog(CUT - H, CUT + 3 * H, 'Guia')]
    links = [_prog(CUT, CUT + H, 'At the cut', link='a'), _prog(CUT + 1, CUT + H, 'A second later', link='b')]
    out_links, _, stats = _filter(fresh, links)
    assert [p['catchup-id'] for p in out_links] == ['a']
    assert stats == _stats(export_yielded=1)


def test_only_an_agreeing_export_slot_keeps_its_link():
    """Same channel, start, stop and title, and episodes that don't clash."""
    fresh = [_prog(CUT + H, CUT + 2 * H, 'Agree'),
             _prog(CUT + 2 * H, CUT + 3 * H, 'Stop'),
             _prog(CUT + 3 * H, CUT + 4 * H, 'Show Y'),
             _prog(CUT + 4 * H, CUT + 5 * H, 'Valle Salvaje T4 E103 · x'),
             _prog(CUT + 5 * H, CUT + 6 * H, 'Valle Salvaje T4 E104 · y'),
             _prog(CUT + 6 * H, CUT + 7 * H, 'Cine de noche')]
    links = [_prog(CUT + H, CUT + 2 * H, 'AGREE', link='l1'),                                     # case ignored
             _prog(CUT + 2 * H, CUT + 2 * H + 50 * M, 'Stop', link='l2'),                         # another stop
             _prog(CUT + 3 * H, CUT + 4 * H, 'Show X', link='l3'),                                # another title
             _prog(CUT + 4 * H, CUT + 5 * H, 'Valle Salvaje', link='l4', sub='T4 "Episodio 101"'),  # another episode
             _prog(CUT + 5 * H, CUT + 6 * H, 'Valle Salvaje', link='l5', sub='T4 "Episodio 104"'),  # the same one
             _prog(CUT + 6 * H, CUT + 7 * H, 'Cine de noche', link='l6', sub='')]                 # nothing to compare
    out_links, _, stats = _filter(fresh, links)
    assert [p['catchup-id'] for p in out_links] == ['l1', 'l5', 'l6']
    assert stats == _stats(export_yielded=3, export_agreeing=3)


def test_a_twin_on_another_channel_doesnt_count():
    fresh = [_prog(CUT, CUT + 3 * H, 'Guia'), _prog(CUT + H, CUT + 2 * H, 'Show', ch='C3@mp')]
    links = [_prog(CUT + H, CUT + 2 * H, 'Show', link='l1')]
    out_links, _, stats = _filter(fresh, links, days={CH: 7, 'C3@mp': 7})
    assert out_links == [] and stats == _stats(export_yielded=1)


def test_slots_the_guide_doesnt_cover_keep_the_exports_schedule():
    fresh = [_prog(CUT + H, CUT + 2 * H, 'G1'), _prog(CUT + 2 * H, CUT + 3 * H, 'G2')]
    links = [_prog(CUT + 30 * M, CUT + H + 30 * M, 'Before the first start', link='e1'),
             _prog(CUT + 2 * H + 30 * M, CUT + 3 * H + 30 * M, 'Across the last stop', link='e2'),
             _prog(CUT + 4 * H, CUT + 5 * H, 'Beyond', link='e3'),
             _prog(CUT + H, CUT + 2 * H, 'Ardilla', ch=SQ, link='sq1')]
    out_links, _, stats = _filter(fresh, links)
    assert len(out_links) == 4 and all(a is b for a, b in zip(out_links, links))
    assert stats == _stats(uncovered=4)


def test_a_hole_in_the_guide_keeps_the_slot_touching_programmes_are_one_stretch():
    """Coverage is joined stretches, not first-to-last: the live guide had a
    60-second hole on one channel."""
    fresh = [_prog(CUT + H, CUT + 2 * H, 'G1'),
             _prog(CUT + 2 * H + M, CUT + 3 * H, 'G2'),
             _prog(CUT + 3 * H, CUT + 4 * H, 'G3')]
    links = [_prog(CUT + H + 30 * M, CUT + 2 * H + 30 * M, 'Across the hole', link='h'),
             _prog(CUT + 2 * H + 30 * M, CUT + 3 * H + 30 * M, 'Across the touch', link='t')]
    out_links, _, stats = _filter(fresh, links)
    assert [p['catchup-id'] for p in out_links] == ['h']
    assert stats == _stats(export_yielded=1, uncovered=1)


def test_entries_the_guard_leaves_alone_pass_through_as_the_same_objects():
    fresh = [_prog(CUT - H, CUT + 4 * H, 'Guia')]
    links = [_prog(CUT + H, CUT + 2 * H, 'No link'),                              # no link
             _prog(CUT + H, CUT + 2 * H, 'Blank', link='  '),
             {'channel': CH, 'start': 'garbage', 'stop': None, 'catchup-id': 'u'},  # no usable time
             _prog(CUT + H, CUT + 2 * H, 'Other', ch='X@other', link='o')]          # not a link channel
    previous = [dict(p) for p in links]
    out_links, out_prev, stats = _filter(fresh, links, previous)
    assert len(out_links) == 4 and all(a is b for a, b in zip(out_links, links))
    assert len(out_prev) == 4 and all(a is b for a, b in zip(out_prev, previous))
    assert stats == NO_GUARD


def _published(fresh, links, now):
    """What an earlier sync with a fresh export published: rule 1 gives each
    export slot the guide show that starts at the same second."""
    out, _ = rh.merge_replay_guide(list(fresh), list(links), [], DAYS, now)
    return out


@pytest.mark.parametrize('export,guide', [
    (dict(title='Show X'), 'Show Y'),                                         # another title
    (dict(title='Valle Salvaje', sub='T4 E101'), 'Valle Salvaje T4 E103'),    # another episode
], ids=['title', 'episode'])
def test_a_published_copy_is_judged_by_the_exports_listing(export, guide):
    """The copy carries the guide's title (rule 1), so judging it by its own
    title would compare the guide with itself and keep the stale link."""
    fresh = [_prog(CUT - H, CUT + H, 'Antes', desc='antes'),
             _prog(CUT + H, CUT + 2 * H, guide, desc='guide details'),
             _prog(CUT + 2 * H, CUT + 3 * H, 'Despues', desc='despues')]
    links = [_prog(CUT + H, CUT + 2 * H, link='x', **export)]
    published = _published(fresh, links, CUT - 10 * H)
    copy = [p for p in published if p.get('catchup-id') == 'x']
    assert len(copy) == 1 and _title(copy[0]) == guide, 'rule 1: the guide\'s show with the export\'s link'
    out, _, guard, filled = _guarded(fresh, links, published)
    assert 'x' not in _links(out)
    assert guard == _stats(export_yielded=1, history_yielded=1)
    at = [p for p in out if p.get('channel') == CH and E(p['start']) == CUT + H]
    assert len(at) == 1 and _title(at[0]) == guide and _desc(at[0]) == 'guide details'
    assert not at[0].get('catchup-id')
    assert _holes(out, CH, CUT, CUT + 3 * H) == [] and _overlaps(out, CH) == [] and filled == 0


def test_a_published_copy_whose_listing_agrees_keeps_its_link():
    fresh = [_prog(CUT - H, CUT + H, 'Antes', desc='antes'),
             _prog(CUT + H, CUT + 2 * H, 'Show Y', desc='guide details'),
             _prog(CUT + 2 * H, CUT + 3 * H, 'Despues', desc='despues')]
    links = [_prog(CUT + H, CUT + 2 * H, 'Show Y', link='x', sub='T1 "Episodio 3"')]
    published = _published(fresh, links, CUT - 10 * H)
    out, _, guard, _ = _guarded(fresh, links, published)
    kept = [p for p in out if p.get('catchup-id') == 'x']
    assert len(kept) == 1 and _desc(kept[0]) == 'guide details'
    assert (kept[0]['start'], kept[0]['stop']) == (_ts(CUT + H), _ts(CUT + 2 * H))
    assert guard == _stats(export_agreeing=1, history_listed=1)


def test_a_published_copy_the_export_no_longer_lists_loses_its_link():
    fresh = [_prog(CUT, CUT + 3 * H, 'Guia', desc='g')]
    previous = [_prog(CUT + H, CUT + 2 * H, 'Old', link='gone', desc='old')]
    links = [_prog(CUT - H, CUT, 'Aired', link='other')]
    _, out_prev, stats = _filter(fresh, links, previous)
    assert 'catchup-id' not in out_prev[0]
    assert stats == _stats(history_yielded=1)


def test_a_published_copy_listed_on_another_channel_loses_its_link():
    fresh = [_prog(CUT, CUT + 3 * H, 'Guia', desc='g')]
    previous = [_prog(CUT + H, CUT + 2 * H, 'Old', link='same', desc='old')]
    links = [_prog(CUT + H, CUT + 2 * H, 'Ardilla', ch=SQ, link='same')]      # kept: uncovered
    _, out_prev, stats = _filter(fresh, links, previous)
    assert 'catchup-id' not in out_prev[0]
    assert stats == _stats(history_yielded=1, uncovered=1)


def test_a_published_copy_loses_only_its_link_an_aired_one_is_untouched():
    fresh = [_prog(CUT - 3 * H, CUT + 3 * H, 'Guia', desc='g')]
    aired = _prog(CUT - 2 * H, CUT - H, 'Aired', link='a1', desc='old aired', icon=[{'src': 'p'}])
    on_air = _prog(CUT - 30 * M, CUT + 30 * M, 'On air', link='a2', desc='old on air')
    upcoming = _prog(CUT + H, CUT + 2 * H, 'Upcoming', link='a3', desc='old up', icon=[{'src': 'q'}],
                     **{'sub-title': [('T1 E2', 'es')]})
    uncovered = _prog(CUT + 4 * H, CUT + 5 * H, 'Later', link='a4', desc='old later')
    previous = [aired, on_air, upcoming, uncovered]
    snapshot = repr(previous)
    _, out_prev, stats = _filter(fresh, [], previous)
    assert out_prev[0] is aired and out_prev[1] is on_air and out_prev[3] is uncovered
    expected = dict(upcoming)
    del expected['catchup-id']
    assert out_prev[2] == expected and out_prev[2] is not upcoming
    assert repr(previous) == snapshot, 'the input is not modified'
    assert stats == _stats(history_yielded=1, uncovered=1)


# ================================================================ the gap fill


def test_gap_fill_after_the_slot_on_air_at_the_cut():
    """Export A on air at the cut, B and C after it; the guide's P2 runs past
    A's stop. Without the fill [cut+2h, cut+2h30) was a hole. The piece's
    times are existing texts: A's stop (+0000) and P2's own stop (+0100)."""
    links = [_prog(CUT - H, CUT + 2 * H, 'A', link='a'),
             _prog(CUT + 2 * H, CUT + 3 * H, 'B', link='b'),
             _prog(CUT + 3 * H, CUT + 4 * H, 'C', link='c')]
    fresh = [_prog(CUT - 90 * M, CUT + H, 'P1', desc='p1', offset_min=60),
             _prog(CUT + H, CUT + 2 * H + 30 * M, 'P2', desc='p2', offset_min=60, icon=[{'src': 'p2'}]),
             _prog(CUT + 2 * H + 30 * M, CUT + 4 * H, 'P3', desc='p3', offset_min=60)]
    out, _, guard, filled = _guarded(fresh, links, [])
    assert guard == _stats(export_yielded=2) and filled == 1
    assert _links(out) == ['a']
    piece = [p for p in out if _title(p) == 'P2']
    assert len(piece) == 1
    piece = piece[0]
    assert piece['start'] == links[0]['stop'] and piece['stop'] == fresh[1]['stop']
    assert 'catchup-id' not in piece and _desc(piece) == 'p2' and piece['icon'] == [{'src': 'p2'}]
    assert _holes(out, CH, CUT, CUT + 4 * H) == [] and _overlaps(out, CH) == []


def test_gap_fill_before_a_slot_crossing_the_guides_last_stop():
    fresh = [_prog(CUT, CUT + 2 * H, 'G1', desc='g1'), _prog(CUT + 2 * H, CUT + 4 * H, 'G2', desc='g2')]
    links = [_prog(CUT + H, CUT + 3 * H, 'E1', link='e1'),            # covered, no twin: gives way
             _prog(CUT + 3 * H, CUT + 5 * H, 'E2', link='e2')]        # crosses the guide's last stop: kept
    out, _, guard, filled = _guarded(fresh, links, [])
    assert guard == _stats(export_yielded=1, uncovered=1) and filled == 1
    piece = [p for p in out if _title(p) == 'G2']
    assert len(piece) == 1
    assert (piece[0]['start'], piece[0]['stop']) == (fresh[1]['start'], links[1]['start'])
    assert _holes(out, CH, CUT, CUT + 5 * H) == [] and _overlaps(out, CH) == []


def test_gap_fill_around_a_slot_in_an_interior_gap_of_the_guide():
    fresh = [_prog(CUT + H, CUT + 2 * H, 'G1', desc='g1'), _prog(CUT + 2 * H + 10 * M, CUT + 4 * H, 'G2', desc='g2')]
    links = [_prog(CUT + H, CUT + H + 30 * M, 'W', link='w'),
             _prog(CUT + H + 30 * M, CUT + 2 * H + 30 * M, 'X', link='x'),      # spans the gap: kept
             _prog(CUT + 2 * H + 30 * M, CUT + 4 * H, 'Z', link='z')]
    out, _, guard, filled = _guarded(fresh, links, [])
    assert guard == _stats(export_yielded=2, uncovered=1) and filled == 2
    assert _links(out) == ['x']
    g1 = [p for p in out if _title(p) == 'G1']
    g2 = [p for p in out if _title(p) == 'G2']
    assert [(p['start'], p['stop']) for p in g1] == [(fresh[0]['start'], links[1]['start'])]   # clipped to X's start
    assert [(p['start'], p['stop']) for p in g2] == [(links[1]['stop'], fresh[1]['stop'])]     # from X's stop
    assert _holes(out, CH, CUT + H, CUT + 4 * H) == [] and _overlaps(out, CH) == []


@pytest.mark.parametrize('fresh_span,link_span,hole', [
    # the kept slot is on air at the cut; the guide show started before it
    ((CUT - 2 * H, CUT + H), (CUT - 30 * M, CUT + H), (CUT - 2 * H, CUT - 30 * M)),
    # the kept slot ends at the cut and the export has nothing after it
    ((CUT - H, CUT + H), (CUT - 2 * H, CUT), (CUT, CUT + H)),
], ids=['before-the-cut', 'at-the-cut'])
def test_no_piece_starts_before_or_at_the_cut(fresh_span, link_span, hole):
    """Up to the cut the guard changed nothing (the show on air counts as
    aired): a hole that starts there is not the guard's, as today."""
    fresh = [_prog(fresh_span[0], fresh_span[1], 'Long', desc='l')]
    links = [_prog(link_span[0], link_span[1], 'Kept', link='k')]
    out, _, guard, filled = _guarded(fresh, links, [])
    assert filled == 0 and guard == NO_GUARD
    assert _holes(out, CH, min(fresh_span[0], link_span[0]), CUT + H) == [hole]


def test_fill_adds_nothing_when_nothing_was_dropped():
    fresh = [_prog(CUT + H, CUT + 2 * H, 'G1'), _prog(CUT + 3 * H, CUT + 4 * H, 'G2')]
    out, filled = rh.fill_gaps(fresh, fresh, DAYS, CUT)
    assert filled == 0 and out == fresh and out is not fresh


def test_fill_ignores_other_channels_and_unusable_times():
    fresh = [_prog(CUT + H, CUT + 2 * H, 'Other', ch='X@other'),
             {'channel': CH, 'start': 'garbage', 'stop': None, 'title': [('Bad', '')]}]
    out, filled = rh.fill_gaps([], fresh, DAYS, CUT)
    assert out == [] and filled == 0


def test_fill_doesnt_add_two_pieces_for_one_time():
    """Two dropped guide programmes that overlap each other: the second one's
    time already filled by the first is not added again."""
    kept = _prog(CUT - H, CUT + H, 'Kept', link='k')
    fresh = [_prog(CUT - H, CUT + 2 * H, 'Long', desc='l'), _prog(CUT + 30 * M, CUT + 3 * H, 'Overlapping', desc='o')]
    out, filled = rh.fill_gaps([kept], fresh, DAYS, CUT)
    assert filled == 2
    assert [(E(p['start']), E(p['stop']), _title(p)) for p in out[1:]] == \
        [(CUT + H, CUT + 2 * H, 'Long'), (CUT + 2 * H, CUT + 3 * H, 'Overlapping')]
    assert _overlaps(out, CH) == []


# ================================================================ whole guarded merges


def test_a_12_hour_placeholder_gives_way_to_the_guides_13_shows():
    """The stale file's 12 h 'Top Mecanic' block shares its start and title
    with the guide's first show, but not its stop: it gives way, the guide's
    13 shows are published without links, and the aired link stays."""
    start = CUT + H
    fresh, t = [], start
    for i, length in enumerate([55 * M] * 12 + [60 * M]):
        fresh.append(_prog(t, t + length, 'Top Mecanic' if i == 0 else 'Show %d' % i, desc='d%d' % i))
        t += length
    assert t == start + 12 * H
    fresh += [_prog(CUT - 3 * H, CUT - 2 * H, 'Aired', desc='aired details'),
              _prog(CUT - 2 * H, start, 'Antes', desc='antes')]
    links = [_prog(CUT - 3 * H, CUT - 2 * H, 'Aired', link='aired'),
             _prog(start, start + 12 * H, 'Top Mecanic', link='placeholder')]
    out, stats, guard, filled = _guarded(fresh, links, [])
    assert _links(out) == ['aired']
    assert _desc([p for p in out if p.get('catchup-id') == 'aired'][0]) == 'aired details'
    shows = [p for p in out if not p.get('catchup-id') and start <= E(p['start']) < start + 12 * H]
    assert len(shows) == 13
    assert guard == _stats(export_yielded=1) and filled == 0
    assert _holes(out, CH, CUT - 3 * H, start + 12 * H) == [] and _overlaps(out, CH) == []


def test_the_cut_is_fixed_a_show_that_started_after_it_gives_way_also_once_aired():
    """The sync runs 5 h after the cut: a show that started 1 h after the cut
    has aired by then and still follows the guide (no jumping back)."""
    fresh = [_prog(CUT - H, CUT + H, 'G0', desc='g0'),
             _prog(CUT + H, CUT + 2 * H, 'Guia uno', desc='g1'),
             _prog(CUT + 2 * H, CUT + 6 * H, 'G2', desc='g2')]
    links = [_prog(CUT - H, CUT + H, 'G0', link='k0'),
             _prog(CUT + H, CUT + 2 * H, 'Export uno', link='k1'),
             _prog(CUT + 2 * H, CUT + 6 * H, 'G2', link='k2')]
    out, _, guard, _ = _guarded(fresh, links, [], now=CUT + 5 * H)
    assert _links(out) == ['k0', 'k2']
    at = [p for p in out if E(p['start']) == CUT + H]
    assert len(at) == 1 and _title(at[0]) == 'Guia uno' and not at[0].get('catchup-id')
    assert guard == _stats(export_yielded=1, export_agreeing=1)


def test_squirrel_keeps_the_exports_schedule_and_replay():
    fresh = [_prog(CUT - H, CUT + 4 * H, 'Guia', desc='g')]
    links = [_prog(CUT - H, CUT + 2 * H, 'Ardilla uno', ch=SQ, link='s1'),
             _prog(CUT + 2 * H, CUT + 14 * H, 'Ardilla 12 h', ch=SQ, link='s2')]
    out, _, guard, filled = _guarded(fresh, links, [])
    assert [p['catchup-id'] for p in out if p.get('channel') == SQ] == ['s1', 's2']
    assert guard == _stats(uncovered=1) and filled == 0


def _everything():
    fresh = [_prog(CUT - 2 * H, CUT + H, 'P1', desc='p1'),
             _prog(CUT + H, CUT + 2 * H + 30 * M, 'P2', desc='p2'),
             _prog(CUT + 2 * H + 30 * M, CUT + 4 * H, 'Agree', desc='p3'),
             _prog(CUT + 5 * H, CUT + 6 * H, 'Later', desc='p4')]
    links = [_prog(CUT - H, CUT + 2 * H, 'A', link='a'),
             _prog(CUT + 2 * H, CUT + 2 * H + 30 * M, 'B', link='b'),
             _prog(CUT + 2 * H + 30 * M, CUT + 4 * H, 'Agree', link='c'),
             _prog(CUT + H, CUT + 3 * H, 'Ardilla', ch=SQ, link='s')]
    previous = [_prog(CUT - 3 * H, CUT - H, 'Old aired', link='o', desc='old'),
                _prog(CUT + 2 * H, CUT + 2 * H + 30 * M, 'B', link='b', desc='old b'),
                _prog(CUT + 5 * H, CUT + 6 * H, 'Gone', link='g', desc='old gone')]
    return fresh, links, previous


def test_inputs_are_not_modified():
    fresh, links, previous = _everything()
    snapshot = repr((fresh, links, previous))
    out, _, guard, filled = _guarded(fresh, links, previous, redescribe=True)
    assert repr((fresh, links, previous)) == snapshot
    assert guard == _stats(export_yielded=1, export_agreeing=1, history_yielded=2, uncovered=1) and filled == 1
    assert _links(out) == ['o', 'a', 's', 'c']
    assert _holes(out, CH, CUT - 3 * H, CUT + 4 * H) == [] and _overlaps(out, CH) == []


# ================================================================ (a) and (c) in one sync


def test_a_new_guide_address_and_a_stale_export_in_one_sync():
    """An agreeing upcoming show keeps its link and takes the NEW guide's
    same-start details; a published copy that gave way (no link, old
    details) is never published and lends its old details to nothing; a
    Squirrel-like upcoming history slot keeps its old details and link; the
    show on air at the cut keeps today's rules."""
    fresh = [_prog(CUT - 2 * H, CUT + H, 'Antes', desc='new antes'),
             _prog(CUT + H, CUT + 2 * H, 'Agree', desc='new agree'),
             _prog(CUT + 2 * H, CUT + 3 * H, 'Nuevo nombre', desc='new nombre'),
             _prog(CUT + 3 * H, CUT + 5 * H, 'Despues', desc='new despues')]
    links = [_prog(CUT - 2 * H, CUT - H, 'Aired', link='a_1'),
             _prog(CUT - H, CUT + H, 'On air', link='a0'),
             _prog(CUT + H, CUT + 2 * H, 'Agree', link='a1'),
             _prog(CUT + 2 * H, CUT + 3 * H, 'Old name', link='a2')]
    previous = [_prog(CUT - H, CUT + H, 'On air', link='a0', desc='old on air'),
                _prog(CUT + H, CUT + 2 * H, 'Agree', link='a1', desc='old agree'),
                _prog(CUT + 2 * H, CUT + 3 * H, 'Old name', link='a2', desc='old details 2'),
                _prog(CUT + H, CUT + 2 * H, 'Ardilla', ch=SQ, link='sq', desc='old ardilla')]
    out, stats, guard, filled = _guarded(fresh, links, previous, redescribe=True)
    assert guard == _stats(export_yielded=1, export_agreeing=1, history_yielded=1, history_listed=1, uncovered=1)
    by_link = {p['catchup-id']: p for p in out if p.get('catchup-id')}
    assert sorted(by_link) == ['a0', 'a1', 'a_1', 'sq']
    assert _desc(by_link['a1']) == 'new agree'
    assert _desc(by_link['a0']) == 'old on air', 'on air: started, today\'s rule 2'
    assert _desc(by_link['sq']) == 'old ardilla'
    assert not [p for p in out if _desc(p) == 'old details 2' or _title(p) == 'Old name']
    at = [p for p in out if p.get('channel') == CH and E(p['start']) == CUT + 2 * H]
    assert len(at) == 1 and _title(at[0]) == 'Nuevo nombre' and not at[0].get('catchup-id')
    assert stats['redescribe_slots'] == 1 and filled == 0
    for ch in (CH, SQ):
        assert _overlaps(out, ch) == []
    assert _holes(out, CH, CUT - 2 * H, CUT + 5 * H) == []


# ================================================================ time zones


def _local_offset_min(epoch):
    return time.localtime(epoch).tm_gmtoff // 60


def test_mixed_offsets_in_every_zone(clock_zone):
    """The export writes +0000, the guide source each instant's local offset,
    the published guide +0100: every decision is made on epochs, so the
    result is the one of +0000 everywhere, in every pinned zone (DST change
    days included). The fill's pieces keep their neighbours' texts."""
    cut = clock_zone // 60 * 60

    def case(guide_off, prev_off):
        def g(start, stop, title, **kw):
            return _prog(start, stop, title, offset_min=guide_off(start), **kw)
        fresh = [g(cut - 2 * H, cut + H, 'P1', desc='p1'),
                 g(cut + H, cut + 2 * H + 30 * M, 'P2', desc='p2'),
                 g(cut + 2 * H + 30 * M, cut + 4 * H, 'Agree', desc='p3')]
        links = [_prog(cut - H, cut + 2 * H, 'A', link='a'),
                 _prog(cut + 2 * H, cut + 2 * H + 30 * M, 'B', link='b'),
                 _prog(cut + 2 * H + 30 * M, cut + 4 * H, 'Agree', link='c'),
                 _prog(cut + H, cut + 3 * H, 'Ardilla', ch=SQ, link='s')]
        previous = [_prog(cut + 2 * H, cut + 2 * H + 30 * M, 'B', link='b', desc='old b', offset_min=prev_off)]
        out, stats, guard, filled = _guarded(fresh, links, previous, cut=cut, now=cut + 30 * M)
        rows = sorted((p['channel'], E(p['start']), E(p['stop']), p.get('catchup-id') or '', _title(p), _desc(p))
                      for p in out)
        return rows, stats, guard, filled, out

    utc = case(lambda e: 0, 0)
    mixed = case(_local_offset_min, 60)
    assert mixed[:4] == utc[:4]
    rows, _, guard, filled, out = mixed
    assert [r[3] for r in rows if r[3]] == ['a', 'c', 's']
    assert guard == _stats(export_yielded=1, export_agreeing=1, history_yielded=1, uncovered=1) and filled == 1
    piece = [p for p in out if _title(p) == 'P2']
    assert len(piece) == 1 and piece[0]['start'] == _ts(cut + 2 * H)        # the kept slot A's stop text
    assert _holes(out, CH, cut, cut + 4 * H) == [] and _overlaps(out, CH) == []


# ================================================================ the guard in a real sync

# imports70_sync_rig: the import 'mp' (Movistar+-like playlist and export, a
# davidmuma-like guide), channels C1 ('Chan One', in the guide) and C2
# ('Squirrel', only in the export). n = the moment the test starts.

AGED = 40 * sr.H        # the export's age in most cases: the cut is 4 h before n


def _day(n, upcoming='Tres', on_air='Dos'):
    """The guide covers C1 from n-7h to n+3h; the export lists C1 from n-6h
    to n+2h and Squirrel on air. With a 40 h old export (cut n-4h): a0 and
    a1 started before the cut, u2 (on air now) and u3 (upcoming) after it;
    u2 agrees with the guide when on_air='Dos', u3 when upcoming='Otro'."""
    H = sr.H
    guide = [sr.show(n - 7 * H, n - 6 * H, 'Previo', 'g previo'),
             sr.show(n - 6 * H, n - 5 * H, 'Antiguo', 'g antiguo'),
             sr.show(n - 5 * H, n - H, 'Largo', 'g largo'),
             sr.show(n - H, n + H, 'Dos', 'g dos'),
             sr.show(n + H, n + 2 * H, 'Otro', 'g otro'),
             sr.show(n + 2 * H, n + 3 * H, 'Final', 'g final')]
    c1 = [sr.show(n - 6 * H, n - 5 * H, 'Antiguo', link='a0'),
          sr.show(n - 5 * H, n - H, 'Largo', link='a1'),
          sr.show(n - H, n + H, on_air, link='u2'),
          sr.show(n + H, n + 2 * H, upcoming, link='u3')]
    c2 = [sr.show(n - H, n + H, 'Ardilla', link='sq')]
    return guide, c1, c2


def _rows(run, channel=sr.C1):
    return [sr.row(p) for p in sr.on(run, channel)]


def _at(run, start, channel=sr.C1):
    found = [r for r in _rows(run, channel) if r[0] == start]
    assert len(found) == 1, found
    return found[0]


STALE_MSG = ('Movistar+ guide file is %s old. Upcoming shows follow your guide where it lists them, and '
             'keep replay only where both list them the same. Restart Kodi or press Export channels and '
             'EPG now in Movistar+ settings, and check Movistar+ is still logged in.')


def _guard_line(rig):
    return sr.logged(rig, '-- shows from ')


def test_a_40_hour_old_export_gives_way_where_the_guide_lists_the_show(tmp_path, monkeypatch):
    rig = sr.make_rig(tmp_path, monkeypatch)
    n, H = rig.now, sr.H
    guide, c1, c2 = _day(n)
    sr.write_export(rig, c1, c2, age_s=AGED)
    run = sr.sync(rig, monkeypatch, sr.entry(rig), guide=sr.guide_xml(guide))
    assert _rows(run) == [
        (n - 7 * H, n - 6 * H, 'Previo', 'g previo', None),
        (n - 6 * H, n - 5 * H, 'Antiguo', 'g antiguo', 'a0'),     # started before the cut: as today
        (n - 5 * H, n - H, 'Largo', 'g largo', 'a1'),             # on air at the cut: counts as aired
        (n - H, n + H, 'Dos', 'g dos', 'u2'),                     # both list it the same: replay kept
        (n + H, n + 2 * H, 'Otro', 'g otro', None),               # 'Tres' gave way to the guide's show
        (n + 2 * H, n + 3 * H, 'Final', 'g final', None)]
    assert _rows(run, sr.C2) == [(n - H, n + H, 'Ardilla', None, 'sq')]   # Squirrel: the export's schedule
    assert sr.overlaps(run, sr.C1) == []
    st = run.playlist.added
    assert (st[sr.C1]['catchup'], st[sr.C1]['catchup-source']) == ('default', '{catchup-id}')
    assert st[sr.C2]['catchup'] == 'default'
    cut = n - 4 * H
    line = ('syncAll, [mp] replay links file is 40.0 h old -- shows from %s follow the guide: export yielded=1 '
            'agreeing=1, history yielded=0 listed=0, not covered=1, gaps filled=0'
            % time.strftime('%Y-%m-%d %H:%M', time.localtime(cut)))
    assert [(m, lvl) for m, lvl in rig.lines if m == line] == [(line, xbmc.LOGINFO)]
    assert run.res['updated_cfg']['last_error'] == STALE_MSG % '40 hours'
    assert run.res['updated_cfg']['last_status'] == 'unchanged' and run.res['updated_cfg']['fail_count'] == 0


def test_the_hand_over_to_the_guide_leaves_no_hole(tmp_path, monkeypatch):
    """The export's 'A' was on air at the cut (kept: counts as aired), 'B'
    and 'C' after it gave way. The guide's 'P2' overlaps A, so the merge
    drops it: the gap fill publishes the rest of P2 from A's stop, without a
    link, instead of a 30-minute hole."""
    rig = sr.make_rig(tmp_path, monkeypatch)
    n, H, M = rig.now, sr.H, sr.M
    guide = [sr.show(n - 5 * H, n - 3 * H, 'P1', 'p1'),
             sr.show(n - 3 * H, n - H - 30 * M, 'P2', 'p2'),
             sr.show(n - H - 30 * M, n + H, 'P3', 'p3')]
    c1 = [sr.show(n - 5 * H, n - 2 * H, 'A', link='a'),
          sr.show(n - 2 * H, n - H, 'B', link='b'),
          sr.show(n - H, n, 'C', link='c')]
    sr.write_export(rig, c1, age_s=AGED)
    run = sr.sync(rig, monkeypatch, sr.entry(rig), guide=sr.guide_xml(guide))
    assert _rows(run) == [(n - 5 * H, n - 2 * H, 'P1', 'p1', 'a'),          # A, with rule 1's details
                          (n - 2 * H, n - H - 30 * M, 'P2', 'p2', None),     # the rest of P2
                          (n - H - 30 * M, n + H, 'P3', 'p3', None)]
    assert 'export yielded=2 agreeing=0, history yielded=0 listed=0, not covered=0, gaps filled=1' \
        in _guard_line(rig)[0]


def test_an_on_air_show_that_gives_way_switches_the_channels_replay_off(tmp_path, monkeypatch):
    """S9: replay follows the show on air, which now has no link on C1;
    Squirrel, which the guide doesn't cover, keeps its replay."""
    rig = sr.make_rig(tmp_path, monkeypatch)
    n, H = rig.now, sr.H
    guide, c1, c2 = _day(n, on_air='Noticias')
    sr.write_export(rig, c1, c2, age_s=AGED)
    run = sr.sync(rig, monkeypatch, sr.entry(rig), guide=sr.guide_xml(guide))
    assert _at(run, n - H) == (n - H, n + H, 'Dos', 'g dos', None)
    st = run.playlist.added
    assert (st[sr.C1]['catchup'], st[sr.C1]['catchup-source'], st[sr.C1]['catchup-days']) == ('', '', '')
    assert (st[sr.C2]['catchup'], st[sr.C2]['catchup-source']) == ('default', '{catchup-id}')


def test_a_show_published_with_a_link_loses_it_when_the_file_gets_old(tmp_path, monkeypatch):
    """Two syncs. With a fresh file, rule 1 publishes the export's 'Tres'
    slot with the guide's 'Otro' (same start) and the export's link. Once
    the same file is 40 h old, that published copy is judged by the export's
    own listing of its link ('Tres', not the guide's show): it loses the
    link and the guide's show stays, without replay."""
    rig = sr.make_rig(tmp_path, monkeypatch)
    n, H = rig.now, sr.H
    guide, c1, c2 = _day(n)
    sr.write_export(rig, c1, c2, age_s=H)
    first = sr.sync(rig, monkeypatch, sr.entry(rig), guide=sr.guide_xml(guide))
    assert _at(first, n + H) == (n + H, n + 2 * H, 'Otro', 'g otro', 'u3')
    assert not _guard_line(rig)
    sr.age(rig, AGED)
    second = sr.sync(rig, monkeypatch, sr.entry(rig), guide=sr.guide_xml(guide),
                     previous=list(first.guide.XMLTVDATA['programmes']))
    assert _at(second, n + H) == (n + H, n + 2 * H, 'Otro', 'g otro', None)
    assert [r[4] for r in _rows(second) if r[4]] == ['a0', 'a1', 'u2']
    assert 'history yielded=1 listed=1' in _guard_line(rig)[0]
    assert sr.overlaps(second, sr.C1) == []


def test_a_new_file_after_a_stale_one_turns_the_guard_off(tmp_path, monkeypatch):
    """Recovery: the stored links_mtime_ns is the old file's (stale), the
    file on disk is new. S17 forces the sync, which reads the new file, and
    the guard uses THIS read: the export's schedule is back."""
    rig = sr.make_rig(tmp_path, monkeypatch)
    monkeypatch.setattr(Imports, '_guideNeedsSelfHeal', lambda self, cfg, chans: False)
    n, H = rig.now, sr.H
    guide, c1, c2 = _day(n)
    new_ns = sr.write_export(rig, c1, c2, age_s=H)
    old_ns = (n - AGED) * 10 ** 9
    cfg = sr.entry(rig, last_sync_at=n - 30 * sr.M, links_mtime_ns=str(old_ns))
    run = sr.sync(rig, monkeypatch, cfg, guide=sr.guide_xml(guide))
    assert run.res['status'] == 'unchanged'
    assert sr.logged(rig, '[mp] syncAll, new replay links file — forcing a sync')
    assert not sr.logged(rig, 'replay links file is stale')
    assert run.res['updated_cfg']['links_mtime_ns'] == str(new_ns)
    assert _at(run, n + H) == (n + H, n + 2 * H, 'Otro', 'g otro', 'u3'), 'the export keeps its schedule'
    assert not _guard_line(rig) and run.res['updated_cfg']['last_error'] is None


def test_a_new_guide_address_and_a_stale_export_in_one_real_sync(tmp_path, monkeypatch):
    """(a) and (c) together: agreeing shows keep their links and take the NEW
    guide's details by rule 1; the show that gave way is the new guide's,
    without a link, and the old guide's details of its published copy are
    never shown; Squirrel and the show that aired before the cut are as
    today; no overlaps."""
    rig = sr.make_rig(tmp_path, monkeypatch)
    n, H, M = rig.now, sr.H, sr.M
    new_guide = [sr.show(n - 7 * H, n - 6 * H, 'Previo', 'new previo'),
                 sr.show(n - 6 * H + 5 * M, n - 5 * H, 'Cero', 'new cero'),       # 5 min off: no rule 1
                 sr.show(n - 5 * H, n - 3 * H, 'Antes', 'new antes'),
                 sr.show(n - 3 * H, n - 2 * H, 'Uno', 'new uno'),
                 sr.show(n - 2 * H, n - H, 'Medio', 'new medio'),
                 sr.show(n - H, n + H, 'Dos', 'new dos'),
                 sr.show(n + H, n + 2 * H, 'Tres', 'new tres'),
                 sr.show(n + 2 * H, n + 3 * H, 'Otro', 'new otro'),
                 sr.show(n + 3 * H, n + 4 * H, 'Fin', 'new fin')]
    c1 = [sr.show(n - 6 * H, n - 5 * H, 'Cero', link='e0'),       # aired before the cut
          sr.show(n - 3 * H, n - 2 * H, 'Uno', link='e1'),        # after the cut, aired: agrees
          sr.show(n - H, n + H, 'Dos', link='e2'),                # on air: agrees
          sr.show(n + H, n + 2 * H, 'Tres', link='e3'),           # upcoming: agrees
          sr.show(n + 2 * H, n + 3 * H, 'Cuatro', link='e4')]     # upcoming: gives way to 'Otro'
    c2 = [sr.show(n - H, n + H, 'Ardilla', link='s1'), sr.show(n + H, n + 2 * H, 'Ardilla 2', link='s2')]
    previous = [sr.published_show(sr.C1, sr.show(s, e, t, 'old ' + t.lower(), link=k)) for s, e, t, _, k, _ in c1]
    previous += [sr.published_show(sr.C2, sr.show(s, e, t, 'old ardilla', link=k)) for s, e, t, _, k, _ in c2]
    sr.write_export(rig, c1, c2, age_s=AGED)
    cfg = sr.entry(rig, epg_url=sr.NEW_GUIDE, epg_source_key=imports_mod.importcfg_helpers.source_key(sr.OLD_GUIDE))
    run = sr.sync(rig, monkeypatch, cfg, guide=sr.guide_xml(new_guide), previous=previous)
    assert run.res['epg_source_changed'] is True
    linked = {r[4]: r for r in _rows(run) if r[4]}
    assert sorted(linked) == ['e0', 'e1', 'e2', 'e3']
    assert linked['e0'] == (n - 6 * H, n - 5 * H, 'Cero', 'old cero', 'e0')     # aired before the cut: as today
    assert linked['e1'][3] == 'new uno' and linked['e2'][3] == 'new dos' and linked['e3'][3] == 'new tres'
    assert _at(run, n + 2 * H) == (n + 2 * H, n + 3 * H, 'Otro', 'new otro', None)
    assert not [r for r in _rows(run) if r[3] == 'old cuatro' or r[2] == 'Cuatro']
    assert _rows(run, sr.C2) == [(n - H, n + H, 'Ardilla', 'old ardilla', 's1'),
                                 (n + H, n + 2 * H, 'Ardilla 2', 'old ardilla', 's2')]
    assert sr.overlaps(run, sr.C1) == [] and sr.overlaps(run, sr.C2) == []
    # not covered: Squirrel's two export slots and their two published copies
    assert 'export yielded=1 agreeing=3, history yielded=1 listed=3, not covered=4' in _guard_line(rig)[0]
    assert sr.logged(rig, 'redescribe-slots=1')
    assert run.res['updated_cfg']['epg_source_key'] == imports_mod.importcfg_helpers.source_key(sr.NEW_GUIDE)


def test_a_30_hour_old_export_keeps_its_schedule_and_shows_no_message(tmp_path, monkeypatch):
    rig = sr.make_rig(tmp_path, monkeypatch)
    n, H = rig.now, sr.H
    guide, c1, c2 = _day(n)
    sr.write_export(rig, c1, c2, age_s=30 * H)
    run = sr.sync(rig, monkeypatch, sr.entry(rig), guide=sr.guide_xml(guide))
    assert _at(run, n + H) == (n + H, n + 2 * H, 'Otro', 'g otro', 'u3')
    assert not _guard_line(rig)
    assert run.res['updated_cfg']['last_error'] is None


def test_the_keep_previous_path_has_no_guard(tmp_path, monkeypatch):
    """Nothing in the guide matches: the kept slice is both the details
    source and the history, so the export keeps its schedule (the message
    still shows)."""
    rig = sr.make_rig(tmp_path, monkeypatch)
    n, H = rig.now, sr.H
    guide, c1, c2 = _day(n)
    kept = [sr.published_show(sr.C1, s) for s in guide]
    sr.write_export(rig, c1, c2, age_s=AGED)
    run = sr.sync(rig, monkeypatch, sr.entry(rig), previous=kept,
                  guide=sr.guide_xml([sr.show(n, n + H, 'x')], channel='Nobody', name='Nobody'))
    assert sr.logged(rig, 'no guide entries matched — keeping the previous guide')
    assert _at(run, n + H) == (n + H, n + 2 * H, 'Otro', 'g otro', 'u3')
    assert not _guard_line(rig)
    assert run.res['updated_cfg']['last_error'] == STALE_MSG % '40 hours'


DOWN = {'status': 0, 'error': 'network down'}


@pytest.mark.parametrize('saved_age,guarded', [(AGED + sr.H, False), (AGED, False), (2 * sr.H, True)],
                         ids=['older-than-the-file', 'same-time', 'newer-than-the-file'])
def test_a_failed_guide_download_guards_only_with_a_saved_guide_newer_than_the_file(
        tmp_path, monkeypatch, saved_age, guarded):
    rig = sr.make_rig(tmp_path, monkeypatch)
    n, H = rig.now, sr.H
    guide, c1, c2 = _day(n)
    sr.write_export(rig, c1, c2, age_s=AGED)
    rig.cache.write_bytes(sr.guide_xml(guide))
    os.utime(str(rig.cache), (n - saved_age, n - saved_age))
    run = sr.sync(rig, monkeypatch, sr.entry(rig), guide_answer=DOWN)
    assert run.res['epg_cache_mtime'] == n - saved_age
    assert run.res['updated_cfg']['last_error'].startswith('Guide download failed: network down')
    off = sr.logged(rig, "replay links file is stale, but this sync's guide is the saved copy from before it "
                         "-- the export keeps its schedule", xbmc.LOGINFO)
    if guarded:
        assert _at(run, n + H) == (n + H, n + 2 * H, 'Otro', 'g otro', None)
        assert _guard_line(rig) and not off
    else:
        assert _at(run, n + H) == (n + H, n + 2 * H, 'Otro', 'g otro', 'u3')
        assert off and not _guard_line(rig)


def test_a_saved_guide_replaced_after_the_sync_read_it_keeps_the_guard_off(tmp_path, monkeypatch):
    """The saved guide is older than the file when syncOne reads it; a
    newer one replaces it before the merge (a Builder sync that stopped
    waiting): the guard judges the copy this sync used, so it stays off."""
    rig = sr.make_rig(tmp_path, monkeypatch)
    n, H = rig.now, sr.H
    guide, c1, c2 = _day(n)
    sr.write_export(rig, c1, c2, age_s=AGED)
    rig.cache.write_bytes(sr.guide_xml(guide))
    os.utime(str(rig.cache), (n - AGED - H, n - AGED - H))
    real_read = Imports._epgCacheRead

    def read_then_replaced(self, import_id):
        pairs = real_read(self, import_id)
        os.utime(str(rig.cache), (n, n))
        return pairs
    monkeypatch.setattr(Imports, '_epgCacheRead', read_then_replaced)
    run = sr.sync(rig, monkeypatch, sr.entry(rig), guide_answer=DOWN)
    assert os.stat(str(rig.cache)).st_mtime == n, 'replaced before the merge'
    assert _at(run, n + H) == (n + H, n + 2 * H, 'Otro', 'g otro', 'u3')
    assert not _guard_line(rig)


# ================================================================ the "went stale" trigger


def _gated(rig, **over):
    """Synced 20 minutes ago (10 h interval): skipped unless forced. The
    stored links_mtime_ns is the file's, so S17 doesn't force it."""
    cfg = sr.entry(rig, last_sync_at=rig.now - 20 * sr.M)
    cfg['links_mtime_ns'] = str(os.stat(str(rig.export)).st_mtime_ns)
    cfg.update(over)
    return cfg


STALE_LINE = '[mp] syncAll, replay links file is stale — forcing a sync'


def _gate(tmp_path, monkeypatch, export_age, ints=None, **over):
    rig = sr.make_rig(tmp_path, monkeypatch)
    rig.ints.update(ints or {})
    monkeypatch.setattr(Imports, '_guideNeedsSelfHeal', lambda self, cfg, chans: False)
    guide, c1, c2 = _day(rig.now)
    sr.write_export(rig, c1, c2, age_s=export_age)
    run = sr.sync(rig, monkeypatch, _gated(rig, **over), guide=sr.guide_xml(guide))
    return rig, run


def test_the_trigger_fires_when_the_file_went_stale_after_the_last_sync(tmp_path, monkeypatch):
    """The cut (36 h after the file's time) fell 10 minutes ago, the last
    sync 20 minutes ago: due now, although that sync is under 50 minutes old."""
    rig, run = _gate(tmp_path, monkeypatch, 36 * sr.H + 10 * sr.M)
    assert run.res['status'] == 'unchanged', 'synced, not skipped'
    assert [lvl for m, lvl in rig.lines if m == STALE_LINE] == [xbmc.LOGINFO]
    assert run.res['updated_cfg']['last_sync_at'] >= rig.now


@pytest.mark.parametrize('minutes,due', [(51, True), (49, False)])
def test_the_hourly_sync_while_the_file_stays_stale(tmp_path, monkeypatch, minutes, due):
    """The file has been stale for hours and the import synced since the cut:
    due again once that sync is 50 minutes old (the hourly cycle)."""
    rig, run = _gate(tmp_path, monkeypatch, AGED, last_sync_at=int(time.time()) - minutes * sr.M)
    assert run.res['status'] == ('unchanged' if due else 'skipped')
    assert bool(sr.logged(rig, STALE_LINE)) is due


def test_a_young_file_never_triggers(tmp_path, monkeypatch):
    rig, run = _gate(tmp_path, monkeypatch, 35 * sr.H, last_sync_at=int(time.time()) - 3 * sr.H)
    assert run.res['status'] == 'skipped' and not sr.logged(rig, STALE_LINE)


@pytest.mark.parametrize('export_age,ints,fail_count,synced_ago,status', [
    # the file went stale after the last sync (400 s ago): the retry backoff (900 s at fail_count 3) rules
    (36 * sr.H + 5 * sr.M, {}, 3, 400, 'skipped'),
    # the hourly sync would be due (55 minutes): the retry backoff (4 h at fail_count 6) rules
    (AGED, {}, 6, 55 * sr.M, 'skipped'),
    # failing for 2 hours with "abandon after 1 hour": abandoned, not forced
    (36 * sr.H + 5 * sr.M, {'Imports_Failure_Abandon_Hours': 1}, 3, 400, 'abandoned'),
], ids=['backoff-at-the-cut', 'backoff-hourly', 'abandoned'])
def test_a_failing_import_is_left_to_the_backoff_and_the_abandon_check(tmp_path, monkeypatch, export_age, ints,
                                                                       fail_count, synced_ago, status):
    """Each case would be due by the trigger's own rules (stale after the last
    sync, or the hourly sync): a failing import is never forced by it."""
    now = int(time.time())
    rig, run = _gate(tmp_path, monkeypatch, export_age, ints=ints, last_status='failed', fail_count=fail_count,
                     failed_since=now - 2 * sr.H, last_sync_at=now - synced_ago)
    assert run.res['status'] == status
    assert not sr.logged(rig, STALE_LINE)


def _spy(monkeypatch, answer):
    calls = []
    monkeypatch.setattr(Imports, '_linksWentStale', lambda self, cfg, chans: calls.append(cfg) or answer)
    return calls


def test_a_switched_off_import_is_not_checked(tmp_path, monkeypatch):
    calls = _spy(monkeypatch, True)
    rig, run = _gate(tmp_path, monkeypatch, AGED, enabled=False)
    assert calls == [] and run.res['status'] == 'disabled'


@pytest.mark.parametrize('how', ['new-file', 'force-scope'])
def test_no_check_when_a_sync_is_already_forced(tmp_path, monkeypatch, how):
    calls = _spy(monkeypatch, True)
    rig = sr.make_rig(tmp_path, monkeypatch)
    monkeypatch.setattr(Imports, '_guideNeedsSelfHeal', lambda self, cfg, chans: False)
    guide, c1, c2 = _day(rig.now)
    sr.write_export(rig, c1, c2, age_s=AGED)
    cfg = _gated(rig, links_mtime_ns='1') if how == 'new-file' else _gated(rig)
    run = sr.sync(rig, monkeypatch, cfg, guide=sr.guide_xml(guide),
                  force_scope='all' if how == 'force-scope' else None)
    assert calls == [] and run.res['status'] == 'unchanged'
    assert not sr.logged(rig, STALE_LINE)


# ---------------------------------------------------------------- _linksWentStale itself


def _unit(tmp_path, monkeypatch, export_age=None):
    rig = sr.make_rig(tmp_path, monkeypatch)
    if export_age is not None:
        sr.write_export(rig, [sr.show(rig.now, rig.now + sr.H, 'x', link='l')], age_s=export_age)
    return rig, Imports(), sr.records()


def test_links_went_stale_unit(tmp_path, monkeypatch):
    """The file turned stale 10 minutes ago (the cut). Due when the last sync
    was before the cut, and while it stays stale, once the last sync is 50
    minutes old (the hourly sync)."""
    rig, imp, recs = _unit(tmp_path, monkeypatch, export_age=36 * sr.H + 10 * sr.M)
    n, M = rig.now, sr.M
    cut = n - 10 * M
    cfg = sr.entry(rig, last_sync_at=cut - 1)
    assert imp._linksWentStale(cfg, recs, now=n) is True                       # synced before the cut
    assert imp._linksWentStale(dict(cfg, last_sync_at=cut), recs, now=n) is False     # at the cut, 10 min ago
    assert imp._linksWentStale(dict(cfg, last_sync_at=cut + 1), recs, now=n) is False
    after = dict(cfg, last_sync_at=cut + 1)
    assert imp._linksWentStale(after, recs, now=cut + 1 + 50 * M) is True       # the hourly sync
    assert imp._linksWentStale(after, recs, now=cut + 50 * M) is False          # 49 min 59 s
    assert imp._linksWentStale(after, recs, now=cut + 1 + 5 * 3600) is True
    assert imp._linksWentStale(dict(cfg, last_sync_at=None), recs, now=n) is True
    assert imp._linksWentStale(dict(cfg, last_sync_at='junk'), recs, now=n) is True
    assert imp._linksWentStale(cfg, recs, now=cut - 1) is False                # not stale yet
    assert imp._linksWentStale(cfg, recs) is True                              # now = the clock
    # the file's own time wins over the stored one
    assert imp._linksWentStale(dict(cfg, links_mtime_ns=str(n * 10 ** 9)), recs, now=n) is True
    # records of another import, or not link-based: not this import's business
    assert imp._linksWentStale(cfg, [dict(r, import_source='other') for r in recs], now=n) is False
    assert imp._linksWentStale(cfg, sr.records(link=False), now=n) is False
    # not a known exporter's playlist
    assert imp._linksWentStale(dict(cfg, m3u_path='', m3u_url='https://example.invalid/list.m3u'),
                               recs, now=n) is False


def test_links_went_stale_uses_the_stored_time_when_the_file_is_missing(tmp_path, monkeypatch):
    rig, imp, recs = _unit(tmp_path, monkeypatch)
    n = rig.now
    cfg = sr.entry(rig, last_sync_at=n - 2 * sr.H)
    assert imp._linksWentStale(dict(cfg, links_mtime_ns=str((n - 40 * sr.H) * 10 ** 9)), recs, now=n) is True
    assert imp._linksWentStale(dict(cfg, links_mtime_ns=str((n - 30 * sr.H) * 10 ** 9)), recs, now=n) is False
    for stored in (None, '', 'junk'):
        assert imp._linksWentStale(dict(cfg, links_mtime_ns=stored), recs, now=n) is False
    without = dict(cfg)
    without.pop('links_mtime_ns', None)
    assert imp._linksWentStale(without, recs, now=n) is False


def test_links_went_stale_never_uses_xbmcvfs_stat(tmp_path, monkeypatch):
    rig, imp, recs = _unit(tmp_path, monkeypatch, export_age=40 * sr.H)

    def boom(*a, **k):
        raise AssertionError('xbmcvfs.Stat must not be used')
    monkeypatch.setattr(xbmcvfs, 'Stat', boom)
    monkeypatch.setattr(imports_mod.xbmcvfs, 'Stat', boom)
    assert imp._linksWentStale(sr.entry(rig, last_sync_at=rig.now - 5 * sr.H), recs) is True


def test_links_went_stale_error_gives_false_and_a_warning(tmp_path, monkeypatch):
    rig, imp, recs = _unit(tmp_path, monkeypatch, export_age=40 * sr.H)

    def boom(cfg):
        raise RuntimeError('simulated')
    monkeypatch.setattr(rh, 'exporter_for', boom)
    assert imp._linksWentStale(sr.entry(rig, last_sync_at=0), recs) is False
    assert sr.logged(rig, '[mp] _linksWentStale, check failed: simulated', xbmc.LOGWARNING)


def test_links_file_state_unit(tmp_path, monkeypatch):
    rig, imp, recs = _unit(tmp_path, monkeypatch, export_age=sr.H)
    cfg = sr.entry(rig)
    st = os.stat(str(rig.export))
    assert imp._linksFileState(cfg, recs) == (True, (st.st_size, st.st_mtime_ns))
    assert imp._linksFileState(dict(cfg, m3u_path='', m3u_url='https://example.invalid/list.m3u'),
                               recs) == (False, None)
    assert imp._linksFileState(dict(cfg, id=''), recs) == (False, None)
    assert imp._linksFileState(cfg, sr.records(link=False)) == (False, None)
    assert imp._linksFileState(cfg, [dict(r, import_source='other') for r in recs]) == (False, None)
    assert imp._linksFileState(cfg, None) == (False, None)
    rig.export.unlink()
    assert imp._linksFileState(cfg, recs) == (True, None)


def test_links_changed_is_unchanged_on_the_shared_helper(tmp_path, monkeypatch):
    """S17's check now runs on _linksFileState: same answers as before."""
    rig, imp, recs = _unit(tmp_path, monkeypatch, export_age=sr.H)
    ns = str(os.stat(str(rig.export)).st_mtime_ns)
    cfg = sr.entry(rig, links_mtime_ns=ns)
    assert imp._linksChanged(cfg, recs) is False
    assert imp._linksChanged(dict(cfg, links_mtime_ns='1'), recs) is True
    assert imp._linksChanged(dict(cfg, links_mtime_ns='1'), sr.records(link=False)) is False
    rig.export.unlink()
    assert imp._linksChanged(dict(cfg, links_mtime_ns='1'), recs) is False


# ---------------------------------------------------------------- _staleCut


def _res(links_mtime_ns, **over):
    res = {'updated_cfg': {'links_mtime_ns': links_mtime_ns}}
    res.update(over)
    return res


def test_stale_cut_unit(tmp_path, monkeypatch):
    rig = sr.make_rig(tmp_path, monkeypatch)
    imp, n, H = Imports(), rig.now, sr.H
    t_file = n - 40 * H
    ns = str(t_file * 10 ** 9)
    cut = t_file + 36 * H
    assert imp._staleCut('mp', _res(ns), n) == cut
    assert imp._staleCut('mp', _res(str((n - 30 * H) * 10 ** 9)), n) is None       # younger than 36 h
    assert imp._staleCut('mp', _res(None), n) is None
    assert imp._staleCut('mp', {'updated_cfg': {}}, n) is None
    failed = dict(epg_error='guide download failed: x')
    assert imp._staleCut('mp', _res(ns, epg_cache_mtime=t_file + 1, **failed), n) == cut
    assert rig.lines == []
    for saved in (t_file, t_file - 1, None):
        assert imp._staleCut('mp', _res(ns, epg_cache_mtime=saved, **failed), n) is None
    assert imp._staleCut('mp', _res(ns, **failed), n) is None
    assert len(sr.logged(rig, "syncAll, [mp] replay links file is stale, but this sync's guide is the saved copy",
                         xbmc.LOGINFO)) == 4


# ================================================================ the message from 36 h


@pytest.mark.parametrize('hours,text', [(36, '36 hours'), (37, '37 hours'), (47.9, '47 hours'), (48.1, '2 days'),
                                        (72.5, '3 days'), (240, '10 days')])
def test_age_text(hours, text):
    assert imports_mod._ageText(hours * 3600) == text


def _read(rig, name='Movistar+'):
    exporter = rh.exporter_for({'m3u_path': str(rig.folder / 'channels.m3u8')})
    cfg = {}
    out = Imports()._readLinks('mp', exporter, cfg, {'C1'})
    return out, cfg


@pytest.mark.parametrize('hours,text', [(35, None), (37, '37 hours'), (47.9, '47 hours'), (48.1, '2 days'),
                                        (72.5, '3 days')])
def test_the_message_from_36_hours(tmp_path, monkeypatch, hours, text):
    rig = sr.make_rig(tmp_path, monkeypatch)
    sr.write_export(rig, [sr.show(rig.now, rig.now + sr.H, 'x', link='l')], age_s=int(hours * 3600))
    out, cfg = _read(rig)
    assert out['pairs']
    assert out['warning'] == (None if text is None else STALE_MSG % text)
    assert cfg['links_mtime_ns'] == str(os.stat(str(rig.export)).st_mtime_ns)


def test_an_old_file_whose_shows_all_ended_says_out_of_date(tmp_path, monkeypatch):
    rig = sr.make_rig(tmp_path, monkeypatch)
    sr.write_export(rig, [sr.show(rig.now - 2 * sr.H, rig.now - sr.H, 'x', link='l')], age_s=72 * sr.H)
    out, _ = _read(rig)
    assert out['warning'] == imports_mod.LINKS_MSG_OUTDATED % {'name': 'Movistar+'}


def test_an_old_file_without_links_says_no_links(tmp_path, monkeypatch):
    rig = sr.make_rig(tmp_path, monkeypatch)
    sr.write_export(rig, [sr.show(rig.now, rig.now + sr.H, 'x')], age_s=72 * sr.H)
    out, _ = _read(rig)
    assert out['warning'] == imports_mod.LINKS_MSG_NO_LINKS % {'name': 'Movistar+'}


def test_an_old_unreadable_file_says_unreadable(tmp_path, monkeypatch):
    rig = sr.make_rig(tmp_path, monkeypatch)
    rig.export.write_bytes(b'')
    sr.age(rig, 72 * sr.H)
    out, _ = _read(rig)
    assert out['warning'] == imports_mod.LINKS_MSG_UNREADABLE % {'name': 'Movistar+', 'reason': 'empty file'}


def test_the_stale_message_comes_before_the_declared_check(tmp_path, monkeypatch):
    """The declared-replay check is the caller's fallback (as for every
    file message)."""
    rig = sr.make_rig(tmp_path, monkeypatch)
    guide, c1, c2 = _day(rig.now)
    sr.write_export(rig, c1, c2, age_s=AGED)
    vod = [dict(r, catchup='vod', **{'catchup-source': ''}) for r in sr.records()]
    run = sr.sync(rig, monkeypatch, sr.entry(rig), guide=sr.guide_xml(guide), chans=vod)
    assert run.res['updated_cfg']['last_error'] == STALE_MSG % '40 hours'


def test_the_old_names_are_gone():
    assert not hasattr(imports_mod, 'LINKS_MSG_OLD') and not hasattr(imports_mod, 'LINKS_OLD_AFTER_DAYS')
    assert imports_mod.LINKS_STALE_RESYNC_SECS == 50 * 60


# ================================================================ Python 3.8


@pytest.mark.parametrize('name', ['replay_helpers.py', 'imports.py'])
def test_changed_files_pass_the_ast_python38_check(name):
    """Kodi 19/20 run Python 3.8: the AST check plus the shared deny-list of
    3.9+ APIs and syntax. A dict '|' is caught by imports.69's no-'|' scanner
    (test_imports69_importcfg_helpers.BITOR_REVIEWED), which covers both files."""
    from test_imports63_clock import py39_api_leaks
    src = _src(name)
    ast.parse(src, feature_version=(3, 8))
    assert py39_api_leaks(src) == []


def test_imports_py_keeps_the_wiring_and_names_the_release():
    """The wiring later steps must keep (step3_design: the fresh-path call
    with fresh_path=True), and the dead zero-match download-marker clear of
    imports.56 stays removed (it ran after the sync's only import-list write,
    so it never reached disk)."""
    src = _src('imports.py')
    for text in ('new_chans, new_progs, fresh_path=True)', 'def _linksFileState', 'def _linksWentStale',
                 'def _staleCut', 'LINKS_MSG_STALE      = (', 'LINKS_STALE_RESYNC_SECS = 50 * 60',
                 "result['epg_source_changed'] = True",
                 "result['epg_cache_mtime'] = os.stat(", 'self._linksWentStale(import_cfg, existing_channels)'):
        assert src.count(text) == 1, text
    assert 'cleared EPG cache validators' not in src
    assert src.count('imports.70') >= 15


def test_replay_helpers_names_the_release():
    src = _src('replay_helpers.py')
    for name in ('LINKS_STALE_AFTER_S', 'def not_started', 'def stale_after', 'def stale_export_filter',
                 'def fill_gaps', 'def _plain_episode', 'redescribe=False'):
        assert name in src
    assert src.count('imports.70') >= 10
