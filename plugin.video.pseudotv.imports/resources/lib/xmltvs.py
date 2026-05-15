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
import xmltv
import io
import os

from globals     import *
from m3u         import M3U
from seasonal    import Seasonal
from fileaccess  import FileAccess, FileLock
from writer_lock import held, WriterTimeout


def _xmltvTzSuffix():
    """Build the local-TZ ' +HHMM' / ' -HHMM' suffix per XMLTV spec.
    Recomputed each call so DST transitions don't trip up long-running services."""
    off = int(getTimeoffset())
    return ' %s%02d%02d' % ('+' if off >= 0 else '-', abs(off) // 3600, (abs(off) % 3600) // 60)


def _stripXmltvTzSuffix(s):
    """Make strpTime(..., DTFORMAT) tolerant of the ' +HHMM' suffix we now emit.
    Backwards-compat: also tolerates legacy no-suffix entries from prior builds."""
    if not isinstance(s, str): return s
    s = s.strip()
    # Pattern: ' +HHMM' or ' -HHMM' at the end (6 trailing chars: space + sign + 4 digits)
    if len(s) >= 6 and s[-6] == ' ' and s[-5] in '+-' and s[-4:].isdigit():
        return s[:-6]
    return s


#todo check for empty recordings/channel meta and trigger refresh/rebuild empty xmltv via Kodi json rpc?

class XMLTVS(object):
    XMLTVDATA = {}
    
    def __init__(self, file=XMLTVFLEPATH, writable=False, m3u=None):
        if m3u is None: m3u = M3U(writable=writable)
        self.m3u       = m3u
        self.writable  = writable
        self.XMLTVFile = file
        self.XMLTVDATA = self._load()
        

    def __del__(self):
        # madteevee (imports fork, imports.12 / writer_lock): GC-path
        # `_save` dropped. Mirrors m3u.py:__del__ — the safety-net save
        # was for the FileLock + @debounceit era; both are gone, and
        # every meaningful mutation now triggers an explicit upstream
        # _save (Builder.__setStation per channel; Imports.syncAll
        # force-flush at end of cycle). Holding GC for up to
        # WRITER_TIMEOUT to acquire the writer lock for a no-op save
        # would be strictly harmful. See resources.py:71-102 for the
        # symmetric lesson on GC + contended I/O.
        try:
            self.log('__del__, writable = %s'%(self.writable))
        except Exception: pass
            
            
    def log(self, msg, level=xbmc.LOGDEBUG):
        return log('%s: %s'%(self.__class__.__name__,msg),level)


    def _load(self) -> dict:
        self.log('_load')
        channels, recordings = self.cleanSelf(self.loadChannels(),'id')
        return {'data'       : self.loadData(),
                'channels'   : channels,
                'recordings' : recordings,
                'programmes' : self.cleanSelf(self.loadProgrammes(),'channel')}


    # madteevee (imports fork, imports.12 / writer_lock): public `_save`
    # serializes via WRITER_LOCK so concurrent writers (Builder thread,
    # chkImports daemon) can't both os.replace pseudotv.xml at the same
    # time. Pre-refactor, the second writer's content silently overwrote
    # the first (the comment below documented this as "benign overwrite"
    # but the Custom-merge-from-disk workaround at imports.py:916-955
    # exists precisely to recover from it). RLock so re-entry from the
    # same thread is safe — buildGenres' FileLock on genre.xml stacks
    # under this lock without deadlock risk (different lock objects,
    # consistent acquisition order). WriterTimeout (lock holder wedged
    # >30s) is treated as benign skip; the skip is logged with ctx.
    def _save(self, reset: bool=True) -> bool:
        try:
            with held(ctx='XMLTVS._save'):
                return self._saveLocked(reset=reset)
        except WriterTimeout:
            return False


    # madteevee (imports fork): @debounceit REMOVED. Mirrors the m3u.py:_save
    # change — the trailing-Timer wrapper was the remaining hang vector in
    # builder.__setStation. Atomic writes already make serialization
    # unnecessary (no FileLock to coalesce against; concurrent writers all
    # rename their own .tmp and the last one wins, benign overwrite).
    #
    # imports.13 / C#10 Follow-up A: render+write delegated to the
    # module-level `render_xmltv` + `write_atomic` in renderers.py.
    # The sort + cleanChannels + orphan-prune pass stays here (class-
    # side) — kept conservative so chkImports / Imports.syncAll byte
    # output stays byte-identical to pre-consolidation. The render
    # itself is now identical to today's chkImports path. See the plan
    # at /home/madalone/.claude/plans/misty-shimmying-liskov.md.
    def _saveLocked(self, reset: bool=True) -> bool:
        if reset: data = self.resetData()
        else:     data = self.XMLTVDATA.get('data', {})
        # Stash data dict so render_xmltv picks it up via xmltvdata['data'].
        self.XMLTVDATA['data'] = data

        self.XMLTVDATA['programmes'] = self.sortProgrammes(self.XMLTVDATA['programmes'])
        self.XMLTVDATA['channels']   = self.cleanChannels(self.sortChannels(self.XMLTVDATA['channels'])  , self.XMLTVDATA['programmes'], opt='PROGRAMMES')
        self.XMLTVDATA['recordings'] = self.cleanChannels(self.sortChannels(self.XMLTVDATA['recordings']), self.XMLTVDATA['programmes'], opt='RECORDINGS')

        # madteevee (imports fork): orphan-programme prune. cleanChannels
        # filters channels by allowlist but leaves programmes whose `channel`
        # reference no longer exists in XMLTVDATA. Live-side fork measured
        # 22 MB / 26% bloat from this. Drop dangling programmes here so they
        # never reach the writer.
        _alive_ids = {c.get('id') for c in self.XMLTVDATA['channels']   if c.get('id')}
        _alive_ids |= {c.get('id') for c in self.XMLTVDATA['recordings'] if c.get('id')}
        _before = len(self.XMLTVDATA['programmes'])
        if _alive_ids:
            self.XMLTVDATA['programmes'] = [p for p in self.XMLTVDATA['programmes'] if p.get('channel') in _alive_ids]
        _after = len(self.XMLTVDATA['programmes'])
        if _before != _after:
            self.log('_save, pruned %d orphan programmes (no matching channel; was %d, now %d)' % (_before - _after, _before, _after), xbmc.LOGINFO)

        self.log('_save, writable = %s, file = %s, reset = %s\nchannels = %s, programmes = %s, recordings = %s'%(self.writable,self.XMLTVFile,reset,len(self.XMLTVDATA['channels']),len(self.XMLTVDATA['programmes']),len(self.XMLTVDATA['recordings'])))

        if not self.writable:
            return False
        # Function-local import: avoids module-load circular dependency
        # (renderers.py imports `from m3u import M3U_TEMP` inside its
        # body, so xmltvs.py must finish loading before renderers' first
        # use). By the time _saveLocked is called, both modules are loaded.
        from renderers import render_xmltv, write_atomic
        rendered = render_xmltv(self.XMLTVDATA)
        if rendered is None:
            # Empty-data guard fired in render_xmltv; preserve disk content.
            return False
        # imports.42: pass channel_count for write_atomic's circuit-breaker
        # + LOGINFO size message. channels + recordings combined matches
        # the renderer's own iteration (render_xmltv line 271).
        channel_count = (len(self.XMLTVDATA.get('channels')   or [])
                       + len(self.XMLTVDATA.get('recordings') or []))
        try:
            write_atomic(self.XMLTVFile, rendered, channel_count=channel_count)
        except Exception as e:
            self.log("_save, failed: %s" % (e,), xbmc.LOGERROR)
            DIALOG.notificationDialog(LANGUAGE(32000))
        # buildGenres writes genre.xml as a side effect under its own
        # FileLock. Lock ordering: outer WRITER_LOCK (held by _save
        # wrapper, RLock so re-entrant) → inner FileLock(GENREFLEPATH).
        # Different lock instances, single-direction nesting; no deadlock
        # risk per the C#5 per-path-FileLock refactor.
        return self.buildGenres()
        
        
    def _reload(self) -> bool:
        self.log('_reload') 
        self.__init__()
    
    
    def _error(self, name, file, e):
        # Signature is (name, file, e) — all 3 callers (loadData/loadChannels/loadProgrammes)
        # pass 3 positional args. Nightly upstream stripped the 'file' parameter, turning every
        # xmltv parse failure into a misleading TypeError that masked the real error and crashed
        # the service. Restored from the master-era fork.
        if not 'no element found: line 1, column 0' in str(e):
            try:
                match = re.compile(r'line\ (.*?),\ column\ (.*)', re.IGNORECASE).search(str(e))
                if match:
                    fle = None
                    try:
                        fle  = FileAccess.open(file,'r')
                        lines = fle.readlines()
                        self.log('%s, failed! parser error %s\nLine: %s\n Error: %s'%(name,e,lines[int(match.group(1))],lines[int(match.group(1))][int(match.group(2))-5:]), xbmc.LOGERROR)
                    except Exception:
                        self.log('%s, failed! parser error %s'%(name,e), xbmc.LOGERROR)
                    finally:
                        if fle is not None and hasattr(fle, 'close'):
                            fle.close()
                else: raise Exception('no parser match %s'%(str(e)))
            except Exception as en: self.log('%s, failed! %s\n%s'%(name,e,en), xbmc.LOGERROR)
    
                
    def resetData(self):
        self.log('resetData')
        return {'date'                : epochTime(float(time.time()),tz=False).strftime(DTFORMAT),
                'generator-info-name' : self.cleanString('%s Guidedata'%(ADDON_NAME)),
                'generator-info-url'  : self.cleanString(ADDON_ID),
                'source-info-name'    : self.cleanString(ADDON_NAME),
                'source-info-url'     : self.cleanString(ADDON_ID)}


    def loadData(self) -> dict:
        self.log('loadData, file = %s'%self.XMLTVFile)
        data = self.resetData()
        fle = None
        try:
            fle  = FileAccess.open(self.XMLTVFile, 'r')
            return (xmltv.read_data(fle) or data)
        except Exception as e:
            self._error('loadData',self.XMLTVFile,e)
            return data
        finally:
            if fle is not None and hasattr(fle, 'close'):
                fle.close()


    def loadChannels(self) -> list:
        self.log('loadChannels, file = %s'%self.XMLTVFile)
        fle = None
        try:
            fle  = FileAccess.open(self.XMLTVFile, 'r')
            return (xmltv.read_channels(fle) or [])
        except Exception as e:
            self._error('loadChannels',self.XMLTVFile,e)
            return []
        finally:
            if fle is not None and hasattr(fle, 'close'):
                fle.close()


    def loadProgrammes(self) -> list:
        self.log('loadProgrammes, file = %s'%self.XMLTVFile)
        fle = None
        try:
            fle = FileAccess.open(self.XMLTVFile, 'r')
            return (xmltv.read_programmes(fle) or [])
        except Exception as e:
            self._error('loadProgrammes',self.XMLTVFile,e)
            return []
        finally:
            if fle is not None and hasattr(fle, 'close'):
                fle.close()

            
    def loadStopTimes(self, channels: list=[], programmes: list=[], fallback=None):
        if not channels:   channels   = self.getChannels()
        if not programmes: programmes = self.getProgrammes()
        if not fallback:   fallback   = epochTime(roundTimeDown(getUTCstamp(),offset=60),tz=False).strftime(DTFORMAT)

        for channel in channels:
            try:
                firstStart = min((program['start'] for program in programmes if program['channel'] == channel['id']), default=fallback)
                lastStop   = max((program['stop']  for program in programmes if program['channel'] == channel['id']), default=fallback)
                self.log(' [%s] loadStopTimes first-start = %s, last-stop = %s, fallback = %s'%(channel['id'],firstStart,lastStop,fallback))
                if _stripXmltvTzSuffix(firstStart) > _stripXmltvTzSuffix(fallback): raise Exception('First start-time in the future, rebuild channel with fallback')
                yield channel['id'],datetime.datetime.timestamp(strpTime(_stripXmltvTzSuffix(lastStop), DTFORMAT))
            except Exception as e:
                self.log(" [%s] loadStopTimes failed!\nMalformed XMLTV channel/programmes %s! rebuilding channel with default stop-time %s"%(channel.get('id'),e,fallback), xbmc.LOGWARNING)
                yield channel['id'],datetime.datetime.timestamp(strpTime(_stripXmltvTzSuffix(fallback), DTFORMAT))


    def hasProgrammes(self, channels: list=[], programmes: list=[], now=None):
        if not channels:   channels   = self.getChannels()
        if not programmes: programmes = self.getProgrammes()
        if not now: now = epochTime(roundTimeDown(getUTCstamp(),offset=60),tz=False).strftime(DTFORMAT)
        for channel in channels:
            try: 
                valid = False
                last_stop = max((program['stop'] for program in programmes if program['channel'] == channel['id']), default=now)
                if last_stop > now: valid = True
                self.log('[%s] hasProgrammes, valid = %s'%(channel['id'],valid))
                yield channel['id'],valid
            except Exception as e:
                self.log("[%s] hasProgrammes, failed!\nMalformed XMLTV channel/programmes %s! valid = False"%(channel.get('id'),e), xbmc.LOGWARNING)
                yield channel['id'],False


    def cleanString(self, text: str) -> str:
        if text == ', ' or not text: text = LANGUAGE(32020) #"Unavailable"
        return bytes(text,DEFAULT_ENCODING).decode(DEFAULT_ENCODING,'ignore')


    def cleanStations(self, programmes: list=[]):
        if not self.m3u is None:
            programs = dict(self.hasProgrammes(self.getChannels(),programmes))
            for id, hasProgram in programs.items():
                print('cleanStations',id, hasProgram)
                if id and not hasProgram:
                    self.m3u.delStation({'id':id})
                    self.delBroadcast({'id':id})
                    self.log('cleanStations, removing = %s; no programmes!'%(id))
        return programmes
        
        
    def cleanRecordings(self, programmes: list=[]):
        if not self.m3u is None:
            programs = dict(self.hasProgrammes(self.getRecordings(), programmes))
            for id, hasProgram in programs.items():
                print('cleanRecordings',id, hasProgram)
                if id and not hasProgram:
                    self.m3u.delRecording({'id':id})
                    self.delRecording({'id':id})
                    self.log('cleanRecordings, removing = %s; no programmes!'%(id))
        return programmes
        
        
    def cleanSelf(self, items: list=[], key: str='id', slug: str='@%s'%(PSEUDOTV_SLUG)) -> list: # remove (Non PseudoTV Live), key = {'id':channels,'channel':programmes}
        if not slug: return items
        # madteevee (imports fork): ownership ("is this one of ours?") goes
        # through globals.isOwnedID — same predicate used by m3u.cleanSelf so
        # the two writers cannot drift on what counts as ours. What stays here
        # is the FORMAT-LENGTH check: PseudoTV channels are 32-char hashes
        # (getChannelID), recordings are 16-char (getRecordID); the length
        # marker is how this writer tells them apart inside a single mixed
        # input list. Imports never produce recordings, so the recording
        # predicate intentionally rejects import suffixes.
        try:
            from channels import Channels as _C
            _chans = _C()
            import_slugs = {'@%s' % (i.get('id'),) for i in (_chans.getImports() or []) if i.get('id')}
            # madteevee (imports fork, imports.15 / C#10 Follow-up E):
            # capture the live channel-id set from channels.json so cleanSelf
            # can drop XMLTV channels (and via the programmes branch, their
            # programmes) whose ids are no longer in channels.json.
            # Symmetric to M3U._verify (m3u.py:471-475) which already does
            # this for the M3U side. Without this cross-reference, Custom
            # channels deleted via the UI leave orphan channel + programme
            # entries in pseudotv.xml indefinitely (the operator-reported
            # bug for "Five Fingers of Death": M3U cleaned correctly, XML
            # carried the orphan + 181 programmes).
            # `None` on lookup failure → cross-reference disabled → falls
            # back to today's namespace+length filter (no regression).
            # imports.33: predicate tightened to `id-in-channels.json AND
            # enabled=True`. Without the enabled filter, disabled Custom
            # channels' <channel> elements and their programmes survive
            # cleanSelf-on-load — pvr.iptvsimple keeps showing stale EPG
            # for them. Symmetric with the parallel m3u.py:_verify change.
            live_channel_ids = {c.get('id') for c in (_chans.getChannels() or [])
                                if c.get('id') and c.get('enabled', True)}
        except Exception:
            import_slugs = set()
            live_channel_ids = None
        def _id_ok_channel(s):
            if not isOwnedID(s, slug, import_slugs): return False
            if slug and s.endswith(slug):
                # PseudoTV-suffixed: must be 32-char hash AND in channels.json.
                if len(s.replace(slug, '')) != 32: return False
                if live_channel_ids is not None and s not in live_channel_ids:
                    return False
                return True
            # Import-suffixed entries: ALSO cross-reference channels.json.
            # setImports purges channelDATA when an import is removed; this
            # check ensures the XML side honors the same purge.
            if live_channel_ids is not None and s not in live_channel_ids:
                return False
            return True  # import suffix already validated by isOwnedID
        def _id_ok_recording(s):
            # Recordings are PseudoTV-only (16-char hash + @PseudoTV_Live).
            # NOT cross-referenced against channels.json — recordings live
            # only in M3U/XML files (managed via context_record.py add/
            # remove), they're not tracked in channels.json.
            if not isOwnedID(s, slug, set()): return False
            return len(s.replace(slug, '')) == 16
        channels   = list([item for item in items if _id_ok_channel(item.get(key, ''))])
        recordings = list([item for item in items if _id_ok_recording(item.get(key, ''))])
        if key == 'id': #stations
            self.log('cleanSelf, slug = %s, import_slugs = %s, key = %s: returning channels = %s, recordings = %s'%(slug,sorted(import_slugs),key,len(channels),len(recordings)))
            return self.sortChannels(Globals._setDictLST(channels)), self.sortChannels(Globals._setDictLST(recordings))
        elif key == 'channel': #programmes
            programmes = self.cleanStations(self.cleanProgrammes(channels)) +  self.cleanRecordings(recordings)
            self.log('cleanSelf, slug = %s, import_slugs = %s, key = %s: returning programmes = %s'%(slug,sorted(import_slugs),key,len(programmes)))
            return self.sortProgrammes(programmes)
        
        
    def cleanChannels(self, channels: list=[], programmes: list=[], opt='PROGRAMMES') -> list: # remove stations with no guidedata
        stations    = list(set([program.get('channel') for program in programmes]))
        tmpChannels = [channel for station in stations for channel in channels if channel.get('id') == station]
        self.log('cleanChannels [%s], before = %s, after = %s'%(opt,len(channels),len(tmpChannels)))
        return tmpChannels


    def cleanProgrammes(self, programmes: list=[]) -> list:
        now     = (epochTime(float(getUTCstamp()),tz=False) - datetime.timedelta(days=MIN_GUIDEDAYS)) #allow some old programmes to avoid empty cells
        holiday = Seasonal().getHoliday()
        
        def __filterProgrammes(program):
            citem    = Globals._decodePlot(program.get('desc',([{}],''))[0][0]).get('citem',{})
            seasonal = citem.get('rules',{}).get(800,{}).get('values',{}).get(0,[{}])[0].get('holiday',{})
            stopTime = program.get('stop',now)
            if isinstance(stopTime, str): stopTime = stopTime.rstrip()
            if seasonal and seasonal.get('name',str(random.random())) != holiday.get('name'):
                self.log('[%s] cleanProgrammes, __filterProgrammes removing expired holiday (%s)'%(citem.get('id'),seasonal))
                return None
            # madteevee (imports fork): strpTime returns '' on parse failure
            # (globals.py:119). Comparing '' < datetime raises TypeError —
            # crashed the addon at startup once the cleanSelf patch let
            # imported programmes through (their stop format may differ
            # from internal DTFORMAT). Treat unparseable stop times as
            # "keep the programme" (safer than dropping silently).
            # _stripXmltvTzSuffix tolerates both legacy no-suffix entries and
            # the +HHMM-suffixed entries we now emit (XMLTV-spec compliance).
            parsed_stop = strpTime(_stripXmltvTzSuffix(stopTime), DTFORMAT)
            if not isinstance(parsed_stop, datetime.datetime):
                return program
            if parsed_stop < now:
                self.log('[%s] cleanProgrammes, __filterProgrammes removing expired programmes (%s)'%(citem.get('id'),stopTime))
                return None  # remove expired content, todo ignore "recordings" ie. media=True
            return program
            
        tmpProgrammes = [program for program in [__filterProgrammes(program) for program in programmes] if program is not None]
        self.log('cleanProgrammes, before = %s, after = %s'%(len(programmes),len(tmpProgrammes)))
        return tmpProgrammes


    def sortChannels(self, channels: list=[]) -> list:
        try:    return sorted(channels, key=itemgetter('display-name'))
        except Exception: return channels
        

    def sortProgrammes(self, programmes: list=[]) -> list:
        try:
            programmes.sort(key=itemgetter('start'))
            programmes.sort(key=itemgetter('channel'))
            self.log('sortProgrammes, programmes = %s'%(len(programmes)))
            return programmes
        except Exception as e:
            self.log("sortProgrammes, failed! %s"%(e), xbmc.LOGERROR)
            return []


    def getRecordings(self) -> list:
        self.log('getRecordings')
        return self.sortChannels(self.XMLTVDATA.get('recordings',[]))
                
                
    def getChannels(self) -> list:
        self.log('getChannels')
        return self.sortChannels(self.XMLTVDATA.get('channels',[]))
        
        
    def getProgrammes(self) -> list:
        self.log('getProgrammes')
        return self.sortProgrammes(self.XMLTVDATA.get('programmes',[]))


    def findChannel(self, citem: dict, channels: list=[]) -> tuple:
        if not channels: channels = self.getChannels()
        return tuple(next(((idx, eitem) for idx, eitem in enumerate(channels) if citem.get('id') == eitem.get('id',str(random.random()))),(None, {})))
        
        
    def findRecording(self, ritem: dict, recordings: list=[]) -> tuple:
        if not recordings: recordings = self.getRecordings()
        def __match(eitem):
            return ritem.get('id') == eitem.get('id',str(random.random())) or (ritem.get('name','').lower() == eitem.get('display-name')[0][0].lower())
        return tuple(next(((idx, eitem) for idx, eitem in enumerate(recordings) if __match(eitem)),(None, {})))


    def getProgramItem(self, citem: dict, fItem: dict) -> dict:
        ''' Convert fileItem to Programme (XMLTV) item '''
        item = {}
        item['channel']       = citem['id']
        item['radio']         = citem['radio']
        item['start']         = fItem['start']
        item['stop']          = fItem['stop']
        item['title']         = fItem['label']
        # madteevee: prepend smart-subs label to the visible description. Set by
        # the ForceSubtitles rule at BUILD_FILELIST_POST for smart-mode channels;
        # absent for legacy / always-on / always-off / non-smart channels.
        _ssl = fItem.get('smart_subs_label') or ''
        item['desc']          = ('[I]%s[/I]\n\n%s'%(_ssl, fItem['plot'])) if _ssl else fItem['plot']
        item['length']        = fItem['duration']
        item['sub-title']     = (fItem.get('episodetitle') or '')
        item['categories']    = (fItem.get('genre')        or ['Undefined'])[:5]#trim list to five
        item['type']          = fItem.get('type','video')
        item['new']           = int(fItem.get('playcount','1')) == 0
        
        item['thumb']                = Globals._getThumb(fItem,EPG_ARTWORK) #unify thumbnail by user preference
        fItem.get('art',{})['thumb'] = Globals._getThumb(fItem,{0:1,1:0}[EPG_ARTWORK]) #unify thumbnail artwork, opposite of EPG_Artwork
         
        if item['type'] == 'movie': item['date'] = (fItem.get('premiered')  or fItem.get('releasedate') or fItem.get('firstaired'))
        else:                       item['date'] = (fItem.get('firstaired') or fItem.get('releasedate') or fItem.get('premiered'))
        
        item['catchup-id']    = VOD_URL.format(addon=ADDON_ID,title=Globals._quoteString(item['title']),chid=Globals._quoteString(citem['id']),vid=(FileAccess._encodeString((fItem.get('originalfile') or fItem.get('file','')))),name=Globals._quoteString(citem['name']))
        fItem['catchup-id']   = item['catchup-id']
            
        if (item['type'] != 'movie' and ((fItem.get("season",0) > 0) and (fItem.get("episode",0) > 0))):
            item['episode-num'] = {'xmltv_ns':'%s.%s'%(fItem.get("season",1)-1,fItem.get("episode",1)-1), # todo support totaleps <episode-num system="xmltv_ns">..44/47</episode-num>https://github.com/kodi-pvr/pvr.iptvsimple/pull/884
                                   'onscreen':'S%sE%s'%(str(fItem.get("season",0)).zfill(2),str(fItem.get("episode",0)).zfill(2))}

        item['rating']      = cleanMPAA(fItem.get('mpaa') or 'NA')
        item['stars']       = (fItem.get('rating')        or '0')
        item['writer']      = fItem.get('writer',[])[:5]   #trim list to five
        item['director']    = fItem.get('director',[])[:5] #trim list to five
        item['actor']       = ['%s - %s'%(actor.get('name'),actor.get('role',LANGUAGE(32020))) for actor in fItem.get('cast',[])[:5] if actor.get('name')]
        
        fItem['citem']      = citem #channel item (stale data due to xmltv storage) use for reference
        item['fitem']       = fItem #raw kodi fileitem/listitem, contains citem both passed through 'plot' xmltv param.
        
        streamdetails = fItem.get('streamdetails',{})
        if streamdetails:
            item['subtitle'] = list(set([sub.get('language','')                    for sub in streamdetails.get('subtitle',[]) if sub.get('language')]))
            item['language'] = ', '.join(list(set([aud.get('language','')          for aud in streamdetails.get('audio',[])    if aud.get('language')])))
            item['audio']    = True if True in list(set([aud.get('codec','')       for aud in streamdetails.get('audio',[])    if aud.get('channels',0) >= 2])) else False
            item.setdefault('video',{})['aspect'] = list(set([vid.get('aspect','') for vid in streamdetails.get('video',[])    if vid.get('aspect','')]))
        return item


    def addRecording(self, ritem: dict, fitem: dict):
        self.log('addRecording = %s'%(ritem.get('id')))
        sitem = ({'id'           : ritem['id'],
                  'display-name' : [(self.cleanString(ritem['name']), LANG)],
                  'icon'         : [{'src':ritem['logo']}]})
                  
        self.log('addRecording, sitem = %s'%(sitem))
        try:              self.XMLTVDATA['recordings'][self.findRecording(ritem)[0]] = sitem # replace existing channel meta
        except Exception: self.XMLTVDATA['recordings'].append(sitem)

        fitem['start'] = getUTCstamp()
        fitem['stop']  = fitem['start'] + fitem['duration']
        if self.addProgram(ritem['id'],self.getProgramItem(ritem,fitem),encodeDESC=True):
            return True
        
    
    def addChannel(self, citem: dict) -> bool:
        mitem = ({'id'           : citem['id'],
                  'display-name' : [(self.cleanString(citem['name']), LANG)],
                  'icon'         : [{'src':citem['logo']}]})
                  
        self.log('addChannel, mitem = %s'%(mitem))
        try:              self.XMLTVDATA['channels'][self.findChannel(mitem)[0]] = mitem
        except Exception: self.XMLTVDATA['channels'].append(mitem)
        return True


    def addProgram(self, id: str, item: dict, encodeDESC: bool=True) -> bool:
        # madteevee: append local-TZ suffix to start/stop. XMLTV spec says
        # timestamps WITHOUT a +HHMM suffix mean local time, but pvr.iptvsimple
        # interprets them as UTC — causing a permanent timezone-offset shift in
        # what it thinks is "broadcastnow" (we write 20:00 local meaning
        # 20:00, iptvsimple reads it as 20:00 UTC = 22:00 local). The bug
        # surfaces as every Custom channel showing a programme from
        # offset-hours-ago as "currently airing", which then resolves to that
        # past programme's plugin URL → playback starts from frame 0 because
        # the join-now check correctly refuses to seek past a stop that's
        # already elapsed. Adding the explicit +HHMM suffix makes iptvsimple
        # parse the local-time timestamps correctly.
        _tz_sfx = _xmltvTzSuffix()
        pitem = {'channel'     : id,
                 'category'    : [(self.cleanString(genre.replace(LANGUAGE(32105),'Undefined')),LANG) for genre in item['categories']],
                 'title'       : [(self.cleanString(item['title']), LANG)],
                 'desc'        : [(Globals._encodePlot(self.cleanString(item['desc']),item['fitem']), LANG) if encodeDESC else (self.cleanString(item['desc']), LANG)],
                 'stop'        : (epochTime(float(item['stop']),tz=False).strftime(DTFORMAT) + _tz_sfx),
                 'start'       : (epochTime(float(item['start']),tz=False).strftime(DTFORMAT) + _tz_sfx),
                 'icon'        : [{'src': Globals._toEpgIconURL(item['thumb'])}], # v.40: wrap image:// → Kodi /image/ proxy URL so iptvsimple accepts it
                 'length'      : {'units': 'seconds', 'length': str(item['length'])}}
                        
        if item.get('sub-title'):
            pitem['sub-title'] = [(self.cleanString(item['sub-title']), LANG)]

        if item.get('stars'):
            pitem['star-rating'] = [{'value': '%s/10'%(int(round(float(item['stars']))))}]
 
        if item.get('writer'):
            pitem.setdefault('credits',{})['writer'] = [self.cleanString(writer) for writer in item['writer']]
            
        if item.get('director'):
            pitem.setdefault('credits',{})['director'] = [self.cleanString(director) for director in item['director']]
            
        if item.get('actor'):
            pitem.setdefault('credits',{})['actor'] = [self.cleanString(actor) for actor in item['actor']]

        if item.get('catchup-id'):
            pitem['catchup-id'] = item['catchup-id']
            
        if item.get('date'):
            try: pitem['date'] = (strpTime(item['date'], '%Y-%m-%d')).strftime('%Y%m%d')
            except Exception: pass

        if item.get('new',False): 
            pitem['new'] = '' #write empty tag, tag == True
        
        rating = item.get('rating','NA')
        if rating != 'NA':
            if rating.lower().startswith('tv'): 
                pitem['rating'] = [{'system': 'VCHIP', 'value': rating}]
            else:  
                pitem['rating'] = [{'system': 'MPAA', 'value': rating}] #todo support international rating systems
            
        if item.get('episode-num'): 
            pitem['episode-num'] = [(item['episode-num'].get('xmltv_ns',''), 'xmltv_ns'),
                                    (item['episode-num'].get('onscreen',''), 'onscreen')]
            
        if item.get('audio',False):
            pitem['audio'] = [{'stereo': 'stereo'}]

        # if item.get('video',{}):
            # pitem['video'] = [{'aspect': item.get('video',{}).get('aspect')}]
        
        # if item.get('language',''):
            # pitem['language'] = [(item.get('language'), LANG)]
           
        # if item.get('subtitle',[]):
            # pitem['subtitles'] = [{'type': 'teletext', 'language': ('%s'%(sub), LANG)} for sub in item.get('subtitle',[])]
            
         ##### TODO #####
           # 'country'     : [('USA', LANG)],#todo
           # 'premiere': (u'Not really. Just testing', u'en'),
           
        self.log('addProgram = %s'%(pitem.get('channel')))
        self.XMLTVDATA['programmes'].append(pitem)
        return True


    def clrProgrammes(self, citem: dict) -> bool:
        self.XMLTVDATA['programmes'] = [program for program in self.XMLTVDATA['programmes'] if program.get('channel') != citem.get('id')]
        self.log('clrProgrammes, removing channel %s programmes' % citem.get('id'))
        return True


    def delBroadcast(self, citem: dict) -> bool:# remove single channel and all programmes from XMLTVDATA
        channels   = self.XMLTVDATA['channels']
        programmes = self.XMLTVDATA['programmes']
        self.XMLTVDATA['channels']   = list([channel for channel in channels if channel.get('id') != citem.get('id')])
        self.XMLTVDATA['programmes'] = list([program for program in programmes if program.get('channel') != citem.get('id')])
        self.log('delBroadcast, removing channel %s; channels: before = %s, after = %s; programmes: before = %s, after = %s'%(citem.get('id'),len(channels),len(self.XMLTVDATA['channels']),len(programmes),len(self.XMLTVDATA['programmes'])))
        return True
        
        
    def delRecording(self, ritem: dict):
        self.log('[%s] delRecording'%((ritem.get('id') or ritem.get('label'))))
        recordings = self.XMLTVDATA['recordings']
        programmes = self.XMLTVDATA['programmes']
        idx, recording = self.findRecording(ritem)
        if idx is not None:
            self.XMLTVDATA['recordings'].pop(idx)
            if not ritem.get('id'): ritem['id'] = recording['id']
            self.XMLTVDATA['programmes'] = list([program for program in programmes if program.get('channel') != ritem.get('id')])
            return True
        
        
    def buildGenres(self, epggenres={}):
        def __parseGenres(plines):
            for line in plines:
                try:    
                    names = line.childNodes[0].data
                    items = names.split(' / ')
                    data  = {'genre':names,'name':names,'genreId':line.attributes['genreId'].value}
                    epggenres[names.lower()] = data
                    for item in items:
                        name = item.strip()
                        if name and not epggenres.get(name.lower()):
                            epgdata = data.copy()
                            epgdata['name'] = name
                            epggenres[name.lower()] = epgdata
                except Exception: continue
            self.log('buildGenres, __parseGenres: epggenres = %s'%(epggenres.keys())) #todo custom user color selector.
            return epggenres

        def __matchGenres(program):
            categories = [cat[0] for cat in program.get('category',[])]
            catcombo   = ' / '.join(categories)
            for category in categories:
                match = genres.get(category.lower())
                if match and not genres.get(catcombo.lower()):
                    genres[catcombo.lower()] = match
                    break
            
        def __getGenres(file=GENREFLE_DEFAULT):
            if FileAccess.exists(file): 
                fle = FileAccess.open(file, "r")
                dom = parse(fle)
                fle.close()
                return __parseGenres(dom.getElementsByTagName('genre'))
            return {}
            
        try:
            doc  = Document()
            root = doc.createElement('genres')
            doc.appendChild(root)
            name = doc.createElement('name')
            name.appendChild(doc.createTextNode('%s'%(ADDON_NAME)))
            root.appendChild(name)
            
            genres = __getGenres()
            [__matchGenres(program) for program in self.XMLTVDATA.get('programmes',[])]
            epggenres = __getGenres(GENREFLEPATH)
            epggenres.update(dict(sorted(sorted(list(genres.items()), key=lambda v:v[1]['name']), key=lambda v:v[1]['genreId'])))
            for key in list(set(epggenres)):
                gen = doc.createElement('genre')
                gen.setAttribute('genreId',epggenres[key].get('genreId'))
                gen.appendChild(doc.createTextNode(key.title()))
                root.appendChild(gen)
            try:
                with FileLock(GENREFLEPATH):
                    xmlData = FileAccess.open(GENREFLEPATH, "w")
                    xmlData.write(doc.toprettyxml(indent='  ',encoding=DEFAULT_ENCODING))
                    xmlData.close()
                    return True
            except Exception as e: self.log("buildGenres failed! %s"%(e), xbmc.LOGERROR)
        except Exception as e: self.log("buildGenres failed! %s"%(e), xbmc.LOGERROR)