#   Copyright (C) 2025 Lunatixz
#
#
# This file is part of PseudoTV Live.
#
# PseudoTV Live is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# PseudoTV Live is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with PseudoTV Live.  If not, see <http://www.gnu.org/licenses/>.
#
# -*- coding: utf-8 -*-
import base64, gzip, mimetypes, re, socket, errno

from zeroconf                  import *
from globals                   import *
from channels                  import Channels
from resources                 import Resources
from six.moves.BaseHTTPServer  import BaseHTTPRequestHandler, HTTPServer
from six.moves.socketserver    import ThreadingMixIn
#todo proper REST API to handle server/client communication incl. sync/update triggers.
#todo incorporate experimental webserver UI to master branch.

ZEROCONF_SERVICE      = "_xbmc-jsonrpc-h._tcp.local."
COMPRESSION_THRESHOLD = 1024  # 1 KB
CHUNK_SIZE            = 4096 #4 KB
SERVER_STARTED_AT     = time.time()   # Step 6 Phase 5 (madteevee): uptime baseline. Module load == addon (re)start.

# Perf (madteevee): simple module-level caches for expensive endpoints.
# Map key → (timestamp, value). Cleared on module reload (Kodi restart).
SYSINFO_CACHE = {'t': 0, 'v': None}
EPG_CACHE     = {}   # key (xmltv_mtime, window_h) → {'t': float, 'v': dict}
# Settings cache stores the fully-enriched schema response (109 settings with
# current values + resolved InfoBools). Re-built when files change (mtime key)
# or when /api/settings/save.json invalidates after a write. TTL is also short
# enough that a Kodi-side native settings dialog change self-heals on next
# fetch even without explicit invalidation.
SCHEMA_RESPONSE_CACHE = {'key': None, 't': 0, 'v': None}

# madteevee (imports fork): allowlist for filenames in /filelist/ and /api/
# request paths. Channel IDs use alnum + `._@-` (the `@` lives in namespaced
# import IDs like `ALJAZE@movistarplus`). Anything outside this set — slashes,
# `..`, null bytes, control chars — is rejected as a path-traversal attempt.
_SAFE_FILE_RE = re.compile(r'^[A-Za-z0-9._@-]+$')

def _safe_filename(path, prefix):
    name = path.replace(prefix, '', 1).split('?', 1)[0]
    return name if _SAFE_FILE_RE.match(name) else None


def _build_sysinfo_payload():
    """Build the System Info payload (same logic as the on-demand endpoint).
    Used by both the GET handler (fallback) and the pre-warm daemon thread.
    """
    import platform as _platform
    from jsonrpc  import JSONRPC
    jrpc = JSONRPC()
    bools = jrpc.getInfoBooleans([
        'System.HasAddon(pvr.iptvsimple)',
        'System.AddonIsEnabled(pvr.iptvsimple)',
        'Pvr.HasTVChannels',
        'Pvr.IsPlayingTV',
        'Pvr.IsRecording',
    ]) or {}
    labels = jrpc.getInfoLabels([
        'System.BuildVersion',
        'System.OSVersionInfo',
    ]) or {}
    ch = Channels(writable=False)
    info = {
        'addon_id'              : ADDON_ID,
        'addon_name'            : ADDON_NAME,
        'addon_version'         : ADDON_VERSION,
        'kodi_build_version'    : labels.get('System.BuildVersion'),
        'kodi_os_version'       : labels.get('System.OSVersionInfo'),
        'remote_host'           : PROPERTIES.getRemoteHost(),
        'uuid'                  : SETTINGS.getMYUUID(),
        'machine'               : _platform.machine(),
        'python_version'        : _platform.python_version(),
        'tcp_port'              : SETTINGS.getSettingInt('TCP_PORT'),
        'uptime_seconds'        : int(time.time() - SERVER_STARTED_AT),
        'channel_count'         : len(ch.getChannels() or []),
        'imports_count'         : len(ch.getImports() or []),
        'iptv_simple_installed' : bool(bools.get('System.HasAddon(pvr.iptvsimple)')),
        'iptv_simple_enabled'   : bool(bools.get('System.AddonIsEnabled(pvr.iptvsimple)')),
        'pvr_has_tv_channels'   : bool(bools.get('Pvr.HasTVChannels')),
        'pvr_playing_tv'        : bool(bools.get('Pvr.IsPlayingTV')),
        'pvr_recording'         : bool(bools.get('Pvr.IsRecording')),
    }
    del ch
    return info


def _epg_prewarm_loop():
    """Daemon thread: when pseudotv.xml mtime changes (post-Builder syncAll),
    proactively parse + cache a default 4h window starting at the current 60s
    bucket. Cold call after a fresh build → cache hit. Polls every 30s; the
    poll itself is cheap (one stat() call). The actual parse only happens when
    mtime moves.
    """
    last_mtime = 0
    while True:
        try:
            xml_real = FileAccess.translatePath(XMLTVFLEPATH)
            try:    mtime = int(os.path.getmtime(xml_real))
            except OSError: mtime = 0
            if mtime and mtime != last_mtime:
                last_mtime = mtime
                _prewarm_epg_grid(mtime, window_h=4)
        except Exception:
            pass
        time.sleep(30)


