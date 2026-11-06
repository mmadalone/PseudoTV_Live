#   Copyright (C) 2025 Lunatixz
#
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
#
# -*- coding: utf-8 -*-
from globals    import *
from cqueue     import *
from overlay    import Background, Overlay, Replay
from rules      import RulesList
from tasks      import Tasks
from jsonrpc    import JSONRPC
import playstate_helpers # madteevee (imports fork, imports.60): Rollback_Watched snapshot/restore
import restart_helpers   # madteevee (imports fork, imports.62): restart from the beginning (pop-up, remote, guide entry)
import clock_helpers     # madteevee (imports fork, imports.63): the one clock + time conversions

class Player(xbmc.Player):
    # imports.16: pendingItem / playingItem live as instance attrs (set in
    # __init__). Class-level mutable defaults are a Python footgun — if Player
    # were ever instantiated more than once (test fixture, future refactor)
    # the dicts would silently share state across instances.
    background        = None
    overlay           = None
    # v.26: chid the current overlay was opened for. Used by toggleOverlay
    # for idempotency (no-op when overlay already shows the right channel)
    # and to force-rebuild on chid mismatch (recovers from the chkOverlay-vs-
    # _onPlay race during fast zaps that captured a stale citem snapshot).
    _overlay_chid     = None
    # imports.18: serializes toggleOverlay reads/writes of self.overlay across
    # threads. Without this, two threads (e.g., _onPlay @threadit worker AND
    # _chkIdle's __chkOverlay) could both see self.overlay = None, both create
    # an Overlay, both assign self.overlay. The first Overlay's channelBug
    # control gets orphaned in xbmcgui.Window(12005) — its instance ref is
    # overwritten, but the Kodi-side control persists for the rest of the
    # session. Operator-visible symptom: the previous channel's logo sticks on
    # top of the current channel. Class-level Lock is fine (one Player
    # instance only); separate from _state_lock-style globals on principle.
    _overlay_lock     = Lock()
    replay            = None
    runActions        = None
    lastSubState      = False
    # madteevee: smart-subtitle policy state. Set by ForceSubtitles.runAction on
    # RULES_ACTION_PLAYER_START (channel tune). One of 'legacy' / 'always_on' /
    # 'always_off' / 'smart'. 'legacy' preserves the pre-fork behavior so channels
    # without the new options behave exactly as before.
    smartSubsMode     = 'legacy'
    
    #global rules
    enableOverlay     = SETTINGS.getSettingBool('Overlay_Enable')
    infoOnChange      = SETTINGS.getSettingBool('Enable_OnInfo')
    disableTrakt      = SETTINGS.getSettingBool('Disable_Trakt')
    rollbackPlaycount = SETTINGS.getSettingBool('Rollback_Watched')
    minDuration       = SETTINGS.getSettingInt('Seek_Tolerance')
    maxProgress       = SETTINGS.getSettingInt('Seek_Threshold')
    sleepTime         = SETTINGS.getSettingInt('Idle_Timer')
    runWhilePlaying   = SETTINGS.getSettingBool('Run_While_Playing')
    replayPercentage  = SETTINGS.getSettingInt('Replay_Percentage')
    OnNextMode        = SETTINGS.getSettingInt('OnNext_Mode')
    onNextPosition    = SETTINGS.getSetting("OnNext_Position_XY")	
    
    """ 
    Player() Trigger Order
    Player: onPlayBackStarted
    Player: onAVChange (if playing)
    Player: onPlayBackEnded (if playing)
    Player: onAVStarted
    Player: onPlayBackSeek (if seek)
    Player: onAVChange (if changed)
    Player: onPlayBackError
    Player: onPlayBackEnded
    Player: onPlayBackStopped
    """
    
    def __init__(self, monitor=None, service=None):
        xbmc.Player.__init__(self)
        self.monitor     = monitor
        self.service     = service
        self.jsonRPC     = service.jsonRPC
        self.pendingItem = {}
        self.playingItem = {}
        # imports.47: transition-supervisor state. _play_started is stamped by
        # onAVStarted and read by _onChange to measure how long the just-ended
        # programme actually ran (loop-breaker short-play detection). _short_plays
        # is the consecutive-short-play counter; resets on a normal-length play.
        self._play_started = 0
        self._short_plays  = 0
        # madteevee (imports fork, imports.60): Rollback_Watched as an exact undo.
        # Every decision (what to snapshot, who owns it, what to send) lives in
        # playstate_helpers.PlaystateTracker, which has its own lock; this class
        # only supplies the Kodi I/O (_readPlaystateDetails, _playstateFile,
        # sendJSON) and calls _syncPlaystate from the player events.
        self.playstate = playstate_helpers.PlaystateTracker(read_details=self._readPlaystateDetails,
                                                            playing_file=self._playstateFile,
                                                            send=self.jsonRPC.sendJSON,
                                                            log=self._playstateLog)
        # madteevee (imports fork, imports.62): restart from the beginning (restart_helpers).
        # _play_epoch counts playback changes (started, AV started, ended, stopped, error),
        # so the pop-up and a restart can tell that what they were for no longer plays.
        # _retune marks the next tune after a Stop (the same channel tuned again).
        # _pendingRestart is a guide-entry restart of a Custom channel waiting for its tune.
        # _restart_lock lets one restart run at a time (@threadit alone doesn't serialize).
        # _play_stops / _play_errors count stop and error events, so a LiveTV restart can
        # tell a viewer's Stop from a replay that failed to open.
        self._play_epoch     = 0
        self._retune         = False
        self._pendingRestart = None
        self._restart_lock   = Lock()
        self._play_stops     = 0
        self._play_errors    = 0


    def log(self, msg, level=xbmc.LOGDEBUG):
        return log('%s: %s'%(self.__class__.__name__,msg),level)


    def onPlayBackStarted(self):
        self._play_epoch += 1 # madteevee (imports fork, imports.62)
        self.pendingItem.update({'invoked':-1,'pending':False,'item':{},'last':self.pendingItem.get('item',{})})
        self.log('onPlayBackStarted, pendingItem = %s'%(self.pendingItem))


    def onAVChange(self):
        self.pendingItem.update({'resume':-1,'seek':-1,'item':{}})
        self.log('onAVChange, playingItem = %s'%(self.playingItem))

        
    def onAVStarted(self):
        # _tune_ts filter (madteevee fork): drop stale onAVStarted events from aborted fast-zap
        # tunes. Kodi back-fills onAVStarted in load-finish order, not user-zap order, so the
        # LAST event often corresponds to the FIRST tune in a CH+/CH- burst. Without this guard
        # player state anchors to the wrong channel; the overlay logo locks to it; and end-of-
        # programme PlayMedia(callback) routes to the wrong channel. _max_tune_ts is monotonic
        # and survives across onPlayBackStarted (which clears item/last but leaves it intact).
        newItem = self.getplayingItem()
        if newItem.get('isPseudoTV', False):
            new_ts = newItem.get('_tune_ts', 0)
            cur_ts = self.pendingItem.get('_max_tune_ts', 0)
            if new_ts and cur_ts and new_ts < cur_ts:
                self.log('onAVStarted, IGNORING stale event: _tune_ts=%.3f < max=%.3f (chid=%s, vid=%s)'%(new_ts, cur_ts, newItem.get('chid'), newItem.get('vid')), xbmc.LOGWARNING)
                return
            if new_ts > cur_ts:
                self.pendingItem['_max_tune_ts'] = new_ts
        self.pendingItem.update({'invoked':-1,'pending':False,'item':newItem})
        # imports.47: stamp the play-start timestamp so the loop-breaker can later
        # measure how long this programme actually played. Set AFTER the _tune_ts
        # stale-event early-return above so aborted fast-zap tunes don't pollute it.
        self._play_started = time.time()
        self._play_epoch  += 1 # madteevee (imports fork, imports.62): after the stale-event filter
        self.log('onAVStarted, pendingItem = %s'%(self.pendingItem))
        self._onPlay(self.pendingItem.get('item',{}), self._play_epoch)
        
                
    def onPlayBackSeek(self, seek_time=None, seek_offset=None): #Kodi bug? `OnPlayBackSeek` no longer called by player during seek, issue limited to pvr?
        self.pendingItem.update({'invoked':-1,'pending':False,'item':{}})
        self.log('onPlayBackSeek, seek_time = %s, seek_offset = %s, playingItem = %s'%(seek_time,seek_offset,self.playingItem))
    
    
    def onPlayBackError(self):
        self._play_epoch += 1 # madteevee (imports fork, imports.62)
        self._play_errors += 1
        self.pendingItem.update({'invoked':-1,'pending':False,'item':{}})
        self.log('onPlayBackError, pendingItem = %s'%(self.pendingItem))
        self._onError(self.playingItem)


    def onPlayBackEnded(self):
        self._play_epoch += 1 # madteevee (imports fork, imports.62)
        self.pendingItem.update({'invoked':-1,'pending':False,'item':{}})
        self.log('onPlayBackEnded, pendingItem = %s'%(self.pendingItem))
        self._onChange(self.playingItem)


    def onPlayBackStopped(self):
        # madteevee (imports fork, imports.62): a tune after a Stop may offer the Restart
        # pop-up even on the same channel (programme changes end, they don't stop).
        self._play_epoch += 1
        self._play_stops += 1
        self._retune      = True
        self.pendingItem.update({'invoked':-1,'pending':False,'item':{}})
        self.log('onPlayBackStopped, pendingItem = %s'%(self.pendingItem))
        self._onStop(self.playingItem)


    def getplayingItem(self):
        playingItem = FileAccess._decodeString(self.getPlayerItem().getProperty('sysInfo'))
        # _decodeString returns '' (str) when the listitem has no sysInfo property — the case for
        # every non-pseudotv playback (Kodi library file, regular IPTV, addon-as-source, etc.).
        # Without this guard, the .get('chid','') call below raises AttributeError every time
        # onAVStarted fires for non-pseudotv content, and the resulting CPythonInvoker exception
        # corrupts service-thread state (downstream saveChanges/builder flows then deadlock).
        # Return an empty dict so callers see playingItem.get('isPseudoTV', False) == False and
        # take the non-pseudotv path cleanly.
        if not isinstance(playingItem, dict):
            return {}
        if '@%s'%(PSEUDOTV_SLUG) in playingItem.get('chid',''):
            playingItem['isPseudoTV'] = True
            playingItem['chfile']     = BUILTIN.getInfoLabel('Filename','Player')
            playingItem['chfolder']   = BUILTIN.getInfoLabel('Folderpath','Player')
            playingItem['chpath']     = BUILTIN.getInfoLabel('Filenameandpath','Player')
            playingItem['isfiller']   = self.isPlayingFiller()
            #playingItem from listitem maybe outdated, check with channels.json for fresh citem.
            playingItem.update({'fitem':combineDicts(playingItem.get('fitem',{}),Globals._decodePlot(BUILTIN.getInfoLabel('Plot','VideoPlayer')))})
            playingItem.update({'nitem':combineDicts(playingItem.get('nitem',{}),Globals._decodePlot(BUILTIN.getInfoLabel('NextPlot','VideoPlayer')))})
            playingItem.update({'citem':combineDicts(playingItem.get('fitem',{}).get('citem',{}),next((item for item in self.service.channels if item.get('id',-1) == playingItem.get('chid',0)),{}))})
            playingItem.get('fitem',{})['runtime'] = self.getPlayerTime()
            PROPERTIES.setProperty('lastPlayed.sysInfo',FileAccess._encodeString(playingItem))
        return playingItem
      
        
    def getPlayerItem(self):
        try: return self.getPlayingItem()
        except Exception:
            if not self.isPlaying(): return xbmcgui.ListItem()
            self.monitor.waitForAbort(1.0) 
            return self.getPlayerItem()
        
        
    def getPlayerFile(self):
        if self.isPlaying(): return self.getPlayingFile()
        else:                return self.playingItem.get('fitem',{}).get('file')


    def getPlayerTime(self):
        if self.isPlaying(): return (self.getTimeLabel('Duration') or self.getTotalTime())
        else:                return (self.playingItem.get('fitem',{}).get('runtime') or -1)
            
       
    def getPlayedTime(self):
        if self.isPlaying(): return (self.getTimeLabel('Time') or self.getTime()) #getTime retrieves Guide times not actual media time.
        else:                return -1
       
            
    def getRemainingTime(self):
        if self.isPlaying(): return self.getPlayerTime() - self.getPlayedTime()
        else:                return self.getTimeLabel('TimeRemaining')


    def getPlayerProgress(self):
        if self.isPlaying(): return abs(int((self.getRemainingTime() / self.getPlayerTime()) * 100) - 100)
        else:                return int((BUILTIN.getInfoLabel('Progress','Player') or '-1'))


    def getTimeLabel(self, prop: str='TimeRemaining') -> int and float: #prop='EpgEventElapsedTime'
        if self.isPlaying(): return timeString2Seconds(BUILTIN.getInfoLabel('%s(hh:mm:ss)'%(prop),'Player'))
        else:                return -1


    def isPlayingFiller(self):
        if self.isPlaying(): return isFiller({'genre':BUILTIN.getInfoLabel('Genre(comma)','VideoPlayer').split(',')})
        else:                return isFiller(self.playingItem.get('fitem',{}))
        
        
    def isNextFiller(self):
        if self.isPlaying(): return isFiller({'genre':BUILTIN.getInfoLabel('NextGenre(comma)','VideoPlayer').split(',')})
        else:                return isFiller(self.playingItem.get('nitem',{}))


    def isPseudoTV(self):
        return (self.playingItem.get('isPseudoTV') or False)


    def isPlayingPseudoTV(self):
        return (self.isPlaying() & self.isPseudoTV())


    def setSubtitles(self, state: bool=True):
        # madteevee: branches by ForceSubtitles Mode. 'legacy' keeps the
        # original behavior (read state arg, gate on HasSubtitles infolabel).
        # 'always_on' / 'always_off' apply a fixed policy regardless of state arg.
        # 'smart' reads the per-item plan written at build time and applies
        # audio/sub stream selection + visibility in one Player.GetProperties
        # roundtrip (retried up to 3x / 200ms when stream lists arrive late
        # post-onAVStarted). If no plan is present (e.g. fitem lacks
        # streamdetails — fillers, recent-add lag), falls through to legacy.
        mode = getattr(self, 'smartSubsMode', 'legacy')
        if mode == 'always_on':
            has = BUILTIN.hasSubtitle()
            self.log('setSubtitles, mode=always_on hasSubtitle=%s'%(has))
            self.showSubtitles(bool(has))
            return
        if mode == 'always_off':
            self.log('setSubtitles, mode=always_off')
            self.showSubtitles(False)
            return
        if mode == 'smart':
            plan = ((self.playingItem.get('fitem') or {}).get('smart_subs') or None)
            if plan:
                self.log('setSubtitles, mode=smart APPLYING plan=%s'%(plan))
                self._applySmartSubsFromPlan(plan)
                return
            self.log('setSubtitles, mode=smart but no plan on fitem; falling through to legacy')
        # legacy
        hasSubtitle = BUILTIN.hasSubtitle()
        self.log('setSubtitles, mode=legacy state=%s hasSubtitle=%s'%(state, hasSubtitle))
        if not hasSubtitle: state = False
        self.showSubtitles(state)


    def _applySmartSubsFromPlan(self, plan):
        """
        Smart subtitle/audio application, driven by the per-item plan written at
        BUILD_FILELIST_POST by the ForceSubtitles rule. Reads available streams
        from Kodi via Player.GetProperties (one RPC with retry, since stream
        lists can lag onAVStarted by a few hundred ms), then issues at most one
        setAudioStream + one setSubtitleStream + one showSubtitles call.

        playerid is hardcoded to 1: this is the video player. _onPlay (the only
        caller path here) fires from onAVStarted on the video player.
        """
        try:
            # Fast paths that need no stream-list inspection
            sub_state         = bool(plan.get('sub_state'))
            sub_lang_target   = (plan.get('sub_lang_target')   or '').lower()
            audio_lang_target = (plan.get('audio_lang_target') or '').lower()
            if not sub_state and not audio_lang_target and not sub_lang_target:
                self.log('_applySmartSubsFromPlan, no-stream-info path: subs OFF', xbmc.LOGINFO)
                self.showSubtitles(False)
                return

            # Pull stream info — retry up to 3x with waitForAbort to respect shutdown
            props = None
            for attempt in range(3):
                resp = self.jsonRPC.sendJSON({
                    'method': 'Player.GetProperties',
                    'params': {
                        'playerid': 1,
                        'properties': ['audiostreams','currentaudiostream','subtitles','currentsubtitle','subtitleenabled'],
                    },
                })
                props = (resp or {}).get('result') or {}
                if props.get('audiostreams') or props.get('subtitles'):
                    break
                if self.service.monitor.waitForAbort(0.2): return

            if not props:
                self.log('_applySmartSubsFromPlan, Player.GetProperties returned empty after retries; falling through to legacy', xbmc.LOGWARNING)
                self.showSubtitles(sub_state if BUILTIN.hasSubtitle() else False)
                return

            from lang_codes import normalize_lang

            audio_streams = (props.get('audiostreams') or [])
            sub_streams   = (props.get('subtitles')   or [])
            self.log('_applySmartSubsFromPlan, runtime audio streams: %s'
                     % ([(s.get('index'), s.get('language'), s.get('name')) for s in audio_streams]))
            self.log('_applySmartSubsFromPlan, runtime sub streams:   %s'
                     % ([(s.get('index'), s.get('language'), s.get('name'), s.get('isforced')) for s in sub_streams]))

            # Audio switch (idempotent — skip when target already current)
            if audio_lang_target:
                cur = (props.get('currentaudiostream') or {})
                cur_lang = normalize_lang(cur.get('language'))
                if cur_lang != audio_lang_target:
                    idx = self._findStreamIdxByLang(audio_streams, audio_lang_target)
                    if idx is not None and 0 <= idx < len(audio_streams):
                        self.setAudioStream(idx)
                        self.log('_applySmartSubsFromPlan, switched audio: %s -> %s (idx=%s)'%(cur_lang, audio_lang_target, idx))

            # Sub stream + visibility
            if sub_state:
                if sub_lang_target:
                    idx = self._findStreamIdxByLang(sub_streams, sub_lang_target)
                    if idx is not None and 0 <= idx < len(sub_streams):
                        cur_sub = (props.get('currentsubtitle') or {})
                        cur_sub_idx = cur_sub.get('index') if isinstance(cur_sub, dict) else None
                        if cur_sub_idx != idx:
                            self.setSubtitleStream(idx)
                            self.log('_applySmartSubsFromPlan, set sub stream lang=%s idx=%s (was idx=%s)' % (sub_lang_target, idx, cur_sub_idx))
                self.showSubtitles(True)
                self.log('_applySmartSubsFromPlan, subs ON (lang=%s, rationale=%s)' % (sub_lang_target or '(default)', plan.get('rationale','')))
            else:
                self.showSubtitles(False)
                self.log('_applySmartSubsFromPlan, subs OFF (rationale=%s)'%(plan.get('rationale','')))
        except Exception as e:
            self.log('_applySmartSubsFromPlan, failed: %s'%(e), xbmc.LOGERROR)
            # graceful degrade
            try: self.showSubtitles(bool(plan.get('sub_state')))
            except Exception: pass


    @staticmethod
    def _findStreamIdxByLang(streams, target):
        if not target or not streams: return None
        from lang_codes import normalize_lang
        # Prefer isforced match for subtitles when multiple streams share the language
        forced_idx = None
        for s in streams:
            if normalize_lang(s.get('language','')) == target:
                if s.get('isforced'):
                    if forced_idx is None: forced_idx = s.get('index')
                else:
                    return s.get('index')
        return forced_idx


    @threadit
    def _onPlay(self, playingItem={}, epoch=None):
        self.log('_onPlay, playingItem = %s'%(playingItem))
        self.toggleInfo(False)
        self.toggleBackground(False)
        # madteevee (imports fork): split the overlay close into two cases to
        # avoid a race that left the previous channel's logo stuck on screen
        # during PseudoTV→PseudoTV zaps. Previously, `toggleOverlay(False)`
        # was the FIRST thing _onPlay did. Since _onPlay is @threadit (runs
        # on a separate thread), the main thread's __chkOverlay poll could
        # land between this close and the `self.playingItem = ...` update
        # below — at that moment self.overlay was None, the STALE
        # self.playingItem still had isPseudoTV=True with the OLD citem, so
        # __chkOverlay built a fresh Overlay snapshotting the OLD citem.
        # That overlay was the WRONG logo. The v.26 chid-aware idempotency
        # then locked it in (subsequent toggleOverlay calls saw
        # `_overlay_chid == current_chid` because both reflected the same
        # stale playingItem at the wrong moment).
        # Fix: for the PseudoTV→non-PseudoTV transition the close still
        # needs to happen up here (__chkOverlay never tells it to close
        # when state=True). For PseudoTV→PseudoTV the close is deferred
        # to AFTER playingItem updates, so __chkOverlay's chid-mismatch
        # branch closes and rebuilds with fresh data.
        if not playingItem.get('isPseudoTV'):
            # madteevee: extension to the v.26 zap-race fix. For PseudoTV→non-
            # PseudoTV transitions (IPTV import, addon source, library file)
            # we MUST update self.playingItem to the new (non-PseudoTV) item
            # BEFORE closing the overlay. Otherwise __chkOverlay (polling on
            # the idle thread) can land between the close below and a later
            # self.playingItem write: at that moment self.overlay = None AND
            # self.playingItem still has isPseudoTV=True with the OLD citem,
            # so __chkOverlay opens a new Overlay capturing the OLD logo.
            # That overlay then sits over the IPTV playback indefinitely —
            # and survives further channel tunes because subsequent
            # toggleOverlay calls see _overlay_chid matching the OLD chid
            # (the stale playingItem.citem at the moment Overlay was built).
            # The v.26 fix addressed the analogous PseudoTV→PseudoTV race;
            # this closes the symmetric gap for the non-PseudoTV destination.
            oldInfo = self.playingItem
            self.playingItem = playingItem
            # madteevee (imports fork, imports.60): PseudoTV handed over to other
            # playback. Kodi reuses the running player for the new file and sends
            # no stop, so the last PseudoTV programme is rolled back here (restore
            # only; a no-op when _onStop already did it).
            if oldInfo.get('isPseudoTV'): self._syncPlaystate(oldInfo)
            self.toggleOverlay(False)
            self.lastSubState = BUILTIN.isSubtitle()
            # madteevee (imports fork, imports.62): the Restart pop-up for LiveTV channels
            # that can replay their show on now. LiveTV follows the global setting
            # (channel rule 54 belongs to Custom channels).
            self._retune = False
            self.toggleReplay(bool(SETTINGS.getSettingInt('Replay_Percentage')), epoch)
            # imports.16: non-PTV path now early-returns. Previously the
            # method fell through to a redundant `else: self.playingItem =
            # playingItem` at the bottom — a second write of the same value.
            # Cleared stale pseudotv state then; cleared above the toggle now.
            return
        # PseudoTV path — non-PTV returned above
        self.lastSubState = BUILTIN.isSubtitle()
        retune, self._retune = self._retune, False # madteevee (imports fork, imports.62): see onPlayBackStopped
        oldInfo = self.playingItem
        newChan = oldInfo.get('chid',random.random()) != playingItem.get('chid')
        self.log('_onPlay, [%s], mode = %s, isPlaylist = %s, new channel = %s'%(playingItem.get('citem',{}).get('id'), playingItem.get('mode'), playingItem.get('isPlaylist',False), newChan))
        if newChan: #New channel
            # madteevee (imports fork, imports.62): start from the global value, so a
            # previous channel's rule 54 ('Restart Button') never carries over.
            self.replayPercentage = SETTINGS.getSettingInt('Replay_Percentage')
            self.runActions  = RulesList([playingItem.get('citem',{})]).runActions
            self.playingItem = self._runActions(RULES_ACTION_PLAYER_START, playingItem.get('citem',{}), playingItem, inherited=self)
            # madteevee (imports fork, imports.60): roll back the previous programme and
            # snapshot this one. Started before the subtitle/overlay work so the snapshot
            # is read right after playback starts; the switch value is the one this
            # channel's start rules (rule 53) just applied.
            self._syncPlaystate(oldInfo, playingItem, bool(self.rollbackPlaycount))
            PROPERTIES.setTrakt(self.disableTrakt)
            self.setSubtitles(self.lastSubState) #todo allow rules to set sub preference per channel.
            self.toggleReplay(bool(self.replayPercentage), epoch)
            # Post-playingItem-update overlay refresh: now that
            # self.playingItem reflects the new channel, ask the overlay
            # to close-and-rebuild. The chid-aware check in toggleOverlay
            # will detect the mismatch with _overlay_chid (still set to
            # the OLD channel) and rebuild with fresh citem.
            self.toggleOverlay(self.enableOverlay)
        else: #New Program/Same Channel
            self.playingItem = playingItem
            self._syncPlaystate(oldInfo, playingItem, bool(self.rollbackPlaycount)) # madteevee (imports fork, imports.60): see the new-channel branch
            if playingItem.get('radio',False):
                BUILTIN.executebuiltin('Action(back)')
                BUILTIN.executewindow('ReplaceWindow(visualisation)')
            elif playingItem.get('isPlaylist',False):
                BUILTIN.executewindow('ReplaceWindow(fullscreenvideo)')
            # madteevee: per-program re-evaluation of smart subtitle/audio
            # policy. Gated to smart mode to keep legacy behavior identical
            # — without this, smart channels with mixed-language content
            # (e.g. anime ch with JA + EN-dubbed episodes interleaved) would
            # only re-evaluate at channel tune, not at each program transition.
            if getattr(self, 'smartSubsMode', 'legacy') == 'smart':
                self.setSubtitles(self.lastSubState)
            self.toggleInfo(self.infoOnChange)
            # madteevee (imports fork, imports.62): the same channel tuned again after a
            # Stop offers the Restart pop-up like any tune. The add-on's own programme
            # changes end the previous file (no Stop), so they never offer it.
            if retune: self.toggleReplay(bool(self.replayPercentage), epoch)
        # madteevee (imports fork, imports.62): a guide-entry restart of this channel was
        # waiting for this tune: jump to the start of the programme.
        self._restartPending(playingItem)
        # madteevee (imports fork, imports.60): quePlaycount removed (the rollback runs in
        # _syncPlaystate above); _setRuntime now only caches the player's runtime.
        self.jsonRPC._setRuntime(playingItem.get('fitem',{}),playingItem.get('fitem',{}).get('runtime'))


    @threadit
    def _onChange(self, playingItem={}):
        self.log('_onChange, playingItem = %s'%(playingItem))
        # madteevee (imports fork, imports.60): the programme reached its end. Kodi has
        # already saved its marks for the file, so roll it back now (restore only)
        # instead of waiting for the next playback.
        if playingItem.get('isPseudoTV'): self._syncPlaystate(playingItem)
        self.toggleOverlay(False)
        if playingItem:
            if not playingItem.get('isPlaylist',False):
                self.toggleBackground(self.enableOverlay)
                # v.46: guard against PlayMedia(None) after a stream-stall onPlayBackEnded.
                # When a VOD stream stalls mid-playback, Kodi fires onPlayBackEnded with the
                # listitem still in playingItem but `callback` never populated — _onIdle's
                # __chkCallback recovery (services.py:323) only runs in periodic ticks, not
                # on the synchronous EOF path. The bare `PlayMedia(%s)%None` formatting
                # produced the literal string "None" as the URL; Kodi logged
                # `VideoPlayer::OpenFile: None` → `OpenInputStream - error opening [None]`
                # → DialogConfirm "Playback failed. One or more items failed to play".
                # Fix: try the same getCallback recovery __chkCallback uses, and skip
                # PlayMedia entirely if it still resolves to falsy. The channel goes to
                # the background overlay instead of popping a user-facing error dialog.
                callback = playingItem.get('callback')
                if not callback:
                    callback = self.jsonRPC.getCallback(playingItem)
                    if callback: playingItem['callback'] = callback
                if callback:
                    # imports.47: transition supervisor — runaway loop-breaker + watchdog arm.
                    # Loop-breaker accounting: for PseudoTV non-filler items, measure how long
                    # the just-ended programme actually played (now - _play_started); a play
                    # shorter than Seek_Tolerance counts as "short" and increments _short_plays.
                    # Fillers are excluded (legitimately short) and so is non-PseudoTV content
                    # (so the supervisor never engages on library/IPTV playback).
                    if playingItem.get('isPseudoTV') and not playingItem.get('isfiller', False) and self._play_started > 0:
                        played = time.time() - self._play_started
                        if played < SETTINGS.getSettingInt('Seek_Tolerance'):
                            self._short_plays += 1
                            self.log('_onChange, [%s] short play: %.1fs < Seek_Tolerance — _short_plays=%s'%(playingItem.get('citem',{}).get('id'), played, self._short_plays))
                        else:
                            self._short_plays = 0
                    if playingItem.get('isPseudoTV') and self._short_plays >= TRANSITION_LOOP_THRESHOLD:
                        # Loop-breaker engaged: do NOT fire PlayMedia immediately. _chkTransition
                        # will fire it after the Playback_Timeout cooldown — calm spaced retries
                        # that naturally wait out the dead zone between a short file's end and
                        # its EPG slot's end. retune_attempts=0 because the watchdog cap is for
                        # true stalls (no onAVStarted), not the loop-breaker case where each
                        # cycle's onAVStarted resets it.
                        self.pendingItem.update({'invoked': time.time(), 'retune_cb': callback, 'retune_attempts': 0})
                        self.log('_onChange, [%s] runaway: %s consecutive short plays — deferring re-tune via Playback_Timeout cooldown'%(playingItem.get('citem',{}).get('id'), self._short_plays), xbmc.LOGWARNING)
                    else:
                        BUILTIN.executebuiltin('PlayMedia(%s)'%(callback))
                        self.log('_onChange, [%s], isPlaylist = %s, callback = %s'%(playingItem.get('citem',{}).get('id'),playingItem.get('isPlaylist',False),callback))
                        # Watchdog arm: only for PseudoTV items (non-PTV transitions stay byte-identical
                        # to pre-imports.47 — no arm, no _chkTransition involvement). Normal transitions
                        # auto-disarm in ~1s when onPlayBackStarted/onAVStarted reset invoked=-1.
                        if playingItem.get('isPseudoTV'):
                            self.pendingItem.update({'invoked': time.time(), 'retune_cb': callback, 'retune_attempts': 0})
                else:
                    self.log('_onChange, [%s], skipping PlayMedia: callback unresolved (would have been PlayMedia(None))'%(playingItem.get('citem',{}).get('id')), xbmc.LOGWARNING)
            self._runActions(RULES_ACTION_PLAYER_CHANGE, playingItem.get('citem',{}), playingItem, inherited=self)
        else:
            self.toggleBackground(False)
        
        
    @threadit
    def _onError(self, playingItem={}): #todo evaluate potential for error handling.
        self.log('_onError, playingItem = %s'%(playingItem))
        if self.isPseudoTV() and SETTINGS.getSettingBool('Debug_Enable'):
            DIALOG.notificationDialog(LANGUAGE(32000))
            self._onStop(playingItem)
            
           
    @threadit 
    def _onStop(self, playingItem={}):
        self.log('_onStop, playingItem = %s'%(playingItem))
        self.toggleInfo(False)
        self.toggleOverlay(False)
        self.toggleBackground(False)
        if playingItem:
            PROPERTIES.setTrakt(False)
            if playingItem.get('isPlaylist',False): xbmc.PlayList(xbmc.PLAYLIST_VIDEO).clear()
            # madteevee (imports fork, imports.60): replaces quePlaycount. Restore only:
            # rolls the programme back if it owns a snapshot.
            if playingItem.get('isPseudoTV'): self._syncPlaystate(playingItem)
            self._runActions(RULES_ACTION_PLAYER_STOP, playingItem.get('citem',{}), playingItem, inherited=self)
        
            
    def _runActions(self, action, citem={}, parameter=None, inherited=None):
        if self.runActions: return self.runActions(action, citem, parameter, inherited)
        else:               return parameter


    # madteevee (imports fork, imports.60): Rollback_Watched Kodi glue. The logic
    # is in playstate_helpers.PlaystateTracker (unit-tested without Kodi).
    @threadit
    def _syncPlaystate(self, oldInfo=None, newInfo=None, allow=False):
        # Own thread, not the service queue: channel builds and tunes block the
        # queue, and a snapshot taken late could capture Kodi's post-play marks.
        # The tracker's lock serializes overlapping events.
        self.playstate.transition(oldInfo, newInfo, allow)


    def _playstateFile(self, playingItem=None):
        # The programme's file, only when Kodi is playing exactly that file now:
        # catch-up clicks, catchup="vod" channels and playlist mode. Live
        # programmes play as pvr://...pvr items, which Kodi never marks (no
        # snapshot, no JSON-RPC). getPlayingItem().getPath() is the item's own
        # path; getPlayingFile() returns Kodi's resolved path and would let live
        # programmes through. plugin:// and resource paths are never looked up.
        playingItem = playingItem or {}
        file = (playingItem.get('fitem') or {}).get('file') or ''
        if not file or playingItem.get('radio',False) or file.startswith(tuple(VFS_TYPES)): return None
        try:
            if self.isPlayingVideo() and self.getPlayingItem().getPath() == file: return file
        except Exception as e: self.log('_playstateFile, playback changed during the check: %s'%(e)) # e.g. stopped between the two calls
        return None


    @threadit
    def _cancelPlaystate(self):
        # Own thread: Monitor.onSettingsChanged runs on the service's main thread,
        # which must not wait for the tracker lock while a transition talks to Kodi.
        self.playstate.clear()


    def _readPlaystateDetails(self, file):
        # delay=False: the tracker checks the player right after this read; the
        # RPC_Delay pause would sit between the two (writes keep the pause).
        return self.jsonRPC.getFileDetails(file, 'video', list(playstate_helpers.PLAYSTATE_PROPERTIES), cache=False, delay=False)


    def _playstateLog(self, msg, level=playstate_helpers.DEBUG):
        self.log(msg, {playstate_helpers.INFO:xbmc.LOGINFO, playstate_helpers.ERROR:xbmc.LOGERROR}.get(level, xbmc.LOGDEBUG))


    def _onSleep(self):
        self.log('_onSleep')
        sec = 0
        cnx = False
        inc = int(100/15)
        xbmc.playSFX(NOTE_WAV)
        dia = DIALOG.progressDialog(message=LANGUAGE(30078))
        while not self.monitor.abortRequested() and (sec < 15):
            sec += 1
            msg = '%s\n%s'%(LANGUAGE(32039),LANGUAGE(32040)%(15-sec))
            dia = DIALOG.progressDialog((inc*sec),dia, msg)
            if self.service._shutdown(1.0) or dia is None:
                cnx = True
                break
        DIALOG.progressDialog(100,dia)
        return not bool(cnx)


    def _chkCallback(self):
        # imports.16: CAS-gated callback resolution. This is the ONLY site
        # outside _onPlay where an idle-thread method mutates self.playingItem
        # in place. The pattern: snapshot the dict ref, resolve callback
        # against that snapshot, publish only if self.playingItem hasn't been
        # swapped under us by an _onPlay worker. If swapped, drop the write;
        # the next idle tick will retry against the fresh state.
        # Lock-free CAS analog — matches Kodi addon convention (GIL-reliance
        # over explicit locks). Replaces the prior nested __chkCallback
        # closure inside _onIdle, which wrote unconditionally and could
        # contaminate a freshly-tuned playingItem with a stale-context
        # callback resolution.
        state = self.playingItem
        if state.get('callback'): return
        callback = self.jsonRPC.getCallback(state)
        if self.playingItem is state:
            state['callback'] = callback
            self.log('_chkCallback, callback = %s'%(callback))
            PROPERTIES.setProperty('lastPlayed.sysInfo', FileAccess._encodeString(state))
        else:
            self.log('_chkCallback, state swapped under us; dropping write (next tick will retry)', xbmc.LOGINFO)


    def _chkTransition(self):
        # imports.47: channel-transition supervisor (watchdog + loop-breaker recovery).
        # Called every ~1s from Monitor._chkIdle's not-playing branch — i.e. it runs
        # during the between-programmes gap where _onIdle (and the dead __chkPlayback
        # it used to host) cannot reach.
        #
        # Two armers in _onChange set pendingItem['invoked']:
        #   1. Watchdog — a normal PseudoTV transition fired PlayMedia and we want
        #      a safety net in case Kodi sits on it (the BUG 1 stall class).
        #   2. Loop-breaker — _onChange detected a runaway (>=3 consecutive short
        #      plays: file ends before its slot does) and deferred the re-tune
        #      instead of firing PlayMedia; _chkTransition fires it after the
        #      Playback_Timeout cooldown, breaking the flicker storm into calm
        #      spaced retries that naturally wait out the dead zone.
        #
        # Auto-disarms (no-op) when:
        #   - pendingItem['invoked'] <= 0 (no transition pending; also clears the
        #     retune_attempts counter so the next genuine arm starts from zero)
        #   - isPlaying() (playback in progress — the relevant onXxx callback
        #     resets invoked=-1; we defensively re-check here too)
        #   - BUILTIN.isBusyDialog() (don't fight a system busy state)
        #   - elapsed < Playback_Timeout (not yet stale)
        #
        # Concurrency: pendingItem is already touched without a lock by Kodi's
        # player-event thread (callbacks), the @threadit workers (_onChange), and
        # this idle-thread method — consistent with the codebase's GIL-reliance
        # (cf. _chkCallback at line 522). The only meaningful race is "playback
        # just started in the ~1s window before we fire" — we re-check isPlaying()
        # immediately before executebuiltin to narrow it; a residual redundant
        # re-tune of the same channel is bounded and benign.
        inv = self.pendingItem.get('invoked', -1)
        if inv <= 0:
            if self.pendingItem.get('retune_attempts', 0) != 0:
                self.pendingItem['retune_attempts'] = 0
            return
        if self.isPlaying(): return
        if BUILTIN.isBusyDialog(): return
        timeout = SETTINGS.getSettingInt('Playback_Timeout') or 45
        if (time.time() - inv) < timeout: return
        attempts = self.pendingItem.get('retune_attempts', 0)
        if attempts >= TRANSITION_MAX_RETRIES:
            self.log('_chkTransition, giving up after %s recovery attempts — '
                     'channel left on background overlay; operator can change channel'
                     %(attempts), xbmc.LOGWARNING)
            self.pendingItem['invoked'] = -1
            DIALOG.notificationDialog(LANGUAGE(32000))
            return
        cb = self.pendingItem.get('retune_cb') or self.playingItem.get('callback')
        if not cb:
            self.log('_chkTransition, no retune_cb available — disarming', xbmc.LOGWARNING)
            self.pendingItem['invoked'] = -1
            return
        self.pendingItem['retune_attempts'] = attempts + 1
        self.pendingItem['invoked']         = time.time()
        self.log('_chkTransition, recovery re-fire #%s after %ss stall — PlayMedia(%s)'
                 %(attempts + 1, int(time.time() - inv), cb), xbmc.LOGWARNING)
        BUILTIN.executebuiltin('PlayMedia(%s)'%(cb))


    def _onIdle(self):
        # imports.47: the nested __chkPlayback that used to live here has been
        # removed — it was dead code (pendingItem['invoked'] was never positive
        # anywhere) AND its action (onPlayBackError → _onError → _onStop on debug)
        # would tear down the channel on a stall instead of recovering it. The
        # replacement is Player._chkTransition() above, called from Monitor._chkIdle
        # so it runs during the between-programmes gap (which _onIdle never could —
        # _chkIdle only invokes _onIdle while playing).

        def __chkBackground():
            remaining = floor(self.getRemainingTime())
            if floor(remaining) <= (OSD_TIMER*2):
                self.log('__chkBackground, remaining = %s'%(remaining))
                self.toggleBackground(self.enableOverlay)

        def __chkResumeTime():
            if not self.playingItem.get('isfiller',True) and self.playingItem.get('isPlaylist',False):
                if self.playingItem.get('fitem',{}).get('file') == self.getPlayingFile():
                    resume = {"position":self.getPlayedTime(),
                              "total":   self.getPlayerTime(),
                              "file" :   self.getPlayerFile(),
                              "updated":{'instance':PROPERTIES.getFriendlyName(),'time':clock_helpers.now()}} # imports.63: the real clock (PauseRule 'last viewed', shown by rules.py via epochTime)
                    self.playingItem.setdefault('resume',{}).update(resume)
                    self.log('__chkResumeTime, resume = %s'%(self.playingItem.get('resume')))

        def __chkOverlay():
            played = ceil(self.getPlayedTime())
            if not self.playingItem.get('isfiller',True) and played > self.minDuration: 
                self.toggleOverlay(self.enableOverlay)

        def __chkOnNext():
            played    = ceil(self.getPlayedTime())
            remaining = floor(self.getRemainingTime())
            totalTime = int(self.getPlayerTime() * (self.maxProgress / 100))
            threshold = abs((totalTime - (totalTime * .75)) - (ONNEXT_TIMER*3))
            intTime   = roundupDIV(threshold,3)
            if self.isPlayingPseudoTV() and not self.playingItem.get('isfiller',True) and not self.overlay is None:
                if played > self.minDuration and (remaining <= threshold and remaining >= intTime): 
                    self.log('__chkOnNext, played = %s, remaining = %s'%(played,remaining))
                    self.overlay.toggleOnNext(bool(self.OnNextMode))
                
        def __chkSleep():
            if self.sleepTime > 0 and (self.monitor.idleTime > (self.sleepTime * 10800)):
                if not PROPERTIES.isRunning('Player.__chkSleep'):
                    with PROPERTIES.chkRunning('Player.__chkSleep'):
                        if self._onSleep(): self.stop()
        
        __chkBackground()
        __chkResumeTime()
        self._chkCallback()
        __chkOverlay()
        __chkOnNext()
        __chkSleep()
            
                
    def toggleBackground(self, state: bool=SETTINGS.getSettingBool('Overlay_Enable')):
        if state and self.service.monitor.isIdle and self.background is None:
            if not self.overlay is None: self.toggleOverlay(False)
            BUILTIN.executebuiltin("Dialog.Close(all)")
            self.background = Background(BACKGROUND_XML, ADDON_PATH, "default", player=self)
            self.background.show()
        elif not state and hasattr(self.background,'close'):
            self.background = self.background.close()
        else: return
        self.log("toggleBackground, state = %s, background = %s"%(state,self.background))


    def toggleOverlay(self, state: bool=SETTINGS.getSettingBool('Overlay_Enable')):
        # v.26: chid-aware idempotency. The Overlay constructor at overlay.py:180
        # snapshots self.player.playingItem.get('citem') at __init__ — once. If
        # __chkOverlay's poll lands between _onPlay's toggleOverlay(False) and
        # _onPlay's self.playingItem update during a fast zap, the new Overlay
        # captures the PREVIOUS channel's citem (and logo). Without this guard,
        # subsequent toggleOverlay(True) calls see overlay != None and no-op,
        # leaving the wrong logo stuck on screen indefinitely. Track the chid
        # the overlay was opened for; if it mismatches the current playingItem's
        # chid, close and rebuild with fresh state. The fix is described in
        # CLAUDE.md memory and was originally master-fork commit 9be4969 — lost
        # during the rebase to upstream/nightly.
        #
        # imports.18: also serialize the entire toggleOverlay critical section
        # with _overlay_lock. v.26's chid check only fires when self.overlay is
        # not None; it doesn't protect the initial-create race where two
        # threads (e.g., _onPlay worker + __chkOverlay idle thread) both see
        # self.overlay = None, both create an Overlay, both `self.overlay = ...`.
        # The loser's Overlay python object is orphaned, but its channelBug
        # control was already added to xbmcgui.Window(12005) by open(). It
        # persists for the rest of the Kodi session, showing the wrong logo
        # over subsequent channels. Lock is held during Overlay.__init__ +
        # open() — both fast (no I/O, just python + xbmcgui control creation).
        with self._overlay_lock:
            if state and self.isPlayingPseudoTV():
                current_chid = self.playingItem.get('citem',{}).get('id')
                if self.overlay is not None:
                    if self._overlay_chid == current_chid:
                        return
                    self.overlay = self.overlay.close()
                self.overlay = Overlay(player=self)
                self.overlay.open()
                self._overlay_chid = current_chid
            elif not state and hasattr(self.overlay,'close'):
                self.overlay = self.overlay.close()
                self._overlay_chid = None
            else:
                return
            self.log("toggleOverlay, state = %s, overlay = %s, _overlay_chid = %s"%(state, self.overlay, self._overlay_chid))


    @debounceit(restart_helpers.POPUP_DELAY) # imports.63: was OSD_TIMER (about 5 s later)
    def toggleReplay(self, state: bool=False, epoch=None):
        # madteevee (imports fork, imports.62): offers the Restart pop-up for the playback
        # `epoch` (Custom and LiveTV channels). restart_helpers decides; the dialog is a
        # passive view that is always released when it closes (it used to stay set for
        # the whole Kodi session, so it could be offered once at most).
        if not state:
            self._closeReplay()
            return
        if self.replay is not None or epoch != self._play_epoch: return
        target = self._restartOffer()
        if not target or not self._waitRestartWindow(epoch): return
        if restart_helpers.popup_done(epoch, self._play_epoch, time.time(), target.get('end')): return
        dialog = Replay(REPLAY_XML, ADDON_PATH, "default", "1080i", player=self, epoch=epoch, end=target.get('end'))
        self.replay, chosen = dialog, False
        self.log("toggleReplay, offering %s"%(target))
        try:
            dialog.doModal()
            chosen = dialog.restart
        finally:
            self.replay = None
            del dialog
        self.log("toggleReplay, closed, restart = %s"%(chosen))
        if chosen: self.requestRestart({'source':'popup','target':target})


    @debounceit(OSD_TIMER)
    def toggleInfo(self, state: bool=SETTINGS.getSettingBool('Enable_OnInfo')):
        if state and self.isPlayingPseudoTV() and not self.playingItem.get('isfiller',True) and not BUILTIN.getInfoBool('IsVisible(fullscreeninfo)','Window'):
            BUILTIN.executewindow('ActivateWindow(fullscreeninfo)')
            timerit(self.toggleInfo)(float(OSD_TIMER),False)
        elif not state and BUILTIN.getInfoBool('IsVisible(fullscreeninfo)','Window'):
            BUILTIN.executebuiltin('Action(back)')
            BUILTIN.executebuiltin('Dialog.Close(fullscreeninfo)')
        else: return
        self.log('toggleInfo, state = %s'%(state))


    # madteevee (imports fork, imports.62): restart from the beginning. Every decision is
    # in restart_helpers (unit-tested without Kodi); these methods only read Kodi and act.
    def _closeReplay(self):
        # Asks an open Restart pop-up to close. One read of self.replay: toggleReplay's
        # finally (another thread) can clear it at any moment.
        replay = self.replay
        if replay is not None: replay.closing = True


    def _restartWait(self, done, limit, step=0.25):
        # True as soon as done() holds; False after `limit` s or at shutdown.
        waited = 0.0
        while not done():
            if waited >= limit or self.service._shutdown(step): return False
            waited += step
        return True


    def _restartOffer(self):
        # The pop-up's target, or None. Custom: the playing episode is >= Restart Percentage
        # and < Seek_Threshold through (not a filler or radio). LiveTV: a live channel whose
        # show on now is that far in and that Kodi says can be replayed.
        if self.isPlayingPseudoTV():
            if restart_helpers.custom_offer(self.playingItem, self.getPlayedTime(), self.getPlayerTime(),
                                            self.replayPercentage, self.maxProgress):
                return restart_helpers.custom_target(self.playingItem)
            return None
        if not BUILTIN.getInfoBool('IsPlayingTv','Pvr'): return None
        item = self.jsonRPC.getPlayerItemRef()
        if item.get('type') != 'channel': return None
        target = self._restartLiveTarget(item.get('id'), time.time(), offer=True)
        return target if target and target.get('playable') else None


    def _restartLiveTarget(self, channelid, now, offer=False):
        # hasarchive first, so the channels without replays never get a broadcastnow read
        # (their artwork links are signed).
        if not channelid or self.jsonRPC.getPVRChannelInfo(channelid, ('hasarchive',)).get('hasarchive') is not True: return None
        details = self.jsonRPC.getPVRChannelInfo(channelid, ('broadcastnow','hasarchive'))
        target  = restart_helpers.livetv_target(channelid, details, now, Globals._decodePlot,
                                                SETTINGS.getSettingInt('Replay_Percentage'), self.maxProgress, offer)
        if target: target['playable'] = self.jsonRPC.isBroadcastPlayable(target['broadcastid'])
        return target


    def _restartGuideTarget(self, path, now):
        # One read of every channel's show on now, the same data matchChannel reads at
        # every own-channel tune; the guide entry's path is matched against it.
        target = restart_helpers.guide_target(self.jsonRPC.getPVRChannelsNow(), path, now, Globals._decodePlot)
        if target and target.get('kind') == 'livetv': target['playable'] = self.jsonRPC.isBroadcastPlayable(target['broadcastid'])
        return target


    def _waitRestartWindow(self, epoch, limit=OSD_TIMER*2):
        # Waits (bounded) until full-screen video plays with Kodi's channel info, the
        # channel preview and any other dialog gone, while the same playback goes on.
        ready = self._restartWait(lambda: epoch != self._play_epoch or xbmc.getCondVisibility(restart_helpers.WINDOW_READY), limit)
        return ready and epoch == self._play_epoch


    def _restartTuneTs(self):
        # The playing ListItem's own tune stamp (default.py), to tell the programme the
        # service knows from a zap still in progress. None when unreadable.
        try:
            sysInfo = FileAccess._decodeString(self.getPlayingItem().getProperty('sysInfo'))
            return sysInfo.get('_tune_ts') if isinstance(sysInfo, dict) else None
        except Exception: return None


    def _restartFacts(self):
        video    = self.isPlayingVideo()
        pseudotv = self.isPlayingPseudoTV()
        return {'video':      video,
                'seekable':   BUILTIN.getInfoBool('SeekEnabled','Player'),
                'pvr_live':   BUILTIN.getInfoBool('IsPlayingTv','Pvr'),
                'pvr_epgtag': BUILTIN.getInfoBool('IsPlayingEpgTag','Pvr'),
                'item':       self.jsonRPC.getPlayerItemRef() if video else {},
                'pseudotv':   self.playingItem if pseudotv else None,
                'tune_ts':    self._restartTuneTs() if pseudotv else None}


    @threadit
    def requestRestart(self, request=None):
        # THE restart action, shared by the pop-up (B), the remote button (C) and the guide
        # entry (D). One at a time: @threadit alone does not serialize.
        if not self._restart_lock.acquire(False):
            self.log('requestRestart, a restart is already running; ignoring %s'%(request), xbmc.LOGINFO)
            return
        try: self._runRestart(request or {})
        except Exception as e: self.log('requestRestart, failed! %s'%(e), xbmc.LOGERROR)
        finally: self._restart_lock.release()


    def _runRestart(self, request):
        # A zap still settling ('tune in progress') is retried for a few seconds, so a
        # pressed button never ends without an answer.
        path  = request.get('path')
        guide = bool(path)
        for _ in range(restart_helpers.SETTLE_TRIES):
            now     = time.time()
            facts   = self._restartFacts()
            playing = restart_helpers.classify_playing(facts)
            target  = self._restartGuideTarget(path, now) if guide else request.get('target')
            action, reason = restart_helpers.decide(playing, target, now, guide)
            if action == restart_helpers.LOOKUP_LIVE:
                target = self._restartLiveTarget(playing.get('channelid'), now)
                if target: action, reason = restart_helpers.decide(playing, target, now)
                else:      action, reason = restart_helpers.UNAVAILABLE, 'no replay for the show on now'
            if action != restart_helpers.IGNORE: break
            if self.service._shutdown(restart_helpers.SETTLE_WAIT): return
        else: action, reason = restart_helpers.UNAVAILABLE, '%s (after %s tries)'%(reason, restart_helpers.SETTLE_TRIES)
        self.log('requestRestart, source = %s, playing = %s, action = %s (%s), target = %s'
                 %(request.get('source'), playing.get('kind'), action, reason, target), xbmc.LOGINFO)
        if action != restart_helpers.UNAVAILABLE: self._closeReplay()
        if   action == restart_helpers.SEEK_START:     self._restartSeek(guide)
        elif action == restart_helpers.OPEN_BROADCAST: self._restartOpen(target, facts.get('item') or {})
        elif action == restart_helpers.TUNE_CUSTOM:    self._restartTune(target)
        elif action == restart_helpers.RETUNE_CUSTOM:  self._restartTune(target, stop_first=True)
        elif action == restart_helpers.UNAVAILABLE:    self._restartNote()


    def _restartSeek(self, guide=False, wait=0.0):
        # Player.seekTime does nothing when the player can't seek and raises when nothing
        # plays. The guide entry also brings the video back to full screen, as Kodi's own
        # 'Play programme' does for the entry that already plays.
        if wait: self._restartWait(lambda: BUILTIN.getInfoBool('SeekEnabled','Player'), wait)
        try: self.seekTime(0)
        except Exception as e:
            self.log('_restartSeek, failed! %s'%(e), xbmc.LOGWARNING)
            return False
        if guide and not BUILTIN.getInfoBool('IsActive(fullscreenvideo)','Window'):
            BUILTIN.executewindow('ActivateWindow(fullscreenvideo)')
        return True


    def _restartOpen(self, target, before):
        # Kodi's own 'Play programme' (PlayEpgTag). Kodi doesn't tell Python that a replay
        # was refused, so the result is read from the player (restart_helpers.watch_outcome),
        # timed from the moment Player.Open returned.
        bid = target['broadcastid']
        if not self.jsonRPC.playerOpen({'item':{'broadcastid':bid}}):
            self.log('_restartOpen, broadcast %s: Player.Open refused'%(bid), xbmc.LOGINFO)
            return self._restartNote()
        t0, idle0, stops, errors = time.time(), BUILTIN.getIdle(), self._play_stops, self._play_errors
        before_key = (before.get('type'), before.get('id')) if before else None
        can_retune = before.get('type') == 'channel' and (before.get('id') or 0) > 0
        while not self.service._shutdown(restart_helpers.WATCH_POLL):
            playing = self.isPlaying()
            item    = self.jsonRPC.getPlayerItemRef() if playing else {}
            outcome = restart_helpers.watch_outcome({'elapsed':    time.time() - t0,
                                                     'replay':     playing and BUILTIN.getInfoBool('IsPlayingEpgTag','Pvr') and item.get('id') == bid,
                                                     'unchanged':  playing and before_key is not None and (item.get('type'), item.get('id')) == before_key,
                                                     'playing':    playing,
                                                     'busy':       BUILTIN.isBusyDialog(),
                                                     'idle':       BUILTIN.getIdle(),
                                                     'idle0':      idle0,
                                                     'stopped':    self._play_stops != stops,
                                                     'errored':    self._play_errors != errors,
                                                     'can_retune': can_retune})
            if outcome == 'wait': continue
            self.log('_restartOpen, broadcast %s: %s'%(bid, outcome), xbmc.LOGINFO)
            if outcome == 'retune': self.jsonRPC.playerOpen({'item':{'channelid':before['id']}})
            if outcome in ('refused','retune','failed'): self._restartNote()
            return


    def _restartTune(self, target, stop_first=False):
        # A Custom guide entry whose channel isn't playing that programme: tune the channel
        # (it joins mid-show), then _restartPending jumps to the start once it plays. When
        # Kodi plays that very channel, Player.Open {channelid} would only switch to full
        # screen, so it is stopped first.
        if stop_first:
            self.stop()
            if not self._restartWait(lambda: not self.isPlaying(), restart_helpers.STOP_WAIT): return self._restartNote()
        self._pendingRestart = {'chid':target['chid'], 'start':target['start'], 'ts':time.time()}
        if not self.jsonRPC.playerOpen({'item':{'channelid':target['channelid']}}):
            self._pendingRestart = None
            self._restartNote()


    def _restartPending(self, playingItem):
        # Called at the end of _onPlay's PseudoTV path.
        pending = self._pendingRestart
        if not pending: return
        now = time.time()
        if restart_helpers.pending_match(pending, playingItem, now):
            self._pendingRestart = None
            self.log('_restartPending, [%s] jumping to the start of the programme'%(pending.get('chid')), xbmc.LOGINFO)
            self._restartSeek(wait=2.0)
        elif restart_helpers.pending_expired(pending, now):
            self._pendingRestart = None


    def _restartNote(self):
        # 'Restart Unavailable...'. Shown even when 'Notify while playing' is off: it
        # answers a button the viewer pressed.
        DIALOG.notificationDialog(LANGUAGE(30154), show=True)


