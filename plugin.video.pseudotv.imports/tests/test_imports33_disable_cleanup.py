# -*- coding: utf-8 -*-
"""imports.33: regression tests for disabled / deleted Custom-channel
cleanup propagation to pseudotv.m3u + pseudotv.xml.

Two phases under test:

  * **Phase A** — cleanSelf-on-load predicate tightened from "id in
    channels.json" to "id in channels.json AND enabled=True" in both
    `m3u.py:_verify` and `xmltvs.py:cleanSelf`. Disabled Custom channels
    are now dropped from in-memory M3UDATA / XMLTVDATA at every M3U()
    / XMLTVS() instantiation.

  * **Phase B** — new `cleanup_helpers.renderCleanedFiles()` triggers
    a synchronous render + atomic write of M3U + XMLTV. Wired into
    `/channels/delete.json` (every delete) and `/channels/edit.json`
    (only when `enabled` flips to False). Mirrors the
    `tasks.chkImports` (tasks.py:709-727) render-and-write pattern.

Bug context: prior to imports.33, disabling a Custom channel via the
dashboard updated channels.json but never removed the channel's
EXTINF entry from pseudotv.m3u or its `<channel>` element +
programmes from pseudotv.xml — Builder._verify (builder.py:215-217)
skips disabled channels in the build loop with `continue`, so
`__setStation` was never called for them. The stale entries
persisted indefinitely and pvr.iptvsimple kept the channel visible
in the PVR guide. Imports (`type='import'`) were unaffected because
`Imports.syncAll` already calls `delStation` for disabled imports
at imports.py:1008. imports.33 brings Custom channels to parity.
"""
import os
import re
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
LIB  = os.path.normpath(os.path.join(HERE, '..', 'resources', 'lib'))
if LIB not in sys.path:
    sys.path.insert(0, LIB)


def _read(path):
    with open(path, 'r', encoding='utf-8') as fh:
        return fh.read()


# ============================================================
# Phase A — m3u.py:_verify enabled-filter predicate
# ============================================================

def test_m3u_verify_filters_on_enabled():
    """m3u.py:_verify must require both id-membership AND enabled=True
    when filtering stations against the channels.json allowlist.

    Without the enabled filter, disabled Custom channels survive
    cleanSelf-on-load. The predicate must include both clauses.
    """
    src = _read(os.path.join(LIB, 'm3u.py'))
    # Locate the _verify body (stations branch)
    m = re.search(
        r"def _verify\(self,\s*stations.*?if stations:.*?return stations",
        src, re.DOTALL,
    )
    assert m, "could not locate m3u.py:_verify stations branch"
    body = m.group(0)
    # Must build an enabled_ids set with enabled=True filter
    assert 'enabled_ids' in body, (
        "imports.33: m3u.py:_verify must build an `enabled_ids` set"
    )
    assert "c.get('enabled', True)" in body, (
        "imports.33: m3u.py:_verify must filter on c.get('enabled', True)"
    )
    # Must use set-membership lookup (O(N+M)) not nested comprehension (O(N*M))
    assert 'in enabled_ids' in body, (
        "imports.33: m3u.py:_verify must use set-membership lookup"
    )


def test_m3u_verify_no_random_id_fallback():
    """imports.33 dropped the legacy `station.get('id',str(random.random()))`
    fallback inside the comprehension. Random fallback would never match
    an enabled_id, so the filter result is identical with or without it,
    but stripping it removes a confusing artefact."""
    src = _read(os.path.join(LIB, 'm3u.py'))
    # The legacy nested comprehension is gone
    assert "for station in stations for channel in channels" not in src, (
        "imports.33: the O(N*M) nested comprehension must be gone from "
        "m3u.py:_verify"
    )


def test_m3u_verify_comment_mentions_imports_33():
    """Grep marker so future archaeologists can locate the rationale."""
    src = _read(os.path.join(LIB, 'm3u.py'))
    m = re.search(
        r"def _verify\(self,\s*stations.*?return stations",
        src, re.DOTALL,
    )
    assert m, "could not locate m3u.py:_verify stations branch"
    body = m.group(0)
    assert 'imports.33' in body, (
        "imports.33: m3u.py:_verify must carry the imports.33 grep marker"
    )


# ============================================================
# Phase A — xmltvs.py:cleanSelf enabled-filter predicate
# ============================================================

