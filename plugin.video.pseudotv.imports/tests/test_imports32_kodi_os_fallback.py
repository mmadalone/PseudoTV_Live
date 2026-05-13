# -*- coding: utf-8 -*-
"""imports.32: regression tests for the Kodi OS version fallback.

Bug: server.py's /api/system/info.json returned the literal string "Busy"
for the "Kodi OS" field whenever Kodi's `System.OSVersionInfo` InfoLabel was
busy (intermittent race observed on Debian/Pi). The dashboard's System Info
tab displayed "Busy" indefinitely until Kodi was restarted.

Fix: sysinfo_helpers.resolveKodiOSVersion falls back to /etc/os-release
PRETTY_NAME, then platform.platform(). server.py wraps the
labels.get('System.OSVersionInfo') call in the helper.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LIB  = os.path.normpath(os.path.join(HERE, '..', 'resources', 'lib'))
if LIB not in sys.path:
    sys.path.insert(0, LIB)

from sysinfo_helpers import (
    BUSY_SENTINEL,
    _readOSReleasePrettyName,
    resolveKodiOSVersion,
)


def _read(path):
    with open(path, 'r', encoding='utf-8') as fh:
        return fh.read()


# ----- _readOSReleasePrettyName -----

def test_readOSReleasePrettyName_quoted_value(tmp_path):
    p = tmp_path / 'os-release'
    p.write_text('PRETTY_NAME="Debian GNU/Linux 13 (trixie)"\nID=debian\n')
    assert _readOSReleasePrettyName(str(p)) == 'Debian GNU/Linux 13 (trixie)'


def test_readOSReleasePrettyName_unquoted_value(tmp_path):
    p = tmp_path / 'os-release'
    p.write_text('PRETTY_NAME=Alpine Linux v3.20\n')
    assert _readOSReleasePrettyName(str(p)) == 'Alpine Linux v3.20'


def test_readOSReleasePrettyName_missing_file_returns_none(tmp_path):
    p = tmp_path / 'nope'
    assert _readOSReleasePrettyName(str(p)) is None


def test_readOSReleasePrettyName_no_pretty_name_returns_none(tmp_path):
    p = tmp_path / 'os-release'
    p.write_text('ID=debian\nVERSION_ID="13"\n')
    assert _readOSReleasePrettyName(str(p)) is None


def test_readOSReleasePrettyName_empty_pretty_name_returns_none(tmp_path):
    p = tmp_path / 'os-release'
    p.write_text('PRETTY_NAME=""\n')
    assert _readOSReleasePrettyName(str(p)) is None


def test_readOSReleasePrettyName_pretty_name_first_match_wins(tmp_path):
    # Standard ordering: PRETTY_NAME typically appears once. Defensive check
    # that the first match is taken if duplicated (shouldn't happen but worth
    # documenting the behavior).
    p = tmp_path / 'os-release'
    p.write_text('PRETTY_NAME="First"\nPRETTY_NAME="Second"\n')
    assert _readOSReleasePrettyName(str(p)) == 'First'


# ----- resolveKodiOSVersion -----

def test_resolveKodiOSVersion_real_label_returns_verbatim():
    # Valid Kodi label passes through unchanged (Windows / macOS happy path
    # where the InfoLabel resolves correctly).
    assert resolveKodiOSVersion('Windows 11 Pro') == 'Windows 11 Pro'


def test_resolveKodiOSVersion_label_with_surrounding_whitespace_returns_verbatim():
    # If Kodi returns a real value with incidental whitespace, preserve it
    # exactly — operator may have reasons to want the raw label.
    assert resolveKodiOSVersion(' macOS 14.0 ') == ' macOS 14.0 '


def test_resolveKodiOSVersion_busy_falls_back(monkeypatch, tmp_path):
    p = tmp_path / 'os-release'
    p.write_text('PRETTY_NAME="Debian GNU/Linux 13 (trixie)"\n')
    monkeypatch.setattr('sysinfo_helpers.OS_RELEASE_PATH', str(p))
    assert resolveKodiOSVersion('Busy') == 'Debian GNU/Linux 13 (trixie)'


def test_resolveKodiOSVersion_none_falls_back(monkeypatch, tmp_path):
    p = tmp_path / 'os-release'
    p.write_text('PRETTY_NAME="Debian GNU/Linux 13 (trixie)"\n')
    monkeypatch.setattr('sysinfo_helpers.OS_RELEASE_PATH', str(p))
    assert resolveKodiOSVersion(None) == 'Debian GNU/Linux 13 (trixie)'


def test_resolveKodiOSVersion_empty_string_falls_back(monkeypatch, tmp_path):
    p = tmp_path / 'os-release'
    p.write_text('PRETTY_NAME="Debian GNU/Linux 13 (trixie)"\n')
    monkeypatch.setattr('sysinfo_helpers.OS_RELEASE_PATH', str(p))
    assert resolveKodiOSVersion('') == 'Debian GNU/Linux 13 (trixie)'


def test_resolveKodiOSVersion_whitespace_only_falls_back(monkeypatch, tmp_path):
    p = tmp_path / 'os-release'
    p.write_text('PRETTY_NAME="Debian GNU/Linux 13 (trixie)"\n')
    monkeypatch.setattr('sysinfo_helpers.OS_RELEASE_PATH', str(p))
    assert resolveKodiOSVersion('   ') == 'Debian GNU/Linux 13 (trixie)'


def test_resolveKodiOSVersion_no_os_release_uses_platform_platform(monkeypatch, tmp_path):
    import platform
    p = tmp_path / 'nope'
    monkeypatch.setattr('sysinfo_helpers.OS_RELEASE_PATH', str(p))
    result = resolveKodiOSVersion('Busy')
    assert result == platform.platform()
    assert isinstance(result, str) and result, (
        "platform.platform() must always return a non-empty string"
    )


def test_resolveKodiOSVersion_os_release_empty_pretty_name_uses_platform(monkeypatch, tmp_path):
    import platform
    p = tmp_path / 'os-release'
    p.write_text('PRETTY_NAME=""\nID=alpine\n')
    monkeypatch.setattr('sysinfo_helpers.OS_RELEASE_PATH', str(p))
    assert resolveKodiOSVersion('Busy') == platform.platform()


def test_resolveKodiOSVersion_busy_sentinel_is_literal_string():
    assert BUSY_SENTINEL == 'Busy'


def test_resolveKodiOSVersion_non_string_input_falls_back(monkeypatch, tmp_path):
    # Defensive: if labels.get() somehow returned a non-string (shouldn't,
    # JSON-RPC returns strings, but be safe), don't crash — fall back.
    p = tmp_path / 'os-release'
    p.write_text('PRETTY_NAME="Debian GNU/Linux 13 (trixie)"\n')
    monkeypatch.setattr('sysinfo_helpers.OS_RELEASE_PATH', str(p))
    assert resolveKodiOSVersion(42) == 'Debian GNU/Linux 13 (trixie)'
    assert resolveKodiOSVersion({}) == 'Debian GNU/Linux 13 (trixie)'


# ----- server.py wiring -----

def test_server_imports_resolveKodiOSVersion():
    src = _read(os.path.join(LIB, 'server.py'))
    # Tolerate the project's aligned-import-whitespace style.
    assert re.search(r'from\s+sysinfo_helpers\s+import\s+resolveKodiOSVersion', src), (
        "server.py must import resolveKodiOSVersion from sysinfo_helpers"
    )


def test_server_wraps_OSVersionInfo_label_get_in_helper():
    src = _read(os.path.join(LIB, 'server.py'))
    assert "resolveKodiOSVersion(labels.get('System.OSVersionInfo'))" in src, (
        "server.py must wrap labels.get('System.OSVersionInfo') in "
        "resolveKodiOSVersion() inside _build_sysinfo_payload"
    )


def test_server_no_bare_OSVersionInfo_label_get():
    """Only the wrapped form may appear anywhere in server.py.

    A bare `labels.get('System.OSVersionInfo')` outside the wrapper would
    re-introduce the imports.32 "Busy" bug.
    """
    src = _read(os.path.join(LIB, 'server.py'))
    stripped = src.replace(
        "resolveKodiOSVersion(labels.get('System.OSVersionInfo'))", ''
    )
    assert "labels.get('System.OSVersionInfo')" not in stripped, (
        "server.py has a bare labels.get('System.OSVersionInfo') outside "
        "the resolveKodiOSVersion wrapper — would re-introduce the 'Busy' bug"
    )


def test_server_still_requests_OSVersionInfo_label():
    """The JSON-RPC getInfoLabels call must still include System.OSVersionInfo.

    If someone "fixed" the Busy bug by dropping the request entirely, we'd
    lose the real value when Kodi DOES resolve it correctly. The fallback
    is a fallback, not a replacement.
    """
    src = _read(os.path.join(LIB, 'server.py'))
    assert "'System.OSVersionInfo'" in src, (
        "server.py must still request System.OSVersionInfo from JSON-RPC — "
        "the fallback is a safety net, not a replacement"
    )
