"""Exact reproduction of the veclipse shell-fur shader (fur_vertex_color*.shader_graph) for glTF export.

Recovered by disassembling the NVN (Ampere, SM86) shader binaries with nvdisasm (work/nvdisasm/*.sass).
Vertex shader (fur_vertex_color.shader_graph.0):
    h    = vertex colour R (layer height, 0..1)
    ext  = pow(h, shell_distance_attenuation) * max(height_tex(uv).r, 0.005) * shell_length
    pos += normal * ext  +  shell_direction * pow(h, c5[0x1c]) * displacement_strength   (shell_direction is 0
                                                                                          in every material)
Depth pre-pass fragment shader (….8; the colour pass has no discard and relies on depth-equal):
    uv2  = uv + (dir_tex(uv).xy*2-1) * pow(h, fur_direction_attenuation) * fur_direction_multiplier
    H    = height_tex(uv2)                       # .r = strand length mask, .g = thickness mask
    s    = strand_density
    cell = int((uv2.x + floor(uv2.y*s)) * s)
    n    = cell ^ (cell << 13)                   # Hugo Elias integer noise
    rnd  = ((n*(n*n*15731 + 789221) + 1376312589) & 0x7fffffff) * 4.6566128730773926e-10
    len  = 1 + rnd*(H.r - 1)
    d    = length(fract(uv2*s)*2 - 1)
    keep = h <= 0.01 || (len - h) * H.g * thickness >= d
glTF mapping: keep <=> A >= h with A = len - d/(H.g*thickness): one texture (alpha = A) shared by all layers,
one MASK material per layer with alphaCutoff = h. The per-layer UV comb (uv2 - uv) is applied to each shell's own UV copy.
"""
import os, struct, sys
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# GPU uniform offsets (c[0x5][...]) used by the shaders; the CPU struct offset is +0x0c, and the shader graph's
# default_uniform_buffer_data blob stores each value at CPU offset + 4 (verified against known values).
GPU = {'fur_direction_attenuation': 0x28, 'fur_direction_multiplier': 0x2c, 'shell_distance_attenuation': 0x6c,
       'shell_length': 0x70, 'strand_density': 0x74, 'thickness': 0x78, 'displacement_strength': 0x20,
       'occlusion_attenuation': 0x30, 'occlusion_bias': 0x34}
# Colour pass (fur_vertex_color.shader_graph.6): fake AO per shell  ao = h^occlusion_attenuation + occlusion_bias,
# colour *= ao (clamped to 1 here); also no metal term - fur is not PBR-metallic.
def shell_ao(p, h):
    return float(min(1.0, max(0.0, (h ** p['occlusion_attenuation'] if h > 0 else 0.0) + p['occlusion_bias'])))

def kmeans_colors(c, k=24, iters=12):
    """Small k-means on linear RGB rows; returns (labels, centers)."""
    k = min(k, len(c))
    rng = np.random.default_rng(0)
    centers = c[rng.choice(len(c), k, replace=False)].copy()
    for _ in range(iters):
        d = ((c[:, None, :] - centers[None]) ** 2).sum(-1)
        lab = d.argmin(1)
        for j in range(k):
            sel = lab == j
            if sel.any(): centers[j] = c[sel].mean(0)
    return lab, centers
TEXSIZE = 4096
_DEF = {}

