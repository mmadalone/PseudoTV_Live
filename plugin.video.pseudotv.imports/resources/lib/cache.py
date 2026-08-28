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
import sqlite3

from variables   import *
from logger      import log
from fileaccess  import FileAccess, FileLock


class Service(object):
    monitor = MONITOR()
    def _shutdown(self, wait=1.0) -> bool:
        return (self.monitor.waitForAbort(wait) | (Globals._getProperty('%s.pendingShutdown'%(ADDON_ID),False)))
    def _aborted(self) -> bool:
        # imports.54: non-blocking twin of _shutdown — same signals, no
        # waitForAbort sleep. For per-statement gates in hot paths that
        # must not stall (see _execute_sql).
        return (self.monitor.abortRequested() | (Globals._getProperty('%s.pendingShutdown'%(ADDON_ID),False)))
    def _interrupt(self) -> bool:
        return (Globals._getProperty('%s.pendingShutdown'%(ADDON_ID),False) | Globals._getProperty('%s.pendingRestart'%(ADDON_ID),False) | Globals._getProperty('%s.pendingInterrupt'%(ADDON_ID),False))
    def _suspend(self, wait=1.0) -> bool:
        return Globals._getProperty('%s.pendingSuspend'%(ADDON_ID),False)
    def _sleep(self, wait=1.0):
        while not self.monitor.abortRequested() and wait > 0:
            if (self.monitor.waitForAbort(CPU_CYCLE) | self._interrupt()): return True
            else: wait -= CPU_CYCLE
        return False

def cacheit(expiration=datetime.timedelta(minutes=15), checksum=ADDON_VERSION):
    def internal(method):
        @wraps(method)
        def wrapper(*args, **kwargs):
            # Build safe, truncated key to avoid huge key strings (which can blow memory)
            instance = args[0]
            cacheName = "%s.%s" % (instance.__class__.__name__, method.__name__)
            for item in args[1:]:
                cacheName += u".%s"%(FileAccess._getMD5(item))
            for k, v in list(kwargs.items()):
                cacheName += u".%s=%s"%(k,FileAccess._getMD5(v))
            results = instance.cache.get(cacheName, checksum,)
            if results is not None:
                log('%s, cacheit returning cache' % (method.__qualname__.replace('.', ': ')))
                return results
            log('%s, cacheit saving results' % (method.__qualname__.replace('.', ': ')))
            value = method(*args, **kwargs)
            instance.cache.set(cacheName, value, checksum, expiration,)
            return value
        return wrapper
    return internal

class Cache(object):
    service = Service()
    monitor = service.monitor

    def __init__(self, mem_cache=False, disable_cache=False):
        self.cache = _Cache(service=self.service)
        self.cache.enable_mem_cache = mem_cache
        # disable_cache is True if explicitly passed OR addon settings say so
        self.disable_cache          = (disable_cache or REAL_SETTINGS.getSettingBool('Disable_Cache'))
        self.log('__init__, mem_cache = %s, disable_cache = %s' % (mem_cache, disable_cache))


    def log(self, msg, level=xbmc.LOGDEBUG):
        log('%s [%s]: %s' % (self.__class__.__name__, {True:'MEM',False:'DB'}[self.cache.enable_mem_cache], msg), level)


    def set(self, name, value, checksum=ADDON_VERSION, expiration=datetime.timedelta(minutes=15)):
        # don't store empty values when cache disabled unless explicitly allowed
        if not any([self.disable_cache,value is None]):
            self.cache.set(name, value, checksum, expiration)
            self.log('set, name = %s, value = %s, type = %s' % (name, '%s...'%(str(value)[:128]),type(value).__name__))
        return value


    def get(self, name, checksum=ADDON_VERSION):
        if not self.disable_cache:
            try:
                value = self.cache.get(name, checksum)
                self.log('get, name = %s, value = %s, type = %s' % (name, '%s...'%(str(value)[:128]),type(value).__name__))
                return value
            except Exception as e:
                self.log("get, name = %s failed! %s" % (name, e), xbmc.LOGERROR)
                self.cache.clr(name)


    def clr(self, name, wait=15):
        self.log('clr, name = %s' % name)
        self.cache.clr(name)


    def chkCleanup(self, force=False):
        # imports.54: service task entry point (Tasks.chkCacheClean is the
        # only force=True caller).
        return self.cache.chkCleanup(force)


