/* PianoEngine — a real sampled piano across the keyboard.

   Five recorded roots (Salamander, CC-BY), each 2.4 seconds, held in SDRAM. A note picks
   the nearest root and plays it at 2^((note - root)/12), which is the ordinary way a
   sampler covers a keyboard without shipping one file per note. */
#pragma once
#include <stdint.h>
#include <string.h>
#include <math.h>
#include "piano.h"

#ifndef DSY_SDRAM_BSS
#define DSY_SDRAM_BSS __attribute__((section(".sdram_bss")))
#endif

DSY_SDRAM_BSS static int16_t g_pno_buf[600000];

class PianoEngine
{
  public:
    static const int kVoices = 12;

    void Init()
    {
        uint32_t at = 0;
        for(int i = 0; i < 5; i++)
        {
            off_[i] = at;
            len_[i] = kPiano[i].len;
            if(at + kPiano[i].len <= 600000)
                memcpy(&g_pno_buf[at], kPiano[i].data, kPiano[i].len * 2);
            at += kPiano[i].len;
        }
        for(int i = 0; i < kVoices; i++) v_[i].on = false;
    }

    void Trigger(int note, int vel, int cents = 0)
    {
        if(note < 12 || note > 120) return;
        int best = 0, dist = 999;
        const int nSlots = zoneCount_ > 0 ? zoneCount_ : 5;
        for(int i = 0; i < nSlots; i++)
        {
            const int root = (zoneCount_ > 0) ? zoneRoot_[i] : (arenaRoot >= 0 ? arenaRoot : kPiano[i].note);
            const int d = abs(note - root);
            if(d < dist) { dist = d; best = i; }
        }
        int pick = -1;
        for(int i = 0; i < kVoices; i++) if(!v_[i].on) { pick = i; break; }
        if(pick < 0)
        {
            float q = 2.0f; pick = 0;
            for(int i = 0; i < kVoices; i++) if(v_[i].amp < q) { q = v_[i].amp; pick = i; }
        }
        V &v = v_[pick];
        v.on   = true;
        /* a fresh note starts unbent: the strip sends its own offset on the next tick, and
           inheriting the last bend made a new note start sharp or flat and then snap */
        v.bendMul = 1.0f;
        v.slot = best;
        v.note = (uint8_t)note;
        v.pos  = 0.0f;
        /* microtonal offset from the N message, so the sampled piano follows the board */
        const int bestRoot = (zoneCount_ > 0) ? zoneRoot_[best]
                                              : (arenaRoot >= 0 ? arenaRoot : kPiano[best].note);
        v.base = powf(2.0f, (note - bestRoot) / 12.0f) * powf(2.0f, (float)cents / 1200.0f);
        v.rate = v.base * v.bendMul;
        v.loopA = (float)len_[best] * 0.45f;
        v.loopB = (float)len_[best] * 0.92f;
        /* the tape samples sit around -9 dBFS: lift them into the same range as the synth
           engines so the sampler is not the quiet one in the room */
        v.amp  = (vel / 127.0f) * 2.4f;
    }

    int Active() const
    {
        int n = 0;
        for(int i = 0; i < kVoices; i++) if(v_[i].on) n++;
        return n;
    }

    void KillAll() { for(int i = 0; i < kVoices; i++) v_[i].on = false; }

    /* A finger lifted has to stop the sample, not let it ring to the end of the file. The
       voice fades over about twenty milliseconds so there is no click. */
    void Release(int note)
    {
        for(int i = 0; i < kVoices; i++)
        {
            V &v = v_[i];
            if(v.on && !v.releasing && (note < 0 || (int)v.note == note))
                v.releasing = true;
        }
    }

    /* TEMPORARY: what the sample voice is actually running at */
    bool Debug(int note, int *outNote, int *outRate1000, int *outBend1000)
    {
        for(int i = 0; i < kVoices; i++)
        {
            V &v = v_[i];
            if(!v.on) continue;
            if(note >= 0 && (int)v.note != note) continue;
            *outNote = v.note;
            *outRate1000 = (int)(v.rate * 1000.0f);
            *outBend1000 = (int)(v.bendMul * 1000.0f);
            return true;
        }
        return false;
    }

