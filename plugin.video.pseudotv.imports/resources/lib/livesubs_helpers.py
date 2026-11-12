#   Copyright (C) 2025 Lunatixz
#
# This file is part of PseudoTV Live.
#
# PseudoTV Live is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# PseudoTV Live is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with PseudoTV Live.  If not, see <http://www.gnu.org/licenses/>.

# -*- coding: utf-8 -*-

# madteevee (imports fork, imports.67): LiveTV subtitles follow the channel.
#
# Import-light pure module (same pattern as rulestate_helpers.py /
# picture_helpers.py): no imports at all, no I/O, Python 3.8 syntax.
# services.py supplies the Kodi reads and writes; the tests run this without
# Kodi stubs.
#
# Background: Kodi remembers video settings (subtitles on/off included) per
# video, and a video it has no saved settings for starts with the default
# (ApplicationPlayerCallback::RequestVideoSettings, Kodi 21) -- subtitles off
# on this box. A live channel is one video, so live viewing keeps its choice
# across shows; but every replay (pvr://guide/<epg>/<start>.epg) is a new
# video: the RESTART pop-up (imports.62) and Kodi's "Play next programme
# automatically" both start replays with subtitles off, whatever was set on
# that channel a moment before.
#
# The add-on remembers the on/off per LiveTV channel ({key: bool}, the memory
# file MEMORY_FILE in the cache folder) and applies it whenever that channel
# plays (live or replay):
#   * restore (Player._onPlay, at Kodi's onAVStarted): restore_action;
#   * record (Monitor._chkIdle, about once a second while non-Custom video
#     plays -- a subtitle toggle fires no player event): record_action.
# Only on/off: the track (language) stays Kodi's choice.
#
# Memory file format: {channel key: true | false}; read through valid_memory.

# Memory file name, under the add-on's cache folder (CACHE_LOC).
MEMORY_FILE = 'livetv_subtitles.json'

# Seconds after a restore before it is checked once more (and re-applied
# once if Kodi changed it): Kodi applies its per-video settings on the same
# event queue as onAVStarted, before it, but that order couldn't be confirmed
# in Kodi's queue code. Short enough that a toggle of yours can't come first
# (the OSD takes longer to reach), so the re-check never undoes one.
RECHECK_S = 1.0

# Seconds to let a re-apply land before reading the state it left (Kodi
# queues the change), so the monitor never reads the value from before it.
APPLY_WAIT_S = 0.5

# imports.68: audio memory file name (same folder): {channel key: language
# code} -- the audio language that last played on each LiveTV channel.
AUDIO_MEMORY_FILE = 'livetv_audio.json'

# Key prefix of a LiveTV channel the add-on doesn't import (another PVR
# source): its name, which Kodi shows the same way live and in replays.
_NAME_PREFIX = 'name:'


def is_livetv(pvr_live, pvr_epgtag, is_pseudotv):
    """True for a LiveTV channel playing live (Pvr.IsPlayingTv) or as a
    replay (Pvr.IsPlayingEpgTag) -- never for a Custom (PseudoTV) channel,
    which has its own subtitle rules."""
    return bool(pvr_live or pvr_epgtag) and not is_pseudotv


def _text(value):
    return value.strip() if isinstance(value, str) else ''


def listed(channel):
    """True for an imported channel Kodi lists: the ones the playlist
    publishes (enabled, not orphaned, allocated -- the M3U writer's test)."""
    return (isinstance(channel, dict) and channel.get('type') == 'import'
            and isinstance(channel.get('id'), str) and bool(channel.get('id'))
            and bool(channel.get('enabled', True))
            and not channel.get('is_orphan') and not channel.get('unallocated'))


