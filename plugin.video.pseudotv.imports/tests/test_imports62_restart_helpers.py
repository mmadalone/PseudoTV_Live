# -*- coding: utf-8 -*-
"""imports.62: restart from the beginning (pop-up B, remote C, guide entry D).

restart_helpers is import-light: every decision is tested here without Kodi
stubs. The Kodi glue in services.py / overlay.py / context_restart.py is
checked in test_imports62_restart_glue.py.
"""
import ast
import ast as _ast
import calendar
import json
import os

import pytest

import restart_helpers as rh
from constants import PSEUDOTV_SLUG

HERE      = os.path.dirname(os.path.abspath(__file__))
ADDON_LIB = os.path.join(os.path.dirname(HERE), 'resources', 'lib')
T0   = calendar.timegm((2026, 11, 5, 10, 0, 0, 0, 0, 0))     # a show 10:00-11:00 UTC
S, E = '2026-11-05 10:00:00', '2026-11-05 11:00:00'
PATH = 'pvr://guide/0042/2026-11-05 10:00:00.epg'


def decode(plot):
    """Stand-in for Globals._decodePlot: Custom plots carry an item= payload."""
    if 'item=' not in plot: return {}
    return json.loads(plot.split('item=', 1)[1])


def custom_plot(chid='abc@PseudoTV_Live', start=T0):
    return 'Plot [COLOR item=%s' % json.dumps({'citem': {'id': chid}, 'start': start})


def details(hasarchive=True, bid=777, start=S, end=E, plot='', path=PATH):
    return {'hasarchive': hasarchive, 'broadcastnow': {'broadcastid': bid, 'starttime': start, 'endtime': end,
                                                       'plot': plot, 'filenameandpath': path}}


def info(chid='abc@PseudoTV_Live', start=T0, isfiller=False, radio=False, tune_ts=1.5):
    return {'isPseudoTV': True, 'chid': chid, 'fitem': {'start': start}, 'isfiller': isfiller, 'radio': radio, '_tune_ts': tune_ts}


# --- module hygiene ------------------------------------------------------------------

def test_module_is_import_light_and_py38():
    src  = open(os.path.join(ADDON_LIB, 'restart_helpers.py'), encoding='utf-8').read()
    tree = ast.parse(src, feature_version=(3, 8))
    names = {a.name for n in _ast.walk(tree) if isinstance(n, _ast.Import) for a in n.names}
    names |= {n.module for n in _ast.walk(tree) if isinstance(n, _ast.ImportFrom)}
    # imports.63: epg_utc_to_epoch moved to clock_helpers (standard library only, see
    # test_imports63_clock.py) and is re-exported here
    assert names <= {'calendar', 'json', 're', 'time', 'clock_helpers'}


def test_custom_suffix_matches_the_channel_id_slug():
    assert rh.CUSTOM_SUFFIX == '@' + PSEUDOTV_SLUG


def test_remote_command_fits_uc3_and_parses_like_the_integration():
    assert len(rh.REMOTE_COMMAND) <= 64
    method, args = rh.REMOTE_COMMAND.split(' ', 1)
    params = ast.literal_eval(args)
    assert method.lower() == 'jsonrpc.notifyall' and params == {'sender': rh.NOTIFY_SENDER, 'message': rh.NOTIFY_MESSAGE}
    assert rh.parse_notification(params['sender'], 'Other.' + params['message'], 'null') == {'path': None}
    sender, message = [p.strip() for p in rh.KEYMAP_ACTION[len('NotifyAll('):-1].split(',')]
    assert (sender, message) == (rh.NOTIFY_SENDER, rh.NOTIFY_MESSAGE)


# --- small helpers -------------------------------------------------------------------

@pytest.mark.parametrize('text, expected', [(S, T0), ('', None), ('  ', None), (None, None), (5, None),
                                            ('2026-11-05', None), ('2026-13-05 10:00:00', None)])
def test_epg_utc_to_epoch(text, expected):
    assert rh.epg_utc_to_epoch(text) == expected


@pytest.mark.parametrize('value, expected', [(0, 0), (5, 5), (95, 95), (120, 95), (-5, 0), ('5', 5),
                                             (None, 0), ([], 0), (True, 0), ('x', 0)])
def test_clamp_percent(value, expected):
    assert rh.clamp_percent(value) == expected


