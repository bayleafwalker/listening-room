"""Deterministic masked motion over immutable room artwork.

Effects come in four classes, chosen by what the painted subject does:

- overlays draw new matter in front of the plate (rain, glints, mist, smoke
  wisps, sparks);
- light-only effects change brightness in place and never move a pixel
  (electrical lights, glow, shimmer, candle, fire light spill);
- procedural layers synthesise a subject the plate can only show frozen
  (fire), confined to its zone and kept behind occluders;
- displacement refracts painted detail, and only where the subject really
  flows (water, cloud drift).

A scene's ``rigid`` regions are pasted back from the plate after every effect,
so grates, bars, frames and furniture occlude moving matter and never move.
"""
import math
import random

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageMath, ImageOps


def bounds(rect, size):
    return tuple(round(v * size[i % 2]) for i, v in enumerate(rect))


class PreparedScene(tuple):
    """``(base, effects)``, plus the rigid occlusion mask and its box."""

    def __new__(cls, base, effects, rigid=None):
        prepared = super().__new__(cls, (base, effects))
        prepared.rigid = rigid
        prepared.rigid_box = rigid.getbbox() if rigid is not None else None
        return prepared


def _polygon_points(polygon, size, origin=(0, 0)):
    if len(polygon) < 3 or any(len(p) != 2 or not (0 <= p[0] <= 1 and 0 <= p[1] <= 1) for p in polygon):
        raise ValueError('Polygons need at least three points inside the scene')
    return [(round(x*size[0])-origin[0], round(y*size[1])-origin[1]) for x, y in polygon]


def rigid_mask(scene, base):
    """Union of the scene's rigid regions at the base size, or None.

    A region is a polygon, optionally narrowed to pixels darker than a luma
    threshold (thin wrought iron in front of bright fire) and grown by a few
    pixels so antialiased edges stay with the structure.
    """
    regions = scene.get('rigid') or []
    if not regions:
        return None
    size = base.size
    mask = Image.new('L', size)
    luma = None
    for region in regions:
        layer = Image.new('L', size)
        ImageDraw.Draw(layer).polygon(_polygon_points(region['polygon'], size), fill=255)
        if 'darker_than' in region:
            threshold = region['darker_than']
            if not isinstance(threshold, int) or not 0 < threshold < 256:
                raise ValueError('Rigid darker_than must be a luma level')
            if luma is None:
                luma = base.convert('L')
            layer = ImageChops.multiply(layer, luma.point(lambda v: 255 if v < threshold else 0))
        grow = region.get('grow', 0)
        if not isinstance(grow, int) or not 0 <= grow <= 4:
            raise ValueError('Rigid grow must be between zero and four pixels')
        grow = round(grow * size[0] / 1440)
        if grow:
            layer = layer.filter(ImageFilter.MaxFilter(grow*2+1))
        mask = ImageChops.lighter(mask, layer)
    return mask


def _noise(size, cell, rng, contrast=1.6):
    """Smooth value noise that tiles exactly in both directions."""
    w, h = size
    gw, gh = max(2, round(w/cell[0])), max(2, round(h/cell[1]))
    grid = Image.new('L', (gw, gh))
    grid.putdata([rng.randrange(256) for _ in range(gw*gh)])
    tiled = Image.new('L', (gw*3, gh*3))
    for i in range(3):
        for j in range(3):
            tiled.paste(grid, (i*gw, j*gh))
    # Resampling a periodic grid keeps its period in the central tile.
    field = tiled.resize((w*3, h*3), Image.Resampling.BICUBIC).crop((w, h, 2*w, 2*h))
    return field.point(lambda v: max(0, min(255, round(128 + (v-128)*contrast))))


def _flicker(phase, offset, harmonics):
    """A gain near one built from whole loop harmonics, so it always wraps."""
    return 1 + sum(amount*math.sin(phase*cycles + offset*(i+1)*1.7)
                   for i, (cycles, amount) in enumerate(harmonics))


