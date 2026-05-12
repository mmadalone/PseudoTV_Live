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

# madteevee (imports fork): process-wide writer lock for pseudotv.m3u
# and pseudotv.xml. Serializes the render + os.replace pair across all
# writer threads (Builder, chkImports daemon, manual HTTP-triggered
# rebuilds). Plan: /home/madalone/.claude/plans/misty-shimmying-liskov.md
# (C#10 from the 2026-05-12 audit).
#
# Pre-refactor, multiple writers could each compute a complete-but-
# divergent view of M3UDATA / XMLTVDATA and race at os.replace; the
# loser's content was silently overwritten. The visible bug was the
# Custom-merge-from-disk workaround at imports.py:916-955 — that
# recovers from chkImports daemon's writes wiping Builder's Custom
# channel programmes. The lock prevents the writes from overlapping
# at the disk boundary; it does NOT address the deeper in-memory
# state-divergence problem (chkImports daemon uses fresh local M3U/
# XMLTVS instances rather than sharing Builder's instance). That
# follow-up is documented in the plan as out-of-scope for this change.
#
# RLock so a thread already holding the lock (e.g. Builder.__setStation
# calling self.m3u._save() which itself acquires) can re-acquire
# safely. Single combined lock (not separate M3U_LOCK + XMLTV_LOCK)
# because Builder.__setStation writes both files per channel; separate
# locks would force a lock-order discipline with no real concurrency
# benefit on a 4-core Pi5 with at most 2-3 writer threads in flight.
#
# Held timeout matches the Resources.__del__ watchdog idiom
# (resources.py:71-102): bounded acquire, log-and-skip on timeout —
# never silently hang. The fallback skip is observably worse than
# today's silent-drop race (the loser's writes are skipped instead of
# partially dropped), but the timeout is logged with a caller-tagged
# ctx so the offending acquire site is identifiable from a single log
# line. 30s is comfortably above worst-observed legitimate hold
# (XMLTV render of 22 MB on Pi5 measures ~3-8s).
import threading

try:
    import xbmc
    _LOG_WARNING = xbmc.LOGWARNING
except Exception:
    # Test scaffolding may load this module without xbmc stubs available.
    # Fall back to a sentinel that the local log() can accept.
    _LOG_WARNING = 2

try:
    from logger import log as _log
except Exception:
    # Same fallback as above — if logger isn't importable in a unit-test
    # context, drop to stderr. Production always has logger available.
    import sys
    def _log(msg, level=_LOG_WARNING):
        try: sys.stderr.write('writer_lock: %s\n' % (msg,))
        except Exception: pass


WRITER_LOCK    = threading.RLock()
WRITER_TIMEOUT = 30.0   # seconds; longer than any expected critical section


class WriterTimeout(Exception):
    """Raised when WRITER_LOCK cannot be acquired within WRITER_TIMEOUT.
    Callers should catch and treat as 'skip this write' — disk content
    is preserved; the next caller succeeds. Better than today's silent
    drop because the skip is logged."""


class _Held(object):
    """Context manager returned by held(). Acquires WRITER_LOCK with a
    bounded timeout; raises WriterTimeout on failure so the caller
    doesn't silently hang on a wedged holder."""

    def __init__(self, timeout, ctx):
        self.timeout = timeout
        self.ctx     = ctx
        self.got     = False

    def __enter__(self):
        self.got = WRITER_LOCK.acquire(timeout=self.timeout)
        if not self.got:
            _log('writer_lock: TIMEOUT (%.1fs) acquiring for %s — skipping write to avoid deadlock'
                 % (self.timeout, self.ctx), _LOG_WARNING)
            raise WriterTimeout(self.ctx)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.got:
            WRITER_LOCK.release()
        return False   # don't suppress exceptions raised inside the with-block


def held(ctx='', timeout=WRITER_TIMEOUT):
    """Context manager factory. Use as `with held(ctx='M3U._save'): ...`
    so the offending acquire site is identifiable on timeout-log."""
    return _Held(timeout, ctx)
