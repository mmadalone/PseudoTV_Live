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

import json               # madteevee (imports fork, imports.69): _readFresh's strict parse (FileAccess.loadJSON hides errors)
import channels_lock      # madteevee (imports fork, imports.69): the lock around every channels.json write
import importcfg_helpers  # madteevee (imports fork, imports.69): import-entry merges (dashboard save, sync commit)


# madteevee (imports fork, imports.69): the outcomes of Channels.commit. Every
# channels.json write of the service goes through commit: a strict fresh read
# of the file, the caller's merge onto it and the atomic write, all under
# channels_lock (see that module for the lock contract). Before imports.69 a
# writer saved the copy it had loaded earlier, so a dashboard Save wiped every
# import's sync info ("never synced") and a running sync wrote back the
# import list it read before the Save, undoing it.
COMMIT_WRITTEN    = 'written'
COMMIT_NOTHING    = 'nothing to write'   # the transform found nothing to change (or refused): no write
COMMIT_UNREADABLE = 'unreadable'         # channels.json exists but can't be read or parsed: no write
COMMIT_MISSING    = 'missing'            # no channels.json and a background writer: no write
COMMIT_BUSY       = 'busy'               # CHANNELS_LOCK not had within its timeout: no write
COMMIT_FAILED     = 'failed'             # read-only copy, or the write itself failed
READ_OK           = 'ok'                 # _readFresh: the file was read and parsed


# imports.20: operator_overrides helpers. The channel record carries a
# sorted list of field-name strings recording which fields the operator
# manually set (vs. the auto-derived defaults). Builder._verify and
# Manager.setLogo respect this list to avoid silently overwriting an
# operator's pick. Module-level (not Channels-method) because callers
# operate on a single citem dict and don't need a Channels() instance —
# which would re-load channels.json on every edit.
#
# Idempotent (set semantics). Output is sorted to match the existing
# inline pattern at server.py:847-851, 1005-1007, 1166-1168 so future
# greps for `operator_overrides` keep matching everywhere.
def markOverrides(citem: dict, *keys: str) -> list:
    """Add keys to citem['operator_overrides']. Returns the new sorted list."""
    overrides = set(citem.get('operator_overrides') or [])
    overrides.update(keys)
    citem['operator_overrides'] = sorted(overrides)
    return citem['operator_overrides']


