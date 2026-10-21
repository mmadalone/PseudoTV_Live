"""imports.58 — a logo set for an imported channel reaches Kodi.

Live case: the operator set TV3 / A Punt logos to web addresses in the
dashboard; Kodi kept the old pictures, across restarts. Four causes:
  1. The addon configures iptvsimple to prefer the guide's <icon>
     (logoFromEpg=2), and syncAll only copied the channel's logo into the
     guide when it was a local file — dead for cached logos since imports.44
     made them http:// links, and never true for a typed web address.
  2. The logo cache reused any file named after the channel (mode 0) and
     never compared the address it came from with the channel's current
     one; mode 1 sent the old address's validators to the new one.
  3. A changed picture kept its file name, so its link never changed —
     and Kodi never re-checks a web image it has already cached.
  4. The cache step overwrote ch['logo'] on the channel records themselves,
     so the addon's own /images/ link could be saved into channels.json.

Operator decisions (2026-10-20): a logo set in the dashboard beats the
guide source's; channels whose logo was not set keep the guide's icon.

Addresses below are from the RFC 5737 documentation ranges.
"""
import hashlib
import os
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
LIB  = os.path.join(os.path.dirname(HERE), 'resources', 'lib')
if LIB not in sys.path:
    sys.path.insert(0, LIB)

import imports as imports_mod
from imports import Imports, logo_file_channel_id

HOST = '192.0.2.10:50002'
STALE_HOST = '198.51.100.7:50002'


def _md5(content):
    return hashlib.md5(content).hexdigest()[:8]


def _logo_bytes(url):
    """Distinct picture bytes per address, so versions differ."""
    return ('PICTURE:%s' % url).encode('utf-8')


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(imports_mod, 'CACHE_LOC', str(tmp_path))
    monkeypatch.setattr(imports_mod, '_isDiskMissing', lambda iid, scope: False)
    monkeypatch.setattr(imports_mod.PROPERTIES, 'getRemoteHost', lambda: HOST)
    real_getBool = imports_mod.SETTINGS.getSettingBool
    real_getInt  = imports_mod.SETTINGS.getSettingInt
    def fake_getBool(key, *a):
        if key == 'Imports_Cache_Logos': return True
        return real_getBool(key, *a)
    def fake_getInt(key, *a):
        if key == 'Imports_Logo_Refresh':          return 0
        if key == 'TCP_PORT':                      return 50002
        if key == 'Imports_Failure_Retry_Curve':   return 1
        if key == 'Imports_Failure_Abandon_Hours': return 0
        return real_getInt(key, *a)
    monkeypatch.setattr(imports_mod.SETTINGS, 'getSettingBool', fake_getBool)
    monkeypatch.setattr(imports_mod.SETTINGS, 'getSettingInt', fake_getInt)
    imports_mod._GUIDE_SELFHEAL_LAST.clear()
    yield
    imports_mod._GUIDE_SELFHEAL_LAST.clear()


def _logos(tmp_path):
    d = tmp_path / 'logos'
    d.mkdir(exist_ok=True)
    return d


def _fetch_by_url(calls=None):
    """_fetchLogo stub: every address serves its own picture."""
    def _fn(url, etag=None, last_modified=None, **kw):
        if calls is not None:
            calls.append({'url': url, 'etag': etag, 'last_modified': last_modified})
        return {'status': 200, 'content': _logo_bytes(url), 'etag': None,
                'last_modified': None, 'content_type': 'image/jpeg', 'error': None}
    return _fn


# ================================================================ file names

@pytest.mark.parametrize('name, cid', [
    ('TV3Cat.es@autonomiques.png',          'TV3Cat.es@autonomiques'),   # pre-imports.58
    ('TV3Cat.es@autonomiques~0123abcd.jpg', 'TV3Cat.es@autonomiques'),   # imports.58
    ('TVE@movistarplus~deadbeef.png',       'TVE@movistarplus'),
    ('TMNT.png',                            'TMNT'),                     # operator upload
])
def test_logo_file_channel_id(name, cid):
    assert logo_file_channel_id(name) == cid


