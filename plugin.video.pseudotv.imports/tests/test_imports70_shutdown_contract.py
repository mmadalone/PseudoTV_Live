# -*- coding: utf-8 -*-
"""imports.70, the repair after the TV test of 2026-11-19: no wait of the add-on may go on for ever.

What happened: the replay-end move began with service._shutdown(0), that is
xbmc.Monitor.waitForAbort(0). Kodi takes a timeout of 0 or less, or none at all, as "no
timeout" (xbmc/interfaces/legacy/Monitor.cpp:39-66; the default argument is -1, Monitor.h:289),
so the move thread stood there, holding the RESTART lock, until Kodi restarted. Every test
passed, because every stand-in for that wait returned at once on 0.

Three layers, so that this can't come back:
  1. the stand-in itself (kodi_faithful.FaithfulMonitor) keeps Kodi's rule and can't be swallowed
     (the tests marked S1-S6);
  2. the contract of the real services.Service._shutdown and the new Service._aborted, both
     compiled from services.py: a positive wait is passed to Kodi unchanged, a wait of 0 or less
     asks without waiting, _aborted never waits and keeps no books (S7-S13);
  3. a guard over the whole library (G1-G4): every waitForAbort / _shutdown call is read from
     the source, and its wait must be a fixed number above 0. A wait that can't be read as one
     ("dynamic") fails until it is on the reviewed list below, with the reason it can't be 0;
     and a census of the tests' own stand-ins for these two calls (G5): a new one fails until it
     is reviewed, so a forgiving fake can't slip back in.
"""
import ast
import collections
import os
import sys
import types
import xml.etree.ElementTree as ET

import pytest

import kodi_faithful
from kodi_faithful import FaithfulMonitor, FaithfulService, ForeverWait, KODI_LOG, positive_wait

HERE       = os.path.dirname(os.path.abspath(__file__))
ADDON      = os.path.dirname(HERE)
LIB        = os.path.join(ADDON, 'resources', 'lib')
ROOT_STUBS = os.path.join(os.path.dirname(ADDON), 'tests')

NAN = float('nan')
INF = float('inf')

# The two calls this file is about. Written out once: the census (G5) lists every place in the
# tests where one of the bare names stands as text.
WAITS = ('waitForAbort', '_shutdown')
W, S  = WAITS


class Bench(object):
    """The real Service._shutdown / _aborted (kodi_faithful.FaithfulService) on a clock of its
    own: `t`; Kodi starts to close at `closing_at`; `pending` is the add-on's own shutdown flag
    (`flag_reads` counts its reads); whatever is in `queued` is a change Kodi makes when time
    passes (it moves to `landed`)."""

    def __init__(self, t=1000.0):
        self.t, self.closing_at, self.pending, self.flag_reads = float(t), None, False, 0
        self.violations, self.queued, self.landed = [], ['a queued Kodi change'], []
        self.service = FaithfulService(self.advance, lambda: self.t, lambda: self.closing_at, self.flag, self.violations)
        self.monitor = self.service.monitor

    def advance(self, seconds):
        self.t += seconds
        self.landed.extend(self.queued)
        del self.queued[:]

    def flag(self):
        self.flag_reads += 1
        return self.pending

    def untouched(self, t=1000.0):
        """No time passed and nothing Kodi had queued landed."""
        return self.t == t and self.landed == [] and self.queued == ['a queued Kodi change']


@pytest.fixture
def bench():
    return Bench()


# === 1. the stand-in keeps Kodi's rule (S1-S6) =====================================================

NEVER_ENDS = [((), {}), ((0,), {}), ((0.0,), {}), ((-0.0,), {}), ((-1,), {}), ((-0.5,), {}), ((-0.0001,), {}),
              ((False,), {}), ((NAN,), {}), ((INF,), {}), ((-INF,), {}), ((2147483.648,), {}),
              ((), {'timeout': 0}), ((), {'timeout': -1})]


@pytest.mark.parametrize('args, kwargs', NEVER_ENDS, ids=[repr(a + tuple(sorted(k.items()))) for a, k in NEVER_ENDS])
@pytest.mark.parametrize('closing', [False, True], ids=['running', 'kodi-closing'])
def test_the_fake_monitor_fails_on_a_wait_kodi_would_never_end(bench, args, kwargs, closing):
    """S1. No timeout (Kodi's default is -1), 0, anything below it, anything that rounds up to 0 ms,
    False, not-a-number, and more ms than Kodi's int holds: Kodi never comes back, so the stand-in
    raises, records it, and no time passes. Also while Kodi is closing, where Kodi itself would
    answer at once: the call is wrong at any other moment."""
    if closing: bench.closing_at = bench.t - 5
    with pytest.raises(ForeverWait) as caught:
        bench.monitor.waitForAbort(*args, **kwargs)
    assert 'Kodi would wait forever' in str(caught.value)
    assert len(bench.violations) == 1 and bench.violations[0].startswith('waitForAbort(')
    assert kodi_faithful.provoked() == bench.violations
    assert bench.untouched() and bench.monitor.waits == []


@pytest.mark.parametrize('args, kwargs', [((None,), {}), (('1',), {}), ((b'1',), {}), ((object(),), {}), (([1],), {}),
                                          ((1, 2), {}), ((), {'wait': 1}), ((1,), {'timeout': 1}), ((), {'timeout': None})])
def test_the_fake_monitor_rejects_what_kodi_rejects(bench, args, kwargs):
    """S2. None, text, anything that isn't a number, a second argument, another keyword: Kodi's
    binding raises TypeError. That is an Exception the add-on could swallow, so it is recorded
    like a refused wait."""
    with pytest.raises(TypeError):
        bench.monitor.waitForAbort(*args, **kwargs)
    assert len(bench.violations) == 1 and kodi_faithful.provoked() == bench.violations
    assert bench.untouched() and bench.monitor.waits == []


def test_forever_wait_cannot_be_swallowed(bench):
    """S3. The add-on catches `Exception` around the move, the tick and its worker threads
    (services.py, pool.py): the refusal must pass through all of them."""
    assert issubclass(ForeverWait, BaseException) and not issubclass(ForeverWait, Exception)
    swallowed = []

    def as_the_addon_does():
        try: bench.monitor.waitForAbort(0)
        except Exception: swallowed.append(True)
    with pytest.raises(ForeverWait):
        as_the_addon_does()
    assert swallowed == [] and kodi_faithful.provoked() == ['waitForAbort(0)']


def test_a_refused_wait_is_on_the_record_even_when_something_swallows_it(bench):
    """A bare `except:` (or `except BaseException`) would still hide the exception: the record,
    which conftest.py checks after every test, does not depend on it."""
    try: bench.monitor.waitForAbort(-1)
    except BaseException: pass                                # noqa: B036, S110 - on purpose: the worst case
    assert bench.violations == ['waitForAbort(-1)'] and kodi_faithful.VIOLATIONS == ['waitForAbort(-1)']
    assert kodi_faithful.provoked() == ['waitForAbort(-1)'] and kodi_faithful.VIOLATIONS == []


@pytest.mark.parametrize('args, kwargs, seconds', [((0.0001,), {}, 0.0001), ((0.016,), {}, 0.016), ((0.05,), {}, 0.05),
                                                   ((0.5,), {}, 0.5), ((5,), {}, 5), ((True,), {}, 1.0),
                                                   ((), {'timeout': 0.25}, 0.25)])
def test_a_positive_wait_moves_the_clock_and_lets_kodi_act(bench, args, kwargs, seconds):
    """S4. Kodi rounds a timeout up to whole ms, so the smallest positive one is a real wait."""
    assert bench.monitor.waitForAbort(*args, **kwargs) is False
    assert bench.t == 1000.0 + seconds and bench.landed == ['a queued Kodi change']
    assert bench.monitor.waits == [args[0] if args else kwargs['timeout']] and bench.violations == []


def test_a_wait_ends_when_kodi_closes(bench):
    """S5. Kodi's abort flag stays set: already closing answers True at once (no time passes,
    nothing queued lands); closing inside the wait ends it at that moment; closing exactly at its
    end counts too."""
    bench.closing_at = 1000.3
    assert bench.monitor.waitForAbort(0.5) is True
    assert bench.t == 1000.3 and bench.landed == ['a queued Kodi change']
    bench.queued.append('another change')
    assert bench.monitor.waitForAbort(0.5) is True and bench.monitor.waitForAbort(15) is True
    assert bench.t == 1000.3 and bench.queued == ['another change']
    assert bench.monitor.waits == [0.5, 0.5, 15]
    late = Bench()
    late.closing_at = 1000.5
    assert late.monitor.waitForAbort(0.25) is False and late.t == 1000.25
    assert late.monitor.waitForAbort(0.25) is True and late.t == 1000.5


