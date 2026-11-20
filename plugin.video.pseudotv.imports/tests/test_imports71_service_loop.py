# -*- coding: utf-8 -*-
"""imports.71: the service survives a failed round; restart requests happen once.

The add-on's background service ended three times (2026-06-05, 2026-10-04 for 3 days 17 h, 2026-11-07):
an exception in one pass of its main loop (services.Service._start) went up to service.py, which logged
it, showed "Something Went Wrong" and ended the script, and Kodi never starts a stopped service script
again. The repair under test:
  - each pass of Service._start's loop, and each tick of Monitor._chkIdle's (the idle check, the LiveTV
    subtitle memory, the transition supervisor and the replay-end watcher), runs inside `except
    Exception`; each distinct failure (loopguard_helpers.error_key: its class and the add-on line it came
    from, not its message) is written once per session, at ERROR with its traceback, and no reminder
    line ever follows (the user's decision, 2026-11-20); then the loop's own wait, and on. Kodi closing
    still ends a loop at once; SystemExit and the like pass through;
  - 'Force background service to reload' restarts the service once: Service._stop clears pendingRestart
    at its end, after the threads were told to stop (before, the request stayed set and the service
    restarted every 20-30 s, doing nothing, until Kodi restarted);
  - a restored channel backup or a moved data folder sets a second Window(10000) marker next to the
    request: every module stays paused as before, and the main loop pauses with them and says once
    'Restart Kodi to finish ...', instead of restarting the service over and over.

Kodi is kodi_faithful's: the waits (FaithfulMonitor, through FaithfulService's real wait), Window(10000)
(FaithfulWindows under kodi.py's real Properties) and the add-on's settings (FaithfulAddons, make_env).
The code under test is the add-on's own, compiled from the source: service.py's _start, the Service and
Monitor methods (__init__ included), the modules' stand-in Service._interrupt, Utilities._runRestart,
Backup.recoverChannels, Tasks.setUserPath, logger.log; the queue is the real CustomQueue.

Every failure a guard catches is recorded (CAUGHT): a test that injects some takes them with caught(),
and the check after each test fails on any other, so the guard can't hide a mistake in a rig's own
wiring (a fake without the method the code calls, a missing global).
"""
import ast
import collections
import contextlib
import functools
import os
import threading
import traceback
import types
import xml.etree.ElementTree as ET

import pytest

import kodi_faithful
import loopguard_helpers
from kodi_faithful import (KODI_LOG, FaithfulMonitor, FaithfulService, FaithfulWindows, ForeverWait, Standstill,
                           compile_function, compile_method, install_windows, lib_constant, lib_source, lib_strings,
                           provoked)
from test_imports70_shutdown_contract import _File
from test_imports70_shutdown_contract import library_sources as _library_sources
from test_imports71_discovery_saves import InlineThread
from test_imports71_settings_reads import USER, make_env

HERE  = os.path.dirname(os.path.abspath(__file__))
ADDON = os.path.dirname(HERE)
LIB   = os.path.join(ADDON, 'resources', 'lib')

STRINGS          = lib_strings()
CPU_CYCLE        = lib_constant('constants.py', 'CPU_CYCLE')
SERVICE_INTERVAL = lib_constant('constants.py', 'SERVICE_INTERVAL')
ERROR            = KODI_LOG['LOGERROR']
ADDON_ID         = kodi_faithful.ADDON_ID
VERSION          = ET.parse(os.path.join(ADDON, 'addon.xml')).getroot().get('version')  # noqa: S314 -- the add-on's own
PREFIX           = '%s-%s-' % (ADDON_ID, VERSION)
T0               = 1800000000.0          # the rig's epoch
INIT             = 15                    # s: Service._start's 'Initializing...' (notificationWait, wait=15)
PASS_LINE = PREFIX + ('Service: _start, a pass of the main loop failed; the service goes on (each different error is '
                      'written once per session): ')
TICK_LINE = PREFIX + ('Monitor: _chkIdle, a tick failed; the idle loop goes on (each different error is written once '
                      'per session): ')
RESTORED, MOVED                       = STRINGS[30910], STRINGS[30911]
RESTARTING, SHUTTING_DOWN, WENT_WRONG = STRINGS[32124], STRINGS[32141], STRINGS[30079]


# === what the guards caught ========================================================================

CAUGHT = []


def caught():
    """For a test that injects failures on purpose: (loop, error) of each one a guard caught so far,
    taken off the record."""
    taken = CAUGHT[:]
    del CAUGHT[:]
    return taken


@pytest.fixture(autouse=True)
def nothing_else_was_caught():
    """After every test: the guards caught nothing the test didn't take. A guard turns any Exception into
    one log line and goes on, so a rig whose wiring is wrong would otherwise pass without running what
    it claims to run."""
    del CAUGHT[:]
    yield
    left = caught()
    assert left == [], 'a guard caught what the test did not inject:\n%s' % '\n'.join(
        '%s: %s' % (where, ''.join(traceback.format_exception(type(e), e, e.__traceback__))) for where, e in left)


def names_of(taken):
    return [type(error).__name__ for _where, error in taken]


@functools.lru_cache(maxsize=1)
def library_sources():
    """The library's sources, read once for this module's scans (not to be changed)."""
    return _library_sources()


# === the rigs ======================================================================================

def service_env(monkeypatch, user=None):
    """make_env's settings (the box's: debug logging off, Debug_Level 0), plus Window(10000) as Kodi keeps
    it and kodi.py's real Properties over it."""
    import xbmcgui
    import kodi
    env = make_env(monkeypatch, user=user)
    env.windows = FaithfulWindows()
    install_windows(monkeypatch, env.windows, xbmcgui)
    env.properties = kodi.Properties()
    env.monkeypatch = monkeypatch
    return env


@pytest.fixture
def senv(monkeypatch):
    return service_env(monkeypatch)


def prop(env, name):
    """The add-on's Window(10000) property `name` as Kodi holds it (text; '' when never set)."""
    return env.windows.get('%s.%s' % (ADDON_ID, name))


def offset(t):
    return round(t - T0, 6)


@functools.lru_cache(maxsize=1)
def class_body_writes():
    """services.Service's class-body statements that write Window(10000) (each calls PROPERTIES), in order,
    compiled from the source (with its file name and lines): what a new interpreter's import of services.py
    does to the add-on's shared flags. Run with PROPERTIES only."""
    service = [n for n in lib_source('services.py')[1].body if isinstance(n, ast.ClassDef) and n.name == 'Service'][0]
    found = [n for n in service.body if not isinstance(n, ast.FunctionDef) and 'PROPERTIES.' in ast.unparse(n)]
    return compile(ast.Module(body=found, type_ignores=[]), os.path.join(LIB, 'services.py'), 'exec')


# The Service methods the rig runs for real (FaithfulService adds the real wait and _aborted).
SERVICE_METHODS = ('log', '_que', '_isPlaying', '_tasks', '_restart', '_kodiRestart', '_interrupt', '_suspend',
                   '_sleep', '_initialize', '_start', '_passFailed', '_stop')


