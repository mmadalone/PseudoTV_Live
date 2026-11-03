"""imports.59 — replay_helpers (pure): the Movistar+ replay-link merge and
its switches.

Movistar+ declares link-based catch-up (catchup="default"
catchup-source="{catchup-id}") and writes one replay link per show into its
own guide export (epg.xml next to channels.m3u8). replay_helpers merges those
links into the import's rich guide (davidmuma), keeps linked history for the
replay window, and holds the one on/off switch (link_replay_days).

Covered here (no Kodi stubs needed — the module is pure):
  - merge_replay_guide: same-start details, the kept-details fallback (two of
    link / start / title, colour labels ignored), overlaps, the window,
    duplicates, empty export, pass-through, DST fixtures, unparseable entries,
    slot origin (only this sync's export entries take kept details);
  - exporter_for, xmltv_to_epoch, is_link_catchup, replay_days,
    link_replay_days, channels_with_links;
  - server.py's two grid-time parsers now delegate to xmltv_to_epoch and give
    the same results as before for valid input (S10.5).
"""
import ast
import calendar
import os
import re
import time

import pytest

import replay_helpers as rh
from replay_helpers import (merge_replay_guide, exporter_for, xmltv_to_epoch,
                            is_link_catchup, replay_days, link_replay_days,
                            channels_with_links, REPLAY_HISTORY_MAX_DAYS)


HERE = os.path.dirname(os.path.abspath(__file__))
LIB  = os.path.join(os.path.dirname(HERE), 'resources', 'lib')

CH   = 'C1@mp'
CH2  = 'C2@mp'
NOW  = 1800000000          # 2027-01-15 08:00 UTC — fixed so tests are deterministic
H    = 3600


