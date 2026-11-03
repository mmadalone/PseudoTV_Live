"""imports.59 (S12) — dashboard messages when Movistar+ replay is at risk.

Movistar+'s guide export is now the only source of replay links, so a
logged-out add-on, an update or missing refreshes must not fail silently.
The links read (Imports._readLinks) and the record check
(_declaredReplayWarning) set one message as the import's last_error -- only
while last_error is otherwise empty -- and never touch last_status,
fail_count or epg_fail_count (a 'warning' status would drop the import from
allocation). Only imports that use link replay ever get one; a read during
which the file changed gets none (it is retried), and neither does a stable
read that can't be parsed of a file modified less than 2 minutes before (the
add-on may still be writing it; retried too).
"""
import os
import time

import pytest

import imports as imports_mod
import replay_helpers
from imports import Imports


IID     = 'mp'
EPG_URL = 'https://example.invalid/rich.xml'
NOW     = int(time.time())
TAIL    = ("Restart Kodi or press Export channels and EPG now in Movistar+ settings, "
           "and check Movistar+ is still logged in.")
UNREADABLE = ("Movistar+ guide file can't be read (%s). Replay keeps using the links already saved. "
              "Restart Kodi or press Export channels and EPG now in Movistar+ settings, "
              "and check Movistar+ is still logged in.")


def _age(folder, seconds):
    t = time.time() - seconds
    os.utime(str(folder / 'epg.xml'), (t, t))


def _ts(epoch):
    return time.strftime('%Y%m%d%H%M%S', time.gmtime(epoch)) + ' +0000'


def _export(slots=((NOW - 1800, NOW + 1800), (NOW + 1800, NOW + 5400)), link=True):
    progs = ''.join('<programme start="%s" stop="%s" channel="C1"%s><title>Show</title></programme>'
                    % (_ts(s), _ts(e), (' catchup-id="plugin://x/?id=%d"' % s) if link else '')
                    for s, e in slots)
    return ('<?xml version="1.0" encoding="UTF-8"?><tv><channel id="C1"><display-name>Chan One</display-name>'
            '</channel>' + progs + '</tv>').encode()


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
    def __init__(self): self.added = []
    def addStation(self, s): self.added.append(s)
    def delStation(self, s): pass


class _X:
    def __init__(self):
        self.XMLTVDATA = {'channels': [], 'programmes': [], 'recordings': []}


def _rec(catchup='default', source='{catchup-id}', days='7'):
    return {'id': 'C1@mp', 'epg_id': 'C1', 'name': 'Chan One', 'source_name': 'Chan One',
            'import_source': IID, 'type': 'import', 'enabled': True, 'number': 1, 'assigned_number': 1,
            'is_orphan': False, 'unallocated': False, 'source_url': 'plugin://x/?live=C1',
            'url': 'plugin://x/?live=C1', 'group': ['MP'], 'logo': '', 'catchup': catchup,
            'catchup-source': source, 'catchup-days': days, 'operator_overrides': []}


@pytest.fixture
def folder(tmp_path, monkeypatch):
    monkeypatch.setattr(imports_mod, 'CACHE_LOC', str(tmp_path))
    monkeypatch.setattr(imports_mod, '_isDiskMissing', lambda iid, scope: False)
    imports_mod._GUIDE_SELFHEAL_LAST.clear()
    folder = tmp_path / 'addon_data' / 'plugin.video.movistarplus'
    folder.mkdir(parents=True)
    (folder / 'channels.m3u8').write_text('#EXTM3U\n')
    (folder / 'epg.xml').write_bytes(_export())
    return folder


def _cfg(folder, **over):
    cfg = {'id': IID, 'name': 'MP', 'enabled': True, 'm3u_path': str(folder / 'channels.m3u8'),
           'epg_url': EPG_URL, 'start_num': 1, 'tombstones': [], 'playlist_rev': 1,
           'refresh_interval_min': 600, 'last_sync_at': int(time.time()) - 11 * 3600}
    cfg.update(over)
    return cfg


def _sync(monkeypatch, cfg, records=None, guide=None):
    real_fetch = Imports.fetchSource
    def stub(self, url, etag=None, last_modified=None, **kw):
        base = {'status': 0, 'content': None, 'etag': None, 'last_modified': None, 'error': None}
        if url.endswith('channels.m3u8') or url.endswith('list.m3u'):
            base['status'] = 304
            return base
        if url == EPG_URL:
            if guide is None:
                base.update(status=200, content=RICH)
            else:
                base.update(guide)
            return base
        return real_fetch(self, url, etag=etag, last_modified=last_modified)
    monkeypatch.setattr(Imports, 'fetchSource', stub)
    res = Imports(channels=_Ch([cfg], records or [_rec()]), m3u=_M3U(), xmltv=_X()).syncAll()
    return res[IID]['updated_cfg']


