# -*- coding: utf-8 -*-
"""imports.70: the plain guide's episode tags (replay_helpers, pure).

Since 2026-11-07 davidmuma's guide is plain: no colour labels, no sub-titles.
The season/episode tag ends the show's part of the title, before an optional
' · <episode name>': 'Valle Salvaje T4 E103', 'Alerta Cobra T23 E12 · Sangre
y agua'. Rule 3 of merge_replay_guide (a show the guide has a few minutes
off) used to read tags only from colour labels, so with the plain guide it
chose without episode numbers, and the stale-export guard's "episodes don't
clash" test needs them too.

_plain_episode reads that tag as the LAST fallback of _episode, after the
colour label and the export's sub-title, so a missing tag can only become a
tag and no existing tag changes. A show name must come before the tag, and
it can't be a season tag itself ('T4 E103' alone reads nothing). Numbers have
at most 6 digits, and _number now caps every reader at 9 digits: int()
refuses more than 4,300 digits on Python 3.11+, which stopped the whole
merge.

Sections:
  - the 16 reads and 15 non-reads (real title shapes from the live guide);
  - the order of the readers;
  - huge numbers never raise (the old readers too);
  - speed on adversarial titles;
  - rule 3 with plain tags (merge level);
  - the shared title helpers are unchanged.
No Kodi stubs: replay_helpers is pure. All data is synthetic.
"""
import time

import pytest

import replay_helpers as rh
from replay_helpers import merge_replay_guide


CH  = 'C1@mp'
NOW = 1800000000          # 2027-01-15 08:00 UTC -- fixed so tests are deterministic
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


def _t(title, sub=None):
    p = {'title': [(title, 'es')]}
    if sub is not None:
        p['sub-title'] = [(sub, 'es')]
    return p


# ================================================================ reads and non-reads


READS = [
    ('Valle Salvaje T4 E103', (4, 103)),
    ('Alerta Cobra T23 E12 · Sangre y agua', (23, 12)),
    ('Horizonte Diarios E40', (None, 40)),
    ('El señor de las moscas (Extras) E2 · Dentro del rodaje', (None, 2)),
    ('D Corazón T4', (4, None)),
    ('Viaje al centro de la tele T10 · Maestro Íñigo', (10, None)),
    ('Top Bakalá T26/27 E10', (26, 10)),                          # a sports season: the first year
    ('UEFA Nations League T26/27 · Serbia - Alemania', (26, None)),
    ('La ruleta de la suerte T2026 E4822', (2026, 4822)),
    ('Gipsy Kings T0 E3 · El nacimiento del retoño', (0, 3)),
    ('Competiciones deportivas - T1, E56 T1 E56', (1, 56)),       # only the tag at the end
    ('Road to Monza - S01E01 T1 E1 · Episode 1', (1, 1)),
    ('Col·lapse T5 · Episodi 8', (5, None)),                      # an unspaced middle dot is part of the name
    ('DIRECTO Ronda 6: Finales T2026 · E1 Series: Miami', (2026, None)),   # the episode name is never read
    ('  Valle Salvaje  T4   E103  ', (4, 103)),                   # padded spaces
    ('Serie T1 E2 [COLOR SlateBlue]Series,Acción[/COLOR]', (1, 2)),         # a trailing genre label
]

NON_READS = [
    'E3',                                                  # no show name
    'T2026',
    'Mañaneros 360',
    'Noticias de Ucrania 24x7',
    'Fórmula E',
    'Serie T1E5',                                          # not the guide's spelling
    'serie t1 e5',
    'Lost Grounds T1 E5/5 · The Road to Wembley',
    'Cazadores sin fronteras T4 Ep2 El corte de patos',    # not at the end of the show part
    'Cuarto Milenio E36 especial',
    'Serie E1234567',                                      # more than 6 digits
    'Serie [COLOR goldenrod]Nombre T1 E5[/COLOR]',         # inside a label: removed with it
    'Serie · T1 E5',                                       # after the separator: the episode name
    'T4 E103',                                             # a season tag is no show name
    'T10 E224',
]


