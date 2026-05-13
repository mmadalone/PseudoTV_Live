# -*- coding: utf-8 -*-
"""Dependency-free helpers for `tasks._filterChannelsNeedingBuild`
(imports.29).

Pure module — no Kodi imports, no addon imports, no I/O. Lives separately
from tasks.py so unit tests can import it without dragging the whole
addon stack (manager.py → xbmcgui.ControlList unstubbed).
"""

# Fields the operator can edit that end up rendered into pseudotv.m3u.
# Used by `tasks._filterChannelsNeedingBuild` to detect when channels.json
# has drifted from the on-disk M3U (e.g., operator renumbered a Custom
# channel via a code path that didn't set the `changed=True` rebuild
# flag). Mirrors M3U.getStationItem output (m3u.py:585+). Deliberately
# excludes:
#   - 'path' / 'rules': not rendered to M3U; 'path' is source-CRC
#     territory.
#   - 'enabled': Builder._verify (builder.py:215-217) drops disabled
#     channels before processing, so a disabled-vs-enabled comparison
#     would either no-op (Builder skips) or infinitely re-queue.
#     Disabled channels are short-circuited BEFORE the drift check in
#     tasks.py.
#   - 'label': derived from 'name' inside addStation, so name-drift
#     transitively covers it.
RENDER_KEYS = ('number', 'name', 'logo', 'group', 'catchup', 'radio', 'favorite')


# imports.30: fields whose edit triggers a metadata-only Builder
# fast-path (sub-second M3U + XMLTV channel-element re-render, NO
# programme re-fetch). Compare to RENDER_KEYS above — the two are
# siblings but NOT identical:
#   - 'radio' is in RENDER_KEYS (drift detection must catch a radio
#     flip and re-render the channel) but NOT in META_ONLY_FIELDS:
#     xmltv.getProgramItem (xmltvs.py:459+) reads citem['radio']
#     during programme rendering, so a radio flip must trigger
#     full rebuild (programme-shape differs between video and radio).
#   - Edits to fields OUTSIDE this set (path, rules, enabled, etc.)
#     trigger a full rebuild via the `changed=True` flag — that's the
#     conservative-safe default. Adding a new field here without
#     verifying it ISN'T read inside getProgramItem / buildVideo /
#     buildMusic is a real correctness risk.
META_ONLY_FIELDS = frozenset({'number', 'name', 'logo', 'group', 'catchup', 'favorite'})


def _renderStateDrift(citem, sitem):
    """Return True when channels.json data differs from the M3U entry on
    any operator-visible rendered field. sitem=None means the channel is
    not in M3U yet (newly added) — always counts as drift."""
    if sitem is None:
        return True
    for k in RENDER_KEYS:
        c_val = citem.get(k)
        s_val = sitem.get(k)
        if k == 'group':
            # M3U._load dedupes/sorts via set (m3u.py:168), so the M3U side
            # has no meaningful order. Compare as sorted sets so a citem
            # group like ['Movies','Drama'] doesn't perpetually drift
            # against an M3U group like ['Drama','Movies'].
            c_val = sorted(set(c_val or []))
            s_val = sorted(set(s_val or []))
        if c_val != s_val:
            return True
    return False
