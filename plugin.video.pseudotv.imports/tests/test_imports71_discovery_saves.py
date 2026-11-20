# -*- coding: utf-8 -*-
"""imports.71: no needless settings saves; the channel list refreshed on its own timer.

Discovery (server.py) saved ZeroConf_Status twice every ~10.5 minutes: 'Listening' (string 32252) when
its 30 s search began, then Online or Offline. About 274 saves a day, effectively the add-on's only
routine ones, and each made Kodi rebuild the service's REAL_SETTINGS in place (the window of the
2026-06-05, 2026-10-04 and 2026-11-07 crashes) and queue onSettingsChanged. Now 'Listening' is not saved,
and the final status goes through kodi.py's Settings._setSetting, which writes a value only when it
changed: in steady state Discovery saves nothing.

Those saves had a side job: each ran Monitor._updateServiceSettings, the only periodic refresh of the
service's channel list (Service.channels), which Player.getplayingItem lays over the playing Custom
channel's guide entry (its rules, logo and name). Tasks.chkServiceChannels refreshes it now every 5
minutes on a timer of its own (chkQueTimer), host and client, the old pace.

Kodi is kodi_faithful's: the settings (FaithfulAddons), Window(10000)'s properties (FaithfulWindows) and
the monitor's waits on a rig clock (FaithfulMonitor through FaithfulService). The code under test is the
add-on's own, compiled from the source or imported: Discovery.run, Service._sleep, the main loop
(Service._start and what it calls) and the queue (cqueue.py), Tasks.chkQueTimer / _chkEpochTimer /
chkServiceChannels, Player.getplayingItem, Multiroom._chkZeroConf, kodi.py's Settings and Properties.
"""
import ast
import collections
import copy
import functools
import types

import pytest

import loopguard_helpers
from kodi_faithful import (KODI_LOG, SETTING_TYPES, FaithfulService, FaithfulWindows, compile_method, install_windows,
                           lib_constant, lib_source, lib_strings)
from test_imports70_shutdown_contract import _File
from test_imports70_shutdown_contract import library_sources as _library_sources
from test_imports71_settings_reads import make_env, monitor_rig, quiet

STRINGS           = lib_strings()
ONLINE            = STRINGS[32211] % ('green', STRINGS[32158])
OFFLINE           = STRINGS[32211] % ('red', STRINGS[32253])
LISTENING         = '[COLOR=yellow][B]%s[/B][/COLOR]' % STRINGS[32252]
CPU_CYCLE         = lib_constant('constants.py', 'CPU_CYCLE')
DISCOVER_INTERVAL = lib_constant('constants.py', 'DISCOVER_INTERVAL')
SERVICE_INTERVAL  = lib_constant('constants.py', 'SERVICE_INTERVAL')
ERROR             = KODI_LOG['LOGERROR']


def _server_text(name):
    """A plain text constant at server.py's top level (server.py starts a thread on import: not imported)."""
    found = [node.value.value for node in lib_source('server.py')[1].body if isinstance(node, ast.Assign)
             and [ast.unparse(t) for t in node.targets] == [name] and isinstance(node.value, ast.Constant)]
    assert len(found) == 1, name
    return found[0]


ZEROCONF_SERVICE = _server_text('ZEROCONF_SERVICE')


@functools.lru_cache(maxsize=1)
def library_sources():
    """The library's sources, read once for this module's scans (not to be changed)."""
    return _library_sources()


def kind(text):
    return {ONLINE: 'Online', OFFLINE: 'Offline', LISTENING: 'Listening'}.get(text, text)


@pytest.fixture
def denv(monkeypatch):
    """make_env's settings, plus Window(10000) as Kodi keeps it and a kodi.Properties built over it."""
    import xbmcgui
    import kodi
    env = make_env(monkeypatch)
    env.windows = FaithfulWindows()
    install_windows(monkeypatch, env.windows, xbmcgui)
    env.properties = kodi.Properties()
    env.monkeypatch = monkeypatch
    return env


def test_window_properties_follow_kodi():
    """K9. interfaces/legacy/Window.cpp:605-634: one property map per window id, shared by every Window
    object; keys lower-cased; '' is a clear. interfaces/python/swig.cpp:95-141: a value is text (None is
    '', bytes are decoded); a number or a bool raises TypeError. K10's other half: the repo-root stub
    stores nothing. Fix stage: each read is a new str (interfaces/python/typemaps/python.string.outtm:10-11
    decodes Kodi's copy on every call), equal to the last but never the same object."""
    windows = FaithfulWindows()
    one, two = windows.Window(10000), windows.Window(10000)
    one.setProperty('Plugin.Video.X.Running', 'True')
    assert two.getProperty('plugin.video.x.running') == 'True'
    first, again = one.getProperty('plugin.video.x.running'), two.getProperty('plugin.video.x.running')
    assert first == again and first is not again
    assert windows.Window(10131).getProperty('plugin.video.x.running') == ''
    one.setProperty('k', None)
    assert two.getProperty('k') == ''
    one.setProperty('k', b'bytes')
    assert two.getProperty('K') == 'bytes'
    for bad in (1, True, 1.5, ['x']):
        with pytest.raises(TypeError, match='must be unicode or str'): one.setProperty('k', bad)
    one.clearProperty('K')
    assert two.getProperty('k') == ''
    import xbmcgui
    stub = xbmcgui.Window(10000)
    stub.setProperty('k', 'x')
    assert stub.getProperty('k') == ''


