# -*- coding: utf-8 -*-
"""imports.72: the Up next card always shows the Next line.

On 2026-11-21 the Up next card (overlay.Overlay: Python controls on the full-screen video window) showed the
right "You're Watching:" line and the next show's picture, but no "Next @ <time>: <next>" line. Both lines went
into ONE Kodi text box, 960x70, in the skin's font13 (the user's skin has no font27): Play Regular 29, 34 px
lines. A text box wraps a line wider than itself. "You're Watching: Teenage Mutant Ninja Turtles on Saturday
Morning TV" is 967 px, so it took two lines and the Next line became the third, 68 px down, where the box's
70 px showed none of its ink. The 'between shows' card (background.xml 40003) had the same box. imports.72 makes
both a LABEL of the same size and place with the same text: a label never wraps, draws each [CR] line one font
line apart and cuts only a too-wide line with "...".

A fake control can't show any of that: what Kodi draws depends on the control's type and on the font. These
tests lay the text out and draw it with kodi_faithful's ports of Kodi 21.3's guilib and a table of the user's
font measured by Kodi's own rules (tests/fixtures/font13_play_regular_29.json, made by
~/.claude/plans/upnext-lines/tools/make_fixture.py):
  - the stand-ins' own tests: Kodi's rules one by one, and the fixture's widths of HarfBuzz-shaped lines;
  - the proof: the lines quoted from 39956387 (imports.71), run through test_imports64's rig of the real
    Overlay, lose the Next line of the 2026-11-21 card, and the quoted window file does the same; the real
    Overlay (modes 1 and 2, its controls made when the card shows or, as on the TV, by Overlay.open() before
    it) and the real Background.onInit with the real background.xml show exactly two lines, line 1 cut with
    "...", the whole Next line 34 px down; a sweep over line heights and widths;
  - the card's controls (labels, their arguments, setLabel only), background.xml, and a code guard.
"""
import ast
import calendar
import hashlib
import json
import math
import os
import xml.etree.ElementTree as ET

import pytest

import kodi_faithful as kf
import onnext_helpers as oh
import test_imports64_upnext as up

HERE = os.path.dirname(os.path.abspath(__file__))
ADDON = os.path.dirname(HERE)
OVERLAYTOOL = os.path.join(ADDON, 'resources', 'lib', 'overlaytool.py')

FONT = kf.KodiFont.load()
FONTS = {'font13': FONT}
with open(kf.FONT13, encoding='utf-8') as _fh: FIXTURE = json.load(_fh)

# The card of 2026-11-21: Saturday Morning TV, the next show at 10:55 in Madrid.
NOW_TITLE = 'Teenage Mutant Ninja Turtles'
CHANNEL = 'Saturday Morning TV'
NEXT_TITLE = 'The Spectacular Spider-Man - Persona (1x10)'
NEXT_START = calendar.timegm((2026, 11, 21, 9, 55, 0))
CARD = ("[B]You're Watching:[/B] Teenage Mutant Ninja Turtles on Saturday Morning TV[CR]"
        "[B]Next[/B] [B]@ 10:55: The Spectacular Spider-Man - Persona (1x10)[/B]")
LINE1_WRAPPED = "You're Watching: Teenage Mutant Ninja Turtles on Saturday Morning"
LINE1_CUT = "You're Watching: Teenage Mutant Ninja Turtles on Saturday Morning ..."
NEXT_LINE = 'Next @ 10:55: The Spectacular Spider-Man - Persona (1x10)'

# Quoted from 39956387 (imports.71, the code on the box on 2026-11-21): overlay.py:366-367 and :396, and
# background.xml:120-132. NEW_* are the lines imports.72 put there.
OLD_SHADOW = "xbmcgui.ControlTextBox(*rect[name], onnext_helpers.TEXT_FONT, onnext_helpers.SHADOW_COLOR)"
OLD_TEXT = "xbmcgui.ControlTextBox(*rect[name], onnext_helpers.TEXT_FONT, onnext_helpers.TEXT_COLOR)"
OLD_SET = "control.setText(text)"
NEW_SHADOW = "xbmcgui.ControlLabel(*rect[name], '', onnext_helpers.TEXT_FONT, onnext_helpers.SHADOW_COLOR)"
NEW_TEXT = "xbmcgui.ControlLabel(*rect[name], '', onnext_helpers.TEXT_FONT, onnext_helpers.TEXT_COLOR)"
NEW_SET = "control.setLabel(text)"
OLD_40003 = """<control type="textbox" id="40003">
                    <height>70</height>
                    <width>960/</width>
                    <posy>140</posy>
                    <font>font27</font>
                    <scroll>true</scroll>
                    <textcolor>white</textcolor>
                    <shadowcolor>dimgray</shadowcolor>
                    <scrolltime>600</scrolltime>
                    <autoscroll delay="5000" time="1000">false</autoscroll>
                    <label/>
                    <visible>Control.IsVisible(40001)</visible>
                </control>"""


@pytest.fixture
def madrid(tz):
    tz('Europe/Madrid')


def chars(*runs):
    """[(character, bold)] of (text, bold) runs."""
    return [(char, bold) for text, bold in runs for char in text]


def element(text):
    """A <control> element of XML written in this file."""
    return ET.fromstring(text)  # noqa: S314 -- XML written in this file


def tiny_font(table, bold=None, line_height=10, cell_height=10, ligatures=()):
    """A hand-made font: {char: [advance, extent, top, rows]} for regular (and `bold`, default the same)."""
    return kf.KodiFont({'metrics': {'line_height': line_height, 'cell_height': cell_height},
                        'glyphs': {'regular': table, 'bold': dict(table if bold is None else bold)},
                        'pairs_substituted': list(ligatures)})


ABC = dict((char, [10, 10, 0, 10]) for char in 'abcdefghijklmnopqrstuvwxyz .')


# ================================================================ the font fixture

