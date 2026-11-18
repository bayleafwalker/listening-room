# Listening room visual direction

Eight places to spend time, each with its own authored plate and sound pairing.
The native control window stays quiet and consistent. The room artwork carries
the identity; a restrained accent ties its heading and Enter button to the plate.
Existing desktop typography and transport controls remain familiar. No spectrum
analyzers, fake audio reactivity, camera drift or flashing effects.

## Scene plan

| Room | Authored elements and palette | Bounded motion | Sound pairing |
|---|---|---|---|
| Midnight library | Book-lined Gothic study, tall rain windows, amber desk lamp. Ink `#19252b`, forest `#34443a`, oak `#624632`, amber `#c79b68`. | Fine rain across four protected glass regions, with varied drop speeds. | Neoclassical piano; intimate reading-room tone. |
| Night drive | Sheltered motorway stop, wet asphalt, distant hills and road lights. Blue `#243d58`, slate `#546677`, gold `#c8a368`, cool accent `#76a9bd`. | Small reflected highlights on wet asphalt. | TruckersFM; broad pop and presenters. |
| Northern quiet | Boreal pines, mossy shore, open lake and cool dawn. Pine `#233e3a`, sage `#71877b`, silver `#c5d1cc`, water `#9cb8b8`. | Low mist over open water. | SomaFM Drone Zone; spacious ambient textures. |
| Velvet circuit | Intimate Art Deco salon, gramophone, velvet, brass and geometric woodwork. Jade `#31584f`, burgundy `#633747`, walnut `#49332e`, brass `#d6b671`. | Very slight warmth around a real lamp. | Electro swing; brass and vintage rhythm with an electronic pulse. |
| After hours | Anime-inspired rooftop refuge, violet skyline, rain-slick rails and soft neon. Indigo `#292b55`, violet `#74639c`, cyan `#78b8c9`, lilac `#c5a5df`. | Small glints on wet rooftop surfaces. | Nightcore; bright, fast electronic pop against a quiet visual. |
| Atomic lounge | Hopeful 1950s desert diner, curved window, valve radio and distant antenna. Turquoise `#6a9f9a`, ivory `#ddd4b7`, ochre `#bd8a55`, dusk `#7c718b`. | Slow localized glow at a pendant light. | SomaFM Secret Agent; cinematic lounge and retro-spy atmosphere. |
| Finnish discoveries | Midsummer lakeside sauna, birch, red timber, wooden pier and pale horizon. Timber `#914c3d`, birch `#d1d4c8`, lake `#748e92`, moss `#a9b7a2`. | Mist over the lake clear of the pier and sauna. | YleX direct audio; Finnish music and culture. |
| Wizarding Valley | Preserve the original sheltered study, distant floating islands, mountains, lake and warm foreground. Stone `#777974`, moss `#69755b`, gold `#b6ad85`, water `#85999e`. | Soft warmth around the existing foreground lantern; valley, figures and architecture stay fixed. | Radio Rivendell fantasy radio; gentler discovery through YouTube Music. |

## Wizarding Valley audio identity

The intended channel is an inhabited, welcoming magical place: celesta and harp,
felt piano, small chamber strings, occasional woodwind and warm sustained tones.
Prefer slow phrasing and space between notes. A dedicated curated mix would avoid
battle percussion, trailer crescendos, aggressive dark drones and frequent vocals.
Radio Rivendell is a working fantasy-radio pairing, **not** a guarantee of that
exact gentle programming. Its official direct stream was checked without local
audio playback: HTTP 200, audio/mpeg, on 2026-11-14. The catalogue also includes a
YouTube Music search for gentler fantasy instrumentation. These are independent
sources; no copyrighted music is bundled with the app.

Source: [Radio Rivendell official listening instructions](https://www.radiorivendell.com/page/listen/).

## Delivery and UX

- Seven original room plates were generated with the built-in image tool by Luna
  workers. Exact prompts and generation method live in the artwork JSON files.
  The Wizarding Valley PNG is an unchanged copy of the existing user artwork;
  its source and hash are in `wizard-provenance.json`.
- Build-time Pillow composition and FFmpeg encode produce 20-second 1440×810 H.264
  loops at 24 fps, matching poster frames and full-resolution stills. No renderer service,
  network dependency or GPU generation is needed to open a room. Videos have no
  audio track. The camera and original architecture stay fixed.
- Each effect has an explicit polygon footprint aligned after inspecting its plate,
  with protected architecture and edges feathered inward.
  Rain travels downward, mist drifts, and local reflections vary independently.
  The frame phase wraps deterministically. Tests check periodicity, actual motion,
  preserved pixels outside effect regions, and complete ultrawide composition.
- Preview selection and Surprise me show artwork only. Enter room is the explicit
  playback action. Include view is optional; Still artwork substitutes the original
  plate for a loop. The choice applies on entry, without changing an open player.
- Full source composition stays in frame in previews and delivery, with padding
  where required. Vistas start on workspace 9; existing Bring here and Pin controls
  remain available in Views.
- These are lightweight animated plates, not WorldWindow actor or neural-finish
  renders. Neither the preserved legacy project nor the WorldWindow kernel is
  modified or claimed as having accepted these scenes.
