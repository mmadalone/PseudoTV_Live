"""Unit tests for the imports.47 transition supervisor (watchdog + loop-breaker).

Mirrors the production logic from services.Player._chkTransition and the
supervisor block inside services.Player._onChange. services.py cannot be
imported directly in the test environment because overlay.py reads
xbmcgui.ControlList at module load time and that class isn't in the vendored
xbmcgui stub — same convention as test_chkcallback_cas.py / test_overlay_lock.py:
mirror the logic inline, faithful to production, AND add source-scan tests so
any drift between mirror and production is caught at the next pytest run.

Production:
    plugin.video.pseudotv.imports/resources/lib/services.py
        Player._chkTransition           (imports.47, new)
        Player._onChange (supervisor block, imports.47)
        Player.__init__ (_play_started, _short_plays — imports.47, new attrs)
        Player.onAVStarted (_play_started stamp — imports.47, new line)
        Monitor._chkIdle (not-playing branch calls _chkTransition — imports.47)
    plugin.video.pseudotv.imports/resources/lib/constants.py
        TRANSITION_LOOP_THRESHOLD = 3   (imports.47, new)
        TRANSITION_MAX_RETRIES    = 5   (imports.47, new)
    plugin.video.pseudotv.imports/resources/settings.xml
        Playback_Timeout default 90 → 45  (imports.47)

Plan: /home/madalone/.claude/plans/elegant-coalescing-tulip.md
"""
import os
import re
import types

import pytest


# Production constants (mirrored — drift caught by test_constants_has_*).
TRANSITION_LOOP_THRESHOLD = 3
TRANSITION_MAX_RETRIES    = 5


# ---------------------------------------------------------------- mirror helpers


def _chkTransition_mirror(self):
    """Mirror of services.Player._chkTransition (imports.47).

    Faithful to the production implementation; any change to production must
    also be reflected here (or the mirror retired once services.py becomes
    test-importable). Source-scan tests below guard the structural shape so
    drift becomes a test failure rather than silent.
    """
    inv = self.pendingItem.get('invoked', -1)
    if inv <= 0:
        if self.pendingItem.get('retune_attempts', 0) != 0:
            self.pendingItem['retune_attempts'] = 0
        return
    if self.isPlaying(): return
    if self._builtin.isBusyDialog(): return
    timeout = self._settings.getSettingInt('Playback_Timeout') or 45
    if (self._now() - inv) < timeout: return
    attempts = self.pendingItem.get('retune_attempts', 0)
    if attempts >= self._max_retries:
        self.log('_chkTransition, giving up after %s attempts' % attempts)
        self.pendingItem['invoked'] = -1
        self._dialog.notificationDialog(self._language(32000))
        return
    cb = self.pendingItem.get('retune_cb') or self.playingItem.get('callback')
    if not cb:
        self.log('_chkTransition, no retune_cb available')
        self.pendingItem['invoked'] = -1
        return
    self.pendingItem['retune_attempts'] = attempts + 1
    self.pendingItem['invoked']         = self._now()
    self.log('_chkTransition, recovery re-fire')
    self._builtin.executebuiltin('PlayMedia(%s)' % cb)


def _onChange_supervisor_mirror(self, playingItem, callback):
    """Mirror of the imports.47 supervisor block inside _onChange.

    Returns 'deferred' (loop-breaker engaged — no PlayMedia, only armed) or
    'fired' (normal — PlayMedia executed; arming gated on isPseudoTV).
    """
    # Short-play accounting (skips fillers and non-PseudoTV).
    if (playingItem.get('isPseudoTV')
            and not playingItem.get('isfiller', False)
            and self._play_started > 0):
        played = self._now() - self._play_started
        if played < self._settings.getSettingInt('Seek_Tolerance'):
            self._short_plays += 1
        else:
            self._short_plays = 0
    if (playingItem.get('isPseudoTV')
            and self._short_plays >= self._loop_threshold):
        # Loop-breaker: defer the re-tune; _chkTransition will fire it after
        # the cooldown.
        self.pendingItem.update({
            'invoked': self._now(),
            'retune_cb': callback,
            'retune_attempts': 0,
        })
        return 'deferred'
    self._builtin.executebuiltin('PlayMedia(%s)' % callback)
    if playingItem.get('isPseudoTV'):
        self.pendingItem.update({
            'invoked': self._now(),
            'retune_cb': callback,
            'retune_attempts': 0,
        })
    return 'fired'


