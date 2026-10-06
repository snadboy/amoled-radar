// Metra app: one line between two stations, each train where it is now.
//
// The hub draws the whole page (server/hub/metra.py) from Home Assistant's SB Metra
// integration; this fetches it every REFRESH_MS and puts it on the panel. The view is
// the line; the stations come as a ready-made query string in the hello, appended
// as-is. KEY refreshes it now; a tap on a train (its row in the list, or its arrow on
// the track) asks the hub for its card (detail.jpg?x=&y=), shown until a tap or KEY, or
// DETAIL_MS. A tap anywhere else refreshes.
//
// Draws to the panel itself (like weather): LVGL is paused while it is active, and
// leave() returns only once the draw task has stopped touching the panel.
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
#include "ui.h"

static const char *TAG = "metra";

#define URL_MAX      320
#define MAX_JPEG     (160 * 1024)
#define REFRESH_MS   15000
#define DETAIL_MS    20000

static char s_view[16] = "UP-W";
static char s_query[128] = "from=Chicago+OTC&to=Elburn";   // from the hub
static volatile bool s_active, s_screen_on = true, s_idle = true;
static QueueHandle_t s_keys;
static TaskHandle_t s_draw;
static bool s_touched;

static int64_t ms(void) { return esp_timer_get_time() / 1000; }
static bool running(void) { return s_active && s_screen_on; }

static bool tapped(int *tx, int *ty)         // a new touch, not a held one; tx, ty: where
{
    int x, y;
    bool now = board_touch(&x, &y), tap = now && !s_touched;
    s_touched = now;
    if (tap && tx) { *tx = x; *ty = y; }
    return tap;
}

// The hub's card about the train drawn at (x, y), until a tap or KEY (or DETAIL_MS).
// False when there is no train there.
static bool show_detail(int x, int y)
{
    char url[URL_MAX]; uint8_t *jpg; size_t len; int status;
    snprintf(url, sizeof(url), "%s/metra/%s/detail.jpg?w=%d&h=%d&r=%d&x=%d&y=%d&%s", hub_url(), s_view,
             BOARD.w, BOARD.h, BOARD.corner_r, x, y, s_query);
    if (net_fetch(url, &jpg, &len, MAX_JPEG, &status) != ESP_OK) return false;
    if (running()) jpeg_draw(jpg, len, 0, 0, 256);
    free(jpg);
    int64_t until = ms() + DETAIL_MS;
    while (running() && ms() < until) {
        key_ev_t ev;
        bool key = xQueueReceive(s_keys, &ev, pdMS_TO_TICKS(40)) == pdTRUE && ev.btn == BTN_KEY && ev.type == KEY_SHORT;
        if (key || tapped(NULL, NULL)) break;
    }
    return true;
}

static void draw_page(void)
{
    char url[URL_MAX]; uint8_t *jpg; size_t len;
    snprintf(url, sizeof(url), "%s/metra/%s/line.jpg?w=%d&h=%d&r=%d&%s", hub_url(), s_view,
             BOARD.w, BOARD.h, BOARD.corner_r, s_query);
    if (net_get(url, &jpg, &len, MAX_JPEG) != ESP_OK) return;   // keep the last page up
    if (running() && jpeg_draw(jpg, len, 0, 0, 256) == ESP_OK) hub_drawn();
    free(jpg);
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
        if (s_idle) { s_idle = false; board_fill(0, 0, BOARD.w, BOARD.h, 0x0000); }
        draw_page();
        int64_t until = ms() + REFRESH_MS;
        while (running() && ms() < until) {
            key_ev_t ev;
            bool key = xQueueReceive(s_keys, &ev, pdMS_TO_TICKS(40)) == pdTRUE && ev.btn == BTN_KEY && ev.type == KEY_SHORT;
            int tx, ty;
            if (tapped(&tx, &ty)) { show_detail(tx, ty); break; }          // then the page again
            if (key) break;                                                // refresh now
        }
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
    const cJSON *v = cJSON_GetArrayItem(views, 0), *id = cJSON_GetObjectItem(v, "id"), *qs = cJSON_GetObjectItem(v, "query");
    if (cJSON_IsString(id)) strlcpy(s_view, id->valuestring, sizeof(s_view));
    if (cJSON_IsString(qs)) strlcpy(s_query, qs->valuestring, sizeof(s_query));
    s_keys = xQueueCreate(8, sizeof(key_ev_t));
    xTaskCreate(draw_task, "metra", 4096, NULL, 5, &s_draw);
    ESP_LOGI(TAG, "view %s, %s", s_view, s_query);
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
    ui_pause(false);
}

static void app_key(key_ev_t ev) { xQueueSend(s_keys, &ev, 0); }

static void app_screen(bool on)
{
    s_screen_on = on;                     // off: stop fetching
    if (on) xTaskNotifyGive(s_draw); else wait_idle();
}

static const char *app_current(void) { return s_view; }
static void app_show(const char *view) {}

const app_t APP_METRA = {
    .id = "metra", .name = "Metra", .store_prefix = 'm', .raw = true,
    .init = app_init, .enter = app_enter, .leave = app_leave, .key = app_key, .screen = app_screen,
    .current = app_current, .show = app_show,
};
