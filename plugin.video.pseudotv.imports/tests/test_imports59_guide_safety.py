"""imports.59 (S1, S4.3) — one bad guide entry can never blank or freeze the
guide, and a half-written links file is never taken as complete.

Kept replay history exists only in pseudotv.xml (older Movistar+ files can't
be downloaded again), so each of these used to turn one bad value into the
loss of that history:
  - a catchup-id of None/int or a stop of None made render_xmltv fail at
    serialise time and its fatal branch return a '<tv/>' stub, which every
    caller then wrote over the whole guide;
  - xmltv.Writer attached a <programme>/<channel> before filling it, so a
    failed check left a half-written element;
  - a stop-less programme made cleanProgrammes call strpTime(None), whose
    TypeError fallback raised NameError ('ime' typo);
  - sortProgrammes returned [] on any error (it runs on every load and save).

S4.3: parseExternalSource(strict=True) raises on empty input and re-raises
the ParseError of a truncated document; the default stays lenient.
"""
import datetime as _real_datetime
import io
import types
from xml.etree.ElementTree import ParseError

import pytest

import globals as globals_mod
import renderers
import xmltv
import xmltvs
from xmltv import parseExternalSource, Writer
from xmltvs import XMLTVS


def _prog(n, **over):
    p = {'channel': 'TVE@mp', 'start': '2099010%d000000 +0000' % n, 'stop': '2099010%d010000 +0000' % n,
         'catchup-id': '', 'title': [('Show %d' % n, 'es')], 'desc': [('Desc %d' % n, 'es')]}
    p.update(over)
    return p


def _data(programmes, channels=None):
    return {'data': {'date': '20990101000000'},
            'channels': channels if channels is not None else
            [{'id': 'TVE@mp', 'display-name': [('TVE', 'es')], 'icon': []}],
            'recordings': [], 'programmes': programmes}


def _reparse(rendered):
    chans, progs = [], []
    for kind, item in parseExternalSource(rendered):
        (chans if kind == 'channel' else progs).append(item)
    return chans, progs


# ================================================================ Writer.setattr / detached build


def test_none_catchup_id_left_out_int_written_as_text():
    out = renderers.render_xmltv(_data([_prog(1, **{'catchup-id': None}),
                                        _prog(2, **{'catchup-id': 12345}),
                                        _prog(3, **{'catchup-id': 'plugin://x/?a=1&b=2'})]))
    assert out is not None and b'<tv/>' not in out
    chans, progs = _reparse(out)
    assert [c['id'] for c in chans] == ['TVE@mp']
    assert [p['title'][0][0] for p in progs] == ['Show 1', 'Show 2', 'Show 3']
    assert [p['catchup-id'] for p in progs] == ['', '12345', 'plugin://x/?a=1&b=2']
    assert b'catchup-id="12345"' in out
    first = out.split(b'<programme ')[1].split(b'>')[0]
    assert b'catchup-id' not in first, 'a None attribute is left out, not written'


def test_writer_failed_programme_leaves_no_partial_element():
    w = Writer(date='20990101000000')
    with pytest.raises(KeyError):
        w.addProgramme({'channel': 'TVE@mp', 'start': '20990101000000 +0000'})   # no title
    with pytest.raises(ValueError):
        w.addProgramme({'channel': 'TVE@mp', 'start': None, 'title': [('x', '')]})
    with pytest.raises(ValueError):
        w.addProgramme({'channel': None, 'start': '20990101000000 +0000', 'title': [('x', '')]})
    with pytest.raises(ValueError):
        w.addProgramme({'channel': 'TVE@mp', 'start': '20990101000000 +0000', 'title': [('x', '')],
                        'icon': [{'width': '1'}]})                                # icon without src
    assert len(w.root) == 0, 'nothing attached for a failed programme'
    w.addProgramme(_prog(1))
    assert len(w.root) == 1 and w.root[0].tag == 'programme'


def test_writer_failed_channel_leaves_no_partial_element():
    w = Writer(date='20990101000000')
    with pytest.raises(KeyError):
        w.addChannel({'id': 'TVE@mp'})                         # no display-name
    with pytest.raises(ValueError):
        w.addChannel({'id': None, 'display-name': [('x', '')]})
    with pytest.raises(ValueError):
        w.addChannel({'id': 'TVE@mp', 'display-name': [('x', '')], 'icon': [{}]})
    assert len(w.root) == 0
    w.addChannel({'id': 'TVE@mp', 'display-name': [('TVE', '')]})
    assert len(w.root) == 1 and w.root[0].get('id') == 'TVE@mp'


