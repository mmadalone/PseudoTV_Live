# -*- coding: utf-8 -*-
"""imports.68: LiveTV audio follows the channel -- the Kodi glue in services.py.

The bug: a replay and the same LiveTV channel live played different audio.
Kodi keeps the audio track per video. Every replay is a new video, so Kodi
starts it in the preferred audio language (English on this TV); a live
channel is one video, so it starts in whatever Kodi saved for it (Neox live:
Spanish). Watching a Neox replay in English and then tuning Neox live gave
Spanish, and the other way round.

The user's rule (2026-11-11), per channel: "last audio on the channel". The
audio language that last played on a LiveTV channel, whether you picked it or
Kodi did, starts that channel next time, live or replay. A language the
stream doesn't offer (a Spanish-only show) never replaces the remembered one,
and the remembered language comes back when the next show offers it again.

Measured 2026-11-11 (JSON-RPC Player.GetProperties 'audiostreams'):
  Warner TV live    0 spa (default), 1 eng (labelled impaired -- normal
                    English, the user confirmed), 2 es-[ADS] (Spanish audio
                    description, impaired); Kodi's saved pick: English;
  Warner TV replay  the same codes, every track default; Kodi picks English;
  Neox live         0 spa (default), 1 eng (labelled impaired); Kodi's saved
                    pick: Spanish.
Neox replays weren't measured: they are modelled on Warner TV's replay (the
live codes, every track default). VideoPlayer.AudioLanguage reads the
playing track's code.

imports.68 rides on imports.67's machinery, and so does this file: the rig of
test_imports67_livetv_subtitles.py (the real services.py methods compiled
onto a fake player, Kodi as a dict, the memory files written for real into a
tmp folder) carries Kodi's audio tracks too. What is tested here:
  - Player._restoreLiveSubtitles: the remembered language's track switched
    to (setAudioStream through _applyLiveAudio, the epoch checked right
    before), once more at the 1 s re-check, and the settled state's audio
    fields (audio, audio_forced, audio_pending);
  - Player._chkLiveAudio, on the monitor's tick behind every imports.67 gate:
    the settle's language saved once, your switch saved, a change Kodi had to
    make (the language disappeared) only marked forced, and the switch back
    once the remembered language is offered again;
  - the audio memory file (cache/livetv_audio.json) beside the subtitle one;
  - Custom channels and library videos untouched;
  - the plan's TV checks 1-3 and the Spanish-only show, end to end.
The pure decisions (livesubs_helpers) are tested in
test_imports68_audio_helpers.py.
"""
import ast

import pytest

import livesubs_helpers as lh
from test_imports67_livetv_subtitles import (
    AUDIO_READ, CN, CN_LIVE, CUSTOM_CN, LIBRARY, LIVE_METHODS, NEOX, NEOX_LIVE, NEOX_REPLAY, ON, S_TREE,
    TRACKS_READ, WARNER, WARNER_LIVE, WARNER_REPLAY, Rig, _calls, _class, _method, _owner_map, once, settled, tick,
    tune)


def _track(index, language, default, impaired):
    """One entry of Kodi's 'audiostreams' answer (the flags that matter here)."""
    return {'index': index, 'language': language, 'isdefault': default, 'isimpaired': impaired}


# Measured 2026-11-11.
WARNER_LIVE_TRACKS = (_track(0, 'spa', True, False), _track(1, 'eng', False, True),
                      _track(2, 'es-[ADS]', False, True))
WARNER_REPLAY_TRACKS = (_track(0, 'spa', True, False), _track(1, 'eng', True, False),
                        _track(2, 'es-[ADS]', True, False))
NEOX_LIVE_TRACKS = (_track(0, 'spa', True, False), _track(1, 'eng', False, True))
# Modelled on Warner TV's replay: the live codes, every track default.
NEOX_REPLAY_TRACKS = (_track(0, 'spa', True, False), _track(1, 'eng', True, False))
# A show with Spanish audio only.
SPANISH_ONLY = (_track(0, 'spa', True, False),)


def switch(index):
    return ('setAudioStream', index)


def kodi_plays(rig, tracks, lang):
    """Kodi lists `tracks` and plays the first one in `lang` (None: no track plays)."""
    index = None if lang is None else [t['index'] for t in tracks if t['language'] == lang][0]
    rig.kodi.state.update(astreams=[dict(t) for t in tracks], atrack=index)


def atune(rig, kodi_state, tracks, kodi_pick, subs=False, has=True):
    """A new LiveTV playback (imports.67's tune) in which Kodi starts `kodi_pick` from `tracks`."""
    kodi_plays(rig, tracks, kodi_pick)
    return tune(rig, kodi_state, subs, has=has)


def playing(rig):
    """What VideoPlayer.AudioLanguage reads now."""
    return rig.kodi.value('audio')


def audio_switches(rig):
    return [call for call in rig.player.switches if call[0] == 'setAudioStream']


def audio_reads(rig):
    return rig.player.jsonRPC.sent.count(AUDIO_READ)


def audio_writes(rig):
    """What was written to the audio memory file, write by write."""
    return [data for name, data in zip(rig.files.files, rig.files.writes) if name == lh.AUDIO_MEMORY_FILE]


def subtitle_writes(rig):
    return [data for name, data in zip(rig.files.files, rig.files.writes) if name == lh.MEMORY_FILE]


def audio_infos(rig):
    return [msg for msg in rig.player.infos() if msg.startswith('live audio:')]


# What a tick reads from Kodi before the audio step: the preview check, then _liveSubsInfo's seven labels.
TICK_READS = ('Player.ChannelPreviewActive', 'Pvr.IsPlayingTv', 'Pvr.IsPlayingEpgTag', 'VideoPlayer.ChannelNumberLabel',
              'VideoPlayer.ChannelName', 'VideoPlayer.SubtitlesEnabled', 'VideoPlayer.HasSubtitles',
              'VideoPlayer.AudioLanguage')


def bump_epoch(rig):
    """A newer playback (a zap, Kodi's next show, a Stop) reaching the add-on."""
    rig.player._play_epoch += 1


# === structure: where the calls sit, who writes what ==============================================

def test_init_loads_the_audio_memory_once_per_instance():
    init = [ast.unparse(n) for n in ast.walk(_method('Player', '__init__')) if isinstance(n, ast.Assign)]
    assert init.count('self._liveAudio = self._loadLiveAudio()') == 1
    class_level = {t.id for n in _class('Player').body if isinstance(n, ast.Assign) for t in n.targets
                   if isinstance(t, ast.Name)}
    assert '_liveAudio' not in class_level


def test_liveSubsInfo_reads_the_audio_label():
    """The playing track's language, read directly (no debug log every second), with the others."""
    ret = [n for n in ast.walk(_method('Player', '_liveSubsInfo')) if isinstance(n, ast.Return)]
    assert len(ret) == 1 and isinstance(ret[0].value, ast.Dict)
    reads = {ast.literal_eval(k): ast.unparse(v) for k, v in zip(ret[0].value.keys, ret[0].value.values)}
    assert reads['audio'] == "xbmc.getInfoLabel('VideoPlayer.AudioLanguage')"


