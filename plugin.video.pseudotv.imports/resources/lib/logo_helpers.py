# -*- coding: utf-8 -*-
"""Operator-uploaded / operator-picked logo file management (imports.36).

Closes the Web-UI ↔ Kodi-UI asymmetry: both paths now copy the operator's
chosen logo into LOGO_LOC for resilience (independence from removable
media unmount, source-file deletion, network share dropping). Single
source of truth for the copy semantics — `manager.py:switchLogo.__browse`
and `server.py:/channels/edit.json` both call `copyToLogoLoc`. The new
`server.py:/channels/logo/upload.json` endpoint uses `writeUploadedLogo`
to receive base64-encoded bytes from the dashboard's local-file upload
flow.

Function-local imports (fileaccess / globals) keep this module cheap to
load in tests — tests can monkeypatch `sys.modules['fileaccess']` and
`sys.modules['globals']` BEFORE the first call without dragging Kodi
stubs. Mirrors the imports.29 filter_helpers / imports.32 sysinfo_helpers
/ imports.33 cleanup_helpers precedent.
"""
import os
import re


# Extensions accepted by the upload endpoint AND used as the safe-default
# extension for copyToLogoLoc. Anything outside this set falls back to
# `.png` (operator can always rename / re-pick if they want a niche
# format). Lowercase + leading dot for direct comparison with
# `os.path.splitext(...)[1].lower()`.
ALLOWED_LOGO_EXTENSIONS = frozenset({
    '.png', '.jpg', '.jpeg', '.webp', '.gif', '.svg', '.ico', '.bmp',
})

# Hard cap enforced server-side in writeUploadedLogo. Client-side JS
# enforces the same limit so the browser doesn't waste bandwidth on a
# doomed upload. 5 MB is comfortably above any realistic logo size
# (typical PNG/JPG channel logos are 5-200 KB).
MAX_LOGO_UPLOAD_BYTES = 5 * 1024 * 1024

# Filesystem-unsafe characters to scrub from chname before using it as a
# basename. Covers Windows + POSIX. The control-char range (\x00-\x1f)
# closes path-traversal vectors via embedded NULs / newlines. Replaced
# with `_` rather than stripped so the result still reflects the
# original chname's shape.
UNSAFE_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitizeChnameForFilename(chname):
    """Replace filesystem-unsafe chars in a channel name so it's safe
    as a basename.

    Returns the sanitized string (capped at 128 chars), or '' for any
    non-string / empty / all-whitespace / all-unsafe input. Callers
    treat '' as "skip the copy/upload — no valid filename can be built."
    """
    if not chname or not isinstance(chname, str):
        return ''
    safe = UNSAFE_FILENAME_CHARS.sub('_', chname).strip().rstrip('.')
    return safe[:128]


def extForFilename(filename, fallback='.png'):
    """Extract a safe extension (including the leading dot) from a
    filename. Returns the lowercased extension if it's in
    ALLOWED_LOGO_EXTENSIONS, else `fallback` (default `.png`).

    Used by both copyToLogoLoc (extension derived from source path) and
    writeUploadedLogo (extension derived from client-provided filename).
    """
    if not filename or not isinstance(filename, str):
        return fallback
    _, ext = os.path.splitext(filename.lower())
    if ext in ALLOWED_LOGO_EXTENSIONS:
        return ext
    return fallback


