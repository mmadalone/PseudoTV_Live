"""Regression tests for imports.28 — failure-aware retry / exponential backoff.

imports.25 wired the per-import refresh_interval_min gate inside syncAll,
and `last_sync_at` now updates on EVERY syncOne outcome including
'failed'. Side effect: a persistently-failing import only retries every
refresh_interval_min minutes (10 hours for a typical operator setting).
imports.28 introduces a settings-driven backoff curve so transient
outages recover within minutes, plus an optional abandon-after threshold.

Four operator decisions locked in:
  1. Backoff curve — Imports_Failure_Retry_Curve enum (Disabled / Aggressive /
     Slower / Fixed). Default Aggressive.
  2. Retry cap — Imports_Failure_Abandon_Hours enum (Never / 6 / 12 / 24 /
     48). Default Never.
  3. Dashboard — statusBadge shows `failed Nx · next retry in Xm` and a
     dedicated `abandoned` state. next_retry_at is server-derived.
  4. Error classification — single curve for all hard failures; `warning`
     (empty M3U, channels kept) does NOT trigger backoff.

Source-scan style mirrors test_imports22 / .23 / .25 / .27 for the static
guards; behavioral tests reuse the _MockChannels infrastructure from
test_imports27 + monkey-patch SETTINGS for deterministic gate decisions.

Plan: /home/madalone/.claude/plans/let-s-plan-for-2-drifting-tulip.md
"""
import os
import re
import sys
import time


HERE = os.path.dirname(os.path.abspath(__file__))
ADDON_ROOT = os.path.dirname(HERE)
LIB = os.path.join(ADDON_ROOT, 'resources', 'lib')
REMOTES = os.path.join(ADDON_ROOT, 'remotes')
RESOURCES = os.path.join(ADDON_ROOT, 'resources')


def _read(path):
    with open(path) as f:
        return f.read()


# Add the addon lib to sys.path so we can import the real Imports module.
if LIB not in sys.path:
    sys.path.insert(0, LIB)


# ======================================================================
# Source-scan: module-level helpers
# ======================================================================

def test_imports_module_defines_retry_curves():
    """imports.py must define RETRY_CURVES with the four operator-chosen
    curve names (disabled is handled by the codepath, not the dict).
    Aggressive/slower/fixed are the active curves; values match the
    operator-locked design (60s/5m/15m/1h, 5m/30m/2h, 5m)."""
    src = _read(os.path.join(LIB, 'imports.py'))
    assert 'RETRY_CURVES' in src, "imports.py missing RETRY_CURVES module-level dict"
    assert "'aggressive': [60, 300, 900, 3600]" in src, (
        "RETRY_CURVES['aggressive'] must be [60, 300, 900, 3600] — "
        "operator-locked design (60s/5m/15m/1h, then 2x to cap)."
    )
    assert "'slower':     [300, 1800, 7200]" in src, (
        "RETRY_CURVES['slower'] must be [300, 1800, 7200] — "
        "operator-locked design (5m/30m/2h, then 2x to cap)."
    )
    assert "'fixed':      [300]" in src, (
        "RETRY_CURVES['fixed'] must be [300] — "
        "operator-locked design (always 5m, capped at rim)."
    )


def test_imports_module_defines_fail_count_cap():
    """imports.py must define FAIL_COUNT_CAP — bounds the persisted
    fail_count integer (backoff is already bounded by refresh_interval_min,
    so this is cosmetic, but keeps tests + dashboard predictable)."""
    src = _read(os.path.join(LIB, 'imports.py'))
    assert re.search(r'FAIL_COUNT_CAP\s*=\s*\d+', src) is not None, (
        "imports.py missing FAIL_COUNT_CAP constant — fail_count would "
        "grow unbounded for sources that fail forever (cosmetic concern)."
    )


def test_imports_module_defines_retry_curve_by_code_map():
    """settings.xml uses integer codes (consistent with imports.23 pattern);
    Python maps via RETRY_CURVE_BY_CODE."""
    src = _read(os.path.join(LIB, 'imports.py'))
    for code, name in ((0, 'disabled'), (1, 'aggressive'), (2, 'slower'), (3, 'fixed')):
        needle = "%d: '%s'" % (code, name)
        assert needle in src, (
            "RETRY_CURVE_BY_CODE missing mapping %s — settings.xml code "
            "for %r will fall back to default." % (needle, name)
        )


