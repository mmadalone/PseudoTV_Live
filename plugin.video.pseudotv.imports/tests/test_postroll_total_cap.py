"""imports.52 — Enable_Postroll literal total cap.

Pre-imports.52, `Enable_Postroll` was a preset spinner (-1/0/1/2/3) and the
post-roll loop in `fillers.injectBCTs` set `postFillRuntime = MIN_EPG_DURATION`
(10800s = 3 hours) when not in Auto mode. The "One Insert" label was
misleading: numberToFetch=1 was the per-ftype FETCH cap, but the inner loop
then consumed items until either the 3-hour time budget was exhausted OR the
pool ran dry. Operators setting Enable_Postroll=1 saw multiple items between
programmes (live-observed 5+ on FFOD with full bumper packs configured).

imports.52 changes:
1. settings.xml — Enable_Postroll spinner → integer slider, range -1..20.
2. fillers.py — after merging postFileList across ftypes (and shuffling for
   ftype balance), apply a HARD TOTAL CAP equal to the Enable_Postroll value
   when not in Auto mode. The value is now literally the max items inserted
   per programme boundary across all ftypes combined.

Semantics:
- Enable_Postroll = -1 (Auto): UNCAPPED, fills the available gap as before.
- Enable_Postroll =  0 (Off): post-roll disabled by the outer `enabled` gate.
- Enable_Postroll =  N>0: exactly min(N, available_pool) items per gap.
"""
import os
import re


def _apply_cap_mirror(postFileList, enable_postroll_value, any_ftype_auto):
    """Mirror of post-imports.52 fillers.injectBCTs cap logic."""
    # Caller is expected to have already shuffled postFileList for ftype balance.
    if not any_ftype_auto:
        cap = enable_postroll_value
        if cap > 0 and len(postFileList) > cap:
            postFileList = postFileList[:cap]
    return postFileList


# ─────────────────────────────────────────────────────────────────── behaviour


def test_cap_of_1_yields_exactly_1_item():
    """The smoking-gun: Enable_Postroll=1 in non-Auto mode = literally 1 item
    per gap. Pre-imports.52 this same input produced multiple items."""
    pool = [{'id': i, 'duration': 60} for i in range(10)]
    result = _apply_cap_mirror(pool, 1, any_ftype_auto=False)
    assert len(result) == 1


def test_cap_of_3_yields_exactly_3_items():
    pool = [{'id': i, 'duration': 60} for i in range(10)]
    result = _apply_cap_mirror(pool, 3, any_ftype_auto=False)
    assert len(result) == 3


def test_cap_larger_than_pool_returns_all_pool_items():
    """Cap=20 but pool only has 5 items → returns all 5 unchanged."""
    pool = [{'id': i, 'duration': 60} for i in range(5)]
    result = _apply_cap_mirror(pool, 20, any_ftype_auto=False)
    assert len(result) == 5
    assert result == pool


def test_auto_mode_bypasses_cap():
    """Enable_Postroll=-1 (Auto) — the value passed in is irrelevant because
    any_ftype_auto=True triggers the bypass."""
    pool = [{'id': i, 'duration': 60} for i in range(10)]
    result = _apply_cap_mirror(pool, -1, any_ftype_auto=True)
    assert len(result) == 10  # unbounded


def test_cap_of_zero_does_not_slice():
    """Cap=0 (Disabled) — the outer enabled gate would prevent items from
    entering the pool anyway, but defensively the cap code must not slice to
    [:0] which would mask the upstream gate."""
    pool = [{'id': i, 'duration': 60} for i in range(3)]
    result = _apply_cap_mirror(pool, 0, any_ftype_auto=False)
    assert len(result) == 3  # cap not applied (cap > 0 required)


def test_cap_preserves_first_N_after_shuffle():
    """The cap is `postFileList[:cap]` — taking the first N items. The
    caller MUST shuffle before calling the cap (which the actual code does
    at line 237) to avoid biasing toward the first ftype's items. This test
    documents that the cap itself doesn't shuffle."""
    pool = [{'id': 'a1'}, {'id': 'a2'}, {'id': 't1'}, {'id': 't2'}]
    result = _apply_cap_mirror(pool, 2, any_ftype_auto=False)
    assert result == [{'id': 'a1'}, {'id': 'a2'}]


# ──────────────────────────────────────────────────────────────── source-scan


def test_source_scan_fillers_applies_total_cap():
    """imports.52 drift guard. fillers.py injectBCTs must contain the cap
    slice `postFileList = postFileList[:cap]` inside a `not postAuto` branch."""
    here = os.path.dirname(os.path.abspath(__file__))
    src  = open(os.path.join(os.path.dirname(here), 'resources', 'lib', 'fillers.py')).read()
    # The slice must exist (with optional whitespace variation)
    assert re.search(r'postFileList\s*=\s*postFileList\[\s*:\s*cap\s*\]', src), (
        'imports.52 regression: fillers.py must slice postFileList[:cap]'
    )
    # The slice must be guarded by `not postAuto`
    assert re.search(r'if\s+not\s+postAuto\s*:', src), (
        'imports.52 regression: cap must be gated on `not postAuto`'
    )


def test_source_scan_settings_xml_has_slider():
    """Enable_Postroll must be a slider with -1 to 20 range in settings.xml."""
    here = os.path.dirname(os.path.abspath(__file__))
    src  = open(os.path.join(os.path.dirname(here), 'resources', 'settings.xml')).read()
    # Find the Enable_Postroll setting block
    m = re.search(r'<setting id="Enable_Postroll".*?</setting>', src, re.DOTALL)
    assert m, 'Enable_Postroll setting not found in settings.xml'
    block = m.group(0)
    assert re.search(r'<minimum>\s*-1\s*</minimum>', block), 'slider min must be -1'
    assert re.search(r'<maximum>\s*(?:1[0-9]|20)\s*</maximum>', block), 'slider max must be 10-20'
    assert 'type="slider"' in block, 'control must be slider'
    # Ensure the old preset options are GONE
    assert '<options>' not in block, 'old preset spinner <options> still present'
