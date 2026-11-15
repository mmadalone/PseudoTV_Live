# -*- coding: utf-8 -*-
"""imports.70 (a): a new guide address re-describes the shows that haven't
started.

Before, an import whose guide address changed kept the OLD guide's
descriptions on its linked Movistar+ shows until they aired: rule 2 of
merge_replay_guide (the already-published show that matches on two of link,
start and title) handed them back at every sync, and rule 3 never ran for a
slot that had details.

imports.70: on the first sync with the new address (syncOne marks it,
epg_source_changed), merge_replay_guide(..., redescribe=True) handles a slot
that hasn't started (not_started(start, now): the show on air counts as
started) and whose whole time the new guide covers on that channel
(_covered/_inside) in re-describe mode: rule 2 is skipped and rule 3 (the
same show a few minutes off, episode-aware) runs even when the slot has
details. Rule 1 (same start second) is unchanged for every slot. Slots,
times and links never change. Shows the new guide can't match safely show
their title only until they air (the user's choice, 2026-11-14). Channels
and times the new guide doesn't cover keep today's rules.

Sections:
  - pure merge cases (the replay_merge harness): upcoming vs started slots,
    the now boundary, coverage (channel missing, edges, gaps), history
    slots, episode tags, same slots and links, time zones.
  - syncOne's flag (epg_source_changed): only a parsed download from
    another address than the cached guide's (imports.69's epg_source_key);
    never a legacy entry, the same address, a 304 or the cache fallback;
    a failed cache write still flags. The saved guide's time
    (epg_cache_mtime) is taken before the fallback reads it.
  - syncAll end to end (imports70_sync_rig: times from the moment the test
    starts): the upcoming slot takes the new guide; legacy and same-address
    entries don't; the keep-previous path never re-describes; an import
    without link replay switches its guide wholesale, as before.
  - across steps: a new guide address saved through POST /imports.json
    (imports.69's dashboard_env, the real Channels on a tmp channels.json)
    keeps epg_source_key, sends no download markers to the new address,
    and re-describes at the next sync.
No Kodi stubs in the pure section. All data is synthetic.
"""
import json
import os
import time

import pytest
import xbmc

import imports70_sync_rig as sr
from importcfg_helpers import GUIDE_RESET, config_fingerprint, source_key
from imports import Imports
from replay_helpers import merge_replay_guide, xmltv_to_epoch
from test_imports69_dashboard_save import dashboard_env  # noqa: F401 -- imports.69's shared fixture, used by name


CH   = 'C1@mp'
CH2  = 'C2@mp'
NOW  = 1800000000          # 2027-01-15 08:00 UTC -- fixed so tests are deterministic
H    = 3600
M    = 60