def _make_stub(*, pendingItem=None, playingItem=None,
               isPlaying=False, isBusyDialog=False,
               playback_timeout=45, seek_tolerance=60,
               now=1_000_000.0, play_started=0, short_plays=0,
               loop_threshold=TRANSITION_LOOP_THRESHOLD,
               max_retries=TRANSITION_MAX_RETRIES):
    """Build a SimpleNamespace stub that resembles the production Player
    closely enough that the mirror functions can be driven against it."""
    executebuiltin_calls = []
    notification_calls   = []
    log_calls            = []

    builtin = types.SimpleNamespace(
        isBusyDialog=lambda: isBusyDialog,
        executebuiltin=lambda cmd: executebuiltin_calls.append(cmd),
    )
    settings = types.SimpleNamespace(getSettingInt=lambda key: {
        'Playback_Timeout': playback_timeout,
        'Seek_Tolerance':   seek_tolerance,
    }.get(key, 0))
    dialog = types.SimpleNamespace(
        notificationDialog=lambda msg: notification_calls.append(msg),
    )

    stub = types.SimpleNamespace(
        pendingItem=dict(pendingItem or {}),
        playingItem=dict(playingItem or {}),
        isPlaying=lambda: isPlaying,
        log=lambda msg, level=None: log_calls.append(msg),
        _builtin=builtin,
        _settings=settings,
        _dialog=dialog,
        _language=lambda i: 'msg-%s' % i,
        _max_retries=max_retries,
        _now=lambda: now,
        _play_started=play_started,
        _short_plays=short_plays,
        _loop_threshold=loop_threshold,
    )
    stub._executebuiltin_calls = executebuiltin_calls
    stub._notification_calls   = notification_calls
    stub._log_calls            = log_calls
    return stub


# =============================================================================
# _chkTransition behaviour
# =============================================================================


def test_chkTransition_no_op_when_not_armed():
    """invoked <= 0 means no transition pending → no-op (no executebuiltin)."""
    s = _make_stub(pendingItem={'invoked': -1})
    _chkTransition_mirror(s)
    assert s._executebuiltin_calls == []
    assert s._notification_calls == []


def test_chkTransition_clears_retune_attempts_on_disarm():
    """A stale retune_attempts left over from a previous arm must be cleared
    when invoked has been reset, so the next genuine arm starts from zero."""
    s = _make_stub(pendingItem={'invoked': -1, 'retune_attempts': 3})
    _chkTransition_mirror(s)
    assert s.pendingItem['retune_attempts'] == 0


def test_chkTransition_no_op_within_timeout():
    """Armed but not yet stale (elapsed < timeout) → no-op."""
    s = _make_stub(
        pendingItem={'invoked': 999_995.0, 'retune_cb': 'pvr://x'},
        playback_timeout=45,
        now=1_000_000.0,  # elapsed = 5s
    )
    _chkTransition_mirror(s)
    assert s._executebuiltin_calls == []


def test_chkTransition_fires_after_timeout():
    """Armed + timeout exceeded + not playing → re-fire PlayMedia, increment
    attempts, re-stamp invoked."""
    s = _make_stub(
        pendingItem={'invoked': 999_950.0, 'retune_cb': 'pvr://channel'},
        playback_timeout=45,
        now=1_000_000.0,  # elapsed = 50s > 45
    )
    _chkTransition_mirror(s)
    assert s._executebuiltin_calls == ['PlayMedia(pvr://channel)']
    assert s.pendingItem['retune_attempts'] == 1
    assert s.pendingItem['invoked'] == 1_000_000.0  # re-stamped


def test_chkTransition_no_op_when_playing():
    """isPlaying() True → no-op (the onXxx callback will reset invoked=-1)."""
    s = _make_stub(
        pendingItem={'invoked': 999_900.0, 'retune_cb': 'pvr://channel'},
        isPlaying=True,
        now=1_000_000.0,
    )
    _chkTransition_mirror(s)
    assert s._executebuiltin_calls == []


