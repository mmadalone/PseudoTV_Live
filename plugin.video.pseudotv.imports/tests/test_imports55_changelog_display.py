# -*- coding: utf-8 -*-
"""imports.55: the version-change changelog popup shows only the newest
version's entry (operator-facing part), not the whole changelog.

changelog_helpers.latestEntry is import-light (stdlib only) so these are
real functional tests; a source-scan pins utilities.showChangelog to the
helper so the popup can't silently regress to the full-file dump.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LIB  = os.path.normpath(os.path.join(HERE, '..', 'resources', 'lib'))
if LIB not in sys.path:
    sys.path.insert(0, LIB)

import changelog_helpers  # noqa: E402


SAMPLE = """### NOTICE: banner line ####

v.0.8.0+imports.55
- Fixed: short operator line one.
- New!: short operator line two.
--- engineering notes ---
- giant engineering narrative that must never reach the popup

v.0.8.0+imports.54
- Fixed: older entry that must not be shown.
"""


def test_latestEntry_returns_only_newest_block():
    out = changelog_helpers.latestEntry(SAMPLE)
    assert out.startswith('v.0.8.0+imports.55')
    assert 'short operator line one' in out
    assert 'imports.54' not in out
    assert 'older entry' not in out


def test_latestEntry_truncates_at_marker():
    out = changelog_helpers.latestEntry(SAMPLE)
    assert 'engineering narrative' not in out
    assert '--- engineering notes' not in out


def test_latestEntry_skips_notice_banner():
    out = changelog_helpers.latestEntry(SAMPLE)
    assert 'NOTICE' not in out


def test_latestEntry_no_header_falls_back_to_full_text():
    txt = "just some text\nwith no version headers"
    assert changelog_helpers.latestEntry(txt) == txt


def test_shipped_changelog_yields_short_latest_entry():
    """The REAL changelog.txt must produce a popup-sized latest entry —
    guards both the helper and the entry-format convention (short bullets
    before the marker)."""
    path = os.path.normpath(os.path.join(HERE, '..', 'changelog.txt'))
    with open(path, 'r', encoding='utf-8') as fh:
        raw = fh.read()
    out = changelog_helpers.latestEntry(raw)
    assert out.startswith('v.'), "latest entry must start at a version header"
    assert len(out) < 2500, (
        "latest changelog entry's operator part is %d chars — keep the short "
        "bullets above the '--- engineering notes ---' marker" % len(out))
    assert out.count('\nv.') == 0, "must contain exactly one version header"


def test_showChangelog_routes_through_latestEntry():
    with open(os.path.join(LIB, 'utilities.py'), 'r', encoding='utf-8') as fh:
        src = fh.read()
    body = re.search(r"def showChangelog\(.*?(?=\n    @|\n    def )", src, re.DOTALL).group(0)
    assert 'latestEntry' in body, \
        "imports.55: showChangelog must trim via changelog_helpers.latestEntry"
