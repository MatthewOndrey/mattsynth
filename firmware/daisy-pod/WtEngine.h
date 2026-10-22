// WtEngine.h - morphing wavetable engine for the Daisy Pod.
//
// Eight frames of 512 samples, built at boot by additive synthesis, held in SDRAM
// (the .bss here lands in the 64 MB region, not the 128 KB flash). A voice reads two
// neighbouring frames and crossfades, so FRAME is a continuous morph rather than a
// stepped switch. Everything downstream, the ladder filter, chorus, delay and the
// voice accounting, stays in PanelLink.cpp and is shared with the FM engine.
#pragma once

#include <cmath>
#include <cstdint>
#include <cstring>

namespace wt {

constexpr int kFrames = 8;
constexpr int kSize   = 512;
constexpr int kMax    = 12;
constexpr int kParts  = 2;

struct Params
{
    float frame  = 0.0f;
    float atk    = 0.004f;
    float dec    = 0.35f;
    float sus    = 0.75f;
    float rel    = 0.35f;
    float detune = 0.0f;
    float morph  = 0.0f;
    float morphT = 1.5f;
    float level  = 1.0f;
    /* three modulation sources per part. Each has its own target, so two of them
       can land on FRAME at different rates while the third does vibrato. */
    /* modulation matrix: each LFO carries an amount for every destination, so one
       LFO can drive several knobs at once, each with its own depth */
    static const int kDests = 14;
    struct Lfo
    {
        bool  on     = false;
        int   shape  = 0;
        int   steps  = 0;
        float rate   = 0.5f;
        float amt[kDests] = {0};
    };
    Lfo lfo[3];
    float lfoPh[3] = {0, 0, 0};   /* block-rate phase, used to sample envelope targets */

    /* per note shaping: a frame envelope, and two ways a note can sit further
       along the table than its neighbours */
    float envAmt   = 0.0f;  /* frames of offset at note-on, decays to zero */
    float envTime  = 0.2f;  /* seconds to get there */
    float velFrame = 0.0f;  /* 0..1, louder notes start brighter */
    float keyFrame = 0.0f;  /* 0..1, higher notes sit further along */
    float phaseRand = 0.0f; /* 0..1, random start phase per note */
};

struct Voice
{
    bool     active  = false;
    bool     held    = false;
    uint8_t  note    = 0;
    uint8_t  part    = 0;
    float    phase   = 0.0f;
    float    inc     = 0.0f;
    float    bend    = 1.0f;
    float    env     = 0.0f;
    int      stage   = 0;      /* 0 attack, 1 decay, 2 sustain, 3 release, 4 done */
    float    relStep = 0.0f;
    float    relSecs = 0.35f;
    float    aStep   = 0.0f, dStep = 0.0f, susLvl = 0.0f;
    float    detune  = 0.0f, level = 1.0f;
    float    lfoPhase[3] = {0.0f, 0.0f, 0.0f};
    uint8_t  pidx = 0;
    float    morphTo = 0.0f;
    float    envF = 0.0f, envFStep = 0.0f;   /* frame envelope offset */
    float    fOff = 0.0f;                    /* velocity + key tracking offset */
    uint32_t samples = 0;
    float    vel     = 1.0f;
    float    fpos    = 0.0f;   /* where this note sits in the frame axis */
    float    fstep   = 0.0f;   /* frame units per sample, for the morph */
};

class Engine
{
  public:
    void Init(float sr)
    {
        sr_ = sr;
        BuildTable();
        const float base = 8.1757989156f;             /* MIDI 0 */
        for(int n = 0; n < 128; ++n)
            hz_[n] = base * powf(2.0f, n / 12.0f);
    }

    /* every parameter belongs to a part, so A and B can be different instruments */
    Params&       prm(int p)       { return prm_[p & 1]; }
    const Params& prm(int p) const { return prm_[p & 1]; }