class ServiceRig(object):
    """The add-on's background service on a rig clock, as on the box: service.py's _start (the script
    Kodi runs) over the REAL Service.__init__ and SERVICE_METHODS (services.py) with FaithfulService's
    real wait and _aborted over a FaithfulMonitor; the REAL CustomQueue (its pop thread runs inline,
    InlineThread); kodi.py's REAL Properties over Window(10000) as Kodi keeps it (FaithfulWindows); the
    add-on's own logger.log writing kodi.log (env.lines) with Kodi 21's levels and the user's settings.
    Fakes, each call counted by the rig's Standstill: BUILTIN, the player, DIALOG (notificationWait waits
    as kodi.py's does, a second at a time), the tasks (each records its run), thread_enumerate (`threads`).
      t / closing_at     the rig clock; Kodi starts to close at closing_at (T0 + seconds)
      at(offset, fn)     fn runs inside the wait in which the clock reaches T0 + offset (Kodi goes on)
      fault(point, fn)   fn runs at a call point ('isScanning', 'isPaused', 'isPlaying', 'push', 'run')
                         and may raise; fails() builds the common kind
      passes             main-loop passes so far (each starts with BUILTIN.isScanning)
      timeline           (offset, what, detail): 'pass' n, 'wait' seconds, 'push' task, 'task' name,
                         'fail' point, 'settings refresh' (Monitor._onSettingsChanged armed: a fake here;
                         settings_chain() puts the real one in)
      notes / exits      DIALOG.notificationDialog (message, show, offset); service.py's sys.exit offsets
      starts / stops     Service._start entries (offset); Service._stop calls (offset, its argument)"""

    def __init__(self, env, seconds=3600.0, playing=False, scanning=False):
        import cqueue
        env.monkeypatch.setattr(cqueue, 'Thread', InlineThread)
        self.env, self.t, self.violations = env, T0, []
        self.closing_at = T0 + seconds
        self.playing, self.scanning = playing, scanning
        self.passes, self.timeline, self.notes, self.exits, self.starts, self.stops = 0, [], [], [], [], []
        self.faults, self._events, self.threads = {}, [], []
        rig, properties = self, env.properties
        self.count = Standstill(lambda: self.t, self.violations)
        self.xbmc = types.SimpleNamespace(log=self._write, **KODI_LOG)
        lg = dict(xbmc=self.xbmc, REAL_SETTINGS=env.real, ADDON_ID=ADDON_ID, ADDON_VERSION=VERSION, traceback=traceback)
        self.log = compile_function('logger.py', 'log', **lg)
        notice = compile_function('logger.py', 'notice', log=self.log, **lg)
        # services.Service's class body, when service.py imports services.py: its statements that write
        # Window(10000), run from the source (the four flags written False; fix stage: the Kodi-restart
        # marker cleared with the request).
        exec(class_body_writes(), {'PROPERTIES': properties})  # noqa: S102 -- the add-on's own source

        class Service(FaithfulService):
            # services.Service's class attributes (checked against the source: the class-body test)
            channels, isScanning, isPaused, isPlaying, isClient = [], scanning, False, playing, False
            pendingSuspend = pendingInterrupt = pendingRestart = False
            kodiRestartNoted = ''
            jsonQue, postQue, logoQue = set(), set(), set()
        svc = Service(self.advance, lambda: self.t, lambda: self.closing_at, properties.isPendingShutdown,
                      self.violations, standstill=self.count)
        properties.monitor = svc.monitor           # kodi.py Dialog.__init__: PROPERTIES waits on this monitor
        self.player = types.SimpleNamespace(runWhilePlaying=False,
                                            isPlaying=self._fake('player.isPlaying', lambda: rig.playing),
                                            isPlayingPseudoTV=self._fake('player.isPlayingPseudoTV', lambda: False),
                                            onAVStarted=self._fake('player.onAVStarted'),
                                            onPlayBackStopped=self._fake('player.onPlayBackStopped'))
        svc.monitor.player = self.player           # Service.__init__: self.player = self.monitor.player

        def settings_refresh():                    # Monitor._onSettingsChanged, which Service._stop re-arms
            rig.count('monitor._onSettingsChanged')
            rig.note('settings refresh')
        svc.monitor._onSettingsChanged = settings_refresh
        self.tasks = types.SimpleNamespace(getChannels=self._fake('tasks.getChannels', lambda: []))
        for name in ('_chkEpochTimer', 'chkQueTimer', '_host', '_client'): setattr(self.tasks, name, self._task(name))
        self.builtin = types.SimpleNamespace(isScanning=self._fake('BUILTIN.isScanning', lambda: rig.scanning),
                                             isPaused=self._fake('BUILTIN.isPaused', lambda: False),
                                             isSettingsOpened=self._fake('BUILTIN.isSettingsOpened', lambda: False))
        self.dialog = types.SimpleNamespace(notificationWait=self._notification_wait,
                                            notificationDialog=self._notification)
        g = dict(PROPERTIES=properties, BUILTIN=self.builtin, DIALOG=self.dialog, LANGUAGE=STRINGS.__getitem__,
                 CPU_CYCLE=CPU_CYCLE, SETTINGS=env.settings, CustomQueue=env.cqueue.CustomQueue,
                 JSONRPC=lambda service=None: None, Monitor=lambda service=None: svc.monitor,
                 Tasks=lambda service=None: rig.tasks, loopguard_helpers=loopguard_helpers, xbmc=self.xbmc,
                 notice=notice, log=self.log, thread_enumerate=lambda: list(rig.threads))
        for name in SERVICE_METHODS:
            setattr(svc, name, types.MethodType(compile_method('services.py', 'Service', name, **g), svc))
        compile_method('services.py', 'Service', '__init__', **g)(svc)        # the real Service.__init__
        queue = svc.priorityQUE
        push, run = queue._push, queue._run

        def pushed(package, priority=0, delay=0, timer=0):
            rig.count('CustomQueue._push')
            rig.note('push', package[0].__name__)
            rig._fault('push')
            return push(package, priority, delay, timer)

        def ran():
            rig.count('CustomQueue._run')
            rig._fault('run')
            return run()
        queue._push, queue._run = pushed, ran
        failed, start, stop = svc._passFailed, svc._start, svc._stop

        def pass_failed(error):
            CAUGHT.append(('main loop', error))
            return failed(error)

        def started():
            rig.starts.append(offset(rig.t))
            return start()

        def stopped(pendingRestart=False):
            rig.stops.append((offset(rig.t), pendingRestart))
            return stop(pendingRestart)
        svc._passFailed, svc._start, svc._stop = pass_failed, started, stopped
        self.service = svc
        self.script = compile_function('service.py', '_start', Service=lambda: svc, DIALOG=self.dialog,
                                       LANGUAGE=STRINGS.__getitem__, CPU_CYCLE=CPU_CYCLE, log=self.log, xbmc=self.xbmc,
                                       sys=types.SimpleNamespace(exit=lambda *a: rig.exits.append(offset(rig.t))))

    # --- Kodi going on --------------------------------------------------------------------------
    def advance(self, seconds):
        self.note('wait', seconds)
        self.t += seconds
        while self._events and T0 + self._events[0][0] <= self.t:
            self._events.pop(0)[1]()

    def at(self, when, fn):
        self._events.append((when, fn))
        self._events.sort(key=lambda event: event[0])

    def note(self, what, detail=None):
        self.timeline.append((offset(self.t), what, detail))

    # --- the fakes ------------------------------------------------------------------------------
    def _write(self, msg, level=0):                    # xbmc.log: Kodi's log
        self.count('xbmc.log')
        self.env.lines.append((msg, level))

    def _fake(self, name, answer=lambda: None):
        rig, point = self, name.split('.')[-1]

        def call(*args, **kwargs):
            rig.count(name)
            if point == 'isScanning':
                rig.passes += 1
                rig.note('pass', rig.passes)
            rig._fault(point)
            return answer()
        return call

    def _task(self, name):
        rig = self

        def task(*args, **kwargs):
            rig.count('tasks.' + name)
            rig.note('task', args[0] if name == '_chkEpochTimer' else name)
        task.__name__ = name
        return task

    def _notification_wait(self, message, header=None, wait=4, show=None, usethread=False):
        self.count('DIALOG.notificationWait')
        for _ in range(int(wait)):
            if self.service.monitor.waitForAbort(1.0): break
        return True

    def _notification(self, message, header=None, sound=False, time=None, icon=None, show=None, usethread=False):
        self.count('DIALOG.notificationDialog')
        self.notes.append((message, show, offset(self.t)))
        return True

    def fault(self, point, action):
        self.faults[point] = action

    def fails(self, point, make, when):
        """`point` raises make() whenever when() is true."""
        def action():
            if when(): raise make()
        self.fault(point, action)

    def _fault(self, point):
        action = self.faults.get(point)
        if action is None: return
        try: action()
        except BaseException:
            self.note('fail', point)
            raise

    # --- running and reading ----------------------------------------------------------------------
    def run(self):
        """The service as Kodi runs it: service.py's _start."""
        return self.script()

    def guard_lines(self):
        return [msg for msg, _level in self.env.lines if msg.startswith(PASS_LINE)]

    def waits(self):
        return [detail for _t, what, detail in self.timeline if what == 'wait']

    def waits_after_failures(self):
        """The first wait after each failure."""
        found, after = [], False
        for _t, what, detail in self.timeline:
            if what == 'fail': after = True
            elif what == 'wait' and after:
                found.append(detail)
                after = False
        return found

    def tasks_run(self, since=-1.0):
        return [(t, detail) for t, what, detail in self.timeline if what == 'task' and t > since]

    def pushes(self, since=-1.0):
        return [(t, detail) for t, what, detail in self.timeline if what == 'push' and t > since]

    def refreshes(self):
        return [t for t, what, _detail in self.timeline if what == 'settings refresh']

    def closing(self):
        return offset(self.closing_at)


class RigTimer(object):
    """threading.Timer on the rig clock, for pool.debounceit: start() lists it among the threads
    Service._stop finds (thread_enumerate, as a live Timer thread is) and runs `function` `interval` s
    later, inside the wait in which the clock gets there (on the box: on the timer's own thread); cancel()
    stops it and its thread ends, as Timer.cancel; join() never blocks."""

    def __init__(self, rig, interval, function):
        self.rig, self.interval, self.function = rig, float(interval), function
        self.name, self.cancelled, self.done = 'Thread-Timer', False, False

    def start(self):
        self.rig.threads.append(self)
        self.rig.at(offset(self.rig.t) + self.interval, self._fire)

    def _fire(self):
        if self.done: return
        self._end()
        self.function()

    def _end(self):
        self.done = True
        if self in self.rig.threads: self.rig.threads.remove(self)

    def cancel(self):
        self.cancelled = True
        self._end()

    def is_alive(self):
        return not self.done

    def join(self, timeout=None):
        return None


def settings_chain(rig, env):
    """The service's settings refresh as on the box, in a ServiceRig: the REAL Monitor.onSettingsChanged,
    _chkRollbackSwitch, _onSettingsChanged under pool.py's REAL debounceit (its wait read from the
    decorator; its Timer a RigTimer), _updatePlayerSettings and _updateServiceSettings (services.py), and
    the REAL Tasks.chkSettingsChange (tasks.py), over kodi.py's SETTINGS on the rig's settings. Its actions
    are recorded in `rig.actions` instead of run: setUserPath, toggleShowLog, setPVRRemote. The player's
    values start at the settings' values; _refreshChannelRules and the rollback calls are counted. Kodi
    runs onSettingsChanged inside the main thread's wait after each save: call it from rig.at()."""
    monitor, service, settings = rig.service.monitor, rig.service, env.settings
    rig.actions = []

    def action(name):
        def act(*args, **kwargs):
            rig.actions.append((offset(rig.t), name, kwargs))
        act.__name__ = name
        return act
    player = rig.player
    player.minDuration = settings.getSettingInt('Seek_Tolerance')
    player.maxProgress = settings.getSettingInt('Seek_Threshold')
    player.sleepTime = settings.getSettingInt('Idle_Timer')
    player.runWhilePlaying = settings.getSettingBool('Run_While_Playing')
    player._refreshChannelRules = rig._fake('player._refreshChannelRules')
    player._rollbackSwitched = rig._fake('player._rollbackSwitched', lambda: False)
    player._cancelPlaystate = rig._fake('player._cancelPlaystate')
    monitor.service, monitor.rollbackSwitch = service, settings.getSettingBool('Rollback_Watched')
    monitor.log = lambda msg, level=0: rig.log('Monitor: %s' % msg, level)
    pool = dict(Timer=lambda interval, function: RigTimer(rig, interval, function), Lock=threading.Lock,
                wraps=functools.wraps, timeit=lambda method: contextlib.nullcontext(), log=rig.log, xbmc=rig.xbmc,
                SERVICE_INTERVAL=SERVICE_INTERVAL)
    debounceit = compile_function('pool.py', 'debounceit', **pool)
    decorator = kodi_faithful.method_node('services.py', 'Monitor', '_onSettingsChanged').decorator_list
    assert [ast.unparse(d) for d in decorator] == ['debounceit(15)']
    wait = decorator[0].args[0].value
    g = dict(SETTINGS=settings)
    for name in ('onSettingsChanged', '_chkRollbackSwitch', '_updatePlayerSettings', '_updateServiceSettings'):
        setattr(monitor, name, types.MethodType(compile_method('services.py', 'Monitor', name, **g), monitor))
    plain = compile_method('services.py', 'Monitor', '_onSettingsChanged', **g)
    monitor._onSettingsChanged = types.MethodType(debounceit(wait)(plain), monitor)
    owner = types.SimpleNamespace(service=service, log=lambda *a, **k: None, setUserPath=action('setUserPath'),
                                  jsonRPC=types.SimpleNamespace(toggleShowLog=action('toggleShowLog')))
    tg = dict(SETTINGS=types.SimpleNamespace(getCurrentSettings=settings.getCurrentSettings,
                                             getSettingBool=settings.getSettingBool,
                                             setPVRRemote=action('setPVRRemote')),
              PROPERTIES=types.SimpleNamespace(getRemoteHost=lambda: 'the host', getFriendlyName=lambda: 'the box'))
    check = compile_method('tasks.py', 'Tasks', 'chkSettingsChange', **tg)
    rig.tasks.chkSettingsChange = types.MethodType(check, owner)
    return monitor


