pub fn slide_hash(s: &mut DeflateState) {
    // Port of deflate.c:slide_hash.
    // After w_size bytes of input have been processed, slide the hash
    // tables by w_size. Any position that would underflow (was less
    // than w_size) becomes NIL (0) — the pre-window sentinel.
    //
    // Both `head` (hash_size entries) and `prev` (w_size entries) get
    // the same slide treatment. NIL = 0 per zlib's deflate.h.
    const NIL: u16 = 0;
    let wsize = s.w_size as u16;

    let n = s.hash_size as usize;
    let head_len = s.head.len().min(n);
    for i in 0..head_len {
        let m = s.head[i];
        s.head[i] = if m >= wsize { m - wsize } else { NIL };
    }

    let n = s.w_size;
    let prev_len = s.prev.len().min(n);
    for i in 0..prev_len {
        let m = s.prev[i];
        s.prev[i] = if m >= wsize { m - wsize } else { NIL };
    }
}