# === Discovery =======================================================================================

class DiscoveryRig(object):
    """Discovery.run (server.py, compiled) on a rig clock. Its service is FaithfulService (the REAL
    _shutdown over a FaithfulMonitor) with the REAL Service._sleep; SETTINGS is kodi.py's Settings over
    the registry (compare-before-write as on the box); PROPERTIES is kodi.py's Properties over
    FaithfulWindows (the single-loop guard as on the box). Zeroconf, ServiceBrowser and the listener
    record what they are asked; multiroom._getStatus answers Kodi's services.zeroconf from `statuses`,
    one per cycle; Kodi closes 1 s into the last cycle's 10-minute pause (or at `close_at`).
    `cycle`: Service._sleep's poll step (constants.CPU_CYCLE by default; a coarser one only to keep a
    50-cycle run short: the loop is the same, it polls less often).
    `timeline`: (rig time, what) for every status read, Zeroconf(), browse, close, _sleep and save."""

    def __init__(self, env, statuses, cycle=CPU_CYCLE, close_at=None, close_error=None):
        self.env, self.t, self.violations = env, 0.0, []
        self.statuses, self.closing_at, self.close_error = list(statuses), close_at, close_error
        self.timeline = []
        rig = self
        service = FaithfulService(self.advance, lambda: self.t, lambda: self.closing_at, lambda: False, self.violations)
        service.pendingInterrupt = False
        real_sleep = types.MethodType(compile_method('services.py', 'Service', '_sleep', CPU_CYCLE=cycle), service)

        def sleep(wait=1.0):
            rig.note('sleep %s' % wait)
            return real_sleep(wait)
        service._sleep = sleep

        class Zeroconf(object):
            def __init__(self):
                rig.note('Zeroconf()')

            def close(self):
                rig.note('close')
                if rig.close_error is not None: raise rig.close_error

        class Listener(object):
            def __init__(self, multiroom=None):
                self.multiroom = multiroom

        def ServiceBrowser(zconf, kind_, listener):
            assert isinstance(zconf, Zeroconf) and isinstance(listener, Listener)
            rig.note('browse %s' % kind_)

        real_save = env.reg._save

        def save(writer):
            done = real_save(writer)
            if done: rig.note('save %s' % kind(env.reg.disk.get('ZeroConf_Status', '')))
            return done
        env.monkeypatch.setattr(env.reg, '_save', save)
        self.disco = types.SimpleNamespace(service=service, monitor=service.monitor, MyListener=Listener, logged=[],
                                           multiroom=types.SimpleNamespace(_getStatus=self.status))
        self.disco.log = lambda msg, level=KODI_LOG['LOGDEBUG']: self.disco.logged.append((msg, level))
        g = dict(PROPERTIES=env.properties, SETTINGS=env.settings, Zeroconf=Zeroconf, ServiceBrowser=ServiceBrowser,
                 ZEROCONF_SERVICE=ZEROCONF_SERVICE, LANGUAGE=STRINGS.__getitem__, DISCOVER_INTERVAL=DISCOVER_INTERVAL,
                 xbmc=types.SimpleNamespace(LOGERROR=ERROR))
        self.run = types.MethodType(compile_method('server.py', 'Discovery', 'run', **g), self.disco)

    def advance(self, seconds):
        self.t += seconds

    def note(self, what):
        self.timeline.append((self.t, what))

    def status(self):
        zcons = self.statuses.pop(0)
        self.note('status %s' % zcons)
        if not self.statuses and self.closing_at is None:
            self.closing_at = self.t + (DISCOVER_INTERVAL if zcons else 0) + 1
        return zcons

    def events(self):
        return [what for _t, what in self.timeline]

    def saved(self):
        return [what[len('save '):] for what in self.events() if what.startswith('save ')]


def test_steady_state_makes_no_save(denv):
    """D1. ZeroConf on and Online already stored: 50 cycles (~8.75 h) without a single save, so Kodi
    never rebuilds REAL_SETTINGS or queues onSettingsChanged for it. 399744fd saved 100 times here."""
    denv.reg.disk['ZeroConf_Status'] = ONLINE
    denv.reg.monitor(types.SimpleNamespace(onSettingsChanged=lambda: None))   # the service's Monitor
    rig = DiscoveryRig(denv, [True] * 50, cycle=1.0)
    rig.run()
    assert rig.events().count('browse %s' % ZEROCONF_SERVICE) == 50
    assert denv.reg.saves == [] and denv.reg.pending() == 0
    assert denv.reg.disk['ZeroConf_Status'] == ONLINE
    assert rig.violations == []


def test_a_leftover_listening_is_overwritten_once(denv):
    """The status 399744fd left behind (a search interrupted after its 'Listening' save) is replaced by
    Online at the first cycle; then nothing is saved."""
    denv.reg.disk['ZeroConf_Status'] = LISTENING
    rig = DiscoveryRig(denv, [True] * 5, cycle=1.0)
    rig.run()
    assert rig.saved() == ['Online'] and denv.reg.saves == [(denv.reg.saves[0][0], ['ZeroConf_Status'])]