def test_abort_requested_never_waits(bench):
    """S6. It only reads Kodi's flag: no time passes and nothing queued lands (Kodi delivers
    callbacks inside waitForAbort / xbmc.sleep only)."""
    assert bench.monitor.abortRequested() is False
    bench.closing_at = 1000.0
    assert bench.monitor.abortRequested() is True and bench.monitor.abortRequested() is True
    assert bench.monitor.checks == 3 and bench.monitor.waits == [] and bench.untouched()


def test_the_fake_monitor_fails_on_a_loop_that_never_waits(bench):
    """A rig's clock moves only inside a wait, so a loop that only asks would never end there
    (in Kodi it would spin without a pause). After SPIN_CAP questions in a row at one instant the
    stand-in refuses, as it refuses a wait Kodi never ends: a loud failure instead of a test run
    that hangs. Three such loops: abortRequested, the real Service._shutdown with a wait of 0 (it
    only asks since the repair), and a wait whose "Kodi is closing" answer is ignored. Time
    passing in between starts the count again."""
    cap = kodi_faithful.SPIN_CAP
    assert 100 <= cap <= 10000
    for _ in range(cap): assert bench.monitor.abortRequested() is False
    with pytest.raises(ForeverWait) as caught:
        bench.monitor.abortRequested()
    assert 'a loop that never waits' in str(caught.value)
    assert bench.violations == ['abortRequested(): %d calls in a row while no time passed' % (cap + 1)]
    assert kodi_faithful.provoked() == bench.violations and bench.untouched() and bench.monitor.checks == cap

    asking = Bench()
    with pytest.raises(ForeverWait):
        for _ in range(cap + 5): assert asking.service._shutdown(0) is False
    assert asking.monitor.checks == cap and asking.untouched() and kodi_faithful.provoked() == asking.violations

    closing = Bench()
    closing.closing_at = closing.t
    with pytest.raises(ForeverWait):
        for _ in range(cap + 5): assert closing.monitor.waitForAbort(0.5) is True
    assert closing.monitor.waits == [0.5] * cap and closing.untouched()
    assert kodi_faithful.provoked() == ['waitForAbort(0.5): %d calls in a row while no time passed' % (cap + 1)]

    waiting = Bench()                                         # a loop that waits may turn as often as it likes
    for _ in range(3 * cap):
        assert waiting.service._shutdown(0.016) is False and waiting.monitor.abortRequested() is False
    assert waiting.violations == [] and len(waiting.monitor.waits) == 3 * cap
    assert waiting.service._shutdown(0.016) is False              # ... and the count starts again after each wait:
    for _ in range(cap - 1): waiting.monitor.abortRequested()     # SPIN_CAP questions at one instant are still answered
    assert waiting.service._aborted() is False and waiting.violations == []


def test_the_standstill_guard_counts_every_kind_of_call(bench):
    """The guard against a loop that never waits is one count per rig clock, shared by the
    monitor and the rig's own fakes (kodi_faithful.Standstill): such a loop fails within SPIN_CAP
    calls whatever mix of calls it makes. 2026-11-19 23:25: a deliberately broken stand-in whose
    waits let no time pass made the watch loops record their calls for ever, about 45 MB a second,
    until RAM and swap were full and this machine froze; the monitor alone saw none of those calls.
    A wait that lets time pass starts the count again; each refusal is recorded."""
    cap = kodi_faithful.SPIN_CAP
    assert 100 <= cap <= 10000                                # this test loops that often
    shared = kodi_faithful.Standstill(lambda: bench.t, bench.violations)
    service = FaithfulService(bench.advance, lambda: bench.t, lambda: bench.closing_at, bench.flag, bench.violations,
                              standstill=shared)
    assert service.monitor.standstill is shared and bench.monitor.standstill is not Bench().monitor.standstill
    kinds = (service.monitor.abortRequested, lambda: shared('GetItem'), lambda: shared('getCondVisibility'),
             lambda: service.log('a line'), service._aborted)
    for n in range(cap): kinds[n % len(kinds)]()              # one count each (_aborted: Kodi's flag; `pending` is no rig call)
    assert shared.count == cap and bench.violations == [] and bench.untouched()
    with pytest.raises(ForeverWait) as caught:
        shared('log')
    assert 'a loop that never waits' in str(caught.value)
    assert bench.violations == ['log: %d calls in a row while no time passed' % (cap + 1)]
    assert kodi_faithful.provoked() == bench.violations and bench.untouched()
    del bench.violations[:]
    for _ in range(3):                                        # a wait moves the clock: SPIN_CAP calls more at the next instant
        assert service._shutdown(0.016) is False
        for n in range(cap - 1): kinds[n % len(kinds)]()
    assert bench.violations == [] and service.monitor.waits == [0.016] * 3 and bench.t == pytest.approx(1000.048)


def test_the_test_session_has_a_memory_cap():
    """conftest.py caps the address space of the test process (RLIMIT_AS, on Linux) at 3 GB or
    below what was already set: a test that loops for ever while it records calls ends with
    MemoryError instead of filling RAM and swap, as one did on 2026-11-19 at 23:25 on this machine,
    which also runs Kodi."""
    resource = pytest.importorskip('resource')
    if not sys.platform.startswith('linux'): pytest.skip('the cap is set on Linux only')
    soft, _hard = resource.getrlimit(resource.RLIMIT_AS)
    assert soft != resource.RLIM_INFINITY and 0 < soft <= 3 * 1024 ** 3


def _conftest_module(request):
    """This folder's conftest.py, as pytest loaded it."""
    path = os.path.join(HERE, 'conftest.py')
    found = [p for p in request.config.pluginmanager.get_plugins() if getattr(p, '__file__', None) == path]
    assert len(found) == 1
    return found[0]


def test_the_memory_cap_is_only_ever_lowered(request, monkeypatch):
    """conftest.py's pytest_configure, given the limits Linux can report (soft <= hard): no limit
    or a higher soft limit -> the cap, with the hard limit left as it was; a soft limit at or
    below the cap stays. (The test above only sees the limit in force, which a runner's own
    `ulimit -v` may have set.)"""
    resource = pytest.importorskip('resource')
    if not sys.platform.startswith('linux'): pytest.skip('the cap is set on Linux only')
    conftest = _conftest_module(request)
    inf, cap, gb = resource.RLIM_INFINITY, conftest.TEST_MEMORY_CAP, 1024 ** 3
    assert cap == 3 * gb
    cases = [((inf, inf), [(cap, inf)]), ((8 * gb, inf), [(cap, inf)]), ((8 * gb, 16 * gb), [(cap, 16 * gb)]),
             ((cap + 1, cap + 1), [(cap, cap + 1)]), ((cap, inf), []), ((2 * gb, inf), []), ((2 * gb, 2 * gb), [])]
    for limits, expected in cases:
        set_to = []
        monkeypatch.setattr(resource, 'getrlimit', lambda what, limits=limits: limits if what == resource.RLIMIT_AS else None)
        monkeypatch.setattr(resource, 'setrlimit', lambda what, new, set_to=set_to: set_to.append((what, new)))
        conftest.pytest_configure(request.config)
        assert set_to == [(resource.RLIMIT_AS, new) for new in expected], limits


def test_the_blocking_stand_in_stands_still_until_kodi_closes():
    """For the test with real threads (test_imports70_replay_end.py): with a `gate`, a wait Kodi
    would never end really blocks, as on the TV, and answers True once the test opens the gate
    (Kodi closing). It is recorded all the same."""
    import threading
    gate, answers, violations = threading.Event(), [], []
    monitor = FaithfulMonitor(lambda s: None, lambda: 0.0, lambda: None, violations, gate=gate)
    thread = threading.Thread(target=lambda: answers.append(monitor.waitForAbort(0)))
    thread.start()
    thread.join(0.3)
    assert thread.is_alive() and answers == []
    gate.set()
    thread.join(10)
    assert not thread.is_alive() and answers == [True]
    assert violations == ['waitForAbort(0)'] and kodi_faithful.provoked() == ['waitForAbort(0)']


