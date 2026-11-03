"""imports.59 (S10) — Movistar+ replay links merged through the real syncAll.

davidmuma stays the Movistar+ import's guide source (descriptions...); the
replay links (catchup-id) come from Movistar+'s own guide export, epg.xml next
to the import's playlist (replay_helpers.exporter_for). syncAll reads it after
the channel records are merged (Imports._readLinks: fetchSource, the safe
local read, and a strict parse limited to the link channels) and merges it on
the fresh path (_mergeReplayLinks) and on both keep-previous paths
(_mergeLinksIntoKept), keeping linked history from the guide data as loaded.

Ported from the plan's prototype (proto/final_proto_test_links.py and
final_proto_test_adversarial.py), every case kept; the prototype read the
links file directly, production goes through fetchSource.
"""
import os
import sys
import time

import pytest

import imports as imports_mod
from imports import Imports

IID = 'mp'
EPG_URL = 'https://example.invalid/rich.xml.gz'


def _ts(epoch):
    return time.strftime('%Y%m%d%H%M%S', time.gmtime(epoch)) + ' +0000'


NOW = int(time.time()) // 3600 * 3600
SLOTS = [(NOW - 2 * 3600, NOW - 3600), (NOW - 3600, NOW + 1800), (NOW + 1800, NOW + 3600)]


def _rich_guide():
    progs = ''.join(
        '<programme start="%s" stop="%s" channel="Chan One HD"><title>Show %d</title><desc>Rich %d</desc></programme>'
        % (_ts(s), _ts(e), i, i) for i, (s, e) in enumerate(SLOTS))
    # one more far in the future, beyond the export
    progs += '<programme start="%s" stop="%s" channel="Chan One HD"><title>Later</title><desc>Rich later</desc></programme>' % (
        _ts(NOW + 10 * 3600), _ts(NOW + 11 * 3600))
    return ('<?xml version="1.0" encoding="UTF-8"?><tv><channel id="Chan One HD"><display-name>Chan One</display-name></channel>'
            + progs + '</tv>').encode()


def _export():
    progs = ''.join(
        '<programme start="%s" stop="%s" channel="C1" catchup-id="plugin://x/?id=%d&amp;a=b"><title>Show %d</title><sub-title></sub-title></programme>'
        % (_ts(s), _ts(e), i, i) for i, (s, e) in enumerate(SLOTS))
    progs += '<programme start="%s" stop="%s" channel="C2" catchup-id="plugin://x/?id=99"><title>Only export</title></programme>' % (
        _ts(NOW - 600), _ts(NOW + 600))
    return ('<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE tv SYSTEM "xmltv.dtd"><tv>'
            '<channel id="C1"><display-name>Chan One</display-name><icon src="http://logo/c1"/></channel>'
            '<channel id="C2"><display-name>Squirrel</display-name><icon src="http://logo/c2"/></channel>'
            + progs + '</tv>').encode()


class _Ch:
    def __init__(self, imports_list, channels_list):
        self._i, self._c = list(imports_list), list(channels_list)
    def getImports(self):  return list(self._i)
    def getChannels(self): return list(self._c)
    def setChannels(self, channels=None, modified_ids=None, deleted_ids=None):
        if channels is not None: self._c = list(channels)
        return True
    def setImports(self, data):
        self._i = list(data)
        return True


class _M3U:
    def __init__(self): self.added = []
    def addStation(self, s): self.added.append(s)
    def delStation(self, s): pass


class _X:
    def __init__(self, channels=None, programmes=None):
        self.XMLTVDATA = {'channels': list(channels or []), 'programmes': list(programmes or []), 'recordings': []}


