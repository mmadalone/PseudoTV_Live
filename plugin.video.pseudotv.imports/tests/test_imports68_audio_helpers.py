# -*- coding: utf-8 -*-
"""imports.68: LiveTV audio follows the channel -- livesubs_helpers, the audio decisions.

The bug: a replay and the same LiveTV channel live played different audio.
Kodi keeps video settings, the audio track included, per video. Every replay
(pvr://guide/<epg>/<start>.epg) is a new video for Kodi, so it starts in the
preferred audio language (English on this TV: locale.audiolanguage). A live
channel is one video, so it starts in whatever Kodi saved for it last (Neox
live: Spanish, track 0). Watching Neox in English in a replay and then tuning
Neox live gave Spanish, and the other way round.

The user's rule (2026-11-11), per channel: "last audio on the channel". The
audio language that last played on a LiveTV channel, whether you picked it or
Kodi did, starts that channel next time, live or replay.

Measured 2026-11-11 on Warner TV (JSON-RPC Player.GetProperties 'audiostreams'):
  live    0 spa (default), 1 eng (labelled impaired -- normal English, the
          user confirmed), 2 es-[ADS] (Spanish audio description, impaired);
  replay  the same codes, every track default, none impaired.
The codes are identical live and in replays (only the flags differ), so
tracks are matched by code. Kodi's VideoPlayer.AudioLanguage reads the
playing track's code ('eng').

The imports.68 section of livesubs_helpers holds every decision and imports
nothing, so it runs here directly, without Kodi stubs:
  - lang_code: a code for comparing (case and surrounding spaces ignored);
  - valid_audio_memory: the memory file (cache/livetv_audio.json) read back;
  - audio_tracks / audio_track: the offered tracks, and which one carries a
    language (a track not labelled impaired wins a tie; no partial matches,
    so the audio description is never taken for Spanish);
  - audio_restore_action: the switch when a channel starts (and at the 1 s
    re-check);
  - audio_settle_record: what to remember once the playback has settled
    (Kodi's pick counts; a language the stream doesn't offer never replaces
    the remembered one);
  - audio_change_record: a change while it plays -- yours is remembered; one
    Kodi had to make because the language disappeared (a Spanish-only show
    on live) is "forced" and isn't;
  - audio_return_action: the switch back once the remembered language is
    offered again;
  - remember_audio: the copy-on-write update the play thread reads without a
    lock.
The Kodi side (services.py) is tested apart. The end of this file chains the
helpers through the Neox case and the Warner TV case.
"""
import ast
import copy
import inspect
import itertools
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
LIB  = os.path.normpath(os.path.join(HERE, '..', 'resources', 'lib'))
if LIB not in sys.path:
    sys.path.insert(0, LIB)

import livesubs_helpers as ls   # noqa: E402

# Channel keys as imports.67's channel_key gives them (channels.json ids, or
# 'name:<name>' for a channel no listed import has).
NEOX      = 'NEOX@movistarplus'                         # Neox, 8
WARNER    = 'TNT@movistarplus'                          # Warner TV, 12
CANAL_SUR = 'name:Canal Sur'                            # another PVR source

_MISSING = object()     # "leave this key out" in parametrized entries
BOOLS    = (True, False)


def _stream(index, language, default, impaired):
    """One entry of Kodi's Player.GetProperties 'audiostreams' answer."""
    return {'index': index, 'language': language, 'name': '', 'isdefault': default, 'isimpaired': impaired}


# Measured 2026-11-11 (Warner TV).
WARNER_LIVE_TRACKS = (
    _stream(0, 'spa',      True,  False),
    _stream(1, 'eng',      False, True),       # labelled impaired on live; normal English
    _stream(2, 'es-[ADS]', False, True),       # Spanish audio description
)
WARNER_REPLAY_TRACKS = (
    _stream(0, 'spa',      True, False),
    _stream(1, 'eng',      True, False),
    _stream(2, 'es-[ADS]', True, False),
)
# Measured: Neox live offers spa (default) and eng (labelled impaired).
NEOX_LIVE_TRACKS = (
    _stream(0, 'spa', True,  False),
    _stream(1, 'eng', False, True),
)
# Neox replays modelled on Warner TV's replay: the live codes, every track default.
NEOX_REPLAY_TRACKS = (
    _stream(0, 'spa', True, False),
    _stream(1, 'eng', True, False),
)
# A show with Spanish audio only.
SPANISH_ONLY_TRACKS = (
    _stream(0, 'spa', True, False),
)


def _props(tracks, current=_MISSING):
    """The JSON-RPC 'result' services.py passes on ((resp or {}).get('result'))."""
    props = {'audiostreams': [dict(t) for t in tracks]}
    if current is not _MISSING:
        props['currentaudiostream'] = current
    return props


LIVE_PROPS    = _props(WARNER_LIVE_TRACKS, dict(WARNER_LIVE_TRACKS[0]))
REPLAY_PROPS  = _props(WARNER_REPLAY_TRACKS, dict(WARNER_REPLAY_TRACKS[1]))
SPANISH_PROPS = _props(SPANISH_ONLY_TRACKS, dict(SPANISH_ONLY_TRACKS[0]))
PROPS = {'live': LIVE_PROPS, 'replay': REPLAY_PROPS, 'spanish-only': SPANISH_PROPS}

# Languages as Kodi spells them, and '' for "none playing / nothing saved".
LANGS = ('spa', 'eng', 'es-[ADS]')

# The functions the imports.68 section adds (the contract services.py calls).
SIGNATURES = {
    'lang_code':            ['value'],
    'valid_audio_memory':   ['data'],
    'audio_tracks':         ['props'],
    'audio_track':          ['props', 'lang'],
    'audio_restore_action': ['saved', 'playing', 'props'],
    'audio_settle_record':  ['saved', 'playing', 'saved_offered'],
    'audio_change_record':  ['playing', 'baseline', 'baseline_offered'],
    'audio_return_action':  ['saved', 'playing', 'props'],
    'remember_audio':       ['memory', 'key', 'lang'],
}


def _src():
    with open(os.path.join(LIB, 'livesubs_helpers.py'), encoding='utf-8') as f:
        return f.read()


def _section_start():
    """The line of the imports.68 section header in livesubs_helpers.py."""
    for number, line in enumerate(_src().splitlines(), 1):
        if line.startswith('# ---') and 'imports.68: audio' in line:
            return number
    raise AssertionError('imports.68 section header not found')


def _section_functions():
    tree = ast.parse(_src(), feature_version=(3, 8))
    start = _section_start()
    return [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.lineno > start]


def _is_index(value, expected):
    """A real int (not a bool or float) equal to `expected`: what setAudioStream takes."""
    return type(value) is int and value == expected


def _code_at(props, index):
    """The comparable code of the track with `index` in `props`."""
    return next(code for i, code, _ in ls.audio_tracks(props) if i == index)


# ================================================================ module hygiene


def test_imports68_section_defines_the_planned_functions():
    """The audio helpers live in one tagged section at the end of the module."""
    assert {node.name for node in _section_functions()} == set(SIGNATURES)
    assert 'imports.68' in _src()


@pytest.mark.parametrize('name', sorted(SIGNATURES))
def test_imports68_signatures_are_the_contract(name):
    """services.py calls these by position: the parameters and their order are
    fixed, and none has a default a call could change."""
    params = inspect.signature(getattr(ls, name)).parameters
    assert list(params) == SIGNATURES[name]
    assert all(p.default is inspect.Parameter.empty for p in params.values())