class _Cache(object):
    _cache_idx           = deque()
    _busy_tasks          = []
    enable_mem_cache     = False
    window               = None
    global_checksum      = ADDON_VERSION
    # madteevee (imports fork): _auto_clean_interval was a class attribute that
    # read REAL_SETTINGS at class-definition time — Max_Days changes did not
    # take effect until Kodi restart, and a non-numeric value would crash the
    # module on import. Moved to __init__ with a safe int parse. Unit kept as
    # hours (matches the original behavior; renaming would change the cadence
    # operators have already tuned to).

    def __init__(self, service=None, winID=10000):
        self.max_bytes = _Cache._getFreeMEM()
        self.service   = service
        self.monitor   = service.monitor
        self.window    = xbmcgui.Window(winID)
        self.dbfile    = FileAccess.translatePath(os.path.join(REAL_SETTINGS.getSetting('User_Folder'),'cache.db'))
        self._auto_clean_interval = self._cleanInterval()
        self.log('__init__, max_bytes = %s, winID = %s, dbfile = %s, auto_clean_interval = %s' % (self.max_bytes, winID, self.dbfile, self._auto_clean_interval))
        self.chkCleanup()


    def _cleanInterval(self):
        # imports.54: read Max_Days at CHECK time, not construction time. The
        # service's Settings.cacheDB instance lives for the whole Kodi session,
        # so an interval cached in __init__ froze operator changes until
        # restart (Max_Days is also hourly re-synced from Kodi's
        # epg.futuredaystodisplay by Tasks.chkKodiSettings). Unit kept as
        # hours — see the class comment above.
        try: _hours = int(REAL_SETTINGS.getSetting('Max_Days') or '3')
        except (TypeError, ValueError): _hours = 3
        return datetime.timedelta(hours=_hours)


    def __del__(self):
        try: self.chkCleanup()
        except Exception: pass


    def log(self, msg, level=xbmc.LOGDEBUG):
        return log('%s: %s' % (self.__class__.__name__, msg), level)


    @staticmethod
    def _getFreeMEM():
        try:              free = int("".join(re.findall(r"\d", BUILTIN.getInfoLabel('FreeMemory','System'))))
        except Exception: free = 1024 #1GB
        return floor(free * (REAL_SETTINGS.getSettingInt('Cache_MEM_Limit') / 100)) * 1024 * 1024
        
        
        
    def chkCleanup(self, force=False):
        # madteevee (imports fork): was using repr(cur_time) for writes and
        # eval(lastexecuted) for reads. Kodi window properties are visible to
        # every addon in the process — any of them could store an expression
        # that eval would execute. Switched to isoformat / fromisoformat which
        # parse only as datetime.
        # imports.54: _cleanUP no longer runs inline here. This is called from
        # _Cache.__init__, which executes at MODULE IMPORT time of every plugin
        # invocation (kodi.py's Settings class body) — a channel tune paid the
        # whole purge + VACUUM behind Kodi's busy spinner whenever the Max_Days
        # window (unit = hours) had lapsed. Observed live 2026-08-28: 15m20s
        # spinner on a 134 MB / 29k-row cache.db. Constructors now only stamp
        # the baseline; the service queue task Tasks.chkCacheClean is the sole
        # force=True caller and the only place maintenance actually runs.
        cur_time     = datetime.datetime.now()
        lastexecuted = Globals._getProperty("%s.cache.lastexecuted"%(ADDON_ID))
        if not lastexecuted: Globals._setProperty("%s.cache.lastexecuted"%(ADDON_ID), cur_time.isoformat())
        else:
            self._auto_clean_interval = self._cleanInterval()
            try:    parsed = datetime.datetime.fromisoformat(lastexecuted)
            except (TypeError, ValueError): parsed = None
            if parsed is None or (parsed + self._auto_clean_interval) < cur_time:
                if force: self._cleanUP()
                else:     self.log('chkCleanup, cleanup due; deferred to service task (chkCacheClean)', xbmc.LOGINFO)


    def get(self, endpoint, checksum=""):
        checksum = self.getChecksum(checksum)
        cur_time = self.getTimestamp(datetime.datetime.now())
        result   = None
        if self.enable_mem_cache: result = self._getMEM(endpoint, checksum, cur_time)
        if result is None:        result = self._getDB(endpoint, checksum, cur_time)
        return result


    def set(self, endpoint, data, checksum="", expiration=datetime.timedelta(days=30)):
        task_name = "set.%s" % endpoint
        self._busy_tasks.append(task_name)
        checksum = self.getChecksum(checksum)
        expires  = self.getTimestamp(datetime.datetime.now() + expiration)
        if self.enable_mem_cache: self._setMEM(endpoint, checksum, expires, data)
        self._setDB(endpoint, checksum, expires, data)
        self._busy_tasks.remove(task_name)


    def clr(self, endpoint, wait=15):
        self._execute_sql('DELETE FROM cache WHERE id LIKE ?', (endpoint + '%',))


    def _getMEM(self, endpoint, checksum, cur_time):
        result = None
        try: 
            cachedata = FileAccess._decodeString(Globals._getProperty('%s.%s'%(ADDON_ID,endpoint)))
            if cachedata[0] > cur_time and not checksum or checksum == cachedata[2]: result = cachedata[1]
        except Exception as e: pass
        return result


    def _setMEM(self, endpoint, checksum, expires, data):
        try: 
            string_text = FileAccess._encodeString((expires, data, checksum))
            string_size = sys.getsizeof(string_text)
            if string_size > self.max_bytes: raise Exception(f"_setMEM, {endpoint} too large for cache limit {self.max_bytes}!")
            else:
                Globals._setProperty('%s.%s'%(ADDON_ID,endpoint), string_text)
                self._cache_idx.append((endpoint, string_size))
                self._trimMEM()
        except Exception as e: self.log("_setMEM, failed! %s"%(e), xbmc.LOGERROR)


    def _getSize(self):
        return sum(size for _, size in self._cache_idx)


    def _trimMEM(self):
        # While current size exceeds limit, remove the oldest (leftmost) items
        while not self.monitor.abortRequested() and self._getSize() > self.max_bytes:
            try:
                endpoint, removed_size = self._cache_idx.popleft()
                Globals._clrProperty('%s.%s'%(ADDON_ID,endpoint))
                self.log(f"_trimMEM, {endpoint} removed {removed_size} bytes from memory!")
            except Exception as e: self.log("_trimMEM, failed! %s"%(e), xbmc.LOGERROR)
        self.log(f'_trimMEM, {self._getSize()}/{self.max_bytes} available bytes')


    def _getDB(self, endpoint, checksum, cur_time):
        result     = None
        query      = "SELECT expires, data, checksum FROM cache WHERE id = ?"
        cache_data = self._execute_sql(query, (endpoint,))
        if cache_data:
            cache_data = cache_data.fetchone()
            if cache_data and cache_data[0] > cur_time:
                if not checksum or cache_data[2] == checksum:
                    try: 
                        result = FileAccess.loadPICKLE(cache_data[1])
                        if self.enable_mem_cache: self._setMEM(endpoint, checksum, cache_data[0], result)
                    except Exception as e: self.log("_getDB, failed! %s"%(e), xbmc.LOGERROR)
        return result


    def _setDB(self, endpoint, checksum, expires, data):
        query = "INSERT OR REPLACE INTO cache( id, expires, data, checksum) VALUES (?, ?, ?, ?)"
        try: self._execute_sql(query, (endpoint, expires, FileAccess.dumpPICKLE(data), checksum))
        except Exception as e: self.log("_setDB, failed! %s"%(e), xbmc.LOGERROR)


    def _cleanUP(self):
        # imports.54: rewritten set-based, mirroring upstream 0.7.x _cleanDB.
        # Old shape iterated EVERY row with a blocking _shutdown(CPU_CYCLE)
        # wait + a window-property clear, then issued one DELETE per expired
        # row — 10-15+ min of grinding at 29k rows / 134 MB, paid behind the
        # busy spinner when a channel tune's import triggered it. Now: one
        # SELECT of the expired ids (their mem-cache property mirrors still
        # need clearing), one set-based DELETE, VACUUM. Fresh entries keep
        # their warm mem-cache mirrors — _getMEM self-expires them — where
        # the old loop wiped every row's property.
        # lastexecuted is stamped ONLY on success: the old code stamped it
        # even when the pass was aborted mid-loop (user cancelling the
        # spinner killed the invoker but the stamp still landed), so expired
        # rows were never actually purged — observed 3-week-old expired rows
        # and unbounded DB growth making every next pass slower.
        cur_time      = datetime.datetime.now()
        cur_timestamp = self.getTimestamp(cur_time)
        if Globals._getProperty("%s.cache.cleanbusy"%(ADDON_ID)):
            self.log("_cleanUP, skipped (cleanup already busy)")
            return
        self.log("_cleanUP, running _cleanUP...")
        Globals._setProperty("%s.cache.cleanbusy"%(ADDON_ID), "busy")
        self._busy_tasks.append(__name__)
        completed = False
        try:
            expired = self._execute_sql("SELECT id FROM cache WHERE expires < ?", (cur_timestamp,))
            if expired is not None:
                expired_ids = [row[0] for row in expired.fetchall()]
                if expired_ids:
                    # _execute_sql signals EVERY failure (FileLock timeout,
                    # exhausted retries, abort gate) by returning None — the
                    # purge only counts as done when the DELETE returned a
                    # cursor. A false stamp here re-opens the original
                    # never-actually-purged growth cycle, silently.
                    deleted = self._execute_sql('DELETE FROM cache WHERE expires < ?', (cur_timestamp,))
                    if deleted is not None:
                        completed = True
                        for cache_id in expired_ids:
                            Globals._clrProperty('%s.%s'%(ADDON_ID,cache_id))
                        # index (upstream parity, self-heals existing DBs) and
                        # VACUUM are best-effort space maintenance — only worth
                        # the exclusive lock when rows actually went away, and
                        # their failure must not force an hourly purge retry.
                        self._execute_sql("CREATE INDEX IF NOT EXISTS idx_expires ON cache(expires)")
                        self._execute_sql("VACUUM")
                        self.log("_cleanUP, purged %d expired entries"%(len(expired_ids)), xbmc.LOGINFO)
                    else:
                        self.log("_cleanUP, DELETE failed (db contended?); %d expired entries kept, will retry next tick"%(len(expired_ids)), xbmc.LOGWARNING)
                else:
                    completed = True
                    self.log("_cleanUP, nothing expired; skipping VACUUM", xbmc.LOGINFO)
        finally:
            try: self._busy_tasks.remove(__name__)
            except ValueError: pass
            if completed: Globals._setProperty("%s.cache.lastexecuted"%(ADDON_ID), cur_time.isoformat())
            Globals._clrProperty("%s.cache.cleanbusy"%(ADDON_ID))


    def _execute_sql(self, query, data=None):
        retries = 0
        result  = None
        if not FileAccess.exists(CACHE_LOC):
            FileAccess.mkdirs(CACHE_LOC)
        # madteevee (imports fork): the original code treated ANY exception
        # from the probe as "delete the cache and start fresh." That meant a
        # single transient lock, disk-I/O blip, or the documented cacheDB.set
        # wedge could wipe the entire cache (and force every cached EPG /
        # logo / lookup to refetch on the next tick — load spike on the Pi).
        # Now we only delete when the error signature actually indicates the
        # file isn't a usable SQLite database; anything else is logged and
        # bubbles up as "this call failed," leaving the cache intact.
        _CORRUPTION_HINTS = ('not a database', 'malformed', 'no such table',
                             'file is encrypted', 'unsupported file format')
        try:
            connection = sqlite3.connect(self.dbfile, timeout=30, isolation_level=None)
            connection.execute('SELECT * FROM cache LIMIT 1')
        except sqlite3.DatabaseError as e:
            msg = str(e).lower()
            if not any(hint in msg for hint in _CORRUPTION_HINTS):
                self.log("_execute_sql, DatabaseError preserved (not a corruption signature): %s" % e, xbmc.LOGWARNING)
                return
            self.log("_execute_sql, cache.db appears corrupt (%s); recreating" % e, xbmc.LOGWARNING)
            if FileAccess.exists(self.dbfile):
                FileAccess.delete(self.dbfile)
            try:
                connection = sqlite3.connect(self.dbfile, timeout=30, isolation_level=None)
                connection.execute( """CREATE TABLE IF NOT EXISTS cache(id TEXT UNIQUE, expires INTEGER, data TEXT, checksum INTEGER)""")
            except Exception as e:
                self.log("_execute_sql, recreate after corruption failed: %s" % str(e), xbmc.LOGWARNING)
                return
        except Exception as e:
            # Connect failed for a non-corruption reason (permissions, disk
            # full, SQLite library complaints). Don't delete; let the next
            # call try again.
            self.log("_execute_sql, connect failed (not deleting): %s" % e, xbmc.LOGWARNING)
            return

        while not self.service.monitor.abortRequested() and not retries == LOCK_MAX_FILE_TIMEOUT:
            # imports.54: was `if self.service._shutdown(CPU_CYCLE): break` —
            # a BLOCKING waitForAbort(0.016) before EVERY statement, taxing
            # every cache get/set addon-wide and turning row-by-row loops
            # into minutes-long grinds. The shutdown gate stays, non-blocking;
            # sleeping belongs only in the contention-retry branch below.
            if self.service._aborted(): break
            try:
                with FileLock(self.dbfile):
                    if isinstance(data, list): result = connection.executemany(query, data)
                    elif data:                 result = connection.execute(query, data)
                    else:                      result = connection.execute(query)
                    return result
            except sqlite3.OperationalError as e:
                retries += 1
                self.log("_execute_sql, retrying DB commit...", xbmc.LOGWARNING)
                self.service._sleep(LOCK_MAX_FILE_DELAY)
            except Exception as e:
                self.log("_execute_sql, connection ERROR ! -- %s" % str(e), xbmc.LOGERROR)
                break
                    
        if connection:
            connection.close()
            del connection


    @staticmethod
    def getTimestamp(date_time):
        try:
            return int(date_time.timestamp())
        except Exception:
            return int(time.mktime(date_time.timetuple()))


    def getChecksum(self, stringinput):
        # return simple summed-ord checksum; include global checksum if present
        if not stringinput and not self.global_checksum: return 0
        if self.global_checksum: combined = "%s-%s" % (self.global_checksum, stringinput)
        else:                    combined = str(stringinput)
        return sum(map(ord, combined))