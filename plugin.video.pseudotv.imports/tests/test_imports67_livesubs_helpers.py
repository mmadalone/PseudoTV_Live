# -*- coding: utf-8 -*-
"""imports.67: LiveTV subtitles follow the channel -- livesubs_helpers, the decisions.

On LiveTV (imported) channels, subtitles you had turned on were off again as
soon as a replay started. Two things open a replay (pvr://guide/<epg>/<start>.epg)
for you: the add-on's RESTART pop-up (imports.62: live -> the replay of the
show on now) and Kodi's "Play next programme automatically" (a replay ends and
Kodi opens the next show by itself). Kodi keeps video settings per video, and
every replay is a new video for Kodi: a video it has no saved settings for
starts with the default (ApplicationPlayerCallback::RequestVideoSettings,
Kodi 21), which is subtitles off on this TV. Plain live viewing kept the
choice only because a live channel is one continuous video. kodi.log on
2026-11-10: Warner TV live, RESTART pop-up, the replay 3 s later; the 13:40
replay ended and Kodi opened the 14:03 show by itself -- subtitles had to be
turned on by hand in both.

imports.67 remembers the on/off per LiveTV channel (your decision: per
channel, so Warner TV can stay on while Neox stays off) and puts it back
whenever that channel plays, live or as a replay. A subtitle toggle fires no
Python player event, so the choice is read on the service's ~1 s tick.

livesubs_helpers holds every decision and imports nothing, so it runs here
directly, without Kodi stubs:
  - is_livetv: which playback it applies to (live or replay, never Custom);
  - listed: which imported channels Kodi can be showing (the ones the
    playlist publishes: enabled, not orphaned, allocated);
  - channel_key: which channel, from Kodi's VideoPlayer.ChannelName (and
    VideoPlayer.ChannelNumberLabel, only to tell apart two listed channels
    with one name -- Kodi numbers channels itself on this TV, so the label is
    a running count, not the add-on's number);
  - subtitle_index: the track "on" goes through (showing the flag alone
    leaves a track Kodi disabled at the start blank);
  - valid_memory: the memory file read back;
  - restore_action: what to switch when a channel starts playing;
  - record_action: what to remember while it plays;
  - remember: the copy-on-write update the play thread reads without a lock.
The Kodi side (services.py _onPlay / _chkIdle, the memory file) is tested
apart. The end of this file chains the helpers through the 2026-11-10 case
and the MTV / CNN Int case.
"""
import ast
import copy
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

# Channel ids as the service's channel list holds them (channels.json records).
NEOX      = 'NEOX@movistarplus'                                  # Neox, 8
WARNER    = 'TNT@movistarplus'                                   # Warner TV, 12
CN        = 'CartoonNetwork.us@epgbest_46477'                    # Cartoon Network, 303 (imported)
CN_CUSTOM = '3330322e436172746f6f6e204e657477@PseudoTV_Live'     # an own (Custom) "Cartoon Network", 304
CN_TWIN   = 'CartoonNetwork.es@epgbest_46477'                    # a second imported "Cartoon Network" (41)
# The real case on this TV: MTV and CNN Int are imported twice each -- enabled
# from autonomiques, disabled in the movistarplus package. Only the enabled
# ones are in the playlist, so only they can be what Kodi is playing.
MTV       = 'MTV.es@autonomiques'
MTV_OFF   = 'MTV@movistarplus'
CNN       = 'CNN.us@autonomiques'
CNN_OFF   = 'CNNINT@movistarplus'

NAME_PREFIX = 'name:'   # ls._NAME_PREFIX: the key of a channel no listed import has


def _import(cid, number, name, source, **extra):
    """An imported channels.json record as syncAll writes it."""
    rec = {'id': cid, 'type': 'import', 'number': number, 'name': name, 'enabled': True,
           'is_orphan': False, 'unallocated': False, 'import_source': source}
    rec.update(extra)
    return rec


CHANNELS = [
    _import(NEOX,   8,   'Neox',            'movistarplus'),
    _import(WARNER, 12,  'Warner TV',       'movistarplus'),
    _import(CN,     303, 'Cartoon Network', 'epgbest_46477'),
    {'id': CN_CUSTOM, 'type': 'Custom', 'number': 304, 'name': 'Cartoon Network', 'enabled': True},
]
# Two listed imported channels with one name: only Kodi's number label can tell them apart.
TWINS = CHANNELS + [_import(CN_TWIN, 41, 'Cartoon Network', 'epgbest_46477')]
# The MTV / CNN Int case: each name twice, once listed and once disabled.
REAL = CHANNELS + [
    _import(MTV,     57,   'MTV',     'autonomiques'),
    _import(MTV_OFF, 9036, 'MTV',     'movistarplus', enabled=False),
    _import(CNN,     58,   'CNN Int', 'autonomiques'),
    _import(CNN_OFF, 9040, 'CNN Int', 'movistarplus', enabled=False),
]

BOOLS    = (True, False)
_MISSING = object()     # "leave this key out" in parametrized entries

# The three ways an imported channel is left out of the playlist (and so of Kodi's list).
UNLISTED = [
    pytest.param({'enabled': False},    id='disabled'),
    pytest.param({'is_orphan': True},   id='orphan'),
    pytest.param({'unallocated': True}, id='unallocated'),
]


def _src(name):
    with open(os.path.join(LIB, name), encoding='utf-8') as f:
        return f.read()


def _tree():
    return ast.parse(_src('livesubs_helpers.py'), feature_version=(3, 8))


def _record(**fields):
    """A Neox record with `fields` changed (_MISSING removes the key)."""
    out = dict(CHANNELS[0])
    for key, value in fields.items():
        if value is _MISSING:
            out.pop(key, None)
        else:
            out[key] = value
    return out


# ================================================================ module hygiene


