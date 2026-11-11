#!/usr/bin/env python3
"""Extract a Star Fox (2026) VZIP data.zip with real file names.

usage: python -I tools/extract.py <data.zip> <out_dir> [--names tools/names.json] [--filter SUBSTR] [--list]

Archive layout:
  'VZIP' u32 count, then count * 40-byte records:
     u32 name_off (into string pool at 12+count*40), u32 ?, u64 offset, u64 csize, u64 usize,
     u32 compressed, u32 fnv1a32(hexname)     -- sorted by the fnv1a32
  Data: a genuine ZIP64 (PK local headers) whose member names are hex keys.
  key = '%x' % fnv1a64(lowercase(path without leading '/'))
  Compressed data = sequence of [u32 size][LZ4 block], 64 KiB linked blocks.
"""
import argparse, csv, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from vzip import VZip

def guess_ext(b):
    if b[:4] == b'VIPR': return '.vbin'
    if b[:1] == b'{': return '.json'
    if b[:4] == b'RIFF': return '.wem'
    if b[:4] == b'BKHD': return '.bnk'
    return '.bin'

def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser()
    ap.add_argument('archive'); ap.add_argument('out')
    ap.add_argument('--names', default=os.path.join(here, 'names.json'))
    ap.add_argument('--filter', default=None, help='only extract paths containing this substring')
    ap.add_argument('--list', action='store_true', help='only write manifest, no data')
    a = ap.parse_args()
    names = json.load(open(a.names))
    z = VZip(a.archive)
    out = os.path.abspath(a.out); os.makedirs(out, exist_ok=True)
    groups = {}
    for e in z.entries: groups.setdefault(e['offset'], []).append(e)
    rows = []; done = 0
    for off in sorted(groups):
        es = groups[off]
        named = [names[e['name']] for e in es if e['name'] in names]
        data = None; targets = []
        if named:
            targets = sorted(set(named))
        if a.filter and not any(a.filter in t for t in targets): 
            continue
        if not a.list:
            data = z.read(es[0])
            if not targets:
                targets = ['_unnamed/%s%s' % (es[0]['name'], guess_ext(data))]
            for t in targets:
                dst = os.path.normpath(os.path.join(out, t))
                if not dst.startswith(out + os.sep): raise SystemExit('unsafe path ' + t)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                with open(dst, 'wb') as fh: fh.write(data)
        primary = targets[0] if targets else ''
        for e in es:
            p = names.get(e['name'])
            rows.append([e['name'], p or '', 'named' if p else ('alias' if named else 'unnamed'),
                         primary if not p else '', e['offset'], e['csize'], e['usize']])
        done += 1
        if done % 5000 == 0: print(done, len(groups), flush=True)
    with open(os.path.join(out, '_manifest.csv'), 'w', newline='') as fh:
        w = csv.writer(fh); w.writerow(['key', 'path', 'status', 'same_data_as', 'offset', 'csize', 'usize']); w.writerows(rows)
    print('done', done, 'blobs')

if __name__ == '__main__':
    main()