def test_writer_non_text_length_units_no_longer_break_the_document():
    p = _prog(1, length={'units': None, 'length': '60'})
    out = renderers.render_xmltv(_data([p]))
    assert out is not None and b'<length>60</length>' in out


# ================================================================ render_xmltv


@pytest.mark.parametrize('bad', [
    {'stop': None}, {'stop': ''}, {'stop': '   '}, {'start': None}, {'channel': None}, {'channel': ''},
    {'stop': 5},
])
def test_programme_without_start_stop_or_channel_is_skipped(bad, monkeypatch):
    logged = []
    monkeypatch.setattr(renderers, '_log', lambda msg, level=None: logged.append((msg, level)))
    out = renderers.render_xmltv(_data([_prog(1), _prog(2, **bad), _prog(3)]))
    assert out is not None
    chans, progs = _reparse(out)
    assert [p['title'][0][0] for p in progs] == ['Show 1', 'Show 3']
    assert b'Show 2' not in out, 'no partial element left'
    summary = [m for m, lvl in logged if 'skipped 1 programmes without a start, stop or channel' in m]
    assert len(summary) == 1 and [lvl for m, lvl in logged if m in summary] == [renderers.xbmc.LOGWARNING]


def test_programme_without_title_is_skipped_and_output_reloads(tmp_path, monkeypatch):
    """A missing title: that programme is skipped (no partial element) and the
    rendered file loads through cleanSelf/cleanProgrammes without raising."""
    no_title = _prog(2)
    no_title.pop('title')
    out = renderers.render_xmltv(_data([_prog(1), no_title, _prog(3)]))
    assert out is not None and b'Desc 2' not in out
    path = tmp_path / 'pseudotv.xml'
    path.write_bytes(out)
    progs = xmltv.read_programmes(io.BytesIO(out))
    assert [p['title'][0][0] for p in progs] == ['Show 1', 'Show 3']
    x = XMLTVS.__new__(XMLTVS)
    x.m3u = None
    x.XMLTVDATA = {}
    assert len(x.cleanProgrammes(progs)) == 2
    import channels as channels_module

    class _Ch(object):
        def __init__(self, *a, **k): pass
        def getChannels(self): return [{'id': 'TVE@mp', 'number': 1, 'type': 'import', 'enabled': True}]
        def getImports(self): return [{'id': 'mp'}]
    monkeypatch.setattr(channels_module, 'Channels', _Ch)
    assert len(x.cleanSelf(progs, 'channel')) == 2


def test_forced_fatal_returns_none(monkeypatch):
    def boom(self, file, pretty_print=False):
        raise RuntimeError('simulated serialise failure')
    monkeypatch.setattr(xmltv.Writer, 'write', boom)
    assert renderers.render_xmltv(_data([_prog(1)])) is None


def test_fatal_branch_no_longer_returns_tv_stub():
    """No bytes literal (the old '<tv/>' stub) is left in render_xmltv."""
    import ast
    import inspect
    import textwrap
    tree = ast.parse(textwrap.dedent(inspect.getsource(renderers.render_xmltv)))
    stubs = [n for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, bytes)]
    assert stubs == [], 'the fatal branch must not hand back a stub'


# ================================================================ sortProgrammes


def _x():
    x = XMLTVS.__new__(XMLTVS)
    x.m3u = None
    x.XMLTVDATA = {}
    return x


def test_sort_with_none_start_keeps_the_list():
    progs = [_prog(2), {'channel': 'A@mp', 'start': None, 'title': [('n', '')]},
             {'channel': None, 'start': '20990101000000 +0000'}, _prog(1, channel='A@mp')]
    out = _x().sortProgrammes(progs)
    assert out is progs and len(out) == 4
    assert [(p.get('channel'), p.get('start')) for p in out] == [
        (None, '20990101000000 +0000'), ('A@mp', None), ('A@mp', _prog(1)['start']), ('TVE@mp', _prog(2)['start'])]


