# -*- coding: utf-8 -*-
"""imports.69: the Save fix -- the dashboard's import endpoints (server.py),
run without a socket on a REAL channels.json.

The bug ("never synced"): the Edit-import window posted the whole import list
back on every Save and POST /imports.json stored it as sent. Every Save wiped
every import's last-sync time, error/retry state and deleted-channel list
(tombstones), and a running sync could write the list it had read before the
Save back over it. imports.69:
  - POST /imports.json takes one operation ({op: 'add' | 'edit' | 'delete'};
    an edit carries only the changed boxes) and applies it with
    Channels.saveImportRequest: one locked commit onto the file as it is now.
    A whole list from a tab opened before the update gets the one-change,
    up-to-date-only rules (importcfg_helpers._apply_list).
  - Replies 200 / 400 / 404 / 409 / 500 / 503 with ASCII messages that the
    old and the new page both show. A written change kicks a render cycle
    through set_kick (merged: it never lowers a pending kick) and shows the
    TV notice, each in its own try, so a written save never becomes a 500.
  - 🗑 Orphans is one commit with a render kick; ↻ Refresh merges its kick.
  - 🔨 Rebuild and 🧹 Cleanup stranded answer 500 "Channels not saved; try
    again" when their save is refused (before: always "ok").
  - GET /imports.json reports 'abandoned' as display_status; last_status
    keeps the stored 'failed', and next_retry_at is reply-only.
  - The TV's Channel Manager (manager.py) says "Channels not saved..." when
    its save is refused, and closing no longer asks again without end.

`dashboard_env` (a real Channels subclass bound to tmp_path, fake PROPERTIES
and DIALOG) is the shared fixture: steps 2 and 3 import it from here instead
of building a second one. All data is synthetic.
"""
import ast
import contextlib
import json
import os
import re
import textwrap
import threading
import time
from types import SimpleNamespace

import pytest
import xbmc

import channels as channels_module
import channels_lock
import importcfg_helpers as h
from channels import COMMIT_UNREADABLE, Channels
from kick_helpers import KICK_PROP, RENDER_ONLY_KICK as R
from test_imports66_show_pictures import _handler
from test_imports69_importcfg_helpers import T0, form_item, never_synced, new_form_item, old_edit, synced

HERE = os.path.dirname(os.path.abspath(__file__))
LIB  = os.path.normpath(os.path.join(HERE, '..', 'resources', 'lib'))

CUSTOM = 'c' * 32 + '@PseudoTV_Live'

MSG_BUSY       = 'Busy saving channels; try again'
MSG_UNREADABLE = "channels.json can't be read; nothing was saved"
MSG_FAILED     = 'Failed to save imports'
MSG_NOT_SAVED  = 'Channels not saved; try again'
MANAGER_NOT_SAVED = "Channels not saved: channels.json can't be read or written right now. Nothing was changed."


def _src(name):
    with open(os.path.join(LIB, name), encoding='utf-8') as f:
        return f.read()


def _rec(cid, number, **over):
    c = {'id': cid, 'number': number, 'name': 'Chan %d' % number, 'path': ['/x'], 'enabled': True, 'group': []}
    c.update(over)
    return c


def _imp(eid, iid, number, **over):
    """An imported channel record of import `iid`."""
    return _rec('%s@%s' % (eid, iid), number, type='import', import_source=iid, **over)


# ================================================================ the shared fixture


class FakeProps(object):
    """server.PROPERTIES for these endpoints: the kick property (read and
    written positionally, as kick_helpers.set_kick does), clrEXTProperty and
    setPropTimer. Records every write; fail_reads makes the kick read raise."""

    def __init__(self):
        self.ext = {}
        self.writes = []
        self.timers = []
        self.fail_reads = False

    def getEXTProperty(self, key, default=None):
        if self.fail_reads:
            raise RuntimeError('window property read failed')
        return self.ext.get(key, default)

    def setEXTProperty(self, key, value):
        self.writes.append((key, value))
        self.ext[key] = value
        return value

    def clrEXTProperty(self, key):
        self.ext.pop(key, None)

    def setPropTimer(self, key):
        self.timers.append(key)


class FakeDialog(object):
    """server.DIALOG: records the TV notices; fail makes them raise."""

    def __init__(self):
        self.notices = []
        self.fail = False

    def notificationDialog(self, message, *args, **kwargs):
        if self.fail:
            raise RuntimeError('notification failed')
        self.notices.append(message)
        return True


