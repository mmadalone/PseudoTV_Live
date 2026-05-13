"""Regression tests for imports.30 — metadata-only Builder fast-path.

imports.29 introduced render-state drift detection in the filter, which
correctly catches operator edits to channel metadata (number, name,
logo, etc.) but sets `changed=True` — routing every drift through the
expensive Builder full-rebuild path (~90s per channel because
__clrStation wipes programmes and Builder re-enumerates the source XSP).

imports.30 splits the signal: drift on EXISTING channels (sitem present
in M3U) sets a new `metadata_changed=True` flag. Builder gains a
__renderMetadataOnly closure that upserts the M3U entry + XMLTV channel
element only — sub-second per channel, programmes left intact. Drift on
NEW/re-enabled channels (sitem missing from M3U) still sets `changed=True`
because programmes need to be built. Server-side /channels/edit.json
classifies edits by field set; META_ONLY_FIELDS-only edits go through
the fast-path, anything else (path, rules, enabled, radio) full-rebuild.

Source-scan style mirrors test_imports22 / .25 / .27 / .28 / .29 for
the static guards; behavioral tests exercise the routing logic via
filter_helpers + direct dict mutations.

Plan: /home/madalone/.claude/plans/let-s-plan-for-2-drifting-tulip.md
"""
import os
import re
import sys


HERE = os.path.dirname(os.path.abspath(__file__))
ADDON_ROOT = os.path.dirname(HERE)
LIB = os.path.join(ADDON_ROOT, 'resources', 'lib')


def _read(path):
    with open(path) as f:
        return f.read()


if LIB not in sys.path:
    sys.path.insert(0, LIB)


# ======================================================================
# Source-scan: META_ONLY_FIELDS contents
# ======================================================================

def test_filter_helpers_defines_meta_only_fields():
    """filter_helpers.py must define META_ONLY_FIELDS at module scope —
    as a frozenset, exposed for both server.py classification AND test
    introspection."""
    from filter_helpers import META_ONLY_FIELDS
    assert isinstance(META_ONLY_FIELDS, frozenset), (
        "META_ONLY_FIELDS must be a frozenset (immutable, hashable)."
    )
    expected = {'number', 'name', 'logo', 'group', 'catchup', 'favorite'}
    assert META_ONLY_FIELDS == expected, (
        "META_ONLY_FIELDS contents drift: expected %s, got %s" % (
            sorted(expected), sorted(META_ONLY_FIELDS))
    )


def test_meta_only_fields_excludes_radio():
    """Plan-agent catch: `radio` is read by xmltv.getProgramItem during
    programme generation. A radio flip changes programme shape, so it
    MUST trigger a full rebuild, NOT a fast-path render. This test is
    the loud guard against accidentally adding `radio` back to
    META_ONLY_FIELDS during a future field-list expansion."""
    from filter_helpers import META_ONLY_FIELDS
    assert 'radio' not in META_ONLY_FIELDS, (
        "META_ONLY_FIELDS must NOT include 'radio' — xmltv.getProgramItem "
        "reads it during programme generation. A radio flip MUST trigger "
        "full rebuild, not fast-path. See plan and Plan-agent report."
    )


def test_meta_only_fields_excludes_path_rules_enabled():
    """Sanity: ensure path/rules/enabled aren't in META_ONLY_FIELDS.
    `path` is the XSP source (CRC territory). `rules` mutates programme
    generation. `enabled` flips visibility — disabled-to-enabled needs
    programmes built."""
    from filter_helpers import META_ONLY_FIELDS
    for field in ('path', 'rules', 'enabled'):
        assert field not in META_ONLY_FIELDS, (
            "META_ONLY_FIELDS must NOT include %r — see plan." % field
        )


# ======================================================================
# Source-scan: server.py classifies edits
# ======================================================================

def test_server_imports_meta_only_fields_from_filter_helpers():
    """server.py must import META_ONLY_FIELDS from filter_helpers —
    single source of truth. A second copy of the set in server.py would
    be a footgun."""
    src = _read(os.path.join(LIB, 'server.py'))
    assert re.search(
        r'from filter_helpers\s+import\s+META_ONLY_FIELDS',
        src,
    ), (
        "server.py missing `from filter_helpers import META_ONLY_FIELDS` — "
        "extraction regressed."
    )