def _autouse_fixture(path, name):
    """The body of the autouse fixture `name` in a test file, as text per statement."""
    with open(path, encoding='utf-8') as fh: tree = ast.parse(fh.read())
    found = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name]
    assert len(found) == 1, (path, name)
    assert [ast.unparse(d) for d in found[0].decorator_list] == ['pytest.fixture(autouse=True)'], (path, name)
    return [ast.unparse(s) for s in found[0].body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]


def test_the_checks_after_every_test_are_in_place():
    """A refused wait is recorded as well as raised (a worker thread, a bare `except:`), so it
    counts only if someone looks: conftest.py after every test of the suite (kodi_faithful.check),
    and the two glue rigs' own checks (their check_rigs: the rig's record and the RESTART lock)."""
    assert _autouse_fixture(os.path.join(HERE, 'conftest.py'), 'no_wait_kodi_would_never_end') == \
        ['del kodi_faithful.VIOLATIONS[:]', 'yield', 'kodi_faithful.check()']
    for name in ('test_imports70_replay_end.py', 'test_imports62_restart_glue.py'):
        assert _autouse_fixture(os.path.join(HERE, name), 'kodi_faithful_checks') == \
            ['del RIGS[:]', 'yield', 'rigs = RIGS[:]', 'del RIGS[:]', 'check_rigs(rigs)'], name
    kodi_faithful.check()                                     # nothing on the record: passes
    kodi_faithful.record('waitForAbort(0)')
    with pytest.raises(AssertionError, match='a wait Kodi would never end'):
        kodi_faithful.check()
    assert kodi_faithful.VIOLATIONS == []                     # reported once, then off the record
    seen = []
    kodi_faithful.record('waitForAbort(-1)', seen)
    assert seen == ['waitForAbort(-1)'] and kodi_faithful.provoked() == seen


@pytest.mark.parametrize('wait', [0.016, 0.5, 1, 15, True])
def test_positive_wait_passes_what_kodi_would_time(wait):
    assert positive_wait(wait) is wait and kodi_faithful.VIOLATIONS == []


@pytest.mark.parametrize('wait', [None, 0, 0.0, -1, False, NAN, '1'])
def test_positive_wait_refuses_the_rest(wait):
    """The simpler fakes of the other rigs can't tell a missing timeout from None: both refused."""
    seen = []
    with pytest.raises(ForeverWait):
        positive_wait(wait, S, seen)
    assert len(seen) == 1 and seen[0].startswith('_shutdown(') and kodi_faithful.provoked() == seen


def test_the_other_rigs_wait_fakes_refuse_a_wait_kodi_would_never_end():
    """imports.67's Player.monitor and _chkIdle service, and imports.64's service: each used to
    answer at once whatever the wait."""
    import test_imports64_upnext as upnext
    import test_imports67_livetv_subtitles as livesubs
    wait = livesubs.Wait(kodi=None)
    for bad in (None, 0, -1):
        with pytest.raises(ForeverWait): wait.waitForAbort(bad)
    with pytest.raises(ForeverWait): wait.waitForAbort()
    with pytest.raises(ForeverWait): livesubs._answered_shutdown(iter([False]))(0)
    assert livesubs._answered_shutdown(iter([False]))(0.5) is False
    rig = upnext.Rig()
    with pytest.raises(ForeverWait): rig._shutdown(0)
    assert wait.waits == [] and rig.waits == []
    assert len(kodi_faithful.provoked()) == 6


def test_kodis_log_levels():
    """xbmc/commons/ilog.h (Kodi 19+). The add-on's logger gates on the number 3 (ERROR): the
    repo-root xbmc stub still has Kodi 18's numbers (LOGWARNING 3), so tests of that gate must
    use these."""
    assert KODI_LOG == {'LOGDEBUG': 0, 'LOGINFO': 1, 'LOGWARNING': 2, 'LOGERROR': 3, 'LOGFATAL': 4, 'LOGNONE': 5}


# === 2. the real Service._shutdown and Service._aborted (S7-S13) ==================================

# Every wait a _shutdown call site passes (G3 keeps this list equal to the library's).
CALL_SITE_WAITS = [0.016, 0.05, 0.15, 0.25, 0.5, 1.0, 15]
NOT_POSITIVE    = [0, 0.0, -0.0, -1, -0.001, False, NAN, -INF]


def test_the_bench_runs_the_real_methods(bench):
    for name in (S, '_aborted'):
        code = getattr(bench.service, name).__func__.__code__
        assert (os.path.basename(code.co_filename), code.co_name) == ('services.py', name)
    assert isinstance(bench.monitor, FaithfulMonitor)


# The smallest waits Kodi still times (it rounds a timeout up to whole ms): the add-on keeps no
# lower bound of its own (imports.70, the fix stage: `if wait > 0`, not a larger number).
SMALL_WAITS = [0.001, 1e-6]


@pytest.mark.parametrize('wait', CALL_SITE_WAITS + SMALL_WAITS)
def test_shutdown_passes_a_positive_wait_through_unchanged(bench, wait):
    """S7. For the 14 call sites that wait a positive time nothing changes: one waitForAbort with
    exactly that wait, then the add-on's own flag; no extra question, no log line. The same for
    the smallest waits Kodi still times (SMALL_WAITS)."""
    assert bench.service._shutdown(wait) is False
    assert bench.monitor.waits == [wait] and bench.monitor.waits[0] is wait
    assert bench.t == 1000.0 + wait and bench.landed == ['a queued Kodi change']
    assert bench.monitor.checks == 0 and bench.flag_reads == 1
    assert bench.service.logged == [] and bench.service.pendingShutdown is False


def test_shutdown_default_wait_is_the_service_interval(bench):
    """S8. The service's main loop calls _shutdown() with no argument."""
    assert bench.service._shutdown() is False
    assert bench.monitor.waits == [5.0] and bench.t == 1005.0
    assert kodi_faithful.lib_constant('constants.py', 'SERVICE_INTERVAL') == 5.0


@pytest.mark.parametrize('wait', NOT_POSITIVE, ids=[repr(w) for w in NOT_POSITIVE])
def test_shutdown_never_passes_a_non_positive_wait_to_kodi(bench, wait):
    """S9. The repair: a wait of 0 or less asks without waiting. Kodi is never handed it (it
    would not come back: the TV test of 2026-11-19), no time passes, nothing queued lands."""
    assert bench.service._shutdown(wait) is False
    assert bench.monitor.waits == [] and bench.monitor.checks == 1 and bench.flag_reads == 1
    assert bench.untouched() and bench.violations == [] and bench.service.logged == []


@pytest.mark.parametrize('wait', [0, -1])
def test_shutdown_check_now_sees_kodi_closing(bench, wait):
    """S10. Asked without waiting, it still answers and keeps its books as a positive wait does."""
    bench.closing_at = 1000.0
    assert bench.service._shutdown(wait) is True and bench.service.pendingShutdown is True
    assert bench.service.logged == [('_shutdown, pendingShutdown = True, wait = %s' % wait, KODI_LOG['LOGDEBUG'])]
    assert bench.monitor.waits == [] and bench.untouched()
    assert bench.service._shutdown(wait) is True and len(bench.service.logged) == 1       # no change: no second line


def test_shutdown_check_now_sees_the_addons_own_flag(bench):
    """S11. The add-on's own shutdown flag counts when asked without waiting, as it always did."""
    bench.pending = True
    assert bench.service._shutdown(0) is True and bench.service.pendingShutdown is True
    assert bench.monitor.waits == [] and bench.monitor.checks == 1 and bench.untouched()
    assert bench.service.logged == [('_shutdown, pendingShutdown = True, wait = 0', 0)]


def test_shutdown_logs_each_change_once(bench):
    """S12. The bookkeeping and its debug line are the ones of the first build."""
    answers = []
    for pending in (False, True, True, False):
        bench.pending = pending
        answers.append(bench.service._shutdown(0.5))
    assert answers == [False, True, True, False] and bench.service.pendingShutdown is False
    assert bench.service.logged == [('_shutdown, pendingShutdown = True, wait = 0.5', 0),
                                    ('_shutdown, pendingShutdown = False, wait = 0.5', 0)]
    assert bench.monitor.waits == [0.5] * 4 and bench.t == 1002.0


def test_shutdown_reads_both_signals_on_a_positive_wait(bench):
    """S13. Either signal is enough, and the wait is made (and the flag read) whichever answers."""
    bench.pending = True
    assert bench.service._shutdown(0.5) is True and bench.monitor.waits == [0.5] and bench.t == 1000.5
    other = Bench()
    other.closing_at = 1000.2
    assert other.service._shutdown(0.5) is True and other.t == 1000.2 and other.flag_reads == 1
    assert other.service.logged == [('_shutdown, pendingShutdown = True, wait = 0.5', 0)]


