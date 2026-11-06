# -*- coding: utf-8 -*-
"""imports.63: one real clock and one tested set of time conversions.

getUTCstamp() (the real epoch minus the UTC offset: 1 h slow in winter, 2 h in
summer in Madrid) put rebuild cuts, new-channel starts and the 'Play programme'
join check in the past; epochTime(tz=True) shifted the Up next time and the
PauseRule 'last viewed' line by the same offset; and the guide writer stamped
every programme with the offset of the moment it was written. clock_helpers is
the one clock (now) and the one place that converts between epochs and local
time. Every test that converts pins the zone (conftest.py: tz, clock_zone).
"""
import ast
import calendar
import copy
import datetime
import io
import json
import os
import re
import time
import tokenize
import types

import pytest
import xbmc

import clock_helpers as ch
import globals as globals_mod
import plugin
import replay_helpers
import restart_helpers
import xmltvs
from builder import Builder
from constants import BACKUP_TIME_FORMAT, DTFORMAT
from globals import Globals
from xmltvs import XMLTVS

HERE = os.path.dirname(os.path.abspath(__file__))
LIB  = os.path.normpath(os.path.join(HERE, '..', 'resources', 'lib'))
X    = '3330312e582d46696c65732e3c6d6435@PseudoTV_Live'     # an own channel id
Y    = 'TVE@movistarplus'


def _src(name):
    with open(os.path.join(LIB, name), encoding='utf-8') as f:
        return f.read()


def ep(*fields):
    """UTC fields -> epoch."""
    return calendar.timegm(fields + (0,) * (6 - len(fields)))


def offset_text(epoch):
    """'+HHMM' of the offset in force at `epoch` in the pinned zone."""
    off = time.localtime(epoch).tm_gmtoff
    return '%s%02d%02d' % ('+' if off >= 0 else '-', abs(off) // 3600, (abs(off) % 3600) // 60)


def iptv_simple_parse(text):
    """Reference reader written independently of clock_helpers, like IPTV
    Simple's XMLTV date parse: the digits as UTC minus the stated offset; no
    offset = UTC."""
    digits, _, off = text.strip().partition(' ')
    t = calendar.timegm(time.strptime(digits, '%Y%m%d%H%M%S'))
    if not off: return t
    sign = 1 if off[0] == '+' else -1
    return t - sign * (int(off[1:3]) * 3600 + int(off[3:5]) * 60)


def old_round_time_down(dt, offset=30):
    """globals.roundTimeDown before imports.63 (reference)."""
    n = datetime.datetime.fromtimestamp(dt)
    if n.minute > (offset - 1): n = n.replace(minute=offset, second=0, microsecond=0)
    else: n = n.replace(minute=0, second=0, microsecond=0)
    return time.mktime(n.timetuple())


# Changes of UTC offset: (zone, UTC instant of the change)
CHANGES = [('Europe/Madrid', ep(2026, 3, 29, 1)), ('Europe/Madrid', ep(2026, 10, 25, 1)),
           ('Europe/Madrid', ep(2027, 3, 28, 1)), ('America/New_York', ep(2026, 11, 1, 6)),
           ('America/New_York', ep(2027, 3, 14, 7)), ('Asia/Kolkata', ep(2026, 11, 5, 12)),
           ('Australia/Lord_Howe', ep(2026, 10, 3, 15, 30)), ('Australia/Lord_Howe', ep(2027, 4, 3, 15)),
           ('UTC', ep(2026, 11, 5, 12))]


# ================================================================ Python 3.8 deny-list (shared with the file guards)

_PY39_MODULES = {'zoneinfo', 'graphlib', 'tomllib'}
_PY39_ATTRS   = {'removeprefix', 'removesuffix', 'utcnow', 'utcfromtimestamp'}
_GENERIC_BUILTINS = {'list', 'dict', 'tuple', 'set', 'frozenset', 'type'}


def _annotations(tree):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            for a in args.posonlyargs + args.args + args.kwonlyargs + [args.vararg, args.kwarg]:
                if a is not None and a.annotation is not None:
                    yield a.annotation
            if node.returns is not None:
                yield node.returns
        elif isinstance(node, ast.AnnAssign):
            yield node.annotation


def _pep701_fstrings(src):
    """f-strings that only Python 3.12+ reads (PEP 701): the outer quote reused
    inside a replacement field, a backslash or a comment in an expression, or a
    line break in a single-quoted f-string. ast.parse(feature_version=(3, 8))
    accepts them, so they are found with the 3.12+ tokenizer."""
    if not hasattr(tokenize, 'FSTRING_START'):
        return []                      # an older tokenizer can't even read them
    found, stack = [], []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.FSTRING_START:
            quote = tok.string.lstrip('rRbBuUfF')
            if stack and (stack[-1] == quote or (len(stack[-1]) == 1 and quote[0] == stack[-1])):
                found.append(tok.start)
            stack.append(quote)
        elif tok.type == tokenize.FSTRING_END:
            if stack: stack.pop()
        elif stack:
            outer = stack[-1]
            if tok.type == tokenize.STRING:
                body = tok.string.lstrip('rRbBuUfF')
                if (len(outer) == 1 and body[0] == outer) or body.startswith(outer) or '\\' in tok.string:
                    found.append(tok.start)
            elif tok.type == tokenize.COMMENT or (tok.type == tokenize.NL and len(outer) == 1):
                found.append(tok.start)
    return found


def py39_api_leaks(src):
    """Python 3.9+ syntax and APIs that ast.parse(feature_version=(3, 8)) does not
    reject (verified: it accepts zoneinfo, datetime.UTC, removeprefix and PEP 701
    f-strings). Kodi 19/20 users run Python 3.8."""
    tree = ast.parse(src, feature_version=(3, 8))
    leaks = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            leaks += [a.name for a in node.names if a.name.split('.')[0] in _PY39_MODULES]
        elif isinstance(node, ast.ImportFrom):
            if (node.module or '').split('.')[0] in _PY39_MODULES: leaks.append(node.module)
            if node.module == 'functools' and any(a.name == 'cache' for a in node.names): leaks.append('functools.cache')
            if node.module == 'datetime' and any(a.name == 'UTC' for a in node.names): leaks.append('datetime.UTC')
        elif isinstance(node, ast.Attribute):
            if node.attr in _PY39_ATTRS: leaks.append(node.attr)
            if node.attr == 'UTC' and isinstance(node.value, ast.Name) and node.value.id == 'datetime': leaks.append('datetime.UTC')
            if node.attr == 'cache' and isinstance(node.value, ast.Name) and node.value.id == 'functools': leaks.append('functools.cache')
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'zip' \
                and any(k.arg == 'strict' for k in node.keywords):
            leaks.append('zip(strict=)')
        elif hasattr(ast, 'Match') and isinstance(node, ast.Match):
            leaks.append('match statement')
    for ann in _annotations(tree):
        for n in ast.walk(ann):
            if isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name) and n.value.id in _GENERIC_BUILTINS:
                leaks.append('PEP 585 %s[...]' % n.value.id)
            if isinstance(n, ast.BinOp) and isinstance(n.op, ast.BitOr):
                leaks.append('PEP 604 X | Y')
    leaks += ['PEP 701 f-string at %s:%s' % pos for pos in _pep701_fstrings(src)]
    return leaks


@pytest.mark.parametrize('src, leak', [
    ('import zoneinfo', 'zoneinfo'), ('import datetime\nx = datetime.UTC', 'datetime.UTC'),
    ('from datetime import UTC', 'datetime.UTC'), ("s = 'ab'.removeprefix('a')", 'removeprefix'),
    ("s = 'ab'.removesuffix('b')", 'removesuffix'), ('import functools\n@functools.cache\ndef f(): pass', 'functools.cache'),
    ('def f(x: list[int]): pass', 'PEP 585 list[...]'), ('def f(x) -> dict[str, int]: pass', 'PEP 585 dict[...]'),
    ('def f(x: int | None): pass', 'PEP 604 X | Y'), ('d = {}\ns = f"{d["x"]}"', 'PEP 701'),
    ("s = f'{chr(10)!r}' + f'{\"\\n\".join([])}'", 'PEP 701'), ('import datetime\nt = datetime.datetime.utcnow()', 'utcnow'),
])
def test_the_python38_deny_list_finds_newer_apis(src, leak):
    """The deny-list itself: each sample is something Python 3.8 can't run."""
    assert any(found.startswith(leak) for found in py39_api_leaks(src)), py39_api_leaks(src)


def test_the_python38_deny_list_accepts_python38_code():
    ok = ("import datetime, time\nx = datetime.timezone.utc\ny = (z := 3)\n"
          "s = f\"{'/'.join(['a'])} {x!r:>10}\"\ndef f(a: list=[], *, b: dict=None) -> 'list[int]':\n    return a\n")
    assert py39_api_leaks(ok) == []


# ================================================================ clock_helpers hygiene

def test_clock_helpers_is_standard_library_only_and_python38():
    src  = _src('clock_helpers.py')
    tree = ast.parse(src, feature_version=(3, 8))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import): imported.update(a.name.split('.')[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom): imported.add((node.module or '').split('.')[0])
    assert imported == {'calendar', 'datetime', 'time'}
    assert py39_api_leaks(src) == []
    assert 'madteevee (imports fork, imports.63)' in src


def _calls_in(tree):
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)]


