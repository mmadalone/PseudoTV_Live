# -*- coding: utf-8 -*-
"""imports.70 (the repair after the TV test of 2026-11-19): logger.notice, a line Kodi's log always gets.

On 2026-11-19 the replay-end watcher did nothing on the TV and left no line in Kodi's log: the box
runs without the add-on's debug logging, and logger.log writes INFO and WARNING lines only with it
(ERROR and above always). The user's decision: the watcher's few lines are always written, with
ids, seconds and UTC times only. logger.notice(event) writes one line at xbmc.LOGINFO, the level
Kodi writes at its normal log level (utils/log.cpp, CLog::IsLogLevelLogged; LOGINFO replaced
LOGNOTICE in Kodi 19), with log()'s prefix, whatever Debug_Enable / Debug_Level say; it reads no
setting and adds no traceback. log() is unchanged.

The real functions are compiled out of logger.py over Kodi's own log levels (kodi_faithful.KODI_LOG:
the repo-root xbmc stub still has Kodi 18's numbers, under which a WARNING would look like an
ERROR) and a settings stand-in; xbmc.log is recorded. Player.notice (services.py) is the one caller,
for the watcher's lines: test_imports70_replay_end.py runs the whole chain.
"""
import ast
import traceback

import pytest

from kodi_faithful import KODI_LOG, compile_function, function_node, lib_source

ADDON_ID  = 'plugin.video.pseudotv.imports'
VERSION   = '0.8.0+imports.70'
MESSAGE   = 'Player: replay end: watching broadcast 90210 on channel 47; its guide entry ends 2026-11-19 17:00:00 UTC'


class Xbmc(object):
    """xbmc with Kodi 21's log levels; `lines` is what Kodi's log got: [(text, level)]."""

    def __init__(self):
        self.lines = []
        for name, value in KODI_LOG.items(): setattr(self, name, value)

    def log(self, msg, level=KODI_LOG['LOGDEBUG']):
        self.lines.append((msg, level))


class Settings(object):
    """REAL_SETTINGS: the two debug settings; every read is listed."""

    def __init__(self, enable='false', level='0'):
        self.values, self.reads = {'Debug_Enable': enable, 'Debug_Level': level}, []

    def getSetting(self, key):
        self.reads.append(key)
        return self.values[key]


class Broken(object):
    """A settings object that can't be read at all."""

    def __getattr__(self, name):
        raise RuntimeError('settings unreadable: %s' % name)


def logger(settings=None):
    """(log, notice, xbmc): the real functions of logger.py over Kodi's levels."""
    xbmc = Xbmc()
    g = dict(xbmc=xbmc, REAL_SETTINGS=Settings() if settings is None else settings, ADDON_ID=ADDON_ID,
             ADDON_VERSION=VERSION, traceback=traceback)
    log = compile_function('logger.py', 'log', **g)
    return log, compile_function('logger.py', 'notice', log=log, **g), xbmc          # notice sees the module's log


def test_written_with_debug_off():
    """The box: Debug_Enable off. One line at INFO, the add-on's prefix, the message as given."""
    _log, notice, xbmc = logger(Settings('false', '0'))
    notice(MESSAGE)
    assert xbmc.lines == [('%s-%s-%s' % (ADDON_ID, VERSION, MESSAGE), KODI_LOG['LOGINFO'])]
    assert KODI_LOG['LOGINFO'] == 1


@pytest.mark.parametrize('enable', ['false', 'true', ''])
@pytest.mark.parametrize('level', ['0', '1', '2', '3', '4', ''])
def test_written_whatever_the_debug_settings(enable, level):
    _log, notice, xbmc = logger(Settings(enable, level))
    notice(MESSAGE)
    assert xbmc.lines == [('%s-%s-%s' % (ADDON_ID, VERSION, MESSAGE), KODI_LOG['LOGINFO'])]


def test_written_once_with_debug_on():
    """With the add-on's debug logging on it is still one line: notice never goes through log()."""
    _log, notice, xbmc = logger(Settings('true', '0'))
    notice(MESSAGE)
    assert len(xbmc.lines) == 1


def test_no_traceback_even_inside_an_except_block():
    """log() appends traceback.format_exc() to an ERROR line; notice adds nothing, also while an
    exception is being handled: one line, no newline."""
    _log, notice, xbmc = logger()
    try:
        raise RuntimeError('a failure being handled')
    except RuntimeError:
        notice(MESSAGE)
    assert len(xbmc.lines) == 1 and 'Traceback' not in xbmc.lines[0][0] and '\n' not in xbmc.lines[0][0]
    assert 'a failure being handled' not in xbmc.lines[0][0]


