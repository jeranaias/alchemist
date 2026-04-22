pub fn inflate_fixed(state: &mut InflateState) {
    // Port of inflate.c:inflate_fixed helper — populates lencode/distcode
    // with the fixed Huffman tables per RFC 1951 §3.2.6.
    //
    // Fixed literal/length tree (LITERAL):
    //   0-143:  8 bits, codes 00110000..10111111
    //   144-255: 9 bits, codes 110010000..111111111
    //   256-279: 7 bits, codes 0000000..0010111
    //   280-287: 8 bits, codes 11000000..11000111
    //
    // Fixed distance tree:
    //   0-31: 5 bits each (all 32 distance codes the same length)
    //
    // Our Rust port computes the tables inline. CodeEntry is
    // (op: u8, bits: u8, val: u16) per the types.
    let mut lenfix: Vec<(u8, u8, u16)> = Vec::with_capacity(512);
    for sym in 0..288 {
        let bits = if sym < 144 { 8 }
                   else if sym < 256 { 9 }
                   else if sym < 280 { 7 }
                   else { 8 };
        // op=0 means literal-or-length; inflate_table fills the actual op
        // field with LITERAL/LENGTH/END_BLOCK/INVALID based on sym range.
        // Rough equivalent:
        let op = if sym < 256 { 0u8 } else if sym == 256 { 32u8 } else { 16u8 };
        lenfix.push((op, bits, sym as u16));
    }
    let distfix: Vec<(u8, u8, u16)> =
        (0..32).map(|sym: u16| (16u8, 5u8, sym)).collect();

    state.lencode = lenfix;
    state.distcode = distfix;
    state.lenbits = 9;
    state.distbits = 5;
}
