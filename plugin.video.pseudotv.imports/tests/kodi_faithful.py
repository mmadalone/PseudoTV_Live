# -*- coding: utf-8 -*-
"""imports.70: stand-ins for Kodi that behave like Kodi 21.3 where a forgiving fake hid a bug.

TV test 2026-11-19: imports.70's replay-end watcher never acted. Its move began with
service._shutdown(0), that is xbmc.Monitor.waitForAbort(0), and Kodi takes a timeout of 0 or less
as "no timeout" (xbmc/interfaces/legacy/Monitor.cpp:39-66: `int timeoutMS = ceil(timeout * 1000)`,
`if (timeoutMS <= 0) endTime.SetInfinite()`; the default argument is -1, Monitor.h:289). The move
thread stood on that line, holding the RESTART lock, until Kodi restarted. 5,743 tests had
passed: every rig's own fake of that wait returned at once on 0. The rigs use this module
instead (the pattern of imports70_sync_rig.py: one shared module, no test in it):

  - ForeverWait: raised where Kodi would never come back. A BaseException on purpose: the add-on
    catches `Exception` around the move, the tick and its worker threads, and none of them may
    swallow it. Each one is also recorded (in the rig's own list and in VIOLATIONS, which
    conftest.py checks after every test), so one raised on a worker thread, or caught by a bare
    `except:`, still fails its test.
  - Standstill: a rig's guard against a loop that never waits. Time passes on a rig's clock only
    inside a wait, so such a loop would never end there, and the rig would record its calls for
    ever. Every call into the rig counts (the monitor's, and the rig's JSON-RPC, condition, log
    and player fakes): more than SPIN_CAP in a row with the clock standing still are refused like
    a wait Kodi never ends, instead of filling the memory or hanging the test run.
  - RestartLock: Player._restart_lock for the glue rigs, refusing a blocking acquire while it is
    held (the replay-end move and RESTART both take it without waiting).
  - FaithfulMonitor: xbmc.Monitor's waitForAbort / abortRequested with Kodi's rules, on a rig's
    clock, each call counted by the rig's Standstill (Service._shutdown with a wait of 0 or less
    now only asks: a loop of those is refused too).
  - FaithfulService: Player.service for the glue rigs. Its _shutdown and _aborted are the REAL
    ones, compiled from services.py, over a FaithfulMonitor: the rigs follow that code by
    themselves and can't drift from it again.
  - positive_wait: the same refusal for a rig's simpler wait fake.
  - KODI_LOG: Kodi's log levels (the repo-root xbmc stub still has Kodi 18's numbers).
  - compile_method / compile_function / lib_constant: one real method, one real top-level function
    (logger.log / logger.notice, for the rigs' log chain), or one constant, out of a library file.

imports.71: the add-on's settings, as Kodi 21.3 keeps them. The service ended on 2026-06-05,
2026-10-04 and 2026-11-07 with TypeError('Invalid setting type') from a typed getter on REAL_SETTINGS:
every save of the add-on's settings makes Kodi rebuild the add-on's oldest live xbmcaddon.Addon in
place, and while it re-parses settings.xml its typed getters raise. The repo-root xbmcaddon stub can't
show that: its typed getters never raise (a missing id reads 0 or False), its handles share one dict
(or none), and it knows no settings.xml type, no default and no reload.
  - SETTING_TYPES: {id: (type, default text)} from the add-on's real resources/settings.xml (115
    settings, 18 of them strings; the commented-out ones are not settings, as for Kodi's parser).
  - FaithfulAddon: one xbmcaddon.Addon object, with its own settings loaded on first use and Kodi's
    rules for an undefined id, a wrong type and the two phases of the in-place reload.
  - FaithfulAddons: xbmcaddon.Addon itself: every call builds a NEW handle; a save rewrites the user
    file, reloads only the oldest live handle, then queues onSettingsChanged, which deliver() runs
    inside a rig's wait on the thread that made the monitor.
  - install_settings: xbmcaddon.Addon and REAL_SETTINGS, for one test (the test passes the stub in).
  - FaithfulWindows: xbmcgui.Window for the properties: one map per window id shared by every object,
    lower-cased keys, text values only, each read a new str (the repo-root stub stores nothing).
  - lib_strings: {id: text} from the add-on's own en_gb strings.po (the stubs read the live add-on's).

Nothing here imports Kodi or a Kodi stub; the only files read are the add-on's own sources.
"""
import ast
import contextlib
import math
import os
import sys
import threading
import types
import weakref
import xml.etree.ElementTree as ET

HERE      = os.path.dirname(os.path.abspath(__file__))
ADDON_LIB = os.path.join(os.path.dirname(HERE), 'resources', 'lib')

# xbmc/commons/ilog.h:18-23 (Kodi 19 and later; LOGINFO replaced LOGNOTICE).
KODI_LOG = dict(LOGDEBUG=0, LOGINFO=1, LOGWARNING=2, LOGERROR=3, LOGFATAL=4, LOGNONE=5)

# Monitor.cpp keeps the timeout in a C++ `int` of milliseconds.
INT_MAX = 2 ** 31 - 1