def test_the_audio_memory_has_one_writer():
    """The monitor thread (_chkLiveAudio, from _chkLiveSubtitles) is the only writer of the audio memory and
    its file after __init__; the play thread only reads it. Every write is a whole new dict from
    remember_audio (copy on write: the play thread reads it without a lock)."""
    owner = _owner_map()
    stores = [n for n in ast.walk(S_TREE) if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Store)
              and n.attr == '_liveAudio']
    assert sorted({owner(n) for n in stores}) == [('Player', '__init__'), ('Player', '_chkLiveAudio')]
    assert [owner(n) for n in _calls(S_TREE, '_saveLiveAudio')] == [('Player', '_chkLiveAudio')] * 2
    writes = [n for n in ast.walk(_method('Player', '_chkLiveAudio')) if isinstance(n, ast.Assign)
              and any(isinstance(t, ast.Attribute) and t.attr == '_liveAudio' for t in n.targets)]
    assert len(writes) == 2
    assert all(ast.unparse(n.value).startswith('livesubs_helpers.remember_audio(self._liveAudio, key, ')
               for n in writes)
    memory = [n for n in ast.walk(S_TREE) if isinstance(n, ast.Attribute) and n.attr == '_liveAudio']
    in_place = [n for n in ast.walk(S_TREE)
                if (isinstance(n, ast.Subscript) and n.value in memory and not isinstance(n.ctx, ast.Load))
                or (isinstance(n, ast.Attribute) and n.value in memory and n.attr != 'get')]
    assert in_place == []


def test_every_audio_switch_goes_through_applyLiveAudio():
    """setAudioStream only in _applyLiveAudio (among the LiveTV methods), which checks the epoch right before
    it; called by the restore, its re-check and the switch back."""
    for name in LIVE_METHODS:
        assert bool(_calls(_method('Player', name), 'setAudioStream')) == (name == '_applyLiveAudio'), name
    body = [ast.unparse(s) for s in _method('Player', '_applyLiveAudio').body]
    assert body == ['if self._play_epoch != epoch:\n    return False', 'self.setAudioStream(index)', 'return True']
    owner = _owner_map()
    assert sorted(owner(n) for n in _calls(S_TREE, '_applyLiveAudio')) == \
        [('Player', '_chkLiveAudio'), ('Player', '_restoreLiveSubtitles'), ('Player', '_restoreLiveSubtitles')]


def test_the_audio_tick_runs_behind_every_gate_and_before_the_subtitle_record():
    """In _chkLiveSubtitles: after the epoch, video, preview, LiveTV, labels and file gates, before the
    late-track and record logic, which go on with the state it returns. It raises into the tick's one
    handler (no try of its own)."""
    chk = _method('Player', '_chkLiveSubtitles')
    assert len(chk.body) == 1 and isinstance(chk.body[0], ast.Try)
    stmts = [ast.unparse(s) for s in chk.body[0].body]
    at = stmts.index('state = self._chkLiveAudio(state, info, key)')
    before = stmts[:at]
    for gate in ("if not state or state.get('epoch') != self._play_epoch:\n    return",
                 'if not self.isPlayingVideo():\n    return',
                 "if xbmc.getCondVisibility('Player.ChannelPreviewActive'):\n    return",
                 "if not livesubs_helpers.is_livetv(info['live'], info['epgtag'], False):\n    return",
                 "if state.get('epoch') != self._play_epoch:\n    return",
                 "if (info['number'], info['name']) != state.get('labels'):\n    return",
                 "if self._liveSubsFile() != state.get('file'):\n    return"):
        assert gate in before, gate
    assert stmts[at - 1] == "key = state.get('key')"
    assert stmts[at + 1].startswith("if info['has'] and (not state.get('has')):")      # the late-track step
    assert len(_calls(S_TREE, '_chkLiveAudio')) == 1
    assert not [n for n in ast.walk(_method('Player', '_chkLiveAudio')) if isinstance(n, ast.Try)]


def test_the_audio_track_read(tmp_path):
    """Player.GetProperties audiostreams/currentaudiostream without the RPC_Delay pause (a switch may follow
    at once); its result, or {} when Kodi gives none."""
    rig = Rig(tmp_path)
    kodi_plays(rig, NEOX_LIVE_TRACKS, 'spa')
    props = rig.player._readAudioProps()
    assert rig.player.jsonRPC.sent == [AUDIO_READ] and rig.player.jsonRPC.delays == [False]
    assert [(s['index'], s['language']) for s in props['audiostreams']] == [(0, 'spa'), (1, 'eng')]
    assert props['currentaudiostream']['index'] == 0
    for answer in (None, {}, {'result': None}):
        rig.player.jsonRPC.audio_answer = answer
        assert rig.player._readAudioProps() == {}


# === restore: a LiveTV channel starts in its remembered language ==================================

@pytest.mark.parametrize('key, kodi_state, tracks, kodi_pick, remembered, index', [
    (NEOX,   NEOX_LIVE,     NEOX_LIVE_TRACKS,     'spa', 'eng',      1),   # English is only "impaired" here
    (NEOX,   NEOX_REPLAY,   NEOX_REPLAY_TRACKS,   'eng', 'spa',      0),
    (WARNER, WARNER_REPLAY, WARNER_REPLAY_TRACKS, 'eng', 'spa',      0),
    (WARNER, WARNER_LIVE,   WARNER_LIVE_TRACKS,   'eng', 'es-[ADS]', 2),   # chosen only when it is remembered
], ids=['neox-live-english', 'neox-replay-spanish', 'warner-replay-spanish', 'warner-live-audio-description'])
def test_the_restore_switches_to_the_remembered_language(tmp_path, key, kodi_state, tracks, kodi_pick,
                                                         remembered, index):
    rig = Rig(tmp_path, audio={key: remembered})
    epoch = atune(rig, kodi_state, tracks, kodi_pick)
    assert audio_switches(rig) == [switch(index)] and playing(rig) == remembered
    assert rig.player.monitor.waits == [lh.RECHECK_S] and audio_reads(rig) == 2
    assert audio_infos(rig) == ['live audio: %s restored %s' % (key, remembered)]
    assert rig.player._liveSubsState == settled(rig, epoch, key, kodi_state, seed=kodi_state['live'],
                                                baseline=False, audio=remembered)
    tick(rig, 3)
    assert audio_writes(rig) == [] and rig.asaved() == {key: remembered} and audio_reads(rig) == 2


@pytest.mark.parametrize('tracks, index', [
    ((_track(0, 'spa', True, False), _track(1, 'eng', False, True), _track(3, 'eng', False, False)), 3),
    ((_track(0, 'spa', True, False), _track(3, 'eng', False, False), _track(1, 'eng', False, True)), 3),
    ((_track(0, 'spa', True, False), _track(4, 'eng', False, False), _track(5, 'eng', False, False)), 4),
], ids=['plain-after-impaired', 'plain-first', 'two-plain'])
def test_the_restore_prefers_a_track_not_labelled_impaired(tmp_path, tracks, index):
    rig = Rig(tmp_path, audio={NEOX: 'eng'})
    atune(rig, NEOX_LIVE, tracks, 'spa')
    assert audio_switches(rig) == [switch(index)]


@pytest.mark.parametrize('remembered', ['eng', 'ENG', ' eng '])
@pytest.mark.parametrize('kodi_state, tracks', [(WARNER_LIVE, WARNER_LIVE_TRACKS),
                                                (WARNER_REPLAY, WARNER_REPLAY_TRACKS)], ids=['live', 'replay'])
def test_the_restore_leaves_the_language_already_playing(tmp_path, kodi_state, tracks, remembered):
    """Warner TV: Kodi already plays English (live: its saved pick; replay: the preferred language)."""
    rig = Rig(tmp_path, audio_raw='{"%s": "%s"}' % (WARNER, remembered))
    epoch = atune(rig, kodi_state, tracks, 'eng')
    assert audio_switches(rig) == [] and audio_infos(rig) == []
    assert rig.player.monitor.waits == [lh.RECHECK_S] and audio_reads(rig) == 2
    assert rig.player._liveSubsState == settled(rig, epoch, WARNER, kodi_state, seed=kodi_state['live'],
                                                baseline=False, audio='eng')
    tick(rig, 3)
    assert audio_writes(rig) == []


