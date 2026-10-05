#pragma once
// What a board provides to the hub firmware. One implementation per board under
// boards/; everything else is board-independent and reads sizes from the profile.
#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"

typedef struct {
    const char *board;      // sent to the hub in /device/hello
    int w, h;               // panel pixels
    int corner_r;           // rounded-glass radius; keep UI this far in from corners
    const char *panel;      // "amoled" | "lcd"
    bool psram;
    int band_rows;          // rows per DMA strip (board_band_buffer)
    bool one_button;        // only KEY: a long press means BOOT short (next app)
} board_profile_t;

extern const board_profile_t BOARD;

esp_err_t board_init(void);                 // power, panel (+ colour-bar self test), touch
// Async strip transfer of big-endian RGB565 (panel order). Waits for the previous
// transfer first, so the two band buffers can be ping-ponged safely.
void board_draw(int x0, int y0, int x1, int y1, const void *rgb565_be);
void board_draw_wait(void);                 // block until the last transfer finished
void board_fill(int x0, int y0, int x1, int y1, uint16_t rgb565);
void board_set_brightness(uint8_t level);   // 0..255
void board_display(bool on);                // display off + sleep-in / sleep-out + display on
uint16_t *board_band_buffer(int which);     // two DMA-capable w x band_rows strips

bool board_touch(int *x, int *y);           // true while touched, in panel coordinates

// Buttons, as raw levels / latched presses; keys.c turns them into events.
enum { BTN_BOOT, BTN_KEY, BTN_PWR, BTN_COUNT };
bool board_button_down(int btn);            // BOOT, KEY: current level
bool board_pwr_pressed(void);               // PWR: a short press latched by the PMU since last call
