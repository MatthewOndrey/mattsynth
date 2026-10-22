"""Build clean band-limited wave sets for the Pod's wavetable engine.

The AKWF files are single cycles sampled from recordings and squeezed into 512
points, so they carry whatever sampling artefacts they had: that is the crunch you
hear. These are synthesised from harmonic series instead, so every frame is smooth
by construction, and the top of each frame is rolled off so the mip levels in the
firmware are not fighting a table that already aliases.

Format the Pod expects: 8 frames of 512 signed 16 bit little endian samples, 8192
bytes per file.
"""
import math, os, struct

N = 512
OUT = "/home/matto/wt"
os.makedirs(OUT, exist_ok=True)

def frame(harm, phase_shift=0.0, tilt=1.0):
    buf = []
    peak = 0.0
    for i in range(N):
        th = 2 * math.pi * i / N + phase_shift
        v = 0.0
        for h, a in enumerate(harm, start=1):
            if a:
                v += (a / (h ** tilt)) * math.sin(th * h)
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
    print("%-18s %d bytes" % (name, len(data)))

def morph(a, b, steps=8, tilt=1.0):
    out = []
    for k in range(steps):
        t = k / (steps - 1)
        n = max(len(a), len(b))
        a = list(a) + [0.0]*(n-len(a)); b = list(b) + [0.0]*(n-len(b))
        h = [a[i] * (1 - t) + b[i] * t for i in range(n)]
        h += [0.0] * (64 - len(h))
        out.append(frame(h, phase_shift=0.0, tilt=tilt))
    return out

# 1 warm analogue: sine through to a soft saw, all harmonics rolled off above 24
warm_a = [1.0] + [0.0] * 63
warm_b = [1.0] + [1.0 / h for h in range(2, 25)] + [0.0] * 39
save("clean-warm", morph(warm_a, warm_b, tilt=0.85))

# 2 glass: odd partials only, bell like, high ones faded
glass = [0.0] * 64
glass[0] = 1.0
for h in (3, 5, 7, 9, 11, 15):
    glass[h - 1] = 0.9 / h ** 0.5
save("clean-glass", morph(warm_a, glass, tilt=0.7))

# 3 vocal: formant shaped, energy around the fifth and ninth harmonic
vox = [0.0] * 64
for h, a in [(1, 1.0), (2, 0.7), (3, 0.9), (4, 0.8), (5, 0.25), (6, 0.3), (7, 0.35),
             (9, 0.2), (11, 0.12), (13, 0.08)]:
    vox[h - 1] = a
save("clean-vox", morph(warm_a, vox, tilt=0.75))

# 4 hollow: a fifth and an octave, no thirds, clarinet-ish
hollow = [0.0] * 64
for h, a in [(1, 1.0), (3, 0.45), (5, 0.25), (7, 0.14), (9, 0.08)]:
    hollow[h - 1] = a
save("clean-hollow", morph(warm_a, hollow, tilt=0.8))

# 5 metal: inharmonic-ish stack of high odd partials, still band limited
metal = [0.0] * 64
for h, a in [(1, 0.6), (5, 0.5), (7, 0.4), (11, 0.3), (13, 0.25), (17, 0.18), (23, 0.1)]:
    metal[h - 1] = a
save("clean-metal", morph(warm_a, metal, tilt=0.6))
