"""Neon Marigold: a new dance track, not a rework of anything.

Key D, 128 bpm, 31-EDO lead and stabs, 12-TET bass on the Pod, Wolf drums on 36,
38, 40, 42, 44. Hook is a three note figure that moves through four chords and
comes back a fourth higher in the last drop.
"""
import sys
sys.path.insert(0, "/tmp")
from compose_songs2 import Song, Tune, DIV, BB

T = Tune(31, "D")
s = Song()
Q, E, S16 = DIV, DIV // 2, DIV // 4
KICK, SNR, PERC, CHH, OHH = 36, 38, 40, 42, 44

def groove(bar, kick=True, snare=True, hats="off", clap=0, roll=False):
    b = bar * BB
    if kick:
        for t in (0, 2 * Q, 3 * Q):                 # three on the floor, not four
            s.note(b + t, 40, KICK * 100, 116, 9)
    if snare:
        s.note(b + Q, 40, SNR * 100, 110, 9)
        s.note(b + 3 * Q, 40, SNR * 100, 110, 9)
    for i in range(clap):
        s.note(b + (i + 1) * Q - S16, 26, PERC * 100, 92, 9)
    if hats == "off":
        for t in range(E, BB, Q):
            s.note(b + t, 24, CHH * 100, 74, 9)
    elif hats == "16":
        for t in range(0, BB, S16):
            if t % Q:
                s.note(b + t, 20, (OHH if (t // E) % 4 == 3 else CHH) * 100, 62, 9)
    elif hats == "8":
        for t in range(0, BB, E):
            s.note(b + t, 24, CHH * 100, 66, 9)
    if roll:
        for i in range(10):
            s.note(b + 3 * Q + i * (S16 // 2), 22, SNR * 100, 46 + i * 8, 9)

def sub(bar, tok, pattern, vel=110, octave=2):
    root, iv = T.chord(tok)
    base = (((T.key + root) % 12) + 12 * octave) * 100
    for i, step in enumerate(pattern):
        if step is None:
            continue
        s.note(bar * BB + i * E, E - 26, base + step * 100, vel, 2)

def hook(bar, tok, vel=98, octave=5, lift=0):
    """the three note figure, transposed by lift EDO steps"""
    base = T.root_cents(tok, octave)
    fig = [(0, 1.0), (2, 0.5), (1, 0.5), (0, 2.0)]
    t = bar * BB
    for deg, beats in fig:
        s.note(t, int(beats * Q) - 24, base + T.c(deg + lift), vel, 0)
        t += int(beats * Q)

def chord(bar, tok, vel=74, octave=4, dur=BB):
    base = T.root_cents(tok, octave)
    for deg in T.chord(tok)[1]:
        s.note(bar * BB, dur - 40, base + T.c(deg), vel, 1)

def penta(bar, tok, vel=68, octave=4):
    """sixteenth arp through the chord plus the 31-EDO neutral second"""
    base = T.root_cents(tok, octave)
    order = [0, 2, 4, 2, 1, 2, 4, 5]
    for i in range(16):
        deg = order[i % len(order)]
        s.note(bar * BB + i * S16, S16 - 16, base + T.c(deg), vel, 0)

prog = ["i", "bVII", "bVI", "bVII"]
bar = 0

# 1  intro: filtered arp, offbeat hats, no kick
for rep in range(5):
    for tok in prog:
        penta(bar, tok, vel=52 + rep * 4, octave=4)
        for t in range(E, BB, Q):
            s.note(bar * BB + t, 24, CHH * 100, 52 + rep * 4, 9)
        bar += 1

# 2  first groove, kick and sub, hook teased on the third pass
for rep in range(6):
    for tok in prog:
        groove(bar, hats="off", clap=1)
        sub(bar, tok, (0, None, 0, 0, None, 7, 0, 3))
        penta(bar, tok, vel=62)
        if rep >= 4:
            hook(bar, tok, vel=82, octave=5)
        bar += 1

# 3  drop one
for rep in range(22):
    for tok in prog:
        groove(bar, hats="16", clap=2, roll=(bar % 8 == 7))
        sub(bar, tok, (0, 0, 7, 0, 5, 0, 3, 10), vel=116)
        chord(bar, tok, vel=70)
        hook(bar, tok, vel=100)
        bar += 1

# 4  breakdown, beat drops, pads and the hook slowed
for rep in range(5):
    for tok in prog:
        chord(bar, tok, vel=64, dur=BB)
        hook(bar, tok, vel=78, octave=4)
        for t in range(E, BB, Q):
            s.note(bar * BB + t, 24, CHH * 100, 48, 9)
        bar += 1

# 5  build, hats sixteen, snare rolls, sub walks up in EDO steps
for rep in range(5):
    for tok in prog:
        groove(bar, kick=(rep > 1), hats="16", clap=2, roll=(rep > 2))
        sub(bar, tok, (0, 3, 5, 7, 5, 3, 1, 0), vel=100)
        penta(bar, tok, vel=60 + rep * 4)
        bar += 1

# 6  drop two, hook lifted a fourth, bass busier
for rep in range(24):
    for tok in prog:
        groove(bar, hats="16", clap=3, roll=(bar % 8 == 7))
        sub(bar, tok, (0, 0, 5, 0, 7, 5, 10, 7), vel=118)
        chord(bar, tok, vel=76)
        hook(bar, tok, vel=104, lift=T.M3 if rep >= 8 else 0)
        penta(bar, tok, vel=64)
        bar += 1

# 7  outro: strip back to kick, sub and hats
for rep in range(5):
    for tok in prog:
        groove(bar, hats="8", snare=False)
        sub(bar, tok, (0, None, 0, 0, None, 5, 0, 3), vel=96)
        bar += 1
s.note(bar * BB, BB - 60, T.root_cents("i", 3), 54, 0)
s.note(bar * BB, BB - 60, T.root_cents("i", 3) + T.c(T.P5), 46, 1)

s.write("31edo Neon Marigold", 128, 4)
print("bars:", bar, "mins:", round(bar * 4 * 60 / 128 / 60, 1))
