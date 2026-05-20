#   Copyright (C) 2025 Lunatixz, mmadalone (madteevee fork)
#
# This file is part of PseudoTV Live (madteevee imports fork).
#
# PseudoTV Live is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# -*- coding: utf-8 -*-
"""
Live import support for PseudoTV (madteevee imports fork).

Pulls external M3U + XMLTV sources (local files or HTTP URLs) and surfaces
them as first-class channels in PseudoTV's `channels.json`, fully editable
through Channel Manager. Replaces the multi-instance IPTV Simple
configuration pattern where each upstream source needed its own PVR client.

Step 2 deliverable: parsers + skeleton. Sync orchestration lands in step 3,
UI in step 4. See plan history for full spec.

Module layout:
- module-level helpers — URL/scheme validation, ID generation, encoding sniff
- Imports class       — instance state + sync orchestration (stubs in step 2)

Threading note: instances live for one Builder.buildChannels cycle. Long-lived
state (cache validators, tombstones) is persisted in `channels.json` not on
the instance.
"""

import hashlib
import json
import random
import urllib.parse

from globals     import *
from m3u         import M3U
from xmltv       import parseExternalSource as _parse_xmltv_stream


# Allowlist for source URL schemes (plan H2 — basic SSRF/path-traversal mitigation).
# `special://` resolves via xbmcvfs.translatePath to addon_data paths; safe.
# `file://` is intentionally NOT included by default.
ALLOWED_M3U_SCHEMES = frozenset({'http', 'https', 'special'})
ALLOWED_EPG_SCHEMES = frozenset({'http', 'https', 'special'})


# imports.28: failure-aware retry / exponential backoff for the per-import
# refresh-interval gate. When an import enters last_status='failed', the
# gate normally waits the full refresh_interval_min before retrying — far
# too long for transient outages on operators with rim=600m (10h). The
# curves below define alternative retry cadences while the import is in
# a consecutive-failure run. `disabled` preserves pre-.28 behavior.
#
# Each curve is a list of seconds-until-next-retry indexed by fail_count.
# After the curve is exhausted (fail_count > len(curve)), delay doubles
# from the last entry. Always capped at refresh_interval_min so the
# operator-set ceiling is never exceeded.
RETRY_CURVES = {
    'aggressive': [60, 300, 900, 3600],   # 60s, 5m, 15m, 1h, then 2x to cap
    'slower':     [300, 1800, 7200],       # 5m, 30m, 2h, then 2x to cap
    'fixed':      [300],                   # always 5m (capped at rim)
}

# Cap on persisted fail_count. Backoff is bounded by refresh_interval_min
# anyway, so further growth is cosmetic — bounding the integer keeps
# tests/dashboard predictable and the channels.json record clean.
FAIL_COUNT_CAP = 20

# Mapping from settings.xml integer codes to curve names. settings.xml uses
# type="integer" with int-keyed <option> values (consistent with the
# imports.23 Imports_Sync_Interval_Minutes pattern; the spinner format
# resolves the labels via strings.po). Unknown codes fall back to
# 'aggressive' (the default).
RETRY_CURVE_BY_CODE = {
    0: 'disabled',
    1: 'aggressive',
    2: 'slower',
    3: 'fixed',
}


def computeRetryDelay(curve, fail_count, refresh_interval_sec):
    """Seconds until next retry for a failing import.

    Returns refresh_interval_sec when curve='disabled' or fail_count<=0
    (preserves pre-imports.28 behavior). For active curves, walks the
    curve indices; past the end, doubles from the last entry. Jitters
    ±12.5% to avoid a thundering herd if many imports fail in the same
    cycle (e.g., LAN outage). Always capped at refresh_interval_sec
    (operator-set ceiling).
    """
    if curve == 'disabled' or fail_count <= 0:
        return refresh_interval_sec
    if curve == 'fixed':
        return min(RETRY_CURVES['fixed'][0], refresh_interval_sec)
    arr = RETRY_CURVES.get(curve, RETRY_CURVES['aggressive'])
    n = min(fail_count, FAIL_COUNT_CAP)
    if n <= len(arr):
        delay = arr[n - 1]
    else:
        delay = arr[-1] * (2 ** (n - len(arr)))
    # Jitter ±12.5% — symmetric bound (Python's floor division on negative
    # values would make `-delay // 8` one larger than `delay // 8` in
    # magnitude, e.g. -60//8 = -8 vs 60//8 = 7; using the same positive
    # bound on both sides keeps the band actually symmetric).
    jitter_bound = delay // 8
    jitter = random.randint(-jitter_bound, jitter_bound) if jitter_bound > 0 else 0
    return max(1, min(delay + jitter, refresh_interval_sec))

# Maximum cascade scan steps before giving up (plan H5).
# 9999 + a small margin lets cascade exhaust the full namespace before
# marking a channel `unallocated`.
CASCADE_OVERFLOW_LIMIT = CHANNEL_LIMIT + 1


# madteevee (imports fork, imports.43): LiveTV-first availability hardening.
# Operator-flagged 2026-05-15 — imports should be visible to iptvsimple
# immediately at Kodi boot, not after Builder finishes rebuilding the
# Custom queue (~30 min wait with 20+ Custom channels). The pain manifests
# specifically in the post-cleanup + first-boot cases where the on-disk M3U
# is missing/empty AND the per-import `refresh_interval_min` gate keeps
# syncAll from re-fetching for hours.
#
# Two settings (resources/settings.xml `Imports_Boot_Strategy` +
# `Imports_Disk_Presence_Scope`, both `type="integer"` mirroring the
# imports.28 RETRY_CURVE_BY_CODE pattern) drive three behavior dimensions.
# The maps below decode the int-stored settings to string identifiers used
# throughout the code paths — cleaner than naked ints, idiomatic with
# imports.28.

# Imports_Boot_Strategy — what happens at service boot+5s
# (tasks.py:_startImportsThread._loop after the existing 5s delay).
BOOT_STRATEGY_BY_CODE = {
    0: 'disk_presence',   # default — no boot action; gate handles missing data on first cycle
    1: 'eager_render',    # render channels.json's existing import records to disk (no HTTP)
    2: 'force_sync',      # first chkImports call carries force_scope='all' (HTTP at every boot)
}

# Imports_Disk_Presence_Scope — what counts as "missing" for the gate's
# disk-presence check. See _isDiskMissing below.
DISK_PRESENCE_SCOPE_BY_CODE = {
    0: 'main_m3u',         # default — only check pseudotv.m3u; missing/empty forces ALL imports
    1: 'per_import_epg',   # check cache/epg_<id>.xml per-import; missing forces just that one
    2: 'both',             # either condition triggers (defense in depth)
}

# imports.43: stub-sized thresholds for the disk-presence check. 200 bytes
# is comfortably above the M3U header-only stub (`#EXTM3U ...\n` is ~107 B,
# the corruption shape past empty-write incidents have produced) and below
# any legitimate single-channel payload (one EXTINF + URL line is ~300 B).
# 100 bytes is above the self-closing `<tv/>` XML root (~50 B) that
# `render_xmltv`'s refuse-empty guard exists to prevent.
_DISK_PRESENCE_M3U_MIN_BYTES = 200
_DISK_PRESENCE_EPG_MIN_BYTES = 100


def _isDiskMissing(import_id, scope):
    """Return True when the on-disk artifact required by `scope` is missing
    or stub-sized for the given import. Caller treats True as "force-sync
    this import despite the refresh_interval_min gate."

    Scopes:
      'main_m3u'        — only check pseudotv.m3u (the merged file).
                          Triggers for ALL imports on a single positive
                          check (the main M3U is shared).
      'per_import_epg'  — check cache/epg_<import_id>.xml per-import.
                          Naming mirrors imports.py:_epgCachePath.
      'both'            — either condition triggers (defense in depth).

    Filesystem errors (rare: OSError from os.path.exists / getsize on a
    permission glitch or stat race) are treated as "missing" — fail-safe
    behavior so we force-sync rather than silently skip when we can't
    tell. Same rationale as the refuse-empty guard's bias toward "preserve
    disk content" — better to do an extra sync than to miss a real
    corruption.
    """
    if scope in ('main_m3u', 'both'):
        try:
            real = FileAccess.translatePath(M3UFLEPATH)
            if not os.path.exists(real) or os.path.getsize(real) < _DISK_PRESENCE_M3U_MIN_BYTES:
                return True
        except OSError:
            return True

    if scope in ('per_import_epg', 'both'):
        try:
            # Mirror imports.py:_epgCachePath naming. Function-local to
            # avoid forward-reference (_epgCachePath is an Imports method,
            # not module-level — this helper is module-level so we build
            # the path here directly).
            epg_path = FileAccess.translatePath(
                'special://profile/addon_data/%s/cache/epg_%s.xml' % (ADDON_ID, import_id),
            )
            if not os.path.exists(epg_path) or os.path.getsize(epg_path) < _DISK_PRESENCE_EPG_MIN_BYTES:
                return True
        except OSError:
            return True

    return False


def validate_source_url(url, allowed=ALLOWED_M3U_SCHEMES):
    """
    Allowlist scheme check for import source URLs.

    Returns the URL unchanged on success; raises ValueError on bad scheme.
    Per plan H2: minimal SSRF mitigation — operator pasting `file:///etc/passwd`
    or `http://localhost:8443/admin` should be refused at save time.

    Empty scheme (= bare absolute or relative local path like `/tmp/x.m3u`)
    is accepted — Kodi's xbmcvfs handles these uniformly.
    """
    if not url:
        raise ValueError('source URL is empty')
    parsed = urllib.parse.urlparse(url)
    scheme = (parsed.scheme or '').lower()
    if scheme == '':
        # Local path (no scheme). Accept; xbmcvfs handles relative/absolute paths.
        return url
    if scheme not in allowed:
        raise ValueError(
            "unsupported scheme '%s' (allowed: %s)" % (scheme, ', '.join(sorted(allowed)))
        )
    if scheme == 'special' and not url.startswith('special://'):
        raise ValueError("special:// URLs must use the 'special://' prefix form")
    return url


def fallback_channel_id(source_url):
    """
    Stable fallback channel id for sources without `tvg-id` (plan H7).

    Returns first 12 hex chars of MD5(url). MD5 used for non-cryptographic
    naming only; cryptographic strength not required.
    """
    if not source_url:
        return ''
    return hashlib.md5(source_url.encode('utf-8')).hexdigest()[:12]


def namespaced_channel_id(source_tvg_id, import_id, source_url=None):
    """
    Construct the PseudoTV channel ID for an imported channel.

    Format: `<source_tvg_id_or_urlhash>@<import_id>` (plan §2 channel ID format).
    Source tvg-id preferred for stability; URL hash fallback when source omits it.
    Both halves are sanitized to safe chars (alnum + `_-.`).
    """
    if not import_id:
        raise ValueError('import_id is required')
    head = (source_tvg_id or '').strip() or fallback_channel_id(source_url)
    if not head:
        raise ValueError('cannot derive channel id without tvg-id or source_url')
    # Sanitize both halves: keep alnum + `_-.`; replace anything else with `_`.
    head = re.sub(r'[^A-Za-z0-9_.\-]', '_', head)
    tail = re.sub(r'[^A-Za-z0-9_.\-]', '_', import_id)
    return '%s@%s' % (head, tail)


def is_remote_url(url_or_path):
    """True if the source string is a remote URL (http/https), False for local paths."""
    if not url_or_path:
        return False
    parsed = urllib.parse.urlparse(url_or_path)
    return (parsed.scheme or '').lower() in {'http', 'https'}


# --------------------------------------------------------------------------
# Imports class
# --------------------------------------------------------------------------