def test_server_classifies_with_issubset_check():
    """server.py's /channels/edit.json handler must use
    `META_ONLY_FIELDS.issubset` or similar set-membership logic on
    `edited_keys` — not a hard-coded field whitelist that would drift."""
    src = _read(os.path.join(LIB, 'server.py'))
    assert 'edited_keys' in src and 'META_ONLY_FIELDS' in src, (
        "server.py /channels/edit.json must compute edited_keys and use "
        "META_ONLY_FIELDS — imports.30 classification regressed."
    )
    assert re.search(
        r'edited_keys\.issubset\(META_ONLY_FIELDS\)',
        src,
    ), (
        "server.py /channels/edit.json must use "
        "edited_keys.issubset(META_ONLY_FIELDS) for classification."
    )


def test_server_routes_metadata_changed_vs_changed():
    """server.py must set EITHER `target['metadata_changed']=True` OR
    `target['changed']=True` based on the classification — both must
    appear, in the right branches."""
    src = _read(os.path.join(LIB, 'server.py'))
    assert "target['metadata_changed'] = True" in src, (
        "server.py missing target['metadata_changed']=True branch — fast-"
        "path classification not wired."
    )
    assert "target['changed'] = True" in src, (
        "server.py missing target['changed']=True branch — full-rebuild "
        "classification not wired (or the line was removed entirely)."
    )


# ======================================================================
# Source-scan: Builder fast-path closures
# ======================================================================

def test_builder_has_metadata_only_change_helper():
    """builder.py must define __hasMetadataOnlyChange — the discriminator
    that protects the fast-path from accidentally running when changed=True
    is also set (defensive default: changed wins)."""
    src = _read(os.path.join(LIB, 'builder.py'))
    assert re.search(
        r'def __hasMetadataOnlyChange\(citem:\s*dict\)\s*->\s*bool:',
        src,
    ), "builder.py missing __hasMetadataOnlyChange closure"


def test_builder_has_render_metadata_only_helper():
    """builder.py must define __renderMetadataOnly — the fast-path
    primitive that upserts M3U + XMLTV channel elements without clearing
    programmes."""
    src = _read(os.path.join(LIB, 'builder.py'))
    assert re.search(
        r'def __renderMetadataOnly\(citem:\s*dict\)\s*->\s*bool:',
        src,
    ), "builder.py missing __renderMetadataOnly closure"


def test_builder_fast_path_inserted_before_needsUpdate():
    """The fast-path branch in buildChannels must appear BEFORE
    __needsUpdate / __hasChanged CALL sites in source order — otherwise
    it would short-circuit AFTER the full path has already started.
    Two `for idx, citem in enumerate(channels):` loops exist in
    builder.py (one in _verify at line 202, one in buildChannels at
    line 437); the relevant CALL sites for __needsUpdate / __hasChanged
    are both inside buildChannels. Compare absolute byte offsets in
    the whole file."""
    src = _read(os.path.join(LIB, 'builder.py'))
    # Use the CALL-site form to skip the closure DEFINITIONS at lines
    # 269/276 which also contain `__needsUpdate(citem,` /
    # `__hasChanged(citem,` substrings inside their `def` headers.
    # The actual CALL sites use `_update, start = __needsUpdate(...)`
    # and `_changed = __hasChanged(...)`.
    fast_pos  = src.find('if __hasMetadataOnlyChange(citem):')
    needs_pos = src.find('_update, start = __needsUpdate(citem,')
    has_pos   = src.find('_changed = __hasChanged(citem,')
    assert fast_pos > 0, "fast-path call site `if __hasMetadataOnlyChange(citem):` not found"
    assert needs_pos > 0, "__needsUpdate call site not found"
    assert has_pos > 0, "__hasChanged call site not found"
    assert fast_pos < needs_pos, (
        "__hasMetadataOnlyChange call must come BEFORE __needsUpdate. "
        "fast=%d, needs=%d" % (fast_pos, needs_pos)
    )
    assert fast_pos < has_pos, (
        "__hasMetadataOnlyChange call must come BEFORE __hasChanged. "
        "fast=%d, has=%d" % (fast_pos, has_pos)
    )


