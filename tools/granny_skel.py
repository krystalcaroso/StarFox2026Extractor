"""Parse the embedded Granny 64-bit file_info blob (unrelocated: pointers are blob offsets).

Only what the engine keeps: Models -> Skeleton -> Bones (name, parent, local transform, inverse world).
"""
import struct
import numpy as np

def _cstr(b, o):
    if not o: return None
    e = b.index(b'\0', o); return b[o:e].decode('utf-8', 'replace')

def parse(blob):
    u = lambda f, o: struct.unpack_from(f, blob, o)
    # granny_file_info (packed, 64-bit): 3 ptrs, then 9 {i32 count; ptr} arrays, variant
    off = 0x18
    arrays = {}
    for nm in ('textures', 'materials', 'skeletons', 'vertex_datas', 'tri_topologies', 'meshes', 'models',
               'track_groups', 'animations'):
        arrays[nm] = u('<iQ', off); off += 12
    out = {'from_file': _cstr(blob, u('<Q', 0x10)[0]), 'models': [], 'skeletons': []}
    seen = {}
    def skeleton(so):
        if so in seen: return seen[so]
        name = _cstr(blob, u('<Q', so)[0])
        n, bp = u('<iQ', so + 8)
        lod_type = u('<i', so + 20)[0]
        bones = []
        for i in range(n):
            b = bp + i * 164
            bn = _cstr(blob, u('<Q', b)[0])
            parent = u('<i', b + 8)[0]
            flags = u('<I', b + 12)[0]
            pos = u('<3f', b + 16); quat = u('<4f', b + 28); ss = np.array(u('<9f', b + 44)).reshape(3, 3)
            inv = np.array(u('<16f', b + 80)).reshape(4, 4)
            lod_err = u('<f', b + 144)[0]
            bones.append(dict(name=bn, parent=parent, flags=flags, pos=pos, quat=quat, scale_shear=ss,
                              inv_world=inv, lod_error=lod_err))
        s = dict(name=name, bones=bones, lod_type=lod_type)
        seen[so] = s; out['skeletons'].append(s)
        return s
    n, p = arrays['models']
    for i in range(n):
        mo = u('<Q', p + 8 * i)[0]
        name = _cstr(blob, u('<Q', mo)[0])
        so = u('<Q', mo + 8)[0]
        out['models'].append(dict(name=name, skeleton=skeleton(so) if so else None))
    n, p = arrays['skeletons']
    for i in range(n):
        skeleton(u('<Q', p + 8 * i)[0])
    return out

def world_matrices(bones):
    """Column-vector 4x4 world matrices from local transforms (pos, quat xyzw, scale/shear)."""
    W = []
    for b in bones:
        x, y, z, w = b['quat']
        R = np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                      [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                      [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])
        L = np.eye(4); L[:3, :3] = R @ b['scale_shear']; L[:3, 3] = b['pos']
        W.append(W[b['parent']] @ L if b['parent'] >= 0 else L)
    return W
