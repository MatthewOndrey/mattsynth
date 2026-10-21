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
            for i in range(1, 9):
                f = i / 8.0
                self._bend(t + int(dur * f), ch, cents + (glide_to - cents) * f - base)
        self.evs.append((t + dur, 0, ch, bytes([0x80 | ch, note, 0])))
    def poly(self):
        ev = sorted([(e[0], 1) for e in self.evs if e[1] == 2] + [(e[0], -1) for e in self.evs if e[1] == 0])
        cur = mx = 0
        for _, d in ev:
            cur += d; mx = max(mx, cur)
        return mx
    def write(self, name, bpm, beats_per_bar=4):
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
        path = os.path.join(OUT, name.replace(" ", "-").lower() + ".mid")
        open(path, "wb").write(data)
        bars = self.evs[-1][0] / (DIV * beats_per_bar)
        print("%-22s %5dB  max %2d voices  %4.0f bars  ~%3.0fs" %
              (name, len(data), self.poly(), bars, bars * beats_per_bar * 60 / bpm))

NOTE = {"C":0,"Db":1,"D":2,"Eb":3,"E":4,"F":5,"Gb":6,"G":7,"Ab":8,"A":9,"Bb":10,"B":11}
ROM = {"I":0,"II":2,"III":4,"IV":5,"V":7,"VI":9,"VII":11}

class Tune:
    def __init__(self, edo, key):
        self.edo = edo; self.step = 1200.0 / edo; self.key = NOTE[key]
        self.M3  = round(386.31 / self.step)
        self.m3  = round(315.64 / self.step)
        self.P5  = round(701.96 / self.step)
        self.m7  = round(968.83 / self.step)
        self.M7  = round(1088.27 / self.step)
        self.M6  = round(884.36 / self.step)
        self.m6  = round(813.69 / self.step)
        self.M2  = round(203.91 / self.step)
        self.m2  = round(111.73 / self.step)     # 16/15, the microtonal spice
    def c(self, st): return st * self.step
    def chord(self, tok):
        flat = 0
        while tok and tok[0] in "b#":
            flat += -1 if tok[0] == "b" else 1; tok = tok[1:]
        roman = ""
        while tok and tok[0] in "IViv":
            roman += tok[0]; tok = tok[1:]
        minor = roman.islower(); ext = tok.upper()
        root = (ROM[roman.upper()] + flat) % 12
        iv = [0, self.m3 if minor else self.M3, self.P5]
        if "7" in ext: iv.append(self.m7)
        if "6" in ext: iv.append(self.m6 if minor else self.M6)
        if "9" in ext: iv.append(self.M2)
        return root, iv
    def root_cents(self, tok, octave):
        r, _ = self.chord(tok)
        return (((self.key + r) % 12) + 12 * octave) * 100

BB = DIV * 4