@pytest.mark.parametrize('elapsed, expected', [(89, False), (90, True), (1763, True), (1764, False),
                                               (1800, False), (-1, False), (30, False)])
def test_offer_progress_on_a_30_minute_show(elapsed, expected):
    assert rh.offer_progress(elapsed, 1800, 5, 98) is expected


def test_offer_progress_short_show_has_no_extra_floor():
    assert rh.offer_progress(30, 600, 5, 98) is True          # 5% of a 10-minute show (user decision: >= 5%)


@pytest.mark.parametrize('args', [(100, 1800, 0, 98), (100, 0, 5, 98), ('x', 1800, 5, 98), (100, 1800, [], 98)])
def test_offer_progress_off_or_garbage(args):
    assert rh.offer_progress(*args) is False


def test_custom_chid():
    assert rh.custom_chid({'citem': {'id': 'abc@PseudoTV_Live'}}) == 'abc@PseudoTV_Live'
    assert rh.custom_chid({'citem': {'id': 'TV3.es@autonomiques'}}) is None
    assert rh.custom_chid({}) is None and rh.custom_chid(None) is None and rh.custom_chid({'citem': 'x'}) is None


# --- pop-up offer (B) ---------------------------------------------------------------

def test_custom_offer():
    assert rh.custom_offer(info(), 600, 1800, 5, 98) is True
    assert rh.custom_offer(info(isfiller=True), 600, 1800, 5, 98) is False
    assert rh.custom_offer({k: v for k, v in info().items() if k != 'isfiller'}, 600, 1800, 5, 98) is False
    assert rh.custom_offer(info(radio=True), 600, 1800, 5, 98) is False
    assert rh.custom_offer({}, 600, 1800, 5, 98) is False
    assert rh.custom_offer(info(), 30, 1800, 5, 98) is False


def test_custom_target():
    assert rh.custom_target(info()) == {'kind': 'custom', 'chid': 'abc@PseudoTV_Live', 'start': T0}
    assert rh.custom_target({'isPseudoTV': True, 'chid': 'x', 'fitem': {}}) is None
    assert rh.custom_target({}) is None


def test_livetv_target_offer_and_explicit():
    now = T0 + 600
    target = rh.livetv_target(42, details(), now, decode, threshold=5, max_progress=98, offer=True)
    assert target == {'kind': 'livetv', 'channelid': 42, 'broadcastid': 777, 'start': T0, 'end': T0 + 3600}
    assert rh.livetv_target(42, details(), T0 + 60, decode, threshold=5, max_progress=98, offer=True) is None
    assert rh.livetv_target(42, details(), T0 + 60, decode, offer=False) is not None


@pytest.mark.parametrize('channelid, det, now', [
    (0, details(), T0 + 600), (True, details(), T0 + 600), (None, details(), T0 + 600),
    (42, details(hasarchive=False), T0 + 600),            # A Punt, USA, autonomiques
    (42, {'hasarchive': True}, T0 + 600),                  # no show on now
    (42, details(bid=0), T0 + 600), (42, details(start=''), T0 + 600),
    (42, details(), T0 - 10),                              # future programme (vod 'playable' quirk)
    (42, details(), T0 + 3600),                            # now == end
    (42, details(plot=custom_plot()), T0 + 600),           # a Custom channel never takes the LiveTV route
], ids=['id0', 'idbool', 'idnone', 'noarchive', 'nobnow', 'bid0', 'nostart', 'future', 'atend', 'custom'])
def test_livetv_target_rejects(channelid, det, now):
    assert rh.livetv_target(channelid, det, now, decode, offer=False) is None


# --- guide entry (D) ----------------------------------------------------------------

def _channels():
    return [dict(details(bid=10, path='pvr://guide/0001/2026-11-05 10:00:00.epg'), channelid=1),
            dict(details(bid=777, plot=''), channelid=42),
            dict(details(bid=55, plot=custom_plot(), path='pvr://guide/0007/2026-11-05 10:00:00.epg'), channelid=7),
            dict(details(hasarchive=False, bid=66, path='pvr://guide/0009/2026-11-05 10:00:00.epg'), channelid=9)]


