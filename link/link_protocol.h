// link_protocol.h
// Wire format for the Daisy Pod <-> ESP32-S3 panel link.
//
// One source of truth for both firmwares. Plain C++11, no STL, no exceptions,
// no dynamic allocation, no float printf. Compiles for arm-none-eabi-g++ and
// for the ESP32 Arduino core.
//
// Frame:   '@' <body> '*' <CS> '\n'
//   body:  printable ASCII, no '@', no '*', no newline. Fields are separated by
//          single spaces, the first field is a one letter verb.
//   CS:    two uppercase hex digits, XOR of every byte of body.
//   values are integers only. Unit values (0.0 - 1.0) travel as milli units
//          (0 - 1000). Notes and velocities are raw MIDI numbers.
//
// Why a checksum and why "ignore anything that does not match": UART0 on the
// panel is shared with the CH343P debug bridge, so log text can appear on the
// same wire. A strict frame plus checksum means noise is simply dropped.
#pragma once

#include <cstddef>
#include <cstdint>
#include <cstring>

namespace podlink {

constexpr uint32_t kBaud    = 115200;
constexpr size_t   kBufSize = 96;   // one line, including framing

inline uint8_t checksum(const char* s, size_t n)
{
    uint8_t x = 0;
    for(size_t i = 0; i < n; ++i)
        x ^= static_cast<uint8_t>(s[i]);
    return x;
}

inline char hexdigit(uint8_t v)
{
    v &= 0x0F;
    return static_cast<char>(v < 10 ? ('0' + v) : ('A' + (v - 10)));
}

// Returns 0..15, or -1 if c is not a hex digit.
inline int hexval(char c)
{
    if(c >= '0' && c <= '9')
        return c - '0';
    if(c >= 'A' && c <= 'F')
        return c - 'A' + 10;
    if(c >= 'a' && c <= 'f')
        return c - 'a' + 10;
    return -1;
}

// Builds "@<body>*CS\n" into out. Returns bytes written, 0 if it does not fit
// or the body is not legal.
inline size_t frame(char* out, size_t cap, const char* body)
{
    const size_t n = strlen(body);
    if(n == 0 || n > kBufSize - 5)
        return 0;
    for(size_t i = 0; i < n; ++i)
    {
        const char c = body[i];
        if(c == '@' || c == '*' || c == '\n' || c == '\r' || c < 32 || c > 126)
            return 0;
    }
    if(cap < n + 5)
        return 0;
    out[0] = '@';
    memcpy(out + 1, body, n);
    out[n + 1]   = '*';
    const uint8_t cs = checksum(body, n);
    out[n + 2]   = hexdigit(static_cast<uint8_t>(cs >> 4));
    out[n + 3]   = hexdigit(cs);
    out[n + 4]   = '\n';
    return n + 5;
}

// Incremental receiver. Feed one byte at a time from the UART read loop.
// When ready() is true, line() holds the verified body. Call clear() after
// handling it.
struct Parser
{
    char   buf[kBufSize];
    size_t len;
    char   line[kBufSize];
    bool   ready;
    bool   discarding;   // after an overlong line: skip until the next frame start

    Parser() { clear(); }

    void clear()
    {
        len        = 0;
        ready      = false;
        discarding = false;
        line[0]    = '\0';
    }

    void feed(char c)
    {
        if(c == '\n' || c == '\r')
        {
            end();
            discarding = false;
            return;
        }
        if(discarding)
        {
            if(c != '@')   // resync on the next start of frame
                return;
            discarding = false;
            len        = 0;
        }
        if(len >= kBufSize)   // runaway line: drop it and resync on the next '@'
        {
            discarding = true;
            len        = 0;
            return;
        }
        buf[len++] = c;
    }

    void end()
    {
        ready = false;
        if(len >= 4 && buf[0] == '@')
        {
            size_t star = 0;
            for(size_t i = 1; i < len; ++i)
                if(buf[i] == '*')
                    star = i;
            // need at least one body char, then '*' and exactly two hex digits
            if(star >= 2 && star + 3 == len)
            {
                const int hi = hexval(buf[star + 1]);
                const int lo = hexval(buf[star + 2]);
                if(hi >= 0 && lo >= 0)
                {
                    const uint8_t want = static_cast<uint8_t>((hi << 4) | lo);
                    if(checksum(buf + 1, star - 1) == want)
                    {
                        const size_t n = star - 1;
                        memcpy(line, buf + 1, n);
                        line[n] = '\0';
                        ready   = true;
                    }
                }
            }
        }
        len = 0;
    }
};

// Body helpers. body is "V args" or just "V".
inline char        verb(const char* body) { return body[0]; }
inline const char* args(const char* body)
{
    const char* p = strchr(body, ' ');
    return p ? p + 1 : "";
}

} // namespace podlink
