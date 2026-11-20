# -*- coding: utf-8 -*-
"""imports.70: LiveTV replays frozen at their end move on -- replayend_helpers, the decisions.

On 2026-11-15 a replay of a Mega show, opened with the RESTART pop-up while the
show was still on air, froze on its last second (1:40:53 of 1:40:54) for
1 h 26 min. Movistar+ serves a show still on air as a live stream whose window
need not match the guide entry (2026-11-15: 6 of 6 measured opened a minute or
two before the guide start and closed about ten minutes after the guide end;
2026-11-19: a 69-minute stream inside a 3 h 05 guide entry, ending a minute
before the guide end); inputstream.adaptive never ends the last period of a
live stream, so Kodi only logs "stream stalled" and its "Play next programme
automatically", which runs on a natural end, never fires. The add-on now
notices the freeze and does what Kodi does at a natural end.

replayend_helpers holds every decision and imports only clock_helpers and
restart_helpers, so it runs here without Kodi stubs:
  - playing_broadcast / programme: the lookup (is the playing replay's show the
    one on air?);
  - new_track / stall_step / ready: the tick's count (position and length
    still for 20 s at the end of the stream, nothing paused or seeking; it
    waits for full-screen video and no key press);
  - autoplay_on / on_air / next_broadcast / after_open: the move (Kodi's own
    flag; the live channel while the guide lists the show as on air, else
    Kodi's next-show rule; the fallbacks);
  - target_text / outcome_note: the wording of the move's log lines.

The repair after the TV test of 2026-11-19 (the user's rule): due 20 s after the
stream froze, with no wait for the guide's end time (the first build also
required the guide end + 60 s, END_GRACE_S, now gone), and the target chosen at
the move: live while the guide lists the show as on air, else the next entry.
Its tests (H1-H6) run the frozen Mega replay of that evening, embedded below.
The Kodi side (services.py) is tested in test_imports70_replay_end.py.
"""
import ast
import calendar
import inspect
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


# imports.70 (the repair): the one table, the log lines' notes on each result word. Text only,
# and no line of the module writes to it.
READ_ONLY_TABLES = ('OUTCOME_NOTES',)


