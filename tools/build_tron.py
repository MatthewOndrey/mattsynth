#!/usr/bin/env python3
"""Fetch Mellotron single notes from the public GitHub SFZ set and build playable sets.

Each set is five zones (C2..C6) trimmed to 2.4 seconds, which is what the Pod's arena holds.
"""
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request

BASE = "https://raw.githubusercontent.com/ExistentiaVirae/Mellotron-SFZ/HEAD/Mellotron/"
NOTES = [("C2", 36), ("C3", 48), ("C4", 60), ("C5", 72), ("C6", 84)]
INSTRUMENTS = {
    "Flute": "Tron Flute",
    "String Section": "Tron Strings",
    "8 Choir": "Tron Choir",
    "Brass": "Tron Brass",
}
SLIB = os.path.expanduser("~/slib")


def grab(folder, note):
    url = BASE + urllib.parse.quote(folder) + "/samples/" + note + ".wav"
    try:
        with urllib.request.urlopen(url, timeout=40) as r:
            return r.read()
    except Exception:
        return None


def main():
    for folder, display in INSTRUMENTS.items():
        slug = display.lower().replace(" ", "-")
        out = os.path.join(SLIB, slug)
        os.makedirs(out, exist_ok=True)
        zones = []
        for note, root in NOTES:
            wav = grab(folder, note)
            if not wav:
                print("  %-14s %s missing" % (folder, note))
                continue
            wpath = os.path.join(out, note + ".wav")
            open(wpath, "wb").write(wav)
            raw = os.path.join(out, note + ".raw")
            subprocess.run(["ffmpeg", "-y", "-loglevel", "quiet", "-i", wpath, "-ac", "1",
                            "-ar", "48000", "-t", "2.4", "-sample_fmt", "s16", "-f", "s16le", raw],
                           check=False)
            if os.path.getsize(raw) > 0:
                zones.append({"file": note + ".raw", "root": root})
            os.remove(wpath)
        if not zones:
            print("%s: nothing downloaded" % folder)
            continue
        json.dump({"name": display, "zones": zones}, open(os.path.join(out, "set.json"), "w"), indent=1)
        total = sum(os.path.getsize(os.path.join(out, z["file"])) // 2 for z in zones)
        print("%-14s -> %-14s %d zones, %d frames" % (folder, display, len(zones), total))


if __name__ == "__main__":
    main()
