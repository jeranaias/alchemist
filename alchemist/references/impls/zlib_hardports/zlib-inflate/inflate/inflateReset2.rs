pub fn inflateReset2(stream: &mut InflateState, window_bits: i32) -> Result<(), InflateError> {
    // Port of inflate.c:inflateReset2.
    // Like inflateReset but also (re)configures the window-bits setting.
    //
    // window_bits interpretation (from zlib docs):
    //    0..7       → invalid (raw deflate requires >=8, zlib wrap too)
    //    8..15      → zlib wrapper (RFC 1950), window = 2^bits
    //    -8..-15    → raw deflate (no zlib wrapper)
    //    24..31     → gzip decoding + window = 2^(bits-16)
    //    40..47     → auto-detect (gzip or zlib), window = 2^(bits-32)
    //
    // Set wrap flag and wbits accordingly, then delegate to inflateReset.

    let (wrap, wbits): (i32, u32) = match window_bits {
        0 => (0, 15),                              // use whatever bits were set previously
        8..=15 => (1, window_bits as u32),         // zlib format
        bits if (-15..=-8).contains(&bits) => (0, (-bits) as u32),  // raw
        24..=31 => (2, (window_bits - 16) as u32), // gzip
        40..=47 => (3, (window_bits - 32) as u32), // auto-detect
        _ => return Err(InflateError::default()),  // Z_STREAM_ERROR
    };

    stream.wrap = wrap;
    stream.wbits = wbits;
    inflateReset(stream)
}
