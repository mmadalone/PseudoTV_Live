# -*- coding: utf-8 -*-
"""imports.71: settings reads that survive Kodi's settings reload.

The add-on's background service ended three times (2026-06-05, 2026-10-04 for 3 days 17 h,
2026-11-07) with one traceback: service.py -> Service._start -> _tasks -> _que -> CustomQueue._push
-> _run -> REAL_SETTINGS.getSettingBool('Enable_Executor') -> TypeError('Invalid setting type').
Every save of the add-on's settings makes Kodi rebuild the add-on's OLDEST live xbmcaddon.Addon in
place (REAL_SETTINGS, constants.py) on the saver's thread: while settings.xml is re-parsed its typed
getters raise and getSetting returns '' (phase A), then it answers the settings.xml defaults without an
error until the user file is read (phase B) (xbmc/addons/Addon.cpp:267-322, 375-406;
AddonManager.cpp:214-225). The service's queue made that typed read on every _run, about every 5 s.

The repair under test:
  - cqueue.CustomQueue keeps Enable_Executor in memory: read when the queue is made and after each
    settings save (Monitor._updateServiceSettings) through kodi.py's SETTINGS, which builds a new handle
    for each read (never the one Kodi rebuilds); until a read has succeeded the 5-minute refresh retries
    (_chkExecutor(retry=True)); a failed read keeps the last good value; _run reads nothing;
  - the four one-off typed reads below kodi.py (cache.py: Disable_Cache, Cache_MEM_Limit; pool.py:
    Enable_Executor, RPC_Wait) go through Globals._readSetting: REAL_SETTINGS first, a new handle only
    on TypeError; a wrong id or type still raises.

Kodi is kodi_faithful's FaithfulAddons (the repo-root xbmcaddon stub never raises and knows no reload:
test_the_repo_root_stub_would_hide_the_bug). The user's values below differ from the settings.xml
defaults, so a default read where the saved value belongs shows as a wrong value. Phase B still gives
the defaults to a read that stays on REAL_SETTINGS (Globals._readSetting's first try): a documented
limit, pinned here, and since imports.71 it happens only during a save someone makes.
"""
import ast
import collections
import contextlib
import math
import os
import threading
import types

import pytest

import kodi_faithful
from kodi_faithful import (GETTERS, KODI_LOG, SETTING_TYPES, TYPED_GETTER, FaithfulAddons, FaithfulMonitor,
                           compile_method, install_settings, lib_source)

HERE  = os.path.dirname(os.path.abspath(__file__))
ADDON = os.path.dirname(HERE)
LIB   = os.path.join(ADDON, 'resources', 'lib')

# The user's values: each differs from its settings.xml default (K8 pins the defaults).
# Debug_Enable stays at its default (off), as on the box; Debug_Level 0 is the box's value.
USER = {'Enable_Executor': 'false', 'Disable_Cache': 'true', 'Cache_MEM_Limit': '10', 'RPC_Wait': '120',
        'Max_Days': '1', 'Enable_Grouping': 'false', 'Debug_Level': '0'}
ERROR = KODI_LOG['LOGERROR']
KODI_EXCEPTION = 'EXCEPTION: Invalid setting type'


# === the rig =======================================================================================

@pytest.fixture
def env(monkeypatch):
    """The library under the stubs, with Kodi's settings: REAL_SETTINGS is the first handle built (the
    service's, constants.py), every xbmcaddon.Addon() call builds a new one, and `lines` is kodi.log:
    Kodi's own lines and the add-on's (xbmc.log, with Kodi 21's levels)."""
    import xbmc
    import xbmcaddon
    import cache
    import cqueue
    import globals as lib_globals
    import pool
    import variables
    lines = []
    for name, level in KODI_LOG.items(): monkeypatch.setattr(xbmc, name, level)
    monkeypatch.setattr(xbmc, 'log', lambda msg, level=0: lines.append((msg, level)))
    reg = FaithfulAddons(user=USER, kodi_log=lines)
    real = reg.Addon(kodi_faithful.ADDON_ID)
    real.getSetting('Debug_Enable')                          # loaded, as by the service's first log line
    install_settings(monkeypatch, reg, xbmcaddon, real)
    started = []

    class IdleThread(object):
        """cqueue's Thread: the pop thread is idle, as on the box between tasks, so every push reaches
        _run; a start is only noted (by its target's name)."""

        def __init__(self, target=None, *args, **kwargs):
            self.name, self.daemon = getattr(target, '__name__', ''), False

        def start(self):
            started.append(self.name)

        def is_alive(self):
            return False

        def join(self, timeout=None):
            return None
    monkeypatch.setattr(cqueue, 'Thread', IdleThread)
    monkeypatch.setattr(cqueue.CustomQueue, 'min_heap', [])
    monkeypatch.setattr(cqueue.CustomQueue, 'futures', set())
    return types.SimpleNamespace(reg=reg, real=real, lines=lines, started=started, cqueue=cqueue, cache=cache,
                                 pool=pool, variables=variables, settings=lib_globals.SETTINGS)


def hold(env, phase, handle=None):
    """REAL_SETTINGS (or `handle`) in reload phase 'A' or 'B' for the block; None: no reload."""
    return contextlib.nullcontext() if phase is None else env.reg.hold(phase, env.real if handle is None else handle)


def typed_calls(handle):
    return [call for call in handle.calls if call[0] in GETTERS + ('getSettings',)]


def not_logger(handle):
    """Its calls apart from logger.log's two reads (Debug_Enable / Debug_Level, every log line)."""
    return [call for call in handle.calls if call[1] not in ('Debug_Enable', 'Debug_Level')]


def kodi_errors(env):
    return [line for line, level in env.lines if line == KODI_EXCEPTION and level == ERROR]


def addon_errors(env):
    return [line for line, level in env.lines if level == ERROR and line != KODI_EXCEPTION
            and line.startswith('plugin.video.pseudotv.imports-')]


def quiet():
    """CustomQueue.service for the queue tests: nothing interrupts or suspends it."""
    return types.SimpleNamespace(_interrupt=lambda: False, _suspend=lambda: False)


def task():
    return None


# === K. the stand-in follows Kodi 21.3 ===============================================================

def test_the_settings_come_from_settings_xml():
    """K8. Types and defaults are the add-on's own (Kodi's parser skips the commented-out ones)."""
    kinds = collections.Counter(kind for kind, _default in SETTING_TYPES.values())
    assert len(SETTING_TYPES) == 115 and kinds == {'boolean': 43, 'integer': 35, 'string': 18, 'action': 13,
                                                   'list[addon]': 5, 'path': 1}
    assert SETTING_TYPES['Enable_Executor'] == ('boolean', 'true') and SETTING_TYPES['RPC_Wait'] == ('integer', '300')
    assert SETTING_TYPES['Cache_MEM_Limit'] == ('integer', '5')
    assert SETTING_TYPES['Disable_Cache'] == ('boolean', 'false')
    assert SETTING_TYPES['ZeroConf_Status'] == ('string', '[COLOR=red][B]Offline[/B][/COLOR]')
    assert 'Backup_Channels' not in SETTING_TYPES and 'Recover_Backup' not in SETTING_TYPES
    for key, value in USER.items():
        assert SETTING_TYPES[key][1] != value, key            # every user value here differs from its default


def test_each_addon_object_has_its_own_settings():
    """K1. AddonManager.cpp:630-655: each xbmcaddon.Addon() is a CAddon of its own. A save rebuilds the
    oldest live one only (AddonManager.cpp:214-225): another loaded earlier stays as it was."""
    reg = FaithfulAddons(user={'RPC_Wait': '120'})
    one, two = reg.Addon(), reg.Addon()
    assert one.getSettingInt('RPC_Wait') == two.getSettingInt('RPC_Wait') == 120
    reg.Addon().setSettingInt('RPC_Wait', 60)
    assert one.getSettingInt('RPC_Wait') == 60                # the oldest: rebuilt after the save
    assert two.getSettingInt('RPC_Wait') == 120               # loaded before the save, never rebuilt: stale
    assert reg.Addon().getSettingInt('RPC_Wait') == 60        # a new one reads the file


