// FmEngine.h - 12-voice, 6-operator, 32-algorithm FM engine for the Daisy Pod.
//
// Routing mirrors the DX7 hardware: two modulation buses plus the voice output,
// each operator reading one bus and writing another. That is the same topology
// the algorithm table in fm_patch.h encodes, so all 32 algorithms come for free
// and modulators always run before the operators they feed (op[0]..op[5]).
//
// Per operator: 4-segment DX7 envelope (R1-R4 / L1-L4), output level, coarse /
// fine ratio or fixed frequency, detune, velocity sensitivity.
// Not implemented yet: LFO, pitch EG, keyboard level scaling curves, amp mod.
//
// Header only, so the Pod Makefile needs no new object file.

#pragma once

#include <math.h>
#include <stdint.h>
#include <string.h>

#include "fm_patch.h"

namespace fmsynth {

static const int kVoices   = 12;
static const int kOps      = 6;
/* two independent timbres sharing one voice pool: each voice carries its part */
static const int kParts    = 2;
static const int kSineBits = 10;
static const int kSineSize = 1 << kSineBits;
static const int kOutGain  = 1; // placeholder, real gain below

struct OpCfg
{
    fm::Route route;
    float     level_amp;  // output level, 0..1
    float     ratio;      // frequency multiplier (or fixed Hz in `fixed`)
    float     fixed_hz;
    bool      fixed;
    float     k[4];       // envelope coefficients
    float     lvl[4];     // envelope targets, 0..1
    float     vel_sens;   // 0..1
    float     break_point;
    float     left_db;
    float     right_db;
};

struct Voice
{
    bool     active;
    bool     held;
    uint8_t  note;
    uint32_t age;
    float    inc[kOps];
    float    amp[kOps];
    float    phase[kOps];
    float    eg[kOps];
    float    fb[kOps];
    uint8_t  stage[kOps];
    float    pitch_eg;     /* current pitch offset, semitones               */
    float    pitch_k[4];
    float    pitch_lvl[4];
    uint8_t  pitch_stage;
    uint32_t samples;      /* since note-on, for the LFO delay              */
    float    pan_l;        /* constant-power pan gains                      */
    float    pan_r;
    uint8_t  part;         /* which timbre this voice plays                 */
    float    bend;         /* live pitch offset, 1.0 = as written            */
    int16_t  cents;        /* the offset this note was started with, for O matching */
    uint32_t rel_at;       /* sample count at key-off, for the watchdog      */
};

static inline int clampi16(int v)
{
    return v < -3200 ? -3200 : (v > 3200 ? 3200 : v);
}

class Engine
{
  public:
    void Init(float sample_rate)
    {
        sr_ = sample_rate;
        for(int i = 0; i <= kSineSize; ++i)
            sine_[i] = sinf((float)i * 6.28318530718f / (float)kSineSize);
        for(int i = 0; i < kVoices; ++i)
            voices_[i].active = false;
        age_      = 0;
        for(int p = 0; p < kParts; ++p)
            fb_gain_[p] = 0.5f;
        mod_scale_ = 1.0f;
        memset(patch_, 0, sizeof(patch_));
    }

    void SetSampleRate(float sr) { sr_ = sr; }

    /* runtime voice limit: lets the host trade polyphony for CPU headroom */
    void SetMaxVoices(int n)
    {
        max_voices_ = n < 1 ? 1 : (n > kVoices ? kVoices : n);
    }
    int max_voices() const { return max_voices_; }
    /* the audio callback reports its own duration so the allocator can stop
       growing the pool when a block is nearly full: an overrunning callback is
       heard as buzz, not as a clean dropout. */
    void SetLoad(int us) { load_us_ = us > 0 ? us : 0; }

