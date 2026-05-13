"""Regression tests for imports.29 — render-state drift detection in
`_filterChannelsNeedingBuild`.

Bug: operator renumbered Custom channels in a prior session; channels.json
held the new numbers + 'number' in operator_overrides, but pseudotv.m3u
still had the OLD numbers. Root cause: the rebuild signal is the
transient `changed=True` flag, set per-edit-path (server.py:629 always;
manager.py:itemInput conditionally; switchLogo never). Paths that miss
the flag silently bypass Builder, and the filter has no other way to
detect that channels.json + pseudotv.m3u have diverged.

Fix: filter detects render-state drift by comparing each Custom
channel's channels.json fields against the corresponding entry in
M3U().M3UDATA['stations']. Mismatch on number/name/logo/group/catchup/
radio/favorite → mark changed=True → queue Builder. Self-healing — no
per-edit-path patching, no schema migration.

Source-scan style mirrors test_imports22 / .25 / .27 / .28 for the
static guards; behavioral tests exercise `_renderStateDrift` directly
plus monkey-patch the M3U class to feed the filter deterministic
station data.

Plan: /home/madalone/.claude/plans/let-s-plan-for-2-drifting-tulip.md
"""
import os
import re
import sys


HERE = os.path.dirname(os.path.abspath(__file__))
ADDON_ROOT = os.path.dirname(HERE)
LIB = os.path.join(ADDON_ROOT, 'resources', 'lib')


def _read(path):
    with open(path) as f:
        return f.read()


if LIB not in sys.path:
    sys.path.insert(0, LIB)


# ======================================================================
# Source-scan guards
# ======================================================================

def test_filter_helpers_module_defines_render_keys():
    """filter_helpers.py (extracted from tasks.py to avoid Kodi-stub
    dependency drag in tests) must define RENDER_KEYS at module scope
    with exactly the seven operator-mutable fields that end up rendered
    into pseudotv.m3u. Adding fields to this tuple is a real design
    decision — see plan."""
    src = _read(os.path.join(LIB, 'filter_helpers.py'))
    m = re.search(r'RENDER_KEYS\s*=\s*\(([^)]+)\)', src)
    assert m is not None, (
        "filter_helpers.py missing module-level RENDER_KEYS — imports.29 "
        "drift detection helper depends on it."
    )
    body = m.group(1)
    for field in ('number', 'name', 'logo', 'group', 'catchup', 'radio', 'favorite'):
        assert "'%s'" % field in body, (
            "RENDER_KEYS missing %r — drift detection wouldn't catch operator "
            "edits to that field." % field
        )
    # Explicit exclusions per plan — adding these here means regression
    # would be loud.
    for field in ('path', 'rules', 'enabled', 'label'):
        assert "'%s'" % field not in body, (
            "RENDER_KEYS must NOT include %r — plan rationale: %r is not "
            "in M3U render output OR handled separately. See plan." % (field, field)
        )


def test_filter_helpers_defines_render_state_drift():
    """`_renderStateDrift(citem, sitem)` module-level pure function in
    filter_helpers (extracted from tasks.py)."""
    src = _read(os.path.join(LIB, 'filter_helpers.py'))
    assert re.search(r'def _renderStateDrift\(citem,\s*sitem\):', src), (
        "filter_helpers.py missing _renderStateDrift(citem, sitem)."
    )


def test_tasks_imports_from_filter_helpers():
    """tasks.py must import RENDER_KEYS and _renderStateDrift from
    filter_helpers (not re-define them inline). Single source of truth."""
    src = _read(os.path.join(LIB, 'tasks.py'))
    assert re.search(
        r'from filter_helpers\s+import\s+RENDER_KEYS,\s*_renderStateDrift',
        src,
    ), (
        "tasks.py missing `from filter_helpers import RENDER_KEYS, "
        "_renderStateDrift` — extraction regressed."
    )
    # And RENDER_KEYS must NOT be defined inline anymore.
    assert not re.search(r'^RENDER_KEYS\s*=', src, re.MULTILINE), (
        "tasks.py defines RENDER_KEYS at module scope — should import "
        "from filter_helpers instead. Re-inlining loses the test-import "
        "decoupling."
    )


def test_filter_uses_m3udata_stations_key():
    """Code must reference `M3UDATA.get('stations')` — iterating M3UDATA
    bare would walk dict keys ('data', 'stations', 'recordings'), not
    the station list. This was a real bug in the first draft."""
    src = _read(os.path.join(LIB, 'tasks.py'))
    # The drift loader must specifically use .get('stations')
    assert "M3UDATA.get('stations')" in src, (
        "tasks.py filter doesn't use M3UDATA.get('stations') — iterating "
        "M3UDATA bare would walk dict keys, not the station list."
    )


