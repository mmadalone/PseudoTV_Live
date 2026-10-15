"""imports.56 — channel logos served by the addon's own /images/ links.

Three defects, one root: cached import logos are published as
http://<remote_host>:<port>/images/<file> (imports.44).
  1. /images/ required a login for every non-loopback client, and Kodi
     fetches channel icons via the LAN address — so the local Kodi got 401
     on every cached logo. Fix: bare image file names inside LOGO_LOC are
     public; every other /images/ form still needs the login.
  2. The host was baked in and Remote_Host was only set when the HTTP
     server started. Fix: render_m3u re-points own links at the current
     host, and the server re-publishes Remote_Host when the address changes.
  3. _cacheLogo tried to download our own links (401, or a 30 s timeout on
     a stale host). Fix: own links are never fetched.

Addresses below are from the RFC 5737 documentation ranges.
"""
import io
import os
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
LIB  = os.path.join(os.path.dirname(HERE), 'resources', 'lib')
if LIB not in sys.path:
    sys.path.insert(0, LIB)

from logo_links import port_set, own_logo_basename, repoint_own_logo, public_logo_name

CURRENT = '192.0.2.10:50002'
STALE   = 'http://198.51.100.7:50002/images/TVE%40movistarplus.png'
PORTS   = {'50002'}


# ================================================================ logo_links

def test_port_set_collects_server_and_configured_ports():
    assert port_set(CURRENT) == {'50002'}
    assert port_set('192.0.2.10:50003', 50002) == {'50002', '50003'}
    assert port_set('', None, 0, '') == set()


def test_own_logo_recognised_even_with_a_stale_host():
    assert own_logo_basename(STALE, PORTS) == 'TVE@movistarplus.png'


@pytest.mark.parametrize('url', [
    'http://198.51.100.7:50009/images/TVE.png',            # other port
    'https://198.51.100.7:50002/images/TVE.png',           # not our scheme
    'https://static.example.org/logos/TVE.png',            # third party
    'http://198.51.100.7:50002/images/..%2Fchannels.json',  # traversal
    'http://198.51.100.7:50002/images/a/b.png',            # nested
    'http://198.51.100.7:50002/images/TVE.png?x=1',        # query
    '', None, 42,
])
def test_not_an_own_logo(url):
    assert own_logo_basename(url, PORTS) is None
    assert repoint_own_logo(url, CURRENT, PORTS) == url


def test_repoint_rewrites_host_and_keeps_quoting():
    assert repoint_own_logo(STALE, CURRENT, PORTS) == \
        'http://192.0.2.10:50002/images/TVE%40movistarplus.png'


def test_repoint_without_current_host_is_a_no_op():
    assert repoint_own_logo(STALE, '', PORTS) == STALE


def test_public_logo_name_accepts_bare_image_names():
    assert public_logo_name('/images/TVE%40movistarplus.png') == 'TVE@movistarplus.png'
    assert public_logo_name('/images/Logo.PNG?cache=1') == 'Logo.PNG'
    for ext in ('jpg', 'jpeg', 'webp', 'gif', 'ico'):
        assert public_logo_name('/images/x.%s' % ext) == 'x.%s' % ext


@pytest.mark.parametrize('path', [
    '/images/..%2Fchannels.json',
    '/images/%2Fetc%2Fpasswd.png',
    '/images/sub/x.png',
    '/images/special%3A%2F%2Fprofile%2Fx.png',
    '/images/x.svg',
    '/images/channels.json',
    '/images/.hidden.png',
    '/images/',
    '/logos/x.png',
    '/images/a%00.png',
    '', None,
])
def test_public_logo_name_rejects_everything_else(path):
    assert public_logo_name(path) is None


# ================================================================ render_m3u

def test_render_m3u_repoints_own_logo_links(monkeypatch):
    import renderers
    monkeypatch.setattr(renderers.PROPERTIES, 'getRemoteHost', lambda: CURRENT)
    stations = [
        {'id': 'TVE@movistarplus', 'number': 1, 'name': 'La 1', 'logo': STALE,
         'group': ['Movistar+'], 'catchup': '', 'radio': False, 'label': 'La 1',
         'url': 'plugin://plugin.video.example/?id=TVE'},
        {'id': 'X@other', 'number': 2, 'name': 'X',
         'logo': 'https://static.example.org/logos/x.png',
         'group': ['Other'], 'catchup': '', 'radio': False, 'label': 'X',
         'url': 'plugin://plugin.video.example/?id=X'},
    ]
    out = renderers.render_m3u({'stations': stations, 'recordings': []})
    assert 'tvg-logo="http://192.0.2.10:50002/images/TVE%40movistarplus.png"' in out
    assert '198.51.100.7' not in out
    assert 'tvg-logo="https://static.example.org/logos/x.png"' in out
    # the stored record is not mutated
    assert stations[0]['logo'] == STALE


# ================================================================ _cacheLogo

