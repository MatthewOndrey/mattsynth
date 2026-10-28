// PanelLink.cpp
// Daisy Pod: 12-voice DX7-style FM engine + the link to the ESP32-S3 panel.
//
// Transport: USB CDC on the Seed's own USB port (ttyACM0 on the host). The Pod's
// header does not bring out a usable UART pair (D11/D12 are the Seed's I2C1
// pads) and the Waveshare panel has no accessible GPIO with its case on, so the
// Pi bridges ttyACM0 (this) to ttyACM1 (the panel's CH343P -> panel UART0).
//
//   Pod USB  <-> Pi (podbridge) <-> panel USB-UART <-> panel UART0
//
// Protocol (ASCII, '@' body '*' CS '\n'):
//   host -> pod   N note vel      note on
//                 O note          note off
//                 R               panic
//                 P idx val       param: 0 cutoff, 1 resonance, 2 feedback,
//                                 3 algorithm 0-31, 4 patch index
//                 V off hex...    DX7 155-byte voice image, chunked (<=32 bytes)
//                 A               ping
//   pod -> host   M text          status ("pod ready", "patch 2 E.PIANO")
//                 K n v           knob 1/2, milli
//                 B n v           button 1/2
//                 E 0 d           encoder delta
//                 C voices load   active voices + CPU load permille, 1 Hz
//                 A               ping reply
//
// Build:
//   cd ../../vendor/libDaisy && make
//   cd ../DaisySP && make
//   cd back here && make
//   dfu-util -a 0 -s 0x08000000:leave -D build/PanelLink.bin

#include "daisy_pod.h"
#include "daisysp.h"
#include "dev/sdram.h"
#include "hid/usb.h"
#include "link_protocol.h"

#include "FmEngine.h"
#include "WtEngine.h"
#include "KitEngine.h"
#include "PianoEngine.h"

static bool kit_on = false;
static bool trace_bend = false;   /* TEMPORARY */
static bool piano_on = false;
KitEngine kit;
PianoEngine piano;
#include "fm_patch.h"

#include <cmath>
#include <cstdio>
#include <cstring>

using namespace daisy;
using namespace daisysp;

static DaisyPod        hw;
static UsbHandle       usb;
/* Both engines live in the same image and share everything downstream: the ladder
   filter, chorus, delay and the voice accounting. Only the oscillator core differs,
   so switching is a flag rather than a reflash. */
struct SynthHost
{
    fmsynth::Engine fm;
    wt::Engine wts;   /* not "wt": that name is the namespace */
    bool       wt_mode = false;

    void Init(float sr)          { fm.Init(sr); wts.Init(sr); }
    void NoteOn(uint8_t n, uint8_t v, uint8_t p = 0, int cents = 0, uint8_t tag = 0)
    {
        if(wt_mode){ wts.NoteOn(n, v, p); if(cents) wts.Bend(p, n, (float)cents); }
        else fm.NoteOn(n, v, p, cents, tag);
    }
    bool Debug(int note, int *a, int *b, int *c) { return fm.Debug(note, a, b, c); }

    void NoteOff(uint8_t n, int c = -1, uint8_t tag = 0) { wt_mode ? wts.NoteOff(n, c) : fm.NoteOff(n, c, tag); }
    void KillNote(uint8_t n)     { wt_mode ? wts.KillNote(n) : fm.KillNote(n); }
    void AllOff()                { wt_mode ? wts.AllOff() : fm.AllOff(); }
    void Bend(int p, int n, float c) { wt_mode ? wts.Bend(p, n, c) : fm.Bend(p, n, c); }
    void SetMaxVoices(int n)     { wt_mode ? wts.SetMaxVoices(n) : fm.SetMaxVoices(n); }
    int  max_voices() const      { return wt_mode ? wts.max_voices() : fm.max_voices(); }
    int  active_voices() const   { return wt_mode ? wts.active_voices() : fm.active_voices(); }
    void SetLoad(int us)         { if(!wt_mode) fm.SetLoad(us); }
    void SetPatch(const fm::Patch& p, int part) { if(!wt_mode) fm.SetPatch(p, part); }
    void SetFeedback(float f, int part = 0)     { if(!wt_mode) fm.SetFeedback(f, part); }
    void Render(float* l, float* r, size_t n)   { wt_mode ? wts.Render(l, r, n) : fm.Render(l, r, n); }
};
static SynthHost synth;
static Svf             filt_l, filt_r;
static Chorus          chorus;
/* 2 s of stereo delay in SDRAM: 768 kB out of the Seed's 64 MB, and it keeps
   the 512 kB SRAM for audio buffers and stack */
static DelayLine<float, 96000> dl_l DSY_SDRAM_BSS;
static DelayLine<float, 96000> dl_r DSY_SDRAM_BSS;
static podlink::Parser parser;

/* single producer (USB ISR) / single consumer (main loop) byte ring */
#define RING_MASK 1023
static volatile uint8_t  rx_ring[RING_MASK + 1];
static volatile uint16_t rx_head = 0, rx_tail = 0;

// ---------------------------------------------------------------- parameters
static volatile int p_cutoff_m = 350;
static volatile int p_res_m    = 550;
static volatile int p_fb_m     = 400;
static volatile int p_alg      = 4; /* table index 4 = DX7 algorithm 5 */
static volatile int p_patch    = 0;
static volatile int p_chorus_m = 300;
/* delay settings are pending until a note starts from silence: changing the
   time mid-tail sounds like a tape stop, so the update waits for the next note */
static int          p_delay_m  = 0;
static int          p_delay_ms = 280;
static int          p_delay_fb = 0;
static int          p_bpm      = 120;
static int          held_notes = 0; /* gates the delay latch: 0 = nothing held */
static int          p_div      = 4; /* index into kDivBeats: 0=1/16 .. 7=1/1 */
static volatile int live_dly_mix = 0;
static volatile int live_dly_fb  = 0;
/* master volume: 1000 = unity. Smoothed per sample in the callback so a drag
   does not step the whole mix and click. */
