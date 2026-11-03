"""imports.59 (S10.3 rule 3) — shows the guide source has a few minutes off
keep their details.

merge_replay_guide gave a linked Movistar+ show the guide source's (davidmuma)
details only when a davidmuma show started at the same second (rule 1) or an
already-published show matched it (rule 2); otherwise the show was published
with its title only. On real files about 490 shows a day had no same-start
davidmuma show although davidmuma had the same show a few minutes off.

Rule 3, for a slot with no details of its own that rules 1-2 left empty: the
fresh programmes that overlap the slot and have the same title are the
candidates. Season/episode tags tell episodes apart -- davidmuma puts them in
the title as a colour label ('Alerta Cobra [COLOR tomato]T22 E9[/COLOR]'),
the export starts its sub-title with them ('T22 E9 "El asesino de reyes"',
'T1 "Episodio 2"'):
  - exactly one candidate whose tag agrees: it is used;
  - no agreeing candidate: the only candidate is used when its tag can't be
    compared (never when it disagrees) and it overlaps no other slot whose
    show has the same title (one-to-one);
  - anything else: titles only, as before.
The slot keeps its own start, stop, channel and replay link. The fresh show
used still overlaps the slot, so it is not also published without a link.
"""
import time

import pytest

import replay_helpers as rh
from replay_helpers import merge_replay_guide


CH  = 'C1@mp'
NOW = 1800000000          # 2027-01-15 08:00 UTC — fixed so tests are deterministic
H   = 3600
M   = 60
X   = NOW - 3 * H         # a past slot inside the replay window


def _ts(epoch):
    return time.strftime('%Y%m%d%H%M%S', time.gmtime(epoch)) + ' +0000'


def _prog(start, stop, title, link=None, desc=None, sub=None, **extra):
    p = {'channel': CH, 'start': _ts(start), 'stop': _ts(stop), 'title': [(title, 'es')]}
    if link is not None:
        p['catchup-id'] = link
    if desc is not None:
        p['desc'] = [(desc, 'es')]
    if sub is not None:
        p['sub-title'] = [(sub, 'es')]
    p.update(extra)
    return p


def _merge(fresh=(), links=(), previous=()):
    return merge_replay_guide(list(fresh), list(links), list(previous), {CH: 7}, NOW)


def _linked(out):
    return [p for p in out if p.get('catchup-id')]


def _desc(p):
    return p['desc'][0][0] if p.get('desc') else None


# ================================================================ episode tags pick the right show


def test_la_chaparra_takes_the_agreeing_episode_not_the_larger_overlap():
    """Export E2 at X (47 min); davidmuma E1 at X-11 (46 min, overlaps 35
    min) and E2 at X+35 (overlaps 12 min). Time alone picks E1."""
    links = [_prog(X, X + 47 * M, 'La Chaparra', link='u2', sub='T1 "Episodio 2"')]
    fresh = [_prog(X - 11 * M, X + 35 * M, 'La Chaparra [COLOR tomato]E1[/COLOR]', desc='Episodio uno'),
             _prog(X + 35 * M, X + 81 * M, 'La Chaparra [COLOR tomato]E2[/COLOR]', desc='Episodio dos')]
    out, stats = _merge(fresh, links)
    assert [_desc(p) for p in out] == ['Episodio dos']
    p = out[0]
    assert (p['start'], p['stop'], p['channel'], p['catchup-id']) == (_ts(X), _ts(X + 47 * M), CH, 'u2')
    assert stats['overlap_details'] == 1 and stats['with_content'] == 1 and stats['plain'] == 0


def test_alerta_cobra_takes_the_agreeing_episode():
    links = [_prog(X, X + 45 * M, 'Alerta Cobra', link='u9', sub='T22 E9 "El asesino de reyes"')]
    fresh = [_prog(X - 10 * M, X + 35 * M,
                   'Alerta Cobra [COLOR tomato]T22 E9[/COLOR] [COLOR goldenrod]El asesino de reyes[/COLOR]',
                   desc='Episodio nueve', icon=[{'src': 'pic9'}]),
             _prog(X + 35 * M, X + 80 * M,
                   'Alerta Cobra [COLOR tomato]T22 E11[/COLOR] [COLOR goldenrod]Otro caso[/COLOR]',
                   desc='Episodio once')]
    out, stats = _merge(fresh, links)
    assert len(out) == 1
    p = out[0]
    assert _desc(p) == 'Episodio nueve' and p['icon'] == [{'src': 'pic9'}]
    assert (p['start'], p['stop'], p['catchup-id']) == (_ts(X), _ts(X + 45 * M), 'u9')
    assert stats['overlap_details'] == 1