def test_find_logo_file_matches_the_channel_id_exactly(tmp_path):
    """The old `<id>.` prefix test also matched a channel whose id merely
    starts that way."""
    d = _logos(tmp_path)
    (d / 'Foo@imp.x.png').write_bytes(b'other channel')
    imp = Imports()
    assert imp._findLogoFile('Foo@imp') is None
    (d / 'Foo@imp~0123abcd.png').write_bytes(b'mine')
    assert imp._findLogoFile('Foo@imp') == str(d / 'Foo@imp~0123abcd.png')


# ================================================================ _cacheLogo

def test_changed_address_is_downloaded_under_a_new_name(tmp_path, monkeypatch):
    d = _logos(tmp_path)
    old = d / 'c1@imp.png'
    old.write_bytes(b'OLD PICTURE')
    validators = {'c1@imp': {'url': 'https://old.example.org/a.png', 'etag': '"o"'}}
    imp = Imports()
    calls = []
    monkeypatch.setattr(imp, '_fetchLogo', _fetch_by_url(calls))
    new_url = 'https://new.example.org/b.jpg'
    result = imp._cacheLogo('c1@imp', new_url, 0, validators)
    assert [c['url'] for c in calls] == [new_url]
    assert result == str(d / ('c1@imp~%s.jpg' % _md5(_logo_bytes(new_url))))
    assert open(result, 'rb').read() == _logo_bytes(new_url)
    assert not old.exists()
    assert validators['c1@imp']['url'] == new_url
    assert validators['c1@imp']['file'] == os.path.basename(result)


def test_same_picture_at_a_new_address_keeps_its_name(tmp_path, monkeypatch):
    d = _logos(tmp_path)
    picture = b'SAME PICTURE'
    current = d / ('c1@imp~%s.jpg' % _md5(picture))
    current.write_bytes(picture)
    validators = {'c1@imp': {'url': 'https://old.example.org/a.jpg', 'file': current.name}}
    imp = Imports()
    monkeypatch.setattr(imp, '_fetchLogo', lambda url, **kw: {
        'status': 200, 'content': picture, 'etag': None, 'last_modified': None,
        'content_type': 'image/jpeg', 'error': None})
    def _no_write(*a, **k):
        raise AssertionError('an unchanged picture must not be rewritten')
    monkeypatch.setattr(imp, '_logoWriteAtomic', _no_write)
    result = imp._cacheLogo('c1@imp', 'https://new.example.org/a.jpg', 0, validators)
    assert result == str(current)
    assert validators['c1@imp']['url'] == 'https://new.example.org/a.jpg'


def test_conditional_get_never_sends_old_validators_to_a_new_address(tmp_path, monkeypatch):
    d = _logos(tmp_path)
    (d / 'c1@imp.png').write_bytes(b'OLD')
    validators = {'c1@imp': {'url': 'https://old.example.org/a.png',
                             'etag': '"o"', 'last_modified': 'Fri, 03 May 2024 09:08:11 GMT'}}
    imp = Imports()
    calls = []
    monkeypatch.setattr(imp, '_fetchLogo', _fetch_by_url(calls))
    imp._cacheLogo('c1@imp', 'https://new.example.org/a.png', 1, validators)
    assert calls == [{'url': 'https://new.example.org/a.png', 'etag': None, 'last_modified': None}]


def test_failed_download_after_an_address_change_publishes_the_new_address(tmp_path, monkeypatch):
    d = _logos(tmp_path)
    old = d / 'c1@imp.png'
    old.write_bytes(b'OLD')
    validators = {'c1@imp': {'url': 'https://old.example.org/a.png'}}
    imp = Imports()
    monkeypatch.setattr(imp, '_fetchLogo', lambda url, **kw: {
        'status': 403, 'content': None, 'etag': None, 'last_modified': None,
        'content_type': None, 'error': 'HTTP 403'})
    assert imp._cacheLogo('c1@imp', 'https://new.example.org/a.png', 0, validators) is None
    assert old.exists()                                   # left in place
    assert validators['c1@imp']['url'] == 'https://old.example.org/a.png'


