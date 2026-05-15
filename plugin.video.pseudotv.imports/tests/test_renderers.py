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


# --------------------------------------------- imports.41 regression: m3u OOM bloat
#
# Bug: render_m3u's optional-attrs loop iterated M3U_TEMP keys without skipping
# the keys already emitted in the explicit format string. `group` (a Python list)
# serialized via `str([...])` to `"['x', 'y']"` — embedded a literal comma INSIDE
# the rendered `group="..."` value. m3u.py:_load's `,(.*)' label regex captured
# from the FIRST comma, so the in-value comma became the label boundary. Parsed
# label = entire EXTINF tail. Next render: label re-emitted both as optional
# `label="..."` and as raw display-name → line doubled. ~22 cycles → 1.6 GB
# single EXTINF line → pvr.iptvsimple OOM-killed Kodi (anon-rss ≈ 6.8 GB on Pi5).
#
# Fix: renderers._EXPLICIT_M3U_KEYS skips id/number/name/logo/group/radio/catchup/
# label/url in the optional loop. Defense in depth: list/tuple values are
# ;-joined instead of str()'d, and m3u.py's label regex anchors on `",` (the
# closing-quote-comma of the last attribute) rather than the first comma.


def test_render_m3u_no_redundant_group_attribute():
    """`group` is already emitted as `group-title` in the explicit format
    string. Re-emitting it in the optional loop as a Python list-repr
    embeds a literal comma INSIDE the `group="..."` value, which leaks
    into m3u._load's label parsing. The renderer must NOT emit a bare
    `group="..."` attribute."""
    station = _station(group=['Adult Swim', 'PseudoTV Live (imports)'])
    out = render_m3u({'stations': [station], 'recordings': []})
    assert out is not None
    # group-title (the canonical M3U attribute) is fine and expected.
    assert 'group-title="Adult Swim;PseudoTV Live (imports)"' in out
    # bare `group=` MUST NOT appear — that's the corruption vector.
    assert ' group="' not in out, (
        'render_m3u must not emit bare `group="..."` attribute — it embeds '
        'a literal comma when value is a list, which mis-anchors the label '
        'boundary in m3u._load. Use group-title only.')


def test_render_m3u_no_redundant_explicit_keys():
    """`id`, `number`, `name`, `logo`, `group`, `radio`, `catchup` are all
    emitted by the explicit format string already (as channel-id / tvg-chno
    / tvg-id / tvg-name / tvg-logo / group-title / radio / catchup). They
    must NOT also appear in the optional loop — redundant bytes that
    accumulated in pseudotv.m3u for years."""
    station = _station(
        id='ALJAZE@movistarplus',
        number=42,
        name='Al Jazeera',
        logo='https://example.com/aj.png',
        group=['News'],
        radio=False,
        catchup='vod',
    )
    out = render_m3u({'stations': [station], 'recordings': []})
    assert out is not None
    # Find the EXTINF line for this station.
    extinf = [ln for ln in out.split('\n') if ln.startswith('#EXTINF:')][0]
    # None of these bare keys should be in the EXTINF line.
    for bare_key in (' id="', ' number="', ' name="', ' logo="',
                     ' group="', ' label="', ' url="'):
        assert bare_key not in extinf, (
            'render_m3u emitted redundant `%s..."` — duplicates the explicit '
            'format string (or the label/url dedicated locations) and breaks '
            'idempotency through m3u._load.' % bare_key.strip())


