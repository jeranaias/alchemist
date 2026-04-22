pub fn adler32_z(adler: u32, buf: &[u8], len: usize) -> u32 {
    let mut s1: u32 = adler & 0xFFFF;
    let mut s2: u32 = (adler >> 16) & 0xFFFF;
    let n = len.min(buf.len());
    let mut i: usize = 0;
    while i < n {
        // NMAX is typed as i32 by the constants extractor (bare decimal).
        // Cast explicitly so usize arithmetic below works; runtime value
        // (5552) fits in usize on every target so the cast is lossless.
        let nmax = NMAX as usize;
        let k = core::cmp::min(n - i, nmax);
        for j in i..i + k {
            s1 = s1.wrapping_add(buf[j] as u32);
            s2 = s2.wrapping_add(s1);
        }
        i += k;
        s1 %= BASE;
        s2 %= BASE;
    }
    (s2 << 16) | s1
}