@pytest.mark.parametrize('kodi_state', [NEOX_LIVE, NEOX_REPLAY], ids=['live', 'replay'])
def test_the_restore_switches_nothing_when_the_language_isnt_offered(tmp_path, kodi_state):
    """A Spanish-only show: nothing to switch to, and Spanish isn't remembered for the channel."""
    rig = Rig(tmp_path, audio={NEOX: 'eng'})
    atune(rig, kodi_state, SPANISH_ONLY, 'spa')
    assert audio_switches(rig) == [] and audio_infos(rig) == []
    assert rig.player._liveSubsState['audio'] == 'spa' and rig.player._liveSubsState['audio_pending'] is None
    tick(rig, 3)
    assert audio_writes(rig) == [] and rig.asaved() == {NEOX: 'eng'} and rig.player._liveAudio == {NEOX: 'eng'}


def test_the_restore_without_a_remembered_language_switches_nothing(tmp_path):
    rig = Rig(tmp_path, audio={WARNER: 'eng'})          # another channel's language
    epoch = atune(rig, NEOX_LIVE, NEOX_LIVE_TRACKS, 'spa')
    assert audio_switches(rig) == [] and audio_reads(rig) == 2
    assert rig.player._liveSubsState == settled(rig, epoch, NEOX, NEOX_LIVE, seed=True, baseline=False,
                                                audio='spa', audio_pending='spa')


def test_the_audio_switch_comes_before_the_wait(tmp_path):
    """Applied at once (after the subtitle step), not left to the re-check."""
    rig = Rig(tmp_path, memory={NEOX: True}, audio={NEOX: 'eng'})
    at_wait = []
    rig.player.monitor.during = lambda: at_wait.append(list(rig.player.switches))
    atune(rig, NEOX_LIVE, NEOX_LIVE_TRACKS, 'spa')
    assert at_wait == [[ON, switch(1)]]
    assert rig.player.jsonRPC.sent == [TRACKS_READ, AUDIO_READ, AUDIO_READ]


def test_a_queued_audio_switch_that_lands_by_the_recheck_is_not_reapplied(tmp_path):
    rig = Rig(tmp_path, audio={NEOX: 'eng'})
    rig.kodi.queued = True                              # Kodi's player applies it by the next wait
    epoch = atune(rig, NEOX_LIVE, NEOX_LIVE_TRACKS, 'spa')
    assert audio_switches(rig) == [switch(1)] and rig.player.monitor.waits == [lh.RECHECK_S]
    assert rig.player._liveSubsState == settled(rig, epoch, NEOX, NEOX_LIVE, seed=True, baseline=False,
                                                audio='eng')


# --- the playback number (epoch) ------------------------------------------------------------------

def test_a_zap_during_the_audio_read_cancels_the_switch(tmp_path):
    """The track list came back for the old channel: nothing switched, no wait, not settled."""
    rig = Rig(tmp_path, audio={NEOX: 'eng'})
    rig.player.jsonRPC.audio_during = once(lambda: bump_epoch(rig))
    atune(rig, NEOX_LIVE, NEOX_LIVE_TRACKS, 'spa')
    assert audio_switches(rig) == [] and rig.player.jsonRPC.sent == [AUDIO_READ]
    assert rig.player.monitor.waits == [] and rig.player._liveSubsState is None
    assert playing(rig) == 'spa'


def test_the_epoch_is_checked_right_before_the_audio_switch(tmp_path):
    """A newer playback starting before the audio step (while the channel list is read): the track list is
    still read (read-only), the switch is not made."""
    rig = Rig(tmp_path, audio={NEOX: 'eng'})
    rig.player.service.tasks.during = lambda: bump_epoch(rig)
    atune(rig, NEOX_LIVE, NEOX_LIVE_TRACKS, 'spa')
    assert audio_switches(rig) == [] and rig.player.jsonRPC.sent == [AUDIO_READ]
    assert rig.player.monitor.waits == [] and rig.player._liveSubsState is None


def test_a_zap_during_the_rechecks_audio_read_cancels_the_reapply(tmp_path):
    rig = Rig(tmp_path, audio={NEOX: 'eng'})
    rig.player.monitor.during = once(lambda: rig.kodi.state.update(atrack=0))     # Kodi puts Spanish back
    reads = []

    def second_read():
        reads.append(None)
        if len(reads) == 2: bump_epoch(rig)
    rig.player.jsonRPC.audio_during = second_read
    atune(rig, NEOX_LIVE, NEOX_LIVE_TRACKS, 'spa')
    assert audio_switches(rig) == [switch(1)] and audio_reads(rig) == 2
    assert rig.player.monitor.waits == [lh.RECHECK_S] and rig.player._liveSubsState is None


@pytest.mark.parametrize('meanwhile', [WARNER_LIVE, LIBRARY, dict(NEOX_LIVE, number='9')],
                         ids=['another-channel', 'not-livetv', 'other-number'])
def test_what_plays_changed_during_the_wait_ends_the_restore_before_the_audio_recheck(tmp_path, meanwhile):
    rig = Rig(tmp_path, audio={NEOX: 'eng', WARNER: 'eng'})
    rig.player.monitor.during = lambda: rig.kodi.state.update(meanwhile, atrack=0)
    atune(rig, NEOX_LIVE, NEOX_LIVE_TRACKS, 'spa')
    assert audio_switches(rig) == [switch(1)] and audio_reads(rig) == 1
    assert rig.player._liveSubsState is None


# --- the 1 s re-check -----------------------------------------------------------------------------

def test_the_recheck_reapplies_the_audio_once_when_kodi_puts_its_pick_back(tmp_path):
    rig = Rig(tmp_path, audio={NEOX: 'eng'})
    rig.player.monitor.during = once(lambda: rig.kodi.state.update(atrack=0))
    epoch = atune(rig, NEOX_LIVE, NEOX_LIVE_TRACKS, 'spa')
    assert audio_switches(rig) == [switch(1), switch(1)] and playing(rig) == 'eng'
    assert rig.player.monitor.waits == [lh.RECHECK_S, lh.APPLY_WAIT_S]
    assert audio_reads(rig) == 3                          # start, re-check, after the re-apply
    assert audio_infos(rig) == ['live audio: %s restored eng' % NEOX, 'live audio: %s re-applied eng' % NEOX]
    assert rig.player._liveSubsState == settled(rig, epoch, NEOX, NEOX_LIVE, seed=True, baseline=False,
                                                audio='eng')
    tick(rig, 3)
    assert audio_writes(rig) == [] and rig.asaved() == {NEOX: 'eng'}


def test_a_switch_that_loses_twice_records_what_plays(tmp_path):
    """Kodi puts Spanish back after the restore and after the re-apply: the re-apply runs once, and the
    settled playback remembers what plays (the remembered language is offered, so another one playing
    counts as the channel's last audio)."""
    rig = Rig(tmp_path, audio={NEOX: 'eng'})
    rig.player.monitor.during = lambda: rig.kodi.state.update(atrack=0)
    epoch = atune(rig, NEOX_LIVE, NEOX_LIVE_TRACKS, 'spa')
    assert audio_switches(rig) == [switch(1), switch(1)] and rig.player.monitor.waits == [lh.RECHECK_S,
                                                                                          lh.APPLY_WAIT_S]
    assert rig.player._liveSubsState == settled(rig, epoch, NEOX, NEOX_LIVE, seed=True, baseline=False,
                                                audio='spa', audio_pending='spa')
    tick(rig)
    assert audio_writes(rig) == [{NEOX: 'spa'}] and rig.asaved() == {NEOX: 'spa'}


