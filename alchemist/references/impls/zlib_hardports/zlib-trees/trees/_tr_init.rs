pub fn _tr_init(s: &mut DeflateState) {
    // Port of trees.c:_tr_init.
    // Initializes (or resets) the deflate state's tree-related fields
    // before a new block. In C, also assigns `l_desc`/`d_desc`/`bl_desc`
    // pointers to their respective dynamic trees and static descriptors;
    // our Rust port stores those as u32 indices (or no-op for static
    // tables baked at const-time), so we clear the bit buffer and kick
    // init_block to zero per-block state.
    s.bi_buf = 0;
    s.bi_valid = 0;
    init_block(s);
}
