"""Regression tests for imports.43 disk-presence check inside the per-import
refresh-interval gate.

When the configured scope (Imports_Disk_Presence_Scope setting) detects
missing/stub on-disk data for an import, the gate force-syncs that import
regardless of `refresh_interval_min`. Integrates with the existing
`force_active` mechanism in imports.py:syncAll so the downstream abandon
check + normal elapsed-time gate honor the disk-missing trigger
automatically — zero new branching in the gate body.

Two test surfaces:
  - Unit tests for the `_isDiskMissing(import_id, scope)` helper directly:
    monkeypatch `os.path.exists` + `os.path.getsize` to control the disk
    state; assert correct True/False return for each scope value.
  - Behavioral tests for the gate's response to `_isDiskMissing`'s return:
    monkeypatch `_isDiskMissing` to True/False directly; run syncAll and
    verify whether force_active was set (per_import_results['status']
    flips from 'skipped' to 'ok' when force-bypassed).

Source-scan style mirrors test_imports22/.23/.27/.28 for the static guards.

Plan: /home/madalone/.claude/plans/declarative-stirring-rainbow.md
"""
import os
import re
import sys
import time

import pytest


HERE = os.path.dirname(os.path.abspath(__file__))
ADDON_ROOT = os.path.dirname(HERE)
LIB = os.path.join(ADDON_ROOT, 'resources', 'lib')

if LIB not in sys.path:
    sys.path.insert(0, LIB)


def _read(path):
    with open(path) as f:
        return f.read()


# ======================================================================
# Source-scan assertions — the gate code reads the setting + invokes
# _isDiskMissing
# ======================================================================

def test_imports_py_defines_disk_presence_scope_by_code_map():
    """imports.py module-scope must define `DISK_PRESENCE_SCOPE_BY_CODE`
    with three entries (0/1/2 → main_m3u/per_import_epg/both). Mirrors
    the imports.28 `RETRY_CURVE_BY_CODE` pattern."""
    src = _read(os.path.join(LIB, 'imports.py'))
    assert 'DISK_PRESENCE_SCOPE_BY_CODE' in src, (
        "imports.py missing module-scope DISK_PRESENCE_SCOPE_BY_CODE map.")
    # All three string labels must appear in the map
    for label in ('main_m3u', 'per_import_epg', 'both'):
        assert "'%s'" % label in src or '"%s"' % label in src, (
            "imports.py DISK_PRESENCE_SCOPE_BY_CODE missing label %r" % label)


def test_imports_py_defines_isDiskMissing_helper():
    """imports.py must define a module-level `_isDiskMissing(import_id, scope)`
    function. Module-scope (not Imports method) so the gate can call it
    without instance state."""
    src = _read(os.path.join(LIB, 'imports.py'))
    assert re.search(r'^def _isDiskMissing\(import_id, scope\):',
                     src, re.MULTILINE) is not None, (
        "imports.py missing module-level `_isDiskMissing(import_id, scope)` "
        "helper — imports.43 gate disk-presence check would have no impl.")


def test_imports_py_isDiskMissing_handles_all_three_scopes():
    """_isDiskMissing must branch on each of the three scope values
    (main_m3u / per_import_epg / both). Tests the helper's body covers
    all configured paths."""
    src = _read(os.path.join(LIB, 'imports.py'))
    # Find the helper body
    m = re.search(r'def _isDiskMissing\(import_id, scope\):(.*?)(?=\n(?:def |class ))',
                  src, re.DOTALL)
    assert m is not None, "_isDiskMissing body not parseable"
    body = m.group(1)
    for scope in ('main_m3u', 'per_import_epg', 'both'):
        assert "'%s'" % scope in body or '"%s"' % scope in body, (
            "_isDiskMissing doesn't handle scope=%r — operator setting "
            "value would be effectively ignored." % scope)


def test_imports_py_isDiskMissing_uses_m3u_min_bytes_threshold():
    """_isDiskMissing must check getsize against a stub-size threshold
    (`_DISK_PRESENCE_M3U_MIN_BYTES`), not just `not exists`. Catches
    the header-only stub case (107-byte `#EXTM3U\\n` rendered after a
    refuse-empty would otherwise have fired) that pure existence-check
    would miss."""
    src = _read(os.path.join(LIB, 'imports.py'))
    assert '_DISK_PRESENCE_M3U_MIN_BYTES' in src, (
        "imports.py missing _DISK_PRESENCE_M3U_MIN_BYTES constant — "
        "stub-sized M3U won't be detected as 'missing'.")
    assert 'getsize' in src, (
        "imports.py disk-presence path missing `os.path.getsize` call — "
        "header-only stub wouldn't trigger force-sync.")


