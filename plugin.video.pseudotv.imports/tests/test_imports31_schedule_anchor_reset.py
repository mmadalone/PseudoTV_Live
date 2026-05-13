"""Regression tests for imports.31 — reset Builder schedule anchor
after `__clrStation` wipe.

Bug: in `Builder.buildChannels` per-channel loop, `start` is captured
from `__needsUpdate` (which returns the channel's PRE-clear last_stop)
BEFORE `__hasChanged` runs and (when True) calls `__clrStation` to wipe
the channel's M3U + programmes. The downstream `addScheduling` call
then uses the stale `start` as the anchor for new programmes. When
the prior schedule extended far into the future (e.g., 10+ days),
`start` is a future timestamp, leaving a gap between `now` and the
first new programme. The gap forces a second rebuild on the next
chkChannels cycle to fill it (~9 min/channel wasted).

Fix: after `__hasChanged` runs, if `_changed=True` (i.e., __clrStation
ran), reset `start = roundTimeDown(now, offset=60)` so the new
schedule anchors at the current bottom-of-hour instead of the stale
pre-clear last_stop. The reset is gated by `_changed`, NOT by
`_update` — the extend-only path (`_update=True, _changed=False`)
correctly anchors at last_stop to preserve catchup semantics for
Custom channels with `catchup_mode='vod'`.

Source-scan tests only — the fix is a single-line conditional rebind
inside a deeply-nested loop that's expensive to set up behaviorally.
Source-scan guards catch real regressions (mirrors test_imports27/
imports29 source-scan asserts on filter ordering).

Plan: /home/madalone/.claude/plans/let-s-plan-for-2-drifting-tulip.md
"""
import os
import re


HERE = os.path.dirname(os.path.abspath(__file__))
ADDON_ROOT = os.path.dirname(HERE)
LIB = os.path.join(ADDON_ROOT, 'resources', 'lib')


def _read(path):
    with open(path) as f:
        return f.read()


def _builder_src():
    return _read(os.path.join(LIB, 'builder.py'))


def _per_channel_loop_body():
    """Slice the per-channel build loop body in builder.py.

    The loop is the SECOND `for idx, citem in enumerate(channels):` in
    the file (the first one is inside `_verify` at line ~202; the
    buildChannels loop is at line ~437). Returns the body from the
    second occurrence up to the next sibling `try:` / function def at
    the 8-space indent."""
    src = _builder_src()
    first = src.find('for idx, citem in enumerate(channels):')
    assert first > 0
    second = src.find('for idx, citem in enumerate(channels):', first + 1)
    assert second > 0, "expected two enumerate(channels) loops in builder.py"
    # Slice generously — far enough to cover addScheduling call at ~line 521
    return src[second:second + 16000]


# ======================================================================
# Source-scan: reset is inserted in the right place
# ======================================================================

def test_builder_resets_start_after_hasChanged():
    """The fix must add `if _changed:` + `start = roundTimeDown(now, offset=60)`
    between `_update, start = __needsUpdate(...)` and the downstream
    `addScheduling(citem, fileList, now, start)` call."""
    body = _per_channel_loop_body()
    needs_pos = body.find('_update, start = __needsUpdate(citem,')
    changed_pos = body.find('_changed = __hasChanged(citem,')
    reset_pos = body.find('start = roundTimeDown(now, offset=60)')
    sched_pos = body.find('addScheduling(citem, fileList, now, start)')

    assert needs_pos > 0, "__needsUpdate call site not found"
    assert changed_pos > 0, "__hasChanged call site not found"
    assert reset_pos > 0, (
        "Missing `start = roundTimeDown(now, offset=60)` reset in the "
        "buildChannels per-channel loop — imports.31 fix regressed."
    )
    assert sched_pos > 0, "addScheduling call site not found"

    # Ordering: __needsUpdate → __hasChanged → reset → addScheduling
    assert needs_pos < changed_pos < reset_pos < sched_pos, (
        "imports.31 ordering broken. Expected: __needsUpdate < "
        "__hasChanged < reset < addScheduling. Got: needs=%d, "
        "changed=%d, reset=%d, sched=%d" % (
            needs_pos, changed_pos, reset_pos, sched_pos)
    )