def test_subtitles_and_audio_reapply_share_one_wait(tmp_path):
    rig = Rig(tmp_path, memory={NEOX: True}, audio={NEOX: 'eng'})
    rig.player.monitor.during = once(lambda: rig.kodi.state.update(subs=False, atrack=0))
    epoch = atune(rig, NEOX_LIVE, NEOX_LIVE_TRACKS, 'spa')
    assert rig.player.switches == [ON, switch(1), ON, switch(1)]
    assert rig.player.monitor.waits == [lh.RECHECK_S, lh.APPLY_WAIT_S]
    assert rig.player.jsonRPC.sent == [TRACKS_READ, AUDIO_READ, TRACKS_READ, AUDIO_READ, AUDIO_READ]
    assert rig.player._liveSubsState == settled(rig, epoch, NEOX, NEOX_LIVE, baseline=True, audio='eng')


# --- what the settled playback leaves for the monitor ---------------------------------------------

NO_LANGUAGE = (_track(0, '', True, False),)


@pytest.mark.parametrize('remembered, kodi_state, tracks, kodi_pick, audio, pending, forced', [
    (None,  NEOX_LIVE,   NEOX_LIVE_TRACKS,   'spa', 'spa', 'spa', False),   # Kodi's pick counts
    (None,  NEOX_REPLAY, NEOX_REPLAY_TRACKS, 'eng', 'eng', 'eng', False),
    ('eng', NEOX_LIVE,   NEOX_LIVE_TRACKS,   'eng', 'eng', None,  False),   # nothing new
    ('eng', NEOX_LIVE,   NEOX_LIVE_TRACKS,   'spa', 'eng', None,  False),   # restored
    ('eng', NEOX_LIVE,   SPANISH_ONLY,       'spa', 'spa', None,  True),    # live: English may come back
    ('eng', NEOX_REPLAY, SPANISH_ONLY,       'spa', 'spa', None,  False),   # a replay's tracks never change
    (None,  NEOX_LIVE,   NO_LANGUAGE,        '',    '',    None,  False),   # no language: nothing to remember
    ('eng', NEOX_LIVE,   (),                 None,  '',    None,  True),
], ids=['new-live', 'new-replay', 'remembered-plays', 'restored', 'not-offered-live', 'not-offered-replay',
        'no-language', 'no-tracks-yet'])
def test_the_settled_audio_fields(tmp_path, remembered, kodi_state, tracks, kodi_pick, audio, pending, forced):
    rig = Rig(tmp_path, audio={} if remembered is None else {NEOX: remembered})
    atune(rig, kodi_state, tracks, kodi_pick)
    state = rig.player._liveSubsState
    assert (state['audio'], state['audio_pending'], state['audio_forced']) == (audio, pending, forced)
    assert rig.files.writes == []                         # the restore never writes


@pytest.mark.parametrize('answer', [{'result': {}}, {'result': {'error': {'code': -32100}}}, None],
                         ids=['no-list', 'error', 'no-answer'])
def test_the_remembered_language_playing_is_never_forced_when_kodi_lists_no_tracks(tmp_path, answer):
    """Kodi gives no track list, but its label shows the remembered language playing: nothing to wait
    for (not forced, no track read per tick) and nothing to remember."""
    rig = Rig(tmp_path, audio={NEOX: 'eng'})
    rig.player.jsonRPC.audio_answer = answer
    atune(rig, NEOX_LIVE, NEOX_LIVE_TRACKS, 'eng')
    state = rig.player._liveSubsState
    assert (state['audio'], state['audio_pending'], state['audio_forced']) == ('eng', None, False)
    tick(rig, 3)
    assert audio_reads(rig) == 2 and audio_writes(rig) == [] and audio_switches(rig) == []


def test_a_language_that_shows_only_after_the_settle_is_never_switched_to_again_and_again(tmp_path):
    """Settled while Kodi showed no audio language (Neox remembered in English); English then plays: the
    monitor never re-selects the track that already plays, and nothing is written."""
    rig = Rig(tmp_path, audio={NEOX: 'eng'})
    atune(rig, NEOX_LIVE, NO_LANGUAGE, '')
    assert rig.player._liveSubsState['audio'] == '' and audio_switches(rig) == []
    tick(rig, 2)
    kodi_plays(rig, NEOX_LIVE_TRACKS, 'eng')
    tick(rig, 4)
    assert audio_switches(rig) == [] and audio_writes(rig) == [] and rig.asaved() == {NEOX: 'eng'}
    assert rig.player._liveSubsState['audio'] == 'eng'


# === the monitor's tick: what is remembered =======================================================

@pytest.mark.parametrize('kodi_state, tracks, kodi_pick', [(NEOX_LIVE, NEOX_LIVE_TRACKS, 'spa'),
                                                           (NEOX_REPLAY, NEOX_REPLAY_TRACKS, 'eng')],
                         ids=['live', 'replay'])
def test_the_settles_language_is_saved_once(tmp_path, kodi_state, tracks, kodi_pick):
    """Nothing remembered for the channel: what Kodi picked is saved at the first tick (whether live or
    replay), then nothing more while it plays -- and no track read per tick."""
    rig = Rig(tmp_path)
    epoch = atune(rig, kodi_state, tracks, kodi_pick)
    pending = rig.player._liveSubsState
    tick(rig)
    assert rig.asaved() == {NEOX: kodi_pick} and audio_writes(rig) == [{NEOX: kodi_pick}]
    assert audio_infos(rig) == ['live audio: %s saved %s' % (NEOX, kodi_pick)]
    assert rig.player._liveSubsState is not pending and pending['audio_pending'] == kodi_pick   # a new dict
    assert rig.player._liveSubsState['audio_pending'] is None
    assert rig.player._liveSubsState['epoch'] == epoch and rig.player._liveSubsState['audio'] == kodi_pick
    tick(rig, 5)
    assert audio_writes(rig) == [{NEOX: kodi_pick}] and audio_reads(rig) == 2


def test_the_audio_memory_is_copied_on_write(tmp_path):
    rig = Rig(tmp_path, audio={WARNER: 'eng'})
    atune(rig, NEOX_REPLAY, NEOX_REPLAY_TRACKS, 'eng')
    before = rig.player._liveAudio
    tick(rig)
    assert before == {WARNER: 'eng'} and rig.player._liveAudio == {WARNER: 'eng', NEOX: 'eng'}
    assert rig.player._liveAudio is not before


@pytest.mark.parametrize('kodi_state, tracks', [(NEOX_LIVE, NEOX_LIVE_TRACKS), (NEOX_REPLAY, NEOX_REPLAY_TRACKS)],
                         ids=['live', 'replay'])
def test_a_switch_you_make_is_saved(tmp_path, kodi_state, tracks):
    rig = Rig(tmp_path, audio={NEOX: 'eng'})
    epoch = atune(rig, kodi_state, tracks, 'eng')
    tick(rig, 2)
    assert audio_writes(rig) == [] and audio_reads(rig) == 2
    rig.kodi.state['atrack'] = 0                          # you pick Spanish
    tick(rig)
    assert rig.asaved() == {NEOX: 'spa'} and audio_writes(rig) == [{NEOX: 'spa'}]
    assert audio_infos(rig) == ['live audio: %s saved spa' % NEOX]
    state = rig.player._liveSubsState
    assert (state['epoch'], state['audio'], state['audio_forced']) == (epoch, 'spa', False)
    assert audio_reads(rig) == 3                          # one read to tell your switch from a forced one
    tick(rig, 3)
    assert audio_writes(rig) == [{NEOX: 'spa'}] and audio_reads(rig) == 3
    rig.kodi.state['atrack'] = 1                          # and back to English
    tick(rig)
    assert audio_writes(rig) == [{NEOX: 'spa'}, {NEOX: 'eng'}] and rig.asaved() == {NEOX: 'eng'}


def test_the_audio_description_is_saved_when_you_pick_it(tmp_path):
    """Kodi's spelling is kept ('es-[ADS]'), and the next Warner TV replay starts with it."""
    rig = Rig(tmp_path, audio={WARNER: 'eng'})
    atune(rig, WARNER_LIVE, WARNER_LIVE_TRACKS, 'eng')
    rig.kodi.state['atrack'] = 2
    tick(rig)
    assert rig.asaved() == {WARNER: 'es-[ADS]'}
    atune(rig, WARNER_REPLAY, WARNER_REPLAY_TRACKS, 'eng')
    assert audio_switches(rig) == [switch(2)]


