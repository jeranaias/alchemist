pub fn zlibCompileFlags() -> u32 {
    // Port of zutil.c:zlibCompileFlags.
    // Returns a bitfield describing the compile-time type sizes + feature
    // flags of the Rust build. Bits 0-1: sizeof(uInt), 2-3: sizeof(uLong),
    // 4-5: sizeof(voidpf), 6-7: sizeof(z_off_t). Other bits flag debug/ZLIB_WINAPI/
    // gzprintf variants; we set them to 0 (Rust build: no WINAPI, no printf
    // variants, no debug macros).
    //
    // The ..00 ..01 ..10 ..11 table maps (byte_size / 2 - 1) for the
    // four primary types. usize = pointer-size.
    let enc = |n: usize| -> u32 {
        match n {
            2 => 0, 4 => 1, 8 => 2, 16 => 3,
            _ => 3, // safest fallback for unusual targets
        }
    };
    let u_int_bits = enc(core::mem::size_of::<u32>());
    let u_long_bits = enc(core::mem::size_of::<u64>());
    let voidpf_bits = enc(core::mem::size_of::<usize>());
    let z_off_bits = enc(core::mem::size_of::<i64>());
    u_int_bits
        | (u_long_bits << 2)
        | (voidpf_bits << 4)
        | (z_off_bits << 6)
}
