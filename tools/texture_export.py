#!/usr/bin/env python3
"""Export veclipse textures (*.png/.tga/.jpg/.hdr .import -> _vrender_texture_t) to PNG (.hdr for float formats).

usage: python -I tools/texture_export.py <extracted_dir> <out_dir> [--filter SUBSTR] [-j N] [--force] [--mips]

Data: backend[8] (k_vrender_backend_nvn_gen2) describes `<tex>.0.nvn_gen2.raw` (full mip chain, data_sizes bytes).
`.1.nvn_gen2.raw`, when present, is a small low-res copy used while streaming; it is not needed.
Tiling: NVIDIA block-linear. GOB = 64 bytes x 8 rows (512 B); blocks are 2^(layout & 0xf) GOBs tall
(reduced per mip while the mip is at most half a block tall). mip_offsets give each mip's start.
Pixels: BCn via Pillow's 'bcn' decoder; the packed component swizzle (identity/zero/one/r/g/b/a) is applied,
alpha is dropped when the source had none, greyscale results are written as L.
"""
import argparse, os, struct, sys, zlib
from concurrent.futures import ProcessPoolExecutor
import numpy as np
from PIL import Image
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vbin

# format -> (block w/h, bytes per element, decoder)
FMT = {
    'bc1_rgb_srgb_block': (4, 8, ('bcn', 1)), 'bc1_rgb_unorm_block': (4, 8, ('bcn', 1)),
    'bc1_rgba_srgb_block': (4, 8, ('bcn', 1)), 'bc1_rgba_unorm_block': (4, 8, ('bcn', 1)),
    'bc2_srgb_block': (4, 16, ('bcn', 2)), 'bc2_unorm_block': (4, 16, ('bcn', 2)),
    'bc3_srgb_block': (4, 16, ('bcn', 3)), 'bc3_unorm_block': (4, 16, ('bcn', 3)),
    'bc4_unorm_block': (4, 8, ('bcn', 4)), 'bc5_unorm_block': (4, 16, ('bcn', 5)),
    'bc6h_ufloat_block': (4, 16, ('bcn6', 'u')), 'bc6h_sfloat_block': (4, 16, ('bcn6', 's')),
    'bc7_srgb_block': (4, 16, ('bcn', 7)), 'bc7_unorm_block': (4, 16, ('bcn', 7)),
    'r8_unorm': (1, 1, ('raw', 'r8')), 'r8g8_unorm': (1, 2, ('raw', 'rg8')),
    'r8g8b8a8_unorm': (1, 4, ('raw', 'rgba8')), 'r8g8b8a8_srgb': (1, 4, ('raw', 'rgba8')),
    'b8g8r8a8_unorm': (1, 4, ('raw', 'bgra8')), 'b8g8r8a8_srgb': (1, 4, ('raw', 'bgra8')),
    'r16_unorm': (1, 2, ('raw', 'r16')), 'r16g16b16a16_unorm': (1, 8, ('raw', 'rgba16')),
    'r16g16b16a16_sfloat': (1, 8, ('raw', 'rgba16f')), 'b10g11r11_ufloat_pack32': (1, 4, ('raw', 'r11g11b10f')),
}

