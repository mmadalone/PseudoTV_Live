# -*- coding: utf-8 -*-
"""imports.54: cache cleanup off the tune path + set-based purge.

Root cause (diagnosed live 2026-08-28 with py-spy against the running
Kodi): selecting a Custom channel hung on Kodi's busy spinner for 15m20s
because the plugin invocation's module import (default.py -> globals ->
kodi.py Settings class body -> Cache()) triggered _Cache.chkCleanup ->
_cleanUP synchronously. _cleanUP iterated every cache row with a blocking
service._shutdown(CPU_CYCLE) wait per row, issued one DELETE per expired
row (each paying _execute_sql's own pre-statement CPU_CYCLE sleep + a
FileLock round-trip), then VACUUMed — 10-15+ min at 29k rows / 134 MB.
Worse, lastexecuted was stamped even when the pass was aborted mid-loop,
so expired rows were never purged and the DB grew without bound (observed
3-week-old expired rows).

Fix under test:
  (a) chkCleanup(force=False) defers — only Tasks.chkCacheClean (service
      queue task) calls with force=True; constructors never run _cleanUP.
  (b) _cleanUP is set-based: one SELECT of expired ids, one DELETE, VACUUM.
  (c) lastexecuted stamped ONLY on success; cleanbusy always cleared.
  (d) _execute_sql no longer sleeps before every statement.
"""
import datetime
import os
import re
import sqlite3
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
LIB  = os.path.normpath(os.path.join(HERE, '..', 'resources', 'lib'))
if LIB not in sys.path:
    sys.path.insert(0, LIB)


def _read(name):
    with open(os.path.join(LIB, name), 'r', encoding='utf-8') as fh:
        return fh.read()


class FakeGlobals(object):
    """Dict-backed stand-in for cache.Globals property helpers."""
    def __init__(self):
        self.props = {}
        self.cleared = []

    def _getProperty(self, key, default=''):
        return self.props.get(key, default)

    def _setProperty(self, key, value):
        self.props[key] = value

    def _clrProperty(self, key):
        self.cleared.append(key)
        self.props.pop(key, None)


class RecordingMonitor(object):
    """Monitor spy: counts waitForAbort calls, never aborts."""
    def __init__(self):
        self.wait_calls = 0

    def abortRequested(self):
        return False

    def waitForAbort(self, timeout=0):
        self.wait_calls += 1
        return False