    void SetPatch(const fm::Patch& p, int part = 0)
    {
        if(part < 0 || part >= kParts)
            part = 0;
        patch_[part] = p;
        for(int i = 0; i < kOps; ++i)
        {
            const fm::Op& o = p.op[i];
            OpCfg&        c = cfg_[part][i];
            c.route         = fm::route(p.algorithm, i);
            c.level_amp     = level_amp(o.output_level);
            c.fixed         = o.osc_mode != 0;
            c.ratio         = (o.freq_coarse == 0) ? 0.5f : (float)o.freq_coarse;
            c.ratio += (float)o.freq_fine * 0.01f;
            const float cents = ((float)o.detune - 7.0f) * 1.5f;
            c.ratio *= powf(2.0f, cents / 1200.0f);
            c.fixed_hz = 10.0f * powf(2.0f, (float)o.freq_coarse - 1.0f
                                               + (float)o.freq_fine * 0.01f);
            c.vel_sens = (float)o.key_vel_sens / 7.0f;
            for(int s = 0; s < 4; ++s)
            {
                c.k[s]   = rate_coef(o.rate[s], s == 0, s == 3);
                c.lvl[s] = level_norm(o.level[s]);
            }
            /* keyboard level scaling: linear in dB either side of the break
               point. The DX7 curve types are ignored, which is audible only on
               extreme -EXP/+EXP settings. */
            c.break_point = (float)o.break_point;
            c.left_db     = (float)o.left_depth * 0.5f;
            c.right_db    = (float)o.right_depth * 0.5f;
        }
        fb_gain_[part] = (float)(p.feedback & 7) / 7.0f * 0.85f;

        /* LFO is shared by both parts, taken from whichever patch was set last */

        /* LFO */
        lfo_wave_  = p.lfo_wave % 6;
        lfo_inc_   = (0.05f + (float)p.lfo_speed * (float)p.lfo_speed * 0.005f) / sr_;
        lfo_delay_ = (float)p.lfo_delay * 0.05f * sr_;
        pmd_       = (float)p.lfo_pmd / 99.0f * 0.03f; /* up to ~3 % pitch      */
        amd_       = (float)p.lfo_amd / 99.0f * 0.55f; /* up to ~55 % amplitude */
        /* phase is deliberately NOT reset here: SetPatch runs on every S verb, so
           resetting it would freeze the LFO while a hand or a modulator edits the
           patch live. Init() still starts it at zero. */
    }

    /* tune a sounding note (or every note of a part when note < 0) by cents */
    void Bend(int part, int note, float cents)
    {
        if(part < 0 || part >= kParts)
            return;
        if(cents > 2400.0f)  cents = 2400.0f;
        if(cents < -2400.0f) cents = -2400.0f;
        const float f = powf(2.0f, cents / 1200.0f);
        for(int i = 0; i < kVoices; ++i)
        {
            Voice& v = voices_[i];
            if(!v.active || v.part != part)
                continue;
            if(note >= 0 && v.note != (uint8_t)note)
                continue;
            v.bend = f;
        }
    }

    void SetFeedback(float f01, int part = 0)
    {
        if(part < 0 || part >= kParts)
            part = 0;
        fb_gain_[part] = (f01 < 0.f ? 0.f : f01 > 1.f ? 1.f : f01) * 0.85f;
    }

    void NoteOn(uint8_t note, uint8_t vel, int part = 0, int cents = 0)
    {
        if(part < 0 || part >= kParts)
            part = 0;
        Voice* v = alloc(part);
        v->active = true;
        v->held   = true;
        v->note   = note;
        v->part   = (uint8_t)part;
        v->cents  = (int16_t)clampi16(cents);
        v->bend   = powf(2.0f, (float)v->cents / 1200.0f);
        v->rel_at = 0;
        v->age    = ++age_;

        const float hz = 440.0f * powf(2.0f, ((float)note - 69.0f) / 12.0f);
        const float vn = (float)vel / 127.0f;

        /* pitch envelope: levels mapped to +/-2 semitones, where the linear
           ratio approximation below stays within ~10 cents of exact */
        for(int s = 0; s < 4; ++s)
        {
            v->pitch_k[s]   = rate_coef(patch_[part].pitch_rate[s], s == 0);
            v->pitch_lvl[s] = ((float)patch_[part].pitch_level[s] - 50.0f) / 50.0f * 2.0f;
        }
        v->pitch_eg    = 0.0f;
        v->pitch_stage = 1;
        v->samples     = 0;

        /* constant-power pan: patch pan plus a deterministic per-note fan so a
           chord spreads instead of stacking in the middle */
        float pan = ((float)patch_[part].pan - 50.0f) / 50.0f;
        if(pan < -1.0f)
            pan = -1.0f;
        if(pan > 1.0f)
            pan = 1.0f;
        const float spread = (float)patch_[part].spread / 99.0f;
        const float jitter = ((float)((note * 7) % 11) - 5.0f) / 5.0f;
        pan += spread * jitter * 0.9f;
        if(pan < -1.0f)
            pan = -1.0f;
        if(pan > 1.0f)
            pan = 1.0f;
        const float ang = (pan + 1.0f) * 0.25f * 3.14159265f;
        v->pan_l        = cosf(ang);
        v->pan_r        = sinf(ang);

        for(int i = 0; i < kOps; ++i)
        {
            const OpCfg& c = cfg_[part][i];
            /* keyboard level scaling: dB attenuation per octave away from the
               operator's break point */
            const float octs = ((float)note - c.break_point) / 12.0f;
            const float db   = octs < 0.0f ? (c.left_db * -octs) : (c.right_db * octs);
            const float ks   = powf(10.0f, -db / 20.0f);

            v->inc[i]   = (c.fixed ? c.fixed_hz : hz * c.ratio) / sr_;
            v->amp[i]   = c.level_amp * ks * (1.0f - c.vel_sens + c.vel_sens * vn);
            v->phase[i] = 0.0f;
            v->eg[i]    = 0.0f;
            v->fb[i]    = 0.0f;
            v->stage[i] = 1; // attack
        }
    }

