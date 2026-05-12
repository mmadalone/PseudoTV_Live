#   Copyright (C) 2025 Lunatixz
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

# -*- coding: utf-8 -*-

# madteevee (imports fork, imports.13 / C#10 Follow-up A):
# Pure-function renderers for pseudotv.m3u / pseudotv.xml + atomic-write
# helper under WRITER_LOCK. Consolidates the M3U/XMLTV writer that used
# to live in two places (tasks.py + m3u.py / xmltvs.py). Plan:
# /home/madalone/.claude/plans/misty-shimmying-liskov.md.
#
# Module-level (not class methods) so M3U._save and XMLTVS._save can
# call into them without a service/Tasks reference — necessary because
# context_record.py constructs M3U/XMLTVS standalone, and because
# tasks.py already imports XMLTVS (circular import would block any
# m3u.py/xmltvs.py → tasks.py reference). Same load-order constraint
# that drove writer_lock.py to be its own module.
#
# The functions are intentionally PURE byte-emitters: they don't sort,
# they don't run cleanChannels, they don't orphan-prune. Callers (M3U.
# _saveLocked, XMLTVS._saveLocked) do the pre-processing class-side
# so chkImports / Imports.syncAll byte output stays byte-identical to
# pre-consolidation behavior (those paths don't sort/prune today).

import io
import os
import re

from globals     import *
from fileaccess  import FileAccess
from writer_lock import held, WriterTimeout


def _log(msg, level=xbmc.LOGDEBUG):
    return log('renderers: %s' % (msg,), level)


# madteevee (imports fork): defensive escape for M3U attribute values.
# Three characters break M3U parsing inside an attribute = "..." pair:
#   "  closes the attribute, leaking the rest of the value into the
#      next attribute slot.
#   \r / \n end the EXTINF line, truncating the record and (worse)
#      letting any following content parse as a new directive.
# Operator-typed strings (catchup_query_format), imported channel names,
# upstream tvg-logo URLs — all reach this writer. Percent-encode so the
# downstream pvr.iptvsimple parser sees a well-formed attribute value.
def _m3u_attr_escape(v):
    if v is None: return ''
    s = str(v)
    return s.replace('"', '%22').replace('\r', '%0D').replace('\n', '%0A')


def _m3u_label_sanitize(v):
    # Display label sits after the EXTINF comma; only newlines truncate.
    if v is None: return ''
    return str(v).replace('\r', ' ').replace('\n', ' ')


