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
    """Return a minimal station dict matching M3U_TEMP shape.

    imports.42: default `group` is a TWO-element list (matches every
    real-world Custom channel — the addon auto-pairs the channel-specific
    group with the addon-default group `PseudoTV Live (imports)`). This
    forces every existing test that doesn't override `group` to exercise
    the str(list)-with-comma path that triggered imports.41's render-loop
    bug. Pre-imports.42 the default was `['TestGroup']` (one element,
    no comma in str-repr) — the bug was invisible to every existing
    test fixture.
    """
    base = {
        'id'       : 'TEST.1@PseudoTV_Live',
        'number'   : 1,
        'name'     : 'Test Channel',
        'logo'     : '',
        'group'    : ['TestGroup', 'PseudoTV Live (imports)'],
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


# --------------------------------------------- imports.42: prevention guards
#
# Four reinforcing safety nets so a future regression of the imports.41 class
# of bug (silent file-bloat from any cause) is caught at cycle ≤10 instead of
# cycle ~22, preserves disk content, and is loud in logs:
#
#   A. Round-trip idempotency tests with adversarial inputs (this block, below)
#   B. Size circuit-breaker in renderers.write_atomic
#   C. LOGINFO success line on writes with channel_count + size
#   D. _station() default fixture now uses a multi-element `group` (already
#      applied at the fixture definition above; every existing test now
#      exercises the str(list)-with-comma path)
#
# See /home/madalone/.claude/plans/declarative-stirring-rainbow.md for full
# scope rationale + plan.


# ---- A. Round-trip idempotency tests with adversarial inputs ----

# Anchored label regex — matches m3u.py:_load and m3u.parseExternalSource
# post-imports.41. Captured at module scope so the helper + each adversarial
# test references the same pattern.
import re as _re
_LABEL_RE = _re.compile(r'(?:.*"|^#EXTINF:[^,]*),(.*)$', _re.IGNORECASE)

# EXTINF attribute regexes — mirror m3u.py:_load line 122-141 (the production
# parser the round-trip test must agree with). Non-greedy `(.*?)` inside
# quotes; case-insensitive.
_ATTR_RE = {
    'id'                : _re.compile(r'tvg-id="(.*?)"'             , _re.IGNORECASE),
    'name'              : _re.compile(r'tvg-name="(.*?)"'           , _re.IGNORECASE),
    'group_title'       : _re.compile(r'group-title="(.*?)"'        , _re.IGNORECASE),
    'number'            : _re.compile(r'tvg-chno="(.*?)"'           , _re.IGNORECASE),
    'logo'              : _re.compile(r'tvg-logo="(.*?)"'           , _re.IGNORECASE),
    'radio'             : _re.compile(r'radio="(.*?)"'              , _re.IGNORECASE),
    'catchup'           : _re.compile(r'catchup="(.*?)"'            , _re.IGNORECASE),
    'catchup-source'    : _re.compile(r'catchup-source="(.*?)"'     , _re.IGNORECASE),
    'provider'          : _re.compile(r'provider="(.*?)"'           , _re.IGNORECASE),
    'provider-type'     : _re.compile(r'provider-type="(.*?)"'      , _re.IGNORECASE),
    'provider-logo'     : _re.compile(r'provider-logo="(.*?)"'      , _re.IGNORECASE),
}


def _parse_extinf_line(line, url_line):
    """Mirror m3u.py:_load EXTINF extraction (lines 119-198). Returns a
    station dict shaped like M3U_TEMP (post-imports.41 regex anchoring).

    Used by `_assert_render_parse_render_byte_equal` below for the
    round-trip assertion. Kept as a pure helper at test scope so we
    don't have to instantiate the M3U class (which would require tmpdir
    + globals + writer_lock setup — heavyweight for unit tests).
    """
    out = {}
    # Label (post-imports.41 anchored regex).
    m = _LABEL_RE.search(line)
    if m: out['label'] = m.group(1)
    # Other attribute fields.
    for key, pat in _ATTR_RE.items():
        m2 = pat.search(line)
        if m2 is None: continue
        val = m2.group(1)
        # Mirror m3u._load:165-170 — typed coercions.
        if key == 'number':
            try:    val = int(val)
            except (TypeError, ValueError):
                try:    val = float(val)
                except (TypeError, ValueError): pass
        elif key == 'radio':
            val = (val or '').lower() == 'true'
        elif key == 'group_title':
            # Mirror m3u._load:168 — split on `;`, sorted, deduped, falsy-filtered.
            val = sorted({_f for _f in val.split(';') if _f})
            out['group'] = val
            continue
        out[key] = val
    # URL from the URL line (m3u._load:197).
    if url_line: out['url'] = url_line
    return out


def _assert_render_parse_render_byte_equal(station, ctx=''):
    """Run station through render → simulate m3u._load's EXTINF parse →
    re-render. Assert the second render is byte-identical to the first.

    Catches any regression where renderer + parser drift such that
    `parse(render(x))` produces a station that re-renders to different
    bytes. THIS IS THE FAMILY OF BUGS imports.41 belonged to — and the
    pre-imports.42 test suite wouldn't have caught it because all
    fixtures used single-element groups (no comma in str-repr).

    The helper uses the imports.41 anchored label regex + the m3u._load
    attribute regex map. The architecture extraction of these regexes
    into a pure module function on m3u.py (so production code + tests
    share one implementation) is parked for imports.43 — see plan's
    Out-of-scope section.
    """
    line1 = render_m3u({'stations': [station], 'recordings': []})
    assert line1 is not None, '%s: first render returned None' % ctx
    extinf_lines = [ln for ln in line1.split('\n') if ln.startswith('#EXTINF:')]
    assert len(extinf_lines) == 1, ('%s: expected exactly one #EXTINF line, '
                                    'got %d' % (ctx, len(extinf_lines)))
    # Mirror m3u._load's scan-forward for the URL line (line 174-197) —
    # the standalone non-directive line right after EXTINF.
    after_extinf = line1.split('\n')[line1.split('\n').index(extinf_lines[0]) + 1]

    # Reconstitute the station as m3u._load would.
    parsed_attrs = _parse_extinf_line(extinf_lines[0], after_extinf)
    # Seed from the input station so non-emitted/non-parsed fields
    # (favorite, realtime, etc) survive — render_m3u + _load drift
    # would still surface via the byte-equality check below.
    reconstituted = dict(station)
    reconstituted.update(parsed_attrs)
    # m3u._load:201-202 fallback for name/label cross-fill.
    reconstituted['name']  = reconstituted.get('name')  or reconstituted.get('label') or ''
    reconstituted['label'] = reconstituted.get('label') or reconstituted.get('name')  or ''

    line2 = render_m3u({'stations': [reconstituted], 'recordings': []})
    assert line2 is not None, '%s: second render returned None' % ctx
    assert line2 == line1, (
        '%s: render→parse→render not byte-stable; drift = %+d bytes\n'
        'line1[:300] = %r\nline2[:300] = %r'
        % (ctx, len(line2) - len(line1), line1[:300], line2[:300])
    )


def test_render_m3u_roundtrip_multi_element_group_two():
    """2-element group — the imports.41 corruption shape. Triggered the
    doubling in production for every Custom channel."""
    _assert_render_parse_render_byte_equal(
        _station(group=['Adult Swim', 'PseudoTV Live (imports)']),
        ctx='2-element group')


def test_render_m3u_roundtrip_multi_element_group_three():
    """3-element group — extra layers of comma-bearing content; same
    failure mode, more violent if regression occurs."""
    _assert_render_parse_render_byte_equal(
        _station(group=['Adult Swim', 'Cartoons', 'PseudoTV Live (imports)']),
        ctx='3-element group')


def test_render_m3u_roundtrip_quote_in_name():
    """`"` in `name` must URL-encode to %22 (renderer's _m3u_attr_escape).
    Parser reads `%22` back as `%22` (no decode step) — re-render emits
    identical bytes. Verifies the escape/parse pair is symmetric."""
    _assert_render_parse_render_byte_equal(
        _station(name='alpha"bravo'), ctx='quote in name')


def test_render_m3u_roundtrip_comma_in_label():
    """Labels can legitimately contain commas per M3U spec (e.g.
    `,Channel, Inc.`). Parser must NOT mis-anchor on the in-label comma
    — that's the imports.41 fix's parser-side defense in action."""
    _assert_render_parse_render_byte_equal(
        _station(label='Channel, Inc.'), ctx='comma in label')


def test_render_m3u_roundtrip_newline_in_name():
    """`\\n` in `name` must encode to %0A (renderer's _m3u_attr_escape) —
    otherwise it terminates the EXTINF line. Round-trip recovers the
    encoded form, not the raw newline."""
    _assert_render_parse_render_byte_equal(
        _station(name='one\ntwo'), ctx='newline in name')


def test_render_m3u_roundtrip_long_url():
    """2 KB URL with many query params — common with catchup template URLs
    (e.g. `?action=play&vid={catchup-id}&now={lutc}&...`). URL line lives
    after the EXTINF on its own line, so round-trip preserves it verbatim."""
    long_url = 'plugin://x/?' + '&'.join('k%d=v%d' % (i, i) for i in range(200))
    _assert_render_parse_render_byte_equal(
        _station(url=long_url), ctx='2 KB URL')


def test_render_m3u_roundtrip_empty_optional_fields():
    """None / empty string for several optional fields — renderer's
    `if key in opts and str(value)` filter must skip cleanly; parser
    must default the missing fields without crashing."""
    _assert_render_parse_render_byte_equal(
        _station(logo='', catchup='', label=None),
        ctx='empty optional fields')


def test_render_m3u_roundtrip_unicode_in_name():
    """Non-ASCII characters in name — the operator's primary imports are
    Spanish/Catalan (autonomiques, movistarplus). UTF-8 byte stream
    must round-trip identical."""
    _assert_render_parse_render_byte_equal(
        _station(name='Tres en clau de Re'), ctx='unicode in name')


# ---- B. Size circuit-breaker tests ----
#
# All breaker tests use a monkeypatched `held()` (no-op context manager) and
# the existing `FileAccess.translatePath` which already handles tmp_path-style
# absolute paths as pass-through. The actual write goes to disk via tmp_path
# so we can assert the on-disk file content (or its non-existence).


@pytest.fixture
def no_op_writer_lock(monkeypatch):
    """Disable the WRITER_LOCK so circuit-breaker tests don't need a
    full lock-thread environment. write_atomic still runs its full
    body otherwise (including the breaker, the open/write/fsync/replace
    sequence, and the log emit)."""
    from contextlib import contextmanager
    @contextmanager
    def _noop_held(ctx=''):
        yield
    monkeypatch.setattr('renderers.held', _noop_held)


def _bytes_of(n):
    """Allocate exactly n bytes for size-cap testing."""
    return b'X' * n


def test_compute_size_limit_arithmetic():
    """Unit test the threshold helper directly — no I/O. Covers each
    branch of min/max + the None default."""
    from renderers import (
        _compute_size_limit,
        _WRITE_ATOMIC_FLOOR_BYTES,
        _WRITE_ATOMIC_HARD_CAP_BYTES,
        _WRITE_ATOMIC_BYTES_PER_CHANNEL,
    )
    # None caller → hard cap (graceful default).
    assert _compute_size_limit(None) == _WRITE_ATOMIC_HARD_CAP_BYTES
    # 0 channels → floor wins (max(FLOOR, 0) → FLOOR).
    assert _compute_size_limit(0) == _WRITE_ATOMIC_FLOOR_BYTES
    # 1 channel × 1 MB = 1 MB < FLOOR (5 MB) → floor wins.
    assert _compute_size_limit(1) == _WRITE_ATOMIC_FLOOR_BYTES
    # 78 channels × 1 MB = 78 MB < HARD_CAP (100 MB), > FLOOR (5 MB) → scaled.
    assert _compute_size_limit(78) == 78 * _WRITE_ATOMIC_BYTES_PER_CHANNEL
    # 1000 channels × 1 MB = 1 GB > HARD_CAP → hard cap wins.
    assert _compute_size_limit(1000) == _WRITE_ATOMIC_HARD_CAP_BYTES


def test_write_atomic_refuses_above_per_channel_limit(tmp_path, no_op_writer_lock):
    """78 channels × 1 MB = 78 MB cap; 80 MB content → refused.
    Disk content preserved (file never created)."""
    from renderers import write_atomic
    target = tmp_path / 'pseudotv.m3u'
    huge = _bytes_of(80 * 1024 * 1024)
    write_atomic(str(target), huge, channel_count=78)
    assert not target.exists(), (
        'write_atomic should have refused the 80 MB write (cap = 78 MB for '
        '78 channels). File should not exist on disk.')


def test_write_atomic_passes_below_per_channel_limit(tmp_path, no_op_writer_lock):
    """78 channels × 1 MB = 78 MB cap; 1 MB content → written normally."""
    from renderers import write_atomic
    target = tmp_path / 'pseudotv.m3u'
    payload = _bytes_of(1 * 1024 * 1024)
    write_atomic(str(target), payload, channel_count=78)
    assert target.exists() and target.read_bytes() == payload


def test_write_atomic_refuses_above_hard_cap_regardless_of_count(tmp_path, no_op_writer_lock):
    """1000 channels would scale to 1 GB, but the 100 MB hard cap wins.
    200 MB content → refused even though scaled limit would have allowed it."""
    from renderers import write_atomic
    target = tmp_path / 'pseudotv.m3u'
    huge = _bytes_of(200 * 1024 * 1024)
    write_atomic(str(target), huge, channel_count=1000)
    assert not target.exists()


def test_write_atomic_uses_floor_for_low_channel_count(tmp_path, no_op_writer_lock):
    """1 channel × 1 MB = 1 MB scaled, but the 5 MB floor wins.
    4 MB → written (under floor). 6 MB → refused (above floor)."""
    from renderers import write_atomic
    # 4 MB under floor — should succeed.
    target1 = tmp_path / 'under.m3u'
    write_atomic(str(target1), _bytes_of(4 * 1024 * 1024), channel_count=1)
    assert target1.exists()
    # 6 MB above floor (with 1 channel × 1 MB scaling = 1 MB < FLOOR = 5 MB,
    # so effective cap = 5 MB) — should be refused.
    target2 = tmp_path / 'over.m3u'
    write_atomic(str(target2), _bytes_of(6 * 1024 * 1024), channel_count=1)
    assert not target2.exists()


def test_write_atomic_no_channel_count_uses_hard_cap_only(tmp_path, no_op_writer_lock):
    """channel_count=None → hard cap (100 MB) only.
    50 MB → written (under hard cap). 200 MB → refused."""
    from renderers import write_atomic
    # 50 MB — well under hard cap — written.
    target1 = tmp_path / 'under.m3u'
    write_atomic(str(target1), _bytes_of(50 * 1024 * 1024))
    assert target1.exists()
    # 200 MB — over hard cap — refused.
    target2 = tmp_path / 'over.m3u'
    write_atomic(str(target2), _bytes_of(200 * 1024 * 1024))
    assert not target2.exists()


@pytest.fixture
def captured_logs(monkeypatch):
    """Capture all `renderers._log(msg, level)` calls into a list.

    The addon's `logger.log()` gates INFO/DEBUG emissions behind the
    `Debug_Enable` setting — by default INFO/DEBUG calls are silent in
    the test environment, so `capsys` won't see them. Intercepting at
    `renderers._log` captures the calls verbatim with their level tags
    regardless of the addon's runtime log filtering. Returns a list of
    `(level, msg)` tuples in call order.
    """
    captured = []
    def fake_log(msg, level=0):
        captured.append((level, msg))
    monkeypatch.setattr('renderers._log', fake_log)
    return captured


def test_write_atomic_circuit_breaker_logs_payload_preview(tmp_path, no_op_writer_lock, captured_logs):
    """When the breaker fires, the log message must include the first
    1 KB of the offending payload (forensics aid). Verify by triggering
    a refuse and reading the captured _log calls."""
    import xbmc
    from renderers import write_atomic, _WRITE_ATOMIC_PREVIEW_BYTES
    target = tmp_path / 'evil.m3u'
    # Recognizable payload start so we can assert it appears in the log.
    marker = b'BLOAT_MARKER_START_'
    payload = marker + _bytes_of(80 * 1024 * 1024)
    write_atomic(str(target), payload, channel_count=78)
    # File refused.
    assert not target.exists(), 'breaker should have refused 80 MB write'
    # Find the REFUSING log line.
    refuse_lines = [(lvl, m) for lvl, m in captured_logs if 'REFUSING' in m]
    assert len(refuse_lines) == 1, (
        'breaker fire must emit exactly one REFUSING log line; '
        'captured: %r' % (captured_logs,))
    level, msg = refuse_lines[0]
    assert level == xbmc.LOGWARNING, (
        'REFUSING log must be at LOGWARNING level; got level=%d, msg=%r'
        % (level, msg))
    assert marker.decode() in msg, (
        'breaker fire must include the first %d bytes of the offending '
        'payload in the log message (got: %r)'
        % (_WRITE_ATOMIC_PREVIEW_BYTES, msg[:500]))


# ---- C. Logging tests (LOGINFO vs LOGDEBUG by channel_count presence) ----


def test_write_atomic_logs_info_when_channel_count_supplied(tmp_path, no_op_writer_lock, captured_logs):
    """When channel_count is supplied, the success log line must be at
    LOGINFO level + include both byte count and channel count. This is
    the imports.42 "make regressions visible at default Kodi log level"
    behavior — operators scanning kodi.log see one INFO line per write
    with size + count, so silent file bloat becomes detectable without
    flipping debug logging on."""
    import xbmc
    from renderers import write_atomic
    target = tmp_path / 'pseudotv.m3u'
    write_atomic(str(target), b'hello', channel_count=42)
    # Find the "wrote N bytes" line.
    wrote_lines = [(lvl, m) for lvl, m in captured_logs if 'wrote' in m and 'bytes' in m]
    assert len(wrote_lines) == 1, (
        'expected exactly one wrote-bytes log line; captured: %r'
        % (captured_logs,))
    level, msg = wrote_lines[0]
    assert level == xbmc.LOGINFO, (
        'with channel_count supplied, success log must be at LOGINFO; '
        'got level=%d, msg=%r' % (level, msg))
    assert '(42 channels)' in msg, (
        'log line must mention the channel count when supplied; got: %r'
        % (msg,))
    assert '5 bytes' in msg, 'log line must mention the byte count; got: %r' % (msg,)


def test_write_atomic_logs_debug_when_no_channel_count(tmp_path, no_op_writer_lock, captured_logs):
    """When channel_count=None, the success log stays at LOGDEBUG (pre-
    imports.42 shape) and OMITS the channels-count phrase. Backward-compat
    for any caller that hasn't been updated to plumb the count through."""
    import xbmc
    from renderers import write_atomic
    target = tmp_path / 'pseudotv.m3u'
    write_atomic(str(target), b'hello')   # channel_count not supplied
    wrote_lines = [(lvl, m) for lvl, m in captured_logs if 'wrote' in m and 'bytes' in m]
    assert len(wrote_lines) == 1, (
        'expected exactly one wrote-bytes log line; captured: %r'
        % (captured_logs,))
    level, msg = wrote_lines[0]
    assert level == xbmc.LOGDEBUG, (
        'without channel_count, success log must stay at LOGDEBUG; '
        'got level=%d, msg=%r' % (level, msg))
    assert '5 bytes' in msg, 'log line must mention byte count; got: %r' % (msg,)
    assert 'channels)' not in msg, (
        'no channel_count → no `(N channels)` phrase in the log; got: %r' % (msg,))
