"""Unit tests for lang_codes.normalize_lang.

Verifies ISO 639-1 / 639-2 (B+T) normalization, the empty/und/null cases,
and the static-fallback path that activates when xbmc.convertLanguage
returns empty (the Kodi missing-language-pack bug)."""

import pytest

from lang_codes import normalize_lang, ISO_639_2_TO_1, LANG_CHOICES


# -------------------------------------------------------------- ISO 639-1 pass-through

@pytest.mark.parametrize("code", ['en', 'es', 'ja', 'ca', 'pt', 'zh'])
def test_iso6391_passes_through_unchanged(code):
    assert normalize_lang(code) == code


def test_iso6391_uppercase_normalized_to_lowercase():
    assert normalize_lang('EN') == 'en'
    assert normalize_lang('JA') == 'ja'


# -------------------------------------------------------------- ISO 639-2 conversion

@pytest.mark.parametrize("three,expected", [
    ('eng', 'en'),
    ('spa', 'es'),
    ('jpn', 'ja'),
    ('cat', 'ca'),
    ('fre', 'fr'),   # bibliographic
    ('fra', 'fr'),   # terminological
    ('ger', 'de'),
    ('deu', 'de'),
])
def test_iso6392_b_and_t_variants(three, expected):
    assert normalize_lang(three) == expected


def test_iso6392_uppercase_normalized():
    assert normalize_lang('ENG') == 'en'


# -------------------------------------------------------------- empties / unknowns

@pytest.mark.parametrize("code", ['', None, 'und', 'mul', 'zxx'])
def test_empty_and_undetermined_become_empty(code):
    assert normalize_lang(code) == ''


def test_unknown_three_letter_falls_through_to_empty():
    # 'xxz' is not in our static table and Kodi convertLanguage stub doesn't know it
    assert normalize_lang('xxz') == ''


def test_unsupported_length_returns_empty():
    assert normalize_lang('a') == ''        # 1-letter
    assert normalize_lang('abcd') == ''     # 4-letter
    assert normalize_lang('  ') == ''       # only whitespace


# -------------------------------------------------------------- static fallback works without xbmc

def test_static_fallback_covers_common_languages():
    # Sanity check: every language we expose in LANG_CHOICES has a fallback entry
    # for both its standard 639-2/B and 639-2/T variants when applicable. We don't
    # require both directions for all langs (Catalan, Basque etc. lack /T variants).
    iso1_codes = set(LANG_CHOICES.values()) - {''}
    fallback_values = set(ISO_639_2_TO_1.values()) - {''}
    missing = iso1_codes - fallback_values
    assert not missing, ("LANG_CHOICES exposes codes missing from ISO_639_2_TO_1 fallback table: %s"
                          % sorted(missing))


def test_lang_choices_none_entry_present():
    # The UI's "(none)" sentinel is what operators pick when they don't want a preference
    assert '(none)' in LANG_CHOICES
    assert LANG_CHOICES['(none)'] == ''
