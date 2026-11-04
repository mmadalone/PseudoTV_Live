"""imports.60 (D3): "Rollback watched" as an exact undo (playstate_helpers).

Rollback_Watched used to queue jsonrpc.quePlaycount on every PseudoTV programme
change: the guide-build play count plus resume {0,0}, which erased resume
points (live programmes included) and ignored its own switch. Now a snapshot of
the played file's watched count / last-played date / resume point is taken when
a programme starts playing its real file, and at its end only the fields Kodi
changed are put back with Files.SetFileDetails.

Everything that decides what is read or written lives in playstate_helpers and
is tested here directly (services.py can't be imported under the test stubs;
its glue is checked by source in test_imports60_playstate_glue.py).
"""
import ast
import json
import os
import re
import threading
import time

import pytest

import playstate_helpers as ph
from playstate_helpers import PlaystateTracker, parse_playstate, restore_request, programme_key


HERE      = os.path.dirname(os.path.abspath(__file__))
ADDON_LIB = os.path.join(os.path.dirname(HERE), 'resources', 'lib')
NOW       = '2026-11-04 21:00:00'
F         = '/media/tv/Show/Season 1/Show - S01E02.mkv'
F2        = '/media/tv/Show/Season 1/Show - S01E03.mkv'


# --- captured Files.GetFileDetails replies (live, read-only, 2026-11-04) -----

EPISODE_REPLY = {"id": "plugin.video.pseudotv.imports", "jsonrpc": "2.0",
                 "result": {"filedetails": {"id": 26091, "label": "Pilot", "lastplayed": "2025-02-23 00:00:00",
                                            "playcount": 1, "resume": {"position": 0.0, "total": 0.0}, "type": "episode"}}}
MOVIE_REPLY   = {"id": "plugin.video.pseudotv.imports", "jsonrpc": "2.0",
                 "result": {"filedetails": {"id": 427, "label": "Rashomon", "lastplayed": "",
                                            "playcount": 0, "resume": {"position": 0.0, "total": 0.0}, "type": "movie"}}}
TRAILER_REPLY = {"id": "plugin.video.pseudotv.imports", "jsonrpc": "2.0",
                 "result": {"filedetails": {"label": "x-trailer.mkv", "lastplayed": "2026-05-23 20:59:08",
                                            "playcount": 0, "resume": {"position": 0.0, "total": 0.0}, "type": "unknown"}}}
# sendJSON moves Kodi's error into result['error'] (jsonrpc.py sendJSON).
ERROR_REPLY   = {"id": "plugin.video.pseudotv.imports", "jsonrpc": "2.0",
                 "result": {"error": {"code": -32602, "message": "Invalid params."}}}


def S(pc, lp, pos=0.0, tot=0.0, ref=('episode', 7)):
    return {'ref': ref, 'playcount': pc, 'lastplayed': lp, 'resume': {'position': pos, 'total': tot}}


# --- a small model of Kodi 21's Files.SetFileDetails ---------------------------
# FileOperations.cpp SetFileDetails + VideoDatabase::SetPlayCount +
# CVideoLibrary::UpdateResumePoint. playCount NULL reads back as 0 and
# lastPlayed NULL as ''.

def kodi_set_file_details(state, params, now=NOW, stream_total=2640.0):
    pc, lp = state['playcount'], state['lastplayed']
    new_pc, new_lp = pc, lp
    if 'lastplayed' in params:
        new_lp = params['lastplayed']
        new_pc = max(1, pc) if new_lp else 0     # "a valid lastplayed without a playcount forces playcount to 1"
    if 'playcount' in params:
        new_pc = params['playcount']
    if new_pc != pc or new_lp != lp:
        if new_pc: pc, lp = new_pc, (new_lp or now)  # count > 0: an empty date becomes "now"
        else:      pc, lp = 0, new_lp                # count 0: playCount NULL, lastPlayed NULL or the date
    resume = dict(state['resume'])
    if 'resume' in params:
        pos = params['resume']['position']
        if pos == 0.0: resume = {'position': 0.0, 'total': 0.0}
        else:
            # UpdateResumePoint: `if (total <= 0.0 && !GetResumeBookMark(...))` uses the
            # stream length, else the given total, so total 0 stays 0 when a resume point exists.
            tot = params['resume']['total']
            has_resume_point = state['resume']['position'] > 0
            resume = {'position': pos, 'total': tot if (tot > 0 or has_resume_point) else stream_total}
    return {'ref': state['ref'], 'playcount': pc, 'lastplayed': lp, 'resume': resume}


