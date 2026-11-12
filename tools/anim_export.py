#!/usr/bin/env python3
"""Export veclipse skeletal animations (*.anim.fbx.import, *.anim.gr2.import) to glTF, one .glb per animation,
each containing the matched model's mesh, skeleton and materials (copied from the model_export.py output) plus the
animation.

usage: python -I tools/anim_export.py <extracted_dir> <models_out> <anims_out> [--filter S] [-j N] [--force]
                                      [--fps N] [--min-coverage F] [--dry-run]

Matching: an animation names the bones it drives, but not the model. Each animation goes to the skinned model whose
skeleton contains the most of its bones (coverage). Many models share a skeleton (fox, fox_comms, fox_cinematics, ...),
so ties go to, in order: the model with the most of the animation's blendshapes; the fewest variant words in its name
(broken, comms, cinematics, ...) that the animation's path doesn't also contain; the most words shared with the
animation's path; the model the animation drives most of; the nearest folder; the shortest name.
Every match is listed in <anims_out>/_index.csv.

Joint channels are sampled at the animation's frame rate (1 / (time_step * oversampling), normally 30 or 60) with
LINEAR interpolation. Bones that never leave the bind pose get a two-key constant channel instead of a full one. Blendshape tracks become morph target
weight animation, matched to the mesh's morph target names.

Recentering: cinematic clips are animated in their shot's scene space and can start far from the origin. Every clip
is shifted so that its first frame sits where the model's bind pose does: horizontally by the frame-0 displacement of
the lowest common ancestor of all skinned joints (the pelvis for characters, the body bone for vehicles), vertically
so the lowest skinned joint is as high as in the bind pose. The shift is a
translation on the file's top-level node, so the bone animation itself is unchanged; the removed offset (engine
units, Z-up) is stored in the scene extras as `animation_recentered_by`.
"""
import argparse, csv, json, math, os, re, struct, sys
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

SUFFIXES = ('.anim.fbx.import', '.anim.gr2.import')


# ---- glb io ---------------------------------------------------------------------------------------------------------
def read_glb(path):
    b = open(path, 'rb').read()
    n = struct.unpack_from('<I', b, 12)[0]
    g = json.loads(b[20:20 + n])
    bin_ = b''
    o = 20 + n
    if o < len(b):
        m = struct.unpack_from('<I', b, o)[0]
        bin_ = b[o + 8:o + 8 + m]
    return g, bytearray(bin_)


def read_glb_json(path):
    with open(path, 'rb') as f:
        h = f.read(20)
        n = struct.unpack_from('<I', h, 12)[0]
        return json.loads(f.read(n))


def write_glb(path, g, bin_):
    while len(bin_) % 4: bin_ += b'\0'
    g['buffers'] = [{'byteLength': len(bin_)}]
    js = json.dumps(g, separators=(',', ':')).encode()
    js += b' ' * (-len(js) % 4)
    out = struct.pack('<III', 0x46546C67, 2, 12 + 8 + len(js) + 8 + len(bin_))
    out += struct.pack('<II', len(js), 0x4E4F534A) + js + struct.pack('<II', len(bin_), 0x004E4942) + bytes(bin_)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.%d.part' % os.getpid()
    with open(tmp, 'wb') as f: f.write(out)
    os.replace(tmp, path)


def add_accessor(g, bin_, arr, typ, minmax=False):
    arr = np.ascontiguousarray(arr, np.float32)
    while len(bin_) % 4: bin_ += b'\0'
    g['bufferViews'].append({'buffer': 0, 'byteOffset': len(bin_), 'byteLength': arr.nbytes})
    bin_ += arr.tobytes()
    acc = {'bufferView': len(g['bufferViews']) - 1, 'componentType': 5126, 'count': int(arr.shape[0]), 'type': typ}
    if minmax:
        a2 = arr.reshape(arr.shape[0], -1)
        acc['min'] = [float(x) for x in a2.min(0)]; acc['max'] = [float(x) for x in a2.max(0)]
    g['accessors'].append(acc)
    return len(g['accessors']) - 1