def render_m3u(m3udata):
    """Build M3U file content from in-memory M3UDATA. Pure function.

    Returns rendered str (UTF-8 text), or None for empty input.
    Caller skips the write on None to preserve disk content
    (refuse-empty guard). Symmetric with render_xmltv.

    Iterates stations + recordings combined-sorted by `number` —
    recordings have float numbers < 1 (per m3u.py: addRecording
    assigns `random.Random(...).random()`) so they cluster at file
    head before integer-numbered stations.
    """
    # Refuse-empty guard: header-only output (no stations / no recordings)
    # causes pvr.iptvsimple to load 0 channels and wipes Kodi PVR EPG.
    if (not (m3udata.get('stations') or [])
        and not (m3udata.get('recordings') or [])):
        _log('render_m3u, REFUSING to render empty M3UDATA — would wipe disk M3U (0 channels).', xbmc.LOGWARNING)
        return None
    # Header line
    header = '#EXTM3U tvg-shift="" x-tvg-url="http://%s/%s" x-tvg-id="" catchup-correction=""\n' % (
        PROPERTIES.getRemoteHost(), XMLTVFLE,
    )
    out = [header]

    # M3U_TEMP keys decide which optional attrs get emitted.
    # Local import: avoid top-level circular with m3u.py (m3u.py
    # imports renderers for its _saveLocked; m3u.py is fully loaded
    # by the time render_m3u is first called).
    from m3u import M3U_TEMP
    opts = list(M3U_TEMP.keys())
    # madteevee (imports fork): the `channel-id="..."` attribute is
    # pvr.iptvsimple's official override for the auto-generated unique
    # channel id (per pvr.iptvsimple README, Omega+). Without it,
    # IPTV Simple hashes `name+streamUrl` — two channels with the same
    # name + same source URL (e.g. movistarplus + autonomiques both
    # streaming `Al Jazeera` from `plugin.video.movistarplus`) collide
    # to the same ChannelId, and Kodi PVR's channel DB dedupes to a
    # single entry. Forcing channel-id to our namespaced @-suffix id
    # guarantees uniqueness across imports.
    line_template = '#EXTINF:-1 channel-id="%s" tvg-chno="%s" tvg-id="%s" tvg-name="%s" tvg-logo="%s" group-title="%s" radio="%s" catchup="%s" %s,%s\n'

    stations = sorted(
        list(m3udata.get('recordings') or []) + list(m3udata.get('stations') or []),
        key=lambda s: s.get('number', 0),
    )
    for station in stations:
        try:
            # Optional attrs: only emit non-empty truthy strings (matches m3u.py:226-232 history)
            optional = ''
            # Pop list-typed attrs so they don't get serialized as ['x','y']
            kodiprops = station.pop('kodiprops', None) or []
            extvlcopt = station.pop('extvlcopt', None) or []
            xplaylist = station.pop('x-playlist-type', '')
            webprops  = station.pop('webprops', None) or []
            for key, value in list(station.items()):
                if key in opts and str(value):
                    optional += '%s="%s" ' % (key, _m3u_attr_escape(value))
            # Restore list-typed attrs back into station dict for any later read
            if kodiprops: station['kodiprops'] = kodiprops
            if extvlcopt: station['extvlcopt'] = extvlcopt
            if xplaylist: station['x-playlist-type'] = xplaylist
            if webprops:  station['webprops'] = webprops

            out.append(line_template % (
                _m3u_attr_escape(station.get('id', '')),   # channel-id (IPTV Simple unique ID override)
                station.get('number', 0),
                _m3u_attr_escape(station.get('id', '')),
                _m3u_attr_escape(station.get('name', '')),
                _m3u_attr_escape(station.get('logo', '')),
                _m3u_attr_escape(';'.join(station.get('group') or [])),
                station.get('radio', False),
                _m3u_attr_escape(station.get('catchup', '')),
                optional.strip(),
                _m3u_label_sanitize(station.get('label') or station.get('name', '')),
            ))
            # KODIPROP / EXTVLCOPT / EXT-X-PLAYLIST-TYPE child lines after EXTINF
            for kp in kodiprops: out.append('#KODIPROP:%s\n' % kp)
            for ev in extvlcopt: out.append('#EXTVLCOPT:%s\n' % ev)
            if xplaylist:        out.append('#EXT-X-PLAYLIST-TYPE:%s\n' % xplaylist)
            for wp in webprops:  out.append('#WEBPROP:%s\n' % wp)
            # madteevee (imports fork): append a per-channel marker to
            # plugin:// URLs so IPTV Simple's hash-based ChannelId
            # doesn't collide across imports that legitimately share
            # the same source URL. Example: movistarplus' Al Jazeera
            # (ALJAZE@movistarplus) and autonomiques' Al Jazeera
            # (AlJazeeraEnglish.uk@autonomiques) both stream via the
            # same `plugin://plugin.video.movistarplus/?...&id=ALJAZE&...`
            # — same URL → same hash → Kodi PVR dedupes and only one
            # channel appears. Adding `&_pseudotv_chid=<unique_id>`
            # makes the URL unique; the receiving plugin sees an extra
            # query param it doesn't know about and ignores it
            # (Kodi plugins iterate known keys via `sys.argv` parsing).
            # Limited to plugin:// URLs — raw HTTP streams would
            # actually forward the extra param to the upstream server,
            # which is unsafe.
            url = station.get('url', '') or ''
            sid = station.get('id', '') or ''
            if url.startswith('plugin://') and sid:
                # Encode the @ in the channel id (safe chars in URL
                # query values per RFC 3986 don't include @, but most
                # plugins tolerate it; encode it anyway for safety).
                # Other chars in channel ids (alnum + . _ -) are safe.
                sid_q = sid.replace('@', '%40')
                # madteevee (imports fork): strip ALL existing
                # `_pseudotv_chid=...` markers before appending a fresh
                # one. Without this, every render cycle appended another
                # copy to the same URL (the M3UDATA load reads back the
                # URL with our previous append included). The URL grew
                # unbounded across reloads, and each unique URL produced
                # a different `ChannelId` from pvr.iptvsimple's name+url
                # hash. Result: cached `pvr://...` callback URLs (e.g.
                # the one Player._onChange invokes to advance to the
                # next scheduled show) referenced a ChannelId that no
                # longer existed in iptvsimple's DB → `LoadDetails:
                # Error filling PVR item details` → silent PlayMedia
                # failure → Custom-channel rotation stalls on the
                # transition overlay.
                url = re.sub(r'[?&]_pseudotv_chid=[^&]*', '', url)
                sep = '&' if '?' in url else '?'
                url = '%s%s_pseudotv_chid=%s' % (url, sep, sid_q)
            out.append('%s\n' % url)
        except Exception as e:
            _log('render_m3u, skipped station [%s]: %s' % (station.get('id'), e), xbmc.LOGWARNING)
    return ''.join(out)


