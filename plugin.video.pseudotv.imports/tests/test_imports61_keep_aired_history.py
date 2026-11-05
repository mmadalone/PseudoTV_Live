"""imports.61 (A): rebuilding one of your own channels keeps its aired programmes.

Own (Custom) channels are catchup="vod" without catchup-days, which is IPTV
Simple's "Channel K": a guide entry can be replayed only while pseudotv.xml
still holds it with its catchup-id. Kodi keeps showing past entries from its
own copy of the guide. When a channel counted as changed (a playlist edit, a
dashboard edit, Rebuild, and before imports.61 every add-on update), the
Builder deleted the channel's whole guide, aired programmes included
(__clrStation -> xmltvs.delBroadcast), so nothing aired before the rebuild could
be replayed.

Now the rebuild keeps every programme that started before the rebuild point
(including the one on air at that point) unchanged, and the new schedule starts
right after that programme: no hole, no overlap. A failed rebuild still
restores exactly the pre-build state.
"""
import ast
import calendar
import copy
import io
import os
import re
import time
import types

import pytest
import xbmc

import renderers
import replay_helpers as rh
import xmltv
from builder import Builder
from m3u import M3U
from xmltvs import XMLTVS

HERE = os.path.dirname(os.path.abspath(__file__))
LIB  = os.path.normpath(os.path.join(HERE, '..', 'resources', 'lib'))

X = '3330312e582d46696c65732e3c6d6435@PseudoTV_Live'     # X-Files' channel id
Y = 'TVE@movistarplus'
R = '0123456789abcdef'                                     # 16-character id


def P(ch, start, stop, cid='', title='t'):
    return {'channel': ch, 'start': start, 'stop': stop, 'catchup-id': cid, 'title': [(title, '')]}


def E(text):
    return rh.xmltv_to_epoch(text)


@pytest.fixture
def utc():
    """Pin the process time zone (xmltv_to_epoch reads suffix-less times as local)."""
    old = os.environ.get('TZ')
    os.environ['TZ'] = 'UTC'
    time.tzset()
    yield
    if old is None: os.environ.pop('TZ', None)
    else:           os.environ['TZ'] = old
    time.tzset()


# --- split_for_rebuild -------------------------------------------------------------------

CUT = E('20261104180000 +0100')                            # 17:00 UTC
A   = P(X, '20261104170000 +0100', '20261104174000 +0100', 'a')
B   = P(X, '20261104174000 +0100', '20261104182500 +0100', 'b')   # on air at the cut
C   = P(X, '20261104182500 +0100', '20261104191000 +0100', 'c')
YP  = P(Y, '20261104173000 +0100', '20261104190000 +0100', 'y')


def test_keeps_what_started_before_the_cut_including_the_show_on_air():
    out, kept, removed = rh.split_for_rebuild([A, YP, B, C], X, CUT)
    assert out == [A, YP, B] and (kept, removed) == (2, 1)
    assert out[0] is A and out[2] is B                     # untouched objects, not copies


def test_a_show_starting_exactly_at_the_cut_is_replaced():
    D = P(X, '20261104180000 +0100', '20261104184500 +0100', 'd')
    out, kept, removed = rh.split_for_rebuild([A, D], X, CUT)
    assert out == [A] and (kept, removed) == (1, 1)
    assert rh.rebuild_resume([A, D], X, CUT) == CUT           # and it doesn't delay the new line-up


@pytest.mark.parametrize('bad', [
    P(X, 'garbage', '20261104174000 +0100'), P(X, '20261104170000 +0100', ''), P(X, None, None),
    P(X, '20261104170000 +01', '20261104174000 +0100'), P(X, '20261340170000 +0100', '20261104174000 +0100'),
])
def test_unparseable_programmes_of_the_channel_are_dropped(bad):
    out, kept, removed = rh.split_for_rebuild([bad, A], X, CUT)
    assert out == [A] and (kept, removed) == (1, 1)


def test_other_channels_and_odd_items_stay_in_order():
    other_bad = P(Y, 'garbage', 'garbage')
    rec = P(R, '20261104190000 +0100', '20261104200000 +0100')
    items = [YP, 'not a dict', other_bad, C, rec, None, A]
    out, kept, removed = rh.split_for_rebuild(items, X, CUT)
    assert out == [YP, 'not a dict', other_bad, rec, None, A] and (kept, removed) == (1, 1)