class Imports(object):
    """
    Orchestrator for live channel imports.

    Construction is cheap; heavy work happens inside `syncAll()`. Instances
    are typically created per-Builder-cycle by `Builder.buildChannels` (step 3
    integration) and discarded after `syncAll()` returns.

    Args:
        channels: `Channels` instance (writable) — mutated to add/refresh imported channels
        m3u:      `M3U` instance — `addStation()` invoked for each imported channel
        xmltv:    `XMLTVS` instance — `addChannel()`/`addProgramme()` invoked for EPG
        service:  `Service` instance for `_interrupt()`/`_suspend()` polling

    All four collaborators are optional at construction time so the class
    can be unit-tested in isolation (parsers don't need them).
    """

    def __init__(self, channels=None, m3u=None, xmltv=None, service=None):
        self.channels = channels
        self.m3u      = m3u
        self.xmltv    = xmltv
        self.service  = service

    def log(self, msg, level=None):
        # Default to LOGDEBUG; matches existing class pattern (m3u.py:82, channels.py).
        try:
            return log('%s: %s' % (self.__class__.__name__, msg),
                       level if level is not None else xbmc.LOGDEBUG)
        except NameError:
            # `log` not yet bound during unit-test import-time when globals are
            # stubbed; degrade silently.
            pass

    # ------------------------------------------------------------------
    # imports.43: LiveTV-first availability — boot-time render-from-persisted
    # ------------------------------------------------------------------

    def renderFromPersistedState(self):
        """Render the existing import-channel state from channels.json to disk
        (pseudotv.m3u) WITHOUT a fresh HTTP fetch.

        Called from `tasks.py:_startImportsThread._loop` at boot+5s when
        `Imports_Boot_Strategy` is set to `eager_render` (code 1). Operator-
        flagged 2026-05-15: with 20+ Custom channels building serially at
        ~90s each, imports stay invisible to iptvsimple for ~30 min on boot
        if the on-disk M3U is empty/missing. This method makes the LAST-
        KNOWN persisted import state visible immediately by rendering the
        type='import' enabled channels straight from channels.json into
        the in-memory M3UDATA + calling m3u._save (which triggers the
        renderers.render_m3u + write_atomic path imports.42 plumbs
        channel_count through).

        Guarded by `_isDiskMissing(scope='main_m3u')` — fires only when
        the on-disk M3U is missing or stub-sized (header-only). Healthy
        M3U → no-op return 0 (don't clobber existing data). This means
        the eager_render strategy is safe to leave enabled across reboots:
        normal boots with intact M3U skip this entirely, only post-cleanup
        or first-ever boots see the eager render fire.

        EPG content is NOT rendered here. Channels alone are enough for
        iptvsimple to load + let the operator tune them; EPG fills in on
        the natural chkImports cycle (≤5 min for autosync operators).
        Adding EPG would require parsing each `cache/epg_<id>.xml` per-
        import and merging into XMLTVDATA — meaningful complexity for
        marginal benefit. Defer to imports.44 if operators report long
        EPG-empty windows post-eager-render.

        Returns the count of imports rendered (excluding disabled), or 0
        if the on-disk M3U is already healthy / no imports are configured.
        Caller logs the result at INFO.

        Requires `self.channels` and `self.m3u` to be set (writable). When
        either is None (test/standalone construction) returns 0 silently.
        Idempotent — the imports.42 writer_lock + atomic-rename in
        `write_atomic` prevents torn writes if multiple boot-render calls
        race (shouldn't happen in practice — `_startImportsThread` is
        single-threaded — but defensive).
        """
        if self.channels is None or self.m3u is None:
            return 0
        # Guard: only fire when on-disk M3U is actually missing/stub.
        # Re-uses the same helper the gate disk-presence check uses, so
        # the trigger semantics are consistent across both paths.
        if not _isDiskMissing(import_id='__bootcheck__', scope='main_m3u'):
            return 0
        # Pull all enabled import-typed channels from channels.json. The
        # records carry full M3U EXTINF state (id, name, url, group, logo,
        # catchup, etc) — verified in the imports.43 planning phase via
        # Phase 1 exploration of the live channels.json shape. No HTTP
        # fetch, no re-parse — just the persisted state Builder/syncAll
        # produced on a prior cycle.
        try:
            all_channels = self.channels.getChannels() or []
        except Exception as e:
            self.log('renderFromPersistedState, channels.getChannels() failed: %s' % (e,),
                     level=xbmc.LOGWARNING)
            return 0
        import_channels = [c for c in all_channels
                           if c.get('type') == 'import' and c.get('enabled', True)]
        if not import_channels:
            return 0
        # Direct stations-list assignment matches the shape M3U._save
        # expects (M3UDATA dict with 'stations' + 'recordings' keys per
        # m3u.py:75). Builder.__setStation does the same kind of mutation
        # per-channel; we just do it bulk for imports. Recordings stay
        # untouched (they're a different concept — DVR-style local
        # recordings, not live imports).
        self.m3u.M3UDATA['stations'] = list(import_channels)
        saved = self.m3u._save()   # imports.42 plumbs channel_count for breaker + INFO log
        if saved:
            return len(import_channels)
        return 0

    # ------------------------------------------------------------------
    # Parsers — implemented in step 2
    # ------------------------------------------------------------------

    def parseM3U(self, content, import_id):
        """
        Parse external M3U bytes into a list of imported-channel dicts.

        Each dict carries:
        - `id`              — namespaced PseudoTV channel ID (`<tvg_id>@<import_id>`)
        - `epg_id`          — original source `tvg-id` (for XMLTV match in step 3)
        - `import_source`   — `import_id` argument
        - `source_tvg_chno` — original source channel number (int) or None
        - `source_url`      — playable URL from the M3U URL line
        - all standard EXTINF attrs preserved verbatim (group, logo, catchup-source, etc.)

        Caller (`syncOne` in step 3) is responsible for cascade allocation,
        operator-override application, tombstone filtering, and the final
        write into `pseudotv.m3u`. This method does no I/O and no mutation.
        """
        raw = M3U.parseExternalSource(content, log_callback=lambda m: self.log(m))
        out = []
        for entry in raw:
            src_tvg_id = entry.get('id') or ''
            cid = namespaced_channel_id(src_tvg_id, import_id, source_url=entry.get('url'))
            chno = entry.get('number')
            out.append({
                # PseudoTV-side identity
                'id'            : cid,
                'import_source' : import_id,
                'epg_id'        : src_tvg_id,            # original; matches XMLTV
                'source_tvg_chno': int(chno) if isinstance(chno, int) else None,
                'source_url'    : entry.get('url', ''),
                # Source-side fields preserved verbatim (caller may override)
                'name'          : entry.get('name', ''),
                'logo'          : entry.get('logo', ''),
                'group'         : list(entry.get('group', [])),
                'catchup'       : entry.get('catchup', ''),
                'catchup-source': entry.get('catchup-source', ''),
                'catchup-days'  : entry.get('catchup-days', ''),
                'catchup-correction': entry.get('catchup-correction', ''),
                'radio'         : bool(entry.get('radio', False)),
                'realtime'      : bool(entry.get('realtime', False)),
                'media'         : bool(entry.get('media', False)),
                'media-dir'     : entry.get('media-dir', ''),
                'media-size'    : entry.get('media-size', ''),
                'tvg-shift'     : entry.get('tvg-shift', ''),
                'provider'      : entry.get('provider', ''),
                'provider-type' : entry.get('provider-type', ''),
                'provider-logo' : entry.get('provider-logo', ''),
                'kodiprops'     : list(entry.get('kodiprops', [])),
                'extvlcopt'     : list(entry.get('extvlcopt', [])),
                'webprops'      : list(entry.get('webprops', [])),
                'x-playlist-type': entry.get('x-playlist-type', ''),
            })
        return out

    def parseXMLTV(self, source, channel_id_filter=None):
        """
        Yield (kind, dict) tuples from an external XMLTV source.

        Wraps `xmltv.parseExternalSource` (auto gzip detection, streaming via
        iterparse + element clear). `channel_id_filter` is an optional iterable
        of source-side XMLTV channel ids; programmes whose `channel=` attr is
        not in the filter are skipped (saves memory when most of the source's
        channels aren't needed by this import).

        Note: yielded dicts contain SOURCE-SIDE channel ids. Caller (step 3)
        is responsible for rewriting `id` / `channel` to the namespaced form
        (`<source_id>@<import_id>`) before writing to `pseudotv.xml`. This
        rewrite is what prevents IPTV Simple's tvg-id dedup from silently
        dropping channels with same source-side tvg-id (plan G1).
        """
        return _parse_xmltv_stream(
            source,
            channel_id_filter=channel_id_filter,
            log_callback=lambda m: self.log(m),
        )

    # ------------------------------------------------------------------
    # Sync orchestration — stubs; implementation lands in step 3
    # ------------------------------------------------------------------

    def syncOne(self, import_cfg, existing_channels):
        """
        Sync a single import: fetch (conditional) + parse + reconcile.

        Does NOT write to channels.json or M3U/XMLTV — returns a delta
        for the caller (syncAll) to merge after running cascadeAllocate
        across all imports.

        Returns dict:
        {
            'import_id'   : str,
            'status'      : str,            # 'ok' | 'unchanged' | 'failed' | 'disabled'
            'updated_cfg' : dict,           # import_cfg with refreshed etag/last_modified/last_*
            'parsed'      : list,           # parseM3U output (namespaced channels)
            'new'         : list,           # channels not yet in existing
            'refreshed'   : list,           # (existing, parsed) pairs
            'orphans'     : list,           # existing channels missing from source
            'epg_pairs'   : list,           # (kind, dict) tuples from XMLTV parse
                                            # — caller rewrites channel ids before pushing
            'epg_id_map'  : dict,           # {source_tvg_id: namespaced_id} for EPG rewrite
            'error'       : str or None,
        }
        """
        result = {
            'import_id'  : import_cfg.get('id', ''),
            'status'     : 'failed',
            'updated_cfg': dict(import_cfg),
            'parsed'     : [],
            'new'        : [],
            'refreshed'  : [],
            'orphans'    : [],
            'epg_pairs'  : [],
            'epg_id_map' : {},
            'error'      : None,
        }
        import_id = import_cfg.get('id', '')

        if not import_cfg.get('enabled', True):
            result['status'] = 'disabled'
            return result
        if not import_id:
            result['error'] = 'import has no id'
            return result

        # ---- M3U fetch + parse ----
        m3u_source = import_cfg.get('m3u_url') or import_cfg.get('m3u_path')
        if m3u_source:
            fetched = self.fetchSource(
                m3u_source,
                etag=import_cfg.get('etag'),
                last_modified=import_cfg.get('last_modified'),
            )
            if fetched['status'] == 304:
                # Source unchanged — but we still need to know what's there
                # to allow cascade allocation. Re-parse from operator's
                # cached/known set: skip the parse step and use existing
                # channels for this import as the "parsed" list.
                self.log('[%s] syncOne, source unchanged (304); preserving channels' % import_id)
                result['status'] = 'unchanged'
                result['parsed'] = [
                    c for c in existing_channels
                    if c.get('import_source') == import_id
                ]
                # epg_id_map for unchanged path
                result['epg_id_map'] = {
                    c.get('epg_id'): c.get('id')
                    for c in result['parsed']
                    if c.get('epg_id') and c.get('id')
                }
                # No reconcile when nothing's parsed-fresh — just no-op
                result['updated_cfg']['last_status'] = 'unchanged'
                result['updated_cfg']['last_error']  = None
                # imports.25: update last_sync_at on EVERY attempt outcome, not just
                # 200. Semantics shift: "time of last sync attempt", not "time of
                # last successful content fetch". Required by the per-import
                # refresh_interval_min gate in syncAll (~line 486), which keys on
                # `now - last_sync_at < interval * 60`. If last_sync_at didn't
                # advance on 304s, a 304-returning source would always look due,
                # and refresh_interval_min would have no effect. The display side
                # (manager.html "last sync N ago") is more honest under the new
                # semantic too — a 304 IS a successful poll.
                result['updated_cfg']['last_sync_at'] = int(time.time())
                # DO refresh etag/last_modified in case server rotated them
                if fetched.get('etag'):          result['updated_cfg']['etag']          = fetched['etag']
                if fetched.get('last_modified'): result['updated_cfg']['last_modified'] = fetched['last_modified']
                # Skip to EPG fetch (still try; might have its own cadence)
            elif fetched['status'] != 200:
                result['error'] = fetched.get('error') or ('M3U fetch status %s' % fetched['status'])
                result['updated_cfg']['last_status'] = 'failed'
                result['updated_cfg']['last_error']  = result['error']
                # imports.25: record the attempt (see 304-path comment above for rationale)
                result['updated_cfg']['last_sync_at'] = int(time.time())
                self.log('[%s] syncOne, M3U fetch failed: %s' % (import_id, result['error']),
                         level=xbmc.LOGWARNING)
                return result
            else:
                # status == 200 — fresh content
                try:
                    parsed = self.parseM3U(fetched['content'], import_id)
                except ValueError as e:
                    result['error'] = 'M3U parse failed: %s' % e
                    result['updated_cfg']['last_status'] = 'failed'
                    result['updated_cfg']['last_error']  = result['error']
                    # imports.25: record the attempt (see 304-path comment for rationale)
                    result['updated_cfg']['last_sync_at'] = int(time.time())
                    self.log('[%s] syncOne, %s' % (import_id, result['error']),
                             level=xbmc.LOGWARNING)
                    return result
                if not parsed:
                    # Empty parsed list — defensive: don't mass-orphan (G24).
                    result['error'] = 'M3U parse yielded zero channels (kept previous)'
                    result['updated_cfg']['last_status'] = 'warning'
                    result['updated_cfg']['last_error']  = result['error']
                    result['parsed'] = [
                        c for c in existing_channels
                        if c.get('import_source') == import_id
                    ]
                    # imports.25: record the attempt (see 304-path comment for rationale)
                    result['updated_cfg']['last_sync_at'] = int(time.time())
                    self.log('[%s] syncOne, %s' % (import_id, result['error']),
                             level=xbmc.LOGWARNING)
                    return result
                result['parsed'] = parsed
                result['epg_id_map'] = {
                    c['epg_id']: c['id']
                    for c in parsed if c.get('epg_id')
                }
                if fetched.get('etag'):          result['updated_cfg']['etag']          = fetched['etag']
                if fetched.get('last_modified'): result['updated_cfg']['last_modified'] = fetched['last_modified']

        # If neither m3u_url nor m3u_path: this is an EPG-only import
        # (plan G7). Caller's existing channels with epg_id set are the
        # match candidates. Skip the parse step.
        else:
            result['parsed'] = []
            self.log('[%s] syncOne, EPG-only import' % import_id)

        # ---- Reconcile against existing channels ----
        existing_for_this_import = [
            c for c in existing_channels
            if c.get('import_source') == import_id
        ]
        new_channels, refreshed_pairs, orphans = self.reconcile(
            result['parsed'], existing_for_this_import, import_cfg
        )
        result['new']       = new_channels
        result['refreshed'] = refreshed_pairs
        result['orphans']   = orphans

        # Apply field merging to refreshed channels in place
        for existing, parsed in refreshed_pairs:
            self.applyRefresh(existing, parsed)

        # Mark orphans
        for orphan in orphans:
            orphan['is_orphan'] = True

        # ---- EPG fetch + parse ----
        epg_source = import_cfg.get('epg_url') or import_cfg.get('epg_path')
        if epg_source:
            # Conditional GET against the per-import EPG cache file at
            # CACHE_LOC/epg_<import_id>.xml. On 200 we (re)parse + (re)write
            # the cache; on 304 we hydrate epg_pairs from the cache so the
            # `XMLTVS._load.cleanSelf` slug-strip on startup doesn't cost us
            # this import's EPG. If the cache is missing/corrupt on a 304,
            # we clear the validators in updated_cfg so the next cycle
            # force-fetches (one-cycle self-heal).
            epg_fetched = self.fetchSource(
                epg_source,
                etag=import_cfg.get('epg_etag'),
                last_modified=import_cfg.get('epg_last_modified'),
            )
            if epg_fetched['status'] == 200:
                # Don't filter at parse time — many EPG sources (davidmuma EPG_dobleM,
                # other regional EPGs) use human-readable display names as channel
                # attributes on <programme> too, not just on <channel>. A tvg-id-only
                # filter drops those programmes silently. syncAll's _match function
                # handles both id-exact AND normalized-name fallback per channel,
                # so filtering there gets the correct set.
                # Memory cost is bounded by iterparse + elem.clear() — even a 50MB
                # EPG processes in <100MB RSS regardless of total programme count.
                try:
                    epg_pairs = list(self.parseXMLTV(epg_fetched['content']))
                    result['epg_pairs'] = epg_pairs
                    self.log('[%s] syncOne, parsed EPG: %d items' % (import_id, len(epg_pairs)))
                except Exception as e:
                    self.log('[%s] syncOne, EPG parse failed: %s' % (import_id, e),
                             level=xbmc.LOGWARNING)
                # Persist the fetched bytes to per-import cache BEFORE updating
                # validators in updated_cfg — if the write fails we keep
                # validators at their previous (now-mismatched) state so the
                # next cycle force-fetches.
                cached_ok = self._epgCacheWrite(import_id, epg_fetched['content'])
                if cached_ok:
                    if epg_fetched.get('etag'):          result['updated_cfg']['epg_etag']          = epg_fetched['etag']
                    if epg_fetched.get('last_modified'): result['updated_cfg']['epg_last_modified'] = epg_fetched['last_modified']
            elif epg_fetched['status'] == 304:
                cached_pairs = self._epgCacheRead(import_id)
                if cached_pairs is not None:
                    result['epg_pairs'] = cached_pairs
                    self.log('[%s] syncOne, EPG 304 hydrated from cache (%d items)'
                             % (import_id, len(cached_pairs)),
                             level=xbmc.LOGINFO)
                else:
                    # Cache missing or corrupt — clear validators so next cycle
                    # force-fetches a fresh copy and rebuilds the cache file.
                    result['updated_cfg']['epg_etag']          = None
                    result['updated_cfg']['epg_last_modified'] = None
                    self.log('[%s] syncOne, EPG 304 but cache missing/corrupt — clearing validators'
                             % (import_id,), level=xbmc.LOGWARNING)
            else:
                # EPG failure is non-fatal — channels still work, just no programmes
                self.log('[%s] syncOne, EPG fetch failed: %s' % (import_id, epg_fetched.get('error')),
                         level=xbmc.LOGWARNING)

        # Success
        if result['status'] == 'failed':
            result['status'] = 'ok'
        result['updated_cfg']['last_status']  = result['status']
        result['updated_cfg']['last_error']   = None
        result['updated_cfg']['last_sync_at'] = int(time.time())
        return result


    def syncAll(self, force_scope=None):
        """
        Sync every enabled import; cascade-allocate; merge into channels.json
        and push to in-memory M3U/XMLTV.

        Requires self.channels (writable), self.m3u, self.xmltv to be set.
        Builder.buildChannels (step 3 hook) is the canonical caller — it
        holds the build lock so concurrent operator edits via Channel
        Manager are safely merged via setChannels' merge-on-write.

        imports.27: `force_scope` controls per-import gate bypass:
          - None (default) — no force; every import honors its
            refresh_interval_min gate. Used by natural daemon cycles and
            by any caller that doesn't want to bypass (Builder step-3
            hook, tests).
          - 'all' — bypass the gate for every import in this cycle. Used
            by global kicks (batch operator edits, disable, cleanup, the
            'Refresh all' button when no specific id is targeted).
          - '<import_id>' — bypass the gate ONLY for the specified import;
            others still honor their interval. Used by single-import kicks
            (Renumber, Orphans, Refresh on a single import row).
          - '<unknown_id>' — no match; all imports gate normally. A debug
            log line surfaces typos.

        The kick value written to chkImports.kick (server.py:982/1022/
        1227/1299/1442/1472 + utilities.py:164) is the source of this
        parameter — the daemon reads the value verbatim and threads it
        through chkImports → syncAll without interpretation.

        Pre-imports.27 this was a boolean `force=False/True`; the rename
        carries scope information so a kick targeting one import doesn't
        force-refresh the unrelated others. See plan at
        /home/madalone/.claude/plans/dig-into-c-do-typed-kettle.md.

        Returns dict {import_id: per-import-result}.
        """
        if self.channels is None:
            raise RuntimeError('syncAll() requires self.channels to be set')

        imports = list(self.channels.getImports() or [])
        if not imports:
            self.log('syncAll, no imports configured; skipping')
            return {}

        # imports.27: surface typos in force_scope (debug-only). If the
        # operator's kick value doesn't match 'all' OR any known import
        # id, the gate falls through to normal gating (no bypass) — the
        # operator sees "Refresh did nothing" which is the same UX as a
        # deferred kick. The debug log line makes the typo visible without
        # spamming for the common cases (None, 'all', valid id).
        if force_scope is not None and force_scope != 'all':
            known_ids = {imp.get('id') for imp in imports if imp.get('id')}
            if force_scope not in known_ids:
                self.log("syncAll, force_scope=%r doesn't match any known import id (typo? known: %s)"
                         % (force_scope, sorted(known_ids)), level=xbmc.LOGDEBUG)

        # Snapshot existing channels (operator-built + imported)
        existing_channels = list(self.channels.getChannels() or [])

        # Drive syncOne in priority order (top of list = highest priority)
        per_import_results = {}
        parsed_imports_for_cascade = []  # [(import_cfg, list_of_parsed_channels)]

        for import_cfg in imports:
            iid = import_cfg.get('id') or ''
            if self.service is not None and hasattr(self.service, '_interrupt'):
                if self.service._interrupt():
                    self.log('syncAll, interrupted before [%s]' % iid)
                    break

            # imports.25 / .27: per-import refresh-interval gate. Honors the
            # operator-facing `refresh_interval_min` setting (saved by the
            # dashboard's edit-import dialog at manager.html:1678). Skip this
            # import when: enabled AND no force-bypass active AND interval > 0
            # AND less time elapsed than interval.
            #
            # imports.27: `force_active` is per-import — set True only when
            # `force_scope` matches THIS import's id specifically OR is 'all'.
            # Pre-imports.27 a single-import kick (Renumber on import X, say)
            # would bypass the gate for EVERY import in the cycle because
            # `force` was a boolean stripped of scope info. The kick property
            # already encoded scope (see server.py:1227/1299/1472 + the 4
            # 'all' setters); now the gate uses it. Disabled imports fall
            # through to syncOne, which already returns status='disabled'
            # (preserves existing per_import_results shape for that case).
            #
            # Operator edits to imported channels (rename / logo / number /
            # enabled / favorite / group / tombstoned) are preserved across
            # syncs by the existing imports.20 `operator_overrides` mechanism
            # in `_mergeChannel` and `cascadeAllocate` — imports.25 + .27 do
            # NOT weaken that guarantee. Skipped imports trivially preserve
            # (no merge runs); non-skipped imports go through the same merge
            # that respected operator_overrides before. See plan in
            # /home/madalone/.claude/plans/dig-into-c-do-typed-kettle.md.
            force_active = (force_scope == 'all'
                            or (force_scope is not None
                                and force_scope == import_cfg.get('id')))

            # imports.43: disk-presence check. When the configured scope
            # detects missing/stub on-disk M3U or per-import EPG cache,
            # force-sync THIS import regardless of refresh_interval_min.
            # Integrates with the existing force_active mechanism so all
            # downstream gate logic (abandon check at the next block, normal
            # gate at the elapsed-time check) automatically honors disk-
            # missing as a force-sync trigger — zero new branching needed.
            # Skipped when force_active is already True (no point double-
            # forcing) or when the import is disabled / lacks an id (the
            # next block handles that). Setting reads use the same defensive
            # try/except + fallback pattern as the imports.28 retry curve
            # reads two blocks down — settings.xml edge cases (corrupt,
            # missing) don't break the gate.
            if not force_active and import_cfg.get('enabled', True) and import_cfg.get('id'):
                try:
                    scope = DISK_PRESENCE_SCOPE_BY_CODE.get(
                        int(SETTINGS.getSettingInt('Imports_Disk_Presence_Scope') or 0),
                        'main_m3u',
                    )
                except Exception:
                    scope = 'main_m3u'
                if _isDiskMissing(import_cfg.get('id'), scope):
                    force_active = True
                    self.log('[%s] syncAll, disk-presence: forcing sync (scope=%s, file missing/stub)'
                             % (import_cfg.get('id'), scope), level=xbmc.LOGINFO)

            if (import_cfg.get('enabled', True)
                and import_cfg.get('id')):
                try:
                    rim = int(import_cfg.get('refresh_interval_min') or 0)
                except (TypeError, ValueError):
                    rim = 0
                try:
                    last = int(import_cfg.get('last_sync_at') or 0)
                except (TypeError, ValueError):
                    last = 0

                # imports.28: failure-aware retry / abandon gating.
                #
                # Read curve + abandon-hours from settings ONCE per cycle (cheap
                # cached reads, but doing it inside the loop keeps the test
                # mocks deterministic). `disabled` curve preserves pre-.28
                # behavior exactly — gate fires at rim*60 regardless of
                # last_status. Abandon-hours=0 means "never abandon" (default).
                try:
                    curve = RETRY_CURVE_BY_CODE.get(
                        int(SETTINGS.getSettingInt('Imports_Failure_Retry_Curve') or 1),
                        'aggressive',
                    )
                except Exception:
                    curve = 'aggressive'
                try:
                    abandon_hours = int(SETTINGS.getSettingInt('Imports_Failure_Abandon_Hours') or 0)
                except Exception:
                    abandon_hours = 0

                try:
                    fail_count = int(import_cfg.get('fail_count') or 0)
                except (TypeError, ValueError):
                    fail_count = 0
                try:
                    failed_since = int(import_cfg.get('failed_since') or 0) or None
                except (TypeError, ValueError):
                    failed_since = None
                last_status = import_cfg.get('last_status')

                now = int(time.time())

                # 1) Abandon check FIRST (precedes normal gate). When an import
                # has been failing for >= abandon_hours, status flips to
                # 'abandoned' and the gate skips unconditionally — operator
                # must hit Refresh (force_scope match) to wake it back up.
                # Bypass-on-force keeps a manual Refresh actually meaningful;
                # without it, abandon would be a one-way trap.
                is_abandoned = (
                    abandon_hours > 0
                    and failed_since
                    and last_status == 'failed'
                    and (now - failed_since) >= abandon_hours * 3600
                )
                if is_abandoned and not force_active:
                    self.log('[%s] syncAll, abandoned: failing %ds >= %dh (force Refresh to retry)'
                             % (iid, now - failed_since, abandon_hours), level=xbmc.LOGINFO)
                    # 'abandoned' is a DERIVED status emitted in this result
                    # only. We deliberately do NOT write last_status='abandoned'
                    # into updated_cfg — storage keeps last_status='failed'
                    # (which is still the true storage state), and the next
                    # cycle's abandon check re-evaluates from failed_since +
                    # the current Imports_Failure_Abandon_Hours setting.
                    # server.py mirrors this derivation when serving the
                    # imports list so the dashboard sees the abandoned state.
                    per_import_results[iid] = {
                        'import_id'  : iid,
                        'status'     : 'abandoned',
                        'updated_cfg': dict(import_cfg),
                        'parsed'     : [],
                        'new'        : [],
                        'refreshed'  : [],
                        'orphans'    : [],
                        'epg_pairs' : [],
                        'epg_id_map': {},
                        'error'      : None,
                    }
                    continue

                # 2) Normal gate (with failure-aware effective interval).
                # When the import is in a consecutive-failure run, derive a
                # shorter retry interval from the configured curve. Capped at
                # rim*60 so operator's ceiling is honored.
                if not force_active and rim > 0 and last > 0:
                    effective_gate_sec = rim * 60
                    if last_status == 'failed' and fail_count > 0:
                        effective_gate_sec = computeRetryDelay(curve, fail_count, rim * 60)
                    elapsed = now - last
                    # Defensive: 0 <= elapsed guards against negative values from
                    # clock skew (NTP step backwards, system time set manually,
                    # etc.) — never block a sync because of a misbehaving clock.
                    if 0 <= elapsed < effective_gate_sec:
                        if last_status == 'failed' and fail_count > 0:
                            self.log('[%s] syncAll, retry gate: skipped (elapsed %ds < %ds backoff, fail_count=%d, curve=%s)'
                                     % (iid, elapsed, effective_gate_sec, fail_count, curve), level=xbmc.LOGINFO)
                        else:
                            self.log('[%s] syncAll, refresh-interval gate: skipped (elapsed %ds < %dm)'
                                     % (iid, elapsed, rim), level=xbmc.LOGINFO)
                        per_import_results[iid] = {
                            'import_id'  : iid,
                            'status'     : 'skipped',
                            'updated_cfg': dict(import_cfg),
                            'parsed'     : [],
                            'new'        : [],
                            'refreshed'  : [],
                            'orphans'    : [],
                            'epg_pairs' : [],
                            'epg_id_map': {},
                            'error'      : None,
                        }
                        continue

            res = self.syncOne(import_cfg, existing_channels)

            # imports.28: maintain fail_count + failed_since on the resulting
            # updated_cfg. Centralized here (not in syncOne) so all status
            # branches converge on one tracking rule. `warning` (empty M3U
            # with channels-kept) deliberately doesn't touch the fields —
            # operator's channels are intact and we want normal cadence to
            # pick up fresh content. Only `failed` (hard fetch/parse error)
            # triggers backoff.
            new_status = res.get('status')
            if new_status in ('ok', 'unchanged'):
                res['updated_cfg']['fail_count']   = 0
                res['updated_cfg']['failed_since'] = None
            elif new_status == 'failed':
                try:
                    prev_fc = int(import_cfg.get('fail_count') or 0)
                except (TypeError, ValueError):
                    prev_fc = 0
                res['updated_cfg']['fail_count'] = min(prev_fc + 1, FAIL_COUNT_CAP)
                if not import_cfg.get('failed_since'):
                    res['updated_cfg']['failed_since'] = int(time.time())
                else:
                    # Preserve the original failure-run start (don't reset on
                    # subsequent failures). The agent-validated rule: the
                    # timer reflects how long the SOURCE has been broken,
                    # not how recently a retry attempt failed. Operator
                    # force-Refresh that fails again does NOT restart the
                    # abandon clock.
                    res['updated_cfg']['failed_since'] = int(import_cfg.get('failed_since'))

            per_import_results[iid] = res
            if res['status'] in ('ok', 'unchanged'):
                parsed_imports_for_cascade.append((res['updated_cfg'], res['parsed']))

        # Cascade-allocate channel numbers across all parsed imports
        # plus existing channels (for hard pins + sticky)
        allocations = self.cascadeAllocate(parsed_imports_for_cascade, existing_channels)

        # madteevee (imports fork): per-import-config lookup so the all_imported_now
        # loop below has the right import_cfg for each iid. Previously the loop
        # referenced `import_cfg` which dangled from the cascade-build loop above
        # (latent bug — line 520 always pulled the LAST import's name for every
        # channel's group fallback).
        import_cfg_by_id = {ic.get('id'): ic for ic in (imports or []) if ic.get('id')}

        # madteevee (imports fork): catchup attribute derivation per import.
        # Operator can set `catchup_mode` + `catchup_query_format` + `catchup_days`
        # per import in channels.json's `imports` array (via the import-config
        # edit endpoint). When unset, fall back to known-host defaults — currently
        # just movistar+. To extend to another catchup-capable plugin, add an
        # entry to KNOWN_CATCHUP_DEFAULTS. The data flow is: import config →
        # _resolveCatchup → per-channel `catchup` + `catchup-source` fields,
        # which get emitted into pseudotv.m3u and pvr.iptvsimple uses them to
        # offer "Play this program" + invoke the source plugin directly with
        # `&start_time=...&end_time=...` appended. Live playback still routes
        # through pseudotv.imports' plugin handler; only catchup bypasses us.
        KNOWN_CATCHUP_DEFAULTS = {
            # source_url prefix → (catchup_mode, catchup_query_format, catchup_days)
            # catchup_mode="default" (NOT "vod"). For "vod" mode, pvr.iptvsimple
            # only substitutes `{catchup-id}` in catchup-source — `{utc}` /
            # `{utcend}` placeholders stay literal. movistarplus then receives
            # an invalid URL like `?...&start_time={utc}&end_time={utcend}`,
            # parses no valid time window, and falls back to live. The user-
            # visible symptom: past-EPG clicks load live instead of catchup.
            # "default" mode appends the INSTANCE-LEVEL `catchupQueryFormat`
            # (operator-set in pvr.iptvsimple instance-settings.xml) to the
            # channel's main URL at catchup time, with `{utc}/{utcend}`
            # substituted from the clicked EPG event. This is what movistar+'s
            # OWN auto-generated iptvsimple instance uses, and is what worked
            # for the operator pre-migration. The per-channel catchup-source
            # we emit is harmless in "default" mode (iptvsimple ignores it).
            # Operator action required: in pvr.iptvsimple/instance-settings-
            # 50002.xml, set catchupQueryFormat to
            # `&start_time={utc}&end_time={utcend}`. Custom-channel catchup
            # (which uses catchup="vod" + per-channel catchup-source with
            # `{catchup-id}` token) is unaffected — those channels use vod
            # mode independently.
            'plugin://plugin.video.movistarplus/': ('default', '&start_time={utc}&end_time={utcend}', 5),
        }
        def _resolveCatchup(import_cfg, source_url):
            """Return (catchup_mode, catchup_source_url, catchup_days) for a single channel.
            Empty mode means the channel has no catchup; caller should emit catchup="".
            """
            cfg_mode = import_cfg.get('catchup_mode')
            cfg_qf   = import_cfg.get('catchup_query_format')
            cfg_days = import_cfg.get('catchup_days')
            if cfg_mode is None or cfg_qf is None or cfg_days is None:
                for prefix, (dm, dqf, dd) in KNOWN_CATCHUP_DEFAULTS.items():
                    if (source_url or '').startswith(prefix):
                        if cfg_mode is None: cfg_mode = dm
                        if cfg_qf   is None: cfg_qf   = dqf
                        if cfg_days is None: cfg_days = dd
                        break
            cfg_mode = cfg_mode or ''
            cfg_qf   = cfg_qf or ''
            if not cfg_mode or not cfg_qf or not source_url:
                return ('', '', cfg_days or 0)
            return (cfg_mode, source_url + cfg_qf, cfg_days or 0)

        # Merge allocations into channels list:
        #  - For new channels: assign number + assigned_number, type='import', enabled=True, etc.
        #  - For refreshed channels: update assigned_number; if not operator-pinned, sync number too
        #  - For orphans: keep in list with is_orphan=True
        all_imported_now = {}  # {channel_id: full channel record}
        for iid, res in per_import_results.items():
            import_cfg = import_cfg_by_id.get(iid) or {}
            for new in res['new']:
                cid = new['id']
                n   = allocations.get(cid)
                if n is None:
                    new['unallocated'] = True
                    new['enabled']     = False
                else:
                    new['number']           = n
                    new['assigned_number']  = n
                    new['enabled']          = True
                    new['unallocated']      = False
                new['type']                = 'import'
                new['changed']             = True
                new['favorite']            = new.get('favorite', False)
                new['operator_overrides']  = []
                new['is_orphan']           = False
                new.setdefault('logo', '')
                new['url']                 = new.get('source_url', '')
                # Preserve group as a list (already from parser)
                new['group']               = new.get('group') or [import_cfg.get('name', iid) or iid]
                # Apply catchup attrs (per-import config + known-host defaults)
                _cm, _cs, _cd = _resolveCatchup(import_cfg, new.get('source_url', ''))
                if _cm:
                    new['catchup']        = _cm
                    # madteevee (imports fork): for "default" catchup mode,
                    # explicitly CLEAR catchup-source. pvr.iptvsimple's mode-
                    # detection treats any channel with a non-empty
                    # catchup-source as VOD mode regardless of the channel's
                    # `catchup` attribute. For "default" mode to actually
                    # use the instance-level catchupQueryFormat (with {utc}/
                    # {utcend} substitution), the per-channel catchup-source
                    # must be EMPTY. iptvsimple then composes the catchup URL
                    # at play-time as: channel.url + instance.catchupQueryFormat.
                    # For "vod"/"shift"/"fs"/"xc" modes we still emit the
                    # per-channel catchup-source so iptvsimple uses it directly.
                    if _cm == 'default':
                        new['catchup-source'] = ''
                    else:
                        new['catchup-source'] = _cs
                    if _cd: new['catchup-days'] = str(_cd)
                # Persist into per-channel record
                all_imported_now[cid] = new
            for existing, _parsed in res['refreshed']:
                cid = existing['id']
                n   = allocations.get(cid)
                if n is not None:
                    existing['assigned_number'] = n
                    if 'number' not in (existing.get('operator_overrides') or []):
                        existing['number'] = n
                # Keep URL synced from source unless operator overrode it
                if 'url' not in (existing.get('operator_overrides') or []):
                    existing['url'] = existing.get('source_url') or existing.get('url', '')
                # Refresh catchup attrs unless operator overrode them
                overrides = existing.get('operator_overrides') or []
                _cm, _cs, _cd = _resolveCatchup(import_cfg, existing.get('source_url', ''))
                if _cm and 'catchup' not in overrides:
                    existing['catchup']        = _cm
                    # Same default-mode catchup-source clearing as new-channel arm
                    if _cm == 'default':
                        existing['catchup-source'] = ''
                    else:
                        existing['catchup-source'] = _cs
                    if _cd: existing['catchup-days'] = str(_cd)
                all_imported_now[cid] = existing
            for orphan in res['orphans']:
                cid = orphan['id']
                # Preserve orphan record; cascade may have assigned it a bucket
                # number (9000+) if disabled — apply that here so UI sort places
                # disabled channels at the bottom.
                if cid not in all_imported_now:
                    orphan['is_orphan'] = True
                    n = allocations.get(cid)
                    if isinstance(n, int):
                        orphan['assigned_number'] = n
                        if 'number' not in (orphan.get('operator_overrides') or []):
                            orphan['number'] = n
                    all_imported_now[cid] = orphan

        # Build merged channel list: keep all non-import channels as-is,
        # replace/add imported channels.
        merged_channels = [c for c in existing_channels if c.get('type') != 'import']
        merged_channels.extend(all_imported_now.values())

        # madteevee (imports fork): union of all tombstones across all imports.
        # These are channel ids the operator explicitly tombstoned (via the
        # "🗑 Orphans" endpoint). Without passing them as `deleted_ids` to
        # setChannels, the merge-on-write preserve loop resurrects them from
        # disk on every syncAll cycle — operator's delete looks like a no-op.
        deleted_ids = set()
        for ic in imports:
            for cid in (ic.get('tombstones') or []):
                deleted_ids.add(cid)

        # Persist channels.json (merge-on-write protects operator edits)
        modified_ids = set(all_imported_now.keys())
        try:
            self.channels.setChannels(channels=merged_channels, modified_ids=modified_ids, deleted_ids=deleted_ids)
        except TypeError:
            # Backward-compat: older signature without deleted_ids
            try:
                self.channels.setChannels(channels=merged_channels, modified_ids=modified_ids)
            except TypeError:
                self.channels.setChannels(channels=merged_channels)

        # Persist updated import configs (etag/last_modified/last_status etc.)
        try:
            updated_imports = [
                per_import_results[ic.get('id')]['updated_cfg']
                if ic.get('id') in per_import_results
                else ic
                for ic in imports
            ]
            self.channels.setImports(updated_imports)
        except Exception as e:
            self.log('syncAll, setImports failed: %s' % e, level=xbmc.LOGWARNING)

        # Per-channel logo cache (Imports_Cache_Logos toggle).
        # Rewrites channel['logo'] from a remote URL to an absolute fs path
        # so IPTV Simple/Kodi PVR loads logos locally instead of re-fetching
        # from upstream every refresh. Walk runs BEFORE the M3U push below
        # so the rewritten path lands in pseudotv.m3u. Failures fall back
        # to the original URL — no regression vs the no-cache path.
        cache_logos = False
        try:
            cache_logos = SETTINGS.getSettingBool('Imports_Cache_Logos')
        except Exception:
            cache_logos = False
        if cache_logos:
            try:
                refresh_mode = int(SETTINGS.getSettingInt('Imports_Logo_Refresh') or 0)
            except Exception:
                refresh_mode = 0
            validators = self._logoLoadValidators()
            cached_count = 0
            for ch in all_imported_now.values():
                if ch.get('is_orphan') or ch.get('unallocated') or not ch.get('enabled', True):
                    continue
                cid = ch.get('id')
                src = ch.get('logo') or ''
                if not cid or not src:
                    continue
                local = self._cacheLogo(cid, src, refresh_mode, validators)
                if local:
                    # imports.44: portable URL wrap — see _portableLogoURL.
                    ch['logo'] = self._portableLogoURL(local)
                    cached_count += 1
            self._logoSaveValidators(validators)
            # Evict logos for channels no longer present
            self._evictOrphanLogos({ch.get('id') for ch in all_imported_now.values() if ch.get('id')})
            self.log('syncAll, logo cache: rewrote %d channel logos to portable HTTP URLs (mode=%d)'
                     % (cached_count, refresh_mode), level=xbmc.LOGINFO)

        # Push imported channels into in-memory M3U/XMLTV
        if self.m3u is not None:
            for ch in all_imported_now.values():
                # madteevee (imports fork): disabled / orphan / unallocated
                # channels must be EXPLICITLY removed from M3UDATA, not just
                # skipped. Skipping leaves stale entries (e.g., a channel
                # disabled in channels.json with number=9036 keeps its old
                # live-range #57 entry in pseudotv.m3u indefinitely, and
                # Kodi PVR keeps showing it). delStation removes by id.
                if ch.get('is_orphan') or ch.get('unallocated') or not ch.get('enabled', True):
                    try: self.m3u.delStation(ch)
                    except Exception: pass
                    continue
                station = self._channelToStation(ch)
                try: self.m3u.addStation(station)
                except Exception as e:
                    self.log('syncAll, addStation [%s] failed: %s' % (ch.get('id'), e),
                             level=xbmc.LOGWARNING)

        if self.xmltv is not None:
            # Per-import EPG: rewrite source-side IDs to namespaced before adding.
            # Match strategy mirrors IPTV Simple's documented chain:
            #   1. XMLTV `id` attr      vs M3U `tvg-id`            (exact)
            #   2. XMLTV `id` attr      vs M3U `tvg-name` / name   (normalized fallback)
            #   3. XMLTV `display-name` vs M3U `tvg-name` / name   (normalized fallback)
            # Normalization: lowercase + strip ' hd'/' sd' suffix + collapse whitespace.
            # Without this fallback, EPG sources that use human-readable display names
            # as channel ids (davidmuma EPG_dobleM, many regional EPGs) silently fail
            # to match M3U sources that use short codes (MovistarPlus, IPTV-org).
            def _norm(s):
                if not s: return ''
                s = s.lower().strip()
                # strip common quality suffixes
                for sfx in (' hd', ' sd', ' fhd', ' uhd', ' 4k'):
                    if s.endswith(sfx): s = s[:-len(sfx)]
                # collapse whitespace
                s = re.sub(r'\s+', ' ', s).strip()
                return s

            # madteevee (imports fork): build the SAME enabled-channel filter
            # the M3U branch applies at line 630, but apply it on the XML side
            # too. Without this, the XML branch happily appends programmes for
            # disabled/orphan/unallocated channels — they go into pseudotv.xml
            # and stay there forever (cleanSelf only drops channels for imports
            # that are entirely removed, not channels disabled within a still-
            # registered import). Empirical measurement 2026-05-11: 22 MB of
            # the 85 MB pseudotv.xml (26%) was programme data for 25 disabled
            # movistar+ channels. pvr.iptvsimple parses + indexes the whole
            # file on every reload, so this is real I/O + memory cost. Also,
            # if a disabled channel later gets re-enabled, its stale EPG (now
            # hours-to-days old) would be served from disk until the next
            # syncAll refreshes it.
            enabled_ids = {
                ch.get('id') for ch in all_imported_now.values()
                if ch.get('id')
                and ch.get('enabled', True)
                and not ch.get('is_orphan')
                and not ch.get('unallocated')
            }

            for iid, res in per_import_results.items():
                id_map = res['epg_id_map']
                if not id_map:
                    continue
                # Build per-import name_map for fallback. Look up by normalized
                # name → namespaced id. Includes both source name and tvg-id-as-name
                # since some imports (epg.best) use short codes for both.
                name_map = {}
                for ch in (res['parsed'] or []):
                    cid = ch.get('id')
                    if not cid: continue
                    for cand in (ch.get('name'), ch.get('epg_id')):
                        n = _norm(cand)
                        if n and n not in name_map:
                            name_map[n] = cid
                self.log('syncAll, [%s] EPG matcher built id_map=%d name_map=%d (sample names: %s)'
                         % (iid, len(id_map), len(name_map), list(name_map.keys())[:5]),
                         level=xbmc.LOGINFO)

                def _match(src_id, item):
                    """Resolve namespaced id from source-side XMLTV id + element attrs."""
                    # 1. Exact tvg-id
                    if src_id in id_map: return id_map[src_id]
                    # 2. Normalized id-as-name
                    n = _norm(src_id)
                    if n in name_map: return name_map[n]
                    # 3. Normalized display-name (only available on `channel` elements;
                    #    elem_to_channel returns display-name as a list of (text, lang) tuples)
                    for dn in (item.get('display-name') or []):
                        text = dn[0] if isinstance(dn, (list, tuple)) and dn else dn
                        n = _norm(text)
                        if n in name_map: return name_map[n]
                    return None

                # Bypass XMLTVS.addChannel / addProgram (which expect PseudoTV's
                # internal scheduling format with categories/thumb/length etc.).
                # The xmltv.parseExternalSource yields XMLTV-format dicts —
                # the SAME shape XMLTVDATA stores natively (xmltvs.py:78-81 _save
                # passes them directly to xmltv.Writer.addChannel/addProgramme).
                # Direct append to XMLTVDATA preserves the format end-to-end.
                xtv_channels   = self.xmltv.XMLTVDATA.setdefault('channels', [])
                xtv_programmes = self.xmltv.XMLTVDATA.setdefault('programmes', [])

                # madteevee (imports fork): drop ALL stale entries for this
                # import's namespace BEFORE appending the freshly-matched ones.
                # Without this, the existing entries (loaded from disk by
                # XMLTVS.__init__) for channels that got disabled since the last
                # cycle would survive forever — our append loop only filters NEW
                # matches against enabled_ids. Stripping by namespace suffix
                # leaves all OTHER imports' entries untouched.
                import_ns_suffix = '@%s' % iid
                pruned_chan = pruned_prog = 0
                kept_chans = []
                for c in xtv_channels:
                    if isinstance(c.get('id'), str) and c['id'].endswith(import_ns_suffix):
                        pruned_chan += 1
                        continue
                    kept_chans.append(c)
                xtv_channels[:] = kept_chans
                kept_progs = []
                for p in xtv_programmes:
                    if isinstance(p.get('channel'), str) and p['channel'].endswith(import_ns_suffix):
                        pruned_prog += 1
                        continue
                    kept_progs.append(p)
                xtv_programmes[:] = kept_progs
                if pruned_chan or pruned_prog:
                    self.log('syncAll, [%s] pruned %d channels + %d programmes from prior cycle (will re-append fresh matches honoring enabled-state)'
                             % (iid, pruned_chan, pruned_prog), level=xbmc.LOGINFO)

                matched_chan = matched_prog = skipped = 0
                skipped_disabled = 0
                for kind, item in res['epg_pairs']:
                    if kind == 'channel':
                        src_id = item.get('id')
                        ns_id  = _match(src_id, item)
                        if not ns_id:
                            skipped += 1; continue
                        # madteevee: drop EPG for disabled/orphan/unallocated
                        # channels — symmetric with the M3U branch's delStation
                        # filter at line 630. Without this, pseudotv.xml carries
                        # ~26% bookkeeping bloat for channels the M3U never
                        # exposes (pvr.iptvsimple has nothing to match the EPG
                        # to). See measurement note in enabled_ids comment.
                        if ns_id not in enabled_ids:
                            skipped_disabled += 1; continue
                        item['id'] = ns_id
                        # Logo-cache reach into XMLTV: if the matched
                        # channel's M3U logo was rewritten to a local fs
                        # path (cache_logos toggle), point the XMLTV <icon>
                        # at the same path. Kodi's EPG-guide view loads
                        # icons from XMLTV separately from the M3U
                        # tvg-logo, and upstream URLs (e.g. Wikimedia)
                        # often fail under Kodi's default UA.
                        ch_rec = all_imported_now.get(ns_id) or {}
                        cached = ch_rec.get('logo') or ''
                        if cached and not is_remote_url(cached):
                            item['icon'] = [{'src': cached}]
                        xtv_channels.append(item)
                        matched_chan += 1
                    elif kind == 'programme':
                        src_id = item.get('channel')
                        ns_id  = _match(src_id, item)
                        if not ns_id:
                            skipped += 1; continue
                        # Same disabled-filter as the channel arm above
                        if ns_id not in enabled_ids:
                            skipped_disabled += 1; continue
                        item['channel'] = ns_id
                        xtv_programmes.append(item)
                        matched_prog += 1
                self.log('syncAll, [%s] EPG match: channels=%d programmes=%d skipped=%d skipped-disabled=%d'
                         % (iid, matched_chan, matched_prog, skipped, skipped_disabled),
                         level=xbmc.LOGINFO)

                # Conditional-GET cache safety: if we DID fetch fresh EPG data
                # (epg_pairs non-empty) but NONE matched, clear the cache validators
                # so next sync re-fetches. Otherwise the server's 304 response would
                # forever pin our XML to an empty matched state, even after operator
                # adds the per-channel `epg_id` overrides that would let it match.
                if res['epg_pairs'] and matched_chan == 0 and matched_prog == 0:
                    res['updated_cfg']['epg_etag']          = None
                    res['updated_cfg']['epg_last_modified'] = None
                    self.log('syncAll, [%s] cleared EPG cache validators (0 matched of %d items)'
                             % (iid, len(res['epg_pairs'])), level=xbmc.LOGINFO)

        # Force-flush merged XMLTVDATA when running under a writable XMLTVS
        # (Builder.buildChannels path). Builder's per-channel addProgramme
        # calls trigger debounced _save's that fire BEFORE this syncAll
        # finishes — so disk has the post-cleanSelf, pre-syncAll-merge state
        # (imports' EPG stripped). After our merge appends to XMLTVDATA the
        # debounce isn't kicked again (we mutate the lists directly, not via
        # addProgramme), so without this flush Builder leaves disk in the
        # imports-stripped state until the next chkImports cycle ~5 min later
        # — visible as the EPG oscillating in/out of Kodi PVR.
        # chkImports uses writable=False + its own _writeAtomic, so this branch
        # short-circuits there.
        if (self.xmltv is not None
            and getattr(self.xmltv, 'writable', False)
            and self.service is not None
            and hasattr(self.service, 'tasks')):
            try:
                # madteevee (imports fork): before flushing, merge in any
                # `@PseudoTV_Live` channels/programmes that Builder may have
                # written via `__setStation`'s `_writeAtomic` in parallel
                # to this syncAll. Without this merge, syncAll's
                # XMLTVDATA (which only holds imports) overwrites Builder's
                # Custom-channel programmes — Saturday Morning TV's 500
                # programmes vanished from pseudotv.xml within seconds of
                # being written. Read disk fresh, extract Custom-suffix
                # items, append the ones we don't already have.
                try:
                    from xmltvs import XMLTVS as _Xdisk
                    disk = _Xdisk(writable=False)
                    pseudo_slug = '@PseudoTV_Live'
                    existing_ch_ids = {c.get('id') for c in self.xmltv.XMLTVDATA.get('channels', []) if c.get('id')}
                    existing_prog_keys = {(p.get('channel'), p.get('start'))
                                          for p in self.xmltv.XMLTVDATA.get('programmes', [])}
                    added_chans = 0
                    added_progs = 0
                    for ch in (disk.XMLTVDATA.get('channels') or []):
                        cid = ch.get('id') or ''
                        if cid.endswith(pseudo_slug) and cid not in existing_ch_ids:
                            self.xmltv.XMLTVDATA.setdefault('channels', []).append(ch)
                            existing_ch_ids.add(cid)
                            added_chans += 1
                    for prog in (disk.XMLTVDATA.get('programmes') or []):
                        pcid = prog.get('channel') or ''
                        if pcid.endswith(pseudo_slug):
                            key = (pcid, prog.get('start'))
                            if key not in existing_prog_keys:
                                self.xmltv.XMLTVDATA.setdefault('programmes', []).append(prog)
                                existing_prog_keys.add(key)
                                added_progs += 1
                    if added_chans or added_progs:
                        self.log('syncAll, merged %d Custom channels + %d programmes from disk before flush'
                                 % (added_chans, added_progs), level=xbmc.LOGINFO)
                    del disk
                except Exception as e:
                    self.log('syncAll, Custom-merge from disk failed (force-flush will proceed without it): %s'
                             % e, level=xbmc.LOGWARNING)

                # imports.13 / C#10 Follow-up A: renderers + write_atomic
                # moved to module-level resources/lib/renderers.py (used to
                # be self.service.tasks._renderXMLTV / _writeAtomic).
                from renderers import render_xmltv, write_atomic
                rendered = render_xmltv(self.xmltv.XMLTVDATA)
                if rendered is None:
                    # Empty-data guard fired in render_xmltv; skip write
                    # so we don't overwrite disk with the self-closing
                    # `<tv/>` corruption shape.
                    self.log('syncAll, force-flush skipped (empty XMLTVDATA); disk content preserved',
                             level=xbmc.LOGWARNING)
                else:
                    # imports.42: pass channel_count for write_atomic's
                    # circuit-breaker + LOGINFO size message.
                    channel_count = (len(self.xmltv.XMLTVDATA.get('channels')   or [])
                                   + len(self.xmltv.XMLTVDATA.get('recordings') or []))
                    write_atomic(XMLTVFLEPATH, rendered, channel_count=channel_count)
                    self.log('syncAll, force-flushed merged XMLTVDATA to disk (%d bytes)'
                             % len(rendered), level=xbmc.LOGINFO)
            except Exception as e:
                self.log('syncAll, force-flush failed: %s' % e, level=xbmc.LOGWARNING)

        # Symmetric force-flush for M3U. Same race as XML above:
        # Builder's per-channel addStation calls fire debounced M3U._save's
        # during the build loop; once syncAll's addStation calls finish,
        # the debounce may have already fired or may not re-fire before the
        # XMLTVS instance is GC'd. End result: pseudotv.m3u on disk misses
        # either the just-built PseudoTV channels (if first save was empty)
        # OR the imports (if second save loaded a stale snapshot). Writing
        # atomically here, after both PseudoTV-built channels and import
        # stations have been pushed into M3UDATA, ensures the file matches
        # the final merged state.
        if (self.m3u is not None
            and getattr(self.m3u, 'writable', False)):
            # imports.13 / C#10 Follow-up A: renderers moved to module-level.
            # The `self.service.tasks` precondition (was needed when renderers
            # lived on Tasks) is no longer required — render_m3u + write_atomic
            # are callable from any context.
            try:
                from renderers import render_m3u, write_atomic
                rendered = render_m3u(self.m3u.M3UDATA)
                # rendered=None means refuse-empty-write guard fired; skip
                # to preserve disk content rather than wipe to header-only.
                if rendered is not None:
                    # imports.42: pass channel_count for write_atomic's
                    # circuit-breaker + LOGINFO size message.
                    station_count = (len(self.m3u.M3UDATA.get('stations')   or [])
                                   + len(self.m3u.M3UDATA.get('recordings') or []))
                    write_atomic(M3UFLEPATH, rendered, channel_count=station_count)
                    self.log('syncAll, force-flushed merged M3UDATA to disk (%d bytes)'
                             % len(rendered), level=xbmc.LOGINFO)
                else:
                    self.log('syncAll, skipped M3U force-flush — empty M3UDATA (would wipe disk M3U)', level=xbmc.LOGWARNING)
            except Exception as e:
                self.log('syncAll, M3U force-flush failed: %s' % e, level=xbmc.LOGWARNING)

        # imports.22: only signal chkPVRRefresh when this syncAll cycle actually
        # changed disk state. Three change-signals jointly describe "bytes on disk
        # differ from before this cycle":
        #   (a) any syncOne returned status='ok' — a fresh HTTP 200 was fetched +
        #       parsed. Content MAY still be byte-identical to the prior cycle (server
        #       didn't honour ETag/If-Modified-Since) but cheap to over-signal vs.
        #       byte-comparing multi-MB M3U/XMLTV bodies.
        #   (b) any tombstone was processed — `deleted_ids` (computed at line 651) is
        #       the union of `import_cfg['tombstones']` across all imports;
        #       bool(deleted_ids) means setChannels' merge-on-write either stripped
        #       records or carried deletion markers forward — either way,
        #       channels.json changed.
        #   (c) any reconcile observed orphans — `res['orphans']` is the diff between
        #       parsed-now and existing channels for an import; non-empty means
        #       is_orphan=True got written to channels.json.
        # All-'unchanged'/'disabled' AND no tombstones AND no orphans is the genuine
        # no-op cycle: every import returned HTTP 304, no operator deletion, nothing
        # fell out of source. Skipping setPropTimer here is correctness-safe because:
        #   - chkImports already runs every 5 min on its own daemon;
        #   - iptvsimple polls m3uUrl every m3uRefreshIntervalMins (default 15)
        #     regardless of whether we signal it;
        #   - this signal's only consumer (tasks.py:802 chkPVRRefresh) calls PVRScan
        #     (no-op against iptvsimple) and, until imports.22, bounced our HTTP
        #     server (also no-op against iptvsimple).
        # Reduces _chkPropTimer ticks, downstream queue churn, and the PVRScan
        # JSON-RPC round-trip on every cycle. Genuine state-change callers of
        # setPropTimer('chkPVRRefresh') outside syncAll (builder.py:477,
        # manager.py:1045, context_record.py:58/76, multiroom.py:165, kodi.py:519,
        # default.py:64, tasks.py:798) are NOT changed — they fire from paths that
        # always mutate state, so their unconditional signal is correct.
        changed = (
            any(r.get('status') == 'ok' for r in per_import_results.values())
            or bool(deleted_ids)
            or any(r.get('orphans') for r in per_import_results.values())
        )
        if changed:
            try: PROPERTIES.setPropTimer('chkPVRRefresh')
            except Exception: pass
        else:
            self.log('syncAll, no disk change this cycle — skipping chkPVRRefresh signal '
                     '(all imports status=unchanged/disabled, no tombstones, no orphans)',
                     level=xbmc.LOGINFO)

        # Summary log
        for iid, res in per_import_results.items():
            self.log('syncAll, [%s] status=%s new=%d refreshed=%d orphan=%d epg=%d'
                     % (iid, res['status'], len(res['new']), len(res['refreshed']),
                        len(res['orphans']), len(res['epg_pairs'])),
                     level=xbmc.LOGINFO)

        return per_import_results


    def _channelToStation(self, ch):
        """Map an imported channel record → M3U station dict for `m3u.addStation`.

        Mirrors the M3U_TEMP shape (m3u.py:26) plus `url` and `label`.
        Imported channels' `tvg-id` in the output M3U is the namespaced form
        (`<source_tvg_id>@<import_id>`) — this prevents IPTV Simple's tvg-id
        dedup from silently dropping channels with same source-side tvg-id
        across multiple imports (plan G1).

        CRITICAL — bool/empty-string semantics:
        M3U._save's optional-attrs loop (m3u.py:226-232) emits ANY key with
        a truthy `str(value)`. `str(False)` is `"False"` (TRUTHY string) → if
        we set `media: False`, output gets `media="False"` → IPTV Simple
        treats the channel as a media item (VOD/recording), NOT a live channel
        (verified empirically: 91 channels misclassified as 91 media items).
        Solution: pass empty strings instead of `False` for the OPTIONAL
        attrs (`media`, `media-dir`, `media-size`, `realtime`, `favorite`,
        `provider-*`, etc.) — they only emit when source actually provides
        a non-empty value. Required attrs (`radio`, `catchup`) stay bool
        because m3u.py's hardcoded line template at m3u.py:218 always
        substitutes them via positional %s.
        """
        def _opt_bool(k):
            """For OPTIONAL bool attrs: empty str when False (don't serialize); the actual bool when True."""
            v = ch.get(k)
            return v if v else ''
        def _opt_str(k):
            v = ch.get(k)
            return v if v else ''
        return {
            'id'                : ch.get('id', ''),  # NAMESPACED — already from parseM3U
            'number'            : ch.get('number', 0),
            'name'              : ch.get('name', ''),
            'logo'              : ch.get('logo', ''),
            'group'             : list(ch.get('group') or []),
            # `radio` and `catchup` are positional in m3u.py:218 line template; always emitted.
            'radio'             : bool(ch.get('radio', False)),
            'catchup'           : ch.get('catchup', ''),
            'label'             : ch.get('name', ''),
            'url'               : ch.get('source_url', '') or ch.get('url', ''),
            # OPTIONAL attrs — empty-string when missing/False so M3U._save's
            # truthy-str check doesn't emit them with literal "False" values.
            'favorite'          : _opt_bool('favorite'),
            'realtime'          : _opt_bool('realtime'),
            'media'             : _opt_bool('media'),
            'media-dir'         : _opt_str('media-dir'),
            'media-size'        : _opt_str('media-size'),
            'tvg-shift'         : _opt_str('tvg-shift'),
            'catchup-source'    : _opt_str('catchup-source'),
            'catchup-days'      : _opt_str('catchup-days'),
            'catchup-correction': _opt_str('catchup-correction'),
            'provider'          : _opt_str('provider'),
            'provider-type'     : _opt_str('provider-type'),
            'provider-logo'     : _opt_str('provider-logo'),
            'provider-countries': _opt_str('provider-countries'),
            'provider-languages': _opt_str('provider-languages'),
            'x-playlist-type'   : _opt_str('x-playlist-type'),
            'kodiprops'         : list(ch.get('kodiprops') or []),
            'extvlcopt'         : list(ch.get('extvlcopt') or []),
            'webprops'          : list(ch.get('webprops') or []),
        }

    def fetchSource(self, url_or_path, etag=None, last_modified=None,
                    timeout=15, chunk_size=65536):
        """
        Conditional fetch — local file or remote URL.

        Returns: dict {
            'status'       : int,           # 200 ok, 304 unchanged, 4xx/5xx fail, 0 local-error
            'content'      : bytes or None, # None for 304 / failure
            'etag'         : str or None,   # remote only; pass back next call
            'last_modified': str or None,   # remote: HTTP header value
                                            # local:  str(stat.st_mtime)
            'error'        : str or None,   # populated on failure
        }

        Local file (`special://`, absolute path): stat mtime; if mtime <=
        `last_modified` (epoch float as string), returns 304 with content=None.
        Otherwise returns full bytes + new mtime as last_modified.

        Remote URL (http/https): conditional GET via `If-None-Match` /
        `If-Modified-Since`. Chunked download with `_interrupt()` polling
        every chunk so Kodi shutdown can interrupt long fetches (plan G15).
        Backoff/retry handled by `requests.adapters.HTTPAdapter` with `Retry`.
        """
        # Validate URL scheme upfront
        try:
            validate_source_url(url_or_path)
        except ValueError as e:
            return {'status': 0, 'content': None, 'etag': None,
                    'last_modified': None, 'error': str(e)}

        if is_remote_url(url_or_path):
            return self._fetchRemote(url_or_path, etag, last_modified, timeout, chunk_size)
        else:
            return self._fetchLocal(url_or_path, last_modified)

    def _fetchLocal(self, path, last_modified):
        """Read a local file; honor mtime for conditional fetch."""
        try:
            # Resolve special:// via FileAccess
            real_path = FileAccess.translatePath(path) if path.startswith('special://') else path
            if not FileAccess.exists(path):
                return {'status': 0, 'content': None, 'etag': None,
                        'last_modified': None,
                        'error': 'local source not found: %s' % path}

            # mtime via xbmcvfs.Stat (works through Kodi VFS layer)
            try:
                stat_obj = xbmcvfs.Stat(path)
                mtime = float(stat_obj.st_mtime())
            except Exception:
                # Fallback to os.stat on resolved path
                mtime = float(os.stat(real_path).st_mtime)

            if last_modified is not None:
                try:
                    if mtime <= float(last_modified):
                        return {'status': 304, 'content': None, 'etag': None,
                                'last_modified': str(mtime), 'error': None}
                except (ValueError, TypeError):
                    pass  # invalid stored last_modified — fall through to read

            # Read raw bytes — fall back to vanilla open() if FileAccess wrapper
            # returns str (test-stub or unexpected encoding handling).
            try:
                with FileAccess.stream(path, 'rb') as f:
                    content = f.read()
            except Exception:
                with open(real_path, 'rb') as f:
                    content = f.read()
            if isinstance(content, str):
                content = content.encode('utf-8')

            return {'status': 200, 'content': content, 'etag': None,
                    'last_modified': str(mtime), 'error': None}
        except Exception as e:
            return {'status': 0, 'content': None, 'etag': None,
                    'last_modified': None,
                    'error': 'local fetch failed: %s' % e}

    def _fetchRemote(self, url, etag, last_modified, timeout, chunk_size):
        """HTTP fetch with conditional GET + chunked download + abort polling."""
        try:
            import requests
            from requests.adapters import HTTPAdapter, Retry
        except ImportError as e:
            return {'status': 0, 'content': None, 'etag': None,
                    'last_modified': None,
                    'error': 'requests module unavailable: %s' % e}

        session = requests.Session()
        retries = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
        adapter = HTTPAdapter(max_retries=retries)
        session.mount('http://',  adapter)
        session.mount('https://', adapter)

        headers = {}
        if etag:          headers['If-None-Match']     = etag
        if last_modified: headers['If-Modified-Since'] = last_modified

        # madteevee (imports fork): cap remote-fetch size at 100 MB. Without
        # this, an upstream that responds 200 OK and trickles bytes forever
        # (or claims an enormous Content-Length) would buffer until OOM on
        # the RPi5. 100 MB is well above any realistic M3U/XMLTV — movistar+
        # XMLTV is ~22 MB and the largest IPTV feeds we have seen are ~50 MB.
        MAX_FETCH_BYTES = 100 * 1024 * 1024

        try:
            with session.get(url, headers=headers, timeout=timeout, stream=True) as r:
                # 304 — unchanged; nothing to download.
                if r.status_code == 304:
                    return {'status': 304, 'content': None, 'etag': etag,
                            'last_modified': last_modified, 'error': None}

                if not (200 <= r.status_code < 300):
                    return {'status': r.status_code, 'content': None, 'etag': None,
                            'last_modified': None,
                            'error': 'HTTP %d for %s' % (r.status_code, url)}

                # Reject up-front if upstream declares an oversize body.
                try: declared = int(r.headers.get('Content-Length', '0') or '0')
                except (TypeError, ValueError): declared = 0
                if declared > MAX_FETCH_BYTES:
                    return {'status': 0, 'content': None, 'etag': None,
                            'last_modified': None,
                            'error': 'fetch aborted: declared %d bytes exceeds cap %d' % (declared, MAX_FETCH_BYTES)}

                # Chunked stream with abort polling (plan G15) + size cap.
                buf = bytearray()
                for chunk in r.iter_content(chunk_size=chunk_size):
                    if self.service is not None and hasattr(self.service, '_interrupt'):
                        if self.service._interrupt():
                            return {'status': 0, 'content': None, 'etag': None,
                                    'last_modified': None,
                                    'error': 'fetch interrupted (shutdown signaled)'}
                    if chunk:
                        buf.extend(chunk)
                        if len(buf) > MAX_FETCH_BYTES:
                            return {'status': 0, 'content': None, 'etag': None,
                                    'last_modified': None,
                                    'error': 'fetch aborted: stream exceeded cap %d bytes' % MAX_FETCH_BYTES}

                return {'status': 200, 'content': bytes(buf),
                        'etag': r.headers.get('ETag'),
                        'last_modified': r.headers.get('Last-Modified'),
                        'error': None}
        except Exception as e:
            return {'status': 0, 'content': None, 'etag': None,
                    'last_modified': None,
                    'error': 'remote fetch failed: %s' % e}
        finally:
            try: session.close()
            except Exception: pass

    # ------------------------------------------------------------------
    # Per-import EPG cache (step 5)
    # ------------------------------------------------------------------
    # Each import's most recent successful EPG fetch is stashed as raw
    # bytes at `CACHE_LOC/epg_<import_id>.xml`. On a 304 response, syncOne
    # hydrates `epg_pairs` from this file instead of leaving them empty —
    # which would otherwise let `_save` write an EPG that's missing this
    # source after a restart-then-304 sequence (XMLTVS._load.cleanSelf
    # strips non-`@PseudoTV_Live`-slug channels from in-memory state).
    # The parser auto-detects gzip via magic bytes, so we store the
    # server's response verbatim and let parseXMLTV do the right thing.

    def _epgCachePath(self, import_id):
        # CACHE_LOC is a Kodi special:// path at runtime (resolves to
        # ~/.kodi/userdata/addon_data/<id>/cache). os.makedirs / os.open
        # can't handle the URI form, so translate to a real fs path here.
        # In unit tests CACHE_LOC is a tmp_path (no scheme) — the test
        # xbmcvfs.translatePath stub returns non-special paths unchanged.
        return FileAccess.translatePath(os.path.join(CACHE_LOC, 'epg_%s.xml' % import_id))

    def _epgCacheWrite(self, import_id, content_bytes):
        """
        Atomically write fetched EPG bytes to the per-import cache file.

        Returns True on success, False on any I/O failure. Never raises.
        Caller MUST NOT update `epg_etag`/`epg_last_modified` in
        `updated_cfg` when this returns False — otherwise the next cycle
        will conditional-GET against validators whose payload isn't on
        disk, and on 304 fall through to "cache missing → force fetch"
        which costs one extra round-trip.
        """
        # madteevee (imports fork): reject empty bytes too — upstream that
        # responds 200 OK with Content-Length: 0 used to poison the per-import
        # EPG cache (empty file → next cycle's 304 load returned no channels).
        if not import_id or not content_bytes:
            return False
        target = self._epgCachePath(import_id)
        tmp    = target + '.tmp'
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            try:
                os.write(fd, content_bytes)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, target)
            self.log('_epgCacheWrite, [%s] wrote %d bytes to %s'
                     % (import_id, len(content_bytes), target))
            return True
        except Exception as e:
            self.log('_epgCacheWrite, [%s] failed: %s' % (import_id, e),
                     level=xbmc.LOGWARNING)
            try: os.unlink(tmp)
            except Exception: pass
            return False

    # ------------------------------------------------------------------
    # Per-channel logo cache (step 5)
    # ------------------------------------------------------------------
    # When `Imports_Cache_Logos` is on, fetched M3U `tvg-logo` URLs are
    # downloaded into CACHE_LOC/logos/ and the channel record's `logo`
    # field is rewritten to the absolute fs path before the M3U is written.
    # IPTV Simple / Kodi PVR then loads logos from disk instead of
    # re-fetching from upstream every refresh.
    #
    # Refresh policy (`Imports_Logo_Refresh`):
    #   0 = Fetch if missing  — write once, reuse forever
    #   1 = Conditional GET   — re-check each cycle via ETag/Last-Modified
    #   2 = Refetch every cycle — always download (~60 MB/hr; not recommended)
    #
    # Validators (etag/last-modified per channel) live alongside the cache
    # files at CACHE_LOC/logos/.validators.json. Loaded once per syncAll
    # cycle, mutated as we go, written back at end.

    def _logoCacheDir(self):
        return FileAccess.translatePath(os.path.join(CACHE_LOC, 'logos'))

    def _logoCachePath(self, channel_id, ext='img'):
        # Channel IDs already use safe chars (alnum + `_-.@`); @ is fine on
        # ext4/NTFS/APFS. Add ext for human inspection.
        return os.path.join(self._logoCacheDir(), '%s.%s' % (channel_id, ext))

    def _logoExtForUrl(self, url, content_type=None):
        # Try Content-Type first (authoritative), fall back to URL suffix.
        if content_type:
            ct = content_type.split(';', 1)[0].strip().lower()
            for prefix, ext in (('image/png', 'png'), ('image/jpeg', 'jpg'),
                                ('image/jpg', 'jpg'), ('image/webp', 'webp'),
                                ('image/gif', 'gif'), ('image/svg+xml', 'svg'),
                                ('image/x-icon', 'ico')):
                if ct == prefix or ct.startswith(prefix):
                    return ext
        # URL extension fallback
        try:
            path = urllib.parse.urlparse(url).path
            tail = path.rsplit('/', 1)[-1]
            if '.' in tail:
                ext = tail.rsplit('.', 1)[-1].lower()
                if 1 <= len(ext) <= 5 and ext.isalnum():
                    return ext
        except Exception:
            pass
        return 'img'

    def _logoLoadValidators(self):
        path = os.path.join(self._logoCacheDir(), '.validators.json')
        try:
            if not os.path.exists(path):
                return {}
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception as e:
            self.log('_logoLoadValidators failed: %s' % e, level=xbmc.LOGWARNING)
            return {}

    def _logoSaveValidators(self, validators):
        target = os.path.join(self._logoCacheDir(), '.validators.json')
        tmp    = target + '.tmp'
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(validators, f)
            os.replace(tmp, target)
        except Exception as e:
            self.log('_logoSaveValidators failed: %s' % e, level=xbmc.LOGWARNING)
            try: os.unlink(tmp)
            except Exception: pass

    def _findLogoFile(self, channel_id):
        """Return the existing on-disk cache path for channel_id, or None."""
        try:
            cache_dir = self._logoCacheDir()
            if not os.path.isdir(cache_dir):
                return None
            prefix = channel_id + '.'
            for name in os.listdir(cache_dir):
                if name.startswith(prefix) and not name.endswith('.tmp'):
                    return os.path.join(cache_dir, name)
        except Exception:
            pass
        return None

    def _fetchLogo(self, url, etag=None, last_modified=None, timeout=15):
        """
        Fetch a single logo via conditional GET. Logos are small (~kB to ~100kB)
        so unchunked + non-streaming is fine; no abort polling either since
        the request is bounded by `timeout`.

        Returns dict {'status', 'content', 'etag', 'last_modified', 'error'}.
        """
        try:
            import requests
        except ImportError as e:
            return {'status': 0, 'content': None, 'etag': None,
                    'last_modified': None, 'error': 'requests unavailable: %s' % e}
        # Wikimedia (upload.wikimedia.org) — and a few other providers —
        # block the default `python-requests/X.Y.Z` User-Agent. Send an
        # identifying UA per the Wikimedia UA policy
        # (https://meta.wikimedia.org/wiki/User-Agent_policy).
        headers = {
            'User-Agent': 'PseudoTV-imports/%s (Kodi addon; https://github.com/mmadalone/PseudoTV_Live)' % ADDON_VERSION,
        }
        if etag:          headers['If-None-Match']     = etag
        if last_modified: headers['If-Modified-Since'] = last_modified
        try:
            r = requests.get(url, headers=headers, timeout=timeout, stream=False)
            if r.status_code == 304:
                return {'status': 304, 'content': None, 'etag': etag,
                        'last_modified': last_modified,
                        'content_type': None, 'error': None}
            if not (200 <= r.status_code < 300):
                return {'status': r.status_code, 'content': None, 'etag': None,
                        'last_modified': None,
                        'content_type': None,
                        'error': 'HTTP %d for %s' % (r.status_code, url)}
            return {'status': 200, 'content': r.content,
                    'etag': r.headers.get('ETag'),
                    'last_modified': r.headers.get('Last-Modified'),
                    'content_type': r.headers.get('Content-Type'),
                    'error': None}
        except Exception as e:
            return {'status': 0, 'content': None, 'etag': None,
                    'last_modified': None, 'content_type': None,
                    'error': 'logo fetch failed: %s' % e}

    def _logoWriteAtomic(self, target, content_bytes):
        tmp = target + '.tmp'
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            try:
                os.write(fd, content_bytes)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, target)
            return True
        except Exception as e:
            self.log('_logoWriteAtomic failed for %s: %s' % (target, e),
                     level=xbmc.LOGWARNING)
            try: os.unlink(tmp)
            except Exception: pass
            return False

    def _cacheLogo(self, channel_id, source_url, refresh_mode, validators):
        """
        Apply refresh policy and return the absolute fs path of the cached
        logo on success, or None on any failure (caller keeps the original URL).
        Mutates `validators` dict in place when fetches succeed.
        """
        if not source_url or not is_remote_url(source_url):
            return None
        existing = self._findLogoFile(channel_id)

        if refresh_mode == 0 and existing:
            # Fetch if missing — already present, no work
            return existing

        cv = (validators.get(channel_id) or {}) if isinstance(validators, dict) else {}
        send_etag = cv.get('etag')          if refresh_mode == 1 else None
        send_lm   = cv.get('last_modified') if refresh_mode == 1 else None

        fetched = self._fetchLogo(source_url, etag=send_etag, last_modified=send_lm)
        if fetched['status'] == 304 and existing:
            return existing
        if fetched['status'] != 200 or not fetched.get('content'):
            self.log('_cacheLogo, [%s] skipped (status=%s, error=%s, url=%s)'
                     % (channel_id, fetched.get('status'),
                        fetched.get('error'), source_url),
                     level=xbmc.LOGWARNING)
            return existing  # may be None; caller falls back to URL
        ext      = self._logoExtForUrl(source_url, fetched.get('content_type'))
        target   = self._logoCachePath(channel_id, ext)
        # If a previous cycle wrote with a different ext, drop the old file.
        if existing and existing != target:
            try: os.unlink(existing)
            except Exception: pass
        if not self._logoWriteAtomic(target, fetched['content']):
            return existing
        if fetched.get('etag') or fetched.get('last_modified'):
            validators[channel_id] = {
                'etag'         : fetched.get('etag'),
                'last_modified': fetched.get('last_modified'),
                'url'          : source_url,
            }
        return target

    def _portableLogoURL(self, local):
        """imports.44: convert an absolute LOGO_LOC path returned by
        _cacheLogo into a portable `http://<remote_host>/images/<basename>`
        URL so cross-host iptvsimple clients (e.g. a tablet pulling our
        M3U via http://<madteevee>:50002/pseudotv.m3u) can resolve the
        logo. Without the wrap, the absolute madteevee-only path lands
        in tvg-logo and remote clients render Kodi's default texture for
        every cached logo. server.py's /images/ handler (server.py:1846+)
        maps the basename back to LOGO_LOC + <basename>, so the URL
        roundtrips to the same file. PROPERTIES.getRemoteHost() lazy-inits
        the Remote_Host window property if syncAll runs before the HTTP
        server has published it via setRemoteHost (server.py:2426). On
        the rare host-empty branch we keep the absolute path so at least
        local Kodi keeps rendering correctly.
        """
        host = PROPERTIES.getRemoteHost()
        if not host:
            return local
        return 'http://%s/images/%s' % (host, Globals._quoteString(os.path.basename(local)))

    def _evictOrphanLogos(self, active_channel_ids):
        """Remove cache/logos/ files for channel ids no longer present.

        imports.35: extended the "keep" predicate to also preserve any
        file currently referenced by a channel record's `logo` field in
        channels.json. Without this, operator-uploaded custom logos
        (added via manager.py:switchLogo.__browse at manager.py:900,
        which copies to cache/logos/<chname>.<ext>) were treated as
        orphans because their filename stem (e.g., "TMNT") doesn't match
        any active import id (e.g., "TVLand.us@epgbest_46477"). Net
        effect pre-fix: every syncAll cycle wiped operator-uploaded
        Custom-channel logos. Naming-convention agnostic — works
        whether the file is named by channel-id (imports cache) or by
        channel-name (switchLogo.__browse).
        """
        try:
            cache_dir = self._logoCacheDir()
            if not os.path.isdir(cache_dir):
                return 0
            active = set(active_channel_ids or ())

            # imports.35: cross-reference channels.json's logo fields to
            # protect operator-uploaded customs (and any other valid
            # referenced file) from eviction. Build the set of basenames
            # currently referenced by ANY channel's logo field.
            #
            # imports.46: also recognize the imports.44 URL form
            # `http://<remote_host>/images/<url-encoded-basename>`. Pre-
            # imports.44 ch['logo'] for cached channels held the absolute
            # `/.../cache/logos/<basename>` path and matched the
            # `'cache/logos/' in logo` predicate; imports.44 replaced
            # that with the portable HTTP URL form so cross-host
            # consumers can resolve it, but `'cache/logos/'` no longer
            # appears in the field. Without this branch every cycle
            # where `all_imported_now` ends up empty (e.g. a 304-only
            # sync — every per-import returned Not-Modified, so no
            # `new`/`refreshed` records were synthesized) walks the
            # cache with active=empty AND referenced=empty and wipes
            # every file. Live-observed 2026-05-20 between imports.44
            # ship and imports.45: 78 PNGs in cache/logos/ → 0 after a
            # subsequent sync, which then made imports.45's translatePath
            # fix moot because the files /images/ pointed at no longer
            # existed.
            # Wrapped in try/except so a channels-layer failure falls
            # back to today's behavior (no regression — just no new
            # protection).
            referenced_basenames = set()
            try:
                for ch in (self.channels.getChannels() or []):
                    logo = ch.get('logo') or ''
                    if not isinstance(logo, str) or not logo:
                        continue
                    if 'cache/logos/' in logo:
                        referenced_basenames.add(os.path.basename(logo))
                    elif '/images/' in logo:
                        # imports.44 wrap form. Extract the URL-encoded
                        # basename from the tail and decode (mirrors
                        # server.py's _unquoteString of img_path).
                        try:
                            tail = logo.rsplit('/images/', 1)[1]
                            decoded = urllib.parse.unquote(tail)
                            if decoded and '/' not in decoded:
                                referenced_basenames.add(decoded)
                        except Exception:
                            pass
            except Exception as e:
                self.log('_evictOrphanLogos, channels cross-ref failed: %s'
                         % e, level=xbmc.LOGWARNING)

            # imports.46: defense-in-depth bail-out. If neither path
            # produced a "keep" predicate (active set empty AND no
            # channel references), refuse to walk the cache. The
            # eviction loop would unlink every file — almost certainly
            # not what the operator wants for a single bad sync cycle.
            if not active and not referenced_basenames:
                self.log('_evictOrphanLogos, skipping: empty active set + no channel references (likely a 304-only or failed sync cycle — refusing to mass-wipe)',
                         level=xbmc.LOGWARNING)
                return 0

            removed = 0
            for name in os.listdir(cache_dir):
                if name.startswith('.') or name.endswith('.tmp'):
                    continue
                # filename = <channel_id>.<ext>
                cid = name.rsplit('.', 1)[0]
                if cid and cid in active:
                    continue
                # imports.35: keep if any channel record references this file.
                if name in referenced_basenames:
                    continue
                try:
                    os.unlink(os.path.join(cache_dir, name))
                    removed += 1
                except Exception:
                    pass
            if removed:
                self.log('_evictOrphanLogos, removed %d orphan logo file(s)'
                         % removed, level=xbmc.LOGINFO)
            return removed
        except Exception as e:
            self.log('_evictOrphanLogos failed: %s' % e, level=xbmc.LOGWARNING)
            return 0

    def _epgCacheRead(self, import_id):
        """
        Hydrate `epg_pairs` from the per-import cache file.

        Returns the list of (kind, dict) tuples on success, or None if the
        cache is absent, unreadable, or yields zero pairs (treated as
        corrupt — `parseExternalSource` returns silently on `ParseError`,
        so an empty result indicates the file is unusable). The caller's
        304 branch interprets None as "cache miss → clear validators so
        next cycle force-fetches".
        """
        if not import_id:
            return None
        path = self._epgCachePath(import_id)
        try:
            if not os.path.exists(path):
                return None
        except Exception:
            return None
        try:
            pairs = list(self.parseXMLTV(path))
        except Exception as e:
            self.log('_epgCacheRead, [%s] parse raised: %s' % (import_id, e),
                     level=xbmc.LOGWARNING)
            return None
        if not pairs:
            self.log('_epgCacheRead, [%s] cache yielded 0 pairs (treating as corrupt)'
                     % (import_id,), level=xbmc.LOGWARNING)
            return None
        return pairs

    def cascadeAllocate(self, parsed_imports, existing_channels):
        """
        Greedy stable allocation per plan §4.

        parsed_imports: list of (import_cfg, list_of_parsed_channel_dicts)
                        in priority order (top = highest priority).
        existing_channels: list of channel records from `channels.json`.

        Returns: dict {channel_id: assigned_number}.

        Algorithm:
          1. Hard pins first (PseudoTV-built channels + operator-pinned imports)
             populate `allocations` and can't be displaced.
          2. Sticky allocations from prior cycles (existing imported channels
             with `assigned_number` that aren't operator-pinned). Preserves
             channel numbers across source updates so MovistarPlus adding a
             channel doesn't renumber everything else.
          3. New source channels in priority order with first-free-slot scan.
             Order within a single import: by source `tvg_chno` (numeric)
             then by name — deterministic across runs (plan G2).

        Cap at CASCADE_OVERFLOW_LIMIT (plan H5): channels that exhaust the
        scan are returned with assigned_number=None — caller marks them
        `unallocated=True` and excludes from the M3U write.
        """
        allocations = {}     # number -> channel_id (so we can detect collisions)
        result      = {}     # channel_id -> number (return value)

        # Disabled imported channels go in this high range so they sort to the
        # bottom of the channel list AND don't block their previous slot from
        # being reused by other channels (operator request: disabled channels
        # should free their slot + get a new number).
        DISABLED_BUCKET_START = 9000

        def _claim(cid, n):
            allocations[n] = cid
            result[cid]    = n

        def _next_free_from(desired):
            """Walk up from desired to first free slot; cap at overflow limit."""
            n = max(1, int(desired))
            while n in allocations:
                n += 1
                if n > CASCADE_OVERFLOW_LIMIT:
                    return None
            return n

        # Index existing channels for O(1) lookup
        existing_by_id = {c.get('id'): c for c in (existing_channels or []) if c.get('id')}

        def _is_enabled(cid):
            """Per-channel enabled lookup; defaults to True for unknown ids."""
            ec = existing_by_id.get(cid)
            return True if ec is None else ec.get('enabled', True)

        # ---- 1. Hard pins (skip disabled — they free their slot) ----
        # PseudoTV-built channels (type != 'import'): their number is sacred.
        # Operator-pinned imported channels: 'number' is in operator_overrides.
        for c in (existing_channels or []):
            cid = c.get('id')
            if not cid:
                continue
            if not c.get('enabled', True):
                continue  # disabled channels don't claim slots
            n = c.get('number')
            if not isinstance(n, int) or n < 1 or n >= DISABLED_BUCKET_START:
                continue
            is_import = c.get('type') == 'import'
            is_pin    = (not is_import) or ('number' in (c.get('operator_overrides') or []))
            if is_pin and n not in allocations:
                _claim(cid, n)

        # ---- 2. Sticky allocations for non-pinned ENABLED imported channels ----
        # Each existing enabled import channel keeps its assigned_number if free.
        # Disabled channels are SKIPPED so their old slot becomes available.
        for c in (existing_channels or []):
            cid = c.get('id')
            if cid in result or not cid:
                continue
            if c.get('type') != 'import':
                continue
            if not c.get('enabled', True):
                continue  # disabled → step 4 (bucket)
            sticky = c.get('assigned_number')
            if isinstance(sticky, int) and 1 <= sticky < DISABLED_BUCKET_START and sticky not in allocations:
                _claim(cid, sticky)

        # ---- 3. Allocate new + sticky-collision-cascade for parsed sources ----
        for import_cfg, parsed_channels in (parsed_imports or []):
            if not import_cfg.get('enabled', True):
                continue
            respect_src = bool(import_cfg.get('respect_source_numbers', True))
            start_num   = int(import_cfg.get('start_num', 1) or 1)

            # Sort source channels deterministically: by source tvg_chno (int),
            # then by name. Sentinel `999999` for missing tvg_chno keeps them
            # at end of the source-numbers ordering — so they fall through to
            # auto-allocation logic below if respect_src is True.
            srcs = sorted(
                parsed_channels,
                key=lambda c: (
                    c.get('source_tvg_chno') if isinstance(c.get('source_tvg_chno'), int) else 999999,
                    c.get('name', ''),
                ),
            )
            for idx, src in enumerate(srcs):
                cid = src.get('id')
                if not cid or cid in result:
                    continue  # already allocated (sticky or hard pin)

                # Skip disabled channels — they go to the 9000+ bucket in step 4
                # (operator request: disabled channels free their live slot).
                if not _is_enabled(cid):
                    continue

                # Decide desired number.
                if respect_src and isinstance(src.get('source_tvg_chno'), int):
                    desired = src['source_tvg_chno']
                else:
                    # Auto-allocate from start_num. Multiple new channels all
                    # start at start_num; _next_free_from cascades them through
                    # the next available slots, naturally skipping anything
                    # already allocated (sticky / hard-pin / earlier-this-loop).
                    desired = start_num

                actual = _next_free_from(desired)
                if actual is None:
                    self.log('cascadeAllocate, [%s] exhausted scan past %d (no free slot)'
                             % (cid, CASCADE_OVERFLOW_LIMIT), level=None)
                    result[cid] = None  # caller marks unallocated
                    continue
                _claim(cid, actual)
                if actual != desired:
                    self.log('cascadeAllocate, [%s] cascaded %d -> %d (taken)' % (cid, desired, actual))

        # ---- 4. Disabled imported channels go to the 9000+ bucket ----
        # Every imported channel that's disabled (or whose import is disabled)
        # gets a number in DISABLED_BUCKET_START..CHANNEL_LIMIT. This keeps them
        # out of the live channel range AND makes them sort to the bottom of
        # any number-ordered display.
        disabled_n = DISABLED_BUCKET_START
        for c in (existing_channels or []):
            cid = c.get('id')
            if not cid or cid in result:
                continue
            if c.get('type') != 'import':
                continue
            # Walk to next free in disabled bucket
            while disabled_n in allocations and disabled_n <= CHANNEL_LIMIT:
                disabled_n += 1
            if disabled_n > CHANNEL_LIMIT:
                # Bucket full — leave unallocated
                result[cid] = None
                continue
            allocations[disabled_n] = cid
            result[cid] = disabled_n
            disabled_n += 1

        return result

    def reconcile(self, parsed_channels, existing_imported_channels, import_cfg):
        """
        Match parsed source channels against existing imported channels.

        Match key: namespaced channel `id` (which embeds source tvg-id +
        import_id). Two channels with the same id are the same channel
        across sync cycles, even if the source renamed `display-name` etc.

        Returns (new_channels, refreshed_channels, orphan_channels):
        - new_channels       — parsed channels NOT in existing (filtered
                               against `import_cfg['tombstones']`)
        - refreshed_channels — list of (existing_channel, parsed_channel)
                               pairs; caller merges parsed source-side fields
                               into existing, preserving operator-edited
                               fields named in `operator_overrides`
        - orphan_channels    — existing imported channels for THIS import
                               that are NOT in the parsed source (operator
                               decides keep-or-delete via UI in step 4)

        Source-side fields (always refreshed): source_url, source_tvg_chno,
        catchup-source. Display fields (name, logo, group) are refreshed
        ONLY if not in operator_overrides.
        """
        if not isinstance(parsed_channels, list):
            parsed_channels = list(parsed_channels)

        tombstones = set(import_cfg.get('tombstones') or [])
        existing_by_id = {
            c.get('id'): c for c in (existing_imported_channels or [])
            if c.get('id')
        }
        parsed_ids = {c.get('id') for c in parsed_channels if c.get('id')}

        new_channels       = []
        refreshed_channels = []

        for src in parsed_channels:
            cid = src.get('id')
            if not cid:
                continue
            if cid in tombstones:
                self.log('reconcile, [%s] skipped (tombstoned)' % cid)
                continue
            if cid in existing_by_id:
                refreshed_channels.append((existing_by_id[cid], src))
            else:
                new_channels.append(src)

        # Orphans: existing imported channels that didn't appear in source.
        # madteevee (imports fork): tombstoned ids are NOT re-emitted as orphans.
        # Without this filter, the orphan delete endpoint (server.py:753) is a
        # no-op against the next syncAll cycle: the channel gets removed from
        # channelDATA, but reconcile re-classifies it as orphan because it's
        # in existing_imported_channels (which is loaded fresh from disk each
        # cycle) AND not in parsed_ids (because the source M3U doesn't have
        # it). syncAll then re-emits it via all_imported_now (imports.py:534-
        # 546) and setChannels' merge-on-write keeps it alive forever.
        # Treating tombstones as the persistent delete marker means once the
        # operator hits "🗑 Orphans" and the tombstone is recorded, the channel
        # is permanently gone — unless the operator explicitly removes the id
        # from tombstones (already supported via import-config edit).
        orphan_channels = [
            c for c in (existing_imported_channels or [])
            if c.get('id') and c.get('id') not in parsed_ids
            and c.get('id') not in tombstones
        ]
        tombstoned_orphans = [
            c for c in (existing_imported_channels or [])
            if c.get('id') and c.get('id') not in parsed_ids
            and c.get('id') in tombstones
        ]

        self.log('reconcile, new=%d refreshed=%d orphan=%d (tombstoned=%d, tombstoned-and-still-on-disk=%d)'
                 % (len(new_channels), len(refreshed_channels),
                    len(orphan_channels), len(tombstones), len(tombstoned_orphans)))

        return new_channels, refreshed_channels, orphan_channels


    def applyRefresh(self, existing_channel, parsed_channel):
        """
        Merge parsed source-side fields into an existing channel record,
        preserving operator-edited fields.

        Returns the merged dict (mutates `existing_channel` in place too).

        Source-side ALWAYS refreshed: source_url, source_tvg_chno,
        catchup-source, catchup-days, catchup-correction, provider,
        provider-type, provider-logo, kodiprops, extvlcopt, webprops.

        Display fields refreshed ONLY if NOT in operator_overrides:
        name, logo, group, catchup, radio.

        `operator_overrides` is the authoritative list of fields the
        operator has manually edited; their values stick across syncs.
        """
        overrides = set(existing_channel.get('operator_overrides') or [])

        # Always-refreshed (source-side identity / playback fields)
        for k in ('source_url', 'source_tvg_chno', 'catchup-source',
                  'catchup-days', 'catchup-correction', 'provider',
                  'provider-type', 'provider-logo', 'kodiprops',
                  'extvlcopt', 'webprops', 'tvg-shift', 'media-dir',
                  'media-size', 'x-playlist-type'):
            if k in parsed_channel:
                existing_channel[k] = parsed_channel[k]

        # Conditionally-refreshed (display fields — operator may have edited)
        for k in ('name', 'logo', 'group', 'catchup', 'radio', 'realtime', 'media'):
            if k not in overrides and k in parsed_channel:
                existing_channel[k] = parsed_channel[k]

        # Re-mark not-orphan (the source returned this channel)
        existing_channel['is_orphan'] = False
        return existing_channel
