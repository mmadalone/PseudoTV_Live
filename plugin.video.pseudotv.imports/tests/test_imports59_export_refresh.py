"""imports.59 (S13) — daily Movistar+ guide-export refresh.

Movistar+ re-exports its playlist and guide (the replay links) only at Kodi
start, and only when its files are over 6 h old; this box runs for days
without a restart. Tasks._maybeRefreshAddonExports, evaluated from the imports
thread's 3-second kick-poll loop at most every 5 minutes, fires the add-on's
own export action about once a day while nobody is watching:
  cheap checks first (nothing loaded in the player, screensaver on, no input
  for 30 min, service up 15 min, not every eligible import fired within 20 h),
  only then channels.json; per import: enabled, known exporter, link-based
  catch-up on at least one live channel, add-on installed AND enabled
  (getInfoBool, never Settings.hasAddon), its export setting on, its guide
  file at least 20 h old or missing. replay_helpers.export_due makes the time
  decision. The file's age comes from Imports._localFileState, the check the
  import sync's local read and new-links trigger use (a plain path is used as
  given; FileAccess.translatePath would cut it at an '@').

tasks.py can't be imported under the stubs, so the SHIPPED method is compiled
from tasks.py and run against fakes (test_imports54 style).
"""
import ast
import os
import re
import textwrap
import types

import pytest
import xbmc

import imports as imports_mod
import replay_helpers
from replay_helpers import (export_due, EXPORT_MIN_IDLE_S, EXPORT_MIN_UPTIME_S, EXPORT_MIN_FILE_AGE_S,
                            EXPORT_MIN_REFIRE_S, EXPORT_CHECK_INTERVAL_S)


HERE = os.path.dirname(os.path.abspath(__file__))
LIB  = os.path.join(os.path.dirname(HERE), 'resources', 'lib')
H    = 3600
ADDON = 'plugin.video.movistarplus'
ACTION = 'RunPlugin(plugin://plugin.video.movistarplus/?action=export_epg_now)'


def _read(name):
    with open(os.path.join(LIB, name)) as f:
        return f.read()


def _method(src, name):
    m = re.search(r"(    def %s\(.*?)(?=\n    def |\Z)" % re.escape(name), src, re.DOTALL)
    assert m, '%s not found' % name
    return m.group(1)


# ================================================================ export_due (pure)


GOOD = dict(file_age_s=21 * H, uptime_s=16 * 60, has_media=False, screensaver_on=True,
            idle_s=31 * 60, since_last_fire_s=None)


def test_constants():
    assert (EXPORT_MIN_IDLE_S, EXPORT_MIN_UPTIME_S, EXPORT_MIN_FILE_AGE_S, EXPORT_MIN_REFIRE_S,
            EXPORT_CHECK_INTERVAL_S) == (1800, 900, 20 * H, 20 * H, 300)


def test_export_due_when_all_conditions_hold():
    assert export_due(**GOOD) is True
    assert export_due(**dict(GOOD, since_last_fire_s=21 * H)) is True
    assert export_due(**dict(GOOD, file_age_s=20 * H, uptime_s=900, idle_s=1800, since_last_fire_s=20 * H)) is True


@pytest.mark.parametrize('over', [
    {'has_media': True},                 # a paused player still has media
    {'screensaver_on': False},
    {'idle_s': 1799}, {'idle_s': None},
    {'uptime_s': 899},
    {'file_age_s': 20 * H - 1}, {'file_age_s': 0},
    {'since_last_fire_s': 20 * H - 1}, {'since_last_fire_s': 0},
    {'idle_s': True},                    # a bool is not a duration
])
def test_export_due_blocked(over):
    assert export_due(**dict(GOOD, **over)) is False


def test_export_due_for_a_missing_file():
    """A missing (or un-stat-able) guide file: file age None passes the
    file-age check, so the export is requested."""
    assert export_due(**dict(GOOD, file_age_s=None)) is True
    assert export_due(**dict(GOOD, file_age_s=None, since_last_fire_s=21 * H)) is True