class Joined(object):
    """A thread Service._stop tells to stop (thread_enumerate): its join() runs `on_join` on a thread of
    its own, as a Timer thread's work lands meanwhile on the box, and waits for it."""

    def __init__(self, name, on_join):
        self.name, self._on_join, self.joins, self.errors = name, on_join, 0, []

    def is_alive(self):
        return True

    def cancel(self):
        return None

    def join(self, timeout=None):
        self.joins += 1

        def work():
            try: self._on_join()
            except BaseException as e:  # a failure on this thread must reach the test
                self.errors.append(e)
        thread = threading.Thread(target=work, name=self.name)
        thread.start()
        thread.join(kodi_faithful.GATE_CAP_S)
        assert not thread.is_alive(), '%s still running' % self.name


def menu_restart(env):
    """Utility Menu 'Force background service to reload.': the real Utilities._runRestart."""
    return compile_method('utilities.py', 'Utilities', '_runRestart', PROPERTIES=env.properties)


def recover(env):
    """Add-on settings 'Restore channels': the real Backup.recoverChannels (backup.py; its yes/no answered
    yes, the file move and copy stand-ins that succeed). It runs in a script interpreter of its own."""
    files = types.SimpleNamespace(move=lambda a, b: True, copy=lambda a, b: True)
    g = dict(DIALOG=types.SimpleNamespace(yesnoDialog=lambda *a, **k: True), LANGUAGE=STRINGS.__getitem__,
             SETTINGS=types.SimpleNamespace(getSetting=lambda key: ''),
             BUILTIN=types.SimpleNamespace(busy_dialog=contextlib.nullcontext), PROPERTIES=env.properties,
             FileAccess=files, CHANNELFLEPATH='channels.json', CHANNELFLE_RESTORE='channels.restore.json',
             CHANNELFLE_BACKUP='channels.backup.json')
    run = compile_method('backup.py', 'Backup', 'recoverChannels', **g)
    return lambda: run(types.SimpleNamespace(log=lambda *a, **k: None))


def move_folder(env):
    """A new User_Folder: the real Tasks.setUserPath (tasks.py; the folder copy a stand-in), which
    Tasks.chkSettingsChange queues on the service's queue."""
    g = dict(DIALOG=types.SimpleNamespace(progressDialog=lambda *a, **k: 'dialog'), LANGUAGE=STRINGS.__getitem__,
             PROPERTIES=env.properties, FileAccess=types.SimpleNamespace(copyFolder=lambda old, new, dia: True))
    run = compile_method('tasks.py', 'Tasks', 'setUserPath', **g)
    return lambda: run(types.SimpleNamespace(log=lambda *a, **k: None), 'the old folder', 'the new folder')


STUB_SERVICES = ('builder.py', 'jsonrpc.py', 'library.py', 'multiroom.py', 'resources.py')


def stub_interrupts(env):
    """{file: its stand-in Service._interrupt}: what the add-on's other parts (the Manager, autotune,
    library walks, built without the real service) ask before each step, over Window(10000)."""
    return {name: functools.partial(compile_method(name, 'Service', '_interrupt', PROPERTIES=env.properties), object())
            for name in STUB_SERVICES}


def quiet_monitor():
    """A monitor for PROPERTIES.interruptActivity when no service rig runs."""
    clock = [T0]

    def advance(seconds):
        clock[0] += seconds
    return FaithfulMonitor(advance, lambda: clock[0], lambda: None, [])


# === loopguard_helpers (pure) =====================================================================

def _function(source, filename, name):
    """A function compiled from `source` as if it were the file `filename` (no file is read)."""
    namespace = {}
    exec(compile(source, filename, 'exec'), namespace)  # noqa: S102 -- test-made code, for its frames
    return namespace[name]


OUTSIDE = os.path.join(HERE, 'not_the_addon.py')
INSIDE  = os.path.join(loopguard_helpers.LIB_DIR, 'pretend.py')


def _failure(text, caller='def g(f, text):\n    return f(text)\n', error=TypeError):
    """An error raised outside the add-on (`f`, think Kodi's binding) and called from an add-on file
    (`g`): what the guard sees when a Kodi call fails."""
    inner = _function('def f(text):\n    raise E(text)\n', OUTSIDE, 'f')
    inner.__globals__['E'] = error
    outer = _function(caller, INSIDE, 'g')
    try: outer(inner, text)
    except error as e: return e
    raise AssertionError('no error')


def test_the_key_is_the_class_and_the_innermost_addon_line():
    """The innermost frame in the add-on's own files: g's line 2 in pretend.py, not f in the file outside."""
    assert loopguard_helpers.error_key(_failure('Invalid setting type')) == ('TypeError', 'pretend.py', 2, 'g')


def test_the_message_is_left_out_of_the_key():
    """One error whose text holds a number or an id is one error, whatever the number."""
    keys = {loopguard_helpers.error_key(_failure('Invalid setting type #%d' % n)) for n in range(5)}
    assert len(keys) == 1


def test_another_line_or_another_class_is_another_error():
    one = loopguard_helpers.error_key(_failure('x'))
    other_line = loopguard_helpers.error_key(_failure('x', caller='def g(f, text):\n    a = 1\n    return f(text)\n'))
    other_class = loopguard_helpers.error_key(_failure('x', error=ValueError))
    assert len({one, other_line, other_class}) == 3
    assert other_line == ('TypeError', 'pretend.py', 3, 'g') and other_class[0] == 'ValueError'


def test_without_an_addon_frame_the_innermost_frame_and_without_a_traceback_the_class():
    error = _failure('x')
    nowhere = os.path.join(HERE, 'nowhere')
    assert loopguard_helpers.error_key(error, lib_dir=nowhere) == ('TypeError', 'not_the_addon.py', 2, 'f')
    assert loopguard_helpers.error_key(KeyError('never raised')) == ('KeyError', '', 0, '')


def test_describe_reads_as_pythons_last_line_and_never_raises():
    class Broken(Exception):
        def __str__(self):
            raise RuntimeError('no text')
    assert loopguard_helpers.describe(TypeError('Invalid setting type')) == 'TypeError: Invalid setting type'
    assert loopguard_helpers.describe(KeyError('chkQueTimer')) == "KeyError: 'chkQueTimer'"
    assert loopguard_helpers.describe(RuntimeError()) == 'RuntimeError'
    assert loopguard_helpers.describe(Broken()) == 'Broken: <exception str() failed>'


def test_each_key_comes_first_once():
    failures = loopguard_helpers.Failures()
    assert [failures.first(k) for k in ('a', 'b', 'a', 'a', 'b', 'c')] == [True, True, False, False, False, True]
    assert len(failures) == 3


def test_the_memory_is_bounded_and_forgets_the_one_seen_longest_ago():
    """L7. At most max_keys; a key that keeps coming back moves to the end and is never the one forgotten."""
    failures = loopguard_helpers.Failures(max_keys=3)
    assert [failures.first(k) for k in ('a', 'b', 'c', 'a', 'd')] == [True, True, True, False, True]
    assert len(failures) == 3
    assert failures.first('a') is False                        # seen again just before 'd': kept
    assert failures.first('b') is True                         # the one seen longest ago: forgotten
    big = loopguard_helpers.Failures()
    for n in range(1000): big.first(('Error%d' % n, 'x.py', n, 'f'))
    assert len(big) == loopguard_helpers.MAX_KEYS == lib_constant('loopguard_helpers.py', 'MAX_KEYS')


def test_the_helper_module_is_pure():
    """Standard library only, no Kodi, no add-on module, no file read (walk_tb, not extract_tb, which reads
    source lines through linecache), Python 3.8, no '|'."""
    from test_imports63_clock import py39_api_leaks
    from test_imports69_importcfg_helpers import BITOR_REVIEWED, bitor_lines
    text, tree = lib_source('loopguard_helpers.py')
    imported = sorted([alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
                      + [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)])
    assert imported == ['collections', 'os', 'traceback']
    calls = sorted({ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)})
    assert calls == ['OrderedDict', 'entry[0].startswith', 'len', 'os.path.abspath', 'os.path.basename',
                     'os.path.dirname', 'self._seen.move_to_end', 'self._seen.popitem', 'str', 'traceback.walk_tb',
                     'type'], calls
    ast.parse(text, feature_version=(3, 8))
    assert py39_api_leaks(text) == [] and bitor_lines(text) == [] and BITOR_REVIEWED['loopguard_helpers.py'] == []
    assert 'imports.71' in text