def _prewarm_epg_grid(xml_mtime, window_h=4):
    """Parse pseudotv.xml for the current 60s-bucket window and stash the
    response in EPG_CACHE so the next /api/epg/grid.json hits warm.
    Mirrors the inline parser in the do_GET endpoint, kept in sync.
    """
    import xml.etree.ElementTree as _ET
    import re as _re
    _KODI_MARKUP_RE = _re.compile(r'\[/?[A-Z][^\]]*\]')
    def _strip(s):
        if not s: return s
        return _KODI_MARKUP_RE.sub('', s).strip()
    def _xmltv_to_ts(s):
        if not s: return 0
        s = s.strip()
        parts = s.split(' ', 1)
        base = parts[0]
        tz_suffix = parts[1].strip() if len(parts) > 1 else ''
        if len(base) < 14: return 0
        try:
            st = time.strptime(base[:14], '%Y%m%d%H%M%S')
            if tz_suffix and len(tz_suffix) >= 5 and tz_suffix[0] in '+-':
                import calendar as _cal
                sign = 1 if tz_suffix[0] == '+' else -1
                hours = int(tz_suffix[1:3])
                mins  = int(tz_suffix[3:5])
                return _cal.timegm(st) - sign * (hours * 3600 + mins * 60)
            return int(time.mktime(st))
        except Exception:
            return 0
    now = int(time.time())
    start_ts = (now // 60) * 60
    end_ts   = start_ts + window_h * 3600
    xml_real = FileAccess.translatePath(XMLTVFLEPATH)
    ch = Channels(writable=False)
    ch_by_id = {c.get('id'): c for c in (ch.getChannels() or [])}
    del ch
    programmes = []
    channels_used = {}
    try:
        for _, elem in _ET.iterparse(xml_real, events=('end',)):
            if elem.tag != 'programme':
                continue
            s = _xmltv_to_ts(elem.get('start'))
            e = _xmltv_to_ts(elem.get('stop'))
            if e < start_ts or s > end_ts:
                elem.clear(); continue
            cid = elem.get('channel')
            title_el = elem.find('title')
            desc_el  = elem.find('desc')
            programmes.append({
                'channel_id': cid,
                'start': s, 'stop': e,
                'title': _strip((title_el.text if title_el is not None else '') or ''),
                'desc':  _strip((desc_el.text  if desc_el  is not None else '') or ''),
            })
            if cid and cid not in channels_used:
                meta = ch_by_id.get(cid) or {}
                channels_used[cid] = {
                    'id': cid,
                    'name': meta.get('name') or cid,
                    'number': meta.get('number'),
                    'logo': meta.get('logo'),
                }
            elem.clear()
    except FileNotFoundError:
        return
    channels_out = sorted(channels_used.values(), key=lambda c: (c.get('number') or 9999, c.get('name','')))
    programmes.sort(key=lambda p: (p['channel_id'], p['start']))
    grid = {'window': {'start': start_ts, 'end': end_ts, 'now': now},
            'channels': channels_out, 'programmes': programmes}
    EPG_CACHE[(xml_mtime, start_ts, window_h)] = {'t': now, 'v': grid}
    # Bound the cache: keep only 12 most recent entries.
    if len(EPG_CACHE) > 12:
        for old_k in sorted(EPG_CACHE.keys(), key=lambda k: EPG_CACHE[k]['t'])[:-12]:
            del EPG_CACHE[old_k]


# Start EPG pre-warm daemon on module load. (SysInfoPrewarm was dropped per
# operator request — replaced with a longer on-demand TTL to avoid sustained
# background CPU + JSON-RPC chatter when nobody's looking at the dashboard.)
# Thread is already imported from constants.py via the `from globals import *` chain.
_epg_thread = Thread(target=_epg_prewarm_loop, name='EpgPrewarm')
_epg_thread.daemon = True
_epg_thread.start()

class Discovery(Thread):
    class MyListener(object):
        def __init__(self, multiroom=None):
            self.zServers  = {}
            self.zeroconf  = Zeroconf()
            self.multiroom = multiroom

        def log(self, msg, level=xbmc.LOGDEBUG):
            return log('%s: %s'%(self.__class__.__name__,msg),level)

        def removeService(self, zeroconf, type, name):
            self.log("removeService, type = %s, name = %s"%(type,name))
             
        def addService(self, zeroconf, type, name):
            INFO = self.zeroconf.getServiceInfo(type, name)
            if INFO:
                server = INFO.getServer()
                ip     = socket.inet_ntoa(INFO.getAddress())
                self.zServers[server] = {'type':type,'name':name,'server':server,'host':'%s:%d'%(ip,INFO.getPort()),'bonjour':'http://%s:%s/api/%s'%(ip,SETTINGS.getSettingInt('TCP_PORT'),BONJOURFLE)}
                self.log("addService, found zeroconf %s @ %s using bonjour %s"%(server,self.zServers[server]['host'],self.zServers[server]['bonjour']))
                self.multiroom.addServer(Globals.requestURL(self.zServers[server]['bonjour'],cache={'cache':SETTINGS.cache, "life": datetime.timedelta(seconds=300)}))
            
    def __init__(self, service=None, multiroom=None):
        Thread.__init__(self)
        self.daemon    = True
        self.service   = service
        self.monitor   = service.monitor
        self.multiroom = multiroom
        self.start()

    def log(self, msg, level=xbmc.LOGDEBUG):
        return log('%s: %s'%(self.__class__.__name__,msg),level)

    def run(self):
        if not PROPERTIES.isRunning('Discovery.run'):
            with PROPERTIES.chkRunning('Discovery.run'):
                while not self.monitor.abortRequested():
                    try: 
                        zcons = self.multiroom._getStatus()
                        self.log("run, starting ZEROCONF ENABLED (%s)"%(zcons))
                        if zcons:
                            zconf = Zeroconf()
                            self.log("run, Multicast DNS Service waiting for (%s)"%(ZEROCONF_SERVICE))
                            ServiceBrowser(zconf, ZEROCONF_SERVICE, self.MyListener(multiroom=self.multiroom))
                            SETTINGS.setSetting('ZeroConf_Status','[COLOR=yellow][B]%s[/B][/COLOR]'%(LANGUAGE(32252)))
                            if self.service._sleep(DISCOVER_INTERVAL): break
                            self.log("run, Multicast DNS Service stopping search for (%s)"%(ZEROCONF_SERVICE))
                            zconf.close()
                        SETTINGS.setSetting('ZeroConf_Status',LANGUAGE(32211)%({True:'green',False:'red'}[zcons],{True:LANGUAGE(32158),False:LANGUAGE(32253)}[zcons]))
                    except Exception as e:
                        self.log("run, Multicast DNS Service failed! %s"%(e), xbmc.LOGERROR)
                        break
                    if self.service._sleep(600):
                        self.log("run, _sleep", xbmc.LOGERROR)
                        break
                self.log("run, shutting down...")


class MyHandler(BaseHTTPRequestHandler):
    # v.30: switch from default HTTP/1.0 to HTTP/1.1 — required by some external
    # clients. UC Remote 3's image fetcher specifically: it consumes pseudotv's
    # /images/<basename> URLs from PVR.GetChannels icon field, but UC3 silently
    # drops HTTP/1.0 responses for image rendering even though the response body
    # is a valid PNG with correct Content-Type. HTTP/1.1 also enables keep-alive
    # connection reuse for clients that batch-fetch multiple logos. __sendChunk
    # already sets Content-Length on every response, so HTTP/1.1's required
    # message-framing constraint is satisfied without code changes.
    protocol_version = "HTTP/1.1"

    def __init__(self, request, client_address, server, service):
        self.service   = service
        self.monitor   = service.monitor
        self.resources = Resources(service)

        try: BaseHTTPRequestHandler.__init__(self, request, client_address, server)
        except Exception: pass


    def log(self, msg, level=xbmc.LOGDEBUG):
        return log('%s: %s'%(self.__class__.__name__,msg),level)


    # madteevee (imports fork): HTTP auth. Loopback bypasses (browser on the Kodi
    # box, pvr.iptvsimple on loopback). Multiroom peers identify via discovered
    # peer UUID in POST body — that path is preserved. Non-loopback browsers must
    # present HTTP Basic Auth against Kodi's own webserver username/password (read
    # via JSON-RPC). If Kodi's webserver is disabled, LAN auth is impossible and
    # the dashboard becomes loopback-only. Public data-product endpoints
    # (M3U/XMLTV/genres/favicon) skip auth entirely so pvr.iptvsimple on peer
    # Kodi boxes keeps working.
    def _isLoopback(self):
        try: host = (self.client_address[0] or '').lower()
        except Exception: host = ''
        return host in ('127.0.0.1', '::1', 'localhost', '::ffff:127.0.0.1')


    def _isKnownPeer(self, uuid):
        if not uuid or uuid == SETTINGS.getMYUUID(): return False
        try:
            from multiroom import Multiroom
            for server in list(Multiroom().getDiscovery().values()):
                if server.get('uuid') == uuid: return True
        except Exception as e:
            self.log('_isKnownPeer, failed! %s'%(e), xbmc.LOGWARNING)
        return False


    def _checkBasicAuth(self):
        auth = self.headers.get('Authorization', '')
        if not auth.startswith('Basic '): return False
        try:
            decoded = base64.b64decode(auth[6:]).decode('utf-8')
            user, sep, password = decoded.partition(':')
            if not sep: return False
        except Exception:
            return False
        try:
            jrpc = self.service.jsonRPC
            if not jrpc.getSettingValue('services.webserver', default=False, cache=True):
                # Kodi's own webserver is off — no creds to compare against.
                return False
            expected_user = jrpc.getSettingValue('services.webserverusername', default='kodi', cache=True)
            expected_pw   = jrpc.getSettingValue('services.webserverpassword', default='', cache=True)
        except Exception as e:
            self.log('_checkBasicAuth, JSON-RPC lookup failed! %s'%(e), xbmc.LOGWARNING)
            return False
        return user == expected_user and password == expected_pw


    def _verifyAuth(self, incoming=None):
        if self._isLoopback(): return True
        if incoming and self._isKnownPeer(incoming.get('uuid')): return True
        return self._checkBasicAuth()


    def _requireAuth(self, incoming=None):
        if self._verifyAuth(incoming): return True
        self.send_response(401, 'Unauthorized')
        self.send_header('WWW-Authenticate', 'Basic realm="PseudoTV Imports"')
        self.send_header('Content-Length', '0')
        self.end_headers()
        return False


    def do_HEAD(self):
        self.log('do_HEAD, incoming path = %s'%(self.path))
        self.send_response(200, "OK")
        self.send_header("Content-type", "*/*")
        self.end_headers()
        
        
    def do_POST(self):
        self.log('do_POST, incoming path = %s'%(self.path))
        # madteevee (imports fork): UUID-only auth was broken — the local UUID
        # is embedded verbatim in /manager.html (channels.py:294), so any LAN
        # peer that fetched the page could replay it. Auth now goes through
        # _requireAuth: loopback bypasses, multiroom peers still authenticate
        # via discovered-peer UUID in the request body, browsers on LAN must
        # supply HTTP Basic Auth against Kodi's own webserver credentials.
        if self.path.lower().endswith('.json'):
            try:    incoming = FileAccess.loadJSON(self.rfile.read(int(self.headers['content-length'])).decode())
            except Exception: incoming = {}

            if self._requireAuth(incoming):
                self.log('do_POST [%s] authed (uuid=%s)'%(self.path, incoming.get('uuid')))
                #channels - channel manager save
                if self.path.lower() == '/%s'%(CHANNELFLE.lower()) and incoming.get('payload'):
                    channels = Channels(writable=True)
                    if channels.setChannels(list(channels._verify(incoming.get('payload')))):
                        DIALOG.notificationDialog(LANGUAGE(30085)%(LANGUAGE(30108),incoming.get('name',ADDON_NAME)))
                    del channels
                    self.send_response(200, "OK")
                # imports fork (madteevee): live-imports management endpoints
                # POST /imports.json — save full imports list (form: {uuid, payload: [{...}, ...]})
                elif self.path.split('?', 1)[0].lower() == '/imports.json' and incoming.get('payload') is not None:
                    try:
                        from imports import validate_source_url, ALLOWED_M3U_SCHEMES, ALLOWED_EPG_SCHEMES
                        # Server-side validation: scheme allowlist (plan H2)
                        for imp in (incoming.get('payload') or []):
                            for url in (imp.get('m3u_url'), imp.get('epg_url')):
                                if url:
                                    validate_source_url(url, ALLOWED_M3U_SCHEMES)
                            for path in (imp.get('m3u_path'), imp.get('epg_path')):
                                if path:
                                    validate_source_url(path, ALLOWED_M3U_SCHEMES)
                        channels = Channels(writable=True)
                        channels.setImports(incoming.get('payload') or [])
                        del channels
                        DIALOG.notificationDialog('Imports saved (%d configured)'%(len(incoming.get('payload') or [])))
                        # Send proper HTTP response with Content-Length + end_headers,
                        # otherwise browser fetch hangs waiting for the body delimiter.
                        body = b'{"ok":true}'
                        self.send_response(200, "OK")
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                    except ValueError as e:
                        self.send_error(400, "Invalid import: %s"%(e))
                    except Exception as e:
                        self.log('do_POST imports.json failed: %s'%(e), xbmc.LOGERROR)
                        self.send_error(500, "Failed to save imports")
                # POST /pvr/reset.json — refresh Kodi PVR's EPG cache.
                #
                # Two modes (backwards-compat: `hard` defaults to false):
                #
                # - SOFT (default, `hard=false`): non-destructive. Bumps the
                #   M3U+XMLTV mtimes so IPTV Simple's poller picks them up
                #   on its next tick, then fires a targeted PVR.Scan against
                #   IPTV Simple to nudge it directly. Existing EPG entries
                #   survive; new programmes appear within ~60s.
                #
                # - HARD (`hard=true`): destructive. Calls Kodi's
                #   `epg.resetepgdatabase` button setting — the same as
                #   "Settings → PVR & Live TV → Guide → Clear data". WIPES
                #   the entire EPG database for ALL PVR clients (not just
                #   imports). Kodi re-pulls from every client on its own
                #   schedule; live EPG is gone for ~30s+ on a Pi5.
                #
                # The earlier always-hard implementation surprised the
                # operator with "live epg gone" — the soft mode is now the
                # default and matches the button's "Refresh" semantics.
                elif self.path.split('?', 1)[0].lower() == '/pvr/reset.json':
                    try:
                        is_hard = bool(incoming.get('hard'))
                        if is_hard:
                            self.service.jsonRPC.sendJSON({
                                "method": "Settings.SetSettingValue",
                                "params": {"setting": "epg.resetepgdatabase", "value": True},
                            })
                            DIALOG.notificationDialog('EPG database wiped — Kodi will re-pull from all clients (~30s+)')
                            body = b'{"ok":true,"mode":"hard"}'
                        else:
                            # mtime bump → IPTV Simple's m3uRefreshIntervalMins
                            # poller treats both files as changed on its next
                            # tick (we configured it to 1 min).
                            for fpath in (M3UFLEPATH, XMLTVFLEPATH):
                                try:
                                    if os.path.exists(fpath):
                                        os.utime(fpath, None)
                                        self.log('pvr/reset (soft): mtime bumped %s'%(fpath))
                                except Exception as e:
                                    self.log('pvr/reset (soft): mtime bump failed for %s: %s'%(fpath, e), xbmc.LOGWARNING)
                            # Targeted PVR.Scan on IPTV Simple — bypasses the
                            # 1-min poll wait. Same pattern as tasks.py
                            # _maybeFirePVRScan (Step 5 fix #7).
                            try:
                                client = self.service.jsonRPC.getPVRClient(PVR_CLIENT_ID)
                                if client and client.get('clientid') is not None:
                                    self.service.jsonRPC.PVRScan(client.get('clientid'))
                                    self.log('pvr/reset (soft): PVR.Scan fired for clientid=%s'%(client.get('clientid')))
                                else:
                                    self.log('pvr/reset (soft): IPTV Simple not registered — skipped PVR.Scan', xbmc.LOGWARNING)
                            except Exception as e:
                                self.log('pvr/reset (soft): PVR.Scan failed: %s'%(e), xbmc.LOGWARNING)
                            DIALOG.notificationDialog('EPG refresh queued — IPTV Simple re-reading (~60s)')
                            body = b'{"ok":true,"mode":"soft"}'
                        self.send_response(200, "OK")
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                    except Exception as e:
                        self.log('do_POST pvr/reset.json failed: %s'%(e), xbmc.LOGERROR)
                        self.send_error(500, "Failed to reset PVR cache")
                # POST /channels/add.json — create a new PseudoTV-built channel.
                # Minimal fields: name, number (int), path (string source URL).
                # The channel is tagged type='Custom', enabled, changed=True so
                # Builder picks it up on next chkChanged cycle (~5s).
                elif self.path.split('?', 1)[0].lower() == '/channels/add.json':
                    try:
                        from globals import getChannelID
                        name   = (incoming.get('name') or '').strip()
                        number = incoming.get('number')
                        path   = (incoming.get('path') or '').strip()
                        if not name:
                            self.send_error(400, "name required"); return
                        if not isinstance(number, int) or number < 1 or number > CHANNEL_LIMIT:
                            self.send_error(400, "number must be int 1..%d"%(CHANNEL_LIMIT)); return
                        if not path:
                            self.send_error(400, "path required (Kodi library URL or smartplaylist .xsp)"); return
                        channels = Channels(writable=True)
                        ch_list = list(channels.getChannels() or [])
                        # Conflict-bump same as edit: cascade other channels out of the way
                        for c in ch_list:
                            if c.get('number') == number:
                                # Walk up
                                taken = {x.get('number') for x in ch_list}
                                taken.add(number)
                                n = number + 1
                                while n in taken: n += 1
                                c['number'] = n
                                c['assigned_number'] = n
                        # Build the new channel record from the template
                        citem = channels.getTemplate()
                        citem['name']     = name
                        citem['number']   = number
                        citem['path']     = [path]
                        citem['type']     = 'Custom'
                        citem['enabled']  = True
                        citem['changed']  = True
                        citem['favorite'] = False
                        citem['radio']    = path.startswith('musicdb://')
                        citem['group']    = [ADDON_NAME]
                        citem['logo']     = LOGO
                        citem['id']       = getChannelID(name, [path], number, SETTINGS.getMYUUID())
                        ch_list.append(citem)
                        channels.setChannels(channels=ch_list)
                        del channels
                        # Step 6 Phase 9 (madteevee): defer_kick=true suppresses the
                        # chkChanged kick so a batch of adds collapses to ONE Builder run
                        # instead of N. Caller (imports.html Save handler) sends
                        # defer_kick=true on every add except the last.
                        if not incoming.get('defer_kick'):
                            PROPERTIES.setPropTimer('chkChanged')
                        DIALOG.notificationDialog('Channel "%s" added at #%d'%(name, number))
                        body = ('{"ok":true,"id":"%s","number":%d}'%(citem['id'], number)).encode('utf-8')
                        self.send_response(200, "OK")
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                    except Exception as e:
                        self.log('do_POST channels/add.json failed: %s'%(e), xbmc.LOGERROR)
                        self.send_error(500, "Failed to add channel: %s"%(e))
                # POST /channels/delete.json — remove a channel by id (PseudoTV-built only).
                elif self.path.split('?', 1)[0].lower() == '/channels/delete.json':
                    try:
                        cid = incoming.get('channel_id')
                        if not cid:
                            self.send_error(400, "channel_id required"); return
                        channels = Channels(writable=True)
                        ch_list = list(channels.getChannels() or [])
                        before  = len(ch_list)
                        # Refuse delete of imported channels (those are managed via import config)
                        target  = next((c for c in ch_list if c.get('id') == cid), None)
                        if not target:
                            del channels
                            self.send_error(404, "channel_id not found"); return
                        if target.get('type') == 'import':
                            del channels
                            self.send_error(400, "cannot delete imported channels via this endpoint — disable in import config or remove the import"); return
                        ch_list = [c for c in ch_list if c.get('id') != cid]
                        channels.setChannels(channels=ch_list)
                        del channels
                        DIALOG.notificationDialog('Deleted channel "%s"'%(target.get('name','?')))
                        body = ('{"ok":true,"removed":%d}'%(before - len(ch_list))).encode('utf-8')
                        self.send_response(200, "OK")
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                    except Exception as e:
                        self.log('do_POST channels/delete.json failed: %s'%(e), xbmc.LOGERROR)
                        self.send_error(500, "Failed to delete channel: %s"%(e))
                # imports fork (madteevee) — Step 6 Phase 10:
                # POST /channels/edit.json — edit a Custom (PseudoTV-built)
                # channel. Mirrors /imports/channel/edit.json shape but for
                # type='Custom' channels and exposes the full editable field
                # set: name, number, enabled, group, logo, path, catchup,
                # radio, favorite. Rejects type='import' channels (those go
                # through /imports/channel/edit.json which has the import-aware
                # cascade logic).
                elif self.path.split('?', 1)[0].lower() == '/channels/edit.json':
                    try:
                        ALLOWED_FIELDS = {'name', 'number', 'enabled', 'group', 'logo', 'path', 'catchup', 'radio', 'favorite'}
                        channel_id = incoming.get('channel_id')
                        fields     = incoming.get('fields') or {}
                        if not channel_id or not isinstance(fields, dict):
                            self.send_error(400, "channel_id + fields required"); return
                        for k in fields:
                            if k not in ALLOWED_FIELDS:
                                self.send_error(400, "field '%s' not editable"%(k)); return
                        if 'number' in fields:
                            try: fields['number'] = int(fields['number'])
                            except (ValueError, TypeError):
                                self.send_error(400, "number must be int"); return
                            if fields['number'] < 1 or fields['number'] > CHANNEL_LIMIT:
                                self.send_error(400, "number out of range 1..%d"%(CHANNEL_LIMIT)); return
                        channels = Channels(writable=True)
                        ch_list = list(channels.getChannels() or [])
                        target = next((c for c in ch_list if c.get('id') == channel_id), None)
                        if target is None:
                            del channels
                            self.send_error(404, "channel_id not found"); return
                        if target.get('type') == 'import':
                            del channels
                            self.send_error(400, "use /imports/channel/edit.json for type='import' channels"); return
                        cascaded = 0
                        # Coerce list-shaped fields (path stored as list, group too).
                        if 'path'  in fields and not isinstance(fields['path'],  list): fields['path']  = [fields['path']]
                        if 'group' in fields and not isinstance(fields['group'], list):
                            fields['group'] = [g.strip() for g in str(fields['group']).split('|') if g.strip()] or [ADDON_NAME]
                        for k in ('enabled', 'radio', 'favorite'):
                            if k in fields: fields[k] = bool(fields[k])
                        for k, v in fields.items():
                            target[k] = v
                        # Cascade-bump on number collision (mirrors imports/channel/edit.json).
                        if 'number' in fields:
                            target_num = fields['number']
                            allocations = {target_num: channel_id}
                            others = [c for c in ch_list if c.get('id') != channel_id]
                            for c in sorted(others, key=lambda x: x.get('number', 0) or 0):
                                cur = c.get('number')
                                if not isinstance(cur, int) or cur < 1: continue
                                n = cur
                                while n in allocations:
                                    n += 1
                                    if n > CHANNEL_LIMIT: n = None; break
                                if n is None: continue
                                allocations[n] = c.get('id')
                                if n != cur:
                                    c['number'] = n
                                    c['assigned_number'] = n
                                    cascaded += 1
                            target['assigned_number'] = target_num
                        # Mark this channel as needing a Builder rebuild.
                        target['changed'] = True
                        channels.setChannels(channels=ch_list)
                        del channels
                        # defer_kick=true suppresses chkChanged; caller batches.
                        if not incoming.get('defer_kick'):
                            PROPERTIES.setPropTimer('chkChanged')
                        body = ('{"ok":true,"cascaded":%d}'%(cascaded)).encode('utf-8')
                        self.send_response(200, "OK")
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                    except Exception as e:
                        self.log('do_POST channels/edit.json failed: %s'%(e), xbmc.LOGERROR)
                        self.send_error(500, "Failed to edit channel: %s"%(e))
                # imports fork (madteevee) — Step 6 Phase 2:
                # POST /api/settings/save.json — batch-apply addon settings.
                # Body: {uuid, changes: {setting_id: new_value, ...}}.
                # Dispatches per-type (Bool/Int/String) using the parsed schema as
                # the source of truth for types + constraints. Unknown ids,
                # type-mismatches, and constraint violations are rejected, not
                # raised. Action settings are NOT saveable here (Phase 4 will
                # add /api/settings/action.json for those).
                elif self.path.split('?', 1)[0].lower() == '/api/settings/save.json':
                    try:
                        from settings_schema import get_schema
                        changes = incoming.get('changes') or {}
                        if not isinstance(changes, dict):
                            self.send_error(400, "changes must be {setting_id: value, ...}")
                            return
                        schema  = get_schema(SETTINGS_XML_PATH, STRINGS_PO_PATH)
                        by_id   = {}
                        for cat in schema.get('categories', []):
                            for grp in cat.get('groups', []):
                                for st in grp.get('settings', []):
                                    if st.get('id'): by_id[st['id']] = st
                        applied, rejected = [], []
                        for key, val in changes.items():
                            setting = by_id.get(key)
                            if setting is None:
                                rejected.append({'key': key, 'reason': 'unknown setting id'}); continue
                            stype = setting.get('type') or ''
                            constraints = setting.get('constraints') or {}
                            try:
                                if stype == 'action':
                                    rejected.append({'key': key, 'reason': 'action settings are not saveable; use /api/settings/action.json'}); continue
                                if stype == 'boolean':
                                    if not isinstance(val, bool):
                                        rejected.append({'key': key, 'reason': 'expected boolean'}); continue
                                    SETTINGS.setSettingBool(key, val)
                                elif stype == 'integer':
                                    try: ival = int(val)
                                    except (TypeError, ValueError):
                                        rejected.append({'key': key, 'reason': 'expected integer'}); continue
                                    if 'options' in constraints:
                                        allowed = {opt.get('value') for opt in constraints['options']}
                                        if str(ival) not in allowed:
                                            rejected.append({'key': key, 'reason': 'value not in options %s'%(sorted(allowed))}); continue
                                    if 'minimum' in constraints and ival < constraints['minimum']:
                                        rejected.append({'key': key, 'reason': 'below minimum %s'%(constraints['minimum'])}); continue
                                    if 'maximum' in constraints and ival > constraints['maximum']:
                                        rejected.append({'key': key, 'reason': 'above maximum %s'%(constraints['maximum'])}); continue
                                    SETTINGS.setSettingInt(key, ival)
                                elif stype and stype.startswith('list'):
                                    if not isinstance(val, list):
                                        rejected.append({'key': key, 'reason': 'expected list'}); continue
                                    joined = '|'.join(str(x) for x in val)
                                    SETTINGS.setSetting(key, joined)
                                else:
                                    # string, path, colorbutton — all string-stored
                                    sval = '' if val is None else str(val)
                                    if 'options' in constraints:
                                        allowed = {opt.get('value') for opt in constraints['options']}
                                        if sval not in allowed:
                                            rejected.append({'key': key, 'reason': 'value not in options %s'%(sorted(allowed))}); continue
                                    if constraints.get('allowempty') is False and sval == '':
                                        rejected.append({'key': key, 'reason': 'empty not allowed'}); continue
                                    SETTINGS.setSettingString(key, sval)
                                applied.append(key)
                            except Exception as e:
                                rejected.append({'key': key, 'reason': str(e)})
                        # Invalidate the schema response cache so the next GET
                        # re-reads fresh `current` values for all settings.
                        SCHEMA_RESPONSE_CACHE['t'] = 0
                        SCHEMA_RESPONSE_CACHE['v'] = None
                        body = FileAccess.dumpJSON({'ok': True, 'applied': applied, 'rejected': rejected}).encode('utf-8')
                        self.send_response(200, "OK")
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                    except Exception as e:
                        self.log('do_POST api/settings/save.json failed: %s'%(e), xbmc.LOGERROR)
                        self.send_error(500, "Failed to save settings: %s"%(e))
                # imports fork (madteevee) — Step 6 Phase 4:
                # POST /api/settings/action.json — fire an action setting's
                # `<data>` builtin on Kodi. Body: {uuid, setting_id}.
                # Server looks up <data> from the parsed schema — client never
                # supplies the RunScript string. Limits the blast radius to
                # whatever settings.xml already declares.
                elif self.path.split('?', 1)[0].lower() == '/api/settings/action.json':
                    try:
                        from settings_schema import get_schema
                        sid = (incoming.get('setting_id') or '').strip()
                        if not sid:
                            self.send_error(400, "setting_id required"); return
                        schema = get_schema(SETTINGS_XML_PATH, STRINGS_PO_PATH)
                        setting = None
                        for cat in schema.get('categories', []):
                            for grp in cat.get('groups', []):
                                for st in grp.get('settings', []):
                                    if st.get('id') == sid:
                                        setting = st; break
                                if setting: break
                            if setting: break
                        if setting is None:
                            self.send_error(404, "unknown setting id: %s"%(sid)); return
                        data = setting.get('data')
                        if not data:
                            self.send_error(400, "setting %s has no <data> payload"%(sid)); return
                        # Trigger on Kodi's UI thread. Returns immediately;
                        # the dialog/script runs async on the TV.
                        xbmc.executebuiltin(data)
                        body = FileAccess.dumpJSON({'ok': True, 'setting_id': sid, 'fired': data}).encode('utf-8')
                        self.send_response(200, "OK")
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                    except Exception as e:
                        self.log('do_POST api/settings/action.json failed: %s'%(e), xbmc.LOGERROR)
                        self.send_error(500, "Failed to fire action: %s"%(e))
                # imports fork (madteevee) — Step 6 Phase 11:
                # POST /api/channels/rules.json — write the rules dict for one
                # PseudoTV-built channel. Body: {uuid, channel_id, rules: {ruleId:{values:{idx:value}}}, defer_kick}.
                # Validates each rule_id exists in RulesList; preserves other
                # channel fields via field-level merge. Rejects type='import'.
                elif self.path.split('?', 1)[0].lower() == '/api/channels/rules.json':
                    try:
                        from rules import RulesList
                        cid    = incoming.get('channel_id')
                        rules  = incoming.get('rules') or {}
                        if not cid or not isinstance(rules, dict):
                            self.send_error(400, "channel_id + rules required"); return
                        # Build set of valid rule ids from the live RulesList.
                        catalog = {r.myId: r for r in RulesList().allRules()}
                        rejected = []
                        normalized = {}
                        for raw_rid, raw_block in rules.items():
                            try: rid = int(raw_rid)
                            except (TypeError, ValueError):
                                rejected.append({'rule_id': raw_rid, 'reason': 'rule_id must be int'}); continue
                            if rid not in catalog:
                                rejected.append({'rule_id': rid, 'reason': 'unknown rule id'}); continue
                            if not isinstance(raw_block, dict):
                                rejected.append({'rule_id': rid, 'reason': 'rule block must be object'}); continue
                            values = (raw_block.get('values') or {}) if isinstance(raw_block.get('values'), dict) else {}
                            norm_values = {}
                            for raw_idx, v in values.items():
                                try: norm_values[int(raw_idx)] = v
                                except (TypeError, ValueError):
                                    rejected.append({'rule_id': rid, 'idx': raw_idx, 'reason': 'idx must be int'}); continue
                            normalized[rid] = {'values': norm_values}
                        channels = Channels(writable=True)
                        ch_list = list(channels.getChannels() or [])
                        target = next((c for c in ch_list if c.get('id') == cid), None)
                        if target is None:
                            del channels
                            self.send_error(404, "channel_id not found"); return
                        if target.get('type') == 'import':
                            del channels
                            self.send_error(400, "rules editor is for PseudoTV-built (type=Custom) channels only"); return
                        target['rules']   = normalized
                        target['changed'] = True
                        channels.setChannels(channels=ch_list)
                        del channels
                        if not incoming.get('defer_kick'):
                            PROPERTIES.setPropTimer('chkChanged')
                        body = FileAccess.dumpJSON({'ok': True, 'channel_id': cid, 'applied': sorted(normalized.keys()), 'rejected': rejected}).encode('utf-8')
                        self.send_response(200, "OK")
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                    except Exception as e:
                        self.log('do_POST api/channels/rules.json failed: %s'%(e), xbmc.LOGERROR)
                        self.send_error(500, "Failed to save rules: %s"%(e))
                # POST /imports/channel/edit.json — update arbitrary fields on
                # one channel (number, enabled, name) + mark them as
                # operator_overrides so syncAll preserves the manual edit.
                # Body: {uuid, channel_id, fields: {key: value, ...}}.
                # ALLOWED_FIELDS gates which keys can be set from the UI.
                elif self.path.split('?', 1)[0].lower() == '/imports/channel/edit.json':
                    try:
                        # 'logo' added so the Imports-tab logo editor (post-consolidation)
                        # can stage per-channel logo overrides. syncAll preserves the
                        # operator_overrides set, so the manual logo survives next sync.
                        ALLOWED_FIELDS = {'number', 'enabled', 'name', 'logo'}
                        channel_id = incoming.get('channel_id')
                        fields     = incoming.get('fields') or {}
                        if not channel_id or not isinstance(fields, dict):
                            self.send_error(400, "channel_id + fields required")
                        else:
                            # Validate fields
                            for k in fields:
                                if k not in ALLOWED_FIELDS:
                                    self.send_error(400, "field '%s' not editable"%(k))
                                    return
                            if 'number' in fields:
                                try: fields['number'] = int(fields['number'])
                                except (ValueError, TypeError):
                                    self.send_error(400, "number must be int")
                                    return
                                if fields['number'] < 1 or fields['number'] > CHANNEL_LIMIT:
                                    self.send_error(400, "number out of range 1..%d"%(CHANNEL_LIMIT))
                                    return
                            channels = Channels(writable=True)
                            ch_list = list(channels.getChannels() or [])
                            found = False
                            cascaded = 0
                            target = None
                            for c in ch_list:
                                if c.get('id') == channel_id:
                                    overrides = set(c.get('operator_overrides') or [])
                                    for k, v in fields.items():
                                        c[k] = bool(v) if k == 'enabled' else v
                                        overrides.add(k)
                                    c['operator_overrides'] = sorted(overrides)
                                    if 'number' in fields:
                                        c['assigned_number'] = fields['number']
                                    found = True
                                    target = c
                                    break

                            # Compact-on-disable: when enabled→false on an imported
                            # channel, immediately shift higher-numbered channels of
                            # the SAME import DOWN to fill the freed slot. Without
                            # this the gap stays empty until cascadeAllocate runs
                            # AND only new channels naturally fill — sticky preserves
                            # existing channels' numbers (which is correct in general
                            # but unwanted here, per operator request).
                            if found and target and 'enabled' in fields and not fields['enabled']:
                                old_num = target.get('number')
                                import_id = target.get('import_source')
                                if isinstance(old_num, int) and import_id:
                                    # Collect siblings: same import, enabled, not operator-number-pinned, number > old_num
                                    siblings = [c for c in ch_list
                                                if c.get('import_source') == import_id
                                                and c.get('id') != channel_id
                                                and c.get('enabled', True)
                                                and isinstance(c.get('number'), int)
                                                and c['number'] > old_num
                                                and c['number'] < 9000  # exclude bucket
                                                and 'number' not in (c.get('operator_overrides') or [])]
                                    siblings.sort(key=lambda c: c['number'])
                                    # Numbers held by everyone NOT being shifted (so we don't collide with them)
                                    held = {c.get('number') for c in ch_list
                                            if c.get('id') != channel_id
                                            and c.get('id') not in {s['id'] for s in siblings}
                                            and isinstance(c.get('number'), int)}
                                    n = old_num
                                    for sib in siblings:
                                        while n in held:
                                            n += 1
                                        if sib['number'] != n:
                                            sib['number'] = n
                                            sib['assigned_number'] = n
                                            cascaded += 1
                                        held.add(n)
                                        n += 1
                                    # Move the disabled channel into the 9000+ bucket
                                    # immediately (next free slot). Without this, the
                                    # channel keeps its old number AND the sibling we
                                    # just shifted to old_num shares the same number
                                    # until the next cascadeAllocate cycle. The UI
                                    # auto-reload at +600ms is too fast to see the
                                    # eventual bucket assignment, so we do it here.
                                    DISABLED_BUCKET_START = 9000
                                    used_bucket = {c.get('number') for c in ch_list
                                                   if isinstance(c.get('number'), int)
                                                   and c.get('number') >= DISABLED_BUCKET_START
                                                   and c.get('id') != channel_id}
                                    bn = DISABLED_BUCKET_START
                                    while bn in used_bucket:
                                        bn += 1
                                        if bn > CHANNEL_LIMIT:
                                            bn = None; break
                                    if bn is not None:
                                        target['number'] = bn
                                        target['assigned_number'] = bn
                                        cascaded += 1
                            # Re-enable: channel was in the 9000+ bucket while
                            # disabled. On enable, give it back a real number
                            # immediately so the UI doesn't show it stuck in the
                            # bucket until the next cascadeAllocate cycle.
                            elif found and target and 'enabled' in fields and fields['enabled']:
                                old_num   = target.get('number')
                                import_id = target.get('import_source')
                                if isinstance(old_num, int) and old_num >= 9000 and import_id:
                                    # Find the import's start_num; default 1.
                                    imports = list(channels.getImports() or [])
                                    imp_cfg = next((i for i in imports if i.get('id') == import_id), {})
                                    start_num = int(imp_cfg.get('start_num') or 1)
                                    # `held` excludes target so we can re-claim cleanly.
                                    held = {c.get('number') for c in ch_list
                                            if c.get('id') != channel_id
                                            and isinstance(c.get('number'), int)
                                            and c.get('number') < 9000}
                                    n = start_num
                                    while n in held and n < 9000:
                                        n += 1
                                    if n < 9000:
                                        target['number'] = n
                                        target['assigned_number'] = n
                                        cascaded += 1
                            # Cascade-bump on number collision: operator's pin wins.
                            # Iterate OTHER channels in current-number order; bump any
                            # whose number is now claimed to the next free slot. Chains
                            # naturally (B at #3 bumped to #4; C at #4 bumped to #5; etc).
                            if found and 'number' in fields:
                                target_num = fields['number']
                                allocations = {target_num: channel_id}
                                others = [c for c in ch_list if c.get('id') != channel_id]
                                for c in sorted(others, key=lambda x: x.get('number', 0) or 0):
                                    cur = c.get('number')
                                    if not isinstance(cur, int) or cur < 1:
                                        continue
                                    n = cur
                                    while n in allocations:
                                        n += 1
                                        if n > CHANNEL_LIMIT:
                                            n = None; break
                                    if n is None:
                                        continue
                                    allocations[n] = c.get('id')
                                    if n != cur:
                                        c['number'] = n
                                        c['assigned_number'] = n
                                        cascaded += 1
                            if found:
                                channels.setChannels(channels=ch_list)
                            del channels
                            if not found:
                                self.send_error(404, "channel_id not found")
                            else:
                                # `defer_kick=true` (e.g. from imports.html's batch
                                # Save button) skips the chkImports kick so a burst
                                # of edits doesn't trigger overlapping syncAll
                                # cycles that race against subsequent edits in
                                # the same batch. Caller is responsible for
                                # firing ONE kick at end-of-batch via
                                # /imports/refresh.json.
                                if not incoming.get('defer_kick'):
                                    PROPERTIES.setEXTProperty('chkImports.kick', 'all')
                                body = ('{"ok":true,"cascaded":%d}'%(cascaded)).encode('utf-8')
                                self.send_response(200, "OK")
                                self.send_header("Content-Type", "application/json")
                                self.send_header("Content-Length", str(len(body)))
                                self.end_headers()
                                self.wfile.write(body)
                    except Exception as e:
                        self.log('do_POST imports/channel/edit.json failed: %s'%(e), xbmc.LOGERROR)
                        self.send_error(500, "Failed to edit channel")
                # POST /imports/channel/toggle.json — flip the `enabled` flag on
                # one channel. Lighter-weight than a full /channels.json POST when
                # the operator just wants to disable a single channel from the
                # imports management page.
                elif self.path.split('?', 1)[0].lower() == '/imports/channel/toggle.json':
                    try:
                        channel_id = incoming.get('channel_id')
                        new_enabled = bool(incoming.get('enabled'))
                        if not channel_id:
                            self.send_error(400, "channel_id required")
                        else:
                            channels = Channels(writable=True)
                            ch_list = list(channels.getChannels() or [])
                            found = False
                            for c in ch_list:
                                if c.get('id') == channel_id:
                                    c['enabled'] = new_enabled
                                    # mark this as an operator override so syncAll preserves it
                                    overrides = set(c.get('operator_overrides') or [])
                                    overrides.add('enabled')
                                    c['operator_overrides'] = sorted(overrides)
                                    found = True
                                    break
                            if found:
                                channels.setChannels(channels=ch_list)
                            del channels
                            if not found:
                                self.send_error(404, "channel_id not found")
                            else:
                                # Trigger a refresh so the M3U is regenerated without/with this channel
                                PROPERTIES.setEXTProperty('chkImports.kick', 'all')
                                body = b'{"ok":true}'
                                self.send_response(200, "OK")
                                self.send_header("Content-Type", "application/json")
                                self.send_header("Content-Length", str(len(body)))
                                self.end_headers()
                                self.wfile.write(body)
                    except Exception as e:
                        self.log('do_POST imports/channel/toggle.json failed: %s'%(e), xbmc.LOGERROR)
                        self.send_error(500, "Failed to toggle channel")
                # POST /imports/renumber.json — re-allocation for one import.
                #
                # Two modes via the `reset_pins` body field (default: false):
                #
                # - SOFT (default, reset_pins=false): COMPACT. Sorts enabled
                #   non-orphan channels of target_id by their CURRENT number
                #   (preserving operator's hand-pinned relative order), then
                #   re-assigns them sequentially from `start_num`, only skipping
                #   numbers claimed by OTHER imports' pins or PseudoTV-Custom
                #   channels. Fills every gap. Pins stay pinned at the new
                #   compact positions ('number' in operator_overrides preserved).
                #   Disabled channels keep their 9000+ bucket numbers.
                #
                # - HARD (reset_pins=true): wipes 'number' from operator_overrides
                #   on EVERY target channel, then runs Imports.cascadeAllocate
                #   from source_tvg_chno. Drops operator pins entirely.
                #
                # Both modes rewrite pseudotv.m3u inline so IPTV Simple picks up
                # new numbers within its 1-min poll. Safe during playback because
                # we never call xbmcvfs.Stat on the player addon's special://
                # M3U (Step 5 fix #5 wedge cause). XMLTV unchanged — programmes
                # are keyed by channel id, not number.
                elif self.path.split('?', 1)[0].lower() == '/imports/renumber.json':
                    try:
                        target_id  = incoming.get('id') or ''
                        reset_pins = bool(incoming.get('reset_pins'))
                        if not target_id:
                            self.send_error(400, "id required")
                        else:
                            from imports import Imports
                            from m3u     import M3U
                            channels = Channels(writable=True)
                            ch_list = list(channels.getChannels() or [])

                            cleared       = 0  # target channels whose number changed
                            pins_cleared  = 0  # only in hard mode
                            cascaded      = 0  # alias of cleared, kept for response compat

                            if reset_pins:
                                # HARD mode: wipe target's 'number' overrides + assigned_number,
                                # then re-cascade from source_tvg_chno. (Existing behavior.)
                                for c in ch_list:
                                    if c.get('import_source') == target_id and c.get('type') == 'import':
                                        c['assigned_number'] = None
                                        ov = list(c.get('operator_overrides') or [])
                                        if 'number' in ov:
                                            c['operator_overrides'] = [o for o in ov if o != 'number']
                                            pins_cleared += 1
                                try:
                                    imports_cfg = list(channels.getImports() or [])
                                    parsed_imports = []
                                    for imp in imports_cfg:
                                        iid = imp.get('id')
                                        if not iid:
                                            continue
                                        src_like = [c for c in ch_list
                                                    if c.get('import_source') == iid
                                                    and c.get('type') == 'import'
                                                    and not c.get('is_orphan')]
                                        parsed_imports.append((imp, src_like))
                                    allocator = Imports(channels=channels, m3u=None, xmltv=None,
                                                        service=self.service)
                                    allocations = allocator.cascadeAllocate(parsed_imports, ch_list)
                                    for c in ch_list:
                                        cid = c.get('id')
                                        if not cid or cid not in allocations:
                                            continue
                                        n = allocations.get(cid)
                                        if n is None:
                                            c['unallocated'] = True
                                            continue
                                        c['assigned_number'] = n
                                        if 'number' not in (c.get('operator_overrides') or []):
                                            if c.get('number') != n:
                                                c['number'] = n
                                                cleared += 1
                                except Exception as e:
                                    self.log('renumber HARD, cascade failed: %s'%(e), xbmc.LOGWARNING)
                                cascaded = cleared
                            else:
                                # SOFT mode: COMPACT.
                                # Collect target channels that should live in the live range
                                # (enabled, non-orphan). Sort by current number — that's
                                # operator's intended order, whether the numbers came from
                                # source tvg-chno or hand pins. Channels in the 9000+ bucket
                                # or orphans are left untouched.
                                DISABLED_BUCKET_START = 9000
                                target_live = []
                                for c in ch_list:
                                    if (c.get('import_source') == target_id
                                        and c.get('type') == 'import'
                                        and c.get('enabled', True)
                                        and not c.get('is_orphan')):
                                        n = c.get('number')
                                        if isinstance(n, int) and 1 <= n < DISABLED_BUCKET_START:
                                            target_live.append(c)
                                target_live.sort(key=lambda c: c.get('number') if isinstance(c.get('number'), int) else 999999)

                                # Slots claimed by anything that's NOT in this import — we
                                # respect those as hard skips:
                                #   - PseudoTV-Custom channels (always sacred).
                                #   - Other imports' channels with 'number' in operator_overrides
                                #     (cross-import operator pins).
                                #   - Other imports' channels at any live number that's NOT
                                #     in their import's cascade range — we just treat every
                                #     live-range number from OTHER imports as claimed, to
                                #     avoid clobbering them. (Conservative: better not to
                                #     bump a sibling import's channels around just because
                                #     we're compacting this one.)
                                blocked_nums = set()
                                for c in ch_list:
                                    if c.get('import_source') == target_id and c.get('type') == 'import':
                                        continue
                                    n = c.get('number')
                                    if isinstance(n, int) and 1 <= n < DISABLED_BUCKET_START:
                                        blocked_nums.add(n)

                                # Find target import's start_num. Default 1.
                                imp_cfg = next((i for i in (channels.getImports() or [])
                                                if i.get('id') == target_id), {})
                                start_num = int(imp_cfg.get('start_num') or 1)

                                # Walk target channels in current order, assign sequential.
                                next_n = max(1, start_num)
                                for c in target_live:
                                    while next_n in blocked_nums:
                                        next_n += 1
                                        if next_n >= DISABLED_BUCKET_START:
                                            break
                                    if next_n >= DISABLED_BUCKET_START:
                                        # Ran out of room; rest become unallocated.
                                        c['unallocated'] = True
                                        continue
                                    if c.get('number') != next_n:
                                        c['number'] = next_n
                                        cleared += 1
                                    c['assigned_number'] = next_n
                                    # Pin at new compact position — operator's edits
                                    # against this number stick across syncs.
                                    ov = set(c.get('operator_overrides') or [])
                                    ov.add('number')
                                    c['operator_overrides'] = sorted(ov)
                                    next_n += 1
                                cascaded = cleared

                            # Persist channels.json with the new allocation.
                            channels.setChannels(channels=ch_list)
                            del channels

                            # Rewrite pseudotv.m3u from CACHED data + the freshly-numbered
                            # channels. No source fetch — we just re-render with new
                            # tvg-chno values. XMLTV is unchanged (it's channel-id-keyed,
                            # not number-keyed) so programmes stay matched.
                            m3u_written = False
                            try:
                                m3u = M3U(writable=False)
                                # Build id→number map from the channels we just wrote.
                                # Re-read via Channels() to avoid stale-snapshot issues
                                # between threads.
                                from channels import Channels as _Channels
                                cnow = list((_Channels().getChannels() or []))
                                num_by_id = {c.get('id'): c.get('number')
                                             for c in cnow
                                             if isinstance(c.get('number'), int)}
                                stations = list(m3u.M3UDATA.get('stations') or [])
                                recordings = list(m3u.M3UDATA.get('recordings') or [])
                                touched = 0
                                for s in stations + recordings:
                                    sid = s.get('id')
                                    if sid in num_by_id and isinstance(num_by_id[sid], int):
                                        if s.get('number') != num_by_id[sid]:
                                            s['number'] = num_by_id[sid]
                                            touched += 1
                                if touched:
                                    # imports.13 / C#10 Follow-up A: renderers moved
                                    # to module-level resources/lib/renderers.py (was
                                    # self.service.tasks._renderM3U / _writeAtomic).
                                    # Add None-check on render_m3u to skip wiping disk
                                    # on the unlikely empty-M3UDATA path.
                                    from renderers import render_m3u, write_atomic
                                    rendered = render_m3u(m3u.M3UDATA)
                                    if rendered is not None:
                                        write_atomic(M3UFLEPATH, rendered)
                                        m3u_written = True
                                        self.log('renumber, rewrote pseudotv.m3u with %d updated numbers'%(touched))
                                    else:
                                        self.log('renumber, skipped M3U rewrite — empty M3UDATA', xbmc.LOGWARNING)
                                del m3u
                            except Exception as e:
                                self.log('renumber, M3U rewrite failed (will fall back to next chkImports cycle): %s'%(e), xbmc.LOGWARNING)

                            # Final kick: if cascade or M3U write was partial, the next
                            # chkImports cycle will pick up the channels.json state and
                            # finish the job. Otherwise this is a no-op until the natural
                            # 5-min cycle, but no harm done.
                            PROPERTIES.setEXTProperty('chkImports.kick', target_id)

                            DIALOG.notificationDialog(
                                'Renumber%s: cleared %d, cascaded %d%s in [%s]%s' % (
                                    ' (HARD)' if reset_pins else '',
                                    cleared, cascaded,
                                    (', %d pins wiped' % pins_cleared) if reset_pins else '',
                                    target_id,
                                    '' if m3u_written else ' (M3U regen pending)',
                                )
                            )
                            body = ('{"ok":true,"cleared":%d,"cascaded":%d,"pins_cleared":%d,"m3u_written":%s}' % (
                                cleared, cascaded, pins_cleared, 'true' if m3u_written else 'false',
                            )).encode('utf-8')
                            self.send_response(200, "OK")
                            self.send_header("Content-Type", "application/json")
                            self.send_header("Content-Length", str(len(body)))
                            self.end_headers()
                            self.wfile.write(body)
                    except Exception as e:
                        self.log('do_POST imports/renumber.json failed: %s'%(e), xbmc.LOGERROR)
                        self.send_error(500, "Failed to renumber")
                # POST /imports/orphans/remove.json — drop orphan channels (channels
                # in channels.json that are no longer in the source M3U) for one
                # import. Adds their ids to the import's `tombstones` list so they
                # don't get re-created on the next sync, and removes the records
                # from channels.json. Kicks chkImports to regen M3U/XML.
                elif self.path.split('?', 1)[0].lower() == '/imports/orphans/remove.json':
                    try:
                        target_id = incoming.get('id') or ''
                        if not target_id:
                            self.send_error(400, "id required")
                        else:
                            channels = Channels(writable=True)
                            ch_list  = list(channels.getChannels() or [])
                            imports  = list(channels.getImports() or [])
                            # Find the target import config
                            imp_cfg = next((i for i in imports if i.get('id') == target_id), None)
                            if imp_cfg is None:
                                self.send_error(404, "import id not found")
                                del channels
                            else:
                                # Identify orphans for this import.
                                orphan_ids = [c.get('id') for c in ch_list
                                              if c.get('import_source') == target_id
                                              and c.get('type') == 'import'
                                              and c.get('is_orphan') is True
                                              and c.get('id')]
                                removed = 0
                                if orphan_ids:
                                    # Tombstone the orphan ids so the next sync's
                                    # reconcile doesn't re-create them as 'new'.
                                    tomb = set(imp_cfg.get('tombstones') or [])
                                    tomb.update(orphan_ids)
                                    imp_cfg['tombstones'] = sorted(tomb)
                                    # Drop the orphans from channels.json.
                                    orphan_set  = set(orphan_ids)
                                    new_ch_list = [c for c in ch_list if c.get('id') not in orphan_set]
                                    removed = len(ch_list) - len(new_ch_list)
                                    # Persist both lists. setImports writes the
                                    # imports list (which includes the updated
                                    # tombstones); setChannels writes the channels.
                                    channels.setImports(imports)
                                    # Bypass setChannels' defensive merge — without
                                    # this, the merge re-adds the just-removed
                                    # orphan records as "disk-only type='import'"
                                    # in the same call, and the delete silently
                                    # reverts. Same pattern as cleanup_stranded.
                                    channels.channelDATA['channels'] = channels.sortChannels(new_ch_list)
                                    channels._save()
                                del channels
                                # Trigger sync so M3U/XML regen (orphan cleanup).
                                PROPERTIES.setEXTProperty('chkImports.kick', target_id)
                                DIALOG.notificationDialog(
                                    'Orphans removed: %d in [%s]' % (removed, target_id)
                                )
                                body = ('{"ok":true,"removed":%d}' % removed).encode('utf-8')
                                self.send_response(200, "OK")
                                self.send_header("Content-Type", "application/json")
                                self.send_header("Content-Length", str(len(body)))
                                self.end_headers()
                                self.wfile.write(body)
                    except Exception as e:
                        self.log('do_POST imports/orphans/remove.json failed: %s'%(e), xbmc.LOGERROR)
                        self.send_error(500, "Failed to remove orphans")
                # POST /channels/rebuild.json — flip `changed:True` on
                # PseudoTV-Custom channels so Builder picks them up
                # immediately. Two modes via the body:
                #
                # - Single channel: `{uuid, channel_id: "<id>"}` → flips
                #   only that channel. Rejected if id is an import.
                # - All Custom: `{uuid, all: true}` → flips every Custom
                #   channel (type != 'import'). Useful when programmes
                #   were lost across the board (e.g. earlier cleanSelf-strip
                #   bug wiped all Custom EPG).
                #
                # Either mode kicks `chkChanged` after the write so Builder
                # picks them up within ~3s. Imports' EPG isn't touched —
                # it comes from Imports.syncAll, not Builder.
                elif self.path.split('?', 1)[0].lower() == '/channels/rebuild.json':
                    try:
                        target_id  = incoming.get('channel_id') or ''
                        rebuild_all = bool(incoming.get('all'))
                        if not target_id and not rebuild_all:
                            self.send_error(400, "channel_id or all=true required")
                        else:
                            channels = Channels(writable=True)
                            ch_list = list(channels.getChannels() or [])
                            flipped = 0
                            if rebuild_all:
                                for c in ch_list:
                                    if c.get('type') != 'import':
                                        c['changed'] = True
                                        flipped += 1
                                if flipped == 0:
                                    del channels
                                    self.send_error(404, "no Custom channels found to rebuild")
                                    return
                            else:
                                found = False
                                for c in ch_list:
                                    if c.get('id') == target_id:
                                        if c.get('type') == 'import':
                                            self.send_error(400, "rebuild is for Custom channels only; this id is an import")
                                            del channels
                                            return
                                        c['changed'] = True
                                        found = True
                                        flipped = 1
                                        break
                                if not found:
                                    self.send_error(404, "channel_id not found")
                                    del channels
                                    return
                            # Direct channelDATA mutation + _save (same pattern
                            # as cleanup_stranded) to bypass setChannels'
                            # defensive merge which would re-read disk-only
                            # state and could miss the `changed` flag if disk
                            # already wrote False during the read.
                            channels.channelDATA['channels'] = channels.sortChannels(ch_list)
                            channels._save()
                            del channels
                            # Kick Builder via the same prop-timer mechanism
                            # /channels/add.json uses. chkChanged scans for
                            # `changed=True` and queues Builder per channel.
                            PROPERTIES.setPropTimer('chkChanged')
                            DIALOG.notificationDialog(
                                'Rebuild queued for %d channel%s — Builder running' % (
                                    flipped, '' if flipped == 1 else 's',
                                )
                            )
                            body = ('{"ok":true,"flipped":%d}' % flipped).encode('utf-8')
                            self.send_response(200, "OK")
                            self.send_header("Content-Type", "application/json")
                            self.send_header("Content-Length", str(len(body)))
                            self.end_headers()
                            self.wfile.write(body)
                    except Exception as e:
                        self.log('do_POST channels/rebuild.json failed: %s'%(e), xbmc.LOGERROR)
                        self.send_error(500, "Failed to queue rebuild")
                # POST /imports/cleanup_stranded.json — purge channels whose
                # `import_source` no longer matches any registered import. Happens
                # when an import is deleted from the UI but its channel records
                # stay in channels.json (they keep claiming numbers, confuse
                # renumber's blocked-slot calc, and pollute pseudotv.m3u/xml).
                # Also removes their EPG cache file if present. PseudoTV-Custom
                # (type != 'import') channels are NEVER touched — they have no
                # import_source. type='import' with a NULL import_source is
                # treated as stranded.
                elif self.path.split('?', 1)[0].lower() == '/imports/cleanup_stranded.json':
                    try:
                        channels = Channels(writable=True)
                        ch_list  = list(channels.getChannels() or [])
                        imports  = list(channels.getImports() or [])
                        active_ids = {i.get('id') for i in imports if i.get('id')}
                        stranded_ids = set()
                        kept = []
                        for c in ch_list:
                            if c.get('type') == 'import':
                                src = c.get('import_source')
                                if not src or src not in active_ids:
                                    stranded_ids.add(src or '<none>')
                                    continue
                            kept.append(c)
                        removed = len(ch_list) - len(kept)
                        if removed:
                            # IMPORTANT: bypass setChannels' defensive merge.
                            # That merge (channels.py:169-187) was added to protect
                            # against the Channel Manager UI accidentally wiping
                            # imports — it re-adds any disk-only `type='import'`
                            # records that aren't in the passed list. For an
                            # explicit cleanup, that's exactly what we DON'T want:
                            # the merge would re-add the stranded records we're
                            # trying to delete. Mutate channelDATA directly and
                            # call _save() to skip the merge entirely. Same
                            # pattern setImports uses for its in-place purge.
                            channels.channelDATA['channels'] = channels.sortChannels(kept)
                            channels._save()
                            # Clean up per-stranded-import EPG cache files
                            # (Step 5 fix #2 normally does this in setImports, but
                            # stranded-source channels don't trigger that path).
                            for src in stranded_ids:
                                if src == '<none>': continue
                                cache_file = os.path.join(CACHE_LOC, 'epg_%s.xml' % src)
                                real = FileAccess.translatePath(cache_file)
                                try:
                                    if os.path.exists(real):
                                        os.unlink(real)
                                        self.log('cleanup_stranded: removed orphan EPG cache %s'%(real))
                                except Exception as e:
                                    self.log('cleanup_stranded: failed to unlink %s: %s'%(real, e), xbmc.LOGWARNING)
                        del channels
                        # Kick a sync so M3U/XML get rewritten without the stranded
                        # entries (M3U.cleanSelf already drops them in memory but
                        # only the next syncAll force-flushes the right state).
                        PROPERTIES.setEXTProperty('chkImports.kick', 'all')
                        DIALOG.notificationDialog(
                            'Cleanup: removed %d channels from %d stranded import source%s' % (
                                removed, len(stranded_ids), '' if len(stranded_ids) == 1 else 's',
                            )
                        )
                        # Manual JSON for stranded_sources list (avoid pulling
                        # stdlib json in this hot path; server.py uses FileAccess
                        # elsewhere for JSON I/O).
                        stranded_json = '[' + ','.join(
                            '"%s"' % s.replace('\\', '\\\\').replace('"', '\\"')
                            for s in sorted(stranded_ids)
                        ) + ']'
                        body = ('{"ok":true,"removed":%d,"stranded_sources":%s}' % (
                            removed, stranded_json,
                        )).encode('utf-8')
                        self.send_response(200, "OK")
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                    except Exception as e:
                        self.log('do_POST imports/cleanup_stranded.json failed: %s'%(e), xbmc.LOGERROR)
                        self.send_error(500, "Failed to cleanup stranded channels")
                # POST /imports/refresh.json — trigger immediate sync (one or all)
                elif self.path.split('?', 1)[0].lower() == '/imports/refresh.json':
                    try:
                        target_id = incoming.get('id') or ''
                        # Set the property the chkImports daemon thread polls
                        # (tasks.py:_loop checks every KICK_POLL_SECS for this).
                        PROPERTIES.setEXTProperty('chkImports.kick', target_id or 'all')
                        DIALOG.notificationDialog('Refresh queued: %s'%(target_id or 'all imports'))
                        body = b'{"ok":true}'
                        self.send_response(200, "OK")
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                    except Exception as e:
                        self.log('do_POST imports/refresh.json failed: %s'%(e), xbmc.LOGERROR)
                        self.send_error(500, "Failed to queue refresh")
                #filelist w/resume - paused channel rule
                elif self.path.lower().startswith('/filelist') and incoming.get('payload'):
                    _fname = _safe_filename(self.path, '/filelist/')
                    if _fname is None:
                        self.log('do_POST /filelist/ rejected: bad filename in %s'%(self.path), xbmc.LOGWARNING)
                        self.send_error(400, "Bad filename")
                    else:
                        if FileAccess.setJSON(os.path.join(RESUME_LOC, _fname), incoming.get('payload')):
                            DIALOG.notificationDialog(LANGUAGE(30085)%(LANGUAGE(30060),incoming.get('name',ADDON_NAME)))
                        self.send_response(200, "OK")
                elif self.path.startswith(('/manager','/wizard')) and self.path.lower().endswith('form.html'):
                    try:
                        # Prefer a hypothetical SETTINGS.setPayload if it exists
                        if hasattr(SETTINGS, 'setPayload'):
                            SETTINGS.setPayload(incoming.get('payload'))
                        else:
                            # Fallback: write to resume location as remote_payload.json
                            FileAccess.setJSON(os.path.join(RESUME_LOC, 'remote_payload.json'), incoming.get('payload'))
                        DIALOG.notificationDialog(LANGUAGE(30085)%(LANGUAGE(30108),incoming.get('name',ADDON_NAME)))
                        self.send_response(200, "OK")
                    except Exception as e:
                        self.log('do_POST, failed to save remote payload: %s'%(e), xbmc.LOGERROR)
                        self.send_error(500, "Failed to save payload")
                else: self.send_error(404, "Path Not found")
            # _requireAuth already sent 401 + WWW-Authenticate on failure; no else needed
        else: return self.do_GET()
                    
    
    def do_GET(self):
        self.log('do_GET, incoming path = %s' % (self.path))
        def __sendChunk(path, chunk, compress=False):
            if compress:
                chunk = gzip.compress(chunk, compresslevel=5)
                self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", len(chunk))
            
            # Determine Content-Type - force content types to support IPTV-Simple, else guess.
            if   path.endswith('.json'): content_type = "application/json"
            elif path.endswith('.m3u'):  content_type = "application/vnd.apple.mpegurl"
            elif path.endswith('.xml'):  content_type = "text/plain"
            elif path.endswith('.html'): content_type = "text/html"
            else:                        content_type = mimetypes.guess_type(path[1:])[0]
            if content_type is None:     content_type = "application/octet-stream"
            self.send_header("Content-type", content_type)
            self.end_headers() # Finalize headers before sending body
            self.log('do_GET, __sendChunk [%s], path = %s, compress = %s'%(content_type, path, compress))
            self.wfile.write(chunk)

        def __sendFile(path, compress=False):
            with FileAccess.stream(path) as fle:
                __sendChunk(path, fle.readBytes(), compress)
            
        try:
            accept_encoding = self.headers.get("Accept-Encoding", "")
            use_compression = "gzip" in accept_encoding
            # madteevee (imports fork): auth gate. Public data-product paths
            # (M3U/XMLTV/genres/favicon) skip auth so pvr.iptvsimple on peer
            # Kodi boxes keeps reaching them. Everything else (dashboard HTML
            # + admin JSON APIs + /images/ + /logos/ + /api/ + /filelist/)
            # requires loopback or HTTP Basic Auth against Kodi's webserver
            # credentials. Must run BEFORE any send_response — 401 on a
            # response stream that already started 200 produces a malformed
            # reply (same constraint as the redirect-before-200 bug).
            _auth_path = self.path.split('?', 1)[0].lower()
            _PUBLIC_GET_PATHS = ('/favicon.ico', f'/{M3UFLE.lower()}',
                                 f'/{GENREFLE.lower()}', f'/{XMLTVFLE.lower()}')
            if _auth_path not in _PUBLIC_GET_PATHS and not self._requireAuth():
                return
            if self.path.startswith('/logos/'): # 302 Temporary Redirect
                self.send_response(302)
                self.send_header('Location', f'http://{PROPERTIES.getRemoteHost()}/images/{Globals._quoteString(self.resources.getCache(Globals._unquoteString(self.path.split("/logos/")[1])))}')
                self.log(f'do_GET, redirecting to http://{PROPERTIES.getRemoteHost()}/images/{Globals._quoteString(image)}')
                self.end_headers()
            # Consolidation (madteevee): channels.html + imports.html folded into
            # manager.html. Old URLs 302 to the appropriate tab fragment. Must
            # happen here (before the implicit send_response(200) below) — sending
            # a 302 AFTER a 200 has been written produces a malformed response.
            elif self.path.split('?', 1)[0].lower() in ('/imports.html', '/channels.html'):
                bare = self.path.split('?', 1)[0].lower()
                tab  = 'tab-imports' if bare == '/imports.html' else 'tab-channels'
                self.send_response(302, "Found")
                self.send_header("Location", "/manager.html#" + tab)
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif self.path.startswith('/images/'):
                # madteevee (imports fork): /images/ moved out of the generic
                # else-200 block so the path validator can short-circuit with
                # 400/403 without conflicting with an already-sent 200.
                # Behavior: filesystem reads are confined to a small set of
                # known roots (LOGO_LOC = user logos, CACHE_LOC = cached
                # downloaded logos, MEDIA_LOC = addon's bundled banner / icons).
                # Both relative and absolute paths are validated — anything
                # outside the allowed roots is a 403. Kodi VFS schemes
                # (special://, image://, http(s)://) bypass filesystem
                # validation; Kodi resolves them via xbmcvfs.
                img_path = Globals._unquoteString(self.path.split('/images/')[1])
                if '\x00' in img_path:
                    return self.send_error(400, "Bad Request")
                if '://' in img_path:
                    self.send_response(200)
                    __sendFile(img_path, False)
                else:
                    _allowed_roots = [os.path.abspath(LOGO_LOC),
                                      os.path.abspath(CACHE_LOC),
                                      os.path.abspath(MEDIA_LOC)]
                    if img_path.startswith('/'):
                        candidate = os.path.abspath(img_path)
                    else:
                        candidate = os.path.abspath(os.path.join(_allowed_roots[0], img_path))
                    if not any(candidate == root or candidate.startswith(root + os.sep) for root in _allowed_roots):
                        self.log('do_GET, /images/ path escape rejected: %s -> %s'%(img_path, candidate), xbmc.LOGWARNING)
                        return self.send_error(403, "Forbidden")
                    self.send_response(200)
                    __sendFile(candidate, False)
            else: # 200 OK
                self.send_response(200)
                if   self.path.lower() == '/favicon.ico':           __sendFile(os.path.join(MEDIA_LOC,'logo.ico'), use_compression)
                elif self.path.lower() == f'/{M3UFLE.lower()}':     __sendFile(M3UFLEPATH, use_compression)
                elif self.path.lower() == f'/{GENREFLE.lower()}':   __sendFile(GENREFLEPATH, use_compression)
                elif self.path.lower() == f'/{XMLTVFLE.lower()}':   __sendFile(XMLTVFLEPATH, use_compression)
                else:
                    # imports fork (madteevee): strip query string before extension checks.
                    # Otherwise `/imports.json?_=12345` (browser cache-buster) fails the
                    # outer `.endswith('.json')` gate and 404s.
                    bare_path = self.path.split('?', 1)[0]
                    use_compression = False if bare_path.endswith('.html') else use_compression
                    if bare_path.lower().endswith('.json'):
                        if   self.path.lower() == f'/api/{BONJOURFLE.lower()}': __sendChunk(self.path, FileAccess.dumpJSON(SETTINGS.getBonjour(),idnt=4).encode(encoding=DEFAULT_ENCODING), use_compression)
                        elif self.path.lower() == f'/api/{DBLOGSFLE.lower()}':  __sendChunk(self.path, FileAccess.dumpJSON(SETTINGS.cache.get('log.%s'%(ADDON_ID),checksum=ADDON_VERSION),idnt=4).encode(encoding=DEFAULT_ENCODING), use_compression)
                        # imports fork (madteevee): GET /imports.json — current imports list as JSON.
                        # Includes a slim per-channel list (id, number, name, enabled, type,
                        # import_source, is_orphan) so the UI can render imported AND
                        # PseudoTV-built channels in the same view, each with a disable
                        # toggle + number editor.
                        elif bare_path.lower() == '/imports.json':
                            channels = Channels(writable=False)
                            ch_list = channels.getChannels() or []
                            slim_channels = []
                            for c in ch_list:
                                # logo + group + path + catchup + radio + favorite
                                # added so the consolidated dashboard's modals
                                # (Custom-edit + imports-logo) can pre-fill from disk.
                                slim_channels.append({
                                    'id'              : c.get('id'),
                                    'name'            : c.get('name'),
                                    'number'          : c.get('number'),
                                    'enabled'         : c.get('enabled', True),
                                    'is_orphan'       : c.get('is_orphan', False),
                                    'type'            : c.get('type', ''),
                                    'import_source'   : c.get('import_source'),
                                    'operator_overrides': c.get('operator_overrides') or [],
                                    'logo'            : c.get('logo'),
                                    'group'           : c.get('group'),
                                    'path'            : c.get('path'),
                                    'catchup'         : c.get('catchup'),
                                    'radio'           : c.get('radio'),
                                    'favorite'        : c.get('favorite'),
                                })
                            payload = {
                                'uuid'     : SETTINGS.getMYUUID(),
                                'imports'  : channels.getImports() or [],
                                'channels' : slim_channels,
                            }
                            del channels
                            __sendChunk(self.path, FileAccess.dumpJSON(payload, idnt=4).encode(encoding=DEFAULT_ENCODING), use_compression)
                        # imports fork (madteevee) — Step 6 Phase 1: GET /api/settings/schema.json
                        # settings.xml + strings.po parsed into a JSON tree, enriched with
                        # current values + pre-resolved InfoBool conditions. Consumed by the
                        # Settings tab on manager.html.
                        elif bare_path.lower() == '/api/settings/schema.json':
                            # 5s mem-cache keyed by (xml_mtime, po_mtime). Cold cost
                            # ~1.1s; warm cost ~30ms (just JSON serialize). Invalidated
                            # by /api/settings/save.json on writes.
                            try:    _smt = os.path.getmtime(SETTINGS_XML_PATH)
                            except OSError: _smt = 0
                            try:    _pmt = os.path.getmtime(STRINGS_PO_PATH)
                            except OSError: _pmt = 0
                            _key  = (_smt, _pmt)
                            _now  = time.time()
                            if (SCHEMA_RESPONSE_CACHE['v'] is not None
                                    and SCHEMA_RESPONSE_CACHE['key'] == _key
                                    and (_now - SCHEMA_RESPONSE_CACHE['t']) < 5):
                                schema = SCHEMA_RESPONSE_CACHE['v']
                            else:
                                try:
                                    from jsonrpc        import JSONRPC
                                    from settings_schema import get_schema
                                    # xbmcaddon.Addon() passed directly to avoid the SETTINGS
                                    # wrapper's per-call debug log (~1s of overhead on Pi5).
                                    schema = get_schema(SETTINGS_XML_PATH, STRINGS_PO_PATH,
                                                        settings_reader=xbmcaddon.Addon(ADDON_ID),
                                                        infobool_resolver=JSONRPC().getInfoBooleans)
                                    SCHEMA_RESPONSE_CACHE['key'] = _key
                                    SCHEMA_RESPONSE_CACHE['t']   = _now
                                    SCHEMA_RESPONSE_CACHE['v']   = schema
                                except Exception as e:
                                    self.log('settings/schema.json, failed! %s' % e, xbmc.LOGERROR)
                                    schema = {'section_id': '', 'categories': [], 'error': str(e)}
                            __sendChunk(self.path, FileAccess.dumpJSON(schema, idnt=2).encode(encoding=DEFAULT_ENCODING), use_compression)
                        # Step 6 Phase 5 (madteevee): GET /api/system/info.json
                        # Live system stats for the manager.html System Info tab.
                        # Single batched InfoBool call for the boolean-ish fields.
                        elif bare_path.lower() == '/api/system/info.json':
                            # 35s on-demand cache. Slightly longer than the dashboard's
                            # 30s polling interval, so an active viewer always hits a
                            # warm cache after the initial cold call. Zero background
                            # cost: no daemon thread.
                            _now = time.time()
                            if (SYSINFO_CACHE['v'] is not None
                                    and (_now - SYSINFO_CACHE['t']) < 35):
                                info = dict(SYSINFO_CACHE['v'])
                                info['uptime_seconds'] = int(_now - SERVER_STARTED_AT)
                            else:
                                try:
                                    info = _build_sysinfo_payload()
                                    SYSINFO_CACHE['v'] = info
                                    SYSINFO_CACHE['t'] = _now
                                except Exception as e:
                                    self.log('system/info.json, failed! %s' % e, xbmc.LOGERROR)
                                    info = {'error': str(e)}
                            __sendChunk(self.path, FileAccess.dumpJSON(info, idnt=2).encode(encoding=DEFAULT_ENCODING), use_compression)
                        # Step 6 Phase 7 (madteevee): GET /api/epg/grid.json?window=4h&start=<unix>
                        # Streams pseudotv.xml with iterparse and filters to a time window.
                        # Returns {channels: [...], programmes: [...], window: {start, end}}.
                        elif bare_path.lower() == '/api/epg/grid.json':
                            try:
                                import xml.etree.ElementTree as _ET
                                import re as _re
                                from urllib.parse import parse_qs, urlparse
                                qs = parse_qs(urlparse(self.path).query)
                                now    = int(time.time())
                                window_h = int((qs.get('window', ['4'])[0] or '4').replace('h','')) or 4
                                # Round start to a 60s bucket so consecutive requests share
                                # the cache (clients hit /api/epg/grid.json without `start=`,
                                # which would otherwise produce a fresh `now` per call →
                                # 100% cache miss). 60s bucket = grid drifts at most a minute.
                                if qs.get('start'):
                                    start_ts = int(qs.get('start', [str(now)])[0])
                                else:
                                    start_ts = (now // 60) * 60
                                end_ts   = start_ts + window_h * 3600
                                # Cache by (xmltv_mtime, start_ts, window_h). Re-parse only
                                # when the file changes or window moves.
                                _xml_real = FileAccess.translatePath(XMLTVFLEPATH)
                                try:    _xmtime = int(os.path.getmtime(_xml_real))
                                except OSError: _xmtime = 0
                                _ck = (_xmtime, start_ts, window_h)
                                _hit = EPG_CACHE.get(_ck)
                                if _hit and (now - _hit['t']) < 120:
                                    grid_cached = dict(_hit['v'])
                                    grid_cached['window'] = dict(grid_cached['window'])
                                    grid_cached['window']['now'] = now
                                    __sendChunk(self.path, FileAccess.dumpJSON(grid_cached).encode(encoding=DEFAULT_ENCODING), use_compression)
                                    return
                                # XMLTV time format: 'YYYYMMDDHHMMSS +0000' or 'YYYYMMDDHHMMSS'.
                                # Per XMLTV spec section 2.1: timestamps WITHOUT a timezone
                                # suffix are interpreted as LOCAL time (of the source). Our
                                # pseudotv.xml inherits this — movistarplus delivers Spanish
                                # local time (CEST/CET) without a suffix. Parsing as UTC
                                # would shift the whole grid by 2 hours, causing the EPG
                                # to disagree with what's actually airing on TV.
                                def _xmltv_to_ts(s):
                                    if not s: return 0
                                    s = s.strip()
                                    parts = s.split(' ', 1)
                                    base = parts[0]
                                    tz_suffix = parts[1].strip() if len(parts) > 1 else ''
                                    if len(base) < 14: return 0
                                    try:
                                        st = time.strptime(base[:14], '%Y%m%d%H%M%S')
                                        if tz_suffix and len(tz_suffix) >= 5 and tz_suffix[0] in '+-':
                                            import calendar as _cal
                                            sign = 1 if tz_suffix[0] == '+' else -1
                                            hours = int(tz_suffix[1:3])
                                            mins  = int(tz_suffix[3:5])
                                            offset_sec = sign * (hours * 3600 + mins * 60)
                                            return _cal.timegm(st) - offset_sec
                                        # No suffix → local time of the addon's environment.
                                        return int(time.mktime(st))
                                    except Exception:
                                        return 0
                                # Step 6 Phase 7 polish (madteevee): EPG titles/descs from some
                                # providers embed Kodi BBcode markup ([COLOR tomato]T8[/COLOR],
                                # [B]bold[/B], [LIGHT]...[/LIGHT], etc). Some providers (movistar+)
                                # stuff the entire EPG item-id payload as [COLOR item="<base64>..."]
                                # which is useless for display. Strip both shapes: the bracket
                                # pairs preserve inner text where present, and unmatched openers
                                # like [COLOR item="..."] (no closing tag) collapse to nothing.
                                _KODI_MARKUP_RE = _re.compile(r'\[/?[A-Z][^\]]*\]')
                                def _strip_kodi_markup(s):
                                    if not s: return s
                                    return _KODI_MARKUP_RE.sub('', s).strip()
                                # Index channels.json for name/number/logo lookup.
                                ch = Channels(writable=False)
                                ch_by_id = {c.get('id'): c for c in (ch.getChannels() or [])}
                                del ch
                                programmes = []
                                channels_used = {}
                                try:
                                    # XMLTVFLEPATH is a Kodi VFS path (special://...).
                                    # iterparse needs a real filesystem path.
                                    xml_real = FileAccess.translatePath(XMLTVFLEPATH)
                                    # iterparse fires end-events bottom-up. Clearing inner
                                    # elements (<title>, <desc>) as they're encountered loses
                                    # the text before the parent <programme> end-event fires.
                                    # Skip non-programme tags WITHOUT clearing; clear only
                                    # at the programme level once we've extracted its data.
                                    for _, elem in _ET.iterparse(xml_real, events=('end',)):
                                        if elem.tag != 'programme':
                                            continue
                                        s = _xmltv_to_ts(elem.get('start'))
                                        e = _xmltv_to_ts(elem.get('stop'))
                                        if e < start_ts or s > end_ts:
                                            elem.clear(); continue
                                        cid = elem.get('channel')
                                        title_el = elem.find('title')
                                        desc_el  = elem.find('desc')
                                        programmes.append({
                                            'channel_id': cid,
                                            'start'     : s,
                                            'stop'      : e,
                                            'title'     : _strip_kodi_markup((title_el.text if title_el is not None else '') or ''),
                                            'desc'      : _strip_kodi_markup((desc_el.text  if desc_el  is not None else '') or ''),
                                        })
                                        if cid and cid not in channels_used:
                                            meta = ch_by_id.get(cid) or {}
                                            channels_used[cid] = {
                                                'id'    : cid,
                                                'name'  : meta.get('name')   or cid,
                                                'number': meta.get('number'),
                                                'logo'  : meta.get('logo'),
                                            }
                                        elem.clear()
                                except FileNotFoundError:
                                    pass
                                channels_out = sorted(channels_used.values(), key=lambda c: (c.get('number') or 9999, c.get('name','')))
                                programmes.sort(key=lambda p: (p['channel_id'], p['start']))
                                grid = {'window': {'start': start_ts, 'end': end_ts, 'now': now},
                                        'channels': channels_out,
                                        'programmes': programmes}
                                EPG_CACHE[_ck] = {'t': now, 'v': grid}
                                # Bound the cache: keep only the 12 most recently inserted entries.
                                if len(EPG_CACHE) > 12:
                                    for old_k in sorted(EPG_CACHE.keys(), key=lambda k: EPG_CACHE[k]['t'])[:-12]:
                                        del EPG_CACHE[old_k]
                            except Exception as e:
                                self.log('epg/grid.json, failed! %s' % e, xbmc.LOGERROR)
                                grid = {'error': str(e), 'channels': [], 'programmes': []}
                            __sendChunk(self.path, FileAccess.dumpJSON(grid).encode(encoding=DEFAULT_ENCODING), use_compression)
                        # Step 6 Phase 8 (madteevee): GET /api/browse.json?path=<vfs-path>
                        # Path-browser modal for `type=path` settings. Wraps
                        # JSONRPC.getDirectory so the front-end can walk
                        # special:// trees, smb://, etc. without per-call JSON-RPC.
                        elif bare_path.lower() == '/api/browse.json':
                            try:
                                from urllib.parse import parse_qs, urlparse
                                from kodi_six     import xbmcvfs
                                qs   = parse_qs(urlparse(self.path).query)
                                vpath = (qs.get('path', ['special://home/'])[0] or 'special://home/')
                                if not vpath.endswith('/'): vpath = vpath + '/'
                                # Kodi's Files.GetDirectory JSON-RPC only enumerates configured
                                # sources. xbmcvfs.listdir returns (dirs, files) for any path
                                # the addon process can read, including special://-prefixed
                                # paths. For resource:// resource packs the per-resource
                                # CONTENTS sometimes come back in the `dirs` slot — reclassify
                                # entries with file extensions to the `files` bucket.
                                _FILE_EXTS = ('.png', '.jpg', '.jpeg', '.gif', '.svg', '.bmp',
                                              '.xml', '.xsp', '.m3u', '.m3u8', '.json',
                                              '.txt', '.log', '.db', '.gz', '.zip',
                                              '.mp4', '.mkv', '.avi', '.mp3')
                                try:
                                    dirs, files = xbmcvfs.listdir(vpath)
                                except Exception:
                                    dirs, files = [], []
                                slim = []
                                for d in (dirs or []):
                                    lower = d.lower()
                                    if any(lower.endswith(ext) for ext in _FILE_EXTS):
                                        slim.append({'label': d, 'file': vpath + d, 'filetype': 'file'})
                                    else:
                                        slim.append({'label': d, 'file': vpath + d + '/', 'filetype': 'directory'})
                                for f in (files or []):
                                    slim.append({'label': f, 'file': vpath + f, 'filetype': 'file'})
                                # Sort: directories first (alphabetical, case-insensitive),
                                # then files (alphabetical, case-insensitive).
                                slim.sort(key=lambda it: (0 if it['filetype'] == 'directory' else 1,
                                                          (it['label'] or '').lower()))
                                parent = ''
                                try:
                                    stripped = vpath.rstrip('/')
                                    if '://' in stripped:
                                        scheme, rest = stripped.split('://', 1)
                                        if '/' in rest:
                                            parent = '%s://%s/' % (scheme, rest.rsplit('/', 1)[0])
                                except Exception:
                                    pass
                                resp = {'path': vpath, 'parent': parent, 'items': slim}
                            except Exception as e:
                                self.log('browse.json, failed! %s' % e, xbmc.LOGERROR)
                                resp = {'error': str(e), 'items': []}
                            __sendChunk(self.path, FileAccess.dumpJSON(resp).encode(encoding=DEFAULT_ENCODING), use_compression)
                        # Step 6 Phase 11 (madteevee): GET /api/channels/rules/catalog.json
                        # Metadata for all 24 rule classes. Derived from
                        # RulesList().allRules() at request time so upstream rule
                        # additions auto-surface. Type inference walks defaults +
                        # selectBoxOptions presence.
                        elif bare_path.lower() == '/api/channels/rules/catalog.json':
                            try:
                                from rules import RulesList
                                def _infer_type(default, sbo):
                                    # Order matters: selectBoxOptions wins if present + non-empty.
                                    if isinstance(sbo, (list, tuple)) and sbo:
                                        return 'select'
                                    if isinstance(sbo, dict) and sbo:
                                        return 'select'  # madteevee: dict-form select (label->value)
                                    if isinstance(default, bool):
                                        return 'bool'
                                    if isinstance(default, int):
                                        return 'int'
                                    return 'string'
                                catalog = []
                                for r in RulesList().allRules():
                                    # Some upstream rule classes (e.g. MST3k) only set the
                                    # attributes they actually use and don't call super.__init__,
                                    # so selectBoxOptions / optionDescriptions / etc. may be
                                    # absent. Guard every attribute read with a safe default.
                                    labels = getattr(r, 'optionLabels',       []) or []
                                    values = getattr(r, 'optionValues',       []) or []
                                    descs  = getattr(r, 'optionDescriptions', []) or []
                                    sbox   = getattr(r, 'selectBoxOptions',   []) or []
                                    n = max(len(labels), len(values), len(descs))
                                    options = []
                                    for i in range(n):
                                        default = values[i] if i < len(values) else None
                                        sbo     = sbox[i]   if i < len(sbox)   else None
                                        # madteevee: serialize selectBoxOptions in both list and dict forms.
                                        # Dict form is {display_label: stored_value} — used by ForceSubtitles
                                        # for human-friendly dropdowns (English / English code "en", etc.).
                                        # Two upstream rules (channel-rotation family + one other) used
                                        # dict-form selectBoxOptions but the previous list-only serializer
                                        # dropped them, falling back to plain text input in the UI.
                                        if isinstance(sbo, dict) and sbo:
                                            select_box = {
                                                'kind' : 'dict',
                                                'items': [{'label': str(k), 'value': v} for k, v in sbo.items()],
                                            }
                                        elif isinstance(sbo, (list, tuple)) and sbo:
                                            select_box = list(sbo)
                                        else:
                                            select_box = None
                                        options.append({
                                            'idx'         : i,
                                            'label'       : labels[i] if i < len(labels) else '',
                                            'description' : descs[i]  if i < len(descs)  else '',
                                            'default'     : default,
                                            'type'        : _infer_type(default, sbo),
                                            'selectBox'   : select_box,
                                        })
                                    catalog.append({
                                        'id'          : getattr(r, 'myId', None),
                                        'name'        : getattr(r, 'name', '') or '',
                                        'description' : getattr(r, 'description', '') or '',
                                        'options'     : options,
                                    })
                                resp = {'rules': catalog}
                            except Exception as e:
                                self.log('channels/rules/catalog.json, failed! %s' % e, xbmc.LOGERROR)
                                resp = {'error': str(e), 'rules': []}
                            __sendChunk(self.path, FileAccess.dumpJSON(resp).encode(encoding=DEFAULT_ENCODING), use_compression)
                        # Step 6 Phase 11 (madteevee): GET /api/channels/rules.json?channel_id=<cid>
                        # Returns the persisted rules dict for ONE channel.
                        elif bare_path.lower() == '/api/channels/rules.json':
                            try:
                                from urllib.parse import parse_qs, urlparse
                                qs  = parse_qs(urlparse(self.path).query)
                                cid = (qs.get('channel_id', [''])[0] or '').strip()
                                if not cid:
                                    resp = {'error': 'channel_id query param required', 'rules': {}}
                                else:
                                    ch = Channels(writable=False)
                                    target = next((c for c in (ch.getChannels() or []) if c.get('id') == cid), None)
                                    del ch
                                    if target is None:
                                        resp = {'error': 'channel_id not found', 'rules': {}}
                                    else:
                                        # Normalize string keys (json-load returns them as str).
                                        raw = target.get('rules') or {}
                                        normalized = {}
                                        for k, v in raw.items():
                                            try: normalized[int(k)] = v
                                            except (TypeError, ValueError): normalized[k] = v
                                        resp = {'channel_id': cid, 'name': target.get('name'), 'number': target.get('number'), 'rules': normalized}
                            except Exception as e:
                                self.log('channels/rules.json, failed! %s' % e, xbmc.LOGERROR)
                                resp = {'error': str(e), 'rules': {}}
                            __sendChunk(self.path, FileAccess.dumpJSON(resp).encode(encoding=DEFAULT_ENCODING), use_compression)
                        elif self.path.lower().startswith('/api'):
                            # madteevee (imports fork): filename allowlist —
                            # `..`, slashes, or null bytes in the request path
                            # used to walk arbitrarily off USER_LOC. Reject
                            # silently with `{}` (no auth/path-shape leaked).
                            _fname = _safe_filename(self.path, '/api/')
                            if _fname is None:
                                self.log('do_GET /api/ rejected: bad filename in %s'%(self.path), xbmc.LOGWARNING)
                                _payload = {}
                            else:
                                _payload = FileAccess.getJSON(os.path.join(USER_LOC, _fname))
                            __sendChunk(self.path, FileAccess.dumpJSON(_payload).encode(encoding=DEFAULT_ENCODING), use_compression)
                        elif self.path.lower().startswith('/filelist'):
                            _fname = _safe_filename(self.path, '/filelist/')
                            if _fname is None:
                                self.log('do_GET /filelist/ rejected: bad filename in %s'%(self.path), xbmc.LOGWARNING)
                                _payload = {}
                            else:
                                _payload = FileAccess.getJSON(os.path.join(RESUME_LOC, _fname))
                            __sendChunk(self.path, FileAccess.dumpJSON(_payload).encode(encoding=DEFAULT_ENCODING), use_compression)
                    elif bare_path.lower().endswith('.html'):
                        if bare_path.lower() == f'/{MANAGERFLE.lower()}':       __sendChunk(self.path, Channels()._channelManager(), use_compression)
                        # /imports.html and /channels.html are handled by a 302 redirect
                        # at the top of the try-block (before send_response(200)), so they
                        # never reach this html-handler branch.
                    else: return self.send_error(404, "File Not Found [%s]" % self.path)
        except FileNotFoundError: self.send_error(404, "File Not Found [%s]" % self.path)
        except Exception as e: self.send_error(500, "Internal Server Error")


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    
    
class HTTP(Thread):
    httpd = None
    
    def __init__(self, service=None):
        Thread.__init__(self)
        self.service = service
        self.monitor = service.monitor
        self.start()
        
                    
    def log(self, msg, level=xbmc.LOGDEBUG):
        return log('%s: %s'%(self.__class__.__name__,msg),level)


    def _chkPort(self, host, port=SETTINGS.getSettingInt('TCP_PORT')):
        def __isAvailable(host, tmpPort):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                try:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    s.bind((host, tmpPort))
                    s.close()
                    return True
                except socket.error as e:
                    if e.errno == errno.EADDRINUSE: return False
                    raise e

        # madteevee (imports fork): retry the operator-configured port a
        # few times before falling back to an incremented one. After a
        # Kodi restart, the previous addon process's HTTP socket on this
        # port often takes a few seconds to release (TIME_WAIT, async
        # teardown by Kodi's threading layer). The original loop
        # incremented on the first failed bind, so every restart shifted
        # the addon to port 50003+ and toasted "port 50002 is in use,
        # port 50003 is available" — confusing operators who never asked
        # for a port change and breaking links / bookmarks pointing at
        # the configured port. With SO_REUSEADDR set (line 1143) the
        # bind succeeds AS SOON AS the previous socket transitions out
        # of LISTEN. Polling the desired port for ~5 s catches almost
        # every restart cleanly. If it's STILL not free after that
        # (genuine port conflict with another process), fall through
        # to the original increment logic.
        DESIRED_PORT_RETRIES = 10   # × 0.5 s = 5 s of waiting for the desired port
        retries = 0
        tmpPort = port
        while not self.monitor.abortRequested() and not __isAvailable(host, tmpPort):
            if self.service._shutdown(0.5): break
            if tmpPort == port and retries < DESIRED_PORT_RETRIES:
                retries += 1
                self.log(f"_chkPort {tmpPort} in use, retry {retries}/{DESIRED_PORT_RETRIES} (waiting for previous socket release)")
                continue
            self.log(f"_chkPort {tmpPort} is in use. Trying next port.")
            tmpPort += 1
        if tmpPort != port: DIALOG.notificationDialog(LANGUAGE(30097)%(port,tmpPort))
        self.log("_chkPort, port available = %s (waited %d retries)"%(tmpPort, retries))
        return tmpPort
        

    def _pendingRestart(self):
        value = PROPERTIES.getEXTProperty('%s.HTTP.pendingRestart'%(ADDON_ID),False)
        PROPERTIES.clrEXTProperty('%s.HTTP.pendingRestart'%(ADDON_ID))
        return value
       

    def run(self):  
        def __update(silent=None):
            if silent is None: silent = BUILTIN.isPlaying()
            isRunning = PROPERTIES.isRunning('HTTP.run')
            if not silent: DIALOG.notificationDialog('%s: %s'%(SETTINGS.getSetting('Remote_NAME'),LANGUAGE(32211)%({True:'green',False:'red'}[isRunning],{True:LANGUAGE(32158),False:LANGUAGE(32253)}[isRunning])))
            SETTINGS.setSetting('Remote_Status',LANGUAGE(32211)%({True:'green',False:'red'}[isRunning],{True:LANGUAGE(32158),False:LANGUAGE(32253)}[isRunning]))

        def __cancel(wait=15):
            try:
                if self.httpd.is_alive():
                    if hasattr(self.httpd, 'cancel'): self.httpd.cancel()
                    try: self.httpd.join(wait)
                    except Exception: pass
                return self.httpd.is_alive()
            except Exception: pass
            
        """Starts the threaded HTTP server with GZIP support."""
        if not PROPERTIES.isRunning('HTTP.start'):
            PROPERTIES.setRunning('HTTP.start',True)
            while not self.monitor.abortRequested():
                pendingRestart = self._pendingRestart()
                if not PROPERTIES.isRunning('HTTP.run'):
                    PROPERTIES.setRunning('HTTP.run',True)
                    try:
                        host   = SETTINGS.getIP()
                        port   = self._chkPort(host, SETTINGS.getSettingInt('TCP_PORT'))
                        server = PROPERTIES.setRemoteHost('%s:%s'%(host,port))
                        SETTINGS.setSetting('Remote_NAME' ,PROPERTIES.getFriendlyName())
                        SETTINGS.setSetting('Remote_M3U'  ,'http://%s/%s'%(server,M3UFLE))
                        SETTINGS.setSetting('Remote_XMLTV','http://%s/%s'%(server,XMLTVFLE))
                        SETTINGS.setSetting('Remote_GENRE','http://%s/%s'%(server,GENREFLE))
                        self.log("run, http server @ %s (bind=0.0.0.0)"%(server),xbmc.LOGINFO)

                        # madteevee (imports fork): bind to 0.0.0.0 so both
                        # 127.0.0.1 (loopback bypass for browsers on the Kodi
                        # box) and the LAN IP (peer Kodi, LAN dashboard with
                        # Basic Auth) reach the server. Previously bound to
                        # SETTINGS.getIP() alone, which made 127.0.0.1
                        # unreachable. URL building (Remote_M3U, getRemoteHost)
                        # still uses the LAN IP so existing pvr.iptvsimple /
                        # multiroom URLs are unchanged.
                        ThreadedHTTPServer.allow_reuse_address = True
                        self._server = ThreadedHTTPServer(('0.0.0.0', port), partial(MyHandler,service=self.service))
                        try: self._server.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                        except Exception as e: self.log("run, http server failed to set SO_REUSEADDR: %s" % e, xbmc.LOGWARNING)

                        self.httpd = Thread(target=self._server.serve_forever)
                        self.httpd.daemon=True
                        self.httpd.start()
                        __update(pendingRestart)
                    except Exception as e:
                        self.log("run, http server failed! %s"%(e), xbmc.LOGERROR)
                        break
                elif self.service._shutdown(15) or pendingRestart: 
                    self.log("run, _shutdown/pendingRestart", xbmc.LOGERROR)
                    break
                    
            try: self._server.shutdown()
            except Exception: pass
            # imports.17: release the listening socket. shutdown() above only stops
            # serve_forever; the BaseServer.socket stays in LISTEN state until
            # server_close() is called, holding the port (typically 50002).
            # Without this, every chkPVRRefresh→pendingRestart cycle leaked the
            # old socket; the next _chkPort(50002) bind failed (SO_REUSEADDR
            # doesn't override LISTEN), retried 10x over 5s, fell back to 50003,
            # and DIALOG.notificationDialog fired the "port in use, next port
            # available" toast on every refresh. Closes the socket cleanly so
            # the next bind picks 50002 again.
            try: self._server.server_close()
            except Exception: pass
            self.log('run, http server shutdown, pendingRestart = %s, isAlive = %s'%(pendingRestart,__cancel()), xbmc.LOGINFO)
            if pendingRestart: timerit(self.service._que)(M3U_REFRESH,*(self.service.tasks.chkHTTP,1))
            __update(pendingRestart)
            PROPERTIES.setRunning('HTTP.run',False)
            PROPERTIES.setRunning('HTTP.start',False)