def test_times_are_compared_as_times_not_as_text():
    """Text says before / after, the real time says the opposite."""
    late  = P(X, '20261104175500 +0000', '20261104184000 +0000')   # text 17:55 < 18:00, but 17:55 UTC is after 17:00 UTC
    early = P(X, '20261104183000 +0200', '20261104190000 +0200')   # text 18:30 > 18:00, but 16:30 UTC is before 17:00 UTC
    out, kept, removed = rh.split_for_rebuild([late, early], X, CUT)
    assert out == [early] and (kept, removed) == (1, 1)


def test_legacy_times_without_suffix_are_local(utc):
    old = P(X, '20261104165000', '20261104173000')           # local = UTC here: 16:50 < 17:00
    new = P(X, '20261104171000', '20261104175000')
    out, _, _ = rh.split_for_rebuild([old, new], X, CUT)
    assert out == [old]


def test_empty_and_missing_guides():
    assert rh.split_for_rebuild([], X, CUT) == ([], 0, 0)
    assert rh.split_for_rebuild(None, X, CUT) == ([], 0, 0)


# --- rebuild_resume ----------------------------------------------------------------------

def test_resume_after_the_show_on_air():
    assert rh.rebuild_resume([A, B, C, YP], X, CUT) == E(B['stop'])


def test_resume_at_the_cut_when_nothing_spans_it():
    ends_at_cut = P(X, '20261104171500 +0100', '20261104180000 +0100')
    assert rh.rebuild_resume([A, ends_at_cut], X, CUT) == CUT
    assert rh.rebuild_resume([], X, CUT) == CUT
    assert rh.rebuild_resume([C], X, CUT) == CUT               # only shows after the cut
    stale = P(X, '20261104120000 +0100', '20261104130000 +0100')
    assert rh.rebuild_resume([stale], X, CUT) == CUT           # guide ended long before the cut


def test_resume_takes_the_latest_stop_and_ignores_bad_rows():
    long_one = P(X, '20261104173000 +0100', '20261104190000 +0100')
    assert rh.rebuild_resume([B, long_one, P(X, 'x', 'y'), YP], X, CUT) == E(long_one['stop'])


def test_resume_is_never_before_the_cut():
    for progs in ([A], [A, B], [B, C], [YP], [P(X, 'x', 'y')]):
        assert rh.rebuild_resume(progs, X, CUT) >= CUT


# --- XMLTVS.trimBroadcast / resumeAfter -------------------------------------------------------

def _xmltvs(programmes, channels=None):
    x = XMLTVS.__new__(XMLTVS)
    x.m3u = None
    x.XMLTVDATA = {'data': {}, 'channels': channels if channels is not None else [{'id': X}, {'id': Y}],
                   'programmes': programmes, 'recordings': [{'id': R}]}
    x.lines = []
    x.log = lambda msg, level=xbmc.LOGDEBUG: x.lines.append((msg, level))
    return x


def test_trim_broadcast_uses_the_helper_and_leaves_channels_alone():
    x = _xmltvs([A, YP, B, C])
    channels, recordings = list(x.XMLTVDATA['channels']), list(x.XMLTVDATA['recordings'])
    assert x.trimBroadcast({'id': X}, CUT) is True
    assert x.XMLTVDATA['programmes'] == [A, YP, B]
    assert x.XMLTVDATA['channels'] == channels and x.XMLTVDATA['recordings'] == recordings
    info = [m for m, level in x.lines if level == xbmc.LOGINFO]
    assert len(info) == 1 and 'trimBroadcast' in info[0] and 'kept 2 aired, removed 1' in info[0]
    assert x.resumeAfter({'id': X}, CUT) == E(B['stop'])


def test_trim_broadcast_reports_success_without_programmes():
    x = _xmltvs([])
    assert x.trimBroadcast({'id': X}, CUT) is True              # builder.py clears 'changed' on True
    assert x.XMLTVDATA['programmes'] == []
    assert x.resumeAfter({'id': X}, CUT) == CUT


def test_channel_delete_still_removes_everything():
    x = _xmltvs([A, YP, B, C])
    assert x.delBroadcast({'id': X}) is True
    assert x.XMLTVDATA['programmes'] == [YP] and x.XMLTVDATA['channels'] == [{'id': Y}]


