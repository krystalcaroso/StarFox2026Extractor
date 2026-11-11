"""Minimal Wwise soundbank (v154) parser for Star Fox (2026) banks."""
import struct

def fnv1_32(s):
    h = 0x811c9dc5
    for c in s.lower().encode():
        h = ((h * 0x01000193) & 0xffffffff) ^ c
    return h

def chunks(d):
    p = 0
    while p + 8 <= len(d):
        t, l = d[p:p + 4], struct.unpack_from('<I', d, p + 4)[0]
        yield t, p + 8, l
        p += 8 + l

def read_var(d, p):
    """Wwise 7-bit variable-length integer."""
    v = 0
    while True:
        b = d[p]; p += 1
        v = (v << 7) | (b & 0x7f)
        if not b & 0x80:
            return v, p

class Bank:
    def __init__(self, data):
        self.d = data
        self.id = None
        self.media = {}      # source id -> bytes (embedded)
        self.objects = {}    # object id -> (type, payload bytes after id)
        didx = []; data_off = None
        for t, o, l in chunks(data):
            if t == b'BKHD':
                self.version, self.id = struct.unpack_from('<II', data, o)
            elif t == b'DIDX':
                didx = [struct.unpack_from('<III', data, o + i) for i in range(0, l, 12)]
            elif t == b'DATA':
                data_off = o
            elif t == b'HIRC':
                n = struct.unpack_from('<I', data, o)[0]; p = o + 4
                for _ in range(n):
                    ty = data[p]; sz, oid = struct.unpack_from('<II', data, p + 1)
                    self.objects[oid] = (ty, data[p + 9:p + 5 + sz])
                    p += 5 + sz
        if data_off is not None:
            for mid, off, size in didx:
                self.media[mid] = data[data_off + off:data_off + off + size]

    # --- typed accessors -------------------------------------------------
    @staticmethod
    def sound_source(payload):
        plugin, stream = struct.unpack_from('<IB', payload, 0)
        sid, size = struct.unpack_from('<II', payload, 5)
        return dict(plugin=plugin, stream=stream, source=sid, size=size)

    @staticmethod
    def event_actions(payload):
        n, p = read_var(payload, 0)
        return list(struct.unpack_from('<%dI' % n, payload, p))

    @staticmethod
    def action_target(payload):
        atype, target = struct.unpack_from('<HI', payload, 0)
        return atype, target

    @staticmethod
    def music_track_sources(payload):
        # v154: u32 numSources, then AkBankSourceData (14 bytes) each (verified on all 287 tracks)
        n = struct.unpack_from('<I', payload, 0)[0]
        out = []
        for i in range(n):
            plugin, stream, sid, size, bits = struct.unpack_from('<IBIIB', payload, 4 + i * 14)
            out.append(sid)
        return out

    def children(self, payload):
        """Heuristic: last 'u32 count + count known object ids' run in the payload."""
        best = []
        L = len(payload)
        for p in range(0, L - 4):
            n = struct.unpack_from('<I', payload, p)[0]
            if 0 < n <= 4096 and p + 4 + 4 * n <= L:
                ids = struct.unpack_from('<%dI' % n, payload, p + 4)
                if all(i in self.objects for i in ids):
                    best = list(ids)
        return best
