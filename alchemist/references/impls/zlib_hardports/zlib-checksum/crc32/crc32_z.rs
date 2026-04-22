pub fn crc32_z(crc: u32, buf: &[u8], len: usize) -> u32 {
    let mut c = !crc;
    let n = len.min(buf.len());
    for i in 0..n {
        let idx = (c ^ buf[i] as u32) as u8;
        c = CRC32_TABLE[idx as usize] ^ (c >> 8);
    }
    !c
}