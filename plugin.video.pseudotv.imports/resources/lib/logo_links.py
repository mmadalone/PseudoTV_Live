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

# madteevee (imports fork, imports.56): rules for the addon's OWN cached
# channel-logo links. Import-light pure module (same pattern as
# changelog_helpers / cleanup_helpers) so renderers.py, imports.py and
# server.py share one definition and the tests can exercise it without
# Kodi stubs.
#
# imports.44 publishes cached import logos as
#     http://<remote_host>:<port>/images/<url-quoted basename>
# where <remote_host>:<port> is the Remote_Host property at write time.
# Two failure modes followed from that form:
#   1. The host is baked in. When the box's address changes, every stored
#      link keeps pointing at the old address until the channel is synced
#      again — and Kodi's icon loader times out on each one.
#   2. /images/ required a login for any non-loopback client, and the local
#      Kodi fetches these links via the LAN address (not 127.0.0.1), so it
#      was refused (401) too.
# `repoint_own_logo` fixes (1) at render time; `public_logo_name` defines
# the narrow set of /images/ requests that may skip the login for (2).

import re

try:
    from urllib.parse import unquote
except ImportError:  # pragma: no cover - py2 fallback, never hit on Kodi 19+
    from urllib import unquote


# http://<host>:<port>/images/<single path segment>. No userinfo, query or
# fragment — the addon never emits those for logo links.
_OWN_LOGO_RE = re.compile(r'^http://[^/?#@\s]+:(\d+)/images/([^/?#\s]+)$', re.IGNORECASE)

# Extensions served without a login. SVG is deliberately excluded: an SVG can
# carry script, and /images/ shares an origin with the dashboard.
PUBLIC_LOGO_EXTS = ('.png', '.jpg', '.jpeg', '.webp', '.gif', '.ico')


def _is_bare_name(name):
    """A single file name: no path separators, NUL, scheme, or leading dot
    (which also rules out '.' and '..')."""
    if not name or not isinstance(name, str):
        return False
    if '/' in name or '\\' in name or '\x00' in name:
        return False
    if '://' in name or name.startswith('.'):
        return False
    return True


def port_set(remote_host='', *ports):
    """Ports that identify our own logo links: the port of the current
    Remote_Host ('host:port') plus any extra candidates (e.g. the configured
    TCP_PORT, in case a port retry moved the server). Strings, no empties."""
    out = set()
    if remote_host and ':' in remote_host:
        out.add(remote_host.rsplit(':', 1)[1])
    for p in ports:
        if p not in (None, '', 0):
            out.add(str(p))
    return out


def own_logo_basename(url, ports):
    """Return the (unquoted) file name when `url` is one of the addon's own
    cached-logo links on one of `ports`, else None. The host part is not
    checked — a stale host is exactly the case callers need to recognise."""
    if not url or not isinstance(url, str):
        return None
    m = _OWN_LOGO_RE.match(url.strip())
    if not m or m.group(1) not in (ports or ()):
        return None
    name = unquote(m.group(2))
    return name if _is_bare_name(name) else None


def repoint_own_logo(url, remote_host, ports):
    """Rewrite one of our own cached-logo links to the current `remote_host`
    ('host:port'). Every other value is returned unchanged."""
    if not remote_host or not url or not isinstance(url, str):
        return url
    m = _OWN_LOGO_RE.match(url.strip())
    if not m or m.group(1) not in (ports or ()):
        return url
    if not _is_bare_name(unquote(m.group(2))):
        return url
    return 'http://%s/images/%s' % (remote_host, m.group(2))


def public_logo_name(request_path):
    """Return the (unquoted) logo file name when an HTTP request path may be
    served without a login: exactly `/images/<bare file name>` with an image
    extension. Anything else — nested or absolute paths, `..`, Kodi VFS
    schemes, other extensions — returns None and keeps requiring a login.
    The caller must still confirm the file sits directly inside LOGO_LOC."""
    if not request_path or not isinstance(request_path, str):
        return None
    raw = request_path.split('?', 1)[0].split('#', 1)[0]
    if not raw.startswith('/images/'):
        return None
    name = unquote(raw[len('/images/'):])
    if not _is_bare_name(name):
        return None
    if not name.lower().endswith(PUBLIC_LOGO_EXTS):
        return None
    return name