@pytest.fixture
def dashboard_env(monkeypatch, tmp_path):
    """server.MyHandler on a REAL channels.json in tmp_path (imports.69).

    server.Channels is the real Channels class, always bound to
    tmp_path/channels.json; channels/imports/server CACHE_LOC point at
    tmp_path/cache; server.PROPERTIES and server.DIALOG are FakeProps /
    FakeDialog. Requests come from the loopback address (no credentials).
    Returns a namespace with: server, path, cache, props, dialog, lines
    (Channels and channels_lock log lines), writes (FileAccess.setJSON calls),
    write(channels, imports), disk(), raw(), post(route, body),
    save(payload) (POST /imports.json), get(route), reply(handler) and
    open() (a writable Channels on the same file). Steps 2 and 3 import it."""
    import server
    import imports as imports_mod
    path = tmp_path / 'channels.json'
    cache = tmp_path / 'cache'
    cache.mkdir()

    class TmpChannels(Channels):
        """The real Channels, always on this test's channels.json."""

        def __init__(self, file=None, writable=False):
            super(TmpChannels, self).__init__(file=str(path), writable=writable)

    props, dialog, lines, writes = FakeProps(), FakeDialog(), [], []
    real_set_json = channels_module.FileAccess.setJSON

    def counting_set_json(fle, data):
        writes.append(fle)
        return real_set_json(fle, data)

    monkeypatch.setattr(server, 'Channels', TmpChannels)
    monkeypatch.setattr(server, 'PROPERTIES', props)
    monkeypatch.setattr(server, 'DIALOG', dialog)
    monkeypatch.setattr(server, 'CACHE_LOC', str(cache))
    monkeypatch.setattr(channels_module, 'CACHE_LOC', str(cache))
    monkeypatch.setattr(imports_mod, 'CACHE_LOC', str(cache))
    monkeypatch.setattr(channels_module.FileAccess, 'setJSON', counting_set_json)
    monkeypatch.setattr(channels_module, 'log', lambda msg, level=xbmc.LOGDEBUG: lines.append((msg, level)))
    monkeypatch.setattr(channels_lock, '_log', lambda msg, level=xbmc.LOGDEBUG: lines.append((msg, level)))
    monkeypatch.setattr(channels_module.PROPERTIES, 'setHasChannels', lambda state=True: None)
    # The stub's getMYUUID opens a self-closing dialog whose timer thread
    # keeps the test process from exiting (as test_imports66's server_env).
    monkeypatch.setattr(server.SETTINGS, 'getMYUUID', lambda: 'uuid-test')

    def write(channels=(), imports=(), **extra):
        doc = {'uuid': 'u', 'channels': list(channels), 'imports': [dict(i) for i in imports]}
        doc.update(extra)
        path.write_text(json.dumps(doc))

    def post(route, body):
        handler = _handler(server, route, body)
        handler.do_POST()
        return handler

    def get(route):
        handler = _handler(server, route)
        handler.do_GET()
        return handler

    env = SimpleNamespace(server=server, path=path, cache=cache, props=props, dialog=dialog, lines=lines,
                          writes=writes, write=write, post=post, get=get)
    env.disk = lambda: json.loads(path.read_text())
    env.raw = lambda: path.read_bytes()
    env.save = lambda payload: post('/imports.json', {'uuid': 'x', 'payload': payload})
    env.reply = lambda handler: json.loads(handler.wfile.getvalue().decode('utf-8'))
    env.open = lambda: TmpChannels(writable=True)
    return env


def _imports(env):
    return {i['id']: i for i in env.disk()['imports']}


def _logged(handler, needle, level):
    return [m for m, lvl in handler.logs if needle in m and lvl == level]


@contextlib.contextmanager
def _lock_held_elsewhere(monkeypatch):
    """CHANNELS_LOCK held by another thread (it is an RLock, so the test's
    own thread would simply re-enter it), with a short wait."""
    monkeypatch.setattr(channels_lock, 'CHANNELS_TIMEOUT', 0.2)
    held, done = threading.Event(), threading.Event()

    def holder():
        with channels_lock.CHANNELS_LOCK:
            held.set()
            done.wait(timeout=10)
    t = threading.Thread(target=holder, daemon=True)
    t.start()
    assert held.wait(timeout=5)
    try:
        yield
    finally:
        done.set()
        t.join(timeout=5)


def _two_imports():
    alpha = synced('alpha', tombstones=['GONE@alpha'], config_key='0123456789abcdef',
                   m3u_source_key='aaaaaaaaaaaaaaaa', epg_source_key='bbbbbbbbbbbbbbbb')
    beta = synced('beta', start_num=200)
    channels = [_rec(CUSTOM, 1), _imp('A1', 'alpha', 100), _imp('A2', 'alpha', 101), _imp('B1', 'beta', 200)]
    return alpha, beta, channels


# ================================================================ the new page's operations


def test_edit_keeps_every_sync_field_and_tombstones(dashboard_env):
    """The bug itself: an Edit changes only the boxes sent; every sync field
    and the tombstones stay, so the row never says "never synced" again."""
    env = dashboard_env
    alpha, beta, channels = _two_imports()
    env.write(channels, [alpha, beta])
    handler = env.save({'op': 'edit', 'id': 'alpha', 'entry': {'id': 'alpha', 'refresh_interval_min': 590}})
    assert handler.errors == [] and handler.statuses == [200]
    assert env.reply(handler) == {'ok': True, 'changed': True, 'op': 'edit', 'id': 'alpha'}
    stored = _imports(env)
    expected = dict(alpha, refresh_interval_min=590, config_key='pending')
    assert stored['alpha'] == expected
    assert stored['beta'] == beta
    assert env.disk()['channels'] == channels
    assert len(env.writes) == 1
    assert env.props.writes == [(KICK_PROP, R)]
    assert env.dialog.notices == ['Imports saved (2 configured)']
    assert _logged(handler, 'do_POST imports.json, edit [alpha] saved', xbmc.LOGINFO)
    shown = {i['id']: i for i in env.reply(env.get('/imports.json'))['imports']}
    assert shown['alpha']['last_sync_at'] == T0 and shown['alpha']['tombstones'] == ['GONE@alpha']


@pytest.mark.parametrize('box, value, reset, kept', [
    ('m3u_url', 'https://playlist.example/alpha-moved.m3u', h.PLAYLIST_RESET,
     ('epg_etag', 'epg_last_modified', 'epg_fail_count', 'playlist_rev', 'last_sync_at', 'm3u_source_key',
      'epg_source_key', 'tombstones')),
    ('epg_url', 'https://guide.example/alpha-moved.xml', h.GUIDE_RESET,
     ('etag', 'last_modified', 'links_mtime_ns', 'fail_count', 'failed_since', 'last_status', 'playlist_rev',
      'last_sync_at', 'm3u_source_key', 'epg_source_key', 'tombstones')),
])
def test_an_address_change_resets_that_address_and_marks_pending(dashboard_env, box, value, reset, kept):
    env = dashboard_env
    alpha, beta, channels = _two_imports()
    env.write(channels, [alpha, beta])
    handler = env.save({'op': 'edit', 'id': 'alpha', 'entry': {'id': 'alpha', box: value}})
    assert handler.statuses == [200] and env.reply(handler)['changed'] is True
    stored = _imports(env)['alpha']
    assert stored[box] == value and stored['config_key'] == 'pending'
    assert not set(reset).intersection(stored)
    for key in kept:
        assert stored[key] == alpha[key], key


