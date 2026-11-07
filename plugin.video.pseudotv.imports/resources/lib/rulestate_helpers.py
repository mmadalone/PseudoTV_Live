#   Copyright (C) 2025 Lunatixz
#
# This file is part of PseudoTV Live.
#
# PseudoTV Live is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# PseudoTV Live is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with PseudoTV Live.  If not, see <http://www.gnu.org/licenses/>.

# -*- coding: utf-8 -*-

# madteevee (imports fork, imports.65): channel player rules follow the channel.
#
# The player values that Custom channel rules can set (Show OnNext 2, Enable
# Overlay 50, Force Subtitles 51, Trakt 52, Rollback 53, Restart Button 54,
# Show OSD on change 55) always equal the settings plus the rules of the
# Custom channel being watched:
#
#     values = effective(baseline(settings), overrides)
#
# The overrides are what that channel's PLAYER_START rules wrote at its last
# tune, recorded on a scratch RuleState (the rules never write to the player
# itself). Stop writes nothing; a hand-over to LiveTV or a library file sets
# the overrides to {}; a settings save re-publishes the same overrides over the
# new settings. Before, each rule restored a snapshot at Stop, but runActions
# re-creates the rules on every call, so the snapshot was always lost.
#
# Import-light pure module (same pattern as restart_helpers.py /
# playstate_helpers.py / onnext_helpers.py): no imports at all, no I/O, Python
# 3.8 syntax. services.Player owns the lock and the four writers
# (_applyChannelRules, _releaseChannelRules, _refreshChannelRules,
# _rollbackSwitched); this module holds the table and the arithmetic.

# (player attribute, setting id, SETTINGS reader): the values a rule can set that have a global setting.
SETTING_FIELDS = (
    ('enableOverlay',     'Overlay_Enable',     'getSettingBool'),  # rule 50 Enable Overlay
    ('infoOnChange',      'Enable_OnInfo',      'getSettingBool'),  # rule 55 Show OSD on change
    ('disableTrakt',      'Disable_Trakt',      'getSettingBool'),  # rule 52 Trakt scrobbling
    ('rollbackPlaycount', 'Rollback_Watched',   'getSettingBool'),  # rule 53 Rollback Playcount
    ('replayPercentage',  'Replay_Percentage',  'getSettingInt'),   # rule 54 Restart Button
    ('OnNextMode',        'OnNext_Mode',        'getSettingInt'),   # rule 2 Show OnNext (mode)
    ('onNextPosition',    'OnNext_Position_XY', 'getSetting'),      # rule 2 Show OnNext (position)
)

# Rule values with no global setting: their value when no channel rule sets them.
FIXED = (
    ('smartSubsMode', 'legacy'),                                    # rule 51 Force Subtitles (mode)
)

# Every value the settings refresh re-publishes.
FIELDS = tuple(attr for attr, _key, _reader in SETTING_FIELDS) + tuple(attr for attr, _value in FIXED)

# What a tune publishes: also rule 51's legacy subtitle state, whose base is Kodi's own
# subtitle state read at each tune (never refreshed from the settings).
TUNE_FIELDS = FIELDS + ('lastSubState',)


class RuleState(object):
    """A scratch stand-in for the player while a channel's PLAYER_START rules run.

    Starts at the given values and records every assignment, so overrides()
    returns exactly what the rules set. A name outside TUNE_FIELDS raises
    AttributeError (runActions logs it as an ERROR), so a new rule that writes
    a value missing from the table can't go unnoticed.
    """
    __slots__ = TUNE_FIELDS + ('_written',)

    def __init__(self, values):
        for field in TUNE_FIELDS:
            object.__setattr__(self, field, values[field])
        object.__setattr__(self, '_written', {})

    def __setattr__(self, name, value):
        if name not in TUNE_FIELDS:
            raise AttributeError('RuleState has no rule value %r' % (name,))
        object.__setattr__(self, name, value)
        self._written[name] = value

    def overrides(self):
        """{field: last value written}, a copy."""
        return dict(self._written)


def baseline(settings):
    """The values with no channel rule: each setting read with its reader, plus FIXED."""
    values = {attr: getattr(settings, reader)(key) for attr, key, reader in SETTING_FIELDS}
    values.update(FIXED)
    return values


def effective(base, overrides, fields):
    """Each field from the overrides when a rule set it, else from base."""
    return {field: overrides.get(field, base[field]) for field in fields}


def publish(values, target, fields):
    """Write the given fields onto target (the player)."""
    for field in fields:
        setattr(target, field, values[field])
