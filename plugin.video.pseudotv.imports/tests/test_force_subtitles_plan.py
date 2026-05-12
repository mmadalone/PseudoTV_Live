"""Unit tests for ForceSubtitles._planForItem (build-time smart_subs plan generation).

Each test seeds optionValues directly on a real ForceSubtitles instance and
calls _planForItem with a faux fileList item carrying streamdetails. We assert
the produced plan's sub_state / sub_lang_target / audio_lang_target / rationale.

The rule itself only depends on lang_codes; we don't exercise runAction here
because that path needs globals/jsonrpc/channels machinery beyond the unit-test
boundary. The integration is validated live on the Pi5 instead."""

import pytest


# Loading rules.py pulls in globals -> jsonrpc -> channels and a LOT of
# Kodi machinery. We import lazily inside a fixture and skip if anything
# in the import chain raises (e.g. xbmc stubs missing a method we don't
# care about). This keeps the test fast and self-contained.
@pytest.fixture(scope='module')
def rule_cls():
    try:
        from rules import ForceSubtitles
    except Exception as e:                  # pragma: no cover — env-specific
        pytest.skip("ForceSubtitles import failed (likely missing Kodi stub method): %s" % e)
    return ForceSubtitles


@pytest.fixture
def rule(rule_cls):
    r = rule_cls()
    # Defaults from __init__: option idx 0 = legacy bool, 1 = mode, 2..6 = smart options
    # Force into Smart mode for these tests.
    r.optionValues[1] = 'smart'
    return r


# -------------------------------------------------------------- helpers

def _item(audio_langs=None, sub_langs=None):
    """Build a faux fileList item with the streamdetails shape Kodi produces."""
    audio_langs = audio_langs or []
    sub_langs   = sub_langs   or []
    return {
        'file': '/fake/path.mkv',
        'streamdetails': {
            'audio':    [{'language': l, 'codec': 'ac3', 'channels': 2} for l in audio_langs],
            'subtitle': [{'language': l}                                 for l in sub_langs],
            'video':    [{'codec': 'h264', 'aspect': '16:9'}],
        },
    }


# -------------------------------------------------------------- empty / missing metadata

def test_returns_none_when_no_audio_streams(rule):
    assert rule._planForItem({'file': '/fake', 'streamdetails': {}}) is None
    assert rule._planForItem({'file': '/fake'}) is None
    assert rule._planForItem({'file': '/fake', 'streamdetails': {'audio': []}}) is None


# -------------------------------------------------------------- native vs foreign

def test_native_audio_disables_subs(rule):
    rule.optionValues[2] = 'en'             # native
    plan = rule._planForItem(_item(audio_langs=['eng']))
    assert plan['sub_state'] is False
    assert plan['sub_lang_target'] == ''
    assert plan['rationale'] == 'native_audio'


def test_foreign_audio_enables_subs_with_pref_match(rule):
    rule.optionValues[2] = 'en'             # native
    rule.optionValues[3] = 'en'             # preferred sub
    plan = rule._planForItem(_item(audio_langs=['jpn'], sub_langs=['eng', 'spa']))
    assert plan['sub_state'] is True
    assert plan['sub_lang_target'] == 'en'
    assert 'foreign_audio' in plan['rationale']
    assert 'pref_sub_match' in plan['rationale']


def test_foreign_audio_with_native_unset_still_enables(rule):
    # If operator hasn't set native, every audio is "foreign" → subs ON
    rule.optionValues[2] = ''               # no native
    rule.optionValues[3] = 'en'
    plan = rule._planForItem(_item(audio_langs=['eng'], sub_langs=['eng']))
    assert plan['sub_state'] is True


# -------------------------------------------------------------- fallback behaviors

def test_fallback_first_picks_first_available_sub(rule):
    rule.optionValues[2] = 'en'
    rule.optionValues[3] = 'fr'             # prefer French
    rule.optionValues[5] = 'first'
    plan = rule._planForItem(_item(audio_langs=['jpn'], sub_langs=['eng', 'spa']))   # no French
    assert plan['sub_state'] is True
    assert plan['sub_lang_target'] == 'en'                # falls back to first detected
    assert 'fallback_first' in plan['rationale']


def test_fallback_off_disables_subs_when_no_match(rule):
    rule.optionValues[2] = 'en'
    rule.optionValues[3] = 'fr'
    rule.optionValues[5] = 'off'
    plan = rule._planForItem(_item(audio_langs=['jpn'], sub_langs=['eng', 'spa']))
    assert plan['sub_state'] is False
    assert 'fallback_off' in plan['rationale']


def test_fallback_kodi_default_leaves_target_empty(rule):
    rule.optionValues[2] = 'en'
    rule.optionValues[3] = 'fr'
    rule.optionValues[5] = 'default'
    plan = rule._planForItem(_item(audio_langs=['jpn'], sub_langs=['eng', 'spa']))
    assert plan['sub_state'] is True
    assert plan['sub_lang_target'] == ''                  # Kodi will pick on its own
    assert 'fallback_kodi_default' in plan['rationale']


