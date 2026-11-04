"""imports.60 (D1): file lengths are no longer written to Kodi's library.

jsonrpc.queDuration queued "duration write-backs" (Store_Duration) without a
"method", so Kodi rejected every one ("JSONRPC: Failed to parse", 1,388 lines
in one day's kodi.log) and a well-formed version would have sent
resume {position 0, total N}, which CLEARS the item's resume point. It is
removed; lengths stay in the add-on's own caches exactly as before, and the
Store_Duration switch is hidden (level 4).

These tests run the real jsonrpc / videoparser under the Kodi test stand-ins,
with the MKV reader and cache replaced. They call every function with the
argument shapes its real callers use.
"""
import inspect
import os
import re
import types
import xml.etree.ElementTree as ET

import pytest

import jsonrpc
import videoparser
from jsonrpc import JSONRPC
from parsers import MKVParser

HERE      = os.path.dirname(os.path.abspath(__file__))
ADDON     = os.path.dirname(HERE)
ADDON_LIB = os.path.join(ADDON, 'resources', 'lib')
FILM      = '/mnt/x/Film (1972)/Film (1972).mkv'
TRAILER   = '/mnt/x/Film (1972)/Film (1972)-trailer.mkv'


class FakeCache(object):
    def __init__(self): self.d = {}
    def get(self, name, checksum=None): return self.d.get(name)
    def set(self, name, value, checksum=None, expiration=None):
        self.d[name] = value
        return value


@pytest.fixture
def rig(monkeypatch):
    probes = {}
    monkeypatch.setattr(MKVParser.MKVParser, 'determineLength', lambda self, fn: probes.get(fn, 0))
    monkeypatch.setattr(videoparser, 'EXTERNAL_PARSER', [])
    monkeypatch.setattr(videoparser.FileAccess, 'exists', staticmethod(lambda p: True))
    queued, errors = [], []
    svc = types.SimpleNamespace(jsonQue=set(), _que=lambda *a, **k: queued.append(a))
    rpc = JSONRPC(service=svc)
    monkeypatch.setattr(rpc, 'cache', FakeCache())
    monkeypatch.setattr(rpc, 'log', lambda msg, level=0: errors.append(msg) if level >= 3 else None)
    monkeypatch.setattr(rpc, 'sendJSON', lambda *a, **k: pytest.fail('nothing may be sent to Kodi'))
    return types.SimpleNamespace(rpc=rpc, svc=svc, queued=queued, errors=errors, probes=probes)


@pytest.fixture
def store_duration_on(monkeypatch):
    """The live configuration (Store_Duration=true). Before imports.60 the setting
    was frozen into the `save` defaults when the service started, so set every
    `save` default to True; the new code must ignore it."""
    for name in ('_setRuntime', '_setDuration', 'getDuration', '_JSONRPC__parseDuration'):
        func  = getattr(JSONRPC, name)
        names = list(inspect.signature(func).parameters)
        if 'save' not in names: continue
        defaults = list(func.__defaults__)
        defaults[names[-len(defaults):].index('save')] = True
        monkeypatch.setattr(func, '__defaults__', tuple(defaults))


def _md5(path): return jsonrpc.FileAccess._getMD5(path)


def _nothing_left_the_addon(rig):
    assert rig.svc.jsonQue == set() and rig.queued == [] and rig.errors == []


def test_write_back_functions_are_gone():
    assert not hasattr(JSONRPC, 'queDuration')
    assert not hasattr(JSONRPC, 'quePlaycount')


@pytest.mark.parametrize('save', [True, False])
def test_builder_call_measures_caches_and_writes_nothing(rig, store_duration_on, save):
    """builder.py getDuration(file, item, self.accurateDuration, self.saveDuration)."""
    rig.probes[FILM] = 4763
    item = {'file': FILM, 'type': 'movie', 'id': 7, 'runtime': 6300}
    assert rig.rpc.getDuration(FILM, item, True, save) == 4763
    assert rig.rpc.cache.d['getDuration.%s' % _md5(FILM)] == 4763
    _nothing_left_the_addon(rig)