def _importer(monkeypatch, existing):
    from imports import Imports
    imp = Imports()
    fetched = []
    monkeypatch.setattr(imp, '_ownLogoPorts', lambda: PORTS)
    monkeypatch.setattr(imp, '_findLogoFile', lambda cid: existing)
    monkeypatch.setattr(imp, '_fetchLogo', lambda url, **kw: fetched.append(url) or {'status': 0})
    return imp, fetched


def test_cacheLogo_never_downloads_own_link_without_file(monkeypatch):
    imp, fetched = _importer(monkeypatch, existing=None)
    assert imp._cacheLogo('TVE@movistarplus', STALE, 0, {}) is None
    assert fetched == []


def test_cacheLogo_reuses_cached_file_for_own_link(monkeypatch):
    imp, fetched = _importer(monkeypatch, existing='/cache/logos/TVE@movistarplus.png')
    for mode in (0, 1, 2):
        assert imp._cacheLogo('TVE@movistarplus', STALE, mode, {}) == '/cache/logos/TVE@movistarplus.png'
    assert fetched == []


def test_cacheLogo_still_fetches_third_party_logos(monkeypatch):
    imp, fetched = _importer(monkeypatch, existing=None)
    imp._cacheLogo('X@other', 'https://static.example.org/logos/x.png', 0, {})
    assert fetched == ['https://static.example.org/logos/x.png']


# ================================================================ eviction across gated cycles

class _Chans:
    def __init__(self, imports_list, channels_list):
        self._i, self._c = list(imports_list), list(channels_list)
    def getImports(self):  return list(self._i)
    def getChannels(self): return list(self._c)
    def setChannels(self, channels=None, modified_ids=None, deleted_ids=None):
        if channels is not None: self._c = list(channels)
        return True
    def setImports(self, data): self._i = list(data); return True


class _M3U:
    def addStation(self, s): pass
    def delStation(self, s): pass


class _XMLTV:
    def __init__(self): self.XMLTVDATA = {'channels': [], 'programmes': [], 'recordings': []}


def test_gated_cycle_keeps_logos_of_present_channels(monkeypatch, tmp_path):
    """imports.56: a skipped import's channels are still present — their
    cached logos must survive the cycle; only logos of channels that are no
    longer in channels.json are evicted."""
    import imports as imports_mod
    from imports import Imports
    monkeypatch.setattr(imports_mod, 'CACHE_LOC', str(tmp_path))
    monkeypatch.setattr(imports_mod, '_isDiskMissing', lambda iid, scope: False)
    real_getBool = imports_mod.SETTINGS.getSettingBool
    monkeypatch.setattr(imports_mod.SETTINGS, 'getSettingBool',
                        lambda key, *a: True if key == 'Imports_Cache_Logos' else real_getBool(key, *a))
    logos = tmp_path / 'logos'
    logos.mkdir()
    (logos / 'c1@imp.png').write_bytes(b'png')
    (logos / 'gone@imp.png').write_bytes(b'png')
    cfg = {'id': 'imp', 'name': 'IMP', 'enabled': True, 'm3u_url': 'https://example.invalid/l.m3u',
           'epg_url': '', 'start_num': 1, 'refresh_interval_min': 600,
           'last_sync_at': int(time.time()) - 300, 'tombstones': []}
    chan = {'id': 'c1@imp', 'import_source': 'imp', 'type': 'import', 'enabled': True,
            'number': 1, 'name': 'Chan 1', 'logo': 'https://static.example.org/c1.png'}
    imp = Imports(channels=_Chans([cfg], [chan]), m3u=_M3U(), xmltv=_XMLTV())
    results = imp.syncAll()
    assert results['imp']['status'] == 'skipped'
    assert (logos / 'c1@imp.png').exists()
    assert not (logos / 'gone@imp.png').exists()


# ================================================================ server: address re-check

class _FakeHTTP:
    ADDRESS_CHECK_SECS = 60
    def log(self, *a, **k): pass


def _patch_server(monkeypatch, *, published, ip, pending_kick=''):
    import server
    calls = {'remote': [], 'settings': {}, 'kick': [], 'getIP': 0}
    def _getIP(*a, **k):
        calls['getIP'] += 1
        return ip
    monkeypatch.setattr(server.PROPERTIES, 'getRemoteHost', lambda: published)
    monkeypatch.setattr(server.PROPERTIES, 'setRemoteHost', lambda v: calls['remote'].append(v) or v)
    monkeypatch.setattr(server.PROPERTIES, 'getEXTProperty', lambda k, d=None: pending_kick if k == 'chkImports.kick' else d)
    monkeypatch.setattr(server.PROPERTIES, 'setEXTProperty', lambda k, v: calls['kick'].append((k, v)))
    monkeypatch.setattr(server.SETTINGS, 'getIP', _getIP)
    monkeypatch.setattr(server.SETTINGS, 'setSetting', lambda k, v: calls['settings'].__setitem__(k, v))
    return server, calls


