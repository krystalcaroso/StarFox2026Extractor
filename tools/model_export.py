#!/usr/bin/env python3
"""Export veclipse render models (.gr2.import / .fbx.import -> _vrender_model_t) to glTF 2.0 (.glb).

usage: python -I tools/model_export.py <extracted_dir> <out_dir> [--textures textures_out] [--filter SUBSTR] [--lods] [-j N] [--force]

Per draw call, GPU buffer `<model>.<N>.nvn_gen2.raw` (N = draw-call index) holds separate streams at
vertex_data_stream_offsets: [indices, P, C, T, N, (S weights), (S joints), end]
  P  snorm16x4  xyz * quantization_scale + quantization_offset, w = tangent handedness (stored sign)
  C  rgba8 unorm
  T  snorm16x4  uv0.xy, uv1.xy * uv_quantization_scale + uv_quantization_offset
  N  half4      octahedral normal (xy), octahedral tangent (zw), values in [0,1]
  S  u8x4 weights (sum 255), u8x4 joint indices (into the skeleton's bone list)
Indices are u16 or u32 (decided from the stream size; the enum is not reliable).
The skeleton is an embedded Granny file_info blob (`skeleton_data`), see granny_skel.py.
Models are Z-up; a root node rotates them to glTF's Y-up. Units are left as stored.
"""
import argparse, json, os, struct, sys, traceback
from concurrent.futures import ProcessPoolExecutor
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vbin, granny_skel, shell_fur

FORMATS = {  # stream letters after the index stream
    'k_vrender_vertex_format_p': 'p', 'k_vrender_vertex_format_ps': 'ps', 'k_vrender_vertex_format_pc': 'pc',
    'k_vrender_vertex_format_pct': 'pct', 'k_vrender_vertex_format_pctn': 'pctn',
    'k_vrender_vertex_format_pctns': 'pctns', 'k_vrender_vertex_format_pctns_bpn': 'pctns',
}
STREAM = {'p': ['P'], 'c': ['C'], 't': ['T'], 'n': ['N'], 's': ['W', 'J']}
STRIDE = {'P': 8, 'C': 4, 'T': 8, 'N': 8, 'W': 4, 'J': 4}

def oct_decode(e):
    e = e * 2.0 - 1.0
    x, y = e[:, 0].copy(), e[:, 1].copy()
    z = 1.0 - np.abs(x) - np.abs(y)
    neg = z < 0
    x[neg], y[neg] = (1 - np.abs(e[neg, 1])) * np.sign(e[neg, 0]), (1 - np.abs(e[neg, 0])) * np.sign(e[neg, 1])
    v = np.stack([x, y, z], 1)
    n = np.linalg.norm(v, axis=1, keepdims=True); n[n == 0] = 1
    return (v / n).astype(np.float32)

class Glb:
    def __init__(self):
        self.bin = bytearray()
        self.g = {'asset': {'version': '2.0', 'generator': 'SF model_export.py'}, 'scene': 0,
                  'scenes': [{'nodes': []}], 'nodes': [], 'meshes': [], 'materials': [],
                  'buffers': [], 'bufferViews': [], 'accessors': []}
    def view(self, data, target=None):
        while len(self.bin) % 4: self.bin.append(0)
        bv = {'buffer': 0, 'byteOffset': len(self.bin), 'byteLength': len(data)}
        if target: bv['target'] = target
        self.bin += data
        self.g['bufferViews'].append(bv)
        return len(self.g['bufferViews']) - 1
    def acc(self, arr, typ, ctype=None, normalized=False, target=34962, minmax=False):
        arr = np.ascontiguousarray(arr)
        ctype = ctype or {np.float32: 5126, np.uint8: 5121, np.uint16: 5123, np.uint32: 5125}[arr.dtype.type]
        a = {'bufferView': self.view(arr.tobytes(), target), 'componentType': ctype,
             'count': int(arr.shape[0]), 'type': typ}
        if normalized: a['normalized'] = True
        if minmax:
            a['min'] = [float(x) for x in arr.min(0)]; a['max'] = [float(x) for x in arr.max(0)]
        self.g['accessors'].append(a)
        return len(self.g['accessors']) - 1
    def node(self, **kw):
        self.g['nodes'].append(kw); return len(self.g['nodes']) - 1
    def write(self, path):
        g = self.g
        for k in [k for k, v in g.items() if v == []]: del g[k]
        while len(self.bin) % 4: self.bin.append(0)
        g['buffers'] = [{'byteLength': len(self.bin)}]
        js = json.dumps(g, separators=(',', ':')).encode()
        js += b' ' * ((4 - len(js) % 4) % 4)
        tmp = path + '.part'
        with open(tmp, 'wb') as f:
            f.write(struct.pack('<III', 0x46546C67, 2, 12 + 8 + len(js) + 8 + len(self.bin)))
            f.write(struct.pack('<II', len(js), 0x4E4F534A)); f.write(js)
            f.write(struct.pack('<II', len(self.bin), 0x004E4942)); f.write(self.bin)
        os.replace(tmp, path)