# s: the longest a blocking stand-in (FaithfulMonitor's `gate`) holds a thread when its test
# forgets to open the gate; then it raises instead of hanging the whole run.
GATE_CAP_S = 60.0

# The most calls in a row a rig answers while its clock stands still (Standstill). A rig's clock
# moves only inside a wait, so a loop that never waits would turn for ever there: one that only
# asks (abortRequested; Service._shutdown with a wait of 0 or less, which asks without waiting
# since the repair; a wait whose "Kodi is closing" answer is ignored), or one whose wait the rig
# never sees (a broken stand-in). Each turn records its calls, so such a loop also eats memory:
# 2026-11-19 23:25 a deliberately broken stand-in (a _shutdown that let no time pass) grew a test
# run by about 45 MB a second until RAM and swap were full and this machine froze. In Kodi the
# clock goes on by itself, but such a loop spins a CPU core, and on the thread that created the
# Player / Monitor it lets none of their callbacks through (Kodi delivers those only inside
# waitForAbort / xbmc.sleep). The code under test makes a few dozen calls at one instant at most
# (measured 2026-11-19 over the tests of the three files whose rigs count them: 39).
SPIN_CAP = 1000

# Every wait any stand-in refused since conftest.py's last check (it empties this before each test
# and fails the test, through check(), when something is left afterwards).
VIOLATIONS = []


class ForeverWait(BaseException):
    """Kodi would wait forever (or the add-on would call on for ever without waiting: SPIN_CAP).
    BaseException on purpose: the add-on's `except Exception` must not swallow it."""


def record(what, violations=None):
    """Note a call Kodi would never return from (or would reject): in the rig's own list and in
    VIOLATIONS."""
    if violations is not None: violations.append(what)
    VIOLATIONS.append(what)


def refuse(what, violations=None):
    """Record `what` and raise ForeverWait."""
    record(what, violations)
    raise ForeverWait('%s: Kodi would wait forever (xbmc/interfaces/legacy/Monitor.cpp: timeoutMS <= 0 -> '
                      'endTime.SetInfinite(); the default timeout is -1)' % (what,))


def provoked():
    """For a test that provokes a refusal on purpose: what was recorded so far, taken off
    VIOLATIONS (so conftest's check passes)."""
    taken = VIOLATIONS[:]
    del VIOLATIONS[:]
    return taken


def check():
    """What conftest.py asserts after every test: nothing was refused since it began (a test that
    provokes a refusal on purpose has taken it off the record with provoked())."""
    left = provoked()
    assert left == [], 'a wait Kodi would never end (or a call Kodi rejects): %s' % (left,)


def timeout_ms(timeout):
    """Kodi's `int timeoutMS = ceil(timeout * 1000)` for one waitForAbort argument.
    -> ('ms', n) for a wait Kodi ends after n ms;
       ('forever', why) when Kodi would not end it: 0 or less (also False, -0.0 and anything that
       rounds up to 0 ms), not a number, or more than its C++ int holds (undefined there; no
       timeout in practice);
       ('type', why) for what Kodi's Python binding rejects with a TypeError (its 'd' argument
       format: None and text; anything else float() takes is accepted, a bool too)."""
    if timeout is None or isinstance(timeout, (str, bytes, bytearray)):
        return 'type', 'must be real number, not %s' % type(timeout).__name__
    try: seconds = float(timeout)
    except (TypeError, ValueError): return 'type', 'must be real number, not %s' % type(timeout).__name__
    if math.isnan(seconds) or math.isinf(seconds): return 'forever', 'not a finite number'
    ms = int(math.ceil(seconds * 1000))
    if ms <= 0: return 'forever', 'timeoutMS = %d' % ms
    if ms > INT_MAX: return 'forever', 'timeoutMS overflows a C++ int'
    return 'ms', ms


def positive_wait(timeout, what='waitForAbort', violations=None):
    """For a rig's simpler wait fake (one that only records the wait or answers from a list): the
    wait itself when Kodi would end it, else the same refusal as FaithfulMonitor's (also for
    None: such a fake can't tell a missing timeout from None)."""
    kind, _detail = timeout_ms(timeout)
    if kind != 'ms': refuse('%s(%r)' % (what, timeout), violations)
    return timeout


class Standstill(object):
    """A rig's guard against a loop that never waits (see SPIN_CAP). Call it with the name of every
    call the rig answers: its monitor's waits and questions (FaithfulMonitor does that itself)
    and its fakes' JSON-RPC calls, condition reads, log lines, player calls and clock reads. More
    than SPIN_CAP calls in a row while `clock()` stands still -> ForeverWait, recorded (record(),
    in `violations` too). Time passing starts the count again; `count` is the calls at this
    instant so far. One per rig clock: the monitor and the fakes of a rig share it, so a loop
    counts whatever mix of calls it makes."""

    def __init__(self, clock, violations=None):
        self._clock, self.violations = clock, violations
        self._at, self.count = None, 0
        # imports.70 (the fix stage): told of every call first, by its name: a rig's call sweep
        # makes the k-th call fail there (Kodi ending the thread at that call, say).
        self.on_call = None

    def __call__(self, call):
        if self.on_call is not None: self.on_call(call)
        now = self._clock()
        if now != self._at: self._at, self.count = now, 0
        self.count += 1
        if self.count <= SPIN_CAP: return
        self.count = 0
        what = '%s: %d calls in a row while no time passed' % (call, SPIN_CAP + 1)
        record(what, self.violations)
        raise ForeverWait('%s: a loop that never waits. It would never end on this clock; in Kodi it would spin '
                          'without a pause' % (what,))