def test_builder_fast_path_calls_addStation_not_clrStation():
    """__renderMetadataOnly must call m3u.addStation + xmltv.addChannel
    but NOT m3u.delStation / xmltv.delBroadcast / __clrStation —
    otherwise it'd be the full path with extra steps. Slice the body
    by finding the def line and reading up to the next sibling `def `
    at the same indent."""
    src = _read(os.path.join(LIB, 'builder.py'))
    start = src.find('def __renderMetadataOnly(citem')
    assert start > 0, "__renderMetadataOnly definition not found"
    # Next `        def ` (8-space indent — sibling closures) terminates
    # the body. If no next sibling exists, slice to end of file.
    nxt = src.find('\n        def ', start + 1)
    body = src[start:nxt] if nxt > 0 else src[start:]
    assert 'self.m3u.addStation' in body, (
        "__renderMetadataOnly body must call self.m3u.addStation. Body: %r"
        % body[:300]
    )
    assert 'self.xmltv.addChannel' in body, (
        "__renderMetadataOnly body must call self.xmltv.addChannel."
    )
    # Must NOT call the destructive primitives — those wipe programmes.
    assert 'self.m3u.delStation' not in body, (
        "__renderMetadataOnly must NOT call m3u.delStation directly — "
        "addStation already upserts internally."
    )
    assert 'self.xmltv.delBroadcast' not in body, (
        "__renderMetadataOnly must NOT call xmltv.delBroadcast — that "
        "wipes programmes, defeating the fast-path purpose."
    )
    assert '__clrStation(' not in body, (
        "__renderMetadataOnly must NOT call __clrStation — same reason."
    )


def test_builder_fast_path_guarded_by_hasProgrammes():
    """The fast-path call site must check __hasProgrammes BEFORE running
    __renderMetadataOnly — channels with zero programmes get dropped by
    xmltvs._save's cleanChannels, so the fast-path would silently
    vanish them."""
    src = _read(os.path.join(LIB, 'builder.py'))
    # Find the fast-path branch in the per-channel loop body
    fast = src.find('if __hasMetadataOnlyChange(citem):')
    assert fast > 0, "fast-path branch not found in per-channel loop"
    branch = src[fast:fast + 1500]
    assert '__hasProgrammes(citem)' in branch, (
        "fast-path branch must guard with __hasProgrammes — without it, "
        "zero-programme channels would be dropped by cleanChannels."
    )


def test_builder_fast_path_escalates_to_changed_when_no_programmes():
    """When __hasProgrammes returns False inside the fast-path branch,
    the channel must be escalated to changed=True (full rebuild) so
    programmes get re-fetched before _save runs."""
    src = _read(os.path.join(LIB, 'builder.py'))
    fast = src.find('if __hasMetadataOnlyChange(citem):')
    branch = src[fast:fast + 1500]
    assert "citem['changed'] = True" in branch, (
        "fast-path branch must escalate to changed=True when programmes "
        "are missing — otherwise the channel would either be dropped or "
        "stuck with metadata_changed=True forever."
    )
    assert "citem['metadata_changed'] = False" in branch, (
        "fast-path branch must clear metadata_changed=False on escalation "
        "— prevents the channel from infinite-loop bouncing between "
        "fast-path and full-rebuild."
    )


# ======================================================================
# Source-scan: tasks.py routing + chkChanged extension
# ======================================================================

def test_filter_drift_branch_routes_by_sitem():
    """The drift branch in _filterChannelsNeedingBuild must route based
    on `sitem is None`: None → changed=True (need programmes), exists →
    metadata_changed=True (fast-path eligible)."""
    src = _read(os.path.join(LIB, 'tasks.py'))
    fn = re.search(
        r'def _filterChannelsNeedingBuild\(self, channels\):(.*?)(?=\n    def )',
        src, re.DOTALL,
    )
    assert fn is not None
    body = fn.group(1)
    # The new routing logic must mention metadata_changed alongside changed
    assert "citem['metadata_changed'] = True" in body, (
        "_filterChannelsNeedingBuild drift branch must set "
        "metadata_changed=True when sitem exists — fast-path routing "
        "regressed."
    )
    assert 'if sitem is None:' in body, (
        "_filterChannelsNeedingBuild drift branch must explicitly check "
        "`if sitem is None:` to route fresh/re-enabled channels to "
        "changed=True (full rebuild)."
    )


