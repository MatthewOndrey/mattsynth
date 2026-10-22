"""A long, additive, Glass-inspired piece in 31-EDO.

Structure: six cycles, each one an additive process. The motor starts as two notes
of the chord and grows to six, the pad holds the harmony underneath, the bass pulses
on the beat, and the last two cycles hand the top line to a slow melody with glides.
Everything is tuned in 31-EDO, so the thirds are just and the sevenths are harmonic.
"""
import sys
sys.path.insert(0, "/tmp")
from compose_songs2 import Song, Tune, DIV, BB

T = Tune(31, "A")
s = Song()

QUARTER = DIV
EIGHTH  = DIV // 2
SIXTEENTH = DIV // 4

def motor(bar, tok, phase, octave=3, vel=58):
    """16th note cell that grows with the phase, the Glass engine of the piece"""
    root, iv = T.chord(tok)
    base = T.root_cents(tok, octave)
    cell = [0, 2, 1, 3, 2, 1][:2 + phase]          # 2 to 6 notes, additive
    for i in range(16):
        deg = cell[i % len(cell)]
        step = iv[deg % len(iv)] + (T.edo if (i // len(cell)) % 2 else 0)
        s.note(bar * BB + i * SIXTEENTH, SIXTEENTH - 12, base + T.c(step),
               vel + (6 if i % 4 == 0 else 0), 0)

def bass(bar, tok, vel=84, octave=2):
    root, iv = T.chord(tok)
    base = T.root_cents(tok, octave)
    for i in range(4):
        step = iv[0] if i % 2 == 0 else iv[2 % len(iv)]
        s.note(bar * BB + i * QUARTER, QUARTER - 40, base + T.c(step), vel, 1)

def pad(bar, tok, bars=1, vel=52, octave=3):
    root, iv = T.chord(tok)
    base = T.root_cents(tok, octave)
    dur = bars * BB - 60
    s.note(bar * BB, dur, base + T.c(iv[0]), vel, 2)
    s.note(bar * BB, dur, base + T.c(iv[2]), vel - 6, 3)

def melody(bar, tok, phrase, vel=92, octave=4, glide=False):
    root, iv = T.chord(tok)
    base = T.root_cents(tok, octave)
    t = bar * BB
    for (deg, beats, gl) in phrase:
        step = iv[deg % len(iv)]
        cents = base + T.c(step)
        dur = int(beats * QUARTER)
        s.note(t, dur - 30, cents, vel, 3,
               glide_to=(base + T.c(iv[gl % len(iv)]) if glide and gl is not None else None))
        t += dur

# ---------------------------------------------------------------- the cycles ---
# each entry: (progression, bars per chord, phases, name)
cycles = [
    (["i", "VI", "III", "VII"], 3, 3, "first light"),
    (["i", "iv", "VII", "III"],  3, 4, "the long room"),
    (["VI", "III", "i", "v"],    3, 5, "rising water"),
    (["i", "II", "v", "VI"],     3, 6, "neutral air"),
    (["i", "v", "VI", "iv"],     3, 6, "the stair"),
    (["i", "iv", "VII", "i"],    4, 6, "coda"),
]

bar = 0
total_bars = 0
for ci, (prog, per, phases, name) in enumerate(cycles):
    # two passes of the progression, each pass denser than the last
    for rep in range(3):
        for tok in prog:
            for b in range(per):
                phase = min(phases - 1, (rep * per + b) * phases // per)
                motor(bar, tok, phase, octave=3 + (rep % 2), vel=54 + rep * 6)
                if b == 0:
                    pad(bar, tok, bars=per, vel=50 + rep * 4)
                if ci >= 3 or rep == 1:
                    bass(bar, tok, vel=78 + rep * 6)
                bar += 1
    total_bars = bar

# --------------------------------------------------------------- the melody ---
# it arrives late, over the fifth and sixth cycles, and glides between chord tones
mel_start = 150
bar = mel_start
phrases = [
    [(0, 4, 2), (2, 4, 1), (1, 4, 0), (2, 4, 2)],
    [(2, 2, 1), (1, 2, 0), (0, 4, 2), (2, 4, 1)],
    [(0, 3, 1), (1, 3, 2), (2, 2, 0), (0, 4, 1)],
    [(1, 4, 2), (2, 4, 0), (0, 8, 1)],
]
prog5 = ["i", "v", "VI", "iv"]
for rep in range(4):
    for tok in prog5:
        for b in range(2):
            if b == 0:
                melody(bar + b, tok, phrases[(rep + b) % len(phrases)], vel=88 + rep * 5)
        bar += 2

# ------------------------------------------------------------------- the coda --
# motor drops out, pad and melody remain, then a last single chord
for tok in ["i", "iv", "VII", "i"]:
    pad(bar, tok, bars=2, vel=46, octave=3)
    melody(bar, tok, [(0, 4, 2), (2, 4, 0)], vel=80)
    bar += 2
for _ in range(4):
    pad(bar, "i", bars=4, vel=42, octave=3)
    bar += 4
s.note(bar * BB, BB * 2 - 80, T.root_cents("i", 3), 44, 2)
s.note(bar * BB, BB * 2 - 80, T.root_cents("i", 3) + T.c(T.P5), 40, 3)

s.write("31edo Glass Cathedral", 92, 4)
print("bars:", bar + 2, " minutes:", round((bar + 2) * 4 * 60 / 92 / 60, 1))
