# Color Variation node: research and proposal

Question: can Quixlend ship its own "Color Variation" node group, in the spirit of Poliigon's but better?

Scope: design from primary sources only. The Poliigon node's internals were not inspected and are not reproduced. Its parameter list below is **user-supplied and unverified**; Poliigon's help centre was searched again on 2026-11-18 and still has no documentation for a node called "Color Variation" (the nearest hit is Poliigon's manual Mix-node imperfection guide, https://help.poliigon.com/en/articles/2843818-using-poliigon-surface-imperfections-in-blender, which does not mention it).

Status legend: **[V-doc]** verified in a primary doc (URL given). **[V-src]** verified by reading Blender source (projects.blender.org). **[V-bpy]** verified by running Blender 5.2.1 LTS (build 2026-08-25) on 2026-11-18. **[V-paper]** verified in the cited paper or first-party page. **[design]** a design choice, not a fact. **[unverified]** inference or memory, not checked against a primary source.

Method for [V-bpy]: the live Blender (via the Blender MCP) was used for version and socket probing only. All renders ran in a background `Blender -b` of the same `/Applications/Blender.app` 5.2.1 LTS binary, from scripts kept in the session scratchpad (Cycles CPU, 1 sample, filter width 0.01, Standard view transform, 32-bit EXR read back with numpy). Emission shader plus orthographic camera over a 20x20 m plane, 1024x1024 px, so each field has about 1M samples over about 1600 noise cells at Scale 2. EEVEE (`BLENDER_EEVEE`, the only EEVEE id in 5.2.1) was run where noted. No datablocks were left in the live Blender or in repo files.

## 1. Repo context

- No `GLOSSARY.md` and no `docs/adr/` exist (`git ls-files`), so there are no glossary terms or ADRs to conflict with. `AGENTS.md` and `docs/agents/*` say issues and specs go in `.scratch/<feature>/`. Nothing in the repo defines a research-notes location, so this file uses `docs/research/` (the location the task named).
- Target Blender: `blender_version_min = "5.2.0"` (`blender_manifest.toml`). Docs are cited from `/manual/en/latest/` (5.3 at the time of writing) and `/api/current/`. Node sockets were checked on a live 5.2.1 LTS.
- Material construction lives in `bl/build.py::build_group_material`. One node group per material, named like the material. A Mapping node feeds the group's `Vector` input. Inside, `tex()` creates one Image Texture per map and every one takes the same `Vector`.
- The colour chain today is: albedo, then multiply AO (`mix` helper, Multiply), then Tint, then Hue/Saturation/Value (hue is offset by 0.5 = no shift), then exposure-style Brightness, then contrast about 0.18, then `emit("Base Color")`. Sockets are created with `ng.interface.new_socket(...)` and grouped with `face.new_panel(...)`. Sliders are `FACTOR` subtype with `min_value`/`max_value`.
- The group is built by a standalone background Blender (`build.py` has no add-on imports), and materials are written into per-asset `.blend` files. A new shared nested group therefore has to be created inside `build.py` and carried into each asset `.blend` (see risk R6).

## 2. Blender building blocks (all checked)

| Need | Node / API | Fact | Source |
|---|---|---|---|
| Low-frequency blotches | Noise Texture, 4D | Fractal Perlin noise. Dimensions 1D to 4D; 4D takes Vector plus **W** as the fourth dimension. Inputs: Scale, Detail (fractional allowed, blends octaves), Roughness, Lacunarity, Offset, Gain, Distortion. Output Factor and Color ("different fractal noise in each component"). An unlinked Vector defaults to Generated coordinates, so the group's `Vector` must be linked. | https://docs.blender.org/manual/en/latest/render/shader_nodes/textures/noise.html |
| Output range | Noise "Normalize" | Enabled: "ensures that the output values stay in the range 0.0 to 1.0"; disabled: "at most -(Detail+1) to Detail+1 (smaller if Roughness < 1)". bpy default is `True`. Measured, defaults, normalize off: -1.003..0.966 (so the doc bound is loose), mean 0.000, std 0.256. | same page; https://docs.blender.org/api/current/bpy.types.ShaderNodeTexNoise.html ; [V-bpy] |
| Integer-alignment pitfall | Noise notes | "any discrete evaluation of noise at integer multiples of the reciprocal of the noise scale will always evaluate to 0.5". Mitigations: change scale, add an arbitrary offset, or add a dimension. The manual says White Noise is "almost always preferred" for discrete evaluations. Measured 4D, Scale 2, Distortion 0: exactly 0.5 at (0,0,0,W=0) and at (0.5,1.0,1.5,W=2.5); 0.519 at W=2.6. With Distortion 0.2 the origin gives 0.561, so distortion breaks the alignment, but the offset in the group is kept as a guard. | noise.html (Notes); [V-bpy] |
| Per-cell/per-object hash | White Noise Texture | Returns a random number in 0..1 from a 1D to 4D seed; outputs Value and Color. Precision: "The slightest difference in seed values would result in completely different outputs"; mitigations include adding an arbitrary value to the seed. Outputs are not guaranteed unique. | https://docs.blender.org/manual/en/latest/render/shader_nodes/textures/white_noise.html |
| Per-object random | Object Info > Random | "Random number unique to a single object instance. Output is a Float between 0.0 and 1.0". "Only works for material shading nodes". The manual describes it as a way to vary one material across instances. Measured behaviour in 2.1. | https://docs.blender.org/manual/en/latest/render/shader_nodes/input/object_info.html ; [V-bpy] |
| Remap / contrast / threshold | Map Range | Inputs From/To Min/Max, interpolation Linear, Stepped Linear, Smooth Step, Smoother Step, Clamp option. Measured (From 0.4..0.6, value 0.45): Linear 0.25, Smooth Step 0.15625, Smoother Step 0.10352. Value 0.7: Linear 1.0 with Clamp on, 1.5 with Clamp off; Smooth and Smoother Step return 1.0 either way (they do not extrapolate). | https://docs.blender.org/manual/en/latest/render/shader_nodes/utilities/math/map_range.html ; [V-bpy] |
| Colour combine | Mix (RGBA) | Factor, A, B. Blend modes include Multiply, Overlay, Soft Light, Linear Light, Hue, Saturation, Value, Color. **Clamp Factor** (default on) limits the factor to 0..1, otherwise extrapolates. **Clamp Result** (default off) limits to 0..1. Measured: Float Mix with Factor 1.5, A=0, B=1 gives 1.0 (Clamp Factor on) and 1.5 (off). RGBA Multiply with B=0.5 grey and A=(0.5,0.4,0.2): Factor 0 gives A, Factor 1 gives A*B, Factor 0.5 the midpoint. B=3: Clamp Result off gives (1.5,1.2,0.6), on gives (1,1,0.6). | https://docs.blender.org/manual/en/latest/render/shader_nodes/utilities/math/mix.html ; https://docs.blender.org/api/current/bpy.types.ShaderNodeMix.html ; [V-bpy] |
| Hue/sat/value | Hue/Saturation/Value | Hue 0..1 is -180..+180 degrees ("0 and 1 have the same result", so 0.5 = no change). Saturation: 0 removes colour, above 1 increases. Value 1 = unchanged, 0 = black. Has a Factor. "Grayscale images have no hue"; measured: grey input stays grey (max chroma 0.0). | https://docs.blender.org/manual/en/latest/render/shader_nodes/color/hue_saturation.html ; [V-bpy] |
| Coordinates | Texture Coordinate | Generated is 0..1 over the undeformed bounding box. Object is relative to an object (empty). UV is the active UV map. | https://docs.blender.org/manual/en/latest/render/shader_nodes/input/texture_coordinate.html |
| Mapping | Mapping node | Point type: order "Scale -> Rotate -> Translate". "Scaling the texture coordinates up would result in the evaluated texture to scale down." | https://docs.blender.org/manual/en/latest/render/shader_nodes/utilities/vector/mapping.html |
| Image extension | Image Texture | Extension modes: Repeat, Extend, Clip, Mirror. Interpolation: Linear, Cubic, Closest, Smart (Smart is OSL only). The page says nothing about mipmaps, filtering or derivatives. | https://docs.blender.org/manual/en/latest/render/shader_nodes/textures/image.html |
| Build programmatically | `NodeTreeInterface` | `new_socket(name, *, description, in_out, socket_type, parent)` and `new_panel(name, *, description, default_closed)`. | https://docs.blender.org/api/current/bpy.types.NodeTreeInterface.html |
| Hash Value node | `FunctionNodeHashValue` | **Not available in shader trees**: `ng.nodes.new("FunctionNodeHashValue")` on a `ShaderNodeTree` raises "Cannot add node of type FunctionNodeHashValue ... Not a geometry node tree". Use White Noise instead. | [V-bpy] (re-run 2026-11-18) |
| Socket names (5.2.1) | live check | Noise 4D inputs: Vector, W, Scale, Detail, Roughness, Lacunarity, Distortion; outputs Factor, Color. White Noise outputs Value, Color. Map Range inputs Value, From Min, From Max, To Min, To Max. HSV inputs Hue, Saturation, Value, Factor, Color. Object Info outputs include Random. Mix blend types include MULTIPLY, OVERLAY, SOFT_LIGHT, LINEAR_LIGHT, HUE, SATURATION, COLOR, VALUE. Mix has one socket set per data type, addressed by identifier (`Factor_Float`, `A_Float`, `B_Float`, `Result_Float`; `A_Color`, ...), not by name, when scripting. | [V-bpy] |
| OSL | Script node | "Cycles Only". The node "is only evaluated if Open Shading Language is enabled" (Render Properties). "OSL is not supported with GPU rendering unless using the OptiX backend." So OSL is out for EEVEE and for GPU Cycles except OptiX. | https://docs.blender.org/manual/en/latest/render/shader_nodes/utilities/script.html ; https://docs.blender.org/manual/en/latest/render/cycles/osl/index.html |
| Image Texture derivatives | EEVEE GLSL / Cycles SVM | EEVEE linear sampling: `dx = gpu_dfdx(co.xy) * texture_lod_bias_get(...)`, `color = textureGrad(ima, co.xy, dx, dy)`. The derivatives are screen-space derivatives of the coordinate arriving at the Image Texture node (verified at tags v5.2.1 and v5.3.0 and on main). Cycles `svm_image_texture` takes a `dual2 uv` (value plus differentials). Consequence for shifted coordinates is in 3 and 4.4. | https://projects.blender.org/blender/blender/src/branch/main/source/blender/gpu/shaders/material/gpu_shader_material_tex_image.bsl.hh (5.2.1: same directory, `gpu_shader_material_tex_image.glsl`) ; https://projects.blender.org/blender/blender/src/branch/main/intern/cycles/kernel/svm/image.h [V-src] |
| Library write | `bpy.data.libraries.write` | Writing only a material that uses a node group to a `.blend` also stores the group: reloading lists `node_groups: ['Quixlend Color Variation v1']` and the appended material still points at it. | [V-bpy] |
| Anti-tiling node | manual index | The 5.3 shader-node manual indexes (Textures, Input, Color, Utilities) list no anti-tiling / randomised-tiling node. Textures: Brick, Checker, Environment, Gabor, Gradient, IES, Image, Magic, Noise, Sky, Voronoi, Wave, White Noise. The Utilities index lists a "Repeat Zone" (not evaluated here). | index pages under https://docs.blender.org/manual/en/latest/render/shader_nodes/ [V-doc] |

### 2.1 Measurements

All [V-bpy], method above.

**Noise 4D Factor, Normalize on** (Vector = Object coordinates, W=0 unless stated; Distortion 0.2 unless stated; Lacunarity 2):

| Config | mean | std | min | max | p0.1 | p1 | p5 | p25 | p50 | p75 | p95 | p99 | p99.9 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **Proposed default** (Scale 2, Detail 2, Rough 0.5) | 0.500 | 0.073 | 0.213 | 0.776 | 0.290 | 0.336 | 0.380 | 0.450 | 0.500 | 0.550 | 0.623 | 0.666 | 0.711 |
| Same, W=7.3 (another slice) | 0.504 | 0.081 | 0.204 | 0.786 | 0.269 | 0.320 | 0.369 | 0.448 | 0.505 | 0.561 | 0.637 | 0.685 | 0.730 |
| Fine layer (Scale 14, Detail 2) | 0.500 | 0.074 | 0.205 | 0.786 | 0.287 | 0.333 | 0.378 | 0.449 | 0.500 | 0.550 | 0.623 | 0.666 | 0.713 |
| Detail 0 | 0.500 | 0.111 | 0.125 | 0.878 | 0.200 | 0.257 | 0.314 | 0.423 | 0.500 | 0.578 | 0.688 | 0.742 | 0.795 |
| Detail 5 | 0.500 | 0.066 | 0.240 | 0.747 | 0.311 | 0.353 | 0.392 | 0.455 | 0.500 | 0.545 | 0.610 | 0.649 | 0.690 |
| Distortion 0 | 0.500 | 0.071 | 0.224 | 0.787 | 0.293 | 0.341 | 0.382 | 0.451 | 0.500 | 0.548 | 0.620 | 0.663 | 0.707 |

Normalised noise is centred on 0.5 but clusters tightly: p5..p95 is about 0.38..0.62 at the defaults (about 0.37..0.64 on another slice), and it never reaches 0 or 1. The draft's assumption (clusters near 0.5, needs a stretch) is confirmed; a raw 0..1 Map Range would leave the Value field almost flat.

**Noise Color channels** (defaults, same pixels, 1M samples): R equals Factor exactly (max abs difference 0.0); the Color output's R channel is the Factor. G: mean 0.498, std 0.079, p5/p95 0.368/0.627, p0.1/p99.9 0.267/0.726. B: mean 0.499, std 0.079, p5/p95 0.367/0.628. Correlations: R-G -0.004, R-B +0.009, G-B -0.006 (a 16-px-subsampled G-B check gives +0.007); correlation of absolute deviations from 0.5 (variance coupling): Factor-G -0.007, G-B 0.0001. Factor vs mean(R,G,B): 0.55 (expected, R is a third of the mean). So G and B are decorrelated from Factor and from each other and are unbiased (means within 0.002 of 0.5); their spread is about 8 percent wider than Factor's. One noise evaluation does give three usable fields, with Factor for value and G, B for hue and saturation. The three-Noise fallback in R4 is not needed.

**Object Info > Random** (one material = emission of Random, objects share one mesh unless stated):

| Case | Cycles | EEVEE |
|---|---|---|
| 5 separate objects Alpha, Beta, Gamma, Delta, Eps | 0.3442, 0.1980, 0.3153, 0.7732, 0.6043 | within 3e-4 of Cycles (0.3440, 0.1979, 0.3152, 0.7730, 0.6040) |
| Linked duplicates of Alpha (shared mesh) Alpha.001, Alpha.002 | 0.9477, 0.3458 | 0.9473, 0.3457 |
| Full copy with its own mesh, Alpha.003 | 0.8530 | 0.8525 |
| 3 empties instancing one collection | 0.1893, 0.9875, 0.8347 | 0.1892, 0.9873, 0.8345 |
| Re-render, same session | identical | identical |
| Change frame (0 to 5) | identical | identical |
| Rename Beta to BetaRenamed | 0.1980 to 0.3367 | 0.1979 to 0.3364 |
| Same object name in a separate Blender process | same value (Alpha 0.3442 in both runs) | |

Findings: non-instanced single objects do get a non-constant Random; duplicates get different values (Alpha.002 landing at 0.346 next to Alpha's 0.344 is a coincidence, they are not identical); collection instances differ per instance; Cycles and EEVEE agree to float16-level differences; the value is stable across renders, frames and sessions but **changes when the object is renamed**. That rename sensitivity is measured; the mechanism (a hash of the name) is [unverified] and a move/scale dependence was not tested.

**Seed and Per Object decorrelation** (full group Mask, Scale 2, Object coordinates; correlation of Mask against Mask at Seed 0, Per Object 0):

| Seed delta | 0.01 | 0.02 | 0.05 | 0.1 | 0.25 | 0.5 | 1 | 3.7 | 10 |
|---|---|---|---|---|---|---|---|---|---|
| Scale 2 | 0.983 | 0.950 | 0.839 | 0.689 | 0.289 | -0.005 | 0.008 | -0.009 | 0.002 |

At Scale 0.5: delta 0.05, 0.1, 0.5, 1.0 give 0.978, 0.937, 0.598, 0.167. Noise decorrelates once W moves by about 1/Scale (W is multiplied by Scale inside the node). Consequence: Object Info Random spans only 0..1, so with Scale 2 two objects whose Random values differ by under 0.3 share most of their pattern. Measured on four objects with Per Object 1 (pairwise Mask correlation, 6 pairs): W = Random x 1 gives 0.929, -0.007, 0.138, -0.009, 0.107, 0.041 (one pair nearly identical); W = Random x 100 gives -0.048, -0.024, 0.007, -0.068, -0.019, 0.018. The proposed group therefore scales Random by 100. Per Object 0 makes all four objects identical (max abs difference 5e-5).

**Built group** (`Quixlend Color Variation`, 34 nodes excluding Group Input/Output, 17 inputs in 3 panels, outputs Color, Mask, Roughness Scale; built in a `ShaderNodeTree` in `Blender -b`; all links valid, renders in Cycles and EEVEE): Mask at defaults (Contrast 0.5, Threshold 0, Fine Mix 0.25, Strength 1):

| Variant | std | p1 | p5 | p25 | p50 | p75 | p95 | p99 |
|---|---|---|---|---|---|---|---|---|
| **Default** | 0.231 | 0.029 | 0.118 | 0.326 | 0.501 | 0.674 | 0.882 | 0.968 |
| Contrast 0 | 0.146 | 0.177 | 0.259 | 0.397 | 0.501 | 0.603 | 0.741 | 0.820 |
| Contrast 1 | 0.412 | 0.000 | 0.000 | 0.021 | 0.504 | 0.979 | 1.000 | 1.000 |
| Threshold -0.1 | 0.165 | 0.000 | 0.000 | 0.026 | 0.119 | 0.259 | 0.500 | 0.658 |
| Threshold +0.1 | 0.165 | 0.335 | 0.500 | 0.742 | 0.883 | 0.974 | 1.000 | 1.000 |
| Fine Mix 0 | 0.272 | 0.000 | 0.054 | 0.284 | 0.501 | 0.717 | 0.946 | 1.000 |
| Strength 0 | 0.000 | 0.5 | 0.5 | 0.5 | 0.5 | 0.5 | 0.5 | 0.5 |

Cycles versus EEVEE Mask at defaults: max abs difference 5.8e-4, mean 1.7e-4, correlation 0.9999999. Roughness Scale (= Mask + 0.5) spans 0.5..1.5, mean 1.002. Colour path, input (0.6, 0.4, 0.2): luma(out)/luma(in) with Dark and Bright Intensity 0 and Preserve Luminance 1 is 1.00004..1.00006 (hue/saturation shift costs no luma); with Preserve Luminance 0 it swings 0.65..1.05. At the default intensities the luma ratio spans 0.7071..1.4142 (exactly 2^-0.5..2^0.5), p1/p99 0.72/1.38. Maximum output channel is 1.21 for the colour above and 1.273 for a 0.9 grey (0.9 x 2^0.5), so Bright Intensity 0.5 can push albedo past 1 (R5).

## 3. Anti-tiling literature

| Technique | What it is | Cost | Expressible with shader nodes? | Source |
|---|---|---|---|---|
| IQ Technique 1 | Per-tile random offset and mirroring. Blend four neighbouring tiles near borders (`smoothstep(0.25, 0.75, fuv)`) to hide seams. His code passes the original screen-space derivatives (`ddx`, `ddy` of `uv`) to `textureGrad` ("for correct mipmapping"); he notes the per-tile hash can alias at high minification. | 4 fetches | **Yes in principle**: Floor/Fraction, White Noise (2D seed) as hash, Vector Math, 4 Image Textures, Mix. A material graph cannot pass the original derivatives: EEVEE derives them from the shifted coordinate at the Image Texture input (section 2, [V-src]), and a per-tile offset is discontinuous at tile borders, so mip selection there is expected to be wrong. Visual effect not rendered: [unverified]. | https://iquilezles.org/articles/texturerepetition/ [V-paper] |
| IQ Technique 2 | "Bombs" the surface with randomly scaled, offset and rotated copies blended by smooth-Voronoi weights (`w = exp(-5 d)`, renormalised by total weight). | 9 fetches ("the algorithm samples the texture 9 times") | Technically yes (Voronoi plus loop unrolled), but 9 image nodes per map is impractical here. | same |
| IQ Technique 3 | A low-frequency index (procedural noise, a LUT or a texture) selects between virtual patterns made of constant offsets (he uses 8, offsets suffice "most of the time"); the float index interpolates the two closest. "Only two texture fetches"; the blend contrast is boosted using the difference between the two samples (`smoothstep(0.2, 0.8, f - 0.1*sum(cola - colb))`). | 2 fetches | **Yes, cheapest option**: Noise (or White Noise lookup), Floor, Fraction, Math sine hash, Vector Add, 2 Image Textures, Mix. Same derivative caveat as Technique 1 (his code uses original `dx, dy`). | same |
| Heitz and Neyret 2018 | Triangle-lattice tiling with random offsets and barycentric blending. Histogram-preserving blend: Gaussianise input, blend, restore variance, invert via LUT. Needs per-texture precomputed transform and inverse textures. **Published in Proc. ACM Comput. Graph. Interact. Tech. (PACMCGIT) 1(2), HPG 2018, article 31, DOI 10.1145/3233304; not JCGT** (the task prompt's "JCGT 2018" was a mislabel). | 3 fetches plus 2 LUT lookups | **Not as published**: precompute step plus LUT textures, which a material-wide Image Texture graph cannot hide. | https://hal.archives-ouvertes.fr/LJK/hal-01824773v1 [V-paper] (the venue line is read from this record). The dblp record https://dblp.uni-trier.de/rec/journals/pacmcgit/HeitzN18.html answers automated clients with an anti-bot challenge, so it was not read. |
| Burley 2019 (JCGT 8(4)) | Removes the lengthy precompute (the paper cites Heitz and Neyret needing "several minutes for a 256 x 256 texture"): per-channel 1D LUTs, truncated Gaussian with a soft-clipping contrast operator, exponentiated blending weights to reduce ghosting; YCbCr blending with histogram preservation on luminance only for textures that show colouration artefacts. States that linear blending "suffers ghosting and contrast loss". | 3 fetches plus LUTs | Partly: weight exponentiation and a contrast restore are plain math nodes. The Gaussianisation LUT is not, without baking an extra image per map. | https://jcgt.org/published/0008/04/02/paper-lowres.pdf [V-paper] |
| Mikkelsen 2022 (JCGT 11(2)) | Hex-tiling without histogram preservation: sample the original texture at a random offset per hex tile, blend three tiles, with a weight metric and a contrast function applied directly to the weights. Emphasises normal maps (and allows random rotation there); for colour "there is a trade-off in quality when observed side by side, though in our experience the compromise is acceptable". Reference shader code is in the appendix listings (the shading language was not checked). | 3 fetches | **Yes in principle**: lattice math in Vector/Math nodes (tens of nodes), 3 Image Textures. Practicality is a node-count and maintenance question. | https://jcgt.org/published/0011/03/05/paper-lowres.pdf [V-paper] |
| Stochastic texture sampling | Not separately researched. Treated as the family above. | | [unverified] | |
| Blender's own approach | No built-in anti-tiling node in the 5.3 shader-node manual indexes (section 2). Image Texture only offers Repeat, Extend, Clip, Mirror. The manual points users to Object Info Random for per-instance variation ("a Noise texture can give random colors or a Color Ramp can give a range of colors to be randomly picked from"). | | | image.html and object_info.html above [V-doc] |

Key takeaway: Color Variation (a colour-in, colour-out group) can only modulate an already-sampled albedo. It hides repetition statistically. It does not remove it. True anti-tiling has to run before the Image Texture nodes, so it is a different feature with a different cost.

## 4. Proposal

### 4.1 Positioning versus the Poliigon-style node

Claimed advantages, with what backs each:

| Claim | Basis |
|---|---|
| No proprietary dependency | By construction. Only stock Blender nodes. |
| Per-object randomisation | Object Info > Random feeds Noise W [V-doc]. Works in Cycles and EEVEE, differs for non-instanced objects, duplicates and collection instances, stable across renders, frames and sessions, changes on rename [V-bpy] (2.1). Needs the x100 scale to decorrelate objects [V-bpy]. |
| Seed that cannot collide | 4D noise with W as seed [V-doc]. Integer-alignment pitfall handled by an arbitrary offset [V-doc, V-bpy]. Seeds closer than about 1/Scale in W are still correlated [V-bpy], so "cannot collide" holds only for seed steps of at least 1/Scale. |
| Hue, saturation, value controlled separately | Single HSV node takes all three [V-doc]; G and B of Noise Color are decorrelated from Factor and each other [V-bpy]. |
| Luminance-preserving hue and saturation shifts | Built and measured: luma ratio 1.00004..1.00006 with Preserve Luminance 1, versus 0.65..1.05 without [V-bpy]. |
| Multi-scale | Two noises at different scales. Built; Fine Mix 0.25 lowers Mask std from 0.272 to 0.231 [V-bpy]. |
| Mask output | Group output; Mask 0.5 neutral, spans 0..1 [V-bpy]. Also feeds roughness (Roughness Scale 0.5..1.5) [V-bpy]. |
| Works on other channels | Same field drives a roughness multiplier [V-bpy, output exists]. |
| EEVEE-compatible, minimal nodes | Only core shader nodes, no OSL; builds in a ShaderNodeTree and renders in Cycles and EEVEE with Mask difference under 6e-4 [V-bpy]. **34 nodes** as built (the earlier 20 was an underestimate). |
| No tiling artifacts | **Not claimed for this node.** See 4.4. |

Comparing parameter-by-parameter with Poliigon (Scale 1.971, Distortion 0, Random Color Var 1, Threshold -2.2, Dark/Bright Spot Intensity 0.5, Seed 1) is not possible beyond the supplied list, which stays user-supplied and unverified. The negative Threshold suggests a raw un-normalised noise range (measured unnormalised Factor spans about -1.0..1.0 at Detail 2), but that is speculation [unverified]. Our design avoids that by normalising and then thresholding around 0.5.

### 4.2 Socket interface

Group name: `Quixlend Color Variation`. Sliders use `FACTOR` subtype like the rest of `build.py`. Defaults below are the ones measured in 2.1.

Inputs:

| Panel | Name | Type | Default | Range | Notes |
|---|---|---|---|---|---|
| (none) | Color | NodeSocketColor | (0.8,0.8,0.8,1) | | Albedo in. |
| (none) | Vector | NodeSocketVector | hidden value | | Coordinate for the field. Must be linked (an unlinked noise Vector falls back to Generated). See R1. |
| (none) | Strength | NodeSocketFloat | 1.0 | 0..1 | Master blend; drives HSV Factor, the stops and the Mask. 0 gives Mask 0.5 everywhere [V-bpy]. |
| Pattern | Seed | NodeSocketFloat | 0.0 | 0..100 [design] | Added to noise W. Use steps of at least 1/Scale. Range cut from 0..1000 to keep W x Scale small (float precision not measured). |
| Pattern | Per Object | NodeSocketFloat | 1.0 | 0..1 | Weight of Object Info Random added to W, after a fixed x100 scale. 0 = all instances identical [V-bpy]. |
| Pattern | Scale | NodeSocketFloat | 2.0 | 0.01..50 | Large blotch scale. |
| Pattern | Detail | NodeSocketFloat | 2.0 | 0..8 | |
| Pattern | Roughness | NodeSocketFloat | 0.5 | 0..1 | |
| Pattern | Distortion | NodeSocketFloat | 0.2 | 0..2 | Organic edges. |
| Pattern | Fine Mix | NodeSocketFloat | 0.25 | 0..1 | Linear blend of a second noise (Detail 2, Roughness 0.5, Distortion 0.2) at 7x the scale, W offset by 31.7. The 7x ratio is a [design] value, not tuned visually. |
| Value | Threshold | NodeSocketFloat | 0.0 | **-0.2..0.2** (was -1..1) | Shifts where dark/bright switch. Measured: +-0.1 already moves the Mask median to 0.12 / 0.88, so wider ranges only saturate. |
| Value | Contrast | NodeSocketFloat | 0.5 | 0..1 | Map Range half-width `h = 0.30 - 0.25*Contrast` (0.30 near-linear, 0.05 near-binary), From Min/Max = `0.5 - Threshold -/+ h`, Smooth Step, Clamp on. Default 0.5 gives h = 0.175 and Mask p5/p95 = 0.12/0.88, which uses the range without clipping more than about 1 percent each end [V-bpy]. |
| Value | Dark Intensity | NodeSocketFloat | 0.5 | 0..2 | In stops (EV). |
| Value | Bright Intensity | NodeSocketFloat | 0.5 | 0..2 | In stops (EV). |
| Hue / Saturation | Hue Variation | NodeSocketFloat | 0.03 | 0..0.25 | Fraction of the hue circle, plus and minus. |
| Hue / Saturation | Saturation Variation | NodeSocketFloat | 0.15 | 0..1 | |
| Hue / Saturation | Preserve Luminance | NodeSocketFloat | 1.0 | 0..1 | |

Outputs: `Color` (Color), `Mask` (Float, 0.5 = neutral, below darkens, above brightens; viewable as a debug overlay, standing in for Poliigon's "Mask View"), `Roughness Scale` (Float, equal to Mask + 0.5, range 0.5..1.5, 1 = no change; the material group multiplies roughness by `1 + k*(Roughness Scale - 1)` with its own slider k).

### 4.3 Node graph sketch

```
ObjectInfo.Random --*Per Object--*100--(+Seed)--(+0.5137)--> W
Vector, W, Scale, Detail, Roughness, Distortion --> Noise A (4D, normalize on)
Vector, W+31.7, Scale*7                         --> Noise B (4D)
Mix(Fine Mix, A.Factor, B.Factor)               --> n
n --MapRange(From 0.5-Threshold-h .. 0.5-Threshold+h, h = 0.30-0.25*Contrast, smooth step, clamp, To 0..1)--> m
Mask = Mix(Strength, 0.5, m)                    ==> Mask ; Roughness Scale = Mask + 0.5
m --MapRange(To -DarkIntensity .. +BrightIntensity)--*Strength--> stops --Math POWER(2, stops)--> gain
Noise A.Color --SeparateColor--> G --MapRange(0.3..0.7 -> -1..1, clamp)--MultiplyAdd(x, HueVariation, 0.5)--> hue
                               B --MapRange(0.3..0.7 -> -1..1, clamp)--MultiplyAdd(x, SatVariation, 1.0)--> sat
Color --HSV(Hue=hue, Sat=sat, Value=1, Factor=Strength)--> shifted
Color, shifted --RGBtoBW x2--> lumIn / max(lumShifted, 1e-4) = ratio
Mix(Preserve Luminance, 1.0, ratio) * gain --> s
shifted --VectorMath SCALE by s--> Color out
```

34 nodes as built and rendered in 2.1 (the sketch's Mix-Multiply luminance step was replaced by a float Mix plus Vector Math SCALE, which also settles R8). Hue and saturation use the G and B channels of Noise A's Color, stretched with a fixed 0.3..0.7 window (G and B p1/p99 are about 0.32/0.68), so one noise evaluation gives three decorrelated fields [V-bpy]. Value gain is in stops so Dark and Bright are symmetric in log space and the mean stays near neutral (measured mean luma ratio 1.013), matching the exposure-style Brightness already in `build.py`.

Plug-in point in `build_group_material`: after the albedo `tex()` and before the AO multiply (so variation does not modulate baked occlusion twice), then feed the existing Tint, HSV, Brightness and Contrast chain unchanged. Expose a `Color Variation` panel on the material group that forwards Strength, Scale, Seed and the intensities. Roughness hooks in at `scaled`/`Roughness Bias` using the `Roughness Scale` output. Create the nested group once (`bpy.data.node_groups` lookup by name), instance it as a `ShaderNodeGroup` per material, and keep the material-level default Strength at 0 so existing materials do not change look (see Recommendation).

### 4.4 Where real anti-tiling would go

Separate, later feature. Order of preference by cost and fit:
1. IQ Technique 3 with offset-only virtual patterns (2 image fetches per map). Offset-only avoids mirror/rotate problems on normal maps; Mikkelsen discusses rotation of normals separately, so flipping or rotating is a normal-map risk [unverified for Blender conventions].
2. Mikkelsen hex-tiling, if Technique 3 shows visible blotchiness (3 fetches per map plus lattice math).
3. Skip histogram-preserving blending; its LUT precompute does not fit the in-graph model.

All variants multiply Image Texture nodes per map in `tex()` (every role shares the same weights), so material node count and sample cost grow 2x to 3x. Mip selection: EEVEE takes `dFdx/dFdy` of the coordinate arriving at each Image Texture node [V-src], whereas IQ passes the unshifted `uv` derivatives. Constant per-region offsets (Technique 3) leave the screen-space derivative continuous except where the index jumps; per-tile offsets (Technique 1) and Floor/Fraction lattice math are discontinuous at tile borders. Whether that produces visible mip artefacts in EEVEE or Cycles was not rendered, so it stays [unverified] and needs a render test before committing to Technique 1.

## 5. Open risks and decisions

- R1 Coordinate choice. UV (existing `Vector`) is easiest but gives seams between UV islands on atlases and 3D assets. Object or Generated coordinates give continuity but scale with the object (Generated = 0..1 over the bounding box [V-doc]; at Scale 2 that is only two noise cells). Decide per asset type; default to the existing Mapping output for surfaces, Object coordinates for 3D assets. Unchanged, a design call.
- R2 Noise statistics. **Resolved [V-bpy]** (2.1): normalised noise is 0.5-centred with p5..p95 about 0.38..0.62. The Threshold/Contrast stretch is needed and the revised defaults in 4.2 give Mask p5/p95 = 0.12/0.88.
- R3 Object Info > Random. **Resolved [V-bpy]** (2.1): works for single non-instanced objects and for duplicates, same in Cycles and EEVEE. Remaining caveat: duplicates and renamed objects get new values. Plants ship 5-10 meshes per asset in `build.py` and a rename on append changes the look, so use Seed for a stable per-asset pattern and keep Per Object for per-instance variation.
- R4 Noise Color decorrelation. **Resolved [V-bpy]** (2.1): G and B are uncorrelated with Factor and each other and unbiased; R is identical to Factor. Use Factor for value and G, B for hue and saturation. No fallback needed.
- R5 Albedo energy: the value gain can push albedo above 1 (measured max channel 1.21 for (0.6,0.4,0.2), 1.273 for a 0.9 grey at the default 0.5 EV). Mix has Clamp Result (default off) [V-doc, V-bpy]; decide whether to clamp. `build.py` already floors at 0 with Vector Math MAXIMUM.
- R6 Shared nested group inside per-asset `.blend` files: each file carries its own copy. If the group is later improved, old assets keep the old copy. **Dependency pull-in resolved [V-bpy]**: `bpy.data.libraries.write` of only the material also stores the node group. Version tag in the group name (as in `... v1`) remains a design choice.
- R7 Interaction with Tint, Hue Shift, Saturation, Brightness and Contrast: the new group duplicates HSV controls. Keep the material group's existing sliders as global grading and the new ones as spatial variation only.
- R8 Image and Mix helper semantics: `build.py::mix` passes a float into a colour socket for the gain; Vector Math SCALE is cleaner for a per-channel-neutral gain, and is what the built group uses [V-bpy].
- Naming: the glossary does not exist, so "Color Variation", "Mask" and "Roughness Scale" are provisional; add to `GLOSSARY.md` if adopted.

## 6. Recommendation

These remain the user's calls; the picks below are the research's suggestion.

- D1 Add `.scratch/color-variation/spec.md` before implementation: **yes**. The tracker convention puts feature specs there, this note is research only, and section 4.2/4.3 already read as a spec to copy from.
- D2 Default Strength: **0 at the material group (opt-in), 1 inside the nested group**. Existing assets keep their look; the measured defaults assume Strength 1 once enabled.
- D3 Anti-tiling: **follow-up milestone**. It is a different feature (pre-sampling, 2x to 3x image fetches per map) and its mip behaviour is untested, while Color Variation is already measured and self-contained.

## 7. Still unverified

- Poliigon's actual node parameters and semantics (user-supplied list only; no documentation found; internals deliberately not inspected). The reading of its negative Threshold as an un-normalised range is speculation.
- Rendered mip/filtering behaviour of Image Texture under per-tile or per-region shifted coordinates in EEVEE and Cycles. Only the source-level facts are verified: EEVEE derivatives come from the coordinate at the node input, Cycles uses dual numbers. The Cycles chain rule through Floor/Fraction/Vector Math nodes was not read.
- Mechanism of Object Info > Random (name hash assumed from the rename result) and whether moving or scaling an object changes it.
- Float-precision behaviour of Noise at large W x Scale (the reason for the Seed 0..100 range).
- Stochastic texture sampling as a separate family (not researched).
- Normal-map handling when flipping or rotating tiles under Blender's conventions.
- The dblp record for Heitz and Neyret (bot challenge). The venue is confirmed from HAL instead.
- Mikkelsen's reference-code language (the appendix listings exist; HLSL was not confirmed).
- Seed range, Fine Mix ratio 7x and the 0.3..0.7 hue/saturation window were checked only statistically, not judged visually on real albedos.

## 8. Source index

All pages fetched 2026-11-18. Blender manual pages are the "latest" (5.3) manual.

- https://docs.blender.org/manual/en/latest/render/shader_nodes/textures/noise.html
- https://docs.blender.org/manual/en/latest/render/shader_nodes/textures/white_noise.html
- https://docs.blender.org/manual/en/latest/render/shader_nodes/textures/voronoi.html (features and outputs read; not used in the base graph, a Voronoi-based mask is a possible later variant)
- https://docs.blender.org/manual/en/latest/render/shader_nodes/textures/image.html
- https://docs.blender.org/manual/en/latest/render/shader_nodes/utilities/math/map_range.html
- https://docs.blender.org/manual/en/latest/render/shader_nodes/utilities/math/mix.html
- https://docs.blender.org/manual/en/latest/render/shader_nodes/color/hue_saturation.html
- https://docs.blender.org/manual/en/latest/render/shader_nodes/input/object_info.html
- https://docs.blender.org/manual/en/latest/render/shader_nodes/input/texture_coordinate.html
- https://docs.blender.org/manual/en/latest/render/shader_nodes/utilities/vector/mapping.html
- https://docs.blender.org/manual/en/latest/render/shader_nodes/utilities/script.html
- https://docs.blender.org/manual/en/latest/render/cycles/osl/index.html
- https://docs.blender.org/manual/en/latest/render/shader_nodes/textures/index.html and the `input`, `color`, `utilities` indexes (no anti-tiling node)
- https://docs.blender.org/api/current/bpy.types.NodeTreeInterface.html
- https://docs.blender.org/api/current/bpy.types.ShaderNodeTexNoise.html
- https://docs.blender.org/api/current/bpy.types.ShaderNodeMix.html
- https://projects.blender.org/blender/blender/src/branch/main/source/blender/gpu/shaders/material/gpu_shader_material_tex_image.bsl.hh (5.2.1 tag: `gpu_shader_material_tex_image.glsl`)
- https://projects.blender.org/blender/blender/src/branch/main/intern/cycles/kernel/svm/image.h
- https://iquilezles.org/articles/texturerepetition/ (returns HTTP 404 to curl and WebFetch with default headers but serves the article, 200, to a browser User-Agent; claims in section 3 were checked against that served text)
- https://jcgt.org/published/0008/04/02/paper-lowres.pdf (Burley 2019)
- https://jcgt.org/published/0011/03/05/paper-lowres.pdf (Mikkelsen 2022)
- https://hal.archives-ouvertes.fr/LJK/hal-01824773v1 (Heitz and Neyret 2018, venue confirmed) and https://dblp.uni-trier.de/rec/journals/pacmcgit/HeitzN18.html (not readable by automated clients)
- https://help.poliigon.com/en/articles/2843818-using-poliigon-surface-imperfections-in-blender (Poliigon's manual Mix-node approach; no Color Variation node docs found)