    void SetFrame(int part, float f)   { prm(part).frame  = f < 0.0f ? 0.0f : (f > kFrames - 1 ? kFrames - 1 : f); }
    void SetAttack(int part, float s)  { prm(part).atk    = s < 0.001f ? 0.001f : s; }
    void SetDecay(int part, float s)   { prm(part).dec    = s < 0.001f ? 0.001f : s; }
    void SetSustain(int part, float l) { prm(part).sus    = l < 0.0f ? 0.0f : (l > 1.0f ? 1.0f : l); }
    void SetRelease(int part, float s) { prm(part).rel    = s < 0.001f ? 0.001f : s; }
    void SetDetune(int part, float c)  { prm(part).detune = c; }
    void SetMorph(int part, float to, float secs) { prm(part).morph = to; prm(part).morphT = secs < 0.001f ? 0.001f : secs; }
    void SetLevel(int part, float l)   { prm(part).level  = l < 0.0f ? 0.0f : (l > 1.0f ? 1.0f : l); }

    /* overwrite part of a frame with a 16-bit single cycle: 512 samples per frame,
       sent as 16-sample chunks so a frame fits the Pod's 96-byte line buffer */
    void WriteFrame(int frame, int offset, const int16_t* d, int n)
    {
        if(frame < 0 || frame >= kFrames) return;
        for(int i = 0; i < n; ++i)
        {
            const int s = offset + i;
            if(s < 0 || s >= kSize) continue;
            table_[frame][s] = (float)d[i] / 32768.0f;
        }
        table_[frame][kSize] = table_[frame][0];
        if(offset + n >= kSize) FinishFrame(frame);
    }

    /* a loaded single cycle is raw: no band limit, so the top octaves fold harmonics
       back as inharmonic tones. Normalise it, then derive two duller copies with a
       three tap binomial, and pick the copy from the note pitch while playing. */
    void FinishFrame(int f)
    {
        float peak = 0.0f;
        for(int i = 0; i < kSize; ++i) if(fabsf(table_[f][i]) > peak) peak = fabsf(table_[f][i]);
        const float g = peak > 0.001f ? 0.95f / peak : 1.0f;
        if(g != 1.0f) for(int i = 0; i < kSize; ++i) table_[f][i] *= g;
        table_[f][kSize] = table_[f][0];

        for(int i = 0; i < kSize; ++i)
        {
            const int a = (i + kSize - 1) % kSize, b = i, c = (i + 1) % kSize;
            mip1_[f][i] = 0.25f * table_[f][a] + 0.5f * table_[f][b] + 0.25f * table_[f][c];
        }
        mip1_[f][kSize] = mip1_[f][0];
        for(int i = 0; i < kSize; ++i)
        {
            const int a = (i + kSize - 1) % kSize, b = i, c = (i + 1) % kSize;
            mip2_[f][i] = 0.25f * mip1_[f][a] + 0.5f * mip1_[f][b] + 0.25f * mip1_[f][c];
        }
        mip2_[f][kSize] = mip2_[f][0];
        if(f == kFrames - 1) MatchSetLevels();
    }

    /* the eight frames of a set should morph at a similar loudness, not jump */
    void MatchSetLevels()
    {
        float rms[kFrames] = {0}, sum = 0.0f;
        int nf = 0;
        for(int f = 0; f < kFrames; ++f)
        {
            float acc = 0.0f;
            for(int i = 0; i < kSize; ++i) acc += table_[f][i] * table_[f][i];
            rms[f] = sqrtf(acc / kSize);
            if(rms[f] > 0.0005f) { sum += rms[f]; ++nf; }
        }
        if(!nf) return;
        const float target = sum / nf;
        for(int f = 0; f < kFrames; ++f)
        {
            if(rms[f] < 0.0005f) continue;
            float g = target / rms[f];
            if(g < 0.5f) g = 0.5f;
            if(g > 2.0f) g = 2.0f;
            for(int i = 0; i <= kSize; ++i)
            {
                table_[f][i] *= g;
                mip1_[f][i]  *= g;
                mip2_[f][i]  *= g;
            }
        }
    }

    bool FrameLoaded(int frame) const { return frame >= 0 && frame < kFrames; }
    void SetMaxVoices(int n) { max_voices_ = n < 1 ? 1 : (n > kMax ? kMax : n); }
    int  max_voices() const  { return max_voices_; }
    int  active_voices() const
    {
        int n = 0;
        for(int i = 0; i < kMax; ++i) if(v_[i].active) ++n;
        return n;
    }

