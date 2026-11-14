# -*- coding: utf-8 -*-
"""imports.69: the Save fix -- the import sync's one commit, through the REAL
Imports.syncAll and the REAL Channels class.

Before imports.69 a sync saved with setChannels plus setImports(the whole
import list as the sync had read it), so a dashboard Save, Add, Delete or
Orphans made while the sync ran was undone by its write, and every Save
wiped the sync's own bookkeeping ("never synced"). Now syncAll saves once,
through Channels.commitSync: each import's own change list of sync fields
onto channels.json as it is at commit time (importcfg_helpers), the channel
records merged as a background writer, and nothing published that the
commit dropped.

Every test runs the real syncAll on a real Channels over a channels.json in
tmp_path (channels.CACHE_LOC and imports.CACHE_LOC point at tmp), with
Imports.fetchSource stubbed. A change made "while the sync runs" is made
inside the stubbed fetch, by a SECOND Channels instance that commits to the
file -- it replaces entries, it never mutates the sync's own objects, as a
dashboard request does in the add-on.

Sections:
  - changes made while a sync runs: edit, a legacy import the sync held back
    (gate or abandon check), add, delete (also with a refused commit),
    Orphans, playlist and guide address changes, Re-discover unticked;
  - Re-discover: back switched on as a new channel in one write, the number
    rules, a stale copy on disk, never a refreshed channel, unticked forever;
  - a merge error in the commit; the settings gate (config_key);
  - download markers bound to their address (m3u_source_key,
    epg_source_key, R4) and a Save never touching the keys;
  - the PVR-refresh signal; the one-time cleanup of display-only keys;
  - switching an import off and on (playlist, guide slice, boot render);
  - one persistence path: the shared test helper (fake_commit_sync) and
    Channels.commitSync call the planner alike;
  - Python 3.8 hygiene of imports.py and m3u.py.
All data is synthetic (example.invalid addresses, made-up ids).
"""
import ast
import copy
import json
import os
import time
from types import SimpleNamespace

import pytest
import xbmc

import channels as channels_module
import fake_commit_sync
import importcfg_helpers
import imports as imports_mod
import kick_helpers
import m3u as m3u_module
from channels import Channels, COMMIT_WRITTEN
from importcfg_helpers import config_fingerprint, source_key
from imports import Imports, RENDER_ONLY_KICK
from m3u import M3U
from xmltvs import XMLTVS

HERE = os.path.dirname(os.path.abspath(__file__))
LIB  = os.path.normpath(os.path.join(HERE, '..', 'resources', 'lib'))

NOW    = int(time.time())
CUSTOM = 'c' * 32 + '@PseudoTV_Live'

P1  = 'https://example.invalid/one.m3u'
P1B = 'https://example.invalid/one-moved.m3u'
G1  = 'https://example.invalid/one.xml'
G1B = 'https://example.invalid/one-moved.xml'
P2  = 'https://example.invalid/two.m3u'
G2  = 'https://example.invalid/two.xml'
PN  = 'https://example.invalid/new.m3u'

UNCHANGED = {'status': 304, 'content': None, 'etag': None, 'last_modified': None, 'error': None}
DOWN      = {'status': 0, 'content': None, 'etag': None, 'last_modified': None, 'error': 'connection refused'}


def ok(content, etag=None, last_modified=None):
    return {'status': 200, 'content': content, 'etag': etag, 'last_modified': last_modified, 'error': None}


def _ts(epoch):
    return time.strftime('%Y%m%d%H%M%S', time.gmtime(epoch)) + ' +0000'


def playlist(*chans):
    """An M3U with (tvg-id, name, tvg-chno) per channel."""
    lines = ['#EXTM3U']
    for eid, name, chno in chans:
        lines.append('#EXTINF:-1 tvg-id="%s" tvg-name="%s" tvg-chno="%d" group-title="Provider",%s'
                     % (eid, name, chno, name))
        lines.append('https://example.invalid/live/%s' % eid)
    return ('\n'.join(lines) + '\n').encode()


def guide(*eids):
    """An XMLTV guide with one channel and one show on air per tvg-id (no pictures)."""
    chans = ''.join('<channel id="%s"><display-name>Chan %s</display-name></channel>' % (e, e) for e in eids)
    progs = ''.join('<programme start="%s" stop="%s" channel="%s"><title>Show %s</title></programme>'
                    % (_ts(NOW - 1800), _ts(NOW + 1800), e, e) for e in eids)
    return ('<?xml version="1.0" encoding="UTF-8"?><tv>' + chans + progs + '</tv>').encode()


def cfg(iid, **over):
    """An import entry as the add-on stores it after earlier syncs (no
    imports.69 keys: a "legacy" entry). Due for a sync unless overridden."""
    entry = {'id': iid, 'name': 'Provider %s' % iid.upper(), 'enabled': True,
             'm3u_url': 'https://example.invalid/%s.m3u' % iid, 'm3u_path': '', 'epg_url': '', 'epg_path': '',
             'start_num': 1, 'refresh_interval_min': 600, 'respect_source_numbers': True,
             'rediscover_deleted': False, 'detect_placeholder_pictures': False,
             'last_sync_at': NOW - 11 * 3600, 'last_status': 'ok', 'playlist_rev': 1}
    entry.update(over)
    return entry


def rec(eid, iid, number, **over):
    """An imported channel record of import `iid`."""
    record = {'id': '%s@%s' % (eid, iid), 'epg_id': eid, 'name': 'Chan %s' % eid, 'source_name': 'Chan %s' % eid,
              'import_source': iid, 'type': 'import', 'enabled': True, 'number': number,
              'assigned_number': number, 'is_orphan': False, 'unallocated': False,
              'source_url': 'https://example.invalid/live/%s' % eid,
              'url': 'https://example.invalid/live/%s' % eid, 'group': ['Provider'], 'logo': '',
              'catchup': '', 'catchup-source': '', 'catchup-days': '', 'operator_overrides': []}
    record.update(over)
    return record


def custom(cid=CUSTOM, number=1, **over):
    record = {'id': cid, 'number': number, 'name': 'My channel %d' % number, 'path': ['/x'], 'enabled': True}
    record.update(over)
    return record


# ================================================================ harness


