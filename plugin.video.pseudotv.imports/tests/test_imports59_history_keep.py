"""imports.59 (S10.4) — the guide-load age trim keeps linked replay history.

Movistar+'s guide export starts at its own day's 00:00, so shows from
yesterday and older exist only in our pseudotv.xml. Every writer loads that
file through XMLTVS.cleanSelf -> cleanProgrammes, whose Min_Days trim would
delete them. cleanProgrammes(programmes, keep_days) now keeps a programme of
a live link-replay channel that carries a catchup-id while its stop is within
that channel's replay window (catchup-days, at most 7); everything else
follows the Min_Days rule as before.

Also (R2C-8): the citem/holiday decode (Globals._decodePlot, which unpickles
the description payload) runs only for '@PseudoTV_Live' programmes -- only
those carry it. Valid data behaves the same.
"""
import time

import pytest

import globals as globals_mod
from fileaccess import FileAccess
from xmltvs import XMLTVS


CUSTOM = 'c' * 32 + '@PseudoTV_Live'
TVE    = 'TVE@mp'
LA2    = 'LA2@mp'
NOW    = int(time.time())
DAY    = 86400


def _ts(epoch):
    return time.strftime('%Y%m%d%H%M%S', time.gmtime(epoch)) + ' +0000'


def _prog(ch, stop, link='', desc=None):
    p = {'channel': ch, 'start': _ts(stop - 3600), 'stop': _ts(stop), 'catchup-id': link,
         'title': [('t', '')]}
    if desc is not None:
        p['desc'] = [(desc, '')]
    return p


def _x():
    x = XMLTVS.__new__(XMLTVS)
    x.m3u = None
    x.XMLTVDATA = {}
    return x


def _kept(progs, keep_days):
    return [(p['channel'], p['stop'], p['catchup-id']) for p in _x().cleanProgrammes(progs, keep_days=keep_days)]


def test_linked_history_kept_inside_window_rest_follows_min_days():
    progs = [_prog(TVE, NOW - 3 * DAY, link='u1'),      # linked, 3 days: kept
             _prog(TVE, NOW - 3 * DAY),                 # unlinked, same age: trimmed
             _prog(TVE, NOW - 8 * DAY, link='u2'),      # linked, older than 7 days: trimmed
             _prog(CUSTOM, NOW - 3 * DAY, link='plugin://plugin.video.pseudotv.imports/?x'),  # Custom: trimmed
             _prog(LA2, NOW - 3 * DAY, link='u3'),      # linked, channel not link-replay: trimmed
             _prog(TVE, NOW + 3600)]                    # future: kept
    assert _kept(progs, {TVE: 7}) == [(TVE, _ts(NOW - 3 * DAY), 'u1'), (TVE, _ts(NOW + 3600), '')]


def test_each_channel_uses_its_own_window():
    progs = [_prog(TVE, NOW - 4 * DAY, link='a'), _prog(LA2, NOW - 4 * DAY, link='b'),
             _prog(LA2, NOW - 2 * DAY, link='c')]
    assert [k[2] for k in _kept(progs, {TVE: 7, LA2: 3})] == ['a', 'c']


def test_window_boundary_is_inclusive():
    progs = [_prog(TVE, NOW - 3 * DAY + 60, link='inside'), _prog(TVE, NOW - 3 * DAY - 60, link='outside')]
    assert [k[2] for k in _kept(progs, {TVE: 3})] == ['inside']


def test_without_keep_days_behaviour_is_unchanged():
    progs = [_prog(TVE, NOW - 3 * DAY, link='u1'), _prog(TVE, NOW + 3600, link='u2')]
    assert _kept(progs, None) == _kept(progs, {}) == [(TVE, _ts(NOW + 3600), 'u2')]
    assert [p['catchup-id'] for p in _x().cleanProgrammes(progs)] == ['u2']


@pytest.mark.parametrize('link', ['', '   ', None])
def test_blank_link_is_not_linked_history(link):
    p = _prog(TVE, NOW - 3 * DAY)
    p['catchup-id'] = link
    assert _x().cleanProgrammes([p], keep_days={TVE: 7}) == []


def test_unparseable_stop_still_kept():
    p = _prog(TVE, NOW - 3 * DAY, link='u1')
    p['stop'] = 'garbage'
    assert _x().cleanProgrammes([p], keep_days={TVE: 7}) == [p]


# ---------------------------------------------------------------- decode restriction (R2C-8)


def _payload(holiday_name):
    citem = {'id': 'x', 'rules': {800: {'values': {0: [{'holiday': {'name': holiday_name}}]}}}}
    return globals_mod.Globals._encodePlot('Plot', {'citem': citem})