def gob_table():
    """Byte offset inside a 512-byte GOB for (row 0..7, xbyte 0..63).
    Switch 2 order (found empirically): 16-byte sectors addressed by bits y0,y1,x16,y2,x32 (low->high);
    differs from Tegra X1 (y0,x16,y1,y2,x32) by swapping the y1/x16 bits."""
    y = np.arange(8)[:, None]; x = np.arange(64)[None, :]
    return ((x % 64) // 32) * 256 + ((y % 8) // 4) * 128 + ((x % 32) // 16) * 64 + ((y % 4) // 2) * 32 + (y % 2) * 16 + (x % 16)
GOB = gob_table()

def block_height_for(rows, bh):
    """NVIDIA mip rule: shrink block height (in GOBs) while the mip is at most half a block tall."""
    while bh > 1 and rows <= (bh * 8) // 2: bh //= 2
    return bh

def deswizzle(data, off, w_el, h_el, bpe, bh):
    row_bytes = w_el * bpe
    gobs_x = (row_bytes + 63) // 64
    block_rows = 8 * bh
    ys = np.arange(h_el)[:, None]
    xb = (np.arange(w_el) * bpe)[None, :]
    addr = ((ys // block_rows) * gobs_x * 512 * bh + (xb // 64) * 512 * bh + ((ys % block_rows) // 8) * 512
            + GOB[ys % 8, xb % 64])
    size = ((h_el + block_rows - 1) // block_rows) * gobs_x * 512 * bh
    src = np.frombuffer(data, np.uint8, min(size, len(data) - off), off)
    if src.size < size: src = np.concatenate([src, np.zeros(size - src.size, np.uint8)])
    idx = addr[..., None] + np.arange(bpe)[None, None, :]
    return src[idx].reshape(h_el, w_el * bpe).tobytes(), size

def half(a): return a.view(np.float16).astype(np.float32)

def r11g11b10(u):
    def f(bits, mbits):
        e = (bits >> mbits) & 0x1f; m = bits & ((1 << mbits) - 1)
        return np.where(e == 0, m / (1 << mbits) * 2.0 ** -14, (1 + m / (1 << mbits)) * 2.0 ** (e.astype(np.float64) - 15))
    return np.stack([f(u & 0x7ff, 6), f((u >> 11) & 0x7ff, 6), f((u >> 22) & 0x3ff, 5)], -1).astype(np.float32)

def to_rgba(buf, w, h, fmt):
    """Return (array HxWx4, kind) where kind is 'u8', 'u16' or 'f32'."""
    blk, bpe, (dk, arg) = FMT[fmt]
    if dk == 'bcn':
        mode = {4: 'L', 5: 'RGB'}.get(arg, 'RGBA')
        im = Image.frombytes(mode, (w, h), buf, 'bcn', arg)
        a = np.asarray(im)
        if mode == 'L': a = np.stack([a, np.zeros_like(a), np.zeros_like(a), np.full_like(a, 255)], -1)
        elif mode == 'RGB': a = np.concatenate([a, np.full(a.shape[:2] + (1,), 255, np.uint8)], -1)
        return a, 'u8'
    if dk == 'bcn6':
        im = Image.frombytes('RGB', (w, h), buf, 'bcn', (6, arg + 'f'))
        a = np.asarray(im).astype(np.float32)
        return np.concatenate([a, np.ones(a.shape[:2] + (1,), np.float32)], -1), 'f32'
    raw = np.frombuffer(buf, np.uint8).reshape(h, w * bpe)
    if arg == 'r8':
        r = raw; z = np.zeros_like(r); return np.stack([r, z, z, np.full_like(r, 255)], -1), 'u8'
    if arg == 'rg8':
        p = raw.reshape(h, w, 2); z = np.zeros((h, w), np.uint8)
        return np.stack([p[..., 0], p[..., 1], z, np.full_like(z, 255)], -1), 'u8'
    if arg == 'rgba8': return raw.reshape(h, w, 4), 'u8'
    if arg == 'bgra8': return raw.reshape(h, w, 4)[..., [2, 1, 0, 3]], 'u8'
    if arg == 'r16':
        r = raw.view('<u2').reshape(h, w); z = np.zeros_like(r)
        return np.stack([r, z, z, np.full_like(r, 65535)], -1), 'u16'
    if arg == 'rgba16': return raw.view('<u2').reshape(h, w, 4), 'u16'
    if arg == 'rgba16f': return half(raw.view('<u2').reshape(h, w, 4)), 'f32'
    if arg == 'r11g11b10f':
        c = r11g11b10(raw.view('<u4').reshape(h, w).astype(np.uint64))
        return np.concatenate([c, np.ones((h, w, 1), np.float32)], -1), 'f32'
    raise ValueError(arg)

SWZ = {'k_vgpu_swizzle_identity': None, 'k_vgpu_swizzle_zero': 'zero', 'k_vgpu_swizzle_one': 'one',
       'k_vgpu_swizzle_r': 0, 'k_vgpu_swizzle_g': 1, 'k_vgpu_swizzle_b': 2, 'k_vgpu_swizzle_a': 3}

def apply_swizzle(a, swz, kind):
    one = {'u8': 255, 'u16': 65535, 'f32': 1.0}[kind]
    out = a.copy()
    for i, ch in enumerate('rgba'):
        s = SWZ.get(swz.get(ch), None) if isinstance(swz, dict) else None
        if s is None: continue
        out[..., i] = 0 if s == 'zero' else one if s == 'one' else a[..., s]
    return out

def write_png16(path, a):
    h, w, c = a.shape
    raw = b''.join(b'\0' + a[y].astype('>u2').tobytes() for y in range(h))
    ct = {1: 0, 2: 4, 3: 2, 4: 6}[c]
    def chunk(t, d): return struct.pack('>I', len(d)) + t + d + struct.pack('>I', zlib.crc32(t + d) & 0xffffffff)
    with open(path, 'wb') as f:
        f.write(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 16, ct, 0, 0, 0))
                + chunk(b'IDAT', zlib.compress(raw, 6)) + chunk(b'IEND', b''))

def write_hdr(path, rgb):
    h, w, _ = rgb.shape
    rgb = np.maximum(rgb, 0).astype(np.float64)
    mx = rgb.max(-1)
    m, e = np.frexp(mx)
    sc = np.where(mx > 1e-32, m * 256.0 / np.where(mx > 1e-32, mx, 1), 0)
    rgbe = np.zeros((h, w, 4), np.uint8)
    rgbe[..., :3] = np.clip(rgb * sc[..., None], 0, 255).astype(np.uint8)
    rgbe[..., 3] = np.where(mx > 1e-32, e + 128, 0).astype(np.uint8)
    with open(path, 'wb') as f:
        f.write(b'#?RADIANCE\nFORMAT=32-bit_rle_rgbe\n\n-Y %d +X %d\n' % (h, w)); f.write(rgbe.tobytes())

def save(a, kind, has_alpha, out_base):
    if kind == 'f32':
        p = out_base + '.hdr'; write_hdr(p, a[..., :3]); return p
    one = 255 if kind == 'u8' else 65535
    keep_a = has_alpha and not np.all(a[..., 3] == one)
    grey = np.array_equal(a[..., 0], a[..., 1]) and np.array_equal(a[..., 0], a[..., 2])
    ch = ([0] if grey else [0, 1, 2]) + ([3] if keep_a else [])
    b = a[..., ch]
    p = out_base + '.png'
    if kind == 'u16': write_png16(p + '.part', b)
    else:
        mode = {1: 'L', 2: 'LA', 3: 'RGB', 4: 'RGBA'}[len(ch)]
        Image.fromarray(np.ascontiguousarray(b if len(ch) > 1 else b[..., 0]), mode).save(p + '.part', format='PNG', compress_level=6)
    os.replace(p + '.part', p)
    return p

def export(imp, out_base, mips=False):
    img, dec, res = vbin.decode_file(imp)
    t = res[0]['value'] if res else None
    if not t or t.get('$type') != '_vrender_texture_t': return 'skip', 'not a texture'
    be = t['backend'][8]
    fmt = be['format'].replace('k_vgpu_format_', '')
    if fmt not in FMT: return 'fail', '%s: unsupported format %s' % (imp, fmt)
    if t['type'] not in ('k_vgpu_texture_type_2d',) or be['depth'] != 1 or be['array_layers'] != 1:
        return 'fail', '%s: unsupported type %s d=%s layers=%s' % (imp, t['type'], be['depth'], be['array_layers'])
    raw = open(imp[:-len('.import')] + '.0.nvn_gen2.raw', 'rb').read()
    if len(raw) != be['data_sizes']: return 'fail', '%s: raw size %d != %d' % (imp, len(raw), be['data_sizes'])
    blk, bpe, _ = FMT[fmt]
    bh0 = 1 << (be['layout'] & 0xf)
    linear = t['tiling'] == 'k_vgpu_tiling_linear' or be['layout'] == 0
    outs = []
    for mip in range(be['mip_levels'] if mips else 1):
        w = max(1, be['width'] >> mip); h = max(1, be['height'] >> mip)
        we, he = (w + blk - 1) // blk, (h + blk - 1) // blk
        off = be['mip_offsets'][mip]
        if linear and be['layout'] == 0:
            buf = raw[off:off + we * he * bpe]
        else:
            bh = block_height_for(he, bh0)
            buf, size = deswizzle(raw, off, we, he, bpe, bh)
            nxt = be['mip_offsets'][mip + 1] if mip + 1 < be['mip_levels'] else None
            if mip == 0 and nxt and nxt != size:
                return 'fail', '%s: mip0 size %d != next offset %d (bh=%d)' % (imp, size, nxt, bh)
        if blk > 1 and (we * blk != w or he * blk != h):   # decode padded, then crop
            a, kind = to_rgba(buf, we * blk, he * blk, fmt); a = a[:h, :w]
        else:
            a, kind = to_rgba(buf, w, h, fmt)
        a = apply_swizzle(a, be.get('swizzle') or {}, kind)
        has_alpha = bool(t.get('source_has_alpha')) or FMT[fmt][2][0] == 'raw'
        os.makedirs(os.path.dirname(out_base), exist_ok=True)
        outs.append(save(a, kind, has_alpha, out_base + ('' if mip == 0 else '.mip%d' % mip)))
    return 'ok', '%s %s %dx%d' % (outs[0], fmt, be['width'], be['height'])

SRC_EXT = ('.png', '.tga', '.jpg', '.jpeg', '.hdr', '.exr', '.tif', '.tiff', '.psd', '.bmp', '.dds')

def out_base_for(imp, extracted, out):
    """x.png.import -> x ; x.tga.import -> x, unless x.png.import also exists (then x.tga)."""
    rel = os.path.relpath(imp, extracted)[:-len('.import')]
    stem, ext = os.path.splitext(rel)
    if ext.lower() not in SRC_EXT: return os.path.join(out, rel)
    if ext.lower() not in ('.png', '.hdr', '.exr') and os.path.exists(os.path.join(extracted, stem + '.png.import')):
        return os.path.join(out, rel)
    return os.path.join(out, stem)

def job(args):
    imp, ob, mips, force = args
    if not force and (os.path.exists(ob + '.png') or os.path.exists(ob + '.hdr')): return 'exists', ob
    try:
        return export(imp, ob, mips)
    except Exception as e:
        return 'fail', '%s: %s %s' % (imp, type(e).__name__, str(e)[:200])

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('extracted'); ap.add_argument('out')
    ap.add_argument('--filter'); ap.add_argument('-j', type=int, default=os.cpu_count() or 4)
    ap.add_argument('--force', action='store_true'); ap.add_argument('--mips', action='store_true')
    a = ap.parse_args()
    jobs = []
    for dp, dn, fn in os.walk(os.path.join(a.extracted, 'data')):
        for f in fn:
            if f.startswith('._') or not f.endswith('.import'): continue
            if os.path.splitext(f[:-len('.import')])[1].lower() not in SRC_EXT: continue
            p = os.path.join(dp, f)
            if a.filter and a.filter not in p: continue
            jobs.append((p, out_base_for(p, a.extracted, a.out), a.mips, a.force))
    jobs.sort()
    stats = {}; fails = []
    with ProcessPoolExecutor(a.j) as ex:
        for i, (st, msg) in enumerate(ex.map(job, jobs, chunksize=4), 1):
            stats[st] = stats.get(st, 0) + 1
            if st == 'fail': fails.append(msg)
            if len(jobs) <= 20: print(st, msg)
            if i % 1000 == 0: print(i, len(jobs), stats, flush=True)
    print('done', stats)
    if fails:
        os.makedirs(a.out, exist_ok=True)
        open(os.path.join(a.out, '_failures.txt'), 'w').write('\n'.join(fails) + '\n')
        for f in fails[:15]: print('  ', f)