def test_two_agreeing_candidates_give_titles_only():
    links = [_prog(X, X + H, 'Serie', link='u1', sub='T1 E3 "x"')]
    fresh = [_prog(X - 10 * M, X + 20 * M, 'Serie [COLOR tomato]T1 E3[/COLOR]', desc='a'),
             _prog(X + 20 * M, X + 70 * M, 'Serie [COLOR tomato]E3[/COLOR]', desc='b')]
    out, stats = _merge(fresh, links)
    assert [_desc(p) for p in _linked(out)] == [None]
    assert stats['overlap_details'] == 0 and stats['plain'] == 1


def test_agreeing_candidate_wins_over_an_untagged_one():
    links = [_prog(X, X + H, 'Serie', link='u1', sub='T1 E3 "x"')]
    fresh = [_prog(X - 10 * M, X + 20 * M, 'Serie', desc='untagged'),
             _prog(X + 20 * M, X + 70 * M, 'Serie [COLOR tomato]T1 E3[/COLOR]', desc='tagged')]
    out, _ = _merge(fresh, links)
    assert [_desc(p) for p in _linked(out)] == ['tagged']


# ================================================================ no tags to compare


def test_shifted_untagged_show_takes_details_with_the_exports_time_and_link():
    links = [_prog(X, X + H, 'Telediario', link='u1'),
             _prog(X + H, X + 2 * H, 'El tiempo', link='u2')]
    fresh = [_prog(X + 3 * M, X + H + 3 * M, 'Telediario', desc='Noticias', icon=[{'src': 'pic'}])]
    out, stats = _merge(fresh, links)
    first = [p for p in out if p['start'] == _ts(X)][0]
    assert _desc(first) == 'Noticias' and first['icon'] == [{'src': 'pic'}]
    assert (first['stop'], first['channel'], first['catchup-id']) == (_ts(X + H), CH, 'u1')
    assert stats['overlap_details'] == 1 and stats['with_content'] == 1 and stats['plain'] == 1


def test_matched_fresh_show_is_not_also_published_unlinked():
    links = [_prog(X, X + H, 'Telediario', link='u1')]
    fresh = [_prog(X + 3 * M, X + H + 3 * M, 'Telediario', desc='Noticias'),
             _prog(X + 2 * H, X + 3 * H, 'Cine', desc='Later')]
    out, stats = _merge(fresh, links)
    assert [(_desc(p), bool(p.get('catchup-id'))) for p in out] == [('Noticias', True), ('Later', False)]
    assert stats['fresh_dropped'] == 1 and stats['fresh_kept'] == 1


def test_season_only_match_needs_the_one_to_one_check():
    """'Leo Talks': the export has T6 E4 at 22:00-22:32 and T6 E5 at
    22:32-23:04; the guide has one 'T6' show (no episode) at 22:01-22:33.
    A season alone can't say which episode it is, and the guide's show
    touches both slots, so neither slot takes its description (the E5
    replay must not show E4's)."""
    links = [_prog(X, X + 32 * M, 'Leo Talks', link='u4', sub='T6 E4 "Cuatro"'),
             _prog(X + 32 * M, X + 64 * M, 'Leo Talks', link='u5', sub='T6 E5 "Cinco"')]
    fresh = [_prog(X + 1 * M, X + 33 * M, 'Leo Talks [COLOR tomato]T6[/COLOR]', desc='Episodio cuatro'),
             _prog(X + 33 * M, X + 90 * M, 'Otro programa', desc='otro')]
    out, stats = _merge(fresh, links)
    assert [_desc(p) for p in _linked(out)] == [None, None]
    assert stats['overlap_details'] == 0 and stats['plain'] == 2


def test_season_only_match_used_when_it_is_one_to_one():
    links = [_prog(X, X + 32 * M, 'Leo Talks', link='u4', sub='T6 E4 "Cuatro"'),
             _prog(X + 32 * M, X + 64 * M, 'Noticias', link='u5')]
    fresh = [_prog(X + 1 * M, X + 33 * M, 'Leo Talks [COLOR tomato]T6[/COLOR]', desc='Episodio cuatro')]
    out, stats = _merge(fresh, links)
    assert [_desc(p) for p in _linked(out)] == ['Episodio cuatro', None]
    assert stats['overlap_details'] == 1


