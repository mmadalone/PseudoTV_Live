# -*- coding: utf-8 -*-
"""imports.70: LiveTV replays frozen at their end move on -- replayend_helpers, the decisions.

On 2026-11-15 a replay of a Mega show, opened with the RESTART pop-up while the
show was still on air, froze on its last second (1:40:53 of 1:40:54) for
1 h 26 min. Movistar+ serves a show still on air as a growing live stream (in
6 of 6 measured cases its window opens a minute or two before the guide start
and closes about ten minutes after the guide end); inputstream.adaptive never
ends the last period of a live stream, so Kodi only logs "stream stalled" and
its "Play next programme automatically", which runs on a natural end, never
fires. The add-on now notices the freeze and does what Kodi does at a natural
end.

replayend_helpers holds every decision and imports only clock_helpers and
restart_helpers, so it runs here without Kodi stubs:
  - playing_broadcast / programme: the lookup (is the playing replay's show the
    one on air?);
  - new_track / stall_step / ready: the tick's count (position and length
    still for 20 s at the end of the stream, after the guide end, nothing
    paused or seeking; it waits for full-screen video and no key press);
  - autoplay_on / next_broadcast / after_open: the move (Kodi's own flag,
    Kodi's next-show rule, the fallbacks).
The Kodi side (services.py) is tested in test_imports70_replay_end.py.
"""
import ast
import calendar
import json
import os
import time

import pytest

import replayend_helpers as rh

HERE      = os.path.dirname(os.path.abspath(__file__))
ADDON_LIB = os.path.join(os.path.dirname(HERE), 'resources', 'lib')


def _src(name):
    with open(os.path.join(ADDON_LIB, name), encoding='utf-8') as f: return f.read()


def utc(epoch):
    """Kodi's JSON-RPC broadcast time text (UTC) of an epoch."""
    return time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(epoch))


def decode(plot):
    """Stand-in for Globals._decodePlot: Custom plots carry an item= payload."""
    if 'item=' not in plot: return {}
    return json.loads(plot.split('item=', 1)[1])


CUSTOM_PLOT = 'Plot [COLOR item=%s' % json.dumps({'citem': {'id': 'abc@PseudoTV_Live'}, 'start': 0})

# The 2026-11-15 case, as measured: the show ran 86 minutes; Movistar+'s window opened 174 s
# before its guide start and closed 720 s after its guide end, so the stream ended 6054 s long
# and the player stood still at 6053 s. (The clock time is illustrative; the offsets are measured.)
S = calendar.timegm((2026, 11, 15, 15, 0, 0, 0, 0, 0))
E = S + 86 * 60
LEAD, TAIL = 174, 720
CID, BID, NEXT_BID = 47, 90210, 90211


def row(cid=CID, bid=BID, start=S, end=E, plot='A show', **extra):
    """A PVR.GetChannels row (broadcastnow, hasarchive), as getPVRChannelsNow reads it."""
    bnow = {'broadcastid': bid, 'starttime': utc(start) if isinstance(start, int) else start,
            'endtime': utc(end) if isinstance(end, int) else end, 'plot': plot, 'title': 'Servicio Secreto'}
    bnow.update(extra)
    return {'channelid': cid, 'hasarchive': True, 'broadcastnow': bnow}


def entry(bid, start, end):
    """A PVR.GetBroadcasts row (properties starttime + endtime; Kodi adds broadcastid and label)."""
    return {'broadcastid': bid, 'label': 'show %s' % bid, 'starttime': utc(start), 'endtime': utc(end)}


# ================================================================ module hygiene


def _tree():
    return ast.parse(_src('replayend_helpers.py'), feature_version=(3, 8))


