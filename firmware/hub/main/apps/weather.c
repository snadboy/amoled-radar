// Weather app: animated radar loops for the hub's cities. Ported from firmware/radar.
//
// draw task : while active and the screen is on, animates the current view's RDL1
//             loop from flash (rdl.c) and the 60 s status strip below it.
//             KEY short or a tap -> picker (KEY / tap = next view, 3 s idle = choose).
//             A swipe pans the view 50 mi (the map follows the finger; up to 3 steps
//             each way): the hub renders "<city>@<dx>,<dy>" on demand. A tap on the
//             offset pill it burns into the frames (top PAN_TAP_H px) recentres, and so
//             does 10 min without a swipe, a city change, or the hub (HA).
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
#include "rdl.h"
#include "store.h"
#include "ui.h"

static const char *TAG = "weather";

#define URL_MAX       384
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
#define PAN_MAX       3
#define PAN_TAP_H     80              // render.PAN_TAP_H: the offset pill's band
#define SWIPE_PX      50
#define PAN_IDLE_MS   (10 * 60 * 1000)

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
static volatile int s_dx, s_dy;           // pan, in 50 mi steps east / north (0,0 = on the city)
static int64_t s_pan_at;

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

static bool panned(void) { return s_dx || s_dy; }

// The hub id and store key of a view, panned if it is the current one. Pans get a short
// key ("w:~<view><dx><dy>") so any city id fits the store's 16 chars.
static void id_of(int vi, int dx, int dy, char out[24])
{
    if (dx || dy) snprintf(out, 24, "%s@%d,%d", s_views[vi].id, dx, dy);
    else strlcpy(out, s_views[vi].id, 24);
}
static void key_of_pan(int vi, int dx, int dy, char out[16])
{
    if (dx || dy) snprintf(out, 16, "w:~%d%c%c", vi, 'd' + dx, 'd' + dy);
    else snprintf(out, 16, "w:%s", s_views[vi].id);
}
static void key_of(int vi, char out[16]) { key_of_pan(vi, 0, 0, out); }

// ---------------------------------------------------------------- sync task
typedef struct { int slot; uint32_t off; } sink_t;
static esp_err_t to_flash(void *ctx, const uint8_t *d, size_t n)
{
    sink_t *s = (sink_t *)ctx;
    esp_err_t e = store_write(s->slot, s->off, d, n);
    s->off += n;
    return e;
}

static bool manifest(const char *id, uint32_t *loop_id, size_t *size, uint32_t *crc)
{
    char url[URL_MAX]; uint8_t *js; size_t len;
    snprintf(url, sizeof(url), "%s/weather/%s/manifest.json?%s", hub_url(), id, s_prof);
    if (net_get(url, &js, &len, 64 * 1024) != ESP_OK) return false;
    cJSON *m = cJSON_Parse((char *)js);
    free(js);
    const cJSON *lid = cJSON_GetObjectItem(m, "loop_id"), *bs = cJSON_GetObjectItem(m, "loop_bin_size"),
                *cr = cJSON_GetObjectItem(m, "loop_crc32");
    bool ok = cJSON_IsNumber(lid) && lid->valuedouble > 0;
    if (ok) *loop_id = (uint32_t)lid->valuedouble;
    if (size) *size = cJSON_IsNumber(bs) ? (size_t)bs->valuedouble : 0;
    if (crc) *crc = cJSON_IsNumber(cr) ? (uint32_t)cr->valuedouble : 0;     // 0: an older hub, no check
    cJSON_Delete(m);
    return ok;
}

