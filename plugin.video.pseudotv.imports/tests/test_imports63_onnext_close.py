# -*- coding: utf-8 -*-
"""imports.63: the 'up next' window (overlay.OnNext) always closes.

OnNext.onInit returned early when the next programme had no time (the next-item
lookup came back empty, or no time text was found) and skipped its final
self.close(). The doModal window then stayed on screen, invisible, until Kodi
restarted: Kodi refused window switches ("refused because there are active
modal dialogs") and the Restart pop-up could not open. Seen live on 2026-11-05.

overlay.py can't be imported under the test stubs, so (as in the imports.62
glue tests) the real onInit is compiled out of overlay.py and run against
stand-ins on every exit path.
"""
import ast
import os
import types

import pytest

HERE      = os.path.dirname(os.path.abspath(__file__))
ADDON_LIB = os.path.join(os.path.dirname(HERE), 'resources', 'lib')

with open(os.path.join(ADDON_LIB, 'overlay.py'), encoding='utf-8') as _f:
    OVERLAY = _f.read()
TREE = ast.parse(OVERLAY, feature_version=(3, 8))


def _method(cls, name):
    for node in TREE.body:
        if isinstance(node, ast.ClassDef) and node.name == cls:
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == name: return item
    raise AssertionError('%s.%s not found' % (cls, name))


def _on_init(**globals_):
    node = ast.parse(ast.get_source_segment(OVERLAY, _method('OnNext', 'onInit'))).body[0]
    node.decorator_list = []
    namespace = dict(globals_)
    exec(compile(ast.Module(body=[node], type_ignores=[]), 'overlay.py', 'exec'), namespace)
    return namespace['onInit']


def test_every_exit_reaches_the_close():
    """A try whose finally closes the window covers every return of onInit, and
    there is no other close call."""
    method = _method('OnNext', 'onInit')
    tries = [n for n in method.body if isinstance(n, ast.Try) and n.finalbody]
    assert len(tries) == 1
    guarded = set(id(n) for n in ast.walk(ast.Module(body=tries[0].body + [h for h in tries[0].handlers], type_ignores=[])))
    returns = [n for n in ast.walk(method) if isinstance(n, ast.Return)]
    assert returns and all(id(r) in guarded for r in returns)
    final_calls = [ast.unparse(n.func) for n in ast.walk(ast.Module(body=tries[0].finalbody, type_ignores=[])) if isinstance(n, ast.Call)]
    assert 'self.close' in final_calls
    closes = [n for n in ast.walk(method) if isinstance(n, ast.Call) and ast.unparse(n.func) == 'self.close']
    assert len(closes) == 1


class _Control(object):
    def __init__(self, fail=False): self.fail = fail
    def __getattr__(self, name):
        if self.fail: raise RuntimeError('control failed')
        return lambda *a, **k: None


class _EmptyTime(object):
    def strftime(self, fmt): return ''


def _window(mode=2, nitem=None, next_label='', fail_controls=False, empty_time=False, close_raises=False):
    calls = {'close': 0, 'logs': [], 'sfx': 0, 'info': 0}
    player = types.SimpleNamespace(isPlayingPseudoTV=lambda: False, toggleInfo=lambda *a: calls.__setitem__('info', calls['info'] + 1))
    view = types.SimpleNamespace(
        onNextMode=mode, nitem={} if nitem is None else nitem, citem={'name': 'Cartoon Cartoons'}, fitem={'label': 'Korra'},
        onNextX=10, onNextY=20, closing=False, intTime=5, player=player,
        monitor=types.SimpleNamespace(abortRequested=lambda: False),
        service=types.SimpleNamespace(_shutdown=lambda wait: False),
        getControl=lambda cid: _Control(fail_controls),
        log=lambda msg, level=0: calls['logs'].append(msg),
        close=lambda: (calls.__setitem__('close', calls['close'] + 1), close_raises and (_ for _ in ()).throw(RuntimeError('window gone'))),
        _updateUpNext=lambda *a: None)
    fn = _on_init(
        epochTime=(lambda e: _EmptyTime()) if empty_time else (lambda e: __import__('datetime').datetime.fromtimestamp(e)),
        BUILTIN=types.SimpleNamespace(getInfoLabel=lambda key, param='ListItem', default='': next_label if key == 'NextStartTime' else ''),
        LOGO='logo.png', validString=lambda s: s, LANGUAGE=lambda i: 'L%s' % i,
        Globals=types.SimpleNamespace(_getThumb=lambda item: ''), BING_WAV='bing.wav', ONNEXT_TIMER=15, CPU_CYCLE=0.016,
        xbmc=types.SimpleNamespace(LOGERROR=4, playSFX=lambda path: calls.__setitem__('sfx', calls['sfx'] + 1)))
    return fn, view, calls


@pytest.mark.parametrize('case, kwargs', [
    ('no next programme: KeyError exit (the live case)', dict(nitem={})),
    ('no start but a title: KeyError exit',              dict(nitem={'showlabel': 'Johnny Bravo'}, next_label='10:29 PM')),
    ('empty time text: the `if not nextTime` exit',      dict(nitem={'start': 1793914174}, empty_time=True, next_label='')),
    ('shown, then closed',                               dict(nitem={'start': 1793914174, 'showlabel': 'Johnny Bravo'})),
    ('start 0 still shows (1970 time, unchanged)',       dict(nitem={'start': 0})),
    ('a control fails',                                  dict(nitem={'start': 1793914174}, fail_controls=True)),
    ('mode 3 (info)',                       dict(mode=3)),
    ('mode 4 (Up Next add-on)',             dict(mode=4)),
])
def test_the_window_always_closes(case, kwargs):
    fn, view, calls = _window(**kwargs)
    fn(view)
    assert calls['close'] == 1, case
    assert calls['logs'][-1] == 'onInit, closing'


def test_a_shown_window_still_plays_its_sound_and_closes():
    fn, view, calls = _window(nitem={'start': 1793914174, 'showlabel': 'Johnny Bravo'})
    fn(view)
    assert calls['sfx'] == 1 and calls['close'] == 1


def test_a_failing_close_is_logged_not_raised():
    fn, view, calls = _window(nitem={}, close_raises=True)
    fn(view)
    assert calls['close'] == 1 and any('close failed!' in m for m in calls['logs'])