def test_zeroconf_switched_off_saves_offline_once(denv):
    denv.reg.disk['ZeroConf_Status'] = ONLINE
    rig = DiscoveryRig(denv, [False] * 5, cycle=1.0)
    rig.run()
    assert rig.saved() == ['Offline'] and len(denv.reg.saves) == 1
    assert 'Zeroconf()' not in rig.events()


@pytest.mark.parametrize('stored, statuses, saved', [
    (None, [True, True, True], ['Online']),                         # nothing stored: the default, Offline
    (ONLINE, [True, False, True], ['Offline', 'Online']),
    (OFFLINE, [False, False, True, True, False], ['Online', 'Offline']),
])
def test_only_real_changes_are_saved(denv, stored, statuses, saved):
    """D3."""
    if stored is not None: denv.reg.disk['ZeroConf_Status'] = stored
    rig = DiscoveryRig(denv, statuses, cycle=1.0)
    rig.run()
    assert rig.saved() == saved and len(denv.reg.saves) == len(saved)


@pytest.mark.parametrize('stored, statuses', [(ONLINE, [True] * 3), (OFFLINE, [True, False, True]),
                                              (LISTENING, [False, True])])
def test_listening_is_never_saved(denv, stored, statuses):
    """D2. No saved value is 'Listening' (32252), and no save falls inside a search (between its browse
    and its close)."""
    denv.reg.disk['ZeroConf_Status'] = stored
    rig = DiscoveryRig(denv, statuses, cycle=1.0)
    rig.run()
    assert 'Listening' not in rig.saved()
    assert all(STRINGS[32252] not in value for value in [denv.reg.disk.get('ZeroConf_Status', '')])
    searching = False
    for what in rig.events():
        if what.startswith('browse'): searching = True
        elif what == 'close': searching = False
        assert not (searching and what.startswith('save')), rig.events()


def test_discovery_runs_as_before(denv):
    """D4. With the real CPU_CYCLE: an 'on' cycle makes one Zeroconf(), one ServiceBrowser on
    ZEROCONF_SERVICE with a listener, waits DISCOVER_INTERVAL (30 s), closes, writes the status, then
    pauses 600 s through Service._sleep; an 'off' cycle only writes the status and pauses."""
    denv.reg.disk['ZeroConf_Status'] = ONLINE
    rig = DiscoveryRig(denv, [True, False, True])
    rig.run()
    browse = 'browse %s' % ZEROCONF_SERVICE
    assert rig.events() == ['status True', 'Zeroconf()', browse, 'sleep 30', 'close', 'sleep 600',
                            'status False', 'save Offline', 'sleep 600',
                            'status True', 'Zeroconf()', browse, 'sleep 30', 'close', 'save Online', 'sleep 600']
    at = {}
    for t, what in rig.timeline: at.setdefault(what, []).append(t)
    for opened, closed in zip(at[browse], at['close']):
        assert abs(closed - opened - DISCOVER_INTERVAL) <= 2 * CPU_CYCLE
    starts = at['status True'] + at['status False']
    starts.sort()
    assert [round(b - a) for a, b in zip(starts, starts[1:])] == [630, 600]
    assert ('run, _sleep', ERROR) in rig.disco.logged                     # Kodi closing in the pause, as before
    assert rig.disco.logged[-1] == ('run, shutting down...', KODI_LOG['LOGDEBUG'])


def test_kodi_closing_during_the_search_saves_nothing(denv):
    """D5. Kodi closes 10 s into the search: the loop ends there. 399744fd had saved 'Listening' already
    (and left it stored). The search's Zeroconf is not closed on this path: pre-existing, left alone."""
    denv.reg.disk['ZeroConf_Status'] = ONLINE
    rig = DiscoveryRig(denv, [True, True], close_at=10.0)
    rig.run()
    assert rig.events() == ['status True', 'Zeroconf()', 'browse %s' % ZEROCONF_SERVICE, 'sleep 30']
    assert denv.reg.saves == [] and denv.reg.disk['ZeroConf_Status'] == ONLINE


def test_a_network_failure_ends_the_loop_and_keeps_the_status(denv):
    """D6. close() failing (the network went away) ends the loop with its ERROR line, as before; no
    save, so the status keeps its value (399744fd left 'Listening' stored)."""
    denv.reg.disk['ZeroConf_Status'] = ONLINE
    rig = DiscoveryRig(denv, [True, True], close_error=OSError(19, 'No such device'))
    rig.run()
    assert rig.events()[-1] == 'close' and denv.reg.saves == []
    assert ('run, Multicast DNS Service failed! [Errno 19] No such device', ERROR) in rig.disco.logged
    assert denv.reg.disk['ZeroConf_Status'] == ONLINE


def test_one_discovery_loop_at_a_time(denv):
    """D7. The Window(10000) running flag (kodi.py Properties.chkRunning) still guards the loop: while it
    is set a second run returns at once; a run clears it when it ends."""
    denv.properties.setRunning('Discovery.run', True)
    rig = DiscoveryRig(denv, [True])
    rig.run()
    assert rig.timeline == []
    denv.properties.setRunning('Discovery.run', False)
    rig = DiscoveryRig(denv, [False], cycle=1.0)
    rig.run()
    assert rig.events()[0] == 'status False' and denv.properties.isRunning('Discovery.run') is False
    assert denv.windows.get('plugin.video.pseudotv.imports.Discovery.run.Running') == 'False'