    void NoteOn(uint8_t note, uint8_t vel, uint8_t part)
    {
        Voice* v = alloc(part);
        if(!v) return;
        v->active = true; v->held = true;
        v->note = note; v->part = part;
        v->phase = 0.0f;
        v->stage = 0;
        v->env   = 0.0f;
        v->bend  = 1.0f;
        v->samples = 0;
        const Params& p = prm(part);
        v->inc    = hz_[note] / sr_;
        v->vel    = 0.25f + 0.75f * (vel / 127.0f);
        /* envelope destinations are sampled at note-on from the block-rate phase */
        float mAtk = 0, mDec = 0, mSus = 0, mRel = 0, mEnv = 0, mEnvT = 0, mVel = 0, mKey = 0, mRnd = 0, mMorphT = 0;
        for(int li = 0; li < 3; ++li)
        {
            const Params::Lfo& L = p.lfo[li];
            if(!L.on) continue;
            const float ph = p.lfoPh[li];
            float w = 0.0f;
            switch(L.shape)
            {
                case 1: w = ph < 0.5f ? (ph * 4.0f - 1.0f) : (3.0f - ph * 4.0f); break;
                case 2: w = ph * 2.0f - 1.0f; break;
                case 3: w = ph < 0.5f ? 1.0f : -1.0f; break;
                case 4: w = 0.0f; break;
                default: w = sinf(ph * 6.2831853f); break;
            }
            mAtk += w * L.amt[4]; mDec += w * L.amt[5]; mSus += w * L.amt[6]; mRel += w * L.amt[7];
            mEnv += w * L.amt[8]; mEnvT += w * L.amt[9]; mVel += w * L.amt[10];
            mKey += w * L.amt[11]; mRnd += w * L.amt[12]; mMorphT += w * L.amt[13];
        }
        const auto clamp01 = [](float x){ return x < 0.0f ? 0.0f : (x > 1.0f ? 1.0f : x); };
        /* the patch value is the base; modulation scales it, it does not replace it */
        const float atkS = fmaxf(0.002f, p.atk * (1.0f + mAtk));
        const float decS = fmaxf(0.005f, p.dec * (1.0f + mDec));
        const float relS = fmaxf(0.005f, p.rel * (1.0f + mRel));

        v->aStep  = 1.0f / (atkS * sr_);
        v->susLvl = clamp01(p.sus + mSus * 0.5f);
        v->dStep  = (1.0f - v->susLvl) / (decS * sr_);
        v->detune = p.detune;
        v->level  = p.level;
        v->relSecs = relS;
        v->fpos   = p.frame;
        v->fstep  = (fabsf(p.morph - p.frame) > 0.001f)
                  ? (p.morph - p.frame) / (p.morphT * sr_) : 0.0f;
        v->pidx      = part & 1;
        v->lfoPhase[0] = v->lfoPhase[1] = v->lfoPhase[2] = 0.0f;
        v->morphTo   = p.morph;
        v->envF      = p.envAmt;
        v->envFStep  = (fabsf(p.envAmt) > 0.001f) ? p.envAmt / (p.envTime * sr_) : 0.0f;
        /* velocity and key tracking push the starting frame along the table */
        v->fOff  = p.velFrame * ((vel / 127.0f) - 0.5f) * (kFrames - 1);
        v->fOff += p.keyFrame * ((note - 60.0f) / 24.0f) * (kFrames - 1);
        if(p.phaseRand > 0.0f)
        {
            static uint32_t seed = 12345;
            seed = seed * 1664525u + 1013904223u;
            v->phase = ((seed >> 8) & 1023) / 1024.0f * p.phaseRand;
        }
    }

    void NoteOff(uint8_t note, int cents = -1)
    {
        (void)cents;
        for(int i = 0; i < max_voices_; ++i)
            if(v_[i].active && v_[i].held && v_[i].note == note)
            {
                v_[i].held  = false;
                v_[i].stage = 3;
                v_[i].relStep = v_[i].env / (prm(v_[i].part).rel * sr_);
            }
    }

    void KillNote(uint8_t note)
    {
        for(int i = 0; i < max_voices_; ++i)
            if(v_[i].active && v_[i].note == note){ v_[i].active = false; v_[i].held = false; }
    }

    void AllOff()
    {
        for(int i = 0; i < kMax; ++i){ v_[i].active = false; v_[i].held = false; }
    }

    void Bend(int part, int note, float cents)
    {
        const float m = powf(2.0f, cents / 1200.0f);
        for(int i = 0; i < kMax; ++i)
            if(v_[i].active && v_[i].part == part && (note < 0 || v_[i].note == note))
                v_[i].bend = m;
    }

