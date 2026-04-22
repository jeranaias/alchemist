/* Auto-compiled shim that exposes zlib's static-local state-mutating
   functions as testable DLL entry points.

   Strategy: pull zlib's source files into a single compile unit. Static
   locals are then in-scope for the shim's extern entry points.

   Each state-mutator function gets:
     - shim_reset() — zeros the shared deflate_state g_state
     - shim_set_<field>(value) — set one field
     - shim_get_<field>(out_ptr, ...) — read one field
     - shim_run_<fn>() — execute the target function on g_state

   The Python fuzz harness:
     1. shim_reset()
     2. shim_set_<field> for each pre-state field
     3. shim_run_<fn>()
     4. shim_get_<field> for each post-state field
     5. Records (pre, post) as a test vector.
*/

#include <stddef.h>
#include <string.h>

/* Include zlib's aggregated headers. Internal state structs (deflate_state,
   inflate_state) live in deflate.h / inflate.h. We compile those .c files
   directly below so static-local helpers become available in this CU.
*/
#include "../zconf.h"
#include "../zutil.h"
#include "../deflate.h"

/* Slurp the zlib source bodies that hold state-mutating helpers.
   Order matters: trees.c uses statics only in trees; deflate.c references
   adler32/crc32 from adler32.c/crc32.c; zutil.c provides zcalloc/zcfree/z_errmsg. */
#include "../adler32.c"
#include "../crc32.c"
#include "../zutil.c"
#include "../trees.c"
#include "../deflate.c"

/* Single shared state buffer — reset between test vectors. */
static deflate_state g_state;

/* Pending buffer backing store (2048 bytes is more than enough for fuzz). */
#define PENDING_BUF_SIZE 2048
static unsigned char g_pending_buf[PENDING_BUF_SIZE];

#ifdef _WIN32
#define EXPORT __declspec(dllexport)
#else
#define EXPORT __attribute__((visibility("default")))
#endif

EXPORT void shim_reset(void) {
    memset(&g_state, 0, sizeof(g_state));
    memset(g_pending_buf, 0, PENDING_BUF_SIZE);
    g_state.pending_buf = g_pending_buf;
    g_state.pending_buf_size = PENDING_BUF_SIZE;
}

/* ---------- bi_buf / bi_valid / pending ---------- */

EXPORT void shim_set_bi_buf(unsigned short v) { g_state.bi_buf = v; }
EXPORT unsigned short shim_get_bi_buf(void) { return g_state.bi_buf; }

EXPORT void shim_set_bi_valid(int v) { g_state.bi_valid = v; }
EXPORT int shim_get_bi_valid(void) { return g_state.bi_valid; }

EXPORT void shim_set_pending(const unsigned char *buf, unsigned n) {
    if (n > PENDING_BUF_SIZE) n = PENDING_BUF_SIZE;
    memcpy(g_pending_buf, buf, n);
    g_state.pending = n;
}
EXPORT unsigned shim_get_pending_len(void) { return g_state.pending; }
EXPORT void shim_get_pending(unsigned char *out, unsigned max) {
    unsigned n = g_state.pending;
    if (n > max) n = max;
    memcpy(out, g_pending_buf, n);
}

/* ---------- hash chains ---------- */

EXPORT void shim_set_w_size(unsigned long v) { g_state.w_size = (uInt)v; }
EXPORT unsigned long shim_get_w_size(void) { return (unsigned long)g_state.w_size; }
EXPORT void shim_set_hash_size(unsigned long v) {
    g_state.hash_size = (uInt)v;
}

/* Note: head/prev are Pos* pointers to allocated memory. For simplicity,
   we let the caller fuzz sizes that match the static allocation we do
   here. */

#define HASH_MAX 1024
static Pos g_head[HASH_MAX];
static Pos g_prev[HASH_MAX];

