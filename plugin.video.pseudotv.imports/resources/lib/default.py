#   Copyright (C) 2026 Lunatixz
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
from globals    import *
from logger     import log
from plugin     import Plugin
from pool       import threadit

def _run(mode, sysInfo={}):
    # No @debounceit decorator — nightly added one with RPC_Delay (interpreted as seconds,
    # default 1s) which coalesced rapid CH+/CH- presses into a single delayed plugin call.
    # Intermediate setResolvedUrl invocations never fired, so Kodi PVR showed the chiron for
    # each press but only opened the last channel after the user paused. Master fork ran each
    # plugin invocation immediately and used MONITOR().waitForAbort(RPC_Delay/1000) as a
    # tiny post-play thread-settle sleep below.
    Plugin(mode, sysInfo)
    
if __name__ == '__main__':
    try:
        with PROPERTIES.suspendActivity():
            try:
                sysARG  = sys.argv
                sysInfo = dict(urllib.parse.parse_qsl(sysARG[2][1:].replace('.pvr','')))
            except Exception as e:
                log(f'Default: __main__, URL parse failed ({e!r}); using sys.argv[1] fallback', xbmc.LOGWARNING)
                sysARG  = ['plugin://plugin.video.pseudotv.imports/', '1', sys.argv[1], 'resume:false']
                sysInfo = dict(urllib.parse.parse_qsl(sysARG[2][1:].replace('.pvr','')))
            log(f'Default: __main__, mode = {sysInfo.get("mode")}, argv = {sysARG}')
            
            if sysInfo.get('mode') is None:
                xbmcplugin.setResolvedUrl(int(sys.argv[1]), False, xbmcgui.ListItem())
                if Globals._getProperty('has.Channels') == "true": Globals._openGuide()
                else:                                              Globals._openSettings()
            else:
                # v.33: live tunes from iptvsimple arrive with the main M3U URL
                # form: vid={catchup-id}&start={utc}&duration={duration}&stop=
                # {utcend}. iptvsimple substitutes {lutc} → now but leaves the
                # catchup tokens as literals for live (they only get filled for
                # actual catchup playback). Pre-rebase nightly aborted with
                # setResolvedUrl(False) on this case — broke ALL live PVR tunes
                # silently (Kodi just dropped the play attempt with no UI).
                # Plugin.__init__ also explodes on int('{utc}') at plugin.py:33
                # if templates reach it, so normalize them here. Master fork
                # pattern: keep the chkPVRRefresh flag, zero the templates,
                # proceed to the normal play path.
                _TEMPLATES = {'start':'{utc}', 'stop':'{utcend}', 'duration':'{duration}', 'vid':'{catchup-id}'}
                if any(sysInfo.get(k) == v for k, v in _TEMPLATES.items()):
                    Globals._setProperty('chkPVRRefresh', 'true')
                    for k, tmpl in _TEMPLATES.items():
                        if sysInfo.get(k) == tmpl:
                            sysInfo[k] = '' if k == 'vid' else '0'
                try:    fitem, nitem = LISTITEMS.buildDictListItem(sys.listitem), {}
                except: fitem, nitem = Globals._decodePlot(Globals._getInfoLabel('Plot')), Globals._decodePlot(Globals._getInfoLabel('NextPlot'))
                chid, vid   = (sysInfo.get("chid")  or fitem.get('citem',{}).get('id')), FileAccess._decodeString(sysInfo.get("vid",""))
                name, title = (Globals._unquoteString(sysInfo.get("name",'')) or Globals._getInfoLabel('ChannelName')), (Globals._unquoteString(sysInfo.get('title','')) or Globals._getInfoLabel('label'))
                # madteevee (imports fork): align fitem.file / fitem.title with
                # the freshly-decoded URL params before they go downstream. The
                # tune URL pvr.iptvsimple invokes carries the AUTHORITATIVE
                # current EPG event (substituted from the just-parsed
                # pseudotv.xml right before iptvsimple invoked us). But
                # sys.listitem here reflects Kodi PVR's internal EPG DB which
                # lags behind iptvsimple's view — frequently by 1-2 EPG slots
                # right after a `_writeAtomic` rewrite. Without this realign,
                # playLive at plugin.py:182 built a listitem from the stale
                # fitem (Korra .mkv) while sysInfo['vid'] pointed at the
                # correct file (Regular Show .mkv). The seek was computed for
                # the URL's start/stop (Regular Show times) but the listitem
                # path was Korra → wrong file played mid-show offset, OR right
                # file played with wrong title/artwork overlays.
                # Now: when the URL provides a non-empty decoded vid (the
                # inner `vid=<base64>` of the catchup-id) and a non-empty
                # title, override fitem.file / fitem.label / fitem.title so
                # downstream code paths (playLive/playPlaylist/_setResume)
                # all build listitems pointing at the right file with the
                # right display metadata.
                if isinstance(fitem, dict) and vid and isinstance(vid, str) and vid.startswith('/'):
                    if fitem.get('file') and fitem['file'] != vid:
                        log(f"Default: realigning fitem.file from {fitem.get('file')} to {vid} (URL-decoded)", xbmc.LOGINFO)
                    fitem['file']         = vid
                    fitem['originalpath'] = vid
                    if title:
                        fitem['title']        = title
                        fitem['label']        = title
                # _tune_ts (madteevee fork): per-invocation timestamp threaded through to onAVStarted.
                # Carries through sysInfo -> ListItem property -> getplayingItem -> services.onAVStarted,
                # where stale (older-tune-ts) events from aborted fast-zaps are dropped before mutating
                # self.pendingItem. Without this, Kodi's load-finish-order onAVStarted re-firing
                # anchors player state to the oldest tune in a CH+/CH- burst.
                sysInfo.update({'mode':sysInfo.get('mode'),'sysARG':sysARG,'fitem':fitem,'nitem':nitem,'chid':chid,'vid':vid,'name':name,'title':title,'radio':sysInfo.get('mode') == "radio",'_tune_ts':time.time()})
                _run(sysInfo.get('mode'), sysInfo)
    except Exception as e: 
        log(f'Default: __main__, failed! {e}', xbmc.LOGERROR)
        Globals._notificationDialog(LANGUAGE(30079))