class _Source(object):
    """The stubbed Imports.fetchSource: per address a list of answers (the
    last repeats), every call recorded as (address, etag, last_modified),
    and `during[address]`, an action run once when that address is fetched
    -- what another thread does while the sync runs."""

    def __init__(self):
        self.answers, self.calls, self.during = {}, [], {}

    def answer(self, url, *answers):
        self.answers[url] = list(answers)

    def fetch(self, url, etag=None, last_modified=None):
        self.calls.append((url, etag, last_modified))
        action = self.during.pop(url, None)
        if action is not None:
            action()
        queue = self.answers[url]
        return dict(queue.pop(0) if len(queue) > 1 else queue[0])

    def sent(self, url):
        """(etag, last_modified) of every fetch of `url`."""
        return [(e, lm) for u, e, lm in self.calls if u == url]


class _M3U(object):
    def __init__(self):
        self.added, self.removed = [], []

    def addStation(self, station):
        self.added.append(station.get('id'))

    def delStation(self, station):
        self.removed.append(station.get('id'))


class _XMLTV(object):
    def __init__(self, programmes=()):
        self.XMLTVDATA = {'channels': [], 'programmes': list(programmes), 'recordings': []}


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    """Every test: channels.json in tmp_path, both CACHE_LOCs in tmp, a fixed
    uuid (getMYUUID would open the "name this device" dialog under the stubs),
    the disk-presence and guide self-heal triggers off (they would force
    syncs the tests don't ask for), no logo cache, the stubbed fetch, and the
    log lines, PVR-refresh signals and channels.json writes recorded."""
    cache = tmp_path / 'cache'
    cache.mkdir()
    path = tmp_path / 'channels.json'
    monkeypatch.setattr(channels_module.SETTINGS, 'getMYUUID', lambda: 'test-uuid')
    monkeypatch.setattr(channels_module, 'CACHE_LOC', str(cache))
    monkeypatch.setattr(imports_mod, 'CACHE_LOC', str(cache))
    monkeypatch.setattr(imports_mod, '_isDiskMissing', lambda iid, scope: False)
    monkeypatch.setattr(Imports, '_guideNeedsSelfHeal', lambda self, import_cfg, existing: False)
    imports_mod._GUIDE_SELFHEAL_LAST.clear()

    ints = {'Imports_Failure_Retry_Curve': 1, 'Imports_Failure_Abandon_Hours': 0, 'Imports_Disk_Presence_Scope': 0}
    real_int, real_bool = imports_mod.SETTINGS.getSettingInt, imports_mod.SETTINGS.getSettingBool

    def get_int(key, *a, **k):
        return ints[key] if key in ints else real_int(key, *a, **k)

    def get_bool(key, *a, **k):
        return False if key == 'Imports_Cache_Logos' else real_bool(key, *a, **k)
    monkeypatch.setattr(imports_mod.SETTINGS, 'getSettingInt', get_int)
    monkeypatch.setattr(imports_mod.SETTINGS, 'getSettingBool', get_bool)

    lines, timers, writes, fail_next = [], [], [], []

    def log(msg, level=xbmc.LOGDEBUG):
        lines.append((msg, level))
    monkeypatch.setattr(imports_mod, 'log', log)
    monkeypatch.setattr(channels_module, 'log', log)
    monkeypatch.setattr(imports_mod.PROPERTIES, 'setPropTimer', lambda key, *a, **k: timers.append(key))
    monkeypatch.setattr(channels_module.PROPERTIES, 'setHasChannels', lambda state=True: None)

    real_set_json = channels_module.FileAccess.setJSON

    def set_json(file, data, *a, **k):
        if file == str(path):
            writes.append(file)
            if fail_next:
                fail_next.pop()
                return False                      # one refused channels.json write
        return real_set_json(file, data, *a, **k)
    monkeypatch.setattr(channels_module.FileAccess, 'setJSON', set_json)

    source = _Source()
    monkeypatch.setattr(Imports, 'fetchSource',
                        lambda self, url, etag=None, last_modified=None, **kw: source.fetch(url, etag, last_modified))
    return SimpleNamespace(tmp=tmp_path, cache=cache, path=path, lines=lines, timers=timers, writes=writes,
                           fail_next=fail_next, source=source, ints=ints)


def write(env, imports, channels):
    env.path.write_text(json.dumps({'uuid': 'u', 'imports': imports, 'channels': channels}))


def disk(env):
    return json.loads(env.path.read_text())


def entry(env, iid):
    return [i for i in disk(env)['imports'] if i.get('id') == iid][0]


def record(env, cid):
    found = [c for c in disk(env)['channels'] if c.get('id') == cid]
    return found[0] if found else None


def ids(env):
    return {c['id'] for c in disk(env)['channels']}


def elsewhere(env):
    """Another copy of channels.json, as the dashboard's request handler has."""
    return Channels(file=str(env.path), writable=True)


def sync(env, force_scope=None, store=None, xmltv=None):
    store = store if store is not None else Channels(file=str(env.path), writable=True)
    run = SimpleNamespace(m3u=_M3U(), xmltv=xmltv if xmltv is not None else _XMLTV())
    run.res = Imports(channels=store, m3u=run.m3u, xmltv=run.xmltv).syncAll(force_scope=force_scope)
    return run


def logged(env, needle, level=None):
    return [m for m, lvl in env.lines if needle in m and (level is None or lvl == level)]


def edit(env, iid, **fields):
    summary = elsewhere(env).saveImportRequest({'op': 'edit', 'id': iid, 'entry': fields})
    assert summary['status'] == COMMIT_WRITTEN and summary['changed'], summary
    return summary


# ================================================================ changes made while a sync runs


def test_a_save_made_while_the_sync_runs_survives_and_the_next_cycle_syncs_it(env):
    """The Save stays; the sync's own bookkeeping is saved; config_key holds
    the settings the sync acted on, so the next cycle syncs the import again
    (with one full read), then it settles."""
    write(env, [cfg('one')], [custom(), rec('A1', 'one', 11)])
    env.source.answer(P1, ok(playlist(('A1', 'Alpha', 11)), etag='"p2"'))
    env.source.during[P1] = lambda: edit(env, 'one', refresh_interval_min=590)
    run = sync(env)
    assert run.res['one']['status'] == 'ok'
    saved = entry(env, 'one')
    assert saved['refresh_interval_min'] == 590, 'the Save made during the sync survives its commit'
    assert saved['last_sync_at'] >= NOW and saved['etag'] == '"p2"' and saved['last_status'] == 'ok'
    assert saved['m3u_source_key'] == source_key(P1)
    assert saved['config_key'] == config_fingerprint(cfg('one')), 'the settings this sync acted on'
    assert importcfg_helpers.config_changed(saved)

    run = sync(env)                             # no kick; the refresh gate alone would skip it
    assert run.res['one']['status'] == 'ok'
    assert env.source.sent(P1)[-1] == (None, None), 'one full read: its markers are not sent'
    assert logged(env, '[one] syncAll, settings changed since its last sync -- syncing it now', xbmc.LOGINFO)
    assert logged(env, '[one] syncOne, settings changed -- reading the playlist in full once', xbmc.LOGINFO)
    settled = entry(env, 'one')
    assert settled['config_key'] == config_fingerprint(settled) and settled['refresh_interval_min'] == 590
    assert sync(env).res['one']['status'] == 'skipped'


