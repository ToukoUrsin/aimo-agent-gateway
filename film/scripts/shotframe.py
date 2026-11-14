#!/usr/bin/env python3
# One frame of a v6 shot at footage time t, resized: python3 scripts/shotframe.py <shot> <t> <out.jpg> [width] [bulk]
import json, sys, os
from PIL import Image
shot, t, out = sys.argv[1], float(sys.argv[2]), sys.argv[3]
w = int(sys.argv[4]) if len(sys.argv) > 4 else 1200
bulk = sys.argv[5] if len(sys.argv) > 5 else os.environ.get('FILM_BULK', '/bulk3/runs/valio/film6')
m = json.load(open(os.path.join(os.path.dirname(__file__), '..', 'src', 'data6', f'shot-{shot}.json')))
fr = m['frames']; best = min(range(len(fr)), key=lambda i: (fr[i] > t, abs(fr[i] - t)))
im = Image.open(os.path.join(bulk, 'public', m['dir'], f'f{best:05d}.jpg'))
im.resize((w, int(im.size[1] * w / im.size[0]))).save(out, quality=85)
print(out, fr[best])