def channel_key(number_label, name, channels):
    """The memory key of the playing LiveTV channel from Kodi's labels
    (VideoPlayer.ChannelNumberLabel, VideoPlayer.ChannelName), which read the
    same live and in a replay:
      - the id of the only listed imported channel with that name (case and
        surrounding spaces ignored) -- stable across a rename or renumber;
      - when several listed channels share the name, None: they can't be
        told apart, so nothing is remembered for them. (Kodi numbers channels
        itself on this box -- pvrmanager.usebackendchannelnumbers off -- so
        its number label is a running count that can coincide with another
        channel's own number: no tie-break on it. `number_label` is unused.)
      - no listed imported channel: 'name:<name>' (a channel from another
        PVR source).
    None when Kodi gives no channel name (nothing to remember it by)."""
    name = _text(name)
    if not name:
        return None
    folded = name.casefold()
    same = [c for c in (channels or ()) if listed(c) and _text(c.get('name')).casefold() == folded]
    if len(same) == 1:
        return same[0]['id']
    if same:
        return None
    return _NAME_PREFIX + name


def subtitle_index(props):
    """The subtitle track to switch on, from JSON-RPC Player.GetProperties
    ('subtitles', 'currentsubtitle'): the current track when it is one of
    the listed tracks, else the first listed track; None without tracks.
    Switching on goes through this track (setSubtitleStream), not only the
    visibility flag: when a video starts with subtitles off, Kodi also
    disables the track in the stream (inputstream.adaptive on Movistar+), and
    showing it again (showSubtitles) leaves it blank."""
    props = props if isinstance(props, dict) else {}
    listing = props.get('subtitles')
    if not isinstance(listing, (list, tuple)):
        return None
    tracks = [_track(s.get('index')) for s in listing if isinstance(s, dict)]
    tracks = [index for index in tracks if index is not None]
    if not tracks:
        return None
    current = props.get('currentsubtitle')
    index = _track(current.get('index')) if isinstance(current, dict) else None
    return index if index in tracks else tracks[0]