def test_only_now_reads_the_clock():
    """time.time() is read in now() and nowhere else; no other clock read at all
    (no-argument localtime/gmtime/ctime/strftime, datetime.now/today/utcnow)."""
    tree = ast.parse(_src('clock_helpers.py'))
    funcs = [f for f in tree.body if isinstance(f, ast.FunctionDef)]
    readers = set()
    for fn in funcs:
        for call in _calls_in(fn):
            f = call.func
            if not isinstance(f, ast.Attribute):
                continue
            dotted = ast.unparse(f)
            if dotted in ('time.time', 'time.monotonic', 'time.perf_counter'):
                readers.add(fn.name)
            assert not (dotted in ('time.localtime', 'time.gmtime', 'time.ctime', 'time.asctime') and not call.args), dotted
            assert not (dotted == 'time.strftime' and len(call.args) < 2), dotted
            assert f.attr not in ('now', 'today', 'utcnow'), dotted
    assert readers == {'now'}


def test_no_module_level_calls():
    """Importing clock_helpers reads nothing: only imports, constants and defs."""
    tree = ast.parse(_src('clock_helpers.py'))
    for node in tree.body:
        assert isinstance(node, (ast.Import, ast.FunctionDef, ast.Assign)), ast.dump(node)[:80]
        if isinstance(node, ast.Assign):
            assert not _calls_in(node), ast.unparse(node)


def test_formats_and_constants():
    assert ch.XMLTV_FORMAT == DTFORMAT
    assert ch.GUIDE_FILL_BUFFER_S == 43200


def test_now_is_time_time(monkeypatch, tz):
    tz('Europe/Madrid')                                            # an offset to subtract, were it slow again
    monkeypatch.setattr(ch.time, 'time', lambda: 1793891463.25)
    assert ch.now() == 1793891463.25


# ================================================================ the zone fixtures (conftest.py)

# The UTC offset in force at each conftest.CLOCK_ZONES instant, written out
# independently of the zone database.
CLOCK_ZONE_OFFSETS = {'utc': 0, 'madrid-winter': 3600, 'madrid-summer': 7200, 'madrid-autumn-first': 7200,
                      'madrid-autumn-second': 3600, 'madrid-spring': 7200, 'new-york': -18000,
                      'kolkata': 19800, 'lord-howe': 37800}


def test_clock_zone_pins_each_zone(clock_zone, request):
    """Each clock_zone case really runs in its own zone: unpinned, every case
    would quietly test the machine zone only, and pass."""
    name = request.node.callspec.params['clock_zone']
    assert time.localtime(clock_zone).tm_gmtoff == CLOCK_ZONE_OFFSETS[name], name


# ================================================================ local_floor

def test_local_floor_is_the_top_of_the_local_hour(clock_zone):
    e = clock_zone
    for minutes in (60, 30, 15, 5, 1):
        f = ch.local_floor(e, minutes)
        lt = time.localtime(f)
        assert isinstance(f, int)
        assert f <= e < f + minutes * 60 and lt.tm_sec == 0 and lt.tm_min % minutes == 0, (minutes, f, e)
    assert ch.local_floor(e + 0.9) == ch.local_floor(e)                 # fractions are cut


def test_local_floor_cuts_the_fraction_up_to_the_last_instant_of_the_hour(clock_zone):
    """Fractions are cut, also a moment before the next hour: a rounded epoch
    would put a new schedule's start at the next hour, in the future."""
    f = ch.local_floor(clock_zone, 60)
    assert ch.local_floor(f + 3599.9, 60) == f
    assert ch.local_floor(f + 3600, 60) == f + 3600


@pytest.mark.parametrize('instant', [ep(2026, 7, 1, 2, 47), ep(2026, 12, 1, 2, 47)])
def test_local_floor_uses_the_offset_of_the_instant(tz, instant):
    """Lord Howe is +10:30 in its winter and +11:00 in its summer: with one offset
    for every instant (the offset of now), one of these two floors lands on :30."""
    tz('Australia/Lord_Howe')
    f = ch.local_floor(instant, 60)
    lt = time.localtime(f)
    assert (lt.tm_min, lt.tm_sec) == (0, 0) and 0 <= instant - f < 3600


@pytest.mark.parametrize('zone, second_half, first_floor, second_floor', [
    ('Europe/Madrid', ep(2026, 10, 25, 1, 30), ep(2026, 10, 25, 0), ep(2026, 10, 25, 1)),   # 02:30 CEST / 02:30 CET
    ('America/New_York', ep(2026, 11, 1, 6, 30), ep(2026, 11, 1, 5), ep(2026, 11, 1, 6)),   # 01:30 EDT / 01:30 EST
])
def test_local_floor_in_the_repeated_hour_does_not_depend_on_earlier_calls(tz, zone, second_half, first_floor, second_floor):
    tz(zone)
    first_half = second_half - 3600
    for prime in (first_half - 7200, second_half + 7200):          # leave mktime in summer, then in winter time
        time.mktime(time.localtime(prime))
        assert ch.local_floor(first_half) == first_floor
        assert ch.local_floor(second_half) == second_floor


def test_old_round_down_was_ambiguous_in_the_repeated_hour(tz):
    """Why the body changed: mktime of a naive repeated time picks either instant."""
    tz('Europe/Madrid')
    e = ep(2026, 10, 25, 1, 30)
    answers = set()
    for prime in (e - 7200, e + 7200):
        time.mktime(time.localtime(prime))
        answers.add(old_round_time_down(e, 60))
    assert ch.local_floor(e, 60) in answers


@pytest.mark.parametrize('zone, change', CHANGES)
def test_local_floor_equals_the_old_round_down_outside_the_repeated_hour(tz, zone, change):
    """local_floor gives the old roundTimeDown result every 5 minutes for 36 hours on
    each side of a change; only the repeated hour differs (there the old body could
    give either answer). Lord Howe's 30-minute change put the old floor off the hour."""
    tz(zone)
    skipped = 0
    for k in range(-36 * 12, 36 * 12):
        e = change + k * 300 + 7
        if time.localtime(e - 3600).tm_gmtoff > time.localtime(e).tm_gmtoff and 0 <= k < 12:
            skipped += 1                                           # the repeated hour (autumn)
            continue
        for minutes in (60, 30):
            f = ch.local_floor(e, minutes)
            assert f <= e < f + minutes * 60                        # the invariant, in every zone
            if zone != 'Australia/Lord_Howe':
                assert f == old_round_time_down(e, minutes), (zone, e, minutes)
    assert skipped in (0, 12)


@pytest.mark.parametrize('minutes', [0, 7, 45, -60, 90, True, 60.0, None])
def test_local_floor_rejects_steps_that_do_not_divide_the_hour(minutes):
    with pytest.raises(ValueError):
        ch.local_floor(ep(2026, 11, 5, 7, 28, 47), minutes)


# ================================================================ xmltv_time / xmltv_to_epoch

@pytest.mark.parametrize('zone, change', CHANGES)
def test_writer_and_readers_agree_every_five_minutes_around_each_change(tz, zone, change):
    tz(zone)
    for k in range(-36 * 12, 36 * 12):
        e = change + k * 300
        text = ch.xmltv_time(e)
        assert re.fullmatch(r'\d{14} [+-]\d{4}', text), text
        assert text.endswith(' ' + offset_text(e)), (text, e)        # the offset of the instant itself
        assert ch.xmltv_to_epoch(text) == e == iptv_simple_parse(text), (text, e)


@pytest.mark.parametrize('frac', [0.0, 0.25, 0.999, 0.9999995, 0.4999995])
def test_writer_digits_are_todays_digits(clock_zone, frac):
    """Same-offset times are byte-identical with the pre-imports.63 writer
    (fromtimestamp digits + the offset of now, here the instant's own offset),
    including how microseconds round."""
    e = clock_zone + frac
    assert ch.xmltv_time(e) == datetime.datetime.fromtimestamp(e).strftime(DTFORMAT) + ' ' + offset_text(e)


@pytest.mark.parametrize('instant, text', [
    (ep(2026, 10, 24, 22, 59, 59), '20261025005959 +0200'),
    (ep(2026, 10, 25, 0, 30),      '20261025023000 +0200'),           # the first 02:30
    (ep(2026, 10, 25, 1, 30),      '20261025023000 +0100'),           # the second 02:30
    (ep(2027, 3, 28, 0, 59, 59),   '20270328015959 +0100'),
    (ep(2027, 3, 28, 1),           '20270328030000 +0200'),
])
def test_each_time_carries_its_own_offset_in_madrid(tz, instant, text):
    tz('Europe/Madrid')
    assert ch.xmltv_time(instant) == text and ch.xmltv_to_epoch(text) == instant


def test_offset_vectors():
    base = ep(2026, 11, 3, 12)
    assert ch.xmltv_to_epoch('20261103120000 +0000') == base
    assert ch.xmltv_to_epoch('20261103130000 +0100') == base
    assert ch.xmltv_to_epoch('20261103070000 -0500') == base
    assert ch.xmltv_to_epoch('20261103173000 +0530') == base
    assert ch.xmltv_to_epoch('  20261103120000  +0000 ') == base
    assert ch.xmltv_to_epoch('20261103130000+0100') == base               # no space
    assert ch.xmltv_to_epoch('20261103223000 +1030') == base


def test_offsetless_times_are_utc(tz):
    """Changed meaning (flagged): the XMLTV DTD and IPTV Simple read a time
    without an offset as UTC; before imports.63 it was read as local time."""
    tz('Europe/Madrid')
    assert ch.xmltv_to_epoch('20261105120000') == ep(2026, 11, 5, 12)
    assert ch.xmltv_to_epoch(' 20261105120000 ') == ep(2026, 11, 5, 12)
    assert ch.xmltv_to_epoch('20270328023000') == ep(2027, 3, 28, 2, 30)  # no such local time in Madrid
    assert ch.xmltv_to_epoch('20261025023000') == ep(2026, 10, 25, 2, 30)  # a repeated local time in Madrid


