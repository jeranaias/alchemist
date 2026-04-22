pub fn tr_static_init() {
    // Port of trees.c:_tr_static_init.
    // In C, this function lazily builds static Huffman tree tables at
    // first call (static_ltree, static_dtree, _dist_code, _length_code).
    // In the Rust port these tables are compile-time constants (see
    // the zlib-types crate's `bl_order` and friends — the full tables
    // can be injected via the constants extractor in a later pass).
    //
    // With const tables, initialization is a no-op.
}