EXPORT void shim_set_head(const unsigned short *src, unsigned n) {
    if (n > HASH_MAX) n = HASH_MAX;
    for (unsigned i = 0; i < n; i++) g_head[i] = (Pos)src[i];
    g_state.head = g_head;
}
EXPORT void shim_get_head(unsigned short *out, unsigned n) {
    if (n > HASH_MAX) n = HASH_MAX;
    for (unsigned i = 0; i < n; i++) out[i] = (unsigned short)g_head[i];
}

EXPORT void shim_set_prev(const unsigned short *src, unsigned n) {
    if (n > HASH_MAX) n = HASH_MAX;
    for (unsigned i = 0; i < n; i++) g_prev[i] = (Pos)src[i];
    g_state.prev = g_prev;
}
EXPORT void shim_get_prev(unsigned short *out, unsigned n) {
    if (n > HASH_MAX) n = HASH_MAX;
    for (unsigned i = 0; i < n; i++) out[i] = (unsigned short)g_prev[i];
}

/* ---------- tree counters ---------- */

EXPORT void shim_set_opt_len(unsigned long v) { g_state.opt_len = (ulg)v; }
EXPORT unsigned long shim_get_opt_len(void) { return g_state.opt_len; }

EXPORT void shim_set_static_len(unsigned long v) {
    g_state.static_len = (ulg)v;
}
EXPORT unsigned long shim_get_static_len(void) { return g_state.static_len; }

EXPORT void shim_set_sym_next(unsigned v) { g_state.sym_next = v; }
EXPORT unsigned shim_get_sym_next(void) { return g_state.sym_next; }

EXPORT void shim_set_matches(unsigned v) { g_state.matches = v; }
EXPORT unsigned shim_get_matches(void) { return g_state.matches; }

/* ---------- Function runners ---------- */

EXPORT void shim_run_bi_flush(void) { bi_flush(&g_state); }
EXPORT void shim_run_bi_windup(void) { bi_windup(&g_state); }
EXPORT void shim_run_slide_hash(void) { slide_hash(&g_state); }
EXPORT void shim_run_init_block(void) { init_block(&g_state); }
EXPORT void shim_run_tr_init(void) { _tr_init(&g_state); }
EXPORT void shim_run_detect_data_type(int *result) {
    *result = detect_data_type(&g_state);
}

/* send_bits is a macro in zlib's trees.c — use an intermediate pointer
   variable so the token expansion doesn't trip over operator precedence. */
EXPORT void shim_run_send_bits(int value, int length) {
    deflate_state *s = &g_state;
    send_bits(s, value, length);
}

/* bi_reverse is pure — takes code + len, returns reversed */
EXPORT unsigned shim_run_bi_reverse(unsigned code, int len) {
    return bi_reverse(code, len);
}

/* _tr_align: flush to byte boundary, emit EOB */
EXPORT void shim_run_tr_align(void) {
    deflate_state *s = &g_state;
    /* Requires static tables initialized */
    tr_static_init();
    _tr_align(s);
}

/* _tr_stored_block: writes a stored block with buffered data */
EXPORT void shim_run_tr_stored_block(const unsigned char *buf, unsigned long stored_len, int last) {
    deflate_state *s = &g_state;
    tr_static_init();
    _tr_stored_block(s, (charf *)buf, stored_len, last);
}

/* Additional field getters/setters for head/prev are already in place.
   Let's add fields for dyn_ltree freq (for detect_data_type). */

EXPORT void shim_set_dyn_ltree_freq(const unsigned short *freqs, unsigned n) {
    deflate_state *s = &g_state;
    unsigned max = n < HEAP_SIZE ? n : HEAP_SIZE;
    for (unsigned i = 0; i < max; i++) {
        s->dyn_ltree[i].Freq = (ush)freqs[i];
    }
}

/* detect_data_type returns Z_BINARY, Z_TEXT, or Z_UNKNOWN */
EXPORT int shim_run_detect_data_type_ret(void) {
    deflate_state *s = &g_state;
    return detect_data_type(s);
}
