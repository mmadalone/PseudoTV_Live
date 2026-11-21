# -*- coding: utf-8 -*-
"""imports.72: the version, the changelog, the add-on's "What's new" news and FORK_NOTES.

The release's docs, pinned where they could drift: one version in addon.xml, the news header and the changelog
header; the news within Kodi's limit, imports.72's block first and imports.71's after it, imports.70's gone; the
changelog pop-up (test_imports55 keeps its 2,500 limit) saying in plain words what the user will notice, with no
code words; the engineering notes naming the code and the Kodi lines; the FORK_NOTES section with the cause, the
measurements, the Kodi source lines, the design, the decisions, the limits and the live checks. No setting changes
(a rollback needs no settings migration), and no address, web address or credential in any of it, in the release's
tests and fixture, or in what it wrote into the code and the window file.
"""
import ast
import hashlib
import os
import re
import sys
import xml.etree.ElementTree as ET

HERE  = os.path.dirname(os.path.abspath(__file__))
ADDON = os.path.dirname(HERE)
LIB   = os.path.join(ADDON, 'resources', 'lib')
ROOT  = os.path.dirname(ADDON)
if LIB not in sys.path: sys.path.insert(0, LIB)

import changelog_helpers  # noqa: E402

VERSION = '0.8.0+imports.72'
SECTION = '**The Up next card always shows the Next line (imports.72):**'
WINDOW  = os.path.join(ADDON, 'resources', 'skins', 'default', '1080i', 'plugin.video.pseudotv.imports.background.xml')


def _read(*parts):
    with open(os.path.join(*parts), encoding='utf-8') as fh: return fh.read()


def _addon():
    return ET.parse(os.path.join(ADDON, 'addon.xml')).getroot()  # noqa: S314 -- the add-on's own addon.xml


def _news():
    return _addon().find('.//news').text


def _entry():
    """The imports.72 changelog entry, the pop-up part and the engineering notes."""
    text = _read(ADDON, 'changelog.txt')
    start = text.index('v.%s\n' % VERSION)
    return text[start:text.index('\nv.0.8.0+imports.71\n')]


def _section():
    text = _read(ROOT, 'FORK_NOTES.md')
    start = text.index(SECTION)
    return text[start:text.index('**Maintenance posture**', start)]


def test_one_version_everywhere():
    assert _addon().get('version') == VERSION
    assert _news().startswith('v%s\n' % VERSION)
    assert changelog_helpers.latestEntry(_read(ADDON, 'changelog.txt')).startswith('v.%s\n' % VERSION)


def test_the_news_fits_kodis_limit_and_says_what_changed():
    """Kodi shows at most 1,500 characters: imports.72's block leads, imports.71's follows, imports.70's left."""
    news = _news()
    assert len(news) <= 1500
    assert [line for line in news.splitlines() if line.startswith('v0.8.0+imports.')] == [
        'v0.8.0+imports.72', 'v0.8.0+imports.71']
    block = news.split('v0.8.0+imports.71')[0]
    for words in ('Up next card', "show's last seconds", 'both lines', '"You\'re Watching: ..."', '"Next @ time: ..."',
                  'ends with "..."'):
        assert words in block, words


def test_the_pop_up_says_what_you_will_notice():
    """The plan's 'What you'll notice', in plain words, above the engineering notes; within the pop-up's limit
    (test_imports55 holds it at 2,500); no code words."""
    popup = changelog_helpers.latestEntry(_read(ADDON, 'changelog.txt'))
    assert len(popup) < 2500 and popup.count('\nv.') == 0
    for words in ('always shows both its lines', '"You\'re Watching: ..."', '"Next @ time: ..."', '21 November',
                  'Saturday Morning", then "TV"', '"between shows"', 'three dots',
                  '"You\'re Watching: Teenage Mutant Ninja Turtles on Saturday Morning ..."', 'whatever font',
                  'never touched', 'its time and the start of its title always show',
                  'the Default, Arial, NoTo and Fira Sans choices', "Kodi's own skin",
                  'the bottoms of letters like g, p and y', 'looks and works as before', 'soft shadow', 'the Move tool'):
        assert words in popup, words
    for code in ('ControlLabel', 'ControlTextBox', 'setLabel', 'setText', '.py', '.xml', '40003', 'textbox',
                 'font27', 'font13', '[CR]'):
        assert code not in popup, code


