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

from globals    import *
from multiroom  import Multiroom

#todo create dataclasses for all jsons
# https://pypi.org/project/dataclasses-json/
class Channels(object):
    
    def __init__(self, file=None, writable=False):
        if file is None: file = self._getCHANNELFLE()
        self.writable    = writable
        self.channelFile = file
        self.channelDATA = FileAccess.getJSON(CHANNELFLE_DEFAULT)
        self.channelTEMP = self.channelDATA.get('channels',[{}]).pop(0)
        self.channelRULE = self.channelTEMP.pop('rules')
        self.channelTEMP['rules'] = {}
        self.channelDATA.update(self._load())
        # Refresh Open_Manager count NOW that self.channelDATA has the loaded channels.
        # _load() called _setSetting() before this update — at that point self.channelDATA
        # was still the post-pop empty template, so the setting always wrote "0 Channels"
        # regardless of how many channels the file actually held.
        self._setSetting()
        self.channelDATA_OLD = self.channelDATA.copy()
        
        
    def log(self, msg, level=xbmc.LOGDEBUG):
        return log('%s: %s'%(self.__class__.__name__,msg),level)


    def _getCHANNELFLE(self):
        if SETTINGS.getSettingBool('Enable_Autotuned_Channels'): return AUTOTUNEFLEPATH
        return CHANNELFLEPATH
        
        
    def _load(self) -> dict:
        channelDATA = FileAccess.getJSON(self.channelFile)
        self.log('_load, file = %s\nchannels = %s'%(self.channelFile,len(channelDATA.get('channels',[]))))
        # imports fork (madteevee): one-time migration of vestigial `imports`
        # schema to the live-imports v0.8 shape (list of import-config dicts).
        # The original schema was `imports:[{vod:[...],live:[...]}]` — getter
        # exists in this class but no consumer ever read it; safe to clear.
        if self._needsImportsMigration(channelDATA):
            self._migrateImports(channelDATA)
        return channelDATA

    @staticmethod
    def _needsImportsMigration(data: dict) -> bool:
        """Detect vestigial imports schema vs new v0.8 shape."""
        imports = data.get('imports')
        if not isinstance(imports, list) or not imports:
            return False
        first = imports[0]
        if not isinstance(first, dict):
            return False
        # New shape: each entry has an 'id' key. Vestigial: has 'vod' or 'live' keys.
        return 'id' not in first and ('vod' in first or 'live' in first)

    def _migrateImports(self, data: dict) -> None:
        """Backup channels.json then rewrite `imports` to the v0.8 shape."""
        # Atomic-write backup (plan G10) — best-effort. Use os.replace so a
        # crash mid-backup leaves no half-written file.
        try:
            real_path = FileAccess.translatePath(self.channelFile)
            if os.path.exists(real_path):
                bak = real_path + '.pre-imports.bak'
                tmp = bak + '.tmp'
                with open(real_path, 'rb') as src, open(tmp, 'wb') as dst:
                    dst.write(src.read())
                    dst.flush()
                    try: os.fsync(dst.fileno())
                    except OSError: pass
                os.replace(tmp, bak)
                self.log('_migrateImports, backup written to %s' % bak, xbmc.LOGINFO)
        except Exception as e:
            self.log('_migrateImports, backup failed (continuing): %s' % e, xbmc.LOGWARNING)
        # Replace vestigial schema with empty list
        data['imports'] = []
        self.log('_migrateImports, vestigial imports schema cleared (v0.8 shape installed)',
                 xbmc.LOGINFO)
    
        
    def _verify(self, channels: list=[]):
        for idx, citem in enumerate(self.channelDATA.get('channels',[])):
            if not citem.get('name') or not citem.get('id') or len(citem.get('path',[])) == 0:
                self.log('_verify, in-valid citem [%s]\n%s'%(citem.get('id'),citem))
                continue
            yield citem
                
                
    def _save(self) -> bool:
        self.log('_save, writable = %s, file = %s\nchannels = %s'%(self.writable,self.channelFile,len(self.channelDATA['channels'])))
        if self.writable:
            with PROPERTIES.interruptActivity():
                if FileAccess.setJSON(self.channelFile,self.channelDATA):
                    self._setSetting()
                    return True
        
        
    def _setSetting(self):
        if self.channelFile == CHANNELFLEPATH:
            SETTINGS.setSetting('Open_Manager','[B]%s[/B] Channels'%(len(self.channelDATA['channels'])))
        
        
    def getTemplate(self) -> dict: 
        return self.channelTEMP.copy()
        
        
    def getChannels(self) -> list:
        return sorted(self.channelDATA['channels'], key=itemgetter('number'))
        
        
    def getChannelbyID(self, id: str) -> list:
        return list([c for c in self.channelDATA['channels'] if c.get('id') == id])
        
        
    def getChannelbyType(self, type: str):
        return list([citem for citem in self.channelDATA['channels'] if citem.get('type') == type])


    def sortChannels(self, channels: list) -> list:
        try:              return sorted(channels, key=itemgetter('number'))
        except Exception: return channels

    
    def setChannels(self, channels=None, modified_ids=None, deleted_ids=None) -> bool:
        if channels is None: channels = self.channelDATA['channels']
        if modified_ids is not None and self.writable:
            # v.43 merge-on-write: Builder.channels is a class-level shared instance loaded
            # once at class-definition time. addChannel() refreshes only the channels the
            # current build batch touched; all other channels carry stale init-time state.
            # A bare _save() would clobber any disk edits to those untouched channels
            # (notably operator-flipped `changed:true` used to force a rebuild). Re-read
            # disk and preserve untouched channels here.
            # madteevee (imports fork): `deleted_ids` is the explicit-delete set —
            # callers (Imports.syncAll for tombstoned channels) pass channel ids that
            # MUST be removed even if still on disk. Without this, the disk-only
            # preserve branch (the second loop below) silently resurrects a channel
            # the operator just tombstoned via the "🗑 Orphans" delete endpoint.
            deleted = set(deleted_ids or [])
            try:
                disk_channels = {c.get('id'): c for c in (self._load().get('channels',[]) or [])}
                in_memory_ids = {c.get('id') for c in channels}
                merged = []
                for c in channels:
                    cid = c.get('id')
                    if cid in deleted:
                        continue                      # explicit delete wins
                    if cid in modified_ids or cid not in disk_channels:
                        merged.append(c)              # in-memory authoritative (touched, or new)
                    else:
                        merged.append(disk_channels[cid])  # disk authoritative (untouched)
                for cid, c in disk_channels.items():
                    if cid in deleted:
                        continue                      # explicit delete wins over disk-preserve
                    if cid not in in_memory_ids:
                        merged.append(c)              # disk-only (preserve; Builder doesn't delete)
                if deleted:
                    self.log('setChannels, applied %d explicit deletes'%(len(deleted)), xbmc.LOGINFO)
                channels = merged
            except Exception as e:
                self.log('setChannels, modified_ids merge failed, falling back to direct write: %s'%(e), xbmc.LOGWARNING)
        elif self.writable:
            # imports fork (madteevee): defensive merge for callers that don't pass
            # modified_ids — most importantly the Channel Manager UI (manager.py:1032
            # calls setChannels(channels) with only the Custom-tab subset). Without this
            # guard, saving a Custom-channel edit silently wipes the entire `type='import'`
            # set from channels.json (verified empirically: 91 movistarplus channels lost
            # on a single Cartoon Cartoons edit). Preserve disk-only import-type channels;
            # in-memory list still wins for everything it contains.
            try:
                disk_channels = {c.get('id'): c for c in (self._load().get('channels',[]) or []) if c.get('id')}
                in_memory_ids = {c.get('id') for c in channels if c.get('id')}
                preserved = [c for cid, c in disk_channels.items()
                             if cid not in in_memory_ids and c.get('type') == 'import']
                if preserved:
                    channels = list(channels) + preserved
                    self.log('setChannels, preserved %d disk-only import channels (caller passed no modified_ids)'
                             % len(preserved), xbmc.LOGINFO)
            except Exception as e:
                self.log('setChannels, defensive import-preserve failed: %s'%(e), xbmc.LOGWARNING)
        self.channelDATA['uuid']     = SETTINGS.getMYUUID()
        self.channelDATA['channels'] = self.sortChannels(channels)
        PROPERTIES.setHasChannels(len(channels)>0)
        return self._save()
        

    def getImports(self) -> list:
        return self.channelDATA.get('imports',[])
        
        
    def setImports(self, data: list=[]) -> bool:
        # Diff prev vs new import-id sets to drop everything tied to a
        # removed import:
        #   1. Per-import EPG cache files (`CACHE_LOC/epg_<id>.xml`,
        #      written by `Imports._epgCacheWrite`).
        #   2. Channel records with `type='import'` and matching
        #      `import_source`. Without this, deleting an import via the
        #      imports.html UI left every channel behind — they kept
        #      claiming channel numbers, polluted pseudotv.m3u/xml, and
        #      confused renumber's cross-import blocked-slot calc. The
        #      operator had to manually run `cleanup_stranded` to recover.
        # Disabled imports keep their caches AND channels — only explicit
        # removal frees them.
        prev_ids = {(imp or {}).get('id') for imp in (self.channelDATA.get('imports') or [])}
        new_ids  = {(imp or {}).get('id') for imp in (data or [])}
        removed_ids = {iid for iid in (prev_ids - new_ids) if iid}

        # Drop the channels for any removed import BEFORE updating the
        # imports list. We mutate channelDATA['channels'] in place so the
        # subsequent _save persists the cleaned channel list along with
        # the new imports list — single atomic write, no extra _save call.
        purged_channels = 0
        if removed_ids:
            old = self.channelDATA.get('channels') or []
            new_channels = [c for c in old
                            if not (c.get('type') == 'import'
                                    and c.get('import_source') in removed_ids)]
            purged_channels = len(old) - len(new_channels)
            if purged_channels:
                self.channelDATA['channels'] = new_channels
                self.log('setImports, purged %d channels from removed imports: %s'
                         % (purged_channels, sorted(removed_ids)), xbmc.LOGINFO)

        self.channelDATA['imports'] = data
        ok = self._save()
        for iid in removed_ids:
            # CACHE_LOC may be a special:// URI at runtime — translate before os.*
            cache_path = FileAccess.translatePath(os.path.join(CACHE_LOC, 'epg_%s.xml' % iid))
            try:
                if os.path.exists(cache_path):
                    os.unlink(cache_path)
                    self.log('setImports, [%s] removed orphan EPG cache: %s'
                             % (iid, cache_path), xbmc.LOGINFO)
            except Exception as e:
                self.log('setImports, [%s] failed to remove EPG cache %s: %s'
                         % (iid, cache_path, e), xbmc.LOGWARNING)
        return ok
         
         
    def clrChannels(self):
        self.channelDATA['channels'] = []
        

    def delChannel(self, citem: dict={}) -> bool:
        if isinstance(citem,list): return any([self.delChannel(channel) for channel in citem])
        try: 
            self.channelDATA['channels'].pop(self.findChannel(citem)[0])
            self.log('[%s] delChannel, channel deleted!'%(citem['id']), xbmc.LOGINFO)
            return True
        except Exception: pass
    
    
    def addChannel(self, citem: dict={}) -> bool:
        if isinstance(citem,list): return any([self.addChannel(channel) for channel in citem])
        self.delChannel(citem)
        self.log('addChannel, [%s] adding channel %s'%(citem["id"],citem["name"]), xbmc.LOGINFO)
        self.channelDATA.setdefault('channels',[]).append(Globals._cleanGroups(citem))
        return True
        
        
    def findChannel(self, citem: dict={}, channels=None) -> tuple:
        if channels is None: channels = self.channelDATA['channels']
        if citem.get('id') is None: citem['id'] = getChannelID(citem.get('name'), citem.get('path'), citem.get('number'), uuid=self.channelDATA.get('uuid'))
        return tuple(next(((idx, eitem) for idx, eitem in enumerate(channels) if citem['id'] == eitem.get('id', str(random.random()))), (None,{})))
                    
                    
    def _channelManager(self):
        with FileAccess.stream(MANAGERPATH, "r") as fle:
            html_content = fle.read()
        html_content = html_content.replace("{{ channel_limit }}", str(CHANNEL_LIMIT))
        html_content = html_content.replace("{{ media_loc }}"    , MEDIA_LOC)
        html_content = html_content.replace("{{ remote_host }}"  , PROPERTIES.getRemoteHost())
        # Step 6 Phase 2 (madteevee): Settings tab posts to /api/settings/save.json
        # which goes through the existing do_POST __verifyUUID gate — UI needs the uuid.
        html_content = html_content.replace("{{ uuid }}"         , SETTINGS.getMYUUID())
        return html_content.encode(encoding=DEFAULT_ENCODING)
        