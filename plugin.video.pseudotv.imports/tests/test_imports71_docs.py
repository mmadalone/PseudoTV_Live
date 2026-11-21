# -*- coding: utf-8 -*-
"""imports.71: the version, the changelog, the add-on's "What's new" news and FORK_NOTES.

The release's docs, pinned where they could drift: one version in addon.xml, the news header and the
changelog header; the news within Kodi's limit with the imports.69 block dropped and imports.70's kept;
the changelog pop-up (test_imports55 keeps its 2,500 limit) saying in plain words what the user will
notice; the engineering notes naming the code; the FORK_NOTES section with the cause, the Kodi source
lines, the three dates, the design, the decisions, the limits and the live checks. The setting
definitions are imports.70's, so a rollback needs no settings migration. No address, web address or
credential in any of it, or in the release's tests.
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

VERSION = '0.8.0+imports.71'
SECTION = '**The service survives settings saves (imports.71):**'


def _read(*parts):
    with open(os.path.join(*parts), encoding='utf-8') as fh: return fh.read()


def _addon():
    return ET.parse(os.path.join(ADDON, 'addon.xml')).getroot()  # noqa: S314 -- the add-on's own addon.xml


def _news():
    return _addon().find('.//news').text


def _entry():
    """The imports.71 changelog entry, the pop-up part and the engineering notes."""
    text = _read(ADDON, 'changelog.txt')
    start = text.index('v.%s\n' % VERSION)
    return text[start:text.index('\nv.0.8.0+imports.70\n')]


def _section():
    text = _read(ROOT, 'FORK_NOTES.md')
    start = text.index(SECTION)
    # imports.72 (a deliberate edit): the section ends where the next one begins (imports.72's follows it now)
    return text[start:text.index('\n**', start + len(SECTION))]


def test_one_version_everywhere():
    # imports.72 (a deliberate edit): imports.71 is no longer the newest release. Its entry and its news block are
    # still there, after the newer ones; the add-on's version is pinned by the newest release's docs test.
    version = _addon().get('version')
    assert version.startswith('0.8.0+imports.') and int(version.rpartition('.')[2]) > 71
    assert 'v%s\n' % VERSION in _news() and not _news().startswith('v%s\n' % VERSION)
    text = _read(ADDON, 'changelog.txt')
    assert text.count('\nv.%s\n' % VERSION) == 1 and not changelog_helpers.latestEntry(text).startswith('v.%s\n' % VERSION)


def test_the_news_fits_kodis_limit_and_says_what_changed():
    """V1. Kodi shows at most 1,500 characters. imports.72 (a deliberate edit): imports.72's block leads, the imports.71
    block follows, imports.70's left the news."""
    news = _news()
    assert len(news) <= 1500
    assert [line for line in news.splitlines() if line.startswith('v0.8.0+imports.')] == [
        'v0.8.0+imports.72', 'v0.8.0+imports.71']
    block = news.split('v0.8.0+imports.71\n', 1)[1]
    for words in ('no longer stops by itself', 'Listening', 'Force background service to reload', 'restart Kodi'):
        assert words in block, words


def test_the_pop_up_says_what_you_will_notice():
    """V2. The plan's 'What you'll notice', in plain words, above the engineering notes; within the pop-up's
    limit (test_imports55 holds it at 2,500). imports.72 (a deliberate edit): the pop-up part of imports.71's entry,
    no longer the newest."""
    popup = _entry().split('--- engineering notes ---', 1)[0]
    assert len(popup) < 2500 and popup.count('\nv.') == 0
    for words in ('no longer stops by itself', 'three times', 'Something Went Wrong', 'each different error once',
                  'no longer saves its settings every few minutes', '"Listening"', 'Online or Offline',
                  '"Force background service to reload"', 'restarts the service once', 'the Manager',
                  'restoring a channel backup', "data folder", '"Restart Kodi to finish', '"Enable Executor"',
                  'about 15 seconds'):
        assert words in popup, words
    for code in ('REAL_SETTINGS', '_start', 'pendingRestart', 'TypeError', '.py'):
        assert code not in popup, code


