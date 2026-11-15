#!/usr/bin/env python3
"""Cut a person's reading of film 8's lines into the film's voice files.

    python3 scripts/own8.py RECORDING [--tempo 1.0] [--model medium.en]

RECORDING: any file ffmpeg reads (wav, m4a, mp4, mov, webm). The 18 lines (the Valio tab of the lines page, the same
lines as scripts/make-vo8.mjs) read in order, a pause between lines. A line that went wrong is said again from its
start; the last take of a line counts. Anything else said in between is ignored.

It has whisper (faster-whisper, on the CPU, a model already on the box) write down every word with its time, finds
each line in what was heard (numbers are compared as spoken words, a misheard word costs little), moves each cut into
the silence around the take, and writes <bulk>/public/audio/own8/<id>.wav and src/data8/vo.json: for every line the
file, its length and the time each caption chunk starts, which is what the scenes hang on. The computer voice's
vo.json is kept as src/data8/vo-tts.json (the lines are always read from there). own8.sh does all of it.

--tempo: play the takes that much faster (own8.sh raises it, to 1.10 at most, only if the film would pass 1:59).
"""
import argparse
import difflib
import json
import os
import re
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

FILM = Path(__file__).resolve().parents[1]
BULK = Path(os.environ.get("FILM_BULK", "/bulk3/runs/valio/film6"))
OUT = BULK / "public" / "audio" / "own8"
D8 = FILM / "src" / "data8"
VOICE_LUFS = -22.4  # the level of the computer voice's takes, so the mix with the music stays as it is
GOOD = 0.75  # share of a line's words that must be heard for a take to count
ONES = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split()
TENS = "twenty thirty forty fifty sixty seventy eighty ninety".split()


def stop(msg):
    sys.exit(f"own-voice: {msg}")


