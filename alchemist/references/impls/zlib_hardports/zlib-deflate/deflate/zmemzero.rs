pub fn zmemzero(buffer: &mut [u8], len: usize) {
    for i in 0..len {
        buffer[i] = 0;
    }
}