def test_discovery_writes_the_status_once_per_cycle():
    """D8 (the source). One settings write in Discovery.run: the Online/Offline status through
    SETTINGS.setSetting; string 32252 no longer there; the release named where the line went."""
    node = None
    for item in lib_source('server.py')[1].body:
        if isinstance(item, ast.ClassDef) and item.name == 'Discovery':
            node = [n for n in item.body if isinstance(n, ast.FunctionDef) and n.name == 'run'][0]
    writes = [ast.unparse(n) for n in ast.walk(node) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
              and n.func.attr.startswith('setSetting')]
    assert writes == ["SETTINGS.setSetting('ZeroConf_Status', LANGUAGE(32211) % ({True: 'green', False: 'red'}[zcons], "
                      "{True: LANGUAGE(32158), False: LANGUAGE(32253)}[zcons]))"]
    assert [n for n in ast.walk(node) if isinstance(n, ast.Constant) and n.value == 32252] == []
    assert 'imports.71' in ast.get_source_segment(lib_source('server.py')[0], node)


def test_the_zeroconf_status_button_reads_what_discovery_stores(denv):
    """The real Multiroom._chkZeroConf (the 'ZeroConf Status' button) is unchanged: it offers to switch
    Kodi's ZeroConf on when the stored status is the red Offline Discovery writes (the settings.xml
    default too), and says it is already on otherwise."""
    asked, told = [], []
    dialog = types.SimpleNamespace(yesnoDialog=lambda message='', **kw: asked.append(message) or False,
                                   notificationDialog=lambda message='', **kw: told.append(message))
    button = types.SimpleNamespace(log=lambda *a, **k: None, jsonRPC=None, service=None)
    press = types.MethodType(compile_method('multiroom.py', 'Multiroom', '_chkZeroConf', SETTINGS=denv.settings,
                                            LANGUAGE=STRINGS.__getitem__, DIALOG=dialog,
                                            BUILTIN=types.SimpleNamespace(getInfoLabel=lambda *a: '')), button)
    assert OFFLINE == SETTING_TYPES['ZeroConf_Status'][1]
    press()                                                                     # nothing stored: the default
    assert (asked, told) == ([STRINGS[30129]], [])
    DiscoveryRig(denv, [True], cycle=1.0).run()
    press()
    assert told == [STRINGS[32219] % STRINGS[30034]]
    denv.properties.setRunning('Discovery.run', False)
    DiscoveryRig(denv, [False], cycle=1.0).run()
    press()
    assert asked == [STRINGS[30129]] * 2


# === who writes the add-on's settings ====================================================================

SETTERS = ('setSetting', 'setSettingBool', 'setSettingInt', 'setSettingNumber', 'setSettingString', 'setSettingList',
           'setSettingDict', 'setSettingBoolList', 'setSettingIntList', 'setSettingNumberList', 'setSettingFloat')
LOOPS   = (ast.For, ast.While, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


def setter_calls(sources):
    """[(file, owner, receiver, method, key, literal, the loop around it or None)] of every settings write
    (key: the id when it is a literal, else the expression's text)."""
    found = []
    for name, text in sources.items():
        src = _File(text)
        for node in ast.walk(src.tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute): continue
            if node.func.attr not in SETTERS: continue
            loop, up = None, node
            while up in src.parent:
                up = src.parent[up]
                if isinstance(up, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)): break
                if isinstance(up, LOOPS):
                    loop = up
                    break
            first = node.args[0] if node.args else None
            literal = isinstance(first, ast.Constant) and isinstance(first.value, str)
            key = first.value if literal else (ast.unparse(first) if first is not None else '')
            found.append((name, src.around(node)[0], ast.unparse(node.func.value), node.func.attr, key, literal, loop))
    return found


DASHBOARD = "the dashboard's settings save: one write per changed key"
# Reviewed 2026-11-20 (imports.71): the writes that sit inside a loop. Each goes through kodi.py's
# Settings._setSetting, which saves only a changed value, so none repeats a save by itself.
LOOP_REVIEWED = {
    ('server.py', 'Discovery.run', 'setSetting', 'ZeroConf_Status'): 'once per ~10.5 min cycle; saved only on a change',
    ('server.py', 'HTTP.run', 'setSetting', 'Remote_NAME'): 'HTTP server start or a new LAN address',
    ('server.py', 'HTTP.run', 'setSetting', 'Remote_M3U'): 'HTTP server start or a new LAN address',
    ('server.py', 'HTTP.run', 'setSetting', 'Remote_XMLTV'): 'HTTP server start or a new LAN address',
    ('server.py', 'HTTP.run', 'setSetting', 'Remote_GENRE'): 'HTTP server start or a new LAN address',
    ('server.py', 'MyHandler.do_POST', 'setSettingBool', 'key'): DASHBOARD,
    ('server.py', 'MyHandler.do_POST', 'setSettingInt', 'key'): DASHBOARD,
    ('server.py', 'MyHandler.do_POST', 'setSetting', 'key'): DASHBOARD,
    ('server.py', 'MyHandler.do_POST', 'setSettingString', 'key'): DASHBOARD,
    ('autotune.py', 'Autotune._runTune', 'setSettingBool', 'Autotuned_Channels'): 'an autotune the user started',
    ('context_create.py', '_auto', 'setSettingBool', 'Enable_Autotuned_Channels'): 'a context-menu action',
    ('kodi.py', 'Settings.setPVRInstanceSettings', 'setSetting', 'setting'): "pvr.iptvsimple's, not this add-on's",
}