def test_imports68_names_are_public():
    public = {name for name in vars(ls) if not name.startswith('_')}
    assert set(SIGNATURES) | {'AUDIO_MEMORY_FILE'} <= public


def test_imports68_section_does_no_io_and_keeps_no_state():
    """The memory file is read and written by services.py, the tracks are read
    there too (JSON-RPC Player.GetProperties): the helpers only decide, and
    keep nothing between calls."""
    called = set()
    for function in _section_functions():
        for node in ast.walk(function):
            assert not isinstance(node, (ast.Global, ast.Nonlocal, ast.Import, ast.ImportFrom)), function.name
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    called.add(node.func.id)
                elif isinstance(node.func, ast.Attribute):
                    called.add(node.func.attr)
    assert not called & {'open', '__import__', 'eval', 'exec', 'compile', 'input', 'print', 'setJSON', 'getJSON'}


def test_audio_memory_file_constant():
    """cache/livetv_audio.json: a bare file name next to the subtitle memory,
    never the same file (the subtitle file's format stays untouched)."""
    assert ls.AUDIO_MEMORY_FILE == 'livetv_audio.json'
    assert '/' not in ls.AUDIO_MEMORY_FILE and '\\' not in ls.AUDIO_MEMORY_FILE
    assert ls.AUDIO_MEMORY_FILE != ls.MEMORY_FILE
    assert ls.MEMORY_FILE == 'livetv_subtitles.json'


def test_module_still_parses_as_python38():
    ast.parse(_src(), feature_version=(3, 8))


# ================================================================ lang_code


@pytest.mark.parametrize('value, code', [
    ('eng', 'eng'), ('ENG', 'eng'), ('Eng', 'eng'), ('eNg', 'eng'),
    (' eng ', 'eng'), ('\teng\n', 'eng'), ('  SPA', 'spa'),
    ('es-[ADS]', 'es-[ads]'), (' ES-[ads] ', 'es-[ads]'),
], ids=repr)
def test_lang_code_ignores_case_and_surrounding_spaces(value, code):
    assert ls.lang_code(value) == code


@pytest.mark.parametrize('value', [
    None, '', '   ', '\t\n', 0, 1, 1.5, True, False, b'eng', ['eng'], ('eng',), {'language': 'eng'},
], ids=repr)
def test_lang_code_non_text_or_blank_is_empty(value):
    """No language: '' (text), never None or the value itself."""
    out = ls.lang_code(value)
    assert out == '' and type(out) is str


def test_lang_code_keeps_inner_text_and_maps_no_codes():
    """Codes are compared as Kodi gives them, no mapping between forms: 'es'
    is not 'spa' and the audio description is not Spanish (risk 3 of the
    plan: a source that spells languages differently only matches its own)."""
    assert ls.lang_code('es-[ADS]') != ls.lang_code('spa')
    assert ls.lang_code('es') != ls.lang_code('spa')
    assert ls.lang_code('en') != ls.lang_code('eng')
    assert ls.lang_code('e ng') == 'e ng'


# ================================================================ valid_audio_memory

AUDIO_GOOD = {NEOX: 'eng', WARNER: 'spa', CANAL_SUR: 'es-[ADS]', 'CNN.us@autonomiques': 'ENG'}


@pytest.mark.parametrize('data', [
    None, [], [[NEOX, 'eng']], 'text', '{"NEOX@movistarplus": "eng"}', b'{}', 1, 0, True, 2.5,
    (NEOX, 'eng'), {NEOX},
], ids=repr)
def test_valid_audio_memory_anything_but_a_dict_reads_as_empty(data):
    """A damaged file is an empty memory: every channel starts again from
    whatever Kodi picks (and that pick is remembered)."""
    assert ls.valid_audio_memory(data) == {}


@pytest.mark.parametrize('key, value', [
    pytest.param('', 'eng', id='empty-key'),
    pytest.param(1, 'eng', id='number-key'),
    pytest.param(None, 'eng', id='none-key'),
    pytest.param(('NEOX', 'movistarplus'), 'eng', id='tuple-key'),
    pytest.param(2.5, 'spa', id='float-key'),
    pytest.param('EMPTY@x', '', id='value-empty'),
    pytest.param('SPACES@x', '   ', id='value-spaces'),
    pytest.param('NONE@x', None, id='value-none'),
    pytest.param('TRUE@x', True, id='value-true'),       # a subtitle on/off, not a language
    pytest.param('FALSE@x', False, id='value-false'),
    pytest.param('ONE@x', 1, id='value-number'),
    pytest.param('FLOAT@x', 1.0, id='value-float'),
    pytest.param('LIST@x', ['eng'], id='value-list'),
    pytest.param('DICT@x', {'language': 'eng'}, id='value-dict'),
    pytest.param('BYTES@x', b'eng', id='value-bytes'),
])
def test_valid_audio_memory_drops_bad_entries_one_by_one(key, value):
    """Text keys with a non-empty text value only; one bad entry doesn't cost the others."""
    data = dict(AUDIO_GOOD)
    data[key] = value
    assert ls.valid_audio_memory(data) == AUDIO_GOOD


def test_valid_audio_memory_strips_values_and_keeps_kodis_spelling():
    """Spaces around a value are dropped; the spelling (case) is Kodi's own,
    compared later through lang_code."""
    out = ls.valid_audio_memory({NEOX: ' eng\n', WARNER: 'ENG', CANAL_SUR: 'es-[ADS]'})
    assert out == {NEOX: 'eng', WARNER: 'ENG', CANAL_SUR: 'es-[ADS]'}


def test_the_subtitle_and_audio_files_cannot_be_mistaken_for_each_other():
    """The two memory files hold different values (on/off vs a language): one
    read as the other gives an empty memory, never a wrong choice."""
    subtitles = {WARNER: True, NEOX: False}
    assert ls.valid_audio_memory(subtitles) == {}
    assert ls.valid_memory(AUDIO_GOOD) == {}


def test_valid_audio_memory_reads_a_damaged_file_text():
    text = ('{"NEOX@movistarplus": "eng", "TNT@movistarplus": true, "": "spa", '
            '"name:Canal Sur": " spa ", "name:La 1": "", "MTV.es@autonomiques": 7}')
    assert ls.valid_audio_memory(json.loads(text)) == {NEOX: 'eng', CANAL_SUR: 'spa'}


def test_valid_audio_memory_round_trip_through_the_file():
    text = json.dumps(AUDIO_GOOD, sort_keys=True)
    assert ls.valid_audio_memory(json.loads(text)) == AUDIO_GOOD


def test_valid_audio_memory_is_a_new_dict_and_leaves_its_input_alone():
    data   = dict(AUDIO_GOOD, bad=1)
    before = dict(data)
    out    = ls.valid_audio_memory(data)
    assert out is not data
    assert data == before
    out[NEOX] = 'spa'
    assert data[NEOX] == 'eng'


# ================================================================ audio_tracks


def test_audio_tracks_warner_live():
    """(index, comparable code, labelled impaired), in Kodi's order."""
    assert ls.audio_tracks(LIVE_PROPS) == [(0, 'spa', False), (1, 'eng', True), (2, 'es-[ads]', True)]


def test_audio_tracks_warner_replay():
    assert ls.audio_tracks(REPLAY_PROPS) == [(0, 'spa', False), (1, 'eng', False), (2, 'es-[ads]', False)]


