# -*- coding: utf-8 -*-
"""imports.66: show pictures in the guide -- picture_helpers, the decisions.

TV3, À Punt and MTV showed a "BesTivi / Get it on Google Play" advert as the
picture of their shows. Their guide came from epg.best, which had no real
picture for most of their shows and answered those picture links with that
same advert: on 2026-11-08 it was the picture of 43 of TV3's 50 titles, 18 of
MTV's 22 and 41 of À Punt's 42. Kodi uses a show's own picture whenever it
has one and falls back to the channel logo only when the show has none
(CFileItem(CPVREpgInfoTag), xbmc/FileItem.cpp, Kodi 21), so the advert never
went away.

imports.66 gives every imported channel a Pictures setting (Auto / Show /
Hide, stored as record['show_pictures'] = 'on' / 'off', absent = Auto) and
every import an automatic check (on unless switched off): about once a day it
compares up to 8 of a channel's show pictures, and when most of them are one
identical image the channel's show pictures are left out of the guide, so
Kodi shows the logo. Show and Hide always beat the check.

picture_helpers holds every decision and is import-light (no Kodi, no I/O),
so it runs here directly, without stubs:
  - normalize_mode / record_mode: the dashboard's value and the stored one;
  - detect_enabled: the per-import switch (missing = on);
  - picture_src / pick_samples (_title_key): which pictures are compared;
  - judge: placeholder, real pictures, the previous result (one short of
    placeholder), or "can't tell";
  - settled: when a check can stop downloading;
  - make_verdict / valid_verdicts: the results file, written and read back;
  - check_due / due_order: when, and in which order, channels are checked;
  - pictures_hidden / strip_pictures: what reaches the guide.
The Kodi side (downloads, syncAll, server.py, manager.html) is tested apart
(test_imports66_show_pictures.py). The end of this file calibrates the rule
on real guide data: the advert / real-picture patterns are embedded here,
nothing is downloaded or read from disk.

Picture links below use the RFC 5737 documentation range.
"""
import ast
import calendar
import copy
import hashlib
import inspect
import itertools
import json
import os
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
LIB  = os.path.normpath(os.path.join(HERE, '..', 'resources', 'lib'))
if LIB not in sys.path:
    sys.path.insert(0, LIB)

import picture_helpers as ph   # noqa: E402
import replay_helpers          # noqa: E402

CH     = 'TV3Cat.es@autonomiques'      # a guide that answers most picture links with one filler image
CH2    = 'CNN.us@autonomiques'         # real show pictures
CH3    = 'MTV.es@autonomiques'
CUSTOM = '3330312e582d46696c65732e3c6d6435@PseudoTV_Live'     # an own (Custom) channel
H      = 3600
DAY    = 24 * H
NOW    = calendar.timegm((2026, 11, 8, 12, 0, 0, 0, 0, 0))    # 2026-11-08 12:00 UTC, fixed

_MISSING = object()     # "leave this key out" in parametrized entries
PREVIOUS = (None, True, False)      # no result yet, placeholder, real


def _src(name):
    with open(os.path.join(LIB, name), encoding='utf-8') as f:
        return f.read()