def test_imported_programme_payload_is_not_decoded(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError('imported programme description must not be decoded')
    monkeypatch.setattr(FileAccess, '_decodeString', staticmethod(refuse))
    p = _prog(TVE, NOW + 3600, desc=_payload('Some Holiday'))
    assert _x().cleanProgrammes([p], keep_days={TVE: 7}) == [p]


def test_custom_holiday_filtering_unchanged(monkeypatch):
    decoded = []
    real = FileAccess._decodeString

    def spy(*a, **k):
        decoded.append(1)
        return real(*a, **k)
    monkeypatch.setattr(FileAccess, '_decodeString', staticmethod(spy))
    expired = _prog(CUSTOM, NOW + 3600, desc=_payload('No Such Holiday 1999'))
    plain = _prog(CUSTOM, NOW + 3600, desc='Plain plot')
    imported = _prog(TVE, NOW + 3600, desc=_payload('No Such Holiday 1999'))
    out = _x().cleanProgrammes([expired, plain, imported])
    assert out == [plain, imported], 'expired-holiday Custom programme removed; imported one never decoded'
    assert decoded == [1], 'only the Custom payload was decoded'


# ---------------------------------------------------------------- through cleanSelf


def _channels(monkeypatch, records):
    import channels as channels_module

    class _Mock(object):
        def __init__(self, *a, **k): pass
        def getChannels(self): return list(records)
        def getImports(self):  return [{'id': 'mp'}]
    monkeypatch.setattr(channels_module, 'Channels', _Mock)


def _record(**over):
    rec = {'id': TVE, 'number': 1, 'type': 'import', 'enabled': True, 'is_orphan': False,
           'unallocated': False, 'catchup': 'default', 'catchup-source': '{catchup-id}', 'catchup-days': '7'}
    rec.update(over)
    return rec


def test_cleanself_keeps_linked_history_of_link_replay_channels(monkeypatch):
    _channels(monkeypatch, [_record()])
    progs = [_prog(TVE, NOW - 5 * DAY, link='u1'), _prog(TVE, NOW - 5 * DAY)]
    out = _x().cleanSelf(progs, 'channel')
    assert [p['catchup-id'] for p in out] == ['u1']


def test_cleanself_old_catchup_row_trims_as_before(monkeypatch):
    """Rollback stage 1 (catch-up not link-based): no replay window, the
    Min_Days trim applies to every programme as before."""
    _channels(monkeypatch, [_record(**{'catchup-source': '', 'catchup-days': '5'})])
    out = _x().cleanSelf([_prog(TVE, NOW - 5 * DAY, link='u1')], 'channel')
    assert out == []


# ---------------------------------------------------------------- writer round trip + _saveLocked


EXPORT_STYLE = (
    b'<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE tv SYSTEM "xmltv.dtd"><tv>'
    b'<channel id="MPLUS@mp"><display-name>Movistar Plus</display-name><icon src="http://logo/mplus.png"/></channel>'
    b'<programme start="20990101000000 +0000" stop="20990101010000 +0000" channel="MPLUS@mp" '
    b'catchup-id="plugin://plugin.video.movistarplus/?action=play&amp;stype=u7d&amp;id=1001">'
    b'<title>Informe Robinson</title><sub-title>Episodio 1</sub-title></programme>'
    b'<programme start="20990101010000 +0000" stop="20990101020000 +0000" channel="MPLUS@mp" '
    b'catchup-id="plugin://plugin.video.movistarplus/?action=play&amp;stype=u7d&amp;id=1002">'
    b'<title>Noticias</title><sub-title></sub-title><desc>Rich &lt;desc&gt;</desc></programme>'
    b'</tv>')


def test_export_style_links_round_trip_byte_identical():
    import io
    import renderers
    import xmltv
    chans, progs = [], []
    for kind, item in xmltv.parseExternalSource(EXPORT_STYLE):
        (chans if kind == 'channel' else progs).append(item)
    assert progs[0]['catchup-id'] == 'plugin://plugin.video.movistarplus/?action=play&stype=u7d&id=1001'
    data = {'date': '20990101000000'}
    gen1 = renderers.render_xmltv({'data': data, 'channels': chans, 'recordings': [], 'programmes': progs})
    assert b'catchup-id="plugin://plugin.video.movistarplus/?action=play&amp;stype=u7d&amp;id=1001"' in gen1
    reread = xmltv.read_programmes(io.BytesIO(gen1))
    rechans = xmltv.read_channels(io.BytesIO(gen1))
    assert [p['catchup-id'] for p in reread] == [p['catchup-id'] for p in progs]
    gen2 = renderers.render_xmltv({'data': data, 'channels': rechans, 'recordings': [], 'programmes': reread})
    assert gen2 == gen1, 'writer -> reader -> writer is byte-identical for export-style entries'


def test_savelocked_keeps_kept_programmes_and_their_channel():
    x = _x()
    x.writable = False
    x.XMLTVFile = 'unused.xml'
    old = [_prog(TVE, NOW - 5 * DAY, link='u1'), _prog(TVE, NOW - 4 * DAY, link='u2')]
    x.XMLTVDATA = {'channels': [{'id': TVE, 'display-name': [('TVE', '')]}], 'recordings': [],
                   'programmes': list(old) + [_prog(TVE, NOW + 3600, link='u3')]}
    assert x._saveLocked() is False                     # not writable: nothing written ...
    assert [p['catchup-id'] for p in x.XMLTVDATA['programmes']] == ['u1', 'u2', 'u3']   # ... nothing dropped
    assert [c['id'] for c in x.XMLTVDATA['channels']] == [TVE]