def test_imports_module_defines_compute_retry_delay():
    """Module-level pure function computeRetryDelay(curve, fail_count, rim_sec)."""
    src = _read(os.path.join(LIB, 'imports.py'))
    assert re.search(r'def computeRetryDelay\(curve,\s*fail_count,\s*refresh_interval_sec\):', src), (
        "imports.py missing computeRetryDelay(curve, fail_count, "
        "refresh_interval_sec) — module-level pure function is the contract "
        "with both syncAll's gate and server.py's next_retry_at derivation."
    )


# ======================================================================
# Unit tests: computeRetryDelay math
# ======================================================================

def _seed_random(monkeypatch):
    """Force jitter to 0 so curve assertions are deterministic. We seed
    the module-level `random` import used by computeRetryDelay so calls to
    randint return 0 (jitter neutral)."""
    import imports as imports_mod
    monkeypatch.setattr(imports_mod.random, 'randint', lambda a, b: 0)


def test_computeRetryDelay_disabled_returns_rim(monkeypatch):
    """Disabled curve returns refresh_interval_sec verbatim regardless of
    fail_count — preserves pre-imports.28 behavior exactly."""
    _seed_random(monkeypatch)
    from imports import computeRetryDelay
    for fc in (1, 2, 5, 20, 100):
        assert computeRetryDelay('disabled', fc, 600) == 600, (
            "Disabled curve must return rim verbatim for fail_count=%d" % fc
        )


def test_computeRetryDelay_aggressive_indices(monkeypatch):
    """Aggressive curve: fail_count=1 → 60s, 2 → 5m, 3 → 15m, 4 → 1h.
    Past the end, doubles from the last entry (capped at rim)."""
    _seed_random(monkeypatch)
    from imports import computeRetryDelay
    rim_sec = 24 * 3600  # 24h ceiling so curve never caps in this test
    assert computeRetryDelay('aggressive', 1, rim_sec) == 60
    assert computeRetryDelay('aggressive', 2, rim_sec) == 300
    assert computeRetryDelay('aggressive', 3, rim_sec) == 900
    assert computeRetryDelay('aggressive', 4, rim_sec) == 3600
    assert computeRetryDelay('aggressive', 5, rim_sec) == 3600 * 2   # 2h
    assert computeRetryDelay('aggressive', 6, rim_sec) == 3600 * 4   # 4h
    assert computeRetryDelay('aggressive', 7, rim_sec) == 3600 * 8   # 8h


def test_computeRetryDelay_slower_indices(monkeypatch):
    """Slower curve: fail_count=1 → 5m, 2 → 30m, 3 → 2h. Past the end,
    doubles from the last entry (capped at rim)."""
    _seed_random(monkeypatch)
    from imports import computeRetryDelay
    rim_sec = 48 * 3600
    assert computeRetryDelay('slower', 1, rim_sec) == 300
    assert computeRetryDelay('slower', 2, rim_sec) == 1800
    assert computeRetryDelay('slower', 3, rim_sec) == 7200
    assert computeRetryDelay('slower', 4, rim_sec) == 7200 * 2   # 4h
    assert computeRetryDelay('slower', 5, rim_sec) == 7200 * 4   # 8h


def test_computeRetryDelay_fixed_returns_5m_capped(monkeypatch):
    """Fixed curve: always 5m capped at refresh_interval_sec regardless
    of fail_count."""
    _seed_random(monkeypatch)
    from imports import computeRetryDelay
    for fc in (1, 2, 5, 20):
        assert computeRetryDelay('fixed', fc, 24 * 3600) == 300
    # When rim < 5m, cap kicks in
    assert computeRetryDelay('fixed', 1, 120) == 120


def test_computeRetryDelay_cap_at_refresh_interval(monkeypatch):
    """All curves are capped at refresh_interval_sec — the operator-set
    ceiling is never exceeded even past the end of the curve."""
    _seed_random(monkeypatch)
    from imports import computeRetryDelay
    rim_sec = 600  # 10 min
    # Aggressive past 5m (fc=2 already at 5m); fc=3+ should cap
    assert computeRetryDelay('aggressive', 3, rim_sec) == rim_sec
    assert computeRetryDelay('aggressive', 10, rim_sec) == rim_sec
    # Slower at fc=2 is 30m, cap kicks in
    assert computeRetryDelay('slower', 2, rim_sec) == rim_sec


