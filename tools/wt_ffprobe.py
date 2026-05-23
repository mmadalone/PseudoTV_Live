#!/usr/bin/env python3
# Confirm: are Weird Tales files actually shorter than the library metadata?
import xml.etree.ElementTree as ET
import re, base64, zlib, urllib.parse, pickle, subprocess, shutil

XML = '/home/madalone/.kodi/userdata/addon_data/plugin.video.pseudotv.imports/cache/pseudotv.xml'
C = re.compile(r'\[COLOR item="([^"]+)"\]')

def dec(d):
    m = C.search(d or '')
    if not m:
        return None
    try:
        return pickle.loads(zlib.decompress(base64.b64decode(urllib.parse.unquote(m.group(1)))))
    except Exception:
        return None

def md(fi):
    v = (fi.get('streamdetails') or {}).get('video') or []
    if v and v[0].get('duration'):
        return int(v[0]['duration'])
    return int(fi['runtime']) if fi.get('runtime') else None

ff = shutil.which('ffprobe')
print('ffprobe found:', ff)
got = {}
for ev, el in ET.iterparse(XML, events=('end',)):
    if el.tag != 'programme':
        if el.tag == 'channel':
            el.clear()
        continue
    le = el.find('length'); de = el.find('desc'); te = el.find('title')
    slot = int(le.text) if (le is not None and le.text and le.text.strip().isdigit()) else None
    fi = dec(de.text if de is not None else None)
    el.clear()
    if not fi or (fi.get('citem') or {}).get('name') != 'Weird Tales':
        continue
    f = fi.get('file')
    if not f or f in got:
        continue
    if slot and 1500 <= slot <= 1800:
        got[f] = (slot, md(fi), te.text)
    if len(got) >= 8:
        break

print('checking', len(got), 'Weird Tales files (slot 1500-1800s):')
for f, (slot, m, t) in got.items():
    real = 'n/a'
    if ff:
        try:
            o = subprocess.run([ff, '-v', 'quiet', '-show_entries', 'format=duration',
                                '-of', 'csv=p=0', f], capture_output=True, text=True, timeout=25)
            real = int(float(o.stdout.strip())) if o.stdout.strip() else 'empty'
        except Exception:
            real = 'ERR'
    flag = ''
    if isinstance(real, int) and slot:
        flag = '  <== file < slot, WILL LOOP' if real < slot else '  ok'
    print('  slot=%-5s  library=%-7s  real-file=%-9s  %s%s' % (slot, m, real, t, flag))
