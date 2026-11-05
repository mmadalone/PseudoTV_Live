"""imports.61 (B): add-on updates no longer count as playlist edits.

The settings cache mixed the add-on version into every check number
(cache._Cache.global_checksum = ADDON_VERSION). The playlist fingerprint
(Settings.getFileCRC) and the playlist position (JSONRPC.autoPagination) were
stored that way, so after every update every Custom channel looked edited:
the Builder wiped and rebuilt all of them and restarted each playlist from its
first episode. Both memories are now stored without a check number
(versioned=False) and compared by value; everything else stays tied to the
version on purpose (the changelog-once flag, the saved job queues that
imports.60 relies on being dropped, measured lengths, JSON-RPC results).

These tests run the real cache code on a throwaway sqlite file and simulate an
update by changing _Cache.global_checksum, as the real cache sees it.
"""
import ast
import datetime
import io
import os
import sqlite3
import types

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
LIB  = os.path.normpath(os.path.join(HERE, '..', 'resources', 'lib'))

V60 = '0.8.0+imports.60'
V61 = '0.8.0+imports.61'
SCHEMA = "CREATE TABLE IF NOT EXISTS cache(id TEXT UNIQUE, expires INTEGER, data TEXT, checksum INTEGER)"


class FakeGlobals(object):
    def __init__(self): self.props = {}
    def _getProperty(self, key, default=''): return self.props.get(key, default)
    def _setProperty(self, key, value): self.props[key] = value
    def _clrProperty(self, key): self.props.pop(key, None)


class QuietMonitor(object):
    def abortRequested(self): return False
    def waitForAbort(self, timeout=0): return False


@pytest.fixture
def db(monkeypatch, tmp_path):
    """The real _Cache (database only, as Settings.cacheDB) wrapped in a real Cache,
    on a temp sqlite file. rig.update(version) simulates installing that version."""
    import cache as cache_mod
    monkeypatch.setattr(cache_mod, 'Globals', FakeGlobals())
    raw = cache_mod._Cache.__new__(cache_mod._Cache)
    raw.service = cache_mod.Service()
    raw.service.monitor = QuietMonitor()
    raw.monitor = raw.service.monitor
    raw.dbfile = str(tmp_path / 'cache.db')
    raw.enable_mem_cache = False
    raw.global_checksum = V60
    conn = sqlite3.connect(raw.dbfile)
    conn.execute(SCHEMA)
    conn.commit()
    conn.close()
    wrapper = cache_mod.Cache.__new__(cache_mod.Cache)
    wrapper.cache = raw
    wrapper.disable_cache = False

    def update(version): raw.global_checksum = version

    def row(key):
        conn = sqlite3.connect(raw.dbfile)
        try: return conn.execute("SELECT expires, data, checksum FROM cache WHERE id = ?", (key,)).fetchone()
        finally: conn.close()

    def seed(key, value, checksum, expires=None):
        from fileaccess import FileAccess
        expires = expires or raw.getTimestamp(datetime.datetime.now() + datetime.timedelta(days=30))
        conn = sqlite3.connect(raw.dbfile)
        conn.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?,?)", (key, expires, FileAccess.dumpPICKLE(value), checksum))
        conn.commit()
        conn.close()

    return types.SimpleNamespace(mod=cache_mod, raw=raw, cache=wrapper, update=update, row=row, seed=seed)


def _old_check(version, value):
    """The check number earlier versions stored: sum of the characters of '<version>-<value>'."""
    return sum(map(ord, '%s-%s' % (version, value)))


# --- the cache flag ---------------------------------------------------------------

def test_unversioned_check_number_is_zero_and_versioned_ones_are_unchanged(db):
    assert db.raw.getChecksum('anything', versioned=False) == 0
    assert db.raw.getChecksum(3424300505) == _old_check(V60, 3424300505) == 1768    # X-Files row, live 2026-11-04
    assert db.raw.getChecksum('') == _old_check(V60, '')


def test_unversioned_entry_survives_an_update_and_a_versioned_one_does_not(db):
    db.cache.set('kept', 'value', versioned=False)
    db.cache.set('tied', 'value')
    db.update(V61)
    assert db.cache.get('kept', versioned=False) == 'value'
    assert db.cache.get('tied') is None
    assert db.row('kept')[2] == 0


def test_unversioned_entry_still_expires(db):
    db.cache.set('old', 'value', expiration=datetime.timedelta(seconds=-5), versioned=False)
    assert db.cache.get('old', versioned=False) is None


