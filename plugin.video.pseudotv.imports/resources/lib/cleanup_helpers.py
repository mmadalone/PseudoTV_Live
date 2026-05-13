# -*- coding: utf-8 -*-
"""Synchronous M3U + XMLTV render-and-write helper (imports.33).

`renderCleanedFiles()` instantiates fresh M3U + XMLTVS, which triggers
`cleanSelf`-on-load — the canonical cross-reference layer that drops any
channel from `pseudotv.m3u` / `pseudotv.xml` whose id is no longer in
`channels.json` OR whose `enabled` flag is False (imports.33 tightened
the predicate; see m3u.py:_verify and xmltvs.py:cleanSelf). The cleaned
in-memory state is then rendered via the module-level renderers and
written atomically.

Mirrors `tasks.chkImports` (tasks.py:709-727) — same pattern, factored
out so HTTP handlers (server.py /channels/edit.json on disable,
server.py /channels/delete.json) can reuse it without duplicating
the boilerplate.

Function-local imports keep this module cheap to load in test
contexts (tests can monkeypatch sys.modules['m3u'] / sys.modules[
'xmltvs'] / sys.modules['renderers'] BEFORE the first call). At
runtime, by the time renderCleanedFiles is reached, all three
modules are already loaded (m3u/xmltvs/renderers are pulled in
during addon startup via globals/tasks).
"""


def renderCleanedFiles():
    """Load M3U + XMLTV with cleanSelf-on-load, render, write_atomic.

    Returns (m3u_written, xml_written) tuple — both bool. False
    indicates the refuse-empty guard fired (render_m3u / render_xmltv
    returned None for empty input) and disk content was preserved.
    """
    from m3u        import M3U
    from xmltvs     import XMLTVS
    from renderers  import render_m3u, render_xmltv, write_atomic
    from globals    import M3UFLEPATH, XMLTVFLEPATH

    m3u   = M3U(writable=False)
    xmltv = XMLTVS(writable=False, m3u=m3u)

    m3u_written = False
    _m3u = render_m3u(m3u.M3UDATA)
    if _m3u is not None:
        write_atomic(M3UFLEPATH, _m3u)
        m3u_written = True

    xml_written = False
    _xml = render_xmltv(xmltv.XMLTVDATA)
    if _xml is not None:
        write_atomic(XMLTVFLEPATH, _xml)
        xml_written = True

    return m3u_written, xml_written
