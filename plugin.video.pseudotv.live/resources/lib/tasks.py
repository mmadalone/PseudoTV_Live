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
from autotune   import Autotune
from builder    import Builder
from backup     import Backup
from multiroom  import Multiroom
from wizard     import Wizard
from server     import HTTP

class Tasks():
    cache   = SETTINGS.cache
    cacheDB = SETTINGS.cacheDB
    
    def __init__(self, service):
        self.log('__init__')    
        self.service = service       
        self.jsonRPC = service.jsonRPC
        self.player  = service.player
        self.monitor = service.monitor
        self.http    = HTTP(service=self.service)


    def log(self, msg, level=xbmc.LOGDEBUG):
        return log('%s: %s'%(self.__class__.__name__,msg),level)


    def _initialize(self):
        if SETTINGS.getSettingBool('Enable_Client'):
            self._client()
        else:
            self._host()
        self.log('_initialize, finished...')
   
   
    def _client(self):
        self.service._que(self.chkHTTP)
        self.service._que(self.chkInstanceID)
        self.service._que(self.chkPVRBackend)
        self.service._que(self.chkPVRRefresh)
        self.service._que(self.chkDebugging)
        self.log('_initialize, _client...')
        
        
    def _host(self):
        self._client()
        self.service._que(self.chkDirs)
        self.service._que(self.chkBackup)
        self.service._que(self.chkWizard)
        self.log('_initialize, _host...')
    
   
    def chkHTTP(self):
        timerit(self.http._start)(0.1,[False])
        self.log('chkHTTP')
        
        
    def chkInstanceID(self):
        self.log('chkInstanceID')
        PROPERTIES.getInstanceID()
        

    def chkWizard(self):
        self.log('chkWizard')
        # if not SETTINGS.hasWizardRun():
            # return Wizard(WIZARD_XML, ADDON_PATH, "default", inherited=self)
        

    def chkDebugging(self):
        self.log('chkDebugging')
        if SETTINGS.getSettingBool('Debug_Enable'):
            if DIALOG.yesnoDialog(LANGUAGE(32142),autoclose=4):
                self.log('_chkDebugging, disabling debugging.')
                SETTINGS.setSettingBool('Debug_Enable',False)
                DIALOG.notificationDialog(LANGUAGE(32025))
        if SETTINGS.getSettingBool('Enable_Kodi_Access'):
            self.jsonRPC.toggleShowLog(SETTINGS.getSettingBool('Debug_Enable'))
                    
                    
    def chkBackup(self):
        self.log('chkBackup')
        Backup().hasBackup()


    def chkServers(self):
        self.log('chkServers')
        Multiroom(service=self.service)._chkServers()


    def chkPVRBackend(self): 
        self.log('chkPVRBackend')
        if SETTINGS.hasAddon(PVR_CLIENT_ID,True,True,True,True):
            if not SETTINGS.hasPVRInstance():
                SETTINGS.setPVRRemote(PROPERTIES.getRemoteHost(), SETTINGS.getFriendlyName())


    def chkQueTimer(self):
        # self.service._que(self._chkEpochTimer,-1,*("chkVersion"      ,self.chkVersion      ,21600))
        # self.service._que(self._chkEpochTimer,-1,*("chkKodiSettings" ,self.chkKodiSettings ,3600))
        # self.service._que(self._chkEpochTimer,-1,*("chkServers"      ,self.chkServers      ,300))
        # self.service._que(self._chkEpochTimer,-1,*("chkDiscovery"    ,self.chkDiscovery    ,300))
        # self.service._que(self._chkEpochTimer,-1,*("chkChannels"     ,self.chkChannels     ,3600))
        
        # self.service._que(self._chkEpochTimer,-1,*("chkFiles"        ,self.chkFiles        ,300))
        # self.service._que(self._chkEpochTimer,-1,*("chkURLQUE"       ,self.chkURLQUE       ,300))
        # self.service._que(self._chkEpochTimer,-1,*("chkJSONQUE"      ,self.chkJSONQUE      ,300))
        # self.service._que(self._chkEpochTimer,-1,*("chkLOGOQUE"      ,self.chkLOGOQUE      ,600))
        
        # self.service._que(self._chkPropTimer ,-1,*("chkPVRRefresh"   ,self.chkPVRRefresh   ,1))
        # self.service._que(self._chkPropTimer ,-1,*("chkChannelUpdate",self.chkChannelUpdate,3))
        
        self._chkEpochTimer('chkVersion'      , self.chkVersion       , 21600)
        self._chkEpochTimer('chkKodiSettings' , self.chkKodiSettings  , 3600)
        self._chkEpochTimer('chkServers'      , self.chkServers       , 300)
        self._chkEpochTimer('chkDiscovery'    , self.chkDiscovery     , 300)
        self._chkEpochTimer('chkChannels'     , self.chkChannels      , 3600)
        
        self._chkEpochTimer('chkFiles'        , self.chkFiles         , 300)
        self._chkEpochTimer('chkURLQUE'       , self.chkURLQUE        , 300)
        self._chkEpochTimer('chkJSONQUE'      , self.chkJSONQUE       , 300)
        self._chkEpochTimer('chkLOGOQUE'      , self.chkLOGOQUE       , 600)

        self._chkPropTimer('chkPVRRefresh'    , self.chkPVRRefresh    , 1)
        self._chkPropTimer('chkChannelUpdate' , self.chkChannelUpdate , 3)
        
        
    def _chkEpochTimer(self, key, func, runevery=900, priority=-1, nextrun=None, *args, **kwargs):
        if nextrun is None: nextrun = PROPERTIES.getPropertyInt(key, default=0) # nextrun == 0 => force que
        epoch = int(time.time())
        if epoch >= nextrun:
            self.log('_chkEpochTimer, key = %s, last run %s' % (key, epoch - nextrun))
            PROPERTIES.setPropertyInt(key, (epoch + runevery))
            self.service._que(func, priority, *args, **kwargs)


    def _chkPropTimer(self, key, func, priority=-1, *args, **kwargs):
        key = '%s.%s' % (ADDON_ID, key)
        if PROPERTIES.getEXTPropertyBool(key):
            self.log('_chkPropTimer, key = %s'%(key))
            PROPERTIES.clrEXTProperty(key)
            self.service._que(func, priority, *args, **kwargs)
            

    @cacheit(expiration=datetime.timedelta(minutes=FIFTEEN))
    def getOnlineVersion(self):
        try:    ONLINE_VERSION = re.compile('" version="(.+?)" name="%s"'%(ADDON_NAME)).findall(str(requestURL(ADDON_URL)))[0]
        except: ONLINE_VERSION = ADDON_VERSION
        self.log('getOnlineVersion, version = %s'%(ONLINE_VERSION))
        return ONLINE_VERSION
        
        
    def chkVersion(self):
        update = False
        ONLINE_VERSION = self.getOnlineVersion()
        if ADDON_VERSION < ONLINE_VERSION: 
            update = True
            DIALOG.notificationDialog(LANGUAGE(30073)%(ONLINE_VERSION))
        elif ADDON_VERSION > (SETTINGS.getCacheSetting('lastVersion', checksum=ADDON_VERSION) or '0.0.0'):
            SETTINGS.setCacheSetting('lastVersion',ADDON_VERSION, checksum=ADDON_VERSION)
            BUILTIN.executescript('special://home/addons/%s/resources/lib/utilities.py, Show_Changelog'%(ADDON_ID))
        self.log('chkVersion, update = %s, installed version = %s, online version = %s'%(update,ADDON_VERSION,ONLINE_VERSION))
        SETTINGS.setSetting('Update_Status',{'True':'[COLOR=yellow]%s [B]v.%s[/B][/COLOR]'%(LANGUAGE(32168),ONLINE_VERSION),'False':'None'}[str(update)])


    def chkKodiSettings(self):
        self.log('chkKodiSettings')
        MIN_GUIDEDAYS = SETTINGS.setSettingInt('Min_Days' ,self.jsonRPC.getSettingValue('epg.pastdaystodisplay'     ,default=1))
        MAX_GUIDEDAYS = SETTINGS.setSettingInt('Max_Days' ,self.jsonRPC.getSettingValue('epg.futuredaystodisplay'   ,default=3))
        OSD_TIMER     = SETTINGS.setSettingInt('OSD_Timer',self.jsonRPC.getSettingValue('pvrmenu.displaychannelinfo',default=5))
         

    def chkDirs(self):
        self.log('chkDirs')
        [FileAccess.makedirs(folder) for folder in [LOGO_LOC,FILLER_LOC,TEMP_LOC] if not FileAccess.exists(os.path.join(folder,''))]


    def chkFiles(self):
        self.log('chkFiles')
        self.chkDirs()
        if FileAccess.exists(CHANNELFLEPATH) and not (FileAccess.exists(M3UFLEPATH) & FileAccess.exists(XMLTVFLEPATH) & FileAccess.exists(GENREFLEPATH)): self.service._que(self.chkChannels,3)


    def chkDiscovery(self):
        self.log('chkDiscovery')
        Multiroom(service=self.service)._chkDiscovery()
        

    def chkChannelUpdate(self):
        ids = PROPERTIES.getUpdateChannels()
        if ids:
            channels = self.getVerifiedChannels()
            channels = [citem for id in ids for citem in channels if citem.get('id') == id]
            self.log('chkChannelUpdate, channels = %s\nid = %s'%(len(channels),ids))
            self.service._que(self.chkChannels,3,channels)


    def chkChannels(self, channels: list=[], save=False):
        builder            = Builder(service=self.service)
        hasAutotuned       = SETTINGS.hasAutotuned()
        hasEnabledServers  = PROPERTIES.hasEnabledServers()
        buildFillerFolders = SETTINGS.getSettingBool('Build_Filler_Folders')
        self.log('chkChannels, hasAutotuned = %s, hasEnabledServers = %s, buildFillerFolders = %s'%(hasAutotuned,hasEnabledServers,buildFillerFolders))
        
        if not channels:
            save     = True
            channels = builder.getVerifiedChannels()
            SETTINGS.setSetting('Select_Channels','[B]%s[/B] Channels'%(len(channels)))
            PROPERTIES.setChannels(len(channels) > 0)
            
        if len(channels) > 0:
            complete, refresh = builder.build(channels)
            self.log('chkChannels, channels = %s, complete = %s, refresh = %s'%(len(channels),complete,refresh))
            if complete:
                if save: builder.channels.setChannels(channels)
                if refresh: PROPERTIES.setPropTimer('chkPVRRefresh')
                # if buildFillerFolders: self.service._que(self.chkFillers,2,channels)#todo repair
            else: self.service._que(self.chkChannels,3,channels)
        elif not hasAutotuned:  DIALOG.notificationDialog(LANGUAGE(32181))
        elif hasEnabledServers: PROPERTIES.setPropTimer('chkPVRRefresh')
        else:                   DIALOG.notificationDialog(LANGUAGE(32058))
        del builder


    def chkLOGOQUE(self):
        if not PROPERTIES.isRunning('chkLOGOQUE') and PROPERTIES.hasFirstRun():
            with PROPERTIES.chkRunning('chkLOGOQUE'):
                updated   = False
                resources = Library(service=self.service).resources
                queuePool = (SETTINGS.getCacheSetting('queueLOGO', json_data=True) or {})
                params    = randomShuffle(queuePool.get('params',[]))
                for i in list(range(QUEUE_CHUNK)):
                    if self.service._interrupt(): 
                        self.log("chkLOGOQUE, _interrupt")
                        break
                    elif len(params) > 0:
                        param   = params.pop(0)
                        updated = True
                        self.log("chkLOGOQUE, queuing = %s\n%s"%(len(params),param))
                        if param.get('name','').startswith('getLogoResources'):
                            self.service._que(resources.getLogoResources, 10+i, *param.get('args',()), **param.get('kwargs',{}))
                        elif param.get('name','').startswith('getTVShowLogo'):
                            self.service._que(resources.getTVShowLogo, 10+i, *param.get('args',()), **param.get('kwargs',{}))
                queuePool['params'] = setDictLST(params)
                if updated and len(queuePool['params']) == 0: PROPERTIES.setPropertyBool('ForceLibrary',True)
                self.log('chkLOGOQUE, remaining = %s'%(len(queuePool['params'])))
                SETTINGS.setCacheSetting('queueLOGO', queuePool, json_data=True)
                del resources


    def chkJSONQUE(self):
        if not PROPERTIES.isRunning('chkJSONQUE') and PROPERTIES.hasFirstRun():
            with PROPERTIES.chkRunning('chkJSONQUE'):
                queuePool = (SETTINGS.getCacheSetting('queueJSON', json_data=True) or {})
                params = queuePool.get('params',[])
                for i in list(range(QUEUE_CHUNK)):
                    if self.service._interrupt(): 
                        self.log("chkJSONQUE, _interrupt")
                        break
                    elif len(params) > 0:
                        param = params.pop(0)
                        self.log("chkJSONQUE, queuing = %s\n%s"%(len(params),param))
                        self.service._que(self.jsonRPC.sendJSON,5+i, param)
                queuePool['params'] = setDictLST(params)
                self.log('chkJSONQUE, remaining = %s'%(len(queuePool['params'])))
                SETTINGS.setCacheSetting('queueJSON', queuePool, json_data=True)


    def chkURLQUE(self):
        if not PROPERTIES.isRunning('chkURLQUE') and PROPERTIES.hasFirstRun():
            with PROPERTIES.chkRunning('chkURLQUE'):
                queuePool = (SETTINGS.getCacheSetting('queueURL', json_data=True) or {})
                params = queuePool.get('params',[])
                for i in list(range(QUEUE_CHUNK)):
                    if self.service._interrupt(): 
                        self.log("chkURLQUE, _interrupt")
                        break
                    elif len(params) > 0:
                        param = params.pop(0)
                        self.log("chkURLQUE, queuing = %s\n%s"%(len(params),param))
                        self.service._que(requestURL,1, param)
                queuePool['params'] = setDictLST(params)
                self.log('chkURLQUE, remaining = %s'%(len(queuePool['params'])))
                SETTINGS.setCacheSetting('queueURL', queuePool, json_data=True)


    def chkPVRRefresh(self, wait=FIFTEEN, brute=SETTINGS.getSettingBool('Enable_PVR_RELOAD')):
        self.log('chkPVRRefresh')
        def __toggle(state):
            self.log('chkPVRRefresh, __toggle = %s'%(state))
            self.service.jsonRPC.sendJSON({"method":"Addons.SetAddonEnabled","params":{"addonid":PVR_CLIENT_ID,"enabled":state}})
            
        if not PROPERTIES.isRunning('chkPVRRefresh'):
            with PROPERTIES.chkRunning('chkPVRRefresh'), PROPERTIES.suspendActivity():
                self.service._que(self.http._restart,1)
                if brute:
                    if not self.monitor.isIdle and not BUILTIN.isPlaying() and BUILTIN.getInfoBool('AddonIsEnabled(%s)'%(PVR_CLIENT_ID),'System'):
                        with BUILTIN.busy_dialog(lock=True):
                            BUILTIN.executebuiltin("Dialog.Close(all)")
                            DIALOG.notificationWait('%s: %s'%(PVR_CLIENT_NAME,LANGUAGE(32125)),wait=wait, usethread=True)
                            __toggle(False), self.monitor.waitForAbort(wait), __toggle(True)
                    else: self.service._que(self.chkPVRRefresh)
            
            
    def chkSettingsChange(self, settings={}):
        with PROPERTIES.interruptActivity():
            nSettings = SETTINGS.getCurrentSettings()
            print(settings,type(settings),nSettings,type(nSettings))
            for setting, value in list(settings.items()):
                actions = {'User_Folder'  :{'func':self.setUserPath            ,'kwargs':{'old':value,'new':nSettings.get(setting)}},
                           'Debug_Enable' :{'func':self.jsonRPC.toggleShowLog  ,'kwargs':{'state':SETTINGS.getSettingBool('Debug_Enable')}},
                           'TCP_PORT'     :{'func':SETTINGS.setPVRRemote       ,'kwargs':{'host':PROPERTIES.getRemoteHost(),'instance':SETTINGS.getFriendlyName()}},}
                           
                if nSettings.get(setting) != value and actions.get(setting):
                    action = actions.get(setting)
                    self.log('chkSettingsChange, detected change in %s: %s => %s\naction = %s'%(setting,value,nSettings.get(setting),action))
                    self.service._que(action.get('func'),1,*action.get('args',()),**action.get('kwargs',{}))
            return nSettings


    def setUserPath(self, old, new):
        with PROPERTIES.interruptActivity():
            self.log('setUserPath, old = %s, new = %s'%(old,new))
            dia = DIALOG.progressDialog(message='%s\n%s'%(LANGUAGE(32050),old))
            FileAccess.copyFolder(old, new, dia)
            PROPERTIES.setPendingRestart()
            DIALOG.progressDialog(100, dia)
            
            
    def getVerifiedChannels(self):
        return Builder(service=self.service).getVerifiedChannels()