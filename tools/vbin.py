#!/usr/bin/env python3
"""Generic decoder for veclipse VIPR/GARD 'vbin-2' .import files, driven by the reflection DB.

Layout (all little endian):
  "VIPR" u32 ver u32 manifest_size ...      manifest GARD block (segment list, imports, exports)
  "GARD" vbin header (struct _vmarshal_image_v0_t-like: version "vbin-2", ..., rtti table)
  "GARD" <segment 'default'> ...  "GARD" <segment 'gpu-nvn-gen2'> ...  "GARDBYE!"
Each segment's base is the byte after its "GARD"; it starts with a vstring-like name.
Tagged 64-bit values (high 16 bits):
  0x8fff  pointer within the same segment (low 48 bits = offset from base)
  0x9000+k pointer into segment k (0 = default, 1 = gpu)
  0xbfff  index into the rtti table (types and imported object GUIDs) - used by vtable and
          pointers to external objects
Vectors: {u64 ptr; i32 n; i32 -n; u64 0}. Strings (vstring): {u32 len|flags; ...} with either
inline chars at +4 or {u32 cap; u64 ptr} when flag 0x40000000 is set.

usage: python -I tools/vbin.py file.import [--json out.json]
"""
import json, struct, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import typedb as T

M48 = (1 << 48) - 1

class Image:
    def __init__(self, data):
        assert data[:4] == b'VIPR'
        self.d = data
        p = data.find(b'GARD\0\0\0\0\0\0\0\0\x06\0\0\0vbin-2')
        assert p > 0, 'no vbin-2 header'
        self.hdr = p + 4
        # rtti table: vector at hdr+0x28 of {u64 kind(1 = type), u64 hash} or a 16-byte guid
        rp, rn = self._vec(self.hdr, 0x28)
        self.rtti = []
        for i in range(rn):
            a, h = struct.unpack_from('<QQ', data, self.hdr + rp + 16 * i)
            raw = data[self.hdr + rp + 16 * i:self.hdr + rp + 16 * i + 16]
            self.rtti.append(('type', h) if a == 1 else ('guid', raw))
        self.segs = []
        q = p
        while True:
            q = data.find(b'GARD', q + 4)
            if q < 0 or data[q:q + 8] == b'GARDBYE!': break
            n = struct.unpack_from('<I', data, q + 4)[0]
            if 0 < n < 64:
                self.segs.append((data[q + 8:q + 8 + n].decode('ascii', 'replace'), q + 4))
        self.seg_names = [s[0] for s in self.segs]
        ep, en = self._vec(self.hdr, 0x40)
        self.exports = [self.guid_str(data[self.hdr + ep + 16 * i:self.hdr + ep + 16 * i + 16]) for i in range(max(en, 0))]

    def _vec(self, base, off):
        v, n = struct.unpack_from('<Qi', self.d, base + off)
        return (v & M48), n

    @staticmethod
    def guid_str(raw):
        # verified against vjson GUID strings: u32le-u16le-u16le-<2 raw bytes>-u16le u32le
        a, b, c = struct.unpack_from('<IHH', raw, 0)
        e, f = struct.unpack_from('<HI', raw, 10)
        return '%08x-%04x-%04x-%s-%04x%08x' % (a, b, c, raw[8:10].hex(), e, f)

    def rtti_name(self, i):
        if not (0 <= i < len(self.rtti)): return 'rtti#%d' % i
        k, v = self.rtti[i]
        if k == 'type': return T.tname(T.get(v))
        return 'import:' + self.guid_str(v)

