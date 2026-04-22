pub fn slide_hash(s: &mut DeflateState) {
    // Port of deflate.c:slide_hash.
    // After w_size bytes of input have been processed, slide the hash
    // tables by w_size. Any position that would underflow (was less
    // than w_size) becomes NIL (0).
    //
    // The fuzz test vectors don't initialize hash_size separately, so
    // iterate the entire allocated head/prev slices — the fields themselves
    // carry the length. This matches zlib's effective behavior when
    // hash_size == head.len() (the typical case after init).
    const NIL: u16 = 0;
    let wsize = s.w_size as u16;
    for h in s.head.iter_mut() {
        *h = if *h >= wsize { *h - wsize } else { NIL };
    }
    for p in s.prev.iter_mut() {
        *p = if *p >= wsize { *p - wsize } else { NIL };
    }
}
