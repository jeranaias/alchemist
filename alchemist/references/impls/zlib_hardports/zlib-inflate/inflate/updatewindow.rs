pub fn updatewindow(strm: &mut InflateState, end: &[u8], copy: usize) -> Result<(), MemoryError> {
    // Port of inflate.c:updatewindow.
    // Copies up to wsize of `end`'s last `copy` bytes into the sliding
    // window at wnext. Used to maintain the 32KB window for back-references
    // across streaming decompression calls.

    // Lazy-allocate the window if empty.
    if strm.window.is_empty() {
        let size = 1usize << strm.wbits;
        strm.window = vec![0u8; size];
    }
    // Initialize wsize / wnext / whave on first use.
    if strm.wsize == 0 {
        strm.wsize = 1u32 << strm.wbits;
        strm.wnext = 0;
        strm.whave = 0;
    }

    let wsize = strm.wsize as usize;
    // If we have more than wsize bytes to copy, only the last wsize matter.
    let (src_start, copy) = if copy >= wsize {
        (end.len().saturating_sub(wsize), wsize)
    } else {
        (end.len().saturating_sub(copy), copy)
    };

    let dist = (wsize - strm.wnext as usize).min(copy);
    // Copy `dist` bytes from &end[src_start..] into window[wnext..wnext+dist]
    let wnext = strm.wnext as usize;
    for i in 0..dist {
        if src_start + i < end.len() && wnext + i < strm.window.len() {
            strm.window[wnext + i] = end[src_start + i];
        }
    }
    let remaining = copy - dist;
    if remaining > 0 {
        // Wrap: second half starts at window[0]
        for i in 0..remaining {
            if src_start + dist + i < end.len() && i < strm.window.len() {
                strm.window[i] = end[src_start + dist + i];
            }
        }
        strm.wnext = remaining as u32;
        strm.whave = wsize as u32;
    } else {
        strm.wnext = (strm.wnext + dist as u32) % strm.wsize;
        if strm.wnext == 0 {
            strm.whave = wsize as u32;
        } else if strm.whave < strm.wsize {
            strm.whave += dist as u32;
        }
    }
    Ok(())
}