@pytest.mark.parametrize('held_back_by', ['refresh gate', 'abandon check'])
def test_a_legacy_import_held_back_by_this_sync_and_saved_during_it_keeps_pending(env, held_back_by):
    """A gate-skipped or abandoned result is a copy of the start entry: it
    applies nothing, so the 'pending' of a Save made meanwhile stays, and the
    next render-kick cycle forces the import with a full read."""
    two = cfg('two', last_sync_at=NOW - 60, etag='v2', last_modified='lm2')
    if held_back_by == 'abandon check':
        env.ints['Imports_Failure_Abandon_Hours'] = 1
        two.update(last_status='failed', fail_count=3, failed_since=NOW - 7200, last_error='down')
    write(env, [cfg('one'), two], [custom(), rec('A1', 'one', 11), rec('B1', 'two', 21)])
    env.source.answer(P1, ok(playlist(('A1', 'Alpha', 11))))
    env.source.answer(P2, ok(playlist(('B1', 'Beta', 21)), etag='"p2-new"'))
    env.source.during[P1] = lambda: edit(env, 'two', start_num=40)
    run = sync(env)
    assert run.res['two']['status'] == ('skipped' if held_back_by == 'refresh gate' else 'abandoned')
    saved = entry(env, 'two')
    assert saved['config_key'] == 'pending' and saved['start_num'] == 40
    assert saved['last_sync_at'] == NOW - 60 and saved['etag'] == 'v2', 'nothing of the held-back result applied'

    run = sync(env, force_scope=RENDER_ONLY_KICK)
    assert run.res['two']['status'] == 'ok'
    assert env.source.sent(P2) == [(None, None)], 'forced with one full read: v2/lm2 not sent'
    saved = entry(env, 'two')
    assert saved['config_key'] == config_fingerprint(saved) and saved['etag'] == '"p2-new"'


def test_an_import_added_while_the_sync_runs_survives_with_its_guide_cache(env):
    write(env, [cfg('one')], [custom(), rec('A1', 'one', 11)])
    env.source.answer(P1, ok(playlist(('A1', 'Alpha', 11))))

    def add():
        summary = elsewhere(env).saveImportRequest({'op': 'add', 'entry': {'id': 'new', 'name': 'New',
                                                                            'm3u_url': PN, 'enabled': True}})
        assert summary['status'] == COMMIT_WRITTEN
        (env.cache / 'epg_new.xml').write_bytes(guide('N1'))
    env.source.during[P1] = add
    sync(env)
    assert [i['id'] for i in disk(env)['imports']] == ['one', 'new']
    assert 'last_sync_at' not in entry(env, 'new'), 'stored as added: its gate stays open'
    assert (env.cache / 'epg_new.xml').exists(), 'a listed import keeps its guide cache'


def _two_imports_with_guides(env):
    write(env, [cfg('one', epg_url=G1), cfg('two', epg_url=G2)],
          [custom(), rec('A1', 'one', 11), rec('B1', 'two', 21)])
    (env.cache / 'epg_one.xml').write_bytes(guide('A1'))
    (env.cache / 'epg_two.xml').write_bytes(guide('B1'))
    env.source.answer(P1, ok(playlist(('A1', 'Alpha', 11))))
    env.source.answer(G1, ok(guide('A1')))
    env.source.answer(P2, ok(playlist(('B1', 'Beta', 21), ('B2', 'Beta Two', 22))))
    env.source.answer(G2, ok(guide('B1', 'B2')))


def _delete_two(env):
    summary = elsewhere(env).saveImportRequest({'op': 'delete', 'id': 'two'})
    assert summary['status'] == COMMIT_WRITTEN and summary['purged_channels'] == 1
    assert not (env.cache / 'epg_two.xml').exists(), 'the Delete removed its guide cache'


def test_an_import_deleted_while_the_sync_runs_stays_deleted(env):
    """The sync still syncs the deleted import (it read it at the start) and
    re-writes its guide cache: its results are dropped, its records are never
    written or published, and the commit's sweep removes the cache again."""
    _two_imports_with_guides(env)
    env.source.during[P2] = lambda: _delete_two(env)
    run = sync(env)
    assert [i['id'] for i in disk(env)['imports']] == ['one']
    assert not [c for c in disk(env)['channels'] if c.get('import_source') == 'two']
    assert not (env.cache / 'epg_two.xml').exists(), "the sync's own re-write is swept after its commit"
    assert (env.cache / 'epg_one.xml').exists()
    assert not [s for s in run.m3u.added if s.endswith('@two')], 'not pushed to the playlist'
    assert 'A1@one' in run.m3u.added
    assert not [p for p in run.xmltv.XMLTVDATA['programmes'] if p['channel'].endswith('@two')], 'nor to the guide'
    assert logged(env, 'syncAll, [two] was deleted during this sync -- its results are dropped', xbmc.LOGINFO)


def test_a_deleted_imports_cache_rewritten_by_a_refused_sync_goes_at_the_next_commit(env):
    _two_imports_with_guides(env)

    def delete_then_refuse_the_sync():
        _delete_two(env)
        env.fail_next.append(True)                # the sync's own commit fails to write
    env.source.during[P2] = delete_then_refuse_the_sync
    sync(env)
    assert logged(env, "syncAll, this sync's results were not saved (failed); the next sync redoes them",
                  xbmc.LOGERROR)
    assert [i['id'] for i in disk(env)['imports']] == ['one'], 'the Delete stays'
    assert (env.cache / 'epg_two.xml').exists(), 're-written by the sync whose commit was refused'
    edit(env, 'one', name='Renamed')              # the next import-list commit
    assert not (env.cache / 'epg_two.xml').exists()
    assert (env.cache / 'epg_one.xml').exists()


def test_orphans_removed_while_the_sync_runs_stay_removed(env):
    """The sync's start copy still holds the orphan (it re-marks it as an
    orphan and writes it as touched): every tombstone on disk at commit time
    is a delete, so it stays removed. One PVR-refresh signal."""
    write(env, [cfg('one')], [custom(), rec('A1', 'one', 11), rec('OLD', 'one', 12, is_orphan=True)])
    env.source.answer(P1, ok(playlist(('A1', 'Alpha', 11))))

    def orphans():
        result = elsewhere(env).removeOrphans('one')
        assert result['status'] == COMMIT_WRITTEN and result['removed'] == 1
    env.source.during[P1] = orphans
    run = sync(env)
    assert run.res['one']['orphans'], 'the sync saw the orphan in its start copy'
    assert record(env, 'OLD@one') is None
    assert entry(env, 'one')['tombstones'] == ['OLD@one']
    assert env.timers.count('chkPVRRefresh') == 1


