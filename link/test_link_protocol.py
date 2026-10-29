#!/usr/bin/env python3
"""
Host-side check of the link framing rules in link_protocol.h.

This mirrors frame() and Parser::feed()/end() in Python and exercises them.
It proves the wire rules (checksum, recovery from log noise, splitting across
reads, overflow). It does NOT prove the C++ compiles; that needs a toolchain.

Run:  python test_link_protocol.py
"""
import random
import sys

BUF_SIZE = 96


def checksum(b: bytes) -> int:
    x = 0
    for c in b:
        x ^= c
    return x


def frame(body: str) -> bytes:
    """Mirror of link::frame(). Returns the framed bytes or b'' if illegal."""
    n = len(body)
    if n == 0 or n > BUF_SIZE - 5:
        return b""
    for c in body:
        if c in "@*\n\r" or not (32 <= ord(c) <= 126):
            return b""
    b = body.encode()
    cs = checksum(b)
    return b"@" + b + b"*" + b"%02X" % cs + b"\n"


class Parser:
    """Mirror of link::Parser."""

    def __init__(self):
        self.buf = bytearray()
        self.line = None          # body str when a valid frame completed
        self.discarding = False
        self.reset()

    def reset(self):
        self.buf = bytearray()
        self.line = None
        self.discarding = False

    def feed(self, data: bytes):
        """Feed bytes; returns a list of completed bodies (usually 0 or 1)."""
        out = []
        for byte in data:
            if byte in (0x0A, 0x0D):
                r = self._end()
                self.discarding = False
                if r is not None:
                    out.append(r)
                continue
            if self.discarding:
                if byte != ord("@"):        # resync on the next start of frame
                    continue
                self.discarding = False
                self.buf = bytearray()
            if len(self.buf) >= BUF_SIZE:
                self.discarding = True      # runaway line: drop, resync on next '@'
                self.buf = bytearray()
                continue
            self.buf.append(byte)
        return out

    def _end(self):
        self.line = None
        buf = self.buf
        n = len(buf)
        if n >= 4 and buf[0] == ord("@"):
            star = 0
            for i in range(1, n):
                if buf[i] == ord("*"):
                    star = i
            if star >= 2 and star + 3 == n:
                try:
                    hi = int(chr(buf[star + 1]), 16)
                    lo = int(chr(buf[star + 2]), 16)
                except ValueError:
                    hi = lo = -1
                if hi >= 0 and lo >= 0:
                    want = (hi << 4) | lo
                    if checksum(bytes(buf[1:star])) == want:
                        self.line = buf[1:star].decode("ascii")
        self.buf = bytearray()
        return self.line


PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print("  FAIL:", name)


# --- 1. round trip ---------------------------------------------------------
bodies = [
    "A",
    "N 60 100",
    "O 60",
    "P 0 900",
    "P 3 1200",
    "K 1 512",
    "E -3",
    "S 1",
    "M pod ready",
    "M " + "x" * 80,
]
print("1. round trip")
for b in bodies:
    f = frame(b)
    check(f"frame() accepts {b!r}", f != b"")
    p = Parser()
    got = p.feed(f)
    check(f"decodes {b!r} -> {got}", got == [b])

# --- 2. exact byte layout --------------------------------------------------
print("2. byte layout")
check("'@N 60 100*XX\\n' layout", frame("N 60 100") == b"@N 60 100*" +
      b"%02X" % checksum(b"N 60 100") + b"\n")
check("checksum of 'N 60 100' is stable", frame("N 60 100") == frame("N 60 100"))
check("different bodies differ", frame("P 0 1") != frame("P 0 2"))

# --- 3. rejected inputs ----------------------------------------------------
print("3. framing rejects bad input")
for bad in ["", "@", "N 60 100*", "N*60", "a" * 100, "N 60 100\n", "N 60 100\r"]:
    check(f"frame() rejects {bad!r}", frame(bad) == b"")

# --- 4. tamper detection --------------------------------------------------
print("4. tamper detection")
f = frame("P 1 500")
bad = bytearray(f)
bad[3] ^= 0x01                      # flip a body byte, keep the checksum
check("single bit flip is dropped", Parser().feed(bytes(bad)) == [])
bad = bytearray(f)
bad[-2] = ord("0")                  # corrupt the checksum digit
check("bad checksum digit is dropped", Parser().feed(bytes(bad)) == [])

# --- 5. survives log noise on the same wire -------------------------------
print("5. noise immunity (shared UART with debug logs)")
noise = (b"ESP-ROM:esp32s3-20221121\r\n"
         b"I (312) cpu_start: Pro cpu up.\r\n"
         b"@garbage without checksum\r\n"
         b"@P 1 500*00\r\n"                       # right shape, wrong checksum
         b"random @ in the middle of a line\r\n"
         b"\x00\xff\xfe binary junk\r\n")
p = Parser()
check("noise alone yields nothing", p.feed(noise) == [])
check("valid frame after noise is accepted", p.feed(frame("N 64 90")) == ["N 64 90"])
p = Parser()
stream = noise + frame("P 0 250") + noise + frame("O 64")
check("good frames among noise", p.feed(stream) == ["P 0 250", "O 64"])

# --- 6. arbitrary chunking (bytes arrive in dribs and drabs) -------------
print("6. chunked delivery")
rng = random.Random(7)
for trial in range(200):
    msgs = [rng.choice(bodies) for _ in range(rng.randint(1, 4))]
    stream = b"".join(frame(m) for m in msgs)
    p = Parser()
    got = []
    i = 0
    while i < len(stream):
        k = rng.randint(1, 9)
        got += p.feed(stream[i:i + k])
        i += k
    check(f"trial {trial}: {got} == {msgs}", got == msgs)

# --- 7. overflow recovery -------------------------------------------------
print("7. overflow recovery")
p = Parser()
check("junk longer than buffer is dropped", p.feed(b"Z" * 500) == [])
check("parser still works afterwards", p.feed(frame("A")) == ["A"])

print()
print(f"PASS {PASS}   FAIL {FAIL}")
sys.exit(1 if FAIL else 0)
