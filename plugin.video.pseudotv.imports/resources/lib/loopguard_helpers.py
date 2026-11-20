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

# madteevee (imports fork, imports.71): which failures of the background service's loops reach
# Kodi's log.
#
# Import-light pure module (same pattern as restart_helpers.py / replayend_helpers.py): standard
# library only (os, traceback, collections), no Kodi imports, no add-on imports, no I/O (it reads
# an error's traceback frames, never a source file), Python 3.8 syntax.
#
# Why: one exception in one pass of the service's main loop (services.Service._start) ended the
# whole service, and Kodi never starts a stopped service script again. On 2026-06-05, 2026-10-04
# (off for 3 days 17 h) and 2026-11-07 a settings read failed while the add-on's settings were
# being saved, and the queue of background tasks stood still until Kodi was restarted. The main
# loop and the idle loop (services.Monitor._chkIdle, about once a second) now catch Exception in
# each pass, ask here whether to write it, and go on. Each distinct error is written once per
# session, at ERROR with its traceback; a repeat writes nothing, so an error that comes back on
# every pass (every 5 s) can't fill the log (the user's decision, 2026-11-20: each different error
# once, no reminder lines).
import os
import traceback
from collections import OrderedDict

# The distinct errors one loop remembers. Past this, the one seen longest ago is forgotten, and
# written again should it come back. A pass of either loop runs a few dozen lines of the add-on,
# so in practice nothing is ever forgotten; the bound only keeps the memory from growing.
MAX_KEYS = 64
# The add-on's own code: resources/lib (and its parsers folder).
LIB_DIR  = os.path.dirname(os.path.abspath(__file__))


def error_key(error, lib_dir=LIB_DIR):
    """What makes a failure distinct: (its class name, file name, line, function) of the innermost
    frame of its traceback that is the add-on's own (a file under lib_dir), so an error raised inside
    Python's or Kodi's code keys on the add-on line that called it. No such frame: the innermost
    frame; no traceback at all: the class alone. The message is left out on purpose: one that holds
    an id, a number or a time would make every failure new."""
    frames = [(frame.f_code.co_filename, lineno, frame.f_code.co_name)
              for frame, lineno in traceback.walk_tb(error.__traceback__)]
    own    = [entry for entry in frames if entry[0].startswith(lib_dir + os.sep)]
    picked = own or frames
    if not picked: return (type(error).__name__, '', 0, '')
    filename, lineno, name = picked[-1]
    return (type(error).__name__, os.path.basename(filename), lineno, name)


def describe(error):
    """The error as the last line of Python's traceback shows it: 'TypeError: Invalid setting type'
    (only the class when it has no text). Never raises: an error whose text can't be made reads
    as Python prints it then."""
    try: text = str(error)
    except Exception: text = '<exception str() failed>'
    name = type(error).__name__
    return '%s: %s' % (name, text) if text else name


class Failures(object):
    """The distinct failures (error_key) one loop has written, the most recently seen last; at
    most max_keys of them. One per loop, used only by that loop's thread."""

    def __init__(self, max_keys=MAX_KEYS):
        self.max_keys = max_keys
        self._seen    = OrderedDict()

    def first(self, key):
        """True the first time `key` comes: write it. False when it came before (while it is
        remembered); it then moves to the end, so a key that keeps coming back is never the one
        forgotten."""
        if key in self._seen:
            self._seen.move_to_end(key)
            return False
        self._seen[key] = True
        while len(self._seen) > self.max_keys: self._seen.popitem(last=False)
        return True

    def __len__(self):
        return len(self._seen)