static volatile int p_master_m = 1000;
static volatile int p_test_tone = 0;   /* 1 = bypass both engines and buzz a sine */

/* wavetable LFO sync: when on, P 42 is a division index rather than 0.01 Hz units,
   and the rate follows P 12 (tempo) so the wobble sits on the grid */
static const float kLfoDivBeats[6] = {0.25f, 0.5f, 1.0f, 2.0f, 4.0f, 8.0f};
static int  wt_lfo_dest[3][2] = {{0,0},{0,0},{0,0}};
static int  wt_lfo_div[3][2] = {{2,2},{2,2},{2,2}};
static bool wt_lfo_sync[2] = {false, false};

static void recalc_wt_lfo(void)
{
    for(int p = 0; p < 2; ++p)
        for(int li = 0; li < 3; ++li)
        {
            const float beats = kLfoDivBeats[wt_lfo_div[li][p] & 7];
            synth.wts.prm(p).lfo[li].rate = (p_bpm / 60.0f) / beats;
        }
}
static float        wt_morph_s[2] = {1.5f, 1.5f};   /* morph time per part */
static int          live_dly_ms  = 280;

static inline int clampi(int v, int lo, int hi)
{
    return v < lo ? lo : (v > hi ? hi : v);
}

/* note values in beats, including the triplets */
static const float kDivBeats[8] = {0.25f, 1.0f / 3.0f, 0.5f, 2.0f / 3.0f,
                                   1.0f,  4.0f / 3.0f, 2.0f, 4.0f};

static void recalc_delay_ms(void)
{
    const float ms = 60000.0f / (float)p_bpm * kDivBeats[p_div];
    p_delay_ms     = clampi((int)(ms + 0.5f), 50, 2000);
}

/* move pending delay settings into the live effect. Called from the main loop
   at note-on, so the ISR only ever sees values that were not mid-edit. */
static void commit_delay(void)
{
    live_dly_mix = p_delay_m;
    live_dly_fb  = p_delay_fb;
    if(live_dly_ms != p_delay_ms)
    {
        live_dly_ms = p_delay_ms;
        dl_l.SetDelay((float)live_dly_ms * hw.AudioSampleRate() / 1000.0f);
        dl_r.SetDelay((float)live_dly_ms * hw.AudioSampleRate() / 1000.0f);
    }
}

// ------------------------------------------------------------------ patch bank
static const int kMaxPatches = 32; /* one DX7 bulk bank */
static fm::Patch bank[kMaxPatches];
static fm::Patch live_part[2]; /* what the engine is actually running, per part */
static int       edit_part = 0; /* which part the edit verbs act on */
static fm::Patch& lp() { return live_part[edit_part]; }
static void       send_dump(void); /* defined below, used by the part switch */
static int       bank_size = 0;

static fm::Op mkop(uint8_t r1, uint8_t r2, uint8_t r3, uint8_t r4,
                   uint8_t l1, uint8_t l2, uint8_t l3, uint8_t l4,
                   uint8_t out, uint8_t coarse, uint8_t fine, uint8_t detune,
                   uint8_t kvs)
{
    fm::Op o;
    memset(&o, 0, sizeof(o));
    o.rate[0] = r1; o.rate[1] = r2; o.rate[2] = r3; o.rate[3] = r4;
    o.level[0] = l1; o.level[1] = l2; o.level[2] = l3; o.level[3] = l4;
    o.break_point  = 39;
    o.left_curve   = 0;
    o.right_curve  = 0;
    o.output_level = out;
    o.freq_coarse  = coarse;
    o.freq_fine    = fine;
    o.detune       = detune;
    o.key_vel_sens = kvs;
    return o;
}

static void add_patch(const char* name, uint8_t alg, uint8_t fb, const fm::Op ops[6])
{
    if(bank_size >= kMaxPatches)
        return;
    fm::Patch& p = bank[bank_size++];
    memset(&p, 0, sizeof(p));
    for(int i = 0; i < 6; ++i)
        p.op[i] = ops[i];
    p.algorithm = alg;
    p.feedback  = fb;
    p.transpose = 24;
    p.pan       = 50;
    p.spread    = 30; /* a little width by default */
    strncpy(p.name, name, 10);
}

