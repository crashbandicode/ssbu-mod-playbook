"""Crop an Eden recording (a GIF from recording.start, or a directory of
PNG frames) into a small GIF: one shared palette, no dithering, so static
pixels stay identical and Pillow stores only the changed rectangle of each
frame.
Usage: encode.py <in.gif|frames_dir> <out.gif> <x,y,w,h> [fps] [colors]"""
import sys
from pathlib import Path
from PIL import Image, ImageSequence

source, out = Path(sys.argv[1]), sys.argv[2]
x, y, w, h = (int(v) for v in sys.argv[3].split(','))
fps = float(sys.argv[4]) if len(sys.argv) > 4 else 10
colors = int(sys.argv[5]) if len(sys.argv) > 5 else 128
if source.is_dir():
    raw = [Image.open(p) for p in sorted(source.glob('*.png'))]
else:
    raw = [frame.copy() for frame in ImageSequence.Iterator(Image.open(source))]
images = [im.convert('RGB').crop((x, y, x + w, y + h)) for im in raw]
mosaic = Image.new('RGB', (w, h * 3))
for i, k in enumerate((0, len(images) // 2, len(images) - 1)):
    mosaic.paste(images[k], (0, i * h))
palette = mosaic.quantize(colors=colors, method=Image.Quantize.MEDIANCUT)
frames = [im.quantize(palette=palette, dither=Image.Dither.NONE) for im in images]
frames[0].save(out, save_all=True, append_images=frames[1:], duration=round(1000 / fps),
               loop=0, disposal=1)
print(f'{len(frames)} frames {w}x{h} -> {Path(out).stat().st_size} bytes')