    void Render(float* l, float* r, size_t n)
    {
        for(int p = 0; p < kParts; ++p)
            for(int li = 0; li < 3; ++li)
            {
                float& ph = prm_[p].lfoPh[li];
                ph += prm_[p].lfo[li].rate * (float)n / sr_;
                if(ph >= 1.0f) ph -= 1.0f;
            }

        for(size_t s = 0; s < n; ++s)
        {
            float mix = 0.0f;
            for(int i = 0; i < max_voices_; ++i)
            {
                Voice& v = v_[i];
                if(!v.active) continue;

                /* envelope */
                switch(v.stage)
                {
                    case 0: v.env += v.aStep; if(v.env >= 1.0f){ v.env = 1.0f; v.stage = 1; } break;
                    case 1: v.env -= v.dStep; if(v.env <= v.susLvl){ v.env = v.susLvl; v.stage = 2; } break;
                    case 2: break;
                    case 3: v.env -= v.relStep; if(v.env <= 0.0f){ v.env = 0.0f; v.active = false; } break;
                    default: v.active = false; break;
                }
                if(!v.active) continue;

                /* two-frame crossfade, linear interpolation inside each frame */
                v.fpos += v.fstep;
                if(v.fpos < 0.0f) v.fpos = 0.0f;
                if(v.fpos > kFrames - 1) v.fpos = kFrames - 1;
                /* three LFOs, evaluated per sample; their contributions add up */
                float fLfo = 0.0f, mrmorph = 0.0f, detLfo = 0.0f, lvlLfo = 0.0f;
                {
                    const Params& pp = prm_[v.pidx];
                    for(int li = 0; li < 3; ++li)
                    {
                        const Params::Lfo& L = pp.lfo[li];
                        if(!L.on) continue;
                        v.lfoPhase[li] += L.rate / sr_;
                        if(v.lfoPhase[li] >= 1.0f) v.lfoPhase[li] -= 1.0f;
                        const float lp = v.lfoPhase[li];
                        float w = 0.0f;
                        switch(L.shape)
                        {
                            case 1: w = lp < 0.5f ? (lp * 4.0f - 1.0f) : (3.0f - lp * 4.0f); break;
                            case 2: w = lp * 2.0f - 1.0f; break;
                            case 3: w = lp < 0.5f ? 1.0f : -1.0f; break;
                            case 4:
                            {
                                static uint32_t shC[3] = {0, 0, 0};
                                static float    shH[3] = {0, 0, 0}, shL[3] = {0, 0, 0};
                                if(lp < shL[li])
                                {
                                    ++shC[li];
                                    shH[li] = (float)(((shC[li] * 1103515245u) >> 9) & 1023) / 511.5f - 1.0f;
                                }
                                shL[li] = lp;
                                w = shH[li];
                                break;
                            }
                            default: w = sinf(lp * 6.2831853f); break;
                        }
                        if(L.steps > 1)
                        {
                            const float q = (L.steps - 1) * 0.5f;
                            w = roundf(w * q) / q;
                        }
                        fLfo    += w * L.amt[0] * (kFrames - 1) * 0.5f;
                        mrmorph += w * L.amt[1] * 0.5f;
                        detLfo  += w * L.amt[2] * 50.0f;
                        lvlLfo  += w * L.amt[3] * 0.5f;
                    }
                }

                float fpos = v.fpos + v.envF + v.fOff + fLfo;
                if(mrmorph != 0.0f) fpos += (v.morphTo - v.fpos) * mrmorph;
                if(fpos < 0.0f) fpos = 0.0f;
                if(fpos > kFrames - 1) fpos = kFrames - 1;

                const float fp   = fpos;
                const int   f0   = (int)fp;
                const int   f1   = f0 + 1 < kFrames ? f0 + 1 : f0;
                const float fMix = fp - f0;

                float ph = v.phase;
                const float x  = ph * kSize;
                const int   i0 = (int)x;
                const float xf = x - i0;

                /* pick the copy that suits this note: raw below 900 Hz, one octave
                   down to 2.2 kHz, two octaves above that */
                const float hzNow = v.inc * sr_;
                const float (*tab)[kSize + 1] = hzNow > 2200.0f ? mip2_
                                               : (hzNow > 900.0f ? mip1_ : table_);
                const float* A = tab[f0];
                const float* B = tab[f1];
                const float a  = A[i0] + (A[i0 + 1] - A[i0]) * xf;
                const float b  = B[i0] + (B[i0 + 1] - B[i0]) * xf;
                const float smp = (a + (b - a) * fMix) * v.env * v.vel * v.level;

                mix += smp;

                v.phase += v.inc * v.bend * (1.0f + v.detune * 0.0006f)
                         * powf(2.0f, detLfo / 1200.0f);
                ++v.samples;
                if(v.phase >= 1.0f) v.phase -= 1.0f;
            }
            l[s] = mix * 0.6f;
            r[s] = mix * 0.6f;
        }
    }