def test_computeRetryDelay_jitter_within_band(monkeypatch):
    """Jitter is ±12.5% of the computed delay. Test that the spread
    stays within the band when jitter is uncapped."""
    import random as real_random
    real_random.seed(42)  # deterministic spread across many calls
    from imports import computeRetryDelay
    rim_sec = 24 * 3600
    samples = [computeRetryDelay('aggressive', 1, rim_sec) for _ in range(50)]
    # Base delay 60; ±12.5% = ±7. Allow off-by-rounding at boundary.
    assert all(53 <= s <= 67 for s in samples), (
        "Jitter outside the ±12.5%% band: %s" % [s for s in samples if not (53 <= s <= 67)]
    )


def test_computeRetryDelay_fail_count_zero_returns_rim(monkeypatch):
    """fail_count=0 (clean import) returns rim regardless of curve —
    the curve only applies when there's an active failure run."""
    _seed_random(monkeypatch)
    from imports import computeRetryDelay
    for curve in ('aggressive', 'slower', 'fixed'):
        assert computeRetryDelay(curve, 0, 600) == 600


# ======================================================================
# Source-scan: settings.xml + strings.po
# ======================================================================

def test_settings_xml_has_failure_retry_curve():
    """settings.xml must contain Imports_Failure_Retry_Curve spinner with 4
    options (0=Disabled / 1=Aggressive / 2=Slower / 3=Fixed). Default 1
    (Aggressive). Option 0 reuses #30021 (Disabled) per the imports.23
    no-duplicate-strings rule."""
    src = _read(os.path.join(RESOURCES, 'settings.xml'))
    pat = re.compile(
        r'<setting id="Imports_Failure_Retry_Curve"[^>]*>(.*?)</setting>',
        re.DOTALL,
    )
    m = pat.search(src)
    assert m is not None, "settings.xml missing Imports_Failure_Retry_Curve setting"
    block = m.group(0)

    assert 'type="integer"' in block, "Imports_Failure_Retry_Curve must be type=integer"
    assert '<default>1</default>' in block, "default must be 1 (Aggressive)"
    for label, value in (('30021', '0'), ('33940', '1'), ('33941', '2'), ('33942', '3')):
        needle = '<option label="%s">%s</option>' % (label, value)
        assert needle in block, (
            "Imports_Failure_Retry_Curve missing option %s. The 0 option "
            "must reuse #30021 ('Disabled')." % needle
        )


def test_settings_xml_has_failure_abandon_hours():
    """settings.xml must contain Imports_Failure_Abandon_Hours spinner with
    5 options (0=Never / 6 / 12 / 24 / 48). Default 0 (Never). 0 uses a
    new string #33945 (not #30021 — 'Never (keep auto-retrying)' is more
    operator-friendly than the generic 'Disabled' in this context)."""
    src = _read(os.path.join(RESOURCES, 'settings.xml'))
    pat = re.compile(
        r'<setting id="Imports_Failure_Abandon_Hours"[^>]*>(.*?)</setting>',
        re.DOTALL,
    )
    m = pat.search(src)
    assert m is not None, "settings.xml missing Imports_Failure_Abandon_Hours setting"
    block = m.group(0)

    assert 'type="integer"' in block
    assert '<default>0</default>' in block, "default must be 0 (Never)"
    for label, value in (('33945', '0'), ('33946', '6'), ('33947', '12'),
                          ('33948', '24'), ('33949', '48')):
        needle = '<option label="%s">%s</option>' % (label, value)
        assert needle in block, (
            "Imports_Failure_Abandon_Hours missing option %s." % needle
        )


def test_strings_po_has_imports28_entries():
    """strings.po must define labels + help text for both new settings."""
    src = _read(os.path.join(RESOURCES, 'language', 'resource.language.en_gb', 'strings.po'))
    for sid in ('33938', '33939', '33940', '33941', '33942',
                '33943', '33944', '33945', '33946', '33947', '33948', '33949'):
        assert 'msgctxt "#%s"' % sid in src, (
            "strings.po missing #%s — imports.28 string surface incomplete." % sid
        )


