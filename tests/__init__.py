# Pytest sys.path setup — mirrors add-ons/plugin.video.vrt.nu (GPL-3.0).
# Runs first when pytest discovers `tests/`. Prepends `tests/` (mocks resolve
# first) and `plugin.video.pseudotv.live/resources/lib/` (addon code) to
# sys.path so `import xbmc` resolves to our mock and `from globals import *`
# resolves to the addon's globals.py.
import os
import sys

PROJECT_PATH = os.getcwd()
TESTS_PATH   = os.path.dirname(os.path.abspath(__file__))
ADDON_LIB    = os.path.join(PROJECT_PATH, 'plugin.video.pseudotv.live', 'resources', 'lib')

sys.path.insert(0, TESTS_PATH)
sys.path.insert(0, ADDON_LIB)