def test_the_lists_are_the_planned_ones():
    assert len(READS) == 16 and len(NON_READS) == 15
    assert len(set(t for t, _ in READS)) == 16 and len(set(NON_READS)) == 15


@pytest.mark.parametrize('title,expected', READS)
def test_plain_tag_is_read(title, expected):
    assert rh._plain_episode(_t(title)) == expected
    assert rh._episode(_t(title)) == expected


@pytest.mark.parametrize('title', NON_READS)
def test_no_plain_tag(title):
    assert rh._plain_episode(_t(title)) is None
    assert rh._episode(_t(title)) is None


def test_the_separator_is_the_spaced_middle_dot():
    assert rh._NAME_SEP == ' · '
    assert rh._episode(_t('Serie T1 E2 · Nombre')) == (1, 2)
    assert rh._episode(_t('Serie T1 E2·Nombre')) is None        # unspaced: part of the show part


def test_a_season_tag_before_the_episode_is_no_show_name():
    """'T4 E103' alone: _EPISODE_TAIL's non-space before the tag is the
    season's last digit. The same tag after a show name is read."""
    assert rh._EPISODE_TAIL.search('T4 E103') is not None             # the pattern alone would read it
    for title in ('T4 E103', 'T10 E224', 'T26/27 E10', '  T4 E103  ', '[COLOR a]x[/COLOR] T4 E103'):
        assert rh._plain_episode(_t(title)) is None, title
    assert rh._plain_episode(_t('Serie T4 E103')) == (4, 103)
    assert rh._plain_episode(_t('Serie T10 E224')) == (10, 224)
    assert rh._plain_episode(_t('X T4 E103')) == (4, 103)             # a name, however short


@pytest.mark.parametrize('programme', [
    {'title': 'Valle Salvaje T4 E103'},                              # a plain-text title
    {'title': [['Valle Salvaje T4 E103', 'es']]},
    {'title': [('Valle Salvaje T4 E103', 'es'), ('Otra T9 E9', 'en')]},   # the first title
])
def test_title_shapes(programme):
    assert rh._episode(programme) == (4, 103)


@pytest.mark.parametrize('programme', [{}, {'title': []}, {'title': [(None, '')]}, {'title': 7}])
def test_no_title(programme):
    assert rh._plain_episode(programme) is None


# ================================================================ order of the readers


def test_the_colour_label_comes_first():
    assert rh._episode(_t('Serie [COLOR tomato]E2[/COLOR] T1 E3')) == (None, 2)


def test_the_sub_title_comes_before_the_plain_tag():
    assert rh._episode(_t('Serie T1 E3', 'T1 E4 "x"')) == (1, 4)
    assert rh._episode(_t('Serie T1 E3', 'T1 "Episodio 7"')) == (1, 7)


def test_an_unread_sub_title_falls_through_to_the_plain_tag():
    """The export's 'T1 <show name>' placeholder carries no tag; the plain
    tag of the title is used."""
    assert rh._episode(_t('Serie T1 E3', 'T1 Serie')) == (1, 3)
    assert rh._episode(_t('Serie T1 E3', '')) == (1, 3)


# ================================================================ huge numbers


def test_number_cap():
    assert rh._number('123456789') == 123456789
    assert rh._number('1234567890') is None
    assert rh._number('0') == 0
    assert rh._number('') is None and rh._number(None) is None


def test_huge_numbers_never_raise():
    """int() refuses more than 4,300 digits on Python 3.11+: before imports.70 a
    label or sub-title like these raised ValueError and stopped the merge."""
    assert rh._episode(_t('Serie [COLOR tomato]E' + '7' * 5000 + '[/COLOR]')) == (None, None)
    assert rh._episode(_t('Serie', 'T1 E' + '2' * 5000)) == (1, None)
    assert rh._episode(_t('Serie T1 E' + '7' * 5000)) is None
    assert rh._episode(_t('Serie T' + '7' * 5000)) is None


