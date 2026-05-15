"""Regression tests for imports.43 boot strategy + eager-render-from-channels.json.

`Imports_Boot_Strategy` setting drives what `_startImportsThread._loop`
does at boot+5s (after the existing initial-delay wait, before the
chkImports cycle loop):

  - 0 (disk_presence, default) — no boot action; gate's disk-presence
    check handles missing-M3U recovery on the first cycle
  - 1 (eager_render) — call `Tasks._eagerImportsRender`, which constructs
    writable Channels/M3U/XMLTVS singletons and asks `Imports.renderFromPersistedState`
    to render the LAST-KNOWN persisted import state to disk (NO HTTP)
  - 2 (force_sync) — initialize `kick_scope='all'` so the first chkImports
    call bypasses every import's refresh_interval_min gate (one cycle)

Three test surfaces:
  - Source-scan: verify the dispatch wires up the setting + each branch
  - Unit tests for `Imports.renderFromPersistedState`: monkeypatch
    `_isDiskMissing` + `Channels.getChannels` + `M3U._save`; assert
    counts + skip-on-healthy-M3U
  - Unit tests for `Tasks._eagerImportsRender`: monkeypatch construction
    + verify it delegates to renderFromPersistedState

Plan: /home/madalone/.claude/plans/declarative-stirring-rainbow.md
"""
import os
import re
import sys

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
# Source-scan — module-scope constants + boot dispatch wiring
# ======================================================================

def test_imports_py_defines_boot_strategy_by_code_map():
    """imports.py module-scope must define BOOT_STRATEGY_BY_CODE with
    three entries (0/1/2). Mirrors RETRY_CURVE_BY_CODE pattern."""
    src = _read(os.path.join(LIB, 'imports.py'))
    assert 'BOOT_STRATEGY_BY_CODE' in src, (
        "imports.py missing module-scope BOOT_STRATEGY_BY_CODE map.")
    for label in ('disk_presence', 'eager_render', 'force_sync'):
        assert "'%s'" % label in src or '"%s"' % label in src, (
            "BOOT_STRATEGY_BY_CODE missing label %r" % label)


def test_imports_py_defines_renderFromPersistedState_method():
    """Imports class must expose `renderFromPersistedState(self)` for
    the eager_render path. Method (not module function) so it owns the
    instance's channels/m3u/xmltv state."""
    src = _read(os.path.join(LIB, 'imports.py'))
    assert 'def renderFromPersistedState(self)' in src, (
        "imports.py missing `Imports.renderFromPersistedState` method.")


def test_tasks_py_defines_eagerImportsRender_method():
    """Tasks class must expose `_eagerImportsRender(self)` — the boot-
    dispatch's eager_render branch invokes it."""
    src = _read(os.path.join(LIB, 'tasks.py'))
    assert 'def _eagerImportsRender(self)' in src, (
        "tasks.py missing `Tasks._eagerImportsRender` method.")


def test_tasks_py_startImportsThread_reads_boot_strategy_setting():
    """_startImportsThread must read `Imports_Boot_Strategy` before the
    main while-loop so the boot dispatch happens once at start, not on
    every cycle."""
    src = _read(os.path.join(LIB, 'tasks.py'))
    start = src.find('def _startImportsThread')
    end = src.find('\n    def ', start + 1)
    body = src[start:end]
    assert 'Imports_Boot_Strategy' in body, (
        "_startImportsThread doesn't read `Imports_Boot_Strategy` setting "
        "— the operator knob isn't consumed at the dispatch point.")
    assert 'BOOT_STRATEGY_BY_CODE' in body, (
        "_startImportsThread must decode the int setting via "
        "BOOT_STRATEGY_BY_CODE (the imports.28 pattern).")


def test_tasks_py_startImportsThread_dispatches_each_strategy():
    """All three strategy branches must appear in the dispatch — operator
    config to any of the three must take effect."""
    src = _read(os.path.join(LIB, 'tasks.py'))
    start = src.find('def _startImportsThread')
    end = src.find('\n    def ', start + 1)
    body = src[start:end]
    for strategy in ('disk_presence', 'eager_render', 'force_sync'):
        # Either as a literal string compare or in a comment near the dispatch
        assert strategy in body, (
            "_startImportsThread missing dispatch for strategy %r — "
            "operator config to this value would silently be a no-op."
            % strategy)


def test_tasks_py_force_sync_sets_initial_kick_scope_all():
    """The force_sync branch must set kick_scope (or a stand-in) to 'all'
    so the first chkImports call bypasses every import's gate."""
    src = _read(os.path.join(LIB, 'tasks.py'))
    start = src.find('def _startImportsThread')
    end = src.find('\n    def ', start + 1)
    body = src[start:end]
    # Look for the force_sync branch and an 'all' assignment near it
    m = re.search(r"force_sync.*?=\s*['\"]all['\"]", body, re.DOTALL)
    assert m is not None, (
        "_startImportsThread's force_sync branch must set the initial "
        "kick_scope to 'all' — otherwise the strategy is a no-op.")


