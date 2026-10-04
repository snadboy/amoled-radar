#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "esp_err.h"

// Radar loops cached in the "frames" flash partition: 5 slots of 2.375 MB for
// up to 4 views, so there is always a spare slot to download into. A slot's
// header is written LAST, so a loop is either complete or invisible.
#define STORE_SLOTS       5
#define STORE_MAX_FRAMES  96
#define STORE_DATA_OFF    4096

typedef struct {
    uint32_t magic, version;
    uint32_t loop_id;
    char     view[16];
    uint16_t nframes, reserved;
    uint32_t off[STORE_MAX_FRAMES];
    uint32_t len[STORE_MAX_FRAMES];
    uint8_t  key[STORE_MAX_FRAMES];          // 1 = real radar frame, 0 = in-between
} loop_hdr_t;

esp_err_t store_init(void);
bool      store_get(const char *view, loop_hdr_t *hdr, int *slot);   // newest complete loop
size_t    store_slot_capacity(void);                                 // bytes for frame data
int       store_begin(const char *view);                             // erased slot, or -1 if none free
esp_err_t store_write(int slot, uint32_t off, const void *data, size_t n);
esp_err_t store_read(int slot, uint32_t off, void *buf, size_t n);
esp_err_t store_commit(int slot, loop_hdr_t *hdr);
void      store_pin(int slot);               // slot currently on screen: never chosen to erase
// Views the server currently offers. A slot holding a view NOT in this list is free:
// without this, a retired view's loop would occupy a slot forever.
void      store_set_views(const char (*ids)[16], int n);