class Decoder:
    def __init__(self, img, max_array=None):
        self.img = img; self.d = img.d
        self.max_array = max_array
        self.visiting = set()

    def resolve(self, t):
        while t and t.get('kind') == 'k_type_kind_typedef':
            t = T.get(t['underlying_type'])
        return t

    def size(self, t):
        t = self.resolve(t)
        if t is None: return 0
        k = t['kind']
        if k == 'builtin':
            return {'vstring': 32, 'string': 8}.get(t['name'], t['size'])
        if 'size_bits' in t: return t['size_bits'] // 8
        if k == 'k_type_kind_vector': return 24
        if k == 'k_type_kind_pointer' or k == 'k_type_kind_handle': return 8
        if k == 'k_type_kind_array': return t['count'] * self.size(T.get(t['underlying_type']))
        if k == 'k_type_kind_enum':
            n = t.get('name', '')
            return {'u8': 1, 's8': 1, 'u16': 2, 's16': 2, 'u64': 8}.get(n.rsplit('#', 1)[-1].split(':')[0], 4)
        return 0

    def target(self, segbase, off):
        v = struct.unpack_from('<Q', self.d, segbase + off)[0]
        tag, low = v >> 48, v & M48
        if v == 0: return None, None
        if tag == 0x8fff: return segbase, low
        if 0x9000 <= tag < 0x9010:
            k = tag - 0x9000
            # segments after the header: index 0 = 'default', 1 = gpu
            segs = [b for n, b in self.img.segs if n != 'vbin-2']
            return (segs[k] if k < len(segs) else None), low
        if tag == 0xbfff: return 'rtti', low
        return 'raw', v

    def vstring(self, base, off):
        a = base + off
        n = struct.unpack_from('<I', self.d, a)[0]
        ln = n & 0x0fffffff
        if n & 0x40000000:
            b, o = self.target(base, off + 8)
            if isinstance(b, int): return self.d[b + o:b + o + ln].decode('utf-8', 'replace')
            return None
        return self.d[a + 4:a + 4 + ln].decode('utf-8', 'replace')

    def decode(self, base, off, t, depth=0):
        t = self.resolve(t)
        if t is None: return None
        k = t['kind']; a = base + off
        if k == 'builtin':
            n = t['name']
            if n == 'vstring': return self.vstring(base, off)
            if n == 'guid': return Image.guid_str(self.d[a:a + 16])
            if n == 'buffer':
                v = struct.unpack_from('<Q', self.d, a)[0]
                size, flags = struct.unpack_from('<II', self.d, a + 16)
                if v == 0 and size == 0: return None
                r = {'$buffer': True, 'size': size, 'flags': flags, 'abs': a}
                if v >> 48 == 0xcfff: r['blob'] = v & M48
                else:
                    b, o = self.target(base, off)
                    if isinstance(b, int): r['seg_off'] = (b, o)
                return r
            if n == 'vtable':
                b, o = self.target(base, off)
                return self.img.rtti_name(o) if b == 'rtti' else None
            if n in ('voidptr', 'string'):
                b, o = self.target(base, off)
                if b == 'rtti': return {'$ref': self.img.rtti_name(o)}
                if n == 'string' and isinstance(b, int):
                    e = self.d.index(b'\0', b + o); return self.d[b + o:e].decode('utf-8', 'replace')
                return None if b is None else {'$ptr': o}
            fmt = {'u8': 'B', 's8': 'b', 'u16': 'H', 's16': 'h', 'u32': 'I', 's32': 'i', 'u64': 'Q', 's64': 'q',
                   'f32': 'f', 'f16': 'e', 'bool': '?', 'fx24': 'i'}[n]
            return struct.unpack_from('<' + fmt, self.d, a)[0]
        if k == 'k_type_kind_enum':
            sz = self.size(t)
            v = int.from_bytes(self.d[a:a + sz], 'little')
            for c in t.get('constants', []):
                if c.get('value', 0) == v: return c['name']
            return v
        if k == 'k_type_kind_array':
            et = T.get(t['underlying_type']); es = self.size(et)
            return [self.decode(base, off + i * es, et, depth + 1) for i in range(t['count'])]
        if k == 'k_type_kind_vector':
            b, o = self.target(base, off)
            n = struct.unpack_from('<i', self.d, a + 8)[0]
            if b is None or n <= 0: return []
            if not isinstance(b, int): return {'$vector': (b, o, n)}
            et = T.get(t['underlying_type']); es = self.size(et)
            et_r = self.resolve(et)
            if et_r and et_r['kind'] == 'builtin' and et_r['name'] in ('u8', 's8') :
                return {'$bytes': (b, o, n)}
            lim = n if self.max_array is None else min(n, self.max_array)
            out = [self.decode(b, o + i * es, et, depth + 1) for i in range(lim)]
            if lim < n: out.append('... %d more' % (n - lim))
            return out
        if k in ('k_type_kind_pointer', 'k_type_kind_handle'):
            b, o = self.target(base, off)
            if b is None: return None
            if b == 'rtti': return {'$ref': self.img.rtti_name(o)}
            if not isinstance(b, int): return {'$raw': o}
            key = (b, o)
            if key in self.visiting or depth > 40: return {'$cycle': o}
            self.visiting.add(key)
            try:
                ut = T.get(t['underlying_type'])
                return self.decode_obj(b, o, ut, depth + 1)
            finally:
                self.visiting.discard(key)
        if k in ('k_type_kind_struct', 'k_type_kind_union'):
            return self.decode_struct(base, off, t, depth)
        if k == 'k_type_kind_bits':
            sz = self.size(t) or 4
            return int.from_bytes(self.d[a:a + sz], 'little')
        return '<%s>' % k

    def decode_obj(self, base, off, t, depth):
        """Pointer target: if the struct has a vtable, use the dynamic type."""
        rt = self.resolve(t)
        if rt and rt['kind'] == 'k_type_kind_struct' and rt.get('fields') and rt['fields'][0]['name'] == 'vtable':
            b, o = self.target(base, off)
            if b == 'rtti' and o < len(self.img.rtti) and self.img.rtti[o][0] == 'type':
                dyn = T.get(self.img.rtti[o][1])
                if dyn: rt = dyn
        return self.decode(base, off, rt, depth)

    def fields(self, t):
        offs = {x['name_hash']: x for x in t.get('field_offsets', [])}
        out = []
        for f in t.get('fields', []):
            fo = offs.get(f['name_hash'], {})
            ob = f.get('offset_bits', fo.get('offset_bits', 0))
            out.append((f['name'], ob, T.get(f['type']), fo.get('size_bits')))
        return out

    def bits(self, base, off, ob, nbits, ft):
        a = base + off + ob // 8
        nbytes = (ob % 8 + nbits + 7) // 8
        v = (int.from_bytes(self.d[a:a + nbytes], 'little') >> (ob % 8)) & ((1 << nbits) - 1)
        rt = self.resolve(ft)
        if rt and rt.get('kind') == 'k_type_kind_enum':
            for c in rt.get('constants', []):
                if c.get('value', 0) == v: return c['name']
        return v

    def decode_struct(self, base, off, t, depth):
        r = {'$type': T.tname(t)}
        for name, ob, ft, nbits in self.fields(t):
            rt = self.resolve(ft)
            tn = (rt or {}).get('name', '') or ''
            if ob % 8 or (nbits and nbits % 8) or (rt and rt.get('kind') == 'k_type_kind_bits') or ':' in tn:
                if not nbits and ':' in tn: nbits = int(tn.rsplit(':', 1)[1])
                r[name] = self.bits(base, off, ob, nbits or 1, ft); continue
            try:
                r[name] = self.decode(base, off + ob // 8, ft, depth + 1)
            except Exception as e:
                r[name] = '<err %s>' % e
        return r

    def roots(self):
        """Objects listed by the 'default' segment (vector<void*> at +0x40)."""
        out = []
        for name, b in self.img.segs:
            if name == 'vbin-2': continue
            p, n = self.img._vec(b, 0x40)
            for i in range(n):
                tb, to = self.target(b, p + 8 * i)
                if isinstance(tb, int):
                    vb, vo = self.target(tb, to)
                    tn = self.img.rtti[vo] if vb == 'rtti' and vo < len(self.img.rtti) else None
                    t = T.get(tn[1]) if tn and tn[0] == 'type' else None
                    out.append((name, tb, to, t))
        return out

def decode_file(path, max_array=None):
    img = Image(open(path, 'rb').read())
    dec = Decoder(img, max_array)
    res = []
    for seg, b, o, t in dec.roots():
        res.append({'$segment': seg, '$offset': o, 'value': dec.decode(b, o, t) if t else None})
    return img, dec, res

if __name__ == '__main__':
    img, dec, res = decode_file(sys.argv[1], max_array=None if '--full' in sys.argv else 8)
    print('segments', img.seg_names)
    print('rtti', [img.rtti_name(i) for i in range(len(img.rtti))])
    s = json.dumps(res, indent=1, default=str)
    if '--json' in sys.argv:
        open(sys.argv[sys.argv.index('--json') + 1], 'w').write(s)
    else:
        print(s)
