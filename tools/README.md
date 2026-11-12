# Star Fox (2026) `data.zip` tools

All tools run from the project root (the folder containing `tools/`) with a local venv: `.venv/bin/python -I tools/<script>.py ...`. Setup and the end-to-end workflow are in the top-level `README.md`.

## Extract
```
.venv/bin/python -I tools/extract.py <path to>/romfs/data.zip extracted
#   --filter SUBSTR   only extract paths containing SUBSTR
#   --list            only write the manifest
```
This writes the files under `extracted/data/...`, plus `extracted/_manifest.csv`, which lists every index key with its path, status, offset and size.
Files with no recovered name go to `extracted/_unnamed/<key>.<guessed ext>`.

## Archive format (engine "veclipse", `src/shared/core/fs/vfs.zip.c`)
* `data.zip` is a real ZIP64 file (`PK\3\4` local headers, a central directory and a ZIP64 EOCD), with a `VZIP` lookup index added at the front.
* Index: `"VZIP"`, `u32 count`, then `count` records of 40 bytes each:
  `u32 name_off, u32 unk(0x65ffffff), u64 data_offset, u64 csize, u64 usize, u32 compressed, u32 fnv1a32(name)`.
  The records are sorted by the last field so the engine can binary-search it. `name_off` points into a string pool at `12 + count*40`.
* The member names are hex keys: `'%lx' % fnv1a64(lowercase(path.lstrip('/')))`, e.g. `data/eclipse/...`.
* Data: if `compressed == 1`, the data is a sequence of `[u32 size][LZ4 block]` entries. Each block decodes to 64 KiB of output and can reference the previous block's output (a linked-block LZ4 dictionary). If `compressed == 0`, the data is stored raw.
* The index has about 49k more keys than the ZIP has members. Several keys can point to one blob: real deduplication (e.g. identical `.raw` mesh chunks), plus one opaque alias key for most assets (rule not recovered).

## How names were recovered
1. Path strings were harvested from every decompressed asset and from the decompressed `main` NSO. Each one was hashed with no suffix and with `.import` appended.
   * Source assets compile to `<path>.import` (VIPR binary or JSON).
   * GPU data goes in `<path>.<N>.nvn_gen2.raw`.
2. Wwise audio: the engine rewrites `GeneratedSoundBanks/Windows` to `GeneratedSoundBanks/Switch2`.
   * Banks: `data/eclipse/audio/eclipse/generatedsoundbanks/switch2/<bankname>.bnk`.
   * Localized banks: `switch2/<lang>/vo.bnk`.
   * Media: `switch2/media/[<lang>/]<sourceid>.wem`, where the IDs come from bank HIRC/DIDX.
   * Languages: english(us), japanese(jp), french(fr), french(ca), german(de), spanish(es), spanish(419), italian(it), korean(kr), chinese(cn), chinese(tw), portuguese(br), dutch(nl).
3. `names.json` (key → path) is the result. To add more names, compute `key(path)` with `resolve.py` and add the matches.

## Files
* `vzip.py`: archive reader.
* `resolve.py` / `fastfnv.py`: hashing.
* `extract.py`: extractor.

## Audio (Wwise 2024-era, bank v154)
* `wwise.py`: bank parser (DIDX/DATA embedded media, HIRC objects).
* `audio_map.py extracted audio_wem/embedded audio_plan.csv`: extracts the embedded `.wem` files and walks event → Play action → containers/tracks → sources.
  * Event names are recovered by FNV-1/32 hashing identifiers taken from `eclipse.wproj.import`, with optional `play_`/`stop_` prefixes. This names all 4,357 events.
  * Add extra name candidates in `tools/wwise_names.txt`, separated by whitespace.
  * Voice lines: `data/eclipse/locs/*.locs_strings` (JSON) holds one entry per line, whose `id` is the voice event name (`A6010` = `a6010`). It gives the speaker (`vo.speaker` attribute) and the text per language (`k_language_xx` / `k_country_yy`; the entry without a language is US English). When a source plays from several events, the first one with an entry in that language's own bank is used. Sources can be shared between languages while playing for different events in each: the English recording of `ME910A` is also stored in every other language, where it plays only for `ME910`, which has no text. 1,697 of 1,766 voice events have one; the rest are grunts, shouts and some multiplayer lines.
