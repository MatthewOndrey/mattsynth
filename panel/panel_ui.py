#!/usr/bin/env python3
"""panel_ui - touch panel for the Daisy Pod, served from the Pi itself.

The Pod's serial port already belongs to podbridge, so this talks to the bridge
on 127.0.0.1:5555: bytes written there go to the Pod, and every frame the Pod
sends is copied back. That means the ESP32 panel and this page can both drive
the same synth at once.

stdlib only. Serves:
  GET  /        the touch UI
  GET  /events  server-sent events, one Pod frame per message
  POST /send    a bare command body, e.g. "N 60 100"
"""

import http.server
import json
import os
import queue
import socket
import threading
import time
import urllib.parse

BRIDGE = ("127.0.0.1", 5555)
PORT = 8080
BANKS_DIR = os.path.expanduser("~/banks")
WT_DIR    = os.path.expanduser("~/wt")
os.makedirs(WT_DIR, exist_ok=True)
MIDI_DIR = os.path.expanduser("~/midi")
os.makedirs(MIDI_DIR, exist_ok=True)
STATE_PATH = os.path.expanduser("~/panel_state.json")

subs = []
subs_lock = threading.Lock()
bank_names = {}
bank_lock = threading.Lock()
sock = None
sock_lock = threading.Lock()


def connect():
    global sock
    while True:
        try:
            s = socket.create_connection(BRIDGE, timeout=5)
            s.settimeout(None)
            with sock_lock:
                sock = s
            print("panel_ui: bridge connected", flush=True)
            return
        except OSError:
            time.sleep(1)


def push(text):
    with subs_lock:
        for q in subs:
            try:
                q.put_nowait(text)
            except queue.Full:
                pass


# --- Rhythm Wolf: parts C (drums) and D (its bass) over USB MIDI ---------------
WOLF = {"dev": None, "notes": {}, "path": "/dev/snd/midiC2D0"}

def wolf_open():
    if WOLF["dev"] is None:
        try:
            WOLF["dev"] = open(WOLF["path"], "wb", buffering=0)
        except Exception:
            WOLF["dev"] = False
    return WOLF["dev"] or None

def wolf_write(bs):
    d = wolf_open()
    if not d:
        return
    try:
        d.write(bytes(bs))
        try:
            open("/tmp/wolf.log", "a").write(" ".join("%02X" % b for b in bs) + chr(10))
        except Exception:
            pass
    except Exception:
        WOLF["dev"] = None

def wolf_handle(body):
    """True when the frame was consumed by the Wolf (part C drums, part D bass)"""
    parts = body.split()
    if not parts:
        return False
    cmd = parts[0]
    if cmd == "N" and len(parts) >= 3:
        try:
            n = int(parts[1]); v = max(1, min(127, int(float(parts[2]))))
        except ValueError:
            return False
        try:
            p = int(parts[3]) if len(parts) > 3 else 0
        except ValueError:
            p = 0
        if p >= 2:
            ch = 9 if p == 2 else 0
            try:
                cents = float(parts[4]) if len(parts) > 4 else 0.0
            except ValueError:
                cents = 0.0
            if p == 3 and cents:
                v14 = int(round(max(-200.0, min(200.0, cents)) / 200.0 * 8192)) + 8192
                wolf_write([0xE0 | ch, v14 & 0x7F, (v14 >> 7) & 0x7F])
            wolf_write([0x90 | ch, n & 127, v])
            WOLF["notes"][(p, n)] = True
            return True
        return False
    if cmd == "O" and len(parts) >= 2:
        try:
            n = int(parts[1])
        except ValueError:
            return False
        for p in (2, 3):
            if WOLF["notes"].pop((p, n), None):
                wolf_write([0x80 | (9 if p == 2 else 0), n & 127, 0])
                if p == 3:
                    wolf_write([0xE0, 0, 64])
                return True
        return False
    if cmd == "R":
        for ch in (0, 9):
            wolf_write([0xB0 | ch, 123, 0])
        WOLF["notes"].clear()
        return False          # panic still goes to the Pod as well
    return False


def send_body(body):
    cs = 0
    raw = body.encode()
    for ch in raw:
        cs ^= ch
    frame = b"@" + raw + ("*%02X\n" % cs).encode()
    with sock_lock:
        s = sock
    if s is None:
        return
    try:
        s.sendall(frame)
    except OSError:
        pass


def reader():
    global sock
    buf = b""
    while True:
        with sock_lock:
            s = sock
        if s is None:
            time.sleep(0.3)
            connect()
            continue
        try:
            data = s.recv(4096)
        except OSError:
            data = b""
        if not data:
            with sock_lock:
                sock = None
            time.sleep(0.5)
            continue
        buf += data
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            text = line.decode("ascii", "ignore").strip()
            if text.startswith("@"):
                push(text)
                if text.startswith("@L "):
                    try:
                        idx, nm = text[3:].split("*")[0].split(" ", 1)
                        with bank_lock:
                            bank_names[int(idx)] = nm
                    except ValueError:
                        pass


def list_banks():
    try:
        return sorted(f for f in os.listdir(BANKS_DIR) if f.lower().endswith(".syx"))
    except OSError:
        return []


def _voice_name(voice):
    return voice[118:128].split(b"\x00")[0].decode("ascii", "ignore").strip()


def _send_voice(slot, voice):
    for off in range(0, 128, 32):
        send_body("B %d %d %s" % (slot, off, voice[off:off + 32].hex().upper()))
        time.sleep(0.008)


def load_bank_file(name):
    """Push a 32-voice DX7 bulk dump into the Pod, then verify and retry.

    Frames are 32-byte chunks because the Pod's line buffer is 96 bytes, and the
    link can drop one, so every slot is checked against the names in the file.
    """
    path = os.path.join(BANKS_DIR, os.path.basename(name or ""))
    if not os.path.isfile(path):
        return "no such file"
    with open(path, "rb") as fh:
        raw = fh.read()
    i = raw.find(b"\xf0\x43")
    if i < 0:
        return "no sysex header"
    if i + 6 + 4096 > len(raw):
        return "too short for 32 voices"
    if raw[i + 3] != 0x09:
        return "not a 32-voice bulk dump"
    data = raw[i + 6:i + 6 + 4096]
    want = {s: _voice_name(data[s * 128:(s + 1) * 128]) for s in range(32)}

    with bank_lock:
        bank_names.clear()

    for attempt in range(3):
        with bank_lock:
            todo = [s for s in range(32) if bank_names.get(s, "").strip() != want[s]]
        if attempt > 0 and not todo:
            break
        for s in todo:
            _send_voice(s, data[s * 128:(s + 1) * 128])
        time.sleep(0.25)
        send_body("L")
        time.sleep(0.6)

    with bank_lock:
        seen = dict(bank_names)
    if not seen:
        # the Pod writes the voices whether or not it answers L, so report what was
        # done instead of claiming failure; the page names the slots from the file
        return "pushed 32 voices, Pod sent no list"
    bad = [s for s in range(32) if seen.get(s, "").strip() != want[s]]
    if bad:
        return "loaded %d of 32, slots %s did not take" % (32 - len(bad), bad[:6])
    return "loaded 32 voices, verified"


def poller():
    n = 0
    while True:
        time.sleep(1.5)
        with subs_lock:
            live = len(subs)
        if not live:
            continue
        send_body("Q")
        n += 1
        if n % 8 == 0:
            send_body("L")  # refresh the bank list for any open page


HTML = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, user-scalable=no">
<title>Daisy Pod</title>
<style>
:root { --bg:#0d0d0d; --panel:#1b1b1b; --line:#2c2c2c; --ink:#eee; --dim:#8a8a8a;
        --acc:#2e7d32; --hot:#ffb74d; --tine:#5d4037; }
* { box-sizing:border-box; -webkit-tap-highlight-color:transparent; }
html,body { margin:0; height:100%; background:var(--bg); color:var(--ink);
            font-family:system-ui,sans-serif; overflow:hidden; user-select:none;
            touch-action:none; }
#hdr { display:flex; align-items:center; gap:24px; padding:10px 20px; height:auto;
       border-bottom:1px solid var(--line); }
#hdr h1 { font-size:22px; margin:0; font-weight:600; letter-spacing:1px; }
.stat { color:var(--dim); font-size:17px; font-variant-numeric:tabular-nums; }
#patch { color:var(--hot); font-size:19px; }
.pbtn { background:var(--panel); color:var(--ink); border:1px solid var(--line);
        border-radius:9px; padding:9px 14px; font-size:18px; }
.parts { display:flex; gap:4px; }
.parts button { background:var(--panel); color:var(--dim); border:1px solid var(--line);
                border-radius:8px; padding:8px 14px; font-size:16px; }
.parts button.on { background:var(--acc); color:#fff; border-color:var(--acc); }
select.pbtn { padding:9px 12px; font-size:17px; color:var(--hot); max-width:280px; }
#hint { margin-left:12px; color:var(--dim); font-size:14px; }
#panic { background:#7f1d1d; color:#fff; border:0; border-radius:9px;
         padding:12px 22px; font-size:17px; }
main { display:grid; grid-template-columns: minmax(0,1fr) 470px; height:calc(100% - var(--hdrh, 74px)); }
body.fxoff main { grid-template-columns: minmax(0,1fr) 0; }
body.fxoff #fx { display:none; }
#stage { padding:10px 12px; display:flex; flex-direction:column; gap:8px;
         min-height:0; min-width:0; overflow:hidden; }
#views { display:flex; gap:8px; }
#views button { background:var(--panel); color:var(--dim); border:1px solid var(--line);
                border-radius:9px; padding:9px 15px; font-size:14px; letter-spacing:.5px; }
