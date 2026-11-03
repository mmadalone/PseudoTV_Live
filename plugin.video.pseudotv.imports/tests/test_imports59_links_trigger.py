"""imports.59 (S17) — a new Movistar+ guide export reaches the guide within
the hour.

davidmuma usually answers 304 and the Movistar+ import is gated for its
refresh interval (10 h on madteevee), so without a trigger a new export --
new replay links -- would wait that long. syncAll's gate now forces a sync
(same pattern as the imports.43 disk-presence and imports.56 self-heal
checks) when Imports._linksChanged: the import's playlist is a known
exporter's, its STORED records use link-based replay, and the links file's
mtime_ns (os.stat via _localFileState, never xbmcvfs.Stat) differs from the
stored links_mtime_ns. Not for a disabled import, nor one whose last sync
failed (the failure backoff rules). A missing file or any error: no force.

links_mtime_ns is stored as a decimal string and compared as text: the number
(about 1.79e18) is above JavaScript's 2^53, and the dashboard posts the whole
import list back after any import edit, rounded -- which forced an extra
Movistar+ sync after every save.

A stable read that can't be parsed is recorded (not re-read every hour),
except when the file was modified less than LINKS_SETTLE_SECS (120 s) before
the read: Movistar+ writes it in place with one write() and Linux sets the
mtime when that write starts, so a stalled writer can leave a stable but
cut-off file whose final mtime never changes again. That read is retried.
"""
import json
import os
import time

import pytest

import imports as imports_mod
import replay_helpers
import xbmcvfs
from imports import Imports


IID     = 'mp'
EPG_URL = 'https://example.invalid/rich.xml'
NOW     = int(time.time())


def _ts(epoch):
    return time.strftime('%Y%m%d%H%M%S', time.gmtime(epoch)) + ' +0000'


EXPORT = ('<?xml version="1.0" encoding="UTF-8"?><tv><channel id="C1"><display-name>Chan One</display-name>'
          '</channel><programme start="%s" stop="%s" channel="C1" catchup-id="plugin://x/?id=1"><title>Show'
          '</title></programme></tv>' % (_ts(NOW - 1800), _ts(NOW + 1800))).encode()
RICH = ('<?xml version="1.0" encoding="UTF-8"?><tv><channel id="Chan One"><display-name>Chan One</display-name>'
        '</channel><programme start="%s" stop="%s" channel="Chan One"><title>Show</title><desc>Rich</desc>'
        '</programme></tv>' % (_ts(NOW - 1800), _ts(NOW + 1800))).encode()


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
    def addStation(self, s): pass
    def delStation(self, s): pass


class _X:
    """Guide data with a current programme, so the imports.56 self-heal
    stays quiet and only S17 can force the sync."""
    def __init__(self):
        self.XMLTVDATA = {'channels': [{'id': 'C1@mp', 'display-name': [('Chan One', '')]}],
                          'programmes': [{'channel': 'C1@mp', 'start': _ts(NOW - 1800), 'stop': _ts(NOW + 1800),
                                          'catchup-id': 'x', 'title': [('t', '')]}],
                          'recordings': []}


def _rec(source='{catchup-id}', days='7'):
    return {'id': 'C1@mp', 'epg_id': 'C1', 'name': 'Chan One', 'source_name': 'Chan One',
            'import_source': IID, 'type': 'import', 'enabled': True, 'number': 1, 'assigned_number': 1,
            'is_orphan': False, 'unallocated': False, 'source_url': 'plugin://x/?live=C1',
            'url': 'plugin://x/?live=C1', 'group': ['MP'], 'logo': '', 'catchup': 'default',
            'catchup-source': source, 'catchup-days': days, 'operator_overrides': []}