def test_xmltvs_cleanSelf_filters_on_enabled():
    """xmltvs.py:cleanSelf must build live_channel_ids with the
    enabled=True filter applied. Symmetric with m3u.py:_verify."""
    src = _read(os.path.join(LIB, 'xmltvs.py'))
    m = re.search(
        r"live_channel_ids\s*=\s*\{[^}]+\}",
        src,
    )
    assert m, "could not locate live_channel_ids comprehension"
    body = m.group(0)
    assert "c.get('enabled', True)" in body, (
        "imports.33: xmltvs.py:cleanSelf live_channel_ids must filter "
        "on c.get('enabled', True)"
    )


def test_xmltvs_cleanSelf_comment_mentions_imports_33():
    """Grep marker."""
    src = _read(os.path.join(LIB, 'xmltvs.py'))
    # The block above live_channel_ids carries the imports.33 explanation
    m = re.search(r"imports\.33:.*?live_channel_ids", src, re.DOTALL)
    assert m, (
        "imports.33: xmltvs.py:cleanSelf must carry the imports.33 grep "
        "marker before live_channel_ids"
    )


# ============================================================
# Phase B — cleanup_helpers.py module shape
# ============================================================

def test_cleanup_helpers_module_exists():
    """The new module file lives at resources/lib/cleanup_helpers.py."""
    assert os.path.isfile(os.path.join(LIB, 'cleanup_helpers.py')), (
        "imports.33: resources/lib/cleanup_helpers.py must exist"
    )


def test_cleanup_helpers_exports_renderCleanedFiles():
    """The function name is part of the public API (server.py imports it
    by name from two call-sites). Rename → guards fire."""
    src = _read(os.path.join(LIB, 'cleanup_helpers.py'))
    assert re.search(r"^def renderCleanedFiles\(", src, re.MULTILINE), (
        "imports.33: cleanup_helpers must define renderCleanedFiles()"
    )


def test_cleanup_helpers_uses_writable_false():
    """Mirrors tasks.chkImports lines 709-710 — writable=False so the
    instances don't try to _save() under __del__ (the imports.12 era
    cleanup; writable=True instances are reserved for Builder which
    drives its own _save lifecycle)."""
    src = _read(os.path.join(LIB, 'cleanup_helpers.py'))
    assert 'M3U(writable=False)' in src, (
        "imports.33: renderCleanedFiles must instantiate M3U(writable=False)"
    )
    assert 'XMLTVS(writable=False' in src, (
        "imports.33: renderCleanedFiles must instantiate XMLTVS(writable=False, ...)"
    )


def test_cleanup_helpers_uses_canonical_renderers():
    """render_m3u + render_xmltv + write_atomic (the imports.13 / C#10
    Follow-up A canonical renderers in renderers.py). NO bespoke
    rendering inside the helper — must reuse the existing pipeline."""
    src = _read(os.path.join(LIB, 'cleanup_helpers.py'))
    assert 'render_m3u' in src and 'render_xmltv' in src and 'write_atomic' in src, (
        "imports.33: renderCleanedFiles must use canonical renderers"
    )


def test_cleanup_helpers_uses_function_local_imports():
    """Tests must be able to monkeypatch sys.modules['m3u'] etc. BEFORE
    the first call to renderCleanedFiles without first importing
    cleanup_helpers triggering a Kodi-bearing chain. Function-local
    imports ensure this — module-top imports would resolve `m3u` at
    cleanup_helpers' import time, which happens at addon startup."""
    src = _read(os.path.join(LIB, 'cleanup_helpers.py'))
    # Split into module-level statements (before any `def`) vs function bodies.
    # Module-level lines are at column 0; function-body lines are indented.
    top_level = []
    in_function = False
    for line in src.splitlines():
        stripped = line.lstrip()
        if not line.strip() or stripped.startswith('#'):
            continue
        if not line.startswith(' ') and not line.startswith('\t'):
            # Column-0 line — either module-level code or a def/class line.
            if stripped.startswith('def ') or stripped.startswith('class '):
                in_function = True
                continue
            in_function = False
            top_level.append(stripped)
        # Indented lines belong to whatever the most recent column-0 def/class was.
    # Module-top imports of m3u / xmltvs / renderers would force eager resolution.
    forbidden_module_imports = [
        l for l in top_level
        if re.match(r"(from\s+(m3u|xmltvs|renderers)\s+import\b)|(import\s+(m3u|xmltvs|renderers)\b)", l)
    ]
    assert not forbidden_module_imports, (
        "imports.33: cleanup_helpers must NOT import m3u/xmltvs/renderers at "
        "module top (prevents test stubbing); found: %r" % forbidden_module_imports
    )
    # AND positively: the function body MUST contain function-local imports.
    func_match = re.search(r"def renderCleanedFiles\([^)]*\):(?:.|\n)*", src)
    assert func_match, "renderCleanedFiles not found"
    func_body = func_match.group(0)
    assert 'from m3u' in func_body, (
        "imports.33: renderCleanedFiles must import m3u function-locally"
    )
    assert 'from xmltvs' in func_body, (
        "imports.33: renderCleanedFiles must import xmltvs function-locally"
    )
    assert 'from renderers' in func_body, (
        "imports.33: renderCleanedFiles must import renderers function-locally"
    )


