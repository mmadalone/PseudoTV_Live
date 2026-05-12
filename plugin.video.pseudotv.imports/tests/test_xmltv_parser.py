"""Unit tests for xmltv.parseExternalSource (live-imports streaming parser).

Covers:
- Plain XMLTV streaming → channel + programme dicts
- Gzip-compressed XMLTV (magic-byte detection, plan I5)
- channel_id_filter prunes programmes for non-imported channels (memory bound)
- Empty source / parse errors handled gracefully
- Source as bytes / file path / file-like object
"""
import io
import os

import pytest

from xmltv import parseExternalSource
from conftest import FIXTURES_DIR


def _load(name):
    with open(os.path.join(FIXTURES_DIR, name), 'rb') as f:
        return f.read()


# ---------------------------------------------------------------- happy path


def test_parses_xmltv_yields_channels_and_programmes():
    raw = _load('sample_xmltv.xml')
    items = list(parseExternalSource(raw))
    channels = [d for k, d in items if k == 'channel']
    programmes = [d for k, d in items if k == 'programme']
    assert len(channels) == 3
    assert len(programmes) == 5  # 4 valid + 1 ORPHAN that's still yielded (no filter)


def test_channel_dict_carries_id_and_display_name():
    raw = _load('sample_xmltv.xml')
    items = list(parseExternalSource(raw))
    tve = next(d for k, d in items if k == 'channel' and d.get('id') == 'TVE')
    # display-name is a list of (text, lang) tuples in elem_to_channel
    assert tve.get('display-name')


def test_channel_icon_extracted():
    raw = _load('sample_xmltv.xml')
    items = list(parseExternalSource(raw))
    tve = next(d for k, d in items if k == 'channel' and d.get('id') == 'TVE')
    icons = tve.get('icon', [])
    assert any(i.get('src') == 'https://example.com/tve.png' for i in icons)


def test_channel_without_icon_yields_empty_list():
    raw = _load('sample_xmltv.xml')
    items = list(parseExternalSource(raw))
    a3 = next(d for k, d in items if k == 'channel' and d.get('id') == 'A3')
    assert a3.get('icon') == []


def test_programme_attributes_preserved():
    raw = _load('sample_xmltv.xml')
    items = list(parseExternalSource(raw))
    progs = [d for k, d in items if k == 'programme']
    telediario = next(p for p in progs if p.get('channel') == 'TVE')
    assert telediario.get('start')
    assert telediario.get('stop')


# ---------------------------------------------------------------- filtering


def test_channel_id_filter_drops_non_imported_programmes():
    raw = _load('sample_xmltv.xml')
    # Only want programmes for TVE — drop everything else.
    items = list(parseExternalSource(raw, channel_id_filter={'TVE'}))
    progs = [d for k, d in items if k == 'programme']
    # 2 TVE programmes in fixture; 0 from LA2/A3/ORPHAN_NOT_IN_M3U.
    assert len(progs) == 2
    assert all(p.get('channel') == 'TVE' for p in progs)


def test_channel_id_filter_does_not_drop_channel_definitions():
    # Filter affects only programmes; <channel> elements always emitted
    # (caller may want them for icon lookup even if they don't receive
    # programmes from this source).
    raw = _load('sample_xmltv.xml')
    items = list(parseExternalSource(raw, channel_id_filter={'TVE'}))
    channels = [d for k, d in items if k == 'channel']
    assert len(channels) == 3


def test_channel_id_filter_accepts_iterable_not_just_set():
    raw = _load('sample_xmltv.xml')
    items = list(parseExternalSource(raw, channel_id_filter=['TVE', 'LA2']))
    progs = [d for k, d in items if k == 'programme']
    assert {p['channel'] for p in progs} == {'TVE', 'LA2'}


# ---------------------------------------------------------------- gzip


def test_gzip_detected_via_magic_bytes_and_decompressed():
    raw = _load('sample_xmltv.xml.gz')
    assert raw[:2] == b'\x1f\x8b'  # sanity: fixture is actually gzipped
    items = list(parseExternalSource(raw))
    channels = [d for k, d in items if k == 'channel']
    programmes = [d for k, d in items if k == 'programme']
    assert len(channels) == 3
    assert len(programmes) == 5


def test_gzip_with_filter_works():
    raw = _load('sample_xmltv.xml.gz')
    items = list(parseExternalSource(raw, channel_id_filter={'A3'}))
    progs = [d for k, d in items if k == 'programme']
    assert len(progs) == 1
    assert progs[0]['channel'] == 'A3'


# ---------------------------------------------------------------- input forms


def test_accepts_file_path_string():
    path = os.path.join(FIXTURES_DIR, 'sample_xmltv.xml')
    items = list(parseExternalSource(path))
    assert any(k == 'channel' for k, _ in items)


def test_accepts_file_like_object():
    raw = _load('sample_xmltv.xml')
    items = list(parseExternalSource(io.BytesIO(raw)))
    assert any(k == 'channel' for k, _ in items)


def test_str_file_like_input_encoded_to_utf8():
    raw_text = open(os.path.join(FIXTURES_DIR, 'sample_xmltv.xml'), 'r', encoding='utf-8').read()
    items = list(parseExternalSource(io.StringIO(raw_text)))
    assert any(k == 'channel' for k, _ in items)


# ---------------------------------------------------------------- defensive


def test_empty_bytes_yields_nothing():
    items = list(parseExternalSource(b''))
    assert items == []


def test_invalid_xml_yields_nothing_no_raise():
    # ParseError caught internally; iterator just stops.
    items = list(parseExternalSource(b'<not-valid xml'))
    # Should NOT raise; may yield 0 items or partial before error.
    assert isinstance(items, list)


def test_nonexistent_file_path_yields_nothing():
    items = list(parseExternalSource('/tmp/this-does-not-exist.xml'))
    assert items == []


def test_unsupported_source_type_raises_typeerror():
    with pytest.raises(TypeError):
        list(parseExternalSource(12345))