# --- library_ref / parse_playstate ----------------------------------------------

@pytest.mark.parametrize('details, ref', [
    ({'type': 'episode', 'id': 26091}, ('episode', 26091)),
    ({'type': 'movie', 'id': 427}, ('movie', 427)),
    ({'type': 'musicvideo', 'id': 3}, ('musicvideo', 3)),
])
def test_library_ref_accepts_library_items(details, ref):
    assert ph.library_ref(details) == ref


@pytest.mark.parametrize('details', [
    {'type': 'unknown'}, {'type': 'video', 'id': 5}, {'type': 'song', 'id': 5}, {'type': 'channel', 'id': 5},
    {'type': 'movies', 'id': 5}, {'type': 'episodes', 'id': 5}, {'id': 5},
    {'type': 'movie', 'id': 0}, {'type': 'movie', 'id': -1}, {'type': 'movie', 'id': None},
    {'type': 'movie', 'id': '26091'}, {'type': 'movie', 'id': True}, {'type': 'movie', 'id': False},
    {'type': 'movie', 'id': 2.0}, None, [], 'movie',
])
def test_library_ref_rejects_everything_else(details):
    assert ph.library_ref(details) is None


def test_parse_playstate_on_captured_replies():
    assert parse_playstate(EPISODE_REPLY) == {'ref': ('episode', 26091), 'playcount': 1, 'lastplayed': '2025-02-23 00:00:00',
                                              'resume': {'position': 0.0, 'total': 0.0}}
    assert parse_playstate(MOVIE_REPLY)['ref'] == ('movie', 427)
    assert parse_playstate(TRAILER_REPLY) is None            # not a library item: skipped quietly
    assert parse_playstate(ERROR_REPLY) is None


@pytest.mark.parametrize('response', [
    None, {}, [], {'result': {}}, {'result': 'OK'}, {'result': {'filedetails': None}},
    {'error': {'code': -32602}}, {'result': {'filedetails': {'type': 'episode', 'id': 7, 'resume': 'x'}}},
    {'result': {'filedetails': {'type': 'episode', 'id': 7, 'playcount': 'many'}}},
])
def test_parse_playstate_rejects_malformed(response):
    assert parse_playstate(response) is None


def test_parse_playstate_missing_fields_default_to_empty():
    state = parse_playstate({'result': {'filedetails': {'type': 'episode', 'id': 7}}})
    assert state == S(0, '')


def test_response_error():
    assert ph.response_error(EPISODE_REPLY) == ''
    assert ph.response_error({'result': 'OK'}) == ''
    assert '-32602' in ph.response_error(ERROR_REPLY)
    assert '-32602' in ph.response_error({'error': {'code': -32602}})
    assert ph.response_error(None) and ph.response_error({})


# --- restore_request ------------------------------------------------------------

def test_restore_nothing_changed_is_none():
    assert restore_request(F, S(1, '2025-02-23 00:00:00'), S(1, '2025-02-23 00:00:00')) is None
    # A position-0 resume with a stray total is "no resume point" on both sides.
    assert restore_request(F, S(0, '', 0.0, 0.0), S(0, '', 0.0, 2640.0)) is None


def test_restore_needs_both_states_same_item_and_a_file():
    assert restore_request(F, S(0, ''), None) is None
    assert restore_request(F, None, S(1, NOW)) is None
    assert restore_request('', S(0, ''), S(1, NOW)) is None
    assert restore_request(F, S(0, ''), S(1, NOW, ref=('episode', 8))) is None
    assert restore_request(F, S(0, '', ref=('movie', 7)), S(1, NOW, ref=('episode', 7))) is None


def test_restore_unwatched_marked_watched():
    request = restore_request(F, S(0, ''), S(1, NOW))
    assert request == {'method': 'Files.SetFileDetails',
                       'params': {'file': F, 'media': 'video', 'playcount': 0, 'lastplayed': ''}}


def test_restore_partial_play_always_sends_playcount_with_lastplayed():
    # Kodi forces playcount to 1 when a valid date comes without a playcount.
    request = restore_request(F, S(0, '2026-01-01 10:00:00'), S(0, NOW, 1234.0, 2640.0))
    assert request['params'] == {'file': F, 'media': 'video', 'playcount': 0, 'lastplayed': '2026-01-01 10:00:00',
                                 'resume': {'position': 0.0, 'total': 0.0}}


