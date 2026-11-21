# -*- coding: utf-8 -*-
"""imports.69: the Save fix -- importcfg_helpers, who writes which import field.

The bug ("never synced"): the dashboard posted the whole import list back on
every Save and the add-on stored it as sent. A Save wiped every import's
last-sync time, error and retry state and deleted-channel list (tombstones),
and a sync that was running wrote back the list it had read before the Save.

imports.69 gives every field of an import entry one owner:
  - DASHBOARD_FIELDS (the form's 12 boxes and the hand-set catchup_* keys):
    only a dashboard save writes them, per box;
  - SYNC_FIELDS: only the sync writes them, as a change list (delta) applied
    onto the entry as it is on disk when the sync commits;
  - ACTION_FIELDS (tombstones): Orphans adds ids, Re-discover removes ids;
  - DERIVED_FIELDS: GET reply only, never stored.
importcfg_helpers holds the tables and every merge as pure functions
(standard library only), so everything here runs without Kodi stubs, except
the source scans at the top, which read files and reuse the Python 3.8
deny-list of test_imports63_clock.

Sections:
  - module hygiene, the Python 3.8 checks and the one no-'|' scanner for the
    three new imports.69 modules (later stages and steps 2-3 extend its table);
  - the tables (pinned, disjoint, complete against imports.py's writers);
  - small helpers (addresses, fingerprints, ids, cache names, cleanup);
  - normalise_form_entry and merge_dashboard_entry;
  - apply_dashboard_request: the new page's add / edit / delete;
  - apply_dashboard_request: a whole list from a tab opened before imports.69
    (lists from an imports.68 GET, and lists whose never-synced imports GET
    marks since imports.69);
  - the refusal messages (no final full stop; what Delete does to a
    duplicated Identifier);
  - sync_delta, apply_sync_results and plan_sync_commit (the sync's commit;
    its deletes are the tombstones on disk at commit time);
  - inputs are never modified.
All data is synthetic (example.* addresses, made-up ids).
"""
import ast
import copy
import json
import os

import pytest

import importcfg_helpers as h
from importcfg_helpers import MISSING, ImportRequestError

HERE = os.path.dirname(os.path.abspath(__file__))
LIB  = os.path.normpath(os.path.join(HERE, '..', 'resources', 'lib'))

T0 = 1762160000                      # a fixed epoch; only differences matter
NEW_MODULES = ('channels_lock.py', 'importcfg_helpers.py', 'kick_helpers.py')


def _src(name):
    with open(os.path.join(LIB, name), encoding='utf-8') as f:
        return f.read()


# ================================================================ synthetic data


def synced(iid, **fields):
    """An import as the add-on stores it after syncs: every form box plus the
    sync results. `fields` overrides; MISSING removes a key."""
    entry = {
        'id': iid, 'name': iid.capitalize(), 'enabled': True,
        'm3u_url': 'https://playlist.example/%s.m3u' % iid, 'm3u_path': '',
        'epg_url': 'https://guide.example/%s.xml' % iid, 'epg_path': '',
        'start_num': 100, 'refresh_interval_min': 600, 'respect_source_numbers': True,
        'rediscover_deleted': False, 'detect_placeholder_pictures': True,
        'last_sync_at': T0, 'last_status': 'ok', 'last_error': None, 'fail_count': 0,
        'failed_since': None, 'playlist_rev': 1, 'etag': '"p1"',
        'last_modified': 'Mon, 03 Nov 2025 08:00:00 GMT', 'links_mtime_ns': '1762156800000000000',
        'epg_etag': '"g1"', 'epg_last_modified': 'Mon, 03 Nov 2025 08:00:00 GMT', 'epg_fail_count': 0,
    }
    return _with(entry, fields)


def never_synced(iid, **fields):
    """An import added but not synced yet: only the form's keys."""
    entry = {k: v for k, v in synced(iid).items() if k in h.FORM_FIELDS}
    return _with(entry, fields)


def _with(entry, fields):
    for key, value in fields.items():
        if value is MISSING:
            entry.pop(key, None)
        else:
            entry[key] = value
    return entry


def get_copy(entries):
    """What GET /imports.json gave a page: a JSON round trip of the list."""
    return json.loads(json.dumps(entries))


def form_item(entry, **changes):
    """What the old page's gatherImportForm posts for `entry` (openImportModal
    fills the boxes from it: manager.html 1767-1813), with `changes` typed in."""
    item = {
        'id': entry['id'],
        'name': (entry.get('name') or '').strip() or entry['id'],
        'm3u_path': (entry.get('m3u_path') or '').strip(),
        'm3u_url': (entry.get('m3u_url') or '').strip(),
        'epg_path': (entry.get('epg_path') or '').strip(),
        'epg_url': (entry.get('epg_url') or '').strip(),
        'start_num': entry.get('start_num') or 800,
        'refresh_interval_min': entry.get('refresh_interval_min') or 60,
        'respect_source_numbers': bool(entry.get('respect_source_numbers')),
        'rediscover_deleted': bool(entry.get('rediscover_deleted')),
        'enabled': entry.get('enabled') is not False,
        'detect_placeholder_pictures': entry.get('detect_placeholder_pictures') is not False,
    }
    item.update(changes)
    return item


def new_form_item(iid, **changes):
    """The old page's Add: the empty form's defaults plus what was typed."""
    return form_item({'id': iid, 'm3u_url': 'https://playlist.example/%s.m3u' % iid}, **changes)


def old_edit(page, iid, **changes):
    """The old page's Edit + Save: its GET list with that item replaced by the form."""
    out = copy.deepcopy(page)
    idx = [i for i, e in enumerate(out) if e['id'] == iid][0]
    out[idx] = form_item(out[idx], **changes)
    return out


def old_add(page, item):
    return copy.deepcopy(page) + [item]


def old_delete(page, iid):
    return [copy.deepcopy(e) for e in page if e['id'] != iid]


def refused(fresh, payload, status, check_address=None):
    """apply_dashboard_request must refuse with `status` and leave `fresh` as it was."""
    before = copy.deepcopy(fresh)
    with pytest.raises(ImportRequestError) as err:
        h.apply_dashboard_request(fresh, payload, check_address)
    assert err.value.status == status, err.value.message
    assert fresh == before
    err.value.message.encode('ascii')                 # every message is ASCII
    return err.value


# ================================================================ module hygiene