def _chan(cid, name):
    return {'id': cid, 'display-name': [(name, 'en')], 'icon': [{'src': ''}]}


def test_channel_upsert_replaces_the_right_element_in_an_unsorted_list():
    """After a save in the same build call the channel list is no longer sorted by
    name. The upsert looked the channel up in a sorted copy and wrote into the real
    list, so it overwrote another channel's element (that channel's guide was then
    pruned as an orphan) and duplicated this one. Rebuilds keep the element now, so
    every rebuild takes this path."""
    x = _xmltvs([], channels=[_chan('b', 'Bravo'), _chan('c', 'Charlie'), _chan('a', 'Alpha')])
    x.addChannel({'id': 'c', 'name': 'Charlie 2', 'logo': 'c.png'})
    x.addChannel({'id': 'a', 'name': 'Alpha 2', 'logo': 'a.png'})
    ids = [c['id'] for c in x.XMLTVDATA['channels']]
    assert ids == ['b', 'c', 'a']                              # nothing lost, nothing duplicated, order kept
    names = {c['id']: c['display-name'][0][0] for c in x.XMLTVDATA['channels']}
    assert names == {'b': 'Bravo', 'c': 'Charlie 2', 'a': 'Alpha 2'}


def test_channel_upsert_appends_a_new_channel():
    x = _xmltvs([], channels=[_chan('b', 'Bravo')])
    x.addChannel({'id': 'n', 'name': 'New', 'logo': ''})
    assert [c['id'] for c in x.XMLTVDATA['channels']] == ['b', 'n']
    empty = _xmltvs([], channels=[])
    empty.addChannel({'id': 'n', 'name': 'New', 'logo': ''})
    assert [c['id'] for c in empty.XMLTVDATA['channels']] == ['n']


# --- Builder._restoreChannel replaces instead of appending ------------------------------------

def _fake_builder(programmes):
    m = M3U.__new__(M3U)
    m.M3UDATA = {'stations': [{'id': X, 'name': 'X-Files', 'number': 61}, {'id': Y, 'name': 'TVE', 'number': 1}]}
    x = _xmltvs(programmes)
    return types.SimpleNamespace(m3u=m, xmltv=x, _build_snapshots={}, log=lambda *a, **k: None)


def _state(b):
    return (copy.deepcopy(b.m3u.M3UDATA['stations']), copy.deepcopy(b.xmltv.XMLTVDATA['channels']),
            copy.deepcopy(b.xmltv.XMLTVDATA['programmes']))


def test_failed_rebuild_restores_exactly_the_pre_build_state():
    b = _fake_builder([A, YP, B, C])
    citem = {'id': X}
    before = _state(b)
    Builder._snapshotChannel(b, citem)
    b.xmltv.trimBroadcast(citem, CUT)                           # the rebuild starts
    b.xmltv.XMLTVDATA['programmes'].append(P(X, B['stop'], '20261104190000 +0100', 'new'))  # partly built
    b.m3u.M3UDATA['stations'].append({'id': X, 'name': 'X-Files', 'number': 61, 'new': True})  # station upserted
    b.xmltv.XMLTVDATA['channels'].append({'id': X, 'new': True})
    Builder._restoreChannel(b, citem)                           # ... then it failed
    stations, channels, programmes = _state(b)
    key = lambda d: sorted(d.items(), key=repr)
    assert sorted(map(key, stations)) == sorted(map(key, before[0]))
    assert sorted(map(key, channels)) == sorted(map(key, before[1]))
    assert sorted(map(key, programmes)) == sorted(map(key, before[2]))
    assert sum(1 for s in stations if s['id'] == X) == 1 and sum(1 for c in channels if c['id'] == X) == 1
    assert b._build_snapshots == {}
    Builder._restoreChannel(b, citem)                           # a second restore is a no-op
    assert _state(b) == (stations, channels, programmes)


def test_restore_without_a_snapshot_changes_nothing():
    b = _fake_builder([A, YP])
    before = _state(b)
    Builder._restoreChannel(b, {'id': X})
    assert _state(b) == before


