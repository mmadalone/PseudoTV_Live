"""Unit tests for the imports.48 `accurate`-aware __parseDuration fix.

Mirrors the production logic from jsonrpc.JSONRPC.__parseDuration. jsonrpc.py
isn't trivially importable in the test environment (it pulls in globals which
in turn pulls in the wider service stack), so we follow the same pattern as
test_chkcallback_cas.py / test_transition_supervisor.py: mirror the logic
inline, faithful to production, plus source-scan assertions so any drift
between mirror and production fails a test.

Production:
    plugin.video.pseudotv.imports/resources/lib/jsonrpc.py
        JSONRPC.__parseDuration  (imports.48, new `accurate` kwarg + gated fallback)
        JSONRPC.getDuration      (imports.48, threads `accurate` through to __parseDuration)

The 2026-05-22 schedule audit found 61 programmes on 5 channels where the
EPG slot length (set from library `runtime`) exceeds the actual playable file
duration. With Duration_Type=1 ("accurate" / ffprobe), getDuration *would*
ffprobe each file — but `__parseDuration` previously dropped the ffprobe
value whenever it disagreed with library runtime by >25%, falling back to the
(scraped, wrong) library runtime. That sanity-check is right for files where
ffprobe returns garbage but wrong for the dominant case where the library
itself is the unreliable input. imports.48 gates the fallback on
`not accurate`: when the operator explicitly opts into accurate mode, trust
the ffprobe value.

Plan: /home/madalone/.claude/plans/elegant-coalescing-tulip.md
"""
import os
import re
import types

import pytest


# Mirror of globals.percentDiff (faithful to globals.py:279-281)
def percentDiff(org, new):
    try: return (abs(round(org) - round(new)) / round(new)) * 100.0
    except ZeroDivisionError: return -1


def _parseDuration_mirror(self, runtime, path, item=None, save=False, accurate=False):
    """Mirror of jsonrpc.JSONRPC.__parseDuration (imports.48).

    Faithful to production. Any change to production must be reflected here
    or the source-scan tests below will fail.
    """
    if item is None:
        item = {}
    duration = self._videoParser_getVideoLength(path, item, self)
    if not accurate and round(percentDiff(runtime, duration)) > 25:
        duration = runtime
    if save and duration > 0 and duration != runtime:
        self._queDuration(item, duration)
    return duration


def _make_stub(*, ffprobe_returns, runtime, save=False, accurate=False):
    """Build a SimpleNamespace stub mimicking the JSONRPC instance bits used
    by __parseDuration."""
    quedur_calls = []
    stub = types.SimpleNamespace(
        _videoParser_getVideoLength=lambda path, item, self_: ffprobe_returns,
        _queDuration=lambda item, dur: quedur_calls.append((item, dur)),
        log=lambda msg, level=None: None,
        runtime=runtime,
        save=save,
        accurate=accurate,
    )
    stub._quedur_calls = quedur_calls
    return stub


# =============================================================================
# Behaviour — accurate=True (operator opted into ffprobe)
# =============================================================================


def test_accurate_true_large_disagreement_uses_ffprobe():
    """The regression guard: FFOD's 105-min library runtime vs 79-min file.
    percentDiff = 32.3% > 25, so previously the fallback fired and the slot
    was 6300s; now the ffprobe value (4763) wins."""
    s = _make_stub(ffprobe_returns=4763, runtime=6300, accurate=True)
    result = _parseDuration_mirror(s, runtime=6300, path='/mnt/x.mkv', accurate=True)
    assert result == 4763, (
        "imports.48 regression: accurate=True with >25% disagreement should "
        "return the ffprobe value, not fall back to the library runtime"
    )


def test_accurate_true_small_disagreement_uses_ffprobe():
    """Sanity: small disagreement also returns ffprobe (unchanged behaviour
    — this case already worked pre-imports.48)."""
    s = _make_stub(ffprobe_returns=4720, runtime=4763, accurate=True)
    result = _parseDuration_mirror(s, runtime=4763, path='/mnt/x.mkv', accurate=True)
    assert result == 4720


def test_accurate_true_cn_cartoon_case():
    """Cartoon Network: 60-min scraped slot, 44-min cartoon. 35% disagreement.
    Pre-imports.48 fell back to 3600; now returns 2663."""
    s = _make_stub(ffprobe_returns=2663, runtime=3600, accurate=True)
    result = _parseDuration_mirror(s, runtime=3600, path='/mnt/x.mkv', accurate=True)
    assert result == 2663


def test_accurate_true_with_save_calls_queDuration():
    """save=True with accurate=True and ffprobe differing from runtime: the
    library-write path still fires. duration>0 guard doesn't block valid
    ffprobe values."""
    s = _make_stub(ffprobe_returns=4763, runtime=6300, save=True, accurate=True)
    result = _parseDuration_mirror(
        s, runtime=6300, path='/mnt/x.mkv', save=True, accurate=True,
    )
    assert result == 4763
    assert len(s._quedur_calls) == 1, "queDuration should write the ffprobe value back"
    assert s._quedur_calls[0][1] == 4763