# ======================================================================
# Source-scan: server.py next_retry_at derivation
# ======================================================================

def test_server_imports_json_derives_next_retry_at():
    """server.py:do_GET /imports.json must enrich each import with a
    derived next_retry_at field when last_status='failed' and fail_count>0.
    The derivation must use the same computeRetryDelay helper as syncAll
    so the dashboard view stays in lock-step with the gate's decision."""
    src = _read(os.path.join(LIB, 'server.py'))
    # The /imports.json endpoint must import and use computeRetryDelay.
    assert 'from imports import computeRetryDelay' in src, (
        "server.py /imports.json missing `from imports import "
        "computeRetryDelay` — derivation would diverge from syncAll's gate."
    )
    assert 'next_retry_at' in src, (
        "server.py missing next_retry_at — manager.html statusBadge can't "
        "compute the retry countdown without it."
    )


def test_server_imports_json_derives_abandoned_status():
    """server.py must override imp['last_status']='abandoned' when
    failed_since older than Imports_Failure_Abandon_Hours. Storage stays
    'failed'; this is a derived view-only state per the agent-validated
    design."""
    src = _read(os.path.join(LIB, 'server.py'))
    assert "imp['last_status'] = 'abandoned'" in src, (
        "server.py missing the derived abandoned override — dashboard "
        "wouldn't surface abandoned imports."
    )


# ======================================================================
# Source-scan: manager.html statusBadge extensions
# ======================================================================

def test_manager_html_status_badge_handles_abandoned():
    """manager.html statusBadge must render the abandoned state distinctly
    from a normal error — operator needs to see that auto-retry stopped."""
    src = _read(os.path.join(REMOTES, 'manager.html'))
    assert "imp.last_status === 'abandoned'" in src, (
        "manager.html statusBadge missing abandoned branch — abandoned "
        "imports would render as a normal 'error' badge."
    )
    assert 'hit Refresh to retry' in src, (
        "manager.html abandoned badge missing operator-facing recovery "
        "hint ('hit Refresh to retry')."
    )


def test_manager_html_status_badge_shows_retry_tail():
    """statusBadge must append `failed Nx · next retry <fmtUntil>` when
    fail_count>0 and next_retry_at is present. Uses the existing fmtUntil
    helper (imports.26)."""
    src = _read(os.path.join(REMOTES, 'manager.html'))
    assert 'imp.fail_count' in src, "statusBadge must read imp.fail_count"
    assert 'imp.next_retry_at' in src, "statusBadge must read imp.next_retry_at"
    assert 'failed ${fc}' in src or 'failed " + fc' in src, (
        "statusBadge must render the failed count in the retry tail."
    )


# ======================================================================
# Behavioral: gate + result-merge with mocked SETTINGS
# ======================================================================
#
# Strategy mirrors test_imports27:
# - 1-3 mock imports with last_sync_at recent so the gate WOULD skip.
# - Configure each with NO m3u_url so syncOne returns status='ok' fast.
# - Monkey-patch SETTINGS reads to deterministic values.
# - Drive syncAll(force_scope=None) and assert per_import_results contents.


class _MockChannels:
    def __init__(self, imports_list, channels_list):
        self._imports = list(imports_list)
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
    def __init__(self):
        self.added = []
        self.M3UDATA = []
        self.writable = False
    def addStation(self, sitem): self.added.append(sitem)


class _MockXMLTV:
    def __init__(self):
        self.XMLTVDATA = {'channels': [], 'programmes': [], 'recordings': []}
    @property
    def channels(self):   return self.XMLTVDATA['channels']
    @property
    def programmes(self): return self.XMLTVDATA['programmes']


def _patch_settings(monkeypatch, *, curve_code=1, abandon_hours=0):
    """Monkey-patch SETTINGS.getSettingInt to return our test values for
    the two imports.28 keys. Other keys fall through to the real (or stub)
    accessor."""
    import imports as imports_mod
    real_getInt = imports_mod.SETTINGS.getSettingInt
    def fake_getInt(key, default=0):
        if key == 'Imports_Failure_Retry_Curve':  return curve_code
        if key == 'Imports_Failure_Abandon_Hours': return abandon_hours
        try:    return real_getInt(key, default)
        except TypeError: return real_getInt(key)
    monkeypatch.setattr(imports_mod.SETTINGS, 'getSettingInt', fake_getInt)
    # Ensure jitter is neutral for deterministic gate behavior
    monkeypatch.setattr(imports_mod.random, 'randint', lambda a, b: 0)