def test_imports_py_syncAll_gate_invokes_isDiskMissing():
    """The syncAll gate body must call `_isDiskMissing` to set
    `force_active=True` on disk-missing. Catches regression where the
    check is silently dropped from the gate."""
    src = _read(os.path.join(LIB, 'imports.py'))
    start = src.find('def syncAll(self,')
    assert start != -1
    end = src.find('\n    def ', start + 1)
    body = src[start:end]
    assert '_isDiskMissing' in body, (
        "imports.py:syncAll gate doesn't call _isDiskMissing — the "
        "imports.43 disk-presence check is bypassed; operator's missing-"
        "M3U recovery won't fire.")
    assert 'force_active = True' in body, (
        "imports.py:syncAll gate must set `force_active = True` somewhere "
        "(the disk-missing path does this).")


def test_imports_py_syncAll_gate_reads_disk_presence_scope_setting():
    """The gate must read `Imports_Disk_Presence_Scope` per cycle so
    operator config changes take effect on the next sync, not next
    Kodi restart."""
    src = _read(os.path.join(LIB, 'imports.py'))
    start = src.find('def syncAll(self,')
    end = src.find('\n    def ', start + 1)
    body = src[start:end]
    assert "'Imports_Disk_Presence_Scope'" in body or '"Imports_Disk_Presence_Scope"' in body, (
        "syncAll doesn't read `Imports_Disk_Presence_Scope` setting — "
        "the granularity knob is wired but not consumed.")
    assert 'getSettingInt' in body, (
        "syncAll should read the setting via SETTINGS.getSettingInt (the "
        "type=\"integer\" + code-mapped pattern from imports.28).")


# ======================================================================
# Unit tests — _isDiskMissing helper, monkeypatched filesystem
# ======================================================================

def _patch_fs(monkeypatch, *, exists=True, size=10000):
    """Monkeypatch os.path.exists + getsize so _isDiskMissing sees a
    controllable filesystem. Both monkeypatched at the imports module's
    `os` reference so the helper picks them up."""
    import imports as _imp
    monkeypatch.setattr(_imp.os.path, 'exists', lambda p: exists)
    monkeypatch.setattr(_imp.os.path, 'getsize', lambda p: size)


def test_isDiskMissing_main_m3u_returns_true_when_file_absent(monkeypatch):
    from imports import _isDiskMissing
    _patch_fs(monkeypatch, exists=False, size=0)
    assert _isDiskMissing('movistarplus', 'main_m3u') is True


def test_isDiskMissing_main_m3u_returns_true_for_stub_sized_file(monkeypatch):
    """A 107-byte header-only M3U (the corruption shape past refuse-empty
    incidents have produced) is below the 200-byte threshold → treated
    as missing."""
    from imports import _isDiskMissing
    _patch_fs(monkeypatch, exists=True, size=107)
    assert _isDiskMissing('movistarplus', 'main_m3u') is True


def test_isDiskMissing_main_m3u_returns_false_for_healthy_file(monkeypatch):
    """Above the 200-byte threshold → file is considered healthy."""
    from imports import _isDiskMissing
    _patch_fs(monkeypatch, exists=True, size=50_000)
    assert _isDiskMissing('movistarplus', 'main_m3u') is False


def test_isDiskMissing_per_import_epg_returns_true_when_absent(monkeypatch):
    from imports import _isDiskMissing
    _patch_fs(monkeypatch, exists=False, size=0)
    assert _isDiskMissing('movistarplus', 'per_import_epg') is True


def test_isDiskMissing_per_import_epg_returns_true_for_stub_size(monkeypatch):
    """<100 bytes → stub-sized XML root, treated as missing."""
    from imports import _isDiskMissing
    _patch_fs(monkeypatch, exists=True, size=50)
    assert _isDiskMissing('movistarplus', 'per_import_epg') is True


def test_isDiskMissing_per_import_epg_returns_false_for_healthy_file(monkeypatch):
    from imports import _isDiskMissing
    _patch_fs(monkeypatch, exists=True, size=500_000)
    assert _isDiskMissing('movistarplus', 'per_import_epg') is False


def test_isDiskMissing_both_triggers_on_either_condition(monkeypatch):
    """`both` scope returns True if EITHER M3U OR EPG is missing/stub.
    Defense in depth."""
    from imports import _isDiskMissing
    # Both healthy → False
    _patch_fs(monkeypatch, exists=True, size=50_000)
    assert _isDiskMissing('movistarplus', 'both') is False
    # First check (M3U) fires → True (helper short-circuits)
    _patch_fs(monkeypatch, exists=False, size=0)
    assert _isDiskMissing('movistarplus', 'both') is True