@pytest.mark.parametrize('entry', [{'id': 'alpha'}, {'id': 'alpha', 'refresh_interval_min': 600, 'name': 'Alpha'}])
def test_a_save_that_changes_nothing_writes_nothing_and_kicks_nothing(dashboard_env, entry):
    env = dashboard_env
    alpha, beta, channels = _two_imports()
    env.write(channels, [alpha, beta])
    before = env.raw()
    handler = env.save({'op': 'edit', 'id': 'alpha', 'entry': entry})
    assert handler.statuses == [200]
    assert env.reply(handler) == {'ok': True, 'changed': False, 'op': 'edit', 'id': 'alpha'}
    assert env.raw() == before and env.writes == []
    assert env.props.writes == [] and env.dialog.notices == []
    assert _logged(handler, 'do_POST imports.json, edit [alpha] no change', xbmc.LOGINFO)


def test_add_appends_the_form_entry_without_sync_fields(dashboard_env):
    env = dashboard_env
    alpha, beta, channels = _two_imports()
    env.write(channels, [alpha, beta])
    entry = new_form_item('gamma', name='Gamma')
    handler = env.save({'op': 'add', 'entry': entry})
    assert handler.statuses == [200]
    assert env.reply(handler) == {'ok': True, 'changed': True, 'op': 'add', 'id': 'gamma'}
    stored = env.disk()['imports']
    assert [i['id'] for i in stored] == ['alpha', 'beta', 'gamma']        # list order is sync priority
    assert stored[2] == entry and set(stored[2]) == set(h.FORM_FIELDS)
    assert stored[0] == alpha and stored[1] == beta
    assert env.props.writes == [(KICK_PROP, R)]
    assert env.dialog.notices == ['Imports saved (3 configured)']


def test_add_without_a_channels_file_creates_it(dashboard_env):
    env = dashboard_env
    handler = env.save({'op': 'add', 'entry': new_form_item('gamma')})
    assert handler.statuses == [200]
    assert [i['id'] for i in env.disk()['imports']] == ['gamma']
    assert env.dialog.notices == ['Imports saved (1 configured)']


def test_delete_takes_the_import_its_records_and_its_guide_file_in_one_write(dashboard_env):
    env = dashboard_env
    alpha, beta, channels = _two_imports()
    env.write(channels, [alpha, beta])
    (env.cache / 'epg_alpha.xml').write_text('<tv/>')
    (env.cache / 'epg_beta.xml').write_text('<tv/>')
    handler = env.save({'op': 'delete', 'id': 'alpha'})
    assert handler.statuses == [200]
    assert env.reply(handler) == {'ok': True, 'changed': True, 'op': 'delete', 'id': 'alpha'}
    doc = env.disk()
    assert [i['id'] for i in doc['imports']] == ['beta'] and doc['imports'][0] == beta
    assert [c['id'] for c in doc['channels']] == [CUSTOM, 'B1@beta']
    assert not (env.cache / 'epg_alpha.xml').exists() and (env.cache / 'epg_beta.xml').exists()
    assert len(env.writes) == 1
    assert env.props.writes == [(KICK_PROP, R)]
    # a second click: already gone, nothing to do
    again = env.save({'op': 'delete', 'id': 'alpha'})
    assert again.statuses == [200] and env.reply(again)['changed'] is False
    assert len(env.writes) == 1 and env.props.writes == [(KICK_PROP, R)]


@pytest.mark.parametrize('pending, expected', [('', R), (R, R), ('alpha', 'alpha'), ('beta', 'beta'), ('all', 'all')])
def test_the_render_kick_never_lowers_a_pending_kick(dashboard_env, pending, expected):
    """S1-M7 at the endpoint: the Save's render kick is merged, so a pending
    'all' or import kick (a Refresh) stays as it is, with no write."""
    env = dashboard_env
    alpha, beta, channels = _two_imports()
    env.write(channels, [alpha, beta])
    if pending:
        env.props.ext[KICK_PROP] = pending
    handler = env.save({'op': 'edit', 'id': 'beta', 'entry': {'id': 'beta', 'enabled': False}})
    assert handler.statuses == [200] and env.reply(handler)['changed'] is True
    assert env.props.ext[KICK_PROP] == expected
    assert env.props.writes == ([(KICK_PROP, R)] if not pending else [])


# ================================================================ refused requests


REFUSALS = [
    ('new id breaks the pattern', {'op': 'add', 'entry': new_form_item('gamma', id='Bad Id')}, 400, h.MSG_BAD_ID),
    ('add an id that is taken', {'op': 'add', 'entry': new_form_item('alpha')}, 409,
     'Identifier "alpha" is already used by another import'),
    ('add a refused scheme', {'op': 'add', 'entry': new_form_item('gamma', m3u_url='file:///srv/test/x.m3u')}, 400,
     "Invalid playlist URL: unsupported scheme 'file' (allowed: http, https, special)"),
    ('edit a refused scheme', {'op': 'edit', 'id': 'alpha', 'entry': {'epg_url': 'ftp://guide.example/x.xml'}}, 400,
     "Invalid guide URL: unsupported scheme 'ftp' (allowed: http, https, special)"),
    ('identifier change', {'op': 'edit', 'id': 'alpha', 'entry': {'id': 'alpha2', 'name': 'x'}}, 400,
     h.MSG_ID_LOCKED),
    ('edit of a deleted import', {'op': 'edit', 'id': 'gone', 'entry': {'name': 'x'}}, 404, h.MSG_GONE),
    ('bad start number', {'op': 'edit', 'id': 'alpha', 'entry': {'start_num': 0}}, 400, h.MSG_START_NUM),
    ('bad tick box', {'op': 'edit', 'id': 'alpha', 'entry': {'enabled': 'yes'}}, 400,
     '"Enabled" must be ticked or unticked (true or false).'),
    ('unknown op', {'op': 'rename', 'id': 'alpha'}, 400, h.MSG_BAD_REQUEST),
    ('payload is text', 'alpha', 400, h.MSG_BAD_REQUEST),
    ('old page, duplicate ids', [form_item(synced('alpha')), form_item(synced('alpha'))], 400, h.MSG_LIST_DUPLICATE),
]