# --- playlist fingerprint (Settings.getFileCRC) --------------------------------------

@pytest.fixture
def crc(db, monkeypatch):
    """Real Settings.getFileCRC over a fake playlist file; returns (settings, path, write)."""
    import binascii
    import kodi
    settings = kodi.Settings.__new__(kodi.Settings)
    settings.cacheDB = db.cache
    files = {}
    real_open = kodi.FileAccess.open

    def fake_open(path, mode='r', *a, **k):
        # Only the playlist is faked; the cache's FileLock opens its lock file through here too.
        if path in files: return io.StringIO(files[path])
        return real_open(path, mode, *a, **k)

    monkeypatch.setattr(kodi.FileAccess, 'open', staticmethod(fake_open))
    path = 'special://profile/playlists/video/X-Files.xsp'

    def write(text):
        files[path] = text
        return binascii.crc32(text.encode('utf-8'))

    return types.SimpleNamespace(settings=settings, path=path, write=write, kodi=kodi)


def _crc_key(crc):
    from fileaccess import FileAccess
    return 'getFileCRC.%s' % FileAccess._getMD5(crc.path)


def test_fingerprint_stored_by_imports60_is_reused_after_the_update(db, crc):
    """The transition: rows written by imports.60 carry a version-tied check number.
    After the update they must still be read (no rebuild) and get rewritten without one."""
    value = crc.write('<smartplaylist type="episodes"><name>X-Files</name></smartplaylist>')
    old_expiry = db.raw.getTimestamp(datetime.datetime.now() + datetime.timedelta(days=10))
    db.seed(_crc_key(crc), value, _old_check(V60, value), expires=old_expiry)
    db.update(V61)
    assert crc.settings.getFileCRC(crc.path) is False                 # not "edited": nothing is rebuilt
    expires, _, checksum = db.row(_crc_key(crc))
    assert checksum == 0 and expires > old_expiry                     # revived without a check number


def test_real_edit_is_detected_once_also_across_updates(db, crc):
    crc.write('<smartplaylist>v1</smartplaylist>')
    assert crc.settings.getFileCRC(crc.path) is True                  # first sight
    assert crc.settings.getFileCRC(crc.path) is False
    db.update(V61)
    assert crc.settings.getFileCRC(crc.path) is False                 # an update is not an edit
    crc.write('<smartplaylist>v2</smartplaylist>')
    assert crc.settings.getFileCRC(crc.path) is True                  # a real edit still is
    assert crc.settings.getFileCRC(crc.path) is False
    assert db.row(_crc_key(crc))[2] == 0


def test_missing_fingerprint_counts_as_edited(db, crc):
    crc.write('<smartplaylist>new channel</smartplaylist>')
    assert crc.settings.getFileCRC(crc.path) is True
    assert db.row(_crc_key(crc))[2] == 0


# --- playlist position (JSONRPC.autoPagination) --------------------------------------

@pytest.fixture
def rpc(db):
    import jsonrpc
    r = jsonrpc.JSONRPC(service=types.SimpleNamespace(jsonQue=set()))
    r.cache = db.cache
    return r


CHID = '3330342e57656972642054616c65732e@PseudoTV_Live'
XSP  = 'special://profile/playlists/video/Weird Tales.xsp'


def _page_key(chid, path, query=None):
    from fileaccess import FileAccess
    return 'autoPagination.%s.%s.%s' % (chid, FileAccess._getMD5(path), FileAccess._getMD5(FileAccess.dumpJSON(query or {})))


def test_playlist_position_stored_by_imports60_is_kept_after_the_update(db, rpc):
    """Weird Tales was at 500/2098 on 2026-11-04; before imports.61 an update reset it to 0."""
    db.seed(_page_key(CHID, XSP), {'end': 500, 'start': 500, 'total': 2098}, _old_check(V60, CHID))
    db.update(V61)
    assert rpc.autoPagination(CHID, XSP) == {'end': 500, 'start': 500, 'total': 2098}


