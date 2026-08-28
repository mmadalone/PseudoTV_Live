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
#
# imports.55: pure helpers for the changelog popup. Kept import-light
# (stdlib only) so they are trivially unit-testable — same pattern as
# logo_helpers / cleanup_helpers / sysinfo_helpers.
import re

# A changelog entry may separate its short operator-facing bullets from the
# deep engineering narrative with this marker line; everything from the
# marker on is kept in changelog.txt as the project's engineering archive
# but dropped from the update popup.
DETAIL_MARKER  = '--- engineering notes'

# Version headers look like `v.0.8.0+imports.54` (fork) or `v0.6.1q`
# (upstream-era). Anything else — including the `### NOTICE` banner at the
# top of the file — is body text.
_VERSION_LINE  = re.compile(r'^v\.?\d')


def latestEntry(text, marker=DETAIL_MARKER):
    """Return only the newest version's entry from a changelog.

    The newest entry is the first version-header line and everything below
    it up to (not including) the next version header. If the entry contains
    a detail marker line, the entry is truncated there. Falls back to the
    full text when no version header is found (better to over-show than to
    show nothing on a malformed file).
    """
    lines = text.splitlines()
    start = None
    end   = len(lines)
    for i, line in enumerate(lines):
        if _VERSION_LINE.match(line.strip()):
            if start is None:
                start = i
            else:
                end = i
                break
    if start is None:
        return text.strip()
    entry = []
    for line in lines[start:end]:
        if line.strip().lower().startswith(marker):
            break
        entry.append(line)
    return '\n'.join(entry).strip()
