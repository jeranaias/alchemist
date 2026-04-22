pub fn buildtables() {
    // Port of inflate.c:makefixed / buildtables.
    // In the C code, this is a BUILD-TIME utility that prints the fixed
    // inflate tables (the ones inflate_fixed installs at runtime) to
    // stdout for inclusion in inffixed.h. Not part of the decompression
    // runtime. In the Rust port it's a no-op — fixed tables are built
    // inline by inflate_fixed when needed.
}