def test_a_save_writes_the_file_then_rebuilds_the_oldest_then_queues_the_callbacks():
    """K2. Addon.cpp:375-406: the file, then CAddonMgr::ReloadSettings, then
    XBPython::OnSettingsChanged. The callback is queued, not run: Kodi runs it inside a wait."""
    reg = FaithfulAddons()
    one = reg.Addon()
    one.getSetting('RPC_Wait')
    seen = []
    reg.on_reload = lambda phase: seen.append((phase, list(reg.events), reg.pending(), reg.disk.get('RPC_Wait')))
    reg.monitor(types.SimpleNamespace(onSettingsChanged=lambda: seen.append('onSettingsChanged')))
    reg.Addon().setSettingInt('RPC_Wait', 60)
    assert reg.events == ['disk', 'reload #1', 'callbacks queued']
    assert seen == [('A', ['disk', 'reload #1'], 0, '60'), ('B', ['disk', 'reload #1'], 0, '60')]
    assert reg.pending() == 1
    assert reg.deliver() == 1 and seen[-1] == 'onSettingsChanged' and reg.pending() == 0


@pytest.mark.parametrize('method, key', [('getSettingBool', 'Enable_Executor'), ('getSettingInt', 'RPC_Wait'),
                                         ('getSettingString', 'ZeroConf_Status'), ('getSettingString', 'User_Folder')])
def test_in_phase_A_typed_getters_raise_and_kodi_logs_it(method, key):
    """K3. Addon.cpp:286-308 (Clear .. Initialize): GetSettingValue -> HasSettings -> LoadSettings(false)
    is false, the binding throws WrongTypeException (legacy Addon.cpp:94-128) and the SWIG wrapper writes
    'EXCEPTION: Invalid setting type' at ERROR before Python sees the TypeError
    (PythonSwig.cpp.template:221-224). getSetting returns '' (Addon.cpp:408-418)."""
    reg = FaithfulAddons(user=USER)
    one = reg.Addon()
    expected = getattr(one, method)(key)
    with reg.hold('A'):
        with pytest.raises(TypeError, match='Invalid setting type'): getattr(one, method)(key)
        assert one.getSetting(key) == ''
    assert reg.kodi_log == [(KODI_EXCEPTION, ERROR)]
    assert getattr(one, method)(key) == expected              # the reload over: it answers again


def test_in_phase_B_the_settings_xml_defaults_come_back_silently():
    """K4. Addon.cpp:315-319: LoadSettings(false) is true again before the user file is read, so every
    getter answers the definitions' default, without an error."""
    reg = FaithfulAddons(user=USER)
    one = reg.Addon()
    assert (one.getSettingBool('Enable_Executor'), one.getSettingInt('RPC_Wait')) == (False, 120)
    with reg.hold('B'):
        assert (one.getSettingBool('Enable_Executor'), one.getSettingInt('RPC_Wait')) == (True, 300)
        assert one.getSetting('Max_Days') == '3'
    assert reg.kodi_log == []


def test_only_the_oldest_live_handle_is_rebuilt():
    """K5. Freed objects leave m_updateableAddons (~Addon, legacy Addon.cpp:74-77); the next oldest is
    then the one a later save rebuilds."""
    reg = FaithfulAddons(user={'RPC_Wait': '120'})
    one, two = reg.Addon(), reg.Addon()
    one.getSettingInt('RPC_Wait')
    two.getSettingInt('RPC_Wait')
    reg.Addon().setSettingInt('RPC_Wait', 60)
    assert (one.getSettingInt('RPC_Wait'), two.getSettingInt('RPC_Wait')) == (60, 120)
    del one
    assert reg.first() is two
    reg.Addon().setSettingInt('RPC_Wait', 30)
    assert two.getSettingInt('RPC_Wait') == 30 and reg.events[-2] == 'reload #2'


def test_undefined_ids_and_wrong_types_raise_like_kodi():
    """K6. Addon.cpp:420-435: a typed getter for an undefined id or of another type fails, with Kodi's
    line; getSetting gives '' (Addon.cpp:408-418); a typed setter of the wrong type raises and saves
    nothing (Addon.cpp:487-515); setSetting takes text (swig.cpp:105-141) and adds an undefined id
    (Addon.cpp:465-485)."""
    reg = FaithfulAddons(user=USER)
    one = reg.Addon()
    for method, key in (('getSettingInt', 'No_Such_Id'), ('getSettingInt', 'Enable_Executor'),
                        ('getSettingBool', 'RPC_Wait'), ('getSettingString', 'Enable_Executor')):
        with pytest.raises(TypeError, match='Invalid setting type'): getattr(one, method)(key)
    assert reg.kodi_errors() == [KODI_EXCEPTION] * 4
    assert one.getSetting('No_Such_Id') == ''
    with pytest.raises(TypeError): one.setSettingInt('Enable_Executor', 1)
    with pytest.raises(TypeError): one.setSetting('RPC_Wait', 60)
    assert reg.saves == [] and one.getSettingBool('Enable_Executor') is False
    one.setSetting('Backup_Channels', 'x')
    assert reg.saves == [(1, ['Backup_Channels'])] and reg.Addon().getSetting('Backup_Channels') == 'x'


def test_settings_load_on_first_use_and_a_dashboard_save_costs_three_parses(env):
    """K7. Building a handle parses nothing; its first settings access parses (Addon.cpp:267-322).
    kodi.Settings.setSetting builds two handles (the compare read, the writer: UpdateSetting loads it,
    Addon.cpp:465-470) and the save rebuilds the oldest: three parses."""
    reg = env.reg
    built, parses = reg.built, reg.parses
    one = reg.Addon()
    assert (reg.built, reg.parses) == (built + 1, parses)
    one.getSetting('RPC_Wait')
    one.getSetting('Max_Days')
    assert reg.parses == parses + 1
    env.settings.setSettingInt('RPC_Wait', 60)
    assert reg.parses == parses + 1 + 3 and reg.saves[-1][1] == ['RPC_Wait']
    env.settings.setSettingInt('RPC_Wait', 60)                # kodi.py compares first: no second save
    assert len(reg.saves) == 1


def test_callbacks_run_only_inside_a_wait_on_the_thread_that_made_the_monitor():
    """K11. python/CallbackHandler.cpp:33-45 (isStateOk: the creating thread) and legacy
    CallbackHandler.cpp:64-110 (run inside a wait; an exception is logged, never passed on)."""
    reg = FaithfulAddons()
    reg.Addon().getSetting('RPC_Wait')
    ran = []

    def boom():
        ran.append('main')
        raise ValueError('a callback failed')
    reg.monitor(types.SimpleNamespace(onSettingsChanged=boom))
    other = []
    thread = threading.Thread(target=lambda: other.append(reg.deliver()))
    reg.Addon().setSetting('ZeroConf_Status', 'x')
    assert ran == [] and reg.pending() == 1
    thread.start()
    thread.join(kodi_faithful.GATE_CAP_S)
    assert other == [0] and ran == []                         # another thread's wait runs nothing of ours
    assert reg.deliver() == 1 and ran == ['main']
    assert reg.kodi_log == [('EXCEPTION Thrown (PythonToCppException) : ValueError: a callback failed', ERROR)]