def test_a_spanish_only_show_on_live_is_forced_and_english_comes_back(tmp_path):
    """Neox live in English; the next show has Spanish only (Kodi has to switch): not remembered, marked
    forced. While it lasts nothing is switched or saved; when the following show offers English again
    (Kodi stays on Spanish) the add-on switches back to it, once."""
    rig = Rig(tmp_path, audio={NEOX: 'eng'})
    epoch = atune(rig, NEOX_LIVE, NEOX_LIVE_TRACKS, 'eng')
    tick(rig)
    kodi_plays(rig, SPANISH_ONLY, 'spa')                  # the Spanish-only show
    tick(rig)
    assert audio_writes(rig) == [] and rig.player._liveAudio == {NEOX: 'eng'}
    assert audio_infos(rig) == ['live audio: %s forced to spa (not remembered)' % NEOX]
    state = rig.player._liveSubsState
    assert (state['epoch'], state['audio'], state['audio_forced']) == (epoch, 'spa', True)
    tick(rig, 3)
    assert audio_switches(rig) == [] and audio_writes(rig) == []
    kodi_plays(rig, NEOX_LIVE_TRACKS, 'spa')              # the next show offers English again
    waits = len(rig.player.monitor.waits)
    tick(rig)
    assert audio_switches(rig) == [switch(1)] and playing(rig) == 'eng'
    assert rig.player.monitor.waits[waits:] == [lh.APPLY_WAIT_S]
    assert audio_infos(rig)[-1] == 'live audio: %s switched back to eng' % NEOX
    state = rig.player._liveSubsState
    assert (state['epoch'], state['audio'], state['audio_forced']) == (epoch, 'eng', False)
    reads = audio_reads(rig)
    tick(rig, 3)
    assert audio_switches(rig) == [switch(1)] and audio_reads(rig) == reads
    assert audio_writes(rig) == [] and rig.asaved() == {NEOX: 'eng'}


def test_a_forced_playback_reads_the_tracks_each_tick_until_the_language_returns(tmp_path):
    rig = Rig(tmp_path, audio={NEOX: 'eng'})
    atune(rig, NEOX_LIVE, NEOX_LIVE_TRACKS, 'eng')
    kodi_plays(rig, SPANISH_ONLY, 'spa')
    tick(rig)
    reads = audio_reads(rig)
    tick(rig, 4)
    assert audio_reads(rig) == reads + 4 and rig.player.jsonRPC.delays == [False] * len(rig.player.jsonRPC.sent)


def test_a_switch_back_kodi_makes_itself_writes_nothing(tmp_path):
    """After a forced change, Kodi itself plays English again when the next show offers it: the playback
    is no longer forced and English is still remembered -- nothing to write (written only on change)."""
    rig = Rig(tmp_path, audio={NEOX: 'eng'})
    atune(rig, NEOX_LIVE, NEOX_LIVE_TRACKS, 'eng')
    kodi_plays(rig, SPANISH_ONLY, 'spa')
    tick(rig)
    kodi_plays(rig, NEOX_LIVE_TRACKS, 'eng')
    tick(rig)
    assert audio_switches(rig) == [] and audio_writes(rig) == [] and rig.asaved() == {NEOX: 'eng'}
    assert rig.player._liveSubsState['audio'] == 'eng' and rig.player._liveSubsState['audio_forced'] is False
    assert not [msg for msg in audio_infos(rig) if 'saved' in msg]


@pytest.mark.parametrize('how', ['preview', 'other-file', 'other-labels', 'another-epoch', 'not-playing',
                                 'not-livetv'])
def test_nothing_is_recorded_or_switched_behind_a_closed_gate(tmp_path, how):
    """The settle left a language to save and the language changes, but the tick's gates are closed
    (the channel preview, Kodi already showing the next video or another channel, a newer playback, no
    video, no LiveTV): no audio read, no write, no switch, the state untouched."""
    rig = Rig(tmp_path, memory={WARNER: True})
    atune(rig, WARNER_LIVE, WARNER_LIVE_TRACKS, 'eng', subs=True)
    state = rig.player._liveSubsState
    assert state['audio_pending'] == 'eng'
    change = {'atrack': 0}
    if how == 'preview': change['preview'] = True
    elif how == 'other-file': change['file'] = 'pvr://guide/0071/next-show.epg'
    elif how == 'other-labels': change.update(number=NEOX_LIVE['number'], name=NEOX_LIVE['name'])
    elif how == 'another-epoch': bump_epoch(rig)
    elif how == 'not-playing': change['playing'] = False
    else: change.update(LIBRARY, number=WARNER_LIVE['number'], name=WARNER_LIVE['name'])
    rig.kodi.state.update(change)
    tick(rig, 3)
    assert audio_writes(rig) == [] and audio_reads(rig) == 2 and audio_switches(rig) == []
    assert rig.player._liveAudio == {} and rig.player._liveSubsState is state
    if how == 'preview':                                  # once the preview ends: the seed, then your switch
        rig.kodi.state['preview'] = False
        tick(rig)
        assert audio_writes(rig) == [{WARNER: 'eng'}, {WARNER: 'spa'}]


def test_nothing_is_recorded_before_the_playback_is_settled(tmp_path):
    rig = Rig(tmp_path)
    kodi_plays(rig, WARNER_LIVE_TRACKS, 'eng')
    rig.kodi.state.update(WARNER_LIVE)
    tick(rig, 3)
    assert rig.kodi.reads == [] and rig.player.jsonRPC.sent == [] and rig.files.writes == []


def test_a_zap_during_the_ticks_audio_read_records_nothing(tmp_path):
    """The read that tells your switch from a forced one came back for the next playback: nothing is
    decided on it -- not saved, not marked forced, the state untouched."""
    rig = Rig(tmp_path, memory={NEOX: False}, audio={NEOX: 'eng'})
    atune(rig, NEOX_LIVE, NEOX_LIVE_TRACKS, 'eng')
    state = rig.player._liveSubsState
    rig.kodi.state['atrack'] = 0
    rig.player.jsonRPC.audio_during = once(lambda: bump_epoch(rig))
    tick(rig)
    assert audio_writes(rig) == [] and rig.player._liveAudio == {NEOX: 'eng'}
    assert rig.player._liveSubsState is state and state['audio'] == 'eng' and state['audio_forced'] is False
    assert rig.player.warnings() == []


def _forced(rig):
    """Neox live in English, then a Spanish-only show: a forced playback."""
    epoch = atune(rig, NEOX_LIVE, NEOX_LIVE_TRACKS, 'eng')
    kodi_plays(rig, SPANISH_ONLY, 'spa')
    tick(rig)
    assert rig.player._liveSubsState['audio_forced'] is True
    return epoch


def test_a_zap_during_the_switch_backs_read_switches_nothing(tmp_path):
    rig = Rig(tmp_path, memory={NEOX: False}, audio={NEOX: 'eng'})
    _forced(rig)
    state = rig.player._liveSubsState
    kodi_plays(rig, NEOX_LIVE_TRACKS, 'spa')
    rig.player.jsonRPC.audio_during = once(lambda: bump_epoch(rig))
    waits, infos = len(rig.player.monitor.waits), len(audio_infos(rig))
    tick(rig)
    assert audio_switches(rig) == [] and rig.player._liveSubsState is state
    assert rig.player.monitor.waits[waits:] == [] and audio_infos(rig)[infos:] == []    # no wait, no log


