#!/usr/bin/env python3
"""Tiny software rasterizer for checking exported .glb files (bind pose, no skinning).

usage: render_glb.py in.glb out.png [--view front|side|back] [--zoom top FRACTION] [--size N]
Per-pixel UV interpolation, base colour texture (+COLOR_0, texCoord), MASK alpha test, BLEND drawn
with alpha (after opaque, unsorted), Lambert + ambient shading from vertex normals.
"""
import argparse, os, numpy as np, pygltflib
from PIL import Image

ap = argparse.ArgumentParser()
ap.add_argument('glb'); ap.add_argument('out')
ap.add_argument('--view', default='front'); ap.add_argument('--zoom', type=float, default=1.0)
ap.add_argument('--size', type=int, default=900)
ap.add_argument('--ids', help='also write a .npy material-index map (opaque/mask pass)')
a = ap.parse_args()
g = pygltflib.GLTF2().load(a.glb); blob = g.binary_blob()
NC = {'SCALAR': 1, 'VEC2': 2, 'VEC3': 3, 'VEC4': 4}
DT = {5126: np.float32, 5125: np.uint32, 5123: np.uint16, 5121: np.uint8}
def acc(i):
    if i is None: return None
    x = g.accessors[i]; bv = g.bufferViews[x.bufferView]
    return np.frombuffer(blob, DT[x.componentType], x.count * NC[x.type], bv.byteOffset).reshape(x.count, -1)
cache = {}
def tex(i):
    uri = g.images[g.textures[i].source].uri
    if uri not in cache:
        cache[uri] = np.asarray(Image.open(os.path.join(os.path.dirname(a.glb), uri)).convert('RGBA')).astype(np.float32) / 255
    return cache[uri]

prims = []
allP = []
for p in [p for m in g.meshes for p in m.primitives]:
    P = acc(p.attributes.POSITION).astype(np.float64)
    allP.append(P); prims.append(p)
allP = np.concatenate(allP)
# engine Z-up: front = look along +x at (y, z); side = look along -y at (x, z)
proj = {'front': (1, 2, 0, -1, 1), 'back': (1, 2, 0, 1, -1), 'side': (0, 2, 1, 1, -1)}[a.view]
ix, iy, iz, zs, xs = proj
mn, mx = allP.min(0), allP.max(0)
if a.zoom < 1: mn = mn.copy(); mn[2] = mx[2] - (mx[2] - mn[2]) * a.zoom
sel_all = allP[(allP[:, 2] >= mn[2])]
lo = np.array([sel_all[:, ix].min(), mn[2]]); hi = np.array([sel_all[:, ix].max(), mx[2]])
S = a.size; scale = (S - 20) / (hi - lo).max()
def to_screen(P):
    x = (xs * (P[:, ix] - (lo[0] + hi[0]) / 2)) * scale + S / 2
    y = S / 2 - (P[:, iy] - (lo[1] + hi[1]) / 2) * scale
    return x, y, zs * P[:, iz]       # larger depth = closer
img = np.ones((S, S, 3), np.float32); zbuf = np.full((S, S), -np.inf); ids = np.full((S, S), -1, np.int32)
L = np.array([-0.6, 0.3, 0.75]); L /= np.linalg.norm(L)
light = {'front': np.array([-0.7, 0.3, 0.6]), 'back': np.array([0.7, 0.3, 0.6]), 'side': np.array([0.3, -0.7, 0.6])}[a.view]
light /= np.linalg.norm(light)