  private:
    Voice* alloc(uint8_t part)
    {
        for(int i = 0; i < max_voices_; ++i)
            if(!v_[i].active){ v_[i].part = part; return &v_[i]; }
        /* steal the oldest release first, then the oldest note of the same part */
        int best = -1; uint32_t oldest = 0xffffffffu;
        for(int i = 0; i < max_voices_; ++i)
        {
            const Voice& c = v_[i];
            if(!c.held && c.samples < oldest){ oldest = c.samples; best = i; }
        }
        if(best < 0)
            for(int i = 0; i < max_voices_; ++i)
                if(v_[i].part == part && v_[i].samples < oldest){ oldest = v_[i].samples; best = i; }
        if(best < 0) best = 0;
        v_[best].active = false;
        return &v_[best];
    }

    /* eight frames: sine, soft second, odd harmonics, saw, formant, bell, hollow, glass */
    void BuildTable()
    {
        static const float harm[kFrames][8] = {
            {1.00f, 0.00f, 0.00f, 0.00f, 0.00f, 0.00f, 0.00f, 0.00f},
            {1.00f, 0.35f, 0.00f, 0.00f, 0.00f, 0.00f, 0.00f, 0.00f},
            {1.00f, 0.00f, 0.33f, 0.00f, 0.20f, 0.00f, 0.14f, 0.00f},
            {1.00f, 0.50f, 0.33f, 0.25f, 0.20f, 0.17f, 0.14f, 0.12f},
            {0.30f, 0.00f, 0.90f, 0.00f, 0.00f, 0.45f, 0.00f, 0.00f},
            {1.00f, 0.00f, 0.55f, 0.00f, 0.00f, 0.00f, 0.35f, 0.00f},
            {1.00f, 0.00f, 0.00f, 0.22f, 0.00f, 0.00f, 0.00f, 0.12f},
            {0.60f, 0.30f, 0.45f, 0.20f, 0.35f, 0.15f, 0.25f, 0.18f},
        };
        for(int f = 0; f < kFrames; ++f)
        {
            float peak = 0.0f;
            for(int i = 0; i < kSize; ++i)
            {
                float a = 0.0f;
                const float th = 6.2831853f * i / kSize;
                for(int h = 0; h < 8; ++h)
                    if(harm[f][h] > 0.0f) a += harm[f][h] * sinf(th * (h + 1));
                table_[f][i] = a;
                if(fabsf(a) > peak) peak = fabsf(a);
            }
            if(peak > 0.0f)
                for(int i = 0; i < kSize; ++i) table_[f][i] /= peak;
            table_[f][kSize] = table_[f][0];   /* wrap sample for interpolation */
            for(int i = 0; i < kSize; ++i)
            {
                const int a = (i + kSize - 1) % kSize, b = i, c = (i + 1) % kSize;
                mip1_[f][i] = 0.25f * table_[f][a] + 0.5f * table_[f][b] + 0.25f * table_[f][c];
            }
            mip1_[f][kSize] = mip1_[f][0];
            for(int i = 0; i < kSize; ++i)
            {
                const int a = (i + kSize - 1) % kSize, b = i, c = (i + 1) % kSize;
                mip2_[f][i] = 0.25f * mip1_[f][a] + 0.5f * mip1_[f][b] + 0.25f * mip1_[f][c];
            }
            mip2_[f][kSize] = mip2_[f][0];
        }
    }

    Voice  v_[kMax];
    Params prm_[kParts];
    float  table_[kFrames][kSize + 1];
    float  mip1_[kFrames][kSize + 1];
    float  mip2_[kFrames][kSize + 1];
    float  hz_[128] = {0};
    float  sr_ = 48000.0f;
    int    max_voices_ = 8;
};

} // namespace wt