def unmarkOverrides(citem: dict, *keys: str) -> list:
    """Remove keys from citem['operator_overrides']. No-op on missing keys."""
    overrides = set(citem.get('operator_overrides') or [])
    overrides.difference_update(keys)
    citem['operator_overrides'] = sorted(overrides)
    return citem['operator_overrides']


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
        loaded = self._load()
        self.channelDATA.update(loaded)
        # imports.69: did this copy load real content? getJSON returns {} for a
        # missing or unreadable file, and the copy then holds the template (no
        # channels). A whole-list writer built on such a copy must not replace
        # the file once it is readable again (setChannels' user role, _save).
        # commit sets it after its first write.
        self._fromFile = bool(loaded)
        # imports.69: never expose the template's old-shape import entry
        # (remotes/channels.json ships imports [{vod, live}] without an id).
        # It stays in channelDATA whenever the file is missing, unreadable or
        # has no import list, and then reached getImports(): a sync, the guide
        # and playlist loads, and every write. XMLTVS.cleanSelf's fail-open
        # ("no channels and no imports") never fired for it, and a later
        # import save could purge every real import against it.
        if self._needsImportsMigration(self.channelDATA): self.channelDATA['imports'] = []
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
                
                
    def _readFresh(self):
        """imports.69: the strict read of channels.json for a commit.
        Returns (status, doc):
          (READ_OK, doc)               parsed; 'channels' and 'imports' are
                                       lists (a missing key becomes []), and
                                       the old-shape import list is migrated
                                       as _load does;
          (COMMIT_MISSING, None)       there is no file;
          (COMMIT_UNREADABLE, reason)  any read, decode or JSON error, a top
                                       level that is not an object, or a
                                       'channels'/'imports' that is not a list.
        Unlike _load it never uses getJSON (which returns {} for every
        failure, so an unreadable file looked like an empty one and the next
        save wrote the copy in memory over it) and never the template. Every
        call parses anew, so the doc is private to the caller."""
        try:
            real = FileAccess._localPath(self.channelFile)
            if real is not None:
                try:
                    with open(real, 'rb') as fle:
                        raw = fle.read()
                except FileNotFoundError:
                    return COMMIT_MISSING, None
            else:
                # smb:// / nfs:// user folders stay on Kodi's VFS (setJSON does the same)
                if not FileAccess.exists(self.channelFile):
                    return COMMIT_MISSING, None
                with FileAccess.stream(self.channelFile, 'r') as fle:
                    raw = fle.read()
            if isinstance(raw, (bytes, bytearray)):
                raw = bytes(raw).decode(DEFAULT_ENCODING)
            doc = json.loads(raw)
        except Exception as e:
            return COMMIT_UNREADABLE, '%s: %s' % (e.__class__.__name__, e)
        if not isinstance(doc, dict):
            return COMMIT_UNREADABLE, 'the top level is a %s, not an object' % (type(doc).__name__,)
        for key in ('channels', 'imports'):
            if key not in doc:
                doc[key] = []
            elif not isinstance(doc[key], list):
                return COMMIT_UNREADABLE, "'%s' is a %s, not a list" % (key, type(doc[key]).__name__)
        if self._needsImportsMigration(doc):
            self._migrateImports(doc)
        return READ_OK, doc


    def _memoryDoc(self) -> dict:
        """imports.69: this copy's own data as a document. A user writer that
        finds no channels.json (first install, or the file deleted while a
        request ran) writes from it, as every save did before."""
        return {'uuid'    : self.channelDATA.get('uuid', ''),
                'channels': list(self.channelDATA.get('channels') or []),
                'imports' : list(self.channelDATA.get('imports') or [])}


    def commit(self, transform, ctx='', background=False, sweep_caches=False) -> str:
        """imports.69: THE channels.json write. Under PROPERTIES.interruptActivity
        (as every save before) and channels_lock: a strict fresh read
        (_readFresh), `new = transform(doc, found)`, the atomic write.

        transform(doc, found) gets a private parse of the file (found False:
        no file, doc = {'uuid': '', 'channels': [], 'imports': []}) and
        returns the whole document to write, or None for "nothing to write".
        It runs inside the lock, so it must not open dialogs, call JSON-RPC,
        download or render; callers compute SETTINGS.getMYUUID() before
        (it can open the "name this device" dialog). An exception it raises
        propagates and nothing is written.

        background=True (the sync's commit) refuses a missing file instead of
        creating one. sweep_caches=True (the import-list writers) removes the
        guide caches of imports the written list no longer holds, right after
        the write and still inside the lock.

        Returns a COMMIT_* outcome. Only COMMIT_WRITTEN wrote: this copy then
        holds what was written, counts as loaded from the file, and the
        Open_Manager count and the "has channels" window property follow the
        written channel list. A write failure is COMMIT_FAILED, never an
        exception."""
        if not self.writable:
            self.log('commit [%s], this copy is read-only -- nothing written' % (ctx,))
            return COMMIT_FAILED
        with PROPERTIES.interruptActivity():
            try:
                with channels_lock.held(ctx):
                    status, doc = self._readFresh()
                    if status == COMMIT_UNREADABLE:
                        self.log("commit [%s], channels.json can't be read (%s) -- nothing written" % (ctx, doc), xbmc.LOGERROR)
                        return COMMIT_UNREADABLE
                    found = status != COMMIT_MISSING
                    if not found:
                        if background:
                            self.log('commit [%s], channels.json is missing -- nothing written' % (ctx,), xbmc.LOGWARNING)
                            return COMMIT_MISSING
                        doc = {'uuid': '', 'channels': [], 'imports': []}
                    new = transform(doc, found)
                    if new is None:
                        return COMMIT_NOTHING
                    # FileAccess.setJSON returns False for the failures it
                    # catches, but its FileLock raises on a timeout or an open
                    # failure: both are a failed save, never an exception out
                    # of a writer (Builder's save sits outside its per-channel try).
                    try:
                        ok, error = FileAccess.setJSON(self.channelFile, new), None
                    except Exception as e:
                        ok, error = False, e
                    if not ok:
                        self.log('commit [%s], channels.json write failed (%s) -- nothing written'
                                 % (ctx, error if error is not None else 'setJSON returned False'), xbmc.LOGERROR)
                        return COMMIT_FAILED
                    self.channelDATA.update(new)   # this copy now holds what was written
                    self._fromFile = True          # ...so its later whole-list saves are not refused
                    if sweep_caches:
                        self._sweepGuideCaches(new.get('imports') or [])
            except channels_lock.ChannelsLockTimeout:
                return COMMIT_BUSY
        # Outside the lock: _setSetting is a Kodi setting write (only when the
        # count changed). "Has channels" was set only by setChannels before;
        # the sync now saves through commitSync, and autotune's first-run
        # prompt and chkChannels' batching read it.
        try:
            self._setSetting()
            PROPERTIES.setHasChannels(len(self.channelDATA.get('channels') or []) > 0)
        except Exception as e:
            self.log('commit [%s], written; the follow-up failed: %s' % (ctx, e), xbmc.LOGWARNING)
        return COMMIT_WRITTEN


    def _save(self) -> bool:
        # imports.69: kept only for the two whole-list bypasses in server.py
        # (/channels/rebuild.json, /imports/cleanup_stranded.json). It writes
        # this copy's channel list and uuid; the import list (and any other
        # key) comes from the file: the import writers own the import list.
        # With no file it writes this copy's import list (a file that vanished
        # while the request ran must not come back with `imports: []`).
        self.log('_save, writable = %s, file = %s\nchannels = %s'%(self.writable,self.channelFile,len(self.channelDATA['channels'])))
        if not self.writable: return False
        channels = list(self.channelDATA.get('channels') or [])
        def _transform(doc, found):
            if found and not self._fromFile:
                # A copy that never read the file (missing or unreadable when
                # it was built) must not replace the whole channel list.
                self.log('_save, this copy never loaded channels.json -- nothing written', xbmc.LOGERROR)
                return None
            new = dict(doc)
            if 'uuid' in self.channelDATA: new['uuid'] = self.channelDATA['uuid']
            new['channels'] = channels
            new['imports']  = doc['imports'] if found else list(self.channelDATA.get('imports') or [])
            return new
        return self.commit(_transform, ctx='_save') == COMMIT_WRITTEN


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

    
    @staticmethod
    def _mergeRecords(disk_channels, channels, modified_ids=None, deleted_ids=None, log=None, ctx='setChannels') -> list:
        """imports.69: the record-level merge, moved here from setChannels'
        two branches (which re-read the file themselves) so that setChannels
        and commitSync merge onto commit's fresh read. It is the single plug
        point that imports.71's per-channel merge replaces.

        disk_channels: the records in channels.json now, or None when there
        is no file. channels: the writer's list. modified_ids given =
        background role, None = user role. Returns the merged list (not yet
        sorted). `log(msg, level)` is optional; ctx names the writer in its
        lines (setChannels or commitSync).

        One rule is new in the background role (moved here from imports.71):
        a record missing from the file is re-created only if this writer
        touched it (its id is in modified_ids). Before, every record of the
        writer's copy that was gone from the file counted as "new" and was
        written back: Builder's session copy re-created a deleted import's
        channels, removed Orphans and Custom channels deleted since Kodi
        started, and a running sync re-created a Custom channel deleted while
        it ran. With no file at all nothing was deleted from it, so the
        writer's whole list is kept (as before)."""
        def _log(msg, level):
            if log is not None: log(msg, level)
        if modified_ids is not None:
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
            on_disk = disk_channels is not None
            disk = {c.get('id'): c for c in (disk_channels or [])}
            in_memory_ids = {c.get('id') for c in channels}
            merged, not_recreated = [], []
            for c in channels:
                cid = c.get('id')
                if cid in deleted:
                    continue                      # explicit delete wins
                if cid in modified_ids or not on_disk:
                    merged.append(c)              # in-memory authoritative (touched, or no file at all)
                elif cid in disk:
                    merged.append(disk[cid])      # disk authoritative (untouched)
                else:
                    not_recreated.append(cid)     # imports.69: deleted on disk since this copy loaded -- stays deleted
            for cid, c in disk.items():
                if cid in deleted:
                    continue                      # explicit delete wins over disk-preserve
                if cid not in in_memory_ids:
                    merged.append(c)              # disk-only (preserve; Builder doesn't delete)
            # imports.69: count the deletes that removed a record (before: the
            # size of the delete set, so every sync logged every tombstone).
            applied = deleted.intersection(in_memory_ids.union(disk))
            if applied:
                _log('%s, applied %d explicit deletes' % (ctx, len(applied)), xbmc.LOGINFO)
            if not_recreated:
                _log('%s, %d record(s) deleted from channels.json since this copy was loaded were not written back: %s'
                     % (ctx, len(not_recreated), sorted(str(cid) for cid in not_recreated)), xbmc.LOGINFO)
            return merged
        # imports fork (madteevee): defensive merge for callers that don't pass
        # modified_ids — most importantly the Channel Manager UI (manager.py:1032
        # calls setChannels(channels) with only the Custom-tab subset). Without this
        # guard, saving a Custom-channel edit silently wipes the entire `type='import'`
        # set from channels.json (verified empirically: 91 movistarplus channels lost
        # on a single Cartoon Cartoons edit). Preserve disk-only import-type channels;
        # in-memory list still wins for everything it contains.
        if disk_channels is None:
            return list(channels)
        disk = {c.get('id'): c for c in disk_channels if c.get('id')}
        in_memory_ids = {c.get('id') for c in channels if c.get('id')}
        preserved = [c for cid, c in disk.items()
                     if cid not in in_memory_ids and c.get('type') == 'import']
        if preserved:
            _log('%s, preserved %d disk-only import channels (caller passed no modified_ids)' % (ctx, len(preserved)), xbmc.LOGINFO)
            return list(channels) + preserved
        return list(channels)


    def setChannels(self, channels=None, modified_ids=None, deleted_ids=None) -> bool:
        """Save a channel list (same signature as before imports.69).

        modified_ids given = background role (Builder's save, the flag
        clears); None = user role (the Channel Manager, the channel
        endpoints, POST /channels.json). The records are merged onto the file
        as it is now (_mergeRecords) and the import list is the file's: only
        the import writers change it (imports.59 S5, generalised).

        imports.69: one commit (strict read, merge, atomic write under
        channels_lock). Returns True only when written. Refused, with nothing
        written: an unreadable channels.json (before: the copy in memory was
        written over it), and in the user role a copy that never loaded the
        file while the file exists (it would delete every Custom channel on
        disk that its empty list lacks). A missing file is still written
        (first install). A write failure returns False; it no longer raises."""
        if channels is None: channels = self.channelDATA['channels']
        # imports.69: before the lock -- getMYUUID can open the "name this device" dialog.
        uuid = SETTINGS.getMYUUID()
        if not self.writable:
            self.channelDATA['uuid']     = uuid
            self.channelDATA['channels'] = self.sortChannels(channels)
            PROPERTIES.setHasChannels(len(channels)>0)
            return False
        def _transform(doc, found):
            if modified_ids is None and found and not self._fromFile:
                self.log('setChannels, this copy never loaded channels.json -- nothing written', xbmc.LOGERROR)
                return None
            try:
                merged = self._mergeRecords(doc['channels'] if found else None, channels, modified_ids,
                                            deleted_ids, self.log, ctx='setChannels')
            except Exception as e:
                # Today's fallback, until imports.71 removes it: write the list as given.
                if modified_ids is not None:
                    self.log('setChannels, modified_ids merge failed, falling back to direct write: %s'%(e), xbmc.LOGWARNING)
                else:
                    self.log('setChannels, defensive import-preserve failed: %s'%(e), xbmc.LOGWARNING)
                merged = list(channels)
            new = dict(doc)
            new['uuid']     = uuid
            new['channels'] = self.sortChannels(merged)
            new['imports']  = doc['imports'] if found else list(self.channelDATA.get('imports') or [])
            return new
        return self.commit(_transform, ctx='setChannels') == COMMIT_WRITTEN


    def getImports(self) -> list:
        return self.channelDATA.get('imports',[])
        
        
    @staticmethod
    def _importIds(entries) -> set:
        """imports.69: the ids of the import entries in a list (dict entries
        with a usable id; a damaged hand-edited entry is skipped)."""
        ids = set()
        for entry in entries or []:
            iid = entry.get('id') if isinstance(entry, dict) else None
            try:
                if iid: ids.add(iid)
            except TypeError:
                continue                          # an unhashable id (list/dict) from a hand edit
        return ids


    def setImports(self, data=None) -> bool:
        """Store this whole import list (the low-level writer; after
        imports.69 the dashboard, Orphans and the sync use saveImportRequest,
        removeOrphans and commitSync).

        Not for production code (tests and tooling only; a test pins that no
        module in resources/lib calls it): it stores `data` exactly as given,
        so a list read earlier would wipe the sync info, tombstones and Saves
        written since -- the "never synced" bug imports.69 fixed.

        The channel records of every import the list no longer holds are
        purged in the same write: without that, a deleted import's channels
        kept claiming channel numbers and polluted pseudotv.m3u/xml until
        "Cleanup stranded". A switched-off import keeps its records and its
        guide cache; only removal frees them.

        imports.69: one commit. The removed imports are found against the
        file, not this copy's start-up list, and the guide caches of imports
        no longer listed are removed only after a successful write (before:
        also when the write failed). An unreadable file is refused."""
        data = list(data or [])
        if not self.writable:
            # A read-only copy changes only its memory, as before; no guide
            # cache is touched any more (before: removed although nothing
            # was written).
            removed = self._importIds(self.channelDATA.get('imports')).difference(self._importIds(data))
            if removed:
                self.channelDATA['channels'] = importcfg_helpers.without_import_records(self.channelDATA.get('channels'), removed)
            self.channelDATA['imports'] = data
            return False
        def _transform(doc, found):
            base = doc if found else self._memoryDoc()
            removed = self._importIds(base['imports']).difference(self._importIds(data))
            new = dict(base)
            if removed:
                kept = importcfg_helpers.without_import_records(base['channels'], removed)
                if len(kept) != len(base['channels']):
                    self.log('setImports, purged %d channels from removed imports: %s'
                             % (len(base['channels']) - len(kept), sorted(str(i) for i in removed)), xbmc.LOGINFO)
                new['channels'] = kept
            new['imports'] = data
            return new
        return self.commit(_transform, ctx='setImports', sweep_caches=True) == COMMIT_WRITTEN


    def saveImportRequest(self, payload, check_address=None) -> dict:
        """imports.69: one dashboard request (POST /imports.json) as one commit.

        importcfg_helpers.apply_dashboard_request applies it to the import
        list as it is on disk now: the dashboard's own fields only, so every
        import keeps its sync info and tombstones; old tabs' whole lists get
        the one-change rules. A Delete also purges that import's channel
        records in the same write, and its guide cache goes right after the
        write. Nothing changed: nothing is written (no Kodi setting write).

        Returns the summary {'op', 'id', 'changed', 'fields', 'reset',
        'removed_ids'} plus 'purged_channels' and 'status' (COMMIT_*).
        ImportRequestError (a bad request: 400/404/409) propagates."""
        result = {'op': None, 'id': None, 'changed': False, 'fields': [], 'reset': [], 'removed_ids': [],
                  'purged_channels': 0}
        def _transform(doc, found):
            base = doc if found else self._memoryDoc()
            new_list, summary = importcfg_helpers.apply_dashboard_request(base['imports'], payload, check_address)
            result.update(summary)
            if not summary.get('changed'):
                return None
            new = dict(base)
            new['imports'] = new_list
            removed = summary.get('removed_ids') or []
            if removed:
                kept = importcfg_helpers.without_import_records(base['channels'], removed)
                result['purged_channels'] = len(base['channels']) - len(kept)
                if result['purged_channels']:
                    self.log('saveImportRequest, purged %d channels of the deleted import %s'
                             % (result['purged_channels'], sorted(str(i) for i in removed)), xbmc.LOGINFO)
                new['channels'] = kept
            return new
        result['status'] = self.commit(_transform, ctx='saveImportRequest', sweep_caches=True)
        return result


    def removeOrphans(self, import_id) -> dict:
        """imports.69: 🗑 Orphans as one commit, on the file as it is now.
        The import's orphan records (type 'import', is_orphan True) leave the
        channel list and their ids join the import's tombstones, so the next
        sync doesn't re-create them. Before, the endpoint wrote the two lists
        separately, from copies read at request start.

        Returns {'status': COMMIT_*, 'found': the import exists,
        'removed': records removed}. Nothing to remove: nothing written."""
        result = {'status': None, 'found': False, 'removed': 0}
        def _transform(doc, found):
            if not any(isinstance(i, dict) and i.get('id') == import_id for i in doc['imports']):
                return None
            result['found'] = True
            orphan_ids = {c.get('id') for c in doc['channels']
                          if isinstance(c, dict) and c.get('type') == 'import' and c.get('import_source') == import_id
                          and c.get('is_orphan') is True and isinstance(c.get('id'), str) and c.get('id')}
            if not orphan_ids:
                return None
            def _tombstoned(entry):
                old = entry.get('tombstones')
                old = [t for t in old if isinstance(t, str)] if isinstance(old, list) else []
                return dict(entry, tombstones=sorted(set(old).union(orphan_ids)))
            new = dict(doc)
            new['imports']  = [_tombstoned(i) if isinstance(i, dict) and i.get('id') == import_id else i
                               for i in doc['imports']]
            new['channels'] = [c for c in doc['channels']
                               if not (isinstance(c, dict) and isinstance(c.get('id'), str) and c.get('id') in orphan_ids)]
            result['removed'] = len(doc['channels']) - len(new['channels'])
            return new
        result['status'] = self.commit(_transform, ctx='removeOrphans')
        return result


    def commitSync(self, start_by_id, results, channels, modified_ids, deleted_ids) -> dict:
        """imports.69: an import sync's one commit (Imports.syncAll), instead
        of a setChannels plus a setImports written from the sync's start copy.

        start_by_id: deep copies of the import entries as the sync read them;
        results: syncAll's per-import results; channels, modified_ids,
        deleted_ids: the sync's channel list and id sets, as it passed them
        to setChannels. importcfg_helpers.plan_sync_commit applies each
        result's own sync fields onto the entry as it is now (a Save, Add,
        Delete or Orphans made while the sync ran stays) and filters the
        sync's channel list; the records are then merged as a background
        writer (_mergeRecords). A merge error propagates and nothing is
        written: writing the list as given would delete the channels of
        every import this sync skipped.

        Refuses a missing file (background). Returns plan_sync_commit's info
        ('gone', 'playlist_moved', 'guide_moved', 'superseded', 'kept',
        'dropped', 'drop_imports', 'dropped_ids', 'deleted_ids',
        'applied_deletes', ...) plus 'status' (COMMIT_*); when nothing ran,
        just {'status': ...}."""
        uuid = SETTINGS.getMYUUID()   # before the lock (it can open a dialog)
        planned = {}
        def _transform(doc, found):
            fresh_ids = {c.get('id') for c in doc['channels'] if isinstance(c, dict) and isinstance(c.get('id'), str)}
            new_imports, writer, mods, dels, info = importcfg_helpers.plan_sync_commit(
                doc['imports'], fresh_ids, start_by_id, results, channels, modified_ids, deleted_ids)
            planned['info'] = info
            merged = self._mergeRecords(doc['channels'], writer, mods, dels, self.log, ctx='commitSync')
            new = dict(doc)
            new['uuid']     = uuid
            new['channels'] = self.sortChannels(merged)
            new['imports']  = new_imports
            return new
        status = self.commit(_transform, ctx='commitSync', background=True, sweep_caches=True)
        info = planned.get('info') or {}
        info['status'] = status
        return info


    def _sweepGuideCaches(self, imports):
        """imports.69: remove every guide cache file (CACHE_LOC/epg_<x>.xml)
        whose <x> is not the id of an entry in `imports` (the import list
        just written). Runs inside commit, right after a successful write of
        an import list (setImports, saveImportRequest, commitSync), so a cache
        left behind by a refused or failed save, by a sync that re-wrote it
        for an import deleted meanwhile, or by a restart between a Delete and
        its removal goes at the next successful one. A switched-off import is
        still listed and keeps its cache. The names come from the folder
        listing (importcfg_helpers.stale_guide_caches), so no path is built
        from an import id; only epg_*.xml files directly in the cache folder
        are touched. Never raises: a failure is logged."""
        try:
            folder = FileAccess.translatePath(CACHE_LOC)   # a special:// path at runtime
            try:
                names = os.listdir(folder)
            except FileNotFoundError:
                return
            keep = [i.get('id') for i in (imports or []) if isinstance(i, dict)]
            for name in importcfg_helpers.stale_guide_caches(names, keep):
                path = os.path.join(folder, name)
                try:
                    if not os.path.isfile(path): continue
                    os.unlink(path)
                    self.log('commit, removed guide cache %s (its import is no longer listed)' % (name,), xbmc.LOGINFO)
                except FileNotFoundError:
                    continue
                except Exception as e:
                    self.log('commit, could not remove guide cache %s: %s' % (name, e), xbmc.LOGWARNING)
        except Exception as e:
            self.log('commit, guide cache sweep failed: %s' % (e,), xbmc.LOGWARNING)


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
        