# -------------------------------------------------------------- audio switch

def test_audio_switch_only_when_pref_exists_in_file(rule):
    rule.optionValues[2] = 'en'
    rule.optionValues[4] = 'ja'             # prefer Japanese audio
    plan = rule._planForItem(_item(audio_langs=['eng', 'jpn'], sub_langs=['eng']))
    assert plan['audio_lang_target'] == 'ja'              # switch will happen
    # After switching to Japanese, audio != native English → subs ON
    assert plan['sub_state'] is True


def test_audio_switch_skipped_when_pref_absent(rule):
    rule.optionValues[2] = 'en'
    rule.optionValues[4] = 'ja'
    # No Japanese audio on the file — switch impossible
    plan = rule._planForItem(_item(audio_langs=['eng', 'spa'], sub_langs=['eng']))
    assert plan['audio_lang_target'] == ''                # no switch
    # First audio is English (native) → subs OFF
    assert plan['sub_state'] is False


def test_audio_switch_to_native_keeps_subs_off(rule):
    rule.optionValues[2] = 'en'             # native = English
    rule.optionValues[4] = 'en'             # operator prefers English audio
    plan = rule._planForItem(_item(audio_langs=['jpn', 'eng']))
    assert plan['audio_lang_target'] == 'en'
    assert plan['sub_state'] is False


# -------------------------------------------------------------- unknown-audio policy

def test_unknown_audio_empty_lang_treated_as_unknown(rule):
    # Audio stream exists but its language tag is empty → effective_audio == ''
    # → triggers unknown-audio policy branch (foreign = subs ON)
    rule.optionValues[2] = 'en'
    rule.optionValues[3] = 'en'
    rule.optionValues[6] = 'foreign'
    item = _item(audio_langs=[''], sub_langs=['eng'])
    plan = rule._planForItem(item)
    assert plan is not None
    assert plan['sub_state'] is True
    assert 'unknown_audio_foreign' in plan['rationale']


def test_unknown_audio_uses_und_language(rule):
    rule.optionValues[2] = 'en'
    rule.optionValues[3] = 'en'
    rule.optionValues[6] = 'foreign'
    # Audio with 'und' (undetermined) is normalized away → no audio_langs detected
    plan = rule._planForItem({
        'file': '/fake', 'streamdetails': {
            'audio': [{'language': 'und', 'codec': 'ac3', 'channels': 2}],
            'subtitle': [{'language': 'eng'}],
        }
    })
    # 'und' is filtered out so audio_streams was non-empty but no usable langs:
    # effective_audio == '' → unknown_audio_foreign path → subs ON
    assert plan is not None
    assert plan['sub_state'] is True
    assert 'unknown_audio_foreign' in plan['rationale']


def test_unknown_audio_native_disables_subs(rule):
    rule.optionValues[2] = 'en'
    rule.optionValues[6] = 'native'
    plan = rule._planForItem({
        'file': '/fake', 'streamdetails': {
            'audio': [{'language': 'und'}],
        }
    })
    assert plan is not None
    assert plan['sub_state'] is False
    assert 'unknown_audio_native' in plan['rationale']


def test_unknown_audio_legacy_uses_option_0_bool(rule):
    rule.optionValues[0] = True             # legacy says subs ON
    rule.optionValues[6] = 'legacy'
    plan = rule._planForItem({
        'file': '/fake', 'streamdetails': {
            'audio': [{'language': 'und'}],
        }
    })
    assert plan is not None
    assert plan['sub_state'] is True
    assert 'unknown_audio_legacy' in plan['rationale']


# -------------------------------------------------------------- label formatting

def test_format_label_basic(rule):
    plan = {'audio_lang_target': '', 'sub_state': True, 'sub_lang_target': 'en'}
    assert rule._formatLabel(plan) == 'Subs EN'


def test_format_label_with_audio_switch(rule):
    plan = {'audio_lang_target': 'ja', 'sub_state': True, 'sub_lang_target': 'en'}
    assert rule._formatLabel(plan) == 'Audio JA / Subs EN'


def test_format_label_subs_off(rule):
    plan = {'audio_lang_target': '', 'sub_state': False, 'sub_lang_target': ''}
    assert rule._formatLabel(plan) == 'Subs OFF'


def test_format_label_subs_on_no_lang_target(rule):
    plan = {'audio_lang_target': '', 'sub_state': True, 'sub_lang_target': ''}
    assert rule._formatLabel(plan) == 'Subs ON'


# -------------------------------------------------------------- mode helper

def test_mode_value_clamps_unknown_to_legacy(rule):
    rule.optionValues[1] = 'invalid_mode'
    assert rule._modeValue() == 'legacy'


def test_mode_value_accepts_known_modes(rule):
    for m in ('legacy', 'always_on', 'always_off', 'smart'):
        rule.optionValues[1] = m
        assert rule._modeValue() == m