def test_address_change_republishes_and_rerenders(monkeypatch):
    from imports import RENDER_ONLY_KICK
    server, calls = _patch_server(monkeypatch, published='198.51.100.7:50002', ip='192.0.2.10')
    assert server.HTTP._chkAddress(_FakeHTTP()) is True
    assert calls['remote'] == ['192.0.2.10:50002']
    assert calls['settings']['Remote_M3U'].startswith('http://192.0.2.10:50002/')
    assert calls['settings']['Remote_XMLTV'].startswith('http://192.0.2.10:50002/')
    assert calls['kick'] == [('chkImports.kick', RENDER_ONLY_KICK)]


@pytest.mark.parametrize('ip', ['198.51.100.7', '127.0.0.1', '0.0.0.0', '', None])
def test_unchanged_or_unusable_address_is_ignored(monkeypatch, ip):
    server, calls = _patch_server(monkeypatch, published='198.51.100.7:50002', ip=ip)
    assert server.HTTP._chkAddress(_FakeHTTP()) is False
    assert calls['remote'] == [] and calls['settings'] == {} and calls['kick'] == []


def test_address_change_never_replaces_a_pending_kick(monkeypatch):
    server, calls = _patch_server(monkeypatch, published='198.51.100.7:50002',
                                  ip='192.0.2.10', pending_kick='all')
    assert server.HTTP._chkAddress(_FakeHTTP()) is True
    assert calls['kick'] == []


def test_address_check_runs_at_most_once_a_minute(monkeypatch):
    server, calls = _patch_server(monkeypatch, published='198.51.100.7:50002', ip='198.51.100.7')
    fake = _FakeHTTP()
    server.HTTP._chkAddress(fake)
    server.HTTP._chkAddress(fake)
    assert calls['getIP'] == 1
    fake._lastAddressCheck = time.time() - 61
    server.HTTP._chkAddress(fake)
    assert calls['getIP'] == 2


def test_http_run_loop_calls_address_check():
    with open(os.path.join(LIB, 'server.py')) as f:
        src = f.read()
    run = src[src.index('    def run(self):  '):]
    assert 'self._chkAddress()' in run


# ================================================================ server: public logos

class _RealFileStream:
    """The Kodi-stub FileAccess.stream only reads the stub userdata tree;
    serve real tmp files the way Kodi's VFS does at runtime."""
    def __init__(self, path): self.path = path
    def __enter__(self): return self
    def __exit__(self, *exc): return False
    def readBytes(self):
        with open(self.path, 'rb') as f:
            return f.read()


def _handler(monkeypatch, tmp_path, path, client='198.51.100.20'):
    """A MyHandler wired to fakes — no socket, no Kodi."""
    import server
    monkeypatch.setattr(server, 'LOGO_LOC', str(tmp_path))
    monkeypatch.setattr(server.FileAccess, 'stream', staticmethod(lambda p, *a, **k: _RealFileStream(p)))
    h = server.MyHandler.__new__(server.MyHandler)
    h.path = path
    h.headers = {}
    h.client_address = (client, 54321)
    h.wfile = io.BytesIO()
    h.sent = {'status': [], 'error': []}
    h.send_response = lambda code, msg=None: h.sent['status'].append(code)
    h.send_header = lambda *a, **k: None
    h.end_headers = lambda: None
    h.send_error = lambda code, msg=None: h.sent['error'].append(code)
    return h


def test_cached_logo_is_served_without_login(monkeypatch, tmp_path):
    (tmp_path / 'TVE@movistarplus.png').write_bytes(b'\x89PNG fake')
    h = _handler(monkeypatch, tmp_path, '/images/TVE%40movistarplus.png')
    h.do_GET()
    assert h.sent['status'] == [200]
    assert h.wfile.getvalue() == b'\x89PNG fake'


def test_missing_public_logo_is_404(monkeypatch, tmp_path):
    h = _handler(monkeypatch, tmp_path, '/images/NOPE%40nowhere.png')
    h.do_GET()
    assert h.sent['error'] == [404]
    assert h.wfile.getvalue() == b''


@pytest.mark.parametrize('path', ['/images/..%2Fchannels.json', '/images/x.svg',
                                  '/images/special%3A%2F%2Fprofile%2Fx.png'])
def test_other_images_requests_still_need_login(monkeypatch, tmp_path, path):
    (tmp_path / 'x.svg').write_bytes(b'<svg/>')
    h = _handler(monkeypatch, tmp_path, path)
    h.do_GET()
    assert h.sent['status'] == [401]
    assert h.wfile.getvalue() == b''


def test_logo_symlink_escaping_the_folder_is_not_public(monkeypatch, tmp_path):
    secret = tmp_path.parent / ('secret_%d.png' % os.getpid())
    secret.write_bytes(b'not a logo')
    try:
        (tmp_path / 'evil.png').symlink_to(secret)
        h = _handler(monkeypatch, tmp_path, '/images/evil.png')
        h.do_GET()
        assert h.sent['error'] == [404]
        assert h.wfile.getvalue() == b''
    finally:
        secret.unlink()