def render_xmltv(xmltvdata):
    """Build XMLTV file content from in-memory XMLTVDATA. Pure function.

    Uses xmltv.Writer (low-level format library). Returns bytes.
    Returns None for empty input — caller skips the write on None
    to preserve disk content (refuse-empty guard avoids self-closing
    `<tv/>` corruption).

    Does NOT sort, cleanChannels, or orphan-prune. Caller is expected
    to have pre-processed XMLTVDATA. Matches today's Tasks._renderXMLTV
    behavior byte-for-byte (chkImports + Imports.syncAll paths unchanged).
    """
    import xmltv as _xmltv_lib
    # madteevee (imports fork): refuse to render empty XMLTVDATA.
    # xmltv.Writer produces a self-closing `<tv ... />` for empty
    # data; combined with our concurrent-writer races that produces
    # line-2 corruption that makes pseudotv.xml unparseable for
    # Kodi PVR. Caller checks for None return and skips the
    # write_atomic — keeps existing disk content instead of
    # overwriting with empty.
    if (not (xmltvdata.get('channels') or [])
        and not (xmltvdata.get('recordings') or [])
        and not (xmltvdata.get('programmes') or [])):
        _log('render_xmltv, REFUSING to render empty XMLTVDATA — would produce self-closing <tv/> corruption.', xbmc.LOGWARNING)
        return None
    try:
        # Resolve metadata defaults
        data = xmltvdata.get('data') or {}
        writer = _xmltv_lib.Writer(
            encoding            = DEFAULT_ENCODING,
            date                = data.get('date') or epochTime(float(time.time()), tz=False).strftime(DTFORMAT),
            source_info_url     = data.get('source-info-url')     or ADDON_ID,
            source_info_name    = data.get('source-info-name')    or ADDON_NAME,
            generator_info_url  = data.get('generator-info-url')  or ADDON_ID,
            generator_info_name = data.get('generator-info-name') or ('%s Imports' % (ADDON_NAME,)),
        )
        for ch in (xmltvdata.get('recordings') or []) + (xmltvdata.get('channels') or []):
            try: writer.addChannel(ch)
            except Exception as e:
                _log('render_xmltv, addChannel skip [%s]: %s' % (ch.get('id'), e), xbmc.LOGDEBUG)
        for prog in (xmltvdata.get('programmes') or []):
            try: writer.addProgramme(prog)
            except Exception as e:
                _log('render_xmltv, addProgramme skip [%s]: %s' % (prog.get('channel'), e), xbmc.LOGDEBUG)
        buf = io.BytesIO()
        writer.write(buf, pretty_print=True)
        return buf.getvalue()
    except Exception as e:
        _log('render_xmltv, fatal: %s' % (e,), xbmc.LOGERROR)
        return b'<?xml version="1.0" encoding="utf-8"?>\n<tv/>\n'


def write_atomic(target_special_path, content_bytes):
    """Atomic file write under WRITER_LOCK.

    imports.12 / writer_lock: serializes concurrent writers (Builder
    thread, chkImports daemon, manual HTTP-triggered rebuilds) at the
    disk boundary so the second writer's os.replace can't silently
    overwrite the first's complete rendering. WriterTimeout (>30s
    lock-wedge) is logged + skipped — disk content preserved; next
    caller succeeds.

    Resolves special:// to a real path. Writes to <path>.tmp then
    renames atomically. Crash-safe: a half-written tmp never replaces
    the real file.

    Carries the lock wrap so callers don't need to know about it.
    Mirrors today's Tasks._writeAtomic public contract (callers pass
    (target_special_path, content_bytes); no return value).
    """
    try:
        with held(ctx='write_atomic[%s]' % os.path.basename(target_special_path)):
            try:
                real_path = FileAccess.translatePath(target_special_path)
                tmp = real_path + '.tmp'
                with open(tmp, 'wb') as f:
                    if isinstance(content_bytes, str):
                        content_bytes = content_bytes.encode('utf-8')
                    f.write(content_bytes)
                    f.flush()
                    try: os.fsync(f.fileno())
                    except OSError: pass
                os.replace(tmp, real_path)  # atomic on POSIX & Win >= 3.3
                _log('write_atomic, wrote %d bytes to %s' % (len(content_bytes), real_path))
            except Exception as e:
                _log('write_atomic, failed for %s: %s' % (target_special_path, e), xbmc.LOGERROR)
                try: os.unlink(tmp)
                except (OSError, NameError): pass
    except WriterTimeout:
        _log('write_atomic, SKIPPED %s (writer lock timeout)' % target_special_path,
             xbmc.LOGWARNING)
