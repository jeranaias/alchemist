pub fn x2nmodp(mut n: u64, mut k: u32) -> u32 {
    // Port of zlib crc32.c:x2nmodp (and its make_crc_table initializer).
    //
    // Returns x^(n * 2^k) mod p(x) in reflected IEEE-802.3 form. Used by
    // crc32_combine_gen to merge CRC-32s of two byte streams.
    //
    // Algorithm (strictly matches the C source):
    //   table[0] = 1 << 30;                              // x^1 (reflected)
    //   for i in 1..32 { table[i] = multmodp(table[i-1], table[i-1]); }
    //   p = 1 << 31;                                     // x^0 == 1
    //   while n:
    //       if n & 1: p = multmodp(table[k & 31], p);
    //       n >>= 1; k += 1;
    //
    // Note: in reflected form bit 31 holds x^0 and bit 30 holds x^1.
    // Looping over n (not k) and incrementing k each iteration — the
    // previous version inverted this and hit a fixed-point at 0x80000000.

    let mut table = [0u32; 32];
    table[0] = 1u32 << 30;
    let mut i = 1;
    while i < 32 {
        table[i] = multmodp(table[i - 1], table[i - 1]);
        i += 1;
    }

    let mut p: u32 = 1u32 << 31;
    while n != 0 {
        if n & 1 != 0 {
            p = multmodp(table[(k & 31) as usize], p);
        }
        n >>= 1;
        k = k.wrapping_add(1);
    }
    p
}