def test_trailer_call_logs_no_error(rig, store_duration_on):
    """builder.py setTrailers: getDuration(file, accurate=True, save=False). With
    the old frozen default, videoparser's _setDuration call reached queDuration
    and logged "'NoneType' object has no attribute 'get'" for every trailer."""
    rig.probes[TRAILER] = 150
    assert rig.rpc.getDuration(TRAILER, accurate=True, save=False) == 150
    _nothing_left_the_addon(rig)


def test_setRuntime_caches_only(rig, store_duration_on):
    """services.py _onPlay: _setRuntime(fitem, runtime); the old 3-argument form still works."""
    rig.rpc._setRuntime({'file': FILM, 'type': 'episode', 'id': 5}, 2661)
    assert rig.rpc.cache.d['getRuntime.%s' % _md5(FILM)] == 2661
    rig.rpc._setRuntime({'file': FILM, 'type': 'episode', 'id': 5}, 2662.4, True)
    assert rig.rpc.cache.d['getRuntime.%s' % _md5(FILM)] == 2662
    _nothing_left_the_addon(rig)


def test_setDuration_caches_only(rig, store_duration_on):
    """videoparser.py: _setDuration(filename, fileitem, round(duration))."""
    assert rig.rpc._setDuration(FILM, {'file': FILM, 'type': 'movie', 'id': 7}, 4763.4) == 4763
    assert rig.rpc._setDuration(FILM, {'file': FILM, 'type': 'movie', 'id': 7}, 4763, True) == 4763
    assert rig.rpc.cache.d['getDuration.%s' % _md5(FILM)] == 4763
    _nothing_left_the_addon(rig)


def test_cached_length_is_reused_without_measuring(rig, monkeypatch):
    rig.probes[FILM] = 4763
    assert rig.rpc.getDuration(FILM, {'file': FILM, 'runtime': 6300}, True) == 4763
    monkeypatch.setattr(MKVParser.MKVParser, 'determineLength', lambda self, fn: pytest.fail('measured twice'))
    assert rig.rpc.getDuration(FILM, {'file': FILM, 'runtime': 6300}, True) == 4763


def test_duration_type_is_read_when_called(rig, monkeypatch):
    rig.probes[FILM] = 4763
    item = {'file': FILM, 'type': 'movie', 'id': 7, 'runtime': 6300}
    monkeypatch.setattr(jsonrpc.SETTINGS, 'getSettingInt', lambda key: 0)
    assert rig.rpc.getDuration(FILM, dict(item)) == 6300
    assert rig.rpc.getTotDuration([dict(item)]) == 6300
    monkeypatch.setattr(jsonrpc.SETTINGS, 'getSettingInt', lambda key: 1)
    assert rig.rpc.getDuration(FILM, dict(item)) == 4763
    assert rig.rpc.getTotDuration([dict(item), dict(item)]) == 2 * 4763


@pytest.mark.parametrize('name, params', [
    ('getDuration', ('item', 'accurate', 'save')), ('getTotDuration', ('items', 'accurate')),
    ('_setRuntime', ('item', 'save')), ('_setDuration', ('item', 'save')), ('_getRuntime', ('item',)),
])
def test_defaults_are_not_frozen_at_service_start(name, params):
    sig = inspect.signature(getattr(JSONRPC, name))
    assert all(sig.parameters[p].default is None for p in params)


def test_positional_slots_kept_for_callers():
    assert list(inspect.signature(JSONRPC._setRuntime).parameters) == ['self', 'item', 'runtime', 'save']
    assert list(inspect.signature(JSONRPC._setDuration).parameters) == ['self', 'path', 'item', 'duration', 'save']
    assert list(inspect.signature(JSONRPC.getDuration).parameters) == ['self', 'path', 'item', 'accurate', 'save']
    assert list(inspect.signature(JSONRPC._JSONRPC__parseDuration).parameters) == ['self', 'runtime', 'path', 'item', 'accurate']


