// RDL1 frames -> panel without JPEG.
//
// Why: JPEG decode on the C6 takes ~240 ms per frame, and because there is no room
// for a framebuffer the panel visibly paints each frame as a downward wipe. Here
// each 16-row strip starts as a straight copy of the map from flash; the frame's
// compressed 8-bit layer (0 = keep map pixel) is streamed through the ROM inflater
// and only changed pixels are overwritten. Then the strip goes to the panel by DMA
// while the next one is built in the other buffer.
#include "rdl.h"

#include <string.h>
#include "board.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "miniz.h"

static const char *TAG = "rdl";
#define W     (BOARD.w)
#define H     424
#define ROWS  16
#define MASK  (TINFL_LZ_DICT_SIZE - 1)

static tinfl_decompressor s_inf;
static uint8_t s_ring[TINFL_LZ_DICT_SIZE];      // inflate window (also the output buffer)
static uint16_t s_pal[256];
static int s_pal_slot = -1;
static uint32_t s_pal_loop;

static inline uint16_t dim565(uint16_t be, int dim)
{
    uint16_t v = (uint16_t)((be >> 8) | (be << 8));
    unsigned r = (v >> 11) * dim >> 8, g = ((v >> 5) & 63) * dim >> 8, b = (v & 31) * dim >> 8;
    v = (uint16_t)((r << 11) | (g << 5) | b);
    return (uint16_t)((v >> 8) | (v << 8));
}

esp_err_t rdl_parse(int slot, loop_hdr_t *h)
{
    uint8_t hdr[16];
    if (store_read(slot, STORE_DATA_OFF, hdr, sizeof(hdr)) != ESP_OK) return ESP_FAIL;
    uint16_t ver, n, w, hh;
    memcpy(&ver, hdr + 4, 2); memcpy(&n, hdr + 6, 2); memcpy(&w, hdr + 8, 2); memcpy(&hh, hdr + 10, 2);
    if (memcmp(hdr, "RDL1", 4) || ver != 1 || w != W || hh != H || n == 0 || n > STORE_MAX_FRAMES) {
        ESP_LOGE(TAG, "bad loop header (v%u %ux%u n=%u)", ver, w, hh, n);
        return ESP_FAIL;
    }
    for (int i = 0; i < n; i++) {
        uint8_t e[12];
        store_read(slot, STORE_DATA_OFF + 16 + 12 * i, e, sizeof(e));
        uint32_t off, len;
        memcpy(&off, e, 4); memcpy(&len, e + 4, 4);
        h->off[i] = STORE_DATA_OFF + off; h->len[i] = len; h->key[i] = e[8];
    }
    h->nframes = n;
    h->pal_off = STORE_DATA_OFF + 16 + 12 * n;
    h->base_off = h->pal_off + 512;
    return ESP_OK;
}

// phase timing, logged by rdl_stats()
static int64_t s_t_base, s_t_inflate, s_t_send; static int s_frames;

void rdl_stats(void)
{
    if (s_frames) ESP_LOGI(TAG, "TIMING per frame: map copy %lld us, inflate+patch %lld us, waiting on panel %lld us (%d frames)",
                           s_t_base / s_frames, s_t_inflate / s_frames, s_t_send / s_frames, s_frames);
    s_t_base = s_t_inflate = s_t_send = 0; s_frames = 0;
}

esp_err_t rdl_draw(int slot, const loop_hdr_t *h, int i, int dim)
{
    const uint8_t *mp = store_map(slot);
    if (!mp) return ESP_FAIL;
    if (s_pal_slot != slot || s_pal_loop != h->loop_id) {
        memcpy(s_pal, mp + h->pal_off, sizeof(s_pal));
        s_pal_slot = slot; s_pal_loop = h->loop_id;
    }
    const uint8_t *map = mp + h->base_off;
    const uint8_t *in = mp + h->off[i];
    size_t in_left = h->len[i], ring_pos = 0;
    int px = 0, which = 0, y0 = 0;
    uint16_t *strip = board_band_buffer(which);
    int64_t t = esp_timer_get_time(), t2;

    tinfl_init(&s_inf);
    board_draw_wait();                                    // both strips free
    memcpy(strip, map, W * ROWS * 2);
    t2 = esp_timer_get_time(); s_t_base += t2 - t; t = t2;

    for (;;) {
        size_t in_bytes = in_left;
        size_t out_bytes = TINFL_LZ_DICT_SIZE - (ring_pos & MASK);
        tinfl_status st = tinfl_decompress(&s_inf, in, &in_bytes, s_ring, s_ring + (ring_pos & MASK),
                                           &out_bytes, TINFL_FLAG_PARSE_ZLIB_HEADER);
        in += in_bytes; in_left -= in_bytes;
        const uint8_t *o = s_ring + (ring_pos & MASK);
        ring_pos += out_bytes;
        for (size_t k = 0; k < out_bytes; k++) {
            if (o[k]) strip[px - y0 * W] = s_pal[o[k]];
            if (++px == (y0 + ROWS) * W || px == W * H) {     // strip complete
                int rows = (px - y0 * W) / W;
                if (dim < 256) for (int q = 0; q < rows * W; q++) strip[q] = dim565(strip[q], dim);
                t2 = esp_timer_get_time(); s_t_inflate += t2 - t; t = t2;
                board_draw(0, y0, W, y0 + rows, strip);
                t2 = esp_timer_get_time(); s_t_send += t2 - t; t = t2;
                y0 += rows;
                if (px < W * H) {                              // next strip starts as the map
                    which ^= 1; strip = board_band_buffer(which);
                    int nr = (H - y0) < ROWS ? (H - y0) : ROWS;
                    memcpy(strip, map + (size_t)y0 * W * 2, (size_t)nr * W * 2);
                    t2 = esp_timer_get_time(); s_t_base += t2 - t; t = t2;
                }
            }
        }
        if (st == TINFL_STATUS_DONE) break;
        if (st < 0 || (st == TINFL_STATUS_NEEDS_MORE_INPUT && !in_left)) {
            ESP_LOGE(TAG, "inflate error %d at px %d (frame %d)", st, px, i); return ESP_FAIL;
        }
    }
    s_frames++;
    if (px != W * H) { ESP_LOGE(TAG, "frame %d decoded %d of %d px", i, px, W * H); return ESP_FAIL; }
    return ESP_OK;
}