def test_no_reminder_line_is_promised():
    """The review's case (fix stage): the user decided against reminder lines (each different error once,
    2026-11-20), and the code writes none. The pop-up and the news never mention one; the engineering notes
    and the FORK_NOTES section only to say there are none ('no reminder lines'), and the FORK_NOTES
    Decisions bullet says so."""
    # imports.72 (a deliberate edit): imports.71's pop-up part and news block, no longer the newest
    popup = _entry().split('--- engineering notes ---', 1)[0]
    for name, text in (('pop-up', popup), ('news', _news().split('v0.8.0+imports.71\n', 1)[1])):
        assert 'reminder' not in text.lower() and 'every 10 minutes' not in text, name
    notes = _entry().split('--- engineering notes ---\n', 1)[1]
    for name, text in (('engineering notes', notes), ('FORK_NOTES', _section())):
        lowered = text.lower()
        assert lowered.count('reminder') == lowered.count('no reminder lines') >= 1, name
        assert 'every 10 minutes' not in text, name
    decisions = [line for line in _section().splitlines() if line.startswith('- Decisions')]
    assert len(decisions) == 1 and 'no reminder lines' in decisions[0]


def test_the_engineering_notes_name_the_code():
    notes = _entry().split('--- engineering notes ---\n', 1)[1]
    for words in ('Addon.cpp:375-406', 'AddonManager.cpp:214-225', 'Service.cpp:37-52', 'cqueue.py', '_chkExecutor',
                  'Globals._readSetting', 'chkServiceChannels', 'loopguard_helpers.py', '_passFailed', '_tickFailed',
                  'Service._stop', '_kodiRestart', 'setPendingKodiRestart', '30910', '30911',
                  'test_imports71_service_loop.py', 'test_imports71_docs.py', 'Madrid, UTC, New York and Kolkata'):
        assert words in notes, words


def test_fork_notes_has_the_section():
    """V3. After the imports.70 replay section, before 'Maintenance posture'."""
    text = _read(ROOT, 'FORK_NOTES.md')
    assert text.count(SECTION) == 1
    assert text.index('**Replays frozen at their end move on (imports.70):**') < text.index(SECTION)
    section = _section()
    for words in ('2026-06-05', '2026-10-04', '2026-11-07', '3 days 17 h', 'Addon.cpp:375-406',
                  'AddonManager.cpp:214-225', 'Addon.cpp:286-319', 'Service.cpp:37-52', 'PythonInvoker.cpp:382-407',
                  'about 274', 'Decisions', 'Limits', 'Live checks', 'Restart Kodi to finish', 'Debug_Level',
                  'Enable_Executor', 'chkServiceChannels', 'loopguard_helpers', 'pendingKodiRestart'):
        assert words in section, words


def test_the_setting_definitions_are_imports70s():
    """V6. imports.71 changes no setting: the (id, type, default) of each of the 115 settings hash as at 399744fd
    (imports.70), so a rollback needs no settings migration."""
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


def test_no_address_web_address_or_credential():
    """V7. In the release's tests, its changelog entry, the news and the FORK_NOTES section."""
    texts = {'news': _news(), 'changelog': _entry(), 'FORK_NOTES': _section()}
    for name in sorted(os.listdir(HERE)):
        if name.startswith('test_imports71_') and name.endswith('.py'): texts[name] = _read(HERE, name)
    assert len(texts) == 7
    for name, text in texts.items():
        assert IPV4.findall(text) == [], name
        assert WEB.findall(text) == [], name
        if not name.endswith('.py'): assert SECRET.findall(text) == [], name


# The GPL licence link every source file's header carries (built here, so this file holds no web address).
GPL_LINK = '<' + 'http' + '://' + 'www' + '.gnu.org/licenses/>'


