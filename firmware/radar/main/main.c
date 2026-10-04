// Desk radar -- ESP32-C6 AMOLED firmware.
//
// main task : animates the current view's loop from flash, handles KEY:
//               short  -> picker (KEY = next view, 3 s idle = choose)
//               hold 1s-> screen off; any press on a dark screen wakes it
// sync task : keeps every view's loop in flash (current view every 60 s, others
//             every 2 h), polls /device.json for screen on/off + brightness
//             (occupancy and room light, decided by the server), checks for OTA.
//
// A screen turned off by hand stays off until KEY: occupancy never wakes it.
// A screen woken by hand in an empty room stays on for 10 minutes.
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "board.h"
#include "cJSON.h"
#include "esp_app_desc.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "jpeg_draw.h"
#include "keys.h"
#include "net.h"
#include "nvs.h"
#include "ota.h"
#include "sdkconfig.h"
#include "store.h"

static const char *TAG = "radar";

#define URL_MAX       200
#define MAX_JPEG      (160 * 1024)
#define VIEW_H        424
#define STATUS_Y      424
#define PICK_X        44
#define HOLD_X        120
#define HOLD_Y        380
#define CHECK_CUR_MS  (60 * 1000)
#define CHECK_BG_MS   (2 * 60 * 60 * 1000)
#define DEVICE_MS     (30 * 1000)
#define OTA_MS        (6 * 60 * 60 * 1000)
#define MANUAL_ON_MS  (10 * 60 * 1000)
#define PICK_IDLE_MS  3000
#define AMBER         0xFD80          // ~ #ffb703
#define TRACK         0x31A7          // dark grey

typedef struct { char id[16]; char name[32]; } view_t;
static view_t s_views[8];
static int s_nviews;
static volatile int s_cur;
static volatile bool s_view_changed;
static volatile bool s_srv_on = true;
static volatile int s_srv_bright = CONFIG_RADAR_BRIGHTNESS;

enum { RUN, OFF };
static int s_state = RUN;
static bool s_manual_off, s_restart, s_hint;
static int64_t s_manual_on_until, s_hint_at, s_status_at;
static int s_shown_slot = -1, s_shown_idx = -1, s_applied_bright = -1;
static loop_hdr_t s_shown;
static uint8_t *s_hold_jpg; static size_t s_hold_len;

static int64_t ms(void) { return esp_timer_get_time() / 1000; }

// ---------------------------------------------------------------- views + NVS
static bool parse_views(const char *js)
{
    cJSON *a = cJSON_Parse(js);
    if (!cJSON_IsArray(a)) { cJSON_Delete(a); return false; }
    int n = 0, def = 0;
    cJSON *it;
    cJSON_ArrayForEach(it, a) {
        if (n >= 8) break;
        cJSON *id = cJSON_GetObjectItem(it, "id"), *nm = cJSON_GetObjectItem(it, "name");
        if (!cJSON_IsString(id) || !cJSON_IsString(nm)) continue;
        strlcpy(s_views[n].id, id->valuestring, sizeof(s_views[n].id));
        strlcpy(s_views[n].name, nm->valuestring, sizeof(s_views[n].name));
        if (cJSON_IsTrue(cJSON_GetObjectItem(it, "default"))) def = n;
        n++;
    }
    cJSON_Delete(a);
    if (!n) return false;
    s_nviews = n; s_cur = def;
    return true;
}

static void nvs_put(const char *k, const char *v)
{
    nvs_handle_t h;
    if (nvs_open("radar", NVS_READWRITE, &h) == ESP_OK) { nvs_set_str(h, k, v); nvs_commit(h); nvs_close(h); }
}

static bool nvs_getstr(const char *k, char *out, size_t sz)
{
    nvs_handle_t h; bool ok = false;
    if (nvs_open("radar", NVS_READONLY, &h) == ESP_OK) { ok = nvs_get_str(h, k, out, &sz) == ESP_OK; nvs_close(h); }
    return ok;
}

static void load_views(void)
{
    char url[URL_MAX];
    snprintf(url, sizeof(url), "%s/cities.json", CONFIG_RADAR_SERVER_URL);
    for (int attempt = 0; attempt < 3; attempt++) {
        uint8_t *js; size_t len;
        if (net_get(url, &js, &len, 8192) == ESP_OK) {
            bool ok = parse_views((char *)js);
            if (ok) nvs_put("cities", (char *)js);
            free(js);
            if (ok) break;
        }
        vTaskDelay(pdMS_TO_TICKS(2000));
    }
    if (!s_nviews) {                                       // offline: last known list
        static char cached[2048];
        if (!(nvs_getstr("cities", cached, sizeof(cached)) && parse_views(cached))) {
            strlcpy(s_views[0].id, CONFIG_RADAR_CITY, sizeof(s_views[0].id));
            strlcpy(s_views[0].name, CONFIG_RADAR_CITY, sizeof(s_views[0].name));
            s_nviews = 1; s_cur = 0;
        }
    }
    char saved[16];
    if (nvs_getstr("view", saved, sizeof(saved)))
        for (int i = 0; i < s_nviews; i++) if (!strcmp(s_views[i].id, saved)) s_cur = i;
    static char ids[8][16];
    for (int i = 0; i < s_nviews; i++) strlcpy(ids[i], s_views[i].id, sizeof(ids[i]));
    store_set_views((const char (*)[16])ids, s_nviews);
    ESP_LOGI(TAG, "%d views, showing %s", s_nviews, s_views[s_cur].id);
}

