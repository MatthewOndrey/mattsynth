#!/usr/bin/env python3
"""Push a raw 16 bit mono sample into the Pod's arena.

Usage: push.py <file.raw> <root-note> [--chunk N]

The Pod owns the serial port through the bridge, so this goes over the panel's /send,
which is the same path the buttons use.
"""
import struct
import sys
import time
import urllib.request

SEND = "http://127.0.0.1:8080/send"


def send(body):
    req = urllib.request.Request(SEND, data=body.encode())
    urllib.request.urlopen(req, timeout=5).read()


def main():
    if len(sys.argv) < 3:
        print(__doc__.strip())
        return 1
    path, root = sys.argv[1], int(sys.argv[2])
    chunk = 128
    if "--chunk" in sys.argv:
        chunk = int(sys.argv[sys.argv.index("--chunk") + 1])

    raw = open(path, "rb").read()
    frames = len(raw) // 2
    send("C %d %d" % (frames, root))
    t0 = time.time()
    for i in range(0, frames, chunk):
        block = raw[i * 2:(i + chunk) * 2]
        vals = struct.unpack("<%dh" % (len(block) // 2), block)
        send("X " + "".join("%04X" % (v & 0xFFFF) for v in vals))
        if (i // chunk) % 64 == 0:
            done = i / frames
            sys.stdout.write("\r%5.1f%%  %5.0f KB/s" % (done * 100, (i * 2) / 1024 / max(time.time() - t0, 1e-6)))
            sys.stdout.flush()
    send("G")
    dt = time.time() - t0
    print("\r100.0%%  %d frames in %.2fs = %.0f KB/s" % (frames, dt, len(raw) / 1024 / dt))
    return 0


if __name__ == "__main__":
    sys.exit(main())
