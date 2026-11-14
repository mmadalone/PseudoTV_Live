# -*- coding: utf-8 -*-
"""imports.69: the Save fix -- kick_helpers, the "sync the imports now" kick.

A kick is the window property 'chkImports.kick'; the imports thread's poll
loop (tasks.py) reads it every 3 seconds, clears it and runs one sync cycle
with it ('__render__' = rewrite the playlist and guide files without forcing a
download, '<import id>' = sync that import now, 'all' = sync every import).

Before imports.69 every setter overwrote whatever was pending: a render kick
could replace a pending 'all', and ↻ Refresh on one row could cancel the
global one. merge_kick keeps the stronger request (none < render < id < all;
two different ids become 'all'), and set_kick reads, merges and writes under
KICK_LOCK, which the poll loop also holds for its read / check / clear.

kick_helpers imports threading only, so these tests use a fake PROPERTIES.
The last section checks the users: the poll loop's shape in tasks.py (an AST
check; the loop is a closure no test can call), _keepImportsKick, the
address re-check in server.py, and which server.py kick setters still write
the property directly (the four channel endpoints imports.71 converts).
"""
import ast
import os
import threading
import time

import pytest

import kick_helpers as kh
from kick_helpers import RENDER_ONLY_KICK as R

HERE = os.path.dirname(os.path.abspath(__file__))
LIB  = os.path.normpath(os.path.join(HERE, '..', 'resources', 'lib'))


class FakeProps(object):
    """The two PROPERTIES calls set_kick makes, positional like the add-on's.
    getEXTProperty returns what Kodi's literal_eval would (kodi.py: a stored
    '7' comes back as the int 7)."""

    def __init__(self, pending=None, read_delay=0.0):
        self.ext = {}
        if pending is not None:
            self.ext[kh.KICK_PROP] = pending
        self.read_delay = read_delay
        self.writes = []

    def getEXTProperty(self, key, default=''):
        value = self.ext.get(key, default)
        if self.read_delay:
            time.sleep(self.read_delay)          # widen the read-then-write window
        return value

    def setEXTProperty(self, key, value):
        self.writes.append((key, value))
        self.ext[key] = value
        return value


# ================================================================ module hygiene


def test_module_imports_threading_only_and_is_python38():
    with open(os.path.join(LIB, 'kick_helpers.py'), encoding='utf-8') as f:
        src = f.read()
    tree = ast.parse(src, feature_version=(3, 8))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module)
    assert names == {'threading'}
    assert not [n for n in ast.walk(tree)
                if isinstance(n, (ast.BinOp, ast.AugAssign)) and isinstance(n.op, ast.BitOr)]
    assert 'madteevee (imports fork, imports.69)' in src


def test_constants():
    assert (kh.RENDER_ONLY_KICK, kh.KICK_PROP, kh.KICK_LOCK_TIMEOUT) == ('__render__', 'chkImports.kick', 2.0)
    assert isinstance(kh.KICK_LOCK, type(threading.Lock()))


def test_render_kick_is_the_one_syncall_knows():
    """imports.py keeps working with the same value: its own constant until
    the imports stage re-exports this one, the same object after."""
    import imports
    assert imports.RENDER_ONLY_KICK == kh.RENDER_ONLY_KICK


# ================================================================ merge_kick


@pytest.mark.parametrize('pending, new, expected', [
    ('', '', ''), (None, None, ''), ('', None, ''),
    ('', R, R), ('', 'alpha', 'alpha'), ('', 'all', 'all'),
    (R, '', R), ('alpha', '', 'alpha'), ('all', None, 'all'),            # an empty kick never clears one
    (R, R, R), (R, 'alpha', 'alpha'), (R, 'all', 'all'),
    ('alpha', R, 'alpha'), ('all', R, 'all'),                            # a render never lowers a pending kick
    ('alpha', 'alpha', 'alpha'), ('alpha', 'beta', 'all'),               # two imports: both must sync
    ('alpha', 'all', 'all'), ('all', 'alpha', 'all'), ('all', 'all', 'all'),
    (7, '7', '7'), (7, 'alpha', 'all'), ('7', 7, '7'),                   # literal_eval gives ints back
    (' alpha ', 'alpha', 'alpha'), ('alpha', ' all ', 'all'), (' ', 'alpha', 'alpha'),
])
def test_merge_kick(pending, new, expected):
    assert kh.merge_kick(pending, new) == expected


_RANK = {'': 0, R: 1, 'all': 3}


def _rank(kick):
    return _RANK.get(kick, 2)


