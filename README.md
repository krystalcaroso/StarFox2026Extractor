# Star Fox (2026) asset extraction

Tools for extracting the contents of the game's asset pack (`romfs/data.zip`) and converting them into usable formats:

* **Models:** glTF 2.0 (`.glb`) with skeletons, skinning, morph targets, materials and textures.
* **Textures:** PNG (Radiance `.hdr` for float formats).
* **Soundtrack:** tagged FLAC with playlists.
* **Voice lines:** tagged FLAC in 13 languages, named by speaker and line, with subtitle text.

This repository contains no game data. You must supply your own legally obtained dump of the game. `tools/names.json` lists only the file paths inside the asset pack, recovered from hashes so that extracted files get their real names. It contains no assets, code or other content from the game.

All scripts live in `tools/`. Run them from this folder (the one containing `tools/`). Format notes and reverse-engineering details are in [`tools/README.md`](tools/README.md).

## Requirements

* **Python 3** with a virtual environment and three packages:
  ```
  python3 -m venv .venv
  .venv/bin/pip install lz4 numpy pillow
  mkdir -p work
  ```
  Pillow must be a recent version that includes the BCn texture decoder (tested with 13.0). The `work/` folder holds caches and intermediate files.
* **For the soundtrack only:**
  * **[vgmstream](https://vgmstream.org)**: `vgmstream-cli` decodes the game's Wwise audio (Opus, Vorbis, PTADPCM). Prebuilt command-line releases for Windows, macOS and Linux are linked from <https://vgmstream.org> and published at <https://github.com/vgmstream/vgmstream/releases>. It can also be built from source: <https://github.com/vgmstream/vgmstream>. The examples below assume the binary is at `bin/vgmstream/vgmstream-cli`.
  * **[FFmpeg](https://ffmpeg.org)**: used for FLAC encoding. Install it with your package manager (for example `brew install ffmpeg` or `apt install ffmpeg`). It must be on `PATH`, or pass its location with `--ffmpeg`.
* **Disk space:** roughly 46 GB for the extracted files, 22 GB for textures, 2 GB for models and 7 GB for audio.

## 1. Extract the asset pack

```
.venv/bin/python -I tools/extract.py <path to>/romfs/data.zip extracted
```

This decompresses every asset and names it from the recovered name table (`tools/names.json`). Files appear under `extracted/data/...`. A manifest of every archive entry is written to `extracted/_manifest.csv`.

Every later step reads from `extracted/`.

## 2. Models and textures

### 2.1 Build the GUID index

```
.venv/bin/python -I tools/guid_index.py extracted work/guid_index.json
```

Assets refer to each other by GUID. This index lets models find their materials, and materials find their textures. The model exporter reads it from `work/guid_index.json`.

### 2.2 Export textures

```
.venv/bin/python -I tools/texture_export.py extracted textures_out
```

This decodes all 8,780 textures to `textures_out/<path>.png`, or `.hdr` for float formats. Run it before the model export, which links to these files.

Options:

| Option | Effect |
|---|---|
| `--filter SUBSTR` | Only export paths containing `SUBSTR` |
| `--mips` | Also write every mip level |
| `-j N` | Number of parallel processes |
| `--force` | Overwrite existing output |

### 2.3 Export models

```
.venv/bin/python -I tools/model_export.py extracted models_out --textures textures_out
```

This writes 3,203 models to `models_out/<path>.glb`. Each model includes:

* geometry (highest LOD by default), with normals, tangents and UVs;
* the skeleton and skinning;
* blendshapes as morph targets;
* materials linked to the PNGs in `textures_out/`;
* shell fur and fur cards, reproduced from the game's own fur shaders.

The exporter also writes a few derived textures next to their sources in `textures_out/` (`*.orm.png`, `*.cutout.png`, `*.fur_*.png`). The `.glb` files reference textures by relative path, so keep `models_out/` and `textures_out/` side by side, or move them together.

Options:

| Option | Effect |
|---|---|
| `--filter SUBSTR` | Only export paths containing `SUBSTR` |
| `--lods` | Export every LOD as its own mesh |
| `--no-fur` | Leave shell-fur layers unbaked |
| `--fur-baked-colors` | Bake fur-card colours into textures (for viewers that ignore material colour factors) |
| `-j N` | Number of parallel processes |
| `--force` | Overwrite existing output |

Optional checking tools:

* `tools/render_glb.py model.glb out.png`: a small software renderer.
* `tools/preview_tex.py model.glb out.png`: a quick point-sampled preview.

They need extra packages: `.venv/bin/pip install pygltflib matplotlib`.

## 3. Soundtrack

### 3.1 Map events to audio

```
.venv/bin/python -I tools/audio_map.py extracted audio_wem/embedded audio_plan.csv
```

This parses the Wwise sound banks and names all 4,357 audio events. Sounds embedded inside banks are written out to `audio_wem/embedded/`. Voice lines are matched to the game's dialogue files (`data/eclipse/locs/`) for their speaker and subtitle text. The output is `audio_plan.csv`, which lists every sound with its events, banks, language, output name and, for voice lines, line ID, speaker and text.

### 3.2 Convert to FLAC

```
.venv/bin/python -I tools/audio_convert.py audio_plan.csv audio_out --vgmstream bin/vgmstream/vgmstream-cli
```

This decodes every sound with vgmstream and encodes it as FLAC with FFmpeg: 28,655 files, about 7 GB. That includes the voice lines (22,810 files, 3.9 GB); see [section 4](#4-voice-lines) to convert only those.

* Music and effects go to `audio_out/<bank>/`, voice lines to `audio_out/vo/<language>/`, and the soundtrack itself to `audio_out/music/`.
* Each file is tagged with title, album, Wwise IDs, events, banks and language. Looping tracks also get `LOOPSTART`/`LOOPLENGTH`.

Options:

| Option | Effect |
|---|---|
| `--ffmpeg PATH` | Use this FFmpeg binary instead of the one on `PATH` |
| `-j N` | Number of parallel conversions |
| `--filter S` | Only convert rows whose output path contains `S` |
| `--limit N` | Only convert the first `N` rows |
| `--retag` | Rewrite the tags of existing files without decoding them again |
| `--prune` | After a full re-run, delete FLACs the plan no longer lists |

### 3.3 Build the playlists

```
.venv/bin/python -I tools/music_order.py extracted audio_plan.csv audio_out
```

This sequences the music from the game's event and route data into M3U playlists under `audio_out/_playlists/`:

* `levels/`: 16 per-level playlists.
* `routes/`: all 25 routes through the game. Routes 01–14 take the easy ending (Bolse → Venom) and 15–25 the hard ending (Area 6 → Venom).
* `other/`: 8 playlists for the music not used on any route (menus, jingles, multiplayer and so on).
* `levels_structure.txt`: an overview of each level's music structure.

The playlists refer to the files in `audio_out/music/` by relative path.

## 4. Voice lines

### 4.1 Map events to audio

```
.venv/bin/python -I tools/audio_map.py extracted audio_wem/embedded audio_plan.csv
```

This is the same step as 3.1; skip it if `audio_plan.csv` already exists. Each voice line is matched to its entry in the game's dialogue files (`data/eclipse/locs/`), which gives its line ID, speaker and subtitle text in every language.

### 4.2 Convert to FLAC

All 13 languages:

```
.venv/bin/python -I tools/audio_convert.py audio_plan.csv audio_out --vgmstream bin/vgmstream/vgmstream-cli --filter vo/
```

One language, for example US English:

```
.venv/bin/python -I tools/audio_convert.py audio_plan.csv audio_out --vgmstream bin/vgmstream/vgmstream-cli --filter "vo/english(us)/"
```

The language folders are `english(us)`, `japanese(jp)`, `german(de)`, `french(fr)`, `french(ca)`, `spanish(es)`, `spanish(419)`, `italian(it)`, `portuguese(br)`, `dutch(nl)`, `korean(kr)`, `chinese(cn)` and `chinese(tw)`. Each holds about 1,750 lines (about 300 MB).

* Files go to `audio_out/vo/<language>/` and are named `<Speaker> - <line ID>.flac`, for example `Peppy - A6110.flac`.
* Each file is tagged with the line ID as `TITLE`, the speaker as `ARTIST` and the subtitle text in that language as `LYRICS`.
* Each language folder also gets a `_lines.csv` listing every file with its line ID, speaker and text.
* About 60 lines per language (grunts, shouts and some multiplayer lines) have no dialogue entry. They keep their event name, for example `ex_fox_01__<id>.flac`.
* A handful of language-neutral lines go directly in `audio_out/vo/`.

The options are the same as in 3.2.