def test_chkTransition_no_op_when_busy_dialog():
    """A busy dialog is up → don't fight it."""
    s = _make_stub(
        pendingItem={'invoked': 999_900.0, 'retune_cb': 'pvr://channel'},
        isBusyDialog=True,
        now=1_000_000.0,
    )
    _chkTransition_mirror(s)
    assert s._executebuiltin_calls == []


def test_chkTransition_gives_up_at_cap():
    """attempts >= TRANSITION_MAX_RETRIES → disarm, notify, do not fire again."""
    s = _make_stub(
        pendingItem={
            'invoked': 999_900.0,
            'retune_cb': 'pvr://channel',
            'retune_attempts': TRANSITION_MAX_RETRIES,
        },
        now=1_000_000.0,
    )
    _chkTransition_mirror(s)
    assert s._executebuiltin_calls == []
    assert s.pendingItem['invoked'] == -1
    assert len(s._notification_calls) == 1


def test_chkTransition_disarms_when_no_callback_available():
    """invoked positive but no retune_cb AND no playingItem.callback → disarm
    (don't spin forever)."""
    s = _make_stub(
        pendingItem={'invoked': 999_900.0},
        playingItem={},  # no callback either
        now=1_000_000.0,
    )
    _chkTransition_mirror(s)
    assert s._executebuiltin_calls == []
    assert s.pendingItem['invoked'] == -1


def test_chkTransition_falls_back_to_playingItem_callback():
    """If pendingItem has no retune_cb but playingItem has a callback, use it."""
    s = _make_stub(
        pendingItem={'invoked': 999_900.0},
        playingItem={'callback': 'pvr://from-playing'},
        now=1_000_000.0,
    )
    _chkTransition_mirror(s)
    assert s._executebuiltin_calls == ['PlayMedia(pvr://from-playing)']


# =============================================================================
# _onChange supervisor block (short-play counting + loop-breaker)
# =============================================================================


def test_onChange_short_play_increments_counter():
    """PseudoTV non-filler that played < Seek_Tolerance → counter +1."""
    s = _make_stub(
        play_started=999_990.0,  # 10s ago
        short_plays=0,
        now=1_000_000.0,
        seek_tolerance=60,
    )
    res = _onChange_supervisor_mirror(s, {'isPseudoTV': True}, 'pvr://x')
    assert s._short_plays == 1
    assert res == 'fired'  # below threshold, still fires


def test_onChange_normal_play_resets_counter():
    """PseudoTV non-filler that played >= Seek_Tolerance → counter resets to 0."""
    s = _make_stub(
        play_started=999_870.0,  # 130s ago
        short_plays=2,
        now=1_000_000.0,
        seek_tolerance=60,
    )
    _onChange_supervisor_mirror(s, {'isPseudoTV': True}, 'pvr://x')
    assert s._short_plays == 0


def test_onChange_filler_excluded_from_short_play_count():
    """Fillers are legitimately short — must NOT count or reset the counter."""
    s = _make_stub(
        play_started=999_995.0,  # 5s ago (would be short)
        short_plays=1,
        now=1_000_000.0,
    )
    _onChange_supervisor_mirror(
        s, {'isPseudoTV': True, 'isfiller': True}, 'pvr://x',
    )
    assert s._short_plays == 1  # untouched


def test_onChange_non_pseudotv_excluded_from_short_play_count():
    """Non-PseudoTV playback must not engage the supervisor at all."""
    s = _make_stub(
        play_started=999_995.0,
        short_plays=2,
        now=1_000_000.0,
    )
    _onChange_supervisor_mirror(s, {'isPseudoTV': False}, 'pvr://x')
    assert s._short_plays == 2  # untouched