@pytest.mark.parametrize('bad', [None, '', 5, 'garbage', '2026110312000', '202611031200xx +0000', '20261303120000 +0000',
                                 '20261103250000', '20261103120000 +01:00', '20261103120000 GMT', '20261103120000 +01',
                                 b'20261103120000'])
def test_xmltv_to_epoch_rejects_what_it_cannot_read(bad):
    assert ch.xmltv_to_epoch(bad) is None


# ================================================================ epg_utc_to_epoch

def test_epg_utc_to_epoch_is_utc(tz):
    tz('Europe/Madrid')
    assert ch.epg_utc_to_epoch('2026-11-05 16:13:09') == ep(2026, 11, 5, 16, 13, 9)
    assert ch.epg_utc_to_epoch(' 2026-11-05 16:13:09 ') == ep(2026, 11, 5, 16, 13, 9)
    assert ch.epg_utc_to_epoch('2026-07-01 10:30:00') == ep(2026, 7, 1, 10, 30)
    for bad in ('2026-11-05', '', '  ', None, 5, '2026-13-05 10:00:00'):
        assert ch.epg_utc_to_epoch(bad) is None


# ================================================================ build_window

@pytest.mark.parametrize('days', [1, 3, 14])
def test_build_window(clock_zone, days):
    anchor, threshold = ch.build_window(clock_zone, days)
    assert anchor == ch.local_floor(clock_zone, 60) and isinstance(anchor, int)
    assert threshold == clock_zone + days * 86400 - 43200


def test_tmnt_rebuild_anchor_is_the_real_hour(tz):
    """2026-11-05 08:28:47 CET: the slow clock's anchor was 07:00 CET; now 08:00 CET."""
    tz('Europe/Madrid')
    now = ep(2026, 11, 5, 7, 28, 47)
    assert ch.build_window(now, 1)[0] == ep(2026, 11, 5, 7)
    assert old_round_time_down(now - 3600, 60) == ep(2026, 11, 5, 6)      # what getUTCstamp gave


# ================================================================ join_offset

@pytest.mark.parametrize('start, stop, now, expected', [
    (1793888180, 1793893725, 1793891463, 3283),        # Weird Tales 11-05, 54 min 43 s in
    (1793880000, 1793880000 + 7200, 1793880000 + 5400, 5400),     # 90 min into a 120-min film
    (1793880000, 1793881800, 1793883600, None),        # ended 30 min ago
    (1793880000, 1793881800, 1793880000, 0),           # starts now
    (1793880000, 1793881800, 1793881800, None),        # ends now: half-open
    (1793880000, 1793881800, 1793881799, 1799),
    (1793880000.9, 1793881800.2, 1793880100.7, 100),   # epochs as floats: whole seconds
    ('1793880000', '1793881800', '1793880100', 100),   # numeric text
    (None, 1793881800, 1793880100, None), (1793880000, None, 1793880100, None),
    (1793880000, 1793881800, None, None), ('x', 1, 2, None), (0, 1793881800, 1793880100, None),
    (-5, 1793881800, 1793880100, None), (1793881800, 1793880000, 1793880100, None), (float('inf'), 1, 1, None),
])
def test_join_offset(start, stop, now, expected):
    assert ch.join_offset(start, stop, now) == expected


def test_the_slow_clock_missed_the_join():
    """The 2026-11-05 16:11 'Play programme' on Weird Tales, had the times arrived:
    getUTCstamp gave now - 3600, before the show started."""
    assert ch.join_offset(1793888180, 1793893725, 1793891463 - 3600) is None
    assert ch.join_offset(1793888180, 1793893725, 1793891463) == 3283


# ================================================================ plot_times_match

@pytest.mark.parametrize('payload, file, expected', [
    ({'file': '/m/a.mkv', 'start': 1}, '/m/a.mkv', True),
    ({'file': '/m/b.mkv', 'originalfile': '/m/a.mkv'}, '/m/a.mkv', True),     # originalfile wins (the catch-up id's vid)
    ({'file': '/m/a.mkv', 'originalfile': '/m/b.mkv'}, '/m/a.mkv', False),
    ({'file': '/m/b.mkv'}, '/m/a.mkv', False),                                 # another programme's file
    ({'file': '/m/a.mkv'}, '', False), ({'file': '/m/a.mkv'}, None, False),
    ({}, '/m/a.mkv', False), (None, '/m/a.mkv', False), ({'file': 5}, '5', False),
    ({'file': '/m/A.mkv'}, '/m/a.mkv', False),                                 # exact, case-sensitive
])
def test_plot_times_match(payload, file, expected):
    assert ch.plot_times_match(payload, file) is expected


# ================================================================ replay_helpers: re-export, rebuild_resume floor, guide_bounds

def P(ch_id, start, stop, cid='', title='t'):
    return {'channel': ch_id, 'start': start, 'stop': stop, 'catchup-id': cid, 'title': [(title, '')]}


NOW   = ep(2026, 11, 5, 7, 28, 47)       # 08:28:47 CET, the 11-05 TMNT rebuild request
FLOOR = ep(2026, 11, 5, 7)               # 08:00 CET


def test_moved_parsers_are_re_exported_as_the_same_objects():
    assert replay_helpers.xmltv_to_epoch is ch.xmltv_to_epoch          # imports.py, server.py, xmltvs.py, tests
    assert restart_helpers.epg_utc_to_epoch is ch.epg_utc_to_epoch     # restart_helpers itself, the imports.62 tests


@pytest.mark.parametrize('progs, expected', [
    ([P(X, '20261105075950 +0100', '20261105083050 +0100')], ep(2026, 11, 5, 7, 30, 50)),   # on air: its stop
    ([P(X, '20261105070000 +0100', '20261105081000 +0100')], ep(2026, 11, 5, 7, 10)),       # ran out after the hour
    ([P(X, '20261105050000 +0100', '20261105060000 +0100')], FLOOR),                         # ran out before the hour
    ([], FLOOR),                                                                              # a new channel
    ([P(X, '20261105090000 +0100', '20261105100000 +0100')], FLOOR),                         # only future rows
    ([P(X, 'garbage', '20261105083050 +0100')], FLOOR),                                       # unparseable: ignored
    ([P(X, '20261105062000 +0000', '20261105073000 +0000')], ep(2026, 11, 5, 7, 30)),        # a '+0000' row on air
    ([P(Y, '20261105075950 +0100', '20261105093050 +0100')], FLOOR),                         # another channel: ignored
])
def test_rebuild_resume_with_a_floor(progs, expected):
    """Epochs only: no zone needed. The cut is the real now, with its fraction."""
    got = replay_helpers.rebuild_resume(progs, X, NOW + 0.4, FLOOR)
    assert got == expected and isinstance(got, int)                   # never the raw float now


def test_rebuild_resume_without_a_floor_is_the_imports61_rule():
    ran_out = [P(X, '20261105070000 +0100', '20261105081000 +0100')]
    on_air  = [P(X, '20261105075950 +0100', '20261105083050 +0100')]
    for progs in (ran_out, on_air, [], [P(X, 'x', 'y')]):
        imports61 = max([NOW] + [ch.xmltv_to_epoch(p['stop']) for p in progs
                                 if ch.xmltv_to_epoch(p['start']) is not None and ch.xmltv_to_epoch(p['start']) < NOW
                                 and ch.xmltv_to_epoch(p['stop']) is not None])
        assert replay_helpers.rebuild_resume(progs, X, NOW) == imports61
        assert replay_helpers.rebuild_resume(progs, X, NOW, None) == imports61


def test_guide_bounds_compares_times_not_text():
    progs = [P(X, '20261105083000 +0100', '20261105090000 +0100'),     # 07:30-08:00 UTC
             P(X, '20261105080000 +0000', '20261105081500 +0000'),     # 08:00-08:15 UTC: its text is earlier, its time later
             P(X, 'bad', '20261105230000 +0100'), P(X, '20261105230000 +0100', None),
             P(Y, '20261101000000 +0000', '20261201000000 +0000'),
             'not a dict', None, {'channel': ['not', 'text'], 'start': '20261105000000 +0000', 'stop': '20261106000000 +0000'}]
    expected = {X: (ep(2026, 11, 5, 7, 30), ep(2026, 11, 5, 8, 15))}
    assert replay_helpers.guide_bounds(progs, [X]) == expected
    assert replay_helpers.guide_bounds(progs, [X, 'none', None]) == expected
    assert replay_helpers.guide_bounds(progs, [X, Y])[Y] == (ep(2026, 11, 1), ep(2026, 12, 1))
    assert replay_helpers.guide_bounds([], [X]) == {} and replay_helpers.guide_bounds(None, None) == {}


# ================================================================ globals: roundTimeDown, epochTime, diffRuntime

def old_diff_runtime(dur, roundto=15):
    """globals.diffRuntime before imports.63 (reference)."""
    def ceil_dt(dt, delta):
        return dt + (datetime.datetime.min - dt) % delta
    now = datetime.datetime.fromtimestamp(dur)
    return (ceil_dt(now, datetime.timedelta(minutes=roundto)) - now).total_seconds()


def test_round_time_down_is_local_floor(clock_zone):
    for minutes in (60, 30):
        got = globals_mod.roundTimeDown(clock_zone, offset=minutes)
        assert got == ch.local_floor(clock_zone, minutes) and isinstance(got, int)
    with pytest.raises(ValueError):
        globals_mod.roundTimeDown(clock_zone, 45)


