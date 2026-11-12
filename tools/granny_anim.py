"""Granny 2 animations embedded in veclipse `_vanim_animation_t` imports (`*.anim.fbx.import`, `*.anim.gr2.import`,
`*.timeline.fbx.import`).

The animation's `data` buffer is a 64-bit granny_file_info with unrelocated pointers (offsets into the blob), the
same as the skeletons in model imports (see granny_skel.py). Curve objects are variants {type definition*, data*}
and the type definitions are embedded, so curves are read by member name rather than by fixed layout.

Curves are B-splines of the stored degree with one knot per control. The full knot vector is
    [K0] + K + [Kn-1] * degree
which makes the valid range exactly [K(degree-1), Kn-1], i.e. [~0, duration] in the data.

usage: python -I tools/granny_anim.py file.import      (prints a summary)
"""
import math, os, struct, sys
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# granny_member_type -> size in a 64-bit file (array width multiplies)
SIZES = {2: 8, 3: 12, 4: 12, 5: 16, 7: 20, 8: 8, 9: 68, 10: 4, 11: 1, 12: 1, 13: 1, 14: 1, 15: 2, 16: 2, 17: 2,
         18: 2, 19: 4, 20: 4, 21: 2, 22: 8}
SCALAR = {10: '<f', 11: '<b', 12: '<B', 13: '<b', 14: '<B', 15: '<h', 16: '<H', 17: '<h', 18: '<H', 19: '<i',
          20: '<I', 21: '<e'}
NP = {10: '<f4', 11: 'i1', 12: 'u1', 15: '<i2', 16: '<u2', 19: '<i4', 20: '<u4', 21: '<f2'}

# D4n quaternion tables (smallest-three encoding; 4 bits per component select a scale/offset)
QSCALE = [1.4142135, 0.70710677, 0.35355338, 0.35355338, 0.35355338, 0.17677669, 0.17677669, 0.17677669,
          -1.4142135, -0.70710677, -0.35355338, -0.35355338, -0.35355338, -0.17677669, -0.17677669, -0.17677669]
QOFFSET = [-0.70710677, -0.35355338, -0.53033006, -0.17677669, 0.17677669, -0.17677669, -0.088388346, 0.0,
           0.70710677, 0.35355338, 0.53033006, 0.17677669, -0.17677669, 0.17677669, 0.088388346, -0.0]


class Blob:
    def __init__(self, data):
        self.b = data

    def u(self, fmt, off):
        return struct.unpack_from(fmt, self.b, off)

    def cstr(self, off):
        if not off: return None
        e = self.b.index(b'\0', off)
        return self.b[off:e].decode('utf-8', 'replace')

    def members(self, tdef):
        out = []
        q = tdef
        while True:
            t, name, ref, width = self.u('<iQQi', q)
            if t == 0: return out
            out.append((t, self.cstr(name), ref, width))
            q += 44

    def size_of(self, tdef):
        n = 0
        for t, name, ref, width in self.members(tdef):
            n += (self.size_of(ref) if t == 1 else SIZES[t]) * max(width, 1)
        return n

    def read(self, tdef, off):
        """Read an object by its type definition. ReferenceToArray of scalars -> numpy array."""
        out = {}
        for t, name, ref, width in self.members(tdef):
            w = max(width, 1)
            if t == 1:
                out[name] = self.read(ref, off); off += self.size_of(ref) * w; continue
            if t in SCALAR:
                vals = [self.u(SCALAR[t], off + i * SIZES[t])[0] for i in range(w)]
                out[name] = vals if width else vals[0]
            elif t == 3:
                n, p = self.u('<iQ', off)
                el = self.members(ref)
                if len(el) == 1 and el[0][0] in NP and n:
                    out[name] = np.frombuffer(self.b, NP[el[0][0]], n * max(el[0][3], 1), p).astype(np.float64)
                else:
                    out[name] = (n, p, ref)
            elif t == 8:
                out[name] = self.cstr(self.u('<Q', off)[0])
            else:
                out[name] = (t, self.b[off:off + SIZES[t] * w])
            off += SIZES[t] * w
        return out


def _trunc(v):
    return struct.unpack('<f', struct.pack('<I', int(v) << 16))[0]


