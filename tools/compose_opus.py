import struct, os

DIV = 480
OUT = "/home/matto/midi"
os.makedirs(OUT, exist_ok=True)

def vlq(n):
    out = [n & 0x7f]; n >>= 7
    while n: out.insert(0, (n & 0x7f) | 0x80); n >>= 7
    return bytes(out)

class Song:
    def __init__(self):
        self.evs = []; self.bend = {}
    def _bend(self, t, ch, cents):
        c = max(-200, min(200, cents))
        v = int(round(c / 200.0 * 8192)) + 8192
        v = max(0, min(16383, v))
        if self.bend.get(ch) == v: return
        self.bend[ch] = v
        self.evs.append((t, 1, ch, bytes([0xE0 | ch, v & 0x7f, (v >> 7) & 0x7f])))
    def note(self, t, dur, cents, vel, ch=0, glide_to=None):
        note = max(0, min(127, int(round(cents / 100.0))))
        base = note * 100
        self._bend(t, ch, cents - base)
        self.evs.append((t, 2, ch, bytes([0x90 | ch, note, max(1, min(127, int(vel)))])))
        if glide_to is not None:
            for i in range(1, 7):
                f = i / 6.0
                self._bend(t + int(dur * f), ch, cents + (glide_to - cents) * f - base)
        self.evs.append((t + dur, 0, ch, bytes([0x80 | ch, note, 0])))
    def poly(self):
        ev = sorted([(e[0], 1) for e in self.evs if e[1] == 2] + [(e[0], -1) for e in self.evs if e[1] == 0])
        cur = mx = 0
        for _, d in ev:
            cur += d; mx = max(mx, cur)
        return mx
    def write(self, name, bpm):
        self.evs.sort(key=lambda e: (e[0], e[1]))
        body = vlq(0) + b"\xff\x03" + vlq(len(name)) + name.encode()
        body += vlq(0) + b"\xff\x51\x03" + struct.pack(">I", int(60000000 / bpm))[1:]
        for ch in range(4):
            body += vlq(0) + bytes([0xB0 | ch, 101, 0]) + vlq(0) + bytes([0xB0 | ch, 100, 0])
            body += vlq(0) + bytes([0xB0 | ch, 6, 2]) + vlq(0) + bytes([0xB0 | ch, 38, 0])
        last = 0
        for (t, _, _, d) in self.evs:
            body += vlq(max(0, t - last)) + d
            last = t
        body += vlq(0) + b"\xff\x2f\x00"
        head = b"MThd" + struct.pack(">IHHH", 6, 0, 1, DIV)
        data = head + b"MTrk" + struct.pack(">I", len(body)) + body
        open(os.path.join(OUT, name.replace(" ", "-").lower() + ".mid"), "wb").write(data)
        bars = self.evs[-1][0] / (DIV * 4)
        print("%-24s %6dB  max %2d voices  %4.0f bars  ~%4.0fs" %
              (name, len(data), self.poly(), bars, bars * 4 * 60 / bpm))

NOTE = {"C":0,"Db":1,"D":2,"Eb":3,"E":4,"F":5,"Gb":6,"G":7,"Ab":8,"A":9,"Bb":10,"B":11}
ROM = {"I":0,"II":2,"III":4,"IV":5,"V":7,"VI":9,"VII":11}

class T:
    def __init__(self, edo, key):
        self.edo = edo; self.step = 1200.0 / edo; self.key = NOTE[key]
        self.M3 = round(386.31 / self.step); self.m3 = round(315.64 / self.step)
        self.P5 = round(701.96 / self.step); self.m7 = round(968.83 / self.step)
        self.M2 = round(203.91 / self.step); self.M6 = round(884.36 / self.step)
    def c(self, st): return st * self.step
    def triad(self, tok):
        flat = 0
        while tok and tok[0] in "b#":
            flat += -1 if tok[0] == "b" else 1; tok = tok[1:]
        roman = ""
        while tok and tok[0] in "IViv":
            roman += tok[0]; tok = tok[1:]
        minor = roman.islower()
        root = (ROM[roman.upper()] + flat) % 12
        return root, [0, self.m3 if minor else self.M3, self.P5]
    def root_cents(self, tok, octave):
        r, _ = self.triad(tok)
        return (((self.key + r) % 12) + 12 * octave) * 100

BAR = DIV * 4
STEP = DIV // 2          # one 8th, the motor runs on eighths here

s = Song()
T1 = T(31, "D")          # 31-EDO: pure fifths, 7-limit color

def bass_motor(bar, tok, vel=86):
    # half notes: the motor still churns, but each note is not another tail in
    # the pool, and eleven tails of a long-release patch is what overloads the block
    b = T1.root_cents(tok, 2)
    for beat in (0, 2):
        s.note(bar * BAR + beat * DIV, int(DIV * 1.7), b, vel + (4 if beat == 0 else 0), 2)