# === the main loop's guard =========================================================================

@pytest.mark.parametrize('state', ['idle', 'playing', 'scanning'])
def test_a_failed_pass_does_not_stop_the_service(senv, state):
    """L1. The queue push of pass 3 fails (the 2026-11-07 crash failed under it, in CustomQueue._run): one
    ERROR line; the passes after it push their task again, and the service ends only when Kodi closes:
    one 'Shutting down', one exit, no 'Something Went Wrong'. Also with pendingInterrupt set (playing
    with "Run while playing" off, a library scan), when the queue holds its tasks back."""
    rig = ServiceRig(senv, seconds=600, playing=state == 'playing', scanning=state == 'scanning')
    rig.fails('push', lambda: TypeError('Invalid setting type'), when=lambda: rig.passes == 3)
    held = []
    rig.at(300, lambda: held.append(rig.service.pendingInterrupt))
    rig.run()
    assert names_of(caught()) == ['TypeError'] and len(rig.guard_lines()) == 1
    failed_at = [t for t, what, _d in rig.timeline if what == 'fail'][0]
    later = [t for t, task in rig.pushes(since=failed_at) if task == '_chkEpochTimer']
    assert len(later) >= (600 - failed_at) // (SERVICE_INTERVAL + 1) and rig.passes > 100
    if state == 'idle': assert len(rig.tasks_run(since=failed_at)) == len(later) - 1   # each but the one as Kodi closed
    assert held == [state != 'idle']
    assert rig.notes == [(SHUTTING_DOWN, None, rig.closing())] and rig.exits == [rig.closing()]
    assert rig.stops == [(rig.closing(), False)] and rig.violations == []


def test_each_different_error_is_written_once_with_its_traceback(senv):
    """L2. Two different errors: E1 (TypeError, the 2026-11-07 text) at the queue push from pass 3 to 50,
    E2 (RuntimeError) at the pass's first Kodi read from pass 60 to 65. Exactly two lines, at ERROR, each
    with the error, its traceback and the add-on lines it went through, with the box's settings (debug
    logging off); the service went on throughout."""
    rig = ServiceRig(senv, seconds=1200)
    rig.fails('push', lambda: TypeError('Invalid setting type'), when=lambda: 3 <= rig.passes <= 50)
    rig.fails('isScanning', lambda: RuntimeError('Kodi said no'), when=lambda: 60 <= rig.passes <= 65)
    rig.run()
    taken = caught()
    assert collections.Counter(names_of(taken)) == {'TypeError': 48, 'RuntimeError': 6}
    lines = [(msg, level) for msg, level in senv.lines if msg.startswith(PASS_LINE)]
    assert [level for _m, level in lines] == [ERROR, ERROR]
    first, second = lines[0][0], lines[1][0]
    assert first.startswith(PASS_LINE + 'TypeError: Invalid setting type\nTraceback (most recent call last):\n')
    assert all(' in %s\n' % name in first for name in ('_start', '_tasks', '_que'))
    assert second.startswith(PASS_LINE + 'RuntimeError: Kodi said no\nTraceback (most recent call last):\n')
    push_line = _line('Service', '_que', 'self.priorityQUE._push')
    assert loopguard_helpers.error_key(taken[0][1])[1:] == ('services.py', push_line, '_que')
    assert rig.passes > 70 and rig.exits == [rig.closing()]


def _line(cls, name, text):
    """The line of services.py's cls.name that holds `text` (once)."""
    node = kodi_faithful.method_node('services.py', cls, name)
    source = lib_source('services.py')[0].splitlines()
    found = [n for n in range(node.lineno, node.end_lineno + 1) if text in source[n - 1]]
    assert len(found) == 1, (cls, name, text, found)
    return found[0]


@pytest.mark.parametrize('debug', ['false', 'true'])
def test_a_repeating_error_writes_no_more_lines(monkeypatch, debug):
    """L3. The same error on every pass for two hours, its text different each time: one line in all and
    no reminder ever (the user's decision: each different error once); the loop passed every 10 s (the
    pass's wait, then the guard's). Fix stage: also with the add-on's debug logging on (Debug_Level 0,
    every level written): no line at any level names the error after the first."""
    env = service_env(monkeypatch, user=dict(USER, Debug_Enable=debug))
    rig = ServiceRig(env, seconds=7200)
    rig.fails('push', lambda: TypeError('Invalid setting type #%d' % rig.passes), when=lambda: rig.passes >= 1)
    rig.run()
    assert len(caught()) == rig.passes >= (7200 - INIT) // (2 * SERVICE_INTERVAL)
    assert len(rig.guard_lines()) == 1
    assert [msg for msg, _level in env.lines if 'Invalid setting type #' in msg] == rig.guard_lines()
    added = [(msg, level) for msg, level in env.lines if ADDON_ID in msg]
    if debug == 'false':
        assert [msg for msg, level in added if level >= KODI_LOG['LOGINFO']] == rig.guard_lines()
    else:
        assert len([1 for _msg, level in added if level == KODI_LOG['LOGDEBUG']]) > rig.passes   # debug lines written


@pytest.mark.parametrize('state', ['idle', 'playing'])
def test_a_pass_that_fails_before_its_wait_still_waits(senv, state):
    """L4. The pass's first Kodi read fails on every pass, before the pass reaches its own wait: the guard
    waits the service's interval, every time, so the loop never turns without a pause (one that did would
    be refused by the rig's Standstill). Also while playing with "Run while playing" off, where
    pendingInterrupt is set and Service._sleep would end its wait after one 16 ms step."""
    rig = ServiceRig(senv, seconds=INIT + 60, playing=state == 'playing')
    rig.fails('isScanning', lambda: OSError(5, 'Input/output error'), when=lambda: rig.passes >= 1)
    held = []
    rig.at(INIT + 30, lambda: held.append(rig.service.pendingInterrupt))
    rig.run()
    assert len(caught()) == rig.passes and len(rig.guard_lines()) == 1
    after = rig.waits_after_failures()
    assert after[:-1] == [SERVICE_INTERVAL] * (len(after) - 1) and 0 < after[-1] <= SERVICE_INTERVAL
    assert rig.passes <= 60 / SERVICE_INTERVAL + 1 and all(wait > 0 for wait in rig.waits())
    assert held == [state == 'playing'] and rig.violations == []


@pytest.mark.parametrize('state', ['playing', 'scanning'])
def test_with_the_queue_held_back_a_failed_pass_still_waits_the_full_interval(senv, state):
    """The review's case: pendingInterrupt set. Service._sleep ends after one 16 ms step then; a guard
    that waited with it would retry about 60 times a second, and one that took its answer for "stop"
    would end the service at the first failure during playback. Each failing pass waits the interval
    twice (its own wait and the guard's), and the service runs until Kodi closes."""
    rig = ServiceRig(senv, seconds=300, playing=state == 'playing', scanning=state == 'scanning')
    rig.fails('push', lambda: KeyError('chkQueTimer'), when=lambda: rig.passes >= 1)
    rig.run()
    assert len(caught()) == rig.passes and rig.service.pendingInterrupt is True
    after = rig.waits_after_failures()
    assert after[:-1] == [SERVICE_INTERVAL] * (len(after) - 1)
    assert rig.passes <= (300 - INIT) / (2 * SERVICE_INTERVAL) + 1
    assert rig.stops == [(rig.closing(), False)] and rig.exits == [rig.closing()]


@pytest.mark.parametrize('state', ['idle', 'playing', 'scanning'])
def test_kodi_closing_during_the_guards_wait_ends_the_service_at_once(senv, state):
    """L5. Kodi starts to close 2 s into the wait after the third failure: no further pass, Service._stop
    once, service.py's 'Shutting down' once and its exit, all at that moment."""
    rig = ServiceRig(senv, seconds=3600, playing=state == 'playing', scanning=state == 'scanning')

    def failure():
        if rig.passes == 3: rig.closing_at = rig.t + 2.0
        return RuntimeError('pass %d' % rig.passes)
    rig.fails('push', failure, when=lambda: rig.passes >= 1)
    rig.run()
    assert len(caught()) == 3 and rig.passes == 3
    assert rig.stops == [(rig.closing(), False)] and rig.exits == [rig.closing()]
    assert rig.notes == [(SHUTTING_DOWN, None, rig.closing())] and offset(rig.t) == rig.closing()


@pytest.mark.parametrize('error', [SystemExit, KeyboardInterrupt, ForeverWait])
def test_what_is_not_an_exception_passes_through(senv, error):
    """L6. SystemExit, KeyboardInterrupt and the rigs' ForeverWait (a wait Kodi would never end) are no
    Exception: the guard lets them through, out of Service._start and service.py's _start (whose finally
    still exits), with no guard line and no 'Something Went Wrong'."""
    rig = ServiceRig(senv, seconds=600)

    def failure():
        if error is ForeverWait: kodi_faithful.record('a wait Kodi would never end', rig.violations)
        return error('pass 3')
    rig.fails('isScanning', failure, when=lambda: rig.passes == 3)
    with pytest.raises(error):
        rig.run()
    if error is ForeverWait: assert provoked() == ['a wait Kodi would never end']
    assert caught() == [] and rig.guard_lines() == []
    assert len(rig.exits) == 1 and rig.notes == [] and rig.stops == []


def test_the_service_keeps_a_bounded_memory(senv):
    """L7 through the service: 100 different errors (a class each) are each written once, and the memory
    holds at most MAX_KEYS of them."""
    rig = ServiceRig(senv, seconds=INIT + 100 * SERVICE_INTERVAL + 30)
    kinds = [type('Error%d' % n, (Exception,), {}) for n in range(100)]
    rig.fails('isScanning', lambda: kinds[rig.passes - 1]('x'), when=lambda: 1 <= rig.passes <= 100)
    rig.run()
    assert len(caught()) == 100 and len(rig.guard_lines()) == 100
    assert len(rig.service.loopFailures) == loopguard_helpers.MAX_KEYS


