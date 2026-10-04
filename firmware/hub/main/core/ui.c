#include "ui.h"

#include "board.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

static SemaphoreHandle_t s_mx;
static volatile bool s_paused;

// The panel takes big-endian RGB565. board_draw() waits for the previous strip
// before queueing this one, so the strip LVGL renders into next is always free
// and flush can report ready at once.
static void flush_cb(lv_display_t *d, const lv_area_t *a, uint8_t *px)
{
    lv_draw_sw_rgb565_swap(px, lv_area_get_width(a) * lv_area_get_height(a));
    board_draw(a->x1, a->y1, a->x2 + 1, a->y2 + 1, px);
    lv_display_flush_ready(d);
}

// The CO5300 needs flush windows aligned to even coordinates.
static void rounder_cb(lv_event_t *e)
{
    lv_area_t *a = (lv_area_t *)lv_event_get_param(e);
    a->x1 &= ~1; a->y1 &= ~1; a->x2 |= 1; a->y2 |= 1;
}

static void touch_cb(lv_indev_t *in, lv_indev_data_t *data)
{
    int x, y;
    if (!s_paused && board_touch(&x, &y)) {
        data->point.x = x; data->point.y = y; data->state = LV_INDEV_STATE_PRESSED;
    } else {
        data->state = LV_INDEV_STATE_RELEASED;
    }
}

static void tick_cb(void *arg) { lv_tick_inc(2); }

static void task(void *arg)
{
    for (;;) {
        uint32_t wait = 30;
        if (!s_paused && ui_lock(0)) { wait = lv_timer_handler(); ui_unlock(); }
        if (wait < 5) wait = 5;
        if (wait > 100) wait = 100;
        vTaskDelay(pdMS_TO_TICKS(wait));
    }
}

bool ui_lock(int timeout_ms) { return xSemaphoreTake(s_mx, timeout_ms ? pdMS_TO_TICKS(timeout_ms) : portMAX_DELAY) == pdTRUE; }
void ui_unlock(void) { xSemaphoreGive(s_mx); }

void ui_pause(bool paused)
{
    if (paused == s_paused) return;
    ui_lock(0);
    s_paused = paused;
    if (!paused) lv_obj_invalidate(lv_screen_active());
    ui_unlock();
    if (paused) board_draw_wait();          // the panel is free once the last strip is out
}

void ui_init(void)
{
    s_mx = xSemaphoreCreateMutex();
    lv_init();
    lv_display_t *d = lv_display_create(BOARD.w, BOARD.h);
    lv_display_set_flush_cb(d, flush_cb);
    lv_display_set_buffers(d, board_band_buffer(0), board_band_buffer(1),
                           BOARD.w * BOARD.band_rows * 2, LV_DISPLAY_RENDER_MODE_PARTIAL);
    lv_display_add_event_cb(d, rounder_cb, LV_EVENT_INVALIDATE_AREA, NULL);
    lv_indev_t *in = lv_indev_create();
    lv_indev_set_type(in, LV_INDEV_TYPE_POINTER);
    lv_indev_set_read_cb(in, touch_cb);
    lv_obj_set_style_bg_color(lv_screen_active(), lv_color_black(), 0);
    esp_timer_create_args_t ta = { .callback = tick_cb, .name = "lv_tick" };
    esp_timer_handle_t t;
    ESP_ERROR_CHECK(esp_timer_create(&ta, &t));
    ESP_ERROR_CHECK(esp_timer_start_periodic(t, 2000));
    xTaskCreate(task, "lvgl", 8192, NULL, 3, NULL);
}