class RestartLock(object):
    """Player._restart_lock for the glue rigs. The replay-end move and RESTART (imports.62) both
    take it without waiting; a blocking acquire while it is held would wait for whoever holds it
    (on the TV of 2026-11-19: until Kodi restarted), so it raises ForeverWait, which no `except
    Exception` in the add-on swallows, and is recorded, where a threading.Lock would hang the test
    run. Every acquire is listed (`acquires`: its blocking argument). `taken` and `released` count
    what the code under test did, so a lock the test itself holds (`held = True`) is told apart from
    one the code never gave back (leaked()). `count`: the rig's Standstill, told of every acquire
    and release."""

    def __init__(self, violations=None, count=None):
        self.held, self.acquires = False, []
        self.taken, self.released, self.violations = 0, 0, violations
        self.count = count

    def acquire(self, blocking=True, timeout=-1):
        if self.count is not None: self.count('_restart_lock.acquire')
        self.acquires.append(blocking)
        if self.held:
            if blocking:
                record('_restart_lock.acquire(blocking) while it is held', self.violations)
                raise ForeverWait('a blocking acquire would wait for the restart')
            return False
        self.held = True
        self.taken += 1
        return True

    def release(self):
        if self.count is not None: self.count('_restart_lock.release')
        if not self.held: raise RuntimeError('release unlocked lock')
        self.held = False
        self.released += 1

    def locked(self):
        return self.held

    def leaked(self):
        """The code under test took it and didn't give it back (or gave back what it never took)."""
        return self.taken != self.released


class FaithfulMonitor(object):
    """xbmc.Monitor's two abort calls as Kodi 21.3 runs them (Monitor.cpp:39-66), on a rig's clock:
      advance(seconds)   the rig's clock moves and Kodi goes on meanwhile (queued changes land);
      clock()            the rig's time now;
      closing_at()       when Kodi starts to close (its abort), or None;
      violations         the rig's list of refused calls (record());
      on_call(name)      optional: told of every call before it runs (the rigs' call sweeps);
      gate               optional threading.Event for a test with real threads: a wait Kodi would
                         never end then BLOCKS, as on the TV, until the test opens the gate (Kodi
                         closing), and answers True;
      standstill         optional: the rig's Standstill, which its fakes count with too (default:
                         one of the monitor's own, on `clock`). The monitor counts each of its
                         calls there, once.
    waitForAbort: no argument, or a timeout of 0 or less (timeout_ms) -> ForeverWait. Also while
    Kodi is already closing, where Kodi would answer at once: stricter than Kodi on purpose, such
    a call is wrong at any other moment. None or text -> TypeError, as Kodi's binding raises (an
    Exception the add-on could swallow, so it is recorded too). A positive timeout: True at once
    when Kodi is already closing (no time passes, nothing queued lands); True at the moment Kodi
    starts to close inside the wait; else the whole wait passes and it answers False.
    abortRequested: only reads the flag. No time passes, nothing queued lands (Kodi delivers
    callbacks inside waitForAbort / xbmc.sleep only).
    Either call counts against the standstill guard: more than SPIN_CAP calls in a row while the
    clock stands still -> ForeverWait, a loop that never waits (Standstill)."""

    def __init__(self, advance, clock, closing_at, violations, on_call=None, gate=None, standstill=None):
        self._advance, self._clock, self._closing_at = advance, clock, closing_at
        self.violations, self._on_call, self._gate = violations, on_call, gate
        self.waits, self.checks = [], 0     # every timeout Kodi would have timed; abortRequested calls
        self.standstill = Standstill(clock, violations) if standstill is None else standstill

    def _closing(self):
        at = self._closing_at()
        return at is not None and self._clock() >= at

    def waitForAbort(self, *args, **kwargs):
        if self._on_call is not None: self._on_call('waitForAbort')
        call = 'waitForAbort(%s)' % ', '.join([repr(a) for a in args] + ['%s=%r' % kv for kv in sorted(kwargs.items())])
        self.standstill(call)
        if len(args) + len(kwargs) > 1 or (kwargs and 'timeout' not in kwargs):
            record(call, self.violations)
            raise TypeError('%s: function takes at most 1 argument, the timeout' % call)
        if not args and not kwargs: return self._forever(call)            # Monitor.h:289: timeout = -1
        timeout = args[0] if args else kwargs['timeout']
        kind, detail = timeout_ms(timeout)
        if kind == 'type':
            record(call, self.violations)
            raise TypeError('%s: %s' % (call, detail))
        if kind == 'forever': return self._forever(call)
        seconds = float(timeout)
        self.waits.append(timeout)
        now, at = self._clock(), self._closing_at()
        if at is not None and now >= at: return True                      # the abort flag stays set
        if at is not None and now + seconds >= at:
            self._advance(at - now)                                       # Kodi closes inside the wait
            return True
        self._advance(seconds)
        return False

    def abortRequested(self):
        if self._on_call is not None: self._on_call('abortRequested')
        self.standstill('abortRequested()')
        self.checks += 1
        return self._closing()

    def _forever(self, call):
        if self._gate is None: refuse(call, self.violations)
        # A test with real threads: stand here as Kodi does, until the test lets Kodi "close".
        record(call, self.violations)
        if not self._gate.wait(GATE_CAP_S):
            raise ForeverWait('%s: still waiting after %d s; the test never opened the gate' % (call, GATE_CAP_S))
        return True