def _make_failing_import(iid, *, fail_count, last_sync_offset_sec,
                         failed_since_offset_sec=None, rim_min=60):
    """Build an import_cfg in a `failed` state for gate-behavior tests."""
    now = int(time.time())
    cfg = {
        'id'                    : iid,
        'name'                  : iid.upper(),
        'enabled'               : True,
        'm3u_url'               : '',
        'm3u_path'              : '',
        'epg_url'               : '',
        'epg_path'              : '',
        'start_num'             : 100,
        'refresh_interval_min'  : rim_min,
        'last_sync_at'          : now + last_sync_offset_sec,
        'last_status'           : 'failed',
        'last_error'            : 'fake fetch failure',
        'fail_count'            : fail_count,
        'tombstones'            : [],
        'respect_source_numbers': True,
        'rediscover_deleted'    : False,
    }
    if failed_since_offset_sec is not None:
        cfg['failed_since'] = now + failed_since_offset_sec
    else:
        cfg['failed_since'] = now + last_sync_offset_sec
    return cfg


def _run_syncAll(imports_list):
    from imports import Imports
    channels = _MockChannels(imports_list, [])
    m3u = _MockM3U()
    xmltv = _MockXMLTV()
    imp = Imports(channels=channels, m3u=m3u, xmltv=xmltv)
    return imp.syncAll()


def test_gate_skips_during_backoff(monkeypatch):
    """Aggressive curve, fail_count=2 → expected delay 5m. Last sync 60s
    ago → gate must skip (60 < 300)."""
    _patch_settings(monkeypatch, curve_code=1, abandon_hours=0)
    imports_list = [_make_failing_import('A', fail_count=2, last_sync_offset_sec=-60)]
    results = _run_syncAll(imports_list)
    assert results['A']['status'] == 'skipped', (
        "fail_count=2 with last_sync 60s ago and aggressive curve (5m expected) "
        "should skip; got status=%r" % results['A']['status']
    )


def test_gate_releases_after_backoff(monkeypatch):
    """Aggressive curve, fail_count=2 → expected delay 5m. Last sync 400s
    ago (past 5m) → gate must release; syncOne runs."""
    _patch_settings(monkeypatch, curve_code=1, abandon_hours=0)
    imports_list = [_make_failing_import('A', fail_count=2, last_sync_offset_sec=-400)]
    results = _run_syncAll(imports_list)
    assert results['A']['status'] != 'skipped', (
        "fail_count=2 with last_sync 400s ago should have released the "
        "gate (5m=300s); got status=%r" % results['A']['status']
    )


def test_disabled_curve_uses_rim_only(monkeypatch):
    """curve=disabled preserves pre-imports.28 behavior: gate fires at
    rim*60 regardless of fail_count. With rim=60m and last_sync 30m ago,
    gate skips even with fail_count=10."""
    _patch_settings(monkeypatch, curve_code=0, abandon_hours=0)
    imports_list = [_make_failing_import('A', fail_count=10, last_sync_offset_sec=-30*60, rim_min=60)]
    results = _run_syncAll(imports_list)
    assert results['A']['status'] == 'skipped', (
        "Disabled curve must gate at rim*60 (60m) regardless of fail_count; "
        "with last_sync 30min ago, expected skip. Got %r." % results['A']['status']
    )


def test_ok_resets_fail_count_and_failed_since(monkeypatch):
    """Successful sync (status='ok') zeros fail_count and clears
    failed_since on the updated_cfg."""
    _patch_settings(monkeypatch, curve_code=1, abandon_hours=0)
    # last_sync 1h ago + aggressive curve at fc=2 (5m) → gate releases.
    # No m3u_url means syncOne falls through to status='ok' fast.
    imports_list = [_make_failing_import('A', fail_count=2, last_sync_offset_sec=-3600)]
    results = _run_syncAll(imports_list)
    cfg = results['A']['updated_cfg']
    assert results['A']['status'] in ('ok', 'unchanged'), (
        "syncOne should have run; got status=%r" % results['A']['status']
    )
    assert cfg.get('fail_count') == 0, (
        "On status=ok, fail_count must reset to 0; got %r" % cfg.get('fail_count')
    )
    assert cfg.get('failed_since') is None, (
        "On status=ok, failed_since must clear to None; got %r" % cfg.get('failed_since')
    )


