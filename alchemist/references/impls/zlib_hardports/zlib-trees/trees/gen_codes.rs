pub fn gen_codes(tree: &mut [HuffmanNode], max_code: usize, bl_count: &[u16]) {
    // Port of trees.c:gen_codes.
    // Generates canonical Huffman codes from bit-lengths (per RFC 1951).
    //
    // Algorithm (RFC 1951 §3.2.2 step 3-4):
    //   1. For each code length L, compute the smallest code starting that L.
    //      next_code[L] = (next_code[L-1] + bl_count[L-1]) << 1
    //   2. Assign code to each symbol:
    //      code[i] = bi_reverse(next_code[tree[i].Len]++, tree[i].Len)
    //
    // HuffmanNode has .freq and .len fields per the generated types.
    // We add to it a .code field via the shared-type extractor — but the
    // generated struct here is likely just (freq: u16, len: u16). We emit
    // the codes by mutating .len in-place? No — Len is the input; Code is
    // the output. In zlib's ct_data struct, both are stored. Our Rust port
    // may have a 3-tuple or explicit fields. Assume HuffmanNode has:
    //     pub freq_or_code: u16,
    //     pub len: u16,
    // where freq_or_code is union-used: frequency on input, code on output.
    // Some variants use separate .code. Adjust if generated type differs.

    const MAX_BITS: usize = 15;
    let mut next_code = [0u32; MAX_BITS + 1];
    let mut code: u32 = 0;

    // Build next_code[]: the smallest code that any symbol of that length
    // can have. Iteration preserves C's (code + bl_count[b-1]) << 1.
    for bits in 1..=MAX_BITS {
        let prev = bl_count.get(bits - 1).copied().unwrap_or(0) as u32;
        code = (code + prev) << 1;
        next_code[bits] = code;
    }

    // Assign codes. tree[n].len is the bit-length (input). Write to .code.
    for n in 0..=max_code.min(tree.len().saturating_sub(1)) {
        let len = tree[n].len as usize;
        if len != 0 && len < next_code.len() {
            let c = next_code[len];
            next_code[len] = c + 1;
            // Reverse bits of `c` over `len` bits (per RFC 1951).
            tree[n].code = bi_reverse(c, len as u8) as u16;
        }
    }
}