def draw(p, blend_pass):
    m = g.materials[p.material] if p.material is not None else None
    mode = (m.alphaMode or 'OPAQUE') if m else 'OPAQUE'
    if (mode == 'BLEND') != blend_pass: return
    P = acc(p.attributes.POSITION).astype(np.float64)
    N = acc(p.attributes.NORMAL); idx = acc(p.indices).reshape(-1, 3).astype(np.int64)
    t = m.pbrMetallicRoughness.baseColorTexture if m else None
    T = tex(t.index) if t else None
    UV = acc(p.attributes.TEXCOORD_1 if (t and t.texCoord == 1) else p.attributes.TEXCOORD_0) if t else None
    VC = acc(p.attributes.COLOR_0)
    base = np.array(m.pbrMetallicRoughness.baseColorFactor or [1, 1, 1, 1], np.float32) if m else np.ones(4, np.float32)
    cutoff = (m.alphaCutoff if m.alphaCutoff is not None else 0.5) if m else 0.5
    sx, sy, sz = to_screen(P)
    keep = (P[idx][:, :, 2] >= mn[2]).any(1)
    for tri in idx[keep]:
        x0, x1, x2 = sx[tri]; y0, y1, y2 = sy[tri]
        bx0, bx1 = int(max(min(x0, x1, x2), 0)), int(min(max(x0, x1, x2) + 1, S))
        by0, by1 = int(max(min(y0, y1, y2), 0)), int(min(max(y0, y1, y2) + 1, S))
        if bx0 >= bx1 or by0 >= by1: continue
        den = (y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2)
        if abs(den) < 1e-12: continue
        X, Y = np.meshgrid(np.arange(bx0, bx1) + 0.5, np.arange(by0, by1) + 0.5)
        w0 = ((y1 - y2) * (X - x2) + (x2 - x1) * (Y - y2)) / den
        w1 = ((y2 - y0) * (X - x2) + (x0 - x2) * (Y - y2)) / den
        w2 = 1 - w0 - w1
        inside = (w0 >= 0) & (w1 >= 0) & (w2 >= 0)
        if not inside.any(): continue
        z = w0 * sz[tri[0]] + w1 * sz[tri[1]] + w2 * sz[tri[2]]
        zb = zbuf[by0:by1, bx0:bx1]
        vis = inside & (z > zb)
        if not vis.any(): continue
        col = np.broadcast_to(base, vis.shape + (4,)).copy()
        if T is not None:
            u = w0 * UV[tri[0], 0] + w1 * UV[tri[1], 0] + w2 * UV[tri[2], 0]
            v = w0 * UV[tri[0], 1] + w1 * UV[tri[1], 1] + w2 * UV[tri[2], 1]
            h, w = T.shape[:2]
            col = col * T[np.clip(((v % 1) * h).astype(int), 0, h - 1), np.clip(((u % 1) * w).astype(int), 0, w - 1)]
        if VC is not None:
            vc = w0[..., None] * VC[tri[0], :3] + w1[..., None] * VC[tri[1], :3] + w2[..., None] * VC[tri[2], :3]
            col[..., :3] *= np.where(vc <= 0.0031308, vc * 12.92, 1.055 * np.clip(vc, 0, None) ** (1 / 2.4) - 0.055)
        if mode == 'MASK': vis &= col[..., 3] >= cutoff
        if not vis.any(): continue
        n = w0[..., None] * N[tri[0]] + w1[..., None] * N[tri[1]] + w2[..., None] * N[tri[2]]
        n /= np.linalg.norm(n, axis=-1, keepdims=True) + 1e-9
        shade = 0.35 + 0.65 * np.abs(n @ light)
        rgb = np.clip(col[..., :3] * shade[..., None], 0, 1)
        ib = img[by0:by1, bx0:bx1]
        if mode == 'BLEND':
            al = col[..., 3:4]
            ib[vis] = (rgb * al + ib * (1 - al))[vis]
        else:
            ib[vis] = rgb[vis]; zb[vis] = z[vis]; ids[by0:by1, bx0:bx1][vis] = p.material if p.material is not None else -1

for p in prims: draw(p, False)
for p in prims: draw(p, True)
Image.fromarray((img * 255).astype(np.uint8)).save(a.out)
if a.ids: np.save(a.ids, ids)
print('ok', a.out)
