"""Downscale screenshots for viewing: thumb.py <out.jpg> <png>... (side by side, 480 px wide each)."""
import sys

from PIL import Image

out, paths = sys.argv[1], sys.argv[2:]
images = [Image.open(p).convert("RGB") for p in paths]
images = [im.resize((480, round(480 * im.height / im.width))) for im in images]
sheet = Image.new("RGB", (480 * len(images), max(im.height for im in images)))
for i, im in enumerate(images):
    sheet.paste(im, (480 * i, 0))
sheet.save(out, quality=70)
print(out)