@pytest.mark.parametrize('pending', ['', R, 'alpha', 'beta', 'all'])
@pytest.mark.parametrize('new', ['', R, 'alpha', 'beta', 'all'])
def test_a_kick_never_lowers_a_pending_one(pending, new):
    merged = kh.merge_kick(pending, new)
    assert _rank(merged) >= max(_rank(pending), _rank(new))
    if _rank(pending) == 2 and _rank(new) == 2 and pending != new:
        assert merged == 'all'


# ================================================================ set_kick


def test_set_kick_writes_the_merged_value_and_returns_both():
    props = FakeProps()
    assert kh.set_kick(props, R) == ('', R)
    assert kh.set_kick(props, 'alpha') == (R, 'alpha')
    assert kh.set_kick(props, 'beta') == ('alpha', 'all')
    assert props.writes == [(kh.KICK_PROP, R), (kh.KICK_PROP, 'alpha'), (kh.KICK_PROP, 'all')]


@pytest.mark.parametrize('pending, value', [('all', R), ('all', 'alpha'), ('alpha', 'alpha'), ('alpha', R),
                                            ('', ''), ('', None), (R, R), (7, '7')])
def test_set_kick_writes_nothing_when_nothing_changes(pending, value):
    """S1-M7: the pending kick stays as it is (no overwrite), and an
    unchanged kick causes no property write."""
    props = FakeProps(pending)
    before, after = kh.set_kick(props, value)
    assert before == after and props.writes == []
    assert props.ext.get(kh.KICK_PROP, '') == pending


def test_set_kick_calls_properties_positionally():
    """The add-on's fakes (test_imports56_logo_links) patch
    getEXTProperty(k, d=None) / setEXTProperty(k, v)."""
    calls = []

    class Positional(object):
        def getEXTProperty(self, *args):
            calls.append(('get',) + args)
            return ''

        def setEXTProperty(self, *args):
            calls.append(('set',) + args)

    kh.set_kick(Positional(), R)
    assert calls == [('get', kh.KICK_PROP, ''), ('set', kh.KICK_PROP, R)]


def test_two_threads_racing_keep_both_kicks():
    """Two Refreshes on different rows at the same moment: without the lock
    both read '' and the later write wins ('beta' only); with it the second
    merges onto the first and both imports sync ('all')."""
    def kick(props, barrier, value):
        barrier.wait()
        kh.set_kick(props, value)

    for _ in range(3):
        props = FakeProps(read_delay=0.05)
        barrier = threading.Barrier(2)
        threads = [threading.Thread(target=kick, args=(props, barrier, v)) for v in ('alpha', 'beta')]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        assert not any(t.is_alive() for t in threads)
        assert props.ext[kh.KICK_PROP] == 'all'


def test_a_lock_timeout_still_writes(monkeypatch):
    """A kick is never dropped: when KICK_LOCK can't be had in time the kick
    is merged and written without it, and the lock it never took is left
    alone."""
    monkeypatch.setattr(kh, 'KICK_LOCK_TIMEOUT', 0.05)       # read at call time
    assert kh.KICK_LOCK.acquire(timeout=1)
    try:
        props = FakeProps('alpha')
        t0 = time.monotonic()
        assert kh.set_kick(props, 'beta') == ('alpha', 'all')
        assert time.monotonic() - t0 < 1.0
        assert props.ext[kh.KICK_PROP] == 'all'
        assert kh.KICK_LOCK.locked()                          # still the test's
    finally:
        kh.KICK_LOCK.release()


def test_the_lock_is_released_when_properties_fail():
    class Broken(object):
        def getEXTProperty(self, key, default=''):
            raise RuntimeError('window gone')

    with pytest.raises(RuntimeError):
        kh.set_kick(Broken(), 'alpha')
    assert not kh.KICK_LOCK.locked()


# ================================================================ the setters and the poll loop (server / tasks stage)
#
# tasks.py can't be imported under the test stubs (xbmcgui.ControlList), and
# the poll loop is a closure inside Tasks._startImportsThread that no test can
# call, so its shape is pinned on the AST, as test_imports23 pins its literals.


def _tree(name):
    with open(os.path.join(LIB, name), encoding='utf-8') as f:
        src = f.read()
    return src, ast.parse(src, feature_version=(3, 8))


def _method(tree, cls, name):
    klass = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls)
    return next(n for n in klass.body if isinstance(n, ast.FunctionDef) and n.name == name)


