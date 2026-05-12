"""Unit tests for the _chkCallback CAS gate (imports.16).

Mirrors the lock-free CAS pattern used in services.Player._chkCallback to
make it driveable from pytest. The full services.py module cannot be imported
in the test environment because overlay.py reads xbmcgui.ControlList at
module load time and that class isn't in the vendored xbmcgui stub. Same
convention as test_chkimports_defer.py:_compute_blocker — mirror the logic
inline, faithful to production.

Production: plugin.video.pseudotv.imports/resources/lib/services.py
            Player._chkCallback (imports.16, replaces nested __chkCallback
            closure that wrote unconditionally)

Race the CAS gate protects against:
  - _onPlay worker thread replaces self.playingItem (pointer assignment,
    GIL-atomic) between __chkCallback's read of self.playingItem.get('callback')
    and its write to self.playingItem['callback']. Without the CAS gate, the
    callback resolved against the OLD dict gets written into the NEW dict —
    contaminating fresh state with a stale-context resolution.

Plan: /home/madalone/.claude/plans/jolly-greeting-globe.md.
"""
import os
import re
import types

import pytest


@pytest.fixture(autouse=True)
def _persistent_xbmcgui_properties(monkeypatch):
    """Same pattern as test_chkimports_defer.py — make Window.setProperty
    actually persist for the duration of the test. Mirror doesn't call
    PROPERTIES.setProperty (commented out as a side-effect we don't need
    to assert on), but the fixture is harmless and matches house style."""
    import xbmcgui
    _store = {}

    def _set(key, value): _store[key] = value
    def _get(key):        return _store.get(key, '')
    def _clr(key):        _store.pop(key, None)

    monkeypatch.setattr(xbmcgui.Window, 'setProperty',   staticmethod(_set))
    monkeypatch.setattr(xbmcgui.Window, 'getProperty',   staticmethod(_get))
    monkeypatch.setattr(xbmcgui.Window, 'clearProperty', staticmethod(_clr))
    yield


def _chkCallback_mirror(self):
    """Mirror of services.Player._chkCallback (imports.16).

    Faithful to the production implementation. Any change to production
    must also be reflected here (or the mirror retired in favor of driving
    the real method, once services.py becomes test-importable). Drift would
    be caught by code review + the live-verification log."""
    state = self.playingItem
    if state.get('callback'): return
    callback = self.jsonRPC.getCallback(state)
    if self.playingItem is state:
        state['callback'] = callback
        self.log('_chkCallback, callback = %s'%(callback))
        # PROPERTIES.setProperty + FileAccess._encodeString are side-effects
        # on Kodi window-properties; omitted from mirror since they're not
        # what the CAS gate's correctness hinges on.
    else:
        self.log('_chkCallback, state swapped under us; dropping write (next tick will retry)')


def _make_stub(playingItem, get_callback_returns='resolved-callback'):
    call_count = {'n': 0}

    def _getCallback(item):
        call_count['n'] += 1
        return get_callback_returns

    stub = types.SimpleNamespace(
        playingItem=playingItem,
        log=lambda msg, level=None: None,
        jsonRPC=types.SimpleNamespace(getCallback=_getCallback),
    )
    stub._call_count = call_count
    return stub


# ---------------------------------------------------------------- CAS behaviour


def test_chkCallback_writes_when_state_stable():
    """Stable state path: snapshot taken, no swap, write goes through.
    Round-trip sanity for the non-race case."""
    state = {'callback': None, 'chid': 'A'}
    stub = _make_stub(state, get_callback_returns='resolved-A')

    _chkCallback_mirror(stub)

    assert state['callback'] == 'resolved-A'
    assert stub.playingItem is state
    assert stub._call_count['n'] == 1


def test_chkCallback_skips_write_when_state_swapped():
    """Race C: _onPlay worker swaps self.playingItem between snapshot and
    write. The stale-context callback resolution must NOT be written into
    the new dict — this is the bug the CAS gate fixes."""
    state_old = {'callback': None, 'chid': 'A'}
    state_new = {'callback': 'fresh-callback', 'chid': 'B'}

    stub = _make_stub(state_old, get_callback_returns='resolved-A-stale')

    # Override getCallback to swap stub.playingItem before returning. This
    # simulates an _onPlay worker thread landing inside _chkCallback's
    # critical window between the snapshot and the write.
    def _getCallback_with_swap(item):
        stub._call_count['n'] += 1
        stub.playingItem = state_new
        return 'resolved-A-stale'
    stub.jsonRPC.getCallback = _getCallback_with_swap

    _chkCallback_mirror(stub)

    assert state_old['callback'] is None, (
        "snapshot dict received a write despite state swap — "
        "CAS gate (`if self.playingItem is state`) failed to suppress publication"
    )
    assert state_new['callback'] == 'fresh-callback', (
        "freshly-tuned playingItem was contaminated by the stale callback "
        "resolution — the exact bug the CAS gate is designed to prevent"
    )
    assert stub.playingItem is state_new


def test_chkCallback_noop_when_callback_already_present():
    """Fast path: callback already set, getCallback must not fire.
    Verifies the early-return guard still works after the refactor."""
    state = {'callback': 'existing', 'chid': 'A'}
    stub = _make_stub(state, get_callback_returns='should-not-be-set')

    _chkCallback_mirror(stub)

    assert state['callback'] == 'existing'
    assert stub._call_count['n'] == 0, (
        "getCallback was called despite a pre-existing callback — early-return guard broken"
    )


# ---------------------------------------------------------------- class-attr hygiene


def test_class_level_mutables_removed_from_Player():
    """imports.16 hygiene: pendingItem / playingItem moved from class-level
    mutable defaults to __init__ instance attrs. Regressing this brings back
    the Python footgun where multiple Player instances would share state."""
    here = os.path.dirname(os.path.abspath(__file__))
    services_path = os.path.join(os.path.dirname(here), 'resources', 'lib', 'services.py')
    src = open(services_path).read()

    # Locate Player class body (start) and its __init__ (boundary)
    class_start = src.find('class Player(xbmc.Player):')
    assert class_start != -1, "Player class not found in services.py"
    init_start = src.find('def __init__', class_start)
    assert init_start != -1, "Player.__init__ not found"
    class_body = src[class_start:init_start]

    # Class scope must NOT have these as mutable defaults
    assert re.search(r'^\s+pendingItem\s*=\s*\{\}', class_body, re.MULTILINE) is None, (
        "class-level `pendingItem = {}` default reintroduced — "
        "Python class-level mutable footgun"
    )
    assert re.search(r'^\s+playingItem\s*=\s*\{\}', class_body, re.MULTILINE) is None, (
        "class-level `playingItem = {}` default reintroduced — "
        "Python class-level mutable footgun"
    )

    # __init__ must initialize them as instance attrs
    init_body = src[init_start:init_start + 1500]
    assert 'self.pendingItem' in init_body, (
        "__init__ missing self.pendingItem initialization — imports.16 regression"
    )
    assert 'self.playingItem' in init_body, (
        "__init__ missing self.playingItem initialization — imports.16 regression"
    )
