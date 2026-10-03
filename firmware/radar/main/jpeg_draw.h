#pragma once
#include <stddef.h>
#include <stdint.h>
#include "esp_err.h"

// Decode a baseline JPEG and draw it at (x, y) one MCU row (16 px) at a time,
// so a full frame never has to fit in RAM. dim: 256 = as-is, lower = darker.
esp_err_t jpeg_draw(const uint8_t *jpg, size_t len, int x, int y, int dim);