* `audio_convert.py audio_plan.csv audio_out --vgmstream bin/vgmstream/vgmstream-cli`: converts with vgmstream, then ffmpeg FLAC (level 8).
  * Tags: title (event, or line ID for voice lines), album (bank), WWISE_SOURCE_ID / WWISE_EVENTS / WWISE_BANKS, language, LOOPSTART/LOOPLENGTH for looping sounds, and ARTIST (speaker) / LYRICS (subtitle text) for voice lines.
  * `--retag` rewrites the tags of existing files by stream copy, without decoding again.
  * The output contains the full sample with loop points ignored (`-i`).
  * Layout: `audio_out/<bank>/<event>__<id>.flac`, `audio_out/vo/<lang>/<Speaker> - <LINE_ID>.flac` (plus `_lines.csv`), `audio_out/_unreferenced/`. Voice lines without a dialogue entry stay `<event>__<id>.flac`.
* Codecs: Wwise Opus (0x3041), Wwise Vorbis (0xFFFF), PTADPCM (0x8311). All need vgmstream.
* Bank copies of streamed sounds are only prefetch heads, so `audio_map.py` drops them (5,252) and keeps the full streamed file.
* Music tracks (HIRC type 11, v154): `u32 numSources` at offset 0, followed by 14-byte source entries.
* Requires `vgmstream-cli` (passed with `--vgmstream`) and FFmpeg; see the top-level `README.md` for where to get them.
* Output: `audio_out/` holds 28,655 FLACs (7.0 GB); `audio_out/_index.csv` maps each file to its source ID, events and banks.
* Music linking happens in two passes:
  * A strict child-list walk decides names and folders (27,670 sources).
  * A second pass follows music-container references outside the child list (transition rules, decision-tree playlists). It only labels sources the strict walk can't reach: 65 music tracks, marked `link=transition` in `_index.csv`.
* Re-running after `audio_map.py` changes: `audio_convert.py ... --prune`. Only new paths are converted, then any `.flac` the plan no longer lists is deleted.

## Music sequencing (`music_order.py extracted audio_plan.csv audio_out`)
* Within a level: event → Play → MusicSwitch decision tree.
  * Entries are 12 bytes (`key, node, weight, prob`), and key 0 is the default branch.
  * States are ordered by `music_stage_NN`.
  * Each state leads to a MusicRanSeq playlist tree, which ends its payload: `u32 count` + 30-byte items (`seg, itemID, nChildren, rsType, loop i16, …`). Loop 0 means forever.
  * Playlist segments lead to tracks. Several tracks in one segment are stems that play together.
* Levels and routes come from `data/eclipse/levels/global.level`: the `path_definition` components (`next_map`, `path_id`) and the map definitions (`wwise_map_state`). There are 25 routes from Corneria.
* Output in `audio_out/_playlists/`:
  * `levels/NN_<level>.m3u`
  * `routes/NN_<easy|hard>-ending__<route>.m3u` (prologue → levels with `mu_mb_*` briefings → Venom cinematic → ending → credits)
  * `levels_structure.txt`

## Models (`model_export.py extracted models_out`)
* Writes glTF 2.0 binaries: `models_out/<path>.gr2.glb` / `.fbx.glb`, one per `_vrender_model_t` import.
  * Imports whose root is an animation, timeline or physics mesh are skipped (9,725 files).
  * `--lods` adds every LOD as its own mesh (default: LOD0 only), `--filter SUBSTR`, `-j N`, `--force`.
  * Before running, build `work/guid_index.json` with `guid_index.py extracted work/guid_index.json` (GUID → asset path).
* Preview: `preview.py file.glb out.png` (matplotlib render of the bind pose).