class Curve:
    """Decoded curve: knots (n,), controls (n, dim), degree; or a constant/identity."""
    def __init__(self, knots, controls, degree, kind):
        self.knots = np.asarray(knots, np.float64)
        self.controls = np.asarray(controls, np.float64)
        self.degree = int(degree)
        self.kind = kind

    @property
    def constant(self):
        return len(self.knots) <= 1

    def sample(self, t):
        t = np.atleast_1d(np.asarray(t, np.float64))
        c = self.controls
        if len(self.knots) <= 1 or len(c) == 1:
            return np.repeat(c[:1], len(t), 0)
        d = min(self.degree, len(c) - 1)
        K = self.knots
        T = np.concatenate([[K[0]], K, np.repeat(K[-1], d)])
        if d == 0:
            T = np.concatenate([[K[0]], K])
        lo, hi = T[d], T[len(c)]
        t = np.clip(t, lo, hi)
        # span s: T[s] <= t < T[s+1], d <= s <= n-1
        s = np.clip(np.searchsorted(T, t, side='right') - 1, d, len(c) - 1)
        P = [c[s - d + j] for j in range(d + 1)]            # de Boor, vectorised over t
        for r in range(1, d + 1):
            for j in range(d, r - 1, -1):
                k = s - d + j
                den = T[k + d - r + 1] - T[k]
                with np.errstate(divide='ignore', invalid='ignore'):
                    a = np.where(den > 0, (t - T[k]) / den, 0.0)[:, None]
                P[j] = (1 - a) * P[j - 1] + a * P[j]
        return P[d]


def _quat_from_controls(a, b, c, sel, bits):
    top = 1 << (bits - 1); mask = top - 1
    unit = 1.0 / mask
    scales = [QSCALE[(sel >> (4 * i)) & 15] * unit for i in range(4)]
    offs = [QOFFSET[(sel >> (4 * i)) & 15] for i in range(4)]
    a = a.astype(np.int64); b = b.astype(np.int64); c = c.astype(np.int64)
    s1 = (((b & top) != 0).astype(int) << 1) | ((c & top) != 0).astype(int)
    q = np.zeros((len(a), 4))
    for i in range(len(a)):
        w1 = s1[i]; w2 = (w1 + 1) & 3; w3 = (w2 + 1) & 3; w4 = (w3 + 1) & 3
        A = (a[i] & mask) * scales[w2] + offs[w2]
        B = (b[i] & mask) * scales[w3] + offs[w3]
        C = (c[i] & mask) * scales[w4] + offs[w4]
        D = math.sqrt(max(0.0, 1 - (A * A + B * B + C * C)))
        if a[i] & top: D = -D
        q[i, w2], q[i, w3], q[i, w4], q[i, w1] = A, B, C, D
    return q


def decode_curve(bl, tdef, off, dim):
    """dim = 3 (position), 4 (orientation, xyzw) or 9 (scale/shear, row-major 3x3)."""
    if not tdef or not off:
        return None
    o = bl.read(tdef, off)
    hdr_name = next(k for k in o if k.startswith('CurveDataHeader_'))
    fmt = hdr_name[len('CurveDataHeader_'):]
    deg = o[hdr_name]['Degree']
    ident = {1: [0], 3: [0, 0, 0], 4: [0, 0, 0, 1], 9: [1, 0, 0, 0, 1, 0, 0, 0, 1]}.get(dim, [0] * dim)
    if fmt == 'DaIdentity':
        return Curve([0], [ident], 0, fmt)
    if fmt in ('D3Constant32f', 'D4Constant32f', 'DaConstant32f'):
        v = np.asarray(o['Controls'], np.float64).reshape(1, -1)
        return Curve([0], _expand(v, dim), 0, fmt)
    if fmt == 'DaKeyframes32f':
        n = int(o['Dimension'])
        v = np.asarray(o['Controls']).reshape(-1, n)
        return Curve(np.arange(len(v), dtype=np.float64), _expand(v, dim), 0, fmt)
    if fmt == 'DaK32fC32f':
        K = np.asarray(o['Knots']); C = np.asarray(o['Controls']).reshape(len(K), -1)
        return Curve(K, _expand(C, dim), deg, fmt)
    kc = o['KnotsControls']
    if fmt in ('D4nK16uC15u', 'D4nK8uC7u'):
        n = len(kc) // 4
        K = kc[:n] / o['OneOverKnotScale']
        C = kc[n:].reshape(n, 3)
        q = _quat_from_controls(C[:, 0], C[:, 1], C[:, 2], int(o['ScaleOffsetTableEntries']),
                                16 if fmt == 'D4nK16uC15u' else 8)
        return Curve(K, q, deg, fmt)
    inv = _trunc(o['OneOverKnotScaleTrunc']) if 'OneOverKnotScaleTrunc' in o else None
    if fmt in ('DaK16uC16u', 'DaK8uC8u'):
        so = np.asarray(o['ControlScaleOffsets']); m = len(so) // 2
        n = len(kc) // (m + 1)
        C = kc[n:].reshape(n, m) * so[:m] + so[m:]
        return Curve(kc[:n] / inv, _expand(C, dim), deg, fmt)
    if fmt in ('D3K16uC16u', 'D3K8uC8u', 'D9I3K16uC16u', 'D9I3K8uC8u'):
        n = len(kc) // 4
        C = kc[n:].reshape(n, 3) * np.asarray(o['ControlScales']) + np.asarray(o['ControlOffsets'])
        return Curve(kc[:n] / inv, _expand(C, dim, diag=fmt.startswith('D9')), deg, fmt)
    if fmt in ('D9I1K16uC16u', 'D9I1K8uC8u'):
        n = len(kc) // 2
        v = kc[n:] * o['ControlScale'] + o['ControlOffset']
        return Curve(kc[:n] / inv, _expand(np.repeat(v[:, None], 3, 1), dim, diag=True), deg, fmt)
    if fmt in ('D3I1K32fC32f', 'D3I1K16uC16u', 'D3I1K8uC8u'):
        n = len(kc) // 2
        K = kc[:n] if fmt == 'D3I1K32fC32f' else kc[:n] / inv
        v = kc[n:]
        C = v[:, None] * np.asarray(o['ControlScales']) + np.asarray(o['ControlOffsets'])
        return Curve(K, _expand(C, dim), deg, fmt)
    raise ValueError('unsupported curve format ' + fmt)