def test_audio_tracks_codes_match_live_and_replay():
    """The measurement the design rests on: the same codes, at the same
    numbers, live and in a replay -- only the flags differ."""
    def codes(props):
        return [(index, code) for index, code, _ in ls.audio_tracks(props)]
    assert codes(LIVE_PROPS) == codes(REPLAY_PROPS)
    assert [imp for _, _, imp in ls.audio_tracks(LIVE_PROPS)] != [imp for _, _, imp in ls.audio_tracks(REPLAY_PROPS)]


def test_audio_tracks_keeps_kodis_order():
    """Kodi's list order, not sorted by number: audio_track's "first" is Kodi's first."""
    tracks = (_stream(2, 'es-[ADS]', False, True), _stream(0, 'spa', True, False), _stream(1, 'eng', False, True))
    assert [index for index, _, _ in ls.audio_tracks(_props(tracks))] == [2, 0, 1]


@pytest.mark.parametrize('index', [True, False, -1, -7, 1.0, 0.0, '1', '0', None, _MISSING, [1], {'i': 1}], ids=repr)
def test_audio_tracks_drops_a_track_without_a_usable_index(index):
    """A track number is a non-negative int -- not a bool (True == 1), a
    float or text: setAudioStream takes the int Kodi listed."""
    bad = {'language': 'cat', 'isimpaired': False}
    if index is not _MISSING:
        bad['index'] = index
    props = _props(NEOX_LIVE_TRACKS + (bad,))
    assert ls.audio_tracks(props) == [(0, 'spa', False), (1, 'eng', True)]


def test_audio_tracks_keeps_track_zero():
    """0 is a track number (Neox live's Spanish), not "nothing"."""
    out = ls.audio_tracks(_props(SPANISH_ONLY_TRACKS))
    assert out == [(0, 'spa', False)]
    assert type(out[0][0]) is int


@pytest.mark.parametrize('language', [_MISSING, None, '', '   ', '\t', 1, ['eng'], b'eng', {'code': 'eng'}], ids=repr)
def test_audio_tracks_drops_a_track_without_a_language(language):
    """Nothing to match a track without a language by."""
    bad = {'index': 5, 'isimpaired': False}
    if language is not _MISSING:
        bad['language'] = language
    props = _props((bad,) + NEOX_LIVE_TRACKS)
    assert ls.audio_tracks(props) == [(0, 'spa', False), (1, 'eng', True)]


@pytest.mark.parametrize('entry', [None, 'eng', 3, ['eng', 1], ('spa', 0), True], ids=repr)
def test_audio_tracks_drops_entries_that_are_not_tracks(entry):
    props = {'audiostreams': [entry] + [dict(t) for t in NEOX_LIVE_TRACKS] + [entry]}
    assert ls.audio_tracks(props) == [(0, 'spa', False), (1, 'eng', True)]


@pytest.mark.parametrize('listing', [None, _MISSING, 'audiostreams', 5, True, {'index': 0, 'language': 'spa'}],
                         ids=repr)
def test_audio_tracks_a_listing_that_is_not_a_list_gives_none(listing):
    props = {'currentaudiostream': dict(WARNER_LIVE_TRACKS[0])}
    if listing is not _MISSING:
        props['audiostreams'] = listing
    assert ls.audio_tracks(props) == []


def test_audio_tracks_takes_a_tuple_listing():
    props = {'audiostreams': tuple(dict(t) for t in NEOX_LIVE_TRACKS)}
    assert ls.audio_tracks(props) == [(0, 'spa', False), (1, 'eng', True)]


@pytest.mark.parametrize('props', [None, {}, [], 'audiostreams', 0, list(NEOX_LIVE_TRACKS)], ids=repr)
def test_audio_tracks_without_a_result_gives_none(props):
    """An error response has no 'result'; a bare list isn't Kodi's answer."""
    assert ls.audio_tracks(props) == []


@pytest.mark.parametrize('flag, impaired', [
    (_MISSING, False), (None, False), (False, False), (0, False), ('', False),
    (True, True), (1, True), ('yes', True),
], ids=repr)
def test_audio_tracks_impaired_flag_reads_as_a_bool(flag, impaired):
    entry = {'index': 3, 'language': 'eng'}
    if flag is not _MISSING:
        entry['isimpaired'] = flag
    (out,) = ls.audio_tracks({'audiostreams': [entry]})
    assert out[2] is impaired


def test_audio_tracks_reads_kodis_answer():
    """Kodi's JSON answer for Warner TV live, as JSON-RPC sends it."""
    resp = json.loads(
        '{"id": 1, "jsonrpc": "2.0", "result": {'
        '"currentaudiostream": {"index": 1, "language": "eng", "name": "", "isdefault": false, "isimpaired": true}, '
        '"audiostreams": ['
        '{"index": 0, "language": "spa", "name": "", "isdefault": true, "isimpaired": false}, '
        '{"index": 1, "language": "eng", "name": "", "isdefault": false, "isimpaired": true}, '
        '{"index": 2, "language": "es-[ADS]", "name": "", "isdefault": false, "isimpaired": true}]}}')
    assert ls.audio_tracks(resp.get('result')) == ls.audio_tracks(LIVE_PROPS)


def test_audio_tracks_leaves_its_input_alone():
    props  = _props(WARNER_LIVE_TRACKS + ({'index': True, 'language': 'cat'},), {'index': 1})
    before = copy.deepcopy(props)
    ls.audio_tracks(props)
    assert props == before


# ================================================================ audio_track


@pytest.mark.parametrize('where', sorted(['live', 'replay']))
@pytest.mark.parametrize('lang, index', [('spa', 0), ('eng', 1), ('es-[ADS]', 2)])
def test_audio_track_by_exact_code_live_and_replay(where, lang, index):
    """The same language is the same track number live and in a replay."""
    assert _is_index(ls.audio_track(PROPS[where], lang), index)


@pytest.mark.parametrize('lang', ['ENG', 'Eng', ' eng ', '\tENG\n'], ids=repr)
def test_audio_track_ignores_case_and_spaces_of_the_language(lang):
    assert _is_index(ls.audio_track(LIVE_PROPS, lang), 1)


def test_audio_track_ignores_case_and_spaces_of_the_track():
    props = _props((_stream(4, ' SPA ', True, False), _stream(7, 'ENG', True, False),
                    _stream(9, 'ES-[ads]', True, False)))
    assert _is_index(ls.audio_track(props, 'spa'), 4)
    assert _is_index(ls.audio_track(props, 'eng'), 7)
    assert _is_index(ls.audio_track(props, 'es-[ADS]'), 9)


def test_audio_track_an_impaired_only_language_still_matches():
    """Live English is labelled impaired and is the only English: it is the
    English track (normal English, the user confirmed)."""
    assert _is_index(ls.audio_track(LIVE_PROPS, 'eng'), 1)
    assert _is_index(ls.audio_track(_props(NEOX_LIVE_TRACKS), 'eng'), 1)