def test_onChange_runaway_defers_re_tune():
    """At TRANSITION_LOOP_THRESHOLD consecutive short plays, loop-breaker
    engages: arm invoked, but do NOT fire PlayMedia."""
    # short_plays starts at THRESHOLD-1 = 2; this short play makes it 3 → defer.
    s = _make_stub(
        play_started=999_995.0,  # 5s ago, short
        short_plays=TRANSITION_LOOP_THRESHOLD - 1,
        now=1_000_000.0,
        seek_tolerance=60,
    )
    res = _onChange_supervisor_mirror(s, {'isPseudoTV': True}, 'pvr://chan')
    assert res == 'deferred'
    assert s._executebuiltin_calls == []  # NO immediate fire
    assert s.pendingItem['invoked'] == 1_000_000.0  # armed for _chkTransition
    assert s.pendingItem['retune_cb'] == 'pvr://chan'
    assert s._short_plays == TRANSITION_LOOP_THRESHOLD


def test_onChange_after_normal_play_fires_immediately():
    """Once a programme plays a normal length, counter resets — next short
    transition is a fresh count, must fire (no defer)."""
    s = _make_stub(
        play_started=999_870.0,  # 130s ago, normal
        short_plays=TRANSITION_LOOP_THRESHOLD,  # was in runaway
        now=1_000_000.0,
        seek_tolerance=60,
    )
    res = _onChange_supervisor_mirror(s, {'isPseudoTV': True}, 'pvr://chan')
    assert res == 'fired'
    assert s._short_plays == 0


def test_onChange_non_pseudotv_does_not_arm_watchdog():
    """Non-PseudoTV transitions stay byte-identical to pre-imports.47: fire
    PlayMedia, no arming of pendingItem.invoked (no _chkTransition involvement)."""
    s = _make_stub()
    res = _onChange_supervisor_mirror(s, {'isPseudoTV': False}, 'plugin://x')
    assert res == 'fired'
    assert s._executebuiltin_calls == ['PlayMedia(plugin://x)']
    assert 'invoked' not in s.pendingItem  # NOT armed


def test_onChange_pseudotv_normal_fire_arms_watchdog():
    """A normal PseudoTV transition (not a runaway) arms the watchdog so a
    stalled PlayMedia can be recovered by _chkTransition."""
    s = _make_stub(
        play_started=999_870.0,  # normal length
        short_plays=0,
        now=1_000_000.0,
    )
    res = _onChange_supervisor_mirror(s, {'isPseudoTV': True}, 'pvr://chan')
    assert res == 'fired'
    assert s.pendingItem['invoked'] == 1_000_000.0
    assert s.pendingItem['retune_cb'] == 'pvr://chan'
    assert s.pendingItem['retune_attempts'] == 0


# =============================================================================
# Source-scan assertions — drift between mirror and production becomes a
# failing test, not silent breakage.
# =============================================================================


def _read(rel_path):
    """Read a file under the addon root, relative to tests/."""
    here = os.path.dirname(os.path.abspath(__file__))
    return open(os.path.join(os.path.dirname(here), rel_path)).read()


def _services_src():
    return _read(os.path.join('resources', 'lib', 'services.py'))


def _constants_src():
    return _read(os.path.join('resources', 'lib', 'constants.py'))


def _settings_xml():
    return _read(os.path.join('resources', 'settings.xml'))


def test_chkPlayback_removed_from_services():
    """imports.47 removed the dead __chkPlayback. Both the nested def and the
    call must be gone."""
    src = _services_src()
    assert 'def __chkPlayback' not in src, (
        "dead __chkPlayback nested def reintroduced; _chkTransition supersedes it"
    )
    assert '__chkPlayback()' not in src, (
        "__chkPlayback() call reintroduced inside _onIdle; remove it"
    )


def test_chkTransition_method_exists_on_Player():
    """The new supervisor method must exist as a Player method."""
    src = _services_src()
    assert re.search(r'^\s+def _chkTransition\(self\):', src, re.MULTILINE), (
        "Player._chkTransition not found in services.py"
    )


def test_chkIdle_calls_chkTransition_in_not_playing_branch():
    """Monitor._chkIdle's not-playing branch must call _chkTransition every tick."""
    src = _services_src()
    # _chkIdle is in the Monitor class.
    chkidle_start = src.find('def _chkIdle(self):')
    assert chkidle_start != -1, "_chkIdle not found"
    # Take a generous slice to cover the body.
    chkidle_body = src[chkidle_start:chkidle_start + 1500]
    assert 'self.player._chkTransition()' in chkidle_body, (
        "Monitor._chkIdle does not call self.player._chkTransition() — "
        "the watchdog/loop-breaker won't run during the between-programmes gap"
    )


