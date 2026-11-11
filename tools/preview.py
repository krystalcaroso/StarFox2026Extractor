#!/usr/bin/env python3
"""Render a quick shaded preview PNG of a .glb (bind pose): usage preview.py in.glb out.png"""
import sys, numpy as np, trimesh
import matplotlib; matplotlib.use('Agg')
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d.art3d import Poly3DCollection
sc = trimesh.load(sys.argv[1], force='scene')
meshes = sc.dump() if hasattr(sc, 'dump') else [sc]
V = []; F = []; C = []; off = 0
cmap = plt.get_cmap('tab10')
for k, m in enumerate(meshes):
    V.append(m.vertices); F.append(m.faces + off); off += len(m.vertices)
    C += [cmap(k % 10)] * len(m.faces)
V = np.concatenate(V); F = np.concatenate(F); C = np.array(C)
# subsample faces for speed
if len(F) > 120000:
    sel = np.random.default_rng(0).choice(len(F), 120000, replace=False); F = F[sel]; C = C[sel]
fig = plt.figure(figsize=(12, 6), dpi=110)
tri = V[F]
n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0]); n /= np.linalg.norm(n, axis=1, keepdims=True) + 1e-12
for j, (az, title) in enumerate(((0, 'side'), (90, 'front'))):
    ax = fig.add_subplot(1, 2, j + 1, projection='3d')
    L = np.array([np.sin(np.radians(az)) * 0.6, 0.6, np.cos(np.radians(az)) * 0.8]); L /= np.linalg.norm(L)
    shade = np.clip(np.abs(n @ L), 0.15, 1)[:, None]
    col = C.copy(); col[:, :3] *= shade
    pc = Poly3DCollection(tri[:, :, [0, 2, 1]], facecolors=col, edgecolors='none')
    ax.add_collection3d(pc)
    mn, mx = V.min(0), V.max(0); c = (mn + mx) / 2; r = (mx - mn).max() / 2
    ax.set_xlim(c[0] - r, c[0] + r); ax.set_ylim(c[2] - r, c[2] + r); ax.set_zlim(c[1] - r, c[1] + r)
    ax.view_init(elev=5, azim=-90 + az); ax.set_axis_off(); ax.set_title(title)
plt.tight_layout(); plt.savefig(sys.argv[2])
print('verts', len(V), 'faces', len(F), 'bounds', V.min(0).round(2), V.max(0).round(2))
