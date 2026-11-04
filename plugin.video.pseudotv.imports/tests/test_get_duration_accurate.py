"""Unit tests for the imports.48 `accurate`-aware __parseDuration fix.

imports.60: these tests now run the real jsonrpc.JSONRPC under the Kodi test
stand-ins (before, they ran a copy of the logic plus source-text checks, so a
change to the real code could pass them). The length write-back they used to
check (queDuration) was removed in imports.60; see
test_imports60_no_duration_writeback.py.

Production:
    plugin.video.pseudotv.imports/resources/lib/jsonrpc.py
        JSONRPC.__parseDuration  (imports.48: `accurate` kwarg + gated fallback)
        JSONRPC.getDuration      (imports.48: threads `accurate` through)

The 2026-05-22 schedule audit found 61 programmes on 5 channels where the
EPG slot length (set from library `runtime`) exceeds the actual playable file
duration. With Duration_Type=1 ("accurate" / ffprobe), getDuration *would*
ffprobe each file, but `__parseDuration` previously dropped the ffprobe value
whenever it disagreed with library runtime by >25%, falling back to the
(scraped, wrong) library runtime. imports.48 gates that fallback on
`not accurate`: when the operator explicitly opts into accurate mode, trust
the measured value.
"""
import types

import pytest

from jsonrpc import JSONRPC

PATH = '/mnt/x/Film (1972)/Film (1972).mkv'


class _Cache(object):
    def __init__(self): self.d = {}
    def get(self, name, checksum=None): return self.d.get(name)
    def set(self, name, value, checksum=None, expiration=None):
        self.d[name] = value
        return value


def _rpc(monkeypatch, probe):
    """Real JSONRPC with the file parser and cache replaced."""
    svc = types.SimpleNamespace(jsonQue=set(), _que=lambda *a, **k: pytest.fail('nothing may be queued'))
    rpc = JSONRPC(service=svc)
    monkeypatch.setattr(rpc, 'cache', _Cache())
    monkeypatch.setattr(rpc, 'videoParser', types.SimpleNamespace(getVideoLength=lambda path, item, jsonRPC: probe))
    monkeypatch.setattr(rpc, 'log', lambda msg, level=0: None)
    return rpc


def _parse(rpc, runtime, accurate):
    return rpc._JSONRPC__parseDuration(runtime, PATH, {'file': PATH}, accurate=accurate)


# =============================================================================
# accurate=True (operator opted into measuring)
# =============================================================================

def test_accurate_true_large_disagreement_uses_measured(monkeypatch):
    """The regression guard: a 105-min library runtime vs a 79-min file.
    percentDiff = 32.3% > 25, so pre-imports.48 the slot was 6300 s."""
    assert _parse(_rpc(monkeypatch, 4763), 6300, True) == 4763


def test_accurate_true_small_disagreement_uses_measured(monkeypatch):
    assert _parse(_rpc(monkeypatch, 4720), 4763, True) == 4720


def test_accurate_true_cartoon_case(monkeypatch):
    """A 60-min scraped slot, a 44-min cartoon (35% disagreement)."""
    assert _parse(_rpc(monkeypatch, 2663), 3600, True) == 2663


def test_accurate_true_measure_failed_getDuration_keeps_runtime(monkeypatch):
    """__parseDuration returns 0; getDuration's `if duration > 0` keeps the runtime."""
    rpc = _rpc(monkeypatch, 0)
    assert _parse(rpc, 6300, True) == 0
    assert rpc.getDuration(PATH, {'file': PATH, 'runtime': 6300}, True) == 6300


# =============================================================================
# accurate=False (conservative path preserved)
# =============================================================================

def test_accurate_false_large_disagreement_falls_back_to_runtime(monkeypatch):
    assert _parse(_rpc(monkeypatch, 2663), 3600, False) == 3600


def test_accurate_false_small_disagreement_uses_measured(monkeypatch):
    assert _parse(_rpc(monkeypatch, 4720), 4763, False) == 4720


def test_accurate_false_measure_zero_returns_zero(monkeypatch):
    """percentDiff(runtime, 0) hits ZeroDivisionError and returns -1, so the
    fallback does not fire; the caller's `if duration > 0` guard keeps the runtime."""
    assert _parse(_rpc(monkeypatch, 0), 6300, False) == 0


# =============================================================================
# getDuration threads `accurate` through
# =============================================================================

def test_getDuration_accurate_true_measures_and_uses_the_measurement(monkeypatch):
    rpc = _rpc(monkeypatch, 4763)
    assert rpc.getDuration(PATH, {'file': PATH, 'runtime': 6300}, True) == 4763


def test_getDuration_accurate_false_with_runtime_does_not_measure(monkeypatch):
    rpc = _rpc(monkeypatch, 4763)
    monkeypatch.setattr(rpc, 'videoParser', types.SimpleNamespace(getVideoLength=lambda *a: pytest.fail('measured')))
    assert rpc.getDuration(PATH, {'file': PATH, 'runtime': 6300}, False) == 6300


def test_getDuration_accurate_true_large_disagreement_is_not_overruled(monkeypatch):
    """imports.48: getDuration passes accurate to __parseDuration, so the 25%
    fallback is skipped in accurate mode."""
    rpc = _rpc(monkeypatch, 2663)
    assert rpc.getDuration(PATH, {'file': PATH, 'runtime': 3600}, True) == 2663
    assert rpc.getDuration(PATH, {'file': PATH, 'runtime': 3600}, False) == 3600
