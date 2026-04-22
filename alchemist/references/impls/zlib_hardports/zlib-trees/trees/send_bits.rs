pub fn send_bits(state: &mut DeflateState, value: u16, length: u8) {
    let len = length as u32;
    let val = value as u32;
    let valid = state.bi_valid as u32;
    if valid + len > 16 {
        state.bi_buf |= (val << valid) as u16;
        state.pending.push((state.bi_buf & 0xff) as u8);
        state.pending.push((state.bi_buf >> 8) as u8);
        state.bi_buf = (val >> (16 - valid)) as u16;
        state.bi_valid = ((valid + len) - 16) as i32;
    } else {
        state.bi_buf |= (val << valid) as u16;
        state.bi_valid = (valid + len) as i32;
    }
}