def _imported(tree):
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split('.')[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add((node.module or '').split('.')[0])
    return names


def _calls(tree):
    """(names called as functions, attribute names called as methods)."""
    names, attrs = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                names.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                attrs.add(node.func.attr)
    return names, attrs


def test_module_is_standard_library_only_and_does_no_io():
    """No Kodi modules, no add-on modules, no file or network access:
    channels.py, imports.py, server.py and these tests share one definition.
    (The imports alone already rule out os, io and urllib.)"""
    src  = _src('importcfg_helpers.py')
    tree = ast.parse(src, feature_version=(3, 8))
    assert _imported(tree) <= {'copy', 'hashlib', 'json', 're'}
    names, attrs = _calls(tree)
    assert not names.intersection({'open', '__import__', 'eval', 'exec', 'compile', 'input', 'print'})
    assert not attrs.intersection({'open', 'urlopen', 'listdir', 'scandir', 'unlink', 'rmdir', 'makedirs', 'load', 'dump'})
    assert 'madteevee (imports fork, imports.69)' in src


@pytest.mark.parametrize('name', NEW_MODULES)
def test_new_modules_are_python38(name):
    """Kodi 19/20 run Python 3.8: the AST check plus the shared deny-list of
    3.9+ APIs and syntax that ast's feature_version does not reject."""
    from test_imports63_clock import py39_api_leaks
    src = _src(name)
    ast.parse(src, feature_version=(3, 8))
    assert py39_api_leaks(src) == []
    assert 'madteevee (imports fork, imports.69)' in src


# imports.69: THE no-'|' scanner. Python 3.8 has no dict '|' (and no '|' in
# annotations), py39_api_leaks doesn't look for a dict '|', and Kodi here runs
# Python 3.13, so only this check catches one before a Kodi 19/20 user does.
# A '|' is allowed only on the reviewed source lines listed per file, by their
# stripped text. The later imports.69 stages and steps 2 and 3 add their files
# to this table instead of writing their own scanner.
_O_FLAGS_LINE = 'fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)'
BITOR_REVIEWED = {
    'channels.py': [],
    'channels_lock.py': [],
    'importcfg_helpers.py': [],
    'kick_helpers.py': [],
    'm3u.py': [],
    'manager.py': [],
    'server.py': [],
    'tasks.py': [],
    # imports.py's '|' at imports.68, reviewed: integer flags in the two
    # atomic cache writers (_epgCacheWrite, _logoWriteAtomic; two '|' per
    # line, so each line is listed twice) and two set unions in syncAll.
    'imports.py': [_O_FLAGS_LINE] * 4 + [
        "pic_present |= {c.get('id') for c in all_imported_now.values() if c.get('id')}",
        "present_ids |= {ch.get('id') for ch in all_imported_now.values() if ch.get('id')}",
    ],
    # imports.70 (step 2): replay_helpers.py's one '|' since imports.59, a set
    # union in merge_replay_guide, reviewed. Everything imports.70 adds or
    # changes there (the episode reader, the re-describe, the stale-export
    # guard and its gap fill) stays free of '|'.
    'replay_helpers.py': ['merge_ids = set(link_slots) | set(prev_slots)'],
    # imports.70 (the replay-end watcher): none. Its BUSY condition's '|' is
    # Kodi's OR inside a string, not an operator.
    'replayend_helpers.py': [],
    # imports.71 (settings reads that survive Kodi's settings reload): none in
    # the three files it changes whole methods of. cache.py and services.py
    # keep their pre-existing '|' lines; their changed methods are checked in
    # test_imports71_settings_reads.py.
    'cqueue.py': [],
    'pool.py': [],
    'variables.py': [],
    # imports.71 (the loop guards and restart requests): none in the new
    # loopguard_helpers.py, nor in kodi.py and backup.py. services.py's one
    # '|' is upstream's, in Monitor._chkIdle (a bool and an int, then a
    # compare), whose body imports.71 only wraps: reviewed, left as it is.
    'loopguard_helpers.py': [],
    'kodi.py': [],
    'backup.py': [],
    'services.py': ['self.isIdle   = bool(self.idleTime) | self.idleTime > OSD_TIMER'],
    # imports.72 (the Up next card's labels): none in the two files it changes.
    'overlay.py': [],
    'onnext_helpers.py': [],
}


def bitor_lines(src):
    """The stripped source lines holding a '|' or '|=' operator."""
    tree  = ast.parse(src, feature_version=(3, 8))
    lines = src.splitlines()
    return sorted(lines[node.lineno - 1].strip() for node in ast.walk(tree)
                  if isinstance(node, (ast.BinOp, ast.AugAssign)) and isinstance(node.op, ast.BitOr))


@pytest.mark.parametrize('name', sorted(BITOR_REVIEWED))
def test_no_bitor_outside_the_reviewed_lines(name):
    assert bitor_lines(_src(name)) == sorted(BITOR_REVIEWED[name])


@pytest.mark.parametrize('src, found', [
    ('a = {}\nb = a | {}', ['b = a | {}']),
    ('s = set()\ns |= {1}', ['s |= {1}']),
    ('flags = 1 | 2', ['flags = 1 | 2']),
    ("pattern = 'a|b'", []),
    ('x = {1}.union({2})', []),
])
def test_the_bitor_scanner_itself(src, found):
    assert bitor_lines(src) == found


# ================================================================ the tables


def test_tables_are_pinned():
    """The ownership tables (precedent: META_ONLY_FIELDS, test_imports30)."""
    assert h.FORM_FIELDS == ('id', 'name', 'm3u_url', 'm3u_path', 'epg_url', 'epg_path', 'start_num',
                             'refresh_interval_min', 'respect_source_numbers', 'rediscover_deleted',
                             'enabled', 'detect_placeholder_pictures')
    assert h.CATCHUP_FIELDS == ('catchup_mode', 'catchup_query_format', 'catchup_days')
    assert isinstance(h.DASHBOARD_FIELDS, frozenset)
    assert h.DASHBOARD_FIELDS == frozenset(h.FORM_FIELDS + h.CATCHUP_FIELDS)
    assert isinstance(h.SYNC_FIELDS, frozenset)
    assert h.SYNC_FIELDS == {'last_sync_at', 'last_status', 'last_error', 'etag', 'last_modified', 'playlist_rev',
                             'links_mtime_ns', 'fail_count', 'failed_since', 'epg_etag', 'epg_last_modified',
                             'epg_fail_count', 'm3u_source_key', 'epg_source_key', 'config_key'}
    assert h.ACTION_FIELDS == frozenset({'tombstones'})
    assert h.DERIVED_FIELDS == frozenset({'next_retry_at', 'display_status'})
    assert h.BOOL_FIELDS == ('respect_source_numbers', 'rediscover_deleted', 'enabled', 'detect_placeholder_pictures')
    assert h.ADDRESS_FIELDS == ('m3u_url', 'm3u_path', 'epg_url', 'epg_path')
    assert h.PLAYLIST_RESET == ('etag', 'last_modified', 'links_mtime_ns', 'fail_count', 'failed_since',
                                'last_status', 'last_error')
    assert h.GUIDE_RESET == ('epg_etag', 'epg_last_modified', 'epg_fail_count', 'last_error')
    assert h.PLAYLIST_GROUP == ('etag', 'last_modified', 'playlist_rev', 'links_mtime_ns', 'm3u_source_key',
                                'fail_count', 'failed_since', 'last_status', 'last_error')
    assert h.GUIDE_GROUP == ('epg_etag', 'epg_last_modified', 'epg_fail_count', 'epg_source_key', 'last_error')
    assert h.CONFIG_KEY_PENDING == 'pending'
    assert (h.START_NUM_RANGE, h.REFRESH_RANGE) == ((1, 9999), (0, 1440))
    assert h.ID_RE.pattern == r'^[a-z0-9_-]+$'


def test_tables_are_disjoint():
    tables = [h.DASHBOARD_FIELDS, h.SYNC_FIELDS, h.ACTION_FIELDS, h.DERIVED_FIELDS]
    for i, a in enumerate(tables):
        for b in tables[i + 1:]:
            assert not a.intersection(b), sorted(a.intersection(b))
    assert set(h.BOOL_FIELDS).union(h.ADDRESS_FIELDS) <= set(h.FORM_FIELDS)


def test_groups_and_resets_hold_sync_fields_only():
    """A Save resets only sync results of the changed address, never
    last_sync_at (so "never synced" can't come back), playlist_rev (a schema
    marker), the *_source_key (the next sync needs them to see the change),
    config_key or tombstones; a moved address never skips last_sync_at or
    config_key."""
    for keys in (h.PLAYLIST_RESET, h.GUIDE_RESET, h.PLAYLIST_GROUP, h.GUIDE_GROUP):
        assert set(keys) <= h.SYNC_FIELDS
    resets = set(h.PLAYLIST_RESET).union(h.GUIDE_RESET)
    assert not resets.intersection({'last_sync_at', 'playlist_rev', 'm3u_source_key', 'epg_source_key',
                                    'config_key', 'tombstones'})
    groups = set(h.PLAYLIST_GROUP).union(h.GUIDE_GROUP)
    assert not groups.intersection({'last_sync_at', 'config_key'})
    assert set(h.PLAYLIST_RESET) <= set(h.PLAYLIST_GROUP) and set(h.GUIDE_RESET) <= set(h.GUIDE_GROUP)


# Key names only (no values, no ids) of the three live import entries, read
# from channels.json on 2026-11-12 for the plan (understand.json).
LIVE_KEYS_ALL_THREE = {'id', 'name', 'enabled', 'm3u_url', 'm3u_path', 'epg_url', 'epg_path', 'start_num',
                       'refresh_interval_min', 'respect_source_numbers', 'rediscover_deleted', 'last_sync_at',
                       'last_status', 'last_error', 'fail_count', 'failed_since', 'playlist_rev'}
LIVE_KEYS_PLAYLIST_ADDON = LIVE_KEYS_ALL_THREE.union({'epg_etag', 'last_modified', 'links_mtime_ns'})


def test_every_live_key_has_exactly_one_owner():
    tables = [h.DASHBOARD_FIELDS, h.SYNC_FIELDS, h.ACTION_FIELDS, h.DERIVED_FIELDS]
    for key in LIVE_KEYS_PLAYLIST_ADDON.union({'tombstones'}):
        assert sum(1 for t in tables if key in t) == 1, key


def _updated_cfg_writes(src):
    """{key: [line, ...]} for every constant key assigned through
    updated_cfg[...] or <x>['updated_cfg'][...] in `src`."""
    def is_updated_cfg(node):
        if isinstance(node, ast.Name) and node.id == 'updated_cfg':
            return True
        if isinstance(node, ast.Subscript):
            sl = node.slice
            sl = getattr(sl, 'value', sl) if type(sl).__name__ == 'Index' else sl
            return isinstance(sl, ast.Constant) and sl.value == 'updated_cfg'
        return False
    found = {}
    for node in ast.walk(ast.parse(src)):
        targets = node.targets if isinstance(node, ast.Assign) else \
            [node.target] if isinstance(node, (ast.AugAssign, ast.AnnAssign)) else []
        for target in targets:
            for sub in ast.walk(target):
                if isinstance(sub, ast.Subscript) and is_updated_cfg(sub.value):
                    sl = sub.slice
                    sl = getattr(sl, 'value', sl) if type(sl).__name__ == 'Index' else sl
                    assert isinstance(sl, ast.Constant), 'updated_cfg key is not a constant at line %d' % sub.lineno
                    found.setdefault(sl.value, []).append(sub.lineno)
    return found


# The 12 keys the sync wrote at imports.68, and the 3 imports.69 adds: syncOne
# writes m3u_source_key (a 304 and a parsed 200) and epg_source_key (the
# cached guide download, and a keyless 304 that loaded the cached guide);
# syncAll writes config_key after every attempt.
SYNC_KEYS_WRITTEN_AT_68 = {'last_sync_at', 'last_status', 'last_error', 'etag', 'last_modified', 'playlist_rev',
                           'links_mtime_ns', 'fail_count', 'failed_since', 'epg_etag', 'epg_last_modified',
                           'epg_fail_count'}
SYNC_KEYS_NEW_IN_69 = {'m3u_source_key', 'epg_source_key', 'config_key'}


def test_sync_fields_cover_every_key_the_sync_writes():
    """The sync's commit keeps only SYNC_FIELDS of its results, so a key
    written into updated_cfg but missing from the table would be dropped
    without a word. The scan can't pass empty: it must find every key the
    sync wrote at imports.68 plus the three imports.69 adds -- today that is
    the whole table."""
    src = _src('imports.py')
    written = _updated_cfg_writes(src)
    assert set(written) <= h.SYNC_FIELDS, sorted(set(written).difference(h.SYNC_FIELDS))
    expected = SYNC_KEYS_WRITTEN_AT_68.union(SYNC_KEYS_NEW_IN_69)
    assert expected <= set(written), sorted(expected.difference(written))
    assert expected == h.SYNC_FIELDS
    # Only subscript writes can be scanned: no updated_cfg.update(...) / .setdefault(...).
    for node in ast.walk(ast.parse(src)):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ('update', 'setdefault')):
            owner = node.func.value
            assert not (isinstance(owner, ast.Name) and owner.id == 'updated_cfg'), node.lineno
            if isinstance(owner, ast.Subscript):
                sl = owner.slice
                sl = getattr(sl, 'value', sl) if type(sl).__name__ == 'Index' else sl
                assert not (isinstance(sl, ast.Constant) and sl.value == 'updated_cfg'), node.lineno


def test_missing_marker():
    assert repr(MISSING) == 'MISSING'
    assert copy.copy(MISSING) is MISSING and copy.deepcopy({'k': MISSING})['k'] is MISSING
    assert MISSING is not None and MISSING != None and MISSING != ''     # noqa: E711


# ================================================================ small helpers


@pytest.mark.parametrize('entry, kind, expected', [
    ({'m3u_url': 'u', 'm3u_path': 'p'}, 'playlist', 'u'),                # the URL wins, as in syncOne
    ({'m3u_url': '', 'm3u_path': 'p'}, 'playlist', 'p'),
    ({'m3u_url': None, 'm3u_path': 'p'}, 'playlist', 'p'),
    ({}, 'playlist', ''),
    ({'epg_url': 'g', 'epg_path': 'q'}, 'guide', 'g'),
    ({'epg_path': 'q'}, 'guide', 'q'),
    ({'m3u_url': 'u'}, 'guide', ''),
    (None, 'guide', ''),
])
def test_effective_source(entry, kind, expected):
    assert h.effective_source(entry, kind) == expected


def test_source_key():
    a, b = 'https://playlist.example/a.m3u?token=SECRET', 'https://playlist.example/b.m3u'
    ka = h.source_key(a)
    assert len(ka) == 16 and all(c in '0123456789abcdef' for c in ka)
    assert ka == h.source_key(a) and ka != h.source_key(b)
    assert 'SECRET' not in ka and 'example' not in ka
    assert h.source_key('') == '' and h.source_key(None) == ''
    assert h.source_key('special://profile/x.m3u') == h.source_key('special://profile/x.m3u')