def test_copy_without_a_recorded_address_is_downloaded_once(tmp_path, monkeypatch):
    """Pre-imports.58 records were written only when the server sent
    ETag/Last-Modified. Such a copy is downloaded once and then recorded —
    even when the server sends neither."""
    d = _logos(tmp_path)
    (d / 'c1@imp.jpg').write_bytes(b'UNKNOWN ORIGIN')
    validators = {}
    imp = Imports()
    calls = []
    monkeypatch.setattr(imp, '_fetchLogo', _fetch_by_url(calls))
    url = 'https://source.example.org/c1.jpg'
    first = imp._cacheLogo('c1@imp', url, 0, validators)
    assert len(calls) == 1
    assert validators['c1@imp'] == {'etag': None, 'last_modified': None,
                                    'url': url, 'file': os.path.basename(first)}
    assert not (d / 'c1@imp.jpg').exists()
    assert imp._cacheLogo('c1@imp', url, 0, validators) == first
    assert len(calls) == 1                                # mode 0: no second download


def test_copy_without_a_recorded_address_is_kept_when_offline(tmp_path, monkeypatch):
    d = _logos(tmp_path)
    legacy = d / 'c1@imp.jpg'
    legacy.write_bytes(b'UNKNOWN ORIGIN')
    imp = Imports()
    monkeypatch.setattr(imp, '_fetchLogo', lambda url, **kw: {
        'status': 0, 'content': None, 'etag': None, 'last_modified': None,
        'content_type': None, 'error': 'offline'})
    assert imp._cacheLogo('c1@imp', 'https://source.example.org/c1.jpg', 0, {}) == str(legacy)


def test_own_link_reuses_the_recorded_file(tmp_path, monkeypatch):
    d = _logos(tmp_path)
    current = d / 'c1@imp~0123abcd.jpg'
    current.write_bytes(b'X')
    imp = Imports()
    def _no_fetch(*a, **k):
        raise AssertionError('own links are never downloaded')
    monkeypatch.setattr(imp, '_fetchLogo', _no_fetch)
    own = 'http://%s/images/c1%%40imp.png' % STALE_HOST
    validators = {'c1@imp': {'url': 'https://source.example.org/c1.jpg', 'file': current.name}}
    assert imp._cacheLogo('c1@imp', own, 0, validators) == str(current)


# ================================================================ eviction

def test_eviction_knows_versioned_names(tmp_path):
    d = _logos(tmp_path)
    (d / 'keep@imp~0123abcd.png').write_bytes(b'A')
    (d / 'gone@imp~89abcdef.png').write_bytes(b'B')
    imp = Imports()
    assert imp._evictOrphanLogos({'keep@imp'}) == 1
    assert (d / 'keep@imp~0123abcd.png').exists()
    assert not (d / 'gone@imp~89abcdef.png').exists()


# ================================================================ _guideIcon

LINK = 'http://%s/images/c1%%40imp~0123abcd.jpg' % HOST


@pytest.mark.parametrize('record, published, expected', [
    ({'logo': 'https://mine.example.org/a.jpg', 'operator_overrides': ['logo']}, LINK, LINK),
    ({'logo': 'https://mine.example.org/a.jpg', 'operator_overrides': ['logo']}, None,
     'https://mine.example.org/a.jpg'),                     # not cached: as typed
    ({'logo': 'special://home/addons/pack/a.png', 'operator_overrides': ['logo']}, None,
     'special://home/addons/pack/a.png'),
    ({'logo': 'special://home/addons/pack/a.png'}, None, 'special://home/addons/pack/a.png'),
    ({'logo': 'https://source.example.org/a.png'}, LINK, None),   # untouched: guide's icon
    ({'logo': 'https://source.example.org/a.png'}, None, None),
    ({'logo': '', 'operator_overrides': ['logo']}, None, None),
    (None, None, None),
])
def test_guide_icon_rule(record, published, expected):
    assert Imports()._guideIcon(record, published) == expected


# ================================================================ syncAll end to end

IMPORT_ID = 'imp'
M3U_URL   = 'https://example.invalid/list.m3u'
EPG_URL   = 'https://example.invalid/guide.xml'