def test_cleanup_helpers_honors_refuse_empty_guard():
    """render_m3u / render_xmltv return None when input is empty
    (refuse-empty guards at renderers.py). The helper MUST check for
    None before writing — otherwise an empty render would still trigger
    write_atomic and could wipe pseudotv.m3u / produce self-closing
    <tv/>."""
    src = _read(os.path.join(LIB, 'cleanup_helpers.py'))
    # Both _m3u and _xml branches must guard via `is not None`
    m3u_branch = re.search(r"_m3u\s*=\s*render_m3u.*?write_atomic\(", src, re.DOTALL)
    xml_branch = re.search(r"_xml\s*=\s*render_xmltv.*?write_atomic\(", src, re.DOTALL)
    assert m3u_branch and 'is not None' in m3u_branch.group(0), (
        "imports.33: renderCleanedFiles must guard render_m3u with `is not None`"
    )
    assert xml_branch and 'is not None' in xml_branch.group(0), (
        "imports.33: renderCleanedFiles must guard render_xmltv with `is not None`"
    )


# ============================================================
# Phase B — cleanup_helpers.renderCleanedFiles behavioral
# ============================================================
#
# Strategy: stub sys.modules for the function-local imports so we can
# exercise renderCleanedFiles end-to-end without needing the real
# M3U/XMLTVS classes (which would drag in Kodi-stubbed addon state).
# We register fake `m3u`, `xmltvs`, `renderers`, and a `globals` patch
# BEFORE the function-local imports run.

class _FakeM3U:
    def __init__(self, writable=False):
        self.writable = writable
        self.M3UDATA = {'stations': [{'id': 'A'}], 'recordings': []}

class _FakeXMLTVS:
    def __init__(self, writable=False, m3u=None):
        self.writable = writable
        self.m3u = m3u
        self.XMLTVDATA = {'channels': [{'id': 'A'}], 'programmes': []}

class _RenderCallTracker:
    def __init__(self, m3u_out=b'M3U', xml_out=b'<xml/>', writes_log=None):
        self.m3u_out    = m3u_out
        self.xml_out    = xml_out
        self.writes_log = writes_log if writes_log is not None else []
    def render_m3u(self, m3udata):
        return self.m3u_out
    def render_xmltv(self, xmltvdata):
        return self.xml_out
    def write_atomic(self, path, content, channel_count=None):
        # imports.42: write_atomic gained an optional `channel_count` kwarg
        # for size-circuit-breaker + LOGINFO size logging. Accept it here
        # (and record it alongside the path/content) so cleanup_helpers'
        # post-imports.42 invocation through this tracker doesn't TypeError.
        self.writes_log.append((path, content, channel_count))