def test_another_match_day_of_a_sports_season_is_not_used():
    """Match day 5 in the export, match day 9 in the guide, 5 minutes apart:
    the tags disagree, so titles only (unread tags would have let it in)."""
    links = [_prog(X, X + 2 * H, 'LaLiga', link='u1', sub='T2025/2026 "Episodio 5"')]
    fresh = [_prog(X + 5 * M, X + 2 * H + 5 * M, 'LaLiga [COLOR tomato]T2025/2026 E9[/COLOR]', desc='Jornada 9')]
    out, stats = _merge(fresh, links)
    assert [_desc(p) for p in _linked(out)] == [None]
    assert stats['overlap_details'] == 0


def test_tags_disagreeing_with_the_only_candidate_give_titles_only():
    links = [_prog(X, X + H, 'Serie', link='u1', sub='T1 E3 "x"')]
    fresh = [_prog(X + 5 * M, X + H + 5 * M, 'Serie [COLOR tomato]T1 E4[/COLOR]', desc='otro episodio')]
    out, stats = _merge(fresh, links)
    assert [_desc(p) for p in _linked(out)] == [None]
    assert stats['overlap_details'] == 0 and stats['plain'] == 1


def test_two_untagged_candidates_give_titles_only():
    links = [_prog(X, X + 2 * H, 'Cine', link='u1')]
    fresh = [_prog(X - 10 * M, X + 50 * M, 'Cine', desc='a'),
             _prog(X + 50 * M, X + 130 * M, 'Cine', desc='b')]
    out, stats = _merge(fresh, links)
    assert [_desc(p) for p in _linked(out)] == [None]
    assert stats['overlap_details'] == 0


def test_untagged_candidate_overlapping_another_same_title_slot_gives_titles_only():
    """Not one-to-one: the guide's show spans two export slots of that title."""
    links = [_prog(X, X + 30 * M, 'Noticias', link='u1'),
             _prog(X + 30 * M, X + H, 'Noticias', link='u2')]
    fresh = [_prog(X + 5 * M, X + 65 * M, 'Noticias', desc='Rich')]
    out, stats = _merge(fresh, links)
    assert [_desc(p) for p in _linked(out)] == [None, None]
    assert stats['overlap_details'] == 0 and stats['plain'] == 2


def test_another_slot_with_a_different_title_doesnt_block():
    links = [_prog(X, X + 30 * M, 'Noticias', link='u1'),
             _prog(X + 30 * M, X + H, 'Deportes', link='u2')]
    fresh = [_prog(X + 5 * M, X + 40 * M, 'Noticias', desc='Rich')]
    out, _ = _merge(fresh, links)
    assert [_desc(p) for p in _linked(out)] == ['Rich', None]


def test_different_title_gives_titles_only():
    links = [_prog(X, X + H, 'Noticias', link='u1')]
    fresh = [_prog(X + 5 * M, X + H + 5 * M, 'Deportes', desc='Rich')]
    out, stats = _merge(fresh, links)
    assert [_desc(p) for p in _linked(out)] == [None]
    assert stats['overlap_details'] == 0


def test_a_fresh_show_that_doesnt_overlap_is_never_used():
    links = [_prog(X, X + H, 'Noticias', link='u1')]
    fresh = [_prog(X + H, X + 2 * H, 'Noticias', desc='next one'),       # starts when the slot ends
             _prog(X - H, X, 'Noticias', desc='previous one')]           # ends when the slot starts
    out, stats = _merge(fresh, links)
    assert [_desc(p) for p in _linked(out)] == [None]
    assert stats['overlap_details'] == 0


def test_long_fresh_show_found_however_early_it_starts():
    """The lookup bisects on start time with the longest fresh length as the
    bound; a long show starting well before the slot still counts."""
    links = [_prog(X, X + 30 * M, 'Maratón', link='u1')]
    fresh = [_prog(X - 5 * H, X + 10 * M, 'Maratón', desc='Rich'),
             _prog(X - 4 * H, X - 3 * H, 'Otro', desc='x')]
    out, _ = _merge(fresh, links)
    assert [_desc(p) for p in _linked(out)] == ['Rich']


# ================================================================ order of the rules


def test_rule_1_beats_rule_3():
    links = [_prog(X, X + H, 'Telediario', link='u1')]
    fresh = [_prog(X, X + H, 'Otro título', desc='same start'),
             _prog(X - 5 * M, X + 55 * M, 'Telediario', desc='shifted')]
    out, stats = _merge(fresh, links)
    assert [_desc(p) for p in _linked(out)] == ['same start']
    assert stats['overlap_details'] == 0