def test_isDiskMissing_filesystem_error_treated_as_missing(monkeypatch):
    """OSError from os.path.exists / getsize (rare: stat race, permission
    glitch) → treated as 'missing'. Fail-safe: force-sync rather than
    silently skip when we can't tell."""
    import imports as _imp
    def _exists_raises(p): raise OSError('boom')
    monkeypatch.setattr(_imp.os.path, 'exists', _exists_raises)
    from imports import _isDiskMissing
    assert _isDiskMissing('movistarplus', 'main_m3u') is True


# ======================================================================
# Behavioral assertions — gate honors _isDiskMissing return
# ======================================================================
#
# Reuses the _MockChannels / _MockM3U / _MockXMLTV pattern from
# test_imports27 (copied for clarity — tests are read in isolation).


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
    def __init__(self):
        self.added = []
        self.M3UDATA = []
        self.writable = False
    def addStation(self, sitem): self.added.append(sitem)


class _MockXMLTV:
    def __init__(self):
        self.XMLTVDATA = {'channels': [], 'programmes': [], 'recordings': []}


def _make_import(iid, refresh_min=60, last_sync_offset=-5 * 60):
    """Import_cfg the gate WOULD skip (last_sync recent, refresh_interval_min
    set), no fetch URLs so syncOne is fast/deterministic."""
    return {
        'id'                    : iid,
        'name'                  : iid.upper(),
        'enabled'               : True,
        'm3u_url'               : '',
        'm3u_path'              : '',
        'epg_url'               : '',
        'epg_path'              : '',
        'start_num'             : 100,
        'refresh_interval_min'  : refresh_min,
        'last_sync_at'          : int(time.time()) + last_sync_offset,
        'tombstones'            : [],
        'respect_source_numbers': True,
        'rediscover_deleted'    : False,
    }


def _run_syncAll(monkeypatch, *, disk_missing):
    """Build 3 mock imports, set _isDiskMissing's return, run syncAll, return per_import_results."""
    import imports as _imp
    monkeypatch.setattr(_imp, '_isDiskMissing', lambda iid, scope: disk_missing)
    from imports import Imports
    imports_list = [_make_import('A'), _make_import('B'), _make_import('C')]
    channels = _MockChannels(imports_list, [])
    m3u = _MockM3U()
    xmltv = _MockXMLTV()
    imp = Imports(channels=channels, m3u=m3u, xmltv=xmltv)
    return imp.syncAll()  # force_scope=None — gate should fire UNLESS disk-missing forces


def test_gate_disk_missing_true_forces_sync_for_all_imports(monkeypatch):
    """When _isDiskMissing returns True, the gate's force_active flips
    to True for that import, and syncOne runs (status='ok') instead of
    being 'skipped' by elapsed-time."""
    results = _run_syncAll(monkeypatch, disk_missing=True)
    for iid in ('A', 'B', 'C'):
        assert results[iid]['status'] != 'skipped', (
            "Import %r was 'skipped' even though disk-presence check should "
            "have forced a sync; result: %r" % (iid, results[iid]))


def test_gate_disk_missing_false_preserves_normal_skip_behavior(monkeypatch):
    """When _isDiskMissing returns False (healthy disk), the gate uses
    its normal elapsed-time logic. All 3 mock imports have recent
    last_sync_at — they should be 'skipped'."""
    results = _run_syncAll(monkeypatch, disk_missing=False)
    for iid in ('A', 'B', 'C'):
        assert results[iid]['status'] == 'skipped', (
            "Import %r was NOT skipped even though disk-presence is False "
            "AND elapsed < refresh_interval; result: %r" % (iid, results[iid]))


def test_gate_disk_presence_check_skipped_when_force_active_already_set(monkeypatch):
    """When force_scope='all' is passed, force_active is already True
    before the disk-presence check runs — the check shouldn't fire
    (and the log shouldn't show 'disk-presence: forcing sync')."""
    import imports as _imp
    called = []
    def _mock_isDiskMissing(iid, scope):
        called.append((iid, scope))
        return True
    monkeypatch.setattr(_imp, '_isDiskMissing', _mock_isDiskMissing)
    from imports import Imports
    imports_list = [_make_import('A')]
    channels = _MockChannels(imports_list, [])
    m3u = _MockM3U()
    xmltv = _MockXMLTV()
    imp = Imports(channels=channels, m3u=m3u, xmltv=xmltv)
    imp.syncAll(force_scope='all')
    # _isDiskMissing should NOT have been called for any import (force_active
    # is already True from force_scope='all'; the disk-presence check is
    # guarded by `if not force_active`).
    assert called == [], (
        "_isDiskMissing called %d times when force_scope='all' was already "
        "set — wasted I/O; the gate must short-circuit on existing force_active. "
        "Calls: %r" % (len(called), called))