def test_filter_imports_M3U_at_module_level():
    """imports.29 wires M3U at module-level (matching the XMLTVS pattern
    at line 25). The pre-existing local `from m3u import M3U` at the
    chkImports function-level is fine to leave; this test just guards
    that the module-level import landed."""
    src = _read(os.path.join(LIB, 'tasks.py'))
    assert re.search(r'^from m3u\s+import M3U', src, re.MULTILINE), (
        "tasks.py missing module-level `from m3u import M3U` — drift "
        "check would NameError without it."
    )


def test_filter_drift_check_runs_before_crc():
    """Drift detection must run BEFORE the CRC branch inside
    _filterChannelsNeedingBuild. Reversed order would mean a CRC change
    sets changed=True first, drift would then short-circuit on the
    `changed=True` branch and never actually compare M3U state."""
    src = _read(os.path.join(LIB, 'tasks.py'))
    # Find the function body
    fn = re.search(
        r'def _filterChannelsNeedingBuild\(self, channels\):(.*?)(?=\n    def )',
        src, re.DOTALL,
    )
    assert fn is not None, "filter function not found"
    body = fn.group(1)
    # Use the actual code-call form to avoid matching the docstring/comment
    # block at the top of the function that mentions getFileCRC narratively.
    drift_pos = body.find('_renderStateDrift(citem')
    crc_pos   = body.find('SETTINGS.getFileCRC(')
    assert 0 < drift_pos < crc_pos, (
        "_renderStateDrift call must appear BEFORE getFileCRC in "
        "_filterChannelsNeedingBuild body. drift=%d, crc=%d" % (drift_pos, crc_pos)
    )


def test_filter_imports_short_circuit_before_drift():
    """Import channels short-circuit BEFORE the drift check — they go
    through Imports.syncAll, not Builder. Order: type=='import' check
    appears in source before _renderStateDrift call."""
    src = _read(os.path.join(LIB, 'tasks.py'))
    fn = re.search(
        r'def _filterChannelsNeedingBuild\(self, channels\):(.*?)(?=\n    def )',
        src, re.DOTALL,
    )
    body = fn.group(1)
    imports_pos = body.find("'type'")
    drift_pos   = body.find('_renderStateDrift')
    assert 0 < imports_pos < drift_pos, (
        "Imports type-check must short-circuit BEFORE _renderStateDrift "
        "to avoid wasted dict lookups on the (typically >100) imported "
        "channels."
    )


def test_filter_disabled_short_circuit_before_drift():
    """Disabled Custom channels short-circuit BEFORE the drift check
    (Builder._verify drops them anyway; queuing would just churn)."""
    src = _read(os.path.join(LIB, 'tasks.py'))
    fn = re.search(
        r'def _filterChannelsNeedingBuild\(self, channels\):(.*?)(?=\n    def )',
        src, re.DOTALL,
    )
    body = fn.group(1)
    disabled_pos = body.find("citem.get('enabled', True)")
    drift_pos    = body.find('_renderStateDrift')
    assert 0 < disabled_pos < drift_pos, (
        "Disabled-channel short-circuit must appear BEFORE _renderStateDrift "
        "— per plan, disabled channels are excluded from drift to avoid "
        "infinite re-queue against Builder._verify's drop."
    )


def test_filter_summary_log_includes_drift_count():
    """The summary log line at the bottom of the filter must include the
    drift counter alongside crc_detected — telemetry for operator
    diagnosis."""
    src = _read(os.path.join(LIB, 'tasks.py'))
    assert 'drift = %s' in src and 'drift_detected' in src, (
        "tasks.py filter summary log missing 'drift = ...' / drift_detected — "
        "operator wouldn't see when drift detection fired."
    )


# ======================================================================
# Unit tests for _renderStateDrift
# ======================================================================

def test_renderStateDrift_returns_true_when_sitem_missing():
    """sitem=None (channel not yet in M3U) → drift True."""
    from filter_helpers import _renderStateDrift
    citem = {'id': 'X', 'number': 100, 'name': 'NEW', 'logo': 'a.png'}
    assert _renderStateDrift(citem, None) is True


def test_renderStateDrift_number_mismatch():
    """citem number=300, sitem number=303 → drift True. The renumber bug."""
    from filter_helpers import _renderStateDrift
    citem = {'id': 'X', 'number': 300, 'name': 'A', 'logo': 'a.png'}
    sitem = {'id': 'X', 'number': 303, 'name': 'A', 'logo': 'a.png'}
    assert _renderStateDrift(citem, sitem) is True


def test_renderStateDrift_name_mismatch():
    """name diff → drift True."""
    from filter_helpers import _renderStateDrift
    citem = {'id': 'X', 'number': 1, 'name': 'NEW', 'logo': 'a.png'}
    sitem = {'id': 'X', 'number': 1, 'name': 'OLD', 'logo': 'a.png'}
    assert _renderStateDrift(citem, sitem) is True