@pytest.mark.parametrize('tracks, index', [
    pytest.param((_stream(1, 'eng', False, True), _stream(3, 'eng', False, False)), 3, id='impaired-first'),
    pytest.param((_stream(3, 'eng', False, False), _stream(1, 'eng', False, True)), 3, id='plain-first'),
    pytest.param((_stream(0, 'spa', True, False), _stream(1, 'eng', False, True), _stream(5, 'ENG', True, False)), 5,
                 id='plain-after-impaired-other-case'),
    pytest.param((_stream(6, 'eng', False, True), _stream(2, 'eng', False, True)), 6,
                 id='only-impaired-first-in-order'),
    pytest.param((_stream(6, 'eng', False, False), _stream(2, 'eng', False, False)), 6, id='only-plain-first-in-order'),
])
def test_audio_track_a_track_not_labelled_impaired_wins_a_tie(tracks, index):
    """Several tracks in one language: the first one not labelled impaired,
    else Kodi's first."""
    assert _is_index(ls.audio_track(_props(tracks), 'eng'), index)


@pytest.mark.parametrize('where', ['live', 'replay', 'ads-first'])
def test_audio_track_the_audio_description_is_never_spanish_and_vice_versa(where):
    """'es-[ADS]' and 'spa' are different codes: no prefix or partial match,
    whatever the flags or the order."""
    props = PROPS.get(where) or _props((WARNER_LIVE_TRACKS[2], WARNER_LIVE_TRACKS[0], WARNER_LIVE_TRACKS[1]))
    assert _is_index(ls.audio_track(props, 'spa'), 0)
    assert _is_index(ls.audio_track(props, 'es-[ADS]'), 2)
    only_ads = _props((WARNER_LIVE_TRACKS[2],))
    assert ls.audio_track(only_ads, 'spa') is None
    assert ls.audio_track(_props(SPANISH_ONLY_TRACKS), 'es-[ADS]') is None


@pytest.mark.parametrize('lang', ['es', 'en', 'sp', 'spa ads', '[ADS]', 'es-', 'es-[ADS', 'english', 'spanish'])
def test_audio_track_no_partial_matches(lang):
    assert ls.audio_track(LIVE_PROPS, lang) is None


@pytest.mark.parametrize('where', sorted(PROPS))
@pytest.mark.parametrize('lang', ['fre', 'cat', 'ita'])
def test_audio_track_absent_language_is_none(where, lang):
    assert ls.audio_track(PROPS[where], lang) is None


def test_audio_track_english_absent_from_a_spanish_only_show():
    assert ls.audio_track(SPANISH_PROPS, 'eng') is None
    assert _is_index(ls.audio_track(SPANISH_PROPS, 'spa'), 0)


@pytest.mark.parametrize('where', sorted(PROPS))
@pytest.mark.parametrize('lang', [None, '', '   ', 1, True, b'eng', ['eng']], ids=repr)
def test_audio_track_without_a_language_is_none(where, lang):
    assert ls.audio_track(PROPS[where], lang) is None


@pytest.mark.parametrize('props', [None, {}, _props(()), {'audiostreams': None}, [], 'eng'], ids=repr)
def test_audio_track_without_tracks_is_none(props):
    assert ls.audio_track(props, 'eng') is None


def test_audio_track_ignores_unusable_tracks():
    props = _props(({'index': True, 'language': 'eng'}, {'index': -1, 'language': 'eng'},
                    {'index': 1.0, 'language': 'eng'}, {'index': 4}, _stream(8, 'eng', False, True)))
    assert _is_index(ls.audio_track(props, 'eng'), 8)


# ================================================================ audio_restore_action

# saved -> playing -> expected track for (live, replay, spanish-only); None = leave it.
_RESTORE = {
    'spa': {
        '':         (0, 0, 0),             # nothing reads as playing: switch to the saved Spanish
        'spa':      (None, None, None),    # already Spanish
        'eng':      (0, 0, 0),
        'es-[ADS]': (0, 0, 0),             # the description playing: Spanish, never the description again
    },
    'eng': {
        '':         (1, 1, None),
        'spa':      (1, 1, None),          # live English is the impaired-labelled track: still the one
        'eng':      (None, None, None),
        'es-[ADS]': (1, 1, None),
    },
    'es-[ADS]': {                          # picked once: the description is restored like any language
        '':         (2, 2, None),
        'spa':      (2, 2, None),
        'eng':      (2, 2, None),
        'es-[ADS]': (None, None, None),
    },
}
_WHERE = ('live', 'replay', 'spanish-only')
RESTORE_TABLE = (
    [pytest.param(saved, playing, where, expected[n], id='%s|%s|%s' % (saved, playing or '-', where))
     for saved, by_playing in sorted(_RESTORE.items())
     for playing, expected in sorted(by_playing.items())
     for n, where in enumerate(_WHERE)]
    # nothing remembered: Kodi's pick stays
    + [pytest.param(None, playing, where, None, id='none|%s|%s' % (playing or '-', where))
       for playing in ('',) + LANGS for where in _WHERE]
)


@pytest.mark.parametrize('saved, playing, where, expected', RESTORE_TABLE)
def test_audio_restore_action_table(saved, playing, where, expected):
    out = ls.audio_restore_action(saved, playing, PROPS[where])
    if expected is None:
        assert out is None
    else:
        assert _is_index(out, expected)


def test_audio_restore_table_is_complete():
    rows = [p.values[:3] for p in RESTORE_TABLE]
    assert len(rows) == len(set(rows)) == 48
    assert set(rows) == set(itertools.product((None,) + LANGS, ('',) + LANGS, _WHERE))


@pytest.mark.parametrize('saved', [None, '', '   ', 1, True, b'eng', ['eng']], ids=repr)
@pytest.mark.parametrize('playing', ['', 'spa', 'eng'])
def test_audio_restore_action_nothing_saved_leaves_kodis_pick(saved, playing):
    for props in PROPS.values():
        assert ls.audio_restore_action(saved, playing, props) is None


@pytest.mark.parametrize('saved, playing', [
    ('eng', 'ENG'), ('ENG', 'eng'), (' eng ', 'eng'), ('eng', '\teng\n'), ('es-[ADS]', 'ES-[ads]'),
], ids=repr)
def test_audio_restore_action_no_switch_when_the_language_already_plays(saved, playing):
    """Same language, whatever the case or spaces: no switch (no audio blip)."""
    for props in PROPS.values():
        assert ls.audio_restore_action(saved, playing, props) is None


@pytest.mark.parametrize('playing', [None, 0, b'spa', ['spa']], ids=repr)
def test_audio_restore_action_unreadable_playing_language_switches_to_the_saved_one(playing):
    """VideoPlayer.AudioLanguage empty or unusable: the saved track is
    selected (selecting the playing track again changes nothing)."""
    assert _is_index(ls.audio_restore_action('eng', playing, LIVE_PROPS), 1)


def test_audio_restore_action_saved_spelling_does_not_matter():
    assert _is_index(ls.audio_restore_action(' ENG ', 'spa', LIVE_PROPS), 1)
    assert _is_index(ls.audio_restore_action('Es-[Ads]', 'spa', REPLAY_PROPS), 2)


def test_audio_restore_action_prefers_a_track_not_labelled_impaired():
    props = _props((_stream(0, 'spa', True, False), _stream(1, 'eng', False, True), _stream(3, 'eng', False, False)))
    assert _is_index(ls.audio_restore_action('eng', 'spa', props), 3)


@pytest.mark.parametrize('props', [None, {}, _props(()), {'audiostreams': 'x'}], ids=repr)
def test_audio_restore_action_without_tracks_is_none(props):
    assert ls.audio_restore_action('eng', 'spa', props) is None


