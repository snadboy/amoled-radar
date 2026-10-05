#include "store.h"

#include <string.h>
#include "esp_log.h"
#include "esp_partition.h"
#include "esp_rom_crc.h"
#include <stdlib.h>
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

static const char *TAG = "store";
#define MAGIC   0x52445231u     // "RDR1"
#define VERSION 7u      // 7 = CRC-verified writes (6 and before could hold corrupt data from two tasks sharing a buffer)

static const esp_partition_t *s_part;
static size_t s_slot_size;
static SemaphoreHandle_t s_mx;
static loop_hdr_t s_hdr[STORE_SLOTS];          // in-RAM copy of every slot's header
static bool s_valid[STORE_SLOTS];
static int s_pinned = -1;
// Slots being downloaded into, per slot: the weather and aircraft sync tasks write
// concurrently. (A single "writing" slot let one task's commit clear the other's
// claim, and a third download then erased and overwrote a slot mid-download.)
static bool s_wr[STORE_SLOTS];
static char s_wr_view[STORE_SLOTS][16];
static uint32_t s_seq;              // highest seq committed so far
static char s_views[8][16];
static int s_nviews = -1;          // -1 = not told yet: treat every view as live
static esp_partition_mmap_handle_t s_mh[STORE_SLOTS];
static const uint8_t *s_mp[STORE_SLOTS];

static uint32_t base(int slot) { return (uint32_t)slot * s_slot_size; }

esp_err_t store_init(void)
{
    s_part = esp_partition_find_first(ESP_PARTITION_TYPE_DATA, 0x40, "frames");
    if (!s_part) { ESP_LOGE(TAG, "no 'frames' partition"); return ESP_ERR_NOT_FOUND; }
    s_slot_size = (s_part->size / STORE_SLOTS) & ~0xFFFu;
    s_mx = xSemaphoreCreateMutex();
    for (int i = 0; i < STORE_SLOTS; i++) {
        esp_partition_read(s_part, base(i), &s_hdr[i], sizeof(loop_hdr_t));
        s_valid[i] = s_hdr[i].magic == MAGIC && s_hdr[i].version == VERSION && s_hdr[i].nframes > 0
                     && s_hdr[i].nframes <= STORE_MAX_FRAMES;
        if (s_valid[i]) {
            if (s_hdr[i].seq > s_seq) s_seq = s_hdr[i].seq;
            ESP_LOGI(TAG, "slot %d: %s id %08lx, %u sections, seq %lu", i, s_hdr[i].view,
                     (unsigned long)s_hdr[i].loop_id, s_hdr[i].nframes, (unsigned long)s_hdr[i].seq);
        }
    }
    ESP_LOGI(TAG, "%d slots of %u KB", STORE_SLOTS, (unsigned)(s_slot_size / 1024));
    return ESP_OK;
}

size_t store_slot_capacity(void) { return s_slot_size - STORE_DATA_OFF; }

// newest valid slot for a view (caller holds the mutex)
static int newest(const char *view)
{
    int best = -1;
    for (int i = 0; i < STORE_SLOTS; i++)
        if (s_valid[i] && strcmp(s_hdr[i].view, view) == 0 &&
            (best < 0 || s_hdr[i].seq > s_hdr[best].seq)) best = i;
    return best;
}

bool store_get(const char *view, loop_hdr_t *hdr, int *slot)
{
    xSemaphoreTake(s_mx, portMAX_DELAY);
    int s = newest(view);
    if (s >= 0) { *hdr = s_hdr[s]; *slot = s; }
    xSemaphoreGive(s_mx);
    return s >= 0;
}

void store_pin(int slot) { s_pinned = slot; }

void store_set_views(const char (*ids)[16], int n)
{
    xSemaphoreTake(s_mx, portMAX_DELAY);
    s_nviews = n > 8 ? 8 : n;
    for (int i = 0; i < s_nviews; i++) strlcpy(s_views[i], ids[i], sizeof(s_views[i]));
    xSemaphoreGive(s_mx);
}

static bool live(const char *view)
{
    if (s_nviews < 0) return true;
    for (int i = 0; i < s_nviews; i++) if (!strcmp(s_views[i], view)) return true;
    return false;
}