def test_tasks_py_eagerImportsRender_constructs_writable_singletons():
    """_eagerImportsRender must construct writable Channels/M3U/XMLTV
    singletons — mirrors the chkImports construction pattern. Critical:
    `writable=True` is required so `_save` actually writes to disk."""
    src = _read(os.path.join(LIB, 'tasks.py'))
    start = src.find('def _eagerImportsRender')
    end = src.find('\n    def ', start + 1)
    body = src[start:end]
    assert 'Channels(writable=True)' in body, (
        "_eagerImportsRender must construct Channels with writable=True.")
    assert 'M3U(writable=True)' in body, (
        "_eagerImportsRender must construct M3U with writable=True (else "
        "_save returns False without writing).")
    assert 'renderFromPersistedState' in body, (
        "_eagerImportsRender must delegate to Imports.renderFromPersistedState.")


# ======================================================================
# Unit tests — Imports.renderFromPersistedState behavior
# ======================================================================
#
# Mocks mirror test_imports27_single_import_force_scope.py for consistency.


class _MockChannels:
    """Channels surface for renderFromPersistedState tests."""
    def __init__(self, channels_list):
        self._channels = list(channels_list)
    def getChannels(self): return list(self._channels)
    def getImports(self):  return []
    def setChannels(self, channels=None, modified_ids=None, deleted_ids=None):
        if channels is not None: self._channels = list(channels)
        return True
    def setImports(self, data): return True


class _MockM3U:
    """M3U surface — tracks _save invocations and the M3UDATA shape at save time."""
    def __init__(self, writable=True):
        self.writable = writable
        self.M3UDATA = {'stations': [], 'recordings': [], 'data': '#EXTM3U ...'}
        self.save_count = 0
        self.save_returns = True
        self.stations_at_save = None
    def _save(self):
        self.save_count += 1
        # Snapshot stations at save time so tests can assert what was rendered
        self.stations_at_save = list(self.M3UDATA.get('stations') or [])
        return self.save_returns


def _import_channel(iid, name, number):
    """Minimal channel record matching the import shape (verified against
    live channels.json in the imports.43 planning phase)."""
    return {
        'id'      : iid,
        'name'    : name,
        'number'  : number,
        'enabled' : True,
        'type'    : 'import',
        'url'     : 'plugin://x/?id=%s' % iid,
        'group'   : ['TestGroup'],
        'logo'    : '',
        'catchup' : 'default',
        'radio'   : False,
    }


def test_renderFromPersistedState_returns_zero_when_m3u_healthy(monkeypatch):
    """When the on-disk M3U is already present (not missing/stub), the
    method must NO-OP — don't clobber a healthy file with potentially
    stale persisted state."""
    import imports as _imp
    monkeypatch.setattr(_imp, '_isDiskMissing', lambda import_id=None, scope=None, **kw: False)
    channels = _MockChannels([
        _import_channel('A', 'A-name', 100),
        _import_channel('B', 'B-name', 101),
    ])
    m3u = _MockM3U()
    imp = _imp.Imports(channels=channels, m3u=m3u)
    assert imp.renderFromPersistedState() == 0
    assert m3u.save_count == 0, (
        "renderFromPersistedState saved %d times when M3U was already "
        "healthy — must be a no-op." % m3u.save_count)


def test_renderFromPersistedState_renders_import_channels_when_missing(monkeypatch):
    """When the disk M3U is missing/stub AND channels.json has enabled
    import channels, render+save them. Returns the count."""
    import imports as _imp
    monkeypatch.setattr(_imp, '_isDiskMissing', lambda import_id=None, scope=None, **kw: True)
    channels = _MockChannels([
        _import_channel('A', 'A-name', 100),
        _import_channel('B', 'B-name', 101),
    ])
    m3u = _MockM3U()
    imp = _imp.Imports(channels=channels, m3u=m3u)
    rendered = imp.renderFromPersistedState()
    assert rendered == 2, "expected 2 imports rendered, got %d" % rendered
    assert m3u.save_count == 1
    assert len(m3u.stations_at_save) == 2


