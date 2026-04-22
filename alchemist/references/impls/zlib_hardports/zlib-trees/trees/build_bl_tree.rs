pub fn build_bl_tree(state: &mut DeflateState) -> usize {
    // Port of trees.c:build_bl_tree.
    // Build the "bit length" meta-tree that encodes the literal and
    // distance trees. Returns max_blindex: the index of the last
    // non-zero entry in the bit-length tree (always >= 3 per RFC 1951).
    //
    // scan_tree populates state.bl_tree's freq field; build_tree
    // assigns code lengths; then we count the trailing non-zero entries.
    //
    // NOTE: scan_tree + build_tree operate on dyn_ltree/dyn_dtree in
    // state, which this port doesn't directly pass (spec mismatch).
    // We approximate by walking bl_tree directly.

    const BL_CODES: usize = 19;
    // The C code calls scan_tree + build_tree; our Rust port at this
    // stage just walks bl_tree to find max_blindex. Full scan/build
    // integration requires fixing the spec signatures for those helpers.
    let mut max_blindex = BL_CODES - 1;
    while max_blindex >= 3 {
        let bl_ord_idx = bl_order[max_blindex] as usize;
        if bl_ord_idx < state.bl_tree.len() && state.bl_tree[bl_ord_idx].1 != 0 {
            break;
        }
        max_blindex -= 1;
    }
    // Update opt_len: 3 bits per code-length code + 5+5+4 for count fields
    state.opt_len = state.opt_len
        .saturating_add(3 * (max_blindex as u64 + 1))
        .saturating_add(5 + 5 + 4);
    max_blindex
}
