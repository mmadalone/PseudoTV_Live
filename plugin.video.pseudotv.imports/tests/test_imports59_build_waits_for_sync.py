"""imports.59 (S16 + A1) — Builder's import sync starts from a fresh
channels.json read, and builds wait for a running import sync.

Part 1: the Builder-path syncAll used Builder.channels, loaded once per Kodi
session, and wrote its import records and the whole import list back (memory
wins), so a dashboard edit to an imported channel or an import's settings was
reverted by the next build of a Custom channel. It now gets a fresh
Channels(writable=True), exactly as chkImports does every cycle.

Part 2: chkImports defers while Builder runs, but Builder never checked for a
running import sync: a build that loaded pseudotv.m3u/xml mid-sync wrote the
pre-sync guide back. Non-preview builds now wait (1-second steps, abort- and
interrupt-aware, at most 5 minutes) while 'Imports.syncAll' is set. When the
wait stops because Kodi is shutting down or the service interrupted it, the
build is skipped before it loads the guide files (and re-queued like the
per-channel interrupt, unless Kodi is aborting); after the 5-minute cap it
proceeds, as before.

A1: chkImports re-checks 'Builder.buildChannels' after raising its own
'Imports.syncAll' flag -- both sides "raise own flag, then check the other's",
so they can never both go ahead. When chkImports defers on Builder with a
dashboard kick the poll loop already took, the kick is put back (unless a
newer one is pending), so it fires after the build instead of being lost.

builder.py imports under the stubs; tasks.py can't (xbmcgui.ControlList), so
the shipped chkImports body is executed against fakes (test_imports54 style).
"""
import ast
import contextlib
import os
import re
import textwrap
import types

import pytest
import xbmc

import globals as globals_mod
import builder as builder_mod
from builder import Builder, IMPORTS_SYNC_WAIT_MAX_SECS


HERE = os.path.dirname(os.path.abspath(__file__))
LIB  = os.path.join(os.path.dirname(HERE), 'resources', 'lib')


def _read(name):
    with open(os.path.join(LIB, name)) as f:
        return f.read()


def _method(src, name):
    m = re.search(r"(    def %s\(.*?)(?=\n    def |\Z)" % re.escape(name), src, re.DOTALL)
    assert m, '%s not found' % name
    return m.group(1)


# ================================================================ part 1 — fresh Channels for the sync


def test_builder_sync_uses_a_fresh_channels_instance():
    body = _method(_read('builder.py'), 'buildChannels')
    m = re.search(r"sync_channels\s*=\s*Channels\(writable=True\)\s*\n"
                  r"\s*if list\(sync_channels\.getImports\(\) or \[\]\):\s*\n"
                  r"\s*Imports\(channels=sync_channels,", body)
    assert m, 'the Builder-path syncAll must get a freshly constructed Channels(writable=True)'
    assert 'Imports(channels=self.channels' not in body
    assert re.search(r'del sync_channels', body)


def test_builder_custom_save_still_uses_the_class_instance():
    body = _method(_read('builder.py'), 'buildChannels')
    assert 'self.channels.setChannels(modified_ids=modified_ids)' in body
    assert re.search(r'^    channels = Channels\(writable=True\)', _read('builder.py'), re.M)


# ================================================================ part 2 — the wait


def test_buildchannels_waits_before_loading_fresh_guide_files():
    body = _method(_read('builder.py'), 'buildChannels')
    m = re.search(r"if not preview:\s*\n(?:\s*#[^\n]*\n)*\s*if not self\._waitForImportsSync\(\):\s*\n"
                  r".*?\n\s*return\s*\n"
                  r"\s*self\.m3u\s*=\s*M3U\(writable=True\)\s*\n"
                  r"\s*self\.xmltv\s*=\s*XMLTVS\(writable=True, m3u=self\.m3u\)", body, re.DOTALL)
    assert m, 'non-preview builds wait for a running import sync right before the fresh M3U/XMLTVS'
    flag = body.index("PROPERTIES.chkRunning('Builder.buildChannels')")
    assert flag < body.index('self._waitForImportsSync()'), 'Builder raises its own flag first'