    /* cents < 0 releases every voice of that note; otherwise only the voice that
       was started with exactly that offset, so 24-EDO quarter tones sharing a note
       number release independently */
    void NoteOff(uint8_t note, int cents = -1)
    {
        for(int i = 0; i < kVoices; ++i)
        {
            Voice& v = voices_[i];
            if(v.active && v.held && v.note == note && (cents < 0 || v.cents == (int16_t)cents))
            {
                v.held = false;
                v.rel_at = v.samples;
                if(v.pitch_stage < 4)
                    v.pitch_stage = 4;
                for(int o = 0; o < kOps; ++o)
                    if(v.stage[o] < 4)
                        v.stage[o] = 4; // release
            }
        }
    }

    /* hard stop for one note: releases take seconds on a slow patch, and a
       held-note button wants silence now, not a tail */
    void KillNote(uint8_t note)
    {
        for(int i = 0; i < kVoices; ++i)
        {
            Voice& v = voices_[i];
            if(v.active && v.note == note)
            {
                v.active = false;
                v.held   = false;
                for(int o = 0; o < kOps; ++o)
                    v.fb[o] = 0.0f;
            }
        }
    }

    void AllOff()
    {
        /* PANIC: hard stop. A release would leave anything with a wedged
           envelope holding its slot, which is how the pool fills up for good. */
        for(int i = 0; i < kVoices; ++i)
        {
            Voice& v   = voices_[i];
            v.active   = false;
            v.held     = false;
            v.fb[0] = v.fb[1] = v.fb[2] = v.fb[3] = v.fb[4] = v.fb[5] = 0.0f;
        }
    }

    void Render(float* outL, float* outR, size_t n)
    {
        if(n > 64)
            n = 64;

        /* LFO once per block: every voice reads the same buffer */
        for(size_t s = 0; s < n; ++s)
            lfo_buf_[s] = lfo_step();

        for(size_t s = 0; s < n; ++s)
        {
            outL[s] = 0.0f;
            outR[s] = 0.0f;
        }

        /* voice outer, sample inner: one voice's state stays hot for the whole
           block instead of being reloaded once per sample */
        for(int vi = 0; vi < kVoices; ++vi)
        {
            Voice& v = voices_[vi];
            if(!v.active)
                continue;

            if(!v.held && v.rel_at && v.samples > v.rel_at + (uint32_t)(sr_ * 25.0f))
            {
                v.active = false;      /* watchdog: released but never went silent */
                continue;
            }

            const OpCfg* pcfg = cfg_[v.part];
            const float  fbg  = fb_gain_[v.part];
            bool silent = true;

            for(size_t s = 0; s < n; ++s)
            {
                float bus[3] = {0.0f, 0.0f, 0.0f};
                float outs   = 0.0f;

                const float semis = pitch_step(v);
                const float pr    = (1.0f + semis * 0.05776f) * v.bend; /* ln2/12 per semitone */
                const float lfo   = (v.samples >= (uint32_t)lfo_delay_) ? lfo_buf_[s] : 0.0f;
                const float pmod  = 1.0f + pmd_ * lfo;

                for(int i = 0; i < kOps; ++i)
                {
                    const OpCfg& c = pcfg[i];
                    const float  e = eg_step(v, i, c);
                    if(e > 0.0f)
                        silent = false;

                    float mod = c.route.in_bus ? bus[c.route.in_bus] : 0.0f;
                    if(c.route.fb)
                        mod += v.fb[i];

                    const float sm = sine_turns(v.phase[i] + mod * mod_scale_) * e * v.amp[i];

                    if(c.route.out_bus == 0)
                    {
                        if(c.route.add)
                            outs += sm;
                        else
                            outs = sm;
                    }
                    else
                    {
                        if(c.route.add)
                            bus[c.route.out_bus] += sm;
                        else
                            bus[c.route.out_bus] = sm;
                    }
                    if(c.route.fb)
                        v.fb[i] = (v.fb[i] + sm) * 0.5f * fbg;

                    v.phase[i] += v.inc[i] * pr * pmod;
                }
                outL[s] += outs * (1.0f + amd_ * lfo) * v.pan_l;
                outR[s] += outs * (1.0f + amd_ * lfo) * v.pan_r;
                v.samples++;
            }
            if(silent)
                v.active = false;
        }

        for(size_t s = 0; s < n; ++s)
        {
            outL[s] = soft_clip(outL[s] * 0.18f) * 0.9f;
            outR[s] = soft_clip(outR[s] * 0.18f) * 0.9f;
        }
    }