def test_restore_puts_back_a_cleared_resume_point():
    request = restore_request(F, S(0, '2026-01-01 10:00:00', 600.0, 2640.0), S(1, NOW))
    assert request['params']['resume'] == {'position': 600.0, 'total': 2640.0}
    assert request['params']['playcount'] == 0 and request['params']['lastplayed'] == '2026-01-01 10:00:00'


def test_restore_resume_moved_only():
    request = restore_request(F, S(0, '2026-01-01 10:00:00', 600.0, 2640.0), S(0, '2026-01-01 10:00:00', 1505.0, 2640.0))
    assert request['params'] == {'file': F, 'media': 'video', 'resume': {'position': 600.0, 'total': 2640.0}}


def test_restore_playcount_above_zero_without_date_keeps_kodis_date():
    # Kodi can't store playcount > 0 with no date; sending '' would become "now".
    request = restore_request(F, S(3, ''), S(4, NOW))
    assert request['params'] == {'file': F, 'media': 'video', 'playcount': 3}


@pytest.mark.parametrize('before, after', [
    (S(0, ''), S(0, NOW, 300.0, 2640.0)),                                                # first partial play
    (S(0, ''), S(1, NOW)),                                                               # watched to the end
    (S(1, '2025-02-23 00:00:00'), S(1, NOW, 300.0, 2640.0)),                             # rewatch, partial
    (S(1, '2025-02-23 00:00:00'), S(2, NOW)),                                            # rewatch, complete
    (S(0, '2026-01-01 10:00:00', 600.0, 2640.0), S(0, NOW, 1505.0, 2640.0)),             # in progress, moved on
    (S(0, '2026-01-01 10:00:00', 600.0, 2640.0), S(1, NOW)),                             # in progress, finished
    (S(1, '2026-01-01 10:00:00', 600.0, 2640.0), S(2, NOW)),                             # watched + resume, finished
    (S(2, '2026-01-01 10:00:00', 600.0, 2640.0), S(2, NOW, 600.0, 2640.0)),              # opened, same position
    (S(0, '', ref=('movie', 427)), S(1, NOW, ref=('movie', 427))),
    (S(0, '', ref=('musicvideo', 3)), S(0, NOW, 61.0, 240.0, ref=('musicvideo', 3))),
])
def test_restore_turns_after_back_into_before_under_kodis_rules(before, after):
    request = restore_request(F, before, after)
    assert request is not None
    assert kodi_set_file_details(after, request['params']) == before


def test_restore_with_unstorable_before_gets_as_close_as_kodi_allows():
    before, after = S(3, ''), S(4, NOW, 300.0, 2640.0)
    result = kodi_set_file_details(after, restore_request(F, before, after)['params'])
    assert result['playcount'] == 3 and result['resume'] == before['resume'] and result['lastplayed'] == NOW


def test_restore_resume_point_without_a_total():
    """A resume point stored with total 0 (e.g. by another app). Kodi keeps total 0
    only while the file still has a resume point; after a play that cleared it,
    Kodi fills in the stream length: the position is right, the total differs."""
    before = S(0, '2026-01-01 10:00:00', 600.0, 0.0)
    after  = S(0, NOW, 900.0, 2640.0)
    assert kodi_set_file_details(after, restore_request(F, before, after)['params']) == before
    after  = S(1, NOW)
    result = kodi_set_file_details(after, restore_request(F, before, after)['params'])
    assert result == S(0, '2026-01-01 10:00:00', 600.0, 2640.0)


@pytest.mark.parametrize('before, after', [
    (S(0, ''), S(1, NOW)), (S(0, '2026-01-01 10:00:00', 600.0, 2640.0), S(0, NOW, 1505.0, 2640.0)), (S(3, ''), S(4, NOW)),
    (S(0, ''), S(0, '', 300.0, 2640.0)),
])
def test_restore_request_shape(before, after):
    request = restore_request(F, before, after)
    assert set(request) == {'method', 'params'}
    assert request['method'] == 'Files.SetFileDetails'
    params = request['params']
    assert params['file'] == F and params['media'] == 'video'
    assert set(params) <= {'file', 'media', 'playcount', 'lastplayed', 'resume'}       # never 'runtime'
    if 'lastplayed' in params: assert 'playcount' in params
    if 'playcount' in params: assert isinstance(params['playcount'], int) and not isinstance(params['playcount'], bool)
    if 'resume' in params:
        assert set(params['resume']) == {'position', 'total'}
        assert all(isinstance(v, float) and v >= 0 for v in params['resume'].values())
    json.dumps(request)