@pytest.mark.parametrize('zone, second_half, first_floor, second_floor', [
    ('Europe/Madrid', ep(2026, 10, 25, 1, 30), ep(2026, 10, 25, 0), ep(2026, 10, 25, 1)),
    ('America/New_York', ep(2026, 11, 1, 6, 30), ep(2026, 11, 1, 5), ep(2026, 11, 1, 6)),
])
def test_round_time_down_is_deterministic_in_the_repeated_hour(tz, zone, second_half, first_floor, second_floor):
    tz(zone)
    for prime in (second_half - 10800, second_half + 7200):
        time.mktime(time.localtime(prime))
        assert globals_mod.roundTimeDown(second_half - 3600, offset=60) == first_floor
        assert globals_mod.roundTimeDown(second_half, offset=60) == second_floor


@pytest.mark.parametrize('zone, fmt, text', [
    ('Europe/Madrid', '%I:%M%p', '04:19PM'),                       # the 11-05 Up next case: 15:19:30 UTC
    ('UTC', '%I:%M%p', '03:19PM'),
    ('Europe/Madrid', BACKUP_TIME_FORMAT, '2026-11-05 04:19 PM'),  # PauseRule 'last viewed', backup dates
])
def test_epoch_time_shows_the_true_local_time(tz, zone, fmt, text):
    import inspect
    assert list(inspect.signature(globals_mod.epochTime).parameters) == ['timestamp']
    tz(zone)
    assert globals_mod.epochTime(1793891970).strftime(fmt) == text
    with pytest.raises(TypeError):
        globals_mod.epochTime(1793891970, tz=False)                 # the switch is gone: old callers fail loudly


@pytest.mark.parametrize('zone', ['Europe/Madrid', 'UTC', 'Asia/Kolkata'])
def test_diff_runtime_is_arithmetic(tz, zone):
    tz(zone)
    for dur in range(0, 7300, 7):                                  # whole seconds: identical
        assert globals_mod.diffRuntime(dur) == old_diff_runtime(dur) == (-dur) % 900
    for dur in (1234.5, 899.999, 900.0, 0.25):                     # fractions: within float rounding
        assert globals_mod.diffRuntime(dur) == pytest.approx(old_diff_runtime(dur), abs=1e-6)
    assert isinstance(globals_mod.diffRuntime(900), float)
    assert globals_mod.diffRuntime(1000, 30) == 800.0


def test_diff_runtime_no_longer_depends_on_the_1970_offset(tz):
    tz('Africa/Monrovia')                                          # 1970: UTC-00:44:30
    assert globals_mod.diffRuntime(900) == 0.0 and old_diff_runtime(900) != 0.0


def test_retired_globals_are_gone():
    for name in ('getUTCstamp', 'getTimeoffset', 'getGMTstamp', 'roundTimeUp'):
        assert not hasattr(globals_mod, name), name


# ================================================================ XMLTVS: readers, writer, Min_Days trim (zone pinned)

def _xmltvs(programmes, channels=None):
    x = XMLTVS.__new__(XMLTVS)
    x.m3u = None
    x.XMLTVDATA = {'data': {}, 'channels': channels if channels is not None else [{'id': X}, {'id': Y}],
                   'programmes': programmes, 'recordings': []}
    x.lines = []
    x.log = lambda msg, level=xbmc.LOGDEBUG: x.lines.append((msg, level))
    return x


def test_load_stop_times_reads_epochs(monkeypatch, tz):
    tz('Europe/Madrid')
    monkeypatch.setattr(ch, 'now', lambda: NOW + 0.4)
    progs = [P(X, '20261105060000 +0100', '20261105070000 +0100'), P(X, '20261105070000 +0100', '20261110093000 +0100'),
             P(Y, '20261105060000 +0000', '20261106000000 +0000'), P(Y, 'bad', 'worse')]
    x = _xmltvs(progs, channels=[{'id': X}, {'id': Y}, {'id': 'empty'}])
    stops = dict(x.loadStopTimes(fallback_epoch=FLOOR))
    assert stops == {X: ep(2026, 11, 10, 8, 30), Y: ep(2026, 11, 6), 'empty': FLOOR}
    assert all(isinstance(v, int) for v in stops.values())         # never None, never text
    default = dict(x.loadStopTimes([{'id': 'empty'}], progs))      # default fallback: the real hour floor
    assert default == {'empty': ch.local_floor(NOW)} == {'empty': FLOOR}
    assert not [m for m, level in x.lines if level == xbmc.LOGWARNING]


def test_load_stop_times_rebuilds_a_guide_that_starts_in_the_future(tz):
    tz('Europe/Madrid')
    x = _xmltvs([P(X, '20261105100000 +0100', '20261105110000 +0100')])
    assert dict(x.loadStopTimes([{'id': X}], fallback_epoch=FLOOR)) == {X: FLOOR}
    warnings = [m for m, level in x.lines if level == xbmc.LOGWARNING]
    assert len(warnings) == 1 and 'first start-time in the future, rebuilding channel from 20261105080000' in warnings[0]


def test_load_stop_times_takes_the_latest_time_not_the_latest_text():
    """The repeated autumn hour: '02:45 +0200' (00:45Z) sorts after '02:15 +0100'
    (01:15Z) as text; the guide really ends at 01:15Z."""
    progs = [P(X, '20261025020000 +0200', '20261025024500 +0200'), P(X, '20261025024500 +0200', '20261025021500 +0100')]
    assert dict(_xmltvs(progs).loadStopTimes([{'id': X}], fallback_epoch=ep(2026, 10, 25))) == {X: ep(2026, 10, 25, 1, 15)}


def test_load_stop_times_old_keyword_fails_loudly():
    with pytest.raises(TypeError):
        dict(_xmltvs([]).loadStopTimes([{'id': X}], [P(X, '1', '2')], fallback='20261105080000'))
    with pytest.raises(TypeError):
        dict(_xmltvs([]).hasProgrammes([{'id': X}], [P(X, '1', '2')], now=FLOOR))


def test_has_programmes_compares_times_with_the_real_hour(monkeypatch, tz):
    tz('Europe/Madrid')
    monkeypatch.setattr(ch, 'now', lambda: NOW)                    # 08:28:47 CET, hour floor 08:00 CET = 07:00Z
    progs = [P(X, '20261105070000 +0000', '20261105073000 +0000'),     # stops 07:30Z: after the hour
             P(Y, '20261105070000 +0100', '20261105075500 +0100'),     # stops 06:55Z: before it
             P('Z', '20261105070000 +0000', '20261105071500 +0000'),   # stops 07:15Z: after the hour, before now
             P('W', 'bad', 'bad')]
    x = _xmltvs(progs, channels=[{'id': X}, {'id': Y}, {'id': 'Z'}, {'id': 'W'}, {'id': 'none'}])
    assert dict(x.hasProgrammes()) == {X: True, Y: False, 'Z': True, 'W': False, 'none': False}
    assert dict(x.hasProgrammes([{'id': X}], progs, now_epoch=ep(2026, 11, 5, 7, 30))) == {X: False}


def test_min_days_trim_uses_the_real_clock_and_parsed_stops(monkeypatch, tz):
    tz('Europe/Madrid')
    monkeypatch.setattr(ch, 'now', lambda: NOW)
    monkeypatch.setattr(xmltvs, 'MIN_GUIDEDAYS', 3)
    cutoff = NOW - 3 * 86400

    def utc_text(e):
        return time.strftime('%Y%m%d%H%M%S', time.gmtime(e)) + ' +0000'
    progs = [P(X, ch.xmltv_time(cutoff - 7200), ch.xmltv_time(cutoff - 1800), title='own old'),     # '+0100'
             P(X, ch.xmltv_time(cutoff - 3600), ch.xmltv_time(cutoff + 1800), title='own kept'),
             P(Y, utc_text(cutoff - 7200), utc_text(cutoff - 1800), title='mp old'),                 # '+0000'
             P(Y, utc_text(cutoff - 3600), utc_text(cutoff + 1800), title='mp kept'),
             P(Y, utc_text(cutoff - 7200), 'garbage', title='unparseable kept'),
             {'channel': Y, 'start': 'x', 'title': [('no stop', '')]}]
    assert progs[0]['stop'].endswith(' +0100')
    kept = [p['title'][0][0] for p in _xmltvs([]).cleanProgrammes(progs)]
    assert kept == ['own kept', 'mp kept', 'unparseable kept', 'no stop']


def _item(start, stop, label):
    return {'categories': ['Drama'], 'title': label, 'desc': 'plot', 'start': start, 'stop': stop, 'thumb': '',
            'length': stop - start, 'catchup-id': 'plugin://x/?vid=%s' % label}


def test_writer_stamps_each_time_with_its_own_offset(tz, monkeypatch):
    """The summer-time writer bug: written on 2026-10-20 (CEST) for 10-27 (CET)."""
    tz('Europe/Madrid')
    monkeypatch.setattr(ch, 'now', lambda: ep(2026, 10, 20, 10))
    x = _xmltvs([])
    x.addProgram(X, _item(ep(2026, 10, 27, 19), ep(2026, 10, 27, 20), 'after the change'), encodeDESC=False)
    x.addProgram(X, _item(ep(2026, 10, 25, 0, 30), ep(2026, 10, 25, 1, 30), 'across it'), encodeDESC=False)
    a, b = x.XMLTVDATA['programmes']
    assert (a['start'], a['stop']) == ('20261027200000 +0100', '20261027210000 +0100')
    assert (b['start'], b['stop']) == ('20261025023000 +0200', '20261025023000 +0100')
    assert [iptv_simple_parse(p[k]) for p in (a, b) for k in ('start', 'stop')] == \
           [ep(2026, 10, 27, 19), ep(2026, 10, 27, 20), ep(2026, 10, 25, 0, 30), ep(2026, 10, 25, 1, 30)]