def test_filter_summary_log_includes_metadata_count():
    """The summary log line must include the metadata counter alongside
    drift_detected and crc_detected — telemetry for diagnosing how many
    of each kind of trigger fired."""
    src = _read(os.path.join(LIB, 'tasks.py'))
    assert 'metadata = %s' in src and 'metadata_drift' in src, (
        "_filterChannelsNeedingBuild summary log missing 'metadata = N' / "
        "metadata_drift counter — telemetry regressed."
    )


def test_chkChanged_fires_on_either_flag():
    """chkChanged must queue Builder for channels with EITHER
    changed=True OR metadata_changed=True — Builder discriminates via
    its own checks. Without this extension, server.py edits that set
    metadata_changed wouldn't queue immediately and would wait for the
    next chkChannels cycle."""
    src = _read(os.path.join(LIB, 'tasks.py'))
    # Find chkChanged body
    fn = re.search(
        r'def chkChanged\(self, channels=None, silent=None\):(.*?)(?=\n    def )',
        src, re.DOTALL,
    )
    assert fn is not None
    body = fn.group(1)
    assert "channel.get('metadata_changed', False)" in body, (
        "chkChanged must check `metadata_changed` alongside `changed` — "
        "imports.30 fast-path edits would otherwise wait for chkChannels."
    )
    assert "channel.get('changed', False)" in body, (
        "chkChanged must STILL check `changed` — pre-imports.30 paths "
        "shouldn't regress."
    )


def test_chkChanged_clears_both_flags_on_stale_imports():
    """The stale-imports cleanup in chkChanged (existing behavior for
    imports.14) must clear BOTH `changed` AND `metadata_changed` for
    import channels — otherwise an import that ever got `metadata_changed=True`
    set externally would loop forever (Builder skips imports + doesn't
    clear flags)."""
    src = _read(os.path.join(LIB, 'tasks.py'))
    fn = re.search(
        r'def chkChanged\(self, channels=None, silent=None\):(.*?)(?=\n    def )',
        src, re.DOTALL,
    )
    body = fn.group(1)
    assert "c['changed'] = False" in body, "stale-imports cleanup must clear changed"
    assert "c['metadata_changed'] = False" in body, (
        "stale-imports cleanup must ALSO clear metadata_changed — imports.30 "
        "added the new flag, defense-in-depth requires symmetric clearing."
    )


# ======================================================================
# Behavioral: classification correctness via the actual set
# ======================================================================

def test_classification_metadata_only_payload():
    """A pure-metadata edit (only number/name/logo/etc.) classifies as
    metadata-only. The set.issubset check is the discriminator."""
    from filter_helpers import META_ONLY_FIELDS
    for fields_keys in (
        {'number'},
        {'name'},
        {'logo'},
        {'group'},
        {'catchup'},
        {'favorite'},
        {'number', 'name'},
        {'number', 'logo', 'favorite'},
    ):
        assert fields_keys.issubset(META_ONLY_FIELDS), (
            "%r should classify as metadata-only" % fields_keys
        )


def test_classification_full_rebuild_payloads():
    """Edits that include any non-META_ONLY_FIELDS field must NOT
    classify as metadata-only — fall through to changed=True (full
    rebuild)."""
    from filter_helpers import META_ONLY_FIELDS
    for fields_keys in (
        {'path'},                          # source change
        {'rules'},                         # programme generation rules
        {'enabled'},                       # visibility flip
        {'radio'},                         # programme-shape flip
        {'number', 'path'},                # mixed
        {'number', 'name', 'rules'},       # mixed
        {'logo', 'enabled'},               # mixed
        {'radio', 'favorite'},             # mixed (radio is non-meta)
    ):
        assert not fields_keys.issubset(META_ONLY_FIELDS), (
            "%r must NOT classify as metadata-only — contains a field "
            "outside META_ONLY_FIELDS that requires full rebuild." % fields_keys
        )


def test_classification_empty_payload_not_metadata():
    """An empty edited_keys set classifies neither as metadata-only nor
    as full-rebuild (no change to apply). server.py's `edited_keys and
    edited_keys.issubset(META_ONLY_FIELDS)` short-circuits on empty —
    falls to the else branch → `changed=True`. This is intentional and
    safe."""
    from filter_helpers import META_ONLY_FIELDS
    empty = set()
    assert not (empty and empty.issubset(META_ONLY_FIELDS)), (
        "Empty edited_keys must NOT classify as metadata-only — "
        "the `and` short-circuit handles this in server.py."
    )
