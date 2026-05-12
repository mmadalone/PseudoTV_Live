"""Regression test for the HTTP socket-release fix (imports.17).

Background: BaseServer.shutdown() stops serve_forever but does NOT release
the listening socket. Without an explicit server_close() call, every
chkPVRRefresh-triggered HTTP restart leaked the listening socket (LISTEN
state, not TIME_WAIT — so SO_REUSEADDR can't rescue the rebind). The next
_chkPort(50002) had to fall back to 50003, firing the "port in use, next
port available" toast.

This test source-scans server.py to assert that server_close() is called
in the same code block as shutdown(). Same convention as
test_chkcallback_cas.py::test_class_level_mutables_removed_from_Player
— driving the actual HTTP class from pytest is impractical (port binds,
threading, Kodi context), so we guard the invariant statically.

Plan: planned and shipped inline (imports.17, one-line fix).
"""
import os
import re


def test_http_run_calls_server_close_after_shutdown():
    """server.py:HTTP.run() must call self._server.server_close() in the
    same shutdown block as self._server.shutdown(). Without it, the listening
    socket leaks on every HTTP restart and chkPVRRefresh→toast cycle returns."""
    here = os.path.dirname(os.path.abspath(__file__))
    server_path = os.path.join(os.path.dirname(here), 'resources', 'lib', 'server.py')
    src = open(server_path).read()

    # Locate the HTTP class
    http_class_start = src.find('class HTTP(Thread):')
    assert http_class_start != -1, "HTTP class not found in server.py"
    # Find the run() method inside HTTP
    run_start = src.find('def run(self):', http_class_start)
    assert run_start != -1, "HTTP.run() not found"
    # End the search at the next top-level class/function definition (or EOF)
    # — `def run` body extends to ~line 2134 in imports.16; give a generous slice
    run_body = src[run_start:run_start + 6000]

    # Both calls must appear, and server_close must come AFTER shutdown
    shutdown_pos = run_body.find('self._server.shutdown()')
    close_pos    = run_body.find('self._server.server_close()')

    assert shutdown_pos != -1, "HTTP.run() missing self._server.shutdown() call"
    assert close_pos != -1, (
        "HTTP.run() missing self._server.server_close() call — "
        "imports.17 fix regressed. The listening socket will leak on every "
        "chkPVRRefresh→HTTP-restart cycle; next bind will port-bump 50002→50003 "
        "and fire the 'port in use' toast."
    )
    assert close_pos > shutdown_pos, (
        "self._server.server_close() must come AFTER self._server.shutdown() — "
        "shutdown() stops serve_forever; server_close() releases the listening socket."
    )

    # Sanity: both should be guarded by try/except (so a missing _server doesn't
    # crash the shutdown path). Check the line containing each is `try: ...`.
    # Slice 50 bytes before each call to inspect the prefix.
    for label, pos in (('shutdown', shutdown_pos), ('server_close', close_pos)):
        prefix = run_body[max(0, pos - 50):pos]
        assert 'try:' in prefix, (
            f"self._server.{label}() must be inside a try/except (no `try:` "
            f"in 50-byte prefix). Otherwise a missing _server raises during "
            f"the shutdown path."
        )