@pytest.mark.parametrize('saved, playing, where', list(itertools.product(LANGS, ('',) + LANGS, _WHERE)))
def test_audio_restore_action_only_ever_selects_the_saved_language(saved, playing, where):
    """A switch is always to a track in the remembered language, and never to
    the language already playing."""
    out = ls.audio_restore_action(saved, playing, PROPS[where])
    if out is not None:
        assert _code_at(PROPS[where], out) == ls.lang_code(saved)
        assert ls.lang_code(saved) != ls.lang_code(playing)


def test_audio_restore_action_leaves_its_input_alone():
    props  = copy.deepcopy(LIVE_PROPS)
    ls.audio_restore_action('eng', 'spa', props)
    assert props == LIVE_PROPS


# ================================================================ audio_settle_record

# (saved, playing, saved_offered) -> the language to remember, or None.
SETTLE_TABLE = [
    # nothing playing (no language): nothing to remember
    (None,  '',    True,  None),
    (None,  '',    False, None),
    ('eng', '',    True,  None),
    ('eng', '',    False, None),
    # nothing remembered yet: the playing language, whether Kodi or you picked it
    (None,  'eng', True,  'eng'),
    (None,  'eng', False, 'eng'),
    (None,  'spa', True,  'spa'),
    (None,  'spa', False, 'spa'),
    # the remembered language plays: nothing new
    ('eng', 'eng', True,  None),
    ('eng', 'eng', False, None),
    # the remembered language is offered but another plays (the restore
    # didn't win, or you switched during the start-up): the playing one
    ('eng', 'spa', True,  'spa'),
    # the remembered language isn't offered (a Spanish-only show): kept
    ('eng', 'spa', False, None),
]


@pytest.mark.parametrize('saved, playing, saved_offered, expected', SETTLE_TABLE)
def test_audio_settle_record_table(saved, playing, saved_offered, expected):
    assert ls.audio_settle_record(saved, playing, saved_offered) == expected


def test_audio_settle_table_is_complete():
    rows = [row[:3] for row in SETTLE_TABLE]
    assert len(rows) == len(set(rows)) == 12
    assert set(rows) == set(itertools.product((None, 'eng'), ('', 'eng', 'spa'), BOOLS))


@pytest.mark.parametrize('saved', ['', '   ', 1, True, b'eng', ['eng']], ids=repr)
def test_audio_settle_record_an_unusable_saved_value_reads_as_nothing_saved(saved):
    """valid_audio_memory never gives one, but the rule holds: Kodi's pick is seeded."""
    for playing, offered in itertools.product(('', 'eng', 'spa'), BOOLS):
        assert ls.audio_settle_record(saved, playing, offered) == ls.audio_settle_record(None, playing, offered)


@pytest.mark.parametrize('playing', [None, '', '   ', 0, True, b'eng', ['eng']], ids=repr)
@pytest.mark.parametrize('saved', [None, 'eng', 'spa'])
def test_audio_settle_record_nothing_playing_records_nothing(saved, playing):
    for offered in BOOLS:
        assert ls.audio_settle_record(saved, playing, offered) is None


@pytest.mark.parametrize('saved, playing', [
    ('eng', 'ENG'), ('ENG', 'eng'), (' eng', 'eng '), ('es-[ADS]', 'ES-[ads]'),
], ids=repr)
def test_audio_settle_record_same_language_whatever_the_spelling(saved, playing):
    for offered in BOOLS:
        assert ls.audio_settle_record(saved, playing, offered) is None


@pytest.mark.parametrize('saved, playing, stored', [
    (None,  ' ENG\n',      'ENG'),
    (None,  'es-[ADS]',    'es-[ADS]'),
    ('eng', ' spa ',       'spa'),
    ('spa', ' es-[ADS] ',  'es-[ADS]'),
], ids=repr)
def test_audio_settle_record_gives_the_playing_language_stripped_in_kodis_spelling(saved, playing, stored):
    assert ls.audio_settle_record(saved, playing, True) == stored


@pytest.mark.parametrize('offered, expected', [
    (True, 'spa'), (1, 'spa'), ('yes', 'spa'), ([0], 'spa'),
    (False, None), (0, None), (None, None), ('', None), ([], None),
], ids=repr)
def test_audio_settle_record_reads_saved_offered_as_a_truth_value(offered, expected):
    assert ls.audio_settle_record('eng', 'spa', offered) == expected


@pytest.mark.parametrize('saved', ['eng', 'es-[ADS]'])
def test_a_spanish_only_show_never_replaces_the_remembered_language(saved):
    """Chained the way services.py does: `saved_offered` is whether the stream
    offers the remembered language. A Spanish-only show doesn't, so Spanish
    is not remembered for good."""
    offered = ls.audio_track(SPANISH_PROPS, saved) is not None
    assert offered is False
    assert ls.audio_settle_record(saved, 'spa', offered) is None


@pytest.mark.parametrize('saved, playing, saved_offered',
                         list(itertools.product((None, '') + LANGS, ('', '  ') + LANGS + ('ENG ',), BOOLS)))
def test_audio_settle_record_only_ever_records_the_playing_language(saved, playing, saved_offered):
    """Never a third language, never the one already remembered: the memory
    file is written only on a change."""
    out = ls.audio_settle_record(saved, playing, saved_offered)
    if out is not None:
        assert isinstance(out, str) and out == out.strip() and out
        assert ls.lang_code(out) == ls.lang_code(playing)
        assert ls.lang_code(out) != ls.lang_code(saved)


# ================================================================ audio_change_record

# (playing, baseline, baseline_offered) -> (language to remember or None, forced).
# `baseline` is the language the playback settled on (or last changed to);
# `baseline_offered` whether the stream still offers it.
CHANGE_TABLE = [
    # nothing playing (no language): no change
    ('',    '',    True,  (None, False)),
    ('',    '',    False, (None, False)),
    ('',    'eng', True,  (None, False)),
    ('',    'eng', False, (None, False)),
    ('',    'spa', True,  (None, False)),
    ('',    'spa', False, (None, False)),
    # the same language: no change
    ('eng', 'eng', True,  (None, False)),
    ('eng', 'eng', False, (None, False)),
    ('spa', 'spa', True,  (None, False)),
    ('spa', 'spa', False, (None, False)),
    # a switch while the previous language is still offered: yours, remembered
    ('spa', 'eng', True,  ('spa', False)),
    ('eng', 'spa', True,  ('eng', False)),
    # the previous language disappeared (a Spanish-only show on live): forced
    ('spa', 'eng', False, (None, True)),
    ('eng', 'spa', False, (None, True)),
    # the playback settled with no language: whether that counts as offered is
    # the caller's (audio_track never offers '', so services.py's own check
    # makes it forced -- nothing recorded, and the remembered language is
    # switched back to through audio_return_action)
    ('eng', '',    True,  ('eng', False)),
    ('eng', '',    False, (None, True)),
    ('spa', '',    True,  ('spa', False)),
    ('spa', '',    False, (None, True)),
]


@pytest.mark.parametrize('playing, baseline, baseline_offered, expected', CHANGE_TABLE)
def test_audio_change_record_table(playing, baseline, baseline_offered, expected):
    assert ls.audio_change_record(playing, baseline, baseline_offered) == expected


def test_audio_change_table_is_complete():
    rows = [row[:3] for row in CHANGE_TABLE]
    assert len(rows) == len(set(rows)) == 18
    assert set(rows) == set(itertools.product(('', 'eng', 'spa'), ('', 'eng', 'spa'), BOOLS))