def _ts(epoch, offset_min=0):
    """XMLTV time for `epoch`, written with a +HHMM / -HHMM offset."""
    sign = '+' if offset_min >= 0 else '-'
    off = abs(offset_min)
    return (time.strftime('%Y%m%d%H%M%S', time.gmtime(epoch + offset_min * 60))
            + ' %s%02d%02d' % (sign, off // 60, off % 60))


def _prog(start, stop, ch=CH, title='Show', link=None, desc=None, sub=None, offset_min=0, **extra):
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


def _merge(fresh=(), links=(), previous=(), days=None, now=NOW, redescribe=False):
    days = {CH: 7} if days is None else days
    return merge_replay_guide(list(fresh), list(links), list(previous), days, now, redescribe=redescribe)


def _desc(p):
    return p['desc'][0][0] if p.get('desc') else None


def _at(out, start, ch=CH):
    """The published programme of `ch` that starts at epoch `start`."""
    found = [p for p in out if p.get('channel') == ch and xmltv_to_epoch(p['start']) == start]
    assert len(found) == 1, found
    return found[0]


def _slots(out):
    """(channel, start, stop, link) of every published programme, as epochs."""
    return sorted((p.get('channel'), xmltv_to_epoch(p['start']), xmltv_to_epoch(p['stop']),
                   p.get('catchup-id') or '') for p in out)


# ================================================================ the basic case


def _news_case():
    """On air at NOW: 'Previo' (u0). Upcoming: 'Telediario' (u1). Both were
    published from the OLD guide (rule 2 hands those back). The new guide
    has both shows 5 minutes later and covers the whole time of u1."""
    links = [_prog(NOW, NOW + H, title='Previo', link='u0'),
             _prog(NOW + H, NOW + 2 * H, title='Telediario', link='u1')]
    previous = [_prog(NOW, NOW + H, title='Previo', link='u0', desc='old previo'),
                _prog(NOW + H, NOW + 2 * H, title='Telediario', link='u1', desc='old tele')]
    fresh = [_prog(NOW + 5 * M, NOW + H + 5 * M, title='Previo', desc='new previo'),
             _prog(NOW + H + 5 * M, NOW + 2 * H + 5 * M, title='Telediario', desc='new tele',
                   icon=[{'src': 'new-pic'}])]
    return fresh, links, previous


def test_an_upcoming_slot_takes_the_new_guides_shifted_show():
    fresh, links, previous = _news_case()
    out, stats = _merge(fresh, links, previous, redescribe=True)
    tele = _at(out, NOW + H)
    assert _desc(tele) == 'new tele' and tele['icon'] == [{'src': 'new-pic'}]
    assert (tele['start'], tele['stop'], tele['catchup-id']) == (_ts(NOW + H), _ts(NOW + 2 * H), 'u1')
    assert _desc(_at(out, NOW)) == 'old previo', 'on air at now: started, today\'s rule 2'
    assert stats['redescribe_slots'] == 1
    assert stats['overlap_details'] == 1 and stats['kept_details'] == 1
    assert len(out) == 2, 'the new guide\'s shows overlap the slots: not published unlinked'


def test_without_redescribe_the_old_details_come_back():
    """Today's behaviour, kept when the address didn't change."""
    fresh, links, previous = _news_case()
    out, stats = _merge(fresh, links, previous)
    assert [_desc(_at(out, s)) for s in (NOW, NOW + H)] == ['old previo', 'old tele']
    assert stats['redescribe_slots'] == 0 and stats['kept_details'] == 2


def test_no_match_gives_titles_only_and_keeps_the_link():
    """The new guide covers the time but has no same-title show: the old
    description is not used (title only until it airs)."""
    links = [_prog(NOW + H, NOW + 2 * H, title='Telediario', link='u1')]
    previous = [_prog(NOW + H, NOW + 2 * H, title='Telediario', link='u1', desc='old tele')]
    fresh = [_prog(NOW + 30 * M, NOW + 2 * H + 30 * M, title='Noticias', desc='new news')]
    out, stats = _merge(fresh, links, previous, redescribe=True)
    linked = [p for p in out if p.get('catchup-id')]
    assert linked == links, 'the export\'s own entry: title only, same slot and link'
    assert stats['redescribe_slots'] == 1 and stats['plain'] == 1 and stats['kept_details'] == 0


# ================================================================ started slots


@pytest.mark.parametrize('start', [NOW - 2 * H, NOW - 30 * M], ids=['aired', 'on-air'])
def test_a_started_slot_without_a_same_start_show_keeps_its_details(start):
    links = [_prog(start, start + H, title='Serie', link='s1')]
    previous = [_prog(start, start + H, title='Serie', link='s1', desc='old serie')]
    fresh = [_prog(start - H, start + 5 * M, title='Antes', desc='x'),
             _prog(start + 5 * M, start + H + 5 * M, title='Serie', desc='new serie')]
    out, stats = _merge(fresh, links, previous, redescribe=True)
    assert _desc(_at(out, start)) == 'old serie'
    assert stats['redescribe_slots'] == 0 and stats['kept_details'] == 1


@pytest.mark.parametrize('start', [NOW - 2 * H, NOW - 30 * M], ids=['aired', 'on-air'])
def test_a_started_slot_with_a_same_start_show_takes_it(start):
    """Rule 1 is unchanged: the new guide's show at the same second wins,
    with or without the re-describe."""
    links = [_prog(start, start + H, title='Serie', link='s1')]
    previous = [_prog(start, start + H, title='Serie', link='s1', desc='old serie')]
    fresh = [_prog(start, start + H, title='Serie nueva', desc='new same start')]
    for redescribe in (True, False):
        out, stats = _merge(fresh, links, previous, redescribe=redescribe)
        assert _desc(_at(out, start)) == 'new same start'
        assert stats['redescribe_slots'] == 0


@pytest.mark.parametrize('offset,upcoming', [(1, True), (0, False), (-1, False)])
def test_the_now_boundary(offset, upcoming):
    """A slot starting at now+1 hasn't started; one starting at now (on air)
    or now-1 has: on air counts as aired."""
    start = NOW + offset
    links = [_prog(start, start + H, title='Show', link='u1')]
    previous = [_prog(start, start + H, title='Show', link='u1', desc='old')]
    fresh = [_prog(start - H, start + 5 * M, title='Antes', desc='x'),
             _prog(start + 5 * M, start + H + 5 * M, title='Show', desc='new')]
    out, stats = _merge(fresh, links, previous, redescribe=True)
    assert _desc(_at(out, start)) == ('new' if upcoming else 'old')
    assert stats['redescribe_slots'] == (1 if upcoming else 0)


# ================================================================ coverage


def test_a_channel_the_new_guide_doesnt_list_keeps_its_details():
    """Two channels; the new guide has only the first. Before this rule every
    upcoming slot of the second went out titles-only."""
    fresh, links, previous = _news_case()
    links = links + [_prog(NOW + H, NOW + 2 * H, ch=CH2, title='Ardilla', link='w1')]
    previous = previous + [_prog(NOW + H, NOW + 2 * H, ch=CH2, title='Ardilla', link='w1', desc='old ardilla')]
    out, stats = _merge(fresh, links, previous, days={CH: 7, CH2: 7}, redescribe=True)
    assert _desc(_at(out, NOW + H)) == 'new tele'
    assert _desc(_at(out, NOW + H, ch=CH2)) == 'old ardilla'
    assert stats['redescribe_slots'] == 1, 'only the covered channel\'s slot'


@pytest.mark.parametrize('slot,guide', [
    # the slot crosses the new guide's last stop
    ((NOW + 2 * H + 30 * M, NOW + 3 * H + 30 * M), [(NOW + H, NOW + 2 * H), (NOW + 2 * H, NOW + 3 * H)]),
    # the slot starts before the new guide's first start
    ((NOW + 30 * M, NOW + H + 30 * M), [(NOW + H, NOW + 2 * H), (NOW + 2 * H, NOW + 3 * H)]),
    # the slot spans an interior gap of the new guide
    ((NOW + H + 30 * M, NOW + 2 * H + 30 * M), [(NOW + H, NOW + 2 * H), (NOW + 2 * H + 10 * M, NOW + 4 * H)]),
], ids=['last-stop', 'first-start', 'interior-gap'])
def test_a_slot_only_partly_inside_the_new_guide_keeps_todays_rules(slot, guide):
    links = [_prog(slot[0], slot[1], title='Serie', link='u1')]
    previous = [_prog(slot[0], slot[1], title='Serie', link='u1', desc='old serie')]
    fresh = [_prog(s, e, title='Serie', desc='new %d' % i) for i, (s, e) in enumerate(guide)]
    out, stats = _merge(fresh, links, previous, redescribe=True)
    assert _desc(_at(out, slot[0])) == 'old serie'
    assert stats['redescribe_slots'] == 0 and stats['kept_details'] == 1


def test_touching_and_overlapping_programmes_cover_the_slot_together():
    links = [_prog(NOW + H, NOW + 3 * H, title='Maraton', link='u1')]
    previous = [_prog(NOW + H, NOW + 3 * H, title='Maraton', link='u1', desc='old')]
    fresh = [_prog(NOW + 50 * M, NOW + 2 * H, title='Maraton', desc='new'),
             _prog(NOW + 2 * H, NOW + 2 * H + 30 * M, title='Avance', desc='x'),      # touching
             _prog(NOW + 2 * H + 20 * M, NOW + 3 * H + 10 * M, title='Otro', desc='y')]   # overlapping
    out, stats = _merge(fresh, links, previous, redescribe=True)
    assert stats['redescribe_slots'] == 1
    assert _desc(_at(out, NOW + H)) == 'new'


# ================================================================ history slots


def test_an_upcoming_history_slot_with_details_takes_rule_3():
    """A linked show already published that the export doesn't list (H
    slot): it has details, so rule 3 never ran for it; in re-describe mode
    it does."""
    previous = [_prog(NOW + H, NOW + 2 * H, title='Telediario', link='h1', desc='old tele')]
    fresh = [_prog(NOW + 30 * M, NOW + H + 4 * M, title='Avance', desc='x'),
             _prog(NOW + H + 4 * M, NOW + 2 * H + 4 * M, title='Telediario', desc='new tele')]
    out, stats = _merge(fresh, [], previous, redescribe=True)
    tele = _at(out, NOW + H)
    assert _desc(tele) == 'new tele' and tele['catchup-id'] == 'h1'
    assert stats['redescribe_slots'] == 1 and stats['overlap_details'] == 1
    out, _ = _merge(fresh, [], previous)
    assert _desc(_at(out, NOW + H)) == 'old tele', 'without the re-describe: as published'


def test_an_upcoming_history_slot_keeps_its_own_details_when_nothing_matches():
    """Known limit (rule 4): only an export outage at the re-describe sync
    leaves such a slot."""
    previous = [_prog(NOW + H, NOW + 2 * H, title='Telediario', link='h1', desc='old tele')]
    fresh = [_prog(NOW + 30 * M, NOW + 3 * H, title='Cine', desc='x')]
    out, stats = _merge(fresh, [], previous, redescribe=True)
    assert _desc(_at(out, NOW + H)) == 'old tele'
    assert stats['redescribe_slots'] == 1 and stats['overlap_details'] == 0


# ================================================================ episode tags


def test_the_new_guides_episode_tags_pick_the_show():
    links = [_prog(NOW + H, NOW + 2 * H, title='Serie', link='u1', sub='T1 E2 "Dos"')]
    previous = [_prog(NOW + H, NOW + 2 * H, title='Serie', link='u1', desc='old dos')]
    fresh = [_prog(NOW + H - 20 * M, NOW + H + 25 * M, title='Serie T1 E1 · Uno', desc='new uno'),
             _prog(NOW + H + 25 * M, NOW + 2 * H + 10 * M, title='Serie T1 E2 · Dos', desc='new dos')]
    out, stats = _merge(fresh, links, previous, redescribe=True)
    assert _desc(_at(out, NOW + H)) == 'new dos'
    assert stats['overlap_details'] == 1


def test_only_another_episode_gives_titles_only():
    links = [_prog(NOW + H, NOW + 2 * H, title='Serie', link='u1', sub='T1 E2 "Dos"')]
    previous = [_prog(NOW + H, NOW + 2 * H, title='Serie', link='u1', desc='old dos')]
    fresh = [_prog(NOW + H - 20 * M, NOW + 2 * H + 10 * M, title='Serie T1 E1 · Uno', desc='new uno')]
    out, stats = _merge(fresh, links, previous, redescribe=True)
    assert _desc(_at(out, NOW + H)) is None, 'neither E1\'s description nor the old one'
    assert stats['plain'] == 1


# ================================================================ slots and links never change


def _mixed_case():
    """Aired, on-air and upcoming export slots, an upcoming history slot, a
    channel the new guide doesn't list, and new-guide shows before, between
    and after the slots."""
    links = [_prog(NOW - 3 * H, NOW - 2 * H, title='Uno', link='u1'),
             _prog(NOW - 30 * M, NOW + 30 * M, title='Dos', link='u2'),
             _prog(NOW + 30 * M, NOW + 90 * M, title='Tres', link='u3'),
             _prog(NOW + 3 * H, NOW + 4 * H, title='Cinco', link='u5'),
             _prog(NOW + H, NOW + 2 * H, ch=CH2, title='Ardilla', link='w1')]
    previous = [_prog(NOW - 3 * H, NOW - 2 * H, title='Uno', link='u1', desc='old uno'),
                _prog(NOW + 30 * M, NOW + 90 * M, title='Tres', link='u3', desc='old tres'),
                _prog(NOW + 2 * H, NOW + 3 * H, title='Cuatro', link='h4', desc='old cuatro'),
                _prog(NOW + H, NOW + 2 * H, ch=CH2, title='Ardilla', link='w1', desc='old ardilla')]
    fresh = [_prog(NOW - 4 * H, NOW - 3 * H + 5 * M, title='Cero', desc='new cero'),
             _prog(NOW - 3 * H + 5 * M, NOW - 2 * H + 5 * M, title='Uno', desc='new uno'),
             _prog(NOW - 2 * H + 5 * M, NOW + 35 * M, title='Dos', desc='new dos'),
             _prog(NOW + 35 * M, NOW + 95 * M, title='Tres', desc='new tres'),
             _prog(NOW + 95 * M, NOW + 2 * H + 5 * M, title='Entre', desc='new entre'),
             _prog(NOW + 2 * H + 5 * M, NOW + 3 * H, title='Cuatro', desc='new cuatro'),
             _prog(NOW + 3 * H, NOW + 4 * H, title='Cinco', desc='new cinco'),
             _prog(NOW + 5 * H, NOW + 6 * H, title='Seis', desc='new seis')]
    return fresh, links, previous


def test_same_slots_and_links_with_and_without_redescribe():
    fresh, links, previous = _mixed_case()
    days = {CH: 7, CH2: 7}
    with_re, stats_re = _merge(fresh, links, previous, days=days, redescribe=True)
    without, stats = _merge(fresh, links, previous, days=days)
    assert _slots(with_re) == _slots(without)
    assert stats_re['linked'] == stats['linked'] == 6
    assert stats_re['redescribe_slots'] == 3                    # Tres, Cuatro (history), Cinco
    descs = {p.get('catchup-id'): _desc(p) for p in with_re if p.get('catchup-id')}
    assert descs == {'u1': 'old uno',            # aired: rule 2 as today
                     'u2': 'new dos',            # on air, nothing kept: today's rule 3 (no details)
                     'u3': 'new tres',           # upcoming: rule 3, not the old details
                     'h4': 'new cuatro',         # upcoming history slot: rule 3
                     'u5': 'new cinco',          # upcoming: rule 1
                     'w1': 'old ardilla'}        # channel the new guide doesn't list


def test_redescribe_false_is_the_default():
    fresh, links, previous = _mixed_case()
    days = {CH: 7, CH2: 7}
    default = merge_replay_guide(list(fresh), list(links), list(previous), days, NOW)
    explicit = merge_replay_guide(list(fresh), list(links), list(previous), days, NOW, redescribe=False)
    assert default == explicit


def test_inputs_are_not_modified():
    fresh, links, previous = _mixed_case()
    snapshot = repr((fresh, links, previous))
    _merge(fresh, links, previous, days={CH: 7, CH2: 7}, redescribe=True)
    assert repr((fresh, links, previous)) == snapshot


# ================================================================ time zones


def _local_offset_min(epoch):
    return time.localtime(epoch).tm_gmtoff // 60


def test_mixed_offsets_in_every_zone(clock_zone):
    """The export writes +0000, the new guide each instant's local offset, the
    published guide +0100: the decisions are made on epochs, so they are the
    same as with +0000 everywhere, in every pinned zone (DST change days
    included)."""
    now = clock_zone // 60 * 60

    def case(link_off, guide_off, prev_off):
        def g(start, stop, **kw):
            return _prog(start, stop, offset_min=guide_off(start), **kw)
        links = [_prog(now - 30 * M, now + 30 * M, title='Dos', link='u2', offset_min=link_off),
                 _prog(now + 30 * M, now + 90 * M, title='Tres', link='u3', offset_min=link_off)]
        previous = [_prog(now + 30 * M, now + 90 * M, title='Tres', link='u3', desc='old tres',
                          offset_min=prev_off),
                    _prog(now - 30 * M, now + 30 * M, title='Dos', link='u2', desc='old dos',
                          offset_min=prev_off)]
        fresh = [g(now - 2 * H, now + 35 * M, title='Dos', desc='new dos'),
                 g(now + 35 * M, now + 95 * M, title='Tres', desc='new tres'),
                 g(now + 95 * M, now + 3 * H, title='Despues', desc='new despues')]
        out, stats = merge_replay_guide(fresh, links, previous, {CH: 7}, now, redescribe=True)
        return ([(p.get('catchup-id') or '', xmltv_to_epoch(p['start']), xmltv_to_epoch(p['stop']), _desc(p))
                 for p in out], stats)

    utc = case(0, lambda e: 0, 0)
    mixed = case(0, _local_offset_min, 60)
    assert mixed == utc
    descs = {link: desc for link, _, _, desc in utc[0] if link}
    assert descs == {'u2': 'old dos', 'u3': 'new tres'}
    assert utc[1]['redescribe_slots'] == 1


# ================================================================ syncOne's flag


G_OLD, G_NEW = sr.OLD_GUIDE, sr.NEW_GUIDE
GUIDE = sr.guide_xml([sr.show(NOW, NOW + H, 'Show', 'd')])
SAVED = sr.guide_xml([sr.show(NOW, NOW + H, 'Saved show', 'd')])
FLAG_LINE = '[g1] syncOne, new guide address -- upcoming shows take its details'
KEYS = {'old': source_key(G_OLD), 'new': source_key(G_NEW), None: None}


def _syncone(tmp_path, monkeypatch, answer, stored=None, saved=None, saved_mtime=None, imp=None, **over):
    """syncOne of a guide-only import 'g1' (guide address G_NEW, download
    markers stored) whose epg_source_key names `stored` ('old', 'new' or
    None: an entry from before imports.69). The guide answers `answer`;
    `saved` is a saved guide in CACHE_LOC (epg_g1.xml) with `saved_mtime`;
    `over`: other entry fields. Returns (result, log lines, fetches)."""
    monkeypatch.setattr('imports.CACHE_LOC', str(tmp_path))
    lines, calls = [], []
    monkeypatch.setattr(Imports, 'log', lambda self, msg, level=None: lines.append((msg, level)))
    imp = imp or Imports()

    def fetch(url, etag=None, last_modified=None, **kw):
        calls.append((url, etag, last_modified))
        out = {'status': 0, 'content': None, 'etag': None, 'last_modified': None, 'error': None}
        out.update(answer)
        return out
    monkeypatch.setattr(imp, 'fetchSource', fetch)
    if saved is not None:
        path = tmp_path / 'epg_g1.xml'
        path.write_bytes(saved)
        if saved_mtime is not None:
            os.utime(str(path), (saved_mtime, saved_mtime))
    cfg = {'id': 'g1', 'enabled': True, 'epg_url': G_NEW, 'epg_etag': '"e"', 'epg_last_modified': 'lm'}
    if stored is not None:
        cfg['epg_source_key'] = KEYS[stored]
    cfg.update(over)
    result = imp.syncOne(cfg, existing_channels=[])
    assert 'epg_source_changed' not in result['updated_cfg'] and 'epg_cache_mtime' not in result['updated_cfg']
    return result, lines, calls


def _flagged(result, lines):
    flag = result.get('epg_source_changed')
    assert flag in (None, True)
    said = [lvl for m, lvl in lines if m == FLAG_LINE]
    assert said == ([xbmc.LOGINFO] if flag else [])
    return bool(flag)


OK200 = {'status': 200, 'content': GUIDE, 'etag': '"new"'}


def test_a_download_from_a_new_guide_address_is_flagged(tmp_path, monkeypatch):
    result, lines, calls = _syncone(tmp_path, monkeypatch, OK200, stored='old')
    assert _flagged(result, lines) is True
    assert calls == [(G_NEW, None, None)], 'imports.69: no markers to another address'
    assert result['updated_cfg']['epg_source_key'] == KEYS['new'] and result['epg_cache_written'] is True
    assert 'epg_cache_mtime' not in result, 'no fallback, no saved-guide time'


def test_a_download_from_a_new_guide_file_path_is_flagged(tmp_path, monkeypatch):
    """A guide given as a file path (epg_path, no epg_url): the key compared
    is imports.69's epg_key, made from the url or else the path."""
    old_path, new_path = str(tmp_path / 'old_guide.xml'), str(tmp_path / 'new_guide.xml')
    result, lines, calls = _syncone(tmp_path, monkeypatch, OK200, epg_url='', epg_path=new_path,
                                    epg_source_key=source_key(old_path))
    assert _flagged(result, lines) is True
    assert calls == [(new_path, None, None)]
    assert result['updated_cfg']['epg_source_key'] == source_key(new_path)


@pytest.mark.parametrize('stored', [None, 'new'], ids=['legacy', 'same-address'])
def test_a_legacy_entry_or_the_same_address_is_not_flagged(tmp_path, monkeypatch, stored):
    result, lines, _ = _syncone(tmp_path, monkeypatch, OK200, stored=stored)
    assert _flagged(result, lines) is False
    assert result['updated_cfg']['epg_source_key'] == KEYS['new']      # recorded (legacy) or kept


@pytest.mark.parametrize('stored', [None, 'new', 'old'])
def test_a_304_is_never_flagged(tmp_path, monkeypatch, stored):
    result, lines, _ = _syncone(tmp_path, monkeypatch, {'status': 304}, stored=stored, saved=SAVED)
    assert _flagged(result, lines) is False
    assert result['epg_pairs'], 'hydrated from the saved guide'
    assert result['updated_cfg']['epg_source_key'] == KEYS[stored or 'new']   # imports.69's R4 for a legacy one


@pytest.mark.parametrize('answer', [{'status': 0, 'error': 'network down'}, {'status': 200, 'content': b''},
                                    {'status': 200, 'content': b'<html>not a guide</html>'}],
                         ids=['download-failed', 'empty-body', 'not-a-guide'])
def test_the_saved_guide_fallback_is_never_flagged(tmp_path, monkeypatch, answer):
    """The saved copy holds the OLD address's guide: it must never re-describe."""
    result, lines, _ = _syncone(tmp_path, monkeypatch, answer, stored='old', saved=SAVED, saved_mtime=NOW - H)
    assert _flagged(result, lines) is False
    assert result['epg_fallback'] == 'using the cached guide' and result['epg_pairs']
    assert result['epg_cache_mtime'] == NOW - H
    assert result['updated_cfg']['epg_source_key'] == KEYS['old']


def test_the_saved_guides_time_is_taken_before_reading_it(tmp_path, monkeypatch):
    """A saved guide replaced while the fallback reads it can only be newer
    than recorded (which keeps the stale-export guard off)."""
    imp = Imports()
    real_read = imp._epgCacheRead

    def replaced_then_read(import_id):
        path = tmp_path / ('epg_%s.xml' % import_id)
        path.write_bytes(GUIDE)
        os.utime(str(path), (NOW, NOW))
        return real_read(import_id)
    monkeypatch.setattr(imp, '_epgCacheRead', replaced_then_read)
    result, _, _ = _syncone(tmp_path, monkeypatch, {'status': 0, 'error': 'down'}, stored='new', saved=SAVED,
                            saved_mtime=NOW - 5 * H, imp=imp)
    assert result['epg_cache_mtime'] == NOW - 5 * H
    assert os.stat(str(tmp_path / 'epg_g1.xml')).st_mtime == NOW


def test_no_saved_guide_on_the_fallback_records_no_time(tmp_path, monkeypatch):
    result, lines, _ = _syncone(tmp_path, monkeypatch, {'status': 0, 'error': 'down'}, stored='old')
    assert result['epg_cache_mtime'] is None and result['epg_keep_previous'] is True
    assert _flagged(result, lines) is False


def test_a_failed_cache_write_still_flags(tmp_path, monkeypatch):
    """The key stays the old one (imports.69), so the next download from the
    new address re-describes again: idempotent."""
    imp = Imports()
    monkeypatch.setattr(imp, '_epgCacheWrite', lambda import_id, content: False)
    result, lines, _ = _syncone(tmp_path, monkeypatch, OK200, stored='old', imp=imp)
    assert _flagged(result, lines) is True
    assert result['updated_cfg']['epg_source_key'] == KEYS['old'] and 'epg_cache_written' not in result


# ================================================================ syncAll end to end


def _news(n):
    """Movistar+'s export lists C1's aired show 'Uno', 'Dos' on air and 'Tres'
    upcoming, and Squirrel on air; the guide data holds them as published
    from the OLD guide ('old ...' details). The new guide has 'Uno' at the
    same second (rule 1), 'Dos' and 'Tres' 5 minutes later, and covers C1
    from n-4h to n+3h."""
    H, M = sr.H, sr.M
    c1 = [sr.show(n - 3 * H, n - 2 * H, 'Uno', link='u1'),
          sr.show(n - H, n + 30 * M, 'Dos', link='u2'),
          sr.show(n + 30 * M, n + 90 * M, 'Tres', link='u3')]
    c2 = [sr.show(n - 10 * M, n + 10 * M, 'Ardilla', link='sq')]
    previous = [sr.published_show(sr.C1, sr.show(s, e, t, 'old ' + t.lower(), link=k)) for s, e, t, _, k, _ in c1]
    previous.append(sr.published_show(sr.C2, sr.show(n - 10 * M, n + 10 * M, 'Ardilla', 'old ardilla', link='sq')))
    new_guide = [sr.show(n - 4 * H, n - 3 * H, 'Cero', 'new cero'),
                 sr.show(n - 3 * H, n - 2 * H + 5 * M, 'Uno', 'new uno'),
                 sr.show(n - 2 * H + 5 * M, n - H + 5 * M, 'Medio', 'new medio'),
                 sr.show(n - H + 5 * M, n + 35 * M, 'Dos', 'new dos'),
                 sr.show(n + 35 * M, n + 95 * M, 'Tres', 'new tres'),
                 sr.show(n + 95 * M, n + 3 * H, 'Despues', 'new despues')]
    return c1, c2, previous, new_guide


def _descs(run):
    return {p.get('catchup-id'): (p['desc'][0][0] if p.get('desc') else None)
            for p in run.guide.XMLTVDATA['programmes'] if p.get('catchup-id')}


def _redescribe_run(tmp_path, monkeypatch, stored, guide=None):
    """One real sync of the import with the guide address NEW_GUIDE, download
    markers stored, and epg_source_key naming `stored`."""
    rig = sr.make_rig(tmp_path, monkeypatch)
    c1, c2, previous, new_guide = _news(rig.now)
    sr.write_export(rig, c1, c2, age_s=sr.H)
    cfg = sr.entry(rig, epg_url=sr.NEW_GUIDE, epg_etag='"e"', epg_last_modified='lm')
    if stored is not None:
        cfg['epg_source_key'] = KEYS[stored]
    run = sr.sync(rig, monkeypatch, cfg, guide=guide or sr.guide_xml(new_guide), previous=previous)
    return rig, run


def _guide_fetches(run):
    return [c for c in run.calls if c[0] == sr.NEW_GUIDE]


def test_a_new_guide_address_re_describes_the_upcoming_show(tmp_path, monkeypatch):
    rig, run = _redescribe_run(tmp_path, monkeypatch, 'old')
    assert run.res['epg_source_changed'] is True
    assert _guide_fetches(run) == [(sr.NEW_GUIDE, None, None)], 'the old address\'s markers are not sent'
    assert _descs(run) == {'u1': 'new uno',          # aired: rule 1 (same second), as today
                           'u2': 'old dos',          # on air: started, today's rule 2
                           'u3': 'new tres',         # upcoming: the new guide's show 5 minutes later
                           'sq': 'old ardilla'}      # a channel the new guide doesn't list
    assert len(sr.logged(rig, 'replay links merged:')) == 1 and sr.logged(rig, 'redescribe-slots=1')
    assert sr.logged(rig, FLAG_LINE.replace('g1', 'mp'), xbmc.LOGINFO)
    assert run.res['updated_cfg']['epg_source_key'] == KEYS['new']
    assert sr.overlaps(run, sr.C1) == []


@pytest.mark.parametrize('stored', [None, 'new'], ids=['legacy', 'same-address'])
def test_no_re_describe_without_an_address_change(tmp_path, monkeypatch, stored):
    rig, run = _redescribe_run(tmp_path, monkeypatch, stored)
    assert 'epg_source_changed' not in run.res
    assert _guide_fetches(run) == [(sr.NEW_GUIDE, '"e"', 'lm')], 'markers sent: the same address, or legacy'
    assert _descs(run) == {'u1': 'new uno', 'u2': 'old dos', 'u3': 'old tres', 'sq': 'old ardilla'}
    assert sr.logged(rig, 'redescribe-slots=0') and not sr.logged(rig, 'redescribe-slots=1')


def test_the_keep_previous_path_never_re_describes(tmp_path, monkeypatch):
    """Nothing in the new guide matches: the guide data is kept, with this
    sync's links, and the flag changes nothing (decided: (a) works only from
    a guide it can use)."""
    nobody = sr.guide_xml([sr.show(NOW, NOW + H, 'x')], channel='Nobody', name='Nobody')
    rig, run = _redescribe_run(tmp_path, monkeypatch, 'old', guide=nobody)
    assert run.res['epg_source_changed'] is True
    assert sr.logged(rig, 'no guide entries matched — keeping the previous guide')
    assert _descs(run) == {'u1': 'old uno', 'u2': 'old dos', 'u3': 'old tres', 'sq': 'old ardilla'}
    assert sr.logged(rig, 'redescribe-slots=0') and not sr.logged(rig, 'redescribe-slots=1')


def test_an_import_without_link_replay_switches_its_guide_wholesale(tmp_path, monkeypatch):
    """Today's behaviour for every import without per-show links: the new
    guide replaces the old one, aired shows included."""
    rig = sr.make_rig(tmp_path, monkeypatch)
    c1, c2, previous, new_guide = _news(rig.now)
    sr.write_export(rig, c1, c2, age_s=sr.H)
    cfg = sr.entry(rig, epg_url=sr.NEW_GUIDE, epg_source_key=KEYS['old'])
    run = sr.sync(rig, monkeypatch, cfg, guide=sr.guide_xml(new_guide), previous=previous,
                  chans=sr.records(link=False))
    assert run.res['epg_source_changed'] is True
    assert [sr.row(p) for p in sr.on(run, sr.C1)] == [(s, e, t, d, None) for s, e, t, d, _, _ in new_guide]
    assert not sr.logged(rig, 'replay links merged:')


# ================================================================ across steps: the dashboard Save


def test_a_guide_address_saved_in_the_dashboard_re_describes_at_the_next_sync(request, monkeypatch):
    """imports.69 R2/R3 as imports.70 needs them: the Save keeps
    epg_source_key and drops the old address's download markers, so the next
    sync downloads the new address in full, flags it and re-describes."""
    env = request.getfixturevalue('dashboard_env')
    folder = env.path.parent / 'addon_data' / 'plugin.video.movistarplus'
    folder.mkdir(parents=True)
    (folder / 'channels.m3u8').write_text('#EXTM3U\n')
    n = int(time.time())
    c1, c2, previous, new_guide = _news(n)
    (folder / 'epg.xml').write_bytes(sr.export_xml(c1, c2))
    links_ns = str(os.stat(str(folder / 'epg.xml')).st_mtime_ns)
    m3u_path = str(folder / 'channels.m3u8')
    entry = {'id': 'mp', 'name': 'MP', 'enabled': True, 'm3u_url': '', 'm3u_path': m3u_path,
             'epg_url': G_OLD, 'epg_path': '', 'start_num': 1, 'refresh_interval_min': 600,
             'respect_source_numbers': True, 'rediscover_deleted': False, 'detect_placeholder_pictures': False,
             'tombstones': [], 'last_sync_at': n - 60, 'last_status': 'unchanged', 'last_error': None,
             'playlist_rev': 1, 'fail_count': 0, 'failed_since': None, 'links_mtime_ns': links_ns,
             'epg_etag': '"old-etag"', 'epg_last_modified': 'old-lm', 'epg_fail_count': 0,
             'm3u_source_key': source_key(m3u_path), 'epg_source_key': KEYS['old']}
    entry['config_key'] = config_fingerprint(entry)
    env.write(channels=sr.records(), imports=[entry])

    handler = env.save({'op': 'edit', 'id': 'mp', 'entry': {'id': 'mp', 'epg_url': G_NEW}})
    assert handler.statuses == [200] and env.reply(handler)['changed'] is True
    saved = [i for i in env.disk()['imports'] if i['id'] == 'mp'][0]
    assert saved['epg_url'] == G_NEW and saved['config_key'] == 'pending'
    assert saved['epg_source_key'] == KEYS['old'], 'R2: a Save never touches the key'
    assert not set(GUIDE_RESET).intersection(saved), 'R3: the old address\'s markers are gone'
    assert (saved['last_sync_at'], saved['links_mtime_ns']) == (n - 60, links_ns)

    import imports as imports_mod
    real_bool = imports_mod.SETTINGS.getSettingBool
    monkeypatch.setattr(imports_mod.SETTINGS, 'getSettingBool',
                        lambda key, *a, **k: False if key == 'Imports_Cache_Logos' else real_bool(key, *a, **k))
    lines, calls = [], []
    monkeypatch.setattr(Imports, 'log', lambda self, msg, level=None: lines.append((msg, level)))
    real_fetch = Imports.fetchSource

    def fetch(self, url, etag=None, last_modified=None, **kw):
        calls.append((url, etag, last_modified))
        if url.endswith('epg.xml'):
            return real_fetch(self, url, etag=etag, last_modified=last_modified)
        base = {'status': 304, 'content': None, 'etag': None, 'last_modified': None, 'error': None}
        if url == G_NEW:
            base.update(status=200, content=sr.guide_xml(new_guide), etag='"new-etag"')
        elif not url.endswith('channels.m3u8'):
            raise AssertionError('unexpected address')
        return base
    monkeypatch.setattr(Imports, 'fetchSource', fetch)
    data = sr.Guide(previous)
    results = Imports(channels=env.open(), m3u=sr.Playlist(), xmltv=data).syncAll()

    assert [c for c in calls if c[0] in (G_OLD, G_NEW)] == [(G_NEW, None, None)], 'a full download, no markers'
    assert results['mp']['epg_source_changed'] is True
    descs = {p['catchup-id']: p['desc'][0][0] for p in data.XMLTVDATA['programmes'] if p.get('catchup-id')}
    assert descs == {'u1': 'new uno', 'u2': 'old dos', 'u3': 'new tres', 'sq': 'old ardilla'}
    assert [m for m, _ in lines if 'redescribe-slots=1' in m]
    synced = [i for i in json.loads(env.path.read_text())['imports'] if i['id'] == 'mp'][0]
    assert synced['epg_source_key'] == KEYS['new'] and synced['epg_etag'] == '"new-etag"'
    assert synced['config_key'] == config_fingerprint(synced) and synced['last_sync_at'] >= n
