//! Property-based differential tests: Alchemist Rust vs C zlib.

use diff_test::{c_adler32, rust_adler32, c_crc32};
use proptest::prelude::*;

proptest! {
    #![proptest_config(ProptestConfig::with_cases(10_000))]

    #[test]
    fn adler32_matches_c_zlib(data in proptest::collection::vec(any::<u8>(), 0..16384)) {
        let c_result = c_adler32(&data);
        let r_result = rust_adler32(&data);
        prop_assert_eq!(c_result, r_result, "adler32 mismatch on {} bytes", data.len());
    }

    #[test]
    fn adler32_empty_is_one(_n in 0u8..=10u8) {
        prop_assert_eq!(rust_adler32(&[]), 1);
        prop_assert_eq!(c_adler32(&[]), 1);
    }

    #[test]
    fn adler32_matches_c_zlib_long(
        data in proptest::collection::vec(any::<u8>(), 16384..65536)
    ) {
        let c_result = c_adler32(&data);
        let r_result = rust_adler32(&data);
        prop_assert_eq!(c_result, r_result, "adler32 mismatch on {} bytes (long)", data.len());
    }
}

// Known test vectors from RFC 1950 and various sources
#[test]
fn rfc1950_test_vectors() {
    // Empty
    assert_eq!(rust_adler32(b""), 0x00000001);
    // Single byte 0x00
    assert_eq!(rust_adler32(&[0]), 0x00010001);
    // "Wikipedia" — canonical
    assert_eq!(rust_adler32(b"Wikipedia"), 0x11e60398);
}