@pytest.mark.parametrize('stored, new, expected', [
    ('aaaa', 'bbbb', True), ('aaaa', 'aaaa', False),
    (None, 'bbbb', False), ('', 'bbbb', False),           # an entry from before imports.69: no change
    ('aaaa', '', False), (None, None, False),
])
def test_source_changed(stored, new, expected):
    assert h.source_changed(stored, new) is expected


def test_config_fingerprint_reads_the_settings_only():
    entry = synced('alpha', catchup_days=7)
    fp = h.config_fingerprint(entry)
    assert len(fp) == 16
    reordered = dict(reversed(list(entry.items())))
    assert h.config_fingerprint(reordered) == fp                       # key order doesn't matter
    assert h.config_fingerprint(dict(entry, id='other')) == fp         # nor the id
    for key, value in (('last_sync_at', T0 + 9), ('etag', 'x'), ('config_key', 'pending'),
                       ('tombstones', ['a@alpha']), ('next_retry_at', 1), ('unknown', 2)):
        assert h.config_fingerprint(dict(entry, **{key: value})) == fp, key
    for key, value in (('refresh_interval_min', 590), ('enabled', False), ('catchup_days', 6),
                       ('detect_placeholder_pictures', False), ('name', 'Alpha 2')):
        assert h.config_fingerprint(dict(entry, **{key: value})) != fp, key
    # presence counts: a box missing from the entry differs from any value
    assert h.config_fingerprint(_with(dict(entry), {'detect_placeholder_pictures': MISSING})) != fp


@pytest.mark.parametrize('config_key, expected', [
    (MISSING, False),           # entries from before imports.69 count as up to date
    (None, False), ('', False),
    ('pending', True),          # a dashboard change
    ('fingerprint', None),      # replaced by the real fingerprint: up to date
    ('0123456789abcdef', True),  # the settings changed since
])
def test_config_changed(config_key, expected):
    entry = synced('alpha')
    if config_key == 'fingerprint':
        entry['config_key'] = h.config_fingerprint(entry)
        expected = False
    elif config_key is not MISSING:
        entry['config_key'] = config_key
    assert h.config_changed(entry) is expected


@pytest.mark.parametrize('value, expected', [
    ('movistarplus', True), ('epg-best_2', True), ('a', True),
    ('', False), ('Alpha', False), ('two words', False), ('tv.uno', False), ('a/b', False),
    ('alpha\n', False),           # Python's '$' alone would accept a trailing newline
    (None, False), (5, False), (['a'], False),
])
def test_is_valid_id(value, expected):
    assert h.is_valid_id(value) is expected


def test_stale_guide_caches():
    names = ['epg_alpha.xml', 'epg_beta.xml', 'epg_Odd Id.xml', 'epg_gone.xml', 'epg_.xml',
             'pseudotv.xml', 'epg_alpha.xml.tmp', 'epg_alpha.XML', 'logos', 'channels.json', None, 5]
    assert h.stale_guide_caches(names, {'alpha', 'beta', 'Odd Id', None, ''}) == ['epg_.xml', 'epg_gone.xml']
    assert h.stale_guide_caches(names, []) == ['epg_.xml', 'epg_Odd Id.xml', 'epg_alpha.xml',
                                               'epg_beta.xml', 'epg_gone.xml']
    assert h.stale_guide_caches(['epg_7.xml'], [7]) == []              # odd legacy ids compared as text
    assert h.stale_guide_caches(['epg_a/b.xml', 'epg_a\\b.xml'], []) == []
    assert h.stale_guide_caches([], {'alpha'}) == [] and h.stale_guide_caches(None, None) == []


def test_clean_stored():
    entry = synced('alpha', last_status='abandoned', next_retry_at=T0 + 60, display_status='abandoned',
                   tombstones=['a@alpha'])
    out = h.clean_stored(entry)
    assert 'next_retry_at' not in out and 'display_status' not in out
    assert out['last_status'] == 'failed'
    assert out['tombstones'] == ['a@alpha'] and out['tombstones'] is not entry['tombstones']
    assert h.clean_stored(synced('beta'))['last_status'] == 'ok'


def test_without_import_records():
    chans = [{'id': 'a@alpha', 'type': 'import', 'import_source': 'alpha'},
             {'id': 'b@beta', 'type': 'import', 'import_source': 'beta'},
             {'id': 'own@PseudoTV_Live', 'type': 'Custom', 'import_source': 'alpha'},   # not imported: kept
             {'id': 'odd', 'type': 'import', 'import_source': ['alpha']},             # hand-edited: kept
             'not a record']
    out = h.without_import_records(chans, {'alpha'})
    assert [c if isinstance(c, str) else c['id'] for c in out] == ['b@beta', 'own@PseudoTV_Live', 'odd',
                                                                   'not a record']
    assert out[0] is chans[1]
    assert h.without_import_records(chans, ()) == chans and h.without_import_records(None, {'x'}) == []


# ================================================================ normalise_form_entry


def test_normalise_keeps_dashboard_keys_only():
    """Sync results, tombstones, GET-only and unknown keys are dropped without
    an error (output-only fields are ignored, AIP-203). S1-M1."""
    raw = dict(synced('alpha', tombstones=['a@alpha'], next_retry_at=1, display_status='abandoned',
                      config_key='pending', m3u_source_key='k', unknown=1),
               catchup_mode='default', catchup_query_format='', catchup_days=7)
    out = h.normalise_form_entry(raw, new_id=False)
    assert set(out) <= h.DASHBOARD_FIELDS
    assert set(out) == set(h.FORM_FIELDS + h.CATCHUP_FIELDS)
    assert {k: out[k] for k in out} == {k: raw[k] for k in out}


def test_normalise_missing_keys_stay_missing():
    assert h.normalise_form_entry({'id': 'alpha'}) == {'id': 'alpha'}
    assert h.normalise_form_entry({'enabled': False}, new_id=False) == {'enabled': False}


@pytest.mark.parametrize('key, value, expected', [
    ('start_num', 1, 1), ('start_num', 9999, 9999), ('start_num', '800', 800),
    ('refresh_interval_min', 0, 0), ('refresh_interval_min', 1440, 1440),
    ('enabled', False, False), ('detect_placeholder_pictures', True, True),
    ('name', 'Alpha TV', 'Alpha TV'), ('name', '  Alpha  ', 'Alpha'),
    ('name', '', 'alpha'), ('name', None, 'alpha'), ('name', '   ', 'alpha'),   # empty: the id, as the form does
    ('m3u_url', '  https://playlist.example/a.m3u ', 'https://playlist.example/a.m3u'),
    ('m3u_path', '', ''), ('epg_path', None, None),
    ('catchup_mode', 'default', 'default'), ('catchup_query_format', None, None),
    ('catchup_days', 7, 7), ('catchup_days', None, None), ('catchup_days', 0, 0),
])
def test_normalise_accepts(key, value, expected):
    assert h.normalise_form_entry({'id': 'alpha', key: value})[key] == expected


@pytest.mark.parametrize('key, value', [
    ('start_num', 0), ('start_num', 10000), ('start_num', -1), ('start_num', True), ('start_num', 800.0),
    ('start_num', '8a'), ('start_num', ' 800'), ('start_num', None), ('start_num', '٣'),
    ('refresh_interval_min', -1), ('refresh_interval_min', 1441), ('refresh_interval_min', False),
    ('refresh_interval_min', '60'), ('refresh_interval_min', 1.5),
    ('enabled', 'true'), ('enabled', 1), ('enabled', None), ('respect_source_numbers', 'on'),
    ('rediscover_deleted', 0), ('detect_placeholder_pictures', 'false'),
    ('name', 5), ('m3u_url', 5), ('epg_path', ['x']),
    ('catchup_mode', 5), ('catchup_query_format', {}), ('catchup_days', 366), ('catchup_days', True),
    ('catchup_days', '7'),
])
def test_normalise_refuses(key, value):
    with pytest.raises(ImportRequestError) as err:
        h.normalise_form_entry({'id': 'alpha', key: value})
    assert err.value.status == 400
    err.value.message.encode('ascii')


@pytest.mark.parametrize('raw', [None, [], 'alpha', 5])
def test_normalise_refuses_a_non_object(raw):
    with pytest.raises(ImportRequestError) as err:
        h.normalise_form_entry(raw)
    assert err.value.status == 400


def test_normalise_checks_non_empty_addresses():
    seen = []

    def check(value):
        seen.append(value)
        if value.startswith('ftp:'):
            raise ValueError("unsupported scheme 'ftp' (allowed: http, https, special)")
        return value

    out = h.normalise_form_entry({'id': 'alpha', 'm3u_url': ' https://playlist.example/a.m3u ', 'm3u_path': '',
                                  'epg_url': 'https://guide.example/a.xml', 'epg_path': None}, check)
    assert seen == ['https://playlist.example/a.m3u', 'https://guide.example/a.xml']    # stripped; '' and None skipped
    assert out['m3u_url'] == 'https://playlist.example/a.m3u'
    with pytest.raises(ImportRequestError) as err:
        h.normalise_form_entry({'id': 'alpha', 'epg_url': 'ftp://guide.example/a.xml'}, check)
    assert err.value.status == 400
    assert "unsupported scheme 'ftp'" in err.value.message and 'guide URL' in err.value.message
    assert isinstance(err.value.__cause__, ValueError)                    # raised from the check's error
    assert 'guide.example' not in err.value.message


@pytest.mark.parametrize('iid', ['Alpha', 'two words', '', None, 5, 'tv.uno', 'alpha\n', 'café'])
def test_normalise_new_id_must_match_the_pattern(iid):
    """S1-M34: the server checks a new Identifier itself; the message never
    echoes it."""
    with pytest.raises(ImportRequestError) as err:
        h.normalise_form_entry({'id': iid, 'name': 'X'}, new_id=True)
    assert err.value.status == 400 and err.value.message == h.MSG_BAD_ID
    if isinstance(iid, str) and iid:
        assert iid.strip() not in err.value.message


@pytest.mark.parametrize('iid', ['Odd Id', 'tv.uno', 'café', 'alpha'])
def test_normalise_existing_id_is_taken_as_stored(iid):
    """A legacy id outside the pattern stays editable."""
    assert h.normalise_form_entry({'id': iid, 'name': ''}, new_id=False) == {'id': iid, 'name': iid}


# ================================================================ merge_dashboard_entry

_SYNC_AND_ACTION = sorted(h.SYNC_FIELDS) + ['tombstones']


def _stale_value(key):
    return {'tombstones': ['zz@alpha'], 'last_sync_at': 1}.get(key, 'stale-page-value')


def _disk_for(key):
    """A stored entry that holds `key` (every sync key, the source keys, a
    config_key and tombstones)."""
    disk = synced('alpha', tombstones=['a@alpha'], m3u_source_key='m' * 16, epg_source_key='e' * 16,
                  config_key='c' * 16)
    disk.setdefault(key, 'disk-%s' % key)
    return disk


