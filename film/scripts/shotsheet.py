#!/usr/bin/env python3
# Contact sheet of a v6 shot: one frame every STEP seconds, labelled with its footage time.
#   python3 scripts/shotsheet.py <shot> <out.jpg> [step] [bulk]
import json, sys, os
from PIL import Image, ImageDraw
shot, out = sys.argv[1], sys.argv[2]
step = float(sys.argv[3]) if len(sys.argv) > 3 else 1.0
bulk = sys.argv[4] if len(sys.argv) > 4 else os.environ.get('FILM_BULK', '/bulk3/runs/valio/film6')
m = json.load(open(os.path.join(os.path.dirname(__file__), '..', 'src', 'data6', f'shot-{shot}.json')))
fr = m['frames']
order = sorted(range(len(fr)), key=lambda i: fr[i])
def at(t):
    best = order[0]
    for i in order:
        if fr[i] <= t: best = i
        else: break
    return best
ts = [i * step for i in range(int(m['dur'] / step) + 1)]
tw = 260
ims = []
for t in ts:
    i = at(t)
    im = Image.open(os.path.join(bulk, 'public', m['dir'], f'f{i:05d}.jpg'))
    im = im.resize((tw, int(im.size[1] * tw / im.size[0])))
    d = ImageDraw.Draw(im); d.rectangle((0, 0, 70, 18), fill='black'); d.text((3, 3), f'{t:.1f}s', fill='yellow')
    ims.append(im)
cols = min(10, len(ims)); rows = (len(ims) + cols - 1) // cols
h = ims[0].size[1]
s = Image.new('RGB', (cols * tw, rows * h), 'white')
for k, im in enumerate(ims): s.paste(im, ((k % cols) * tw, (k // cols) * h))
s.save(out, quality=82)
print(out, s.size)
