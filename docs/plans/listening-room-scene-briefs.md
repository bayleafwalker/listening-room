# Listening Room scene motion briefs

Review direction, 2026-11-15. Each composition is a painted shelter or landscape, not a general-purpose animated overlay. Motion should be perceptible at production size over a 20-second loop, calm enough for listening, and attached to existing objects. Preserve the still and reduced-motion paths. Protect architecture, foreground objects and figures; do not add invented features or strobe. Test the encoded loop, including its boundary.

| Room | Meaningful movement | Boundaries and character |
| --- | --- | --- |
| Midnight Library | Fire within the grate and rain on the exterior window panes. | Preserve stone, grate bars, window mullions, curtains and the desk. Warm, sheltered interior against wet night. |
| Atomic Lounge | Restrained activity in the painted radio tubes and slow movement in the sunset sky. | Keep cloud motion outside the curved windows and behind the mullions, terrain and tower. Do not animate the diner architecture. |
| Velvet Circuit | A gentle candle in the cut-crystal holder and delicate highlights on the glass or lamps. | Keep the candle warm and coherent; protect the facets, gramophone, cabinet and furniture. No conspicuous lamp pulsing. |
| Finnish Discoveries | Chimney smoke rising and lake surface movement. | Smoke starts at the chimney and respects the roof and birches; ripples stay on water, avoiding dock, reeds and shoreline. |
| Night Drive | Rain beyond the shelter, distant mist and restrained movement in wet-road reflections. | Preserve roof, posts, furniture and truck. Glints belong to existing wet surfaces, not arbitrary bright strokes. |
| Northern Quiet | Lake ripples and low, slowly drifting distant mist. | Keep the water within the shoreline and around rocks and reeds; foreground trees remain stationary. Avoid a conspicuous uniform haze pulse. |
| After Hours | Slow cloud movement and restrained activity in existing city lights or reflections. | Preserve moon, building, skyline and balcony rails; no broad terrace flash. |
| Wizard Valley | Distant clouds or mist, water movement, and a restrained lantern flame. | Keep water within painted river and pool; preserve pool rim, people, arch, vegetation and foreground objects. Atmospheric depth should not wash out the scene. |

Acceptance is visual, not merely evidence that pixels change. Inspect encoded 1440×810, 24 fps, 480-frame output at common phases and around the seam, with focused crops for small movement and protected edges. Verify descriptive copy against the actual effect, and distinguish sampled frames from a continuous watch. Approval applies only to the source and artifact hashes reviewed.

## Motion approach (2026-11-18)

Live use showed the library fire did not look like fire: it wobbled, and the
grate bars bent with it. The cause was the method, not the tuning. Every effect
except rain and glints moved painted pixels with a mesh warp, so a warp that
covered both the flame and the grate made both wobble like jelly. Measured
against the plate, the same method also moved a mooring post by up to 28 luma
levels on average, rocks and a bather beside rippling water, the diner's window
frame beside drifting cloud, and the library's fire-screen post. The chimney
plume swayed from side to side instead of rising.

| Option | Reads as its subject | Rigid structure | Cost | Decision |
| --- | --- | --- | --- | --- |
| Tighter warp masks only | No. A warped painted flame still slides as one shape, and smoke still sways. | Better, but warped edges still pull in neighbouring pixels. | None | Rejected |
| Procedural flame layer in the firebox: tongues rising from the painted fire, colour by heat, flicker, sparks, firelight on the hearth | Yes. Tongues rise and split, with a hot core and cooler edges. | The grate is pasted back over it. | Pillow operations on one small patch | Chosen for the open fire |
| Light-only effects that change painted light in place | Yes for lamps, glow, highlights and enclosed flames (a candle in cut glass, a lantern) | Nothing moves. | Trivial | Chosen for lights and enclosed flames |
| Occlusion masks: named rigid regions pasted back from the plate after every effect | Not an effect | Guaranteed, and checkable | Trivial | Chosen for every room |
| Displacement only where the subject flows | Yes for reflections on water and slow cloud drift | Kept clear by an exclusion band as wide as the displacement | As before | Kept for water and clouds; clouds now drift as one layer |
| Rising procedural wisps over the painted plume | Yes. Density travels upward through the plume. | Nothing is displaced, so the chimney cannot smear. | Small | Chosen for chimney smoke |
| Generated video, neural effects or third-party clips | Possibly | Uncontrolled | Network access, licences, non-deterministic output | Ruled out by the constraints |