def release_spots(name, text):
    """{label: text} of what imports.71 wrote in one Python file: each def whose lines name the release
    (each changed spot names it: the tests' rule) and each run of comment lines naming it, with the line
    after it (a class or module statement, or the code the comment is about). Lexical, as the rule is."""
    found, lines = {}, text.splitlines()
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            segment = '\n'.join(lines[node.lineno - 1:node.end_lineno])
            if 'imports.71' in segment: found['%s:%s:%d' % (name, node.name, node.lineno)] = segment
    for at, line in enumerate(lines):
        if 'imports.71' not in line: continue
        first = last = at
        while first > 0 and lines[first - 1].strip().startswith('#'): first -= 1
        while last + 1 < len(lines) and lines[last + 1].strip().startswith('#'): last += 1
        found['%s:%d' % (name, at + 1)] = '\n'.join(lines[first:last + 2])
    return found


def release_code():
    """{label: text}: imports.71's code beyond its own test files (the review's case, fix stage): its spots
    in resources/lib, loopguard_helpers.py whole (its licence link aside), kodi_faithful.py's imports.71
    part (its docstring paragraph and everything after its imports.71 marker), the spots of the imports.69
    and imports.70 tests it edited on purpose, and its strings in strings.po."""
    texts = {}
    for name in sorted(os.listdir(LIB)):
        if not name.endswith('.py'): continue
        text = _read(LIB, name)
        if name == 'loopguard_helpers.py': texts[name] = text.replace(GPL_LINK, '', 1)
        else: texts.update(release_spots(name, text))
    for name in ('test_imports70_replay_end.py', 'test_imports69_importcfg_helpers.py'):
        texts.update(release_spots(name, _read(HERE, name)))
    faithful = _read(HERE, 'kodi_faithful.py')
    start, end = faithful.index('imports.71: the add-on'), faithful.index('"""\nimport')
    texts['kodi_faithful.py docstring'] = faithful[start:end]
    texts['kodi_faithful.py imports.71'] = faithful[faithful.index("imports.71: the add-on's settings\n"):]
    po = _read(ADDON, 'resources', 'language', 'resource.language.en_gb', 'strings.po')
    texts['strings.po'] = po[po.index('imports.71'):po.index('msgctxt "#33900"')]
    return texts


def test_no_address_in_the_release_code():
    """V7b, the review's case (fix stage): the repo is public, and the rule covers code, tests and comments,
    not only the release's own test files and docs. No address and no web address in what imports.71
    wrote anywhere else (release_code); no credential word in its strings."""
    texts = release_code()
    assert 'loopguard_helpers.py' in texts and 'strings.po' in texts and len(texts) > 40
    assert any(label.startswith('services.py:_kodiRestart') for label in texts)
    for label, text in texts.items():
        assert IPV4.findall(text) == [], label
        assert WEB.findall(text) == [], label
    assert SECRET.findall(texts['strings.po']) == []


def test_the_scans_see_what_they_look_for():
    # made at run time, so this file holds none of them itself (it is scanned too)
    address, web = '.'.join(['10', '0', '0', '1']), ['https' + '://', 'www' + '.']
    assert IPV4.findall('at %s and 0.8.0+imports.71, then at %s.' % (address, address)) == [address, address]
    assert WEB.findall('see %sx and %sy' % tuple(web)) == web
    assert SECRET.findall('the PIN, a password') == ['PIN', 'password']
    # release_spots sees a tagged comment in a def, a tagged class statement, and leaves the rest alone
    source = ('class C:\n    # madteevee (imports fork, imports.71): the flag\n    FLAG = %r\n\n'
              '    def f(self):\n        # madteevee (imports fork, imports.71): seen at %s\n        return 1\n\n'
              '    def g(self):\n        return %r\n') % (web[1] + 'x', address, web[0] + 'y')
    spots = release_spots('x.py', source)
    assert sorted(spots) == ['x.py:2', 'x.py:6', 'x.py:f:5']
    found = sorted(hit for text in spots.values() for hit in IPV4.findall(text) + WEB.findall(text))
    assert found == sorted([address, address, web[1]])                # g() names no release: not scanned
    assert GPL_LINK in _read(LIB, 'loopguard_helpers.py') and WEB.findall(GPL_LINK)