@pytest.mark.parametrize('over', [
    {'has_media': True}, {'screensaver_on': False}, {'idle_s': 1799}, {'uptime_s': 899},
    {'since_last_fire_s': 20 * H - 1},   # at most once per 20 h
])
def test_export_due_missing_file_still_obeys_every_other_check(over):
    assert export_due(**dict(GOOD, file_age_s=None, **over)) is False


# ================================================================ the evaluator (shipped code, fakes)


class _Clock(object):
    def __init__(self, mono, wall):
        self.mono, self.wall = mono, wall

    def monotonic(self):
        return self.mono

    def time(self):
        return self.wall


class _Builtin(object):
    def __init__(self):
        self.bools = {'Player.HasMedia': False, 'System.ScreenSaverActive': True,
                      'System.HasAddon(%s)' % ADDON: True, 'System.AddonIsEnabled(%s)' % ADDON: True}
        self.idle = 2 * 1800
        self.asked = []
        self.fired = []

    def getInfoBool(self, key, param='Library'):
        name = '%s.%s' % (param, key)
        self.asked.append(name)
        return self.bools.get(name, False)

    def getIdle(self):
        self.asked.append('idle')
        return self.idle

    def executebuiltin(self, key, wait=False, delay=False, condition=None):
        self.fired.append(key)
        return True


class _Env(object):
    """Fakes for one Tasks instance + its module globals. The import's
    playlist is a plain path into tmp_path (folder `kodi_dir`)."""
    def __init__(self, tmp_path, kodi_dir='kodi'):
        self.clock = _Clock(mono=10 * H, wall=1800000000)
        self.builtin = _Builtin()
        self.settings = {'export_epg': True}
        self.setting_error = None
        folder = tmp_path / kodi_dir / 'addon_data' / ADDON
        folder.mkdir(parents=True)
        self.playlist = str(folder / 'channels.m3u8')
        self.imports = [{'id': 'mp', 'enabled': True, 'm3u_url': '', 'm3u_path': self.playlist}]
        self.channels = [{'id': 'MPLUS@mp', 'import_source': 'mp', 'type': 'import', 'enabled': True,
                          'number': 17, 'catchup': 'default', 'catchup-source': '{catchup-id}',
                          'catchup-days': '7'},
                         {'id': 'c' * 32 + '@PseudoTV_Live', 'number': 1}]
        self.reads = 0
        self.guide = folder / 'epg.xml'
        self.guide.write_bytes(b'<tv></tv>')
        self.set_file_age(21 * H)
        self.lines = []
        env = self

        class _Channels(object):
            def __init__(self, *a, **k):
                env.reads += 1
            def getImports(self): return list(env.imports)
            def getChannels(self): return list(env.channels)

        class _Addon(object):
            def __init__(self, addon_id):
                assert addon_id == ADDON
            def getSettingBool(self, key):
                if env.setting_error:
                    raise env.setting_error
                return env.settings[key]

        # The real FileAccess and os: the method reads the file's real age.
        namespace = {'time': self.clock, 'os': os, 'BUILTIN': self.builtin, 'Channels': _Channels,
                     'replay_helpers': replay_helpers, 'xbmcaddon': types.SimpleNamespace(Addon=_Addon),
                     'FileAccess': imports_mod.FileAccess, 'xbmc': xbmc}
        src = textwrap.dedent(_method(_read('tasks.py'), '_maybeRefreshAddonExports'))
        code = compile(src, 'tasks.py:_maybeRefreshAddonExports', 'exec')
        fn_code = next(c for c in code.co_consts
                       if isinstance(c, types.CodeType) and c.co_name == '_maybeRefreshAddonExports')
        self.fn = types.FunctionType(fn_code, namespace)

        class _Tasks(object):
            _imports_started_at = 0.0
            def log(self, msg, level=xbmc.LOGDEBUG): env.lines.append((msg, level))
        self.tasks = _Tasks()

    def set_file_age(self, age_s):
        mtime = self.clock.wall - age_s
        os.utime(str(self.guide), (mtime, mtime))

    def run(self):
        self.fn(self.tasks)

    def advance(self, seconds):
        self.clock.mono += seconds
        self.clock.wall += seconds