@pytest.mark.parametrize('payload, code, message', [r[1:] for r in REFUSALS], ids=[r[0] for r in REFUSALS])
def test_a_refused_request_writes_nothing_and_kicks_nothing(dashboard_env, payload, code, message):
    env = dashboard_env
    alpha, beta, channels = _two_imports()
    env.write(channels, [alpha, beta])
    before = env.raw()
    handler = env.save(payload)
    assert handler.errors == [(code, message)] and handler.statuses == []
    assert env.raw() == before and env.writes == []
    assert env.props.writes == [] and env.dialog.notices == []
    assert _logged(handler, 'do_POST imports.json refused (%d): %s' % (code, message), xbmc.LOGWARNING)
    message.encode('ascii')                                        # send_error: latin-1 status line


def test_an_edit_of_a_duplicated_id_is_refused_without_echoing_it(dashboard_env):
    """Two entries with one id (older versions or hand edits only): 409, and a
    legacy id outside latin-1 never reaches the status line (S1-M48)."""
    env = dashboard_env
    odd = 'canal-日本'
    env.write([_rec(CUSTOM, 1)], [synced(odd), synced(odd, name='Second')])
    before = env.raw()
    handler = env.save({'op': 'edit', 'id': odd, 'entry': {'refresh_interval_min': 30}})
    assert handler.errors == [(409, h.MSG_DUPLICATE_ID)]
    assert env.raw() == before and env.props.writes == []


def test_a_busy_channels_file_answers_503(dashboard_env, monkeypatch):
    env = dashboard_env
    alpha, beta, channels = _two_imports()
    env.write(channels, [alpha, beta])
    before = env.raw()
    with _lock_held_elsewhere(monkeypatch):
        handler = env.save({'op': 'edit', 'id': 'alpha', 'entry': {'refresh_interval_min': 30}})
    assert handler.errors == [(503, MSG_BUSY)] and handler.statuses == []
    assert env.raw() == before and env.props.writes == [] and env.dialog.notices == []
    assert [m for m, lvl in env.lines if 'channels_lock: TIMEOUT' in m and lvl == xbmc.LOGERROR]


def test_an_unreadable_channels_file_answers_500(dashboard_env):
    env = dashboard_env
    env.path.write_text('{"channels": [], "imports": [')            # cut short
    before = env.raw()
    handler = env.save({'op': 'add', 'entry': new_form_item('gamma')})
    assert handler.errors == [(500, MSG_UNREADABLE)]
    assert env.raw() == before and env.writes == [] and env.props.writes == []


@pytest.mark.parametrize('how', ['returns False', 'raises'])
def test_a_failed_write_answers_500(dashboard_env, monkeypatch, how):
    env = dashboard_env
    alpha, beta, channels = _two_imports()
    env.write(channels, [alpha, beta])
    before = env.raw()

    def failing(fle, data):
        if how == 'raises':
            raise Exception('FileLock: Timeout occurred.')
        return False
    monkeypatch.setattr(channels_module.FileAccess, 'setJSON', failing)
    handler = env.save({'op': 'edit', 'id': 'alpha', 'entry': {'refresh_interval_min': 30}})
    assert handler.errors == [(500, MSG_FAILED)]
    assert env.raw() == before and env.props.writes == [] and env.dialog.notices == []


def test_an_unexpected_error_answers_500(dashboard_env, monkeypatch):
    env = dashboard_env
    alpha, beta, channels = _two_imports()
    env.write(channels, [alpha, beta])

    def broken(self, payload, check_address=None):
        raise RuntimeError('broken merge')
    monkeypatch.setattr(Channels, 'saveImportRequest', broken)
    handler = env.save({'op': 'edit', 'id': 'alpha', 'entry': {'refresh_interval_min': 30}})
    assert handler.errors == [(500, MSG_FAILED)]
    assert _logged(handler, 'do_POST imports.json failed: broken merge', xbmc.LOGERROR)


@pytest.mark.parametrize('broken', ['kick', 'notice'])
def test_a_follow_up_failure_after_a_written_save_still_answers_200(dashboard_env, broken):
    """S1-M44: the save is written, so a failing kick or TV notice is logged
    at ERROR and the page still gets the 200 that matches the file; the other
    follow-up still runs."""
    env = dashboard_env
    alpha, beta, channels = _two_imports()
    env.write(channels, [alpha, beta])
    if broken == 'kick':
        env.props.fail_reads = True
    else:
        env.dialog.fail = True
    handler = env.save({'op': 'edit', 'id': 'alpha', 'entry': {'refresh_interval_min': 30}})
    assert handler.errors == [] and handler.statuses == [200]
    assert env.reply(handler)['changed'] is True
    assert _imports(env)['alpha']['refresh_interval_min'] == 30
    assert len(_logged(handler, 'do_POST imports.json, saved but the follow-up failed', xbmc.LOGERROR)) == 1
    if broken == 'kick':
        assert env.props.writes == [] and env.dialog.notices == ['Imports saved (2 configured)']
    else:
        assert env.props.writes == [(KICK_PROP, R)] and env.dialog.notices == []


def _real_error_reply(env, payload):
    """POST through the REAL BaseHTTPRequestHandler.send_error: the status
    line is encoded latin-1 (strict), so a message outside it would raise in
    the error path. Returns (status line, the message the page shows)."""
    import server
    handler = _handler(server, '/imports.json', {'uuid': 'x', 'payload': payload})
    for name in ('send_error', 'send_response', 'send_header', 'end_headers'):
        delattr(handler, name)
    handler.request_version = 'HTTP/1.1'
    handler.requestline = 'POST /imports.json HTTP/1.1'
    handler.command = 'POST'
    handler.log_message = lambda *a, **k: None
    handler.do_POST()
    raw = handler.wfile.getvalue()
    head, _, body = raw.partition(b'\r\n\r\n')
    status = head.split(b'\r\n')[0].decode('latin-1')
    # manager.html postImports: (t.match(/<p>Message: ([^<]+)/) || [, 'HTTP ' + res.status])[1]
    shown = re.search(r'<p>Message: ([^<]+)', body.decode('utf-8')).group(1)
    return status, shown


