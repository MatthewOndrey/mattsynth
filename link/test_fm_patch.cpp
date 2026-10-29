// test_fm_patch.cpp - host test for the DX7 patch layout and routing table.
// Build and run:  g++ -O1 -o /tmp/t test_fm_patch.cpp && /tmp/t

#include "fm_patch.h"

#include <assert.h>
#include <stdio.h>
#include <string.h>

static void test_layout()
{
    uint8_t packed[fm::kPatchSize];
    memset(packed, 0, sizeof(packed));

    // give every packed byte a recognisable value
    for(int i = 0; i < 126; ++i)
        packed[i] = (uint8_t)(i + 1);
    for(int i = 126; i < 143; ++i)
        packed[i] = (uint8_t)(200 + (i - 126));
    memcpy(packed + 143, "TESTPATCH", 9);

    fm::Patch p;
    fm::unpack(packed, &p);

    // op 0 in the struct must be the FIRST packed operator (DX7 OP6)
    assert(p.op[0].rate[0] == 1);
    assert(p.op[0].rate[3] == 4);
    assert(p.op[0].level[0] == 5);
    assert(p.op[0].level[3] == 8);
    assert(p.op[0].break_point == 9);
    assert(p.op[0].right_curve == 13);
    assert(p.op[0].output_level == 17);
    assert(p.op[0].freq_coarse == 19);
    assert(p.op[0].detune == 21);

    // op 5 must be the last packed block
    assert(p.op[5].rate[0] == 106);
    assert(p.op[5].detune == 126);

    assert(p.pitch_rate[0] == 200);
    assert(p.pitch_level[0] == 204);
    assert(p.algorithm == ((200 + 8) & 31));
    assert(p.feedback == ((200 + 9) & 7));
    assert(p.osc_key_sync == (((200 + 9) >> 3) & 1));
    assert(strcmp(p.name, "TESTPATCH") == 0);

    // round trip: operators and pitch EG are byte-exact; the global bytes are
    // masked on unpack by design (algorithm 5 bits, feedback 3 bits, etc)
    uint8_t again[fm::kPatchSize];
    fm::pack(&p, again);
    assert(memcmp(packed, again, 134) == 0);
    printf("roundtrip: operators + pitch EG byte-exact, globals normalised\n");
    printf("layout: ok (op0=OP6, op5=OP1, roundtrip matches)\n");
}

static void test_routes()
{
    int carriers = 0;
    for(int i = 0; i < 6; ++i)
        if(fm::route(0, i).out_bus == 0)
            ++carriers;
    assert(carriers == 2); // algorithm 1 is two carriers
    printf("alg 1: %d carriers\n", carriers);

    carriers = 0;
    for(int i = 0; i < 6; ++i)
        if(fm::route(31, i).out_bus == 0)
            ++carriers;
    assert(carriers == 6); // algorithm 32 is fully additive
    printf("alg 32: %d carriers\n", carriers);

    // feedback is on exactly one op in alg 1, and on op 0 in alg 32
    int fb = 0;
    for(int i = 0; i < 6; ++i)
        if(fm::route(0, i).fb)
            ++fb;
    assert(fb == 1);
    assert(fm::route(31, 0).fb);
    printf("feedback: ok\n");

    // every algorithm must have at least one carrier
    for(int a = 0; a < 32; ++a)
    {
        int c = 0;
        for(int i = 0; i < 6; ++i)
            c += fm::route(a, i).out_bus == 0 ? 1 : 0;
        if(c == 0)
        {
            printf("algorithm %d has no carrier!\n", a + 1);
            assert(0);
        }
    }
    printf("all 32 algorithms have carriers\n");
}

int main()
{
    test_layout();
    test_routes();
    printf("ALL PATCH TESTS PASS\n");
    return 0;
}
