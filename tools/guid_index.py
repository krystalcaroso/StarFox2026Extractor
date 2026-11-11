#!/usr/bin/env python3
"""Build GUID -> asset path index for all .import (VIPR) and vjson assets. usage: guid_index.py <extracted> <out.json>"""
import json, os, sys
from concurrent.futures import ProcessPoolExecutor
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vbin

def scan(p):
    out = []
    try:
        with open(p, 'rb') as f: head = f.read(4)
        if head == b'VIPR':
            img = vbin.Image(open(p, 'rb').read())
            out = [(g, 'export') for g in img.exports]
        elif head[:1] == b'{':
            j = json.load(open(p, encoding='utf-8', errors='replace'))
            for g in j.get('exports', []): out.append((g, 'export'))
            for s in j.get('segments', []):
                for o in s.get('exported_objects', []):
                    if '$guid' in o: out.append((o['$guid'], 'object'))
    except Exception:
        pass
    return p, out

if __name__ == '__main__':
    ext, dst = sys.argv[1:3]
    files = []
    for dp, dn, fn in os.walk(os.path.join(ext, 'data')):
        for f in fn:
            if not f.endswith('.raw') and not f.startswith('._') and not f.endswith(('.wem', '.bnk')):
                files.append(os.path.join(dp, f))
    idx = {}
    with ProcessPoolExecutor() as ex:
        for p, gs in ex.map(scan, files, chunksize=64):
            rel = '/' + os.path.relpath(p, ext)
            for g, kind in gs:
                idx.setdefault(g, rel)
    json.dump(idx, open(dst, 'w'), indent=0, sort_keys=True)
    print('files', len(files), 'guids', len(idx))