def test_a_torn_read_and_broken_definitions():
    """K12. A torn user file (a second save writing it): Kodi logs it, the handle serves the defaults
    silently and its saves are skipped (Addon.cpp:346-367, 377-378). settings.xml unreadable: the handle
    stays without settings for good (Addon.cpp:274-279, 295-312)."""
    reg = FaithfulAddons(user=USER)
    reg.torn_next_load = True
    torn = reg.Addon()
    assert torn.getSettingBool('Enable_Executor') is True                 # the default, no error
    assert [line for line, _ in reg.kodi_log] == [
        'CAddon[plugin.video.pseudotv.imports]: failed to load addon settings from <the user file>']
    torn.setSetting('RPC_Wait', '60')
    assert reg.saves == [] and reg.disk == USER
    reg.fail_definitions = True
    broken = reg.Addon()
    with pytest.raises(TypeError): broken.getSettingBool('Enable_Executor')
    assert broken.getSetting('Enable_Executor') == ''
    reg.fail_definitions = False
    with pytest.raises(TypeError): broken.getSettingBool('Enable_Executor')   # for good


def test_the_repo_root_stub_would_hide_the_bug():
    """K10. The shared stub (tests/xbmcaddon.py), side by side with the stand-in: it never raises, it
    reads 'false' as True, and its handles of this add-on share no writes."""
    import xbmcaddon
    stub_one, stub_two = xbmcaddon.Addon(kodi_faithful.ADDON_ID), xbmcaddon.Addon(kodi_faithful.ADDON_ID)
    assert stub_one.getSettingInt('No_Such_Id') == 0
    stub_one.setSetting('Enable_Executor', 'false')
    assert stub_one.getSettingBool('Enable_Executor') is True
    assert stub_two.getSetting('Enable_Executor') == ''
    reg = FaithfulAddons()
    one, two = reg.Addon(), reg.Addon()
    with pytest.raises(TypeError): one.getSettingInt('No_Such_Id')
    one.setSetting('Enable_Executor', 'false')
    assert one.getSettingBool('Enable_Executor') is False and two.getSettingBool('Enable_Executor') is False


# === R. the six typed reads ===========================================================================

@pytest.mark.parametrize('phase', [None, 'A', 'B'])
def test_the_queue_reads_the_users_value_when_it_is_made(env, phase):
    """R1 (cqueue.py:48 before). CustomQueue() reads Enable_Executor through SETTINGS: one new handle,
    never the one Kodi rebuilds. Before: a typed read of REAL_SETTINGS (raised in phase A, the default in
    phase B)."""
    built = env.reg.built
    with hold(env, phase):
        queue = env.cqueue.CustomQueue(priority=True, service=quiet())
    assert queue.useExecutor is False and queue.executorRead is True
    assert env.reg.built == built + 1
    assert typed_calls(env.real) == [] and kodi_errors(env) == []


@pytest.mark.parametrize('phase', ['A', 'B'])
def test_the_queue_run_reads_no_setting(env, phase):
    """R2 (cqueue.py:77 before: the crash line, about every 5 s on the box). 1,000 pushes while
    REAL_SETTINGS is being rebuilt: each reaches _run (the pop thread idle), none raises, none reads a
    setting of any kind (logger.log's own two reads aside), none builds a handle or parses."""
    queue = env.cqueue.CustomQueue(priority=True, service=quiet())
    built, parses = env.reg.built, env.reg.parses
    del env.real.calls[:]
    del env.started[:]
    with hold(env, phase):
        for _ in range(1000): queue._push((task, (), {}), -1)
    assert env.started == ['_start'] * 1000                    # _run 1,000 times
    assert queue.useExecutor is False
    assert (env.reg.built, env.reg.parses) == (built, parses)
    assert not_logger(env.real) == [] and kodi_errors(env) == []


def monitor_rig(env, queue):
    """Monitor's real settings chain (services.py, decorators dropped: no 15 s debounce) over a service
    whose _que runs a task at once."""
    service = types.SimpleNamespace(priorityQUE=queue, channels=[], settings={}, ran=[])

    def run_now(func, priority=-1, *args, **kwargs):
        service.ran.append(func.__name__)
        return func(*args, **kwargs)
    service._que = run_now
    service.tasks = types.SimpleNamespace(getChannels=lambda: ['the channel list'],
                                          chkSettingsChange=lambda settings: settings)
    player = types.SimpleNamespace(_refreshChannelRules=lambda: None)
    rig = types.SimpleNamespace(service=service, player=player, rollbackSwitch=True, logged=[])
    rig.log = lambda msg, level=0: rig.logged.append(msg)
    for name in ('onSettingsChanged', '_chkRollbackSwitch', '_onSettingsChanged', '_updateServiceSettings',
                 '_updatePlayerSettings'):
        setattr(rig, name, types.MethodType(compile_method('services.py', 'Monitor', name, SETTINGS=env.settings), rig))
    return env.reg.monitor(rig)


def kodi_wait(env):
    """A FaithfulMonitor whose waits let Kodi run the queued callbacks (on this thread)."""
    clock = [0.0]

    def advance(seconds):
        clock[0] += seconds
        env.reg.deliver()
    return FaithfulMonitor(advance, lambda: clock[0], lambda: None, [])


def test_a_saved_change_reaches_the_queue_at_the_next_refresh(env):
    """R3. The dashboard's save (kodi.Settings: a new handle writes; Kodi rebuilds REAL_SETTINGS, then
    queues onSettingsChanged). The queue keeps its value until Kodi runs the callback inside the service's
    next wait; then Monitor._updateServiceSettings re-reads it, first thing."""
    queue = env.cqueue.CustomQueue(priority=True, service=quiet())
    rig = monitor_rig(env, queue)
    env.settings.setSettingBool('Enable_Executor', True)
    assert env.reg.saves[-1][1] == ['Enable_Executor'] and env.reg.pending() == 1
    assert queue.useExecutor is False                         # nothing runs inside the save
    kodi_wait(env).waitForAbort(1.0)
    assert queue.useExecutor is True
    assert rig.service.ran == ['_updatePlayerSettings', '_updateServiceSettings']
    assert rig.service.channels == ['the channel list']


def test_a_failed_refresh_keeps_the_last_good_value(env):
    """R3b. The refresh read fails (no new handle can be made, so kodi.py falls back to REAL_SETTINGS, and
    that is mid-rebuild): Settings._getSetting writes it at ERROR naming the id and gives None, and the
    queue keeps the value it had (not the default, not None)."""
    queue = env.cqueue.CustomQueue(priority=True, service=quiet())
    rig = monitor_rig(env, queue)
    env.reg.refuse_new = True
    with hold(env, 'A'):
        rig._updateServiceSettings()
    assert queue.useExecutor is False and queue.executorRead is True
    assert len(kodi_errors(env)) == 1
    failed = [line for line in addon_errors(env) if '_getSetting, failed!' in line]
    assert len(failed) == 1 and 'key = Enable_Executor' in failed[0]


def test_a_failure_later_in_the_refresh_cannot_skip_the_queue(env):
    """_updateServiceSettings refreshes the queue first: the channel list failing to load after it (the
    queue's _start logs that and goes on) leaves Enable_Executor refreshed all the same."""
    queue = env.cqueue.CustomQueue(priority=True, service=quiet())
    rig = monitor_rig(env, queue)

    def broken():
        raise OSError('channels.json unreadable')
    rig.service.tasks.getChannels = broken
    env.reg.disk['Enable_Executor'] = 'true'
    with pytest.raises(OSError):
        rig._updateServiceSettings()
    assert queue.useExecutor is True


