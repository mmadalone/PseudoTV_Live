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

# madteevee (imports fork, imports.69): the "sync the imports now" kick.
#
# A kick is the window property KICK_PROP. The dashboard and the add-on's own
# threads set it; the imports thread's poll loop (tasks.py) reads it every
# 3 seconds, clears it and runs one chkImports cycle with it as force_scope:
#   ''                none: no kick pending
#   RENDER_ONLY_KICK  a normal gated cycle that rewrites the playlist and guide
#                     files and forces no download (a Save, a Delete, Orphans)
#   '<import id>'     that import syncs now, the others keep their gates
#   'all'             every import syncs now
#
# Before imports.69 each setter overwrote whatever kick was pending, so a
# render kick could replace a pending 'all' and a Refresh on one row could
# cancel the global one. merge_kick keeps the stronger request instead
# (none < render < id < all; two different ids become 'all'), and set_kick
# reads, merges and writes under KICK_LOCK, which the poll loop also holds
# for its read / check / clear, so no kick set between those steps is lost.
#
# Imports threading only (no Kodi modules): every caller passes its own
# PROPERTIES object, and the tests pass a fake. KICK_LOCK covers the service
# interpreter; "Refresh imports now" in Kodi's settings runs in a script
# interpreter and can't share it (a millisecond window, accepted).
import threading

# imports.56 (moved here from imports.py in imports.69, which re-exports it):
# kick value that runs a normal, gated chkImports cycle right away -- no gate
# bypass, so no forced downloads. syncAll treats it exactly like
# force_scope=None.
RENDER_ONLY_KICK = '__render__'
KICK_PROP        = 'chkImports.kick'
KICK_ALL         = 'all'

KICK_LOCK         = threading.Lock()
KICK_LOCK_TIMEOUT = 2.0   # seconds; read at call time (tests shorten it)


def _kick_text(value):
    """PROPERTIES.getEXTProperty runs literal_eval on the stored text
    (kodi.py getEXTProperty), so an import id that looks like a number comes
    back as an int: compare kicks as stripped text."""
    return '' if value is None else str(value).strip()


def merge_kick(pending, new):
    """The kick to keep when `new` arrives while `pending` is set.
    Order none < render < import id < all: a kick never lowers a pending one,
    and two different import ids become 'all' (both imports must sync)."""
    pending, new = _kick_text(pending), _kick_text(new)
    if not new:
        return pending
    if not pending:
        return new
    if KICK_ALL in (pending, new):
        return KICK_ALL
    if pending == RENDER_ONLY_KICK:
        return new
    if new == RENDER_ONLY_KICK or new == pending:
        return pending
    return KICK_ALL


def set_kick(props, value):
    """Merge `value` into the pending kick; returns (before, after) as kick
    text. The property is written only when the merged value differs, so an
    unchanged kick causes no write (and no log line in Kodi's property code).
    If KICK_LOCK can't be had within KICK_LOCK_TIMEOUT the kick is still
    merged and written without it: a kick is never dropped. `props` is the
    add-on's PROPERTIES (or a test fake) with getEXTProperty(key, default) and
    setEXTProperty(key, value); both are called positionally."""
    lock = KICK_LOCK
    got = lock.acquire(timeout=KICK_LOCK_TIMEOUT)
    try:
        before = _kick_text(props.getEXTProperty(KICK_PROP, ''))
        after  = merge_kick(before, value)
        if after != before:
            props.setEXTProperty(KICK_PROP, after)
        return before, after
    finally:
        if got:
            lock.release()