def test_module_imports_only_clock_and_restart_helpers():
    """`import clock_helpers` (never `from clock_helpers import ...`: tests replace
    clock_helpers.now) and restart_helpers for the Custom-plot check; nothing from Kodi."""
    tree = _tree()
    imports = [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert all(isinstance(n, ast.Import) for n in imports)
    assert sorted(a.name for n in imports for a in n.names) == ['clock_helpers', 'restart_helpers']
    assert all(n in tree.body for n in imports)
    assert 'madteevee (imports fork, imports.70)' in _src('replayend_helpers.py')


def test_module_is_python38():
    """Kodi 19/20 run Python 3.8: the AST check plus the shared deny-list of 3.9+ APIs and syntax
    (the no-'|' scanner of test_imports69_importcfg_helpers.py lists this file too)."""
    from test_imports63_clock import py39_api_leaks
    src = _src('replayend_helpers.py')
    ast.parse(src, feature_version=(3, 8))
    assert py39_api_leaks(src) == []
    from test_imports69_importcfg_helpers import BITOR_REVIEWED, bitor_lines
    assert BITOR_REVIEWED['replayend_helpers.py'] == [] and bitor_lines(src) == []


def test_module_does_no_io_and_reads_no_clock():
    """Callers pass `now`; the helpers only decide."""
    called = set()
    for node in ast.walk(_tree()):
        if isinstance(node, ast.Call):
            called.add(ast.unparse(node.func))
    assert not called & {'open', '__import__', 'eval', 'exec', 'compile', 'input', 'print', 'clock_helpers.now',
                         'time.time', 'time.monotonic'}
    assert {c for c in called if c.startswith('clock_helpers.')} == {'clock_helpers.epg_utc_to_epoch'}
    assert {c for c in called if c.startswith('restart_helpers.')} == {'restart_helpers.custom_chid'}


def test_module_has_no_shared_state_and_no_mutable_defaults():
    tree = _tree()
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            assert isinstance(node.value, ast.Constant), ast.unparse(node)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            defaults = node.args.defaults + [d for d in node.args.kw_defaults if d is not None]
            assert not [d for d in defaults if not isinstance(d, ast.Constant)], node.name


def test_public_api_is_the_planned_one():
    public = {name for name in vars(rh) if not name.startswith('_')}
    assert public == {'clock_helpers', 'restart_helpers',
                      'ARM_DELAY_S', 'STALL_S', 'POS_EPS', 'END_SLACK_S', 'END_GRACE_S', 'TICK_GAP_S', 'QUIET_S',
                      'REARM_S', 'MAX_FIRES', 'RETRY_S', 'BUSY', 'ACT_WINDOW', 'AUTOPLAY_PROPERTY',
                      'playing_broadcast', 'programme', 'new_track', 'ready', 'stall_step', 'autoplay_on',
                      'next_broadcast', 'after_open'}


def test_constants_are_the_planned_ones():
    assert (rh.ARM_DELAY_S, rh.STALL_S, rh.POS_EPS, rh.END_SLACK_S, rh.END_GRACE_S, rh.TICK_GAP_S) == \
        (5.0, 20.0, 0.25, 10.0, 60.0, 5.0)
    assert (rh.QUIET_S, rh.REARM_S, rh.MAX_FIRES, rh.RETRY_S) == (10, 60.0, 2, 60.0)
    # 20 s is four of Kodi's 5 s caching timers: normal buffering never counts as a freeze.
    assert rh.STALL_S >= 4 * 5
    # a seek back starts a new pass only beyond the end slack (a pass can't rearm itself)
    assert rh.REARM_S > rh.END_SLACK_S
    assert rh.AUTOPLAY_PROPERTY == 'epg_playlist_item'


def test_kodi_conditions_are_the_planned_ones():
    """Kodi's own condition syntax: '|' is OR, '+' is AND, '!' is NOT (InfoExpression)."""
    assert rh.BUSY == 'Player.Paused | Player.Seeking | Player.Forwarding | Player.Rewinding'
    assert rh.ACT_WINDOW == ('Window.IsActive(fullscreenvideo) + !System.HasActiveModalDialog'
                             ' + !Player.ChannelPreviewActive')


# ================================================================ playing_broadcast


@pytest.mark.parametrize('item, expected', [
    ({'type': 'unknown', 'id': 777, 'label': 'A show'}, 777),       # a playing guide entry: its broadcast id
    ({'type': 'unknown', 'id': 1}, 1),
    ({'type': 'channel', 'id': 42}, None),                           # live: a channel id, never a broadcast
    ({'type': 'unknown', 'id': 0}, None),
    ({'type': 'unknown', 'id': -1}, None),                           # Kodi's "no id"
    ({'type': 'unknown', 'id': True}, None),
    ({'type': 'unknown', 'id': '777'}, None),
    ({'type': 'unknown', 'id': 777.0}, None),
    ({'type': 'unknown'}, None),
    ({}, None), (None, None), ([], None), ('777', None),
])
def test_playing_broadcast(item, expected):
    assert rh.playing_broadcast(item) == expected


# ================================================================ programme


def test_programme_on_air():
    facts = rh.programme([row(cid=12, bid=5), row()], BID, S + 600, decode)
    assert facts == {'channelid': CID, 'broadcastid': BID, 'start': S, 'end': E, 'endtime': utc(E)}


@pytest.mark.parametrize('now, on_air', [(S - 1, False), (S, True), (E - 1, True), (E, False), (E + 600, False)])
def test_programme_only_while_on_air(now, on_air):
    """A finished show's replay is a recording that ends by itself (left to Kodi), and Kodi's
    broadcastnow can lag the clock by a moment: the times decide, start <= now < end."""
    assert (rh.programme([row()], BID, now, decode) is not None) is on_air


def test_programme_not_the_show_on_now():
    assert rh.programme([row(bid=BID + 1)], BID, S + 600, decode) is None
    assert rh.programme([], BID, S + 600, decode) is None
    assert rh.programme(None, BID, S + 600, decode) is None


def test_programme_never_a_custom_show():
    """Custom (PseudoTV) channels have their own player; their show is never watched."""
    assert rh.programme([row(plot=CUSTOM_PLOT)], BID, S + 600, decode) is None
    # without a decoder nothing tells a Custom plot apart (services.py always passes one)
    assert rh.programme([row(plot=CUSTOM_PLOT)], BID, S + 600) is not None


@pytest.mark.parametrize('kw', [
    {'start': ''}, {'end': ''}, {'start': 'yesterday'}, {'end': None}, {'start': '2026-13-15 10:00:00'},
], ids=['no-start', 'no-end', 'text-start', 'none-end', 'bad-month'])
def test_programme_bad_times(kw):
    assert rh.programme([row(**kw)], BID, S + 600, decode) is None


@pytest.mark.parametrize('broadcastid', [True, '90210', 0, -1, None, 90210.0])
def test_programme_bad_broadcast_ids(broadcastid):
    assert rh.programme([row()], broadcastid, S + 600, decode) is None
    assert rh.programme([row(bid=broadcastid)], BID, S + 600, decode) is None


@pytest.mark.parametrize('channelid', [True, '47', 0, None])
def test_programme_bad_channel_ids(channelid):
    assert rh.programme([row(cid=channelid)], BID, S + 600, decode) is None


def test_programme_skips_unusable_rows():
    rows = [None, 'x', {'channelid': 5}, {'channelid': 6, 'broadcastnow': None}, row()]
    assert rh.programme(rows, BID, S + 600, decode)['channelid'] == CID


def test_programme_is_the_same_in_every_time_zone(clock_zone):
    """Kodi's broadcast times are UTC text; the add-on's clock is the epoch (imports.63)."""
    start, end = clock_zone - 600, clock_zone + 1200
    facts = rh.programme([row(start=start, end=end)], BID, clock_zone, decode)
    assert (facts['start'], facts['end'], facts['endtime']) == (start, end, utc(end))


# ================================================================ new_track / ready


def test_new_track():
    assert rh.new_track(7, 1000.5) == {'epoch': 7, 'seen': 1000.5, 'lookup': False, 'tick': None, 'pos': None,
                                       'total': None, 'since': None, 'fires': 0, 'fired_at': None,
                                       'deferred': False}
    assert rh.new_track(7, 1.0) is not rh.new_track(7, 1.0)


@pytest.mark.parametrize('window, idle, expected', [
    (True, 10, True), (True, 9999, True), (True, 10.0, True),
    (True, 9, False), (True, 9.99, False), (True, 0, False),
    (False, 9999, False), (None, 9999, False),
    (True, None, False), (True, '20', False), (True, True, False),
])
def test_ready(window, idle, expected):
    assert rh.ready(window, idle) is expected


# ================================================================ stall_step

QUIET = {'busy': False, 'window': True, 'idle': 9999}


def sample(pos, total, **kw):
    out = dict(QUIET, pos=pos, total=total)
    out.update(kw)
    return out


def run(ticks, end=E, track=None):
    """Feeds [(now, sample)] through stall_step -> (the last track, the times it was due)."""
    if track is None: track = rh.new_track(1, ticks[0][0] - 1)
    dues = []
    for now, s in ticks:
        kept = json.dumps(track, sort_keys=True)
        track_in = track
        track, due = rh.stall_step(track_in, s, end, now)
        assert json.dumps(track_in, sort_keys=True) == kept and track is not track_in     # never changed in place
        if due: dues.append(now)
    return track, dues


def held(t0, seconds, pos, total, step=1.0, **kw):
    """Ticks from t0 for `seconds`, the player standing still at pos / total."""
    return [(t0 + i * step, sample(pos, total, **kw)) for i in range(int(round(seconds / step)) + 1)]


def growing(t_open, start=S, end=E, offset=0.0, lead=LEAD, tail=TAIL):
    """Movistar+'s replay of a show on air: the window opens `lead` s before the guide start and
    closes `tail` s after the guide end; the viewer plays it at 1x from `offset` s in, from
    t_open; the player stops one second before the end of what the window holds."""
    w0, w1 = start - lead, end + tail

    def at(now):
        total = min(now, w1) - w0
        return sample(float(min(now - t_open + offset, total - 1)), float(total))
    return at


def timeline(model, t_from, t_to, step=1.0):
    n = int(round((t_to - t_from) / step))
    return [(t_from + i * step, model(t_from + i * step)) for i in range(n + 1)]


def test_the_2026_11_15_freeze_fires_once_twenty_seconds_after_it_stopped():
    """RESTART 10 minutes into the show: the viewer stays 10 min 174 s behind live, the window
    closes at the guide end + 12 min, the player reaches 6053 of 6054 s 6053 s after the open and
    stands there. Due exactly once 20 s later; once the move worked the playback is another one
    (the tick then starts a new track), so nothing else is fed."""
    t_open = S + 600
    stuck  = t_open + 6053                        # the first tick that reads 6053 of 6054
    track, dues = run(timeline(growing(t_open), t_open, stuck + 59))
    assert dues == [stuck + rh.STALL_S]
    assert (track['pos'], track['total'], track['fires']) == (6053.0, 6054.0, 1)


def test_a_freeze_that_goes_on_fires_once_more_after_a_minute_and_never_a_third_time():
    """1 h 26 min standing still with every move failing: two attempts, RETRY_S apart."""
    t_open = S + 600
    stuck  = t_open + 6053
    track, dues = run(timeline(growing(t_open), t_open, stuck + 86 * 60))
    assert dues == [stuck + rh.STALL_S, stuck + rh.STALL_S + rh.RETRY_S]
    assert track['fires'] == rh.MAX_FIRES


def test_normal_play_never_fires():
    """A recording (a finished show) played to its end: the position moves every tick."""
    ticks = [(E + 900 + i, sample(float(i), 3600.0)) for i in range(3601)]
    assert run(ticks)[1] == []


def test_the_live_edge_never_fires_while_the_window_grows():
    """A viewer at the live edge: position and length both grow."""
    model = growing(S + 600, offset=600 + LEAD - 1)
    assert run(timeline(model, S + 600, E + TAIL - 1))[1] == []


def test_a_growing_length_with_the_position_stuck_never_fires():
    ticks = [(E + 100 + i, sample(5000.0, 5000.0 + i)) for i in range(600)]
    assert run(ticks)[1] == []


@pytest.mark.parametrize('left, fires', [(1.0, True), (10.0, True), (10.01, False), (11.0, False), (300.0, False)])
def test_only_at_the_end_of_the_stream(left, fires):
    ticks = held(E + 900, 40, 6054.0 - left, 6054.0)
    assert bool(run(ticks)[1]) is fires


@pytest.mark.parametrize('frozen_from, first_due', [
    (E - 300, E + 60),            # the window closed early, before the guide end: waits for end + 60
    (E + 30, E + 60),             # stood still since end + 30: end + 60 is later than its 20 s
    (E + 50, E + 70),             # stood still since end + 50: its 20 s come after end + 60
])
def test_never_before_the_guide_end_and_a_minute(frozen_from, first_due):
    ticks = held(frozen_from, first_due - frozen_from + 5, 3000.0, 3001.0)
    assert run(ticks)[1][:1] == [first_due]


def test_the_grace_boundary():
    """end + 59 s waits, end + 60 s acts."""
    track, dues = run(held(E, 59, 3000.0, 3001.0))
    assert dues == []
    _, dues = run([(E + 60, sample(3000.0, 3001.0))], track=track)
    assert dues == [E + 60]


def test_without_the_guide_end_never():
    assert run(held(E + 900, 100, 3000.0, 3001.0), end=None)[1] == []


def test_pause_and_seeks_start_the_count_again():
    """Kodi's BUSY condition, one read (paused, a CEC standby that pauses, seeking, fast-forward /
    rewind): 15 s still, one busy tick, then still again: due 20 s after the busy tick, not 5 s
    later."""
    t0 = E + 900
    ticks = held(t0, 15, 3000.0, 3001.0)
    ticks.append((t0 + 16, sample(3000.0, 3001.0, busy=True)))
    ticks += held(t0 + 17, 30, 3000.0, 3001.0)
    assert run(ticks)[1][:1] == [t0 + 16 + rh.STALL_S]


@pytest.mark.parametrize('gap, restarts', [(5.0, False), (5.01, True), (60.0, True), (-1.0, True), (-0.01, True)],
                         ids=['5s', '5.01s', 'suspend', 'clock-back', 'clock-back-a-little'])
def test_a_long_gap_or_a_clock_going_back_starts_the_count_again(gap, restarts):
    t0 = E + 900
    ticks = held(t0, 10, 3000.0, 3001.0)
    t1 = t0 + 10 + gap
    ticks += held(t1, 40, 3000.0, 3001.0)
    first = run(ticks)[1][0]
    assert first == (t1 + rh.STALL_S if restarts else t0 + rh.STALL_S)


@pytest.mark.parametrize('delta, still', [(0.0, True), (0.25, True), (-0.25, True), (0.26, False), (-0.26, False)])
def test_standing_still_means_within_a_quarter_second(delta, still):
    """Position and length each: a paused clock reads the same value, play moves about 1 s."""
    t0 = E + 900
    for field in ('pos', 'total'):
        ticks = []
        for i in range(30):
            pos, total = 3000.0, 3001.0
            if field == 'pos': pos += delta * (i % 2)
            else: total += delta * (i % 2)
            ticks.append((t0 + i, sample(pos, total)))
        assert bool(run(ticks)[1]) is still, field


@pytest.mark.parametrize('step', [(2999.7, 3001.0), (3000.0, 3000.7)], ids=['pos-back', 'length-shrinks'])
def test_a_one_way_step_back_starts_the_count_again(step):
    """imports.70 review (breaks T1, T2): standing still is within POS_EPS both ways. One step back
    of 0.3 s with no busy flag (a short seek the tick missed, Kodi's clock correcting itself), or a
    length that shrinks, starts the 20 s count again; the wobble test above can't tell."""
    t0 = E + 900
    ticks = held(t0, 15, 3000.0, 3001.0) + held(t0 + 16, 40, *step)
    assert run(ticks)[1][:1] == [t0 + 16 + rh.STALL_S]


@pytest.mark.parametrize('held_for, fires', [(19.9, False), (20.0, True)])
def test_the_twenty_second_boundary(held_for, fires):
    t0 = E + 900
    ticks = [(t0, sample(3000.0, 3001.0)), (t0 + 1, sample(3000.0, 3001.0)), (t0 + held_for, sample(3000.0, 3001.0))]
    ticks[2:2] = [(t0 + s, sample(3000.0, 3001.0)) for s in range(2, int(held_for))]
    assert bool(run(ticks)[1]) is fires


@pytest.mark.parametrize('field, value', [('pos', None), ('total', None), ('pos', 'x'), ('total', True)])
def test_unknown_values_never_count_as_still(field, value):
    t0 = E + 900
    ticks = [(t0 + i, sample(**dict({'pos': 3000.0, 'total': 3001.0}, **{field: value}))) for i in range(40)]
    assert run(ticks)[1] == []


def test_an_unknown_value_starts_the_count_again():
    t0 = E + 900
    ticks = held(t0, 15, 3000.0, 3001.0) + [(t0 + 16, sample(None, None))] + held(t0 + 17, 30, 3000.0, 3001.0)
    assert run(ticks)[1][:1] == [t0 + 17 + rh.STALL_S]


def test_a_zero_length_never_fires():
    assert run(held(E + 900, 40, 0.0, 0.0))[1] == []


def test_no_sample_at_all_never_fires():
    track = rh.new_track(1, E)
    for i in range(40):
        track, due = rh.stall_step(track, None, E, E + 900 + i)
        assert due is False


@pytest.mark.parametrize('away', [{'window': False}, {'idle': 9}, {'window': False, 'idle': 0}],
                         ids=['in-the-guide', 'key-pressed', 'both'])
def test_it_waits_for_full_screen_and_no_key_then_fires_at_once(away):
    """In the guide, a menu, the OSD or a key pressed in the last 10 s: due but deferred (no
    attempt used); the first tick back in full-screen video with no key acts."""
    t0 = E + 900
    ticks = held(t0, 60, 3000.0, 3001.0, **away)
    track, dues = run(ticks)
    assert dues == [] and track['deferred'] is True and track['fires'] == 0
    track, dues = run([(t0 + 61, sample(3000.0, 3001.0))], track=track)
    assert dues == [t0 + 61] and track['deferred'] is False and track['fires'] == 1


def test_not_deferred_before_it_is_due():
    track, _ = run(held(E + 900, 19, 3000.0, 3001.0, window=False))
    assert track['deferred'] is False


def test_the_second_attempt_needs_a_minute():
    t0 = E + 900
    track, dues = run(held(t0, 20 + 59, 3000.0, 3001.0))
    assert dues == [t0 + 20]
    track, dues = run([(t0 + 20 + 59.9, sample(3000.0, 3001.0)), (t0 + 80, sample(3000.0, 3001.0))], track=track)
    assert dues == [t0 + 80]


def test_a_deferred_second_attempt_still_waits_its_minute():
    t0 = E + 900
    track, dues = run(held(t0, 20, 3000.0, 3001.0))
    track, more = run(held(t0 + 21, 100, 3000.0, 3001.0, idle=0), track=track)    # the viewer keeps pressing keys
    assert dues == [t0 + 20] and more == [] and track['deferred'] is True
    track, more = run([(t0 + 122, sample(3000.0, 3001.0))], track=track)
    assert more == [t0 + 122] and track['fires'] == 2


def test_a_seek_back_starts_a_new_pass():
    """After both attempts, the viewer seeks back two minutes and plays to the end again: two
    more attempts, counted from when the end is read again."""
    t0 = E + 900
    track, dues = run(held(t0, 200, 3000.0, 3001.0))
    assert len(dues) == 2
    back = [(t0 + 201 + i, sample(2880.0 + i, 3001.0)) for i in range(120)]      # 2880 .. 2999
    stuck_at = t0 + 321                                                            # 3000 again
    track, more = run(back + held(stuck_at, 100, 3000.0, 3001.0), track=track)
    assert more == [stuck_at + rh.STALL_S, stuck_at + rh.STALL_S + rh.RETRY_S]


@pytest.mark.parametrize('before_end, new_pass', [(31.0, False), (60.0, False), (60.5, True), (121.0, True)])
def test_only_a_seek_back_of_more_than_a_minute_starts_a_new_pass(before_end, new_pass):
    """A pass ends when the position goes more than REARM_S before the end of the stream; a
    smaller jump back (still more than the end slack) keeps the two attempts used."""
    t0 = E + 900
    track, dues = run(held(t0, 200, 3000.0, 3001.0))
    assert len(dues) == 2
    start = 3001.0 - before_end
    n = int(3000.0 - start)
    replay = [(t0 + 201 + i, sample(start + i, 3001.0)) for i in range(n)]
    track, more = run(replay + held(t0 + 201 + n, 40, 3000.0, 3001.0), track=track)
    assert bool(more) is new_pass


def test_a_new_pass_clears_a_deferred_wait():
    t0 = E + 900
    track, _ = run(held(t0, 60, 3000.0, 3001.0, window=False))
    assert track['deferred'] is True
    track, _ = run([(t0 + 61, sample(1000.0, 3001.0, window=False))], track=track)
    assert track['deferred'] is False and track['fires'] == 0


def test_the_track_keeps_its_own_fields():
    track = dict(rh.new_track(9, E), seen=E - 5, lookup=True)
    track, _ = run(held(E + 900, 30, 3000.0, 3001.0), track=track)
    assert (track['epoch'], track['seen'], track['lookup']) == (9, E - 5, True)


# ================================================================ autoplay_on


@pytest.mark.parametrize('value, expected', [
    ('true', True), ('True', True), (' TRUE ', True), ('true\n', True),
    ('false', False), ('', False), ('1', False), ('yes', False), (None, False), (True, False), (1, False),
])
def test_autoplay_on(value, expected):
    """Kodi's CVariant(true).asString() is 'true'; an item without the flag reads ''."""
    assert rh.autoplay_on(value) is expected


# ================================================================ next_broadcast


def guide(*shows):
    return [entry(bid, start, end) for bid, start, end in shows]


def test_next_broadcast_is_the_entry_after_the_frozen_show():
    rows = guide((BID - 1, S - 3600, S), (BID, S, E), (NEXT_BID, E, E + 3000), (NEXT_BID + 1, E + 3000, E + 6000))
    assert rh.next_broadcast(rows, S, E, E + 700) == {'broadcastid': NEXT_BID, 'start': E, 'end': E + 3000}


def test_next_broadcast_sorts_the_rows():
    rows = guide((NEXT_BID + 1, E + 3000, E + 6000), (NEXT_BID, E, E + 3000), (BID, S, E), (BID - 1, S - 3600, S))
    assert rh.next_broadcast(rows, S, E, E + 700)['broadcastid'] == NEXT_BID


def test_next_broadcast_takes_the_next_entry_even_after_a_gap():
    """Kodi plays the next entry in the timeline, not 'the entry starting at the end'."""
    rows = guide((BID, S, E), (NEXT_BID, E + 600, E + 3000))
    assert rh.next_broadcast(rows, S, E, E + 700)['broadcastid'] == NEXT_BID


def test_the_last_entry_means_live():
    assert rh.next_broadcast(guide((BID - 1, S - 3600, S), (BID, S, E)), S, E, E + 700) is None


@pytest.mark.parametrize('start, end', [(S, E + 60), (S - 60, E), (S + 1, E)], ids=['end-moved', 'start-moved', 'shifted'])
def test_a_changed_entry_means_live(start, end):
    """Kodi matches the playing entry by its start AND end; the guide changed: the live channel."""
    rows = guide((BID, start, end), (NEXT_BID, E + 60, E + 3000))
    assert rh.next_broadcast(rows, S, E, E + 700) is None


@pytest.mark.parametrize('next_start, plays', [(E + 700, True), (E + 701, False), (E + 3600, False)],
                         ids=['started-now', 'a-second-later', 'an-hour-later'])
def test_a_next_entry_that_has_not_started_means_live(next_start, plays):
    """The one difference from Kodi: 'vod' catch-up reports even future entries as playable."""
    rows = guide((BID, S, E), (NEXT_BID, next_start, next_start + 1800))
    found = rh.next_broadcast(rows, S, E, E + 700)
    assert (found is not None) is plays


def test_next_broadcast_skips_bad_rows():
    rows = [None, 'x', {'broadcastid': NEXT_BID + 5, 'starttime': '', 'endtime': utc(E)},
            {'broadcastid': True, 'starttime': utc(E), 'endtime': utc(E + 60)},
            {'broadcastid': '7', 'starttime': utc(E), 'endtime': utc(E + 60)}] + guide((BID, S, E), (NEXT_BID, E, E + 3000))
    assert rh.next_broadcast(rows, S, E, E + 700)['broadcastid'] == NEXT_BID


def test_duplicate_entries_follow_kodis_first_match():
    """Kodi keys a channel's entries by start time, so this can't come from it; the rule stays
    Kodi's anyway: the first match, then the entry right after it."""
    rows = guide((BID, S, E), (BID + 7, S, E), (NEXT_BID, E, E + 3000))
    assert rh.next_broadcast(rows, S, E, E + 700)['broadcastid'] == BID + 7


@pytest.mark.parametrize('rows', [[], None, 'x'])
def test_no_guide_means_live(rows):
    assert rh.next_broadcast(rows, S, E, E + 700) is None


def test_next_broadcast_in_every_time_zone(clock_zone):
    s, e = clock_zone - 3600, clock_zone - 600
    rows = guide((BID, s, e), (NEXT_BID, e, e + 1800))
    assert rh.next_broadcast(rows, s, e, clock_zone) == {'broadcastid': NEXT_BID, 'start': e, 'end': e + 1800}


# ================================================================ after_open

AFTER_OPEN = [
    ('broadcast', 'started', None), ('broadcast', 'other', None), ('broadcast', 'gave_up', None),
    ('broadcast', 'failed', None),              # gone without an error: nothing plays, like imports.62
    ('broadcast', 'shutdown', None),
    ('broadcast', 'not_opened', 'live'), ('broadcast', 'refused', 'live'), ('broadcast', 'retune', 'live'),
    ('channel', 'started', None), ('channel', 'other', None), ('channel', 'gave_up', None),
    ('channel', 'failed', None), ('channel', 'shutdown', None), ('channel', 'retune', None),
    ('channel', 'not_opened', 'stop'), ('channel', 'refused', 'stop'),
    ('other', 'refused', None), (None, 'not_opened', None),
]


@pytest.mark.parametrize('kind, outcome, expected', AFTER_OPEN)
def test_after_open(kind, outcome, expected):
    assert rh.after_open(kind, outcome) == expected


# ================================================================ the 2026-11-15 case, chained


def test_the_2026_11_15_case_through_the_helpers():
    """The lookup 5 s after the RESTART finds the show on air; the freeze is due once; Kodi's
    flag was set; the next guide entry has started: the next show."""
    t_open = S + 600
    channels = [row(cid=12, bid=5, start=S - 60, end=E + 60), row()]
    facts = rh.programme(channels, rh.playing_broadcast({'type': 'unknown', 'id': BID}), t_open + rh.ARM_DELAY_S, decode)
    assert facts['channelid'] == CID
    stuck = t_open + 6053
    _, dues = run(timeline(growing(t_open), t_open + rh.ARM_DELAY_S, stuck + 30), end=facts['end'])
    assert dues == [stuck + 20]
    assert rh.autoplay_on('true')
    rows = guide((BID, S, E), (NEXT_BID, E, E + 3600), (NEXT_BID + 1, E + 3600, E + 7200))
    assert rh.next_broadcast(rows, facts['start'], facts['end'], dues[0])['broadcastid'] == NEXT_BID
