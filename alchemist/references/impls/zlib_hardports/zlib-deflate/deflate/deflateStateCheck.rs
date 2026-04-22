pub fn deflateStateCheck(strm: &mut DeflateStream) -> bool {
    // Port of deflate.c:deflateStateCheck.
    // Returns true if the stream is INVALID (matches C's "return 1 on bad").
    // Valid status values from deflate.h. &mut DeflateStream guarantees
    // non-null; the generated DeflateStream owns its DeflateState (not
    // Option), so we check the status field directly.
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
    !VALID_STATES.contains(&strm.state.status)
}
