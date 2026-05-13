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

# -*- coding: utf-8 -*-

import copy

from globals    import *
from channels   import Channels
from xmltvs     import XMLTVS
from xsp        import XSP
from m3u        import M3U
from fillers    import Fillers
from resources  import Resources
from seasonal   import Seasonal 
from rules      import RulesList
from seasonal   import Seasonal

class Service(object):
    from jsonrpc import JSONRPC
    jsonRPC = JSONRPC()
    player  = PLAYER()
    monitor = MONITOR()
    def _shutdown(self, wait=1.0) -> bool:
        return (self.monitor.waitForAbort(wait) or PROPERTIES.isPendingShutdown())
    def _interrupt(self) -> bool:
        return (PROPERTIES.isPendingShutdown() or PROPERTIES.isPendingRestart() or PROPERTIES.isPendingInterrupt())
    def _suspend(self, wait=1.0) -> bool:
        return PROPERTIES.isPendingSuspend()
    def _sleep(self, wait=1.0):
        while not self.monitor.abortRequested() and wait > 0:
            if (self.monitor.waitForAbort(CPU_CYCLE) or self._interrupt()): return True
            else: wait -= CPU_CYCLE
        return False

class Builder(object):
    xsp      = XSP()
    # v.45: read-only fallbacks. buildChannels constructs fresh writable
    # M3U/XMLTVS instances per-build (~line 320) and assigns them as instance
    # attrs, shadowing these class-level objects. writable=False makes
    # __del__'s _save() a no-op (m3u.py:75-79, xmltvs.py:40-44), so process
    # exit / module teardown / addon disable can't persist this frozen-at-
    # import-time state to disk and clobber post-import builds. Verified
    # zero external accessors of Builder.m3u / Builder.xmltv across the
    # codebase; these stay declared as defensive fallbacks only.
    m3u      = M3U(writable=False)
    xmltv    = XMLTVS(writable=False, m3u=m3u)
    channels = Channels(writable=True)
    seasonal = Seasonal()
    loopback = None
    
    def __init__(self, service=None):
        if service  is None:
            service  = Service()
            
        self.service  = service     
        self.monitor  = service.monitor
        self.jsonRPC  = service.jsonRPC
        self.cache    = service.jsonRPC.cache
        self.holiday  = self.seasonal.getHoliday()
        
        #global dialog
        self.fCount  = 0
        self.pCount  = 0
        self.pDialog = None
        self.pMSG    = ''
        self.pName   = ''
        self.pHeader = ''
        self.pErrors = []
        
        #global rules
        self.accurateDuration = bool(SETTINGS.getSettingInt('Duration_Type'))
        self.interleaveSet    = SETTINGS.getSettingInt('Interleave_Set')
        self.interleaveRepeat = SETTINGS.getSettingBool('Interleave_Repeat')
        self.incStrms         = SETTINGS.getSettingBool('Enable_Strms')
        self.inc3D            = SETTINGS.getSettingBool('Enable_3D')
        self.incExtras        = SETTINGS.getSettingBool('Enable_Extras') 
        self.incStrmDetails   = SETTINGS.getSettingBool('Enable_Details')
        self.enableBCTs       = SETTINGS.getSettingBool('Enable_Fillers')#todo add to adv. rules
        self.saveDuration     = SETTINGS.getSettingBool('Store_Duration')
        self.minDuration      = SETTINGS.getSettingInt('Seek_Tolerance')
        self.limit            = SETTINGS.getSettingInt('Page_Limit')
        self.recursiveLimit   = SETTINGS.getSettingInt('Recursive_Depth') #todo adv. channel rule. set recursive depth.
        self.padScheduling    = False #todo Adv. Channel Rule, No Global: Default False
        self.padTarget        = MIN_EPG_DURATION # PadScheduling rule overrides per-channel via inherited.padTarget (rules.py:2999)
        self.padFilelist      = False #todo Adv. Channel Rule, No Global: Default False
        # madteevee v.24: per-Builder-instance snapshot store for in-flight
        # channel rebuilds. Keyed by chid. Populated by _snapshotChannel
        # before __hasChanged.__clrStation; popped on success
        # (_discardSnapshot) or replayed on failure (_restoreChannel).
        # Lives on instance — concurrent Builder instances don't share keys.
        self._build_snapshots = {}
        self.enableEven       = bool(SETTINGS.getSettingInt('Enable_Even'))
        self.evenEpisode      = SETTINGS.getSettingBool('Enable_Even_Force_Episode')
        self.evenShuffle      = SETTINGS.getSettingBool('Enable_Even_Force_Random')

        self.filter           = {}#{"and": [{"operator": "contains", "field": "title", "value": "Star Wars"},{"operator": "contains", "field": "tag", "value": "Good"}],"or":[]}
        self.sort             = {}#{"ignorearticle":True,"method":"random","order":"ascending","useartistsortname":True}
        self.limits           = {"end":-1,"start":0,"total":0}
        self.query            = {}
        
        self.bctTypes         = {"ratings" :{"min":-1, "max":SETTINGS.getSettingInt('Enable_Preroll'), "auto":SETTINGS.getSettingInt('Enable_Preroll') == -1, "enabled":bool(SETTINGS.getSettingInt('Enable_Preroll')), "chance":SETTINGS.getSettingInt('Random_Pre_Chance'),
                                             "sources" :{"ids":SETTINGS.getSetting('Resource_Ratings').split('|'),"paths":[os.path.join(FILLER_LOC,'Ratings' ,'')]},"items":{}},
                                 
                                 "bumpers" :{"min":-1, "max":SETTINGS.getSettingInt('Enable_Preroll'), "auto":SETTINGS.getSettingInt('Enable_Preroll') == -1, "enabled":bool(SETTINGS.getSettingInt('Enable_Preroll')), "chance":SETTINGS.getSettingInt('Random_Pre_Chance'),
                                             "sources" :{"ids":SETTINGS.getSetting('Resource_Bumpers').split('|'),"paths":[os.path.join(FILLER_LOC,'Bumpers' ,'')]},"items":{}},
                                 
                                 "adverts" :{"min":SETTINGS.getSettingInt('Enable_Postroll'), "max":PAGE_LIMIT, "auto":SETTINGS.getSettingInt('Enable_Postroll') == -1, "enabled":bool(SETTINGS.getSettingInt('Enable_Postroll')), "chance":SETTINGS.getSettingInt('Random_Post_Chance'),
                                             "sources" :{"ids":SETTINGS.getSetting('Resource_Adverts').split('|'),"paths":[os.path.join(FILLER_LOC,'Adverts' ,'')]},"items":{}},
                                 
                                 "trailers":{"min":SETTINGS.getSettingInt('Enable_Postroll'), "max":PAGE_LIMIT, "auto":SETTINGS.getSettingInt('Enable_Postroll') == -1, "enabled":bool(SETTINGS.getSettingInt('Enable_Postroll')), "chance":SETTINGS.getSettingInt('Random_Post_Chance'),
                                             "sources" :{"ids":SETTINGS.getSetting('Resource_Trailers').split('|'),"paths":[os.path.join(FILLER_LOC,'Trailers','')]},"items":{}, "incKODI":SETTINGS.getSettingBool('Include_Trailers_KODI')}}

        self.resources    = Resources(service=self.service)
        self.runActions   = RulesList(self.channels.getChannels()).runActions
        self.trailerCache = (SETTINGS.getCacheSetting('trailerCache') or {})
        self.log(f'__init__, trailerCache = {len(self.trailerCache)}')


    def __del__(self):
        try:
            SETTINGS.setCacheSetting('trailerCache', self.trailerCache)
            self.log(f'__del__, trailerCache = {len(self.trailerCache)}')
        except Exception: pass


    def log(self, msg, level=xbmc.LOGDEBUG):
        return log('%s: %s'%(self.__class__.__name__,msg),level)


    def _snapshotChannel(self, citem):
        """v.24: Capture this channel's M3U + XMLTV state for potential restore.
        Stored under self._build_snapshots[citem['id']] until popped (success
        path) or replayed (failure path). Uses deepcopy because the underlying
        dicts/lists are mutable shared state on class-level M3U/XMLTVS instances.
        """
        chid = citem.get('id')
        if not chid: return
        self._build_snapshots[chid] = {
            'station'   : copy.deepcopy(self.m3u.findStation(citem)[1] or {}),
            'channel'   : copy.deepcopy(self.xmltv.findChannel(citem)[1] or {}),
            'programmes': copy.deepcopy([p for p in self.xmltv.XMLTVDATA.get('programmes', [])
                                         if p.get('channel') == chid]),
        }
        s = self._build_snapshots[chid]
        self.log('[%s] _snapshotChannel, M3U=%s, XMLTV ch=%s, progs=%s'
                 %(chid, bool(s['station']), bool(s['channel']), len(s['programmes'])))


    def _restoreChannel(self, citem):
        """v.24: Restore a previously-snapshotted channel into in-memory M3U + XMLTV.
        No-op if no snapshot exists (e.g. __hasChanged didn't fire __clrStation
        for this channel, or this channel was already restored). Direct list
        appends because addStation/addChannel are upsert (delete-then-append) and
        not safe for replay; we bypass them entirely.
        """
        chid = citem.get('id')
        snap = self._build_snapshots.pop(chid, None) if chid else None
        if not snap: return
        if snap['station']:
            self.m3u.M3UDATA.setdefault('stations', []).append(snap['station'])
        if snap['channel']:
            self.xmltv.XMLTVDATA.setdefault('channels', []).append(snap['channel'])
        if snap['programmes']:
            self.xmltv.XMLTVDATA.setdefault('programmes', []).extend(snap['programmes'])
        self.log('[%s] _restoreChannel after failed build, M3U+%s, XMLTV ch+%s, progs+%s'
                 %(chid, 1 if snap['station'] else 0,
                   1 if snap['channel'] else 0, len(snap['programmes'])))


    def _discardSnapshot(self, citem):
        """v.24: Drop the snapshot for this channel — called on successful build
        paths so self._build_snapshots doesn't accumulate stale entries within
        a single Builder lifetime."""
        self._build_snapshots.pop(citem.get('id'), None)


    def getVerifiedChannels(self, channels=None):
        if channels is None: channels = self.channels.getChannels()
        channels = sorted(self._verify(channels), key=itemgetter('number'))
        self.log('getVerifiedChannels, channels = %s'%(len(channels)))
        return channels

 
    def _verify(self, channels=None):
        if channels is None: channels = self.channels.getChannels()
        for idx, citem in enumerate(channels):
            if not citem.get('name') or len(citem.get('path',[])) == 0 or not citem.get('number'):
                # madteevee (imports fork): defensive logging. Imports have no
                # `path` field by design (EPG comes from Imports.syncAll, not
                # Builder). tasks._filterChannelsNeedingBuild + chkChanged
                # filter imports out of the queue upstream, but if one slips
                # through (legacy state, bug elsewhere), call it out clearly
                # rather than the generic "missing necessary channel meta".
                if citem.get('type') == 'import':
                    self.log('[%s] SKIPPING - import channel reached Builder (type=import, changed=%s); imports get EPG from syncAll\n%s'%(citem.get('id'),citem.get('changed',False),citem),xbmc.LOGWARNING)
                else:
                    self.log('[%s] SKIPPING - missing necessary channel meta\n%s'%(citem.get('id'),citem),xbmc.LOGINFO)
                continue
            elif not citem.get('enabled',True):
                self.log('[%s] SKIPPING - disabled channel\n%s'%(citem.get('id'),citem),xbmc.LOGINFO)
                continue
            else:
                if not citem.get('id'): citem['id'] = getChannelID(citem['name'],citem['path'],citem['number'],SETTINGS.getMYUUID()) #generate new channelid
                # imports.20: respect operator_overrides. The operator can set
                # a logo via web manager (/channels/edit.json) or in-Kodi
                # Channel Manager (manager.switchLogo). Without this guard,
                # every Builder rebuild silently re-derives the auto-logo via
                # getLogo and overwrites the operator's pick. Logo is the only
                # Custom-channel field _verify re-derives (audit confirmed).
                if 'logo' not in (citem.get('operator_overrides') or []):
                    citem['logo'] = self.resources.getLogo(citem,fallback=self.resources.getCache(citem['name']),lookup=True)
                self.log('[%s] VERIFIED - channel %s: %s changed = %s'%(citem['id'],citem['number'],citem['name'],citem.get('changed',False)),xbmc.LOGINFO)
                yield self.runActions(RULES_ACTION_CHANNEL_CITEM, citem, citem, inherited=self) #inject persistent citem changes here

             
    def buildCells(self, citem: dict, duration: int=10800, type: str='video', entries: int=3, info=None) -> list:
        if info is None: info = {}
        tmpItem  = {'label'       : (info.get('title')        or citem['name']),
                    'episodetitle': (info.get('episodetitle') or '|'.join(citem.get('group',[]))),
                    'plot'        : (info.get('plot')         or LANGUAGE(32020)),
                    'genre'       : (info.get('genre')        or ['Undefined']),
                    'file'        : (info.get('path')         or info.get('file') or info.get('originalpath') or  '|'.join(citem.get('path',[]))),
                    'art'         : (info.get('art')          or {"thumb":LOGO_COLOR,"fanart":FANART,"logo":LOGO,"icon":LOGO}),
                    'type'        : type,
                    'duration'    : duration,
                    'start'       : 0,
                    'stop'        : 0}
        info.update(tmpItem)
        out = []
        for _ in range(entries):
            out.append(info.copy())
        return out


    def buildChannels(self, channels=None, preview=False, silent=False):
        # madteevee (imports fork): mutable list default replaced with sentinel.
        # The body already handles channels is None below.
        enableChanged = SETTINGS.getSettingBool('Enable_Changed')
        self.log('buildChannels, channels = %s'%(len(channels)))
        if channels is None: channels = []
        # madteevee (imports fork): the existing per-channel _progressDialog
        # at line ~411 (opens with the channel name + percent inside the
        # `if _update or _changed` block) is the canonical on-screen build
        # indicator. An earlier attempt added an outer DialogProgressBG
        # spanning the whole function lifecycle, but it stacked visually
        # with the inner per-channel dialog — operator saw two competing
        # toasts ("Building: X" + "Updating channel: X") rendering at the
        # same screen slot. Removed. The CRC-in-filter slow-load fix means
        # buildChannels is only invoked when there's genuine work to do
        # (the inner dialog opens immediately), so we don't need an outer
        # "inspection phase" indicator anymore.
        build_progress = None
        def __needsUpdate(citem, now, fallback, state=True):
            #max guidedata days to seconds, minus fill buffer (12hrs) in seconds.
            last_stop = dict(self.xmltv.loadStopTimes([citem], fallback=fallback)).get(citem['id']) #check last stop times 
            if last_stop > (now + ((MAX_GUIDEDAYS * 86400) - 43200)): state = False
            self.log('[%s] buildChannels, __needsUpdate = %s, last_stop = %s'%(citem['id'],state, last_stop))
            return state, last_stop
            
        def __hasChanged(citem: dict, detect=SETTINGS.getSettingBool('Enable_Changed')) -> bool:
            if not citem.get('changed',False) and detect:
                state = any([SETTINGS.getFileCRC(file) for file in citem.get('path',[]) if file.endswith(tuple(KODI_PLAYLISTS + BASIC_PLAYLISTS))])
            else: state = citem.get('changed',False)
            self.log('[%s] buildChannels, __hasChanged = %s'%(citem['id'],state))
            if state: #clear channel m3u/xmltv
                # v.24: snapshot BEFORE __clrStation so a subsequent buildVideo
                # failure (Enable_Extras=false skipping all-Specials sources,
                # _suspend interrupt, smartplaylist returning nothing, plugin
                # source timeout) can be recovered by _restoreChannel rather
                # than letting __setStation persist the cleared state to disk.
                # Without this, multiple sequential per-channel builds compound
                # the wipe across the class-level shared M3U/XMLTV state.
                self._snapshotChannel(citem)
                if __clrStation(citem):
                    self.log('[%s] buildChannels, __hasChanged cleared channel meta'%(citem['id']))
                    citem['changed'] = False
                changes.add(self.channels.addChannel(citem))
                modified_ids.add(citem['id']) # v.43: track touched IDs for merge-on-write at setChannels
            return state
                    
        def __hasProgrammes(citem: dict) -> bool:
            try:    state = dict(self.xmltv.hasProgrammes([citem])).get(citem['id'],False)
            except Exception: state = False
            self.log('[%s] buildChannels, __hasProgrammes = %s'%(citem['id'],state))
            return state

        # imports.30: metadata-only fast-path helpers. `changed=True` always
        # wins over `metadata_changed=True` — defensive default so any code
        # path that set BOTH (e.g., a hypothetical bulk-renumber that also
        # changed a path) gets the safe full rebuild.
        def __hasMetadataOnlyChange(citem: dict) -> bool:
            state = (bool(citem.get('metadata_changed', False))
                     and not bool(citem.get('changed', False)))
            self.log('[%s] buildChannels, __hasMetadataOnlyChange = %s' % (citem['id'], state))
            return state

        def __renderMetadataOnly(citem: dict) -> bool:
            # Upsert M3U entry + XMLTV channel element from the current citem
            # state. Skips __clrStation + __addProgrammes — the channel's
            # programmes in XMLTVDATA['programmes'] stay intact (keyed by
            # channel id, independent from the channel element).
            #
            # Safe because:
            #   - m3u.addStation (m3u.py:592) internally deletes by id then
            #     appends → net upsert; calling it on the existing channel
            #     replaces the M3U entry with the new metadata.
            #   - xmltv.addChannel (xmltvs.py:531) explicit upsert by id at
            #     the same index.
            #   - Programmes re-associate with the (replaced) channel element
            #     by id at lookup time — no orphan risk.
            #
            # Caller must guard with __hasProgrammes — without programmes,
            # xmltvs._save's cleanChannels (xmltvs.py:376) drops the channel
            # silently. Caller escalates to changed=True in that case.
            sitem = self.m3u.getStationItem(citem)
            state = any([self.m3u.addStation(sitem), self.xmltv.addChannel(sitem)])
            self.log('[%s] buildChannels, __renderMetadataOnly = %s' % (citem['id'], state))
            citem['metadata_changed'] = False
            changes.add(self.channels.addChannel(citem))
            modified_ids.add(citem['id'])
            return state

        def __hasFileList(fileList: list, state=False) -> bool:
            if isinstance(fileList,list) and len(fileList) > 0: state = True
            self.log('[%s] buildChannels, __hasFileList = %s'%(citem['id'],state))
            return state
        
        def __addProgrammes(citem: dict, fileList: list) -> bool:
            state = any([self.xmltv.addProgram(citem['id'], self.xmltv.getProgramItem(citem, item)) for item in fileList])
            self.log('[%s] buildChannels, __addProgrammes fileList = %s'%(citem['id'],len(fileList)))
            return state
        
        def __addStation(citem: dict) -> bool:
            sitem = self.m3u.getStationItem(citem)
            state = any([self.m3u.addStation(sitem),self.xmltv.addChannel(sitem)])
            self.log('[%s] buildChannels, __addStation = %s'%(citem['id'],state))
            return state
        
        def __clrStation(citem: dict) -> bool:
            state = any([self.resetPagination(citem),self.m3u.delStation(citem),self.xmltv.delBroadcast(citem)])
            self.log('[%s] buildChannels, __clrStation = %s'%(citem['id'],state))
            return state
            
        def __setStation():
            # madteevee (imports fork, imports.12 / writer_lock): single
            # _save call per file, no belt-and-suspenders. The original
            # double-write (_save then _writeAtomic with a separate
            # _renderM3U/_renderXMLTV pass) existed because @debounceit
            # on _save was unreliable for end-of-build persistence —
            # XMLTVS._save was empirically never firing for one-channel
            # rebuilds (Saturday Morning TV added 500 programmes to in-
            # memory XMLTVDATA that never reached disk because the
            # debounce was canceled before firing). @debounceit has
            # since been removed from both _save methods (m3u.py:391-
            # 399, xmltvs.py:81-85), and `_save` now runs synchronously
            # under WRITER_LOCK — atomic + serialized end-to-end.
            # The redundant _writeAtomic block produced a second
            # os.replace per channel with byte-identical content; it's
            # gone in imports.12.
            state = any([self.m3u._save(), self.xmltv._save()])
            self.log('[%s] buildChannels, __setStation = %s'%(citem['id'],state))
            return state
            
        # try/finally guarantees the persistent build_progress dialog
        # (opened above) is closed on every exit path — normal completion,
        # exceptions, queue-busy short-circuit at line below, etc. Otherwise
        # an unclosed DialogProgressBG would linger on screen forever.
        try:
          if not PROPERTIES.isRunning('Builder.buildChannels'):
            with PROPERTIES.legacy(), PROPERTIES.chkRunning('Builder.buildChannels'):
                # v.45: per-build fresh M3U/XMLTVS for non-preview builds.
                # Each fresh _load() picks up disk modifications by other
                # writers (context_record.py:51,69 add/remove recordings;
                # operator hand-edits to pseudotv.m3u / pseudotv.xml) at build
                # start instead of letting this build's _save() clobber them
                # with stale class-level state. Builds are serial (cqueue's
                # single-threaded popThread; _exe at cqueue.py:120 runs func
                # synchronously even with useExecutor=True), so each fresh
                # load reflects the prior build's _save. Preview path skips
                # this — preview is a non-persistent dry run; constructing
                # fresh writable=True instances would let M3U.__del__ persist
                # any mid-preview mutations (e.g. __clrStation matching by
                # url at m3u.py:387-390) to disk on Builder GC. Class-level
                # fallbacks at lines 53-62 are now writable=False so they
                # don't compete with these instances at shutdown. Closures
                # defined below capture `self` and resolve self.m3u/self.xmltv
                # at call time, so they see these fresh instances automatically.
                # Same applies to v.24 snapshot/restore at builder.py:138-182.
                # FORK_NOTES priority #4 (small-fix scope).
                if not preview:
                    self.m3u   = M3U(writable=True)
                    self.xmltv = XMLTVS(writable=True, m3u=self.m3u)
                channels = self.getVerifiedChannels(channels)
                # madteevee (imports fork): keep the persistent build_progress
                # dialog (opened at function entry) animated by updating it
                # at each major phase transition. Without these explicit
                # update() calls, DialogProgressBG renders the progress bar
                # at 0% indefinitely and operator sees a "frozen" toast.
                # The inner _progressDialog (per-channel, opened at the
                # `_update or _changed` block further down) shows the
                # fine-grained build percent for the actual rebuild work;
                # this outer dialog tracks the COARSE channel-of-N progress
                # plus the syncAll tail at the end.
                _total_ch = max(1, len(channels))
                if build_progress is not None:
                    try: build_progress.update(5, ADDON_NAME, '%s (verifying %d channels)'%(LANGUAGE(30014), _total_ch))
                    except Exception: pass
                if len(channels) > 0:
                    completed    = set()
                    changes      = set()
                    modified_ids = set() # v.43: per-citem IDs that __hasChanged actually mutated; passed to setChannels for merge-on-write
                    now          = getUTCstamp()
                    nstart    = roundTimeDown(now,offset=60)#offset time to start bottom of the hour
                    fallback  = epochTime(nstart,tz=False).strftime(DTFORMAT)

                    self.pDialog = None
                    self.pMSG    = ''
                    self.pName   = ''
                    self.pHeader = ''
                    self.pErrors = []
                    for idx, citem in enumerate(channels):
                        # madteevee: outer-dialog progress per-iteration.
                        # Reserve 0-10% for verify, 10-85% for the build
                        # loop (75% across N channels = 75/N per channel),
                        # 85-100% for the post-loop syncAll tail.
                        if build_progress is not None:
                            try:
                                _outer_pct = int(10 + (idx / _total_ch) * 75)
                                _outer_msg = '%s: %s (%d/%d)' % (LANGUAGE(30014), citem.get('name','?'), idx + 1, _total_ch)
                                build_progress.update(_outer_pct, ADDON_NAME, _outer_msg)
                            except Exception: pass
                        try:
                            # imports fork (madteevee): live-import channels are pass-through
                            # (their content / EPG comes from external M3U+XMLTV, not buildVideo).
                            # Skip the rules + buildVideo + programme-generation pipeline; the
                            # post-loop syncAll() handles them. Plan §3 (Builder integration).
                            if citem.get('type') == 'import':
                                self.log('[%s] buildChannels, type=import — skipping per-channel build (handled by syncAll)'%(citem['id']))
                                continue
                            updated      = set()
                            self.pCount  = int(idx+1*100)//len(channels)
                            self.pMSG    = '%s: %s'%(LANGUAGE(32144),LANGUAGE(32212))
                            self.pHeader = ADDON_NAME
                            self.pName   = citem['name']
                            citem = self.runActions(RULES_ACTION_CHANNEL_TEMP_CITEM, citem, Globals._cleanGroups(citem), inherited=self) #inject temporary citem changes here
                            # imports.30: metadata-only fast-path. Catches operator
                            # edits to number/name/logo/group/catchup/favorite via
                            # any path that set `metadata_changed=True` (server.py
                            # /channels/edit.json classifier, tasks.py drift
                            # detection). Skips the ~90s programme re-enumeration
                            # cost of the full-rebuild path below — Builder still
                            # writes M3U + XMLTV (channel element only) atomically.
                            # The `__hasProgrammes` guard is required: without it,
                            # xmltvs._save's cleanChannels drops zero-programme
                            # channels silently. When the guard fails, escalate
                            # to full rebuild by setting changed=True and falling
                            # through — __hasChanged below will see the flag and
                            # trigger __clrStation + buildVideo + __addProgrammes.
                            if __hasMetadataOnlyChange(citem):
                                if __hasProgrammes(citem):
                                    __renderMetadataOnly(citem)
                                    completed.add(True)
                                    PROPERTIES.setPropTimer('chkPVRRefresh') # nudge PVR
                                    __setStation()                            # write M3U + XMLTV
                                    continue
                                else:
                                    self.log('[%s] buildChannels, metadata-only escalating to full rebuild (no programmes — cleanChannels would drop the channel)' % (citem['id']), xbmc.LOGINFO)
                                    citem['changed'] = True
                                    citem['metadata_changed'] = False
                            _update, start = __needsUpdate(citem, now, fallback)
                            _changed = __hasChanged(citem, enableChanged)
                            # imports.31: when __hasChanged=True, __clrStation
                            # (line 290 inside __hasChanged) just wiped this
                            # channel's M3U entry + all programmes. The `start`
                            # value captured by __needsUpdate above reflects
                            # the PRE-clear last_stop, which can be far in the
                            # future when the prior schedule already extended
                            # N days out. Using a future `start` as the new
                            # schedule anchor leaves a gap between `now` and
                            # the first new programme; the next chkChannels
                            # cycle then re-triggers __needsUpdate to fill the
                            # gap (wasted ~9-min rebuild). After a wipe, the
                            # anchor must be `now` (bottom-of-hour aligned to
                            # match the existing programme-grid convention
                            # established by `nstart` at line 429 + xmltvs.
                            # loadStopTimes fallback semantics at xmltvs.py:
                            # 266). NO change for the _update-only path
                            # (extend without wipe) — that path correctly
                            # anchors at last_stop, which preserves catchup
                            # semantics (catchup_mode='vod' intentionally
                            # schedules past programmes per
                            # project_imports_catchup memory).
                            if _changed:
                                start = roundTimeDown(now, offset=60)
                            self.log('[%s] buildChannels, preview = %s, rules = %s, _update = %s'%(citem['id'],preview,citem.get('rules',{}),_update))
                            if self.service._interrupt():
                                self.log("[%s] buildChannels, _interrupt"%(citem['id']))
                                self.pErrors = [LANGUAGE(32160)]
                                # v.24: restore the pre-clear snapshot before bailing — without
                                # this the in-memory M3U/XMLTV stays wiped and a later __setStation
                                # (this Builder lifetime or class-shared mutation across instances)
                                # would persist the wipe to disk.
                                self._restoreChannel(citem)
                                if hasattr(self.service,'_que'): self.service._que(self.service.tasks.chkChannels,3,*(channels[idx:],silent))
                                break
                            elif self.service._suspend(CPU_CYCLE):
                                self.log("[%s] buildChannels, _suspend"%(citem['id']))
                                # v.24: restore so the next iteration's __setStation doesn't save
                                # this channel's cleared state from the failed mid-build interrupt.
                                self._restoreChannel(citem)
                                self.monitor.waitForAbort(CPU_CYCLE)
                                continue
                            elif _update or _changed:                       
                                if    preview:           self.pMSG = LANGUAGE(32236)                           #Preview
                                elif  start == fallback: self.pMSG = '%s %s'%(LANGUAGE(30014),LANGUAGE(30223)) #Building
                                else:                    self.pMSG = '%s %s'%(LANGUAGE(32022),LANGUAGE(30223)) #Updating
                                    
                                self.pHeader = f'{ADDON_NAME}, {self.pMSG}'
                                self.log("[%s] buildChannels, start (%s) => %s"%(citem['id'],start,self.pMSG))

                                if start > 0:
                                    with DIALOG._progressDialog(self.pMSG, ADDON_NAME, silent=None, background=not preview) as self.pDialog:
                                        self.runActions(RULES_ACTION_CHANNEL_START, citem, inherited=self)
                                        if citem.get('radio',False): fileList = self.buildMusic(citem)
                                        else:                        fileList = self.buildVideo(citem)
                                        #fileList = {False:'In-Valid Channel', True:'Valid Channel w/o programmes', list:'Valid Channel w/ programmes}
                                        if isinstance(fileList,list):
                                            fileList = sorted(self.addScheduling(citem, fileList, now, start), key=itemgetter('start'))
                                            if not preview and __hasFileList(fileList):
                                                updated.add(__addProgrammes(citem, fileList))#add xmltv lineup entries.
                                        elif not fileList:
                                            updated.add(__hasProgrammes(citem))
                                            if len(self.pErrors) > 0:
                                                self.pErrors.append(LANGUAGE(32026))
                                                chanErrors = ' | '.join(list(sorted(set(self.pErrors))))
                                                self.log('[%s] buildChannels, In-Valid Channel (%s) %s'%(citem['id'],self.pName,chanErrors))
                                                self.pDialog = DIALOG._updateProgress(self.pDialog, self.pCount, message='%s: %s'%(self.pName,chanErrors),header=f'{ADDON_NAME}, {LANGUAGE(32027)} {LANGUAGE(30223)}')
                                        self.runActions(RULES_ACTION_CHANNEL_STOP, citem, inherited=self)
                                        if preview: return fileList
                            else: updated.add(__hasProgrammes(citem))
                                
                            if any(updated):
                                completed.add(__addStation(citem)) #add m3u station if lineup available.
                                PROPERTIES.setPropTimer('chkPVRRefresh')#refresh pvr guide
                                # v.24: build succeeded with new content — discard the snapshot
                                # so it doesn't accumulate in self._build_snapshots.
                                self._discardSnapshot(citem)
                            else:
                                # B3 forward-port (madteevee): don't __clrStation on transient/empty build.
                                # buildVideo can return True (Valid Channel w/o programmes — BUILD_AT_MAX),
                                # an empty list (paginate-stalled), or False mid-suspend — none of those
                                # mean the channel is gone. Keep prior M3U/XMLTV state so the next
                                # chkLibrary cycle can retry. M3U/XMLTV are removed via channel manager
                                # only — never from the build path. Mirrors master B3's behavior.
                                # v.24: B3 covered the case where __clrStation hadn't run. When
                                # __hasChanged DID fire __clrStation earlier this iteration, the
                                # snapshot taken before the clear is now restored so the cleared
                                # state never reaches __setStation below. Without this the
                                # class-level shared M3U/XMLTV would persist the wipe.
                                self._restoreChannel(citem)
                                self.log('[%s] buildChannels, preserving M3U/XMLTV across transient empty (snapshot restored if __clrStation ran)'%(citem['id']), xbmc.LOGWARNING)
                            __setStation()
                        except Exception as e:
                            self.log("buildChannels, failed! %s"%(e), xbmc.LOGERROR)
                            # v.24: best-effort restore on unexpected exception — same rationale
                            # as the _interrupt/_suspend/B3-else branches.
                            try: self._restoreChannel(citem)
                            except Exception: pass
                    if any(changes): self.channels.setChannels(modified_ids=modified_ids)
                    self.log('[%s] buildChannels, completed = %s, updated = %s, changes = %s'%(citem['id'],any(completed),any(updated),any(changes)))

                    # imports fork (madteevee): live-imports sync runs AFTER per-channel
                    # loop completes so cascade allocation sees the operator-built
                    # channels' final numbers as hard pins. Skipped on preview path
                    # (preview is dry-run; imports.syncAll mutates persisted state).
                    # No-op when no imports configured. See plan §3 / §5.
                    if not preview:
                        # madteevee (imports fork): targeted toast for the
                        # syncAll tail ONLY. The inner _progressDialog has
                        # already closed (its `with` block exited at 100%
                        # after the per-channel build completed), so the
                        # operator sees no indicator during the 30-180s
                        # syncAll tail — looks like nothing is happening.
                        # Opening DialogProgressBG here doesn't stack with
                        # the inner one (which is already closed); it ONLY
                        # spans the syncAll phase and closes immediately
                        # after. No "two racing toasts" issue.
                        # Not silenced during playback — DialogProgressBG
                        # is a small unobtrusive indicator that operators
                        # WANT to see during this otherwise-silent gap.
                        sync_progress = None
                        try:
                            sync_progress = xbmcgui.DialogProgressBG()
                            sync_progress.create(ADDON_NAME, '%s (syncing imports)' % (LANGUAGE(30014)))
                            sync_progress.update(10, ADDON_NAME, '%s (syncing imports)' % (LANGUAGE(30014)))
                        except Exception as _sp_e:
                            self.log('buildChannels, sync_progress create failed: %s' % (_sp_e), xbmc.LOGWARNING)
                            sync_progress = None
                        try:
                            from imports import Imports
                            if list(self.channels.getImports() or []):
                                Imports(channels=self.channels, m3u=self.m3u,
                                        xmltv=self.xmltv, service=self.service).syncAll()
                        except Exception as e:
                            self.log('buildChannels, imports.syncAll failed: %s'%(e), xbmc.LOGERROR)
                        finally:
                            # Close the syncAll-specific dialog regardless of
                            # syncAll outcome. Brief 100% flash before close
                            # gives operator visual confirmation.
                            if sync_progress is not None:
                                try:
                                    sync_progress.update(100, ADDON_NAME, '%s' % (LANGUAGE(32025)))  # "Complete"
                                    sync_progress.close()
                                except Exception: pass

                    # madteevee (imports fork): build_progress (the persistent
                    # DialogProgressBG opened at function entry) is closed in
                    # the finally below — no separate closing toast needed.
                    # The dialog disappearing IS the "Complete" indicator.
        finally:
            # Always close the persistent build_progress dialog, regardless of
            # which exit path the function takes (normal completion, exception
            # inside the build loop, or short-circuit when buildChannels was
            # already running on another thread).
            if build_progress is not None:
                try: build_progress.close()
                except Exception: pass


    def buildMusic(self, citem: dict) -> list:
        self.log("[%s] buildMusic"%(citem['id']))
        #todo insert custom radio labels,plots based on genre type?
        return self.buildCells(citem, MIN_EPG_DURATION, 'music', ((MAX_GUIDEDAYS * 8)), info={'genre':["Music"],'art':{'thumb':citem['logo'],'icon':citem['logo'],'fanart':citem['logo']},'plot':LANGUAGE(32029)%(citem['name'])})
        

    def buildVideo(self, citem: dict, validate: bool=False):
        def _validFileList(fileArray):
            return any(len(fileList) > 0 for fileList in fileArray)
            
        def _injectFillers(citem, fileList, enable=False):#todo refactor
            self.log("[%s] buildVideo: _injectFillers, enable = %s, fileList = %s"%(citem['id'],enable,len(fileList)))
            if enable: return  Fillers(citem,self).injectBCTs(fileList)
            return fileList
          
        def _injectRules(citem):
            tmpCitem = citem.copy()
            #"Seasonal Content"
            if tmpCitem.get('path',[]) == ["{Seasonal}"]:
                nrules = {800:{"values":{0:list(self.seasonal.buildSeasonal(self.holiday))}}}
                tmpCitem.setdefault('rules',{}).update(nrules)
                self.log(" [%s] buildVideo: _injectRules, Seasonal Content, new rules = %s"%(citem['id'],nrules))
                
            #"Even Show Distribution"
            if self.enableEven and not citem.get('rules',{}).get(1000):
                nrules = {1000:{"values":{0:SETTINGS.getSettingInt('Enable_Even'),1:self.evenEpisode,2:self.evenShuffle}}}
                tmpCitem.setdefault('rules',{}).update(nrules)
                self.log(" [%s] buildVideo: _injectRules, Even Show Distribution, new rules = %s"%(citem['id'],nrules))
            return tmpCitem
            
        citem     = _injectRules(citem) #inject temporary adv. channel rules here
        fileArray = self.runActions(RULES_ACTION_CHANNEL_BUILD_FILEARRAY_PRE, citem, list(), inherited=self) #inject fileArray thru adv. channel rules here
        self.log("[%s] buildVideo, channel pre fileArray items = %s"%(citem['id'],len(fileArray)),xbmc.LOGINFO)
        
        #Primary rule for handling fileList injection bypassing channel building below.
        if not _validFileList(fileArray): #if valid array bypass channel building
            for idx, paths in enumerate(citem.get('path',[])):
                if self.service._interrupt():
                    self.log("[%s] buildVideo, _interrupt"%(citem['id']))
                    self.pDialog = DIALOG._updateProgress(self.pDialog, self.pCount, message='%s: %s'%(LANGUAGE(32144),LANGUAGE(32213)),header=self.pHeader)
                    return []
                elif self.service._suspend(CPU_CYCLE):
                    self.log("[%s] buildVideo, _suspend"%(citem['id']))
                    self.monitor.waitForAbort(CPU_CYCLE)
                    continue
                else:
                    if   self.xsp.isXSP(paths):           paths = self.xsp.parseXSP(citem['id'], paths)# smartplaylist - convert tvshows types to multi-path, apply sort methods
                    elif isinstance(paths,(str,bytes)): paths = [paths]
                    
                    if self.sort.get("method","") == 'random':
                        self.log("[%s] buildVideo, random shuffling [%s/%s]"%(citem['id'],idx,len(paths)))
                        paths = Globals._randomShuffle(paths)               

                    for cnt, path in enumerate(paths):
                        if len(paths) > 1: self.pName = '%s %s/%s'%(citem['name'],cnt+1,len(paths))
                        self.pDialog = DIALOG._updateProgress(self.pDialog, self.pCount, message=f'{self.pName}',header=self.pHeader)
                        fileList = self.buildFileList(citem, self.runActions(RULES_ACTION_CHANNEL_BUILD_PATH, citem, path, inherited=self), 'video', self.limit, self.sort, self.limits, self.query)
                        if isinstance(fileList,list): fileArray.append(fileList)
                        if validate and len(fileList) > 0: break
                        self.log("[%s]  buildVideo, validate = %s, fileList [%s/%s], path [%s/%s]\n%s, "%(citem['id'],validate,len(fileList),(sum(len(sublist) for sublist in fileArray)),cnt,self.limit,path))
        
        fileArray = self.runActions(RULES_ACTION_CHANNEL_BUILD_FILEARRAY_POST, citem, fileArray, inherited=self) #flatten fileArray here to pass as fileList below
        #Primary rule for handling adv. interleaving, must return single list to avoid default interleave() below. Add adv. rule to setDictLST duplicates
        if isinstance(fileArray, list):
            self.log("[%s] buildVideo, channel post fileArray items = %s"%(citem['id'],len(fileArray)),xbmc.LOGINFO)
            if not _validFileList(fileArray):#check that at least one fileList in array contains meta
                self.log("[%s] buildVideo, channel fileArray In-Valid!"%(citem['id']),xbmc.LOGINFO)
                return False
            # self.log("[%s] buildVideo, fileArray = %s"%(citem['id'],','.join(['[%s]'%(len(fileList)) for fileList in fileArray])))
            fileList = self.runActions(RULES_ACTION_CHANNEL_BUILD_FILELIST_PRE, citem, interleave(fileArray, self.interleaveSet, self.interleaveRepeat), inherited=self)
            self.log('[%s] buildVideo, pre fileList items = %s'%(citem['id'],len(fileList)),xbmc.LOGINFO)
            fileList = self.runActions(RULES_ACTION_CHANNEL_BUILD_FILELIST_POST, citem, fileList, inherited=self)
            fileList = _injectFillers(citem, fileList, self.enableBCTs)
            self.log('[%s] buildVideo, post fileList items = %s'%(citem['id'],len(fileList)),xbmc.LOGINFO)
        else:
            fileList = fileArray
        return self.runActions(RULES_ACTION_CHANNEL_BUILD_FILELIST_RETURN, citem, fileList, inherited=self)


    def buildFileList(self, citem: dict, path: str, media: str='video', page=None, sort=None, limits=None, query=None) -> list: #buildChannels channel via vfs path.
        # madteevee (imports fork): page= default used to call SETTINGS at
        # class-definition time (immutable across Kodi session). Mutable
        # dict/list defaults made calls share state. All callers pass these
        # positionally today, so this is hygiene + future-proofing.
        if page   is None: page   = SETTINGS.getSettingInt('Page_Limit')
        if sort   is None: sort   = {}
        if limits is None: limits = {"end": -1, "start": 0, "total": 0}
        if query  is None: query  = {}
        self.log("[%s] buildFileList, path = %s\nmedia = %s, limit = %s, sort = %s, page = %s"%(citem['id'],path,media,page,sort,limits))
        self.loopback = None
        def __padFileList(fileItems, page):
            self.log('[%s] buildFileList, __padFileList fileItems'%(citem['id']))
            if page > len(fileItems):
                tmpList   = fileItems * (page // len(fileItems))
                remainder = page % len(fileItems)
                if remainder > 0:
                    tmpList.extend(fileItems[-remainder:])
                self.pDialog = DIALOG._updateProgress(self.pDialog, self.pCount, message=f'padding {remainder} files',header=self.pHeader)
                return tmpList
            return fileItems

        if self.xsp.isDXSP(path):
            path = self.xsp.parseDXSP(citem['id'], path, self.filter, self.incExtras)#dynamicplaylist - correct param issues, inject adv. filters rules.
  
        fileList = []
        dirCount = -1
        dirList  = [{'file':path}]
        npath    = path  # B6: latest dir path, for reparse when first page is all-extras
        reparseCount = 0 # B6: capped at MAX_BUILDFILELIST_REPARSE
        self.log("[%s] buildFileList, path = %s\nsort = %s, limits = %s, page = %s"%(citem['id'], path, sort, limits, page))
        while not self.monitor.abortRequested():
            if self.service._interrupt():
                self.log("[%s] buildFileList, _interrupt"%(citem['id']))
                self.pDialog = DIALOG._updateProgress(self.pDialog, self.pCount, message='%s: %s'%(LANGUAGE(32144),LANGUAGE(32213)), header=self.pHeader)
                return []
            elif self.service._suspend():
                self.log("[%s] buildFileList, _suspend"%(citem['id']))
                self.pDialog = DIALOG._updateProgress(self.pDialog, self.pCount, message='%s: %s'%(LANGUAGE(32144),LANGUAGE(32145)), header=self.pHeader)
                self.monitor.waitForAbort(CPU_CYCLE)
                continue
            elif len(dirList) == 0 or dirCount >= self.recursiveLimit:
                # B6 forward-port (madteevee): if dirList drained but pagination shows more
                # content (end < total) and the page is unfilled, re-insert the last path.
                # Handles the case where the first page is entirely filtered out (extras / 3D
                # / sub-min-duration / strm) — without the reparse the channel would bail
                # empty. Capped at MAX_BUILDFILELIST_REPARSE for pathological smartplaylists.
                if (len(dirList) == 0 and dirCount < self.recursiveLimit
                        and len(fileList) < page
                        and limits.get('end',0) < limits.get('total',0)
                        and reparseCount < MAX_BUILDFILELIST_REPARSE):
                    reparseCount += 1
                    dirList.insert(0, {'file': npath})
                    self.log('[%s] buildFileList, B6 reparse path (%s/%s) end=%s/total=%s, fileList=%s/%s'%(citem['id'],reparseCount,MAX_BUILDFILELIST_REPARSE,limits.get('end'),limits.get('total'),len(fileList),page))
                    continue
                if self.padFilelist and len(fileList) > 0 and len(fileList) < page: fileList = __padFileList(fileList,page)
                elif len(fileList) < page and len(dirList) > dirCount: self.pErrors.append(LANGUAGE(32262))
                self.log('[%s] buildFileList, no more folders to parse or recursive limit met.'%(citem['id']))
                break
            elif len(dirList) > 0:
                dirCount += 1
                dir   = dirList.pop(0)
                path  = dir.get('file')
                npath = path  # B6: track for reparse
                if dir.get("label"): self.pDialog = DIALOG._updateProgress(self.pDialog, self.pCount, message=f'parsing folder: {dir.get("label")}',header=self.pHeader)
                subfileList, subdirList, limits, errors = self.buildList(citem, path, media, abs(page - len(fileList)), sort, limits, dir, query) #parse all directories under root. Flattened hierarchies recommended to stream line channel building.

                if sort.get("method","") == 'random':
                    self.log("[%s] buildFileList, depth [%s/%s], random shuffling "%(citem['id'],dirCount,self.recursiveLimit))
                    subdirList  = Globals._randomShuffle(subdirList)
                    subfileList = Globals._randomShuffle(subfileList)
                    
                if isinstance(subfileList,list): fileList.extend(subfileList)
                if isinstance(subdirList,list):  dirList = Globals._setDictLST(dirList + subdirList)#recursive paths
                self.log('[%s] buildFileList, depth [%s/%s], adding fileList [%s/%s] remaining sub-directories [%s]\npath = %s, limits = %s'%(citem['id'],dirCount,self.recursiveLimit,len(fileList),page,len(dirList),path,limits))

        self.log("[%s] buildFileList, depth [%s/%s], returning fileList [%s/%s]"%(citem['id'],dirCount,self.recursiveLimit,len(fileList),page))
        return fileList


    def buildList(self, citem: dict, path: str, media: str='video', page: int=SETTINGS.getSettingInt('Page_Limit'), sort={}, limits={"end":-1,"start":0,"total":0}, dirItem={}, query={}):
        self.log("[%s] buildList, media = %s, path = %s\npage = %s, sort = %s, query = %s, limits = %s\ndirItem = %s"%(citem['id'],media,path,page,sort,query,limits,dirItem))
        nlimits = limits
        errors  = {}
        items   = self.runActions(RULES_ACTION_CHANNEL_REQUEST_FILELIST_PRE, citem, [], inherited=self)
        items, nlimits, errors = self.jsonRPC.requestList(citem, path, media, page, sort, self.filter, limits, query)
        items = self.runActions(RULES_ACTION_CHANNEL_REQUEST_FILELIST_POST, citem, items, inherited=self)
        
        if errors.get('message'):
            self.pErrors.append(errors['message'])
            return [], [], nlimits, errors

        elif not items:
            self.log("[%s] buildList, no request items found using path = %s"%(citem['id'],path))
            self.pErrors.append(LANGUAGE(32026))
            return [], [], nlimits, errors
                        
        elif items == self.loopback and limits != nlimits:# malformed jsonrpc queries will return root response, catch a re-parse and return.
            self.log("[%s] buildList, loopback detected using path = %s"%(citem['id'],path))
            self.pErrors.append(LANGUAGE(32030))
            return [], [], nlimits, errors
            
        elif items:
            self.loopback = items
            fileList, dirList = self.buildFiles(citem, path, items, media, page, sort, limits, dirItem, query)
            if len(fileList) == 0 and path in dirList: self.jsonRPC.autoPagination(citem['id'], path, query, limits) #rollback pagination limits due to _interrupt
            self.log("[%s] buildList, returning fileList [%s], dirList [%s]"%(citem['id'],len(fileList),len(dirList)))
            return fileList, dirList, nlimits, errors


    def buildFiles(self, citem: dict, path: str, items=None, media: str='video', page=None, sort=None, limits=None, dirItem=None, query=None):
        # madteevee (imports fork): same hygiene as buildFileList — sentinel
        # defaults so each call gets fresh containers and current Page_Limit.
        if items   is None: items   = []
        if page    is None: page    = SETTINGS.getSettingInt('Page_Limit')
        if sort    is None: sort    = {}
        if limits  is None: limits  = {"end": -1, "start": 0, "total": 0}
        if dirItem is None: dirItem = {}
        if query   is None: query   = {}
        fileList, dirList, seasoneplist = [], [], []
        for idx, item in enumerate(items):
            file        = item.get('file','')
            fileType    = item.get('filetype','file')
            self.fCount = int(idx*100)//len(items)
            if not item.get('type'):  item['type'] = query.get('key','files')
            if self.service._interrupt() or self.service._suspend():
                self.log("[%s] buildFiles, _interrupt/_suspend"%(citem['id']))
                self.pDialog = DIALOG._updateProgress(self.pDialog, self.pCount, message='%s: %s'%(LANGUAGE(32144),LANGUAGE(32213)), header=self.pHeader)
                return [], [{'file':path}]
            elif fileType == 'directory':
                dirList.append(item)
                continue
            elif fileType != 'file':
                continue
            else:
                self.pDialog = DIALOG._updateProgress(self.pDialog, self.pCount, message=f'{self.pName}: {self.fCount}%',header=self.pHeader)
                if file.startswith('pvr://'): #parse encoded fileitem otherwise no relevant meta provided via org. query. playable pvr:// paths are limited in Kodi.
                    self.log("[%s] buildFiles, IDX = %s, PVR item => FileItem! file = %s"%(citem['id'],idx,file),xbmc.LOGINFO)
                    item = Globals._decodePlot(item.get('plot',''))
                    file = item.get('file')
                if not file:
                    self.pErrors.append(LANGUAGE(32031))
                    self.log("[%s] buildFiles, IDX = %s, skipping missing playable file! path = %s"%(citem['id'],idx,path),xbmc.LOGINFO)
                    continue
                elif (file.lower().endswith('strm') and not self.incStrms): 
                    self.pErrors.append('%s STRM'%(LANGUAGE(32027)))
                    self.log("[%s] buildFiles, IDX = %s, skipping strm file! file = %s"%(citem['id'],idx,file),xbmc.LOGINFO)
                    continue
                elif not self.inc3D:
                    if self.is3D(item):
                        item['is3D'] = True
                        self.pErrors.append('%s 3D'%(LANGUAGE(32027)))
                        self.log("[%s] buildFiles, IDX = %s skipping 3D file! file = %s"%(citem['id'],idx,file),xbmc.LOGINFO)
                        continue

                if self.incStrmDetails and not item.get('streamdetails',{}).get('video',[]) and not file.startswith(tuple(VFS_TYPES)): #parsing missing meta, kodi rpc bug fails to return streamdetails during Files.GetDirectory.
                    item['streamdetails'] = self.jsonRPC.getStreamDetails(file, media)

                title   = (item.get("title")     or item.get("label") or dirItem.get('label') or '')
                tvtitle = (item.get("showtitle") or item.get("label") or dirItem.get('label') or '')
                if (item['type'].startswith(tuple(TV_TYPES)) or item.get("showtitle")):# This is a TV show
                    season  = int(item.get("season","0"))
                    episode = int(item.get("episode","0"))
                    if not file.startswith(tuple(VFS_TYPES)) and not self.incExtras and (season == 0 or episode == 0):
                        self.pErrors.append('%s Extras'%(LANGUAGE(32027)))
                        self.log("[%s] buildFiles, IDX = %s skipping extras! file = %s"%(citem['id'],idx,file),xbmc.LOGINFO)
                        continue

                    label = tvtitle
                    item["tvshowtitle"]  = tvtitle
                    item["episodetitle"] = title
                    item["episodelabel"] = '%s%s'%(title,' (%sx%s)'%(season,str(episode).zfill(2))) #Episode Title (SSxEE) Mimic Kodi's PVR label format
                    item["showlabel"]    = '%s%s'%(item["tvshowtitle"],' - %s'%(item['episodelabel']) if item['episodelabel'] else '')
                else: # This is a Movie
                    label = title
                    item["episodetitle"] = item.get("tagline","")
                    item["episodelabel"] = item.get("tagline","")
                    item["showlabel"]    = '%s%s'%(item.get("title",""), ' - %s'%(item['episodelabel']) if item['episodelabel'] else '')
                
                if not label:  
                    self.pErrors.append(LANGUAGE(32018)(LANGUAGE(30188)))
                    continue
                    
                dur = self.jsonRPC.getDuration(file, item, self.accurateDuration, self.saveDuration)
                if dur > self.minDuration: #include media that's duration is above the players seek tolerance & users adv. rule
                    self.pDialog = DIALOG._updateProgress(self.pDialog, self.pCount, message=f'{self.pName}: {self.fCount}%',header=self.pHeader)
                    item['duration']     = dur
                    item['media']        = media
                    item['originalpath'] = path #use for path sorting/playback verification 
                    item['friendly']     = PROPERTIES.getFriendlyName()
                    item['remote']       = PROPERTIES.getRemoteHost()
                        
                    if item.get("year",0) == 1601: item['year'] = 0 #detect kodi bug that sets a fallback year to 1601 https://github.com/xbmc/xbmc/issues/15554
                    spTitle, spYear = splitYear(label)
                    item['label']   = spTitle
                        
                    if item.get('year',0) == 0 and spYear: item['year'] = spYear #replace missing item year with one parsed from show title
                    item['plot'] = (item.get("plot","") or item.get("plotoutline","") or item.get("description","") or LANGUAGE(32020)).strip()
                        
                    holiday = citem.get('rules',{}).get(800,{}).get('values',{}).get(0,[{}])[0].get('holiday',{})
                    if holiday: #add seasonal meta
                        item["plot"] = "%s \n%s"%("[B]%s[/B] - [I]%s[/I]"%(holiday["name"],holiday["tagline"]) if holiday["tagline"] else "[B]%s[/B]"%(holiday["name"]),item["plot"])
                        
                    item['art'] = (item.get('art',{}) or dirItem.get('art',{}))
                    item.get('art',{})['icon'] = citem['logo']
                        
                    if item.get('trailer'): self.setTrailers(item)
                    if sort.get("method","") == 'episode' and (int(item.get("season","0")) + int(item.get("episode","0"))) > 0: 
                        seasoneplist.append([int(item.get("season","0")), int(item.get("episode","0")), item])
                    else: 
                        fileList.append(item)
                else: 
                    self.pErrors.append(LANGUAGE(32032))
                    self.log("[%s] buildFiles, IDX = %s skipping content no duration meta found! or runtime below minDuration (%s/%s) file = %s"%(citem['id'],idx,dur,self.minDuration,file),xbmc.LOGINFO)
        
        if sort.get("method","") == 'episode':
            self.log("[%s] buildFiles, sorting by episode"%(citem['id']))
            seasoneplist.sort(key=lambda seep: seep[1])
            seasoneplist.sort(key=lambda seep: seep[0])
            for seepitem in seasoneplist: 
                fileList.append(seepitem[2])
                
        elif sort.get("method","") == 'random':
            self.log("[%s] buildFiles, random shuffling"%(citem['id']))
            dirList  = Globals._randomShuffle(dirList)
            fileList = Globals._randomShuffle(fileList)
            
        self.log("[%s] buildFiles, returning (%s) files, (%s) dirs"%(citem['id'],len(fileList),len(dirList)))
        return fileList, dirList


    def addScheduling(self, citem: dict, fileList: list, now: int, start: int) -> list: #quota meet MIN_EPG_DURATION requirements. 
        self.log("[%s] addScheduling, IN fileList = %s, now = %s, start = %s"%(citem['id'],len(fileList),now,start))
        fileList = self.runActions(RULES_ACTION_CHANNEL_BUILD_TIME_PRE, citem, fileList.copy(), inherited=self)
        for idx, item in enumerate(fileList):
            item["idx"]   = idx
            item['start'] = start
            item['stop']  = start + item['duration']
            start = item['stop']
        fileList = self.runActions(RULES_ACTION_CHANNEL_BUILD_TIME_POST, citem, fileList.copy(), inherited=self) #adv. scheduling second pass and cleanup.
        self.log("[%s] addScheduling, OUT fileList = %s"%(citem['id'],len(fileList)))
        return fileList


    def is3D(self, item: dict) -> bool:
        if 'is3D' in item: return item['is3D']
        elif not item.get('streamdetails',{}).get('video',[]) and not item.get('file','').startswith(tuple(VFS_TYPES)):
            item['streamdetails'] = self.jsonRPC.getStreamDetails(item.get('file'), item.get('media','video'))
        details = item.get('streamdetails',{})
        if 'video' in details and details.get('video') != [] and len(details.get('video')) > 0:
            if len(details['video'][0]['stereomode'] or []) > 0: return True
        return False


    def setTrailers(self, item):
        dur = self.jsonRPC.getDuration(item.get('trailer'), accurate=True, save=False)
        if dur > 0:
            item.update({'label':'%s - %s'%(item.get("label",""),LANGUAGE(30187)),'episodetitle':'%s - %s'%(item.get("episodetitle",""),LANGUAGE(30187)),'episodelabel':'%s - %s'%(item.get("episodelabel",""),LANGUAGE(30187)),'duration':dur, 'runtime':dur, 'file':item.get('trailer'), 'streamdetails':{}})
            for genre in (item.get('genre',[]) or ['resources']): self.trailerCache.setdefault(genre.lower(),[]).append(item)
                        
                        
    def getTrailers(self, genre=None) -> dict:
        if genre: return self.trailerCache.get(genre,[]) #return genre
        return self.trailerCache #return all


    def resetPagination(self, citem):
        if isinstance(citem, list): return any([self.resetPagination(item) for item in citem])
        return any([self.jsonRPC.resetPagination(citem.get('id'), path) for path in citem.get('path',[]) if citem.get('id')])
    
        