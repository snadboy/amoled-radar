// Weather app: animated radar loops for the hub's cities. Ported from firmware/radar.
//
// draw task : while active and the screen is on, animates the current view's RDL1
//             loop from flash (rdl.c) and the 60 s status strip below it.
//             KEY short or a tap -> picker (KEY / tap = next view, 3 s idle = choose).
//
// Everything is sized by the hub for this board's profile (?w=&h=&r=&panel=): the
// radar view's height comes from the loop itself, the strip sits below it.
// sync task : keeps every view's loop in flash, active or not (current view every
//             60 s, the others every 2 h), so switching cities -- and apps -- is instant.
//
// This app draws to the panel itself: LVGL is paused while it is active, and leave()
// returns only once the draw task has stopped touching the panel.
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "app.h"
#include "board.h"
#include "cJSON.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "jpeg_draw.h"
#include "net.h"
#include "nvs.h"
#include "rdl.h"
#include "store.h"
#include "ui.h"

static const char *TAG = "weather";

#define URL_MAX       256
#define MAX_VIEWS     8
#define MAX_JPEG      (160 * 1024)
#define CHECK_CUR_MS  (60 * 1000)
#define CHECK_BG_MS   (2 * 60 * 60 * 1000)
#define STATUS_MS     (60 * 1000)
#define PICK_IDLE_MS  3000
#define LOOP_MS       5000
#define DWELL_MS      1500
#define AMBER         0xFD80          // ~ #ffb703
#define TRACK         0x31A7          // dark grey

typedef struct { char id[14]; char name[32]; } view_t;
static view_t s_views[MAX_VIEWS];
static int s_nviews;
static volatile int s_cur;
static volatile bool s_view_changed;

static volatile bool s_active, s_screen_on = true, s_idle = true;
static bool s_restart;
static int64_t s_status_at;
static int s_shown_slot = -1, s_shown_idx = -1;
static loop_hdr_t s_shown;
static QueueHandle_t s_keys;
static TaskHandle_t s_draw;

static int s_view_h = 424;                // the loop's height; the status strip starts here
static char s_prof[64];                   // ?w=&h=&r=&panel= for this board
static bool s_touched;

static int64_t ms(void) { return esp_timer_get_time() / 1000; }

// A new touch (finger down), not a held one.
static bool tapped(void)
{
    int x, y;
    bool now = board_touch(&x, &y), tap = now && !s_touched;
    s_touched = now;
    return tap;
}
static bool running(void) { return s_active && s_screen_on; }

static void key_of(int vi, char out[16]) { snprintf(out, 16, "w:%s", s_views[vi].id); }

// ---------------------------------------------------------------- sync task
typedef struct { int slot; uint32_t off; } sink_t;
static esp_err_t to_flash(void *ctx, const uint8_t *d, size_t n)
{
    sink_t *s = (sink_t *)ctx;
    esp_err_t e = store_write(s->slot, s->off, d, n);
    s->off += n;
    return e;
}

static bool manifest(int vi, uint32_t *loop_id, size_t *size)
{
    char url[URL_MAX]; uint8_t *js; size_t len;
    snprintf(url, sizeof(url), "%s/weather/%s/manifest.json?%s", hub_url(), s_views[vi].id, s_prof);
    if (net_get(url, &js, &len, 64 * 1024) != ESP_OK) return false;
    cJSON *m = cJSON_Parse((char *)js);
    free(js);
    const cJSON *lid = cJSON_GetObjectItem(m, "loop_id"), *bs = cJSON_GetObjectItem(m, "loop_bin_size");
    bool ok = cJSON_IsNumber(lid) && lid->valuedouble > 0;
    if (ok) *loop_id = (uint32_t)lid->valuedouble;
    if (size) *size = cJSON_IsNumber(bs) ? (size_t)bs->valuedouble : 0;
    cJSON_Delete(m);
    return ok;
}

