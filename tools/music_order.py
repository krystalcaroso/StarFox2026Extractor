#!/usr/bin/env python3
"""Derive play order for Star Fox (2026) music and write .m3u playlists.

usage: python -I tools/music_order.py <extracted_dir> <audio_plan.csv> <audio_out_dir>

Per level: event -> Play -> MusicSwitch decision tree (state 'music_stage_NN' order)
           -> MusicRanSeq playlist tree (sequence order, loop counts) -> segments -> tracks (stems).
Per route: route table from data/eclipse/levels/global.level (25 routes), stitched with
           mu_mb_<a>_to_<b> transitions, intros, Venom cinematics and endings.
"""
import csv, glob, json, os, re, struct, sys
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wwise import Bank, fnv1_32

ext, plan_path, out_dir = sys.argv[1:4]
root = os.path.join(ext, 'data/eclipse/audio/eclipse/generatedsoundbanks/switch2')

objs = {}
for f in glob.glob(os.path.join(root, '*.bnk')):
    for o, v in Bank(open(f, 'rb').read()).objects.items():
        objs.setdefault(o, v)
wproj = open(os.path.join(ext, 'data/eclipse/audio/eclipse/eclipse.wproj.import'), 'rb').read()
names = {fnv1_32(c): c for c in {m.group().decode().lower() for m in re.finditer(rb'[A-Za-z0-9_]{3,}', wproj)}}

flac_of = {}
for r in csv.DictReader(open(plan_path, encoding='utf-8')):
    if not r['lang']: flac_of[int(r['source_id'])] = r['flac']

RS_TYPE = {0: 'sequence', 1: 'sequence-step', 2: 'random', 3: 'random-step'}

# ---------------------------------------------------------------- parsers
def kids_by_type(p, types, me=None):
    out = []
    for i in range(0, len(p) - 3):
        x = struct.unpack_from('<I', p, i)[0]
        if x != me and x in objs and objs[x][0] in types and x not in out: out.append(x)
    return out

def playlist(p):
    """MusicRanSeq playlist: u32 count + count * 30-byte items, ending the payload."""
    for pos in range(len(p) - 4, -1, -1):
        n = struct.unpack_from('<I', p, pos)[0]
        if 0 < n < 4096 and pos + 4 + 30 * n == len(p):
            items = []
            for k in range(n):
                seg, iid, nch, rst, loop = struct.unpack_from('<IIIIh', p, pos + 4 + 30 * k)
                items.append(dict(seg=seg, n=nch, type=rst, loop=loop))
            # rebuild tree from pre-order list
            it = iter(items)
            def build():
                x = next(it); x['children'] = [build() for _ in range(x['n'])]; return x
            return build()
    return None

def decision_tree(p):
    """MusicSwitch: 12-byte (key, node, weight, prob) entries whose node is a music object."""
    best = []
    for start in range(0, 12):
        run = []
        for i in range(start, len(p) - 11, 12):
            key, node = struct.unpack_from('<II', p, i)
            if (key in names or key == 0) and (node in objs and objs[node][0] in (10, 11, 12, 13)):
                run.append((names.get(key, '~default'), node))   # key 0 = default branch; '~' sorts last
        if len(run) > len(best): best = run
    return best

def natkey(s): return [int(t) if t.isdigit() else t for t in re.split(r'(\d+)', s)]

# ---------------------------------------------------------------- expansion
def segment_step(seg, loop=1, label=''):
    tracks = [t for t in kids_by_type(objs[seg][1], (11,), seg)]
    files = []
    for t in tracks:
        for s in Bank.music_track_sources(objs[t][1]):
            if s in flac_of and flac_of[s] not in files: files.append(flac_of[s])
    return dict(segment=seg, files=sorted(files), loop=loop, label=label)