@pytest.mark.parametrize('payload, code, message', [
    ({'op': 'add', 'entry': new_form_item('alpha')}, 409, 'Identifier "alpha" is already used by another import'),
    ({'op': 'edit', 'id': 'canal-日本', 'entry': {'name': 'x'}}, 409, h.MSG_DUPLICATE_ID),
    ({'op': 'edit', 'id': 'gone', 'entry': {'name': 'x'}}, 404, h.MSG_GONE),
    ([form_item(synced('alpha'), id='alpha2')], 400, h.MSG_LIST_ID_CHANGE),
], ids=['add duplicate', 'non-latin-1 duplicate', 'deleted elsewhere', 'old page id change'])
def test_the_page_reads_the_real_error_reply(dashboard_env, payload, code, message):
    """Old and new pages extract `<p>Message: ...` from send_error's page."""
    env = dashboard_env
    odd = 'canal-日本'
    env.write([_rec(CUSTOM, 1)], [synced('alpha'), synced(odd), synced(odd, name='Second')])
    status, shown = _real_error_reply(env, payload)
    assert status.startswith('HTTP/1.1 %d %s' % (code, message))
    assert shown == message + '.'                     # http.server's page ends the message with a full stop


def test_the_page_reads_a_real_busy_reply(dashboard_env, monkeypatch):
    env = dashboard_env
    env.write([_rec(CUSTOM, 1)], [synced('alpha')])
    with _lock_held_elsewhere(monkeypatch):
        status, shown = _real_error_reply(env, {'op': 'delete', 'id': 'alpha'})
    assert status.startswith('HTTP/1.1 503 ') and shown == MSG_BUSY + '.'


# ================================================================ a tab opened before the update (whole lists)


def _morning(env, monkeypatch):
    """09:00: alpha and beta synced, beta failing (so GET adds next_retry_at;
    abandoning is off, as on the live box). Returns the page's list: GET's
    reply as the old page keeps it."""
    _abandon_after(monkeypatch, env.server, 0)
    alpha = synced('alpha', tombstones=['GONE@alpha'])
    beta = synced('beta', start_num=200, last_status='failed', last_error='HTTP 503', fail_count=2,
                  failed_since=T0 - 100)
    env.write([_rec(CUSTOM, 1), _imp('A1', 'alpha', 100), _imp('B1', 'beta', 200)], [alpha, beta])
    page = env.reply(env.get('/imports.json'))['imports']
    assert 'next_retry_at' in page[1]                    # a derived key the old page posts back
    return page


def _a_sync_ran(env, **changes):
    """A sync committed newer sync info since the page loaded (dashboard
    fields untouched); `changes` = {id: {field: value}} on top."""
    doc = env.disk()
    for entry in doc['imports']:
        entry.update(last_sync_at=T0 + 3600, etag='"p2"')
        entry.update(changes.get(entry['id'], {}))
    env.path.write_text(json.dumps(doc))


def test_old_tab_edit_keeps_everything_else(dashboard_env, monkeypatch):
    """An old tab's Edit applies that import's boxes only: the newer sync
    info of both imports, beta's state and alpha's tombstones stay, and the
    derived keys it posted back are never stored."""
    env = dashboard_env
    page = _morning(env, monkeypatch)
    _a_sync_ran(env, beta={'fail_count': 3})
    fresh = _imports(env)
    handler = env.save(old_edit(page, 'alpha', refresh_interval_min=590))
    assert handler.statuses == [200]
    assert env.reply(handler) == {'ok': True, 'changed': True, 'op': 'edit', 'id': 'alpha'}
    stored = _imports(env)
    assert stored['alpha'] == dict(fresh['alpha'], refresh_interval_min=590, config_key='pending')
    assert stored['beta'] == fresh['beta']
    assert stored['alpha']['last_sync_at'] == T0 + 3600 and stored['alpha']['tombstones'] == ['GONE@alpha']
    for entry in stored.values():
        assert not set(h.DERIVED_FIELDS).intersection(entry)
    assert env.props.writes == [(KICK_PROP, R)]


def test_old_tab_save_without_changes_writes_nothing(dashboard_env, monkeypatch):
    env = dashboard_env
    page = _morning(env, monkeypatch)
    _a_sync_ran(env)
    before = env.raw()
    handler = env.save(old_edit(page, 'alpha'))
    assert handler.statuses == [200] and env.reply(handler)['changed'] is False
    assert env.raw() == before and env.props.writes == []


def test_old_tab_delete_and_add_still_work(dashboard_env, monkeypatch):
    env = dashboard_env
    page = _morning(env, monkeypatch)
    handler = env.save([e for e in page if e['id'] != 'beta'])
    assert handler.statuses == [200]
    assert env.reply(handler) == {'ok': True, 'changed': True, 'op': 'delete', 'id': 'beta'}
    assert [c['id'] for c in env.disk()['channels']] == [CUSTOM, 'A1@alpha']
    page = env.reply(env.get('/imports.json'))['imports']
    handler = env.save(page + [new_form_item('gamma')])
    assert handler.statuses == [200] and env.reply(handler)['op'] == 'add'
    assert [i['id'] for i in env.disk()['imports']] == ['alpha', 'gamma']


def _changed_elsewhere(env):
    _a_sync_ran(env, beta={'refresh_interval_min': 300})


def _added_elsewhere(env):
    doc = env.disk()
    doc['imports'].append(never_synced('gamma'))
    env.path.write_text(json.dumps(doc))


def _deleted_elsewhere(env):
    doc = env.disk()
    doc['imports'] = [i for i in doc['imports'] if i['id'] != 'beta']
    env.path.write_text(json.dumps(doc))


def _no_change_save(page):
    return old_edit(page, 'alpha')


def _two_edits(page):
    out = old_edit(page, 'alpha', refresh_interval_min=590)
    return old_edit(out, 'beta', refresh_interval_min=590)


