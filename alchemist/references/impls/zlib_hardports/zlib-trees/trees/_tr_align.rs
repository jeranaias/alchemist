pub fn _tr_align(s: &mut DeflateState) {
    // Port of trees.c:_tr_align. Emits a STATIC block header (3 bits)
    // followed by the END_BLOCK code from static_ltree, then byte-aligns
    // the bit buffer via bi_flush.
    //
    // Values per zlib's static_ltree table (RFC 1951 fixed Huffman):
    //   STATIC_TREES = 1; header = STATIC_TREES << 1 = 2 in 3 bits
    //   END_BLOCK = 256; static_ltree[256] = { code: 0, len: 7 }
    send_bits(s, 2u16, 3);
    send_bits(s, 0u16, 7);
    bi_flush(s);
}