def test_an_oversized_label_cant_be_compared():
    """A label made only of an oversized number reads (None, None), which
    _episodes_agree treats as 'can't compare'."""
    tag = rh._episode(_t('Serie [COLOR tomato]E' + '7' * 12 + '[/COLOR]'))
    assert tag == (None, None)
    assert rh._episodes_agree(tag, (1, 2)) is None


def test_a_huge_number_in_a_guide_title_doesnt_stop_the_merge():
    links = [_prog(X, X + H, 'Serie', link='u1', sub='T1 E' + '9' * 5000)]
    fresh = [_prog(X + 5 * M, X + H + 5 * M, 'Serie [COLOR tomato]E' + '9' * 5000 + '[/COLOR]', desc='d')]
    out, stats = _merge(fresh, links)
    assert stats['linked'] == 1


# ================================================================ speed


ADVERSARIAL = [
    'x' + ' ' * 19998 + 'y',                 # a long space run before a non-tag
    'x T1' + ' ' * 19994 + 'Ex',             # a long space run between the season and a broken episode
    'x ' * 10000,                            # a possible show name at every other character
    'x T' + '1' * 19997,                     # a long digit run after T
    ' T1' * 6666 + ' Ex',                    # many season tags, none at the end
    'x' + ' E1' * 6666 + '/',                # many episode tags, then a slash
]


def test_adversarial_titles_are_read_quickly():
    """The guide comes from the internet: the plain reader has one way to
    match, so it stays linear (measured about 1 ms each)."""
    assert all(len(t) >= 19999 for t in ADVERSARIAL)
    started = time.monotonic()
    for title in ADVERSARIAL:
        assert rh._episode(_t(title)) is None
        assert rh._plain_episode(_t(title)) is None
    assert time.monotonic() - started < 0.5


# ================================================================ rule 3 with plain tags


def test_celeste_the_agreeing_episode_wins_over_a_larger_overlap():
    """Export E6; the guide has E5 overlapping 30 minutes and E6 overlapping
    20. Without the plain reader both candidates were untagged: titles only."""
    links = [_prog(X, X + 50 * M, 'Celeste', link='u6', sub='T1 "Episodio 6"')]
    fresh = [_prog(X - 20 * M, X + 30 * M, 'Celeste T1 E5 · La carta', desc='Episodio cinco'),
             _prog(X + 30 * M, X + 80 * M, 'Celeste T1 E6 · El viaje', desc='Episodio seis')]
    out, stats = _merge(fresh, links)
    assert [_desc(p) for p in out] == ['Episodio seis']
    assert stats['overlap_details'] == 1 and stats['plain'] == 0


def test_la_peste_the_only_candidate_with_another_episode_gives_titles_only():
    """Before imports.70 the untagged-looking only candidate was used: the
    E1 replay showed E2's description."""
    links = [_prog(X, X + H, 'La Peste', link='u1', sub='T1 E1 "La palabra"')]
    fresh = [_prog(X + 5 * M, X + H + 5 * M, 'La Peste T1 E2 · El pacto', desc='Episodio dos')]
    out, stats = _merge(fresh, links)
    assert [_desc(p) for p in _linked(out)] == [None]
    assert stats['overlap_details'] == 0 and stats['plain'] == 1


def test_chicago_fire_another_season_gives_titles_only():
    links = [_prog(X, X + H, 'Chicago Fire', link='u1', sub='T10 E22 "x"')]
    fresh = [_prog(X + 4 * M, X + H + 4 * M, 'Chicago Fire T13 E1 · Vuelta', desc='otra temporada')]
    out, stats = _merge(fresh, links)
    assert [_desc(p) for p in _linked(out)] == [None]
    assert stats['overlap_details'] == 0


