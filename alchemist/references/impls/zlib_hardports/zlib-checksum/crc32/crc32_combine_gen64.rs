pub fn crc32_combine_gen64(len2: u64) -> u32 {
    // Port of crc32.c:crc32_combine_gen.
    // Returns x^(8 * len2) mod p(x) — the polynomial you multiply the
    // first CRC by before XORing the second to combine them.
    // The `8 *` comes from byte-level expansion (1 byte = 8 bits of
    // polynomial shift).
    x2nmodp(len2, 3)
}
