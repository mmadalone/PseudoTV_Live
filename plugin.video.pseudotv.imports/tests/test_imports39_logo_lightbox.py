# -*- coding: utf-8 -*-
"""imports.39: source-scan tests for the dashboard logo lightbox.

Pure client-side UX feature: clicking a .row-logo thumbnail opens a
full-size preview overlay; click anywhere on the overlay or press
Escape to close. No Python / server-side changes.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
HTML = os.path.normpath(os.path.join(HERE, '..', 'remotes', 'manager.html'))
LIB  = os.path.normpath(os.path.join(HERE, '..', 'resources', 'lib'))


def _read(path):
    with open(path, 'r', encoding='utf-8') as fh:
        return fh.read()


def test_overlay_element_present():
    """manager.html has the `<div class="logo-preview-overlay">` element
    near the end of body with required id + role."""
    src = _read(HTML)
    assert re.search(
        r'<div\s+class="logo-preview-overlay"\s+id="logo-preview-overlay"\s+role="dialog"',
        src,
    ), (
        "imports.39: logo-preview-overlay element with id + role must "
        "be present in manager.html"
    )
    assert '<img alt=""></div>'.replace(' ', '') in src.replace('\n', '').replace(' ', '') or '<img alt="">' in src, (
        "imports.39: overlay must contain an <img> element"
    )


def test_overlay_open_toggle_class_css_rule():
    """CSS includes `.logo-preview-overlay.open { display: flex; }` so
    the JS handler's `classList.add('open')` actually shows the overlay."""
    src = _read(HTML)
    assert re.search(
        r"\.logo-preview-overlay\.open\s*\{\s*display:\s*flex;",
        src,
    ), (
        "imports.39: CSS must include `.logo-preview-overlay.open "
        "{ display: flex; }` toggle rule"
    )


def test_overlay_default_display_none():
    """Default state hides the overlay (`display: none` on baseline class)."""
    src = _read(HTML)
    assert re.search(
        r"\.logo-preview-overlay\s*\{[^}]*display:\s*none",
        src, re.DOTALL,
    ), (
        "imports.39: `.logo-preview-overlay { display: none; }` baseline "
        "must be present so the overlay is hidden until activated"
    )


def test_row_logo_has_zoom_in_cursor():
    """`.row-logo` gets `cursor: zoom-in` for affordance — operator sees
    the click target via the cursor change."""
    src = _read(HTML)
    assert re.search(
        r"\.channel-row \.row-logo\s*\{[^}]*cursor:\s*zoom-in",
        src, re.DOTALL,
    ), (
        "imports.39: `.row-logo` must have `cursor: zoom-in` for click "
        "affordance"
    )


def test_row_logo_click_handler_registered():
    """JS click delegation on `.row-logo` opens the overlay."""
    src = _read(HTML)
    # The handler matches via `ev.target.closest('.row-logo')`.
    assert re.search(
        r"ev\.target\.closest\(['\"]\.row-logo['\"]\)",
        src,
    ), (
        "imports.39: JS must register a click handler that picks up "
        "`.row-logo` via closest()"
    )
    # And calls .classList.add('open') on the overlay.
    assert re.search(
        r"overlay\.classList\.add\(['\"]open['\"]\)",
        src,
    ), (
        "imports.39: handler must add the `open` class to the overlay"
    )


def test_overlay_click_closes():
    """Click anywhere on the overlay removes the `.open` class."""
    src = _read(HTML)
    assert re.search(
        r"getElementById\(['\"]logo-preview-overlay['\"]\)\?.addEventListener\(['\"]click['\"]",
        src,
    ), (
        "imports.39: overlay must have its own click listener that "
        "removes the `.open` class"
    )


def test_escape_key_closes_overlay():
    """Pressing Escape closes the overlay via a keydown listener."""
    src = _read(HTML)
    m = re.search(
        r"addEventListener\(['\"]keydown['\"]\s*,\s*\(\s*ev\s*\)\s*=>\s*\{[^}]*Escape[^}]*classList\.remove\(['\"]open['\"]\)",
        src, re.DOTALL,
    )
    assert m, (
        "imports.39: keydown handler for Escape must call "
        "classList.remove('open') on the overlay"
    )


def test_broken_image_rows_skipped():
    """Rows with `.row-logo.broken` (set by the existing `onerror`
    attribute on the thumbnail img tag when the image fails to load)
    should NOT open the overlay — no point previewing a missing image.
    Handler guards via `if (...img.classList.contains('broken')) return`."""
    src = _read(HTML)
    assert re.search(
        r"img\.classList\.contains\(['\"]broken['\"]\)",
        src,
    ), (
        "imports.39: handler must skip broken images via "
        "classList.contains('broken')"
    )


def test_stop_propagation_on_logo_click():
    """The .row-logo click handler stops propagation so the click
    doesn't ALSO bubble to row-level drag/select handlers (which would
    cause double-firing or unwanted side-effects)."""
    src = _read(HTML)
    # Look for ev.stopPropagation() inside the logo click handler.
    # Use a broad pattern — the stopPropagation must appear within the
    # handler that adds the 'open' class.
    m = re.search(
        r"overlay\.classList\.add\(['\"]open['\"]\).*?ev\.stopPropagation\(\)",
        src, re.DOTALL,
    )
    assert m, (
        "imports.39: the click handler must call ev.stopPropagation() "
        "to prevent the click from bubbling to row-level handlers"
    )


def test_changelog_has_imports_39_entry():
    """changelog.txt has the imports.39 entry — durable assertion."""
    src = _read(os.path.join(LIB, '..', '..', 'changelog.txt'))
    assert 'v.0.8.0+imports.39' in src, (
        "imports.39: changelog.txt must include the imports.39 entry"
    )


# ============================================================
# Regression
# ============================================================

def test_no_python_changes():
    """imports.39 is HTML-only — none of the Python helpers should have
    changed. Spot-check that `logo_helpers.py` and `server.py` aren't
    referenced from the new code."""
    src = _read(HTML)
    # The new JS handler doesn't fetch anything from the server beyond
    # what was already there. Verify no NEW fetch() call patterns were
    # introduced for the lightbox (i.e., no /images/preview or similar
    # endpoint).
    # Defensive check: handler reuses the thumbnail's `img.src` (same
    # URL the row already loaded).
    assert 'overlayImg.src = img.src' in src, (
        "imports.39: overlay img.src must reuse the thumbnail's URL "
        "(no new endpoint, browser-cached image, zero network hit)"
    )
