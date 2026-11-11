"""veclipse reflection database (data/game/types.c.import) keyed by fnv1a64 type hash."""
import json, os, pickle
HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, '..', 'extracted', 'data', 'game', 'types.c.import')
CACHE = os.path.join(HERE, '..', 'work', 'typedb.pkl')

def g2h(g):
    s = g.replace('-', '')[16:]
    return (int(s[8:16], 16) << 32) | (int(s[4:8], 16) << 16) | int(s[2:4] + s[0:2], 16)

BUILTIN = {  # guid suffix -> (kind, size_bytes)
    'a238-0d3a9fa95da1': ('u8', 1), '8450-0d409fb059a1': ('s8', 1),
    '2119-7a0887213ea9': ('u16', 2), '2bab-843498e110a9': ('s16', 2),
    '8fbf-7a02871ab2a9': ('u32', 4), '75b5-843a98e804a9': ('s32', 4),
    'f459-79f48709baa9': ('u64', 8), '763b-842c98d700a9': ('s64', 8),
    '5b5b-eaf52fd6fb15': ('f32', 4), 'f239-d89c55caba5c': ('f16', 2),
    '0bed-ad15fbead88d': ('bool', 1), '458e-ef95690d0c9e': ('fx24', 4),
    '033a-1772b9841656': ('guid', 16), '0f0f-c51b7c875119': ('vstring', 8),
    'fa15-4a9de52c184b': ('string', 8), '4ff3-89cd82a08182': ('voidptr', 8),
    '6a7f-d84ffbada4dd': ('vtable', 8), '8247-4fad89c1481a': ('voidptr', 8),
    '313f-ca4860a8e4f2': ('void', 0), 'c5ee-98b552cc2d6e': ('buffer', 24),
}

def load():
    if os.path.exists(CACHE) and os.path.getmtime(CACHE) > os.path.getmtime(SRC):
        return pickle.load(open(CACHE, 'rb'))
    objs = json.load(open(SRC))['segments'][0]['exported_objects']
    db = {}
    for o in objs:
        db[g2h(o['$guid'])] = o
    for g, (k, sz) in BUILTIN.items():
        db[g2h('00000000-0000-0000-' + g)] = {'kind': 'builtin', 'name': k, 'size': sz}
    byguid = {o['$guid']: g2h(o['$guid']) for o in objs}
    for g in BUILTIN: byguid['00000000-0000-0000-' + g] = g2h('00000000-0000-0000-' + g)
    pickle.dump((db, byguid), open(CACHE, 'wb'))
    return db, byguid

DB, BYGUID = load()
def get(guid_or_hash):
    h = guid_or_hash if isinstance(guid_or_hash, int) else g2h(guid_or_hash)
    return DB.get(h)
def tname(t):
    if t is None: return '?'
    return t.get('name') or t.get('$name') or '?'
def by_name(n):
    for h, o in DB.items():
        if o.get('name') == n: return o
def show(n):
    o = by_name(n) if isinstance(n, str) else n
    print(tname(o), o.get('kind'), o.get('size_bits'))
    offs = {x['name_hash']: x for x in o.get('field_offsets', [])}
    for f in o.get('fields', []):
        t = get(f['type'])
        fo = offs.get(f['name_hash'], {})
        ob = f.get('offset_bits', fo.get('offset_bits'))
        print('  %6s %-34s %-40s %s' % (ob // 8 if ob is not None else '?', f['name'], tname(t), (t or {}).get('kind', '')))