static void build_bank()
{
    /* three independent 2-op stacks (algorithm 5): op0->op1, op2->op3, op4->op5 */
    fm::Op o[6];

    /* E.PIANO: 1:1 body plus a 14:1 tine */
    o[0] = mkop(99, 62, 45, 40, 99, 45, 0, 0, 88, 1, 0, 7, 3);
    o[1] = mkop(99, 62, 45, 40, 99, 70, 0, 0, 99, 1, 0, 7, 3);
    o[2] = mkop(99, 76, 60, 40, 99, 30, 0, 0, 68, 14, 0, 7, 5);
    o[3] = mkop(99, 76, 60, 40, 99, 55, 0, 0, 86, 1, 0, 7, 4);
    o[4] = mkop(99, 58, 50, 40, 99, 35, 0, 0, 58, 1, 0, 5, 2);
    o[5] = mkop(99, 58, 50, 40, 99, 60, 0, 0, 92, 1, 0, 9, 3);
    add_patch("E.PIANO", 4, 4, o);

    /* BASS: low, snappy, a bit of feedback grit */
    o[0] = mkop(99, 58, 30, 46, 99, 60, 0, 0, 92, 1, 0, 7, 2);
    o[1] = mkop(99, 58, 30, 46, 99, 80, 0, 0, 99, 0, 0, 7, 2);
    o[2] = mkop(99, 50, 25, 46, 99, 40, 0, 0, 45, 2, 0, 7, 1);
    o[3] = mkop(99, 50, 25, 46, 99, 70, 0, 0, 92, 0, 0, 7, 2);
    o[4] = mkop(99, 45, 25, 46, 99, 20, 0, 0, 28, 3, 0, 7, 0);
    o[5] = mkop(99, 45, 25, 46, 99, 30, 0, 0, 40, 1, 0, 7, 1);
    add_patch("BASS", 4, 6, o);

    /* BELL: inharmonic ratios, long tail */
    o[0] = mkop(99, 40, 30, 35, 99, 55, 20, 0, 86, 3, 50, 7, 4);
    o[1] = mkop(99, 40, 30, 35, 99, 70, 30, 0, 99, 1, 0, 7, 4);
    o[2] = mkop(99, 36, 26, 32, 99, 45, 10, 0, 70, 7, 0, 7, 5);
    o[3] = mkop(99, 36, 26, 32, 99, 60, 20, 0, 92, 2, 0, 7, 5);
    o[4] = mkop(99, 32, 22, 30, 99, 30, 0, 0, 50, 11, 0, 7, 6);
    o[5] = mkop(99, 32, 22, 30, 99, 45, 0, 0, 78, 4, 0, 7, 6);
    add_patch("BELL", 4, 3, o);
    {
        fm::Patch& pb = bank[bank_size - 1];
        /* pitch envelope is flat by default: a held note must not drift down.
           The OPS page can still dial one in when a sound wants the bounce. */
        pb.pitch_rate[0]  = 99;
        pb.pitch_rate[1]  = 34;
        pb.pitch_rate[2]  = 20;
        pb.pitch_rate[3]  = 20;
        pb.pitch_level[0] = 50;
        pb.pitch_level[1] = 50;
        pb.pitch_level[2] = 50;
        pb.pitch_level[3] = 50;
    }

    /* ORGAN: algorithm 32, six carriers, purely additive */
    o[0] = mkop(99, 99, 99, 60, 99, 99, 99, 0, 92, 1, 0, 7, 0);
    o[1] = mkop(99, 99, 99, 60, 99, 99, 99, 0, 80, 2, 0, 7, 0);
    o[2] = mkop(99, 99, 99, 60, 99, 99, 99, 0, 70, 3, 0, 7, 0);
    o[3] = mkop(99, 99, 99, 60, 99, 99, 99, 0, 58, 4, 0, 7, 0);
    o[4] = mkop(99, 99, 99, 60, 99, 99, 99, 0, 46, 6, 0, 6, 0);
    o[5] = mkop(99, 99, 99, 60, 99, 99, 99, 0, 36, 8, 0, 8, 0);
    add_patch("ORGAN", 31, 0, o);

    /* PAD: slow attack, long release, detuned stacks */
    o[0] = mkop(45, 60, 55, 40, 99, 80, 70, 0, 70, 1, 0, 5, 2);
    o[1] = mkop(45, 60, 55, 40, 99, 90, 85, 0, 92, 1, 0, 9, 2);
    o[2] = mkop(40, 55, 50, 35, 99, 75, 65, 0, 62, 2, 0, 7, 2);
    o[3] = mkop(40, 55, 50, 35, 99, 85, 80, 0, 88, 2, 0, 5, 2);
    o[4] = mkop(35, 50, 45, 30, 99, 70, 60, 0, 48, 3, 0, 6, 1);
    o[5] = mkop(35, 50, 45, 30, 99, 80, 75, 0, 74, 3, 0, 8, 1);
    add_patch("PAD", 4, 2, o);
    {
        fm::Patch& pp = bank[bank_size - 1];
        pp.lfo_wave   = 4; /* sine */
        pp.lfo_speed  = 34;
        pp.lfo_delay  = 8;
        pp.lfo_pmd    = 30;
        pp.lfo_amd    = 12;
    }

    /* PLUCK: instant attack, fast decay, velocity sensitive */
    o[0] = mkop(99, 70, 45, 45, 99, 40, 0, 0, 90, 1, 0, 7, 4);
    o[1] = mkop(99, 70, 45, 45, 99, 60, 0, 0, 99, 1, 0, 7, 4);
    o[2] = mkop(99, 80, 50, 45, 99, 30, 0, 0, 60, 5, 0, 7, 6);
    o[3] = mkop(99, 80, 50, 45, 99, 50, 0, 0, 84, 1, 0, 7, 6);
    o[4] = mkop(99, 65, 40, 45, 99, 20, 0, 0, 40, 2, 0, 6, 3);
    o[5] = mkop(99, 65, 40, 45, 99, 40, 0, 0, 70, 2, 0, 8, 3);
    add_patch("PLUCK", 4, 3, o);
}

// ------------------------------------------------------------------- sending
static void send_body(const char* body)
{
    char         out[podlink::kBufSize];
    const size_t n = podlink::frame(out, sizeof(out), body);
    if(n > 0)
    {
        for(int attempt = 0; attempt < 8; ++attempt)
        {
            if(usb.TransmitInternal(reinterpret_cast<uint8_t*>(out), n)
               == UsbHandle::Result::OK)
                break;
            System::Delay(1);
        }
    }
}

static void send_iv(char verb, int idx, int val)
{
    char body[24];
    snprintf(body, sizeof(body), "%c %d %d", verb, idx, val);
    send_body(body);
}

static void send_msg(const char* text)
{
    char body[podlink::kBufSize];
    snprintf(body, sizeof(body), "M %s", text);
    send_body(body);
}

/* whole bank, one frame each: "@L <idx> <name>", so a host can build a picker */
static void send_patch_list(void)
{
    for(int i = 0; i < bank_size; ++i)
    {
        char body[48];
        snprintf(body, sizeof(body), "L %d %s", i, bank[i].name);
        send_body(body);
    }
}