def test_wait_helper_checks_the_import_sync_flag():
    body = _method(_read('builder.py'), '_waitForImportsSync')
    assert "PROPERTIES.isRunning('Imports.syncAll')" in body
    assert 'self.monitor.waitForAbort(1.0)' in body and 'self.service._interrupt()' in body
    assert IMPORTS_SYNC_WAIT_MAX_SECS == 300


class _Monitor(object):
    def __init__(self, abort_after=None, aborting=False):
        self.waits = []
        self.abort_after = abort_after
        self.aborting = aborting

    def waitForAbort(self, timeout=0):
        self.waits.append(timeout)
        return self.abort_after is not None and len(self.waits) >= self.abort_after

    def abortRequested(self):
        return self.aborting


class _Service(object):
    def __init__(self, interrupt=False):
        self.interrupt = interrupt
        self.calls = 0

    def _interrupt(self):
        self.calls += 1
        return self.interrupt


class _FakeBuilder(object):
    def __init__(self, monitor=None, service=None):
        self.monitor = monitor or _Monitor()
        self.service = service or _Service()
        self.lines = []

    def log(self, msg, level=xbmc.LOGDEBUG):
        self.lines.append((msg, level))


def _running(monkeypatch, polls_true):
    """Imports.syncAll reads as running for `polls_true` polls (None = always)."""
    seen = []

    def is_running(key):
        assert key == 'Imports.syncAll'
        seen.append(key)
        return polls_true is None or len(seen) <= polls_true
    monkeypatch.setattr(globals_mod.PROPERTIES, 'isRunning', is_running)
    return seen


def test_no_sync_running_no_wait(monkeypatch):
    _running(monkeypatch, 0)
    b = _FakeBuilder()
    assert Builder._waitForImportsSync(b) is True
    assert b.monitor.waits == [] and b.lines == []


def test_proceeds_after_the_sync_finishes(monkeypatch):
    _running(monkeypatch, 3)
    b = _FakeBuilder()
    assert Builder._waitForImportsSync(b) is True
    assert b.monitor.waits == [1.0, 1.0, 1.0]
    assert b.lines == [('buildChannels, waited 3s for the running import sync to finish', xbmc.LOGINFO)]


def test_interrupt_stops_the_wait_at_once(monkeypatch):
    _running(monkeypatch, None)
    b = _FakeBuilder(service=_Service(interrupt=True))
    assert Builder._waitForImportsSync(b) is False
    assert b.monitor.waits == []
    assert b.lines and b.lines[0][1] == xbmc.LOGINFO


def test_abort_stops_the_wait_at_once(monkeypatch):
    _running(monkeypatch, None)
    b = _FakeBuilder(monitor=_Monitor(abort_after=1))
    assert Builder._waitForImportsSync(b) is False
    assert b.monitor.waits == [1.0]


def test_five_minute_cap(monkeypatch):
    """Giving up after 5 minutes lets the build proceed (True), as before."""
    _running(monkeypatch, None)
    b = _FakeBuilder()
    assert Builder._waitForImportsSync(b) is True
    assert len(b.monitor.waits) == 300
    assert b.lines == [('buildChannels, import sync still running after 300s -- building anyway', xbmc.LOGWARNING)]


def test_cap_is_a_parameter(monkeypatch):
    _running(monkeypatch, None)
    b = _FakeBuilder()
    assert Builder._waitForImportsSync(b, max_wait=2) is True
    assert len(b.monitor.waits) == 2


class _LateInterrupt(_Service):
    """_interrupt() turns True from its `after`-th call on."""
    def __init__(self, after):
        _Service.__init__(self)
        self.after = after

    def _interrupt(self):
        self.calls += 1
        return self.calls >= self.after


