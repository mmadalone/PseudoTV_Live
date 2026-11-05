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
