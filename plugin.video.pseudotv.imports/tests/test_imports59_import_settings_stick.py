"""imports.59 (S5) — import settings stay saved: a long-lived Channels
instance never writes back an old import list.

Builder.channels is one Channels(writable=True) per Kodi session and every
Custom build saves through it; the Channel Manager window holds another.
Channels._save writes the whole channelDATA -- including the import list
loaded when Kodi started -- so a Custom build reverted the tombstones,
validators, last_sync_at and the replay-link bookkeeping (links_mtime_ns,
playlist_rev) saved since. Both setChannels merge branches already re-read
channels.json; they now adopt its import list before saving (only setImports
writes the import list). imports.69: every save is one locked commit on a
strict fresh read, so an unreadable channels.json is refused (nothing
written) instead of keeping the in-memory list as before.

These tests use the REAL Channels class against a channels.json in tmp_path.
"""
import json
import os
import time

import pytest

from channels import Channels


CUSTOM = 'a' * 32 + '@PseudoTV_Live'

IMPORTS_V1 = [{'id': 'mp', 'name': 'Movistar+', 'enabled': True, 'tombstones': [],
               'last_sync_at': 100, 'etag': 'v1'}]
IMPORTS_V2 = [{'id': 'mp', 'name': 'Movistar+', 'enabled': True, 'tombstones': ['GONE@mp'],
               'last_sync_at': 200, 'etag': 'v2', 'links_mtime_ns': 1793642891620000000, 'playlist_rev': 1}]


def _channel(cid, number, **over):
    c = {'id': cid, 'number': number, 'name': 'Chan %d' % number, 'path': ['/x'], 'enabled': True}
    c.update(over)
    return c


@pytest.fixture(autouse=True)
def _fixed_uuid(monkeypatch, tmp_path):
    """setChannels stamps SETTINGS.getMYUUID(). Under the Kodi stubs there is
    no device name, so getMYUUID would open the 'name this device' OK dialog
    whose 300 s auto-close Timer (non-daemon) keeps pytest from exiting.

    imports.69: setImports (and the sync's import-list save) now sweeps the
    guide caches of unlisted imports from channels.CACHE_LOC after the write.
    Unpatched, that is the working folder's tests/userdata/cache, so point it
    at this test's tmp_path (isolation only)."""
    import channels as channels_module
    monkeypatch.setattr(channels_module.SETTINGS, 'getMYUUID', lambda: 'test-uuid')
    monkeypatch.setattr(channels_module, 'CACHE_LOC', str(tmp_path))


@pytest.fixture
def path(tmp_path):
    p = tmp_path / 'channels.json'
    p.write_text(json.dumps({'uuid': 'u', 'imports': IMPORTS_V1,
                             'channels': [_channel(CUSTOM, 1),
                                          _channel('TVE@mp', 2, type='import', import_source='mp')]}))
    return p


def _disk(path):
    return json.loads(path.read_text())


def _dashboard_saves_v2(path):
    """A second instance (the dashboard / an import sync) saves new import
    settings after the long-lived instance was loaded."""
    other = Channels(file=str(path), writable=True)
    assert other.setImports([dict(i) for i in IMPORTS_V2]) is True
    assert _disk(path)['imports'] == IMPORTS_V2


def test_modified_ids_branch_keeps_disk_import_list(path):
    long_lived = Channels(file=str(path), writable=True)
    assert long_lived.getImports() == IMPORTS_V1
    _dashboard_saves_v2(path)
    custom = long_lived.getChannelbyID(CUSTOM)[0]
    custom['name'] = 'Renamed by a build'
    long_lived.setChannels(modified_ids={CUSTOM})
    disk = _disk(path)
    assert disk['imports'] == IMPORTS_V2, 'the build no longer writes back the start-up import list'
    assert long_lived.getImports() == IMPORTS_V2
    assert [c['name'] for c in disk['channels'] if c['id'] == CUSTOM] == ['Renamed by a build']
    assert {c['id'] for c in disk['channels']} == {CUSTOM, 'TVE@mp'}