def test_a_playlist_address_changed_while_the_sync_runs(env):
    """The results describe the old address: its markers, source key and
    channel changes are dropped (last_sync_at and config_key still apply);
    the next cycle reads the new address in full."""
    write(env, [cfg('one', etag='p1-old', links_mtime_ns='123', m3u_source_key=source_key(P1))],
          [custom(), rec('A1', 'one', 11)])
    env.source.answer(P1, ok(playlist(('A1', 'Alpha', 11), ('B1', 'Beta', 12)), etag='"p1-new"'))
    env.source.answer(P1B, ok(playlist(('C1', 'Gamma', 13)), etag='"p1b"'))
    env.source.during[P1] = lambda: edit(env, 'one', m3u_url=P1B)
    run = sync(env)
    saved = entry(env, 'one')
    assert saved['m3u_url'] == P1B
    assert 'etag' not in saved and 'links_mtime_ns' not in saved, 'neither the old markers nor the moved ones'
    assert saved['m3u_source_key'] == source_key(P1), "the Save never touches it; the sync's was dropped"
    assert saved['last_sync_at'] >= NOW
    assert ids(env) == {CUSTOM, 'A1@one'}, "the old address's new channel is not written"
    assert 'B1@one' not in run.m3u.added
    assert logged(env, 'syncAll, [one] playlist address changed during this sync -- its channel changes are '
                       'dropped; the next sync reads the new address', xbmc.LOGINFO)

    sync(env)
    assert env.source.sent(P1B) == [(None, None)]
    assert logged(env, '[one] syncOne, playlist address changed since its saved validators -- reading it in full',
                  xbmc.LOGINFO)
    saved = entry(env, 'one')
    assert saved['m3u_source_key'] == source_key(P1B) and saved['etag'] == '"p1b"'
    assert 'C1@one' in ids(env)


@pytest.mark.parametrize('download', ['cached', 'failed'])
def test_a_guide_address_changed_while_the_sync_runs(env, download):
    """The guide results describe the old address: the guide group is
    dropped and this import's guide is not re-published. When this sync
    replaced the cache file, no stored marker may describe another download
    and the key names the address the file came from; otherwise the guide
    group on disk is kept as it is. (The change is a hand edit that kept the
    old markers -- a dashboard Save drops them itself.)"""
    write(env, [cfg('one', epg_url=G1, epg_etag='g-old', epg_last_modified='g-lm')],
          [custom(), rec('A1', 'one', 11)])
    (env.cache / 'epg_one.xml').write_bytes(guide('A1'))
    env.source.answer(P1, UNCHANGED)
    env.source.answer(G1, ok(guide('A1', 'Z9'), etag='"g1-new"') if download == 'cached' else DOWN)

    def hand_edit():
        other = elsewhere(env)
        assert other.setImports([dict(i, epg_url=G1B) for i in other.getImports()]) is True
    env.source.during[G1] = hand_edit
    old_show = {'channel': 'A1@one', 'start': _ts(NOW - 7200), 'stop': _ts(NOW - 3600), 'title': [('Old', '')]}
    run = sync(env, xmltv=_XMLTV([old_show]))
    saved = entry(env, 'one')
    assert saved['epg_url'] == G1B
    if download == 'cached':
        assert run.res['one'].get('epg_cache_written') is True
        assert 'epg_etag' not in saved and 'epg_last_modified' not in saved
        assert saved['epg_source_key'] == source_key(G1)
        assert (env.cache / 'epg_one.xml').read_bytes() == guide('A1', 'Z9')
    else:
        assert (saved['epg_etag'], saved['epg_last_modified']) == ('g-old', 'g-lm')
        assert 'epg_source_key' not in saved and 'epg_fail_count' not in saved
    assert run.xmltv.XMLTVDATA['programmes'] == [old_show], 'the XML branch skipped this import'
    assert logged(env, 'syncAll, [one] guide address changed during this sync -- its guide results are dropped',
                  xbmc.LOGINFO)


def test_one_of_your_own_channels_deleted_while_the_sync_runs_stays_deleted(env):
    """The sync's list carries the Custom records it read at its start; the
    commit (a background writer) never re-creates a record gone from the file
    that it didn't touch (the rule moved from imports.71 into imports.69)."""
    second = 'd' * 32 + '@PseudoTV_Live'
    write(env, [cfg('one')], [custom(), custom(second, 2), rec('A1', 'one', 11)])
    env.source.answer(P1, ok(playlist(('A1', 'Alpha', 11))))

    def delete_custom():
        other = elsewhere(env)
        assert other.setChannels([c for c in other.getChannels() if c['id'] != second]) is True
    env.source.during[P1] = delete_custom
    sync(env)
    assert ids(env) == {CUSTOM, 'A1@one'}
    assert logged(env, 'commitSync, 1 record(s) deleted from channels.json since this copy was loaded were not '
                       'written back', xbmc.LOGINFO)


def test_re_discover_unticked_while_the_sync_runs_keeps_the_channel_deleted(env):
    write(env, [cfg('one', rediscover_deleted=True, tombstones=['B1@one'])], [custom(), rec('A1', 'one', 11)])
    env.source.answer(P1, ok(playlist(('A1', 'Alpha', 11), ('B1', 'Beta', 12))))
    env.source.during[P1] = lambda: edit(env, 'one', rediscover_deleted=False)
    run = sync(env)
    assert run.res['one']['rediscovered'] == ['B1@one'], 'the sync, ticked at its start, re-created it'
    assert record(env, 'B1@one') is None
    assert entry(env, 'one')['tombstones'] == ['B1@one']
    assert 'B1@one' not in run.m3u.added


# ================================================================ Re-discover


