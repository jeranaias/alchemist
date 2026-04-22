pub fn crc32(crc: u32, buf: &[u8], len: usize) -> u32 {
    // Port of the simple crc32 variant (crc32.c, non-_z shape).
    // Same byte-at-a-time IEEE 802.3 loop as crc32_z; differs only in
    // the signature used by zlib's public C API.
    let mut c = !crc;
    let n = len.min(buf.len());
    for i in 0..n {
        let idx = (c ^ buf[i] as u32) as u8;
        c = CRC32_TABLE[idx as usize] ^ (c >> 8);
    }
    !c
}