def expand(node, label='', seen=None):
    seen = set() if seen is None else seen
    if node in seen or node not in objs: return []
    seen.add(node)
    t, p = objs[node]
    if t == 10: return [segment_step(node, 1, label)]
    if t == 11:
        fs = [flac_of[s] for s in Bank.music_track_sources(p) if s in flac_of]
        return [dict(segment=None, files=fs, loop=1, label=label)] if fs else []
    if t == 13:
        tree = playlist(p)
        steps = []
        def walk(x, lab):
            if x['seg']:
                st = segment_step(x['seg'], x['loop'], lab)
                if x['seg'] not in seen: steps.append(st)
            else:
                mode = RS_TYPE.get(x['type'], '?')
                for c in x['children']: walk(c, lab + ('' if mode == 'sequence' else ' [%s]' % mode))
        if tree: walk(tree, label)
        return steps
    if t == 12:
        steps = []
        for state, n in sorted(decision_tree(p), key=lambda kv: natkey(kv[0])):
            if state == 'music_none': continue
            steps += expand(n, (label + ' ' + state.lstrip('~')).strip(), seen)
        return steps
    return []

def event_steps(name):
    e = fnv1_32(name)
    if e not in objs: return []
    steps = []
    for a in Bank.event_actions(objs[e][1]):
        at, tg = Bank.action_target(objs[a][1])
        if at == 0x403: steps += expand(tg, '')
    return steps

# ---------------------------------------------------------------- levels
g = json.load(open(os.path.join(ext, 'data/eclipse/levels/global.level')))
PATHDEF = 'c09512c8-5c63-11ee-98ca-567409dd0f3c'
edges, mapname = defaultdict(list), {'k_mapno_cn': 'Corneria'}
def gwalk(e):
    for c in e.get('components', []):
        if c.get('vtable') == PATHDEF: edges[e['name'].strip()].append(c['next_map'])
        elif 'map_name' in c: mapname[c['map_name']] = e['name'].strip()
    for ch in e.get('children', []) or []: gwalk(ch)
for e in g['segments'][0]['exported_objects'][0]['entities']: gwalk(e)

def key(m): return {'Venom_2': 'venom2', 'Venom': 'venom1'}.get(m, m.lower().replace(' ', '_'))
LEVEL_EVENTS = defaultdict(list)
for m in set(mapname.values()) | {'Corneria'}:
    k = key(m)
    if k == 'venom1':
        LEVEL_EVENTS[m] = ['mu_lvl_venom1', 'mu_lvl_venom1_checkpoint_02', 'mu_lvl_venom1_end_sting']
    elif k == 'venom2':
        LEVEL_EVENTS[m] = ['mu_lvl_intro_venom_2', 'mu_lvl_venom2', 'mu_lvl_venom2_corridor', 'mu_lvl_venom2_checkpoint_02']
    else:
        LEVEL_EVENTS[m] = ['mu_lvl_intro_' + k, 'mu_lvl_' + k, 'mu_lvl_' + k + '_01']

def dedupe(steps, used):
    out = []
    for s in steps:
        fs = [f for f in s['files'] if f not in used]
        if fs: used.update(fs); out.append(dict(s, files=fs))
    return out

level_steps = {}
for m, evs in LEVEL_EVENTS.items():
    used = set(); st = []
    for ev in evs:
        st += [dict(s, event=ev) for s in dedupe(event_steps(ev), used)]
    if st: level_steps[m] = st

# ---------------------------------------------------------------- routes
routes = []
def dfs(node, route):
    if node in ('Venom', 'Venom_2'): routes.append(route); return
    for nm in edges.get(node, []): dfs(mapname[nm], route + [mapname[nm]])
dfs('Corneria', ['Corneria'])
routes.sort(key=lambda r: (r[-1], r))

def event_block(ev, title):
    return [dict(s, event=ev, label=(title + ' ' + s['label']).strip()) for s in event_steps(ev)]

