#!/usr/bin/env python3
"""Fetch a few Salamander piano notes (CC-BY 3.0, Alexander Holm) and turn them into
a C header of 48 kHz mono 16 bit roots, trimmed and faded."""
import os, subprocess, struct, urllib.request

ROOTS = [("C2", 36), ("C3", 48), ("C4", 60), ("C5", 72), ("C6", 84)]
OUT = os.path.expanduser("~/samples")
os.makedirs(OUT, exist_ok=True)
HEADER = os.path.expanduser("~/daisy-panel/firmware/daisy-pod/piano.h")
SECONDS = 2.4
SR = 48000

arrays = []
for name, note in ROOTS:
    mp3 = os.path.join(OUT, name + ".mp3")
    wav = os.path.join(OUT, name + ".raw")
    if not os.path.isfile(mp3):
        urllib.request.urlretrieve("https://tonejs.github.io/audio/salamander/%s.mp3" % name, mp3)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", mp3,
                    "-ac", "1", "-ar", str(SR), "-f", "s16le", wav], check=True)
    raw = open(wav, "rb").read()
    frames = len(raw) // 2
    want = int(SR * SECONDS)
    pcm = struct.unpack("<%dh" % frames, raw)[:want]
    if len(pcm) < want:
        pcm = pcm + (0,) * (want - len(pcm))
    peak = max(1, max(abs(v) for v in pcm))
    gain = 30000.0 / peak
    tail = int(SR * 0.25)
    out = []
    for i, v in enumerate(pcm):
        g = gain
        if i > len(pcm) - tail:
            g *= (len(pcm) - i) / tail
        out.append(max(-32767, min(32767, int(v * g))))
    arrays.append((name, note, out))
    print("%s %d frames peak %d" % (name, len(out), peak))

with open(HEADER, "w", encoding="utf-8") as fh:
    fh.write("/* Salamander piano, Alexander Holm, CC-BY 3.0. Trimmed to %0.1f s, 48 kHz mono. */\n" % SECONDS)
    fh.write("#pragma once\n#include <stdint.h>\n\n")
    for name, note, pcm in arrays:
        fh.write("static const int16_t pno_%s[] = {\n" % name.lower())
        for i in range(0, len(pcm), 16):
            fh.write("    " + ",".join(str(v) for v in pcm[i:i + 16]) + ",\n")
        fh.write("};\n")
    fh.write("\nstruct PianoRoot { const int16_t *data; uint32_t len; int8_t note; };\n")
    fh.write("static const PianoRoot kPiano[%d] = {\n" % len(arrays))
    for name, note, pcm in arrays:
        fh.write('    { pno_%s, %d, %d },\n' % (name.lower(), len(pcm), note))
    fh.write("};\n")
print("wrote", HEADER, round(os.path.getsize(HEADER) / 1024), "KB of header")