def test_module_has_no_shared_state_and_no_mutable_defaults():
    tree = _tree()
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = [ast.unparse(t) for t in (node.targets if isinstance(node, ast.Assign) else [node.target])]
            if targets[0] in READ_ONLY_TABLES:
                assert isinstance(node.value, ast.Dict), ast.unparse(node)
                assert all(isinstance(v, ast.Constant) and isinstance(v.value, str)
                           for v in node.value.keys + node.value.values), ast.unparse(node)
                continue
            assert isinstance(node.value, ast.Constant), ast.unparse(node)
    for name in READ_ONLY_TABLES:
        used = [n for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id == name]
        assert len(used) == 2 and [isinstance(n.ctx, ast.Store) for n in used].count(True) == 1, name
        parents = {c: p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
        reads = [parents[n] for n in used if isinstance(n.ctx, ast.Load)]
        assert [ast.unparse(r) for r in reads] == ['%s.get' % name], name       # only ever looked up
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            defaults = node.args.defaults + [d for d in node.args.kw_defaults if d is not None]
            assert not [d for d in defaults if not isinstance(d, ast.Constant)], node.name


def test_public_api_is_the_planned_one():
    """imports.70 (the repair): END_GRACE_S is gone (no wait for the guide end); on_air decides
    live or next at the move; target_text / OUTCOME_NOTES / outcome_note word the log lines."""
    public = {name for name in vars(rh) if not name.startswith('_')}
    assert public == {'clock_helpers', 'restart_helpers',
                      'ARM_DELAY_S', 'STALL_S', 'POS_EPS', 'END_SLACK_S', 'TICK_GAP_S', 'QUIET_S',
                      'REARM_S', 'MAX_FIRES', 'RETRY_S', 'BUSY', 'ACT_WINDOW', 'AUTOPLAY_PROPERTY',
                      'playing_broadcast', 'programme', 'new_track', 'ready', 'stall_step', 'autoplay_on',
                      'on_air', 'next_broadcast', 'after_open', 'target_text', 'OUTCOME_NOTES', 'outcome_note'}
    assert not hasattr(rh, 'END_GRACE_S')


def test_constants_are_the_planned_ones():
    assert (rh.ARM_DELAY_S, rh.STALL_S, rh.POS_EPS, rh.END_SLACK_S, rh.TICK_GAP_S) == \
        (5.0, 20.0, 0.25, 10.0, 5.0)
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


def run(ticks, track=None):
    """Feeds [(now, sample)] through stall_step -> (the last track, the times it was due)."""
    if track is None: track = rh.new_track(1, ticks[0][0] - 1)
    dues = []
    for now, s in ticks:
        kept = json.dumps(track, sort_keys=True)
        track_in = track
        track, due = rh.stall_step(track_in, s, now)
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


# Two anchors for the boundary tests: well before the frozen show's guide end and well after it.
# The guide end is no gate (imports.70, the repair), so each boundary holds at both.
ANCHORS = pytest.mark.parametrize('t0', [S + 600, E + 900], ids=['during-the-show', 'after-the-show'])


@ANCHORS
@pytest.mark.parametrize('left, fires', [(1.0, True), (10.0, True), (10.01, False), (11.0, False), (300.0, False)])
def test_only_at_the_end_of_the_stream(left, fires, t0):
    ticks = held(t0, 40, 6054.0 - left, 6054.0)
    assert bool(run(ticks)[1]) is fires


@pytest.mark.parametrize('frozen_from', [E - 3600, E - 300, E - 21, E - 20, E - 1, E, E + 30, E + 50, E + 3600],
                         ids=['an-hour-before', '5-min-before', '21-s-before', '20-s-before', '1-s-before', 'at-the-end',
                              '30-s-after', '50-s-after', 'an-hour-after'])
def test_the_guide_end_is_no_gate(frozen_from):
    """imports.70 (the repair after the 2026-11-19 TV test; the user's rule): due exactly 20 s after
    the stream stood still, wherever that is from the guide end. The first build waited for the
    guide end + 60 s as well (END_GRACE_S): on 2026-11-19 the Mega stream froze 28 s before its
    guide end, and that rule would only have acted after 88 s of frozen picture."""
    ticks = held(frozen_from, 25, 3000.0, 3001.0)
    assert run(ticks)[1][:1] == [frozen_from + rh.STALL_S]


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


@ANCHORS
@pytest.mark.parametrize('held_for, fires', [(19.9, False), (20.0, True)])
def test_the_twenty_second_boundary(held_for, fires, t0):
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
        track, due = rh.stall_step(track, None, E + 900 + i)
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


@ANCHORS
def test_the_second_attempt_needs_a_minute(t0):
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


def test_stall_step_takes_no_guide_time():
    """H1. imports.70 (the repair): the guide end left the freeze detection; only the move reads the
    guide (on_air, next_broadcast)."""
    assert list(inspect.signature(rh.stall_step).parameters) == ['track', 'sample', 'now']
    text = ast.unparse([n for n in _tree().body if isinstance(n, ast.FunctionDef) and n.name == 'stall_step'][0])
    assert 'end' not in [n.id for n in ast.walk(ast.parse(text)) if isinstance(n, ast.Name)]


def test_limit_a_freeze_far_from_the_declared_length_never_moves_on():
    """H5, a known limit (FORK_NOTES): a stream that stops growing well short of the length it
    declared (a Telecinco-type window, 2026-11-19: 10620 s declared, an hour past the guide end;
    how it ends is unmeasured) is not at its end: 10 minutes at 8000 of 10620 s are never due."""
    track, dues = run(held(E + 900, 600, 8000.0, 10620.0))
    assert dues == [] and track['fires'] == 0 and track['deferred'] is False


def test_the_telecinco_live_edge_never_fires():
    """H6. 2026-11-19, Telecinco (the recording, 17:49:43-17:51:58): a seek to the end was clamped
    to the live edge, where the position moves at 1x (6662 -> 6799 s) under a fixed declared
    length of 10620 s. A moving position is never a freeze."""
    t0 = calendar.timegm((2026, 11, 19, 16, 49, 43))
    ticks = [(t0 + i, sample(6662.0 + i, 10620.0)) for i in range(138)]
    assert run(ticks)[1] == []


# ================================================================ autoplay_on


@pytest.mark.parametrize('value, expected', [
    ('true', True), ('True', True), (' TRUE ', True), ('true\n', True),
    ('false', False), ('', False), ('1', False), ('yes', False), (None, False), (True, False), (1, False),
])
def test_autoplay_on(value, expected):
    """Kodi's CVariant(true).asString() is 'true'; an item without the flag reads ''."""
    assert rh.autoplay_on(value) is expected


# ================================================================ on_air


@pytest.mark.parametrize('end, now, expected', [
    (E, S, True), (E, E - 3600, True), (E, E - 1, True), (E, E - 0.001, True),
    (E, E, False), (E, E + 0.001, False), (E, E + 3600, False),
    (float(E), E - 1, True), (float(E), E, False),
    (None, E - 1, False), (True, 0, False), (str(E), E - 1, False), ([E], E - 1, False),
], ids=['at-its-start', 'an-hour-before', '1-s-before', '1-ms-before', 'at-the-end', '1-ms-after', 'an-hour-after',
        'float-before', 'float-at-the-end', 'no-end', 'bool-end', 'text-end', 'list-end'])
def test_on_air(end, now, expected):
    """imports.70 (the repair): the guide still lists the frozen show as on air while now < its
    guide end (the end the lookup read). An unusable end counts as over: then Kodi's next-show rule
    decides, which can't match it either (live)."""
    assert rh.on_air(end, now) is expected


# ================================================================ next_broadcast


def guide(*shows):
    return [entry(bid, start, end) for bid, start, end in shows]


class Unread(object):
    """PVR.GetBroadcasts rows that must not be looked at."""

    def __iter__(self):
        raise AssertionError('the guide rows were read')


@pytest.mark.parametrize('now', [S, S + 1, E - 600, E - 1, E - 0.001])
def test_while_the_show_is_on_air_the_live_channel_whatever_the_rows_say(now):
    """imports.70 (the repair; the user's rule of 2026-11-19): a replay frozen while the guide still
    lists its show as on air goes to the live channel, before any row is looked at; even a next entry
    that has started already (an overlap) is not played then."""
    assert rh.next_broadcast(Unread(), S, E, now) is None
    rows = guide((BID, S, E), (NEXT_BID, S + 1, E + 3000))
    assert rh.next_broadcast(rows, S, E, now) is None


def move_target(rows, start, end, now, flag='true'):
    """What the move opens, as services.Player._runReplayEnd chooses it from the helpers: 'stop'
    (Kodi's flag off), 'live' or the next show's broadcast id."""
    if not rh.autoplay_on(flag): return 'stop'
    nxt = rh.next_broadcast(rows, start, end, now)
    return nxt['broadcastid'] if nxt else 'live'


MOVE_TARGETS = [
    # (case, rows, now, Kodi's flag, target)
    ('flag off, during the show', 'unread', E - 30, '', 'stop'),
    ('flag off, at the end', 'next', E, '', 'stop'),
    ('flag off, after the end', 'next', E + 300, 'false', 'stop'),
    ('on air', 'unread', E - 30, 'true', 'live'),
    ('on air, a second before the end', 'unread', E - 1, 'true', 'live'),
    ('at the end, next started', 'next', E, 'true', NEXT_BID),
    ('at the end, next a second later', 'next+1', E, 'true', 'live'),
    ('over, next started', 'next', E + 300, 'true', NEXT_BID),
    ('over, no match', 'changed', E + 300, 'true', 'live'),
    ('over, the last entry', 'last', E + 300, 'true', 'live'),
    ('over, no guide', 'none', E + 300, 'true', 'live'),
    ('on air, the next entry overlaps', 'overlap', E - 30, 'true', 'live'),
    ('over, the next entry overlapped', 'overlap', E, 'true', NEXT_BID),
]
ROWS = {'unread': Unread, 'next': lambda: guide((BID, S, E), (NEXT_BID, E, E + 3000)),
        'next+1': lambda: guide((BID, S, E), (NEXT_BID, E + 1, E + 3000)),
        'changed': lambda: guide((BID, S, E + 60), (NEXT_BID, E + 60, E + 3000)),
        'last': lambda: guide((BID - 1, S - 3600, S), (BID, S, E)), 'none': lambda: [],
        'overlap': lambda: guide((BID, S, E), (NEXT_BID, E - 600, E + 3000))}


@pytest.mark.parametrize('case, rows, now, flag, expected', MOVE_TARGETS, ids=[c[0] for c in MOVE_TARGETS])
def test_the_move_target(case, rows, now, flag, expected):
    """H4. Where the move goes, by the guide at the moment of the move: Kodi's flag off -> stop;
    the guide lists the show as on air -> live, rows unread; it is over -> Kodi's next-show rule
    (the next entry when it has started, else live)."""
    assert move_target(ROWS[rows](), S, E, now, flag) == expected


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


# ================================================================ the log lines' wording (imports.70, the repair)


def test_target_text():
    assert rh.target_text(('broadcast', NEXT_BID)) == 'the next show, broadcast %s' % NEXT_BID
    assert rh.target_text(('channel', CID)) == 'live channel %s' % CID


def _returned(node):
    """The words a `return` hands back: a text, or either side of `a if test else b`."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str): return {node.value}
    if isinstance(node, ast.IfExp): return _returned(node.body).union(_returned(node.orelse))
    return set()


def _watch_outcome_words():
    """Every word restart_helpers.watch_outcome returns, read from its source."""
    with open(os.path.join(ADDON_LIB, 'restart_helpers.py'), encoding='utf-8') as f: tree = ast.parse(f.read())
    node = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'watch_outcome'][0]
    returns = [r for r in ast.walk(node) if isinstance(r, ast.Return)]
    assert all(_returned(r.value) for r in returns)                     # every return hands back a word
    return set().union(*[_returned(r.value) for r in returns])


def test_every_result_word_has_its_note():
    """services.Player._replayEndOpen returns 'not_opened', a watch_outcome word (never 'wait': it
    polls on) or 'shutdown'; each one has its few words for the log line, and nothing else does."""
    words = _watch_outcome_words()
    assert 'wait' in words and 'started' in words
    assert set(rh.OUTCOME_NOTES) == (words - {'wait'}).union({'not_opened', 'shutdown'})


@pytest.mark.parametrize('word', sorted(rh.OUTCOME_NOTES))
def test_outcome_note(word):
    note = rh.OUTCOME_NOTES[word]
    assert rh.outcome_note(word) == '%s (%s)' % (word, note)
    assert note and note == note.strip() and '%' not in note and '://' not in note


def test_outcome_note_of_an_unknown_word_is_the_word():
    assert rh.outcome_note('wait') == 'wait' and rh.outcome_note(None) == 'None' and rh.outcome_note('') == ''


def test_the_notes_say_what_the_watch_does():
    """The notes quote the watch's own rule (restart_helpers): it gives up after WATCH_TIMEOUT s."""
    import restart_helpers
    assert 'after %d s' % restart_helpers.WATCH_TIMEOUT in rh.OUTCOME_NOTES['gave_up']


# ================================================================ the 2026-11-15 case, chained


def test_the_2026_11_15_case_through_the_helpers():
    """The lookup 5 s after the RESTART finds the show on air; the freeze is due once; Kodi's
    flag was set; at the move the guide entry is over and the next one has started: the next
    show."""
    t_open = S + 600
    channels = [row(cid=12, bid=5, start=S - 60, end=E + 60), row()]
    facts = rh.programme(channels, rh.playing_broadcast({'type': 'unknown', 'id': BID}), t_open + rh.ARM_DELAY_S, decode)
    assert facts['channelid'] == CID
    stuck = t_open + 6053
    _, dues = run(timeline(growing(t_open), t_open + rh.ARM_DELAY_S, stuck + 30))
    assert dues == [stuck + 20]
    assert rh.autoplay_on('true') and not rh.on_air(facts['end'], dues[0])
    rows = guide((BID, S, E), (NEXT_BID, E, E + 3600), (NEXT_BID + 1, E + 3600, E + 7200))
    assert rh.next_broadcast(rows, facts['start'], facts['end'], dues[0])['broadcastid'] == NEXT_BID


# ================================================================ the 2026-11-19 case (the TV test)
# imports.70 (the repair). The values below are copied from the TV test's 1 Hz recording (read-only
# JSON-RPC, 2026-11-19), so the test reads no file:
#   ~/.claude/plans/never-synced/replay-end-build/tvtest-2026-11-19/monitor.log
# Mega, broadcast 883321 (guide 13:55:00-17:00:00 UTC, the next entry 17:00:00-18:45:00), channel
# 424. The recorder sampled about twice a second: series 1 is the first sample of each second
# (lines 963-1005, odd), series 2 the second (lines 964-1006, even). Every sample: not paused, not
# seeking, full-screen video, no dialog, Kodi idle for 10 s or more (b = 00000100101); then the
# freeze, lines 1007-1516: 510 identical samples from 17:59:32 to 18:03:46, 4133.5 of 4134.0 s
# (b = 10000100101: Player.Caching on, the rest the same), until the live channel was opened by hand.
MEGA_BASE = calendar.timegm((2026, 11, 19, 16, 59, 0))          # 17:59:00 local
MEGA_GUIDE_END = calendar.timegm((2026, 11, 19, 17, 0, 0))     # 18:00:00 local = MEGA_BASE + 60
MEGA_MOVING = {
    1: [(10, 4112.5, 4140.0), (11, 4113.3, 4140.0), (12, 4114.3, 4140.0), (13, 4115.4, 4140.0),
        (14, 4116.3, 4140.0), (15, 4117.3, 4140.0), (16, 4118.2, 4134.0), (17, 4119.4, 4134.0),
        (18, 4120.4, 4134.0), (19, 4121.5, 4134.0), (20, 4122.3, 4134.0), (21, 4123.3, 4134.0),
        (22, 4124.4, 4134.0), (23, 4125.2, 4134.0), (24, 4126.3, 4134.0), (25, 4127.3, 4134.0),
        (26, 4128.3, 4134.0), (27, 4129.3, 4134.0), (28, 4130.3, 4134.0), (29, 4131.3, 4134.0),
        (30, 4132.3, 4134.0), (31, 4133.3, 4134.0)],
    2: [(10, 4112.5, 4140.0), (11, 4113.6, 4140.0), (12, 4114.3, 4140.0), (13, 4115.4, 4140.0),
        (14, 4116.4, 4140.0), (15, 4117.4, 4140.0), (16, 4118.6, 4134.0), (17, 4119.5, 4134.0),
        (18, 4120.7, 4134.0), (19, 4121.5, 4134.0), (20, 4122.3, 4134.0), (21, 4123.3, 4134.0),
        (22, 4124.4, 4134.0), (23, 4125.4, 4134.0), (24, 4126.5, 4134.0), (25, 4127.5, 4134.0),
        (26, 4128.5, 4134.0), (27, 4129.5, 4134.0), (28, 4130.5, 4134.0), (29, 4131.5, 4134.0),
        (30, 4132.5, 4134.0), (31, 4133.5, 4134.0)],
}
MEGA_FROZEN = [(s, 4133.5, 4134.0) for s in range(32, 287)]
# the recorder's booleans -> the tick's sample: busy (bits 2-5 off), window (6 on, 7-8 off), idle (11 on)
MEGA_SAMPLE = {'busy': False, 'window': True, 'idle': 10}


def mega_ticks(series):
    return [(MEGA_BASE + s, dict(MEGA_SAMPLE, pos=pos, total=total)) for s, pos, total in MEGA_MOVING[series] + MEGA_FROZEN]


def mega_rows(guide_end):
    """PVR.GetBroadcasts of channel 424 around it: the Mega entry, then the next one from its end."""
    start = calendar.timegm((2026, 11, 19, 13, 55, 0))
    return guide((883321, start, guide_end), (883322, guide_end, guide_end + 6300))


@pytest.mark.parametrize('series', [1, 2])
def test_the_mega_recording_fires_twenty_seconds_after_the_freeze_before_the_guide_end(series):
    """H2. The TV test of 2026-11-19, sample by sample: due 20 s after the position first stood
    still (17:59:31, the first sample within 0.25 s of 4133.5), at 17:59:51, nine seconds before the
    guide end; the second attempt a minute later. At the move the guide still lists the show as on
    air: the live channel. The first build's rule (guide end + 60 s) would have waited until
    18:01:00 (the README of that evening), and then hung (FORK_NOTES)."""
    track, dues = run(mega_ticks(series))
    assert dues == [MEGA_BASE + 51, MEGA_BASE + 111]
    assert dues[0] < MEGA_GUIDE_END == MEGA_BASE + 60
    assert (track['pos'], track['total'], track['fires']) == (4133.5, 4134.0, rh.MAX_FIRES)
    start = calendar.timegm((2026, 11, 19, 13, 55, 0))
    assert rh.on_air(MEGA_GUIDE_END, dues[0])
    assert move_target(mega_rows(MEGA_GUIDE_END), start, MEGA_GUIDE_END, dues[0]) == 'live'
    assert move_target(mega_rows(MEGA_GUIDE_END), start, MEGA_GUIDE_END, dues[1]) == 883322     # the second attempt


@pytest.mark.parametrize('series', [1, 2])
def test_the_same_freeze_five_minutes_after_the_guide_end_means_the_next_show(series):
    """H3. The same samples with the guide end 300 s before the first frozen one (a viewer further
    behind live): the same fire time, and the move plays the next entry."""
    guide_end = MEGA_BASE + 32 - 300
    _, dues = run(mega_ticks(series))
    assert dues[:1] == [MEGA_BASE + 51]
    start = calendar.timegm((2026, 11, 19, 13, 55, 0))
    assert not rh.on_air(guide_end, dues[0])
    assert move_target(mega_rows(guide_end), start, guide_end, dues[0]) == 883322