def _assert_counters_untouched(cfg):
    assert cfg['last_status'] == 'unchanged'
    assert cfg['fail_count'] == 0
    assert not cfg.get('epg_fail_count')


def test_fresh_healthy_file_gives_no_message(folder, monkeypatch):
    cfg = _sync(monkeypatch, _cfg(folder))
    assert cfg['last_error'] is None
    _assert_counters_untouched(cfg)


def test_missing_file(folder, monkeypatch):
    (folder / 'epg.xml').unlink()
    cfg = _sync(monkeypatch, _cfg(folder))
    assert cfg['last_error'] == UNREADABLE % 'missing'
    _assert_counters_untouched(cfg)


def test_empty_file(folder, monkeypatch):
    (folder / 'epg.xml').write_bytes(b'')
    _age(folder, 600)
    cfg = _sync(monkeypatch, _cfg(folder))
    assert cfg['last_error'] == UNREADABLE % 'empty file'
    assert cfg['links_mtime_ns'] == str(os.stat(str(folder / 'epg.xml')).st_mtime_ns)
    _assert_counters_untouched(cfg)


def test_damaged_file(folder, monkeypatch):
    data = _export()
    (folder / 'epg.xml').write_bytes(data[:len(data) - 20])
    _age(folder, 600)
    cfg = _sync(monkeypatch, _cfg(folder))
    assert cfg['last_error'] == UNREADABLE % 'damaged'
    assert cfg['links_mtime_ns'] == str(os.stat(str(folder / 'epg.xml')).st_mtime_ns)
    _assert_counters_untouched(cfg)


def test_unreadable_file(folder, monkeypatch):
    (folder / 'epg.xml').unlink()
    (folder / 'epg.xml').mkdir()          # stat works, open() fails
    cfg = _sync(monkeypatch, _cfg(folder))
    assert cfg['last_error'] == UNREADABLE % 'unreadable'
    assert cfg['links_mtime_ns'] == str(os.stat(str(folder / 'epg.xml')).st_mtime_ns)
    _assert_counters_untouched(cfg)


def _logged(monkeypatch):
    lines = []
    monkeypatch.setattr(Imports, 'log', lambda self, msg, level=None: lines.append((msg, level)))
    return lines


@pytest.mark.parametrize('kind', ['empty file', 'damaged'])
def test_broken_file_just_written_gives_no_message_and_is_retried(folder, monkeypatch, kind):
    """Movistar+ writes its export in place with one write(); Linux sets the
    mtime when it starts. A stable but cut-off read of a file modified under
    2 minutes ago may be a stalled writer: nothing recorded, no message."""
    data = _export()
    (folder / 'epg.xml').write_bytes(b'' if kind == 'empty file' else data[:len(data) - 20])
    _age(folder, 30)
    lines = _logged(monkeypatch)
    cfg = _sync(monkeypatch, _cfg(folder))
    assert cfg['last_error'] is None
    assert 'links_mtime_ns' not in cfg, 'not recorded: the next cycle reads it again'
    _assert_counters_untouched(cfg)
    info = [m for m, lvl in lines if 'may still be being written; will retry' in m]
    assert len(info) == 1 and ('replay links file %s' % kind) in info[0]
    assert [lvl for m, lvl in lines if m == info[0]] == [imports_mod.xbmc.LOGINFO]


def test_broken_file_with_a_future_mtime_is_recorded(folder, monkeypatch):
    """A file 'modified' after the read (a clock change) proves nothing:
    recorded and shown as before, so it can't force a sync every cycle."""
    (folder / 'epg.xml').write_bytes(b'')
    _age(folder, -3600)
    cfg = _sync(monkeypatch, _cfg(folder))
    assert cfg['last_error'] == UNREADABLE % 'empty file'
    assert cfg['links_mtime_ns'] == str(os.stat(str(folder / 'epg.xml')).st_mtime_ns)


def test_settle_time_is_two_minutes():
    assert imports_mod.LINKS_SETTLE_SECS == 120


def test_no_links(folder, monkeypatch):
    (folder / 'epg.xml').write_bytes(_export(link=False))
    cfg = _sync(monkeypatch, _cfg(folder))
    assert cfg['last_error'] == 'Movistar+ guide file has no replay links, so replay is off.'
    _assert_counters_untouched(cfg)