def _install_fakes(monkeypatch, tracker, fake_m3u_cls=_FakeM3U, fake_xmltvs_cls=_FakeXMLTVS):
    fake_m3u_mod = types.ModuleType('m3u')
    fake_m3u_mod.M3U = fake_m3u_cls
    monkeypatch.setitem(sys.modules, 'm3u', fake_m3u_mod)

    fake_xmltvs_mod = types.ModuleType('xmltvs')
    fake_xmltvs_mod.XMLTVS = fake_xmltvs_cls
    monkeypatch.setitem(sys.modules, 'xmltvs', fake_xmltvs_mod)

    fake_renderers = types.ModuleType('renderers')
    fake_renderers.render_m3u   = tracker.render_m3u
    fake_renderers.render_xmltv = tracker.render_xmltv
    fake_renderers.write_atomic = tracker.write_atomic
    monkeypatch.setitem(sys.modules, 'renderers', fake_renderers)

    # cleanup_helpers does `from globals import M3UFLEPATH, XMLTVFLEPATH`.
    # Install fake constants without disturbing other modules' globals access.
    # (cleanup_helpers' import is function-local, so it ONLY queries `globals`
    # for these names at call time; a partial fake is sufficient.)
    fake_globals = types.ModuleType('globals')
    fake_globals.M3UFLEPATH   = '/fake/pseudotv.m3u'
    fake_globals.XMLTVFLEPATH = '/fake/pseudotv.xml'
    monkeypatch.setitem(sys.modules, 'globals', fake_globals)


def test_renderCleanedFiles_writes_both_files(monkeypatch):
    """Happy path: render_m3u + render_xmltv both return content;
    write_atomic fires for both files at the right paths."""
    tracker = _RenderCallTracker()
    _install_fakes(monkeypatch, tracker)

    # Import AFTER the fakes are in place — cleanup_helpers is light.
    if 'cleanup_helpers' in sys.modules:
        del sys.modules['cleanup_helpers']
    import cleanup_helpers

    m3u_written, xml_written = cleanup_helpers.renderCleanedFiles()
    assert m3u_written is True
    assert xml_written is True
    paths = [entry[0] for entry in tracker.writes_log]   # imports.42: writes_log tuples are 3-element (path, content, channel_count)
    assert '/fake/pseudotv.m3u' in paths
    assert '/fake/pseudotv.xml' in paths


def test_renderCleanedFiles_skips_write_on_empty_m3u(monkeypatch):
    """render_m3u returns None when M3UDATA is empty (refuse-empty
    guard at renderers.py). write_atomic for the M3U must NOT fire;
    XML still writes (independent guard)."""
    tracker = _RenderCallTracker(m3u_out=None, xml_out=b'<xml/>')
    _install_fakes(monkeypatch, tracker)

    if 'cleanup_helpers' in sys.modules:
        del sys.modules['cleanup_helpers']
    import cleanup_helpers

    m3u_written, xml_written = cleanup_helpers.renderCleanedFiles()
    assert m3u_written is False
    assert xml_written is True
    paths = [entry[0] for entry in tracker.writes_log]   # imports.42: writes_log tuples are 3-element (path, content, channel_count)
    assert '/fake/pseudotv.m3u' not in paths
    assert '/fake/pseudotv.xml' in paths


def test_renderCleanedFiles_skips_write_on_empty_xml(monkeypatch):
    """Symmetric: render_xmltv returns None → no XML write; M3U still
    writes."""
    tracker = _RenderCallTracker(m3u_out=b'M3U', xml_out=None)
    _install_fakes(monkeypatch, tracker)

    if 'cleanup_helpers' in sys.modules:
        del sys.modules['cleanup_helpers']
    import cleanup_helpers

    m3u_written, xml_written = cleanup_helpers.renderCleanedFiles()
    assert m3u_written is True
    assert xml_written is False
    paths = [entry[0] for entry in tracker.writes_log]   # imports.42: writes_log tuples are 3-element (path, content, channel_count)
    assert '/fake/pseudotv.m3u' in paths
    assert '/fake/pseudotv.xml' not in paths


def test_renderCleanedFiles_skips_both_on_empty(monkeypatch):
    """Both renderers refuse empty: no writes fire. Disk content
    preserved (the operator-protective edge case where they disable
    their last enabled channel — we'd rather leave the stale M3U
    intact than wipe PVR EPG)."""
    tracker = _RenderCallTracker(m3u_out=None, xml_out=None)
    _install_fakes(monkeypatch, tracker)

    if 'cleanup_helpers' in sys.modules:
        del sys.modules['cleanup_helpers']
    import cleanup_helpers

    m3u_written, xml_written = cleanup_helpers.renderCleanedFiles()
    assert m3u_written is False
    assert xml_written is False
    assert tracker.writes_log == []