GUIDE = b'''<?xml version="1.0" encoding="UTF-8"?>
<tv>
<channel id="c1"><display-name>Chan 1</display-name><icon src="https://guide.example.org/c1.png"/></channel>
<channel id="c2"><display-name>Chan 2</display-name><icon src="https://guide.example.org/c2.png"/></channel>
<channel id="c3"><display-name>Chan 3</display-name><icon src="https://guide.example.org/c3.png"/></channel>
<programme start="20990101000000 +0000" stop="20990101010000 +0000" channel="c1"><title>One</title></programme>
<programme start="20990101000000 +0000" stop="20990101010000 +0000" channel="c2"><title>Two</title></programme>
<programme start="20990101000000 +0000" stop="20990101010000 +0000" channel="c3"><title>Three</title></programme>
</tv>'''

MINE = 'https://mine.example.org/tv3.jpg'


class _Store:
    """Holds the very dicts it is given, like Channels.channelDATA — so a
    write into a record after setChannels shows up here."""
    def __init__(self, imports_list, channels_list):
        self.imports  = list(imports_list)
        self.channels = list(channels_list)
    def getImports(self):  return list(self.imports)
    def getChannels(self): return list(self.channels)
    def setChannels(self, channels=None, modified_ids=None, deleted_ids=None):
        if channels is not None: self.channels = list(channels)
        return True
    def setImports(self, data):
        self.imports = list(data)
        return True


class _M3U:
    def __init__(self):
        self.stations = {}
    def addStation(self, s): self.stations[s.get('id')] = s
    def delStation(self, s): self.stations.pop(s.get('id'), None)


class _XMLTV:
    def __init__(self, channels=None):
        self.XMLTVDATA = {'channels': list(channels or []), 'programmes': [], 'recordings': []}


def _record(n, logo, overrides=()):
    cid = 'c%d@%s' % (n, IMPORT_ID)
    url = 'plugin://plugin.video.example/?id=c%d' % n
    return {'id': cid, 'epg_id': 'c%d' % n, 'name': 'Chan %d' % n,
            'import_source': IMPORT_ID, 'type': 'import', 'enabled': True,
            'number': n, 'assigned_number': n, 'is_orphan': False,
            'unallocated': False, 'source_url': url, 'url': url,
            'group': ['IMP'], 'logo': logo, 'operator_overrides': list(overrides)}


def _cfg():
    return {'id': IMPORT_ID, 'name': 'IMP', 'enabled': True,
            'm3u_url': M3U_URL, 'epg_url': EPG_URL, 'start_num': 1,
            'refresh_interval_min': 600,
            'last_sync_at': int(time.time()) - 11 * 3600,
            'tombstones': []}


def _sync(monkeypatch, store, xmltv, guide_response, force_scope=None):
    """One cycle: the channel list answers 304 (records are reused), the
    guide answers `guide_response`, every logo address serves its picture."""
    m3u = _M3U()
    importer = Imports(channels=store, m3u=m3u, xmltv=xmltv)
    def _fetch(url, etag=None, last_modified=None, **kw):
        base = {'status': 0, 'content': None, 'etag': None, 'last_modified': None, 'error': None}
        if url == M3U_URL:
            base['status'] = 304
        elif url == EPG_URL:
            base.update(guide_response)
        else:
            raise AssertionError('unexpected fetch: %r' % (url,))
        return base
    monkeypatch.setattr(importer, 'fetchSource', _fetch)
    monkeypatch.setattr(importer, '_fetchLogo', _fetch_by_url())
    importer.syncAll(force_scope=force_scope)
    return m3u


def _link(cid, url, ext='jpg'):
    return 'http://%s/images/%s~%s.%s' % (HOST, cid.replace('@', '%40'), _md5(_logo_bytes(url)), ext)


def _icons(xmltv):
    return {c['id']: (c.get('icon') or [{}])[0].get('src') for c in xmltv.XMLTVDATA['channels']}


def _store():
    return _Store([_cfg()], [
        _record(1, MINE, overrides=['logo', 'number']),            # set in the dashboard
        _record(2, 'https://source.example.org/c2.jpg'),            # from the source
        _record(3, 'special://home/addons/pack/c3.png'),            # local file
    ])


GOOD = {'status': 200, 'content': GUIDE}
DOWN = {'status': 0, 'error': 'Temporary failure in name resolution'}