def test_writes_inside_loops_are_reviewed():
    """G4 (lexical: a write reached from a loop through a call or an alias is not seen). A new write in a
    loop fails here until reviewed; Discovery.run keeps one."""
    found = collections.Counter((f, owner, method, key) for f, owner, _r, method, key, _lit, loop
                                in setter_calls(library_sources()) if loop is not None)
    assert found == {key: 1 for key in LOOP_REVIEWED}
    assert all(LOOP_REVIEWED.values())


def test_no_loop_writes_one_setting_twice():
    """G4b. The pattern of 399744fd's Discovery ('Listening', then Online/Offline, in one loop body): two
    writes of the same literal id in one loop make a save each time round whatever the compare."""
    assert twice_in_a_loop(library_sources()) == []


def twice_in_a_loop(sources):
    twice = collections.Counter()
    for f, owner, _r, _m, key, literal, loop in setter_calls(sources):
        if loop is not None and literal: twice[(f, owner, id(loop), key)] += 1
    return sorted((f, owner, key) for (f, owner, _loop, key), n in twice.items() if n > 1)


@pytest.mark.parametrize('snippet, found', [
    ("while True:\n    SETTINGS.setSetting('A', 1)\n    SETTINGS.setSetting('A', 2)", 2),
    ("for x in y:\n    SETTINGS.setSettingBool('A', x)", 1),
    ("[SETTINGS.setSetting(k, v) for k, v in pairs]", 1),
    ("def f():\n    while True:\n        g = lambda: SETTINGS.setSetting('A', 1)", 0),
    ("SETTINGS.setSetting('A', 1)", 0),
])
def test_the_loop_scan_sees_writes_in_loops(snippet, found):
    """G7 for G4 and G4b."""
    assert len([c for c in setter_calls({'x.py': snippet}) if c[-1] is not None]) == found
    assert twice_in_a_loop({'x.py': snippet}) == ([('x.py', '<module>', 'A')] if found == 2 else [])


WRAPPER_REVIEWED = {('kodi.py', 'Settings.setEXTSetting', 'xbmcaddon.Addon(id)'): "another add-on's settings",
                    ('kodi.py', 'Settings.setPVRInstanceSettings', 'addon'): "pvr.iptvsimple's instance settings"}


def test_the_addons_settings_are_written_only_through_the_wrapper():
    """G5. Every write of this add-on's settings goes through kodi.py's Settings (SETTINGS, or the
    wrapper's own helpers), which compares before it saves. A raw setter on REAL_SETTINGS or on a new
    xbmcaddon.Addon of this add-on would save on every call."""
    odd = collections.Counter()
    for f, owner, receiver, _method, _key, _literal, _loop in setter_calls(library_sources()):
        if receiver == 'SETTINGS' or (f == 'kodi.py' and owner.startswith('Settings.') and receiver == 'self'): continue
        odd[(f, owner, receiver)] += 1
    assert set(odd) == set(WRAPPER_REVIEWED)


def test_the_wrapper_compares_before_it_saves(denv):
    """G6. kodi.py Settings over Kodi's settings: the same value (in any case) saves nothing, a new value
    saves once; the typed setters too."""
    settings, reg = denv.settings, denv.reg
    reg.disk['ZeroConf_Status'] = ONLINE
    settings.setSetting('ZeroConf_Status', ONLINE)
    settings.setSetting('ZeroConf_Status', ONLINE.upper())
    settings.setSettingBool('Enable_Executor', False)
    settings.setSettingInt('RPC_Wait', 120)
    assert reg.saves == []
    settings.setSetting('ZeroConf_Status', OFFLINE)
    settings.setSettingBool('Enable_Executor', True)
    settings.setSettingInt('RPC_Wait', 121)
    assert [changed for _writer, changed in reg.saves] == [['ZeroConf_Status'], ['Enable_Executor'], ['RPC_Wait']]


# === the service's channel list ==========================================================================

T0 = 1800000000.0          # the rig's epoch (2027-01-15 08:00 UTC): _chkEpochTimer counts whole seconds
CHID = 'a1b2c3d4e5f60718293a4b5c6d7e8f90@PseudoTV_Live'


class InlineThread(object):
    """cqueue's Thread for the loop rig: start() works the target through at once on this thread (the
    pop thread emptying the queue right away, as an idle one does), is_alive() is True only meanwhile.
    _worker is a generator function: calling it runs nothing, as in a thread."""

    def __init__(self, target=None, *args, **kwargs):
        self._target, self._alive, self.daemon = target, False, False
        self.name = getattr(target, '__name__', '')

    def start(self):
        self._alive = True
        try: self._target()
        finally: self._alive = False

    def is_alive(self):
        return self._alive

    def join(self, timeout=None):
        return None