def test_renderFromPersistedState_filters_out_disabled_imports(monkeypatch):
    """Disabled imports (`enabled=False`) must NOT be rendered — operator
    intent."""
    import imports as _imp
    monkeypatch.setattr(_imp, '_isDiskMissing', lambda import_id=None, scope=None, **kw: True)
    enabled = _import_channel('A', 'A-name', 100)
    disabled = _import_channel('B', 'B-name', 101)
    disabled['enabled'] = False
    channels = _MockChannels([enabled, disabled])
    m3u = _MockM3U()
    imp = _imp.Imports(channels=channels, m3u=m3u)
    assert imp.renderFromPersistedState() == 1
    saved_ids = [s.get('id') for s in m3u.stations_at_save]
    assert saved_ids == ['A'], (
        "Disabled import 'B' leaked into the eager render; got: %r" % saved_ids)


def test_renderFromPersistedState_excludes_custom_channels(monkeypatch):
    """`type='Custom'` records (PseudoTV-built channels) must NOT be
    rendered by the eager-import path — those go through Builder, not
    here. This method is for imports ONLY."""
    import imports as _imp
    monkeypatch.setattr(_imp, '_isDiskMissing', lambda import_id=None, scope=None, **kw: True)
    imp_ch = _import_channel('A', 'A-name', 100)
    custom_ch = _import_channel('CUSTOM', 'C-name', 200)
    custom_ch['type'] = 'Custom'
    channels = _MockChannels([imp_ch, custom_ch])
    m3u = _MockM3U()
    imp = _imp.Imports(channels=channels, m3u=m3u)
    assert imp.renderFromPersistedState() == 1
    saved_ids = [s.get('id') for s in m3u.stations_at_save]
    assert saved_ids == ['A'], (
        "Custom channel leaked into eager-import render; got: %r" % saved_ids)


def test_renderFromPersistedState_returns_zero_when_no_imports_configured(monkeypatch):
    """No imports in channels.json → nothing to render → return 0,
    don't call _save (avoid an empty-data write that refuse-empty would
    refuse anyway)."""
    import imports as _imp
    monkeypatch.setattr(_imp, '_isDiskMissing', lambda import_id=None, scope=None, **kw: True)
    channels = _MockChannels([])
    m3u = _MockM3U()
    imp = _imp.Imports(channels=channels, m3u=m3u)
    assert imp.renderFromPersistedState() == 0
    assert m3u.save_count == 0


def test_renderFromPersistedState_silent_no_op_when_channels_or_m3u_missing(monkeypatch):
    """If `self.channels` or `self.m3u` is None (test/standalone
    construction), return 0 silently — don't crash."""
    import imports as _imp
    monkeypatch.setattr(_imp, '_isDiskMissing', lambda import_id=None, scope=None, **kw: True)
    imp_no_channels = _imp.Imports(channels=None, m3u=_MockM3U())
    assert imp_no_channels.renderFromPersistedState() == 0
    imp_no_m3u = _imp.Imports(channels=_MockChannels([]), m3u=None)
    assert imp_no_m3u.renderFromPersistedState() == 0


def test_renderFromPersistedState_returns_zero_when_save_returns_false(monkeypatch):
    """When `_save` returns False (e.g., the imports.42 size-circuit-
    breaker refused), report 0 — operator's log line should say
    'skipped', not 'rendered N'."""
    import imports as _imp
    monkeypatch.setattr(_imp, '_isDiskMissing', lambda import_id=None, scope=None, **kw: True)
    channels = _MockChannels([_import_channel('A', 'A-name', 100)])
    m3u = _MockM3U()
    m3u.save_returns = False
    imp = _imp.Imports(channels=channels, m3u=m3u)
    assert imp.renderFromPersistedState() == 0


def test_renderFromPersistedState_handles_getChannels_exception(monkeypatch):
    """`channels.getChannels()` exception (corrupt channels.json,
    permission issue, etc) → return 0, don't crash the daemon. Logged
    at WARNING in production but here we only verify behavior."""
    import imports as _imp
    monkeypatch.setattr(_imp, '_isDiskMissing', lambda import_id=None, scope=None, **kw: True)
    class _BadChannels:
        def getChannels(self): raise RuntimeError('corrupt channels.json')
    m3u = _MockM3U()
    imp = _imp.Imports(channels=_BadChannels(), m3u=m3u)
    assert imp.renderFromPersistedState() == 0
    assert m3u.save_count == 0


def test_renderFromPersistedState_idempotent_across_calls(monkeypatch):
    """Multiple calls are safe — each render-and-save is atomic via the
    imports.12 writer_lock + atomic-rename. Verifies the method doesn't
    have hidden state that breaks on re-entry."""
    import imports as _imp
    monkeypatch.setattr(_imp, '_isDiskMissing', lambda import_id=None, scope=None, **kw: True)
    channels = _MockChannels([_import_channel('A', 'A-name', 100)])
    m3u = _MockM3U()
    imp = _imp.Imports(channels=channels, m3u=m3u)
    assert imp.renderFromPersistedState() == 1
    assert imp.renderFromPersistedState() == 1
    assert m3u.save_count == 2