int store_begin(const char *view, bool urgent)
{
    xSemaphoreTake(s_mx, portMAX_DELAY);
    for (int i = 0; i < STORE_SLOTS; i++)       // this key's earlier, abandoned download
        if (s_wr[i] && !strcmp(s_wr_view[i], view)) s_wr[i] = false;
    int pick = -1;
    for (int i = 0; i < STORE_SLOTS && pick < 0; i++) {
        // A mapped slot is in use (an app is drawing from it): never erase it. Apps
        // unmap a slot when they stop showing it.
        if (i == s_pinned || s_wr[i] || s_mp[i]) continue;
        bool current = false;                   // newest bundle of a view still offered?
        if (s_valid[i]) current = live(s_hdr[i].view) && newest(s_hdr[i].view) == i;
        if (!current) pick = i;
    }
    for (int i = 0; i < STORE_SLOTS && pick < 0 && urgent; i++)    // evict, oldest first
        if (i != s_pinned && !s_wr[i] && !s_mp[i] && s_valid[i]) {
            int best = i;
            for (int j = i + 1; j < STORE_SLOTS; j++)
                if (j != s_pinned && !s_wr[j] && !s_mp[j] && s_valid[j] && s_hdr[j].seq < s_hdr[best].seq) best = j;
            ESP_LOGI(TAG, "no free slot for %s: evicting %s from slot %d", view, s_hdr[best].view, best);
            pick = best;
        }
    if (pick >= 0) { s_valid[pick] = false; s_wr[pick] = true; strlcpy(s_wr_view[pick], view, sizeof(s_wr_view[0])); }
    xSemaphoreGive(s_mx);
    if (pick < 0) return -1;
    esp_err_t e = esp_partition_erase_range(s_part, base(pick), s_slot_size);
    if (e != ESP_OK) { ESP_LOGE(TAG, "erase slot %d: %s", pick, esp_err_to_name(e)); s_wr[pick] = false; return -1; }
    ESP_LOGI(TAG, "slot %d erased for %s", pick, view);
    return pick;
}

esp_err_t store_write(int slot, uint32_t off, const void *data, size_t n)
{
    if (off + n > s_slot_size) return ESP_ERR_INVALID_SIZE;
    return esp_partition_write(s_part, base(slot) + off, data, n);
}

esp_err_t store_read(int slot, uint32_t off, void *buf, size_t n)
{
    if (off + n > s_slot_size) return ESP_ERR_INVALID_SIZE;
    return esp_partition_read(s_part, base(slot) + off, buf, n);
}

esp_err_t store_commit(int slot, loop_hdr_t *hdr)
{
    hdr->magic = MAGIC; hdr->version = VERSION;
    xSemaphoreTake(s_mx, portMAX_DELAY);
    hdr->seq = ++s_seq;
    xSemaphoreGive(s_mx);
    esp_err_t e = esp_partition_write(s_part, base(slot), hdr, sizeof(*hdr));   // header last
    xSemaphoreTake(s_mx, portMAX_DELAY);
    if (e == ESP_OK) { s_hdr[slot] = *hdr; s_valid[slot] = true; }
    s_wr[slot] = false;
    xSemaphoreGive(s_mx);
    return e;
}

uint32_t store_crc32(int slot, uint32_t off, size_t len)
{
    uint8_t *buf = malloc(4096);
    uint32_t crc = 0;
    if (!buf) return 0;
    for (size_t done = 0; done < len; ) {
        size_t n = len - done < 4096 ? len - done : 4096;
        if (store_read(slot, off + done, buf, n) != ESP_OK) { crc = 0; break; }
        crc = esp_rom_crc32_le(crc, buf, n);
        done += n;
    }
    free(buf);
    return crc;
}

const uint8_t *store_map(int slot)
{
    xSemaphoreTake(s_mx, portMAX_DELAY);
    if (!s_mp[slot]) {
        const void *ptr = NULL;
        if (esp_partition_mmap(s_part, base(slot), s_slot_size, ESP_PARTITION_MMAP_DATA, &ptr, &s_mh[slot]) == ESP_OK)
            s_mp[slot] = (const uint8_t *)ptr;
        else ESP_LOGE(TAG, "mmap slot %d failed", slot);
    }
    const uint8_t *p = s_mp[slot];
    xSemaphoreGive(s_mx);
    return p;
}

void store_unmap(int slot)
{
    xSemaphoreTake(s_mx, portMAX_DELAY);
    if (s_mp[slot]) { esp_partition_munmap(s_mh[slot]); s_mp[slot] = NULL; }
    xSemaphoreGive(s_mx);
}