class LoopRig(object):
    """The service's main loop on a rig clock, as on the box: the REAL Service._start and what it calls
    (_initialize, _tasks, _que, _restart, _interrupt, _suspend, _sleep, _isPlaying, _passFailed;
    FaithfulService's real _shutdown over a FaithfulMonitor), the REAL CustomQueue (its pop thread inline),
    the REAL Tasks.chkQueTimer, _chkEpochTimer and chkServiceChannels over the REAL kodi.Properties
    (Window(10000): FaithfulWindows). Every other task records its run. channels.json is `self.disk`; Kodi
    closes after `hours`; `at(t, fn)` runs fn when the rig clock reaches T0 + t (inside a wait, as Kodi
    goes on). imports.71 (part 2): a failed pass goes through the real loop guard (_passFailed), which
    writes it and lets the loop go on, as on the box; run() then fails with the first such failure, since
    these tests expect none (the guard must not hide a mistake in this rig's wiring)."""

    def __init__(self, env, channels, client=False, hours=1.0):
        import cqueue
        env.monkeypatch.setattr(cqueue, 'Thread', InlineThread)
        self.env, self.t, self.violations = env, T0, []
        self.closing_at, self.disk = T0 + hours * 3600, copy.deepcopy(channels)
        self.ran, self.assigned, self._events = [], [], []
        rig = self

        class Service(FaithfulService):
            @property
            def channels(self):
                return self.__dict__.get('_channels', [])

            @channels.setter
            def channels(self, value):
                rig.assigned.append(rig.t - T0)
                self.__dict__['_channels'] = value
        svc = Service(self.advance, lambda: self.t, lambda: self.closing_at, lambda: False, self.violations)
        svc.isClient, svc.pendingRestart, svc.pendingInterrupt, svc.pendingSuspend = client, False, False, False
        svc.isScanning = svc.isPaused = svc.isPlaying = False
        svc.player = types.SimpleNamespace(runWhilePlaying=False, isPlaying=lambda: False,
                                           isPlayingPseudoTV=lambda: False)

        def notification_wait(message, wait=4, **kwargs):            # kodi.py Dialog.notificationWait's loop
            for _ in range(int(wait)):
                if svc.monitor.waitForAbort(1.0): break
            return True
        builtin = types.SimpleNamespace(isScanning=lambda: False, isPaused=lambda: False,
                                        isSettingsOpened=lambda: False)
        g = dict(PROPERTIES=env.properties, BUILTIN=builtin, LANGUAGE=STRINGS.__getitem__, CPU_CYCLE=CPU_CYCLE,
                 DIALOG=types.SimpleNamespace(notificationWait=notification_wait), loopguard_helpers=loopguard_helpers,
                 xbmc=types.SimpleNamespace(LOGERROR=ERROR))
        for name in ('_start', '_initialize', '_tasks', '_que', '_restart', '_interrupt', '_suspend', '_sleep',
                     '_isPlaying', '_passFailed'):
            setattr(svc, name, types.MethodType(compile_method('services.py', 'Service', name, **g), svc))
        svc._stop = lambda pendingRestart=False: self.ran.append(('_stop', self.t - T0)) or pendingRestart
        svc.loopFailures, self.caught, guard = loopguard_helpers.Failures(), [], svc._passFailed

        def pass_failed(error):
            rig.caught.append(error)
            return guard(error)
        svc._passFailed = pass_failed

        def recorder(name):
            def task(*args, **kwargs):
                rig.ran.append((name, rig.t - T0))
            task.__name__ = name
            return task
        tasks = types.SimpleNamespace(service=svc, log=lambda *a, **k: None,
                                      getChannels=lambda: copy.deepcopy(rig.disk))
        for name in ('chkVersion', 'chkKodiSettings', 'chkCacheClean', 'chkDiscovery', 'chkQUES', 'chkFiles',
                     'chkLibrary', 'chkPVRRefresh', 'chkChanged', '_host', '_client'):
            setattr(tasks, name, recorder(name))
        tasks._chkPropTimer = lambda key, func, priority=-1, *a, **k: None
        tg = dict(PROPERTIES=env.properties, SETTINGS=env.settings, time=types.SimpleNamespace(time=lambda: rig.t))
        for name in ('chkQueTimer', '_chkEpochTimer', 'chkServiceChannels'):
            setattr(tasks, name, types.MethodType(compile_method('tasks.py', 'Tasks', name, **tg), tasks))
        svc.tasks = tasks
        svc.channels = copy.deepcopy(channels)                  # Service.__init__: self.tasks.getChannels()
        del self.assigned[:]
        svc.priorityQUE = cqueue.CustomQueue(priority=True, service=svc)
        self.service = svc

    def advance(self, seconds):
        self.t += seconds
        while self._events and T0 + self._events[0][0] <= self.t:
            self._events.pop(0)[1]()

    def at(self, offset, fn):
        self._events.append((offset, fn))
        self._events.sort(key=lambda event: event[0])

    def run(self):
        stopped = self.service._start()
        if self.caught: raise self.caught[0]
        return stopped