def test_sort_valid_data_gives_todays_order():
    """Today's order = sort by start, then (stable) by channel."""
    progs = [_prog(3, channel='B@mp'), _prog(1, channel='B@mp'), _prog(2, channel='A@mp'),
             _prog(1, channel='A@mp', title=[('dup-a', '')]), _prog(1, channel='A@mp', title=[('dup-b', '')])]
    expected = sorted(sorted(list(progs), key=lambda p: p['start']), key=lambda p: p['channel'])
    out = _x().sortProgrammes(list(progs))
    assert out == expected
    assert [p['title'][0][0] for p in out][:2] == ['dup-a', 'dup-b'], 'stable for equal keys'


def test_sort_error_logs_and_returns_list_unchanged():
    progs = [_prog(2), {'channel': 5, 'start': '1'}, _prog(1)]
    snapshot = list(progs)
    out = _x().sortProgrammes(progs)
    assert out == snapshot, 'unsortable mix: list unchanged, nothing lost'


# ================================================================ cleanProgrammes (S1.4)


def test_clean_programmes_keeps_stopless_programme_without_strptime(monkeypatch):
    calls = []
    real = xmltvs.strpTime
    monkeypatch.setattr(xmltvs, 'strpTime', lambda *a, **k: calls.append(a) or real(*a, **k))
    progs = [{'channel': 'TVE@mp', 'start': '20990101000000 +0000', 'stop': None, 'title': [('a', '')]},
             {'channel': 'TVE@mp', 'start': '20990101000000 +0000', 'title': [('b', '')]}]
    out = _x().cleanProgrammes(progs)
    assert out == progs
    assert calls == [], 'a non-text stop never reaches strpTime'


# ================================================================ strpTime (S1.5)


def test_strptime_typeerror_fallback_returns_datetime(monkeypatch):
    class _BrokenDatetime(object):
        @staticmethod
        def strptime(s, f):
            raise TypeError('simulated Kodi embedded-Python strptime bug')
        fromtimestamp = staticmethod(_real_datetime.datetime.fromtimestamp)
    fake = types.SimpleNamespace(datetime=_BrokenDatetime, timedelta=_real_datetime.timedelta)
    monkeypatch.setattr(globals_mod, 'datetime', fake)
    out = globals_mod.strpTime('20261103120000', '%Y%m%d%H%M%S')
    assert isinstance(out, _real_datetime.datetime)
    assert (out.year, out.month, out.day, out.hour, out.minute) == (2026, 11, 3, 12, 0)


def test_strptime_bad_text_still_returns_empty_string():
    assert globals_mod.strpTime('not a date', '%Y%m%d%H%M%S') == ''


# ================================================================ parseExternalSource strict (S4.3)


GOOD = (b'<?xml version="1.0" encoding="UTF-8"?><tv><channel id="C1"><display-name>One</display-name></channel>'
        b'<programme start="20990101000000 +0000" stop="20990101010000 +0000" channel="C1" catchup-id="u1">'
        b'<title>A</title></programme>'
        b'<programme start="20990101010000 +0000" stop="20990101020000 +0000" channel="C1" catchup-id="u2">'
        b'<title>B</title></programme></tv>')


def test_strict_complete_document_parses_like_default():
    assert list(parseExternalSource(GOOD, strict=True)) == list(parseExternalSource(GOOD))


def test_strict_raises_on_empty_input():
    with pytest.raises(ParseError):
        list(parseExternalSource(b'', strict=True))
    assert list(parseExternalSource(b'')) == [], 'default stays lenient'


def test_strict_reraises_truncated_document():
    half = GOOD[: len(GOOD) - 40]
    with pytest.raises(ParseError):
        list(parseExternalSource(half, strict=True))
    lenient = list(parseExternalSource(half))
    assert lenient and lenient[0][0] == 'channel', 'default returns what was read before the error'


def test_strict_raises_on_unreadable_path(tmp_path):
    missing = str(tmp_path / 'nope.xml')
    with pytest.raises(OSError):
        list(parseExternalSource(missing, strict=True))
    assert list(parseExternalSource(missing)) == []


def test_strict_keeps_channel_id_filter():
    items = list(parseExternalSource(GOOD.replace(b'channel="C1" catchup-id="u2"', b'channel="C9" catchup-id="u2"'),
                                     channel_id_filter={'C1'}, strict=True))
    assert [i[1].get('catchup-id') for i in items if i[0] == 'programme'] == ['u1']