class Monitor(xbmc.Monitor):
    idleTime  = 0
    isIdle    = False
    isRunning = False
    
    def __init__(self, service=None):
        xbmc.Monitor.__init__(self)
        self.service = service
        self.jsonRPC = service.jsonRPC
        self.player  = Player(self,service)
        self.rollbackSwitch = SETTINGS.getSettingBool('Rollback_Watched') # madteevee (imports fork, imports.60): see _chkRollbackSwitch

        
    def log(self, msg, level=xbmc.LOGDEBUG):
        return log('%s: %s'%(self.__class__.__name__,msg),level)


    def chkIdle(self):
        self.idleThread = Thread(target=self._chkIdle)
        self.idleThread.daemon = True
        self.idleThread.start()
            
        
    def _chkIdle(self):
        if not self.isRunning:
            self.isRunning = True
            while not self.abortRequested():
                if   self.service._shutdown(0.5): break
                elif self.player.isPlayingPseudoTV():
                    self.idleTime = BUILTIN.getIdle()
                    self.isIdle   = bool(self.idleTime) | self.idleTime > OSD_TIMER
                    self.log('_chkIdle, isIdle = %s, idleTime = %s'%(self.isIdle, self.idleTime))
                    if self.isIdle: self.player._onIdle()
                else:
                    # imports.47: not-playing branch now drives the transition supervisor
                    # (watchdog stall-recovery + loop-breaker cooldown). _chkTransition is
                    # a fast no-op when no transition is pending — safe to call every tick.
                    # Keeps the second _shutdown wait so shutdown stays responsive and the
                    # not-playing loop cadence stays ~1s (matches pre-imports.47 timing).
                    self.player._chkTransition()
                    if self.service._shutdown(0.5): break
            self.isRunning = False
            self.log("_chkIdle, shutdown!")


    def onNotification(self, sender, method, data):
        self.log("onNotification, sender %s - method: %s  - data: %s" % (sender, method, data))
        # madteevee (imports fork, imports.62): the remote button (no data) and the guide
        # entry (data {path}) both arrive as NotifyAll(pseudotv, restart). The work runs on
        # the restart thread, so this returns at once.
        request = restart_helpers.parse_notification(sender, method, data)
        if request is not None:
            request['source'] = 'guide' if request.get('path') else 'remote'
            self.player.requestRestart(request)
            

    def onSettingsChanged(self):
        # madteevee (imports fork, imports.60): "Rollback watched" applies at once;
        # everything else stays debounced (15 s) and queued as before.
        self._chkRollbackSwitch()
        self._onSettingsChanged()


    def _chkRollbackSwitch(self):
        # Acts only when this setting itself changed: switching it off cancels the
        # pending rollback of the programme playing at once (before, that waited for
        # the 15 s debounce plus the service queue, so a quick Stop still rolled
        # back). Other settings changes cancel nothing.
        state = SETTINGS.getSettingBool('Rollback_Watched')
        if state == self.rollbackSwitch: return
        self.log('_chkRollbackSwitch, Rollback_Watched = %s'%(state))
        self.rollbackSwitch = state
        self.player.rollbackPlaycount = state
        if not state: self.player._cancelPlaystate()


    @debounceit(15)
    def _onSettingsChanged(self):
        self.log('onSettingsChanged')
        self.service._que(self._updatePlayerSettings)
        self.service._que(self._updateServiceSettings)
            
            
    def _updateServiceSettings(self):
        self.log('_updateServiceSettings')
        self.service.channels = self.service.tasks.getChannels()
        self.service.settings = self.service.tasks.chkSettingsChange(self.service.settings) #check for settings change, take action if needed
        
        
    def _updatePlayerSettings(self):
        self.log('_updatePlayerSettings')
        self.player.enableOverlay     = SETTINGS.getSettingBool('Overlay_Enable')
        self.player.infoOnChange      = SETTINGS.getSettingBool('Enable_OnInfo')
        self.player.disableTrakt      = SETTINGS.getSettingBool('Disable_Trakt')
        self.player.rollbackPlaycount = SETTINGS.getSettingBool('Rollback_Watched')
        self.player.minDuration       = SETTINGS.getSettingInt('Seek_Tolerance')
        self.player.maxProgress       = SETTINGS.getSettingInt('Seek_Threshold')
        self.player.sleepTime         = SETTINGS.getSettingInt('Idle_Timer')
        self.player.runWhilePlaying   = SETTINGS.getSettingBool('Run_While_Playing')
        self.player.replayPercentage  = SETTINGS.getSettingInt('Replay_Percentage')
        self.player.OnNextMode        = SETTINGS.getSettingInt('OnNext_Mode')
        self.player.onNextPosition    = SETTINGS.getSetting("OnNext_Position_XY")
        
        