def _expand(v, dim, diag=False):
    v = np.asarray(v, np.float64)
    if dim == 9 and (diag or v.shape[1] == 3):
        m = np.zeros((len(v), 9)); m[:, 0], m[:, 4], m[:, 8] = v[:, 0], v[:, 1], v[:, 2]
        return m
    return v


def parse(blob):
    """-> list of animations: {name, duration, time_step, oversampling, track_groups: [{name, initial, tracks}]}
    track = {name, flags, orientation, position, scale_shear} (Curve or None)."""
    bl = Blob(blob)
    u = bl.u
    n, p = u('<iQ', 0x18 + 8 * 12)
    anims = []
    for i in range(n):
        a = u('<Q', p + 8 * i)[0]
        name = bl.cstr(u('<Q', a)[0])
        dur, ts, ov, ntg, tgp = u('<fffiQ', a + 8)
        groups = []
        for j in range(ntg):
            tg = u('<Q', tgp + 8 * j)[0]
            gname = bl.cstr(u('<Q', tg)[0])
            nv, vp, nt, tp = u('<iQiQ', tg + 8)
            tracks = []
            for k in range(nt):
                tt = tp + 60 * k
                tr = {'name': bl.cstr(u('<Q', tt)[0]), 'flags': u('<i', tt + 8)[0]}
                for ci, (key, dim) in enumerate((('orientation', 4), ('position', 3), ('scale_shear', 9))):
                    ty, ob = u('<QQ', tt + 12 + 16 * ci)
                    tr[key] = decode_curve(bl, ty, ob, dim)
                tracks.append(tr)
            vtracks = []
            for k in range(nv):
                vt = vp + 32 * k               # granny_vector_track: name, u32 track key, i32 dimension, curve
                vdim = u('<i', vt + 12)[0]
                ty, ob = u('<QQ', vt + 16)
                vtracks.append({'name': bl.cstr(u('<Q', vt)[0]), 'key': u('<I', vt + 8)[0], 'dimension': vdim,
                                'curve': decode_curve(bl, ty, ob, vdim)})
            groups.append({'name': gname, 'tracks': tracks, 'vector_tracks': vtracks, 'vector_track_count': nv})
        anims.append({'name': name, 'duration': dur, 'time_step': ts, 'oversampling': ov, 'track_groups': groups})
    return anims


def load(path):
    """Read an animation import -> (vanim object, granny animations)."""
    import vbin
    img, dec, res = vbin.decode_file(path)
    v = res[0]['value']
    d = v.get('data') or {}
    if 'seg_off' not in d:
        return v, []
    b, o = d['seg_off']
    return v, parse(img.d[b + o:b + o + d['size']])


if __name__ == '__main__':
    v, anims = load(sys.argv[1])
    for a in anims:
        print('animation %r duration %.4f step %.6f oversampling %g' % (a['name'], a['duration'], a['time_step'],
                                                                         a['oversampling']))
        for g in a['track_groups']:
            print(' group %r: %d transform tracks, %d vector tracks' % (g['name'], len(g['tracks']),
                                                                        g['vector_track_count']))
            for tr in g['tracks'][:int(sys.argv[2]) if len(sys.argv) > 2 else 5]:
                print('  %-30s' % tr['name'], ' '.join('%s:%s/%d/deg%d' % (k[0], c.kind, len(c.knots), c.degree)
                                                     for k in ('orientation', 'position', 'scale_shear')
                                                     if (c := tr[k]) is not None))
    print('blendshape tracks', len(v.get('blendshape_tracks') or []))