    /* T <part> <note> <cents>: note -1 bends the whole instrument */
    void Bend(int note, int cents)
    {
        const float mul = powf(2.0f, (float)cents / 1200.0f);
        for(int i = 0; i < kVoices; i++)
        {
            V &v = v_[i];
            if(!v.on) continue;
            if(note >= 0 && (int)v.note != note) continue;
            v.bendMul = mul;
            v.rate    = v.base * mul;
        }
        bend_ = mul;
    }

    void Render(float *l, float *r, size_t n)
    {
        for(int vi = 0; vi < kVoices; vi++)
        {
            V &v = v_[vi];
            if(!v.on) continue;
            const int16_t *src = &g_pno_buf[off_[v.slot]];
            const uint32_t len = len_[v.slot];
            for(size_t i = 0; i < n; i++)
            {
                const uint32_t p = (uint32_t)v.pos;
                if(p + 1 >= len) { v.on = false; break; }
                const float fr = v.pos - (float)p;
                const float a  = src[p] * (1.0f - fr) + src[p + 1] * fr;
                float s = (a / 32768.0f) * v.amp;
                if(s >  1.0f) s =  1.0f;            /* soft ceiling, no hard clip on sums */
                if(s < -1.0f) s = -1.0f;
                l[i] += s;
                r[i] += s;
                v.pos += v.rate;
                if(loopOn_ && !v.releasing && v.loopB > v.loopA + 64.0f && v.pos >= v.loopB)
                    v.pos -= (v.loopB - v.loopA);        /* hold the note for as long as it is held */
                if(v.releasing)
                {
                    v.amp *= 0.9990f;                 /* about twenty milliseconds to silence */
                    if(v.amp < 0.0008f) { v.on = false; break; }
                }
            }
        }
    }

  private:
    struct V { bool on; int slot; uint8_t note; float pos, base, rate, amp, bendMul = 1.0f;
               bool releasing = false; float loopA = 0.0f, loopB = 0.0f; };

public:
    /* When a sample has been pushed over the link it lives at the start of the buffer and
       covers the whole keyboard as a single zone, so the root note comes from the loader
       rather than from the baked in table. */
    int arenaRoot = -1;
    int zoneRoot_[5] = {0, 0, 0, 0, 0};
    bool loopOn_ = false;   /* off by default so the pianos keep their natural decay */   /* sustained instruments loop the body of the sample */
    int zoneCount_ = 0;
    void UseArena(int frames, int root)
    {
        if(frames <= 0 || frames > 600000){ arenaRoot = -1; return; }
        off_[0] = 0;
        len_[0] = frames;
        arenaRoot = root;
    }
    bool arenaActive() const { return arenaRoot >= 0; }

    /* A zone is one pushed sample: where it sits in the arena, how long it is and what note
       it was recorded at. Up to five, which is what the root slots already allowed. */
    void ResetZones() { zoneCount_ = 0; arenaRoot = -1; }
    void SetLoop(bool b) { loopOn_ = b; }
    int zoneCount() const { return zoneCount_; }
    int zoneLen(int z) const { return (z >= 0 && z < 5) ? (int)len_[z] : -1; }
    int zoneOff(int z) const { return (z >= 0 && z < 5) ? (int)off_[z] : -1; }
    int zoneRoot(int z) const { return (z >= 0 && z < 5) ? zoneRoot_[z] : -1; }
    void SetZone(int z, int off, int len, int root)
    {
        if(z < 0 || z > 4) return;
        off_[z] = off;
        len_[z] = len;
        zoneRoot_[z] = root;
        if(z + 1 > zoneCount_) zoneCount_ = z + 1;
        arenaRoot = -1;
    }
    uint32_t off_[5], len_[5];
    float    bend_ = 1.0f;
    V v_[kVoices];
};
