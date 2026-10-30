#!/usr/bin/env python3
"""Load a sample set into the Pod's arena by writing the serial port directly.

The Pod's line buffer accepts at most sixteen samples per frame (an 82 character line);
longer frames are discarded whole, which is why bulk transfers never arrived. This takes the
port for the transfer: stop the bridge, write, verify, start the bridge.

Usage: loadset.py <set-name>
"""
import json
import os
import struct
import subprocess
import sys
import time

SLIB = os.path.expanduser("~/slib")
PORT = "/dev/ttyACM0"
PER_FRAME = 16          # samples per message: 82 characters, under the buffer limit


def frame(body):
    x = 0
    for ch in body.encode():
        x ^= ch
    return ("@" + body + "*%02X\n" % x).encode()


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True)


def read_want(ser, want, timeout=1.5):
    end = time.time() + timeout
    buf = b""
    while time.time() < end:
        buf += ser.read(600)
        for line in buf.decode("ascii", "ignore").split("\n"):
            if line.startswith(want):
                return line
    return ""


def main():
    if len(sys.argv) < 2:
        print(__doc__.strip())
        return 1
    name = os.path.basename(sys.argv[1])
    spec = json.load(open(os.path.join(SLIB, name, "set.json")))
    zones = spec["zones"]
    total = sum(os.path.getsize(os.path.join(SLIB, name, z["file"])) // 2 for z in zones)

    # the two pianos are baked into flash and far larger than the arena, so selecting them
    # restores the flash copies instead of streaming anything
    if name.startswith("piano"):
        import serial
        sh("sudo -n systemctl stop podbridge")
        time.sleep(0.5)
        ser = serial.Serial(PORT, 115200, timeout=0.2)
        time.sleep(0.3)
        ser.reset_input_buffer()
        ser.write(frame("P 46 1"))
        time.sleep(0.3)
        ser.write(frame("P 45 0"))
        time.sleep(0.3)
        reply = read_want(ser, "@L 46")
        ser.close()
        sh("sudo -n systemctl start podbridge")
        open(os.path.join(SLIB, "current.txt"), "w").write(spec.get("name", name))
        print("restored %s (%s)" % (spec.get("name", name), "ok" if reply else "no reply"))
        return 0

    sh("sudo -n systemctl stop podbridge")
    time.sleep(0.6)
    import serial
    ser = serial.Serial(PORT, 115200, timeout=0.2)
    time.sleep(0.3)
    ser.reset_input_buffer()

    ser.write(frame("C 0"))
    time.sleep(0.12)
    for z in zones:
        raw = open(os.path.join(SLIB, name, z["file"]), "rb").read()
        ser.write(frame("C %d %d" % (len(raw) // 2, int(z["root"]))))
        time.sleep(0.1)
        for i in range(0, len(raw), PER_FRAME * 2):
            blk = raw[i:i + PER_FRAME * 2]
            vals = struct.unpack("<%dh" % (len(blk) // 2), blk)
            ser.write(frame("X " + " ".join("%04X" % (v & 0xFFFF) for v in vals)))
            if (i // (PER_FRAME * 2)) % 64 == 63:
                ser.flush()
                time.sleep(0.002)
        ser.flush()
        time.sleep(0.05)

    ser.write(frame("G"))
    time.sleep(0.1)
    # sustained material loops the sample body; pianos keep their natural decay
    ser.write(frame("P 45 " + ("1" if name.startswith("tron") else "0")))
    lo = int(spec.get("loopA", 0.45) * 1000)
    hi = int(spec.get("loopB", 0.92) * 1000)
    ser.write(frame("P 47 %d" % lo))
    ser.write(frame("P 48 %d" % hi))
    reply = read_want(ser, "@J ")
    count = -1
    if reply:
        try:
            count = int(reply[3:].split("*")[0].strip())
        except ValueError:
            pass

    ok = False
    if count == total:
        first = open(os.path.join(SLIB, name, zones[0]["file"]), "rb").read(8)
        expect = " ".join("%04X" % (v & 0xFFFF) for v in struct.unpack("<4h", first))
        ser.write(frame("J 0 4"))
        back = read_want(ser, "@J 0 ")
        got = back[6:].split("*")[0].strip().upper() if back else ""
        ok = (got == expect)
        print("frames %d/%d  first %s  %s" % (count, total, got, "match" if ok else "MISMATCH " + expect))
    else:
        print("frames %d of %d" % (count, total))

    ser.close()
    sh("sudo -n systemctl start podbridge")
    open(os.path.join(SLIB, "current.txt"), "w").write(spec.get("name", name))
    print("set: %s" % spec.get("name", name))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