GUIDS = {}      # guid -> '/data/...' path   (work/guid_index.json, see guid_index.py)
LINKS = {}      # mesh guid -> [(model_path, {slot: info})]

def _json(path):
    try: return json.load(open(path, encoding='utf-8', errors='replace'))
    except Exception: return None

# Unbound texture parameters fall back to the shader graph's default (read from the compiled graph's imports)
SG_DEFAULTS = {
    'sg_chr_shared_furcard.shader_graph.import':
        {'_VAHAlpha': '/data/eclipse/characters/shared/textures/t_chr_shared_fur_cards_var.png.import'},
}
# Fur-card graphs (same depth pre-pass, disassembled): a = _VAHAlpha.a at UV0, c = AlphaClip (default 0.5)
#   v = c < 0.5 ? 2a*c : 1 - (2-2a)(1-c);  keep if v >= 0.5   <=>   a >= cutoff(c) below
FURCARD_SGS = ('sg_chr_shared_furcard.shader_graph.import', 'sg_chr_fox_fur.shader_graph.import')
def card_cutoff(c):
    c = min(max(float(c), 1e-6), 1 - 1e-6)
    return min(1.0, 0.25 / c) if c < 0.5 else max(0.0, 1 - 0.25 / (1 - c))

def tinted_cutout_png(cutout_png, color_lin, size=1024):
    """Strand cutout pre-multiplied by a flat colour (sRGB baked into RGB), for viewers that ignore baseColorFactor."""
    c = np.clip(np.asarray(color_lin, np.float64), 0, 1)
    srgb = np.where(c <= 0.0031308, c * 12.92, 1.055 * c ** (1 / 2.4) - 0.055)
    rgb8 = tuple(int(round(x * 255)) for x in srgb)
    out = cutout_png[:-4] + '.tint_%02x%02x%02x.png' % rgb8
    if not os.path.exists(out):
        from PIL import Image
        a = Image.open(cutout_png).getchannel('A').resize((size, size), Image.BILINEAR)
        im = Image.merge('RGBA', [Image.new('L', a.size, v) for v in rgb8] + [a])
        tmp = out + '.%d.part' % os.getpid()
        im.save(tmp, format='PNG', compress_level=6); os.replace(tmp, out)
    return out

def strand_cutout_png(vah_png):
    """Fur-card strand atlas (R/G/B masks, A = strand alpha) -> white RGB + A, cached next to the source."""
    out = vah_png[:-4] + '.cutout.png'
    if not os.path.exists(out) or os.path.getmtime(out) < os.path.getmtime(vah_png):
        from PIL import Image
        a = np.asarray(Image.open(vah_png).convert('RGBA'))
        o = np.empty_like(a); o[..., :3] = 255; o[..., 3] = a[..., 3]
        tmp = out + '.%d.part' % os.getpid()
        Image.fromarray(o, 'RGBA').save(tmp, format='PNG', compress_level=6); os.replace(tmp, out)
    return out

_IMG_CACHE = {}
def sample_png(png, uv):
    """Nearest-texel sample of an sRGB PNG at UVs (repeat), returned as linear RGB float32."""
    from PIL import Image
    if png not in _IMG_CACHE:
        _IMG_CACHE.clear(); _IMG_CACHE[png] = np.asarray(Image.open(png).convert('RGB')).astype(np.float32) / 255
    im = _IMG_CACHE[png]; h, w = im.shape[:2]
    c = im[np.clip(((uv[:, 1] % 1) * h).astype(int), 0, h - 1), np.clip(((uv[:, 0] % 1) * w).astype(int), 0, w - 1)]
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4).astype(np.float32)

def material_info(extracted, guid):
    p = GUIDS.get(guid)
    info = {'material_guid': guid, 'material': p}
    if p and p.endswith(('.material_instance', '.material')):
        j = _json(os.path.join(extracted, p.lstrip('/')))
        if j:
            for o in j['segments'][0]['exported_objects']:
                if o.get('$guid') != guid: continue
                tex = {}
                for k, v in (o.get('texture_bindings') or {}).items():
                    if isinstance(v, dict) and v.get('texture'):
                        tex[k] = GUIDS.get(v['texture'], v['texture'])
                sg = GUIDS.get(o.get('shader_graph') or '')
                if sg:
                    info['shader_graph'] = sg
                    for k, v in SG_DEFAULTS.get(os.path.basename(sg), {}).items(): tex.setdefault(k, v)
                info['textures'] = tex
                for k in ('render_type', 'cull_mode'):
                    if k in o: info[k] = o[k]
                ub = o.get('uniform_buffer_data') or {}
                info['uniforms'] = {k: v for k, v in ub.items() if k != 'vtable'}
                tint = ub.get('Glass_Tint') or ub.get('glass_tint')
                if isinstance(tint, dict) and {'x', 'y', 'z'} <= set(tint): info['glass_tint'] = [tint['x'], tint['y'], tint['z']]
    return info

