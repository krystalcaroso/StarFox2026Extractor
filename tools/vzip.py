"""Reader for the VZIP-indexed data.zip used by Star Fox (2026)."""
import struct, lz4.block

class VZip:
    def __init__(self, path):
        self.f = open(path, 'rb')
        hdr = self.f.read(12)
        assert hdr[:4] == b'VZIP'
        self.count = struct.unpack_from('<I', hdr, 4)[0]
        raw = self.f.read(self.count * 40)
        pool_start = 12 + self.count * 40
        recs = [struct.unpack_from('<IIQQQII', raw, i * 40) for i in range(self.count)]
        # read string pool (names are offsets relative to pool_start)
        maxoff = max(r[0] for r in recs)
        self.f.seek(pool_start)
        pool = self.f.read(maxoff + 64)
        self.entries = []
        for nameoff, _unk, off, cs, us, flag, h32 in recs:
            name = pool[nameoff:pool.index(b'\0', nameoff)].decode()
            self.entries.append(dict(name=name, offset=off, csize=cs, usize=us, compressed=flag, h32=h32))
        self.by_name = {e['name']: e for e in self.entries}

    def read(self, e):
        if isinstance(e, str):
            e = self.by_name[e]
        self.f.seek(e['offset'])
        b = self.f.read(e['csize'])
        if not e['compressed']:
            return b
        out = bytearray(); p = 0
        while p < len(b):
            cs = struct.unpack_from('<I', b, p)[0]; p += 4
            out += lz4.block.decompress(b[p:p + cs], uncompressed_size=min(0x10000, e['usize'] - len(out)),
                                        dict=bytes(out[-0x10000:]))
            p += cs
        assert len(out) == e['usize'], (e, len(out))
        return bytes(out)