@pytest.mark.parametrize('key', _SYNC_AND_ACTION)
def test_merge_keeps_disk_sync_values_on_a_settings_change(key):
    """S1-M1b: merge_dashboard_entry takes only dashboard keys, even from a
    raw form that carries sync keys (a stale page value is ignored)."""
    disk = _disk_for(key)
    merged, changed, reset = h.merge_dashboard_entry(disk, {'name': 'Alpha 2', key: _stale_value(key)})
    assert changed == ['name'] and reset == []
    if key == 'config_key':
        assert merged[key] == 'pending'
    else:
        assert merged[key] == disk[key]


@pytest.mark.parametrize('key', _SYNC_AND_ACTION)
def test_merge_ignores_a_stale_page_value_without_a_change(key):
    disk = _disk_for(key)
    merged, changed, reset = h.merge_dashboard_entry(disk, {key: _stale_value(key)})
    assert (changed, reset) == ([], []) and merged == h.clean_stored(disk)


@pytest.mark.parametrize('key', _SYNC_AND_ACTION)
def test_merge_absent_key_stays_absent(key):
    disk = _with(_disk_for(key), {key: MISSING})
    merged, changed, _ = h.merge_dashboard_entry(disk, {'name': 'Alpha 2', key: _stale_value(key)})
    if key == 'config_key':
        assert merged[key] == 'pending'
    else:
        assert key not in merged


@pytest.mark.parametrize('key', h.CATCHUP_FIELDS)
def test_merge_keeps_hand_set_catchup_keys(key):
    """The form has no catch-up boxes: a Save keeps them (before imports.69 it wiped them)."""
    disk = synced('alpha', catchup_mode='default', catchup_query_format='&utc={utc}', catchup_days=7)
    merged, _, _ = h.merge_dashboard_entry(disk, form_item(disk, refresh_interval_min=590))
    assert merged[key] == disk[key]
    merged, _, _ = h.merge_dashboard_entry(synced('alpha'), form_item(disk, refresh_interval_min=590))
    assert key not in merged


def _removed(disk, merged):
    return {k for k in disk if k not in merged}


@pytest.mark.parametrize('form', [
    {'m3u_url': 'https://playlist.example/new.m3u'},
    {'m3u_url': ''},                                                       # the file path takes over
    {'m3u_url': 'https://playlist.example/p.m3u', 'm3u_path': 'https://playlist.example/alpha.m3u'},   # swapped
], ids=['url changed', 'url cleared', 'url and path swapped'])
def test_merge_playlist_change_resets_exactly_the_playlist_group(form):
    disk = synced('alpha', m3u_path='https://playlist.example/p.m3u', m3u_source_key='m' * 16,
                  epg_source_key='e' * 16, epg_fail_count=2, tombstones=['a@alpha'])
    merged, changed, reset = h.merge_dashboard_entry(disk, form)
    assert reset == ['playlist']
    assert _removed(disk, merged) == set(h.PLAYLIST_RESET)
    assert merged['config_key'] == 'pending'
    for key in ('last_sync_at', 'playlist_rev', 'm3u_source_key', 'epg_source_key', 'tombstones',
                'epg_etag', 'epg_last_modified', 'epg_fail_count'):
        assert merged[key] == disk[key], key


def test_merge_path_change_under_a_url_resets_nothing():
    """The URL wins, so a new file path behind it changes no address."""
    disk = synced('alpha', m3u_path='special://profile/a.m3u')
    merged, changed, reset = h.merge_dashboard_entry(disk, {'m3u_path': 'special://profile/b.m3u'})
    assert changed == ['m3u_path'] and reset == [] and merged['etag'] == disk['etag']
    assert merged['config_key'] == 'pending'


@pytest.mark.parametrize('form', [{'epg_url': 'https://guide.example/new.xml'},
                                  {'epg_url': '', 'epg_path': 'special://profile/guide.xml'}])
def test_merge_guide_change_resets_exactly_the_guide_group(form):
    disk = synced('alpha', epg_fail_count=3, last_error='guide download failed', epg_source_key='e' * 16,
                  m3u_source_key='m' * 16)
    merged, changed, reset = h.merge_dashboard_entry(disk, form)
    assert reset == ['guide']
    assert _removed(disk, merged) == set(h.GUIDE_RESET)
    for key in ('last_sync_at', 'epg_source_key', 'm3u_source_key', 'etag', 'last_status', 'fail_count'):
        assert merged[key] == disk[key], key


def test_merge_both_addresses_never_reset_the_sync_time_or_keys():
    """S1-M13: both groups reset; last_sync_at and the source keys stay."""
    disk = synced('alpha', m3u_source_key='m' * 16, epg_source_key='e' * 16)
    merged, changed, reset = h.merge_dashboard_entry(
        disk, {'m3u_url': 'https://playlist.example/n.m3u', 'epg_url': 'https://guide.example/n.xml'})
    assert reset == ['playlist', 'guide'] and changed == ['epg_url', 'm3u_url']
    assert _removed(disk, merged) == set(h.PLAYLIST_RESET).union(h.GUIDE_RESET)
    assert (merged['last_sync_at'], merged['m3u_source_key'], merged['epg_source_key']) == (T0, 'm' * 16, 'e' * 16)


def test_merge_without_a_change_marks_nothing():
    """S1-M15 (the other half): no change, no 'pending'."""
    disk = synced('alpha')
    merged, changed, reset = h.merge_dashboard_entry(disk, form_item(disk))
    assert (changed, reset) == ([], []) and 'config_key' not in merged
    merged, changed, _ = h.merge_dashboard_entry(disk, {'id': 'renamed'})          # the id is never merged
    assert changed == [] and merged['id'] == 'alpha'


def test_merge_any_change_marks_pending():
    """S1-M15: a change of any box writes config_key='pending', which forces
    that import's next sync cycle."""
    for key, value in (('name', 'Alpha 2'), ('refresh_interval_min', 590), ('enabled', False),
                       ('detect_placeholder_pictures', False), ('start_num', 101)):
        disk = synced('alpha', config_key='0123456789abcdef')
        merged, changed, reset = h.merge_dashboard_entry(disk, {key: value})
        assert changed == [key] and reset == [] and merged['config_key'] == 'pending', key
        assert h.config_changed(merged)


def test_merge_a_box_missing_on_disk_counts_as_changed():
    disk = synced('alpha', detect_placeholder_pictures=MISSING)
    merged, changed, _ = h.merge_dashboard_entry(disk, {'detect_placeholder_pictures': True})
    assert changed == ['detect_placeholder_pictures'] and merged['detect_placeholder_pictures'] is True


def test_merge_an_explicit_null_for_a_key_missing_on_disk_counts_as_changed():
    """A key missing on disk differs from any value, null included (MISSING
    is not None): the null is stored and the import marked 'pending'."""
    disk = synced('alpha')                                              # no catchup_* keys
    merged, changed, _ = h.merge_dashboard_entry(disk, {'catchup_mode': None})
    assert changed == ['catchup_mode'] and merged['catchup_mode'] is None and merged['config_key'] == 'pending'


def test_merge_cleans_the_stored_entry():
    disk = synced('alpha', last_status='abandoned', next_retry_at=T0, display_status='abandoned')
    merged, _, _ = h.merge_dashboard_entry(disk, {'refresh_interval_min': 590})
    assert merged['last_status'] == 'failed' and 'next_retry_at' not in merged and 'display_status' not in merged


# ================================================================ the new page: one operation


def _disk3():
    return [synced('alpha', tombstones=['a1@alpha']), synced('beta', catchup_days=7), never_synced('gamma')]


def test_op_add_appends_the_form_entry():
    fresh = _disk3()
    entry = dict(form_item({'id': 'delta'}), last_sync_at=T0, tombstones=['x'], next_retry_at=1)
    new, summary = h.apply_dashboard_request(fresh, {'op': 'add', 'entry': entry})
    assert [e['id'] for e in new] == ['alpha', 'beta', 'gamma', 'delta']          # list order is sync priority
    assert new[:3] == fresh
    assert set(new[3]) == set(h.FORM_FIELDS)                                       # no sync fields: the gate is open
    assert summary == {'op': 'add', 'id': 'delta', 'changed': True, 'fields': sorted(set(h.FORM_FIELDS) - {'id'}),
                       'reset': [], 'removed_ids': []}


def test_op_add_refuses_a_used_identifier():
    """S1-M35: the only message that names an id, after it passed ID_RE."""
    err = refused(_disk3(), {'op': 'add', 'entry': form_item({'id': 'beta'})}, 409)
    assert err.message == 'Identifier "beta" is already used by another import'


def test_op_add_refuses_a_bad_identifier():
    err = refused(_disk3(), {'op': 'add', 'entry': form_item({'id': 'New Import'})}, 400)
    assert err.message == h.MSG_BAD_ID


def test_op_edit_keeps_every_sync_field_and_the_tombstones():
    fresh = _disk3()
    new, summary = h.apply_dashboard_request(
        fresh, {'op': 'edit', 'id': 'alpha', 'entry': {'id': 'alpha', 'refresh_interval_min': 590}})
    assert summary == {'op': 'edit', 'id': 'alpha', 'changed': True, 'fields': ['refresh_interval_min'],
                       'reset': [], 'removed_ids': []}
    expected = dict(fresh[0], refresh_interval_min=590, config_key='pending')
    assert new[0] == expected and new[1:] == fresh[1:]


def test_op_edit_of_an_address_resets_that_address_only():
    fresh = _disk3()
    new, summary = h.apply_dashboard_request(
        fresh, {'op': 'edit', 'id': 'alpha', 'entry': {'epg_url': 'https://guide.example/other.xml'}})
    assert summary['reset'] == ['guide'] and summary['fields'] == ['epg_url']
    assert _removed(fresh[0], new[0]) == set(h.GUIDE_RESET)
    assert new[0]['last_sync_at'] == T0 and new[0]['config_key'] == 'pending'


def test_op_edit_without_a_change_writes_nothing():
    fresh = _disk3()
    new, summary = h.apply_dashboard_request(fresh, {'op': 'edit', 'id': 'alpha', 'entry': form_item(fresh[0])})
    assert summary['changed'] is False and new == fresh


def test_op_edit_can_not_change_the_identifier():
    """S1-M14: the server-side Identifier lock."""
    err = refused(_disk3(), {'op': 'edit', 'id': 'alpha', 'entry': {'id': 'alpha2', 'name': 'A'}}, 400)
    assert err.message == h.MSG_ID_LOCKED


def test_op_edit_of_an_import_deleted_elsewhere():
    err = refused(_disk3(), {'op': 'edit', 'id': 'zeta', 'entry': {'name': 'Z'}}, 404)
    assert err.message == h.MSG_GONE