def test_season_only_plain_tag_needs_the_one_to_one_check():
    """'Viaje al centro de la tele T10 · Maestro' has a season only: it can't
    tell two episodes of that season apart. Touching two same-title slots:
    neither takes it."""
    links = [_prog(X, X + 30 * M, 'Viaje al centro de la tele', link='u1', sub='T10 E224 "x"'),
             _prog(X + 30 * M, X + H, 'Viaje al centro de la tele', link='u2', sub='T10 E225 "y"')]
    fresh = [_prog(X + 2 * M, X + 32 * M, 'Viaje al centro de la tele T10 · Maestro', desc='Maestro')]
    out, stats = _merge(fresh, links)
    assert [_desc(p) for p in _linked(out)] == [None, None]
    assert stats['overlap_details'] == 0


def test_season_only_plain_tag_used_when_one_to_one():
    links = [_prog(X, X + 30 * M, 'Viaje al centro de la tele', link='u1', sub='T10 E224 "x"'),
             _prog(X + 30 * M, X + H, 'Noticias', link='u2')]
    fresh = [_prog(X + 2 * M, X + 32 * M, 'Viaje al centro de la tele T10 · Maestro', desc='Maestro')]
    out, stats = _merge(fresh, links)
    assert [_desc(p) for p in _linked(out)] == ['Maestro', None]
    assert stats['overlap_details'] == 1


@pytest.mark.parametrize('sub', [None, 'T1 Bones'])
def test_an_untagged_slot_takes_the_only_candidate_as_before(sub):
    """No sub-title, or the export's 'T1 <show>' placeholder: the slot's tag
    can't be compared, so the only candidate is used (one-to-one)."""
    links = [_prog(X, X + H, 'Bones', link='u1', sub=sub)]
    fresh = [_prog(X + 3 * M, X + H + 3 * M, 'Bones T4 E23 · El hueso', desc='Episodio 23')]
    out, stats = _merge(fresh, links)
    assert [_desc(p) for p in _linked(out)] == ['Episodio 23']
    assert stats['overlap_details'] == 1


def test_the_slot_keeps_its_own_start_stop_channel_and_link():
    """La Chaparra with plain tags: E2's description, the export's slot."""
    links = [_prog(X, X + 47 * M, 'La Chaparra', link='u2', sub='T1 "Episodio 2"')]
    fresh = [_prog(X - 11 * M, X + 35 * M, 'La Chaparra E1', desc='Episodio uno'),
             _prog(X + 35 * M, X + 81 * M, 'La Chaparra E2', desc='Episodio dos', icon=[{'src': 'pic2'}])]
    out, stats = _merge(fresh, links)
    assert len(out) == 1, 'both guide shows overlap the slot: not published unlinked'
    p = out[0]
    assert _desc(p) == 'Episodio dos' and p['icon'] == [{'src': 'pic2'}]
    assert p['title'] == [('La Chaparra E2', 'es')]
    assert (p['start'], p['stop'], p['channel'], p['catchup-id']) == (_ts(X), _ts(X + 47 * M), CH, 'u2')
    assert stats['overlap_details'] == 1


# ================================================================ shared helpers unchanged


@pytest.mark.parametrize('programme,expected', [
    ({'title': 'Valle Salvaje T4 E103 · Nombre'}, 'valle salvaje t4 e103 nombre'),
    ({'title': 'Alerta Cobra [COLOR tomato]T22 E9[/COLOR]'}, 'alerta cobra'),
])
def test_norm_title_still_keeps_plain_tags(programme, expected):
    """The reader works on its own label-free copy: norm_title, strip_labels
    and title_text (shared with picture_helpers) are what they were."""
    assert rh._norm_title(programme) == expected == rh.norm_title(programme)


def test_shared_helpers_unchanged():
    assert rh.strip_labels('Serie T1 E2 · x') == 'Serie T1 E2 · x'
    assert rh.title_text({'title': 'Serie T1 E2'}) == 'Serie T1 E2'
    assert rh._same_title({'title': 'Valle Salvaje T4 E103 · Nombre'}, {'title': 'Valle Salvaje'})
    assert rh.norm_title is rh._norm_title and rh.title_text is rh._title_text