# --- programme_key ---------------------------------------------------------------

def P(file, start=100, ts=10.0, chid='ch@PseudoTV_Live', **extra):
    info = {'chid': chid, '_tune_ts': ts, 'isPseudoTV': True, 'fitem': {'file': file, 'start': start}}
    info.update(extra)
    return info


def test_programme_key():
    assert programme_key(P(F)) == programme_key(P(F))
    assert programme_key(P(F)) != programme_key(P(F, ts=11.0))       # re-click of the same programme
    assert programme_key(P(F)) != programme_key(P(F, start=200))     # next playlist entry, same file
    assert programme_key(P(F)) != programme_key(P(F2))
    assert programme_key(None) == programme_key({}) == (None, None, None, None)


# --- PlaystateTracker with a fake Kodi ---------------------------------------------

class FakeKodi(object):
    """Library rows per file, the item Kodi is playing, and JSON-RPC traffic."""

    def __init__(self):
        self.rows    = {}       # file -> state (or 'unknown' for a non-library file)
        self.playing = None     # path of the item Kodi plays (pvr://... for live)
        self.reads   = []
        self.sent    = []
        self.logs    = []
        self.reject  = False
        self.on_read = None

    # injected I/O
    def read_details(self, file):
        self.reads.append(file)
        if self.on_read: self.on_read(file)
        row = self.rows.get(file)
        if row is None: return dict(ERROR_REPLY)
        if row == 'unknown': return json.loads(json.dumps(TRAILER_REPLY))
        details = {'id': row['ref'][1], 'type': row['ref'][0], 'label': os.path.basename(file),
                   'playcount': row['playcount'], 'lastplayed': row['lastplayed'], 'resume': dict(row['resume'])}
        return {'id': 'x', 'jsonrpc': '2.0', 'result': {'filedetails': details}}

    def playing_file(self, info):
        file = ((info or {}).get('fitem') or {}).get('file')
        return file if (file and self.playing == file) else None

    def send(self, request):
        self.sent.append(json.loads(json.dumps(request)))
        if self.reject: return {'result': {'error': {'code': -32602, 'message': 'Invalid params.'}}}
        params = request['params']
        self.rows[params['file']] = kodi_set_file_details(self.rows[params['file']], params)
        return {'id': 'x', 'jsonrpc': '2.0', 'result': 'OK'}

    def log(self, msg, level=ph.DEBUG):
        self.logs.append((level, msg))

    # Kodi's own behaviour
    def play(self, path):
        self.playing = path

    def close_and_save(self, file, after):
        """Kodi closes the file and saves its marks (before Python hears about it)."""
        self.rows[file] = after
        self.playing = None

    def lines(self, level=None, text=''):
        return [m for lv, m in self.logs if (level is None or lv == level) and text in m]


def tracker(kodi, writes=True, **kw):
    return PlaystateTracker(kodi.read_details, kodi.playing_file, kodi.send, kodi.log, writes=writes, **kw)


def test_stage1_logs_the_exact_request_and_never_sends():
    kodi = FakeKodi()
    kodi.rows[F] = S(0, '', ref=('episode', 26091))
    t = tracker(kodi, writes=False)
    A = P(F)
    kodi.play(F)
    t.transition({}, A, True)
    assert len(kodi.lines(ph.INFO, 'snapshot ' + F)) == 1
    kodi.close_and_save(F, S(0, NOW, 300.0, 2640.0, ref=('episode', 26091)))
    t.transition(A)
    assert kodi.sent == []
    assert kodi.rows[F] == S(0, NOW, 300.0, 2640.0, ref=('episode', 26091))       # Kodi's marks stay in stage 1
    would = kodi.lines(ph.INFO, 'would send ')
    assert len(would) == 1
    text = re.search(r'would send (\{.*\}) \(Kodi now', would[0]).group(1)
    assert json.loads(text) == restore_request(F, S(0, '', ref=('episode', 26091)), kodi.rows[F])
    assert t.pending() == 0


