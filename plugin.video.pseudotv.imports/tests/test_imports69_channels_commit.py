# -*- coding: utf-8 -*-
"""imports.69: the Save fix -- Channels.commit, the one locked write of channels.json.

Before imports.69 every writer saved the copy of channels.json it had loaded
earlier: a dashboard Save wiped every import's sync info ("never synced"), a
running sync wrote back the import list it read before the Save, and a long-
lived copy (Builder's, loaded once per Kodi session) wrote back channel
records deleted since it loaded. Now every write is Channels.commit: a strict
fresh read, the writer's merge onto it and the atomic write, all under
channels_lock.

These tests use the REAL Channels class on a channels.json in tmp_path, with
channels.CACHE_LOC and imports.CACHE_LOC pointed at a tmp folder:
  - the lock through commit (no overlap, no lost update, re-entry, busy,
    exception, kill switch, getMYUUID outside the lock);
  - the strict read (_readFresh: missing / unreadable / migrated);
  - the constructor never exposes the template's old-shape import entry
    (checked with the add-on's REAL remotes/channels.json template), and
    XMLTVS.cleanSelf fails open again with it;
  - every writer refuses an unreadable file; a copy that never loaded the
    file can't replace the whole channel list; a missing file;
  - write failures, what the copy holds afterwards, Open_Manager and the
    "has channels" property;
  - setImports against the file, the guide-cache sweep, removeOrphans,
    saveImportRequest and commitSync;
  - the "never re-create" rule moved from imports.71: a background writer
    (Builder-shaped setChannels(modified_ids=...), the sync's commit) never
    writes back a record deleted from the file unless it touched it itself.
"""
import ast
import contextlib
import copy
import json
import os
import shutil
import threading
import time
from types import SimpleNamespace

import pytest
import xbmc

import channels as channels_module
import channels_lock
import importcfg_helpers
from channels import (Channels, COMMIT_BUSY, COMMIT_FAILED, COMMIT_MISSING, COMMIT_NOTHING, COMMIT_UNREADABLE,
                      COMMIT_WRITTEN, READ_OK)

HERE           = os.path.dirname(os.path.abspath(__file__))
ADDON_ROOT     = os.path.dirname(HERE)
LIB            = os.path.join(ADDON_ROOT, 'resources', 'lib')
REAL_TEMPLATE  = os.path.join(ADDON_ROOT, 'remotes', 'channels.json')

CUSTOM_A = 'a' * 32 + '@PseudoTV_Live'
CUSTOM_B = 'b' * 32 + '@PseudoTV_Live'
CUSTOM_C = 'c' * 32 + '@PseudoTV_Live'

# Synthetic import entries (no real addresses: example.invalid is reserved).
SYNCED = {'id': 'one', 'name': 'Provider One', 'enabled': True, 'm3u_path': '/srv/test/one.m3u8',
          'epg_url': 'https://example.invalid/one.xml', 'start_num': 1, 'refresh_interval_min': 600,
          'respect_source_numbers': True, 'rediscover_deleted': False, 'detect_placeholder_pictures': True,
          'last_sync_at': 1763100000, 'last_status': 'ok', 'etag': 'v1', 'epg_etag': 'e1', 'playlist_rev': 1}
SECOND = {'id': 'two', 'name': 'Provider Two', 'enabled': True, 'm3u_url': 'https://example.invalid/two.m3u',
          'last_sync_at': 1763100500, 'last_status': 'ok'}


def _rec(cid, number, **over):
    c = {'id': cid, 'number': number, 'name': 'Chan %d' % number, 'path': ['/x'], 'enabled': True, 'group': []}
    c.update(over)
    return c


def _imp(eid, iid, number, **over):
    """An imported channel record of import `iid`."""
    return _rec('%s@%s' % (eid, iid), number, type='import', import_source=iid, **over)


def _write(path, channels=(), imports=(), **extra):
    doc = {'uuid': 'u', 'channels': list(channels), 'imports': [dict(i) for i in imports]}
    doc.update(extra)
    path.write_text(json.dumps(doc))


def _disk(path):
    return json.loads(path.read_text())


def _ids(path):
    return {c['id'] for c in _disk(path)['channels']}


def _open(path):
    return Channels(file=str(path), writable=True)


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    """Every test: a fixed uuid (getMYUUID would open the 'name this device'
    dialog under the stubs), both CACHE_LOCs in tmp, Channels' log lines and
    the "has channels" property recorded."""
    import imports as imports_mod
    cache = tmp_path / 'cache'
    cache.mkdir()
    monkeypatch.setattr(channels_module.SETTINGS, 'getMYUUID', lambda: 'test-uuid')
    monkeypatch.setattr(channels_module, 'CACHE_LOC', str(cache))
    monkeypatch.setattr(imports_mod, 'CACHE_LOC', str(cache))
    lines, has = [], []
    monkeypatch.setattr(channels_module, 'log', lambda msg, level=xbmc.LOGDEBUG: lines.append((msg, level)))
    monkeypatch.setattr(channels_module.PROPERTIES, 'setHasChannels', lambda state=True: has.append(state))
    return SimpleNamespace(tmp=tmp_path, cache=cache, lines=lines, has=has, path=tmp_path / 'channels.json')


def _logged(env, needle, level):
    return [m for m, lvl in env.lines if needle in m and lvl == level]


# ================================================================ the lock, through commit