# ---- model index and matching ---------------------------------------------------------------------------------------
def model_index(models_out):
    """[(data path of the model, glb path, joint names, morph target names)] for every skinned model."""
    out = []
    for dp, dn, fn in os.walk(models_out):
        for f in fn:
            if not f.endswith('.glb') or f.startswith('._'): continue
            p = os.path.join(dp, f)
            try:
                g = read_glb_json(p)
            except Exception:
                continue
            if not g.get('skins'): continue
            joints = [g['nodes'][j].get('name') for s in g['skins'] for j in s['joints']]
            targets = [t for m in g.get('meshes', []) for t in (m.get('extras') or {}).get('targetNames', [])]
            rel = os.path.relpath(p, models_out)[:-len('.glb')]
            out.append((rel, p, set(joints), set(targets)))
    return out


SYN = {'cin': 'cinematics', 'cine': 'cinematics', 'cinematic': 'cinematics', 'damage': 'damaged'}
VARIANT = {'broken', 'damaged', 'destroyed', 'comms', 'aviators', 'cinematics', 'prelude', 'holo', 'hologram',
           'mp', 'portrait', 'navi', 'low', 'lod', 'proxy', 'dead', 'wreck'}


def tokens(s):
    out = set()
    for t in re.split(r'[^a-z]+', s.lower()):
        if len(t) > 1: out.add(SYN.get(t, t))
    return out - {'anim', 'fbx', 'gr', 'game', 'assets', 'animations', 'models', 'data', 'eclipse', 'shot', 'chr',
                  'sk', 'sm', 'import'}


def match(anim_rel, names, models, shapes=()):
    best = None
    adir = os.path.dirname(anim_rel).split('/')
    at = tokens(anim_rel)
    shapes = set(shapes)
    for rel, glb, joints, targets in models:
        hit = len(names & joints)
        if not hit: continue
        cov = hit / len(names)
        used = hit / len(joints)
        mdir = os.path.dirname(rel).split('/')
        prox = 0
        for x, y in zip(adir, mdir):
            if x != y: break
            prox += 1
        mt = tokens(os.path.basename(rel))
        key = (round(cov, 2), len(shapes & targets), -len((mt & VARIANT) - at), len(mt & at), round(used, 2), prox,
               -len(os.path.basename(rel)))
        if best is None or key > best[0]:
            best = (key, rel, glb, cov, used)
    return best


# ---- animation ------------------------------------------------------------------------------------------------------
def continuous_quats(q):
    q = q / np.linalg.norm(q, axis=1, keepdims=True)
    if len(q) > 1:
        flip = np.cumprod(np.where((q[1:] * q[:-1]).sum(1) < 0, -1.0, 1.0))
        q[1:] *= flip[:, None]
    return q


def scale_from_ss(m):
    m = m.reshape(-1, 3, 3)
    return np.stack([m[:, 0, 0], m[:, 1, 1], m[:, 2, 2]], 1)