def test_guide_target_livetv_custom_and_refusals():
    now = T0 + 600
    assert rh.guide_target(_channels(), PATH, now, decode) == {'kind': 'livetv', 'channelid': 42, 'broadcastid': 777,
                                                              'start': T0, 'end': T0 + 3600}
    custom = rh.guide_target(_channels(), 'pvr://guide/0007/2026-11-05 10:00:00.epg', now, decode)
    assert custom == {'kind': 'custom', 'chid': 'abc@PseudoTV_Live', 'start': T0, 'channelid': 7, 'broadcastid': 55, 'end': T0 + 3600}
    assert rh.guide_target(_channels(), 'pvr://guide/0009/2026-11-05 10:00:00.epg', now, decode) is None   # no archive
    assert rh.guide_target(_channels(), 'pvr://guide/0042/2026-11-05 09:00:00.epg', now, decode) is None   # not the show on now
    assert rh.guide_target(_channels(), PATH, T0 + 3600, decode) is None                                   # ended meanwhile
    assert rh.guide_target(_channels(), 'pvr://recordings/tv/active/x.pvr', now, decode) is None
    assert rh.guide_target([], PATH, now, decode) is None and rh.guide_target(None, PATH, now, decode) is None


# --- playing snapshot -----------------------------------------------------------------

def test_classify_playing():
    live = {'type': 'channel', 'id': 42}
    assert rh.classify_playing({'video': False}) == {'kind': 'none'}
    c = rh.classify_playing({'video': True, 'item': live, 'pvr_live': True, 'seekable': True, 'pseudotv': info(), 'tune_ts': 1.5})
    assert c['kind'] == 'custom' and c['tune_ok'] and c['channelid'] == 42 and c['start'] == T0
    assert rh.classify_playing({'video': True, 'pseudotv': info(), 'tune_ts': 2.5})['tune_ok'] is False      # zap in progress
    assert rh.classify_playing({'video': True, 'pseudotv': info(), 'tune_ts': None})['tune_ok'] is False
    assert rh.classify_playing({'video': True, 'item': {'type': 'unknown', 'id': 777}, 'pvr_epgtag': True,
                                'seekable': True}) == {'kind': 'replay', 'broadcastid': 777, 'seekable': True}
    assert rh.classify_playing({'video': True, 'item': live, 'pvr_live': True}) == {'kind': 'livetv', 'channelid': 42, 'seekable': False}
    assert rh.classify_playing({'video': True, 'item': {'type': 'movie', 'id': 5}}) == {'kind': 'other'}


# --- decide ------------------------------------------------------------------------------

NOW    = T0 + 600
CUSTOM = {'kind': 'custom', 'chid': 'abc@PseudoTV_Live', 'start': T0, 'isfiller': False, 'radio': False,
          'tune_ok': True, 'seekable': True, 'channelid': 7}
LIVE   = {'kind': 'livetv', 'channelid': 42, 'seekable': False}
REPLAY = {'kind': 'replay', 'broadcastid': 777, 'seekable': True}
LT     = {'kind': 'livetv', 'channelid': 42, 'broadcastid': 777, 'start': T0, 'end': T0 + 3600, 'playable': True}
CT     = {'kind': 'custom', 'chid': 'abc@PseudoTV_Live', 'start': T0, 'channelid': 7, 'broadcastid': 55, 'end': T0 + 3600}