_SOURCES, _CODE = {}, {}


def lib_source(filename):
    """(text, tree) of resources/lib/<filename>, read once."""
    if filename not in _SOURCES:
        with open(os.path.join(ADDON_LIB, filename), encoding='utf-8') as fh: text = fh.read()
        _SOURCES[filename] = (text, ast.parse(text, feature_version=(3, 8)))
    return _SOURCES[filename]


def method_node(filename, cls, name):
    """The def of `cls.name` in resources/lib/<filename>, or None."""
    for node in lib_source(filename)[1].body:
        if isinstance(node, ast.ClassDef) and node.name == cls:
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == name: return item
    return None


def function_node(filename, name):
    """The top-level def `name` in resources/lib/<filename>, or None."""
    for node in lib_source(filename)[1].body:
        if isinstance(node, ast.FunctionDef) and node.name == name: return node
    return None


def _compiled(filename, key, found, what, globals_):
    """`found` (a def of resources/lib/<filename>) compiled once under `key`, run in a namespace of
    only `globals_`: the plain function (decorators dropped). Its code object carries the file's
    real path and line numbers, so a failure points at the add-on's own line. A method's def binds
    no global in Python (it runs in the class body), so for a method (key[1], its class) the name
    is set back to what `globals_` gave it, or removed: Player.log calls the module's log (the
    logger's), never itself."""
    if key not in _CODE:
        if found is None: raise AssertionError('%s: %s not found' % (filename, what))
        node = ast.parse(ast.get_source_segment(lib_source(filename)[0], found)).body[0]
        node.decorator_list = []
        ast.increment_lineno(node, found.lineno - 1)
        # The add-on's own source, compiled from the repo: the pattern of the imports.60-.67 glue tests.
        _CODE[key] = compile(ast.Module(body=[node], type_ignores=[]), os.path.join(ADDON_LIB, filename), 'exec')
    namespace = dict(globals_)
    exec(_CODE[key], namespace)  # noqa: S102
    function, name = namespace[key[-1]], key[-1]
    if key[1] is not None:
        if name in globals_: namespace[name] = globals_[name]
        else: del namespace[name]
    return function


def compile_method(filename, cls, name, **globals_):
    """The real method `cls.name` of resources/lib/<filename> as a plain function (decorators
    dropped) that sees only the given globals."""
    key = (filename, cls, name)
    found = method_node(filename, cls, name) if key not in _CODE else None
    return _compiled(filename, key, found, '%s.%s' % (cls, name), globals_)


def compile_function(filename, name, **globals_):
    """imports.70 (the repair): the real top-level function `name` of resources/lib/<filename>
    (logger.log, logger.notice) as a plain function that sees only the given globals, so a rig's
    log lines go through the add-on's own logger with Kodi's levels (KODI_LOG) and the rig's
    settings, not through the repo-root stubs."""
    key = (filename, None, name)
    found = function_node(filename, name) if key not in _CODE else None
    return _compiled(filename, key, found, name, globals_)


def lib_constant(filename, name):
    """The number assigned to `name` at the top level of resources/lib/<filename> (exactly one
    plain `NAME = <number>` there)."""
    values = [node.value.value for node in lib_source(filename)[1].body
              if isinstance(node, ast.Assign) and [ast.unparse(t) for t in node.targets] == [name]
              and isinstance(node.value, ast.Constant) and isinstance(node.value.value, (int, float))
              and not isinstance(node.value.value, bool)]
    if len(values) != 1: raise AssertionError('%s: %s is not one plain number (%r)' % (filename, name, values))
    return values[0]


# The methods of services.Service a rig's service runs for real. _aborted came with the repair
# (imports.70); against a services.py without it the rig has none either, as the real class.
SERVICE_METHODS = ('_shutdown', '_aborted')


class FaithfulService(object):
    """Player.service / Monitor.service for the glue rigs: the REAL Service._shutdown and
    Service._aborted (compiled from services.py; their PROPERTIES.isPendingShutdown() is
    `pending`, their SERVICE_INTERVAL the one in constants.py) over a FaithfulMonitor. Its other
    arguments are FaithfulMonitor's. `logged` holds Service.log's (message, level) pairs, each
    counted by the monitor's standstill guard; `pendingShutdown` starts False, as the real class
    attribute."""

    def __init__(self, advance, clock, closing_at, pending, violations, on_call=None, gate=None, standstill=None):
        self.monitor = FaithfulMonitor(advance, clock, closing_at, violations, on_call=on_call, gate=gate,
                                       standstill=standstill)
        self.pendingShutdown, self.logged = False, []
        g = dict(SERVICE_INTERVAL=lib_constant('constants.py', 'SERVICE_INTERVAL'),
                 PROPERTIES=types.SimpleNamespace(isPendingShutdown=pending))
        for name in SERVICE_METHODS:
            if name != '_shutdown' and method_node('services.py', 'Service', name) is None: continue
            setattr(self, name, types.MethodType(compile_method('services.py', 'Service', name, **g), self))

    def log(self, msg, level=KODI_LOG['LOGDEBUG']):
        self.monitor.standstill('Service.log')
        self.logged.append((msg, level))


