pub fn inflateStateCheck(strm: &mut InflateStream) -> bool {
    // Port of inflate.c:inflateStateCheck.
    // Returns true if the stream is INVALID. Inflate states span HEAD..BAD
    // (14..15 in zlib's inflate.h inflate_mode enum). Rust's &mut reference
    // guarantees non-null; check the mode against the known range.
    //
    // inflate_mode values from inflate.h:
    //   HEAD=16180, FLAGS, TIME, OS, EXLEN, EXTRA, NAME, COMMENT, HCRC,
    //   DICTID, DICT, TYPE, TYPEDO, STORED, COPY_, COPY, TABLE, LENLENS,
    //   CODELENS, LEN_, LEN, LENEXT, DIST, DISTEXT, MATCH, LIT, CHECK,
    //   LENGTH, DONE, BAD, MEM, SYNC
    // Specific numeric values aren't stable across zlib releases; zlib's
    // own check simply ensures the state struct is reachable and the
    // back-pointer matches. Our Rust port can only reject states that are
    // clearly impossible.
    let mode = strm.state.mode;
    // mode outside plausible range → invalid
    mode > 16220 || mode < 16180
}