@pytest.fixture
def env(tmp_path):
    return _Env(tmp_path)


def test_fires_when_everything_allows_it(env):
    env.run()
    assert env.builtin.fired == [ACTION]
    assert env.reads == 1
    info = [m for m, lvl in env.lines if lvl == xbmc.LOGINFO]
    assert len(info) == 1 and 'asked Movistar+ for a fresh guide export' in info[0] and '[mp]' in info[0]
    assert info[0].endswith('(file 21.0 h old)')


def test_plain_path_with_an_at_sign_is_found(tmp_path):
    """The age is read like the import sync reads the file: a plain path is
    used as given (FileAccess.translatePath would cut it at the '@' and the
    file would never be found)."""
    env = _Env(tmp_path, kodi_dir='kodi@home')
    env.run()
    assert env.builtin.fired == [ACTION]
    assert [m for m, lvl in env.lines if lvl == xbmc.LOGINFO][0].endswith('(file 21.0 h old)')


def test_never_fires_without_a_known_exporter(env):
    env.imports[0]['m3u_url'] = 'https://example.invalid/list.m3u'
    env.run()
    assert env.builtin.fired == []


def test_never_fires_without_link_based_catchup(env):
    """Rollback stage 1 (old catch-up row restored): no refresh either."""
    env.channels[0]['catchup-source'] = ''
    env.channels[0]['catchup-days'] = '5'
    env.run()
    assert env.builtin.fired == []
    assert 'System.HasAddon(%s)' % ADDON not in env.builtin.asked


@pytest.mark.parametrize('missing', ['System.HasAddon(%s)' % ADDON, 'System.AddonIsEnabled(%s)' % ADDON])
def test_never_fires_with_the_addon_missing_or_disabled(env, missing):
    env.builtin.bools[missing] = False
    env.run()
    assert env.builtin.fired == []


def test_never_fires_with_export_setting_off(env):
    env.settings['export_epg'] = False
    env.run()
    assert env.builtin.fired == []


def test_unreadable_export_setting_is_logged_and_skipped(env):
    env.setting_error = RuntimeError('unknown setting')
    env.run()
    assert env.builtin.fired == []
    assert [m for m, lvl in env.lines if lvl == xbmc.LOGWARNING and 'could not read' in m]


def test_never_fires_with_a_young_guide_file(env):
    env.set_file_age(20 * H - 60)
    env.run()
    assert env.builtin.fired == []
    env.advance(EXPORT_CHECK_INTERVAL_S)
    env.set_file_age(60)
    env.run()
    assert env.builtin.fired == []


def test_missing_guide_file_fires_at_most_once_per_20_hours(env):
    """E.g. Movistar+ wrote its playlist but its first guide export failed,
    or its data was cleared: the export is requested, then not again for
    20 h while the file stays missing."""
    os.unlink(str(env.guide))
    env.run()
    assert env.builtin.fired == [ACTION]
    info = [m for m, lvl in env.lines if lvl == xbmc.LOGINFO]
    assert len(info) == 1 and info[0].endswith('(file missing)')
    for _ in range(4):
        env.advance(5 * H - 60)
        env.run()
    assert env.builtin.fired == [ACTION]
    env.advance(5 * 60)
    env.run()
    assert env.builtin.fired == [ACTION, ACTION]


def test_missing_guide_file_still_waits_for_the_screensaver(env):
    os.unlink(str(env.guide))
    env.builtin.bools['System.ScreenSaverActive'] = False
    env.run()
    assert env.builtin.fired == []


def test_never_fires_for_a_disabled_import(env):
    env.imports[0]['enabled'] = False
    env.run()
    assert env.builtin.fired == []


def test_never_twice_within_20_hours(env):
    env.run()
    assert env.builtin.fired == [ACTION]
    reads = env.reads
    for _ in range(4):                      # 4 x 5 h = 20 h minus a bit: no second fire, no channels.json read
        env.advance(5 * H - 60)
        env.set_file_age(21 * H)
        env.run()
    assert env.builtin.fired == [ACTION]
    assert env.reads == reads, 'every eligible import fired within 20 h: channels.json not read'
    env.advance(5 * 60)
    env.set_file_age(21 * H)
    env.run()
    assert env.builtin.fired == [ACTION, ACTION]