def test_op_edit_of_a_duplicated_identifier():
    fresh = _disk3() + [synced('beta', name='Second beta')]
    err = refused(fresh, {'op': 'edit', 'id': 'beta', 'entry': {'name': 'B'}}, 409)
    assert err.message == h.MSG_DUPLICATE_ID


def test_op_edit_and_delete_of_a_legacy_identifier():
    """Ids outside the pattern (older versions, hand edits) stay editable and deletable."""
    fresh = [synced('Odd Id'), synced('alpha')]
    new, summary = h.apply_dashboard_request(fresh, {'op': 'edit', 'id': 'Odd Id', 'entry': {'name': 'Renamed'}})
    assert summary['changed'] and new[0]['name'] == 'Renamed' and new[0]['id'] == 'Odd Id'
    new, summary = h.apply_dashboard_request(fresh, {'op': 'delete', 'id': 'Odd Id'})
    assert [e['id'] for e in new] == ['alpha'] and summary['removed_ids'] == ['Odd Id']


def test_op_delete():
    fresh = _disk3() + [synced('beta', name='Second beta')]
    new, summary = h.apply_dashboard_request(fresh, {'op': 'delete', 'id': 'beta'})
    assert [e['id'] for e in new] == ['alpha', 'gamma']                # every entry with that id
    assert summary == {'op': 'delete', 'id': 'beta', 'changed': True, 'fields': [], 'reset': [],
                       'removed_ids': ['beta']}


def test_op_delete_of_a_missing_import_changes_nothing():
    """A double click is harmless."""
    fresh = _disk3()
    new, summary = h.apply_dashboard_request(fresh, {'op': 'delete', 'id': 'zeta'})
    assert new == fresh and summary['changed'] is False and summary['removed_ids'] == []


@pytest.mark.parametrize('payload', [
    {'op': 'rename', 'id': 'alpha'}, {'op': None}, {}, None, 'alpha', 5, True,
    {'op': 'edit', 'id': '', 'entry': {}}, {'op': 'edit', 'id': 5, 'entry': {}},
    {'op': 'edit', 'id': 'alpha', 'entry': 'x'}, {'op': 'edit', 'id': 'alpha'},
    {'op': 'add'}, {'op': 'add', 'entry': ['alpha']},
    {'op': 'delete'}, {'op': 'delete', 'id': ['alpha']},
])
def test_bad_requests(payload):
    refused(_disk3(), payload, 400)


@pytest.mark.parametrize('odd', ['電視 ☃', 'Odd Id'], ids=['not latin-1', 'ascii legacy'])
def test_messages_never_name_a_legacy_identifier(odd):
    """S1-M48: send_error writes the message into the latin-1 HTTP status
    line; a legacy id (outside the pattern, possibly not latin-1) must never
    reach it. Duplicated, then edited: the fixed messages, nothing else."""
    fresh = [synced('alpha'), dict(synced('alpha'), id=odd), dict(synced('beta'), id=odd)]
    for payload, status, message in (
            ({'op': 'edit', 'id': odd, 'entry': {'name': 'x'}}, 409, h.MSG_DUPLICATE_ID),
            ({'op': 'edit', 'id': odd, 'entry': {'id': odd + 'x'}}, 400, h.MSG_ID_LOCKED),
            ({'op': 'add', 'entry': {'id': odd}}, 400, h.MSG_BAD_ID)):
        err = refused(fresh, payload, status)
        err.message.encode('latin-1')
        assert err.message == message and odd not in err.message


def test_error_message_guard():
    err = ImportRequestError(400, 'bad\r\nvalue ☃')
    assert err.message == 'bad  value ?' and str(err) == err.message and isinstance(err, ValueError)


def _all_messages():
    """Every refusal text: the MSG_* constants (with a sample value where
    they take one) and the ones normalise_form_entry builds."""
    texts = []
    for name in sorted(n for n in dir(h) if n.startswith('MSG_')):
        text = getattr(h, name)
        texts.append(text % ('x',) if '%s' in text else text)
    for key, value in (('m3u_url', 5), ('enabled', 'yes'), ('catchup_mode', 5), ('catchup_days', -1),
                       ('start_num', 0), ('refresh_interval_min', -1), ('name', 5)):
        with pytest.raises(ImportRequestError) as err:
            h.normalise_form_entry({'id': 'alpha', key: value})
        texts.append(err.value.message)
    return texts


def test_no_message_ends_with_a_full_stop():
    """http.server's error page adds one ('<p>Message: %(message)s.</p>') and
    both pages show the text with it: a message ending in '.' was shown as
    "... Reload the page.." (review fix)."""
    texts = _all_messages()
    assert len(texts) == 24
    assert [t for t in texts if t.rstrip().endswith('.')] == []
    for text in texts:
        text.encode('ascii')


def test_the_duplicate_identifier_message_says_what_delete_does():
    """Delete removes every entry with that id, with all its channels and its
    guide file (_op_delete, Channels.saveImportRequest), so the refusal must
    not suggest deleting "one of them" (review fix)."""
    assert h.MSG_DUPLICATE_ID == ('Two imports use this Identifier. Delete removes both, with their channels; '
                                  'then add the import again')
    fresh = _disk3() + [synced('beta', name='Second beta')]
    refused(fresh, {'op': 'edit', 'id': 'beta', 'entry': {'name': 'B'}}, 409)
    new, summary = h.apply_dashboard_request(fresh, {'op': 'delete', 'id': 'beta'})
    assert [e['id'] for e in new] == ['alpha', 'gamma'] and summary['removed_ids'] == ['beta']


# ================================================================ a tab opened before imports.69


def _old_disk():
    """09:00: what an old tab loaded (alpha, beta synced; beta has tombstones and catch-up)."""
    return [synced('alpha'), synced('beta', tombstones=['b1@beta'], catchup_mode='default',
                                    catchup_query_format='&utc={utc}', catchup_days=7)]


def test_old_tab_edit_is_accepted():
    disk = _old_disk()
    new, summary = h.apply_dashboard_request(disk, old_edit(get_copy(disk), 'alpha', refresh_interval_min=590))
    assert (summary['op'], summary['id'], summary['changed'], summary['fields']) == \
        ('edit', 'alpha', True, ['refresh_interval_min'])
    assert new[0] == dict(disk[0], refresh_interval_min=590, config_key='pending')
    assert new[1] == disk[1]


def test_old_tab_add_is_accepted():
    disk = _old_disk()
    new, summary = h.apply_dashboard_request(disk, old_add(get_copy(disk), new_form_item('delta')))
    assert summary['op'] == 'add' and [e['id'] for e in new] == ['alpha', 'beta', 'delta']
    assert new[:2] == disk


def test_old_tab_delete_is_accepted():
    disk = _old_disk()
    new, summary = h.apply_dashboard_request(disk, old_delete(get_copy(disk), 'alpha'))
    assert summary['op'] == 'delete' and summary['removed_ids'] == ['alpha'] and new == [disk[1]]


def test_old_tab_no_change_save_writes_nothing():
    """beta stores hand-set catch-up keys the form doesn't have: its
    form-built item is compared on its own keys, so this is no change at all
    (op 'none'), not an Edit that happens to change nothing."""
    disk = _old_disk()
    new, summary = h.apply_dashboard_request(disk, old_edit(get_copy(disk), 'beta'))
    assert summary['changed'] is False and new == disk
    assert summary['op'] == 'none'


def test_old_tab_first_save_stores_the_pictures_box():
    """imports.69 addendum 6 (V7): none of the live imports stores
    detect_placeholder_pictures and the old page always sends it, so an old
    tab's first "no-change" Save stores that box, marks the import 'pending'
    (it syncs once) and changes nothing else."""
    disk = [synced('alpha', detect_placeholder_pictures=MISSING), synced('beta', detect_placeholder_pictures=MISSING)]
    new, summary = h.apply_dashboard_request(disk, old_edit(get_copy(disk), 'alpha'))
    assert (summary['op'], summary['fields'], summary['reset']) == ('edit', ['detect_placeholder_pictures'], [])
    assert new[0] == dict(disk[0], detect_placeholder_pictures=True, config_key='pending')
    assert new[1] == disk[1]


@pytest.mark.parametrize('elsewhere', [never_synced('gamma'), synced('gamma')], ids=['never synced', 'synced'])
def test_old_tab_no_change_save_while_an_import_was_added_elsewhere(elsewhere):
    """S1-M41: the page misses gamma; its no-change Save must not delete it."""
    page = old_edit(get_copy(_old_disk()), 'alpha')
    refused(_old_disk() + [elsewhere], page, 409)


def test_old_tab_no_change_save_while_another_import_changed_elsewhere():
    """S1-M41: beta's verbatim copy no longer matches disk: refused, not reverted."""
    page = old_edit(get_copy(_old_disk()), 'alpha')
    now = _old_disk()
    now[1]['refresh_interval_min'] = 30
    err = refused(now, page, 409)
    assert err.message == h.MSG_OUT_OF_DATE


def test_old_tab_delete_next_to_a_never_synced_import_is_refused():
    """A never-synced import's GET copy looks form-built, so the Delete can't
    be told from a no-change Save that misses an import: refused (safe)."""
    disk = _old_disk() + [never_synced('gamma')]
    refused(disk, old_delete(get_copy(disk), 'alpha'), 409)


def test_old_tab_delete_while_an_import_was_added_elsewhere():
    page = old_delete(get_copy(_old_disk()), 'beta')
    refused(_old_disk() + [synced('gamma')], page, 409)


def test_old_tab_edit_next_to_a_never_synced_copy_applies_only_that_edit():
    disk = _old_disk() + [never_synced('gamma')]
    new, summary = h.apply_dashboard_request(disk, old_edit(get_copy(disk), 'beta', name='Beta TV'))
    assert summary['id'] == 'beta' and summary['fields'] == ['name']
    assert new[1]['name'] == 'Beta TV' and new[1]['catchup_days'] == 7 and new[1]['tombstones'] == ['b1@beta']
    assert new[0] == disk[0] and new[2] == disk[2]


def test_old_tab_two_edits_are_refused():
    """Two form-built items that differ from disk (here: the edit, and a
    never-synced import changed elsewhere): more than one change."""
    loaded = _old_disk() + [never_synced('gamma')]
    now = copy.deepcopy(loaded)
    now[2]['refresh_interval_min'] = 30
    refused(now, old_edit(get_copy(loaded), 'alpha', refresh_interval_min=590), 409)


def test_old_tab_an_add_next_to_a_second_change_is_refused():
    """S1-M12b: one change at a time on the Add path too. The tab's list (from
    an imports.68 add-on: no marker) holds a never-synced import that was
    deleted elsewhere, so its copy looks like a second Add. Accepting the
    first Add would re-add the deleted import and silently drop the new one
    (or the edit), and still say "Imports saved"."""
    loaded = _old_disk() + [never_synced('gamma')]
    refused(_old_disk(), old_add(get_copy(loaded), new_form_item('delta')), 409)
    refused(_old_disk(), old_edit(get_copy(loaded), 'beta', name='Beta TV'), 409)