def test_interrupt_in_the_last_second_still_skips(monkeypatch):
    """An interrupt that arrives while the last 1-second wait runs is seen
    before the limit, so the build is skipped instead of loading mid-sync."""
    _running(monkeypatch, None)
    b = _FakeBuilder(service=_LateInterrupt(after=3))
    assert Builder._waitForImportsSync(b, max_wait=2) is False
    assert len(b.monitor.waits) == 2
    assert b.lines and b.lines[-1][1] == xbmc.LOGINFO


def test_kodi_already_closing_skips_without_waiting(monkeypatch):
    _running(monkeypatch, None)
    b = _FakeBuilder(monitor=_Monitor(aborting=True))
    assert Builder._waitForImportsSync(b) is False
    assert b.monitor.waits == []


# ---------------------------------------------------------------- buildChannels on the wait's answer


class _BuildProps(object):
    """PROPERTIES for buildChannels: Builder not running yet; records its flag."""
    def __init__(self):
        self.flags = []

    def isRunning(self, key):
        return False

    @contextlib.contextmanager
    def legacy(self):
        yield

    @contextlib.contextmanager
    def chkRunning(self, key):
        self.flags.append(('set', key))
        try:
            yield True
        finally:
            self.flags.append(('clear', key))


class _QueService(object):
    def __init__(self):
        self.queued = []
        self.tasks = types.SimpleNamespace(chkChannels=self.chkChannels)

    def chkChannels(self, channels=None, silent=None):
        raise AssertionError('only queued, never run here')

    def _que(self, func, priority=-1, *args, **kwargs):
        self.queued.append((func, priority, args))


class _BuildSelf(object):
    """Just enough of a Builder for buildChannels up to the fresh guide files."""
    def __init__(self, wait_result, service=None, aborting=False):
        self.wait_result = wait_result
        self.waits = 0
        self.service = service if service is not None else _QueService()
        self.monitor = _Monitor(aborting=aborting)
        self.lines = []

    def log(self, msg, level=xbmc.LOGDEBUG):
        self.lines.append((msg, level))

    def _waitForImportsSync(self):
        self.waits += 1
        return self.wait_result


class _Loaded(Exception):
    pass


def _build(monkeypatch, me, channels):
    props = _BuildProps()
    loaded = []

    def _load(name):
        def make(*a, **k):
            loaded.append(name)
            raise _Loaded(name)
        return make
    monkeypatch.setattr(builder_mod, 'PROPERTIES', props)
    monkeypatch.setattr(builder_mod, 'M3U', _load('M3U'))
    monkeypatch.setattr(builder_mod, 'XMLTVS', _load('XMLTVS'))
    try:
        result = Builder.buildChannels(me, channels, preview=False, silent=False)
    except _Loaded:
        result = 'loaded'
    return result, props, loaded


CHANS = [{'id': 'c1', 'name': 'Custom One', 'number': 1, 'path': ['x']}]


def test_build_skipped_when_the_wait_stopped_on_abort_or_interrupt(monkeypatch):
    me = _BuildSelf(wait_result=False)
    result, props, loaded = _build(monkeypatch, me, list(CHANS))
    assert result is None and loaded == [], 'returned before loading the guide files'
    assert me.waits == 1
    assert me.service.queued == [(me.service.tasks.chkChannels, 3, (CHANS, False))], \
        're-queued like the per-channel interrupt branch'
    assert props.flags == [('set', 'Builder.buildChannels'), ('clear', 'Builder.buildChannels')], \
        'Builder flag released'
    assert [lvl for m, lvl in me.lines if 'build skipped' in m] == [xbmc.LOGINFO]


def test_build_skipped_without_requeue_while_kodi_aborts(monkeypatch):
    me = _BuildSelf(wait_result=False, aborting=True)
    result, props, loaded = _build(monkeypatch, me, list(CHANS))
    assert result is None and loaded == []
    assert me.service.queued == []
    assert props.flags[-1] == ('clear', 'Builder.buildChannels')