@pytest.mark.parametrize('closing, pending, answer', [(False, False, False), (True, False, True), (False, True, True),
                                                      (True, True, True)])
def test_aborted_asks_without_waiting_and_keeps_no_books(bench, closing, pending, answer):
    """The move's own question (Service._aborted): Kodi closing or the add-on's own flag, read
    once each. No wait, no time, nothing queued lands, `pendingShutdown` untouched and no log
    line, so any thread may ask at any time."""
    if closing: bench.closing_at = 999.0
    bench.pending = pending
    assert bench.service._aborted() is answer
    assert bench.monitor.waits == [] and bench.monitor.checks == 1 and bench.flag_reads == 1
    assert bench.untouched() and bench.service.pendingShutdown is False and bench.service.logged == []


def _service_method(name):
    found = kodi_faithful.method_node('services.py', 'Service', name)
    assert found is not None, name
    return found


def test_the_two_methods_are_written_as_designed():
    """The positive wait is the first build's statement, unchanged, under `if wait > 0:` (NaN and
    everything else take the other branch); the other branch and _aborted ask the same two
    signals through abortRequested."""
    shutdown = _service_method(S)
    assert [a.arg for a in shutdown.args.args] == ['self', 'wait']
    assert [ast.unparse(d) for d in shutdown.args.defaults] == ['SERVICE_INTERVAL']
    first = shutdown.body[0]
    assert isinstance(first, ast.If) and ast.unparse(first.test) == 'wait > 0'
    assert [ast.unparse(s) for s in first.body] == \
        ['pendingShutdown = any([self.monitor.waitForAbort(wait), PROPERTIES.isPendingShutdown()])']
    assert [ast.unparse(s) for s in first.orelse] == ['pendingShutdown = self._aborted()']
    assert [ast.unparse(s) for s in shutdown.body[1:]] == [
        "if self.pendingShutdown != pendingShutdown:\n    self.pendingShutdown = pendingShutdown\n"
        "    self.log('_shutdown, pendingShutdown = %s, wait = %s' % (self.pendingShutdown, wait))",
        'return self.pendingShutdown']
    aborted = _service_method('_aborted')
    assert [a.arg for a in aborted.args.args] == ['self']
    assert [ast.unparse(s) for s in aborted.body] == \
        ['return any([self.monitor.abortRequested(), PROPERTIES.isPendingShutdown()])']
    text = kodi_faithful.lib_source('services.py')[0]
    for node in (shutdown, aborted):
        segment = ast.get_source_segment(text, node)
        assert 'imports.70' in segment and 'Monitor' in segment
        assert [n for n in ast.walk(node) if isinstance(n, (ast.BinOp, ast.AugAssign)) and isinstance(n.op, ast.BitOr)] == []


# === 3. the guard over the whole library (G1-G4) ===================================================

# What `from globals import *` (variables.py, constants.py) puts into every library module.
STAR_MODULES = ('constants.py', 'variables.py')


def library_sources():
    """{path under resources/lib: text} of every .py there."""
    sources = {}
    for folder, _dirs, files in os.walk(LIB):
        for name in sorted(files):
            if not name.endswith('.py'): continue
            path = os.path.join(folder, name)
            with open(path, encoding='utf-8') as fh: sources[os.path.relpath(path, LIB)] = fh.read()
    return sources


class _File(object):
    """One parsed source: each node's parent, its owner ('Class.method', '<module>') and the
    functions around it, and which string constants are docstrings."""

    def __init__(self, text):
        self.tree = ast.parse(text, feature_version=(3, 8))
        self.parent = {child: node for node in ast.walk(self.tree) for child in ast.iter_child_nodes(node)}
        self.docstrings = set()
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
                first = node.body[0]
                if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                    self.docstrings.add(first.value)

    def around(self, node):
        """(owner text, the functions around `node`, innermost first)."""
        names, functions = [], []
        while node in self.parent:
            node = self.parent[node]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                functions.append(node)
                names.append(getattr(node, 'name', 'lambda'))
            elif isinstance(node, ast.ClassDef):
                names.append(node.name)
        return '.'.join(reversed(names)) or '<module>', functions

    def own(self, scope):
        """The nodes of one scope (a function or the module), without what nested functions and
        classes contain."""
        todo = list(scope.body) if isinstance(scope.body, list) else [scope.body]
        while todo:
            node = todo.pop()
            yield node
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)): continue
            todo.extend(ast.iter_child_nodes(node))

    def bindings(self, scope, name):
        """Whatever gives `name` a value in that scope: the value of a plain `name = <expression>`,
        ('from', module, original name) for `from module import name`, and None for any other way
        (a loop, another import, global, a def ...)."""
        found = []
        for node in self.own(scope):
            if isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, (ast.Store, ast.Del)):
                holder = self.parent[node]
                plain = isinstance(holder, ast.Assign) and holder.targets == [node]
                found.append(holder.value if plain else None)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name:
                found.append(None)
            elif isinstance(node, (ast.Global, ast.Nonlocal)) and name in node.names:
                found.append(None)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    if (alias.asname or alias.name).split('.')[0] != name: continue
                    followed = isinstance(node, ast.ImportFrom) and node.level == 0 and node.module
                    found.append(('from', node.module, alias.name) if followed else None)
            elif isinstance(node, ast.ExceptHandler) and node.name == name:
                found.append(None)
        return found


def _parameters(function):
    a = function.args
    return [p.arg for p in a.posonlyargs + a.args + a.kwonlyargs] + [p.arg for p in (a.vararg, a.kwarg) if p is not None]