@pytest.fixture
def folder(tmp_path, monkeypatch):
    monkeypatch.setattr(imports_mod, 'CACHE_LOC', str(tmp_path))
    monkeypatch.setattr(imports_mod, '_isDiskMissing', lambda iid, scope: False)
    imports_mod._GUIDE_SELFHEAL_LAST.clear()
    real_getInt = imports_mod.SETTINGS.getSettingInt
    def fake_getInt(key, default=0):
        if key == 'Imports_Failure_Retry_Curve':   return 1     # aggressive
        if key == 'Imports_Failure_Abandon_Hours': return 0
        try:    return real_getInt(key, default)
        except TypeError: return real_getInt(key)
    monkeypatch.setattr(imports_mod.SETTINGS, 'getSettingInt', fake_getInt)
    monkeypatch.setattr(imports_mod.random, 'randint', lambda a, b: 0)
    folder = tmp_path / 'addon_data' / 'plugin.video.movistarplus'
    folder.mkdir(parents=True)
    (folder / 'channels.m3u8').write_text('#EXTM3U\n')
    (folder / 'epg.xml').write_bytes(EXPORT)
    return folder


def _mtime_ns(folder):
    return os.stat(str(folder / 'epg.xml')).st_mtime_ns


def _age(folder, seconds):
    """Make the links file `seconds` old."""
    t = time.time() - seconds
    os.utime(str(folder / 'epg.xml'), (t, t))


def _gated(folder, **over):
    """Synced a minute ago with a 10 h refresh interval: skipped unless
    forced. links_mtime_ns defaults to the current file's."""
    cfg = {'id': IID, 'name': 'MP', 'enabled': True, 'm3u_path': str(folder / 'channels.m3u8'),
           'epg_url': EPG_URL, 'start_num': 1, 'tombstones': [], 'playlist_rev': 1,
           'refresh_interval_min': 600, 'last_sync_at': int(time.time()) - 60,
           'last_status': 'unchanged'}
    cfg.update(over)
    if 'links_mtime_ns' not in over:
        cfg['links_mtime_ns'] = _mtime_ns(folder)
    return cfg


def _sync(monkeypatch, cfg, records=None):
    real_fetch = Imports.fetchSource
    reads = []
    def stub(self, url, etag=None, last_modified=None, **kw):
        base = {'status': 0, 'content': None, 'etag': None, 'last_modified': None, 'error': None}
        if url.endswith('channels.m3u8'):
            base['status'] = 304
            return base
        if url == EPG_URL:
            base.update(status=200, content=RICH)
            return base
        reads.append(url)
        return real_fetch(self, url, etag=etag, last_modified=last_modified)
    monkeypatch.setattr(Imports, 'fetchSource', stub)
    res = Imports(channels=_Ch([cfg], records or [_rec()]), m3u=_M3U(), xmltv=_X()).syncAll()
    return res[IID], reads


def test_new_file_forces_a_gated_import(folder, monkeypatch):
    res, reads = _sync(monkeypatch, _gated(folder, links_mtime_ns=_mtime_ns(folder) - 10 ** 9))
    assert res['status'] == 'unchanged', 'synced, not skipped'
    assert res['updated_cfg']['links_mtime_ns'] == str(_mtime_ns(folder))
    assert reads == [str(folder / 'epg.xml')]


# ---------------------------------------------------------------- stored as text (FIX-4)


FILE_NS    = 1793642969358653307
JS_ROUNDED = 1793642969358653200     # what the dashboard's JSON.parse/stringify posts back


def test_stored_string_equal_to_the_file_doesnt_force(folder, monkeypatch):
    res, reads = _sync(monkeypatch, _gated(folder, links_mtime_ns=str(_mtime_ns(folder))))
    assert res['status'] == 'skipped'
    assert reads == []


def test_js_rounded_number_forces_one_sync_then_the_exact_string_is_stored(folder, monkeypatch):
    path = str(folder / 'epg.xml')
    os.utime(path, ns=(FILE_NS, FILE_NS))
    assert _mtime_ns(folder) == FILE_NS
    res, reads = _sync(monkeypatch, _gated(folder, links_mtime_ns=JS_ROUNDED))
    assert res['status'] == 'unchanged' and reads == [path], 'one forced sync'
    stored = res['updated_cfg']['links_mtime_ns']
    assert stored == '1793642969358653307'
    # A string comes back from the dashboard as it went: no further force.
    res, reads = _sync(monkeypatch, _gated(folder, links_mtime_ns=json.loads(json.dumps(stored))))
    assert res['status'] == 'skipped' and reads == []


