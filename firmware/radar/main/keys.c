// KEY (GPIO10, active low) -> short / hold events. Polled every 10 ms with a
// 30 ms debounce; a long press fires while still held, and the release that
// follows it is swallowed so one gesture never produces two actions.
#include "keys.h"

#include "driver/gpio.h"
#include "esp_timer.h"
#include "freertos/task.h"

#define KEY_GPIO 10
static QueueHandle_t s_q;
static volatile bool s_held;

static void post(key_event_t e) { xQueueSend(s_q, &e, 0); }

static void task(void *arg)
{
    int stable = 1, last = 1;
    int64_t changed = 0, down_at = 0;
    bool hinted = false, longed = false;
    for (;;) {
        int64_t now = esp_timer_get_time() / 1000;
        int v = gpio_get_level(KEY_GPIO);
        if (v != last) { last = v; changed = now; }
        if (v != stable && now - changed >= 30) {
            stable = v;
            if (stable == 0) {                       // pressed
                down_at = now; hinted = longed = false; s_held = true;
            } else {                                 // released
                s_held = false;
                if (!longed) {
                    if (hinted) post(KEY_HOLD_CANCEL);
                    post(KEY_SHORT);
                }
            }
        }
        if (stable == 0 && !longed) {
            if (!hinted && now - down_at >= KEY_HINT_MS) { hinted = true; post(KEY_HOLD_START); }
            if (now - down_at >= KEY_LONG_MS) { longed = true; post(KEY_LONG); }
        }
        vTaskDelay(pdMS_TO_TICKS(10));
    }
}

void keys_start(void)
{
    gpio_config_t c = { .pin_bit_mask = 1ULL << KEY_GPIO, .mode = GPIO_MODE_INPUT, .pull_up_en = GPIO_PULLUP_ENABLE };
    gpio_config(&c);
    s_q = xQueueCreate(8, sizeof(key_event_t));
    xTaskCreate(task, "keys", 2048, NULL, 6, NULL);
}

bool keys_get(key_event_t *ev, TickType_t wait) { return xQueueReceive(s_q, ev, wait) == pdTRUE; }
bool keys_held(void) { return s_held; }
