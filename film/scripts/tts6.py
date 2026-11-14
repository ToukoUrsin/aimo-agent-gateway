#!/usr/bin/env python3
# Local voice (Kokoro-82M, on this machine): the fallback when the paid voice has no credit left.
# Reads a JSON job [{"out": path.wav, "text": spoken text}] on stdin; writes each missing wav (24 kHz mono).
#   echo '[...]' | HYPERFRAMES_PYTHON-venv/bin/python scripts/tts6.py [voice] [speed]
import json, os, sys
import soundfile as sf
from kokoro_onnx import Kokoro

voice = sys.argv[1] if len(sys.argv) > 1 else 'af_heart'
speed = float(sys.argv[2]) if len(sys.argv) > 2 else 1.05
root = os.path.expanduser('~/.cache/hyperframes/tts')
k = Kokoro(f'{root}/models/kokoro-v1.0.onnx', f'{root}/voices/voices-v1.0.bin')
for job in json.load(sys.stdin):
    if os.path.exists(job['out']):
        continue
    samples, sr = k.create(job['text'], voice=voice, speed=speed, lang='en-us')
    sf.write(job['out'] + '.tmp.wav', samples, sr)
    os.replace(job['out'] + '.tmp.wav', job['out'])
    print(f"{len(samples) / sr:5.2f} s  {job['text']}", flush=True)