def build_links(extracted):
    """Scan .model assets: model GUID -> material overrides per slot."""
    for dp, dn, fn in os.walk(os.path.join(extracted, 'data')):
        for f in fn:
            if not f.endswith('.model') or f.startswith('._'): continue
            j = _json(os.path.join(dp, f))
            if not j: continue
            for o in j['segments'][0].get('exported_objects', []):
                mg = o.get('model')
                if not mg: continue
                slots = {}
                for mo in o.get('material_overrides', []):
                    ent = {'name': mo.get('name'), 'material_guid': mo.get('material')}
                    slots[mo.get('slot', 0)] = ent
                    if mo.get('name_hash') is not None: slots[('hash', mo['name_hash'])] = ent
                LINKS.setdefault(mg, []).append(('/' + os.path.relpath(os.path.join(dp, f), extracted), slots))

EXTRACTED = [None]
FUR = [True]
FUR_FACTORS = [True]    # default: card colour / shell metal+AO via material factors (24 clusters);
                        # --fur-baked-colors: pre-tinted card textures + constant fur roughness (for viewers ignoring factors)     # bake shell fur (shell_fur.py); --no-fur leaves the coincident layers untouched
TEXDIR = [None]  # textures_out root (texture_export.py output); None = don't link textures
ROLE_NAMES = {   # glTF role -> binding names (lowercased, leading '_' stripped), in priority order
    'base': ['albedo', 'basecolor', 'base_color', 'diffuse', 'bc_01', 'texture', 'tile_texture_albedo', 'detailed_albedo', 'color', 'macro_albedo'],
    'normal': ['normals', 'normal', 'n_01', 'unique_normal', 'tile_texture_normal', 'tiletexture_normal', 'normal_map', 'macro_normal'],
    'rmao': ['roughness_metal_ao', 'rmao', 'rmao_01', 'tile_texture_rmao', 'tiletexture_rmao', 'macro_rmao'],
    'emissive': ['emissive', 'unique_emissive', 'tiletexture_emissive'],
}

def tex_png(asset_path):
    """'/data/.../x.png.import' -> exported PNG path under TEXDIR (or None)."""
    if not TEXDIR[0] or not asset_path or not asset_path.endswith('.import'): return None
    import texture_export
    p = texture_export.out_base_for(os.path.join(EXTRACTED[0], asset_path.lstrip('/')), EXTRACTED[0], TEXDIR[0]) + '.png'
    return p if os.path.exists(p) else None

def orm_png(rmao_png):
    """RMAO (R=roughness, G=metal, B=AO) -> glTF ORM (R=AO, G=roughness, B=metal), cached next to the source."""
    out = rmao_png[:-4] + '.orm.png'
    if not os.path.exists(out) or os.path.getmtime(out) < os.path.getmtime(rmao_png):
        from PIL import Image
        a = np.asarray(Image.open(rmao_png).convert('RGB'))
        tmp = out + '.%d.part' % os.getpid()
        Image.fromarray(np.ascontiguousarray(a[..., [2, 0, 1]]), 'RGB').save(tmp, format='PNG', compress_level=6)
        os.replace(tmp, out)
    return out

def axes(v): return np.array(v['axes'], dtype=np.float64)

