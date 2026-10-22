/* Test harness for UxPlay's AirPlay audio buffer (lib/raop_buffer.c), original or MirrorDesk's.
 *
 * Feeds sequences of RTP audio packets the way UxPlay's audio thread does (enqueue, then dequeue
 * until nothing is ready) and prints one line per scenario:
 *     <name> released=<n> lost=<n> max_held=<n> held_after_gap=<n> order=<ok|BAD>
 * max_held: most packets that waited in the buffer at once; held_after_gap: packets that arrived
 * after a gap before the first of them was released. Built and run by tests/test_p7_uxplay_patch.py.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>

#include "raop_buffer.h"
#include "crypto.h"

/* ---- stubs for what raop_buffer.c links against (no decryption, no logging) ---- */
struct aes_ctx_s { int unused; };
static struct aes_ctx_s dummy_ctx;
aes_ctx_t *aes_cbc_init(const uint8_t *key, const uint8_t *iv, aes_direction_t direction) {
    (void) key; (void) iv; (void) direction; return &dummy_ctx;
}
void aes_cbc_reset(aes_ctx_t *ctx) { (void) ctx; }
void aes_cbc_decrypt(aes_ctx_t *ctx, const uint8_t *in, uint8_t *out, int len) { (void) ctx; memcpy(out, in, len); }
void aes_cbc_destroy(aes_ctx_t *ctx) { (void) ctx; }
void logger_log(logger_t *logger, int level, const char *fmt, ...) { (void) logger; (void) level; (void) fmt; }
char *utils_data_to_string(const unsigned char *data, int datalen, int chars_per_line) {
    (void) data; (void) datalen; (void) chars_per_line; return calloc(1, 1);
}
#ifdef MIRRORDESK_PATCHED
#define SKIP_TO(buffer, seq) raop_buffer_skip_to(buffer, seq)
#else
#define SKIP_TO(buffer, seq) ((void) (buffer), (void) (seq))   /* the original ignored a FLUSH */
#endif

/* ---- the scenario runner ---- */
typedef struct {
    raop_buffer_t *buffer;
    int released, max_held, order_ok;
    long last_released;          /* sequence number of the last released packet, unwrapped */
    int arrived_since_gap;       /* packets that arrived after a gap was marked, not yet released */
    int gap_marked, held_after_gap;
    long held;                   /* distinct packets enqueued and not yet released */
    unsigned char seen[65536];
} run_t;

static void run_init(run_t *r) {
    static const unsigned char key[16], iv[16];
    memset(r, 0, sizeof(*r));
    r->buffer = raop_buffer_init(NULL, key, iv);
    r->order_ok = 1;
    r->last_released = -1;
    r->held_after_gap = -1;
}

static void drain(run_t *r) {
    void *payload;
    unsigned int length = 0;
    uint32_t timestamp = 0;
    unsigned short seq = 0;
    while ((payload = raop_buffer_dequeue(r->buffer, &length, &timestamp, &seq, 0))) {
        long unwrapped = (long) timestamp;   /* the harness puts the unwrapped number in the timestamp */
        if (r->last_released >= 0 && unwrapped <= r->last_released) r->order_ok = 0;
        r->last_released = unwrapped;
        r->released++;
        r->held--;
        if (r->gap_marked && r->held_after_gap < 0) r->held_after_gap = r->arrived_since_gap;
        free(payload);
    }
}

static void packet(run_t *r, long n) {      /* n: unwrapped sequence number */
    unsigned char data[12 + 32];
    unsigned short seq = (unsigned short) (n & 0xffff);
    memset(data, 0x11, sizeof(data));
    data[0] = 0x80; data[1] = 0x60;
    data[2] = (unsigned char) (seq >> 8); data[3] = (unsigned char) seq;
    data[4] = (unsigned char) (n >> 24); data[5] = (unsigned char) (n >> 16);
    data[6] = (unsigned char) (n >> 8); data[7] = (unsigned char) n;
    if (raop_buffer_enqueue(r->buffer, data, sizeof(data), 1) == 1) {
        r->held++;
        if (r->gap_marked && r->held_after_gap < 0) r->arrived_since_gap++;
    }
    if (r->held > r->max_held) r->max_held = (int) r->held;
    drain(r);
}

/* AAC-ELD as an iPhone sends it: every packet three times, 0 / 0 1 / 0 1 2 / 1 2 3 ... */
static void triple(run_t *r, long first, long count) {
    for (long n = first; n < first + count; n++) {
        if (n - 2 >= first) packet(r, n - 2);
        if (n - 1 >= first) packet(r, n - 1);
        packet(r, n);
    }
}

static void report(const char *name, run_t *r, long sent) {
    printf("%s released=%d lost=%ld max_held=%d held_after_gap=%d order=%s\n", name, r->released,
           sent - r->released, r->max_held, r->held_after_gap, r->order_ok ? "ok" : "BAD");
    raop_buffer_destroy(r->buffer);
}

static run_t r;

int main(void) {
    /* 1. Normal stream: everything in order, nothing held. */
    run_init(&r); triple(&r, 1000, 300); report("steady", &r, 300);

    /* 2. Pause/seek WITHOUT a FLUSH: the phone carries on 50 packets later. */
    run_init(&r); triple(&r, 1000, 100); r.gap_marked = 1; triple(&r, 1150, 300); report("gap", &r, 400);

    /* 3. Pause/seek WITH a FLUSH (RTP-Info seq=1150) before the new sound. */
    run_init(&r); triple(&r, 1000, 100); SKIP_TO(r.buffer, 1150); r.gap_marked = 1; triple(&r, 1150, 300);
    report("flush", &r, 400);

    /* 4. The FLUSH is handled after the first new packets arrived. */
    run_init(&r); triple(&r, 1000, 100); r.gap_marked = 1; packet(&r, 1150); packet(&r, 1151);
    SKIP_TO(r.buffer, 1150); drain(&r); triple(&r, 1152, 300); report("flush_late", &r, 402);

    /* 5. A FLUSH for a position already played: no effect. */
    run_init(&r); triple(&r, 1000, 100); SKIP_TO(r.buffer, 1050); triple(&r, 1100, 100); report("flush_past", &r, 200);

    /* 6. One packet lost, its resend arrives three packets later: played in order. */
    run_init(&r);
    for (long n = 1000; n < 1010; n++) packet(&r, n);
    for (long n = 1011; n < 1014; n++) packet(&r, n);
    packet(&r, 1010);
    for (long n = 1014; n < 1100; n++) packet(&r, n);
    report("resend", &r, 100);

    /* 7. Sequence numbers wrapping around 65535 across a gap with a FLUSH. */
    run_init(&r); triple(&r, 65500, 20); SKIP_TO(r.buffer, (int) ((65540) & 0xffff)); r.gap_marked = 1;
    triple(&r, 65540, 100); report("wrap", &r, 120);

    /* 8. A long stop (more than the buffer) without a FLUSH: the original's own overflow rule. */
    run_init(&r); triple(&r, 1000, 100); r.gap_marked = 1; triple(&r, 1500, 300); report("long_gap", &r, 400);
    return 0;
}
