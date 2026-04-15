fn main() {
    // Tell rustc where to find the C zlib import library
    println!("cargo:rustc-link-search=native=C:/Users/jesse/projects/alchemist/verify");
    // Link against z (libz.dll.a → zlib1.dll)
    println!("cargo:rustc-link-lib=dylib=z");
}
