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

Nothing here imports Kodi or a Kodi stub; the only files read are the add-on's own sources.
"""
import ast
import math
import os
import types

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
