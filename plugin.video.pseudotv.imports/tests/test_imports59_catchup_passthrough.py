"""imports.59 (S7) — imported channels keep the catch-up their playlist declares.

Movistar+ 0.11.9 declares catchup="default" catchup-source="{catchup-id}"
catchup-days="7" on every playlist line: in IPTV Simple's default mode a
non-empty catchup-source is the full replay address (here each show's own
link). The KNOWN_CATCHUP_DEFAULTS row for 'plugin://plugin.video.movistarplus/'
forced ('default', '&start_time={utc}&end_time={utcend}', 5) and blanked the
source, so every past show played live. The row is gone; the table,
_resolveCatchup, the per-import catchup_* settings and the operator's
'catchup' override all still work.
"""
import os
import re
import time

import pytest

import imports as imports_mod
from imports import Imports


HERE     = os.path.dirname(os.path.abspath(__file__))
FIXTURE  = os.path.join(HERE, 'fixtures', 'sample_movistarplus_0119.m3u8')
LIB      = os.path.join(os.path.dirname(HERE), 'resources', 'lib')
IID      = 'mp'
LINKED   = ('default', '{catchup-id}', '7')


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(imports_mod, 'CACHE_LOC', str(tmp_path))
    monkeypatch.setattr(imports_mod, '_isDiskMissing', lambda iid, scope: False)
    imports_mod._GUIDE_SELFHEAL_LAST.clear()


class _MockChannels:
    def __init__(self, imports_list, channels_list):
        self._imports  = list(imports_list)
        self._channels = list(channels_list)
    def getImports(self):  return list(self._imports)
    def getChannels(self): return list(self._channels)
    def setChannels(self, channels=None, modified_ids=None, deleted_ids=None):
        if channels is not None: self._channels = list(channels)
        return True
    def setImports(self, data):
        self._imports = list(data)
        return True


class _MockM3U:
    def __init__(self): self.added = []
    def addStation(self, sitem): self.added.append(sitem)
    def delStation(self, sitem): pass


def _cfg(**over):
    cfg = {'id': IID, 'name': 'Movistar+', 'enabled': True, 'm3u_path': FIXTURE,
           'respect_source_numbers': True, 'start_num': 1, 'tombstones': []}
    cfg.update(over)
    return cfg


def _sync(cfg, existing=()):
    channels = _MockChannels([cfg], list(existing))
    m3u = _MockM3U()
    Imports(channels=channels, m3u=m3u, xmltv=None).syncAll()
    records = {c['id']: c for c in channels.getChannels() if c.get('type') == 'import'}
    stations = {s['id']: s for s in m3u.added}
    return records, stations


def _catchup(rec):
    return (rec.get('catchup'), rec.get('catchup-source'), rec.get('catchup-days'))


def _old_record(tvg, number, **over):
    """A record as the Movistar+ catch-up row left it: ('default', '', '5')."""
    cid = '%s@%s' % (tvg, IID)
    url = 'plugin://plugin.video.movistarplus/?action=play&stype=tv&id=%s' % tvg
    rec = {'id': cid, 'epg_id': tvg, 'name': 'Old %s' % tvg, 'import_source': IID, 'type': 'import',
           'enabled': True, 'number': number, 'assigned_number': number, 'is_orphan': False,
           'unallocated': False, 'source_url': url, 'url': url, 'group': ['Movistar+'], 'logo': '',
           'catchup': 'default', 'catchup-source': '', 'catchup-days': '5', 'operator_overrides': []}
    rec.update(over)
    return rec


def test_new_channels_keep_the_playlist_catchup():
    records, stations = _sync(_cfg())
    assert sorted(records) == ['CH1@mp', 'CH2@mp', 'CH3@mp']
    for rec in records.values():
        assert _catchup(rec) == LINKED
    for st in stations.values():
        assert (st['catchup'], st['catchup-source'], st['catchup-days']) == LINKED


def test_refreshed_channels_get_the_playlist_catchup_back():
    """Records saved under the old row hold ('default', '', '5'); the full
    read (S8) refreshes them to the playlist's own values."""
    existing = [_old_record('CH1', 1), _old_record('CH2', 2), _old_record('CH3', 3)]
    records, _stations = _sync(_cfg(), existing)
    for rec in records.values():
        assert _catchup(rec) == LINKED


def test_operator_catchup_override_still_wins():
    existing = [_old_record('CH1', 1, catchup='vod', operator_overrides=['catchup']),
                _old_record('CH2', 2), _old_record('CH3', 3)]
    records, _stations = _sync(_cfg(), existing)
    assert records['CH1@mp']['catchup'] == 'vod'
    assert records['CH2@mp']['catchup'] == 'default'


@pytest.mark.parametrize('existing', [(), 'refreshed'])
def test_per_import_catchup_config_still_wins(existing):
    cfg = _cfg(catchup_mode='default', catchup_query_format='&start={utc}&end={utcend}', catchup_days=3)
    records_in = [_old_record('CH1', 1), _old_record('CH2', 2), _old_record('CH3', 3)] if existing else []
    records, _stations = _sync(cfg, records_in)
    for rec in records.values():
        # configured default mode: source cleared so IPTV Simple appends the
        # instance query format, days from the config
        assert _catchup(rec) == ('default', '', '3')


def test_known_catchup_defaults_has_no_movistarplus_row():
    with open(os.path.join(LIB, 'imports.py'), encoding='utf-8') as f:
        src = f.read()
    start = src.find('KNOWN_CATCHUP_DEFAULTS = {')
    end = src.find('def _resolveCatchup(', start)
    table = src[start:end]
    assert start != -1 and end != -1
    assert not re.search(r"^\s*['\"]plugin://plugin\.video\.movistarplus/['\"]\s*:", table, re.M), \
        'the Movistar+ catch-up row is back'
    assert 'def _resolveCatchup(' in src, '_resolveCatchup stays'


def test_last_sync_records_playlist_rev():
    channels = _MockChannels([_cfg()], [])
    Imports(channels=channels, m3u=_MockM3U(), xmltv=None).syncAll()
    cfg = channels.getImports()[0]
    assert cfg['playlist_rev'] == imports_mod.PLAYLIST_REREAD_REV
    assert cfg['last_status'] == 'ok'
    assert isinstance(cfg['last_sync_at'], int) and abs(cfg['last_sync_at'] - time.time()) < 60