def _ts(epoch, offset_min=0):
    """XMLTV time for `epoch`, written with a +HHMM / -HHMM offset."""
    sign = '+' if offset_min >= 0 else '-'
    off = abs(offset_min)
    return (time.strftime('%Y%m%d%H%M%S', time.gmtime(epoch + offset_min * 60))
            + ' %s%02d%02d' % (sign, off // 60, off % 60))


def _link(name):
    return 'http://192.0.2.20/epg/%s.jpg' % name


def _show(title, start, link=None, ch=CH, offset_min=0, **extra):
    """One programme as the XMLTV reader gives it: (text, lang) titles and a
    list of {'src': ...} icons. No `link` = no picture."""
    programme = {'channel': ch, 'start': _ts(start, offset_min), 'stop': _ts(start + H, offset_min),
                 'title': [(title, 'es')]}
    if link is not None:
        programme['icon'] = [{'src': link}]
    programme.update(extra)
    return programme


def _with(base, **fields):
    """A copy of `base` with `fields` set (_MISSING removes the key)."""
    out = dict(base)
    for key, value in fields.items():
        if value is _MISSING:
            out.pop(key, None)
        else:
            out[key] = value
    return out


def _check(samples, fingerprint, previous=None):
    """One channel's check the way Imports._checkShowPictures runs it: the
    sampled pictures are read one at a time, reading stops as soon as
    settled() knows the result, and judge() decides on the pictures read.
    Returns (result, the fingerprints read)."""
    fps = []
    for url in samples:
        fps.append(fingerprint(url))
        if ph.settled(fps, len(samples) - len(fps), previous) is not None:
            break
    return ph.judge(fps, previous), fps


# ================================================================ module hygiene


def test_module_is_import_light_and_python38():
    """No Kodi modules and no file or network access: imports.py, server.py
    and these tests share one definition and need no Kodi stubs. The two
    add-on modules it uses are standard library only themselves
    (test_imports61 / test_imports63 check them)."""
    src  = _src('picture_helpers.py')
    tree = ast.parse(src, feature_version=(3, 8))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split('.')[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or '').split('.')[0])
    # xmltv_to_epoch comes through replay_helpers' re-export: the imports.63
    # one-clock rule (test_imports63_clock) lets only replay_helpers and
    # restart_helpers import names from clock_helpers. math is standard
    # library (finite-number checks on the verdicts file).
    assert 'replay_helpers' in imported and imported <= {'math', 'replay_helpers', 'unicodedata'}
    calls = {node.func.id for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert 'open' not in calls
    assert 'madteevee (imports fork, imports.66)' in src


def test_title_matching_is_replay_helpers_own():
    """One title rule for the whole add-on: picture_helpers reads the title
    with the replay merge's own reader and removes Kodi labels with its own
    pattern (public aliases, not private names, not copies), and for Latin
    titles its key is exactly norm_title."""
    assert ph.title_text is replay_helpers.title_text is replay_helpers._title_text
    assert ph.strip_labels is replay_helpers.strip_labels
    assert replay_helpers.strip_labels('[B]A[/B] [COLOR red]T1 E2[/COLOR]').split() == ['A']
    for title in ('Película', 'Alerta Cobra [COLOR tomato]T22 E9[/COLOR]', '[B]Telediario[/B] 1',
                  "Help! I'm in a Secret Relationship! T3 · Arielle y Frank", 'Notícies 3CatInfo',
                  'L\'Hora del Te', '¡Qué Tiempo Tan Feliz!'):
        programme = {'title': [(title, 'es')]}
        assert ph._title_key(programme) == replay_helpers.norm_title(programme), title
    tree = ast.parse(_src('picture_helpers.py'))
    private = [alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
               for alias in node.names if alias.name.startswith('_')]
    assert private == []


def test_constants_are_the_planned_ones():
    assert ph.MODES == ('auto', 'on', 'off')
    # imports.66 (rule revised 2026-11-10): up to 8 different shows are
    # sampled; placeholder when the most common picture covers 5 of the
    # pictures read (all of them when fewer were read), never with fewer
    # than 3 read. "All samples identical" missed TV3 and MTV, which mixed
    # the advert with a few real pictures. 2 equal pictures prove nothing:
    # Movistar+ channels reuse one picture for a few shows (up to 3 of 30
    # different shows on 2026-11-10).
    assert (ph.SAMPLES, ph.PLACEHOLDER_AT, ph.MIN_SAMPLES) == (8, 5, 3)
    assert ph.MIN_SAMPLES <= ph.PLACEHOLDER_AT <= ph.SAMPLES
    # A strict majority of a full sample: two different pictures can never
    # both be "the" placeholder.
    assert 2 * ph.PLACEHOLDER_AT > ph.SAMPLES
    assert ph.FP_BYTES == 64 * 1024
    assert ph.MAX_AGE_S == 24 * 3600
    assert ph.BUDGET_S == 15
    assert ph.VERDICTS_FILE == 'show_pictures.json'


# ================================================================ normalize_mode / record_mode


@pytest.mark.parametrize('value, mode', [
    ('auto', 'auto'), ('on', 'on'), ('off', 'off'),
    ('AUTO', 'auto'), ('On', 'on'), ('oFF', 'off'),
    (' on ', 'on'), ('\toff\n', 'off'), ('  Auto  ', 'auto'),
    (None, 'auto'), ('', 'auto'), ('   ', 'auto'),
])
def test_normalize_mode_accepts(value, mode):
    assert ph.normalize_mode(value) == mode


@pytest.mark.parametrize('value', [
    'yes', 'no', 'true', 'false', 'show', 'hide', 'none', 'o n', 'onn',
    1, 0, 1.0, True, False, [], {}, ['on'], ('on',), b'on',
], ids=repr)
def test_normalize_mode_rejects_with_valueerror(value):
    """imports.66: the dashboard boundary. server.py answers ValueError --
    and only ValueError -- with a 400, so a list or a number must not raise
    anything else (that would be a 500). 'show' / 'hide' are only the
    dashboard's labels; the stored values are 'on' / 'off'."""
    with pytest.raises(ValueError):
        ph.normalize_mode(value)


@pytest.mark.parametrize('record, mode', [
    (None, 'auto'), ({}, 'auto'),
    ({'show_pictures': None}, 'auto'), ({'show_pictures': ''}, 'auto'),
    ({'show_pictures': 'yes'}, 'auto'), ({'show_pictures': 'hide'}, 'auto'),
    ({'show_pictures': 1}, 'auto'), ({'show_pictures': True}, 'auto'), ({'show_pictures': []}, 'auto'),
    ({'show_pictures': 'auto'}, 'auto'), ({'show_pictures': 'on'}, 'on'), ({'show_pictures': 'off'}, 'off'),
    ({'show_pictures': ' OFF '}, 'off'),
], ids=repr)
def test_record_mode_reads_damaged_values_as_auto(record, mode):
    """A hand-edited channels.json must not fail a sync: anything that isn't
    a valid setting reads as Auto. Reading never writes the record."""
    before = copy.deepcopy(record)
    assert ph.record_mode(record) == mode
    assert record == before


# ================================================================ detect_enabled


@pytest.mark.parametrize('import_cfg', [None, {}, {'id': 'autonomiques'}],
                         ids=['no-import-entry', 'empty', 'never-set'])
def test_detect_enabled_is_on_when_never_set(import_cfg):
    """On for every import, including ones added later: a missing setting
    counts as on."""
    assert ph.detect_enabled(import_cfg) is True


@pytest.mark.parametrize('value, on', [
    (None, True), ('', True), ('   ', True),
    (True, True), (False, False),
    (1, True), (0, False), (2, True), (0.0, False), (0.5, True),
    ('false', False), ('False', False), ('OFF', False), ('off', False), ('no', False), ('NO', False),
    ('0', False), (' Off ', False), ('\tfalse\n', False),
    ('true', True), ('yes', True), ('on', True), ('1', True), ('anything else', True),
    ([], True), (['false'], True), ({}, True), ({'off': True}, True),
], ids=repr)
def test_detect_enabled_reads_the_switch(value, on):
    """A bool is itself, a number is on unless 0, text is off only for
    false / 0 / no / off (any case, spaces ignored); anything unexpected keeps
    the check on."""
    assert ph.detect_enabled({'detect_placeholder_pictures': value}) is on


# ================================================================ picture_src

L1, L2 = _link('one'), _link('two')


@pytest.mark.parametrize('programme, src', [
    pytest.param({'channel': CH}, '', id='no-icon'),
    pytest.param({'icon': []}, '', id='empty-list'),
    pytest.param({'icon': None}, '', id='icon-none'),
    pytest.param({'icon': {'src': L1}}, '', id='dict-not-list'),
    pytest.param({'icon': L1}, '', id='text-not-list'),
    pytest.param({'icon': [L1]}, '', id='first-item-text'),
    pytest.param({'icon': [None, {'src': L1}]}, '', id='first-item-none'),
    pytest.param({'icon': [{}]}, '', id='no-src'),
    pytest.param({'icon': [{'src': None}]}, '', id='src-none'),
    pytest.param({'icon': [{'src': 42}]}, '', id='src-number'),
    pytest.param({'icon': [{'src': ' \t\n'}]}, '', id='src-blank'),
    pytest.param({'icon': [{'src': '  %s\n' % L1}]}, L1, id='src-stripped'),
    pytest.param({'icon': [{'src': L1}, {'src': L2}]}, L1, id='first-icon-is-the-picture'),
    pytest.param({'rating': [{'value': '12', 'icon': [{'src': L1}]}]}, '', id='rating-icon-is-not-a-picture'),
    pytest.param(None, '', id='none'),
    pytest.param('text', '', id='text'),
    pytest.param([{'icon': [{'src': L1}]}], '', id='list'),
])
def test_picture_src(programme, src):
    assert ph.picture_src(programme) == src


# ================================================================ pick_samples

# The earliest show of the guide: it would be the first sample if it were usable.
SPOILER = _show('Spoiler', NOW - H, _link('spoiler'))


@pytest.mark.parametrize('spoiler', [
    pytest.param(_with(SPOILER, icon=_MISSING), id='no-picture'),
    pytest.param(_with(SPOILER, icon=[]), id='empty-icon-list'),
    pytest.param(_with(SPOILER, icon=[{'src': '  '}]), id='blank-link'),
    pytest.param(_with(SPOILER, title=_MISSING), id='no-title'),
    pytest.param(_with(SPOILER, title=[('', 'es')]), id='empty-title'),
    pytest.param(_with(SPOILER, title=[('[COLOR tomato]T1 E2[/COLOR]', 'es')]), id='title-only-a-colour-label'),
    pytest.param(_with(SPOILER, title=[('¡¿...?!', 'es')]), id='title-only-punctuation'),
    pytest.param(_with(SPOILER, title=[('«» — …', 'es')]), id='title-only-non-ascii-punctuation'),
    pytest.param(_with(SPOILER, start=_MISSING), id='no-start'),
    pytest.param(_with(SPOILER, start='tomorrow'), id='garbage-start'),
    pytest.param(_with(SPOILER, start='20261332250000 +0000'), id='impossible-date'),
    pytest.param(_with(SPOILER, start='20261108110000 0100'), id='bad-offset'),
    pytest.param(None, id='none'),
    pytest.param('<programme/>', id='text'),
])
def test_pick_samples_skips_shows_it_cannot_use(spoiler):
    guide = [spoiler, _show('A', NOW, _link('a')), _show('B', NOW + H, _link('b')), _show('C', NOW + 2 * H, _link('c'))]
    assert ph.pick_samples(guide, CH, n=10) == [_link('a'), _link('b'), _link('c')]


def test_pick_samples_reads_only_the_channels_own_shows():
    guide = [
        _show('Noticias', NOW - 3 * H, _link('cnn-news'), ch=CH2),     # same title, earlier, other channel
        _show('Noticias', NOW - 2 * H, _link('tv3-news')),
        _show('Deportes', NOW - H, _link('cnn-sport'), ch=CH2),
        _show('Cine', NOW, _link('tv3-film')),
        _show('Cine', NOW + H, _link('own-film'), ch=CUSTOM),
    ]
    assert ph.pick_samples(guide, CH) == [_link('tv3-news'), _link('tv3-film')]
    assert ph.pick_samples(guide, CH2) == [_link('cnn-news'), _link('cnn-sport')]
    assert ph.pick_samples(guide, CH3) == []


def test_pick_samples_one_sample_per_title_from_its_earliest_airing():
    """Titles are compared through replay_helpers.norm_title: Kodi colour
    labels (the season / episode tags davidmuma-style guides add) go together
    with their text; [B] / [I] tags, case, accents and punctuation are
    ignored. Of a title's airings, the earliest one with a picture is used."""
    guide = [
        _show('Alerta Cobra [COLOR tomato]T22 E9[/COLOR]', NOW + 2 * H, _link('cobra-e9')),
        _show('ALERTA COBRA [COLOR tomato]T22 E8[/COLOR]', NOW, _link('cobra-e8')),
        _show('[B]Alerta Cobra[/B]', NOW + 5 * H, _link('cobra-e10')),
        _show('Película', NOW + 3 * H, _link('film-late')),
        _show('PELICULA!', NOW + H, _link('film-early')),
        _show('Noticias', NOW + 4 * H, _link('news')),
    ]
    assert ph.pick_samples(guide, CH, n=10) == [_link('cobra-e8'), _link('film-early'), _link('news')]


def test_pick_samples_a_later_airing_stands_in_for_one_without_a_picture():
    guide = [_show('Telenotícies', NOW), _show('TELENOTICIES', NOW + 3 * H, _link('tn-evening'))]
    assert ph.pick_samples(guide, CH) == [_link('tn-evening')]


def test_pick_samples_a_series_marathon_fills_one_sample_only():
    """Why one sample per title: eight airings of one series share its series
    picture. Sampled per airing they would be most of the samples -- a
    placeholder by the rule; sampled per title they are one picture of four."""
    marathon = [_show('Friends', NOW + i * H, _link('friends-series')) for i in range(8)]
    rest = [_show('Noticias', NOW + 8 * H, _link('news')), _show('Cine', NOW + 9 * H, _link('film')),
            _show('Deportes', NOW + 10 * H, _link('sport'))]
    per_airing = [ph.picture_src(p) for p in marathon + rest]
    assert ph.judge(per_airing) is True      # the links stand in for fingerprints here
    samples = ph.pick_samples(marathon + rest, CH)
    assert samples == [_link('friends-series'), _link('news'), _link('film'), _link('sport')]
    assert ph.judge(samples) is False


@pytest.mark.parametrize('titles, n, picked', [
    (20, 8, [0, 2, 5, 8, 10, 13, 16, 19]),
    (12, 8, [0, 1, 3, 4, 6, 7, 9, 11]),
    (9, 8, [0, 1, 2, 3, 4, 5, 6, 8]),
    (9, 4, [0, 2, 5, 8]),
    (9, 3, [0, 4, 8]),
    (9, 2, [0, 8]),
    (9, 1, [0]),
    (10, 4, [0, 3, 6, 9]),
    (5, 4, [0, 1, 2, 4]),
])
def test_pick_samples_spreads_from_the_first_show_to_the_last(titles, n, picked):
    """Spread over the whole guide (the first title is always in, and the
    last one too when n > 1), not the next n shows. The guide is given in
    reverse: the order of the input doesn't matter."""
    guide = [_show('Show %d' % i, NOW + i * H, _link('s%d' % i)) for i in range(titles)]
    assert ph.pick_samples(guide[::-1], CH, n=n) == [_link('s%d' % i) for i in picked]


def test_pick_samples_takes_samples_pictures_by_default():
    guide = [_show('Show %d' % i, NOW + i * H, _link('s%d' % i)) for i in range(20)]
    assert ph.pick_samples(guide, CH) == ph.pick_samples(guide, CH, n=ph.SAMPLES)
    assert len(ph.pick_samples(guide, CH)) == ph.SAMPLES == 8


@pytest.mark.parametrize('n', [3, 4, 8, 50])
def test_pick_samples_takes_every_title_in_start_order_when_there_are_n_or_fewer(n):
    guide = [_show('C', NOW + 2 * H, _link('c')), _show('A', NOW, _link('a')), _show('B', NOW + H, _link('b'))]
    assert ph.pick_samples(guide, CH, n=n) == [_link('a'), _link('b'), _link('c')]


@pytest.mark.parametrize('n', [0, -1])
def test_pick_samples_none_asked_gives_none(n):
    assert ph.pick_samples([_show('A', NOW, _link('a'))], CH, n=n) == []


@pytest.mark.parametrize('programmes', [None, [], ()], ids=['none', 'list', 'tuple'])
def test_pick_samples_empty_guide(programmes):
    assert ph.pick_samples(programmes, CH) == []


def test_pick_samples_keeps_a_link_shared_by_several_shows():
    """Links are not de-duplicated: when every show points at one link, every
    show has the same picture -- exactly what a placeholder looks like."""
    filler = _link('no-picture')
    titles = ('Telenotícies', 'Els matins', 'Cuines', 'Polònia', 'APM?',
              'Tot es mou', 'Està passant', 'Divendres', 'El foraster', 'Mésnit')
    guide = [_show(title, NOW + i * H, filler) for i, title in enumerate(titles)]
    samples = ph.pick_samples(guide, CH)
    assert samples == [filler] * ph.SAMPLES
    assert ph.judge(samples) is True


@pytest.mark.parametrize('shape', [
    pytest.param(lambda text: [(text, 'ca')], id='xmltv-text-lang'),
    pytest.param(lambda text: text, id='plain-text'),
    pytest.param(lambda text: [[text, 'ca']], id='json-lists'),
])
def test_pick_samples_reads_every_title_shape(shape):
    """The XMLTV reader gives [(text, lang)]; plain text and JSON-style
    [[text, lang]] read the same -- in any script."""
    titles = ('Polònia', 'Cuines', 'POLONIA', 'Новости', 'НОВОСТИ!')
    guide = [_with(_show(title, NOW + i * H, _link('t%d' % i)), title=shape(title))
             for i, title in enumerate(titles)]
    assert ph.pick_samples(guide, CH) == [_link('t0'), _link('t1'), _link('t3')]


def test_pick_samples_orders_by_the_real_instant_across_utc_offsets():
    """Guides mix offsets: the order and a title's earliest airing follow the
    real instant, not the text (by text the order here would be C, B, A)."""
    a = _show('A', NOW, _link('a'), offset_min=60)              # 13:00 +0100 = 12:00 UTC
    b = _show('B', NOW + 30 * 60, _link('b'))                   # 12:30 +0000
    c = _show('C', NOW + H, _link('c'), offset_min=-360)        # 07:00 -0600 = 13:00 UTC
    assert sorted(p['start'] for p in (a, b, c)) == [c['start'], b['start'], a['start']]
    assert ph.pick_samples([c, b, a], CH, n=10) == [_link('a'), _link('b'), _link('c')]
    assert ph.pick_samples([c, b, a], CH, n=1) == [_link('a')]
    early = _show('Noticias', NOW, _link('news-early'), offset_min=120)     # 14:00 +0200 = 12:00 UTC
    late = _show('Noticias', NOW + 30 * 60, _link('news-late'))             # 12:30 +0000
    assert late['start'] < early['start']
    assert ph.pick_samples([late, early], CH) == [_link('news-early')]


def test_pick_samples_does_not_depend_on_the_guide_order():
    """The same samples whatever the order of the guide -- also when two
    airings of one title share a start (a duplicated entry) or two titles
    start together."""
    guide = [_show('Show %d' % i, NOW + i * H, _link('s%d' % i)) for i in range(11)]
    guide.append(_show('Show 3', NOW + 3 * H, _link('s3-duplicate')))
    guide.append(_show('Other', NOW + 3 * H, _link('other')))
    expected = ph.pick_samples(guide, CH)
    assert len(expected) == len(set(expected)) == ph.SAMPLES      # 12 titles: a spread of 8
    assert not {_link('s3'), _link('s3-duplicate')} <= set(expected)     # never two airings of one title
    orders = [guide[i:] + guide[:i] for i in range(len(guide))]
    orders += [order[::-1] for order in orders]
    for order in orders:
        assert ph.pick_samples(order, CH) == expected


def test_pick_samples_leaves_the_guide_alone():
    guide = [_show('A', NOW, _link('a')), _show('B', NOW + H, _link('b'), ch=CH2), None]
    snapshot = copy.deepcopy(guide)
    ph.pick_samples(guide, CH)
    assert guide == snapshot


# ================================================================ _title_key: titles in any script
# imports.66: norm_title keeps ASCII only (accents are folded away first), so
# a title written in another script -- Chinese, Cyrillic, Arabic... -- comes
# out empty, and such shows used to be skipped: a channel whose guide is in
# one of those scripts gave no samples and was never checked. _title_key
# keys those titles on their own letters and digits instead.


@pytest.mark.parametrize('title, key', [
    pytest.param('Película', 'pelicula', id='latin-is-norm-title'),
    pytest.param('Alerta Cobra [COLOR tomato]T22 E9[/COLOR]', 'alerta cobra', id='latin-colour-label'),
    pytest.param('新闻联播', '新闻联播', id='chinese'),
    pytest.param('新闻  联播', '新闻 联播', id='chinese-spaces-collapsed'),
    pytest.param('Новости', 'новости', id='cyrillic'),
    pytest.param('НОВОСТИ!', 'новости', id='cyrillic-case-and-punctuation-ignored'),
    pytest.param('Время', 'время', id='cyrillic-other-title'),
    pytest.param('نشرة الأخبار', 'نشرة الاخبار', id='arabic-marks-dropped'),
    pytest.param('الاتجاه المعاكس', 'الاتجاه المعاكس', id='arabic-other-title'),
    pytest.param('[B]Новости[/B]', 'новости', id='cyrillic-bold-label'),
    pytest.param('Новости [COLOR tomato]T1 E2[/COLOR]', 'новости', id='cyrillic-episode-label'),
    pytest.param('新闻 [COLOR yellow]T1 E3[/COLOR]', '新闻', id='chinese-episode-label'),
    pytest.param('ειδήσεις', 'ειδησεισ', id='greek-accent-dropped'),
    pytest.param('ΕΙΔΗΣΕΙΣ', 'ειδησεισ', id='greek-capitals'),
    pytest.param('Новости 24', 'новости 24', id='mixed-script-keeps-its-letters'),
    pytest.param('Спорт 24', 'спорт 24', id='mixed-script-other-title'),
    pytest.param('١٢٣', '١٢٣', id='arabic-indic-digits'),
    pytest.param('[COLOR tomato]T1 E2[/COLOR]', '', id='colour-label-only'),
    pytest.param('¡¿...?!', '', id='punctuation-only'),
    pytest.param('«» — …', '', id='non-ascii-punctuation-only'),
    pytest.param('', '', id='empty'),
    pytest.param('   ', '', id='blank'),
])
def test_title_key(title, key):
    assert ph._title_key({'title': [(title, 'xx')]}) == key


@pytest.mark.parametrize('title', [
    pytest.param([('Новости', 'ru')], id='xmltv-text-lang'),
    pytest.param('Новости', id='plain-text'),
    pytest.param([['Новости', 'ru']], id='json-lists'),
    pytest.param(['Новости'], id='list-of-text'),
])
def test_title_key_reads_every_title_shape_in_any_script(title):
    assert ph._title_key({'title': title}) == 'новости'


@pytest.mark.parametrize('title', [
    _MISSING, None, 42, [], [()], [[]], [(None, 'ru')], [42], {'text': 'Новости'},
], ids=['missing', 'none', 'number', 'empty-list', 'empty-pair', 'empty-inner-list',
      'none-text', 'number-in-list', 'dict'])
def test_title_key_without_a_usable_title_is_empty(title):
    programme = {} if title is _MISSING else {'title': title}
    assert ph._title_key(programme) == ''


def test_pick_samples_samples_titles_in_any_script_and_keeps_them_apart():
    """Chinese, Cyrillic and Arabic titles are sampled, each title once (its
    earliest airing), and different titles in one script stay different
    titles. A title that is only a colour label or only punctuation is still
    unusable."""
    guide = [
        _show('新闻联播', NOW, _link('cn-news')),
        _show('Новости', NOW + H, _link('ru-news')),
        _show('نشرة الأخبار', NOW + 2 * H, _link('ar-news')),
        _show('朝闻天下', NOW + 3 * H, _link('cn-morning')),
        _show('Время', NOW + 4 * H, _link('ru-time')),
        _show('الاتجاه المعاكس', NOW + 5 * H, _link('ar-debate')),
        _show('新闻联播', NOW + 6 * H, _link('cn-news-again')),         # same title, later airing
        _show('НОВОСТИ', NOW + 7 * H, _link('ru-news-again')),         # same title, later airing
        _show('[COLOR tomato]T1 E2[/COLOR]', NOW - H, _link('label')),
        _show('«» — …', NOW - 2 * H, _link('punctuation')),
    ]
    assert [replay_helpers.norm_title(p) for p in guide[:6]] == [''] * 6    # what used to drop them
    assert ph.pick_samples(guide, CH) == [_link('cn-news'), _link('ru-news'), _link('ar-news'),
                                          _link('cn-morning'), _link('ru-time'), _link('ar-debate')]


def test_a_guide_mixing_latin_and_other_scripts_is_spread_over_all_of_it():
    """Twelve titles alternating between Latin and other scripts: the 8
    samples are spread over all twelve, and no Latin title is confused with
    one in another script (Noticias / Новости are two shows)."""
    titles = ('Noticias', 'Новости', 'Deportes', '体育新闻', 'Cine', 'السينما',
              'El tiempo', 'Погода', 'Documental', '纪录片', 'Música', 'موسيقى')
    guide = [_show(title, NOW + i * H, _link('m%d' % i)) for i, title in enumerate(titles)]
    keys = [ph._title_key(p) for p in guide]
    assert all(keys) and len(set(keys)) == len(titles)
    assert ph.pick_samples(guide, CH) == [_link('m%d' % i) for i in (0, 1, 3, 4, 6, 7, 9, 11)]


def test_a_placeholder_channel_titled_in_another_script_is_now_found():
    """A channel whose whole guide is in Chinese and whose every picture link
    answers with one filler image: 8 samples, placeholder. Before the
    non-Latin keys it gave no samples at all."""
    titles = ('新闻联播', '朝闻天下', '今日说法', '焦点访谈', '天气预报', '动物世界',
              '人与自然', '经济半小时', '国际时讯', '体育新闻')
    filler = _link('cn-filler')
    guide = [_show(title, NOW + i * H, filler) for i, title in enumerate(titles)]
    samples = ph.pick_samples(guide, CH)
    assert samples == [filler] * ph.SAMPLES
    assert ph.judge(samples) is True


# ================================================================ judge
# imports.66: the rule as a table. For each count of pictures read (failed
# reads don't count), the result when the largest group of identical
# pictures holds 1, 2, 3 ... of them:
#   P = placeholder, R = real pictures,
#   B = buffer: one short of placeholder, the previous result stands (so a
#       mixed channel doesn't flip from one day's samples to the next),
#   ? = can't tell: fewer than MIN_SAMPLES read; nothing is recorded.
REGIONS = {
    0: '?',             # nothing read
    1: '?',
    2: '??',
    3: 'RBP',           # fewer than PLACEHOLDER_AT read: all of them must match
    4: 'RRBP',
    5: 'RRRBP',
    6: 'RRRBPP',
    7: 'RRRBPPP',
    8: 'RRRBPPPP',      # SAMPLES: 5+ of 8 = placeholder, 4 = buffer, 3 or fewer = real
}
# Each region's result after no result yet / placeholder / real (PREVIOUS).
WANT = {'P': (True, True, True), 'R': (False, False, False), 'B': (False, True, False), '?': (None, None, None)}
_CELLS = [(read, top) for read in sorted(REGIONS) for top in (range(1, read + 1) if read else (0,))]


def _cell(read, top):
    return REGIONS[read][max(top, 1) - 1]


def _layouts(read, top):
    """Fingerprints with `read` good reads whose largest identical group
    holds `top` (the others all different): the group first, the group
    last, and the two interleaved among failed reads (None and '')."""
    same = ['same'] * top
    others = ['other%d' % i for i in range(read - top)]
    mixed = [''] + [fp for pair in itertools.zip_longest(others, same) for fp in pair] + [None]
    return [same + others, others + same, mixed]


def test_the_region_table_matches_the_constants():
    assert max(REGIONS) == ph.SAMPLES
    assert REGIONS[ph.SAMPLES].index('P') + 1 == ph.PLACEHOLDER_AT
    assert all(REGIONS[read] == '?' * max(read, 1) for read in range(ph.MIN_SAMPLES))
    assert all(REGIONS[read].count('B') == 1 for read in range(ph.MIN_SAMPLES, ph.SAMPLES + 1))


@pytest.mark.parametrize('read, top', _CELLS, ids=['read%d-top%d' % cell for cell in _CELLS])
def test_judge_region_table(read, top):
    """Every count read 0..8 and every largest-group size, with no previous
    result, after placeholder and after real. The buffer keeps placeholder
    only when the previous result was placeholder."""
    want = WANT[_cell(read, top)]
    for fps in _layouts(read, top):
        for previous, result in zip(PREVIOUS, want):
            assert ph.judge(fps, previous) is result, (fps, previous)


@pytest.mark.parametrize('pattern, want', [
    # mixed channels, 8 pictures read (2026-11-08: the advert was the
    # picture of 43 of TV3's 50 titles and 18 of MTV's 22)
    pytest.param('aaaaaabc', (True, True, True), id='6-of-8-identical'),
    pytest.param('aaaaabcd', (True, True, True), id='5-of-8-identical'),
    pytest.param('aaaabcde', (False, True, False), id='4-of-8-identical-previous-stands'),
    pytest.param('aaabcdef', (False, False, False), id='3-of-8-identical'),
    # two groups: only the largest one counts
    pytest.param('aaaaabbb', (True, True, True), id='5-and-3-of-8'),
    pytest.param('aaaabbbb', (False, True, False), id='4-and-4-of-8-previous-stands'),
    pytest.param('aaabbbcc', (False, False, False), id='3-3-2-of-8'),
    # small channels: fewer than PLACEHOLDER_AT read, so all must match
    pytest.param('aaa', (True, True, True), id='3-of-3'),
    pytest.param('aab', (False, True, False), id='2-of-3-previous-stands'),
    pytest.param('abc', (False, False, False), id='1-of-3'),
    pytest.param('aaaa', (True, True, True), id='4-of-4'),
    pytest.param('aaab', (False, True, False), id='3-of-4-previous-stands'),
    pytest.param('aabb', (False, False, False), id='2-and-2-of-4'),
])
def test_judge_mixed_and_small_channels(pattern, want):
    """`want`: the result with no previous result yet (which counts as
    real), after placeholder, after real. The order of the pictures doesn't
    matter."""
    for fps in (list(pattern), list(pattern[::-1]), list(pattern[1::2] + pattern[::2])):
        for previous, result in zip(PREVIOUS, want):
            assert ph.judge(fps, previous) is result, (fps, previous)


@pytest.mark.parametrize('fingerprints, result', [
    pytest.param(['x', 'x', 'x'] + [None] * 5, True, id='3-read-all-identical'),
    pytest.param(['x', None, 'x', '', 'x', None, None, None], True, id='failed-reads-between'),
    pytest.param(['x', 'y', 'z'] + [None] * 5, False, id='3-read-all-different'),
    pytest.param(['x', 'x', None, 'y', 'x', 'z', 'x', 'x'], True, id='5-of-7-read'),
    pytest.param(['x', 'x'] + [None] * 6, None, id='2-read'),
    pytest.param(['x', 'y'], None, id='two-different-are-too-few-now'),
    pytest.param(['x'], None, id='one'),
    pytest.param([], None, id='nothing-read'),
    pytest.param([None] * 8, None, id='every-read-failed'),
    pytest.param([''] * 8, None, id='empty-fingerprints'),
])
def test_judge_ignores_failed_reads(fingerprints, result):
    """Failed reads (None / '') are left out before anything is counted:
    True = placeholder, False = real pictures, None = can't tell (nothing
    is recorded and the channel is retried at the next sync)."""
    for previous in PREVIOUS:
        assert ph.judge(fingerprints, previous) is result


def test_judge_default_minimum_is_min_samples():
    assert ph.judge(['x'] * ph.MIN_SAMPLES) is True
    assert ph.judge(['x'] * (ph.MIN_SAMPLES - 1)) is None
    assert ph.judge(['x', 'y', 'z']) is False
    assert ph.judge(['x', 'y']) is None


@pytest.mark.parametrize('previous', [1, 'true', 'yes', [True], {'placeholder': True}], ids=repr)
def test_judge_buffer_keeps_placeholder_only_after_a_real_true(previous):
    """valid_verdicts only ever hands over a bool; anything else that looks
    true still counts as "no previous result" in the buffer."""
    assert ph.judge(list('aaaabcde'), previous) is False      # buffer
    assert ph.judge(list('aaaaabcd'), previous) is True       # placeholder whatever came before
    assert ph.judge(list('aaabcdef'), previous) is False      # real whatever came before
    assert ph.judge(list('aa'), previous) is None


def test_judge_and_settled_take_the_previous_result():
    """imports.66: imports.py calls judge(fps, previous) and settled(fps,
    remaining, previous); the old min_samples argument is gone (the minimum
    is MIN_SAMPLES), and no previous result is the default."""
    assert list(inspect.signature(ph.judge).parameters) == ['fingerprints', 'previous']
    assert list(inspect.signature(ph.settled).parameters) == ['fingerprints', 'remaining', 'previous']
    assert inspect.signature(ph.judge).parameters['previous'].default is None
    assert inspect.signature(ph.settled).parameters['previous'].default is None
    assert ph.judge(list('aaab')) is ph.judge(list('aaab'), None) is False


def test_judge_leaves_the_fingerprints_alone():
    fps = ['x', None, 'x', 'y', '', 'x']
    snapshot = list(fps)
    ph.judge(fps, True)
    ph.settled(fps, 2, True)
    assert fps == snapshot


# ================================================================ settled
# imports.66: a check reads its pictures one at a time and stops as soon as
# settled() says the ones not read yet can't change judge()'s result (they
# could be the most common picture again, new pictures, or failed reads).


def _sequences(alphabet, longest):
    """Every sequence over `alphabet` up to `longest` long; 'new' stands for
    a picture not seen before (a different fingerprint at each position)."""
    for length in range(longest + 1):
        for seq in itertools.product(alphabet, repeat=length):
            yield [('new%d' % i if fp == 'new' else fp) for i, fp in enumerate(seq)]


@pytest.mark.parametrize('alphabet, longest', [
    pytest.param(('a', 'b', 'c', None), 6, id='three-pictures-and-failed-reads-up-to-6'),
    pytest.param(('a', 'new', None), ph.SAMPLES, id='one-picture-new-ones-and-failed-reads-up-to-8'),
])
def test_settled_never_disagrees_with_judge(alphabet, longest):
    """Exhaustive: for every sequence of reads, every point a check could
    stop at, and every previous result -- whenever settled() gives a result,
    judge() gives that same result however the unread pictures turn out,
    and judge() of just the pictures read (what imports.py records after
    stopping early) gives it too."""
    decided = set()
    for fps in _sequences(alphabet, longest):
        for previous in PREVIOUS:
            final = ph.judge(fps, previous)
            for read in range(len(fps) + 1):
                early = ph.settled(fps[:read], len(fps) - read, previous)
                if early is not None:
                    assert early is final, (fps, read, previous)
                    assert ph.judge(fps[:read], previous) is early, (fps, read, previous)
                    decided.add(early)
    assert decided == {True, False}       # not vacuous: it stops early, both ways


def _reads(fps, previous=None):
    """settled()'s answer after each picture a check reads, until it stops
    (all of them when it never does), as text: - = keep reading, P / R =
    stop, placeholder / real."""
    steps = ''
    for read in range(1, len(fps) + 1):
        early = ph.settled(fps[:read], len(fps) - read, previous)
        steps += {None: '-', True: 'P', False: 'R'}[early]
        if early is not None:
            break
    return steps


@pytest.mark.parametrize('pattern, steps', [
    pytest.param('abcdefgh', '-----R', id='real-pictures-cost-6'),
    pytest.param('aaaaaaaa', '----P', id='a-placeholder-costs-5'),
    pytest.param('aaaaabcd', '----P', id='5-identical-first'),
    pytest.param('aaaaaaab', '----P', id='the-real-one-is-never-read'),
    pytest.param('abaaaaaa', '-----P', id='one-real-picture-read'),
    pytest.param('abcdefaa', '-----R', id='a-pair-left-unread'),
    pytest.param('aabcdefg', '------R', id='a-pair-first'),
    pytest.param('abc.....', '-----R', id='failed-reads-at-the-end'),
    pytest.param('a.a.aaaa', '------P', id='failed-reads-between'),
    pytest.param('aaabbbcc', '-------R', id='two-groups-of-3-read-to-the-end'),
    pytest.param('abababab', '--------', id='buffer-reads-everything'),
    pytest.param('........', '--------', id='every-read-failed'),
])
def test_settled_how_many_pictures_a_check_downloads(pattern, steps):
    """With SAMPLES = 8: a channel with real pictures costs 6 downloads, an
    all-placeholder one 5; a result in the buffer is never settled early.
    Stopping changes nothing: the pictures read give the full sample's
    result. ('.' = a failed read.)"""
    fps = [None if c == '.' else c for c in pattern]
    assert len(fps) == ph.SAMPLES
    for previous in PREVIOUS:
        assert _reads(fps, previous) == steps
        assert ph.judge(fps[:len(steps)], previous) is ph.judge(fps, previous)


@pytest.mark.parametrize('fingerprints', [
    [], ['a'], ['a', 'a'], ['a', 'b'], ['a', None, 'a'], ['', 'a', None, 'a'], [None] * 8,
], ids=repr)
def test_settled_never_before_min_samples_read(fingerprints):
    for remaining in (0, 1, 3, 6, 100, -1):
        for previous in PREVIOUS:
            assert ph.settled(fingerprints, remaining, previous) is None


@pytest.mark.parametrize('pattern, result', [
    ('aaaaa', True), ('aaa', True), ('abc', False), ('abcdefgh', False),
    ('aaab', None), ('aaaabcde', None),
])
def test_settled_negative_remaining_counts_as_none_left(pattern, result):
    for remaining in (0, -1, -8):
        for previous in PREVIOUS:
            assert ph.settled(list(pattern), remaining, previous) is result


@pytest.mark.parametrize('read, top', _CELLS, ids=['read%d-top%d' % cell for cell in _CELLS])
def test_settled_with_nothing_left_is_judge_except_in_the_buffer(read, top):
    """With nothing left to read, settled() knows what judge() knows --
    except in the buffer (and below MIN_SAMPLES), where it leaves the result
    to judge() and the previous one."""
    want = {'P': True, 'R': False}.get(_cell(read, top))
    for fps in _layouts(read, top):
        for previous in PREVIOUS:
            assert ph.settled(fps, 0, previous) is want


# ================================================================ make_verdict / valid_verdicts

GOOD = {'placeholder': True, 'checked_at': NOW, 'samples': 5}


@pytest.mark.parametrize('placeholder, now, samples, expected', [
    (True, NOW, 5, {'placeholder': True, 'checked_at': NOW, 'samples': 5}),
    (False, NOW + 0.9, 6, {'placeholder': False, 'checked_at': NOW, 'samples': 6}),
    (1, float(NOW), 3.0, {'placeholder': True, 'checked_at': NOW, 'samples': 3}),
    (0, NOW, 0, {'placeholder': False, 'checked_at': NOW, 'samples': 0}),
])
def test_make_verdict(placeholder, now, samples, expected):
    verdict = ph.make_verdict(placeholder, now, samples)
    assert verdict == expected
    assert type(verdict['placeholder']) is bool
    assert type(verdict['checked_at']) is int and type(verdict['samples']) is int


def test_a_written_verdict_reads_back_unchanged():
    """Writer and readers agree: what syncAll writes (make_verdict, saved as
    JSON) is exactly what syncAll and server.py read back (valid_verdicts),
    and it counts as fresh until MAX_AGE_S has passed."""
    written = {CH: ph.make_verdict(True, NOW + 0.7, 5), CH2: ph.make_verdict(False, NOW - H, 6)}
    loaded = ph.valid_verdicts(json.loads(json.dumps(written)))
    assert loaded == written
    assert ph.check_due(loaded[CH], NOW) is False
    assert ph.check_due(loaded[CH], NOW + ph.MAX_AGE_S) is True


@pytest.mark.parametrize('data', [None, [], [(CH, GOOD)], 'text', 0, 1.5, True],
                         ids=['none', 'list', 'pairs', 'text', 'zero', 'float', 'bool'])
def test_valid_verdicts_anything_but_an_object_reads_as_empty(data):
    """{} means "not checked yet": every channel keeps its pictures."""
    assert ph.valid_verdicts(data) == {}


def test_valid_verdicts_keeps_good_entries():
    data = {CH: dict(GOOD), CH2: {'placeholder': False, 'checked_at': NOW - H, 'samples': 6}}
    # False ("real pictures") is a result too, not a missing one
    assert ph.valid_verdicts(data) == data


@pytest.mark.parametrize('cid', [1, '', None, (CH,)], ids=['number', 'empty', 'none', 'tuple'])
def test_valid_verdicts_drops_entries_without_a_text_id(cid):
    assert ph.valid_verdicts({cid: dict(GOOD), CH2: dict(GOOD)}) == {CH2: GOOD}


@pytest.mark.parametrize('entry', [None, 'placeholder', [True, NOW, 5], True, 1],
                         ids=['none', 'text', 'list', 'bool', 'number'])
def test_valid_verdicts_drops_entries_that_are_not_objects(entry):
    assert ph.valid_verdicts({CH: entry, CH2: dict(GOOD)}) == {CH2: GOOD}


@pytest.mark.parametrize('placeholder', [1, 0, 'true', 'false', None, _MISSING],
                         ids=['one', 'zero', 'text-true', 'text-false', 'none', 'missing'])
def test_valid_verdicts_drops_a_placeholder_that_is_not_a_bool(placeholder):
    assert ph.valid_verdicts({CH: _with(GOOD, placeholder=placeholder), CH2: dict(GOOD)}) == {CH2: GOOD}


@pytest.mark.parametrize('checked_at', [True, False, str(NOW), None, [NOW], _MISSING],
                         ids=['true', 'false', 'text', 'none', 'list', 'missing'])
def test_valid_verdicts_drops_an_unusable_check_time(checked_at):
    assert ph.valid_verdicts({CH: _with(GOOD, checked_at=checked_at), CH2: dict(GOOD)}) == {CH2: GOOD}


@pytest.mark.parametrize('samples', [None, '4', True, False, 2.5, [4], _MISSING],
                         ids=['none', 'text', 'true', 'false', 'float', 'list', 'missing'])
def test_valid_verdicts_counts_unreadable_samples_as_zero(samples):
    """The sample count is information only: a bad one doesn't cost the result."""
    assert ph.valid_verdicts({CH: _with(GOOD, samples=samples)}) == {CH: _with(GOOD, samples=0)}


def test_valid_verdicts_float_check_time_becomes_int():
    out = ph.valid_verdicts({CH: _with(GOOD, checked_at=NOW + 0.75)})
    assert out[CH]['checked_at'] == NOW and type(out[CH]['checked_at']) is int


def test_valid_verdicts_keeps_only_the_three_fields():
    assert ph.valid_verdicts({CH: dict(GOOD, note='hand edit', url=_link('x'))}) == {CH: GOOD}


def test_valid_verdicts_neither_changes_nor_shares_the_loaded_data():
    data = {CH: dict(GOOD), 'bad@autonomiques': None, CH2: _with(GOOD, samples='x', checked_at=NOW + 0.5)}
    snapshot = copy.deepcopy(data)
    out = ph.valid_verdicts(data)
    assert data == snapshot
    out[CH]['placeholder'] = False
    assert data[CH]['placeholder'] is True


# ================================================================ check_due / due_order


@pytest.mark.parametrize('verdict', [
    None, 'checked', [], 5, {}, {'placeholder': True},
    {'checked_at': None}, {'checked_at': str(NOW)}, {'checked_at': [NOW]},
], ids=repr)
def test_check_due_without_a_usable_check_time(verdict):
    assert ph.check_due(verdict, NOW) is True


def test_check_due_a_bool_is_not_a_check_time():
    """True would count as 1 (one second after 1970): with a clock at 1 s it
    would look fresh. It is not a time, so the channel is due."""
    assert ph.check_due({'checked_at': True}, 1, max_age=10) is True
    assert ph.check_due({'checked_at': 1}, 1, max_age=10) is False


@pytest.mark.parametrize('age, due', [
    pytest.param(0, False, id='just-checked'),
    pytest.param(0.5, False, id='float-time'),
    pytest.param(ph.MAX_AGE_S - 1, False, id='just-under-a-day'),
    pytest.param(ph.MAX_AGE_S, True, id='exactly-a-day'),
    pytest.param(3 * ph.MAX_AGE_S, True, id='days-old'),
    pytest.param(-1, True, id='a-second-in-the-future'),
    pytest.param(-ph.MAX_AGE_S, True, id='a-day-in-the-future'),
])
def test_check_due_by_age(age, due):
    """Re-checked once a day; a check dated in the future (the clock was
    moved back) is due at once rather than trusted for longer."""
    assert ph.check_due({'placeholder': False, 'checked_at': NOW - age, 'samples': 6}, NOW) is due


def test_check_due_honours_max_age():
    verdict = {'placeholder': True, 'checked_at': NOW - 59, 'samples': 5}
    assert ph.check_due(verdict, NOW, max_age=60) is False
    assert ph.check_due(verdict, NOW + 1, max_age=60) is True


def test_due_order_never_checked_first_then_the_oldest_check():
    verdicts = {
        'a@imp': ph.make_verdict(False, NOW - 10, 6),
        'c@imp': ph.make_verdict(True, NOW - 500, 5),
        'd@imp': ph.make_verdict(False, NOW - 500, 6),                          # same time as c: by id
        'e@imp': {'placeholder': True, 'checked_at': True},                     # not a time: never checked
        'f@imp': 'damaged',
        'g@imp': {'placeholder': False, 'checked_at': NOW - 499.5, 'samples': 6},
    }
    ids = ['d@imp', 'g@imp', 'b@imp', 'a@imp', 'f@imp', 'c@imp', 'e@imp']
    assert ph.due_order(ids, verdicts) == ['b@imp', 'e@imp', 'f@imp', 'c@imp', 'd@imp', 'g@imp', 'a@imp']
    assert ids == ['d@imp', 'g@imp', 'b@imp', 'a@imp', 'f@imp', 'c@imp', 'e@imp']     # a new list


@pytest.mark.parametrize('verdicts', [None, {}, [], 'damaged'], ids=['none', 'empty', 'list', 'text'])
def test_due_order_without_results_is_by_id(verdicts):
    assert ph.due_order(['c@imp', 'a@imp', 'b@imp'], verdicts) == ['a@imp', 'b@imp', 'c@imp']


def test_due_order_takes_any_iterable():
    assert ph.due_order({'b@imp', 'a@imp'}, None) == ['a@imp', 'b@imp']
    assert ph.due_order(iter(['b@imp', 'a@imp']), None) == ['a@imp', 'b@imp']
    assert ph.due_order([], None) == []


# ================================================================ pictures_hidden

_RECORDS = {
    'show':      {'id': CH, 'show_pictures': 'on'},
    'hide':      {'id': CH, 'show_pictures': 'off'},
    'auto':      {'id': CH, 'show_pictures': 'auto'},
    'unset':     {'id': CH},
    'damaged':   {'id': CH, 'show_pictures': 'yes'},
    'no-record': None,
}
_VERDICTS = {
    'placeholder': {'placeholder': True, 'checked_at': NOW, 'samples': 5},
    'real':        {'placeholder': False, 'checked_at': NOW, 'samples': 6},
    'unchecked':   None,
    'damaged':     'placeholder',
}


@pytest.mark.parametrize('mode, detect_on, kind', list(itertools.product(_RECORDS, (True, False), _VERDICTS)))
def test_pictures_hidden_matrix(mode, detect_on, kind):
    """Hide always hides, Show never does; Auto (also unset, damaged or no
    record) hides only while the import's check is on and the channel's last
    result says placeholder."""
    if mode == 'hide':
        expected = True
    elif mode == 'show':
        expected = False
    else:
        expected = detect_on and kind == 'placeholder'
    record = copy.deepcopy(_RECORDS[mode])
    assert ph.pictures_hidden(record, _VERDICTS[kind], detect_on) is expected
    assert record == _RECORDS[mode]


def test_pictures_hidden_the_planned_cases():
    placeholder = ph.make_verdict(True, NOW, 5)
    real = ph.make_verdict(False, NOW, 6)
    assert ph.pictures_hidden({'id': CH}, placeholder, True) is True                             # TV3 on Auto: logo
    assert ph.pictures_hidden({'id': CH, 'show_pictures': 'on'}, placeholder, True) is False     # Show beats the check
    assert ph.pictures_hidden({'id': CH}, placeholder, False) is False                           # switch off: pictures back
    assert ph.pictures_hidden({'id': CH2, 'show_pictures': 'off'}, real, False) is True          # Hide, even with the switch off
    assert ph.pictures_hidden({'id': CH2}, real, True) is False                                  # real pictures stay


@pytest.mark.parametrize('placeholder', [1, 'true', 'yes', [True]], ids=repr)
def test_pictures_hidden_needs_a_true_result(placeholder):
    """Only a real True counts (valid_verdicts drops anything else anyway)."""
    assert ph.pictures_hidden({}, {'placeholder': placeholder, 'checked_at': NOW}, True) is False


# ================================================================ strip_pictures


def _guide():
    rating = {'system': 'ES', 'value': '12', 'icon': [{'src': _link('rating-12')}]}
    return [
        _show('Telenotícies', NOW, _link('tv3-0'), desc=[('Les notícies', 'ca')], rating=[rating],
              **{'catchup-id': 'replay-0'}),
        _show('Polònia', NOW + H, _link('tv3-1')),
        _show('Cuines', NOW + 2 * H),                                               # TV3, no picture
        _show('Newsroom', NOW, _link('cnn-0'), ch=CH2, rating=[copy.deepcopy(rating)]),
        _show('Cine', NOW, _link('own-0'), ch=CUSTOM),
        _show('MTV Hits', NOW, _link('mtv-0'), ch=CH3),
    ]


def test_strip_pictures_removes_only_the_hidden_channels_own_picture():
    guide = _guide()
    expected = copy.deepcopy(guide)
    for programme in expected:
        if programme['channel'] in (CH, CH3):
            programme.pop('icon', None)
    assert ph.strip_pictures(guide, {CH, CH3}) == 3
    assert guide == expected
    first = guide[0]
    assert first['rating'][0]['icon'] == [{'src': _link('rating-12')}]      # a rating's own icon stays
    assert first['desc'] == [('Les notícies', 'ca')] and first['catchup-id'] == 'replay-0'


@pytest.mark.parametrize('container', [set, frozenset, list, tuple])
def test_strip_pictures_takes_any_collection_of_ids(container):
    guide = _guide()
    assert ph.strip_pictures(guide, container([CH])) == 2
    assert [ph.picture_src(p) for p in guide] == ['', '', '', _link('cnn-0'), _link('own-0'), _link('mtv-0')]


@pytest.mark.parametrize('hidden', [set(), frozenset(), [], (), None], ids=repr)
def test_strip_pictures_nothing_hidden_changes_nothing(hidden):
    guide = _guide()
    snapshot = copy.deepcopy(guide)
    assert ph.strip_pictures(guide, hidden) == 0
    assert guide == snapshot


def test_strip_pictures_skips_entries_that_are_not_programmes():
    guide = [None, 'text', 42, ['channel', CH], _show('A', NOW, _link('a'))]
    assert ph.strip_pictures(guide, {CH}) == 1
    assert guide[:4] == [None, 'text', 42, ['channel', CH]]
    assert 'icon' not in guide[4]


@pytest.mark.parametrize('programmes', [None, [], ()], ids=['none', 'list', 'tuple'])
def test_strip_pictures_on_an_empty_guide(programmes):
    assert ph.strip_pictures(programmes, {CH}) == 0


def test_strip_pictures_running_again_changes_nothing():
    """syncAll runs it on every publish, also over shows it already stripped."""
    guide = _guide()
    assert ph.strip_pictures(guide, {CH}) == 2
    after_first = copy.deepcopy(guide)
    assert ph.strip_pictures(guide, {CH}) == 0
    assert guide == after_first


def test_strip_pictures_edits_the_guide_in_place():
    guide = _guide()
    items = list(guide)
    ph.strip_pictures(guide, {CH})
    assert len(guide) == len(items) and all(a is b for a, b in zip(guide, items))


# ================================================================ the TV3 case, end to end


def test_the_tv3_case_end_to_end_without_downloads():
    """The decisions in syncAll's order, with picture bytes from a dict
    instead of downloads and the check's own early stop (_check). TV3's
    links all differ; most answer with the same advert (as epg.best did),
    two with a real picture -- and both of those are sampled, which the old
    "all identical" rule took for real pictures. CNN's are all real.

    Day 1: TV3 is placeholder (6 of 8), so all its shows lose their picture,
    the two real ones too (the setting is per channel); Show, or the
    import's switch off, brings them back. Day 2: TV3's samples are only
    half advert -- one short of placeholder -- so the previous result
    stands and the channel doesn't flip."""
    advert = b'\x89PNG BesTivi / Get it on Google Play'
    body = {}

    def channel(ch, prefix, titles, real=None):
        """Shows with their own links; `real`: indexes answering with a
        real picture (None = every one), the others with the advert."""
        shows = []
        for i, title in enumerate(titles):
            link = _link('%s-%d' % (prefix, i))
            body[link] = link.encode('ascii') if real is None or i in real else advert
            shows.append(_show(title, NOW + i * H, link, ch=ch))
        return shows

    def fingerprint(url):
        data = body.get(url)
        return hashlib.sha256(data[:ph.FP_BYTES]).hexdigest() if data is not None else None

    import_cfg = {'id': 'autonomiques'}                 # switch never set: on
    records = {CH: {'id': CH}, CH2: {'id': CH2}}        # both on Auto

    def sync(guide, verdicts, now):
        """syncAll's check for the import (Imports._checkShowPictures): due
        channels with enough pictures, oldest check first, each judged
        against its previous result; then saved and read back."""
        due = [cid for cid in records if ph.check_due(verdicts.get(cid), now)]
        for cid in ph.due_order(due, verdicts):
            samples = ph.pick_samples(guide, cid)
            if len(samples) < ph.MIN_SAMPLES:
                continue
            previous = verdicts.get(cid)
            previous = previous.get('placeholder') if isinstance(previous, dict) else None
            result, fps = _check(samples, fingerprint, previous)
            if result is not None:
                verdicts[cid] = ph.make_verdict(result, now, sum(1 for fp in fps if fp))
        return ph.valid_verdicts(json.loads(json.dumps(verdicts)))

    # ---- day 1: 12 TV3 titles, 2 with a real picture (the 2nd and 7th)
    tv3 = channel(CH, 'tv3', ('Telenotícies', 'Els matins', 'Cuines', 'Polònia', 'APM?', 'Tot es mou',
                              'Està passant', 'Divendres', 'El foraster', 'Sense ficció', 'Joc de cartes',
                              'Mésnit'), real={1, 6})
    cnn = channel(CH2, 'cnn', ('Newsroom', 'Connect the World', 'Amanpour', 'Quest Means Business',
                               'The Lead', 'Anderson Cooper 360', 'CNN Tonight', 'Inside Politics',
                               'State of the Union', 'Fareed Zakaria GPS'))
    own = [_show('Cine', NOW, _link('own-0'), ch=CUSTOM)]
    guide = tv3 + cnn + own
    tv3_fps = [fingerprint(url) for url in ph.pick_samples(guide, CH)]
    assert len(set(tv3_fps)) == 3 and len(tv3_fps) == ph.SAMPLES          # the advert + 2 real pictures

    verdicts = sync(guide, {}, NOW)
    # TV3 stops after 7 downloads (5 adverts seen, 1 left: settled); CNN after 6
    assert verdicts == {CH: {'placeholder': True, 'checked_at': NOW, 'samples': 7},
                        CH2: {'placeholder': False, 'checked_at': NOW, 'samples': 6}}

    hidden = {cid for cid, record in records.items()
              if ph.pictures_hidden(record, verdicts.get(cid), ph.detect_enabled(import_cfg))}
    assert hidden == {CH}
    assert ph.strip_pictures(guide, hidden) == len(tv3)
    assert [ph.picture_src(p) for p in tv3] == [''] * len(tv3)
    assert all(ph.picture_src(p) for p in cnn + own)

    assert ph.check_due(verdicts[CH], NOW + H) is False         # not re-checked within the day
    assert ph.pictures_hidden(dict(records[CH], show_pictures='on'), verdicts[CH], True) is False
    switch_off = dict(import_cfg, detect_placeholder_pictures=False)
    assert ph.pictures_hidden(records[CH], verdicts[CH], ph.detect_enabled(switch_off)) is False

    # ---- day 2: a fresh guide; TV3's 8 titles alternate advert / real picture
    later = NOW + ph.MAX_AGE_S
    tv3 = channel(CH, 'tv3-day2', ('Telenotícies', 'Els matins', 'Cuines', 'Polònia', 'APM?',
                                   'Tot es mou', 'Està passant', 'Divendres'), real={1, 3, 5, 7})
    guide = tv3 + cnn + own
    tv3_fps = [fingerprint(url) for url in ph.pick_samples(guide, CH)]
    assert ph.judge(tv3_fps) is False            # on its own, 4 of 8 is not a placeholder...
    verdicts = sync(guide, verdicts, later)
    assert verdicts == {CH: {'placeholder': True, 'checked_at': later, 'samples': 8},     # ...after one, it stays
                        CH2: {'placeholder': False, 'checked_at': later, 'samples': 6}}


# ================================================================ calibration on real guide data
# imports.66: the rule's numbers come from the real guides. The patterns
# below were taken once from two censuses of the live guides (one picture
# read per show title, as the check reads them) and are embedded here as
# letters: the same letter = the same picture, so no picture link, feed or
# fingerprint is in this file and nothing is downloaded.


def _letter_guide(ch, letters_by_day, day0):
    """A channel's guide built from a census pattern: one distinct title per
    letter, in order; the titles of letters_by_day[d] first air on day d
    after `day0`, 10 minutes apart. Every show has its own picture link, and
    'downloading' a link gives its letter. Returns (programmes, fingerprint)."""
    guide, letter_of = [], {}
    for day, letters in enumerate(letters_by_day):
        for i, letter in enumerate(letters):
            link = _link('%s-%d-%d' % (ch, day, i))
            guide.append(_show('%s show %d-%d' % (ch, day, i), day0 + day * DAY + (i + 1) * 600, link, ch=ch))
            letter_of[link] = letter
    return guide, letter_of.get


# Saturday 2026-11-08, epg.best, the three advert channels: every title with
# a picture, by the local day (Madrid, +0100) it first airs, 2026-11-07 to
# 2026-11-14, each day in start order. 'a' = the BesTivi advert; every other
# letter is one real picture (all different).
SATURDAY = {
    'TV3Cat.es': ('baaaaaacaaadaae', 'aaaaaaaafaa', 'aaaaagaaaaa', 'a', 'h', 'aaaaaa', 'a', 'aaaa'),
    'MTV.es':    ('aabaacadaaae', 'aa', 'aaaaa', '', '', '', 'a', 'aa'),
    'APunt.es':  ('aaaaabaaaaaaaaaaa', 'aaaaaaaaaaaaaaaaaa', 'aaaa', 'aa', 'a', '', '', ''),
}
SATURDAY_DAY0 = calendar.timegm((2026, 11, 7, 0, 0, 0, 0, 0, 0)) - H     # 2026-11-07 00:00 +0100


@pytest.mark.parametrize('ch, titles, advert', [
    ('TV3Cat.es', 50, 43), ('MTV.es', 22, 18), ('APunt.es', 42, 41),
])
def test_saturday_census_is_the_one_in_the_plan(ch, titles, advert):
    letters = ''.join(SATURDAY[ch])
    assert (len(letters), letters.count('a')) == (titles, advert)
    real = letters.replace('a', '')
    assert len(set(real)) == len(real)          # every real picture is a different one


@pytest.mark.parametrize('ch, results', [
    ('TV3Cat.es', [True] * 7),
    ('MTV.es', [True] * 7),
    # From 2026-11-11 on, Saturday's data has fewer than MIN_SAMPLES À Punt
    # titles with a picture left: the channel isn't checked (None) and its
    # placeholder result stands.
    ('APunt.es', [True, True, True, True, None, None, None]),
])
def test_saturday_advert_channels_hide_the_advert_every_day_of_a_week(ch, results):
    """The guide window slides a day at a time for 7 days (the titles first
    airing on or after each day); each day the channel is checked as
    imports.py checks it, its previous result chained from the day before.
    The recorded result is placeholder every day: no flip."""
    guide, fingerprint = _letter_guide(ch, SATURDAY[ch], SATURDAY_DAY0)
    previous, checked, recorded = None, [], []
    for day in range(7):
        cutoff = SATURDAY_DAY0 + day * DAY
        window = [p for p in guide if replay_helpers.xmltv_to_epoch(p['start']) >= cutoff]
        samples = ph.pick_samples(window, ch)
        result = _check(samples, fingerprint, previous)[0] if len(samples) >= ph.MIN_SAMPLES else None
        if result is not None:
            previous = result
        checked.append(result)
        recorded.append(previous)
    assert checked == results
    assert recorded == [True] * 7


@pytest.mark.parametrize('ch', sorted(SATURDAY))
def test_saturday_advert_channels_are_placeholder_from_any_first_show(ch):
    """Finer than a day: whichever show the guide starts at, the 8 spread
    samples say placeholder on their own -- with no previous result, so not
    even the buffer is needed. Some of those samples include a real picture:
    there, the old "all samples identical" rule said real pictures."""
    guide, fingerprint = _letter_guide(ch, SATURDAY[ch], SATURDAY_DAY0)
    with_real = 0
    for first in range(len(guide) - ph.MIN_SAMPLES + 1):
        samples = ph.pick_samples(guide[first:], ch)
        assert _check(samples, fingerprint)[0] is True, first
        with_real += any(fingerprint(url) != 'a' for url in samples)
    assert with_real > 0


# 2026-11-10, every imported channel (Movistar+, epg.best and the
# autonomiques import; epg.best had removed its advert on 2026-11-09): up to
# 30 different show titles per channel, spread over its guide, in start
# order. Within a channel the same letter = the same picture.
TODAY = {
    '13TV':                'abcdefghijklmnopqrstuvwxyz',
    '24H':                 'abcdefghijklmnopqrstfu',
    'A3':                  'abcdefghijklmnopqrstuvwxyzAB',
    'ALJAZE':              'abcdefghijklmnopq',
    'APunt.es':            'a',
    'ATRESS':              'abcdefghi',
    'AXN':                 'abcdefghijklmnopqrstuvw',
    'BEMAD':               'abcdefghijklmnopqrstuvwxyzABCD',
    'BuzzrTV.us':          'abcdefghijklmnopqrstuvwxyzABCD',
    'C4':                  'abcdefghijklmnopqrstuv',
    'CartoonNetwork.us':   'abcdefghijklmnopqrstuvwxyzABCD',
    'CCTV-E':              'abcdefghijkl',
    'CL13':                'abcdefghijklmnopq',
    'CNBC.uk':             'abcdefghijklmnopqrstuvuwxkkyzA',
    'CNN.au':              'abcdefghijklmnopqrstuvwxyzABCD',
    'CNNInternational.us': 'abcdefghijklmnopqrstuvklmwxyzA',
    'COSMO':               'abcdefghijklmnopqrstuvwxyz',
    'DCR':                 'abcdefghijklmnoopqrstuvwxyzABC',
    'DCRMAX':              'abcdefghijklmnopqrstuvwxyzABCD',
    'DIVINI':              'abcdefghijklm',
    'DKISS':               'abcdefghijklmnopqrstuvwxyyzABC',
    'DWSP':                'abcdefghijklmnopq',
    'ENERGY':              'abcdefghijk',
    'ENW':                 'abcdefghijk',
    'FDFIC':               'abcdefghij',
    'FOXGE':               'abcdefghijklmnopqrs',
    'FREELI':              'abcdefghijkllmnoplqrstuvwxyzAB',
    'GameShowNetwork.us':  'abcdefghijklmnopqrstuvwxyzABCD',
    'LA2':                 'abcdefghijklmnopqrstuvwxyzABCD',
    'MDOC':                'abcdefghijklmnopqrrstuvwxyzABC',
    'MEGA':                'abcdefghijklm',
    'MEZZO':               'abcdefghijklmnopqrstuvwxyzABCD',
    'MHITS':               'abcdefghijklmnopqrstuvwxyzABCD',
    'MORIG':               'abcdefghijklmnopqorstuvwxyzABs',
    'MPLUS':               'abcdefghijklmnopqrstnuvwxyzABC',
    'MROMA':               'abcdefghijklmnopqrstuvwxyzABCD',
    'MSNBC.ca':            'abcdefghigjklmnopqrstuvdwxuyyz',
    'MTV.es':              'ab',
    'MV1':                 'abcdefghijklmnopqrstuvwxyzABCD',
    'NATGEO':              'abcdefghijklmnopqrstuvwxyzABCD',
    'NATGW':               'abcdefghijklmnopqrsttuvwxyzABC',
    'NEOX':                'abcdefghijklmn',
    'NICK':                'abcdefghijklmnopq',
    'NOVA':                'abcdefghijklmnopqrstuv',
    'PCM':                 'abcdefghijklmnopqrstuvw',
    'PLAYDC':              'abcdefghijklmnopq',
    'RED':                 'abcdefghij',
    'SCI-FI':              'abcdefghijklmnopqrstuvw',
    'SET':                 'abcdefghijklmnopqrstuvwxyzABCD',
    'SEXTA':               'abcdefghijklmnopqrstuv',
    'T5':                  'abcdefghijklmnopqrstuvwx',
    'TCM':                 'abcdefghijklmnopqrstuvwxyzABCD',
    'TEN':                 'abcdefghijklmnopqqrstuvwxyzABC',
    'TERROR':              'abcdefghijklmnopqrstuvwxyzABCD',
    'TNT':                 'abcdefghijklmnopqrstuvwxyz',
    'TV3Cat.es':           'abcde',
    'TVE':                 'abcdefghijkdlmnopqrstuvwx',
    'TVLand.us':           'abcdefghij',
    'VEO7':                'abcdefghi',
}
# À Punt and MTV had only 1 and 2 show titles with a picture: too few to
# judge, so the check leaves them alone (their previous result stands).
TODAY_TOO_FEW = ('APunt.es', 'MTV.es')


def _today_samples(ch):
    guide, fingerprint = _letter_guide(ch, (TODAY[ch],), NOW)
    return ph.pick_samples(guide, ch), fingerprint


@pytest.mark.parametrize('ch', sorted(set(TODAY) - set(TODAY_TOO_FEW)))
def test_today_every_channel_with_real_pictures_is_judged_real(ch):
    """With 8 spread samples, every channel that has enough pictures comes
    out real -- whatever its previous result, so none is even in the
    buffer -- and a full sample costs 6 downloads."""
    samples, fingerprint = _today_samples(ch)
    assert len(samples) >= ph.MIN_SAMPLES
    fps = [fingerprint(url) for url in samples]
    for previous in PREVIOUS:
        assert ph.judge(fps, previous) is False
        result, read = _check(samples, fingerprint, previous)
        assert result is False
        if len(samples) == ph.SAMPLES:
            assert len(read) == 6


@pytest.mark.parametrize('ch', TODAY_TOO_FEW)
def test_today_channels_with_too_few_pictures_are_not_judged(ch):
    samples, fingerprint = _today_samples(ch)
    assert len(samples) < ph.MIN_SAMPLES
    for previous in PREVIOUS:
        assert ph.judge([fingerprint(url) for url in samples], previous) is None


def test_today_census_matches_the_plans_figures():
    """59 channels; the most common picture covers at most 3 of a channel's
    30 titles; in the 8 spread samples 58 channels have all-different
    pictures and one (CNN International) has 2 alike."""
    assert len(TODAY) == 59
    assert max(max(letters.count(x) for x in letters) for letters in TODAY.values()) == 3
    alike = []
    for ch in TODAY:
        samples, fingerprint = _today_samples(ch)
        fps = [fingerprint(url) for url in samples]
        if len(set(fps)) < len(fps):
            alike.append(ch)
    assert alike == ['CNNInternational.us']
    assert sorted(ch for ch, letters in TODAY.items() if len(letters) < ph.MIN_SAMPLES) == sorted(TODAY_TOO_FEW)


# ================================================================ hand-edited / odd input (review fixes)
# The verdicts file is JSON, and Python's json reads Infinity and NaN. A bad
# check time in one entry must drop that entry only: before this fix it made
# valid_verdicts raise, the loader caught it and the whole file read as {}
# (every channel back to its placeholder pictures until the next check).


@pytest.mark.parametrize('bad', [float('inf'), float('-inf'), float('nan')], ids=['inf', '-inf', 'nan'])
def test_valid_verdicts_drops_a_non_finite_check_time_and_keeps_the_rest(bad):
    good = {'placeholder': True, 'checked_at': NOW, 'samples': 5}
    data = json.loads(json.dumps({CH: good, 'other@imp': {'placeholder': False, 'checked_at': bad, 'samples': 6}}))
    assert ph.valid_verdicts(data) == {CH: good}


@pytest.mark.parametrize('bad', [float('inf'), float('-inf'), float('nan')], ids=['inf', '-inf', 'nan'])
def test_check_due_a_non_finite_check_time_is_due(bad):
    assert ph.check_due({'placeholder': True, 'checked_at': bad}, NOW) is True


def test_due_order_treats_a_non_finite_check_time_as_never_checked():
    verdicts = {'b@imp': {'checked_at': float('nan')}, 'c@imp': {'checked_at': NOW - 10}}
    assert ph.due_order(['c@imp', 'b@imp', 'a@imp'], verdicts) == ['a@imp', 'b@imp', 'c@imp']


@pytest.mark.parametrize('record', [['show_pictures', 'off'], ('off',), 'off', 7], ids=repr)
def test_record_mode_of_a_record_that_is_not_a_dict_is_auto(record):
    assert ph.record_mode(record) == 'auto'
    assert ph.pictures_hidden(record, {'placeholder': True, 'checked_at': NOW}, False) is False


@pytest.mark.parametrize('cfg', [['detect_placeholder_pictures', False], ('x',), 'off', 3], ids=repr)
def test_detect_enabled_of_an_import_that_is_not_a_dict_is_on(cfg):
    assert ph.detect_enabled(cfg) is True


def test_detect_enabled_nan_counts_as_on_like_any_other_junk():
    assert ph.detect_enabled({'detect_placeholder_pictures': float('nan')}) is True


def test_strip_pictures_removes_an_empty_icon_list_without_counting_it():
    with_picture = _show('Has one', NOW, link=_link('has-one'))
    empty = _show('Empty icon', NOW + H, icon=[])
    other = _show('Other channel', NOW, link=_link('other'), ch='other@imp')
    guide = [with_picture, empty, other]
    assert ph.strip_pictures(guide, {CH}) == 1
    assert 'icon' not in with_picture and 'icon' not in empty
    assert ph.picture_src(other)


# ================================================================ review round 2 (2026-11-10)


def test_title_key_reads_the_first_of_several_titles():
    """XMLTV programmes can carry one <title> per language: the key is the
    first title's, as replay_helpers reads it -- not a later ASCII one."""
    assert ph._title_key({'title': [('Новости', 'ru'), ('News', 'en')]}) == 'новости'


def test_a_labelled_non_latin_series_is_one_title():
    """Episode labels (davidmuma style) on a series titled in another script
    don't make every episode a different show: a run of one series with one
    poster fills one sample, not eight."""
    guide = [_show('Сериал [COLOR tomato]T1 E%d[/COLOR]' % i, NOW + i * H, _link('poster')) for i in range(6)]
    guide += [_show('Другое %d' % i, NOW + (6 + i) * H, _link('other-%d' % i)) for i in range(2)]
    assert ph.pick_samples(guide, CH) == [_link('poster'), _link('other-0'), _link('other-1')]
