pub fn send_tree(state: &mut DeflateState, tree: &[TreeElement], max_code: usize) {
    // Port of trees.c:send_tree.
    // Sends the bit lengths of a Huffman tree, encoded via bl_tree
    // using the RLE codes defined in RFC 1951 §3.2.7. Mirrors scan_tree
    // exactly but calls send_code/send_bits instead of incrementing Freq.
    //
    // bl_tree indices:
    //   REP_3_6 = 16  (repeat previous 3-6 times, 2 extra bits)
    //   REPZ_3_10 = 17  (repeat zero 3-10 times, 3 extra bits)
    //   REPZ_11_138 = 18  (repeat zero 11-138 times, 7 extra bits)

    const REP_3_6_IDX: usize = 16;
    const REPZ_3_10_IDX: usize = 17;
    const REPZ_11_138_IDX: usize = 18;

    let send_code = |state: &mut DeflateState, idx: usize| {
        if idx < state.bl_tree.len() {
            // bl_tree is (code, len) — send `code` as `len` bits via send_bits.
            // (If tuple ordering is (freq, len), this still works after
            //  gen_codes fills the .code slot; here we read whatever is stored
            //  as the first field — by convention after gen_codes, that's code.)
            let (val, bits) = state.bl_tree[idx];
            send_bits(state, val, bits as u8);
        }
    };

    let mut prevlen: i32 = -1;
    let mut nextlen: u8 = if !tree.is_empty() { tree[0].len } else { 0 };
    let mut count: i32 = 0;
    let mut max_count: i32 = 7;
    let mut min_count: i32 = 4;
    if nextlen == 0 {
        max_count = 138;
        min_count = 3;
    }

    for n in 0..=max_code {
        let curlen = nextlen;
        nextlen = if n + 1 < tree.len() { tree[n + 1].len } else { 0xFF };
        count += 1;
        if count < max_count && curlen == nextlen {
            continue;
        } else if count < min_count {
            while count > 0 {
                send_code(state, curlen as usize);
                count -= 1;
            }
        } else if curlen != 0 {
            if (curlen as i32) != prevlen {
                send_code(state, curlen as usize);
                count -= 1;
            }
            send_code(state, REP_3_6_IDX);
            send_bits(state, (count - 3) as u16, 2);
        } else if count <= 10 {
            send_code(state, REPZ_3_10_IDX);
            send_bits(state, (count - 3) as u16, 3);
        } else {
            send_code(state, REPZ_11_138_IDX);
            send_bits(state, (count - 11) as u16, 7);
        }
        count = 0;
        prevlen = curlen as i32;
        if nextlen == 0 {
            max_count = 138;
            min_count = 3;
        } else if curlen == nextlen {
            max_count = 6;
            min_count = 3;
        } else {
            max_count = 7;
            min_count = 4;
        }
    }
}
