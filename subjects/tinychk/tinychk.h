/* tinychk.h — Tiny checksum library for Alchemist end-to-end test.
 *
 * Exposes:
 *   - adler32(): RFC 1950 Adler-32 checksum
 *   - crc32():   IEEE 802.3 / zlib CRC-32 (polynomial 0xEDB88320)
 *   - fletcher16(): 16-bit Fletcher checksum
 */
#ifndef TINYCHK_H
#define TINYCHK_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Compute the RFC 1950 Adler-32 checksum of `buf` of length `len`.
 *
 * BASE = 65521 (largest prime < 2^16).
 * NMAX = 5552.
 * Initial seed should be 1.
 *
 * Returns (s2 << 16) | s1.
 */
uint32_t adler32(uint32_t seed, const uint8_t *buf, size_t len);

/* Compute the IEEE 802.3 / zlib CRC-32 of `buf` of length `len`.
 *
 * Polynomial: 0xEDB88320 (reflected representation of 0x04C11DB7).
 * Initial value: 0xFFFFFFFF.
 * Final XOR: 0xFFFFFFFF.
 * Initial seed should be 0.
 */
uint32_t crc32(uint32_t seed, const uint8_t *buf, size_t len);

/* Compute the 16-bit Fletcher checksum of `buf`.
 * Returns (sum2 << 8) | sum1, each mod 255.
 */
uint16_t fletcher16(const uint8_t *buf, size_t len);

#ifdef __cplusplus
}
#endif

#endif /* TINYCHK_H */