def _quat_mat(q):
    x, y, z, w = np.asarray(q, float) / np.linalg.norm(q)
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                     [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                     [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


def _local(trs):
    M = np.eye(4)
    if 'rotation' in trs: M[:3, :3] = _quat_mat(trs['rotation'])
    if 'scale' in trs: M[:3, :3] = M[:3, :3] @ np.diag(trs['scale'])
    if 'translation' in trs: M[:3, 3] = trs['translation']
    return M


def skinned_joints(g, bin_):
    """Joint node indices that carry vertex weight in any primitive."""
    DT = {5121: np.uint8, 5123: np.uint16, 5125: np.uint32, 5126: np.float32}
    B = bytes(bin_)
    def acc(i):
        x = g['accessors'][i]; bv = g['bufferViews'][x['bufferView']]
        return np.frombuffer(B, DT[x['componentType']], x['count'] * 4,
                             bv.get('byteOffset', 0) + x.get('byteOffset', 0)).reshape(x['count'], 4)
    used = set()
    for nd in g['nodes']:
        if 'mesh' not in nd or 'skin' not in nd: continue
        joints = g['skins'][nd['skin']]['joints']
        seen = set()
        for p in g['meshes'][nd['mesh']]['primitives']:
            a = p['attributes']
            if 'JOINTS_0' not in a or (a['JOINTS_0'], a['WEIGHTS_0']) in seen: continue
            seen.add((a['JOINTS_0'], a['WEIGHTS_0']))
            J, W = acc(a['JOINTS_0']), acc(a['WEIGHTS_0'])
            used.update(joints[j] for j in np.unique(J[W > 0]))
    return used


def recenter(g, bin_, first):
    """Shift the top-level node so frame 0 lines up with the bind pose; returns the removed offset (engine space)."""
    nodes = g['nodes']
    roots = g['scenes'][g.get('scene', 0)]['nodes']
    parent = {c: i for i, nd in enumerate(nodes) for c in nd.get('children', [])}
    def world(i, posed, cache):
        if i in roots: return np.eye(4)          # engine space: leave out the top-level (Z-up -> Y-up) node
        if i not in cache:
            trs = {k: np.asarray(nodes[i][k], float) for k in ('translation', 'rotation', 'scale') if k in nodes[i]}
            if posed: trs.update(first.get(i, {}))
            cache[i] = world(parent[i], posed, cache) @ _local(trs) if i in parent else _local(trs)
        return cache[i]
    joints = sorted(skinned_joints(g, bin_)) or [j for s in g.get('skins', []) for j in s['joints']]
    if not joints: return np.zeros(3)
    cb, cp = {}, {}
    B = np.array([world(j, False, cb)[:3, 3] for j in joints])
    P = np.array([world(j, True, cp)[:3, 3] for j in joints])
    def chain(i):
        out = [i]
        while i in parent and i not in roots: i = parent[i]; out.append(i)
        return out[::-1]
    chains = [chain(j) for j in joints]
    lca = None
    for k in range(min(map(len, chains))):
        if len({c[k] for c in chains}) > 1: break
        lca = chains[0][k]
    if lca is None or lca in roots:
        h = (P[:, :2].min(0) + P[:, :2].max(0)) / 2 - (B[:, :2].min(0) + B[:, :2].max(0)) / 2
    else:
        h = (world(lca, True, cp) - world(lca, False, cb))[:2, 3]
    off = np.r_[h, P[:, 2].min() - B[:, 2].min()]
    if np.abs(off).max() < 1e-6: return np.zeros(3)
    for r in roots:
        nd = nodes[r]
        R = _quat_mat(nd['rotation']) if 'rotation' in nd else np.eye(3)
        S = np.diag(nd['scale']) if 'scale' in nd else np.eye(3)
        T = np.asarray(nd.get('translation', [0, 0, 0]), float) - R @ S @ off
        nd['translation'] = [float(x) for x in T]
    return off


def export_one(args):
    src, out, model_glb, model_rel, extracted, fps_override = args
    import granny_anim as G
    v, anims = G.load(src)
    if not anims: return 'empty', src
    a = anims[0]
    g, bin_ = read_glb(model_glb)
    # textures: re-relativise URIs from the model's folder to this file's folder
    for im in g.get('images', []):
        if 'uri' in im and not im['uri'].startswith('data:'):
            ab = os.path.normpath(os.path.join(os.path.dirname(model_glb), im['uri']))
            im['uri'] = os.path.relpath(ab, os.path.dirname(out)).replace(os.sep, '/')
    nodes_by_name = {}
    for s in g['skins']:
        for j in s['joints']:
            nodes_by_name.setdefault(g['nodes'][j].get('name'), j)
    fps = fps_override or (1.0 / (a['time_step'] * a['oversampling']) if a['time_step'] and a['oversampling'] else 30)
    fps = float(round(fps)) if abs(fps - round(fps)) < 0.5 else fps
    dur = max(float(a['duration']), 0.0)
    n = max(2, int(math.ceil(dur * fps - 1e-6)) + 1)
    t = np.minimum(np.arange(n) / fps, dur)
    t_acc = None
    samplers, channels = [], []

    def channel(node, path, values, typ):
        nonlocal t_acc
        if t_acc is None: t_acc = add_accessor(g, bin_, t.reshape(-1, 1), 'SCALAR', minmax=True)
        o = add_accessor(g, bin_, values, typ)
        samplers.append({'input': t_acc, 'output': o, 'interpolation': 'LINEAR'})
        channels.append({'sampler': len(samplers) - 1, 'target': {'node': node, 'path': path}})

    driven = 0
    first = {}                    # node -> {path: value at frame 0}, for recentering
    for grp in a['track_groups']:
        for tr in grp['tracks']:
            node = nodes_by_name.get(tr['name'])
            if node is None: continue
            driven += 1
            nd = g['nodes'][node]
            rest = {'rotation': np.array(nd.get('rotation', [0, 0, 0, 1])),
                    'translation': np.array(nd.get('translation', [0, 0, 0])),
                    'scale': np.array(nd.get('scale', [1, 1, 1]))}
            vals = {}
            if tr['orientation'] is not None:
                q = continuous_quats(tr['orientation'].sample(t))
                if np.dot(q[0], rest['rotation']) < 0: q = -q
                vals['rotation'] = (q, 'VEC4')
            if tr['position'] is not None:
                vals['translation'] = (tr['position'].sample(t), 'VEC3')
            if tr['scale_shear'] is not None:
                vals['scale'] = (scale_from_ss(tr['scale_shear'].sample(t)), 'VEC3')
            for path, (val, typ) in vals.items():
                first.setdefault(node, {})[path] = val[0]
                r = rest[path]
                if path == 'rotation':
                    same = np.all(np.abs(np.abs((val * r).sum(1)) - 1) < 1e-6)
                else:
                    same = np.all(np.abs(val - r) <= 1e-5 * max(1.0, float(np.abs(r).max())))
                if same: continue
                channel(node, path, val, typ)
    # blendshapes -> morph target weights on every mesh node that has those targets
    bs = {b['vector_track_index']: b['blendshape_name'] for b in v.get('blendshape_tracks') or []}
    vtracks = [vt for grp in a['track_groups'] for vt in grp.get('vector_tracks', [])]
    morphs = 0
    if bs and vtracks:
        for ni, nd in enumerate(g['nodes']):
            if 'mesh' not in nd: continue
            names = (g['meshes'][nd['mesh']].get('extras') or {}).get('targetNames') or []
            if not names: continue
            w = np.zeros((n, len(names)))
            hit = 0
            for idx, bname in bs.items():
                if idx < len(vtracks) and bname in names and vtracks[idx]['curve'] is not None:
                    w[:, names.index(bname)] = vtracks[idx]['curve'].sample(t)[:, 0]
                    hit += 1
            if hit:
                channel(ni, 'weights', w.reshape(-1, 1), 'SCALAR')
                morphs += hit
    # every joint gets rotation/translation/scale channels: bones that stay at rest get a two-key constant. glTF
    # viewers don't need this, but converters to USD (macOS Preview / Quick Look) otherwise leave those bones at
    # their bind position in the skeleton's space, so their skin stays behind when the character moves.
    have = {(c['target']['node'], c['target']['path']) for c in channels}
    joints = sorted({j for s_ in g['skins'] for j in s_['joints']})
    if channels and joints:
        t2 = add_accessor(g, bin_, np.array([[0.0], [float(t[-1])]]), 'SCALAR', minmax=True)
        for j in joints:
            nd = g['nodes'][j]
            for path, typ, rest in (('rotation', 'VEC4', nd.get('rotation', [0, 0, 0, 1])),
                                    ('translation', 'VEC3', nd.get('translation', [0, 0, 0])),
                                    ('scale', 'VEC3', nd.get('scale', [1, 1, 1]))):
                if (j, path) in have: continue
                o = add_accessor(g, bin_, np.array([rest, rest], np.float32), typ)
                samplers.append({'input': t2, 'output': o, 'interpolation': 'LINEAR'})
                channels.append({'sampler': len(samplers) - 1, 'target': {'node': j, 'path': path}})
    off = recenter(g, bin_, first)
    name = os.path.basename(src)
    for s in SUFFIXES:
        if name.endswith(s): name = name[:-len(s)]
    g['animations'] = [{'name': name, 'samplers': samplers, 'channels': channels}] if channels else []
    sc = g['scenes'][g.get('scene', 0)]
    sc.setdefault('extras', {}).update({'animation_source': '/' + os.path.relpath(src, extracted).replace(os.sep, '/'),
                                        'animation_model': '/' + model_rel, 'animation_fps': fps,
                                        'animation_duration': dur,
                                        'animation_recentered_by': [round(float(x), 4) for x in off]})
    g['asset']['generator'] = 'SF anim_export.py'
    write_glb(out, g, bin_)
    return 'ok', (out, driven, len(channels), morphs, n)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('extracted'); ap.add_argument('models_out'); ap.add_argument('out')
    ap.add_argument('--filter', default=None)
    ap.add_argument('-j', type=int, default=os.cpu_count() or 4)
    ap.add_argument('--force', action='store_true')
    ap.add_argument('--fps', type=float, default=None, help='sample at this rate instead of the source rate')
    ap.add_argument('--dry-run', action='store_true', help='only match, write _index.csv and estimate the output size')
    ap.add_argument('--min-coverage', type=float, default=0.5,
                    help='skip animations whose best model has fewer than this fraction of their bones')
    a = ap.parse_args()
    import granny_anim as G
    files = sorted(os.path.join(dp, f) for dp, dn, fn in os.walk(os.path.join(a.extracted, 'data'))
                   for f in fn if f.endswith(SUFFIXES) and not f.startswith('._'))
    if a.filter: files = [f for f in files if a.filter in f]
    print('animations', len(files), flush=True)
    models = model_index(a.models_out)
    print('skinned models', len(models), flush=True)
    from concurrent.futures import ProcessPoolExecutor

    jobs, rows = [], []
    with ProcessPoolExecutor(a.j) as ex:
        for src, (names, shapes) in zip(files, ex.map(_track_names, files, chunksize=16)):
            rel = os.path.relpath(src, a.extracted).replace(os.sep, '/')
            base = rel
            for s in SUFFIXES:
                if base.endswith(s): base = base[:-len('.import')]
            out = os.path.join(a.out, base + '.glb')
            if not names:
                rows.append([rel, '', '', 0, 'empty animation']); continue
            m = match(rel, names, models, shapes)
            if not m or m[3] < a.min_coverage:
                rows.append([rel, m[1] if m else '', '', round(m[3], 3) if m else 0, 'no matching model']); continue
            key, mrel, mglb, cov, used = m
            rows.append([rel, mrel, os.path.relpath(out, a.out).replace(os.sep, '/'), round(cov, 3), ''])
            if os.path.exists(out) and not a.force: continue
            jobs.append((src, out, mglb, mrel, a.extracted, a.fps))
        print('to export', len(jobs), flush=True)
        if a.dry_run:
            est = sum(os.path.getsize(j[2]) for j in jobs)
            print('estimated size of the models alone: %.1f GB' % (est / 1e9))
            jobs = []
        stats = {}; fails = []
        for i, (st, info) in enumerate(ex.map(_safe_export, jobs, chunksize=4), 1):
            stats[st] = stats.get(st, 0) + 1
            if st == 'fail': fails.append(info)
            if i % 250 == 0: print(i, len(jobs), stats, flush=True)
    print('done', stats)
    os.makedirs(a.out, exist_ok=True)
    if not a.filter:
        with open(os.path.join(a.out, '_index.csv'), 'w', newline='', encoding='utf-8') as fh:
            w = csv.writer(fh)
            w.writerow(['animation', 'model', 'glb', 'bone_coverage', 'note'])
            w.writerows(rows)
    if fails:
        with open(os.path.join(a.out, '_failures.txt'), 'w') as fh: fh.write('\n'.join(map(str, fails)))
        print('failures written to', os.path.join(a.out, '_failures.txt'))
    print('unmatched', sum(1 for r in rows if r[4]))


def _track_names(src):
    import granny_anim as G
    try:
        v, anims = G.load(src)
    except Exception:
        return set(), set()
    # cinematic clips also carry the scene's group nodes (VEHICLES > Vipers_Grp > viper12 > root ...); they are
    # identity transforms, so leave them out of the bone matching
    tracks = [tr for an in anims for grp in an['track_groups'] for tr in grp['tracks']]
    ident = lambda tr: all(tr[k] is None or tr[k].kind == 'DaIdentity' for k in ('orientation', 'position', 'scale_shear'))
    names = {tr['name'] for tr in tracks if not ident(tr)} or {tr['name'] for tr in tracks}
    return names, {b['blendshape_name'] for b in v.get('blendshape_tracks') or []}


def _safe_export(job):
    try:
        return export_one(job)
    except Exception as e:
        import traceback
        return 'fail', '%s: %s' % (job[0], traceback.format_exc(limit=3))


if __name__ == '__main__':
    main()