def test_stage_switch_is_on_and_the_default_tracker_follows_it(monkeypatch):
    # Stage 2 (the released build) writes; the stage-1 test build had this False.
    assert ph.PLAYSTATE_WRITES is True
    kodi = FakeKodi()
    kodi.rows[F] = S(0, '')
    t = PlaystateTracker(kodi.read_details, kodi.playing_file, kodi.send, kodi.log)
    A = P(F)
    kodi.play(F)
    t.transition({}, A, True)
    kodi.close_and_save(F, S(1, NOW))
    t.transition(A)
    assert len(kodi.sent) == 1 and kodi.rows[F] == S(0, '') and not kodi.lines(ph.INFO, 'would send ')
    # The default follows the constant when the event runs.
    monkeypatch.setattr(ph, 'PLAYSTATE_WRITES', False)
    kodi.play(F)
    B = P(F, ts=20.0)
    t.transition({}, B, True)
    kodi.close_and_save(F, S(2, NOW))
    t.transition(B)
    assert len(kodi.sent) == 1 and kodi.lines(ph.INFO, 'would send ')


STAGE1_NOTE = 'Test build: "Rollback watched" only logs what it would undo'


def test_whats_new_says_what_this_stage_does():
    """The update's What's new popup shows once, at the stage-1 restart (stage 2 keeps
    the version), so the stage-1 build says rollback isn't on yet; stage 2 removes it."""
    addon = os.path.dirname(HERE)
    texts = []
    with open(os.path.join(addon, 'changelog.txt'), encoding='utf-8') as f:
        texts.append(f.read().split('v.0.8.0+imports.59')[0])
    with open(os.path.join(addon, 'addon.xml'), encoding='utf-8') as f:
        texts.append(f.read().split('<news>')[1].split('v0.8.0+imports.59')[0])
    for text in texts:
        assert (STAGE1_NOTE in text) is (not ph.PLAYSTATE_WRITES)


def test_stage2_restores_exactly_once():
    before = S(0, '2026-01-01 10:00:00', 600.0, 2640.0)
    kodi = FakeKodi()
    kodi.rows[F] = before
    t = tracker(kodi)
    A = P(F)
    kodi.play(F)
    t.transition({}, A, True)
    kodi.close_and_save(F, S(1, NOW))
    t.transition(A)
    assert len(kodi.sent) == 1 and kodi.sent[0] == restore_request(F, before, S(1, NOW))
    assert kodi.rows[F] == before
    assert len(kodi.lines(ph.INFO, 'restored ')) == 1
    t.transition(A)                                           # a second end event for the same programme
    assert len(kodi.sent) == 1


def test_no_change_is_logged_and_nothing_sent():
    kodi = FakeKodi()
    kodi.rows[F] = S(1, '2025-02-23 00:00:00')
    t = tracker(kodi)
    A = P(F)
    kodi.play(F)
    t.transition({}, A, True)
    kodi.close_and_save(F, S(1, '2025-02-23 00:00:00'))
    t.transition(A)
    assert kodi.sent == [] and len(kodi.lines(ph.INFO, 'no change')) == 1


def test_live_programme_gets_no_snapshot_and_no_jsonrpc():
    kodi = FakeKodi()
    kodi.rows[F] = S(0, '')
    t = tracker(kodi)
    A = P(F)
    kodi.play('pvr://channels/tv/All channels@-1/50002@pvr.iptvsimple_1.pvr')
    t.transition({}, A, True)
    t.transition(A, P(F2, start=200), True)
    t.transition(P(F2, start=200))
    assert kodi.reads == [] and kodi.sent == [] and t.pending() == 0


def test_switch_off_takes_no_snapshot():
    kodi = FakeKodi()
    kodi.rows[F] = S(0, '')
    t = tracker(kodi)
    A = P(F)
    kodi.play(F)
    for allow in (False, None, 0, []):
        t.transition({}, A, allow)
    assert kodi.reads == [] and t.pending() == 0


def test_non_library_file_is_skipped_quietly():
    kodi = FakeKodi()
    kodi.rows[F] = 'unknown'
    t = tracker(kodi)
    A = P(F)
    kodi.play(F)
    t.transition({}, A, True)
    t.transition(A)
    assert t.pending() == 0 and kodi.sent == []
    assert kodi.lines(ph.INFO, 'not a library') and not kodi.lines(ph.ERROR)


