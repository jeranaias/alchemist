pub fn inflatePrime(strm: &mut InflateState, bits: i32, value: u32) -> Result<(), InflateError> {
    // Port of inflate.c:inflatePrime.
    // Prepends `bits` bits of `value` into the inflater's bit buffer so
    // decompression can resume from a non-byte-aligned point. Used by
    // raw inflate to handle custom entry points.
    //
    // Validity checks (C semantics):
    //   - bits == 0  → Ok(())
    //   - bits < 0   → clear hold & bits, Ok(())
    //   - bits > 16  → StreamError
    //   - bits + state.bits > 32  → StreamError (hold is u32-sized)
    //   - otherwise: OR value into hold at current bit offset, advance bits.
    if bits == 0 {
        return Ok(());
    }
    if bits < 0 {
        strm.hold = 0;
        strm.bits = 0;
        return Ok(());
    }
    if bits > 16 || strm.bits.saturating_add(bits as u32) > 32 {
        return Err(InflateError::default()); // Z_STREAM_ERROR
    }
    let value = value & ((1u32 << (bits as u32)) - 1);
    strm.hold = strm.hold.wrapping_add((value as u64) << strm.bits);
    strm.bits = strm.bits.saturating_add(bits as u32);
    Ok(())
}