def test_getFileDetails_cache_switch(monkeypatch):
    rpc = JSONRPC(service=types.SimpleNamespace())
    calls = []
    monkeypatch.setattr(rpc, 'cacheJSON', lambda param, *a, **k: calls.append(('cache', param)) or {'result': {}})
    monkeypatch.setattr(rpc, 'sendJSON', lambda param, delay=True: calls.append(('send', param, delay)) or {'result': {}})
    rpc.getFileDetails(FILM)                                                 # VFSParser: cached, unchanged
    rpc.getFileDetails(FILM, 'video', ['playcount', 'lastplayed', 'resume'], cache=False)
    rpc.getFileDetails(FILM, 'video', ['playcount', 'lastplayed', 'resume'], cache=False, delay=False)
    assert [c[0] for c in calls] == ['cache', 'send', 'send']
    assert calls[0][1] == {'method': 'Files.GetFileDetails', 'params': {'file': FILM, 'media': 'video', 'properties': ['duration', 'runtime']}}
    assert calls[1][1]['params']['properties'] == ['playcount', 'lastplayed', 'resume']
    assert [c[2] for c in calls[1:]] == [True, False]


@pytest.mark.parametrize('kwargs, waits', [({}, [1.0]), ({'delay': True}, [1.0]), ({'delay': False}, [])])
def test_sendJSON_pauses_unless_told_not_to(monkeypatch, kwargs, waits):
    """RPC_Delay (1 s here) after each reply; the Rollback_Watched reads skip it so the
    tracker can check the player right after Kodi answers."""
    seen = []
    svc = types.SimpleNamespace(monitor=types.SimpleNamespace(waitForAbort=lambda t: seen.append(t) or False))
    rpc = JSONRPC(service=svc)
    monkeypatch.setattr(jsonrpc.BUILTIN, 'executeJSONRPC', lambda request: '{"id": 1, "jsonrpc": "2.0", "result": "OK"}')
    monkeypatch.setattr(jsonrpc.SETTINGS, 'getSettingInt', lambda key: {'RPC_Delay': 1}[key])
    assert rpc.sendJSON({'method': 'JSONRPC.Ping'}, **kwargs)['result'] == 'OK'
    assert seen == waits


# --- the switch, the rule and the strings ------------------------------------------

def _settings_xml():
    return ET.parse(os.path.join(ADDON, 'resources', 'settings.xml')).getroot()


def test_store_duration_is_hidden_but_still_defined():
    node = next(s for s in _settings_xml().iter('setting') if s.get('id') == 'Store_Duration')
    assert node.findtext('level') == '4'                     # Kodi: 4 = internal, never shown
    assert node.get('type') == 'boolean' and node.findtext('default') == 'false'


def test_rule_500_has_one_description_per_option():
    from rules import DurationOptions
    rule = DurationOptions()
    assert len(rule.optionLabels) == len(rule.optionValues) == len(rule.optionDescriptions) == 3
    src = open(os.path.join(ADDON_LIB, 'rules.py'), encoding='utf-8').read()
    assert 'LANGUAGE(33015),LANGUAGE(33049)' not in src


def test_strings_say_no_longer_used():
    po = open(os.path.join(ADDON, 'resources', 'language', 'resource.language.en_gb', 'strings.po'), encoding='utf-8').read()
    m30052 = re.search(r'msgctxt "#30052"\nmsgid "([^"]*)"', po).group(1)
    m33052 = re.search(r'msgctxt "#33052"\nmsgid "([^"]*)"', po).group(1)
    assert 'no longer used' in m30052.lower() and 'no longer used' in m33052.lower()
    assert 'library' in m33052 and 'cache' in m33052


def _src(name):
    return open(os.path.join(ADDON_LIB, name), encoding='utf-8').read()


def test_player_no_longer_reads_store_duration():
    services = _src('services.py')
    assert 'saveDuration' not in services and 'Store_Duration' not in services


def test_no_code_defines_or_calls_the_removed_functions():
    for root, _, files in os.walk(ADDON_LIB):
        for name in files:
            if not name.endswith('.py'): continue
            src = open(os.path.join(root, name), encoding='utf-8').read()
            assert not re.search(r'def (queDuration|quePlaycount)\b|\.(queDuration|quePlaycount)\(', src), name


def test_builder_keeps_saveDuration_for_rule_500():
    """Rule 500 saves/restores builder.saveDuration; removing it would raise for
    any channel with that rule."""
    assert re.search(r"self\.saveDuration\s*=\s*SETTINGS\.getSettingBool\('Store_Duration'\)", _src('builder.py'))