def test_re_discover_brings_a_deleted_channel_back_switched_on_in_one_write(env):
    write(env, [cfg('one', last_sync_at=NOW - 60, tombstones=['B1@one'])], [custom(), rec('A1', 'one', 11)])
    edit(env, 'one', rediscover_deleted=True)     # ticked: 'pending', so this cycle syncs it
    env.source.answer(P1, ok(playlist(('A1', 'Alpha', 11), ('B1', 'Beta', 12))))
    writes_before = len(env.writes)
    run = sync(env, force_scope=RENDER_ONLY_KICK)
    assert len(env.writes) - writes_before == 1, 'record and tombstone in one write'
    back = record(env, 'B1@one')
    assert back is not None
    assert (back['enabled'], back['number'], back['name'], back['operator_overrides'], back['unallocated']) == \
        (True, 12, 'Beta', [], False)
    assert 'tombstones' not in entry(env, 'one')
    assert run.res['one']['rediscovered'] == ['B1@one'], 'the refreshed A1 is never reported'
    assert 'B1@one' in run.m3u.added
    assert logged(env, "syncAll, [one] re-discovered 1 deleted channel(s): ['B1@one']", xbmc.LOGINFO)


@pytest.mark.parametrize('case, respect, start_num, taken, limit, number, enabled', [
    ("the provider's number", True, 1, (), None, 12, True),
    ('the next free number above it', True, 1, (12,), None, 13, True),
    ('the next free number from the start number', False, 50, (), None, 50, True),
    ('no number free: back switched off', True, 1, (12, 13), 13, 9000, False),
])
def test_re_discover_number_rules(env, monkeypatch, case, respect, start_num, taken, limit, number, enabled):
    channels = [custom(), rec('A1', 'one', 11)]
    channels += [custom('%032d@PseudoTV_Live' % n, n) for n in taken]
    write(env, [cfg('one', tombstones=['B1@one'], respect_source_numbers=respect, start_num=start_num,
                    rediscover_deleted=True)], channels)
    if limit is not None:
        monkeypatch.setattr(imports_mod, 'CASCADE_OVERFLOW_LIMIT', limit)
    env.source.answer(P1, ok(playlist(('A1', 'Alpha', 11), ('B1', 'Beta', 12))))
    sync(env, force_scope='all')
    back = record(env, 'B1@one')
    assert back is not None, case
    assert back['enabled'] is enabled, case
    if enabled:
        assert back['number'] == number, case
    else:
        assert back['unallocated'] is True, case


def test_re_discover_with_a_stale_copy_on_disk_comes_back_as_a_new_channel(env):
    """A copy of the record written back before imports.69 (renamed, switched
    off) is replaced: provider name, no overrides, switched on. Its number
    follows the stale copy (the cascade reads it; switched off = the 9000+
    range): the documented rare exception (design 2.5.3)."""
    stale = rec('B1', 'one', 9000, assigned_number=9000, name='My own name', operator_overrides=['name'],
                enabled=False)
    write(env, [cfg('one', tombstones=['B1@one'], rediscover_deleted=True)], [custom(), rec('A1', 'one', 11), stale])
    env.source.answer(P1, ok(playlist(('A1', 'Alpha', 11), ('B1', 'Beta', 12))))
    run = sync(env, force_scope='all')
    back = record(env, 'B1@one')
    assert (back['name'], back['operator_overrides'], back['enabled']) == ('Beta', [], True)
    assert back['number'] >= 9000
    assert run.res['one']['rediscovered'] == ['B1@one']
    assert 'tombstones' not in entry(env, 'one')


def test_a_refreshed_channel_is_never_reported_as_re_discovered(env):
    write(env, [cfg('one', tombstones=['GONE@one'], rediscover_deleted=True)], [custom(), rec('A1', 'one', 11)])
    env.source.answer(P1, ok(playlist(('A1', 'Alpha', 11))))
    run = sync(env, force_scope='all')
    assert not run.res['one'].get('rediscovered')
    assert not logged(env, 're-discovered')
    assert entry(env, 'one')['tombstones'] == ['GONE@one']


def test_re_discover_unticked_keeps_deleted_channels_deleted_cycle_after_cycle(env):
    write(env, [cfg('one', tombstones=['B1@one'])], [custom(), rec('A1', 'one', 11)])
    env.source.answer(P1, ok(playlist(('A1', 'Alpha', 11), ('B1', 'Beta', 12))))
    for _cycle in range(3):
        run = sync(env, force_scope='all')
        assert record(env, 'B1@one') is None and 'B1@one' not in run.m3u.added
        assert not run.res['one'].get('rediscovered')
        assert entry(env, 'one')['tombstones'] == ['B1@one']


# ================================================================ a failed commit


def test_a_merge_error_in_the_sync_commit_writes_nothing_and_the_cycle_still_renders(env, monkeypatch):
    write(env, [cfg('one', epg_url=G1)], [custom(), rec('A1', 'one', 11)])
    env.source.answer(P1, ok(playlist(('A1', 'Alpha', 11))))
    env.source.answer(G1, ok(guide('A1')))
    before = env.path.read_bytes()

    def boom(*a, **k):
        raise RuntimeError('simulated merge failure')
    monkeypatch.setattr(channels_module.Channels, '_mergeRecords', staticmethod(boom))
    run = sync(env)
    assert env.path.read_bytes() == before
    assert logged(env, "syncAll, saving this sync's results raised: simulated merge failure", xbmc.LOGERROR)
    assert logged(env, "syncAll, this sync's results were not saved (failed)", xbmc.LOGERROR)
    assert 'A1@one' in run.m3u.added, 'the playlist is still rendered'
    assert [p['channel'] for p in run.xmltv.XMLTVDATA['programmes']] == ['A1@one'], 'and the guide'


# ================================================================ the settings gate (config_key)


def test_a_legacy_entry_is_not_forced(env):
    write(env, [cfg('one', last_sync_at=NOW - 60)], [custom(), rec('A1', 'one', 11)])
    run = sync(env)
    assert run.res['one']['status'] == 'skipped'
    assert 'config_key' not in entry(env, 'one')
    assert not logged(env, 'settings changed')


@pytest.mark.parametrize('answer, over, status, last_status', [
    (ok(playlist(('A1', 'Alpha', 11))), {}, 'ok', 'ok'),
    (UNCHANGED, {'etag': 'v1'}, 'unchanged', 'unchanged'),
    (DOWN, {}, 'failed', 'failed'),
    (ok(b'#EXTM3U\n'), {}, 'failed', 'warning'),        # an empty playlist: channels kept
    (None, {'enabled': False}, 'disabled', 'ok'),
])
def test_config_key_is_recorded_after_every_attempted_outcome(env, answer, over, status, last_status):
    write(env, [cfg('one', **over)], [custom(), rec('A1', 'one', 11)])
    if answer is not None:
        env.source.answer(P1, answer)
    run = sync(env)
    assert run.res['one']['status'] == status
    saved = entry(env, 'one')
    assert saved['last_status'] == last_status
    assert saved['config_key'] == config_fingerprint(cfg('one', **over))
    assert not importcfg_helpers.config_changed(saved), 'the next cycle is not forced by it'