#views button.on { background:var(--acc); color:#fff; border-color:var(--acc); }
#view { flex:1; display:flex; flex-direction:column; gap:8px; min-height:0; }
/* one finger must never be cancelled by the browser when a second lands */
#stage, #view, #chroma, #grid, #kali, #drone, #dfield, #top, #bot { touch-action:none; }
.pad, .tine, .dstone, .dvh { touch-action:none; }
.row { display:flex; gap:10px; }
.tine { flex:1; background:var(--tine); border-radius:12px; display:flex;
        align-items:flex-end; justify-content:center; padding-bottom:14px;
        font-size:20px; color:#ffe0b2; transition:background .04s; }
.tine.up { background:var(--hot); color:#3e2723; }
#top .tine { height:auto; }
#bot .tine { height:auto; }
#view .row { flex:1; min-height:0; }
#view .row:first-child { flex:1.12; }
#grid { display:grid; grid-template-columns:repeat(8,1fr); grid-auto-rows:1fr;
        gap:10px; flex:1; min-height:0; }
.pad { background:#2b2b2b; border-radius:12px; display:flex; align-items:flex-end;
       justify-content:center; padding-bottom:12px; font-size:19px; color:#cfd8dc;
       transition:background .04s; }
.pad.dark { background:#1f1f1f; color:#78909c; }
.pad.croot { background:#1f3b2a; color:#8dffbe; border:1px solid #56d78d; }
.pad.c3rd  { background:#3a2a18; color:#ffc07a; border:1px solid #b07c33; }
.pad.c5th  { background:#182735; color:#8cc7ff; border:1px solid #3f6f9e; }
.pad.c7th  { background:#2a1f3a; color:#d3b0ff; border:1px solid #6f4a9e; }
.pad.ckey  { background:#232323; color:#8a939b; }
.pad.coff  { background:#161616; color:#464d54; }
#chroma { display:grid; grid-template-columns:repeat(12, minmax(0,1fr));
          grid-auto-rows:minmax(0,1fr); gap:6px; flex:1; height:100%; min-width:0; }
#chroma .pad { min-width:0; padding:0 2px; }
.pad.up { background:var(--hot); color:#3e2723; }
#seq { display:flex; flex-direction:column; gap:8px; height:100%; }
.seqbar { flex-wrap: nowrap; gap: 4px; display:flex; gap:10px; align-items:center; }
.seqbar button { background:var(--panel); color:var(--ink); border:1px solid var(--line);
                 border-radius:9px; padding:10px 18px; font-size:16px; }
.seqbar button.on { background:var(--acc); border-color:var(--acc); color:#fff; }
.seqbar .lab { white-space: nowrap; color:var(--dim); font-size:15px; }
.trk { display:flex; gap:10px; align-items:stretch; flex:1; min-height:0; }
.trk .meta { width:170px; display:flex; flex-direction:column; gap:5px; justify-content:center; }
.trk .meta .row2 { display:flex; gap:6px; align-items:center; }
.trk .meta button { background:var(--panel); color:var(--ink); border:1px solid var(--line);
                    border-radius:7px; padding:8px 0; font-size:15px; flex:1; }
.trk .meta button.on { background:#7f1d1d; border-color:#7f1d1d; }
.trk .meta .nm { color:var(--hot); font-size:17px; text-align:center; flex:1.6; }
.trk .meta input[type=range] { height:22px; }
.steps { display:grid; grid-template-columns:repeat(16,1fr); gap:6px; flex:1; }
.st { background:#232323; border-radius:9px; border:1px solid #2e2e2e; }
.st.beat { background:#1b1b1b; }
.st.on { background:#2e7d32; border-color:#3d8b40; }
.st.now { border-color:var(--hot); box-shadow:inset 0 0 0 3px rgba(255,183,77,.55); }
#roll { display:grid; grid-template-columns:70px repeat(16,1fr); grid-auto-rows:1fr;
        gap:4px; flex:1; min-height:0; }
.rlab { color:var(--dim); font-size:13px; display:flex; align-items:center;
        justify-content:flex-end; padding-right:6px; }
.rc { background:#232323; border:1px solid #2c2c2c; border-radius:6px; }
.rc.on { background:#2e7d32; border-color:#3d8b40; }
.rc.now { box-shadow:inset 0 0 0 2px var(--hot); }
.num { width:52px; height:26px; background:#242424; color:#ddd; border:1px solid var(--line);
       border-radius:6px; font-size:14px; text-align:center; padding:0; }
#xywrap { display:flex; gap:18px; flex:1; min-height:0; }
#xy { flex:1; background:#151515; border:1px solid var(--line); border-radius:14px;
      position:relative; touch-action:none; }
#xydot { position:absolute; width:48px; height:48px; margin:-24px 0 0 -24px; border-radius:50%;
         background:rgba(46,125,50,.45); border:2px solid #4caf50; pointer-events:none;
         left:50%; top:50%; }
#xymacros { width:330px; display:flex; flex-direction:column; justify-content:center; gap:8px; }
#algwrap { display:flex; flex-direction:column; gap:8px; flex:1; min-height:0; }
#alggrid { display:grid; grid-template-columns:repeat(8,1fr); grid-auto-rows:1fr; gap:6px;
           flex:1; min-height:0; }
.algcard { background:#1a1a1a; border:1px solid #2c2c2c; border-radius:8px; padding:2px;
           position:relative; }
.algcard.on { border-color:var(--hot); background:#20261f; }
.algtag { position:absolute; right:5px; top:2px; color:#777; font-size:11px; }
#opswrap { display:flex; flex-direction:column; gap:6px; flex:1; min-height:0; overflow-y:auto; }
#opstbl { display:grid; grid-template-columns:90px repeat(6,1fr); gap:5px; align-content:start; }
.opsgrp { grid-column:1 / -1; color:var(--hot); font-size:12px; letter-spacing:.14em;
          margin-top:10px; border-bottom:1px solid #2c2c2c; padding-bottom:3px; }
.opscell { display:flex; flex-direction:column; align-items:center; gap:2px; padding:5px 0; }
.opbadge { font-size:11px; letter-spacing:.08em; padding:1px 6px; border-radius:8px; }
.opbadge.car { background:#1d3320; color:#7fd18b; border:1px solid #35603c; }
.opbadge.mod { background:#332512; color:#e0a458; border:1px solid #63451f; }
.opmini { width:100%; height:26px; display:block; }
.opsrow2 { display:flex; gap:4px; }
.opsrow2 button { font-size:11px; padding:1px 7px; height:22px; border-radius:6px; }
.opsband .num { background:#242424; }
#opsdet { display:flex; flex-direction:column; gap:10px; height:100%; min-height:0; }
#openv { flex:0 0 34%; min-height:190px; background:#171717; border:1px solid #2a2a2a;
         border-radius:12px; touch-action:none; }
#opknobs { display:flex; flex-wrap:wrap; gap:10px 14px; align-content:center;
           justify-content:center; overflow-y:auto; flex:1; min-height:0; }
.opkn { width:132px; display:flex; flex-direction:column; align-items:center; gap:1px;
        background:#1b1b1b; border:1px solid #292929; border-radius:12px; padding:8px 4px 6px;
        touch-action:none; user-select:none; }
.opkn .kt { font-size:13px; letter-spacing:.1em; color:var(--dim); }
.opkn .kv { font-size:19px; color:#eee; }
.opkn .kdial { position:relative; width:92px; height:92px; }
.opkn svg { width:92px; height:92px; transform:rotate(135deg); }
.opkn .ktr { fill:none; stroke:#2e2e2e; stroke-width:7; }
.opkn .karc { fill:none; stroke-width:7; stroke-linecap:round; }
.opkn .kptr { position:absolute; left:50%; top:12%; width:2px; height:26%; margin-left:-1px;
              background:#e8e8e8; transform-origin:50% 146%; border-radius:2px; }
.opkn.grp { border-color:#3a3226; }
#wtwrap { display:flex; flex-direction:column; gap:8px; height:100%; min-height:0; }
#wtplot { flex:0 0 24%; min-height:140px; background:#151515; border:1px solid #262626;
          border-radius:12px; position:relative; }
#wtplot .cap { position:absolute; left:12px; top:8px; font-size:12px; letter-spacing:.12em;
               color:var(--dim); }
#wtknobs { display:flex; flex-wrap:wrap; gap:8px 12px; align-content:flex-start;
           overflow-y:auto; flex:0 0 auto; }
#wtpresets { display:flex; flex-wrap:wrap; gap:6px; }
#wtpresets button { padding:8px 14px; border-radius:9px; }
.opkn.modded { border-color:#4dd0e1 !important; }
.opkn .kband { fill:none; stroke:#4dd0e1; stroke-width:10; opacity:.45; }
.opkn .kdot { position:absolute; width:10px; height:10px; border-radius:50%;
              background:#d6fbff; box-shadow:0 0 8px #4dd0e1; pointer-events:none; }
#wtmods { display:flex; gap:8px; align-items:center; }
.modchip { padding:9px 16px; border-radius:10px; background:#123; border:1px solid #2b6f7d;
           color:#8fe3f2; font-size:13px; letter-spacing:.1em; touch-action:none; }
.modchip.on { background:#1d4d59; color:#e8fbff; }
.modchip.armed { outline:2px solid #fff; background:#1d4d59; }
.opkn .kx { position:absolute; top:-4px; right:-4px; display:flex; gap:2px; z-index:3; }
.opkn .kx i { width:15px; height:15px; border-radius:50%; font-style:normal; font-size:11px;
              line-height:15px; text-align:center; color:#04121a; cursor:pointer; font-weight:700; }
.modchip.drag { opacity:.6; }
button.onhold { background:#2f6b3a !important; color:#fff !important; }
/* the note the bend has snapped to: thick cyan ring so you can see the target */
.lockon { box-shadow: inset 0 0 0 4px #4dd0e1, 0 0 20px rgba(77,208,225,.65) !important;
          z-index: 4 !important; }
.lockon::after { content: "LOCK"; position: absolute; left: 50%; bottom: 6px;
                 transform: translateX(-50%); font-size: 11px; letter-spacing: .12em;
                 color: #041014; background: #4dd0e1; padding: 1px 6px; border-radius: 6px; }
#bpmwrap { display:flex; align-items:center; gap:6px; }
#bpm { width:64px; height:30px; background:#1c1c1c; color:#eee; border:1px solid #333;
       border-radius:8px; font-size:15px; text-align:center; }
#gplay.on { background:#2f6b3a; color:#fff; }
#abslots { display:flex; gap:4px; }
#abslots .slot { font: 600 13px system-ui; padding: 5px 9px; border-radius: 8px;
  border:1px solid #2a3238; background:#12181c; color:#9fb2bd; max-width:190px;
  overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
#abslots .slot.on { border-color:#4dd0e1; color:#e8fbff; background:#123038; }
#hdr { flex-wrap: wrap; row-gap: 6px; height: auto; padding: 10px 18px; }
.hrow { display: contents; }
#hdr h1 { font-size: 19px; }
.hrow { display: flex; align-items: center; gap: 8px; min-width: 0; }
.hrow > * { flex: 0 0 auto; }
.hrow .hspacer { flex: 1 1 auto; }
#hr1 #patchsel { flex: 0 1 240px; min-width: 150px; }
#hr2 #hmidi { flex: 0 1 260px; }
#hdr h1 { margin: 0 10px 0 0; font-size: 20px; letter-spacing: .06em; }
#bankmsg, #algfb { font-size: 12px; color: #7f949f; max-width: 90px; overflow: hidden;
                   text-overflow: ellipsis; white-space: nowrap; }
#volwrap { flex: 0 0 auto; display: flex; align-items: center; gap: 6px; }
#volwrap input[type=range] { width: 80px; }
#bpm { width: 56px; }
#volwrap { margin-left:auto; display:flex; align-items:center; gap:8px; padding:0 6px; }
#volwrap .lab { white-space: nowrap; font-size:12px; letter-spacing:.12em; }
#vol { width:170px; height:26px; accent-color:#4caf50; }
#volv { min-width:38px; text-align:right; font-size:14px; color:#ddd; }
#bankmsg { flex:0 0 auto; max-width:none; font-size:13px; color:#9fd8a4; }
.opsh { color:var(--dim); font-size:14px; text-align:center; padding:4px 0; }
.opsr { color:var(--dim); font-size:14px; display:flex; align-items:center; }
#opstbl .num { width:100%; height:34px; }
#clipgrid { display:grid; grid-template-columns:repeat(4,1fr); grid-auto-rows:1fr;
            gap:10px; flex:1; min-height:0; }
.clip { background:#1c1c1c; border:1px solid #2c2c2c; border-radius:12px;
        display:flex; flex-direction:column; justify-content:space-between; padding:12px; }
.clip.on { background:#20301f; border-color:#4caf50; }
.clip.queued { border-color:var(--hot); }
.clip.now { box-shadow:inset 0 0 0 3px rgba(255,183,77,.3); }
.clipnum { color:#cfd8dc; font-size:22px; }
.clipdots { color:#4caf50; font-size:18px; letter-spacing:3px; }
.rlab.tap { cursor:pointer; border-radius:6px; }
.rlab.tap.up { background:var(--hot); color:#3e2723; }
#ctl { display:flex; gap:6px; align-items:center; }
#hdr #ctl button { font-size:12px; padding:4px 10px; border-radius:7px; }
#ctl button { flex:1; background:var(--panel); color:var(--ink); border:1px solid var(--line);
              border-radius:10px; padding:13px 0; font-size:16px; }
#ctl button.on { background:var(--acc); border-color:var(--acc); }
#drone { display:flex; flex-direction:column; height:100%; }
#dfield { position:relative; flex:1; margin:10px 4px 6px; border-radius:16px;
          background:radial-gradient(circle at 50% 45%, #171a20 0%, #0a0b0e 72%);
          border:1px solid var(--line); overflow:hidden; touch-action:none; }
#dcore { position:absolute; width:150px; height:150px; border-radius:50%; pointer-events:none;
         background:radial-gradient(circle, rgba(120,255,170,.50) 0%, rgba(60,200,140,.16) 42%, transparent 70%);
         transform:translate(-50%,-50%); }
.dveil { position:absolute; border:1px solid rgba(120,255,170,.30); border-radius:50%; pointer-events:none; }
.dvh { position:absolute; width:34px; height:34px; margin:-17px 0 0 -17px; border-radius:50%;
       background:var(--acc); opacity:.85; touch-action:none; box-shadow:0 0 14px rgba(120,255,170,.5); }
.dstone { position:absolute; width:70px; height:70px; border-radius:50%; touch-action:none;
          background:rgba(255,255,255,.05); border:1px solid rgba(120,255,170,var(--a,.55));
          display:flex; align-items:center; justify-content:center; }
.dstone .dn { font-size:15px; color:var(--ink); }
.dstone.up { background:rgba(120,255,170,.22); }
#drone .seqbar { flex-wrap:wrap; }
#dbody { display:flex; gap:10px; flex:1; min-height:0; }
#dfield { flex:1; min-width:0; margin:0; height:auto; }
#dlfo { width:330px; display:flex; flex-direction:column; gap:8px; overflow-y:auto; }
.lp { background:#12141a; border:1px solid var(--line); border-radius:12px;
      padding:6px 7px; display:flex; flex-direction:column; gap:3px; }
.lp.off { opacity:.45; }
.lp .lph { display:flex; align-items:center; justify-content:space-between; }
.lp .lph b { font-size:13px; color:var(--ink); letter-spacing:.08em; }
.lp .shapes { display:grid; grid-template-columns:repeat(3,1fr); gap:4px; }
.lp .shapes button { padding:5px 0; font-size:10px; border-radius:7px; background:var(--panel);
                     color:var(--dim); border:1px solid var(--line); letter-spacing:.04em; }
.lp .shapes button.on { background:var(--acc); color:#06210f; border-color:var(--acc); }
.lp .knobs { display:flex; justify-content:space-around; padding:2px 0; }
.kw { display:flex; flex-direction:column; align-items:center; gap:3px; }
.knob { position:relative; width:44px; height:44px; border-radius:50%; touch-action:none;
        background:radial-gradient(circle at 38% 32%, #3d434c, #13151a 72%);
        border:1px solid #2c313a; box-shadow:0 2px 6px rgba(0,0,0,.5); }
.knob .dial { position:absolute; inset:0; }
.knob .ptr { position:absolute; left:50%; top:9%; width:3px; height:38%; margin-left:-1.5px;
             border-radius:2px; background:var(--acc); }
.klab { font-size:10px; color:var(--dim); letter-spacing:.06em; }
.kval { font-size:10px; color:var(--ink); }
.lchips { display:flex; flex-wrap:wrap; gap:3px; }
.lchips .chip { background:rgba(120,255,170,.14); color:#9dffc6; border:1px solid #2f6b4a;
                border-radius:5px; padding:2px 6px; font-size:10px; }
.lchips .lnone { font-size:10px; color:var(--dim); }
.lp select { width:100%; background:var(--panel); color:var(--ink); border:1px solid var(--line);
             border-radius:7px; padding:4px; font-size:11px; }
.drack { display:flex; gap:5px; padding:8px 4px 2px; }
.pf { flex:1; display:flex; flex-direction:column; align-items:center; gap:5px; min-width:0; }
.pdot { width:18px; height:18px; border-radius:50%; border:1px solid var(--line);
        background:var(--panel); padding:0; }
.pdot.on { background:var(--acc); border-color:var(--acc); box-shadow:0 0 10px rgba(120,255,170,.6); }
.pslot { position:relative; flex:1; width:100%; min-height:68px; border-radius:9px;
         background:#11131a; border:1px solid var(--line); touch-action:none;
         overflow:hidden; cursor:ns-resize; }
.pfill { position:absolute; left:0; right:0; bottom:0; background:rgba(120,255,170,.20); }
.pthumb { position:absolute; left:1px; right:1px; height:6px; border-radius:3px;
          background:var(--acc); }
.plab { font-size:11px; color:var(--dim); white-space:nowrap; }
.pval { font-size:12px; color:var(--ink); }
#fx { border-left:1px solid var(--line); padding:16px 20px; overflow-y:auto;
      min-width:0; overflow-x:hidden; }
.fxrow { margin-bottom:8px; }
.gmrow { display:flex; align-items:center; gap:10px; }
.gmrow label { flex:1; }
.pbtn.small { padding:6px 8px; font-size:14px; width:44px; min-width:44px; text-align:center; }
#midipart { width:74px; min-width:74px; }
#ksplit   { width:74px;  min-width:74px; }
#dragmode { width:44px;  min-width:44px; }
#gplay    { width:74px;  min-width:74px; }
#hload    { width:64px;  min-width:64px; }
#bpm      { width:56px; }
#splitlab { display:inline-block; width:86px; text-align:center; font-size:12px; }
.fxrow label { display:flex; justify-content:space-between; font-size:15px; color:var(--dim);
               margin-bottom:3px; }
.fxrow label b { color:var(--ink); font-weight:500; }
.rng { display:inline-flex; gap:6px; }
.rng input { width:56px; height:26px; background:#242424; color:#ddd;
             border:1px solid var(--line); border-radius:6px; font-size:14px;
             text-align:center; padding:0; }
input[type=range] { width:100%; height:28px; background:transparent; }
input[type=range]::-webkit-slider-runnable-track { height:9px; background:#333; border-radius:5px; }
input[type=range]::-webkit-slider-thumb { -webkit-appearance:none; width:30px; height:30px;
       margin-top:-11px; border-radius:50%; background:var(--acc); border:2px solid #4caf50; }
#knobs { display:grid; grid-template-columns:minmax(0,1fr) minmax(0,1fr); gap:12px 6px;
         margin-bottom:10px; }
.kcell { display:flex; flex-direction:column; align-items:center; gap:5px; min-width:0; }
/* microtonal grid: one cell per key, hue by scale degree */
#micro { flex:1; display:flex; flex-direction:column; gap:10px; min-height:0; padding:4px 6px; }
#mrow { display:flex; flex-direction:column; gap:8px; flex:1; min-height:0; }
.mrow { display:flex; gap:8px; flex:1; min-height:0; }
.mkey { flex:1 1 0; border-radius:12px; display:flex; flex-direction:column;
        align-items:center; justify-content:center; gap:2px; border:1px solid #2c2c2c;
        color:#111; font-weight:700; font-size:20px; touch-action:none;
        text-shadow:none; min-width:0; }
.mkey small { font-size:13px; font-weight:600; opacity:.72; }
.mkey.up { outline:4px solid #fff; }
.mkey.pb { filter: saturate(.35) brightness(.55); box-shadow: inset 0 0 0 4px #ffb74d; opacity:.85; }
#mrow.hex { position:relative; flex:1; min-height:0; }
#mrow.hex .mkey { position:absolute; border:none; border-radius:0;
                  clip-path:polygon(50% 0%,100% 25%,100% 75%,50% 100%,0 75%,0 25%); }
/* --- press feedback: a held key glows, and every hit throws a spark burst ---- */
@keyframes heldPulse {
  0%,100% { filter: drop-shadow(0 0 6px rgba(255,255,255,.55)) brightness(1.12); }
  50%     { filter: drop-shadow(0 0 18px rgba(255,255,255,.95)) brightness(1.32); }
}
@keyframes sparkRing {
  0%   { transform: scale(.5); opacity: 1;
         border-width: 6px;
         box-shadow: 0 0 26px 10px hsl(var(--h,50) 100% 68%),
                    inset 0 0 22px hsl(var(--h,50) 100% 75%); }
  70%  { opacity: .65; }
  100% { transform: scale(3.6); opacity: 0; border-width: 1px;
         box-shadow: 0 0 4px 0 transparent; }
}
@keyframes sparkCore {
  0%   { transform: scale(.6); opacity: 1; }
  100% { transform: scale(2.6); opacity: 0; }
}
@keyframes sparkDot {
  0%   { transform: translate(0,0) scale(1); opacity: 1; }
  100% { transform: translate(var(--dx),var(--dy)) scale(.2); opacity: 0; }
}
.mkey.mplay {
  box-shadow: inset 0 0 0 5px #fff, inset 0 0 28px rgba(255,255,255,.5);
  filter: brightness(1.5) drop-shadow(0 0 16px rgba(255,255,255,.95));
  z-index: 4;
}
.mkey.up, .pad.up, .tine.up, .dstone.up, .dvh.up {
  animation: heldPulse .75s ease-in-out infinite; z-index: 3;
  transform: scale(.97);
}
@keyframes trailFade {
  0%   { transform: scale(1);   opacity: .85; }
  100% { transform: scale(.15); opacity: 0; }
}
.btrail { position: fixed; width: 18px; height: 18px; margin: -9px 0 0 -9px;
          border-radius: 50%; pointer-events: none; z-index: 9998;
          background: radial-gradient(circle, hsl(var(--h,190) 100% 78%) 0%,
                      hsl(var(--h,190) 100% 60% / .55) 45%, transparent 72%);
          animation: trailFade .5s ease-out forwards; }
.spark { position: fixed; width: 26px; height: 26px; margin: -13px 0 0 -13px; border-radius: 50%;
         pointer-events: none; z-index: 9999; border: 6px solid hsl(var(--h,50) 100% 74%);
         animation: sparkRing .55s cubic-bezier(.2,.7,.3,1) forwards; }
.score { position: fixed; width: 22px; height: 22px; margin: -11px 0 0 -11px; border-radius: 50%;
         pointer-events: none; z-index: 9999;
         background: radial-gradient(circle, #fff 0%, hsl(var(--h,50) 100% 78%) 55%, transparent 72%);
         animation: sparkCore .3s ease-out forwards; }
.sdot  { position: fixed; width: 9px; height: 9px; margin: -4.5px 0 0 -4.5px; border-radius: 50%;
         pointer-events: none; z-index: 9999; background: hsl(var(--h,50) 100% 78%);
         box-shadow: 0 0 14px 2px hsl(var(--h,50) 100% 70%);
         animation: sparkDot .5s cubic-bezier(.2,.7,.3,1) forwards; }

/* keys that belong to a chord a finger is holding */
.chordlit { box-shadow: inset 0 0 0 3px #ffb74d !important; }
.ksteps { display:flex; gap:4px; width:100%; }
.ksteps button { flex:1 1 0; min-height:38px; min-width:0; padding:0; font-size:14px;
                 border-radius:8px; touch-action:manipulation; }
.ksteps button.on { background:#2e7d32; color:#fff; border-color:#2e7d32; }
.kt { font-size:11px; color:var(--dim); letter-spacing:.07em; }
.kv { font-size:13px; color:var(--ink); }
.kdial { position:relative; width:64px; height:64px; touch-action:none; cursor:ns-resize; }
.kdial svg { position:absolute; inset:0; width:100%; height:100%; transform:rotate(135deg); }
.ktr { fill:none; stroke:#232930; stroke-width:4; stroke-linecap:round; stroke-dasharray:122.5 40.9; }
.karc { fill:none; stroke:var(--acc); stroke-width:4; stroke-linecap:round; }
/* the fx dials need an explicit size: an SVG with only a viewBox grows to its cell */
#knobs .kdial { width: 56px; height: 56px; }
#knobs .kdial svg { width: 56px; height: 56px; display: block; }
.kdial .kptr { position:absolute; left:50%; top:50%; width:3px; height:22px; margin-left:-1.5px;
               margin-top:-22px; background:#e6edf2; border-radius:2px; transform-origin:50% 100%; }
.krng { display:flex; gap:4px; }
.krng input { width:46px; height:22px; background:#242424; color:#ddd; border:1px solid var(--line);
              border-radius:5px; font-size:11px; text-align:center; padding:0; }
#divs { display:grid; grid-template-columns:repeat(4,1fr); gap:7px; margin:4px 0 8px; }
#divs button { background:var(--panel); color:var(--ink); border:1px solid var(--line);
               border-radius:9px; padding:11px 0; font-size:16px; }
#divs button.on { background:var(--acc); border-color:var(--acc); }
#readout { color:var(--dim); font-size:15px; margin-bottom:8px; }
</style></head><body>
<div id="hdr">
  <div class="hrow" id="hr1">
    <h1>MattSynth</h1>
    <div class="stat" id="voices">-</div>
    <div class="stat" id="cpu">-</div>
    <div class="stat" id="fingers">0 fingers</div>
    <div id="partcd">
    <button class="slot" id="partC" title="Rhythm Wolf drums">C: drums</button>
    <button class="slot" id="partD" title="Rhythm Wolf bass">D: bass</button>
  </div>
  <div id="abslots">
      <button class="slot" id="slotA" data-p="0">A: init</button>
      <button class="slot" id="slotB" data-p="1">B: init</button>
    </div>
    <button id="pprev" class="pbtn">&lsaquo;</button>
    <select id="patchsel" class="pbtn"></select>
    <button id="pnext" class="pbtn">&rsaquo;</button>
    <button id="engsel" class="pbtn" title="oscillator engine">FM</button>
    <select id="banksel" class="pbtn" title="bank file"></select>
    <button id="bankload" class="pbtn">LOAD</button>
    <div class="stat" id="bankmsg">-</div>
    <select id="algsel" class="pbtn"></select>
    <div class="stat" id="algfb">-</div>
  </div>
  <div class="hrow" id="hr2">
    <span class="lab">MIDI</span>
    <select id="hmidi" class="pbtn" style="min-width:170px; max-width:260px"></select>
    <button id="hload" class="pbtn">LOAD</button>
    <button id="gplay" class="pbtn">PLAY</button>
    <div id="bpmwrap"><span class="lab">BPM</span>
      <input id="bpm" type="number" min="60" max="200" step="1" value="120"></div>
    <div id="volwrap">
      <span class="lab">VOL</span>
      <input id="vol" type="range" min="0" max="100" value="100">
      <b id="volv">100</b>
    </div>
    <div id="ctl">
      <button id="octdn">&minus; OCT</button>
      <button id="keyb">KEY C</button>
      <button id="typeb">TYPE maj</button>
      <button id="chordb">CHORD off</button>
      <button id="sustb">SUSTain off</button>
      <button id="bendb">BEND off</button>
      <button id="octup">+ OCT</button>
    </div>
    <div class="hspacer"></div>
    <button id="fxtog">HIDE KNOBS</button>
    <button id="panic">PANIC</button>
  </div>
</div>
<main>
  <section id="stage">
    <div id="views">
      <button data-v="kali" class="on">KALIMBA</button>
      <button data-v="drone">DRONE</button>
      <button data-v="wt">WT</button>
      <button data-v="grid">GRID</button>
      <button data-v="chroma">CHROMA</button>
      <button data-v="micro">MICRO</button>
      <button data-v="seq">SEQ</button>
      <button data-v="roll">PIANO</button>
      <button data-v="arp">ARP</button>
      <button data-v="euclid">EUCLID</button>
      <button data-v="xy">XY</button>
      <button data-v="alg">ALG</button>
      <button data-v="ops">OPS</button>
      <button data-v="rand">RAND</button>
      <button data-v="clips">CLIPS</button>
      <span id="hint">pads: y = velocity, drag = filter</span>
    </div>
    <div id="view"></div>
  </section>
  <section id="fx">
    <div id="knobs"></div>
    <div id="divs"></div>
    <div id="readout"></div>
    <div class="fxrow gmrow"><label>play buttons <b id="gmodelab">per tab</b></label>
      <button id="gmode" class="pbtn small">PER TAB</button></div>
  </section>
</main>
<script>
var engWT = false;
var wtPart = 0;
const NOTE = ["C","C#","D","D#","E","F","F#","G","G#","A","A#","B"];
const DIVN = ["1/16","1/8T","1/8","1/4T","1/4","1/2T","1/2","1/1"];
const DIVB = [0.25,1/3,0.5,2/3,1,4/3,2,4];
/* ---------------------------------------------------------------- knobs --- */
/* The right column is a bank of synth knobs. Each one keeps a hidden range
   input with the old id, so every handler and every P send still works; the
   coloured arc is just a readout of that input's value. */
const FXK = [
  {id:"p0",  lab:"CUTOFF",    min:0,  max:1000, def:350, rng:true},
  {id:"p1",  lab:"RESONANCE", min:0,  max:1000, def:550, rng:true},
  {id:"p2",  lab:"FM FB",     min:0,  max:1000, def:400},
  {id:"p6",  lab:"CHORUS",    min:0,  max:1000, def:300},
  {id:"p7",  lab:"DELAY MIX", min:0,  max:1000, def:250},
  {id:"p11", lab:"D. FBACK",  min:0,  max:1000, def:350},
  {id:"p12", lab:"TEMPO",     min:60, max:200,  def:120, unit:" bpm"},
  {id:"p9",  lab:"SPREAD",    min:0,  max:990,  def:300, bi:true},
  {id:"p10", lab:"PAN",       min:0,  max:990,  def:500, bi:true},
  {id:"p5",  lab:"VOICES",    min:1,  max:12,   def:8},   /* 12 is the engine cap, 8 is the safe default */
  {id:"pB",  lab:"BEND",      min:50, max:2400, def:1200, unit:" c"}  /* engine clamps T at 2400 */,
  {id:"pC",  lab:"CHORD",     min:2,  max:6,    def:3,    unit:" n", steps:true},
  {id:"pS",  lab:"SPACE",     min:1,  max:4,    def:1,    unit:" x", steps:true}
];
const KARC_C = 2 * Math.PI * 26;          /* circumference of the r=26 circle */
const knobPaint = {};

(function buildKnobs(){
  const box = document.getElementById("knobs");
  if(!box) return;
  FXK.forEach(k=>{
    const n   = k.id.slice(1);
    const cell = document.createElement("div"); cell.className = "kcell";
    const t   = document.createElement("div"); t.className = "kt"; t.textContent = k.lab;
    const dial = document.createElement("div"); dial.className = "kdial";
    dial.innerHTML = '<svg viewBox="0 0 64 64">' +
      '<circle class="ktr" cx="32" cy="32" r="26"/>' +
      '<circle class="karc" cx="32" cy="32" r="26"/></svg>' +
      '<div class="kptr"></div>';
    dial.style.width = "56px"; dial.style.height = "56px";
    const dsvg = dial.querySelector("svg");
    if(dsvg){ dsvg.setAttribute("width", "56"); dsvg.setAttribute("height", "56"); }

    const val = document.createElement("b"); val.id = "v" + n; val.className = "kv";
    const inp = document.createElement("input");
    inp.type = "range"; inp.id = k.id;
    inp.min = k.min; inp.max = k.max; inp.value = k.def;
    inp.style.display = "none";
    cell.appendChild(t); cell.appendChild(dial); cell.appendChild(val); cell.appendChild(inp);

    if(k.rng){
      const r = document.createElement("div"); r.className = "krng";
      const lo = document.createElement("input"); lo.type = "number"; lo.id = "lo" + n;
      lo.min = k.min; lo.max = k.max; lo.value = k.min;
      const hi = document.createElement("input"); hi.type = "number"; hi.id = "hi" + n;
      hi.min = k.min; hi.max = k.max; hi.value = k.max;
      r.appendChild(lo); r.appendChild(hi); cell.appendChild(r);
    }
    if(k.steps){
      /* a four or five value knob is hopeless with a finger: give it buttons */
      const row = document.createElement("div"); row.className = "ksteps";
      for(let v = k.min; v <= k.max; v++){
        const b = document.createElement("button");
        b.textContent = v; b.dataset.v = v;
        b.onclick = ()=>{ if(+inp.value === v) return;
                          inp.value = v; paint(); inp.dispatchEvent(new Event("input")); };
        row.appendChild(b);
      }
      cell.appendChild(row);
    }
    box.appendChild(cell);

    const arc = dial.querySelector(".karc");
    const ptr = dial.querySelector(".kptr");
    const paint = ()=>{
      const f = Math.max(0, Math.min(1, (+inp.value - k.min) / (k.max - k.min)));
      const span = 0.75 * KARC_C;
      let a, b, dev;
      if(k.bi){
        /* grow out from twelve o'clock, which is the middle of the sweep */
        a = Math.min(f, 0.5) * span;
        b = Math.max(f, 0.5) * span;
        dev = Math.abs(f - 0.5) * 2;
      } else {
        a = 0;
        b = f * span;
        dev = f;
      }
      const L = b - a;
      arc.style.strokeDasharray = L.toFixed(2) + " " + (KARC_C - L + 1).toFixed(2);
      arc.style.strokeDashoffset = (-a).toFixed(2);
      arc.style.stroke = "hsl(" + (160 - 150 * dev).toFixed(0) + ",72%,56%)";
      ptr.style.transform = "rotate(" + (-135 + f * 270).toFixed(1) + "deg)";
      val.textContent = inp.value + (k.unit || "");
      k.shown = inp.value;
      if(k.steps)
        cell.querySelectorAll(".ksteps button").forEach(b=>b.classList.toggle("on", +b.dataset.v === +inp.value));
    };
    knobPaint[k.id] = paint;
    paint();

    let drag = false, lastY = 0;
    dial.addEventListener("pointerdown", ev=>{
      ev.preventDefault(); drag = true; lastY = ev.clientY;
      try { dial.setPointerCapture(ev.pointerId); } catch(e) {}
    });
    dial.addEventListener("pointermove", ev=>{
      if(!drag) return;
      const dy = lastY - ev.clientY;
      lastY = ev.clientY;
      const step = (k.max - k.min) / 170;      /* 170 px of travel is the full range */
      const nv = Math.round(Math.max(k.min, Math.min(k.max, (+inp.value) + dy * step)));
      if(nv === +inp.value) return;            /* at the stop: do not spam the Pod */
      inp.value = nv;
      paint();
      inp.dispatchEvent(new Event("input"));
    });
    const up = ()=>{ drag = false; };
    dial.addEventListener("pointerup", up);
    dial.addEventListener("pointercancel", up);
    dial.addEventListener("lostpointercapture", up);
  });

  /* the Pod and the drone field also write these inputs directly, so poll */
  setInterval(()=>{
    FXK.forEach(k=>{
      const i = document.getElementById(k.id);
      if(i && i.value !== k.shown) knobPaint[k.id]();
    });
  }, 200);
})();

const SCALE = [[0,2,4,5,7,9,11],[0,2,3,5,7,8,10],[0,2,4,5,7,9,10],[0,2,4,5,7,9,11],
               [0,2,3,5,7,9,10],[0,2,4,5,7,9,11],[0,2,3,5,6,8,9]];
const CHOFF = [[0,2,4,-1],[0,2,4,-1],[0,2,4,6],[0,2,4,6],[0,2,4,6],[0,3,4,-1],[0,2,4,-1]];
const TYPEN = ["maj","min","7","maj7","min7","sus4","dim"];

let keySel=0, typeSel=0, oct=0, chord=false, sust=false, divSel=4, bpm=120;
/* pads bend pitch with finger height instead of sweeping the filter */
let bendMode=false;
let bendRange=1200;   /* cents at full deflection, set by the BEND knob */
let chordN=3;         /* how many notes a pad plays with CHORD on, set by the CHORD knob */
let chordSpace=1;     /* multiplies the chord type gaps: maj 0-2-4 at 2 becomes 0-4-8 */
/* microtonal player: EDO, layout and root */
const micro = { edo:24, layout:"bosa", root:0, size:"big" };
let fxHidden = false;   /* the right column can collapse so a player gets the width */
let part = 0;   /* which timbre the pads, roll and edit verbs act on */
const fingers = new Map();   // pointerId -> the exact notes that finger started

function setPart(p){
  part = p | 0;
  if(part < 2) send("P 17 " + part);   /* C and D live on the Wolf, not the Pod */
  const a = document.getElementById("partA"), b = document.getElementById("partB");
  if(a) a.classList.toggle("on", part === 0);
  if(b) b.classList.toggle("on", part === 1);
  const c = document.getElementById("partC"), d = document.getElementById("partD");
  if(c) c.classList.toggle("on", part === 2);
  if(d) d.classList.toggle("on", part === 3);
}

function send(body){
  fetch("/send", {method:"POST", body:body}).catch(()=>{});
}
function scalePitch(step){
  const o=Math.floor(step/7), d=step%7;
  return 12*o + SCALE[typeSel][d];
}
function padNote(step){
  let n = 60 + keySel + oct*12 + scalePitch(step);
  return Math.max(0, Math.min(127, n));
}
function padChord(step){
  /* chordN decides the size: 2 is root+third, 5 is the triad plus the root and
     third an octave up. Notes wrap with +12 so any size works on any type. */
  const q = CHOFF[typeSel].filter(o=>o>=0);
  const out = [];
  let last = -1000;
  for(let i = 0; i < chordN; i++){
    /* spacing widens every gap the chord type defines, so the maj/min/7 shape
       survives: maj is 0-2-4 and at spacing 2 that is 0-4-8. Extra notes past
       the type walk up by octaves until they are above the note before them,
       otherwise a wide spacing puts the fifth-octave root underneath the fifth. */
    let iv = q[i % q.length] * chordSpace + 7 * Math.floor(i / q.length);
    while(iv <= last) iv += 7;
    out.push(padNote(step + iv));
    last = iv;
  }
  return out;
}

let view = new URLSearchParams(location.search).get("view") || "kali";

function touchY(ev, el){
  const r = el.getBoundingClientRect();
  return Math.min(1, Math.max(0, (ev.clientY - r.top) / r.height));
}
function touchX(ev, el){
  const r = el.getBoundingClientRect();
  return Math.min(1, Math.max(0, (ev.clientX - r.left) / r.width));
}
/* Bending with the whole screen: the strip follows the finger 1:1, but a tine
   the finger actually rests on wins over it. A tine only wins after BEND_DWELL
   milliseconds, so a fast slide across the board never stumbles on the notes
   it passes: the pitch holds the last locked note until the next dwell. */
const BEND_DWELL = 380;   /* a finger has to actually settle before the pitch snaps */
function bendHover(ev, f){
  f.lx = ev.clientX; f.ly = ev.clientY;
  const el = document.elementFromPoint(ev.clientX, ev.clientY);
  const note = (el && el.dataset && el.dataset.note !== undefined) ? +el.dataset.note : null;
  if(note !== null){
    if(note !== f.hover){ f.hover = note; f.hoverAt = performance.now(); }
    if(performance.now() - f.hoverAt >= BEND_DWELL){
      const lock = (f.hover - f.origin) * 100;      /* exact pitch of that tine */
      if(lock !== f.lockCents){ f.lockCents = lock; f.target = lock; }
      f.locked = true;
      return;
    }
    if(f.locked) return;                            /* still passing over: hold */
  } else if(f.locked){
    return;
  }
  const half = Math.max(200, window.innerWidth / 2);
  f.target = Math.max(-bendRange, Math.min(bendRange,
      Math.round((ev.clientX - f.bx) / half * bendRange)));
}
setInterval(()=>{
  if(!bendMode) return;
  const now = performance.now();
  fingers.forEach(f=>{
    if(f.target === undefined) return;
    /* the dwell has to be judged here, not only on pointermove: a finger that
       has come to rest on a tine sends no more moves, and that is exactly the
       finger that wants to lock. */
    if(f.hover !== null && now - f.hoverAt >= BEND_DWELL){
      const lock = (f.hover - f.origin) * 100;
      if(lock !== f.lockCents){ f.lockCents = lock; f.target = lock; }
      f.locked = true;
      paintLock(f);
    }
    f.cents += (f.target - f.cents) * 0.35;         /* ~50 ms glide to a new note */
    const c = Math.round(f.cents);
    if(c === f.sent) return;
    f.sent = c;
    bendTrail(f.lx, f.ly, c, bendRange);
    f.notes.forEach(nn=>send("T " + part + " " + nn + " " + c));
  });
}, 16);


/* ring the tine the bend snapped to, and only that one */
function paintLock(f){
  const want = (f.locked && f.hover !== null) ? f.hover : null;
  if(f.lockEl && (want === null || +f.lockEl.dataset.note !== want)){
    f.lockEl.classList.remove("lockon");
    f.lockEl = null;
  }
  if(want !== null){
    const el = document.querySelector('[data-note="' + want + '"]');
    if(el){ el.classList.add("lockon"); f.lockEl = el; }
  }
}
function clearLocks(){
  document.querySelectorAll(".lockon").forEach(e=>e.classList.remove("lockon"));
  fingers.forEach(f=>{ f.lockEl = null; });
}

function syncCutoff(cut){
  const el = document.getElementById("p0");
  if(el){ el.value = cut; document.getElementById("v0").textContent = cut; }
}
function syncRes(v){
  const el = document.getElementById("p1");
  if(el){ el.value = v; document.getElementById("v1").textContent = v; }
}

/* each pad axis maps into the min/max set on that slider's row */
function rangeOf(idx){
  const loEl = document.getElementById("lo" + idx);
  const hiEl = document.getElementById("hi" + idx);
  let lo = +(loEl.value || 0);
  let hi = +(hiEl.value || 1000);
  if(isNaN(lo)) lo = 0;
  if(isNaN(hi)) hi = 1000;
  return [Math.min(lo, hi), Math.max(lo, hi)];
}
function applyRange(idx){
  const [lo, hi] = rangeOf(idx);
  const el = document.getElementById("p" + idx);
  el.min = lo;
  el.max = hi;
  let v = +el.value;
  if(v < lo) v = lo;
  if(v > hi) v = hi;
  el.value = v;
  document.getElementById("v" + idx).textContent = v;
  send("P " + idx + " " + v);
}
["0", "1"].forEach(i=>{
  document.getElementById("lo" + i).addEventListener("change", ()=>applyRange(+i));
  document.getElementById("hi" + i).addEventListener("change", ()=>applyRange(+i));
});

/* expression easing: fingers set targets, the values glide there at ~30 fps,
   so a wobbling fingertip does not step the filter in audible jumps.
   up/down = cutoff, left/right = resonance. */
const expr = {cut:null, res:null, tCut:null, tRes:null, active:false, sentC:-1, sentR:-1};

/* bend mode takes the horizontal axis for pitch, so only the vertical axis
   still drives a parameter (cutoff). Resonance stays where it was. */
function exprStartY(y){
  const [cl, ch] = rangeOf(0);
  if(expr.cut === null) expr.cut = +document.getElementById("p0").value;
  expr.tCut = cl + (1 - y) * (ch - cl);
  expr.active = true;
}
function exprStart(x, y){
  const [cl, ch] = rangeOf(0);
  const [rl, rh] = rangeOf(1);
  if(expr.cut === null) expr.cut = +document.getElementById("p0").value;
  if(expr.res === null) expr.res = +document.getElementById("p1").value;
  expr.tCut = cl + (1 - y) * (ch - cl);
  expr.tRes = rl + x * (rh - rl);
  expr.active = true;
}
function exprStop(){
  expr.active = false;
}
setInterval(()=>{
  if(!expr.active) return;
  if(expr.tCut !== null){
    expr.cut += (expr.tCut - expr.cut) * 0.35;
    const v = Math.round(expr.cut);
    if(v !== expr.sentC){ expr.sentC = v; send("P 0 " + v); syncCutoff(v); }
  }
  if(expr.tRes !== null){
    expr.res += (expr.tRes - expr.res) * 0.35;
    const v = Math.round(expr.res);
    if(v !== expr.sentR){ expr.sentR = v; send("P 1 " + v); syncRes(v); }
  }
}, 33);

function makePad(step, cls, showOct){
  const d=document.createElement("div");
  d.className=cls; d.style.touchAction="none";
  const n = padNote(step);
  d.dataset.note = n;
  d.textContent = NOTE[n%12] + (showOct ? (Math.floor(n/12)-1) : "");

  d.addEventListener("pointerdown", ev=>{
    ev.preventDefault();
    if(fingers.has(ev.pointerId)) return;
    /* where the finger lands sets the velocity: bottom of the pad is hardest */
    const y = touchY(ev, d);
    const x = touchX(ev, d);
    const vel = Math.round(36 + y * 91);
    const notes = chord ? padChord(step) : [padNote(step)];
    fingers.set(ev.pointerId, {notes:notes, el:d, y:y, x:x, t:0, bx:ev.clientX,
      cents:0, sent:0, target:0, origin:n, hover:null, hoverAt:0, locked:false, lockCents:null, chording:chord});
    notes.forEach(nn=>send("N "+nn+" "+vel+" "+part));
    paintChordLights();
    if(bendMode) exprStartY(y);      /* bend takes X for pitch, Y still sweeps cutoff */
    else exprStart(x, y);
    d.classList.add("up");
    showFingers();
    try { d.setPointerCapture(ev.pointerId); } catch(e) {}
  });

  /* sliding up and down the pad moves the filter target while it sounds */
  d.addEventListener("pointermove", ev=>{
    const f = fingers.get(ev.pointerId);
    if(!f || f.el !== d) return;
    if(bendMode){
      bendHover(ev, f);
      exprStartY(touchY(ev, d));     /* up and down still moves the parameter */
      return;
    }
    const y = touchY(ev, d);
    const x = touchX(ev, d);
    if(Math.abs(y - f.y) < 0.008 && Math.abs(x - f.x) < 0.008) return;
    f.y = y; f.x = x;
    const [cl, ch] = rangeOf(0);
    const [rl, rh] = rangeOf(1);
    expr.tCut = cl + (1 - y) * (ch - cl);
    expr.tRes = rl + x * (rh - rl);
    expr.active = true;
  });
  return d;
}

/* controllers live in #view; add a builder and a button to get another one */
function buildKalimba(){
  const v=document.getElementById("view"); v.innerHTML="";
  /* two rows of fat tines like the original, just more of them: the upper row is
     inset by half a pad so the two rows interlock the way a kalimba's do */
  const top=document.createElement("div"); top.className="row"; top.id="top";
  const bot=document.createElement("div"); bot.className="row"; bot.id="bot";
  const LO = 9, HI = 8;
  for(let i=LO;i<LO+HI;i++) top.appendChild(makePad(i,"tine"));
  for(let i=0;i<LO;i++) bot.appendChild(makePad(i,"tine"));
  top.style.paddingLeft = (100 / (LO * 2)) + "%";
  top.style.paddingRight = (100 / (LO * 2)) + "%";
  v.appendChild(top); v.appendChild(bot);
}

function buildGrid(){
  const v=document.getElementById("view"); v.innerHTML="";
  const g=document.createElement("div"); g.id="grid";
  for(let r=0;r<4;r++){
    for(let c=0;c<8;c++){
      /* 7 scale degrees per row, the 8th column is the next octave root.
         row 0 is drawn at the top, so count octaves downwards from the top */
      const step = (3-r)*7 + c;
      g.appendChild(makePad(step, c===7 ? "pad dark" : "pad", true));
    }
  }
  v.appendChild(g);
}

/* ------------------------------------------------------------- transport --- */
/* One absolute-time clock for every sequencer view. A 10 ms tick fires whatever
   falls inside a 20 ms window; the grid is computed from a monotonic clock and
   never from "previous step + interval", so a late tick cannot push the groove
   backwards. Swing offsets the odd steps on top of that grid. */
const transport = {
  on:false, timer:null, lanes:[], global:false, swing:0.18,
  start(models){
    this.lanes = models.map(m=>Object.assign({}, m, {step:0, next:performance.now() + 60}));
    this.on = true;
    clearInterval(this.timer);
    this.timer = setInterval(()=>this.tick(), 10);
    this.tick();
  },
  tick(){
    if(!this.on) return;
    const now = performance.now();
    reapOffs(now);
    for(const l of this.lanes){
      const dur = stepMs() * l.rate;
      while(l.next <= now + 20){
        const off = (l.step % 2 === 1) ? dur * l.swing * 0.5 : 0;
        l.cb(l.step, l.next + off, dur);
        l.step = (l.step + 1) % l.len;
        l.next += dur;
      }
    }
  },
  stop(){ this.on = false; this.lanes = []; clearInterval(this.timer); this.timer = null; },
  stepOf(mode){ const l = this.lanes.find(x=>x.mode === mode); return l ? l.step : 0; },
  owns(mode){ return this.on && this.lanes.some(x=>x.mode === mode); }
};

/* Every tab with a PLAY button is one lane on the same grid. Each lane keeps
   its own length and rate, so a 64-step sequence and a 3-note arp stay in sync
   without sharing a step counter. */
function engSpec(mode){
  if(mode === "seq")   return {mode, cb:seqFiring,   len:seq.len,        rate:1,        swing:seq.swing};
  if(mode === "arp"){ const n = arpNotes(); return {mode, cb:arpFiring, len:Math.max(1,n.length), rate:arp.rate, swing:0}; }
  if(mode === "eu")    return {mode, cb:euFiring,    len:16,             rate:1,        swing:0};
  if(mode === "roll")  return {mode, cb:rollFiring,  len:16,             rate:1,        swing:0.12};
  if(mode === "clips") return {mode, cb:clipsFiring, len:16,             rate:1,        swing:0.18};
  if(mode === "drone") return {mode, cb:droneFiring, len:16,             rate:1,        swing:0};
  return null;
}
const ALL_MODES = ["seq", "arp", "eu", "roll", "clips", "drone"];

function arpFiring(step, when, dur){
  const notes = arpNotes();
  fireNote(notes[step % notes.length], arp.vel, when, dur * arp.gate);
}
function euFiring(step, when, dur){
  eu.lanes.forEach(l=>{ if(!l.mute && step < l.steps && euHit(l, step))
    fireNote(padNote(l.step), l.vel, when, dur * 0.45); });
}
function rollFiring(step, when, dur){
  rollNow(step);
  for(let r = 0; r < 13; r++) if(roll.cells[r][step]) fireNote(rollNote(r), 100, when, dur * roll.gate);
}
function clipsFiring(step, when, dur){
  if(step === 0 && clips.queued >= 0){
    clips.active = clips.queued;
    clips.queued = -1;
    paintClips();
  }
  const c = clips.items[clips.active];
  for(let ti = 0; ti < 4; ti++){
    const tr = c.tracks[ti];
    if(tr.hits[step]) fireNote(padNote(tr.step), tr.vel, when, dur * 0.45);
  }
  clipPlayhead(step);
}

function startEngine(mode){
  const list = transport.global ? ALL_MODES.map(engSpec) : [engSpec(mode)];
  transport.start(list.filter(Boolean));
  paintPlay();
}
/* one place decides what every PLAY button says and does */
function tOn(mode){ return transport.global ? transport.on : transport.owns(mode); }
const PLAYBTN = {seq:"seqplay", arp:"arpplay", eu:"euplay", roll:"rollplay", clips:"clipplay", drone:"droneplay"};
/* the drone is a HOLD, not a PLAY, but it obeys the same switch */
const PLAYLABEL = {drone:["HOLD", "RELEASE"]};
function paintPlay(){
  Object.keys(PLAYBTN).forEach(m=>{
    const b = document.getElementById(PLAYBTN[m]);
    if(!b) return;
    const lab = PLAYLABEL[m] || ["PLAY", "STOP"];
    b.textContent = tOn(m) ? lab[1] : lab[0];
    b.classList.toggle("on", tOn(m));
  });
}
function paintGlobal(){
  const b = document.getElementById("gmode"), l = document.getElementById("gmodelab");
  if(b){ b.textContent = transport.global ? "ALL TABS" : "PER TAB"; b.classList.toggle("on", transport.global); }
  if(l) l.textContent = transport.global ? "all tabs" : "per tab";
  const be = document.getElementById("bpm");
  if(be && +be.value !== bpm) be.value = bpm;
  paintGplay();
}

const noteOffs = [];
function stepMs(){ return (60000 / Math.max(60, Math.min(200, bpm))) / 4; }
function fireNote(n, vel, when, gateMs, p){
  send("N " + n + " " + vel + " " + (p === undefined ? part : p));
  noteOffs.push({t: when + gateMs, n:n});
}
function reapOffs(now){
  for(let i = noteOffs.length - 1; i >= 0; i--){
    if(noteOffs[i].t <= now){
      send("O " + noteOffs[i].n);
      noteOffs.splice(i, 1);
    }
  }
}
function flushOffs(){
  noteOffs.forEach(o=>send("O " + o.n));
  noteOffs.length = 0;
}

/* ------------------------------------------------------------ step seq ---- */
const SEQ_STEPS = 64, SEQ_TRACKS = 4;
const seq = {
  swing:0.18, gate:0.45, len:16, bar:0,
  tracks: Array.from({length:SEQ_TRACKS}, (_,i)=>({
    step: i*2, vel: i===0 ? 112 : 88, mute:false, part:0,
    hits: new Array(SEQ_STEPS).fill(false)
  }))
};
const seqBanks = Array.from({length:8}, ()=>null);
let seqCells = [];
let seqNowEls = [];

function seqHighlight(step){
  seqNowEls.forEach(e=>e.classList.remove("now"));
  seqNowEls = [];
  if(Math.floor(step/16) !== seq.bar) return;
  const i = step % 16;
  seqNowEls = seqCells.map(row=>row[i]).filter(Boolean);
  seqNowEls.forEach(e=>e.classList.add("now"));
}
function seqClearHi(){ seqNowEls.forEach(e=>e.classList.remove("now")); seqNowEls = []; }

function seqFiring(step, when, dur){
  seqHighlight(step);
  for(let ti=0; ti<SEQ_TRACKS; ti++){
    const tr = seq.tracks[ti];
    if(tr.mute || !tr.hits[step]) continue;
    fireNote(padNote(tr.step), tr.vel, when, dur * seq.gate, tr.part);
  }
}
function seqStart(){
  transport.swing = seq.swing;
  startEngine("seq");
}
function seqStop(){
  stopAll();
  seqClearHi();
}

function buildSeq(){
  const v = document.getElementById("view"); v.innerHTML = "";
  const wrap = document.createElement("div"); wrap.id = "seq";

  const bar = document.createElement("div"); bar.className = "seqbar";
  const play = document.createElement("button");
  play.id = "seqplay"; play.textContent = tOn("seq") ? "STOP" : "PLAY";
  if(tOn("seq")) play.classList.add("on");
  play.onclick = ()=> tOn("seq") ? seqStop() : seqStart();
  bar.appendChild(play);

  const mkLab = (t)=>{ const s=document.createElement("span"); s.className="lab"; s.textContent=t; return s; };
  const mkRange = (min,max,val,w)=>{
    const r=document.createElement("input"); r.type="range";
    r.min=min; r.max=max; r.value=val; r.style.width=w+"px"; return r;
  };

  bar.appendChild(mkLab("BPM"));
  const bpmIn = mkRange(60,200,bpm,150);
  const bpmVal = mkLab(String(bpm)); bpmVal.id = "seqbpmv";
  bpmIn.oninput = ()=>{
    bpm = +bpmIn.value; bpmVal.textContent = bpm;
    const p12 = document.getElementById("p12");
    if(p12){ p12.value = bpm; document.getElementById("v12").textContent = bpm; }
    send("P 12 " + bpm);
  };
  bar.appendChild(bpmIn); bar.appendChild(bpmVal);

  bar.appendChild(mkLab("SWING"));
  const swIn = mkRange(0,60,Math.round(seq.swing*100),120);
  const swVal = mkLab(Math.round(seq.swing*100) + "%");
  swIn.oninput = ()=>{ seq.swing = (+swIn.value)/100; swVal.textContent = swIn.value + "%"; };
  bar.appendChild(swIn); bar.appendChild(swVal);

  const bClear = document.createElement("button"); bClear.textContent = "CLEAR";
  bClear.onclick = ()=>{
    seq.tracks.forEach(tr=>tr.hits.fill(false));
    seqCells.forEach(row=>row.forEach(c=>c.classList.remove("on")));
  };
  const bRand = document.createElement("button"); bRand.textContent = "RAND";
  bRand.onclick = ()=>{
    seq.tracks.forEach((tr,ti)=>{
      for(let s=0;s<seq.len;s++){
        const p = ti===0 ? (s%4===0 ? 0.9 : 0.15) : (s%4===2 ? 0.45 : 0.18);
        tr.hits[s] = Math.random() < p;
      }
    });
    refreshSeqCells();
  };
  const bLen = document.createElement("button"); bLen.textContent = "LEN " + seq.len;
  bLen.onclick = ()=>{
    seq.len = seq.len >= 64 ? 16 : seq.len + 16;
    if(seq.bar * 16 >= seq.len) seq.bar = 0;
    bLen.textContent = "LEN " + seq.len;
    buildSeq();
    if(transport.on) seqStart();
  };
  const bBar = document.createElement("button"); bBar.textContent = "BAR " + (seq.bar+1);
  bBar.onclick = ()=>{
    const bars = Math.max(1, seq.len/16);
    seq.bar = (seq.bar + 1) % bars;
    buildSeq();
  };
  bar.append(bClear, bRand, bLen, bBar);
  wrap.appendChild(bar);

  const bankRow = document.createElement("div"); bankRow.className = "seqbar";
  const bankMsg = document.createElement("span"); bankMsg.className = "lab"; bankMsg.textContent = "BANKS";
  bankRow.appendChild(bankMsg);
  for(let i=0;i<8;i++){
    const b = document.createElement("button");
    b.textContent = "ABCDEFGH"[i];
    if(seqBanks[i]) b.classList.add("on");
    b.onclick = ()=>{
      if(seqBanks[i]){
        seq.tracks.forEach((tr,ti)=>{ tr.hits.set(seqBanks[i].tracks[ti].hits); });
        seq.len = seqBanks[i].len;
        seq.bar = 0;
        buildSeq();
        if(transport.on) seqStart();
      }else{
        seqBanks[i] = {
          len: seq.len,
          tracks: seq.tracks.map(tr=>({hits: tr.hits.slice()}))
        };
        b.classList.add("on");
      }
    };
    bankRow.appendChild(b);
  }
  const bStore = document.createElement("button"); bStore.textContent = "STORE";
  bStore.onclick = ()=>{
    seqBanks[0] = {len: seq.len, tracks: seq.tracks.map(tr=>({hits: tr.hits.slice()}))};
    buildSeq();
  };
  bankRow.appendChild(bStore);
  wrap.appendChild(bankRow);

  seqCells = [];
  for(let ti=0; ti<SEQ_TRACKS; ti++){
    const tr = seq.tracks[ti];
    const row = document.createElement("div"); row.className = "trk";
    const meta = document.createElement("div"); meta.className = "meta";

    const nmRow = document.createElement("div"); nmRow.className = "row2";
    const dec = document.createElement("button"); dec.textContent = "\u25c0";
    const nm = document.createElement("div"); nm.className = "nm";
    nm.textContent = NOTE[padNote(tr.step)%12] + (Math.floor(padNote(tr.step)/12)-1);
    const inc = document.createElement("button"); inc.textContent = "\u25b6";
    const mute = document.createElement("button"); mute.textContent = "M";
    if(tr.mute) mute.classList.add("on");
    const pb = document.createElement("button");
    pb.textContent = tr.part ? "B" : "A";
    if(tr.part) pb.classList.add("on");
    pb.style.flex = "0 0 46px";
    pb.onclick = ()=>{ tr.part = tr.part ? 0 : 1;
      pb.textContent = tr.part ? "B" : "A";
      pb.classList.toggle("on", !!tr.part); };
    const label = ()=>{ const n=padNote(tr.step); nm.textContent = NOTE[n%12] + (Math.floor(n/12)-1); };
    dec.onclick = ()=>{ tr.step = Math.max(0, tr.step-1); label(); };
    inc.onclick = ()=>{ tr.step = Math.min(28, tr.step+1); label(); };
    mute.onclick = ()=>{ tr.mute = !tr.mute; mute.classList.toggle("on", tr.mute); };
    nmRow.append(dec, nm, inc, pb, mute);
    meta.appendChild(nmRow);

    const velRow = document.createElement("div"); velRow.className = "row2";
    const vel = mkRange(1,127,tr.vel,150);
    vel.oninput = ()=>{ tr.vel = +vel.value; };
    velRow.appendChild(vel);
    meta.appendChild(velRow);
    row.appendChild(meta);

    const grid = document.createElement("div"); grid.className = "steps";
    const cells = [];
    for(let s=0; s<16; s++){
      const g = seq.bar * 16 + s;
      const c = document.createElement("div");
      c.className = "st" + (s%4===0 ? " beat" : "");
      if(tr.hits[g]) c.classList.add("on");
      c.onpointerdown = (ev)=>{
        ev.preventDefault();
        tr.hits[g] = !tr.hits[g];
        c.classList.toggle("on", tr.hits[g]);
      };
      grid.appendChild(c); cells.push(c);
    }
    seqCells.push(cells);
    row.appendChild(grid);
    wrap.appendChild(row);
  }
  v.appendChild(wrap);
}

function refreshSeqCells(){
  seqCells.forEach((row,ti)=>row.forEach((c,s)=>{
    c.classList.toggle("on", !!seq.tracks[ti].hits[seq.bar*16 + s]);
  }));
}

function stopAll(){
  transport.stop();
  flushOffs();
  seqClearHi();
  if(typeof drone !== "undefined" && drone.on){ drone.on = false; droneHeldOff(); }
  paintPlay();
}

/* ------------------------------------------------------------ piano roll -- */
const roll = {
  gate:0.5, rec:false,
  cells: Array.from({length:13}, ()=>new Array(16).fill(false))
};
function rollNote(r){ return padNote(12 - r); }
function buildRoll(){
  const v = document.getElementById("view"); v.innerHTML = "";
  const wrap = document.createElement("div"); wrap.id = "seq";

  const bar = document.createElement("div"); bar.className = "seqbar";
  const play = document.createElement("button"); play.id = "rollplay";
  play.textContent = tOn("roll") ? "STOP" : "PLAY";
  if(tOn("roll")) play.classList.add("on");
  play.onclick = ()=>{ if(tOn("roll")) stopAll(); else startEngine("roll"); };
  bar.appendChild(play);
  const lab = (t)=>{ const s=document.createElement("span"); s.className="lab"; s.textContent=t; return s; };
  bar.appendChild(lab("scale-locked rows, drag to paint"));
  const clr = document.createElement("button"); clr.textContent = "CLEAR";
  clr.onclick = ()=>{ roll.cells.forEach(r=>r.fill(false));
    document.querySelectorAll("#roll .rc").forEach(c=>c.classList.remove("on")); };
  const rec = document.createElement("button");
  rec.textContent = "REC " + (roll.rec ? "ON" : "OFF");
  if(roll.rec) rec.classList.add("on");
  rec.onclick = ()=>{ roll.rec = !roll.rec;
    rec.textContent = "REC " + (roll.rec ? "ON" : "OFF");
    rec.classList.toggle("on", roll.rec); };
  bar.append(rec, clr);
  wrap.appendChild(bar);

  const grid = document.createElement("div"); grid.id = "roll";
  let paint = null;
  for(let r=0;r<13;r++){
    const l = document.createElement("div"); l.className = "rlab tap";
    const n = rollNote(r);
    l.textContent = NOTE[n%12] + (Math.floor(n/12)-1);
    l.onpointerdown = (ev)=>{
      ev.preventDefault();
      l.classList.add("up"); setTimeout(()=>l.classList.remove("up"), 110);
      send("N " + n + " 100 " + part);
      setTimeout(()=>send("O " + n), 180);
      if(!roll.rec) return;
      const st = transport.on ? transport.stepOf("roll") : 0;
      roll.cells[r][st] = true;
      const cell = document.querySelector('#roll .rc[data-r="'+r+'"][data-s="'+st+'"]');
      if(cell) cell.classList.add("on");
    };
    grid.appendChild(l);
    for(let s=0;s<16;s++){
      const c = document.createElement("div");
      c.className = "rc" + (roll.cells[r][s] ? " on" : "");
      c.dataset.r = r; c.dataset.s = s;
      c.onpointerdown = (ev)=>{
        ev.preventDefault();
        paint = !roll.cells[r][s];
        roll.cells[r][s] = paint;
        c.classList.toggle("on", paint);
      };
      c.onpointerenter = ()=>{
        if(paint === null) return;
        roll.cells[r][s] = paint;
        c.classList.toggle("on", paint);
      };
      grid.appendChild(c);
    }
  }
  document.addEventListener("pointerup", ()=>{ paint = null; });
  wrap.appendChild(grid);
  v.appendChild(wrap);
}
function rollNow(s){
  document.querySelectorAll("#roll .rc.now").forEach(c=>c.classList.remove("now"));
  document.querySelectorAll('#roll .rc[data-s="'+s+'"]').forEach(c=>c.classList.add("now"));
}

/* -------------------------------------------------------------------- arp -- */
const arp = {
  root:0, octaves:1, order:"up", rate:2, gate:0.5, vel:104
};
function arpNotes(){
  const offsets = CHOFF[typeSel].filter(o=>o>=0);
  let base = offsets.map(o=>padNote(arp.root + o));
  let all = [];
  for(let oc=0; oc<arp.octaves; oc++)
    all = all.concat(base.map(n=>n + oc*12));
  if(arp.order === "down") all.reverse();
  if(arp.order === "updown") all = all.concat(all.slice(1, -1).reverse());
  return all.length ? all : [padNote(arp.root)];
}
function buildArp(){
  const v = document.getElementById("view"); v.innerHTML = "";
  const wrap = document.createElement("div"); wrap.id = "seq";
  const bar = document.createElement("div"); bar.className = "seqbar";

  const play = document.createElement("button"); play.id = "arpplay";
  play.textContent = tOn("arp") ? "STOP" : "PLAY";
  if(tOn("arp")) play.classList.add("on");
  play.onclick = ()=>{ if(tOn("arp")) stopAll(); else startEngine("arp"); };
  bar.appendChild(play);

  const lab = (t)=>{ const s=document.createElement("span"); s.className="lab"; s.textContent=t; return s; };
  const btn = (t, fn, on)=>{ const b=document.createElement("button"); b.textContent=t; b.onclick=fn;
    if(on) b.classList.add("on"); return b; };

  bar.appendChild(lab("ROOT"));
  const rootB = btn(NOTE[padNote(arp.root)%12], ()=>{ arp.root=(arp.root+2)%14; rootB.textContent=NOTE[padNote(arp.root)%12]; });
  bar.appendChild(rootB);

  bar.appendChild(lab("OCT"));
  const octB = btn(String(arp.octaves), ()=>{ arp.octaves = arp.octaves>=3 ? 1 : arp.octaves+1; octB.textContent=arp.octaves; });
  bar.appendChild(octB);

  bar.appendChild(lab("ORDER"));
  const ordB = btn(arp.order.toUpperCase(), ()=>{
    arp.order = arp.order==="up" ? "down" : arp.order==="down" ? "updown" : "up";
    ordB.textContent = arp.order.toUpperCase();
  });
  bar.appendChild(ordB);

  bar.appendChild(lab("RATE"));
  const rates = [[1/1.333,"1/8T"],[2,"1/8"],[4,"1/4"],[1,"1/16"]];
  let ri = 0;
  const rateB = btn(rates[0][1], ()=>{ ri=(ri+1)%rates.length; arp.rate=rates[ri][0]; rateB.textContent=rates[ri][1]; });
  bar.appendChild(rateB);

  const clr = btn("STOP", ()=>stopAll());
  bar.appendChild(clr);

  const info = document.createElement("div"); info.className = "lab";
  info.style.marginTop = "8px"; info.style.fontSize = "15px";
  info.textContent = "arp follows KEY and TYPE: " + TYPEN[typeSel] + " on " + NOTE[padNote(arp.root)%12]
    + "  (" + arpNotes().join(", ") + ")";
  wrap.appendChild(bar); wrap.appendChild(info);

  const big = document.createElement("div");
  big.style.cssText = "flex:1;display:flex;align-items:center;justify-content:center;color:#2e7d32;font-size:64px;letter-spacing:6px";
  big.textContent = arp.order.toUpperCase();
  wrap.appendChild(big);
  v.appendChild(wrap);
}

/* ----------------------------------------------------------------- euclid -- */
const eu = {
  lanes: [
    {steps:16, pulses:4, step:0, vel:112, mute:false},
    {steps:16, pulses:3, step:2, vel:92,  mute:false},
    {steps:16, pulses:5, step:4, vel:86,  mute:false},
    {steps:16, pulses:2, step:6, vel:80,  mute:false}
  ]
};
function euHit(lane, i){ return ((i * lane.pulses) % lane.steps) < lane.pulses; }
function buildEuclid(){
  const v = document.getElementById("view"); v.innerHTML = "";
  const wrap = document.createElement("div"); wrap.id = "seq";
  const bar = document.createElement("div"); bar.className = "seqbar";

  const play = document.createElement("button"); play.id = "euplay";
  play.textContent = tOn("eu") ? "STOP" : "PLAY";
  if(tOn("eu")) play.classList.add("on");
  play.onclick = ()=>{ if(tOn("eu")) stopAll(); else startEngine("eu"); };
  bar.appendChild(play);
  const lab = (t)=>{ const s=document.createElement("span"); s.className="lab"; s.textContent=t; return s; };
  bar.appendChild(lab("euclidean lanes: pulses spread over steps, no two lanes the same"));
  wrap.appendChild(bar);

  eu.lanes.forEach((lane, li)=>{
    const row = document.createElement("div"); row.className = "trk";
    const meta = document.createElement("div"); meta.className = "meta";
    const r1 = document.createElement("div"); r1.className = "row2";
    const mk = (t, fn)=>{ const b=document.createElement("button"); b.textContent=t; b.onclick=fn; return b; };
    const nm = document.createElement("div"); nm.className = "nm";
    const upd = ()=>{ nm.textContent = NOTE[padNote(lane.step)%12]; };
    upd();
    r1.append(mk("\u25c0", ()=>{ lane.step=Math.max(0,lane.step-1); upd(); }),
              nm,
              mk("\u25b6", ()=>{ lane.step=Math.min(28,lane.step+1); upd(); }));
    meta.appendChild(r1);

    const r2 = document.createElement("div"); r2.className = "row2";
    const st = document.createElement("input"); st.type="number"; st.min=1; st.max=16; st.value=lane.steps;
    st.className="num"; st.onchange=()=>{ lane.steps = Math.max(1, Math.min(16, +st.value)); redraw(); };
    const pu = document.createElement("input"); pu.type="number"; pu.min=0; pu.max=16; pu.value=lane.pulses;
    pu.className="num"; pu.onchange=()=>{ lane.pulses = Math.max(0, Math.min(lane.steps, +pu.value)); redraw(); };
    r2.append(lab("steps"), st, lab("pulses"), pu);
    meta.appendChild(r2);
    row.appendChild(meta);

    const grid = document.createElement("div"); grid.className = "steps";
    const cells = [];
    for(let s=0;s<16;s++){
      const c = document.createElement("div"); c.className = "st"; grid.appendChild(c); cells.push(c);
    }
    const redraw = ()=>{ cells.forEach((c,s)=>{ c.classList.toggle("on", s < lane.steps && euHit(lane, s)); }); };
    redraw();
    row.appendChild(grid);
    wrap.appendChild(row);
  });
  v.appendChild(wrap);
}

/* ---------------------------------------------------------------- xy pad --- */
function buildXY(){
  const v = document.getElementById("view"); v.innerHTML = "";
  const wrap = document.createElement("div"); wrap.id = "xywrap";

  const padEl = document.createElement("div"); padEl.id = "xy";
  const dot = document.createElement("div"); dot.id = "xydot";
  padEl.appendChild(dot);

  const pos = (ev)=>{
    const r = padEl.getBoundingClientRect();
    return [Math.min(1, Math.max(0, (ev.clientX-r.left)/r.width)),
            Math.min(1, Math.max(0, (ev.clientY-r.top)/r.height))];
  };
  const move = (ev)=>{
    const [x,y] = pos(ev);
    dot.style.left = (x*100)+"%";
    dot.style.top = (y*100)+"%";
    expr.tCut = expr.tCut === null ? null : (rangeOf(0)[0] + (1-y)*(rangeOf(0)[1]-rangeOf(0)[0]));
    expr.tRes = rangeOf(1)[0] + x*(rangeOf(1)[1]-rangeOf(1)[0]);
    expr.active = true;
  };
  padEl.onpointerdown = (ev)=>{
    ev.preventDefault();
    if(expr.cut === null) expr.cut = +document.getElementById("p0").value;
    if(expr.res === null) expr.res = +document.getElementById("p1").value;
    try { padEl.setPointerCapture(ev.pointerId); } catch(e){}
    move(ev);
  };
  padEl.onpointermove = (ev)=>{ if(ev.buttons || ev.pointerType === "touch") move(ev); };
  padEl.onpointerup = ()=> exprStop();
  wrap.appendChild(padEl);

  const macros = document.createElement("div"); macros.id = "xymacros";
  [[6,"chorus mix"],[7,"delay mix"],[11,"delay feedback"],[9,"spread"]].forEach(([idx,label])=>{
    const box = document.createElement("div"); box.className = "fxrow";
    const l = document.createElement("label");
    const b = document.createElement("b"); b.id = "xyv"+idx; b.textContent = "0";
    l.textContent = label; l.appendChild(b);
    const r = document.createElement("input"); r.type="range"; r.min=0; r.max=1000; r.value=0;
    const src = document.getElementById("p"+idx);
    if(src){ r.value = src.value; b.textContent = src.value; }
    r.oninput = ()=>{ b.textContent = r.value; send("P "+idx+" "+r.value);
      if(src){ src.value = r.value; const sv=document.getElementById("v"+idx); if(sv) sv.textContent=r.value; } };
    box.appendChild(l); box.appendChild(r); macros.appendChild(box);
  });
  wrap.appendChild(macros);
  v.appendChild(wrap);
}

/* --------------------------------------------------- algorithm browser --- */
const ALG_TABLE = [
 [0xc1,0x11,0x11,0x14,0x01,0x14],[0x01,0x11,0x11,0x14,0xc1,0x14],
 [0xc1,0x11,0x14,0x01,0x11,0x14],[0xc1,0x11,0x94,0x01,0x11,0x14],
 [0xc1,0x14,0x01,0x14,0x01,0x14],[0xc1,0x94,0x01,0x14,0x01,0x14],
 [0xc1,0x11,0x05,0x14,0x01,0x14],[0x01,0x11,0xc5,0x14,0x01,0x14],
 [0x01,0x11,0x05,0x14,0xc1,0x14],[0x01,0x05,0x14,0xc1,0x11,0x14],
 [0xc1,0x05,0x14,0x01,0x11,0x14],[0x01,0x05,0x05,0x14,0xc1,0x14],
 [0xc1,0x05,0x05,0x14,0x01,0x14],[0xc1,0x05,0x11,0x14,0x01,0x14],
 [0x01,0x05,0x11,0x14,0xc1,0x14],[0xc1,0x11,0x02,0x25,0x05,0x14],
 [0x01,0x11,0x02,0x25,0xc5,0x14],[0x01,0x11,0x11,0xc5,0x05,0x14],
 [0xc1,0x14,0x14,0x01,0x11,0x14],[0x01,0x05,0x14,0xc1,0x14,0x14],
 [0x01,0x14,0x14,0xc1,0x14,0x14],[0xc1,0x14,0x14,0x14,0x01,0x14],
 [0xc1,0x14,0x14,0x01,0x14,0x04],[0xc1,0x14,0x14,0x14,0x04,0x04],
 [0xc1,0x14,0x14,0x04,0x04,0x04],[0xc1,0x05,0x14,0x01,0x14,0x04],
 [0x01,0x05,0x14,0xc1,0x14,0x04],[0x04,0xc1,0x11,0x14,0x01,0x14],
 [0xc1,0x14,0x01,0x14,0x04,0x04],[0x04,0xc1,0x11,0x14,0x04,0x04],
 [0xc1,0x14,0x04,0x04,0x04,0x04],[0xc4,0x04,0x04,0x04,0x04,0x04]
];
function algRoutes(a){
  return ALG_TABLE[a & 31].map(f=>({inb:(f>>4)&3, out:f&3, fb:(f&0xC0)===0xC0}));
}
function algCard(a){
  const r = algRoutes(a);
  const depth = [0,0,0,0,0,0];
  for(let i=0;i<6;i++){
    if(r[i].inb === 0) continue;
    for(let j=0;j<i;j++)
      if(r[j].out === r[i].inb) depth[i] = Math.max(depth[i], depth[j] + 1);
  }
  const rows = {};
  const W = 150, H = 132;
  let svg = '<svg viewBox="0 0 '+W+' '+H+'" width="100%" height="100%">';
  const pos = [];
  for(let i=0;i<6;i++){
    const d = depth[i];
    rows[d] = rows[d] || [];
    rows[d].push(i);
  }
  Object.keys(rows).forEach(d=>{
    const list = rows[d];
    list.forEach((i,k)=>{
      const x = 10 + (k + 0.5) * ((W - 20) / list.length);
      const y = H - 34 - d * 24;
      pos[i] = [x, y];
    });
  });
  for(let i=0;i<6;i++){
    for(let j=0;j<6;j++){
      if(j >= i) continue;
      if(r[j].out !== 0 && r[j].out === r[i].inb && pos[j] && pos[i]){
        const [x1,y1] = pos[j], [x2,y2] = pos[i];
        svg += '<line x1="'+x1+'" y1="'+y1+'" x2="'+x2+'" y2="'+y2+
               '" stroke="#555" stroke-width="1.2"/>';
      }
    }
  }
  for(let i=0;i<6;i++){
    const [x,y] = pos[i];
    const carrier = r[i].out === 0;
    svg += '<rect x="'+(x-13)+'" y="'+(y-8)+'" width="26" height="16" rx="4" fill="'+
           (carrier ? '#2e7d32' : '#333')+'" stroke="'+(carrier?'#4caf50':'#555')+'"/>';
    svg += '<text x="'+x+'" y="'+(y+4)+'" fill="#eee" font-size="9" text-anchor="middle">'+
           (6-i)+'</text>';
    if(r[i].fb)
      svg += '<circle cx="'+(x+18)+'" cy="'+(y-8)+'" r="2.6" fill="#ffb74d"/>';
    if(carrier)
      svg += '<line x1="'+x+'" y1="'+y+'" x2="'+x+'" y2="'+(H-14)+'" stroke="#2e7d32" stroke-width="1.2"/>';
  }
  svg += '<text x="'+(W/2)+'" y="'+(H-3)+'" fill="#666" font-size="8" text-anchor="middle">OUT</text>';
  svg += '</svg>';
  return svg;
}
function buildAlg(){
  const v = document.getElementById("view"); v.innerHTML = "";
  const wrap = document.createElement("div"); wrap.id = "algwrap";
  const head = document.createElement("div"); head.className = "seqbar";
  const lab = document.createElement("span"); lab.className = "lab";
  lab.textContent = "32 DX7 routings, green = carrier, orange dot = feedback. Tap to load.";
  head.appendChild(lab);
  wrap.appendChild(head);

  const grid = document.createElement("div"); grid.id = "alggrid";
  for(let a=0;a<32;a++){
    const c = document.createElement("div");
    c.className = "algcard" + (a === patchAlg ? " on" : "");
    c.innerHTML = algCard(a);
    const tag = document.createElement("div"); tag.className = "algtag"; tag.textContent = a+1;
    c.appendChild(tag);
    c.onclick = ()=>{
      patchAlg = a;
      send("P 3 " + a);
      document.querySelectorAll("#alggrid .algcard").forEach((el,i)=>el.classList.toggle("on", i===a));
    };
    grid.appendChild(c);
  }
  wrap.appendChild(grid);
  v.appendChild(wrap);
}

/* --------------------------------------------------------- operator edit --- */
/* name, packed byte offset in the dump, logical param for the S verb, max */
const OPS_ROWS = [["R1",0,0,99],["R2",1,1,99],["R3",2,2,99],["R4",3,3,99],
                  ["L1",4,4,99],["L2",5,5,99],["L3",6,6,99],["L4",7,7,99],
                  ["OUT",16,8,99],["COARSE",18,9,31],["FINE",19,10,99],
                  ["DETUNE",20,11,14],["KVS",15,12,7]];
const opVals = Array.from({length:6}, ()=>new Array(21).fill(0));
const txq = [];
setInterval(()=>{ if(txq.length) send(txq.shift()); }, 25);

function opsRender(){
  OPS_ROWS.forEach(([name,byte],ri)=>{
    for(let op=0;op<6;op++){
      const el = document.getElementById("op"+ri+"_"+op);
      if(!el || document.activeElement === el) continue;
      el.value = opVals[op][byte];
    }
  });
}
let opClip = null;

/* the 4-stage envelope as a shape: time from each rate, height from each level */
function opSpark(op){
  const W = 60, H = 26, pad = 2;
  const v = opVals[op];
  const L = [v[4], v[5], v[6], v[7]];
  const R = [v[0], v[1], v[2], v[3]];
  let t = 0;
  const seg = R.map(r => 1 / (r + 3));
  const total = seg.reduce((a, b) => a + b, 0) || 1;
  let pts = [[pad, H - pad - (L[0] / 99) * (H - pad * 2)]];
  let x = pad;
  for(let i = 0; i < 4; i++){
    x += (seg[i] / total) * (W - pad * 2);
    pts.push([x, H - pad - (L[i] / 99) * (H - pad * 2)]);
    t += seg[i];
  }
  return '<svg class="opmini" viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none">' +
         '<polyline points="' + pts.map(p => p[0].toFixed(1) + ',' + p[1].toFixed(1)).join(' ') +
         '" fill="none" stroke="' + (isCarrierOp(op) ? '#7fd18b' : '#e0a458') + '" stroke-width="1.6"/></svg>';
}

function isCarrierOp(op){
  const r = algRoutes(patchAlg);
  return r[op] && r[op].out === 0;
}

function opCopy(op){ opClip = opVals[op].slice(); paintOpClip(); }

function opPaste(op){
  if(!opClip) return;
  OPS_ROWS.forEach(([name, byte, param])=>{
    if(byte > 20) return;
    opVals[op][byte] = opClip[byte];
    txq.push("S " + op + " " + param + " " + opClip[byte]);
  });
  paintOpClip();
  opsRender();
}

function paintOpClip(){
  document.querySelectorAll("#opstbl .opscell").forEach((el, op)=>{
    el.classList.toggle("hasc", !!opClip);
    el.dataset.clip = opClip ? "1" : "0";
  });
}

function buildOps(){
  if(opDetail >= 0 && opDetail < 6) return buildOpsDetail(opDetail);
  const v = document.getElementById("view"); v.innerHTML = "";
  const wrap = document.createElement("div"); wrap.id = "opswrap";
  const head = document.createElement("div"); head.className = "seqbar";
  const lab = document.createElement("span"); lab.className = "lab";
  lab.textContent = "one column per operator. green = carrier (you hear it), orange = modulator (it only bends a carrier). COPY takes a whole column, PASTE drops it on another.";
  const test = document.createElement("button"); test.textContent = "HOLD A NOTE";
  holdButton(test, 60, 100);
  head.append(lab, test);
  wrap.appendChild(head);

  const peg = document.createElement("div"); peg.className = "seqbar";
  const pl = document.createElement("span"); pl.className = "lab";
  pl.textContent = "PITCH EG (50 = flat)";
  peg.appendChild(pl);
  const pegRow = (tag, base, def) => {
    const t = document.createElement("span"); t.className = "lab"; t.textContent = tag;
    peg.appendChild(t);
    for(let i = 0; i < 4; i++){
      const inp = document.createElement("input");
      inp.type = "number"; inp.min = 0; inp.max = 99; inp.className = "num";
      inp.value = def[i]; inp.id = "peg" + tag + i; inp.style.width = "64px";
      inp.onchange = ()=>{
        const val = Math.max(0, Math.min(99, +inp.value || 0));
        inp.value = val;
        send("S 0 " + (base + i) + " " + val);
      };
      peg.appendChild(inp);
    }
  };
  pegRow("L", 18, [50,50,50,50]);
  pegRow("R", 22, [99,40,40,40]);
  const flat = document.createElement("button"); flat.textContent = "FLAT";
  flat.onclick = ()=>{
    for(let i = 0; i < 4; i++){
      send("S 0 " + (18 + i) + " 50");
      const el = document.getElementById("pegL" + i); if(el) el.value = 50;
    }
  };
  peg.appendChild(flat);
  wrap.appendChild(peg);

  const tbl = document.createElement("div"); tbl.id = "opstbl";
  tbl.appendChild(document.createElement("div"));

  for(let op = 0; op < 6; op++){
    const cell = document.createElement("div"); cell.className = "opscell";
    const name = document.createElement("div");
    name.style.cssText = "font-size:15px;color:#eee";
    name.textContent = "OP" + (6 - op);
    const badge = document.createElement("div");
    badge.className = "opbadge " + (isCarrierOp(op) ? "car" : "mod");
    badge.textContent = isCarrierOp(op) ? "CARRIER" : "MOD";
    const mini = document.createElement("div");
    mini.innerHTML = opSpark(op);
    mini.dataset.op = op;
    mini.className = "opspark";
    const row2 = document.createElement("div"); row2.className = "opsrow2";
    const be = document.createElement("button"); be.textContent = "EDIT";
    be.onclick = ()=>{ opDetail = op; buildOps(); };
    const bc = document.createElement("button"); bc.textContent = "COPY";
    bc.onclick = ()=>opCopy(op);
    const bp = document.createElement("button"); bp.textContent = "PASTE";
    bp.onclick = ()=>opPaste(op);
    row2.append(be, bc, bp);
    cell.append(name, badge, mini, row2);
    tbl.appendChild(cell);
  }

  const group = (title)=>{
    const g = document.createElement("div"); g.className = "opsgrp"; g.textContent = title;
    tbl.appendChild(g);
  };
  const rowFor = ([name, byte, param, max], ri)=>{
    const lbl = document.createElement("div"); lbl.className = "opsr"; lbl.textContent = name;
    tbl.appendChild(lbl);
    for(let op = 0; op < 6; op++){
      const inp = document.createElement("input");
      inp.type = "number"; inp.min = 0; inp.max = max; inp.className = "num";
      inp.id = "op" + ri + "_" + op;
      inp.value = opVals[op][byte];
      inp.onchange = ()=>{
        const val = Math.max(0, Math.min(max, +inp.value || 0));
        inp.value = val;
        opVals[op][byte] = val;
        txq.push("S " + op + " " + param + " " + val);
        const sp = document.querySelector('.opspark[data-op="' + op + '"]');
        if(sp) sp.innerHTML = opSpark(op);
      };
      tbl.appendChild(inp);
    }
  };

  group("ENVELOPE  (rate = how fast that stage moves, level = where it lands)");
  for(let i = 0; i < 8; i++) rowFor(OPS_ROWS[i], i);
  group("OUTPUT  (what this operator gives to its carrier, or to the voice)");
  rowFor(OPS_ROWS[8], 8);
  group("PITCH AND RESPONSE  (coarse = harmonic ratio, fine = cents, KVS = velocity)");
  for(let i = 9; i < OPS_ROWS.length; i++) rowFor(OPS_ROWS[i], i);

  wrap.appendChild(tbl);
  v.appendChild(wrap);
  opsRender();
  paintOpClip();
}



/* latch, not a hold: one tap starts the note and it keeps ringing while the same
   finger goes off and turns knobs, the next tap stops it. One latch at a time. */
let heldNote = -1;
let lastVoices = 0;   /* from the Pod's @C frames */

/* a latched note on a percussive patch fades out by itself, so re-strike it when
   the pool empties. A sustaining patch never trips this and just rings. */
setInterval(()=>{
  if(heldNote >= 0 && lastVoices === 0 && typeof send === "function") send("N " + heldNote + " 100");
}, 400);

function paintHoldButtons(){
  document.querySelectorAll("[data-hold]").forEach(b=>{
    const note = +b.dataset.hold;
    const on = note === heldNote;
    b.classList.toggle("onhold", on);
    b.textContent = on ? "RELEASE NOTE" : "HOLD A NOTE";
  });
}

function holdButton(btn, note, vel){
  btn.dataset.hold = note;
  btn.textContent = "HOLD A NOTE";
  btn.addEventListener("pointerdown", e=>{
    e.preventDefault();
    e.stopPropagation();
    if(heldNote >= 0){ send("K " + heldNote); send("O " + heldNote); } /* K silences now, O covers firmware that predates it */
    if(heldNote === note){
      heldNote = -1;
    } else {
      heldNote = note;
      send("N " + note + " " + (vel || 100));
    }
    paintHoldButtons();
  });
}

function releaseHeldNote(){
  if(heldNote >= 0){ send("K " + heldNote); send("O " + heldNote); }
  heldNote = -1;
  paintHoldButtons();
}

/* ------------------------------------------------- operator detail view --- */
let opDetail = -1;

function opSet(op, byte, param, val){
  opVals[op][byte] = val;
  txq.push("S " + op + " " + param + " " + val);
  const grid = document.getElementById("op" + OPS_ROWS.findIndex(r => r[1] === byte) + "_" + op);
  if(grid && document.activeElement !== grid) grid.value = val;
}

/* one draggable synth knob for a single operator parameter */
function opKnob(op, row, label){
  const [name, byte, param, max] = row;
  const cell = document.createElement("div");
  cell.className = "opkn" + (byte <= 3 || byte >= 4 && byte <= 7 ? " grp" : "");
  const t = document.createElement("div"); t.className = "kt"; t.textContent = label || name;
  const dial = document.createElement("div"); dial.className = "kdial";
  dial.innerHTML = '<svg viewBox="0 0 64 64"><circle class="ktr" cx="32" cy="32" r="26"/>' +
                   '<circle class="karc" cx="32" cy="32" r="26"/></svg><div class="kptr"></div>';
  const val = document.createElement("b"); val.className = "kv";
  cell.append(t, dial, val);

  const arc = dial.querySelector(".karc");
  const ptr = dial.querySelector(".kptr");
  const C = 2 * Math.PI * 26;
  const paint = ()=>{
    const v = opVals[op][byte];
    const f = max ? v / max : 0;
    val.textContent = v;
    arc.style.strokeDasharray = (0.75 * C) + " " + C;
    arc.style.strokeDashoffset = (0.75 * C * (1 - f)).toFixed(2);
    arc.style.stroke = "hsl(" + (160 - 155 * f).toFixed(0) + " 70% 55%)";
    ptr.style.transform = "rotate(" + (-135 + 270 * f).toFixed(1) + "deg)";
  };

  let drag = null;
  cell.addEventListener("pointerdown", e=>{
    drag = {y: e.clientY, v: opVals[op][byte]};
    cell.setPointerCapture(e.pointerId);
    e.preventDefault();
  });
  cell.addEventListener("pointermove", e=>{
    if(!drag) return;
    const nv = Math.max(0, Math.min(max, Math.round(drag.v + (drag.y - e.clientY) / 170 * max)));
    if(nv === opVals[op][byte]) return;
    opSet(op, byte, param, nv);
    paint();
    if(byte >= 4 && byte <= 7) drawOpEnv();
  });
  const stop = ()=>{ drag = null; };
  cell.addEventListener("pointerup", stop);
  cell.addEventListener("pointercancel", stop);
  cell.addEventListener("lostpointercapture", stop);
  cell.addEventListener("click", ()=>{
    const i = window.__armLfo;
    if(i === undefined || i === null) return;
    const idx = WT_MOD_FOR.indexOf(key);
    if(idx < 0) return;
    const L = WTg().lfo[i];
    if(!L) return;
    L.target = idx; L.on = true;
    wtLfoSend("target", i); wtLfoSend("on", i);
    window.__armLfo = null;
    renderView();
  });
  cell.paint = paint;
  paint();
  return cell;
}

function opPaintKnob(i){
  const c = document.querySelectorAll("#opknobs .opkn")[i];
  if(c && c.paint) c.paint();
}

function envPoints(){
  const svg = document.getElementById("openv");
  if(!svg) return null;
  const W = svg.clientWidth || 900, H = svg.clientHeight || 240, pad = 26;
  const v = opVals[opDetail];
  const R = [v[0], v[1], v[2], v[3]], L = [v[4], v[5], v[6], v[7]];
  const seg = R.map(r => 1 / (r + 3));
  const total = seg.reduce((a, b) => a + b, 0) || 1;
  let x = pad;
  const pts = [[pad, H - pad - (L[0] / 99) * (H - pad * 2)]];
  for(let i = 0; i < 4; i++){
    x += (seg[i] / total) * (W - pad * 2);
    pts.push([x, H - pad - (L[i] / 99) * (H - pad * 2)]);
  }
  return {pts, W, H, pad};
}

function drawOpEnv(){
  const svg = document.getElementById("openv");
  if(!svg) return;
  const g = envPoints();
  if(!g) return;
  const {pts, W, H, pad} = g;
  const colour = isCarrierOp(opDetail) ? "#7fd18b" : "#e0a458";
  let out = '<svg width="100%" height="100%" viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none">';
  out += '<line x1="' + pad + '" y1="' + (H - pad) + '" x2="' + (W - pad) + '" y2="' + (H - pad) + '" stroke="#2e2e2e"/>';
  out += '<line x1="' + pad + '" y1="' + pad + '" x2="' + pad + '" y2="' + (H - pad) + '" stroke="#2e2e2e"/>';
  out += '<polyline points="' + pts.map(p => p[0].toFixed(1) + ',' + p[1].toFixed(1)).join(' ') +
         '" fill="none" stroke="' + colour + '" stroke-width="3"/>';
  pts.slice(1).forEach((p, i)=>{
    out += '<circle cx="' + p[0].toFixed(1) + '" cy="' + p[1].toFixed(1) + '" r="13" fill="#141414" stroke="' +
           colour + '" stroke-width="3" data-lv="' + i + '" style="cursor:ns-resize"/>';
    out += '<text x="' + (p[0] + 20).toFixed(1) + '" y="' + (p[1] + 6).toFixed(1) +
           '" fill="#9a9a9a" font-size="15">L' + (i + 1) + ' ' + opVals[opDetail][4 + i] + '</text>';
  });
  out += '</svg>';
  svg.innerHTML = out;
}

function buildOpsDetail(op){
  const v = document.getElementById("view"); v.innerHTML = "";
  const wrap = document.createElement("div"); wrap.id = "opsdet";

  const bar = document.createElement("div"); bar.className = "seqbar";
  const back = document.createElement("button"); back.textContent = "BACK TO GRID";
  back.onclick = ()=>{ opDetail = -1; buildOps(); };
  const title = document.createElement("span"); title.className = "lab";
  title.textContent = "OP" + (6 - op) + "  " + (isCarrierOp(op) ? "CARRIER, you hear this one" : "MODULATOR, it bends another operator");
  title.style.color = isCarrierOp(op) ? "#7fd18b" : "#e0a458";
  const test = document.createElement("button"); test.textContent = "HOLD A NOTE";
  holdButton(test, 60, 100);
  const prev = document.createElement("button"); prev.textContent = "‹";
  prev.onclick = ()=>{ opDetail = (opDetail + 5) % 6; buildOps(); };
  const next = document.createElement("button"); next.textContent = "›";
  next.onclick = ()=>{ opDetail = (opDetail + 1) % 6; buildOps(); };
  bar.append(back, prev, next, title, test);
  wrap.appendChild(bar);

  const env = document.createElement("div"); env.id = "openv";
  wrap.appendChild(env);

  const knobs = document.createElement("div"); knobs.id = "opknobs";
  OPS_ROWS.forEach(row => knobs.appendChild(opKnob(op, row, null)));
  wrap.appendChild(knobs);
  v.appendChild(wrap);

  drawOpEnv();

  /* drag a level handle straight up and down: that is the whole envelope */
  let hold = null;
  env.addEventListener("pointerdown", e=>{
    const t = e.target;
    if(!t.dataset || t.dataset.lv === undefined) return;
    const g = envPoints();
    hold = {lv: +t.dataset.lv, H: g.H, pad: g.pad, top: env.getBoundingClientRect().top};
    env.setPointerCapture(e.pointerId);
    e.preventDefault();
  });
  env.addEventListener("pointermove", e=>{
    if(!hold) return;
    const y = e.clientY - hold.top;
    const lv = Math.round(99 * (1 - (y - hold.pad) / (hold.H - hold.pad * 2)));
    const nv = Math.max(0, Math.min(99, lv));
    if(nv === opVals[opDetail][4 + hold.lv]) return;
    opSet(opDetail, 4 + hold.lv, 4 + hold.lv, nv);
    drawOpEnv();
    opPaintKnob(4 + hold.lv);
  });
  const end = ()=>{ hold = null; };
  env.addEventListener("pointerup", end);
  env.addEventListener("pointercancel", end);
  env.addEventListener("lostpointercapture", end);
  window.addEventListener("resize", ()=>setTimeout(drawOpEnv, 60));
}


/* ----------------------------------------------------------- wavetable tab --- */
/* The eight frames are the same additive sets the firmware builds, so the plot is
   the real wave and not an illustration: drag FRAME and watch it become a saw. */
const WT_HARM = [
  [1.00,0,0,0,0,0,0,0],
  [1.00,0.35,0,0,0,0,0,0],
  [1.00,0,0.33,0,0.20,0,0.14,0],
  [1.00,0.50,0.33,0.25,0.20,0.17,0.14,0.12],
  [0.30,0,0.90,0,0,0.45,0,0],
  [1.00,0,0.55,0,0,0,0.35,0],
  [1.00,0,0,0.22,0,0,0,0.12],
  [0.60,0.30,0.45,0.20,0.35,0.15,0.25,0.18]
];
const WT_PARAMS = [
  ["frame", "FRAME",   0, 1000, 400, ""],
  ["atk",   "ATTACK",  1, 4000, 20,  " ms"],
  ["dec",   "DECAY",   1, 4000, 400, " ms"],
  ["sus",   "SUSTAIN", 0, 1000, 700, ""],
  ["rel",   "RELEASE", 1, 4000, 600, " ms"],
  ["det",   "DETUNE",  0, 60,   10,  " c"],
  ["pan",   "PAN",     0, 1000, 500, ""],
  ["to",    "MORPH TO",0, 1000, 400, ""],
  ["time",  "MORPH T", 1, 8000, 1500," ms"],
  ["lvl",   "LEVEL",   0, 1000, 800, ""],
  ["envAmt","FRAME ENV",0,1000, 500, ""],
  ["envT",  "ENV TIME", 1, 4000, 200, " ms"],
  ["velF",  "VEL>FRAME",0,1000, 0,   ""],
  ["keyF",  "KEY>FRAME",0,1000, 0,   ""],
  ["rnd",   "RND PHASE",0,1000, 0,   ""]
];
const WT_PRESETS = [
  ["GLASS",   {frame:1000, atk:4,  dec:500, sus:500, rel:1200, det:8,  to:1000, time:1500, lvl:800}],
  ["BELL",    {frame:700,  atk:1,  dec:900, sus:0,   rel:1600, det:3,  to:1000, time:900,  lvl:850}],
  ["PAD",     {frame:200,  atk:1400,dec:900,sus:850, rel:2200, det:22, to:520,  time:4000, lvl:700}],
  ["SAW LEAD",{frame:560,  atk:6,  dec:300, sus:900, rel:250,  det:7,  to:560,  time:1500, lvl:750}],
  ["ORGAN",   {frame:330,  atk:15, dec:120, sus:1000,rel:180,  det:0,  to:330,  time:1500, lvl:800}],
  ["VOX",     {frame:480,  atk:300,dec:700, sus:800, rel:900,  det:14, to:620,  time:2500, lvl:780}],
  ["PLUCK",   {frame:620,  atk:1,  dec:160, sus:0,   rel:200,  det:5,  to:380,  time:250,  lvl:820}],
  ["MORPH UP",{frame:0,    atk:8,  dec:1200,sus:800, rel:1300, det:9,  to:1000, time:5000, lvl:760}]
];
const WT_DEF = { frame:400, atk:20, dec:400, sus:700, rel:600, det:10, to:400, time:1500, lvl:800, pan:500,
                 envAmt:500, envT:200, velF:0, keyF:0, rnd:0,
                 lfo:[0,1,2].map(function(){ return {on:false,shape:0,div:2,steps:0,amt:[0,0,0,0,0,0,0,0,0,0,0,0,0,0]}; }) };
const WT2 = [Object.assign({}, WT_DEF), Object.assign({}, WT_DEF)];
WT2.forEach(w=>{ if(!Array.isArray(w.lfo)) w.lfo = [0,1,2].map(function(){ return {on:false,shape:0,div:2,steps:0,amt:[0,0,0,0,0,0,0,0,0,0,0,0,0,0]}; }); });
const WTg = ()=> WT2[wtPart];
let wtUser  = [];      /* saved presets, persisted in the panel state */
let wtFile  = "";      /* the .wt set currently loaded, if any */
let wtSamples = null;  /* eight Float32Array(512) once a set is loaded */

function wtSend(k){
  const P = {frame:30, atk:31, dec:32, sus:33, rel:34, det:35, to:36, time:37, lvl:38,
             envAmt:46, envT:47, velF:48, keyF:49, rnd:50, pan:39}[k];
  if(P === undefined) return;
  send("P " + P + " " + Math.round(WTg()[k]));
}

function wtWave(x, pos){
  const f0 = Math.max(0, Math.min(6, Math.floor(pos)));
  const f1 = Math.min(7, f0 + 1);
  const m  = pos - f0;
  if(wtSamples){
    const N = 512;
    const i = Math.floor(x * N) % N;
    const j = (i + 1) % N;
    const a = wtSamples[f0][i] + (wtSamples[f0][j] - wtSamples[f0][i]) * (x * N - Math.floor(x * N));
    const b = wtSamples[f1][i] + (wtSamples[f1][j] - wtSamples[f1][i]) * (x * N - Math.floor(x * N));
    return a + (b - a) * m;
  }
  const th = 6.2831853 * x;
  let v = 0;
  for(let h = 0; h < 8; h++){
    const a = WT_HARM[f0][h] * (1 - m) + WT_HARM[f1][h] * m;
    if(a) v += a * Math.sin(th * (h + 1));
  }
  return v;
}

function wtPlot(){
  const el = document.getElementById("wtplot");
  if(!el) return;
  const W = el.clientWidth || 900, H = el.clientHeight || 190;
  const pos = WTg().frame / 1000 * 7;
  const to  = WTg().to / 1000 * 7;
  let peak = 1;
  for(let i = 0; i < 200; i++) peak = Math.max(peak, Math.abs(wtWave(i / 200, pos)));
  const pts = [], tgt = [];
  for(let i = 0; i <= 240; i++){
    const x = i / 240, px = 10 + x * (W - 20);
    const y  = H / 2 - (wtWave(x, pos) / peak) * (H * 0.40);
    const y2 = H / 2 - (wtWave(x, to)  / peak) * (H * 0.40);
    pts.push(px.toFixed(1) + "," + y.toFixed(1));
    tgt.push(px.toFixed(1) + "," + y2.toFixed(1));
  }
  el.innerHTML = '<div class="cap">FRAME ' + (Math.round(pos * 100) / 100) + '  of 7' +
    (Math.abs(to - pos) > 0.05 ? '   morphing to ' + (Math.round(to * 100) / 100) : '') + '</div>' +
    '<svg width="100%" height="100%" viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none">' +
    '<line x1="10" y1="' + (H / 2) + '" x2="' + (W - 10) + '" y2="' + (H / 2) + '" stroke="#2a2a2a"/>' +
    '<polyline points="' + tgt.join(" ") + '" fill="none" stroke="#3f5a66" stroke-width="1.5" stroke-dasharray="5 5"/>' +
    '<polyline points="' + pts.join(" ") + '" fill="none" stroke="#4dd0e1" stroke-width="2.4"/></svg>';
}

function wtKnob(def){
  const [key, label, min, max, defv, unit] = def;
  const cell = document.createElement("div");
  cell.className = "opkn";
  cell.dataset.key = key;
  cell.dataset.min = min;
  cell.dataset.max = max;
  const t = document.createElement("div"); t.className = "kt"; t.textContent = label;
  const dial = document.createElement("div"); dial.className = "kdial";
  dial.innerHTML = '<svg viewBox="0 0 64 64"><circle class="ktr" cx="32" cy="32" r="26"/>' +
                   '<circle class="kband b0" cx="32" cy="32" r="26"/>' +
                   '<circle class="kband b1" cx="32" cy="32" r="26"/>' +
                   '<circle class="kband b2" cx="32" cy="32" r="26"/>' +
                   '<circle class="karc" cx="32" cy="32" r="26"/></svg><div class="kptr"></div>' +
                   '<div class="kdot d0" style="display:none"></div>' +
                   '<div class="kdot d1" style="display:none"></div>' +
                   '<div class="kdot d2" style="display:none"></div>' +
                   '<div class="kx"></div>';
  const val = document.createElement("b"); val.className = "kv";
  cell.append(t, dial, val);
  const arc = dial.querySelector(".karc"), ptr = dial.querySelector(".kptr");
  const bands = [0,1,2].map(i=>dial.querySelector(".kband.b"+i));
  const dots  = [0,1,2].map(i=>dial.querySelector(".kdot.d"+i));
  const kx = dial.querySelector(".kx");
  kx.addEventListener("click", e=>{
    const t = e.target && e.target.dataset ? e.target.dataset.l : null;
    if(t === undefined || t === null) return;
    e.stopPropagation();
    const L = WTg().lfo[+t];
    const d = WT_DEST[key];
    if(!L || d === undefined) return;
    L.amt[d] = 0;
    send("M " + wtPart + " " + (+t) + " " + d + " 0");
    renderView();
  });
  const C = 2 * Math.PI * 26;
  const paint = ()=>{
    const v = WTg()[key], f = (v - min) / (max - min || 1);
    val.textContent = (key === "envAmt") ? (((v - 500) / 500 * 7).toFixed(1) + " fr")
                    : (key === "velF" || key === "keyF" || key === "rnd") ? Math.round(v / 10) + "%"
                    : Math.round(v) + unit;
    arc.style.strokeDasharray = (0.75 * C) + " " + C;
    arc.style.strokeDashoffset = (0.75 * C * (1 - f)).toFixed(2);
    arc.style.stroke = "hsl(" + (160 - 155 * f).toFixed(0) + " 70% 55%)";
    ptr.style.transform = "rotate(" + (-135 + 270 * f).toFixed(1) + "deg)";

    const COLS = ["#4dd0e1", "#ffb74d", "#b388ff"];
    const dest = WT_DEST[key];
    WTg().lfo.forEach((L, i)=>{
      const a = dest === undefined ? 0 : (L.amt[dest] || 0);
      if(!L.on || !a){
        bands[i].style.display = "none";
        dots[i].style.display = "none";
        return;
      }
      const dep = Math.min(1, Math.abs(a) / 1000);
      const lo = Math.max(0, f - dep / 2), hi = Math.min(1, f + dep / 2);
      bands[i].style.display = "";
      bands[i].style.stroke = COLS[i];
      bands[i].style.strokeDasharray = (0.75 * C * (hi - lo)) + " " + C;
      bands[i].style.strokeDashoffset = (-0.75 * C * lo).toFixed(2);
      bands[i].style.opacity = 0.28 + i * 0.06;
      dots[i].style.display = "";
      dots[i].style.background = COLS[i];
      dots[i].style.boxShadow = "0 0 8px " + COLS[i];
    });
    const live = WTg().lfo.map((L, i)=> (dest !== undefined && L.on && L.amt[dest]) ? i : -1).filter(i=>i >= 0);
    kx.innerHTML = live.map(i=>'<i data-l="' + i + '" style="background:' + COLS[i] + '">x</i>').join("");
  };
  let drag = null;
  cell.addEventListener("pointerdown", e=>{
    const arm = window.__armLfo;
    if(arm !== undefined && arm !== null){
      const idx = WT_DEST[key];
      const L = WTg().lfo[arm];
      if(idx !== undefined && L){
        /* tap adds a cell, tap again removes just that one */
        L.amt[idx] = L.amt[idx] ? 0 : (L.depthHint || 400);
        L.on = true;
        send("M " + wtPart + " " + arm + " " + idx + " " + Math.round(L.amt[idx]));
        wtLfoSend("on", arm);
        window.__armLfo = null;
        document.querySelectorAll(".modchip").forEach(c=>c.classList.remove("armed"));
        renderView();
      }
      e.preventDefault();
      return;
    }
    drag = {y:e.clientY, v:WTg()[key]}; cell.setPointerCapture(e.pointerId); e.preventDefault();
  });
  cell.addEventListener("pointermove", e=>{
    if(!drag) return;
    const nv = Math.max(min, Math.min(max, Math.round(drag.v + (drag.y - e.clientY) / 170 * (max - min))));
    if(nv === WTg()[key]) return;
    WTg()[key] = nv; wtSend(key); paint();
    if(key === "frame" || key === "to") wtPlot();
  });
  const stop = ()=>{ drag = null; };
  cell.addEventListener("pointerup", stop);
  cell.addEventListener("pointercancel", stop);
  cell.addEventListener("lostpointercapture", stop);
  paint();
  return cell;
}

function wtApply(p){
  Object.keys(p).forEach(k=>{
    if(k === "lfo" && p.lfo) Object.assign(WTg().lfo, p.lfo);
    else if(k in WTg()) WTg()[k] = p[k];
  });
  Object.keys(WTg()).forEach(k=>{ if(k !== "lfo") wtSend(k); });
  wtLfoSend("all");
  renderView();
}

async function wtLoadFile(name){
  wtFile = name;
  const buf = await (await fetch("/wtfile?f=" + encodeURIComponent(name))).arrayBuffer();
  if(buf.byteLength < 8192) return "short file";
  const dv = new DataView(buf);
  wtSamples = [];
  for(let f = 0; f < 8; f++){
    const a = new Float32Array(512);
    for(let i = 0; i < 512; i++) a[i] = dv.getInt16((f * 512 + i) * 2, true) / 32768;
    wtSamples[f] = a;
  }
  const msg = await (await fetch("/wt/load?f=" + encodeURIComponent(name), {method:"POST"})).text();
  return msg;
}

function wtSavePreset(){
  const name = "USER " + (wtUser.length + 1);
  wtUser.push({ n: name, part: wtPart, p: JSON.parse(JSON.stringify(WTg())) });
  renderView();
}

function wtPresetRow(){
  const row = document.createElement("div");
  row.id = "wtpresets";
  WT_PRESETS.forEach(([name, p])=>{
    const b = document.createElement("button"); b.className = "pbtn small"; b.textContent = name;
    b.onclick = ()=>wtApply(p);
    row.appendChild(b);
  });
  wtUser.forEach((u, i)=>{
    const b = document.createElement("button"); b.className = "pbtn small"; b.textContent = u.n;
    b.style.borderColor = "#3f6b74";
    let timer = null;
    const arm = ()=>{ timer = setTimeout(()=>{ wtUser.splice(i, 1); renderView(); }, 650); };
    const disarm = ()=>{ if(timer){ clearTimeout(timer); timer = null; } };
    b.addEventListener("pointerdown", arm);
    b.addEventListener("pointerup", ()=>{ disarm(); if(typeof u.part === "number" && u.part !== wtPart){ wtPart = u.part; send("P 17 " + wtPart); } wtApply(u.p); });
    b.addEventListener("pointercancel", disarm);
    b.addEventListener("pointerleave", disarm);
    row.appendChild(b);
  });
  return row;
}

const WT_LFO_TARGETS = ["FRAME", "MORPH", "DETUNE"];
const WT_LFO_SHAPES  = ["SIN", "TRI", "SAW", "SQR", "S&H"];

const WT_MOD_FOR = ["frame", "to", "det", "lvl"];
const WT_DEST = {frame:0, to:1, det:2, lvl:3, atk:4, dec:5, sus:6, rel:7,
                 envAmt:8, envT:9, velF:10, keyF:11, rnd:12, morphT:13, pan:14};
const WT_ZERO14 = ()=>[0,0,0,0,0,0,0,0,0,0,0,0,0,0];
function wtLfoSend(which, li){
  const list = WTg().lfo;
  const put = (idx, v)=>send("P " + idx + " " + Math.round(v));
  list.forEach((p, i)=>{
    if(li !== undefined && li !== i) return;
    if(which === "all" || which === "matrix")
      p.amt.forEach((a, d)=>{ if(a) send("M " + wtPart + " " + i + " " + d + " " + Math.round(a)); });
    if(which === "all" || which === "rate")   put(60 + i*5 + 1, p.div);
    if(which === "all" || which === "shape")  put(60 + i*5 + 3, p.shape);
    if(which === "all" || which === "on")     put(60 + i*5 + 4, p.on ? 1 : 0);
  });
}

/* a compact knob bound to any getter/setter, used by the LFO row */
function wtMini(label, min, max, get, set, unit, fmt){
  const cell = document.createElement("div");
  cell.className = "opkn"; cell.style.width = "96px";
  const t = document.createElement("div"); t.className = "kt"; t.textContent = label;
  const dial = document.createElement("div"); dial.className = "kdial";
  dial.style.width = "76px"; dial.style.height = "76px";
  dial.innerHTML = '<svg viewBox="0 0 64 64" style="width:76px;height:76px">' +
                   '<circle class="ktr" cx="32" cy="32" r="26"/>' +
                   '<circle class="karc" cx="32" cy="32" r="26"/></svg><div class="kptr"></div>';
  const val = document.createElement("b"); val.className = "kv";
  cell.append(t, dial, val);
  const arc = dial.querySelector(".karc"), ptr = dial.querySelector(".kptr");
  const C = 2 * Math.PI * 26;
  const paint = ()=>{
    const v = get(), f = (v - min) / (max - min || 1);
    val.textContent = fmt ? fmt(v) : ((max <= 200 ? v : Math.round(v)) + (unit || ""));
    arc.style.strokeDasharray = (0.75 * C) + " " + C;
    arc.style.strokeDashoffset = (0.75 * C * (1 - f)).toFixed(2);
    arc.style.stroke = "hsl(" + (160 - 155 * f).toFixed(0) + " 70% 55%)";
    ptr.style.transform = "rotate(" + (-135 + 270 * f).toFixed(1) + "deg)";
  };
  let drag = null;
  cell.addEventListener("pointerdown", e=>{ drag = {y:e.clientY, v:get()}; cell.setPointerCapture(e.pointerId); e.preventDefault(); });
  cell.addEventListener("pointermove", e=>{
    if(!drag) return;
    const nv = Math.max(min, Math.min(max, Math.round(drag.v + (drag.y - e.clientY) / 170 * (max - min))));
    if(nv === get()) return;
    set(nv); paint();
  });
  const stop = ()=>{ drag = null; };
  cell.addEventListener("pointerup", stop);
  cell.addEventListener("pointercancel", stop);
  cell.addEventListener("lostpointercapture", stop);
  cell.paint = paint;
  paint();
  return cell;
}

function wtLfoRow(){
  const wrap = document.createElement("div");
  wrap.style.cssText = "display:flex; gap:8px; flex-wrap:nowrap; align-items:flex-start; width:100%;";
  const COLS = ["#4dd0e1", "#ffb74d", "#b388ff"];
  const DIVS = ["1/16", "1/8", "1/4", "1/2", "1 bar", "2 bar"];
  /* tiny waveform glyphs, drawn rather than spelled */
  const GLYPH = [
    "0,7 2,2 4,7 6,12 8,7 10,2 12,7 14,12 16,7 18,2 20,7 22,12",
    "0,12 6,2 16,12 22,2",
    "0,12 22,2 22,12",
    "0,12 0,2 11,2 11,12 22,12 22,2",
    "0,8 4,3 4,12 9,6 9,2 14,11 14,5 18,9 18,3 22,10"
  ];
  const glyph = (i)=>{
    const pts = GLYPH[i].split(" ").map(p=>p.split(","));
    let d = "M" + pts[0][0] + " " + pts[0][1];
    for(let k = 1; k < pts.length; k++) d += " L" + pts[k][0] + " " + pts[k][1];
    return '<svg width="30" height="16" viewBox="0 0 22 14" style="display:block">' +
           '<path d="' + d + '" fill="none" stroke="currentColor" stroke-width="2" ' +
           'stroke-linecap="round" stroke-linejoin="round"/>' +
           '</svg>';
  };

  WTg().lfo.forEach((L, i)=>{
    const box = document.createElement("div");
    box.style.cssText = "border:1px solid " + (L.on ? COLS[i] : "#2a2a2a") +
      "; border-radius:12px; padding:5px 7px; display:flex; gap:5px; align-items:center; flex:1 1 0; min-width:0;";

    const chip = document.createElement("div");
    chip.className = "modchip";
    chip.textContent = "LFO " + "ABC"[i];
    chip.style.color = COLS[i];
    chip.style.borderColor = COLS[i];
    chip.style.opacity = L.on ? 1 : 0.55;
    chip.dataset.lfo = i;
    chip.addEventListener("pointerdown", e=>{
      window.__armLfo = i;
      document.querySelectorAll(".modchip").forEach(c=>c.classList.remove("armed"));
      chip.classList.add("armed");
      chip.setPointerCapture(e.pointerId);
      chip.classList.add("drag");
      e.preventDefault();
    });
    chip.addEventListener("pointerup", e=>{
      chip.classList.remove("drag");
      const el = document.elementFromPoint(e.clientX, e.clientY);
      const knob = el && el.closest ? el.closest(".opkn") : null;
      const idx = knob ? WT_MOD_FOR.indexOf(knob.dataset.key) : -1;
      if(idx !== undefined){
        L.amt[idx] = L.amt[idx] ? 0 : 400;
        L.on = true;
        if(L.amt[idx]) send("M " + wtPart + " " + i + " " + idx + " 400");
        else send("M " + wtPart + " " + i + " " + idx + " 0");
        wtLfoSend("on", i);
        renderView();
      }
    });
    chip.addEventListener("pointercancel", ()=>{ chip.classList.remove("drag"); chip.classList.remove("armed"); });

    const cv = document.createElement("canvas");
    cv.width = 104; cv.height = 28;
    cv.dataset.lfo = i;
    cv.style.cssText = "flex:0 0 auto; width:104px; height:28px; background:#070d10; border-radius:6px; border:1px solid #1b2a30;";
    box.appendChild(cv);

    const on = document.createElement("button");
    on.className = "pbtn small";
    on.textContent = L.on ? "ON" : "OFF";
    on.classList.toggle("on", L.on);
    on.onclick = ()=>{ L.on = !L.on; wtLfoSend("on", i); renderView(); };

    const shapes = document.createElement("div");
    shapes.style.cssText = "display:grid; grid-template-columns:repeat(3, auto); gap:2px; flex:0 0 auto;";
    GLYPH.forEach((_, si)=>{
      const b = document.createElement("button");
      b.className = "pbtn small";
      b.innerHTML = glyph(si);
      b.style.padding = "2px 3px";
      b.style.lineHeight = "0";
      if(L.shape === si) b.classList.add("on");
      b.onclick = ()=>{ L.shape = si; wtLfoSend("shape", i); renderView(); };
      shapes.appendChild(b);
    });

    const clr = document.createElement("button");
    clr.className = "pbtn small";
    clr.textContent = "x";
    clr.title = "remove this LFO from its knob";
    clr.style.padding = "2px 7px";
    clr.onclick = ()=>{
      L.amt = WT_ZERO14(); L.on = false;
      for(let d = 0; d < 14; d++) send("M " + wtPart + " " + i + " " + d + " 0");
      wtLfoSend("on", i); renderView();
    };
    box.append(chip, on, clr, shapes);
    box.appendChild(wtMini("DIV", 0, 5, ()=>L.div, v=>{ L.div = v; wtLfoSend("rate", i); }, "", v=>DIVS[v]));
    box.appendChild(wtMini("AMT", 0, 1000,
      ()=>L.depthHint || 400,
      v=>{
        L.depthHint = v;
        L.amt = L.amt.map(a=>{ return a ? v : 0; });
        wtLfoSend("matrix", i);
      }, "", v=>Math.round(v/10)+"%"));
    wrap.appendChild(box);
  });

  return wrap;
}

function wtShape(ph, shape){
  switch(shape){
    case 1: return ph < 0.5 ? (ph * 4 - 1) : (3 - ph * 4);
    case 2: return ph * 2 - 1;
    case 3: return ph < 0.5 ? 1 : -1;
    case 4: return Math.sin(ph * 6.283) > 0 ? 0.6 : -0.6;
    default: return Math.sin(ph * 6.2831853);
  }
}
const WT_LFO_COLS = ["#4dd0e1", "#ffb74d", "#b388ff"];
function wtDrawTraces(){
  document.querySelectorAll("#wtwrap canvas").forEach(cv=>{
    const i = +cv.dataset.lfo;
    const L = WTg().lfo[i];
    if(!L) return;
    const ctx = cv.getContext("2d"), w = cv.width, h = cv.height;
    ctx.clearRect(0, 0, w, h);
    ctx.strokeStyle = "rgba(255,255,255,0.10)";
    ctx.beginPath(); ctx.moveTo(0, h/2); ctx.lineTo(w, h/2); ctx.stroke();
    ctx.strokeStyle = WT_LFO_COLS[i];
    ctx.globalAlpha = L.on ? 0.95 : 0.35;
    ctx.lineWidth = 1.6;
    ctx.beginPath();
    for(let x = 0; x < w; x++){
      const v = wtShape(x / (w - 1), L.shape);
      const y = h/2 - v * (h/2 - 4);
      x ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    }
    ctx.stroke();
    const beats = [0.25,0.5,1,2,4,8][L.div] || 1;
    const hz = ((bpm || 120) / 60) / beats;
    const ph = (performance.now() / 1000 * hz) % 1;
    const y = h/2 - wtShape(ph, L.shape) * (h/2 - 4);
    ctx.globalAlpha = 1;
    ctx.fillStyle = "#fff";
    ctx.beginPath(); ctx.arc(ph * (w - 1), y, 2.6, 0, 6.283); ctx.fill();
    ctx.globalAlpha = 1;
  });
}

setInterval(()=>{
  if(view !== "wt") return;
  const list = WTg().lfo;
  if(!list || !list.length) return;
  wtDrawTraces();
  document.querySelectorAll("#wtknobs .opkn").forEach(c=>{
    const key = c.dataset.key, dial = c.querySelector(".kdial");
    if(!dial) return;
    const W = dial.clientWidth || 76, R = W/2 - 11, cx = W/2, cy = W/2;
    list.forEach((L, i)=>{
      const dot = c.querySelector(".kdot.d" + i);
      if(!dot) return;
      const dIdx = WT_DEST[key];
      const amt = dIdx === undefined ? 0 : (L.amt[dIdx] || 0);
      if(!L.on || !amt){ dot.style.display = "none"; return; }
      const beats = [0.25,0.5,1,2,4,8][L.div] || 1;
      const hz = ((bpm || 120)/60)/beats;
      const ph = (performance.now()/1000*hz) % 1;
      let w = Math.sin(ph*6.2831853);
      if(L.shape === 2) w = ph*2 - 1;
      else if(L.shape === 3) w = ph < 0.5 ? 1 : -1;
      else if(L.shape === 1) w = ph < 0.5 ? (ph*4 - 1) : (3 - ph*4);
      else if(L.shape === 4) w = Math.sin(ph*6.2831853) > 0 ? 0.6 : -0.6;
      /* the dot rides the band: value plus or minus half the amount, never the
         full knob sweep, so what you see matches what the LFO will do */
      const lo = +(c.dataset.min || 0), hi = +(c.dataset.max || 1000);
      const fv = (WTg()[key] - lo) / ((hi - lo) || 1);
      const dep = Math.min(1, Math.abs(amt) / 1000);
      const u = Math.max(0, Math.min(1, fv + w * dep / 2));
      const a = (-135 + 270*u) * Math.PI/180;
      dot.style.left = (cx + Math.sin(a)*R - 5).toFixed(1)+"px";
      dot.style.top  = (cy - Math.cos(a)*R - 5).toFixed(1)+"px";
    });
  });
}, 60);

function buildWt(){
  const v = document.getElementById("view"); v.innerHTML = "";
  const wrap = document.createElement("div"); wrap.id = "wtwrap";

  const bar = document.createElement("div"); bar.className = "seqbar";
  const lab = document.createElement("span"); lab.className = "lab";
  lab.textContent = engWT ? "wavetable engine active" : "FM engine is running: press WT in the header to hear this";
  const pa = document.createElement("button"); pa.textContent = "PART A";
  const pb = document.createElement("button"); pb.textContent = "PART B";
  const paintParts = ()=>{
    pa.classList.toggle("on", wtPart === 0);
    pb.classList.toggle("on", wtPart === 1);
  };
  pa.onclick = ()=>{ wtPart = 0; send("P 17 0"); paintParts(); renderView(); };
  pb.onclick = ()=>{ wtPart = 1; send("P 17 1"); paintParts(); renderView(); };
  const hold = document.createElement("button"); hold.textContent = "HOLD A NOTE";
  holdButton(hold, 60, 100);
  const test = document.createElement("button"); test.textContent = "TAP NOTE";
  test.onclick = ()=>{ send("N 60 100"); setTimeout(()=>send("O 60"), 260); };
  bar.append(lab, pa, pb, hold, test);
  paintParts();
  wrap.appendChild(bar);

  const presets = wtPresetRow();
  const save = document.createElement("button");
  save.className = "pbtn small"; save.textContent = "SAVE PRESET";
  save.style.borderColor = "#3f6b74";
  save.onclick = wtSavePreset;
  presets.appendChild(save);
  wrap.appendChild(presets);

  const files = document.createElement("div");
  files.className = "seqbar";
  const fsel = document.createElement("select"); fsel.id = "wtsel"; fsel.className = "pbtn";
  fsel.style.minWidth = "220px";
  const fload = document.createElement("button"); fload.className = "pbtn small"; fload.textContent = "⤓"; fload.title = "load wave set";
  const fmsg = document.createElement("span"); fmsg.className = "lab"; fmsg.id = "wtmsg";
  fmsg.textContent = wtFile ? wtFile : "built-in additive frames";
  fload.onclick = async ()=>{
    if(!fsel.value) return;
    fmsg.textContent = "loading...";
    try{
      const m = await wtLoadFile(fsel.value);
      fmsg.textContent = fsel.value + " : " + m;
      slotName[wtPart] = fsel.value.replace(/\.wt$/i, "");
      paintSlots();
      if(!engWT) document.getElementById("engsel").click();
      renderView();
    }catch(e){ fmsg.textContent = "failed"; }
  };
  const voices = document.createElement("span"); voices.className = "lab";
  voices.textContent = "12 voices";
  files.append(fsel, fload, fmsg, voices);
  wrap.appendChild(files);
  fetch("/wt").then(r=>r.json()).then(names=>{
    wtFiles = names.slice();
    if(engWT) renderWtPatchList();
    fsel.innerHTML = "";
    if(!names.length){
      const o = document.createElement("option"); o.textContent = "(no .wt files)"; fsel.appendChild(o);
      return;
    }
    names.forEach(n=>{ const o = document.createElement("option"); o.value = n; o.textContent = n; fsel.appendChild(o); });
    if(wtFile) fsel.value = wtFile;
  }).catch(()=>{});


  const plot = document.createElement("div"); plot.id = "wtplot";
  wrap.appendChild(plot);

  const knobs = document.createElement("div"); knobs.id = "wtknobs";
  WT_PARAMS.forEach(def => knobs.appendChild(wtKnob(def)));
  wrap.appendChild(knobs);
  wrap.appendChild(wtLfoRow());
  v.appendChild(wrap);

  requestAnimationFrame(wtPlot);
  window.addEventListener("resize", ()=>setTimeout(wtPlot, 60));
}

/* --------------------------------------------------------------- random --- */
function randPatch(){
  send("P 3 " + Math.floor(Math.random()*32));
  for(let op=0;op<6;op++){
    const r1 = 55 + Math.floor(Math.random()*45);
    const r2 = 40 + Math.floor(Math.random()*50);
    const r3 = 25 + Math.floor(Math.random()*45);
    const r4 = 15 + Math.floor(Math.random()*40);
    const l1 = 80 + Math.floor(Math.random()*20);
    const l2 = 40 + Math.floor(Math.random()*50);
    const l3 = 10 + Math.floor(Math.random()*60);
    txq.push("S "+op+" 0 "+r1, "S "+op+" 1 "+r2, "S "+op+" 2 "+r3, "S "+op+" 3 "+r4,
             "S "+op+" 4 "+l1, "S "+op+" 5 "+l2, "S "+op+" 6 "+l3, "S "+op+" 7 0",
             "S "+op+" 8 "+(op===0 ? 99 : Math.floor(Math.random()*90)),
             "S "+op+" 9 "+(1+Math.floor(Math.random()*8)),
             "S "+op+" 10 "+Math.floor(Math.random()*30),
             "S "+op+" 11 "+Math.floor(Math.random()*15));
  }
  setTimeout(()=>{ send("N 60 100"); setTimeout(()=>send("O 60"), 500); }, 2500);
}
function buildRand(){
  const v = document.getElementById("view"); v.innerHTML = "";
  const wrap = document.createElement("div"); wrap.id = "seq";
  const bar = document.createElement("div"); bar.className = "seqbar";
  const mk = (t, fn)=>{ const b=document.createElement("button"); b.textContent=t; b.onclick=fn; return b; };
  bar.append(
    mk("RANDOMISE PATCH", randPatch),
    mk("NUDGE", ()=>{
      for(let op=0;op<6;op++)
        for(const b of [0,1,2,3]){
          const cur = opVals[op][b];
          const nv = Math.max(0, Math.min(99, cur + Math.round((Math.random()-0.5)*30)));
          txq.push("S "+op+" "+b+" "+nv);
        }
    }),
    mk("TEST", ()=>{ send("N 60 100"); setTimeout(()=>send("O 60"), 500); })
  );
  const lab = document.createElement("span"); lab.className = "lab";
  lab.textContent = "writes straight into the patch: randomise, listen, nudge, repeat";
  wrap.appendChild(bar); wrap.appendChild(lab);
  const big = document.createElement("div");
  big.style.cssText = "flex:1;display:flex;align-items:center;justify-content:center;color:#5a5a5a;font-size:22px;text-align:center;line-height:1.6";
  big.innerHTML = "RANDOMISE sets a new algorithm and a full set of envelopes,<br>then plays a test note so you hear it immediately.<br>NUDGE keeps what you have and walks it.";
  wrap.appendChild(big);
  v.appendChild(wrap);
}

/* ----------------------------------------------------------------- clips --- */
const clips = {
  active:0, queued:-1,
  items: Array.from({length:16}, ()=>({
    len:16,
    tracks: Array.from({length:4}, (_,i)=>({
      step: i*2, vel: i===0 ? 112 : 88,
      hits: new Array(16).fill(false)
    }))
  }))
};
let clipEls = [];

function buildClips(){
  const v = document.getElementById("view"); v.innerHTML = "";
  const wrap = document.createElement("div"); wrap.id = "seq";
  const bar = document.createElement("div"); bar.className = "seqbar";

  const play = document.createElement("button"); play.id = "clipplay";
  play.textContent = tOn("clips") ? "STOP" : "PLAY";
  if(tOn("clips")) play.classList.add("on");
  play.onclick = ()=>{ if(tOn("clips")) stopAll(); else startEngine("clips"); };
  bar.appendChild(play);
  const lab = document.createElement("span"); lab.className = "lab";
  lab.textContent = "tap a clip to queue it at the next bar. tap RAND to fill one.";
  const rnd = document.createElement("button"); rnd.textContent = "RAND CLIP";
  rnd.onclick = ()=>{
    const i = clips.queued >= 0 ? clips.queued : clips.active;
    clips.items[i].tracks.forEach((tr,ti)=>{
      for(let s=0;s<16;s++){
        const p = ti===0 ? (s%4===0 ? 0.9 : 0.12) : (s%4===2 ? 0.4 : 0.15);
        tr.hits[s] = Math.random() < p;
      }
    });
    paintClips();
  };
  bar.append(lab, rnd);
  wrap.appendChild(bar);

  const grid = document.createElement("div"); grid.id = "clipgrid";
  clipEls = [];
  for(let i=0;i<16;i++){
    const c = document.createElement("div"); c.className = "clip";
    const num = document.createElement("div"); num.className = "clipnum"; num.textContent = (i+1);
    const dots = document.createElement("div"); dots.className = "clipdots";
    c.append(num, dots);
    c.onclick = ()=>{
      if(i === clips.active){ clips.queued = -1; }
      else { clips.queued = i; }
      paintClips();
    };
    grid.appendChild(c); clipEls.push(c);
  }
  wrap.appendChild(grid);
  v.appendChild(wrap);
  paintClips();
}
function clipDots(i){
  const c = clips.items[i];
  let n = 0;
  c.tracks.forEach(tr=>tr.hits.forEach(h=>{ if(h) n++; }));
  return n;
}
function paintClips(){
  clipEls.forEach((el,i)=>{
    el.classList.toggle("on", i === clips.active);
    el.classList.toggle("queued", i === clips.queued);
    const dots = el.querySelector(".clipdots");
    const n = clipDots(i);
    dots.textContent = n ? "\u25cf".repeat(Math.min(8, Math.ceil(n/4))) : "";
  });
}
function clipPlayhead(s){
  clipEls.forEach(el=>el.classList.remove("now"));
  const el = clipEls[clips.active];
  if(el) el.classList.add("now");
}

/* ---------------------------------------------------------------- chroma -- */
/* A chromatic player: twelve columns, four octaves, no scale locking. The pads
   are coloured by the chord you have chosen, so the root, third, fifth and
   seventh read at a glance and the notes outside the key fade into the floor. */
const CHORD_Q = [[0,4,7], [0,3,7], [0,4,7,10], [0,4,7,11], [0,3,7,10], [0,5,7], [0,3,6]];

function chromaChord(n){
  /* CHROMA is chromatic, so the gaps here are semitones: spacing multiplies them
     and extra notes past the type wrap an octave up. Same CHORD and SPACE knobs as
     the scale pads, so both players agree. */
  const q = CHORD_Q[typeSel];
  const out = [];
  let last = -1000;
  for(let i = 0; i < chordN; i++){
    let iv = q[i % q.length] * chordSpace + 12 * Math.floor(i / q.length);
    while(iv <= last) iv += 12;
    out.push(Math.min(127, n + iv));
    last = iv;
  }
  return out;
}

function makePadN(n, cls, showOct){
  const d = document.createElement("div");
  d.className = cls; d.style.touchAction = "none";
  d.dataset.note = n;
  d.textContent = NOTE[n % 12] + (showOct ? (Math.floor(n / 12) - 1) : "");

  d.addEventListener("pointerdown", ev=>{
    ev.preventDefault();
    if(fingers.has(ev.pointerId)) return;
    const y = touchY(ev, d), x = touchX(ev, d);
    const vel = Math.round(36 + y * 91);
    const notes = chord ? chromaChord(n) : [n];
    fingers.set(ev.pointerId, {notes:notes, el:d, y:y, x:x, t:0, bx:ev.clientX,
      cents:0, sent:0, target:0, origin:n, hover:null, hoverAt:0, locked:false, lockCents:null, chording:chord});
    notes.forEach(nn=>send("N " + nn + " " + vel + " " + part));
    paintChordLights();
    if(bendMode) exprStartY(y);
    else exprStart(x, y);
    d.classList.add("up");
    showFingers();
    try { d.setPointerCapture(ev.pointerId); } catch(e) {}
  });

  d.addEventListener("pointermove", ev=>{
    const f = fingers.get(ev.pointerId);
    if(!f || f.el !== d) return;
    if(bendMode){
      bendHover(ev, f);
      exprStartY(touchY(ev, d));     /* up and down still moves the parameter */
      return;
    }
    const y = touchY(ev, d), x = touchX(ev, d);
    if(Math.abs(y - f.y) < 0.008 && Math.abs(x - f.x) < 0.008) return;
    f.y = y; f.x = x;
    const [cl, ch] = rangeOf(0);
    const [rl, rh] = rangeOf(1);
    expr.tCut = cl + (1 - y) * (ch - cl);
    expr.tRes = rl + x * (rh - rl);
    expr.active = true;
  });
  return d;
}

function buildChroma(){
  const v = document.getElementById("view"); v.innerHTML = "";
  const g = document.createElement("div"); g.id = "chroma";
  const q = CHORD_Q[typeSel];
  const rootPc = keySel;
  const ivOf = (pc)=> ((pc - rootPc) % 12 + 12) % 12;
  const inKey = (pc)=> SCALE[typeSel].indexOf(ivOf(pc)) >= 0;
  const base = 48 + oct * 12;               /* C3 at octave 0, four octaves tall */

  for(let r = 0; r < 4; r++){
    for(let c = 0; c < 12; c++){
      const n  = base + (3 - r) * 12 + c;   /* row 0 on top is the highest */
      const pc = ((n % 12) + 12) % 12;
      const k  = q.indexOf(ivOf(pc));
      let cls = "pad";
      if(k === 0)      cls += " croot";
      else if(k === 1) cls += " c3rd";
      else if(k === 2) cls += " c5th";
      else if(k === 3) cls += " c7th";
      else if(inKey(pc)) cls += " ckey";
      else             cls += " coff";
      g.appendChild(makePadN(n, cls, c === 0));
    }
  }
  v.appendChild(g);
}

/* ----------------------------------------------------------------- drone --
   A tanpura, not a chord: the stones hold the key, one stone is re-plucked on
   the pulse, and the rack below moves every parameter the Pod exposes. Three
   LFOs sit to the right of the field: pick a shape, set rate and depth, then
   point it at any fader. LFO RUN starts the movement, HOLD starts the sound. */
const drone = {
  on:false, div:4, alg:-1, wave:-1, lfoRun:true,
  core:{x:0.5, y:0.35},
  fb:0.35,
  stones:[
    {deg:0,  lvl:0.90},
    {deg:4,  lvl:0.55},
    {deg:7,  lvl:0.45},
    {deg:11, lvl:0.34},
    {deg:14, lvl:0.24}
  ],
  held:[]
};

let dronePaint = null;   /* buildDrone hands the field's paint() up here */

/* operator wide sends: the S verb takes (operator, logical param, value) */
function sFan(param, amt, centre, lo, hi){
  for(let o = 0; o < 6; o++){
    const f = (o - 2.5) / 2.5;
    const v = Math.round(Math.max(lo, Math.min(hi, centre + amt * f)));
    send("S " + o + " " + param + " " + v);
  }
}
function sAll(param, val){
  const v = Math.round(val);
  for(let o = 0; o < 6; o++) send("S " + o + " " + param + " " + v);
}

/* every knob the Pod answers to, in the order they sit on the rack */
const DR = [
  {k:"cut",  lab:"CUTOFF", lo:0,   hi:1000, def:500, send:v=>send("P 0 " + v)},
  {k:"res",  lab:"RES",    lo:0,   hi:1000, def:650, send:v=>send("P 1 " + v)},
  {k:"fb",   lab:"F.BACK", lo:0,   hi:1000, def:350, send:v=>send("P 2 " + v)},
  {k:"fan",  lab:"FAN",    lo:0,   hi:7,    def:0,   send:v=>sFan(11, v, 7, 0, 14)},
  {k:"fine", lab:"FINE",   lo:0,   hi:99,   def:0,   send:v=>sFan(10, v, 0, 0, 99)},
  {k:"olvl", lab:"OP LVL", lo:0,   hi:99,   def:99,  send:v=>sAll(8, v)},
  {k:"lrt",  lab:"LFO RT", lo:0,   hi:99,   def:24,  send:v=>send("S 0 13 " + v)},
  {k:"pmd",  lab:"PMD",    lo:0,   hi:99,   def:0,   send:v=>send("S 0 14 " + v)},
  {k:"amd",  lab:"AMD",    lo:0,   hi:99,   def:0,   send:v=>send("S 0 15 " + v)},
  {k:"ch",   lab:"CHORUS", lo:0,   hi:1000, def:300, send:v=>send("P 6 " + v)},
  {k:"dmix", lab:"D.MIX",  lo:0,   hi:1000, def:250, send:v=>send("P 7 " + v)},
  {k:"dms",  lab:"D.TIME", lo:50,  hi:2000, def:280, send:v=>send("P 8 " + v)},
  {k:"sprd", lab:"SPREAD", lo:0,   hi:990,  def:300, send:v=>send("P 9 " + v)},
  {k:"pt1",  lab:"PITCH 1", lo:-100, hi:100, def:0, bi:true, send:v=>dronePitch(0, v)},
  {k:"pt2",  lab:"PITCH 2", lo:-100, hi:100, def:0, bi:true, send:v=>dronePitch(1, v)},
  {k:"pt3",  lab:"PITCH 3", lo:-100, hi:100, def:0, bi:true, send:v=>dronePitch(2, v)},
  {k:"pt4",  lab:"PITCH 4", lo:-100, hi:100, def:0, bi:true, send:v=>dronePitch(3, v)},
  {k:"pt5",  lab:"PITCH 5", lo:-100, hi:100, def:0, bi:true, send:v=>dronePitch(4, v)},
  {k:"ptA",  lab:"PITCH ALL", lo:-100, hi:100, def:0, bi:true, send:v=>send("T " + part + " -1 " + v)}
];
/* the Pod tunes a sounding note by cents; note -1 means the whole part */
function dronePitch(i, cents){
  const n = drone.held[i];
  if(n === undefined) return;
  send("T " + part + " " + n + " " + cents);
}
DR.forEach(p=>{ p.base = p.def; p.sent = -1; p.el = null; });

/* ------------------------------------------------------------------- LFOs */
const LSHAPES = ["SIN", "TRI", "SAW", "RAMP", "SQR", "S&H"];
const LFOS = [
  {on:true,  shape:0, rate:0.09, depth:0.45, targets:["cut"]},
  {on:true,  shape:1, rate:0.05, depth:0.30, targets:["ch"]},
  {on:false, shape:4, rate:0.25, depth:0.40, targets:["sprd"]}
];

function lfoVal(l, t){
  const ph = (t * l.rate) % 1;
  if(ph < 0) return 0;
  switch(l.shape){
    case 0: return Math.sin(ph * Math.PI * 2);              /* sine        */
    case 1: return ph < 0.5 ? (ph * 4 - 1) : (3 - ph * 4);  /* triangle    */
    case 2: return 1 - ph * 2;                              /* falling saw */
    case 3: return ph * 2 - 1;                              /* rising ramp */
    case 4: return ph < 0.5 ? 1 : -1;                       /* square      */
    default: {                                              /* sample+hold */
      const cyc = Math.floor(t * l.rate);
      let x = Math.sin(cyc * 12.9898) * 43758.5453;
      return (x - Math.floor(x)) * 2 - 1;
    }
  }
}
/* a fader's live value: its setting plus every LFO pointing at it */
function drEffective(p, t){
  let v = p.base;
  if(drone.lfoRun){
    for(const l of LFOS){
      if(!l.on || l.targets.indexOf(p.k) < 0) continue;
      v += lfoVal(l, t) * l.depth * (p.hi - p.lo) * 0.5;
    }
  }
  return Math.max(p.lo, Math.min(p.hi, v));
}
function drLfosOf(k){
  return LFOS.map((l, i)=> l.targets.indexOf(k) >= 0 ? i : -1).filter(i=>i >= 0);
}
/* tapping a fader's dot walks it through none -> LFO1 -> LFO1+2 -> all -> none */
function drCycle(k){
  const cur = drLfosOf(k);
  const next = cur.length === 0 ? [0] : (cur.length < LFOS.length ? cur.concat([cur.length]) : []);
  LFOS.forEach((l, i)=>{
    l.targets = l.targets.filter(x=>x !== k);
    if(next.indexOf(i) >= 0) l.targets.push(k);
  });
}
function drDropTarget(k, li){
  LFOS[li].targets = LFOS[li].targets.filter(x=>x !== k);
}

function drGet(k){ return DR.find(p=>p.k === k); }
function drPaint(p, v){
  if(!p.el) return;
  const f = Math.max(0, Math.min(1, (v - p.lo) / Math.max(1, p.hi - p.lo)));
  let base = 0, pc = f * 100;
  if(p.bi){                       /* pitch faders grow out of the centre */
    base = Math.min(f, 0.5) * 100;
    pc   = Math.abs(f - 0.5) * 100;
  }
  p.el.fill.style.bottom = base.toFixed(1) + "%";
  p.el.fill.style.height = pc.toFixed(1) + "%";
  p.el.th.style.bottom = "calc(" + (f * 100).toFixed(1) + "% - 3px)";
  p.el.val.textContent = Math.round(v);
}
function drSet(k, v, sendIt){
  const p = drGet(k);
  if(!p) return;
  p.base = Math.max(p.lo, Math.min(p.hi, v));
  drPaint(p, p.base);
  if(sendIt){ p.sent = p.base; p.send(Math.round(p.base)); }
}

/* the field and the rack are the same three parameters, so keep them together */
const droneSent = {cut:-1, res:-1, fb:-1};
let droneLastSend = 0;
function droneFlush(force){
  const t = performance.now();
  if(!force && t - droneLastSend < 90) return;
  droneLastSend = t;
  const cut = Math.round(drone.core.x * 1000);
  const res = Math.round((1 - drone.core.y) * 1000);
  const fb  = Math.round(drone.fb * 1000);
  const mirror = (i, val)=>{
    const el = document.getElementById("p" + i);
    if(el) el.value = val;
    const l = document.getElementById("v" + i);
    if(l) l.textContent = val;
  };
  if(cut !== droneSent.cut){ droneSent.cut = cut; send("P 0 " + cut); mirror(0, cut); drSet("cut", cut, false); }
  if(res !== droneSent.res){ droneSent.res = res; send("P 1 " + res); mirror(1, res); drSet("res", res, false); }
  if(fb  !== droneSent.fb ){ droneSent.fb  = fb;  send("P 2 " + fb);  mirror(2, fb);  drSet("fb",  fb,  false); }
}

function droneNote(s){ return padNote(s.deg); }  /* follows the keyboard octave */
function droneVel(lvl){ return Math.round(28 + 99 * Math.max(0, Math.min(1, lvl))); }

function droneFiring(step, when, dur){
  if(step % drone.div !== 0) return;             /* the pulse lands on this grid */
  const s = drone.stones[0];
  fireNote(droneNote(s), droneVel(s.lvl), when, dur * drone.div * 1.05, part);
}

function droneStart(){
  droneHeldOff();
  drone.stones.forEach(s=>{
    const n = droneNote(s);
    send("N " + n + " " + droneVel(s.lvl) + " " + part);
    drone.held.push(n);
  });
  if(drone.alg >= 0) send("P 3 " + drone.alg);
  if(drone.wave >= 0) for(let o = 0; o < 6; o++) send("S " + o + " 16 " + drone.wave);
  /* push the whole rack once so the Pod matches what the screen shows */
  DR.forEach(p=>{ p.sent = p.base; p.send(Math.round(p.base)); });
  drone.on = true;
  startEngine("drone");
}
function droneHeldOff(){
  drone.held.forEach(n=>send("O " + n));
  drone.held = [];
}
function droneStop(){
  drone.on = false;
  stopAll();
  droneHeldOff();
}
function droneRetrigger(i){
  const s = drone.stones[i];
  if(!drone.on){ drone.held[i] = droneNote(s); return; }
  const old = drone.held[i], n = droneNote(s);
  if(old !== undefined) send("O " + old);
  send("N " + n + " " + droneVel(s.lvl) + " " + part);
  drone.held[i] = n;
}

/* the glow stays inside the middle of the field: the ring has to keep fitting */
function droneSetCore(px, py, b){
  const x = (px - (b.cx - b.w / 2)) / b.w;
  const y = (py - (b.cy - b.h / 2)) / b.h;
  drone.core.x = Math.max(0.22, Math.min(0.78, x));
  drone.core.y = Math.max(0.22, Math.min(0.78, y));
}

/* ------------------------------------------------------------------ knobs */
function makeKnob(label, min, max, val, onchange, fmt){
  const wrap = document.createElement("div"); wrap.className = "kw";
  const k = document.createElement("div"); k.className = "knob";
  const dial = document.createElement("div"); dial.className = "dial";
  const ptr = document.createElement("div"); ptr.className = "ptr";
  dial.appendChild(ptr); k.appendChild(dial);
  const lab = document.createElement("div"); lab.className = "klab"; lab.textContent = label;
  const v = document.createElement("div"); v.className = "kval";
  wrap.appendChild(k); wrap.appendChild(lab); wrap.appendChild(v);

  let cur = val;
  const paint = ()=>{
    const f = (cur - min) / Math.max(0.000001, max - min);
    dial.style.transform = "rotate(" + (-135 + f * 270).toFixed(1) + "deg)";
    v.textContent = fmt ? fmt(cur) : cur.toFixed(2);
  };
  paint();

  let drag = false, lastY = 0;
  k.addEventListener("pointerdown", ev=>{
    ev.preventDefault(); drag = true; lastY = ev.clientY;
    try { k.setPointerCapture(ev.pointerId); } catch(e) {}
  });
  k.addEventListener("pointermove", ev=>{
    if(!drag) return;
    const dy = lastY - ev.clientY;
    lastY = ev.clientY;
    cur = Math.max(min, Math.min(max, cur + (dy / 170) * (max - min)));
    paint(); onchange(cur);
  });
  const up = ()=>{ drag = false; };
  k.addEventListener("pointerup", up);
  k.addEventListener("pointercancel", up);
  k.addEventListener("lostpointercapture", up);
  wrap.setValue = (x)=>{ cur = x; paint(); };
  return wrap;
}

/* --------------------------------------------------------------- presets --- */
/* Each preset is only the difference from the default drone, so a reset is the
   same code path as picking one. */
const DRONE_PRESETS = {
  "TANPURA":  {lab:"TANPURA",  note:"root, fifth, octaves; slow drift, chorus",
               lfo:[{on:true, shape:0, rate:0.09, depth:0.45, targets:["cut"]},
                    {on:true, shape:1, rate:0.05, depth:0.30, targets:["ch"]},
                    {on:false, shape:4, rate:0.25, depth:0.40, targets:["sprd"]}]},
  "DEEP":     {lab:"DEEP",     note:"low register, long delay, wide spread",
               stones:[[0,0.95],[2,0.6],[4,0.5],[7,0.4],[9,0.3]],
               core:[0.34,0.55], fb:0.5, div:8,
               p:{cut:330,res:560,dms:640,dmix:420,sprd:700,ch:150},
               lfo:[{on:true, shape:0, rate:0.04, depth:0.5, targets:["cut"]},
                    {on:true, shape:1, rate:0.03, depth:0.35, targets:["dmix"]},
                    {on:true, shape:5, rate:0.08, depth:0.25, targets:["dms"]}]},
  "BELL":     {lab:"BELL",     note:"inharmonic fan, bright, dry",
               stones:[[0,0.9],[7,0.7],[14,0.55],[4,0.45],[11,0.5]],
               core:[0.86,0.30], fb:0.2, div:1,
               p:{cut:880,res:220,fan:4,olvl:88,ch:60,dmix:300,dms:180,sprd:420},
               lfo:[{on:true, shape:0, rate:0.05, depth:0.35, targets:["fan"]},
                    {on:true, shape:1, rate:0.12, depth:0.30, targets:["sprd"]},
                    {on:false, shape:4, rate:0.25, depth:0.40, targets:["cut"]}]},
  "GLASS":    {lab:"GLASS",    note:"fine detune shimmer, wide stereo",
               core:[0.7,0.42], fb:0.3, div:2,
               p:{cut:740,res:380,fine:55,ch:560,dmix:340,dms:220,sprd:820},
               lfo:[{on:true, shape:1, rate:0.07, depth:0.5, targets:["fine","sprd"]},
                    {on:true, shape:0, rate:0.03, depth:0.3, targets:["cut"]},
                    {on:false, shape:4, rate:0.2, depth:0.3, targets:["dmix"]}]},
  "FROZEN":   {lab:"FROZEN",   note:"feedback heavy, dark, very slow",
               core:[0.28,0.62], fb:0.82, div:8,
               p:{cut:260,res:700,dmix:560,dms:900,ch:220,sprd:260},
               lfo:[{on:true, shape:0, rate:0.03, depth:0.6, targets:["fb","cut"]},
                    {on:true, shape:5, rate:0.11, depth:0.25, targets:["dmix"]},
                    {on:true, shape:0, rate:0.02, depth:0.30, targets:["dms"]}]},
  "PULSE":    {lab:"PULSE",    note:"short delay, fast pulse, rhythmic",
               core:[0.55,0.4], fb:0.55, div:1,
               p:{cut:600,res:420,dmix:520,dms:130,ch:180,sprd:340},
               lfo:[{on:true, shape:4, rate:0.5, depth:0.45, targets:["cut"]},
                    {on:true, shape:1, rate:0.25, depth:0.30, targets:["dmix"]},
                    {on:false, shape:0, rate:0.1, depth:0.3, targets:["sprd"]}]}
};

function droneDefaults(){
  drone.core.x = 0.5; drone.core.y = 0.35;
  drone.fb = 0.35; drone.div = 4; drone.alg = -1; drone.wave = -1; drone.lfoRun = true;
  const deg = [0, 4, 7, 11, 14], lvl = [0.90, 0.55, 0.45, 0.34, 0.24];
  drone.stones.forEach((st, i)=>{ st.deg = deg[i]; st.lvl = lvl[i]; });
  DR.forEach(q=>{ q.base = q.def; q.sent = -1; drPaint(q, q.base); });
  LFOS[0] = {on:true,  shape:0, rate:0.09, depth:0.45, targets:["cut"]};
  LFOS[1] = {on:true,  shape:1, rate:0.05, depth:0.30, targets:["ch"]};
  LFOS[2] = {on:false, shape:4, rate:0.25, depth:0.40, targets:["sprd"]};
}

function applyPreset(name){
  droneDefaults();
  const pre = name ? DRONE_PRESETS[name] : null;
  if(pre){
    if(pre.stones) pre.stones.forEach((v, i)=>{ if(drone.stones[i]){ drone.stones[i].deg = v[0]; drone.stones[i].lvl = v[1]; } });
    if(pre.core){ drone.core.x = pre.core[0]; drone.core.y = pre.core[1]; }
    if(typeof pre.fb === "number") drone.fb = pre.fb;
    if(pre.div) drone.div = pre.div;
    if(pre.p) Object.keys(pre.p).forEach(k=>{ const q = drGet(k); if(q) q.base = pre.p[k]; });
    if(pre.lfo) pre.lfo.forEach((l, i)=>{ if(LFOS[i]){ LFOS[i].on = !!l.on; LFOS[i].shape = l.shape; LFOS[i].rate = l.rate; LFOS[i].depth = l.depth; LFOS[i].targets = l.targets.slice(); } });
    for(let i = pre.lfo ? pre.lfo.length : 0; i < LFOS.length; i++) LFOS[i].on = false;
  }
  const wasOn = drone.on;
  if(wasOn) droneStop();
  renderView();
  if(wasOn) droneStart();
  else { DR.forEach(q=>{ if(q.k.indexOf("pt") !== 0){ q.sent = q.base; q.send(Math.round(q.base)); } }); }
  saveState();
}

function buildDrone(){
  const v = document.getElementById("view"); v.innerHTML = "";
  const wrap = document.createElement("div"); wrap.id = "drone";

  /* ---------------------------------------------------------------- toolbar */
  const bar = document.createElement("div"); bar.className = "seqbar";
  const play = document.createElement("button"); play.id = "droneplay";
  play.textContent = tOn("drone") ? "RELEASE" : "HOLD";
  if(tOn("drone")) play.classList.add("on");
  play.onclick = ()=>{ if(tOn("drone")) droneStop(); else droneStart(); };
  bar.appendChild(play);

  const runB = document.createElement("button");
  runB.textContent = drone.lfoRun ? "LFO RUN" : "LFO OFF";
  if(drone.lfoRun) runB.classList.add("on");
  runB.onclick = ()=>{
    drone.lfoRun = !drone.lfoRun;
    runB.textContent = drone.lfoRun ? "LFO RUN" : "LFO OFF";
    runB.classList.toggle("on", drone.lfoRun);
    DR.forEach(p=>{ p.sent = -1; });     /* let the next tick resend either way */
    saveState();
  };
  bar.appendChild(runB);

  const lab = (t)=>{ const s = document.createElement("span"); s.className = "lab"; s.textContent = t; return s; };
  bar.appendChild(lab("pulse"));
  [[ "1/4", 4], [ "1/8", 2], [ "1/16", 1], [ "1/2", 8]].forEach(([t, d])=>{
    const b = document.createElement("button"); b.textContent = t; b.className = "pdiv";
    if(drone.div === d) b.classList.add("on");
    b.onclick = ()=>{ drone.div = d;
      bar.querySelectorAll("button.pdiv").forEach(x=>x.classList.remove("on"));
      b.classList.add("on"); };
    bar.appendChild(b);
  });

  const algB = document.createElement("button");
  const algNow = ()=> drone.alg >= 0 ? drone.alg : patchAlg;
  algB.textContent = "ALG " + (algNow() + 1);
  algB.onclick = ()=>{ drone.alg = (algNow() + 1) % 32; algB.textContent = "ALG " + (drone.alg + 1);
    send("P 3 " + drone.alg); };
  bar.appendChild(algB);

  const waveB = document.createElement("button");
  waveB.textContent = "WAVE " + ((drone.wave >= 0 ? drone.wave : 0) + 1);
  waveB.onclick = ()=>{ drone.wave = ((drone.wave >= 0 ? drone.wave : 0) + 1) % 6;
    waveB.textContent = "WAVE " + (drone.wave + 1);
    for(let o = 0; o < 6; o++) send("S " + o + " 16 " + drone.wave); };
  bar.appendChild(waveB);
  const preSel = document.createElement("select"); preSel.className = "pbtn"; preSel.id = "dpreset";
  const ph = document.createElement("option"); ph.value = ""; ph.textContent = "preset...";
  preSel.appendChild(ph);
  Object.keys(DRONE_PRESETS).forEach(k=>{
    const o = document.createElement("option"); o.value = k; o.textContent = DRONE_PRESETS[k].lab;
    preSel.appendChild(o);
  });
  preSel.onchange = ()=>{ if(preSel.value) applyPreset(preSel.value); };
  bar.appendChild(preSel);

  const resetB = document.createElement("button"); resetB.id = "dreset";
  resetB.textContent = "RESET";
  resetB.onclick = ()=> applyPreset(null);
  bar.appendChild(resetB);

  bar.appendChild(lab("root " + NOTE[padNote(0) % 12] + (Math.floor(padNote(0) / 12) - 1)));
  wrap.appendChild(bar);

  const body = document.createElement("div"); body.id = "dbody";

  /* ------------------------------------------------------------------ field */
  const field = document.createElement("div"); field.id = "dfield";
  const veil = document.createElement("div"); veil.className = "dveil"; field.appendChild(veil);
  const vh = document.createElement("div"); vh.className = "dvh"; field.appendChild(vh);
  const core = document.createElement("div"); core.id = "dcore"; field.appendChild(core);

  const stones = drone.stones.map((s, i)=>{
    const d = document.createElement("div"); d.className = "dstone";
    const n = document.createElement("span"); n.className = "dn";
    d.appendChild(n); field.appendChild(d);
    return {el:d, note:n};
  });
  body.appendChild(field);

  /* ---------------------------------------------------------------- LFO bay */
  const bay = document.createElement("div"); bay.id = "dlfo";
  LFOS.forEach((l, li)=>{
    const pan = document.createElement("div"); pan.className = "lp";
    if(!l.on) pan.classList.add("off");

    const head = document.createElement("div"); head.className = "lph";
    const nm = document.createElement("b"); nm.textContent = "LFO " + (li + 1);
    const en = document.createElement("button"); en.className = "pdot"; en.title = "on / off";
    if(l.on) en.classList.add("on");
    en.onclick = ()=>{ l.on = !l.on; en.classList.toggle("on", l.on);
      pan.classList.toggle("off", !l.on); DR.forEach(p=>{ p.sent = -1; }); saveState(); };
    head.appendChild(nm); head.appendChild(en);
    pan.appendChild(head);

    const shapes = document.createElement("div"); shapes.className = "shapes";
    LSHAPES.forEach((s, si)=>{
      const b = document.createElement("button"); b.textContent = s;
      if(l.shape === si) b.classList.add("on");
      b.onclick = ()=>{ l.shape = si;
        shapes.querySelectorAll("button").forEach(x=>x.classList.remove("on"));
        b.classList.add("on"); saveState(); };
      shapes.appendChild(b);
    });
    pan.appendChild(shapes);

    const knobs = document.createElement("div"); knobs.className = "knobs";
    knobs.appendChild(makeKnob("RATE", 0.01, 8, l.rate, x=>{ l.rate = x; saveState(); },
      x=> x < 1 ? x.toFixed(2) + " Hz" : x.toFixed(1) + " Hz"));
    knobs.appendChild(makeKnob("DEPTH", 0, 1, l.depth, x=>{ l.depth = x; saveState(); },
      x=>Math.round(x * 100) + "%"));
    pan.appendChild(knobs);

    const chips = document.createElement("div"); chips.className = "lchips";
    const drawChips = ()=>{
      chips.innerHTML = "";
      if(!l.targets.length){
        const none = document.createElement("span"); none.className = "lnone"; none.textContent = "no targets";
        chips.appendChild(none);
      }
      l.targets.forEach(k=>{
        const p = drGet(k);
        const c = document.createElement("button"); c.className = "chip";
        c.textContent = (p ? p.lab : k) + " ×";
        c.onclick = ()=>{ drDropTarget(k, li); drawChips(); saveState(); };
        chips.appendChild(c);
      });
    };
    drawChips();
    pan.appendChild(chips);

    const sel = document.createElement("select");
    const none = document.createElement("option"); none.value = ""; none.textContent = "+ add target";
    sel.appendChild(none);
    DR.forEach(p=>{
      const o = document.createElement("option"); o.value = p.k; o.textContent = "+ " + p.lab;
      sel.appendChild(o);
    });
    sel.onchange = ()=>{
      const k = sel.value;
      sel.value = "";
      if(!k || l.targets.indexOf(k) >= 0) return;
      l.targets.push(k);
      drawChips(); saveState();
    };
    pan.appendChild(sel);

    bay.appendChild(pan);
  });
  body.appendChild(bay);
  wrap.appendChild(body);

  /* ------------------------------------------------------------------- rack */
  const rack = document.createElement("div"); rack.className = "drack";
  DR.forEach((p, i)=>{
    const cell = document.createElement("div"); cell.className = "pf";
    const dot = document.createElement("button"); dot.className = "pdot";
    dot.title = "LFOs driving this";
    const owners = drLfosOf(p.k);
    if(owners.length){ dot.classList.add("on"); dot.textContent = owners.map(i=>i + 1).join("·"); }
    dot.onclick = ()=>{ drCycle(p.k); renderView(); saveState(); };
    const slot = document.createElement("div"); slot.className = "pslot";
    const fill = document.createElement("div"); fill.className = "pfill";
    const th = document.createElement("div"); th.className = "pthumb";
    slot.appendChild(fill); slot.appendChild(th);
    const l2 = document.createElement("div"); l2.className = "plab"; l2.textContent = p.lab;
    const val = document.createElement("div"); val.className = "pval";
    cell.appendChild(dot); cell.appendChild(slot); cell.appendChild(l2); cell.appendChild(val);
    rack.appendChild(cell);
    p.el = {dot, slot, fill, th, val};
    drPaint(p, p.base);

    const set = (ev)=>{
      const r = slot.getBoundingClientRect();
      const f = 1 - (ev.clientY - r.top) / Math.max(1, r.height);
      p.base = p.lo + Math.max(0, Math.min(1, f)) * (p.hi - p.lo);
      drPaint(p, p.base);
      p.sent = p.base;
      p.send(Math.round(p.base));
      if(p.k === "cut" || p.k === "res" || p.k === "fb"){
        if(p.k === "cut") drone.core.x = p.base / 1000;
        if(p.k === "res") drone.core.y = 1 - p.base / 1000;
        if(p.k === "fb")  drone.fb = p.base / 1000;
        if(dronePaint) dronePaint();
      }
    };
    slot.addEventListener("pointerdown", (ev)=>{
      ev.preventDefault(); slot.setPointerCapture(ev.pointerId); p.hot = true; set(ev); });
    slot.addEventListener("pointermove", (ev)=>{ if(p.hot) set(ev); });
    const up = ()=>{ p.hot = false; };
    slot.addEventListener("pointerup", up);
    slot.addEventListener("pointercancel", up);
    slot.addEventListener("lostpointercapture", up);
  });
  wrap.appendChild(rack);
  v.appendChild(wrap);

  /* ------------------------------------------------------------------ paint */
  const box = ()=>{ const r = field.getBoundingClientRect();
    return {w:r.width, h:r.height, cx:r.left + r.width / 2, cy:r.top + r.height / 2,
            half: Math.min(r.width, r.height) * 0.44}; };

  function paint(){
    const b = box();
    if(!b.w) return;
    const ccx = b.w * drone.core.x, ccy = b.h * drone.core.y;
    core.style.left = (drone.core.x * 100) + "%";
    core.style.top  = (drone.core.y * 100) + "%";
    const vr = b.half * (1.02 - drone.fb * 0.30);
    veil.style.width = veil.style.height = (vr * 2) + "px";
    veil.style.left = ccx + "px"; veil.style.top = ccy + "px";
    veil.style.transform = "translate(-50%,-50%)";
    veil.style.opacity = (0.20 + drone.fb * 0.6).toFixed(2);
    vh.style.left = ccx + "px";
    vh.style.top = (ccy - vr) + "px";
    drone.stones.forEach((s, i)=>{
      const ang = (s.deg / 14) * Math.PI * 2 - Math.PI / 2;
      const ca = Math.cos(ang), sa = Math.sin(ang);
      const edgeX = ca > 0.001 ? (b.w - ccx) / ca : (ca < -0.001 ? ccx / -ca : 1e9);
      const edgeY = sa > 0.001 ? (b.h - ccy) / sa : (sa < -0.001 ? ccy / -sa : 1e9);
      const room  = Math.min(edgeX, edgeY) - 44;
      const rad   = Math.max(46, Math.min(b.half * (0.30 + s.lvl * 0.70), room));
      const st = stones[i];
      st.el.style.left = (ccx + ca * rad).toFixed(1) + "px";
      st.el.style.top  = (ccy + sa * rad).toFixed(1) + "px";
      st.el.style.transform = "translate(-50%,-50%)";
      st.el.style.setProperty("--a", (0.30 + s.lvl * 0.65).toFixed(2));
      const nn = droneNote(s);
      st.note.textContent = NOTE[nn % 12] + (Math.floor(nn / 12) - 1);
    });
  }
  dronePaint = paint;

  let drag = null;
  field.addEventListener("pointerdown", (ev)=>{
    const b = box();
    const target = ev.target.closest ? ev.target.closest(".dstone") : null;
    if(ev.target === vh){
      drag = {kind:"veil"}; vh.setPointerCapture(ev.pointerId);
    } else if(target){
      const i = stones.findIndex(s=>s.el === target);
      drag = {kind:"stone", i}; target.classList.add("up");
      target.setPointerCapture(ev.pointerId);
    } else {
      drag = {kind:"core"};
      field.setPointerCapture(ev.pointerId);
      droneSetCore(ev.clientX, ev.clientY, b);
      paint(); droneFlush();
    }
    ev.preventDefault();
  });
  field.addEventListener("pointermove", (ev)=>{
    if(!drag) return;
    const b = box();
    const ccx = b.cx - b.w / 2 + b.w * drone.core.x;
    const ccy = b.cy - b.h / 2 + b.h * drone.core.y;
    if(drag.kind === "core"){
      droneSetCore(ev.clientX, ev.clientY, b);
      paint(); droneFlush();
    } else if(drag.kind === "veil"){
      const dy = Math.abs(ev.clientY - ccy);
      drone.fb = Math.max(0.02, Math.min(0.95, 1 - dy / b.half));
      paint(); droneFlush();
    } else {
      const s = drone.stones[drag.i];
      const dx = ev.clientX - ccx, dy = ev.clientY - ccy;
      let ang = Math.atan2(dy, dx) + Math.PI / 2;
      if(ang < 0) ang += Math.PI * 2;
      const deg = Math.round((ang / (Math.PI * 2)) * 14);
      const dist = Math.hypot(dx, dy);
      s.deg = Math.max(0, Math.min(14, deg));
      s.lvl = Math.max(0.06, Math.min(1, (dist / b.half - 0.30) / 0.70));
      paint();
    }
  });
  const endDrag = ()=>{
    if(drag && drag.kind === "stone"){
      stones[drag.i].el.classList.remove("up");
      droneRetrigger(drag.i);
      const pp = drGet("pt" + (drag.i + 1));
      if(pp && Math.round(pp.base) !== 0) pp.send(Math.round(pp.base));
    }
    if(drag && drag.kind !== "stone") droneFlush(true);
    drag = null;
  };
  field.addEventListener("pointerup", endDrag);
  field.addEventListener("pointercancel", endDrag);
  field.addEventListener("lostpointercapture", endDrag);

  /* the core breathes on the pulse, so the field moves even when untouched */
  let t0 = performance.now(), raf = 0;
  function loop(t){
    if(!document.body.contains(field)){
      /* only tear down shared state if no newer drone view has taken over */
      cancelAnimationFrame(raf);
      if(!document.getElementById("dfield")) droneModStop();
      if(dronePaint === paint) dronePaint = null;
      return;
    }
    const beat = (t - t0) / (60000 / Math.max(40, bpm)) * (4 / drone.div);
    const s = 1 + Math.sin(beat * Math.PI * 2) * (0.03 + drone.fb * 0.10);
    core.style.transform = "translate(-50%,-50%) scale(" + s.toFixed(3) + ")";
    raf = requestAnimationFrame(loop);
  }
  raf = requestAnimationFrame(loop);
  paint();
  droneModStart();
}

/* --------------------------------------------------------------- modulators */
let droneModTimer = null;
function droneModStart(){
  if(droneModTimer) return;
  droneModTimer = setInterval(droneModTick, 110);
  droneModTick();
}
function droneModStop(){
  if(droneModTimer){ clearInterval(droneModTimer); droneModTimer = null; }
}
function droneModTick(){
  if(!document.getElementById("dfield")){ droneModStop(); return; }
  const t = performance.now() / 1000;
  DR.forEach(p=>{
    if(p.hot) return;
    const v = drEffective(p, t);
    if(p.sent === -1 || Math.abs(v - p.sent) >= Math.max(1, (p.hi - p.lo) * 0.006)){
      p.sent = v;
      p.send(Math.round(v));
      drPaint(p, v);
      if(p.k === "cut"){ drone.core.x = v / 1000; if(dronePaint) dronePaint(); }
      if(p.k === "res"){ drone.core.y = 1 - v / 1000; if(dronePaint) dronePaint(); }
      if(p.k === "fb"){  drone.fb = v / 1000;     if(dronePaint) dronePaint(); }
    }
  });
}

/* ------------------------------------------------------------ microtonal --- */
/* Two isomorphic layouts over the same tuning maths. Every key is one step of
   the EDO, placed at c*generator + r*third, so a given interval is the same
   shape anywhere on the board. The synth takes 12-TET notes with a cents
   offset, so a key sends the nearest note number plus its own offset and the
   release carries the same offset to keep quarter tones apart. */
/* g = fifths per octave, s = the row step. s must be COPRIME with g, otherwise
   the grid only ever lands on a fraction of the steps: 24-EDO with g=14 and s=8
   both even leaves every key exactly on a semitone and no quarter tone reachable. */
const EDO_CFG = {
  12: { g:7,  s:4  },   /* 600c rows, major third          */
  19: { g:11, s:6  },   /* 379c rows                       */
  24: { g:14, s:9  },   /* 450c rows, odd so parity varies */
  31: { g:18, s:11 }    /* 426c rows                       */
};
/* Axis choice, and why it matters: a real Lumatone's honeycomb puts ONE SMALL STEP
   along the short axis and the fifth or third along the other, so 280 keys cover a
   few octaves. Put fifths on both and 20 columns climb 20 fifths, which is 12
   octaves of inaudible squeak at the right hand edge.
   hex  : right = one EDO step, up = the EDO's major third  (Lumatone-like)
   bosa : right = the fifth, up = the third                 (Bosanquet chain of fifths)
   Both are centred on C4 so the board sits in a playable register. */
function microCents(c, r, cols, rows){
  const cfg = EDO_CFG[micro.edo];
  const stepX = micro.layout === "hex" ? 1 : cfg.g;
  const cx = (cols - 1) / 2, cy = (rows - 1) / 2;
  const steps = (c - cx) * stepX - (r - cy) * cfg.s;
  return 6000 + micro.root * 100 + steps * (1200 / micro.edo);
}
function microKey(c, r, cols, rows, forcedNote, forcedLabel, forcedCents){
  const cents = microCents(c, r, cols, rows);
  const note = (forcedNote !== undefined && forcedNote !== null)
             ? Math.max(0, Math.min(127, forcedNote | 0))
             : Math.max(0, Math.min(127, Math.round(cents / 100)));
  const off = (forcedCents !== undefined && forcedCents !== null)
            ? (forcedCents | 0)
            : Math.round(cents - note * 100);
  const cfg = EDO_CFG[micro.edo];
  const stepX = micro.layout === "hex" ? 1 : cfg.g;
  const deg = Math.round(((c - (cols - 1) / 2) * stepX - (r - (rows - 1) / 2) * cfg.s) % micro.edo);
  const degn = ((deg % micro.edo) + micro.edo) % micro.edo;
  const hue = Math.round(degn / micro.edo * 360);
  const d = document.createElement("div");
  d.className = "mkey";
  d.dataset.note = note;
  d.dataset.cents = off;
  d.dataset.h = hue;
  d.style.background = "hsl(" + hue + ",62%," + (58 + (off % 12)) + "%)";
  d.textContent = forcedLabel ? forcedLabel : (NOTE[note % 12] + (Math.floor(note / 12) - 1));
  const sub = document.createElement("small");
  sub.textContent = forcedLabel ? "" : ((off === 0 ? "0" : (off > 0 ? "+" + off : String(off))) + "c");
  d.appendChild(sub);
  d.addEventListener("pointerdown", ev=>{
    ev.preventDefault();
    if(fingers.has(ev.pointerId)) return;
    const y = touchY(ev, d);
    const vel = Math.round(36 + y * 91);
    /* the split applies to the fingers, not just to files */
    const zn = +d.dataset.zone || 0;
    const liveP = d.dataset.part !== undefined ? (+d.dataset.part)
               : (zn ? zn : (micro.splitOn ? ((note < (micro.split|0)) ? 1 : 0) : part));
    fingers.set(ev.pointerId, {notes:[note], micro:[off], el:d, y:y, x:0, bx:ev.clientX,
      cents:0, sent:0, target:0, origin:note, hover:null, hoverAt:0, locked:false,
      lockCents:null, chording:false});
    send("N " + note + " " + vel + " " + liveP + " " + off);
    d.classList.add("up");
    showFingers();
    try { d.setPointerCapture(ev.pointerId); } catch(e) {}
  });
  return d;
}
function buildMicro(){
  const v = document.getElementById("view"); v.innerHTML = "";
  v.style.display = "flex"; v.style.flexDirection = "column"; v.style.minHeight = "0";
  const wrap = document.createElement("div"); wrap.id = "micro";
  wrap.style.cssText = "display:flex; flex-direction:column; flex:1 1 auto; min-height:0;";

  const bar = document.createElement("div"); bar.className = "seqbar";
  const lab = (t)=>{ const s=document.createElement("span"); s.className="lab"; s.textContent=t; return s; };
  bar.appendChild(lab("EDO"));
  [12, 19, 24, 31].forEach(n=>{
    const b = document.createElement("button");
    b.textContent = n;
    if(micro.edo === n) b.classList.add("on");
    b.onclick = ()=>{ micro.edo = n; buildMicro(); saveState(); };
    bar.appendChild(b);
  });
  bar.appendChild(lab("LAYOUT"));
  const lb = document.createElement("button");
  lb.textContent = micro.layout === "hex" ? "LUMATONE" : "BOSANQUET";
  if(micro.layout === "hex") lb.classList.add("on");
  lb.onclick = ()=>{ micro.layout = micro.layout === "hex" ? "bosa" : "hex"; buildMicro(); saveState(); };
  bar.appendChild(lb);
  const rb = document.createElement("button");
  rb.textContent = "ROOT " + NOTE[(60 + micro.root) % 12];
  rb.onclick = ()=>{ micro.root = (micro.root + 1) % 12; buildMicro(); saveState(); };
  bar.appendChild(rb);
  /* the file transport rides on this board: no second keyboard */
  const sel = document.createElement("select"); sel.id = "midisel"; sel.className = "pbtn";
  sel.style.minWidth = "230px";
  sel.onchange = ()=>{ if(midi.playing) midiStop(); };
  const ld = document.createElement("button"); ld.id = "midiload"; ld.className = "pbtn small"; ld.textContent = "⤓"; ld.title = "load midi file";
  if(sel){
    sel.addEventListener("mousedown", ()=> refreshMidiLists());
    sel.addEventListener("focus", ()=> refreshMidiLists());
  }
  ld.onclick = ()=>{
    const f = sel.value;
    if(!f) return;
    midiStop();
    fetch("/midi/f?f=" + encodeURIComponent(f)).then(r=> r.arrayBuffer()).then(buf=>{
      try{
        const m = parseSmf(buf);
        midi.evs = m.evs; midi.endMs = m.endMs; midi.notes = m.notes; midi.name = f;
      }catch(err){
        midi.evs = []; midi.name = f; midi.notes = 0; midi.endMs = 0;
        const st = document.getElementById("midistat");
        if(st) st.textContent = "cannot parse: " + err.message;
        return;
      }
      paintMidi();
    }).catch(()=>{});
  };
  const pl = document.createElement("button"); pl.id = "midiplay"; pl.className = "pbtn small"; pl.textContent = "▶"; pl.title = "play or stop the file";
  pl.onclick = ()=> midi.playing ? midiStop() : midiPlay();
  const pm = document.createElement("button"); pm.id = "midipart";
  pm.onclick = midiPartCycle;
  const lp = document.createElement("button"); lp.id = "midiloop"; lp.className = "pbtn small"; lp.textContent = "⟲"; lp.title = "loop the file";
  lp.onclick = ()=>{ midi.loop = !midi.loop; paintMidi(); };
  lp.classList.toggle("on", !!midi.loop);
  const st = document.createElement("span"); st.className = "lab"; st.id = "midistat";
  const sp = document.createElement("input");
  sp.type = "number"; sp.step = "0.01"; sp.min = "0.5"; sp.max = "1.5";
  sp.value = (typeof micro.bassSpan === "number" ? micro.bassSpan : 1.2).toFixed(2);
  sp.title = "bass span: 1.00 is chromatic, lower compresses, higher stretches";
  sp.style.cssText = "width:62px; height:30px; background:#12181c; color:#e8fbff; border:1px solid #2a3238;" +
    " border-radius:7px; text-align:center; font:600 13px system-ui;";
  sp.onchange = ()=>{
    const v = Math.max(0.5, Math.min(1.5, parseFloat(sp.value) || 1.2));
    micro.bassSpan = Math.round(v * 100) / 100;
    sp.value = micro.bassSpan.toFixed(2);
    buildMicro(); saveState();
  };
  const bo = document.createElement("button"); bo.className = "pbtn small"; bo.textContent = "B-";
  bo.title = "bass down a semitone";
  bo.onclick = ()=>{ micro.bassOfs = ((typeof micro.bassOfs === "number") ? micro.bassOfs : -8) - 1; buildMicro(); saveState(); };
  const bu = document.createElement("button"); bu.className = "pbtn small"; bu.textContent = "B+";
  bu.title = "bass up a semitone";
  bu.onclick = ()=>{ micro.bassOfs = ((typeof micro.bassOfs === "number") ? micro.bassOfs : -8) + 1; buildMicro(); saveState(); };
  const drg = document.createElement("button"); drg.className = "pbtn small"; drg.id = "dragmode";
  drg.textContent = "↔"; drg.title = "drag across the keys to glide";
  drg.classList.toggle("on", !!micro.dragOn);
  drg.onclick = ()=>{ micro.dragOn = !micro.dragOn; buildMicro(); saveState(); };
  const stg = document.createElement("button"); stg.className = "pbtn small"; stg.id = "ksplit";
  stg.onclick = ()=>{
    if(!micro.splitOn) micro.splitOn = true;
    else if(micro.splitMode !== "abcd") micro.splitMode = "abcd";
    else { micro.splitOn = false; micro.splitMode = "ab"; }
    buildMicro(); saveState();
  };
  stg.textContent = !micro.splitOn ? "A only" : (micro.splitMode === "abcd" ? "A|B|C|D" : "A|B");
  stg.title = "cycle the keyboard split: off, two instruments, then drums and bass along the bottom";
  stg.classList.toggle("on", !!micro.splitOn);
  const sm = document.createElement("button"); sm.className = "pbtn small"; sm.textContent = "-";
  sm.title = "move the split down";
  sm.onclick = ()=>{ micro.split = Math.max(24, (micro.split|0) - 1); paintMidi(); buildMicro(); saveState(); };
  const sl = document.createElement("span"); sl.className = "lab"; sl.id = "splitlab";
  const spl = document.createElement("button"); spl.className = "pbtn small"; spl.textContent = "+";
  spl.title = "move the split up";
  spl.onclick = ()=>{ micro.split = Math.min(96, (micro.split|0) + 1); paintMidi(); buildMicro(); saveState(); };
  bar.append(sel, ld, pl, pm, lp, sp, bo, bu, drg, stg, sm, sl, st);
  refreshMidiLists().then(()=> paintMidi());
  const sz = document.createElement("button"); sz.className = "pbtn small";
  sz.textContent = micro.size === "small" ? "▦" : "▩"; sz.title = micro.size === "small" ? "small keys" : "big keys";
  if(micro.size === "small") sz.classList.add("on");
  sz.onclick = ()=>{
    micro.size = micro.size === "small" ? "big" : "small";
    /* small keys means more columns, which needs the width: fold the knobs away */
    fxHidden = (micro.size === "small");
    paintFx();
    buildMicro(); saveState();
  };
  bar.appendChild(sz);
  const info = lab(EDO_CFG[micro.edo].g + " fifths / " + EDO_CFG[micro.edo].s + " thirds per octave, " +
                   (1200 / micro.edo).toFixed(2) + "c per key");
  bar.appendChild(info);
  wrap.appendChild(bar);

  if(micro.layout === "hex"){
    wrap.appendChild(hexGrid(v));
    v.appendChild(wrap);
    /* measure after the grid is in the page: the toolbar height is not a constant,
       and guessing it clipped the bottom row */
    return;
  }
  const grid = document.createElement("div"); grid.id = "mrow";
  const COLS = 9, ROWS = 4;
  for(let r = 0; r < ROWS; r++){
    const row = document.createElement("div"); row.className = "mrow";
    for(let c = 0; c < COLS; c++) row.appendChild(microKey(c, r, COLS, ROWS));
    grid.appendChild(row);
  }
  wrap.appendChild(grid);
  v.appendChild(wrap);
}

/* ------------------------------------------------------------ midi player --- */
/* Standard MIDI files, which is how Lumatone and xenharmonic pieces circulate.
   Microtones ride on pitch bend: the file gives note + bend, we send the note
   number with the bend converted to cents, so a quarter tone plays as N 60 ... 50.
   RPN 0,0 (pitch bend sensitivity) is honoured per channel, default 2 semitones. */
const midi = {
  loop: false,
  partMode: "a",   /* a = everything on part A, chan = by channel, key = by register */
  partSplit: 55,   /* the note where B starts when splitting by register */ name:"", evs:[], endMs:0, notes:0, playing:false, t0:0, i:0,
               timer:null, bend:{}, range:{}, held:new Map() };

function parseSmf(buf){
  const b = new Uint8Array(buf);
  let p = 0;
  const u8  = ()=> b[p++];
  const u16 = ()=> (b[p++] << 8) | b[p++];
  const u32 = ()=> (((b[p++] << 24) | (b[p++] << 16) | (b[p++] << 8) | b[p++]) >>> 0);
  const vint = ()=>{ let v = 0, c; do { c = b[p++]; v = (v << 7) | (c & 0x7f); } while(c & 0x80); return v; };
  const tag = ()=> String.fromCharCode(b[p++], b[p++], b[p++], b[p++]);
  if(tag() !== "MThd") throw new Error("not a MIDI file");
  const hlen = u32(); u16(); const ntr = u16(); const div = u16();
  p += Math.max(0, hlen - 6);
  const raw = [], tempos = [{t:0, us:500000}];
  let rpn = {};
  for(let ti = 0; ti < ntr; ti++){
    if(p + 8 > b.length || tag() !== "MTrk") break;
    const len = u32(), end = p + len;
    let tick = 0, st = 0;
    while(p < end){
      tick += vint();
      let c = b[p];
      if(c & 0x80){ st = c; p++; } else { c = st; }
      const hi = st & 0xf0, ch = st & 0x0f;
      if(st === 0xff){
        const type = u8(), l = vint();
        if(type === 0x51 && l === 3) tempos.push({t:tick, us:(b[p]<<16)|(b[p+1]<<8)|b[p+2]});
        p += l; continue;
      }
      if(st === 0xf0 || st === 0xf7){ p += vint(); continue; }
      if(hi === 0x90 || hi === 0x80){
        const n = u8(), v = u8();
        raw.push({tick, k:(hi === 0x90 && v > 0) ? "on" : "off", ch, n, v});
      } else if(hi === 0xe0){
        const lsb = u8(), msb = u8();
        raw.push({tick, k:"bend", ch, val:((msb << 7) | lsb) - 8192});
      } else if(hi === 0xb0){
        const cc = u8(), val = u8();
        if(cc === 101) rpn.msb = val;
        else if(cc === 100) rpn.lsb = val;
        else if(cc === 6 && rpn.msb === 0 && rpn.lsb === 0) raw.push({tick, k:"range", ch, val});
      } else if(hi === 0xc0 || hi === 0xd0){ p++; }
      else { p += 2; }
    }
    p = end;
  }
  tempos.sort((x, y)=> x.t - y.t);
  const tickMs = (tick)=>{
    let ms = 0, last = 0, us = 500000;
    for(const t of tempos){
      if(t.t > tick) break;
      ms += (t.t - last) * us / div / 1000; last = t.t; us = t.us;
    }
    return ms + (tick - last) * us / div / 1000;
  };
  raw.sort((x, y)=> x.tick - y.tick);
  const evs = raw.map(e=> Object.assign({}, e, {ms: tickMs(e.tick)}));
  const endMs = evs.length ? evs[evs.length - 1].ms + 400 : 0;
  return {evs, endMs, notes: evs.filter(e=> e.k === "on").length};
}
const midiLit = new Map();      /* key element -> how many notes are holding it lit */
function midiLightAll(off){
  midiLit.forEach((_, el)=> el.classList.remove("mplay"));
  midiLit.clear();
}
function relightHeld(){
  if(typeof midi === "undefined" || !midi.playing) return;
  midi.held.forEach(h=>{
    const p = h.n * 100 + h.cents;
    document.querySelectorAll(".mkey").forEach(el=>{
      const kp = (+el.dataset.note) * 100 + (+el.dataset.cents);
      if(Math.abs(kp - p) <= 15) el.classList.add("mplay");
    });
  });
}
function midiLight(note, cents, on){
  const pitch = note * 100 + cents;
  document.querySelectorAll(".mkey").forEach(el=>{
    const p = (+el.dataset.note) * 100 + (+el.dataset.cents);
    if(Math.abs(p - pitch) > 15) return;           /* nearest lattice key wins */
    const n = (midiLit.get(el) || 0) + (on ? 1 : -1);
    if(n <= 0){ midiLit.delete(el); el.classList.remove("mplay"); }
    else { midiLit.set(el, n); el.classList.add("mplay"); }
  });
}
function midiRange(ch){ return midi.range[ch] === undefined ? 2 : midi.range[ch]; }
function midiCents(ch){ return Math.round((midi.bend[ch] || 0) / 8192 * midiRange(ch) * 100); }
/* two part routing for files: everything on A, or split by channel, or by register */
const MIDI_PART_SPLIT = 55;   /* G3, the default split */
function midiPartOf(e){
  if(midi.partMode === "chan") return ((e.ch | 0) >= 2) ? 1 : 0;
  if(midi.partMode === "key")  return ((e.n | 0) < (midi.partSplit | 0)) ? 1 : 0;
  return 0;
}
function midiPartLabel(){
  /* short, so the button stays the size of its neighbours */
  if(midi.partMode === "chan") return "BY CH";
  if(midi.partMode === "key")  return "BY KEY";
  return "ALL A";
}
function midiPartTitle(){
  if(midi.partMode === "chan") return "files route by channel: A on 0-1, B on 2-3";
  if(midi.partMode === "key")  return "files route by register: A above " +
    NOTE[(midi.partSplit|0) % 12] + (Math.floor((midi.partSplit|0)/12)-1) + ", B below";
  return "every file note plays part A";
}
function midiPartCycle(){
  const modes = ["a", "chan", "key"];
  const i = Math.max(0, modes.indexOf(midi.partMode || "a"));
  midi.partMode = modes[(i + 1) % modes.length];
  const b = document.getElementById("midipart");
  if(b){ b.textContent = midiPartLabel(); b.classList.toggle("on", midi.partMode !== "a"); }
  saveState();
  return midi.partMode;
}

function midiStop(){
  midi.playing = false;
  if(midi.timer){ clearInterval(midi.timer); midi.timer = null; }
  midi.held.forEach((h, k)=>{ send("O " + h.n + (h.cents ? " " + h.cents : "")); });
  midi.held.clear();
  /* if the page stalled mid file, per note offs never went out: panic is the only
     thing that guarantees silence */
  send("R");
  paintMidi();
  paintGplay();
}

function midiTick(){
  midi.lastTick = performance.now();
  if(!midi.playing) return;
  const now = performance.now() - midi.t0;
  while(midi.i < midi.evs.length && midi.evs[midi.i].ms <= now + 12){
    const e = midi.evs[midi.i++];
    if(e.k === "range"){ midi.range[e.ch] = e.val; continue; }
    if(e.k === "bend"){
      midi.bend[e.ch] = e.val;
      const c = midiCents(e.ch);
      midi.held.forEach(h=>{
        if(h.ch === e.ch && h.cents !== c){ h.cents = c; send("T " + (h.part || 0) + " " + h.n + " " + c); }
      });
      continue;
    }
    const key = e.ch + ":" + e.n;
    if(e.k === "on"){
      const c = midiCents(e.ch);
      const p = midiPartOf(e);
      midi.held.set(key, {n:e.n, ch:e.ch, cents:c, part:p});
      send("N " + e.n + " " + e.v + " " + p + " " + c);
      midiLight(e.n, c, true);
    } else {
      const h = midi.held.get(key);
      midi.held.delete(key);
      send("O " + e.n + (h ? " " + h.cents : ""));
      midiLight(e.n, h ? h.cents : 0, false);
    }
  }
  if(now > midi.endMs){
    if(!midi.loop){ midiStop(); return; }
    /* loop: drop whatever is still sounding, then start the file again on the
       same tick clock so the tempo does not drift */
    midi.held.forEach((h, k)=>{ send("O " + h.n + (h.cents ? " " + h.cents : "")); midiLight(h.n, h.cents, false); });
    midi.held.clear();
    midi.i = 0; midi.t0 = performance.now() - 1; midi.bend = {}; midi.range = {};
    paintMidi();
  }
}
function midiLoadFile(f, then){
  midiStop();
  return fetch("/midi/f?f=" + encodeURIComponent(f)).then(r=>r.arrayBuffer()).then(buf=>{
    const m = parseSmf(buf);
    midi.evs = m.evs; midi.endMs = m.endMs; midi.notes = m.notes; midi.name = f;
    midi.held.clear();
    paintMidi();
    if(then) then();
  }).catch(()=>{});
}
function midiPlay(){
  if(!midi.evs.length) return;
  midiStop();
  midi.playing = true; midi.t0 = performance.now(); midi.i = 0;
  midi.bend = {}; midi.range = {}; midi.held.clear();
  midi.timer = setInterval(midiTick, 16);
  paintMidi();
}
setInterval(()=>{
  /* the file player can be starved by a background window: if it has not ticked in
     two seconds, stop it and silence the Pod rather than leave notes hanging */
  if(midi.playing && midi.lastTick && performance.now() - midi.lastTick > 2000){
    midiStop();
    paintGplay();
  }
}, 1000);

function paintMidi(){
  const b = document.getElementById("midiplay");
  if(!b) return;
  b.textContent = midi.playing ? "■" : "▶";
  b.classList.toggle("on", midi.playing);
  const lp = document.getElementById("midiloop");
  if(lp) lp.classList.toggle("on", !!midi.loop);
  const pm = document.getElementById("midipart");
  if(pm){ pm.textContent = midiPartLabel(); pm.title = midiPartTitle();
          pm.classList.toggle("on", midi.partMode !== "a"); }
  const sl = document.getElementById("splitlab");
  if(sl){
    const n = micro.split | 0;
    sl.textContent = "B BELOW " + NOTE[n % 12] + (Math.floor(n / 12) - 1);
    sl.style.opacity = micro.splitOn ? 1 : 0.4;
  }
  const st = document.getElementById("midistat");
  if(st) st.textContent = midi.name
    ? (midi.notes + " notes, " + (midi.endMs / 1000).toFixed(1) + " s" + (midi.playing ? ", playing" : ""))
    : "no file loaded";
}
/* the honeycomb, shared by every view that shows the board */
let hexKeys = [];
function hexGrid(container){
  const v = container;
  const grid = document.createElement("div");
  grid.id = "mrow"; grid.classList.add("hex");
  container.appendChild(grid);
  const fill = ()=>{
    /* measure on the next frame: the column width changes when the knob panel
       folds away, and reading it in the same tick measures the old layout */
    const availW = v.clientWidth || window.innerWidth;
    const top = grid.getBoundingClientRect().top;
    /* the octave strip lives in the header now, so the floor is the view itself */
    const stripEl = document.getElementById("drumstrip");
    const bottom = (v.getBoundingClientRect().bottom || window.innerHeight) - (stripEl ? 92 : 0);
    const availH = Math.max(220, bottom - top - 6);
    const RUNS = 11;
    const wantCols = micro.size === "small" ? 28.5 : 16.5;
    const wFitH = availH / (0.75 * (RUNS - 1) + 1) * (Math.sqrt(3) / 2);
    const wFitW = availW / wantCols;
    const w = Math.max(44, Math.floor(Math.min(wFitH, wFitW)));
    const h = Math.round(w * 2 / Math.sqrt(3));       /* pointy-top hexagon */
    const vpitch = h * 0.75;                          /* rows interlock by a quarter */
    const ROWS = Math.max(6, Math.floor((availH - h) / vpitch) + 1);
    const COLS = Math.max(8, Math.floor((availW - w / 2) / w));
    const gridH = Math.round((ROWS - 1) * vpitch + h);
    /* the keys are absolute, so the grid has no intrinsic width: give it one or the
       board collapses to a single clipped column */
    grid.style.height = gridH + "px";
    grid.style.width = Math.round(COLS * w + w / 2) + "px";
    grid.style.position = "relative";
    grid.style.marginTop = "0px";
    grid.__keys = [];
    for(let r = 0; r < ROWS; r++){
      for(let c = 0; c < COLS; c++){
        const DRUMS = [36, 38, 40, 42, 44];
        const DRUM_NAMES = ["KICK", "SNR", "PERC", "CHH", "OHH"];
        const rUp = ROWS - 1 - r;
        let zone = 0, fNote = null, fLab = null, fCents = 0;
        if(micro.splitMode === "abcd" && rUp === 0){
          if(c < DRUMS.length){
            zone = 2; fNote = DRUMS[c]; fLab = DRUM_NAMES[c];
          } else {
            /* the rest of the bottom row is the bass, plain chromatic semitones */
            /* the Wolf's bass sits six semitones above the note it is sent, measured
               against the board, so compensate and keep the label honest */
            const want = 40 + (c - DRUMS.length);   /* the Wolf will not speak below E2 */
            zone = 3;
            /* the Wolf's bass tracks pitch badly: measured, note 28 sounds as 34 and 34
               sounds as 42, so it is not an offset but a scale of four thirds. Invert
               that so the key's label is the pitch you actually hear. */
            /* back on the Wolf, as asked. Its bass stretches pitch, so the row is a
               near chromatic approximation rather than exact; the offset below was
               measured against the board. */
            /* the root sits where it sounds right; the Wolf stretches everything above it
               by four thirds, so the span is expanded and the remainder goes out as
               pitch bend cents. Root unchanged. */
            const rootSent = 32;
            const exact = rootSent + (typeof micro.bassSpan === "number" ? micro.bassSpan : 1.2) * (want - 40);
            fNote = Math.max(12, Math.min(120, Math.round(exact)));
            fCents = Math.round((exact - Math.round(exact)) * 100);
            fLab = NOTE[(want % 12 + 12) % 12];
          }
        }
        const k = microKey(c, r, COLS, ROWS, fNote, fLab, (zone ? fCents : null));
        k.dataset.zone = zone;
        k.style.width = w + "px";
        k.style.height = h + "px";
        k.style.left = Math.round(c * w + (r % 2 ? w / 2 : 0)) + "px";
        k.style.top = Math.round(r * vpitch) + "px";
        const zn = +k.dataset.zone || 0;
        if(zn === 2 || zn === 3 || (micro.splitOn && (+k.dataset.note < (micro.split|0)))){
          const rim = zn === 2 ? "#ff8a65" : (zn === 3 ? "#b388ff" : "#ffb74d");
          const hu = zn === 2 ? 14 : (zn === 3 ? 265 : (+k.dataset.h || 200));
          k.dataset.hue = k.style.background;
          k.style.background = "linear-gradient(160deg, hsl(" + hu + ",52%,38%), hsl(" + hu + ",58%,19%))";
          k.style.boxShadow = "inset 0 2px 5px rgba(255,255,255,.22), inset 0 -6px 12px rgba(0,0,0,.55), 0 0 0 2px " + rim;
          k.style.color = "#ffe6bf";
          if(zn === 2) k.style.fontSize = "12px";
        } else {
          if(k.dataset.hue){ k.style.background = k.dataset.hue; k.dataset.hue = ""; }
          k.style.boxShadow = "";
          k.style.color = "#111";
        }
        k.style.fontSize = Math.max(11, Math.round(w * 0.19)) + "px";
        k.style.lineHeight = "1.05";
        grid.__keys.push({el: k, pitch: (+k.dataset.note) * 100 + (+k.dataset.cents)});
        grid.appendChild(k);
      }
    }
    hexKeys = grid.__keys;
    relightHeld();
  };
  /* drag mode: one pointer, every key it crosses sounds and the last one stops */
  let dPtr = null, dEl = null, dNote = -1, dOff = 0;
  const releaseDrag = ()=>{
    if(dEl){ send("O " + dNote + (dOff ? " " + dOff : "")); dEl.classList.remove("up"); }
    dEl = null; dNote = -1; dOff = 0;
  };
  const hitDrag = (ev)=>{
    const el = document.elementFromPoint(ev.clientX, ev.clientY);
    const k = el && el.closest ? el.closest(".mkey") : null;
    if(!k || k === dEl) return;
    releaseDrag();
    const n = +k.dataset.note, off = +k.dataset.cents || 0;
    const p = micro.splitOn ? ((n < (micro.split | 0)) ? 1 : 0) : part;
    send("N " + n + " 96 " + p + " " + off);
    k.classList.add("up");
    dEl = k; dNote = n; dOff = off;
  };
  grid.addEventListener("pointerdown", ev=>{
    if(!micro.dragOn) return;
    ev.preventDefault(); ev.stopPropagation();
    dPtr = ev.pointerId;
    try { grid.setPointerCapture(ev.pointerId); } catch(e) {}
    hitDrag(ev);
  }, true);
  grid.addEventListener("pointermove", ev=>{
    if(!micro.dragOn || ev.pointerId !== dPtr) return;
    ev.preventDefault();
    hitDrag(ev);
  }, true);
  const endDrag = ev=>{ if(!micro.dragOn) return; if(dPtr !== null && ev.pointerId !== dPtr) return; dPtr = null; releaseDrag(); };
  grid.addEventListener("pointerup", endDrag, true);
  grid.addEventListener("pointercancel", endDrag, true);

  requestAnimationFrame(()=>{ try { fill(); } catch(e){ console.error("hexGrid", e); } });
  return grid;
}

function renderView(){
  if(fingers.size) releaseAll();
  if(typeof heldNote !== "undefined" && heldNote >= 0) releaseHeldNote();
  /* a file keeps playing across tabs on purpose, so the algorithm and operator
     pages can be tweaked while it runs */
  if(view==="seq") buildSeq();
  else if(view==="drone") buildDrone();
  else if(view==="wt") buildWt();
  else if(view==="roll") buildRoll();
  else if(view==="arp") buildArp();
  else if(view==="euclid") buildEuclid();
  else if(view==="xy") buildXY();
  else if(view==="alg") buildAlg();
  else if(view==="ops") buildOps();
  else if(view==="rand") buildRand();
  else if(view==="clips") buildClips();
  else if(view==="grid") buildGrid();
  else if(view==="chroma") buildChroma();
  else if(view==="micro") buildMicro();
  else buildKalimba();
}

function showFingers(){
  let nn = 0;
  fingers.forEach(f=>{ nn += f.notes.length; });
  document.getElementById("fingers").textContent = fingers.size + " fingers" + (chord ? " · " + nn + " notes" : "");
}

/* a bend leaves a comet trail: colour runs cyan at full flat to red at full sharp */
let lastTrail = 0;
function bendTrail(x, y, cents, range){
  if(x === undefined || y === undefined) return;
  const now = performance.now();
  if(now - lastTrail < 28) return;
  lastTrail = now;
  const t = Math.max(-1, Math.min(1, cents / Math.max(1, range)));
  const d = document.createElement("div");
  d.className = "btrail";
  d.style.left = x + "px"; d.style.top = y + "px";
  d.style.setProperty("--h", (190 - 190 * (t + 1) / 2).toFixed(0));
  document.body.appendChild(d);
  d.addEventListener("animationend", () => d.remove());
}

/* one document listener covers every player: kalimba tines, grid pads, chroma,
   micro hexagons and the drone stones */
function sparkBurst(x, y, hue){
  const ring = document.createElement("div");
  ring.className = "spark";
  ring.style.left = x + "px"; ring.style.top = y + "px";
  ring.style.setProperty("--h", hue);
  document.body.appendChild(ring);
  ring.addEventListener("animationend", ()=>ring.remove());
  const core = document.createElement("div");
  core.className = "score";
  core.style.left = x + "px"; core.style.top = y + "px";
  core.style.setProperty("--h", hue);
  document.body.appendChild(core);
  core.addEventListener("animationend", ()=>core.remove());
  for(let i = 0; i < 9; i++){
    const d = document.createElement("div");
    d.className = "sdot";
    const a = (Math.PI * 2 * i) / 9 + Math.random() * 0.4;
    const r = 46 + Math.random() * 38;
    d.style.left = x + "px"; d.style.top = y + "px";
    d.style.setProperty("--h", hue);
    d.style.setProperty("--dx", Math.cos(a) * r + "px");
    d.style.setProperty("--dy", Math.sin(a) * r + "px");
    document.body.appendChild(d);
    d.addEventListener("animationend", ()=>d.remove());
  }
  setTimeout(()=>{ ring.remove(); }, 700);
}
document.addEventListener("pointerdown", ev=>{
  const t = ev.target;
  if(!t || !t.closest) return;
  const key = t.closest(".mkey,.pad,.tine,.dstone,.dvh");
  if(!key) return;
  const deg = parseInt(key.dataset.note, 10);
  sparkBurst(ev.clientX, ev.clientY, isNaN(deg) ? 50 : (deg * 37) % 360);
}, true);

/* light every key in the chords currently held, so the generated notes are visible */
function paintChordLights(){
  const set = new Set();
  fingers.forEach(f=>{ if(f.chording) f.notes.forEach(n=>set.add(n)); });
  document.querySelectorAll("[data-note]").forEach(el=>{
    el.classList.toggle("chordlit", set.has(+el.dataset.note));
  });
}
function releasePointer(ev){
  const f = fingers.get(ev.pointerId);
  if(!f) return;
  if(f.lockEl){ f.lockEl.classList.remove("lockon"); f.lockEl = null; }
  fingers.delete(ev.pointerId);
  f.el.classList.remove("up");
  if(!sust) f.notes.forEach((n,i)=>send("O "+n+(f.micro ? " "+f.micro[i] : "")));
  if(fingers.size === 0) exprStop();
  paintChordLights();
  showFingers();
}
function releaseAll(){
  fingers.forEach(f=>{ if(f.el) f.el.classList.remove("up"); if(!sust) f.notes.forEach((n,i)=>send("O "+n+(f.micro ? " "+f.micro[i] : ""))); });
  fingers.clear(); paintChordLights(); showFingers(); exprStop();
}
document.addEventListener("pointerup", releasePointer);
document.addEventListener("pointercancel", releasePointer);
/* capture can be lost without a pointerup (view rebuilt under the finger,
   browser gesture, device quirk): none of these may leave a note hanging */
document.addEventListener("lostpointercapture", releasePointer);
document.addEventListener("touchcancel", ()=>releaseAll(), true);
document.addEventListener("touchend", (ev)=>{
  if(!ev.touches || ev.touches.length === 0) releaseAll();
}, true);
document.addEventListener("blur", ()=>releaseAll());
document.addEventListener("contextmenu", ev=>ev.preventDefault());

function label(){
  document.getElementById("keyb").textContent = "KEY "+NOTE[keySel];
  document.getElementById("typeb").textContent = "TYPE "+TYPEN[typeSel];
  document.getElementById("chordb").textContent = chord ? ("CHORD on · " + chordN + " notes" + (chordSpace > 1 ? " · sp " + chordSpace : "")) : "CHORD off";
  document.getElementById("chordb").className = chord?"on":"";
  document.getElementById("sustb").textContent = "SUSTain "+(sust?"on":"off");
  document.getElementById("sustb").className = sust?"on":"";
}
function readout(){
  let ms = Math.round(60000/bpm*DIVB[divSel]);
  ms = Math.max(50, Math.min(2000, ms));
  document.getElementById("readout").textContent =
     DIVN[divSel]+" at "+bpm+" BPM  =  "+ms+" ms";
}
function divs(){
  const box=document.getElementById("divs");
  box.innerHTML="";
  DIVN.forEach((n,i)=>{
    const b=document.createElement("button");
    b.textContent=n; if(i===divSel) b.className="on";
    b.onclick=()=>{ divSel=i; send("P 13 "+i); divs(); readout(); };
    box.appendChild(b);
  });
}

document.querySelectorAll("#views button").forEach(b=>{
  b.onclick=()=>{
    /* deliberately no stopAll(): switching tabs must not silence a running
       sequencer. The transport keeps ticking and its fire callback only reads
       globals, so the old view's pattern keeps playing headless. */
    view = b.dataset.v;
    document.querySelectorAll("#views button").forEach(x=>x.classList.toggle("on", x===b));
    renderView();
  };
  b.classList.toggle("on", b.dataset.v === view);
});

const fxTog = document.getElementById("fxtog");
function paintFx(){
  document.body.classList.toggle("fxoff", fxHidden);
  if(fxTog){
    fxTog.textContent = fxHidden ? "SHOW KNOBS" : "HIDE KNOBS";
    fxTog.classList.toggle("on", !fxHidden);
  }
}
fxTog.onclick = ()=>{ fxHidden = !fxHidden; paintFx(); renderView(); saveState(); };
paintFx();

const bendBtn = document.getElementById("bendb");
function paintBend(){ if(bendBtn) bendBtn.textContent = "BEND " + (bendMode ? "on" : "off"); }
bendBtn.onclick = ()=>{ bendMode = !bendMode; paintBend(); saveState(); };
paintBend();

/* the header A/B buttons became the slot chips; keep these guarded so an older
   or newer header can never abort the whole script again */
(function(){
  const a = document.getElementById("partA"), b = document.getElementById("partB");
  if(a) a.onclick = ()=> setPart(0);
  if(b) b.onclick = ()=> setPart(1);
})();

document.getElementById("gmode").onclick = ()=>{
  transport.global = !transport.global;
  if(transport.on) stopAll();   /* a mode change stops; press PLAY to restart */
  paintGlobal();
};
paintGlobal();

document.getElementById("octdn").onclick=()=>{ oct=Math.max(-4,oct-1); renderView(); };
document.getElementById("octup").onclick=()=>{ oct=Math.min(4,oct+1); renderView(); };
document.getElementById("keyb").onclick=()=>{ keySel=(keySel+1)%12; label(); renderView(); };
document.getElementById("typeb").onclick=()=>{ typeSel=(typeSel+1)%7; label(); renderView(); };
document.getElementById("chordb").onclick=()=>{ chord=!chord; label(); };
document.getElementById("sustb").onclick=()=>{ sust=!sust; label(); };
document.getElementById("panic").onclick=()=>{ send("R"); releaseHeldNote(); };

/* header transport: one button for every lane plus the midi player */
function paintGplay(){
  const k = document.getElementById("p12");
  if(k && +k.value !== bpm) bpm = +k.value;
  const b = document.getElementById("gplay");
  if(!b) return;
  const on = transport.on || (typeof midi !== "undefined" && midi.playing);
  b.textContent = on ? "■" : "▶";
  b.classList.toggle("on", on);
}
document.getElementById("gplay").onclick = ()=>{
  /* the header transport drives the midi file when one is loaded, the lanes otherwise */
  if(midi.playing){ midiStop(); paintPlay(); paintGplay(); return; }
  if(midi.evs && midi.evs.length){ midiPlay(); paintPlay(); paintGplay(); return; }
  if(transport.on){ stopAll(); } else { transport.global = true; startEngine(view); }
  paintPlay(); paintGplay();
};

/* file picker in the header, so a song is one click from the transport */
/* the wave set list has to be known before the WT tab is ever opened, otherwise the
   header dropdown only offers INIT and the saved presets */
function loadWtFiles(){
  return fetch("/wt").then(r=> r.json()).then(names=>{
    wtFiles = names.slice();
    if(engWT) renderWtPatchList();
  }).catch(()=>{});
}

function refreshMidiLists(){
  return fetch("/midi").then(r=> r.json()).then(names=>{
    ["hmidi", "midisel"].forEach(id=>{
      const sel = document.getElementById(id);
      if(!sel) return;
      const keep = sel.value;
      sel.innerHTML = "";
      if(!names.length){
        const o = document.createElement("option"); o.textContent = "(no .mid files)"; sel.appendChild(o);
      }
      names.forEach(n=>{
        const o = document.createElement("option"); o.value = n; o.textContent = n; sel.appendChild(o);
      });
      if(keep && names.indexOf(keep) >= 0) sel.value = keep;
      else if(midi.name && names.indexOf(midi.name) >= 0) sel.value = midi.name;
    });
  }).catch(()=>{});
}

(function(){
  const sel = document.getElementById("hmidi"), ld = document.getElementById("hload");
  if(!sel || !ld) return;
  refreshMidiLists();
  loadWtFiles();
  ld.onclick = ()=>{
    const f = sel.value;
    if(!f) return;
    midiLoadFile(f, ()=>{ midiPlay(); paintGplay(); });
  };
})();

function bpmApply(fromUi){
  const be = document.getElementById("bpm");
  if(!be) return;
  const v = Math.max(60, Math.min(200, parseInt(be.value, 10) || 120));
  be.value = v;
  bpm = v;
  const k = document.getElementById("p12");
  if(k){ k.value = v; k.dispatchEvent(new Event("input")); }
  else send("P 12 " + v);
  if(fromUi) send("P 12 " + v);
}
const bpmEl = document.getElementById("bpm");
if(bpmEl) bpmEl.addEventListener("change", ()=>bpmApply(true));


/* engine switch: the wavetable core lives in the same firmware, so this is a flag */
function paintEng(){
  const b = document.getElementById("engsel");
  if(!b) return;
  b.textContent = engWT ? "WT" : "FM";
  b.classList.toggle("on", engWT);
  FXK.forEach(k=>{
    if(k.id.indexOf("p3") !== 0) return;
    const cell = document.querySelector(".kcell [id='" + k.id + "']");
    if(cell && cell.parentNode) cell.parentNode.style.opacity = engWT ? "1" : "0.35";
  });
}
document.getElementById("engsel").onclick = ()=>{
  engWT = !engWT;
  send("P 28 " + (engWT ? 1 : 0));
  /* the wavetable core costs ~11 us a voice, so it gets the full twelve */
  const vk = document.getElementById("p5");
  if(vk){ vk.value = engWT ? 12 : 8; vk.dispatchEvent(new Event("input")); }
  if(engWT){
    ["p30","p31","p32","p33"].forEach(id=>{
      const el = document.getElementById(id);
      if(el) send("P " + id.slice(1) + " " + el.value);
    });
  }
  paintEng();
  if(engWT) renderWtPatchList(); else renderPatchList();
  paintSlots();
  syncDropdownToPart();
};

/* master volume: P 26 on the Pod, 0..1000, 1000 = unity, smoothed per sample */
const volEl = document.getElementById("vol");
const volV  = document.getElementById("volv");
function volApply(send_it){
  const v = Math.max(0, Math.min(100, +volEl.value | 0));
  volV.textContent = v;
  if(send_it) send("P 26 " + (v * 10));
}
volEl.addEventListener("input", ()=>volApply(true));
volEl.addEventListener("change", ()=>volApply(true));

/* page-side knobs: they send nothing, they only change what the UI sends */
function localKnob(id, get, set){
  const el = document.getElementById(id);
  if(!el) return;
  el.value = get();
  el.addEventListener("input", ()=>{
    set(+el.value);
    if(knobPaint[id]) knobPaint[id]();
    if(typeof label === "function") label();
    saveState();
  });
  if(knobPaint[id]) knobPaint[id]();
}
localKnob("pB", ()=>bendRange, v=>{ bendRange = v; });
localKnob("pC", ()=>chordN,    v=>{ chordN    = v; });
localKnob("pS", ()=>chordSpace, v=>{ chordSpace = v; });

[0,1,2,5,6,7,9,10,11,12].forEach(i=>{
  const el=document.getElementById("p"+i);
  if(!el) return;
  let last=0;
  el.addEventListener("input", ()=>{
    document.getElementById("v"+i).textContent = el.value;
    const now=Date.now();
    if(now-last>40){ last=now; send("P "+i+" "+el.value); }
    if(i===12){ bpm=+el.value; readout(); }
  });
  el.addEventListener("change", ()=>send("P "+i+" "+el.value));
});

let patchIdx = 0, patchAlg = 0, patchFb = 0;
const patches = [];
const patchSel = document.getElementById("patchsel");
const algSel = document.getElementById("algsel");
for(let i=1;i<=32;i++){
  const o=document.createElement("option");
  o.value=i-1; o.textContent="ALG "+i;
  algSel.appendChild(o);
}
algSel.value = 0;
algSel.onchange = ()=> send("P 3 " + algSel.value);

var slotName = ["init", "init"];
var wtFiles = [];
function paintSlots(){
  [0, 1].forEach(p=>{
    const b = document.getElementById(p ? "slotB" : "slotA");
    if(!b) return;
    b.textContent = (p ? "B: " : "A: ") + (slotName[p] || "init");
    b.classList.toggle("on", p === wtPart);
  });
}
document.getElementById("partC").onclick = ()=> setPart(2);
document.getElementById("partD").onclick = ()=> setPart(3);
document.querySelectorAll("#abslots .slot").forEach(b=>{
  b.onclick = ()=>{
    wtPart = +b.dataset.p;
    send("P 17 " + wtPart);
    paintSlots();
    syncDropdownToPart();
    if(view === "wt") renderView();
  };
});

/* the header has to show what is loaded in the slot you just selected */
function syncDropdownToPart(){
  const name = slotName[wtPart] || "";
  if(engWT){
    const i = (wtListItems || []).indexOf(name);
    if(i >= 0) patchSel.value = i;
  } else {
    const i = patches.findIndex(n=> String(n).trim() === String(name).trim());
    if(i >= 0) patchSel.value = i;
  }
}

/* in WT mode the header dropdown lists wavetable patches, not DX7 banks */
let wtListItems = [], wtListFiles = [];
function renderWtPatchList(){
  const files = wtFiles.map(f=> f.replace(/\.wt$/i, ""));
  const items = ["INIT"].concat(files).concat(wtUser.map(u=>u.n));
  wtListItems = items; wtListFiles = wtFiles.slice();
  patchSel.innerHTML = "";
  items.forEach((t, i)=>{
    const o = document.createElement("option");
    o.value = i; o.textContent = "WT " + t;
    patchSel.appendChild(o);
  });
  const idx = items.indexOf(slotName[wtPart]);
  patchSel.value = idx >= 0 ? idx : 0;
}

/* picking a wavetable patch: a wave set loads its samples, a saved preset or INIT
   just writes the parameters. It never changes the engine on its own. */
async function wtPick(i){
  const nFiles = wtListFiles.length;
  if(i > 0 && i <= nFiles){
    try{ await wtLoadFile(wtListFiles[i-1]); }catch(e){}
  } else {
    const src = i === 0 ? JSON.parse(JSON.stringify(WT_DEF))
                        : JSON.parse(JSON.stringify((wtUser[i-1-nFiles] || {p:WT_DEF}).p));
    wtApply(src);
  }
  slotName[wtPart] = wtListItems[i];
  paintSlots();
  saveState();
}

function renderPatchList(){
  if(engWT) return renderWtPatchList();   /* the header tracks the engine */
  const want = patches.map((nm,i)=>(i+1) + "   " + (nm || "-"));
  const have = [...patchSel.options].map(o=>o.textContent);
  const same = want.length === have.length && want.every((t,i)=>t === have[i]);
  if(!same){
    patchSel.innerHTML = "";
    want.forEach((t,i)=>{
      const o = document.createElement("option");
      o.value = i;
      o.textContent = t;
      patchSel.appendChild(o);
    });
  }
  if(patches.length) patchSel.value = patchIdx;
}
function renderPatch(){
  document.getElementById("algfb").textContent = "fb " + patchFb;
  if(patches.length) patchSel.value = patchIdx;
  algSel.value = patchAlg;
}
function selectPatch(i){
  patchIdx = i;
  send("P 4 " + i);
  slotName[wtPart] = (patches[i] || "patch") + "";
  paintSlots();
  renderPatch();
}
patchSel.onchange = ()=>{
  const i = +patchSel.value;
  if(engWT) wtPick(i); else selectPatch(i);
};
document.getElementById("pprev").onclick=()=>{
  const n = Math.max(1, patches.length);
  selectPatch((patchIdx + n - 1) % n);
};
document.getElementById("pnext").onclick=()=>{
  const n = Math.max(1, patches.length);
  selectPatch((patchIdx + 1) % n);
};
/* the bank list is asked for once things are awake, and again if the link drops */
setTimeout(()=>send("L"), 900);

const es=new EventSource("/events");
es.onopen=()=>send("L");

function refreshBanks(){
  fetch("/banks").then(r=>r.json()).then(list=>{
    const s=document.getElementById("banksel");
    const have=[...s.options].map(o=>o.value);
    if(have.length===list.length && have.every((v,i)=>v===list[i])) return;
    const keep=s.value;
    s.innerHTML="";
    if(!list.length){
      const o=document.createElement("option");
      o.textContent="(no .syx in ~/banks)";
      s.appendChild(o);
      return;
    }
    list.forEach(f=>{
      const o=document.createElement("option");
      o.value=f; o.textContent=f; s.appendChild(o);
    });
    if(list.includes(keep)) s.value=keep;
  }).catch(()=>{});
}
async function bankNames(name){
  try{
    const buf = await (await fetch("/bankfile?f=" + encodeURIComponent(name))).arrayBuffer();
    const b = new Uint8Array(buf);
    let i = 0;
    for(let k = 0; k + 1 < b.length; k++){ if(b[k] === 0xF0 && b[k+1] === 0x43){ i = k; break; } }
    if(b[i+3] !== 0x09 || i + 6 + 4096 > b.length) return false;
    const base = i + 6;
    for(let slot = 0; slot < 32; slot++){
      let nm = "";
      for(let c = 0; c < 10; c++){
        const ch = b[base + slot*128 + 118 + c];
        nm += (ch >= 32 && ch < 127) ? String.fromCharCode(ch) : "";
      }
      patches[slot] = nm.trim() || ("slot " + slot);
    }
    patches.length = 32;
    renderPatchList();
    return true;
  }catch(err){ return false; }
}

document.getElementById("bankload").onclick=async ()=>{
  const s=document.getElementById("banksel");
  if(!s.value) return;
  const msg=document.getElementById("bankmsg");
  msg.textContent="loading...";
  try{
    /* read the names straight out of the file: the Pod does not always answer L,
       and the page should not depend on that to label the slots */
    await bankNames(s.value);
    const t = await (await fetch("/banks/load?f="+encodeURIComponent(s.value), {method:"POST"})).text();
    msg.textContent = /pushed|verified|loaded/i.test(t) ? "32 voices" : t;
    msg.title = t;
  } catch(err){ msg.textContent = "failed"; }
};
/* A flash or a power cycle leaves the Pod with its built-in voices: the bank lives in
   RAM. Whenever the link comes back, push the bank and the patch the panel had. */
let lastRx = 0, linkWasDown = false, restoring = false;
async function restoreBankPatch(){
  if(restoring) return;
  restoring = true;
  try{
    const be = document.getElementById("banksel");
    const b = (typeof savedBank === "string" && savedBank) || (be ? be.value : "");
    if(!be || !b) { restoring = false; return; }
    for(let i = 0; i < 25; i++){
      if([].some.call(be.options, o=> o.value === b)) break;
      await new Promise(r=> setTimeout(r, 200));
    }
    be.value = b;
    document.getElementById("bankload").click();
    await new Promise(r=> setTimeout(r, 1400));
    send("P 4 " + (patchIdx | 0));
    send("P 17 " + wtPart);
    if(engWT) wtLfoSend("all");
  }catch(e){}
  restoring = false;
}
setInterval(()=>{
  if(!lastRx) return;
  const down = Date.now() - lastRx > 4000;
  if(down) linkWasDown = true;
  else if(linkWasDown && !down){ linkWasDown = false; restoreBankPatch(); }
}, 1000);

refreshBanks();
setInterval(refreshBanks, 8000);
document.getElementById("banksel").addEventListener("pointerdown", refreshBanks);
es.onmessage=ev=>{
  const t=ev.data;
  lastRx = Date.now();
  if(t.startsWith("@C ")){
    const p=t.slice(3, t.indexOf("*")).split(" ");
    lastVoices = p[0]|0;
    document.getElementById("voices").textContent=p[0]+" voices";
    document.getElementById("cpu").textContent=p[1]+" us";
  } else if(t.startsWith("@G ")){
    const p=t.slice(3, t.indexOf("*")).split(" ");
    patchAlg  = p[0]|0;
    patchFb   = p[1]|0;
    renderPatch();
  } else if(t.startsWith("@L ")){
    const p=t.slice(3, t.indexOf("*")).split(" ");
    const i=+p[0];
    const nm=p.slice(1).join(" ");
    if(i>=0 && i<64) patches[i]=nm;
    renderPatchList();
  } else if(t.startsWith("@D ")){
    const p=t.slice(3, t.indexOf("*")).split(" ");
    const op=+p[0], hexs=p[1]||"";
    if(op>=0 && op<6 && hexs.length>=42){
      for(let i=0;i<21;i++) opVals[op][i]=parseInt(hexs.substr(i*2,2),16);
      if(view==="ops") opsRender();
    }
  }
};

/* ---------------------------------------------------------- persistence --- */
const FX_IDS = [0,1,2,5,6,7,9,10,11,12];

function snapshot(){
  const fx = {};
  FX_IDS.forEach(i=>{ const el = document.getElementById("p"+i); if(el) fx[i] = +el.value; });
  const num = (id)=>{ const el = document.getElementById(id); return el ? +el.value : 0; };
  return {
    v:1, view, keySel, typeSel, oct, chord, sust, bpm, divSel, part, patch: patchIdx, bend: bendMode, bendRange: bendRange, vol: +volEl.value, bank: document.getElementById("banksel").value, eng: engWT, wtv: WT2, wtp: wtUser, wtf: wtFile, wtpart: wtPart, slot: slotName.slice(), midi: midi.name, partSplit: midi.partSplit, chordN: chordN, chordSpace: chordSpace, fxHidden: fxHidden, micro: {edo: micro.edo, layout: micro.layout, root: micro.root, size: micro.size, splitOn: micro.splitOn, split: micro.split, dragOn: micro.dragOn, splitMode: micro.splitMode, bassOfs: micro.bassOfs, bassSpan: micro.bassSpan},
    gmode: transport.global,
    range: {lo0:num("lo0"), hi0:num("hi0"), lo1:num("lo1"), hi1:num("hi1")},
    fx,
    seq: {len:seq.len, bar:seq.bar, swing:seq.swing,
          tracks: seq.tracks.map(t=>({step:t.step, vel:t.vel, mute:t.mute, part:t.part, hits:t.hits.slice()}))},
    banks: seqBanks,
    clips: clips.items,
    clipActive: clips.active,
    roll: {cells:roll.cells},
    arp: {root:arp.root, octaves:arp.octaves, order:arp.order, rate:arp.rate},
    eu: eu.lanes,
    drone: {div:drone.div, alg:drone.alg, wave:drone.wave, lfoRun:drone.lfoRun,
            core:{x:drone.core.x, y:drone.core.y}, fb:drone.fb,
            stones:drone.stones.map(s=>({deg:s.deg, lvl:s.lvl})),
            params: Object.fromEntries(DR.map(p=>[p.k, Math.round(p.base*100)/100])),
            lfos: LFOS.map(l=>({on:l.on, shape:l.shape, rate:Math.round(l.rate*1000)/1000,
                               depth:Math.round(l.depth*1000)/1000, targets:l.targets.slice()}))}
  };
}

let savedBank = "";
function applyState(s){
  if(!s || s.v !== 1) return;
  /* what was loaded last time comes back: slot labels, wave set, midi file */
  if(Array.isArray(s.slot)){ s.slot.forEach((n, i)=>{ if(i < 2) slotName[i] = n || "init"; }); }
  if(s.wtf) setTimeout(()=>{ wtLoadFile(s.wtf).catch(()=>{}); }, 700);
  if(typeof s.partSplit === "number") midi.partSplit = s.partSplit;
  if(typeof s.bank === "string" && s.bank) savedBank = s.bank;
  if(s.midi) setTimeout(()=>{ midiLoadFile(s.midi).catch(()=>{}); }, 900);
  setTimeout(()=>{ paintSlots(); if(engWT) renderWtPatchList(); fitHdr(); }, 1200);
  if(s.view) view = s.view;
  if(typeof s.keySel === "number") keySel = s.keySel;
  if(typeof s.typeSel === "number") typeSel = s.typeSel;
  if(typeof s.oct === "number") oct = s.oct;
  chord = !!s.chord;
  sust = !!s.sust;
  if(typeof s.bpm === "number" && s.bpm >= 60 && s.bpm <= 200) bpm = s.bpm;
  setTimeout(()=>bpmApply(true), 400);   /* the header field is the authority on boot */
  if(typeof s.divSel === "number") divSel = s.divSel;
  if(typeof s.part === "number") part = s.part ? 1 : 0;
  if(typeof s.patch === "number") patchIdx = s.patch;
  if(typeof s.bend === "boolean") bendMode = s.bend;
  if(typeof s.vol === "number"){ volEl.value = Math.max(0, Math.min(100, s.vol)); volApply(true); }
  if(Array.isArray(s.wtv)){
    for(let p = 0; p < 2; p++) if(s.wtv[p]) Object.keys(WT2[p]).forEach(k=>{
      const v = s.wtv[p][k];
      if(typeof v === "number") WT2[p][k] = v;
      else if(k === "lfo"){
        /* migration: the old build stored one LFO object, the new one wants three */
        if(Array.isArray(v))
          WT2[p].lfo = v.map(o=>{
            const amt = WT_ZERO14();
            if(o && o.amt && o.amt.length === 14) return Object.assign({}, o, {amt:o.amt.slice()});
            if(o && typeof o.depth === "number") amt[o.target | 0] = o.depth;
            return {on:!!(o && o.on), shape:(o && o.shape) | 0, div:(o && o.div) | 0, steps:(o && o.steps) | 0, amt:amt};
          });
      }
    });
  }
  if(typeof s.wtpart === "number") wtPart = s.wtpart ? 1 : 0;
  if(Array.isArray(s.wtp)) wtUser = s.wtp;
  if(typeof s.wtf === "string") wtFile = s.wtf;
  if(typeof s.eng === "boolean"){
    engWT = s.eng;
    send("P 28 " + (engWT ? 1 : 0));
    if(engWT) ["p30","p31","p32","p33"].forEach(id=>{
      const el = document.getElementById(id);
      if(el) send("P " + id.slice(1) + " " + el.value);
    });
    paintEng();
  }
  if(typeof s.bank === "string" && s.bank){
    const want = s.bank;
    /* the option list arrives with refreshBanks, so try again once it is there */
    const apply = ()=>{
      const b = document.getElementById("banksel");
      if(b && Array.from(b.options).some(o=>o.value===want)) b.value = want;
      bankNames(want);
    };
    setTimeout(apply, 500);
    setTimeout(apply, 1500);
  }
  if(typeof s.bendRange === "number") bendRange = Math.max(50, Math.min(2400, s.bendRange));
  if(typeof s.fxHidden === "boolean"){ fxHidden = s.fxHidden; if(typeof paintFx === "function") paintFx(); }
  if(typeof s.chordN === "number") chordN = Math.max(2, Math.min(6, s.chordN));
  if(typeof s.chordSpace === "number") chordSpace = Math.max(1, Math.min(4, s.chordSpace));
  if(s.micro){
    if(typeof s.micro.splitOn === "boolean") micro.splitOn = s.micro.splitOn;
    if(typeof s.micro.split === "number") micro.split = s.micro.split;
    if(typeof s.micro.dragOn === "boolean") micro.dragOn = s.micro.dragOn;
    if(typeof s.micro.splitMode === "string") micro.splitMode = s.micro.splitMode;
    if(typeof s.micro.bassOfs === "number") micro.bassOfs = s.micro.bassOfs;
    if(typeof s.micro.bassSpan === "number") micro.bassSpan = s.micro.bassSpan;
    if([12,19,24,31].indexOf(s.micro.edo) >= 0) micro.edo = s.micro.edo;
    if(s.micro.layout === "hex" || s.micro.layout === "bosa") micro.layout = s.micro.layout;
    if(s.micro.size === "small" || s.micro.size === "big") micro.size = s.micro.size;
    if(typeof s.micro.root === "number") micro.root = ((s.micro.root % 12) + 12) % 12;
  }
  transport.global = !!s.gmode;
  paintGlobal();
  if(s.range){
    Object.keys(s.range).forEach(k=>{
      const el = document.getElementById(k);
      if(el && typeof s.range[k] === "number") el.value = s.range[k];
    });
  }
  if(s.fx){
    Object.keys(s.fx).forEach(k=>{
      const el = document.getElementById("p"+k);
      if(!el) return;
      el.value = s.fx[k];
      const lab = document.getElementById("v"+k);
      if(lab) lab.textContent = s.fx[k];
    });
  }
  if(s.seq){
    seq.len = s.seq.len || 16;
    seq.bar = s.seq.bar || 0;
    if(typeof s.seq.swing === "number") seq.swing = s.seq.swing;
    (s.seq.tracks || []).forEach((t,i)=>{
      if(!seq.tracks[i]) return;
      seq.tracks[i].step = t.step;
      seq.tracks[i].vel = t.vel;
      seq.tracks[i].mute = t.mute;
      seq.tracks[i].part = t.part ? 1 : 0;
      seq.tracks[i].hits = t.hits.slice(0, 64);
    });
  }
  if(Array.isArray(s.banks)) s.banks.forEach((b,i)=>{ if(b) seqBanks[i] = b; });
  if(Array.isArray(s.clips)) s.clips.forEach((c,i)=>{ if(c && clips.items[i]) clips.items[i] = c; });
  if(typeof s.clipActive === "number") clips.active = s.clipActive;
  if(s.roll && Array.isArray(s.roll.cells)) roll.cells = s.roll.cells;
  if(s.arp) Object.assign(arp, s.arp);
  if(Array.isArray(s.eu)) eu.lanes.forEach((l,i)=>{ if(s.eu[i]) Object.assign(l, s.eu[i]); });
  if(s.drone){
    const d = s.drone;
    if(typeof d.div === "number") drone.div = d.div;
    if(typeof d.fb === "number") drone.fb = d.fb;
    if(d.core){ drone.core.x = d.core.x; drone.core.y = d.core.y; }
    if(typeof d.alg === "number") drone.alg = d.alg;
    if(typeof d.wave === "number") drone.wave = d.wave;
    if(Array.isArray(d.stones)) d.stones.forEach((t,i)=>{
      if(drone.stones[i] && t){ drone.stones[i].deg = t.deg; drone.stones[i].lvl = t.lvl; }
    });
    if(d.params) Object.keys(d.params).forEach(k=>{ const p = drGet(k); if(p && typeof d.params[k] === "number"){ p.base = d.params[k]; p.sent = -1; drPaint(p, p.base); } });
    if(typeof d.lfoRun === "boolean") drone.lfoRun = d.lfoRun;
    if(Array.isArray(d.lfos)) d.lfos.forEach((t,i)=>{
      const l = LFOS[i];
      if(!l || !t) return;
      if(typeof t.on === "boolean") l.on = t.on;
      if(typeof t.shape === "number") l.shape = t.shape;
      if(typeof t.rate === "number") l.rate = t.rate;
      if(typeof t.depth === "number") l.depth = t.depth;
      if(Array.isArray(t.targets)) l.targets = t.targets.filter(k=>!!drGet(k));
      else if(typeof t.target === "string" && drGet(t.target)) l.targets = [t.target];
      else l.targets = [];
    });
    droneSent.cut = droneSent.res = droneSent.fb = -1;
  }

  /* the Pod has its own copy of the sound settings, so push them back to it */
  setTimeout(()=>{
    const pa = document.getElementById("partA"), pb = document.getElementById("partB");
    if(pa) pa.classList.toggle("on", part === 0);
    if(pb) pb.classList.toggle("on", part === 1);
    Object.keys(s.fx || {}).forEach(k=>send("P " + k + " " + s.fx[k]));
    send("P 12 " + bpm);
    /* the Pod boots on its own patch; re-send the saved slot so the name in
       the header and the sound actually match after a reload */
    send("P 4 " + patchIdx);
    send("P 17 " + part);
    /* keep the drone field in step with the restored sliders, they are the truth */
    const sv = (id)=>{ const el = document.getElementById(id); return el ? +el.value : NaN; };
    if(isFinite(sv("p0"))) drone.core.x = sv("p0") / 1000;
    if(isFinite(sv("p1"))) drone.core.y = 1 - sv("p1") / 1000;
    if(isFinite(sv("p2"))) drone.fb = sv("p2") / 1000;
  }, 800);
}

let lastSaved = "";
function saveState(){
  try{
    const j = JSON.stringify(snapshot());
    if(j === lastSaved) return;
    lastSaved = j;
    fetch("/state", {method:"POST", body:j}).catch(()=>{});
  }catch(e){}
}
setInterval(saveState, 3000);
document.addEventListener("visibilitychange", ()=>{ if(document.hidden) saveState(); });
window.addEventListener("beforeunload", saveState);

function fitHdr(){
  const h = document.getElementById("hdr");
  if(!h) return;
  document.documentElement.style.setProperty("--hdrh", h.offsetHeight + "px");
}
addEventListener("resize", fitHdr);

function bootUI(){
  paintSlots();
  setTimeout(function(){ try{ send("R"); }catch(e){} }, 1200);   /* never inherit a hung note */
  fitHdr();
  /* a reload forgets which notes this page was holding, so clear the Pod first
     or they hang until their release tail runs out */
  send("R");
  fetch("/state").then(r=> r.ok ? r.json() : null).then(s=>{
    if(s) applyState(s);
    document.querySelectorAll("#views button").forEach(x=>x.classList.toggle("on", x.dataset.v === view));
    paintBend();
    renderView(); label(); divs(); readout();
    lastSaved = JSON.stringify(snapshot());
  }).catch(()=>{
    renderView(); label(); divs(); readout();
  });
}

try{ bootUI(); }catch(e){ console.error("boot", e); }
</script></body></html>
"""


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            body = HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/midi":
            try:
                names = sorted(n for n in os.listdir(MIDI_DIR)
                               if n.lower().endswith((".mid", ".midi", ".smf")))
            except OSError:
                names = []
            body = json.dumps(names).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/wt":
            try:
                names = sorted(n for n in os.listdir(WT_DIR) if n.lower().endswith(".wt"))
            except OSError:
                names = []
            body = json.dumps(names).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/wtfile":
            name = os.path.basename(self.path.split("f=")[-1]) if "f=" in self.path else ""
            full = os.path.join(WT_DIR, name)
            if name and os.path.isfile(full):
                with open(full, "rb") as fh:
                    body = fh.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(404)
        elif path == "/bankfile":
            name = os.path.basename(self.path.split("f=")[-1]) if "f=" in self.path else ""
            full = os.path.join(BANKS_DIR, name)
            if name and os.path.isfile(full):
                with open(full, "rb") as fh:
                    body = fh.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(404)
        elif path == "/midi/f":
            name = os.path.basename(self.path.split("f=")[-1]) if "f=" in self.path else ""
            full = os.path.join(MIDI_DIR, name)
            if name and os.path.isfile(full):
                with open(full, "rb") as fh:
                    body = fh.read()
                self.send_response(200)
                self.send_header("Content-Type", "audio/midi")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
        elif path == "/banks":
            body = json.dumps(list_banks()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/state":
            try:
                with open(STATE_PATH, "rb") as fh:
                    body = fh.read()
            except OSError:
                body = b""
            if not body:
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            q = queue.Queue(maxsize=400)
            with subs_lock:
                subs.append(q)
            try:
                while True:
                    try:
                        text = q.get(timeout=10)
                        self.wfile.write(("data: %s\n\n" % text).encode())
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                with subs_lock:
                    if q in subs:
                        subs.remove(q)
        else:
            self.send_error(404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode("ascii", "ignore").strip() if n else ""
        if path == "/wt/load":
            name = os.path.basename(self.path.split("f=")[-1]) if "f=" in self.path else ""
            full = os.path.join(WT_DIR, name)
            msg = "no such file"
            if name and os.path.isfile(full):
                with open(full, "rb") as fh:
                    raw = fh.read()
                if len(raw) >= 8 * 512 * 2:
                    for frame in range(8):
                        base = frame * 1024
                        for off in range(0, 512, 16):
                            chunk = raw[base + off * 2: base + off * 2 + 32]
                            send_body("F %d %d %s" % (frame, off, chunk.hex()))
                            time.sleep(0.008)
                    send_body("P 28 1")      # wavetable engine on
                    send_body("P 30 400")
                    msg = "loaded 8 frames"
                else:
                    msg = "file too short, need 8192 bytes"
            body = msg.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/banks/load":
            err = load_bank_file((query.get("f") or [""])[0])
            payload = (err or "loaded 32 voices").encode()
            self.send_response(200 if not err else 500)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if path == "/state":
            ok = True
            try:
                json.loads(body)  # refuse to store garbage
                tmp = STATE_PATH + ".tmp"
                with open(tmp, "w") as fh:
                    fh.write(body)
                os.replace(tmp, STATE_PATH)
            except (ValueError, OSError):
                ok = False
            self.send_response(204 if ok else 400)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/send" and body:
            try:
                open("/tmp/frames.log", "a").write(body + chr(10))
            except Exception:
                pass
            if not wolf_handle(body):
                send_body(body)
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self.send_error(400)


def _up():
    """bridge link comes up in the background: the touch page must load even
    when the Pod bridge is down, otherwise the screen is stuck on an error."""
    connect()
    threading.Thread(target=reader, daemon=True).start()
    threading.Thread(target=poller, daemon=True).start()


def main():
    srv = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("panel_ui: http://0.0.0.0:%d" % PORT, flush=True)
    threading.Thread(target=_up, daemon=True).start()
    srv.serve_forever()


if __name__ == "__main__":
    main()