def test_build_skipped_without_requeue_for_no_channels_or_no_queue(monkeypatch):
    me = _BuildSelf(wait_result=False)
    result, _props, loaded = _build(monkeypatch, me, [])
    assert result is None and loaded == [] and me.service.queued == []
    me = _BuildSelf(wait_result=False, service=types.SimpleNamespace())     # builder.Service: no _que
    result, _props, loaded = _build(monkeypatch, me, list(CHANS))
    assert result is None and loaded == []


def test_build_proceeds_when_the_wait_says_so(monkeypatch):
    """No sync running, sync finished, or the 5-minute cap: load the guide."""
    me = _BuildSelf(wait_result=True)
    result, props, loaded = _build(monkeypatch, me, list(CHANS))
    assert result == 'loaded' and loaded == ['M3U']
    assert me.service.queued == []
    assert props.flags[-1] == ('clear', 'Builder.buildChannels')


# ================================================================ A1 — chkImports re-checks Builder


def test_chkimports_rechecks_builder_inside_its_own_flag():
    body = _method(_read('tasks.py'), 'chkImports')
    m = re.search(r"with PROPERTIES\.chkRunning\('Imports\.syncAll'\):\s*\n"
                  r"(?:\s*#[^\n]*\n)*"
                  r"\s*if PROPERTIES\.isRunning\('Builder\.buildChannels'\):\s*\n"
                  r"\s*self\.log\('chkImports, deferred \(Builder\.buildChannels running\)', xbmc\.LOGINFO\)\s*\n"
                  r"(?:\s*self\._keepImportsKick\(force_scope\)\s*\n)?"
                  r"\s*return\b", body)
    assert m, 'the first statement inside the Imports.syncAll flag re-checks Builder and defers'
    # the original top-of-function defer stays
    assert body.count("PROPERTIES.isRunning('Builder.buildChannels')") == 2


class _Props(object):
    def __init__(self, builder_states):
        self.builder_states = list(builder_states)
        self.flags = []
        self.ext = {}

    def isRunning(self, key):
        if key == 'Builder.buildChannels':
            return self.builder_states.pop(0)
        return False

    def getEXTProperty(self, key, default=''):
        return self.ext.get(key, default)

    def setEXTProperty(self, key, value):
        self.ext[key] = value
        return value

    @contextlib.contextmanager
    def chkRunning(self, key):
        self.flags.append(('set', key))
        try:
            yield True
        finally:
            self.flags.append(('clear', key))


class _Stop(Exception):
    pass


def _shipped_function(src, name, namespace):
    """The function `name` compiled from the shipped source `src`, with
    `namespace` as its globals (module-level fakes). Default argument values
    are not carried over: callers pass every argument."""
    code = compile(src, 'tasks.py:%s' % name, 'exec')
    fn_code = next(c for c in code.co_consts if isinstance(c, types.CodeType) and c.co_name == name)
    return types.FunctionType(fn_code, namespace)


def _run_chkimports(monkeypatch, builder_states, force_scope=None, pending_kick=''):
    """Execute the SHIPPED chkImports body (and, when tasks.py has it, its
    kick helper) against fakes."""
    tasks_src = _read('tasks.py')
    src = textwrap.dedent(_method(tasks_src, 'chkImports'))
    props = _Props(builder_states)
    if pending_kick:
        props.ext['chkImports.kick'] = pending_kick
    constructed = []

    class _Chans(object):
        def __init__(self, *a, **k): pass
        def getImports(self): return [{'id': 'mp'}]

    import m3u as m3u_mod
    import xmltvs as xmltvs_mod
    import imports as imports_mod

    def _forbidden(name):
        def make(*a, **k):
            constructed.append(name)
            raise _Stop(name)
        return make
    monkeypatch.setattr(m3u_mod, 'M3U', _forbidden('M3U'))
    monkeypatch.setattr(xmltvs_mod, 'XMLTVS', _forbidden('XMLTVS'))
    monkeypatch.setattr(imports_mod, 'Imports', _forbidden('Imports'))
    namespace = {'PROPERTIES': props, 'Channels': _Chans, 'xbmc': xbmc}
    fn = _shipped_function(src, 'chkImports', namespace)

    class _Self(object):
        service = None
        lines = []
        def log(self, msg, level=xbmc.LOGDEBUG): self.lines.append((msg, level))
    if re.search(r"\n    def _keepImportsKick\(", tasks_src):
        helper_src = textwrap.dedent(_method(tasks_src, '_keepImportsKick'))
        _Self._keepImportsKick = _shipped_function(helper_src, '_keepImportsKick', namespace)
    me = _Self()
    me.lines = []
    fn(me, None, force_scope)        # silent, force_scope (defaults live on the def, not the code)
    return me.lines, props, constructed