def test_until_a_read_succeeds_the_five_minute_refresh_retries(env):
    """The plan's retry: a queue made while no read works keeps Enable_Executor's settings.xml default
    (the class default) and remembers that no read has succeeded; the 5-minute refresh
    (_chkExecutor(retry=True)) reads until one does, then stops reading. A refresh after a save
    (_chkExecutor()) always reads."""
    env.reg.refuse_new = True
    with hold(env, 'A'):
        queue = env.cqueue.CustomQueue(priority=True, service=quiet())
    assert queue.useExecutor is True and queue.executorRead is False
    with hold(env, 'A'):
        assert queue._chkExecutor(retry=True) is True          # still failing: nothing learnt
    env.reg.refuse_new = False
    built = env.reg.built
    assert queue._chkExecutor(retry=True) is False and queue.executorRead is True
    assert env.reg.built == built + 1
    assert queue._chkExecutor(retry=True) is False and env.reg.built == built + 1     # known: no read
    env.reg.disk['Enable_Executor'] = 'true'
    assert queue._chkExecutor() is True and env.reg.built == built + 2               # after a save: read


@pytest.mark.parametrize('first_read_fails', [False, True])
def test_with_the_boxs_value_one_good_read_ends_the_retries(env, first_read_fails):
    """The review's case (fix stage): on the box Enable_Executor is at its settings.xml default, true,
    which is the class default too. A good read that leaves the value as it was still counts: after it, a
    day of 5-minute refreshes (288) reads nothing more. Counting a read only when the value changed would
    build a settings object and parse the settings every 5 minutes for ever there."""
    env.reg.disk['Enable_Executor'] = 'true'                  # the user file as on the box
    env.reg.refuse_new = first_read_fails
    with hold(env, 'A' if first_read_fails else None):
        queue = env.cqueue.CustomQueue(priority=True, service=quiet())
    assert queue.useExecutor is True and queue.executorRead is (not first_read_fails)
    env.reg.refuse_new = False
    built = env.reg.built
    for _ in range(288): assert queue._chkExecutor(retry=True) is True
    assert queue.executorRead is True and env.reg.built == built + (1 if first_read_fails else 0)


@pytest.mark.parametrize('phase, expected', [(None, True), ('A', True), ('B', False)])
def test_the_cache_switch_during_a_reload(env, phase, expected):
    """R4 (cache.py:76). Cache() through Globals._readSetting: phase A falls back to a new handle and
    reads the user's value; phase B gives the settings.xml default from REAL_SETTINGS (the documented
    limit). No exception either way. _Cache.__init__'s Cache_MEM_Limit goes the same way (R5)."""
    built = env.reg.built
    with hold(env, phase):
        made = env.cache.Cache()
    assert made.disable_cache is expected
    assert typed_calls(env.real) == [('getSettingInt', 'Cache_MEM_Limit'), ('getSettingBool', 'Disable_Cache')]
    assert env.reg.built == built + (2 if phase == 'A' else 0)
    assert len(kodi_errors(env)) == (2 if phase == 'A' else 0)


@pytest.mark.parametrize('phase, percent', [(None, 10), ('A', 10), ('B', 5)])
def test_the_cache_memory_limit_during_a_reload(env, phase, percent):
    """R5 (cache.py:165). Free memory is always the 1 GB fallback: cache.py never imports BUILTIN
    (pre-existing, left alone)."""
    with hold(env, phase):
        assert env.cache._Cache._getFreeMEM() == math.floor(1024 * percent / 100) * 1024 * 1024
    assert typed_calls(env.real) == [('getSettingInt', 'Cache_MEM_Limit')]


@pytest.mark.parametrize('phase, expected', [(None, False), ('A', False), ('B', True)])
def test_the_executor_pool_during_a_reload(env, phase, expected):
    """R6 (pool.py:30). ExecutorPool() reads exactly one typed Enable_Executor from REAL_SETTINGS (the
    first try) and nothing else there; in phase A one new handle answers. poolit still returns the
    list its callers extend."""
    built = env.reg.built
    with hold(env, phase):
        made = env.pool.ExecutorPool()
        assert env.pool.poolit(lambda item: item * 2)([1, 2, 3]) == [2, 4, 6]
    assert made.useExecutor is expected
    reads = [call for call in env.real.calls if call[1] == 'Enable_Executor']
    assert reads == [('getSettingBool', 'Enable_Executor')] * 2           # ExecutorPool() and poolit's own
    assert env.reg.built == built + (2 if phase == 'A' else 0)


@pytest.mark.parametrize('phase, wait', [(None, 120.0), ('A', 120.0), ('B', 300.0)])
def test_killit_during_a_reload(env, monkeypatch, phase, wait):
    """R7 (pool.py:124; killit has no callers today). wait=None reads RPC_Wait through
    Globals._readSetting."""
    joins = []

    class Joined(threading.Thread):
        def join(self, timeout=None):
            joins.append(timeout)
            return threading.Thread.join(self, timeout)
    monkeypatch.setattr(env.pool, 'Thread', Joined)

    def work():
        return 'done'
    with hold(env, phase):
        assert env.pool.killit(work)() == 'done'
    assert joins == [wait]
    assert [call for call in env.real.calls if call[1] == 'RPC_Wait'] == [('getSettingInt', 'RPC_Wait')]


@pytest.mark.parametrize('getter, key', [('getSettingBool', 'Enable_Executorr'), ('getSettingInt', 'Enable_Executor')])
def test_a_wrong_id_or_type_is_never_silent(env, getter, key):
    """R8. A programming mistake (a misspelt id, the wrong getter) is no reload: Globals._readSetting
    raises it, after two typed calls and Kodi's two lines (one per handle). Through SETTINGS (the queue's
    path) it is None with Kodi's line and the add-on's ERROR line naming the id, and the queue keeps its
    value. The library guard below checks every literal id against settings.xml before a release."""
    built = env.reg.built
    with pytest.raises(TypeError, match='Invalid setting type'):
        env.variables.Globals._readSetting(getter, key)
    assert typed_calls(env.real) == [(getter, key)] and env.reg.built == built + 1
    assert len(kodi_errors(env)) == 2
    del env.lines[:]
    assert getattr(env.settings, getter)(key) is None
    assert len(kodi_errors(env)) == 1
    failed = [line for line in addon_errors(env) if '_getSetting, failed!' in line]
    assert len(failed) == 1 and 'key = %s' % key in failed[0]


def test_only_a_TypeError_makes_the_helper_build_a_new_handle(env):
    """The reload's only signal is TypeError (WrongTypeException). Anything else, such as a getter name
    that doesn't exist, is a programming mistake: it raises at once and builds nothing."""
    built = env.reg.built
    with pytest.raises(AttributeError):
        env.variables.Globals._readSetting('getSettingBoool', 'Enable_Executor')
    assert env.reg.built == built and kodi_errors(env) == []


def test_the_helper_answers_from_REAL_SETTINGS_when_no_reload_runs(env):
    """Globals._readSetting outside a reload: REAL_SETTINGS answers (no new handle, no parse), with the
    user's value, for each typed getter it is used with."""
    built, parses = env.reg.built, env.reg.parses
    read = env.variables.Globals._readSetting
    assert (read('getSettingBool', 'Enable_Executor'), read('getSettingBool', 'Disable_Cache')) == (False, True)
    assert (read('getSettingInt', 'RPC_Wait'), read('getSettingInt', 'Cache_MEM_Limit')) == (120, 10)
    assert (env.reg.built, env.reg.parses) == (built, parses) and kodi_errors(env) == []