def test_dashboard_logo_reaches_the_guide_and_the_playlist(monkeypatch):
    store, xmltv = _store(), _XMLTV()
    m3u = _sync(monkeypatch, store, xmltv, GOOD)
    icons = _icons(xmltv)
    assert icons['c1@imp'] == _link('c1@imp', MINE)                 # operator's logo wins
    assert icons['c2@imp'] == 'https://guide.example.org/c2.png'    # untouched: guide's icon
    assert icons['c3@imp'] == 'special://home/addons/pack/c3.png'   # local logo, as before
    assert m3u.stations['c1@imp']['logo'] == _link('c1@imp', MINE)
    assert m3u.stations['c2@imp']['logo'] == _link('c2@imp', 'https://source.example.org/c2.jpg')
    assert m3u.stations['c3@imp']['logo'] == 'special://home/addons/pack/c3.png'


def test_channel_records_never_receive_internal_links(monkeypatch):
    store, xmltv = _store(), _XMLTV()
    _sync(monkeypatch, store, xmltv, GOOD)
    logos = {c['id']: c['logo'] for c in store.channels}
    assert logos == {'c1@imp': MINE,
                     'c2@imp': 'https://source.example.org/c2.jpg',
                     'c3@imp': 'special://home/addons/pack/c3.png'}


def test_changed_dashboard_logo_shows_after_the_next_sync(monkeypatch, tmp_path):
    store, xmltv = _store(), _XMLTV()
    _sync(monkeypatch, store, xmltv, GOOD)
    old_file = tmp_path / 'logos' / _link('c1@imp', MINE).rsplit('/', 1)[1].replace('%40', '@')
    assert old_file.exists()
    newer = 'https://mine.example.org/tv3-new.jpg'
    next(c for c in store.channels if c['id'] == 'c1@imp')['logo'] = newer   # dashboard edit
    m3u = _sync(monkeypatch, store, xmltv, GOOD, force_scope='all')        # its kick
    assert _icons(xmltv)['c1@imp'] == _link('c1@imp', newer)
    assert m3u.stations['c1@imp']['logo'] == _link('c1@imp', newer)
    assert not old_file.exists()


def test_kept_guide_still_gets_the_dashboard_logo(monkeypatch):
    """Guide download fails with no cached copy: the previous slice is
    kept, and the operator's logo is still applied to it."""
    store = _store()
    xmltv = _XMLTV(channels=[
        {'id': 'c1@imp', 'display-name': [('Chan 1', '')], 'icon': [{'src': 'https://guide.example.org/c1.png'}]},
        {'id': 'c2@imp', 'display-name': [('Chan 2', '')], 'icon': [{'src': 'https://guide.example.org/c2.png'}]},
    ])
    _sync(monkeypatch, store, xmltv, DOWN)
    icons = _icons(xmltv)
    assert icons['c1@imp'] == _link('c1@imp', MINE)
    assert icons['c2@imp'] == 'https://guide.example.org/c2.png'


# ================================================================ render_xmltv

def test_render_xmltv_repoints_own_logo_icons_only(monkeypatch):
    import renderers
    monkeypatch.setattr(renderers.PROPERTIES, 'getRemoteHost', lambda: HOST)
    stale = 'http://%s/images/c1%%40imp~0123abcd.jpg' % STALE_HOST
    thumb = 'http://%s/images/show-thumb.jpg' % STALE_HOST
    data = {
        'channels': [
            {'id': 'c1@imp', 'display-name': [('Chan 1', '')], 'icon': [{'src': stale}]},
            {'id': 'c2@imp', 'display-name': [('Chan 2', '')],
             'icon': [{'src': 'https://guide.example.org/c2.png'}]},
        ],
        'programmes': [{'channel': 'c1@imp', 'start': '20990101000000 +0000',
                        'stop': '20990101010000 +0000', 'title': [('One', '')],
                        'icon': [{'src': thumb}]}],
        'recordings': [],
    }
    out = renderers.render_xmltv(data).decode('utf-8')
    assert 'src="http://%s/images/c1%%40imp~0123abcd.jpg"' % HOST in out
    assert 'src="https://guide.example.org/c2.png"' in out
    assert thumb in out                                         # programme icon untouched
    assert data['channels'][0]['icon'][0]['src'] == stale       # input not mutated