def test_chkimports_defers_when_builder_starts_after_the_first_check(monkeypatch):
    lines, props, constructed = _run_chkimports(monkeypatch, [False, True])
    assert constructed == [], 'no guide files loaded, no sync started'
    assert ('chkImports, deferred (Builder.buildChannels running)', xbmc.LOGINFO) in lines
    assert props.flags == [('set', 'Imports.syncAll'), ('clear', 'Imports.syncAll')], 'own flag released'


def test_chkimports_proceeds_when_builder_is_idle(monkeypatch):
    lines, props, constructed = _run_chkimports(monkeypatch, [False, False])
    assert constructed == ['M3U'], 'went on to load the guide files'
    assert not [m for m, lvl in lines if 'deferred' in m]


def test_chkimports_top_defer_unchanged(monkeypatch):
    lines, props, constructed = _run_chkimports(monkeypatch, [True])
    assert constructed == [] and props.flags == []
    assert ('chkImports, deferred (Builder.buildChannels running)', xbmc.LOGINFO) in lines


# ---------------------------------------------------------------- a taken kick survives a Builder deferral


@pytest.mark.parametrize('builder_states', [[True], [False, True]], ids=['top-defer', 'A1-defer'])
def test_deferred_kick_is_put_back(monkeypatch, builder_states):
    """The poll loop took the kick off 'chkImports.kick' and called chkImports,
    which then deferred on Builder: the kick goes back, so the poll loop fires
    it about 3 s after the build."""
    lines, props, constructed = _run_chkimports(monkeypatch, builder_states, force_scope='mp')
    assert constructed == []
    assert props.ext.get('chkImports.kick') == 'mp'
    assert [lvl for m, lvl in lines if 'kick (mp) kept' in m] == [xbmc.LOGINFO]


@pytest.mark.parametrize('builder_states', [[True], [False, True]], ids=['top-defer', 'A1-defer'])
def test_newer_pending_kick_is_left_alone(monkeypatch, builder_states):
    lines, props, constructed = _run_chkimports(monkeypatch, builder_states, force_scope='mp',
                                                pending_kick='all')
    assert props.ext.get('chkImports.kick') == 'all'
    assert not [m for m, lvl in lines if 'kept' in m]


def test_natural_cycle_puts_no_kick_back(monkeypatch):
    lines, props, constructed = _run_chkimports(monkeypatch, [True], force_scope=None)
    assert 'chkImports.kick' not in props.ext


def test_no_kick_put_back_when_chkimports_runs(monkeypatch):
    lines, props, constructed = _run_chkimports(monkeypatch, [False, False], force_scope='all')
    assert constructed == ['M3U']
    assert 'chkImports.kick' not in props.ext


def test_tasks_source_parses_as_python38():
    ast.parse(_read('tasks.py'), feature_version=(3, 8))
    ast.parse(_read('builder.py'), feature_version=(3, 8))


def test_wait_helper_executes_in_buildchannels_only_for_non_preview():
    """The wait sits inside `if not preview:` -- previews never wait."""
    body = _method(_read('builder.py'), 'buildChannels')
    lines = body.splitlines()
    at = [i for i, ln in enumerate(lines) if 'self._waitForImportsSync()' in ln]
    assert len(at) == 1
    block = [ln for ln in lines[max(0, at[0] - 12):at[0]] if ln.strip() and not ln.strip().startswith('#')]
    assert block[-1].strip() == 'if not preview:'
    assert builder_mod.Builder._waitForImportsSync.__doc__
