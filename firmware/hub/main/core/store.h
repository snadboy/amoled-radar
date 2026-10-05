#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "esp_err.h"

// Bundles cached in the "frames" flash partition: 6 slots of ~2.07 MB (4 weather
// views + 1 aircraft view + a spare to download into). A slot's header is written
// LAST, so a bundle is either complete or invisible. Keys are namespaced by app
// ("w:geneva", "a:home"). Weather loops use the frame table; other bundles keep
// their own index inside the data (e.g. ABN1) and set nframes to their section count.
#include "sdkconfig.h"
#if CONFIG_IDF_TARGET_ESP32
#define STORE_SLOTS       3     // 4 MB boards: ~460 KB each (a 240x240 four-level ABN1 just fits)
#else
#define STORE_SLOTS       6
#endif
#define STORE_MAX_FRAMES  96
#define STORE_DATA_OFF    4096

typedef struct {
    uint32_t magic, version;
    uint32_t loop_id;
    char     view[16];
    uint16_t nframes, height;                // RDL1: rows per frame (the radar view's height)
    uint32_t base_off, pal_off;              // RDL1: map pixels and palette within the slot
    uint32_t off[STORE_MAX_FRAMES];
    uint32_t len[STORE_MAX_FRAMES];
    uint8_t  key[STORE_MAX_FRAMES];          // 1 = real radar frame, 0 = in-between
    uint32_t seq;                            // set by store_commit: newest wins (ids are hashes, not ordered)
} loop_hdr_t;

esp_err_t store_init(void);
bool      store_get(const char *view, loop_hdr_t *hdr, int *slot);   // newest complete loop
size_t    store_slot_capacity(void);                                 // bytes for frame data
// Erased slot to download into, or -1 if none is free. urgent (what's on screen now): with
// no free slot, take the least recently refreshed bundle that isn't on screen or being
// written -- on 4 MB boards (3 slots) the views can outnumber the slots.
int       store_begin(const char *view, bool urgent);
esp_err_t store_write(int slot, uint32_t off, const void *data, size_t n);
esp_err_t store_read(int slot, uint32_t off, void *buf, size_t n);
esp_err_t store_commit(int slot, loop_hdr_t *hdr);
// CRC32 (zlib's) of `len` bytes at `off`, read back from flash -- checks what was
// actually written, not just what was received.
uint32_t  store_crc32(int slot, uint32_t off, size_t len);
void      store_pin(int slot);               // slot currently on screen: never chosen to erase
// Views the server currently offers. A slot holding a view NOT in this list is free:
// without this, a retired view's loop would occupy a slot forever.
void      store_set_views(const char (*ids)[16], int n);
// Memory-map a slot for fast reads through the flash cache (NULL on failure).
// A slot is unmapped automatically before it is erased for a new loop.
const uint8_t *store_map(int slot);
void      store_unmap(int slot);