def _rec(tvg, name, src_name=None):
    cid = '%s@%s' % (tvg, IID)
    return {'id': cid, 'epg_id': tvg, 'name': name, 'source_name': src_name or name,
            'import_source': IID, 'type': 'import', 'enabled': True, 'number': 1 if tvg == 'C1' else 2,
            'assigned_number': 1 if tvg == 'C1' else 2, 'is_orphan': False, 'unallocated': False,
            'source_url': 'plugin://x/?live=%s' % tvg, 'url': 'plugin://x/?live=%s' % tvg,
            'group': ['MP'], 'logo': '', 'catchup': 'default', 'catchup-source': '{catchup-id}',
            'catchup-days': '7', 'operator_overrides': []}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(imports_mod, 'CACHE_LOC', str(tmp_path))
    monkeypatch.setattr(imports_mod, '_isDiskMissing', lambda iid, scope: False)
    imports_mod._GUIDE_SELFHEAL_LAST.clear()
    folder = tmp_path / 'addon_data' / 'plugin.video.movistarplus'
    folder.mkdir(parents=True)
    (folder / 'channels.m3u8').write_text('#EXTM3U\n')
    (folder / 'epg.xml').write_bytes(_export())
    return tmp_path, folder


def _cfg(folder, **over):
    cfg = {'id': IID, 'name': 'MP', 'enabled': True, 'm3u_path': str(folder / 'channels.m3u8'), 'm3u_url': '',
           'epg_url': EPG_URL, 'epg_path': '', 'start_num': 1, 'refresh_interval_min': 600,
           'last_sync_at': int(time.time()) - 11 * 3600, 'last_modified': '1', 'tombstones': [],
           'respect_source_numbers': True, 'rediscover_deleted': False}
    cfg.update(over)
    return cfg


def _run(monkeypatch, folder, cfg, xmltv=None, channels=None):
    real_fetch = Imports.fetchSource
    calls = []
    def stub(self, url, etag=None, last_modified=None, **kw):
        calls.append(url)
        base = {'status': 0, 'content': None, 'etag': None, 'last_modified': None, 'error': None}
        if url.endswith('channels.m3u8'):
            base['status'] = 304
            return base
        if url == EPG_URL:
            base.update(status=200, content=_rich_guide())
            return base
        if url.endswith('epg.xml'):
            return real_fetch(self, url, etag=etag, last_modified=last_modified)
        raise AssertionError(url)
    monkeypatch.setattr(Imports, 'fetchSource', stub)
    other = [{'channel': 'x@other', 'start': _ts(NOW), 'stop': _ts(NOW + 60), 'title': [('Other', '')]}]
    xmltv = xmltv or _X(channels=[{'id': 'x@other', 'display-name': [('Other', '')]}], programmes=other)
    chans = channels or [_rec('C1', 'Renamed One', 'Chan One'), _rec('C2', 'Squirrel')]
    m3u = _M3U()
    imp = Imports(channels=_Ch([cfg], chans), m3u=m3u, xmltv=xmltv)
    res = imp.syncAll()
    return res, xmltv, m3u, calls


def test_merge_gives_rich_content_plus_links(env, monkeypatch):
    tmp_path, folder = env
    res, xmltv, m3u, calls = _run(monkeypatch, folder, _cfg(folder))
    progs = [p for p in xmltv.XMLTVDATA['programmes'] if p['channel'] == 'C1@mp']
    assert [p['catchup-id'] for p in progs] == ['plugin://x/?id=0&a=b', 'plugin://x/?id=1&a=b', 'plugin://x/?id=2&a=b', '']
    assert [p['desc'][0][0] for p in progs] == ['Rich 0', 'Rich 1', 'Rich 2', 'Rich later']   # renamed channel still matched (S18)
    squi = [p for p in xmltv.XMLTVDATA['programmes'] if p['channel'] == 'C2@mp']
    assert squi and squi[0]['title'][0][0] == 'Only export'
    assert 'C2@mp' in {c['id'] for c in xmltv.XMLTVDATA['channels']}                        # export channel element
    assert [p for p in xmltv.XMLTVDATA['programmes'] if p['channel'] == 'x@other'][0]['title'][0][0] == 'Other'
    st = {s['id']: s for s in m3u.added}
    assert st['C1@mp']['catchup'] == 'default' and st['C1@mp']['catchup-source'] == '{catchup-id}'
    assert isinstance(res[IID]['updated_cfg'].get('links_mtime_ns'), str)
    assert res[IID]['updated_cfg'].get('last_error') is None


