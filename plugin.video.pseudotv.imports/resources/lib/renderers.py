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


# madteevee (imports fork, imports.41): keys ALREADY emitted by render_m3u's
# explicit format string (channel-id / tvg-chno / tvg-id / tvg-name / tvg-logo
# / group-title / radio / catchup) and dedicated locations (label after the
# final comma; url on its own standalone line). The optional-attrs loop must
# NOT re-emit these — `group` is the load-bearer of the bug: as a Python list
# it serializes via `str(['x','y'])` to `"['x', 'y']"` which embeds a literal
# comma INSIDE the rendered `group="..."` value. m3u.py:_load's greedy regex
# `,(.*)' for the display-name field captures from the FIRST comma — so the
# in-value comma becomes the label boundary, and the parsed label ends up
# containing the entire EXTINF tail (url=, catchup-source=, provider=, ...).
# Next render the giant label gets escaped (`"` → `%22`) and emitted both as
# the optional `label="..."` attr AND as the raw display-name after the line
# comma — line size doubles per cycle. Live-observed in imports.40: one
# pseudotv.m3u line grew from ~442 bytes to 1.6 GB in ~22 doublings, which
# OOM-killed Kodi on parse + put the host into a kodi-standalone crash loop
# (kernel killed each Kodi at anon-rss ≈ 6.8 GB on an 8 GB Pi5).
_EXPLICIT_M3U_KEYS = frozenset({'id', 'number', 'name', 'logo', 'group',
                                'radio', 'catchup', 'label', 'url'})


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
                # imports.41: skip already-explicitly-emitted keys (see
                # _EXPLICIT_M3U_KEYS doc above). Without this guard, `group`
                # leaks a literal comma into the EXTINF line and m3u._load
                # mis-parses the display-name field — file size doubles per
                # render→reload→render cycle, eventually OOMing Kodi.
                if key in _EXPLICIT_M3U_KEYS:
                    continue
                if key in opts and str(value):
                    # imports.41: normalize list/tuple values to a ';'-joined
                    # string (matches how `group-title` is rendered above).
                    # str(list) would embed `, ` separators inside the value,
                    # which is the corruption vector documented at
                    # _EXPLICIT_M3U_KEYS. Defense in depth so a future M3U
                    # attribute happening to be list-typed doesn't reopen the
                    # bug.
                    if isinstance(value, (list, tuple)):
                        value = ';'.join(str(x) for x in value)
                    optional += '%s="%s" ' % (key, _m3u_attr_escape(value))
            # Restore list-typed attrs back into station dict for any later read
            if kodiprops: station['kodiprops'] = kodiprops
            if extvlcopt: station['extvlcopt'] = extvlcopt
            if xplaylist: station['x-playlist-type'] = xplaylist
            if webprops:  station['webprops'] = webprops

            # imports.42: sort + dedup + filter-falsy the group list before
            # `;`-joining. This matches m3u.py:_load line 168 which does
            # `sorted(list(set(...split(';'))))` on parse. Pre-imports.42
            # the renderer emitted input order while the parser stored
            # sorted-dedup'd — a one-cycle drift that wasn't a bug per se
            # (iptvsimple is order-agnostic on group-title) but broke
            # render→parse→render byte-stability. After this change, the
            # renderer is exactly symmetric with the parser.
            group_sorted = sorted({g for g in (station.get('group') or []) if g})
            out.append(line_template % (
                _m3u_attr_escape(station.get('id', '')),   # channel-id (IPTV Simple unique ID override)
                station.get('number', 0),
                _m3u_attr_escape(station.get('id', '')),
                _m3u_attr_escape(station.get('name', '')),
                _m3u_attr_escape(station.get('logo', '')),
                _m3u_attr_escape(';'.join(group_sorted)),
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


# madteevee (imports fork, imports.42): size circuit-breaker thresholds for
# write_atomic. Defends against runaway file bloat (imports.40 incident:
# pseudotv.m3u grew to 1.6 GB before OOM-killing Kodi at anon-rss ≈ 6.8 GB on
# an 8 GB Pi5, kicking the host into a kodi-standalone crash loop). The
# imports.41 fix closed the specific render→parse→render doubling bug; this
# is the safety net so a future regression of the WHOLE CLASS of bug (any
# silent file bloat from any cause) is caught at cycle ~10-17 instead of
# cycle ~22 (where imports.40 OOM'd), preserves disk content, and is loud
# in logs.
#
# Threshold math (per _compute_size_limit below):
#   limit = min(HARD_CAP, max(FLOOR, channel_count × PER_CHANNEL))
#
# Real-world calibration (operator's prod is 78 channels):
#   - Norm: ~150 KB total M3U (~2 KB/channel). 78 × 1 MB = 78 MB cap → 500×
#     safety margin. False-positive risk: near zero.
#   - For 1000 channels: 1 GB scaled → hits 100 MB hard ceiling. Sensible —
#     community reports pvr.iptvsimple slows badly above ~20 MB; 100 MB is
#     hard-OOM territory only on memory-constrained boxes.
#   - For low channel counts (1-5): FLOOR wins (5 MB), avoiding false
#     positives on first-channel-only or small Custom-only configs.
#   - channel_count=None (caller without count context): HARD_CAP only,
#     graceful default protection.
#
# When the limit is exceeded: LOGWARNING + return without writing (disk
# content preserved). Mirrors the refuse-empty guards at render_m3u line 109
# and render_xmltv line 258. Includes the first _WRITE_ATOMIC_PREVIEW_BYTES
# of the offending payload in the warning so on-disk forensics doesn't need
# a debug-level rerun to identify what bloated.
_WRITE_ATOMIC_HARD_CAP_BYTES    = 100 * 1024 * 1024   # 100 MB absolute ceiling
_WRITE_ATOMIC_FLOOR_BYTES       =   5 * 1024 * 1024   # 5 MB floor (handles low channel counts)
_WRITE_ATOMIC_BYTES_PER_CHANNEL =   1 * 1024 * 1024   # 1 MB per channel scaling factor
_WRITE_ATOMIC_PREVIEW_BYTES     =        1024         # 1 KB payload preview on refuse (forensics)


def _compute_size_limit(channel_count):
    """Compute the maximum allowed byte length for a write_atomic call.

    Returns _WRITE_ATOMIC_HARD_CAP_BYTES when channel_count is None
    (graceful default for callers without channel-count context).
    Otherwise: min(HARD_CAP, max(FLOOR, channel_count × PER_CHANNEL)).
    See the _WRITE_ATOMIC_HARD_CAP_BYTES block comment above for the
    calibration rationale.
    """
    if channel_count is None:
        return _WRITE_ATOMIC_HARD_CAP_BYTES
    scaled = channel_count * _WRITE_ATOMIC_BYTES_PER_CHANNEL
    return min(_WRITE_ATOMIC_HARD_CAP_BYTES, max(_WRITE_ATOMIC_FLOOR_BYTES, scaled))


def write_atomic(target_special_path, content_bytes, channel_count=None):
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
    Mirrors today's Tasks._writeAtomic public contract; callers pass
    (target_special_path, content_bytes) plus an OPTIONAL channel_count
    (added imports.42 — see _WRITE_ATOMIC_HARD_CAP_BYTES block comment).
    No return value.

    imports.42 — `channel_count`:
        Optional. When supplied, used both for:
          (1) Per-channel size cap (catches runaway bloat — see
              _compute_size_limit + the constants above for math).
          (2) Promoting the success log line to LOGINFO and including
              the channel count alongside the byte size (makes
              regressions visible in routine log review at default
              Kodi log level).
        When None, the circuit-breaker falls back to the absolute
        100 MB hard cap only, and the success log stays at LOGDEBUG.
        Backward-compatible: existing callers continue to work; they
        just lose the per-channel-cap + INFO logging until updated.
    """
    try:
        with held(ctx='write_atomic[%s]' % os.path.basename(target_special_path)):
            try:
                real_path = FileAccess.translatePath(target_special_path)
                tmp = real_path + '.tmp'
                # Normalize to bytes BEFORE the size check so the check
                # operates on the on-disk byte count (matches what the
                # filesystem will actually see). Pre-imports.42 the
                # normalize happened inside the `with open(...)` block,
                # which would have measured the wrong size for str input.
                if isinstance(content_bytes, str):
                    content_bytes = content_bytes.encode('utf-8')

                # imports.42: size circuit-breaker. See
                # _WRITE_ATOMIC_HARD_CAP_BYTES block comment above for
                # the OOM-incident background. Refuse oversize writes;
                # preserve disk content. channel_count over-allocates by
                # design (counts pre-render, but render's try/except at
                # render_m3u line 230 may drop malformed stations) —
                # that's safer than under-allocating.
                limit = _compute_size_limit(channel_count)
                if len(content_bytes) > limit:
                    preview = content_bytes[:_WRITE_ATOMIC_PREVIEW_BYTES]
                    try:    preview = preview.decode('utf-8', errors='replace')
                    except Exception: preview = repr(preview)
                    _log('write_atomic, REFUSING to write %s — content size %d bytes '
                         'exceeds limit %d (channel_count=%s). Disk content preserved. '
                         'First %d bytes of offending payload: %r'
                         % (target_special_path, len(content_bytes), limit,
                            channel_count, len(preview), preview),
                         xbmc.LOGWARNING)
                    return  # preserve disk content

                with open(tmp, 'wb') as f:
                    f.write(content_bytes)
                    f.flush()
                    try: os.fsync(f.fileno())
                    except OSError: pass
                os.replace(tmp, real_path)  # atomic on POSIX & Win >= 3.3
                # imports.42: LOGINFO when channel_count is supplied (the
                # common case post-imports.42 caller updates), LOGDEBUG
                # otherwise (backward-compat for any caller without the
                # count in scope). The INFO line is the routine log
                # signal — operators scanning kodi.log at default level
                # see one INFO line per M3U/XML write with size + count,
                # so any future bloat is visible without flipping debug
                # logging on.
                if channel_count is not None:
                    _log('write_atomic, wrote %d bytes (%d channels) to %s'
                         % (len(content_bytes), channel_count, real_path),
                         xbmc.LOGINFO)
                else:
                    _log('write_atomic, wrote %d bytes to %s'
                         % (len(content_bytes), real_path),
                         xbmc.LOGDEBUG)
            except Exception as e:
                _log('write_atomic, failed for %s: %s' % (target_special_path, e), xbmc.LOGERROR)
                try: os.unlink(tmp)
                except (OSError, NameError): pass
    except WriterTimeout:
        _log('write_atomic, SKIPPED %s (writer lock timeout)' % target_special_path,
             xbmc.LOGWARNING)