def test_builder_anchor_reset_only_under_changed():
    """The reset must be gated by `if _changed:` — not unconditional,
    not gated on `_update`. Unconditional would break the extend-only
    path (which needs `start = last_stop` to preserve catchup). `_update`
    gating would miss the bug case (when only `_changed=True` fires)."""
    body = _per_channel_loop_body()
    # The reset line itself
    reset = re.search(
        r'if _changed:\s*\n\s+start = roundTimeDown\(now, offset=60\)',
        body,
    )
    assert reset is not None, (
        "imports.31 reset must be guarded by `if _changed:` exactly. "
        "Catches: dropped guard (unconditional), wrong guard "
        "(`if _update:`), or syntax drift."
    )

    # Defensive: no `if _update:` sibling that would also reset start
    # to a non-last_stop value (would break catchup-on-extend).
    bad_pat = re.search(
        r'if _update:\s*\n\s+start = roundTimeDown',
        body,
    )
    assert bad_pat is None, (
        "imports.31 must NOT also reset `start` inside an `if _update:` "
        "branch — that would break catchup semantics for the extend-"
        "only path. Found a suspicious match: %r" % (bad_pat.group(0) if bad_pat else None)
    )


def test_builder_anchor_reset_appears_before_addScheduling():
    """Stricter version of test #1: the reset must appear BEFORE the
    `addScheduling(citem, fileList, now, start)` call in source order.
    Catches a misplacement where someone moves the reset INTO the
    inner `if start > 0:` block where it'd execute AFTER addScheduling
    (or not at all)."""
    body = _per_channel_loop_body()
    reset_pos = body.find('start = roundTimeDown(now, offset=60)')
    sched_pos = body.find('addScheduling(citem, fileList, now, start)')
    assert 0 < reset_pos < sched_pos, (
        "imports.31 reset must precede addScheduling in source order. "
        "reset=%d, sched=%d" % (reset_pos, sched_pos)
    )


def test_builder_anchor_uses_roundTimeDown_not_bare_now():
    """The anchor must use `roundTimeDown(now, offset=60)` for
    bottom-of-hour alignment. Bare `now` would produce fractional-
    second-aligned schedules that look odd in skin guides and don't
    match the addon's `nstart` convention at builder.py:429."""
    body = _per_channel_loop_body()
    # The fix uses the canonical call
    assert 'start = roundTimeDown(now, offset=60)' in body, (
        "imports.31 reset must use `roundTimeDown(now, offset=60)` "
        "(matches builder.py:429 `nstart` convention + xmltvs.py:266 "
        "fallback semantics). Bare `now` is wrong."
    )

    # Catch a bare-`now` reset under `if _changed:`
    bare_now_pat = re.search(
        r'if _changed:\s*\n\s+start = now\b',
        body,
    )
    assert bare_now_pat is None, (
        "imports.31 reset uses bare `now` instead of "
        "`roundTimeDown(now, offset=60)` — alignment convention "
        "regressed."
    )


def test_builder_anchor_reset_NOT_in_update_only_path():
    """The fix must NOT reset `start` in the `_update`-only path
    (extend without wipe). That path correctly anchors at `last_stop`
    to preserve catchup semantics — past programmes intentionally
    scheduled when `catchup_mode='vod'`. Regression here would break
    catchup for Custom channels.

    Check: there's exactly ONE `start = roundTimeDown(now, offset=60)`
    in the loop body, and it's under `if _changed:` (test #2 already
    confirms the guard; this test confirms no DUPLICATE reset exists
    that might fire under `_update`)."""
    body = _per_channel_loop_body()
    # Count occurrences of the exact reset line
    count = body.count('start = roundTimeDown(now, offset=60)')
    assert count == 1, (
        "Expected exactly ONE `start = roundTimeDown(now, offset=60)` "
        "reset in the per-channel loop body; found %d. Multiple "
        "resets risk over-applying the fix and breaking catchup."
        % count
    )


def test_anchor_reset_documented_with_imports_31_comment():
    """The fix must be preceded by a comment block that mentions
    `imports.31` and the `__clrStation` rationale. Helps future
    grep-based code archaeology and makes the design intent
    discoverable."""
    body = _per_channel_loop_body()
    # Capture the lines around the reset
    reset_pos = body.find('if _changed:\n')
    assert reset_pos > 0
    # Look at the 2400 chars before for the comment (block is long
    # due to 28-space indent eating most of each line)
    preamble = body[max(0, reset_pos - 2400):reset_pos]
    assert 'imports.31' in preamble, (
        "imports.31 reset must be preceded by an `imports.31` comment "
        "marker so future code archaeology / grep can locate the fix."
    )
    assert '__clrStation' in preamble, (
        "imports.31 comment must explain that __clrStation just wiped "
        "the channel — that's the WHY for the reset."
    )