SCHEMA = "CREATE TABLE IF NOT EXISTS cache(id TEXT UNIQUE, expires INTEGER, data TEXT, checksum INTEGER)"


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Bare _Cache on a tmp sqlite file with fake Globals + monitor spy."""
    import cache as cache_mod
    fake = FakeGlobals()
    monkeypatch.setattr(cache_mod, 'Globals', fake)
    c = cache_mod._Cache.__new__(cache_mod._Cache)
    c.service = cache_mod.Service()
    mon = RecordingMonitor()
    c.service.monitor = mon           # instance attr shadows class attr
    c.monitor = mon
    c.dbfile = str(tmp_path / 'cache.db')
    c._auto_clean_interval = datetime.timedelta(hours=3)
    # chkCleanup re-reads the interval each call (imports.54: Max_Days must
    # not freeze at construction) — pin it for test determinism.
    monkeypatch.setattr(c, '_cleanInterval', lambda: datetime.timedelta(hours=3))
    conn = sqlite3.connect(c.dbfile)
    conn.execute(SCHEMA)
    conn.commit()
    conn.close()
    return cache_mod, c, fake, mon


def _seed(dbfile, rows):
    conn = sqlite3.connect(dbfile)
    conn.executemany("INSERT INTO cache VALUES (?,?,?,?)", rows)
    conn.commit()
    conn.close()


def _rows(dbfile):
    conn = sqlite3.connect(dbfile)
    ids = sorted(r[0] for r in conn.execute("SELECT id FROM cache").fetchall())
    conn.close()
    return ids


def _key(cache_mod, suffix):
    return '%s.%s' % (cache_mod.ADDON_ID, suffix)


# ---------------------------------------------------------------------------
# (a) chkCleanup defers unless force=True
# ---------------------------------------------------------------------------

def test_chkCleanup_due_without_force_defers(env, monkeypatch):
    cache_mod, c, fake, _ = env
    stale = (datetime.datetime.now() - datetime.timedelta(hours=10)).isoformat()
    fake._setProperty(_key(cache_mod, 'cache.lastexecuted'), stale)
    calls = []
    monkeypatch.setattr(c, '_cleanUP', lambda: calls.append(True))
    c.chkCleanup()
    assert calls == [], "constructor-path chkCleanup must NOT run _cleanUP"
    # stamp untouched — the service task must still see it as due
    assert fake._getProperty(_key(cache_mod, 'cache.lastexecuted')) == stale


def test_chkCleanup_due_with_force_runs(env, monkeypatch):
    cache_mod, c, fake, _ = env
    stale = (datetime.datetime.now() - datetime.timedelta(hours=10)).isoformat()
    fake._setProperty(_key(cache_mod, 'cache.lastexecuted'), stale)
    calls = []
    monkeypatch.setattr(c, '_cleanUP', lambda: calls.append(True))
    c.chkCleanup(force=True)
    assert calls == [True]


def test_chkCleanup_not_due_never_runs_even_forced(env, monkeypatch):
    cache_mod, c, fake, _ = env
    fresh = datetime.datetime.now().isoformat()
    fake._setProperty(_key(cache_mod, 'cache.lastexecuted'), fresh)
    calls = []
    monkeypatch.setattr(c, '_cleanUP', lambda: calls.append(True))
    c.chkCleanup(force=True)
    assert calls == []


def test_chkCleanup_absent_stamp_initializes_baseline(env, monkeypatch):
    cache_mod, c, fake, _ = env
    calls = []
    monkeypatch.setattr(c, '_cleanUP', lambda: calls.append(True))
    c.chkCleanup(force=True)
    assert calls == []
    stamped = fake._getProperty(_key(cache_mod, 'cache.lastexecuted'))
    assert stamped, "first call must initialize the lastexecuted baseline"
    datetime.datetime.fromisoformat(stamped)  # parseable


def test_chkCleanup_unparseable_stamp_treated_as_due(env, monkeypatch):
    cache_mod, c, fake, _ = env
    fake._setProperty(_key(cache_mod, 'cache.lastexecuted'), 'not-a-date')
    calls = []
    monkeypatch.setattr(c, '_cleanUP', lambda: calls.append(True))
    c.chkCleanup(force=True)
    assert calls == [True]


# ---------------------------------------------------------------------------
# (b) _cleanUP set-based purge semantics
# ---------------------------------------------------------------------------

def test_cleanUP_purges_expired_keeps_fresh(env):
    cache_mod, c, fake, _ = env
    future = 2**33
    _seed(c.dbfile, [('dead.1', 1, 'x', 0), ('dead.2', 2, 'x', 0),
                     ('live.1', future, 'y', 0)])
    c._cleanUP()
    assert _rows(c.dbfile) == ['live.1']


def test_cleanUP_clears_mem_mirrors_of_expired_only(env):
    cache_mod, c, fake, _ = env
    future = 2**33
    _seed(c.dbfile, [('dead.1', 1, 'x', 0), ('live.1', future, 'y', 0)])
    fake._setProperty(_key(cache_mod, 'dead.1'), 'mem')
    fake._setProperty(_key(cache_mod, 'live.1'), 'mem')
    c._cleanUP()
    assert _key(cache_mod, 'dead.1') in fake.cleared
    # fresh entries keep their warm mem-cache mirror (old code wiped all)
    assert fake._getProperty(_key(cache_mod, 'live.1')) == 'mem'


def test_cleanUP_success_stamps_lastexecuted_and_clears_busy(env):
    cache_mod, c, fake, _ = env
    _seed(c.dbfile, [('dead.1', 1, 'x', 0)])
    c._cleanUP()
    stamped = fake._getProperty(_key(cache_mod, 'cache.lastexecuted'))
    assert stamped and datetime.datetime.fromisoformat(stamped)
    assert not fake._getProperty(_key(cache_mod, 'cache.cleanbusy'))


def test_cleanUP_respects_cleanbusy_reentrancy_gate(env, monkeypatch):
    cache_mod, c, fake, _ = env
    fake._setProperty(_key(cache_mod, 'cache.cleanbusy'), 'busy')
    called = []
    monkeypatch.setattr(c, '_execute_sql', lambda *a, **k: called.append(a) or None)
    c._cleanUP()
    assert called == [], "busy gate must short-circuit before any SQL"
    # gate owner's marker must not be cleared by the skipped pass
    assert fake._getProperty(_key(cache_mod, 'cache.cleanbusy')) == 'busy'


def test_cleanUP_zero_expired_skips_vacuum_but_stamps(env):
    cache_mod, c, fake, _ = env
    future = 2**33
    _seed(c.dbfile, [('live.1', future, 'y', 0)])
    executed = []
    real = c._execute_sql

    def spy(query, data=None):
        executed.append(query)
        return real(query, data)
    c._execute_sql = spy
    c._cleanUP()
    assert not any('VACUUM' in q for q in executed), \
        "no purge -> no VACUUM (exclusive lock not worth it)"
    assert fake._getProperty(_key(cache_mod, 'cache.lastexecuted')), \
        "an honestly-empty pass still counts as done"


def test_cleanUP_partial_failure_delete_none_does_not_stamp(env):
    """SELECT succeeds, DELETE fails (FileLock timeout / retry exhaustion /
    abort gate all surface as None). The pass must NOT stamp, must NOT log
    success, must NOT clear the expired ids' mem mirrors, and must skip
    VACUUM — otherwise the never-actually-purged growth cycle this diff
    exists to fix comes back silently."""
    cache_mod, c, fake, _ = env
    _seed(c.dbfile, [('dead.1', 1, 'x', 0)])
    fake._setProperty(_key(cache_mod, 'dead.1'), 'mem')
    stale = (datetime.datetime.now() - datetime.timedelta(hours=10)).isoformat()
    fake._setProperty(_key(cache_mod, 'cache.lastexecuted'), stale)
    executed = []
    real = c._execute_sql

    def flaky(query, data=None):
        executed.append(query)
        if query.startswith('DELETE'):
            return None
        return real(query, data)
    c._execute_sql = flaky
    c._cleanUP()
    assert fake._getProperty(_key(cache_mod, 'cache.lastexecuted')) == stale, \
        "failed DELETE must NOT stamp lastexecuted"
    assert fake._getProperty(_key(cache_mod, 'dead.1')) == 'mem', \
        "mem mirror must survive when its row was NOT purged"
    assert not any('VACUUM' in q for q in executed)
    assert not fake._getProperty(_key(cache_mod, 'cache.cleanbusy'))
    assert _rows(c.dbfile) == ['dead.1']


# ---------------------------------------------------------------------------
# (c) failure handling: no stamp on failure, busy flag always cleared
# ---------------------------------------------------------------------------

def test_cleanUP_no_stamp_when_db_unreachable(env, monkeypatch):
    cache_mod, c, fake, _ = env
    stale = (datetime.datetime.now() - datetime.timedelta(hours=10)).isoformat()
    fake._setProperty(_key(cache_mod, 'cache.lastexecuted'), stale)
    monkeypatch.setattr(c, '_execute_sql', lambda *a, **k: None)
    c._cleanUP()
    assert fake._getProperty(_key(cache_mod, 'cache.lastexecuted')) == stale, \
        "failed pass must NOT stamp lastexecuted (service retries next tick)"
    assert not fake._getProperty(_key(cache_mod, 'cache.cleanbusy'))


def test_cleanUP_busy_cleared_even_on_exception(env, monkeypatch):
    cache_mod, c, fake, _ = env
    stale = (datetime.datetime.now() - datetime.timedelta(hours=10)).isoformat()
    fake._setProperty(_key(cache_mod, 'cache.lastexecuted'), stale)

    def boom(*a, **k):
        raise RuntimeError('wedged')
    monkeypatch.setattr(c, '_execute_sql', boom)
    with pytest.raises(RuntimeError):
        c._cleanUP()
    assert not fake._getProperty(_key(cache_mod, 'cache.cleanbusy')), \
        "cleanbusy leak would permanently disable cleanup"
    assert fake._getProperty(_key(cache_mod, 'cache.lastexecuted')) == stale


# ---------------------------------------------------------------------------
# (d) _execute_sql happy path has no blocking wait
# ---------------------------------------------------------------------------

def test_execute_sql_happy_path_never_sleeps(env, monkeypatch):
    cache_mod, c, fake, mon = env
    # Belt and braces: also spy EVERY monitor in the process (FileLock has
    # its own class-level Monitor the fixture's instance spy can't see).
    import xbmc
    global_waits = []
    monkeypatch.setattr(
        xbmc.Monitor, 'waitForAbort',
        lambda self, timeout=0: global_waits.append(timeout) or False,
        raising=False)
    res = c._execute_sql("INSERT INTO cache VALUES (?,?,?,?)", ('k', 1, 'v', 0))
    assert res is not None
    assert mon.wait_calls == 0, \
        "_execute_sql must not call waitForAbort on the happy path"
    assert global_waits == [], \
        "no monitor anywhere (incl. FileLock's own) may sleep on the happy path"


def test_service_aborted_is_nonblocking(env):
    cache_mod, c, fake, mon = env
    assert c.service._aborted() is False
    assert mon.wait_calls == 0


# ---------------------------------------------------------------------------
# (e) service-side wiring actually executes (not just source-scanned)
# ---------------------------------------------------------------------------

def test_Cache_chkCleanup_passthrough_delegates_force(env, monkeypatch):
    cache_mod, c, fake, _ = env
    wrapper = cache_mod.Cache.__new__(cache_mod.Cache)
    calls = []

    class Recorder(object):
        def chkCleanup(self, force=False):
            calls.append(force)
    wrapper.cache = Recorder()
    wrapper.chkCleanup(force=True)
    wrapper.chkCleanup()
    assert calls == [True, False], \
        "Cache.chkCleanup must delegate to _Cache.chkCleanup with the force flag"


def test_tasks_chkCacheClean_body_executes_force_true():
    """tasks.py can't be imported under the stubs, so execute the SHIPPED
    method source (not a hand-copied mirror) against a recorder self."""
    src = _read('tasks.py')
    m = re.search(r"(    def chkCacheClean\(self\):.*?)(?=\n    def )", src, re.DOTALL)
    assert m, "chkCacheClean must exist in tasks.py"
    import textwrap
    ns = {}
    exec(textwrap.dedent(m.group(1)), ns)

    calls = []

    class FakeCacheDB(object):
        def chkCleanup(self, force=False):
            calls.append(force)

    class FakeSelf(object):
        cacheDB = FakeCacheDB()

        def log(self, *a, **k):
            pass
    ns['chkCacheClean'](FakeSelf())
    assert calls == [True], \
        "the shipped chkCacheClean body must call cacheDB.chkCleanup(force=True)"


# ---------------------------------------------------------------------------
# Source-scan invariants (structure guards, style of test_imports22/35)
# ---------------------------------------------------------------------------

def _strip_comments(body):
    return '\n'.join(l for l in body.splitlines() if not l.lstrip().startswith('#'))


def test_cache_cleanup_loops_carry_no_blocking_shutdown():
    src = _read('cache.py')
    body = _strip_comments(re.search(r"def _cleanUP\(.*?(?=\n    def )", src, re.DOTALL).group(0))
    assert '_shutdown(CPU_CYCLE)' not in body, \
        "imports.54: _cleanUP must not block per row"
    assert 'DELETE FROM cache WHERE expires <' in body, \
        "imports.54: _cleanUP must purge with one set-based DELETE"
    exec_body = _strip_comments(re.search(r"def _execute_sql\(.*?(?=\n    @|\n    def )", src, re.DOTALL).group(0))
    assert '_shutdown(CPU_CYCLE)' not in exec_body, \
        "imports.54: _execute_sql must not sleep before every statement"
    assert '_aborted()' in exec_body, \
        "imports.54: _execute_sql keeps a NON-blocking shutdown gate"


def test_constructor_and_del_never_run_cleanup_directly():
    """The tune-path pin: __init__/__del__ may only route through
    chkCleanup's default (deferring) path — no direct _cleanUP call, no
    force. A regression here re-opens the 15-min spinner."""
    src = _read('cache.py')
    init_body = _strip_comments(re.search(
        r"def __init__\(self, service=None.*?(?=\n    def )", src, re.DOTALL).group(0))
    del_body = _strip_comments(re.search(
        r"def __del__\(self\):.*?(?=\n    def )", src, re.DOTALL).group(0))
    assert 'self.chkCleanup()' in init_body, \
        "imports.54: __init__ must still call chkCleanup (baseline stamp only)"
    for name, body in (('__init__', init_body), ('__del__', del_body)):
        assert '_cleanUP' not in body, \
            "imports.54: %s must not call _cleanUP directly" % name
        assert not re.search(r"chkCleanup\s*\(\s*(?:force\s*=\s*)?True", body), \
            "imports.54: %s must not force cleanup" % name


_FORCE_CALL = re.compile(r"chkCleanup\s*\(\s*(?:force\s*=\s*)?True")


def test_tasks_wires_chkCacheClean_as_sole_force_caller():
    src = _read('tasks.py')
    assert re.search(r"_chkEpochTimer\('chkCacheClean'\s*,\s*self\.chkCacheClean", src), \
        "imports.54: chkCacheClean must be registered in chkQueTimer's epoch list"
    body = re.search(r"def chkCacheClean\(.*?(?=\n    def )", src, re.DOTALL).group(0)
    assert _FORCE_CALL.search(body)
    # sole force caller across the whole lib tree (recursive, regex — catches
    # positional True and spacing variants)
    for root, _dirs, files in os.walk(LIB):
        for name in sorted(files):
            if not name.endswith('.py'):
                continue
            path = os.path.join(root, name)
            if os.path.samefile(path, os.path.join(LIB, 'tasks.py')):
                continue
            with open(path, 'r', encoding='utf-8') as fh:
                assert not _FORCE_CALL.search(fh.read()), \
                    "imports.54: %s must not force cache cleanup (service task only)" % \
                    os.path.relpath(path, LIB)