@pytest.mark.parametrize('enable', ['false', 'true'])
@pytest.mark.parametrize('level', ['0', '1', '2', '3', '4'])
def test_the_line_reaches_kodis_log_whatever_debug_logging_says(monkeypatch, enable, level):
    """L8. Written at ERROR with the add-on's debug logging off or on, at Debug_Level 0-3 (the box: off,
    0). Debug_Level 4 ('fatal only') drops it, as it drops every ERROR line of the add-on: a documented
    limit (the plan)."""
    env = service_env(monkeypatch, user=dict(USER, Debug_Enable=enable, Debug_Level=level))
    rig = ServiceRig(env, seconds=40)
    rig.fails('push', lambda: TypeError('Invalid setting type'), when=lambda: rig.passes == 2)
    rig.run()
    assert len(caught()) == 1
    lines = [(m, lv) for m, lv in env.lines if m.startswith(PASS_LINE)]
    assert [lv for _m, lv in lines] == ([] if level == '4' else [ERROR])


@pytest.mark.parametrize('phase', ['A', 'B'])
def test_the_line_is_written_while_kodi_rebuilds_the_settings(senv, phase):
    """A failure during a save of the add-on's settings: logger.log reads Debug_Level from REAL_SETTINGS,
    '' in phase A (read as 3), the settings.xml default 3 in phase B: written either way."""
    rig = ServiceRig(senv, seconds=60)

    def failure():
        senv.real.phase = phase
        return TypeError('Invalid setting type')
    rig.fails('push', failure, when=lambda: rig.passes == 2)
    rig.at(INIT + 2 * SERVICE_INTERVAL + 3, lambda: setattr(senv.real, 'phase', None))
    rig.run()
    assert len(caught()) == 1 and len(rig.guard_lines()) == 1


def test_the_2026_11_07_chain_through_service_py(senv):
    """X1. service.py -> Service._start -> _tasks -> _que -> CustomQueue._push -> _run, with REAL_SETTINGS
    held mid-rebuild (phase A, a settings save's window) from the 4th pass for a minute: nothing fails,
    no guard line, no Kodi 'EXCEPTION: Invalid setting type', chkQueTimer queued and run on every pass;
    only 'Shutting down' at Kodi's close. On 399744fd the pass inside the window raised TypeError at
    cqueue.py:77, and service.py showed 'Something Went Wrong' and ended (proof-run.txt)."""
    rig = ServiceRig(senv, seconds=300)
    rig.at(INIT + 3 * SERVICE_INTERVAL, lambda: setattr(senv.real, 'phase', 'A'))
    rig.at(INIT + 3 * SERVICE_INTERVAL + 60, lambda: setattr(senv.real, 'phase', None))
    rig.run()
    inside = [t for t, task in rig.tasks_run() if INIT + 3 * SERVICE_INTERVAL < t < INIT + 3 * SERVICE_INTERVAL + 60]
    assert len(inside) >= 11 and set(task for _t, task in rig.tasks_run()) == {'_host', 'chkQueTimer'}
    assert caught() == [] and senv.reg.kodi_errors() == []
    assert rig.notes == [(SHUTTING_DOWN, None, rig.closing())] and rig.exits == [rig.closing()]


def test_the_old_read_would_now_cost_one_line_not_the_service(senv):
    """The same window with 399744fd's typed read put back in CustomQueue._run (a stand-in here): each
    pass inside it raises TypeError('Invalid setting type') with Kodi's own EXCEPTION line; the guard
    writes one ERROR line and the service runs on to Kodi's close."""
    rig = ServiceRig(senv, seconds=300)
    rig.fault('run', lambda: senv.real.getSettingBool('Enable_Executor'))
    rig.at(INIT + 3 * SERVICE_INTERVAL, lambda: setattr(senv.real, 'phase', 'A'))
    rig.at(INIT + 3 * SERVICE_INTERVAL + 60, lambda: setattr(senv.real, 'phase', None))
    rig.run()
    taken = caught()
    assert len(taken) >= 5 and set(names_of(taken)) == {'TypeError'}
    assert len(rig.guard_lines()) == 1 and len(senv.reg.kodi_errors()) == len(taken)
    assert [t for t, _task in rig.tasks_run() if t > INIT + 3 * SERVICE_INTERVAL + 60]       # tasks run again after it
    assert rig.notes == [(SHUTTING_DOWN, None, rig.closing())]


# === the idle loop's guard (Monitor._chkIdle) =======================================================

MONITOR_METHODS = ('log', '_chkIdle', '_tickFailed')


class IdleRig(object):
    """Monitor._chkIdle, the real loop of services.py, with the real Monitor.__init__ (its Kodi base class
    and its Player stand-ins), _tickFailed and log (the real logger, the box's settings), over
    FaithfulService (the real wait) on a rig clock. The tick's player calls are fakes that record and
    can be made to fail (`faults`); `pending` is the add-on's pending-shutdown property read. Kodi closes
    at closing_at."""

    def __init__(self, env, seconds=60.0, pseudotv=False):
        self.env, self.t, self.violations = env, T0, []
        self.closing_at, self.pseudotv = T0 + seconds, pseudotv
        self.calls, self.faults, self.waits = [], {}, []
        rig = self
        count = Standstill(lambda: self.t, self.violations)
        self.service = FaithfulService(self.advance, lambda: self.t, lambda: self.closing_at,
                                       lambda: rig._call('pending'), self.violations, standstill=count)
        self.service.jsonRPC = None
        xbmc = types.SimpleNamespace(log=lambda msg, level=0: (count('xbmc.log'), env.lines.append((msg, level))),
                                     Monitor=types.SimpleNamespace(__init__=lambda monitor: None), **KODI_LOG)
        lg = dict(xbmc=xbmc, REAL_SETTINGS=env.real, ADDON_ID=ADDON_ID, ADDON_VERSION=VERSION, traceback=traceback)
        player = types.SimpleNamespace(**{name: functools.partial(self._call, name) for name in
                                          ('_onIdle', '_chkLiveSubtitles', '_chkTransition', '_chkReplayEnd')})
        player.isPlayingPseudoTV = lambda: rig._call('isPlayingPseudoTV') or rig.pseudotv
        self.count = count

        class Monitor(object):
            idleTime, isIdle, isRunning = 0, False, False
        monitor = Monitor()
        g = dict(xbmc=xbmc, Player=lambda monitor_, service: player, SETTINGS=env.settings,
                 loopguard_helpers=loopguard_helpers, OSD_TIMER=5, log=compile_function('logger.py', 'log', **lg),
                 BUILTIN=types.SimpleNamespace(getIdle=lambda: rig._call('getIdle') or 0))
        compile_method('services.py', 'Monitor', '__init__', **g)(monitor, service=self.service)
        for name in MONITOR_METHODS:
            setattr(monitor, name, types.MethodType(compile_method('services.py', 'Monitor', name, **g), monitor))
        monitor.abortRequested = self.service.monitor.abortRequested
        failed = monitor._tickFailed

        def tick_failed(error):
            CAUGHT.append(('idle loop', error))
            return failed(error)
        monitor._tickFailed = tick_failed
        self.monitor = monitor

    def advance(self, seconds):
        self.waits.append(seconds)
        self.t += seconds

    def _call(self, name):
        self.count(name)
        self.calls.append((offset(self.t), name))
        action = self.faults.get(name)
        if action is not None: action()
        return None

    def fails(self, name, make, when=lambda: True):
        def action():
            if when(): raise make()
        self.faults[name] = action

    def ticks(self, name='_chkTransition'):
        return [t for t, call in self.calls if call == name]

    def guard_lines(self):
        return [msg for msg, _level in self.env.lines if msg.startswith(TICK_LINE)]

    def run(self):
        return self.monitor._chkIdle()


def test_a_failed_tick_does_not_end_the_idle_loop(senv):
    """I1. The replay-end watcher's tick fails from the 3rd to the 10th tick: one ERROR line with its
    traceback; the ticks go on (subtitle memory, transition supervisor, replay-end watcher) until Kodi
    closes, and the loop ends as before (isRunning False). Before, the first failure ended this thread
    for the session."""
    rig = IdleRig(senv, seconds=60)
    rig.fails('_chkReplayEnd', lambda: KeyError('epg_playlist_item'), when=lambda: 3 <= len(rig.ticks()) <= 10)
    rig.run()
    assert names_of(caught()) == ['KeyError'] * 8
    lines = rig.guard_lines()
    assert len(lines) == 1
    assert lines[0].startswith(TICK_LINE + "KeyError: 'epg_playlist_item'\nTraceback (most recent call last):\n")
    assert ' in _chkIdle\n' in lines[0] and len(rig.ticks()) >= 55
    assert len(rig.ticks('_chkLiveSubtitles')) == len(rig.ticks('_chkReplayEnd')) == len(rig.ticks())
    assert rig.monitor.isRunning is False and rig.violations == []


def test_each_different_tick_error_is_written_once(senv):
    rig = IdleRig(senv, seconds=60)
    rig.fails('_chkReplayEnd', lambda: KeyError('x'), when=lambda: len(rig.ticks()) % 2 == 0)
    rig.fails('_chkTransition', lambda: ValueError('y'), when=lambda: len(rig.ticks()) % 3 == 0)
    rig.run()
    assert set(names_of(caught())) == {'KeyError', 'ValueError'}
    assert [line.split('\n')[0][len(TICK_LINE):] for line in rig.guard_lines()] == ["KeyError: 'x'", 'ValueError: y']


