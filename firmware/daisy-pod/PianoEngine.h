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
        for(int i = 0; i < 5; i++)
        {
            const int d = abs(note - kPiano[i].note);
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
        v.bendMul = bend_;
        v.slot = best;
        v.note = (uint8_t)note;
        v.pos  = 0.0f;
        /* microtonal offset from the N message, so the sampled piano follows the board */
        v.base = powf(2.0f, (note - kPiano[best].note) / 12.0f) * powf(2.0f, (float)cents / 1200.0f);
        v.rate = v.base * v.bendMul;
        v.amp  = (vel / 127.0f) * 0.85f;
    }

    int Active() const
    {
        int n = 0;
        for(int i = 0; i < kVoices; i++) if(v_[i].on) n++;
        return n;
    }

    void KillAll() { for(int i = 0; i < kVoices; i++) v_[i].on = false; }

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
                const float s  = (a / 32768.0f) * v.amp;
                l[i] += s;
                r[i] += s;
                v.pos += v.rate;
            }
        }
    }

  private:
    struct V { bool on; int slot; uint8_t note; float pos, base, rate, amp, bendMul = 1.0f; };
    uint32_t off_[5], len_[5];
    float    bend_ = 1.0f;
    V v_[kVoices];
};