def test_audio_change_record_a_switch_on_live_is_remembered():
    """Neox live in English, you pick Spanish: English is still offered, so
    it was your switch."""
    offered = ls.audio_track(_props(NEOX_LIVE_TRACKS), 'eng') is not None
    assert ls.audio_change_record('spa', 'eng', offered) == ('spa', False)


def test_audio_change_record_a_spanish_only_show_is_forced():
    """Neox live in English, the next show is Spanish-only and Kodi falls back
    to Spanish: English is gone, so the change is forced and not remembered."""
    offered = ls.audio_track(SPANISH_PROPS, 'eng') is not None
    assert ls.audio_change_record('spa', 'eng', offered) == (None, True)


def test_audio_change_record_the_audio_description_is_a_choice_like_any_other():
    """Picking the description on Warner TV live is remembered (Spanish is
    still offered); losing it to a show without one is forced."""
    assert ls.audio_change_record('es-[ADS]', 'spa', ls.audio_track(LIVE_PROPS, 'spa') is not None) == \
        ('es-[ADS]', False)
    assert ls.audio_change_record('spa', 'es-[ADS]', ls.audio_track(SPANISH_PROPS, 'es-[ADS]') is not None) == \
        (None, True)


@pytest.mark.parametrize('playing, baseline', [
    ('ENG', 'eng'), ('eng', 'ENG'), (' eng ', 'eng'), ('es-[ADS]', 'ES-[ads]'),
], ids=repr)
def test_audio_change_record_same_language_whatever_the_spelling(playing, baseline):
    for offered in BOOLS:
        assert ls.audio_change_record(playing, baseline, offered) == (None, False)


@pytest.mark.parametrize('playing', [None, '   ', 0, True, b'spa', ['spa']], ids=repr)
def test_audio_change_record_unusable_playing_language_is_no_change(playing):
    """VideoPlayer.AudioLanguage briefly empty (a stream switch): never a
    record, never forced."""
    for baseline, offered in itertools.product(('', 'eng', 'spa'), BOOLS):
        assert ls.audio_change_record(playing, baseline, offered) == (None, False)


@pytest.mark.parametrize('playing, stored', [(' SPA ', 'SPA'), ('es-[ADS]\n', 'es-[ADS]')], ids=repr)
def test_audio_change_record_gives_the_playing_language_stripped_in_kodis_spelling(playing, stored):
    assert ls.audio_change_record(playing, 'eng', True) == (stored, False)


@pytest.mark.parametrize('offered, expected', [
    (True, ('spa', False)), (1, ('spa', False)), ('yes', ('spa', False)),
    (False, (None, True)), (0, (None, True)), (None, (None, True)), ('', (None, True)),
], ids=repr)
def test_audio_change_record_reads_baseline_offered_as_a_truth_value(offered, expected):
    assert ls.audio_change_record('spa', 'eng', offered) == expected


@pytest.mark.parametrize('playing, baseline, baseline_offered',
                         list(itertools.product(('', ' ') + LANGS + ('ENG',), ('', ' ') + LANGS, BOOLS)))
def test_audio_change_record_shape_and_invariants(playing, baseline, baseline_offered):
    """Always a pair; forced is a real bool; a forced change records nothing;
    a record is the playing language and never the unchanged baseline."""
    out = ls.audio_change_record(playing, baseline, baseline_offered)
    assert isinstance(out, tuple) and len(out) == 2
    record, forced = out
    assert forced is True or forced is False
    if forced:
        assert record is None and not baseline_offered
    if record is not None:
        assert record == record.strip() and record
        assert ls.lang_code(record) == ls.lang_code(playing) != ls.lang_code(baseline)


# ================================================================ audio_return_action


def test_audio_return_action_stays_while_the_language_is_not_offered():
    """Forced to Spanish by a Spanish-only show: nothing to switch back to yet."""
    assert ls.audio_return_action('eng', 'spa', SPANISH_PROPS) is None


def test_audio_return_action_switches_back_when_the_next_show_offers_it():
    """The next show on Neox live offers English again (the impaired-labelled
    track): back to it."""
    assert _is_index(ls.audio_return_action('eng', 'spa', _props(NEOX_LIVE_TRACKS)), 1)
    assert _is_index(ls.audio_return_action('es-[ADS]', 'spa', LIVE_PROPS), 2)


def test_audio_return_action_nothing_when_already_back():
    """Kodi (or you) went back to it already: no switch."""
    assert ls.audio_return_action('eng', 'eng', _props(NEOX_LIVE_TRACKS)) is None
    assert ls.audio_return_action('eng', ' ENG ', LIVE_PROPS) is None


@pytest.mark.parametrize('saved', [None, '', '  '], ids=repr)
def test_audio_return_action_nothing_remembered_nothing_to_return_to(saved):
    assert ls.audio_return_action(saved, 'spa', LIVE_PROPS) is None


@pytest.mark.parametrize('saved, playing, where',
                         list(itertools.product((None, '') + LANGS, ('',) + LANGS, _WHERE)))
def test_audio_return_action_is_the_restore_rule(saved, playing, where):
    """Switching back after a forced change is the same decision as the
    restore at the start: the remembered language, when offered and not
    already playing."""
    assert (ls.audio_return_action(saved, playing, PROPS[where])
            == ls.audio_restore_action(saved, playing, PROPS[where]))


# ================================================================ remember_audio


def test_remember_audio_is_copy_on_write():
    """The play thread may hold the old dict while the monitor thread
    remembers a language: the old dict never changes; the new one is swapped
    in whole (no lock needed)."""
    memory = {WARNER: 'eng'}
    reader = memory
    new = ls.remember_audio(memory, NEOX, 'spa')
    assert new == {WARNER: 'eng', NEOX: 'spa'}
    assert new is not memory
    assert memory == {WARNER: 'eng'} and reader == {WARNER: 'eng'}
    newer = ls.remember_audio(new, NEOX, 'eng')
    assert newer == {WARNER: 'eng', NEOX: 'eng'}
    assert new == {WARNER: 'eng', NEOX: 'spa'}


@pytest.mark.parametrize('memory', [None, {}], ids=['none', 'empty'])
def test_remember_audio_starts_an_empty_memory(memory):
    assert ls.remember_audio(memory, NEOX, 'eng') == {NEOX: 'eng'}


@pytest.mark.parametrize('lang, stored', [
    ('eng', 'eng'), (' eng ', 'eng'), ('\tspa\n', 'spa'), ('ENG', 'ENG'), (' es-[ADS] ', 'es-[ADS]'),
], ids=repr)
def test_remember_audio_strips_and_keeps_kodis_spelling(lang, stored):
    assert ls.remember_audio({}, NEOX, lang)[NEOX] == stored


def test_remember_audio_keeps_the_other_channels():
    memory = dict(AUDIO_GOOD)
    out = ls.remember_audio(memory, CANAL_SUR, 'spa')
    assert out == dict(AUDIO_GOOD, **{CANAL_SUR: 'spa'})
    assert memory == AUDIO_GOOD


def test_remember_audio_result_reads_back_unchanged():
    memory = ls.remember_audio(ls.remember_audio({}, NEOX, ' eng '), CANAL_SUR, 'es-[ADS]')
    assert ls.valid_audio_memory(json.loads(json.dumps(memory))) == memory == {NEOX: 'eng', CANAL_SUR: 'es-[ADS]'}


