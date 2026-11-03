"""imports.59 (S11) — the guide self-heal counts only shows that haven't ended.

The imports.56 self-heal force-syncs an import whose guide is missing from
the guide data about to be written. With up to 7 days of linked replay
history now kept, a slice holding only past shows would hide a missing
current guide. _guideNeedsSelfHeal now counts a programme as present only if
its stop is in the future; an unreadable stop still counts (as before).
"""
import time

import pytest

import imports as imports_mod
from imports import Imports


IID = 'imp'
NOW = int(time.time())


def _ts(epoch):
    return time.strftime('%Y%m%d%H%M%S', time.gmtime(epoch)) + ' +0000'


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(imports_mod, 'CACHE_LOC', str(tmp_path))
    monkeypatch.setattr(imports_mod, '_isDiskMissing', lambda iid, scope: False)
    imports_mod._GUIDE_SELFHEAL_LAST.clear()
    (tmp_path / ('epg_%s.xml' % IID)).write_bytes(b'<?xml version="1.0"?><tv>' + b' ' * 200 + b'</tv>')
    yield
    imports_mod._GUIDE_SELFHEAL_LAST.clear()


class _X:
    def __init__(self, programmes):
        self.XMLTVDATA = {'channels': [], 'programmes': list(programmes), 'recordings': []}


def _cfg():
    return {'id': IID, 'enabled': True, 'epg_url': 'https://example.invalid/guide.xml'}


def _channels():
    return [{'id': 'c1@imp', 'import_source': IID, 'type': 'import', 'enabled': True,
             'is_orphan': False, 'unallocated': False}]


def _prog(stop, channel='c1@imp'):
    return {'channel': channel, 'start': _ts(stop - 3600), 'stop': _ts(stop), 'title': [('t', '')]}


def _needs(programmes):
    return Imports(xmltv=_X(programmes))._guideNeedsSelfHeal(_cfg(), _channels())


def test_past_only_slice_triggers_the_self_heal():
    past = [_prog(NOW - 3 * 86400), _prog(NOW - 600)]
    assert _needs(past) is True


def test_a_future_show_still_counts_as_guide_present():
    assert _needs([_prog(NOW - 3 * 86400), _prog(NOW + 600)]) is False


@pytest.mark.parametrize('stop', [None, '', 'garbage', 12345])
def test_unreadable_stop_still_counts(stop):
    prog = {'channel': 'c1@imp', 'start': _ts(NOW - 3 * 86400), 'stop': stop, 'title': [('t', '')]}
    assert _needs([prog]) is False


def test_other_imports_dont_count():
    assert _needs([_prog(NOW + 600, channel='c1@other')]) is True


def test_self_heal_throttle_still_applies():
    past = [_prog(NOW - 600)]
    assert _needs(past) is True
    assert _needs(past) is False, 'once per GUIDE_SELFHEAL_MIN_INTERVAL_SEC'