def decode_drawcall(m, dc, raw):
    fmt = FORMATS.get(dc['format'])
    if fmt is None: raise ValueError('unsupported vertex format ' + dc['format'])
    so = dc['vertex_data_stream_offsets']; n = dc['vertex_count']; ic = dc['index_count']
    names = [s for ch in fmt for s in STREAM[ch]]
    isz = so[1] - so[0]
    iw = 4 if isz >= 4 * ic else 2
    idx = np.frombuffer(raw, '<u4' if iw == 4 else '<u2', ic, so[0]).astype(np.uint32)
    out = {'indices': idx}
    for k, nm in enumerate(names):
        a, b = so[1 + k], (so[2 + k] if 2 + k < len(so) else len(raw))
        if b - a < STRIDE[nm] * n: raise ValueError('stream %s too small (%d < %d)' % (nm, b - a, STRIDE[nm] * n))
        seg = raw[a:a + STRIDE[nm] * n]
        if nm == 'P':
            p = np.frombuffer(seg, '<i2').reshape(n, 4).astype(np.float64) / 32767.0
            out['POSITION'] = (p[:, :3] * axes(m['quantization_scale']) + axes(m['quantization_offset'])).astype(np.float32)
            out['_hand'] = np.where(p[:, 3] < 0, 1.0, -1.0).astype(np.float32)   # glTF w = -stored sign
        elif nm == 'C':
            out['COLOR'] = np.frombuffer(seg, 'u1').reshape(n, 4)
        elif nm == 'T':
            t = np.frombuffer(seg, '<i2').reshape(n, 4).astype(np.float64) / 32767.0
            s, o = axes(m['uv_quantization_scale']), axes(m['uv_quantization_offset'])
            out['TEXCOORD_0'] = (t[:, :2] * s + o).astype(np.float32)
            uv1 = (t[:, 2:] * s + o).astype(np.float32)
            if np.ptp(uv1, axis=0).max() > 1e-6: out['TEXCOORD_1'] = uv1
        elif nm == 'N':
            h = np.frombuffer(seg, '<f2').reshape(n, 4).astype(np.float32)
            out['NORMAL'] = oct_decode(h[:, :2])
            out['_tan'] = oct_decode(h[:, 2:])
        elif nm == 'W':
            out['WEIGHTS_0'] = np.frombuffer(seg, 'u1').reshape(n, 4)
        elif nm == 'J':
            out['JOINTS_0'] = np.frombuffer(seg, 'u1').reshape(n, 4)
    if '_tan' in out:
        out['TANGENT'] = np.concatenate([out.pop('_tan'), out['_hand'][:, None]], 1).astype(np.float32)
    out.pop('_hand', None)
    # blendshapes (format *_bpn): per shape [position delta snorm16x4 (same quantization incl. offset),
    #                                        morphed normal/tangent half4 octahedral (absolute)]
    out['morphs'] = {}
    for bs in dc.get('blendshapes') or []:
        pa, na = bs['vertex_data_stream_offsets']
        dp = np.frombuffer(raw, '<i2', 4 * n, pa).reshape(n, 4).astype(np.float64) / 32767.0
        t = {'POSITION': (dp[:, :3] * axes(m['quantization_scale']) + axes(m['quantization_offset'])).astype(np.float32)}
        if 'NORMAL' in out:
            h = np.frombuffer(raw, '<f2', 4 * n, na).reshape(n, 4).astype(np.float32)
            t['NORMAL'] = oct_decode(h[:, :2]) - out['NORMAL']
            if 'TANGENT' in out: t['TANGENT'] = oct_decode(h[:, 2:]) - out['TANGENT'][:, :3]
        out['morphs'][bs['model_blendshape_index']] = t
    return out

