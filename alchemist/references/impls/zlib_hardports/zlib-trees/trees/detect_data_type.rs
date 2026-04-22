pub fn detect_data_type(s: &DeflateState) -> u8 {
    // Port of trees.c:detect_data_type.
    // Classify the pending block as binary (0), text (1), or unknown (2).
    // Algorithm (RFC 1950 is silent; zlib's heuristic):
    //   - If any frequency for a non-text control byte (0-31 except 9,10,13)
    //     is non-zero → BINARY.
    //   - Else, if any text byte (9,10,13 or 32+) has frequency → TEXT.
    //   - Else → UNKNOWN.
    //
    // In the Rust generated types, dyn_ltree is Vec<(u16, u16)> = (freq, len).
    // Walk [0..31] for binary control chars (mask 0xF3FFC07F per zlib),
    // then [32..LITERALS=256] for text.

    // Block-specific bitmask: each bit N means "byte N counts as BINARY".
    // The mask 0xF3FFC07F = 1111_0011_1111_1111_1100_0000_0111_1111 in
    // bits 0..31 — all control chars EXCEPT 9 (tab), 10 (LF), 13 (CR).
    let block_mask: u32 = 0xF3FF_C07F;
    let mut mask = block_mask;
    let mut n: usize = 0;
    while mask != 0 && n < 32 {
        if (mask & 1) != 0 && n < s.dyn_ltree.len() && s.dyn_ltree[n].0 != 0 {
            return 0; // Z_BINARY
        }
        mask >>= 1;
        n += 1;
    }
    // Text bytes present? Tab/LF/CR + byte 32 and above (LITERALS = 256).
    for idx in [9usize, 10, 13] {
        if idx < s.dyn_ltree.len() && s.dyn_ltree[idx].0 != 0 {
            return 1; // Z_TEXT
        }
    }
    let mut n = 32usize;
    let upper = s.dyn_ltree.len().min(256);
    while n < upper {
        if s.dyn_ltree[n].0 != 0 {
            return 1;
        }
        n += 1;
    }
    2 // Z_UNKNOWN
}