    const fm::Patch& patch() const { return patch_[0]; }
    const fm::Patch& patch(int part) const { return patch_[part & (kParts - 1)]; }
    int              active_voices() const
    {
        int n = 0;
        for(int i = 0; i < kVoices; ++i)
            n += voices_[i].active ? 1 : 0;
        return n;
    }

  private:
    float  sine_[kSineSize + 1];
    float  sr_       = 48000.0f;
    float  fb_gain_[kParts] = {0.5f, 0.5f};
    float  mod_scale_ = 1.0f;
    uint32_t age_    = 0;
    int      max_voices_ = 8; /* kVoices = 12 is the hard cap, but measured with a full patch:
                                8 voices 782us, 10 voices 945us of the 1000us block. 10 is
                                already past the 850us steal threshold, so 8 is the honest
                                default and P 5 10/12 is for a light patch only */
    int      load_us_ = 0;    /* duration of the last rendered block */
    OpCfg  cfg_[kParts][kOps];
    Voice  voices_[kVoices];
    fm::Patch patch_[kParts];

    /* LFO, shared by all voices */
    float    lfo_phase_ = 0.0f;
    float    lfo_inc_   = 0.0f;
    float    lfo_delay_ = 0.0f;
    int      lfo_wave_  = 0;
    float    pmd_       = 0.0f;
    float    amd_       = 0.0f;
    float    lfo_buf_[128];
    float    lfo_val_   = 0.0f;
    uint32_t lfo_state_ = 22222;

    /* EG levels are dB like the DX7, so decays sound exponential not linear */
    static float level_norm(uint8_t l)
    {
        if(l == 0)
            return 0.0f;
        return powf(10.0f, ((float)l - 99.0f) * 0.6f / 20.0f);
    }

    /* output level 0..99 -> amplitude; 0.75 dB per step, 99 = unity */
    static float level_amp(uint8_t l)
    {
        return powf(10.0f, ((float)l - 99.0f) * 0.75f / 20.0f);
    }

    /* DX7 rate 0..99 -> one-pole coefficient; 99 is ~6 ms, 0 is ~19 s.
       The release stage is capped: an uncapped tail keeps the voice 'active'
       for minutes, so a handful of slow pads fills the pool and panic cannot
       free it. Capped, the worst case frees its slot in about 14 s. */
    float rate_coef(uint8_t r, bool attack, bool release = false) const
    {
        float t = 0.006f * powf(2.0f, (99.0f - (float)r) / 8.5f);
        if(release && t > 2.5f)
            t = 2.5f;
        if(attack)
            t *= 0.2f; /* a one-pole needs several time constants to arrive */
        float k = 1.0f - expf(-1.0f / (t * sr_));
        return k > 1.0f ? 1.0f : k;
    }

    float sine_turns(float t) const
    {
        /* +1024 cycles keeps the argument positive, so the wrap is an integer
           truncation instead of a floorf call; ~5 cycles instead of ~100. */
        const float x    = (t + 1024.0f) * (float)kSineSize;
        const uint32_t i = (uint32_t)x;
        const float frac = x - (float)i;
        const int   a    = (int)(i & (kSineSize - 1));
        return sine_[a] + (sine_[a + 1] - sine_[a]) * frac;
    }

    /* Pade tanh approximation: ~10 cycles, indistinguishable at this level */
    static float soft_clip(float x)
    {
        const float x2 = x * x;
        return x * (27.0f + x2) / (27.0f + 9.0f * x2);
    }

