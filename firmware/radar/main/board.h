#pragma once
// Board support for the Waveshare ESP32-C6-Touch-AMOLED-2.16.
// Pins and the panel's power-cycle reset are documented in ../../CLAUDE.md.
#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"

#define PANEL_W 480
#define PANEL_H 480

esp_err_t board_init(void);                 // PMU rails, panel power-cycle + init
void board_draw(int x0, int y0, int x1, int y1, const void *rgb565_be);   // async, ping-pong safe
void board_draw_wait(void);                 // block until the last transfer finished
void board_fill(int x0, int y0, int x1, int y1, uint16_t rgb565);
void board_set_brightness(uint8_t level);   // 0..255, DCS 0x51
void board_display(bool on);                // display off + sleep-in / sleep-out + display on
uint16_t *board_band_buffer(int which);     // two DMA-capable PANEL_W x 16 strips
