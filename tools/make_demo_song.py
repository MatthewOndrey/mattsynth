"""Write a demo song for the sequencer: four patterns, a chain, kit drums."""
import json

KICK, SNR, HAT, CLAP, TOM, RIM, BASS, CHORD = 36, 37, 38, 39, 40, 41, 42, 43

def ev(step, note, part=0, vel=105, length=1):
    return {"s": step, "part": part, "n": note, "v": vel, "len": length}

p1 = []
for st in range(0, 16, 4):
    p1.append(ev(st, KICK, vel=118))
for st in (4, 12):
    p1.append(ev(st, SNR, vel=104))
for st in range(0, 16, 2):
    p1.append(ev(st, HAT, vel=72 if st % 4 else 92))

p2 = list(p1)
bassline = [45, 45, 45, 48, 43, 43, 41, 43]
for i, n in enumerate(bassline):
    p2.append(ev(i * 2, n, part=1, vel=100, length=1))

p3 = list(p2)
for st in (0, 6, 10):
    p3.append(ev(st, CHORD, vel=84, length=4))
p3.append(ev(14, RIM, vel=70))

p4 = [ev(st, KICK, vel=112) for st in (0, 8)]
p4 += [ev(st, CLAP, vel=96) for st in (4, 12)]
p4 += [ev(st, HAT, vel=60, length=1) for st in range(0, 16, 4)]
p4 += [ev(st, TOM, vel=100) for st in (14, 15)]
for i, n in enumerate([45, 45, 48, 45]):
    p4.append(ev(i * 4, n, part=1, vel=98, length=2))

song = {
    "v": 1,
    "bpm": 124,
    "cur": 0,
    "songOn": True,
    "pats": [
        {"len": 16, "ev": p1, "swing": 0},
        {"len": 16, "ev": p2, "swing": 12},
        {"len": 16, "ev": p3, "swing": 12},
        {"len": 16, "ev": p4, "swing": 0},
    ],
    "song": [{"p": 0, "r": 4}, {"p": 1, "r": 4}, {"p": 2, "r": 4}, {"p": 3, "r": 2}],
}
open("C:/Users/matto/pibuild/demo-house.json", "w", encoding="utf-8").write(json.dumps(song, indent=1))
print("patterns", len(song["pats"]), "chain", song["song"])