def copyToLogoLoc(source_path, chname, log_fn=None):
    """Copy a logo file from `source_path` INTO LOGO_LOC for resilience.

    Returns:
        New path string (`special://...`) inside LOGO_LOC on successful
        copy. Original `source_path` on skip (URL/resource://, already
        inside LOGO_LOC, source missing, empty chname, copy failure).

    Skip rules:
      - `source_path` starts with `http://`, `https://`, `resource://`,
        or `image://` → not a real file; pass through.
      - `source_path` already resolves to a path inside LOGO_LOC →
        no-op (idempotent re-save).
      - `source_path` doesn't exist → can't copy; preserve original
        (better than wiping the operator's choice on a transient
        unmount).
      - `sanitizeChnameForFilename(chname)` is empty → can't derive a
        safe basename; preserve original.
      - `FileAccess.copy` returns False or raises → log + preserve
        original. The operator sees their original choice on next
        save attempt.

    Mirrors `manager.py:switchLogo.__browse` semantics (copies into
    LOGO_LOC using `<sanitized_chname><ext>` naming). The web-side
    `/channels/edit.json` auto-call (server.py) and the Kodi-side
    switchLogo both go through this helper → same end state regardless
    of UI.
    """
    from fileaccess import FileAccess
    from globals    import LOGO_LOC, ADDON_ID

    if log_fn is None:
        log_fn = lambda *a, **k: None

    if not source_path or not isinstance(source_path, str):
        return source_path

    # URLs / resource paths aren't files to copy.
    if source_path.startswith(('http://', 'https://', 'resource://', 'image://')):
        return source_path

    # Already inside LOGO_LOC? Idempotent skip.
    try:
        abs_loc = FileAccess.translatePath(LOGO_LOC)
        abs_src = (FileAccess.translatePath(source_path)
                   if source_path.startswith('special://') else source_path)
        if abs_src.startswith(abs_loc):
            return source_path
    except Exception:
        # Path translation failed; fall through and let the file-existence
        # check below handle it.
        pass

    if not FileAccess.exists(source_path):
        log_fn('copyToLogoLoc, source missing: %s' % source_path)
        return source_path

    safe = sanitizeChnameForFilename(chname)
    if not safe:
        log_fn('copyToLogoLoc, sanitized chname empty for %r' % (chname,))
        return source_path

    ext = extForFilename(source_path)
    try:
        dest_dir = FileAccess.translatePath(LOGO_LOC)
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, safe + ext)
        if not FileAccess.copy(source_path, dest):
            log_fn('copyToLogoLoc, FileAccess.copy returned False: %s → %s'
                   % (source_path, dest))
            return source_path
    except Exception as e:
        log_fn('copyToLogoLoc, copy failed: %s' % e)
        return source_path

    return 'special://profile/addon_data/%s/cache/logos/%s%s' % (
        ADDON_ID, safe, ext,
    )


def writeUploadedLogo(file_bytes, chname, filename, log_fn=None):
    """Write base64-decoded operator-uploaded bytes into LOGO_LOC.

    Args:
        file_bytes : decoded bytes from the upload endpoint.
        chname     : channel name (becomes the basename).
        filename   : client-provided filename (only used for the
                     extension; basename is derived from chname).

    Returns:
        New path string (`special://...`) on success, or None on
        failure (size cap exceeded, empty sanitized chname, write
        error). The endpoint maps None → 500.

    Size cap + extension allow-list enforced HERE (writeUploadedLogo
    is the gatekeeper — the endpoint just shovels bytes). The cap is
    defense in depth against a client that bypasses the 5 MB JS check.
    """
    from fileaccess import FileAccess
    from globals    import LOGO_LOC, ADDON_ID

    if log_fn is None:
        log_fn = lambda *a, **k: None

    if not isinstance(file_bytes, (bytes, bytearray)):
        log_fn('writeUploadedLogo, non-bytes input rejected')
        return None
    if len(file_bytes) > MAX_LOGO_UPLOAD_BYTES:
        log_fn('writeUploadedLogo, rejected (size %d > limit %d)'
               % (len(file_bytes), MAX_LOGO_UPLOAD_BYTES))
        return None
    if len(file_bytes) == 0:
        log_fn('writeUploadedLogo, rejected empty upload')
        return None

    safe = sanitizeChnameForFilename(chname)
    if not safe:
        log_fn('writeUploadedLogo, sanitized chname empty for %r' % (chname,))
        return None

    ext = extForFilename(filename)
    try:
        dest_dir = FileAccess.translatePath(LOGO_LOC)
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, safe + ext)
        with open(dest, 'wb') as f:
            f.write(file_bytes)
    except Exception as e:
        log_fn('writeUploadedLogo, write failed: %s' % e)
        return None

    return 'special://profile/addon_data/%s/cache/logos/%s%s' % (
        ADDON_ID, safe, ext,
    )