/* bulk voice, uploaded in chunks that fit the line buffer:
   "B <slot> <offset> <hex bytes>" */
static int hexval(char c);
static uint8_t bank_stage[128];
static int     bank_stage_slot = -1;
static int     bank_stage_len  = 0;

static void handle_bank_voice(const char* body)
{
    int slot = 0;
    int off  = 0;
    if(sscanf(body, "B %d %d", &slot, &off) != 2)
        return;
    if(slot < 0 || slot >= kMaxPatches || off < 0 || off >= 128)
        return;

    const char* q      = body;
    int         spaces = 0;
    while(*q && spaces < 3)
    {
        if(*q == ' ')
            ++spaces;
        ++q;
    }
    if(spaces < 3)
        return;

    if(slot != bank_stage_slot || off != bank_stage_len)
    {
        bank_stage_slot = slot;
        bank_stage_len  = off; /* start over at the offset they claim */
    }

    while(q[0] && q[1] && bank_stage_len < 128)
    {
        const int hi = hexval(q[0]);
        const int lo = hexval(q[1]);
        if(hi < 0 || lo < 0)
            break;
        bank_stage[bank_stage_len++] = (uint8_t)((hi << 4) | lo);
        q += 2;
    }

    if(bank_stage_len < 128)
        return;

    fm::Patch p;
    fm::unpack_bulk(bank_stage, &p);
    bank[slot] = p;
    if(slot >= bank_size)
        bank_size = slot + 1;
    bank_stage_slot = -1;
    bank_stage_len  = 0;

    char msg[48];
    snprintf(msg, sizeof(msg), "bank %d %s", slot, p.name);
    send_msg(msg);
}

static void select_patch(int idx, bool announce)
{
    if(bank_size <= 0)
        return;
    idx = clampi(idx, 0, bank_size - 1);
    p_patch = idx;
    fm::Patch p = bank[idx];
    p_alg       = p.algorithm & 31; /* a patch carries its own routing */
    lp()  = p;
    synth.SetPatch(p, edit_part);
    if(announce)
    {
        char msg[48];
        snprintf(msg, sizeof(msg), "patch %d %s", idx, p.name);
        send_msg(msg);
    }
}

// ----------------------------------------------------------------- receiving
/* USB CDC receive: ISR context, so only move bytes into the ring. */
static void UsbRx(uint8_t* buff, uint32_t* len)
{
    for(uint32_t i = 0; i < *len; ++i)
    {
        const uint16_t next = (uint16_t)((rx_head + 1) & RING_MASK);
        if(next != rx_tail) /* drop on overflow rather than corrupt a frame */
        {
            rx_ring[rx_head] = buff[i];
            rx_head          = next;
        }
    }
}

