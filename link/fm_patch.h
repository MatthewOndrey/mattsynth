// fm_patch.h - DX7-compatible patch format and the 32 algorithm routing table.
// Pure C++ with no Daisy dependency, so it compiles and runs on a host for
// testing (link/test_fm_patch.cpp).
//
// Packed voice layout follows the DX7 SysEx voice dump (155 bytes):
//   operators are packed OP6 first, then OP5 ... OP1 (21 bytes each)
//   then pitch EG (8), algorithm (1), feedback+osc sync (1), LFO (6),
//   transpose (1), name (10), 2 unused.
//
// The routing table is the DX7 hardware topology, in the same bus encoding used
// by the msfa/Google FM core (Apache-2.0): each op byte is
//   bit 0-1  output bus (0 = voice output)
//   bit 2    add to the destination instead of overwriting
//   bit 4-5  input bus (0 = none)
//   bit 6-7  feedback pair (both set = op feeds itself)
// Processing order is op[0] .. op[5], which matches OP6 .. OP1, so modulators
// always run before the operators they feed.

#pragma once

#include <stdint.h>
#include <string.h>

namespace fm {

static const int kPatchSize = 155;

struct Op
{
    uint8_t rate[4];
    uint8_t level[4];
    uint8_t break_point;
    uint8_t left_depth;
    uint8_t right_depth;
    uint8_t left_curve;
    uint8_t right_curve;
    uint8_t rate_scaling;
    uint8_t amp_mod_sens;
    uint8_t key_vel_sens;
    uint8_t output_level;
    uint8_t osc_mode;
    uint8_t freq_coarse;
    uint8_t freq_fine;
    uint8_t detune;
};

struct Patch
{
    Op      op[6]; /* op[0] = DX7 OP6 ... op[5] = DX7 OP1 */
    uint8_t pitch_rate[4];
    uint8_t pitch_level[4];
    uint8_t algorithm;
    uint8_t feedback;
    uint8_t osc_key_sync;
    uint8_t lfo_speed;
    uint8_t lfo_delay;
    uint8_t lfo_pmd;
    uint8_t lfo_amd;
    uint8_t lfo_sync;
    uint8_t lfo_wave;
    uint8_t transpose;
    uint8_t pan;    /* 0-99, 50 = centre. Stored in the two spare DX7 bytes. */
    uint8_t spread; /* 0-99, note-to-note stereo fan                        */
    char    name[11];
};

/* ---- routing table ------------------------------------------------------ */

static const uint8_t kAlg[32][6] = {
    {0xc1, 0x11, 0x11, 0x14, 0x01, 0x14}, /*  1 */
    {0x01, 0x11, 0x11, 0x14, 0xc1, 0x14}, /*  2 */
    {0xc1, 0x11, 0x14, 0x01, 0x11, 0x14}, /*  3 */
    {0xc1, 0x11, 0x94, 0x01, 0x11, 0x14}, /*  4 */
    {0xc1, 0x14, 0x01, 0x14, 0x01, 0x14}, /*  5 */
    {0xc1, 0x94, 0x01, 0x14, 0x01, 0x14}, /*  6 */
    {0xc1, 0x11, 0x05, 0x14, 0x01, 0x14}, /*  7 */
    {0x01, 0x11, 0xc5, 0x14, 0x01, 0x14}, /*  8 */
    {0x01, 0x11, 0x05, 0x14, 0xc1, 0x14}, /*  9 */
    {0x01, 0x05, 0x14, 0xc1, 0x11, 0x14}, /* 10 */
    {0xc1, 0x05, 0x14, 0x01, 0x11, 0x14}, /* 11 */
    {0x01, 0x05, 0x05, 0x14, 0xc1, 0x14}, /* 12 */
    {0xc1, 0x05, 0x05, 0x14, 0x01, 0x14}, /* 13 */
    {0xc1, 0x05, 0x11, 0x14, 0x01, 0x14}, /* 14 */
    {0x01, 0x05, 0x11, 0x14, 0xc1, 0x14}, /* 15 */
    {0xc1, 0x11, 0x02, 0x25, 0x05, 0x14}, /* 16 */
    {0x01, 0x11, 0x02, 0x25, 0xc5, 0x14}, /* 17 */
    {0x01, 0x11, 0x11, 0xc5, 0x05, 0x14}, /* 18 */
    {0xc1, 0x14, 0x14, 0x01, 0x11, 0x14}, /* 19 */
    {0x01, 0x05, 0x14, 0xc1, 0x14, 0x14}, /* 20 */
    {0x01, 0x14, 0x14, 0xc1, 0x14, 0x14}, /* 21 */
    {0xc1, 0x14, 0x14, 0x14, 0x01, 0x14}, /* 22 */
    {0xc1, 0x14, 0x14, 0x01, 0x14, 0x04}, /* 23 */
    {0xc1, 0x14, 0x14, 0x14, 0x04, 0x04}, /* 24 */
    {0xc1, 0x14, 0x14, 0x04, 0x04, 0x04}, /* 25 */
    {0xc1, 0x05, 0x14, 0x01, 0x14, 0x04}, /* 26 */
    {0x01, 0x05, 0x14, 0xc1, 0x14, 0x04}, /* 27 */
    {0x04, 0xc1, 0x11, 0x14, 0x01, 0x14}, /* 28 */
    {0xc1, 0x14, 0x01, 0x14, 0x04, 0x04}, /* 29 */
    {0x04, 0xc1, 0x11, 0x14, 0x04, 0x04}, /* 30 */
    {0xc1, 0x14, 0x04, 0x04, 0x04, 0x04}, /* 31 */
    {0xc4, 0x04, 0x04, 0x04, 0x04, 0x04}, /* 32 */
};

struct Route
{
    uint8_t in_bus;  /* 0 = unmodulated */
    uint8_t out_bus; /* 0 = voice output */
    bool    add;     /* add to destination */
    bool    fb;      /* self feedback */
};

inline Route route(uint8_t algorithm, int op_index)
{
    const uint8_t f = kAlg[algorithm & 31][op_index & 7];
    Route         r;
    r.in_bus  = (uint8_t)((f >> 4) & 3);
    r.out_bus = (uint8_t)(f & 3);
    r.add     = (f & 4) != 0;
    r.fb      = (f & 0xC0) == 0xC0;
    return r;
}

inline bool is_carrier(uint8_t algorithm, int op_index)
{
    return route(algorithm, op_index).out_bus == 0;
}

/* ---- packed <-> struct -------------------------------------------------- */

inline void unpack(const uint8_t* p, Patch* out)
{
    memset(out, 0, sizeof(*out));
    for(int i = 0; i < 6; ++i)
    {
        const uint8_t* q = p + (i * 21);
        Op&            o = out->op[i]; /* i = 0 is OP6, as packed */
        for(int k = 0; k < 4; ++k)
        {
            o.rate[k]  = q[k];
            o.level[k] = q[4 + k];
        }
        o.break_point   = q[8];
        o.left_depth    = q[9];
        o.right_depth   = q[10];
        o.left_curve    = q[11];
        o.right_curve   = q[12];
        o.rate_scaling  = q[13];
        o.amp_mod_sens  = q[14];
        o.key_vel_sens  = q[15];
        o.output_level  = q[16];
        o.osc_mode      = q[17];
        o.freq_coarse   = q[18];
        o.freq_fine     = q[19];
        o.detune        = q[20];
    }
    const uint8_t* g = p + 126;
    for(int k = 0; k < 4; ++k)
    {
        out->pitch_rate[k]  = g[k];
        out->pitch_level[k] = g[4 + k];
    }
    out->algorithm    = g[8] & 31;
    out->feedback     = g[9] & 7;
    out->osc_key_sync = (g[9] >> 3) & 1;
    out->lfo_speed    = g[10];
    out->lfo_delay    = g[11];
    out->lfo_pmd      = g[12];
    out->lfo_amd      = g[13];
    out->lfo_sync     = g[14] & 1;
    out->lfo_wave     = g[15] & 7;
    out->transpose    = g[16];
    memcpy(out->name, p + 143, 10);
    out->name[10] = 0;
    /* the two spare bytes carry pan/spread; zero pan means "unset" so that
       stock DX7 patches land in the centre instead of hard left */
    out->pan    = p[153] ? p[153] : 50;
    out->spread = p[154];
}

/* 32-voice bulk dump voice: 128 bytes, ops packed tighter than the single
   voice format (curves, detune+rate scaling, kvs+ams share bytes). Layout per
   the DX7 data format sheet, ops OP6 first. */
inline void unpack_bulk(const uint8_t* p, Patch* out)
{
    memset(out, 0, sizeof(*out));
    for(int i = 0; i < 6; ++i)
    {
        const uint8_t* q = p + (i * 17);
        Op&            o = out->op[i];
        for(int k = 0; k < 4; ++k)
        {
            o.rate[k]  = q[k];
            o.level[k] = q[4 + k];
        }
        o.break_point   = q[8];
        o.left_depth    = q[9];
        o.right_depth   = q[10];
        o.left_curve    = q[11] & 3;
        o.right_curve   = (q[11] >> 2) & 3;
        o.detune        = (q[12] >> 3) & 15;
        o.rate_scaling  = q[12] & 7;
        o.key_vel_sens  = (q[13] >> 3) & 7;
        o.amp_mod_sens  = q[13] & 3;
        o.output_level  = q[14];
        o.freq_coarse   = (q[15] >> 1) & 31;
        o.osc_mode      = q[15] & 1;
        o.freq_fine     = q[16];
    }
    for(int k = 0; k < 4; ++k)
    {
        out->pitch_rate[k]  = p[102 + k];
        out->pitch_level[k] = p[106 + k];
    }
    out->algorithm    = p[110] & 31;
    out->feedback     = p[111] & 7;
    out->osc_key_sync = (p[111] >> 3) & 1;
    out->lfo_speed    = p[112];
    out->lfo_delay    = p[113];
    out->lfo_pmd      = p[114];
    out->lfo_amd      = p[115];
    out->lfo_sync     = p[116] & 1;
    out->lfo_wave     = (p[116] >> 1) & 7;
    out->transpose    = p[117];
    memcpy(out->name, p + 118, 10);
    out->name[10] = 0;
    out->pan      = 50;
    out->spread   = 30;
}

inline void pack(const Patch* in, uint8_t* p)
{
    memset(p, 0, kPatchSize);
    for(int i = 0; i < 6; ++i)
    {
        uint8_t* q            = p + (i * 21);
        const Op& o           = in->op[i];
        for(int k = 0; k < 4; ++k)
        {
            q[k]     = o.rate[k];
            q[4 + k] = o.level[k];
        }
        q[8]  = o.break_point;
        q[9]  = o.left_depth;
        q[10] = o.right_depth;
        q[11] = o.left_curve;
        q[12] = o.right_curve;
        q[13] = o.rate_scaling;
        q[14] = o.amp_mod_sens;
        q[15] = o.key_vel_sens;
        q[16] = o.output_level;
        q[17] = o.osc_mode;
        q[18] = o.freq_coarse;
        q[19] = o.freq_fine;
        q[20] = o.detune;
    }
    uint8_t* g = p + 126;
    for(int k = 0; k < 4; ++k)
    {
        g[k]     = in->pitch_rate[k];
        g[4 + k] = in->pitch_level[k];
    }
    g[8]  = (uint8_t)(in->algorithm & 31);
    g[9]  = (uint8_t)((in->feedback & 7) | ((in->osc_key_sync & 1) << 3));
    g[10] = in->lfo_speed;
    g[11] = in->lfo_delay;
    g[12] = in->lfo_pmd;
    g[13] = in->lfo_amd;
    g[14] = in->lfo_sync & 1;
    g[15] = in->lfo_wave & 7;
    g[16] = in->transpose;
    memcpy(p + 143, in->name, 10);
    p[153] = in->pan;
    p[154] = in->spread;
}

} // namespace fm
