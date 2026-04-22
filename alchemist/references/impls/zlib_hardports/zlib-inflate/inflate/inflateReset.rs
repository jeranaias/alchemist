pub fn inflate_reset(strm: &mut InflateState) -> Result<(), InflateError> {
    // Port of inflate.c:inflateReset.
    // Hard reset including wsize/whave/wnext. Caller uses this to
    // fully restart decompression (flushes window state). The keep-less
    // variant: wsize=0, whave=0, wnext=0, THEN call inflateResetKeep.
    strm.wsize = 0;
    strm.whave = 0;
    strm.wnext = 0;
    // Leave window buffer allocated — just reset the logical view.
    // Mode reset to HEAD (matches inflateResetKeep).
    const HEAD: u32 = 16180;
    strm.total = 0;
    strm.mode = HEAD;
    strm.last = false;
    strm.havedict = false;
    strm.flags = -1;
    strm.dmax = 32768;
    strm.head = 0;
    strm.hold = 0;
    strm.bits = 0;
    Ok(())
}