@pytest.mark.parametrize('saved, playing, offered', list(itertools.product((None,) + LANGS, LANGS + (' ENG ',), BOOLS)))
def test_every_record_survives_the_file(saved, playing, offered):
    """Whatever the settle or change rule hands remember_audio is a value the
    memory file gives back unchanged after a restart."""
    records = [ls.audio_settle_record(saved, playing, offered),
               ls.audio_change_record(playing, saved or '', offered)[0]]
    for record in records:
        if record is None:
            continue
        memory = ls.remember_audio({WARNER: 'eng'}, NEOX, record)
        assert ls.valid_audio_memory(json.loads(json.dumps(memory))) == memory


# ================================================================ restore + record together


@pytest.mark.parametrize('saved, playing, where', list(itertools.product(LANGS, LANGS, _WHERE)))
def test_a_restore_is_never_recorded_back_as_a_change(saved, playing, where):
    """After the restore (or none), settling finds nothing new to remember
    when the switch landed, and the first tick sees no change: the add-on's
    own switch is never taken for yours."""
    props  = PROPS[where]
    index  = ls.audio_restore_action(saved, playing, props)
    after  = playing if index is None else _code_at(props, index)
    offered = ls.audio_track(props, saved) is not None
    if index is not None:
        assert ls.audio_settle_record(saved, after, offered) is None
    assert ls.audio_change_record(after, after, True) == (None, False)


@pytest.mark.parametrize('saved, playing, where', list(itertools.product(LANGS, LANGS, _WHERE)))
def test_settle_records_only_when_the_restore_could_not_apply_or_nothing_was_saved(saved, playing, where):
    """Kodi starts on `playing`. When the stream offers the remembered
    language the restore switches to it and settling records nothing; when it
    doesn't, nothing is switched and nothing recorded either."""
    props   = PROPS[where]
    offered = ls.audio_track(props, saved) is not None
    index   = ls.audio_restore_action(saved, playing, props)
    if offered and ls.lang_code(saved) != ls.lang_code(playing):
        assert index is not None
    if not offered:
        assert index is None
        assert ls.audio_settle_record(saved, playing, offered) is None


# ================================================================ the Neox / Warner TV cases, end to end

NEOX_LIVE   = 'pvr://channels/tv/All channels/pvr.iptvsimple_8.pvr'
WARNER_LIVE = 'pvr://channels/tv/All channels/pvr.iptvsimple_12.pvr'


def _replay(epg, start):
    return 'pvr://guide/%04d/2026-11-11 %s:00.epg' % (epg, start)


class _Kodi(object):
    """Kodi's audio side as measured on 2026-11-11: video settings are kept
    per video (a live channel is one video, every replay a new one) and
    stored when the video stops -- the audio setting as the track number.
    A video with no stored setting starts in the preferred audio language
    (English on this TV), else on its first track. On live, the next show
    changes the tracks inside the same video: Kodi keeps the playing
    language when the new show offers it, else falls back to the first
    track, and doesn't go back by itself when the language returns.
    `reverts` counts switches Kodi undoes (it put its own choice back), the
    case the 1 s re-check exists for."""

    PREFERRED = 'eng'

    def __init__(self, stored=None):
        self.stored   = dict(stored or {})      # video -> track number (Kodi's per-video settings)
        self.video    = None
        self.tracks   = []
        self.current  = None
        self.reverts  = 0
        self.switches = 0

    def _index_of(self, lang):
        return next((t['index'] for t in self.tracks if t['language'] == lang), None)

    def _first(self):
        return self.tracks[0]['index'] if self.tracks else None

    def open(self, video, tracks):
        if self.video is not None and self.current is not None:
            self.stored[self.video] = self.current                      # StoreVideoSettings
        self.video  = video
        self.tracks = [dict(t) for t in tracks]
        stored = self.stored.get(video)
        if stored is not None and any(t['index'] == stored for t in self.tracks):
            self.current = stored                                       # RequestVideoSettings
        else:
            index = self._index_of(self.PREFERRED)
            self.current = self._first() if index is None else index

    def next_show(self, tracks):
        """Live only: the next show in the same stream."""
        playing = self.language
        self.tracks = [dict(t) for t in tracks]
        index = self._index_of(playing)
        self.current = self._first() if index is None else index

    @property
    def language(self):                                                 # VideoPlayer.AudioLanguage
        return next((t['language'] for t in self.tracks if t['index'] == self.current), '')

    def properties(self):                                               # the JSON-RPC 'result'
        current = next((dict(t) for t in self.tracks if t['index'] == self.current), None)
        return {'audiostreams': [dict(t) for t in self.tracks], 'currentaudiostream': current}

    def setAudioStream(self, index):
        self.switches += 1
        if self.reverts:
            self.reverts -= 1
            return
        self.current = index

    def remote(self, lang):
        """You, with the OSD audio menu."""
        self.current = self._index_of(lang)


class _AddOn(object):
    """The helper calls as the service makes them (imports.67's machinery):
    at the start of a LiveTV playback the restore switches to the channel's
    language and re-checks once; then the playback settles and the settle
    record runs. Afterwards a tick about once a second either switches back
    after a forced change or records a change of VideoPlayer.AudioLanguage.
    The memory is written (here: dumped to text) only when it changes."""

    def __init__(self, stored):
        self.memory   = ls.valid_audio_memory(stored)
        self.writes   = []
        self.key      = None
        self.baseline = ''
        self.forced   = False

    def _save(self, lang):
        if ls.lang_code(lang) == ls.lang_code(self.memory.get(self.key)):
            return
        self.memory = ls.remember_audio(self.memory, self.key, lang)
        self.writes.append(json.dumps(self.memory, sort_keys=True))

    def started(self, kodi, key):
        self.key, self.forced = key, False
        saved = self.memory.get(key)
        index = ls.audio_restore_action(saved, kodi.language, kodi.properties())
        if index is not None:
            kodi.setAudioStream(index)
            again = ls.audio_restore_action(saved, kodi.language, kodi.properties())     # the 1 s re-check
            if again is not None:
                kodi.setAudioStream(again)
        offered = ls.audio_track(kodi.properties(), saved) is not None
        record  = ls.audio_settle_record(saved, kodi.language, offered)
        if record is not None:
            self._save(record)
        self.baseline = kodi.language
        return index

    def tick(self, kodi):
        saved = self.memory.get(self.key)
        if self.forced:
            index = ls.audio_return_action(saved, kodi.language, kodi.properties())
            if index is not None:
                kodi.setAudioStream(index)
                self.forced, self.baseline = False, kodi.language
                return 'switched back', index
        offered = ls.audio_track(kodi.properties(), self.baseline) is not None
        record, forced = ls.audio_change_record(kodi.language, self.baseline, offered)
        self.baseline = kodi.language or self.baseline
        if forced:
            self.forced = True
            return 'forced', kodi.language
        if record is not None:
            self.forced = False
            self._save(record)
            return 'remembered', record
        return None