### Container: VIPR / GARD "vbin-2" (`vbin.py`, generic, driven by the reflection DB)
* `data/game/types.c.import` is the engine's full reflection database (vjson). It has 8,918 types: structs with field names, bit offsets and types, plus enums, vectors and typedefs.
  * A type's hash is `fnv1a64('_<type name>')`.
  * Type GUIDs are `00000001-0000-0000-<hash>`, printed with a word shuffle (`typedb.g2h`).
  * Builtin kinds are the hashes of `k_type_kind_*` names.
  * `typedb.py` loads and caches the database (`work/typedb.pkl`).
* File layout:
  1. `"VIPR"` and a manifest.
  2. A `GARD` block holding the vbin-2 header. At +0x28 is the rtti vector: entries are `{1, type hash}` or a 16-byte import GUID. At +0x40 is the export GUID vector.
  3. One `GARD` block per segment (`default`, `gpu-nvn-gen2`).
  4. `"GARDBYE!"`.
* Each segment's base is the byte after its `GARD`. Root objects are listed by a `void*` vector at segment+0x40.
* Tagged u64 values (the high 16 bits are the tag):

  | Tag | Meaning |
  |---|---|
  | `0x8fff` | Same-segment offset |
  | `0x9000+k` | Pointer into segment k |
  | `0xbfff` | rtti index: the vtable or an external object reference |
  | `0xcfff` | Index of a GPU blob |

  Vectors are `{ptr, n, -n, 0}` (24 bytes). A vstring is 32 bytes: `u32 len|flags`, then the characters inline, or a pointer at +8 if flag 0x40000000 is set.
* A 16-byte GUID prints as `u32le-u16le-u16le-<2 raw bytes>-u16le u32le`. The result matches the vjson GUID strings (verified).

### Render model `_vrender_model_t`
* `draw_calls[i]` is stored in GPU file `<model>.<i>.nvn_gen2.raw`, and `dc_source_lod_levels[i]` gives its LOD.
* `vertex_data_stream_offsets` give separate streams:
  * Indices: u16 or u32. Decide from the stream size, because the `index_type` enum isn't reliable.
  * P: snorm16×4. Position = xyz × `quantization_scale` + `quantization_offset`. w is the tangent handedness, and glTF w = −w.
  * C: rgba8. Exported as the custom attribute `_COLOR` (it looks like mask/AO data, not display colour).
  * T: snorm16×4. Two UV sets, each = uv × `uv_quantization_scale` + `uv_quantization_offset`. V is already top-down (glTF convention).
  * N: half4. Octahedral normal in xy and octahedral tangent in zw, with values in [0,1].
  * S: u8×4 weights (sum 255), then u8×4 joint indices into the skeleton's bone array.
