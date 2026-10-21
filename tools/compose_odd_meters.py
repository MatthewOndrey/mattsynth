import struct, os, math

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
        c = max(-200.0, min(200.0, cents))
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
        ev = sorted([(e[0], 1) for e in self.evs if e[1] == 2] +
                    [(e[0], -1) for e in self.evs if e[1] == 0])
        cur = mx = 0
        for _, d in ev:
            cur += d; mx = max(mx, cur)
        return mx
    def write(self, name, bpm, num, den_pow, bar_ticks):
        self.evs.sort(key=lambda e: (e[0], e[1]))
        body = vlq(0) + b"\xff\x03" + vlq(len(name)) + name.encode()
        body += vlq(0) + b"\xff\x51\x03" + struct.pack(">I", int(60000000 / bpm))[1:]
        body += vlq(0) + b"\xff\x58\x04" + bytes([num, den_pow, 24, 8])
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
        secs = self.evs[-1][0] / DIV * 60.0 / bpm
        print("%-22s %6dB  %2d voices max  %5.0f bars  %4.0f s" %
              (name, len(data), self.poly(), self.evs[-1][0] / bar_ticks, secs))

NOTE = {"C":0,"Db":1,"D":2,"Eb":3,"E":4,"F":5,"Gb":6,"G":7,"Ab":8,"A":9,"Bb":10,"B":11}

# just-intonation intervals, in cents
R = dict(P1=0.0, m2=111.73, M2=203.91, m3=315.64, n3=347.41, M3=386.31, P4=498.04,
         TT=551.32, P5=701.96, m6=813.69, n6=840.53, M6=884.36, m7=968.83, M7=1088.27,
         O=1200.0)

class Tune:
    """cents are derived from just ratios, then rounded onto the EDO grid so the
    bends stay inside +/-50 cents and the file plays on any synth."""
    def __init__(self, edo, key):
        self.edo = edo; self.step = 1200.0 / edo; self.key = NOTE[key]
    def snap(self, c):
        return round(c / self.step) * self.step
    def root(self, semis, octave):
        return (((self.key + semis) % 12) + 12 * octave) * 100.0

def chord_of(T, root_semis, shape, octave):
    return [T.snap(T.root(root_semis, octave) + R[k]) for k in shape]

def voice(s, t, dur, notes, vel, ch, stagger=4, glide=False):
    for k, c in enumerate(notes):
        g = None
        if glide and k == 0:
            g = c + T_glide
        s.note(t + k * stagger, dur - k * stagger, c, vel - k * 3, ch, g)