class WaitScan(object):
    """Reads every waitForAbort / _shutdown call out of `sources` ({file: text}).
      findings   what is wrong, as text: a wait Kodi would never end, a call the wait can't be
                 read from, an alias, a default of 0 ...
      dynamic    Counter of (wait name, file, owner, argument as written): waits that are not a
                 fixed number in the source. Not wrong by themselves; the caller compares them
                 with its reviewed list.
      strings    Counter of (wait name, file, owner): the bare name as text (getattr / setattr).
      values     {(wait name, file, owner, argument as written): number} of every fixed wait, and
                 `sites`, a Counter of the same keys (two calls in one method share a key).
      defaults   {(file, owner): number} of every `def _shutdown(self, wait=<number>)`."""

    def __init__(self, sources):
        self.files = {name: _File(text) for name, text in sources.items()}
        self.findings, self.values, self.defaults = [], {}, {}
        self.dynamic, self.strings, self.sites = collections.Counter(), collections.Counter(), collections.Counter()
        for name in sorted(self.files): self._scan(name, self.files[name])

    # --- what a wait's argument is worth -------------------------------------------------------
    def _single(self, name, values, file, depth):
        """One plain numeric assignment (also behind `from module import name`) -> its number;
        anything else is not a fixed number."""
        if len(values) != 1 or values[0] is None or depth > 8: return 'dynamic', None
        if isinstance(values[0], tuple):
            _from, module, original = values[0]
            other = self.files.get(module + '.py')
            if other is None: return 'dynamic', None
            return self._single(original, other.bindings(other.tree, original), module + '.py', depth + 1)
        return self._number(values[0], file, [], depth + 1)

    def _name(self, name, file, functions, depth):
        source = self.files[file]
        for function in functions:
            if name in _parameters(function): return 'dynamic', None
            found = source.bindings(function, name)
            if found: return self._single(name, found, file, depth)
        found = [(file, value) for value in source.bindings(source.tree, name)]
        if not found:                                                 # not the file's own: the star import
            for other in STAR_MODULES:
                if other != file and other in self.files:
                    found.extend((other, value) for value in self.files[other].bindings(self.files[other].tree, name))
        if not found: return 'bad', 'no assignment of %s found' % name
        if len(found) != 1: return 'dynamic', None
        return self._single(name, [found[0][1]], found[0][0], depth)

    def _number(self, node, file, functions, depth=0):
        """('value', number) | ('dynamic', None) | ('bad', why)."""
        if depth > 8: return 'dynamic', None
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool): return 'bad', 'a boolean'
            if node.value is None: return 'bad', 'None'
            if isinstance(node.value, (int, float)): return 'value', node.value
            return 'bad', 'not a number'
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            kind, value = self._number(node.operand, file, functions, depth)
            if kind != 'value': return kind, value
            return 'value', -value if isinstance(node.op, ast.USub) else value
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            left, right = self._number(node.left, file, functions, depth), self._number(node.right, file, functions, depth)
            for kind, value in (left, right):
                if kind != 'value': return kind, value
            try:
                if isinstance(node.op, ast.Add):  return 'value', left[1] + right[1]
                if isinstance(node.op, ast.Sub):  return 'value', left[1] - right[1]
                if isinstance(node.op, ast.Mult): return 'value', left[1] * right[1]
                return 'value', left[1] / right[1]
            except ZeroDivisionError: return 'bad', 'a division by zero'
        if isinstance(node, ast.Name): return self._name(node.id, file, functions, depth)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id + '.py' in self.files:
            module = node.value.id
            shadowed = any(module in _parameters(f) or self.files[file].bindings(f, module) for f in functions)
            if not shadowed:
                other = self.files[module + '.py']
                found = other.bindings(other.tree, node.attr)
                if not found: return 'bad', 'no assignment of %s.%s found' % (module, node.attr)
                return self._single(node.attr, found, module + '.py', depth)
        return 'dynamic', None

    def _judge(self, wait, node, file, owner, functions, where):
        kind, value = self._number(node, file, functions)
        text = ast.unparse(node)
        if kind == 'bad':
            self.findings.append('%s: %s(%s): %s' % (where, wait, text, value))
        elif kind == 'dynamic':
            self.dynamic[(wait, file, owner, text)] += 1
        elif kodi_faithful.timeout_ms(value)[0] != 'ms':
            self.findings.append('%s: %s(%s) = %r: Kodi would wait forever' % (where, wait, text, value))
        else:
            self.values[(wait, file, owner, text)] = value
            self.sites[(wait, file, owner, text)] += 1
        return kind, value

    # --- one file ------------------------------------------------------------------------------
    def _scan(self, file, source):
        for node in ast.walk(source.tree):
            line = getattr(node, 'lineno', 0)
            if isinstance(node, ast.Attribute) and node.attr in WAITS:
                owner, functions = source.around(node)
                where = '%s:%s %s' % (file, line, owner)
                call = source.parent.get(node)
                if not (isinstance(call, ast.Call) and call.func is node):
                    self.findings.append('%s: %s is used without being called (an alias hides its wait)' % (where, node.attr))
                    continue
                self._call(node.attr, call, file, owner, functions, where)
            elif isinstance(node, ast.Name) and node.id in WAITS:
                owner, _functions = source.around(node)
                self.findings.append('%s:%s %s: %s as a bare name (an alias hides its wait)' % (file, line, owner, node.id))
            elif isinstance(node, ast.Constant) and node.value in WAITS and isinstance(node.value, str) \
                    and node not in source.docstrings:
                owner, _functions = source.around(node)
                self.strings[(node.value, file, owner)] += 1
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in WAITS:
                owner, functions = source.around(node)
                owner = node.name if owner == '<module>' else '%s.%s' % (owner, node.name)
                where = '%s:%s %s' % (file, line, owner)
                if node.name == W:
                    self.findings.append("%s: the library must not define waitForAbort (Kodi's own is the one that waits)" % where)
                    continue
                self._default(node, file, owner, functions, where)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    if alias.name.split('.')[-1] in WAITS or alias.asname in WAITS:
                        self.findings.append('%s:%s: %s is imported by name (an alias hides its wait)' % (file, line, alias.name))

    def _call(self, wait, call, file, owner, functions, where):
        keyword = 'timeout' if wait == W else 'wait'
        if any(isinstance(a, ast.Starred) for a in call.args) or any(k.arg is None for k in call.keywords):
            self.findings.append('%s: %s(%s): its wait is hidden in * / **' % (where, wait, ', '.join(
                [ast.unparse(a) for a in call.args] + [ast.unparse(k) for k in call.keywords])))
            return
        given = list(call.args) + [k.value for k in call.keywords]
        if not given:
            # Service._shutdown() waits its default (checked where it is defined); Kodi's own
            # default is "no timeout".
            if wait == W: self.findings.append('%s: waitForAbort() with no timeout: Kodi would wait forever' % where)
            return
        if len(given) != 1 or [k.arg for k in call.keywords] not in ([], [keyword]):
            self.findings.append('%s: %s takes one wait, positional or %s=' % (where, wait, keyword))
            return
        self._judge(wait, given[0], file, owner, functions, where)

    def _default(self, function, file, owner, functions, where):
        names, defaults = [a.arg for a in function.args.args], function.args.defaults
        if 'wait' not in names:
            self.findings.append('%s: def _shutdown without a `wait` parameter' % where)
            return
        at = names.index('wait') - (len(names) - len(defaults))
        if at < 0: return                                             # no default: every caller must pass one
        kind, value = self._number(defaults[at], file, functions)
        if kind == 'value' and kodi_faithful.timeout_ms(value)[0] == 'ms': self.defaults[(file, owner)] = value
        else: self.findings.append('%s: def _shutdown(wait=%s): the default must be a fixed number above 0'
                                   % (where, ast.unparse(defaults[at])))


# Waits that are not a fixed number in the source, each with the reason it can't be 0 or less.
# A new one fails test G1 / G3 until it is reviewed and listed here; a listed one that is gone
# fails too, so the list can't go stale.
FALLBACK = ("uncalled pass-through: this module's fallback Service; nothing calls its _shutdown (every _shutdown call "
            "site passes a fixed wait above 0, G3), and the real services.Service is the one every live call reaches")
RPC_DELAY = "the RPC_Delay setting: default 1, minimum 1 (G4); a failed read gives float(None), which raises"
REVIEWED_DYNAMIC = {
    (W, 'services.py', 'Service._shutdown', 'wait'):
        'guarded: only under `if wait > 0:` (S7, S9); a wait of 0 or less asks _aborted() instead',
    (W, 'builder.py', 'Service._shutdown', 'wait'): FALLBACK,
    (W, 'cache.py', 'Service._shutdown', 'wait'): FALLBACK,
    (W, 'jsonrpc.py', 'Service._shutdown', 'wait'): FALLBACK,
    (W, 'library.py', 'Service._shutdown', 'wait'): FALLBACK,
    (W, 'multiroom.py', 'Service._shutdown', 'wait'): FALLBACK,
    (W, 'resources.py', 'Service._shutdown', 'wait'): FALLBACK,
    (W, 'fileaccess.py', 'FileLock.acquire', 'self.delay'):
        "FileLock.__init__'s `delay`, default LOCK_MAX_FILE_DELAY (0.5); no FileLock(...) call passes one (checked below)",
    (W, 'jsonrpc.py', 'JSONRPC.sendJSON', "float(SETTINGS.getSettingInt('RPC_Delay'))"): RPC_DELAY,
    (W, 'jsonrpc.py', 'JSONRPC.sendRPC', "float(SETTINGS.getSettingInt('RPC_Delay'))"): RPC_DELAY,
    (W, 'kodi.py', 'Dialog.doInfoMonitor', "float(self.settings.getSettingInt('RPC_Delay'))"): RPC_DELAY,
    (S, 'services.py', 'Player._restartWait', 'step'):
        "_restartWait's `step`, default 0.25; none of its callers passes one (checked below)",
}
# The bare names as text: only the thread pool's own private flag, read with getattr.
REVIEWED_STRINGS = {(S, 'pool.py', 'ExecutorPool.isShutdown'):
                    "getattr(self._executor, '_shutdown', False): ThreadPoolExecutor's flag, not the add-on's Service"}
# Every class that defines _shutdown, with its default wait.
SHUTDOWN_DEFAULTS = {('services.py', 'Service._shutdown'): 5.0, ('builder.py', 'Service._shutdown'): 1.0,
                     ('cache.py', 'Service._shutdown'): 1.0, ('jsonrpc.py', 'Service._shutdown'): 1.0,
                     ('library.py', 'Service._shutdown'): 1.0, ('multiroom.py', 'Service._shutdown'): 1.0,
                     ('resources.py', 'Service._shutdown'): 1.0}


@pytest.fixture(scope='module')
def library():
    return WaitScan(library_sources())


def _of(counter, wait):
    return {key: n for key, n in counter.items() if key[0] == wait}


