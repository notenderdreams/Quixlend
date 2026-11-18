# Texture anti-tiling for PBR materials: research and proposal

Question: how should Quixlend add texture anti-tiling (repetition removal) to its material node groups, better than a colour-only variation node?

Builds on `docs/research/color-variation-node.md` (not redone). That note concluded anti-tiling must run before the Image Texture nodes. This note goes deeper on that one feature.

Status legend: **[V-src]** read in a primary source (URL given). **[V-bpy]** verified by running Blender 5.2.1 LTS on 2026-11-18, on throwaway scenes/datablocks prefixed `_AT_`/`_AT2_` (never saved into any repo file). Pass 1 and the EEVEE/Cycles seam, grazing-angle and directional experiments ran in the live Blender via the Blender MCP; the live session then went away mid-run (see R8), so the mask, Object Info and real-asset experiments ran in a separate headless `Blender -b --factory-startup` (same 5.2.1 LTS build). **[derived]** my own reasoning from a primary source, not stated by it. **[unverified]** not checked against a primary source (collected in 7.3).

## 0. Bottom line

- Best fit: **Mikkelsen hex-tiling, ported to shader nodes, with ONE set of blend weights shared by all maps** (3 image samples per map, no LUT textures, optional per-tile rotation with proper normal rotation). It builds and works on Blender 5.2.1 in Cycles and EEVEE. [V-bpy]
- Known cost, measured in this pass: in **EEVEE** the hex-tiled lookup leaves thin (1-2 px) derivative-seam lines along the triangle edges (3-4x the error of plain tiling in those pixels, very faint in the beauty image, clear in a x10 difference image, see 4.2). In **Cycles** there are no seams, but the hex lookup gets **no mip filtering** even with the 5.2 Texture Cache on, while plain textures do. [V-bpy]
- Rotation must default to **0** for directional materials: Rotation = 1 destroys wood grain and brick courses (7.4). [V-bpy]
- Cheap fallback: **Quilez Technique 3** (2 samples per map, offset-only, noise-driven). Less effective against recognisable features, but trivially safe for normals. Not built here.
- Not recommended: Quilez Technique 2 (9 samples), Heitz-Neyret/Burley histogram LUT blending (needs baked per-map LUT/transformed textures that a plain node graph cannot hide).
- Blender has no built-in anti-tiling in the Image Texture node. [V-src]/[V-bpy] (see 1.6)
- Apply to the **surface** type only. Atlases and UV-unwrapped 3D assets must not use it (see 5.3). Even "surface" assets are not always tileable (see 3.5).

## 1. Candidate algorithms

### 1.1 Quilez "Technique 1": per-tile random offset and mirror, four-tile blend

Source: https://iquilezles.org/articles/texturerepetition/ (also https://www.iquilezles.org/articles/texturerepetition/; both served the page, HTTP 200, when re-checked 2026-11-18; the earlier 404 on the non-www URL did not reproduce). Article text re-read in full this pass. [V-src]

- Assigns a random offset and orientation (his example: mirror in x/y) to each integer tile. Per-tile hash gives 4 random values.
- Caveats he states: seams at tile borders, and the coordinate discontinuity makes derivatives jump so "the derivatives will have huge jumps at the tile borders and mipmapping will break apart, creating line artifacts".
- Fix: fetch the 4 neighbouring tiles (offset and orient each) and blend near the border with `smoothstep(0.25, 0.75, fract(uv))`. "This will introduced some amount of blurring". Requires `textureGrad` with "custom texture gradients ... which must come from the original repeating UV mapping", and he propagates the mirror sign to the derivatives (and notes the hardware probably takes the absolute value, so passing plain ddx/ddy is a safe optimisation).
- Cost: 4 fetches per map. Hash can alias at high minification (his own caveat).
- Can use any space-filling pattern ("triangles or hexagons"), per his text.

### 1.2 Quilez "Technique 2": smooth Voronoi bombing

Same article. [V-src]

- Bombs the surface with randomly scaled, offset, rotated copies blended with weight = Gaussian of distance to each feature point; renormalise by the sum of weights "otherwise texture brightness range will be lost".
- His code: 3x3 neighbour loop, `exp(-5.0*d)` weight (confirmed in the page source), **9 texture fetches** ("the algorithm samples the texture 9 times, which might stress the memory bus too much").
- Verdict for Blender: 9 image nodes per map. Rejected on cost.

### 1.3 Quilez "Technique 3": noise-indexed virtual patterns

Same article. [V-src]

- Imagine ~8 "virtual" copies of the tiling (constant offsets, optionally rotations/symmetries/scales). A low-frequency float index (procedural noise, or a LUT/texture lookup) picks the two nearest copies; blend them. **Two texture fetches.**
- Blend contrast boost in his code: `smoothstep(0.2, 0.8, f - 0.1*sum(cola - colb))` (uses the difference between the two samples).
- He says an index from a LUT/texture is easier to filter than a procedural function ("completely filterable resulting pattern (unlike index values based on a procedural function which is usually more difficult to filter)"). "Texture coordinate derivatives need to be computed for proper filtering since we are introducing discontinuities."
- Offsets only is "sufficient most of the time"; rotations, symmetries and scales are possible.
- Earlier drafts cited a third-party Unity forum report of seams in rotated parts of T3 (from a search snippet). **Dropped**: the article itself says nothing about rotated-variant seams and no primary source supports the forum claim. Not a claim of this note.

### 1.4 Mikkelsen hex-tiling (JCGT 11(2), 2022)

Source: https://jcgt.org/published/0011/03/05/paper-lowres.pdf (full text re-read this pass via pdftotext; every number below re-checked). [V-src]