# ---------------------------------------------------------------- lists from GET since imports.69 (marked)


def get_copy_69(entries):
    """What GET /imports.json gives a page since imports.69: as get_copy,
    and a never-synced import (only form keys) carries display_status
    'never_synced' (server.py). End to end in test_imports69_dashboard_save."""
    page = get_copy(entries)
    for item in page:
        if h.form_only(item):
            item['display_status'] = h.DISPLAY_NEVER_SYNCED
    return page


def test_form_only():
    assert h.form_only(never_synced('gamma')) and h.form_only({}) and h.form_only(form_item(synced('alpha')))
    assert not h.form_only(synced('alpha')) and not h.form_only(dict(never_synced('gamma'), catchup_days=7))
    assert not h.form_only(dict(never_synced('gamma'), display_status=h.DISPLAY_NEVER_SYNCED))
    assert not h.form_only('gamma') and not h.form_only(None) and not h.form_only(['id'])
    assert h.DISPLAY_NEVER_SYNCED == 'never_synced' and 'display_status' in h.DERIVED_FIELDS


@pytest.mark.parametrize('elsewhere', ['switched off', 'deleted'])
def test_old_tab_marked_never_synced_copy_is_checked_against_disk(elsewhere):
    """Review fix: with the marker the never-synced import's copy is a GET
    copy, not a form, so an old tab's no-change Save is refused instead of
    switching that import back on (or re-adding it) and saying "saved"."""
    loaded = _old_disk() + [never_synced('gamma')]
    page = old_edit(get_copy_69(loaded), 'alpha')
    now = copy.deepcopy(loaded)
    if elsewhere == 'switched off':
        now[2]['enabled'] = False
    else:
        del now[2]
    err = refused(now, page, 409)
    assert err.message == h.MSG_OUT_OF_DATE


def test_old_tab_marked_never_synced_copy_allows_delete_edit_and_add():
    """The third documented leftover goes for lists loaded since imports.69: a
    Delete next to a never-synced import is accepted. Editing that import
    itself still works (the form replaces its copy), and the marker is never
    stored."""
    loaded = _old_disk() + [never_synced('gamma')]
    new, summary = h.apply_dashboard_request(loaded, old_delete(get_copy_69(loaded), 'alpha'))
    assert (summary['op'], summary['removed_ids']) == ('delete', ['alpha']) and new == loaded[1:]
    new, summary = h.apply_dashboard_request(loaded, old_edit(get_copy_69(loaded), 'gamma', name='Gamma TV'))
    assert (summary['op'], summary['id'], summary['fields']) == ('edit', 'gamma', ['name'])
    assert new[2] == dict(loaded[2], name='Gamma TV', config_key='pending')
    new, summary = h.apply_dashboard_request(loaded, old_add(get_copy_69(loaded), new_form_item('delta')))
    assert summary['op'] == 'add' and new[:3] == loaded and new[3]['id'] == 'delta'
    new, summary = h.apply_dashboard_request(loaded, old_edit(get_copy_69(loaded), 'alpha'))
    assert summary == {'op': 'none', 'id': None, 'changed': False, 'fields': [], 'reset': [], 'removed_ids': []}


def test_old_tab_identifier_change_is_refused():
    """S1-M14: remove + add in one list is an Identifier change."""
    disk = _old_disk()
    err = refused(disk, old_edit(get_copy(disk), 'alpha', id='alpha2'), 400)
    assert err.message == h.MSG_LIST_ID_CHANGE


def test_old_tab_whose_list_failed_to_load():
    """The page's list was empty (GET failed): its Add posts [new]."""
    refused(_old_disk(), [new_form_item('delta')], 400)


def test_old_tab_stale_copy_of_an_import_deleted_elsewhere():
    """S1-M26: a verbatim (synced) copy of an import that is gone is never re-added."""
    page = old_edit(get_copy(_old_disk()), 'alpha')
    refused([_old_disk()[0]], page, 409)


def test_old_tab_verbatim_derived_keys_are_never_stored():
    disk = [synced('alpha'), synced('beta', last_status='failed', fail_count=3, failed_since=T0 - 7200)]
    page = get_copy(disk)
    page[1].update(last_status='abandoned', next_retry_at=T0 + 600, display_status='abandoned')
    new, summary = h.apply_dashboard_request(disk, old_edit(page, 'alpha', refresh_interval_min=590))
    assert summary['changed'] and new[1] == disk[1]
    assert 'next_retry_at' not in new[1] and new[1]['last_status'] == 'failed'


@pytest.mark.parametrize('page', [
    [form_item(synced('alpha')), form_item(synced('alpha'))],
    [form_item(synced('alpha')), {'name': 'no id'}],
    [form_item(synced('alpha')), dict(form_item(synced('alpha')), id='')],
    [form_item(synced('alpha')), 'alpha'],
    [{'id': 5}],
], ids=['duplicate ids', 'item without id', 'empty id', 'not an object', 'id not text'])
def test_old_tab_malformed_lists(page):
    refused(_old_disk(), page, 400)


def test_old_tab_keeps_disk_order():
    disk = [synced('alpha'), synced('beta'), synced('gamma')]
    new, _ = h.apply_dashboard_request(disk, old_edit(get_copy(disk), 'beta', name='B'))
    assert [e['id'] for e in new] == ['alpha', 'beta', 'gamma']
    page = get_copy(disk)
    page.reverse()                                                        # the page's order is never applied
    new, summary = h.apply_dashboard_request(disk, old_edit(page, 'gamma', name='G'))
    assert [e['id'] for e in new] == ['alpha', 'beta', 'gamma'] and new[2]['name'] == 'G'
    new, _ = h.apply_dashboard_request(disk, old_delete(get_copy(disk), 'beta'))
    assert [e['id'] for e in new] == ['alpha', 'gamma']


def test_old_tab_edit_keeps_entries_the_page_does_not_know():
    """Disk entries without a usable id (hand edits) are left as they are."""
    disk = _old_disk() + [{'name': 'no id here'}]
    page = old_edit(get_copy(_old_disk()), 'alpha', name='A')
    new, summary = h.apply_dashboard_request(disk, page)
    assert summary['changed'] and new[2] == {'name': 'no id here'}


# ---------------------------------------------------------------- leftovers, pinned (old tabs only)


def test_leftover_old_tab_edit_resends_every_box_of_that_import():
    """Accepted leftover: the old page sends all 12 boxes, so a change made
    elsewhere to another box of the SAME import is overwritten (reloading the
    tab avoids it). The new page sends only changed boxes."""
    loaded = _old_disk()
    now = copy.deepcopy(loaded)
    now[0]['name'] = 'Changed elsewhere'
    new, summary = h.apply_dashboard_request(now, old_edit(get_copy(loaded), 'alpha', refresh_interval_min=590))
    assert summary['fields'] == ['name', 'refresh_interval_min']
    assert new[0]['name'] == 'Alpha' and new[0]['refresh_interval_min'] == 590


def test_leftover_old_tab_edit_of_an_import_deleted_elsewhere_re_adds_it():
    """Accepted leftover: it looks exactly like an Add."""
    loaded = _old_disk()
    page = old_edit(get_copy(loaded), 'alpha', refresh_interval_min=590)
    new, summary = h.apply_dashboard_request([loaded[1]], page)
    assert summary['op'] == 'add' and [e['id'] for e in new] == ['beta', 'alpha']
    assert set(new[1]) == set(h.FORM_FIELDS) and new[1]['refresh_interval_min'] == 590


# ================================================================ the sync's commit


def result(start, status='ok', rediscovered=None, cache_written=False, **changes):
    """A syncOne result for `start`: updated_cfg is a copy of it with
    `changes` (MISSING removes a key)."""
    res = {'import_id': start['id'], 'status': status, 'updated_cfg': _with(copy.deepcopy(start), changes)}
    if rediscovered is not None:
        res['rediscovered'] = list(rediscovered)
    if cache_written:
        res['epg_cache_written'] = True
    return res


def starts(*entries):
    return {e['id']: copy.deepcopy(e) for e in entries}


def rec(cid, source=None):
    if source is None:
        return {'id': cid, 'type': 'Custom', 'number': 1}
    return {'id': cid, 'type': 'import', 'import_source': source, 'number': 800}


def plan(fresh, start_by_id, results, writer=(), mods=(), dels=(), channel_ids=()):
    return h.plan_sync_commit(fresh, set(channel_ids), start_by_id, results, list(writer), set(mods), set(dels))


def test_sync_delta():
    start = synced('alpha')
    updated = _with(dict(start, last_sync_at=T0 + 60, etag='"p2"', name='ignored', tombstones=['x'],
                         config_key='f' * 16), {'last_modified': MISSING})
    assert h.sync_delta(start, updated) == {'last_sync_at': T0 + 60, 'etag': '"p2"', 'last_modified': MISSING,
                                            'config_key': 'f' * 16}
    assert h.sync_delta(start, copy.deepcopy(start)) == {}


def test_sync_result_lands_on_the_entry_as_it_is_now():
    """S1-M2: a Save made while the sync ran (refresh 600 -> 590, 'pending')
    survives; the sync's bookkeeping is saved; the next cycle is forced
    because the settings differ from what the sync acted on."""
    start = synced('alpha')
    fresh = [dict(start, refresh_interval_min=590, config_key='pending'), synced('beta')]
    res = result(start, last_sync_at=T0 + 60, etag='"p2"', last_status='unchanged',
                 config_key=h.config_fingerprint(start))
    new, info = h.apply_sync_results(fresh, starts(start), {'alpha': res['updated_cfg']}, {})
    assert new[0]['refresh_interval_min'] == 590
    assert (new[0]['last_sync_at'], new[0]['etag'], new[0]['last_status']) == (T0 + 60, '"p2"', 'unchanged')
    assert new[0]['config_key'] == h.config_fingerprint(start) and h.config_changed(new[0])
    assert new[1] == fresh[1] and info['updated'] == {'alpha'}


def test_steady_state_keeps_pending_from_a_save_during_the_sync():
    """The start entry already held its fingerprint, so the result carries no
    new config_key and the 'pending' of a Save made meanwhile stays."""
    start = synced('alpha')
    start['config_key'] = h.config_fingerprint(start)
    fresh = [dict(start, name='Alpha 2', config_key='pending')]
    res = result(start, last_sync_at=T0 + 60, config_key=h.config_fingerprint(start))
    new, _ = h.apply_sync_results(fresh, starts(start), {'alpha': res['updated_cfg']}, {})
    assert new[0]['config_key'] == 'pending' and new[0]['last_sync_at'] == T0 + 60