def test_pending_forces_the_import_with_one_full_read(env):
    write(env, [cfg('one', last_sync_at=NOW - 60, etag='v1', last_modified='lm1', config_key='pending')],
          [custom(), rec('A1', 'one', 11)])
    env.source.answer(P1, ok(playlist(('A1', 'Alpha', 11)), etag='"v2"'))
    run = sync(env)
    assert run.res['one']['status'] == 'ok'
    assert env.source.sent(P1) == [(None, None)], 'its markers are not sent'
    assert logged(env, '[one] syncAll, settings changed since its last sync -- syncing it now', xbmc.LOGINFO)
    saved = entry(env, 'one')
    assert saved['config_key'] == config_fingerprint(saved) and saved['etag'] == '"v2"'


def test_switching_an_import_back_on_by_hand_forces_it(env):
    write(env, [cfg('one', enabled=False, last_sync_at=NOW - 60)], [custom(), rec('A1', 'one', 11)])
    assert sync(env).res['one']['status'] == 'disabled'
    assert entry(env, 'one')['config_key'] == config_fingerprint(cfg('one', enabled=False))
    other = elsewhere(env)
    assert other.setImports([dict(i, enabled=True) for i in other.getImports()]) is True   # no 'pending'
    env.source.answer(P1, ok(playlist(('A1', 'Alpha', 11))))
    assert sync(env).res['one']['status'] == 'ok', 'synced at once, although it synced seconds ago'


# ================================================================ download markers and their address


def test_playlist_markers_are_never_sent_to_another_address(env):
    write(env, [cfg('one', m3u_url=P1B, etag='from-p1', last_modified='lm-p1', m3u_source_key=source_key(P1))],
          [custom(), rec('A1', 'one', 11)])
    env.source.answer(P1B, ok(playlist(('A1', 'Alpha', 11)), etag='"p1b"'))
    sync(env, force_scope='all')
    assert env.source.sent(P1B) == [(None, None)]
    saved = entry(env, 'one')
    assert saved['etag'] == '"p1b"' and 'last_modified' not in saved
    assert saved['m3u_source_key'] == source_key(P1B)
    assert logged(env, '[one] syncOne, playlist address changed since its saved validators -- reading it in full',
                  xbmc.LOGINFO)


@pytest.mark.parametrize('stored_key', [None, source_key(P1)])
def test_markers_of_the_same_address_are_sent(env, stored_key):
    """A legacy entry (no key) and an entry keyed to this address send their
    markers; the key is recorded at the 304 that confirms them."""
    over = {'etag': 'v1', 'last_modified': 'lm1'}
    if stored_key:
        over['m3u_source_key'] = stored_key
    write(env, [cfg('one', **over)], [custom(), rec('A1', 'one', 11)])
    env.source.answer(P1, UNCHANGED)
    run = sync(env, force_scope='all')
    assert run.res['one']['status'] == 'unchanged'
    assert env.source.sent(P1) == [('v1', 'lm1')]
    assert entry(env, 'one')['m3u_source_key'] == source_key(P1)


@pytest.mark.parametrize('case, answer, cache, stored, expected, written', [
    ('downloaded and cached', ok(guide('A1'), etag='"g"'), False, None, source_key(G1), True),
    ('unchanged, cached guide loaded, no key stored (R4)', UNCHANGED, True, None, source_key(G1), False),
    ("unchanged, another address's key stored", UNCHANGED, True, source_key(G1B), source_key(G1B), False),
    ('download failed, cached guide used', DOWN, True, None, None, False),
    ('unchanged but no cached guide', UNCHANGED, False, None, None, False),
    ('download failed, no cached guide', DOWN, False, None, None, False),
])
def test_the_guide_key_names_the_address_whose_download_is_cached(env, case, answer, cache, stored, expected,
                                                                    written):
    """epg_source_key is written where this sync replaced the cache file, and
    at a 304 that loaded the cached guide while no key is stored (step 2's
    R4) -- never on a failure or a fallback, and a stored key is never
    replaced on a 304. Markers keyed to another address are not sent."""
    over = {'epg_url': G1, 'epg_etag': 'g1'}
    if stored:
        over['epg_source_key'] = stored
    write(env, [cfg('one', **over)], [custom(), rec('A1', 'one', 11)])
    if cache:
        (env.cache / 'epg_one.xml').write_bytes(guide('A1'))
    env.source.answer(P1, UNCHANGED)
    env.source.answer(G1, answer)
    run = sync(env, force_scope='all')
    assert entry(env, 'one').get('epg_source_key') == expected, case
    assert bool(run.res['one'].get('epg_cache_written')) is written, case
    assert env.source.sent(G1) == [((None, None) if stored else ('g1', None))], case


def test_a_save_never_touches_the_source_keys(env):
    write(env, [cfg('one', epg_url=G1)], [custom(), rec('A1', 'one', 11)])
    env.source.answer(P1, ok(playlist(('A1', 'Alpha', 11)), etag='"p"'))
    env.source.answer(G1, ok(guide('A1'), etag='"g"'))
    env.source.answer(G1B, ok(guide('A1'), etag='"g1b"'))
    sync(env)
    keys = {k: entry(env, 'one')[k] for k in ('m3u_source_key', 'epg_source_key')}
    assert keys == {'m3u_source_key': source_key(P1), 'epg_source_key': source_key(G1)}
    edit(env, 'one', name='Renamed')
    edit(env, 'one', epg_url=G1B)
    saved = entry(env, 'one')
    assert {k: saved[k] for k in keys} == keys
    assert 'epg_etag' not in saved, 'the Save drops the old guide markers'
    sync(env)                                    # forced by 'pending'
    assert env.source.sent(G1B) == [(None, None)]
    assert logged(env, '[one] syncOne, guide address changed since its saved validators -- full download',
                  xbmc.LOGINFO)
    assert entry(env, 'one')['epg_source_key'] == source_key(G1B)


# ================================================================ PVR refresh and the one-time cleanup