def test_restore_of_a_channel_that_had_no_station_or_channel_element():
    b = _fake_builder([YP])
    b.m3u.M3UDATA['stations'] = [s for s in b.m3u.M3UDATA['stations'] if s['id'] != X]
    b.xmltv.XMLTVDATA['channels'] = [{'id': Y}]
    before = _state(b)
    Builder._snapshotChannel(b, {'id': X})
    b.m3u.M3UDATA['stations'].append({'id': X, 'new': True})
    b.xmltv.XMLTVDATA['channels'].append({'id': X, 'new': True})
    b.xmltv.XMLTVDATA['programmes'].append(P(X, A['start'], A['stop']))
    Builder._restoreChannel(b, {'id': X})
    assert _state(b) == before


# --- end to end: real addProgram + addScheduling + render/read ------------------------------------

@pytest.fixture(params=[('UTC', (2026, 11, 4)), ('Europe/Madrid', (2026, 11, 4)), ('Europe/Madrid', (2026, 7, 1))],
                ids=['utc', 'madrid-winter', 'madrid-summer'])
def zone(request, monkeypatch):
    """Pin the process time zone and the offset the writer stamps: addProgram adds the
    CURRENT UTC offset (xmltvs._xmltvTzSuffix -> getTimeoffset), so without the pin the
    winter-dated data would get a summer suffix whenever the suite runs in summer."""
    import datetime
    import xmltvs
    name, day = request.param
    old = os.environ.get('TZ')
    os.environ['TZ'] = name
    time.tzset()
    base = calendar.timegm(day + (12, 0, 0))
    offset = int(datetime.datetime.fromtimestamp(base).astimezone().utcoffset().total_seconds())
    monkeypatch.setattr(xmltvs, 'getTimeoffset', lambda: offset)
    yield base
    if old is None: os.environ.pop('TZ', None)
    else:           os.environ['TZ'] = old
    time.tzset()


def _item(start, stop, label, cid):
    return {'categories': ['Drama'], 'title': label, 'desc': 'plot of %s' % label, 'start': start, 'stop': stop,
            'thumb': '', 'length': stop - start, 'catchup-id': cid}


def test_rebuild_keeps_aired_programmes_with_no_gap_and_no_overlap(zone):
    x = _xmltvs([])
    base = zone
    t = base
    for i, d in enumerate([2400, 2700, 3000, 2580, 2760, 3120, 2460, 2880, 2640, 2520]):
        x.addProgram(X, _item(t, t + d, 'old %d' % i, 'plugin://plugin.video.pseudotv.imports/?mode=vod&vid=old%d' % i), encodeDESC=False)
        t += d
    for h in range(10):
        x.addProgram(Y, _item(base + h * 3600, base + (h + 1) * 3600, 'tve %d' % h, 'tve%d' % h), encodeDESC=False)
    cut = base + 3 * 3600                                       # the builder's anchor (a round hour)
    before_x = copy.deepcopy([p for p in x.XMLTVDATA['programmes'] if p['channel'] == X])
    before_y = copy.deepcopy([p for p in x.XMLTVDATA['programmes'] if p['channel'] == Y])
    kept = [p for p in before_x if E(p['start']) < cut]
    spanning = kept[-1]
    assert E(spanning['start']) < cut < E(spanning['stop'])     # the data has a show on air at the cut

    x.trimBroadcast({'id': X}, cut)
    start = x.resumeAfter({'id': X}, cut)
    assert start == E(spanning['stop'])
    fake = types.SimpleNamespace(runActions=lambda action, citem, parameter=None, inherited=None: parameter,
                                 log=lambda *a, **k: None)
    new = [{'label': 'new %d' % i, 'duration': d} for i, d in enumerate([2700, 2400, 3000])]
    for it in Builder.addScheduling(fake, {'id': X}, new, cut, start):
        x.addProgram(X, _item(it['start'], it['stop'], it['label'], 'plugin://plugin.video.pseudotv.imports/?mode=vod&vid=%s' % it['label']), encodeDESC=False)

    after_x = [p for p in x.XMLTVDATA['programmes'] if p['channel'] == X]
    assert after_x[:len(kept)] == kept                          # aired programmes untouched
    assert after_x[len(kept)]['start'] == spanning['stop']      # new line-up right after the show on air
    ordered = sorted(after_x, key=lambda p: E(p['start']))
    for prev, nxt in zip(ordered, ordered[1:]):
        assert E(nxt['start']) == E(prev['stop'])               # no hole, no overlap
    assert E(after_x[len(kept)]['start']) >= cut
    assert [p for p in x.XMLTVDATA['programmes'] if p['channel'] == Y] == before_y

    x.XMLTVDATA['data'] = {'date': '20261104120000'}
    rendered = renderers.render_xmltv(x.XMLTVDATA)
    assert rendered is not None
    read = [p for p in xmltv.read_programmes(io.BytesIO(rendered)) if p['channel'] == X]
    assert [(p['start'], p['stop'], p['catchup-id']) for p in read[:len(kept)]] == \
           [(p['start'], p['stop'], p['catchup-id']) for p in kept]


