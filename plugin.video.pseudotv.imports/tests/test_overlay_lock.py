"""Regression test for the toggleOverlay thread-safety fix (imports.18).

Background: two threads could enter toggleOverlay simultaneously, both see
self.overlay = None, both create an Overlay, both assign self.overlay. The
loser's Overlay python object was orphaned, but its channelBug control was
already added to xbmcgui.Window(12005) by open() — that control persisted
for the rest of the Kodi session, showing the wrong logo over subsequent
channels. The v.26 chid-aware idempotency at the same site doesn't help
here because it only fires when self.overlay is not None; the initial-create
race happens BEFORE any overlay exists.

imports.18 ships:
  1. services.py:Player._overlay_lock = Lock() (class attr)
  2. toggleOverlay body wrapped in `with self._overlay_lock:`
  3. overlay.py:Overlay.cntrlManager moved from class scope into __init__
     (defense in depth — was a class-level mutable default footgun that
     shared the control tracker across all Overlay instances).

This test source-scans both files to guard those invariants. Same convention
as test_chkcallback_cas.py and test_http_server_close.py — driving real
Player + Overlay from pytest is impractical (Kodi window context required).
"""
import os
import re


def _read(rel_path):
    here = os.path.dirname(os.path.abspath(__file__))
    return open(os.path.join(os.path.dirname(here), 'resources', 'lib', rel_path)).read()


# ---------------------------------------------------------------- services.py


def test_player_declares_overlay_lock():
    """Player class must declare _overlay_lock = Lock() — without it the race
    is back."""
    src = _read('services.py')
    class_start = src.find('class Player(xbmc.Player):')
    assert class_start != -1, "Player class not found"
    init_start = src.find('def __init__', class_start)
    class_body = src[class_start:init_start]

    assert re.search(r'^\s+_overlay_lock\s*=\s*Lock\s*\(\s*\)', class_body, re.MULTILINE), (
        "Player._overlay_lock = Lock() not declared at class scope — "
        "toggleOverlay race fix (imports.18) regressed"
    )


def test_toggleOverlay_uses_overlay_lock():
    """toggleOverlay body must be wrapped `with self._overlay_lock:` — that's
    the actual serialization point."""
    src = _read('services.py')
    fn_start = src.find('def toggleOverlay(self')
    assert fn_start != -1, "toggleOverlay not found in services.py"
    # Slice a generous window for the function body
    fn_body = src[fn_start:fn_start + 4000]
    # Look for `with self._overlay_lock:` before any of the toggleOverlay
    # critical statements (the overlay assignment / Overlay() construction)
    overlay_assign = fn_body.find('self.overlay = Overlay(player=self)')
    assert overlay_assign != -1, "toggleOverlay no longer creates Overlay — structural change unexpected"
    head = fn_body[:overlay_assign]
    assert 'with self._overlay_lock:' in head, (
        "toggleOverlay's critical section (self.overlay = Overlay(...)) is not "
        "inside a `with self._overlay_lock:` block — imports.18 fix regressed. "
        "Two threads can race the initial-create path and orphan an Overlay's "
        "channelBug control in xbmcgui.Window(12005)."
    )


# ---------------------------------------------------------------- overlay.py


def test_overlay_cntrlManager_not_at_class_scope():
    """Overlay must NOT have `cntrlManager = {}` at class scope — that's the
    Python mutable-default footgun. Defense in depth: even with the lock,
    any future direct Overlay() construction outside toggleOverlay should
    get a per-instance tracker."""
    src = _read('overlay.py')
    class_start = src.find('class Overlay(object):')
    assert class_start != -1, "Overlay class not found"
    init_start = src.find('def __init__', class_start)
    class_body = src[class_start:init_start]

    assert re.search(r'^\s+cntrlManager\s*=\s*\{\}', class_body, re.MULTILINE) is None, (
        "class-level `cntrlManager = {}` reintroduced on Overlay — "
        "Python class-level mutable defaults are a footgun"
    )


def test_overlay_init_assigns_cntrlManager():
    """The instance-level tracker must be set in __init__."""
    src = _read('overlay.py')
    class_start = src.find('class Overlay(object):')
    assert class_start != -1, "Overlay class not found"
    init_start = src.find('def __init__', class_start)
    # Next method def marks end of __init__
    next_def = src.find('\n    def ', init_start + 5)
    if next_def == -1: next_def = len(src)
    init_body = src[init_start:next_def]

    assert 'self.cntrlManager' in init_body, (
        "Overlay.__init__ missing `self.cntrlManager = {}` — instance-level "
        "control tracker not initialized; imports.18 hygiene regression"
    )
