pub fn inflate_reset_keep(strm: &mut InflateStream) -> Result<(), InflateError> {
    // Port of inflate.c:inflateResetKeep.
    // Soft reset — keeps the allocated window/tables but zeroes the
    // parse state so decompression can start a new stream on the
    // same InflateStream instance.
    //
    // "Keep" means: preserve `wbits`, `window` allocation, `lencode`/
    // `distcode` slice origin. Only reset the state machine.

    // zlib's HEAD mode identifier. inflate_mode uses symbolic names; the
    // numeric values depend on the enum emission. HEAD is typically the
    // first variant (= 16180 based on zlib 1.3.x, or 0 in older releases).
    // The generated types use u32 mode, so write 16180 which matches the
    // zlib 1.3 public release.
    const HEAD: u32 = 16180;

    strm.total_in = 0;
    strm.total_out = 0;
    let state = &mut strm.state;
    state.total = 0;
    // Adler carry-over: when wrapping is enabled, strm.adler mirrors
    // (wrap & 1). Minimal reset: start adler fresh. Kept as 0 since
    // the wrap flag lives in state.wrap.
    if state.wrap != 0 {
        strm.adler = (state.wrap & 1) as u32;
    }
    state.mode = HEAD;
    state.last = false;
    state.havedict = false;
    state.flags = -1;
    state.dmax = 32768;
    state.head = 0;
    state.hold = 0;
    state.bits = 0;
    // Leave lencode/distcode/window untouched — that's the "keep" in
    // inflateResetKeep.
    Ok(())
}
