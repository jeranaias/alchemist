# Build script for the zlib C shims (deflate + inflate).
#
# The shims amalgamate zlib source files so Alchemist can observe and
# mutate zlib's internal state via FFI during test-vector fuzzing.
#
# Runs from a shim-build directory (typically a copy of this folder next
# to the zlib source tree). ZLIB_SRC points at the tree holding zlib's
# .c/.h files. BUILD_DIR is where the .o / .dll artifacts land.
#
# Default layout (what the Alchemist pipeline expects):
#   subjects/zlib/          <- upstream zlib source (.c, .h)
#   subjects/zlib/shim/     <- BUILD_DIR; Makefile + shim .c copied from
#                              alchemist/references/shims/zlib/
#
# Usage (from subjects/zlib/shim/):
#   cp ../../../alchemist/references/shims/zlib/* .
#   mingw32-make -f Makefile

CC := gcc
CFLAGS := -O2 -I ../
LDFLAGS := -shared
IMPLIB_FLAG = -Wl,--out-implib,$@.a

.PHONY: all clean

all: zlib_state_shim.dll zlib_inflate_shim.dll

# --- Deflate shim (amalgamated into a single CU) ---

zlib_state_shim.dll: zlib_state_shim.c
	$(CC) $(LDFLAGS) -o $@ $(CFLAGS) $< $(IMPLIB_FLAG)

# --- Inflate shim ---
# Inflate headers lack include guards, so amalgamation double-defines
# struct inflate_state. Instead compile inffast.o / inftrees.o / zutil.o /
# adler32.o / crc32.o as separate objects and link them with the shim.

INFLATE_SHIM_OBJS := inffast.o inftrees.o zutil.o adler32.o crc32.o

zlib_inflate_shim.dll: zlib_inflate_shim.c $(INFLATE_SHIM_OBJS)
	$(CC) $(LDFLAGS) -o $@ $(CFLAGS) zlib_inflate_shim.c $(INFLATE_SHIM_OBJS) $(IMPLIB_FLAG)

%.o: ../%.c
	$(CC) -c $(CFLAGS) $< -o $@

clean:
	rm -f *.o *.a *.dll *.so