# Flame colour by intensity: transparent, ember red, orange, yellow, pale core.
_FLAME_STOPS = ((0, (0, 0, 0)), (.18, (70, 10, 0)), (.38, (175, 48, 6)),
                (.58, (240, 112, 22)), (.78, (255, 176, 64)), (.92, (255, 222, 140)),
                (1, (255, 244, 205)))


def _ramp(channel):
    lut = []
    for v in range(256):
        t = v/255
        for (a, ca), (b, cb) in zip(_FLAME_STOPS, _FLAME_STOPS[1:]):
            if t <= b:
                lut.append(round(ca[channel] + (cb[channel]-ca[channel])*(t-a)/(b-a)))
                break
    return lut


_FLAME_LUT = [_ramp(c) for c in range(3)]
_FIRE_FLICKER = ((7, .05), (17, .045), (29, .035), (43, .025))
_CANDLE_FLICKER = ((5, .035), (13, .05), (31, .04), (47, .03))


def _mask(effect, size, box):
    left, top, right, bottom = box
    mask = Image.new('L', (right-left, bottom-top))
    draw = ImageDraw.Draw(mask)
    def points(polygon):
        return [(round(x*size[0])-left, round(y*size[1])-top) for x,y in polygon]
    if effect.get('polygon'):
        draw.polygon(points(effect['polygon']), fill=255)
    else:
        draw.rectangle((0, 0, mask.width-1, mask.height-1), fill=255)
    for polygon in effect.get('protect', []):
        draw.polygon(points(polygon), fill=0)
    # Blur inward: no light/rain leaks onto protected architecture or beyond
    # the authored footprint. Padding also softens the outer rectangle edge.
    radius = max(1, round(min(mask.size)*effect.get('feather', .08)))
    padded = ImageOps.expand(mask, border=radius*3, fill=0)
    soft = padded.filter(ImageFilter.GaussianBlur(radius)).crop((radius*3,radius*3,radius*3+mask.width,radius*3+mask.height))
    return ImageChops.multiply(mask, soft)


def _amplitude(effect, default):
    amplitude = effect.get('amplitude', default)
    if not isinstance(amplitude, (int, float)) or not math.isfinite(amplitude) or not 0 <= amplitude <= 30:
        raise ValueError('Effect amplitude must be between zero and 30')
    return amplitude


def _union_mask(polygons, size, box, feather):
    mask = Image.new('L', (box[2]-box[0], box[3]-box[1]))
    for polygon in polygons:
        mask = ImageChops.lighter(mask, _mask({'polygon': polygon, 'feather': feather}, size, box))
    return mask