def test_writer_is_byte_identical_for_todays_own_times(tz):
    """For a time with the same offset as the moment of writing, the text is what
    the pre-imports.63 writer produced (fromtimestamp digits + ' +HHMM')."""
    tz('Europe/Madrid')
    x = _xmltvs([])
    start, stop = 1793888180.25, 1793893725.999
    x.addProgram(X, _item(start, stop, 'Weird Tales'), encodeDESC=False)
    p = x.XMLTVDATA['programmes'][0]
    assert (p['start'], p['stop']) == ('20261105151620 +0100', '20261105164845 +0100')
    assert p['start'] == datetime.datetime.fromtimestamp(start).strftime(DTFORMAT) + ' +0100'


def test_recording_slot_starts_now(monkeypatch):
    monkeypatch.setattr(ch, 'now', lambda: NOW + 0.5)
    x = _xmltvs([])
    x.findRecording = lambda ritem: (None, {})
    x.getProgramItem = lambda ritem, fitem: dict(_item(fitem['start'], fitem['stop'], 'rec'), fitem=fitem)
    fitem = {'duration': 600}
    assert x.addRecording({'id': '0123456789abcdef', 'name': 'Rec', 'logo': ''}, fitem)
    assert fitem['start'] == NOW + 0.5 and fitem['stop'] == NOW + 600.5


def test_trim_log_shows_the_cut_in_local_time_with_seconds(tz):
    tz('Europe/Madrid')
    x = _xmltvs([P(X, '20261105075950 +0100', '20261105083050 +0100'), P(X, '20261105083050 +0100', '20261105084245 +0100')])
    x.trimBroadcast({'id': X}, NOW + 0.4)
    info = [m for m, level in x.lines if level == xbmc.LOGINFO]
    assert len(info) == 1 and 'trimBroadcast, cut 20261105082847: kept 1 aired, removed 1' in info[0]


def test_resume_after_passes_cut_and_floor():
    ran_out = _xmltvs([P(X, '20261105070000 +0100', '20261105081000 +0100')])
    assert ran_out.resumeAfter({'id': X}, NOW, FLOOR) == ep(2026, 11, 5, 7, 10)
    assert ran_out.resumeAfter({'id': X}, NOW) == NOW                 # the imports.61 rule without a floor
    empty = _xmltvs([])
    assert empty.resumeAfter({'id': X}, NOW + 0.4, FLOOR) == FLOOR
    on_air = _xmltvs([P(X, '20261105075950 +0100', '20261105083050 +0100')])
    assert on_air.resumeAfter({'id': X}, NOW, FLOOR) == ep(2026, 11, 5, 7, 30, 50)


# ================================================================ Builder._rebuildStart: the late cut, and its wiring

TMNT_FIXTURE = os.path.join(HERE, 'fixtures', 'imports63_tmnt_guide_2026-11-05.json')
TRIM  = ep(2026, 11, 5, 7, 28, 47)     # 08:28:47 CET: rebuild requested (imports.62 trimmed here, at its 07:00 anchor)
LAY   = ep(2026, 11, 5, 7, 38, 1)      # 08:38:01 CET: 'addScheduling' after the ~9-minute build
SAVED = ep(2026, 11, 5, 7, 38, 5)      # 08:38:05 CET: guide file written


def _fake_builder(programmes):
    b = types.SimpleNamespace(xmltv=_xmltvs(programmes), lines=[])
    b.log = lambda msg, level=xbmc.LOGDEBUG: b.lines.append((msg, level))
    b._rebuildContinuesAfterAired = Builder._rebuildContinuesAfterAired
    return b


# TMNT 11-05 slots (CET): on air at the request, started during the build, the next one
TMNT = [P(X, '20261105081854 +0100', '20261105083050 +0100', 'a'), P(X, '20261105083050 +0100', '20261105084245 +0100', 'b'),
        P(X, '20261105084245 +0100', '20261105085441 +0100', 'c')]


def test_late_cut_keeps_the_show_that_started_during_the_build(tz, monkeypatch):
    tz('Europe/Madrid')
    b = _fake_builder(list(TMNT))
    monkeypatch.setattr(ch, 'now', lambda: LAY + 0.4)                  # the clock reads the moment it is laid
    start = Builder._rebuildStart(b, {'id': X, 'rules': {'51': {}}})
    assert [p['catchup-id'] for p in b.xmltv.XMLTVDATA['programmes']] == ['a', 'b']   # 'b' started 08:30:50: kept
    assert start == ch.xmltv_to_epoch('20261105084245 +0100') and isinstance(start, int)
    trim = [m for m, level in b.xmltv.lines if level == xbmc.LOGINFO]
    assert len(trim) == 1 and 'trimBroadcast, cut 20261105083801: kept 2 aired, removed 1' in trim[0]
    info = [m for m, level in b.lines if level == xbmc.LOGINFO]
    assert info == ['[%s] buildChannels, rebuild keeps the aired guide; new schedule from 20261105084245 (cut 20261105083801)' % X]


def test_a_cut_before_the_build_would_replace_it(tz, monkeypatch):
    """What a cut at the moment of the request (the rejected option A) does on the
    same data: 'b' is replaced although it had aired 7 minutes by the time the
    guide was written."""
    tz('Europe/Madrid')
    b = _fake_builder(list(TMNT))
    monkeypatch.setattr(ch, 'now', lambda: TRIM + 0.4)
    Builder._rebuildStart(b, {'id': X})
    assert [p['catchup-id'] for p in b.xmltv.XMLTVDATA['programmes']] == ['a']


@pytest.mark.parametrize('rules', [{'3000': {}}, {3000: {}}])
def test_pause_rule_cuts_and_starts_at_the_real_hour(tz, monkeypatch, rules):
    tz('Europe/Madrid')
    b = _fake_builder([P(X, '20261105075000 +0100', '20261105081854 +0100', 'old')] + list(TMNT))
    monkeypatch.setattr(ch, 'now', lambda: LAY + 0.4)
    start = Builder._rebuildStart(b, {'id': X, 'rules': rules})
    assert start == ep(2026, 11, 5, 7) and isinstance(start, int)      # 08:00 CET, the real hour (slow clock: 07:00)
    assert [p['catchup-id'] for p in b.xmltv.XMLTVDATA['programmes']] == ['old']   # cut at the floor too
    assert 'new schedule from 20261105080000 (cut 20261105080000)' in b.lines[-1][0]


def test_nothing_on_air_starts_at_the_later_of_the_hour_and_the_last_stop(tz, monkeypatch):
    tz('Europe/Madrid')
    monkeypatch.setattr(ch, 'now', lambda: LAY + 0.7)
    ran_out_in_the_hour = _fake_builder([P(X, '20261105080000 +0100', '20261105081000 +0100', 'x')])
    assert Builder._rebuildStart(ran_out_in_the_hour, {'id': X}) == ch.xmltv_to_epoch('20261105081000 +0100')
    ran_out_before = _fake_builder([P(X, '20261105060000 +0100', '20261105070000 +0100', 'y')])
    assert Builder._rebuildStart(ran_out_before, {'id': X}) == ep(2026, 11, 5, 7)
    empty = _fake_builder([])
    start = Builder._rebuildStart(empty, {'id': X})
    assert start == ep(2026, 11, 5, 7) and isinstance(start, int)


def test_tmnt_rebuild_of_2026_11_05_replayed(tz, monkeypatch):
    """The real TMNT guide before its 11-05 rebuild (times only), cut the three
    ways: imports.62's slow-clock hour replaced 6 shows that had started; a cut at
    the request still replaced the one that started during the build; the late cut
    keeps all 101 and continues at 08:42:45."""
    tz('Europe/Madrid')
    with open(TMNT_FIXTURE, encoding='utf-8') as f:
        guide = [P(X, start, stop, cid='%03d' % i) for i, (start, stop) in enumerate(json.load(f)['programmes'])]
    assert len(guide) == 500

    def replay(clock):
        b = _fake_builder([dict(p) for p in guide])
        monkeypatch.setattr(ch, 'now', lambda: clock)
        start = Builder._rebuildStart(b, {'id': X, 'rules': {'51': {}}})
        kept = {p['catchup-id'] for p in b.xmltv.XMLTVDATA['programmes']}
        replaced = [p for p in guide if ch.xmltv_to_epoch(p['start']) < SAVED and p['catchup-id'] not in kept]
        return len(kept), len(replaced), time.strftime('%H:%M:%S', time.localtime(start))

    slow_hour = old_round_time_down(TRIM - 3600, 60)
    out, kept_62, removed_62 = replay_helpers.split_for_rebuild(guide, X, slow_hour)
    assert (kept_62, removed_62) == (95, 405)                            # the imports.62 log line
    assert len([p for p in guide if ch.xmltv_to_epoch(p['start']) < SAVED and p not in out]) == 6
    assert replay(TRIM + 0.4) == (100, 1, '08:30:50')                  # rejected option A
    assert replay(LAY + 0.4) == (101, 0, '08:42:45')                   # imports.63


def _code(text):
    return '\n'.join(line.split('#')[0].rstrip() for line in text.splitlines())


def _func(tree, name):
    return next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)