static void sync_view(int vi)
{
    char key[16]; uint32_t loop_id; size_t size;
    key_of(vi, key);
    if (!manifest(vi, &loop_id, &size)) return;
    loop_hdr_t cur; int cs;
    if (store_get(key, &cur, &cs) && cur.loop_id == loop_id) return;
    if (!size || size > store_slot_capacity()) { ESP_LOGW(TAG, "%s: loop size %u unusable", key, (unsigned)size); return; }

    int slot = store_begin(key);
    if (slot < 0) { ESP_LOGW(TAG, "no free slot for %s yet", key); return; }
    char url[URL_MAX]; size_t got;
    sink_t sk = { .slot = slot, .off = STORE_DATA_OFF };
    int64_t t0 = ms();
    snprintf(url, sizeof(url), "%s/weather/%s/loop.bin?%s", hub_url(), s_views[vi].id, s_prof);
    if (net_stream(url, to_flash, &sk, &got) != ESP_OK) { ESP_LOGW(TAG, "%s loop download failed; retry later", key); return; }
    loop_hdr_t h = {0};
    strlcpy(h.view, key, sizeof(h.view));
    h.loop_id = loop_id;
    if (rdl_parse(slot, &h) != ESP_OK) return;
    uint32_t after;                                        // loop rebuilt mid-download? discard
    if (!manifest(vi, &after, NULL) || after != loop_id) { ESP_LOGW(TAG, "%s changed during download", key); return; }
    store_commit(slot, &h);
    ESP_LOGI(TAG, "%s: loop %lu cached in slot %d (%u frames, %u KB, %lld ms)", key, (unsigned long)loop_id,
             slot, h.nframes, (unsigned)(got / 1024), ms() - t0);
}