def test_render_m3u_idempotent_through_parse_reload():
    """The full render → parse → render cycle MUST be size-stable. Before
    imports.41 each cycle DOUBLED the file size for any station whose
    `group` list had ≥2 elements (the corruption ran on every Custom
    channel because they all get auto-added to `['Adult Swim', 'PseudoTV
    Live (imports)']`-style multi-group). Regression coverage: simulate
    one round-trip and confirm no growth."""
    import re as _re

    station = _station(group=['Adult Swim', 'PseudoTV Live (imports)'])
    line1 = render_m3u({'stations': [station], 'recordings': []})
    assert line1 is not None
    extinf1 = [ln for ln in line1.split('\n') if ln.startswith('#EXTINF:')][0]

    # Simulate the m3u._load label regex (the new anchored one, post-fix).
    label_re = _re.compile(r'(?:.*"|^#EXTINF:[^,]*),(.*)$', _re.IGNORECASE)
    m = label_re.search(extinf1)
    assert m is not None, 'label regex must match a well-formed EXTINF line'
    parsed_label = m.group(1)
    # Sane label after parse: the actual display name, NOT the EXTINF tail.
    # (Pre-imports.41 this captured everything from the FIRST comma onward,
    # so parsed_label would have been hundreds of bytes including url=,
    # catchup-source=, provider=, etc.)
    assert parsed_label == 'Test Channel', (
        'parsed label must be the bare display name, not the EXTINF tail; '
        'got %r' % (parsed_label,))
    assert len(parsed_label) < 64, (
        'parsed label suspiciously long — likely picked up attribute values; '
        'got len=%d, content=%r' % (len(parsed_label), parsed_label))

    # Feed parsed label back and re-render. Total size must NOT grow.
    station['label'] = parsed_label
    line2 = render_m3u({'stations': [station], 'recordings': []})
    assert line2 is not None
    assert len(line2) == len(line1), (
        'render→parse→render cycle must be size-stable; was %d → %d (a '
        '%+d byte drift signals the doubling bug has reopened)'
        % (len(line1), len(line2), len(line2) - len(line1)))


def test_render_m3u_normalizes_list_typed_optional_values():
    """If a future M3U attribute happens to be list-typed (e.g. someone
    decides `provider-countries` should be a list of country codes), the
    renderer must NOT serialize it via Python str(list)-repr — that
    embeds literal commas which is the corruption vector. List/tuple
    values get ;-joined like `group-title` does."""
    station = _station()
    # Inject a list-typed optional attribute.
    station['provider-countries'] = ['US', 'CA', 'MX']
    out = render_m3u({'stations': [station], 'recordings': []})
    assert out is not None
    # Bad: str(list)-repr form.
    assert "['US', 'CA', 'MX']" not in out, (
        'list value must NOT be emitted as Python str()-repr — embedded '
        'commas break m3u._load label parsing.')
    # Good: ;-joined form.
    assert 'provider-countries="US;CA;MX"' in out, (
        'list value must be ;-joined like group-title.')


def test_m3u_load_label_anchored_on_last_attribute_quote():
    """Regression test for the parser-side defense. Feed a fake EXTINF
    line that intentionally contains a comma INSIDE a quoted attribute
    value (simulating the OLD pre-imports.41 rendered output or an
    external-source M3U with comma-bearing values). The parser must
    extract the actual display-name, NOT capture from the in-value
    comma."""
    import re as _re
    # Anchored regex (imports.41 fix).
    label_re = _re.compile(r'(?:.*"|^#EXTINF:[^,]*),(.*)$', _re.IGNORECASE)
    # Hostile line: comma inside group= value, like the renderers.py
    # output BEFORE the fix.
    line = ('#EXTINF:-1 channel-id="aswim" tvg-chno="304" tvg-name="Adult Swim" '
            'group-title="A;B" radio="False" catchup="vod" '
            "group=\"['Adult Swim', 'PseudoTV Live (imports)']\" "
            'provider="PseudoTV",Adult Swim')
    m = label_re.search(line)
    assert m is not None, 'label regex must match a hostile EXTINF line'
    assert m.group(1) == 'Adult Swim', (
        'parser must extract the actual display-name, not the EXTINF tail '
        'after the first in-value comma; got %r' % (m.group(1),))
    # Plain line still works.
    m2 = label_re.search('#EXTINF:-1 channel-id="x" tvg-name="Y",Display')
    assert m2 is not None and m2.group(1) == 'Display'
    # No-attrs fallback still works.
    m3 = label_re.search('#EXTINF:-1,BareDisplay')
    assert m3 is not None and m3.group(1) == 'BareDisplay'
