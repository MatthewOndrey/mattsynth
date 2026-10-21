# Protocol

Every frame: `@` body `*` XOR-hex `\n`. The checksum is the XOR of the body bytes.

## Notes and transport

    N <note> <vel> [part]     note on, part 0 or 1
    O <note> [cents]          note off, optional cent offset for microtonal ties
    K <note>                  hard free, used by the latch
    R                         panic, all notes off
    T <cents>                 pitch bend, cents
    Q                         status query, answers @C <voices> <us>

## Parameters

    P 0  cutoff          P 1  resonance        P 2  feedback
    P 3  algorithm 0-31  P 4  patch 0-31       P 5  max voices
    P 6  chorus          P 7  delay mix        P 8  delay ms
    P 9  spread          P 10 pan              P 12 bpm
    P 17 part             P 18-21 PEG levels   P 22-25 PEG rates
    P 26 volume 0-100     P 28 engine 0 FM 1 WT
    P 30 frame            P 31 attack ms       P 32 decay ms
    P 33 sustain          P 34 release ms      P 35 detune cents
    P 42 LFO rate         P 51 test tone       P 52 LFO sync
    P 60-74 LFO blocks: target, div, depth, shape, on

## Modulation matrix

    M <part> <lfo> <dest> <amount -1000..1000>

Destinations, in engine order:

    0 FRAME   1 MORPH TO   2 DETUNE   3 LEVEL
    4 ATTACK  5 DECAY      6 SUSTAIN  7 RELEASE
    8 FRAME ENV  9 ENV TIME  10 VEL-FRAME  11 KEY-FRAME
    12 RND PHASE  13 MORPH T

## Patch and bank

    L <slot> <name>           patch list entry
    B <bytes>                 bank upload
    S <logical> <value>       logical parameter write
    W <offset> <value>        packed voice byte write

`@D` echoes packed byte offsets; `S` uses logical numbers. Never mix them.
