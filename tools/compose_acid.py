"""Acid Cathedral: late nineties hard techno, Chemical Brothers weight with Aphex
slither. 146 bpm, 31-EDO acid lines with real slides, five themes and a coda.

Channels: 0 acid and leads (31-EDO, glides), 1 stabs and pads, 2 sub and bass,
9 Wolf drums (36 kick, 38 snare, 40 perc, 42 closed hat, 44 open hat).
"""
import sys
sys.path.insert(0, "/tmp")
from compose_songs2 import Song, Tune, DIV, BB

T = Tune(31, "A")
s = Song()
Q, E, S16, S32 = DIV, DIV // 2, DIV // 4, DIV // 8
KICK, SNR, PERC, CHH, OHH = 36, 38, 40, 42, 44

def beat(bar, kick=4, snare=True, hats="16", open_off=True, perc=0, roll=0):
    b = bar * BB
    for i in range(kick):
        s.note(b + i * Q, 34, KICK * 100, 120, 9)
    if snare:
        for i in (1, 3):
            s.note(b + i * Q, 34, SNR * 100, 112, 9)
    if hats == "16":
        for t in range(0, BB, S16):
            if t % Q == 0:
                continue
            v = 78 if (t // S16) % 2 else 54
            s.note(b + t, 16, CHH * 100, v, 9)
    elif hats == "32":
        for t in range(0, BB, S32):
            s.note(b + t, 10, CHH * 100, 40 + (t // S32) % 5 * 8, 9)
    elif hats == "8":
        for t in range(0, BB, E):
            s.note(b + t, 20, CHH * 100, 70, 9)
    if open_off:
        for i in range(4):
            s.note(b + i * Q + E, 22, OHH * 100, 84, 9)
    for i in range(perc):
        s.note(b + (i * 2 + 1) * S16, 18, PERC * 100, 88, 9)
    for i in range(roll):
        s.note(b + 3 * Q + i * S32, 10, SNR * 100, 50 + i * 6, 9)

def acid(bar, tok, pattern, vel=104, octave=3, slides=(), cutoff_hi=False):
    """303 line: pattern is (degree, length in sixteenths), slides glide into the next"""
    root, iv = T.chord(tok)
    base = T.root_cents(tok, octave)
    t = bar * BB
    for i, (deg, ln) in enumerate(pattern):
        cents = base + T.c(iv[deg % len(iv)] + (T.edo if cutoff_hi and i % 5 == 4 else 0))
        dur = ln * S16
        nxt = pattern[(i + 1) % len(pattern)]
        gl = None
        if i in slides:
            gl = base + T.c(iv[nxt[0] % len(iv)])
        s.note(t, dur - 10, cents, vel, 0, glide_to=gl)
        t += dur

def sub(bar, tok, pattern, vel=112, octave=1):
    root, iv = T.chord(tok)
    base = (((T.key + root) % 12) + 12 * octave) * 100
    for i, step in enumerate(pattern):
        if step is None:
            continue
        s.note(bar * BB + i * E, E - 22, base + step * 100, vel, 2)

def stab(bar, tok, when=(0, 1.5, 2.5), vel=86, octave=4):
    base = T.root_cents(tok, octave)
    for w in when:
        for deg in T.chord(tok)[1][:2]:
            s.note(bar * BB + int(w * Q), int(0.4 * Q), base + T.c(deg), vel, 1)

def pad(bar, tok, vel=58, octave=4, bars=2):
    base = T.root_cents(tok, octave)
    for deg in T.chord(tok)[1][:2]:
        s.note(bar * BB, bars * BB - 60, base + T.c(deg), vel, 1)  # 31-EDO color

def cluster(bar, vel=46, octave=5):
    """Aphex interlude: stacked microtonal seconds that beat against each other."""
    base = T.root_cents("i", octave)
    for k in range(6):
        s.note(bar * BB, BB - 20, base + T.c(k), vel + (k % 3) * 4, 1)

# ---------------------------------------------------------------- the themes ---
A = ["i", "i", "bVII", "i"]
B = ["i", "bIII", "bVII", "bVI"]
ACID1 = [(0, 2), (0, 1), (2, 1), (1, 2), (0, 2), (2, 3), (1, 1), (2, 2)]
ACID2 = [(0, 1), (1, 1), (2, 1), (0, 1), (3, 2), (2, 1), (1, 1), (0, 4)]
ACID3 = [(2, 2), (1, 2), (0, 1), (1, 1), (2, 2), (3, 4), (0, 4)]

bar = 0

# theme 1: acid rave, cold open, kick from bar one
for rep in range(16):
    for tok in A:
        beat(bar, kick=4, hats="16", perc=1 if rep > 4 else 0, roll=8 if bar % 8 == 7 else 0)
        acid(bar, tok, ACID1, vel=100 + rep * 2, slides=(1, 4, 6))
        sub(bar, tok, (0, None, 0, 0, None, 5, 0, 3))
        if rep > 5:
            stab(bar, tok, vel=80)
        bar += 1

# theme 2: Chemical Brothers big beat, broken kick, heavier stabs
for rep in range(12):
    for tok in B:
        beat(bar, kick=3, hats="8", perc=2, roll=6 if bar % 4 == 3 else 0)
        acid(bar, tok, ACID2, vel=108, slides=(0, 3, 5), cutoff_hi=True)
        sub(bar, tok, (0, 0, 7, 0, 5, 3, 7, 10), vel=118)
        stab(bar, tok, when=(0, 1.5, 2.5, 3.5), vel=92)
        bar += 1

# theme 3: Aphex interlude, no kick, clusters and a thin acid line
for rep in range(8):
    for tok in A:
        cluster(bar, vel=44)
        acid(bar, tok, [(0, 3), (1, 3), (2, 2), (0, 8)], vel=70, octave=4, slides=(0, 2))
        if bar % 2 == 0:
            pad(bar, tok, vel=50, bars=2)
        for t in range(0, BB, E):
            s.note(bar * BB + t, 12, CHH * 100, 34 + (t // E) % 3 * 6, 9)
        bar += 1

# theme 4: drill, hats at thirty seconds, snare displaced
for rep in range(8):
    for tok in B:
        beat(bar, kick=4, hats="32", open_off=False, perc=3,
             roll=10 if bar % 2 == 1 else 0)
        for i in (1, 2):
            s.note(bar * BB + i * Q + S16, 18, SNR * 100, 90, 9)
        acid(bar, tok, ACID3, vel=112, octave=3, slides=(1, 4), cutoff_hi=True)
        sub(bar, tok, (0, None, 5, 0, 7, None, 3, 0), vel=120)
        bar += 1

# theme 5: the blowout, everything at once
for rep in range(22):
    for tok in B:
        beat(bar, kick=4, hats="16", perc=3, roll=10 if bar % 8 == 7 else 0)
        acid(bar, tok, ACID1 if rep % 2 == 0 else ACID3, vel=116, slides=(1, 3, 5, 6))
        sub(bar, tok, (0, 0, 5, 0, 7, 10, 7, 5), vel=122)
        stab(bar, tok, when=(0, 1.5, 2.5), vel=96)
        if rep >= 6:
            pad(bar, tok, vel=54, bars=1)
        bar += 1

# coda: kick and a single acid line slowing into the last chord
for rep in range(8):
    for tok in A:
        beat(bar, kick=4, hats="8", open_off=False)
        acid(bar, tok, [(0, 4), (2, 4), (1, 4), (0, 4)], vel=92, octave=3, slides=(1,))
        bar += 1
s.note(bar * BB, BB * 2 - 40, T.root_cents("i", 2), 60, 2)
s.note(bar * BB, BB * 2 - 40, T.root_cents("i", 3), 46, 0)

s.write("31edo Acid Cathedral", 146, 4)
print("bars:", bar, "mins:", round(bar * 4 * 60 / 146 / 60, 1))
