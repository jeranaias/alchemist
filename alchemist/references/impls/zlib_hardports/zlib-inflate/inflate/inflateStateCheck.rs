pub fn inflate_state_check(strm: &mut InflateStream) -> i32 {
    // Port of inflate.c:inflateStateCheck.
    // Returns 1 if invalid, 0 if valid (matches C's `int` return).
    // Rust's &mut reference guarantees non-null; check the mode against
    // the known range. Inflate mode values from inflate.h range roughly
    // HEAD=16180..SYNC (about 32 states).
    let mode = strm.state.mode;
    if mode > 16220 || mode < 16180 { 1 } else { 0 }
}
