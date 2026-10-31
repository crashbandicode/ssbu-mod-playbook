"""Contact sheet: grid.py <out.jpg> <cols> <width> <png>... (row-major)."""
import sys

from PIL import Image

out, cols, width, paths = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4:]
images = [Image.open(p).convert("RGB") for p in paths]
height = round(width * images[0].height / images[0].width)
rows = (len(images) + cols - 1) // cols
sheet = Image.new("RGB", (cols * width, rows * height))
for i, im in enumerate(images):
    sheet.paste(im.resize((width, height)), ((i % cols) * width, (i // cols) * height))
sheet.save(out, quality=70)
print(out)
