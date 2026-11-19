# Pytest sys.path setup for imports-feature tests.
#
# Self-contained pytest target: run from project root with
#   pytest plugin.video.pseudotv.imports/tests/
# OR cd into this dir and run `pytest`.
#
# The xbmc/xbmcgui/xbmcaddon mocks live in the project-root tests/ directory
# (vendored from the vrt.nu addon, GPL-3.0); we reuse them rather than
# duplicating. sys.path order:
#   1. project_root/tests        — Kodi API stubs (highest priority)
#   2. imports-addon resources/lib  — addon code under test
import os
import sys

HERE         = os.path.dirname(os.path.abspath(__file__))
ADDON_ROOT   = os.path.dirname(HERE)
PROJECT_ROOT = os.path.dirname(ADDON_ROOT)
KODI_STUBS   = os.path.join(PROJECT_ROOT, 'tests')
ADDON_LIB    = os.path.join(ADDON_ROOT, 'resources', 'lib')

for path in (ADDON_LIB, KODI_STUBS):
    if path not in sys.path:
        sys.path.insert(0, path)

# Test data lives next to this conftest.
FIXTURES_DIR = os.path.join(HERE, 'fixtures')


# ---------------------------------------------------------------- imports.63: time-zone pinning
# Shared by every test that converts between epochs and local time.
# os.environ['TZ'] plus time.tzset() is how Kodi itself sets the process zone
# on Linux (putenv("TZ=:<zone>") + tzset()). POSIX only: skipped elsewhere.
import calendar   # noqa: E402
import time       # noqa: E402

import pytest     # noqa: E402

# name -> (zone, the instant as UTC fields)
CLOCK_ZONES = {
    'utc':                  ('UTC',                 (2026, 11, 5, 7, 28, 47)),   # the 11-05 TMNT rebuild instant
    'madrid-winter':        ('Europe/Madrid',       (2026, 11, 5, 7, 28, 47)),   # 08:28:47 CET
    'madrid-summer':        ('Europe/Madrid',       (2026, 7, 1, 10, 30, 0)),    # 12:30 CEST
    'madrid-autumn-first':  ('Europe/Madrid',       (2026, 10, 25, 0, 30, 0)),   # 02:30 CEST, the FIRST 02:30
    'madrid-autumn-second': ('Europe/Madrid',       (2026, 10, 25, 1, 30, 0)),   # 02:30 CET, the SECOND 02:30
    'madrid-spring':        ('Europe/Madrid',       (2027, 3, 28, 1, 30, 0)),    # 03:30 CEST, just after 02:00 -> 03:00
    'new-york':             ('America/New_York',    (2026, 11, 1, 6, 30, 0)),    # 01:30 EST, the second 01:30
    'kolkata':              ('Asia/Kolkata',        (2026, 11, 5, 7, 28, 47)),   # 12:58:47 IST (+05:30)
    'lord-howe':            ('Australia/Lord_Howe', (2026, 7, 1, 10, 50, 0)),    # 21:20 (+10:30; +11:00 in its summer)
}


@pytest.fixture
def tz():
    """tz('Europe/Madrid') pins the process time zone for the rest of the test;
    the previous zone (or none) is restored afterwards."""
    if not hasattr(time, 'tzset'):
        pytest.skip('time.tzset is POSIX-only')
    old = os.environ.get('TZ')

    def pin(name):
        os.environ['TZ'] = name
        time.tzset()
    yield pin
    if old is None:
        os.environ.pop('TZ', None)
    else:
        os.environ['TZ'] = old
    time.tzset()


@pytest.fixture(params=sorted(CLOCK_ZONES))
def clock_zone(request, tz):
    """Each CLOCK_ZONES case in turn: the zone is pinned, the instant (epoch) returned."""
    zone, fields = CLOCK_ZONES[request.param]
    tz(zone)
    return calendar.timegm(fields)


# ---------------------------------------------------------------- imports.70: waits Kodi would never end
# TV test 2026-11-19: the replay-end move called Monitor.waitForAbort(0), which Kodi turns into a
# wait without end, and 5,743 tests passed because their stand-ins for Kodi returned at once. The
# stand-ins of kodi_faithful.py refuse such a wait (ForeverWait) and record it; this check fails
# the test for whatever was recorded, so a refusal that an `except` swallowed, or that happened
# on a worker thread, can't pass either. A test that provokes one on purpose takes it off the
# record (kodi_faithful.provoked()).
import kodi_faithful   # noqa: E402


@pytest.fixture(autouse=True)
def no_wait_kodi_would_never_end():
    del kodi_faithful.VIOLATIONS[:]
    yield
    kodi_faithful.check()


# ---------------------------------------------------------------- imports.70: a memory cap for the test run
# 2026-11-19 23:25 a deliberately broken stand-in (a service whose waits let no time pass) made a
# test loop for ever while its rig recorded every call: the run grew by about 45 MB a second, filled
# RAM and swap and froze this machine, which also runs Kodi. kodi_faithful.Standstill now ends such
# a loop within SPIN_CAP calls; this cap is the net under it: a run that still runs away ends with
# MemoryError instead. RLIMIT_AS (the address space, what `ulimit -v` sets) is only ever lowered
# here: a lower limit already in force stays, and the hard limit is left alone (a soft limit
# above the cap means a hard one above it too). Linux only, where the resource module has it.
TEST_MEMORY_CAP = 3 * 1024 ** 3   # bytes (3 GB); a whole-suite run passes under it


def pytest_configure(config):
    if not sys.platform.startswith('linux'): return
    try: import resource
    except ImportError: return
    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    if soft == resource.RLIM_INFINITY or soft > TEST_MEMORY_CAP:
        resource.setrlimit(resource.RLIMIT_AS, (TEST_MEMORY_CAP, hard))
