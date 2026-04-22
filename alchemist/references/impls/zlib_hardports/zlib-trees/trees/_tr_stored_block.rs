pub fn _tr_stored_block(state: &mut DeflateState, buf: &[u8], stored_len: u32, last: bool) {
    // Port of trees.c:_tr_stored_block.
    // Writes a non-compressed (stored) block to the bitstream.
    // Block layout per RFC 1951 §3.2.4:
    //   - 3 bits: block header (1 bit BFINAL, 2 bits BTYPE=0)
    //   - align to byte boundary
    //   - 2 bytes: LEN (little-endian)
    //   - 2 bytes: NLEN (one's complement of LEN, for redundancy check)
    //   - LEN bytes: raw data

    // Block type STORED = 0; combine with BFINAL.
    let header: u16 = (if last { 1 } else { 0 }) << 0;
    send_bits(state, header, 3);
    bi_windup(state);

    // Write LEN (u16 little-endian)
    let len = stored_len as u16;
    state.pending.push((len & 0xFF) as u8);
    state.pending.push(((len >> 8) & 0xFF) as u8);
    // Write NLEN
    let nlen = !len;
    state.pending.push((nlen & 0xFF) as u8);
    state.pending.push(((nlen >> 8) & 0xFF) as u8);

    // Copy `stored_len` bytes from `buf`
    let n = (stored_len as usize).min(buf.len());
    for i in 0..n {
        state.pending.push(buf[i]);
    }
}