def _ts(epoch, offset_min=0):
    """XMLTV time for `epoch`, written with a +HHMM / -HHMM offset."""
    sign = '+' if offset_min >= 0 else '-'
    off = abs(offset_min)
    return (time.strftime('%Y%m%d%H%M%S', time.gmtime(epoch + offset_min * 60))
            + ' %s%02d%02d' % (sign, off // 60, off % 60))


def _prog(start, stop, ch=CH, title='Show', link=None, desc=None, **extra):
    p = {'channel': ch, 'start': _ts(start), 'stop': _ts(stop), 'title': [(title, 'es')]}
    if link is not None:
        p['catchup-id'] = link
    if desc is not None:
        p['desc'] = [(desc, 'es')]
    p.update(extra)
    return p


def _merge(fresh=(), links=(), previous=(), days=None, now=NOW):
    days = {CH: 7} if days is None else days
    out, stats = merge_replay_guide(list(fresh), list(links), list(previous), days, now)
    return out, stats


def _by_start(out, ch=CH):
    return {p['start']: p for p in out if p.get('channel') == ch}


# ================================================================ merge rule 1


def test_same_start_takes_guide_details_and_keeps_slot_and_link():
    fresh = [_prog(NOW, NOW + H, title='Rich title', desc='Rich desc', icon=[{'src': 'pic'}])]
    links = [_prog(NOW, NOW + H, title='Plain', link='u0', **{'sub-title': [('ep', '')]})]
    out, stats = _merge(fresh, links)
    assert len(out) == 1
    p = out[0]
    assert p['catchup-id'] == 'u0'
    assert p['desc'] == [('Rich desc', 'es')]
    assert p['title'] == [('Rich title', 'es')]
    assert p['icon'] == [{'src': 'pic'}]
    assert (p['start'], p['stop'], p['channel']) == (_ts(NOW), _ts(NOW + H), CH)
    assert stats['with_content'] == 1 and stats['linked'] == 1


def test_inputs_are_not_modified():
    fresh = [_prog(NOW, NOW + H, desc='Rich')]
    links = [_prog(NOW, NOW + H, link='u0')]
    previous = [_prog(NOW - 2 * H, NOW - H, link='old', desc='kept')]
    snapshot = repr((fresh, links, previous))
    _merge(fresh, links, previous)
    assert repr((fresh, links, previous)) == snapshot


# ================================================================ merge rule 2 (kept details)


def test_kept_details_same_link_and_start_with_other_title():
    """No guide partner any more (its window moved on): the already-published
    show matches on link + start -> its details stay, with the new link/time."""
    previous = [_prog(NOW - 3 * H, NOW - 2 * H, title='Cine: Película', link='u7', desc='Rich old')]
    links = [_prog(NOW - 3 * H, NOW - 2 * H, title='Pelicula de la tarde', link='u7')]
    out, stats = _merge(links=links, previous=previous)
    p = _by_start(out)[_ts(NOW - 3 * H)]
    assert p['desc'] == [('Rich old', 'es')] and p['catchup-id'] == 'u7'
    assert stats['kept_details'] == 1


def test_kept_details_same_link_and_title_retimed_show_takes_new_time():
    previous = [_prog(NOW - 3 * H, NOW - 2 * H, title='Informe', link='u7', desc='Rich old')]
    links = [_prog(NOW - 3 * H + 600, NOW - 2 * H + 600, title='INFORME', link='u7')]
    out, _ = _merge(links=links, previous=previous)
    progs = [p for p in out if p['channel'] == CH]
    assert len(progs) == 1, 'the re-timed export entry replaces the overlapping published one'
    p = progs[0]
    assert p['start'] == _ts(NOW - 3 * H + 600) and p['stop'] == _ts(NOW - 2 * H + 600)
    assert p['desc'] == [('Rich old', 'es')] and p['catchup-id'] == 'u7'


def test_kept_details_same_start_and_title_new_link():
    previous = [_prog(NOW - 3 * H, NOW - 2 * H, title='Telediario', link='old-link', desc='Rich old')]
    links = [_prog(NOW - 3 * H, NOW - 2 * H, title='Telediario', link='new-link')]
    out, _ = _merge(links=links, previous=previous)
    p = _by_start(out)[_ts(NOW - 3 * H)]
    assert p['catchup-id'] == 'new-link' and p['desc'] == [('Rich old', 'es')]


def test_same_link_but_other_start_and_title_is_not_used():
    previous = [_prog(NOW - 6 * H, NOW - 5 * H, title='Documental', link='u7', desc='Rich old')]
    links = [_prog(NOW - 3 * H, NOW - 2 * H, title='Noticias', link='u7')]
    out, stats = _merge(links=links, previous=previous)
    p = _by_start(out)[_ts(NOW - 3 * H)]
    assert 'desc' not in p and p['title'] == [('Noticias', 'es')]
    assert stats['kept_details'] == 0


def test_start_clash_with_another_title_and_link_is_not_used():
    """Only one of three matches (same start): another show's details must not
    be copied."""
    previous = [_prog(NOW - 3 * H, NOW - 2 * H, title='Documental', link='other', desc='Rich old')]
    links = [_prog(NOW - 3 * H, NOW - 2 * H, title='Noticias', link='u7')]
    out, stats = _merge(links=links, previous=previous)
    p = _by_start(out)[_ts(NOW - 3 * H)]
    assert 'desc' not in p and p['catchup-id'] == 'u7'
    assert stats['kept_details'] == 0


def test_colour_labels_case_accents_and_punctuation_ignored_in_titles():
    previous = [_prog(NOW - 3 * H, NOW - 2 * H, title='Hudson y Rex [COLOR orange](T4 E5)[/COLOR]',
                      link='old', desc='Rich old')]
    links = [_prog(NOW - 3 * H, NOW - 2 * H, title='HUDSON Y REX', link='new')]
    out, _ = _merge(links=links, previous=previous)
    assert _by_start(out)[_ts(NOW - 3 * H)]['desc'] == [('Rich old', 'es')]
    # one title may extend the other; accents don't matter
    previous = [_prog(NOW - 3 * H, NOW - 2 * H, title='Pelota vasca: final', link='old', desc='Rich')]
    links = [_prog(NOW - 3 * H, NOW - 2 * H, title='Pelóta vasca', link='new')]
    out, _ = _merge(links=links, previous=previous)
    assert _by_start(out)[_ts(NOW - 3 * H)]['desc'] == [('Rich', 'es')]


def test_kept_details_need_a_description():
    """A published show without a description (empty <desc/>) has no details
    to keep: the export entry stays plain."""
    previous = [_prog(NOW - 3 * H, NOW - 2 * H, title='Telediario', link='u7', desc='')]
    links = [_prog(NOW - 3 * H, NOW - 2 * H, title='Telediario', link='u7')]
    out, stats = _merge(links=links, previous=previous)
    assert stats['kept_details'] == 0
    assert out == links, 'L wins the slot and keeps its own (plain) entry'


def test_only_export_slots_take_kept_details():
    """Slot origin: rule 2 (kept details) applies only to this sync's export
    entries. A kept (H) slot keeps what was published, even when another
    published show with details matches it on link and start."""
    plain_kept = _prog(NOW - 5 * H, NOW - 4 * H, title='Plain kept', link='h1')
    rich_twin = _prog(NOW - 5 * H, NOW - 4 * H, title='Plain kept', link='h1', desc='Rich')
    out, stats = _merge(previous=[plain_kept, rich_twin])
    assert out == [plain_kept], 'overlapping kept slots: the first is kept, as published'
    assert stats['kept_details'] == 0
    # the same entry arriving from this sync's export does take the details
    out, stats = _merge(links=[dict(plain_kept)], previous=[rich_twin])
    assert [p.get('desc') for p in out] == [[('Rich', 'es')]]
    assert out[0]['catchup-id'] == 'h1' and stats['kept_details'] == 1


# ================================================================ overlaps / history / window


def test_guide_programmes_overlapping_the_timeline_are_dropped_others_kept():
    links = [_prog(NOW, NOW + H, link='u0')]
    fresh = [_prog(NOW - 1800, NOW + 1800, title='Overlaps', desc='x'),
             _prog(NOW + 2 * H, NOW + 3 * H, title='Later', desc='y')]
    out, stats = _merge(fresh, links)
    titles = [p['title'][0][0] for p in out]
    assert 'Overlaps' not in titles and 'Later' in titles
    later = [p for p in out if p['title'][0][0] == 'Later'][0]
    assert not later.get('catchup-id'), 'guide programmes outside the timeline stay unlinked'
    assert stats['fresh_dropped'] == 1 and stats['fresh_kept'] == 1


def test_history_kept_without_overlap_dropped_with_one():
    previous = [_prog(NOW - 4 * H, NOW - 3 * H, title='Kept', link='h1', desc='old'),
                _prog(NOW - 1800, NOW + 1800, title='Replaced', link='h2', desc='old')]
    links = [_prog(NOW, NOW + H, title='New', link='u0')]
    out, stats = _merge(links=links, previous=previous)
    starts = _by_start(out)
    assert starts[_ts(NOW - 4 * H)]['catchup-id'] == 'h1'
    assert _ts(NOW - 1800) not in starts
    assert starts[_ts(NOW)]['catchup-id'] == 'u0'
    assert stats['history_in'] == 2 and stats['history_kept'] == 1


def test_window_drops_older_links_and_history():
    days = 3
    previous = [_prog(NOW - 4 * 86400, NOW - 4 * 86400 + H, link='too-old', desc='x'),
                _prog(NOW - 2 * 86400, NOW - 2 * 86400 + H, link='inside', desc='x')]
    links = [_prog(NOW - 5 * 86400, NOW - 5 * 86400 + H, link='old-export'),
             _prog(NOW, NOW + H, link='u0')]
    out, _ = _merge(links=links, previous=previous, days={CH: days})
    linked = sorted(p['catchup-id'] for p in out)
    assert linked == ['inside', 'u0']


def test_duplicate_and_overlapping_links_keep_the_first():
    links = [_prog(NOW, NOW + H, title='First', link='a'),
             _prog(NOW, NOW + H, title='Duplicate', link='b'),
             _prog(NOW + 1800, NOW + 2 * H, title='Overlap', link='c'),
             _prog(NOW + 2 * H, NOW + 3 * H, title='Next', link='d')]
    out, _ = _merge(links=links)
    assert [p['catchup-id'] for p in out] == ['a', 'd']


def test_empty_export_keeps_history():
    previous = [_prog(NOW - 2 * H, NOW - H, link='h1', desc='kept'),
                _prog(NOW - H, NOW, link='h2', desc='kept')]
    fresh = [_prog(NOW - 2 * H, NOW - H, title='Fresh', desc='rich'),
             _prog(NOW + H, NOW + 2 * H, title='Future', desc='rich')]
    out, _ = _merge(fresh, [], previous)
    starts = _by_start(out)
    assert starts[_ts(NOW - 2 * H)]['catchup-id'] == 'h1'
    assert starts[_ts(NOW - 2 * H)]['desc'] == [('rich', 'es')], 'rule 1 applies to kept slots too'
    assert starts[_ts(NOW - H)]['catchup-id'] == 'h2'
    assert not starts[_ts(NOW + H)].get('catchup-id')


def test_non_merge_channels_pass_through_in_order():
    other = [_prog(NOW + 2 * H, NOW + 3 * H, ch='X@other', title='b'),
             _prog(NOW, NOW + H, ch='X@other', title='a'),
             _prog(NOW, NOW + H, ch=CH2, title='not link-based')]
    links = [_prog(NOW, NOW + H, link='u0')]
    out, _ = _merge(other, links, days={CH: 7})
    assert out[:3] == other, 'non-merge programmes: same objects, same order'
    # a link-based channel without any link (fresh or kept) is not merged either
    out, stats = _merge([_prog(NOW, NOW + H, ch=CH2, title='plain')], [], [], days={CH2: 7})
    assert out[0]['title'] == [('plain', 'es')] and stats['channels'] == 0


def test_unparseable_fresh_programmes_are_kept_unchanged():
    bad = {'channel': CH, 'start': 'garbage', 'stop': None, 'title': [('Bad', '')]}
    zero = _prog(NOW + 5 * H, NOW + 5 * H, title='Zero length')
    links = [_prog(NOW, NOW + H, link='u0')]
    out, _ = _merge([bad, zero], links)
    assert bad in out and zero in out


def test_links_without_a_usable_link_or_time_are_ignored():
    links = [_prog(NOW, NOW + H, link=''), _prog(NOW, NOW + H, link=None),
             {'channel': CH, 'start': 'x', 'stop': 'y', 'catchup-id': 'u9'},
             _prog(NOW, NOW + H, ch='Z@nope', link='u8')]
    out, stats = _merge(links=links)
    assert out == [] and stats['links_in'] == 0


def test_dst_fall_back_2027_10_31_is_decided_by_epoch():
    """+0200 -> +0100 at 2027-10-31 01:00 UTC. The repeated local hour 02:00
    exists twice; epochs keep the two shows apart, and an export entry written
    in +0000 matches a guide entry written in +0200 for the same instant."""
    first = calendar.timegm((2027, 10, 31, 0, 0, 0))    # 02:00 +0200
    second = calendar.timegm((2027, 10, 31, 1, 0, 0))   # 02:00 +0100
    fresh = [{'channel': CH, 'start': '20271031020000 +0200', 'stop': '20271031020000 +0100',
              'title': [('Night A', '')], 'desc': [('rich A', '')]},
             {'channel': CH, 'start': '20271031020000 +0100', 'stop': '20271031030000 +0100',
              'title': [('Night B', '')], 'desc': [('rich B', '')]}]
    links = [{'channel': CH, 'start': '20271031000000 +0000', 'stop': '20271031010000 +0000',
              'title': [('A', '')], 'catchup-id': 'la'},
             {'channel': CH, 'start': '20271031010000 +0000', 'stop': '20271031020000 +0000',
              'title': [('B', '')], 'catchup-id': 'lb'}]
    out, stats = _merge(fresh, links, now=second + 86400)
    assert [(xmltv_to_epoch(p['start']), p['catchup-id'], p['desc'][0][0]) for p in out] == \
        [(first, 'la', 'rich A'), (second, 'lb', 'rich B')]
    assert stats['with_content'] == 2 and stats['fresh_dropped'] == 2 and stats['fresh_kept'] == 0


def test_dst_spring_forward_2027_03_28_is_decided_by_epoch():
    """+0100 -> +0200 at 2027-03-28 01:00 UTC: 02:00 +0100 and 03:00 +0200 are
    the same instant; 01:30 +0100 ... 03:30 +0200 is one hour long."""
    links = [{'channel': CH, 'start': '20270328003000 +0000', 'stop': '20270328013000 +0000',
              'title': [('Madrugada', '')], 'catchup-id': 'l1'}]
    fresh = [{'channel': CH, 'start': '20270328013000 +0100', 'stop': '20270328033000 +0200',
              'title': [('Madrugada', '')], 'desc': [('rich', '')]},
             {'channel': CH, 'start': '20270328033000 +0200', 'stop': '20270328043000 +0200',
              'title': [('Next', '')], 'desc': [('rich next', '')]}]
    out, _ = _merge(fresh, links, now=calendar.timegm((2027, 3, 29, 0, 0, 0)))
    assert [(p.get('catchup-id') or '', p['desc'][0][0]) for p in out] == [('l1', 'rich'), ('', 'rich next')]


def test_stats_count_what_happened():
    fresh = [_prog(NOW, NOW + H, desc='Rich'), _prog(NOW + 5 * H, NOW + 6 * H, desc='Later')]
    links = [_prog(NOW, NOW + H, link='u0'), _prog(NOW + H, NOW + 2 * H, link='u1')]
    _, stats = _merge(fresh, links)
    assert stats == {'links_in': 2, 'history_in': 0, 'history_kept': 0, 'linked': 2,
                     'with_content': 1, 'plain': 1, 'fresh_kept': 1, 'fresh_dropped': 1,
                     'channels': 1, 'kept_details': 0, 'overlap_details': 0}


# ================================================================ exporter_for


@pytest.mark.parametrize('path', [
    'special://userdata/addon_data/plugin.video.movistarplus/channels.m3u8',
    'special://profile/addon_data/plugin.video.movistarplus/channels.m3u8',
    '/home/user/.kodi/userdata/addon_data/plugin.video.movistarplus/channels.m3u8',
    '  special://userdata/addon_data/plugin.video.movistarplus/channels.m3u8  ',
])
def test_exporter_for_local_movistar_playlist(path):
    res = exporter_for({'m3u_url': '', 'm3u_path': path})
    assert res is not None
    addon_id, spec, links = res
    assert addon_id == 'plugin.video.movistarplus'
    assert spec is rh.KNOWN_EXPORTERS['plugin.video.movistarplus']
    assert links == path.strip()[:-len('channels.m3u8')] + 'epg.xml'
    assert spec['action'] == 'RunPlugin(plugin://plugin.video.movistarplus/?action=export_epg_now)'
    assert spec['setting'] == 'export_epg' and spec['catchup'] == ('default', '{catchup-id}')


def test_exporter_for_m3u_url_wins_and_remote_never_matches():
    local = 'special://userdata/addon_data/plugin.video.movistarplus/channels.m3u8'
    assert exporter_for({'m3u_url': 'https://example.invalid/list.m3u', 'm3u_path': local}) is None
    for remote in ('http://host/addon_data/plugin.video.movistarplus/channels.m3u8',
                   'https://host/addon_data/plugin.video.movistarplus/channels.m3u8',
                   'smb://nas/kodi/userdata/addon_data/plugin.video.movistarplus/channels.m3u8'):
        assert exporter_for({'m3u_url': remote}) is None, remote
    assert exporter_for({'m3u_url': local}) is not None


@pytest.mark.parametrize('cfg', [
    {'m3u_path': 'special://userdata/addon_data/plugin.video.movistarplus/other.m3u8'},
    {'m3u_path': 'special://userdata/addon_data/plugin.video.other/channels.m3u8'},
    {'m3u_path': 'special://userdata/addon_data/plugin.video.movistarplus/sub/channels.m3u8'},
    {'m3u_path': ''}, {}, None, {'m3u_path': 5}, 'not a dict',
])
def test_exporter_for_no_match(cfg):
    assert exporter_for(cfg) is None


# ================================================================ xmltv_to_epoch


def test_xmltv_to_epoch_offsets():
    base = calendar.timegm((2026, 11, 3, 12, 0, 0))
    assert xmltv_to_epoch('20261103120000 +0000') == base
    assert xmltv_to_epoch('20261103130000 +0100') == base
    assert xmltv_to_epoch('20261103070000 -0500') == base
    assert xmltv_to_epoch('20261103173000 +0530') == base
    assert xmltv_to_epoch('  20261103120000  +0000 ') == base


def test_xmltv_to_epoch_without_offset_is_local_time():
    assert xmltv_to_epoch('20261103120000') == int(time.mktime((2026, 11, 3, 12, 0, 0, 0, 0, -1)))


@pytest.mark.parametrize('bad', [None, '', 5, 'garbage', '2026110312000', '202611031200xx +0000',
                                 '20261303120000 +0000', '20261103250000', '20261103120000 +01:00',
                                 '20261103120000 GMT', '20261103120000 +01', b'20261103120000'])
def test_xmltv_to_epoch_invalid_gives_none(bad):
    assert xmltv_to_epoch(bad) is None


# ================================================================ replay switches


def _rec(**over):
    rec = {'id': 'A@mp', 'type': 'import', 'enabled': True, 'is_orphan': False, 'unallocated': False,
           'catchup': 'default', 'catchup-source': '{catchup-id}', 'catchup-days': '7'}
    rec.update(over)
    return rec


def test_is_link_catchup():
    assert is_link_catchup(_rec())
    assert is_link_catchup(_rec(catchup='append', **{'catchup-source': '&id={catchup-id}'}))
    assert is_link_catchup(_rec(catchup='vod', **{'catchup-source': ''}))
    assert not is_link_catchup(_rec(**{'catchup-source': ''}))
    assert not is_link_catchup(_rec(**{'catchup-source': '&start={utc}'}))
    assert not is_link_catchup(_rec(catchup='vod', **{'catchup-source': 'http://x/{utc}'}))
    assert not is_link_catchup(_rec(**{'catchup-source': 5}))
    assert not is_link_catchup(None) and not is_link_catchup(['x'])


@pytest.mark.parametrize('over,days', [
    ({}, 7), ({'catchup-days': '3'}, 3), ({'catchup-days': 3}, 3), ({'catchup-days': '9'}, 7),
    ({'catchup-days': '-1'}, 7), ({'catchup-days': '0'}, 7), ({'catchup-days': ''}, 7),
    ({'catchup-days': None}, 7), ({'catchup-days': 'abc'}, 7),
    ({'is_orphan': True}, 0), ({'unallocated': True}, 0), ({'enabled': False}, 0),
    ({'type': 'custom'}, 0), ({'catchup-source': ''}, 0),
])
def test_replay_days(over, days):
    rec = _rec(**over)
    if over.get('catchup-days', 'x') is None:
        rec.pop('catchup-days')
    assert replay_days(rec) == days


def test_replay_days_odd_records():
    assert replay_days(None) == 0 and replay_days('x') == 0 and replay_days([]) == 0
    assert REPLAY_HISTORY_MAX_DAYS == 7


def test_link_replay_days():
    recs = [_rec(id='A@mp'), _rec(id='B@mp', **{'catchup-days': '3'}),
            _rec(id='C@mp', **{'catchup-source': ''}), _rec(id='D@mp', enabled=False),
            _rec(id=''), _rec(id=None), None, 'junk', {'id': 'E@mp'}]
    assert link_replay_days(recs) == {'A@mp': 7, 'B@mp': 3}
    assert link_replay_days([]) == {} and link_replay_days(None) == {}
    stage1 = [_rec(id='A@mp', **{'catchup-source': '', 'catchup-days': '5'})]
    assert link_replay_days(stage1) == {}, 'old catch-up row restored: link replay off'


# ================================================================ channels_with_links


def test_channels_with_links_on_air_decides():
    progs = [_prog(NOW - 2 * H, NOW - H, link='old'),
             _prog(NOW - H, NOW + H, link=''),                  # on air, no link
             _prog(NOW - H, NOW + H, ch=CH2, link='u'),         # on air, link
             _prog(NOW + H, NOW + 2 * H, ch=CH2, link='')]
    assert channels_with_links(progs, {CH, CH2}, NOW) == {CH: False, CH2: True}


def test_channels_with_links_latest_started_decides_without_on_air():
    progs = [_prog(NOW - 3 * H, NOW - 2 * H, link=''),
             _prog(NOW - 2 * H, NOW - H, link='u1'),
             _prog(NOW + H, NOW + 2 * H, link='')]
    assert channels_with_links(progs, {CH}, NOW) == {CH: True}
    assert channels_with_links([_prog(NOW + H, NOW + 2 * H, link='u')], {CH, CH2}, NOW) == {CH: False, CH2: False}
    assert channels_with_links([], {CH}, NOW) == {CH: False}


# ================================================================ server.py grid times (S10.5)


def _old_server_xmltv_to_ts(s):
    """The pre-imports.59 inline parser from server.py, kept here as the
    reference for 'same results for valid input'."""
    if not s: return 0
    s = s.strip()
    parts = s.split(' ', 1)
    base = parts[0]
    tz_suffix = parts[1].strip() if len(parts) > 1 else ''
    if len(base) < 14: return 0
    try:
        st = time.strptime(base[:14], '%Y%m%d%H%M%S')
        if tz_suffix and len(tz_suffix) >= 5 and tz_suffix[0] in '+-':
            sign = 1 if tz_suffix[0] == '+' else -1
            return calendar.timegm(st) - sign * (int(tz_suffix[1:3]) * 3600 + int(tz_suffix[3:5]) * 60)
        return int(time.mktime(st))
    except Exception:
        return 0


@pytest.mark.parametrize('s', ['20261103120000 +0000', '20261103120000 +0100', '20261103120000 -0330',
                               '20271031020000 +0200', '20271031020000 +0100', '20270328033000 +0200',
                               '20261103120000', '20270328023000', '  20261103120000 +0000  ', '19700101000000 +0000'])
def test_server_grid_time_same_for_valid_input(s):
    assert (xmltv_to_epoch(s) or 0) == _old_server_xmltv_to_ts(s)


@pytest.mark.parametrize('s', [None, '', 'garbage', '2026', '20261303120000 +0000', '20261103120000 +01:00'])
def test_server_grid_time_invalid_gives_zero(s):
    assert (xmltv_to_epoch(s) or 0) == 0 == _old_server_xmltv_to_ts(s)


def test_server_parsers_delegate_to_xmltv_to_epoch():
    with open(os.path.join(LIB, 'server.py')) as f:
        src = f.read()
    tree = ast.parse(src)
    defs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == '_xmltv_to_ts']
    assert len(defs) == 2, 'both grid parsers still exist (prewarm + endpoint)'
    for d in defs:
        body = ast.get_source_segment(src, d)
        assert 'xmltv_to_epoch(s) or 0' in body
        assert 'strptime' not in body and 'timegm' not in body, 'the duplicate parsing logic is gone'
    assert re.search(r'^from\s+replay_helpers\s+import\s+xmltv_to_epoch\s*$', src, re.M)


def test_replay_helpers_is_pure():
    """No Kodi or addon imports (same contract as filter_helpers/logo_links)."""
    with open(os.path.join(LIB, 'replay_helpers.py')) as f:
        tree = ast.parse(f.read())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split('.')[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add((node.module or '').split('.')[0])
    assert names <= {'calendar', 'datetime', 're', 'time', 'unicodedata', 'bisect', 'operator'}, names