def export(imp_path, out_path, all_lods=False, extracted=None):
    img, dec, res = vbin.decode_file(imp_path)
    models = [r['value'] for r in res if r['value'] and r['value'].get('$type') == '_vrender_model_t']
    if not models: return 'skip', 'not a render model'
    m = models[0]
    stem = imp_path[:-len('.import')]
    dcs = m['draw_calls']
    lodb = m['dc_source_lod_levels']
    if isinstance(lodb, dict) and '$bytes' in lodb:
        b, o, n = lodb['$bytes']; lods = list(img.d[b + o:b + o + n])
    else:
        lods = [0] * len(dcs)
    if len(lods) != len(dcs): lods = [0] * len(dcs)
    G = Glb(); g = G.g
    root = G.node(name=os.path.basename(stem), rotation=[-0.70710678, 0, 0, 0.70710678])
    g['scenes'][0]['nodes'].append(root)
    # materials: slot names from the mesh; real materials from the .model asset(s) that use this mesh
    slots = {s['slot']: s['name'] for s in m['material_slots']}
    slot_ref = {s['slot']: (s['material'] or {}).get('$ref', '') for s in m['material_slots']}
    slot_hash = {s['slot']: s.get('name_hash') for s in m['material_slots']}
    links = LINKS.get(img.exports[0], []) if img.exports else []
    links = sorted(links, key=lambda l: (os.path.basename(l[0]).split('.')[0] not in os.path.basename(stem), l[0]))
    if links: g['scenes'][0]['extras'] = {'model_assets': [l[0] for l in links], 'mesh_guid': img.exports[0]}
    mat_index = {}; furcard = {}; furshell = {}; layer_mats = {}
    def material(slot):
        if slot not in mat_index:
            mat = {'name': slots.get(slot, 'slot_%d' % slot),
                   'pbrMetallicRoughness': {'baseColorFactor': [0.8, 0.8, 0.8, 1], 'metallicFactor': 0,
                                            'roughnessFactor': 0.7}}
            extras = {}
            # the engine matches overrides by name hash; .model slot numbers can be stale (e.g. sk_andrew)
            ov = None
            if links:
                ov = links[0][1].get(('hash', slot_hash.get(slot))) or next(
                    (e for k, e in links[0][1].items() if not isinstance(k, tuple) and e.get('name') == slots.get(slot)), None)
            if ov and extracted:
                extras = material_info(extracted, ov['material_guid']); extras['model_asset'] = links[0][0]
            elif slot_ref.get(slot, '').startswith('import:') and extracted:
                extras = material_info(extracted, slot_ref[slot][7:])
            if extras.get('cull_mode') == 'k_vgpu_cull_mode_none': mat['doubleSided'] = True
            rt = extras.get('render_type', '')
            if 'blend' in rt or 'transparent' in rt:
                mat['alphaMode'] = 'BLEND'
                if not extras.get('textures'):   # untextured blended = glass (lenses, visors): tinted, mostly clear
                    t = extras.get('glass_tint') or [0.8, 0.8, 0.8]
                    mat['pbrMetallicRoughness'].update({'baseColorFactor': [float(x) for x in t] + [0.25], 'roughnessFactor': 0.05})
            elif any(k in rt for k in ('mask', 'cutout', 'alpha_test', 'clip')): mat['alphaMode'] = 'MASK'
            tex = {k.lower().lstrip('_'): v for k, v in (extras.get('textures') or {}).items()}
            def pick(role):
                for n in ROLE_NAMES[role]:
                    png = tex_png(tex.get(n))
                    if png: return png
            if os.path.basename(extras.get('shader_graph') or '') in FURCARD_SGS:
                # cards: strand alpha atlas on UV0 (_VAHAlpha), fur colour atlas on UV1 (_BaseColor).
                # glTF can't mix UV sets in one channel: colour is baked per vertex (COLOR_0), texture = strand cutout.
                vah = tex_png(tex.get('vahalpha')); base = tex_png(tex.get('basecolor'))
                if vah:
                    mat['pbrMetallicRoughness']['baseTextureTmp'] = strand_cutout_png(vah)
                    clip = (extras.get('uniforms') or {}).get('AlphaClip', 0.5)
                    mat['alphaMode'] = 'MASK'; mat['alphaCutoff'] = card_cutoff(clip if isinstance(clip, (int, float)) else 0.5)
                    mat['doubleSided'] = True
                    furcard[slot] = base
                    mat['extras'] = extras
                    g['materials'].append(mat); mat_index[slot] = len(g['materials']) - 1
                    return mat_index[slot]
            if os.path.basename(extras.get('shader_graph') or '').startswith('fur_vertex_color'):
                # shell fur: exact port of the game's strand shader (see shell_fur.py); layers get MASK materials
                alb = tex_png(tex.get('albedo')); hgt = tex_png(tex.get('height_texture'))
                if alb and hgt and FUR[0]:
                    fp = shell_fur.params(extracted, extras)
                    furshell[slot] = dict(params=fp, height=hgt, dir=tex_png(tex.get('fur_direction_map')),
                                          tex=shell_fur.fur_texture(alb, hgt, fp['strand_density'], fp['thickness'],
                                                                    os.path.dirname(alb)))
                    extras['shell_fur'] = fp
            for role in ('base', 'normal', 'rmao', 'emissive'):
                png = pick(role)
                if not png: continue
                if role == 'base':
                    mat['pbrMetallicRoughness']['baseTextureTmp'] = png
                elif role == 'normal': mat['normalTmp'] = png
                elif role == 'rmao':
                    try: mat['ormTmp'] = orm_png(png)
                    except Exception: pass
                elif role == 'emissive': mat['emissiveTmp'] = png
            if slot in furshell:
                mat['pbrMetallicRoughness']['baseTextureTmp'] = furshell[slot]['tex']
                mat['doubleSided'] = True
            if extras: mat['extras'] = extras
            g['materials'].append(mat)
            mat_index[slot] = len(g['materials']) - 1
        return mat_index[slot]
    card_mats = {}
    def card_material(slot, k, color):
        """Fur-card colour cluster: white strand cutout x baseColorFactor (works without vertex-colour support)."""
        if (slot, k) not in card_mats:
            base = g['materials'][material(slot)]
            mat = json.loads(json.dumps(base))
            mat['name'] = '%s_c%02d' % (base['name'], k)
            cut = mat['pbrMetallicRoughness'].get('baseTextureTmp')
            if cut and not FUR_FACTORS[0]:   # colour baked into the texture (Preview/USD conversion drops baseColorFactor with a texture)
                mat['pbrMetallicRoughness']['baseTextureTmp'] = tinted_cutout_png(cut, color)
                mat['pbrMetallicRoughness']['baseColorFactor'] = [1.0, 1.0, 1.0, 1.0]
            else:
                mat['pbrMetallicRoughness']['baseColorFactor'] = [float(x) for x in color] + [1.0]
            mat['pbrMetallicRoughness']['metallicFactor'] = 0.0
            mat['extras_color'] = [float(x) for x in color]
            mat['extras'] = {'card_of': base['name'], 'color_linear': mat.pop('extras_color')}; mat['_keep_factors'] = True
            g['materials'].append(mat); card_mats[(slot, k)] = len(g['materials']) - 1
        return card_mats[(slot, k)]
    def layer_material(slot, h):
        """Per-shell copy of a fur material: MASK with alphaCutoff = h (the shader's discard test)."""
        key = (slot, round(float(h), 6))
        if key not in layer_mats:
            base = g['materials'][material(slot)]
            mat = json.loads(json.dumps(base))
            mat['name'] = '%s_shell%03d' % (base['name'], int(round(h * 255)))
            if h <= 0.01: mat['alphaMode'] = 'OPAQUE'; mat.pop('alphaCutoff', None)
            else: mat['alphaMode'] = 'MASK'; mat['alphaCutoff'] = float(h)
            pbr = mat['pbrMetallicRoughness']; pbr['metallicFactor'] = 0.0
            if not FUR_FACTORS[0]:
                pbr['roughnessFactor'] = 0.85
                mat.pop('ormTmp', None)   # no metal/roughness texture on fur: converters may ignore metallicFactor
            ao = shell_fur.shell_ao(furshell[slot]['params'], float(h)) if slot in furshell else 1.0
            pbr['baseColorFactor'] = [ao, ao, ao, 1.0]; mat['_keep_factors'] = True
            mat.pop('extras', None); mat['extras'] = {'shell_of': base['name'], 'shell_height': float(h)}
            g['materials'].append(mat); layer_mats[key] = len(g['materials']) - 1
        return layer_mats[key]
    # skeleton
    skin = None; bones = None
    sk = m.get('skeleton_data')
    if sk and sk.get('data') and 'seg_off' in sk['data']:
        b, o = sk['data']['seg_off']
        blob = img.d[b + o:b + o + sk['data']['size']]
        gi = granny_skel.parse(blob)
        if gi['skeletons'] and gi['skeletons'][0]['bones']:
            bones = gi['skeletons'][0]['bones']
            jn = []
            for bi, bo in enumerate(bones):
                ss = bo['scale_shear']
                node = {'name': bo['name'] or 'bone_%d' % bi}
                if bo['flags'] & 1: node['translation'] = [float(x) for x in bo['pos']]
                if bo['flags'] & 2: node['rotation'] = [float(x) for x in bo['quat']]
                if bo['flags'] & 4:
                    if np.allclose(ss, np.diag(np.diag(ss)), atol=1e-6): node['scale'] = [float(x) for x in np.diag(ss)]
                    else:   # shear: bake into a matrix
                        x, y, z, w = bo['quat']
                        R = np.array([[1-2*(y*y+z*z), 2*(x*y-z*w), 2*(x*z+y*w)], [2*(x*y+z*w), 1-2*(x*x+z*z), 2*(y*z-x*w)],
                                      [2*(x*z-y*w), 2*(y*z+x*w), 1-2*(x*x+y*y)]])
                        L = np.eye(4); L[:3, :3] = R @ ss; L[:3, 3] = bo['pos']
                        for k in ('translation', 'rotation'): node.pop(k, None)
                        node['matrix'] = [float(x) for x in L.flatten('F')]
                jn.append(G.node(**node))
            for bi, bo in enumerate(bones):
                if bo['parent'] >= 0: g['nodes'][jn[bo['parent']]].setdefault('children', []).append(jn[bi])
                else: g['nodes'][root].setdefault('children', []).append(jn[bi])
            ibm = np.stack([bo['inv_world'] for bo in bones]).astype(np.float32).reshape(-1, 16)  # already column-major
            g['skins'] = [{'name': gi['models'][0]['name'] if gi['models'] else 'skeleton', 'joints': jn,
                           'inverseBindMatrices': G.acc(ibm, 'MAT4', target=None),
                           'skeleton': jn[[i for i, bo in enumerate(bones) if bo['parent'] < 0][0]]}]
            skin = 0
    wanted = sorted(set(lods)) if all_lods else [min(lods)]
    shape_names = [b['name'] for b in (m.get('blendshapes') or [])]
    stats = {'draw_calls': 0, 'verts': 0, 'tris': 0}
    for lod in wanted:
        prims = []
        for i, dc in enumerate(dcs):
            if lods[i] != lod: continue
            raw_path = '%s.%d.nvn_gen2.raw' % (stem, i)
            if not os.path.exists(raw_path): raise FileNotFoundError(raw_path)
            raw = open(raw_path, 'rb').read()
            v = decode_drawcall(m, dc, raw)
            fs = furshell.get(dc['material_slot_index']) if material(dc['material_slot_index']) is not None else None
            shell_h = None
            if fs and 'COLOR' in v and 'NORMAL' in v and 'TEXCOORD_0' in v:
                v['POSITION'], shell_h, v['TEXCOORD_0'] = shell_fur.extrude(v, fs['params'], fs['height'], fs['dir'])
            attrs = {'POSITION': G.acc(v['POSITION'], 'VEC3', minmax=True)}
            if 'NORMAL' in v: attrs['NORMAL'] = G.acc(v['NORMAL'], 'VEC3')
            if 'TANGENT' in v: attrs['TANGENT'] = G.acc(v['TANGENT'], 'VEC4')
            for t in ('TEXCOORD_0', 'TEXCOORD_1'):
                if t in v: attrs[t] = G.acc(v[t], 'VEC2')
            if 'COLOR' in v:   # engine data channel (masks/AO); exported as a custom attribute, not COLOR_0
                attrs['_COLOR'] = G.acc(v['COLOR'], 'VEC4', normalized=True)
            mi = material(dc['material_slot_index'])
            card_col = None
            if dc['material_slot_index'] in furcard:
                uv1 = v.get('TEXCOORD_1', v.get('TEXCOORD_0'))
                base = furcard[dc['material_slot_index']]
                card_col = sample_png(base, uv1) if base else np.full((len(uv1), 3), 0.5, np.float32)
            if skin is not None and 'JOINTS_0' in v:
                w = v['WEIGHTS_0'].astype(np.float32); s = w.sum(1, keepdims=True); s[s == 0] = 1
                j = v['JOINTS_0'].copy(); w = w / s
                j[w == 0] = 0
                attrs['JOINTS_0'] = G.acc(j, 'VEC4'); attrs['WEIGHTS_0'] = G.acc(w.astype(np.float32), 'VEC4')
            prim_targets = None
            if shape_names:
                prim_targets = []
                for k in range(len(shape_names)):
                    t = v['morphs'].get(k)
                    tt = {}
                    for an, typ in (('POSITION', 'VEC3'), ('NORMAL', 'VEC3'), ('TANGENT', 'VEC3')):
                        if an == 'POSITION' or an in attrs:
                            if t is not None and an in t:
                                tt[an] = G.acc(t[an], typ, minmax=(an == 'POSITION'))
                            else:   # zero target: accessor without bufferView is all zeros
                                g['accessors'].append({'componentType': 5126, 'count': int(len(v['POSITION'])),
                                                       'type': typ, **({'min': [0, 0, 0], 'max': [0, 0, 0]} if an == 'POSITION' else {})})
                                tt[an] = len(g['accessors']) - 1
                    prim_targets.append(tt)
            mode = {'k_vgpu_primitive_triangle_list': 4, 'k_vgpu_primitive_triangle_strip': 5,
                    'k_vgpu_primitive_line_list': 1, 'k_vgpu_primitive_point_list': 0}.get(dc['primitive_type'], 4)
            idx = v['indices']
            slot_i = dc['material_slot_index']
            if shell_h is not None and mode == 4:
                tri = idx.reshape(-1, 3); th = shell_h[tri[:, 0]]
                groups = [(layer_material(slot_i, hv), tri[th == hv].reshape(-1)) for hv in np.unique(th)]
            elif card_col is not None and mode == 4 and len(idx) >= 3:
                # per-triangle fur colour (sampled on UV1), clustered into a few flat-colour materials
                tri = idx.reshape(-1, 3); tc = card_col[tri].mean(1)
                lab, centers = shell_fur.kmeans_colors(tc.astype(np.float64), k=24 if FUR_FACTORS[0] else 12)
                groups = [(card_material(slot_i, k, centers[k]), tri[lab == k].reshape(-1)) for k in np.unique(lab)]
            else:
                groups = [(material(slot_i), idx)]
            for mi_, gi in groups:
                prims.append({'attributes': attrs,
                              'indices': G.acc(gi if gi.max(initial=0) > 65535 else gi.astype(np.uint16), 'SCALAR', target=34963),
                              'material': mi_, 'mode': mode})
                if prim_targets: prims[-1]['targets'] = prim_targets
            stats['draw_calls'] += 1; stats['verts'] += dc['vertex_count']; stats['tris'] += dc['index_count'] // 3
        if not prims: continue
        g['meshes'].append({'name': '%s_lod%d' % (os.path.basename(stem), lod), 'primitives': prims})
        if shape_names:
            g['meshes'][-1]['weights'] = [0.0] * len(shape_names)
            g['meshes'][-1]['extras'] = {'targetNames': shape_names}
        mn = {'name': '%s_lod%d' % (os.path.basename(stem), lod), 'mesh': len(g['meshes']) - 1}
        if skin is not None: mn['skin'] = skin
        g['nodes'][root].setdefault('children', []).append(G.node(**mn))
    if not g['meshes']: return 'skip', 'no draw calls'
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    img_index = {}
    def texref(png):
        if png not in img_index:
            g.setdefault('images', []).append({'uri': os.path.relpath(png, os.path.dirname(out_path)).replace(os.sep, '/'),
                                               'name': os.path.basename(png)})
            g.setdefault('textures', []).append({'source': len(g['images']) - 1})
            img_index[png] = len(g['textures']) - 1
        return {'index': img_index[png]}
    for mat in g['materials']:
        pbr = mat['pbrMetallicRoughness']
        b = pbr.pop('baseTextureTmp', None)
        keep = mat.pop('_keep_factors', False)   # card colours / shell AO / no-metal set deliberately
        if b:
            pbr['baseColorTexture'] = texref(b)
            if not keep: pbr['baseColorFactor'] = [1, 1, 1, 1]
        n = mat.pop('normalTmp', None)
        if n: mat['normalTexture'] = texref(n)
        o = mat.pop('ormTmp', None)
        if o:
            pbr['metallicRoughnessTexture'] = texref(o); pbr['roughnessFactor'] = 1
            if not keep: pbr['metallicFactor'] = 1
            mat['occlusionTexture'] = texref(o)
        e = mat.pop('emissiveTmp', None)
        if e: mat['emissiveTexture'] = texref(e); mat['emissiveFactor'] = [1, 1, 1]
    G.write(out_path)
    return 'ok', '%s bones=%s %s' % (out_path, len(bones) if bones else 0, stats)

