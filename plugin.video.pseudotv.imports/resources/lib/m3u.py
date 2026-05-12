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
# https://github.com/kodi-pvr/pvr.iptvsimple#supported-m3u-and-xmltv-elements

# -*- coding: utf-8 -*-

import io
import os

from globals     import *
from channels    import Channels
from fileaccess  import FileAccess, FileLock
from writer_lock import held, WriterTimeout

M3U_TEMP = {"id"                : "",
            "number"            : 0,
            "name"              : "",
            "logo"              : "",
            "group"             : [],
            "catchup"           : "vod",
            "radio"             : False,
            "favorite"          : False,
            "realtime"          : False,
            "media"             : "",
            "label"             : "",
            "url"               : "",
            "tvg-shift"         : "",
            "x-tvg-url"         : "",
            "media-dir"         : "",
            "media-size"        : "",
            "media-type"        : "",
            "catchup-source"    : "",
            "catchup-days"      : "",
            "catchup-correction": "",
            "provider"          : "",
            "provider-type"     : "",
            "provider-logo"     : "",
            "provider-countries": "",
            "provider-languages": "",
            "x-playlist-type"   : "",
            "kodiprops"         : []}
            
M3U_MIN  = {"id"                : "",
            "number"            : 0,
            "name"              : "",
            "logo"              : "",
            "group"             : [],
            "catchup"           : "vod",
            "radio"             : False,
            "label"             : "",
            "url"               : ""}
            