@pytest.mark.parametrize('phase', ['A', 'B'])
def test_untyped_reads_of_the_reload_target_never_raise(env, phase):
    """R9 (a characterization: the same before and after imports.71). The reads that stay on
    REAL_SETTINGS (the census below) read '' in phase A and the settings.xml defaults in phase B; none
    raises. What they then do is pinned, so a change has to be deliberate."""
    import globals as lib_globals
    import xbmc
    from fileaccess import FileAccess
    assert env.cache._Cache._cleanInterval(None).total_seconds() == 1 * 3600       # the user's Max_Days
    with hold(env, phase):
        made = env.cache._Cache(service=env.cache.Cache.service)
        grouped = env.variables.Globals._cleanGroups({'group': [], 'favorite': True})
        central = lib_globals.isCenterlized()
        env.variables.log('a debug line')
        env.variables.log('an error line', xbmc.LOGERROR)
    assert made._auto_clean_interval.total_seconds() == 3 * 3600   # Max_Days: '' -> 3, the default 3
    folder = '' if phase == 'A' else SETTING_TYPES['User_Folder'][1]
    assert made.dbfile == FileAccess.translatePath(os.path.join(folder, 'cache.db'))
    assert (len(grouped['group']), central) == ((1, True) if phase == 'A' else (2, False))
    written = [line for line, _level in env.lines if line.startswith('plugin.video.pseudotv.imports-')]
    assert [line for line in written if 'a debug line' in line] == []
    assert len([line for line in written if 'an error line' in line]) == 1
    untyped = [call for call in env.real.calls if call[0] == 'getSetting']
    assert {key for _m, key in untyped} == {'Debug_Enable', 'Debug_Level', 'User_Folder', 'Max_Days', 'Enable_Grouping'}


def test_reads_follow_a_save_when_another_handle_is_the_oldest(monkeypatch):
    """R10 (research F03b). A plugin interpreter's handle older than REAL_SETTINGS is the one a save
    rebuilds: REAL_SETTINGS then keeps the old value. The queue still follows the save at its refresh
    (a new handle), and its next push uses the new value without reading anything."""
    import xbmc
    import xbmcaddon
    import cqueue
    import globals as lib_globals
    lines = []
    for name, level in KODI_LOG.items(): monkeypatch.setattr(xbmc, name, level)
    monkeypatch.setattr(xbmc, 'log', lambda msg, level=0: lines.append((msg, level)))
    reg = FaithfulAddons(user=USER, kodi_log=lines)
    plugin = reg.Addon()
    plugin.getSetting('Debug_Enable')
    real = reg.Addon()
    real.getSetting('Debug_Enable')
    install_settings(monkeypatch, reg, xbmcaddon, real)
    started = []

    class IdleThread(object):
        def __init__(self, target=None, *args, **kwargs):
            self.name, self.daemon = getattr(target, '__name__', ''), False

        def start(self):
            started.append(self.name)

        def is_alive(self):
            return False
    monkeypatch.setattr(cqueue, 'Thread', IdleThread)
    monkeypatch.setattr(cqueue.CustomQueue, 'min_heap', [])
    env = types.SimpleNamespace(reg=reg, real=real, lines=lines, settings=lib_globals.SETTINGS)
    queue = cqueue.CustomQueue(priority=True, service=quiet())
    rig = monitor_rig(env, queue)
    lib_globals.SETTINGS.setSettingBool('Enable_Executor', True)
    assert reg.events[-2] == 'reload #1'                      # the plugin's handle, not REAL_SETTINGS
    assert real.getSettingBool('Enable_Executor') is False    # REAL_SETTINGS: stale
    kodi_wait(env).waitForAbort(1.0)
    assert queue.useExecutor is True
    del real.calls[:]
    del started[:]
    queue._push((task, (), {}), -1)
    assert started == ['_worker', '_start'] and not_logger(real) == []
    assert rig.service.ran == ['_updatePlayerSettings', '_updateServiceSettings']


def test_a_save_on_another_thread_while_the_main_thread_pushes(env):
    """R11 (real threads, a rendezvous, not a race). The dashboard saves on its own thread; Kodi's
    rebuild of REAL_SETTINGS holds in phase A (the saver has released Python's lock there) until the
    main thread's push has run. Both finish, nothing raises."""
    queue = env.cqueue.CustomQueue(priority=True, service=quiet())
    in_a, pushed, errors = threading.Event(), threading.Event(), []

    def at_reload(phase):
        if phase == 'A':
            in_a.set()
            if not pushed.wait(kodi_faithful.GATE_CAP_S): errors.append('the push never came')
    env.reg.on_reload = at_reload

    def dashboard():
        try: env.settings.setSettingInt('RPC_Wait', 60)
        except BaseException as e:  # a failure on this thread must reach the test
            errors.append(e)
    writer = threading.Thread(target=dashboard, name='dashboard save')
    writer.start()
    try:
        assert in_a.wait(kodi_faithful.GATE_CAP_S)
        assert env.real.phase == 'A'
        queue._push((task, (), {}), -1)
    finally:
        pushed.set()
        writer.join(kodi_faithful.GATE_CAP_S)
    assert not writer.is_alive() and errors == []
    assert env.real.getSettingInt('RPC_Wait') == 60 and kodi_errors(env) == []


# === THE PROOF: the 2026-11-07 chain ===================================================================

def chain_rig(env):
    """The service's pass as on 2026-11-07, from _tasks down: the REAL Service._tasks, _que, _interrupt,
    _suspend and _isPlaying (services.py) over the REAL CustomQueue (cqueue.py). Kodi's Window and the
    add-on's tasks are stand-ins; nothing interrupts the queue (as at 19:56 that evening)."""
    rig = types.SimpleNamespace(pendingShutdown=False, pendingRestart=False, pendingInterrupt=False,
                                pendingSuspend=False, isScanning=False, isPlaying=False, isPaused=False,
                                logged=[], queued=[])
    rig.player = types.SimpleNamespace(runWhilePlaying=False)
    rig.log = lambda msg, level=0: rig.logged.append(msg)

    def chkQueTimer():
        return None

    def epoch_timer(key, func, runevery=900, priority=-1):
        rig.queued.append(key)
    rig.tasks = types.SimpleNamespace(chkQueTimer=chkQueTimer, _chkEpochTimer=epoch_timer)
    window = types.SimpleNamespace(isInterruptActivity=lambda: False, isSuspendActivity=lambda: False,
                                   setPendingInterrupt=lambda state=True: state,
                                   setPendingSuspend=lambda state=True: state)
    g = dict(PROPERTIES=window, BUILTIN=types.SimpleNamespace(isSettingsOpened=lambda: False))
    for name in ('_tasks', '_que', '_interrupt', '_suspend', '_isPlaying'):
        setattr(rig, name, types.MethodType(compile_method('services.py', 'Service', name, **g), rig))
    rig.priorityQUE = env.cqueue.CustomQueue(priority=True, service=rig)
    return rig


def test_the_2026_11_07_chain_runs_through_a_reload(env):
    """THE PROOF. A settings save made Kodi rebuild REAL_SETTINGS while the main loop's pass pushed its
    chkQueTimer task: Service._tasks -> _que -> CustomQueue._push -> _run. On 399744fd _run's typed read
    raises TypeError('Invalid setting type') here (cqueue.py:77), which ended the service; now the pass
    queues its task and reads no setting. Recorded on the unfixed code in
    ~/.claude/plans/settings-crash/proof-run.txt."""
    rig = chain_rig(env)
    del env.real.calls[:]
    del env.started[:]
    with hold(env, 'A'):
        rig._tasks()
    assert [entry[2][0].__name__ for entry in rig.priorityQUE.min_heap] == ['epoch_timer']
    assert rig.priorityQUE.min_heap[0][2][1] == ('chkQueTimer', rig.tasks.chkQueTimer, 15)
    assert env.started == ['_start']                           # _run was reached and started the pop thread
    assert not_logger(env.real) == [] and kodi_errors(env) == []


def test_the_chain_survives_a_day_of_reloads(env):
    """The same pass, 17,280 times (a day at the 5 s interval), every one inside a reload, alternating
    phases A and B: never a settings read, never a new handle."""
    rig = chain_rig(env)
    built, parses = env.reg.built, env.reg.parses
    del env.real.calls[:]
    for n in range(17280):
        with hold(env, 'AB'[n % 2]):
            rig._tasks()
    assert (env.reg.built, env.reg.parses) == (built, parses)
    assert not_logger(env.real) == [] and kodi_errors(env) == []
    assert len(rig.priorityQUE.min_heap) == 1                 # the same package: queued once, as on the box