static void sync_view(int vi)
{
    char key[16], id[24]; uint32_t loop_id, crc; size_t size;
    int dx = vi == s_cur ? s_dx : 0, dy = vi == s_cur ? s_dy : 0;
    key_of_pan(vi, dx, dy, key); id_of(vi, dx, dy, id);
    if (!manifest(id, &loop_id, &size, &crc)) return;
    loop_hdr_t cur; int cs;
    if (store_get(key, &cur, &cs) && cur.loop_id == loop_id) return;
    if (!size || size > store_slot_capacity()) { ESP_LOGW(TAG, "%s: loop size %u unusable", key, (unsigned)size); return; }

    int slot = store_begin(key, vi == s_cur);
    if (slot < 0) { ESP_LOGW(TAG, "no free slot for %s yet", key); return; }
    char url[URL_MAX]; size_t got;
    sink_t sk = { .slot = slot, .off = STORE_DATA_OFF };
    int64_t t0 = ms();
    snprintf(url, sizeof(url), "%s/weather/%s/loop.bin?%s", hub_url(), id, s_prof);
    if (net_stream(url, to_flash, &sk, &got) != ESP_OK) { ESP_LOGW(TAG, "%s loop download failed; retry later", key); return; }
    if (got != size || (crc && store_crc32(slot, STORE_DATA_OFF, size) != crc)) {
        ESP_LOGE(TAG, "%s: loop failed its check after writing (%u of %u bytes) -- not used", key, (unsigned)got, (unsigned)size);
        return;
    }
    loop_hdr_t h = {0};
    strlcpy(h.view, key, sizeof(h.view));
    h.loop_id = loop_id;
    if (rdl_parse(slot, &h) != ESP_OK) return;
    uint32_t after;                                        // loop rebuilt mid-download? discard
    if (!manifest(id, &after, NULL, NULL) || after != loop_id) { ESP_LOGW(TAG, "%s changed during download", key); return; }
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
            char key[16]; loop_hdr_t h; int sl;
            key_of_pan(cur, s_dx, s_dy, key);
            // nothing to show yet (a pan the hub is still rendering): ask again in 3 s
            if (!store_get(key, &h, &sl)) checked[cur] = ms() - CHECK_CUR_MS + 3000;
        } else {
            for (int v = 0; v < s_nviews; v++)              // one background view per pass
                if (v != cur && (!checked[v] || now - checked[v] > CHECK_BG_MS)) {
                    sync_view(v); checked[v] = ms();
                    char key[16]; loop_hdr_t h; int sl;
                    key_of(v, key);
                    // nothing cached yet (e.g. the hub is still building it): retry in a minute, not 2 h
                    if (!store_get(key, &h, &sl)) checked[v] = ms() - CHECK_BG_MS + CHECK_CUR_MS;
                    break;
                }
        }
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}

// ---------------------------------------------------------------- drawing
static void draw_status(void)
{
    char url[URL_MAX]; uint8_t *jpg; size_t len;
    s_status_at = ms();
    if (s_view_h >= BOARD.h) return;           // no strip: the loop fills the panel
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
    char ids[MAX_VIEWS * 15] = "";
    for (int i = 0; i < s_nviews; i++) { if (i) strlcat(ids, ",", sizeof(ids)); strlcat(ids, s_views[i].id, sizeof(ids)); }
    snprintf(url, sizeof(url), "%s/weather/ui/picker.jpg?hl=%d&cur=%s&ids=%s&%s", hub_url(), hl, s_views[s_cur].id, ids, s_prof);
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
        s_cur = hl; s_dx = s_dy = 0; s_view_changed = true;
        ESP_LOGI(TAG, "view -> %s", s_views[hl].id);
    }
    s_restart = true; s_status_at = 0; s_shown_idx = -1;
}

static void pan_to(int dx, int dy)
{
    dx = dx < -PAN_MAX ? -PAN_MAX : dx > PAN_MAX ? PAN_MAX : dx;
    dy = dy < -PAN_MAX ? -PAN_MAX : dy > PAN_MAX ? PAN_MAX : dy;
    s_pan_at = ms();
    if (dx == s_dx && dy == s_dy) return;
    s_dx = dx; s_dy = dy;
    s_view_changed = true; s_restart = true; s_status_at = 0;
    ESP_LOGI(TAG, "pan -> %d,%d", dx, dy);
}

// Touch, judged on release: a short move is a tap, a long one a swipe.
enum { G_NONE, G_TAP, G_SWIPE };
static bool s_g_down, s_g_ignore;
static int s_gx0, s_gy0, s_gx1, s_gy1;
static int gesture(void)
{
    int x, y;
    if (board_touch(&x, &y)) {
        if (!s_g_down) { s_g_down = true; s_gx0 = x; s_gy0 = y; }
        s_gx1 = x; s_gy1 = y;
        return G_NONE;
    }
    if (!s_g_down) return G_NONE;
    s_g_down = false;
    if (s_g_ignore) { s_g_ignore = false; return G_NONE; }    // the press that closed the picker
    int dx = s_gx1 - s_gx0, dy = s_gy1 - s_gy0;
    return (abs(dx) >= SWIPE_PX || abs(dy) >= SWIPE_PX) ? G_SWIPE : G_TAP;
}

static void wait_events(int timeout_ms)
{
    int64_t end = ms() + timeout_ms;
    do {
        key_ev_t ev;
        int left = (int)(end - ms());
        bool key = xQueueReceive(s_keys, &ev, pdMS_TO_TICKS(left > 20 ? 20 : (left > 0 ? left : 0))) == pdTRUE
                   && ev.btn == BTN_KEY && ev.type == KEY_SHORT;
        int g = gesture();
        if (g == G_SWIPE) {
            int dx = s_gx1 - s_gx0, dy = s_gy1 - s_gy0;
            if (abs(dx) > abs(dy)) pan_to(s_dx + (dx < 0 ? 1 : -1), s_dy);   // finger left: look east
            else pan_to(s_dx, s_dy + (dy > 0 ? 1 : -1));                    // finger down: look north
        } else if (g == G_TAP && panned() && s_gy0 < PAN_TAP_H) {
            pan_to(0, 0);                                                     // the offset pill
        } else if (key || g == G_TAP) {
            picker();
            int x, y;
            s_g_down = false; s_g_ignore = board_touch(&x, &y);
        }
    } while (ms() < end && running() && !s_restart);
}

