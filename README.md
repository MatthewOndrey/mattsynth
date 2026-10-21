# MattSynth

A two-part FM and wavetable synth: Daisy Pod does the audio, a Raspberry Pi 400 runs
the touch panel and serves it to a kiosk Chromium on a 15 inch screen.

## Layout

    firmware/daisy-pod   Pod firmware: CDC protocol, FM engine, wavetable engine
    panel/panel_ui.py    the whole UI: HTTP, SSE, state, every tab
    panel/systemd        panel-ui, kiosk, podbridge units
    tools                composition scripts and flash helpers
    docs                 protocol and patch map

## Signal path

    browser (kiosk, 1920x1080)  ->  HTTP/SSE on the Pi
    Pi  ->  podbridge (TCP 5555)  ->  USB CDC  ->  Daisy Pod

Wire frames are `'@' <body> '*' <XOR hex> '\n'`. Every parameter is an integer.

## Engines

FM is a six operator DX7 class engine, 32 algorithms, 155 byte packed patch, two
parts sharing one voice pool. WT is eight additive frames of 512 samples morphing
per voice, four stage envelope, 60 cents detune, sharing the same filter, chorus,
delay, pan and voice pool. `P 28` switches engines at runtime, no reflash.

## Modulation

Three LFOs per part, each with a 14 entry amount matrix, so one LFO can drive
several knobs with separate depths. Continuous destinations (FRAME, MORPH TO,
DETUNE, LEVEL) are summed per sample; envelope destinations are sampled at
note-on. `M <part> <lfo> <dest> <amount>` writes a cell.

## Flashing

The app can reboot itself into the STM bootloader with the `Z` verb, then:

    dfu-util -d 0483:df11 -a 0 -s 0x08000000:leave -D build/PanelLink.bin

## Running the panel

    sudo systemctl restart panel-ui     # serves :8080
    sudo systemctl restart kiosk        # Chromium on HDMI-A-2

State lives in `/home/matto/panel_state.json` and is saved on change.