static void apply_param(int idx, int val)
{
    switch(idx)
    {
        case 0: p_cutoff_m = clampi(val, 0, 1000); break;
        case 1: p_res_m = clampi(val, 0, 1000); break;
        case 2:
            p_fb_m = clampi(val, 0, 1000);
            synth.SetFeedback((float)p_fb_m / 1000.0f);
            break;
        case 3:
            /* edit the selected patch in place, like a synth's edit buffer */
            p_alg = clampi(val, 0, 31);
            lp().algorithm        = (uint8_t)p_alg;
            bank[p_patch].algorithm     = (uint8_t)p_alg;
            synth.SetPatch(lp(), edit_part);
            break;
        case 4:
            select_patch(val, true);
            /* the header patch control is global: load it on both parts, otherwise
               scrolling while you play the other part changes something you cannot hear */
            synth.SetPatch(bank[clampi(val, 0, bank_size - 1)], 1 - edit_part);
            break;
        case 5:
        {
            synth.SetMaxVoices(val);
            char msg[32];
            snprintf(msg, sizeof(msg), "voices %d", synth.max_voices());
            send_msg(msg);
            break;
        }
        /* effects and stereo, added with the chain */
        case 6: p_chorus_m = clampi(val, 0, 1000); break;
        case 7: p_delay_m = clampi(val, 0, 1000); break;
        case 8: p_delay_ms = clampi(val, 50, 2000); break;
        case 9:
            lp().spread = (uint8_t)clampi(val / 10, 0, 99);
            synth.SetPatch(lp(), edit_part);
            break;
        case 10:
            lp().pan = (uint8_t)clampi(val / 10, 0, 99);
            synth.SetPatch(lp(), edit_part);
            break;
        case 11: p_delay_fb = clampi(val, 0, 1000); break;
        case 12:
            p_bpm = clampi(val, 60, 200);
            recalc_delay_ms();
            recalc_wt_lfo();
            break;
        case 13:
            p_div = clampi(val, 0, 7);
            recalc_delay_ms();
            break;
        case 26: p_master_m = clampi(val, 0, 1000); break;
        case 51: p_test_tone = val != 0; break;
        case 52:
            wt_lfo_sync[edit_part] = val != 0;
            if(wt_lfo_sync[edit_part]) recalc_wt_lfo();
            send_msg(wt_lfo_sync[edit_part] ? "lfo sync on" : "lfo free");
            break;
        case 28:
            /* engine: 0 = six-operator FM, 1 = wavetable */
            synth.AllOff();
            synth.wt_mode = val != 0;
            send_msg(synth.wt_mode ? "engine wavetable" : "engine fm");
            break;
        /* wavetable voice: frame, envelope, morph, trim */
        /* wavetable voice, per part: P 17 chooses which part edits below */
        case 30: synth.wts.SetFrame(edit_part, (float)clampi(val, 0, 1000) * (wt::kFrames - 1) / 1000.0f); break;
        case 31: synth.wts.SetAttack(edit_part, (float)clampi(val, 1, 4000) / 1000.0f); break;
        case 32: synth.wts.SetDecay(edit_part, (float)clampi(val, 1, 4000) / 1000.0f); break;
        case 33: synth.wts.SetSustain(edit_part, (float)clampi(val, 0, 1000) / 1000.0f); break;
        case 34: synth.wts.SetRelease(edit_part, (float)clampi(val, 1, 4000) / 1000.0f); break;
        case 35: synth.wts.SetDetune(edit_part, (float)clampi(val, 0, 60)); break;
        case 36: synth.wts.SetMorph(edit_part, (float)clampi(val, 0, 1000) * (wt::kFrames - 1) / 1000.0f,
                                    wt_morph_s[edit_part]); break;
        case 37: wt_morph_s[edit_part] = clampi(val, 1, 8000) / 1000.0f; break;
        case 38: synth.wts.SetLevel(edit_part, (float)clampi(val, 0, 1000) / 1000.0f); break;
        case 39: synth.wts.SetPan(edit_part, ((float)clampi(val, 0, 1000) / 500.0f) - 1.0f); break;
        /* part LFO: target, shape, rate, depth, granularity, on */
        /* three LFOs per part: P 60..64 is A, 65..69 B, 70..74 C
           fields in order: target, div, depth, shape, on */
        case 40: case 41: case 42: case 43: case 44: case 45:
        case 60: case 61: case 62: case 63: case 64:
        case 65: case 66: case 67: case 68: case 69:
        case 70: case 71: case 72: case 73: case 74:
            {
                int li = 0, fld = 0;
                if(idx >= 60)
                {
                    const int k = idx - 60;
                    li  = k / 5;
                    fld = k % 5;
                }
                else
                {
                    li  = 0;
                    fld = idx - 40;
                }
                if(li > 2) break;
                auto& L = synth.wts.prm(edit_part).lfo[li];
                switch(fld)
                {
                    case 0: wt_lfo_dest[li][edit_part] = clampi(val, 0, 13); break;
                    case 1: wt_lfo_div[li][edit_part] = clampi(val, 0, 5); recalc_wt_lfo(); break;
                    case 2: L.amt[wt_lfo_dest[li][edit_part]] = (float)clampi(val, 0, 1000) / 1000.0f; break;
                    case 3: L.shape = clampi(val, 0, 4); break;
                    default: L.on = val != 0; break;
                }
            }
            break;
        /* per note shaping, also per part */
        case 46: synth.wts.prm(edit_part).envAmt    = ((float)clampi(val, 0, 1000) - 500.0f) / 500.0f * (wt::kFrames - 1); break;
        case 47: synth.wts.prm(edit_part).envTime   = (float)clampi(val, 1, 4000) / 1000.0f; break;
        case 48: synth.wts.prm(edit_part).velFrame  = (float)clampi(val, 0, 1000) / 1000.0f; break;
        case 49: synth.wts.prm(edit_part).keyFrame  = (float)clampi(val, 0, 1000) / 1000.0f; break;
        case 50: synth.wts.prm(edit_part).phaseRand = (float)clampi(val, 0, 1000) / 1000.0f; break;

        case 17:
            /* edit target: every other edit verb now acts on this part */
            edit_part = clampi(val, 0, 1);
            p_alg     = lp().algorithm & 31;
            send_dump();
            break;
        default: break;
    }
}