def test_no_modified_ids_branch_keeps_disk_import_list(path):
    """The Channel Manager path: setChannels(channels) with only the Custom subset."""
    long_lived = Channels(file=str(path), writable=True)
    _dashboard_saves_v2(path)
    long_lived.setChannels([_channel(CUSTOM, 1, name='Edited in the manager')])
    disk = _disk(path)
    assert disk['imports'] == IMPORTS_V2
    assert {c['id'] for c in disk['channels']} == {CUSTOM, 'TVE@mp'}, 'disk-only import channel preserved'
    assert [c['name'] for c in disk['channels'] if c['id'] == CUSTOM] == ['Edited in the manager']


def test_unreadable_file_refuses_the_save(path):
    """imports.69 (was: a failed re-read wrote the in-memory list). An
    unreadable channels.json is never overwritten with this copy's data."""
    long_lived = Channels(file=str(path), writable=True)
    _dashboard_saves_v2(path)
    path.write_text('{"uuid": "u", "imports": [')                # damaged (half-written / hand edit)
    before = path.read_bytes()
    assert long_lived.setChannels(modified_ids={CUSTOM}) is False
    assert path.read_bytes() == before, 'refused: the file is left byte-identical'


@pytest.mark.parametrize('bad, imports_written', [
    ({'imports': None}, None),                    # not a list: unreadable, refused
    ({'imports': 'junk'}, None),
    ([], None),                                   # top level not an object: refused
    ({'channels': []}, []),                       # no import list: an empty one (the file's, never memory's)
])
def test_strict_read_of_the_import_list(path, bad, imports_written):
    """imports.69 (was: _adoptDiskImports kept the in-memory list for each of
    these). The strict read refuses a file whose import list isn't a list; a
    file without one has none."""
    long_lived = Channels(file=str(path), writable=True)
    path.write_text(json.dumps(bad))
    before = path.read_bytes()
    saved = long_lived.setChannels(modified_ids={CUSTOM})
    if imports_written is None:
        assert saved is False and path.read_bytes() == before
    else:
        assert saved is True
        assert _disk(path)['imports'] == imports_written, 'the start-up list (IMPORTS_V1) is not written back'


def test_setimports_still_writes_the_import_list(path):
    ch = Channels(file=str(path), writable=True)
    ch.setImports([dict(i) for i in IMPORTS_V2])
    ch.setChannels(modified_ids=set())
    assert _disk(path)['imports'] == IMPORTS_V2


# ---------------------------------------------------------------- through Imports.syncAll (R2C-6, S16)
#
# A Movistar+-style import (playlist + guide export in an addon_data folder),
# synced by the real Imports.syncAll against the real Channels class.

EPG_URL = 'https://example.invalid/rich.xml'
_T0 = int(time.time())


def _ts(epoch):
    return time.strftime('%Y%m%d%H%M%S', time.gmtime(epoch)) + ' +0000'


PLAYLIST = ('#EXTM3U\n#EXTINF:-1 tvg-id="TVE" tvg-name="La Uno" tvg-chno="2" group-title="Movistar+" '
            'catchup="default" catchup-source="{catchup-id}" catchup-days="7",La Uno\nplugin://x/?id=TVE\n')
EXPORT = ('<?xml version="1.0" encoding="UTF-8"?><tv><channel id="TVE"><display-name>La Uno</display-name></channel>'
          '<programme start="%s" stop="%s" channel="TVE" catchup-id="plugin://x/?id=1"><title>Show</title></programme>'
          '</tv>' % (_ts(_T0 - 1800), _ts(_T0 + 1800))).encode()
RICH = ('<?xml version="1.0" encoding="UTF-8"?><tv><channel id="La Uno HD"><display-name>La Uno</display-name></channel>'
        '<programme start="%s" stop="%s" channel="La Uno HD"><title>Show</title><desc>Rich</desc></programme>'
        '</tv>' % (_ts(_T0 - 1800), _ts(_T0 + 1800))).encode()


class _M3U:
    def addStation(self, s): pass
    def delStation(self, s): pass


class _X:
    def __init__(self):
        self.XMLTVDATA = {'channels': [], 'programmes': [], 'recordings': []}