class Service(object):
    channels         = []
    isScanning       = BUILTIN.isScanning()
    isPaused         = BUILTIN.isPaused()
    isPlaying        = BUILTIN.isPlaying()
    isClient         = SETTINGS.getSettingBool('Enable_Client')
    pendingSuspend   = PROPERTIES.setPendingSuspend(False)
    pendingInterrupt = PROPERTIES.setPendingInterrupt(False)
    pendingShutdown  = PROPERTIES.setPendingShutdown(False)
    pendingRestart   = PROPERTIES.setPendingRestart(False)
    jsonQue          = set(SETTINGS.getCacheSetting('jsonQue') or [])
    postQue          = set(SETTINGS.getCacheSetting('postQue') or [])
    logoQue          = set(SETTINGS.getCacheSetting('logoQue') or [])

    def __init__(self):
        self.jsonRPC     = JSONRPC(service=self)
        self.monitor     = Monitor(service=self)
        self.player      = self.monitor.player
        self.tasks       = Tasks(service=self)
        self.channels    = self.tasks.getChannels()
        self.settings    = SETTINGS.getCurrentSettings()
        self.isClient    = SETTINGS.getSettingBool('Enable_Client')
        self.priorityQUE = CustomQueue(priority=True, service=self)
    

    def __del__(self):
        try:
            SETTINGS.setCacheSetting('jsonQue', self.jsonQue)
            SETTINGS.setCacheSetting('postQue', self.postQue)
            SETTINGS.setCacheSetting('logoQue', self.logoQue)
        except Exception: pass


    def log(self, msg, level=xbmc.LOGDEBUG):
        return log('%s: %s'%(self.__class__.__name__,msg),level)


    def _que(self, func, priority=-1, *args, **kwargs):# priority -1 autostack (FIFO), 1 Highest, 5 Lowest
        self.priorityQUE._push((func, args, kwargs), priority)


    def _isPlaying(self) -> bool: #assert playback for background service throttling.
        if (self.isPlaying or self.isPaused) and not self.player.runWhilePlaying: return True
        return False
    
        
    def _tasks(self):
        self._que(self.tasks._chkEpochTimer,-1,*('chkQueTimer', self.tasks.chkQueTimer, 15)) #keep CustomQueue alive after interrupt.
    
    
    def _shutdown(self, wait=SERVICE_INTERVAL) -> bool: #service break
        pendingShutdown = any([self.monitor.waitForAbort(wait),PROPERTIES.isPendingShutdown()])
        if self.pendingShutdown != pendingShutdown:
            self.pendingShutdown = pendingShutdown
            self.log('_shutdown, pendingShutdown = %s, wait = %s'%(self.pendingShutdown,wait))
        return self.pendingShutdown
    
    
    def _restart(self) -> bool: #service restart
        pendingRestart = PROPERTIES.isPendingRestart()
        if self.pendingRestart != pendingRestart:
            self.pendingRestart = pendingRestart
            self.log('_restart, pendingRestart = %s'%(self.pendingRestart))
        return self.pendingRestart
         
        
    def _interrupt(self) -> bool: #tasks break
        # Same self-referential bug class as _suspend below: ORing against PROPERTIES
        # .isPendingInterrupt() — the property this method writes via setPendingInterrupt
        # — meant any one-shot interrupt (Manager.saveChanges sets it, Builder honours it,
        # finally clauses release it) could leave pendingInterrupt stuck True if anything
        # in the path failed. Then the OR keeps reading True and never clears, deadlocking
        # the saveChanges → builder.interruptActivity → setChannels chain. Master fork ORs
        # against PROPERTIES.isInterruptActivity() — the flag set/cleared by the
        # interruptActivity() context manager — which has its own finally to clear cleanly.
        pendingInterrupt = any([self.pendingShutdown, self.pendingRestart, self.isScanning, self._isPlaying(), PROPERTIES.isInterruptActivity()])
        if pendingInterrupt != self.pendingInterrupt:
            self.pendingInterrupt = PROPERTIES.setPendingInterrupt(pendingInterrupt)
            self.log('_interrupt, pendingInterrupt = %s'%(self.pendingInterrupt))
        return self.pendingInterrupt
    

    def _suspend(self, wait=0) -> bool: #tasks continue
        # OR against isSuspendActivity (set by the suspendActivity() context manager around
        # plugin invocations), NOT isPendingSuspend — that's the property this method writes
        # via setPendingSuspend below, so ORing against it creates a self-referential lock:
        # once any earlier event set pendingSuspend=True (settings opened, plugin invocation),
        # subsequent _suspend calls see isPendingSuspend()=True, OR keeps it True, the property
        # never clears, and the service stays paused forever even after the trigger is gone.
        # Master fork uses isSuspendActivity() here.
        pendingSuspend = any([BUILTIN.isSettingsOpened(), PROPERTIES.isSuspendActivity(),])
        if pendingSuspend != self.pendingSuspend:
            self.pendingSuspend = PROPERTIES.setPendingSuspend(pendingSuspend)
            self.log('_suspend, pendingSuspend = %s'%(self.pendingSuspend))
        return self.pendingSuspend
        

    def _sleep(self, wait=1.0): #waitForAbort replacement for tasks
        while not self.monitor.abortRequested() and wait > 0:
            if any([self.monitor.waitForAbort(CPU_CYCLE),self.pendingInterrupt]): return True
            else: wait -= CPU_CYCLE
        if wait > 0: self.log('_wait, remaining = %s'%(wait))
        return False
                    

    def _initialize(self):
        if self.isClient: self._que(self.tasks._client,1)
        else:             self._que(self.tasks._host,1)


    def _start(self):
        self.log('_start')
        self._initialize()
        if DIALOG.notificationWait('%s...'%(LANGUAGE(32054)),wait=15):
            if self.player.isPlayingPseudoTV(): self.player.onAVStarted()
            while not self.monitor.abortRequested():
                self.isScanning = BUILTIN.isScanning()
                self.isPaused   = BUILTIN.isPaused()
                self.isPlaying  = self.player.isPlaying()
                if    self._shutdown() and self._interrupt() and self._sleep(): break
                elif  self._restart()  and self._interrupt() and self._sleep(): break
                else: self._tasks()
            return self._stop(self.pendingRestart)


    def _stop(self, pendingRestart: bool=False):
        if self.player.isPlayingPseudoTV(): self.player.onPlayBackStopped()
        with PROPERTIES.interruptActivity():
            for thread in thread_enumerate():
                if thread.name != "MainThread" and thread.is_alive():
                    if hasattr(thread, 'cancel'): thread.cancel()
                    try: thread.join(1.0)
                    except Exception: pass
                    self.log('_stop, closing %s...'%(thread.name))
            PROPERTIES.setInstanceID() #clrTrash
        return pendingRestart