def test_unreadable_file_gets_no_snapshot():
    kodi = FakeKodi()                                         # no row: Kodi answers -32602
    t = tracker(kodi)
    A = P(F)
    kodi.play(F)
    t.transition({}, A, True)
    assert t.pending() == 0 and kodi.lines(ph.INFO, 'could not read') and not kodi.lines(ph.ERROR)


def test_snapshot_discarded_when_playback_moves_on_during_the_read():
    kodi = FakeKodi()
    kodi.rows[F] = S(0, '')
    kodi.on_read = lambda file: kodi.play(F2)                # the user zapped while the read was running
    t = tracker(kodi)
    kodi.play(F)
    t.transition({}, P(F), True)
    assert kodi.reads == [F]
    assert t.pending() == 0 and kodi.lines(ph.INFO, 'discarded')


def test_library_item_changed_under_the_file_restores_nothing():
    kodi = FakeKodi()
    kodi.rows[F] = S(0, '', ref=('episode', 7))
    t = tracker(kodi)
    A = P(F)
    kodi.play(F)
    t.transition({}, A, True)
    kodi.close_and_save(F, S(1, NOW, ref=('episode', 99)))   # e.g. a library re-scan gave the file a new id
    t.transition(A)
    assert kodi.sent == [] and len(kodi.lines(ph.ERROR, 'different library item')) == 1


def test_failed_reread_restores_nothing():
    kodi = FakeKodi()
    kodi.rows[F] = S(0, '')
    t = tracker(kodi)
    A = P(F)
    kodi.play(F)
    t.transition({}, A, True)
    del kodi.rows[F]                                         # the file went away
    t.transition(A)
    assert kodi.sent == [] and len(kodi.lines(ph.ERROR, 'could not re-read')) == 1


def test_rejected_write_is_an_error_with_the_request():
    kodi = FakeKodi()
    kodi.rows[F] = S(0, '')
    t = tracker(kodi)
    A = P(F)
    kodi.play(F)
    t.transition({}, A, True)
    kodi.close_and_save(F, S(1, NOW))
    kodi.reject = True
    t.transition(A)
    errors = kodi.lines(ph.ERROR, 'rejected')
    assert len(errors) == 1 and 'Files.SetFileDetails' in errors[0] and '-32602' in errors[0]


def test_exception_in_snapshot_read_keeps_store_consistent_and_still_restores_the_old_programme():
    kodi = FakeKodi()
    kodi.rows[F] = S(0, '')
    kodi.rows[F2] = S(0, '')
    t = tracker(kodi)
    A = P(F)
    kodi.play(F)
    t.transition({}, A, True)
    kodi.close_and_save(F, S(1, NOW))

    def boom(file):
        if file == F2: raise RuntimeError('read failed')
    kodi.on_read = boom
    B = P(F2, start=200)
    kodi.play(F2)
    t.transition(A, B, True)
    assert kodi.lines(ph.ERROR, 'snapshot of ' + F2)
    assert kodi.rows[F] == S(0, '')                          # A was still rolled back
    assert t.pending() == 0                                  # no half-written entry for B
    kodi.on_read = None
    t.transition(B, P(F2, start=300), True)                  # the lock was released
    assert t.pending() == 1


def test_exception_in_send_releases_the_lock():
    kodi = FakeKodi()
    kodi.rows[F] = S(0, '')
    t = tracker(kodi)
    A = P(F)
    kodi.play(F)
    t.transition({}, A, True)
    kodi.close_and_save(F, S(1, NOW))

    def broken(request): raise RuntimeError('socket closed')
    t._send = broken
    t.transition(A)
    assert kodi.lines(ph.ERROR, 'rollback of ' + F) and t.pending() == 0
    assert t._lock.acquire(False)
    t._lock.release()


def test_exception_in_the_gate_is_contained():
    kodi = FakeKodi()
    t = PlaystateTracker(kodi.read_details, lambda info: 1 / 0, kodi.send, kodi.log, writes=True)
    t.transition({}, P(F), True)
    assert kodi.lines(ph.ERROR, 'transition failed') and t._lock.acquire(False)
    t._lock.release()


