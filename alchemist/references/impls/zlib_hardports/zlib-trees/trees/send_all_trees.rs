pub fn send_all_trees(state: &mut DeflateState, lcodes: u32, dcodes: u32, blcodes: u32) {
    // Port of trees.c:send_all_trees.
    // Sends the header of a DYNAMIC_HUFFMAN block: the literal tree
    // description, distance tree description, and the bit-length tree
    // that compresses the other two.
    //
    // Wire format (RFC 1951 §3.2.7):
    //   5 bits: HLIT (lcodes - 257)
    //   5 bits: HDIST (dcodes - 1)
    //   4 bits: HCLEN (blcodes - 4)
    //   HCLEN*3 bits: code lengths for the bit-length tree in bl_order order
    //   encoded literal tree (via send_tree)
    //   encoded distance tree (via send_tree)

    send_bits(state, (lcodes - 257) as u16, 5);
    send_bits(state, (dcodes - 1) as u16, 5);
    send_bits(state, (blcodes - 4) as u16, 4);

    // bl_order is the permutation that improves compressibility of the
    // bit-length tree itself; it's a crate-level const in trees.rs skeleton.
    // Walk the first `blcodes` entries, send each code length as 3 bits.
    for rank in 0..(blcodes as usize) {
        let idx = bl_order[rank] as usize;
        let blen = if idx < state.bl_tree.len() {
            state.bl_tree[idx].1 as u16
        } else {
            0
        };
        send_bits(state, blen, 3);
    }

    // Delegate to send_tree for the literal and distance trees.
    // scan_tree + send_tree already populated bl_tree's codes via gen_codes.
    // Note: the generated TreeElement type carries the bit lengths these
    // callers expect to find in dyn_ltree / dyn_dtree.
    // (Callers wire the trees through state.dyn_ltree / state.dyn_dtree.)
    // We can't pass state.dyn_ltree as &[TreeElement] inside this same
    // method because of borrowing (send_tree takes &mut state). So we
    // clone the lengths into a temporary TreeElement vec.
    let lit: Vec<TreeElement> = state.dyn_ltree.iter()
        .map(|(freq, len)| TreeElement { freq: *freq, code: 0, len: *len as u8 })
        .collect();
    send_tree(state, &lit, (lcodes as usize) - 1);

    let dist: Vec<TreeElement> = state.dyn_dtree.iter()
        .map(|(freq, len)| TreeElement { freq: *freq, code: 0, len: *len as u8 })
        .collect();
    send_tree(state, &dist, (dcodes as usize) - 1);
}