def defaults(extracted, sg_path):
    """Default uniform values of a fur shader graph (.shader_graph.import)."""
    if sg_path in _DEF: return _DEF[sg_path]
    import vbin
    img = vbin.Image(open(os.path.join(extracted, sg_path.lstrip('/')), 'rb').read())
    dec = vbin.Decoder(img)
    seg, b, o, t = dec.roots()[0]
    off = {n: ob // 8 for n, ob, ft, nb in dec.fields(dec.resolve(t))}['default_uniform_buffer_data']
    tb, to = dec.target(b, o + off)
    out = {k: struct.unpack_from('<f', img.d, tb + to + g + 0x10)[0] for k, g in GPU.items()}
    _DEF[sg_path] = out
    return out

def params(extracted, extras):
    p = dict(defaults(extracted, extras['shader_graph']))
    for k, v in (extras.get('uniforms') or {}).items():
        if k in p and isinstance(v, (int, float)): p[k] = float(v)
    return p

def _load(png, size=None, resample=None):
    from PIL import Image
    im = Image.open(png).convert('RGBA')
    if size and im.size != (size, size): im = im.resize((size, size), resample or Image.BILINEAR)
    return np.asarray(im).astype(np.float32) / 255

def sample(png, uv, cache={}):
    """Bilinear, repeat-wrapped sample of a PNG (linear values 0..1, RGBA) at UVs (V down = image rows)."""
    if png not in cache:
        cache.clear(); cache[png] = _load(png)
    im = cache[png]; h, w = im.shape[:2]
    x = (uv[:, 0] % 1) * w - 0.5; y = (uv[:, 1] % 1) * h - 0.5
    x0 = np.floor(x).astype(int); y0 = np.floor(y).astype(int); fx = (x - x0)[:, None]; fy = (y - y0)[:, None]
    def px(yy, xx): return im[yy % h, xx % w]
    return (px(y0, x0) * (1 - fx) * (1 - fy) + px(y0, x0 + 1) * fx * (1 - fy)
            + px(y0 + 1, x0) * (1 - fx) * fy + px(y0 + 1, x0 + 1) * fx * fy)

def fur_texture(albedo_png, height_png, density, thickness, out_dir):
    """RGB = albedo, A = floor(255 * clamp(len - d/(H.g*thickness))) so that alpha >= h/255 <=> shader keeps."""
    key = '%s.fur_s%g_t%g.png' % (os.path.splitext(os.path.basename(albedo_png))[0], density, thickness)
    out = os.path.join(out_dir, key)
    if os.path.exists(out) and os.path.getmtime(out) >= max(os.path.getmtime(albedo_png), os.path.getmtime(height_png)):
        return out
    from PIL import Image
    N = TEXSIZE
    s = np.float32(density)
    t = (np.arange(N, dtype=np.float32) + np.float32(0.5)) / np.float32(N)
    U, V = np.meshgrid(t, t)                       # U across columns, V down rows
    vy = V * s
    fy = np.floor(vy)
    cell = ((U + fy) * s).astype(np.int64).astype(np.uint32)          # F2I.TRUNC of a positive value
    n = cell ^ (cell << np.uint32(13))
    n = n.astype(np.uint32)
    r = (n * (n * n * np.uint32(15731) + np.uint32(789221)) + np.uint32(1376312589)) & np.uint32(0x7fffffff)
    rnd = r.astype(np.int32).astype(np.float32) * np.float32(4.6566128730773926e-10)
    del n, r, cell
    H = _load(height_png, N)
    ln = rnd * (H[..., 0] - 1) + 1
    vx = U * s
    dx = (vx - np.floor(vx)) * 2 - 1; dy = (vy - fy) * 2 - 1
    d = np.sqrt(dx * dx + dy * dy)
    gt = H[..., 1] * np.float32(thickness)
    with np.errstate(divide='ignore', invalid='ignore'):
        A = np.where(gt > 0, ln - d / gt, -1.0)
    alpha = np.floor(np.clip(A, 0, 1) * 255).astype(np.uint8)
    rgb = (_load(albedo_png, N)[..., :3] * 255 + 0.5).astype(np.uint8)
    os.makedirs(out_dir, exist_ok=True)
    tmp = out + '.%d.part' % os.getpid()
    Image.fromarray(np.dstack([rgb, alpha]), 'RGBA').save(tmp, format='PNG', compress_level=6)
    os.replace(tmp, out)
    return out

def extrude(v, p, height_png, dir_png):
    """Return (POSITION with the vertex-shader shell offset, h, TEXCOORD_0 with the fragment comb applied)."""
    P = v['POSITION'].astype(np.float64); N = v['NORMAL'].astype(np.float64); uv = v['TEXCOORD_0'].astype(np.float64)
    h = v['COLOR'][:, 0].astype(np.float64) / 255
    hr = sample(height_png, uv)[:, 0]
    with np.errstate(divide='ignore'):
        ext = np.where(h > 0, h ** p['shell_distance_attenuation'], 0) * np.maximum(hr, 0.005) * p['shell_length']
    out = P + N * ext[:, None]
    uv_out = uv.copy()
    if dir_png and p['fur_direction_multiplier']:
        # comb: the shader samples the strand pattern at uv + delta. Every shell has its own copy of each vertex,
        # so this is applied exactly as a per-vertex UV shift (moving geometry instead pushed fur through clothing)
        dvec = sample(dir_png, uv)[:, :2] * 2 - 1
        delta = dvec * (np.where(h > 0, h ** p['fur_direction_attenuation'], 0) * p['fur_direction_multiplier'])[:, None]
        uv_out = uv + delta
    return out.astype(np.float32), h, uv_out.astype(np.float32)
