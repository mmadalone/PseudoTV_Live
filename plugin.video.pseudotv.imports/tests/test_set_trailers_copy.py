"""imports.51 — setTrailers mutation fix.

Pre-imports.51, `Builder.setTrailers` did `item.update({...})` to construct
trailer data, mutating the CALLER'S movie dict in place — file URL swapped to
the trailer URL, duration shrunk to the trailer duration, label suffixed
with ' - Trailer'. The caller at `builder.py:931-936` then appended this
(now-mutated) item to `fileList`, so the channel content chain silently
substituted the 2-3 min trailer for the 60-180 min movie. Channels lost any
movie whose Kodi-library entry had a `trailer` field with a fetchable
duration.

Live-observed on FFOD: 4 Kurosawa films (Throne of Blood, Yojimbo, Sanjuro,
Samurai Rebellion) appeared ONLY as their trailers — the full-movie versions
did not exist anywhere in the channel's 184 programmes.

The fix builds a fresh `trailer_item = dict(item)` shallow copy, mutates the
copy, stores the copy in trailerCache. Original `item` left untouched.
"""
import os
import re


def _setTrailers_mirror(jsonRPC_getDuration, trailerCache, item, trailer_suffix=' - Trailer'):
    """Mirror of post-imports.51 Builder.setTrailers."""
    dur = jsonRPC_getDuration(item.get('trailer'), True, False)
    if dur > 0:
        trailer_item = dict(item)
        trailer_item.update({
            'label':        '%s%s' % (item.get('label', ''),        trailer_suffix),
            'episodetitle': '%s%s' % (item.get('episodetitle', ''), trailer_suffix),
            'episodelabel': '%s%s' % (item.get('episodelabel', ''), trailer_suffix),
            'duration': dur, 'runtime': dur,
            'file': item.get('trailer'),
            'streamdetails': {},
        })
        for genre in (trailer_item.get('genre', []) or ['resources']):
            trailerCache.setdefault(genre.lower(), []).append(trailer_item)


def _make_movie(trailer_url='http://example.com/throne_trailer.mp4'):
    return {
        'label': 'Throne of Blood',
        'episodetitle': 'Throne of Blood',
        'episodelabel': '',
        'duration': 6300,
        'runtime': 6300,
        'file': 'smb://NAS/Movies/Throne_of_Blood.mkv',
        'trailer': trailer_url,
        'genre': ['Samurai'],
        'streamdetails': {'video': [{'duration': 6300}]},
    }


# ──────────────────────────────────────────────────────────────────── behaviour


def test_original_movie_dict_unmutated_after_setTrailers():
    """Smoking-gun regression guard. Pre-imports.51, this exact assertion
    failed — the movie dict came out the other side with file=trailer URL
    and duration=trailer duration."""
    movie = _make_movie()
    cache = {}
    _setTrailers_mirror(lambda *a, **k: 225, cache, movie)
    assert movie['label'] == 'Throne of Blood', 'movie label mutated'
    assert movie['duration'] == 6300,          'movie duration mutated'
    assert movie['runtime']  == 6300,          'movie runtime mutated'
    assert movie['file']     == 'smb://NAS/Movies/Throne_of_Blood.mkv', 'movie file mutated'
    assert movie['streamdetails'] != {},       'movie streamdetails cleared'


def test_trailer_cache_entry_is_a_separate_dict_object():
    """The cached entry must NOT alias the original movie dict — otherwise
    any downstream mutation by the caller would corrupt the cache, and vice
    versa."""
    movie = _make_movie()
    cache = {}
    _setTrailers_mirror(lambda *a, **k: 225, cache, movie)
    cached = cache['samurai'][0]
    assert cached is not movie, 'trailerCache entry is the same dict as the movie'


def test_trailer_cache_entry_has_trailer_data():
    """The cached entry must reflect the TRAILER's file/duration/label so the
    filler pool (fillers.py:54 getTrailers merge) gets trailer content."""
    movie = _make_movie()
    cache = {}
    _setTrailers_mirror(lambda *a, **k: 225, cache, movie)
    cached = cache['samurai'][0]
    assert cached['file']     == movie['trailer']
    assert cached['duration'] == 225
    assert cached['runtime']  == 225
    assert ' - Trailer' in cached['label']
    assert cached['streamdetails'] == {}


def test_no_cache_write_when_trailer_duration_zero():
    """When ffprobe returns 0 for the trailer URL, setTrailers must skip — no
    cache write AND no mutation of the original (already-correct pre-fix)."""
    movie = _make_movie()
    cache = {}
    _setTrailers_mirror(lambda *a, **k: 0, cache, movie)
    assert cache == {}
    assert movie['label'] == 'Throne of Blood'  # unmutated even pre-fix


def test_genre_fallback_to_resources_when_movie_has_no_genre():
    """Empty/missing genre falls back to 'resources' bucket — matches the
    `(item.get('genre',[]) or ['resources'])` logic."""
    movie = _make_movie()
    movie['genre'] = []
    cache = {}
    _setTrailers_mirror(lambda *a, **k: 225, cache, movie)
    assert 'resources' in cache
    assert 'samurai' not in cache


def test_multi_genre_movie_cached_under_each_lowercased_genre():
    """A multi-genre movie gets a cache entry under each genre key,
    lowercased. The SAME trailer_item appears in multiple buckets — this
    matches the existing behaviour intentionally (filler pool can find the
    same trailer via any of its genres)."""
    movie = _make_movie()
    movie['genre'] = ['Samurai', 'Action', 'Period Drama']
    cache = {}
    _setTrailers_mirror(lambda *a, **k: 225, cache, movie)
    assert 'samurai'       in cache
    assert 'action'        in cache
    assert 'period drama'  in cache
    # Same trailer_item object referenced from each bucket (preserved behaviour)
    assert cache['samurai'][0] is cache['action'][0]


# ──────────────────────────────────────────────────────────────── source-scan


def test_source_scan_setTrailers_copies_item_before_mutating():
    """imports.51 drift guard. The actual builder.py setTrailers function must
    build a copy of item BEFORE the .update call. Catches regression to
    in-place mutation."""
    here = os.path.dirname(os.path.abspath(__file__))
    src  = open(os.path.join(os.path.dirname(here), 'resources', 'lib', 'builder.py')).read()
    m = re.search(r'def setTrailers\(self, item\):(.*?)(?=\n    def |\nclass )', src, re.DOTALL)
    assert m, 'setTrailers function not found in builder.py'
    body = m.group(1)
    # Permissive: dict(item) OR copy.copy(item) OR item.copy() — any copy primitive
    assert re.search(r'dict\(\s*item\s*\)|copy\.copy\(\s*item\s*\)|item\.copy\(\)', body), (
        'imports.51 regression: setTrailers must copy item before mutating'
    )
    # The .update() and .append() should both target the copy, not `item` directly
    assert 'trailer_item.update(' in body or 'trailer_item[' in body, (
        'imports.51 regression: setTrailers must mutate the COPY (trailer_item), not item'
    )
    # Belt-and-braces: ensure no `item.update(` remains in the function CODE.
    # Comments stripped (rationale comment quotes the old code). Word-boundary
    # regex required because the SUBSTRING 'item.update(' is contained in the
    # new code's `trailer_item.update(` calls.
    code_only = '\n'.join(line for line in body.split('\n') if not line.lstrip().startswith('#'))
    assert not re.search(r'\bitem\.update\(', code_only), (
        'imports.51 regression: setTrailers still calls item.update() — should be trailer_item.update()'
    )
