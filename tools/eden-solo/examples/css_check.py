"""Is Mr. Game & Watch the fighter shown in the P1 panel of a character-select
screenshot?  Usage: css_check.py <screenshot.png> [--stats]
Prints `gamewatch` or `other` (and the measured numbers with --stats).
G&W's render is a flat black figure; every other fighter's portrait region
is mostly coloured.

Example from ssbu-gnw-judge-log (eden/css_check.py, 2026-10-30) of a cheap
screenshot marker: the runner polled this until it said `gamewatch` before
pressing A, instead of sleeping a fixed time. Write one like it for any
screen a route has to wait for; the region and threshold are this screen's."""
import sys

from PIL import Image

# The P1 panel's character render (1280x720 screenshots).
REGION = (70, 440, 330, 660)


def stats(path):
    image = Image.open(path).convert('RGB').crop(REGION)
    data = image.tobytes()
    pixels = [data[i:i + 3] for i in range(0, len(data), 3)]
    n = len(pixels)
    black = sum(1 for r, g, b in pixels if r < 45 and g < 45 and b < 45) / n
    grey = sum(1 for r, g, b in pixels if abs(r - g) < 18 and abs(g - b) < 18) / n
    mean = tuple(sum(p[i] for p in pixels) / n for i in range(3))
    return black, grey, mean


def is_gamewatch(black, grey):
    # Measured: G&W hovered or selected 0.35 black; Meta Knight 0.05.
    return black > 0.18


if __name__ == '__main__':
    black, grey, mean = stats(sys.argv[1])
    verdict = 'gamewatch' if is_gamewatch(black, grey) else 'other'
    if '--stats' in sys.argv:
        print(f'{verdict} black={black:.2f} grey={grey:.2f} mean={tuple(round(m) for m in mean)}')
    else:
        print(verdict)
