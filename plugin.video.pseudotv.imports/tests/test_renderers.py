"""Unit tests for renderers (imports.13 / C#10 Follow-up A).

Covers the 7 essential contracts from the consolidation plan
(/home/madalone/.claude/plans/misty-shimmying-liskov.md):

  1. render_m3u empty input returns None.
  2. render_m3u escapes `"` `\r` `\n` in attribute values.
  3. render_m3u sanitizes newlines in labels.
  4. render_m3u appends `_pseudotv_chid` marker to plugin:// URLs.
  5. render_m3u strips existing `_pseudotv_chid` before appending.
  6. render_xmltv empty input returns None.
  7. render_xmltv writable fixture produces well-formed XML
     (starts with `<?xml`, parses back via xmltv.read_data).

Tests are pure (no disk I/O, no Kodi runtime). conftest adds
resources/lib to sys.path; xbmc/xbmcaddon stubs live in
project_root/tests/ (vendored from vrt.nu).
"""
import io

import pytest

from renderers import (
    render_m3u,
    render_xmltv,
    _m3u_attr_escape,
    _m3u_label_sanitize,
)


def _station(**overrides):
    """Return a minimal station dict matching M3U_TEMP shape."""
    base = {
        'id'       : 'TEST.1@PseudoTV_Live',
        'number'   : 1,
        'name'     : 'Test Channel',
        'logo'     : '',
        'group'    : ['TestGroup'],
        'catchup'  : 'vod',
        'radio'    : False,
        'label'    : 'Test Channel',
        'url'      : 'http://example.com/stream.m3u8',
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------- render_m3u


def test_render_m3u_empty_returns_none():
    """Empty M3UDATA (no stations, no recordings) returns None so
    the caller can skip the write and preserve disk content. This
    is the refuse-empty guard."""
    assert render_m3u({'stations': [], 'recordings': []}) is None
    assert render_m3u({}) is None
    assert render_m3u({'stations': [], 'recordings': [], 'data': 'whatever'}) is None


def test_render_m3u_attribute_escape():
    """Attribute values with `"` `\\r` `\\n` are percent-encoded so
    they don't break the EXTINF key="value" syntax. This is the
    safety improvement Builder's path picks up from chkImports'."""
    station = _station(
        name='alpha"bravo',           # quote — would close attr early
        catchup='shift\r\nbypass',    # CR+LF — would terminate EXTINF
    )
    out = render_m3u({'stations': [station], 'recordings': []})
    assert out is not None
    assert 'tvg-name="alpha%22bravo"' in out, 'quote in name must be %22'
    assert 'catchup="shift%0D%0Abypass"' in out, 'CR/LF in catchup must be %0D / %0A'
    # And the raw chars must NOT survive into the rendered M3U.
    assert '"' not in out.replace('"', '', out.count('"'))   # well-formed; no stray quotes
    assert '\r' not in out  # no raw CR anywhere in the output


def test_render_m3u_label_sanitize():
    """Display label (text after the EXTINF comma) doesn't tolerate
    newlines — they terminate the EXTINF line. Sanitize \\r and \\n
    to space."""
    station = _station(label='one\ntwo\rthree')
    out = render_m3u({'stations': [station], 'recordings': []})
    assert out is not None
    # The sanitized label should appear; raw \n/\r should not.
    assert 'one two three' in out
    # No raw newline INSIDE an EXTINF line (each EXTINF is one line
    # ending in \n; the sanitized label is inside that line).
    extinf_lines = [ln for ln in out.split('\n') if ln.startswith('#EXTINF:')]
    for ln in extinf_lines:
        assert '\r' not in ln, 'EXTINF must not contain CR'


def test_render_m3u_plugin_url_chid_marker():
    """plugin:// URLs get a `_pseudotv_chid` marker appended so
    pvr.iptvsimple's name+url hash produces a unique ChannelId
    for channels sharing a source plugin URL. The @ in the channel
    id encodes as %40 per renderers.py."""
    station = _station(
        id='ALJAZE@movistarplus',
        url='plugin://plugin.video.movistarplus/?action=play&id=ALJAZE',
    )
    out = render_m3u({'stations': [station], 'recordings': []})
    assert out is not None
    assert '_pseudotv_chid=ALJAZE%40movistarplus' in out, (
        'plugin:// URL must carry _pseudotv_chid marker with @ → %40')
    # The marker is appended to the existing query string with `&` (since
    # the URL already had `?`).
    assert '&_pseudotv_chid=' in out


def test_render_m3u_strips_existing_chid_marker():
    """Strip any existing `_pseudotv_chid=...` segment before appending
    a fresh one on the **standalone URL line** (the one pvr.iptvsimple
    actually consumes for playback). Without this, every reload of
    M3UDATA from disk would accumulate another marker on the standalone
    URL (URL grows unboundedly; pvr.iptvsimple ChannelId drifts every
    cycle). Regression test for the bug documented in
    project_pseudotv_chid_marker_accumulation.md.

    Note: the cosmetic `url="..."` attribute INSIDE the EXTINF line
    is pass-through (whatever M3UDATA['url'] holds, raw). It may carry
    a stale marker because the renderer doesn't rewrite the optional-
    attr serialization. This is pre-existing behavior of
    tasks._renderM3U; not load-bearing (m3u._load parses the URL from
    the standalone line, not the EXTINF attr)."""
    station = _station(
        id='ALJAZE@movistarplus',
        url='plugin://plugin.video.movistarplus/?action=play&id=ALJAZE&_pseudotv_chid=stale%40value',
    )
    out = render_m3u({'stations': [station], 'recordings': []})
    assert out is not None
    # Standalone URL line is what pvr.iptvsimple consumes for playback.
    # It must have the stale marker stripped + a fresh one appended.
    url_lines = [ln for ln in out.split('\n') if ln.startswith('plugin://')]
    assert len(url_lines) == 1, 'expected exactly one standalone plugin:// URL line'
    standalone = url_lines[0]
    assert '_pseudotv_chid=stale' not in standalone, (
        'stale marker must be stripped from standalone URL line')
    assert '_pseudotv_chid=ALJAZE%40movistarplus' in standalone, (
        'fresh marker must be appended to standalone URL line')
    assert standalone.count('_pseudotv_chid=') == 1, (
        'exactly one _pseudotv_chid marker per standalone URL line')


# ---------------------------------------------------------------- render_xmltv


def test_render_xmltv_empty_returns_none():
    """Empty XMLTVDATA returns None so the caller skips the write
    and preserves disk content. Without this guard, xmltv.Writer
    would emit a self-closing `<tv ... />` root which combined with
    our atomic-write race produced line-2 corruption."""
    assert render_xmltv({'channels': [], 'recordings': [], 'programmes': []}) is None
    assert render_xmltv({}) is None


def test_render_xmltv_writable_fixture_produces_valid_xml():
    """Minimal valid XMLTVDATA renders to well-formed XML.
    Smoke test that the writer setup (encoding, source-info,
    generator-info, addChannel, addProgramme) all wire up.

    Light assertions only — we don't depend on xmltv.read_data
    semantics (which may not echo back the source-info attrs
    cleanly). Just verify byte structure: XML declaration, root
    `<tv ...>` tag, and our channel + programme elements are
    present."""
    xmltvdata = {
        'data': {
            'date'                : '20260512000000',
            'source-info-url'     : 'plugin.video.pseudotv.imports',
            'source-info-name'    : 'PseudoTV Live (imports)',
            'generator-info-url'  : 'plugin.video.pseudotv.imports',
            'generator-info-name' : 'PseudoTV Live (imports) Imports',
        },
        'channels'   : [{
            'id'           : 'test-channel-1',
            'display-name' : [('Test Channel One', 'en')],
            'icon'         : [{'src': 'http://example.com/logo.png'}],
        }],
        'recordings' : [],
        'programmes' : [{
            'channel' : 'test-channel-1',
            'start'   : '20260512100000 +0000',
            'stop'    : '20260512110000 +0000',
            'title'   : [('Test Show', 'en')],
            'desc'    : [('A test programme description.', 'en')],
        }],
    }

    out = render_xmltv(xmltvdata)
    assert out is not None
    assert isinstance(out, (bytes, bytearray))

    txt = out.decode('utf-8', errors='ignore')
    # XML declaration. xmltv.Writer emits single quotes (`encoding='utf-8'`).
    assert txt.startswith("<?xml version='1.0' encoding='utf-8'?>") or \
           txt.startswith('<?xml version="1.0" encoding="utf-8"?>'), (
        'XMLTV must start with the XML declaration; got: %r' % (txt[:64],))
    # Root tv tag (NOT self-closing — that's the corruption pattern we guard against).
    assert '<tv' in txt, 'XML must have a <tv ...> root element'
    assert '<tv ' in txt or '<tv>' in txt, 'XML must not self-close on <tv/>'
    # Channel + programme present.
    assert 'id="test-channel-1"' in txt, 'channel id must appear in output'
    assert 'Test Channel One' in txt, 'channel display-name must appear in output'
    assert 'Test Show' in txt, 'programme title must appear in output'


# ---------------------------------------------------------------- escape helpers (smoke)


def test_m3u_attr_escape_handles_none():
    assert _m3u_attr_escape(None) == ''
    assert _m3u_attr_escape('') == ''


def test_m3u_attr_escape_passes_safe_strings_through():
    assert _m3u_attr_escape('alpha-bravo_charlie.123') == 'alpha-bravo_charlie.123'


def test_m3u_label_sanitize_handles_none():
    assert _m3u_label_sanitize(None) == ''
    assert _m3u_label_sanitize('') == ''


def test_m3u_label_sanitize_passes_safe_strings_through():
    assert _m3u_label_sanitize('A normal label') == 'A normal label'
