pub fn scan_tree(state: &mut DeflateState, tree: &[TreeElement], max_code: usize) {
    // Port of trees.c:scan_tree.
    // Count frequencies of run-length codes that will be emitted by
    // send_tree. We update state.bl_tree[].freq for entries
    // REP_3_6 (16), REPZ_3_10 (17), REPZ_11_138 (18), and the
    // base-length entries. The C version writes a sentinel at
    // tree[max_code+1], but our Rust signature gets &[TreeElement]
    // (immutable) — we bounds-check instead.

    // bl_tree indices (from trees.h constants already in the skeleton)
    const REP_3_6_IDX: usize = 16;
    const REPZ_3_10_IDX: usize = 17;
    const REPZ_11_138_IDX: usize = 18;

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
        nextlen = if n + 1 < tree.len() { tree[n + 1].len } else { 0xFF }; // sentinel
        count += 1;
        if count < max_count && curlen == nextlen {
            continue;
        } else if count < min_count {
            let idx = curlen as usize;
            if idx < state.bl_tree.len() {
                state.bl_tree[idx].0 = state.bl_tree[idx].0.saturating_add(count as u16);
            }
        } else if curlen != 0 {
            if (curlen as i32) != prevlen {
                let idx = curlen as usize;
                if idx < state.bl_tree.len() {
                    state.bl_tree[idx].0 = state.bl_tree[idx].0.saturating_add(1);
                }
            }
            state.bl_tree[REP_3_6_IDX].0 = state.bl_tree[REP_3_6_IDX].0.saturating_add(1);
        } else if count <= 10 {
            state.bl_tree[REPZ_3_10_IDX].0 = state.bl_tree[REPZ_3_10_IDX].0.saturating_add(1);
        } else {
            state.bl_tree[REPZ_11_138_IDX].0 = state.bl_tree[REPZ_11_138_IDX].0.saturating_add(1);
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