def test_a_zap_during_the_switch_backs_wait_leaves_the_state_alone(tmp_path):
    """The switch was made for this playback; the language read after the wait would be the next one's."""
    rig = Rig(tmp_path, memory={NEOX: False}, audio={NEOX: 'eng'})
    _forced(rig)
    state = rig.player._liveSubsState
    kodi_plays(rig, NEOX_LIVE_TRACKS, 'spa')
    rig.player.monitor.during = once(lambda: bump_epoch(rig))
    reads = len(rig.kodi.reads)
    tick(rig)
    assert audio_switches(rig) == [switch(1)]
    assert rig.player._liveSubsState is state and state['audio_forced'] is True
    assert tuple(rig.kodi.reads[reads:]) == TICK_READS                  # no reading after the wait
    assert audio_writes(rig) == []


@pytest.mark.parametrize('write', ['settle', 'your-switch'])
def test_a_slow_audio_save_never_overwrites_a_newer_playbacks_state(tmp_path, write):
    """The write (the settle's language, or a switch you made) stalls while a newer playback (a zap to Neox
    live) settles: the old playback's state is not put back over it, and the newer one saves its own
    language at its first tick."""
    rig = Rig(tmp_path, audio={} if write == 'settle' else {WARNER: 'eng'})
    atune(rig, WARNER_REPLAY, WARNER_REPLAY_TRACKS, 'eng')
    if write == 'your-switch': rig.kodi.state['atrack'] = 0
    newer = []

    def stall(data):
        rig.files.on_write = None
        newer.append((atune(rig, NEOX_LIVE, NEOX_LIVE_TRACKS, 'spa'), rig.player._liveSubsState))
    rig.files.on_write = stall
    tick(rig)
    remembered = 'eng' if write == 'settle' else 'spa'
    assert rig.asaved() == {WARNER: remembered}
    (epoch, state), = newer
    assert rig.player._liveSubsState is state
    assert state == settled(rig, epoch, NEOX, NEOX_LIVE, seed=True, baseline=False, audio='spa',
                            audio_pending='spa')
    tick(rig)
    assert rig.asaved() == {WARNER: remembered, NEOX: 'spa'}


def test_a_failing_audio_read_in_the_tick_is_logged(tmp_path):
    rig = Rig(tmp_path, audio={NEOX: 'eng'})
    atune(rig, NEOX_LIVE, NEOX_LIVE_TRACKS, 'eng')
    rig.kodi.state['atrack'] = 0
    rig.player.jsonRPC.fail = RuntimeError('JSON-RPC unavailable')
    tick(rig)
    assert rig.player.warnings() == ['_chkLiveSubtitles, failed: JSON-RPC unavailable']
    assert audio_writes(rig) == [] and rig.player._liveAudio == {NEOX: 'eng'}


def test_a_failing_audio_read_in_the_restore_is_logged(tmp_path):
    rig = Rig(tmp_path, audio={NEOX: 'eng'})
    rig.player.jsonRPC.fail = RuntimeError('JSON-RPC unavailable')
    atune(rig, NEOX_LIVE, NEOX_LIVE_TRACKS, 'spa')
    assert rig.player.warnings() == ['_restoreLiveSubtitles, failed: JSON-RPC unavailable']
    assert audio_switches(rig) == [] and rig.player._liveSubsState is None


# === Custom channels and library videos ===========================================================

def test_library_playback_never_touches_the_audio(tmp_path):
    rig = Rig(tmp_path, audio={WARNER: 'spa'})
    kodi_plays(rig, WARNER_REPLAY_TRACKS, 'eng')
    for state in (LIBRARY, dict(LIBRARY, number='12', name='Warner TV')):
        tune(rig, state, False)
    assert rig.player.jsonRPC.sent == [] and rig.player.switches == [] and rig.player.monitor.waits == []
    rig.kodi.state['atrack'] = 0
    tick(rig, 3)
    assert rig.player.jsonRPC.sent == [] and rig.files.writes == [] and rig.asaved() == {WARNER: 'spa'}


def test_custom_playback_after_livetv_never_touches_the_audio(tmp_path):
    """A Custom channel tune bumps the epoch and never restores: the settled LiveTV state is stale, so a
    language change on the Custom "Cartoon Network" (304) never lands on the imported one (303)."""
    rig = Rig(tmp_path, audio={CN: 'eng'})
    atune(rig, CN_LIVE, NEOX_LIVE_TRACKS, 'eng')
    reads = audio_reads(rig)
    rig.player._play_epoch += 2                           # the Custom tune: _onPlay's PseudoTV path
    rig.kodi.state.update(CUSTOM_CN)
    kodi_plays(rig, NEOX_LIVE_TRACKS, 'spa')
    tick(rig, 3)
    assert audio_reads(rig) == reads and audio_switches(rig) == [] and audio_writes(rig) == []
    assert rig.player._liveAudio == {CN: 'eng'}


def test_onplay_livetv_branch_restores_the_audio_after_scheduling_the_popup(tmp_path):
    """The real _onPlay on Neox live: the Restart pop-up is scheduled, then the channel's language."""
    rig = Rig(tmp_path, audio={NEOX: 'eng'})
    kodi_plays(rig, NEOX_LIVE_TRACKS, 'spa')
    rig.kodi.state.update(NEOX_LIVE, subs=False, has=True, file='pvr://channels/tv/All channels/pvr.iptvsimple_8.pvr')
    rig.player._play_epoch = 9
    rig.player._onPlay({}, 9)
    assert rig.player.calls[-4:] == [('toggleReplay', True, 9), ('sendJSON', 'Player.GetProperties'), switch(1),
                                     ('sendJSON', 'Player.GetProperties')]
    assert rig.player._liveSubsState['epoch'] == 9 and rig.player._liveSubsState['audio'] == 'eng'


# === the two memory files ========================================================================

def test_subtitle_and_audio_memories_are_two_files(tmp_path):
    """Each memory goes to its own file in its own format; a tick that changes both writes each once."""
    assert lh.MEMORY_FILE != lh.AUDIO_MEMORY_FILE
    rig = Rig(tmp_path)
    assert rig.path.parent == rig.apath.parent and rig.path != rig.apath
    atune(rig, WARNER_REPLAY, WARNER_REPLAY_TRACKS, 'eng')
    tick(rig)                                             # the settle's language
    rig.kodi.state.update(subs=True, atrack=0)            # you turn subtitles on and pick Spanish
    tick(rig)
    assert rig.saved() == {WARNER: True} and rig.asaved() == {WARNER: 'spa'}
    assert list(zip(rig.files.files, rig.files.writes)) == [
        (lh.AUDIO_MEMORY_FILE, {WARNER: 'eng'}), (lh.AUDIO_MEMORY_FILE, {WARNER: 'spa'}),
        (lh.MEMORY_FILE, {WARNER: True})]
    again = Rig(tmp_path)
    assert again.player._liveSubs == {WARNER: True} and again.player._liveAudio == {WARNER: 'spa'}


@pytest.mark.parametrize('raw, loaded', [
    ('{"TNT@movistarplus": "en', {}),
    ('["eng"]', {}),
    ('{"TNT@movistarplus": true}', {}),                  # the subtitle memory's format
    ('{"TNT@movistarplus": "", "": "eng", "NEOX@movistarplus": " spa ", "name:Warner TV": 1}', {NEOX: 'spa'}),
], ids=['truncated', 'list', 'subtitle-format', 'bad-entries'])
def test_a_damaged_audio_file_loads_what_it_can_and_leaves_the_subtitles_alone(tmp_path, raw, loaded):
    rig = Rig(tmp_path, memory={WARNER: True}, audio_raw=raw)
    assert rig.player._liveAudio == loaded and rig.player._liveSubs == {WARNER: True}
    atune(rig, WARNER_REPLAY, WARNER_REPLAY_TRACKS, 'eng')
    assert rig.player.switches == [ON]                    # the subtitle memory still applies
    tick(rig)
    assert rig.asaved() == dict(loaded, **{WARNER: 'eng'})   # the next save writes a good file
    assert rig.saved() == {WARNER: True}


