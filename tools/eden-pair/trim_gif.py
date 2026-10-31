"""trim_gif.py <in.gif> <out.gif> <start fraction> <end fraction> <step> <width>"""
import sys

from PIL import Image

src, dst = sys.argv[1], sys.argv[2]
start, end, step, width = float(sys.argv[3]), float(sys.argv[4]), int(sys.argv[5]), int(sys.argv[6])
im = Image.open(src)
n = im.n_frames
duration = im.info.get("duration", 100) * step
frames = []
for i in range(int(n * start), int(n * end), step):
    im.seek(i)
    frame = im.convert("RGB")
    frames.append(frame.resize((width, round(width * frame.height / frame.width))))
frames[0].save(dst, save_all=True, append_images=frames[1:], duration=duration, loop=0, optimize=True)
print(len(frames), "frames")
