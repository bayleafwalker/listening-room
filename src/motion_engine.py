"""Deterministic masked motion over immutable room artwork."""
import math
import random

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps


def bounds(rect, size):
    return tuple(round(v * size[i % 2]) for i, v in enumerate(rect))


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


def prepare_scene(plate, scene, size=(1440, 810)):
    base = ImageOps.pad(plate.convert('RGB'), size, method=Image.Resampling.LANCZOS, color=(0,0,0))
    effects=[]
    for index, original in enumerate(scene['effects']):
        effect=dict(original)
        rect=effect['rect']
        if len(rect)!=4 or not (0<=rect[0]<rect[2]<=1 and 0<=rect[1]<rect[3]<=1):
            raise ValueError('Effect rectangle must fit the scene')
        box=bounds(rect,size); w,h=box[2]-box[0],box[3]-box[1]
        if min(w,h)<1: raise ValueError('Effect rectangle is too small')
        kind=effect['kind']
        if kind not in ('rain','mist','glint','glow','water','steam','fire','electrical','cloud','smoke','shimmer'):
            raise ValueError('Unknown room visual effect: '+kind)
        rng=random.Random(effect.get('seed',2409+index))
        effect.update(box=box,mask=_mask(effect,size,box))
        if kind in ('cloud', 'smoke') and effect.get('protect'):
            # Expand foreground exclusion by the maximum source displacement:
            # a protected tower or branch must not be sampled into the sky.
            amplitude=effect.get('amplitude', 4 if kind == 'cloud' else 5)
            if not isinstance(amplitude, (int, float)) or not math.isfinite(amplitude) or not 0 <= amplitude <= 30:
                raise ValueError('Effect amplitude must be between zero and 30')
            exclusion=Image.new('L',(w,h)); draw=ImageDraw.Draw(exclusion)
            for polygon in effect['protect']:
                draw.polygon([(round(x*size[0])-box[0],round(y*size[1])-box[1]) for x,y in polygon], fill=255)
            radius=math.ceil(amplitude*size[0]/1440)
            if radius:
                exclusion=exclusion.filter(ImageFilter.MaxFilter(radius*2+1))
            effect['mask']=ImageChops.multiply(effect['mask'],ImageOps.invert(exclusion))
        count=effect.get('count',max(16,min(70,w*h//2500)))
        effect['particles']=[(rng.random(),rng.random(),rng.random(),rng.random()) for _ in range(count)]
        effect['phase_offset']=rng.random()*2*math.pi
        if kind in ('steam', 'smoke') and 'source_threshold' in effect:
            source = base.crop(box).convert('L')
            threshold = effect['source_threshold']
            effect['mask'] = ImageChops.multiply(
                effect['mask'], source.point(lambda v: max(0, min(255, (v-threshold)*4))))
        if kind == 'electrical':
            # Restrict the animated tint to light already painted in the plate.
            # Different seeds give each sign/window group its own loop pattern.
            patch = base.crop(box).convert('L')
            threshold = effect.get('threshold', 110)
            effect['light_mask'] = ImageChops.multiply(
                effect['mask'], patch.point(lambda v: max(0, min(255, (v-threshold)*4))))
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
    return base,effects


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
        elif kind in ('cloud', 'smoke'):
            # Refract painted detail within the authored mask. Cloud fields
            # crossfade across their resets to maintain one-way drift.
            amplitude=effect.get('amplitude', 4 if kind == 'cloud' else 5)
            if not isinstance(amplitude, (int, float)) or not math.isfinite(amplitude) or not 0 <= amplitude <= 30:
                raise ValueError('Effect amplitude must be between zero and 30')
            amplitude *= base.width / 1440
            patch=base.crop((x0,y0,x1,y1))
            step=max(2,h//24)
            def warped_field(offset=None):
                mesh=[]
                def displacement(pos):
                    t=pos/max(1,h)
                    envelope=math.sin(math.pi*t)
                    if kind == 'cloud':
                        return amplitude*envelope*offset, 0
                    return (amplitude*envelope*math.sin(local_phase+t*6),
                            amplitude*.7*envelope*math.sin(local_phase+t*4))
                for y in range(0,h,step):
                    end=min(h,y+step)
                    ax,ay=displacement(y); bx,by=displacement(end)
                    mesh.append(((0,y,w,end),(max(0,ax), max(0,y+ay),
                        max(0,bx), min(h,end+by), min(w,w+bx), min(h,end+by),
                        min(w,w+ax), max(0,y+ay))))
                return patch.transform((w,h),Image.Transform.MESH,mesh,resample=Image.Resampling.BILINEAR)
            if kind == 'cloud':
                u=(unit*cycles)%1
                first=warped_field(u-.5)
                second=warped_field((u+.5)%1-.5)
                warped=Image.blend(second,first,math.sin(math.pi*u)**2)
            else:
                warped=warped_field()
            shot.paste(warped,(x0,y0),effect['mask'].point(lambda v:round(v*strength)))
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
        elif kind=='fire':
            # Refract only the painted flame, using a deforming mesh. The
            # stationary authored mask excludes grate bars and stonework.
            patch=base.crop((x0,y0,x1,y1))
            mesh=[]
            step=max(2,h//16)
            def displacement(y):
                height=1-y/max(1,h)
                return (w*.10*height*math.sin(local_phase*3+y/h*5)
                        +w*.045*height*math.sin(local_phase*7-y/h*8+.7))
            for y in range(0,h,step):
                end=min(h,y+step)
                a,b=displacement(y),displacement(end)
                mesh.append(((0,y,w,end),(max(0,a),y,max(0,b),end,
                    min(w,w+b),end,min(w,w+a),y)))
            warped=patch.transform((w,h),Image.Transform.MESH,mesh,resample=Image.Resampling.BILINEAR)
            shot.paste(warped,(x0,y0),effect['mask'].point(lambda v:round(v*strength)))
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
    return shot
