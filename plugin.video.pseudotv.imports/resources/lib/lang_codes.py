#   Copyright (C) 2025 Lunatixz
#
#
# This file is part of PseudoTV Live (madteevee imports fork).
#
# Language-code normalization for the Smart Subtitle/Audio rule (ForceSubtitles, myId=51).
#
# Kodi mixes ISO 639-1 (2-letter) and ISO 639-2 (3-letter) codes depending on source:
#   - VideoLibrary streamdetails (.audio[].language, .subtitle[].language)        → usually 3-letter (eng, spa, jpn)
#   - JSON-RPC Player.GetProperties (audiostreams/subtitles[].language)            → usually 2-letter (en, es, ja)
#   - XMLTV / external M3U feeds                                                   → 2-letter
#
# We normalize everything to ISO 639-1 for comparison. xbmc.convertLanguage()
# is the official Kodi helper but has a known empty-string return bug when
# language packs are not installed (https://forum.kodi.tv/showthread.php?tid=372626).
# We try it first, then fall back to a static table covering the languages most
# operators actually configure.

import xbmc


# Static ISO 639-2 → ISO 639-1 mapping. Includes both /B (bibliographic) and /T
# (terminological) variants where they differ (fre/fra, ger/deu, etc.). Used as
# fallback when xbmc.convertLanguage returns empty.
ISO_639_2_TO_1 = {
    'eng': 'en',                          # English
    'spa': 'es',                          # Spanish
    'cat': 'ca',                          # Catalan
    'fre': 'fr', 'fra': 'fr',             # French (B/T)
    'ger': 'de', 'deu': 'de',             # German (B/T)
    'ita': 'it',                          # Italian
    'por': 'pt',                          # Portuguese
    'jpn': 'ja',                          # Japanese
    'kor': 'ko',                          # Korean
    'chi': 'zh', 'zho': 'zh',             # Chinese (B/T)
    'rus': 'ru',                          # Russian
    'ara': 'ar',                          # Arabic
    'nld': 'nl', 'dut': 'nl',             # Dutch (B/T)
    'swe': 'sv',                          # Swedish
    'nor': 'no',                          # Norwegian
    'fin': 'fi',                          # Finnish
    'dan': 'da',                          # Danish
    'pol': 'pl',                          # Polish
    'tur': 'tr',                          # Turkish
    'heb': 'he',                          # Hebrew
    'tha': 'th',                          # Thai
    'gle': 'ga',                          # Irish
    'glg': 'gl',                          # Galician
    'eus': 'eu', 'baq': 'eu',             # Basque (B/T)
    'und': '',                            # Undetermined
    'mul': '',                            # Multiple — treat as undetermined
    'zxx': '',                            # No linguistic content
}


# Display label → ISO 639-1 code. Used by ForceSubtitles for dropdown options.
# Order matters (Python 3.7+ preserves dict insertion order); shown in UI in this order.
LANG_CHOICES = {
    '(none)':            '',
    'English':           'en',
    'Spanish':           'es',
    'Catalan':           'ca',
    'French':            'fr',
    'German':            'de',
    'Italian':           'it',
    'Portuguese':        'pt',
    'Japanese':          'ja',
    'Korean':            'ko',
    'Chinese':           'zh',
    'Russian':           'ru',
    'Arabic':            'ar',
    'Dutch':             'nl',
    'Swedish':           'sv',
    'Norwegian':         'no',
    'Finnish':           'fi',
    'Polish':            'pl',
    'Turkish':           'tr',
}


def normalize_lang(code):
    """
    Accept any-case ISO 639-1 or ISO 639-2 (B or T variant) code and return
    lowercase ISO 639-1. Empty / unknown / 'und' / None → ''.

    Tries xbmc.convertLanguage first (Kodi's blessed converter, supports any
    locale Kodi has installed); falls back to static table when convertLanguage
    returns empty (known bug when language packs are missing).
    """
    if not code: return ''
    code = str(code).lower().strip()
    if not code or code in ('und', 'mul', 'zxx'): return ''
    if len(code) == 2: return code        # already ISO 639-1
    if len(code) == 3:
        try:
            v = xbmc.convertLanguage(code, xbmc.ISO_639_1)
            if v:
                v = v.lower().strip()
                if v and v != code: return v   # successful conversion (sometimes returns same code on failure)
        except Exception:
            pass
        return ISO_639_2_TO_1.get(code, '')
    return ''                              # unrecognized format
