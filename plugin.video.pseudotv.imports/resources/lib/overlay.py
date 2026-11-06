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
from resources import Resources
import restart_helpers # madteevee (imports fork, imports.62): Restart pop-up close rule
import onnext_helpers  # madteevee (imports fork, imports.64): Up next drawn on the video

WH, WIN = BUILTIN.getResolution()
# madteevee (imports fork, imports.64): Home window property set while Up next shows (onnext.xml's
# onload/onunload set it; nothing here reads it, kept for skins).
ONNEXT_RUNNING = '%s.ONNEXT_OVERLAY.Running'%(ADDON_ID)

class Busy(xbmcgui.WindowXMLDialog):
    def __init__(self, *args, **kwargs):
        xbmcgui.WindowXMLDialog.__init__(self, *args, **kwargs)
        self.isLocked = kwargs.get('isLocked',False)
                
                
    def onInit(self):
        log('Busy: onInit, isLocked = %s'%(self.isLocked))
        spinner = self.getControl(41)
        spinner.setColorDiffuse({True:"0xC0FF0000",False:"0xFF01416b"}[self.isLocked])


    def onAction(self, act):
        actionId = act.getId()
        log('Busy: onAction, actionId = %s, isLocked = %s'%(actionId,self.isLocked))
        if not self.isLocked and actionId in ACTION_PREVIOUS_MENU: self.close()
        