@pytest.mark.parametrize('record_on_disk, write_refused, signals', [
    (False, False, False),
    (True, False, True),
    (True, True, False),
])
def test_the_pvr_refresh_signal_needs_an_applied_delete(env, record_on_disk, write_refused, signals):
    """A kept tombstone whose record is long gone no longer signals every
    cycle; a tombstoned record still on disk is removed and signals -- only
    when the commit was written."""
    channels = [custom(), rec('A1', 'one', 11)]
    if record_on_disk:
        channels.append(rec('GONE', 'one', 12))
    write(env, [cfg('one', etag='v1', tombstones=['GONE@one'])], channels)
    env.source.answer(P1, UNCHANGED)
    if write_refused:
        env.fail_next.append(True)
    run = sync(env, force_scope='all')
    assert run.res['one']['status'] == 'unchanged' and not run.res['one']['orphans']
    assert (record(env, 'GONE@one') is None) is not write_refused
    assert env.timers == (['chkPVRRefresh'] if signals else [])
    if not signals:
        assert logged(env, 'skipping chkPVRRefresh signal (all imports status=unchanged/disabled, no deletes '
                           'applied, no orphans)', xbmc.LOGINFO)


def test_a_sync_commit_cleans_display_only_keys_off_every_entry(env):
    two = cfg('two', last_sync_at=NOW - 60, last_status='abandoned', next_retry_at=NOW + 60,
              display_status='abandoned')
    write(env, [cfg('one'), two], [custom(), rec('A1', 'one', 11)])
    env.source.answer(P1, ok(playlist(('A1', 'Alpha', 11))))
    run = sync(env)
    assert run.res['two']['status'] == 'skipped'
    saved = entry(env, 'two')
    assert saved['last_status'] == 'failed'
    assert 'next_retry_at' not in saved and 'display_status' not in saved


# ================================================================ switching an import off (extra 4)


@pytest.fixture
def bound(env, monkeypatch):
    """Channels() in channels.py and in m3u.py (M3U._verify uses its own
    binding) read this test's channels.json."""
    path = str(env.path)

    class _Bound(Channels):
        def __init__(self, file=None, writable=False):
            super(_Bound, self).__init__(file=path, writable=writable)
    monkeypatch.setattr(channels_module, 'Channels', _Bound)
    monkeypatch.setattr(m3u_module, 'Channels', _Bound)
    return _Bound


def _on_and_off(env):
    write(env, [cfg('on'), cfg('off', enabled=False, last_sync_at=NOW - 60)],
          [custom(), rec('A1', 'on', 11), rec('B1', 'off', 77, operator_overrides=['number'])])


class _GuideOnly(XMLTVS):
    """The real XMLTVS.cleanSelf, without loading a file."""
    def __init__(self):
        self.m3u, self.XMLTVDATA = None, {}

    def log(self, msg, level=None):
        pass


def test_a_switched_off_import_leaves_the_playlist_and_keeps_its_guide_slice(env, bound):
    _on_and_off(env)
    stations = [{'id': CUSTOM, 'number': 1}, {'id': 'A1@on', 'number': 11}, {'id': 'B1@off', 'number': 77}]
    kept, _recordings = M3U.__new__(M3U).cleanSelf(stations)
    assert [s['id'] for s in kept] == [CUSTOM, 'A1@on']
    guide_channels = [{'id': 'A1@on'}, {'id': 'B1@off'}]
    shows = [{'channel': cid, 'start': _ts(NOW - 600), 'stop': _ts(NOW + 600), 'title': [('Show', '')]}
             for cid in ('A1@on', 'B1@off')]
    loader = _GuideOnly()
    channels, _ = loader.cleanSelf(guide_channels, 'id')
    assert {c['id'] for c in channels} == {'A1@on', 'B1@off'}, 'the guide slice stays, frozen'
    assert {p['channel'] for p in loader.cleanSelf(shows, 'channel')} == {'A1@on', 'B1@off'}
    assert record(env, 'B1@off')['number'] == 77, 'its records stay with their settings'


def test_the_boot_render_skips_a_switched_off_import(env, monkeypatch):
    _on_and_off(env)
    monkeypatch.setattr(imports_mod, '_isDiskMissing', lambda import_id, scope: True)

    class _Playlist(object):
        def __init__(self):
            self.M3UDATA = {'stations': [], 'recordings': []}

        def _save(self):
            return True
    playlist_store = _Playlist()
    importer = Imports(channels=Channels(file=str(env.path)), m3u=playlist_store)
    assert importer.renderFromPersistedState() == 1
    assert [s['id'] for s in playlist_store.M3UDATA['stations']] == ['A1@on']


def test_switching_it_back_on_syncs_it_and_republishes_its_channels(env, bound):
    _on_and_off(env)
    env.source.answer('https://example.invalid/on.m3u', ok(playlist(('A1', 'Alpha', 11))))
    env.source.answer('https://example.invalid/off.m3u', ok(playlist(('B1', 'Beta', 21))))
    run = sync(env)
    assert run.res['off']['status'] == 'disabled' and 'B1@off' not in run.m3u.added
    assert record(env, 'B1@off')['number'] == 77
    edit(env, 'off', enabled=True)
    run = sync(env, force_scope=RENDER_ONLY_KICK)
    assert run.res['off']['status'] == 'ok'
    assert 'B1@off' in run.m3u.added
    assert record(env, 'B1@off')['number'] == 77, 'back with its dashboard number'


# ================================================================ one persistence path


class _ListStore(object):
    """An in-memory store like the syncAll tests' fakes, recording its calls."""
    def __init__(self, imports, channels):
        self.imports, self.channels, self.calls = list(imports), list(channels), []

    def getImports(self):
        return list(self.imports)

    def getChannels(self):
        return list(self.channels)

    def setChannels(self, channels=None, modified_ids=None, deleted_ids=None):
        self.calls.append(('setChannels', channels, modified_ids, deleted_ids))
        self.channels = list(channels)
        return True

    def setImports(self, data):
        self.calls.append(('setImports', data))
        self.imports = list(data)
        return True

    commitSync = fake_commit_sync.commit_sync


def _commit_inputs():
    imports = [cfg('one', tombstones=['T@one'], rediscover_deleted=True), cfg('two', last_sync_at=NOW - 60)]
    channels = [custom(), rec('A1', 'one', 11), rec('T', 'one', 12), rec('B1', 'two', 21)]
    start = {i['id']: copy.deepcopy(i) for i in imports}
    results = {'one': {'status': 'ok', 'updated_cfg': dict(imports[0], last_sync_at=NOW, etag='"e"'),
                       'rediscovered': ['T@one'], 'epg_cache_written': True},
               'two': {'status': 'skipped', 'updated_cfg': dict(imports[1])}}
    merged = [custom(), rec('A1', 'one', 13), rec('T', 'one', 12)]
    return imports, channels, start, results, merged, {'A1@one', 'T@one'}, {'T@one'}