def test_links_file_missing_keeps_previous_links(env, monkeypatch):
    tmp_path, folder = env
    prev = _X(channels=[{'id': 'C1@mp', 'display-name': [('Chan One', '')]}],
              programmes=[{'channel': 'C1@mp', 'start': _ts(s), 'stop': _ts(e), 'catchup-id': 'old%d' % i,
                           'title': [('Show %d' % i, '')]} for i, (s, e) in enumerate(SLOTS)])
    (folder / 'epg.xml').unlink()
    res, xmltv, m3u, calls = _run(monkeypatch, folder, _cfg(folder), xmltv=prev)
    progs = [p for p in xmltv.XMLTVDATA['programmes'] if p['channel'] == 'C1@mp']
    assert [p['catchup-id'] for p in progs][:3] == ['old0', 'old1', 'old2']
    assert progs[0]['desc'][0][0] == 'Rich 0'
    assert "can't be read (missing)" in res[IID]['updated_cfg']['last_error']
    assert 'links_mtime_ns' not in res[IID]['updated_cfg']


def test_no_links_turns_replay_off(env, monkeypatch):
    tmp_path, folder = env
    (folder / 'epg.xml').write_bytes(_export().replace(b' catchup-id="plugin://x/?id=', b' data-x="'))
    res, xmltv, m3u, calls = _run(monkeypatch, folder, _cfg(folder))
    st = {s['id']: s for s in m3u.added}
    assert st['C1@mp']['catchup'] == '' and st['C1@mp']['catchup-source'] == ''
    assert 'no replay links' in res[IID]['updated_cfg']['last_error']


def test_new_links_file_forces_gated_import(env, monkeypatch):
    tmp_path, folder = env
    mtime_ns = os.stat(str(folder / 'epg.xml')).st_mtime_ns
    gated = _cfg(folder, last_sync_at=int(time.time()) - 60, links_mtime_ns=mtime_ns - 10**9)
    res, xmltv, m3u, calls = _run(monkeypatch, folder, gated)
    assert res[IID]['status'] != 'skipped'
    same = _cfg(folder, last_sync_at=int(time.time()) - 60, links_mtime_ns=mtime_ns)
    loaded = _X(channels=[{'id': 'C1@mp', 'display-name': [('Chan One', '')]}],
                programmes=[{'channel': 'C1@mp', 'start': _ts(NOW), 'stop': _ts(NOW + 3600), 'catchup-id': 'x', 'title': [('t', '')]}])
    res, xmltv, m3u, calls = _run(monkeypatch, folder, same, xmltv=loaded)
    assert res[IID]['status'] == 'skipped'