def test_no_wait_for_abort_call_can_wait_forever(library):
    """G1. Every waitForAbort in the library is called with one wait that is a fixed number above
    0 in the source (a literal, or a constant of the function, the file, constants.py /
    variables.py or the named helper module), or it is on the reviewed list with its reason."""
    assert [f for f in library.findings if S not in f] == []
    assert _of(library.dynamic, W) == {k: 1 for k in REVIEWED_DYNAMIC if k[0] == W}
    assert _of(library.strings, W) == {}
    assert len(library.files) >= 80 and 'services.py' in library.files and os.path.join('parsers', 'MP4Parser.py') in library.files
    assert sum(n for key, n in library.sites.items() if key[0] == W) >= 30                # the scan saw them


def test_shutdown_call_sites_and_defaults(library):
    """G3. Every _shutdown call passes a fixed wait above 0, or none (the default); the one that
    doesn't is reviewed; every `def _shutdown` has a default above 0. So no call can hand a wait
    of 0 or less to a fallback Service either (those pass it straight to Kodi). To ask without
    waiting, the library calls Service._aborted()."""
    assert [f for f in library.findings if S in f] == []
    assert _of(library.dynamic, S) == {k: 1 for k in REVIEWED_DYNAMIC if k[0] == S}
    assert dict(library.strings) == {k: 1 for k in REVIEWED_STRINGS}
    assert library.defaults == SHUTDOWN_DEFAULTS
    passed = sorted({value for key, value in library.values.items() if key[0] == S})
    assert passed == CALL_SITE_WAITS                              # S7 tests exactly these
    assert sum(n for key, n in library.sites.items() if key[0] == S) >= 12                # the scan saw them


