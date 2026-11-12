#!/usr/bin/env python3
"""Convert planned .wem files to tagged FLAC via vgmstream-cli + ffmpeg.

usage: python -I tools/audio_convert.py <plan.csv> <out_dir> --vgmstream PATH [--ffmpeg PATH] [-j N] [--limit N] [--filter S]
                                      [--retag] [--prune]
Voice lines are also tagged with speaker (ARTIST) and subtitle text (LYRICS), and each voice folder gets a
_lines.csv listing file, line ID, speaker and text.
"""
import argparse, csv, os, re, subprocess, sys
from concurrent.futures import ThreadPoolExecutor

ap = argparse.ArgumentParser()
ap.add_argument('plan'); ap.add_argument('out')
ap.add_argument('--vgmstream', required=True)
ap.add_argument('--ffmpeg', default='ffmpeg')
ap.add_argument('-j', type=int, default=os.cpu_count() or 4)
ap.add_argument('--limit', type=int, default=0)
ap.add_argument('--filter', default=None, help='only rows whose flac path contains this')
ap.add_argument('--retag', action='store_true',
                help='rewrite the tags of existing files (stream copy, no re-decode) instead of skipping them')
ap.add_argument('--prune', action='store_true',
                help='after converting, delete .flac files in out_dir that the plan no longer lists '
                     '(e.g. old names after re-running audio_map.py)')
a = ap.parse_args()

all_rows = list(csv.DictReader(open(a.plan, encoding='utf-8')))
rows = all_rows
if a.filter: rows = [r for r in rows if a.filter in r['flac']]
if a.limit: rows = rows[:a.limit]
out_root = os.path.abspath(a.out)

def info(wem):
    r = subprocess.run([a.vgmstream, '-m', wem], capture_output=True, text=True)
    d = {}
    for key, pat in (('loop_start', r'loop start: (\d+) samples'), ('loop_end', r'loop end: (\d+) samples'),
                     ('rate', r'sample rate: (\d+)'), ('codec', r'encoding: (.+)')):
        m = re.search(pat, r.stdout)
        if m: d[key] = m.group(1).strip()
    return r.returncode, d, r.stdout + r.stderr

def convert(r):
    dst = os.path.normpath(os.path.join(out_root, r['flac']))
    if not dst.startswith(out_root + os.sep): return ('unsafe', r['flac'])
    exists = os.path.exists(dst) and os.path.getsize(dst) > 0
    if exists and not a.retag: return ('skip', dst)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    rc, meta, log = info(r['wem'])
    if rc != 0: return ('fail-info', r['wem'] + ' ' + log.strip()[-200:])
    events = r['events'].split(';') if r['events'] else []
    title = r.get('line_id') or (events[0] if events else 'source %s' % r['source_id'])
    tags = ['-metadata', 'title=%s' % title,
            '-metadata', 'album=Star Fox (2026) - %s' % (r['banks'].split(';')[0] if r['banks'] else 'unreferenced'),
            '-metadata', 'WWISE_SOURCE_ID=%s' % r['source_id'],
            '-metadata', 'WWISE_EVENTS=%s' % r['events'],
            '-metadata', 'WWISE_BANKS=%s' % r['banks']]
    if r['lang']: tags += ['-metadata', 'language=%s' % r['lang']]
    if r.get('speaker'): tags += ['-metadata', 'artist=%s' % r['speaker']]
    if r.get('text'): tags += ['-metadata', 'LYRICS=%s' % r['text']]
    if 'loop_start' in meta and 'loop_end' in meta:
        ls, le = int(meta['loop_start']), int(meta['loop_end'])
        tags += ['-metadata', 'LOOPSTART=%d' % ls, '-metadata', 'LOOPLENGTH=%d' % (le - ls)]
    tmp = dst + '.part.flac'
    if exists:      # --retag: copy the audio, replace all tags
        ff = subprocess.run([a.ffmpeg, '-hide_banner', '-loglevel', 'error', '-y', '-i', dst, '-map', '0:a',
                             '-c', 'copy', '-map_metadata', '-1', *tags, tmp], capture_output=True, text=True)
        if ff.returncode != 0:
            if os.path.exists(tmp): os.remove(tmp)
            return ('fail', '%s retag ff=%s' % (dst, ff.stderr.strip()[-200:]))
        os.replace(tmp, dst)
        return ('retag', dst)
    vg = subprocess.Popen([a.vgmstream, '-i', '-p', r['wem']], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    ff = subprocess.run([a.ffmpeg, '-hide_banner', '-loglevel', 'error', '-y', '-f', 'wav', '-i', 'pipe:0',
                         '-c:a', 'flac', '-compression_level', '8', *tags, tmp],
                        stdin=vg.stdout, capture_output=True, text=True)
    vg.stdout.close(); vg.wait()
    if vg.returncode != 0 or ff.returncode != 0:
        if os.path.exists(tmp): os.remove(tmp)
        return ('fail', '%s vg=%s ff=%s' % (r['wem'], vg.returncode, ff.stderr.strip()[-200:]))
    os.replace(tmp, dst)
    return ('ok', dst)

stats = {}; fails = []
with ThreadPoolExecutor(a.j) as ex:
    for i, (st, msg) in enumerate(ex.map(convert, rows), 1):
        stats[st] = stats.get(st, 0) + 1
        if st.startswith('fail') or st == 'unsafe': fails.append(msg)
        if i % 1000 == 0: print(i, len(rows), stats, flush=True)
print('done', stats)
if fails:
    with open(os.path.join(out_root, '_failures.txt'), 'w') as fh: fh.write('\n'.join(fails))
    print('failures written to', os.path.join(out_root, '_failures.txt'))

# voice line lists: one _lines.csv per voice folder touched by this run, built from the full plan
folders = {os.path.dirname(r['flac']) for r in rows if r.get('line_id')}
for folder in sorted(folders):
    vo = sorted((r for r in all_rows if os.path.dirname(r['flac']) == folder and r.get('line_id')),
                key=lambda r: r['line_id'])
    with open(os.path.join(out_root, folder, '_lines.csv'), 'w', newline='', encoding='utf-8-sig') as fh:
        w = csv.writer(fh)
        w.writerow(['file', 'line_id', 'speaker', 'text', 'events'])
        for r in vo: w.writerow([os.path.basename(r['flac']), r['line_id'], r['speaker'], r['text'], r['events']])
if folders: print('wrote _lines.csv in', len(folders), 'voice folders')

if a.prune:
    if a.filter or a.limit:
        sys.exit('--prune needs the full plan (no --filter/--limit)')
    if fails:
        sys.exit('not pruning: there were conversion failures')
    keep = {os.path.normpath(os.path.join(out_root, r['flac'])) for r in all_rows}
    removed = 0
    for dp, dn, fn in os.walk(out_root):
        for f in fn:
            p = os.path.join(dp, f)
            if f.endswith('.flac') and not f.startswith('._') and p not in keep:   # skip macOS AppleDouble sidecars
                os.remove(p); removed += 1
    for dp, dn, fn in sorted(os.walk(out_root), reverse=True):   # drop emptied folders
        if dp != out_root and not os.listdir(dp): os.rmdir(dp)
    print('pruned', removed, 'stale .flac files')
