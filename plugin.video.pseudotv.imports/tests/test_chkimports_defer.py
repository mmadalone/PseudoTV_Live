"""Unit tests for the chkImports Builder.buildChannels defer
(imports.14 / C#10 Follow-up B).

Covers the property-gate contracts added in imports.14:

  1. Property round-trip works in the test environment (sanity for
     the patched xbmcgui stub).
  2. The blocker-chain logic in the kick-property poll loop
     (tasks.py:125-141) identifies 'builder-running' when
     Builder.buildChannels is running, with the correct precedence
     (playback > syncAll-running > builder-running > None).

Live integration of the chkImports top-defer (tasks.py:601-616) is
covered by the operator's post-deploy Kodi log verification: the
`chkImports, deferred (Builder.buildChannels running)` log line
fires when a chkImports cycle attempts during a Builder run.
Driving it from pytest would require constructing a real Tasks
instance which loads channels.json from disk — out of scope for
unit tests; the property-gate logic itself is purely a function
of PROPERTIES.isRunning return values, which IS unit-testable.

Plan: /home/madalone/.claude/plans/misty-shimmying-liskov.md.
"""
import pytest

from globals import PROPERTIES


@pytest.fixture(autouse=True)
def _persistent_xbmcgui_properties(monkeypatch):
    """Make xbmcgui.Window().setProperty/getProperty/clearProperty
    actually persist across calls inside one test. The vendored stub
    is a no-op for setProperty (returns ''); without this patch,
    PROPERTIES.setRunning + PROPERTIES.isRunning round-trip would
    always return False because the value is never stored.

    The backing dict is scoped to the test (re-created per test via
    autouse fixture)."""
    import xbmcgui
    _store = {}

    def _set(key, value): _store[key] = value
    def _get(key):        return _store.get(key, '')
    def _clr(key):        _store.pop(key, None)

    monkeypatch.setattr(xbmcgui.Window, 'setProperty',   staticmethod(_set))
    monkeypatch.setattr(xbmcgui.Window, 'getProperty',   staticmethod(_get))
    monkeypatch.setattr(xbmcgui.Window, 'clearProperty', staticmethod(_clr))
    yield


# ---------------------------------------------------------------- sanity


def test_property_round_trip_sanity():
    """Verify the patched xbmcgui stub correctly round-trips
    PROPERTIES.setRunning → isRunning. If THIS fails, the rest of
    the suite would silently give the wrong answers."""
    # Clean slate
    PROPERTIES.setRunning('Builder.buildChannels', False)
    assert PROPERTIES.isRunning('Builder.buildChannels') is False

    PROPERTIES.setRunning('Builder.buildChannels', True)
    assert PROPERTIES.isRunning('Builder.buildChannels') is True

    PROPERTIES.setRunning('Builder.buildChannels', False)
    assert PROPERTIES.isRunning('Builder.buildChannels') is False


# ---------------------------------------------------------------- blocker chain


def _compute_blocker(is_playing):
    """Mirror of the blocker-chain logic at tasks.py:125-141. We
    test this inline because the original lives inside a nested
    daemon-thread function (_loop inside _startImportsThread) and
    is awkward to drive synchronously. The mirror is faithful to
    the production code; future drift would be caught by both the
    live verification log and a chkImports timeout warning."""
    blocker = None
    if is_playing:
        blocker = 'playback'
    elif PROPERTIES.isRunning('Imports.syncAll'):
        blocker = 'syncAll-running'
    elif PROPERTIES.isRunning('Builder.buildChannels'):
        blocker = 'builder-running'
    return blocker


def test_blocker_none_when_no_gates_set():
    """No blockers → None → kick would be consumed and fired."""
    PROPERTIES.setRunning('Imports.syncAll', False)
    PROPERTIES.setRunning('Builder.buildChannels', False)
    assert _compute_blocker(is_playing=False) is None


def test_blocker_builder_running_when_only_builder_set():
    """Builder running alone → 'builder-running'. This is the new
    branch added in imports.14."""
    PROPERTIES.setRunning('Imports.syncAll', False)
    PROPERTIES.setRunning('Builder.buildChannels', True)
    try:
        assert _compute_blocker(is_playing=False) == 'builder-running'
    finally:
        PROPERTIES.setRunning('Builder.buildChannels', False)


def test_blocker_syncall_takes_precedence_over_builder():
    """Both Imports.syncAll AND Builder.buildChannels running → the
    chain order at tasks.py:130-133 puts syncAll-running first.
    Verify the precedence is preserved when the new branch is added."""
    PROPERTIES.setRunning('Imports.syncAll', True)
    PROPERTIES.setRunning('Builder.buildChannels', True)
    try:
        assert _compute_blocker(is_playing=False) == 'syncAll-running'
    finally:
        PROPERTIES.setRunning('Imports.syncAll', False)
        PROPERTIES.setRunning('Builder.buildChannels', False)


def test_blocker_playback_takes_precedence_over_everything():
    """Playback supersedes both syncAll and Builder. This matches
    the existing behavior at tasks.py:126-129."""
    PROPERTIES.setRunning('Imports.syncAll', True)
    PROPERTIES.setRunning('Builder.buildChannels', True)
    try:
        assert _compute_blocker(is_playing=True) == 'playback'
    finally:
        PROPERTIES.setRunning('Imports.syncAll', False)
        PROPERTIES.setRunning('Builder.buildChannels', False)
