  # Copyright (C) 2025 Lunatixz


# This file is part of PseudoTV Live.

# PseudoTV Live is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# PseudoTV Live is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with PseudoTV Live.  If not, see <http://www.gnu.org/licenses/>.
# https://github.com/xbmc/xbmc/blob/master/xbmc/input/actions/ActionIDs.h
# https://github.com/xbmc/xbmc/blob/master/xbmc/input/Key.h

# -*- coding: utf-8 -*-
from globals   import *

# https://github.com/PseudoTV/PseudoTV_Live/issues/68

#todo move autotuning/startup to wizard.

#display welcome
#search discovery
#parse library
#prompt autotune



# if SETTINGS.hasWizardRun():
# hasAutotuned = SETTINGS.hasAutotuned()
        # DIALOG.qrDialog(URL_WIKI,LANGUAGE(32216)%(ADDON_NAME,ADDON_AUTHOR))

class Wizard(xbmcgui.WindowXMLDialog):
    
    def __init__(self, *args, **kwargs):
        self.log('__init__')    
        xbmcgui.WindowXMLDialog.__init__(self, *args, **kwargs)    
        with BUILTIN.busy_dialog():
            self.tasks   = kwargs.get('inherited')
            self.cache   = tasks.cache    
            self.cacheDB = tasks.cacheDB        
            self.jsonRPC = tasks.jsonRPC
            self.player  = tasks.player
            self.monitor = tasks.monitor
            
        self.doModal()
            
        
    def log(self, msg, level=xbmc.LOGDEBUG):
        log('%s: %s'%(self.__class__.__name__,msg),level)
        
        
    def onInit(self):
        self.log('onInit')   
        
        
    def onClose(self):
        # SETTINGS.hasAutotuned()
        self.close()
        
        
    def onAction(self, act):
        actionId = act.getId()   
        self.log('onAction: actionId = %s'%(actionId))
        if (time.time() - self.lastActionTime) < .5 and actionId not in ACTION_PREVIOUS_MENU: action = ACTION_INVALID # during certain times we just want to discard all input
        else:
            if actionId in ACTION_PREVIOUS_MENU:
                self.onClose()
                
            
    def onFocus(self, controlId):
        self.log('onFocus: controlId = %s'%(controlId))

        
    def onClick(self, controlId):
        self.log('onClick: controlId = %s'%(controlId))
        
        