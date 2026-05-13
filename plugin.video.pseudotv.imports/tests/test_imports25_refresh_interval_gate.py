"""Regression tests for the per-import refresh-interval gate + "never synced"
display fix (imports.25).

Two bugs:
- Bug A: `manager.html:1465` read `imp.last_synced` but storage writes
  `last_sync_at` (imports.py:449). Field-name drift caused every import to
  render "never synced" forever.
- Bug B: `refresh_interval_min` setting was operator-facing but unwired —
  zero `.py` references; imports were polled on every chkImports cycle
  regardless of the configured interval.

Bug B prerequisite: `last_sync_at` must update on EVERY syncOne outcome
(success / 304 / fetch-failed / parse-failed / empty-parsed), not just
success. Otherwise the gate keyed on `now - last_sync_at < interval`
breaks for any source that returns 304 (since the timestamp never
advances).

Plus: imports.25 must NOT weaken the imports.20 operator_overrides
guarantee — per-channel operator edits to imported LiveTV channels
(rename / logo / number / enabled / favorite / group / tombstoned) must
survive every autorefresh cycle.

Source-scan style mirrors test_imports22 / .23 / .24 — driving the actual
daemon is impractical (Kodi Monitor stub, threading, settings DB), so we
guard the invariants statically.

Plan: /home/madalone/.claude/plans/dig-into-c-do-typed-kettle.md (imports.25)
"""
import os
import re


HERE = os.path.dirname(os.path.abspath(__file__))
ADDON_ROOT = os.path.dirname(HERE)
LIB = os.path.join(ADDON_ROOT, 'resources', 'lib')
REMOTES = os.path.join(ADDON_ROOT, 'remotes')


def _read(path):
    with open(path) as f:
        return f.read()


# ----------------------------------------------------------------------
# Bug A: manager.html "never synced" display
# ----------------------------------------------------------------------

def test_manager_html_reads_last_sync_at_not_last_synced():
    """manager.html must read `imp.last_sync_at` (storage field), NOT
    `imp.last_synced` (the pre-imports.25 typo that caused every import
    to render 'never synced' forever)."""
    src = _read(os.path.join(REMOTES, 'manager.html'))

    # The fix: imp.last_sync_at MUST appear.
    assert 'imp.last_sync_at' in src, (
        "manager.html missing `imp.last_sync_at` read — imports.25 Bug A "
        "fix regressed. statusBadge() should compute "
        "`const synced = imp.last_sync_at;` (not `imp.last_synced`)."
    )

    # The typo: imp.last_synced MUST NOT appear anywhere (the only callsite
    # was the bug we're fixing; no legitimate reads of this key exist).
    # Allow it inside comments that explain the historical bug.
    code_lines = [
        ln for ln in src.splitlines()
        if 'imp.last_synced' in ln
        and '//' not in ln.split('imp.last_synced', 1)[0]
    ]
    assert not code_lines, (
        "manager.html has `imp.last_synced` outside a comment — imports.25 "
        "Bug A regression. Offending line(s): %s" % code_lines[:3]
    )


# ----------------------------------------------------------------------
# Bug B: per-import refresh-interval gate in syncAll
# ----------------------------------------------------------------------

def test_syncAll_accepts_force_parameter():
    """`Imports.syncAll` must accept a `force_scope=None` keyword arg.
    Used by chkImports to bypass the per-import gate when the daemon
    wakes due to a manual kick. imports.27 renamed the parameter from
    boolean `force=False/True` to scope-aware string
    `force_scope=None/'all'/<id>` so a kick on one import doesn't
    force-refresh unrelated imports."""
    src = _read(os.path.join(LIB, 'imports.py'))
    m = re.search(r'def syncAll\(self,\s*force_scope\s*=\s*None\s*\):', src)
    assert m is not None, (
        "imports.py:syncAll signature missing `force_scope=None` parameter — "
        "imports.27 single-import scope refinement regressed."
    )


def test_syncAll_has_per_import_interval_gate():
    """`Imports.syncAll` must implement the per-import refresh_interval_min
    gate. Required code patterns inside the per-import loop:
      - reads `refresh_interval_min` from the import_cfg
      - reads `last_sync_at` from the import_cfg
      - computes `force_active` from force_scope (imports.27 per-import)
      - checks `not force_active` before applying the gate
      - has a defensive `0 <= elapsed` clock-skew guard
      - skips via `continue` (does NOT call syncOne for gated imports)
    """
    src = _read(os.path.join(LIB, 'imports.py'))

    # Slice the syncAll body
    start = src.find('def syncAll(self,')
    assert start != -1, "syncAll not found"
    end = src.find('\n    def ', start + 1)
    assert end != -1, "couldn't locate end of syncAll"
    body = src[start:end]

    for needle, why in (
        ('refresh_interval_min',  "must read refresh_interval_min from import_cfg"),
        ('last_sync_at',          "must read last_sync_at from import_cfg"),
        ('force_active',          "imports.27: gate must use per-import `force_active` (replaces boolean `not force`)"),
        ('not force_active',      "gate must be guarded by `not force_active` so single-import kicks scope correctly"),
        ('0 <= elapsed',          "defensive clock-skew guard (negative elapsed shouldn't gate)"),
        ("'skipped'",             "gated imports must record status='skipped'"),
        ('continue',              "gate must skip syncOne via `continue` (not just status flag)"),
        ('enabled',               "gate must respect `enabled` (disabled imports fall through to syncOne)"),
    ):
        assert needle in body, (
            "imports.py:syncAll missing required pattern %r — %s." % (needle, why)
        )


