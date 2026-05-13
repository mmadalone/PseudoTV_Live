"""Regression test for the HTTP-restart cascade fix (imports.22).

Background: operators saw a `madteevee: Online` toast every ~5 min during
movie playback. Full cascade traced:

  chkImports (5-min daemon)
    → Imports.syncAll() (deferred-on-playback gate doesn't fire when
      Run_While_Playing=true)
    → imports.py:1005 setPropTimer('chkPVRRefresh')   [unconditional]
    → _chkPropTimer queues chkPVRRefresh
    → tasks.py:835 timerit(setEXTProperty)(M3U_REFRESH,
        '<ADDON_ID>.HTTP.pendingRestart', True)
    → server.py HTTP.run loop reads pendingRestart=True → break →
      chkHTTP +15s → fresh HTTP() instance
    → __update(pendingRestart=False) → silent=False →
      DIALOG.notificationDialog("...: Online")

Two-part fix:
  (A) tasks.py: drop the timerit re-arm line — the ONLY setter of
      <ADDON_ID>.HTTP.pendingRestart in the codebase. pvr.iptvsimple
      polls its m3uUrl on its own m3uRefreshIntervalMins cadence so the
      restart is vestigial; MyHandler in server.py reads disk on every
      GET so no in-process cache is invalidated; PVRScan is documented
      no-op against iptvsimple.
  (B) imports.py: gate the chkPVRRefresh signal in syncAll on actual
      disk-state change (status='ok' OR deleted_ids OR orphans).

Source-scan style mirrors test_http_server_close.py + test_chkcallback_cas.py
— driving the actual syncAll from pytest is impractical (HTTP fixtures,
channels.json, M3U/XMLTV, writer-lock stack), so we guard the invariants
statically.

Plan: /home/madalone/.claude/plans/dig-into-c-do-typed-kettle.md
"""
import os
import re


HERE = os.path.dirname(os.path.abspath(__file__))
LIB  = os.path.join(os.path.dirname(HERE), 'resources', 'lib')


def _read(name):
    with open(os.path.join(LIB, name)) as f:
        return f.read()


def _slice(src, start_anchor, end_anchor):
    """Return the substring between two anchors. Fails if either is absent."""
    s = src.find(start_anchor)
    assert s != -1, "anchor not found: %r" % start_anchor
    e = src.find(end_anchor, s + len(start_anchor))
    assert e != -1, "anchor not found after %r: %r" % (start_anchor, end_anchor)
    return src[s:e]


def test_chkPVRRefresh_does_not_set_HTTP_pendingRestart():
    """tasks.py chkPVRRefresh must NOT contain any setter of the
    '<ADDON_ID>.HTTP.pendingRestart' property. That setter was the trigger
    of the operator-visible 'madteevee: Online' toast cascade every ~5 min
    during playback. Removed in imports.22; this test guards against future
    regression."""
    src = _read('tasks.py')
    body = _slice(src, 'def chkPVRRefresh', 'def chkSettingsChange')

    # Allow the substring to appear inside comments (the imports.22 comment
    # block references it explanatorily). Strip comment-only lines first.
    code_lines = [ln for ln in body.splitlines() if not ln.lstrip().startswith('#')]
    code_only  = '\n'.join(code_lines)

    # The forbidden pattern is the literal setter — any timerit call that
    # passes 'HTTP.pendingRestart' and True. Match liberally to catch
    # cosmetic variations.
    forbidden = re.compile(
        r"setEXTProperty[^\n]*HTTP\.pendingRestart[^\n]*True",
        re.IGNORECASE,
    )
    match = forbidden.search(code_only)
    assert match is None, (
        "tasks.py:chkPVRRefresh contains a setter of HTTP.pendingRestart: %r\n"
        "imports.22 dropped this line because it caused the 'madteevee: Online' "
        "toast cascade every ~5 min during playback. pvr.iptvsimple polls its "
        "m3uUrl on its own m3uRefreshIntervalMins cadence; the restart is "
        "vestigial. If you genuinely need to bounce the HTTP server, queue "
        "tasks.chkHTTP directly via service._que instead." % match.group(0)
    )


def test_syncAll_chkPVRRefresh_signal_is_conditional():
    """imports.py:syncAll must guard `setPropTimer('chkPVRRefresh')` on a
    `changed` boolean that references the three change-signals:
    status=='ok', deleted_ids, and orphans. imports.22 added this gate so
    all-304 cycles (every import returned HTTP 304, no operator deletions,
    no orphans) don't fire the downstream PVR-refresh cascade."""
    src = _read('imports.py')

    # syncAll's tail is the bit after the M3U force-flush try/except. Anchor
    # on the unique log message just above the historical signal site.
    body = _slice(src, 'def syncAll', '# Summary log')

    # The conditional must exist: a `changed = (...)` binding that references
    # 'ok', deleted_ids, and 'orphans'.
    changed_binding = re.search(
        r"changed\s*=\s*\(",
        body,
    )
    assert changed_binding is not None, (
        "imports.py:syncAll missing `changed = (...)` binding before the "
        "chkPVRRefresh signal — imports.22 conditional gate regressed."
    )

    # Slice from the `changed = (` to the next `)` at column 0 of a logical
    # close. Match the three referenced signals.
    tail = body[changed_binding.start():]
    for needle, why in (
        ("'ok'",        "status='ok' check (fresh HTTP 200 fetched)"),
        ("deleted_ids", "tombstone-processed check"),
        ("'orphans'",   "reconcile-orphan check"),
    ):
        assert needle in tail[:1500], (
            "imports.py:syncAll `changed` expression missing %s — %s."
            % (needle, why)
        )

    # The setPropTimer call must be reachable only via `if changed:`.
    # Anchor: the call site is now wrapped by an `if changed:` branch with
    # an `else:` that logs the no-op-cycle message.
    assert "if changed:" in tail, (
        "imports.py:syncAll missing `if changed:` guard around "
        "setPropTimer('chkPVRRefresh')."
    )
    assert "no disk change this cycle" in tail, (
        "imports.py:syncAll missing the `no disk change this cycle` "
        "else-branch log message that confirms the gate fired."
    )


def test_setPropTimer_chkPVRRefresh_callers_outside_syncAll_unchanged():
    """Non-syncAll callers of setPropTimer('chkPVRRefresh') must keep
    firing unconditionally — they fire from paths that always mutate state
    (Builder completion, recording added, operator edits, multiroom config
    change, iptvsimple settings change, etc.). imports.22 is scoped to the
    syncAll site only; this test guards against accidental over-application
    of the conditional pattern to those callers."""
    expected = {
        'builder.py'        : "setPropTimer('chkPVRRefresh')",
        'context_record.py' : "setPropTimer('chkPVRRefresh')",
        'manager.py'        : "setPropTimer('chkPVRRefresh')",
        'multiroom.py'      : "setPropTimer('chkPVRRefresh')",
        'kodi.py'           : "setPropTimer('chkPVRRefresh')",
    }
    for fname, needle in expected.items():
        src = _read(fname)
        assert needle in src, (
            "%s lost its setPropTimer('chkPVRRefresh') call — imports.22 "
            "should NOT have changed this file. Genuine state-change "
            "callers must keep signaling unconditionally." % fname
        )