Each effect now belongs to one of four classes (see `src/motion_engine.py`):

- **Overlays** draw new matter in front of the plate: rain, glints, mist,
  smoke wisps and sparks.
- **Light-only effects** change brightness in place: electrical lights, glow,
  shimmer, candle, and fire light spill.
- **Procedural layers** synthesise a subject that the plate can only show
  frozen. Fire is the only one. It is confined to its zone and kept behind
  occluders.
- **Displacement** refracts painted detail only where the subject flows: water
  and clouds.

The fire layer first subdues the painted flame: its bright licks are filled in
from the surrounding wall, while the painted ember bed stays and brightens with
the firelight. Two tiling noise fields rise at different whole-loop speeds and
multiply into separate tongues inside an envelope grown from the painted fire.
A colour ramp then maps intensity to ember red, orange, yellow and a pale core.
A flicker built from whole-loop harmonics also drives the firelight on the
hearth floor, the back wall and the jamb. The grate, fire screen and stone are
pasted back last, so the bars occlude the flames.

Constraints: the effects are Pillow-only, because the asset build has no NumPy.
Noise is seeded and tiles exactly, and every phase is a whole number of cycles,
so frame 480 equals frame 0. No new media is added; the CC0 plates and tuning
stay the only inputs. Reduced motion still shows the unchanged still. On the
same machine, the room render took 30 s before and 36 s after, and the whole
asset derivation (scenes, sound beds and validation) took 40 s before and 48 s
after.

### Rigid structure oracle

Each `src/scene_tuning/<room>.json` lists named `rigid` polygons. A polygon can
be narrowed to pixels darker than a luma level (`darker_than`) for thin wrought
iron in front of bright fire, and grown by a few pixels (`grow`).

- **Render.** The engine pastes the plate over these regions after every
  effect. Displacement effects exclude them, plus a band as wide as their
  amplitude.
- **Unit test.** `tests/test_listening_visuals.py` checks that every rigid
  pixel equals the plate in sampled source frames of every room.
- **Build check.** `src/validate_vistas.py` decodes the luma of eight sampled
  frames. Inside each region, the mean absolute change against frame 0 must
  stay below 0.05 levels. The encode is lossless in luma, so a truly rigid
  region decodes unchanged.
- **Results.** The previous videos fail this check: mooring post 28.5, library
  fire-screen post 7.1, rock in the shallows 4.0, library grate 2.7, shore rocks
  2.3, bather 1.2, lantern frame 0.85, diner window frame 0.53, litter bin 0.28.
  The new videos score 0.0 in every region.

`scripts/scene-review.py` regenerates the review evidence from any asset
directory, with an optional baseline: contact sheets, before and after
close-ups, rigid overlays, motion maps, preview clips and the stability numbers.

### Review record (2026-11-18)

The videos changed on purpose. The previous hashes stay in git history and in
the 2.3.1 package. The values below are SHA-256 hashes of the new deliveries
from `nix build .#listening-hub.assets`. Frames were reviewed at full size, in
crops up to 8×, at consecutive frames and across the loop seam. These are
sampled frames and short clips, not a continuous watch on the target display.

