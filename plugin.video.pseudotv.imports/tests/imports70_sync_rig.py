# -*- coding: utf-8 -*-
"""imports.70: the syncAll rig of the guide re-describe (a) and stale-export
guard (c) integration tests (test_imports70_guide_redescribe.py,
test_imports70_stale_export.py).

The imports.59 replay-links rig (test_imports59_replay_links_sync.py), with
what these tests need on top. A Movistar+-like import 'mp': its playlist is
plugin.video.movistarplus's channels.m3u8 in a tmp addon_data folder (it
always answers "unchanged", so the stored records are the channels), its
replay links come from the epg.xml next to it (read for real, through
Imports.fetchSource's local read), and its guide comes from an address
(davidmuma-like: channel ids are display names). Two link-replay channels:
C1 ('Chan One', in the guide) and C2 ('Squirrel', only in the export).

  - Every time is built from the moment the rig is made (rig.now), never
    from the hour, so the show on air is the one the test means (the first
    prototype hit this with the hour-anchored slots).
  - The guide and the export are written per test (guide_xml, export_xml);
    the export is aged with os.utime (write_export's age_s).
  - Each fetch is recorded with its download markers; the log lines too.
  - The channel store is in memory, with the shared sync commit
    (fake_commit_sync).

make_rig is a plain function, not a fixture, so a test's arguments never
shadow an imported name. Not collected by pytest (no test_ prefix).
Synthetic data only: example.invalid addresses, made-up ids.
"""
import os
import time
from types import SimpleNamespace
from xml.sax.saxutils import escape, quoteattr

import fake_commit_sync
import imports as imports_mod
from imports import Imports


IID       = 'mp'
OLD_GUIDE = 'https://guide-old.example.invalid/guide.xml'
NEW_GUIDE = 'https://guide-new.example.invalid/guide.xml'
H         = 3600
M         = 60
C1        = 'C1@%s' % IID
C2        = 'C2@%s' % IID


def ts(epoch):
    """XMLTV time, +0000."""
    return time.strftime('%Y%m%d%H%M%S', time.gmtime(epoch)) + ' +0000'


def show(start, stop, title, desc=None, link=None, sub=None):
    """One programme as (start, stop, title, desc, link, sub-title)."""
    return (start, stop, title, desc, link, sub)


def _programme_xml(channel, item):
    start, stop, title, desc, link, sub = item
    attrs = 'start="%s" stop="%s" channel=%s' % (ts(start), ts(stop), quoteattr(channel))
    if link is not None:
        attrs += ' catchup-id=%s' % quoteattr(link)
    body = '<title>%s</title>' % escape(title)
    if sub is not None:
        body += '<sub-title>%s</sub-title>' % escape(sub)
    if desc is not None:
        body += '<desc>%s</desc>' % escape(desc)
    return '<programme %s>%s</programme>' % (attrs, body)


def guide_xml(shows, channel='Chan One HD', name='Chan One'):
    """A davidmuma-like guide: one channel whose id is a display name."""
    return ('<?xml version="1.0" encoding="UTF-8"?><tv><channel id=%s><display-name>%s</display-name></channel>'
            % (quoteattr(channel), escape(name))
            + ''.join(_programme_xml(channel, s) for s in shows) + '</tv>').encode('utf-8')


def export_xml(c1=(), c2=()):
    """A Movistar+-like guide export: channels C1 and C2, shows with links."""
    return ('<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE tv SYSTEM "xmltv.dtd"><tv>'
            '<channel id="C1"><display-name>Chan One</display-name></channel>'
            '<channel id="C2"><display-name>Squirrel</display-name></channel>'
            + ''.join(_programme_xml('C1', s) for s in c1)
            + ''.join(_programme_xml('C2', s) for s in c2) + '</tv>').encode('utf-8')


