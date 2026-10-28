"""Better wave sets for the Pod's wavetable engine.

Same wire format as before: eight frames of 512 signed 16 bit samples, 8192 bytes.
Every frame is built from a harmonic series, so it is band limited by construction, and
the partials are rolled off above the 24th so the top of the keyboard does not fizz.

The difference from the first generation is the content: these morph through something
musical across the eight frames, a vowel path, a pulse width sweep, a drawbar pull, rather
than starting at a sine and ending somewhere else.
"""
import math, os, struct

N = 512
OUT = "/home/matto/wt"
os.makedirs(OUT, exist_ok=True)

def frame(harm, rolloff=14.0):
    """harm: list of amplitudes per harmonic. Rolloff is a soft knee above that partial.

    Fourteen partials is deliberate: the engine has no mip levels, so a table with forty
    eight harmonics aliases badly in the top two octaves. Fourteen keeps the top clean at
    the cost of some bite, which is the right trade for a touch panel with no filters on
    the table."""
    buf, peak = [], 0.0
    for i in range(N):
        th = 2 * math.pi * i / N
        v = 0.0
        for h, a in enumerate(harm, start=1):
            if not a:
                continue
            if h > rolloff:
                a *= math.exp(-((h - rolloff) / 6.0) ** 2)
            v += a * math.sin(th * h)
        buf.append(v)
        peak = max(peak, abs(v))
    if peak:
        buf = [x / peak * 0.95 for x in buf]
    return buf

def save(name, frames):
    data = bytearray()
    for f in frames:
        for x in f:
            data += struct.pack("<h", max(-32767, min(32767, int(round(x * 32767)))))
    open(os.path.join(OUT, name + ".wt"), "wb").write(bytes(data))
    print("%-16s %d bytes" % (name, len(data)))

def blend(a, b, t):
    n = max(len(a), len(b))
    a = list(a) + [0.0] * (n - len(a))
    b = list(b) + [0.0] * (n - len(b))
    return [a[i] * (1 - t) + b[i] * t for i in range(n)]

def path(points, steps=8):
    """Walk a list of harmonic vectors and return `steps` frames along it."""
    out = []
    for k in range(steps):
        pos = k / (steps - 1) * (len(points) - 1)
        i = min(int(pos), len(points) - 2)
        t = pos - i
        out.append(frame(blend(points[i], points[i + 1], t)))
    return out

def formants(f1, f2, f3, f0=110.0, tilt=1.0):
    """A vowel: harmonics near the formant frequencies are loud."""
    h = [0.0] * 48
    for n in range(1, 49):
        f = n * f0
        a = 0.0
        for fc, bw, g in ((f1, 90, 1.0), (f2, 110, 0.7), (f3, 140, 0.45)):
            a += g * math.exp(-((f - fc) / bw) ** 2)
        h[n - 1] = a / (n ** tilt) + (1.0 / n if n < 8 else 0.0) * 0.25
    return h

# 1 vowels: A, E, I, O, U
save("vox-path", path([
    formants(730, 1090, 2440), formants(530, 1840, 2480), formants(270, 2290, 3010),
    formants(570, 840, 2410),  formants(300, 870, 2240),
]))

# 2 pulse width: 50 percent down to 4 percent, the classic hollow sweep
pwm = []
for k in range(8):
    d = 0.5 - 0.46 * (k / 7)
    h = [(2.0 / (n * math.pi)) * math.sin(math.pi * n * d) for n in range(1, 49)]
    pwm.append(frame(h))
save("pwm-sweep", pwm)

# 3 drawbars: 16, 8, 4, 2 2/3 pulled in one at a time
bars = [
    {1: 1.0},
    {1: 1.0, 2: 0.6},
    {1: 1.0, 2: 0.6, 4: 0.35},
    {1: 1.0, 2: 0.6, 4: 0.35, 3: 0.3},
    {1: 1.0, 2: 0.6, 4: 0.35, 3: 0.3, 8: 0.2},
    {1: 1.0, 2: 0.7, 4: 0.5, 3: 0.4, 8: 0.25, 6: 0.15},
    {1: 1.0, 2: 0.8, 4: 0.6, 3: 0.5, 8: 0.35, 6: 0.25, 12: 0.15},
    {1: 1.0, 2: 0.9, 4: 0.7, 3: 0.6, 8: 0.45, 6: 0.35, 12: 0.25, 16: 0.15},
]
save("organ-drawbar", path([[(b.get(n, 0.0)) for n in range(1, 33)] for b in bars]))

# 4 bell: sparse odd partials, each with its own weight, thinning as it morphs
bell_a = [(1, 1.0), (3, 0.5), (5, 0.35), (9, 0.2), (15, 0.1)]
bell_b = [(1, 1.0), (3, 0.2), (7, 0.45), (11, 0.3), (17, 0.18), (23, 0.1)]
def sparse(pairs):
    h = [0.0] * 32
    for n, a in pairs:
        h[n - 1] = a
    return h
save("bell-sparse", path([sparse(bell_a), sparse(bell_b)]))

# 5 saw that loses its top as it morphs: bright to warm, useful for bass and pads
saw = [(1.0 / n ** 0.9) for n in range(1, 49)]
soft = [(1.0 / n ** 1.8) if n < 12 else 0.0 for n in range(1, 49)]
save("saw-warm", path([saw, soft]))

# 6 two harmonic families, a rough edge for leads
rough = [(1.0 / n) * (1.0 + 0.5 * math.cos(n * 0.7)) for n in range(1, 49)]
clean = [(1.0 / n ** 1.2) for n in range(1, 49)]
save("rough-lead", path([clean, rough, clean]))

print("done")
