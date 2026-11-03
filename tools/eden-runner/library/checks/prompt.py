"""Native challenger/leave-prompt capture trigger, not semantic acceptance."""
from PIL import Image


def check(image_path, gate):
    with Image.open(image_path) as image:
        if image.size != (1280, 674):
            return False
        pixels = list(image.convert('RGB').crop(tuple(gate.get('roi', (300, 265, 980, 430)))).getdata())
    # The waiting-stage grid is neutral gray and must NOT trigger approval before
    # the challenger appears. Native dialogs have a warm beige (R/G above B).
    beige = sum(210 < r < 255 and 205 < g < 250 and 185 < b < 240 and r-b > 5 and g-b > 5
                for r, g, b in pixels)
    return bool(pixels) and beige / len(pixels) > .45
