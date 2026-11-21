"""Room posters at card size: decoding a full 1440x810 poster for a 224-pixel card is the slowest part of
opening the window.

The package build writes ``NAME.card.jpg`` next to every ``NAME.png`` poster in the assets folder
(``python3 poster_cards.py ASSETS``). The window shows that file on a room card when it exists, and
otherwise scales the poster itself, off the GTK thread, with ``scaled`` below.

Pillow only: no GTK here, so the asset build can run it.
"""
import sys
from pathlib import Path

CARD_SIZE = (640, 360)      # covers the widest card (about 290 px) at a 2x display scale
SUFFIX = '.card.jpg'


def card_file(poster):
    """The pre-scaled card file for a poster path, or None when the build did not write one."""
    if not poster:
        return None
    card = Path(poster).with_suffix(SUFFIX)
    return str(card) if card.is_file() else None


def cover(width, height, size=CARD_SIZE):
    """The smallest size with the image's aspect ratio that covers ``size``; never larger than the image."""
    scale = min(1., max(size[0] / width, size[1] / height))
    return max(1, round(width * scale)), max(1, round(height * scale))


def scaled(path, size=CARD_SIZE):
    """Decode ``path`` at about ``size``: (width, height, has_alpha, pixels) with rows packed (RGB or RGBA)."""
    from PIL import Image
    with Image.open(path) as image:
        image.draft('RGB', size)        # JPEG decodes straight at a reduced scale
        alpha = image.mode in ('RGBA', 'LA', 'PA') or 'transparency' in image.info
        image = image.convert('RGBA' if alpha else 'RGB')
        target = cover(image.width, image.height, size)
        if target != image.size:
            image = image.resize(target, Image.Resampling.LANCZOS, reducing_gap=2.)
        return image.width, image.height, alpha, image.tobytes()


def write_cards(assets):
    """Write NAME.card.jpg for every NAME.png poster at the top of ``assets``; returns how many."""
    from PIL import Image
    written = 0
    for poster in sorted(Path(assets).glob('*.png')):
        width, height, alpha, pixels = scaled(poster)
        image = Image.frombytes('RGBA' if alpha else 'RGB', (width, height), pixels).convert('RGB')
        image.save(poster.with_suffix(SUFFIX), quality=90, optimize=True)
        written += 1
    return written


if __name__ == '__main__':
    if len(sys.argv) != 2:
        sys.exit('usage: poster_cards.py ASSETS')
    print('poster cards: %d written' % write_cards(sys.argv[1]))