* Blendshapes (format `pctns_bpn`): each `dc.blendshapes[k].vertex_data_stream_offsets` holds a position delta (snorm16×4, same quantization including the offset) and the morphed octahedral normal/tangent (absolute). These are exported as glTF morph targets with `extras.targetNames`.
* `skeleton_data` is an embedded Granny 64-bit `granny_file_info` blob. Its pointers are unrelocated offsets into the blob. The skeleton has 164-byte bones: name, parent, local transform (flags, pos, quat xyzw, 3×3 scale/shear) and InverseWorld4x4. See `granny_skel.py`. The IBMs are written to glTF as stored.
* Materials:
  * Mesh slot names come from `material_slots`.
  * The real materials come from the `.model` assets that reference the mesh's export GUID (`material_overrides` per slot).
  * Each override points to a `.material_instance`, whose `texture_bindings` (`_albedo`, `_normals`, `_rmao`, …) are texture GUIDs, which resolve to `*.png.import`.
  * All of this goes into each glTF material's `extras`.
  * With `--textures textures_out`, materials also reference the exported PNGs by relative URI. Binding names vary by shader graph, so each glTF role takes the first matching name from a priority list:
    * base colour: `Albedo`, `BaseColor`, `Diffuse`, …
    * normal: `Normals`, …
    * emissive: `Emissive`, …
    * occlusion + metallicRoughness: an `*.orm.png` derived from the `RMAO` texture and cached next to it (glTF wants R = AO, G = roughness, B = metal).
  * Cull mode `none` makes the material `doubleSided`. Render type `blended` maps to `BLEND`, and `mask`/`cutout` to `MASK`.
  * Shader-specific inputs (for example fur `_fur_direction_map` / `_height_texture`) are left in `extras`.
  * `.model` overrides are matched to mesh slots by `name_hash`, falling back to name. The `.model` `slot` numbers can be stale; `sk_andrew` was shifted by one.
  * Unbound shader parameters fall back to the shader graph's default texture (`SG_DEFAULTS`). The fur-card graph defaults `_VAHAlpha` to `characters/shared/textures/t_chr_shared_fur_cards_var.png`.
  * Fur cards (`sg_chr_shared_furcard` and `sg_chr_fox_fur`, which share an identical depth pre-pass):
    * UV0 samples the strand atlas (`_VAHAlpha`); UV1 samples the fur colour (`_BaseColor`).
    * Cutout (from the disassembly): with `a = VAH.a` and `c = AlphaClip` (default 0.5), compute `v = c<0.5 ? 2ac : 1−(2−2a)(1−c)` and keep the pixel if `v ≥ 0.5`. In glTF terms that's `alphaCutoff = 0.25/c` when c < 0.5, otherwise `1 − 0.25/(1−c)`.
    * Export: the base texture is a white `*.cutout.png` carrying the strand alpha on UV0, with `MASK`, double-sided. Card colour (`_BaseColor` sampled on UV1) is averaged per triangle and k-means-clustered into 12 colours. By default there are 24 colours, each applied as `baseColorFactor` on the shared white cutout. `--fur-baked-colors` instead uses 12 pre-tinted cutouts (`*.cutout.tint_rrggbb.png`) and constant fur roughness, for viewers that ignore factors. `extras.color_linear` keeps the exact colour.
    * Colour isn't stored in `COLOR_0`, because some viewers ignore vertex colour: macOS Preview/USDZ rendered every card white.
  * Untextured blended materials (lenses, visors) become clear glass: `Glass_Tint` (if present) at alpha 0.25, roughness 0.05.
* Shell fur (`fur_vertex_color*.shader_graph`, 27 material instances):
  * The fur draw call is N coincident copies of the skin, with the layer height `h` in vertex colour R (32 layers on Fox).
  * `shell_fur.py` is an exact port of the game's shaders, recovered by disassembly (see below).
  * Vertex: extrude by `h^shell_distance_attenuation × max(height.r, 0.005) × shell_length` along the normal.
  * Depth pre-pass fragment:
    1. `uv2 = uv + (dir.xy×2−1) × h^fur_direction_attenuation × fur_direction_multiplier`
    2. `cell = int((uv2.x + floor(uv2.y·s))·s)`, where s is `strand_density`
    3. Hugo Elias integer noise: `n = cell ^ (cell<<13)`, `rnd = ((n·(n²·15731+789221)+1376312589) & 0x7fffffff)/2³¹`
    4. `len = 1 + rnd·(H.r−1)`, `d = |fract(uv2·s)·2−1|`
    5. Keep the pixel if `h ≤ 0.01` or `(len−h)·H.g·thickness ≥ d`.
  * glTF mapping:
    * The test becomes `A ≥ h` with `A = len − d/(H.g·thickness)`, stored once as alpha in `<albedo>.fur_s<density>_t<thickness>.png` (4096², ≈5.5 texels per strand cell).
    * Each layer is its own primitive, with a MASK material and `alphaCutoff = h`. That's identical to the shader's test, up to 8-bit alpha (floor-quantized, so exact at layer heights).
    * The per-layer UV comb is applied exactly to each shell's own UV copy (`uv + δ` per vertex). An earlier version shifted the geometry instead, which pushed fur out through clothing.
  * Shell materials have `metallicFactor` 0 (the colour pass has no metal term; the inherited body ORM otherwise caused glints at grazing angles). With `--fur-baked-colors` the ORM texture is dropped instead: roughness 0.85, metal 0.
  * Per-layer fake AO from the colour pass is applied: `baseColorFactor = min(1, h^occlusion_attenuation + occlusion_bias)`.
  * Not reproduced: the rim light, and the bind-pose extrusion runs before skinning rather than after.
  * `--no-fur` keeps the raw coincident layers.
