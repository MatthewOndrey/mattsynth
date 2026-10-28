/* KitEngine — eight one shot samples, played from a copy held in SDRAM.

   The data lives in QSPI as const arrays (see samples.h). At boot the whole kit is
   copied into SDRAM once, because fetching audio samples from QSPI in the middle of
   the audio callback is slow enough to show up as a drop out. */
#pragma once
#include <stdint.h>
#include <string.h>
#include "samples.h"

#ifndef DSY_SDRAM_BSS
#define DSY_SDRAM_BSS __attribute__((section(".sdram_bss")))
#endif

/* the kit buffer has to be file scope: a section attribute is not allowed on a
   class member */
DSY_SDRAM_BSS static int16_t g_kit_buf[240000];

class KitEngine
{
  public:
    static const int kVoices = 8;

    void Init()
    {
        uint32_t total = 0;
        for(int i = 0; i < 8; i++)
            total += kKit[i].len;
        if(total > kKitCapacity)
            total = kKitCapacity;

        uint32_t at = 0;
        for(int i = 0; i < 8; i++)
        {
            off_[i] = at;
            len_[i] = kKit[i].len;
            if(at + kKit[i].len <= kKitCapacity)
                memcpy(&g_kit_buf[at], kKit[i].data, kKit[i].len * 2);
            at += kKit[i].len;
        }
        for(int i = 0; i < kVoices; i++)
            v_[i].on = false;
    }

    void Trigger(int note, int vel)
    {
        const int s = note - 36;
        if(s < 0 || s > 7)
            return;
        int pick = -1;
        for(int i = 0; i < kVoices; i++)
            if(!v_[i].on) { pick = i; break; }
        if(pick < 0)
        {
            /* steal the quietest */
            float best = 2.0f;
            for(int i = 0; i < kVoices; i++)
                if(v_[i].amp < best) { best = v_[i].amp; pick = i; }
        }
        v_[pick].on   = true;
        v_[pick].slot = s;
        v_[pick].pos  = 0.0f;
        v_[pick].amp  = (vel / 127.0f) * 0.9f * level_[s];
        v_[pick].rate = rate_[s];   /* was left uninitialised: garbage rate meant a click */
        v_[pick].end  = (uint32_t)(len_[s] * trim_[s]);
        if(v_[pick].end < 64) v_[pick].end = len_[s];
    }

    int Active() const
    {
        int n = 0;
        for(int i = 0; i < kVoices; i++) if(v_[i].on) n++;
        return n;
    }

    void KillAll()
    {
        for(int i = 0; i < kVoices; i++) v_[i].on = false;
    }

    /* mono sum into both channels; the kit bypasses the synth's stereo spread */
    void Render(float *l, float *r, size_t n)
    {
        for(int vi = 0; vi < kVoices; vi++)
        {
            V &v = v_[vi];
            if(!v.on) continue;
            const int16_t *src = &g_kit_buf[off_[v.slot]];
            const uint32_t len = len_[v.slot];
            const uint32_t end = v.end ? v.end : len;
            for(size_t i = 0; i < n; i++)
            {
                const uint32_t p = (uint32_t)v.pos;
                if(p + 1 >= len || p >= end) { v.on = false; break; }
                const float fr = v.pos - (float)p;
                const float a = src[p] * (1.0f - fr) + src[p + 1] * fr;
                /* a two millisecond fade keeps the one shots from clicking on */
                float g = 1.0f;
                if(v.pos < 96.0f)            g = v.pos / 96.0f;
                else if(end - v.pos < 96.0f) g = (end - v.pos) / 96.0f;
                const float s = (a / 32768.0f) * v.amp * g;
                l[i] += s;
                r[i] += s;
                v.pos += v.rate;
            }
        }
    }

    /* Y <pad> <rate> <level> <trim> in thousandths, so nothing float crosses the wire */
    void SetPad(int pad, int rate_m, int level_m, int trim_m)
    {
        if(pad < 0 || pad > 7) return;
        rate_[pad]  = (rate_m  > 50 && rate_m  < 4000) ? rate_m  / 1000.0f : 1.0f;
        level_[pad] = (level_m >= 0 && level_m <= 2000) ? level_m / 1000.0f : 1.0f;
        trim_[pad]  = (trim_m  >= 50 && trim_m  <= 1000) ? trim_m  / 1000.0f : 1.0f;
    }

  private:
    struct V { bool on; int slot; uint32_t end; float pos, amp, rate; };
    static const uint32_t kKitCapacity = 240000;   /* frames, about five seconds */
    uint32_t off_[8], len_[8];
    float    rate_[8]  = {1,1,1,1,1,1,1,1};
    float    level_[8] = {1,1,1,1,1,1,1,1};
    float    trim_[8]  = {1,1,1,1,1,1,1,1};
    V        v_[kVoices];
};