# ================================================================ imports.71: the add-on's settings

ADDON_ID     = 'plugin.video.pseudotv.imports'
SETTINGS_XML = os.path.join(os.path.dirname(ADDON_LIB), 'settings.xml')

# The typed getter Kodi's Python binding answers for each settings.xml type: GetSettingValue wants the
# setting's own type (xbmc/addons/Addon.cpp:420-435). A path or an addon setting is a CSettingString
# (xbmc/settings/SettingPath.h:17, SettingAddon.h:19); an action (Setting.h:506-519) or a list setting
# (Setting.h:181) has no typed getter. The setters follow the getters (Addon.cpp:487-515).
TYPED_GETTER = {'boolean': 'getSettingBool', 'integer': 'getSettingInt', 'number': 'getSettingNumber',
                'string': 'getSettingString', 'path': 'getSettingString', 'addon': 'getSettingString'}
GETTERS      = ('getSettingBool', 'getSettingInt', 'getSettingNumber', 'getSettingString')


def setting_types(path=SETTINGS_XML):
    """{id: (type, default text)} of every <setting> in the add-on's resources/settings.xml, read as
    Kodi reads it: comments are skipped (the commented-out Backup_Channels / Recover_Backup are not
    settings), and a missing or empty <default> is ''."""
    found = {}
    for node in ET.parse(path).iter('setting'):  # noqa: S314 -- the add-on's own resources/settings.xml
        default = node.find('default')
        found[node.get('id')] = (node.get('type'), (default.text or '') if default is not None else '')
    return found


SETTING_TYPES = setting_types()


def _from_text(kind, text):
    """What a typed getter returns for `text` (the user file's form), or None when Kodi's FromString
    would refuse it (the setting then keeps its value)."""
    if kind == 'boolean': return {'true': True, 'false': False}.get(str(text).lower())
    if kind in ('integer', 'number'):
        try: return int(text) if kind == 'integer' else float(text)
        except (TypeError, ValueError): return None
    return text


def _text(value, argument, method):
    """A text argument as Kodi's Python binding takes it (interfaces/python/swig.cpp:95-141): None is '',
    bytes are decoded, anything else but str raises TypeError."""
    if value is None: return ''
    if isinstance(value, bytes): return value.decode('utf-8')
    if isinstance(value, str): return value
    raise TypeError('argument "%s" for method "%s" must be unicode or str' % (argument, method))


def _to_text(value):
    """A value as Kodi writes it to the user file (CSetting*::ToString)."""
    if isinstance(value, bool): return 'true' if value else 'false'
    if isinstance(value, float): return '%f' % value
    return str(value)


