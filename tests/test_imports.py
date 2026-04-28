"""Smoke test: every module under resources/lib/ must import without error.

Catches:
- SyntaxError in any .py file (the data/station.py vestige class)
- ImportError / ModuleNotFoundError on missing transitive deps
- NameError at module-level / class-body / decorator evaluation
  (the tasks.py @cacheit class-decorator failure we hit in Phase 3)

Does NOT catch function-body NameErrors (utilities.py:171 msg, backup.py:75 ctl
type bugs) — those are caught by ruff F821 with wildcards removed (audit item 4).
The two layers together provide defense in depth.
"""
from pathlib import Path
import importlib

import pytest

# Discover all .py modules under resources/lib (no __init__.py required —
# pathlib.rglob walks files directly).
_LIB_ROOT = Path(__file__).parent.parent / 'plugin.video.pseudotv.live' / 'resources' / 'lib'


def _discover():
    """Yield (module_name, abspath) for every .py under lib root, recursive.

    Examples:
      resources/lib/globals.py        -> 'globals'
      resources/lib/parsers/MP4Parser.py -> 'parsers.MP4Parser'
    """
    for py in sorted(_LIB_ROOT.rglob('*.py')):
        if '__pycache__' in py.parts:
            continue
        rel = py.relative_to(_LIB_ROOT).with_suffix('')
        yield '.'.join(rel.parts), str(py)


MODULES = list(_discover())


@pytest.mark.parametrize('modname,abspath', MODULES, ids=[m[0] for m in MODULES])
def test_import(modname, abspath):
    """Import each module; any failure (Syntax/Import/NameError) fails this test."""
    importlib.import_module(modname)