@pytest.mark.parametrize('client', [False, True])
def test_the_channel_list_is_refreshed_every_five_minutes(denv, client):
    """C1. Without a single settings save, the main loop's chkQueTimer runs chkServiceChannels at once
    and then every 300 s (each time once chkQueTimer's own 15 s turn comes round), on host and client."""
    rig = LoopRig(denv, [{'id': CHID, 'name': 'Cartoons'}], client=client, hours=1.0)
    assert rig.run() is False
    times = rig.assigned
    assert times[0] < 2 * SERVICE_INTERVAL + 20                # the first pass (after the 15 s 'Initializing')
    gaps = [b - a for a, b in zip(times, times[1:])]
    # _chkEpochTimer schedules from int(time.time()): a gap can be up to 1 s short of 300
    assert len(times) >= 12 and all(299 <= gap <= 300 + 15 + 2 * SERVICE_INTERVAL for gap in gaps), gaps
    assert denv.reg.saves == [] and rig.violations == []
    assert ('_client' if client else '_host') in [name for name, _t in rig.ran]


def getplaying(env, service):
    """The REAL Player.getplayingItem (services.py) for a Custom channel playing with the guide's entry
    for it (the listitem's sysInfo), over `service`."""
    import globals as lib_globals
    from fileaccess import FileAccess
    guide = {'chid': CHID, 'fitem': {'citem': {'id': CHID, 'name': 'Cartoons', 'logo': 'old.png',
                                               'rules': {'1': 'old'}}}}
    sysinfo = FileAccess._encodeString(guide)
    listitem = types.SimpleNamespace(getProperty=lambda key: sysinfo if key == 'sysInfo' else '')
    player = types.SimpleNamespace(service=service, getPlayerItem=lambda: listitem, isPlayingFiller=lambda: False,
                                   getPlayerTime=lambda: 0)
    run = compile_method('services.py', 'Player', 'getplayingItem', FileAccess=FileAccess,
                         PSEUDOTV_SLUG='PseudoTV_Live', BUILTIN=types.SimpleNamespace(getInfoLabel=lambda *a: ''),
                         combineDicts=lib_globals.combineDicts, Globals=lib_globals.Globals,
                         PROPERTIES=types.SimpleNamespace(setProperty=lambda k, v: v))
    return run(player)


def test_an_edited_channel_reaches_the_playing_item_within_one_refresh(denv):
    """C1 end to end. The channel is edited in channels.json at +1000 s (its name, logo and rules, as
    the dashboard does), with no settings save. Until the next refresh the playing item still carries
    the old entry; from it on, the new one: at most one refresh period (plus chkQueTimer's turn) later.
    Without the timer, the old entry would stay until a settings save or a Kodi restart."""
    old = {'id': CHID, 'name': 'Cartoons', 'logo': 'old.png', 'rules': {'1': 'old'}}
    new = {'id': CHID, 'name': 'Cartoons HD', 'logo': 'new.png', 'rules': {'1': 'new'}}
    rig = LoopRig(denv, [old], hours=1.0)
    seen = []
    rig.at(1000, lambda: rig.disk.__setitem__(0, copy.deepcopy(new)))
    for second in range(1000, 1700, 5):
        rig.at(second + 1, lambda: seen.append((rig.t - T0, getplaying(denv, rig.service)['citem'])))
    rig.run()
    first_new = [t for t, citem in seen if citem['name'] == 'Cartoons HD'][0]
    assert all(citem == old for t, citem in seen if t < first_new)
    assert all(citem == new for t, citem in seen if t >= first_new)
    assert first_new - 1000 <= 300 + 15 + 2 * SERVICE_INTERVAL + 5
    assert denv.reg.saves == []


def test_the_refresh_retries_the_executor_read_until_one_succeeds(denv):
    """The 5-minute refresh is also the queue's retry (plan: until a read of Enable_Executor has
    succeeded): a queue made while no read worked reads it at the first refresh, then never again there."""
    denv.reg.refuse_new = True
    with denv.reg.hold('A', denv.real):
        rig = LoopRig(denv, [{'id': CHID, 'name': 'Cartoons'}], hours=0.5)
    denv.reg.refuse_new = False
    queue = rig.service.priorityQUE
    assert queue.useExecutor is True and queue.executorRead is False
    calls, check = [], queue._chkExecutor

    def spy(retry=False):
        built = denv.reg.built
        answer = check(retry=retry)
        calls.append((retry, denv.reg.built - built, answer))
        return answer
    queue._chkExecutor = spy
    rig.run()
    assert len(rig.assigned) >= 6 and denv.reg.saves == []
    assert calls == [(True, 1, False)] + [(True, 0, False)] * (len(rig.assigned) - 1)    # one read, then none
    assert queue.useExecutor is False and queue.executorRead is True