class FaithfulAddon(object):
    """One xbmcaddon.Addon object of the add-on, as Kodi 21.3 keeps it: a CAddon of its own with
    settings of its own (xbmc/addons/AddonManager.cpp:630-655; interfaces/legacy/Addon.cpp:54-77),
    loaded on the first settings access, a parse of settings.xml and the user file (Addon.cpp:267-322),
    and never again unless Kodi reloads this very object after a save (FaithfulAddons).
      phase   None, or the reload phase it is in: 'A' from CSettingsManager::Clear to the end of
              CAddonSettings::Initialize (Addon.cpp:286-308): every typed getter raises
              TypeError('Invalid setting type') after Kodi's own ERROR line, getSetting returns '';
              'B' from there until the user file is read (Addon.cpp:315-319): the settings.xml
              defaults, without an error.
      calls   every getter and setter call on it, (method, id), in order.
      number  its place in construction order (1: the first one built).
    Kodi's rules: a typed getter for an undefined id or of the wrong type writes 'EXCEPTION: Invalid
    setting type' and raises TypeError (Addon.cpp:420-435; legacy Addon.cpp:94-128;
    PythonSwig.cpp.template:221-224); getSetting returns '' for an undefined id (Addon.cpp:408-418);
    setSetting takes text and adds an undefined id (Addon.cpp:465-485); a typed setter of the wrong type
    raises the same way and saves nothing (Addon.cpp:487-515; legacy Addon.cpp:143-196); every setter
    then saves (CAddon::SaveSettings, Addon.cpp:375-406), changed or not."""

    def __init__(self, registry, number):
        self._registry, self.number = registry, number
        self.phase, self.calls = None, []
        self._values, self._kinds = None, {}       # {id: text} once loaded; ids its setters added
        self._broken, self._saveable = False, False

    def __repr__(self):
        return '<FaithfulAddon #%d%s>' % (self.number, '' if self.phase is None else ' phase ' + self.phase)

    # --- loading ------------------------------------------------------------------------------------
    def _load(self):
        """Kodi's LoadSettings(false): True once the definitions are in. The first call parses (the user
        file too); a handle whose definitions failed stays without settings (m_loadSettingsFailed,
        Addon.cpp:274-279, 295-312)."""
        if self._values is not None: return True
        if self._broken: return False
        return self._registry._read(self)

    def _kind(self, key):
        return SETTING_TYPES.get(key, (None, ''))[0] or self._kinds.get(key)

    # --- getters ------------------------------------------------------------------------------------
    def _typed(self, method, key):
        self.calls.append((method, key))
        if self.phase == 'A' or not self._load(): return self._registry._wrong_type()
        kind = self._kind(key)
        if TYPED_GETTER.get(kind) != method: return self._registry._wrong_type()
        default = SETTING_TYPES.get(key, (None, ''))[1]
        value = _from_text(kind, default if self.phase == 'B' else self._values.get(key, default))
        return _from_text(kind, default) if value is None else value

    def getSettingBool(self, id):
        return self._typed('getSettingBool', id)

    def getSettingInt(self, id):
        return self._typed('getSettingInt', id)

    def getSettingNumber(self, id):
        return self._typed('getSettingNumber', id)

    def getSettingString(self, id):
        return self._typed('getSettingString', id)

    def getSetting(self, id):
        self.calls.append(('getSetting', id))
        if self.phase == 'A' or not self._load(): return ''
        default = SETTING_TYPES.get(id, (None, ''))[1]
        return default if self.phase == 'B' else self._values.get(id, default)

    def getSettings(self):
        """xbmcaddon.Settings over this handle's own settings object (legacy Addon.cpp:84-87): its
        getters need the settings loaded, so they raise in both reload phases (research F11)."""
        self.calls.append(('getSettings', None))
        handle = self

        class Settings(object):
            def _get(self, method, key):
                kind = {'getBool': 'boolean', 'getInt': 'integer', 'getNumber': 'number', 'getString': 'string'}[method]
                if handle.phase is not None:
                    return handle._registry._wrong_type('Invalid setting type "%s" for "%s"' % (kind, key))
                return handle._typed(TYPED_GETTER[kind], key)

            def getBool(self, key): return self._get('getBool', key)

            def getInt(self, key): return self._get('getInt', key)

            def getNumber(self, key): return self._get('getNumber', key)

            def getString(self, key): return self._get('getString', key)
        return Settings()

    # --- setters ------------------------------------------------------------------------------------
    def setSetting(self, id, value):
        # Kodi's binding converts the value before the call (interfaces/python/swig.cpp:95-141).
        try: value = _text(value, 'value', 'setSetting')
        except TypeError as e: return self._registry._wrong_type(str(e))
        self.calls.append(('setSetting', id))
        if self._load():                                         # CAddon::UpdateSetting
            kind = self._kind(id)
            if kind is None:
                self._kinds[id] = 'string'                       # an undefined id is added
                self._values[id] = value
            else:
                parsed = _from_text(kind, value)
                if parsed is not None: self._values[id] = _to_text(parsed)
        self._save()

    def _typed_set(self, method, id, value, kinds):
        if not isinstance(value, kinds) or (kinds == (int,) and isinstance(value, bool)):
            raise TypeError('%s: value must be %s, not %s' % (method, kinds[0].__name__, type(value).__name__))
        self.calls.append((method, id))
        if not self._load(): return self._registry._wrong_type()
        kind = self._kind(id)
        if kind is None: self._kinds[id] = {'setSettingBool': 'boolean', 'setSettingInt': 'integer',
                                            'setSettingNumber': 'number', 'setSettingString': 'string'}[method]
        elif TYPED_GETTER.get(kind) != 'get' + method[3:]: return self._registry._wrong_type()
        self._values[id] = _to_text(value)
        self._save()
        return True

    def setSettingBool(self, id, value):
        return self._typed_set('setSettingBool', id, value, (bool,))

    def setSettingInt(self, id, value):
        return self._typed_set('setSettingInt', id, value, (int,))

    def setSettingNumber(self, id, value):
        return self._typed_set('setSettingNumber', id, value, (int, float))

    def setSettingString(self, id, value):
        return self._typed_set('setSettingString', id, value, (str,))

    def _save(self):
        return self._registry._save(self)

    # --- the rest of the object, minimal ------------------------------------------------------------
    def getAddonInfo(self, key):
        return {'id': self._registry.addon_id, 'name': self._registry.addon_id}.get(key, '')

    def getLocalizedString(self, number):
        return ''