@pytest.mark.parametrize('playing, target, guide, action', [
    # C: restart what plays
    ({'kind': 'none'}, None, False, rh.UNAVAILABLE),
    ({'kind': 'other'}, None, False, rh.UNAVAILABLE),
    (CUSTOM, None, False, rh.SEEK_START),
    (dict(CUSTOM, isfiller=True), None, False, rh.UNAVAILABLE),
    (dict(CUSTOM, seekable=False), None, False, rh.UNAVAILABLE),
    (dict(CUSTOM, tune_ok=False), None, False, rh.IGNORE),
    (REPLAY, None, False, rh.SEEK_START),
    (dict(REPLAY, seekable=False), None, False, rh.UNAVAILABLE),
    (LIVE, None, False, rh.LOOKUP_LIVE),
    # B / C with a target: it must still be what plays
    (LIVE, LT, False, rh.OPEN_BROADCAST),
    (LIVE, dict(LT, playable=False), False, rh.UNAVAILABLE),
    (dict(LIVE, channelid=43), LT, False, rh.IGNORE),
    (LIVE, dict(LT, end=NOW), False, rh.UNAVAILABLE),
    (CUSTOM, {'kind': 'custom', 'chid': 'abc@PseudoTV_Live', 'start': T0}, False, rh.SEEK_START),
    (CUSTOM, {'kind': 'custom', 'chid': 'abc@PseudoTV_Live', 'start': T0 + 1800}, False, rh.IGNORE),
    # D: restart that guide entry
    (None, None, True, rh.UNAVAILABLE),
    ({'kind': 'none'}, LT, True, rh.OPEN_BROADCAST),
    (dict(LIVE, channelid=43), LT, True, rh.OPEN_BROADCAST),
    (REPLAY, LT, True, rh.SEEK_START),
    (dict(REPLAY, broadcastid=778), LT, True, rh.OPEN_BROADCAST),
    (CUSTOM, CT, True, rh.SEEK_START),
    (dict(CUSTOM, start=T0 - 1800), CT, True, rh.RETUNE_CUSTOM),
    (dict(LIVE, channelid=7), CT, True, rh.RETUNE_CUSTOM),
    (dict(CUSTOM, start=T0 - 1800, channelid=None), CT, True, rh.TUNE_CUSTOM),   # a replay file of that channel: no stop needed
    (dict(CUSTOM, chid='zzz@PseudoTV_Live', channelid=8), CT, True, rh.TUNE_CUSTOM),
    ({'kind': 'none'}, CT, True, rh.TUNE_CUSTOM),
    (LIVE, CT, True, rh.TUNE_CUSTOM),
    ({'kind': 'none'}, dict(CT, end=NOW), True, rh.UNAVAILABLE),
], ids=lambda v: v if isinstance(v, (str, bool)) else None)
def test_decide(playing, target, guide, action):
    assert rh.decide(playing, target, NOW, guide=guide)[0] == action


# --- after Player.Open {broadcastid} -----------------------------------------------------

@pytest.mark.parametrize('state, outcome', [
    ({'elapsed': 3, 'replay': True, 'playing': True}, 'started'),
    ({'elapsed': 3, 'unchanged': True, 'playing': True, 'busy': True, 'idle': 3}, 'wait'),
    ({'elapsed': 9, 'unchanged': True, 'playing': True, 'idle': 9}, 'wait'),
    ({'elapsed': 10, 'unchanged': True, 'playing': True, 'idle': 10}, 'refused'),        # live goes on: note only
    ({'elapsed': 2, 'playing': False, 'idle': 2, 'errored': True, 'can_retune': True}, 'retune'),
    ({'elapsed': 1.5, 'playing': False, 'idle': 2, 'errored': True, 'can_retune': True}, 'wait'),   # error settles first
    ({'elapsed': 2, 'playing': False, 'idle': 2, 'errored': True, 'can_retune': False}, 'failed'),  # D with nothing before
    ({'elapsed': 5, 'playing': False, 'idle': 10, 'can_retune': True}, 'wait'),
    ({'elapsed': 10, 'playing': False, 'idle': 10, 'can_retune': True}, 'failed'),       # gone without an error: no re-tune
    ({'elapsed': 1, 'playing': False, 'idle': 99, 'stopped': True, 'can_retune': True}, 'gave_up'),   # a Stop (JSON-RPC too)
    ({'elapsed': 3, 'playing': False, 'idle': 99, 'stopped': True, 'errored': True, 'can_retune': True}, 'gave_up'),
    ({'elapsed': 4, 'playing': False, 'idle': 1, 'errored': True, 'can_retune': True}, 'gave_up'),    # a key press: leave it
    ({'elapsed': 12, 'playing': False, 'busy': True, 'idle': 12, 'errored': True, 'can_retune': True}, 'wait'),   # busy dialog
    ({'elapsed': 30, 'playing': False, 'busy': True, 'idle': 30, 'errored': True, 'can_retune': True}, 'gave_up'),
    ({'elapsed': 30, 'unchanged': True, 'playing': True, 'busy': True, 'idle': 30}, 'refused'),
    ({'elapsed': 4, 'playing': True, 'unchanged': False}, 'other'),                     # zapped elsewhere: stop watching
    # idle anchored at Player.Open (idle0): 1.4 s before it, then no key / a key 0.5 s after it
    ({'elapsed': 2.6, 'playing': False, 'idle': 4, 'idle0': 1, 'errored': True, 'can_retune': True}, 'retune'),
    ({'elapsed': 2.6, 'playing': False, 'idle': 2, 'idle0': 1, 'errored': True, 'can_retune': True}, 'gave_up'),
    ({'elapsed': 2.0, 'playing': False, 'idle': 1, 'idle0': 0, 'errored': True, 'can_retune': True}, 'gave_up'),
])
def test_watch_outcome(state, outcome):
    assert rh.watch_outcome(state) == outcome