@pytest.mark.parametrize('value', ['true', 'false'])
def test_only_the_retry_reads_a_setting_while_the_queue_works(denv, value):
    """The review's cases (fix stage), in the main loop with its tasks really going through the queue
    (_push, _run, the pop thread's _exe). With the box's value (true: the settings.xml default, and the
    class default too) as with the other one, the first refresh reads Enable_Executor once and no later
    refresh reads it. And nothing else on the queue's path reads a setting: the one settings object built
    in the whole run is that read, and REAL_SETTINGS answers only logger.log's own two reads. A read per
    push or per task would show here. The rig is a client, whose chkQueTimer reads no setting itself."""
    denv.reg.disk['Enable_Executor'] = value
    denv.reg.refuse_new = True
    with denv.reg.hold('A', denv.real):
        rig = LoopRig(denv, [{'id': CHID, 'name': 'Cartoons'}], client=True, hours=0.5)
    denv.reg.refuse_new = False
    queue, expected = rig.service.priorityQUE, value == 'true'
    assert queue.useExecutor is True and queue.executorRead is False
    calls, ran, check, exe = [], [], queue._chkExecutor, queue._exe

    def spy(retry=False):
        built = denv.reg.built
        answer = check(retry=retry)
        calls.append((retry, denv.reg.built - built, answer))
        return answer

    def executed(func, *args, **kwargs):
        ran.append(func.__name__)
        return exe(func, *args, **kwargs)
    queue._chkExecutor, queue._exe = spy, executed
    built = denv.reg.built
    del denv.real.calls[:]
    rig.run()
    assert len(rig.assigned) >= 6 and denv.reg.saves == []
    assert calls == [(True, 1, expected)] + [(True, 0, expected)] * (len(rig.assigned) - 1)
    assert queue.useExecutor is expected and queue.executorRead is True
    assert ran.count('chkQueTimer') >= 60 and 'chkServiceChannels' in ran      # the tasks ran through _exe
    assert denv.reg.built - built == 1
    assert [call for call in denv.real.calls if call[1] not in ('Debug_Enable', 'Debug_Level')] == []


def test_a_failed_channel_read_cannot_skip_the_executor_retry():
    """chkServiceChannels retries the queue's read first: channels.json failing to load after it (the
    queue logs that and goes on) leaves the retry done."""
    asked = []

    def broken():
        raise OSError('channels.json unreadable')
    queue = types.SimpleNamespace(_chkExecutor=lambda retry=False: asked.append(retry))
    tasks = types.SimpleNamespace(service=types.SimpleNamespace(priorityQUE=queue), getChannels=broken,
                                  log=lambda *a, **k: None)
    with pytest.raises(OSError):
        compile_method('tasks.py', 'Tasks', 'chkServiceChannels')(tasks)
    assert asked == [True]


def test_chkQueTimer_registers_the_refresh_for_host_and_client():
    """chkQueTimer's own body, outside its host-only block: every 300 s, priority 1."""
    node = [n for n in lib_source('tasks.py')[1].body if isinstance(n, ast.ClassDef) and n.name == 'Tasks'][0]
    timer = [n for n in node.body if isinstance(n, ast.FunctionDef) and n.name == 'chkQueTimer'][0]
    top = [ast.unparse(s) for s in timer.body]
    assert "self._chkEpochTimer('chkServiceChannels', self.chkServiceChannels, 300, 1)" in top
    refresh = [n for n in node.body if isinstance(n, ast.FunctionDef) and n.name == 'chkServiceChannels'][0]
    assert [ast.unparse(s) for s in refresh.body] == ["self.log('chkServiceChannels')",
                                                      'self.service.priorityQUE._chkExecutor(retry=True)',
                                                      'self.service.channels = self.getChannels()']
    assert 'imports.71' in ast.get_source_segment(lib_source('tasks.py')[0], refresh)
    assert 'imports.71' in ast.get_source_segment(lib_source('tasks.py')[0], timer)


def test_the_service_channel_list_is_assigned_in_three_places():
    """Service.channels: the class default, Service.__init__, Monitor._updateServiceSettings (after a
    settings save) and Tasks.chkServiceChannels (the timer). Player.getplayingItem is its reader."""
    stores = []
    for name, text in library_sources().items():
        src = _File(text)
        for node in ast.walk(src.tree):
            if isinstance(node, ast.Attribute) and node.attr == 'channels' and isinstance(node.ctx, ast.Store):
                owner, receiver = src.around(node)[0], ast.unparse(node.value)
                if receiver == 'self.service' or (name == 'services.py' and owner.startswith('Service.')):
                    stores.append((name, owner, ast.unparse(node)))
    assert sorted(stores) == [('services.py', 'Monitor._updateServiceSettings', 'self.service.channels'),
                              ('services.py', 'Service.__init__', 'self.channels'),
                              ('tasks.py', 'Tasks.chkServiceChannels', 'self.service.channels')]
    service = [n for n in lib_source('services.py')[1].body if isinstance(n, ast.ClassDef) and n.name == 'Service'][0]
    assert 'channels = []' in [ast.unparse(n) for n in service.body if isinstance(n, ast.Assign)]
    reads = [ast.unparse(n) for n in ast.walk(lib_source('services.py')[1]) if isinstance(n, ast.Attribute)
             and n.attr == 'channels' and isinstance(n.ctx, ast.Load) and ast.unparse(n.value) == 'self.service']
    assert reads == ['self.service.channels']


def test_the_changed_code_is_python38_and_free_of_bitor():
    from test_imports63_clock import py39_api_leaks
    from test_imports69_importcfg_helpers import BITOR_REVIEWED, bitor_lines
    for filename in ('server.py', 'tasks.py'):
        text = lib_source(filename)[0]
        ast.parse(text, feature_version=(3, 8))
        assert py39_api_leaks(text) == [] and BITOR_REVIEWED[filename] == [] and bitor_lines(text) == []


def test_a_settings_save_still_refreshes_the_list(denv):
    """The old path stays: a save runs Monitor._updateServiceSettings, which re-reads the list too."""
    queue = denv.cqueue.CustomQueue(priority=True, service=quiet())
    rig = monitor_rig(denv, queue)
    rig.service.channels = []
    rig._updateServiceSettings()
    assert rig.service.channels == ['the channel list']