# === the code: where the reads are now ===================================================================

def _class(filename, name):
    for node in lib_source(filename)[1].body:
        if isinstance(node, ast.ClassDef) and node.name == name: return node
    raise AssertionError('%s: class %s not found' % (filename, name))


def _def(filename, cls, name):
    for node in (lib_source(filename)[1].body if cls is None else _class(filename, cls).body):
        if isinstance(node, ast.FunctionDef) and node.name == name: return node
    raise AssertionError('%s: %s.%s not found' % (filename, cls, name))


def _lines(node):
    return [ast.unparse(statement) for statement in node.body]


def test_the_queue_keeps_the_value_in_memory():
    """cqueue.py: the class default is Enable_Executor's settings.xml default; __init__ reads it once
    through _chkExecutor; _run reads nothing; _chkExecutor reads through SETTINGS only."""
    queue = _class('cqueue.py', 'CustomQueue')
    defaults = {ast.unparse(n.targets[0]): ast.unparse(n.value) for n in queue.body if isinstance(n, ast.Assign)}
    assert defaults['useExecutor'] == 'True' == SETTING_TYPES['Enable_Executor'][1].capitalize()
    assert defaults['executorRead'] == 'False'
    assert 'self._chkExecutor()' in _lines(_def('cqueue.py', 'CustomQueue', '__init__'))
    run = ast.unparse(_def('cqueue.py', 'CustomQueue', '_run'))
    assert 'SETTINGS' not in run and 'getSetting' not in run and '_chkExecutor' not in run
    check = _def('cqueue.py', 'CustomQueue', '_chkExecutor')
    reads = [ast.unparse(n) for n in ast.walk(check) if isinstance(n, ast.Call) and 'etSetting' in ast.unparse(n.func)]
    assert reads == ["SETTINGS.getSettingBool('Enable_Executor')"]
    assert _lines(check)[0] == 'if retry and self.executorRead:\n    return self.useExecutor'


def test_no_queue_code_but_chkExecutor_touches_the_settings():
    """The review's case (fix stage): every push reaches _run and every task goes through _exe (tens of
    thousands a day on the box), and the rigs that run tasks don't count settings objects. Anywhere in
    cqueue.py, a settings object, a settings call or xbmcaddon appears only in CustomQueue._chkExecutor:
    a read in _exe through SETTINGS would parse the settings for every task; through REAL_SETTINGS it
    would reopen the reload window."""
    src = _File(lib_source('cqueue.py')[0])
    found = []
    for node in ast.walk(src.tree):
        named = ((isinstance(node, ast.Name) and node.id in ('SETTINGS', 'REAL_SETTINGS', 'xbmcaddon'))
                 or (isinstance(node, ast.Attribute) and (node.attr in ('SETTINGS', 'REAL_SETTINGS', 'Addon')
                                                          or 'etSetting' in node.attr)))
        if named: found.append((src.around(node)[0], ast.unparse(node)))
    assert sorted(set(found)) == [('CustomQueue._chkExecutor', 'SETTINGS'),
                                  ('CustomQueue._chkExecutor', 'SETTINGS.getSettingBool')]


def test_a_settings_save_refreshes_the_queue_first():
    """services.py Monitor._updateServiceSettings: the queue's read is its first statement after the log
    line, so a failure further down can't skip it."""
    lines = _lines(_def('services.py', 'Monitor', '_updateServiceSettings'))
    assert lines[:3] == ["self.log('_updateServiceSettings')", 'self.service.priorityQUE._chkExecutor()',
                         'self.service.channels = self.service.tasks.getChannels()']


def test_the_helper_is_one_try_and_one_fallback():
    """variables.py Globals._readSetting: REAL_SETTINGS first; a new handle only on TypeError."""
    node = _def('variables.py', 'Globals', '_readSetting')
    assert [ast.unparse(d) for d in node.decorator_list] == ['staticmethod']
    assert _lines(node) == ['try:\n    return getattr(REAL_SETTINGS, getter)(key)\n'
                            'except TypeError:\n    return getattr(xbmcaddon.Addon(ADDON_ID), getter)(key)']


SITES = [('cache.py', 'Cache', '__init__', 'getSettingBool', 'Disable_Cache'),
         ('cache.py', '_Cache', '_getFreeMEM', 'getSettingInt', 'Cache_MEM_Limit'),
         ('pool.py', 'ExecutorPool', '__init__', 'getSettingBool', 'Enable_Executor'),
         ('pool.py', None, 'killit', 'getSettingInt', 'RPC_Wait')]


def helper_calls(text):
    """(getter, key) of every Globals._readSetting call in `text`."""
    return [tuple(arg.value for arg in n.args) for n in ast.walk(ast.parse(text)) if isinstance(n, ast.Call)
            and ast.unparse(n.func) == 'Globals._readSetting']


@pytest.mark.parametrize('filename, cls, name, getter, key', SITES)
def test_the_one_off_reads_go_through_the_helper(filename, cls, name, getter, key):
    assert helper_calls(ast.unparse(_def(filename, cls, name))) == [(getter, key)]


def test_the_helper_is_used_with_defined_ids_of_the_right_type():
    """Every Globals._readSetting call in the library, and the queue's read: a literal getter and a
    literal id that settings.xml defines with that getter's type."""
    found = []
    for name in sorted(os.listdir(LIB)):
        if name.endswith('.py'): found += [(name,) + call for call in helper_calls(lib_source(name)[0])]
    assert sorted(found) == sorted((filename, getter, key) for filename, _c, _n, getter, key in SITES)
    found.append(('cqueue.py', 'getSettingBool', 'Enable_Executor'))
    for filename, getter, key in found:
        assert TYPED_GETTER[SETTING_TYPES[key][0]] == getter, (filename, getter, key)


# === the library guard: REAL_SETTINGS is read only where reviewed =========================================
from test_imports70_shutdown_contract import _File, library_sources  # noqa: E402

TYPED = GETTERS + ('getSettings',)
INFO  = ('getAddonInfo', 'getLocalizedString')       # the add-on's info and strings: no setting involved


def _how(src, node):
    """What one use of REAL_SETTINGS (the name, or an attribute of that name: constants.REAL_SETTINGS)
    does: (how, detail)."""
    parent = src.parent.get(node)
    if isinstance(node.ctx, ast.Store): return 'assign', ''
    if isinstance(parent, ast.Attribute) and parent.value is node:
        call = src.parent.get(parent)
        if isinstance(call, ast.Call) and call.func is parent:
            first = call.args[0] if call.args else None
            if first is None: return 'call ' + parent.attr, ''
            return 'call ' + parent.attr, (first.value if isinstance(first, ast.Constant) else ast.unparse(first))
        return 'attribute ' + parent.attr, ''
    if isinstance(parent, ast.Call) and node in parent.args:
        rest = ', '.join(ast.unparse(a) for a in parent.args if a is not node)
        return 'argument of ' + ast.unparse(parent.func), rest
    if isinstance(parent, ast.Return): return 'return', ''
    return 'other ' + type(parent).__name__, ast.unparse(parent)[:80]


def real_settings_uses(sources):
    """Counter of (file, owner, how, detail) for every use of REAL_SETTINGS in `sources`, the add-on's
    info and strings left out; an import under another name counts as ('alias', name). A use through a
    module (constants.REAL_SETTINGS: the same object) counts like the bare name (the review's case, fix
    stage)."""
    found = collections.Counter()
    for name, text in sources.items():
        src = _File(text)
        for node in ast.walk(src.tree):
            if isinstance(node, ast.alias) and node.name == 'REAL_SETTINGS' and node.asname:
                found[(name, '<import>', 'alias', node.asname)] += 1
            elif ((isinstance(node, ast.Name) and node.id == 'REAL_SETTINGS')
                  or (isinstance(node, ast.Attribute) and node.attr == 'REAL_SETTINGS')):
                how, detail = _how(src, node)
                if how.split(' ')[-1] in INFO: continue
                found[(name, src.around(node)[0], how, detail)] += 1
    return found


