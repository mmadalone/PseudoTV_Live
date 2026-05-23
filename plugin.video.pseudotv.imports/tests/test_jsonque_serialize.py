"""imports.49 — jsonQue serialize/deserialize fix.

queueJSON pre-imports.49 added raw dicts to `self.service.jsonQue` (a `set`).
Sets only accept hashable items, so the call raised:
    TypeError: unhashable type: 'dict'
…on every invocation. The path was dormant until imports.48 + Store_Duration=true
made queDuration call queueJSON for every ffprobed-and-different-from-runtime file.

Fix mirrors the logoQue pattern at resources.py:120 (which has been serializing
to JSON-string for years): writer at jsonrpc.py:86 now FileAccess.dumpJSON(param)
before .add; reader at tasks.py:1135 FileAccess.loadJSON(...) before dispatch.
Dedup is preserved (identical dict → identical JSON string → one set entry).
"""
import json
import os
import re
import types


def _queueJSON_mirror(self, param):
    """Mirror of jsonrpc.JSONRPC.queueJSON (imports.49)."""
    if hasattr(self.service, 'jsonQue'):
        self.service.jsonQue.add(json.dumps(param, sort_keys=True))


def _pop_jsonQue_mirror(jsonQue):
    """Mirror of tasks.py chkQUES jsonQue pop branch (imports.49)."""
    return json.loads(jsonQue.pop())


def _make_stub():
    return types.SimpleNamespace(service=types.SimpleNamespace(jsonQue=set()))


# ────────────────────────────────────────────────────────────────── behaviour


def test_dict_param_no_longer_raises_TypeError():
    """The regression guard: dict param + queueJSON used to TypeError on the .add()
    because dicts aren't hashable. imports.49 serializes to string first."""
    stub = _make_stub()
    payload = {'method': 'VideoLibrary.SetEpisodeDetails',
               'params': {'episodeid': 14, 'runtime': 2661,
                          'resume': {'position': 0.0, 'total': 2661}}}
    _queueJSON_mirror(stub, payload)  # would raise pre-imports.49
    assert len(stub.service.jsonQue) == 1


def test_dispatch_roundtrip_recovers_original_dict():
    """Consumer pops the JSON string and deserializes; result must equal the
    original dict so sendJSON gets the same dispatchable shape it expected."""
    stub = _make_stub()
    payload = {'method': 'VideoLibrary.SetMovieDetails',
               'params': {'movieid': 99, 'runtime': 4763}}
    _queueJSON_mirror(stub, payload)
    recovered = _pop_jsonQue_mirror(stub.service.jsonQue)
    assert recovered == payload


def test_set_dedup_still_works():
    """Same dict added three times → set still dedups to one entry. sort_keys
    ensures stable serialization across calls."""
    stub = _make_stub()
    p = {'method': 'X', 'params': {'a': 1, 'b': 2}}
    for _ in range(3):
        _queueJSON_mirror(stub, p)
    assert len(stub.service.jsonQue) == 1


def test_different_dicts_get_different_entries():
    stub = _make_stub()
    _queueJSON_mirror(stub, {'method': 'A', 'params': {}})
    _queueJSON_mirror(stub, {'method': 'B', 'params': {}})
    assert len(stub.service.jsonQue) == 2


# ────────────────────────────────────────────────────────────────── source-scan


def _read(rel):
    here = os.path.dirname(os.path.abspath(__file__))
    return open(os.path.join(os.path.dirname(here), rel)).read()


def test_jsonrpc_queueJSON_serializes_before_add():
    src = _read(os.path.join('resources', 'lib', 'jsonrpc.py'))
    # Permissive regex: jsonQue.add(FileAccess.dumpJSON(param)) with optional whitespace
    assert re.search(r'jsonQue\.add\(\s*FileAccess\.dumpJSON\(\s*param\s*\)\s*\)', src), (
        "imports.49 regression: jsonrpc.JSONRPC.queueJSON must call "
        "FileAccess.dumpJSON(param) before .add() — otherwise dict params "
        "raise TypeError: unhashable type"
    )


def test_tasks_chkQUES_deserializes_after_pop():
    src = _read(os.path.join('resources', 'lib', 'tasks.py'))
    assert re.search(r'FileAccess\.loadJSON\(\s*self\.service\.jsonQue\.pop\(\)\s*\)', src), (
        "imports.49 regression: tasks.py chkQUES must FileAccess.loadJSON "
        "the jsonQue.pop() value — the queue now stores JSON strings"
    )