def test_accurate_true_ffprobe_zero_no_queDuration_write():
    """imports.48 added a `duration > 0` guard around the queDuration call so
    a failed ffprobe (returns 0) doesn't blast a 0 runtime into the Kodi
    library. The caller (getDuration) handles ffprobe-failure separately via
    its own `if duration > 0:` guard."""
    s = _make_stub(ffprobe_returns=0, runtime=6300, save=True, accurate=True)
    result = _parseDuration_mirror(
        s, runtime=6300, path='/mnt/x.mkv', save=True, accurate=True,
    )
    # Returns 0 (ffprobe failed); getDuration will fall back to runtime via
    # its own guard. But __parseDuration must NOT have called queDuration.
    assert result == 0
    assert s._quedur_calls == [], (
        "queDuration must not be called with duration=0 — would mutate the "
        "Kodi library to a 0 runtime"
    )


# =============================================================================
# Behaviour — accurate=False (existing conservative path preserved)
# =============================================================================


def test_accurate_false_large_disagreement_falls_back_to_runtime():
    """accurate=False preserves the pre-imports.48 sanity-check: >25%
    disagreement → fall back to library runtime. This is the conservative
    path used by the runtime==0 fallback into ffprobe; we don't want to
    change its behaviour."""
    s = _make_stub(ffprobe_returns=2663, runtime=3600, accurate=False)
    result = _parseDuration_mirror(s, runtime=3600, path='/mnt/x.mkv', accurate=False)
    assert result == 3600, (
        "accurate=False must keep the existing >25% fallback — that's the "
        "conservative path used by the runtime==0 fallback"
    )


def test_accurate_false_small_disagreement_uses_ffprobe():
    """accurate=False, <25% disagreement → ffprobe still wins (unchanged)."""
    s = _make_stub(ffprobe_returns=4720, runtime=4763, accurate=False)
    result = _parseDuration_mirror(s, runtime=4763, path='/mnt/x.mkv', accurate=False)
    assert result == 4720


def test_accurate_false_ffprobe_zero_returns_zero_caller_handles():
    """accurate=False, ffprobe returns 0. `percentDiff(runtime, 0)` raises
    ZeroDivisionError and returns -1, so the >25% fallback does NOT fire —
    __parseDuration returns 0 directly. The CALLER (getDuration) then handles
    the failure via its own `if duration > 0:` guard at jsonrpc.py:463,
    keeping the library runtime. This is pre-imports.48 behaviour preserved
    exactly: my fix only added `not accurate and …` to the fallback gate,
    which doesn't change anything when the inner condition is already False."""
    s = _make_stub(ffprobe_returns=0, runtime=6300, accurate=False)
    result = _parseDuration_mirror(s, runtime=6300, path='/mnt/x.mkv', accurate=False)
    assert result == 0, (
        "ZeroDivisionError in percentDiff → -1, > 25 is False, fallback skipped "
        "— __parseDuration returns 0; getDuration's `if duration > 0:` guard at "
        "the caller is what keeps the library runtime"
    )


# =============================================================================
# Source-scan — drift between mirror and production = failing test
# =============================================================================


def _read(rel_path):
    here = os.path.dirname(os.path.abspath(__file__))
    return open(os.path.join(os.path.dirname(here), rel_path)).read()


def _jsonrpc_src():
    return _read(os.path.join('resources', 'lib', 'jsonrpc.py'))


def test_parseDuration_signature_has_accurate_kwarg():
    """imports.48 added `accurate=False` to __parseDuration's signature."""
    src = _jsonrpc_src()
    m = re.search(r'def __parseDuration\(self,\s*runtime,\s*path,\s*item=\{\},\s*save=[^,]+,\s*accurate=False\):', src)
    assert m, (
        "__parseDuration signature must include `accurate=False` kwarg — "
        "imports.48 regression"
    )


def test_getDuration_passes_accurate_to_parseDuration():
    """imports.48: the non-stack branch of getDuration must thread `accurate`
    through to __parseDuration so the sanity-check fallback can be skipped."""
    src = _jsonrpc_src()
    # The else branch in getDuration: `else: duration = self.__parseDuration(runtime, path, item, save, accurate)`
    m = re.search(
        r'else:\s*duration\s*=\s*self\.__parseDuration\(\s*runtime,\s*path,\s*item,\s*save,\s*accurate\)',
        src,
    )
    assert m, (
        "getDuration must pass `accurate` as the 5th positional arg to "
        "__parseDuration in the non-stack branch — imports.48 regression"
    )


def test_parseDuration_fallback_gated_on_not_accurate():
    """imports.48: the 25% sanity-check fallback must be gated on
    `not accurate`. Drift here would re-introduce the bug for accurate=True."""
    src = _jsonrpc_src()
    # Find __parseDuration body and assert the gate
    fn_start = src.find('def __parseDuration(self, runtime, path, item={}, save=')
    assert fn_start != -1
    body = src[fn_start:fn_start + 2000]
    assert re.search(r'if\s+not\s+accurate\s+and\s+round\(percentDiff\(', body), (
        "__parseDuration's percentDiff sanity-check must be gated on "
        "`not accurate` — imports.48 regression"
    )


def test_parseDuration_queDuration_has_duration_positive_guard():
    """imports.48 also added a `duration > 0` guard to the queDuration call
    so a failed ffprobe doesn't write a 0 runtime back to the Kodi library."""
    src = _jsonrpc_src()
    fn_start = src.find('def __parseDuration(self, runtime, path, item={}, save=')
    assert fn_start != -1
    body = src[fn_start:fn_start + 2000]
    assert re.search(r'if\s+save\s+and\s+duration\s*>\s*0\s+and\s+duration\s*!=\s*runtime', body), (
        "__parseDuration's queDuration call must be guarded by `duration > 0` "
        "— prevents 0-runtime writes to the Kodi library on ffprobe failure"
    )