@pytest.mark.parametrize('status', ['skipped', 'abandoned'])
def test_results_that_never_reached_syncone_are_ignored(status):
    """S1-M29: a gate-skipped or abandoned result is a copy of its start
    entry; a legacy entry (no config_key) saved during the sync keeps
    'pending', its settings and its sync time."""
    start = synced('alpha')
    fresh = [dict(start, refresh_interval_min=590, config_key='pending')]
    res = result(start, status=status)
    new, writer, mods, dels, info = plan(fresh, starts(start), {'alpha': res})
    assert new == fresh and info['updated'] == set()


def test_an_attempted_legacy_result_records_the_old_settings():
    """The first attempt after the update records the fingerprint of the
    settings it synced; a Save made meanwhile then still forces the next cycle."""
    start = synced('alpha')                                            # no config_key: from before imports.69
    fresh = [dict(start, refresh_interval_min=590, config_key='pending')]
    res = result(start, last_sync_at=T0 + 60, config_key=h.config_fingerprint(start))
    new, _, _, _, _ = plan(fresh, starts(start), {'alpha': res})
    assert new[0]['config_key'] == h.config_fingerprint(start) and h.config_changed(new[0])
    assert not h.config_changed(start)                                # before that: no key, not forced (S1-M10)


def test_import_deleted_during_the_sync_is_never_re_created():
    """S1-M4: its results and its channel records are dropped."""
    a, b = synced('alpha'), synced('beta')
    fresh = [a]
    results = {'alpha': result(a, last_sync_at=T0 + 60), 'beta': result(b, last_sync_at=T0 + 60)}
    writer = [rec('own@PseudoTV_Live'), rec('a1@alpha', 'alpha'), rec('b1@beta', 'beta')]
    new, w, mods, dels, info = plan(fresh, starts(a, b), results, writer,
                                    mods={'a1@alpha', 'b1@beta'}, channel_ids={'own@PseudoTV_Live', 'a1@alpha'})
    assert [e['id'] for e in new] == ['alpha'] and info['gone'] == {'beta'}
    assert [c['id'] for c in w] == ['own@PseudoTV_Live', 'a1@alpha'] and mods == {'a1@alpha'}
    assert info['drop_imports'] == {'beta'}


def test_import_added_during_the_sync_is_kept():
    """S1-M36."""
    a = synced('alpha')
    fresh = [a, never_synced('delta')]
    new, _, _, _, info = plan(fresh, starts(a), {'alpha': result(a, last_sync_at=T0 + 60)})
    assert new[1] == fresh[1] and 'delta' not in info['updated']


def test_playlist_moved_during_the_sync():
    """S1-M3: the old address's playlist results and channel changes are
    dropped; the sync time, config_key and the guide's results apply."""
    start = synced('alpha', m3u_source_key='m' * 16)
    saved = h.merge_dashboard_entry(start, {'m3u_url': 'https://playlist.example/new.m3u'})[0]
    res = result(start, rediscovered=['a9@alpha'], last_sync_at=T0 + 60, etag='"p2"', last_modified='x',
                 links_mtime_ns='9', playlist_rev=2, m3u_source_key='n' * 16, fail_count=0, failed_since=None,
                 last_status='ok', last_error=None, epg_etag='"g2"', config_key=h.config_fingerprint(start))
    writer = [rec('own@PseudoTV_Live'), rec('a1@alpha', 'alpha'), rec('a9@alpha', 'alpha')]
    new, w, mods, dels, info = plan([saved], starts(start), {'alpha': res}, writer, mods={'a1@alpha', 'a9@alpha'})
    entry = new[0]
    for key in h.PLAYLIST_GROUP:
        assert entry.get(key, MISSING) == saved.get(key, MISSING), key
    assert (entry['last_sync_at'], entry['config_key'], entry['epg_etag']) == \
        (T0 + 60, h.config_fingerprint(start), '"g2"')
    assert info['playlist_moved'] == {'alpha'} and info['dropped'] == {'alpha': {'a9@alpha'}}
    assert [c['id'] for c in w] == ['own@PseudoTV_Live'] and mods == set()


def test_guide_moved_during_the_sync():
    """S1-M32: the guide group is skipped; the playlist's results apply."""
    start = synced('alpha', epg_source_key='e' * 16)
    saved = h.merge_dashboard_entry(start, {'epg_url': 'https://guide.example/new.xml'})[0]
    saved['epg_fail_count'] = 1                                       # whatever disk holds now stays
    res = result(start, last_sync_at=T0 + 60, etag='"p2"', epg_etag='"g2"', epg_last_modified='y',
                 epg_fail_count=0, last_error=None, config_key=h.config_fingerprint(start))
    new, _, _, _, info = plan([saved], starts(start), {'alpha': res})
    entry = new[0]
    for key in h.GUIDE_GROUP:
        assert entry.get(key, MISSING) == saved.get(key, MISSING), key
    assert entry['etag'] == '"p2"' and entry['last_sync_at'] == T0 + 60
    assert entry['config_key'] == h.config_fingerprint(start) and h.config_changed(entry)   # next cycle forced
    assert info['guide_moved'] == {'alpha'} and info['playlist_moved'] == set()


def test_guide_moved_while_this_sync_replaced_the_cache_file():
    """S1-M31: the cache file now holds this sync's download of the old
    address: no stored guide validator may describe another download, and
    epg_source_key names the address the file came from (R7)."""
    start = synced('alpha', epg_source_key='a' * 16)
    saved = dict(h.merge_dashboard_entry(start, {'epg_url': 'https://guide.example/b.xml'})[0],
                 epg_etag='"stale"', epg_last_modified='stale')
    res = result(start, cache_written=True, last_sync_at=T0 + 60, epg_etag='"g2"', epg_last_modified='y',
                 epg_source_key=h.source_key(start['epg_url']))
    new, _, _, _, info = plan([saved], starts(start), {'alpha': res})
    assert 'epg_etag' not in new[0] and 'epg_last_modified' not in new[0]
    assert new[0]['epg_source_key'] == h.source_key(start['epg_url'])
    # without the flag the guide group on disk is left as it is
    res = result(start, last_sync_at=T0 + 60, epg_etag='"g2"', epg_source_key=h.source_key(start['epg_url']))
    new, _, _, _, _ = plan([saved], starts(start), {'alpha': res})
    assert (new[0]['epg_etag'], new[0]['epg_source_key']) == ('"stale"', 'a' * 16)
    # a cache-written result without a key leaves no key behind
    res = result(start, cache_written=True, last_sync_at=T0 + 60, epg_source_key=MISSING)
    new, _, _, _, _ = plan([saved], starts(start), {'alpha': res})
    assert 'epg_source_key' not in new[0] and 'epg_etag' not in new[0]


def test_guide_not_moved_applies_its_key_and_validators():
    start = synced('alpha')
    key = h.source_key(start['epg_url'])
    res = result(start, cache_written=True, last_sync_at=T0 + 60, epg_etag='"g2"', epg_source_key=key)
    new, _, _, _, _ = plan([copy.deepcopy(start)], starts(start), {'alpha': res})
    assert (new[0]['epg_etag'], new[0]['epg_source_key']) == ('"g2"', key)


@pytest.mark.parametrize('first, second', [('a', 'b'), ('b', 'a')], ids=['older last', 'newer last'])
def test_overlapping_syncs_newest_attempt_wins(first, second):
    """S1-M30: two syncs of the same import overlap (a Builder run that gave
    up waiting); whichever commits last, last_sync_at never goes backwards and
    the newer attempt's validators stay."""
    start = synced('alpha')
    out = {'a': result(start, last_sync_at=T0 + 50, etag='"older"', epg_etag='"older"'),
           'b': result(start, last_sync_at=T0 + 80, etag='"newer"', epg_etag='"newer"')}
    disk = [copy.deepcopy(start)]
    for name in (first, second):
        disk, _, _, _, info = plan(disk, starts(start), {'alpha': out[name]})
    assert (disk[0]['last_sync_at'], disk[0]['etag'], disk[0]['epg_etag']) == (T0 + 80, '"newer"', '"newer"')


def test_superseded_result_and_its_records_are_dropped():
    start = synced('alpha', rediscover_deleted=True, tombstones=['a9@alpha'])
    fresh = [dict(start, last_sync_at=T0 + 80, etag='"newer"')]
    res = result(start, rediscovered=['a9@alpha'], last_sync_at=T0 + 50, etag='"older"')
    writer = [rec('a1@alpha', 'alpha'), rec('a9@alpha', 'alpha'), rec('own@PseudoTV_Live')]
    new, w, mods, dels, info = plan(fresh, starts(start), {'alpha': res}, writer, mods={'a1@alpha', 'a9@alpha'})
    assert new == fresh and info['superseded'] == {'alpha'} and info['drop_imports'] == {'alpha'}
    assert [c['id'] for c in w] == ['own@PseudoTV_Live'] and mods == set()
    assert info['kept'] == {} and info['dropped'] == {'alpha': {'a9@alpha'}} and 'a9@alpha' in dels


def test_an_equal_sync_time_is_applied():
    start = synced('alpha')
    fresh = [dict(start, last_sync_at=T0 + 50, etag='"first"')]
    res = result(start, last_sync_at=T0 + 50, etag='"second"')
    new, _, _, _, info = plan(fresh, starts(start), {'alpha': res})
    assert new[0]['etag'] == '"second"' and info['superseded'] == set()


def test_entry_re_added_during_the_sync_takes_the_result():
    """Deleted and added again with the same Identifier while the sync ran
    (no last_sync_at yet): the result applies (rare; the next sync corrects)."""
    start = synced('alpha')
    fresh = [never_synced('alpha')]
    new, _, _, _, info = plan(fresh, starts(start), {'alpha': result(start, last_sync_at=T0 + 60)})
    assert new[0]['last_sync_at'] == T0 + 60 and info['updated'] == {'alpha'}


def test_a_key_the_sync_removed_is_removed():
    start = synced('alpha')
    res = result(start, last_sync_at=T0 + 60, etag=MISSING, last_modified=MISSING)
    new, _, _, _, _ = plan([copy.deepcopy(start)], starts(start), {'alpha': res})
    assert 'etag' not in new[0] and 'last_modified' not in new[0]


def test_rediscovered_channels_are_kept_while_the_box_is_ticked():
    """S1-M6: the re-created ids leave the tombstones in the same write and
    are never deleted by them; the key goes when it is empty."""
    start = synced('alpha', rediscover_deleted=True, tombstones=['a8@alpha', 'a9@alpha'])
    res = result(start, rediscovered=['a9@alpha'], last_sync_at=T0 + 60)
    writer = [rec('a9@alpha', 'alpha')]
    new, w, mods, dels, info = plan([copy.deepcopy(start)], starts(start), {'alpha': res}, writer,
                                    mods={'a9@alpha'}, dels={'a8@alpha', 'a9@alpha'})
    assert new[0]['tombstones'] == ['a8@alpha'] and info['kept'] == {'alpha': {'a9@alpha'}}
    assert dels == {'a8@alpha'} and mods == {'a9@alpha'} and [c['id'] for c in w] == ['a9@alpha']
    res = result(start, rediscovered=['a8@alpha', 'a9@alpha'], last_sync_at=T0 + 60)
    new, _, _, dels, _ = plan([copy.deepcopy(start)], starts(start), {'alpha': res}, dels={'a8@alpha', 'a9@alpha'})
    assert 'tombstones' not in new[0] and dels == set()