def test_renderCleanedFiles_instantiates_writable_false(monkeypatch):
    """The fake M3U/XMLTVS classes must be called with writable=False
    so __del__ doesn't try to _save under GC. Verify via constructor
    spy."""
    seen = {'m3u_kw': None, 'xmltvs_kw': None}

    class SpyM3U:
        def __init__(self, writable=False):
            seen['m3u_kw'] = {'writable': writable}
            self.M3UDATA = {'stations': [], 'recordings': []}

    class SpyXMLTVS:
        def __init__(self, writable=False, m3u=None):
            seen['xmltvs_kw'] = {'writable': writable, 'm3u': m3u}
            self.XMLTVDATA = {'channels': [], 'programmes': []}

    tracker = _RenderCallTracker()
    _install_fakes(monkeypatch, tracker, SpyM3U, SpyXMLTVS)
    if 'cleanup_helpers' in sys.modules:
        del sys.modules['cleanup_helpers']
    import cleanup_helpers
    cleanup_helpers.renderCleanedFiles()
    assert seen['m3u_kw']    == {'writable': False}
    assert seen['xmltvs_kw'] is not None
    assert seen['xmltvs_kw']['writable'] is False
    # XMLTVS is constructed with the M3U instance threaded through (matches
    # the chkImports pattern + Builder pattern — XMLTVS uses m3u for some
    # cross-reference internally).
    assert seen['xmltvs_kw']['m3u'] is not None


# ============================================================
# Phase B — server.py wiring (source-scan)
# ============================================================

def test_server_delete_calls_renderCleanedFiles():
    """`/channels/delete.json` must call renderCleanedFiles after the
    channels.json setChannels write so the deleted channel disappears
    from M3U/XML immediately (closes the eventually-consistent latency
    window that the pre-imports.33 code had)."""
    src = _read(os.path.join(LIB, 'server.py'))
    m = re.search(
        r"/channels/delete\.json.*?wfile\.write\(body\)",
        src, re.DOTALL,
    )
    assert m, "could not locate /channels/delete.json handler"
    body = m.group(0)
    assert 'renderCleanedFiles' in body, (
        "imports.33: /channels/delete.json must call renderCleanedFiles "
        "after setChannels"
    )
    assert 'from cleanup_helpers import renderCleanedFiles' in body, (
        "imports.33: /channels/delete.json must import the helper "
        "function-locally"
    )


def test_server_edit_calls_renderCleanedFiles_on_disable():
    """`/channels/edit.json` must call renderCleanedFiles when the edit
    flips `enabled` to False. Metadata-only edits (number/name/logo/
    group/catchup/favorite) and enable-True / path / rule changes
    flow through the existing Builder pipelines and MUST NOT trigger
    a synchronous render."""
    src = _read(os.path.join(LIB, 'server.py'))
    m = re.search(
        r"/channels/edit\.json.*?wfile\.write\(body\)",
        src, re.DOTALL,
    )
    assert m, "could not locate /channels/edit.json handler"
    body = m.group(0)
    assert 'renderCleanedFiles' in body, (
        "imports.33: /channels/edit.json must call renderCleanedFiles "
        "on disable"
    )
    # Guard: must check 'enabled' in fields AND `not fields['enabled']`
    assert re.search(
        r"'enabled'\s+in\s+fields\s+and\s+not\s+fields\['enabled'\]",
        body,
    ), (
        "imports.33: /channels/edit.json must guard renderCleanedFiles "
        "with `'enabled' in fields and not fields['enabled']` so it ONLY "
        "fires on disable transitions"
    )


def test_server_renderCleanedFiles_wrapped_in_try_except():
    """A render failure must NOT 500 the HTTP request — channels.json
    is already saved by the time we call renderCleanedFiles, so a
    write-side failure should log + continue, not invalidate the
    operator's edit."""
    src = _read(os.path.join(LIB, 'server.py'))
    # Both call-sites should be inside their own try/except so an
    # exception is logged + swallowed.
    for endpoint in ('/channels/delete.json', '/channels/edit.json'):
        m = re.search(
            re.escape(endpoint) + r".*?wfile\.write\(body\)",
            src, re.DOTALL,
        )
        assert m, "could not locate handler for %s" % endpoint
        # Locate the renderCleanedFiles call and check that it's preceded
        # by `try:` within the same handler.
        body = m.group(0)
        call_pos = body.find('renderCleanedFiles()')
        assert call_pos > 0, "%s missing renderCleanedFiles call" % endpoint
        # Walk backward for `try:` — must be present within a small window
        window = body[max(0, call_pos - 400):call_pos]
        assert 'try:' in window, (
            "imports.33: %s renderCleanedFiles call must be wrapped in "
            "try/except (a render failure can't 500 the response)" % endpoint
        )


