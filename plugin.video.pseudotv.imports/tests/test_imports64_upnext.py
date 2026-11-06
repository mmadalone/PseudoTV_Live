# -*- coding: utf-8 -*-
"""imports.64: "Up next" drawn on the video, never a dialog.

Up next was an add-on dialog (overlay.OnNext, onnext.xml) opened with doModal()
from the service's idle thread. Kodi treats every add-on dialog as modal: while
it was open (30 s visible, then minutes invisible) it took every key, Kodi
refused menus and the guide, the RESTART pop-up could not appear and the idle
checks waited. Now overlay.Overlay draws it as four controls on the full-screen
video window (like the channel logo) and a short wait thread hides it.

The decisions are in onnext_helpers (imported and run directly). overlay.py and
services.py can't be imported under the test stubs, so (as in the imports.62
glue tests) the real Overlay class, Background.onInit and Player._onIdle are
compiled out of the source and run against fakes. The reference formulas of
16bf2e04 (before this change) are quoted verbatim below; the tests prove the
new code gives the same numbers and the same text.
"""
import ast
import binascii
import calendar
import collections
import hashlib
import json
import math
import os
import threading
import time
import types

import pytest

import globals as globals_mod
import onnext_helpers as oh

HERE      = os.path.dirname(os.path.abspath(__file__))
ADDON     = os.path.dirname(HERE)
ADDON_LIB = os.path.join(ADDON, 'resources', 'lib')
ADDON_ID  = 'plugin.video.pseudotv.imports'
PROP      = ADDON_ID + '.ONNEXT_OVERLAY.Running'


def _src(name):
    with open(os.path.join(ADDON_LIB, name), encoding='utf-8') as f: return f.read()


OVERLAY  = _src('overlay.py')
O_TREE   = ast.parse(OVERLAY, feature_version=(3, 8))
SERVICES = _src('services.py')
S_TREE   = ast.parse(SERVICES, feature_version=(3, 8))
HELPERS  = _src('onnext_helpers.py')
H_TREE   = ast.parse(HELPERS, feature_version=(3, 8))


def _class(tree, cls):
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == cls: return node
    raise AssertionError('class %s not found' % cls)


def _method(tree, cls, name):
    for item in _class(tree, cls).body:
        if isinstance(item, ast.FunctionDef) and item.name == name: return item
    raise AssertionError('%s.%s not found' % (cls, name))


def _function(src, tree, cls, name, **globals_):
    node = ast.parse(ast.get_source_segment(src, _method(tree, cls, name))).body[0]
    node.decorator_list = []
    namespace = dict(globals_)
    exec(compile(ast.Module(body=[node], type_ignores=[]), cls + '.py', 'exec'), namespace)
    return namespace[name]


# ================================================================ references quoted from 16bf2e04

def old_roundupDIV(p, q):
    # globals.py roundupDIV (16bf2e04), verbatim
    try:
        d, r = divmod(p, q)
        if r: d += 1
        return d
    except ZeroDivisionError:
        return 1


def old_chkOnNext(playerTime, maxProgress, played, remaining, minDuration, ONNEXT_TIMER=15):
    # services.py Player._onIdle.__chkOnNext (16bf2e04), verbatim apart from self.
    totalTime = int(playerTime * (maxProgress / 100))
    threshold = abs((totalTime - (totalTime * .75)) - (ONNEXT_TIMER*3))
    intTime   = old_roundupDIV(threshold,3)
    return threshold, intTime, (played > minDuration and (remaining <= threshold and remaining >= intTime))


def old_text(LANGUAGE, validString, nowTitle, chname, nextTime, nextTitle):
    # overlay.py OnNext.onInit / Background.onInit (16bf2e04), verbatim
    onNow  = '%s on %s'%(nowTitle,chname) if chname not in validString(nowTitle) else nowTitle
    onNext = '@ %s: %s'%(nextTime,nextTitle)
    return '%s %s[CR]%s [B]%s[/B]'%(LANGUAGE(32104),onNow,LANGUAGE(32116),onNext)