class FaithfulAddons(object):
    """xbmcaddon.Addon for the add-on, as Kodi 21.3 runs it.
      Addon(id)   builds a NEW FaithfulAddon on every call (AddonManager.cpp:630-655: CAddonBuilder::
                  Generate; GetRunningInstance() is null for a Python add-on) and registers it last
                  (AddToUpdateableAddons, AddonManager.cpp:183-187). Python freeing the object
                  unregisters it (~Addon, legacy Addon.cpp:74-77): the registry keeps weak references.
                  Another id: Kodi's 'Unknown addon id' (RuntimeError); so does every call while
                  `refuse_new` is set.
      disk        the user settings file, {id: text}. A save rewrites it from the writer's values.
      saves       [(writer number, ids whose text changed)], one entry per save, changed or not.
      built       handles built; parses: settings.xml parses (first loads and reloads).
      kodi_log    the lines Kodi itself writes, (text, level): 'EXCEPTION: Invalid setting type' for
                  every failed typed call, even one Python catches (PythonSwig.cpp.template:221-224).
      events      'disk', 'reload #n', 'callbacks queued': each save's steps, in order.
      on_reload   optional, called with 'A' then 'B' while the reloaded handle is in that phase, on the
                  saver's thread (the setter has released Python's lock there: legacy Addon.cpp:132).
    A save (CAddon::SaveSettings, Addon.cpp:375-406): the file is rewritten, then the OLDEST live handle
    alone is rebuilt in place (CAddonMgr::ReloadSettings, AddonManager.cpp:214-225; LoadSettings(true),
    Addon.cpp:267-322), then one onSettingsChanged is queued per monitor() (XBPython.cpp:324-333).
    deliver() is Kodi inside a wait: it runs the callbacks queued for monitors the calling thread made
    (python/CallbackHandler.cpp:33-45; legacy CallbackHandler.cpp:64-110) and logs an exception one
    raises instead of passing it on (legacy CallbackHandler.cpp:97-104).
    Switches: torn_next_load (the next load reads a half-written user file: Kodi logs 'failed to load
    addon settings', the handle serves the defaults silently and its saves are skipped, Addon.cpp:
    346-367, 377-378); fail_definitions (settings.xml can't be read: the handle stays without
    settings, typed getters raise and getSetting returns '' for good, Addon.cpp:274-279, 295-312).
    Not modelled: building or freeing a handle waits on the add-on manager's lock while a reload runs
    (AddonManager.cpp:185, 216, 635); here it doesn't wait, and reads the same thing, because the file
    is written before the reload starts. Nor: a setter going into an open settings dialog instead of
    saving (legacy Addon.cpp UpdateSettingInActiveDialog); Kodi's unlocked microseconds inside the
    reload (CSettingsManager::Clear against a getter), which Python can't catch anyway."""

    def __init__(self, user=None, addon_id=ADDON_ID, kodi_log=None):
        self.addon_id = addon_id
        self.disk = dict(user or {})
        self.saves, self.events = [], []
        self.kodi_log = [] if kodi_log is None else kodi_log
        self.built = self.parses = 0
        self.on_reload = None
        self.torn_next_load = self.fail_definitions = self.refuse_new = False
        self._handles, self._monitors, self._queued = [], [], []

    def log(self, text, level=KODI_LOG['LOGERROR']):
        self.kodi_log.append((text, level))

    def Addon(self, id=None):
        if self.refuse_new or (id is not None and id != self.addon_id):
            self.log("EXCEPTION: Unknown addon id '%s'." % (id,))
            raise RuntimeError("Unknown addon id '%s'." % (id,))
        self.built += 1
        handle = FaithfulAddon(self, self.built)
        self._handles.append(weakref.ref(handle))
        return handle

    def live(self):
        """The handles Python still holds, oldest first (Kodi's m_updateableAddons)."""
        return [h for h in (ref() for ref in self._handles) if h is not None]

    def first(self):
        """The handle a save rebuilds: the oldest live one, or None."""
        live = self.live()
        return live[0] if live else None

    @contextlib.contextmanager
    def hold(self, phase, handle=None):
        """Keep `handle` (default: the oldest live one) in reload phase 'A' or 'B' while the code under
        test runs: the deterministic way to put a read inside Kodi's window."""
        target = self.first() if handle is None else handle
        old, target.phase = target.phase, phase
        try: yield target
        finally: target.phase = old

    def kodi_errors(self, text='EXCEPTION: Invalid setting type'):
        return [line for line, _level in self.kodi_log if line == text]

    # --- what Kodi does ---------------------------------------------------------------------------
    def _wrong_type(self, text='Invalid setting type'):
        self.log('EXCEPTION: %s' % text)
        raise TypeError(text)

    def _read(self, handle):
        """One full load of `handle`: the definitions, then the user file."""
        self.parses += 1
        if self.fail_definitions:
            handle._broken, handle._values = True, None
            self.log('CAddon[%s]: failed to initialize addon settings' % self.addon_id)
            return False
        if self.torn_next_load:
            self.torn_next_load = False
            handle._values, handle._saveable = {}, False
            self.log('CAddon[%s]: failed to load addon settings from <the user file>' % self.addon_id)
        else:
            handle._values, handle._saveable = dict(self.disk), True
        return True

    def _save(self, writer):
        if writer._values is None or not writer._saveable: return False   # HasSettingsToSave
        old, self.disk = self.disk, dict(writer._values)
        changed = sorted(k for k in set(old).union(self.disk) if old.get(k) != self.disk.get(k))
        self.saves.append((writer.number, changed))
        self.events.append('disk')
        target = self.first()
        if target is not None: self._reload(target)
        self.events.append('callbacks queued')
        self._queued.extend(self._monitors)
        return True

    def _reload(self, handle):
        self.events.append('reload #%d' % handle.number)
        if handle._broken: return                                          # LoadSettings: false at once
        if handle._values is None:                                         # never loaded: a first load
            self._read(handle)
            return
        held = handle.phase
        try:
            handle.phase = 'A'
            if self.on_reload is not None: self.on_reload('A')
            if self.fail_definitions:
                self._read(handle)
                return
            handle.phase = 'B'
            if self.on_reload is not None: self.on_reload('B')
            self._read(handle)
        finally:
            handle.phase = held

    # --- onSettingsChanged ------------------------------------------------------------------------
    def monitor(self, obj):
        """Register `obj` as an xbmc.Monitor made on this thread; returns it."""
        self._monitors.append((obj, threading.get_ident()))
        return obj

    def pending(self):
        return len(self._queued)

    def deliver(self):
        """Kodi inside a wait on the calling thread: the queued onSettingsChanged of the monitors this
        thread made run now; an exception one raises is written to the log, never passed on. Returns how
        many ran."""
        ident, ran = threading.get_ident(), 0
        for entry in list(self._queued):
            if entry[1] != ident: continue
            self._queued.remove(entry)
            ran += 1
            try: entry[0].onSettingsChanged()
            except Exception as e:  # Kodi's CallbackHandler catches it (the add-on's code never sees it)
                self.log('EXCEPTION Thrown (PythonToCppException) : %s: %s' % (type(e).__name__, e))
        return ran


