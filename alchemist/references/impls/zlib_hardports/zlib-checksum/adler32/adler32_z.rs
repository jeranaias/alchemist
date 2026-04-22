pub fn adler32_z(adler: u32, buf: &[u8], len: usize) -> u32 {
    let mut s1: u32 = adler & 0xFFFF;
    let mut s2: u32 = (adler >> 16) & 0xFFFF;
    let n = len.min(buf.len());
    let mut i: usize = 0;
    while i < n {
        let k = core::cmp::min(n - i, NMAX);
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