def init(ext, texdir=None, fur=True, fur_factors=True):
    TEXDIR[0] = texdir; EXTRACTED[0] = ext; FUR[0] = fur; FUR_FACTORS[0] = fur_factors
    GUIDS.update(json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'work', 'guid_index.json'))))
    build_links(ext)

def job(args):
    imp, out, lods, force, ext = args
    if not force and os.path.exists(out) and os.path.getmtime(out) >= os.path.getmtime(imp): return 'exists', out
    try:
        return export(imp, out, lods, ext)
    except Exception as e:
        return 'fail', '%s: %s %s' % (imp, type(e).__name__, str(e)[:200])

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('extracted'); ap.add_argument('out')
    ap.add_argument('--filter'); ap.add_argument('--lods', action='store_true', help='export every LOD as its own mesh')
    ap.add_argument('-j', type=int, default=os.cpu_count() or 4); ap.add_argument('--force', action='store_true')
    ap.add_argument('--textures', help='texture_export.py output dir; materials then reference its PNGs')
    ap.add_argument('--no-fur', action='store_true', help="don't bake shell fur (needs --textures)")
    ap.add_argument('--fur-baked-colors', action='store_true',
                    help='bake fur-card colours into tinted textures (for viewers that ignore baseColorFactor)')
    a = ap.parse_args()
    jobs = []
    for dp, dn, fn in os.walk(os.path.join(a.extracted, 'data')):
        for f in fn:
            if f.endswith(('.gr2.import', '.fbx.import')) and not f.startswith('._'):
                p = os.path.join(dp, f)
                if a.filter and a.filter not in p: continue
                rel = os.path.relpath(p, a.extracted)[:-len('.import')]
                jobs.append((p, os.path.join(a.out, rel + '.glb'), a.lods, a.force, a.extracted))
    jobs.sort()
    stats = {}; fails = []
    with ProcessPoolExecutor(a.j, initializer=init, initargs=(a.extracted, os.path.abspath(a.textures) if a.textures else None, not a.no_fur, not a.fur_baked_colors)) as ex:
        for i, (st, msg) in enumerate(ex.map(job, jobs, chunksize=8), 1):
            stats[st] = stats.get(st, 0) + 1
            if st == 'fail': fails.append(msg)
            if len(jobs) < 20 and st != 'skip': print(st, msg)
            if i % 1000 == 0: print(i, len(jobs), stats, flush=True)
    print('done', stats)
    if fails:
        os.makedirs(a.out, exist_ok=True)
        open(os.path.join(a.out, '_failures.txt'), 'w').write('\n'.join(fails) + '\n')
        print('failures ->', os.path.join(a.out, '_failures.txt'))
        for f in fails[:10]: print('  ', f)
