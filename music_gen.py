#!/usr/bin/env python3
# music_gen.py - ABET desk digital music, stdlib-only synth (restored from the v11/v17 lineage).
# Arena-broadcast feel: ~110 BPM, dominant-7 progression, walking bass, backbeat snare, hats,
# Rhodes-ish comps, glock accents, tape hiss. Peak 0.55. Usage: python music_gen.py out.wav
import math, random, struct, sys, wave

SR = 44100
BPM = 110
BEAT = 60.0 / BPM
BARS = 30                      # 30-bar form => ~65.5s at 110 BPM
DUR = BARS * 4 * BEAT

def env(i, n, a=0.01, r=0.06):
    t = i / n
    if t < a: return t / a
    if t > 1 - r: return (1 - t) / r
    return 1.0

def add(buf, start_s, dur_s, freq, amp, wave_fn=math.sin, detune=0.0):
    s0 = int(start_s * SR); n = int(dur_s * SR)
    if s0 >= len(buf): return
    n = min(n, len(buf) - s0)
    for i in range(n):
        t = i / SR
        v = wave_fn(2 * math.pi * freq * t)
        if detune:
            v = 0.5 * v + 0.5 * wave_fn(2 * math.pi * freq * (1 + detune) * t)
        buf[s0 + i] += v * amp * env(i, n)

def add_noise(buf, start_s, dur_s, amp, lp=0.15, seed=1):
    rng = random.Random(seed); s0 = int(start_s * SR); n = int(dur_s * SR)
    if s0 >= len(buf): return
    n = min(n, len(buf) - s0)
    prev = 0.0
    for i in range(n):
        white = rng.uniform(-1, 1)
        prev = prev * (1 - lp) + white * lp      # cheap lowpass for body
        buf[s0 + i] += (white * 0.6 + prev * 1.4) * amp * env(i, n, 0.005, 0.4)

def main(out):
    rng = random.Random()
    n_total = int(DUR * SR)
    buf = [0.0] * n_total
    # progression: Am7 - Fmaj7 - Cmaj7 - G7 (arena cadence), two beats per chord
    chords = [("Am7", [220.00, 261.63, 329.63, 392.00]),
              ("Fmaj7", [174.61, 220.00, 261.63, 329.63]),
              ("Cmaj7", [130.81, 164.81, 196.00, 246.94]),
              ("G7", [196.00, 246.94, 293.66, 349.23])]
    bass_roots = {"Am7": 55.0, "Fmaj7": 43.65, "Cmaj7": 32.70, "G7": 49.0}
    t = 0.0
    prev_root = None
    bar = 0
    while t < DUR - 0.1:
        for ci, (cname, notes) in enumerate(chords):
            if t >= DUR - 0.1: break
            # pad chord (Rhodes-ish: detuned sine pair)
            for f in notes:
                add(buf, t, 2 * BEAT * 0.96, f, 0.055, detune=0.004)
            # walking bass: root, fifth, third, approach
            root = bass_roots[cname]
            nxt = bass_roots[chords[(ci + 1) % 4][0]]
            walk = [root, root * 1.5, root * 1.25, nxt * (1.0 if rng.random() < 0.5 else 1.06)]
            for bi, bf in enumerate(walk):
                add(buf, t + bi * (BEAT / 2), BEAT / 2 * 0.9, bf, 0.14, detune=0.002)
            # comp stabs on the 'and' of 2
            if ci % 2 == 1:
                for f in notes[1:]:
                    add(buf, t + BEAT * 1.5, 0.22, f, 0.05)
            t += 2 * BEAT
        # drums per bar
        bs = bar * 4 * BEAT
        if bs >= DUR: break
        # kick on 1 (+ and-of-3 every other bar)
        add(buf, bs, 0.16, 55.0, 0.5, wave_fn=lambda x: math.sin(x) * math.exp(-x * 3))
        if bar % 2 == 0:
            add(buf, bs + 2.5 * BEAT, 0.14, 52.0, 0.4, wave_fn=lambda x: math.sin(x) * math.exp(-x * 3))
        # snare 2 & 4
        add_noise(buf, bs + BEAT, 0.14, 0.28, 0.5, seed=bar * 2)
        add_noise(buf, bs + 3 * BEAT, 0.14, 0.28, 0.5, seed=bar * 2 + 1)
        # swung hats 8ths
        for h in range(8):
            ht = bs + h * BEAT / 2 + (0.03 if h % 2 else 0)
            add_noise(buf, ht, 0.05, 0.07, 0.9, seed=bar * 20 + h)
        # glock accents (every 4th bar, beats 1 and 3+)
        if bar % 4 == 3:
            g = notes[-1] * 4
            add(buf, bs, 0.5, g, 0.10)
            add(buf, bs + 2 * BEAT, 0.5, notes[1] * 4, 0.08)
        bar += 1
    # tape hiss
    rng2 = random.Random(99)
    for i in range(0, n_total, 2):
        buf[i] += rng2.uniform(-1, 1) * 0.0035
    # normalize to peak 0.55
    peak = max(abs(v) for v in buf) or 1.0
    k = 0.55 / peak
    with wave.open(out, "w") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes(b"".join(struct.pack("<h", int(max(-1, min(1, v * k)) * 32767)) for v in buf))
    print("music ->", out, round(len(buf) / SR, 1), "s")

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "bed.wav")