def test_a_damaged_subtitle_file_leaves_the_audio_alone(tmp_path):
    rig = Rig(tmp_path, raw='{"TNT@movistarplus": tru', audio={WARNER: 'spa'})
    assert rig.player._liveSubs == {} and rig.player._liveAudio == {WARNER: 'spa'}
    atune(rig, WARNER_REPLAY, WARNER_REPLAY_TRACKS, 'eng')
    assert audio_switches(rig) == [switch(0)]


def test_a_missing_audio_file_loads_empty(tmp_path):
    rig = Rig(tmp_path)
    assert rig.player._liveAudio == {} and rig.player.warnings() == []


def test_an_audio_load_failure_is_logged_and_loads_empty(tmp_path):
    rig = Rig(tmp_path, audio={WARNER: 'eng'})
    rig.files.fail_read = OSError('Input/output error')
    assert rig.player._loadLiveAudio() == {}
    warnings = rig.player.warnings()
    assert len(warnings) == 1 and warnings[0].startswith('_loadLiveAudio, could not read')
    assert 'Input/output error' in warnings[0]


@pytest.mark.parametrize('failure', ['false', 'raise'])
def test_an_audio_save_failure_is_logged_and_the_memory_still_applies(tmp_path, failure):
    """The file keeps its previous content; the memory in RAM still applies."""
    rig = Rig(tmp_path, audio={NEOX: 'spa'})
    rig.files.fail_write = failure
    assert rig.player._saveLiveAudio({WARNER: 'eng'}) is False
    atune(rig, WARNER_REPLAY, WARNER_REPLAY_TRACKS, 'eng')
    tick(rig)
    warnings = [msg for msg in rig.player.warnings() if msg.startswith('_saveLiveAudio')]
    assert len(warnings) == 2 and all(msg.startswith('_saveLiveAudio, could not write') for msg in warnings)
    assert rig.asaved() == {NEOX: 'spa'} and rig.player._liveAudio == {NEOX: 'spa', WARNER: 'eng'}
    atune(rig, WARNER_LIVE, WARNER_LIVE_TRACKS, 'spa')
    assert audio_switches(rig) == [switch(1)]


def test_an_audio_save_returns_true_when_written(tmp_path):
    rig = Rig(tmp_path)
    assert rig.player._saveLiveAudio({WARNER: 'eng'}) is True and rig.asaved() == {WARNER: 'eng'}


def test_the_audio_path_is_built_on_every_call(tmp_path):
    rig = Rig(tmp_path)
    assert rig.player._liveAudioPath() == str(tmp_path / lh.AUDIO_MEMORY_FILE)
    moved = tmp_path / 'moved'
    type(rig.player)._liveAudioPath.__globals__['CACHE_LOC'] = str(moved)
    assert rig.player._liveAudioPath() == str(moved / lh.AUDIO_MEMORY_FILE)


def test_the_audio_memory_survives_a_kodi_restart(tmp_path):
    rig = Rig(tmp_path)
    atune(rig, NEOX_REPLAY, NEOX_REPLAY_TRACKS, 'eng')
    tick(rig)
    atune(rig, WARNER_LIVE, WARNER_LIVE_TRACKS, 'eng')
    rig.kodi.state['atrack'] = 0
    tick(rig)
    again = Rig(tmp_path)                                 # a new service: __init__ loads the file
    assert again.player._liveAudio == {NEOX: 'eng', WARNER: 'spa'}
    atune(again, NEOX_LIVE, NEOX_LIVE_TRACKS, 'spa')
    atune(again, WARNER_REPLAY, WARNER_REPLAY_TRACKS, 'eng')
    assert audio_switches(again) == [switch(1), switch(0)]


# === end to end: the plan's TV checks =============================================================

def test_tv_checks_1_and_2_neox(tmp_path):
    """1: a Neox replay in English, then Neox live: English. 2: Neox live switched to Spanish, then a Neox
    replay: Spanish; then back to English."""
    rig = Rig(tmp_path)                                   # imports.68's first start: no audio file yet
    # 1. a Neox replay: Kodi starts it in the preferred language, English -- remembered
    atune(rig, NEOX_REPLAY, NEOX_REPLAY_TRACKS, 'eng')
    tick(rig, 2)
    assert rig.asaved() == {NEOX: 'eng'} and audio_switches(rig) == []
    #    Neox live: Kodi starts its saved Spanish; English (labelled impaired, track 1) is switched to
    atune(rig, NEOX_LIVE, NEOX_LIVE_TRACKS, 'spa')
    assert audio_switches(rig) == [switch(1)] and playing(rig) == 'eng'
    tick(rig, 3)
    # 2. you switch Neox live to Spanish
    rig.kodi.state['atrack'] = 0
    tick(rig, 2)
    assert rig.asaved() == {NEOX: 'spa'}
    #    a Neox replay: Kodi starts English, the add-on switches to Spanish
    atune(rig, NEOX_REPLAY, NEOX_REPLAY_TRACKS, 'eng')
    assert audio_switches(rig) == [switch(1), switch(0)] and playing(rig) == 'spa'
    tick(rig, 2)
    #    then back to English, in the replay; Neox live follows
    rig.kodi.state['atrack'] = 1
    tick(rig, 2)
    atune(rig, NEOX_LIVE, NEOX_LIVE_TRACKS, 'spa')
    assert audio_switches(rig) == [switch(1), switch(0), switch(1)] and playing(rig) == 'eng'
    tick(rig, 3)
    assert audio_writes(rig) == [{NEOX: 'eng'}, {NEOX: 'spa'}, {NEOX: 'eng'}]
    assert audio_infos(rig) == ['live audio: %s %s' % (NEOX, what) for what in (
        'saved eng', 'restored eng', 'saved spa', 'restored spa', 'saved eng', 'restored eng')]
    assert rig.player.warnings() == []


def test_tv_check_3_warner_tv_stays_english(tmp_path):
    """Warner TV live (Kodi's saved English, track 1) and its replays (Kodi's preferred English): English
    throughout, nothing switched; the audio description never chosen."""
    rig = Rig(tmp_path)
    atune(rig, WARNER_LIVE, WARNER_LIVE_TRACKS, 'eng')
    tick(rig, 2)
    atune(rig, WARNER_REPLAY, WARNER_REPLAY_TRACKS, 'eng')        # the RESTART pop-up
    tick(rig, 2)
    rig.player._play_epoch += 1                                    # the replay ends: Kodi's next show
    atune(rig, WARNER_REPLAY, WARNER_REPLAY_TRACKS, 'eng')
    tick(rig, 2)
    atune(rig, WARNER_LIVE, WARNER_LIVE_TRACKS, 'eng')
    tick(rig, 2)
    assert audio_switches(rig) == [] and audio_writes(rig) == [{WARNER: 'eng'}]
    assert rig.asaved() == {WARNER: 'eng'} and rig.player.warnings() == []


