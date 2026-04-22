pub fn zcalloc(
    _opaque: Option<&dyn std::any::Any>,
    items: usize,
    size: usize,
) -> Box<[u8]> {
    // Port of zutil.c:zcalloc.
    // Safe-Rust equivalent of `calloc(items, size)`: allocate
    // items*size bytes, zero-initialized, return boxed slice.
    // The `opaque` parameter is the C allocator-callback context; Rust
    // uses the global allocator so we ignore it. All Alchemist-generated
    // code assumes Vec<u8>/Box<[u8]> backing.
    let total = items.saturating_mul(size);
    vec![0u8; total].into_boxed_slice()
}