def test_abandon_after_threshold_emits_abandoned(monkeypatch):
    """failed_since older than abandon_hours → status='abandoned'.
    syncOne is NOT called (gate skips with the derived status)."""
    _patch_settings(monkeypatch, curve_code=1, abandon_hours=6)
    # Failed 7 hours ago; abandon threshold 6h → abandoned.
    imports_list = [_make_failing_import(
        'A', fail_count=20, last_sync_offset_sec=-7*3600,
        failed_since_offset_sec=-7*3600,
    )]
    results = _run_syncAll(imports_list)
    assert results['A']['status'] == 'abandoned', (
        "Import failing for 7h with abandon_hours=6 should be abandoned; "
        "got status=%r" % results['A']['status']
    )


def test_abandon_with_force_scope_bypasses_to_syncone(monkeypatch):
    """force_scope='A' on an abandoned import bypasses the abandon check
    and runs syncOne. (Operator clicked Refresh on a stuck import.)"""
    from imports import Imports
    _patch_settings(monkeypatch, curve_code=1, abandon_hours=6)
    imports_list = [_make_failing_import(
        'A', fail_count=20, last_sync_offset_sec=-7*3600,
        failed_since_offset_sec=-7*3600,
    )]
    channels = _MockChannels(imports_list, [])
    imp = Imports(channels=channels, m3u=_MockM3U(), xmltv=_MockXMLTV())
    results = imp.syncAll(force_scope='A')
    assert results['A']['status'] != 'abandoned', (
        "force_scope='A' should bypass abandon; got status=%r" % results['A']['status']
    )
    assert results['A']['status'] != 'skipped', (
        "force_scope='A' should also bypass the gate; got status=%r"
        % results['A']['status']
    )


def test_abandon_disabled_when_hours_zero(monkeypatch):
    """abandon_hours=0 (Never) preserves auto-retry forever — no import
    is ever abandoned regardless of how long it's been failing."""
    _patch_settings(monkeypatch, curve_code=1, abandon_hours=0)
    # Failed 30 days ago — would be way past any abandon threshold.
    imports_list = [_make_failing_import(
        'A', fail_count=20, last_sync_offset_sec=-30*86400,
        failed_since_offset_sec=-30*86400,
    )]
    results = _run_syncAll(imports_list)
    assert results['A']['status'] != 'abandoned', (
        "abandon_hours=0 (Never) must never abandon; got status=%r"
        % results['A']['status']
    )


def test_backward_compat_no_fail_count_field(monkeypatch):
    """Imports created before .28 won't have fail_count or failed_since
    keys. The gate must handle the missing fields without KeyError —
    .get('fail_count') or 0 + .get('failed_since') chained with the right
    falsy handling."""
    _patch_settings(monkeypatch, curve_code=1, abandon_hours=24)
    # Build a pre-.28-shaped import: no fail_count, no failed_since,
    # last_sync_at recent (would gate normally), last_status='ok' (so
    # the failure-aware path doesn't trigger).
    now = int(time.time())
    pre28_cfg = {
        'id'                    : 'A',
        'name'                  : 'A',
        'enabled'               : True,
        'm3u_url'               : '',
        'm3u_path'              : '',
        'epg_url'               : '',
        'epg_path'              : '',
        'start_num'             : 100,
        'refresh_interval_min'  : 60,
        'last_sync_at'          : now - 60,
        'last_status'           : 'ok',
        'tombstones'            : [],
        'respect_source_numbers': True,
        'rediscover_deleted'    : False,
    }
    # Must not raise
    results = _run_syncAll([pre28_cfg])
    # Either gates (rim not elapsed) or runs syncOne — both are acceptable;
    # we're testing that it didn't crash.
    assert 'A' in results
