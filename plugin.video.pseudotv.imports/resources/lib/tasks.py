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
from library    import Library
from builder    import Builder
from channels   import Channels
from xmltvs     import XMLTVS
from backup     import Backup
from multiroom  import Multiroom
from wizard     import Wizard
from server     import HTTP, Discovery
from renderers  import render_m3u, render_xmltv, write_atomic

from context_create import _auto

# madteevee (imports fork, imports.13 / C#10 Follow-up A): the M3U render,
# XMLTV render, atomic write, and the two M3U attr helpers (_m3u_attr_escape,
# _m3u_label_sanitize) all moved to resources/lib/renderers.py. Tasks no
# longer owns the writer pipeline — callers (Tasks.chkImports, Imports.
# syncAll, M3U._save, XMLTVS._save, server.py renumber) all dispatch to
# the module-level functions in renderers.py. Same WRITER_LOCK
# protection, same on-disk format. See the plan at
# /home/madalone/.claude/plans/misty-shimmying-liskov.md for details.


class Tasks(object):
    citems  = []
    cache   = SETTINGS.cache
    cacheDB = SETTINGS.cacheDB
    
    def __init__(self, service):
        self.service = service
        self.jsonRPC = service.jsonRPC
        self.player  = service.player
        self.monitor = service.monitor
        # imports fork (madteevee): live-imports sync runs on its OWN daemon
        # thread, decoupled from the priority queue. The queue's worker can
        # block for many seconds inside chkLibrary._exe (synchronous
        # cqueue.py:122 due to existing executor-submit semantics) which
        # starves any other queued task. A dedicated thread guarantees
        # imports sync fires regardless of queue state.
        self._startImportsThread()


    def _startImportsThread(self):
        """Spawn the daemon thread that runs chkImports on a 5-min interval.

        Idempotent: re-entry no-ops. Thread terminates on addon abort.
        """
        if getattr(self, '_imports_thread', None) is not None and self._imports_thread.is_alive():
            return
        import threading
        IMPORTS_INTERVAL_SECS = 300  # 5 minutes between auto-syncs
        KICK_POLL_SECS        = 3    # how often to check the UI-set kick property
        def _loop():
            try:
                # First-run: short delay so the addon's main init can complete
                # (Service / JSONRPC / property machinery) before we touch the
                # writable Channels/M3U/XMLTV singletons.
                if self.monitor.waitForAbort(5):
                    return
                while not self.monitor.abortRequested():
                    try:
                        self.chkImports()
                    except Exception as e:
                        self.log('imports thread, chkImports failed: %s'%(e), xbmc.LOGERROR)
                    # One-shot post-cycle PVR.Scan trigger. No-op after first
                    # successful fire (or when toggle is off). Spawns its own
                    # daemon thread for the readiness probe so this loop's
                    # cadence isn't disturbed.
                    try:
                        self._maybeFirePVRScan()
                    except Exception as e:
                        self.log('imports thread, _maybeFirePVRScan failed: %s'%(e), xbmc.LOGWARNING)
                    # Sleep for IMPORTS_INTERVAL_SECS, but wake early if the
                    # UI ("Refresh now" / "Refresh all" buttons via server.py,
                    # OR the per-edit kick from /imports/channel/edit.json)
                    # sets the chkImports.kick property. Polls every
                    # KICK_POLL_SECS so manual refresh has near-realtime UX.
                    # Use the EXT (cross-thread) property variant — getProperty/
                    # setProperty thread-scope the key (`<key>.<instance>.<tid>`)
                    # so a kick set from the HTTP-server thread would never be
                    # visible to this daemon thread. Verified empirically:
                    # 24 edits via imports.html sat unprocessed for 5 min until
                    # the natural 300s cycle fired.
                    elapsed = 0
                    last_defer_log = None  # dedupe deferred-kick log spam
                    while elapsed < IMPORTS_INTERVAL_SECS:
                        if self.monitor.waitForAbort(KICK_POLL_SECS):
                            return
                        elapsed += KICK_POLL_SECS
                        try:
                            kick = PROPERTIES.getEXTProperty('chkImports.kick', '')
                            if kick:
                                # Don't consume the kick unless chkImports CAN actually
                                # run right now. Previously, kicks fired during playback
                                # were silently swallowed: the wait loop would clear the
                                # property, fall through to chkImports(), which would
                                # then early-return due to Run_While_Playing — and the
                                # kick was lost. UI actions like ⟳ Renumber, Save, or
                                # ↻ Refresh would land server-side but their syncAll
                                # never ran, stranding the channels.json half-state
                                # (assigned_number=None on cleared channels) until the
                                # next natural 5-min cycle (or longer if playback
                                # continued). Now the kick survives until conditions
                                # allow chkImports to actually run.
                                blocker = None
                                if (self.service is not None
                                    and getattr(self.service, '_isPlaying', None)
                                    and self.service._isPlaying()):
                                    blocker = 'playback'
                                elif PROPERTIES.isRunning('Imports.syncAll'):
                                    blocker = 'syncAll-running'
                                elif PROPERTIES.isRunning('Builder.buildChannels'):
                                    # imports.14 / C#10 Follow-up B: kick survives
                                    # until Builder completes. UI actions
                                    # (⟳ Renumber, ↻ Refresh, Save) fired mid-build
                                    # are not lost — they fire ~3s after Builder's
                                    # chkRunning context manager releases the flag.
                                    blocker = 'builder-running'
                                if blocker is None:
                                    PROPERTIES.clrEXTProperty('chkImports.kick')
                                    self.log('imports thread, kick received (%s) — firing now'%(kick))
                                    break  # fall out of wait → next chkImports iteration
                                # Defer: leave kick set; log once per (kick, blocker) so
                                # successive 3s polls don't spam the log while video plays.
                                state = (kick, blocker)
                                if state != last_defer_log:
                                    self.log('imports thread, kick (%s) deferred — %s; keeping kick set'%(kick, blocker))
                                    last_defer_log = state
                        except Exception:
                            pass
            except Exception as e:
                self.log('imports thread, fatal: %s'%(e), xbmc.LOGERROR)
        self._imports_thread = threading.Thread(target=_loop, name='pseudotv-imports', daemon=True)
        self._imports_thread.start()
        self.log('_startImportsThread, daemon thread started (interval=%ds)'%(IMPORTS_INTERVAL_SECS))


    def _maybeFirePVRScan(self):
        """One-shot post-first-cycle PVR.Scan trigger.

        Probes for IPTV Simple's readiness via PVR.GetClients (in a separate
        daemon thread so the imports loop isn't blocked), then sends a single
        PVR.Scan to bump it into reading our fresh M3U+XML. Speeds up
        "Kodi-start → EPG-visible" when Kodi PVR Manager hasn't initialized
        IPTV Simple by the time chkImports cycle 1 has written the files.

        Honors the `Imports_PVR_Scan_After_Sync` setting (default on).
        Fires once per Kodi process lifetime — subsequent cycles let
        IPTV Simple's own m3uRefreshIntervalMins polling handle change
        detection.
        """
        if getattr(self, '_pvr_scan_done', False):
            return
        try:
            if not SETTINGS.getSettingBool('Imports_PVR_Scan_After_Sync'):
                return
        except Exception:
            return
        # Mark immediately to prevent any race from spawning multiple probe
        # threads. Worst case (probe fails), the user gets the fallback
        # IPTV Simple polling cadence — same as if the setting were off.
        self._pvr_scan_done = True

        import threading
        def _probe_and_fire():
            try:
                deadline = time.time() + 90  # cap probe at 90 sec
                while time.time() < deadline:
                    if self.monitor.waitForAbort(5):
                        return
                    try:
                        client = self.service.jsonRPC.getPVRClient(PVR_CLIENT_ID)
                        # Kodi's PVR.GetClients response doesn't include a
                        # `connected`/`enabled` flag — the client is in the
                        # list iff PVR Manager has registered it. So
                        # presence + a clientid is the readiness signal.
                        if client and client.get('clientid') is not None:
                            clientid = client.get('clientid')
                            self.log('PVR scan probe: IPTV Simple registered (clientid=%s) — firing PVR.Scan'
                                     % clientid, xbmc.LOGINFO)
                            self.service.jsonRPC.PVRScan(clientid)
                            return
                    except Exception as e:
                        self.log('PVR scan probe: poll failed: %s'%(e), xbmc.LOGWARNING)
                self.log('PVR scan probe: no PVR client ready in 90s — skipping PVR.Scan',
                         xbmc.LOGWARNING)
            except Exception as e:
                self.log('PVR scan probe: fatal: %s'%(e), xbmc.LOGERROR)
        threading.Thread(target=_probe_and_fire, name='pseudotv-pvr-scan', daemon=True).start()


    def log(self, msg, level=xbmc.LOGDEBUG):
        return log('%s: %s'%(self.__class__.__name__,msg),level)


    def _client(self):
        self.service._que(self.monitor.chkIdle,1)
        self.service._que(self.chkHTTP        ,1)
        self.service._que(self.chkInstanceID  ,1)
        self.service._que(self.chkPVRBackend  ,1)
        self.service._que(self.chkDebugging   ,1)
        self.log('_initialize, _client...')
        
        
    def _host(self):
        self._client()
        self.service._que(self.chkDirs,1)
        self.service._que(self.chkBackup,1)
        self.service._que(self.chkCrash,1)
        self.log('_initialize, _host...')
    
    
    def chkHTTP(self):
        timerit(HTTP)(0.1,self.service)
        self.log('chkHTTP')
        
        
    def chkInstanceID(self):
        self.log('chkInstanceID')
        PROPERTIES.getInstanceID()
        

    def chkWizard(self):
        self.log('chkWizard')
        if not SETTINGS.hasWizardRun():
            BUILTIN.executescript('special://home/addons/%s/resources/lib/utilities.py, Run_Wizard'%(ADDON_ID))
            

    def chkDebugging(self):
        self.log('chkDebugging')
        if SETTINGS.getSettingBool('Debug_Enable'):
            if DIALOG.yesnoDialog(LANGUAGE(32142),autoclose=4):
                self.log('_chkDebugging, disabling debugging.')
                SETTINGS.setSettingBool('Debug_Enable',False)
                DIALOG.notificationDialog(LANGUAGE(32025))
        #Force enable Kodi Debugging when enabled in PseudoTV via JSONRPC only if Kodi access allowed by user.
        if SETTINGS.getSettingBool('Enable_Kodi_Access'):
            self.jsonRPC.toggleShowLog(SETTINGS.getSettingBool('Debug_Enable'))
                    
             
    def chkDiscovery(self):
        timerit(Discovery)(0.1,*(self.service, Multiroom(service=self.service)))
        self.log('chkDiscovery')
         
         
    def chkBackup(self):
        self.log('chkBackup')
        Backup().hasBackup()


    def chkCrash(self):
        citem = (SETTINGS.getCacheSetting('KODI.CRASH.JSONRPC.CITEM') or {})
        SETTINGS.setCacheSetting('KODI.CRASH.JSONRPC.CITEM',None)
        if citem:
            self.log('chkCrash\n%s'%(citem))
            # with BUILTIN.busy_dialog(), PROPERTIES.suspendActivity():
                # channels = Channels(writable=True)
                # chanLST  = channels.getChannels()
                # idx, channel = channels.findChannel(citem, chanLST)
                # chanLST[idx].update({'enabled':False})
                # if channels.setChannels(chanLST):
                    # DIALOG.okDialog(f'Kodi encountered a fatal crash while parsing a [B]{ADDON_NAME}[\B] channel.\nPlease check the channel configuration for [B]{citem.get('name')}[\B]\n Channel [B]{citem.get('number')}[\B] temporarily disabled!', usethread=False)
                # del channels
  
  
    def chkPVRBackend(self): 
        self.log('chkPVRBackend')
        if SETTINGS.hasAddon(PVR_CLIENT_ID,notify=True):
            if not SETTINGS.hasPVRInstance():
                SETTINGS.setPVRRemote(PROPERTIES.getRemoteHost(), PROPERTIES.getFriendlyName(), cache=False)


    def chkQueTimer(self):
        self.log('chkQueTimer')
        self._chkEpochTimer('chkVersion'      , self.chkVersion       , 43200 , 1)#12HRS
        self._chkEpochTimer('chkKodiSettings' , self.chkKodiSettings  , 10800 , 1)#3HRS
        self._chkEpochTimer('chkDiscovery'    , self.chkDiscovery     , 300   , 1)#5MINS
        self._chkEpochTimer('chkQUES'         , self.chkQUES          , 120   , 1)#2MINS
        
        if not self.service.isClient:
            self._chkEpochTimer('chkFiles'    , self.chkFiles         , 600   , 1)#10MINS
            # 'Library_Walk_Interval' (hours, default 12, range 1-72) controls how often
            # the autotune library types are re-walked. Channel-build still fires immediately
            # via _chkPropTimer('chkUpdate') on user actions, so this only paces background work.
            self._chkEpochTimer('chkLibrary'  , self.chkLibrary       , max(1, SETTINGS.getSettingInt('Library_Walk_Interval') or 12) * 3600 , 2)
            # imports fork (madteevee): chkImports runs on its OWN daemon thread
            # (Tasks._startImportsThread) — NOT through this queue. The priority
            # queue can block on chkLibrary's synchronous _exe, which would
            # starve imports sync indefinitely on first-install.
            
        #immediate run, bypass schedule
        self._chkPropTimer('chkPVRRefresh'    , self.chkPVRRefresh    , 1) 
        self._chkPropTimer('chkChanged'       , self.chkChanged       , 3)
        
        
    #_chkEpochTimer trigger - Time = 0 == Run
    def _chkEpochTimer(self, key, func, runevery=900, priority=-1, nextrun=None, *args, **kwargs):
        if nextrun is None: nextrun = int(PROPERTIES.getEXTProperty(key,'0')) # nextrun == 0 => force que
        epoch = int(time.time())
        if epoch >= nextrun:
            self.log('_chkEpochTimer, key = %s, last run %s' % (key, epoch - nextrun))
            PROPERTIES.setEXTProperty(key, (epoch + runevery))
            self.service._que(func, priority, *args, **kwargs)


     #_chkPropTimer trigger - True == Run
    def _chkPropTimer(self, key, func, priority=-1, *args, **kwargs):
        if PROPERTIES.getPropTimer(key):
            # v.44 chkChanged debounce: defer queueing chkChanged if a Builder.buildChannels
            # task is currently running. Otherwise multiple chkChanged ticks (separated by
            # SERVICE_INTERVAL) can each queue fresh Builder()-bound buildChannels tasks per
            # channel — and since the bound method on each fresh Builder instance is a distinct
            # Python object, cqueue._exists package-equality dedup (cqueue.py:126) doesn't
            # recognize them as duplicates. Property stays True so the next tick after the
            # build completes will retry. FORK_NOTES priority #3.
            if key == 'chkChanged' and PROPERTIES.isRunning('Builder.buildChannels'):
                self.log('_chkPropTimer, %s deferred (Builder.buildChannels running)'%(key))
                return
            self.log('_chkPropTimer, key = %s'%(key))
            PROPERTIES.clrEXTProperty(key)
            self.service._que(func, priority, *args, **kwargs)
            

    @cacheit(expiration=datetime.timedelta(minutes=15))
    def getOnlineVersion(self):
        try:    ONLINE_VERSION = re.compile('" version="(.+?)" name="%s"'%(ADDON_NAME)).findall(str(Globals.requestURL(ADDON_URL)))[0]
        except Exception: ONLINE_VERSION = ADDON_VERSION
        self.log('getOnlineVersion, version = %s'%(ONLINE_VERSION))
        return ONLINE_VERSION
        
        
    def chkVersion(self):
        update = False
        ONLINE_VERSION = self.getOnlineVersion()
        if ADDON_VERSION < ONLINE_VERSION: 
            update = True
            DIALOG.notificationDialog(LANGUAGE(30073)%(ONLINE_VERSION))
        elif ADDON_VERSION > (SETTINGS.getCacheSetting('lastVersion', checksum=ADDON_VERSION, revive=True) or '0.0.0'):
            SETTINGS.setCacheSetting('lastVersion',ADDON_VERSION, checksum=ADDON_VERSION)
            BUILTIN.executescript('special://home/addons/%s/resources/lib/utilities.py, Show_Changelog'%(ADDON_ID))
        self.log('chkVersion, update = %s, installed version = %s, online version = %s'%(update,ADDON_VERSION,ONLINE_VERSION))
        SETTINGS.setSetting('Update_Status',{'True':'[COLOR=yellow]%s [B]v.%s[/B][/COLOR]'%(LANGUAGE(32168),ONLINE_VERSION),'False':'None'}[str(update)])


    def chkKodiSettings(self):
        self.log('chkKodiSettings')
        # When 'Decouple_Kodi_PVR' is enabled, the user has opted to make pseudotv's
        # Min_Days, Max_Days, and OSD_Timer authoritative. Skip the auto-sync from
        # Kodi's PVR settings (epg.pastdaystodisplay / epg.futuredaystodisplay /
        # pvrmenu.displaychannelinfo) so the user's values aren't overwritten hourly.
        if SETTINGS.getSettingBool('Decouple_Kodi_PVR'):
            self.log('chkKodiSettings, Decouple_Kodi_PVR enabled; skipping Kodi PVR sync')
            return
        MIN_GUIDEDAYS = SETTINGS.setSettingInt('Min_Days' ,self.jsonRPC.getSettingValue('epg.pastdaystodisplay'     ,default=1))
        MAX_GUIDEDAYS = SETTINGS.setSettingInt('Max_Days' ,self.jsonRPC.getSettingValue('epg.futuredaystodisplay'   ,default=3))
        OSD_TIMER     = SETTINGS.setSettingInt('OSD_Timer',self.jsonRPC.getSettingValue('pvrmenu.displaychannelinfo',default=5))
         

    def chkDirs(self):
        [(self.log('chkDirs, creating [%s]'%(folder)),FileAccess.makedirs(folder)) for folder in [LOGO_LOC,FILLER_LOC,TEMP_LOC,TEMP_IMAGE_LOC] if not FileAccess.exists(os.path.join(folder,''))]


    def chkFiles(self):
        for file in [CHANNELFLEPATH,M3UFLEPATH,XMLTVFLEPATH,GENREFLEPATH]:
            if not FileAccess.exists(file):
                self.log('chkFiles, missing [%s]'%(file))
                return self.service._que(self.chkChannels,3)


    def chkFillers(self, channels=None, silent=None):
        if silent is None: silent = BUILTIN.isPlaying()
        self.log('chkFillers')
        # if channels is None: channels = self.getChannels())))
        # if len(channels) > 0:
            # for fidx, ftype in enumerate(FILLER_TYPES):
                # fpath = os.path.join(FILLER_LOC,ftype.lower(),'')
                # if not FileAccess.exists(fpath): FileAccess.makedirs(fpath)
                
                # for cidx, citem in enumerate(channels):
                    # cpath = os.path.join(fpath, citem.get('name','').lower())
                    # if not FileAccess.exists(cpath): FileAccess.makedirs(cpath)
                    
                    # for gidx, genres in channels()
                    
                    # if not FileAccess.exists(os.path.join(FILLER_LOC,ftype.lower(),genre.lower(),'')):
                        # FileAccess.makedirs(os.path.join(FILLER_LOC,ftype.lower(),''))
                
                # for ftype in FILLER_TYPES[1:]:
                    # for genre in genres:
                        # if not FileAccess.exists(os.path.join(FILLER_LOC,ftype.lower(),genre.lower(),'')):
                            # pDialog = DIALOG.progressBGDialog(int(idx*50//len(channels)), pDialog, message='%s: %s'%(genre,int(idx*100//len(channels)))+'%', header='%s, %s'%(ADDON_NAME,LANGUAGE(32179)))
                            # FileAccess.makedirs(os.path.join(FILLER_LOC,ftype.lower(),genre.lower()))
                    
                    # if not FileAccess.exists(os.path.join(FILLER_LOC,ftype.lower(),citem.get('name','').lower())):
                        # if ftype.lower() == 'adverts': IGNORE = IGNORE_CHTYPE + MOVIE_CHTYPE
                        # else:                          IGNORE = IGNORE_CHTYPE
                        # if citem.get('name') and not citem.get('radio',False) and citem.get('type') not in IGNORE: 
                            # pDialog = DIALOG.progressBGDialog(int(idx*50//len(channels)), pDialog, message='%s: %s'%(citem.get('name'),int(idx*100//len(channels)))+'%', header='%s, %s'%(ADDON_NAME,LANGUAGE(32179)))
                            # FileAccess.makedirs(os.path.join(FILLER_LOC,ftype.lower(),citem['name'].lower()))
            # pDialog = DIALOG.progressBGDialog(100, pDialog, message=LANGUAGE(32025), header='%s, %s'%(ADDON_NAME,LANGUAGE(32179)))
        

    def chkLibrary(self, types=None, silent=None):
        if silent is None: silent = BUILTIN.isPlaying()
        self.log("chkLibrary, types = %s, silent = %s"%(types,silent))
        complete = set()
        # madteevee (imports fork): persistent background progress dialog
        # spanning the entire library walk. chkLibrary triggers chkChannels
        # on completion (which then triggers Builder), but the walk itself
        # is a 1-10-min JSON-RPC-heavy operation with no on-screen
        # indicator. Operator was waiting for a build to start after adding
        # channels and saw nothing — looked broken. This toast surfaces
        # the activity so operator knows the system is alive and working
        # toward the build. Suppressed for "single library type" re-queue
        # paths (`types is not None`) which are interrupted-walk retries.
        # Gated by `Imports_Library_Walk_Toast` setting (default ON) so
        # operator can silence the indicator if preferred.
        # NOT silenced during playback — DialogProgressBG is a small
        # top-right indicator (or top full-width depending on skin) and
        # less intrusive than a notification.
        lib_progress = None
        is_full_walk = types is None
        try:    _show_toast = SETTINGS.getSettingBool('Imports_Library_Walk_Toast')
        except: _show_toast = True
        if is_full_walk and _show_toast:
            try:
                lib_progress = xbmcgui.DialogProgressBG()
                lib_progress.create(ADDON_NAME, 'Library walk: starting')
            except Exception as _lp_e:
                self.log("chkLibrary, DialogProgressBG create failed: %s"%(_lp_e), xbmc.LOGWARNING)
                lib_progress = None
        try:
            library  = Library(service=self.service, writable=True)
            library.searchRecommended()
            if types is None: types = AUTOTUNE_TYPES
            # madteevee (imports fork): if operator has `Skip_Music_Library`
            # set (which already short-circuits Library.getMusicInfo() to
            # return empty), also skip the music-typed AUTOTUNE_TYPES
            # iterations entirely. Previously chkLibrary still did the
            # full JSON-RPC roundtrip for "Music Genres" and "Mixed Music"
            # before getting an empty result — wasted time and confused
            # operators who saw "Music Genres library not found! starting
            # update." in the log despite the setting being on.
            try:    _skip_music = SETTINGS.getSettingBool('Skip_Music_Library')
            except: _skip_music = False
            if _skip_music:
                MUSIC_TYPES = {'Music Genres', 'Mixed Music'}
                _orig_len = len(types)
                types = [t for t in types if t not in MUSIC_TYPES]
                if len(types) < _orig_len:
                    self.log("chkLibrary, skipped %d music types per Skip_Music_Library setting" % (_orig_len - len(types)))
            _total = max(1, len(types))
            for idx, type in enumerate(types):
                # Update progress before each library type iteration
                if lib_progress is not None:
                    try:
                        _pct = int((idx / _total) * 100)
                        lib_progress.update(_pct, ADDON_NAME, 'Library walk: %s (%d/%d)' % (type, idx + 1, _total))
                    except Exception: pass
                items = library.getLibrary(type)
                if self.service._interrupt() or self.service._suspend():
                    self.log("chkLibrary, _interrupt/_suspend")
                    return self.service._que(self.chkLibrary,2,*(types[idx:],silent))
                elif items:
                    self.log("chkLibrary, %s library found! Setting items (%s), queuing update."%(type,len(items)))
                    complete.add(library.setLibrary(type, items))
                    self.service._que(library.updateLibrary,-1,*([type],True))
                else:
                    self.log("chkLibrary, %s library not found! starting update."%(type))
                    complete.add(library.updateLibrary([type],silent))
            del library
            if any(list(complete)): self.service._que(self.chkChannels,3)
            self.log("chkLibrary, complete = %s"%(any(list(complete))))
        finally:
            # Always close the persistent dialog, regardless of which exit
            # path (normal completion, interrupt/suspend re-queue, exception).
            if lib_progress is not None:
                try: lib_progress.close()
                except Exception: pass
        

    # madteevee (imports fork, imports.13 / C#10 Follow-up A): _writeAtomic,
    # _writeAtomicLocked, _renderM3U, _renderXMLTV all moved to module-level
    # pure functions in resources/lib/renderers.py. Tasks no longer owns
    # the writer pipeline. See tasks.py top-of-file comment and the plan
    # at /home/madalone/.claude/plans/misty-shimmying-liskov.md.


    def _refreshPVR(self):
        """imports fork (madteevee): force IPTV Simple to reload by toggling
        addon enabled state via JSON-RPC.

        CRITICAL TIMING: PVR Manager's full Stop sequence can take 20-30s
        (verified empirically: Stopping at 20:10:55 → Stopped at 20:11:24).
        Re-enabling the PVR client BEFORE Kodi PVR Manager has finished
        Stopping causes the Recreate to land in an inconsistent state and
        PVR Manager ends up permanently Stopped, requiring a Kodi restart.

        Wait at least 30s between disable and enable (with abort-aware
        polling so shutdown still works promptly). Skip toggle entirely if
        PVR Manager appears to be already in a transition state.
        """
        if PROPERTIES.isRunning('Tasks._refreshPVR'):
            self.log('_refreshPVR, already running — skipping')
            return
        try:
            with PROPERTIES.chkRunning('Tasks._refreshPVR'):
                self.log('_refreshPVR, toggling %s OFF'%(PVR_CLIENT_ID))
                self.service.jsonRPC.sendJSON({
                    "method": "Addons.SetAddonEnabled",
                    "params": {"addonid": PVR_CLIENT_ID, "enabled": False},
                })
                # Wait for PVR Manager to fully Stop before re-enabling.
                # 30s + abort-aware polling.
                for _ in range(15):
                    if self.monitor.waitForAbort(2):
                        return
                self.log('_refreshPVR, toggling %s ON'%(PVR_CLIENT_ID))
                self.service.jsonRPC.sendJSON({
                    "method": "Addons.SetAddonEnabled",
                    "params": {"addonid": PVR_CLIENT_ID, "enabled": True},
                })
        except Exception as e:
            self.log('_refreshPVR, failed: %s'%(e), xbmc.LOGWARNING)


    def chkChanged(self, channels=None, silent=None):
        if silent is None: silent = BUILTIN.isPlaying()
        if channels is None: channels = self.getChannels()
        changes = [channel for channel in channels if channel.get('changed',False)]
        # madteevee (imports fork): import channels MUST NOT go through Builder.
        # Imports get their EPG from Imports.syncAll, not from buildChannels.
        # Builder._verify skips them (no `path` field) AND doesn't clear the
        # `changed:True` flag, so any import that ever got flagged (manual
        # operator action, legacy state, a bug elsewhere) stays in the queue
        # forever — every chkChanged tick re-queues them, Builder skips again,
        # ad infinitum. Worse, Custom channels (Cartoon Cartoons / Saturday
        # Morning TV) that legitimately need a rebuild get buried behind 100+
        # stale import flags and never reach the queue head.
        # Fix: filter imports out of the build queue AND clear their stale
        # changed=True flag (persists via Channels.setChannels merge-on-write)
        # so the queue self-heals. Custom channels still flow normally.
        stale_imports = [c for c in changes if c.get('type') == 'import']
        if stale_imports:
            self.log('chkChanged, clearing stale changed=True on %s import channels (imports get EPG from syncAll, not Builder)' % (len(stale_imports)))
            try:
                ch = Channels(writable=True)
                modified = set()
                for c in stale_imports:
                    c['changed'] = False
                    ch.addChannel(c)
                    modified.add(c.get('id'))
                ch.setChannels(modified_ids=modified)
            except Exception as e:
                self.log('chkChanged, failed to clear stale import flags: %s' % e, xbmc.LOGWARNING)
        changes = [c for c in changes if c.get('type') != 'import']
        self.log('chkChanged, changes = %s'%(len(changes)))
        for channel in changes:
            self.service._que(Builder(service=self.service).buildChannels,3,*([channel],False,silent))


    def chkImports(self, silent=None):
        """imports fork (madteevee): periodic live-imports sync.

        Runs Imports.syncAll() directly (NOT through Builder), so first-install
        scenarios — operator has imports configured but zero PseudoTV-built
        channels — still get synced. Builder integration handles the case where
        operator-built channels exist alongside imports.

        Per-import conditional fetch (mtime / ETag / If-Modified-Since) makes
        repeated calls cheap when sources haven't changed.
        """
        self.log('chkImports')
        # Honor the operator's "Run during playback" preference (Run_While_Playing).
        # When that's off and a video is playing, defer the entire cycle — without
        # this early-out we'd still load the M3U + XMLTV cache files (10+ MB of
        # disk I/O) before syncAll's per-iteration _interrupt() check breaks the
        # loop, AND xbmcvfs.Stat on a player addon's special:// M3U can wedge the
        # imports thread for minutes during playback (verified empirically).
        if (self.service is not None
            and getattr(self.service, '_isPlaying', None)
            and self.service._isPlaying()):
            self.log('chkImports, deferred (Run_While_Playing=false and a video is playing)')
            return
        if PROPERTIES.isRunning('Imports.syncAll'):
            self.log('chkImports, deferred (Imports.syncAll already running)')
            return
        # madteevee (imports fork, imports.14 / C#10 Follow-up B):
        # close the in-process race where this daemon would write
        # pseudotv.m3u / pseudotv.xml while Builder thread is mid-build.
        # Symmetric to chkChanged's defer at tasks.py:329 and
        # chkPVRRefresh's defer at tasks.py:789 — every background
        # worker that touches the same on-disk artifacts now defers on
        # Builder.buildChannels. The Custom-merge-from-disk workaround
        # at imports.py:916-955 still defends the cross-process race
        # (context_record.py is a separate Python process) and the
        # external race (operator hand-edits pseudotv.xml/m3u); those
        # cannot be reached by an in-process property gate. See plan
        # at /home/madalone/.claude/plans/misty-shimmying-liskov.md.
        if PROPERTIES.isRunning('Builder.buildChannels'):
            self.log('chkImports, deferred (Builder.buildChannels running)')
            return
        try:
            from imports import Imports
            from m3u     import M3U
            from xmltvs  import XMLTVS
            channels = Channels(writable=True)
            if not list(channels.getImports() or []):
                self.log('chkImports, no imports configured; skipping')
                return
            with PROPERTIES.chkRunning('Imports.syncAll'):
                # writable=False: we'll do our OWN atomic write below.
                # XMLTVS.__del__ would otherwise try _save() on GC (unreliable
                # timing) AND _save itself is wrapped in @debounceit which
                # combined with FileLock's class-level thread_lock can deadlock
                # when the second chkImports cycle fires while the first's
                # debounced save is still pending (verified empirically in
                # step 3 smoke test: T:325618 _save thread hung 5+ minutes on
                # second cycle without ever completing or releasing the lock).
                m3u   = M3U(writable=False)
                xmltv = XMLTVS(writable=False, m3u=m3u)
                results = Imports(channels=channels, m3u=m3u, xmltv=xmltv,
                                  service=self.service).syncAll()
                # Atomic write under WRITER_LOCK. imports.13: writes go through
                # the module-level renderers (consolidates the duplicated
                # render that used to live in m3u._save / xmltvs._save).
                _m3u = render_m3u(m3u.M3UDATA)
                if _m3u is not None:
                    write_atomic(M3UFLEPATH, _m3u)
                else:
                    self.log('chkImports, skipped M3U write — empty M3UDATA (would wipe disk M3U)', xbmc.LOGWARNING)
                _xml = render_xmltv(xmltv.XMLTVDATA)
                if _xml is not None:
                    write_atomic(XMLTVFLEPATH, _xml)
                else:
                    self.log('chkImports, skipped XML write — empty XMLTVDATA (would produce corrupt self-closing root)', xbmc.LOGWARNING)
                del xmltv
                del m3u
                del channels
            # NOTE: We DON'T auto-call _refreshPVR every cycle anymore. Empirically
            # the disable+enable toggle races PVR Manager's 30-60s Stop sequence:
            # if the re-enable lands before Stop completes, PVR Manager ends up
            # permanently Stopped requiring a Kodi restart (verified twice in
            # step 4 smoke testing).
            #
            # Instead: rely on IPTV Simple's own m3uRefreshIntervalMins (instance
            # config) to pick up file-mtime changes. _refreshPVR is still wired
            # for explicit operator request via the UI's "↻ Refresh" button —
            # operator-initiated and infrequent, so race window is acceptable.
        except Exception as e:
            self.log('chkImports, failed! %s'%(e), xbmc.LOGERROR)
        
        
    def _filterChannelsNeedingBuild(self, channels):
        # madteevee: pre-filter for chkChannels. Returns subset of channels
        # that actually need a build, so Builder.buildChannels isn't invoked
        # (and the 16+ MB pseudotv.xml isn't re-loaded + re-parsed) for no-op
        # passes.
        #
        # Previous behavior (pre 2026-05-11 architectural change):
        #   When Enable_Changed=true, this filter UNCONDITIONALLY appended
        #   every Custom channel, leaving the real CRC-based change detection
        #   to Builder.buildChannels' __hasChanged closure. Result: every
        #   chkChannels tick (every ~3s, gated by Builder.Running) queued ALL
        #   Custom channels — each Builder pass took ~50s for the M3U/XMLTV
        #   _load + filler enumeration + _verify, then __hasChanged correctly
        #   returned False and the pass exited without writing anything. Net:
        #   continuous 50s-per-channel waste with no actual rebuild work, and
        #   operator tunes were slow due to file I/O contention.
        #
        # New behavior: do the CRC check HERE. Channels where no source file
        # has changed are excluded from `needed` so Builder isn't queued at
        # all for them. The side-effect concern from the old comment (calling
        # getFileCRC updates the cached CRC, so a subsequent buildChannels
        # would see False and skip even when filter intended a rebuild) is
        # solved by setting `citem['changed'] = True` for any channel whose
        # CRC differed — buildChannels' __hasChanged honors the explicit flag
        # before falling through to its own getFileCRC call.
        if not channels: return []
        now       = getUTCstamp()
        nstart    = roundTimeDown(now, offset=60)
        fallback  = epochTime(nstart, tz=False).strftime(DTFORMAT)
        threshold = now + ((MAX_GUIDEDAYS * 86400) - 43200)
        detect    = SETTINGS.getSettingBool('Enable_Changed')

        xmltv  = XMLTVS()
        stops  = dict(xmltv.loadStopTimes(channels, fallback=fallback))
        needed = []
        crc_detected = 0
        for citem in channels:
            if citem.get('changed', False):
                needed.append(citem); continue
            if stops.get(citem['id'], 0) <= threshold:
                needed.append(citem); continue
            if detect:
                # CRC-based change detection moved here from buildChannels.
                # Side-effect mitigation: if any source file's CRC differs,
                # set citem['changed']=True so buildChannels' __hasChanged
                # honors the rebuild signal via its FIRST branch (the
                # explicit `changed=True` check), bypassing its own CRC call
                # which would now see the updated cached value and return
                # False. The in-memory citem reference is the same one the
                # caller passes to Builder via service._que, so the flag
                # propagates without a disk write.
                try:
                    src_files = [f for f in citem.get('path', [])
                                 if f.endswith(tuple(KODI_PLAYLISTS + BASIC_PLAYLISTS))]
                    if any(SETTINGS.getFileCRC(f) for f in src_files):
                        citem['changed'] = True
                        crc_detected += 1
                        needed.append(citem)
                except Exception as e:
                    # If the CRC check itself fails (file missing, permissions,
                    # etc.) fall back to the old conservative behavior of
                    # including the channel so buildChannels can investigate.
                    self.log('_filterChannelsNeedingBuild, CRC check failed for [%s], conservatively including: %s'
                             % (citem.get('id'), e), xbmc.LOGWARNING)
                    needed.append(citem)
        # madteevee (imports fork): symmetric fix to chkChanged. Import channels
        # MUST NOT go through Builder.buildChannels — they have no `path` field,
        # so Builder._verify logs "SKIPPING - missing necessary channel meta"
        # AND doesn't clear the `changed:True` flag. Every chkChannels tick
        # then re-queues them via the `citem.get('changed', False)` branch
        # above, burying Custom channels that legitimately need a rebuild
        # behind 100+ stale imports. Imports get EPG from Imports.syncAll.
        # Fix: drop imports from `needed` AND clear any stale changed=True so
        # the queue self-heals (persists via Channels.setChannels merge-on-write).
        stale_imports = [c for c in needed if c.get('type') == 'import']
        cleared = [c for c in stale_imports if c.get('changed', False)]
        if stale_imports:
            self.log('_filterChannelsNeedingBuild, dropping %s import channels from queue (imports get EPG from syncAll)' % (len(stale_imports)))
            if cleared:
                self.log('_filterChannelsNeedingBuild, cleared stale changed=True on %s import channels' % (len(cleared)))
                try:
                    ch = Channels(writable=True)
                    modified = set()
                    for c in cleared:
                        c['changed'] = False
                        ch.addChannel(c)
                        modified.add(c.get('id'))
                    ch.setChannels(modified_ids=modified)
                except Exception as e:
                    self.log('_filterChannelsNeedingBuild, failed to clear stale import flags: %s' % e, xbmc.LOGWARNING)
        needed = [c for c in needed if c.get('type') != 'import']
        self.log('_filterChannelsNeedingBuild, total = %s, needed = %s, crc_detected = %s, detect = %s'%(len(channels), len(needed), crc_detected, detect))
        return needed


    def chkChannels(self, channels=None, silent=None):
        if silent is None: silent = BUILTIN.isPlaying()
        if channels is None: channels = self.getChannels()
        if len(channels) > 0:
            self.log('chkChannels, channels = %s'%(len(channels)))
            # madteevee: hoist the freshness check from Builder.buildChannels'
            # inner __needsUpdate so we don't even queue a job for fresh
            # channels. The per-channel guard still runs inside buildChannels
            # for any channel that passes the filter.
            needed = self._filterChannelsNeedingBuild(channels)
            if not needed:
                self.log('chkChannels, all %s channels fresh - skipping buildChannels'%(len(channels)))
                return
            if not PROPERTIES.hasChannels():
                self.service._que(Builder(service=self.service).buildChannels,3,*(needed,False,silent))
            else:
                [self.service._que(Builder(service=self.service).buildChannels,3,*([channel],False,silent)) for channel in needed]
            if SETTINGS.getSettingBool('Build_Filler_Folders'): self.service._que(self.chkFillers,4,*(needed,silent))
        else:
            self.log('chkChannels, No Channels Configured!')
            if not SETTINGS.hasAutotuned():
                if SETTINGS.setAutotuned(_auto()):
                    if self.service._sleep(5):
                        self.service._que(self.chkChannels,3)
            elif PROPERTIES.hasEnabledServers():
                PROPERTIES.setPropTimer('chkPVRRefresh')#refresh pvr guide


    @debounceit(M3U_REFRESH)
    def chkPVRRefresh(self, brute=None):
        # v.46: re-evaluate Enable_PVR_RELOAD per call. Previous default-arg form
        # `brute=SETTINGS.getSettingBool('Enable_PVR_RELOAD')` evaluated once at
        # class-definition time, so toggling the setting at runtime never reached
        # subsequent calls. Sentinel `None` lets explicit callers still pass an
        # override; everything else picks up the current setting value.
        if brute is None: brute = SETTINGS.getSettingBool('Enable_PVR_RELOAD')
        self.log('chkPVRRefresh, brute = %s'%(brute))
        def __toggle(state=True):
            with BUILTIN.busy_dialog(lock=True):
                self.log('chkPVRRefresh, __toggle = %s'%(state))
                self.service.jsonRPC.sendJSON({"method":"Addons.SetAddonEnabled","params":{"addonid":PVR_CLIENT_ID,"enabled":state}})

        if not PROPERTIES.isRunning('Tasks.chkPVRRefresh') and self.service.monitor.isIdle and not PROPERTIES.isRunning('Builder.buildChannels'):
            with PROPERTIES.chkRunning('Tasks.chkPVRRefresh'):
                # v.46: drop the unconditional re-arm in the brute-deferred branch. The
                # previous `else: timerit(PROPERTIES.setPropTimer('chkPVRRefresh'))(M3U_REFRESH)`
                # rescheduled chkPVRRefresh after M3U_REFRESH whenever the brute toggle
                # couldn't run (player playing OR PVR client disabled) — and never gave up.
                # While anything was playing in fullscreen, the loop fired every M3U_REFRESH
                # (15 s); each iteration also set HTTP.pendingRestart=True, which flapped the
                # plugin HTTP server every ~45 s (15 s setEXTProperty delay + 15 s _shutdown
                # wait at server.py:326 + 15 s chkHTTP delay at server.py:333 + 0.1 s spawn
                # at tasks.py:67) for the entire playback session. Net effect in kodi.log:
                # thousands of `HTTP: run, _shutdown/pendingRestart` errors plus matching
                # `StartChannelScan: Provided client id '2' could not be found` warnings,
                # one of each per ~15 s for the duration of any active stream. Fix: drop
                # the re-arm. Genuine refresh triggers (builder.py:396 after a build,
                # context_record.py:58/76 after a recording, manager.py:1030 after channel
                # edits, etc.) still re-fire chkPVRRefresh as needed; if the player has
                # stopped by then, the brute branch runs normally. The HTTP server restart
                # signal still fires once per successful chkPVRRefresh call (timerit
                # setEXTProperty below) but no longer in a self-perpetuating loop.
                timerit(PROPERTIES.setEXTProperty)(M3U_REFRESH,*('%s.HTTP.pendingRestart'%(ADDON_ID),True))
                if brute:
                    if not self.service.player.isPlaying() and BUILTIN.getInfoBool('AddonIsEnabled(%s)'%(PVR_CLIENT_ID),'System'):
                        DIALOG.notificationWait('%s: %s'%(PVR_CLIENT_NAME,LANGUAGE(32125)),wait=M3U_REFRESH, usethread=True)
                        BUILTIN.executewindow('ActivateWindow(home)')
                        __toggle(False), timerit(__toggle)(M3U_REFRESH)
                    # else: brute toggle deferred (player is playing or PVR client disabled).
                    # Previously re-armed via setPropTimer; now relies on the next genuine
                    # refresh trigger to retry.
                self.jsonRPC.PVRScan(self.jsonRPC.getPVRClient(PVR_CLIENT_ID).get('clientid',-1)) #currently not supported by IPTV Simple.
            
            
    def chkSettingsChange(self, settings={}):
        nSettings = SETTINGS.getCurrentSettings()
        for setting, value in list(settings.items()):
            actions = {'User_Folder'  :{'func':self.setUserPath            ,'kwargs':{'old':value,'new':nSettings.get(setting)}},
                       'Debug_Enable' :{'func':self.jsonRPC.toggleShowLog  ,'kwargs':{'state':SETTINGS.getSettingBool('Debug_Enable')}},
                       'TCP_PORT'     :{'func':SETTINGS.setPVRRemote       ,'kwargs':{'host':PROPERTIES.getRemoteHost(),'instance':PROPERTIES.getFriendlyName()}},}
                       
            if nSettings.get(setting) != value and actions.get(setting):
                action = actions.get(setting)
                self.log('chkSettingsChange, detected change in %s: %s => %s\naction = %s'%(setting,value,nSettings.get(setting),action))
                self.service._que(action.get('func'),1,*action.get('args',()),**action.get('kwargs',{}))
        return nSettings


    def chkQUES(self):
        library = Library()
        for i in list(range(QUEUE_CHUNK)):
            if self.service._interrupt() or self.service._suspend():
                self.log("chkQUES, _interrupt/_suspend")
                self.service._que(self.chkQUES,-1)
                break
            else:
                if len(self.service.postQue) > 0:
                    param = self.service.postQue.pop()
                    self.log("chkQUES, queuing = %s\npostQue: %s"%(len(self.service.postQue),param))
                    self.service._que(requestURL,1,*param)
                if len(self.service.jsonQue) > 0:
                    param = self.service.jsonQue.pop()
                    self.log("chkQUES, queuing = %s\njsonQue:%s"%(len(self.service.jsonQue),param))
                    self.service._que(self.jsonRPC.sendJSON,-1,param)
                if len(self.service.logoQue) > 0:
                    chname = FileAccess.loadJSON(self.service.logoQue.pop())
                    self.log("chkQUES, queuing = %s\nlogoQue:%s"%(len(self.service.logoQue),chname))
                    # `chname` is the dict loaded from logoQue (queueLogo stores {'name': ...}).
                    # Nightly upstream referenced `param.get('name')` here, but `param` is only
                    # assigned in the postQue / jsonQue branches above — when the only non-empty
                    # queue is logoQue, that's an UnboundLocalError that aborts chkQUES and stops
                    # logo cache priming, which in turn fails getLogo lookups during channel tune
                    # and the playback chain crashes downstream.
                    self.service._que(library.resources.getLogo,-1,*(chname,library.resources.getCache(chname.get('name')),True))
        del library
        
                
    def setUserPath(self, old, new):
        self.log('setUserPath, old = %s, new = %s'%(old,new))
        dia = DIALOG.progressDialog(message='%s\n%s'%(LANGUAGE(32050),old))
        with PROPERTIES.interruptActivity():
            FileAccess.copyFolder(old, new, dia)
        PROPERTIES.setPendingRestart()
        DIALOG.progressDialog(100, dia)


    def getChannels(self):
        return Channels().getChannels()