def test_last_sync_at_writes_on_every_attempt_outcome():
    """imports.25 requires `last_sync_at = int(time.time())` to fire on
    every syncOne attempt outcome, not just success. The pre-imports.25
    304 path explicitly skipped this; the gate breaks if it stays skipped.
    Verified by counting occurrences of the write — must be ≥ 5 (304,
    fetch-failed, parse-failed, empty-parsed, success-path)."""
    src = _read(os.path.join(LIB, 'imports.py'))
    # Match any line that assigns last_sync_at = int(time.time())
    matches = re.findall(
        r"\['last_sync_at'\]\s*=\s*int\(time\.time\(\)\)",
        src,
    )
    assert len(matches) >= 5, (
        "imports.py has only %d last_sync_at writes — imports.25 requires "
        "writes on at least 5 outcome paths (304 unchanged, fetch-failed, "
        "parse-failed, empty-parsed, success). Found %d matches. If you "
        "consolidated multiple paths into a shared helper, update this "
        "assertion to match the new structure." % (len(matches), len(matches))
    )

    # Also assert the OLD "Don't update last_sync_at on 304" comment is gone
    # (or rephrased to NOT say "Don't update"). Catches a regression that
    # restores the old skip behavior.
    assert "Don't update last_sync_at on 304" not in src, (
        "imports.py still has the pre-imports.25 'Don't update last_sync_at "
        "on 304' comment. The 304 path must update the timestamp now — "
        "remove the stale comment to avoid confusion."
    )


# ----------------------------------------------------------------------
# Force-flag threading: daemon → chkImports → syncAll
# ----------------------------------------------------------------------

def test_tasks_py_threads_kick_scope_through_to_syncAll():
    """tasks.py:_startImportsThread must (imports.27 rename of the
    imports.25 `was_kicked` boolean):
      - Initialize `kick_scope = None` before the outer loop.
      - Set `kick_scope = kick` (the actual kick value string — 'all' or
        a specific import_id) when the kick-consume path fires.
      - Pass `force_scope=kick_scope` to chkImports.
      - Reset `kick_scope = None` after passing it.

    chkImports signature must accept `force_scope=None`. The Imports()
    call inside must pass `force_scope=force_scope` to syncAll.
    """
    src = _read(os.path.join(LIB, 'tasks.py'))

    # Daemon loop checks
    start = src.find('def _startImportsThread')
    assert start != -1, "_startImportsThread not found"
    end = src.find('\n    def ', start + 1)
    body = src[start:end]

    for needle, why in (
        ('kick_scope = None',           "initialize kick_scope before outer loop"),
        ('kick_scope = kick',           "set kick_scope to the actual kick value when consumed"),
        ('force_scope=kick_scope',      "pass kick_scope through to chkImports as force_scope"),
    ):
        assert needle in body, (
            "tasks.py:_startImportsThread missing %r — %s." % (needle, why)
        )

    # chkImports signature
    assert re.search(
        r'def chkImports\(self,\s*silent\s*=\s*None,\s*force_scope\s*=\s*None\s*\):',
        src,
    ) is not None, (
        "tasks.py:chkImports signature missing `force_scope=None` parameter — "
        "imports.27 single-import scope refinement regressed."
    )

    # syncAll call inside chkImports passes force_scope
    assert 'syncAll(force_scope=force_scope)' in src, (
        "tasks.py:chkImports must pass `force_scope=force_scope` to "
        "Imports(...).syncAll() — imports.27 scope-threading regressed."
    )


# ----------------------------------------------------------------------
# Imports.20 inheritance: operator_overrides preserved through syncAll
# ----------------------------------------------------------------------

