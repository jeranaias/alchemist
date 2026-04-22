pub fn deflate_state_check(strm: &mut DeflateStream) -> i32 {
    // Port of deflate.c:deflateStateCheck.
    // Returns 1 if the stream is INVALID, 0 if valid (matches C's `int`
    // return convention). &mut DeflateStream guarantees non-null; the
    // generated DeflateStream owns its DeflateState (not Option), so we
    // check the status field directly.
    const VALID_STATES: &[i32] = &[
        42,   // INIT_STATE
        57,   // GZIP_STATE
        69,   // EXTRA_STATE
        73,   // NAME_STATE
        91,   // COMMENT_STATE
        103,  // HCRC_STATE
        113,  // BUSY_STATE
        666,  // FINISH_STATE
    ];
    if VALID_STATES.contains(&strm.state.status) { 0 } else { 1 }
}