def test_all_links_ended(folder, monkeypatch):
    (folder / 'epg.xml').write_bytes(_export(slots=((NOW - 7200, NOW - 3600), (NOW - 3600, NOW - 60))))
    cfg = _sync(monkeypatch, _cfg(folder))
    assert cfg['last_error'] == 'Movistar+ guide file is out of date, so replay is paused. ' + TAIL
    _assert_counters_untouched(cfg)


@pytest.mark.parametrize('days,expected', [(3, None), (4, 4), (5, 5)])
def test_file_age(folder, monkeypatch, days, expected):
    old = time.time() - days * 86400 - 600
    os.utime(str(folder / 'epg.xml'), (old, old))
    cfg = _sync(monkeypatch, _cfg(folder))
    if expected is None:
        assert cfg['last_error'] is None
    else:
        assert cfg['last_error'] == 'Movistar+ guide file is %d days old. %s' % (expected, TAIL)
    _assert_counters_untouched(cfg)


def test_declared_differently(folder, monkeypatch):
    """Link-based, but not the verified ('default', '{catchup-id}') form."""
    cfg = _sync(monkeypatch, _cfg(folder), records=[_rec(catchup='vod', source='', days='')])
    assert cfg['last_error'] == ('Movistar+ changed how it declares replay; replay may misbehave '
                                 'until this add-on is updated.')
    _assert_counters_untouched(cfg)


def test_file_message_comes_before_the_declared_check(folder, monkeypatch):
    (folder / 'epg.xml').unlink()
    cfg = _sync(monkeypatch, _cfg(folder), records=[_rec(catchup='vod', source='', days='')])
    assert "can't be read (missing)" in cfg['last_error']


def test_message_uses_the_exporters_name(folder, monkeypatch):
    monkeypatch.setitem(replay_helpers.KNOWN_EXPORTERS['plugin.video.movistarplus'], 'name', 'Some Exporter')
    (folder / 'epg.xml').write_bytes(_export(link=False))
    cfg = _sync(monkeypatch, _cfg(folder))
    assert cfg['last_error'] == 'Some Exporter guide file has no replay links, so replay is off.'


def test_existing_guide_error_takes_precedence(folder, monkeypatch, tmp_path):
    (tmp_path / ('epg_%s.xml' % IID)).write_bytes(RICH)          # cached guide to fall back on
    (folder / 'epg.xml').unlink()
    cfg = _sync(monkeypatch, _cfg(folder), guide={'status': 0, 'error': 'network down'})
    assert cfg['last_error'].startswith('Guide download failed: network down')
    assert cfg['last_status'] == 'unchanged' and cfg['fail_count'] == 0
    assert cfg['epg_fail_count'] == 1, 'the guide failure counts as before; the links message adds nothing'


def test_every_file_message_tells_what_to_do():
    """All the file messages end with the same action hint, including the
    login check: a logged-out Movistar+ ignores its own Export button."""
    hint = ('Restart Kodi or press Export channels and EPG now in %(name)s settings, '
            'and check %(name)s is still logged in.')
    for msg in (imports_mod.LINKS_MSG_UNREADABLE, imports_mod.LINKS_MSG_OUTDATED, imports_mod.LINKS_MSG_OLD):
        assert msg.endswith(hint)


def test_changed_during_read_gives_no_message(folder, monkeypatch):
    real = Imports._localFileState
    calls = {'n': 0}
    def flaky(path):
        st = real(path)
        calls['n'] += 1
        return st if calls['n'] % 2 else (st[0] + 1, st[1])
    monkeypatch.setattr(Imports, '_localFileState', staticmethod(flaky))
    (folder / 'epg.xml').write_bytes(b'')       # would be "empty file" if it were stable
    cfg = _sync(monkeypatch, _cfg(folder))
    assert cfg['last_error'] is None
    assert 'links_mtime_ns' not in cfg


def test_old_catchup_row_restored_gives_no_message(folder, monkeypatch):
    """Rollback stage 1: the old row blanks the source, so no channel is
    link-based -- no read, no message, even for a missing file."""
    (folder / 'epg.xml').unlink()
    cfg = _sync(monkeypatch, _cfg(folder), records=[_rec(source='', days='5')])
    assert cfg['last_error'] is None
    assert 'links_mtime_ns' not in cfg


def test_non_exporter_import_never_gets_a_message(folder, monkeypatch):
    """Link-based records on an import whose playlist is not a known
    exporter's file (e.g. a remote list): no links read, no message."""
    cfg = _sync(monkeypatch, _cfg(folder, m3u_path='', m3u_url='https://example.invalid/list.m3u'))
    assert cfg['last_error'] is None
    assert 'links_mtime_ns' not in cfg
