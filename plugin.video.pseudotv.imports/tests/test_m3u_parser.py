"""Unit tests for M3U.parseExternalSource (live-imports parser).

Covers the cases the live-imports feature will exercise:
- MovistarPlus-shaped M3U with mixed attribute sets
- Duplicate tvg-id filtering (matches existing M3U._load behavior)
- Missing #EXTM3U header (defensive — should raise per plan G24)
- Missing URL line for an entry (defensive skip per plan H7)
- Encoding edge cases: BOM stripping, UTF-8 with errors=replace fallback (G2)
"""
import os

import pytest

# conftest.py prepends sys.path with KODI_STUBS + ADDON_LIB before this
# module is collected, so the next import resolves cleanly.
from m3u import M3U
from conftest import FIXTURES_DIR


def _load_fixture(name):
    with open(os.path.join(FIXTURES_DIR, name), 'rb') as f:
        return f.read()


# ---------------------------------------------------------------- happy path


def test_parses_movistarplus_sample_yields_5_channels():
    raw = _load_fixture('sample_movistarplus.m3u8')
    out = M3U.parseExternalSource(raw)
    assert len(out) == 5
    names = [c['name'] for c in out]
    assert names == ['LA 1', 'LA 2', 'Antena 3', 'Cuatro', 'Telecinco']


def test_extracts_tvg_id_per_channel():
    raw = _load_fixture('sample_movistarplus.m3u8')
    out = M3U.parseExternalSource(raw)
    ids = [c['id'] for c in out]
    assert ids == ['TVE', 'LA2', 'A3', 'C4', 'T5']


def test_extracts_tvg_chno_as_int():
    raw = _load_fixture('sample_movistarplus.m3u8')
    out = M3U.parseExternalSource(raw)
    nums = [c['number'] for c in out]
    assert nums == [1, 2, 3, 4, 5]
    assert all(isinstance(n, int) for n in nums)


def test_extracts_tvg_logo_url_verbatim():
    raw = _load_fixture('sample_movistarplus.m3u8')
    out = M3U.parseExternalSource(raw)
    assert out[0]['logo'] == 'https://example.com/tve.png'


def test_extracts_group_title_as_list():
    raw = _load_fixture('sample_movistarplus.m3u8')
    out = M3U.parseExternalSource(raw)
    # group-title="Movistar+" → ['Movistar+'] (split on ; produces single element)
    assert out[0]['group'] == ['Movistar+']


def test_extracts_url_from_line_after_extinf():
    raw = _load_fixture('sample_movistarplus.m3u8')
    out = M3U.parseExternalSource(raw)
    assert out[0]['url'].startswith('plugin://plugin.video.movistarplus/?action=play&id=TVE')


def test_extracts_catchup_source_when_present():
    raw = _load_fixture('sample_movistarplus.m3u8')
    out = M3U.parseExternalSource(raw)
    cuatro = next(c for c in out if c['name'] == 'Cuatro')
    assert cuatro['catchup'] == 'vod'
    assert 'catchup' in cuatro['catchup-source']


# ---------------------------------------------------------------- defensive


def test_missing_extm3u_header_raises_value_error():
    raw = _load_fixture('malformed_no_header.m3u')
    with pytest.raises(ValueError, match='#EXTM3U'):
        M3U.parseExternalSource(raw)


def test_entry_with_no_url_line_is_skipped():
    raw = _load_fixture('malformed_missing_url.m3u')
    out = M3U.parseExternalSource(raw)
    # 'GoodOne' has URL → kept; 'NoUrl' has no following URL line → skipped.
    names = [c['name'] for c in out]
    assert names == ['GoodOne']


def test_duplicate_tvg_id_is_filtered_out():
    raw = _load_fixture('duplicates.m3u')
    out = M3U.parseExternalSource(raw)
    # 'First' has tvg-id="DUP" → kept; 'Second' has tvg-id="DUP" → filtered.
    # 'Unique' has its own id → kept.
    names = [c['name'] for c in out]
    assert names == ['First', 'Unique']


# ---------------------------------------------------------------- encoding


def test_utf8_bom_is_stripped():
    raw = b'\xef\xbb\xbf' + _load_fixture('sample_movistarplus.m3u8')
    out = M3U.parseExternalSource(raw)
    assert len(out) == 5  # parses identically; BOM stripped


def test_utf8_with_undecodable_bytes_falls_back_to_errors_replace():
    # Inject an invalid UTF-8 byte sequence in the middle of a name.
    # Should NOT raise — falls back to errors='replace' (plan G2).
    raw = b'#EXTM3U\n#EXTINF:-1 tvg-name="Bad\xff\xfeName" tvg-id="X",Bad\xff\xfeName\nhttp://ex/x\n'
    out = M3U.parseExternalSource(raw)
    assert len(out) == 1
    # Replacement character lands in the decoded string somewhere
    assert '�' in out[0]['name']


def test_str_input_is_accepted_directly():
    # The parser should accept already-decoded str (caller may pre-decode).
    raw_text = """#EXTM3U
#EXTINF:-1 tvg-name="Plain" tvg-id="P" tvg-chno="42",Plain
http://example.com/plain
"""
    out = M3U.parseExternalSource(raw_text)
    assert len(out) == 1
    assert out[0]['name'] == 'Plain'
    assert out[0]['number'] == 42


# ---------------------------------------------------------------- log callback


def test_log_callback_invoked_on_dedup():
    raw = _load_fixture('duplicates.m3u')
    captured = []
    M3U.parseExternalSource(raw, log_callback=captured.append)
    assert any('duplicate' in m.lower() for m in captured)


def test_log_callback_optional():
    # Default None — must not error.
    raw = _load_fixture('sample_movistarplus.m3u8')
    out = M3U.parseExternalSource(raw)
    assert len(out) == 5