def _calls(node, attr, owner=None):
    """Calls `owner.attr(...)` (or a bare `attr(...)` when owner is None) under node."""
    found = []
    for n in ast.walk(node):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        if owner is None and isinstance(f, ast.Name) and f.id == attr:
            found.append(n)
        elif (owner is not None and isinstance(f, ast.Attribute) and f.attr == attr
              and isinstance(f.value, ast.Name) and f.value.id == owner):
            found.append(n)
    return found


def _first_arg(call):
    return call.args[0].value if call.args and isinstance(call.args[0], ast.Constant) else None


def _blocks(node):
    """Every statement list under node (bodies, else/finally blocks, handlers)."""
    for n in ast.walk(node):
        for field in ('body', 'orelse', 'finalbody'):
            block = getattr(n, field, None)
            if isinstance(block, list) and block and isinstance(block[0], ast.stmt):
                yield block


def _is_acquire_or_continue(stmt):
    """`if not KICK_LOCK.acquire(timeout=KICK_LOCK_TIMEOUT): continue`"""
    if not (isinstance(stmt, ast.If) and isinstance(stmt.test, ast.UnaryOp) and isinstance(stmt.test.op, ast.Not)):
        return False
    call = stmt.test.operand
    return (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr == 'acquire'
            and isinstance(call.func.value, ast.Name) and call.func.value.id == 'KICK_LOCK'
            and not call.args and len(call.keywords) == 1 and call.keywords[0].arg == 'timeout'
            and isinstance(call.keywords[0].value, ast.Name) and call.keywords[0].value.id == 'KICK_LOCK_TIMEOUT'
            and len(stmt.body) == 1 and isinstance(stmt.body[0], ast.Continue) and not stmt.orelse)


def test_the_poll_loop_reads_checks_and_clears_the_kick_under_kick_lock():
    """S1-M20. In the imports thread's wait loop:
    `if not KICK_LOCK.acquire(timeout=KICK_LOCK_TIMEOUT): continue` comes
    right before the `try` whose body reads 'chkImports.kick'
    (getEXTProperty) and clears it (clrEXTProperty), and whose `finally`
    releases KICK_LOCK, so a set_kick from a web-server thread can't land
    between the read and the clear and be wiped. The time-based exit stays
    before the lock (test_imports23's order), and nothing inside the locked
    block calls set_kick (KICK_LOCK is a plain Lock)."""
    src, tree = _tree('tasks.py')
    start = _method(tree, 'Tasks', '_startImportsThread')
    loop = next(n for n in ast.walk(start) if isinstance(n, ast.FunctionDef) and n.name == '_loop')
    reads = [c for c in _calls(loop, 'getEXTProperty', 'PROPERTIES') if _first_arg(c) == 'chkImports.kick']
    clears = [c for c in _calls(loop, 'clrEXTProperty', 'PROPERTIES') if _first_arg(c) == 'chkImports.kick']
    assert len(reads) == 1 and len(clears) == 1
    pairs = []
    for block in _blocks(loop):
        for i, stmt in enumerate(block[:-1]):
            nxt = block[i + 1]
            if _is_acquire_or_continue(stmt) and isinstance(nxt, ast.Try):
                pairs.append((block, i, nxt))
    assert len(pairs) == 1, 'exactly one `if not KICK_LOCK.acquire(...): continue` followed by its try'
    block, at, guarded = pairs[0]
    inside = set(id(n) for stmt in guarded.body for n in ast.walk(stmt))
    assert id(reads[0]) in inside and id(clears[0]) in inside
    releases = [c for stmt in guarded.finalbody for c in _calls(stmt, 'release', 'KICK_LOCK')]
    assert len(releases) == 1 and not releases[0].args
    assert len(_calls(loop, 'release', 'KICK_LOCK')) == 1 and len(_calls(loop, 'acquire', 'KICK_LOCK')) == 1
    assert not _calls(guarded, 'set_kick'), 'set_kick under KICK_LOCK would wait 2 s and write unlocked'
    # the time-based exit comes first, so the lock is never held for a cycle that leaves anyway
    exits = [j for j, stmt in enumerate(block[:at]) if isinstance(stmt, ast.If)
             and 'interval_secs > 0 and elapsed >= interval_secs' in ast.get_source_segment(src, stmt.test)]
    assert exits, 'the interval exit check must precede the lock in the same block'
    # the loop picks up the module-level names from kick_helpers
    imported = {(n.module, a.name) for n in tree.body if isinstance(n, ast.ImportFrom) for a in n.names}
    assert {('kick_helpers', 'KICK_LOCK'), ('kick_helpers', 'KICK_LOCK_TIMEOUT'),
            ('kick_helpers', 'set_kick')}.issubset(imported)