@pytest.mark.parametrize('case', ['unticked', 'gone', 'moved', 'superseded'])
def test_rediscovered_channels_are_dropped(case):
    start = synced('alpha', rediscover_deleted=True, tombstones=['a9@alpha'])
    fresh = [copy.deepcopy(start)]
    if case == 'unticked':
        fresh[0]['rediscover_deleted'] = False
    elif case == 'gone':
        fresh = []
    elif case == 'moved':
        fresh[0]['m3u_url'] = 'https://playlist.example/new.m3u'
    else:
        fresh[0]['last_sync_at'] = T0 + 90
    res = result(start, rediscovered=['a9@alpha'], last_sync_at=T0 + 60)
    writer = [rec('a9@alpha', 'alpha'), rec('own@PseudoTV_Live')]
    new, w, mods, dels, info = plan(fresh, starts(start), {'alpha': res}, writer, mods={'a9@alpha'})
    assert info['kept'] == {} and info['dropped'] == {'alpha': {'a9@alpha'}}
    assert [c['id'] for c in w] == ['own@PseudoTV_Live'] and mods == set()
    if fresh:
        assert fresh[0].get('tombstones') == ['a9@alpha'] and new[0]['tombstones'] == ['a9@alpha']
        assert 'a9@alpha' in dels


def test_every_entry_is_cleaned_at_a_sync_commit():
    """S1-M37: a stored next_retry_at / display_status / 'abandoned' (older
    versions) goes at the first sync commit, also for imports the sync skipped."""
    a = synced('alpha', last_status='abandoned', next_retry_at=T0 + 60)
    b = synced('beta', display_status='abandoned')
    new, _, _, _, _ = plan([a, b], starts(a, b), {'alpha': result(a, status='skipped'),
                                                   'beta': result(b, last_sync_at=T0 + 60)})
    assert new[0]['last_status'] == 'failed' and 'next_retry_at' not in new[0]
    assert 'display_status' not in new[1]


def test_deletes_are_every_tombstone_on_disk():
    """S1-M5: an Orphans made during the sync (a tombstone the sync didn't
    read) stays deleted; applied_deletes counts only ids that exist."""
    a = synced('alpha', tombstones=['a1@alpha'])
    fresh = [dict(a, tombstones=['a1@alpha', 'a2@alpha'])]                    # Orphans during the sync
    writer = [rec('own@PseudoTV_Live'), rec('a2@alpha', 'alpha'), rec('a3@alpha', 'alpha')]
    new, w, mods, dels, info = plan(fresh, starts(a), {'alpha': result(a, last_sync_at=T0 + 60)}, writer,
                                    mods={'a2@alpha', 'a3@alpha', 'not-written'}, dels={'a1@alpha'},
                                    channel_ids={'own@PseudoTV_Live', 'a2@alpha', 'a3@alpha'})
    assert dels == {'a1@alpha', 'a2@alpha'} and info['deleted_ids'] == dels
    assert info['applied_deletes'] == {'a2@alpha'}                           # a1 has no record anywhere
    assert mods == {'a2@alpha', 'a3@alpha'}                                  # limited to the writer list
    assert info['tombstones'] == {'a1@alpha', 'a2@alpha'}


def test_an_older_overlapping_sync_never_deletes_a_newer_syncs_re_discovery():
    """Review fix: two overlapping syncs both re-create a9 (Re-discover
    ticked). The newer one commits first: a9's record is written and a9
    leaves the tombstones. The older one is superseded and its re-discovery
    dropped; its start-of-sync tombstones (deleted_ids) must not delete the
    record the newer sync wrote (before: deleted, with no tombstone left, so
    the channel was missing until the next sync re-created it)."""
    start = synced('alpha', rediscover_deleted=True, tombstones=['a9@alpha'])
    fresh = [synced('alpha', rediscover_deleted=True, last_sync_at=T0 + 90)]       # the newer sync's commit
    res = result(start, rediscovered=['a9@alpha'], last_sync_at=T0 + 60)
    writer = [rec('a9@alpha', 'alpha'), rec('own@PseudoTV_Live')]
    new, w, mods, dels, info = plan(fresh, starts(start), {'alpha': res}, writer, mods={'a9@alpha'},
                                    dels={'a9@alpha'}, channel_ids={'a9@alpha', 'own@PseudoTV_Live'})
    assert info['superseded'] == {'alpha'} and info['dropped'] == {'alpha': {'a9@alpha'}}
    assert [c['id'] for c in w] == ['own@PseudoTV_Live'] and mods == set()
    assert dels == set() and info['deleted_ids'] == set() and info['applied_deletes'] == set()
    assert 'tombstones' not in new[0]


def test_gone_counts_only_results_that_reached_syncone():
    """An import the sync held back (gate-skipped) and that was deleted
    meanwhile has no results to drop: it is not 'gone' (no "was deleted
    during this sync" line for it)."""
    a, b = synced('alpha'), synced('beta')
    _, _, _, _, info = plan([copy.deepcopy(a)], starts(a, b), {'alpha': result(a, last_sync_at=T0 + 60),
                                                               'beta': result(b, status='skipped')})
    assert info['gone'] == set()
    _, _, _, _, info = plan([copy.deepcopy(a)], starts(a, b), {'alpha': result(a, last_sync_at=T0 + 60),
                                                               'beta': result(b, last_sync_at=T0 + 60)})
    assert info['gone'] == {'beta'}


def test_kept_tombstones_without_records_apply_no_delete():
    """The PVR refresh fires only when a delete was applied."""
    a = synced('alpha', tombstones=['a1@alpha'])
    _, _, _, dels, info = plan([copy.deepcopy(a)], starts(a), {'alpha': result(a, last_sync_at=T0 + 60)},
                               [rec('own@PseudoTV_Live')], dels={'a1@alpha'}, channel_ids={'own@PseudoTV_Live'})
    assert dels == {'a1@alpha'} and info['applied_deletes'] == set()


def test_plan_reads_only_results_that_reached_syncone():
    a, b, c = synced('alpha'), synced('beta'), synced('gamma')
    results = {'alpha': result(a, status='skipped', last_sync_at=T0 + 60, rediscovered=['x'], cache_written=True),
               'beta': result(b, status='abandoned', last_sync_at=T0 + 60),
               'gamma': result(c, status='failed', last_sync_at=T0 + 60, last_status='failed'),
               '': {'status': 'failed', 'updated_cfg': {'last_status': 'failed'}},     # an entry without an id
               'odd': 'not a result'}
    new, _, _, _, info = plan([a, b, c], starts(a, b, c), results)
    assert info['updated'] == {'gamma'} and new[:2] == [a, b] and new[2]['last_status'] == 'failed'
    assert info['kept'] == {} and info['dropped'] == {}


def test_sync_commit_handles_odd_disk_entries():
    a = synced('alpha')
    fresh = [a, 'not an entry', {'id': ['list id']}, {'name': 'no id'}]
    new, _, _, _, info = plan(fresh, starts(a), {'alpha': result(a, last_sync_at=T0 + 60)})
    assert new[1:] == fresh[1:] and new[0]['last_sync_at'] == T0 + 60


def test_sync_commit_output_shares_nothing_with_its_inputs():
    a = synced('alpha', tombstones=['a1@alpha'])
    start = starts(a)
    res = result(a, last_sync_at=T0 + 60, etag=['list', 'value'])
    fresh = [copy.deepcopy(a)]
    new, _, _, _, _ = plan(fresh, start, {'alpha': res})
    new[0]['tombstones'].append('x')
    new[0]['etag'].append('x')
    assert fresh[0]['tombstones'] == ['a1@alpha'] and res['updated_cfg']['etag'] == ['list', 'value']


# ================================================================ inputs are never modified


def _frozen(*values):
    return copy.deepcopy(values)


def test_dashboard_functions_leave_their_inputs_alone():
    disk = _old_disk() + [never_synced('gamma'), dict(synced('delta'), last_status='abandoned', next_retry_at=1)]
    payloads = [
        {'op': 'add', 'entry': new_form_item('zeta')},
        {'op': 'edit', 'id': 'alpha', 'entry': {'m3u_url': 'https://playlist.example/x.m3u', 'name': ' A '}},
        {'op': 'edit', 'id': 'delta', 'entry': {'refresh_interval_min': 5}},
        {'op': 'delete', 'id': 'beta'},
        old_edit(get_copy(disk), 'alpha', epg_url='https://guide.example/x.xml'),
        old_delete(get_copy(disk[:2]), 'beta'),
    ]
    for payload in payloads:
        snapshot = _frozen(disk, payload)
        try:
            h.apply_dashboard_request(disk, payload)
        except ImportRequestError:
            pass
        assert _frozen(disk, payload) == snapshot, payload
    raw = {'id': 'alpha', 'name': '  A ', 'm3u_url': ' u ', 'tombstones': ['x']}
    snapshot = _frozen(raw, disk[0])
    h.normalise_form_entry(raw, new_id=False)
    h.merge_dashboard_entry(disk[0], raw)
    h.clean_stored(disk[3])
    h.config_fingerprint(disk[0])
    assert _frozen(raw, disk[0]) == snapshot and disk[3]['last_status'] == 'abandoned'


def test_sync_functions_leave_their_inputs_alone():
    a = synced('alpha', rediscover_deleted=True, tombstones=['a1@alpha', 'a2@alpha'], next_retry_at=1)
    b = synced('beta')
    fresh = [dict(a, refresh_interval_min=5), b]
    start = starts(a, b)
    results = {'alpha': result(a, rediscovered=['a1@alpha'], cache_written=True, last_sync_at=T0 + 60),
               'beta': result(b, last_sync_at=T0 + 60, etag=MISSING)}
    writer = [rec('a1@alpha', 'alpha'), rec('own@PseudoTV_Live')]
    mods, dels, ids = {'a1@alpha'}, {'a1@alpha', 'a2@alpha'}, {'own@PseudoTV_Live'}
    snapshot = _frozen(fresh, start, results, writer, mods, dels, ids)
    h.plan_sync_commit(fresh, ids, start, results, writer, mods, dels)
    h.sync_delta(a, results['alpha']['updated_cfg'])
    h.without_import_records(writer, {'alpha'})
    h.stale_guide_caches(['epg_alpha.xml'], {'beta'})
    assert _frozen(fresh, start, results, writer, mods, dels, ids) == snapshot
