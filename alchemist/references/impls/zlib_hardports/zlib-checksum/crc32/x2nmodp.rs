pub fn x2nmodp(mut n: u64, k: u32) -> u32 {
    // Port of crc32.c:x2nmodp.
    // Computes x^(n * 2^(k+3)) mod p(x) in GF(2)[x]/p(x), reflected form,
    // where p(x) is the IEEE 802.3 CRC-32 polynomial. Used to combine
    // CRC-32 checksums of two separate byte streams without rescanning.
    //
    // The algorithm:
    //   t[0] = x,  t[i] = t[i-1]^2  (so t[i] = x^(2^(i+1)))
    //   p = x^1
    //   walk k's bits: when bit is set, fold t[i] into p
    //   walk n's bits: same
    //
    // Reflected-form convention: x^0 is 0x80000000 (MSB of the bit-reversed
    // polynomial). multiplication is via multmodp (already hardported).

    let mut table = [0u32; 32];
    table[0] = 0x80000000_u32; // x^1 in reflected form (before first squaring)
    // Actually zlib initializes t[0] = x^2 = multmodp of x^1 with itself.
    // Follow the C source strictly:
    //   p_x1 = 0x80000000  (the monomial x in reflected form? Actually in
    //                       zlib's "polynomial with bit 31 being x^0"
    //                       convention, this is x.)
    // The table in zlib is built as:
    //   t[0] = x;  t[i] = multmodp(t[i-1], t[i-1])  for i in 1..32
    // and used with initial p = x and initial i = 3 (offset for byte-aligned
    // exponents — bytes are 8 bits, hence the log2(8) = 3 offset).
    let mut i = 1;
    while i < 32 {
        table[i] = multmodp(table[i - 1], table[i - 1]);
        i += 1;
    }

    let mut p: u32 = 0x80000000;
    let mut idx: usize = 3;
    let mut k = k;
    while k != 0 {
        if k & 1 != 0 {
            p = multmodp(table[idx & 31], p);
        }
        idx = (idx + 1) & 31;
        k >>= 1;
    }
    while n != 0 {
        if n & 1 != 0 {
            p = multmodp(table[idx & 31], p);
        }
        idx = (idx + 1) & 31;
        n >>= 1;
    }
    p
}