def published_show(channel, item):
    """A programme as the guide data holds it (what an earlier sync
    published), from show()."""
    start, stop, title, desc, link, sub = item
    p = {'channel': channel, 'start': ts(start), 'stop': ts(stop), 'title': [(title, '')]}
    if link is not None:
        p['catchup-id'] = link
    if desc is not None:
        p['desc'] = [(desc, '')]
    if sub is not None:
        p['sub-title'] = [(sub, '')]
    return p


class Store(object):
    """The channel store in memory, with the shared sync commit."""

    def __init__(self, imports_list, channels_list):
        self._i, self._c = list(imports_list), list(channels_list)

    def getImports(self):
        return list(self._i)

    def getChannels(self):
        return list(self._c)

    def setChannels(self, channels=None, modified_ids=None, deleted_ids=None):
        if channels is not None:
            self._c = list(channels)
        return True

    def setImports(self, data):
        self._i = list(data)
        return True

    commitSync = fake_commit_sync.commit_sync


class Playlist(object):
    """The in-memory playlist: the stations added, by id."""

    def __init__(self):
        self.added = {}

    def addStation(self, station):
        self.added[station['id']] = station

    def delStation(self, station):
        self.added.pop(station.get('id'), None)


class Guide(object):
    """The in-memory guide data (what pseudotv.xml held when the sync started)."""

    def __init__(self, programmes=()):
        self.XMLTVDATA = {'channels': [{'id': C1, 'display-name': [('Chan One', '')]},
                                       {'id': C2, 'display-name': [('Squirrel', '')]}],
                          'programmes': list(programmes), 'recordings': []}


def rec(tvg, name, number, link=True):
    """A stored channel record of the import; link=False: catch-up that
    doesn't use per-show links (the old Movistar+ row)."""
    cid = '%s@%s' % (tvg, IID)
    return {'id': cid, 'epg_id': tvg, 'name': name, 'source_name': name, 'import_source': IID,
            'type': 'import', 'enabled': True, 'number': number, 'assigned_number': number,
            'is_orphan': False, 'unallocated': False, 'source_url': 'plugin://x/?live=%s' % tvg,
            'url': 'plugin://x/?live=%s' % tvg, 'group': ['MP'], 'logo': '', 'catchup': 'default',
            'catchup-source': '{catchup-id}' if link else '', 'catchup-days': '7' if link else '5',
            'operator_overrides': []}


def records(link=True):
    return [rec('C1', 'Chan One', 1, link=link), rec('C2', 'Squirrel', 2, link=link)]


def make_rig(tmp_path, monkeypatch):
    """The rig for one test: CACHE_LOC in tmp, the disk-presence check off,
    the failure settings fixed (aggressive retry curve, never abandon, no
    jitter; rig.ints holds them, a test may change them), the exporter's
    folder with its playlist, and the log recorded. rig.now is the moment
    every show of the test is built from."""
    monkeypatch.setattr(imports_mod, 'CACHE_LOC', str(tmp_path))
    monkeypatch.setattr(imports_mod, '_isDiskMissing', lambda iid, scope: False)
    imports_mod._GUIDE_SELFHEAL_LAST.clear()
    real_int, real_bool = imports_mod.SETTINGS.getSettingInt, imports_mod.SETTINGS.getSettingBool
    ints = {'Imports_Failure_Retry_Curve': 1, 'Imports_Failure_Abandon_Hours': 0, 'Imports_Disk_Presence_Scope': 0}

    def get_int(key, *a, **k):
        return ints[key] if key in ints else real_int(key, *a, **k)

    def get_bool(key, *a, **k):
        return False if key == 'Imports_Cache_Logos' else real_bool(key, *a, **k)
    monkeypatch.setattr(imports_mod.SETTINGS, 'getSettingInt', get_int)
    monkeypatch.setattr(imports_mod.SETTINGS, 'getSettingBool', get_bool)
    monkeypatch.setattr(imports_mod.random, 'randint', lambda a, b: 0)
    folder = tmp_path / 'addon_data' / 'plugin.video.movistarplus'
    folder.mkdir(parents=True)
    (folder / 'channels.m3u8').write_text('#EXTM3U\n')
    lines = []
    monkeypatch.setattr(Imports, 'log', lambda self, msg, level=None: lines.append((msg, level)))
    return SimpleNamespace(tmp=tmp_path, folder=folder, export=folder / 'epg.xml',
                           cache=tmp_path / ('epg_%s.xml' % IID), lines=lines, now=int(time.time()), ints=ints)