def test_neox_audio_follows_the_channel_live_and_in_replays():
    """The 2026-11-11 case: Kodi's stored row for Neox live is Spanish (track
    0), and every replay starts in English (no stored row: the preferred
    language)."""
    kodi = _Kodi({NEOX_LIVE: 0})
    tv   = _AddOn(None)                 # first start: no memory file yet

    # 1. A Neox replay starts in English (Kodi's pick). Nothing remembered
    #    yet: Kodi's pick counts, so 'eng' is seeded.
    kodi.open(_replay(90, '18:00'), NEOX_REPLAY_TRACKS)
    assert kodi.language == 'eng'
    assert tv.started(kodi, NEOX) is None
    assert tv.memory == {NEOX: 'eng'} and len(tv.writes) == 1
    assert tv.tick(kodi) is None

    # 2. Neox live: Kodi's stored row says Spanish; the add-on switches to
    #    English -- on live, the track labelled impaired (normal English).
    kodi.open(NEOX_LIVE, NEOX_LIVE_TRACKS)
    assert kodi.language == 'spa'
    assert tv.started(kodi, NEOX) == 1
    assert kodi.language == 'eng' and kodi.switches == 1
    assert tv.tick(kodi) is None
    assert len(tv.writes) == 1          # the add-on's own switch is not a choice of yours

    # 3. The next show on live is Spanish-only: Kodi has to fall back to
    #    Spanish. Forced -- not remembered.
    kodi.next_show(SPANISH_ONLY_TRACKS)
    assert kodi.language == 'spa'
    assert tv.tick(kodi) == ('forced', 'spa')
    assert tv.forced and tv.memory == {NEOX: 'eng'} and len(tv.writes) == 1
    assert tv.tick(kodi) is None        # still Spanish-only: nothing to switch back to

    # 4. The show after it offers English again; Kodi stays on Spanish, the
    #    add-on switches back to English.
    kodi.next_show(NEOX_LIVE_TRACKS)
    assert kodi.language == 'spa'
    assert tv.tick(kodi) == ('switched back', 1)
    assert kodi.language == 'eng' and not tv.forced
    assert tv.tick(kodi) is None
    assert tv.memory == {NEOX: 'eng'} and len(tv.writes) == 1

    # 5. You switch to Spanish on live: English is still offered, so it's
    #    your choice -- 'spa' is remembered.
    kodi.remote('spa')
    assert tv.tick(kodi) == ('remembered', 'spa')
    assert tv.memory == {NEOX: 'spa'} and len(tv.writes) == 2
    assert tv.tick(kodi) is None

    # 6. The next Neox replay starts in English (Kodi's pick for a new video);
    #    the add-on restores Spanish.
    kodi.open(_replay(91, '19:00'), NEOX_REPLAY_TRACKS)
    assert kodi.language == 'eng'
    assert tv.started(kodi, NEOX) == 0
    assert kodi.language == 'spa'
    assert tv.tick(kodi) is None

    # 7. Kodi restarts: the memory comes back from the file.
    assert json.loads(tv.writes[-1]) == {NEOX: 'spa'}
    tv = _AddOn(json.loads(tv.writes[-1]))
    assert tv.memory == {NEOX: 'spa'}
    kodi.open(_replay(92, '20:00'), NEOX_REPLAY_TRACKS)
    assert tv.started(kodi, NEOX) == 0
    assert kodi.language == 'spa' and tv.writes == []


def test_warner_tv_stays_english_and_the_audio_description_only_when_picked():
    """Warner TV: Kodi's stored live row is English (track 1). It stays in
    English live and in replays (TV check 3), never drifts to the Spanish
    audio description, and Neox's memory is its own."""
    kodi = _Kodi({WARNER_LIVE: 1})
    tv   = _AddOn({NEOX: 'spa'})

    kodi.open(WARNER_LIVE, WARNER_LIVE_TRACKS)
    assert tv.started(kodi, WARNER) is None
    assert kodi.language == 'eng'
    assert tv.memory == {NEOX: 'spa', WARNER: 'eng'}

    # RESTART -> the replay starts in English already: no switch.
    kodi.open(_replay(80, '21:00'), WARNER_REPLAY_TRACKS)
    assert tv.started(kodi, WARNER) is None
    assert kodi.switches == 0 and kodi.language == 'eng'

    # You pick Spanish in the replay: the next live tune is Spanish -- track
    # 0, never the description (track 2).
    kodi.remote('spa')
    assert tv.tick(kodi) == ('remembered', 'spa')
    kodi.open(WARNER_LIVE, WARNER_LIVE_TRACKS)
    assert kodi.language == 'eng'
    assert tv.started(kodi, WARNER) == 0
    assert kodi.language == 'spa'

    # You pick the description on live: it is your choice, and a replay
    # restores it.
    kodi.remote('es-[ADS]')
    assert tv.tick(kodi) == ('remembered', 'es-[ADS]')
    kodi.open(_replay(81, '22:00'), WARNER_REPLAY_TRACKS)
    assert tv.started(kodi, WARNER) == 2
    assert kodi.language == 'es-[ADS]'

    assert tv.memory == {NEOX: 'spa', WARNER: 'es-[ADS]'}
    assert len(tv.writes) == 3


def test_a_spanish_only_replay_does_not_flip_the_channel():
    """Neox remembered in English; a replay of a Spanish-only show plays
    Spanish (nothing to switch to) and the memory stays English: the next
    live tune is English again."""
    kodi = _Kodi({NEOX_LIVE: 0})
    tv   = _AddOn({NEOX: 'eng'})

    kodi.open(_replay(70, '15:00'), SPANISH_ONLY_TRACKS)
    assert tv.started(kodi, NEOX) is None
    assert kodi.language == 'spa' and kodi.switches == 0
    assert tv.tick(kodi) is None
    assert tv.memory == {NEOX: 'eng'} and tv.writes == []

    kodi.open(NEOX_LIVE, NEOX_LIVE_TRACKS)
    assert tv.started(kodi, NEOX) == 1
    assert kodi.language == 'eng' and tv.writes == []


def test_the_re_check_wins_once_and_a_lost_switch_records_the_playing_language():
    """Kodi puts its own choice back once: the 1 s re-check re-applies and
    nothing is recorded. If Kodi wins both times, the language that plays
    (offered next to the remembered one) is what last played on the channel,
    so it is remembered -- the plan's settle rule."""
    kodi = _Kodi({NEOX_LIVE: 0})
    tv   = _AddOn({NEOX: 'eng'})
    kodi.reverts = 1
    kodi.open(NEOX_LIVE, NEOX_LIVE_TRACKS)
    assert tv.started(kodi, NEOX) == 1
    assert kodi.switches == 2 and kodi.language == 'eng'
    assert tv.writes == []

    kodi.reverts = 2
    kodi.open(_replay(71, '16:00'), NEOX_REPLAY_TRACKS)
    kodi.remote('spa')                  # Kodi's choice is Spanish this time
    kodi.switches = 0
    assert tv.started(kodi, NEOX) == 1
    assert kodi.switches == 2 and kodi.language == 'spa'
    assert tv.memory == {NEOX: 'spa'} and len(tv.writes) == 1


def test_two_channels_keep_their_own_language():
    """Each channel key has its own entry: a choice on one never moves the other."""
    kodi = _Kodi({NEOX_LIVE: 0, WARNER_LIVE: 1})
    tv   = _AddOn({})
    kodi.open(NEOX_LIVE, NEOX_LIVE_TRACKS)
    tv.started(kodi, NEOX)
    kodi.open(WARNER_LIVE, WARNER_LIVE_TRACKS)
    tv.started(kodi, WARNER)
    assert tv.memory == {NEOX: 'spa', WARNER: 'eng'}
    kodi.open(_replay(60, '10:00'), NEOX_REPLAY_TRACKS)
    assert tv.started(kodi, NEOX) == 0
    kodi.open(_replay(61, '10:00'), WARNER_REPLAY_TRACKS)
    assert tv.started(kodi, WARNER) is None
    assert kodi.language == 'eng'
