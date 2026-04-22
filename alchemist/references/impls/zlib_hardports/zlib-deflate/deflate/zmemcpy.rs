pub fn zmemcpy(dst: &mut [u8], src: &[u8], n: usize) {
    for i in 0..n {
        dst[i] = src[i];
    }
}