def test_module_imports_nothing_and_parses_as_python38():
    """No Kodi modules, not even the standard library: services.py and these
    tests share one definition and need no Kodi stubs. Helper modules keep
    Python 3.8 syntax (Kodi Matrix's Python)."""
    src  = _src('livesubs_helpers.py')
    tree = ast.parse(src, feature_version=(3, 8))
    assert not [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert 'madteevee (imports fork, imports.67)' in src


def test_module_does_no_io():
    """The memory file is read and written by services.py (FileAccess.setJSON,
    crash-safe); the subtitle tracks are read there too (JSON-RPC
    Player.GetProperties). The helpers only decide."""
    called = set()
    for node in ast.walk(_tree()):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                called.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                called.add(node.func.attr)
    assert not called & {'open', '__import__', 'eval', 'exec', 'compile', 'input', 'print'}


def test_public_api_is_the_planned_one():
    public = {name for name in vars(ls) if not name.startswith('_')}
    assert public == {'MEMORY_FILE', 'RECHECK_S', 'APPLY_WAIT_S', 'is_livetv', 'listed', 'channel_key',
                      'subtitle_index', 'valid_memory', 'restore_action', 'record_action', 'remember',
                      # imports.68: audio follows the channel (tested in test_imports68_audio_helpers.py)
                      'AUDIO_MEMORY_FILE', 'lang_code', 'valid_audio_memory', 'audio_tracks', 'audio_track',
                      'audio_restore_action', 'audio_settle_record', 'audio_change_record',
                      'audio_return_action', 'remember_audio'}


def test_module_has_no_shared_state_and_no_mutable_defaults():
    """Module-level names are constants only (the memory lives in services.py
    and is swapped whole), and no function has a default a call could change."""
    tree = _tree()
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            assert isinstance(node.value, ast.Constant), ast.dump(node)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            defaults = node.args.defaults + [d for d in node.args.kw_defaults if d is not None]
            assert not [d for d in defaults if not isinstance(d, ast.Constant)], node.name


def test_constants_are_the_planned_ones():
    # cache/livetv_subtitles.json: a bare file name, joined to the cache folder by services.py.
    assert ls.MEMORY_FILE == 'livetv_subtitles.json'
    assert '/' not in ls.MEMORY_FILE and '\\' not in ls.MEMORY_FILE
    # One re-check 1 s after a restore: guards the one point of Kodi's start-up
    # order that couldn't be confirmed in its queue code, and a toggle of yours
    # can't come that fast (the OSD takes longer), so it never undoes one.
    assert ls.RECHECK_S == 1.0 and isinstance(ls.RECHECK_S, float)
    # After a re-apply, half a second for Kodi's queued change to land before
    # the state that settles the playback is read.
    assert ls.APPLY_WAIT_S == 0.5 and isinstance(ls.APPLY_WAIT_S, float)
    # A channel no listed import has is keyed by its name under this prefix (private).
    assert ls._NAME_PREFIX == NAME_PREFIX


# ================================================================ is_livetv


# (pvr_live, pvr_epgtag, is_pseudotv) -> expected
IS_LIVETV_TABLE = [
    pytest.param(True,  False, False, True,  id='livetv-live'),
    pytest.param(False, True,  False, True,  id='livetv-replay'),
    pytest.param(True,  True,  False, True,  id='livetv-both-flags'),
    pytest.param(False, False, False, False, id='library-or-other-addon'),
    pytest.param(True,  False, True,  False, id='custom-live'),
    pytest.param(False, True,  True,  False, id='custom-replay'),
    pytest.param(True,  True,  True,  False, id='custom-both-flags'),
    pytest.param(False, False, True,  False, id='custom-not-pvr'),
]


@pytest.mark.parametrize('pvr_live, pvr_epgtag, is_pseudotv, expected', IS_LIVETV_TABLE)
def test_is_livetv_table(pvr_live, pvr_epgtag, is_pseudotv, expected):
    """Pvr.IsPlayingTv (live) or Pvr.IsPlayingEpgTag (a replay), and never a
    Custom channel: Custom channels keep their own rules (Force Subtitles,
    rule 51) and the existing subtitle code."""
    assert ls.is_livetv(pvr_live, pvr_epgtag, is_pseudotv) is expected


def test_is_livetv_table_is_complete():
    rows = [p.values[:3] for p in IS_LIVETV_TABLE]
    assert len(rows) == len(set(rows))
    assert set(rows) == set(itertools.product(BOOLS, repeat=3))


@pytest.mark.parametrize('pvr_live, pvr_epgtag, is_pseudotv, expected', [
    (True, None, None, True),       # no add-on playing item yet reads as "not Custom"
    (None, 1, 0, True),
    (0, '', None, False),
    (None, None, None, False),
    (1, 0, 1, False),
])
def test_is_livetv_takes_any_truth_value_and_answers_a_bool(pvr_live, pvr_epgtag, is_pseudotv, expected):
    assert ls.is_livetv(pvr_live, pvr_epgtag, is_pseudotv) is expected


# ================================================================ listed


@pytest.mark.parametrize('record', [c for c in REAL if c['type'] == 'import' and c['enabled']],
                         ids=lambda c: c['id'])
def test_listed_an_enabled_imported_channel(record):
    assert ls.listed(record) is True


@pytest.mark.parametrize('fields', UNLISTED)
def test_listed_not_a_channel_the_playlist_leaves_out(fields):
    """Disabled, orphaned (gone from its source) or unallocated (no number in
    the import's range): the M3U writer drops it, so Kodi can't be showing it."""
    assert ls.listed(_record(**fields)) is False


@pytest.mark.parametrize('fields', [
    pytest.param({'enabled': _MISSING},     id='no-enabled'),
    pytest.param({'is_orphan': _MISSING},   id='no-is_orphan'),
    pytest.param({'unallocated': _MISSING}, id='no-unallocated'),
    pytest.param({'enabled': _MISSING, 'is_orphan': _MISSING, 'unallocated': _MISSING}, id='no-flags'),
    pytest.param({'name': _MISSING, 'number': _MISSING}, id='no-name-no-number'),
])
def test_listed_missing_flags_read_as_the_writers_defaults(fields):
    """A flag left out is the default (enabled, not orphaned, allocated); the
    name and number don't decide it (channel_key matches the name)."""
    assert ls.listed(_record(**fields)) is True


@pytest.mark.parametrize('kind', ['Custom', 'custom', '', None, _MISSING],
                         ids=['Custom', 'custom', 'empty', 'none', 'missing'])
def test_listed_only_imported_channels(kind):
    """Custom channels have their own subtitle rules: never in this memory."""
    record = _record(type=kind)
    assert ls.listed(record) is False


@pytest.mark.parametrize('broken', [
    pytest.param(None, id='none'),
    pytest.param(NEOX, id='text'),
    pytest.param(['Neox', 8], id='list'),
    pytest.param(8, id='number'),
    pytest.param({}, id='empty-dict'),
    pytest.param(_record(id=_MISSING), id='no-id'),
    pytest.param(_record(id=''), id='empty-id'),
    pytest.param(_record(id=None), id='id-none'),
    pytest.param(_record(id=8), id='id-number'),
    pytest.param(_record(id=[NEOX]), id='id-list'),
])
def test_listed_unusable_entries_are_not(broken):
    assert ls.listed(broken) is False


def _m3u_writer_lists(ch):
    # imports.py syncAll, the M3U branch: a channel with this test true is
    # removed from pseudotv.m3u (delStation) instead of written.
    return not (ch.get('is_orphan') or ch.get('unallocated') or not ch.get('enabled', True))


def test_m3u_writer_test_is_still_the_one_listed_follows():
    """listed() copies the M3U writer's test; if that test changes, this flags it."""
    assert "if ch.get('is_orphan') or ch.get('unallocated') or not ch.get('enabled', True):" in _src('imports.py')


@pytest.mark.parametrize('enabled, is_orphan, unallocated', list(itertools.product((True, False, _MISSING), repeat=3)))
def test_listed_agrees_with_the_m3u_writer(enabled, is_orphan, unallocated):
    """For every combination of the flags as channels.json stores them
    (true, false or left out), listed() answers what the playlist does."""
    record = _record(enabled=enabled, is_orphan=is_orphan, unallocated=unallocated)
    assert ls.listed(record) is _m3u_writer_lists(record)


# ================================================================ channel_key


@pytest.mark.parametrize('number, name, key', [
    ('8',   'Neox',            NEOX),
    ('12',  'Warner TV',       WARNER),
    ('303', 'Cartoon Network', CN),
])
def test_channel_key_a_unique_name_gives_the_channel_id(number, name, key):
    """The id survives a rename or a renumber: the name follows the record."""
    assert ls.channel_key(number, name, CHANNELS) == key


@pytest.mark.parametrize('number', ['8', '99', '12', '1', '', None, '   ', '\t', 8, 8.0, ['8'], b'8'], ids=repr)
def test_channel_key_a_unique_name_ignores_the_number_label(number):
    """Kodi numbers channels itself on this TV (usebackendchannelnumbers
    off): its number label is a running count that moves when a channel is
    added before it, so it never decides a unique name -- missing, not text,
    or another channel's number all give the same id."""
    assert ls.channel_key(number, 'Neox', CHANNELS) == NEOX


@pytest.mark.parametrize('name', ['neox', 'NEOX', 'nEoX', '  Neox  ', '\tNeox\n', ' NEOX '], ids=repr)
def test_channel_key_ignores_case_and_surrounding_spaces(name):
    assert ls.channel_key('8', name, CHANNELS) == NEOX


@pytest.mark.parametrize('stored, label', [
    ('  Neox ', 'Neox'),            # spaces around the stored name
    ('NEOX', 'neox'),
    ('À Punt', 'À PUNT'),           # accented capitals
    ('Straße TV', 'STRASSE TV'),    # casefold, not lower: ß is "ss", on both sides
    ('STRASSE TV', 'Straße TV'),
])
def test_channel_key_name_comparison_on_both_sides(stored, label):
    channels = [_record(name=stored)]
    assert ls.channel_key('8', label, channels) == NEOX


@pytest.mark.parametrize('kind', ['Custom', 'custom', '', None, _MISSING],
                         ids=['Custom', 'custom', 'empty', 'none', 'missing'])
@pytest.mark.parametrize('number', ['304', '', '9'])
def test_channel_key_counts_only_imported_channels(kind, number):
    """Only imported channels (type 'import') give their id: a Custom channel
    with exactly these labels is no listed import, so the name key."""
    channels = [{'id': CN_CUSTOM, 'number': 304, 'name': 'Cartoon Network'}]
    if kind is not _MISSING:
        channels[0]['type'] = kind
    assert ls.channel_key(number, 'Cartoon Network', channels) == NAME_PREFIX + 'Cartoon Network'


@pytest.mark.parametrize('number', ['303', '304', '', '999'])
def test_channel_key_a_custom_namesake_does_not_make_the_name_ambiguous(number):
    """This TV has an imported Cartoon Network (303) and a Custom one (304):
    among listed imports the name is unique, so the imported channel is
    found whatever Kodi's number label says."""
    assert ls.channel_key(number, 'Cartoon Network', CHANNELS) == CN


def test_channel_key_alone_would_map_the_custom_namesake_so_is_livetv_keeps_it_out():
    """The name maps the Custom channel's labels (304, Cartoon Network) to
    the imported channel: is_livetv is what keeps Custom playback out of the
    memory."""
    assert ls.channel_key('304', 'Cartoon Network', CHANNELS) == CN
    assert ls.is_livetv(True, False, True) is False
    assert ls.is_livetv(False, True, True) is False


@pytest.mark.parametrize('number', ['57', '9036', '41', '42', '', '1', None], ids=repr)
def test_channel_key_mtv_and_cnn_int_give_the_listed_import(number):
    """The real case: MTV and CNN Int each have an enabled autonomiques entry
    and a disabled movistarplus entry with the same name. Only the enabled
    one is in Kodi's list, so it is the channel -- whatever Kodi's number
    label (its running count, the enabled entry's number, the disabled one's,
    or none)."""
    assert ls.channel_key(number, 'MTV', REAL) == MTV
    assert ls.channel_key(number, 'CNN Int', REAL) == CNN
    assert ls.channel_key(number, ' mtv ', REAL) == MTV


@pytest.mark.parametrize('fields', UNLISTED)
@pytest.mark.parametrize('number', ['303', '41', '', '99'])
def test_channel_key_an_unlisted_namesake_never_makes_a_tie(fields, number):
    """The second imported Cartoon Network disabled, orphaned or
    unallocated: the listed one is the only one, even when Kodi's number
    label is the unlisted one's number."""
    channels = CHANNELS + [_import(CN_TWIN, 41, 'Cartoon Network', 'epgbest_46477', **fields)]
    assert ls.channel_key(number, 'Cartoon Network', channels) == CN


@pytest.mark.parametrize('fields', UNLISTED)
@pytest.mark.parametrize('number', ['8', '9036', ''])
def test_channel_key_only_unlisted_namesakes_give_the_name_key(fields, number):
    """No listed import has the name: Kodi's channel can't be from the
    playlist (another PVR source, or the list behind Kodi's), so the name key
    -- never the id of a channel the playlist leaves out."""
    assert ls.channel_key(number, 'Neox', [_record(**fields)]) == NAME_PREFIX + 'Neox'
    mtv_only = dict(next(c for c in REAL if c['id'] == MTV_OFF), enabled=True)
    mtv_only.update(fields)
    assert ls.channel_key(number, 'MTV', [mtv_only]) == NAME_PREFIX + 'MTV'


@pytest.mark.parametrize('number', ['303', '41', ' 41 ', '\t303\n', '', '7', None], ids=repr)
def test_channel_key_listed_twins_are_never_told_apart(number):
    """Two listed imports with one name: Kodi numbers channels itself here, so its number label is a
    running count that can coincide with either record's own number -- no tie-break on it: nothing
    is remembered for them (None), whatever the label (review 2026-11-12)."""
    assert ls.channel_key(number, 'Cartoon Network', TWINS) is None


@pytest.mark.parametrize('stored_number', [41, '41'], ids=repr)
def test_channel_key_twins_stay_untold_whatever_the_stored_number(stored_number):
    twins = TWINS[:-1] + [dict(TWINS[-1], number=stored_number)]
    assert ls.channel_key('41', 'Cartoon Network', twins) is None


@pytest.mark.parametrize('fields', UNLISTED)
def test_channel_key_an_unlisted_namesake_makes_no_tie(fields):
    """An unlisted namesake doesn't count: with two listed twins it is still a tie (None); with one
    listed channel that one's id, whatever the label."""
    third = _import('CartoonNetwork.uk@epgbest_46477', 7, 'Cartoon Network', 'epgbest_46477', **fields)
    for label in ('7', '303', '41'):
        assert ls.channel_key(label, 'Cartoon Network', TWINS + [third]) is None
        assert ls.channel_key(label, 'Cartoon Network', TWINS[:-1] + [third]) == CN


def test_channel_key_exact_twins_cannot_be_told_apart():
    """Two listed records with the same number and name (no duplicate
    numbers on this TV, but a hand-edited list could have them): None, never
    a guess."""
    channels = [_record(), _record(id='NEOXHD@movistarplus')]
    assert ls.channel_key('8', 'Neox', channels) is None


@pytest.mark.parametrize('number, name', [
    ('8', 'Neox HD'),               # renamed in Kodi only: the number alone never matches
    ('12', 'Warner'),
    ('8', 'Neo'),
])
def test_channel_key_number_alone_never_matches(number, name):
    assert ls.channel_key(number, name, CHANNELS) == NAME_PREFIX + name


@pytest.mark.parametrize('channels', [CHANNELS, REAL, None, [], ()],
                         ids=['other-channels', 'real', 'none', 'empty-list', 'empty-tuple'])
def test_channel_key_no_listed_match_gives_the_name_key(channels):
    """A channel from another PVR source (or one the list doesn't know yet):
    'name:<name>', built from the name Kodi shows, spaces around it removed."""
    assert ls.channel_key('77', 'Canal Sur', channels) == NAME_PREFIX + 'Canal Sur'
    assert ls.channel_key(' 77 ', '\tCanal Sur ', channels) == NAME_PREFIX + 'Canal Sur'


@pytest.mark.parametrize('number', ['77', '78', '1', '', None, '   ', 77], ids=repr)
def test_channel_key_the_name_key_ignores_the_number_label(number):
    """Kodi's running count moves when a channel is added before it; the
    name key stays, so the choice isn't lost on a renumber."""
    assert ls.channel_key(number, 'Canal Sur', CHANNELS) == NAME_PREFIX + 'Canal Sur'


def test_channel_key_the_name_key_keeps_the_name_as_kodi_shows_it():
    """Matching a listed import ignores case; the name key is the name as
    Kodi shows it (Kodi shows it the same way live and in replays)."""
    assert ls.channel_key('77', 'canal sur', CHANNELS) == NAME_PREFIX + 'canal sur'
    assert ls.channel_key('77', 'Canal  Sur', CHANNELS) == NAME_PREFIX + 'Canal  Sur'   # inner spaces kept


@pytest.mark.parametrize('name', [None, '', '   ', '\t\n', 8, ['Neox'], b'Neox'], ids=repr)
@pytest.mark.parametrize('number', ['8', ''])
def test_channel_key_without_a_name_is_none(number, name):
    """No channel name: nothing to remember it by, so nothing is done."""
    assert ls.channel_key(number, name, CHANNELS) is None
    assert ls.channel_key(number, name, None) is None


@pytest.mark.parametrize('broken', [
    pytest.param(None, id='none'),
    pytest.param(NEOX, id='text'),
    pytest.param(['Neox', 8], id='list'),
    pytest.param(8, id='number'),
    pytest.param({}, id='empty-dict'),
    pytest.param(_record(id=_MISSING), id='no-id'),
    pytest.param(_record(id=''), id='empty-id'),
    pytest.param(_record(id=None), id='id-none'),
    pytest.param(_record(id=8), id='id-number'),
    pytest.param(_record(id=[NEOX]), id='id-list'),
    pytest.param(_record(type=_MISSING), id='no-type'),
    pytest.param(_record(type='Custom'), id='custom'),
    pytest.param(_record(enabled=False), id='disabled'),
    pytest.param(_record(is_orphan=True), id='orphan'),
    pytest.param(_record(unallocated=True), id='unallocated'),
    pytest.param(_record(name=None), id='name-none'),
    pytest.param(_record(name=8), id='name-number'),
    pytest.param(_record(name=_MISSING), id='no-name'),
])
def test_channel_key_ignores_unusable_and_unlisted_entries(broken):
    """An unusable or unlisted entry neither matches nor makes a name ambiguous."""
    assert ls.channel_key('8', 'Neox', [broken]) == NAME_PREFIX + 'Neox'
    # next to the real record: any number label finds the only listed Neox
    assert ls.channel_key('99', 'Neox', [broken, CHANNELS[0]]) == NEOX
    assert ls.channel_key('8', 'Neox', [CHANNELS[0], broken]) == NEOX


@pytest.mark.parametrize('stored_number', [None, _MISSING, ''], ids=['none', 'missing', 'empty'])
def test_channel_key_a_record_without_a_number_matches_by_its_unique_name(stored_number):
    assert ls.channel_key('8', 'Neox', [_record(number=stored_number)]) == NEOX
    assert ls.channel_key('', 'Neox', [_record(number=stored_number)]) == NEOX


def test_channel_key_leaves_the_channel_list_alone():
    channels = copy.deepcopy(TWINS + REAL[len(CHANNELS):])
    before   = copy.deepcopy(channels)
    for number, name in (('8', 'Neox'), ('41', ' cartoon network '), ('', 'Cartoon Network'),
                         ('77', 'Canal Sur'), ('9036', 'MTV')):
        ls.channel_key(number, name, channels)
    assert channels == before


def test_channel_key_name_keys_never_look_like_ids():
    """Imported ids carry '@<import>'; name keys are 'name:<name>': each
    channel keeps its own entry in the memory."""
    ids   = [ls.channel_key(n, name, REAL) for n, name in (('8', 'Neox'), ('12', 'Warner TV'), ('41', 'MTV'))]
    names = [ls.channel_key(n, name, REAL) for n, name in (('77', 'Canal Sur'), ('', 'La 1'))]
    assert ids == [NEOX, WARNER, MTV]
    assert all('@' in key and not key.startswith(NAME_PREFIX) for key in ids)
    assert names == [NAME_PREFIX + 'Canal Sur', NAME_PREFIX + 'La 1']


# ================================================================ subtitle_index

# Kodi's JSON-RPC Player.GetProperties(['subtitles', 'currentsubtitle']) result, as
# services.py passes it ((resp or {}).get('result')).
SPA = {'index': 0, 'language': 'spa', 'name': '', 'isdefault': False, 'isforced': False, 'isimpaired': False}
ENG = {'index': 1, 'language': 'eng', 'name': '', 'isdefault': False, 'isforced': False, 'isimpaired': False}
CAT = {'index': 2, 'language': 'cat', 'name': '', 'isdefault': False, 'isforced': False, 'isimpaired': False}


def _props(tracks, current=_MISSING):
    props = {'subtitles': tracks}
    if current is not _MISSING:
        props['currentsubtitle'] = current
    return props


def _is_index(value, expected):
    """A real int (not a bool or float) equal to `expected`: what setSubtitleStream takes."""
    return type(value) is int and value == expected


@pytest.mark.parametrize('current, expected', [
    (SPA, 0), (ENG, 1), (CAT, 2), ({'index': 1}, 1),
])
def test_subtitle_index_the_current_track_when_it_is_listed(current, expected):
    """The track Kodi has selected (Spanish, your preferred language) is the
    one switched on: only on/off is the add-on's, the track stays Kodi's."""
    assert _is_index(ls.subtitle_index(_props([SPA, ENG, CAT], current)), expected)


@pytest.mark.parametrize('current', [
    pytest.param({'index': 5}, id='not-listed'),
    pytest.param({'index': -1}, id='negative'),         # no track selected
    pytest.param({}, id='no-index'),
    pytest.param({'index': None}, id='index-none'),
    pytest.param({'index': '1'}, id='index-text'),
    pytest.param(None, id='none'),                      # Kodi sends null without a selection
    pytest.param(_MISSING, id='missing'),
    pytest.param(1, id='not-a-dict-number'),
    pytest.param('1', id='not-a-dict-text'),
    pytest.param([ENG], id='not-a-dict-list'),
])
def test_subtitle_index_otherwise_the_first_listed_track(current):
    """Current not listed, missing or unusable: the first track in Kodi's
    list -- its order, not the lowest number."""
    assert _is_index(ls.subtitle_index(_props([SPA, ENG], current)), 0)
    assert _is_index(ls.subtitle_index(_props([CAT, ENG], current)), 2)


@pytest.mark.parametrize('current', [True, False], ids=repr)
def test_subtitle_index_a_bool_current_index_is_not_a_track(current):
    """Contract: a bool is no track number (the track list already drops bool
    indexes), so the first listed track, as a real int."""
    assert _is_index(ls.subtitle_index(_props([SPA, ENG], {'index': current})), 0)


def test_subtitle_index_a_bool_current_index_matching_no_track_gives_the_first():
    assert _is_index(ls.subtitle_index(_props([CAT], {'index': True})), 2)


def test_subtitle_index_tracks_without_an_int_index_are_ignored():
    tracks = [{'index': '0'}, {'index': None}, {}, {'index': True}, {'index': False}, {'index': -1},
              {'index': 1.0}, 'spa', None, 3, ['index', 0], CAT, ENG]
    assert _is_index(ls.subtitle_index(_props(tracks)), 2)
    # a current track that is only among the ignored ones isn't listed
    assert _is_index(ls.subtitle_index(_props(tracks, {'index': 0})), 2)
    assert _is_index(ls.subtitle_index(_props(tracks, ENG)), 1)


@pytest.mark.parametrize('props', [
    pytest.param(_props([]), id='empty-list'),
    pytest.param(_props([], SPA), id='empty-list-with-current'),
    pytest.param(_props(None, SPA), id='none-list'),
    pytest.param(_props((), SPA), id='empty-tuple'),
    pytest.param({'currentsubtitle': SPA}, id='no-list'),
    pytest.param(_props([{'index': '0'}, {'index': True}, {'index': -1}, None], SPA), id='only-unusable'),
    pytest.param({}, id='empty-result'),
    pytest.param(None, id='none'),                  # an error response has no 'result'
    pytest.param([SPA], id='list'),
    pytest.param('subtitles', id='text'),
    pytest.param(0, id='number'),
])
def test_subtitle_index_none_without_tracks(props):
    """Nothing to select: services.py then shows the flag at least."""
    assert ls.subtitle_index(props) is None


def test_subtitle_index_reads_kodis_answer():
    """A Movistar+ replay started off: Kodi still reports its Spanish track
    as the current one."""
    resp = json.loads('{"id": 1, "jsonrpc": "2.0", "result": {'
                      '"currentsubtitle": {"index": 0, "isdefault": false, "isforced": false, '
                      '"isimpaired": false, "language": "spa", "name": ""}, '
                      '"subtitles": [{"index": 0, "isdefault": false, "isforced": false, '
                      '"isimpaired": false, "language": "spa", "name": ""}]}}')
    assert _is_index(ls.subtitle_index(resp.get('result')), 0)


def test_subtitle_index_leaves_its_input_alone():
    props  = _props([SPA, ENG, {'index': True}], {'index': 7})
    before = copy.deepcopy(props)
    ls.subtitle_index(props)
    assert props == before


# ================================================================ valid_memory


@pytest.mark.parametrize('data', [
    None, [], [[WARNER, True]], 'text', '{"TNT@movistarplus": true}', b'{}', 1, 0, True, 2.5,
    (WARNER, True), {WARNER},
], ids=repr)
def test_valid_memory_anything_but_a_dict_reads_as_empty(data):
    """A damaged file (a list, text, a number) is an empty memory: every
    channel starts again from Kodi's own state."""
    assert ls.valid_memory(data) == {}


GOOD = {WARNER: True, NEOX: False, MTV: True, NAME_PREFIX + 'Canal Sur': True, NAME_PREFIX + 'La 1': False}


@pytest.mark.parametrize('key, value', [
    pytest.param('', True, id='empty-key'),
    pytest.param(1, True, id='number-key'),
    pytest.param(None, False, id='none-key'),
    pytest.param(('TNT', 'movistarplus'), True, id='tuple-key'),
    pytest.param(2.5, False, id='float-key'),
    pytest.param('ONE@x', 1, id='value-1'),
    pytest.param('ZERO@x', 0, id='value-0'),
    pytest.param('TRUE@x', 'true', id='value-text-true'),
    pytest.param('FALSE@x', 'false', id='value-text-false'),
    pytest.param('ON@x', 'on', id='value-text-on'),
    pytest.param('NONE@x', None, id='value-none'),
    pytest.param('FLOAT@x', 1.0, id='value-float'),
    pytest.param('LIST@x', [True], id='value-list'),
    pytest.param('DICT@x', {'on': True}, id='value-dict'),
])
def test_valid_memory_drops_bad_entries_one_by_one(key, value):
    """Text keys with a bool value only; one bad entry doesn't cost the others."""
    data = dict(GOOD)
    data[key] = value
    assert ls.valid_memory(data) == GOOD


def test_valid_memory_keeps_true_and_false():
    """"Off" is a choice too: False survives like True."""
    assert ls.valid_memory({WARNER: False}) == {WARNER: False}
    assert ls.valid_memory({WARNER: True}) == {WARNER: True}
    assert ls.valid_memory({}) == {}


def test_valid_memory_reads_a_damaged_file_text():
    text = ('{"TNT@movistarplus": true, "NEOX@movistarplus": "yes", "": false, '
            '"name:Neox": 1, "name:Canal Sur": false}')
    assert ls.valid_memory(json.loads(text)) == {WARNER: True, NAME_PREFIX + 'Canal Sur': False}


def test_valid_memory_round_trip_through_the_file():
    text = json.dumps(GOOD, sort_keys=True)
    assert ls.valid_memory(json.loads(text)) == GOOD


def test_valid_memory_is_a_new_dict_and_leaves_its_input_alone():
    data = dict(GOOD, bad=1)
    before = dict(data)
    out = ls.valid_memory(data)
    assert out is not data
    assert data == before
    out[WARNER] = False
    assert data[WARNER] is True


# ================================================================ restore_action

# (saved, current, has_track) -> expected: True = turn on, False = turn off, None = leave.
RESTORE_TABLE = [
    # remembered "on"
    (True,  True,  True,  None),     # already on
    (True,  True,  False, None),
    (True,  False, True,  True),     # a replay started off: turn on
    (True,  False, False, None),     # no subtitle track: nothing to turn on
    # remembered "off"
    (False, True,  True,  False),    # Kodi's own live setting is on: turn off
    (False, True,  False, False),    # turning off needs no track
    (False, False, True,  None),     # already off
    (False, False, False, None),
    # no choice yet: Kodi's own state stays
    (None,  True,  True,  None),
    (None,  True,  False, None),
    (None,  False, True,  None),
    (None,  False, False, None),
    # not a bool (valid_memory never gives one): read as no choice
    ('on',  True,  True,  None),
    ('on',  True,  False, None),
    ('on',  False, True,  None),
    ('on',  False, False, None),
]


@pytest.mark.parametrize('saved, current, has_track, expected', RESTORE_TABLE)
def test_restore_action_table(saved, current, has_track, expected):
    assert ls.restore_action(saved, current, has_track) is expected


def test_restore_table_is_complete():
    rows = [(saved, current, has_track) for saved, current, has_track, _ in RESTORE_TABLE]
    assert len(rows) == len(set(rows))
    assert set(rows) == set(itertools.product((True, False, None, 'on'), BOOLS, BOOLS))


@pytest.mark.parametrize('saved', [1, 0, 'off', '', [], {}], ids=repr)
@pytest.mark.parametrize('current', BOOLS)
def test_restore_action_numbers_and_text_are_not_a_choice(saved, current):
    """1 == True in Python: only a real bool counts as a remembered choice."""
    assert ls.restore_action(saved, current, True) is None


@pytest.mark.parametrize('saved, current, expected', [
    (True, None, True),     # Kodi's state read as None counts as off
    (True, 0, True),
    (True, 1, None),
    (False, 1, False),
    (False, 'on', False),
    (False, None, None),
])
def test_restore_action_reads_current_as_a_truth_value(saved, current, expected):
    assert ls.restore_action(saved, current, True) is expected


# ================================================================ record_action

# (saved, current, has_track, seed, baseline) -> expected: the choice to remember, or None.
_WITH_TRACK = [
    # remembered "on": only a change is recorded, how the playback started doesn't matter
    (True,  True,  True, True,  True,  None),
    (True,  True,  True, True,  False, None),
    (True,  True,  True, False, True,  None),
    (True,  True,  True, False, False, None),
    (True,  False, True, True,  True,  False),    # you turned them off ("off" is recorded)
    (True,  False, True, True,  False, False),
    (True,  False, True, False, True,  False),
    (True,  False, True, False, False, False),
    # remembered "off"
    (False, False, True, True,  True,  None),
    (False, False, True, True,  False, None),
    (False, False, True, False, True,  None),
    (False, False, True, False, False, None),
    (False, True,  True, True,  True,  True),     # you turned them on
    (False, True,  True, True,  False, True),
    (False, True,  True, False, True,  True),
    (False, True,  True, False, False, True),
    # no choice yet, a live tune (seed): the starting state is recorded right away
    (None,  True,  True, True,  True,  True),     # Kodi's own saved "on" for the channel
    (None,  True,  True, True,  False, True),
    (None,  False, True, True,  True,  False),
    (None,  False, True, True,  False, False),    # "off" is seeded too
    # no choice yet, a replay (no seed): only a change from how it started
    (None,  True,  True, False, True,  None),
    (None,  True,  True, False, False, True),     # you turned them on
    (None,  False, True, False, True,  False),    # you turned them off
    (None,  False, True, False, False, None),     # replay started off, still off: nothing
]
# Without a subtitle track nothing is ever recorded: a show without subtitles
# must not erase "on".
_NO_TRACK = [(saved, current, False, seed, baseline, None)
             for saved, current, seed, baseline in itertools.product((None, True, False), BOOLS, BOOLS, BOOLS)]
RECORD_TABLE = _WITH_TRACK + _NO_TRACK


@pytest.mark.parametrize('saved, current, has_track, seed, baseline, expected', RECORD_TABLE)
def test_record_action_table(saved, current, has_track, seed, baseline, expected):
    assert ls.record_action(saved, current, has_track, seed, baseline) is expected


def test_record_table_is_complete():
    rows = [row[:5] for row in RECORD_TABLE]
    assert len(rows) == len(set(rows)) == 48
    assert set(rows) == set(itertools.product((None, True, False), BOOLS, BOOLS, BOOLS, BOOLS))


@pytest.mark.parametrize('saved, current, has_track, seed, baseline',
                         list(itertools.product((None, True, False), BOOLS, BOOLS, BOOLS, BOOLS)))
def test_record_action_only_ever_records_the_current_state_and_never_the_same_choice(saved, current, has_track,
                                                                                    seed, baseline):
    """The memory file is written only when a choice changes."""
    choice = ls.record_action(saved, current, has_track, seed, baseline)
    assert choice is None or choice is current
    if isinstance(saved, bool):
        assert choice is None or choice is not saved


def test_record_action_seeds_on_a_first_live_tune():
    """Warner TV live, Kodi applied its own per-channel "on": the add-on has
    no choice yet and records "on" at once, so a RESTART straight after
    carries it over from the very first time."""
    assert ls.record_action(None, True, True, True, True) is True


def test_record_action_a_replay_without_memory_records_only_a_change():
    assert ls.record_action(None, False, True, False, False) is None     # started off, still off
    assert ls.record_action(None, True, True, False, False) is True      # you turned them on
    assert ls.record_action(None, True, True, False, True) is None       # started on, still on
    assert ls.record_action(None, False, True, False, True) is False     # you turned them off


def test_record_action_records_off():
    """"Off" is a choice, not "nothing": it comes back as False, never None."""
    assert ls.record_action(True, False, True, False, True) is False
    assert ls.record_action(True, False, True, True, True) is False
    assert ls.record_action(None, False, True, True, False) is False


@pytest.mark.parametrize('saved, current, seed, baseline',
                         list(itertools.product((None, True, False), BOOLS, BOOLS, BOOLS)))
@pytest.mark.parametrize('has_track', [False, None, 0, ''], ids=repr)
def test_record_action_no_track_never_records(saved, current, seed, baseline, has_track):
    assert ls.record_action(saved, current, has_track, seed, baseline) is None


@pytest.mark.parametrize('saved', ['on', 1, 0, [], {}], ids=repr)
def test_record_action_a_non_bool_choice_reads_as_none(saved):
    """Only a real bool is a remembered choice (valid_memory never gives
    anything else): otherwise it behaves exactly as no choice."""
    for current, seed, baseline in itertools.product(BOOLS, BOOLS, BOOLS):
        assert (ls.record_action(saved, current, True, seed, baseline)
                is ls.record_action(None, current, True, seed, baseline))


@pytest.mark.parametrize('current, baseline, expected', [
    (1, None, True), (0, 1, False), (None, 0, None), ('on', '', True), (1, 'yes', None),
])
def test_record_action_reads_truth_values_and_answers_a_bool(current, baseline, expected):
    assert ls.record_action(None, current, True, False, baseline) is expected


@pytest.mark.parametrize('saved', BOOLS)
@pytest.mark.parametrize('current', BOOLS)
@pytest.mark.parametrize('has_track', BOOLS)
def test_a_restore_is_never_recorded_back_as_your_choice(saved, current, has_track):
    """After the restore (or none), the next tick finds nothing to record:
    the add-on's own switch is never taken for a toggle of yours."""
    action = ls.restore_action(saved, current, has_track)
    after = current if action is None else action
    for seed, baseline in itertools.product(BOOLS, BOOLS):
        assert ls.record_action(saved, after, has_track, seed, baseline) is None


# ================================================================ remember


def test_remember_is_copy_on_write():
    """The play thread may hold the old dict while the monitor thread
    remembers a choice: the old dict never changes, the new one is swapped in
    whole (no lock needed)."""
    memory = {WARNER: True}
    reader = memory
    new = ls.remember(memory, NEOX, False)
    assert new == {WARNER: True, NEOX: False}
    assert new is not memory
    assert memory == {WARNER: True} and reader == {WARNER: True}
    newer = ls.remember(new, WARNER, False)
    assert newer == {WARNER: False, NEOX: False}
    assert new == {WARNER: True, NEOX: False}


@pytest.mark.parametrize('memory', [None, {}], ids=['none', 'empty'])
def test_remember_starts_an_empty_memory(memory):
    assert ls.remember(memory, WARNER, True) == {WARNER: True}


@pytest.mark.parametrize('value, stored', [
    (True, True), (False, False), (1, True), (0, False), (None, False),
], ids=repr)
def test_remember_stores_a_bool(value, stored):
    out = ls.remember({}, WARNER, value)
    assert out[WARNER] is stored


def test_remember_keeps_the_other_channels():
    memory = dict(GOOD)
    out = ls.remember(memory, NAME_PREFIX + 'Canal Sur', False)
    assert out == dict(GOOD, **{NAME_PREFIX + 'Canal Sur': False})
    assert memory == GOOD


def test_remember_result_reads_back_unchanged():
    memory = ls.remember(ls.remember({}, WARNER, 1), NAME_PREFIX + 'La 1', 0)
    assert ls.valid_memory(json.loads(json.dumps(memory))) == memory == {WARNER: True, NAME_PREFIX + 'La 1': False}


# ================================================================ the 2026-11-10 case, end to end

WARNER_LIVE = 'pvr://channels/tv/All channels/pvr.iptvsimple_12.pvr'
NEOX_LIVE   = 'pvr://channels/tv/All channels/pvr.iptvsimple_8.pvr'
CUSTOM_LIVE = 'pvr://channels/tv/All channels/pvr.iptvsimple_304.pvr'
LIVE        = {'pvr_live': True}
REPLAY      = {'pvr_epgtag': True}


def _replay(epg, start):
    return 'pvr://guide/%04d/2026-11-10 %s:00.epg' % (epg, start)


def _live(uid):
    return 'pvr://channels/tv/All channels/pvr.iptvsimple_%d.pvr' % uid


class _Kodi(object):
    """Kodi's side as measured: video settings are kept per video (a live
    channel is one video, every replay a new one) and stored when the video
    stops; a video with no saved settings starts with the default, subtitles
    off on this TV. A video that starts off also has its subtitle track
    disabled in the stream (inputstream.adaptive on Movistar+): the flag
    alone (showSubtitles(True)) then reads "on" while nothing shows;
    re-selecting the track (setSubtitleStream, as the OSD does) shows it."""

    def __init__(self, saved=None):
        self.saved      = dict(saved or {})
        self.video      = None
        self.subtitles  = False         # VideoPlayer.SubtitlesEnabled
        self.tracks     = []            # Player.GetProperties 'subtitles'
        self.current    = None          # the selected track's index
        self.track_open = False

    def open(self, video, tracks=(SPA,)):
        if self.video is not None:
            self.saved[self.video] = self.subtitles         # StoreVideoSettings
        self.video      = video
        self.subtitles  = self.saved.get(video, False)      # RequestVideoSettings
        self.tracks     = [dict(t) for t in tracks]
        self.current    = self.tracks[0]['index'] if self.tracks else None
        self.track_open = self.subtitles

    @property
    def has_track(self):                                    # VideoPlayer.HasSubtitles
        return bool(self.tracks)

    @property
    def shows_text(self):
        return self.subtitles and self.track_open and self.has_track

    def properties(self):                                   # the JSON-RPC 'result'
        current = {'index': self.current} if self.tracks else None
        return {'subtitles': [dict(t) for t in self.tracks], 'currentsubtitle': current}

    def showSubtitles(self, on):                            # the visibility flag only
        self.subtitles = bool(on)

    def setSubtitleStream(self, index):                     # re-selects the track and shows it
        self.current, self.track_open, self.subtitles = index, True, True

    def remote(self, on):
        """You, with the OSD."""
        if on:
            self.setSubtitleStream(self.current)
        else:
            self.showSubtitles(False)


class _AddOn(object):
    """The helper calls as the service makes them: restore when a channel
    starts (onAVStarted), "on" through the track (_showLiveSubtitles), then a
    record tick about once a second; the memory is written (here: dumped to
    text) only when a choice changes."""

    def __init__(self, channels, stored):
        self.channels = channels
        self.memory   = ls.valid_memory(stored)
        self.writes   = []
        self.key      = None
        self.seed     = False
        self.baseline = False

    @staticmethod
    def show(kodi, on):
        if not on:
            kodi.showSubtitles(False)
            return
        index = ls.subtitle_index(kodi.properties())
        if index is None:
            kodi.showSubtitles(True)
        else:
            kodi.setSubtitleStream(index)

    def started(self, kodi, number, name, pvr_live=False, pvr_epgtag=False, is_pseudotv=False):
        self.key = None
        if not ls.is_livetv(pvr_live, pvr_epgtag, is_pseudotv):
            return None
        self.key = ls.channel_key(number, name, self.channels)
        if self.key is None:
            return None
        saved  = self.memory.get(self.key)
        action = ls.restore_action(saved, kodi.subtitles, kodi.has_track)
        if action is not None:
            self.show(kodi, action)
        self.seed     = bool(pvr_live) and not isinstance(saved, bool)
        self.baseline = kodi.subtitles
        return action

    def tick(self, kodi):
        if self.key is None:
            return None
        choice = ls.record_action(self.memory.get(self.key), kodi.subtitles, kodi.has_track,
                                  self.seed, self.baseline)
        if choice is not None:
            self.memory = ls.remember(self.memory, self.key, choice)
            self.writes.append(json.dumps(self.memory, sort_keys=True))
            self.seed, self.baseline = False, choice
        return choice


def test_the_flag_alone_leaves_a_disabled_track_blank():
    """Why "on" goes through subtitle_index: a replay that started off reads
    "on" after showSubtitles(True) but shows nothing; the track re-selected
    shows it."""
    kodi = _Kodi()
    kodi.open(_replay(42, '13:40'))
    kodi.showSubtitles(True)
    assert kodi.subtitles is True and not kodi.shows_text
    kodi.open(_replay(43, '14:03'))
    _AddOn.show(kodi, True)
    assert kodi.shows_text and kodi.current == 0
    # no track list from Kodi: the flag at least
    kodi.open(_replay(44, '15:00'), tracks=())
    _AddOn.show(kodi, True)
    assert kodi.subtitles is True


def test_warner_tv_follows_your_choice_and_neox_stays_independent():
    kodi = _Kodi({WARNER_LIVE: True})       # Warner TV live saved "on" by Kodi before imports.67
    tv   = _AddOn(CHANNELS, None)           # first start: no memory file yet

    # 1. Warner TV live: Kodi applies its own per-channel "on"; the add-on has
    #    no choice yet, changes nothing, and the first tick seeds "on".
    kodi.open(WARNER_LIVE)
    assert tv.started(kodi, '12', 'Warner TV', **LIVE) is None
    assert kodi.shows_text
    assert tv.tick(kodi) is True
    assert tv.memory == {WARNER: True}
    assert tv.tick(kodi) is None            # no change, no write
    assert len(tv.writes) == 1

    # 2. RESTART pop-up: the replay of the show on now is a new video -> Kodi
    #    starts it off; the add-on puts "on" back, through the track, so the
    #    text shows. The restore isn't recorded.
    kodi.open(_replay(42, '13:40'))
    assert kodi.subtitles is False
    assert tv.started(kodi, '12', 'Warner TV', **REPLAY) is True
    assert kodi.shows_text
    assert tv.tick(kodi) is None

    # 3. The replay ends; "Play next programme automatically" opens the next
    #    show by itself: off again, "on" again.
    kodi.open(_replay(43, '14:03'))
    assert kodi.subtitles is False
    assert tv.started(kodi, '12', 'Warner TV', **REPLAY) is True
    assert kodi.shows_text
    assert tv.tick(kodi) is None

    # 4. A show without subtitles: nothing to turn on, and "on" isn't erased.
    kodi.open(_replay(44, '15:00'), tracks=())
    assert tv.started(kodi, '12', 'Warner TV', **REPLAY) is None
    assert kodi.subtitles is False
    assert tv.tick(kodi) is None
    assert tv.memory == {WARNER: True}

    # 5. The next show has subtitles again (Spanish and English; Kodi picked
    #    Spanish): restored "on" on Kodi's pick; you turn them off in the
    #    replay -> "off" is recorded.
    kodi.open(_replay(45, '16:00'), tracks=(SPA, ENG))
    assert tv.started(kodi, '12', 'Warner TV', **REPLAY) is True
    assert kodi.shows_text and kodi.current == 0
    kodi.remote(False)
    assert tv.tick(kodi) is False
    assert tv.tick(kodi) is None
    assert tv.memory == {WARNER: False}
    assert len(tv.writes) == 2

    # 6. Kodi restarts: the memory comes back from the file.
    tv = _AddOn(CHANNELS, json.loads(tv.writes[-1]))
    assert tv.memory == {WARNER: False}

    # 7. The next live tune: Kodi still starts the live channel "on" (its own
    #    setting from when you left it), the add-on restores "off", and the
    #    live seed doesn't overwrite a remembered choice.
    kodi.open(WARNER_LIVE)
    assert kodi.subtitles is True
    assert tv.started(kodi, '12', 'Warner TV', **LIVE) is False
    assert kodi.subtitles is False
    assert tv.tick(kodi) is None
    assert tv.writes == []

    # 8. Neox is its own channel: Kodi never saved it, so it starts (and is
    #    seeded) "off"; Warner TV's "off" is untouched.
    kodi.open(NEOX_LIVE)
    assert tv.started(kodi, '8', 'Neox', **LIVE) is None
    assert tv.tick(kodi) is False
    assert tv.memory == {WARNER: False, NEOX: False}
    #    You turn subtitles on in a Neox replay -> "on" for Neox only.
    kodi.open(_replay(50, '17:48'))
    assert tv.started(kodi, '8', 'Neox', **REPLAY) is None
    kodi.remote(True)
    assert tv.tick(kodi) is True
    assert tv.memory == {WARNER: False, NEOX: True}
    #    Neox live: Kodi's own live setting is off -> restored "on".
    kodi.open(NEOX_LIVE)
    assert kodi.subtitles is False
    assert tv.started(kodi, '8', 'Neox', **LIVE) is True
    assert kodi.shows_text
    #    Warner TV, live and as a replay: still off.
    kodi.open(WARNER_LIVE)
    tv.started(kodi, '12', 'Warner TV', **LIVE)
    assert kodi.subtitles is False
    kodi.open(_replay(46, '18:00'))
    tv.started(kodi, '12', 'Warner TV', **REPLAY)
    assert kodi.subtitles is False
    assert tv.tick(kodi) is None

    # 9. A Custom channel with an imported channel's name, and a library
    #    video: never switched, never recorded.
    kodi.open(CUSTOM_LIVE)
    kodi.remote(True)
    assert tv.started(kodi, '304', 'Cartoon Network', is_pseudotv=True, **LIVE) is None
    kodi.remote(False)
    assert tv.tick(kodi) is None
    kodi.open('/storage/videos/film.mkv')
    assert tv.started(kodi, '', '') is None
    kodi.remote(True)
    assert tv.tick(kodi) is None

    # Every write was a change, and the last one is the memory.
    assert len(tv.writes) == 2
    assert json.loads(tv.writes[-1]) == tv.memory == {WARNER: False, NEOX: True}


def test_mtv_and_cnn_int_follow_the_listed_import_whatever_kodi_numbers_them():
    """MTV and CNN Int are each imported twice on this TV (enabled from
    autonomiques, disabled in the movistarplus package). The choice is kept
    under the enabled entry's id, live and in replays, and survives Kodi
    renumbering its list (a channel added before them); the disabled entries
    never get one."""
    kodi = _Kodi()
    tv   = _AddOn(REAL, {})

    kodi.open(_live(41))
    assert tv.started(kodi, '41', 'MTV', **LIVE) is None
    assert tv.key == MTV
    assert tv.tick(kodi) is False           # seeded "off"
    kodi.remote(True)
    assert tv.tick(kodi) is True
    kodi.open(_replay(80, '21:00'))
    assert tv.started(kodi, '41', 'MTV', **REPLAY) is True
    assert kodi.shows_text
    #   Kodi renumbered: MTV is 42 now -- the same channel, the same choice.
    kodi.open(_replay(81, '22:00'))
    assert tv.started(kodi, '42', 'MTV', **REPLAY) is True
    assert tv.key == MTV and kodi.shows_text

    #   CNN Int is its own channel.
    kodi.open(_live(43))
    assert tv.started(kodi, '43', 'CNN Int', **LIVE) is None
    assert tv.key == CNN
    assert tv.tick(kodi) is False

    assert tv.memory == {MTV: True, CNN: False}
    assert MTV_OFF not in tv.memory and CNN_OFF not in tv.memory


def test_two_listed_imports_with_one_name_are_never_told_apart():
    """A hypothetical second listed Cartoon Network: whatever Kodi's number label says, nothing is
    switched or remembered for either of them (Kodi's label is its own running count here)."""
    kodi = _Kodi()
    tv   = _AddOn(TWINS, {CN: True})
    for minute, label in enumerate(('303', '41', '7')):
        kodi.open(_replay(60 + minute, '09:00'))
        assert tv.started(kodi, label, 'Cartoon Network', **REPLAY) is None
        assert tv.key is None and kodi.subtitles is False
        kodi.remote(True)
        assert tv.tick(kodi) is None
        kodi.remote(False)
    assert tv.memory == {CN: True}


def test_an_unknown_channel_is_remembered_by_its_name():
    """A channel the list doesn't know (another PVR source, or just imported)
    still keeps your choice, under 'name:<name>', live and in replays -- and
    after Kodi renumbers it."""
    kodi = _Kodi()
    tv   = _AddOn(CHANNELS, {})
    kodi.open(_live(77))
    assert tv.started(kodi, '77', 'Canal Sur', **LIVE) is None
    assert tv.tick(kodi) is False           # seeded "off"
    kodi.remote(True)
    assert tv.tick(kodi) is True
    kodi.open(_replay(70, '20:00'))
    assert tv.started(kodi, '77', 'Canal Sur', **REPLAY) is True
    kodi.open(_replay(71, '21:00'))
    assert tv.started(kodi, '78', 'Canal Sur', **REPLAY) is True             # renumbered
    assert kodi.shows_text
    assert tv.memory == {NAME_PREFIX + 'Canal Sur': True}



@pytest.mark.parametrize('enabled', [None, 0, '', False], ids=repr)
def test_listed_treats_a_falsy_enabled_as_disabled_like_the_playlist_writer(enabled):
    """imports.py publishes a channel only when `ch.get('enabled', True)` is true: any falsy value
    is disabled there, so it is not listed here either."""
    assert ls.listed({'id': 'A@imp', 'type': 'import', 'name': 'A', 'enabled': enabled}) is False


@pytest.mark.parametrize('listing', [5, True, 'tracks', {'index': 0}], ids=repr)
def test_subtitle_index_a_track_list_that_is_not_a_list_gives_none(listing):
    assert ls.subtitle_index({'subtitles': listing, 'currentsubtitle': {'index': 0}}) is None