def route_steps(r):
    st = event_block('mu_cine_prologue', 'prologue')
    for i, m in enumerate(r):
        st += [dict(s, label=('%s %s' % (m, s['label'])).strip()) for s in level_steps.get(m, [])]
        if i + 1 < len(r):
            nxt = r[i + 1]
            if nxt == 'Venom': st += event_block('mu_cine_bolse_to_venom', 'cinematic')
            elif nxt == 'Venom_2': st += event_block('mu_cine_area_6_to_venom', 'cinematic')
            else: st += event_block('mu_mb_%s_to_%s' % (key(m), key(nxt)), 'briefing %s→%s' % (m, nxt))
    st += event_block('mu_cine_easy_ending' if r[-1] == 'Venom' else 'mu_cine_hard_ending', 'ending')
    st += event_block('mu_credits_01', 'credits') + event_block('mu_credits_02', 'credits')
    # Keep music the game replays across levels (shared battle/boss pieces, intro stings);
    # only collapse a step identical to the one right before it.
    out = []
    for s in st:
        if not out or out[-1]['files'] != s['files']: out.append(s)
    return out

# ---------------------------------------------------------------- output
pl = os.path.join(out_dir, '_playlists')
os.makedirs(os.path.join(pl, 'levels'), exist_ok=True)
os.makedirs(os.path.join(pl, 'routes'), exist_ok=True)

def write_m3u(path, title, steps):
    rel = os.path.relpath(out_dir, os.path.dirname(path))
    with open(path, 'w') as fh:
        fh.write('#EXTM3U\n#PLAYLIST:%s\n' % title)
        for s in steps:
            loop = 'loops forever' if s['loop'] == 0 else ('x%d' % s['loop'] if s['loop'] > 1 else '')
            stems = ' (%d stems, play together)' % len(s['files']) if len(s['files']) > 1 else ''
            for f in s['files']:
                fh.write('#EXTINF:-1,%s | %s%s%s\n' % (s['event'], s['label'] or '-', (' | ' + loop) if loop else '', stems))
                fh.write(os.path.join(rel, f) + '\n')

order = ['Corneria', 'Meteo', 'Sector Y', 'Fichina', 'Katina', 'Aquas', 'Sector X', 'Solar', 'Zoness',
         'Macbeth', 'Titania', 'Sector Z', 'Bolse', 'Area 6', 'Venom', 'Venom_2']
for i, m in enumerate([m for m in order if m in level_steps], 1):
    write_m3u(os.path.join(pl, 'levels', '%02d_%s.m3u' % (i, key(m))), m, level_steps[m])

summary = []
for i, r in enumerate(routes, 1):
    end = 'easy-ending' if r[-1] == 'Venom' else 'hard-ending'
    name = '%02d_%s__%s.m3u' % (i, end, '-'.join(key(m) for m in r))
    st = route_steps(r)
    write_m3u(os.path.join(pl, 'routes', name), ' → '.join(r), st)
    summary.append((name, len(st), sum(len(s['files']) for s in st)))

with open(os.path.join(pl, 'levels_structure.txt'), 'w') as fh:
    for m in [m for m in order if m in level_steps]:
        fh.write('== %s\n' % m)
        for s in level_steps[m]:
            fh.write('  %-34s %-28s loop=%-3s %s\n' % (s['event'], s['label'] or '-', s['loop'],
                     ', '.join(os.path.basename(f) for f in s['files'])))
print('levels', len(level_steps), 'routes', len(routes))
for x in summary[:3]: print(x)

# ---------------------------------------------------------------- music not placed in level/route playlists
placed = set()
for m in glob.glob(os.path.join(pl, '**/*.m3u'), recursive=True):
    for l in open(m):
        l = l.strip()
        if l and not l.startswith('#'): placed.add(os.path.normpath(os.path.join(os.path.dirname(m), l)))
plan_rows = [r for r in csv.DictReader(open(plan_path, encoding='utf-8')) if r['flac'].startswith('music/') and not r['lang']]
info = {r['flac']: r for r in plan_rows}
left = [r['flac'] for r in plan_rows if os.path.normpath(os.path.join(out_dir, r['flac'])) not in placed]

