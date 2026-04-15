/* tinychk.c — Reference implementations of the three checksums. */

#include "tinychk.h"

#define ADLER_BASE 65521U
#define ADLER_NMAX 5552U

uint32_t adler32(uint32_t seed, const uint8_t *buf, size_t len)
{
    uint32_t s1 = seed & 0xFFFF;
    uint32_t s2 = (seed >> 16) & 0xFFFF;
    size_t k;

    while (len > 0) {
        k = len < ADLER_NMAX ? len : ADLER_NMAX;
        len -= k;
        while (k--) {
            s1 += *buf++;
            s2 += s1;
        }
        s1 %= ADLER_BASE;
        s2 %= ADLER_BASE;
    }
    return (s2 << 16) | s1;
}

/* CRC-32 table is computed lazily on first call. */
static uint32_t crc_table[256];
static int      crc_table_ready = 0;

static void crc32_init_table(void)
{
    uint32_t c, n, k;
    for (n = 0; n < 256; n++) {
        c = n;
        for (k = 0; k < 8; k++) {
            c = (c & 1) ? (0xEDB88320U ^ (c >> 1)) : (c >> 1);
        }
        crc_table[n] = c;
    }
    crc_table_ready = 1;
}

uint32_t crc32(uint32_t seed, const uint8_t *buf, size_t len)
{
    uint32_t c;
    size_t   i;
    if (!crc_table_ready) crc32_init_table();
    c = seed ^ 0xFFFFFFFFU;
    for (i = 0; i < len; i++) {
        c = crc_table[(c ^ buf[i]) & 0xFF] ^ (c >> 8);
    }
    return c ^ 0xFFFFFFFFU;
}

uint16_t fletcher16(const uint8_t *buf, size_t len)
{
    uint16_t sum1 = 0, sum2 = 0;
    size_t i;
    for (i = 0; i < len; i++) {
        sum1 = (sum1 + buf[i]) % 255;
        sum2 = (sum2 + sum1) % 255;
    }
    return (sum2 << 8) | sum1;
}