static void sync_task(void *arg)
{
    static int64_t checked[MAX_VIEWS];
    for (;;) {
        int64_t now = ms();
        int cur = s_cur;
        if (s_view_changed || !checked[cur] || now - checked[cur] > CHECK_CUR_MS) {
            s_view_changed = false; sync_view(cur); checked[cur] = ms();
        } else {
            for (int v = 0; v < s_nviews; v++)              // one background view per pass
                if (v != cur && (!checked[v] || now - checked[v] > CHECK_BG_MS)) { sync_view(v); checked[v] = ms(); break; }
        }
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}

// ---------------------------------------------------------------- drawing
static void draw_status(void)
{
    char url[URL_MAX]; uint8_t *jpg; size_t len;
    snprintf(url, sizeof(url), "%s/weather/%s/status.jpg?%s", hub_url(), s_views[s_cur].id, s_prof);
    if (net_get(url, &jpg, &len, MAX_JPEG) == ESP_OK) {
        if (running()) jpeg_draw(jpg, len, 0, s_view_h, 256);
        free(jpg);
    }
    s_status_at = ms();
}

static void redraw_frame(int dim)
{
    if (s_shown_slot >= 0 && s_shown_idx >= 0) rdl_draw(s_shown_slot, &s_shown, s_shown_idx, dim);
    else board_fill(0, 0, BOARD.w, s_view_h, 0x0000);
}

static void bar(int x, int y, int w, int h, float frac)
{
    int f = (int)(w * (frac < 0 ? 0 : frac > 1 ? 1 : frac)) & ~1;
    if (f > 0) board_fill(x, y, x + f, y + h, AMBER);
    if (f < w) board_fill(x + f, y, x + w, y + h, TRACK);
}

// ---------------------------------------------------------------- picker
static int s_pick_x, s_pick_y, s_pick_w, s_pick_h;

static bool picker_draw(int hl)
{
    char url[URL_MAX]; uint8_t *jpg; size_t len; int w, h;
    snprintf(url, sizeof(url), "%s/weather/ui/picker.jpg?hl=%d&cur=%s&%s", hub_url(), hl, s_views[s_cur].id, s_prof);
    if (net_get(url, &jpg, &len, MAX_JPEG) != ESP_OK) return false;
    if (running() && jpeg_size(jpg, len, &w, &h) == ESP_OK) {
        s_pick_w = w; s_pick_h = h;
        s_pick_x = ((BOARD.w - w) / 2) & ~1; s_pick_y = ((s_view_h - h) / 2) & ~1;
        jpeg_draw(jpg, len, s_pick_x, s_pick_y, 256);
    }
    free(jpg);
    return true;
}

static void picker(void)
{
    int hl = s_cur;
    redraw_frame(90);                                      // the paused frame, dimmed
    if (!picker_draw(hl)) { s_restart = true; return; }    // hub unreachable: no picker
    int64_t deadline = ms() + PICK_IDLE_MS;
    while (running()) {
        key_ev_t ev;
        bool key = xQueueReceive(s_keys, &ev, pdMS_TO_TICKS(40)) == pdTRUE && ev.btn == BTN_KEY && ev.type == KEY_SHORT;
        if (key || tapped()) {
            hl = (hl + 1) % s_nviews;
            picker_draw(hl); deadline = ms() + PICK_IDLE_MS;
        }
        if (board_button_down(BTN_KEY) || s_touched) deadline = ms() + PICK_IDLE_MS;   // don't time out mid-press
        bar(s_pick_x + 20, s_pick_y + s_pick_h - 10, s_pick_w - 40, 4, (float)(deadline - ms()) / PICK_IDLE_MS);
        if (ms() >= deadline) break;
    }
    if (hl != s_cur) {
        s_cur = hl; s_view_changed = true;
        hub_nvs_put("wview", s_views[hl].id);
        ESP_LOGI(TAG, "view -> %s", s_views[hl].id);
    }
    s_restart = true; s_status_at = 0; s_shown_idx = -1;
}

static void wait_events(int timeout_ms)
{
    int64_t end = ms() + timeout_ms;
    do {
        key_ev_t ev;
        int left = (int)(end - ms());
        bool key = xQueueReceive(s_keys, &ev, pdMS_TO_TICKS(left > 20 ? 20 : (left > 0 ? left : 0))) == pdTRUE
                   && ev.btn == BTN_KEY && ev.type == KEY_SHORT;
        if (key || tapped()) picker();
    } while (ms() < end && running() && !s_restart);
}

// One pass over the current loop (or the waiting screen until there is one).
static void play(void)
{
    loop_hdr_t h; int slot; char key[16];
    key_of(s_cur, key);
    if (!store_get(key, &h, &slot)) {                     // first boot / new view: wait for sync
        if (s_shown_idx != -2) { board_fill(0, 0, BOARD.w, BOARD.h, 0x0000); s_shown_idx = -2; s_shown_slot = -1; }
        if (!s_status_at || ms() - s_status_at > STATUS_MS) draw_status();
        s_restart = false;
        wait_events(500);
        return;
    }
    if (s_shown_slot >= 0 && s_shown_slot != slot) store_unmap(s_shown_slot);   // free the old mapping
    store_pin(slot); s_shown_slot = slot; s_shown = h;
    if (h.height != s_view_h) { s_view_h = h.height; s_status_at = 0; }   // geometry comes with the loop
    s_restart = false;
    // Whole loop ~5 s plus a 1.5 s dwell on the latest frame, whatever the frame
    // count (the hub drops in-betweens when a stormy loop would not fit).
    int frame_ms = LOOP_MS / h.nframes;
    if (frame_ms < 40) frame_ms = 40;
    for (int i = 0; i < h.nframes && running() && !s_restart; i++) {
        int64_t t0 = ms();
        if (rdl_draw(slot, &h, i, 256) != ESP_OK) { s_restart = true; break; }
        board_draw_wait();
        s_shown_idx = i;
        hub_drawn();
        if (!s_status_at || ms() - s_status_at > STATUS_MS) draw_status();
        int left = (i == h.nframes - 1 ? DWELL_MS : frame_ms) - (int)(ms() - t0);
        wait_events(left > 0 ? left : 0);
    }
}

static void draw_task(void *arg)
{
    for (;;) {
        if (!running()) {
            board_draw_wait();
            s_idle = true;
            ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
            continue;
        }
        if (s_idle) { s_idle = false; s_restart = true; s_status_at = 0; s_shown_idx = -1; }
        play();
    }
}

static void wait_idle(void)
{
    xTaskNotifyGive(s_draw);
    for (int i = 0; i < 300 && !s_idle; i++) vTaskDelay(pdMS_TO_TICKS(10));
}

// ---------------------------------------------------------------- app interface
// A board migrated from firmware/radar keeps the city it was showing.
static bool old_radar_view(char *out, size_t sz)
{
    nvs_handle_t h; bool ok = false;
    if (nvs_open("radar", NVS_READONLY, &h) == ESP_OK) { ok = nvs_get_str(h, "view", out, &sz) == ESP_OK; nvs_close(h); }
    return ok;
}

static void app_init(const cJSON *views)
{
    const cJSON *v;
    int def = 0;
    cJSON_ArrayForEach(v, views) {
        const cJSON *id = cJSON_GetObjectItem(v, "id"), *nm = cJSON_GetObjectItem(v, "name");
        if (!cJSON_IsString(id) || s_nviews >= MAX_VIEWS) continue;
        strlcpy(s_views[s_nviews].id, id->valuestring, sizeof(s_views[0].id));
        strlcpy(s_views[s_nviews].name, cJSON_IsString(nm) ? nm->valuestring : id->valuestring, sizeof(s_views[0].name));
        if (cJSON_IsTrue(cJSON_GetObjectItem(v, "default"))) def = s_nviews;
        s_nviews++;
    }
    if (!s_nviews) { strlcpy(s_views[0].id, "geneva", sizeof(s_views[0].id)); strlcpy(s_views[0].name, "Geneva", sizeof(s_views[0].name)); s_nviews = 1; }
    s_cur = def;
    char saved[16];
    if (hub_nvs_get("wview", saved, sizeof(saved)) || old_radar_view(saved, sizeof(saved)))
        for (int i = 0; i < s_nviews; i++) if (!strcmp(s_views[i].id, saved)) s_cur = i;
    snprintf(s_prof, sizeof(s_prof), "w=%d&h=%d&r=%d&panel=%s", BOARD.w, BOARD.h, BOARD.corner_r, BOARD.panel);
    s_keys = xQueueCreate(8, sizeof(key_ev_t));
    xTaskCreate(draw_task, "weather", 6144, NULL, 5, &s_draw);
    xTaskCreate(sync_task, "wsync", 6144, NULL, 3, NULL);
    ESP_LOGI(TAG, "%d views, showing %s", s_nviews, s_views[s_cur].id);
}

static void app_enter(void)
{
    ui_pause(true);                       // LVGL hands the panel over
    xQueueReset(s_keys);
    s_active = true;
    xTaskNotifyGive(s_draw);
}

static void app_leave(void)
{
    s_active = false;
    wait_idle();                          // never draw after this returns
    // Release the loop on screen: a mapped or pinned slot is never erased, and while
    // another app runs, the store needs it free for this view's next loop.
    if (s_shown_slot >= 0) store_unmap(s_shown_slot);
    store_pin(-1);
    s_shown_slot = s_shown_idx = -1;
    ui_pause(false);
}

static void app_key(key_ev_t ev) { xQueueSend(s_keys, &ev, 0); }

static void app_screen(bool on)
{
    s_screen_on = on;
    if (on) xTaskNotifyGive(s_draw); else wait_idle();
}

const app_t APP_WEATHER = {
    .id = "weather", .name = "Weather", .store_prefix = 'w', .raw = true,
    .init = app_init, .enter = app_enter, .leave = app_leave, .key = app_key, .screen = app_screen,
};
