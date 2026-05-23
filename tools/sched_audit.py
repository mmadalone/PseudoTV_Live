#!/usr/bin/env python3
# Read-only audit of pseudotv.xml: EPG slot length vs actual media file duration.
import xml.etree.ElementTree as ET
import re, base64, zlib, urllib.parse, pickle, collections

XML = '/home/madalone/.kodi/userdata/addon_data/plugin.video.pseudotv.imports/cache/pseudotv.xml'
COLOR = re.compile(r'\[COLOR item="([^"]+)"\]')

def decode_fitem(desc):
    if not desc:
        return None
    m = COLOR.search(desc)
    if not m:
        return None
    try:
        return pickle.loads(zlib.decompress(base64.b64decode(urllib.parse.unquote(m.group(1)))))
    except Exception:
        return None

def file_dur(fitem):
    try:
        v = (fitem.get('streamdetails') or {}).get('video') or []
        if v and v[0].get('duration'):
            return int(v[0]['duration'])
    except Exception:
        pass
    try:
        if fitem.get('runtime'):
            return int(fitem['runtime'])
    except Exception:
        pass
    return None

total = decoded = nofitem = nodur = 0
per_chan = collections.defaultdict(lambda: {'n': 0, 'loop': 0, 'early': 0, 'worst': 0, 'worst_show': ''})
loopers = []

for ev, el in ET.iterparse(XML, events=('end',)):
    if el.tag == 'channel':
        el.clear(); continue
    if el.tag != 'programme':
        continue
    total += 1
    chan = el.get('channel', '?')
    le = el.find('length')
    slot = int(le.text) if (le is not None and le.text and le.text.strip().isdigit()) else None
    te = el.find('title')
    title = te.text if (te is not None and te.text) else '?'
    de = el.find('desc')
    fitem = decode_fitem(de.text if de is not None else None)
    el.clear()
    if fitem is None:
        nofitem += 1; continue
    decoded += 1
    fd = file_dur(fitem)
    if slot is None or fd is None or fd <= 0:
        nodur += 1; continue
    cname = (fitem.get('citem') or {}).get('name') or chan
    c = per_chan[cname]; c['n'] += 1
    gap = slot - fd
    if gap >= 10:
        c['loop'] += 1
        loopers.append((gap, cname, title, slot, fd))
    if gap <= -10:
        c['early'] += 1
    if gap > c['worst']:
        c['worst'] = gap; c['worst_show'] = title

print('=== PSEUDOTV SCHEDULE AUDIT (pseudotv.xml) ===')
print('programmes: %d | decoded: %d | undecodable: %d | no-duration: %d' % (total, decoded, nofitem, nodur))

buck = collections.Counter()
for gap, *_ in loopers:
    if gap >= 120: buck['>=120s'] += 1
    elif gap >= 60: buck['60-119s'] += 1
    elif gap >= 30: buck['30-59s'] += 1
    else: buck['10-29s'] += 1
print()
print('LOOP-RISK (EPG slot >=10s LONGER than the video file):  %d programmes' % len(loopers))
for k in ('>=120s', '60-119s', '30-59s', '10-29s'):
    print('   %-9s : %d' % (k, buck[k]))
early = sum(c['early'] for c in per_chan.values())
print('(for reference, slot >=10s SHORTER than file - show cut off early: %d)' % early)

loopers.sort(reverse=True)
print()
print('worst 20 (gap = seconds the channel can loop at that show end):')
for gap, cn, t, slot, fd in loopers[:20]:
    print('   +%4ds   slot %4ds / file %4ds   %-20s | %s' % (gap, slot, fd, str(cn)[:20], str(t)[:32]))

rows = [(c['loop'], cn, c) for cn, c in per_chan.items() if c['loop'] > 0]
rows.sort(reverse=True)
print()
print('per-channel loop-risk tally:')
for loop, cn, c in rows:
    print('   %-24s %4d loop-risk / %4d shows   worst +%ds' % (str(cn)[:24], loop, c['n'], c['worst']))
if not rows:
    print('   (none)')