def test_the_reviewed_waits_are_what_their_reasons_say(library):
    """The reasons on the reviewed list, read from the source rather than taken on trust."""
    services, fileaccess = library.files['services.py'], library.files['fileaccess.py']
    # Service._shutdown: the pass-through sits in the body of `if wait > 0:`.
    calls = [n for n in ast.walk(kodi_faithful.method_node('services.py', 'Service', S))
             if isinstance(n, ast.Call) and ast.unparse(n.func) == 'self.monitor.waitForAbort']
    assert len(calls) == 1
    guard = _service_method(S).body[0]
    assert ast.unparse(guard.test) == 'wait > 0' and any(calls[0] in list(ast.walk(s)) for s in guard.body)
    # Player._restartWait(done, limit, step=0.25): no caller passes `step`.
    wait = kodi_faithful.method_node('services.py', 'Player', '_restartWait')
    assert [a.arg for a in wait.args.args] == ['self', 'done', 'limit', 'step']
    assert [ast.unparse(d) for d in wait.args.defaults] == ['0.25']
    callers = [n for n in ast.walk(services.tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
               and n.func.attr == '_restartWait']
    assert len(callers) == 3 and all(len(c.args) == 2 and not c.keywords for c in callers)
    for name, source in library.files.items():
        assert not [n for n in ast.walk(source.tree) if isinstance(n, ast.Constant) and n.value == '_restartWait'], name
    # FileLock(filename, timeout=..., delay=LOCK_MAX_FILE_DELAY): self.delay is that argument, and no
    # FileLock(...) call passes it.
    lock = [n for n in fileaccess.tree.body if isinstance(n, ast.ClassDef) and n.name == 'FileLock'][0]
    init = [n for n in lock.body if isinstance(n, ast.FunctionDef) and n.name == '__init__'][0]
    names = [a.arg for a in init.args.args]
    default = init.args.defaults[names.index('delay') - (len(names) - len(init.args.defaults))]
    assert ast.unparse(default) == 'LOCK_MAX_FILE_DELAY' and kodi_faithful.lib_constant('constants.py', 'LOCK_MAX_FILE_DELAY') == 0.5
    stores = [ast.unparse(fileaccess.parent[n]) for n in ast.walk(lock)
              if isinstance(n, ast.Attribute) and n.attr == 'delay' and isinstance(n.ctx, ast.Store)]
    assert stores == ['self.delay = delay']
    made = [n for source in library.files.values() for n in ast.walk(source.tree)
            if isinstance(n, ast.Call) and ast.unparse(n.func) == 'FileLock']
    assert len(made) == 4 and all(len(c.args) == 1 and not c.keywords for c in made)


def test_rpc_delay_can_never_be_zero(library):
    """G4. sendJSON / sendRPC / doInfoMonitor wait RPC_Delay seconds (Player.Open inside the
    replay-end move among them). The setting's default and minimum are 1, the dashboard and Kodi
    refuse anything below the minimum, and nothing in the library writes it."""
    root = ET.parse(os.path.join(ADDON, 'resources', 'settings.xml')).getroot()
    found = [s for s in root.iter('setting') if s.get('id') == 'RPC_Delay']
    assert len(found) == 1 and found[0].get('type') == 'integer'
    assert int(found[0].findtext('default')) >= 1
    assert int(found[0].findtext('constraints/minimum')) >= 1
    for name, source in library.files.items():
        writes = [n for n in ast.walk(source.tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                  and n.func.attr.startswith('setSetting') and n.args and isinstance(n.args[0], ast.Constant)
                  and n.args[0].value == 'RPC_Delay']
        assert writes == [], name


def _setting(key):
    """The <setting id=key> element of the add-on's settings.xml (exactly one)."""
    found = [s for s in ET.parse(os.path.join(ADDON, 'resources', 'settings.xml')).getroot().iter('setting')
             if s.get('id') == key]
    assert len(found) == 1, key
    return found[0]


def test_player_opens_pause_waits_the_settings_default_by_kodis_rules(bench, monkeypatch):
    """G4, run for real (imports.70, the fix stage). The replay-end move's Player.Open is
    jsonrpc.playerOpen -> sendJSON, which pauses RPC_Delay seconds after Kodi's reply, on the move
    thread, holding the RESTART lock; the glue rigs model that pause on their own clock. Here the
    real two methods run over the faithful monitor with the setting's default: one wait of that
    many seconds, passed to Kodi as it is. With 0 (what the shared stub's empty settings read)
    Kodi would never come back: refused."""
    import jsonrpc
    default = int(_setting('RPC_Delay').findtext('default'))
    reply = '{"id": 1, "jsonrpc": "2.0", "result": "OK"}'
    rpc = jsonrpc.JSONRPC(service=types.SimpleNamespace(monitor=bench.monitor))
    monkeypatch.setattr(jsonrpc.BUILTIN, 'executeJSONRPC', lambda request: reply)
    monkeypatch.setattr(jsonrpc.SETTINGS, 'getSettingInt', lambda key: {'RPC_Delay': default}[key])
    assert default >= 1 and rpc.playerOpen({'item': {'broadcastid': 1}}) is True
    assert bench.monitor.waits == [float(default)] and bench.t == 1000.0 + default and bench.violations == []
    monkeypatch.setattr(jsonrpc.SETTINGS, 'getSettingInt', lambda key: {'RPC_Delay': 0}[key])
    with pytest.raises(ForeverWait):
        rpc.playerOpen({'item': {'broadcastid': 1}})
    assert bench.t == 1000.0 + default and len(bench.violations) == 1
    assert kodi_faithful.provoked() == bench.violations


# --- G2: the guard itself --------------------------------------------------------------------------

CONSTANTS = 'CPU_CYCLE = 0.016\nSERVICE_INTERVAL = 5.0\nZERO = 0\nTWICE = 1\nTWICE = 2\nCOMPUTED = int("5")\n'
HELPERS   = 'POLL = 0.5\nNONE = 0.0\n'


def _scan(body, file='mod.py', extra=None):
    """A WaitScan over one synthetic module (plus constants.py and helpers.py)."""
    sources = {'constants.py': CONSTANTS, 'helpers.py': HELPERS, file: body}
    sources.update(extra or {})
    return WaitScan(sources)


GOOD = [
    'm.waitForAbort(0.5)',
    'm.waitForAbort(5)',
    'm.waitForAbort(0.0001)',
    'm.waitForAbort(timeout=1.0)',
    'm.waitForAbort(CPU_CYCLE)',
    'm.waitForAbort(helpers.POLL)',
    'm.waitForAbort(2 * 0.25)',
    'm.waitForAbort(+1)',
    'LOCAL = 3\nm.waitForAbort(LOCAL)',
    'from constants import CPU_CYCLE\nm.waitForAbort(CPU_CYCLE)',
    'from helpers import POLL as STEP\nm.waitForAbort(STEP)',
    'def f():\n    LOCAL = 3\n    def g():\n        return m.waitForAbort(LOCAL)\n',
    'def f():\n    """It calls waitForAbort and _shutdown."""\n    return m.waitForAbort(1)\n',
    's._shutdown()',
    's._shutdown(0.5)',
    's._shutdown(CPU_CYCLE)',
    's._shutdown(helpers.POLL)',
    's._shutdown(wait=1.0)',
    'class Service:\n    def _shutdown(self, wait=1.0):\n        return wait\n',
    'class Service:\n    def _shutdown(self, wait=SERVICE_INTERVAL):\n        return wait\n',
    'class Service:\n    def _shutdown(self, wait):\n        return wait\n',
    'class Service:\n    def _aborted(self):\n        return self.monitor.abortRequested()\n',
]

BAD = [
    ('m.waitForAbort(0)', 'Kodi would wait forever'),
    ('m.waitForAbort(-1)', 'Kodi would wait forever'),
    ('m.waitForAbort(0.0)', 'Kodi would wait forever'),
    ('m.waitForAbort(-0.0)', 'Kodi would wait forever'),
    ('m.waitForAbort(1 - 1)', 'Kodi would wait forever'),
    ('m.waitForAbort(1e999)', 'Kodi would wait forever'),
    ('m.waitForAbort(3000000)', 'Kodi would wait forever'),
    ('m.waitForAbort()', 'no timeout'),
    ('m.waitForAbort(None)', 'None'),
    ('m.waitForAbort(False)', 'a boolean'),
    ('m.waitForAbort(True)', 'a boolean'),
    ('m.waitForAbort("1")', 'not a number'),
    ('m.waitForAbort(timeout=0)', 'Kodi would wait forever'),
    ('m.waitForAbort(timeout=None)', 'None'),
    ('m.waitForAbort(wait=1)', 'takes one wait'),
    ('m.waitForAbort(1, 2)', 'takes one wait'),
    ('m.waitForAbort(*args)', 'hidden in'),
    ('m.waitForAbort(**kwargs)', 'hidden in'),
    ('m.waitForAbort(ZERO)', 'Kodi would wait forever'),
    ('m.waitForAbort(helpers.NONE)', 'Kodi would wait forever'),
    ('m.waitForAbort(helpers.MISSING)', 'no assignment'),
    ('m.waitForAbort(MISSING)', 'no assignment'),
    ('from helpers import NONE\nm.waitForAbort(NONE)', 'Kodi would wait forever'),
    ('m.waitForAbort(1 / 0)', 'division by zero'),
    ('w = m.waitForAbort\nw(0)', 'without being called'),
    ('from xbmc import waitForAbort', 'imported by name'),
    ('waitForAbort = None', 'bare name'),
    ('class Monitor:\n    def waitForAbort(self, timeout=0):\n        return False\n', 'must not define waitForAbort'),
    ('s._shutdown(0)', 'Kodi would wait forever'),
    ('s._shutdown(-1)', 'Kodi would wait forever'),
    ('s._shutdown(None)', 'None'),
    ('s._shutdown(wait=0)', 'Kodi would wait forever'),
    ('s._shutdown(timeout=1)', 'takes one wait'),
    ('s._shutdown(1, 2)', 'takes one wait'),
    ('s._shutdown(ZERO)', 'Kodi would wait forever'),
    ('f = s._shutdown\nf(0)', 'without being called'),
    ('class Service:\n    def _shutdown(self, wait=0):\n        return wait\n', 'default must be'),
    ('class Service:\n    def _shutdown(self, wait=ZERO):\n        return wait\n', 'default must be'),
    ('class Service:\n    def _shutdown(self, wait=None):\n        return wait\n', 'default must be'),
    ('class Service:\n    def _shutdown(self):\n        return False\n', 'without a `wait` parameter'),
]

DYNAMIC = [
    ('def f(delay):\n    return m.waitForAbort(delay)\n', (W, 'mod.py', 'f', 'delay')),
    ('class A:\n    def f(self):\n        return self.m.waitForAbort(self.delay)\n', (W, 'mod.py', 'A.f', 'self.delay')),
    ("m.waitForAbort(float(SETTINGS.getSettingInt('RPC_Delay')))",
     (W, 'mod.py', '<module>', "float(SETTINGS.getSettingInt('RPC_Delay'))")),
    ('m.waitForAbort(TWICE)', (W, 'mod.py', '<module>', 'TWICE')),
    ('m.waitForAbort(COMPUTED)', (W, 'mod.py', '<module>', 'COMPUTED')),
    ('from elsewhere import STEP\nm.waitForAbort(STEP)', (W, 'mod.py', '<module>', 'STEP')),
    ('import helpers as STEP\nm.waitForAbort(STEP)', (W, 'mod.py', '<module>', 'STEP')),
    ('def f(n):\n    m.waitForAbort(CPU_CYCLE * n)\n', (W, 'mod.py', 'f', 'CPU_CYCLE * n')),
    ('def f():\n    for LOCAL in (1, 0):\n        m.waitForAbort(LOCAL)\n', (W, 'mod.py', 'f', 'LOCAL')),
    ('def f():\n    LOCAL = 1\n    LOCAL = 0\n    m.waitForAbort(LOCAL)\n', (W, 'mod.py', 'f', 'LOCAL')),
    ('def f(helpers):\n    m.waitForAbort(helpers.POLL)\n', (W, 'mod.py', 'f', 'helpers.POLL')),
    ('class P:\n    def w(self, step=0.25):\n        return self.service._shutdown(step)\n', (S, 'mod.py', 'P.w', 'step')),
]


@pytest.mark.parametrize('body', GOOD)
def test_the_guard_passes_a_wait_kodi_ends(body):
    scan = _scan(body)
    assert scan.findings == [] and not scan.dynamic and not scan.strings


@pytest.mark.parametrize('body, why', BAD, ids=[b.splitlines()[0] + (' ...' if '\n' in b else '') for b, _ in BAD])
def test_the_guard_itself(body, why):
    """G2. Each of these must be found: the checker is broken on purpose in the review."""
    scan = _scan(body)
    assert len(scan.findings) >= 1 and why in ' | '.join(scan.findings), scan.findings


@pytest.mark.parametrize('body, key', DYNAMIC, ids=[k[3] + ' in ' + k[2] for _, k in DYNAMIC])
def test_the_guard_lists_a_wait_it_cannot_read_as_a_number(body, key):
    """Not a finding by itself: G1 / G3 compare these with the reviewed list, so a new one fails."""
    scan = _scan(body)
    assert scan.findings == [] and dict(scan.dynamic) == {key: 1}


def test_the_guard_reads_constants_as_the_library_does():
    """A constant of constants.py counts in every module (the star import), a zero there makes
    every wait that uses it a finding, and the bare names as text are listed apart."""
    zeroed = _scan('m.waitForAbort(CPU_CYCLE)\ns._shutdown(CPU_CYCLE)', extra={'constants.py': 'CPU_CYCLE = 0\n'})
    assert len(zeroed.findings) == 2 and all('Kodi would wait forever' in f for f in zeroed.findings)
    text = _scan("getattr(m, 'waitForAbort')(0)\ngetattr(pool, '_shutdown', False)")
    assert dict(text.strings) == {(W, 'mod.py', '<module>'): 1, (S, 'mod.py', '<module>'): 1}
    values = _scan('m.waitForAbort(CPU_CYCLE)\ns._shutdown(helpers.POLL)\nclass S:\n    def _shutdown(self, wait=SERVICE_INTERVAL): pass\n')
    assert values.values == {(W, 'mod.py', '<module>', 'CPU_CYCLE'): 0.016,
                             (S, 'mod.py', '<module>', 'helpers.POLL'): 0.5}
    assert values.defaults == {('mod.py', 'S._shutdown'): 5.0}


# === 4. the tests' own stand-ins for these two calls (G5) ==========================================

def _test_sources():
    sources = {}
    for label, folder in (('tests', HERE), ('stubs', ROOT_STUBS)):
        for base, _dirs, files in os.walk(folder):
            if '__pycache__' in base or os.path.join(HERE, 'fixtures') in base: continue
            for name in sorted(files):
                if not name.endswith('.py'): continue
                path = os.path.join(base, name)
                with open(path, encoding='utf-8') as fh: sources['%s/%s' % (label, os.path.relpath(path, folder))] = fh.read()
    return sources


# Calls that put a new body under a name given as text: setattr(x, 'name', f),
# monkeypatch.setattr('pkg.Class.name', f), mock.patch / patch.object.
PATCHERS = ('setattr', '__setattr__', 'patch', 'object')


def stand_ins(sources):
    """Counter of (file, how, owner, name): every place a test file or a Kodi stub gives
    waitForAbort or _shutdown a body of its own, or could: 'def' (a function of that name),
    'keyword' (SimpleNamespace(waitForAbort=...)), 'store' (x.waitForAbort = ...) and 'text' (the
    bare name as a string anywhere but a docstring: setattr / monkeypatch.setattr, a dict key, a
    list of names to bind; and a dotted path ending in it when handed to one of PATCHERS)."""
    found = collections.Counter()
    for file, text in sources.items():
        source = _File(text)
        for node in ast.walk(source.tree):
            how = name = None
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in WAITS: how, name = 'def', node.name
            elif isinstance(node, ast.keyword) and node.arg in WAITS: how, name = 'keyword', node.arg
            elif isinstance(node, ast.Attribute) and node.attr in WAITS and isinstance(node.ctx, ast.Store): how, name = 'store', node.attr
            elif isinstance(node, ast.Name) and node.id in WAITS and isinstance(node.ctx, ast.Store): how, name = 'store', node.id
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node not in source.docstrings:
                if node.value in WAITS: how, name = 'text', node.value
                else:
                    call = source.parent.get(node)
                    if isinstance(call, ast.keyword): call = source.parent.get(call)
                    callee = ast.unparse(call.func).split('.')[-1] if isinstance(call, ast.Call) else ''
                    dotted = [w for w in WAITS if node.value.endswith('.' + w)]
                    if dotted and callee in PATCHERS: how, name = 'text', dotted[0]
            if how: found[(file, how, source.around(node)[0], name)] += 1
    return found


FAITHFUL  = 'faithful: Kodi 21.3 rules (kodi_faithful.FaithfulMonitor); a wait Kodi would never end raises ForeverWait'
REAL      = 'the real method, compiled from services.py (kodi_faithful.FaithfulService binds it)'
STRICT    = 'strict since imports.70: refuses a wait of 0 or less (kodi_faithful.positive_wait)'
LABEL     = "the call's name as a label, or in a list of names that are only looked at: no stand-in"
FORGIVING = ('forgiving, left as it is: it answers at once whatever the wait. The code it stands under passes waits '
             'that are fixed numbers above 0 in the source (G1), and no test hands a wait of 0 through it')
SPY       = 'a spy that records the wait it is given; its test asserts the recorded waits'
SHARED    = ('the shared Kodi stub, forgiving, left as it is: it sleeps the timeout, so 0 returns at once. Measured '
             '2026-11-19: 215 calls reach it in one suite run, all with 0.0, from 45 tests (JSON-RPC reads made while '
             'rules are built: sendJSON waits RPC_Delay, which the stub settings read as 0 where Kodi gives 1). Making '
             'it strict needs the stub settings fixed first: a follow-up')
# Reviewed 2026-11-19. A new stand-in fails G5 until it is listed here with what it does on a wait of 0.
REVIEWED_STAND_INS = {
    ('stubs/xbmc.py', 'def', 'Monitor', W): SHARED,
    ('tests/kodi_faithful.py', 'def', 'FaithfulMonitor', W): FAITHFUL,
    ('tests/kodi_faithful.py', 'text', '<module>', S): REAL,
    ('tests/kodi_faithful.py', 'text', 'FaithfulService.__init__', S): REAL,
    ('tests/kodi_faithful.py', 'text', 'FaithfulMonitor.waitForAbort', W): LABEL,
    ('tests/kodi_faithful.py', 'text', 'positive_wait', W): LABEL,
    ('tests/test_imports54_cache_cleanup.py', 'def', 'RecordingMonitor', W): FORGIVING,
    ('tests/test_imports54_cache_cleanup.py', 'text', 'test_execute_sql_happy_path_never_sleeps', W):
        'a spy put over the shared stub for one test, which asserts that no wait is made at all',
    ('tests/test_imports59_build_waits_for_sync.py', 'def', '_Monitor', W): SPY + '; forgiving on 0',
    ('tests/test_imports60_no_duration_writeback.py', 'keyword', 'test_sendJSON_pauses_unless_told_not_to', W):
        SPY + ' (RPC_Delay, 1.0)',
    ('tests/test_imports61_update_keeps_playlist_state.py', 'def', 'QuietMonitor', W): FORGIVING,
    ('tests/test_imports62_restart_glue.py', 'text', 'test_the_rig_runs_the_real_shutdown_over_the_faithful_monitor', S): LABEL,
    ('tests/test_imports64_upnext.py', 'def', 'Rig', S): STRICT,
    ('tests/test_imports64_upnext.py', 'keyword', 'Rig.__init__', S): STRICT + ' (binds Rig._shutdown)',
    ('tests/test_imports64_upnext.py', 'text', 'Rig._shutdown', S): LABEL,
    ('tests/test_imports65_channel_rules.py', 'text', 'test_writers_hold_only_the_lock_for_in_process_work', W): LABEL,
    ('tests/test_imports67_livetv_subtitles.py', 'def', 'Wait', W): STRICT,
    ('tests/test_imports67_livetv_subtitles.py', 'text', 'Wait.waitForAbort', W): LABEL,
    ('tests/test_imports67_livetv_subtitles.py', 'def', '_answered_shutdown', S): STRICT,
    ('tests/test_imports67_livetv_subtitles.py', 'text', '_answered_shutdown._shutdown', S): LABEL,
    ('tests/test_imports67_livetv_subtitles.py', 'keyword', '_run_chkidle', S): STRICT + ' (binds _answered_shutdown)',
    ('tests/test_imports70_replay_end.py', 'text', 'test_the_move_asks_whether_kodi_is_closing_without_waiting', S): LABEL,
    ('tests/test_imports70_replay_end.py', 'text', 'test_the_move_asks_whether_kodi_is_closing_without_waiting', W): LABEL,
    ('tests/test_imports70_replay_end.py', 'text', 'test_the_new_methods_are_python38_and_free_of_bitor', S): LABEL,
    ('tests/test_imports70_replay_end.py', 'text', 'test_the_rig_runs_the_real_shutdown_over_the_faithful_monitor', S): LABEL,
    ('tests/test_imports70_replay_end.py', 'text', 'test_the_sweeps_cover_the_whole_move', W): LABEL,
    ('tests/test_imports70_replay_end.py', 'text', 'test_every_call_into_the_rig_counts_against_a_loop_that_never_waits', W):
        LABEL,
    ('tests/test_imports70_shutdown_contract.py', 'text', '<module>', W): LABEL,
    ('tests/test_imports70_shutdown_contract.py', 'text', '<module>', S): LABEL,
}


def test_the_fake_census():
    """G5. The root cause of 2026-11-19 was a forgiving fake. Every stand-in for waitForAbort or
    _shutdown in the tests and the Kodi stubs is listed with what it does on a wait of 0; a new
    one, or one that changed its shape, fails here until someone has looked at it. The two glue
    rigs (test_imports70_replay_end.py, test_imports62_restart_glue.py) have none: their service
    is kodi_faithful's."""
    found = stand_ins(_test_sources())
    assert dict(found) == {key: 1 for key in REVIEWED_STAND_INS}
    assert all(why for why in REVIEWED_STAND_INS.values())
    rigs = [key for key in found if key[0] in ('tests/test_imports70_replay_end.py', 'tests/test_imports62_restart_glue.py')
            and key[1] != 'text']
    assert rigs == []


def test_the_census_sees_every_way_to_make_a_stand_in():
    """The census itself, on a synthetic test file."""
    text = ('import types\n'
            'class Service(object):\n'
            '    def _shutdown(self, wait=0): return False\n'
            'monitor = types.SimpleNamespace(waitForAbort=lambda timeout=None: False)\n'
            'def test_x(monkeypatch):\n'
            '    """Mentions waitForAbort and _shutdown."""\n'
            '    monitor.waitForAbort = lambda *a: False\n'
            "    monkeypatch.setattr(monitor, 'waitForAbort', lambda t: False)\n"
            "    setattr(monitor, '_shutdown', None)\n"
            "    monkeypatch.setattr('xbmc.Monitor.waitForAbort', lambda t: False)\n"
            '    _shutdown = None\n')
    assert dict(stand_ins({'tests/test_x.py': text})) == {
        ('tests/test_x.py', 'def', 'Service', S): 1,
        ('tests/test_x.py', 'keyword', '<module>', W): 1,
        ('tests/test_x.py', 'store', 'test_x', W): 1,
        ('tests/test_x.py', 'text', 'test_x', W): 2,
        ('tests/test_x.py', 'text', 'test_x', S): 1,
        ('tests/test_x.py', 'store', 'test_x', S): 1}
