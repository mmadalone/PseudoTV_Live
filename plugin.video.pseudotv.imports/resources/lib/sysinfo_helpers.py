# -*- coding: utf-8 -*-
# sysinfo_helpers (madteevee imports.32): Kodi-stub-free helpers for the
# System Info endpoint. Imported by server.py and unit-tested in isolation
# (kept separate from server.py to dodge the heavy `from globals import *`
# import surface in tests — same precedent as filter_helpers.py for the
# imports.29/.30 helpers).
#
# Bug context: Kodi's `System.OSVersionInfo` InfoLabel can return the literal
# string "Busy" intermittently (observed on Debian/Pi when the JSON-RPC
# resolver loses a race against background work — Kodi forum threads document
# the same behavior). server.py's /api/system/info.json was emitting "Busy"
# verbatim, so the dashboard's "Kodi OS" field displayed "Busy" indefinitely
# until the next Kodi restart cleared it.
#
# Fix: resolveKodiOSVersion falls back through /etc/os-release PRETTY_NAME
# (Linux happy path — produces "Debian GNU/Linux 13 (trixie)" etc.), then
# platform.platform() (Windows / macOS / container edge cases).
import platform as _platform

# Sentinel string Kodi returns when System.OSVersionInfo resolver is blocked.
BUSY_SENTINEL = 'Busy'

# Path to the systemd os-release file (Linux). Overridable for tests.
OS_RELEASE_PATH = '/etc/os-release'


def _readOSReleasePrettyName(path=None):
    """Parse /etc/os-release; return PRETTY_NAME value or None.

    Handles both quoted (`PRETTY_NAME="X"`) and unquoted (`PRETTY_NAME=X`)
    forms per the os-release(5) spec. Returns None on any error, missing key,
    or empty value so the caller can fall through to platform.platform().
    """
    if path is None:
        path = OS_RELEASE_PATH
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            for line in fh:
                if line.startswith('PRETTY_NAME='):
                    value = line.split('=', 1)[1].strip()
                    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
                        value = value[1:-1]
                    return value or None
    except (OSError, IOError, UnicodeDecodeError):
        pass
    return None


def resolveKodiOSVersion(label_value):
    """Resolve the Kodi OS version string with two-level fallback.

    label_value is what Kodi's JSON-RPC returned for `System.OSVersionInfo`.
    If it's a real value, pass through verbatim. If it's None / empty /
    whitespace / "Busy", fall back to /etc/os-release PRETTY_NAME, then
    platform.platform(). Always returns a non-empty string.
    """
    if isinstance(label_value, str):
        stripped = label_value.strip()
        if stripped and stripped != BUSY_SENTINEL:
            return label_value
    pretty = _readOSReleasePrettyName()
    if pretty:
        return pretty
    return _platform.platform()
