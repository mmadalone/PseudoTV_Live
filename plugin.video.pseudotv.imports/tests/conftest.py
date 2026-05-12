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