@pytest.mark.parametrize('debug', ['false', 'true'])
def test_a_repeating_tick_error_writes_no_more_lines(monkeypatch, debug):
    """The review's case (fix stage): the replay-end watcher's tick fails on every tick for a minute (about
    60), its text different each time. With the add-on's debug logging off or on (Debug_Level 0, every
    level written), one line in all names it: the guard's first."""
    env = service_env(monkeypatch, user=dict(USER, Debug_Enable=debug))
    rig = IdleRig(env, seconds=60)
    rig.monitor.log('a debug line')                           # the rig's logger follows the setting
    assert [lv for msg, lv in env.lines if msg.endswith('Monitor: a debug line')] == (
        [KODI_LOG['LOGDEBUG']] if debug == 'true' else [])
    rig.fails('_chkReplayEnd', lambda: KeyError('epg_playlist_item #%d' % len(rig.ticks())))
    rig.run()
    assert len(caught()) == len(rig.ticks()) >= 55
    assert [msg for msg, _level in env.lines if 'epg_playlist_item #' in msg] == rig.guard_lines()
    assert len(rig.guard_lines()) == 1


@pytest.mark.parametrize('pseudotv', [False, True])
def test_a_failing_tick_never_turns_without_a_wait(senv, pseudotv):
    """I3. A tick that fails right after its first wait, every time, for a minute: each failure is followed
    by the tick's 0.5 s wait (a turn takes 1 s), on both branches; nothing refused, one line."""
    rig = IdleRig(senv, seconds=60, pseudotv=pseudotv)
    rig.fails('isPlayingPseudoTV', lambda: RuntimeError('no player'))
    rig.run()
    assert len(caught()) == len(rig.ticks('isPlayingPseudoTV')) and len(rig.guard_lines()) == 1
    assert set(rig.waits) == {0.5} and len(rig.ticks('isPlayingPseudoTV')) <= 60 / 1.0 + 1
    assert rig.violations == []


def test_a_failing_wait_ends_the_thread_as_before(senv):
    """I4. When the tick's wait itself fails (here the add-on's pending-shutdown read after it), the
    guard's own wait fails the same way and the exception ends the thread, as before imports.71: written
    once, never a loop without a wait."""
    rig = IdleRig(senv, seconds=60)
    rig.fails('pending', lambda: OSError('Window(10000) gone'), when=lambda: len(rig.waits) >= 4)
    with pytest.raises(OSError):
        rig.run()
    assert names_of(caught()) == ['OSError'] and len(rig.guard_lines()) == 1
    assert rig.waits == [0.5] * 5 and rig.monitor.isRunning is True


@pytest.mark.parametrize('error', [SystemExit, KeyboardInterrupt, ForeverWait])
def test_what_is_not_an_exception_passes_through_a_tick(senv, error):
    rig = IdleRig(senv, seconds=60)
    rig.fails('_chkTransition', lambda: error('tick 3'), when=lambda: len(rig.ticks()) == 3)
    with pytest.raises(error):
        rig.run()
    assert caught() == [] and rig.guard_lines() == []


def test_each_loop_has_a_memory_of_its_own():
    """Monitor.__init__ makes the idle loop's memory, Service.__init__ the main loop's; each guard writes
    through its own (an error both loops hit is written by each)."""
    def assigned(cls):
        node = kodi_faithful.method_node('services.py', cls, '__init__')
        return [ast.unparse(n) for n in node.body if isinstance(n, ast.Assign) and 'Failures' in ast.unparse(n)]
    assert assigned('Monitor') == ['self.tickFailures = loopguard_helpers.Failures()']
    assert assigned('Service') == ['self.loopFailures = loopguard_helpers.Failures()']

    def memories(cls, name):
        node = kodi_faithful.method_node('services.py', cls, name)
        return sorted({ast.unparse(n) for n in ast.walk(node)
                       if isinstance(n, ast.Attribute) and n.attr.endswith('Failures')})
    assert memories('Monitor', '_tickFailed') == ['self.tickFailures']
    assert memories('Service', '_passFailed') == ['self.loopFailures']


# === restart requests ================================================================================

def test_the_menu_restart_restarts_the_service_once(senv):
    """P1. 'Force background service to reload.' (the real Utilities._runRestart) one minute in: one
    'Restarting background service...', Service._start entered twice, the request taken (pendingRestart
    reads False after), the restarted run queues its start-up tasks and passes again; at Kodi's close one
    'Shutting down'. 399744fd restarted again every 20-30 s, doing nothing, until Kodi closed."""
    rig = ServiceRig(senv, seconds=1800)
    rig.at(60, menu_restart(senv))
    rig.run()
    assert [message for message, _show, _t in rig.notes] == [RESTARTING, SHUTTING_DOWN]
    assert len(rig.starts) == 2 and [argument for _t, argument in rig.stops] == [True, False]
    assert prop(senv, 'pendingRestart') == 'False' and rig.service.pendingRestart is False
    again = rig.tasks_run(since=rig.starts[1] - 0.001)
    assert again[0][1] == '_host' and len([1 for _t, task in again if task == 'chkQueTimer']) > 300
    assert rig.exits == [rig.closing()]
    assert rig.refreshes() == [rig.stops[0][0]]       # fix stage: the restart re-arms the settings refresh


def test_the_guards_memory_outlives_a_restart(senv):
    """The same failure before and after a menu restart is written once: the memory belongs to the service
    object (Service.__init__), which service.py restarts in place, as Kodi's log belongs to the session."""
    rig = ServiceRig(senv, seconds=600)
    rig.fails('push', lambda: TypeError('Invalid setting type'), when=lambda: rig.passes in (3, 30))
    rig.at(60, menu_restart(senv))
    rig.run()
    assert len(rig.starts) == 2 and names_of(caught()) == ['TypeError', 'TypeError']
    assert len(rig.guard_lines()) == 1


def test_a_later_request_restarts_once_more(senv):
    """P2. A second request a quarter of an hour after the first restart: exactly one more."""
    rig = ServiceRig(senv, seconds=1800)
    restart = menu_restart(senv)
    rig.at(60, restart)
    rig.at(900, restart)
    rig.run()
    assert [message for message, _show, _t in rig.notes] == [RESTARTING, RESTARTING, SHUTTING_DOWN]
    assert len(rig.starts) == 3 and prop(senv, 'pendingRestart') == 'False'
    assert rig.refreshes() == [t for t, argument in rig.stops if argument]


def test_the_request_stays_set_while_the_threads_are_told_to_stop(senv):
    """P3. Service._stop clears the request at its end, after the threads were told to stop: inside the
    joins (a Timer thread's work, on a thread of its own) the property still reads True, the service's
    _interrupt (a Timer task's _que -> _run) still sees the restart, and so do the other modules' stand-in
    services. A second request that lands there merges into this restart."""
    rig = ServiceRig(senv, seconds=600)
    stubs, seen = stub_interrupts(senv), []
    restart = menu_restart(senv)

    def timer_work():
        if len(rig.stops) != 1: return
        seen.append((senv.properties.isPendingRestart(), rig.service._interrupt(),
                     sorted(n for n, f in stubs.items() if f())))
        restart()
    timer = Joined('Timer-chkQUES', timer_work)
    rig.threads.append(timer)
    rig.at(60, restart)
    rig.run()
    assert seen == [(True, True, sorted(STUB_SERVICES))] and timer.errors == []
    assert [message for message, _show, _t in rig.notes] == [RESTARTING, SHUTTING_DOWN]
    assert prop(senv, 'pendingRestart') == 'False'


def test_after_a_restart_the_other_parts_run_again(senv):
    """The review's flag: the modules' stand-in services (builder, jsonrpc, library, multiroom, resources:
    the Manager, autotune, library walks) read 'interrupted' while the request is pending, and no longer
    once the restarted run has begun. On 399744fd they stayed interrupted until Kodi restarted."""
    rig = ServiceRig(senv, seconds=600)
    stubs, samples, restart = stub_interrupts(senv), {}, menu_restart(senv)

    def request():
        restart()
        samples['pending'] = sorted(n for n, f in stubs.items() if f())
    rig.at(60, request)
    rig.at(200, lambda: samples.setdefault('after', sorted(n for n, f in stubs.items() if f())))
    rig.run()
    assert samples == {'pending': sorted(STUB_SERVICES), 'after': []}


def test_a_run_that_ends_for_kodis_close_leaves_the_request_alone(senv, monkeypatch):
    """Service._stop(False): no write of pendingRestart (the class body's False stays)."""
    rig = ServiceRig(senv, seconds=120)
    writes = []
    real = senv.properties.setPendingRestart
    monkeypatch.setattr(senv.properties, 'setPendingRestart', lambda state=True: writes.append(state) or real(state))
    rig.run()
    assert writes == [] and prop(senv, 'pendingRestart') == 'False' and rig.stops == [(rig.closing(), False)]
    assert rig.refreshes() == []                     # Kodi closing: nothing re-armed


def test_a_settings_save_right_before_a_menu_reload_reaches_the_service(senv):
    """The review's case (fix stage). You change settings in the add-on's settings window and press its
    Utility Menu button: Kodi saves the settings as the button closes the window, and the service's
    refresh of them waits 15 s (Monitor._onSettingsChanged under debounceit). You pick 'Force background
    service to reload' 4 s later: Service._stop tells every thread to stop, that timer too. The restart
    re-arms the refresh, so the restarted service takes the saved values: the player's (Seek Tolerance),
    the queue's Enable Executor, and chkSettingsChange's action for a new TCP port (the PVR's address).
    Before the fix nothing ran that refresh again until the next settings save (Discovery's saves no
    longer repeat it, and the queue no longer reads Enable_Executor by itself). The chain is the real one
    (settings_chain); Kodi calls onSettingsChanged once per save, inside the service's wait."""
    rig = ServiceRig(senv, seconds=600)
    monitor, queue = settings_chain(rig, senv), rig.service.priorityQUE
    tolerance, port = rig.player.minDuration, senv.settings.getSettingInt('TCP_PORT')
    assert queue.useExecutor is False

    def save():
        senv.settings.setSettingInt('Seek_Tolerance', tolerance + 7)
        senv.settings.setSettingBool('Enable_Executor', True)
        senv.settings.setSettingInt('TCP_PORT', port + 1)
        for _ in range(3): monitor.onSettingsChanged()
    rig.at(60, save)
    rig.at(64, menu_restart(senv))
    rig.run()
    assert [message for message, _show, _t in rig.notes] == [RESTARTING, SHUTTING_DOWN]
    assert len(rig.starts) == 2 and rig.starts[1] < 60 + 15         # the reload before the first refresh was due
    assert rig.player.minDuration == tolerance + 7 and queue.useExecutor is True
    assert [(name, kwargs) for _t, name, kwargs in rig.actions] == [
        ('setPVRRemote', {'host': 'the host', 'instance': 'the box'})]
    assert rig.starts[1] < rig.actions[0][0] <= rig.starts[1] + 15 + SERVICE_INTERVAL
    assert [t for t in rig.threads if isinstance(t, RigTimer)] == []