def test_the_shared_test_helper_and_channels_commitsync_call_the_planner_alike(env, monkeypatch):
    imports, channels, start, results, merged, mods, dels = _commit_inputs()
    write(env, imports, channels)
    calls = []
    real = importcfg_helpers.plan_sync_commit

    def recorder(*args, **kwargs):
        calls.append((copy.deepcopy(args), copy.deepcopy(kwargs)))
        return real(*args, **kwargs)
    monkeypatch.setattr(importcfg_helpers, 'plan_sync_commit', recorder)
    info = Channels(file=str(env.path), writable=True).commitSync(start, results, merged, mods, dels)
    store = _ListStore(copy.deepcopy(imports), copy.deepcopy(channels))
    fake_info = fake_commit_sync.commit_sync(store, start, results, merged, mods, dels)
    assert len(calls) == 2
    assert calls[0] == calls[1], 'same positional arguments, no keywords, on both paths'
    assert calls[0][1] == {} and len(calls[0][0]) == 7
    assert info['status'] == fake_info['status'] == 'written'
    assert store.imports == disk(env)['imports'], 'and the same import list stored'


def test_the_shared_test_helper_makes_the_two_store_calls_with_the_planners_lists():
    imports, channels, start, results, merged, mods, dels = _commit_inputs()
    store = _ListStore(copy.deepcopy(imports), copy.deepcopy(channels))
    expected = importcfg_helpers.plan_sync_commit(
        *fake_commit_sync.planner_arguments(store, start, results, merged, mods, dels))
    new_imports, writer, plan_mods, plan_dels, _info = expected
    info = store.commitSync(start, results, merged, mods, dels)
    assert store.calls == [('setChannels', writer, plan_mods, plan_dels), ('setImports', new_imports)]
    assert info['status'] == 'written' and info['kept'] == {'one': {'T@one'}}
    assert 'T@one' not in plan_dels, 'a kept re-discovery is never deleted by its own tombstone'

    class _OldSignature(_ListStore):            # test_imports_module.py's store: no deleted_ids
        def setChannels(self, channels=None, modified_ids=None):
            self.calls.append(('setChannels', channels, modified_ids))
            return True
    old = _OldSignature(copy.deepcopy(imports), copy.deepcopy(channels))
    old.commitSync(start, results, merged, mods, dels)
    assert old.calls[0] == ('setChannels', writer, plan_mods)

    class _Refusing(_ListStore):
        def setImports(self, data):
            return False
    assert _Refusing(imports, channels).commitSync(start, results, merged, mods, dels)['status'] == 'failed'


def _function(src, name):
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError('no function %s' % name)


def _src(name):
    with open(os.path.join(LIB, name), encoding='utf-8') as f:
        return f.read()


def test_syncall_saves_through_commitsync_only():
    """One production persistence path: syncAll calls self.channels.commitSync
    once, never setChannels/setImports, and has no getattr fallback."""
    body = _function(_src('imports.py'), 'syncAll')
    calls = [n for n in ast.walk(body) if isinstance(n, ast.Call)]
    methods = [n.func for n in calls if isinstance(n.func, ast.Attribute)]
    commit = [m for m in methods if m.attr == 'commitSync']
    assert len(commit) == 1
    owner = commit[0].value
    assert isinstance(owner, ast.Attribute) and owner.attr == 'channels'
    assert isinstance(owner.value, ast.Name) and owner.value.id == 'self'
    assert not [m for m in methods if m.attr in ('setChannels', 'setImports')]
    assert not [n for n in calls if isinstance(n.func, ast.Name) and n.func.id in ('getattr', 'hasattr')
                and any(isinstance(a, ast.Constant) and a.value == 'commitSync' for a in n.args)]


def test_a_store_without_commitsync_saves_nothing_and_the_cycle_still_renders(env):
    class _NoCommit(object):
        def __init__(self):
            self.calls = []

        def getImports(self):
            return [cfg('one')]

        def getChannels(self):
            return [custom(), rec('A1', 'one', 11)]

        def setChannels(self, channels=None, modified_ids=None, deleted_ids=None):
            self.calls.append('setChannels')
            return True

        def setImports(self, data):
            self.calls.append('setImports')
            return True
    store = _NoCommit()
    env.source.answer(P1, ok(playlist(('A1', 'Alpha', 11))))
    run = sync(env, store=store)
    assert store.calls == [], 'no second persistence path'
    assert logged(env, "syncAll, this sync's results were not saved (failed)", xbmc.LOGERROR)
    assert 'A1@one' in run.m3u.added


# ================================================================ hygiene


@pytest.mark.parametrize('name', ['imports.py', 'm3u.py'])
def test_changed_files_are_python38(name):
    """Kodi 19/20 run Python 3.8: the AST check plus the shared deny-list
    ('|' is checked by the one scanner in test_imports69_importcfg_helpers)."""
    from test_imports63_clock import py39_api_leaks
    src = _src(name)
    ast.parse(src, feature_version=(3, 8))
    assert py39_api_leaks(src) == []
    assert 'madteevee (imports fork, imports.69)' in src


def test_the_new_sync_keys_are_written_only_where_they_belong():
    """The SYNC_FIELDS completeness scan (every key written through
    updated_cfg is a sync field, and none is missing) lives in
    test_imports69_importcfg_helpers.py; this pins where the three keys
    imports.69 adds are written, with that scan: m3u_source_key at the 304
    and the parsed 200, epg_source_key at the cached download and the keyless
    304 (R4) -- both in syncOne -- and config_key once, in syncAll."""
    from test_imports69_importcfg_helpers import _updated_cfg_writes
    src = _src('imports.py')
    functions = [fn for fn in ast.walk(ast.parse(src)) if isinstance(fn, ast.FunctionDef)]

    def owner(line):
        inside = [fn for fn in functions if fn.lineno <= line <= fn.end_lineno]
        return max(inside, key=lambda fn: fn.lineno).name
    writes = _updated_cfg_writes(src)
    found = {key: sorted(owner(line) for line in writes.get(key, ()))
             for key in ('m3u_source_key', 'epg_source_key', 'config_key')}
    assert found == {'m3u_source_key': ['syncOne', 'syncOne'], 'epg_source_key': ['syncOne', 'syncOne'],
                     'config_key': ['syncAll']}


def test_render_kick_has_one_definition_re_exported_by_imports():
    assert imports_mod.RENDER_ONLY_KICK is kick_helpers.RENDER_ONLY_KICK
    tree = ast.parse(_src('imports.py'))
    assigned = [t.id for n in ast.walk(tree) if isinstance(n, ast.Assign) for t in n.targets
                if isinstance(t, ast.Name)]
    assert 'RENDER_ONLY_KICK' not in assigned, 'defined in kick_helpers only'