# --- the builder wiring (source checks; buildChannels needs Kodi) ---------------------------------

def _builder_src():
    return open(os.path.join(LIB, 'builder.py'), encoding='utf-8').read()


def _code(text):
    return '\n'.join(line.split('#')[0].rstrip() for line in text.splitlines())


def test_rebuild_trims_instead_of_deleting_the_channel():
    code = _code(_builder_src())
    clr = re.search(r'def __clrStation\(citem: dict, cut\) -> bool:\n(.*?)\n\s+return state', code, re.S)
    assert clr, '__clrStation(citem, cut) not found'
    body = clr.group(1)
    assert 'self.xmltv.trimBroadcast(citem, cut)' in body and 'self.resetPagination(citem)' in body
    assert 'delBroadcast' not in body and 'delStation' not in body
    assert 'if __clrStation(citem, nstart):' in code
    assert 'nstart    = roundTimeDown(now,offset=60)' in code


def test_new_schedule_continues_after_the_show_on_air():
    code = _code(_builder_src())
    assert re.search(r'if _changed:\n\s+start = roundTimeDown\(now, offset=60\)\n\s+'
                     r'if self\._rebuildContinuesAfterAired\(citem\):\n\s+start = self\.xmltv\.resumeAfter\(citem, start\)\n', code)
    assert code.count('start = roundTimeDown(now, offset=60)') == 1


@pytest.mark.parametrize('citem, continues', [
    ({'rules': {'3000': {}}}, False), ({'rules': {3000: {}}}, False),       # PauseRule, text or number id
    ({'rules': {'51': {}}}, True), ({'rules': {}}, True), ({'rules': None}, True), ({}, True), (None, True),
])
def test_pause_rule_channels_keep_the_anchor(citem, continues):
    """PauseRule's BUILD_TIME_PRE clears all of the channel's programmes and lays one
    cell from the schedule start; continuing after its long kept 'paused' cell would
    leave a guide hole of days."""
    assert Builder._rebuildContinuesAfterAired(citem) is continues


def test_snapshot_is_taken_before_the_trim():
    """A failed rebuild can restore exactly the pre-build state only if the snapshot
    precedes the trim."""
    code = _code(_builder_src())
    body = code[code.index('def __hasChanged('):code.index('def __hasProgrammes(')]
    assert 0 < body.index('self._snapshotChannel(citem)') < body.index('if __clrStation(citem, nstart):')


def test_a_failed_changed_rebuild_still_counts_as_failed():
    code = _code(_builder_src())
    assert 'updated.add(False if _changed else __hasProgrammes(citem))' in code


def test_restore_filters_by_channel_before_appending():
    tree = ast.parse(_builder_src())
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == '_restoreChannel')
    src = ast.get_source_segment(_builder_src(), fn)
    assert src.index('stations[:]') < src.index('stations.append(')
    assert src.index('programmes[:]') < src.index('programmes.extend(')


@pytest.mark.parametrize('name', ['builder.py', 'xmltvs.py', 'replay_helpers.py'])
def test_changed_files_pass_the_ast_python38_check(name):
    ast.parse(open(os.path.join(LIB, name), encoding='utf-8').read(), feature_version=(3, 8))


def test_replay_helpers_stays_import_light():
    """Standard library only: no Kodi modules, no add-on modules."""
    import sys
    tree = ast.parse(open(os.path.join(LIB, 'replay_helpers.py'), encoding='utf-8').read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import): imported.update(a.name.split('.')[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom): imported.add((node.module or '').split('.')[0])
    assert imported and imported <= set(sys.stdlib_module_names), imported - set(sys.stdlib_module_names)
