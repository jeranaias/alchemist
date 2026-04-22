pub fn zcfree(
    _opaque: Option<&mut dyn std::any::Any>,
    _ptr: Box<dyn std::any::Any>,
) {
    // Port of zutil.c:zcfree.
    // No-op in safe Rust: the Box is dropped when this function returns,
    // deallocating the buffer via the global allocator. The C code calls
    // free(ptr); taking ownership by-value here is the equivalent.
}
