"""imports.59 (S2) — loading the guide never throws entries away when the
channel list can't be read.

Every writer (chkImports, Builder, the recordings menu, cleanup) loads
pseudotv.xml through XMLTVS.cleanSelf. Before imports.59 a failed Channels()
lookup set import_slugs=set(), so isOwnedID rejected every import entry, and
the next save wrote the guide without them -- kept replay history included.
FileAccess.getJSON returns {} on any read error, so a failed or half-written
channels.json read looked like "no channels, no imports" and had the same
effect. Custom entries already failed open (test_xmltvs_cleanself_channels_xref).

Now, when the channel list is unknown, cleanSelf keeps every entry of ours as
loaded for that load (no cross-reference, no age trim) and logs an error.
Normal channel lists behave as before.
"""
import time

import pytest

import replay_helpers
from xmltvs import XMLTVS


CUSTOM = 'a' * 32 + '@PseudoTV_Live'
GONE   = 'b' * 32 + '@PseudoTV_Live'
REC    = 'e' * 16 + '@PseudoTV_Live'
TVE    = 'TVE@mp'
OTHER  = 'X@other'
NOW    = int(time.time())


def _ts(epoch):
    return time.strftime('%Y%m%d%H%M%S', time.gmtime(epoch)) + ' +0000'


def _prog(ch, stop, link=''):
    return {'channel': ch, 'start': _ts(stop - 3600), 'stop': _ts(stop), 'catchup-id': link,
            'title': [('t', '')]}


class _Logged(XMLTVS):
    """Real XMLTVS methods, no file load; records log lines."""
    def __init__(self):
        self.m3u = None
        self.XMLTVDATA = {}
        self.lines = []

    def log(self, msg, level=None):
        self.lines.append((msg, level))


def _channels(monkeypatch, channels_list=None, imports_list=None, raises=False):
    import channels as channels_module

    class _Mock(object):
        def __init__(self, *a, **k):
            if raises:
                raise RuntimeError('simulated channels.json read failure')
        def getChannels(self): return list(channels_list or [])
        def getImports(self):  return list(imports_list or [])
    monkeypatch.setattr(channels_module, 'Channels', _Mock)


def _items():
    return [{'id': CUSTOM, 'display-name': [('Custom', '')]},
            {'id': TVE, 'display-name': [('TVE', '')]},
            {'id': OTHER, 'display-name': [('Other', '')]},
            {'id': REC, 'display-name': [('Rec', '')]},
            {'id': 'foreign', 'display-name': [('Not ours', '')]}]


def _old_programmes():
    ten_days_ago = NOW - 10 * 86400
    return [_prog(CUSTOM, ten_days_ago), _prog(TVE, ten_days_ago, link='plugin://x/?id=1'),
            _prog(OTHER, ten_days_ago), _prog(TVE, NOW + 3600), _prog('foreign', NOW + 3600)]


# ---------------------------------------------------------------- unknown channel list


@pytest.mark.parametrize('how', ['raises', 'empty'])
def test_unknown_channel_list_keeps_every_entry_of_ours(monkeypatch, how):
    if how == 'raises':
        _channels(monkeypatch, raises=True)
    else:
        _channels(monkeypatch, channels_list=[], imports_list=[])
    x = _Logged()
    channels, recordings = x.cleanSelf(_items(), 'id')
    assert {c['id'] for c in channels} == {CUSTOM, TVE, OTHER}
    assert {r['id'] for r in recordings} == {REC}
    progs = x.cleanSelf(_old_programmes(), 'channel')
    kept = [(p['channel'], p['stop']) for p in progs]
    assert len(kept) == 4, 'import slices, their kept history and Custom entries all stay (no age trim)'
    assert ('foreign', _ts(NOW + 3600)) not in kept, 'entries that are not ours still go'
    assert [m for m, lvl in x.lines if 'channel list unavailable' in m]


def test_unknown_channel_list_logs_an_error(monkeypatch):
    import xbmc
    _channels(monkeypatch, raises=True)
    x = _Logged()
    x.cleanSelf(_old_programmes(), 'channel')
    levels = [lvl for m, lvl in x.lines if 'channel list unavailable' in m or 'lookup failed' in m]
    assert levels and set(levels) == {xbmc.LOGERROR}


def test_unknown_channel_list_skips_the_age_trim(monkeypatch):
    _channels(monkeypatch, raises=True)
    x = _Logged()
    called = []
    x.cleanProgrammes = lambda progs, keep_days=None: called.append(1) or progs
    x.cleanSelf(_old_programmes(), 'channel')
    assert called == []


# ---------------------------------------------------------------- normal channel list


def test_normal_channel_list_unchanged(monkeypatch):
    _channels(monkeypatch,
              channels_list=[{'id': CUSTOM, 'number': 1, 'enabled': True},
                             {'id': TVE, 'number': 2, 'type': 'import', 'enabled': True,
                              'catchup': 'default', 'catchup-source': '', 'catchup-days': '5'}],
              imports_list=[{'id': 'mp'}])
    x = _Logged()
    channels, recordings = x.cleanSelf(_items(), 'id')
    assert {c['id'] for c in channels} == {CUSTOM, TVE}, 'removed import and foreign ids dropped'
    assert {r['id'] for r in recordings} == {REC}
    progs = x.cleanSelf(_old_programmes(), 'channel')
    assert [(p['channel'], p['stop']) for p in progs] == [(TVE, _ts(NOW + 3600))], \
        'old programmes trimmed (not link-based), other import and foreign entries dropped'
    assert not [m for m, lvl in x.lines if 'channel list unavailable' in m]


def test_disabled_channel_still_dropped(monkeypatch):
    _channels(monkeypatch, channels_list=[{'id': TVE, 'number': 2, 'enabled': False}],
              imports_list=[{'id': 'mp'}])
    channels, _ = _Logged().cleanSelf(_items(), 'id')
    assert channels == []


def test_imports_only_list_is_known(monkeypatch):
    """An import list without channels is a readable channels.json: the
    cross-reference applies (no channel is live yet)."""
    _channels(monkeypatch, channels_list=[], imports_list=[{'id': 'mp'}])
    channels, _ = _Logged().cleanSelf(_items(), 'id')
    assert channels == []


# ---------------------------------------------------------------- replay windows, one record at a time


def test_replay_windows_computed_one_record_at_a_time(monkeypatch):
    linked = {'id': TVE, 'number': 2, 'type': 'import', 'enabled': True,
              'catchup': 'default', 'catchup-source': '{catchup-id}', 'catchup-days': '7'}
    odd = {'id': 'ODD@mp', 'number': 3, 'type': 'import', 'enabled': True}
    _channels(monkeypatch, channels_list=[odd, linked], imports_list=[{'id': 'mp'}])
    real = replay_helpers.link_replay_days

    def flaky(records):
        if records and records[0] is odd:
            raise ValueError('odd record')
        return real(records)
    monkeypatch.setattr(replay_helpers, 'link_replay_days', flaky)
    x = _Logged()
    three_days_ago = NOW - 3 * 86400
    progs = x.cleanSelf([_prog(TVE, three_days_ago, link='plugin://x/?id=9'),
                         _prog(TVE, three_days_ago)], 'channel')
    assert [p['catchup-id'] for p in progs] == ['plugin://x/?id=9'], \
        'the odd record did not break the lookup: linked history kept, unlinked trimmed'
    assert [m for m, lvl in x.lines if 'replay window skipped' in m]