def test_it_reads_no_setting():
    """No settings read at all (Kodi's settings can be mid-reload: the 2026-11-07 crash), so it
    works with settings that can't be read."""
    settings = Settings()
    _log, notice, xbmc = logger(settings)
    notice(MESSAGE)
    assert settings.reads == []
    _log, notice, xbmc = logger(Broken())
    notice(MESSAGE)
    assert xbmc.lines == [('%s-%s-%s' % (ADDON_ID, VERSION, MESSAGE), KODI_LOG['LOGINFO'])]


def test_log_keeps_its_gate():
    """log() is unchanged: with debug logging off, DEBUG, INFO and WARNING lines are not written;
    an ERROR line is, with its traceback; Debug_Level 4 keeps even that one out."""
    _log, notice, xbmc = logger(Settings('false', '0'))
    for level in (KODI_LOG['LOGDEBUG'], KODI_LOG['LOGINFO'], KODI_LOG['LOGWARNING']):
        _log('a line', level)
    _log('a debug line')
    assert xbmc.lines == []
    _log('it failed', KODI_LOG['LOGERROR'])
    assert len(xbmc.lines) == 1 and xbmc.lines[0][1] == KODI_LOG['LOGERROR']
    assert xbmc.lines[0][0].startswith('%s-%s-it failed\n' % (ADDON_ID, VERSION))
    _log, notice, xbmc = logger(Settings('false', '4'))
    _log('it failed', KODI_LOG['LOGERROR'])
    assert xbmc.lines == []


def test_with_debug_on_log_and_notice_write_the_same_text():
    """The same prefix: a 'replay end' search finds both kinds of line alike."""
    _log, notice, xbmc = logger(Settings('true', '0'))
    _log(MESSAGE, KODI_LOG['LOGINFO'])
    notice(MESSAGE)
    assert xbmc.lines[0] == xbmc.lines[1]


def test_the_always_on_function_is_small():
    """notice: one xbmc.log call at LOGINFO with log()'s prefix; it names no setting and no
    traceback, and has no branch."""
    node = function_node('logger.py', 'notice')
    assert [a.arg for a in node.args.args] == ['event'] and not node.args.defaults
    body = [s for s in node.body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
    assert [ast.unparse(s) for s in body] == ["xbmc.log('%s-%s-%s' % (ADDON_ID, ADDON_VERSION, event), xbmc.LOGINFO)"]
    names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
    names.update(n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute))
    assert not names & {'traceback', 'REAL_SETTINGS', 'getSetting', 'format_exc'}
    text = ast.get_source_segment(lib_source('logger.py')[0], node)
    assert 'madteevee (imports fork, imports.70)' in text and 'never a file path or a web address' in text


def test_log_is_unchanged_and_the_module_imports_nothing_new():
    """The repair adds notice and nothing else to logger.py (log() and the imports as before)."""
    tree = lib_source('logger.py')[1]
    assert [n.name for n in tree.body if isinstance(n, ast.FunctionDef)] == ['log', 'notice']
    imports = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert imports == ['import traceback', 'from constants import *']
    gate = [ast.unparse(n.test) for n in ast.walk(function_node('logger.py', 'log')) if isinstance(n, ast.If)]
    assert gate[0] == "REAL_SETTINGS.getSetting('Debug_Enable') == 'true' or level >= 3"


def test_logger_is_python38():
    from test_imports63_clock import py39_api_leaks
    from test_imports69_importcfg_helpers import bitor_lines
    src = lib_source('logger.py')[0]
    ast.parse(src, feature_version=(3, 8))
    assert py39_api_leaks(src) == [] and bitor_lines(src) == []


def test_services_imports_it_and_the_player_wraps_it():
    """services.py: `from logger import notice` (the star import of globals brings only log), and
    Player.notice adds the class name as Player.log does."""
    tree = lib_source('services.py')[1]
    assert [ast.unparse(n) for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == 'logger'] == \
        ['from logger import notice']
    player = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Player'][0]
    methods = {n.name: n for n in player.body if isinstance(n, ast.FunctionDef)}
    assert [ast.unparse(s) for s in methods['notice'].body] == ["return notice('%s: %s' % (self.__class__.__name__, msg))"]
    assert [ast.unparse(s) for s in methods['log'].body] == ["return log('%s: %s' % (self.__class__.__name__, msg), level)"]