def _prepare_fire(effect, base, size, rng):
    box = effect['box']; w, h = box[2]-box[0], box[3]-box[1]
    scale = size[0] / 1440
    patch = base.crop(box)
    threshold = effect.get('flame_threshold', 150)
    # The painted flame says where the fire burns; the living flame grows
    # from it, a little taller, and from the fuel bed along the zone floor.
    painted = ImageChops.multiply(effect['mask'], patch.convert('L').point(
        lambda v: max(0, min(255, (v-threshold)*3))))
    zone = effect['mask'].getbbox() or (0, 0, w, h)
    envelope = painted
    reach = max(1, round(effect.get('reach', .15) * h))
    for step in range(1, 7):
        lifted = Image.new('L', (w, h))
        lifted.paste(painted, (0, -round(reach*step/6)))
        envelope = ImageChops.lighter(envelope, lifted.point(lambda v, f=1-step/8: round(v*f)))
    bed = effect.get('bed', .45)
    if not 0 <= bed <= 1:
        raise ValueError('Fire bed must be between zero and one')
    floor = Image.new('L', (w, h))
    floor.paste(Image.linear_gradient('L').resize((w, zone[3]-zone[1])).point(
        lambda v: round(255*bed*(v/255)**2.5)), (0, zone[1]))
    envelope = ImageChops.lighter(envelope, ImageChops.multiply(floor, effect['mask']))
    effect['envelope'] = ImageChops.multiply(
        envelope.filter(ImageFilter.GaussianBlur(max(1, 2*scale))), effect['mask'])
    tongue = effect.get('tongue', 9) * scale
    effect['textures'] = (_noise((w, h), (tongue, tongue*4), rng, 2),
                          _noise((w, h), (tongue*.5, tongue*2), rng, 2))
    suppress = effect.get('suppress', .7)
    if not 0 <= suppress <= 1:
        raise ValueError('Fire suppress must be between zero and one')
    # Suppression reaches fainter painted licks than the envelope does.
    faint = ImageChops.multiply(effect['mask'], patch.convert('L').point(
        lambda v: max(0, min(255, (v-threshold+35)*5))))
    # The painted ember bed at the foot of the zone stays; only flames above
    # it are replaced.
    top, foot = zone[3] - .3*(zone[3]-zone[1]), zone[3] - .1*(zone[3]-zone[1])
    above_bed = Image.new('L', (1, h))
    above_bed.putdata([round(255*max(0, min(1, (foot-y)/(foot-top)))) for y in range(h)])
    effect['suppress_mask'] = ImageChops.multiply(ImageChops.multiply(faint.filter(ImageFilter.MaxFilter(3)).filter(
        ImageFilter.GaussianBlur(max(1, 1.5*scale))), effect['mask']), above_bed.resize((w, h))).point(
        lambda v: round(v*suppress))
    # Stand-in for the wall behind the painted flame: the surrounding wall,
    # spread in from all sides (a blur normalised by what it averaged).
    keep = ImageOps.invert(faint.filter(ImageFilter.MaxFilter(5)))
    radius = max(2, 10*scale)
    weight = keep.filter(ImageFilter.GaussianBlur(radius))
    effect['suppressed'] = Image.merge('RGB', [
        ImageMath.lambda_eval(lambda a: a['n'] * 255 / (a['d'] + 1),
                              n=ImageChops.multiply(band, keep).filter(ImageFilter.GaussianBlur(radius)),
                              d=weight).convert('L')
        for band in patch.split()])
    effect['spill_mask'] = None
    if effect.get('spill'):
        spill = _union_mask(effect['spill'], size, box, effect.get('spill_feather', .12))
        protected = Image.new('L', (w, h))
        for polygon in effect.get('protect', []):
            ImageDraw.Draw(protected).polygon(_polygon_points(polygon, size, box[:2]), fill=255)
        effect['spill_mask'] = ImageChops.multiply(spill, ImageOps.invert(protected))
    effect['sparks'] = [(zone[0] + rng.random()*(zone[2]-zone[0]), rng.choice((7, 9, 11, 13)),
                         rng.random(), .5+.5*rng.random(), rng.random())
                        for _ in range(effect.get('sparks', 0))]
    effect['zone'] = zone