# ---------------------------------------------------------------- 3/16 -------
def pebble_dance():
    """19-EDO, three sixteenths to the bar, phrases of four bars so the melody
    runs 3-against-4 against the barline. Bright plucked ostinato."""
    T = Tune(19, "G")
    s = Song()
    BAR = 360                       # 3 * 120 ticks
    unit = BAR // 3                 # one sixteenth
    prog = [(7, ("P1","m3","P5")), (0, ("P1","M3","P5")), (5, ("P1","m3","P5")),
            (10,("P1","M3","P5"))]
    # a five-note figure that never lands on the same place twice
    fig = [0, 2, 1, 4, 3]
    bar = 0
    def layer(bars, vel, oct_, melody, dens=1.0):
        nonlocal bar
        for i in range(bars):
            root, shape = prog[(bar // 2) % 4]
            ch = chord_of(T, root, shape, oct_)
            # ostinato: three sixteenths, shifted by one step every bar
            for k in (0, 2):        # the middle sixteenth is the hole in the motor;
                                    # three hits per 0.54 s bar outruns the release tail
                c = ch[fig[(k + bar) % len(fig)] % len(ch)]
                s.note(bar * BAR + k * unit, int(unit * 0.75), c, vel, 1)
            # bass on the downbeat, an octave down, every other bar
            if bar % 2 == 0:
                s.note(bar * BAR, BAR - 20, ch[0] - 1200, vel + 12, 2)
            # melody in four-bar phrases
            if melody:
                m = fig[(bar + 1) % len(fig)] % len(ch)
                if bar % 4 == 0:
                    s.note(bar * BAR, unit * 2 - 8, ch[m] + 1200, vel + 24, 0, ch[m] + 1200 + 38.7)
                elif bar % 4 == 2:
                    s.note(bar * BAR + unit, unit * 2 - 8, ch[(m + 1) % len(ch)] + 1200, vel + 18, 0)
            bar += 1

    layer(16, 44, 4, False)                 # intro: ostinato only
    layer(64, 56, 4, False)                 # A: bass joins, long
    layer(64, 64, 4, True)                  # A: melody
    layer(32, 50, 4, True)                  # B: thinner
    layer(64, 70, 4, True)                  # A'
    layer(32, 76, 4, True)                  # drive
    layer(32, 54, 4, False)                 # outro
    s.write("Pebble Dance", 84, 3, 4, BAR)   # slower: the ostinato repeats as fast as the release tail allows

# ---------------------------------------------------------------- 3/12 -------
T_glide = 0
def triplet_rain():
    """24-EDO, bars of three triplet eighths (a 3/12 feel), so every bar is one
    quarter note of 0.65 s. Slow rain: neutral thirds, septimal sevenths, glides."""
    T = Tune(24, "B")
    s = Song()
    BAR = 480                       # 3 triplet eighths = one quarter note
    unit = BAR // 3
    # Bm-ish drone on 1, then a chord a neutral third away, then the flat seven
    prog = [(11, ("P1","m3","P5")), (11, ("P1","n3","P5")), (4,  ("P1","M3","TT")),
            (9,  ("P1","m3","m7"))]
    bar = 0
    def section(bars, vel, oct_, lead, glide=False):
        nonlocal bar
        for i in range(bars):
            root, shape = prog[(bar // 4) % 4]
            ch = chord_of(T, root, shape, oct_)
            if bar % 4 == 0:                       # pad enters every four bars
                for k, c in enumerate(ch):
                    s.note(bar * BAR + k * 6, BAR * 4 - 60, c, vel - 6 + k * 2, 3,
                           (c + 50) if (glide and k == 0) else None)
            s.note(bar * BAR, BAR - 30, ch[0] - 1200, vel + 10, 2)
            if lead:
                # two notes per bar, the second a quarter tone away
                s.note(bar * BAR + unit, unit - 12, ch[(bar) % len(ch)] + 1200, vel + 14, 0)
                if bar % 2 == 1:
                    s.note(bar * BAR + 2 * unit, unit - 12, ch[(bar + 1) % len(ch)] + 1150,
                           vel + 6, 0, ch[(bar + 1) % len(ch)] + 1150 - 50)
            bar += 1
    section(24, 44, 3, False)
    section(64, 52, 3, True)
    section(64, 58, 3, True, glide=True)
    section(32, 46, 3, False)
    section(64, 62, 3, True, glide=True)
    section(24, 40, 3, False)
    s.write("Triplet Rain", 92, 3, 3, BAR)

# ---------------------------------------------------------------- 3/8 --------
def three_eight():
    """31-EDO, a 3/8 waltz-hemiola. Bass on one, chord on two, arpeggio on three;
    the middle section goes two against three. Sevenths are 7/4, not 12-TET."""
    T = Tune(31, "D")
    s = Song()
    BAR = 720                       # 3 eighths
    e = BAR // 3
    prog = [(2,  ("P1","m3","P5","m7")), (2,  ("P1","m3","P5","m7")),
            (9,  ("P1","M3","P5","m7")), (7,  ("P1","m3","P5","m7"))]
    bar = 0
    def waltz(bars, vel, lead, arp=True):
        nonlocal bar
        for i in range(bars):
            root, shape = prog[(bar // 4) % 4]
            ch = chord_of(T, root, shape, 3)
            s.note(bar * BAR, e - 12, ch[0] - 1200, vel + 8, 2)          # beat 1
            # harmony lives in the sustained pad; per-bar stabs cost four release
            # tails each and that is what pushes the pool past 850 us
            if bar % 4 == 0:
                for k, c in enumerate(ch):
                    s.note(bar * BAR + k * 8, BAR * 4 - 40, c, vel - 8 + k * 2, 3)
            if arp:
                s.note(bar * BAR + 2 * e, int(e * 0.95), ch[(bar + 1) % len(ch)] + 1200, vel, 1)
            if lead and bar % 4 == 0:
                top = ch[-1] + 1200
                s.note(bar * BAR + 2 * e, e * 2 - 20, top, vel + 20, 0, top - 38.7)
            bar += 1
    def two_against_three(bars, vel):
        nonlocal bar
        for i in range(bars):
            root, shape = prog[(bar // 4) % 4]
            ch = chord_of(T, root, shape, 3)
            for k in range(2):                       # two even notes over three
                s.note(bar * BAR + int(k * BAR / 2), int(BAR / 2) - 16, ch[k % len(ch)], vel, 3)
            s.note(bar * BAR, BAR - 30, ch[0] - 1200, vel + 6, 2)
            if bar % 2 == 0:
                s.note(bar * BAR + e, e - 10, ch[-1] + 1200, vel + 16, 0)
            bar += 1

    waltz(32, 46, False)
    waltz(64, 58, True)
    two_against_three(48, 54)
    waltz(64, 66, True)
    waltz(32, 72, True)
    waltz(32, 48, False)
    s.write("Three Eight", 96, 3, 3, BAR)

pebble_dance()
triplet_rain()
three_eight()