def test_read_links_stores_a_decimal_string(folder):
    exporter = replay_helpers.exporter_for({'m3u_path': str(folder / 'channels.m3u8')})
    cfg = {}
    out = Imports()._readLinks(IID, exporter, cfg, {'C1'})
    assert out['warning'] is None and out['pairs']
    assert isinstance(cfg['links_mtime_ns'], str)
    assert cfg['links_mtime_ns'] == str(_mtime_ns(folder))


def test_same_file_stays_skipped(folder, monkeypatch):
    res, reads = _sync(monkeypatch, _gated(folder))
    assert res['status'] == 'skipped'
    assert reads == []


def test_never_read_file_forces_once(folder, monkeypatch):
    """No links_mtime_ns stored yet (first sync after the update was already
    gated): the file counts as new."""
    cfg = _gated(folder)
    del cfg['links_mtime_ns']
    res, _reads = _sync(monkeypatch, cfg)
    assert res['status'] == 'unchanged'


def test_records_not_link_based_never_force(folder, monkeypatch):
    """Old catch-up row restored (rollback stage 1): no force, no read."""
    cfg = _gated(folder, links_mtime_ns=1)
    res, reads = _sync(monkeypatch, cfg, records=[_rec(source='', days='5')])
    assert res['status'] == 'skipped'
    assert reads == []
    assert res['updated_cfg'].get('last_error') is None