def _id_change(page):
    return old_edit(page, 'alpha', id='alpha2')


@pytest.mark.parametrize('elsewhere, build, code, message', [
    (_changed_elsewhere, _no_change_save, 409, h.MSG_OUT_OF_DATE),
    (_added_elsewhere, _no_change_save, 409, h.MSG_OUT_OF_DATE),
    (_deleted_elsewhere, _no_change_save, 409, h.MSG_OUT_OF_DATE),
    (None, _two_edits, 409, h.MSG_OUT_OF_DATE),
    (None, _id_change, 400, h.MSG_LIST_ID_CHANGE),
], ids=['another import changed', 'an import added', 'an import deleted', 'two edits', 'identifier change'])
def test_old_tab_out_of_date_is_refused(dashboard_env, monkeypatch, elsewhere, build, code, message):
    """S1-M12 / S1-M41 / S1-M14 end to end: never "saved" with another import
    reverted, deleted or re-added; the file stays byte-identical."""
    env = dashboard_env
    page = _morning(env, monkeypatch)
    if elsewhere is not None:
        elsewhere(env)
    before = env.raw()
    handler = env.save(build(page))
    assert handler.errors == [(code, message)]
    assert env.raw() == before and env.writes == [] and env.props.writes == []


# ================================================================ GET /imports.json


def _abandon_after(monkeypatch, server, hours):
    real = server.SETTINGS.getSettingInt

    def fake(key, *args, **kwargs):
        if key == 'Imports_Failure_Abandon_Hours':
            return hours
        if key == 'Imports_Failure_Retry_Curve':
            return 1
        return real(key, *args, **kwargs)
    monkeypatch.setattr(server.SETTINGS, 'getSettingInt', fake)


def _failing(env, monkeypatch):
    now = int(time.time())
    retrying = synced('beta', last_status='failed', last_error='HTTP 503', fail_count=2,
                      failed_since=now - 60, last_sync_at=now - 60)
    stuck = synced('gamma', last_status='failed', last_error='HTTP 404', fail_count=9,
                   failed_since=now - 3 * 3600, last_sync_at=now - 600)
    env.write([_rec(CUSTOM, 1)], [synced('alpha'), retrying, stuck])
    _abandon_after(monkeypatch, env.server, 1)


def test_get_shows_abandoned_as_display_status(dashboard_env, monkeypatch):
    """S1-M25: 'abandoned' comes as display_status; last_status keeps the
    stored 'failed'; next_retry_at only for an import still retrying; the
    file is never touched by a GET."""
    env = dashboard_env
    _failing(env, monkeypatch)
    before = env.raw()
    handler = env.get('/imports.json')
    assert handler.statuses == [200]
    shown = {i['id']: i for i in env.reply(handler)['imports']}
    assert shown['gamma']['display_status'] == 'abandoned' and shown['gamma']['last_status'] == 'failed'
    assert 'next_retry_at' not in shown['gamma']
    assert 'display_status' not in shown['beta'] and shown['beta']['last_status'] == 'failed'
    assert isinstance(shown['beta']['next_retry_at'], int) and shown['beta']['next_retry_at'] > shown['beta']['last_sync_at']
    assert not set(h.DERIVED_FIELDS).intersection(shown['alpha'])
    assert env.raw() == before and env.writes == []


def test_derived_keys_never_reach_storage_after_a_round_trip(dashboard_env, monkeypatch):
    env = dashboard_env
    _failing(env, monkeypatch)
    page = env.reply(env.get('/imports.json'))['imports']
    before = env.raw()
    unchanged = env.save(page)                                     # an old tab posts GET's list back
    assert unchanged.statuses == [200] and env.reply(unchanged)['changed'] is False
    assert env.raw() == before
    old = env.save(old_edit(page, 'gamma', refresh_interval_min=30))
    assert old.statuses == [200]
    new = env.save({'op': 'edit', 'id': 'beta', 'entry': {'id': 'beta', 'refresh_interval_min': 45}})
    assert new.statuses == [200]
    stored = _imports(env)
    for entry in stored.values():
        assert not set(h.DERIVED_FIELDS).intersection(entry)
    assert stored['gamma']['last_status'] == 'failed' and stored['gamma']['fail_count'] == 9
    assert stored['beta']['last_status'] == 'failed' and stored['beta']['fail_count'] == 2


# ================================================================ 🗑 Orphans


def _orphans_disk(env):
    alpha = synced('alpha', tombstones=['OLD@alpha'])
    env.write([_rec(CUSTOM, 1), _imp('O1', 'alpha', 100, is_orphan=True), _imp('O2', 'alpha', 101, is_orphan=True),
               _imp('L1', 'alpha', 102, is_orphan=False), _imp('O3', 'beta', 200, is_orphan=True)],
              [alpha, synced('beta')])


def test_orphans_is_one_commit_with_a_render_kick(dashboard_env):
    env = dashboard_env
    _orphans_disk(env)
    handler = env.post('/imports/orphans/remove.json', {'uuid': 'x', 'id': 'alpha'})
    assert handler.errors == [] and handler.statuses == [200]
    assert env.reply(handler) == {'ok': True, 'removed': 2}
    doc = env.disk()
    assert [c['id'] for c in doc['channels']] == [CUSTOM, 'L1@alpha', 'O3@beta']
    assert _imports(env)['alpha']['tombstones'] == ['O1@alpha', 'O2@alpha', 'OLD@alpha']
    assert 'tombstones' not in _imports(env)['beta']
    assert len(env.writes) == 1
    assert env.props.writes == [(KICK_PROP, R)]                   # a render kick, not a forced 'alpha'
    assert env.dialog.notices == ['Orphans removed: 2 in [alpha]']


def test_orphans_render_kick_never_lowers_a_pending_one(dashboard_env):
    env = dashboard_env
    _orphans_disk(env)
    env.props.ext[KICK_PROP] = 'all'
    handler = env.post('/imports/orphans/remove.json', {'uuid': 'x', 'id': 'alpha'})
    assert handler.statuses == [200] and env.props.ext[KICK_PROP] == 'all' and env.props.writes == []


