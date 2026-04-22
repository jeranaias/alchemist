pub fn bi_windup(state: &mut DeflateState) {
    if state.bi_valid > 8 {
        state.pending.push((state.bi_buf & 0xff) as u8);
        state.pending.push((state.bi_buf >> 8) as u8);
    } else if state.bi_valid > 0 {
        state.pending.push((state.bi_buf & 0xff) as u8);
    }
    state.bi_buf = 0;
    state.bi_valid = 0;
}