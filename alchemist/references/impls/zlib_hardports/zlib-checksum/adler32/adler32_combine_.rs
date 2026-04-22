pub fn adler32_combine_(adler1: u32, adler2: u32, len2: u64) -> u32 {
    // Port of zlib's adler32_combine_ (adler32.c).
    // RFC 1950 Adler-32. BASE = 65521 is the largest prime < 2^16.
    // The formula combines two Adler-32 checksums given the length of
    // the second input, without rescanning either payload.
    //
    // sum1 = (s1a + s1b - 1) mod BASE
    // sum2 = (len2 * s1a + s2a + s2b - 1) mod BASE
    // where (s1x, s2x) are the low/high halves of the adler32 values.
    //
    // See adler32.c:adler32_combine_ for the full derivation. The
    // explicit "if sum >= BASE" cascade replaces the C MOD() macro;
    // correctness requires exactly two subtractions for sum1 and up to
    // two (by BASE, by BASE*2) for sum2.

    let rem = (len2 % (BASE as u64)) as u32;
    let sum1_a = adler1 & 0xffff;

    // `rem * sum1_a` bounded by (BASE-1) * 65535 < 2^32, no overflow.
    let mut sum2 = (rem * sum1_a) % BASE;

    // sum1 = sum1_a + (adler2 low) + BASE - 1
    // Working in u32: all terms fit, cascade two BASE subtractions.
    let mut sum1 = sum1_a.wrapping_add((adler2 & 0xffff).wrapping_add(BASE).wrapping_sub(1));

    // sum2 += (adler1 high) + (adler2 high) + BASE - rem
    sum2 = sum2.wrapping_add(
        ((adler1 >> 16) & 0xffff)
            .wrapping_add((adler2 >> 16) & 0xffff)
            .wrapping_add(BASE)
            .wrapping_sub(rem),
    );

    // Fold sum1 into [0, BASE)
    if sum1 >= BASE {
        sum1 -= BASE;
    }
    if sum1 >= BASE {
        sum1 -= BASE;
    }
    // Fold sum2 into [0, BASE)
    let base2 = BASE << 1;
    if sum2 >= base2 {
        sum2 -= base2;
    }
    if sum2 >= BASE {
        sum2 -= BASE;
    }

    sum1 | (sum2 << 16)
}
