"""imports.60 (D8) - MP4 reader: an MP4 whose moov has no 'udta' box gets its duration.

The MP4Parser fallback looked up 'trak' and 'udta' boxes it never used, so a film
laid out ftyp + free(8 bytes) + moov(mvhd + trak, no udta) + mdat raised
KeyError b'udta', got no duration, was never cached, and fell through to
NFOParser (which fails on combination NFOs) on every build. Port of upstream
10a2728b's fallback fix + its later try/finally close; upstream's tkhd fallback
and 2 GB box-size cap are deliberately NOT ported (both give wrong answers).
"""
import ast
import struct
import types

import pytest
import xbmc

from parsers import MP4Parser as mp4


def _box(typ, payload=b''):
    return struct.pack('>I4s', 8 + len(payload), typ) + payload


def _mvhd(timescale, duration, version=0):
    if version == 1: head = struct.pack('>B3xQQIQ', 1, 0, 0, timescale, duration)
    else:            head = struct.pack('>B3xIIII', 0, 0, 0, timescale, duration)
    return _box(b'mvhd', head + b'\0' * 80)


def _trak(tkhd_duration, mdhd_timescale):
    # tkhd duration is in the MOVIE timescale; mdhd has its own media timescale.
    tkhd = _box(b'tkhd', struct.pack('>B3xIIIII', 0, 0, 0, 1, 0, tkhd_duration) + b'\0' * 60)
    mdhd = _box(b'mdhd', struct.pack('>B3xIIIIHH', 0, 0, 0, mdhd_timescale,
                                     tkhd_duration * mdhd_timescale // 1000, 0x55c4, 0))
    return _box(b'trak', tkhd + _box(b'mdia', mdhd))


FTYP  = _box(b'ftyp', b'isom' + struct.pack('>I', 0x200) + b'isomiso2avc1mp41')
FREE8 = _box(b'free')                      # header-only box (ffmpeg's 64-bit-size placeholder)
MDAT  = _box(b'mdat', b'\0' * 16)
UDTA  = _box(b'udta', _box(b'meta', b'\0' * 12))
MS    = 5291400                            # 5291.4 s: rounding and flooring agree (5291)
MS_HALF = 5291600                          # 5291.6 s: fallback rounds (5292), readHeader floors (5291)
TRAK  = _trak(MS, 24000)


class _FakeVFSFile(object):
    """What FileAccess.open returns (VFSFile over xbmcvfs.File): read() -> str,
    readBytes() -> bytearray, seek(offset, whence). Sparse: ('MDAT', size) is a
    zero-filled mdat of that size, so multi-GB layouts cost nothing."""
    def __init__(self, parts, fail_absolute_seek=False):
        self.segments, off = [], 0
        for part in parts:
            if isinstance(part, tuple):
                self.segments.append((off, struct.pack('>I4s', part[1], b'mdat')))
                off += part[1]
            else:
                self.segments.append((off, part))
                off += len(part)
        self.size, self.pos, self.closed = off, 0, False
        self.fail_absolute_seek = fail_absolute_seek

    def _take(self, n):
        n = max(0, min(n, self.size - self.pos))
        buf = bytearray(n)
        for off, data in self.segments:
            lo, hi = max(off, self.pos), min(off + len(data), self.pos + n)
            if lo < hi: buf[lo - self.pos:hi - self.pos] = data[lo - off:hi - off]
        self.pos += n
        return buf

    def read(self, n=0):      return bytes(self._take(n)).decode('latin-1')
    def readBytes(self, n=0): return self._take(n)

    def seek(self, off, whence=1):
        if whence == 0 and self.fail_absolute_seek: raise OSError('simulated I/O error')
        self.pos = off if whence == 0 else (self.pos + off if whence == 1 else self.size + off)
        return self.pos

    def close(self): self.closed = True


@pytest.fixture
def run(monkeypatch):
    lines = []
    monkeypatch.setattr(mp4, 'log', lambda msg, level=xbmc.LOGDEBUG: lines.append((level, msg)))
    # The stub xbmc.Monitor.abortRequested() returns True on every 5th call and
    # MP4Parser.monitor is a class attribute, so without this the box loops
    # "abort" at points that depend on earlier tests.
    monkeypatch.setattr(mp4.MP4Parser, 'monitor', types.SimpleNamespace(abortRequested=lambda: False))

    def _run(parts, **kw):
        fle = _FakeVFSFile(parts, **kw)
        monkeypatch.setattr(mp4, 'FileAccess', types.SimpleNamespace(open=lambda *a, **k: fle))
        return mp4.MP4Parser().determineLength('/movies/film.mp4'), fle, lines
    return _run


def _errors(lines):
    return [msg for level, msg in lines if level >= xbmc.LOGERROR]


@pytest.mark.parametrize('version, timescale', [(0, 1000), (1, 90000)], ids=['mvhd-v0', 'mvhd-v1'])
def test_free_box_without_udta_gets_duration(run, version, timescale):
    """The 7 films' layout: readHeader stops at the empty 'free' box, the fallback reads mvhd."""
    moov = _box(b'moov', _mvhd(timescale, MS * timescale // 1000, version) + TRAK)
    dur, fle, lines = run([FTYP, FREE8, moov, MDAT])
    assert dur == 5291
    assert fle.closed
    assert _errors(lines) == []


def test_free_box_with_udta_unchanged(run):
    dur, fle, _ = run([FTYP, FREE8, _box(b'moov', _mvhd(1000, MS) + TRAK + UDTA), MDAT])
    assert dur == 5291 and fle.closed


def test_fallback_rounds_to_nearest_second(run):
    """Pins today's fallback rounding (scan_mvhd uses round()). Changes to 5291 only
    if readHeader is ever taught to step over empty boxes (then it reads this file)."""
    dur, fle, lines = run([FTYP, FREE8, _box(b'moov', _mvhd(1000, MS_HALF) + TRAK), MDAT])
    assert dur == 5292
    assert any('Using New Parser' in msg for _, msg in lines)


def test_no_free_box_still_read_by_readheader(run):
    dur, fle, lines = run([FTYP, _box(b'moov', _mvhd(1000, MS_HALF) + TRAK + UDTA), MDAT])
    assert dur == 5291                    # readHeader floors - unchanged by imports.60
    assert not any('Using New Parser' in msg for _, msg in lines)
    assert fle.closed


def test_moov_after_mdat_without_udta(run):
    dur, fle, _ = run([FTYP, FREE8, MDAT, _box(b'moov', _mvhd(1000, MS) + TRAK)])
    assert dur == 5291 and fle.closed


def test_mdat_over_2gb_before_moov_is_walked(run):
    """32-bit box sizes go up to 4 GB; upstream's 2 GB cap would lose this moov."""
    dur, fle, _ = run([FTYP, FREE8, ('MDAT', 3 * 1024 ** 3), _box(b'moov', _mvhd(1000, MS) + TRAK)])
    assert dur == 5291 and fle.closed


@pytest.mark.parametrize('parts', [
    [FTYP, FREE8, MDAT],
    [_box(b'wide'), MDAT, _box(b'moov', _mvhd(1000, MS) + TRAK)],
    [FTYP, FREE8, _box(b'moov', TRAK), MDAT],
    [FTYP, FREE8, _box(b'moov', _mvhd(1000, 0) + TRAK), MDAT],
], ids=['no-moov', 'no-ftyp', 'no-mvhd', 'zero-mvhd'])
def test_unreadable_returns_zero_quietly(run, parts):
    """0 lets VideoParser try the other parsers; no guess from tkhd (upstream returns
    5291400 or 220 for no-mvhd / zero-mvhd), no ERROR line."""
    dur, fle, lines = run(parts)
    assert dur == 0
    assert fle.closed
    assert _errors(lines) == []


def test_file_closed_when_parsing_raises(run):
    dur, fle, lines = run([FTYP, FREE8, _box(b'moov', TRAK), MDAT], fail_absolute_seek=True)
    assert dur == 0
    assert fle.closed
    assert len(_errors(lines)) == 1


def test_open_failure_returns_zero(monkeypatch, run):
    def _boom(*a, **k): raise IOError('cannot open')
    monkeypatch.setattr(mp4, 'FileAccess', types.SimpleNamespace(open=_boom))
    assert mp4.MP4Parser().determineLength('/movies/film.mp4') == 0


def test_mp4parser_is_python38_syntax():
    with open(mp4.__file__, encoding='utf-8') as f:
        ast.parse(f.read(), feature_version=(3, 8))
