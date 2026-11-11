#!/usr/bin/env python3
"""Point-sampled textured preview of a .glb in bind pose (no skinning).
usage: preview_tex.py in.glb out.png [--head]   (--head: zoom on the top 20% of the model)
Honours COLOR_0 and MASK alpha cutouts. Accessor data is engine Z-up: front view = (y, z) seen from -x, side = (x, z)."""
import sys, os, numpy as np, pygltflib
from PIL import Image
import matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt
path = sys.argv[1]
g = pygltflib.GLTF2().load(path); blob = g.binary_blob()
def acc(i):
    a = g.accessors[i]; bv = g.bufferViews[a.bufferView]; n = {'VEC3': 3, 'VEC2': 2, 'VEC4': 4}[a.type]
    return np.frombuffer(blob, np.float32, a.count * n, bv.byteOffset).reshape(-1, n)
P = []; C = []; cache = {}
def img(uri):
    if uri not in cache: cache[uri] = np.asarray(Image.open(os.path.join(os.path.dirname(path), uri)).convert('RGBA')).astype(np.float32) / 255
    return cache[uri]
for p in g.meshes[0].primitives:
    pos = acc(p.attributes.POSITION)
    m = g.materials[p.material]; t = m.pbrMetallicRoughness.baseColorTexture
    c = np.ones((len(pos), 4), np.float32); c[:, :3] = [0.6, 0.6, 0.9]
    if t:
        im = img(g.images[g.textures[t.index].source].uri)
        uv = acc(p.attributes.TEXCOORD_1 if t.texCoord == 1 else p.attributes.TEXCOORD_0); h, w = im.shape[:2]
        c = im[np.clip(((uv[:, 1] % 1) * h).astype(int), 0, h - 1), np.clip(((uv[:, 0] % 1) * w).astype(int), 0, w - 1)].copy()
    if p.attributes.COLOR_0 is not None:
        vc = acc(p.attributes.COLOR_0)[:, :3]
        c[:, :3] *= np.where(vc <= 0.0031308, vc * 12.92, 1.055 * vc ** (1 / 2.4) - 0.055)   # linear -> sRGB for display
    keep = c[:, 3] >= (m.alphaCutoff or 0.5) if m.alphaMode == 'MASK' else np.ones(len(pos), bool)
    if m.alphaMode == 'BLEND': keep &= c[:, 3] > 0.5
    P.append(pos[keep]); C.append(c[keep, :3])
P = np.concatenate(P); C = np.clip(np.concatenate(C), 0, 1)
if '--head' in sys.argv:
    top = P[:, 2].max(); span = top - P[:, 2].min(); s = P[:, 2] > top - 0.2 * span; P, C = P[s], C[s]
sel = np.random.default_rng(0).choice(len(P), min(len(P), 300000), replace=False)
fig, ax = plt.subplots(1, 2, figsize=(12, 7))
for a, (i, j, depth, sgn) in zip(ax, [(1, 2, 0, 1), (0, 2, 1, -1)]):
    q = P[sel]; o = np.argsort(sgn * q[:, depth])
    a.scatter(q[o, i], q[o, j], c=C[sel][o], s=1.5 if '--head' in sys.argv else 0.6, linewidths=0); a.set_aspect('equal'); a.axis('off')
plt.tight_layout(); plt.savefig(sys.argv[2], dpi=80)
