#pragma once
#include "esp_err.h"
#include "store.h"

// Draw frame i of an RDL1 loop stored in a flash slot (format: server/render.py
// encode_loop). Map pixels are copied from flash; only pixels that changed are
// taken from the frame's zlib-compressed 8-bit layer. dim: 256 = as-is.
esp_err_t rdl_draw(int slot, const loop_hdr_t *h, int i, int dim);
// Fill in a loop header from an RDL1 blob just written at STORE_DATA_OFF.
esp_err_t rdl_parse(int slot, loop_hdr_t *h);
void rdl_stats(void);   // log + reset the per-phase timing