def _parents(tree):
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _compiled(name, funcs, namespace):
    """The named function definitions (methods and the closures nested in them)
    compiled out of an add-on module and run with `namespace` as their globals,
    the house pattern of the imports.60/.62 glue tests: the real code, with
    fakes for what it reads. A closure's free variables come from `namespace`."""
    tree = ast.parse(_src(name))
    body = []
    for func in funcs:
        node = copy.deepcopy(_func(tree, func))
        node.decorator_list = []
        body.append(node)
    exec(compile(ast.Module(body=body, type_ignores=[]), name, 'exec'), namespace)
    return [namespace[func] for func in funcs]


def _stores(func, name):
    """Line numbers where `func`'s own scope binds `name` (assignments of any
    kind, loop and with targets, del); nested defs and lambdas have their own
    scope and are skipped."""
    found, todo = [], list(func.body)
    while todo:
        node = todo.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        if isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, (ast.Store, ast.Del)):
            found.append(node.lineno)
        todo.extend(ast.iter_child_nodes(node))
    return sorted(found)


def test_clr_station_only_resets_the_playlist():
    tree = ast.parse(_src('builder.py'))
    clr = _func(tree, '__clrStation')
    calls = {ast.unparse(c.func) for c in _calls_in(clr)}
    assert 'self.resetPagination' in calls
    assert not {c for c in calls if c.split('.')[-1] in ('trimBroadcast', 'delBroadcast', 'delStation', 'clrProgrammes')}
    assert [a.arg for a in clr.args.args] == ['citem']


def test_changed_is_cleared_whatever_clr_station_returns():
    tree = ast.parse(_src('builder.py'))
    has_changed = _func(tree, '__hasChanged')
    parents = _parents(tree)
    clears = [n for n in ast.walk(has_changed) if isinstance(n, ast.Assign)
              and ast.unparse(n.targets[0]) == "citem['changed']" and ast.unparse(n.value) == 'False']
    assert len(clears) == 1
    node = clears[0]
    while node is not has_changed:                                     # no If around it that tests __clrStation
        node = parents[node]
        if isinstance(node, ast.If):
            assert '__clrStation' not in ast.unparse(node.test), ast.unparse(node.test)
    code = _code(_src('builder.py'))
    body = code[code.index('def __hasChanged('):code.index('def __hasProgrammes(')]
    assert 0 < body.index('self._snapshotChannel(citem)') < body.index('__clrStation(citem)') < body.index("citem['changed'] = False")


def test_changed_is_cleared_when_the_playlist_reset_reports_false():
    """The REAL __hasChanged and __clrStation closures of Builder.buildChannels,
    compiled out of builder.py: resetPagination returning False still clears
    'changed' (also when its result is kept in a variable, which the AST test
    above can't see), the snapshot comes first, and nothing trims the guide here
    (Builder._rebuildStart does, after the build)."""
    calls = []
    fake = types.SimpleNamespace(
        log=lambda *a, **k: None,
        _snapshotChannel=lambda citem: calls.append(('snapshot', citem['id'])),
        resetPagination=lambda citem: calls.append(('reset', citem['id'])) or False,
        channels=types.SimpleNamespace(addChannel=lambda citem: calls.append(('addChannel', dict(citem))) or True),
        xmltv=types.SimpleNamespace(trimBroadcast=lambda *a: calls.append(('trim',) + a) or True))
    namespace = {'self': fake, 'SETTINGS': types.SimpleNamespace(getSettingBool=lambda key: False),
                 'KODI_PLAYLISTS': [], 'BASIC_PLAYLISTS': [], 'changes': set(), 'modified_ids': set(), 'nstart': FLOOR}
    has_changed = _compiled('builder.py', ['__clrStation', '__hasChanged'], namespace)[1]
    citem = {'id': X, 'changed': True}
    assert has_changed(citem, False) is True and citem['changed'] is False
    assert calls == [('snapshot', X), ('reset', X), ('addChannel', {'id': X, 'changed': False})]
    assert namespace['changes'] == {True} and namespace['modified_ids'] == {X}
    calls.clear()
    assert has_changed({'id': Y, 'changed': False}, False) is False and calls == []


def test_the_cut_is_taken_after_the_build_right_before_the_schedule():
    code = _code(_src('builder.py'))
    loop = code[code.index('_update, start = __needsUpdate(citem,'):]
    order = [loop.index('_changed = __hasChanged(citem,'), loop.index('fileList = self.buildVideo(citem)'),
             loop.index('if _changed: start = self._rebuildStart(citem)'), loop.index('self.addScheduling(citem, fileList, now, start)')]
    assert order == sorted(order)
    assert code.count('self._rebuildStart(citem)') == 1
    tree = ast.parse(_src('builder.py'))
    rebuild = _func(tree, '_rebuildStart')
    assert [ast.unparse(c) for c in _calls_in(rebuild) if ast.unparse(c.func) == 'clock_helpers.now'] == ['clock_helpers.now()']


def test_build_pass_reads_the_real_clock_and_shares_the_window():
    tree = ast.parse(_src('builder.py'))
    build = _func(tree, 'buildChannels')
    nows = [ast.unparse(n.value) for n in ast.walk(build) if isinstance(n, ast.Assign)
            and [ast.unparse(t) for t in n.targets] == ['now']]
    assert nows == ['clock_helpers.now()']                             # the one build-pass now: the real clock
    code = _code(_src('builder.py'))
    assert 'fallback  = nstart' in code                                # an epoch: the 'Building' label compares like with like
    assert 'elif  start == fallback:' in code
    assert 'def __needsUpdate(citem, threshold, fallback, state=True):' in code
    assert 'if last_stop > threshold: state = False' in code
    assert 'loadStopTimes([citem], fallback_epoch=fallback)' in code
    assert '_update, start = __needsUpdate(citem, threshold, fallback)' in code
    tasks = _code(_src('tasks.py'))
    assert 'fallback, threshold = clock_helpers.build_window(clock_helpers.now(), MAX_GUIDEDAYS)' in tasks
    assert 'xmltv.loadStopTimes(channels, fallback_epoch=fallback)' in tasks


@pytest.mark.parametrize('name, first_arg', [('builder.py', 'now'), ('tasks.py', 'clock_helpers.now()')])
def test_builder_and_tasks_share_the_build_window(name, first_arg):
    tree = ast.parse(_src(name))
    calls = [c for c in _calls_in(tree) if ast.unparse(c.func) == 'clock_helpers.build_window']
    assert len(calls) == 1 and [ast.unparse(a) for a in calls[0].args] == [first_arg, 'MAX_GUIDEDAYS']
    horizon = [n for n in ast.walk(tree) if isinstance(n, ast.BinOp) and 'MAX_GUIDEDAYS * 86400' in ast.unparse(n)]
    assert horizon == [], 'the extend threshold is computed only by clock_helpers.build_window'
    assert not [c for c in _calls_in(tree) if ast.unparse(c.func).endswith('loadStopTimes')
                and any(k.arg == 'fallback' for k in c.keywords)]


def test_the_build_window_is_bound_once_and_used_as_given():
    """Builder.buildChannels and Tasks._filterChannelsNeedingBuild bind the anchor
    and the extend threshold once, from clock_helpers.build_window, and never
    shift them afterwards (the tests above see only the call itself);
    __needsUpdate compares with the threshold it is given."""
    builder = ast.parse(_src('builder.py'))
    build = _func(builder, 'buildChannels')
    window = [n for n in ast.walk(build) if isinstance(n, ast.Assign) and 'clock_helpers.build_window' in ast.unparse(n.value)]
    assert [ast.unparse(n) for n in window] == ['nstart, threshold = clock_helpers.build_window(now, MAX_GUIDEDAYS)']
    assert _stores(build, 'nstart') == _stores(build, 'threshold') == [window[0].lineno]
    fallback = [n for n in ast.walk(build) if isinstance(n, ast.Assign) and [ast.unparse(t) for t in n.targets] == ['fallback']]
    assert [ast.unparse(n.value) for n in fallback] == ['nstart'] and _stores(build, 'fallback') == [fallback[0].lineno]
    assert len(_stores(build, 'now')) == 1
    needs = _func(builder, '__needsUpdate')
    assert _stores(needs, 'threshold') == [] and _stores(needs, 'fallback') == []
    assert [ast.unparse(c) for c in ast.walk(needs) if isinstance(c, ast.Compare)] == ['last_stop > threshold']
    tasks = ast.parse(_src('tasks.py'))
    filt = _func(tasks, '_filterChannelsNeedingBuild')
    window = [n for n in ast.walk(filt) if isinstance(n, ast.Assign) and 'clock_helpers.build_window' in ast.unparse(n.value)]
    assert [ast.unparse(n) for n in window] == ['fallback, threshold = clock_helpers.build_window(clock_helpers.now(), MAX_GUIDEDAYS)']
    assert _stores(filt, 'fallback') == _stores(filt, 'threshold') == [window[0].lineno]


