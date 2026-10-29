#!/usr/bin/env python3
"""Push a whole multisample set into the Pod's arena, one zone per file.

Usage: push_set.py <dir> <name=root> [<name=root> ...]

Example: push_set.py ~/slib/iowa C2=36 C3=48 C4=60 C5=72 C6=84

Files are raw 16 bit mono at 48 kHz, named <name>.raw. The Pod takes five zones and 600,000
frames in total, so a set of five two second samples just fits.
"""
import os
import struct
import sys
import time
import urllib.parse
import urllib.request

SEND = "http://127.0.0.1:8080/send"
CHUNK = 128


def send(body):
    urllib.request.urlopen(urllib.request.Request(SEND, data=body.encode()), timeout=6).read()


def main():
    if len(sys.argv) < 3:
        print(__doc__.strip())
        return 1
    folder = os.path.expanduser(sys.argv[1])
    zones = [a.split("=") for a in sys.argv[2:]]
    send("C 0")                       # clear the previous set
    t0 = time.time()
    total = 0
    for name, root in zones:
        path = os.path.join(folder, name + ".raw")
        raw = open(path, "rb").read()
        frames = len(raw) // 2
        total += frames
        send("C %d %d" % (frames, int(root)))
        for i in range(0, len(raw), CHUNK * 2):
            block = raw[i:i + CHUNK * 2]
            vals = struct.unpack("<%dh" % (len(block) // 2), block)
            send("X " + "".join("%04X" % (v & 0xFFFF) for v in vals))
        print("  %-6s root %-3s %6d frames" % (name, root, frames))
    send("G")
    # tell the panel what is loaded, so the header names the set rather than guessing
    try:
        name = os.path.basename(os.path.normpath(folder)) or "Streamed set"
        urllib.request.urlopen("http://127.0.0.1:8080/setname?n=" + urllib.parse.quote(name), timeout=4).read()
    except Exception:
        pass
    dt = time.time() - t0
    print("%d zones, %d frames, %.1fs" % (len(zones), total, dt))
    return 0


if __name__ == "__main__":
    sys.exit(main())
