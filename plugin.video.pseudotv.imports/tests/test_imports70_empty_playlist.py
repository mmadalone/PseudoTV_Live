# -*- coding: utf-8 -*-
"""imports.70: a playlist that answers with no channels is a warning, not a
failure.

Since imports.25, syncOne keeps an import's channels when its playlist reads
as empty (200 with zero channels, G24) and stores last_status 'warning'. But
the result it returned still said 'failed' (its starting value), so syncAll's
imports.28 counter step raised fail_count and started failed_since -- against
its own rule that a 'warning' leaves them alone. The abandon clock and the
retry backoff of a later real failure then counted from the empty read.

imports.70: the result says 'warning' too. syncAll leaves fail_count and
failed_since as they were; like 'failed', 'warning' keeps the import out of
the cycle's allocation, links read and guide merge, so its channels keep
their records, numbers, playlist entries and guide. A later real failure
counts as before.

Runs the real syncAll on the real Channels over a channels.json in tmp_path:
imports.69's rig (test_imports69_sync_commit: its `env` fixture is autouse
there and here, taken with request.getfixturevalue so no test argument
shadows the import). All data is synthetic.
"""
import time

import xbmc

from test_imports69_sync_commit import (  # noqa: F401 -- env: imports.69's autouse rig, used by name
    DOWN, NOW, P1, _XMLTV, cfg, custom, disk, entry, env, logged, ok, playlist, rec, sync, write)


EMPTY = ok(b'#EXTM3U\n')
FAILING = {'last_status': 'failed', 'fail_count': 2, 'failed_since': NOW - 7200}


def _ts(epoch):
    return time.strftime('%Y%m%d%H%M%S', time.gmtime(epoch)) + ' +0000'


def _setup(request, **prior):
    rig = request.getfixturevalue('env')
    write(rig, [cfg('one', **prior), cfg('two', start_num=100)],
          [custom(), rec('A1', 'one', 11), rec('A2', 'one', 12), rec('B1', 'two', 100)])
    rig.source.answer(P1, EMPTY)
    rig.source.answer('https://example.invalid/two.m3u', ok(playlist(('B1', 'Beta', 100))))
    return rig


def test_an_empty_playlist_leaves_the_failure_counters_alone(request):
    rig = _setup(request)
    run = sync(rig)
    assert run.res['one']['status'] == 'warning'
    saved = entry(rig, 'one')
    assert saved['last_status'] == 'warning'
    assert saved['last_error'] == 'M3U parse yielded zero channels (kept previous)'
    assert 'fail_count' not in saved and 'failed_since' not in saved
    assert saved['last_sync_at'] >= NOW
    assert logged(rig, 'syncAll, [one] status=warning new=0 refreshed=0 orphan=0 epg=0', xbmc.LOGINFO)
    assert run.res['two']['status'] == 'ok' and entry(rig, 'two')['fail_count'] == 0


def test_an_empty_playlist_alone_is_no_disk_change(request):
    """Like 'failed', a 'warning' changes nothing on disk: a cycle whose only
    import read an empty playlist signals no PVR refresh (imports.22)."""
    rig = request.getfixturevalue('env')
    write(rig, [cfg('one')], [custom(), rec('A1', 'one', 11), rec('A2', 'one', 12)])
    rig.source.answer(P1, EMPTY)
    run = sync(rig)
    assert run.res['one']['status'] == 'warning'
    assert 'chkPVRRefresh' not in rig.timers
    assert logged(rig, 'syncAll, no disk change this cycle', xbmc.LOGINFO)


def test_an_empty_playlist_during_a_failure_run_keeps_its_counters(request):
    """The run's start and count stay as they were: neither raised nor reset."""
    rig = _setup(request, **FAILING)
    run = sync(rig)
    assert run.res['one']['status'] == 'warning'
    saved = entry(rig, 'one')
    assert (saved['last_status'], saved['fail_count'], saved['failed_since']) == ('warning', 2, NOW - 7200)


def test_an_empty_playlist_keeps_the_imports_channels(request):
    rig = _setup(request)
    before = {c['id']: c for c in disk(rig)['channels']}
    shows = [{'channel': 'A1@one', 'start': _ts(NOW - 600), 'stop': _ts(NOW + 600), 'title': [('Show', '')]}]
    run = sync(rig, xmltv=_XMLTV(shows))
    after = {c['id']: c for c in disk(rig)['channels']}
    for cid in ('A1@one', 'A2@one'):
        assert after[cid] == before[cid], 'record, number and switch kept as they were'
    assert not {'A1@one', 'A2@one'}.intersection(run.m3u.added + run.m3u.removed), 'playlist entries untouched'
    assert run.xmltv.XMLTVDATA['programmes'] == shows, 'its guide kept'
    assert 'B1@two' in run.m3u.added, 'the other import synced as usual'


def test_a_later_real_failure_still_counts(request):
    rig = _setup(request)
    sync(rig)
    assert 'fail_count' not in entry(rig, 'one')
    rig.source.answer(P1, DOWN)
    started = int(time.time())
    run = sync(rig, force_scope='one')
    assert run.res['one']['status'] == 'failed'
    saved = entry(rig, 'one')
    assert saved['last_status'] == 'failed' and saved['fail_count'] == 1
    assert saved['failed_since'] >= started, 'the failure run starts at the real failure, not at the empty read'


def test_a_later_real_failure_continues_an_earlier_run(request):
    rig = _setup(request, **FAILING)
    sync(rig)
    rig.source.answer(P1, DOWN)
    sync(rig, force_scope='one')
    saved = entry(rig, 'one')
    assert (saved['last_status'], saved['fail_count'], saved['failed_since']) == ('failed', 3, NOW - 7200)