def test_server_edit_renderCleanedFiles_NOT_called_on_metadata_only():
    """Defensive: the renderCleanedFiles call inside /channels/edit.json
    must be INSIDE the `if 'enabled' in fields and not fields['enabled']:`
    block, not at the top level of the handler. Otherwise every edit
    (renumber, rename, logo-pick) would synchronously re-render M3U/XML
    — wasteful and would compete with Builder's metadata fast-path
    (imports.30)."""
    src = _read(os.path.join(LIB, 'server.py'))
    m = re.search(
        r"/channels/edit\.json.*?wfile\.write\(body\)",
        src, re.DOTALL,
    )
    assert m, "could not locate /channels/edit.json handler"
    body = m.group(0)
    # The guard block must appear before the renderCleanedFiles call
    guard_pos = body.find("'enabled' in fields and not fields['enabled']")
    call_pos  = body.find('renderCleanedFiles()')
    assert guard_pos > 0, "imports.33: disable guard missing"
    assert call_pos > 0, "imports.33: renderCleanedFiles call missing"
    assert guard_pos < call_pos, (
        "imports.33: the disable guard must appear BEFORE renderCleanedFiles "
        "in /channels/edit.json (must be inside the guarded block, not "
        "above it)"
    )


# ============================================================
# Backward-compat / regression guards
# ============================================================

def test_changelog_has_imports_33_entry():
    """changelog.txt must include the imports.33 entry — durable
    assertion across future cycle bumps. The addon.xml version-string
    check that previously lived here (`version="0.8.0+imports.33"`)
    was a cycle-specific footgun — it broke when imports.34 shipped.
    The changelog entry is the durable record."""
    src = _read(os.path.join(LIB, '..', '..', 'changelog.txt'))
    assert 'v.0.8.0+imports.33' in src, (
        "imports.33: changelog.txt must include the imports.33 entry"
    )


def test_builder_verify_still_skips_disabled():
    """imports.33 does NOT change Builder._verify's skip-disabled
    behavior — Phase A handles cleanup at the cleanSelf-on-load
    layer; Builder's contract stays "build the enabled ones."
    Symmetric with how it worked before, just with M3U/XML now
    correctly reflecting the filter."""
    src = _read(os.path.join(LIB, 'builder.py'))
    assert "not citem.get('enabled',True)" in src, (
        "imports.33 must preserve Builder._verify's disabled-skip "
        "(not citem.get('enabled',True))"
    )
    assert 'SKIPPING - disabled channel' in src, (
        "imports.33 must preserve Builder._verify's `SKIPPING - disabled "
        "channel` log line"
    )


def test_imports_syncAll_still_calls_delStation_for_disabled():
    """imports.33 does NOT change Imports.syncAll's per-disabled-import
    delStation cleanup at imports.py:1008. The import pipeline already
    cleaned disabled imports correctly; imports.33 brings Custom
    channels to parity but does not touch the import-side path."""
    src = _read(os.path.join(LIB, 'imports.py'))
    # The delStation call inside the disabled-channel branch.
    assert re.search(
        r"if ch\.get\('is_orphan'\) or ch\.get\('unallocated'\) or not ch\.get\('enabled', True\):.*?self\.m3u\.delStation",
        src, re.DOTALL,
    ), (
        "imports.33: Imports.syncAll's per-disabled-import delStation "
        "cleanup must be preserved"
    )


def test_imports_syncAll_still_filters_enabled_ids_on_xml():
    """Same regression guard for the XML branch. imports.py:1051's
    enabled_ids set must still filter on enabled=True."""
    src = _read(os.path.join(LIB, 'imports.py'))
    assert re.search(
        r"enabled_ids\s*=\s*\{.*?ch\.get\('enabled',\s*True\)",
        src, re.DOTALL,
    ), (
        "imports.33: Imports.syncAll's XML-side enabled_ids filter must "
        "be preserved"
    )
