"""imports.53 — Enable_Preroll literal total cap.

Mirrors imports.52's post-roll cap for the pre-roll path. Pre-imports.53,
Enable_Preroll was a 2-option spinner (-1/0 = Auto/Off) with no count control;
the pre-roll loop in fillers.injectBCTs called `getSingle(ftype, preKeys, ...)`
which returns up to one item per preKey — movies use preKeys=[fmpaa, fcodec]
(up to 2 items), TV uses preKeys=[chname, fgenre] (up to 2 items). Operators
who wanted "exactly 1 pre-roll per programme" had no setting for it.

imports.53 changes:
1. settings.xml — Enable_Preroll spinner → integer slider, range -1..20.
2. fillers.py — after `getSingle(ftype, preKeys, ...)` returns preFileList,
   apply a HARD TOTAL CAP equal to the Enable_Preroll value when not in Auto
   mode. Shuffle before slicing to avoid biasing toward the first preKey.

Semantics:
- Enable_Preroll = -1 (Auto): UNCAPPED, original getSingle output passes through.
- Enable_Preroll =  0 (Off): disabled by outer `enabled` gate.
- Enable_Preroll =  N>0: exactly min(N, len(preFileList)) items per programme.
"""
import os
import re


def _apply_preroll_cap_mirror(preFileList, enable_preroll_value, ftype_auto):
    """Mirror of post-imports.53 fillers.injectBCTs pre-roll cap logic.

    Caller is expected to shuffle before slicing when the cap would trigger
    (which the actual code does inline). For test determinism, this mirror
    only slices and asserts on length, not on which specific items remain.
    """
    if not ftype_auto:
        cap = enable_preroll_value
        if cap > 0 and len(preFileList) > cap:
            preFileList = preFileList[:cap]
    return preFileList


# ─────────────────────────────────────────────────────────────────── behaviour


def test_cap_of_1_yields_exactly_1_item():
    """Smoking-gun: Enable_Preroll=1 in non-Auto mode = literally 1 pre-roll
    item per programme. Pre-imports.53 the operator had no slider option;
    getSingle returned up to 2 items (one per preKey)."""
    pool = [{'id': i} for i in range(5)]
    result = _apply_preroll_cap_mirror(pool, 1, ftype_auto=False)
    assert len(result) == 1


def test_cap_of_2_yields_exactly_2_items():
    pool = [{'id': i} for i in range(5)]
    result = _apply_preroll_cap_mirror(pool, 2, ftype_auto=False)
    assert len(result) == 2


def test_cap_larger_than_pool_returns_all_items():
    """Cap=20 but pool only has 2 items → returns both unchanged."""
    pool = [{'id': i} for i in range(2)]
    result = _apply_preroll_cap_mirror(pool, 20, ftype_auto=False)
    assert len(result) == 2
    assert result == pool


def test_auto_mode_bypasses_cap():
    """Enable_Preroll=-1 (Auto). ftype_auto=True triggers the bypass even if
    the cap value is non-Auto."""
    pool = [{'id': i} for i in range(5)]
    result = _apply_preroll_cap_mirror(pool, -1, ftype_auto=True)
    assert len(result) == 5


def test_cap_of_zero_does_not_slice():
    """Cap=0 (Disabled). Outer enabled gate prevents fetch; defensively the
    cap code does not slice to [:0] (cap > 0 required)."""
    pool = [{'id': i} for i in range(3)]
    result = _apply_preroll_cap_mirror(pool, 0, ftype_auto=False)
    assert len(result) == 3


# ──────────────────────────────────────────────────────────────── source-scan


def test_source_scan_fillers_applies_preroll_cap():
    """imports.53 drift guard. fillers.py injectBCTs pre-roll path must
    apply `preFileList = preFileList[:cap]` when not in Auto mode."""
    here = os.path.dirname(os.path.abspath(__file__))
    src  = open(os.path.join(os.path.dirname(here), 'resources', 'lib', 'fillers.py')).read()
    assert re.search(r'preFileList\s*=\s*preFileList\[\s*:\s*cap\s*\]', src), (
        'imports.53 regression: fillers.py must slice preFileList[:cap]'
    )
    # Must be guarded by `not bct.get('auto', False)` or similar
    assert re.search(r"not\s+bct\.get\('auto'", src), (
        'imports.53 regression: pre-roll cap must be gated on not bct.get(\'auto\', False)'
    )


def test_source_scan_settings_xml_preroll_has_slider():
    """Enable_Preroll must be a slider with -1..20 range in settings.xml."""
    here = os.path.dirname(os.path.abspath(__file__))
    src  = open(os.path.join(os.path.dirname(here), 'resources', 'settings.xml')).read()
    m = re.search(r'<setting id="Enable_Preroll".*?</setting>', src, re.DOTALL)
    assert m, 'Enable_Preroll setting not found in settings.xml'
    block = m.group(0)
    assert re.search(r'<minimum>\s*-1\s*</minimum>', block), 'pre-roll slider min must be -1'
    assert re.search(r'<maximum>\s*(?:1[0-9]|20)\s*</maximum>', block), 'pre-roll slider max must be 10-20'
    assert 'type="slider"' in block, 'pre-roll control must be slider'
    assert '<options>' not in block, 'pre-roll old preset spinner <options> still present'
