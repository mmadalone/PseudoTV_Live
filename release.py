#!/usr/bin/env python3
"""
Build a Kodi-style addon repository at _site/ for hosting on GitHub Pages.

Layout produced (matches Kodi's repository <datadir> convention):

    _site/
        addons.xml
        addons.xml.md5
        plugin.video.pseudotv.live/
            plugin.video.pseudotv.live-<VERSION>.zip
            icon.png
            fanart.jpg
        repository.mmadalone.pseudotv/
            repository.mmadalone.pseudotv-<VERSION>.zip
            icon.png
            fanart.jpg

Run from the repo root:
    python3 release.py

Then publish _site/ via gh-pages branch (see FORK_NOTES.md).
"""
import hashlib
import os
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

REPO_ROOT = Path(__file__).resolve().parent
SITE_DIR = REPO_ROOT / "_site"

# Each entry is (addon_dir_under_repo_root, addon_id)
ADDONS = [
    ("plugin.video.pseudotv.live", "plugin.video.pseudotv.live"),
    ("repository.mmadalone.pseudotv", "repository.mmadalone.pseudotv"),
]

# Files/dirs to exclude from the addon zip
EXCLUDE_FILE_SUFFIXES = (".pyc", ".pyo", ".db", ".bak", ".bak2")
EXCLUDE_DIR_NAMES = {"__pycache__", ".git", ".idea", ".vscode", "venv", "_site"}


def read_addon_metadata(addon_dir: Path) -> tuple[str, str, ET.Element]:
    """Parse addon.xml; return (addon_id, version, root_element)."""
    addon_xml = addon_dir / "addon.xml"
    tree = ET.parse(addon_xml)
    root = tree.getroot()
    return root.get("id"), root.get("version"), root


def should_skip(path: Path) -> bool:
    if path.name.endswith(EXCLUDE_FILE_SUFFIXES):
        return True
    parts = set(path.parts)
    return bool(parts & EXCLUDE_DIR_NAMES)


def zip_addon(addon_dir: Path, addon_id: str, version: str, dest_dir: Path) -> Path:
    """Build <addon_id>-<version>.zip with the addon dir at the zip root."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    zip_name = f"{addon_id}-{version}.zip"
    zip_path = dest_dir / zip_name

    if zip_path.exists():
        zip_path.unlink()

    with ZipFile(zip_path, "w", ZIP_DEFLATED) as zf:
        for path in sorted(addon_dir.rglob("*")):
            rel = path.relative_to(addon_dir.parent)
            if should_skip(rel):
                continue
            if path.is_file():
                zf.write(path, arcname=str(rel))
    return zip_path


def copy_assets(addon_dir: Path, dest: Path) -> None:
    """Copy icon.png + fanart.jpg next to the zip if present (so Kodi can preview)."""
    for asset in ("icon.png", "fanart.jpg"):
        src = addon_dir / asset
        if not src.exists():
            # fallback: search nested resources/images/
            for nested in addon_dir.rglob(asset):
                src = nested
                break
        if src.exists():
            shutil.copy2(src, dest / asset)


def build_addons_xml(addon_roots: list[ET.Element]) -> bytes:
    """Concatenate each addon's root <addon> element into <addons> wrapper."""
    out = ET.Element("addons")
    for r in addon_roots:
        out.append(r)
    ET.indent(out, space="    ")
    return b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + ET.tostring(out, encoding="utf-8")


def md5_of(content: bytes) -> str:
    return hashlib.md5(content).hexdigest()


def main() -> int:
    if SITE_DIR.exists():
        shutil.rmtree(SITE_DIR)
    SITE_DIR.mkdir(parents=True)

    roots = []
    for addon_subdir, addon_id in ADDONS:
        addon_dir = REPO_ROOT / addon_subdir
        if not addon_dir.is_dir():
            print(f"SKIP {addon_id}: dir not found at {addon_dir}", file=sys.stderr)
            continue
        parsed_id, version, root = read_addon_metadata(addon_dir)
        if parsed_id != addon_id:
            print(f"WARN: addon.xml id={parsed_id!r} differs from expected {addon_id!r}", file=sys.stderr)

        # Per-addon dir under _site/
        per_addon_dir = SITE_DIR / addon_id
        per_addon_dir.mkdir(parents=True, exist_ok=True)

        zip_path = zip_addon(addon_dir, addon_id, version, per_addon_dir)
        copy_assets(addon_dir, per_addon_dir)
        print(f"  built {zip_path.relative_to(REPO_ROOT)} ({zip_path.stat().st_size} bytes)")
        roots.append(root)

    addons_xml = build_addons_xml(roots)
    (SITE_DIR / "addons.xml").write_bytes(addons_xml)
    (SITE_DIR / "addons.xml.md5").write_text(md5_of(addons_xml) + "\n")
    print(f"  wrote {SITE_DIR/'addons.xml'} (md5 {md5_of(addons_xml)})")

    # Drop a .nojekyll so GH Pages serves files (e.g. ones starting with _ or .)
    (SITE_DIR / ".nojekyll").write_text("")

    # README for the gh-pages root (browseable via GH Pages too)
    (SITE_DIR / "index.md").write_text(
        "# mmadalone Kodi repo (PseudoTV Live madteevee fork)\n\n"
        "Add this URL to a Kodi repository addon's `<info>` element:\n"
        "    https://mmadalone.github.io/PseudoTV_Live/addons.xml\n\n"
        "Or install the bundled repository addon:\n"
        "    https://mmadalone.github.io/PseudoTV_Live/repository.mmadalone.pseudotv/repository.mmadalone.pseudotv-1.0.0.zip\n\n"
        "Source: https://github.com/mmadalone/PseudoTV_Live\n"
    )

    print(f"\nDone. Publish {SITE_DIR}/ to the gh-pages branch.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
