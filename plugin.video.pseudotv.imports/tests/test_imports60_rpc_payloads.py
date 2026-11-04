"""imports.60: every JSON-RPC request the add-on sends carries a "method".

The Store_Duration write-back queued bare params ({"episodeid":..,"resume":..})
with no "method"; Kodi rejected every one ("JSONRPC: Failed to parse") and the
add-on only logged it below its default log level. This scan reads every
sendJSON / cacheJSON / queueJSON / sendRPC call and every
_que(<x>.sendJSON, prio, request) call under resources/lib, and traces the
request argument back to a dict literal with a "method" key.

Two pass-throughs are allowed by name: cacheJSON hands its own `param` to
sendJSON, and tasks.chkQUES sends what queueJSON stored (queueJSON's callers
are scanned). The Rollback_Watched restore request is built by
playstate_helpers.restore_request and its shape is tested in
test_imports60_playstate.py.

It also checks that no library/file setter request carries "runtime" (a
runtime write would overwrite the scraped runtime with a measured length).
"""
import ast
import os

HERE      = os.path.dirname(os.path.abspath(__file__))
ADDON_LIB = os.path.join(os.path.dirname(HERE), 'resources', 'lib')
SINKS     = {'sendJSON', 'cacheJSON', 'queueJSON', 'sendRPC'}
PASS_THROUGH = {('jsonrpc.py', 'cacheJSON'), ('tasks.py', 'chkQUES')}
SETTERS   = {'Files.SetFileDetails', 'VideoLibrary.SetMovieDetails', 'VideoLibrary.SetEpisodeDetails',
             'VideoLibrary.SetMusicVideoDetails', 'VideoLibrary.SetTVShowDetails', 'VideoLibrary.SetSeasonDetails',
             'AudioLibrary.SetSongDetails'}


def _name(node):
    if isinstance(node, ast.Attribute): return node.attr
    if isinstance(node, ast.Name): return node.id
    return None


def _has_method(node):
    return isinstance(node, ast.Dict) and any(isinstance(k, ast.Constant) and k.value == 'method' for k in node.keys)


def _traceable(expr, func, depth=0):
    """True when `expr` is (or is only ever assigned from) a request dict literal."""
    if _has_method(expr): return True
    if isinstance(expr, ast.Name) and func is not None and depth < 3:
        values = [n.value for n in ast.walk(func) if isinstance(n, ast.Assign)
                  for t in n.targets if isinstance(t, ast.Name) and t.id == expr.id]
        return bool(values) and all(_traceable(v, func, depth + 1) for v in values)
    if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute) and expr.func.attr == 'get':
        base = expr.func.value  # table.get(key) over a dict of request literals
        if isinstance(base, ast.Name) and func is not None:
            for n in ast.walk(func):
                if (isinstance(n, ast.Assign) and isinstance(n.value, ast.Dict) and n.value.values
                        and any(isinstance(t, ast.Name) and t.id == base.id for t in n.targets)):
                    return all(_has_method(v) for v in n.value.values)
    return False


def scan(source, filename):
    """[(line, sink, function, ok)] for every request sent in `source`."""
    tree = ast.parse(source, filename=filename, feature_version=(3, 8))
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node): parents[child] = node
    rows = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call): continue
        sink = _name(node.func)
        if sink in SINKS and node.args:
            payload = node.args[0]
        elif sink == '_que' and node.args and _name(node.args[0]) == 'sendJSON':
            sink, payload = '_que(sendJSON)', (node.args[2] if len(node.args) > 2 else None)
        else:
            continue
        func = node
        while func in parents and not isinstance(func, ast.FunctionDef): func = parents[func]
        func = func if isinstance(func, ast.FunctionDef) else None
        fname = func.name if func else '<module>'
        ok = payload is not None and ((os.path.basename(filename), fname) in PASS_THROUGH or _traceable(payload, func))
        rows.append((node.lineno, sink, fname, ok))
    return rows


def setter_literals_with_runtime(source, filename):
    """Lines of request literals for a library/file setter whose params have 'runtime'."""
    hits = []
    for node in ast.walk(ast.parse(source, filename=filename, feature_version=(3, 8))):
        if not _has_method(node): continue
        fields = {k.value: v for k, v in zip(node.keys, node.values) if isinstance(k, ast.Constant)}
        method, params = fields.get('method'), fields.get('params')
        if isinstance(method, ast.Constant) and method.value in SETTERS and isinstance(params, ast.Dict):
            if any(isinstance(k, ast.Constant) and k.value == 'runtime' for k in params.keys):
                hits.append(node.lineno)
    return hits


def _sources():
    for root, _, files in os.walk(ADDON_LIB):
        if '__pycache__' in root: continue
        for name in sorted(files):
            if name.endswith('.py'):
                path = os.path.join(root, name)
                with open(path, encoding='utf-8') as f: yield path, f.read()


# The imports.59 queDuration shape (jsonrpc.py), reduced: the scan must flag it.
OLD_QUEDURATION = '''
class JSONRPC(object):
    def queDuration(self, item={}, duration=0, runtime=0):
        mtypes = {'movie'  : {"method":"VideoLibrary.SetMovieDetails"  ,"params":{"movieid"  :item.get('id',-1),"runtime": runtime,"resume": {"position": 0.0,"total": duration}}},
                  'episode': {"method":"VideoLibrary.SetEpisodeDetails","params":{"episodeid":item.get('id',-1),"runtime": runtime,"resume": {"position": 0.0,"total": duration}}}}
        mtype = mtypes.get(item.get('type'))
        self.queueJSON(mtype['params'])
'''


def test_scan_flags_the_imports59_write_back():
    rows = scan(OLD_QUEDURATION, 'jsonrpc.py')
    assert [(sink, fname, ok) for _, sink, fname, ok in rows] == [('queueJSON', 'queDuration', False)]
    assert len(setter_literals_with_runtime(OLD_QUEDURATION, 'jsonrpc.py')) == 2


def test_scan_accepts_well_formed_requests():
    src = '''
class JSONRPC(object):
    def a(self, key):
        param = {"method":"Settings.GetSettingValue","params":{"setting":key}}
        return self.sendJSON(param)
    def b(self):
        return self.cacheJSON({"method":"Player.GetViewMode","params":{}})
'''
    assert [ok for *_, ok in scan(src, 'jsonrpc.py')] == [True, True]


def test_every_request_in_the_addon_has_a_method():
    bad, total = [], 0
    for path, src in _sources():
        for line, sink, fname, ok in scan(src, path):
            total += 1
            if not ok: bad.append('%s:%s %s in %s' % (os.path.relpath(path, ADDON_LIB), line, sink, fname))
    assert total > 50          # the scan really ran over the add-on
    assert bad == []


def test_no_setter_request_writes_runtime():
    hits = ['%s:%s' % (os.path.relpath(path, ADDON_LIB), line)
            for path, src in _sources() for line in setter_literals_with_runtime(src, path)]
    assert hits == []