def test_onAVStarted_records_play_started():
    """onAVStarted must stamp self._play_started for the loop-breaker to read."""
    src = _services_src()
    on_av = src.find('def onAVStarted(self):')
    assert on_av != -1, "onAVStarted not found"
    # Body extends to next def (rough bound; OK for a substring search).
    body = src[on_av:on_av + 2500]
    assert 'self._play_started = time.time()' in body, (
        "onAVStarted is missing `self._play_started = time.time()` — "
        "loop-breaker cannot measure how long a programme played"
    )


def test_player_init_has_play_started_and_short_plays_as_instance_attrs():
    """imports.47 added _play_started and _short_plays; must be instance attrs
    inside __init__, not class-level mutables (matches imports.16 hygiene)."""
    src = _services_src()
    # Locate Player class body up to __init__'s end.
    class_start = src.find('class Player(xbmc.Player):')
    assert class_start != -1, "Player class not found"
    init_start = src.find('def __init__', class_start)
    assert init_start != -1, "Player.__init__ not found"
    # Class body (above __init__) must NOT have these as class-level mutables.
    class_body = src[class_start:init_start]
    assert re.search(r'^\s+_play_started\s*=', class_body, re.MULTILINE) is None, (
        "class-level _play_started default — Python mutable footgun"
    )
    assert re.search(r'^\s+_short_plays\s*=', class_body, re.MULTILINE) is None, (
        "class-level _short_plays default — Python mutable footgun"
    )
    # __init__ must initialize them.
    init_body = src[init_start:init_start + 1500]
    assert 'self._play_started' in init_body, (
        "__init__ missing self._play_started initialization"
    )
    assert 'self._short_plays' in init_body, (
        "__init__ missing self._short_plays initialization"
    )


def test_chkTransition_uses_existing_Playback_Timeout_setting():
    """imports.47 reuses the existing Playback_Timeout setting (no new setting)."""
    src = _services_src()
    chk_start = src.find('def _chkTransition(self):')
    assert chk_start != -1
    body = src[chk_start:chk_start + 3000]
    assert "getSettingInt('Playback_Timeout')" in body, (
        "_chkTransition should read SETTINGS.getSettingInt('Playback_Timeout') — "
        "the existing setting whose purpose this completes"
    )


def test_onChange_supervisor_uses_Seek_Tolerance_and_threshold():
    """_onChange's short-play classifier reuses Seek_Tolerance; runaway
    detection uses TRANSITION_LOOP_THRESHOLD."""
    src = _services_src()
    on_change = src.find('def _onChange(self, playingItem')
    assert on_change != -1
    body = src[on_change:on_change + 4000]
    assert "getSettingInt('Seek_Tolerance')" in body, (
        "_onChange supervisor block should read Seek_Tolerance for short-play threshold"
    )
    assert 'TRANSITION_LOOP_THRESHOLD' in body, (
        "_onChange supervisor block should reference TRANSITION_LOOP_THRESHOLD"
    )


def test_constants_has_transition_supervisor_constants():
    """imports.47 added two tuning constants to constants.py."""
    src = _constants_src()
    assert re.search(r'^TRANSITION_LOOP_THRESHOLD\s*=\s*3\b', src, re.MULTILINE), (
        "TRANSITION_LOOP_THRESHOLD = 3 missing from constants.py"
    )
    assert re.search(r'^TRANSITION_MAX_RETRIES\s*=\s*5\b', src, re.MULTILINE), (
        "TRANSITION_MAX_RETRIES = 5 missing from constants.py"
    )


def test_settings_playback_timeout_default_45():
    """imports.47 changed Playback_Timeout's schema default 90 → 45."""
    src = _settings_xml()
    # Locate the Playback_Timeout block specifically.
    pt_idx = src.find('id="Playback_Timeout"')
    assert pt_idx != -1, "Playback_Timeout setting not found in settings.xml"
    block = src[pt_idx:pt_idx + 600]
    assert '<default>45</default>' in block, (
        "Playback_Timeout default should be 45 (was 90 pre-imports.47)"
    )