def test_evaluated_at_most_every_5_minutes(env):
    env.builtin.bools['System.ScreenSaverActive'] = False
    env.run()
    asked = len(env.builtin.asked)
    env.advance(EXPORT_CHECK_INTERVAL_S - 1)
    env.run()
    assert len(env.builtin.asked) == asked, 'within 5 minutes: returns before any check'
    env.advance(1)
    env.run()
    assert len(env.builtin.asked) > asked


@pytest.mark.parametrize('block', ['media', 'screensaver', 'idle', 'uptime'])
def test_channels_json_read_only_after_the_cheap_checks(env, block):
    if block == 'media':
        env.builtin.bools['Player.HasMedia'] = True
    elif block == 'screensaver':
        env.builtin.bools['System.ScreenSaverActive'] = False
    elif block == 'idle':
        env.builtin.idle = EXPORT_MIN_IDLE_S - 1
    else:
        env.tasks._imports_started_at = env.clock.mono - EXPORT_MIN_UPTIME_S + 1
    env.run()
    assert env.reads == 0 and env.builtin.fired == []
    if block == 'media':
        assert env.builtin.asked == ['Player.HasMedia'], 'cheapest check first'


def test_one_request_per_addon_per_pass(env):
    env.imports.append({'id': 'mp2', 'enabled': True, 'm3u_url': '', 'm3u_path': env.playlist})
    env.channels.append(dict(env.channels[0], id='MPLUS@mp2', import_source='mp2'))
    env.run()
    assert env.builtin.fired == [ACTION], 'the same add-on export is requested once'
    reads = env.reads
    env.advance(EXPORT_CHECK_INTERVAL_S)
    env.run()
    assert env.reads == reads, 'both imports count as refreshed'


# ================================================================ source guards


def _method_node(name):
    tree = ast.parse(_read('tasks.py'))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(name)


def test_uses_getinfobool_never_hasaddon_or_isplaying():
    node = _method_node('_maybeRefreshAddonExports')
    called = {c.func.attr for c in ast.walk(node) if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)}
    assert 'hasAddon' not in called, 'Settings.hasAddon can install/enable add-ons'
    assert 'isPlaying' not in called, 'a paused player must block the refresh too (HasMedia)'
    texts = {c.value for c in ast.walk(node) if isinstance(c, ast.Constant) and isinstance(c.value, str)}
    for needed in ('HasMedia', 'ScreenSaverActive', 'HasAddon(%s)', 'AddonIsEnabled(%s)'):
        assert needed in texts, needed
    assert 'executebuiltin' in called and 'export_due' in called and 'getSettingBool' in called


def test_file_age_comes_from_the_import_syncs_own_check():
    """Imports._localFileState: the same file the import sync's local read
    (S4) and its new-links trigger (S17) look at."""
    node = _method_node('_maybeRefreshAddonExports')
    called = {c.func.attr for c in ast.walk(node) if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)}
    assert '_localFileState' in called
    assert 'translatePath' not in called and 'stat' not in called
    imported = [n for n in ast.walk(node) if isinstance(n, ast.ImportFrom)]
    assert [(n.module, [a.name for a in n.names]) for n in imported] == [('imports', ['Imports'])]


def test_wired_into_the_kick_poll_loop_with_its_own_guard():
    body = _method(_read('tasks.py'), '_startImportsThread')
    assert 'self._imports_started_at = time.monotonic()' in body
    m = re.search(r"if self\.monitor\.waitForAbort\(KICK_POLL_SECS\):.*?"
                  r"try:\s*\n\s*self\._maybeRefreshAddonExports\(\)\s*\n\s*except Exception as e:\s*\n"
                  r"\s*self\.log\([^\n]*_maybeRefreshAddonExports failed", body, re.DOTALL)
    assert m, 'evaluated from the 3-second kick-poll loop inside its own try/except with logging'
