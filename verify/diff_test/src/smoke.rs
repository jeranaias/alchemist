//! Quick smoke test — does Alchemist's adler32 match C zlib's?

use diff_test::{c_adler32, rust_adler32, c_crc32, c_compress, c_uncompress};

fn main() {
    println!("=== Alchemist vs C zlib differential test ===\n");

    // Test 1: empty input
    let empty: &[u8] = &[];
    let c = c_adler32(empty);
    let r = rust_adler32(empty);
    println!("adler32 empty:    C=0x{:08x} Rust=0x{:08x}  {}", c, r, if c == r {"PASS"} else {"FAIL"});

    // Test 2: "Hello"
    let hello = b"Hello";
    let c = c_adler32(hello);
    let r = rust_adler32(hello);
    println!("adler32 'Hello':  C=0x{:08x} Rust=0x{:08x}  {}", c, r, if c == r {"PASS"} else {"FAIL"});

    // Test 3: 1-byte
    let one = &[0x42u8];
    let c = c_adler32(one);
    let r = rust_adler32(one);
    println!("adler32 [0x42]:   C=0x{:08x} Rust=0x{:08x}  {}", c, r, if c == r {"PASS"} else {"FAIL"});

    // Test 4: "wikipedia" (canonical RFC 1950 example, expected 0x11e60398)
    let wiki = b"Wikipedia";
    let c = c_adler32(wiki);
    let r = rust_adler32(wiki);
    println!("adler32 'Wikipedia': C=0x{:08x} Rust=0x{:08x}  {}", c, r, if c == r {"PASS"} else {"FAIL"});
    println!("  (RFC 1950 expected: 0x11e60398)");

    // Test 5: 1000 random bytes
    let mut data = vec![0u8; 1000];
    for (i, b) in data.iter_mut().enumerate() {
        *b = ((i * 37) % 256) as u8;
    }
    let c = c_adler32(&data);
    let r = rust_adler32(&data);
    println!("adler32 [1000B]:  C=0x{:08x} Rust=0x{:08x}  {}", c, r, if c == r {"PASS"} else {"FAIL"});

    // CRC-32 sanity (we don't have a Rust binding for this yet, just print the C result)
    println!("\ncrc32 'Hello':     C=0x{:08x}  (expected 0xf7d18982)", c_crc32(hello));

    // C round-trip sanity (proves C zlib FFI works)
    let original = b"The quick brown fox jumps over the lazy dog";
    let compressed = c_compress(original);
    let decompressed = c_uncompress(&compressed, 1024);
    println!("\nC compress/uncompress roundtrip: {}", if decompressed == original {"PASS"} else {"FAIL"});
    println!("  Original: {} bytes, compressed: {} bytes", original.len(), compressed.len());
}