def test_keep_imports_kick_merges_through_set_kick():
    """S1-M21 is caught by test_imports59_build_waits_for_sync's harness; this
    pins the shape: one set_kick(PROPERTIES, force_scope), no direct property
    access, and the 'kept' line only when the pending kick changed."""
    src, tree = _tree('tasks.py')
    keep = _method(tree, 'Tasks', '_keepImportsKick')
    calls = _calls(keep, 'set_kick')
    assert len(calls) == 1
    assert [ast.get_source_segment(src, a) for a in calls[0].args] == ['PROPERTIES', 'force_scope']
    assert not _calls(keep, 'setEXTProperty', 'PROPERTIES') and not _calls(keep, 'getEXTProperty', 'PROPERTIES')
    assert 'if after != before:' in ast.get_source_segment(src, keep)


def test_chkaddress_sets_the_kick_only_through_set_kick():
    """S1-M46: the address re-check merges a render kick under KICK_LOCK
    instead of its unlocked check-then-set (a Refresh or Save setting 'all'
    between the two could be overwritten)."""
    src, tree = _tree('server.py')
    chk = _method(tree, 'HTTP', '_chkAddress')
    calls = _calls(chk, 'set_kick')
    assert len(calls) == 1
    assert [ast.get_source_segment(src, a) for a in calls[0].args] == ['PROPERTIES', 'RENDER_ONLY_KICK']
    assert not _calls(chk, 'setEXTProperty', 'PROPERTIES') and not _calls(chk, 'getEXTProperty', 'PROPERTIES')
    assert not [n for n in ast.walk(chk) if isinstance(n, (ast.Import, ast.ImportFrom))]
    imported = {(n.module, a.name) for n in tree.body if isinstance(n, ast.ImportFrom) for a in n.names}
    assert {('kick_helpers', 'set_kick'), ('kick_helpers', 'RENDER_ONLY_KICK')}.issubset(imported)


@pytest.mark.parametrize('pending, written', [('', [R]), ('all', []), ('alpha', []), (R, [])])
def test_chkaddress_never_lowers_a_pending_kick(monkeypatch, pending, written):
    """The address re-check end to end (the test_imports56_logo_links fakes'
    shape): a render kick is written only when nothing is pending."""
    import server
    writes = []
    monkeypatch.setattr(server.PROPERTIES, 'getRemoteHost', lambda: '198.51.100.7:50002')
    monkeypatch.setattr(server.PROPERTIES, 'setRemoteHost', lambda v: v)
    monkeypatch.setattr(server.PROPERTIES, 'getEXTProperty',
                        lambda k, d=None: pending if k == kh.KICK_PROP else d)
    monkeypatch.setattr(server.PROPERTIES, 'setEXTProperty', lambda k, v: writes.append(v))
    monkeypatch.setattr(server.SETTINGS, 'getIP', lambda *a, **k: '192.0.2.10')
    monkeypatch.setattr(server.SETTINGS, 'setSetting', lambda k, v: None)

    class _Http(object):
        ADDRESS_CHECK_SECS = 60

        def log(self, *a, **k):
            pass

    assert server.HTTP._chkAddress(_Http()) is True
    assert writes == written


def test_server_kick_setters_outside_set_kick_are_the_step3_ones():
    """POST /imports.json, Orphans, Refresh and _chkAddress go through
    set_kick. The four direct writes left belong to the channel endpoints
    that imports.71 (step 3) converts: channel edit, toggle, renumber and
    Cleanup stranded."""
    src, tree = _tree('server.py')
    direct = [c for c in _calls(tree, 'setEXTProperty', 'PROPERTIES') if _first_arg(c) == 'chkImports.kick']
    assert [ast.get_source_segment(src, c.args[1]) for c in direct] == ["'all'", "'all'", 'target_id', "'all'"]
    handler = _method(tree, 'MyHandler', 'do_POST')
    body = ast.get_source_segment(src, handler)
    for route, kick in (("'/imports.json'", 'set_kick(PROPERTIES, RENDER_ONLY_KICK)'),
                        ("'/imports/orphans/remove.json'", 'set_kick(PROPERTIES, RENDER_ONLY_KICK)'),
                        ("'/imports/refresh.json'", "set_kick(PROPERTIES, target_id or 'all')")):
        start = body.index(route)
        end = body.index('\n                elif ', start)
        branch = body[start:end]
        assert branch.count(kick) == 1, route
        assert "setEXTProperty('chkImports.kick'" not in branch, route
