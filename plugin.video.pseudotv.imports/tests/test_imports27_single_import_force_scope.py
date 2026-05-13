"""Regression tests for the imports.27 single-import force scope refinement.

imports.25 introduced the per-import refresh_interval_min gate inside
syncAll, threading a boolean `force=True/False` through to bypass the
gate for ALL imports on a manual kick. That's blunt when the operator
clicked Refresh on a single import — the kick value already encoded the
scope, but the boolean threw the info away. imports.27 plumbs the actual
kick value through as `force_scope` (string or None) and has the gate
check `force_scope == 'all' OR force_scope == this_import.id` per-import.

Effect:
  - force_scope = 'all'        → all imports bypass gate (= imports.25 force=True)
  - force_scope = '<id>'       → only that import bypasses; others gate
  - force_scope = None         → no bypass; all imports gate (= imports.25 force=False)
  - force_scope = '<unknown>'  → no match; all gate; debug log line surfaces typo

Source-scan style mirrors test_imports22 / .23 / .24 / .25 / .26.
Behavioral tests reuse the _MockChannels infrastructure from
test_imports_module.py via direct construction (the mock is simple
enough to copy locally for clarity).

Plan: /home/madalone/.claude/plans/dig-into-c-do-typed-kettle.md (imports.27 #3)
"""
import os
import re
import sys
import time


HERE = os.path.dirname(os.path.abspath(__file__))
ADDON_ROOT = os.path.dirname(HERE)
LIB = os.path.join(ADDON_ROOT, 'resources', 'lib')


def _read(path):
    with open(path) as f:
        return f.read()


# ======================================================================
# Source-scan assertions
# ======================================================================

def test_syncAll_signature_renamed_to_force_scope():
    """`syncAll(self, force_scope=None)` exists; the OLD `force=False`
    signature is gone (no deprecated alias kept — fork-only addon,
    clean rename)."""
    src = _read(os.path.join(LIB, 'imports.py'))
    assert re.search(
        r'def syncAll\(self,\s*force_scope\s*=\s*None\s*\):',
        src,
    ) is not None, (
        "imports.py:syncAll signature missing `force_scope=None` — "
        "imports.27 rename regressed."
    )
    assert re.search(
        r'def syncAll\(self,\s*force\s*=\s*False\s*\):',
        src,
    ) is None, (
        "imports.py:syncAll still has the old `force=False` signature — "
        "imports.27 should have fully replaced it, not kept an alias."
    )


def test_syncAll_gate_uses_force_scope_per_import():
    """The gate body must compute `force_active` from BOTH
    `force_scope == 'all'` AND `force_scope == import_cfg.get('id')`.
    Catches a regression where someone reverts to a simple
    `not force_scope` check, which would treat 'all' and '<id>' as just
    'truthy' and bypass for everyone again."""
    src = _read(os.path.join(LIB, 'imports.py'))

    start = src.find('def syncAll(self,')
    assert start != -1
    end = src.find('\n    def ', start + 1)
    body = src[start:end]

    # Both arms of the OR must appear within the gate-decision span
    assert "force_scope == 'all'" in body, (
        "imports.py:syncAll gate missing `force_scope == 'all'` — global "
        "kicks would no longer bypass the gate."
    )
    assert "force_scope == import_cfg.get('id')" in body, (
        "imports.py:syncAll gate missing `force_scope == import_cfg.get('id')` "
        "— single-import scope would not match; ALL imports would gate "
        "(or none, depending on the broken expression)."
    )
    assert 'force_active' in body, (
        "imports.py:syncAll gate must compute a `force_active` boolean — "
        "regression to the old simple `not force` check."
    )


def test_tasks_py_threads_kick_scope():
    """tasks.py:_startImportsThread must use `kick_scope` (string) not
    `was_kicked` (boolean), and pass `force_scope=kick_scope` through to
    chkImports. chkImports must pass `force_scope=force_scope` to syncAll."""
    src = _read(os.path.join(LIB, 'tasks.py'))

    start = src.find('def _startImportsThread')
    end = src.find('\n    def ', start + 1)
    body = src[start:end]

    for needle, why in (
        ('kick_scope = None',           "initialize kick_scope (None = no force) before outer loop"),
        ('kick_scope = kick',           "set kick_scope to the kick VALUE (not a boolean) when consumed"),
        ('force_scope=kick_scope',      "pass kick_scope through to chkImports as force_scope"),
    ):
        assert needle in body, (
            "tasks.py:_startImportsThread missing %r — %s." % (needle, why)
        )

    # Old boolean names should be gone (allow them in comments as historical
    # context, but not in code).
    code_lines = [
        ln for ln in body.splitlines()
        if not ln.lstrip().startswith('#') and 'was_kicked' in ln
    ]
    assert not code_lines, (
        "tasks.py:_startImportsThread still has `was_kicked` in code "
        "(not in comments) — imports.27 rename incomplete. Offending: %s"
        % code_lines[:2]
    )

    # chkImports signature uses force_scope
    assert re.search(
        r'def chkImports\(self,\s*silent\s*=\s*None,\s*force_scope\s*=\s*None\s*\):',
        src,
    ) is not None, (
        "tasks.py:chkImports signature missing `force_scope=None` — "
        "imports.27 rename regressed."
    )

    # syncAll call inside chkImports passes force_scope
    assert 'syncAll(force_scope=force_scope)' in src, (
        "tasks.py:chkImports must pass `force_scope=force_scope` to "
        "Imports(...).syncAll() — imports.27 threading regressed."
    )