def test_tasks_queues_exactly_the_channels_the_builder_extends(monkeypatch, tz):
    """The REAL Tasks._filterChannelsNeedingBuild and the REAL __needsUpdate
    closure of Builder.buildChannels, compiled out of tasks.py and builder.py:
    around the shared build window the filter queues a channel exactly when the
    Builder then extends it (each declined build re-reads the whole guide), and
    both read the guide with the window's anchor as the fallback."""
    tz('Europe/Madrid')
    now = NOW + 0.4
    monkeypatch.setattr(ch, 'now', lambda: now)
    days = 3
    anchor, threshold = ch.build_window(now, days)
    stops = {'due': int(threshold), 'fresh': int(threshold) + 1, 'aired': anchor - 7200, 'new': None}
    fallbacks = []

    class Guide(object):
        def loadStopTimes(self, channels, programmes=None, fallback_epoch=None):
            fallbacks.append(fallback_epoch)
            for citem in channels:
                stop = stops[citem['id']]
                yield citem['id'], fallback_epoch if stop is None else stop

    class Playlist(object):
        M3UDATA = {'stations': [{'id': cid} for cid in stops]}

    filter_needed = _compiled('tasks.py', ['_filterChannelsNeedingBuild'], {
        'clock_helpers': ch, 'MAX_GUIDEDAYS': days, 'XMLTVS': Guide, 'M3U': Playlist, 'xbmc': xbmc,
        'SETTINGS': types.SimpleNamespace(getSettingBool=lambda key: False), '_renderStateDrift': lambda citem, sitem: False})[0]
    queued = [c['id'] for c in filter_needed(types.SimpleNamespace(log=lambda *a, **k: None), [{'id': cid} for cid in stops])]
    assert queued == ['due', 'aired', 'new']
    needs_update = _compiled('builder.py', ['__needsUpdate'], {'self': types.SimpleNamespace(xmltv=Guide(), log=lambda *a, **k: None)})[0]
    assert [cid for cid in stops if needs_update({'id': cid}, threshold, anchor)[0]] == queued
    assert fallbacks == [anchor] * (1 + len(stops))


# ================================================================ plugin.py: _setResume, playPlaylist, playLive, _getPVRItems

WEIRD = {'start': 1793888180, 'stop': 1793893725}  # Weird Tales, 2026-11-05 15:16:20-16:48:45 CET (5545 s)
CLICK = 1793891463.4                               # 16:11:03 CET, the logged 'Play programme'


class _ListItem(object):
    def __init__(self, path='/m/a.mkv'):
        self.props = {}
        self.path  = path
    def setProperty(self, key, value): self.props[key] = value
    def getPath(self): return self.path


def _patch_resume(monkeypatch, tolerance=60):
    points = []
    monkeypatch.setattr(plugin, 'SETTINGS', types.SimpleNamespace(getSettingInt=lambda key: {'Seek_Tolerance': tolerance}[key]))
    monkeypatch.setattr(plugin, 'ListItemInfoTag', lambda li, kind: types.SimpleNamespace(set_resume_point=lambda d: points.append(dict(d))))
    return points


@pytest.mark.parametrize('extra, total', [
    ({}, 0),                                       # the 'Play programme' route has no duration: was KeyError
    ({'duration': 5545}, 5545),
    ({'duration': '5545'}, 5545),                  # a channel tune's URL text, in seconds: was the text repeated 60 times
    ({'duration': '5545.0'}, 5545),
    ({'duration': 'x'}, 0), ({'duration': 'inf'}, 0), ({'duration': None}, 0), ({'duration': ''}, 0),
])
def test_set_resume_uses_seconds_and_never_raises(monkeypatch, extra, total):
    """The REAL Plugin._setResume: the checker's blocker. Before imports.63 a join
    on the 'Play programme' route raised KeyError('duration') and nothing played."""
    points = _patch_resume(monkeypatch)
    sysinfo = dict({'mode': 'broadcast', 'chid': X, 'seek': 3283, 'progresspercentage': 59}, **extra)
    li = _ListItem()
    out = plugin.Plugin._setResume(types.SimpleNamespace(sysInfo=sysinfo, log=lambda *a, **k: None), li)
    assert out is li and li.props == {'startoffset': '3283'}
    assert points == [{'ResumeTime': 3283, 'TotalTime': total}]


@pytest.mark.parametrize('seek, progress', [(60, 1), (5, 0), (3283, 100)])
def test_set_resume_leaves_short_or_finished_seeks_alone(monkeypatch, seek, progress):
    points = _patch_resume(monkeypatch)
    li = _ListItem()
    plugin.Plugin._setResume(types.SimpleNamespace(sysInfo={'seek': seek, 'progresspercentage': progress, 'duration': 5545},
                                                   log=lambda *a, **k: None), li)
    assert li.props == {} and points == []


def _play_fake(monkeypatch, sysinfo):
    points = _patch_resume(monkeypatch)
    monkeypatch.setattr(plugin, 'LISTITEMS', types.SimpleNamespace(buildItemListItem=lambda *a, **k: _ListItem()))
    monkeypatch.setattr(plugin, 'DIALOG', types.SimpleNamespace(notificationDialog=lambda *a, **k: None))
    monkeypatch.setattr(plugin.FileAccess, 'exists', staticmethod(lambda path: True))
    calls = {'played': [], 'resolved': [], 'pvr': 0, 'log': []}
    fake = types.SimpleNamespace(sysInfo=sysinfo)
    fake.log = lambda msg, level=xbmc.LOGDEBUG: calls['log'].append(msg)
    fake._setResume = lambda li: plugin.Plugin._setResume(fake, li)       # the REAL method
    fake._play = lambda path, li: calls['played'].append((path, li))
    fake._resolveURL = lambda found, li: calls['resolved'].append(li)

    def lookup():
        calls['pvr'] += 1
        return []
    fake._getPVRItems = lookup
    return fake, calls, points


def test_play_programme_with_times_joins_on_the_real_clock(monkeypatch):
    fitem = dict(WEIRD, file='/m/weird.mkv', label='Weird Tales')
    fake, calls, points = _play_fake(monkeypatch, {'mode': 'broadcast', 'chid': X, 'name': 'Weird Tales', 'isVOD': False,
                                                   'seek': -1, 'progresspercentage': -1, 'fitem': fitem})
    monkeypatch.setattr(ch, 'now', lambda: CLICK)
    plugin.Plugin.playPlaylist.__wrapped__(fake)
    assert fake.sysInfo['seek'] == 3283 and fake.sysInfo['progresspercentage'] == int(3283 * 100 / 5545)
    assert fake.sysInfo['duration'] == 5545
    assert [path for path, li in calls['played']] == ['/m/weird.mkv'] and calls['pvr'] == 0   # the FAST PATH
    assert calls['played'][0][1].props['startoffset'] == '3283'
    assert points == [{'ResumeTime': 3283, 'TotalTime': 5545}]                              # no KeyError
    assert any('joining current EPG event at seek=3283/5545s' in m for m in calls['log'])


def test_play_programme_without_times_plays_from_the_start(monkeypatch):
    """Routes that pass no times (JSON-RPC, in-player guide, widgets): no lookup,
    no join (decision 3)."""
    fake, calls, points = _play_fake(monkeypatch, {'mode': 'broadcast', 'chid': X, 'name': 'n', 'isVOD': False,
                                                   'seek': -1, 'progresspercentage': -1,
                                                   'fitem': {'file': '/m/a.mkv', 'label': 'a'}})
    monkeypatch.setattr(ch, 'now', lambda: CLICK)
    plugin.Plugin.playPlaylist.__wrapped__(fake)
    assert fake.sysInfo['seek'] == -1 and 'duration' not in fake.sysInfo
    assert len(calls['played']) == 1 and calls['pvr'] == 0 and points == []


@pytest.mark.parametrize('now, joins', [(1793891463, True), (WEIRD['stop'], False), (WEIRD['start'] - 1, False),
                                        (WEIRD['start'], True)])
def test_channel_tune_joins_on_the_half_open_window(monkeypatch, now, joins):
    start, stop = WEIRD['start'], WEIRD['stop']
    seek0 = abs(start - now)                                           # what Plugin.__init__ computes for a live URL
    fake, calls, points = _play_fake(monkeypatch, {
        'mode': 'live', 'chid': X, 'name': 'n', 'isVOD': False, 'isSTRM': False, 'start': str(start), 'stop': str(stop),
        'now': str(now), 'duration': '5545', 'seek': seek0, 'progresspercentage': round(seek0 / 5545 * 100, 2),
        'fitem': {'file': '/m/a.mkv', 'label': 'a'}})
    plugin.Plugin.playLive.__wrapped__(fake)
    assert len(calls['resolved']) == 1                                 # the URL is resolved once either way
    joined = [m for m in calls['log'] if 'joining now-cell' in m]
    assert bool(joined) is joins
    if joins:
        assert fake.sysInfo['seek'] == now - start and fake.sysInfo['progresspercentage'] == int((now - start) * 100 / 5545)
    if joins and now - start > 60:
        assert points == [{'ResumeTime': now - start, 'TotalTime': 5545}]
    else:
        assert points == []
    assert fake.sysInfo['duration'] == '5545'                          # the URL's own length is left as it came


def test_channel_tune_without_url_times_reads_the_real_clock(monkeypatch):
    fake, calls, points = _play_fake(monkeypatch, {
        'mode': 'live', 'chid': X, 'name': 'n', 'isVOD': False, 'isSTRM': False, 'start': '0', 'stop': '0', 'now': '0',
        'duration': '0', 'seek': -1, 'progresspercentage': -1, 'fitem': dict(WEIRD, file='/m/a.mkv', label='a')})
    monkeypatch.setattr(ch, 'now', lambda: CLICK)
    plugin.Plugin.playLive.__wrapped__(fake)
    assert fake.sysInfo['seek'] == 3283 and len(calls['resolved']) == 1
    # review round (imports.63): the zeroed URL {duration} is replaced by the show length,
    # so the resume point Kodi gets has a real total (it was TotalTime 0)
    assert fake.sysInfo['duration'] == 5545
    assert points == [{'ResumeTime': 3283, 'TotalTime': 5545}]