def arp(s, T, bar, tok, beats=4, vel=60, octave=3, pattern=(0, 2, 1, 3, 2, 1, 0, 1)):
    """eighth-note arpeggio through the chord, accenting the beat"""
    root, iv = T.chord(tok)
    base = T.root_cents(tok, octave)
    n = len(pattern)
    for i in range(int(beats * 2)):
        step = iv[pattern[i % n] % len(iv)]
        cents = base + T.c(step + (T.edo if i >= 4 and len(iv) > 2 else 0))
        v = vel + (10 if i % 2 == 0 else 0) + (i == 0) * 6
        s.note(bar * BB + i * (DIV // 2), DIV // 2 - 30, cents, v, 1)

def pad(s, T, bar, tok, beats=4, vel=54, octave=3):
    root, iv = T.chord(tok)
    base = T.root_cents(tok, octave)
    for k, st in enumerate(iv):
        s.note(bar * BB + k * 6, beats * DIV - 40, base + T.c(step_oct(st, k)), vel + (7 if k == 0 else 0), 1)

def step_oct(st, k):
    return st

def bassline(s, T, bar, tok, beats=4, vel=80, octave=2, walk=False):
    base = T.root_cents(tok, octave)
    if beats == 4:
        s.note(bar * BB, DIV - 25, base, vel, 2)
        s.note(bar * BB + DIV, DIV - 25, base + T.c(T.P5) - 100 if False else base + T.c(T.P5), vel - 8, 2)
        if walk:
            s.note(bar * BB + 2 * DIV, DIV - 25, base + T.c(T.P5) + T.c(T.M2), vel - 12, 2)
        else:
            s.note(bar * BB + 2 * DIV, DIV - 25, base, vel - 6, 2)
        s.note(bar * BB + 3 * DIV, DIV - 25, base + T.c(T.M3), vel - 14, 2)
    else:
        s.note(bar * BB, beats * DIV - 30, base, vel, 2)

# a melody note is placed by scale degree; on strong beats it is pulled to a chord tone
def snap_to_chord(T, tok, deg, octave, strong):
    root, iv = T.chord(tok)
    base = T.root_cents(tok, octave)
    if not strong:
        return base + T.c(deg)
    tone = min(iv, key=lambda x: abs(x - deg))
    return base + T.c(tone)

def melody(s, T, bar, tok, phrase, octave=4, vel=92, ch=0):
    for (beat, dur, deg, strong, glide) in phrase:
        c0 = snap_to_chord(T, tok, deg, octave, strong)
        s.note(bar * BB + int(beat * DIV), int(dur * DIV),
               c0, vel + (6 if strong else 0), ch,
               (c0 + glide) if glide else None)

# --------------------------------------------------------------- pieces ------
def amber_ladder():
    T = Tune(31, "D")
    s = Song()
    A = ["I", "bVIIM", "ivm", "bIIIM"]           # Modal/D - I bVIIM ivm bIIIM
    B = ["bVIM", "bVIIM", "I", "ivm"]
    motif = [(0, 1, 0, True, None), (1, .5, 2, False, None), (1.5, .5, 3, False, 39),
             (2, 1.5, 4, True, None), (3.5, .5, 2, False, None)]
    lift  = [(0, .5, 4, True, None), (.5, .5, 5, False, None), (1, 1, 7, True, -39),
             (2, .5, 5, False, None), (2.5, .5, 4, False, None), (3, 1, 7, True, None)]

    s.note(0, BB * 2 - 30, T.root_cents("I", 3), 44, 1)      # intro pad
    s.note(0, BB * 2 - 30, T.root_cents("I", 2), 40, 2)
    melody(s, T, 1, "I", [(0, 2, 7, True, -39), (2, 2, 4, True, None)], 5, 72)

    bar = 2
    for tok in A + A + A + A:
        arp(s, T, bar, tok, 4, 56)
        bassline(s, T, bar, tok, 4, 78, 2)
        melody(s, T, bar, tok, motif, 4, 92)
        bar += 1
    for tok in B + B:                                        # contrast: lower, softer
        pad(s, T, bar, tok, 4, 52)
        bassline(s, T, bar, tok, 4, 74, 2)
        melody(s, T, bar, tok, motif, 4, 78)
        bar += 1
    for tok in B:                                            # build through the second half
        pad(s, T, bar, tok, 4, 58)
        bassline(s, T, bar, tok, 4, 80, 2, walk=True)
        melody(s, T, bar, tok, lift, 4, 90)
        bar += 1
    for tok in A + A:                                        # lift, melody an octave up
        arp(s, T, bar, tok, 4, 62)
        bassline(s, T, bar, tok, 4, 84, 2, walk=True)
        melody(s, T, bar, tok, lift, 5, 100)
        bar += 1
    for tok in ["I", "I"]:
        pad(s, T, bar, tok, 4, 46)
        bassline(s, T, bar, tok, 4, 66, 2)
        bar += 1
    s.write("Amber Ladder", 88)

def quarter_light():
    T = Tune(24, "B")
    s = Song()
    A = ["I", "ivm", "bIIIM", "bVIIM"]            # Modal/B - I ivm bIIIM bVIIM
    B = ["im", "bIIM", "bVIIM", "vm"]
    # quarter-tone neighbours: the m2 is exactly one 24-EDO step
    motif = [(0, .5, 0, True, None), (.5, .5, 1, False, None), (1, .5, 2, False, 25),
             (1.5, 1, 3, True, None), (3, .5, 2, False, None), (3.5, .5, 1, False, -25)]
    s.note(0, BB - 30, T.root_cents("I", 2), 46, 2)
    arp(s, T, 0, "I", 4, 50, 3, pattern=(0, 1, 2, 1, 0, 1, 2, 3))
    bar = 1
    for tok in A + A + A + A:
        arp(s, T, bar, tok, 4, 58, 3, pattern=(0, 1, 2, 1, 0, 1, 2, 1))
        bassline(s, T, bar, tok, 4, 82, 2, walk=True)
        melody(s, T, bar, tok, motif, 4, 94)
        bar += 1
    for tok in B + B:
        pad(s, T, bar, tok, 4, 56)
        bassline(s, T, bar, tok, 4, 80, 2)
        melody(s, T, bar, tok, motif, 5, 88)
        bar += 1
    for tok in A + A:
        arp(s, T, bar, tok, 4, 66, 3, pattern=(0, 2, 1, 3, 2, 1, 0, 2))
        bassline(s, T, bar, tok, 4, 86, 2, walk=True)
        melody(s, T, bar, tok, motif, 5, 102)
        bar += 1
    for tok in ["I", "I"]:
        pad(s, T, bar, tok, 4, 44)
        bar += 1
    s.write("Quarter Light", 100)

def nineteen_windows():
    T = Tune(19, "A")
    s = Song()
    prog = ["I", "bVIM", "bVIIM", "ivm", "bIIIM", "I"]
    # slow, spacious: long notes with glides, chords breathing underneath
    long_motif = [(0, 2, 7, True, -39), (2, 2, 4, True, None), (4, 2, 2, False, 39), (6, 2, 0, True, None)]
    bar = 0
    for rep in range(4):
        for tok in prog:
            pad(s, T, bar, tok, 4, 46 if rep == 0 else 54)
            bassline(s, T, bar, tok, 4, 64 if rep == 0 else 72, 2)
            melody(s, T, bar, tok, long_motif, 4 if rep == 0 else 5, 72 if rep == 0 else 88)
            bar += 1
    for tok in ["I", "I"]:
        pad(s, T, bar, tok, 4, 40)
        bassline(s, T, bar, tok, 4, 58, 2)
        bar += 1
    s.write("Nineteen Windows", 63)

def seventh_rain():
    T = Tune(31, "D")
    s = Song()
    # 3/4 waltz: bass on 1, chord on 2 and 3, melody floating above
    A = ["I", "I7", "IV", "ivm"]
    B = ["bVIM", "bIIIM", "bVIIM", "I"]
    waltz = [(0, .5, 7, True, None), (.5, .5, 5, False, None), (1, 1, 4, True, -39),
             (2, 1, 2, False, None)]
    BBB = DIV * 3
    bar = 0
    for tok in ["I", "I"]:
        s.note(bar * BBB, BBB - 30, T.root_cents(tok, 3), 44, 1)
        bar += 1
    for tok in A + B + A + A + B + A + A:
        root, iv = T.chord(tok)
        base = T.root_cents(tok, 3)
        s.note(bar * BBB, DIV - 20, T.root_cents(tok, 2), 78, 2)          # beat 1 bass
        for k, st in enumerate(iv):                                        # beats 2 and 3
            s.note(bar * BBB + DIV, DIV - 20, base + T.c(st), 58 + k * 4, 1)
            s.note(bar * BBB + 2 * DIV, DIV - 20, base + T.c(st), 52 + k * 4, 1)
        for (beat, dur, deg, strong, glide) in waltz:
            c0 = snap_to_chord(T, tok, deg, 5, strong)
            s.note(bar * BBB + int(beat * DIV), int(dur * DIV), c0, 90, 0,
                   (c0 + glide) if glide else None)
        bar += 1
    for tok in ["I", "I"]:
        s.note(bar * BBB, BBB - 30, T.root_cents(tok, 3), 38, 1)
        bar += 1
    s.write("Seventh Rain", 76, beats_per_bar=3)

amber_ladder()
quarter_light()
nineteen_windows()
seventh_rain()