def test_playlist_position_round_trip_and_reset(db, rpc):
    rpc.autoPagination(CHID, XSP, {}, {'end': 1000, 'start': 1000, 'total': 2098})
    db.update(V61)
    assert rpc.autoPagination(CHID, XSP) == {'end': 1000, 'start': 1000, 'total': 2098}
    expires, _, checksum = db.row(_page_key(CHID, XSP))
    assert checksum == 0
    now = datetime.datetime.now()                                      # kept 28 days (Cache.set's default is 15 min)
    assert db.raw.getTimestamp(now + datetime.timedelta(days=27)) < expires <= db.raw.getTimestamp(now + datetime.timedelta(days=28, minutes=1))
    assert rpc.resetPagination(CHID, XSP)                               # a real edit still restarts the playlist
    assert rpc.autoPagination(CHID, XSP) == {'end': 0, 'start': 0, 'total': 0}


def test_unknown_playlist_starts_at_the_top(rpc):
    assert rpc.autoPagination(CHID, 'special://profile/playlists/video/None.xsp') == {'end': 0, 'start': 0, 'total': 0}


# --- what must still reset on an update ------------------------------------------------

def test_version_tied_entries_still_reset_on_update(db, crc, rpc):
    from fileaccess import FileAccess
    settings = crc.settings
    settings.setCacheSetting('lastVersion', V60, checksum=V60)          # tasks.py: changelog shown once per version
    settings.setCacheSetting('jsonQue', ['queued request'])             # services.py: dropped on update (imports.60)
    md5 = FileAccess._getMD5('/media/film.mkv')
    rpc.cache.set('getDuration.%s' % md5, 4763, checksum=md5)          # jsonrpc._setDuration: re-measured after update
    db.update(V61)
    assert settings.getCacheSetting('lastVersion', checksum=V61) is None
    assert settings.getCacheSetting('jsonQue') is None
    assert rpc.cache.get('getDuration.%s' % md5, checksum=md5) is None


# --- only the two intended memories are version-free --------------------------------------

# Positional slot of `versioned` in each cache entry point (self excluded).
VERSIONED_SLOT = {'getCacheSetting': 3, 'setCacheSetting': 4, 'get': 2, 'set': 4}


def _versioned_calls():
    """Every call that sets `versioned` to anything but its own `versioned` parameter:
    keyword or positional, in functions, class bodies or at module level. Returns
    (file, enclosing def/class or '<module>', how)."""
    found = []
    for root, _, names in os.walk(LIB):
        for name in names:
            if not name.endswith('.py'): continue
            tree = ast.parse(open(os.path.join(root, name), encoding='utf-8').read())
            parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}

            def owner(node):
                while node in parents:
                    node = parents[node]
                    if isinstance(node, (ast.FunctionDef, ast.ClassDef)): return node.name
                return '<module>'

            for call in [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]:
                attr = call.func.attr
                for kw in call.keywords:
                    if kw.arg == 'versioned' and not (isinstance(kw.value, ast.Name) and kw.value.id == 'versioned'):
                        found.append((name, owner(call), 'versioned=%s' % ast.unparse(kw.value)))
                slot = VERSIONED_SLOT.get(attr)
                receiver = ast.unparse(call.func.value).lower()
                if slot is None or (attr in ('get', 'set') and 'cache' not in receiver): continue
                if len(call.args) > slot and not (isinstance(call.args[slot], ast.Name) and call.args[slot].id == 'versioned'):
                    found.append((name, owner(call), 'positional %s' % ast.unparse(call.args[slot])))
    return sorted(set(found))


def test_only_the_fingerprint_and_the_playlist_position_are_version_free():
    assert _versioned_calls() == [('jsonrpc.py', 'autoPagination', 'versioned=False'),
                                  ('kodi.py', 'getFileCRC', 'versioned=False')]


def test_both_use_the_database_only_cache_instance():
    """The memory cache (_getMEM) returns expired entries when both check numbers are 0."""
    kodi_src = open(os.path.join(LIB, 'kodi.py'), encoding='utf-8').read()
    rpc_src  = open(os.path.join(LIB, 'jsonrpc.py'), encoding='utf-8').read()
    assert '    cacheDB    = Cache()\n' in kodi_src
    assert 'self.cacheDB.get(key, checksum, versioned)' in kodi_src and 'self.cacheDB.set(key, value, checksum, life, versioned)' in kodi_src
    assert '    cache       = SETTINGS.cacheDB\n' in rpc_src


@pytest.mark.parametrize('name', ['cache.py', 'kodi.py', 'jsonrpc.py'])
def test_changed_files_pass_the_ast_python38_check(name):
    ast.parse(open(os.path.join(LIB, name), encoding='utf-8').read(), feature_version=(3, 8))
