"""Verify encoded room deliveries, including motion and the loop boundary."""
import json
from pathlib import Path
import subprocess
import sys

from PIL import Image, ImageChops, ImageDraw, ImageStat
from visual_scenes import SCENES


def validate(directory):
    results = []
    width, height, frames = 1440, 810, 480
    samples = (0, 1, 60, 120, 240, 360, 478, 479)
    for identity, scene in SCENES.items():
        path = directory / (identity + '.mp4')
        still_path = directory / (identity + '-still.jpg')
        assert still_path.is_file(), identity + ': reduced-motion artwork is missing'
        with Image.open(still_path) as still:
            still.load()
            assert still.width > 0 and still.height > 0, identity + ': invalid still artwork'
        # Packaging has the original plates available. The installed diagnostic
        # can still check deliveries without shipping a second copy of them.
        plate_path = Path(__file__).parent / 'artwork' / scene['plate']
        if plate_path.is_file():
            if plate_path.suffix.lower() in ('.jpg', '.jpeg'):
                assert still_path.read_bytes() == plate_path.read_bytes(), identity + ': still artwork was altered'
            else:
                with Image.open(plate_path) as original, Image.open(still_path) as still:
                    assert original.size == still.size, identity + ': still artwork was cropped'
                    mean = sum(ImageStat.Stat(ImageChops.difference(original.convert('RGB'), still.convert('RGB'))).mean) / 3
                    assert mean < 3, identity + ': still artwork differs from its original'
        metadata = json.loads(subprocess.check_output([
            'ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(path)]))
        streams = metadata['streams']
        assert len(streams) == 1 and streams[0]['codec_type'] == 'video', identity + ': expected silent video only'
        video = streams[0]
        assert (video['width'], video['height']) == (width, height), identity + ': wrong delivery size'
        assert video['codec_name'] == 'h264' and video['pix_fmt'] == 'yuv420p', identity + ': unsupported delivery codec'
        assert video['r_frame_rate'] == '24/1' and int(video['nb_frames']) == frames, identity + ': wrong frame rate or count'
        assert abs(float(metadata['format']['duration']) - 20) < .001, identity + ': wrong duration'
        selection = '+'.join('eq(n\\,' + str(frame) + ')' for frame in samples)
        blob = subprocess.check_output([
            'ffmpeg', '-v', 'error', '-i', str(path), '-vf', 'select=' + selection,
            '-fps_mode', 'passthrough', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'])
        length = width * height * 3
        assert len(blob) == length * len(samples), identity + ': incomplete decoded samples'
        decoded = [Image.frombytes('RGB', (width, height), blob[i:i+length]) for i in range(0, len(blob), length)]
        footprint = Image.new('L', (width, height))
        draw = ImageDraw.Draw(footprint)
        for effect in scene['effects']:
            x0, y0, x1, y1 = effect['rect']
            # Chroma subsampling can affect a neighbouring pixel. This guard
            # excludes that edge without accepting whole-background drift.
            draw.rectangle((round(x0*width)-4, round(y0*height)-4,
                            round(x1*width)+4, round(y1*height)+4), fill=255)
        outside = ImageChops.invert(footprint)
        movement = False
        background = 0
        for other in decoded[1:]:
            difference = ImageChops.difference(decoded[0], other)
            movement |= any(high > 0 for _, high in ImageStat.Stat(difference, footprint).extrema)
            mean = sum(ImageStat.Stat(difference, outside).mean) / 3
            background = max(background, mean)
        seam = sum(ImageStat.Stat(ImageChops.difference(decoded[0], decoded[-1])).mean) / 3
        assert movement, identity + ': encoded animation is static'
        assert background < .01, identity + ': fixed background changes during the loop'
        assert seam < .25, identity + ': visible encoded loop-boundary change'
        result = {'scene': identity, 'bytes': path.stat().st_size,
                  'background_delta': round(background, 6), 'seam_delta': round(seam, 6)}
        results.append(result)
        print(json.dumps(result), flush=True)
    return results


if __name__ == '__main__':
    validate(Path(sys.argv[1]))