* Uniforms:
  * Defaults come from the shader graph's `default_uniform_buffer_data` (value for GPU `c5[x]` is at blob offset x+0x10). Material instance `uniform_buffer_data` overrides them.
  * The GPU offsets were matched to names through the graph's `typeinfo` segment (CPU struct offset = GPU + 0x0c).
* Checking tools:
  * `render_glb.py in.glb out.png [--view front|side|back] [--zoom 0.25]`: software rasterizer with per-pixel UVs, alpha test/blend and `COLOR_0`.
  * `preview_tex.py`: fast point-sampled preview.
* Models are Z-up. The glTF root node rotates −90° about X. Units are as stored (Fox is about 17.4 units tall).

## Textures (`texture_export.py extracted textures_out`)
* Handles every `*.png|tga|jpg|hdr.import` (`_vrender_texture_t`): 8,780 textures, all 2D with one layer.
* Output: `textures_out/<path>.png`, or `.hdr` (Radiance) for float formats.
  * The source extension is dropped (`x.png.import` → `x.png`). When a non-PNG source collides with a `.png` source of the same name, it keeps its extension (`x.tga.png`).
  * 16-bit formats are written as 16-bit PNG.
* Options: `--mips` also writes `.mipN` files; plus `--filter`, `-j`, `--force`.
* `backend[8]` (`k_vrender_backend_nvn_gen2`) describes `.0.nvn_gen2.raw`:
  * the fields are `format`, `width`/`height`, `mip_levels` and `mip_offsets`;
  * `data_sizes` equals the raw size in every case;
  * `layout & 0xf` is log2 of the block height in GOBs.
* `.1.nvn_gen2.raw` is a small low-res streaming copy and isn't used.
* Tiling: NVIDIA block-linear, with 512-byte GOBs (64 B × 8 rows) and blocks 2^n GOBs tall. The standard per-mip shrink rule applies.
  * Within a GOB, Switch 2 orders its 16-byte sectors by the address bits `y0, y1, x16, y2, x32` (low → high). Tegra X1 uses `y0, x16, y1, y2, x32`; the Switch 2 order was found by brute force and checked visually.
* Formats:
  * BC1/BC3/BC4/BC5/BC7 decode through Pillow's `bcn` decoder.
  * r8, r16, rgba8, rgba16 unorm, rgba16f and r11g11b10f are handled too.
* `_vrender_packed_swizzle_t` (r/g/b/a → identity/zero/one/r/g/b/a) is applied; BC4 greyscale, for example, uses (r, r, r, 1).
* Alpha is dropped when the source had none or it's fully opaque. Images whose R, G and B are equal are written as L (greyscale).
* Data ordering: `rmao` is R = roughness, G = metal, B = AO. UVs are V-down, matching the image orientation directly (checked on Fox).

## GPU shader disassembly
* NVN gen2 shader blob layout:
  * `u32 magic 0x19866891`, `u32 code_size` at +4;
  * a 0x98-byte header (including the shader program header), then the code;
  * NOP padding and zero padding, then a footer starting `34 12 76 98`.
* The code is NVIDIA Ampere SASS. NVIDIA's `nvdisasm`, part of the CUDA toolkit (Linux and Windows; the `cuda-nvdisasm` package is enough), decodes it: `nvdisasm -b SM86 shader.code`, where `shader.code` is the code section cut out of the blob.
  * Disassembly isn't needed to run the exporters: the recovered fur shader logic is already ported in `shell_fur.py` and `model_export.py`.
* Constant banks:
  * `c[0x5][…]` is the material uniform buffer.
  * Attributes: vertex `a[0x80]` = position, `a[0x90]` = colour, `a[0xa0]` = uv.
  * Interpolants: fragment `a[0xa0]` = h, `a[0xb0]` = uv.