def test_the_fixture_is_the_users_font13():
    p = FIXTURE['provenance']
    assert p['ttf_sha256'] == '6fb240521cfe3fe9983b590fdf0fac15e891b19b261ae382517066029eec46b3'
    assert p['font_xml_entry'] == {'name': 'font13', 'filename': 'play.regular.ttf', 'size': '29'}   # no style, no linespacing
    assert (p['freetype'], p['harfbuzz']) == ('2.13.3', '10.2.0')
    assert p['skin'].startswith('skin.aeon.nox.silvo 10.0.3') and 'GUIFontManager.cpp:361-363' in p['font_xml']
    assert 'floor(x + 0.5)' in p['rounding'] and p['tool'].startswith('~/.claude/plans/upnext-lines/tools/')
    m = FIXTURE['metrics']
    assert (m['size'], m['line_height'], m['cell_baseline'], m['cell_height']) == (29, 34.0, 28, 35)
    assert (FONT.line_height, FONT.cell_height, FONT.dot) == (34, 35, 7)
    for style, bold in (('regular', False), ('bold', True)):
        assert sorted(FIXTURE['glyphs'][style]) == [chr(code) for code in range(32, 127)]
        assert FONT.glyph('A', bold) == tuple(FIXTURE['glyphs'][style]['A'])
    assert FIXTURE['glyph_columns'] == ['advance', 'extent', 'top', 'rows']
    # the whole table, locked: a changed row (even of a letter no check uses) needs a deliberate regeneration with
    # make_fixture.py and a new value here
    canonical = json.dumps(FIXTURE, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')
    assert hashlib.sha256(canonical).hexdigest() == '31cf8d44b6b4e9f6be88f5c6e0aad00492d77ce3aeb71509c6fd4cd4dddc3a0c'


def test_the_fixture_follows_kodis_glyph_rules():
    """Every glyph from its raw FreeType numbers by Kodi's rules (GUIFontTTF.cpp:1049-1059): the advance
    round_int(advance.x / 64), never round() (bold ',' and ';' are 8.5 px: 9, where round() gives 8), the
    extent bitmap width + left, the top cell baseline - bitmap top, the rows the bitmap rows."""
    baseline, ties = FIXTURE['metrics']['cell_baseline'], []
    for style in ('regular', 'bold'):
        for char, (_gid, advance_266, left, top, width, rows) in FIXTURE['raw'][style].items():
            row = FIXTURE['glyphs'][style][char]
            assert row == [math.floor(advance_266 / 64 + 0.5), width + left, baseline - top, rows], (char, style)
            assert row[0] == kf.round_int(advance_266 / 64.0)
            if advance_266 % 64 == 32: ties.append((char, style, row[0], round(advance_266 / 64.0)))
    assert sorted(ties) == [(',', 'bold', 9, 8), (';', 'bold', 9, 8)]
    assert (kf.round_int(8.5), kf.round_int(-0.5), kf.round_int(2.4999)) == (9, 0, 2)


def test_the_fixture_checks_are_what_the_stand_in_measures():
    """The fixture's checks: lines shaped by HarfBuzz as Kodi shapes them (GetHarfBuzzShapedGlyphs) and measured
    through the shaped glyphs. The stand-in, per character, gets the same width for each, and refuses the one
    with a ligature. The first two are the card of 2026-11-21."""
    checks = FIXTURE['checks']
    assert [c['width'] for c in checks[:2]] == [967, 831] and len(checks) == 15
    assert [c['markup'] for c in checks if c['substituted']] == ["[B]Next[/B] [B]@ 10:55: The Fifth Element[/B]"]
    for check in checks:
        parsed = kf.parse(check['markup'])
        assert parsed == chars(*[tuple(run) for run in check['runs']]), check['markup']
        assert not check['offsets']
        if check['substituted']:
            with pytest.raises(NotImplementedError, match='ligature'): kf.width(parsed, FONT)
        else:
            assert kf.width(parsed, FONT) == check['width'], check['markup']


def test_ligatures_and_missing_glyphs_are_refused():
    """HarfBuzz shapes seven ASCII pairs into ligatures in this font (its morx table): Kodi measures that glyph,
    so the stand-in refuses the pair rather than measure two. A character the table lacks raises KeyError."""
    assert FIXTURE['pairs_substituted'] == ['fb', 'fh', 'fi', 'fj', 'fk', 'fl', 'ft'] == sorted(FONT.ligatures)
    with pytest.raises(NotImplementedError): kf.width(kf.parse('Fifth'), FONT)
    with pytest.raises(NotImplementedError): kf.width(chars(('f', True), ('i', False)), FONT)   # styles don't matter to the shaper
    assert kf.width(kf.parse('f i'), FONT) > 0
    for text in ('Ñu', 'café', 'Up…', 'a b'):
        with pytest.raises(KeyError): kf.width(kf.parse(text), FONT)
    with pytest.raises(KeyError): FONT.glyph('…')


# ================================================================ parse: [B], [/B], [CR]

@pytest.mark.parametrize('text, expected', [
    ('[B]a[/B]b', [('a', True), ('b', False)]),
    ('[B]a', chars(('[B]a', False))),                                  # no [/B] after it: literal
    ('a[/B]b', chars(('a[/B]b', False))),                              # not bold: literal
    ('x[/B]y[B]z', chars(('x[/B]y[B]z', False))),                      # a [/B] BEFORE the [B] doesn't count
    ('a[CR]b', [('a', False), ('\n', False), ('b', False)]),
    ('a[/CR]b', chars(('a[/CR]b', False))),
    ('a[', chars(('a[', False))), ('[', [('[', False)]), ('[/', chars(('[/', False))),
    ('[B]a[CR]b[/B]', [('a', True), ('\n', False), ('b', True)]),
    ('[B][B]a[/B]b[/B]', [('a', True)] + chars(('b[/B]', False))),
    ('[b]a[/b]', chars(('[b]a[/b]', False))), ('[x]', chars(('[x]', False))),
    ('a[cr]b', chars(('a[cr]b', False))),                              # the tags are case-sensitive (:406, 460)
    ("[B]Next[/B] [B]@ 1[/B]", chars(('Next', True), (' ', False), ('@ 1', True))),
])
def test_parse(text, expected):
    assert kf.parse(text) == expected


def test_parse_default_style_and_the_card():
    assert kf.parse('a[/B]b', default_bold=True) == [('a', True), ('b', False)]
    assert kf.parse(CARD) == chars(("You're Watching:", True), (' %s on %s' % (NOW_TITLE, CHANNEL), False), ('\n', False),
                                   ('Next', True), (' ', False), ('@ 10:55: %s' % NEXT_TITLE, True))


@pytest.mark.parametrize('text', ['[I]a[/I]', '[COLOR red]a[/COLOR]', '[UPPERCASE]a[/UPPERCASE]', '[LOWERCASE]A[/LOWERCASE]',
                                  '[CAPITALIZE]a[/CAPITALIZE]', '[LIGHT]a[/LIGHT]', '[TABS]2[/TABS]', '$INFO[Player.Title]',
                                  '$LOCALIZE[32104]', '$ADDON[x 1]', '$VAR[x]', '$ESCINFO[x]', '$ESCVAR[x]', '$NUMBER[3]',
                                  'Up $FEATURE[a,b] next'])
def test_parse_refuses_what_it_does_not_model(text):
    with pytest.raises(NotImplementedError): kf.parse(text)


# ================================================================ width, line breaks, wrapping

def test_width_counts_the_last_glyphs_ink():
    """GetTextWidthInternal: advances, but the last glyph counts max(extent, advance) (GUIFontTTF.cpp:724-725)."""
    font = tiny_font(dict(ABC, a=[10, 14, 0, 10], b=[10, 8, 0, 10]))
    assert kf.width([], font) == 0
    assert kf.width(kf.parse('ab'), font) == 20 and kf.width(kf.parse('ba'), font) == 24 and kf.width(kf.parse('a'), font) == 14
    assert kf.width(kf.parse('[B]Eddy[/B]'), FONT) == sum(FONT.glyph(c, True).advance for c in 'Edd') + FONT.glyph('y', True).extent
    assert FONT.glyph('y', True).extent > FONT.glyph('y', True).advance
    for char in '\t\r\n':
        with pytest.raises(NotImplementedError): kf.width([('a', False), (char, False)], font)


def test_line_break_splits_at_cr_only_up_to_the_height():
    """LineBreakText: [CR] only, at most ceil(max height / line height) lines; UpdateStyled drops trailing blank
    lines (GUITextLayout.cpp:646-671, 273-275)."""
    text = kf.parse('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa[CR]b[CR]c[CR]d')
    texts = lambda lines: [''.join(c for c, _b in line) for line in lines]   # noqa: E731
    assert texts(kf.line_break(text, 70, 34)) == ['a' * 30, 'b', 'c']                 # ceil(70 / 34) = 3
    assert texts(kf.line_break(text, 68, 34)) == ['a' * 30, 'b']                      # exactly 2
    assert texts(kf.line_break(text, 70, 41)) == ['a' * 30, 'b']                      # ceil(70 / 41) = 2
    assert texts(kf.line_break(text, 0, 34)) == ['a' * 30, 'b', 'c', 'd']
    assert texts(kf.line_break(kf.parse('a[CR][CR]'), 0, 34)) == ['a']
    assert texts(kf.line_break(kf.parse('a[CR][CR]b'), 0, 34)) == ['a', '', 'b']
    assert kf.line_break([], 70, 34) == []


def test_wrap_breaks_only_when_strictly_wider_at_the_last_space():
    """WrapText (GUITextLayout.cpp:588-631): a line exactly as wide as the box stays; a wider one breaks at its
    last space, which is dropped (no line starts or ends with it); with no space before the character that
    overflowed; a character wider than the box alone gets a line of its own."""
    font = tiny_font(dict(ABC, w=[100, 100, 0, 10]))
    texts = lambda lines: [''.join(c for c, _b in line) for line in lines]   # noqa: E731
    assert texts(kf.wrap(kf.parse('aaaaaa'), 60, font)) == ['aaaaaa']
    assert texts(kf.wrap(kf.parse('aaa bbb'), 60, font)) == ['aaa', 'bbb']
    assert texts(kf.wrap(kf.parse('aa bb cc dd'), 50, font)) == ['aa bb', 'cc dd']
    assert texts(kf.wrap(kf.parse('aaaa  bb'), 50, font)) == ['aaaa ', 'bb']        # the LAST space goes, only it
    assert texts(kf.wrap(kf.parse('aaaaaaaa'), 60, font)) == ['aaaaaa', 'aa']
    assert texts(kf.wrap(kf.parse('ww'), 60, font)) == ['w', 'w']
    assert texts(kf.wrap(kf.parse('aaa bbb[CR][CR]c[CR]'), 60, font)) == ['aaa', 'bbb', '', 'c']
    assert texts(kf.wrap(kf.parse('aaa bbb'), 0, font)) == ['aaa bbb'] and kf.wrap(kf.parse('a'), -1, font) == []
    for line in kf.wrap(kf.parse(CARD), 960, FONT):
        assert line and line[0][0] != ' ' and line[-1][0] != ' '
    assert texts(kf.wrap(kf.parse(CARD), 960, FONT)) == [LINE1_WRAPPED, 'TV', NEXT_LINE]


# ================================================================ drawing: the cut, the label, the text box

def test_the_cut_keeps_room_for_the_dots():
    """DrawTextInternal, truncated (GUIFontTTF.cpp:619-637): a glyph is drawn while x + its advance + the three
    dots fit, exactly fitting included; then '...' in the regular weight, whatever the line's."""
    font = tiny_font(ABC)                                              # every glyph and the dot 10 px: dots 30 px
    drawn, cut = kf.draw_line(kf.parse('aaaaaaaaaa'), font, 60, True)
    assert cut and drawn == chars(('aaa', False), ('...', False))       # 'aaa' ends at 30, +30 = 60: kept
    drawn, cut = kf.draw_line(kf.parse('aaaaaaaaaa'), font, 61, True)
    assert cut and drawn == chars(('aaa', False), ('...', False))
    drawn, cut = kf.draw_line(kf.parse('aaaaaaaaaa'), font, 59, True)
    assert cut and drawn == chars(('aa', False), ('...', False))
    drawn, cut = kf.draw_line(kf.parse('[B]aaaaaaaaaa[/B]'), font, 60, True)
    assert cut and drawn == chars(('aaa', True), ('...', False))        # bold text, regular dots
    drawn, cut = kf.draw_line(kf.parse('aaaaaa'), font, 60, True)
    assert not cut and drawn == chars(('aaaaaa', False))               # fits: untouched (:444-447)
    drawn, cut = kf.draw_line(kf.parse('aaaaaaaaaa'), font, 60, False)
    assert not cut and drawn == chars(('aaaaaaa', False))              # not truncated: drawn until x passes 60
    regular, bold = FONT.glyph('.', False), FONT.glyph('.', True)
    assert (FONT.dot, regular.advance, bold.advance) == (7, 7, 8)       # bold dots would be 24 px, not 21


def test_the_cut_measures_as_kodi_does():
    """The room for the dots is three REGULAR '.' even in a bold line whose '.' is wider (GetCharacter(L'.', 0),
    GUIFontTTF.cpp:431-441). Whether a line fits counts its last glyph's ink (GetTextWidthInternal, :444-447 with
    :724-725), and so does the label's widest line, which decides whether it overflows (CalcTextExtent,
    GUITextLayout.cpp:679-692)."""
    wide_bold_dot = tiny_font(ABC, bold=dict(ABC, **{'.': [20, 20, 0, 10]}))
    drawn, cut = kf.draw_line(kf.parse('[B]aaaaaaaaaa[/B]'), wide_bold_dot, 60, True)
    assert cut and drawn == chars(('aaa', True), ('...', False))        # 30 px kept for the dots, not 60
    inky = tiny_font(dict(ABC, b=[10, 14, 0, 10]))                      # 'b' inks 4 px past its 10 px advance
    assert kf.width(kf.parse('aaaaab'), inky) == 64
    drawn, cut = kf.draw_line(kf.parse('aaaaab'), inky, 60, True)
    assert cut and drawn == chars(('aaa', False), ('...', False))       # 64 px > 60: cut, its advances only 60
    label = kf.KodiLabel(60, 10, inky).set_text('aaaaab')
    assert label.overflows() and [(d.text, d.cut) for d in label.render()] == [('aaa...', True)]


def test_a_label_cuts_only_the_line_that_is_too_wide():
    font = tiny_font(ABC, line_height=10, cell_height=12)
    label = kf.KodiLabel(60, 25, font).set_text('aaaaaaaaaa[CR]bb')
    assert [(d.y, d.text, d.cut) for d in label.render()] == [(0, 'aaa...', True), (10, 'bb', False)]
    label.set_text('bb[CR][B]aaaaaaaaaa[/B]')
    drawn = label.render()
    assert [(d.y, d.text, d.cut) for d in drawn] == [(0, 'bb', False), (10, 'aaa...', True)]
    assert drawn[1].chars[-3:] == [('.', False)] * 3
    label.set_text('aaaaaa[CR]bbbbbb')                                   # exactly 60: nothing cut
    assert [(d.text, d.cut) for d in label.render()] == [('aaaaaa', False), ('bbbbbb', False)]
    assert kf.KodiLabel(60, 25, font).set_text('').render() == []


def test_a_label_overflows_past_half_a_pixel_and_cuts_at_the_rounded_width():
    """GUILabel.cpp:112: it overflows when min(widest, width) + 0.5 < widest. Drawn, each line is checked against
    the width rounded to a whole pixel (GUIFont.cpp:106), so a label 59.6 px wide shows a 60 px line whole
    whether or not it overflows, and one 59.4 px wide cuts it."""
    font = tiny_font(ABC)
    label = lambda w: kf.KodiLabel(w, 10, font).set_text('aaaaaa')   # noqa: E731 -- 60 px
    assert [label(w).overflows() for w in (59.4, 59.5, 59.6, 60, 61)] == [True, False, False, False, False]
    assert [[d.cut for d in label(w).render()] for w in (59.4, 59.5, 59.6, 60)] == [[True], [False], [False], [False]]


def test_a_label_draws_its_lines_one_line_apart_without_clipping():
    """Up to ceil(height / line height) lines (GUILabel.cpp:20), one line height apart, top aligned, clipped
    only by each line's own clip (labels set no clip region): a third line hangs below a 70 px label."""
    font = kf.KodiFont.uniform(10, 34)
    drawn = kf.KodiLabel(960, 70, font).set_text('a[CR]b[CR]c[CR]d').render()
    assert [(d.y, d.text) for d in drawn] == [(0, 'a'), (34, 'b'), (68, 'c')]
    assert drawn[2].ink == tuple(range(68, 102))
    assert [d.y for d in kf.KodiLabel(960, 70, kf.KodiFont.uniform(10, 41)).set_text('a[CR]b[CR]c').render()] == [0, 41]
    assert [d.text for d in kf.KodiLabel(960, 68, font).set_text('a[CR]b[CR]c').render()] == ['a', 'b']       # ceil(68 / 34) = 2
    assert [d.text for d in kf.KodiLabel(960, 70, kf.KodiFont.uniform(10, 35)).set_text('a[CR]b[CR]c').render()] == ['a', 'b']   # 70 / 35


def test_a_label_refuses_wrapping_and_scrolling():
    with pytest.raises(NotImplementedError): kf.KodiLabel(960, 70, FONT, wrap=True)
    with pytest.raises(NotImplementedError): kf.KodiLabel(960, 70, FONT, scroll=True)


def test_a_text_box_draws_the_lines_that_start_inside_and_clips_them():
    """GUITextBox.cpp:204, 240-251: lines one line height apart while a line's top is above the box's bottom,
    each clipped to the box."""
    font = kf.KodiFont.uniform(10, 34)
    drawn = kf.KodiTextBox(960, 70, font).set_text('a[CR]b[CR]c[CR]d').render()
    assert [(d.y, d.text) for d in drawn] == [(0, 'a'), (34, 'b'), (68, 'c')]
    assert [d.ink for d in drawn] == [tuple(range(0, 34)), tuple(range(34, 68)), (68, 69)]
    assert [d.y for d in kf.KodiTextBox(960, 68, font).set_text('a[CR]b[CR]c').render()] == [0, 34]   # top at the edge: not drawn
    real = kf.KodiTextBox(960, 70, FONT).set_text('a[CR]b[CR]' + NEXT_LINE).render()
    assert real[2].y == 68 and real[2].ink == ()                       # Play's ink starts 5 px or more below a line's top
    # the card's 967 px first line: whole in a box 967 px wide, wrapped in one a pixel narrower
    assert [(d.y, d.text) for d in kf.KodiTextBox(967, 70, FONT).set_text(CARD).render()] == [(0, LINE1_WRAPPED + ' TV'), (34, NEXT_LINE)]
    assert [(d.y, d.text) for d in kf.KodiTextBox(966, 70, FONT).set_text(CARD).render()] == [(0, LINE1_WRAPPED), (34, 'TV'), (68, NEXT_LINE)]
    assert min(FONT.glyph(chr(c), b).top for c in range(33, 127) for b in (False, True)) == 5


# ================================================================ stand_in: from a rig control or the window file

def test_stand_in_from_rig_controls():
    label = up.FakeControl.made('label')(1, 2, 960, 70, '', 'font27', '0xFFFFFFFF')
    made = kf.stand_in(label, FONTS)
    assert isinstance(made, kf.KodiLabel) and (made.width, made.height, made.font) == (960, 70, FONT)   # font27 -> font13
    box = kf.stand_in(up.FakeControl.made('textbox')(1, 2, 960, 70, 'font27', '0xFFFFFFFF'), FONTS)
    assert isinstance(box, kf.KodiTextBox) and (box.width, box.height, box.font) == (960, 70, FONT)
    assert kf.stand_in(up.FakeControl.made('label')(1, 2, 3, 4, 'x'), FONTS).font is FONT          # no font: font13
    with pytest.raises(NotImplementedError): kf.stand_in(up.FakeControl.made('label')(1, 2, 3, 4, 'x', alignment=2), FONTS)
    with pytest.raises(NotImplementedError): kf.stand_in(up.FakeControl.made('image')(1, 2, 3, 4, ' '), FONTS)
    refused = [up.FakeControl.made('label')(1, 2, 960, 70, '', ''),                     # font '': Kodi finds none, draws nothing
               up.FakeControl.made('textbox')(1, 2, 960, 70, ''),
               up.FakeControl.made('label')(1, 2, 960, 70, '', 'font27', hasPath=True),  # its text shortened as a path
               up.FakeControl.made('label')(1, 2, 0, 70, '', 'font27')]                  # 0 wide: as wide as its text
    for control in refused:
        with pytest.raises(NotImplementedError): kf.stand_in(control, FONTS)
    for width in (0, -1, 0.0):
        with pytest.raises(NotImplementedError): kf.KodiLabel(width, 70, FONT)


@pytest.mark.parametrize('xml, kind, size', [
    (OLD_40003, kf.KodiTextBox, (960, 70)),
    ('<control type="label"><width>960/</width><height>70</height><font>font27</font></control>', kf.KodiLabel, (960, 70)),
    ('<control type="label"><width>960</width><height>70</height><font>font27</font><scroll>false</scroll></control>', kf.KodiLabel, (960, 70)),
    ('<control type="label"><width>9x</width><height>7</height><font>font13</font><scroll>sometimes</scroll></control>', kf.KodiLabel, (9, 7)),
    ('<control type="label"><width>960x</width><height>70</height><font>FONT13</font><wrapmultiline>no</wrapmultiline>'
     '<haspath>false</haspath></control>', kf.KodiLabel, (960, 70)),
])
def test_stand_in_from_the_window_file(xml, kind, size):
    """GetDimension's strtod ('960/' is 960, no number 0); <scroll> counts only when Kodi reads it as a
    boolean; a font name in any case."""
    made = kf.stand_in(element(xml), FONTS)
    assert isinstance(made, kind) and (made.width, made.height) == size and made.font is FONT


def test_sizes_as_kodi_reads_them():
    """strtod's leading number, the rest left (GUIControlFactory.cpp:183-195): no number is 0."""
    assert [kf.parse_size(text) for text in ('960', '960/', ' 70.5px', 'x', '', '-3')] == [960.0, 960.0, 70.5, 0.0, 0.0, -3.0]


@pytest.mark.parametrize('xml', [
    '<control type="label"><width>960</width><height>70</height><font>font27</font><scroll>true</scroll></control>',
    '<control type="label"><width>960</width><height>70</height><font>font27</font><scroll>Yes</scroll></control>',
    '<control type="label"><width>960</width><height>70</height><font>font27</font><wrapmultiline>true</wrapmultiline></control>',
    '<control type="label"><width>960</width><height>70</height><font>font27</font><wrapmultiline>maybe</wrapmultiline></control>',
    '<control type="label"><width>960</width><height>70</height><font>font27</font><align>center</align></control>',
    '<control type="label"><width>960</width><height>70</height><font>font27</font><textwidth>500</textwidth></control>',
    '<control type="label"><width>960</width><height>70</height><font>font27</font><haspath>true</haspath></control>',
    '<control type="label"><width>960</width><height>70</height><font>font27</font><haspath>maybe</haspath></control>',
    '<control type="label"><width>x</width><height>70</height><font>font27</font></control>',
    '<control type="label"><width>50%</width><height>70</height><font>font27</font></control>',
    '<control type="label"><width>10r</width><height>70</height><font>font27</font></control>',
    '<control type="label"><width>auto</width><height>70</height><font>font27</font></control>',
    '<control type="label"><height>70</height><font>font27</font></control>',
    '<control type="label"><width>960</width><height>70</height></control>',
    '<control type="textbox"><width>960</width><height>70</height><font>font27</font><autoscroll time="1000">true</autoscroll></control>',
    '<control type="textbox"><width>960</width><height>70.5</height><font>font27</font></control>',
    '<control type="image"><width>960</width><height>70</height></control>',
])
def test_stand_in_refuses_what_it_does_not_model(xml):
    with pytest.raises(NotImplementedError): kf.stand_in(element(xml), FONTS)


def test_strict_controls_refuse_calls_kodi_lacks():
    """A label has no setText, a text box no setLabel (interfaces/legacy/Control.h): the fakes raise as Python
    would and record it, so a test fails even where the code swallows the error."""
    label = up.FakeControl.made('label')(1, 2, 3, 4, '')
    with pytest.raises(AttributeError): label.setText('x')
    with pytest.raises(AttributeError): up.FakeControl.made('textbox')(1, 2, 3, 4).setLabel('x')
    with pytest.raises(AttributeError): up.FakeControl('group').setImage('x')
    with pytest.raises(AttributeError): label.setLable('x')
    with pytest.raises(AttributeError): label.getId()                  # Kodi has it; the fake doesn't: extend it
    assert kf.provoked() == ["'xbmcgui.ControlLabel' object has no attribute 'setText'",
                             "'xbmcgui.ControlTextBox' object has no attribute 'setLabel'",
                             "'xbmcgui.ControlGroup' object has no attribute 'setImage'",
                             "'xbmcgui.ControlLabel' object has no attribute 'setLable'",
                             "the rig's fake label control has no getId(): extend it"]
    for bad in (lambda: up.FakeControl.made('label')(1, 2, 3, 4), lambda: up.FakeControl.made('textbox')(1, 2, 3, 4, 'f', 'c', 'x'),
                lambda: up.FakeControl.made('image')(1, 2, 3, 4, ' ', aspect=1), lambda: up.FakeControl.made('label')(1, 2, 3, 4, 'x', label='y')):
        with pytest.raises(TypeError): bad()
    assert len(kf.provoked()) == 4
    assert kf.bind_control('label', (1, 2, 3, 4, ''), {'textColor': 'c'})['textColor'] == 'c'
    # the types Kodi's binding takes: an int (a bool is one) for a long or bool, a str for a const char*, a str,
    # bytes or None for a String; anything else raises TypeError there (an Overlay card that never shows)
    for bad in (lambda: up.FakeControl.made('label')(1, 2, 960.0, 70, ''), lambda: up.FakeControl.made('label')(1, 2, 3, 4, '', None),
                lambda: up.FakeControl.made('label')(1, 2, 3, 4, 7), lambda: up.FakeControl.made('label')(1, 2, 3, 4, '', alignment=2.0),
                lambda: up.FakeControl.made('image')(1, 2.5, 3, 4, ' '), lambda: up.FakeControl.made('image')(1, 2, 3, 4, None),
                lambda: up.FakeControl.made('textbox')(1, 2, 3, 4, b'font13'), lambda: up.FakeControl('group').setPosition(213.0, 657)):
        with pytest.raises(TypeError): bad()
    refusals = kf.provoked()
    assert len(refusals) == 8 and refusals[:2] == ["xbmcgui.ControlLabel(): argument 'width' must be an int, not float",
                                                   "xbmcgui.ControlLabel(): argument 'font' must be str, not NoneType"]
    assert refusals[-1] == "xbmcgui.ControlGroup.setPosition(): argument 'x' must be an int, not float"
    assert up.FakeControl.made('label')(True, 2, 3, 4, None, 'font13', hasPath=1).label is None
    assert up.FakeControl.made('label')(1, 2, 3, 4, b'x').label == b'x'


# ================================================================ the proof: the card of 2026-11-21

def card_rig(mode, source=None, size=(1920, 1080), opened=False):
    """test_imports64's rig of the real Overlay class (or of `source`), with the card of 2026-11-21 shown.
    `opened`: Overlay.open() first, as on the TV (services.py toggleOverlay opens the Overlay when a channel
    plays): its _prepareOnNext makes all four controls, hidden and empty, long before the card; the card then
    only fills and shows them. Otherwise the card makes its controls itself (a mode change, or open() failed)."""
    rig = up.Rig(mode=mode, size=size, source=source,
                 nitem={'start': NEXT_START, 'showlabel': NEXT_TITLE, 'art': {'thumb': '/art/spider-man.jpg'}})
    rig.player.playingItem = {'citem': {'name': CHANNEL, 'id': 'ch310'}, 'fitem': {'label': NOW_TITLE},
                              'nitem': {'start': NEXT_START, 'id': 12}}
    rig.advance = False                                                  # the 30 s never run out
    if opened: rig.overlay.open()
    rig.overlay.toggleOnNext(True)
    return rig


def old_overlay_source():
    """overlay.py with its three imports.72 lines put back to the lines quoted from 39956387 (on 39956387 itself
    the file already has them)."""
    source = up.OVERLAY
    for new, old in ((NEW_SHADOW, OLD_SHADOW), (NEW_TEXT, OLD_TEXT), (NEW_SET, OLD_SET)):
        if source.count(new) == 0 and source.count(old) == 1: continue
        assert source.count(new) == 1 and source.count(old) == 0, new
        source = source.replace(new, old)
    return source


def background_xml():
    with open(up.BACKGROUND_XML, encoding='utf-8') as fh: return fh.read()


def window_control(control_id, text=None):
    root = ET.fromstring(background_xml() if text is None else text)  # noqa: S314 -- the add-on's own window file
    found = [c for c in root.iter('control') if c.get('id') == str(control_id)]
    assert len(found) == 1
    return found[0]


@pytest.mark.parametrize('mode', [1, 2])
def test_old_overlay_card_loses_the_next_line(madrid, mode):
    """The lines quoted from 39956387 make text boxes: the 967 px first line wraps after "Morning", "TV" takes the
    second line, and the Next line, drawn 68 px down, shows none of its ink in the 70 px box: what the user saw."""
    rig = card_rig(mode, source=old_overlay_source())
    try:
        boxes = rig.video.controls[-2:]
        assert [c.kind for c in boxes] == ['textbox', 'textbox'] and rig.prop == 'true'
        for box in boxes:
            assert box.text == CARD and box.call['height'] == 70
            drawn = kf.stand_in(box, FONTS).set_text(box.text).render()
            assert [(d.y, d.text, d.cut) for d in drawn] == [(0, LINE1_WRAPPED, False), (34, 'TV', False), (68, NEXT_LINE, False)]
            assert drawn[0].ink and drawn[1].ink and drawn[2].ink == ()
    finally:
        rig.finish()


def test_old_background_card_loses_the_next_line():
    """The text box quoted from 39956387's background.xml, with the same text: the same loss."""
    drawn = kf.stand_in(element(OLD_40003), FONTS).set_text(CARD).render()
    assert [(d.y, d.text) for d in drawn] == [(0, LINE1_WRAPPED), (34, 'TV'), (68, NEXT_LINE)] and drawn[2].ink == ()


@pytest.mark.parametrize('opened', [False, True], ids=['made-when-shown', 'made-by-open'])
@pytest.mark.parametrize('mode', [1, 2])
def test_new_overlay_card_shows_both_lines(madrid, mode, opened):
    """The real Overlay makes labels: line 1 ends in "...", the whole Next line is 34 px down, for the text and
    for its shadow; whether the card made its controls itself (then it waits a few frames) or, as on the TV,
    Overlay.open() made them before (no wait: the card only fills and shows them)."""
    rig = card_rig(mode, opened=opened)
    try:
        controls = rig.video.controls[-2:]
        assert rig.prop == 'true' and len(rig.video.controls) == (4 if opened else 2 * mode)
        assert (oh.SETTLE_WAIT in [w for w, _p, _n in rig.waits]) == (not opened)   # the wait only for controls made now
        for control in controls:                                     # drawn first: whatever the code made
            shown = control.label if control.kind == 'label' else control.text
            assert shown == CARD
            drawn = kf.stand_in(control, FONTS).set_text(shown).render()
            assert [(d.y, d.text, d.cut) for d in drawn] == [(0, LINE1_CUT, True), (34, NEXT_LINE, False)]
            assert drawn[1].ink == kf.ink_rows(drawn[1].chars, FONT, 34) and drawn[1].ink
            assert drawn[1].chars == kf.parse(CARD)[kf.parse(CARD).index(('\n', False)) + 1:]
        assert [c.kind for c in controls] == ['label', 'label']
        assert [c.call['textColor'] for c in controls] == ['0xFF696969', '0xFFFFFFFF']   # the shadow, then the text
        for control in controls:
            assert [call for call in control.calls if call[0] in ('setLabel', 'setText')] == [('setLabel', CARD)]
    finally:
        rig.finish()


def test_new_background_card_shows_both_lines(madrid):
    """The real Background.onInit fills the real background.xml's 40003, a label: the same two lines."""
    controls, calls = up._background(nitem={'start': NEXT_START, 'showlabel': NEXT_TITLE},
                                     citem={'name': CHANNEL, 'logo': '/logo.png'}, fitem={'label': NOW_TITLE})
    control = controls[40003]
    shown = control.label if control.kind == 'label' else control.text
    assert calls['close'] == 0 and shown == CARD, calls['logs']
    drawn = kf.stand_in(window_control(40003), FONTS).set_text(shown).render()   # drawn first: whatever the window has
    assert [(d.y, d.text, d.cut) for d in drawn] == [(0, LINE1_CUT, True), (34, NEXT_LINE, False)]
    assert drawn[1].ink == kf.ink_rows(drawn[1].chars, FONT, 34) and drawn[1].ink
    assert control.kind == 'label' and [c for c in control.calls if c[0] in ('setLabel', 'setText')] == [('setLabel', CARD)]


def test_the_cut_first_line_fits_the_card():
    """Line 1 keeps "...Morning " (931 px) and its dots end at 952 px: the next glyph, 'T', would not leave room
    for them within 960."""
    drawn = kf.KodiLabel(960, 70, FONT).set_text(CARD).render()[0]
    kept = drawn.chars[:-3]
    assert kf.width(kept, FONT) == 931 and 931 + 3 * FONT.dot == 952 <= 960
    assert 931 + FONT.glyph('T').advance + 3 * FONT.dot > 960


def sweep_card(line1_px, advance=10):
    """A card whose first line is `line1_px` wide in a font of `advance` px glyphs."""
    count = line1_px // advance - len("You're Watching: ")
    filler = ('Saturday Morning ' * 200)[:count]
    if filler.endswith(' '): filler = filler[:-1] + 'x'
    return "[B]You're Watching:[/B] %s[CR][B]Next[/B] [B]@ 10:55: Spider-Man[/B]" % filler


SWEEP_LINE = 'Next @ 10:55: Spider-Man'
SWEEP_WIDTHS = sorted(set(range(900, 2001, 50)) | set(range(940, 990, 10)))


@pytest.mark.parametrize('line_h', [34, 36, 41])
def test_the_sweep(madrid, line_h):
    """Line heights of 34, 36 and 41 px (the user's Play, and Aeon Nox: SiLVO's Fira Sans and NoTo font13 lines) and
    first lines of 900 to 2,000 px, each glyph's ink filling its line. The old controls (the text boxes the quoted 39956387 lines make,
    and the quoted window file's) show the whole Next line only at 34 px with a first line that fits; the
    controls the code makes now (the real Overlay's labels, the real background.xml's 40003) always show it,
    one line below the first, and cut the first line exactly when it is wider than 960 px."""
    font = kf.KodiFont.uniform(10, line_h)
    fonts = {'font13': font}
    old_rig, new_rig = card_rig(2, source=old_overlay_source()), card_rig(2)
    try:
        old = [old_rig.video.controls[-1], element(OLD_40003)]
        new = [new_rig.video.controls[-1], window_control(40003)]
    finally:
        old_rig.finish()
        new_rig.finish()
    for line1_px in SWEEP_WIDTHS:
        text = sweep_card(line1_px)
        assert kf.width(kf.parse(text.split('[CR]')[0]), font) == line1_px
        for source in new:
            drawn = kf.stand_in(source, fonts).set_text(text).render()
            assert [(d.y, d.text[:16], d.cut) for d in drawn] == [(0, "You're Watching:", line1_px > 960), (line_h, SWEEP_LINE[:16], False)]
            assert drawn[1].text == SWEEP_LINE and drawn[1].ink == kf.ink_rows(drawn[1].chars, font, line_h)
        for source in old:
            drawn = kf.stand_in(source, fonts).set_text(text).render()
            whole = [d for d in drawn if d.text == SWEEP_LINE and d.ink == kf.ink_rows(d.chars, font, d.y)]
            assert bool(whole) == (line_h == 34 and line1_px <= 960), (line1_px, [(d.y, d.text[:20], len(d.ink)) for d in drawn])


# ================================================================ the card itself

def test_the_card_controls_exactly(madrid):
    """Drawing order frame, picture, shadow label, text label; their exact arguments; both labels get the card's
    text with setLabel, once, and nothing calls setText."""
    rig = card_rig(2)
    try:
        x, y = 213, 657
        image = dict(colorDiffuse=None)
        label = dict(width=960, height=70, label='', font='font27', disabledColor=None, alignment=0, hasPath=False, angle=0)
        assert [(c.kind, c.call) for c in rig.video.controls] == [
            ('image', dict(image, x=x, y=y, width=240, height=135, filename=' ', aspectRatio=0)),
            ('image', dict(image, x=x + 5, y=y + 5, width=230, height=125, filename=' ', aspectRatio=1)),
            ('label', dict(label, x=x + 1, y=y + 141, textColor='0xFF696969')),
            ('label', dict(label, x=x, y=y + 140, textColor='0xFFFFFFFF'))]
        assert rig.video.controls == rig.video.added and len(rig.video.added) == 4
        assert [type(c.call[k]) for c in rig.video.controls for k in ('x', 'y', 'width', 'height')] == [int] * 16
        for c in rig.video.controls[2:]:
            assert [call for call in c.calls if call[0] in ('setLabel', 'setText')] == [('setLabel', CARD)]
            assert c.label == CARD and c.text == '' and c.visible
        assert rig.video.controls[1].image == '/art/spider-man.jpg'
    finally:
        rig.finish()


def test_switching_modes_keeps_the_labels(madrid):
    """Text only (1), then with the picture (2): the frame and the picture join the two labels, which keep the
    card's text; then text only again hides them."""
    rig = card_rig(1)
    rig.idle = 0
    rig.join()
    assert [c.kind for c in rig.video.added] == ['label', 'label']
    rig.overlay.rearmOnNext()
    rig.player.OnNextMode, rig.idle = 2, 20
    rig.overlay.toggleOnNext(True)
    try:
        assert sorted(c.kind for c in rig.video.controls) == ['image', 'image', 'label', 'label'] and rig.prop == 'true'
        assert all(c.visible for c in rig.video.controls)
        assert [c.label for c in rig.video.controls if c.kind == 'label'] == [CARD, CARD]
    finally:
        rig.finish()
    rig.overlay.rearmOnNext()
    rig.player.OnNextMode, rig.stop, rig.idle = 1, False, 30
    rig.overlay.toggleOnNext(True)
    try:
        assert [(c.kind, c.visible) for c in rig.video.controls if c.kind == 'image'] == [('image', False), ('image', False)]
        assert [(c.kind, c.visible) for c in rig.video.controls if c.kind == 'label'] == [('label', True), ('label', True)]
    finally:
        rig.finish()


@pytest.mark.parametrize('size', [(1280, 720), (3840, 2160)])
def test_labels_follow_the_skin_grid(madrid, size):
    rig = card_rig(2, size=size)
    try:
        f = size[1] / 1080
        rect = oh.layout(213, 657, f, f)
        assert [(c.kind, c.args[:4]) for c in rig.video.controls[2:]] == [('label', rect['shadow']), ('label', rect['text'])]
        assert [type(v) for c in rig.video.controls for v in c.args[:4]] == [int] * 16
        assert [c.args[4:] for c in rig.video.controls[2:]] == [('', 'font27', '0xFF696969'), ('', 'font27', '0xFFFFFFFF')]
    finally:
        rig.finish()


def test_closing_leaves_no_controls(madrid):
    rig = card_rig(2)
    rig.overlay.close()
    rig.join()
    assert rig.video.controls == [] and rig.overlay._onNextControls == {} and rig.prop == 'false'
    assert [c.kind for c in rig.video.added] == ['image', 'image', 'label', 'label']


def test_the_card_text_and_layout_are_unchanged():
    """The words, the place and the sizes are imports.64's: only the controls' type changed."""
    assert oh.card_text("[B]You're Watching:[/B]", '[B]Next[/B]', NOW_TITLE, CHANNEL, '10:55', NEXT_TITLE) == CARD
    assert (oh.TEXT_W, oh.TEXT_H, oh.TEXT_GAP, oh.SHADOW_OFFSET) == (960, 70, 140, 1)
    assert (oh.TEXT_FONT, oh.TEXT_COLOR, oh.SHADOW_COLOR) == ('font27', '0xFFFFFFFF', '0xFF696969')
    assert oh.NAMES == ('frame', 'art', 'shadow', 'text')


# ================================================================ the plan's limits, as they are

def test_a_long_next_title_loses_its_end_but_keeps_the_time():
    long_card = "[B]You're Watching:[/B] Korra on Cartoon Cartoons[CR][B]Next[/B] [B]@ 10:55: Super Secret Secret Squirrel - Robin Hood and the Purple Plotters of Sherwood (1x02)[/B]"
    drawn = kf.KodiLabel(960, 70, FONT).set_text(long_card).render()
    assert [(d.text, d.cut) for d in drawn] == [("You're Watching: Korra on Cartoon Cartoons", False),
                                                ('Next @ 10:55: Super Secret Secret Squirrel - Robin Hood and the Pu...', True)]
    assert drawn[1].chars[-3:] == [('.', False)] * 3 and drawn[1].chars[-4] == ('u', True)   # regular dots after bold text
    assert kf.width(drawn[1].chars[:-3], FONT) + 3 * FONT.dot == 936 + 21 <= 960


def test_a_title_with_kodis_line_break_adds_a_third_line():
    """A guide title holding "[CR]" makes a third line, 68 px down, which the label draws whole below the card
    (the text box clipped it)."""
    text = oh.card_text("[B]You're Watching:[/B]", '[B]Next[/B]', 'Korra', 'Cartoon Cartoons', '10:55', 'Part one[CR]Part two')
    drawn = kf.KodiLabel(960, 70, FONT).set_text(text).render()
    assert [(d.y, d.text) for d in drawn] == [(0, "You're Watching: Korra on Cartoon Cartoons"), (34, 'Next @ 10:55: Part one'), (68, 'Part two')]
    assert drawn[2].ink == kf.ink_rows(drawn[2].chars, FONT, 68) and max(drawn[2].ink) >= 70


@pytest.mark.parametrize('line_h, kept', [(34, 3), (35, 2), (36, 2), (41, 2)])
def test_a_line_break_in_a_title_needs_lines_under_35_px(line_h, kept):
    """A 70 px label keeps at most ceil(70 / line height) lines (GUITextLayout.cpp:646-651): three only with
    lines under 35 px (the user's Play, Arial and Ubuntu Condensed in Aeon Nox: SiLVO), two with 35 px or
    taller ones (its Default, Raleway, Caviar Dreams, Fira Sans and NoTo). There a "[CR]" in the next show's
    title loses its second part, and one in the show's or the channel's name the Next line itself, as with
    the text box."""
    font = kf.KodiFont.uniform(10, line_h)
    title = oh.card_text("[B]You're Watching:[/B]", '[B]Next[/B]', 'Korra', 'Cartoon Cartoons', '10:55', 'Part one[CR]Part two')
    name = oh.card_text("[B]You're Watching:[/B]", '[B]Next[/B]', 'Korra[CR]Book one', 'Cartoon Cartoons', '10:55', 'Johnny Bravo')
    assert [d.text for d in kf.KodiLabel(960, 70, font).set_text(title).render()] == [
        "You're Watching: Korra on Cartoon Cartoons", 'Next @ 10:55: Part one', 'Part two'][:kept]
    assert [d.text for d in kf.KodiLabel(960, 70, font).set_text(name).render()] == [
        "You're Watching: Korra", 'Book one on Cartoon Cartoons', 'Next @ 10:55: Johnny Bravo'][:kept]


def test_the_move_tool_preview_keeps_its_sample_text_box():
    with open(OVERLAYTOOL, encoding='utf-8') as fh: assert 'ControlTextBox' in fh.read()


# ================================================================ background.xml and the code

def test_background_xml_40003_is_a_label():
    """40003: a label in group 40001, the text box's size, place, font, colours and visibility; no scroll,
    scrolltime, autoscroll or wrapmultiline; no "960/" left; one 40003 in the file."""
    text = background_xml()
    root = ET.fromstring(text)  # noqa: S314 -- the add-on's own window file
    by_id = dict((c.get('id'), c) for c in root.iter('control') if c.get('id'))
    group, label = by_id['40001'], by_id['40003']
    assert text.count('id="40003"') == 1 and label in list(group) and group.get('type') == 'group'
    assert label.get('type') == 'label'
    assert [(child.tag, (child.text or '').strip(), dict(child.attrib)) for child in label] == [
        ('height', '70', {}), ('width', '960', {}), ('posy', '140', {}), ('font', 'font27', {}), ('textcolor', 'white', {}),
        ('shadowcolor', 'dimgray', {}), ('label', '', {}), ('visible', 'Control.IsVisible(40001)', {})]
    assert group.findtext('width') == '960' and '960/' not in text
    assert [c.get('id') for c in root.iter('control') if c.get('type') == 'textbox'] == []
    assert isinstance(kf.stand_in(label, FONTS), kf.KodiLabel)


def test_no_text_box_left_in_either_card():
    """Overlay and Background make and fill labels only (the code, comments left out)."""
    for cls in ('Overlay', 'Background'):
        code = ast.unparse(up._class(up.O_TREE, cls))
        assert 'ControlTextBox' not in code and '.setText(' not in code, cls
    overlay = ast.unparse(up._method(up.O_TREE, 'Overlay', '_onNextCreate'))
    assert overlay.count('xbmcgui.ControlLabel(*rect[name], ') == 2
    assert ast.unparse(up._method(up.O_TREE, 'Overlay', '_showOnNext')).count('control.setLabel(text)') == 1
    assert ast.unparse(up._method(up.O_TREE, 'Background', 'onInit')).count('self.getControl(40003).setLabel(') == 1
    for line in (NEW_SHADOW, NEW_TEXT, NEW_SET):
        assert up.OVERLAY.count(line) == 1, line
    for line in (OLD_SHADOW, OLD_TEXT, OLD_SET):
        assert up.OVERLAY.count(line) == 0, line