- Builds on Heitz and Neyret: equilateral-triangle lattice, each vertex is a hex tile with a random offset; the sample position's barycentric coordinate (w1, w2, w3) blends the three tiles' samples.
- **Omits histogram preservation** so it can sample the original texture directly. Compensates with:
  - weights exponentiated `w^gamma` as in Burley, and modulated by a per-sample metric delta: `w'_i = delta(x_i) * w_i^gamma / sum_j delta(x_j) * w_j^gamma` (Eq. 1); "we are using gamma = 7 and beta = 0.6" (Section 2.2).
  - colour: `delta_C = (1 - beta) + beta * luminance(x)` with weights (0.299, 0.587, 0.114) (Eq. 4).
  - normals: `delta_N = (1 - beta) + beta * sqrt(|x|^2 / (1 + |x|^2))`, x the derivative (Eq. 3). That is the sine of the slope angle.
  - an optional contrast S-curve applied to the weights (Perlin's `Gain`, Eq. 5, parameter r in (0,1); r = 0.5 is identity; then renormalise). Section 4: "a conservative choice is in the range r in [0.65, 0.75]"; Fig. 7 uses r = 0.75 for albedo and roughness and r = 0.5 (ramp disabled) for normal maps.
- **Normals**: treats the tangent-space normal as the partial derivatives of an implicit height map, `n = normalize(-dH/du, -dH/dv, 1)` (Eq. 2). "The derivative is a linear operator, which means that by blending the partial derivatives, the obtained result is equivalent to blending directly from the height map." Converts each normal sample to a derivative, blends derivatives, converts back (`normalize(float3(-deriv.xy, 1.0))`; with `gFlipVertDeriv` the second component is negated, Listings 9/10).
- **Rotation**: random per-tile rotation is supported ("a particularly useful feature when there are recognizable elements in the sample texture with a distinct orientation", Fig. 6). For normal maps "we must rotate each sample with respect to its hex tile prior to blending". His code rotates the sampling coordinates about the tile centre, passes the rotated gradients to `SampleGrad`, then rotates each derivative back before blending.
- Cost: 3 fetches per map, in his reference HLSL. Weight and metric are computed **per map** in his listings. Roughness is "sampled using the same approach as we have described for color" (end of Section 4). Whether weights are shared across maps is not stated; sharing is our decision (2.1).
- Reference HLSL for `TriangleGrid`, `hash`, `MakeCenST`, `LoadRot2x2`, `Gain3`, derivative conversion is in the appendix (Listings 1-10); our prototype is a port of these.
- Honest quality statement from the paper: for normal maps results are "strikingly similar" to histogram-preserving (Section 3; Section 1 says "very similar results"); for colour "there is a trade-off in quality" and "the compromise is acceptable" in the authors' experience (Section 1).

### 1.5 Heitz and Neyret 2018, and Burley 2019 (histogram-preserving blending)

- Heitz and Neyret, "High-Performance By-Example Noise using a Histogram-Preserving Blending Operator", HPG 2018 / PACM CGIT 1(2). **The paper PDF could not be read**: HAL (hal.science, hal.inria.fr, hal.archives-ouvertes.fr) and dblp now serve a "Making sure you're not a bot!" proof-of-work page to scripted clients (I did not try to bypass it) and the ACM page returns a Cloudflare 403. What I did read, first-party:
  - the HAL record metadata through the HAL search API (https://api.archives-ouvertes.fr/search/?q=halId_s:hal-01824773&fl=abstract_s), which returns the full abstract. [V-src] It states: the algorithm "takes as input a small example of a stochastic texture"; "works on any kind of random-phase inputs as well as on many non-random-phase inputs that are stochastic and non-periodic, typically natural textures such as moss, granite, sand, bark"; "more than 20 times faster"; triangle grid, 3 patches blended per triangle; histogram-preserving blending "boils down to mean and variance preservation"; non-Gaussian inputs are "Gaussianized" with a histogram transformation, "de-Gaussianized" with the inverse after blending; the transformations are precomputed and stored so the algorithm runs in a fragment shader.
  - the authors' HPG 2018 talk slides (97 MB PDF, first-party, linked from the author's page https://eheitzresearch.wordpress.com/722-2/; file https://www.highperformancegraphics.org/wp-content/uploads/2018/Papers-Session3/HPG2018_ByExampleNoise.pdf). [V-src] Failure-case slide: "Our approach is meant for stochastic textures. If the input has strong spatial correlations (like repetition or strong patterns) our approach fails ... because our histogram-preserving blending operator is based on the assumption that the blending takes independent inputs." They add that strong patterns "remain an open problem for by-example procedural texturing".
  - The claim "stochastic textures only; structured textures ghost" is therefore confirmed by the authors (abstract and slides). The paper's own section 1.5 text was still **not read**; the description of T and T^-1 textures comes from the abstract plus Mikkelsen's Section 1 summary.
- Burley, JCGT 8(4), 2019: https://jcgt.org/published/0008/04/02/paper-lowres.pdf (text re-read this pass). [V-src]
  - Replaces the minutes-long optimal-transport precompute (Burley: Heitz and Neyret's generation "took several minutes for a 256x256 texture") with per-channel 1D LUTs built "trivially from the input histogram on texture load".
  - Truncated Gaussian + soft-clipping contrast operator to avoid clipping artefacts.
  - "textures with strong structure can exhibit ghosting artifacts"; linear blending "suffers ghosting and contrast loss"; exponentiated weights `w_i^gamma / sum w_j^gamma` (Eq. 5, plotted in his Fig. 9) reduce ghosting even with histogram preservation.
  - Per-channel preservation can cause colour artefacts; optionally blend in YCbCr and preserve only luminance.
- Verdict: both need LUT/transformed textures sampled in the shader. A Blender node graph cannot create those on load; we would have to bake extra images per map into each asset. Out of scope for a first version. The exponentiated-weight and contrast pieces from Burley/Mikkelsen are plain maths and are used in the recommended design.

### 1.6 Blender's own options

- Image Texture manual (5.3 "latest"): https://docs.blender.org/manual/en/latest/render/shader_nodes/textures/image.html . Interpolation: Linear, Cubic ("Bump maps should use this for best results"), Closest, Smart (Smart is "Cycles Only ... Only for Open Shading Language"). Projection: Flat, Box, Sphere, Tube. Box **Blend** is a blend between projection sides, "rather than projecting onto just one side (which creates sharp transitions)"; it is not repetition removal. Extension: Repeat, Extend, Clip, Mirror. Nothing named anti-tiling, stochastic or tile randomisation. [V-src] (re-read 2026-11-18)
- Live 5.2.1: `ShaderNodeTexImage` writable properties are image, projection, interpolation, projection_blend, extension (plus generic node props); inputs Vector, outputs Color, Alpha. No anti-tiling property. [V-bpy]
- Mirror extension doubles the period only. Same as Quilez's remark about mirrored repeat. [V-src]
- OSL-based approaches are out: Script node is "Cycles Only" (see earlier note).

### 1.7 Other families named in the brief

- Texture bombing / Voronoi-cell randomisation = Quilez Technique 2 (above). Cheaper variants (nearest feature only) have hard cell borders. [derived]
- "Stochastic sampling" in the broad sense (one random sample per pixel, noise in time) was not researched from a primary source. It also would not suit EEVEE's single-sample pipeline. [derived] (listed in 7.3)

## 2. Multi-channel PBR correctness

### 2.1 One transform for all maps

Principle: every map must sample the same hex tile, with the same offset and the same rotation, and be blended with the same weights. Otherwise albedo features (a pebble) would not line up with their roughness, normal and height. [derived; Mikkelsen's listings use identical offsets/rotations per vertex for any map, but compute delta per map]

Design decision: compute the three UVs, three angles and **one weight set** once, from albedo luminance (or from a normal-slope metric if there is no albedo), and reuse the same weights for every role. Mikkelsen computes weights per map; sharing is our deviation and keeps masks identical across maps. [derived]

### 2.2 Normals

- Convert to derivative form, rotate, blend, convert back (Mikkelsen, 1.4). [V-src]
- Blender Normal Map node: "Blender uses the OpenGL convention by default for rendering and baking, where the Y axis in the green channel points up. The DirectX convention can be used for normal maps from other software, that assumes the Y axis points down"; the image should be Non-Color. https://docs.blender.org/manual/en/latest/render/shader_nodes/displacement/normal_map.html [V-src] (re-read 2026-11-18)
- Convention interplay [derived]: the derivative conversion `d = -xy/z` and the back-conversion negate consistently, so the OpenGL sign survives. Rotation direction does flip if the green channel is inverted (a mirrored basis reverses rotation sense), so a DirectX-convention map would need flipping to OpenGL before blending or its angle negated.
- **Megascans convention, resolved**: first-party Quixel/Epic docs do not state it (Mixer docs only say "Mixer uses tangent-space normals" with an "Invert" toggle on the normal export channel, https://docs.quixel.com/mixer/1/en/topic/advanced-texture-setup-for-exports.html; Epic's Fab-upload page says Fab-in-Launcher assumes DirectX unless suffixed, https://dev.epicgames.com/documentation/en-us/fab/setting-up-assets-for-fab-in-launcher , which is about uploads, not Megascans downloads). So I measured it on the local Megascans library (7 downloaded `surface` assets, 1024x1024 PNG): correlation of (G - 0.5) with dH/dv of the blurred Displacement map (v up, Blender pixel order; for OpenGL it must be negative, same sign as R vs dH/du): [V-bpy]

  | Asset | corr(R, dH/du) | corr(G, dH/dv) |
  |---|---|---|
  | venoehv | -0.975 | -0.978 |
  | vepxaa1cc | -0.980 | -0.981 |
  | vktfeilaw | -0.963 | -0.968 |
  | xicmef1r | -0.981 | -0.980 |
  | xikxcgl | -0.967 | -0.970 |
  | xiqebhm | -0.965 | -0.967 |
  | xiqgafr | -0.965 | -0.970 |

  All seven are **OpenGL (Y up)**, i.e. the Blender default; no flip needed (assumes white = high in Displacement; consistent R and G signs support it). Limit: 7 assets of one library, whatever Bridge exported; not a statement about all Megascans.
- Mirroring (Quilez T1 style) [derived]: mirror in u negates the x derivative component. Quilez's article does not discuss normals. Our design avoids mirroring.
- Why not lerp or whiteout/RNM: blending raw normal colours (lerp) and renormalising shrinks slopes in blend regions; whiteout/RNM are for overlaying two different detail layers, not for blending equivalent samples [derived; no primary source read on whiteout/RNM, only Mikkelsen's derivative blending]. Derivative blending is mathematically equal to blending heights (Mikkelsen) and is what we use.

**Measured [V-bpy]** (pass 1, Cycles, 5 test planes): hex-tiled with per-tile rotation, normals through the prototype blend vs a reference where the **height** map is blended with the same weights and fed to a Bump node. Image RMS difference 0.0101, correlation 0.977. Same setup with the normal rotation deliberately disconnected: RMS 0.0427, correlation 0.799. Unrotated plain tiling vs reference: RMS 0.0857. So the rotate-then-blend path is needed and works. Limits: single synthetic texture, 8-bit PNG normals, Bump approximation, not a Megascans asset.

### 2.3 Roughness, metallic, AO, opacity, translucency

Scalar/colour linear maps: same weighted sum as albedo. Mikkelsen treats roughness like colour with r = 0.75. [V-src]

**Sharp masks (metallic, opacity), measured [V-bpy]** (headless; the mask is a synthetic 1024 px 0/1 map with ~3 px edges, blended with weights computed from an independent brick albedo, i.e. the worst case where the mask is uncorrelated with the weight source; 2 tiles across 1024 px, Cell Scale 0.5, Rotation 0, Cycles 16 spp; "mid" = fraction of pixels with 0.1 < v < 0.9):

| Blend | mean | std | mid | mid vs plain |
|---|---|---|---|---|
| Plain tiling | 0.4321 | 0.4863 | 0.041 | 1.0x |
| Linear (exp 1, no delta, r 0.5) | 0.4308 | 0.3444 | 0.607 | 14.8x |
| Exp 7, r 0.5 (delta 0 or 0.6) | 0.4289 | 0.4562 | 0.166 | 4.0x |
| Exp 7, delta 0.6, r 0.75 (colour default) | 0.4288 | 0.4692 | 0.112 | 2.7x |
| Exp 7, delta 0.6, r 0.9 | 0.4288 | 0.4749 | 0.088 | 2.1x |
| Exp 12, delta 0.6, r 0.95 | 0.4288 | 0.4801 | 0.066 | 1.6x |
| r 0.75 output + smoothstep(0.3, 0.7) snap | 0.4288 | 0.4895 | 0.025 | 0.6x |

Reading: the mean barely moves (-0.003), but the blend zones turn a binary mask into mid-grey bands; even the paper's colour default leaves 2.7x more intermediate pixels than plain tiling. The luminance delta has negligible effect on a mask (max pixel difference 0.028 between delta 0 and 0.6, mean 0.0008). For near-binary masks use a higher Contrast (0.9, optionally plus a post-blend smoothstep snap); for graded masks and roughness keep 0.75. Limit: one synthetic mask; real metallic masks correlated with the albedo may behave better or worse.

### 2.4 Height / displacement

- Blend heights with the same weights P_i; rotation does not change height values, only where they are sampled. [derived]
- Because normals blend as derivatives and height blends as value, the true-displacement geometry and the normal map are equal only to first order; in the test above, correlation was 0.977. [V-bpy] for the bump comparison; true displacement geometry not tested.
- Displacement node inputs Height, Midlevel (default 0.5), Scale, Normal; https://docs.blender.org/manual/en/latest/render/shader_nodes/displacement/displacement.html [V-src]. The existing `Displacement Scale` socket and `Midlevel` default stay untouched: anti-tiling only changes the Vector going into the height Image Texture and adds the blend before the Displacement node.
- Cost: 3 more height fetches per displaced vertex/sample. Heights blended linearly lose amplitude in blend zones (variance drop). Weight gain (Gain/r and exponent) shrinks those zones.

### 2.5 Contrast loss and the fixes

**Measured [V-bpy]** (pass 1, std of luminance of the rendered synthetic cosine pattern, plain tiling = 0.0444; hex cell scale 1.0, no rotation):

| Blend | Std | vs plain |
|---|---|---|
| Linear barycentric (exponent 1, no delta, no gain) | 0.0321 | -28% |
| Exponent 7 | 0.0418 | -6% |
| Exponent 7 + luminance delta (beta 0.6) + gain r = 0.75 | 0.0429 | -3.4% |

**Re-measured on real Megascans albedo [V-bpy]** (headless; 7 local `surface` albedos, 1024 px, 4 tiles across 1024 px, Cell Scale 1.0, Cycles 8 spp; luminance std change vs plain tiling):

| Blend | Change vs plain (range over 7 assets) |
|---|---|
| Exp 7 + delta 0.6 + r 0.75, Rotation 0 | -2.2% .. -5.2% (mean -3.4%) |
| Same, Rotation 1 | -2.4% .. -4.7% |
| Linear (exp 1, no delta, r 0.5) | -26.0% .. -30.8% |

The real-asset numbers match the synthetic ones, supporting the paper's exponent and gain (Mikkelsen, Burley). [V-src for the technique, V-bpy for the numbers]

Further options if more is needed: Burley's YCbCr/luminance-only histogram preservation (needs LUTs, out of scope), or a post-blend variance restore (not tested; listed in 7.3).

## 3. Implementability in shader nodes (EEVEE + Cycles, no OSL)

### 3.1 What was built [V-bpy]

All in `ShaderNodeTree` node groups built from Python on Blender 5.2.1 LTS, throwaway names prefixed `_AT_` (pass 1) and `_AT2_` (this pass, same recipe re-created), none kept:

| Group | Own nodes | Role |
|---|---|---|
| HexCoords | 35 (+3 HexVertex instances) | skew, floor/fract, barycentric weights W1..3, vertex ids; outputs UV1..3, Angle1..3, W1..3 |
| HexVertex | 19 | per vertex: tile centre, White Noise 4D hash (offset xy, angle from Value), rotate about tile centre |
| HexWeights | 34 (+3 Gain) | `delta` modulation, `w^7`, normalise, contrast Gain, renormalise |
| Gain | 14 | Perlin/Schlick S-curve |
| HexBlendColor | 7 | `sum(P_i * C_i)` for any colour/scalar map |
| HexBlendNormal | 43 | normal to derivative, rotate +angle, blend, derivative to normal |

- Hash: **White Noise Texture 4D** (Vector = vertex id, W = seed) replaces Mikkelsen's `frac(sin(...))`. Value output gives the angle, Color gives the offset. Docs: https://docs.blender.org/manual/en/latest/render/shader_nodes/textures/white_noise.html. [V-src for the node; V-bpy that it builds]
- Rotation uses the **Vector Rotate** node (Axis Angle, Z), angle -theta on coordinates, +theta on derivatives, matching Mikkelsen's `mul(v, rot)` / `mul(rot, d)` convention. Correct orientation confirmed indirectly by the 0.977 correlation test above.
- No Hash Value node (not available in shader trees, see earlier note).
- A material with albedo only: 13 top-level nodes (3 Image Texture, 3 RGB to BW, weights, coords, blend, + Tex Coord, Mapping, BSDF, Output). With albedo + normal: 18 top-level nodes, 6 Image Textures. Fully expanded node count for the albedo+normal material was 236 vs 7 for plain tiling (counts every nested instance; group definitions are shared).

### 3.2 Image samples per map

| Method | Samples per map | Notes |
|---|---|---|
| Plain Repeat | 1 | baseline |
| Quilez T3 | 2 | noise lookup for index is an extra Noise node, no image sample |
| Quilez T1 | 4 | |
| Mikkelsen hex | 3 | shared weights need the albedo (or one weight map) sampled first |
| Quilez T2 | 9 | |
| Heitz/Burley | 3 + LUT lookups | LUT textures not possible in-graph |

At 1k/2k/4k: the sample **count** does not depend on resolution; no extra texture memory (the same images are referenced). Cache and bandwidth behaviour at 4k with 3 offset samples per map was not measured (7.3). A full Megascans surface (albedo, AO, roughness, normal, displacement, possibly metalness) is 5-6 maps, i.e. 15-18 samples instead of 5-6.

### 3.3 Cost measured (crude) [V-bpy]

Cycles CPU, 1024x1024, 64 spp, one plane, albedo only, wall time including scene setup and shader compile: plain 4.0 s, hex 9.9 s (about 2.5x). Re-check this pass (emission-only material, 8 spp, 512x512): plain 0.23 s vs hex 0.58 s in Cycles (about 2.5x again); EEVEE 512x512 0.5-1.7 s (dominated by engine start). Not a clean per-sample benchmark; no GPU Cycles numbers.

### 3.4 Visual verification [V-bpy]

Throwaway scene, plane(s) with a synthetic tileable texture (stochastic cosine pattern plus a red "comet" marker), UV scale 6:

- Plain tiling: marker repeats in an obvious 6x6 grid. Hex-tiling: grid gone; with rotation enabled the stripe direction also varies per tile. Cell Scale 1.0 chops the 0.07-radius marker into fragments (cell spacing about 0.29 texture units); Cell Scale 2.5 keeps more markers intact. Cell Scale is therefore an artistic parameter tied to feature size.
- Cycles vs EEVEE on the same material: same layout (hash and weights agree), image RMS 0.019 for the hex panel vs 0.009 for plain tiling. The extra difference in the hex panel is plausibly the EEVEE derivative seams measured in 4.2 [derived]. This pass also confirmed the hash agrees across engines for per-object seeds (3.6).
- Not verified: Metal/Vulkan GPU Cycles; Blender 5.3/5.4; animation; true displacement.

### 3.5 Real Megascans albedo, visual check [V-bpy]

Headless, 4 of the 7 local `surface` albedos, plain vs hex (Rotation 0, Cell Scale 1.0, 4 tiles across):

- Two stochastic ground surfaces (grass, gravel-like): the 4x4 grid is gone, but because their albedo carries a low-frequency brightness gradient, hex-tiling at Cell Scale 1.0 shows a **patchwork of lighter/darker blobs** (the gradient is cut into hex pieces). Plain tiling showed the same gradient as an obvious grid. So anti-tiling trades a grid for blobs on non-stationary albedo; this is what the colour-variation note's low-frequency approach or delighting would address, not the blend. [derived reading of the images; no metric]
- One `surface`-typed asset (`vktfeilaw`) is a sphere on a dark background, i.e. **not a tileable texture**; hex-tiling cuts it into fragments (plain tiling already gives a grid of spheres). The `surface` type does not guarantee a seamless texture; anti-tiling cannot help such assets. Decide per asset or accept the flag as opt-in (see D2). Limit: only 7 local assets inspected.
- A reddish cracked-soil surface looked good.

### 3.6 Per-object seeds: Object Info > Random [V-bpy]

Headless, three planes (separate objects) sharing ONE material, ortho top-down, 16 spp. Object Info > Random output per plane: Cycles 0.0367 / 0.2303 / 0.3283, EEVEE 0.0367 / 0.2302 / 0.3281 (agree to EEVEE's half-float rounding). Material = hex-tiled albedo with seed = seed + Random * 1.0:

| Engine | Per Object | Image RMS between duplicates (p0-p1 / p0-p2 / p1-p2) | Cycles-vs-EEVEE RMS per plane |
|---|---|---|---|
| Cycles | off | 0.0085 / 0.0085 / 0.0084 (sampling noise at 16 spp) | 0.0135 each |
| Cycles | on | 0.0523 / 0.0536 / 0.0528 | 0.0138 / 0.0132 / 0.0136 |
| EEVEE | off | 0.0000 / 0.0000 / 0.0000 | (same) |
| EEVEE | on | 0.0453 / 0.0463 / 0.0455 | (same) |

So Per Object makes duplicates differ in both engines, and the two engines agree on the per-object layout as well as they agree without it. Manual: Random is "unique to a single object instance. Output is a Float between 0.0 and 1.0"; the node "only works for material shading nodes" (https://docs.blender.org/manual/en/latest/render/shader_nodes/input/object_info.html) [V-src]. The EEVEE supported-nodes page lists Curves Info > Random as using a different RNG but does not list Object Info as unsupported ("If something is not listed here, it is supported") [V-src]. Not tested: collection/Geometry Nodes instances, linked duplicates, renamed objects.

## 4. Mipmapping and filtering at tile seams

### 4.1 Sources

- Quilez and Mikkelsen both require `textureGrad`/`SampleGrad` with gradients from the **original** coordinates, because the per-tile offset (and rotation) makes derivatives discontinuous (Quilez: "mipmapping will break apart, creating line artifacts"; Mikkelsen passes rotated gradients). [V-src]
- The Blender Image Texture node has **no gradient input** (inputs: Vector only). [V-bpy]
- EEVEE: Image Texture (Linear) computes `dx = gpu_dfdx(co.xy) * texture_lod_bias_get()`, `dy = gpu_dfdy(co.xy) * ...` and calls `textureGrad(ima, co.xy, dx, dy)`, i.e. the gradient comes from the incoming coordinate as shaded, so any per-tile offset shows up in the derivative. Source read at tag v5.2.1: https://raw.githubusercontent.com/blender/blender/v5.2.1/source/blender/gpu/shaders/material/gpu_shader_material_tex_image.glsl (lines 59-70; the Cubic variant, lines 75-79, calls `texture_bicubic` instead). The colour-variation pass independently reported the same for 5.2.1/5.3.0/main. [V-src]
- EEVEE docs, current 5.3 manual, Supported Nodes (the page moved to `render/eevee/limitations/nodes_support.html`; the 4.0 URL is gone): https://docs.blender.org/manual/en/latest/render/eevee/limitations/nodes_support.html . Image Texture: "Smart Interpolation always uses Cubic interpolation. Artifact present using Tube or Sphere projection with linear interpolation. This is due to hardware mip-mapping and Anisotropic filtering. This kind of artifact will be also visible if the texture coordinates provided are not continuous." [V-src] That last sentence is exactly the hex-tiling case.
- EEVEE 5.2 release notes (https://developer.blender.org/docs/release_notes/5.2/eevee/): "Scene based anisotropic filtering setting", "Mipmap generation is now consistent between backends"; also "EEVEE force use of GLSL precise derivatives for more deterministic bump mapping across platform". [V-src] The setting is exposed as `scene.render.anisotropic_filter` (enum FILTER_0, 2, 4, 8, 16) in 5.2.1 [V-bpy] and documented under Render > Performance > Anisotropic Filtering (Off/2x/4x/8x/16x), https://docs.blender.org/manual/en/latest/render/eevee/render_settings/performance.html [V-src]. No EEVEE mipmap toggle exists in the 5.3 manual's EEVEE render-settings pages (searched: only the anisotropic and film filter-size settings mention filtering).
- Cycles: task "Cycles: texture cache and mipmaps" https://projects.blender.org/blender/blender/issues/68917 (read via the Gitea API, https://projects.blender.org/api/v1/repos/blender/blender/issues/68917, since the HTML page returns 403 to scripts): **open**, last updated 2026-10-26. Description: tiles loaded at needed mip levels for bounded memory; status text says an implementation exists ("To test, go to the Performance panel and enable 'Texture Cache'"); checklist items done include "Implement mipmap texture filtering", ray differentials in SVM for Image texture, Mapping, UV Map, Texture Coordinate, Vector Math, Bump; **not done**: "Math, Mix, .. (?)", "Better filtering for bump and normal maps", volume/light differentials. [V-src] The feature shipped as opt-in **Texture Cache** in Blender 5.2 LTS (Performance > Texture Cache + Auto Generate; per image a `.tx` file in a `blender_tx/` folder; https://developer.blender.org/docs/release_notes/5.2/cycles/ ); the 5.3 Cycles notes list no texture-filtering change. [V-src] Properties in 5.2.1: `scene.render.use_texture_cache`, `use_auto_generate_texture_cache`. [V-bpy] The colour-variation pass reports Cycles passes dual2 differentials into the Image Texture node (source reading by that agent, not repeated here).

### 4.2 Measured: EEVEE derivative seams (R1) [V-bpy]

Method. Synthetic brick albedo (1024 px, 8 courses, grain noise, high contrast; tileable), Emission material (no lighting), Cell Scale 1.0, Rotation 0, exponent 7, delta 0.6, r 0.75. Reference = the same material rendered in Cycles CPU at 256 spp (pixel-integrated "truth"). Error = RMSE of display-encoded luminance of the test render vs the reference, split by an **edge zone** = pixels with min barycentric weight < 0.05 (taken from a Cycles debug render of the same hex material). Plain tiling gets the same split, as the baseline for engine differences. EEVEE 64 TAA samples; Cycles 16 spp.

(a) Perspective plane at grazing angle (10x60 m plane, camera 1.2 m high, 50 mm, 2.5 degrees below the horizon, 800x450, UV tile 2 m, texel about 2 mm). Distance bands from the camera; hex/plain RMSE:

| Band | Renderer | zone hex / plain | inner hex / plain | all hex / plain |
|---|---|---|---|---|
| 0-8 m | EEVEE aniso off | 0.0205 / 0.0082 | 0.0088 / 0.0086 | 0.0132 / 0.0085 |
| 0-8 m | EEVEE aniso 16x | 0.0181 / 0.0044 | 0.0052 / 0.0044 | 0.0106 / 0.0044 |
| 0-8 m | Cycles, no cache | 0.0072 / 0.0070 | 0.0070 / 0.0073 | 0.0070 / 0.0072 |
| 0-8 m | Cycles, texture cache | 0.0072 / 0.0045 | 0.0070 / 0.0046 | 0.0070 / 0.0046 |
| 8-20 m | EEVEE aniso off | 0.0181 / 0.0106 | 0.0151 / 0.0128 | 0.0158 / 0.0123 |
| 8-20 m | EEVEE aniso 16x | 0.0148 / 0.0037 | 0.0105 / 0.0042 | 0.0116 / 0.0041 |
| 8-20 m | Cycles, no cache | 0.0084 / 0.0088 | 0.0092 / 0.0097 | 0.0091 / 0.0095 |
| 8-20 m | Cycles, texture cache | 0.0084 / 0.0052 | 0.0092 / 0.0054 | 0.0091 / 0.0053 |
| 20-60 m (zone meaningless, sub-pixel) | EEVEE aniso off / 16x | - | - | 0.0076 / 0.0075 ; 0.0062 / 0.0032 |
| 20-60 m | Cycles no cache / cache | - | - | 0.0084 / 0.0088 ; 0.0084 / 0.0052 |

(b) Flat orthographic view, 4 tiles across, minification = 4096 / resolution (texels per pixel), EEVEE aniso 16x, Cell Scale 1.0, hex/plain:

| Minification | edge zone (<0.05) | <= 2 px from a triangle edge | 2-6 px from edge | inner (>= 0.05) |
|---|---|---|---|---|
| 2:1 | 0.0105 / 0.0034 | 0.0147 / 0.0040 | 0.0078 / 0.0031 | 0.0067 / 0.0033 |
| 4:1 | 0.0116 / 0.0031 | 0.0131 / 0.0035 | 0.0067 / 0.0026 | 0.0057 / 0.0025 |
| 8:1 | 0.0135 / 0.0039 | 0.0123 / 0.0038 | 0.0062 / 0.0026 | 0.0050 / 0.0026 |
| 16:1 | 0.0159 / 0.0068 | 0.0122 / 0.0057 | 0.0071 / 0.0044 | 0.0060 / 0.0043 |

(1:1 was attempted and exceeded the MCP call time limit; not measured. Aniso off vs 16x at 2/4/8/16:1: zone 0.0109/0.0122/0.0145/0.0174 vs 0.0105/0.0116/0.0135/0.0159, i.e. no meaningful change.)

(c) Parameter sweep at 8:1 (zone hex/plain; baseline 0.0135/0.0039): Rotation 1: 0.0138/0.0039. Exponent 12 + r 0.9: 0.0136/0.0039. Exponent 3 + r 0.5: 0.0128/0.0039. Cell Scale 2.5: 0.0099/0.0036 (and the share of pixels within 2 px of a triangle edge drops from 0.31 to 0.14; the 2 px band error is unchanged, 0.0122 vs 0.0123, so there are fewer seam pixels, not a fainter seam). Interpolation Cubic: 0.0056/0.0083 (no excess over plain; but EEVEE's Cubic path differs from Cycles' Cubic on plain textures, 0.0083 vs 0.0039 for Linear, so the reference itself is shakier; treat as "no seam excess observed", not a clean fix).

Reading:
- EEVEE hex-tiling adds a thin line pattern along the triangle edges: in the edge zone the error is 2.5-4x the plain-tiling error in the same pixels; it grows with minification. The amplified difference image (x10) shows a clean lattice of 1-2 px lines at the hex triangle edges. In the beauty image at normal viewing the lines are very faint (some triangle outlines are barely visible in a 2x upscale; this is my reading of one image, not a perceptual test). **R1 is real but low amplitude** (about 0.012-0.015 display-luminance RMSE in the edge band on a high-contrast brick).
- It matches the manual's statement for discontinuous coordinates (4.1) and the mechanism (`dFdx/dFdy` of the shifted coordinate, 4.1). Exponent, contrast, rotation and anisotropic setting do not remove it; **larger Cell Scale reduces the number of seam pixels**, which supports the 2.5 default.
- Inner pixels are also about 2x the plain error (0.0050 vs 0.0026 at 8:1): each hex sample is individually shifted, so some of this is EEVEE-vs-Cycles filter difference accumulated over three samples, not seams.
- **Cycles has no seams**: its hex error equals its plain error in the zone (0.0072 vs 0.0070).
- **Cycles Texture Cache** (5.2.1, `.tx` generated automatically): plain-texture renders change (max pixel difference 0.052 vs cache off; error vs reference drops from 0.0072 to 0.0046 near, i.e. mip filtering works) while the hex-tiled render is **bit-for-bit the same as with the cache off** (max difference 1e-5, sampling noise). So the hex lookup is not mip-filtered in Cycles. Consistent with the tracker checklist (Math/Mix differential propagation unchecked [V-src]); the mechanism is [derived]. Consequence: in Cycles, hex-tiled textures behave like 5.1 and earlier (no mips; the pixel-sample averaging does the filtering) and gain nothing from the Texture Cache's mip levels, only from its memory savings.
- Mitigations in node graphs: none removes the EEVEE lines fully; larger Cell Scale reduces them; offset-only (Rotation 0) is no better or worse for seams but is needed for grain (3.5/D3); keep Extension at Repeat; do not use Closest; Cubic interpolation showed no seam excess but is unconfirmed.
- Interpolation: Linear is the default and plays well with the blend; Cubic is documented for bump maps and costs more taps.

## 5. Integration with the repo

### 5.1 Where it goes

`bl/build.py::build_group_material`: today one Mapping node feeds the group's `Vector` input (line 64 and 206), and the inner `tex()` helper wires `gi.outputs["Vector"]` into every Image Texture (line 122). Plan:

1. Add a `tex_hex(role, color)` path that creates three Image Textures per role, one per UV output of a shared `HexCoords` instance, plus one `HexBlendColor` per role (or `HexBlendNormal` for normal).
2. Create `HexCoords`, `HexWeights`, `HexBlendColor`, `HexBlendNormal` (and their children) **once** per `.blend` (check `bpy.data.node_groups` by name; mind that each per-asset `.blend` carries its own copy, same risk R6 as the colour-variation note). Instance them as nested groups inside the per-material group. Avoid per-material copies of the logic; a shared group is smaller but versioning must be handled (name with a version suffix).
3. Order: weights need the three albedo samples first (luminance delta), then every other role reuses P1..3. Existing AO multiply, Tint, HSV, Brightness, Contrast chain is unchanged and stays downstream of the blended albedo.
4. The Normal Map node (current line 189) now takes the blended colour from `HexBlendNormal`; `Normal Strength` unchanged. No convention flip needed for Megascans (2.2).
5. The Displacement node (line 197) takes the blended height; `Displacement Scale` unchanged. Material `displacement_method = "BOTH"` unchanged.

### 5.2 Socket interface (proposed)

New "Anti-Tiling" panel on the material group, driven by one new group `Quixlend Anti-Tiling`:

| Socket | Type | Default | Range | Notes |
|---|---|---|---|---|
| Enabled | Boolean at build time | per D2 | - | A Factor blending toward plain tiling was never tested; a build-time boolean is simpler. |
| Cell Scale | Float | 2.5 | 0.25..16 | hex cell size in texture units; 1.0 chopped features, 2.5 kept them (3.4), and 2.5 halves the number of EEVEE seam pixels vs 1.0 (4.2) |
| Rotation | Float | **0.0** | 0..1 | 0 = offset only; 1 = random angle in +/- pi. Data in 7.4: 1 destroys directional grain and brick courses. Offer as opt-in for isotropic stuff (pebbles, soil). |
| Blend Softness | Float | maps to exponent gamma 7 | 1..12 | exponent; paper default 7. Raising it did not help seams (4.2c). |
| Contrast | Float | 0.75 colour/roughness, 0.5 normal, **0.9 near-binary masks (metallic, opacity)** | 0.5..0.99 | S-curve r. 0.9 for masks from 2.3 (mid-grey share 2.1x vs 2.7x at 0.75). |
| Seed | Float | 0 | 0..1000 | White Noise W |
| Per Object | Float | 0 | 0..1 | add Object Info Random to W so duplicates differ. Verified in Cycles and EEVEE (3.6). |

Group interface: Vector in, `Vector` outputs UV1..3 plus floats; the material group keeps one `Vector` input. Expose to the user only Cell Scale, Rotation, Seed, Per Object, Contrast/Softness as an advanced foldout.

### 5.3 Coordinates, atlases, displacement

- **UV vs object coordinates**: Hex-tiling uses xy only. The repo feeds UV through a Mapping node. For surface assets (planes/tiles) UV is correct. For a 3D object coordinates would avoid UV island seams, but those are non-tileable assets anyway. [derived]
- **Atlases**: Quixlend categories are `surface`, `atlas`, etc. (`bl/build.py` line 24, `bl/ui.py` line 36). Primary sources: Atlas is a separate Megascans library category from Surfaces ("Atlases, Displacements, and Brushes are not supported in Bridge for Unreal Engine", https://dev.epicgames.com/documentation/en-us/unreal-engine/quixel-bridge-plugin-for-unreal-engine ), and Quixel's Mixer documents the Atlas layer as "real-world scanned assets from the Megascans library that serve as decals that you can mix or blend with all other layer types" (https://docs.quixel.com/mixer/1/en/topic/atlas-layer.html; Twinmotion lists "Decals" next to Surfaces). [V-src] So an atlas here is a decal-type asset, not a repeating surface. The finer detail (multiple elements packed in one sheet, so UV offsets would sample neighbouring elements) is **not stated** by those pages [unverified], but decal assets are not meant to repeat either way, so the conclusion is unchanged: **anti-tiling must be skipped for atlases and for UV-unwrapped meshes** (the `objects` branch of `build()`). [derived]
- **Displacement**: `build()` calls `build_group_material(..., displace=asset_type == "surface")`, so displacement already exists only for surfaces, which is exactly where anti-tiling applies. Real displacement uses the vertex sample points; anti-tiling adds 3 height samples there.
- **Cost at resolutions**: the repo supports 1k/2k/4k (commit "add 1k resolution support"); sample count is resolution-independent (3.2). A 4k set means three 4k fetches per role; memory unchanged.

### 5.4 Rollout relative to the colour-variation node

1. Ship anti-tiling first for the **surface** type behind a build-time flag (see D2 for the default).
2. Colour Variation stays a separate, optional, downstream node. Order in the albedo chain: anti-tiled albedo, then AO, then colour variation (broad low-frequency), then global Tint/HSV/Brightness/Contrast. The colour-variation note put its plug point right after the albedo `tex()`; the new blend output becomes that `color`.
3. Keep them independent: anti-tiling removes repetition of features; colour variation hides the remaining statistical repetition. Sharing the same Seed/Per Object control is possible. [design]
4. Add `GLOSSARY.md` terms if adopted ("Anti-Tiling", "Hex Tile", "Cell Scale"): the repo has none yet.

## 6. Ranking of approaches

1. **Hex-tiling (Mikkelsen), shared weights, optional rotation.** Best removal of repetition for the cost; handles normals correctly with measured evidence. 3 samples per map; about 150 stored nodes in shared groups; per-material top-level nodes grow by 3 Image Textures and one blend group per map. Costs: faint EEVEE seam lines (4.2), no Cycles mip filtering (4.2), 2.5x shader time on a crude CPU test.
2. **Quilez Technique 3, offset-only.** 2 samples per map, ~20 nodes. Weaker against distinct features (visible low-frequency "patchiness"; not tested here). Safe fallback for low-end/EEVEE-heavy scenes. It has the same coordinate-discontinuity exposure to derivatives (Quilez: derivatives "need to be computed for proper filtering since we are introducing discontinuities"), so do not assume it is seam-free in EEVEE. [derived]
3. **Quilez Technique 1 (4 tiles).** 4 samples, mirror requires normal sign handling, visible grid structure persists (blend zones follow the original tile grid). Not preferred.
4. Not now: Technique 2 (9 samples); Heitz-Neyret/Burley LUT blending (needs baked LUTs).

## 7. Decisions, risks, and what is still unverified

### 7.1 Recommendation (the user still decides)

- **D1 (weight source): one shared weight set from albedo luminance; for assets without albedo use the normal-slope delta (Mikkelsen Eq. 3).** Reason: it keeps every map on the same tiles and matched the sharp-mask test (2.3) without per-map weights; Mikkelsen's own per-map weights cannot guarantee aligned masks.
- **D2 (default on or opt-in): opt-in, off for the existing library; enable per asset or per category after a real-asset pass.** Reason: 2.5x shader cost, faint EEVEE seams, no Cycles mip benefit, blob patchwork on non-stationary albedo and at least one local `surface` that is not tileable (3.5); real-asset testing so far is 7 local albedos, colour only.
- **D3 (Rotation default): 0 (offset-only), exposed 0..1.** Reason: Rotation 1 drops the share of gradient energy along the grain from 0.83 to 0.10 on wood and makes brick courses isotropic (0.31 vs 0.33 chance level); Rotation 0 keeps 0.75-0.82 (table in 7.4).
- **D4 (where the maths lives): shared nested groups, built once per `.blend`, name with a version suffix (e.g. `QX_AntiTiling_v1`), not flattened per material.** Reason: the 236-node expanded count is mostly Gain/Vertex instances; a shared definition keeps materials at 13-18 top-level nodes and makes a v2 fix a one-name change (accepting the per-asset-.blend copy risk R6).

### 7.2 Risks

- R1: Derivative-seam lines in EEVEE at triangle edges. **Reproduced and measured** (4.2): 2.5-4x plain error in the edge zone, faint in the beauty. Not mitigated by exponent/rotation/aniso; fewer with larger Cell Scale.
- R2: Real-asset quality. Real Megascans albedo contrast loss measured (-2..-5%, 2.5), visual check on 4 assets (3.5): stochastic ground works but shows blob patchwork on gradients; structured textures (brick, 3.5 directional test) show ghosting/blurry overlaps with Rotation 0 and broken courses with Rotation 1, as both Burley and the Heitz-Neyret talk caution. Normal and roughness on real assets not tested.
- R3: Roughness/metallic masks with sharp transitions. **Measured** (2.3): 2.7x more intermediate pixels at the default Contrast; 2.1x at 0.9; snap needed for truly binary masks. Roughness itself not measured.
- R4: Hash determinism between EEVEE and Cycles: same layout, same per-object seeds (3.6, 4.2); only CPU Cycles and one GPU-backed EEVEE tested.
- R5: DirectX-convention normals: not an issue for the 7 local Megascans surfaces (all OpenGL, 2.2).
- R6: True displacement geometry vs normal consistency untested.
- R7: Per-object randomness: **verified** in both engines for separate objects sharing a material (3.6); instances not tested.
- R8: Two session notes. (i) Pass 1: the image count in the live session dropped by 12 (58 to 46) after cleanup; my code removed only `_AT_` names; the missing 12 were the `.quixlend_*` previews, probably removed by the add-on or app independently (not verified). In this pass I did not touch any `.quixlend_*` or other non-`_AT2_` datablock. (ii) During this pass the live Blender 5.2.1 stopped answering the MCP mid-call (a mask experiment that ran fine headless afterwards: 7 small Cycles renders then scene cleanup) and was no longer running when I looked; no new crash report appeared in `~/Library/Logs/DiagnosticReports`. I do not know whether my script, the other agent or the user ended it; if its session had unsaved work, it is gone with the process. I did not restart the GUI session and ran the remaining experiments headless in a separate process.
- R9 (new): Cycles hex lookups get no mip filtering even with the 5.2 Texture Cache (4.2); at heavy minification they rely on supersampling noise. Revisit when `#68917` closes (open as of 2026-10-26).

### 7.3 Still unverified

- Heitz and Neyret paper full text (HAL/dblp bot challenge, ACM 403): only the abstract (HAL API) and the authors' talk slides were read; section-level claims in the paper are not checked.
- Whether Megascans normals are OpenGL for all assets (7 local ones measured; no first-party statement exists in the Quixel/Epic pages I found).
- The finer meaning of "atlas" (multi-element sheet) and whether the library's `atlas` assets are packed sheets; first-party pages only say decal.
- Cycles dual2 differential propagation (source) was reported by the colour-variation agent, not re-read; why Cycles' hex path is unfiltered is inferred from the tracker checklist and a measurement, not read in the Cycles source.
- EEVEE seam visibility in practice: measured, but only on a synthetic brick, one GPU/Metal backend, one view per setting; no perceptual test; 1:1 minification not measured.
- True displacement geometry; normal and roughness blends on real Megascans assets; GPU Cycles (Metal/OptiX); Blender 5.3/5.4; animation.
- Cache/bandwidth behaviour at 4k with 3 offset samples per map; a real GPU timing (only crude CPU Cycles).
- "Stochastic sampling" family; post-blend variance restore; Quilez T3 as built in nodes (never built or measured here); whiteout/RNM comparison; Cubic interpolation as an EEVEE-seam mitigation.
- Object Info > Random with collection/Geometry Nodes instances.

### 7.4 Directional materials (D3 data) [V-bpy]

Live Blender, Cycles CPU 16 spp, flat ortho, 2 tiles across 1024 px (2:1), synthetic 1024 px textures. **Wood** = horizontal grain (rings + long fibres), **brick** = 8 courses of 4 bricks with grain. Gradient-energy statistics of display luminance: "near grain" = share of gradient energy within +/-15 degrees of the grain-normal direction (chance level 0.167); "windows aligned" = share of 32 px windows whose dominant orientation is within 15 degrees of the grain; "H/V" = share of energy within +/-15 degrees of horizontal or vertical (chance 0.333).

| Material | Setting | near grain / H/V | windows aligned | global coherence R |
|---|---|---|---|---|
| Wood | plain | 0.831 | 99.6% | 0.902 |
| Wood | hex Cell 0.5, Rot 0 | 0.745 | 99.3% | 0.821 |
| Wood | hex Cell 1.0, Rot 0 | 0.815 | 99.8% | 0.887 |
| Wood | hex Cell 0.5, Rot 1 | 0.098 | 8.6% | 0.070 |
| Wood | hex Cell 1.0, Rot 1 | 0.110 | 12.2% | 0.053 |
| Brick | plain | H/V 0.471 | - | - |
| Brick | hex Cell 0.5 / 1.0, Rot 0 | H/V 0.424 / 0.421 | - | - |
| Brick | hex Cell 0.5 / 1.0, Rot 1 | H/V 0.311 / 0.313 | - | - |

Visual check agrees: Rotation 0 keeps grain and courses but shows blurry overlapping bricks and offset mortar lines in the blend zones; Rotation 1 scatters brick courses and wood grain into random directions. Limit: synthetic textures; a quantised rotation (multiples of 90/180 degrees) was not tested [derived suggestion].

## 8. Reproducing the experiments

All scripts lived in the session scratchpads (not in the repo, not durable): pass 1 `qx_hex.py`, `qx_test.py`; this pass `at2.py` (textures, materials, scenes), `qx_hex2.py` (node groups, `_AT2_` prefix), `ex.py` (flat/EEVEE seam sweeps), `ana.py` (metrics), `hl_*.py` (headless runners for masks, Object Info, real assets, Megascans normals). The node recipe is the one in 3.1 and the maths is Mikkelsen's Listings 1-6, 8-10, with these deviations: White Noise 4D for the hash (and a random angle from its Value output), a shared weight set, normals clamped with `max(|z|, 1/128)` instead of the paper's `max(|z|, max(|x|,|y|)/128)`, `Gain` applied unconditionally (identity at r = 0.5). Reference for all seam numbers: Cycles CPU, 256 spp (perspective) or 64-256 spp (ortho), the same material; metric = RMSE of display-encoded (sRGB) luminance. Texture Cache runs wrote `.tx` files under the scratchpad's `blender_tx/`, nothing in the repo.

## 9. Source index

Re-fetched 2026-11-18 (HTTP 200 unless noted):

- https://iquilezles.org/articles/texturerepetition/ and https://www.iquilezles.org/articles/texturerepetition/ (read; `exp(-5.0*d)`, 9 fetches, smoothstep(0.25, 0.75), T3 contrast formula, "offsets suffice most of the time" confirmed)
- https://jcgt.org/published/0011/03/05/paper-lowres.pdf (Mikkelsen 2022, full text; Eq. 1-5, gamma 7, beta 0.6, r 0.75/0.5, "conservative r in [0.65, 0.75]" confirmed; the host timed out on later HEAD checks, the GET at 12:55 succeeded)
- https://jcgt.org/published/0008/04/02/paper-lowres.pdf (Burley 2019, text)
- https://api.archives-ouvertes.fr/search/?q=halId_s:hal-01824773&fl=abstract_s,files_s (HAL record metadata and abstract of Heitz and Neyret 2018; the file URLs it lists, https://inria.hal.science/hal-01824773/document, return a bot-challenge page)
- https://eheitzresearch.wordpress.com/722-2/ (author page: paper, supplemental, slides, video links) and https://www.highperformancegraphics.org/wp-content/uploads/2018/Papers-Session3/HPG2018_ByExampleNoise.pdf (talk slides, read)
- https://hal.archives-ouvertes.fr/LJK/hal-01824773v1 and https://dblp.uni-trier.de/rec/journals/pacmcgit/HeitzN18.html (reachable but served a "Making sure you're not a bot!" page to scripts; not usable as sources)
- https://docs.blender.org/manual/en/latest/render/shader_nodes/textures/image.html
- https://docs.blender.org/manual/en/latest/render/shader_nodes/textures/white_noise.html
- https://docs.blender.org/manual/en/latest/render/shader_nodes/displacement/normal_map.html
- https://docs.blender.org/manual/en/latest/render/shader_nodes/displacement/displacement.html
- https://docs.blender.org/manual/en/latest/render/shader_nodes/input/object_info.html
- https://docs.blender.org/manual/en/latest/render/eevee/limitations/nodes_support.html (replaces the dead 4.0 URL `.../4.0/render/eevee/materials/nodes_support.html`, which only now exists in the 4.0 manual)
- https://docs.blender.org/manual/en/latest/render/eevee/render_settings/performance.html
- https://developer.blender.org/docs/release_notes/5.2/eevee/ and https://developer.blender.org/docs/release_notes/5.2/cycles/ (also 5.3 EEVEE/Cycles notes: no texture-filtering entries)
- https://projects.blender.org/blender/blender/issues/68917 (HTML returns 403 to scripts; content read through https://projects.blender.org/api/v1/repos/blender/blender/issues/68917 and `/comments`)
- https://raw.githubusercontent.com/blender/blender/v5.2.1/source/blender/gpu/shaders/material/gpu_shader_material_tex_image.glsl (GitHub mirror of the Blender source; `projects.blender.org` raw URLs return 403 to scripts)
- https://docs.quixel.com/mixer/1/en/topic/atlas-layer.html ; https://docs.quixel.com/mixer/1/en/topic/advanced-texture-setup-for-exports.html
- https://dev.epicgames.com/documentation/en-us/unreal-engine/quixel-bridge-plugin-for-unreal-engine ; https://dev.epicgames.com/documentation/en-us/twinmotion/quixel-megascans-assets-in-the-twinmotion-library ; https://dev.epicgames.com/documentation/en-us/fab/setting-up-assets-for-fab-in-launcher
- Blender 5.2.1 LTS checks via the Blender MCP (live) and headless `Blender -b`, 2026-11-18, plus a read of the local Megascans library under `~/Documents/Megascans Library/Downloaded/surface` (7 assets).

## 10. What shipped (supersedes the recommendation in section 0)

A single UV-in/UV-out group (`Quixlend Anti-Tiling`), hard cell borders. Spec: `.scratch/anti-tiling/spec.md`.

- **Path:** the hex prototype (sections 3 and 5; four groups, three samples per map) was judged too heavy and its 3-UV node ugly. A UV-only group in the style of Poliigon's Mosaic mapping (random per-cell offset/rotation, one sample per map; structure seen in the Poliigon Material Converter add-on, nothing copied, that add-on is GPL and Quixlend is MIT) was kept. Border softening was tried and dropped: a noise-bent border only made borders wander, a lean 3-sample hex fade showed triangle-edge lines in EEVEE, and a stochastic Blend (cell lookup jittered per sample) gave a cross-fade in Cycles only, blurrier and grainy elsewhere.
- **Controls:** Cell Size, Rotation, Seed, Per Object. Surfaces get it by default at build time; other types via a Shader Editor panel.
- **Not measured:** cost versus plain tiling, GPU Cycles, 4k bandwidth, other assets, Rotation on normal-mapped assets.