@pytest.fixture
def sync_path(tmp_path, monkeypatch):
    import imports as imports_mod
    monkeypatch.setattr(imports_mod, 'CACHE_LOC', str(tmp_path))
    monkeypatch.setattr(imports_mod, '_isDiskMissing', lambda iid, scope: False)
    imports_mod._GUIDE_SELFHEAL_LAST.clear()
    folder = tmp_path / 'addon_data' / 'plugin.video.movistarplus'
    folder.mkdir(parents=True)
    (folder / 'channels.m3u8').write_text(PLAYLIST)
    (folder / 'epg.xml').write_bytes(EXPORT)
    from imports import Imports
    real_fetch = Imports.fetchSource
    def fetch(self, url, etag=None, last_modified=None, **kw):
        if url == EPG_URL:
            return {'status': 200, 'content': RICH, 'etag': None, 'last_modified': None, 'error': None}
        return real_fetch(self, url, etag=etag, last_modified=last_modified)
    monkeypatch.setattr(Imports, 'fetchSource', fetch)
    cfg = {'id': 'mp', 'name': 'Movistar+', 'enabled': True, 'm3u_path': str(folder / 'channels.m3u8'),
           'epg_url': EPG_URL, 'start_num': 1, 'tombstones': [], 'respect_source_numbers': True,
           'last_sync_at': 100}
    record = {'id': 'TVE@mp', 'epg_id': 'TVE', 'name': 'La Uno', 'import_source': 'mp', 'type': 'import',
              'enabled': True, 'number': 2, 'assigned_number': 2, 'is_orphan': False, 'unallocated': False,
              'source_url': 'plugin://x/?id=TVE', 'url': 'plugin://x/?id=TVE', 'group': ['Movistar+'],
              'logo': '', 'catchup': 'default', 'catchup-source': '', 'catchup-days': '5',
              'operator_overrides': []}
    p = tmp_path / 'channels.json'
    p.write_text(json.dumps({'uuid': 'u', 'imports': [cfg], 'channels': [_channel(CUSTOM, 1), record]}))
    return p, folder


def _sync_with(channels):
    from imports import Imports
    return Imports(channels=channels, m3u=_M3U(), xmltv=_X()).syncAll()


def test_sync_bookkeeping_survives_a_custom_build_of_a_long_lived_instance(sync_path):
    """R2C-6 (S5 through syncAll): Builder's session instance was loaded before
    the import sync; the sync (a fresh instance, as chkImports uses) saves its
    bookkeeping; the next Custom build saves through the old instance."""
    path, folder = sync_path
    long_lived = Channels(file=str(path), writable=True)
    res = _sync_with(Channels(file=str(path), writable=True))
    assert res['mp']['status'] == 'ok'
    saved = _disk(path)['imports'][0]
    assert saved['links_mtime_ns'] == str(os.stat(str(folder / 'epg.xml')).st_mtime_ns)
    assert saved['playlist_rev'] == 1 and saved['last_sync_at'] > 100
    long_lived.getChannelbyID(CUSTOM)[0]['name'] = 'Rebuilt'
    long_lived.setChannels(modified_ids={CUSTOM})
    disk = _disk(path)
    assert disk['imports'][0] == saved, 'the Custom build kept the sync bookkeeping'
    tve = [c for c in disk['channels'] if c['id'] == 'TVE@mp'][0]
    assert (tve['catchup'], tve['catchup-source'], tve['catchup-days']) == ('default', '{catchup-id}', '7')


def test_fresh_instance_sync_keeps_a_dashboard_channel_edit(sync_path):
    """S16: a dashboard edit to an imported channel, saved after Builder's
    session instance was loaded, survives an import sync that starts from a
    fresh Channels read (Builder now constructs one for its sync)."""
    path, _folder = sync_path
    long_lived = Channels(file=str(path), writable=True)
    dashboard = Channels(file=str(path), writable=True)
    rec = dashboard.getChannelbyID('TVE@mp')[0]
    rec['number'] = 17
    rec['operator_overrides'] = ['number']
    dashboard.setChannels(modified_ids={'TVE@mp'})
    res = _sync_with(Channels(file=str(path), writable=True))
    assert res['mp']['status'] == 'ok'
    tve = [c for c in _disk(path)['channels'] if c['id'] == 'TVE@mp'][0]
    assert tve['number'] == 17 and tve['operator_overrides'] == ['number']
    # Why the fresh read matters: the session instance still holds the
    # channel as loaded, and syncAll writes its import records back with
    # memory winning -- handed that instance, the edit is reverted.
    _sync_with(long_lived)
    tve = [c for c in _disk(path)['channels'] if c['id'] == 'TVE@mp'][0]
    assert tve['number'] == 2 and tve['operator_overrides'] == []