def test_a_live_tune_onto_a_spanish_only_show_switches_to_english_at_the_next_show(tmp_path):
    """Neox remembered in English; Neox live is tuned while a Spanish-only show airs. Spanish plays because
    it has to: it isn't remembered, and when the next show offers English (Kodi keeps the stream it plays)
    the add-on switches to it. A replay afterwards stays English."""
    rig = Rig(tmp_path, audio={NEOX: 'eng'})
    epoch = atune(rig, NEOX_LIVE, SPANISH_ONLY, 'spa')
    assert audio_switches(rig) == []
    assert rig.player._liveSubsState == settled(rig, epoch, NEOX, NEOX_LIVE, seed=True, baseline=False,
                                                audio='spa', audio_forced=True)
    tick(rig, 3)
    assert audio_switches(rig) == [] and audio_writes(rig) == [] and rig.asaved() == {NEOX: 'eng'}
    kodi_plays(rig, NEOX_LIVE_TRACKS, 'spa')               # the next show offers English
    tick(rig)
    assert audio_switches(rig) == [switch(1)] and playing(rig) == 'eng'
    assert rig.player._liveSubsState['audio_forced'] is False and rig.player._liveSubsState['audio'] == 'eng'
    tick(rig, 2)
    atune(rig, NEOX_REPLAY, NEOX_REPLAY_TRACKS, 'eng')
    tick(rig, 2)
    assert audio_switches(rig) == [switch(1)] and audio_writes(rig) == [] and rig.asaved() == {NEOX: 'eng'}


def test_a_spanish_only_replay_doesnt_flip_the_channel(tmp_path):
    """A Spanish-only replay of a channel remembered in English: nothing switched, nothing saved, no
    track read per tick (a replay's tracks never change); the next Neox live is English again."""
    rig = Rig(tmp_path, audio={NEOX: 'eng'})
    epoch = atune(rig, NEOX_REPLAY, SPANISH_ONLY, 'spa')
    assert rig.player._liveSubsState == settled(rig, epoch, NEOX, NEOX_REPLAY, baseline=False, audio='spa')
    tick(rig, 5)
    assert audio_writes(rig) == [] and audio_reads(rig) == 2 and audio_switches(rig) == []
    atune(rig, NEOX_LIVE, NEOX_LIVE_TRACKS, 'spa')
    assert audio_switches(rig) == [switch(1)] and rig.asaved() == {NEOX: 'eng'}


def test_two_channels_keep_their_own_language(tmp_path):
    rig = Rig(tmp_path, audio={NEOX: 'spa', WARNER: 'eng'})
    atune(rig, NEOX_REPLAY, NEOX_REPLAY_TRACKS, 'eng')
    atune(rig, WARNER_LIVE, WARNER_LIVE_TRACKS, 'spa')
    assert audio_switches(rig) == [switch(0), switch(1)]
    rig.kodi.state['atrack'] = 2                           # Warner TV: you pick the audio description
    tick(rig)
    assert rig.asaved() == {NEOX: 'spa', WARNER: 'es-[ADS]'}



# === review round (2026-11-11): channel preview at tune time, absent language, blank labels ======

def test_a_channel_preview_at_tune_time_is_waited_out_before_anything_is_read(tmp_path):
    """Up/Down while Neox still loads previews Warner TV: its labels show when Neox's playback
    starts. Nothing of Warner TV's choice (English, subtitles on) is applied to Neox; the restore
    waits for the preview to end and settles on Neox."""
    from test_imports67_livetv_subtitles import WARNER_LIVE, once, playing_file
    rig = Rig(tmp_path, memory={NEOX: False, WARNER: True}, audio={NEOX: 'spa', WARNER: 'eng'})
    kodi_plays(rig, NEOX_LIVE_TRACKS, 'spa')
    rig.player._play_epoch += 2
    epoch = rig.player._play_epoch
    rig.kodi.state.update(NEOX_LIVE, subs=False, has=True, playing=True, file=playing_file(NEOX_LIVE, epoch),
                          preview=True, number=WARNER_LIVE['number'], name=WARNER_LIVE['name'])
    rig.player.monitor.during = once(lambda: rig.kodi.state.update(
        preview=False, number=NEOX_LIVE['number'], name=NEOX_LIVE['name']))
    rig.player._restoreLiveSubtitles(epoch)
    assert rig.player.switches == []                              # nothing of Warner TV's applied
    assert rig.player.monitor.waits[0] == 0.25                    # the preview, waited out first
    assert rig.player._liveSubsState['key'] == NEOX and rig.player._liveSubsState['audio'] == 'spa'


SPA_AND_ADS = (_track(0, 'spa', True, False), _track(2, 'es-[ADS]', False, True))


@pytest.mark.parametrize('kodi_state', [NEOX_REPLAY, NEOX_LIVE], ids=['replay', 'live'])
def test_a_switch_you_make_while_the_remembered_language_is_absent_is_saved(tmp_path, kodi_state):
    """English remembered, a show offering only Spanish and the audio description: Spanish plays,
    you pick the audio description -- your pick (Spanish was still offered), so it is remembered."""
    rig = Rig(tmp_path, audio={NEOX: 'eng'})
    atune(rig, kodi_state, SPA_AND_ADS, 'spa')
    rig.kodi.state['atrack'] = 2                                 # you pick the audio description
    tick(rig)
    assert rig.asaved() == {NEOX: 'es-[ADS]'}
    assert rig.player._liveSubsState['audio_forced'] is False


def test_a_blank_audio_label_mid_playback_is_ignored(tmp_path):
    """Kodi's label reads '' for a moment while a stream switches: not a language, so your switch
    to Spanish right after is judged against English (still offered) and remembered."""
    rig = Rig(tmp_path, audio={NEOX: 'eng'})
    atune(rig, NEOX_LIVE, NEOX_LIVE_TRACKS, 'eng')
    tick(rig)
    rig.kodi.state['atrack'] = 99                                # no track plays: the label reads ''
    tick(rig)
    rig.kodi.state['atrack'] = 0                                 # you pick Spanish
    tick(rig, 3)
    assert rig.asaved() == {NEOX: 'spa'} and audio_switches(rig) == []
    assert rig.player._liveSubsState['audio_forced'] is False


def test_the_first_language_after_a_blank_settle_is_seeded_not_forced(tmp_path):
    """The label was still blank when the playback settled: the first language Kodi then shows is
    what the settle would have remembered (nothing remembered yet: Kodi's pick), not a forced change."""
    rig = Rig(tmp_path)
    kodi_plays(rig, NEOX_LIVE_TRACKS, 'eng')
    rig.kodi.state['atrack'] = 99                                # blank at the settle
    tune(rig, NEOX_LIVE, False)
    assert rig.player._liveSubsState['audio'] == ''
    rig.kodi.state['atrack'] = 1                                 # English shows
    tick(rig, 2)
    assert rig.asaved() == {NEOX: 'eng'}
    assert rig.player._liveSubsState['audio_forced'] is False and rig.player._liveSubsState['audio'] == 'eng'


def test_a_blank_settle_on_a_show_without_the_remembered_language_is_forced_then_switched_back(tmp_path):
    rig = Rig(tmp_path, audio={NEOX: 'eng'})
    kodi_plays(rig, SPANISH_ONLY, 'spa')
    rig.kodi.state['atrack'] = 99
    tune(rig, NEOX_LIVE, False)
    rig.kodi.state['atrack'] = 0                                 # Spanish shows (English isn't offered)
    tick(rig)
    assert rig.asaved() == {NEOX: 'eng'} and rig.player._liveSubsState['audio_forced'] is True
    kodi_plays(rig, NEOX_LIVE_TRACKS, 'spa')                     # the next show offers English again
    tick(rig)
    assert audio_switches(rig) == [('setAudioStream', 1)]
    assert rig.player._liveSubsState['audio_forced'] is False


def test_forced_stays_off_with_nothing_to_go_back_to(tmp_path):
    """A forced change while no different language is remembered: nothing to switch back to, so
    the playback isn't marked forced (no track-list read every second for nothing)."""
    rig = Rig(tmp_path)
    atune(rig, NEOX_LIVE, NEOX_LIVE_TRACKS, 'eng')
    rig.player._liveAudio = {}                                   # as if the seed hadn't been saved
    rig.player._liveSubsState = dict(rig.player._liveSubsState, audio_pending=None)
    kodi_plays(rig, SPANISH_ONLY, 'spa')                         # English disappears
    tick(rig)
    assert rig.player._liveSubsState['audio_forced'] is False
