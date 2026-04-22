pub fn compressBound_z(sourceLen: usize) -> Option<usize> {
    // Port of compress.c:compressBound.
    // Returns the upper bound on the compressed size of `sourceLen` bytes.
    // Formula: sourceLen + (sourceLen / 1000) + 12 + 6 (zlib header + trailer
    // for deflate-with-zlib-wrapper).
    // Uses checked arithmetic to avoid overflow on pathological inputs.
    let q = sourceLen.checked_div(1000)?;
    sourceLen.checked_add(q)?.checked_add(12)?.checked_add(6)
}