    float lfo_step()
    {
        lfo_phase_ += lfo_inc_;
        if(lfo_phase_ >= 1.0f)
        {
            lfo_phase_ -= 1.0f;
            lfo_state_ = lfo_state_ * 1103515245u + 12345u;
            lfo_val_   = (float)((lfo_state_ >> 16) & 0x7FFF) / 16384.0f - 1.0f;
        }
        const float t = lfo_phase_;
        switch(lfo_wave_)
        {
            case 1: return 1.0f - 2.0f * t;                /* saw down      */
            case 2: return 2.0f * t - 1.0f;                /* saw up        */
            case 3: return t < 0.5f ? 1.0f : -1.0f;        /* square        */
            case 4: return sinf(t * 6.2831853f);           /* sine          */
            case 5: return lfo_val_;                       /* sample & hold */
            default: return 1.0f - 4.0f * fabsf(t - 0.5f); /* triangle      */
        }
    }

    /* pitch envelope, +/-2 semitones; step per sample, cheap */
    float pitch_step(Voice& v)
    {
        switch(v.pitch_stage)
        {
            case 1:
                v.pitch_eg += (v.pitch_lvl[0] - v.pitch_eg) * v.pitch_k[0];
                if(fabsf(v.pitch_eg - v.pitch_lvl[0]) < 0.01f)
                {
                    v.pitch_eg    = v.pitch_lvl[0];
                    v.pitch_stage = 2;
                }
                break;
            case 2:
                v.pitch_eg += (v.pitch_lvl[1] - v.pitch_eg) * v.pitch_k[1];
                if(fabsf(v.pitch_eg - v.pitch_lvl[1]) < 0.01f)
                {
                    v.pitch_eg    = v.pitch_lvl[1];
                    v.pitch_stage = 3;
                }
                break;
            case 3: v.pitch_eg += (v.pitch_lvl[2] - v.pitch_eg) * v.pitch_k[2]; break;
            case 4: v.pitch_eg += (v.pitch_lvl[3] - v.pitch_eg) * v.pitch_k[3]; break;
            default: break;
        }
        return v.pitch_eg;
    }

    static float eg_step(Voice& v, int i, const OpCfg& c)
    {
        uint8_t& st = v.stage[i];
        float&   e  = v.eg[i];
        switch(st)
        {
            case 1: /* attack to L1 */
                e += (c.lvl[0] - e) * c.k[0];
                if(e >= c.lvl[0] - 0.001f)
                {
                    e  = c.lvl[0];
                    st = 2;
                }
                break;
            case 2: /* decay to L2 */
                e += (c.lvl[1] - e) * c.k[1];
                if(e <= c.lvl[1] + 0.001f)
                {
                    e  = c.lvl[1];
                    st = 3;
                }
                break;
            case 3: /* sustain at L3 */
                e += (c.lvl[2] - e) * c.k[2];
                if(e <= c.lvl[2] + 0.001f)
                    e = c.lvl[2];
                break;
            case 4: /* release: always to zero
                       (the DX7 stops at L4; we stop at silence so the voice can
                       be reused, otherwise any patch with L4 > 0 holds a slot
                       forever and the pool fills with inaudible drones) */
                e -= e * c.k[3];
                if(e <= 0.004f)
                {
                    e  = 0.0f;
                    st = 5;
                }
                break;
            default:
                e  = 0.0f;
                st = 5;
                break;
        }
        return e;
    }

    Voice* alloc(int part)
    {
        /* tight only stops the pool GROWING: an empty pool still takes the note */
        int active = 0;
        for(int i = 0; i < max_voices_; ++i)
            if(voices_[i].active)
                ++active;
        const bool tight = (load_us_ > 850) && active > 0;  /* 48-frame block is 1000 us */
        Voice* best     = nullptr;
        Voice* released = nullptr;
        Voice* same     = nullptr;
        Voice* sameRel  = nullptr;
        for(int i = 0; i < max_voices_; ++i)
        {
            Voice& v = voices_[i];
            if(!v.active)
            {
                if(!tight)
                    return &v;
                continue;   /* tight: steal the oldest instead of adding a voice */
            }
            if(!v.held && (!released || v.age < released->age))
                released = &v;
            if(!best || v.age < best->age)
                best = &v;
            /* prefer to steal from the same part, so a busy part cannot eat the
               other part's voices while it is still holding notes */
            if(v.part == part)
            {
                if(!v.held && (!sameRel || v.age < sameRel->age))
                    sameRel = &v;
                if(!same || v.age < same->age)
                    same = &v;
            }
        }
        if(sameRel)
            return sameRel;
        if(released)
            return released;
        if(same)
            return same;
        return best;
    }
};

} // namespace fmsynth