def test_the_engineering_notes_name_the_code():
    notes = _entry().split('--- engineering notes ---\n', 1)[1]
    for words in ('overlay.py', 'background.xml', 'onnext_helpers.py', 'Overlay._onNextCreate', 'Background.onInit',
                  "xbmcgui.ControlLabel(*rect, '', font27, colour)", 'setLabel', 'GUIFontManager.cpp:361-363',
                  'GUITextBox.cpp:27', '967 px', 'y = 68', 'Control.cpp:1012-1021', 'GUILabelControl.cpp:21',
                  'GUITextLayout.cpp:646-671', 'GUILabel.cpp:112', 'GUIFontTTF.cpp:444-447', 'GUIFontTTF.cpp:619-637',
                  'GUIControlFactory.cpp:1133-1135', 'kodi_faithful.py', 'font13_play_regular_29.json',
                  'test_imports72_upnext_lines.py', 'test_imports72_docs.py', 'test_imports64_upnext.py',
                  'test_imports71_docs.py', 'test_imports70_replay_end.py', '26 failed, 238 passed',
                  'Madrid, UTC, New York and Kolkata', 'Overlay.open()', 'GUIFont.cpp:111-112', '81 px in NoTo',
                  "the build's 69, the review's 11, the fix stage's 11"):
        assert words in notes, words


def test_fork_notes_has_the_section():
    """After the imports.71 section, before 'Maintenance posture'."""
    text = _read(ROOT, 'FORK_NOTES.md')
    assert text.count(SECTION) == 1
    assert text.index('**The service survives settings saves (imports.71):**') < text.index(SECTION)
    assert text.index(SECTION) < text.index('**Maintenance posture**')
    section = _section()
    for words in ('2026-11-21', 'Play Regular', '967 px', '831 px', '34 px', 'y = 68', 'GUITextBox.cpp:27',
                  'GUIFontManager.cpp:361-363', 'Control.cpp:1012-1021', 'GUILabel.cpp:112', 'GUIFontTTF.cpp:444-447',
                  'GUITextLayout.cpp:646-671', 'GUIControlFactory.cpp:1133-1135', 'Two labels per card', 'Decisions',
                  'Behaviour changes', 'Limits', 'Tests', 'Undoing it', 'Live checks', '0067c5cd', 'FreeType 2.13.3',
                  'HarfBuzz 10.2.0', 'GUILabelControl.cpp:290-293', 'Estuary, defines its own font27',
                  'GUIFontManager.cpp:495-503', 'GUIFont.cpp:111-112', 'ceil(70 / line height)', 'GUILabel.cpp:147',
                  'stop Kodi, move the live copy back', "the build's 69, the review's 11, the fix stage's 11"):
        assert words in section, words


def test_the_counts_in_the_docs_agree():
    """The engineering notes and the FORK_NOTES section give the same numbers, and the test files hold them."""
    notes, section = _entry().split('--- engineering notes ---\n', 1)[1], _section()
    for name in ('test_imports72_upnext_lines.py', 'test_imports72_docs.py'):
        found = [re.search(re.escape(name) + r'`? \((\d+)\)', text) for text in (notes, section)]
        assert all(found) and found[0].group(1) == found[1].group(1), name
    for pattern in (r'(\d+) pytest in the Madrid', r'(\d+) deliberate breaks'):
        assert re.search(pattern, notes).group(1) == re.search(pattern, section).group(1), pattern
    for placeholder in ('DOCS_COUNT', 'SUITE_COUNT', 'BREAK_COUNT'): assert placeholder not in notes + section


def test_the_setting_definitions_are_unchanged():
    """imports.72 changes no setting: the (id, type, default) of each of the 115 settings hash as at imports.70 and
    imports.71, so a rollback needs no settings migration."""
    rows = []
    for node in ET.parse(os.path.join(ADDON, 'resources', 'settings.xml')).iter('setting'):  # noqa: S314 -- the add-on's own
        default = node.find('default')
        text = (default.text or '') if default is not None else ''
        rows.append('%s|%s|%s' % (node.get('id'), node.get('type'), text))
    digest = hashlib.sha256('\n'.join(sorted(rows)).encode('utf-8')).hexdigest()
    assert (len(rows), digest) == (115, 'c9ee9ecbbdd28acc9eff53f061d6c6a58fcfd428f20f276b520137d8ce542801')