def test_two_commits_never_overlap_and_both_land(env):
    """Each commit reads the file the other wrote: no overlap, no lost record."""
    _write(env.path, channels=[_rec(CUSTOM_A, 1)])
    barrier = threading.Barrier(2)
    spans, results, guard = [], {}, threading.Lock()

    def adder(cid, number):
        ch = _open(env.path)

        def transform(doc, found):
            enter = time.monotonic()
            time.sleep(0.05)                       # widen the gap between the read and the write
            new = dict(doc)
            new['channels'] = doc['channels'] + [_rec(cid, number)]
            with guard:
                spans.append((enter, time.monotonic()))
            return new
        barrier.wait(timeout=5)
        results[cid] = ch.commit(transform, ctx='test.overlap')

    threads = [threading.Thread(target=adder, args=args) for args in ((CUSTOM_B, 2), (CUSTOM_C, 3))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert not any(t.is_alive() for t in threads)
    assert results == {CUSTOM_B: COMMIT_WRITTEN, CUSTOM_C: COMMIT_WRITTEN}
    first, second = sorted(spans)
    assert second[0] >= first[1] - 0.001, (first, second)
    assert _ids(env.path) == {CUSTOM_A, CUSTOM_B, CUSTOM_C}


def test_commit_is_re_entrant_for_the_lock_holder(env, monkeypatch):
    monkeypatch.setattr(channels_lock, 'CHANNELS_TIMEOUT', 0.5)
    _write(env.path, channels=[_rec(CUSTOM_A, 1)])
    ch = _open(env.path)
    with channels_lock.held('test.outer'):
        assert ch.setChannels([_rec(CUSTOM_A, 1, name='Renamed')]) is True
    assert _disk(env.path)['channels'][0]['name'] == 'Renamed'


def _hold_the_lock():
    ready, release = threading.Event(), threading.Event()

    def hold():
        with channels_lock.held('test.holder'):
            ready.set()
            release.wait(timeout=5)
    t = threading.Thread(target=hold, daemon=True)
    t.start()
    assert ready.wait(timeout=2)
    return t, release


def test_lock_timeout_is_busy_and_writes_nothing(env, monkeypatch):
    lock_lines = []
    monkeypatch.setattr(channels_lock, '_log', lambda msg, level=None: lock_lines.append((msg, level)))
    monkeypatch.setattr(channels_lock, 'CHANNELS_TIMEOUT', 0.2)
    _write(env.path, channels=[_rec(CUSTOM_A, 1), _imp('L', 'one', 2, is_orphan=True)], imports=[SYNCED])
    (env.cache / 'epg_gone.xml').write_bytes(b'<tv/>')
    ch = _open(env.path)
    before = env.path.read_bytes()
    holder, release = _hold_the_lock()
    called = []
    try:
        assert ch.commit(lambda doc, found: called.append(1), ctx='test.busy') == COMMIT_BUSY
        assert ch.setChannels([_rec(CUSTOM_A, 1, name='x')]) is False
        assert ch.saveImportRequest({'op': 'delete', 'id': 'one'})['status'] == COMMIT_BUSY
        assert ch.removeOrphans('one')['status'] == COMMIT_BUSY
        assert ch.commitSync({}, {}, [], set(), set()) == {'status': COMMIT_BUSY}
    finally:
        release.set()
        holder.join(timeout=2)
    assert called == [], 'the transform never ran'
    assert env.path.read_bytes() == before
    assert (env.cache / 'epg_gone.xml').exists()
    assert env.has == []
    assert [lvl for _m, lvl in lock_lines] == [xbmc.LOGERROR] * 5
    assert lock_lines[0][0] == ('channels_lock: TIMEOUT (0.2s) waiting to save channels.json for test.busy '
                                '-- nothing written')
    assert {m.rsplit(' for ', 1)[1] for m, _l in lock_lines[1:]} == {
        'setChannels -- nothing written', 'saveImportRequest -- nothing written',
        'removeOrphans -- nothing written', 'commitSync -- nothing written'}


def test_exception_in_the_transform_writes_nothing_and_releases_the_lock(env, monkeypatch):
    monkeypatch.setattr(channels_lock, 'CHANNELS_TIMEOUT', 0.5)
    _write(env.path, channels=[_rec(CUSTOM_A, 1)])
    ch = _open(env.path)
    before = env.path.read_bytes()

    class Boom(Exception):
        pass

    def boom(doc, found):
        raise Boom()
    with pytest.raises(Boom):
        ch.commit(boom, ctx='test.boom')
    assert env.path.read_bytes() == before
    assert not channels_module.PROPERTIES.isInterruptActivity(), 'interruptActivity was left too'
    got = []
    t = threading.Thread(target=lambda: got.append(_open(env.path).setChannels([_rec(CUSTOM_A, 1, name='after')])))
    t.start()
    t.join(timeout=5)
    assert got == [True]
    assert _disk(env.path)['channels'][0]['name'] == 'after'


def test_kill_switch_keeps_every_merge(env, monkeypatch):
    """With the one-file kill switch only the serialisation goes: a Save
    still lands on the file as it is and keeps the sync info."""
    monkeypatch.setattr(channels_lock, 'CHANNELS_LOCK', channels_lock._NoLock())
    _write(env.path, channels=[_rec(CUSTOM_A, 1)], imports=[SYNCED])
    ch = _open(env.path)
    out = ch.saveImportRequest({'op': 'edit', 'id': 'one', 'entry': {'refresh_interval_min': 590}})
    assert out['status'] == COMMIT_WRITTEN and out['changed'] is True
    saved = _disk(env.path)['imports'][0]
    assert saved['refresh_interval_min'] == 590 and saved['last_sync_at'] == SYNCED['last_sync_at']
    assert ch.setChannels([_rec(CUSTOM_A, 1, name='x')]) is True


def test_getmyuuid_is_called_outside_the_lock(env, monkeypatch):
    """getMYUUID can open the 'name this device' dialog: never inside the lock."""
    owned = []

    def uuid():
        owned.append(channels_lock.CHANNELS_LOCK._is_owned())
        return 'test-uuid'
    monkeypatch.setattr(channels_module.SETTINGS, 'getMYUUID', uuid)
    _write(env.path, channels=[_rec(CUSTOM_A, 1)], imports=[SYNCED])
    ch = _open(env.path)
    assert ch.setChannels([_rec(CUSTOM_A, 1)]) is True
    assert ch.setChannels(modified_ids={CUSTOM_A}) is True
    assert ch.commitSync({}, {}, ch.getChannels(), set(), set())['status'] == COMMIT_WRITTEN
    assert owned == [False, False, False]
    assert _disk(env.path)['uuid'] == 'test-uuid'


def test_the_write_runs_inside_interrupt_activity_and_the_lock(env, monkeypatch):
    """X-IA1: the contract's order, outermost first: interruptActivity (the
    Channel Manager preview is waited out, and Builder sees the interrupt
    flag, as with every save before imports.69) -> CHANNELS_LOCK -> the
    write."""
    events = []

    @contextlib.contextmanager
    def interrupt_activity():
        events.append('enter')
        try:
            yield
        finally:
            events.append('exit')
    monkeypatch.setattr(channels_module.PROPERTIES, 'interruptActivity', interrupt_activity)
    real = channels_module.FileAccess.setJSON

    def set_json(fle, data, *args, **kwargs):
        events.append('write (lock owned=%s)' % (channels_lock.CHANNELS_LOCK._is_owned(),))
        return real(fle, data, *args, **kwargs)
    monkeypatch.setattr(channels_module.FileAccess, 'setJSON', set_json)
    _write(env.path, channels=[_rec(CUSTOM_A, 1)], imports=[SYNCED])
    assert _open(env.path).setChannels([_rec(CUSTOM_A, 1, name='x')]) is True
    assert events == ['enter', 'write (lock owned=True)', 'exit']


def test_the_open_manager_setting_is_written_after_the_lock(env, monkeypatch):
    """X-SS1: _setSetting is a Kodi setting write, so it runs after
    CHANNELS_LOCK is released (nothing slow while held; a settings write also
    makes Kodi reload the add-on's settings)."""
    _write(env.path, channels=[_rec(CUSTOM_A, 1)], imports=[SYNCED])
    ch = _open(env.path)
    owned = []
    monkeypatch.setattr(ch, '_setSetting', lambda: owned.append(channels_lock.CHANNELS_LOCK._is_owned()))
    assert ch.setChannels([_rec(CUSTOM_A, 1), _rec(CUSTOM_B, 2)]) is True
    assert owned == [False]


# ================================================================ the strict read


@pytest.mark.parametrize('content, status, reason', [
    (None, COMMIT_MISSING, None),
    ('{"uuid": "u", "channels": [', COMMIT_UNREADABLE, 'JSONDecodeError: '),
    ('', COMMIT_UNREADABLE, 'JSONDecodeError: '),
    ('[]', COMMIT_UNREADABLE, 'the top level is a list, not an object'),
    ('"text"', COMMIT_UNREADABLE, 'the top level is a str, not an object'),
    ('{"channels": [], "imports": null}', COMMIT_UNREADABLE, "'imports' is a NoneType, not a list"),
    ('{"channels": "x"}', COMMIT_UNREADABLE, "'channels' is a str, not a list"),
    (b'\xff\xfe{"channels": []}', COMMIT_UNREADABLE, 'UnicodeDecodeError: '),
])
@pytest.mark.parametrize('route', ['local', 'vfs'])
def test_readfresh_tells_missing_from_unreadable(env, monkeypatch, content, status, reason, route):
    ch = _open(env.path)
    if route == 'vfs':                                   # an smb:// user folder: Kodi's VFS
        monkeypatch.setattr(channels_module.FileAccess, '_localPath', lambda file: None)
    if isinstance(content, bytes):
        env.path.write_bytes(content)
    elif content is not None:
        env.path.write_text(content)
    got_status, got = ch._readFresh()
    assert got_status == status
    if reason is None:
        assert got is None
    elif route == 'local' or not reason.startswith('UnicodeDecodeError'):
        assert got.startswith(reason) if reason.endswith(': ') else got == reason


def test_readfresh_parses_anew_and_fills_missing_lists(env):
    env.path.write_text('{"uuid": "u", "extra": 1}')
    ch = _open(env.path)
    status, doc = ch._readFresh()
    assert status == READ_OK and doc == {'uuid': 'u', 'extra': 1, 'channels': [], 'imports': []}
    doc['channels'].append('changed by a caller')
    assert ch._readFresh()[1]['channels'] == [], 'every call parses anew: the doc is private'


def test_readfresh_migrates_the_old_import_shape(env):
    env.path.write_text(json.dumps({'uuid': 'u', 'channels': [], 'imports': [{'vod': [], 'live': []}]}))
    ch = _open(env.path)
    status, doc = ch._readFresh()
    assert status == READ_OK and doc['imports'] == []


# ================================================================ the constructor and the template


@pytest.fixture
def real_template(monkeypatch, tmp_path):
    """The add-on's real remotes/channels.json (the stub template has no
    imports key, so it can't show the bug), copied to tmp."""
    path = tmp_path / 'template.json'
    shutil.copyfile(REAL_TEMPLATE, str(path))
    monkeypatch.setattr(channels_module, 'CHANNELFLE_DEFAULT', str(path))
    return path


def test_the_real_template_ships_the_old_import_entry():
    with open(REAL_TEMPLATE, encoding='utf-8') as f:
        template = json.load(f)
    assert Channels._needsImportsMigration(template), 'the template tests below need its old-shape entry'


@pytest.mark.parametrize('state', ['missing', 'broken', 'no import list'])
def test_constructor_never_exposes_the_template_import_entry(env, real_template, state):
    if state == 'broken':
        env.path.write_text('{"uuid": "u", "channels": [')
    elif state == 'no import list':
        env.path.write_text(json.dumps({'uuid': 'u', 'channels': [_rec(CUSTOM_A, 1)]}))
    ch = _open(env.path)
    assert ch.getImports() == []
    assert ch._fromFile is (state == 'no import list')


TEMPLATE_WRITERS = {
    'setChannels user':       lambda ch: ch.setChannels([_rec(CUSTOM_A, 1)]),
    'setChannels background': lambda ch: ch.setChannels(modified_ids={CUSTOM_A}),
    '_save':                  lambda ch: ch._save(),
    'setImports':             lambda ch: ch.setImports(ch.getImports()),
    'saveImportRequest':      lambda ch: ch.saveImportRequest({'op': 'add', 'entry': {'id': 'new', 'enabled': True}}),
}


@pytest.mark.parametrize('state', ['missing', 'no import list'])
@pytest.mark.parametrize('writer', sorted(TEMPLATE_WRITERS))
def test_no_writer_stores_the_template_import_entry(env, real_template, writer, state):
    if state == 'no import list':
        env.path.write_text(json.dumps({'uuid': 'u', 'channels': [_rec(CUSTOM_A, 1)]}))
    ch = _open(env.path)
    TEMPLATE_WRITERS[writer](ch)
    imports = _disk(env.path)['imports']
    assert all('id' in i for i in imports) and not [i for i in imports if 'vod' in i or 'live' in i]


def test_cleanself_fails_open_with_the_real_template_and_an_unreadable_file(env, real_template, monkeypatch):
    """imports.59 S2 again, through the real classes: with channels.json
    unreadable the guide load keeps every entry of ours. The template's
    import entry made the list look known (and every import entry was
    dropped)."""
    from xmltvs import XMLTVS

    class _Logged(XMLTVS):                         # test_imports59_cleanself_fail_open's harness
        def __init__(self):
            self.m3u = None
            self.XMLTVDATA = {}
            self.lines = []

        def log(self, msg, level=None):
            self.lines.append((msg, level))
    env.path.write_text('{"uuid": "u", "channels": [')
    monkeypatch.setattr(channels_module, 'CHANNELFLEPATH', str(env.path))   # Channels() with no file argument
    monkeypatch.setattr(channels_module.SETTINGS, 'setSetting', lambda *a, **k: None)
    rec = 'e' * 16 + '@PseudoTV_Live'
    items = [{'id': CUSTOM_A, 'display-name': [('Custom', '')]}, {'id': 'TVE@one', 'display-name': [('TVE', '')]},
             {'id': 'X@two', 'display-name': [('X', '')]}, {'id': rec, 'display-name': [('Rec', '')]},
             {'id': 'foreign', 'display-name': [('Not ours', '')]}]
    x = _Logged()
    kept, recordings = x.cleanSelf(items, 'id')
    assert {c['id'] for c in kept} == {CUSTOM_A, 'TVE@one', 'X@two'}
    assert {r['id'] for r in recordings} == {rec}
    assert [m for m, _l in x.lines if 'channel list unavailable' in m]


# ================================================================ refusals


REFUSING = {
    'setChannels user':       lambda ch: ch.setChannels([_rec(CUSTOM_A, 1, name='x')]),
    'setChannels background': lambda ch: ch.setChannels(modified_ids={CUSTOM_A}),
    '_save':                  lambda ch: ch._save(),
    'setImports':             lambda ch: ch.setImports([]),
    'saveImportRequest':      lambda ch: ch.saveImportRequest({'op': 'delete', 'id': 'one'})['status'],
    'removeOrphans':          lambda ch: ch.removeOrphans('one')['status'],
    'commitSync':             lambda ch: ch.commitSync({}, {}, ch.getChannels(), set(), set())['status'],
}


@pytest.mark.parametrize('broken', ['{"uuid": "u", "channels": [', '[]', '{"channels": [], "imports": null}'])
@pytest.mark.parametrize('writer', sorted(REFUSING))
def test_every_writer_refuses_an_unreadable_file(env, writer, broken):
    _write(env.path, channels=[_rec(CUSTOM_A, 1), _imp('L', 'one', 2, is_orphan=True)], imports=[SYNCED])
    (env.cache / 'epg_gone.xml').write_bytes(b'<tv/>')
    ch = _open(env.path)                                  # loaded while the file was fine
    env.path.write_text(broken)
    before = env.path.read_bytes()
    assert REFUSING[writer](ch) in (False, COMMIT_UNREADABLE)
    assert env.path.read_bytes() == before, 'left byte-identical'
    assert (env.cache / 'epg_gone.xml').exists(), 'no guide cache removed'
    assert _logged(env, "channels.json can't be read", xbmc.LOGERROR)
    assert env.has == []


def test_user_role_refuses_a_copy_that_never_loaded_the_file(env):
    """A copy built on a broken file holds the template (no channels). Once the
    file is repaired, its user-role save would delete every Custom channel on
    disk that its empty list lacks."""
    env.path.write_text('{"uuid": "u", "channels": [')
    stale = _open(env.path)
    assert stale._fromFile is False and stale.getChannels() == []
    _write(env.path, channels=[_rec(CUSTOM_A, 1), _imp('TVE', 'one', 2)], imports=[SYNCED])    # repaired
    before = env.path.read_bytes()
    assert stale.setChannels([_rec(CUSTOM_B, 5)]) is False
    assert env.path.read_bytes() == before, 'the Custom channel on disk is kept'
    assert ('Channels: setChannels, this copy never loaded channels.json -- nothing written', xbmc.LOGERROR) in env.lines


def test_save_refuses_a_copy_that_never_loaded_the_file(env):
    env.path.write_text('{"uuid": "u", "channels": [')
    stale = _open(env.path)
    _write(env.path, channels=[_rec(CUSTOM_A, 1)], imports=[SYNCED])
    before = env.path.read_bytes()
    stale.channelDATA['channels'] = [_rec(CUSTOM_B, 5)]
    assert stale._save() is False
    assert env.path.read_bytes() == before
    assert ('Channels: _save, this copy never loaded channels.json -- nothing written', xbmc.LOGERROR) in env.lines


def test_save_writes_its_channels_with_the_file_import_list(env):
    """🔨 Rebuild / 🧹 Cleanup stranded (the two _save bypasses) no longer
    write the import list they loaded at request start: a dashboard Save
    that landed in between stays."""
    _write(env.path, channels=[_rec(CUSTOM_A, 1), _imp('TVE', 'one', 2)], imports=[SYNCED])
    bypass = _open(env.path)
    assert _open(env.path).saveImportRequest({'op': 'edit', 'id': 'one', 'entry': {'refresh_interval_min': 590}})['changed']
    bypass.channelDATA['channels'] = bypass.sortChannels([_rec(CUSTOM_A, 1, changed=True)])
    assert bypass._save() is True
    disk = _disk(env.path)
    assert disk['imports'][0]['refresh_interval_min'] == 590 and disk['imports'][0]['config_key'] == 'pending'
    assert disk['channels'] == [_rec(CUSTOM_A, 1, changed=True)], 'its own channel list, as before'


def test_background_role_of_a_never_loaded_copy_still_merges(env):
    """Only whole-list writers are refused: a background save merges onto the
    file (untouched records come from disk)."""
    env.path.write_text('{"uuid": "u", "channels": [')
    stale = _open(env.path)
    _write(env.path, channels=[_rec(CUSTOM_A, 1), _imp('TVE', 'one', 2)], imports=[SYNCED])
    stale.addChannel(_rec(CUSTOM_B, 5))
    assert stale.setChannels(modified_ids={CUSTOM_B}) is True
    assert _ids(env.path) == {CUSTOM_A, 'TVE@one', CUSTOM_B}
    assert _disk(env.path)['imports'] == [SYNCED]


def test_first_install_writes_and_writes_again(env):
    """No file yet: user writers create it, and the copy then counts as
    loaded (commit sets _fromFile), so its next save is not refused."""
    ch = _open(env.path)
    assert ch._fromFile is False
    assert ch.setChannels([_rec(CUSTOM_A, 1)]) is True
    assert ch._fromFile is True
    assert ch.setChannels([_rec(CUSTOM_A, 1), _rec(CUSTOM_B, 2)]) is True
    assert _ids(env.path) == {CUSTOM_A, CUSTOM_B}
    ch.channelDATA['channels'] = [_rec(CUSTOM_A, 1)]
    assert ch._save() is True
    assert _ids(env.path) == {CUSTOM_A}


def test_missing_file(env):
    """A user writer writes (first install); the sync's commit refuses;
    _save keeps this copy's import list."""
    _write(env.path, channels=[_rec(CUSTOM_A, 1), _imp('TVE', 'one', 2)], imports=[SYNCED])
    ch = _open(env.path)
    env.path.unlink()
    start = {'one': copy.deepcopy(SYNCED)}
    res = {'one': {'status': 'ok', 'updated_cfg': dict(SYNCED, last_sync_at=SYNCED['last_sync_at'] + 60)}}
    assert ch.commitSync(start, res, ch.getChannels(), {'TVE@one'}, set()) == {'status': COMMIT_MISSING}
    assert not env.path.exists()
    assert ('Channels: commit [commitSync], channels.json is missing -- nothing written', xbmc.LOGWARNING) in env.lines
    assert ch._save() is True
    assert _disk(env.path)['imports'] == [SYNCED], "a vanished file isn't re-created with imports: []"
    assert _ids(env.path) == {CUSTOM_A, 'TVE@one'}
    env.path.unlink()
    assert ch.setChannels([_rec(CUSTOM_B, 3)]) is True
    assert _ids(env.path) == {CUSTOM_B} and _disk(env.path)['imports'] == [SYNCED]


def test_background_writer_with_no_file_keeps_its_whole_list(env):
    """With no file at all nothing was deleted from it: a background save
    writes its whole list, as before (imports.71 refuses a missing file for
    background writers instead)."""
    _write(env.path, channels=[_rec(CUSTOM_A, 1), _rec(CUSTOM_B, 2)])
    builder = _open(env.path)
    env.path.unlink()
    assert builder.setChannels(modified_ids={CUSTOM_A}) is True
    assert _ids(env.path) == {CUSTOM_A, CUSTOM_B}


@pytest.mark.parametrize('how', ['raises', 'returns False'])
def test_a_write_failure_is_a_failed_save(env, monkeypatch, how):
    """FileLock raises on a timeout: a failed save, never an exception out of
    a writer (Builder's save sits outside its per-channel try)."""
    _write(env.path, channels=[_rec(CUSTOM_A, 1)], imports=[SYNCED])
    ch = _open(env.path)
    before = env.path.read_bytes()

    def failing(file, data):
        if how == 'raises':
            raise Exception('FileLock: Timeout occurred.')
        return False
    monkeypatch.setattr(channels_module.FileAccess, 'setJSON', failing)
    setting = []
    monkeypatch.setattr(ch, '_setSetting', lambda: setting.append(1))
    assert ch.setChannels([_rec(CUSTOM_A, 1, name='x')]) is False
    assert ch.commit(lambda doc, found: dict(doc), ctx='test.write') == COMMIT_FAILED
    assert ch.saveImportRequest({'op': 'delete', 'id': 'one'})['status'] == COMMIT_FAILED
    assert env.path.read_bytes() == before
    assert setting == [] and env.has == []
    assert ch.getChannels()[0]['name'] == 'Chan 1', 'the copy still holds what the file holds'
    assert len(_logged(env, 'channels.json write failed', xbmc.LOGERROR)) == 3


def test_a_read_only_copy_never_writes(env):
    _write(env.path, channels=[_rec(CUSTOM_A, 1)], imports=[SYNCED])
    ch = Channels(file=str(env.path), writable=False)
    before = env.path.read_bytes()
    assert ch.commit(lambda doc, found: dict(doc), ctx='test.ro') == COMMIT_FAILED
    assert ch.setChannels([_rec(CUSTOM_B, 2)]) is False
    assert ch.setImports([]) is False
    assert ch._save() is False
    assert env.path.read_bytes() == before


# ================================================================ after a write


def test_the_copy_holds_what_was_written(env):
    _write(env.path, channels=[_rec(CUSTOM_A, 1), _imp('TVE', 'one', 2)], imports=[SYNCED])
    ch = _open(env.path)
    other = _open(env.path)
    assert other.saveImportRequest({'op': 'edit', 'id': 'one', 'entry': {'refresh_interval_min': 590}})['changed']
    assert ch.setChannels([_rec(CUSTOM_A, 1, name='Edited')]) is True
    disk = _disk(env.path)
    assert ch.channelDATA['imports'] == disk['imports'], 'the import list is the file one, not the start-up copy'
    assert ch.getImports()[0]['refresh_interval_min'] == 590
    assert ch.getChannels() == disk['channels'] and ch.channelDATA['uuid'] == 'test-uuid'


def test_open_manager_count_is_written_only_after_a_write(env, monkeypatch):
    _write(env.path, channels=[_rec(CUSTOM_A, 1)], imports=[SYNCED])
    ch = _open(env.path)
    calls = []
    monkeypatch.setattr(ch, '_setSetting', lambda: calls.append(len(ch.channelDATA['channels'])))
    assert ch.removeOrphans('one')['status'] == COMMIT_NOTHING
    assert ch.saveImportRequest({'op': 'edit', 'id': 'one', 'entry': {'name': SYNCED['name']}})['status'] == COMMIT_NOTHING
    assert calls == [], 'nothing written: no Kodi setting write'
    assert ch.setChannels([_rec(CUSTOM_A, 1), _rec(CUSTOM_B, 2)]) is True
    assert calls == [2]
    env.path.write_text('{"uuid": "u", "channels": [')
    assert ch.setChannels([_rec(CUSTOM_A, 1)]) is False
    assert calls == [2]


def test_has_channels_follows_the_written_list(env):
    """Set after every written commit from the list it wrote; the sync now
    writes through commitSync, so setChannels alone would leave it unset."""
    _write(env.path, channels=[_rec(CUSTOM_A, 1)], imports=[SYNCED])
    ch = _open(env.path)
    assert ch.setChannels([_rec(CUSTOM_A, 1, name='x')]) is True
    assert env.has == [True]
    start = {'one': copy.deepcopy(SYNCED)}
    res = {'one': {'status': 'ok', 'updated_cfg': dict(SYNCED, last_sync_at=SYNCED['last_sync_at'] + 60)}}
    info = ch.commitSync(start, res, [_rec(CUSTOM_A, 1, name='x'), _imp('TVE', 'one', 2)], {'TVE@one'}, set())
    assert info['status'] == COMMIT_WRITTEN and env.has == [True, True]
    assert ch.saveImportRequest({'op': 'delete', 'id': 'one'})['purged_channels'] == 1
    assert env.has == [True, True, True], 'Custom A is still there'
    assert ch.setChannels([]) is True
    assert env.has[-1] is False, 'a write that leaves no channel clears it'
    assert _disk(env.path)['channels'] == []


# ================================================================ setImports and the guide-cache sweep


def test_setimports_purges_against_the_file(env):
    """The removed imports are found against the file: an import added after
    this copy loaded, and dropped by its save, leaves no stranded records."""
    _write(env.path, channels=[_rec(CUSTOM_A, 1), _imp('TVE', 'one', 2)], imports=[SYNCED])
    stale = _open(env.path)
    adder = _open(env.path)
    assert adder.setImports([dict(SYNCED), dict(SECOND)]) is True
    assert adder.setChannels(adder.getChannels() + [_imp('X', 'two', 3)]) is True
    (env.cache / 'epg_two.xml').write_bytes(b'<tv/>')
    (env.cache / 'epg_one.xml').write_bytes(b'<tv/>')
    assert stale.setImports([dict(SYNCED)]) is True
    disk = _disk(env.path)
    assert [i['id'] for i in disk['imports']] == ['one']
    assert _ids(env.path) == {CUSTOM_A, 'TVE@one'}, 'the removed import takes its records with it'
    assert not (env.cache / 'epg_two.xml').exists() and (env.cache / 'epg_one.xml').exists()
    assert _logged(env, 'setImports, purged 1 channels from removed imports', xbmc.LOGINFO)


def test_cache_sweep_after_a_written_import_list(env):
    off = {'id': 'off', 'name': 'Off', 'enabled': False}
    _write(env.path, channels=[], imports=[SYNCED, off])
    others = ['epg_one.xml', 'epg_off.xml', 'epg_gone.xml.tmp', 'pseudotv.xml', 'show_pictures.json',
              'channels.json', 'gone.xml']
    for name in others + ['epg_gone.xml', 'epg_older.xml']:
        (env.cache / name).write_bytes(b'x')
    (env.cache / 'logos').mkdir()
    (env.cache / 'logos' / 'epg_gone.xml').write_bytes(b'x')
    (env.cache / 'epg_folder.xml').mkdir()
    ch = _open(env.path)
    out = ch.saveImportRequest({'op': 'edit', 'id': 'one', 'entry': {'refresh_interval_min': 590}})
    assert out['status'] == COMMIT_WRITTEN
    assert sorted(os.listdir(str(env.cache))) == sorted(others + ['logos', 'epg_folder.xml'])
    assert (env.cache / 'logos' / 'epg_gone.xml').exists(), 'only files directly in the cache folder'
    assert ('Channels: commit, removed guide cache epg_gone.xml (its import is no longer listed)',
            xbmc.LOGINFO) in env.lines
    assert not _logged(env, 'guide cache', xbmc.LOGWARNING), 'a folder named like a cache is skipped, not tried'


def test_cache_sweep_runs_only_after_a_written_import_list(env):
    """A leftover from a refused or failed save, or from a sync that re-wrote
    it after a Delete, goes at the next successful import-list write."""
    _write(env.path, channels=[_rec(CUSTOM_A, 1)], imports=[SYNCED])
    (env.cache / 'epg_gone.xml').write_bytes(b'x')
    ch = _open(env.path)
    assert ch.setChannels([_rec(CUSTOM_A, 1, name='x')]) is True
    assert ch._save() is True
    assert ch.removeOrphans('one')['status'] == COMMIT_NOTHING
    assert (env.cache / 'epg_gone.xml').exists(), 'channel writers never sweep'
    assert ch.saveImportRequest({'op': 'edit', 'id': 'one', 'entry': {'name': SYNCED['name']}})['status'] == COMMIT_NOTHING
    assert (env.cache / 'epg_gone.xml').exists(), 'nothing written, nothing swept'
    good = env.path.read_bytes()
    env.path.write_text('{"uuid": "u", "channels": [')
    assert ch.setImports([dict(SYNCED)]) is False
    assert (env.cache / 'epg_gone.xml').exists(), 'a refused save removes no cache'
    env.path.write_bytes(good)
    assert ch.saveImportRequest({'op': 'edit', 'id': 'one', 'entry': {'refresh_interval_min': 590}})['changed']
    assert not (env.cache / 'epg_gone.xml').exists()


def test_an_orphans_write_never_sweeps_the_guide_caches(env):
    """E46: the sweep follows the writers of the import list itself (Save,
    Add, Delete, setImports and the sync's commit); 🗑 Orphans changes only
    an import's tombstones, so a written Orphans removal deletes no cache."""
    one = dict(SYNCED, tombstones=['OLD@one'])
    _write(env.path, channels=[_rec(CUSTOM_A, 1), _imp('O1', 'one', 2, is_orphan=True)], imports=[one])
    (env.cache / 'epg_gone.xml').write_bytes(b'x')
    out = _open(env.path).removeOrphans('one')
    assert out['status'] == COMMIT_WRITTEN and out['removed'] == 1
    assert (env.cache / 'epg_gone.xml').exists()


def test_cache_sweep_errors_are_logged_never_raised(env, monkeypatch):
    _write(env.path, channels=[], imports=[SYNCED])
    (env.cache / 'epg_gone.xml').write_bytes(b'x')
    ch = _open(env.path)
    real_unlink, real_listdir = os.unlink, os.listdir

    def unlink(path, *a, **k):
        if str(path).endswith('epg_gone.xml'):
            raise PermissionError(13, 'Permission denied')
        return real_unlink(path, *a, **k)
    monkeypatch.setattr(os, 'unlink', unlink)
    assert ch.setImports([dict(SYNCED)]) is True
    assert (env.cache / 'epg_gone.xml').exists()
    assert _logged(env, 'commit, could not remove guide cache epg_gone.xml: ', xbmc.LOGWARNING)
    monkeypatch.setattr(os, 'unlink', real_unlink)

    def listdir(path='.'):
        if str(path) == str(env.cache):
            raise PermissionError(13, 'Permission denied')
        return real_listdir(path)
    monkeypatch.setattr(os, 'listdir', listdir)
    assert ch.setImports([dict(SYNCED)]) is True
    assert _logged(env, 'commit, guide cache sweep failed: ', xbmc.LOGWARNING)


def test_cache_sweep_with_no_cache_folder(env):
    _write(env.path, channels=[], imports=[SYNCED])
    shutil.rmtree(str(env.cache))
    assert _open(env.path).setImports([dict(SYNCED)]) is True
    assert not _logged(env, 'guide cache', xbmc.LOGWARNING)


# ================================================================ removeOrphans


def _count_writes(monkeypatch):
    writes = []
    real = channels_module.FileAccess.setJSON

    def counting(file, data):
        writes.append(file)
        return real(file, data)
    monkeypatch.setattr(channels_module.FileAccess, 'setJSON', counting)
    return writes


def test_remove_orphans_is_one_write(env, monkeypatch):
    one = dict(SYNCED, tombstones=['OLD@one'])
    _write(env.path, channels=[_rec(CUSTOM_A, 1), _imp('O1', 'one', 2, is_orphan=True),
                               _imp('O2', 'one', 3, is_orphan=True), _imp('L', 'one', 4, is_orphan=False),
                               _imp('O3', 'two', 5, is_orphan=True)],
           imports=[one, SECOND])
    ch = _open(env.path)
    writes = _count_writes(monkeypatch)
    assert ch.removeOrphans('one') == {'status': COMMIT_WRITTEN, 'found': True, 'removed': 2}
    assert len(writes) == 1
    disk = _disk(env.path)
    assert disk['imports'][0] == dict(SYNCED, tombstones=['O1@one', 'O2@one', 'OLD@one'])
    assert disk['imports'][1] == SECOND
    assert _ids(env.path) == {CUSTOM_A, 'L@one', 'O3@two'}


def test_remove_orphans_works_on_the_file_as_it_is_now(env):
    """The orphans and the import entry come from the fresh read: a sync that
    marked a record orphan, and saved its own results, after this copy loaded."""
    _write(env.path, channels=[_imp('L', 'one', 4)], imports=[SYNCED])
    ch = _open(env.path)
    synced_later = dict(SYNCED, last_sync_at=SYNCED['last_sync_at'] + 3600, etag='v2')
    _write(env.path, channels=[_imp('L', 'one', 4, is_orphan=True)], imports=[synced_later])
    assert ch.removeOrphans('one') == {'status': COMMIT_WRITTEN, 'found': True, 'removed': 1}
    disk = _disk(env.path)
    assert disk['channels'] == [] and disk['imports'] == [dict(synced_later, tombstones=['L@one'])]


def test_remove_orphans_with_nothing_to_do_writes_nothing(env):
    _write(env.path, channels=[_imp('L', 'one', 4)], imports=[SYNCED])
    ch = _open(env.path)
    before = env.path.read_bytes()
    assert ch.removeOrphans('nope') == {'status': COMMIT_NOTHING, 'found': False, 'removed': 0}
    assert ch.removeOrphans('one') == {'status': COMMIT_NOTHING, 'found': True, 'removed': 0}
    assert env.path.read_bytes() == before


# ================================================================ saveImportRequest


def test_save_import_request_keeps_sync_info_and_tombstones(env):
    stored = dict(SYNCED, tombstones=['GONE@one'], next_retry_at=123, last_status='abandoned')
    _write(env.path, channels=[_rec(CUSTOM_A, 1)], imports=[stored, SECOND])
    ch = _open(env.path)
    out = ch.saveImportRequest({'op': 'edit', 'id': 'one', 'entry': {'refresh_interval_min': 590}})
    assert out['status'] == COMMIT_WRITTEN and out['changed'] is True and out['fields'] == ['refresh_interval_min']
    assert out['purged_channels'] == 0
    saved, second = _disk(env.path)['imports']
    assert saved['last_sync_at'] == SYNCED['last_sync_at'] and saved['tombstones'] == ['GONE@one']
    assert saved['etag'] == 'v1' and saved['config_key'] == 'pending'
    assert 'next_retry_at' not in saved and saved['last_status'] == 'failed'
    assert second == SECOND


def test_save_import_request_delete_is_one_write_with_its_records_and_cache(env, monkeypatch):
    _write(env.path, channels=[_rec(CUSTOM_A, 1), _imp('TVE', 'one', 10), _imp('X', 'two', 20)],
           imports=[SYNCED, SECOND])
    (env.cache / 'epg_one.xml').write_bytes(b'x')
    (env.cache / 'epg_two.xml').write_bytes(b'x')
    ch = _open(env.path)
    writes = _count_writes(monkeypatch)
    out = ch.saveImportRequest({'op': 'delete', 'id': 'one'})
    assert out['status'] == COMMIT_WRITTEN and out['removed_ids'] == ['one'] and out['purged_channels'] == 1
    assert len(writes) == 1
    assert _disk(env.path)['imports'] == [SECOND] and _ids(env.path) == {CUSTOM_A, 'X@two'}
    assert not (env.cache / 'epg_one.xml').exists() and (env.cache / 'epg_two.xml').exists()
    again = ch.saveImportRequest({'op': 'delete', 'id': 'one'})
    assert again['status'] == COMMIT_NOTHING and again['changed'] is False and len(writes) == 1


def test_save_import_request_refusals_propagate_and_write_nothing(env):
    _write(env.path, channels=[_rec(CUSTOM_A, 1)], imports=[SYNCED])
    ch = _open(env.path)
    before = env.path.read_bytes()
    cases = [({'op': 'edit', 'id': 'one', 'entry': {'id': 'renamed'}}, 400),
             ({'op': 'edit', 'id': 'nope', 'entry': {'name': 'x'}}, 404),
             ({'op': 'add', 'entry': {'id': 'one'}}, 409),
             ({'op': 'add', 'entry': {'id': 'new', 'm3u_url': 'ftp://example.invalid/x'}}, 400)]

    def check(address):
        if not address.startswith('https://'):
            raise ValueError('scheme not allowed')
    for payload, status in cases:
        with pytest.raises(importcfg_helpers.ImportRequestError) as err:
            ch.saveImportRequest(payload, check_address=check)
        assert err.value.status == status
    assert env.path.read_bytes() == before
    assert env.has == []


def test_save_import_request_with_no_file_starts_from_this_copy(env):
    """First install: the request applies to this copy's (empty) lists and
    creates the file. A file that vanished while the request ran is not
    re-created without the other imports and channels."""
    ch = _open(env.path)
    out = ch.saveImportRequest({'op': 'add', 'entry': {'id': 'new', 'name': 'New', 'enabled': True}})
    assert out['status'] == COMMIT_WRITTEN
    assert _disk(env.path)['imports'] == [{'id': 'new', 'name': 'New', 'enabled': True}]
    _write(env.path, channels=[_rec(CUSTOM_A, 1)], imports=[SYNCED])
    ch = _open(env.path)
    env.path.unlink()
    assert ch.saveImportRequest({'op': 'add', 'entry': {'id': 'new', 'enabled': True}})['status'] == COMMIT_WRITTEN
    assert [i['id'] for i in _disk(env.path)['imports']] == ['one', 'new'] and _ids(env.path) == {CUSTOM_A}


# ================================================================ commitSync


def _result(base, **cfg):
    return {'status': 'ok', 'updated_cfg': dict(base, **cfg)}


def test_commitsync_is_one_write_onto_the_file_as_it_is_now(env, monkeypatch):
    _write(env.path, channels=[_rec(CUSTOM_A, 1), _imp('TVE', 'one', 10), _imp('GONE', 'one', 11)],
           imports=[dict(SYNCED, tombstones=['GONE@one']), SECOND])
    sync = _open(env.path)
    start = {i['id']: copy.deepcopy(i) for i in sync.getImports()}
    # a Save while the sync runs
    assert _open(env.path).saveImportRequest({'op': 'edit', 'id': 'one',
                                              'entry': {'refresh_interval_min': 590}})['changed']
    writes = _count_writes(monkeypatch)
    acted_on = importcfg_helpers.config_fingerprint(start['one'])
    res = {'one': _result(start['one'], last_sync_at=SYNCED['last_sync_at'] + 3600, etag='v2', config_key=acted_on),
           'two': {'status': 'skipped', 'updated_cfg': copy.deepcopy(start['two'])}}
    merged = [_rec(CUSTOM_A, 1), _imp('TVE', 'one', 10, name='Renamed by the provider')]
    info = sync.commitSync(start, res, merged, {'TVE@one'}, {'GONE@one'})
    assert info['status'] == COMMIT_WRITTEN and len(writes) == 1
    assert info['applied_deletes'] == {'GONE@one'} and info['updated'] == {'one'}
    saved, second = _disk(env.path)['imports']
    assert saved['refresh_interval_min'] == 590, 'the Save made during the sync stays'
    assert saved['etag'] == 'v2' and saved['last_sync_at'] == SYNCED['last_sync_at'] + 3600
    assert saved['config_key'] == acted_on and importcfg_helpers.config_changed(saved), 'next cycle syncs it again'
    assert second == SECOND
    disk = _disk(env.path)
    assert {c['id']: c['name'] for c in disk['channels']} == {CUSTOM_A: 'Chan 1', 'TVE@one': 'Renamed by the provider'}
    assert disk['uuid'] == 'test-uuid'


def test_commitsync_drops_an_import_deleted_while_it_ran(env):
    _write(env.path, channels=[_rec(CUSTOM_A, 1), _imp('TVE', 'one', 10)], imports=[SYNCED])
    sync = _open(env.path)
    start = {'one': copy.deepcopy(SYNCED)}
    assert _open(env.path).saveImportRequest({'op': 'delete', 'id': 'one'})['purged_channels'] == 1
    (env.cache / 'epg_one.xml').write_bytes(b'<tv/>')            # the running sync re-wrote its guide cache
    res = {'one': _result(SYNCED, last_sync_at=SYNCED['last_sync_at'] + 3600)}
    info = sync.commitSync(start, res, [_rec(CUSTOM_A, 1), _imp('TVE', 'one', 10)], {'TVE@one'}, set())
    assert info['status'] == COMMIT_WRITTEN and info['gone'] == {'one'}
    assert _disk(env.path)['imports'] == [] and _ids(env.path) == {CUSTOM_A}
    assert not (env.cache / 'epg_one.xml').exists()


def test_commitsync_merge_error_writes_nothing(env, monkeypatch):
    """No "write the list as given" fallback here: the sync's list holds only
    the records of the imports it synced, so writing it would delete every
    skipped import's channels."""
    _write(env.path, channels=[_rec(CUSTOM_A, 1), _imp('TVE', 'one', 10), _imp('X', 'two', 20)],
           imports=[SYNCED, SECOND])
    sync = _open(env.path)
    before = env.path.read_bytes()

    def broken(*a, **k):
        raise RuntimeError('merge failed')
    monkeypatch.setattr(Channels, '_mergeRecords', staticmethod(broken))
    with pytest.raises(RuntimeError):
        sync.commitSync({'one': copy.deepcopy(SYNCED)}, {'one': _result(SYNCED, etag='v2')},
                        [_rec(CUSTOM_A, 1), _imp('TVE', 'one', 10)], {'TVE@one'}, set())
    assert env.path.read_bytes() == before


def test_setchannels_merge_error_still_writes_the_given_list(env, monkeypatch):
    """Pinned until imports.71 removes it: setChannels' fallback after a merge
    error writes the list as given (with a WARNING)."""
    _write(env.path, channels=[_rec(CUSTOM_A, 1), _imp('TVE', 'one', 10)], imports=[SYNCED])
    ch = _open(env.path)

    def broken(*a, **k):
        raise RuntimeError('merge failed')
    monkeypatch.setattr(Channels, '_mergeRecords', staticmethod(broken))
    assert ch.setChannels([_rec(CUSTOM_B, 2)], modified_ids={CUSTOM_B}) is True
    assert _ids(env.path) == {CUSTOM_B} and _disk(env.path)['imports'] == [SYNCED]
    assert _logged(env, 'falling back to direct write', xbmc.LOGWARNING)


# ================================================================ the "never re-create" rule (moved from imports.71)


def test_builder_shaped_writer_never_writes_back_deleted_records(env):
    """Builder's copy is loaded once per Kodi session and every build of a
    Custom channel saves its whole list with modified_ids. Records deleted
    from the file since it loaded -- a deleted import's channels, removed
    Orphans, a Custom channel deleted in the Channel Manager -- stay deleted;
    the records it built itself (a rebuilt one, a new one) are written."""
    _write(env.path, channels=[_rec(CUSTOM_A, 1), _rec(CUSTOM_B, 2), _imp('TVE', 'one', 10), _imp('LA2', 'one', 11),
                               _imp('KEEP', 'two', 20), _imp('ORPH', 'two', 21, is_orphan=True)],
           imports=[SYNCED, SECOND])
    builder = _open(env.path)
    assert _open(env.path).saveImportRequest({'op': 'delete', 'id': 'one'})['purged_channels'] == 2
    assert _open(env.path).removeOrphans('two')['removed'] == 1
    assert _open(env.path).setChannels([_rec(CUSTOM_A, 1)]) is True          # Custom B deleted
    builder.getChannelbyID(CUSTOM_A)[0]['name'] = 'Rebuilt'
    builder.addChannel(_rec(CUSTOM_C, 3))                                     # a channel Builder created
    assert builder.setChannels(modified_ids={CUSTOM_A, CUSTOM_C}) is True
    disk = _disk(env.path)
    assert _ids(env.path) == {CUSTOM_A, CUSTOM_C, 'KEEP@two'}
    assert [c['name'] for c in disk['channels'] if c['id'] == CUSTOM_A] == ['Rebuilt']
    assert disk['imports'] == [dict(SECOND, tombstones=['ORPH@two'])]
    assert _logged(env, 'setChannels, 4 record(s) deleted from channels.json since this copy was loaded '
                        'were not written back', xbmc.LOGINFO)


def test_sync_shaped_commit_never_writes_back_a_deleted_custom_channel(env):
    """The sync's channel list carries its start-of-sync copy of the Custom
    records: one deleted while the sync ran stays deleted, while the import
    records the sync built (in modified_ids) are written, new ones included."""
    _write(env.path, channels=[_rec(CUSTOM_A, 1), _rec(CUSTOM_B, 2), _imp('TVE', 'one', 10)], imports=[SYNCED])
    sync = _open(env.path)
    start = {'one': copy.deepcopy(SYNCED)}
    existing = sync.getChannels()
    assert _open(env.path).setChannels([_rec(CUSTOM_A, 1)]) is True          # Custom B deleted meanwhile
    merged = [c for c in existing if c.get('type') != 'import'] + [_imp('TVE', 'one', 10), _imp('NEW', 'one', 12)]
    res = {'one': _result(SYNCED, last_sync_at=SYNCED['last_sync_at'] + 3600, etag='v2')}
    info = sync.commitSync(start, res, merged, {'TVE@one', 'NEW@one'}, set())
    assert info['status'] == COMMIT_WRITTEN
    assert _ids(env.path) == {CUSTOM_A, 'TVE@one', 'NEW@one'}
    assert _logged(env, 'commitSync, 1 record(s) deleted from channels.json', xbmc.LOGINFO)


def test_orphans_removed_during_a_sync_stay_removed(env):
    """The tombstones on disk now count as deletes in the sync's commit, so
    orphans removed while it ran stay removed, although the sync rebuilt
    their records (they are in its modified_ids)."""
    _write(env.path, channels=[_imp('TVE', 'one', 10), _imp('ORPH', 'one', 11, is_orphan=True)], imports=[SYNCED])
    sync = _open(env.path)
    start = {'one': copy.deepcopy(SYNCED)}
    assert _open(env.path).removeOrphans('one')['removed'] == 1
    res = {'one': _result(SYNCED, last_sync_at=SYNCED['last_sync_at'] + 3600)}
    info = sync.commitSync(start, res, [_imp('TVE', 'one', 10), _imp('ORPH', 'one', 11, is_orphan=True)],
                           {'TVE@one', 'ORPH@one'}, set())
    assert info['status'] == COMMIT_WRITTEN
    assert _ids(env.path) == {'TVE@one'}
    assert _disk(env.path)['imports'][0]['tombstones'] == ['ORPH@one']


def test_a_record_the_writer_rebuilt_is_still_written_back(env):
    """Pinned leftover until imports.71: a record deleted while that very
    record was being rebuilt (it is in the writer's modified_ids) comes back."""
    _write(env.path, channels=[_rec(CUSTOM_A, 1), _rec(CUSTOM_B, 2)])
    builder = _open(env.path)
    assert _open(env.path).setChannels([_rec(CUSTOM_A, 1)]) is True
    builder.getChannelbyID(CUSTOM_B)[0]['name'] = 'Rebuilt while deleted'
    assert builder.setChannels(modified_ids={CUSTOM_B}) is True
    assert _ids(env.path) == {CUSTOM_A, CUSTOM_B}


def test_user_role_writes_its_whole_list(env):
    """The rule is for background writers only: the Channel Manager's list is
    authoritative (a channel it adds is written; imported records on disk it
    lacks are kept)."""
    _write(env.path, channels=[_rec(CUSTOM_A, 1), _imp('TVE', 'one', 10)], imports=[SYNCED])
    manager = _open(env.path)
    assert manager.setChannels([_rec(CUSTOM_A, 1), _rec(CUSTOM_B, 2)]) is True
    assert _ids(env.path) == {CUSTOM_A, CUSTOM_B, 'TVE@one'}


def test_explicit_deletes_count_the_records_removed(env):
    """'applied N explicit deletes' counts records that existed, not every
    tombstone passed in (before: every sync logged them all)."""
    _write(env.path, channels=[_rec(CUSTOM_A, 1), _imp('GONE', 'one', 10)], imports=[SYNCED])
    ch = _open(env.path)
    assert ch.setChannels(modified_ids=set(), deleted_ids={'GONE@one', 'LONG_GONE@one', 'OLDER@one'}) is True
    assert _ids(env.path) == {CUSTOM_A}
    assert _logged(env, 'setChannels, applied 1 explicit deletes', xbmc.LOGINFO)
    env.lines.clear()
    assert ch.setChannels(modified_ids=set(), deleted_ids={'GONE@one'}) is True
    assert not _logged(env, 'explicit deletes', xbmc.LOGINFO)


# ================================================================ source checks


def _channels_src():
    with open(os.path.join(LIB, 'channels.py'), encoding='utf-8') as f:
        return f.read()


def test_channels_py_is_python38():
    """Kodi 19/20 run Python 3.8 (the shared deny-list; '|' is checked by the
    one scanner in test_imports69_importcfg_helpers.py)."""
    from test_imports63_clock import py39_api_leaks
    src = _channels_src()
    ast.parse(src, feature_version=(3, 8))
    assert py39_api_leaks(src) == []
    assert 'madteevee (imports fork, imports.69)' in src


def test_commit_is_the_only_write_of_channels_json():
    """FileAccess.setJSON appears once in channels.py, inside commit; the
    lenient getJSON read is never used for a commit."""
    tree = ast.parse(_channels_src())
    owners = []
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef):
            for node in ast.walk(fn):
                if isinstance(node, ast.Attribute) and node.attr == 'setJSON':
                    owners.append(fn.name)
    assert owners == ['commit']
    fresh = next(fn for fn in ast.walk(tree) if isinstance(fn, ast.FunctionDef) and fn.name == '_readFresh')
    assert not [n for n in ast.walk(fresh) if isinstance(n, ast.Attribute) and n.attr in ('getJSON', 'loadJSON')]
    assert not hasattr(Channels, '_adoptDiskImports')
