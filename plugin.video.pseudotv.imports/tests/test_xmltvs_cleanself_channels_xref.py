"""Unit tests for XMLTVS.cleanSelf channels.json cross-reference
(imports.15 / C#10 Follow-up E).

Operator-reported bug: deleting a Custom channel via the UI left
orphan <channel> + <programme> entries in pseudotv.xml indefinitely.
Root cause: M3U.cleanSelf calls M3U._verify which cross-references
Channels().getChannels(); XMLTVS.cleanSelf had no equivalent check.

The fix adds a channels.json id-set lookup to cleanSelf so orphan
ids get dropped on load (symmetric to the M3U side). The lookup is
gated by `if live_channel_ids is not None` so a Channels() failure
falls open to today's namespace+length filter (no regression).

Tests exercise cleanSelf directly via a stub `self`, monkeypatching
the `Channels` class in the channels module to control the live id
set. This avoids needing a real channels.json on disk for tests.

Plan: /home/madalone/.claude/plans/misty-shimmying-liskov.md.
"""
import pytest

from xmltvs import XMLTVS


class _StubXMLTVS(object):
    """Minimal duck-typed surface for cleanSelf to call against.
    Only `log`, `sortChannels`, `sortProgrammes`, `cleanProgrammes`,
    `cleanStations`, `cleanRecordings` are referenced by cleanSelf's
    body. None of them need real behavior for these tests — we only
    assert on the filtered output, not on sort order or programme
    cleanup."""
    def log(self, msg, level=None): pass
    def sortChannels(self, lst): return list(lst)
    def sortProgrammes(self, lst): return list(lst)
    def cleanProgrammes(self, lst): return list(lst)
    def cleanStations(self, lst): return list(lst)
    def cleanRecordings(self, lst): return list(lst)


@pytest.fixture
def stub():
    return _StubXMLTVS()


def _mock_channels(monkeypatch, channels_list, imports_list=None):
    """Monkeypatch the `Channels` class in the channels module so
    cleanSelf's local `from channels import Channels as _C` picks
    up our stub instead of the real (disk-loading) Channels.

    Returns a controller that lets tests toggle behavior (e.g.
    raise on construction)."""
    if imports_list is None:
        imports_list = []
    import channels as channels_module

    class _MockChannels(object):
        def __init__(self, *args, **kwargs): pass
        def getChannels(self): return channels_list
        def getImports(self): return imports_list

    monkeypatch.setattr(channels_module, 'Channels', _MockChannels)


# ---------------------------------------------------------------- 1. drops orphan


def test_cleanself_drops_channel_not_in_channels_json(stub, monkeypatch):
    """A Custom channel id whose entry was deleted from channels.json
    (e.g. operator clicked Delete in the UI) must be dropped by
    cleanSelf on the next load. This is the fix for the operator-
    reported Five Fingers of Death orphan bug."""
    # channels.json has Channel A only — Channel B (the deleted one)
    # is NOT in the list.
    channel_a_id = 'a' * 32 + '@PseudoTV_Live'
    deleted_b_id = 'b' * 32 + '@PseudoTV_Live'

    _mock_channels(monkeypatch, channels_list=[{'id': channel_a_id}])

    items = [
        {'id': channel_a_id,  'display-name': [('Channel A', 'en')]},
        {'id': deleted_b_id,  'display-name': [('Five Fingers of Death', 'en')]},
    ]
    channels, recordings = XMLTVS.cleanSelf(stub, items=items, key='id',
                                            slug='@PseudoTV_Live')
    kept_ids = {c.get('id') for c in channels}
    assert channel_a_id in kept_ids, 'live channel must be kept'
    assert deleted_b_id not in kept_ids, (
        'orphan channel (not in channels.json) must be dropped — '
        'this is the fix for the operator-reported XML orphan bug')
    assert recordings == [], 'no recordings expected in this input'


# ---------------------------------------------------------------- 2. keeps live


def test_cleanself_keeps_channel_in_channels_json(stub, monkeypatch):
    """Sanity check: a Custom channel whose id IS in channels.json
    must survive cleanSelf. Confirms the cross-reference doesn't
    drop legitimate live entries."""
    channel_id = 'c' * 32 + '@PseudoTV_Live'
    _mock_channels(monkeypatch, channels_list=[{'id': channel_id}])

    items = [{'id': channel_id, 'display-name': [('Channel C', 'en')]}]
    channels, recordings = XMLTVS.cleanSelf(stub, items=items, key='id',
                                            slug='@PseudoTV_Live')
    kept_ids = {c.get('id') for c in channels}
    assert channel_id in kept_ids, 'live channel must survive cleanSelf'


# ---------------------------------------------------------------- 3. fall-open


def test_cleanself_falls_open_on_channels_failure(stub, monkeypatch):
    """If Channels() construction raises, cleanSelf must fall open
    to the pre-imports.15 namespace+length filter — i.e., keep
    Custom channels by namespace check alone (live_channel_ids = None
    disables the cross-reference). This preserves today's behavior
    for test scaffolds and any transient channels.json read failure."""
    import channels as channels_module

    class _BrokenChannels(object):
        def __init__(self, *args, **kwargs):
            raise RuntimeError('simulated channels.json read failure')

    monkeypatch.setattr(channels_module, 'Channels', _BrokenChannels)

    channel_id = 'd' * 32 + '@PseudoTV_Live'
    items = [{'id': channel_id, 'display-name': [('Channel D', 'en')]}]
    # Despite the Channels() failure, cleanSelf must not crash and
    # must keep the namespace-valid channel.
    channels, recordings = XMLTVS.cleanSelf(stub, items=items, key='id',
                                            slug='@PseudoTV_Live')
    kept_ids = {c.get('id') for c in channels}
    assert channel_id in kept_ids, (
        'Channels() failure must fall open: keep entries that pass '
        'the namespace+length check (today\'s behavior preserved)')


# ---------------------------------------------------------------- 4. recording untouched


def test_cleanself_recording_not_affected_by_cross_reference(stub, monkeypatch):
    """Recordings (16-char @PseudoTV_Live ids) are NOT in channels.json —
    they live only in M3U/XML files. The cross-reference must not
    apply to recordings; _id_ok_recording is unchanged in imports.15."""
    recording_id = 'e' * 16 + '@PseudoTV_Live'  # 16-char recording
    # channels.json is EMPTY (no Customs registered)
    _mock_channels(monkeypatch, channels_list=[])

    items = [{'id': recording_id, 'display-name': [('Recording E', 'en')]}]
    channels, recordings = XMLTVS.cleanSelf(stub, items=items, key='id',
                                            slug='@PseudoTV_Live')
    kept_recording_ids = {r.get('id') for r in recordings}
    assert recording_id in kept_recording_ids, (
        'recording must survive even when channels.json is empty — '
        'recordings are not tracked in channels.json (managed by '
        'context_record.py via M3U/XML directly)')