def test_same_file_continues_carries_the_snapshot_over():
    before = S(0, '')
    kodi = FakeKodi()
    kodi.rows[F] = before
    t = tracker(kodi)
    A, B = P(F), P(F, ts=20.0)                               # re-click processed before the stop
    kodi.play(F)
    t.transition({}, A, True)
    kodi.rows[F] = S(0, NOW, 120.0, 2640.0)                  # Kodi saved A when the file re-opened
    reads = len(kodi.reads)
    t.transition(A, B, True)
    assert len(kodi.reads) == reads and kodi.sent == []      # carried over: no read, no write
    assert kodi.lines(ph.INFO, 'carried over')
    t.transition(A)                                          # the late stop for A: not A's any more
    assert kodi.sent == []
    kodi.close_and_save(F, S(1, NOW))
    t.transition(B)
    assert len(kodi.sent) == 1 and kodi.rows[F] == before


def test_reclick_after_stop_restores_twice_and_ends_at_the_original_state():
    before = S(0, '2026-01-01 10:00:00', 600.0, 2640.0)
    kodi = FakeKodi()
    kodi.rows[F] = before
    t = tracker(kodi)
    A, B = P(F), P(F, ts=20.0)
    kodi.play(F)
    t.transition({}, A, True)
    kodi.close_and_save(F, S(0, NOW, 900.0, 2640.0))          # plugin._play stops first...
    t.transition(A)
    assert kodi.rows[F] == before
    kodi.play(F)                                              # ...then plays the file again
    t.transition(A, B, True)
    kodi.close_and_save(F, S(1, NOW))
    t.transition(B)
    assert len(kodi.lines(ph.INFO, 'restored ')) == 2 and kodi.rows[F] == before


def test_live_then_catchup_of_the_same_file_takes_a_snapshot():
    kodi = FakeKodi()
    kodi.rows[F] = S(0, '')
    t = tracker(kodi)
    A, B = P(F), P(F, ts=20.0)
    kodi.play('pvr://channels/tv/All channels@-1/50002@pvr.iptvsimple_1.pvr')
    t.transition({}, A, True)
    kodi.play(F)                                              # the airing programme clicked in the guide
    t.transition(A, B, True)
    assert t.pending() == 1 and kodi.lines(ph.INFO, 'snapshot ' + F)


def test_catchup_then_back_to_live_restores():
    before = S(0, '')
    kodi = FakeKodi()
    kodi.rows[F] = before
    t = tracker(kodi)
    A, B = P(F), P(F, ts=20.0)
    kodi.play(F)
    t.transition({}, A, True)
    kodi.close_and_save(F, S(0, NOW, 300.0, 2640.0))
    kodi.play('pvr://channels/tv/All channels@-1/50002@pvr.iptvsimple_1.pvr')
    t.transition(A, B, True)                                  # same file name, but now live: no carry-over
    assert kodi.rows[F] == before and t.pending() == 0


def test_next_programme_restores_the_old_and_snapshots_the_new():
    kodi = FakeKodi()
    kodi.rows[F] = S(0, '')
    kodi.rows[F2] = S(1, '2025-02-23 00:00:00')
    t = tracker(kodi)
    A, B = P(F), P(F2, start=200)
    kodi.play(F)
    t.transition({}, A, True)
    kodi.close_and_save(F, S(1, NOW))
    kodi.play(F2)
    t.transition(A, B, True)
    assert kodi.reads[-2:] == [F2, F]                         # new snapshot first, then the restore
    assert kodi.rows[F] == S(0, '') and t.pending() == 1


def test_switch_off_at_the_next_programme_still_restores_the_old_one():
    kodi = FakeKodi()
    kodi.rows[F] = S(0, '')
    kodi.rows[F2] = S(0, '')
    t = tracker(kodi)
    A, B = P(F), P(F2, start=200, chid='other@PseudoTV_Live')
    kodi.play(F)
    t.transition({}, A, True)
    kodi.close_and_save(F, S(1, NOW))
    kodi.play(F2)
    t.transition(A, B, False)                                 # the new channel has rollback off
    assert kodi.rows[F] == S(0, '') and t.pending() == 0