def sequence(bar, tok, vel=62, octave=3, pat=(0, 2, 1, 2, 0, 2, 1, 3), steps=8, ch=3):
    root, iv = T1.triad(tok)
    base = T1.root_cents(tok, octave)
    for i in range(steps):
        st = iv[pat[i % len(pat)] % len(iv)]
        accent = 10 if i % 4 == 0 else (4 if i % 2 == 0 else 0)
        s.note(bar * BAR + i * STEP, int(STEP * 0.7), base + T1.c(st), vel + accent, ch)

def pad(bar, tok, bars=8, vel=48):
    root, iv = T1.triad(tok)
    base = T1.root_cents(tok, 3)
    s.note(bar * BAR, bars * BAR - 60, base, vel + 6, 1)

def lead(bar, tok, vel=96, transpose=0, octave=5):
    root, iv = T1.triad(tok)
    base = T1.root_cents(tok, octave) + T1.c(transpose)
    figure = [(0, .5, 0), (.5, .5, 2), (1, .5, 1), (1.5, .5, 2),
              (2, 1, 3), (3, .5, 2), (3.5, .5, 1)]
    for (beat, dur, deg) in figure:
        s.note(bar * BAR + int(beat * DIV), int(dur * DIV), base + T1.c(iv[deg % len(iv)]),
               vel, 0, (base + T1.c(iv[deg % len(iv)])) - 39 if deg == 3 else None)

# --- form: layers arrive one at a time, break down, then return thicker -------
# 0-16   motor bass alone, then a single-note pulse
for bar, tok in [(b, "im" if (b // 8) % 2 == 0 else "bVIM") for b in range(16)]:
    bass_motor(bar, tok, 78)
    if bar >= 4:
        sequence(bar, tok, 44, 4, pat=(0, 0, 0, 0, 0, 0, 0, 0))

# 16-32  add the arpeggio
for bar, tok in [(b, "im" if (b // 8) % 2 == 0 else "bVIM") for b in range(16, 32)]:
    bass_motor(bar, tok, 82)
    sequence(bar, tok, 54, 3)

# 32-48  add the pad, chords change every eight bars
for k in range(2):
    tok = "im" if k % 2 == 0 else "bVIM"
    pad(32 + k * 8, tok, 8, 46)
for bar in range(32, 48):
    tok = "im" if (bar // 8) % 2 == 0 else "bVIM"
    bass_motor(bar, tok, 84)
    sequence(bar, tok, 56, 3)
    sequence(bar, tok, 40, 4, pat=(2, 1, 2, 3, 2, 1, 2, 0))

# 48-64  the theme appears
for bar in range(48, 64):
    tok = "im" if (bar // 8) % 2 == 0 else "bVIM"
    bass_motor(bar, tok, 86)
    sequence(bar, tok, 58, 3)
    if (bar - 48) % 4 == 0:
        lead(bar, tok, 94)
        lead(bar + 2, tok, 88, transpose=T1.M3)

# 64-80  full: double the sequence an octave up
for bar in range(64, 80):
    tok = "im" if (bar // 8) % 2 == 0 else "bVIM"
    bass_motor(bar, tok, 90)
    sequence(bar, tok, 62, 3)
    if bar % 4 == 0:
        lead(bar, tok, 100)

# 80-96  breakdown: pad out, pulse and a bare melody
for bar in range(80, 96):
    tok = "im" if (bar // 8) % 2 == 0 else "bVIM"
    bass_motor(bar, tok, 72)
    sequence(bar, tok, 40, 4)
    if bar % 4 == 2:
        lead(bar, tok, 84, transpose=-T1.P5)

# 96-128 return, transposed up five 31-EDO steps, everything on
for bar in range(96, 128):
    tok = "im" if (bar // 8) % 2 == 0 else "bVIM"
    bass_motor(bar, tok, 92)
    sequence(bar, tok, 64, 3)
    if bar % 4 == 0:
        lead(bar, tok, 104, transpose=5)
    if bar % 8 == 0:
        pad(bar, tok, 8, 52)

# 128-152 coda: layers leave one by one
for i, bar in enumerate(range(128, 152)):
    tok = "im" if (bar // 8) % 2 == 0 else "bVIM"
    if bar < 140:
        bass_motor(bar, tok, max(60, 90 - i * 2))
        sequence(bar, tok, max(30, 60 - i * 2), 3)
        if bar % 4 == 0: lead(bar, tok, max(60, 100 - i * 3), transpose=5)
    else:
        bass_motor(bar, tok, max(46, 64 - i))
    if bar % 8 == 0 and bar < 144:
        pad(bar, tok, 8, max(34, 50 - i))

# final chord, long, with the fifth ringing
root, iv = T1.triad("im")
b = T1.root_cents("im", 3)
for k, st in enumerate(iv):
    s.note(152 * BAR + k * 10, BAR * 2 - 60, b + T1.c(st), 44 - k * 4, 1)
s.note(152 * BAR, BAR * 2 - 60, T1.root_cents("im", 2), 40, 2)

s.write("Trans Europa 31", 120)