// ---------------------------------------------------------------- sync task
typedef struct { int slot; uint32_t off; } sink_t;
static esp_err_t to_flash(void *ctx, const uint8_t *d, size_t n)
{
    sink_t *s = (sink_t *)ctx;
    esp_err_t e = store_write(s->slot, s->off, d, n);
    s->off += n;
    return e;
}

static bool manifest(int vi, uint32_t *loop_id, cJSON **out)
{
    char url[URL_MAX]; uint8_t *js; size_t len;
    snprintf(url, sizeof(url), "%s/c/%s/manifest.json", CONFIG_RADAR_SERVER_URL, s_views[vi].id);
    if (net_get(url, &js, &len, 64 * 1024) != ESP_OK) return false;
    cJSON *m = cJSON_Parse((char *)js);
    free(js);
    const cJSON *lid = m ? cJSON_GetObjectItem(m, "loop_id") : NULL;
    if (!cJSON_IsNumber(lid) || lid->valuedouble <= 0) { cJSON_Delete(m); return false; }
    *loop_id = (uint32_t)lid->valuedouble;
    if (out) *out = m; else cJSON_Delete(m);
    return true;
}

static void sync_view(int vi)
{
    const char *id = s_views[vi].id;
    uint32_t loop_id; cJSON *m;
    if (!manifest(vi, &loop_id, &m)) return;
    loop_hdr_t cur; int cs;
    if (store_get(id, &cur, &cs) && cur.loop_id == loop_id) { cJSON_Delete(m); return; }

    cJSON *sizes = cJSON_GetObjectItem(m, "sizes"), *keys = cJSON_GetObjectItem(m, "keys");
    int n = cJSON_GetArraySize(sizes);
    if (n > STORE_MAX_FRAMES) n = STORE_MAX_FRAMES;
    uint8_t iskey[STORE_MAX_FRAMES] = {0};
    cJSON *k;
    cJSON_ArrayForEach(k, keys) if (k->valueint >= 0 && k->valueint < n) iskey[k->valueint] = 1;
    size_t total = 0;
    for (int i = 0; i < n; i++) total += (size_t)cJSON_GetArrayItem(sizes, i)->valueint;
    bool keys_only = total > store_slot_capacity();        // a stormy loop too big: real frames only
    cJSON_Delete(m);

    int slot = store_begin(id);
    if (slot < 0) { ESP_LOGW(TAG, "no free slot for %s yet", id); return; }
    loop_hdr_t h = {0};
    strlcpy(h.view, id, sizeof(h.view));
    h.loop_id = loop_id;
    sink_t sk = { .slot = slot, .off = STORE_DATA_OFF };
    int64_t t0 = ms();
    for (int i = 0, j = 0; i < n; i++) {
        if (keys_only && !iskey[i]) continue;
        char url[URL_MAX]; size_t got;
        snprintf(url, sizeof(url), "%s/c/%s/frame/%d.jpg", CONFIG_RADAR_SERVER_URL, id, i);
        uint32_t start = sk.off;
        if (net_stream(url, to_flash, &sk, &got) != ESP_OK) { ESP_LOGW(TAG, "%s frame %d failed; retry later", id, i); return; }
        h.off[j] = start; h.len[j] = (uint32_t)got; h.key[j] = iskey[i]; h.nframes = (uint16_t)++j;
    }
    uint32_t after;                                        // loop rebuilt mid-download? discard
    if (!manifest(vi, &after, NULL) || after != loop_id) { ESP_LOGW(TAG, "%s changed during download", id); return; }
    store_commit(slot, &h);
    ESP_LOGI(TAG, "%s: loop %lu cached in slot %d (%u frames, %lu KB, %lld ms%s)", id, (unsigned long)loop_id, slot,
             h.nframes, (unsigned long)((sk.off - STORE_DATA_OFF) / 1024), ms() - t0, keys_only ? ", real frames only" : "");
}