def test_channel_tune_without_a_programme_does_not_join(monkeypatch):
    """No decoded programme (empty fitem): there is nothing to join, so the
    URL's own seek and progress stay as Plugin.__init__ computed them and the
    tune is resolved as before (the pre-imports.63 condition, kept)."""
    start, stop, now = WEIRD['start'], WEIRD['stop'], 1793891463
    seek0 = abs(start - now)
    fake, calls, points = _play_fake(monkeypatch, {
        'mode': 'live', 'chid': X, 'name': 'n', 'isVOD': False, 'isSTRM': False, 'start': str(start), 'stop': str(stop),
        'now': str(now), 'duration': '5545', 'seek': seek0, 'progresspercentage': round(seek0 / 5545 * 100, 2), 'fitem': {}})
    plugin.Plugin.playLive.__wrapped__(fake)
    assert not [m for m in calls['log'] if 'joining now-cell' in m]
    assert fake.sysInfo['seek'] == seek0 and fake.sysInfo['progresspercentage'] == round(seek0 / 5545 * 100, 2)
    assert len(calls['resolved']) == 1 and points == [{'ResumeTime': seek0, 'TotalTime': 5545}]


def test_find_current_by_time_has_no_undefined_name(monkeypatch):
    """_getPVRItems' by-time match (no file to match) raised NameError ('ntime')."""
    monkeypatch.setattr(plugin, 'DIALOG', types.SimpleNamespace(notificationDialog=lambda *a, **k: None))
    plot = Globals._encodePlot('Plot', {'citem': {'id': X}, 'file': '/m/a.mkv'})
    past    = {'starttime': '2026-11-05 14:00:00', 'endtime': '2026-11-05 15:00:00', 'plot': plot}
    on_air  = {'starttime': '2026-11-05 15:00:00', 'endtime': '2026-11-05 16:00:00', 'plot': plot}
    nxt     = {'starttime': '2026-11-05 16:00:00', 'endtime': '2026-11-05 17:00:00', 'plot': plot}
    pvritem = {'broadcastpast': [past], 'broadcastnow': on_air, 'broadcastnext': [nxt]}
    fake = types.SimpleNamespace(sysInfo={'chid': X, 'name': 'n', 'mode': 'broadcast', 'fitem': {}},
                                 log=lambda *a, **k: None,
                                 jsonRPC=types.SimpleNamespace(matchChannel=lambda *a, **k: pvritem))
    monkeypatch.setattr(ch, 'now', lambda: ep(2026, 11, 5, 15, 30))
    assert plugin.Plugin._getPVRItems(fake) == [on_air, nxt]


# ================================================================ default.py: the plot-times guard

def _guard():
    """The guard's If node, compiled out of default.py and run (house pattern of
    the imports.60/.62 glue tests)."""
    tree = ast.parse(_src('default.py'))
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.If) and 'clock_helpers.plot_times_match' in ast.unparse(n.test))
    code = compile(ast.Module(body=[node], type_ignores=[]), 'default.py', 'exec')

    def run(fitem, vid):
        logged = []
        exec(code, {'fitem': fitem, 'vid': vid, 'clock_helpers': ch, 'log': lambda *a: logged.append(a), 'xbmc': xbmc})
        return fitem, logged
    return run


@pytest.mark.parametrize('fitem, vid, kept', [
    ({'file': '/m/a.mkv', 'start': 1, 'stop': 2}, '/m/a.mkv', True),           # the clicked programme: times kept
    ({'file': '/m/b.mkv', 'start': 1, 'stop': 2}, '/m/a.mkv', False),          # another programme's plot: dropped
    ({'file': '/m/b.mkv', 'originalfile': '/m/a.mkv', 'start': 1, 'stop': 2}, '/m/a.mkv', True),
    ({'file': '/m/b.mkv', 'start': 1, 'stop': 2}, '', True),                   # nothing to compare: as before
])
def test_default_drops_times_of_another_programme(fitem, vid, kept):
    out, logged = _guard()(dict(fitem), vid)
    assert ('start' in out and 'stop' in out) is kept
    assert bool(logged) is (not kept)
    if logged:
        assert logged == [('Default: dropped plot start/stop of another programme (/m/b.mkv)', xbmc.LOGINFO)]


def test_default_guard_logs_nothing_when_there_were_no_times():
    out, logged = _guard()({'file': '/m/b.mkv', 'label': 'b'}, '/m/a.mkv')
    assert logged == [] and out == {'file': '/m/b.mkv', 'label': 'b'}


def test_default_guard_runs_before_the_realign():
    src = _src('default.py')
    assert src.index('clock_helpers.plot_times_match(fitem, vid)') < src.index("fitem['file']         = vid")
    assert "'_tune_ts':time.time()" in src                            # the imports.60 identity token, unchanged


# ================================================================ displays and dormant features

def test_up_next_time_is_true_local_time():
    """imports.64: Up next and the end-of-show screen take the next show's time from
    onnext_helpers.next_time, which converts the epoch to true local time like
    epochTime (datetime.fromtimestamp); overlay.py no longer calls epochTime
    (was: two epochTime(self.nitem['start']) calls, right since imports.63)."""
    calls = [ast.unparse(c) for c in _calls_in(ast.parse(_src('overlay.py'))) if ast.unparse(c.func) == 'epochTime']
    helpers = [ast.unparse(c.func) for c in _calls_in(ast.parse(_src('onnext_helpers.py')))]
    assert calls == [] and 'datetime.datetime.fromtimestamp' in helpers


def test_pause_rule_last_viewed_is_written_and_shown_on_the_real_clock():
    services = ast.parse(_src('services.py'))
    resume = [n for n in ast.walk(services) if isinstance(n, ast.Dict)
              and [getattr(k, 'value', None) for k in n.keys] == ['instance', 'time']]
    assert [ast.unparse(d) for d in resume] == ["{'instance': PROPERTIES.getFriendlyName(), 'time': clock_helpers.now()}"]
    rules = [ast.unparse(c) for c in _calls_in(ast.parse(_src('rules.py'))) if ast.unparse(c.func) == 'epochTime']
    assert sorted(rules) == ['epochTime(time.time())', "epochTime(updated.get('time'))"]


def test_pad_scheduling_pads_from_the_real_clock():
    rules = ast.parse(_src('rules.py'))
    pad = next(n for n in ast.walk(rules) if isinstance(n, ast.ClassDef) and n.name == 'PadScheduling')
    nows = [ast.unparse(n.value) for n in ast.walk(pad) if isinstance(n, ast.Assign)
            and [ast.unparse(t) for t in n.targets] == ['now']]
    assert nows == ['clock_helpers.now()']


# ================================================================ guards over every add-on module

RETIRED = {'getUTCstamp', 'getTimeoffset', 'getGMTstamp', 'roundTimeUp', 'getTZShift', '_xmltvTzSuffix',
           '_stripXmltvTzSuffix', 'ntime', 'utcnow', 'utcfromtimestamp'}

CHANGED = ['clock_helpers.py', 'globals.py', 'replay_helpers.py', 'restart_helpers.py', 'xmltvs.py', 'builder.py', 'tasks.py',
           'plugin.py', 'default.py', 'rules.py', 'services.py', 'renderers.py', 'backup.py', 'm3u.py', 'server.py',
           'onnext_helpers.py', 'overlay.py', 'constants.py']   # imports.64


def _py_files():
    """Every add-on Python file under resources/lib, sub-folders included."""
    out = []
    for root, dirs, files in os.walk(LIB):
        dirs[:] = [d for d in dirs if d != '__pycache__']
        out += [os.path.relpath(os.path.join(root, f), LIB) for f in files if f.endswith('.py')]
    return sorted(out)


def test_the_guards_see_every_module():
    files = _py_files()
    assert len(files) > 60 and set(CHANGED) <= set(files)


@pytest.mark.parametrize('name', _py_files())
def test_no_retired_clock_names(name):
    tree = ast.parse(_src(name))
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | \
           {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | \
           {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    assert not used & RETIRED, used & RETIRED


@pytest.mark.parametrize('name', _py_files())
def test_epoch_time_has_one_argument_everywhere(name):
    for call in _calls_in(ast.parse(_src(name))):
        if ast.unparse(call.func).split('.')[-1] == 'epochTime':
            assert len(call.args) == 1 and not call.keywords, '%s: %s' % (name, ast.unparse(call))


@pytest.mark.parametrize('name', _py_files())
def test_clock_helpers_is_imported_explicitly(name):
    tree = ast.parse(_src(name))
    uses = any(isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == 'clock_helpers'
               for n in ast.walk(tree))
    top = [n for n in tree.body if isinstance(n, ast.Import) and any(a.name == 'clock_helpers' and a.asname is None for a in n.names)]
    if uses:
        assert top, '%s uses clock_helpers without its own top-level import' % name
    froms = [a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module == 'clock_helpers' for a in n.names]
    allowed = {'replay_helpers.py': ['xmltv_to_epoch'], 'restart_helpers.py': ['epg_utc_to_epoch']}.get(name, [])
    assert froms == allowed                                            # tests can replace clock_helpers.now


@pytest.mark.parametrize('name', CHANGED)
def test_changed_files_are_python38(name):
    src = _src(name)
    ast.parse(src, feature_version=(3, 8))
    assert py39_api_leaks(src) == []


def test_every_resume_point_total_is_in_seconds():
    """Review round (imports.63): playPaused sent 'TotalTime': runtime * 60 although
    resume.total is already seconds (getPlayerTime). No set_resume_point call in
    plugin.py may scale its TotalTime."""
    tree = ast.parse(_src('plugin.py'))
    totals = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, 'attr', '') == 'set_resume_point' and node.args \
                and isinstance(node.args[0], ast.Dict):
            for key, value in zip(node.args[0].keys, node.args[0].values):
                if isinstance(key, ast.Constant) and key.value == 'TotalTime':
                    totals.append(ast.unparse(value))
    assert len(totals) >= 2
    assert not [t for t in totals if '60' in t or '*' in t], totals