def prepare_scene(plate, scene, size=(1440, 810)):
    base = ImageOps.pad(plate.convert('RGB'), size, method=Image.Resampling.LANCZOS, color=(0,0,0))
    rigid = rigid_mask(scene, base)
    effects=[]
    for index, original in enumerate(scene['effects']):
        effect=dict(original)
        rect=effect['rect']
        if len(rect)!=4 or not (0<=rect[0]<rect[2]<=1 and 0<=rect[1]<rect[3]<=1):
            raise ValueError('Effect rectangle must fit the scene')
        box=bounds(rect,size); w,h=box[2]-box[0],box[3]-box[1]
        if min(w,h)<1: raise ValueError('Effect rectangle is too small')
        kind=effect['kind']
        if kind not in ('rain','mist','glint','glow','water','steam','fire','candle','electrical','cloud','smoke','shimmer'):
            raise ValueError('Unknown room visual effect: '+kind)
        rng=random.Random(effect.get('seed',2409+index))
        effect.update(box=box,mask=_mask(effect,size,box))
        if kind in ('cloud', 'water', 'smoke'):
            amplitude = _amplitude(effect, {'cloud': 4, 'water': 1.4, 'smoke': 5}[kind])
        if kind in ('cloud', 'water'):
            # Expand foreground exclusion by the maximum source displacement:
            # a protected tower, post or rigid frame must not be sampled into
            # the moving sky or water beside it.
            exclusion=Image.new('L',(w,h)); draw=ImageDraw.Draw(exclusion)
            for polygon in effect.get('protect', []):
                draw.polygon([(round(x*size[0])-box[0],round(y*size[1])-box[1]) for x,y in polygon], fill=255)
            if rigid is not None:
                exclusion = ImageChops.lighter(exclusion, rigid.crop(box))
            if kind == 'cloud' or 'amplitude' in effect:
                amplitude *= size[0]/1440
            radius=math.ceil(amplitude)
            if radius and exclusion.getbbox():
                exclusion=exclusion.filter(ImageFilter.MaxFilter(radius*2+1))
            effect['mask']=ImageChops.multiply(effect['mask'],ImageOps.invert(exclusion))
        count=effect.get('count',max(16,min(70,w*h//2500)))
        effect['particles']=[(rng.random(),rng.random(),rng.random(),rng.random()) for _ in range(count)]
        effect['phase_offset']=rng.random()*2*math.pi
        if kind == 'cloud' and 'source_threshold' in effect:
            # Drift only painted cloud and the sky just around it: isolated
            # stars (one or two pixels) are opened away and stay fixed.
            threshold = effect['source_threshold']
            reach = math.ceil(amplitude) + max(1, round(min(w, h)*effect.get('feather', .08)))
            clouds = base.crop(box).convert('L').point(lambda v: 255 if v > threshold else 0).filter(
                ImageFilter.MinFilter(3)).filter(ImageFilter.MaxFilter(3))
            # A blur and a low threshold grow the cloud by about `reach`.
            grown = clouds.filter(ImageFilter.GaussianBlur(reach/2)).point(lambda v: 255 if v > 6 else 0)
            effect['mask'] = ImageChops.multiply(effect['mask'], grown.filter(ImageFilter.GaussianBlur(reach/3)))
        if kind in ('steam', 'smoke') and 'source_threshold' in effect:
            source = base.crop(box).convert('L')
            threshold = effect['source_threshold']
            effect['mask'] = ImageChops.multiply(
                effect['mask'], source.point(lambda v: max(0, min(255, (v-threshold)*4))))
        if kind == 'smoke':
            # Rising wisps: soft tiling density that scrolls up through the
            # painted plume. Nothing is displaced, so the chimney and the
            # branches behind the plume cannot smear into it.
            wisp = effect.get('wisp', 26) * size[0]/1440
            effect['textures'] = (_noise((w, h), (wisp, wisp*1.3), rng, 1.3),
                                  _noise((w, h), (wisp*.6, wisp*.8), rng, 1.3))
        if kind in ('electrical', 'candle'):
            # Restrict the animated tint to light already painted in the plate.
            # Different seeds give each sign/window group its own loop pattern.
            patch = base.crop(box).convert('L')
            threshold = effect.get('threshold', 110 if kind == 'electrical' else 150)
            effect['light_mask'] = ImageChops.multiply(
                effect['mask'], patch.point(lambda v: max(0, min(255, (v-threshold)*4))))
        if kind == 'fire':
            _prepare_fire(effect, base, size, rng)
        # Spatially periodic bands avoid a visible texture seam when moving.
        if kind in ('mist','steam'):
            tile=Image.new('L',(w,h)); pixels=tile.load()
            for y in range(h):
                envelope=math.sin(math.pi*(y+.5)/h)**1.4
                for x in range(w):
                    cloud=(.48+.22*math.sin(2*math.pi*x/w+3*y/h)+.13*math.sin(6*math.pi*x/w-5*y/h)+.07*math.sin(10*math.pi*x/w+y/h))
                    pixels[x,y]=round(38*max(0,cloud)*envelope)
            effect['texture']=tile.filter(ImageFilter.GaussianBlur(max(1,h//12)))
        effects.append(effect)
    return PreparedScene(base, effects, rigid)


def _fire(shot, effect, unit, phase, strength):
    """A living flame layer inside the firebox, its light, and a few sparks."""
    x0, y0, x1, y1 = box = effect['box']; w, h = x1-x0, y1-y0
    heat = _flicker(phase, effect['phase_offset'], _FIRE_FLICKER)
    patch = shot.crop(box)
    # Subdue the frozen painted flame so the moving one replaces it.
    patch = Image.composite(effect['suppressed'], patch, effect['suppress_mask'])
    if effect['spill_mask'] is not None:
        gain = 1 + (heat-1)*effect.get('spill_gain', .8)
        patch = Image.composite(patch.point(lambda v: min(255, round(v*gain))), patch, effect['spill_mask'])
    rise = effect.get('rise', 9)
    if not isinstance(rise, int) or rise < 1:
        raise ValueError('Fire rise must be a positive whole number of cycles')
    coarse, fine = effect['textures']
    lean = round(1.5*math.sin(phase*3 + effect['phase_offset']) * shot.width/1440)
    # Multiplying two rising fields gives separate tongues that merge,
    # split and thin out as they climb, instead of one smooth sheet.
    noise = ImageChops.multiply(ImageChops.offset(coarse, lean, -round(unit*rise*h)),
                                ImageChops.offset(fine, -lean, -round(unit*(rise + rise//2 + 1)*h))
                                ).point(lambda v: min(255, round(v*2.2)))
    boost = effect.get('heat', 1.6) * heat
    field = ImageChops.multiply(effect['envelope'], noise).point(lambda v: min(255, round(v*boost)))
    flame = Image.merge('RGB', [field.point(lut) for lut in _FLAME_LUT])
    patch = Image.composite(ImageChops.screen(patch, flame), patch,
                            effect['mask'].point(lambda v: round(v*strength)))
    sparks = Image.new('L', (w, h)); draw = ImageDraw.Draw(sparks)
    zx0, zy0, zx1, zy1 = effect['zone']
    for x, cycles, offset, light, sway in effect['sparks']:
        life = (unit*cycles + offset) % 1
        if life > .55:
            continue
        t = life / .55
        sx = x + 4*math.sin(t*5 + sway*6.3) * shot.width/1440
        sy = zy1 - (zy1-zy0)*(.25 + .7*t)
        value = round(255*light*(1-t)**1.5)
        draw.rectangle((round(sx), round(sy), round(sx)+1, round(sy)), fill=value)
    sparks = ImageChops.multiply(sparks.filter(ImageFilter.GaussianBlur(.6)).point(lambda v: min(255, v*2)),
                                 effect['mask'])
    patch.paste((255, 196, 112), (0, 0, w, h), sparks)
    shot.paste(patch, box[:2])


def room_frame(prepared, frame, total_frames=480):
    if total_frames<=0:raise ValueError('total_frames must be positive')
    base,effects=prepared
    shot=base.copy()
    unit=(frame%total_frames)/total_frames
    phase=2*math.pi*unit
    for effect in effects:
        x0,y0,x1,y1=effect['box'];w,h=x1-x0,y1-y0
        kind=effect['kind']; strength=effect.get('strength',.65)
        cycles=effect.get('cycles',2 if kind=='water' else 1)
        if not isinstance(cycles,int) or cycles<1:raise ValueError('Effect cycles must be a positive integer')
        local_phase=phase*cycles+effect['phase_offset']
        if not 0<=strength<=1:raise ValueError('Effect strength must be between zero and one')
        alpha=Image.new('L',(w,h));draw=ImageDraw.Draw(alpha)
        particles=effect['particles']
        if kind=='rain':
            # Integer cycles make downward travel periodic. Fade at the mask
            # edges rather than reversing drops at the end of a sine wave.
            cycles=effect.get('cycles',3)
            rain_cycles = effect.get('rain_cycles', [cycles])
            if not rain_cycles or any(not isinstance(c, int) or c < 1 for c in rain_cycles):
                raise ValueError('Rain cycles must be positive integers')
            for px,py,speed,light in particles:
                # Integer periods preserve the loop while individual drops travel
                # at different speeds. The legacy default remains unchanged.
                drop_cycles = rain_cycles[min(int(speed * len(rain_cycles)), len(rain_cycles)-1)]
                x=round(px*w); y=round(((py+unit*drop_cycles)%1)*h)
                length=max(3,round(h*(.012+.012*speed)))
                value=round(22+light*25)
                draw.line((x,y,x-1,y+length),fill=value,width=1)
                if y+length>=h:draw.line((x,y-h,x-1,y-h+length),fill=value,width=1)
        elif kind in ('mist','steam'):
            direction=effect.get('cycles',1)
            if kind=='mist':
                # Two independently drifting, seamless fields suggest depth.
                a=ImageChops.offset(effect['texture'],round(unit*w*direction),0)
                b=ImageChops.offset(effect['texture'],-round(unit*w*direction*2),round(2*math.sin(local_phase)))
                alpha=ImageChops.add(a,b,scale=1.5)
            else:
                alpha=ImageChops.offset(effect['texture'],round(2*math.sin(local_phase)),-round(unit*h*direction))
        elif kind == 'cloud':
            # Clouds drift one way as a whole. Two copies travel the same
            # path half a cycle apart and crossfade at their resets, so the
            # loop wraps without a jump and painted shapes never shear.
            amplitude = _amplitude(effect, 4) * base.width / 1440
            drift = effect.get('drift', 1)
            if drift not in (-1, 1):
                raise ValueError('Cloud drift must be -1 or 1')
            u = (unit*cycles) % 1
            def drifted(offset):
                return base.transform((w, h), Image.Transform.AFFINE,
                                      (1, 0, x0 - drift*offset*amplitude, 0, 1, y0),
                                      resample=Image.Resampling.BILINEAR)
            warped = Image.blend(drifted((u+.5) % 1 - .5), drifted(u-.5), math.sin(math.pi*u)**2)
            shot.paste(warped,(x0,y0),effect['mask'].point(lambda v:round(v*strength)))
            continue
        elif kind == 'smoke':
            # Two soft density fields rise at different whole-loop speeds and
            # multiply into wisps that thicken the painted plume as they pass.
            amplitude = _amplitude(effect, 5) * base.width / 1440
            first, second = effect['textures']
            drift = effect.get('drift', 0)
            if not isinstance(drift, int):
                raise ValueError('Smoke drift must be a whole number of widths per loop')
            sway = round(amplitude * .3 * math.sin(local_phase))
            a = ImageChops.offset(first, round(unit*drift*w) + sway, -round(unit*cycles*h))
            b = ImageChops.offset(second, round(unit*drift*w) - sway, -round(unit*(cycles+1)*h))
            density = ImageChops.multiply(a, b).point(lambda v: max(0, min(255, (v-18)*3)))
            alpha = ImageChops.multiply(density, effect['mask']).point(lambda v: round(v*strength*.55))
            shot.paste(tuple(effect.get('color', (204, 206, 195))), (x0, y0, x1, y1), alpha)
            continue
        elif kind=='shimmer':
            # A traveling highlight only strengthens existing bright details.
            # The spatial wave moves continuously and wraps with the loop.
            patch=base.crop((x0,y0,x1,y1))
            light=patch.convert('L').point(lambda v: max(0,min(255,(v-effect.get('threshold',140))*4)))
            wave=Image.new('L',(w,1)); pixels=wave.load()
            for x in range(w):
                amount=max(0,math.cos(2*math.pi*x/max(1,w)-local_phase))**6
                value=round(180*amount)
                pixels[x,0]=value
            wave=wave.resize((w,h))
            alpha=ImageChops.multiply(ImageChops.multiply(effect['mask'],light),wave).point(lambda v:round(v*strength))
            bright=patch.point(lambda v:min(255,round(v*1.45)))
            shot.paste(bright,(x0,y0),alpha)
            continue
        elif kind == 'fire':
            _fire(shot, effect, unit, phase, strength)
            continue
        elif kind == 'candle':
            # An enclosed flame: only its painted light flickers, quickly and
            # shallowly, so cut glass and holder stay exactly where they are.
            gain = 1 + (_flicker(phase, effect['phase_offset'], _CANDLE_FLICKER)-1)*effect.get('depth', 1.5)
            patch = shot.crop(effect['box']).point(lambda v: min(255, round(v*gain)))
            shot.paste(patch, (x0, y0), effect['light_mask'].point(lambda v: round(v*strength)))
            continue
        elif kind=='electrical':
            # Bounded, irregular but fully periodic harmonic changes. The
            # low frequencies avoid strobing and never switch a light off.
            wave=(.50+.23*math.sin(local_phase*2)+.14*math.sin(local_phase*3+.9)
                  +.08*math.sin(local_phase*5+2.1))
            gain=.72+.56*wave
            patch=base.crop((x0,y0,x1,y1)).point(lambda v: min(255, round(v*gain)))
            alpha=effect['light_mask'].point(lambda v:round(v*strength))
            shot.paste(patch,(x0,y0),alpha)
            continue
        elif kind=='water':
            # Refract the existing reflection instead of drawing light dashes
            # over it. Small horizontal displacements stay inside the mask.
            patch=base.crop((x0,y0,x1,y1))
            mesh=[]
            for y in range(0,h,4):
                end=min(h,y+4)
                amplitude = effect.get('amplitude', 1.4)
                if not isinstance(amplitude, (int, float)) or not math.isfinite(amplitude) or not 0 <= amplitude <= 30:
                    raise ValueError('Effect amplitude must be between zero and 30')
                if 'amplitude' in effect:
                    amplitude *= base.width / 1440
                shift=amplitude*math.sin(local_phase+y/max(1,h)*math.pi*6)
                left=max(0,shift);right=min(w,w+shift)
                mesh.append(((0,y,w,end),(left,y,left,end,right,end,right,y)))
            warped=patch.transform((w,h),Image.Transform.MESH,mesh,resample=Image.Resampling.BILINEAR)
            mask=effect['mask'].point(lambda v:round(v*strength))
            shot.paste(warped,(x0,y0),mask)
            continue
        elif kind=='glint':
            for px,py,variation,light in particles:
                # Highlights remain near their reflected light, varying
                # independently rather than flashing the complete patch.
                wave=local_phase*(1+int(variation*3))+px*6.28
                x=round(px*w+math.sin(wave)*max(1,w*.006))
                y=round(py*h)
                length=max(2,round(w*(.008+.018*variation)*(1+.12*math.sin(wave))))
                value=round((12+20*light)*(.55+.45*math.sin(wave)**2))
                draw.line((x-length,y,x+length,y),fill=value,width=1)
            alpha=alpha.filter(ImageFilter.GaussianBlur(.45))
        else:
            value=round(14+5*math.sin(local_phase)+2*math.sin(local_phase*3+.7))
            draw.ellipse((w*.15,h*.15,w*.85,h*.85),fill=value)
            alpha=alpha.filter(ImageFilter.GaussianBlur(max(1,min(w,h)*.17)))
        alpha=ImageChops.multiply(alpha,effect['mask']).point(lambda v:round(v*strength))
        color=effect.get('color',(255,187,103) if kind=='glow' else (183,204,214))
        shot.paste(tuple(color),(x0,y0,x1,y1),alpha)
    rigid = getattr(prepared, 'rigid', None)
    if rigid is not None and prepared.rigid_box:
        # Occlusion last: rigid structure is the plate itself in every frame.
        box = prepared.rigid_box
        shot.paste(base.crop(box), box[:2], rigid.crop(box))
    return shot