class M3U(object):
    M3UDATA = {}
    
    def __init__(self, file=M3UFLEPATH, writable=False):
        self.writable    = writable
        self.stationFile = file
        stations, recordings = self.cleanSelf(list(self._load()))
        self.M3UDATA = {'data':'#EXTM3U tvg-shift="" x-tvg-url="%s" x-tvg-id="" catchup-correction=""'%('http://%s/%s'%(PROPERTIES.getRemoteHost(),XMLTVFLE)),
                        'stations':stations,'recordings':recordings}


    def __del__(self):
        # madteevee (imports fork, imports.12 / writer_lock): the GC-path
        # `_save` was a safety net for the FileLock + @debounceit era when
        # explicit saves could be debounced into oblivion. Both are gone;
        # every meaningful mutation now triggers an explicit upstream
        # _save (Builder.__setStation per channel; Imports.syncAll force-
        # flush at end of cycle). Holding GC for up to WRITER_TIMEOUT to
        # acquire the writer lock for a no-op save would be strictly
        # harmful. Mirrors the lesson from resources.py:71-102 — don't
        # block GC on contended I/O.
        try:
            self.log('__del__, writable = %s'%(self.writable))
        except Exception: pass


    def log(self, msg, level=xbmc.LOGDEBUG):
        return log('%s: %s'%(self.__class__.__name__,msg),level)


    def _load(self): # https://github.com/kodi-pvr/pvr.iptvsimple#supported-m3u-and-xmltv-elements
        self.log('_load, file = %s'%self.stationFile)
        if FileAccess.exists(self.stationFile): 
            try:
                fle   = FileAccess.open(self.stationFile, 'r')
                lines = fle.readlines()
            except Exception: lines = []
            finally:
                if hasattr(fle, 'close'): 
                    fle.close()
            
            chCount = 0
            data    = {}
            filter  = []
            for idx, line in enumerate(lines):
                line = line.rstrip()
                if line.startswith('#EXTM3U'):
                    data = {'tvg-shift'         :re.compile('tvg-shift=\"(.*?)\"'          , re.IGNORECASE).search(line),
                            'x-tvg-url'         :re.compile('x-tvg-url=\"(.*?)\"'          , re.IGNORECASE).search(line),
                            'catchup-correction':re.compile('catchup-correction=\"(.*?)\"' , re.IGNORECASE).search(line)}

                elif line.startswith('#EXTINF:'):
                    chCount += 1
                    match = {'label'             :re.compile(',(.*)'                        , re.IGNORECASE).search(line),
                             'id'                :re.compile('tvg-id=\"(.*?)\"'             , re.IGNORECASE).search(line),
                             'name'              :re.compile('tvg-name=\"(.*?)\"'           , re.IGNORECASE).search(line),
                             'group'             :re.compile('group-title=\"(.*?)\"'        , re.IGNORECASE).search(line),
                             'number'            :re.compile('tvg-chno=\"(.*?)\"'           , re.IGNORECASE).search(line),
                             'logo'              :re.compile('tvg-logo=\"(.*?)\"'           , re.IGNORECASE).search(line),
                             'radio'             :re.compile('radio=\"(.*?)\"'              , re.IGNORECASE).search(line),
                             'tvg-shift'         :re.compile('tvg-shift=\"(.*?)\"'          , re.IGNORECASE).search(line),
                             'catchup'           :re.compile('catchup=\"(.*?)\"'            , re.IGNORECASE).search(line),
                             'catchup-source'    :re.compile('catchup-source=\"(.*?)\"'     , re.IGNORECASE).search(line),
                             'catchup-days'      :re.compile('catchup-days=\"(.*?)\"'       , re.IGNORECASE).search(line),
                             'catchup-correction':re.compile('catchup-correction=\"(.*?)\"' , re.IGNORECASE).search(line),
                             'provider'          :re.compile('provider=\"(.*?)\"'           , re.IGNORECASE).search(line),
                             'provider-type'     :re.compile('provider-type=\"(.*?)\"'      , re.IGNORECASE).search(line),
                             'provider-logo'     :re.compile('provider-logo=\"(.*?)\"'      , re.IGNORECASE).search(line),
                             'provider-countries':re.compile('provider-countries=\"(.*?)\"' , re.IGNORECASE).search(line),
                             'provider-languages':re.compile('provider-languages=\"(.*?)\"' , re.IGNORECASE).search(line),
                             'media'             :re.compile('media=\"(.*?)\"'              , re.IGNORECASE).search(line),
                             'media-dir'         :re.compile('media-dir=\"(.*?)\"'          , re.IGNORECASE).search(line),
                             'media-size'        :re.compile('media-size=\"(.*?)\"'         , re.IGNORECASE).search(line),
                             'realtime'          :re.compile('realtime=\"(.*?)\"'           , re.IGNORECASE).search(line)}
                    
                    if match['id'].group(1) in filter:
                        self.log('_load, filtering duplicate %s'%(match['id'].group(1)))
                        continue
                    filter.append(match['id'].group(1)) #filter dups, todo find where dups originate from. 
                    
                    mitem = self.getMitem()
                    mitem.update({'number' :chCount,
                                  'logo'   :LOGO,
                                  'catchup':''}) #set default parameters
                    
                    for key, value in list(match.items()):
                        if value is None:
                            if data.get(key,None) is not None:
                                self.log('_load, using #EXTM3U "%s" value for #EXTINF'%(key))
                                value = data[key] #no local EXTINF value found; use global EXTM3U if applicable.
                            else: continue
                        
                        if value.group(1) is None:
                            continue
                        elif key == 'logo':
                            mitem[key] = value.group(1)
                        elif key == 'number':
                            try:    mitem[key] = int(value.group(1))
                            except Exception: mitem[key] = float(value.group(1))#todo why was this needed?
                        elif key == 'group':
                            mitem[key] = [_f for _f in sorted(list(set((value.group(1)).split(';')))) if _f]
                        elif key in ['radio','favorite','realtime','media']:
                            mitem[key] = (value.group(1)).lower() == 'true'
                        else:
                            mitem[key] = value.group(1)

                    for nidx in range(idx+1,len(lines)):
                        try:
                            nline = lines[nidx].rstrip()
                            if   nline.startswith('#EXTINF:'): break
                            elif nline.startswith('#EXTGRP'):
                                grop = re.compile('^#EXTGRP:(.*)$', re.IGNORECASE).search(nline)
                                if grop is not None: 
                                    mitem['group'].append(grop.group(1).split(';'))
                                    mitem['group'] = sorted(set(mitem['group']))
                            elif nline.startswith('#KODIPROP:'):
                                prop = re.compile('^#KODIPROP:(.*)$', re.IGNORECASE).search(nline)
                                if prop is not None: mitem.setdefault('kodiprops',[]).append(prop.group(1))
                            elif nline.startswith('#EXTVLCOPT'):
                                copt = re.compile('^#EXTVLCOPT:(.*)$', re.IGNORECASE).search(nline)
                                if copt is not None:  mitem.setdefault('extvlcopt',[]).append(copt.group(1))
                            elif nline.startswith('#WEBPROP'):
                                web = re.compile('^#WEBPROP:(.*)$', re.IGNORECASE).search(nline)
                                if web is not None:  mitem.setdefault('webprops',[]).append(web.group(1))
                            elif nline.startswith('#EXT-X-PLAYLIST-TYPE'):
                                xplay = re.compile('^#EXT-X-PLAYLIST-TYPE:(.*)$', re.IGNORECASE).search(nline)
                                if xplay is not None: mitem['x-playlist-type'] = xplay.group(1)
                            elif nline.startswith('##'): continue
                            elif not nline: continue
                            else: mitem['url'] = nline
                        except Exception as e: self.log('_load, error parsing m3u! %s'%(e))
                            
                    #Fill missing with similar parameters.
                    mitem['name']     = (mitem.get('name')     or mitem.get('label') or '')
                    mitem['label']    = (mitem.get('label')    or mitem.get('name')  or '')
                    mitem['favorite'] = (mitem.get('favorite') or False)
                    
                    #Set Fav. based on group value.
                    if LANGUAGE(32019) in mitem['group'] and not mitem['favorite']:
                        mitem['favorite'] = True
                    
                    #Core m3u parameters missing, ignore entry.
                    if not mitem.get('id') or not mitem.get('name') or not mitem.get('number'): 
                        self.log('_load, SKIPPED MISSING META m3u item = %s'%mitem)
                        continue
                        
                    self.log('_load, m3u item = %s'%mitem)
                    yield mitem
        
        
    @staticmethod
    def parseExternalSource(content, log_callback=None):
        # imports fork (madteevee): stateless EXTINF parser for live-import sources.
        # Mirrors _load's regex set verbatim — any future EXTINF tag added there
        # MUST also be added here. Returns a list of channel dicts; does NOT
        # touch M3UDATA (caller decides what to do with the parsed channels).
        # Defensive: validates EXTM3U header, decodes encoding, skips malformed
        # entries with missing id/name. See plan G2 (encoding), G24 (header
        # validation), H7 (malformed entries), I6 (mid-write detection).
        def _log(msg):
            if log_callback: log_callback(msg)

        # Decode bytes with BOM detection + UTF-8 errors=replace fallback (G2).
        if isinstance(content, bytes):
            if   content.startswith(b'\xef\xbb\xbf'): text = content[3:].decode('utf-8')
            elif content.startswith(b'\xff\xfe'):     text = content.decode('utf-16-le')
            elif content.startswith(b'\xfe\xff'):     text = content.decode('utf-16-be')
            else:
                try:    text = content.decode('utf-8')
                except UnicodeDecodeError:
                    _log('parseExternalSource, source not strict UTF-8; decoding with errors=replace')
                    text = content.decode('utf-8', errors='replace')
        else:
            text = content

        lines = text.splitlines()

        # Defensive: source must start with #EXTM3U (G24/I6).
        if not lines or not lines[0].lstrip().startswith('#EXTM3U'):
            raise ValueError('parseExternalSource: source missing #EXTM3U header')

        # Same regex set as _load (lines 103-129).
        EXTM3U_PATTERNS = {
            'tvg-shift'         : re.compile(r'tvg-shift="(.*?)"'         , re.IGNORECASE),
            'x-tvg-url'         : re.compile(r'x-tvg-url="(.*?)"'         , re.IGNORECASE),
            'catchup-correction': re.compile(r'catchup-correction="(.*?)"', re.IGNORECASE),
        }
        EXTINF_PATTERNS = {
            'label'             : re.compile(r',(.*)'                       , re.IGNORECASE),
            'id'                : re.compile(r'tvg-id="(.*?)"'              , re.IGNORECASE),
            'name'              : re.compile(r'tvg-name="(.*?)"'            , re.IGNORECASE),
            'group'             : re.compile(r'group-title="(.*?)"'         , re.IGNORECASE),
            'number'            : re.compile(r'tvg-chno="(.*?)"'            , re.IGNORECASE),
            'logo'              : re.compile(r'tvg-logo="(.*?)"'            , re.IGNORECASE),
            'radio'             : re.compile(r'radio="(.*?)"'               , re.IGNORECASE),
            'tvg-shift'         : re.compile(r'tvg-shift="(.*?)"'           , re.IGNORECASE),
            'catchup'           : re.compile(r'catchup="(.*?)"'             , re.IGNORECASE),
            'catchup-source'    : re.compile(r'catchup-source="(.*?)"'      , re.IGNORECASE),
            'catchup-days'      : re.compile(r'catchup-days="(.*?)"'        , re.IGNORECASE),
            'catchup-correction': re.compile(r'catchup-correction="(.*?)"'  , re.IGNORECASE),
            'provider'          : re.compile(r'provider="(.*?)"'            , re.IGNORECASE),
            'provider-type'     : re.compile(r'provider-type="(.*?)"'       , re.IGNORECASE),
            'provider-logo'     : re.compile(r'provider-logo="(.*?)"'       , re.IGNORECASE),
            'provider-countries': re.compile(r'provider-countries="(.*?)"'  , re.IGNORECASE),
            'provider-languages': re.compile(r'provider-languages="(.*?)"'  , re.IGNORECASE),
            'media'             : re.compile(r'media="(.*?)"'               , re.IGNORECASE),
            'media-dir'         : re.compile(r'media-dir="(.*?)"'           , re.IGNORECASE),
            'media-size'        : re.compile(r'media-size="(.*?)"'          , re.IGNORECASE),
            'realtime'          : re.compile(r'realtime="(.*?)"'            , re.IGNORECASE),
        }

        out      = []
        chCount  = 0
        ext_data = {}
        seen_ids = set()

        for idx, line in enumerate(lines):
            line = line.rstrip()
            if line.startswith('#EXTM3U'):
                ext_data = {k: pat.search(line) for k, pat in EXTM3U_PATTERNS.items()}
            elif line.startswith('#EXTINF:'):
                chCount += 1
                match = {k: pat.search(line) for k, pat in EXTINF_PATTERNS.items()}

                # Dedup by tvg-id (matches _load:131-134).
                src_tvg_id = match['id'].group(1) if match.get('id') else None
                if src_tvg_id and src_tvg_id in seen_ids:
                    _log('parseExternalSource, filtering duplicate tvg-id %s' % src_tvg_id)
                    continue
                if src_tvg_id:
                    seen_ids.add(src_tvg_id)

                mitem = {
                    'id'                : '',
                    'number'            : chCount,
                    'name'              : '',
                    'logo'              : '',
                    'group'             : [],
                    'catchup'           : '',
                    'radio'             : False,
                    'favorite'          : False,
                    'realtime'          : False,
                    'media'             : '',
                    'label'             : '',
                    'url'               : '',
                    'tvg-shift'         : '',
                    'x-tvg-url'         : '',
                    'media-dir'         : '',
                    'media-size'        : '',
                    'catchup-source'    : '',
                    'catchup-days'      : '',
                    'catchup-correction': '',
                    'provider'          : '',
                    'provider-type'     : '',
                    'provider-logo'     : '',
                    'provider-countries': '',
                    'provider-languages': '',
                    'x-playlist-type'   : '',
                    'kodiprops'         : [],
                }

                for key, value in list(match.items()):
                    if value is None:
                        if ext_data.get(key) is not None:
                            value = ext_data[key]  # fall back to global EXTM3U attr
                        else:
                            continue
                    if value.group(1) is None:
                        continue
                    elif key == 'logo':
                        mitem[key] = value.group(1)
                    elif key == 'number':
                        try:    mitem[key] = int(value.group(1))
                        except (ValueError, TypeError):
                            try: mitem[key] = float(value.group(1))
                            except (ValueError, TypeError): pass
                    elif key == 'group':
                        mitem[key] = [g for g in sorted(set(value.group(1).split(';'))) if g]
                    elif key in ['radio', 'favorite', 'realtime', 'media']:
                        mitem[key] = value.group(1).lower() == 'true'
                    else:
                        mitem[key] = value.group(1)

                # Lookahead for URL line + EXTGRP/KODIPROP/EXTVLCOPT/WEBPROP/EXT-X-PLAYLIST-TYPE.
                # Mirrors _load:162-186. H7 fix: validate URL line is non-empty and
                # not a comment; skip entry on missing URL.
                for nidx in range(idx + 1, len(lines)):
                    nline = lines[nidx].rstrip()
                    if   nline.startswith('#EXTINF:'):
                        break
                    elif nline.startswith('#EXTGRP'):
                        m = re.search(r'^#EXTGRP:(.*)$', nline, re.IGNORECASE)
                        if m:
                            mitem['group'].extend(m.group(1).split(';'))
                            mitem['group'] = sorted(set(mitem['group']))
                    elif nline.startswith('#KODIPROP:'):
                        m = re.search(r'^#KODIPROP:(.*)$', nline, re.IGNORECASE)
                        if m: mitem.setdefault('kodiprops', []).append(m.group(1))
                    elif nline.startswith('#EXTVLCOPT'):
                        m = re.search(r'^#EXTVLCOPT:(.*)$', nline, re.IGNORECASE)
                        if m: mitem.setdefault('extvlcopt', []).append(m.group(1))
                    elif nline.startswith('#WEBPROP'):
                        m = re.search(r'^#WEBPROP:(.*)$', nline, re.IGNORECASE)
                        if m: mitem.setdefault('webprops', []).append(m.group(1))
                    elif nline.startswith('#EXT-X-PLAYLIST-TYPE'):
                        m = re.search(r'^#EXT-X-PLAYLIST-TYPE:(.*)$', nline, re.IGNORECASE)
                        if m: mitem['x-playlist-type'] = m.group(1)
                    elif nline.startswith('#') or nline.startswith('##') or not nline:
                        continue
                    else:
                        mitem['url'] = nline
                        break  # URL found; subsequent #EXTINF starts next entry

                # Fill missing with similar parameters (mirrors _load:189-191).
                mitem['name']  = mitem.get('name')  or mitem.get('label') or ''
                mitem['label'] = mitem.get('label') or mitem.get('name')  or ''
                mitem['id']    = src_tvg_id or ''

                # Skip incomplete entries (H7: missing URL is fatal for this entry).
                if not mitem['url']:
                    _log('parseExternalSource, skipping entry with no URL: name=%s' % mitem['name'])
                    continue
                # Skip entries missing name (id may be empty — caller falls back to URL hash).
                if not mitem['name']:
                    _log('parseExternalSource, skipping entry with no name')
                    continue

                out.append(mitem)

        return out


    # madteevee (imports fork, imports.12 / writer_lock): public `_save`
    # serializes via WRITER_LOCK so concurrent writers (Builder thread,
    # chkImports daemon) can't both os.replace pseudotv.m3u at the same
    # time. Pre-refactor, the second writer's content silently overwrote
    # the first (`xmltvs.py:81-85` documented this as "benign" but it
    # isn't — see the Custom-merge-from-disk workaround at imports.py:
    # 916-955). RLock so re-entry from the same thread is safe. A
    # WriterTimeout (lock holder wedged >30s) is treated as benign skip:
    # disk content preserved, next caller succeeds, the skip is logged
    # with ctx so the offender is identifiable.
    def _save(self):
        try:
            with held(ctx='M3U._save'):
                return self._saveLocked()
        except WriterTimeout:
            return False


    # madteevee (imports fork): @debounceit REMOVED. The trailing-Timer wrapper
    # was wedging Builder threads after `timer.start()`. Symptom: Builder logs
    # `_save waiting (5.0)` and then goes silent for the rest of the session;
    # `state = any([m3u._save(), xmltv._save()])` in builder.__setStation never
    # advances to xmltv._save(). Observed twice on 2026-05-11 (Late Night
    # 06:38:51 and SleepyTV 07:13:03). The wrapper has no `return` so callers
    # got None instead of True/False, and the 5s Timer window was an extra
    # serialization point with no benefit now that writes are atomic and
    # FileLock-free. Inline call returns immediately on the calling thread.
    #
    # imports.13 / C#10 Follow-up A: render+write delegated to the
    # module-level `render_m3u` + `write_atomic` in renderers.py. This
    # eliminates the ~140-line duplicated writer body that used to mirror
    # tasks._renderM3U. The two paths had drifted (attribute escaping,
    # label sanitization, webprops emit) — consolidating means Builder's
    # M3U output now picks up the same safety improvements chkImports
    # was already producing. See the plan at
    # /home/madalone/.claude/plans/misty-shimmying-liskov.md.
    def _saveLocked(self):
        if not self.writable:
            return False
        # Header side-effect preserved for downstream M3UDATA readers
        # (the rendered file uses render_m3u's own header — this just
        # keeps the stored field in sync).
        self.M3UDATA['data'] = '#EXTM3U tvg-shift="" x-tvg-url="%s" x-tvg-id="" catchup-correction=""' % (
            'http://%s/%s' % (PROPERTIES.getRemoteHost(), XMLTVFLE),)
        # In-place sortStations preserved — getStations / getRecordings
        # readers rely on these being ordered. Not load-bearing for the
        # render itself (renderer does its own combined-sort-by-number).
        self.M3UDATA['stations']   = self.sortStations(self.M3UDATA.get('stations',   []))
        self.M3UDATA['recordings'] = self.sortStations(self.M3UDATA.get('recordings', []), key='name')
        # Function-local import: avoids module-load circular dependency
        # (renderers.py imports `from m3u import M3U_TEMP` inside its
        # body, so m3u.py must finish loading before renderers' first
        # use). By the time _saveLocked is called, both modules are loaded.
        from renderers import render_m3u, write_atomic
        rendered = render_m3u(self.M3UDATA)
        if rendered is None:
            # Empty-data guard fired inside render_m3u; preserve disk content.
            return False
        self.log('_save, writable = %s, file = %s, stations = %s, recordings = %s' % (
            self.writable, self.stationFile, len(self.M3UDATA.get('stations') or []), len(self.M3UDATA.get('recordings') or []),
        ))
        write_atomic(self.stationFile, rendered)
        return True
        
        
    def _reload(self):
        self.log('_reload') 
        self.__init__()
        
        
    def _verify(self, stations=[], recordings=[], chkPath=SETTINGS.getSettingBool('Clean_Recordings')):
        if stations: #remove abandoned m3u entries; Stations that are not found in the channel list
            channels = Channels().getChannels()
            stations = [station for station in stations for channel in channels if channel.get('id') == station.get('id',str(random.random()))] 
            self.log('_verify, stations = %s'%(len(stations)))
            return stations
        elif recordings:#remove recordings that no longer exists on disk
            if chkPath: recordings = [recording for recording in recordings if hasFile(FileAccess._decodeString(dict(urllib.parse.parse_qsl(recording.get('url',''))).get('vid').replace('.pvr','')))]
            else:       recordings = [recording for recording in recordings if recording.get('media',False)]
            self.log('_verify, recordings = %s, chkPath = %s'%(len(recordings),chkPath))
            return recordings
        return []
        

    def cleanSelf(self, items, key='id', slug='@%s'%(PSEUDOTV_SLUG)): # remove m3u imports (Non PseudoTV Live)
        if not slug: return items
        # madteevee (imports fork): ownership check now lives in globals.isOwnedID
        # so this writer and xmltvs.cleanSelf can't drift on what "ours" means.
        # Stations vs recordings is split by the `media` flag (M3U convention);
        # ID format (32-char channel hash vs 16-char recording hash) doesn't
        # enter the picture here.
        try:
            from channels import Channels as _C
            import_slugs = {'@%s' % (i.get('id'),) for i in (_C().getImports() or []) if i.get('id')}
        except Exception:
            import_slugs = set()
        def _id_ok(s):
            return isOwnedID(s, slug, import_slugs)
        stations   = self.sortStations(self._verify(stations=[s for s in items if _id_ok(s.get(key,'')) and not s.get('media',False)]))
        recordings = self.sortStations(self._verify(recordings=[r for r in items if _id_ok(r.get(key,'')) and r.get('media',False)]), key='name')
        self.log('cleanSelf, slug = %s, import_slugs = %s, key = %s: stations = %s, recordings = %s'%(slug,sorted(import_slugs),key,len(stations),len(recordings)))
        return stations, recordings


    def sortStations(self, stations, key='number'):
        try:              return sorted(stations, key=itemgetter(key))
        except Exception: return stations
        
        
    def getM3U(self):
        return self.M3UDATA
        
        
    def getMitem(self):
        return M3U_TEMP.copy()
        
        
    def getTZShift(self):
        self.log('getTZShift')
        return ((time.mktime(time.localtime()) - time.mktime(time.gmtime())) / 60 / 60)

    
    def getStations(self):
        stations = self.sortStations(self.M3UDATA.get('stations',[]))
        self.log('getStations, stations = %s'%(len(stations)))
        return stations
              
              
    def getRecordings(self):
        recordings = self.sortStations(self.M3UDATA.get('recordings',[]), key='name')
        self.log('getRecordings, recordings = %s'%(len(recordings)))
        return recordings
               
               
    def getStationItem(self, sitem):
        if 3000 in list(sitem.get('rules',{}).keys()): #PauseRule
               sitem['url'] = RESUME_URL.format(addon=ADDON_ID,name=Globals._quoteString(sitem['name']),chid=Globals._quoteString(sitem['id']))
        elif   sitem['radio']: sitem['url'] = RADIO_URL.format(addon=ADDON_ID,name=Globals._quoteString(sitem['name']),chid=Globals._quoteString(sitem['id']),radio=str(sitem['radio']),vid='{catchup-id}')
        elif   sitem['catchup']:
               sitem['catchup-source'] = BROADCAST_URL.format(addon=ADDON_ID,name=Globals._quoteString(sitem['name']),chid=Globals._quoteString(sitem['id']),vid='{catchup-id}')
               sitem['url'] = LIVE_URL.format(addon=ADDON_ID,name=Globals._quoteString(sitem['name']),chid=Globals._quoteString(sitem['id']),vid='{catchup-id}',now='{lutc}',start='{utc}',duration='{duration}',stop='{utcend}')
        else:  sitem['url'] = TV_URL.format(addon=ADDON_ID,name=Globals._quoteString(sitem['name']),chid=Globals._quoteString(sitem['id']))
        return sitem
    
    
    def getRecordItem(self, fitem, seek=0):
        if seek <= 0: group = LANGUAGE(30119)
        else:         group = LANGUAGE(30152)
        ritem = self.getMitem()
        ritem['provider']      = '%s (%s)'%(ADDON_NAME,PROPERTIES.getFriendlyName())
        ritem['provider-type'] = 'addon'
        ritem['provider-logo'] = LOGO_HOST
        ritem['label']         = (fitem.get('showlabel') or '%s%s'%(fitem.get('label',''),' - %s'%(fitem.get('episodelabel','')) if fitem.get('episodelabel','') else ''))
        ritem['name']          = ritem['label']
        ritem['number']        = random.Random(str(fitem.get('id',1))).random()
        ritem['logo']          = Globals._getThumb(fitem,opt=EPG_ARTWORK)
        ritem['media']         = True
        ritem['media-size']    = str(fitem.get('size',0))
        ritem['media-dir']     = ''#todo optional add parent directory via user prompt?
        ritem['group']         = ['%s (%s)'%(group,ADDON_NAME)]
        ritem['id']            = getRecordID(ritem['name'], (fitem.get('originalfile') or fitem.get('file','')), ritem['number'], SETTINGS.getMYUUID())
        ritem['url']           = DVR_URL.format(addon=ADDON_ID,title=Globals._quoteString(ritem['label']),chid=Globals._quoteString(ritem['id']),vid=(FileAccess._encodeString((fitem.get('originalfile') or fitem.get('file','')))),seek=seek,duration=fitem.get('duration',0))#fitem.get('catchup-id','')
        return ritem
        
        
    def delStation(self, citem):
        try: 
            self.M3UDATA['stations'].pop(self.findStation(citem)[0])
            self.log('[%s] delStation, channel deleted!'%(citem['id']), xbmc.LOGINFO)
            return True
        except Exception: pass
        

    def delRecording(self, ritem):
        try: 
            self.M3UDATA['recordings'].pop(self.findRecording(ritem)[0])
            self.log('[%s] delRecording, channel deleted!'%(ritem['id']), xbmc.LOGINFO)
            return True
        except Exception: pass
            
            
    def addStation(self, citem):
        self.log('addStation, citem:\n%s'%(citem))
        mitem = self.getMitem()
        mitem.update(citem)            
        mitem['label']         = citem['name'] #todo channel manager opt to change channel 'label' leaving 'name' static for channelid purposes
        mitem['logo']          = citem['logo']
        mitem['realtime']      = False
        mitem['provider']      = '%s (%s)'%(ADDON_NAME,PROPERTIES.getFriendlyName())
        mitem['provider-type'] = 'addon'
        mitem['provider-logo'] = LOGO_HOST
        self.log('addStation, mitem:\n%s'%(mitem))
        self.delStation(citem)
        self.M3UDATA.setdefault('stations',[]).append(mitem)
        self.log('addStation, [%s] adding channel %s'%(citem["id"],citem["name"]), xbmc.LOGINFO)
        return True
        
        
    def addRecording(self, ritem):
        # https://github.com/kodi-pvr/pvr.iptvsimple/blob/Omega/README.md#media
        self.delRecording(ritem)
        self.M3UDATA.setdefault('recordings',[]).append(ritem)
        self.log('addRecording, [%s] adding recording %s'%(ritem["id"],ritem["name"]), xbmc.LOGINFO)
        return True
        
        
    def findStation(self, citem):
        # madteevee (imports fork): match by id ONLY. Upstream's "id OR url"
        # was fine in single-instance PseudoTV, but in the imports fork
        # multiple imports can legitimately share a source URL — e.g.,
        # CNN@movistarplus and CNNInternational.us@autonomiques both stream
        # from the same movistarplus plugin URL (Movistar+ delivers both;
        # autonomiques re-uses Movistar+'s plugin path). With the URL
        # match, addStation for the second one's delStation evicted the
        # first — the channel that called addStation LAST won, and the
        # other's record silently vanished from M3UDATA before _save.
        # Symptom: channels.json has both, pseudotv.m3u has only one,
        # Kodi PVR sees only one. Our @<import_id> namespacing makes ids
        # unique, so id-only match is sufficient AND correct.
        def __match(eitem):
            return citem.get('id', str(random.random())) == eitem.get('id')
        return tuple(next(((idx, eitem) for idx, eitem in enumerate(self.M3UDATA.get('stations',[])) if __match(eitem)),(None, {})))
        
        
    def findRecording(self, ritem):
        def __match(eitem):
            return (ritem.get('id',str(random.random())) == eitem.get('id')) or (ritem.get('label',str(random.random())).lower() == eitem.get('label','').lower()) or (ritem.get('path',str(random.random())).endswith('%s.pvr'%(eitem.get('name'))))
        return tuple(next(((idx, eitem) for idx, eitem in enumerate(self.M3UDATA.get('recordings',[])) if __match(eitem)),(None, {})))
        
        