def test_renderStateDrift_logo_mismatch():
    """logo diff → drift True."""
    from filter_helpers import _renderStateDrift
    citem = {'id': 'X', 'number': 1, 'name': 'A', 'logo': 'new.png'}
    sitem = {'id': 'X', 'number': 1, 'name': 'A', 'logo': 'old.png'}
    assert _renderStateDrift(citem, sitem) is True


def test_renderStateDrift_group_order_independence():
    """citem group=['A','B'], sitem group=['B','A'] → drift False.
    M3U._load dedupes/sorts via set, so order is never meaningful on
    the M3U side — avoid perpetual-drift loop."""
    from filter_helpers import _renderStateDrift
    citem = {'id': 'X', 'number': 1, 'name': 'A', 'logo': 'a.png',
             'group': ['Movies', 'Drama'], 'catchup': '', 'radio': False, 'favorite': False}
    sitem = {'id': 'X', 'number': 1, 'name': 'A', 'logo': 'a.png',
             'group': ['Drama', 'Movies'], 'catchup': '', 'radio': False, 'favorite': False}
    assert _renderStateDrift(citem, sitem) is False, (
        "Group order must not count as drift — M3U._load dedupes/sorts "
        "via set so it has no meaningful order."
    )


def test_renderStateDrift_group_membership_change():
    """group=['A','B'] vs ['A'] (real membership change) → drift True."""
    from filter_helpers import _renderStateDrift
    citem = {'id': 'X', 'number': 1, 'name': 'A', 'logo': 'a.png',
             'group': ['Movies', 'Drama'], 'catchup': '', 'radio': False, 'favorite': False}
    sitem = {'id': 'X', 'number': 1, 'name': 'A', 'logo': 'a.png',
             'group': ['Movies'], 'catchup': '', 'radio': False, 'favorite': False}
    assert _renderStateDrift(citem, sitem) is True


def test_renderStateDrift_all_fields_aligned():
    """Every RENDER_KEY matches → drift False."""
    from filter_helpers import _renderStateDrift
    base = {'id': 'X', 'number': 1, 'name': 'A', 'logo': 'a.png',
            'group': ['M'], 'catchup': 'vod', 'radio': False, 'favorite': True}
    assert _renderStateDrift(dict(base), dict(base)) is False


def test_renderStateDrift_favorite_flip():
    """favorite True → False → drift True (catches the case where the
    operator unfavorites via Channel Manager and switchLogo path doesn't
    set changed=True)."""
    from filter_helpers import _renderStateDrift
    citem = {'id': 'X', 'number': 1, 'name': 'A', 'logo': 'a.png',
             'group': ['M'], 'catchup': '', 'radio': False, 'favorite': True}
    sitem = {'id': 'X', 'number': 1, 'name': 'A', 'logo': 'a.png',
             'group': ['M'], 'catchup': '', 'radio': False, 'favorite': False}
    assert _renderStateDrift(citem, sitem) is True


def test_renderStateDrift_catchup_mode_change():
    """catchup mode flip ('vod' → '' or vice versa) → drift True."""
    from filter_helpers import _renderStateDrift
    citem = {'id': 'X', 'number': 1, 'name': 'A', 'logo': 'a.png',
             'group': ['M'], 'catchup': 'vod', 'radio': False, 'favorite': False}
    sitem = {'id': 'X', 'number': 1, 'name': 'A', 'logo': 'a.png',
             'group': ['M'], 'catchup': '', 'radio': False, 'favorite': False}
    assert _renderStateDrift(citem, sitem) is True


def test_renderStateDrift_immutable_id_across_renumber():
    """Operator renumber must NOT regenerate the channel id. citem.id and
    sitem.id remain identical even when numbers diverge — that's why the
    M3U lookup `m3u_by_id.get(citem.get('id'))` can find the prior
    rendered entry to compare against. If the id changed, sitem would be
    None and we'd hit the 'missing sitem' branch instead — also drift
    True, but for the wrong semantic reason."""
    from filter_helpers import _renderStateDrift
    citem = {'id': 'STABLE_ID_X', 'number': 300, 'name': 'A', 'logo': 'a.png',
             'group': [], 'catchup': '', 'radio': False, 'favorite': False}
    sitem = {'id': 'STABLE_ID_X', 'number': 303, 'name': 'A', 'logo': 'a.png',
             'group': [], 'catchup': '', 'radio': False, 'favorite': False}
    # The id is the SAME in both; only number differs.
    assert citem['id'] == sitem['id']
    assert _renderStateDrift(citem, sitem) is True
