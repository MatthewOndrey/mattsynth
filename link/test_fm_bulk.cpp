// Host check for the 32-voice bulk unpacker: builds a 128-byte bulk voice with
// known field values (packed per the DX7 data format sheet) and asserts the
// unpacker lands them in the right members.
#include <cassert>
#include <cstdio>
#include <cstring>

#include "fm_patch.h"

int main()
{
    uint8_t v[128];
    memset(v, 0, sizeof(v));

    // OP6 (op index 0) at bytes 0..16
    v[0] = 11;
    v[1] = 22;
    v[2] = 33;
    v[3] = 44; // rates
    v[4] = 55;
    v[5] = 66;
    v[6] = 77;
    v[7] = 88; // levels
    v[8] = 60; // break point
    v[9] = 61;
    v[10] = 62;                // left/right depth
    v[11] = (2 << 2) | 1;       // right curve 2, left curve 1
    v[12] = (7 << 3) | 5;       // detune 7, rate scaling 5
    v[13] = (5 << 3) | 2;       // key vel sens 5, amp mod sens 2
    v[14] = 99;                 // output level
    v[15] = (3 << 1) | 1;       // coarse 3, osc mode 1
    v[16] = 42;                 // fine

    // OP1 (op index 5) at bytes 85..101, give it a distinct rate
    v[85] = 91;

    for(int k = 0; k < 4; ++k)
    {
        v[102 + k] = (uint8_t)(10 + k);
        v[106 + k] = (uint8_t)(70 + k);
    }
    v[110] = 20;          // algorithm
    v[111] = (1 << 3) | 5; // key sync 1, feedback 5
    v[112] = 40;
    v[113] = 41;
    v[114] = 42;
    v[115] = 43;
    v[116] = (3 << 4) | (2 << 1) | 1; // pitch mod sens 3, wave 2, sync 1
    v[117] = 24;
    memcpy(v + 118, "TESTBULK9", 9);

    fm::Patch p;
    fm::unpack_bulk(v, &p);

    assert(p.op[0].rate[0] == 11);
    assert(p.op[0].level[3] == 88);
    assert(p.op[0].break_point == 60);
    assert(p.op[0].left_depth == 61);
    assert(p.op[0].right_depth == 62);
    assert(p.op[0].left_curve == 1);
    assert(p.op[0].right_curve == 2);
    assert(p.op[0].detune == 7);
    assert(p.op[0].rate_scaling == 5);
    assert(p.op[0].key_vel_sens == 5);
    assert(p.op[0].amp_mod_sens == 2);
    assert(p.op[0].output_level == 99);
    assert(p.op[0].freq_coarse == 3);
    assert(p.op[0].osc_mode == 1);
    assert(p.op[0].freq_fine == 42);
    assert(p.op[5].rate[0] == 91);
    assert(p.pitch_rate[0] == 10 && p.pitch_level[3] == 73);
    assert(p.algorithm == 20);
    assert(p.feedback == 5);
    assert(p.osc_key_sync == 1);
    assert(p.lfo_speed == 40 && p.lfo_amd == 43);
    assert(p.lfo_wave == 2);
    assert(p.lfo_sync == 1);
    assert(p.transpose == 24);
    assert(strcmp(p.name, "TESTBULK9") == 0);
    assert(p.pan == 50);
    assert(p.spread == 30);

    printf("bulk unpack: all assertions passed, name=%s alg=%d\n", p.name, p.algorithm);
    return 0;
}