def test_rule_2_beats_rule_3():
    previous = [_prog(X, X + H, 'Telediario', link='u1', desc='kept')]
    links = [_prog(X, X + H, 'Telediario', link='u1')]
    fresh = [_prog(X + 4 * M, X + H + 4 * M, 'Telediario', desc='shifted')]
    out, stats = _merge(fresh, links, previous)
    assert [_desc(p) for p in _linked(out)] == ['kept']
    assert stats['kept_details'] == 1 and stats['overlap_details'] == 0


def test_history_with_details_keeps_them_titles_only_history_takes_rule_3():
    previous = [_prog(X - 4 * H, X - 3 * H, 'Uno', link='h1', desc='kept uno'),
                _prog(X - 2 * H, X - H, 'Dos', link='h2')]
    fresh = [_prog(X - 4 * H + 5 * M, X - 3 * H + 5 * M, 'Uno', desc='fresh uno'),
             _prog(X - 2 * H + 5 * M, X - H + 5 * M, 'Dos', desc='fresh dos')]
    out, stats = _merge(fresh, [], previous)
    linked = _linked(out)
    assert [(p['catchup-id'], _desc(p)) for p in linked] == [('h1', 'kept uno'), ('h2', 'fresh dos')]
    assert (linked[1]['start'], linked[1]['stop']) == (_ts(X - 2 * H), _ts(X - H))
    assert stats['overlap_details'] == 1
    assert len(out) == 2, 'both fresh shows overlap a kept slot: not published unlinked'


def test_stats_count_rule_3_inside_with_content():
    links = [_prog(X, X + H, 'Uno', link='u1'),                  # rule 1
             _prog(X + H, X + 2 * H, 'Dos', link='u2'),          # rule 3
             _prog(X + 2 * H, X + 3 * H, 'Tres', link='u3')]     # titles only
    fresh = [_prog(X, X + H, 'Uno', desc='a'),
             _prog(X + H + 2 * M, X + 2 * H + 2 * M, 'Dos', desc='b')]
    _, stats = _merge(fresh, links)
    assert stats == {'links_in': 3, 'history_in': 0, 'history_kept': 0, 'linked': 3,
                     'with_content': 2, 'plain': 1, 'fresh_kept': 0, 'fresh_dropped': 2,
                     'channels': 1, 'kept_details': 0, 'overlap_details': 1}


def test_inputs_are_not_modified():
    links = [_prog(X, X + H, 'Telediario', link='u1')]
    fresh = [_prog(X + 3 * M, X + H + 3 * M, 'Telediario', desc='Noticias')]
    snapshot = repr((links, fresh))
    _merge(fresh, links)
    assert repr((links, fresh)) == snapshot


# ================================================================ _episode / _episodes_agree


def _t(title, sub=None):
    p = {'title': [(title, 'es')]}
    if sub is not None:
        p['sub-title'] = [(sub, 'es')]
    return p


@pytest.mark.parametrize('title,expected', [
    ('Alerta Cobra [COLOR tomato]T22 E9[/COLOR] [COLOR goldenrod]El asesino de reyes[/COLOR]', (22, 9)),
    ('La Chaparra [COLOR tomato]E1[/COLOR]', (None, 1)),
    ('Serie [COLOR tomato]T3 E101[/COLOR]', (3, 101)),
    ('Serie [COLOR tomato]T2[/COLOR]', (2, None)),
    ('Serie [COLOR goldenrod]Nombre[/COLOR] [COLOR tomato]T4 E5[/COLOR]', (4, 5)),   # first label WITH a number
    ('Serie [COLOR tomato][/COLOR] [COLOR tomato]E7[/COLOR]', (None, 7)),
])
def test_episode_from_title_label(title, expected):
    assert rh._episode(_t(title)) == expected


@pytest.mark.parametrize('sub,expected', [
    ('T22 E9 "x"', (22, 9)),
    ('T1 "Episodio 2"', (1, 2)),
    ('T8 "Episodio 110"', (8, 110)),
    ('E5 x', (None, 5)),
    ('"Episodio 4"', (None, 4)),
])
def test_episode_from_sub_title(sub, expected):
    assert rh._episode(_t('Serie', sub)) == expected


def test_title_label_comes_before_the_sub_title():
    assert rh._episode(_t('Serie [COLOR tomato]E2[/COLOR]', 'T1 E3 "x"')) == (None, 2)


