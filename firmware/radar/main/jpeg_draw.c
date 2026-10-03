// JPEG -> panel, one MCU row at a time, using the TJpgDec decoder in the C6 ROM.
//
// The ROM decoder hands back RGB888 blocks (JD_FORMAT 0) in raster order. Blocks
// are converted to big-endian RGB565 into a strip buffer; when a block ends the
// row (right edge == image width - 1) the strip is sent to the panel by DMA while
// the next row decodes into the other strip buffer.
#include "jpeg_draw.h"

#include <string.h>
#include "board.h"
#include "esp32c6/rom/tjpgd.h"
#include "esp_log.h"

static const char *TAG = "jpeg";

typedef struct {
    const uint8_t *src;
    size_t len, pos;
    int x, y, dim, which;
} ctx_t;

static uint8_t s_pool[4096] __attribute__((aligned(4)));   // TJpgDec work area

static UINT in_fn(JDEC *jd, BYTE *buf, UINT n)
{
    ctx_t *c = (ctx_t *)jd->device;
    if (c->pos + n > c->len) n = (UINT)(c->len - c->pos);
    if (buf) memcpy(buf, c->src + c->pos, n);
    c->pos += n;
    return n;
}

static UINT out_fn(JDEC *jd, void *bitmap, JRECT *r)
{
    ctx_t *c = (ctx_t *)jd->device;
    uint16_t *strip = board_band_buffer(c->which);
    const uint8_t *p = (const uint8_t *)bitmap;
    int w = r->right - r->left + 1, h = r->bottom - r->top + 1, stride = jd->width;
    for (int yy = 0; yy < h; yy++) {
        uint16_t *dst = strip + yy * stride + r->left;
        for (int xx = 0; xx < w; xx++, p += 3) {
            unsigned R = p[0], G = p[1], B = p[2];
            if (c->dim < 256) { R = R * c->dim >> 8; G = G * c->dim >> 8; B = B * c->dim >> 8; }
            uint16_t v = (uint16_t)(((R & 0xF8) << 8) | ((G & 0xFC) << 3) | (B >> 3));
            dst[xx] = (uint16_t)((v >> 8) | (v << 8));
        }
    }
    if (r->right == jd->width - 1) {             // row complete: ship it, flip strips
        board_draw(c->x, c->y + r->top, c->x + jd->width, c->y + r->bottom + 1, strip);
        c->which ^= 1;
    }
    return 1;
}

esp_err_t jpeg_draw(const uint8_t *jpg, size_t len, int x, int y, int dim)
{
    JDEC jd;
    ctx_t c = { .src = jpg, .len = len, .pos = 0, .x = x, .y = y, .dim = dim, .which = 0 };
    board_draw_wait();                           // both strips must be free before we start
    JRESULT r = jd_prepare(&jd, in_fn, s_pool, sizeof(s_pool), &c);
    if (r != JDR_OK) { ESP_LOGE(TAG, "prepare failed: %d", r); return ESP_FAIL; }
    if (jd.width > PANEL_W || jd.msy * 8 > 16) {
        ESP_LOGE(TAG, "unsupported image %ux%u (MCU height %d)", jd.width, jd.height, jd.msy * 8);
        return ESP_ERR_NOT_SUPPORTED;
    }
    r = jd_decomp(&jd, out_fn, 0);
    if (r != JDR_OK) { ESP_LOGE(TAG, "decode failed: %d", r); return ESP_FAIL; }
    return ESP_OK;
}
