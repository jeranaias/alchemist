pub fn crc32_combine_op(crc1: u32, crc2: u32, op: u32) -> u32 {
    if op == 0 {
        return 0;
    }
    multmodp(op, crc1) ^ crc2
}