def write_export(rig, c1=(), c2=(), age_s=None):
    """Write the export; age_s: make it that many seconds old (from rig.now).
    Returns its st_mtime_ns."""
    rig.export.write_bytes(export_xml(c1, c2))
    if age_s is not None:
        age(rig, age_s)
    return os.stat(str(rig.export)).st_mtime_ns


def age(rig, age_s):
    """Make the export age_s seconds old (from rig.now), content unchanged."""
    t = rig.now - age_s
    os.utime(str(rig.export), (t, t))
    return os.stat(str(rig.export)).st_mtime_ns


def entry(rig, **over):
    """The import entry: due for a sync (synced 11 h ago, 10 h interval)."""
    cfg = {'id': IID, 'name': 'MP', 'enabled': True, 'm3u_path': str(rig.folder / 'channels.m3u8'),
           'm3u_url': '', 'epg_url': OLD_GUIDE, 'epg_path': '', 'start_num': 1, 'refresh_interval_min': 600,
           'last_sync_at': rig.now - 11 * H, 'last_status': 'unchanged', 'tombstones': [],
           'respect_source_numbers': True, 'rediscover_deleted': False, 'playlist_rev': 1}
    cfg.update(over)
    return cfg


def sync(rig, monkeypatch, cfg, guide=None, guide_answer=None, previous=(), chans=None, force_scope=None):
    """One real syncAll. The guide address answers 200 with `guide`, or
    `guide_answer` (a fetchSource result) when given; the playlist answers
    "unchanged"; the export is read for real. `previous`: the guide data's
    programmes when the sync starts. Returns a namespace: res (the import's
    result), results, guide (the guide data after the sync), playlist,
    calls ((address, etag, last_modified) of each fetch) and store."""
    real_fetch = Imports.fetchSource
    calls = []

    def fetch(self, url, etag=None, last_modified=None, **kw):
        calls.append((url, etag, last_modified))
        if url.endswith('epg.xml'):
            return real_fetch(self, url, etag=etag, last_modified=last_modified)
        base = {'status': 0, 'content': None, 'etag': None, 'last_modified': None, 'error': None}
        if url.endswith('channels.m3u8'):
            base['status'] = 304
        elif guide_answer is not None:
            base.update(guide_answer)
        else:
            base.update(status=200, content=guide)
        return base
    monkeypatch.setattr(Imports, 'fetchSource', fetch)
    store = Store([cfg], records() if chans is None else chans)
    data, playlist = Guide(previous), Playlist()
    results = Imports(channels=store, m3u=playlist, xmltv=data).syncAll(force_scope=force_scope)
    return SimpleNamespace(res=results.get(IID), results=results, guide=data, playlist=playlist,
                           calls=calls, store=store)


def on(run, channel):
    """The guide data's programmes of `channel` after the sync, by start."""
    return sorted((p for p in run.guide.XMLTVDATA['programmes'] if p.get('channel') == channel),
                  key=lambda p: imports_mod.replay_helpers.xmltv_to_epoch(p['start']))


def row(p):
    """(start, stop, title, desc, link) of a published programme, epochs."""
    to_epoch = imports_mod.replay_helpers.xmltv_to_epoch
    return (to_epoch(p['start']), to_epoch(p['stop']), p['title'][0][0],
            p['desc'][0][0] if p.get('desc') else None, p.get('catchup-id') or None)


def overlaps(run, channel):
    spans = [row(p)[:2] for p in on(run, channel)]
    return [(a, b) for a, b in zip(spans, spans[1:]) if b[0] < a[1]]


def logged(rig, needle, level=None):
    return [m for m, lvl in rig.lines if needle in m and (level is None or lvl == level)]
