// JPEG -> panel on the ESP32-P4, using its hardware JPEG decoder (the P4 ROM has no
// TJpgDec). With 32 MB of PSRAM the whole image decodes at once; it then goes to the
// panel in strips, converted to big-endian RGB565, through the board's DMA buffers.
// Same interface as jpeg_draw.c (the C6's ROM-decoder version).
#include "jpeg_draw.h"

#include <string.h>
#include "board.h"
#include "driver/jpeg_decode.h"
#include "esp_log.h"

static const char *TAG = "jpeg";
static jpeg_decoder_handle_t s_dec;

static inline uint16_t px565(uint16_t v, int dim)          // little-endian RGB565 in, panel order out
{
    if (dim < 256) {
        unsigned r = (v >> 11) * dim >> 8, g = ((v >> 5) & 63) * dim >> 8, b = (v & 31) * dim >> 8;
        v = (uint16_t)((r << 11) | (g << 5) | b);
    }
    return (uint16_t)((v >> 8) | (v << 8));
}

esp_err_t jpeg_size(const uint8_t *jpg, size_t len, int *w, int *h)
{
    jpeg_decode_picture_info_t info;
    if (jpeg_decoder_get_info(jpg, len, &info) != ESP_OK) return ESP_FAIL;
    *w = info.width; *h = info.height;
    return ESP_OK;
}

esp_err_t jpeg_draw(const uint8_t *jpg, size_t len, int x, int y, int dim)
{
    if (!s_dec) {
        jpeg_decode_engine_cfg_t ec = { .timeout_ms = 200 };
        if (jpeg_new_decoder_engine(&ec, &s_dec) != ESP_OK) { ESP_LOGE(TAG, "no decoder engine"); return ESP_FAIL; }
    }
    int w, h;
    if (jpeg_size(jpg, len, &w, &h) != ESP_OK || w > BOARD.w) return ESP_ERR_NOT_SUPPORTED;
    int aw = (w + 15) & ~15, ah = (h + 15) & ~15;       // the decoder writes whole MCUs
    size_t in_sz = 0, out_sz = 0;
    jpeg_decode_memory_alloc_cfg_t ic = { .buffer_direction = JPEG_DEC_ALLOC_INPUT_BUFFER };
    jpeg_decode_memory_alloc_cfg_t oc = { .buffer_direction = JPEG_DEC_ALLOC_OUTPUT_BUFFER };
    uint8_t *in = jpeg_alloc_decoder_mem(len, &ic, &in_sz);
    uint16_t *out = jpeg_alloc_decoder_mem((size_t)aw * ah * 2, &oc, &out_sz);
    esp_err_t err = ESP_ERR_NO_MEM;
    if (in && out) {
        memcpy(in, jpg, len);
        jpeg_decode_cfg_t dc = { .output_format = JPEG_DECODE_OUT_FORMAT_RGB565,
                                 .rgb_order = JPEG_DEC_RGB_ELEMENT_ORDER_RGB, .conv_std = JPEG_YUV_RGB_CONV_STD_BT601 };
        uint32_t got = 0;
        err = jpeg_decoder_process(s_dec, &dc, in, len, (uint8_t *)out, out_sz, &got);
        if (err == ESP_OK) {
            int rows = BOARD.band_rows, which = 0;
            board_draw_wait();
            for (int y0 = 0; y0 < h; y0 += rows) {
                int n = h - y0 < rows ? h - y0 : rows;
                uint16_t *strip = board_band_buffer(which);
                board_draw_wait();                       // this strip's last transfer is done
                for (int r = 0; r < n; r++)
                    for (int c = 0; c < w; c++) strip[r * w + c] = px565(out[(y0 + r) * aw + c], dim);
                board_draw(x, y + y0, x + w, y + y0 + n, strip);
                which ^= 1;
            }
        } else {
            ESP_LOGE(TAG, "decode failed: %s", esp_err_to_name(err));
        }
    }
    free(in); free(out);
    return err;
}

esp_err_t jpeg_draw_flash(int slot, uint32_t off, uint32_t len, int x, int y, int dim)
{
    return ESP_ERR_NOT_SUPPORTED;                       // no caller; the C6 version keeps it
}