def test_orphans_with_nothing_to_remove_writes_nothing(dashboard_env):
    env = dashboard_env
    _orphans_disk(env)
    env.post('/imports/orphans/remove.json', {'uuid': 'x', 'id': 'alpha'})
    env.props.ext.pop(KICK_PROP, None)                             # the poll loop took that kick
    env.props.writes.clear()
    env.dialog.notices.clear()
    before = env.raw()
    handler = env.post('/imports/orphans/remove.json', {'uuid': 'x', 'id': 'alpha'})
    assert handler.statuses == [200] and env.reply(handler) == {'ok': True, 'removed': 0}
    assert env.raw() == before and env.props.writes == [] and KICK_PROP not in env.props.ext
    assert env.dialog.notices == ['Orphans removed: 0 in [alpha]']


@pytest.mark.parametrize('body, code, message', [
    ({'uuid': 'x', 'id': 'gone'}, 404, 'import id not found'),
    ({'uuid': 'x'}, 400, 'id required'),
])
def test_orphans_refusals(dashboard_env, body, code, message):
    env = dashboard_env
    _orphans_disk(env)
    before = env.raw()
    handler = env.post('/imports/orphans/remove.json', body)
    assert handler.errors == [(code, message)] and handler.statuses == []
    assert env.raw() == before and env.props.writes == [] and env.dialog.notices == []


def test_orphans_busy_and_unreadable(dashboard_env, monkeypatch):
    env = dashboard_env
    _orphans_disk(env)
    with _lock_held_elsewhere(monkeypatch):
        busy = env.post('/imports/orphans/remove.json', {'uuid': 'x', 'id': 'alpha'})
    assert busy.errors == [(503, MSG_BUSY)]
    env.path.write_text('not json')
    unreadable = env.post('/imports/orphans/remove.json', {'uuid': 'x', 'id': 'alpha'})
    assert unreadable.errors == [(500, MSG_UNREADABLE)]
    assert env.props.writes == [] and env.dialog.notices == []


# ================================================================ ↻ Refresh


@pytest.mark.parametrize('pending, target, expected', [
    ('', 'alpha', 'alpha'), ('', '', 'all'), (R, 'alpha', 'alpha'), ('alpha', 'alpha', 'alpha'),
    ('beta', 'alpha', 'all'), ('all', 'alpha', 'all'), ('alpha', '', 'all'),
])
def test_refresh_merges_its_kick(dashboard_env, pending, target, expected):
    """A row's Refresh never cancels a pending 'all'; two rows waiting become
    'all' (both sync)."""
    env = dashboard_env
    if pending:
        env.props.ext[KICK_PROP] = pending
    handler = env.post('/imports/refresh.json', {'uuid': 'x', 'id': target})
    assert handler.statuses == [200] and env.reply(handler) == {'ok': True}
    assert env.props.ext[KICK_PROP] == expected
    assert env.props.writes == ([] if expected == pending else [(KICK_PROP, expected)])
    assert env.dialog.notices == ['Refresh queued: %s' % (target or 'all imports')]


# ================================================================ 🔨 Rebuild and 🧹 Cleanup stranded


@contextlib.contextmanager
def _save_refused(env, monkeypatch, how):
    if how == 'busy':
        with _lock_held_elsewhere(monkeypatch):
            yield
        return
    if how == 'failed write':
        monkeypatch.setattr(channels_module.FileAccess, 'setJSON', lambda fle, data: False)
    else:                                                          # damaged after the request read it
        monkeypatch.setattr(Channels, '_readFresh', lambda self: (COMMIT_UNREADABLE, 'ValueError: damaged'))
    yield


HOWS = ['failed write', 'unreadable', 'busy']


@pytest.mark.parametrize('how', HOWS)
@pytest.mark.parametrize('body', [{'uuid': 'x', 'all': True}, {'uuid': 'x', 'channel_id': CUSTOM}],
                         ids=['all', 'one'])
def test_rebuild_reports_a_refused_save(dashboard_env, monkeypatch, how, body):
    """S1-M45: no 'queued' for a rebuild that wasn't saved: 500, no Builder
    kick, no TV notice."""
    env = dashboard_env
    env.write([_rec(CUSTOM, 1, changed=False), _imp('A1', 'alpha', 100)], [synced('alpha')])
    before = env.raw()
    with _save_refused(env, monkeypatch, how):
        handler = env.post('/channels/rebuild.json', body)
    assert handler.errors == [(500, MSG_NOT_SAVED)] and handler.statuses == []
    assert env.raw() == before and env.props.timers == [] and env.dialog.notices == []


def test_rebuild_still_queues_when_saved(dashboard_env):
    env = dashboard_env
    env.write([_rec(CUSTOM, 1, changed=False), _imp('A1', 'alpha', 100)], [synced('alpha')])
    handler = env.post('/channels/rebuild.json', {'uuid': 'x', 'all': True})
    assert handler.statuses == [200] and env.reply(handler) == {'ok': True, 'flipped': 1}
    assert env.disk()['channels'][0]['changed'] is True
    assert env.props.timers == ['chkChanged'] and len(env.dialog.notices) == 1


def _stranded(env):
    env.write([_rec(CUSTOM, 1), _imp('A1', 'alpha', 100), _imp('S1', 'gone', 900)], [synced('alpha')])
    (env.cache / 'epg_gone.xml').write_text('<tv/>')


@pytest.mark.parametrize('how', HOWS)
def test_cleanup_stranded_reports_a_refused_save_and_keeps_the_guide_file(dashboard_env, monkeypatch, how):
    env = dashboard_env
    _stranded(env)
    before = env.raw()
    with _save_refused(env, monkeypatch, how):
        handler = env.post('/imports/cleanup_stranded.json', {'uuid': 'x'})
    assert handler.errors == [(500, MSG_NOT_SAVED)] and handler.statuses == []
    assert env.raw() == before
    assert (env.cache / 'epg_gone.xml').exists()
    assert env.props.writes == [] and env.dialog.notices == []