static int hexval(char c)
{
    if(c >= '0' && c <= '9') return c - '0';
    if(c >= 'a' && c <= 'f') return c - 'a' + 10;
    if(c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

/* V <offset> <hex bytes> : DX7 packed voice, chunked */
static uint8_t up_buf[fm::kPatchSize];
static int     up_len = 0;

static void handle_voice(const char* body)
{
    int off = 0;
    if(sscanf(body, "V %d", &off) != 1)
        return;
    if(off < 0 || off >= fm::kPatchSize)
    {
        up_len = 0;
        return;
    }

    const char* q      = body;
    int         spaces = 0;
    while(*q && spaces < 2)
    {
        if(*q == ' ')
            ++spaces;
        ++q;
    }
    if(spaces < 2)
        return;

    int written = 0;
    while(q[0] && q[1] && (off + written) < fm::kPatchSize)
    {
        const int hi = hexval(q[0]);
        const int lo = hexval(q[1]);
        if(hi < 0 || lo < 0)
            break;
        up_buf[off + written] = (uint8_t)((hi << 4) | lo);
        ++written;
        q += 2;
    }
    if(written == 0)
        return;

    if(off == 0)
        up_len = 0;
    else if(off != up_len)
    {
        up_len = 0; /* out of order, start over */
        return;
    }
    up_len += written;

    if(up_len >= fm::kPatchSize)
    {
        fm::Patch p;
        fm::unpack(up_buf, &p);
        p.algorithm = (uint8_t)(p_alg & 31);
        lp()  = p;
        synth.SetPatch(p, edit_part);
        char msg[48];
        snprintf(msg, sizeof(msg), "loaded %s", p.name);
        send_msg(msg);
        up_len = 0;
    }
}

/* ---- patch dump and per-operator edits ---------------------------------- */

/* D <op> <42 hex> : one operator, 21 packed bytes. G <alg> <fb> <name>. */
static void send_op_dump(int opi)
{
    uint8_t packed[fm::kPatchSize];
    fm::pack(&lp(), packed);
    char body[64];
    int  off = snprintf(body, sizeof(body), "D %d ", opi);
    for(int k = 0; k < 21 && off < (int)sizeof(body) - 3; ++k)
        off += snprintf(body + off, sizeof(body) - off, "%02X", packed[opi * 21 + k]);
    send_body(body);
}

static void send_dump(void)
{
    for(int i = 0; i < 6; ++i)
        send_op_dump(i);
    char body[48];
    snprintf(body, sizeof(body), "G %d %d %s", lp().algorithm, lp().feedback,
             lp().name);
    send_body(body);
}

/* S <op> <param> <val>: params 0-3 rates, 4-7 levels, 8 out, 9 coarse,
   10 fine, 11 detune, 12 key velocity sensitivity. */
static void set_op_value(int opi, int param, int val)
{
    if(opi < 0 || opi > 5)
        return;
    fm::Op& o = lp().op[opi];
    switch(param)
    {
        case 0:
        case 1:
        case 2:
        case 3: o.rate[param] = (uint8_t)clampi(val, 0, 99); break;
        case 4:
        case 5:
        case 6:
        case 7: o.level[param - 4] = (uint8_t)clampi(val, 0, 99); break;
        case 8: o.output_level = (uint8_t)clampi(val, 0, 99); break;
        case 9: o.freq_coarse = (uint8_t)clampi(val, 0, 31); break;
        case 10: o.freq_fine = (uint8_t)clampi(val, 0, 99); break;
        case 11: o.detune = (uint8_t)clampi(val, 0, 14); break;
        case 12: o.key_vel_sens = (uint8_t)clampi(val, 0, 7); break;
        /* global LFO fields, per-operator index ignored */
        case 13: lp().lfo_speed = (uint8_t)clampi(val, 0, 99); break;
        case 14: lp().lfo_pmd = (uint8_t)clampi(val, 0, 99); break;
        case 15: lp().lfo_amd = (uint8_t)clampi(val, 0, 99); break;
        case 16: lp().lfo_wave = (uint8_t)clampi(val, 0, 5); break;
        case 17: lp().lfo_delay = (uint8_t)clampi(val, 0, 99); break;
        /* pitch envelope: 4 levels then 4 rates. Level 50 is "no offset", so a
           patch whose L1 sits at 96 starts 1.8 semitones sharp and glides down.
           Setting all four levels to 50 flattens the glide on that patch. */
        case 18: case 19: case 20: case 21:
            lp().pitch_level[param - 18] = (uint8_t)clampi(val, 0, 99);
            break;
        case 22: case 23: case 24: case 25:
            lp().pitch_rate[param - 22] = (uint8_t)clampi(val, 0, 99);
            break;
        default: return;
    }
    synth.SetPatch(lp(), edit_part);
    send_dump(); /* echo the whole patch so the panel stays in sync */
}

static void handle(const char* body)
{
    const char v = podlink::verb(body);
    int        a = 0, b = 0, c = 0;

    switch(v)
    {
        case 'N':
            {
                int part = 0, cents = 0, tag = 0;
                const int n = sscanf(body, "N %d %d %d %d %d", &a, &b, &part, &cents, &tag);
                if(n >= 2)
                {
                    /* notes 36..43 are the kit while it is armed */
                    if(piano_on)
                    {
                        piano.Trigger(a, b);
                        break;
                    }
                    if(kit_on && a >= 36 && a <= 43)
                    {
                        kit.Trigger(a, b);
                        break;
                    }
                    /* latch any pending delay edit on a note started from nothing
                       held: release tails no longer block it, only legato does */
                    if(held_notes == 0)
                        commit_delay();
                    held_notes++;
                    synth.NoteOn((uint8_t)clampi(a, 0, 127), (uint8_t)clampi(b, 0, 127),
                                 n >= 3 ? clampi(part, 0, 1) : 0,
                                 n >= 4 ? cents : 0,
                                 n >= 5 ? (uint8_t)clampi(tag, 0, 1) : 0);
                }
            }
            break;
        case 'M':
        {
            /* M <part> <lfo> <dest> <amount> : matrix cell, any LFO to any destination */
            int pp = 0, li = 0, de = 0, am = 0;
            if(sscanf(body + 1, "%d %d %d %d", &pp, &li, &de, &am) == 4 && li >= 0 && li < 3
               && de >= 0 && de < 14)
                synth.wts.prm(clampi(pp, 0, 1)).lfo[li].amt[de] = (float)clampi(am, -1000, 1000) / 1000.0f;
            break;
        }
        case 'K':
            /* K <note>: hard free, used by the panel's held-note latch */
            if(sscanf(body, "K %d", &a) == 1)
            {
                if(kit_on && a >= 36 && a <= 43) kit.KillAll();
                piano.KillAll();
                synth.KillNote((uint8_t)clampi(a, 0, 127));
            }
            break;
        case 'O':
            {
                int cents = -1, tag = 0;
                const int n = sscanf(body, "O %d %d %d", &a, &cents, &tag);
                if(n >= 1)
                {
                    if(held_notes > 0)
                        held_notes--;
                    synth.NoteOff((uint8_t)clampi(a, 0, 127), n >= 2 ? cents : -1,
                                  n >= 3 ? (uint8_t)clampi(tag, 0, 1) : 0);
                }
            }
            break;
        case 'P':
            if(sscanf(body, "P %d %d", &a, &b) == 2)
            {
                if(a == 36) { kit_on = (b != 0); if(!kit_on) kit.KillAll(); }
                else if(a == 37) { piano_on = (b != 0); if(!piano_on) piano.KillAll(); }
                else apply_param(a, b);
            }
            break;
        case 'T':
            {
                /* T <part> <note> <cents> : live pitch offset, note -1 = whole part */
                /* integers only: newlib-nano sscanf has no %f unless float scanning is
                   linked in, and it then returns 2 and the bend is silently skipped */
                int cents = 0;
                if(sscanf(body, "T %d %d %d", &a, &b, &cents) == 3)
                {
                    synth.Bend(clampi(a, 0, 1), b, (float)cents);
                    if(piano_on)
                    {
                        piano.Bend(b, cents);   /* the sample engine bends too */
                        if(trace_bend)
                        {
                            int pn = 0, pr = 0, pb = 0;
                            if(piano.Debug(b, &pn, &pr, &pb))
                            {
                                char m[40];
                                snprintf(m, sizeof(m), "D %d %d %d", pn, pr, pb);
                                send_body(m);
                            }
                        }
                    }
                    if(trace_bend)
                    {
                        int dn = 0, dhz = 0, db = 0;
                        if(synth.Debug(b, &dn, &dhz, &db))
                        {
                            char m[40];
                            snprintf(m, sizeof(m), "D %d %d %d", dn, dhz, db);
                            send_body(m);
                        }
                    }
                }
            }
            break;
        case 'Y':
        {
            /* Y <pad> <rate> <level> <trim>, thousandths */
            int pd = 0, rt = 1000, lv = 1000, tr = 1000;
            if(sscanf(body + 1, "%d %d %d %d", &pd, &rt, &lv, &tr) == 4)
                kit.SetPad(pd, rt, lv, tr);
            break;
        }
        case 'V': handle_voice(body); break;
        case 'Q': send_dump(); break;
        case 'W':
            if(sscanf(body, "W %d", &a) == 1)
            {
                a = clampi(a, 0, bank_size - 1);
                bank[a] = lp();
                char msg[32];
                snprintf(msg, sizeof(msg), "saved slot %d", a);
                send_msg(msg);
            }
            break;
        case 'S':
            if(sscanf(body, "S %d %d %d", &a, &b, &c) == 3)
                set_op_value(a, b, c);
            break;
        case 'R':
            held_notes = 0;
            synth.AllOff();
            break;
        case 'F':
            {
                /* F <frame> <offset> <hex int16 little endian> : wavetable upload */
                int fr = 0, off = 0, used = 0;
                if(sscanf(body, "F %d %d %n", &fr, &off, &used) == 2 && used > 0)
                {
                    int16_t buf[16];
                    int n = 0;
                    const char* h = body + used;
                    while(n < 16 && h[0] && h[1] && h[2] && h[3])
                    {
                        const int hi = hexval(h[0]) * 16 + hexval(h[1]);
                        const int lo = hexval(h[2]) * 16 + hexval(h[3]);
                        if(hi < 0 || lo < 0) break;
                        buf[n++] = (int16_t)((hi << 8) | lo);
                        h += 4;
                    }
                    if(n) synth.wts.WriteFrame(fr, off, buf, n);
                }
            }
            break;
        case 'Z':
            /* hands-free reflash: reboot straight into the STM bootloader, so a
               firmware update never needs a BOOT + RESET press again */
            send_msg("entering DFU");
            for(volatile int i = 0; i < 200000; ++i) {}
#if defined(BOOT_APP)
            /* running from QSPI, so the Daisy bootloader is the one that can rewrite
               it; it waits for DFU instead of timing out */
            System::ResetToBootloader(System::DAISY_INFINITE_TIMEOUT);
#else
            System::ResetToBootloader(System::STM);
#endif
            break;
        case 'A': send_body("A"); break;
        case 'L': send_patch_list(); break;
        case 'B': handle_bank_voice(body); break;
        default: break; /* unknown verbs are ignored on purpose */
    }
}

// --------------------------------------------------------------------- audio
static volatile uint32_t g_cb_us = 0; /* audio callback time, microseconds */

static void AudioCallback(AudioHandle::InterleavingInputBuffer  in,
                          AudioHandle::InterleavingOutputBuffer out,
                          size_t                                size)
{
    const uint32_t t0 = System::GetUs();
    (void)in;
    float  l[64], r[64];
    size_t frames = size / 2;
    if(frames > 64)
        frames = 64;

    synth.Render(l, r, frames);
    if(kit_on)
        kit.Render(l, r, frames);
    if(piano_on)
        piano.Render(l, r, frames);

    /* diagnostic: a plain sine through the same output stage. If this is silent the
       problem is downstream of the engines, in the codec, the cable or the amp. */
    if(p_test_tone)
    {
        static float ph = 0.0f;
        for(size_t i = 0; i < frames; ++i)
        {
            ph += 220.0f / hw.AudioSampleRate();
            if(ph >= 1.0f) ph -= 1.0f;
            l[i] = r[i] = sinf(ph * 6.2831853f) * 0.25f;
        }
    }

    const float c   = (float)p_cutoff_m / 1000.0f;
    const float fq  = 30.0f + (c * c) * 12000.0f;
    const float res = 0.15f + ((float)p_res_m / 1000.0f) * 0.8f;
    filt_l.SetFreq(fq);
    filt_r.SetFreq(fq);
    filt_l.SetRes(res);
    filt_r.SetRes(res);

    const float chorus_mix = (float)p_chorus_m / 1000.0f;
    const float g_target   = (float)p_master_m / 1000.0f;
    static float g_master  = 1.0f;
    const float delay_mix  = (float)live_dly_mix / 1000.0f;
    float       fb         = (float)live_dly_fb / 1000.0f;
    if(fb > 0.99f)
        fb = 0.99f; /* 100% is a unity loop: one hair of headroom keeps it alive */

    for(size_t i = 0; i < frames; ++i)
    {
        float xl = l[i];
        float xr = r[i];

        filt_l.Process(xl);
        filt_r.Process(xr);
        xl = filt_l.Low();
        xr = filt_r.Low();

        /* stereo chorus: DaisySP's Chorus takes a mono input and produces an
           internally panned pair, so feed it the sum and blend both sides */
        chorus.Process((xl + xr) * 0.5f);
        xl = xl * (1.0f - chorus_mix) + chorus.GetLeft() * chorus_mix;
        xr = xr * (1.0f - chorus_mix) + chorus.GetRight() * chorus_mix;

        /* ping-pong delay with cross feedback */
        const float dl = dl_l.Read();
        const float dr = dl_r.Read();
        dl_l.Write(xl + dr * fb);
        dl_r.Write(xr + dl * fb);
        xl = xl * (1.0f - delay_mix) + dl * delay_mix;
        xr = xr * (1.0f - delay_mix) + dr * delay_mix;

        g_master += (g_target - g_master) * 0.15f;
        out[i * 2]     = xl * g_master;
        out[i * 2 + 1] = xr * g_master;
    }
    g_cb_us = System::GetUs() - t0;
    synth.SetLoad((int)g_cb_us);
}

// ---------------------------------------------------------------------- main
static IWDG_HandleTypeDef hiwdg;

int main(void)
{
    hw.Init();
    kit.Init();
    piano.Init();
    /* say where the code is running from: internal flash or the QSPI boot image */
    send_msg(System::GetProgramMemoryRegion() == System::MemoryRegion::INTERNAL_FLASH
             ? "@BOOT int" : "@BOOT qspi");
    hw.SetAudioBlockSize(48);

    /* Self recovery: a wedged main loop kills USB and the only cure used to be
       pulling the cable. With the independent watchdog a hang resets the board
       in about two seconds. Kicked from the main loop, not the audio callback,
       because the audio interrupt keeps running even when the loop is stuck. */
    hiwdg.Instance       = IWDG1;
    hiwdg.Init.Prescaler = IWDG_PRESCALER_64;   /* 32 kHz LSI / 64 = 500 Hz */
    hiwdg.Init.Reload    = 1000;                /* 1000 ticks = 2 s */
    hiwdg.Init.Window    = IWDG_WINDOW_DISABLE;
    HAL_IWDG_Init(&hiwdg);

    const float sr0 = hw.AudioSampleRate();

    filt_l.Init(sr0);
    filt_r.Init(sr0);
    filt_l.SetDrive(0.2f);
    filt_r.SetDrive(0.2f);

    chorus.Init(sr0);
    chorus.SetLfoFreq(0.85f);
    chorus.SetLfoDepth(0.35f, 0.60f);
    chorus.SetDelayMs(12.0f);
    chorus.SetFeedback(0.12f);

    recalc_delay_ms();
    live_dly_ms = p_delay_ms;
    dl_l.Init();
    dl_r.Init();
    dl_l.SetDelay((float)live_dly_ms * sr0 / 1000.0f);
    dl_r.SetDelay((float)live_dly_ms * sr0 / 1000.0f);

    synth.Init(hw.AudioSampleRate());
    build_bank();
    /* part A starts on patch 0, part B on patch 1, so two instruments are
       audible immediately: the UI can reassign either with P 17 then P 4 */
    select_patch(0, false);
    {
        const int keep = edit_part;
        edit_part      = 1;
        select_patch(1, false);
        edit_part = keep;
    }
    synth.SetFeedback((float)p_fb_m / 1000.0f, 0);
    synth.SetFeedback((float)p_fb_m / 1000.0f, 1);

    /* CDC on the Seed's own USB port */
    usb.Init(UsbHandle::FS_INTERNAL);
    usb.SetReceiveCallback(UsbRx, UsbHandle::FS_INTERNAL);

    hw.StartAdc();
    hw.StartAudio(AudioCallback);

    send_msg("pod ready");

    uint32_t t_report = 0, t_blink = 0, t_cpu = 0;
    float    last_k1 = -1.0f, last_k2 = -1.0f;
    bool     last_b1 = false, last_b2 = false, led_on = false;

    while(1)
    {
        HAL_IWDG_Refresh(&hiwdg);
        hw.ProcessAnalogControls();
        hw.ProcessDigitalControls();

        while(rx_tail != rx_head)
        {
            const char c = (char)rx_ring[rx_tail];
            rx_tail      = (uint16_t)((rx_tail + 1) & RING_MASK);
            parser.feed(c);
            /* handle as soon as a frame completes: a chord arrives as several
               frames back to back, and draining the whole ring first would
               overwrite the parser buffer and swallow all but one */
            if(parser.ready)
            {
                handle(parser.line);
                parser.clear();
            }
        }

        const uint32_t now = System::GetNow();

        /* control reports, 40 Hz max */
        if(now - t_report >= 25)
        {
            t_report = now;

            const float k1 = hw.GetKnobValue(DaisyPod::KNOB_1);
            const float k2 = hw.GetKnobValue(DaisyPod::KNOB_2);
            if(fabsf(k1 - last_k1) > 0.003f)
            {
                last_k1 = k1;
                send_iv('K', 1, (int)(k1 * 1000.0f + 0.5f));
            }
            if(fabsf(k2 - last_k2) > 0.003f)
            {
                last_k2 = k2;
                send_iv('K', 2, (int)(k2 * 1000.0f + 0.5f));
            }

            const bool b1 = hw.button1.Pressed();
            const bool b2 = hw.button2.Pressed();
            if(b1 != last_b1)
            {
                last_b1 = b1;
                send_iv('B', 1, b1 ? 1 : 0);
            }
            if(b2 != last_b2)
            {
                last_b2 = b2;
                send_iv('B', 2, b2 ? 1 : 0);
            }

            const int enc = hw.encoder.Increment();
            if(enc != 0)
                send_iv('E', 0, enc);
        }

        /* voices + CPU load once a second, so the voice budget is measurable */
        if(now - t_cpu >= 1000)
        {
            t_cpu = now;
            send_iv('C', synth.active_voices() + kit.Active() + piano.Active(), (int)g_cb_us);
        }

        /* LEDs: green while notes sound, blue heartbeat on the link */
        if(now - t_blink >= 500)
        {
            t_blink = now;
            led_on  = !led_on;
        }
        hw.led1.Set(0.0f, synth.active_voices() > 0 ? 0.6f : 0.0f, 0.0f);
        hw.led2.Set(0.0f, 0.0f, led_on ? 0.15f : 0.0f);
        hw.UpdateLeds();
    }
}
