#   Copyright (C) 2025 Lunatixz
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

# madteevee (imports fork, imports.69): the in-process lock around every
# channels.json write (the writer_lock.py idiom).
#
# Channels.commit (channels.py) holds it for one strict fresh read of
# channels.json, the merge and the atomic write. No other thread of the
# service can then write channels.json between a commit's read and its write,
# so a dashboard Save, a sync's results and an Orphans removal each land on
# the file as it is at that moment instead of overwriting each other with
# copies read earlier. Before imports.69 a Save wiped every import's sync
# info ("never synced") and a running sync could undo a Save.
#
# Lock contract (imports.71's per-channel merge follows it too):
#   - Order: PROPERTIES.interruptActivity() (outer) -> CHANNELS_LOCK -> the
#     FileLock inside FileAccess.setJSON (inner).
#   - Never taken together with writer_lock.WRITER_LOCK, in either order.
#   - Nothing slow while held: no dialogs, no JSON-RPC, no downloads, no
#     renders. The hold is the read, merge, dump and fsync of a ~215 KB file
#     (tens of milliseconds on the Pi); it is NOT a lock for a whole build.
#   - It covers the service interpreter only: the HTTP threads (server.py),
#     the imports thread (tasks.py) and the queue/Builder thread. Kodi
#     scripts (Channel Manager, context menu) run in their own Python
#     sub-interpreter with their own copy of this module; that millisecond
#     gap is accepted.
#
# Timeout: CHANNELS_TIMEOUT (15 s, the same bound as the FileLock taken
# inside, LOCK_MAX_FILE_TIMEOUT). A timeout is logged at ERROR, so it shows
# with debug logging off, and raises ChannelsLockTimeout; Channels.commit
# turns it into "busy" and writes nothing. held() reads CHANNELS_TIMEOUT when
# it is called (a default argument would freeze the value at import), so
# tests can shorten the wait with monkeypatch.
#
# Kill switch (this file only): replace
#     CHANNELS_LOCK = threading.RLock()
# with
#     CHANNELS_LOCK = _NoLock()
# Every merge still runs on fresh data; only the serialisation between
# threads goes away.
import threading

try:
    import xbmc
    _LOG_ERROR = xbmc.LOGERROR
except Exception:
    # Test scaffolding may load this module without xbmc stubs available
    # (same fallback as writer_lock.py).
    _LOG_ERROR = 3

try:
    from logger import log as _log
except Exception:
    # Same fallback as writer_lock.py: without the add-on's logger (a unit
    # test context) the line goes to stderr. Production always has logger.
    import sys

    def _log(msg, level=_LOG_ERROR):
        try: sys.stderr.write('%s\n' % (msg,))
        except Exception: pass  # noqa: S110 -- logging must never raise


CHANNELS_LOCK    = threading.RLock()
CHANNELS_TIMEOUT = 15.0   # seconds; read by held() at call time


class ChannelsLockTimeout(Exception):
    """CHANNELS_LOCK could not be taken within the timeout. Nothing was
    written; the caller reports "busy" (the dashboard answers 503, a sync
    logs an ERROR and its next cycle redoes the work). The argument is the
    caller's ctx."""


class _NoLock(object):
    """The kill switch's stand-in for CHANNELS_LOCK (see the header): the
    same acquire/release calls, no serialisation."""

    def acquire(self, blocking=True, timeout=None):
        return True

    def release(self):
        return None


class _Held(object):
    """Context manager returned by held(). Takes CHANNELS_LOCK with a
    bounded wait and raises ChannelsLockTimeout instead of hanging behind a
    wedged holder. __exit__ releases the lock object it took (even if the
    module's CHANNELS_LOCK was swapped meanwhile) and never suppresses an
    exception raised inside the block."""

    def __init__(self, timeout, ctx):
        self.timeout = timeout
        self.ctx     = ctx
        self._lock   = None

    def __enter__(self):
        lock = CHANNELS_LOCK
        if not lock.acquire(timeout=self.timeout):
            _log('channels_lock: TIMEOUT (%.1fs) waiting to save channels.json for %s -- nothing written'
                 % (self.timeout, self.ctx), _LOG_ERROR)
            raise ChannelsLockTimeout(self.ctx)
        self._lock = lock
        return self

    def __exit__(self, exc_type, exc, tb):
        lock, self._lock = self._lock, None
        if lock is not None:
            lock.release()
        return False   # never suppress an exception raised inside the with-block


def held(ctx='', timeout=None):
    """`with channels_lock.held('saveImportRequest'): ...` -- the ctx names
    the caller in the timeout line. timeout=None means CHANNELS_TIMEOUT as it
    is at the moment of the call."""
    if timeout is None:
        timeout = CHANNELS_TIMEOUT
    return _Held(timeout, ctx)