IPV4 = re.compile(r'(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?!\.?\d)')    # also one that ends a sentence
WEB  = re.compile(r'(?i)\b(?:https?|ftp)://|\bwww\.')
SECRET = re.compile(r'(?i)\bpass(?:word|wd)\b|\bpin\b|\bsecret|\btoken|\bapi_?key')
HOME = re.compile('/' + '(?:home|root|Users)' + '/')    # built so this file holds no such path


def release_texts():
    """{name: text} of what imports.72 wrote: the news, its changelog entry, its FORK_NOTES section, its test files,
    the font fixture, kodi_faithful.py's imports.72 part (its docstring paragraph and everything after its marker),
    the imports.72 lines of overlay.py and onnext_helpers.py (each comment run naming the release, with the line
    after it) and background.xml's comment."""
    texts = {'news': _news(), 'changelog': _entry(), 'FORK_NOTES': _section()}
    for name in sorted(os.listdir(HERE)):
        if name.startswith('test_imports72_') and name.endswith('.py'): texts[name] = _read(HERE, name)
    texts['fixture'] = _read(HERE, 'fixtures', 'font13_play_regular_29.json')
    faithful = _read(HERE, 'kodi_faithful.py')
    texts['kodi_faithful.py docstring'] = faithful[faithful.index('imports.72: text as Kodi'):faithful.index('"""\nimport')]
    texts['kodi_faithful.py imports.72'] = faithful[faithful.index('imports.72: text as Kodi 21.3 draws it\n'):]
    for name in ('overlay.py', 'onnext_helpers.py'):
        lines = _read(LIB, name).splitlines()
        for at, line in enumerate(lines):
            if 'imports.72' not in line: continue
            first, last = at, at
            while first > 0 and lines[first - 1].strip().startswith('#'): first -= 1
            while last + 1 < len(lines) and lines[last + 1].strip().startswith('#'): last += 1
            texts['%s:%d' % (name, at + 1)] = '\n'.join(lines[first:last + 2])
    window = _read(WINDOW)
    start = window.index('<!-- madteevee (imports fork, imports.72)')
    texts['background.xml'] = window[start:window.index('-->', start)]
    return texts


def test_no_address_web_address_credential_or_home_path():
    """The repo is public: no address, web address, home path or credential word in what imports.72 wrote."""
    texts = release_texts()
    assert {'news', 'changelog', 'FORK_NOTES', 'fixture', 'background.xml', 'test_imports72_docs.py',
            'test_imports72_upnext_lines.py', 'kodi_faithful.py imports.72'} <= set(texts)
    assert len([name for name in texts if name.startswith(('overlay.py:', 'onnext_helpers.py:'))]) >= 5
    for name, text in texts.items():
        assert IPV4.findall(text) == [], name
        assert WEB.findall(text) == [], name
        assert HOME.findall(text) == [], name
        # the credential words in the prose only: code names them, and the fixture's show titles include "Super
        # Secret Secret Squirrel" (a cartoon)
        if name in ('news', 'changelog', 'FORK_NOTES', 'background.xml'): assert SECRET.findall(text) == [], name
    assert SECRET.findall(texts['fixture']) == ['Secret'] * 4


def test_the_scans_see_what_they_look_for():
    # made at run time, so this file holds none of them itself (it is scanned too)
    address, web, home = '.'.join(['10', '0', '0', '1']), ['https' + '://', 'www' + '.'], '/' + 'home' + '/'
    assert IPV4.findall('at %s and 0.8.0+imports.72, then at %s.' % (address, address)) == [address, address]
    assert WEB.findall('see %sx and %sy' % tuple(web)) == web
    assert HOME.findall('in %sx' % home) == [home]
    assert SECRET.findall('the PIN, a password') == ['PIN', 'password']
    assert IPV4.findall('FreeType 2.13.3, HarfBuzz 10.2.0, Kodi 21.3') == []


def test_only_the_card_changed():
    """The release's code is the card's: overlay.py, onnext_helpers.py (wording) and background.xml; the Python
    files still parse as Python 3.8."""
    for name in ('overlay.py', 'onnext_helpers.py'):
        ast.parse(_read(LIB, name), feature_version=(3, 8))
    marked = sorted(name for name in os.listdir(LIB) if name.endswith('.py') and 'imports.72' in _read(LIB, name))
    assert marked == ['onnext_helpers.py', 'overlay.py']