static void loading_pill(void)
{
    char url[URL_MAX]; uint8_t *jpg; size_t len; int w, h;
    snprintf(url, sizeof(url), "%s/weather/ui/pan.jpg?pan=%d,%d&%s", hub_url(), s_dx, s_dy, s_prof);
    if (net_get(url, &jpg, &len, MAX_JPEG) != ESP_OK) return;
    if (running() && jpeg_size(jpg, len, &w, &h) == ESP_OK)
        jpeg_draw(jpg, len, ((BOARD.w - w) / 2) & ~1, ((s_view_h - h) / 2) & ~1, 256);
    free(jpg);
}

// One pass over the current loop (or the waiting screen until there is one).
static void play(void)
{
    loop_hdr_t h; int slot; char key[16];
    if (panned() && ms() - s_pan_at > PAN_IDLE_MS) pan_to(0, 0);     // back home after 10 min
    key_of_pan(s_cur, s_dx, s_dy, key);
    if (!store_get(key, &h, &slot)) {                     // first boot / new view or pan: wait for sync
        if (s_shown_idx != -2) {
            // Keep the last frame up, dimmed, with "Loading..." (the hub renders a pan in
            // ~20 s); the slot stays pinned until the new loop replaces it.
            if (s_shown_slot >= 0 && s_shown_idx >= 0) { redraw_frame(90); loading_pill(); }
            else board_fill(0, 0, BOARD.w, BOARD.h, 0x0000);
            s_shown_idx = -2;
        }
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
static void app_init(const cJSON *views)
{
    const cJSON *v;
    int def = 0;
    bool strip = true;
    cJSON_ArrayForEach(v, views) {
        if (cJSON_IsFalse(cJSON_GetObjectItem(v, "strip"))) strip = false;   // device setting, on every view
        const cJSON *id = cJSON_GetObjectItem(v, "id"), *nm = cJSON_GetObjectItem(v, "name");
        if (!cJSON_IsString(id) || s_nviews >= MAX_VIEWS) continue;
        strlcpy(s_views[s_nviews].id, id->valuestring, sizeof(s_views[0].id));
        strlcpy(s_views[s_nviews].name, cJSON_IsString(nm) ? nm->valuestring : id->valuestring, sizeof(s_views[0].name));
        if (cJSON_IsTrue(cJSON_GetObjectItem(v, "default"))) def = s_nviews;
        s_nviews++;
    }
    if (!s_nviews) { strlcpy(s_views[0].id, "geneva", sizeof(s_views[0].id)); strlcpy(s_views[0].name, "Geneva", sizeof(s_views[0].name)); s_nviews = 1; }
    s_cur = def;
    snprintf(s_prof, sizeof(s_prof), "w=%d&h=%d&r=%d&panel=%s%s", BOARD.w, BOARD.h, BOARD.corner_r, BOARD.panel,
             strip ? "" : "&strip=0");
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

static const char *app_current(void)
{
    static char id[24];
    id_of(s_cur, s_dx, s_dy, id);
    return id;
}

// "<city>" or "<city>@<dx>,<dy>" (HA's Pan select, or its City select)
static void app_show(const char *view)
{
    char base[24]; int dx = 0, dy = 0;
    strlcpy(base, view, sizeof(base));
    char *at = strchr(base, '@');
    if (at) { *at = 0; if (sscanf(at + 1, "%d,%d", &dx, &dy) != 2) dx = dy = 0; }
    for (int i = 0; i < s_nviews; i++) {
        if (strcmp(s_views[i].id, base)) continue;
        if (i != s_cur) {
            s_cur = i; s_dx = s_dy = 0; s_view_changed = true;   // the sync task fetches it first if needed
            s_restart = true; s_status_at = 0; s_shown_idx = -1;
            ESP_LOGI(TAG, "view -> %s (hub)", base);
        }
        pan_to(dx, dy);
    }
}

const app_t APP_WEATHER = {
    .id = "weather", .name = "Weather", .store_prefix = 'w', .raw = true,
    .init = app_init, .enter = app_enter, .leave = app_leave, .key = app_key, .screen = app_screen,
    .current = app_current, .show = app_show,
};