@pytest.mark.parametrize('programme', [
    _t('Serie', 'Especial'),
    _t('Serie', '[COLOR SlateBlue]Series,Acción[/COLOR]'),          # davidmuma's genre label
    _t('Película [COLOR tomato](2019)[/COLOR]'),                     # a year label
    _t('Serie [COLOR goldenrod]Más allá del edén[/COLOR]'),          # an episode-name label
    _t('Serie', 'T1 Puro Cuatro'),                                   # season only, no episode
    _t('Serie', 'E5x'),
    _t('Serie'),
    {'title': 'plain text title'},
    {'title': [(None, '')], 'sub-title': [(None, '')]},
    {},
])
def test_episode_none(programme):
    assert rh._episode(programme) is None


@pytest.mark.parametrize('a,b,expected', [
    (None, None, None),
    (None, (1, 2), None),
    ((1, 2), None, None),
    ((1, None), (None, 2), None),        # no part present on both
    ((None, 2), (1, None), None),
    ((1, 2), (None, 2), True),
    ((None, 2), (1, 2), True),
    ((22, 9), (22, 9), True),
    ((1, None), (1, 5), None),           # the season alone doesn't tell episodes apart
    ((6, None), (6, None), None),
    ((5, None), (4, None), False),       # a different season is still a different show
    ((1, 2), (None, 1), False),
    ((22, 9), (22, 11), False),
    ((1, 2), (2, 2), False),
    ((1, 2), (2, None), False),
    ((2025, 5), (2025, 9), False),       # two match days of one sports season
])
def test_episodes_agree_truth_table(a, b, expected):
    assert rh._episodes_agree(a, b) is expected


@pytest.mark.parametrize('programme,expected', [
    (_t('LaLiga [COLOR tomato]T2025/2026 E9[/COLOR]'), (2025, 9)),
    (_t('LaLiga', 'T2025/2026 "Episodio 5"'), (2025, 5)),
    (_t('LaLiga', 'T2025/2026 E5 "x"'), (2025, 5)),
])
def test_sports_season_tags_are_read(programme, expected):
    """Sports seasons are written 'T2025/2026'; the first year is the season."""
    assert rh._episode(programme) == expected


def test_broken_title_with_long_space_runs_is_read_quickly():
    """The guide comes from the internet: a label with thousands of spaces
    must not stall the sync (the first patterns backtracked cubically:
    about 6 s at 2,000 spaces)."""
    spaces = ' ' * 2000
    started = time.monotonic()
    assert rh._episode(_t('Noticias [COLOR red]' + spaces + 'Edicion[/COLOR]')) is None
    assert rh._episode(_t('Noticias [COLOR red]T1' + spaces + 'x[/COLOR]')) is None
    assert rh._episode(_t('Noticias', spaces + 'x')) is None
    assert rh._episode(_t('Noticias', 'T1' + spaces + 'x')) is None
    assert rh._episode(_t('Noticias [COLOR red]' + spaces + 'E3' + spaces + '[/COLOR]')) == (None, 3)
    assert time.monotonic() - started < 0.5


@pytest.mark.parametrize('programme,expected', [
    ({'title': [('Hudson y Rex [COLOR orange](T4 E5)[/COLOR]', 'es')]}, 'hudson y rex'),
    ({'title': [('Alerta Cobra [COLOR tomato]T22 E9[/COLOR] [COLOR goldenrod]El asesino[/COLOR]', 'es')]},
     'alerta cobra'),
    ({'title': 'Pelóta vasca: final'}, 'pelota vasca final'),
    ({'title': [['[B]Lista[/B]', 'es']]}, 'lista'),
    ({'title': [('First', 'es'), ('Second', 'en')]}, 'first'),
    ({'title': []}, ''),
    ({'title': [(None, '')]}, ''),
    ({'title': 7}, ''),
    ({}, ''),
])
def test_title_reading_unchanged(programme, expected):
    """The title reader is now shared with the sub-title (_first_text);
    normalised titles are what they were."""
    assert rh._norm_title(programme) == expected


def test_interval_lookup():
    items = [(0, 100, 'long'), (50, 60, 'a'), (60, 70, 'b'), (200, 210, 'c')]
    index = rh._interval_index(items)
    assert [i[2] for i in rh._overlapping(index, 65, 66)] == ['long', 'b']
    assert [i[2] for i in rh._overlapping(index, 100, 200)] == []
    assert [i[2] for i in rh._overlapping(index, 99, 201)] == ['long', 'c']
    assert rh._overlapping(rh._interval_index([]), 0, 10) == []