static void poll_device(void)
{
    char url[URL_MAX]; uint8_t *js; size_t len;
    snprintf(url, sizeof(url), "%s/device.json", CONFIG_RADAR_SERVER_URL);
    if (net_get(url, &js, &len, 4096) != ESP_OK) return;
    cJSON *d = cJSON_Parse((char *)js);
    free(js);
    const cJSON *disp = d ? cJSON_GetObjectItem(d, "display") : NULL, *br = d ? cJSON_GetObjectItem(d, "brightness") : NULL;
    if (cJSON_IsString(disp)) s_srv_on = strcmp(disp->valuestring, "off") != 0;
    if (cJSON_IsNumber(br) && br->valueint > 0 && br->valueint < 256) s_srv_bright = br->valueint;
    cJSON_Delete(d);
}

static void sync_task(void *arg)
{
    static int64_t checked[8];
    int64_t dev_at = 0, ota_at = 0;
    for (;;) {
        int64_t now = ms();
        if (now - dev_at > DEVICE_MS) { poll_device(); dev_at = ms(); }
        int cur = s_cur;
        if (s_view_changed || !checked[cur] || now - checked[cur] > CHECK_CUR_MS) {
            s_view_changed = false; sync_view(cur); checked[cur] = ms();
        } else {
            for (int v = 0; v < s_nviews; v++)              // one background view per pass
                if (v != cur && (!checked[v] || now - checked[v] > CHECK_BG_MS)) { sync_view(v); checked[v] = ms(); break; }
        }
        if (now > 90 * 1000 && (!ota_at || now - ota_at > OTA_MS)) { ota_at = ms(); ota_check(); }
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}

// ---------------------------------------------------------------- drawing
static void draw_status(void)
{
    char url[URL_MAX]; uint8_t *jpg; size_t len;
    snprintf(url, sizeof(url), "%s/c/%s/status.jpg", CONFIG_RADAR_SERVER_URL, s_views[s_cur].id);
    if (net_get(url, &jpg, &len, MAX_JPEG) == ESP_OK) { jpeg_draw(jpg, len, 0, STATUS_Y, 256); free(jpg); }
    s_status_at = ms();
}

static void redraw_frame(int dim)
{
    if (s_shown_slot >= 0 && s_shown_idx >= 0)
        jpeg_draw_flash(s_shown_slot, s_shown.off[s_shown_idx], s_shown.len[s_shown_idx], 0, 0, dim);
    else
        board_fill(0, 0, PANEL_W, VIEW_H, 0x0000);
}

static void bar(int x, int y, int w, int h, float frac)
{
    int f = (int)(w * (frac < 0 ? 0 : frac > 1 ? 1 : frac)) & ~1;
    if (f > 0) board_fill(x, y, x + f, y + h, AMBER);
    if (f < w) board_fill(x + f, y, x + w, y + h, TRACK);
}

static void hold_show(void)
{
    if (!s_hold_jpg) {
        char url[URL_MAX];
        snprintf(url, sizeof(url), "%s/ui/hold.jpg", CONFIG_RADAR_SERVER_URL);
        if (net_get(url, &s_hold_jpg, &s_hold_len, 32 * 1024) != ESP_OK) s_hold_jpg = NULL;
    }
    if (s_hold_jpg) jpeg_draw(s_hold_jpg, s_hold_len, HOLD_X, HOLD_Y, 256);
    s_hint = true; s_hint_at = ms();
}

static void hold_progress(void)
{
    if (s_hint) bar(HOLD_X + 16, HOLD_Y + 28, 208, 4, (float)(ms() - s_hint_at) / (KEY_LONG_MS - KEY_HINT_MS));
}

static void apply_brightness(void)
{
    if (s_srv_bright != s_applied_bright) { board_set_brightness((uint8_t)s_srv_bright); s_applied_bright = s_srv_bright; }
}

static void screen(bool on, bool manual)
{
    if (!on) {
        board_display(false);
        s_state = OFF; s_manual_off = manual; s_hint = false;
        ESP_LOGI(TAG, "screen off (%s)", manual ? "KEY" : "room empty");
    } else {
        board_display(true);
        s_state = RUN; s_manual_off = false; s_restart = true; s_status_at = 0;
        if (manual && !s_srv_on) s_manual_on_until = ms() + MANUAL_ON_MS;
        s_applied_bright = -1; apply_brightness();
        ESP_LOGI(TAG, "screen on (%s)", manual ? "KEY" : "room occupied");
    }
}

// ---------------------------------------------------------------- picker
static int s_pick_y, s_pick_h;

static bool picker_draw(int hl)
{
    char url[URL_MAX]; uint8_t *jpg; size_t len; int w, h;
    snprintf(url, sizeof(url), "%s/ui/picker.jpg?hl=%d&cur=%s", CONFIG_RADAR_SERVER_URL, hl, s_views[s_cur].id);
    if (net_get(url, &jpg, &len, MAX_JPEG) != ESP_OK) return false;
    if (jpeg_size(jpg, len, &w, &h) == ESP_OK) {
        s_pick_h = h; s_pick_y = ((VIEW_H - h) / 2) & ~1;
        jpeg_draw(jpg, len, PICK_X, s_pick_y, 256);
    }
    free(jpg);
    return true;
}

static void picker(void)
{
    int hl = s_cur;
    redraw_frame(90);                                      // the paused frame, dimmed
    if (!picker_draw(hl)) { s_restart = true; return; }    // server unreachable: no picker
    int64_t deadline = ms() + PICK_IDLE_MS;
    for (;;) {
        key_event_t ev;
        if (keys_get(&ev, pdMS_TO_TICKS(40))) {
            if (ev == KEY_SHORT) {
                hl = (hl + 1) % s_nviews;
                if (s_hint) { s_hint = false; redraw_frame(90); }
                picker_draw(hl); deadline = ms() + PICK_IDLE_MS;
            } else if (ev == KEY_HOLD_START) {
                hold_show();
            } else if (ev == KEY_LONG) {
                screen(false, true); return;
            }
        }
        if (keys_held()) deadline = ms() + PICK_IDLE_MS;  // don't time out mid-press
        hold_progress();
        bar(PICK_X + 20, s_pick_y + s_pick_h - 10, 352, 4, (float)(deadline - ms()) / PICK_IDLE_MS);
        if (ms() >= deadline) break;
    }
    if (hl != s_cur) {
        s_cur = hl; s_view_changed = true;
        nvs_put("view", s_views[hl].id);
        ESP_LOGI(TAG, "view -> %s", s_views[hl].id);
    }
    s_restart = true; s_status_at = 0; s_shown_idx = -1;
}

// ---------------------------------------------------------------- main loop
static void handle(key_event_t ev)
{
    if (s_state == OFF) {
        if (ev == KEY_SHORT || ev == KEY_LONG) screen(true, true);
        return;
    }
    switch (ev) {
    case KEY_HOLD_START:  hold_show(); break;
    case KEY_HOLD_CANCEL: s_hint = false; break;           // next frame paints over the hint
    case KEY_SHORT:       picker(); break;
    case KEY_LONG:        screen(false, true); break;
    }
}

static void wait_events(int timeout_ms)
{
    int64_t end = ms() + timeout_ms;
    do {
        key_event_t ev;
        int left = (int)(end - ms());
        if (keys_get(&ev, pdMS_TO_TICKS(left > 20 ? 20 : (left > 0 ? left : 0)))) handle(ev);
        if (s_hint) hold_progress();
    } while ((ms() < end || keys_held()) && s_state == RUN && !s_restart);
}

void app_main(void)
{
    ESP_ERROR_CHECK(board_init());
    keys_start();
    ESP_ERROR_CHECK(store_init());
    ESP_LOGI(TAG, "firmware %s", esp_app_get_description()->version);
    if (net_wifi_connect(30000) != ESP_OK) ESP_LOGW(TAG, "WiFi not up yet -- showing cached loops, still retrying");
    load_views();
    xTaskCreate(sync_task, "sync", 8192, NULL, 4, NULL);
    bool marked = false;

    for (;;) {
        if (s_state == OFF) {
            key_event_t ev;
            if (keys_get(&ev, pdMS_TO_TICKS(100))) handle(ev);
            else if (!s_manual_off && s_srv_on) screen(true, false);
            continue;
        }
        if (!s_srv_on && ms() > s_manual_on_until) { screen(false, false); continue; }
        apply_brightness();

        loop_hdr_t h; int slot;
        if (!store_get(s_views[s_cur].id, &h, &slot)) {          // first boot / new view: wait for sync
            if (s_shown_idx != -2) { board_fill(0, 0, PANEL_W, VIEW_H, 0x0000); s_shown_idx = -2; s_shown_slot = -1; }
            if (ms() - s_status_at > 60000 || !s_status_at) draw_status();
            s_restart = false;
            wait_events(500);
            continue;
        }
        store_pin(slot); s_shown_slot = slot; s_shown = h;
        s_restart = false;
        for (int i = 0; i < h.nframes && s_state == RUN && !s_restart; i++) {
            jpeg_draw_flash(slot, h.off[i], h.len[i], 0, 0, 256);
            board_draw_wait();
            s_shown_idx = i;
            if (!marked) { ota_mark_good(); marked = true; }
            if (!s_status_at || ms() - s_status_at > 60000) draw_status();
            wait_events(i == h.nframes - 1 ? 1500 : 0);           // dwell on the latest frame
        }
    }
}