def test_syncAll_logs_unknown_force_scope():
    """When force_scope doesn't match 'all' AND doesn't match any known
    import id, syncAll must log a debug line surfacing the typo. Doesn't
    fire for None or 'all' (the common cases)."""
    src = _read(os.path.join(LIB, 'imports.py'))
    assert "doesn't match any known import id" in src, (
        "imports.py:syncAll missing the unknown-force_scope debug log — "
        "operator typos in kick values would be invisible. Catches the "
        "'I clicked Refresh but nothing happened' diagnosis."
    )


# ======================================================================
# Behavioral assertions — using local mocks (copied/adapted from
# test_imports_module.py's _MockChannels pattern)
# ======================================================================
#
# Strategy:
# - Set up 3 mock imports A/B/C with last_sync_at recent + refresh_interval_min
#   set, so the per-import gate would skip them unless force-bypassed.
# - Configure each import with NO m3u_url / m3u_path / epg_url — syncOne
#   skips its fetch blocks entirely and falls through to status='ok'.
# - Track which imports actually went through syncOne by examining
#   per_import_results[iid]['status']: 'ok' means syncOne ran; 'skipped'
#   means the gate fired.
# - Verify per scope variant: 'all' / specific id / None / unknown id.


# Add the addon lib to sys.path so we can import the real Imports module.
# (conftest.py at tests/conftest.py already does this for collection;
# the explicit prepend below makes module-level imports robust.)
if LIB not in sys.path:
    sys.path.insert(0, LIB)


class _MockChannels:
    """Minimal Channels surface for syncAll behavioral tests. Mirrors
    test_imports_module.py:_MockChannels with the imports.22 deleted_ids
    signature support (accept extra kwarg, ignore it)."""
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
        self.writable = False  # imports.25 force-flush guarded on .writable
    def addStation(self, sitem): self.added.append(sitem)


class _MockXMLTV:
    def __init__(self):
        self.XMLTVDATA = {'channels': [], 'programmes': [], 'recordings': []}
    @property
    def channels(self):   return self.XMLTVDATA['channels']
    @property
    def programmes(self): return self.XMLTVDATA['programmes']


def _make_import(iid, refresh_min=60, last_sync_offset=-5*60):
    """Build an import_cfg that the gate WILL skip (last_sync recent +
    refresh_interval_min set), with no fetch URLs so syncOne is fast +
    deterministic ('ok' status)."""
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


def _run_syncAll(force_scope, *, mark='dont_use_default'):
    """Helper: build 3 mock imports, run syncAll, return per_import_results.
    `mark` is a sentinel to differentiate "passed None explicitly" from
    "didn't pass anything" at the call site, but for our use it's just
    documentation."""
    from imports import Imports

    imports_list = [_make_import('A'), _make_import('B'), _make_import('C')]
    channels = _MockChannels(imports_list, [])
    m3u = _MockM3U()
    xmltv = _MockXMLTV()
    imp = Imports(channels=channels, m3u=m3u, xmltv=xmltv)
    if force_scope is mark:
        return imp.syncAll()  # default arg path
    return imp.syncAll(force_scope=force_scope)


def test_force_scope_targets_one_import_others_gate():
    """force_scope='A' bypasses the gate for A only; B and C are gated."""
    results = _run_syncAll('A')
    assert results['A']['status'] != 'skipped', (
        "Import 'A' was NOT synced even though force_scope='A' should have "
        "bypassed its gate. Per-import scope refinement regressed (got "
        "status=%r)." % results['A']['status']
    )
    assert results['B']['status'] == 'skipped', (
        "Import 'B' was synced even though force_scope='A' should have "
        "kept B's gate active. Got status=%r." % results['B']['status']
    )
    assert results['C']['status'] == 'skipped', (
        "Import 'C' was synced even though force_scope='A' should have "
        "kept C's gate active. Got status=%r." % results['C']['status']
    )


def test_force_scope_all_bypasses_every_import():
    """force_scope='all' bypasses the gate for every import (preserves
    imports.25 `force=True` behavior verbatim — 5 of the 7 kick setters
    in production write 'all')."""
    results = _run_syncAll('all')
    for iid in ('A', 'B', 'C'):
        assert results[iid]['status'] != 'skipped', (
            "Import %r was gated even though force_scope='all' should have "
            "bypassed every import. Got status=%r — imports.25 behavior for "
            "'all' kicks regressed." % (iid, results[iid]['status'])
        )


def test_force_scope_none_gates_every_import():
    """force_scope=None (default) gates every import (preserves the
    natural-daemon-cycle behavior — no kick, all gates fire)."""
    results = _run_syncAll(None)
    for iid in ('A', 'B', 'C'):
        assert results[iid]['status'] == 'skipped', (
            "Import %r was synced even though force_scope=None means no "
            "kick. The gate should have fired (last_sync=5min ago, "
            "interval=60m). Got status=%r — natural-cycle gating regressed."
            % (iid, results[iid]['status'])
        )


def test_force_scope_unknown_id_falls_through_to_gating():
    """force_scope='bogus_id' (a typo) doesn't match any import; all
    gates fire normally (no bypass). This is the safe default for
    operator typos — the operator sees 'Refresh did nothing' which is
    the same UX as a deferred kick. A debug log line surfaces the typo
    for diagnostics."""
    results = _run_syncAll('bogus_id_typo')
    for iid in ('A', 'B', 'C'):
        assert results[iid]['status'] == 'skipped', (
            "Import %r was synced even though force_scope='bogus_id_typo' "
            "doesn't match any known import id. Should fall through to "
            "normal gating. Got status=%r." % (iid, results[iid]['status'])
        )
