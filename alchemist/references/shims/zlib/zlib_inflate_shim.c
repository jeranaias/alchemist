/* Companion shim exposing zlib's inflate state-mutating functions as
 * testable DLL entry points. Parallel to zlib_state_shim.c (deflate side).
 *
 * Build note: zlib's internal headers (inflate.h, inftrees.h) lack include
 * guards. Amalgamating all inflate .c files into one CU therefore double-
 * defines struct inflate_state. Instead, this shim pulls only inflate.c
 * into the CU (its own #include brings the needed types) and links
 * separately-compiled inffast.o / inftrees.o / zutil.o at link time.
 */

#include <stddef.h>
#include <string.h>

#include "../zutil.h"

#include "../inflate.c"

/* Shared test state. */
static struct inflate_state g_state;
static z_stream g_strm;

#define SHIM_WINDOW_BYTES (1u << 15)
static unsigned char g_window[SHIM_WINDOW_BYTES];

#ifdef _WIN32
#define EXPORT __declspec(dllexport)
#else
#define EXPORT __attribute__((visibility("default")))
#endif

static voidpf shim_zalloc(voidpf opaque, uInt items, uInt size) {
    (void)opaque; (void)items; (void)size;
    return Z_NULL;
}
static void shim_zfree(voidpf opaque, voidpf addr) {
    (void)opaque; (void)addr;
}

EXPORT void shim_inflate_reset(void) {
    memset(&g_state, 0, sizeof(g_state));
    memset(&g_strm, 0, sizeof(g_strm));
    memset(g_window, 0, sizeof(g_window));
    g_strm.zalloc = shim_zalloc;
    g_strm.zfree = shim_zfree;
    g_strm.state = (struct internal_state FAR *)&g_state;
    g_state.window = g_window;
    g_state.strm = &g_strm;
}

/* ---------- inflate_state field getters/setters ---------- */

EXPORT void shim_set_mode(int v) { g_state.mode = (inflate_mode)v; }
EXPORT int shim_get_mode(void) { return (int)g_state.mode; }

EXPORT void shim_set_wrap(int v) { g_state.wrap = v; }
EXPORT int shim_get_wrap(void) { return g_state.wrap; }

EXPORT void shim_set_wbits(int v) { g_state.wbits = (unsigned)v; }
EXPORT int shim_get_wbits(void) { return (int)g_state.wbits; }

EXPORT void shim_set_hold(unsigned long v) { g_state.hold = v; }
EXPORT unsigned long shim_get_hold(void) { return g_state.hold; }

EXPORT void shim_set_bits(int v) { g_state.bits = (unsigned)v; }
EXPORT int shim_get_bits(void) { return (int)g_state.bits; }

EXPORT void shim_set_total(unsigned long v) { g_state.total = v; }
EXPORT unsigned long shim_get_total(void) { return g_state.total; }

EXPORT void shim_set_havedict(int v) { g_state.havedict = v; }
EXPORT int shim_get_havedict(void) { return g_state.havedict; }

EXPORT void shim_set_whave(unsigned v) { g_state.whave = v; }
EXPORT unsigned shim_get_whave(void) { return g_state.whave; }

EXPORT void shim_set_wnext(unsigned v) { g_state.wnext = v; }
EXPORT unsigned shim_get_wnext(void) { return g_state.wnext; }

EXPORT void shim_set_wsize(unsigned v) { g_state.wsize = v; }
EXPORT unsigned shim_get_wsize(void) { return g_state.wsize; }

EXPORT void shim_set_adler(unsigned long v) { g_strm.adler = v; }
EXPORT unsigned long shim_get_adler(void) { return g_strm.adler; }

EXPORT void shim_set_total_in(unsigned long v) { g_strm.total_in = v; }
EXPORT unsigned long shim_get_total_in(void) { return g_strm.total_in; }

EXPORT void shim_set_total_out(unsigned long v) { g_strm.total_out = v; }
EXPORT unsigned long shim_get_total_out(void) { return g_strm.total_out; }

EXPORT void shim_set_msg_null(void) { g_strm.msg = Z_NULL; }

/* ---------- Function runners ---------- */

EXPORT int shim_run_inflateReset(void) {
    return inflateReset(&g_strm);
}

EXPORT int shim_run_inflateResetKeep(void) {
    return inflateResetKeep(&g_strm);
}

EXPORT int shim_run_inflateReset2(int windowBits) {
    return inflateReset2(&g_strm, windowBits);
}

EXPORT int shim_run_inflateStateCheck(void) {
    return inflateStateCheck(&g_strm);
}

EXPORT int shim_run_inflatePrime(int bits, int value) {
    return inflatePrime(&g_strm, bits, value);
}
