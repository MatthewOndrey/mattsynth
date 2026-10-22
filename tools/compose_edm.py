"""A long microtonal dance track for the Pod and the Rhythm Wolf.

Channels: 0 lead and arps (31-EDO), 1 chords and stabs (31-EDO), 2 bass (12-TET so
the Pod plays it exactly), 9 drums (Wolf: 36 kick, 38 snare, 40 perc, 42 closed hat,
44 open hat). The panel's A|B|C|D file routing sends 9 to C and 2 to B.
"""
import sys
sys.path.insert(0, "/tmp")
from compose_songs2 import Song, Tune, DIV, BB

T = Tune(31, "A")
s = Song()
Q = DIV
E = DIV // 2
S16 = DIV // 4

KICK, SNR, PERC, CHH, OHH = 36, 38, 40, 42, 44

def drums(bar, four=True, hats="16", snare=True, perc=0, fill=False):
    b = bar * BB
    if four:
        for i in range(4):
            s.note(b + i * Q, 40, KICK * 100, 118, 9)
    if snare:
        for i in (1, 3):
            s.note(b + i * Q, 40, SNR * 100, 108, 9)
    if hats:
        step = S16 if hats == "16" else E
        for t in range(0, BB, step):
            if t % Q == 0 and hats == "16":
                continue
            s.note(b + t, 30, (OHH if (t // E) % 4 == 3 else CHH) * 100,
                   70 if hats == "16" else 88, 9)
    for i in range(perc):
        s.note(b + (i * 2 + 1) * E + S16, 30, PERC * 100, 84, 9)
    if fill:
        for i in range(8):
            s.note(b + 3 * Q + i * S16, 30, SNR * 100, 60 + i * 7, 9)

def bass(bar, tok, pattern=(0, None, 0, 7, 0, None, 3, 0), vel=104, octave=2):
    root, iv = T.chord(tok)
    base = (((T.key + root) % 12) + 12 * octave) * 100
    for i, deg in enumerate(pattern):
        if deg is None:
            continue
        step = deg
        # bass is 12-TET on purpose: part B plays it exactly
        s.note(bar * BB + i * E, E - 30, base + step * 100, vel, 2)

def stab(bar, tok, vel=76, octave=4):
    root, iv = T.chord(tok)
    base = T.root_cents(tok, octave)
    for deg in iv[:3]:
        s.note(bar * BB + Q, Q - 20, base + T.c(deg), vel, 1)
        s.note(bar * BB + 3 * Q, Q - 20, base + T.c(deg), vel - 8, 1)

def arp(bar, tok, vel=72, octave=4, div=S16):
    root, iv = T.chord(tok)
    base = T.root_cents(tok, octave)
    seq = [0, 1, 2, 3, 2, 1, 3, 2]
    n = BB // div
    for i in range(n):
        deg = iv[seq[i % len(seq)] % len(iv)] + (T.edo if (i // 4) % 2 else 0)
        s.note(bar * BB + i * div, div - 18, base + T.c(deg), vel, 0)

def lead(bar, tok, notes, vel=96, octave=5):
    base = T.root_cents(tok, octave)
    t = bar * BB
    for (deg, beats) in notes:
        s.note(t, int(beats * Q) - 20, base + T.c(deg), vel, 0)
        t += int(beats * Q)

# --------------------------------------------------------------- arrangement ---
prog = ["i", "VI", "III", "VII"]
bar = 0

# intro: filtered arp and hats, no kick
for rep in range(6):
    for tok in prog:
        arp(bar, tok, vel=58, octave=4)
        for t in range(0, BB, E):
            s.note(bar * BB + t, 30, CHH * 100, 56 + (t // E) * 2, 9)
        bass(bar, tok, vel=76)
        bar += 1

# build 1
for rep in range(6):
    for tok in prog:
        arp(bar, tok, vel=74)
        bass(bar, tok, pattern=(0, 0, 7, 0, 5, 0, 3, 7), vel=96)
        if bar % 4 == 3:
            drums(bar, four=True, hats="8", fill=True)
        else:
            drums(bar, four=True, hats="16", snare=False)
        bar += 1

# drop 1
for rep in range(20):
    for tok in prog:
        drums(bar, four=True, hats="16", snare=True, perc=2)
        bass(bar, tok, pattern=(0, None, 0, 0, None, 7, 0, 5), vel=112)
        stab(bar, tok)
        if rep >= 1:
            lead(bar, tok, [(0, 1), (2, 1), (1, 2)])
        bar += 1

# breakdown: no kick, wide chords, microtonal lead
for rep in range(6):
    for tok in prog:
        for deg in T.chord(tok)[1]:
            s.note(bar * BB, BB - 40, T.root_cents(tok, 4) + T.c(deg), 60, 1)
        lead(bar, tok, [(2, 2), (1, 1), (0, 1)], vel=88, octave=5)
        arp(bar, tok, vel=54, octave=3, div=E)
        bar += 1

# build 2 with a riser feel: hats thicken, snare rolls
for rep in range(6):
    for tok in prog:
        drums(bar, four=(rep > 0), hats="16", snare=rep > 0, fill=(bar % 4 == 3))
        bass(bar, tok, vel=100)
        arp(bar, tok, vel=70)
        if rep > 0:
            for i in range(8):
                s.note(bar * BB + 3 * Q + i * S16, 24, SNR * 100, 40 + i * 10, 9)
        bar += 1

# drop 2, bigger
for rep in range(24):
    for tok in prog:
        drums(bar, four=True, hats="16", snare=True, perc=3, fill=(bar % 8 == 7))
        bass(bar, tok, pattern=(0, 0, 7, 0, 5, 3, 7, 10), vel=116)
        stab(bar, tok, vel=84)
        arp(bar, tok, vel=64)
        lead(bar, tok, [(0, 2), (2, 1), (3, 1)], vel=100, octave=5)
        bar += 1

# outro: strip to bass and hats, then a last chord
for rep in range(6):
    for tok in prog:
        drums(bar, four=True, hats="8", snare=False)
        bass(bar, tok, vel=92)
        bar += 1
for tok in ("i", "iv"):
    s.note(bar * BB, BB * 2 - 60, T.root_cents(tok, 3), 50, 0)
    s.note(bar * BB, BB * 2 - 60, T.root_cents(tok, 3) + T.c(T.P5), 44, 1)
    bar += 2

s.write("31edo Dancefloor Cathedral", 126, 4)
print("bars:", bar, "mins:", round(bar * 4 * 60 / 126 / 60, 1))