class Background(xbmcgui.WindowXMLDialog):
    def __init__(self, *args, **kwargs):
        xbmcgui.WindowXMLDialog.__init__(self, *args, **kwargs)
        self.player = kwargs.get('player', None)
        self.citem  = self.player.playingItem.get('citem',{})
        self.fitem  = self.player.playingItem.get('fitem',{})
        self.nitem  = self.player.playingItem.get('nitem',{})
      
    def log(self, msg, level=xbmc.LOGDEBUG):
        return log('%s: %s'%(self.__class__.__name__,msg),level)


    def onInit(self):
        try:
            self.log('onInit, citem = %s\nfitem = %ss\nnitem = %s'%(self.citem,self.fitem,self.nitem))
            logo      = (self.citem.get('logo')        or BUILTIN.getInfoLabel('Art(icon)','Player') or LOGO)
            chname    = (self.citem.get('name')        or BUILTIN.getInfoLabel('ChannelName','VideoPlayer'))
            nowTitle  = (self.fitem.get('label')       or BUILTIN.getInfoLabel('Title','VideoPlayer'))
            nextTitle = (self.nitem.get('showlabel')   or BUILTIN.getInfoLabel('NextTitle','VideoPlayer') or chname)

            # madteevee (imports fork, imports.64): Kodi's clock format (13:30 here, was '01:30PM') and
            # the Kodi label when the guide has no start (it was never reached); shared with Up next.
            nextTime = onnext_helpers.next_time(self.nitem.get('start'), onnext_helpers.time_format(xbmc.getRegion('time')), BUILTIN.getInfoLabel('NextStartTime','VideoPlayer'))
            if not nextTime: return
        
            window_w, window_h = WH # window_h, window_w = (self.getHeight(), self.getWidth())
            onNextX, onNextY = abs(int(window_w // 9)), abs(int(window_h // 16) - window_h) - 356 #auto
            
            self.getControl(40001).setPosition(onNextX, onNextY)
            self.getControl(40001).setVisibleCondition('[Player.Playing + !Window.IsVisible(fullscreeninfo) + Window.IsVisible(fullscreenvideo)]')
            self.getControl(40001).setAnimations([('WindowOpen' , 'effect=zoom start=80 end=100 center=%s,%s delay=160 tween=back time=240 reversible=false'%(onNextX, onNextY)),
                                                  ('WindowOpen' , 'effect=fade start=0 end=100 delay=160 time=240 reversible=false'),
                                                  ('WindowClose', 'effect=zoom start=100 end=80 center=%s,%s delay=160 tween=back time=240 reversible=false'%(onNextX, onNextY)),
                                                  ('WindowClose', 'effect=fade start=100 end=0 time=240 reversible=false'),
                                                  ('Visible'    , 'effect=zoom start=80 end=100 center=%s,%s delay=160 tween=back time=240 reversible=false'%(onNextX, onNextY)),
                                                  ('Visible'    , 'effect=fade end=100 time=240 reversible=false')])
            self.getControl(40002).setImage(LOGO_COLOR if logo.endswith('wlogo.png') else logo)
            self.getControl(40003).setText(onnext_helpers.card_text(LANGUAGE(32104),LANGUAGE(32116),nowTitle,chname,nextTime,nextTitle))
            self.getControl(40004).setImage(Globals._getThumb(self.nitem))
        except Exception as e:
            self.log("onInit, failed! %s"%(e), xbmc.LOGERROR)
            self.close()

class Replay(xbmcgui.WindowXMLDialog):
    # madteevee (imports fork, imports.62): a passive view. Player.toggleReplay decides
    # when to offer it (restart_helpers), opens it with doModal() and, once it has
    # closed, reads self.restart; the restart itself is Player.requestRestart, the one
    # action shared with the remote button and the guide entry.
    # Before: the constructor opened it, onInit used a never-set self.service (so it
    # closed the moment it opened), only Custom playback kept it open, and OK stopped
    # playback to replay the programme's catch-up link.
    def __init__(self, *args, **kwargs):
        xbmcgui.WindowXMLDialog.__init__(self, *args, **kwargs)
        self.player  = kwargs['player']
        self.service = self.player.service
        self.monitor = self.service.monitor
        self.epoch   = kwargs.get('epoch')   # Player._play_epoch it was offered for
        self.end     = kwargs.get('end')     # LiveTV: the show's end (epoch s), else None
        self.restart = False
        self.closing = False


    def log(self, msg, level=xbmc.LOGDEBUG):
        return log('%s: %s'%(self.__class__.__name__,msg),level)


    def onInit(self):
        self.log("onInit")
        try:
            prog    = 0
            tot     = float(OSD_TIMER*2)
            started = time.time()
            control = self.getControl(40000)
            control.setVisibleCondition('[Player.Playing + !Window.IsVisible(fullscreeninfo) + Window.IsVisible(fullscreenvideo)]')
            xpos = control.getX()
            # One bounded countdown. It also ends when playback changes (zap, programme
            # end, stop) or the LiveTV show ends. _shutdown waits through
            # Monitor.waitForAbort, which lets Kodi run onAction meanwhile.
            while not self.service._shutdown(restart_helpers.POPUP_POLL) and not self.closing:
                now = time.time()
                if now - started >= tot or restart_helpers.popup_done(self.epoch, self.player._play_epoch, now, self.end): break
                step = int(((now - started)*100)//tot)
                if step > 0 and step != prog:
                    prog = step
                    control.setAnimations([('Conditional', 'effect=zoom start=%s,100 end=%s,100 time=1000 center=%s,100 condition=True'%((prog-20),(prog),xpos))])
            control.setAnimations([('Conditional', 'effect=fade start=%s end=0 time=240 delay=0.240 condition=True'%(prog))])
            control.setVisible(False)
        except Exception as e: self.log("onInit, failed! %s"%(e), xbmc.LOGERROR)
        self.log("onInit, closing, restart = %s"%(self.restart))
        self.close()


    def onAction(self, act):
        # Any key closes the pop-up; OK on the button asks for the restart. Keys that
        # mean something to the player (restart_helpers.POPUP_PASS_ON) are passed on after
        # the 240 ms close animation (Kodi drops actions while a modal dialog animates
        # closed). Back only closes it (Python windows consume Back). The old
        # AlarmClock(...,.5,...) pass-on fired at once: AlarmClock reads '.5' as 0 minutes.
        actionId = act.getId()
        self.log('onAction: actionId = %s'%(actionId))
        self.closing = True
        if actionId in ACTION_SELECT_ITEM:
            if self.getFocusId() == 40001: self.restart = True
        elif actionId in (ACTION_PAGE_UP, ACTION_PAGE_DOWN):
            # channel +/- reach a dialog as PageUp/PageDown (keymap global section)
            if BUILTIN.getInfoBool('IsPlayingTv','Pvr'):
                timerit(BUILTIN.executebuiltin)(0.5,'Action(ChannelUp)' if actionId == ACTION_PAGE_UP else 'Action(ChannelDown)')
        elif actionId in restart_helpers.POPUP_PASS_ON:
            timerit(BUILTIN.executebuiltin)(0.5,'Action(%s)'%(restart_helpers.POPUP_PASS_ON[actionId]))


    def onClose(self):
        self.log("onClose")
        self.closing = True
        return None


class Overlay(object):
    channelBug = None
    vignette   = None
    # imports.18: cntrlManager moved to __init__ (instance attr). Class-level
    # mutable defaults are a Python footgun — if two Overlay instances ever
    # co-existed (which they used to, during the toggleOverlay race fixed by
    # services.py:_overlay_lock), they shared the same dict. With the lock
    # the race is closed at the Player layer, but this is defense in depth:
    # any future code path that constructs Overlay directly should still get
    # an instance-local control tracker.

    def __init__(self, *args, **kwargs):
        self.player     = kwargs.get('player', None)
        self.service    = self.player.service
        self.jsonRPC    = self.player.jsonRPC
        self.runActions = self.player.runActions
        self.citem      = self.player.playingItem.get('citem',{})
        self.fitem      = self.player.playingItem.get('fitem',{})
        self.nitem      = self.player.playingItem.get('nitem',{})
        self.resources  = Resources(service=self.service)
        self.cntrlManager = {}
        # madteevee (imports fork, imports.64): Up next is drawn here, on the video window, as
        # controls that never take focus (it was a modal dialog that took every key). Its own
        # lock: the idle thread, the wait thread and close() (run under services'
        # _overlay_lock, which the wait thread never takes) share this state.
        self._onNextLock     = Lock()
        self._onNextControls = {}    # 'frame' / 'art' / 'shadow' / 'text' -> control, made in open() (else on first use)
        self._onNextShown    = False # shown (or tried) in this pass; rearmOnNext clears it
        self._onNextVisible  = False
        self._onNextToken    = 0     # moves at every show and hide: a wait thread exits when it moves
        self._onNextClosed   = False
        
        self.window     = xbmcgui.Window(12005) 
        self.window_w, self.window_h = WH
        
        #vignette rules
        self.enableVignette = False
        self.defaultView    = self.jsonRPC.getViewMode()
        self.vinView        = self.defaultView
        self.vinImage       = ''
        
        #channelBug rules
        self.enableChannelBug = SETTINGS.getSettingBool('Enable_ChannelBug')
        self.forceBugDiffuse  = SETTINGS.getSettingBool('Force_Diffuse')
        self.channelBugColor  = '0x%s'%((SETTINGS.getSetting('ChannelBug_Color') or 'FFFFFFFF'))
        self.channelBugFade   = SETTINGS.getSettingInt('ChannelBug_Transparency')
        
        try:    self.channelBugX, self.channelBugY = literal_eval(SETTINGS.getSetting("Channel_Bug_Position_XY")) #user
        except Exception: self.channelBugX, self.channelBugY = abs(int(self.window_w // 9) - self.window_w) - 128, abs(int(self.window_h // 16) - self.window_h) - 128 #auto        

        
        
    def log(self, msg, level=xbmc.LOGDEBUG):
        return log('%s: %s'%(self.__class__.__name__,msg),level)


    def _hasControl(self, control):
        return control in self.cntrlManager
        

    def _isVisible(self, control):
        return self.cntrlManager.get(control,False)


    def _setVisible(self, control, state: bool=False):
        self.log('_setVisible, %s = %s'%(control,state))
        try:
            control.setVisible(state)
            return state
        except Exception as e: 
            self.log('_setVisible, failed! control = %s %s'%(control,e))
            self._delControl(control)
            return False


    def _addControl(self, control):
        if not self._hasControl(control):
            try: 
                self.log('_addControl, %s'%(control))
                self.window.addControl(control)
                self.cntrlManager[control] = self._setVisible(control,True)
            except Exception as e: 
                self.log('_addControl failed! control = %s %s'%(control, e), xbmc.LOGERROR)
                self._delControl(control)
        
        
    def _delControl(self, control):
        if self._hasControl(control):
            self.log('_delControl, %s'%(control))
            try: self.window.removeControl(control)
            except Exception as e: self.log('_delControl failed! control = %s %s'%(control, e), xbmc.LOGERROR)
            self.cntrlManager.pop(control)  
            
            
    def open(self):
        if not PROPERTIES.isRunning('Overlay'):
            PROPERTIES.setRunning('Overlay',True)
            if self.citem:
                self.runActions(RULES_ACTION_OVERLAY_OPEN, self.citem, inherited=self)
                self.log("open, enableVignette = %s, enableChannelBug = %s"%(self.enableVignette, self.enableChannelBug))
                if self.enableVignette:
                    window_h, window_w = (self.window.getHeight(), self.window.getWidth())
                    self.vignette = xbmcgui.ControlImage(0, 0, window_w, window_h, ' ', aspectRatio=0)
                    self._addControl(self.vignette)
                    self.vignette.setImage(self.vinImage)
                    if self.vinView != self.defaultView: timerit(self.jsonRPC.setViewMode)(0.5,[self.vinView])
                    self.vignette.setAnimations([('Conditional', 'effect=fade start=0 end=100 time=240 delay=160 condition=True reversible=True')])
                    self.log('enableVignette, vinImage = %s, vinView = %s'%(self.vinImage,self.vinView))
                
                if self.enableChannelBug:
                    self.channelBug = xbmcgui.ControlImage(self.channelBugX, self.channelBugY, 128, 128, ' ', aspectRatio=2)
                    self._addControl(self.channelBug)
                    
                    logo = self.citem.get('logo',(BUILTIN.getInfoLabel('Art(icon)','Player') or LOGO))
                    if   self.forceBugDiffuse:        self.channelBug.setColorDiffuse(self.channelBugColor)
                    elif self.resources.isMono(logo): self.channelBug.setColorDiffuse(self.channelBugColor)
                    self.channelBug.setImage(logo)
                    self.channelBug.setAnimations([('Conditional', 'effect=fade start=0 end=100 time=2000 delay=1000 condition=Control.IsVisible(%i) reversible=false'%(self.channelBug.getId())),
                                                   ('Conditional', 'effect=fade start=100 end=%i time=1000 delay=3000 condition=Control.IsVisible(%i) reversible=false'%(self.channelBugFade,self.channelBug.getId()))])
                    self.log('enableChannelBug, logo = %s, channelBugColor = %s, window = (%s,%s)'%(logo,self.channelBugColor,self.window_h, self.window_w))
                self._prepareOnNext()
            else: self.close()


    def _prepareOnNext(self):
        # madteevee (imports fork, imports.64): Up next's controls are made now, hidden (on top of the
        # logo), so Kodi has drawn them hidden long before the first show: it plays the 'Visible'
        # zoom and fade only on a hidden->shown change of the condition it has seen (GUIControl.cpp
        # UpdateVisibility), and a new control starts out as 'shown'.
        if self.player.OnNextMode not in (1,2): return
        try:
            with self._onNextLock:
                if self._onNextClosed or self._onNextVisible: return
                xbmcgui.Window(10000).setProperty(ONNEXT_RUNNING,'false')
                self._onNextCreate(onnext_helpers.NAMES)
        except Exception as e: self.log('_prepareOnNext, failed! %s'%(e), xbmc.LOGERROR)
        
        
    def toggleOnNext(self, state: bool=bool(SETTINGS.getSettingInt('OnNext_Mode'))):
        # madteevee (imports fork, imports.64): once per pass through the end-of-show window
        # (services.__chkOnNext calls it every idle tick in the window; rearmOnNext starts a new
        # pass). Returns at once: a wait thread hides the card. Mode 3 asks for the info screen
        # explicitly (toggleInfo's default is the Enable_OnInfo setting, so it did nothing with
        # that setting off). Before: an OnNext dialog held the idle thread and every key for
        # minutes, once per Overlay.
        if not state: return self._hideOnNext('off')
        with self._onNextLock:
            if self._onNextShown or self._onNextClosed: return
            self._onNextShown = True # also when nothing ends up shown: getNextItem once per pass
        mode = self.player.OnNextMode
        self.log('toggleOnNext, mode = %s'%(mode))
        try: # runs on the service's idle thread: never let an error stop it
            if mode == 3: self.player.toggleInfo(True)
            elif mode in (1,2,4):
                item  = self.player.playingItem # read now: this Overlay can outlive a programme (same channel)
                nitem = self.jsonRPC.getNextItem(item.get('citem',{}),item.get('nitem',{}))
                if mode == 4: self._updateUpNext(item.get('fitem',{}),nitem)
                else:         self._showOnNext(mode,item,nitem)
        except Exception as e: self.log('toggleOnNext, failed! %s'%(e), xbmc.LOGERROR)


    def rearmOnNext(self):
        # madteevee (imports fork, imports.64): before the window (a new show, or the same show
        # restarted from the beginning): the next pass shows Up next again. A card still up goes now:
        # it is only shown inside the window, so the show was restarted or sought back (a remote's
        # JSON-RPC restart moves neither Kodi's idle time nor the playback epoch).
        with self._onNextLock:
            hidden = self._onNextHide()
            if self._onNextShown:
                self._onNextShown = False
                self.log('rearmOnNext')
        if hidden: self.log('toggleOnNext, hidden, back before the window')


    def _onNextText(self, item, nitem):
        citem, fitem = item.get('citem',{}), item.get('fitem',{})
        chname    = (citem.get('name')      or BUILTIN.getInfoLabel('ChannelName','VideoPlayer'))
        nowTitle  = (fitem.get('label')     or BUILTIN.getInfoLabel('Title','VideoPlayer'))
        nextTitle = (nitem.get('showlabel') or BUILTIN.getInfoLabel('NextTitle','VideoPlayer') or chname)
        nextTime  = onnext_helpers.next_time(nitem.get('start'), onnext_helpers.time_format(xbmc.getRegion('time')), BUILTIN.getInfoLabel('NextStartTime','VideoPlayer'))
        if not nextTime: return ''
        return onnext_helpers.card_text(LANGUAGE(32104), LANGUAGE(32116), nowTitle, chname, nextTime, nextTitle)


    def _onNextCreate(self, names):
        # Created once with blank content; the visible condition keeps them hidden until
        # ONNEXT_RUNNING is 'true'. Python controls on Window(12005) live in the skin's
        # window, so textures are absolute paths and positions (1080 grid, like the Move
        # tool's preview) are moved to the skin's grid. Returns True when it made any.
        x, y = onnext_helpers.position(self.player.onNextPosition, self.window_w, self.window_h)
        try:    fx, fy = onnext_helpers.grid(self.window.getWidth(), self.window.getHeight())
        except Exception: fx, fy = 1.0, 1.0
        rect = onnext_helpers.layout(x, y, fx, fy)
        made = False
        for name in names:
            if name in self._onNextControls: continue
            # the frame's middle is see-through; the art fills it (scale) like the old border hugged it
            if   name == 'frame': control = xbmcgui.ControlImage(*rect[name], ' ', aspectRatio=onnext_helpers.FRAME_ASPECT)
            elif name == 'art':   control = xbmcgui.ControlImage(*rect[name], ' ', aspectRatio=onnext_helpers.ART_ASPECT)
            elif name == 'shadow': control = xbmcgui.ControlTextBox(*rect[name], onnext_helpers.TEXT_FONT, onnext_helpers.SHADOW_COLOR)
            else:                  control = xbmcgui.ControlTextBox(*rect[name], onnext_helpers.TEXT_FONT, onnext_helpers.TEXT_COLOR)
            self._addControl(control)
            if not self._hasControl(control): continue
            control.setVisibleCondition(onnext_helpers.condition(ONNEXT_RUNNING))
            control.setAnimations(onnext_helpers.animations(*rect['frame'][:2]))
            if name == 'frame':
                control.setImage(os.path.join(MEDIA_LOC,onnext_helpers.FRAME_IMAGE))
                control.setColorDiffuse(onnext_helpers.FRAME_COLOR)
            self._onNextControls[name] = control
            made = True
        return made


    def _showOnNext(self, mode, item, nitem):
        text = self._onNextText(item, nitem)
        if not text: return self.log('toggleOnNext, no time for the next programme, not shown')
        names = onnext_helpers.NAMES if mode == 2 else onnext_helpers.NAMES[2:] # z-order
        with self._onNextLock:
            if self._onNextClosed or self._onNextVisible: return
            xbmcgui.Window(10000).setProperty(ONNEXT_RUNNING,'false')
            made = self._onNextCreate(names)
        # Controls made just now (a mode change, or open() could not): let Kodi draw them hidden
        # first, else they pop in without the zoom and fade (see _prepareOnNext).
        if made and self.service._shutdown(onnext_helpers.SETTLE_WAIT): return
        with self._onNextLock:
            if self._onNextClosed or self._onNextVisible: return # closed while it waited
            for name, control in list(self._onNextControls.items()):
                if name not in names: self._setVisible(control, False) # mode 1 after mode 2 (a settings change)
                elif name == 'art':   control.setImage((Globals._getThumb(nitem) or FANART_COLOR))
                elif name in ('shadow','text'): control.setText(text)
            for name in names:
                if name in self._onNextControls: self._setVisible(self._onNextControls[name], True)
            if not self._onNextControls: return self.log('toggleOnNext, no controls, not shown', xbmc.LOGERROR)
            token  = self._onNextToken + 1
            thread = Thread(target=self._waitOnNext, args=(token, self.player._play_epoch, BUILTIN.getIdle()), name='Overlay.waitOnNext', daemon=True)
            self._onNextToken, self._onNextVisible = token, True
            try: thread.start() # before the card shows: never a card with no thread to hide it
            except Exception as e:
                self._onNextToken  += 1
                self._onNextVisible = False
                return self.log('toggleOnNext, wait thread failed, not shown! %s'%(e), xbmc.LOGERROR)
            xbmcgui.Window(10000).setProperty(ONNEXT_RUNNING,'true')
        self.log('toggleOnNext, shown, mode = %s'%(mode))
        if mode == 2: xbmc.playSFX(BING_WAV)


    def _waitOnNext(self, token, epoch, idle):
        # Up to 30 s, checked every 0.25 s; onnext_helpers.hide_reason decides. A key press
        # lowers Kodi's idle seconds; the key itself goes to Kodi as usual.
        started, reason = time.monotonic(), ''
        try:
            while not reason:
                stopping = self.service._shutdown(onnext_helpers.WAIT_STEP)
                idleNow  = BUILTIN.getIdle()
                with self._onNextLock: closed = self._onNextClosed or self._onNextToken != token
                reason = onnext_helpers.hide_reason(time.monotonic() - started, ONNEXT_TIMER*2, idleNow, idle,
                                                    self.player._play_epoch, epoch, closed, self.player.isPlayingPseudoTV(), stopping)
                idle = idleNow
        except Exception as e:
            reason = 'failed! %s'%(e)
            self.log('_waitOnNext, failed! %s'%(e), xbmc.LOGERROR)
        self._hideOnNext(reason, token)


    def _hideOnNext(self, reason='', token=None):
        with self._onNextLock:
            if token is not None and token != self._onNextToken: return
            if not self._onNextHide(): return
        self.log('toggleOnNext, hidden, %s'%(reason))


    def _onNextHide(self):
        # Called with _onNextLock held. True when the card was up: the token moves (its wait thread
        # ends quietly) and the property goes 'false' (the controls fade out, 240 ms).
        if not self._onNextVisible: return False
        self._onNextToken  += 1
        self._onNextVisible = False
        try: xbmcgui.Window(10000).setProperty(ONNEXT_RUNNING,'false')
        except Exception as e: self.log('_hideOnNext, failed! %s'%(e), xbmc.LOGERROR)
        return True


    def _updateUpNext(self, nowItem: dict={}, nextItem: dict={}):
        self.log('_updateUpNext')
        data = {}
        try:# https://github.com/im85288/service.upnext/wiki/Example-source-code
            data.update({"notification_offset":int(floor(self.player.getRemainingTime())) + OSD_TIMER})
            current_episode = {"current_episode":{"episodeid" :(nowItem.get("id")            or ""),
                                                  "tvshowid"  :(nowItem.get("tvshowid")      or ""),
                                                  "title"     :(nowItem.get("title")         or ""),
                                                  "art"       :(nowItem.get("art")           or ""),
                                                  "season"    :(nowItem.get("season")        or ""),
                                                  "episode"   :(nowItem.get("episode")       or ""),
                                                  "showtitle" :(nowItem.get("tvshowtitle")   or ""),
                                                  "plot"      :(nowItem.get("plot")          or ""),
                                                  "playcount" :(nowItem.get("playcount")     or ""),
                                                  "rating"    :(nowItem.get("rating")        or ""),
                                                  "firstaired":(nowItem.get("firstaired")    or ""),
                                                  "runtime"   :(nowItem.get("runtime")       or "")}}
            data.update(current_episode)
        except Exception: pass
        try:
            next_episode    = {"next_episode"   :{"episodeid" :(nextItem.get("id")           or ""),
                                                  "tvshowid"  :(nextItem.get("tvshowid")     or ""),
                                                  "title"     :(nextItem.get("title")        or ""),
                                                  "art"       :(nextItem.get("art")          or ""),
                                                  "season"    :(nextItem.get("season")       or ""),
                                                  "episode"   :(nextItem.get("episode")      or ""),
                                                  "showtitle" :(nextItem.get("tvshowtitle")  or ""),
                                                  "plot"      :(nextItem.get("plot" )        or ""),
                                                  "playcount" :(nextItem.get("playcount")    or ""),
                                                  "rating"    :(nextItem.get("rating")       or ""),
                                                  "firstaired":(nextItem.get("firstaired")   or ""),
                                                  "runtime"   :(nextItem.get("runtime")      or "")}}
            data.update(next_episode)

        except Exception: pass
        if data: timerit(self.jsonRPC.notifyAll)(0.5,*('upnext_data', binascii.hexlify(FileAccess.dumpJSON(data).encode('utf-8')).decode('utf-8'), '%s.SIGNAL'%(ADDON_ID)))


    def close(self):
        self.log("close")
        with self._onNextLock: # madteevee (imports fork, imports.64): Up next's controls go with the overlay;
            self._onNextClosed = True # closed first, so no show can start in between
            for control in list(self._onNextControls.values()): self._delControl(control)
            self._onNextControls = {}
        self.toggleOnNext(False)
        self._delControl(self.vignette)
        self._delControl(self.channelBug)
        if self.vinView != self.defaultView: timerit(self.jsonRPC.setViewMode)(0.5,self.defaultView)
        # self.runActions(RULES_ACTION_OVERLAY_CLOSE, self.citem, inherited=self) #debug NoneType, RULES_ACTION_OVERLAY_CLOSE obsolete? 
        PROPERTIES.setRunning('Overlay',False)
