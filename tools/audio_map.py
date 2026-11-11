#!/usr/bin/env python3
"""Map Wwise events -> media sources, extract bank-embedded .wem, write a conversion plan.

usage: python -I tools/audio_map.py <extracted_dir> <wem_stage_dir> <plan.csv>
"""
import csv, glob, os, pickle, re, struct, sys
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wwise import Bank, fnv1_32

ext, stage, plan_path = sys.argv[1:4]
root = os.path.join(ext, 'data/eclipse/audio/eclipse/generatedsoundbanks/switch2')
here = os.path.dirname(os.path.abspath(__file__))

# ---- load banks ---------------------------------------------------------
objs = {}                      # id -> (type, payload, bankname)  (global, first seen)
bank_objs = {}                 # bankname -> {id: (type, payload, bankname)}
bank_events = defaultdict(set) # bankname -> event ids
embedded = {}                  # source id -> (bankname, bytes)
for f in sorted(glob.glob(os.path.join(root, '**/*.bnk'), recursive=True)):
    rel = os.path.relpath(f, root)
    bname = rel[:-4]           # e.g. 'arwing' or 'english(us)/vo'
    b = Bank(open(f, 'rb').read())
    bank_objs[bname] = {oid: (t, p, bname) for oid, (t, p) in b.objects.items()}
    for oid, (t, p) in b.objects.items():
        objs.setdefault(oid, (t, p, bname))
        if t == 4: bank_events[bname].add(oid)
    for mid, data in b.media.items():
        embedded.setdefault(mid, (bname, data))

# ---- names --------------------------------------------------------------
names = {}
wproj = os.path.join(ext, 'data/eclipse/audio/eclipse/eclipse.wproj.import')
cands = set(m.group().decode() for m in re.finditer(rb'[A-Za-z0-9_]{3,}', open(wproj, 'rb').read()))
extra = os.path.join(here, 'wwise_names.txt')
if os.path.exists(extra): cands |= set(open(extra).read().split())
for c in cands:
    for p in ('', 'play_', 'stop_'):
        h = fnv1_32(p + c)
        if h in objs: names.setdefault(h, (p + c).lower())

# ---- graph walk ---------------------------------------------------------
LOCAL = {}
def get(oid): return LOCAL.get(oid) or objs.get(oid)
def children(payload):
    best = []; L = len(payload)
    for p in range(0, L - 4):
        n = struct.unpack_from('<I', payload, p)[0]
        if 0 < n <= 4096 and p + 4 + 4 * n <= L:
            ids = struct.unpack_from('<%dI' % n, payload, p + 4)
            if all(i in objs or i in LOCAL for i in ids): best = list(ids)
    return best

def sources_of(oid, seen, broad):
    o = get(oid)
    if oid in seen or o is None: return set()
    seen.add(oid)
    t, p, _ = o
    if t == 2: return {Bank.sound_source(p)['source']}
    if t == 11:
        try: return set(Bank.music_track_sources(p))
        except struct.error: return set()
    if t in (5, 6, 7, 9, 10, 12, 13):
        out = set()
        kids = set(children(p))
        if broad and t in (10, 12, 13):
            # Music containers also reference segments/tracks outside the child list
            # (transition rules / bridge segments, decision-tree playlists). Over-inclusive (also hits transition
            # destinations), so only used to label sources the strict walk misses.
            for i in range(0, len(p) - 3):
                x = struct.unpack_from('<I', p, i)[0]
                o2 = get(x)
                if o2 is not None and o2[0] in (10, 11, 13) and x != oid: kids.add(x)
        for c in kids: out |= sources_of(c, seen, broad)
        return out
    return set()

def walk(broad):
    global LOCAL
    ev, bk = defaultdict(set), defaultdict(set)
    for bname, evs in bank_events.items():
        LOCAL = bank_objs[bname]
        for e in evs:
            for a in Bank.event_actions(get(e)[1]):
                if get(a) is None: continue
                atype, tgt = Bank.action_target(get(a)[1])
                if atype != 0x403: continue           # Play actions only
                for s in sources_of(tgt, set(), broad):
                    ev[s].add(names.get(e, 'event_%u' % e))
                    bk[s].add(bname)
    return ev, bk

src_events, src_banks = walk(False)
src_link = {s: 'direct' for s in src_events}
ev2, bk2 = walk(True)
for s in ev2:
    if s not in src_events:
        src_events[s], src_banks[s], src_link[s] = ev2[s], bk2[s], 'transition'
print('sources linked: direct', sum(v == 'direct' for v in src_link.values()),
      'via transition rules', sum(v == 'transition' for v in src_link.values()))

# ---- enumerate all media ------------------------------------------------
def safe(s): return re.sub(r'[^A-Za-z0-9_()\-]+', '_', s)[:120]
rows = []
media_dir = os.path.join(root, 'media')
loose = []
for f in glob.glob(os.path.join(media_dir, '**/*.wem'), recursive=True):
    rel = os.path.relpath(f, media_dir)
    lang = os.path.dirname(rel)
    loose.append((int(os.path.basename(f)[:-4]), lang, f))
os.makedirs(stage, exist_ok=True)
emb_items = []
for sid, (bname, data) in embedded.items():
    d = os.path.join(stage, safe(bname.replace('/', '__')))
    os.makedirs(d, exist_ok=True)
    f = os.path.join(d, '%u.wem' % sid)
    with open(f, 'wb') as fh: fh.write(data)
    lang = os.path.dirname(bname)
    emb_items.append((sid, lang, f))

# A bank copy of a streamed sound is only the prefetch head; keep the full streamed file.
loose_keys = {(lang, sid) for sid, lang, _ in loose}
prefetch = [x for x in emb_items if (x[1], x[0]) in loose_keys]
emb_items = [x for x in emb_items if (x[1], x[0]) not in loose_keys]
for _, _, f in prefetch: os.remove(f)
print('dropped prefetch heads', len(prefetch))

for sid, lang, f in loose + emb_items:
    evs = sorted(src_events.get(sid, ()))
    banks = sorted(src_banks.get(sid, ())) or ([embedded[sid][0]] if sid in embedded and f.startswith(stage) else [])
    if lang:
        top = os.path.join('vo', lang)
    else:
        top = os.path.basename(banks[0]) if banks else '_unreferenced'
    label = evs[0].removeprefix('play_') if evs else 'unref'
    out = os.path.join(top, '%s__%u.flac' % (safe(label), sid))
    rows.append([sid, lang, 'embedded' if f.startswith(stage) else 'streamed', f, out,
                 ';'.join(evs), ';'.join(banks), src_link.get(sid, 'none')])

# disambiguate duplicate output paths (same id present loose + embedded)
seen = {}
for r in rows:
    k = r[4]
    if k in seen:
        seen[k] += 1; r[4] = k[:-5] + '_dup%d.flac' % seen[k]
    else: seen[k] = 0
with open(plan_path, 'w', newline='') as fh:
    w = csv.writer(fh)
    w.writerow(['source_id', 'lang', 'kind', 'wem', 'flac', 'events', 'banks', 'link'])
    w.writerows(sorted(rows, key=lambda r: r[4]))
print('objects', len(objs), 'events', sum(map(len, bank_events.values())), 'named', len(names))
print('media rows', len(rows), 'with events', sum(1 for r in rows if r[5]))