def test_cleanup_stranded_still_cleans_when_saved(dashboard_env):
    env = dashboard_env
    _stranded(env)
    handler = env.post('/imports/cleanup_stranded.json', {'uuid': 'x'})
    assert handler.statuses == [200]
    assert env.reply(handler) == {'ok': True, 'removed': 1, 'stranded_sources': ['gone']}
    assert [c['id'] for c in env.disk()['channels']] == [CUSTOM, 'A1@alpha']
    assert not (env.cache / 'epg_gone.xml').exists()
    assert env.props.writes == [(KICK_PROP, 'all')]               # imports.71 converts this one


# ================================================================ the TV's Channel Manager (manager.py)
#
# manager.py's window class can't be built under the test stubs, so its
# saveChanges and closeManager are compiled from the shipped source and run on
# a stand-in, with fake module globals.


def _shipped_manager_methods(namespace):
    src = _src('manager.py')
    tree = ast.parse(src)
    klass = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Manager')
    methods = {}
    for name in ('saveChanges', 'closeManager'):
        fn = next(n for n in klass.body if isinstance(n, ast.FunctionDef) and n.name == name)
        code = textwrap.dedent(ast.get_source_segment(src, fn, padded=True))
        exec(compile(code, 'manager.py:%s' % (name,), 'exec'), namespace)    # noqa: S102 -- the shipped method, run on fakes
        methods[name] = namespace[name]
    return methods


class _GiveUp(Exception):
    pass


def _manager(saves_ok, launch=True):
    """A Channel Manager stand-in whose local save returns `saves_ok`. More
    than 3 prompts or saves means it asks again without end."""
    state = SimpleNamespace(prompts=0, saves=0, notices=[], timers=[], closed=0, filled=0)

    def yesno(*args, **kwargs):
        state.prompts += 1
        if state.prompts > 3:
            raise _GiveUp('asked "save changes?" again and again')
        return True

    def set_channels(channels):
        state.saves += 1
        if state.saves > 3:
            raise _GiveUp('saved again and again')
        return saves_ok

    namespace = {
        'DIALOG': SimpleNamespace(yesnoDialog=yesno, notificationDialog=lambda msg, *a, **k: state.notices.append(msg)),
        'LANGUAGE': lambda number: 'string %d' % (number,),
        'PROPERTIES': SimpleNamespace(isRunning=lambda key: False, setPropTimer=state.timers.append),
        'SETTINGS': SimpleNamespace(getMYUUID=lambda: 'uuid-test'),
        'Globals': SimpleNamespace(requestURL=lambda *a, **k: None),
        'CHANNELFLE': 'channels.json', 'CHANNELFLE_CHANGED': 'changed', 'CHANNELFLE_LATEST': 'latest',
        'xbmc': xbmc,
    }
    methods = _shipped_manager_methods(namespace)

    class Manager(object):
        saveChanges = methods['saveChanges']
        closeManager = methods['closeManager']

        def __init__(self):
            self.launchManager = launch
            self.server = {}
            self.madeChanges = True
            self.newChannels = [{'id': CUSTOM, 'name': 'One', 'path': ['/x'], 'number': 1}]
            self.channels = SimpleNamespace(setChannels=set_channels, sortChannels=list)
            self.backup = SimpleNamespace(backupChannels=lambda *a, **k: True)

        def log(self, *args, **kwargs):
            pass

        def setID(self, citem):
            return citem

        @contextlib.contextmanager
        def toggleSpinner(self, condition=True):
            yield

        def fillChanList(self, *args, **kwargs):
            state.filled += 1

        def close(self):
            state.closed += 1

    return Manager(), state


@pytest.mark.parametrize('launch, prompts', [(True, 1), (False, 0)], ids=['window', 'no window (autotune)'])
def test_channel_manager_reports_a_refused_save_and_closes(launch, prompts):
    """Closing with changes: one "save changes?" (none without a window), the
    refused save is reported once, and the Manager closes instead of asking
    and retrying without end."""
    manager, state = _manager(saves_ok=False, launch=launch)
    manager.closeManager()
    assert state.prompts == prompts and state.saves == 1
    assert state.notices == [MANAGER_NOT_SAVED]
    assert manager.madeChanges is False and state.closed >= 1
    assert state.timers == []


def test_channel_manager_save_button_keeps_the_changes_when_refused():
    """The Save button (close=False): reported, the window stays open with
    the changes, so Save can be tried again."""
    manager, state = _manager(saves_ok=False)
    manager.saveChanges(1, close=False)
    assert state.saves == 1 and state.notices == [MANAGER_NOT_SAVED]
    assert manager.madeChanges is True and state.closed == 0


def test_channel_manager_written_save_is_unchanged():
    manager, state = _manager(saves_ok=True)
    manager.closeManager()
    assert state.saves == 1 and state.notices == [] and manager.madeChanges is False
    assert state.timers == ['chkChanged'] and state.filled == 1


# ================================================================ hygiene of the changed files


@pytest.mark.parametrize('name', ['server.py', 'tasks.py', 'manager.py'])
def test_changed_files_are_python38(name):
    """Kodi 19/20 run Python 3.8: the AST check plus the shared deny-list
    ('|' is checked by the one scanner in test_imports69_importcfg_helpers)."""
    from test_imports63_clock import py39_api_leaks
    src = _src(name)
    ast.parse(src, feature_version=(3, 8))
    assert py39_api_leaks(src) == []
    assert 'madteevee (imports fork, imports.69)' in src


def test_the_save_endpoint_no_longer_stores_whole_lists():
    """POST /imports.json goes through saveImportRequest only; setImports is
    no longer reachable from server.py, and the scheme check is passed in."""
    src = _src('server.py')
    assert 'setImports(' not in src
    assert src.count('saveImportRequest(') == 1
    assert 'check_address=lambda a: validate_source_url(a, ALLOWED_M3U_SCHEMES)' in src