@pytest.mark.parametrize('content', [b'', EXPORT[:len(EXPORT) // 2]])
def test_unusable_stable_file_is_recorded_and_not_re_forced(folder, monkeypatch, content):
    (folder / 'epg.xml').write_bytes(content)
    _age(folder, 600)                               # written 10 minutes ago: not still being written
    res, _reads = _sync(monkeypatch, _gated(folder, links_mtime_ns=1))
    assert res['status'] == 'unchanged'
    stored = res['updated_cfg']['links_mtime_ns']
    assert stored == str(_mtime_ns(folder))
    res, reads = _sync(monkeypatch, _gated(folder, links_mtime_ns=stored))
    assert res['status'] == 'skipped'
    assert reads == []


@pytest.mark.parametrize('content', [b'', EXPORT[:len(EXPORT) // 2]])
def test_unusable_file_just_written_is_retried_next_cycle(folder, monkeypatch, content):
    """Modified a moment ago: maybe a stalled writer. Nothing recorded, no
    message, and the next cycle forces a re-read, which records the file
    once it is complete."""
    (folder / 'epg.xml').write_bytes(content)
    _age(folder, 5)
    res, reads = _sync(monkeypatch, _gated(folder, links_mtime_ns=1))
    assert res['status'] == 'unchanged' and reads == [str(folder / 'epg.xml')]
    assert res['updated_cfg']['links_mtime_ns'] == 1, 'the stored value is left as it was'
    assert res['updated_cfg'].get('last_error') is None
    # the writer finishes (same mtime: one write() sets it when it starts)
    mtime = _mtime_ns(folder)
    (folder / 'epg.xml').write_bytes(EXPORT)
    os.utime(str(folder / 'epg.xml'), ns=(mtime, mtime))
    res, reads = _sync(monkeypatch, _gated(folder, links_mtime_ns=res['updated_cfg']['links_mtime_ns']))
    assert res['status'] == 'unchanged' and reads == [str(folder / 'epg.xml')], 're-read'
    assert res['updated_cfg']['links_mtime_ns'] == str(mtime)
    assert res['updated_cfg'].get('last_error') is None


def test_missing_file_doesnt_force(folder, monkeypatch):
    (folder / 'epg.xml').unlink()
    res, reads = _sync(monkeypatch, _gated(folder, links_mtime_ns=1))
    assert res['status'] == 'skipped'
    assert reads == []


def test_failing_import_is_left_to_the_backoff(folder, monkeypatch):
    cfg = _gated(folder, last_status='failed', fail_count=3, failed_since=int(time.time()) - 3600,
                 last_sync_at=int(time.time()) - 30, links_mtime_ns=1)
    res, reads = _sync(monkeypatch, cfg)
    assert res['status'] == 'skipped', 'retry gate (900 s at fail_count 3) still rules'
    assert reads == []


def test_disabled_import_is_not_checked(folder, monkeypatch):
    calls = []
    monkeypatch.setattr(Imports, '_linksChanged', lambda self, cfg, chans: calls.append(cfg) or True)
    res, _reads = _sync(monkeypatch, _gated(folder, enabled=False))
    assert calls == []
    assert res['status'] == 'disabled'


def test_force_scope_already_forcing_skips_the_check(folder, monkeypatch):
    calls = []
    monkeypatch.setattr(Imports, '_linksChanged', lambda self, cfg, chans: calls.append(cfg) or False)
    real_fetch = Imports.fetchSource
    def stub(self, url, etag=None, last_modified=None, **kw):
        if url == EPG_URL:
            return {'status': 200, 'content': RICH, 'etag': None, 'last_modified': None, 'error': None}
        if url.endswith('channels.m3u8'):
            return {'status': 304, 'content': None, 'etag': None, 'last_modified': None, 'error': None}
        return real_fetch(self, url)
    monkeypatch.setattr(Imports, 'fetchSource', stub)
    Imports(channels=_Ch([_gated(folder)], [_rec()]), m3u=_M3U(), xmltv=_X()).syncAll(force_scope='all')
    assert calls == []


# ---------------------------------------------------------------- _linksChanged itself


def test_links_changed_unit(folder, monkeypatch):
    imp = Imports()
    recs = [_rec()]
    cfg = _gated(folder)
    assert imp._linksChanged(cfg, recs) is False
    assert imp._linksChanged(dict(cfg, links_mtime_ns=str(cfg['links_mtime_ns'])), recs) is False
    assert imp._linksChanged(dict(cfg, links_mtime_ns=cfg['links_mtime_ns'] + 1), recs) is True
    assert imp._linksChanged(dict(cfg, links_mtime_ns=str(cfg['links_mtime_ns'] + 1)), recs) is True
    assert imp._linksChanged(dict(cfg, links_mtime_ns=None), recs) is True
    without = dict(cfg)
    del without['links_mtime_ns']
    assert imp._linksChanged(without, recs) is True
    # not a known exporter's playlist
    assert imp._linksChanged(dict(cfg, m3u_path='', m3u_url='https://example.invalid/list.m3u',
                                  links_mtime_ns=1), recs) is False
    # records of another import don't count
    other = dict(_rec(), import_source='other')
    assert imp._linksChanged(dict(cfg, links_mtime_ns=1), [other]) is False
    # no id
    assert imp._linksChanged(dict(cfg, id='', links_mtime_ns=1), recs) is False


def test_links_changed_never_uses_xbmcvfs_stat(folder, monkeypatch):
    def boom(*a, **k):
        raise AssertionError('xbmcvfs.Stat must not be used')
    monkeypatch.setattr(xbmcvfs, 'Stat', boom)
    monkeypatch.setattr(imports_mod.xbmcvfs, 'Stat', boom)
    assert Imports()._linksChanged(_gated(folder, links_mtime_ns=1), [_rec()]) is True


def test_links_changed_error_gives_false(folder, monkeypatch):
    def boom(cfg):
        raise RuntimeError('simulated')
    monkeypatch.setattr(replay_helpers, 'exporter_for', boom)
    assert Imports()._linksChanged(_gated(folder, links_mtime_ns=1), [_rec()]) is False