STORES = ('append', 'add', 'insert', 'extend', 'setdefault', 'update', '__setitem__')


def long_lived_handles(sources):
    """(file, owner, target) of every xbmcaddon.Addon(...) kept beyond one call: assigned at module or
    class level, to an attribute (self.x = ...) or into a container, whether by an item assignment
    (d['h'] = ...), by a container's own method (STORES: l.append(...), d.setdefault(k, ...)) or inside a
    literal that is kept so (the review's case, fix stage: a module dict holding a fallback handle).
    Lexical: a handle handed to a function that keeps it is not followed."""
    found = []
    for name, text in sources.items():
        src = _File(text)
        for node in ast.walk(src.tree):
            if not (isinstance(node, ast.Call) and ast.unparse(node.func) in ('xbmcaddon.Addon', 'Addon')): continue
            held, parent = node, src.parent.get(node)
            while isinstance(parent, (ast.Dict, ast.List, ast.Tuple, ast.Set)):        # inside a literal
                held, parent = parent, src.parent.get(parent)
            owner, functions = src.around(node)
            if (isinstance(parent, ast.Call) and held in parent.args and isinstance(parent.func, ast.Attribute)
                    and parent.func.attr in STORES):
                found.append((name, owner, ast.unparse(parent.func)))
                continue
            if not isinstance(parent, (ast.Assign, ast.AnnAssign)): continue
            targets = parent.targets if isinstance(parent, ast.Assign) else [parent.target]
            for target in targets:
                if isinstance(target, (ast.Attribute, ast.Subscript)) or not functions:
                    found.append((name, owner, ast.unparse(target)))
    return sorted(found)


# Reviewed 2026-11-20 (imports.71): the reads that stay on REAL_SETTINGS. All untyped: they never raise;
# in phase A they read '' and in phase B the settings.xml defaults (R9 pins what each then does). Since
# imports.71 the add-on itself no longer saves its settings every few minutes, so these windows open
# only while someone saves (the settings dialog, the dashboard).
UNTYPED_REVIEWED = {
    ('logger.py', 'log', 'Debug_Enable'): "every log line, every interpreter: '' and the default 'false' -> debug off",
    ('logger.py', 'log', 'Debug_Level'): "'' -> 3 (ERROR lines only), as the default 3: ERROR lines still written",
    ('cache.py', '_Cache.__init__', 'User_Folder'): 'the cache.db path, at import (Settings.cacheDB/cache)',
    ('cache.py', '_Cache._cleanInterval', 'Max_Days'): "one cleanup check: '' and the default give 3 h instead of 1 h",
    ('variables.py', '<module>', 'Page_Limit'): 'import time only',
    ('variables.py', '<module>', 'Min_Days'): 'import time only',
    ('variables.py', '<module>', 'Max_Days'): 'import time only',
    ('variables.py', '<module>', 'OSD_Timer'): 'import time only',
    ('variables.py', '<module>', 'EPG_Artwork'): 'import time only',
    ('variables.py', '<module>', 'User_Folder'): 'import time only: USER_LOC and every data path',
    ('variables.py', 'Globals._cleanGroups', 'Enable_Grouping'):
        "once per channel in builds: '' skips grouping (the user value here); the default adds Favourites",
    ('globals.py', 'isCenterlized', 'User_Folder'): 'no callers',
}
OTHER_REVIEWED = {
    ('constants.py', '<module>', 'assign', ''): 'the one long-lived handle: the one Kodi rebuilds after a save',
    ('variables.py', 'Globals._readSetting', 'argument of getattr', 'getter'):
        'its first try; TypeError falls back to a new handle',
    ('kodi.py', 'Settings.getRealSettings', 'return', ''):
        ("the fallback when no new handle can be made (never for this add-on's own id): "
         'Settings._getSetting catches a failed read'),
}


def test_no_typed_read_of_REAL_SETTINGS_anywhere():
    """G1. No getSettingBool / Int / Number / String or getSettings on REAL_SETTINGS, however written:
    called, taken as a value, through getattr outside Globals._readSetting, or under another name. Every
    use is either an untyped read on the reviewed list (G3) or one of the reviewed others."""
    uses = real_settings_uses(library_sources())
    typed = {key: n for key, n in uses.items() if key[2].split(' ')[-1] in TYPED}
    assert typed == {}
    others = {key: n for key, n in uses.items() if key[2] != 'call getSetting'}
    assert others == {key: 1 for key in OTHER_REVIEWED}


def test_untyped_reads_of_REAL_SETTINGS_are_reviewed():
    """G3. The untyped reads that stay on the reload target: each on the list with what phase A / B
    means for it. A new one fails here until reviewed."""
    uses = real_settings_uses(library_sources())
    reads = {(f, owner, key): n for (f, owner, how, key), n in uses.items() if how == 'call getSetting'}
    assert reads == {key: 1 for key in UNTYPED_REVIEWED}
    assert all(UNTYPED_REVIEWED.values()) and all(OTHER_REVIEWED.values())


def test_the_only_long_lived_handle_is_REAL_SETTINGS():
    """G2. Kodi rebuilds the oldest live handle after a save; one kept beyond a call would be exposed
    the same way."""
    assert long_lived_handles(library_sources()) == [('constants.py', '<module>', 'REAL_SETTINGS')]


@pytest.mark.parametrize('snippet, found', [
    ("x = REAL_SETTINGS.getSettingBool('Enable_Executor')", ('<module>', 'call getSettingBool', 'Enable_Executor')),
    ('read = REAL_SETTINGS.getSettingInt', ('<module>', 'attribute getSettingInt', '')),
    ("v = REAL_SETTINGS.getSettings().getBool('Enable_Executor')", ('<module>', 'call getSettings', '')),
    ("rs = REAL_SETTINGS\nrs.getSettingBool('Enable_Executor')", ('<module>', 'other Assign', 'rs = REAL_SETTINGS')),
    ('from constants import REAL_SETTINGS as RS', ('<import>', 'alias', 'RS')),
    ("getattr(REAL_SETTINGS, 'getSettingBool')('Enable_Executor')",
     ('<module>', 'argument of getattr', "'getSettingBool'")),
    ('read(REAL_SETTINGS)', ('<module>', 'argument of read', '')),
    ('def f():\n    return REAL_SETTINGS', ('f', 'return', '')),
    ("REAL_SETTINGS.setSetting('ZeroConf_Status', 'x')", ('<module>', 'call setSetting', 'ZeroConf_Status')),
    ("class C:\n    def f(self):\n        return REAL_SETTINGS.getSetting('Max_Days')",
     ('C.f', 'call getSetting', 'Max_Days')),
    # the six reads of 399744fd
    ("self.useExecutor = REAL_SETTINGS.getSettingBool('Enable_Executor')",
     ('<module>', 'call getSettingBool', 'Enable_Executor')),
    ("self.disable_cache = (disable_cache or REAL_SETTINGS.getSettingBool('Disable_Cache'))",
     ('<module>', 'call getSettingBool', 'Disable_Cache')),
    ("x = floor(free * (REAL_SETTINGS.getSettingInt('Cache_MEM_Limit') / 100))",
     ('<module>', 'call getSettingInt', 'Cache_MEM_Limit')),
    ("if wait is None: wait = REAL_SETTINGS.getSettingInt('RPC_Wait')", ('<module>', 'call getSettingInt', 'RPC_Wait')),
    # the review's case (fix stage): the same object through its module
    ("def chkVersion(self):\n    if constants.REAL_SETTINGS.getSettingBool('Enable_Client'): pass",
     ('chkVersion', 'call getSettingBool', 'Enable_Client')),
    ('rs = constants.REAL_SETTINGS', ('<module>', 'other Assign', 'rs = constants.REAL_SETTINGS')),
])
def test_the_scan_sees_every_way(snippet, found):
    """G7. The guard's scan on synthetic sources: each way of reaching REAL_SETTINGS is seen once."""
    assert dict(real_settings_uses({'x.py': snippet})) == {('x.py',) + found: 1}