# --- pending Custom guide restart ----------------------------------------------------------

def test_pending_match_and_expiry():
    pending = {'chid': 'abc@PseudoTV_Live', 'start': T0, 'ts': 1000.0}
    assert rh.pending_match(pending, info(), 1010.0) is True
    assert rh.pending_match(pending, info(start=T0 + 1800), 1010.0) is False
    assert rh.pending_match(pending, info(chid='x@PseudoTV_Live'), 1010.0) is False
    assert rh.pending_match(pending, info(isfiller=True), 1010.0) is False
    assert rh.pending_match(pending, info(), 1031.0) is False and rh.pending_expired(pending, 1031.0) is True
    assert rh.pending_expired(pending, 1010.0) is False and rh.pending_expired(None, 1.0) is True
    assert rh.pending_match(None, info(), 1010.0) is False


def test_popup_done():
    assert rh.popup_done(3, 3, NOW) is False and rh.popup_done(3, 4, NOW) is True
    assert rh.popup_done(3, 3, NOW, end=NOW) is True and rh.popup_done(3, 3, NOW, end=NOW + 1) is False


# --- notifications -------------------------------------------------------------------------

@pytest.mark.parametrize('sender, method, data, expected', [
    ('pseudotv', 'Other.restart', 'null', {'path': None}),
    ('pseudotv', 'Other.restart', None, {'path': None}),
    ('pseudotv', 'Other.restart', '{}', {'path': None}),
    ('pseudotv', 'Other.restart', '[null]', {'path': None}),
    ('pseudotv', 'Other.restart', json.dumps({'path': PATH}), {'path': PATH}),
    ('pseudotv', 'Other.restart', json.dumps([{'path': PATH}]), {'path': PATH}),
    ('pseudotv', 'Other.restart', json.dumps({'path': 'pvr://recordings/tv/x.pvr'}), None),
    ('pseudotv', 'Other.restart', '{bad json', None),
    ('pseudotv', 'Other.restart', '5', None),
    ('plugin.video.pseudotv.imports.SIGNAL', 'Other.upnext_data', 'null', None),
    ('pseudotv', 'Other.Restart', 'null', None),
    ('xbmc', 'Player.OnPlay', 'null', None),
])
def test_parse_notification(sender, method, data, expected):
    assert rh.parse_notification(sender, method, data) == expected


def test_guide_request():
    assert rh.guide_request(PATH) == {'path': PATH}
    assert rh.guide_request('pvr://guide/12345/2026-11-05 10:00:00.epg') is not None
    for bad in ('pvr://guide/abc/2026-11-05 10:00:00.epg', 'pvr://channels/tv/All/pvr.iptvsimple_1.pvr', '', None, 7):
        assert rh.guide_request(bad) is None


def test_watch_timings_are_ordered():
    assert 0 < rh.ERROR_SETTLE < rh.WATCH_GRACE < rh.WATCH_TIMEOUT and rh.WATCH_POLL < rh.ERROR_SETTLE


def test_popup_pass_on_keys():
    assert rh.POPUP_PASS_ON[3] == 'Up' and rh.POPUP_PASS_ON[4] == 'Down' and rh.POPUP_PASS_ON[11] == 'Info'
    assert [rh.POPUP_PASS_ON[58 + n] for n in range(10)] == ['Number%d' % n for n in range(10)]
    for key in (7, 92, 10, 1, 2, 117, 184, 185, 5, 6):     # OK, Back, Previous menu, Left, Right, Context menu, Channel +/-, PageUp/Down
        assert key not in rh.POPUP_PASS_ON


def test_window_ready_condition_names():
    for part in ('Window.IsActive(fullscreenvideo)', '!Player.ShowInfo', '!Player.ChannelPreviewActive',
                 '!System.HasActiveModalDialog', 'Player.Playing'):
        assert part in rh.WINDOW_READY


def test_now_is_not_read_inside_decisions():
    # every time-dependent helper takes `now` from the caller (testable, no hidden clock)
    src = open(os.path.join(ADDON_LIB, 'restart_helpers.py'), encoding='utf-8').read()
    assert 'time.time(' not in src