def test_a_re_arm_that_fails_is_written_and_the_restart_goes_on(senv):
    """Service._stop runs outside the loop's guard: a failure to re-arm the settings refresh (here a thread
    that can't be started) is written at ERROR and the restart goes on as usual (fix stage)."""
    rig = ServiceRig(senv, seconds=600)

    def cannot():
        raise RuntimeError("can't start new thread")
    rig.service.monitor._onSettingsChanged = cannot
    rig.at(60, menu_restart(senv))
    rig.run()
    assert [message for message, _show, _t in rig.notes] == [RESTARTING, SHUTTING_DOWN] and len(rig.starts) == 2
    lines = [msg for msg, level in senv.lines if level == ERROR and 'settings refresh was not re-armed' in msg]
    assert len(lines) == 1 and lines[0].startswith(PREFIX + "Service: _stop, the settings refresh was not re-armed: "
                                                           "can't start new thread\nTraceback")


def test_the_import_time_clear_stays():
    """services.Service's class body still writes the request False when services.py is imported, and its
    class attributes are the ones the rig copies. Fix stage: right after the request, it clears the
    Kodi-restart marker too (the rig does both), with the release named."""
    service = [n for n in lib_source('services.py')[1].body if isinstance(n, ast.ClassDef) and n.name == 'Service'][0]
    body = [ast.unparse(n) for n in service.body if isinstance(n, ast.Assign)]
    assert 'pendingRestart = PROPERTIES.setPendingRestart(False)' in body
    assert "kodiRestartNoted = ''" in body and 'channels = []' in body
    names = sorted(t.id for n in service.body if isinstance(n, ast.Assign) for t in n.targets)
    assert names == ['channels', 'isClient', 'isPaused', 'isPlaying', 'isScanning', 'jsonQue', 'kodiRestartNoted',
                     'logoQue', 'pendingInterrupt', 'pendingRestart', 'pendingShutdown', 'pendingSuspend', 'postQue']
    statements = [ast.unparse(n) for n in service.body if not isinstance(n, ast.FunctionDef)]
    at = statements.index('pendingRestart = PROPERTIES.setPendingRestart(False)')
    assert statements[at + 1] == "PROPERTIES.setPendingKodiRestart('')"
    assert [ast.unparse(n) for n in service.body if isinstance(n, ast.Expr)] == ["PROPERTIES.setPendingKodiRestart('')"]
    clear = [n for n in service.body if isinstance(n, ast.Expr)][0]
    above = lib_source('services.py')[0].splitlines()[clear.lineno - 7:clear.lineno - 1]
    assert any('imports.71' in line for line in above)


def _pause_samples(env, rig, stubs, every=60, until=None):
    """Every `every` s: (the request, the marker, the service's own pendingInterrupt, the stand-in services
    that would NOT pause). The shared pendingInterrupt property is left out on purpose: any
    PROPERTIES.interruptActivity() ends by writing it False, and nothing relies on it alone."""
    samples = []
    for when in range(every, int(until or rig.closing()) - 1, every):
        rig.at(when, lambda: samples.append((prop(env, 'pendingRestart'), prop(env, 'pendingKodiRestart'),
                                             rig.service.pendingInterrupt,
                                             tuple(sorted(n for n, f in stubs.items() if not f())))))
    return samples


def other_writer(env):
    """What an imports sync's commit (or any other PROPERTIES.interruptActivity() user) does to the shared
    flags meanwhile: interruptActivity, entered and left, writes pendingInterrupt True, then False."""
    def run():
        with env.properties.interruptActivity(): pass
    return run


def pause_lines(env):
    """The add-on's lines about the pause, as kodi.log gets them: (text, level)."""
    return [(msg, level) for msg, level in env.lines if '_kodiRestart' in msg]


@pytest.mark.parametrize('action, reason, note', [(recover, 'restore', 30910), (move_folder, 'folder', 30911)])
def test_a_restore_or_a_folder_move_pauses_the_service_until_kodi_restarts(senv, action, reason, note):
    """P5/P6. 'Restore channels' (the real Backup.recoverChannels) or a new data folder (the real
    Tasks.setUserPath) one minute in, then half an hour: one note, 'Restart Kodi to finish ...' (its own
    text for each), shown even with 'Notifications During Playback' off, and one line Kodi's log keeps
    with debug logging off (fix stage); no 'Restarting background service...', no Service._stop until
    Kodi closes, no task queued or run after the request, no settings refresh re-armed; the request and the
    marker stay set, the service's own pendingInterrupt stays set and every module's stand-in service reads
    'interrupted' throughout, also after another module's interruptActivity wrote the shared
    pendingInterrupt property False at 300 s (fix stage: the property itself is not relied on); the loop
    waits its interval each pass (no churn, no spin). 399744fd restarted the service every 20-30 s
    instead, doing nothing, until Kodi restarted."""
    rig = ServiceRig(senv, seconds=60 + 1800)
    stubs = stub_interrupts(senv)
    rig.at(60, action(senv))
    rig.at(300, other_writer(senv))
    samples = _pause_samples(senv, rig, stubs, until=rig.closing())
    rig.run()
    assert [(message, show) for message, show, _t in rig.notes] == [(STRINGS[note], True), (SHUTTING_DOWN, None)]
    assert 60 < rig.notes[0][2] <= 60 + 2 * SERVICE_INTERVAL
    assert pause_lines(senv) == [(PREFIX + 'Service: _kodiRestart, paused until Kodi restarts (%s)' % reason,
                                  KODI_LOG['LOGINFO'])]
    assert rig.starts == [0.0] and rig.stops == [(rig.closing(), True)] and rig.exits == [rig.closing()]
    assert rig.pushes(since=rig.notes[0][2]) == [] and rig.tasks_run(since=60) == [] and rig.refreshes() == []
    assert set(samples[1:]) == {('True', reason, True, ())} and len(samples) == 30
    assert prop(senv, 'pendingInterrupt') == 'False'          # the other writer's; nothing reads it alone
    paused = [t for t, what, _d in rig.timeline if what == 'pass' and t > rig.notes[0][2]]
    assert len(paused) >= 1800 / SERVICE_INTERVAL - 3
    assert all(w == SERVICE_INTERVAL for w in rig.waits()[-len(paused):-1])
    assert prop(senv, 'pendingKodiRestart') == reason and prop(senv, 'pendingRestart') == 'True'


def test_a_menu_restart_during_the_pause_changes_nothing(senv):
    """The pause already waits for a Kodi restart: a 'Force background service to reload' then neither
    restarts the service nor shows the note or writes the line again."""
    rig = ServiceRig(senv, seconds=1200)
    rig.at(60, recover(senv))
    rig.at(600, menu_restart(senv))
    rig.run()
    assert [message for message, _show, _t in rig.notes] == [RESTORED, SHUTTING_DOWN]
    assert len(rig.starts) == 1 and rig.tasks_run(since=60) == []
    assert len(pause_lines(senv)) == 1 and rig.refreshes() == []


@pytest.mark.parametrize('debug', ['false', 'true'])
def test_the_pause_is_written_once_per_reason(monkeypatch, debug):
    """The review's case (fix stage): the box runs without the add-on's debug logging, and the note shows
    for 4 s. The pause writes one INFO line Kodi's log always keeps (logger.notice), with its reason word
    and nothing else, once per reason: a restore, then a folder move during the same pause, give two
    lines; the passes in between give none, with debug logging off or on."""
    env = service_env(monkeypatch, user=dict(USER, Debug_Enable=debug))
    rig = ServiceRig(env, seconds=1800)
    rig.at(60, recover(env))
    rig.at(900, move_folder(env))
    rig.run()
    line = PREFIX + 'Service: _kodiRestart, paused until Kodi restarts (%s)'
    assert pause_lines(env) == [(line % 'restore', KODI_LOG['LOGINFO']), (line % 'folder', KODI_LOG['LOGINFO'])]
    assert [message for message, _show, _t in rig.notes] == [RESTORED, MOVED, SHUTTING_DOWN]


def test_a_restore_while_a_restart_runs_pauses_the_restarted_run(senv):
    """A restore that lands while Service._stop tells the threads to stop for a menu restart: _stop leaves
    the request set (the marker asks for Kodi), so the service restarts once (the menu's) and the
    restarted run pauses, with the restore's note."""
    rig = ServiceRig(senv, seconds=1200)
    restore = recover(senv)
    rig.threads.append(Joined('Timer-1', lambda: restore() if len(rig.stops) == 1 else None))
    rig.at(60, menu_restart(senv))
    rig.run()
    assert [message for message, _show, _t in rig.notes] == [RESTARTING, RESTORED, SHUTTING_DOWN]
    assert len(rig.starts) == 2 and [argument for _t, argument in rig.stops] == [True, True]
    assert rig.tasks_run(since=rig.starts[1]) == [] and prop(senv, 'pendingRestart') == 'True'
    assert rig.refreshes() == []                    # the paused run needs no settings refresh


def test_the_marker_alone_does_not_pause(senv):
    """A marker without a request (no module waits then) doesn't pause the main loop."""
    rig = ServiceRig(senv, seconds=120)
    senv.properties.setPendingKodiRestart('restore')
    rig.run()
    assert len(rig.tasks_run()) > 15 and rig.notes == [(SHUTTING_DOWN, None, rig.closing())]
    assert pause_lines(senv) == []