@pytest.mark.parametrize('snippet, found', [
    ('H = xbmcaddon.Addon(ADDON_ID)', [('x.py', '<module>', 'H')]),
    ('class C:\n    h = xbmcaddon.Addon()', [('x.py', 'C', 'h')]),
    ('class C:\n    def __init__(self):\n        self.h = xbmcaddon.Addon()', [('x.py', 'C.__init__', 'self.h')]),
    ('def f():\n    h = xbmcaddon.Addon()\n    return h.getSetting("x")', []),
    # the review's cases (fix stage): a handle kept in a container
    ("_FALLBACK = {}\ndef f():\n    _FALLBACK['h'] = xbmcaddon.Addon(ADDON_ID)", [('x.py', 'f', "_FALLBACK['h']")]),
    ('HANDLES = []\ndef f():\n    HANDLES.append(xbmcaddon.Addon())', [('x.py', 'f', 'HANDLES.append')]),
    ("def f(cache):\n    return cache.setdefault('h', xbmcaddon.Addon())", [('x.py', 'f', 'cache.setdefault')]),
    ("CACHE = {'h': xbmcaddon.Addon()}", [('x.py', '<module>', 'CACHE')]),
    ('class C:\n    def f(self):\n        self.handles = [xbmcaddon.Addon()]', [('x.py', 'C.f', 'self.handles')]),
    ('def f():\n    pair = (xbmcaddon.Addon(), 1)\n    return pair[0].getSetting("x")', []),
])
def test_the_handle_scan_sees_every_way(snippet, found):
    """G7 for G2."""
    assert long_lived_handles({'x.py': snippet}) == found


# G8: every literal setting id the add-on reads of its own settings is defined, with the getter's type.
OWN_READERS = ('SETTINGS', 'REAL_SETTINGS', 'Settings()')
KODI_SELF   = {'Settings': 'self', 'Builtin': 'self.settings', 'Dialog': 'self.settings'}   # kodi.py
READ_NAMES  = GETTERS + ('getSetting', 'getSettingList', 'getSettingBoolList', 'getSettingIntList',
                         'getSettingNumberList', 'getSettingFloatList', 'getSettingDict', 'getSettingFloat')
# Reviewed 2026-11-20, all pre-existing: read but not defined in settings.xml (Backup_Channels and
# Recover_Backup are commented out there); an untyped read of them gives '', a typed one None.
UNDEFINED_REVIEWED = {('autotune.py', 'Autotuned_Channels'): 'getSettingBool -> None (falsy)',
                      ('backup.py', 'Backup_Channels'): "getSetting -> '' until the add-on saves it",
                      ('backup.py', 'Recover_Backup'): "getSetting -> '' until the add-on saves it",
                      ('manager.py', 'User_Groups'): "getSetting -> ''"}


def literal_reads(sources):
    """[(file, line, getter, id)] of every read of the add-on's own settings with a literal id."""
    found = []
    for name, text in sources.items():
        src = _File(text)
        for node in ast.walk(src.tree):
            if not isinstance(node, ast.Call): continue
            func = node.func
            if ast.unparse(func) == 'Globals._readSetting' and len(node.args) == 2:
                getter, key = (a.value if isinstance(a, ast.Constant) else None for a in node.args)
                found.append((name, node.lineno, getter, key))
                continue
            if not (isinstance(func, ast.Attribute) and func.attr in READ_NAMES): continue
            reader = ast.unparse(func.value)
            owner = src.around(node)[0].split('.')[0]
            if reader not in OWN_READERS and not (name == 'kodi.py' and KODI_SELF.get(owner) == reader): continue
            if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                found.append((name, node.lineno, func.attr, node.args[0].value))
    return found


def type_errors(reads):
    bad = []
    for name, line, getter, key in reads:
        if key not in SETTING_TYPES:
            if (name, key) not in UNDEFINED_REVIEWED: bad.append((name, line, getter, key, 'not defined'))
        elif getter in GETTERS and TYPED_GETTER.get(SETTING_TYPES[key][0]) != getter:
            bad.append((name, line, getter, key, SETTING_TYPES[key][0]))
    return bad


def test_every_setting_id_read_is_defined_with_its_type():
    """G8. A misspelt id or a getter of the wrong type looks like a reload at runtime (TypeError, or
    None through SETTINGS); this catches it before a release."""
    reads = literal_reads(library_sources())
    assert len(reads) > 200 and len({r[3] for r in reads}) > 90          # the scan sees the library
    assert type_errors(reads) == []
    assert {(name, key) for name, _l, _g, key in reads if key not in SETTING_TYPES} == set(UNDEFINED_REVIEWED)


@pytest.mark.parametrize('snippet, bad', [
    ("SETTINGS.getSettingBool('Enable_Clientt')", 'not defined'),
    ("SETTINGS.getSettingInt('Enable_Client')", 'boolean'),
    ("Globals._readSetting('getSettingInt', 'Disable_Cache')", 'boolean'),
    ("REAL_SETTINGS.getSetting('No_Such_Id')", 'not defined'),
    ("Settings().getSettingString('RPC_Wait')", 'integer'),
])
def test_the_id_scan_sees_a_mistake(snippet, bad):
    """G7 for G8."""
    assert [entry[-1] for entry in type_errors(literal_reads({'x.py': snippet}))] == [bad]


# === the changed code ======================================================================================

CHANGED = [('cqueue.py', 'CustomQueue', '__init__'), ('cqueue.py', 'CustomQueue', '_chkExecutor'),
           ('cqueue.py', 'CustomQueue', '_run'), ('variables.py', 'Globals', '_readSetting'),
           ('cache.py', 'Cache', '__init__'), ('cache.py', '_Cache', '_getFreeMEM'),
           ('pool.py', 'ExecutorPool', '__init__'), ('pool.py', None, 'killit'),
           ('services.py', 'Monitor', '_updateServiceSettings')]


@pytest.mark.parametrize('filename, cls, name', CHANGED)
def test_each_changed_spot_names_the_release(filename, cls, name):
    assert 'imports.71' in ast.get_source_segment(lib_source(filename)[0], _def(filename, cls, name))


def test_the_changed_code_is_python38_and_free_of_bitor():
    from test_imports63_clock import py39_api_leaks
    from test_imports69_importcfg_helpers import BITOR_REVIEWED, bitor_lines
    for filename in ('cqueue.py', 'variables.py', 'cache.py', 'pool.py', 'services.py'):
        text = lib_source(filename)[0]
        ast.parse(text, feature_version=(3, 8))
        assert py39_api_leaks(text) == [], filename
    for filename in ('cqueue.py', 'variables.py', 'pool.py'):
        assert BITOR_REVIEWED[filename] == [] and bitor_lines(lib_source(filename)[0]) == []
    for filename, cls, name in CHANGED:
        node = _def(filename, cls, name)
        bitor = [n for n in ast.walk(node) if isinstance(n, (ast.BinOp, ast.AugAssign)) and isinstance(n.op, ast.BitOr)]
        assert bitor == [], (filename, cls, name)
    assert 'imports.71' in ast.get_source_segment(lib_source('cqueue.py')[0], _class('cqueue.py', 'CustomQueue'))
