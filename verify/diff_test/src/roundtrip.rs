//! Roundtrip verification — does our Rust actually compress data?

use diff_test::{c_compress, c_uncompress};

fn main() {
    let test_data = b"The quick brown fox jumps over the lazy dog. \
                       This is a test to see if our Rust DEFLATE implementation \
                       actually works. Repeat repeat repeat repeat repeat repeat.";

    println!("=== Testing Rust compress() ===\n");
    println!("Input: {} bytes\n", test_data.len());

    // Try to call our Rust compress
    let mut dest = vec![0u8; 1024];
    let mut dest_len = 1024usize;

    use zlib_compression::compress::compress;

    match compress(&mut dest, &mut dest_len, test_data, test_data.len(), 6) {
        Ok(()) => {
            println!("Rust compress returned Ok");
            println!("dest_len after: {}", dest_len);
            println!("First 32 bytes of dest: {:02x?}", &dest[..32.min(dest.len())]);

            // Try to decompress with C
            if dest_len > 0 {
                let compressed = &dest[..dest_len];
                println!("\nAttempting C uncompress on Rust output...");
                let decompressed = c_uncompress(compressed, test_data.len() * 2);
                if decompressed == test_data {
                    println!("ROUNDTRIP PASS");
                } else {
                    println!("ROUNDTRIP FAIL: decompressed != original");
                    println!("Got {} bytes, expected {}", decompressed.len(), test_data.len());
                }
            } else {
                println!("\ndest_len is 0 — Rust didn't actually compress anything");
            }
        }
        Err(e) => {
            println!("Rust compress FAILED: {:?}", e);
        }
    }

    // Also test: C compress → Rust decompress
    println!("\n=== Testing C compress → Rust decompress ===\n");
    let c_compressed = c_compress(test_data);
    println!("C produced {} compressed bytes", c_compressed.len());

    use zlib_compression::uncompr::uncompr as uncompress;
    let mut rust_dest = vec![0u8; test_data.len() * 2];
    let mut rust_dest_len = rust_dest.len();
    match uncompress(&mut rust_dest, &mut rust_dest_len, &c_compressed, c_compressed.len()) {
        Ok(()) => {
            rust_dest.truncate(rust_dest_len);
            if rust_dest == test_data {
                println!("Rust decompress of C output PASS");
            } else {
                println!("Rust decompress: got {} bytes, expected {}", rust_dest.len(), test_data.len());
                println!("First 32 of result: {:02x?}", &rust_dest[..32.min(rust_dest.len())]);
            }
        }
        Err(e) => {
            println!("Rust uncompress FAILED: {:?}", e);
        }
    }
}