@pytest.mark.parametrize('reason', ['restore', 'folder'])
def test_a_new_interpreter_drops_a_leftover_marker(senv, reason):
    """The review's case (fix stage). A restore (or a folder move) paused the old service; instead of a Kodi
    restart, Kodi started the service script again in a new interpreter (an add-on update, a reinstall, a
    disable and enable), whose modules read their data paths anew. Its import of services.py (the class
    body, which the rig runs from the source) clears the request as before and the marker with it: the
    service runs, and a later 'Force background service to reload' restarts it once, with no pause and no
    old note. Before the fix that reload paused the service until Kodi restarted, with the old note."""
    senv.properties.setPendingKodiRestart(reason)      # what the old interpreter left in Window(10000)
    senv.properties.setPendingRestart(True)
    rig = ServiceRig(senv, seconds=1200)
    assert prop(senv, 'pendingKodiRestart') == '' and prop(senv, 'pendingRestart') == 'False'
    rig.at(600, menu_restart(senv))
    rig.run()
    assert [message for message, _show, _t in rig.notes] == [RESTARTING, SHUTTING_DOWN]
    assert len(rig.starts) == 2 and pause_lines(senv) == [] and len(rig.refreshes()) == 1
    assert len(rig.tasks_run(since=rig.starts[1])) > 100


@pytest.mark.parametrize('action, reason', [(recover, 'restore'), (move_folder, 'folder')])
def test_the_marker_is_set_before_the_request(senv, monkeypatch, action, reason):
    """Whoever sees the request sees the marker too: a main loop that read the request between the two
    writes would restart once before it paused."""
    senv.properties.monitor = quiet_monitor()
    order = []
    marker, request = senv.properties.setPendingKodiRestart, senv.properties.setPendingRestart
    monkeypatch.setattr(senv.properties, 'setPendingKodiRestart',
                        lambda why: order.append(('marker', why)) or marker(why))
    monkeypatch.setattr(senv.properties, 'setPendingRestart',
                        lambda state=True: order.append(('request', state)) or request(state))
    action(senv)()
    assert order == [('marker', reason), ('request', True)]


def test_only_a_new_interpreter_clears_the_marker():
    """The marker's key is in kodi.py's two helpers alone; backup.py ('restore') and tasks.py ('folder') set
    it; the service reads it in _kodiRestart and _stop; the one clear is services.Service's class body (a
    new interpreter's import, fix stage), none while a service runs; the reasons are the two _kodiRestart
    has a note for."""
    uses, sets, reads = [], [], []
    for name, text in library_sources().items():
        src = _File(text)
        for node in ast.walk(src.tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and 'pendingKodiRestart' in node.value:
                uses.append((name, src.around(node)[0]))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == 'setPendingKodiRestart':
                    sets.append((name, src.around(node)[0], [ast.unparse(a) for a in node.args]))
                elif node.func.attr == 'getPendingKodiRestart':
                    reads.append((name, src.around(node)[0]))
    assert sorted(uses) == [('kodi.py', 'Properties.getPendingKodiRestart'),
                            ('kodi.py', 'Properties.setPendingKodiRestart')]
    assert sorted(sets) == [('backup.py', 'Backup.recoverChannels', ["'restore'"]),
                            ('services.py', 'Service', ["''"]),
                            ('tasks.py', 'Tasks.setUserPath', ["'folder'"])]
    assert sorted(reads) == [('services.py', 'Service._kodiRestart'), ('services.py', 'Service._stop')]
    pause = kodi_faithful.method_node('services.py', 'Service', '_kodiRestart')
    notes = [n for n in ast.walk(pause) if isinstance(n, ast.Dict)]
    assert len(notes) == 1 and [k.value for k in notes[0].keys] == ['restore', 'folder']
    assert [v.value for v in notes[0].values] == [30910, 30911]


def test_the_two_notes():
    """New strings in the add-on's own range (30000-30999), after imports.62's 30909, each once in
    strings.po and used only by Service._kodiRestart: nothing else in the add-on (code, settings.xml,
    addon.xml, the skins) names 30910 or 30911."""
    assert RESTORED == 'Restart Kodi to finish restoring the channel backup'
    assert MOVED == 'Restart Kodi to finish moving the data folder'
    with open(kodi_faithful.STRINGS_PO, encoding='utf-8') as fh: po = fh.read()
    assert po.count('msgctxt "#30910"') == po.count('msgctxt "#30911"') == 1
    assert po.index('msgctxt "#30909"') < po.index('msgctxt "#30910"') < po.index('msgctxt "#30911"')
    found = []
    for folder, _dirs, files in os.walk(ADDON):
        if os.sep + 'tests' in folder + os.sep or '__pycache__' in folder: continue
        for name in files:
            if not name.endswith(('.py', '.xml', '.po', '.html', '.js')): continue
            path = os.path.join(folder, name)
            with open(path, encoding='utf-8', errors='replace') as fh: text = fh.read()
            for number in ('30910', '30911'):
                if number in text: found.append((os.path.relpath(path, ADDON), number, text.count(number)))
    po_path = os.path.join('resources', 'language', 'resource.language.en_gb', 'strings.po')
    services = os.path.join('resources', 'lib', 'services.py')
    assert sorted(found) == [(po_path, '30910', 1), (po_path, '30911', 1), (services, '30910', 1),
                             (services, '30911', 1)]


# === the code ======================================================================================

def _try_in(cls, name):
    node = kodi_faithful.method_node('services.py', cls, name)
    loops = [n for n in ast.walk(node) if isinstance(n, ast.While)]
    assert len(loops) == 1 and len(loops[0].body) == 1 and isinstance(loops[0].body[0], ast.Try), (cls, name)
    return loops[0].body[0]


@pytest.mark.parametrize('cls, name, guard, wait', [
    ('Service', '_start', 'self._passFailed(e)', 'if self._shutdown():\n    break'),
    ('Monitor', '_chkIdle', 'self._tickFailed(e)', 'if self.service._shutdown(0.5):\n    break'),
])
def test_each_loop_turn_is_one_guarded_try(cls, name, guard, wait):
    """The whole turn is inside the try; one handler, `except Exception as e`; it writes, then waits the
    loop's own wait (Kodi closing ends that wait and the loop). No finally, no else."""
    node = _try_in(cls, name)
    assert len(node.handlers) == 1 and not node.orelse and not node.finalbody
    handler = node.handlers[0]
    assert ast.unparse(handler.type) == 'Exception' and handler.name == 'e'
    assert [ast.unparse(s) for s in handler.body] == [guard, wait]


def test_the_pass_keeps_its_steps_and_pauses_before_it_restarts():
    node = _try_in('Service', '_start')
    assert [ast.unparse(s) for s in node.body[:3]] == ['self.isScanning = BUILTIN.isScanning()',
                                                       'self.isPaused = BUILTIN.isPaused()',
                                                       'self.isPlaying = self.player.isPlaying()']
    chain = ast.unparse(node.body[3])
    assert chain.startswith('if self._shutdown() and self._interrupt() and self._sleep():\n    break\n'
                            'elif self._restart() and self._kodiRestart():\n    pass\n'
                            'elif self.pendingRestart and self._interrupt() and self._sleep():\n    break\nelse:\n'
                            '    self._tasks()'), chain


def test_the_request_is_cleared_last_in_stop():
    """The clear, then (fix stage) the settings refresh re-armed, its failure written, never raised."""
    node = kodi_faithful.method_node('services.py', 'Service', '_stop')
    assert [ast.unparse(s) for s in node.body[-2:]] == [
        'if pendingRestart and (not PROPERTIES.getPendingKodiRestart()):\n'
        '    self.pendingRestart = PROPERTIES.setPendingRestart(False)\n'
        '    try:\n'
        '        self.monitor._onSettingsChanged()\n'
        '    except Exception as e:\n'
        "        self.log('_stop, the settings refresh was not re-armed: %s' % e, xbmc.LOGERROR)",
        'return pendingRestart']


CHANGED = [('services.py', 'Service', '_start'), ('services.py', 'Service', '_passFailed'),
           ('services.py', 'Service', '_kodiRestart'), ('services.py', 'Service', '_stop'),
           ('services.py', 'Service', '__init__'), ('services.py', 'Monitor', '__init__'),
           ('services.py', 'Monitor', '_chkIdle'), ('services.py', 'Monitor', '_tickFailed'),
           ('kodi.py', 'Properties', 'setPendingKodiRestart'), ('kodi.py', 'Properties', 'getPendingKodiRestart'),
           ('backup.py', 'Backup', 'recoverChannels'), ('tasks.py', 'Tasks', 'setUserPath')]


def _lines_of(filename, node):
    """The whole source lines of `node`, comments at their ends included."""
    return '\n'.join(lib_source(filename)[0].splitlines()[node.lineno - 1:node.end_lineno])


@pytest.mark.parametrize('filename, cls, name', CHANGED)
def test_each_changed_spot_names_the_release(filename, cls, name):
    assert 'imports.71' in _lines_of(filename, kodi_faithful.method_node(filename, cls, name))


def test_the_changed_files_are_python38_and_their_bitor_reviewed():
    from test_imports63_clock import py39_api_leaks
    from test_imports69_importcfg_helpers import BITOR_REVIEWED, bitor_lines
    for filename in ('services.py', 'kodi.py', 'backup.py', 'tasks.py', 'loopguard_helpers.py'):
        text = lib_source(filename)[0]
        ast.parse(text, feature_version=(3, 8))
        assert py39_api_leaks(text) == [], filename
        assert bitor_lines(text) == sorted(BITOR_REVIEWED[filename]), filename
    for filename, cls, name in CHANGED:
        node = kodi_faithful.method_node(filename, cls, name)
        bitor = [ast.unparse(n) for n in ast.walk(node)
                 if isinstance(n, (ast.BinOp, ast.AugAssign)) and isinstance(n.op, ast.BitOr)]
        assert bitor == ([] if name != '_chkIdle' else ['bool(self.idleTime) | self.idleTime']), (filename, cls, name)