| Room | Video SHA-256 (new) | What changed and why | Review notes |
| --- | --- | --- | --- |
| Midnight Library | `c600a3e9ee7089f48b61b90035acb8040c3bf6c08b322457a694189637dc0051` | The warped painted fire is replaced by the flame layer, sparks and hearth light. The fire screen, grate, stone jamb, ivy and window mullions are rigid. | Tongues rise and split with a pale core and red edges. The bars occlude them and do not move. The embers behind the grate brighten with the fire. A thin lit rim on the screen post stays still, which is how the painting shows it. Sparks are sparse and mostly hidden behind flame. Rain and lamps are unchanged. |
| Night Drive | `1acd3ad9752dd02dfd8aa1be4ff4c12c3affcb5e4e94eb05970c9d3e15301615` | The litter bin under the roof, the post, the roof edge and the truck are rigid. Rain used to cross the top of the bin. | Rain beyond the shelter and the road glints are otherwise as before. The rain fades out above the car park, as the brief asks for distant rain. |
| Northern Quiet | `8d2efff3579a3f3483b6f6f8cf69c5b17aa7cb260b182a4c92a0807189be34ea` | The far shore, young pine and reeds are rigid, and the ripples keep clear of them. | The reflections waver as on rippled water. The shoreline and trees hold still. |
| Velvet Circuit | `1e6714201cc1fd25eeff117d69981e09a4bae703cc38dba78e8b245c2347ae2d` | The 20×17 px warp over the crystal holder is now a candle flicker of its painted light. The table, lamp stems, gramophone arm and sconce bracket are rigid. | The flame light flickers quickly and shallowly while the facets stay put. The lamps and the horn highlight are unchanged. |
| After Hours | `465b45e28206093a0fd72d731f40edf0c44042ec6c4a39e4e90aaccaceb00fde` | Clouds drift as one layer (amplitude 30 to 12), and isolated stars stay fixed. The railing posts, parapet, air conditioner and skyline are rigid. | The old warp sheared clouds and doubled them 15 px apart. Now there is a mild softening at quarter phases, and the stars no longer slide. A few thin wisps between the big clouds stay still. |
| Atomic Lounge | `d63ebc78475adeced032a9306ca47f983c0f821facfae1af1fdb85352aaf20e2` | Clouds drift as one layer. The window head, mullion, right frame, tower, dish and horizon are rigid. The cloud used to bend the chrome window frame. | The sunset clouds drift behind a still frame. The valves and pendants are unchanged. |
| Finnish Discoveries | `99403c0cee872ebd3e6be77ac6a8bc458ced90118167511abf5a75d4cdd836c4` | The swaying plume warp is now rising wisps. The chimney, far shore, pier, posts, ladder, reeds and rocks are rigid. | The plume stays where it is painted while density travels up through it. The mooring post no longer moves (it changed by 28 levels before), and the rocks are still. The lake ripples are otherwise as before. |
| Wizarding Valley | `1a25f202e2c9b0cc744d8b06e6857bd8a2d3d2cc1179f00e2ba593a9281150a7` | Clouds drift as one layer (amplitude 30 to 12). The lantern's invisible glow is now a candle flicker. The peaks, pool rim, both bathers and the lantern frame are rigid. | The bather beside the pool and the lantern frame used to ripple with the water. The lantern light now visibly breathes. |

### Polish record (2026-11-19)

Four changes after review of the 2026-11-18 deliveries. The new hashes are
SHA-256 of the room videos from `nix build .#listening-hub.assets`; other rooms
are unchanged. Frames were reviewed at full size and at 4× (sampled frames and
short clips, not a continuous watch).

- **Library fire size.** The tuning now has `heat` 1.8, `bed` 0.7 and `reach`
  0.32 (was default, 0.35 and 0.15), so tongues fill the firebox from the
  grate to the upper stones. Brighter values (`heat` 2.0 and above) lit the
  edge beside the left post enough that the independent rigid oracle read it
  as a displaced block (gain .051 against .05); 1.8 leaves it at .040.
- **Smudge.** The fill that replaces the painted flame is smooth, so it showed
  as a flat patch at 3–4× zoom. It now carries two scales of tiling noise
  (`grain`, 0.3), the fill spreads from a wider gap around the flame
  (`fill_gap`, `fill_radius`) and fainter licks are caught (`faint`).
- **Cloud tick.** The drift was an affine bilinear transform. PIL's transform
  at any non-zero shift is about 0.27 mean luma levels softer than the exact
  copy it gives at zero, so frame 0 and the half-loop frame were different
  from their neighbours. They were the largest frame step of the loop (0.29,
  0.58 and 0.70 against a median of 0.02), once per 10 s, since two copies
  cross zero half a loop apart. Clouds now use exact crops and a bias-free
  blend with a fixed per-column dither. The largest step is now 1.4 times
  the median in each room, and frame 0 equals the plate in the cloud rectangle.
- **Tuning validation.** `validate_tuning` in `src/visual_scenes.py` checks
  every key, type and range of every effect kind and rigid region at load. The
  unused fire `lick` key was removed.

| Room | Video SHA-256 (new) |
| --- | --- |
| Midnight Library | `33d699a19a4bb848feb0e67aeb9776c4d0cdf9e3ea64370772e6bd35d8bef8af` |
| After Hours | `784d3def6d8941e1aeb8a79500007f15ce2bee56106b6310d0b61c52ae86d256` |
| Atomic Lounge | `aa84df717c960e3bcf9e048f18e0ecfd678ddab8041bfd940417a6c3b6a9d345` |
| Wizarding Valley | `bbeac8e15c534654b09dcefd06f7880bd0f65a44e52784cbf5d9015a4f189a11` |