def test_clear_cancels_pending_rollbacks():
    kodi = FakeKodi()
    kodi.rows[F] = S(0, '')
    t = tracker(kodi)
    A = P(F)
    kodi.play(F)
    t.transition({}, A, True)
    t.clear()
    assert t.pending() == 0 and kodi.lines(ph.INFO, '1 pending rollback(s) cancelled')
    kodi.close_and_save(F, S(1, NOW))
    t.transition(A)
    assert kodi.sent == [] and kodi.rows[F] == S(1, NOW)
    t.clear()
    assert len(kodi.lines(ph.INFO, 'cancelled')) == 1         # nothing pending: no second line


def test_overlapping_transitions_miss_but_never_restore_the_wrong_programme():
    """A plays F, B plays F2, C plays F again; C's event is handled before B's
    (thread order is not first-come-first-served). A's rollback is missed (its
    snapshot is dropped) but nothing wrong is written."""
    kodi = FakeKodi()
    kodi.rows[F] = S(0, '')
    kodi.rows[F2] = S(0, '')
    t = tracker(kodi)
    A, B, C = P(F), P(F2, start=200), P(F, start=300)
    kodi.play(F)
    t.transition({}, A, True)
    kodi.close_and_save(F, S(1, NOW))                         # A ended, Kodi marked F
    kodi.play(F)                                              # C is now playing F (B came and went)
    t.transition(B, C, True)                                  # handled first
    t.transition(A, B, True)                                  # handled second: B isn't playing any more
    assert kodi.sent == []
    assert kodi.lines(ph.INFO, 'dropped an older snapshot')
    kodi.close_and_save(F, S(2, NOW))
    t.transition(C)                                           # C restores to what it saw at its start
    assert kodi.rows[F] == S(1, NOW) and len(kodi.sent) == 1


def test_cap_drops_the_least_recently_used_snapshot():
    kodi = FakeKodi()
    t = tracker(kodi, cap=3)
    files = ['/f%d.mkv' % i for i in range(4)]
    infos = [P(f, start=i) for i, f in enumerate(files)]
    for f in files: kodi.rows[f] = S(0, '')
    # Snapshots for f0..f2 whose ends were never seen (e.g. missed events).
    for info in infos[:3]:
        kodi.play(info['fitem']['file'])
        t.transition({}, info, True)
    # f0 is touched again by a carry-over, so f1 is now the least recently used.
    kodi.play(files[0])
    t.transition(infos[0], P(files[0], start=0, ts=99.0), True)
    kodi.play(files[3])
    t.transition({}, infos[3], True)
    assert t.pending() == 3
    assert set(t._store) == {files[0], files[2], files[3]}
    assert kodi.lines(ph.INFO, 'cap reached, dropped the oldest: ' + files[1])
    assert ph.SNAPSHOT_CAP == 16


def test_concurrent_events_are_serialized():
    kodi = FakeKodi()
    for i in range(6): kodi.rows['/c%d.mkv' % i] = S(0, '')
    active, peak = [0], [0]
    guard = threading.Lock()

    def slow_read(file):
        with guard:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.01)
        with guard: active[0] -= 1
    kodi.on_read = slow_read
    kodi.playing_file = lambda info: info['fitem']['file']    # every programme "is playing" for this test
    t = PlaystateTracker(kodi.read_details, kodi.playing_file, kodi.send, kodi.log, writes=True)
    threads = [threading.Thread(target=t.transition, args=({}, P('/c%d.mkv' % i, start=i), True)) for i in range(6)]
    for th in threads: th.start()
    for th in threads: th.join(5)
    assert peak[0] == 1 and t.pending() == 6


# --- module hygiene ------------------------------------------------------------------

def _helpers_src():
    with open(os.path.join(ADDON_LIB, 'playstate_helpers.py'), encoding='utf-8') as f:
        return f.read()


def test_helpers_are_import_light_and_python38():
    src  = _helpers_src()
    tree = ast.parse(src, feature_version=(3, 8))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import): imported.update(a.name.split('.')[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom): imported.add((node.module or '').split('.')[0])
    assert imported <= {'json', 'threading'}
    # Grammar-valid but 3.9+ at runtime: builtin generics, str.removeprefix/removesuffix, dict |.
    assert not re.search(r'\b(removeprefix|removesuffix)\b', src)
    assert not re.search(r'\b(dict|list|tuple|set)\[', src)
    assert not any(isinstance(n, (ast.AnnAssign,)) for n in ast.walk(tree))
    assert all(not a.annotation for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) for a in n.args.args)
    assert 'madteevee (imports fork, imports.60)' in src