def old_auto(window_w, window_h):
    # overlay.py OnNext.__init__ (16bf2e04), the 'auto' position
    return abs(int(window_w // 9)), abs(int(window_h // 16) - window_h) - 356


def old_validString(s):
    # globals.py validString (16bf2e04), verbatim
    return "".join(x for x in s if (x.isalnum() or x not in '\\/:*?"<>|'))


STRINGS = {32104: "[B]You're Watching:[/B]", 32116: '[B]Next[/B]'}
UPDATE_UPNEXT_SHA256 = '254ec2bdc47b971e66083e7da4b00bee5d9d47ada961435cc9a33f05436465e2' # OnNext._updateUpNext at 16bf2e04
NEXT_START = calendar.timegm((2026, 11, 5, 15, 19, 30))   # the 11-05 Up next case: 16:19 in Madrid


# ================================================================ helpers: window / in_window parity

def test_roundup_div_and_valid_string_are_the_globals_ones():
    for p in [0, 1, 2, 3, 4, 5, 855, 855.0, 855.5, 1e-9, -7, -7.5]:
        for q in [3, 1, 0]:
            assert oh.roundup_div(p, q) == globals_mod.roundupDIV(p, q) == old_roundupDIV(p, q)
    for s in ['', 'Korra', 'a/b:c*d?"e<f>g|h\\i', 'Cartoon Cartoons: Johnny Bravo', 'ñandú 2 — x']:
        assert oh.valid_string(s) == globals_mod.validString(s) == old_validString(s)


def test_window_equals_the_old_formula_for_every_length_and_setting():
    for maxProgress in range(50, 101):
        for total in list(range(0, 4*3600 + 1, 13)) + [1320.5, 1799.9, 3599.4, 5400.25]:
            old_threshold, old_int, _ = old_chkOnNext(total, maxProgress, 0, 0, 0)
            threshold, int_time = oh.window(total, maxProgress, 15)
            assert (threshold, int_time) == (old_threshold, old_int)
            assert type(threshold) is type(old_threshold) and type(int_time) is type(old_int)


@pytest.mark.parametrize('total, maxProgress', [(1320, 100), (1320, 97), (3600, 97), (3600, 50), (5400, 75), (60, 100), (0, 100)])
def test_in_window_equals_the_old_condition(total, maxProgress):
    for minDuration in (0, 30, 120):
        for played in range(0, int(total) + 1, 7):
            for remaining in range(-5, int(total) + 6, 11):
                threshold, intTime, old = old_chkOnNext(total, maxProgress, played, remaining, minDuration)
                assert oh.in_window(played, remaining, threshold, intTime, minDuration) is old


def test_window_numbers_of_a_60_minute_show():
    assert oh.window(3600, 100, 15) == (855.0, 285.0)   # appears 14:15 before the end, until 4:45 before


# ================================================================ helpers: time format, next time, text

@pytest.mark.parametrize('region, fmt', [
    ('%H:%M:%S', '%H:%M'), ('%I:%M:%S %p', '%I:%M %p'), ('%I%I:%M:%S %p', '%I:%M %p'), ('%H%H:%M:%S', '%H:%M'),
    ('%H.%M.%S', '%H.%M'), ('%H:%M', '%H:%M'), ('%p %I:%M:%S', '%p %I:%M'),
    ('', '%H:%M'), (None, '%H:%M'), (b'%H', '%H:%M'), ('garbage', '%H:%M'), ('%Y-%m-%d', '%H:%M'), ('%H:%M %Z', '%H:%M'),
    ('  %H:%M:%S  ', '%H:%M'),
])
def test_time_format(region, fmt):
    assert oh.time_format(region) == fmt


@pytest.mark.parametrize('start', [NEXT_START, float(NEXT_START), str(NEXT_START), ' %s ' % NEXT_START, '%s.0' % NEXT_START])
@pytest.mark.parametrize('zone, fmt, text', [
    ('Europe/Madrid', '%H:%M', '16:19'), ('UTC', '%H:%M', '15:19'), ('America/New_York', '%H:%M', '10:19'),
    ('Asia/Kolkata', '%H:%M', '20:49'), ('Europe/Madrid', '%I:%M %p', '04:19 PM'),
])
def test_next_time_from_the_guide(tz, start, zone, fmt, text):
    tz(zone)
    assert oh.next_time(start, fmt, '22:29') == text


@pytest.mark.parametrize('start', [None, '', 'soon', 0, '0', -5, True, False, float('nan'), float('inf'), 10**20, {}, []])
def test_next_time_falls_back_to_kodis_label(start):
    assert oh.next_time(start, '%H:%M', ' 22:29 ') == '22:29'
    assert oh.next_time(start, '%H:%M', '') == ''
    assert oh.next_time(start, '%H:%M', None) == ''


def test_next_time_default_format(tz):
    tz('Europe/Madrid')
    assert oh.next_time(NEXT_START, '', '') == '16:19'


@pytest.mark.parametrize('nowTitle, chname', [
    ('Korra', 'Cartoon Cartoons'), ('Cartoon Cartoons', 'Cartoon Cartoons'), ('Cartoon Cartoons: Korra', 'Cartoon Cartoons'),
    ('Korra', ''), ('', 'Channel K'), ('a/b', 'a'), ('Ñu', 'Ñu'),
])
def test_card_text_is_byte_equal_to_the_old_text(nowTitle, chname):
    for nextTime, nextTitle in [('16:19', 'Johnny Bravo'), ('04:19PM', ''), ('', 'x')]:
        assert oh.card_text(STRINGS[32104], STRINGS[32116], nowTitle, chname, nextTime, nextTitle) == \
               old_text(STRINGS.get, globals_mod.validString, nowTitle, chname, nextTime, nextTitle)


def test_card_text_example():
    assert oh.card_text(STRINGS[32104], STRINGS[32116], 'Korra', 'Cartoon Cartoons', '16:19', 'Johnny Bravo') == \
        "[B]You're Watching:[/B] Korra on Cartoon Cartoons[CR][B]Next[/B] [B]@ 16:19: Johnny Bravo[/B]"


# ================================================================ helpers: place, look, hide

@pytest.mark.parametrize('w, h', [(1920, 1080), (1280, 720), (3840, 2160)])
@pytest.mark.parametrize('setting', ['', 'Auto', 'Automatic', None, '(1,)', 'garbage'])
def test_position_auto_is_todays_rule(w, h, setting):
    assert oh.position(setting, w, h) == old_auto(w, h)


def test_position_from_the_setting():
    assert oh.position('(130,735)', 1920, 1080) == (130, 735)
    assert oh.position('(130.0, 735.9)', 1920, 1080) == (130, 735)
    assert old_auto(1920, 1080) == (213, 657)


def test_layout_is_onnext_xml():
    assert oh.layout(130, 735) == {'frame': (130, 735, 240, 135), 'art': (135, 740, 230, 125),
                                   'shadow': (131, 876, 960, 70), 'text': (130, 875, 960, 70)}


def test_condition_and_animations():
    cond = oh.condition(PROP)
    assert cond == ('[Player.Playing + !Window.IsVisible(fullscreeninfo) + Window.IsVisible(fullscreenvideo)'
                    ' + String.IsEqual(Window(Home).Property(%s),true)]' % PROP)
    assert cond.count('[') == cond.count(']') == 1
    anims = oh.animations(130, 735)
    assert anims[0] == ('Visible', 'effect=zoom start=80 end=100 center=130,735 delay=160 tween=back time=240 reversible=false')
    assert anims[1] == ('Visible', 'effect=fade end=100 time=240 reversible=false')
    assert anims[2] == ('Hidden', 'effect=fade start=100 end=0 time=240 reversible=false')


@pytest.mark.parametrize('kwargs, reason', [
    (dict(), ''),
    (dict(elapsed=30), 'time up'), (dict(elapsed=29.99), ''),
    (dict(idle_now=0), 'button'), (dict(idle_now=11), ''), (dict(idle_now=10), ''),
    (dict(epoch_now=8), 'playback changed'), (dict(playing=False), 'not playing'),
    (dict(closed=True), 'closed'), (dict(stopping=True), 'shutdown'),
    (dict(stopping=True, closed=True, epoch_now=8, playing=False, idle_now=0, elapsed=99), 'shutdown'),
    (dict(closed=True, epoch_now=8, idle_now=0), 'closed'),
    (dict(epoch_now=8, playing=False, idle_now=0), 'playback changed'),
    (dict(idle_now=0, elapsed=31), 'button'),
])
def test_hide_reason(kwargs, reason):
    facts = dict(elapsed=1.0, show_seconds=30, idle_now=10, idle_last=10, epoch_now=7, epoch_shown=7,
                 closed=False, playing=True, stopping=False)
    facts.update(kwargs)
    assert oh.hide_reason(**facts) == reason


# ================================================================ the Overlay rig (real class, fake Kodi)

class FakeControl(object):
    def __init__(self, kind, *args, **kwargs):
        self.kind, self.args, self.kwargs = kind, args, kwargs
        self.visible, self.condition, self.anims = True, None, None
        self.image = args[4] if kind == 'image' else None
        self.text, self.diffuse, self.calls = '', None, []

    def setVisible(self, state):
        self.calls.append(('setVisible', state))
        self.visible = state

    def setPosition(self, x, y): self.position = (x, y)
    def setVisibleCondition(self, cond, allowHiddenFocus=False): self.condition = cond
    def setAnimations(self, anims): self.anims = anims
    def setImage(self, image, useCache=True): self.image = image
    def setColorDiffuse(self, color): self.diffuse = color

    def setText(self, text):
        self.calls.append(('setText', text))
        self.text = text


class FakeWindow(object):
    def __init__(self, size=(1920, 1080)): self.controls, self.added, self.props, self.size = [], [], {}, size

    def getWidth(self): return self.size[0]
    def getHeight(self): return self.size[1]

    def addControl(self, c):
        self.controls.append(c)
        self.added.append(c)

    def removeControl(self, c): self.controls.remove(c)
    def setProperty(self, k, v): self.props[k] = v
    def getProperty(self, k): return self.props.get(k, '')


class Rig(object):
    def __init__(self, mode=2, nitem=None, region='%H:%M:%S', labels=None, position='', size=(1920, 1080)):
        self.clock, self.advance, self.idle, self.stop, self.playing = 0.0, True, 10, False, True
        self.on_next, self.on_wait, self.waits, self.levels, self.thread_error = None, None, [], [], None
        self.region, self.labels = region, dict(labels or {})
        self.next_item = {'start': NEXT_START, 'showlabel': 'Johnny Bravo', 'title': 'Johnny Bravo',
                          'art': {'thumb': '/art/bravo.jpg'}} if nitem is None else nitem
        self.calls, self.logs, self.threads, self.windows = collections.defaultdict(list), [], [], {12005: FakeWindow(size)}
        self.next_error = None
        self.service = types.SimpleNamespace(_shutdown=self._shutdown)
        self.jsonRPC = types.SimpleNamespace(getViewMode=lambda: {}, getNextItem=self._getNextItem, setViewMode=lambda *a: None,
                                             notifyAll=lambda *a: self.calls['notify'].append(a))
        self.player = types.SimpleNamespace(
            service=self.service, jsonRPC=self.jsonRPC, runActions=lambda *a, **k: None, OnNextMode=mode, onNextPosition=position, _play_epoch=7,
            playingItem={'citem': {'name': 'Cartoon Cartoons', 'id': 'ch1'}, 'fitem': {'label': 'Korra', 'title': 'Korra', 'id': 11},
                         'nitem': {'start': NEXT_START, 'id': 12}},
            isPlayingPseudoTV=lambda: self.playing, toggleInfo=lambda *a: self.calls['info'].append(a),
            getRemainingTime=lambda: 600.4)
        ns = self.namespace()
        cls = _class(O_TREE, 'Overlay')
        prop = [n for n in O_TREE.body if isinstance(n, ast.Assign) and [ast.unparse(t) for t in n.targets] == ['ONNEXT_RUNNING']]
        exec(compile(ast.Module(body=prop + [cls], type_ignores=[]), 'overlay.py', 'exec'), ns)
        self.ns = ns
        self.overlay = ns['Overlay'](player=self.player)

    def namespace(self):
        rig = self

        def thread(*a, **k):
            t = threading.Thread(*a, **k)
            if rig.thread_error:
                def start(): raise rig.thread_error
                t.start = start
            rig.threads.append(t)
            return t

        def log(msg, level=0):
            rig.logs.append(msg)
            rig.levels.append((msg, level))
        return dict(
            Lock=threading.Lock, Thread=thread, time=types.SimpleNamespace(monotonic=lambda: rig.clock), os=os,
            BUILTIN=types.SimpleNamespace(getIdle=lambda: rig.idle, getInfoLabel=lambda key, param='ListItem', default='': rig.labels.get(key, '')),
            xbmc=types.SimpleNamespace(LOGDEBUG=0, LOGERROR=4, getRegion=lambda key: rig.region if key == 'time' else '',
                                       playSFX=lambda path: rig.calls['sfx'].append(path)),
            xbmcgui=types.SimpleNamespace(Window=lambda wid: rig.windows.setdefault(wid, FakeWindow()),
                                          ControlImage=lambda *a, **k: FakeControl('image', *a, **k),
                                          ControlTextBox=lambda *a, **k: FakeControl('textbox', *a, **k)),
            onnext_helpers=oh, LANGUAGE=lambda i: STRINGS[i], Globals=types.SimpleNamespace(_getThumb=lambda item: item.get('art', {}).get('thumb', '')),
            FANART_COLOR='/media/fanart.jpg', MEDIA_LOC='/media', BING_WAV='/media/sfx/bing.wav', ONNEXT_TIMER=15, OSD_TIMER=5,
            floor=math.floor, binascii=binascii, FileAccess=types.SimpleNamespace(dumpJSON=lambda d: json.dumps(d, sort_keys=True)),
            timerit=lambda fn: (lambda wait, *a: fn(*a)), log=log,
            SETTINGS=types.SimpleNamespace(getSettingInt=lambda k: 2, getSettingBool=lambda k: False, getSetting=lambda k: ''),
            Resources=lambda service=None: None, WH=(1920, 1080), literal_eval=ast.literal_eval, ADDON_ID=ADDON_ID,
            PROPERTIES=types.SimpleNamespace(setRunning=lambda *a: rig.calls['running'].append(a), isRunning=lambda *a: False),
            LOGO='', RULES_ACTION_OVERLAY_OPEN=0)

    def _shutdown(self, wait):
        self.waits.append((wait, self.prop, len(self.video.controls)))
        if self.on_wait: self.on_wait()
        time.sleep(0.001)
        if self.advance: self.clock += wait
        return self.stop

    def _getNextItem(self, citem, nitem):
        self.calls['next'].append((citem, nitem))
        if self.next_error: raise self.next_error
        if self.on_next: self.on_next()
        return self.next_item

    @property
    def video(self): return self.windows.setdefault(12005, FakeWindow())

    @property
    def prop(self): return self.windows.setdefault(10000, FakeWindow()).props.get(PROP)

    def join(self, timeout=5.0):
        for t in self.threads:
            if t.ident is not None: t.join(timeout)                  # never started: nothing to join
        assert not any(t.is_alive() for t in self.threads)

    def finish(self):
        self.stop = True
        self.join()

    def by_kind(self):
        return [(c.kind, c.args) for c in self.video.controls]


@pytest.fixture
def madrid(tz):
    tz('Europe/Madrid')


TEXT = "[B]You're Watching:[/B] Korra on Cartoon Cartoons[CR][B]Next[/B] [B]@ 16:19: Johnny Bravo[/B]"


def test_real_init_sets_up_next_state():
    rig = Rig()
    o = rig.overlay
    assert (o._onNextControls, o._onNextShown, o._onNextVisible, o._onNextToken, o._onNextClosed) == ({}, False, False, 0, False)
    assert type(o._onNextLock) is type(threading.Lock())
    assert rig.ns['ONNEXT_RUNNING'] == PROP


def test_mode2_draws_four_controls_in_order_once(madrid):
    rig = Rig(mode=2)
    rig.advance = False
    rig.overlay.toggleOnNext(True)
    try:
        x, y = 213, 657
        assert rig.by_kind() == [('image', (x, y, 240, 135, ' ')), ('image', (x + 5, y + 5, 230, 125, ' ')),
                                 ('textbox', (x + 1, y + 141, 960, 70, 'font27', '0xFF696969')),
                                 ('textbox', (x, y + 140, 960, 70, 'font27', '0xFFFFFFFF'))]
        frame, art, shadow, text = rig.video.controls
        assert frame.kwargs == {'aspectRatio': 0} and art.kwargs == {'aspectRatio': 1}   # art fills the frame (scale)
        assert frame.image == '/media/onnext_frame.png' and frame.diffuse == '0xFF01416b'
        assert art.image == '/art/bravo.jpg'
        assert shadow.text == text.text == TEXT
        for c in rig.video.controls:
            assert c.condition == oh.condition(PROP) and c.anims == oh.animations(x, y) and c.visible
        assert rig.prop == 'true' and rig.calls['sfx'] == ['/media/sfx/bing.wav']
        assert len(rig.threads) == 1 and rig.threads[0].daemon and rig.threads[0].is_alive()
        rig.overlay.toggleOnNext(True)                               # the next idle tick: nothing new
        assert len(rig.video.added) == 4 and len(rig.calls['next']) == 1 and len(rig.calls['sfx']) == 1
        assert rig.calls['next'][0] == (rig.player.playingItem['citem'], rig.player.playingItem['nitem'])
    finally:
        rig.finish()


def test_it_hides_after_30_seconds(madrid):
    rig = Rig(mode=2)
    rig.overlay.toggleOnNext(True)
    rig.join()
    assert rig.prop == 'false' and 30 <= rig.clock < 30.5
    assert 'Overlay: toggleOnNext, hidden, time up' in rig.logs
    assert len(rig.video.controls) == 4                              # kept for the next show
    assert not rig.overlay._onNextVisible


@pytest.mark.parametrize('change, reason', [
    (lambda rig: setattr(rig, 'idle', 0), 'button'),
    (lambda rig: setattr(rig.player, '_play_epoch', 8), 'playback changed'),
    (lambda rig: setattr(rig, 'playing', False), 'not playing'),
    (lambda rig: setattr(rig, 'stop', True), 'shutdown'),
])
def test_it_hides_at_once_when(madrid, change, reason):
    rig = Rig(mode=2)
    rig.advance = False                                              # the 30 s never run out
    rig.overlay.toggleOnNext(True)
    assert rig.prop == 'true'
    rig.idle = 12                                                    # idle seconds going up is no key press
    time.sleep(0.01)
    assert rig.threads[0].is_alive()
    change(rig)
    rig.join()
    assert rig.prop == 'false' and rig.clock == 0
    assert 'Overlay: toggleOnNext, hidden, %s' % reason in rig.logs


def test_close_mid_show_removes_the_controls_and_ends_the_thread(madrid):
    rig = Rig(mode=2)
    rig.advance = False
    rig.overlay.toggleOnNext(True)
    assert len(rig.video.controls) == 4
    rig.overlay.close()
    rig.join()
    assert rig.video.controls == [] and rig.prop == 'false' and rig.overlay._onNextControls == {}
    rig.overlay.rearmOnNext()
    rig.overlay.toggleOnNext(True)                                   # a closed overlay never shows again
    assert rig.video.added and len(rig.video.added) == 4 and rig.prop == 'false'


def test_toggle_off_hides_now(madrid):
    rig = Rig(mode=2)
    rig.advance = False
    rig.overlay.toggleOnNext(True)
    rig.overlay.toggleOnNext(False)
    assert rig.prop == 'false'
    rig.join()
    assert rig.logs.count('Overlay: toggleOnNext, hidden, off') == 1
    assert not [m for m in rig.logs if m.startswith('Overlay: toggleOnNext, hidden,') and not m.endswith('off')]


def test_mode1_shows_text_and_shadow_only(madrid):
    rig = Rig(mode=1)
    rig.advance = False
    rig.overlay.toggleOnNext(True)
    try:
        assert [c.kind for c in rig.video.controls] == ['textbox', 'textbox']
        assert [c.text for c in rig.video.controls] == [TEXT, TEXT] and all(c.visible for c in rig.video.controls)
        assert rig.prop == 'true' and rig.calls['sfx'] == []
    finally:
        rig.finish()


def test_mode3_opens_kodis_info_only():
    rig = Rig(mode=3)
    rig.overlay.toggleOnNext(True)
    rig.overlay.toggleOnNext(True)
    assert rig.calls['info'] == [(True,)]
    assert rig.video.added == [] and rig.calls['next'] == [] and rig.prop is None and rig.threads == []


def test_mode4_sends_the_upnext_payload_only():
    rig = Rig(mode=4)
    rig.overlay.toggleOnNext(True)
    assert rig.video.added == [] and rig.prop is None and rig.threads == [] and rig.calls['info'] == []
    assert len(rig.calls['notify']) == 1
    method, data, sender = rig.calls['notify'][0]
    payload = json.loads(binascii.unhexlify(data).decode('utf-8'))
    assert (method, sender) == ('upnext_data', ADDON_ID + '.SIGNAL')
    assert payload['notification_offset'] == 600 + 5
    assert payload['current_episode']['title'] == 'Korra' and payload['current_episode']['episodeid'] == 11
    assert payload['next_episode']['title'] == 'Johnny Bravo'


def test_upnext_payload_code_is_unchanged():
    """Moved from OnNext to Overlay character for character."""
    text = ast.get_source_segment(OVERLAY, _method(O_TREE, 'Overlay', '_updateUpNext'))
    assert hashlib.sha256(text.encode('utf-8')).hexdigest() == UPDATE_UPNEXT_SHA256


def test_no_time_shows_nothing_and_asks_once():
    rig = Rig(mode=2, nitem={'showlabel': 'Johnny Bravo'})
    rig.overlay.toggleOnNext(True)
    rig.overlay.toggleOnNext(True)
    assert rig.video.added == [] and rig.prop is None and rig.threads == [] and rig.calls['sfx'] == []
    assert len(rig.calls['next']) == 1
    assert 'Overlay: toggleOnNext, no time for the next programme, not shown' in rig.logs


def test_kodis_label_is_used_when_the_guide_has_no_start():
    rig = Rig(mode=2, nitem={'showlabel': 'Johnny Bravo'}, labels={'NextStartTime': ' 22:29 '})
    rig.advance = False
    rig.overlay.toggleOnNext(True)
    try:
        assert rig.video.controls[-1].text.endswith('[B]@ 22:29: Johnny Bravo[/B]')
        assert rig.video.controls[1].image == '/media/fanart.jpg'   # no artwork: the add-on's fanart
    finally:
        rig.finish()


def test_kodis_12_hour_format_is_followed(madrid):
    rig = Rig(mode=1, region='%I:%M:%S %p')
    rig.advance = False
    rig.overlay.toggleOnNext(True)
    try:
        assert '@ 04:19 PM: Johnny Bravo' in rig.video.controls[-1].text
    finally:
        rig.finish()


def test_position_setting_is_used(madrid):
    rig = Rig(mode=2, position='(130,735)')
    rig.advance = False
    rig.overlay.toggleOnNext(True)
    try:
        assert [c.args[:4] for c in rig.video.controls] == [(130, 735, 240, 135), (135, 740, 230, 125),
                                                            (131, 876, 960, 70), (130, 875, 960, 70)]
    finally:
        rig.finish()


def test_a_second_pass_reuses_the_controls(madrid):
    rig = Rig(mode=2)
    rig.advance = False
    rig.overlay.toggleOnNext(True)
    assert len(rig.threads) == 1
    rig.idle = 0
    rig.join()
    rig.overlay.toggleOnNext(True)                                   # same pass: not again
    assert len(rig.threads) == 1
    rig.overlay.rearmOnNext()
    rig.idle = 20
    rig.next_item = {'start': NEXT_START + 1800, 'showlabel': 'Dexter', 'art': {'thumb': '/art/dexter.jpg'}}
    rig.overlay.toggleOnNext(True)
    try:
        assert len(rig.video.added) == 4 and len(rig.video.controls) == 4
        assert rig.video.controls[1].image == '/art/dexter.jpg'
        assert rig.video.controls[-1].text.endswith('[B]@ 16:49: Dexter[/B]')
        assert rig.prop == 'true' and len(rig.calls['sfx']) == 2 and len(rig.threads) == 2
        assert rig.threads[1].is_alive()
    finally:
        rig.finish()


def test_mode1_after_mode2_hides_the_artwork(madrid):
    rig = Rig(mode=2)
    rig.overlay.toggleOnNext(True)
    rig.idle = 0
    rig.join()
    rig.overlay.rearmOnNext()
    rig.player.OnNextMode, rig.idle, rig.advance = 1, 20, False
    rig.overlay.toggleOnNext(True)
    try:
        assert [c.visible for c in rig.video.controls] == [False, False, True, True]
    finally:
        rig.finish()


def test_errors_are_logged_never_raised():
    rig = Rig(mode=2)
    rig.next_error = RuntimeError('rpc down')
    rig.overlay.toggleOnNext(True)
    assert 'Overlay: toggleOnNext, failed! rpc down' in rig.logs and rig.prop is None


def test_the_next_programme_is_read_when_shown(madrid):
    """An Overlay lives as long as the channel: Up next reads the programme that
    plays now, not the one the Overlay was opened for."""
    rig = Rig(mode=1)
    rig.advance = False
    rig.player.playingItem = {'citem': {'name': 'Cartoon Cartoons', 'id': 'ch1'}, 'fitem': {'label': 'Ed, Edd n Eddy'},
                              'nitem': {'start': NEXT_START, 'id': 99}}
    rig.overlay.toggleOnNext(True)
    try:
        assert rig.video.controls[-1].text.startswith("[B]You're Watching:[/B] Ed, Edd n Eddy on Cartoon Cartoons")
        assert rig.calls['next'][0][1] == {'start': NEXT_START, 'id': 99}
    finally:
        rig.finish()


# ================================================================ review fixes (imports.64)

def test_rearm_while_up_hides_it(madrid):
    """A remote's restart (JSON-RPC NotifyAll -> seekTime(0) on a Custom show) moves
    neither Kodi's idle time nor the playback epoch: the card goes when the idle
    loop sees the time left back above the window."""
    rig = Rig(mode=2)
    rig.advance = False
    rig.overlay.toggleOnNext(True)
    assert rig.prop == 'true'
    rig.overlay.rearmOnNext()
    o = rig.overlay
    assert rig.prop == 'false' and not o._onNextVisible and not o._onNextShown
    assert 'Overlay: toggleOnNext, hidden, back before the window' in rig.logs
    rig.join()                                                       # the wait thread ends quietly
    assert [m for m in rig.logs if m.startswith('Overlay: toggleOnNext, hidden,')] == ['Overlay: toggleOnNext, hidden, back before the window']
    rig.overlay.toggleOnNext(True)                                   # the window again: shown again
    try:
        assert rig.prop == 'true' and len(rig.threads) == 2 and len(rig.video.added) == 4
    finally:
        rig.finish()


def test_rearm_when_hidden_only_rearms(madrid):
    rig = Rig(mode=2)
    rig.overlay.toggleOnNext(True)
    rig.join()                                                       # 30 s ran out
    rig.overlay.rearmOnNext()
    assert not rig.overlay._onNextShown and 'Overlay: toggleOnNext, hidden, back before the window' not in rig.logs


@pytest.mark.parametrize('mode', [1, 2])
def test_open_makes_the_controls_before_the_first_show(madrid, mode):
    """Kodi plays the 'Visible' zoom and fade only on a hidden->shown change of the
    condition that a drawn frame has seen; a new control starts out as 'shown'."""
    rig = Rig(mode=mode)
    rig.advance = False
    rig.overlay.open()
    assert [c.kind for c in rig.video.added] == ['image', 'image', 'textbox', 'textbox'] and rig.prop == 'false'
    assert rig.threads == [] and rig.calls['next'] == []
    rig.overlay.toggleOnNext(True)
    try:
        assert len(rig.video.added) == 4 and rig.prop == 'true'
        assert oh.SETTLE_WAIT not in [w for w, _p, _n in rig.waits]  # nothing new: no wait
        assert [c.visible for c in rig.video.controls] == ([False, False, True, True] if mode == 1 else [True] * 4)
    finally:
        rig.finish()


@pytest.mark.parametrize('mode', [0, 3, 4])
def test_open_makes_no_controls_for_other_modes(mode):
    rig = Rig(mode=mode)
    rig.overlay.open()
    assert rig.video.added == [] and rig.prop is None


def test_new_controls_wait_a_few_frames_before_they_show(madrid):
    rig = Rig(mode=2)
    rig.advance = False
    rig.overlay.toggleOnNext(True)
    try:
        assert rig.waits[0] == (oh.SETTLE_WAIT, 'false', 4)          # made, hidden, then the wait
        assert rig.prop == 'true'
    finally:
        rig.finish()


def test_close_during_the_wait_shows_nothing(madrid):
    rig = Rig(mode=2)
    rig.advance = False
    rig.on_wait = lambda: (rig.overlay.close(), setattr(rig, 'on_wait', None))
    rig.overlay.toggleOnNext(True)
    assert rig.video.controls == [] and rig.overlay._onNextControls == {} and rig.prop != 'true'
    assert rig.threads == [] and rig.calls['sfx'] == []
    assert not [m for m, level in rig.levels if level == 4]           # a quiet stop, not 'no controls'


def test_shutdown_during_the_wait_shows_nothing(madrid):
    rig = Rig(mode=2)
    rig.stop = True
    rig.overlay.toggleOnNext(True)
    assert rig.prop != 'true' and rig.threads == [] and not rig.overlay._onNextVisible


@pytest.mark.parametrize('mode', [1, 2])
def test_close_while_the_next_programme_is_read(madrid, mode):
    rig = Rig(mode=mode)
    rig.advance = False
    rig.on_next = rig.overlay.close
    rig.overlay.toggleOnNext(True)
    rig.join()
    assert rig.video.added == [] and rig.video.controls == [] and rig.overlay._onNextControls == {}
    assert rig.prop != 'true' and rig.threads == [] and rig.calls['sfx'] == []
    assert 'Overlay: toggleOnNext, shown, mode = %s' % mode not in rig.logs


def test_no_card_without_a_wait_thread(madrid):
    rig = Rig(mode=2)
    rig.advance = False
    rig.thread_error = RuntimeError("can't start new thread")
    rig.overlay.toggleOnNext(True)
    o = rig.overlay
    assert rig.prop != 'true' and not o._onNextVisible and o._onNextShown   # once per pass, no retry
    assert ("Overlay: toggleOnNext, wait thread failed, not shown! can't start new thread", 4) in rig.levels
    assert 'Overlay: toggleOnNext, shown, mode = 2' not in rig.logs and rig.calls['sfx'] == []
    rig.thread_error = None
    rig.overlay.rearmOnNext()
    rig.overlay.toggleOnNext(True)                                   # the next pass works
    try:
        assert rig.prop == 'true' and o._onNextVisible
    finally:
        rig.finish()


def test_wait_thread_errors_are_logged_as_errors(madrid):
    rig = Rig(mode=2)
    rig.advance = False
    rig.overlay.toggleOnNext(True)

    def boom(): raise RuntimeError('boom')
    rig.ns['BUILTIN'].getIdle = boom
    rig.join()
    assert rig.prop == 'false'
    assert ('Overlay: _waitOnNext, failed! boom', 4) in rig.levels
    assert ('Overlay: toggleOnNext, hidden, failed! boom', 0) in rig.levels   # normal hides stay at debug


def _png_alpha(path):
    import struct
    import zlib
    with open(path, 'rb') as f: data = f.read()
    assert data[:8] == b'\x89PNG\r\n\x1a\n'
    pos, idat, head = 8, b'', None
    while pos < len(data):
        n, kind = struct.unpack('>I4s', data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + n]
        if kind == b'IHDR': head = struct.unpack('>IIBBBBB', body)
        elif kind == b'IDAT': idat += body
        pos += 12 + n
    w, h, depth, color, _c, _f, interlace = head
    assert (depth, color, interlace) == (8, 6, 0)                    # 8-bit RGBA
    raw, rows, stride, prev = zlib.decompress(idat), [], w * 4, bytearray(w * 4)
    for y in range(h):
        ftype, line = raw[y * (stride + 1)], bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        for i in range(stride):
            a = line[i - 4] if i >= 4 else 0
            b = prev[i]
            c = prev[i - 4] if i >= 4 else 0
            if ftype == 1: line[i] = (line[i] + a) & 255
            elif ftype == 2: line[i] = (line[i] + b) & 255
            elif ftype == 3: line[i] = (line[i] + (a + b) // 2) & 255
            elif ftype == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        rows.append([line[x * 4 + 3] for x in range(w)])
        prev = line
    return w, h, rows


def test_the_frame_is_a_5px_border_with_a_see_through_middle():
    """onnext.xml's bordertexture had infill="false": the video showed through the
    frame's middle (an image that loads late or has see-through parts)."""
    w, h, alpha = _png_alpha(os.path.join(ADDON, 'resources', 'skins', 'default', 'media', oh.FRAME_IMAGE))
    assert (w, h) == (oh.CARD_W, oh.CARD_H)
    b = oh.CARD_BORDER
    for y in range(h):
        for x in range(w):
            edge = x < b or x >= w - b or y < b or y >= h - b
            assert alpha[y][x] == (255 if edge else 0), (x, y)
    assert (oh.FRAME_ASPECT, oh.ART_ASPECT) == (0, 1)


def test_grid():
    assert oh.grid(1920, 1080) == (1.0, 1.0)
    assert oh.grid(1280, 720) == (1280 / 1920, 720 / 1080)
    for bad in [(0, 720), (1280, -1), (None, 720), (True, 720), (float('nan'), 720), ('1280', 720)]:
        assert oh.grid(*bad) == (1.0, 1.0)


def test_layout_in_a_720_skin():
    f = 720 / 1080
    assert oh.layout(213, 657, f, f) == {'frame': (142, 438, 160, 90), 'art': (145, 441, 154, 84),
                                        'shadow': (143, 532, 640, 47), 'text': (142, 531, 640, 47)}
    assert oh.layout(130, 735, 1.0, 1.0) == oh.layout(130, 735)


def test_controls_follow_the_skin_grid(madrid):
    rig = Rig(mode=2, size=(1280, 720))
    rig.advance = False
    rig.overlay.toggleOnNext(True)
    try:
        rect = oh.layout(213, 657, 720 / 1080, 720 / 1080)
        assert [c.args[:4] for c in rig.video.controls] == [rect['frame'], rect['art'], rect['shadow'], rect['text']]
        assert all(c.anims == oh.animations(142, 438) for c in rig.video.controls)
    finally:
        rig.finish()


# ================================================================ services: once per pass, never blocks

class FakePlayer(object):
    def __init__(self, overlay, total=3600, played=0):
        self.overlay, self.total, self.played = overlay, total, played
        self.maxProgress, self.minDuration, self.OnNextMode, self.sleepTime = 100, 30, 2, 0
        self.enableOverlay = True
        self.playingItem = {'isfiller': False, 'isPlaylist': False}
        self.calls = collections.defaultdict(int)

    def getPlayedTime(self):    return self.played
    def getRemainingTime(self): return self.total - self.played
    def getPlayerTime(self):    return self.total
    def isPlayingPseudoTV(self): return True
    def _chkCallback(self):     self.calls['callback'] += 1
    def toggleOverlay(self, state): self.calls['overlay'] += 1
    def toggleBackground(self, state): self.calls['background'] += 1
    def log(self, msg, level=0): pass


def _on_idle():
    return _function(SERVICES, S_TREE, 'Player', '_onIdle', ceil=math.ceil, floor=math.floor, onnext_helpers=oh,
                     ONNEXT_TIMER=15, OSD_TIMER=5, PROPERTIES=None, clock_helpers=None)


class RecordingOverlay(object):
    def __init__(self): self.calls = []
    def rearmOnNext(self): self.calls.append('rearm')
    def toggleOnNext(self, state): self.calls.append(state)


def test_chk_on_next_calls_match_the_old_condition():
    on_idle = _on_idle()
    for total in (1320, 3600):
        for maxProgress in (100, 97):
            for played in range(0, total + 1, 5):
                overlay = RecordingOverlay()
                p = FakePlayer(overlay, total, played)
                p.maxProgress = maxProgress
                on_idle(p)
                threshold, _, show = old_chkOnNext(total, maxProgress, played, total - played, p.minDuration)
                expected = ['rearm'] if total - played > threshold else ([True] if show else [])
                assert overlay.calls == expected, (total, maxProgress, played)


def test_once_per_pass_and_again_after_a_restart():
    rig = Rig(mode=3)
    p = FakePlayer(rig.overlay, 3600)
    on_idle = _on_idle()
    p.OnNextMode = 3
    for played in (100, 2000, 2745, 2750, 2800, 3000, 3300, 3500):   # window: 855..285 left
        p.played = played
        on_idle(p)
    assert rig.calls['info'] == [(True,)]
    for played in (40, 1000, 2746, 2900):                            # restarted from the beginning
        p.played = played
        on_idle(p)
    assert rig.calls['info'] == [(True,), (True,)]
    p.overlay = None                                                 # no overlay: nothing at all
    p.played = 1000
    on_idle(p)


def test_on_idle_returns_at_once_while_up_next_shows(madrid):
    rig = Rig(mode=2)
    rig.advance = False
    p = FakePlayer(rig.overlay, 3600, 2800)
    started = time.monotonic()
    _on_idle()(p)
    try:
        assert time.monotonic() - started < 0.5
        assert rig.prop == 'true' and rig.threads[0].is_alive()
        assert p.calls == {'callback': 1, 'overlay': 1}              # the other idle checks ran
    finally:
        rig.finish()


def test_mode_off_never_shows():
    rig = Rig(mode=0)
    p = FakePlayer(rig.overlay, 3600, 2800)
    p.OnNextMode = 0
    _on_idle()(p)
    assert rig.video.added == [] and rig.calls['next'] == [] and rig.threads == []


# ================================================================ the end-of-show screen

def _background(rig_labels=None, nitem=None, region='%H:%M:%S'):
    controls = collections.defaultdict(lambda: FakeControl('xml'))
    calls = {'close': 0, 'logs': []}
    labels = dict(rig_labels or {})
    view = types.SimpleNamespace(citem={'name': 'Cartoon Cartoons', 'logo': '/logo.png'}, fitem={'label': 'Korra'},
                                 nitem={'start': NEXT_START, 'showlabel': 'Johnny Bravo'} if nitem is None else nitem,
                                 getControl=lambda cid: controls[cid], log=lambda msg, level=0: calls['logs'].append(msg),
                                 close=lambda: calls.__setitem__('close', calls['close'] + 1))
    fn = _function(OVERLAY, O_TREE, 'Background', 'onInit',
                   BUILTIN=types.SimpleNamespace(getInfoLabel=lambda key, param='ListItem', default='': labels.get(key, '')),
                   LOGO='', LOGO_COLOR='/logo_color.png', WH=(1920, 1080), onnext_helpers=oh, LANGUAGE=lambda i: STRINGS[i],
                   Globals=types.SimpleNamespace(_getThumb=lambda item: '/thumb.jpg'),
                   xbmc=types.SimpleNamespace(LOGDEBUG=0, LOGERROR=4, getRegion=lambda key: region))
    fn(view)
    return controls, calls


def test_background_uses_kodis_clock_format(madrid):
    controls, calls = _background()
    assert controls[40003].text == TEXT and calls['close'] == 0, calls['logs']
    assert controls[40004].image == '/thumb.jpg' and controls[40001].position == (213, 657)


def test_background_12_hour(madrid):
    controls, calls = _background(region='%I:%M:%S %p')
    assert '@ 04:19 PM: Johnny Bravo' in controls[40003].text


def test_background_kodi_label_fallback():
    controls, calls = _background({'NextStartTime': '22:29'}, nitem={'showlabel': 'Johnny Bravo'})
    assert controls[40003].text.endswith('[B]@ 22:29: Johnny Bravo[/B]')


def test_background_without_any_time_returns_like_before():
    controls, calls = _background(nitem={'showlabel': 'Johnny Bravo'})
    assert dict(controls) == {} and calls['close'] == 0


# ================================================================ structure guards

def test_on_next_window_is_gone():
    names = {n.name for n in O_TREE.body if isinstance(n, ast.ClassDef)}
    assert 'OnNext' not in names and {'Overlay', 'Background', 'Replay', 'Busy'} <= names
    assert not os.path.exists(os.path.join(ADDON, 'resources', 'skins', 'default', '1080i', ADDON_ID + '.onnext.xml'))
    for root, _dirs, files in os.walk(ADDON_LIB):
        for f in files:
            if f.endswith('.py'):
                with open(os.path.join(root, f), encoding='utf-8') as fh: tree = ast.parse(fh.read())
                names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
                strings = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
                assert 'ONNEXT_XML' not in names and 'OnNext' not in names, f
                assert not [t for t in strings if '.onnext.xml' in t], f


def test_up_next_path_has_no_dialog():
    overlay = ast.unparse(_class(O_TREE, 'Overlay'))                 # code only, comments dropped
    for word in ('doModal', 'WindowXMLDialog', 'WindowXML', '.show()', '_overlay_lock'):
        assert word not in overlay, word
    assert 'class Overlay(object):' in overlay and 'Thread(target=self._waitOnNext' in overlay
    chk = ast.get_source_segment(SERVICES, _method(S_TREE, 'Player', '_onIdle'))
    assert 'onnext_helpers.window(' in chk and 'onnext_helpers.in_window(' in chk and 'overlay.rearmOnNext()' in chk
    assert 'roundupDIV' not in chk and 'ONNEXT_TIMER*3' not in chk


def test_mode3_asks_for_the_info_screen_explicitly():
    calls = [ast.unparse(n) for n in ast.walk(_method(O_TREE, 'Overlay', 'toggleOnNext')) if isinstance(n, ast.Call)]
    assert 'self.player.toggleInfo(True)' in calls


def test_no_12_hour_literal_left():
    assert '%I:%M%p' not in OVERLAY and 'epochTime' not in OVERLAY


def test_background_uses_the_helpers():
    text = ast.get_source_segment(OVERLAY, _method(O_TREE, 'Background', 'onInit'))
    assert 'onnext_helpers.next_time(' in text and 'onnext_helpers.card_text(' in text
    assert 'onnext_helpers.time_format(xbmc.getRegion(' in text


def test_imports_are_explicit():
    for tree in (O_TREE, S_TREE):
        assert any(isinstance(n, ast.Import) and [a.name for a in n.names] == ['onnext_helpers'] for n in tree.body)


def test_helpers_are_standard_library_python38_and_inert():
    imported = set()
    for node in ast.walk(H_TREE):
        if isinstance(node, ast.Import): imported.update(a.name.split('.')[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom): imported.add((node.module or '').split('.')[0])
    assert imported == {'ast', 'datetime', 'math', 're'}
    for node in H_TREE.body:                                         # importing reads nothing
        assert isinstance(node, (ast.Import, ast.FunctionDef, ast.Assign)), ast.dump(node)[:80]
        if isinstance(node, ast.Assign):
            assert not [n for n in ast.walk(node) if isinstance(n, ast.Call)], ast.unparse(node)
    calls = {ast.unparse(n.func) for n in ast.walk(H_TREE) if isinstance(n, ast.Call)}
    assert not calls & {'time.time', 'datetime.datetime.now', 'datetime.datetime.today', 'time.monotonic'}
    assert 'madteevee (imports fork, imports.64)' in HELPERS


def test_on_next_state_is_per_instance():
    class_level = {t.id for n in _class(O_TREE, 'Overlay').body if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)}
    assert not class_level & {'onnext', '_onNextLock', '_onNextControls', '_onNextShown', '_onNextVisible', '_onNextToken', '_onNextClosed'}


def test_constants_has_no_onnext_xml():
    assert 'ONNEXT_XML' not in _src('constants.py')


# ================================================================ mutation-test gaps (imports.64)

def _until(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end: raise AssertionError('timed out')
        time.sleep(0.001)


@pytest.mark.parametrize('item', [{'isfiller': True, 'isPlaylist': False}, {'isPlaylist': False}])
def test_no_up_next_during_filler(item):
    """Filler (and an item that does not say) never shows Up next nor re-arms it,
    as before."""
    for played in (100, 2800):                                       # before and inside the window
        overlay = RecordingOverlay()
        p = FakePlayer(overlay, 3600, played)
        p.playingItem = dict(item)
        _on_idle()(p)
        assert overlay.calls == [], played


def test_an_old_wait_thread_never_hides_the_next_card(madrid):
    """Card A is hidden by a restart and card B shows before A's wait thread looks
    again: A's thread ends quietly and B stays up."""
    rig = Rig(mode=2)
    rig.advance = False
    rig.overlay.open()                                               # no settle wait: every wait is a wait thread's
    gate, entered = threading.Event(), threading.Event()

    def hold_first():
        if rig.threads and threading.current_thread() is rig.threads[0]:
            entered.set()
            gate.wait(5)
    rig.on_wait = hold_first
    rig.overlay.toggleOnNext(True)                                   # card A
    try:
        assert entered.wait(5) and rig.prop == 'true'
        rig.overlay.rearmOnNext()                                    # restarted: A hidden
        rig.overlay.toggleOnNext(True)                               # the window again: card B
        assert rig.prop == 'true' and len(rig.threads) == 2
        gate.set()
        rig.threads[0].join(5)
        assert not rig.threads[0].is_alive()
        assert rig.prop == 'true' and rig.overlay._onNextVisible and rig.threads[1].is_alive()
        assert [m for m in rig.logs if m.startswith('Overlay: toggleOnNext, hidden,')] == ['Overlay: toggleOnNext, hidden, back before the window']
    finally:
        gate.set()
        rig.finish()


def test_a_button_is_seen_against_the_last_check(madrid):
    """A key pressed as the card came up (Kodi's idle seconds 0 then) and another
    a few seconds later: the second one hides it. Each check compares with the
    one before, not with the idle seconds at show time."""
    rig = Rig(mode=2)
    rig.advance = False
    rig.idle = 0
    rig.overlay.toggleOnNext(True)
    assert rig.prop == 'true'
    rig.idle = 3
    seen = len(rig.waits)
    _until(lambda: len(rig.waits) >= seen + 3)                       # the thread has read 3
    assert rig.threads[0].is_alive() and rig.prop == 'true'
    rig.idle = 0                                                     # the second key
    rig.join()
    assert rig.prop == 'false' and 'Overlay: toggleOnNext, hidden, button' in rig.logs


def test_toggle_on_next_never_waits_for_the_card(madrid):
    """The idle thread asks for Up next and goes on: the card's 30 s run on the
    wait thread (a hang here would stop every idle check, as the dialog did)."""
    rig = Rig(mode=2)
    rig.advance = False                                              # the 30 s never run out
    worker = threading.Thread(target=rig.overlay.toggleOnNext, args=(True,), daemon=True)
    worker.start()
    worker.join(2)
    try:
        assert not worker.is_alive() and rig.prop == 'true' and rig.threads[0].is_alive()
    finally:
        rig.finish()
        worker.join(5)