def test_truncated_links_file_is_rejected(env, monkeypatch):
    tmp_path, folder = env
    data = _export()
    (folder / 'epg.xml').write_bytes(data[: len(data) // 2])
    _age(folder, 600)                       # not one Movistar+ may still be writing
    res, xmltv, m3u, calls = _run(monkeypatch, folder, _cfg(folder))
    assert "can't be read" in res[IID]['updated_cfg']['last_error']
    assert isinstance(res[IID]['updated_cfg'].get('links_mtime_ns'), str)   # stable read: not re-forced hourly


def _age(folder, seconds):
    t = time.time() - seconds
    os.utime(str(folder / 'epg.xml'), (t, t))


def _nolink(rec):
    rec = dict(rec)
    rec['catchup-source'] = ''
    rec['catchup-days'] = '5'
    return rec


def test_stage1_rollback_is_quiet(env, monkeypatch):
    """Row restored (catch-up not link-based): no links read, no message, no forced sync."""
    tmp_path, folder = env
    chans = [_nolink(_rec('C1', 'Renamed One', 'Chan One')), _nolink(_rec('C2', 'Squirrel'))]
    res, xmltv, m3u, calls = _run(monkeypatch, folder, _cfg(folder), channels=chans)
    assert 'links_mtime_ns' not in res[IID]['updated_cfg']
    assert res[IID]['updated_cfg'].get('last_error') is None
    assert not any(p.get('catchup-id') for p in xmltv.XMLTVDATA['programmes'] if p['channel'] == 'C1@mp')
    assert not any(c.endswith('epg.xml') for c in calls), 'the links file is not even read'
    gated = _cfg(folder, last_sync_at=int(time.time()) - 60)
    res, xmltv, m3u, calls = _run(monkeypatch, folder, gated, channels=chans,
                                  xmltv=_X(channels=[{'id': 'C1@mp', 'display-name': [('Chan One', '')]}],
                                           programmes=[{'channel': 'C1@mp', 'start': _ts(NOW), 'stop': _ts(NOW + 3600), 'title': [('t', '')]}]))
    assert res[IID]['status'] == 'skipped'


def test_empty_links_file_is_reported_once(env, monkeypatch):
    tmp_path, folder = env
    (folder / 'epg.xml').write_bytes(b'')
    _age(folder, 600)
    res, xmltv, m3u, calls = _run(monkeypatch, folder, _cfg(folder))
    assert "can't be read (empty file)" in res[IID]['updated_cfg']['last_error']
    stored = res[IID]['updated_cfg']['links_mtime_ns']
    gated = _cfg(folder, last_sync_at=int(time.time()) - 60, links_mtime_ns=stored)
    loaded = _X(channels=[{'id': 'C1@mp', 'display-name': [('Chan One', '')]}],
                programmes=[{'channel': 'C1@mp', 'start': _ts(NOW), 'stop': _ts(NOW + 3600), 'catchup-id': 'x', 'title': [('t', '')]}])
    res, xmltv, m3u, calls = _run(monkeypatch, folder, gated, xmltv=loaded)
    assert res[IID]['status'] == 'skipped'


def test_file_changing_during_read_is_transient(env, monkeypatch):
    tmp_path, folder = env
    real = Imports._localFileState
    state = {'n': 0}
    def flaky(path):
        st = real(path)
        state['n'] += 1
        return st if state['n'] % 2 else (st[0] + 1, st[1] + 1)
    monkeypatch.setattr(Imports, '_localFileState', staticmethod(flaky))
    res, xmltv, m3u, calls = _run(monkeypatch, folder, _cfg(folder))
    assert 'links_mtime_ns' not in res[IID]['updated_cfg']
    assert res[IID]['updated_cfg'].get('last_error') is None


def test_zero_match_path_merges_links(env, monkeypatch):
    tmp_path, folder = env
    monkeypatch.setattr(sys.modules[__name__], '_rich_guide',
                        lambda: b'<?xml version="1.0"?><tv><channel id="Nobody"><display-name>Nobody</display-name></channel>'
                                b'<programme start="20990101000000 +0000" stop="20990101010000 +0000" channel="Nobody"><title>x</title></programme></tv>')
    prev = _X(channels=[{'id': 'C1@mp', 'display-name': [('Chan One', '')]}],
              programmes=[{'channel': 'C1@mp', 'start': _ts(s), 'stop': _ts(e), 'title': [('Show %d' % i, '')],
                           'desc': [('Rich %d' % i, '')]} for i, (s, e) in enumerate(SLOTS)])
    res, xmltv, m3u, calls = _run(monkeypatch, folder, _cfg(folder), xmltv=prev)
    progs = [p for p in xmltv.XMLTVDATA['programmes'] if p['channel'] == 'C1@mp']
    assert [p.get('catchup-id') for p in progs] == ['plugin://x/?id=0&a=b', 'plugin://x/?id=1&a=b', 'plugin://x/?id=2&a=b']
    assert [p['desc'][0][0] for p in progs] == ['Rich 0', 'Rich 1', 'Rich 2']   # kept details


def test_export_parse_limited_to_link_channels(env, monkeypatch):
    tmp_path, folder = env
    data = _export().replace(b'</tv>', b'<programme start="%s" stop="%s" channel="C9" catchup-id="z"><title>off</title></programme></tv>'
                             % (_ts(NOW).encode(), _ts(NOW + 600).encode()))
    (folder / 'epg.xml').write_bytes(data)
    res, xmltv, m3u, calls = _run(monkeypatch, folder, _cfg(folder))
    assert not any(p.get('channel') == 'C9' for k, p in res[IID]['links']['pairs'] if k == 'programme')


# ---------------------------------------------------------------- reviewer's adversarial cases


def test_rich_content_survives_davidmuma_window_move(env, monkeypatch):
    """Slot 0 was published earlier WITH davidmuma content + link. davidmuma's
    next file no longer covers slot 0 (its TV-day window moved), but the
    Movistar+ export still lists slot 0 (export day starts 00:00).
    Expectation (user decision 2: keep the rich guide): slot 0 keeps 'Rich 0'."""
    tmp_path, folder = env
    prev = _X(channels=[{'id': 'C1@mp', 'display-name': [('Chan One', '')]}],
              programmes=[{'channel': 'C1@mp', 'start': _ts(s), 'stop': _ts(e),
                           'catchup-id': 'plugin://x/?id=%d&a=b' % i,
                           'title': [('Show %d' % i, '')], 'desc': [('Rich %d' % i, '')]}
                          for i, (s, e) in enumerate(SLOTS)])
    real_rich = _rich_guide
    def moved():
        data = real_rich()
        # drop slot 0 from the rich guide (window moved forward)
        s0 = ('<programme start="%s"' % _ts(SLOTS[0][0])).encode()
        a = data.index(s0)
        b = data.index(b'</programme>', a) + len(b'</programme>')
        return data[:a] + data[b:]
    monkeypatch.setattr(sys.modules[__name__], '_rich_guide', moved)
    res, xmltv, m3u, calls = _run(monkeypatch, folder, _cfg(folder), xmltv=prev)
    progs = [p for p in xmltv.XMLTVDATA['programmes'] if p['channel'] == 'C1@mp']
    p0 = [p for p in progs if p['start'] == _ts(SLOTS[0][0])][0]
    assert p0['catchup-id'] == 'plugin://x/?id=0&a=b'
    assert p0.get('desc'), 'rich content of an already-published show was replaced by the plain export entry'
    # the moved davidmuma file was published (fresh path), not just the kept slice
    assert 'Rich later' in [p['desc'][0][0] for p in progs if p.get('desc')]


def test_keep_previous_path_still_merges_fresh_links(env, monkeypatch):
    """davidmuma unusable (download fails, no cache) while the Movistar+ export
    is fresh: the keep-previous branch must still merge the fresh links, since
    links_mtime_ns advances (S17 will not retry)."""
    tmp_path, folder = env
    prev = _X(channels=[{'id': 'C1@mp', 'display-name': [('Chan One', '')]}],
              programmes=[{'channel': 'C1@mp', 'start': _ts(s), 'stop': _ts(e), 'catchup-id': '',
                           'title': [('Show %d' % i, '')], 'desc': [('Rich %d' % i, '')]}
                          for i, (s, e) in enumerate(SLOTS)])
    real_fetch = Imports.fetchSource
    def stub(self, url, etag=None, last_modified=None, **kw):
        base = {'status': 0, 'content': None, 'etag': None, 'last_modified': None, 'error': None}
        if url.endswith('channels.m3u8'):
            base['status'] = 304
            return base
        if url == EPG_URL:
            base['error'] = 'network down'
            return base
        return real_fetch(self, url)
    monkeypatch.setattr(Imports, 'fetchSource', stub)
    chans = [_rec('C1', 'Renamed One', 'Chan One'), _rec('C2', 'Squirrel')]
    imp = Imports(channels=_Ch([_cfg(folder)], chans), m3u=_M3U(), xmltv=prev)
    res = imp.syncAll()
    progs = [p for p in prev.XMLTVDATA['programmes'] if p['channel'] == 'C1@mp']
    linked = [p for p in progs if p.get('catchup-id')]
    assert res[IID]['updated_cfg'].get('links_mtime_ns'), 'validator advanced'
    assert linked, 'fresh export links were not merged on the keep-previous path'
    assert [p['desc'][0][0] for p in linked] == ['Rich 0', 'Rich 1', 'Rich 2']


# ---------------------------------------------------------------- production-path details


def test_links_mtime_ns_is_the_files_own(env, monkeypatch):
    tmp_path, folder = env
    res, xmltv, m3u, calls = _run(monkeypatch, folder, _cfg(folder))
    assert res[IID]['updated_cfg']['links_mtime_ns'] == str(os.stat(str(folder / 'epg.xml')).st_mtime_ns)
    assert calls.count(str(folder / 'epg.xml')) == 1, 'one full read per sync, through fetchSource'


def test_links_read_takes_no_validators(env, monkeypatch):
    """A full read every sync: the stored links_mtime_ns never turns the read
    into a 304."""
    tmp_path, folder = env
    seen = []
    real = Imports._fetchLocal
    def spy(self, path, last_modified):
        if path.endswith('epg.xml'):
            seen.append(last_modified)
        return real(self, path, last_modified)
    monkeypatch.setattr(Imports, '_fetchLocal', spy)
    mtime_ns = os.stat(str(folder / 'epg.xml')).st_mtime_ns
    res, xmltv, m3u, calls = _run(monkeypatch, folder, _cfg(folder, links_mtime_ns=mtime_ns))
    assert seen == [None]
    assert any(p.get('catchup-id') for p in xmltv.XMLTVDATA['programmes'] if p['channel'] == 'C1@mp')


def test_other_imports_and_other_channels_untouched(env, monkeypatch):
    """Guide entries of another import pass through unchanged and in order."""
    tmp_path, folder = env
    other = [{'channel': 'x@other', 'start': _ts(NOW + i * 60), 'stop': _ts(NOW + i * 60 + 60),
              'title': [('Other %d' % i, '')]} for i in range(3)]
    loaded = _X(channels=[{'id': 'x@other', 'display-name': [('Other', '')]}], programmes=list(other))
    res, xmltv, m3u, calls = _run(monkeypatch, folder, _cfg(folder), xmltv=loaded)
    assert [p for p in xmltv.XMLTVDATA['programmes'] if p['channel'] == 'x@other'] == other
    assert [c['id'] for c in xmltv.XMLTVDATA['channels'] if c['id'] == 'x@other'] == ['x@other']


def test_unexpected_read_error_still_keeps_the_linked_history(env, monkeypatch):
    """An unexpected error in the links read merges with no new links, so the
    linked history already published stays (the fresh path's prune would
    otherwise drop it); the guide source's entries are still refreshed."""
    tmp_path, folder = env
    prev = _X(channels=[{'id': 'C1@mp', 'display-name': [('Chan One', '')]}],
              programmes=[{'channel': 'C1@mp', 'start': _ts(NOW - 2 * 86400), 'stop': _ts(NOW - 2 * 86400 + 3600),
                           'catchup-id': 'kept', 'title': [('Old', '')]}])
    def boom(self, *a, **k):
        raise RuntimeError('simulated read bug')
    monkeypatch.setattr(Imports, '_readLinks', boom)
    res, xmltv, m3u, calls = _run(monkeypatch, folder, _cfg(folder), xmltv=prev)
    progs = [p for p in xmltv.XMLTVDATA['programmes'] if p['channel'] == 'C1@mp']
    assert [p['catchup-id'] for p in progs if p.get('catchup-id')] == ['kept']
    assert 'Rich later' in [p['desc'][0][0] for p in progs if p.get('desc')]
    assert res[IID]['status'] == 'unchanged' and 'links_mtime_ns' not in res[IID]['updated_cfg']


def test_merge_failure_keeps_the_previous_slice(env, monkeypatch):
    """A merge error never costs the kept history: the import keeps its
    previous guide, as when its guide source is unavailable."""
    tmp_path, folder = env
    prev = _X(channels=[{'id': 'C1@mp', 'display-name': [('Chan One', '')]}],
              programmes=[{'channel': 'C1@mp', 'start': _ts(NOW - 3 * 86400), 'stop': _ts(NOW - 3 * 86400 + 3600),
                           'catchup-id': 'kept', 'title': [('Old', '')]}])
    def boom(*a, **k):
        raise RuntimeError('simulated merge bug')
    monkeypatch.setattr(imports_mod.replay_helpers, 'merge_replay_guide', boom)
    res, xmltv, m3u, calls = _run(monkeypatch, folder, _cfg(folder), xmltv=prev)
    progs = [p for p in xmltv.XMLTVDATA['programmes'] if p['channel'] == 'C1@mp']
    assert [p['catchup-id'] for p in progs] == ['kept']