def test_syncAll_merge_loop_respects_operator_overrides():
    """The merge loop INSIDE syncAll (which iterates res['refreshed'] and
    syncs source-side fields into the existing channel record) must respect
    operator_overrides for the fields it touches. This is the imports.20
    guarantee that imports.25 inherits.

    Required protections in the syncAll body:
      - `'number' not in operator_overrides` guards `existing['number'] = n`
        (operator-pinned channel numbers preserved across syncs).
      - `'url' not in operator_overrides` guards the URL refresh from source.
      - `'catchup' not in overrides` guards the catchup-mode refresh.

    Equally critical: the merge loop must NOT unconditionally assign to
    `existing['name']`, `existing['logo']`, `existing['enabled']`, or
    `existing['favorite']` — those operator-editable fields are preserved
    by inheriting from the existing channel record (never overwritten by
    source). If a future refactor adds an unguarded refresh for any of
    them, operator edits would silently disappear.
    """
    src = _read(os.path.join(LIB, 'imports.py'))

    # Slice syncAll body
    start = src.find('def syncAll(self,')
    assert start != -1, "syncAll not found"
    end = src.find('\n    def ', start + 1)
    assert end != -1, "couldn't locate end of syncAll"
    body = src[start:end]

    # Operator-pinned NUMBER protection
    assert re.search(
        r"['\"]number['\"][\s\S]{0,80}operator_overrides",
        body,
    ) is not None, (
        "syncAll merge loop missing the `'number' not in operator_overrides` "
        "guard — operator-pinned channel numbers would be silently moved "
        "by cascade re-allocation. imports.20 protection regressed."
    )

    # Operator-overridden URL protection
    assert re.search(
        r"['\"]url['\"][\s\S]{0,80}operator_overrides",
        body,
    ) is not None, (
        "syncAll merge loop missing the `'url' not in operator_overrides` "
        "guard — operator URL overrides would get overwritten by source."
    )

    # Operator-overridden CATCHUP protection
    assert re.search(
        r"['\"]catchup['\"][\s\S]{0,80}overrides",
        body,
    ) is not None, (
        "syncAll merge loop missing the `'catchup' not in overrides` "
        "guard — operator catchup overrides would get overwritten."
    )

    # Negative guards: must NOT have unguarded writes to name/logo/enabled/
    # favorite on the EXISTING channel record inside the merge loop. The
    # merge currently doesn't touch these at all (preserved by inheritance);
    # any new assignment would be a regression that silently wipes operator
    # edits. We allow assignments where the line ALSO references
    # operator_overrides nearby (means a future contributor added guarded
    # refresh — that's fine).
    for field in ('name', 'logo', 'enabled', 'favorite'):
        # Find any line in the merge loop that writes `existing[field] = ...`
        pattern = r"existing\[\s*['\"]%s['\"]\s*\]\s*=" % field
        matches = [
            ln for ln in body.splitlines()
            if re.search(pattern, ln)
        ]
        # If any match exists, check that operator_overrides protection is
        # nearby (within ~3 lines). If not, fail.
        if matches:
            # Find the position of the first match and check 3 lines before/after
            for ln in matches:
                line_idx = body.find(ln)
                window = body[max(0, line_idx - 200): line_idx + 200]
                assert 'operator_overrides' in window, (
                    "syncAll merge loop writes to existing[%r] without an "
                    "operator_overrides guard nearby — operator's %s edit "
                    "would be silently overwritten on every sync. Offending "
                    "line: %r" % (field, field, ln.strip())
                )


def test_cascade_allocate_respects_operator_pinned_numbers():
    """The cascadeAllocate function must skip re-allocation for channels
    whose 'number' is in operator_overrides — the operator pinned that
    channel to a specific slot via /imports/channel/edit.json or the
    Renumber endpoint, and re-numbering would silently move it.
    """
    src = _read(os.path.join(LIB, 'imports.py'))

    # Locate cascadeAllocate
    m = re.search(r'def cascadeAllocate\s*\(', src)
    assert m is not None, "imports.py:cascadeAllocate function not found"
    start = m.start()
    end = src.find('\n    def ', start + 1)
    if end == -1: end = len(src)
    body = src[start:end]

    # Must check 'number' in operator_overrides somewhere in the body.
    # The standard pattern from imports.20 is something like:
    #     'number' in (c.get('operator_overrides') or [])
    assert re.search(
        r"['\"]number['\"][\s\S]{0,80}operator_overrides",
        body,
    ) is not None, (
        "cascadeAllocate does not gate on `'number' in operator_overrides` "
        "— operator-pinned channel numbers would get reshuffled on sync. "
        "imports.20 pinning guarantee regressed."
    )


# ----------------------------------------------------------------------
# Note on behavioral tests
# ----------------------------------------------------------------------
#
# This file is source-scan-only by design — mirrors test_imports22 /
# test_imports23 / test_imports24. The existing _MockChannels / _MockM3U /
# _MockXMLTV pattern in test_imports_module.py supports more behavioral
# coverage if you want to extend (mocking syncOne to count calls under
# `force_scope=None` vs `force_scope='all'` vs `force_scope='<id>'`, or
# running the full sync against a fixture source with operator_overrides
# preset and asserting field preservation — see test_imports27 for the
# scope-aware behavioral coverage).
# That's an additive follow-up; the source-scan above catches the most
# common regressions: pattern drift, accidental removal of the gate, drop
# of operator_overrides protection.