def _track(value):
    """A usable track index: a non-negative int (not a bool), else None."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def valid_memory(data):
    """The usable entries of a loaded memory file: text keys with a bool
    value ({} for anything that isn't a dict); anything else is dropped."""
    if not isinstance(data, dict):
        return {}
    return {key: value for key, value in data.items()
            if isinstance(key, str) and key and isinstance(value, bool)}


def restore_action(saved, current, has_track):
    """What to do when a LiveTV channel starts: True = turn subtitles on,
    False = turn them off, None = leave them. `saved` is the channel's
    remembered choice (None when there is none); `current` whether Kodi
    shows subtitles now; `has_track` whether the video has a subtitle track
    (nothing to turn on without one)."""
    if not isinstance(saved, bool) or saved == bool(current):
        return None
    if saved and not has_track:
        return None
    return saved


def record_action(saved, current, has_track, seed, baseline):
    """The choice to remember for the playing LiveTV channel, or None.
      - Nothing while the video has no subtitle track: a show without
        subtitles must not erase "on".
      - With a remembered choice: the current state when it differs (you
        switched subtitles on or off).
      - Without one: the current state when it differs from how this
        playback started (`baseline`: you switched them), or right away on a
        live tune (`seed`): that starting state is Kodi's own saved setting
        for the live channel, so a RESTART straight after carries it over."""
    if not has_track:
        return None
    current = bool(current)
    if isinstance(saved, bool):
        return current if current != saved else None
    if seed or current != bool(baseline):
        return current
    return None


def remember(memory, key, value):
    """A new memory dict with `key` set to `value` (copy on write: a reader
    holding the old dict never sees it change)."""
    out = dict(memory or {})
    out[key] = bool(value)
    return out



# ------------------------------------------------------------------ imports.68: audio
# Audio follows the channel too: the language that last played on a LiveTV
# channel (whether you picked it or Kodi did -- the user's rule, 2026-11-11)
# starts that channel next time, live or replay. Kodi otherwise starts a
# replay in the preferred audio language (no saved setting for a new video)
# and a live channel in whatever it saved for it last (Neox live: Spanish).
# Tracks are matched by language code, which Movistar+ gives the same live and
# in replays (spa, eng, es-[ADS]); only the flags differ (on live, English is
# labelled "impaired" -- normal English, the user confirmed). A language the
# stream doesn't offer (a Spanish-only show) never replaces the remembered
# one; a switch Kodi had to make because the playing language disappeared
# ("forced") isn't recorded, and the remembered language is switched back to
# when it reappears in the same playback.


def lang_code(value):
    """A language code for comparing: stripped and case-folded ('' when not
    text). Stored values keep Kodi's own spelling."""
    return value.strip().casefold() if isinstance(value, str) else ''


def valid_audio_memory(data):
    """The usable entries of a loaded audio memory file: text keys with a
    non-empty text value ({} for anything that isn't a dict)."""
    if not isinstance(data, dict):
        return {}
    return {key: value.strip() for key, value in data.items()
            if isinstance(key, str) and key and isinstance(value, str) and value.strip()}


def audio_tracks(props):
    """[(index, code, impaired)] from JSON-RPC Player.GetProperties
    ('audiostreams'), in Kodi's order; tracks without a usable index or
    language are left out."""
    props = props if isinstance(props, dict) else {}
    listing = props.get('audiostreams')
    if not isinstance(listing, (list, tuple)):
        return []
    out = []
    for stream in listing:
        if not isinstance(stream, dict):
            continue
        index, code = _track(stream.get('index')), lang_code(stream.get('language'))
        if index is not None and code:
            out.append((index, code, bool(stream.get('isimpaired'))))
    return out


def audio_track(props, lang):
    """The index of the track in language `lang`: one not labelled
    "impaired" when there are several, else the first; None when the stream
    doesn't offer it."""
    code = lang_code(lang)
    if not code:
        return None
    same = [(index, impaired) for index, track_code, impaired in audio_tracks(props) if track_code == code]
    plain = [index for index, impaired in same if not impaired]
    if plain:
        return plain[0]
    return same[0][0] if same else None


def audio_restore_action(saved, playing, props):
    """The track to switch to when a LiveTV channel starts (or at the
    re-check), or None: the remembered language when it differs from the
    playing one and the stream offers it."""
    if not lang_code(saved) or lang_code(saved) == lang_code(playing):
        return None
    return audio_track(props, saved)


def audio_settle_record(saved, playing, saved_offered):
    """The language to remember once a playback has settled, or None:
      - nothing playing (no language): None;
      - nothing remembered yet: the playing language (Kodi's pick counts);
      - the remembered language plays: None (nothing new);
      - the remembered language is offered but another plays (the restore
        didn't win, or you switched during the start-up): the playing one;
      - the remembered language isn't offered (a Spanish-only show): None,
        so it isn't replaced for good."""
    if not lang_code(playing):
        return None
    if not lang_code(saved):
        return playing.strip()
    if lang_code(saved) == lang_code(playing):
        return None
    return playing.strip() if saved_offered else None


def audio_change_record(playing, baseline, baseline_offered):
    """A change of the playing language during a settled playback ->
    (language to remember or None, forced). A change while the previous
    language is still offered is a switch (yours): remembered. One made
    because the previous language disappeared (Kodi had to switch, e.g. a
    Spanish-only next show on live) is forced: not remembered. No change, or
    no language: (None, False)."""
    if not lang_code(playing) or lang_code(playing) == lang_code(baseline):
        return None, False
    if baseline_offered:
        return playing.strip(), False
    return None, True


def audio_return_action(saved, playing, props):
    """After a forced change: the track to switch back to once the
    remembered language is offered again, or None."""
    return audio_restore_action(saved, playing, props)


def remember_audio(memory, key, lang):
    """A new audio memory dict with `key` set to `lang` (copy on write)."""
    out = dict(memory or {})
    out[key] = lang.strip()
    return out