def number_words(n):
    """180 -> one hundred eighty, 1500 -> one thousand five hundred: the words a person says."""
    if n < 20:
        return [ONES[n]]
    if n < 100:
        return [TENS[n // 10 - 2]] + ([ONES[n % 10]] if n % 10 else [])
    if n < 1000:
        return [ONES[n // 100], "hundred"] + (number_words(n % 100) if n % 100 else [])
    if n < 100000:
        return number_words(n // 1000) + ["thousand"] + (number_words(n % 1000) if n % 1000 else [])
    return [ONES[int(d)] for d in str(n)]


def tokens(word):
    """One written word as the words a person says: '0.78' -> zero point seven eight, "it's" -> its."""
    w = word.strip().lower().replace("’", "'").replace("%", " percent ")
    w = re.sub(r"(?<=\d),(?=\d{3})", "", w)  # 1,500 is one number
    w = re.sub(r"^[^a-z0-9.]+|[^a-z0-9]+$", "", w)
    out = []
    for part in re.split(r"[\s\-–—/]+", w):
        if not part:
            continue
        m = re.fullmatch(r"(\d*)\.(\d+)", part)
        if m:
            if m.group(1):
                out += number_words(int(m.group(1)))
            out += ["point"] + [ONES[int(d)] for d in m.group(2)]
        elif re.fullmatch(r"\d+", part):
            out += number_words(int(part))
        else:
            part = re.sub(r"[^a-z0-9]", "", part)
            if part:
                out.append(part)
    return out


def similar(a, b, cache={}):
    if a == b:
        return 1.0
    k = (a, b)
    if k not in cache:
        cache[k] = difflib.SequenceMatcher(None, a, b).ratio()
    return cache[k]


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        stop(f"{cmd[0]} failed: {r.stderr.strip()[-400:]}")
    return r


def load(model):
    os.environ.setdefault("HF_HUB_OFFLINE", "1")  # the models are on the box; nothing is downloaded
    from faster_whisper import WhisperModel

    # the box holds medium.en, medium, large-v3 and tiny.en whole; the first of them that loads is used
    for name in [model] + [x for x in ("medium.en", "medium", "large-v3", "tiny.en") if x != model]:
        try:
            return WhisperModel(name, device="cpu", compute_type="int8", cpu_threads=4)
        except Exception as e:  # not on the box, or its files are incomplete
            print(f"own-voice: whisper model {name} is not usable here ({type(e).__name__}), trying the next")
    stop("no whisper model on this box could be loaded")


def listen(m, audio, t_off=0.0):
    """Every word whisper hears in these samples, as spoken tokens with times (t_off: where the samples start)."""
    # the samples are handed over as an array: the PyAV on this box is too new for faster-whisper's own file reader
    segs, _ = m.transcribe(
        audio, language="en", word_timestamps=True, vad_filter=True, vad_parameters={"min_silence_duration_ms": 400},
        condition_on_previous_text=False, beam_size=5,
        initial_prompt="Aimo Agent Gateway, built for Valio Aimo, a food wholesaler. Kitchens, the cook, the web shop's chat, Nutella for a nut-free kitchen, litres of milk, contract share, allergen rule, AI models.")
    heard = []
    for s in segs:
        for w in s.words or []:
            ts = tokens(w.word)
            if not ts:
                continue
            # a written number is several spoken words: they share its time by their length
            total = sum(len(t) for t in ts)
            # whisper stretches a word back over a pause, or over words it skipped: no word is that long, and the
            # word itself sits at the end of the time it was given
            t0 = max(w.start, w.end - (0.45 + 0.12 * total))
            for t in ts:
                t1 = t0 + (w.end - t0) * len(t) / total
                heard.append({"t": t, "s": round(t_off + t0, 3), "e": round(t_off + t1, 3), "raw": w.word.strip()})
                t0 = t1
    return heard


def unheard(db, thr, heard):
    """Stretches of voice in which whisper wrote down no word. It tends to skip words that were just said, which
    is exactly what a second take of a line is."""
    n = len(db)
    covered = np.zeros(n, dtype=bool)
    for h in heard:
        covered[max(0, int(h["s"] * 100) - 15):min(n, int(h["e"] * 100) + 15)] = True
    free = (db >= thr) & ~covered
    out, a, last, voiced = [], None, 0, 0
    for i in range(n + 1):
        on = i < n and free[i]
        if a is not None and (i == n or covered[i] or (not on and i - last > 35)):
            if voiced >= 60:  # 0.6 s of voice nobody wrote down
                out.append((a / 100, (last + 1) / 100))
            a = None
        if on:
            if a is None:
                a, voiced = i, 0
            last, voiced = i, voiced + 1
    return out


def hear(wav16, model, db, thr):
    """The whole recording, then once more every stretch of voice the first pass skipped."""
    with wave.open(str(wav16)) as w:
        sr = w.getframerate()
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    m = load(model)
    heard = listen(m, audio)
    again = unheard(db, thr, heard)[:12]
    for a, b in again:
        a0, b0 = max(0.0, a - 0.3), min(len(audio) / sr, b + 0.3)
        more = [h for h in listen(m, audio[int(a0 * sr):int(b0 * sr)], a0) if h["e"] > a - 0.15 and h["s"] < b + 0.15]
        heard += more
    heard.sort(key=lambda h: h["s"])
    return heard, len(again)


def fit(line, heard):
    """Every place the whole line fits into what was heard: [(score, first heard index, last, pairs)].
    pairs: for each word of the line the index of the heard word it stands on, or None."""
    n, m = len(line), len(heard)
    GAP = -1.0  # a word of the line that was not heard
    EXTRA = -0.8  # a heard word the line does not have: whisper adds one more easily than it drops one
    S = np.zeros((n + 1, m + 1))
    B = np.zeros((n + 1, m + 1), dtype=np.int8)  # 0 start, 1 diagonal, 2 line word not heard, 3 extra heard word
    for j in range(1, n + 1):
        S[j][0] = S[j - 1][0] + GAP
        B[j][0] = 2
        for k in range(1, m + 1):
            sub = max(-0.6, 2 * similar(line[j - 1], heard[k - 1]["t"]) - 1)
            best, how = S[j - 1][k - 1] + sub, 1
            if S[j - 1][k] + GAP > best:
                best, how = S[j - 1][k] + GAP, 2
            if S[j][k - 1] + EXTRA > best:
                best, how = S[j][k - 1] + EXTRA, 3
            S[j][k], B[j][k] = best, how
    out = []
    row = S[n]
    for k in range(1, m + 1):
        if row[k] < 0.4 * n:
            continue
        if row[k] < row[k - 1] or (k < m and row[k] < row[k + 1]):
            continue  # not a peak
        j, kk = n, k
        pairs = [None] * n
        while j > 0:
            how = B[j][kk]
            if how == 1:
                pairs[j - 1] = kk - 1
                j, kk = j - 1, kk - 1
            elif how == 2:
                j -= 1
            else:
                kk -= 1
        got = [p for p in pairs if p is not None]
        if not got:
            continue
        hits = sum(1 for j, p in enumerate(pairs) if p is not None and similar(line[j], heard[p]["t"]) >= 0.8)
        # how many words at the start and at the end of the line were not clearly heard
        sure = [p is not None and similar(line[j], heard[p]["t"]) >= 0.8 for j, p in enumerate(pairs)]
        lead = sure.index(True) if True in sure else n
        trail = sure[::-1].index(True) if True in sure else n
        out.append({"q": hits / n, "a": got[0], "b": got[-1], "pairs": pairs, "lead": lead, "trail": trail})
    return out


def envelope(wav16):
    with wave.open(str(wav16)) as w:
        sr = w.getframerate()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    hop = sr // 100  # 10 ms
    n = len(x) // hop
    rms = np.sqrt((x[: n * hop].reshape(n, hop) ** 2).mean(axis=1) + 1e-12)
    db = 20 * np.log10(rms)
    floor = np.percentile(db, 10)
    speech = np.percentile(db, 95)
    # quiet = nearer to the room than to the voice
    return db, floor + 0.3 * (speech - floor), len(x) / sr


def snap(db, thr, t, lo, hi, step, need=6):
    """From t, walk (step -1 back, +1 on) while the recording is louder than the room; stay inside lo..hi.
    need: how many 10 ms frames of room end the walk (6: the gap before a word; 25: a real pause)."""
    i = int(round(t * 100))
    quiet = 0
    while lo * 100 <= i + step <= min(hi * 100, len(db) - 1):
        i += step
        quiet = quiet + 1 if db[i] < thr else 0
        if quiet >= need:
            return (i - step * (need - 1)) / 100
    return i / 100


def loud_edge(db, thr, a, b, step):
    """The first (step +1, from a) or last (step -1, from b) moment between a and b where the recording is louder
    than the room for 30 ms: where the voice really starts or stops. None if it never is."""
    lo, hi = max(0, int(round(a * 100))), min(len(db) - 1, int(round(b * 100)))
    run = 0
    for i in (range(lo, hi + 1) if step > 0 else range(hi, lo - 1, -1)):
        run = run + 1 if db[i] >= thr else 0
        if run >= 3:
            return (i - step * 2) / 100
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("recording")
    ap.add_argument("--tempo", type=float, default=1.0)
    ap.add_argument("--model", default="medium.en")
    a = ap.parse_args()
    src = Path(a.recording).resolve()
    if not src.is_file():
        stop(f"no file {src}")
    if not 1.0 <= a.tempo <= 1.10:
        stop("--tempo must be between 1.0 and 1.10")
    # the lines as the film has them: the computer voice's vo.json, kept aside the first time
    tts = D8 / "vo-tts.json"
    if not tts.is_file():
        tts.write_text((D8 / "vo.json").read_text())
    base = json.loads(tts.read_text())
    ids = list(base["lines"])
    chunks = [[w["w"] for w in base["lines"][i]["words"]] for i in ids]
    lines = [" ".join(c) for c in chunks]
    spoken, chunk_of = [], []
    for cs in chunks:
        sp, co = [], []
        for k, c in enumerate(cs):
            for w in c.split():
                for t in tokens(w):
                    sp.append(t)
                    co.append(k)
        spoken.append(sp)
        chunk_of.append(co)
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        wav16 = Path(tmp) / "rec16.wav"
        run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav16)])
        db, thr, length = envelope(wav16)
        # what whisper heard is kept next to the takes, so a second pass (another tempo) does not listen again
        st = src.stat()
        cache = OUT / "heard.json"
        key = f"8|{src}|{st.st_size}|{int(st.st_mtime)}|{a.model}"
        old = json.loads(cache.read_text()) if cache.is_file() else {}
        if old.get("key") == key:
            heard, again = old["heard"], old.get("again", 0)
        else:
            heard, again = hear(wav16, a.model, db, thr)
            cache.write_text(json.dumps({"key": key, "heard": heard, "again": again}))
        if not heard:
            stop("whisper heard no words in the recording")
        # the last good take of each line, working back from the last line
        takes = [None] * len(lines)
        limit = len(heard)
        for i in range(len(lines) - 1, -1, -1):
            cands = [c for c in fit(spoken[i], heard) if c["b"] < limit]
            good = [c for c in cands if c["q"] >= GOOD]
            if not good:
                best = max(cands, key=lambda c: c["q"], default=None)
                near = " ".join(h["raw"] for h in heard[max(0, limit - 30):limit])
                stop(f"line {i + 1} was not found before line {i + 2 if i + 1 < len(lines) else 'the end'}.\n  the line: {lines[i]}\n"
                     f"  best match: {0 if not best else round(100 * best['q'])} % of its words\n  heard there: ...{near}\n"
                     "  Read the line again at the end of a new recording, or record all of them again.")
            c = max(good, key=lambda c: c["b"])
            c["takes"] = len(good)
            takes[i] = c
            limit = c["a"]
        # cut points, in the silence around each take
        meta = []
        for i, c in enumerate(takes):
            first, last = heard[c["a"]], heard[c["b"]]
            before = heard[c["a"] - 1]["e"] if c["a"] > 0 else 0.0
            after = heard[c["b"] + 1]["s"] if c["b"] + 1 < len(heard) else length
            lead, trail = c["lead"], c["trail"]
            lo = max(before, first["s"] - 0.6) if not lead else max(0.0, first["s"] - 0.6 - min(2.0, 0.7 * lead))
            hi = min(after, last["e"] + 0.9) if not trail else min(length, last["e"] + 0.9 + min(2.0, 0.7 * trail))
            t0 = snap(db, thr, first["s"] + 0.05, lo, first["s"] + 0.05, -1, 25 if lead else 6)
            t1 = snap(db, thr, last["e"] - 0.05, last["e"] - 0.05, hi, +1, 25 if trail else 6)
            if lead:
                before = min(before, t0)
            if trail:
                after = max(after, t1)
            on = loud_edge(db, thr, t0, first["e"], +1)
            off = loud_edge(db, thr, last["s"], t1, -1)
            t0 = max(before + 0.02 if c["a"] > 0 else 0.0, (t0 if on is None else on) - 0.08)
            t1 = min(after - 0.02 if c["b"] + 1 < len(heard) else length, (t1 if off is None else off + 0.01) + 0.14)
            # the time of every spoken word of the line: the heard word it stands on, or between its neighbours
            ws = []
            for j, p in enumerate(c["pairs"]):
                ws.append(None if p is None else [heard[p]["s"] - t0, heard[p]["e"] - t0])
            for j, w in enumerate(ws):
                if w is None:
                    prev = next((ws[k][1] for k in range(j - 1, -1, -1) if ws[k]), 0.0)
                    nxt = next((ws[k][0] for k in range(j + 1, len(ws)) if ws[k]), t1 - t0)
                    ws[j] = [prev, max(prev, nxt)]
            if on is not None:
                ws[0][0] = max(ws[0][0], on - t0)
            if off is not None:
                ws[-1][1] = min(ws[-1][1], off + 0.01 - t0)
            last_e = 0.0
            for w in ws:  # never backwards
                w[0] = max(last_e, min(w[0], t1 - t0))
                w[1] = max(w[0], min(w[1], t1 - t0))
                last_e = w[0]
            got = [heard[q] for q in c["pairs"] if q is not None]
            pause = max((y["s"] - x["e"] for x, y in zip(got, got[1:])), default=0.0)
            meta.append({"t0": t0, "t1": t1, "ws": ws, "q": c["q"], "takes": c["takes"], "pause": pause})
        # one gain for all takes: the level of the computer voice
        concat = Path(tmp) / "all.wav"
        sel = "+".join(f"between(t,{m['t0']:.3f},{m['t1']:.3f})" for m in meta)
        run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(src), "-vn", "-ac", "1", "-ar", "48000", "-af", f"aselect='{sel}',asetpts=N/SR/TB", str(concat)])
        r = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-i", str(concat), "-af", "ebur128", "-f", "null", "-"], capture_output=True, text=True)
        m = re.findall(r"I:\s+(-?[\d.]+) LUFS", r.stderr)
        gain = max(-12.0, min(24.0, VOICE_LUFS - float(m[-1]))) if m else 0.0
        out, report = {}, []
        stamp = f"{int(st.st_mtime) % 100000}-{int(a.tempo * 100)}"  # a new name per recording and tempo: the renderer caches files by name
        for i, (lid, mt) in enumerate(zip(ids, meta)):
            name = f"audio/own8/{lid}-{stamp}.wav"
            dur = mt["t1"] - mt["t0"]
            af = f"highpass=f=70,volume={gain:.2f}dB,afade=t=in:d=0.02,afade=t=out:st={max(0, dur - 0.06):.3f}:d=0.06"
            if a.tempo != 1.0:
                af += f",atempo={a.tempo:.4f}"
            run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-ss", f"{mt['t0']:.3f}", "-t", f"{dur:.3f}", "-i", str(src), "-vn", "-ac", "1", "-ar", "48000", "-af", af, "-c:a", "pcm_s16le", str(BULK / "public" / name)])
            k = a.tempo
            words = []
            for ci, ctext in enumerate(chunks[i]):
                idx = [j for j, cc in enumerate(chunk_of[i]) if cc == ci]
                s = mt["ws"][idx[0]][0] if idx else (words[-1]["e"] if words else 0.0)
                e = mt["ws"][idx[-1]][1] if idx else s
                words.append({"w": ctext, "s": round((0.0 if ci == 0 else s) / k, 3), "e": round(e / k, 3)})
            out[lid] = {"id": lid, "text": lines[i], "file": name, "dur": round(dur / k, 3), "words": words}
            report.append({"id": lid, "from": round(mt["t0"], 2), "to": round(mt["t1"], 2), "dur": round(dur / k, 2), "heard": round(mt["q"], 2), "takes": mt["takes"], "pause": round(mt["pause"], 2)})
    (D8 / "vo.json").write_text(json.dumps({**base, "voice": f"own ({src.name}, tempo {a.tempo})", "lines": out}, indent=1))
    (OUT / "takes.json").write_text(json.dumps({"recording": str(src), "tempo": a.tempo, "model": a.model, "gain_db": round(gain, 2), "lines": report}, indent=1))
    print(f"own8: {src.name}, {length:.0f} s, {len(heard)} words heard" + (f" ({again} stretches listened to twice)" if again else "")
          + f", takes {gain:+.1f} dB" + (f", tempo {a.tempo}" if a.tempo != 1.0 else "") + f", voice {sum(l['dur'] for l in out.values()):.1f} s")
    for i, l in enumerate(report):
        note = f"  ({l['takes']} takes, the last one used)" if l["takes"] > 1 else ""
        warn = "  CHECK: some words were heard differently" if l["heard"] < 0.9 else ""
        if l["pause"] > 2.5:
            warn += f"  CHECK: a pause of {l['pause']:.1f} s inside this take"
        print(f"  line {i + 1:2d} {l['id']:4s} {l['from']:7.2f} to {l['to']:7.2f} s  {l['dur']:5.2f} s  {round(100 * l['heard']):3d} % of its words heard{note}{warn}")
    print(f"own8: wrote {OUT}/ and {D8 / 'vo.json'}")


if __name__ == "__main__":
    main()