lvl_order = ['corneria', 'meteo', 'sector_y', 'fichina', 'katina', 'aquas', 'sector_x', 'solar', 'zoness',
             'macbeth', 'titania', 'sector_z', 'bolse', 'area_6', 'venom1', 'venom2']
CATS = [  # (playlist, title, ordered event-name patterns)
    ('01_title_and_menus', 'Title & menus', ['mu_cine_title_banner', 'mu_title_first_flow', 'mu_title_second_flow',
        'mu_ui_opening', 'mu_ui_title_screen', 'mu_ui_menu_master', 'mu_ui_select_options', 'mu_ui_map_screen',
        'mu_switch_menu_map_back']),
    ('02_tutorial_and_training', 'Tutorial & training', ['mu_mb_tutorial_to_corneria', 'mu_lvl_training_intro_cine_complete',
        'mu_lvl_training_respawn', 'mu_mb_tutorial_outro']),
    ('03_jingles', 'Jingles', ['mu_all_clear', 'mu_mission_accomplished', 'mu_mission_failed', 'mu_player_down',
        'mu_player_down_mp', 'mu_game_over']),
    ('04_character_themes', 'Character themes', ['mu_bill_theme', 'mu_cat_theme']),
    ('05_cinematics_extra', 'Cinematics (extra)', ['mu_cine_invoice', 'mu_cine_easy_ending', 'mu_cine_hard_ending']),
    ('06_level_transitions_and_alternates', 'Level transitions & alternates',
        [p for k in lvl_order for p in ('mu_lvl_intro_' + k, 'mu_lvl_' + k)] + ['mu_lvl_intro_venom_2', 'mu_lvl_venom2_corridor']),
    ('07_multiplayer', 'Multiplayer', ['mu_mp_lobby', 'mu_mp_intro_team_fox', 'mu_mp_intro_team_fox_01',
        'mu_mp_intro_team_wolf', 'mu_mp_intro_team_wolf_01', 'mu_mp_lvl_corneria', 'mu_mp_lvl_fichina', 'mu_mp_lvl_katina',
        'mu_mp_lvl_sector_x', 'mu_mp_lvl_sector_y', 'mu_mp_lvl_venom', 'mu_mp_lvl_event_01', 'mu_mp_hurry_up',
        'mu_mp_hurry_up_01', 'mu_mp_results_victory', 'mu_mp_results_defeat']),
    ('08_unused_legacy', 'Unused / legacy', ['temp_mu_corneria_legacy']),
]
ev_cat = {e: c for c, _, evs in CATS for e in evs}
bucket = defaultdict(lambda: defaultdict(list))   # cat -> primary event -> files
for f in left:
    ev = info[f]['events'].split(';')[0]
    cat = ev_cat.get(ev) or ('07_multiplayer' if ev.startswith('mu_mp_') else '08_unused_legacy')
    bucket[cat][ev].append(f)
os.makedirs(os.path.join(pl, 'other'), exist_ok=True)
covered = 0
for cat, title, evs in CATS:
    if cat not in bucket: continue
    steps = []
    for ev in evs + sorted(set(bucket[cat]) - set(evs)):
        files = bucket[cat].get(ev)
        if not files: continue
        ordered = [f for s in event_steps(ev) for f in s['files'] if f in files]
        lab = {f: s for s in event_steps(ev) for f in s['files']}
        for f in list(dict.fromkeys(ordered)) + sorted(set(files) - set(ordered)):
            s = lab.get(f, dict(label='', loop=1))
            note = (s['label'] + (' via transition rules' if info[f]['link'] == 'transition' else '')).strip()
            steps.append(dict(files=[f], event=ev, label=note, loop=s['loop']))
    covered += len(steps)
    write_m3u(os.path.join(pl, 'other', cat + '.m3u'), title, steps)
print('other playlists cover', covered, 'of', len(left), 'unplaced music files')
