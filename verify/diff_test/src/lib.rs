//! Differential test harness — compares Alchemist-generated Rust against C zlib.

use std::os::raw::{c_uchar, c_ulong, c_int};

// FFI declarations for C zlib functions
unsafe extern "C" {
    /// adler32(adler, buf, len) — RFC 1950 Adler-32 checksum
    pub fn adler32(adler: c_ulong, buf: *const c_uchar, len: u32) -> c_ulong;

    /// crc32(crc, buf, len) — RFC 1952 CRC-32
    pub fn crc32(crc: c_ulong, buf: *const c_uchar, len: u32) -> c_ulong;

    /// compress(dest, destLen, source, sourceLen) — basic compress
    pub fn compress(
        dest: *mut c_uchar,
        dest_len: *mut c_ulong,
        source: *const c_uchar,
        source_len: c_ulong,
    ) -> c_int;

    /// uncompress(dest, destLen, source, sourceLen) — basic decompress
    pub fn uncompress(
        dest: *mut c_uchar,
        dest_len: *mut c_ulong,
        source: *const c_uchar,
        source_len: c_ulong,
    ) -> c_int;
}

/// Wrapper: compute Adler-32 via C zlib for given byte slice.
pub fn c_adler32(data: &[u8]) -> u32 {
    unsafe { adler32(1, data.as_ptr(), data.len() as u32) as u32 }
}

/// Wrapper: compute CRC-32 via C zlib for given byte slice.
pub fn c_crc32(data: &[u8]) -> u32 {
    unsafe { crc32(0, data.as_ptr(), data.len() as u32) as u32 }
}

/// Wrapper: compress via C zlib. Returns compressed bytes.
pub fn c_compress(data: &[u8]) -> Vec<u8> {
    let mut dest_len: c_ulong = (data.len() + data.len() / 1000 + 12) as c_ulong;
    let mut dest = vec![0u8; dest_len as usize];
    let ret = unsafe {
        compress(
            dest.as_mut_ptr(),
            &mut dest_len,
            data.as_ptr(),
            data.len() as c_ulong,
        )
    };
    assert_eq!(ret, 0, "C compress failed: {}", ret);
    dest.truncate(dest_len as usize);
    dest
}

/// Wrapper: decompress via C zlib. expected_size must be at least decompressed length.
pub fn c_uncompress(compressed: &[u8], expected_size: usize) -> Vec<u8> {
    let mut dest_len: c_ulong = expected_size as c_ulong;
    let mut dest = vec![0u8; expected_size];
    let ret = unsafe {
        uncompress(
            dest.as_mut_ptr(),
            &mut dest_len,
            compressed.as_ptr(),
            compressed.len() as c_ulong,
        )
    };
    assert_eq!(ret, 0, "C uncompress failed: {}", ret);
    dest.truncate(dest_len as usize);
    dest
}

/// Wrapper: compute Adler-32 via Alchemist Rust.
pub fn rust_adler32(data: &[u8]) -> u32 {
    use zlib_checksum::Adler32;
    Adler32::compute(1, data, data.len())
}