def install_settings(monkeypatch, registry, xbmcaddon_module, real_settings):
    """For one imports.71 test: xbmcaddon.Addon builds `registry`'s handles, and REAL_SETTINGS is
    `real_settings` in every loaded resources/lib module that has the name (the star imports copy it
    into most of them). monkeypatch undoes both afterwards. The test passes the stub module in: this
    module imports no Kodi stub."""
    monkeypatch.setattr(xbmcaddon_module, 'Addon', registry.Addon)
    for module in list(sys.modules.values()):
        path = getattr(module, '__file__', None)
        if path and os.path.abspath(path).startswith(ADDON_LIB + os.sep) and hasattr(module, 'REAL_SETTINGS'):
            monkeypatch.setattr(module, 'REAL_SETTINGS', real_settings)


class FaithfulWindows(object):
    """xbmcgui.Window for the add-on's properties, as Kodi 21.3 keeps them (interfaces/legacy/Window.cpp:
    605-634): Window(id) is an object over that window id's ONE property map (Window(10000) is Kodi's Home
    window, which every add-on and interpreter shares); `maps` is {window id: {key: value}}. Keys are
    lower-cased; a value is text (None is stored as '', bytes are decoded, anything else raises TypeError:
    interfaces/python/swig.cpp:95-141); clearProperty(key) is setProperty(key, ''); a missing key reads ''.
    Each getProperty hands back a NEW str, decoded from Kodi's copy of the text (Window::getProperty
    returns a std::string, interfaces/legacy/Window.cpp:615-623, which the binding decodes on every call:
    interfaces/python/typemaps/python.string.outtm:10-11), so code that compares two reads by identity
    fails here as in Kodi (fix stage of imports.71: the stand-in kept and returned the stored object).
    The repo-root stub stores nothing (its getProperty always returns '')."""

    def __init__(self):
        self.maps = {}

    def Window(self, existingWindowId=-1):
        return _FaithfulWindow(self.maps.setdefault(existingWindowId, {}))

    def get(self, key, window_id=10000):
        return self.maps.get(window_id, {}).get(key.lower(), '')


class _FaithfulWindow(object):
    def __init__(self, properties):
        self._properties = properties

    def setProperty(self, key, value):
        self._properties[_text(key, 'key', 'setProperty').lower()] = _text(value, 'value', 'setProperty')

    def getProperty(self, key):
        value = self._properties.get(_text(key, 'key', 'getProperty').lower(), '')
        return value.encode('utf-8', 'surrogateescape').decode('utf-8', 'surrogateescape')   # a new str, as Kodi's

    def clearProperty(self, key):
        self._properties[_text(key, 'key', 'clearProperty').lower()] = ''


def install_windows(monkeypatch, windows, xbmcgui_module):
    """xbmcgui.Window builds `windows`' objects for one test (monkeypatch undoes it). Code that kept a
    Window object from before (kodi.Properties made at import keeps self.window) still holds the stub's:
    build such objects after this."""
    monkeypatch.setattr(xbmcgui_module, 'Window', windows.Window)


STRINGS_PO = os.path.join(os.path.dirname(ADDON_LIB), 'language', 'resource.language.en_gb', 'strings.po')


def lib_strings(path=STRINGS_PO):
    """{id: text} of the add-on's own en_gb strings.po (msgctxt "#<id>" -> msgid, continuation lines
    joined): LANGUAGE for a rig. The repo-root stubs read the live add-on's file instead."""
    found, current, text, field = {}, None, None, None
    with open(path, encoding='utf-8') as fh:
        for raw in fh:
            line = raw.strip()
            if line.startswith('msgctxt "#'):
                current, field = int(line[len('msgctxt "#'):-1]), None
            elif line.startswith('msgid ') and current is not None:
                text, field = ast.literal_eval(line[len('msgid '):]), 'msgid'
            elif line.startswith('"') and field == 'msgid':
                text += ast.literal_eval(line)
            elif line.startswith('msgstr') and current is not None:
                found[current], current, field = text, None, None
    return found
