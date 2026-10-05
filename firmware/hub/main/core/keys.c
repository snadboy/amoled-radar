// Polls BOOT/KEY every 10 ms with a 30 ms debounce, and the PMU's PWR latch every
// 100 ms (it is an I2C read, shared with touch).
#include "keys.h"

#include "esp_timer.h"
#include "freertos/queue.h"
#include "freertos/task.h"

static QueueHandle_t s_q;

static void post(int btn, key_type_t t) { key_ev_t e = { btn, t }; xQueueSend(s_q, &e, 0); }

static void task(void *arg)
{
    int stable[2] = {0, 0}, last[2] = {0, 0};
    int64_t changed[2] = {0}, down_at[2] = {0};
    bool longed[2] = {false, false};
    int tick = 0;
    for (;;) {
        int64_t now = esp_timer_get_time() / 1000;
        for (int b = 0; b < 2; b++) {                 // BTN_BOOT, BTN_KEY
            int v = board_button_down(b);
            if (v != last[b]) { last[b] = v; changed[b] = now; }
            if (v != stable[b] && now - changed[b] >= 30) {
                stable[b] = v;
                if (v) { down_at[b] = now; longed[b] = false; }
                else if (!longed[b]) post(b, KEY_SHORT);
            }
            if (stable[b] && !longed[b] && now - down_at[b] >= KEYS_LONG_MS) {
                longed[b] = true;
                if (BOARD.one_button && b == BTN_KEY) post(BTN_BOOT, KEY_SHORT);   // one button: hold = next app
                else post(b, KEY_LONG);
            }
        }
        if (++tick % 10 == 0 && board_pwr_pressed()) post(BTN_PWR, KEY_SHORT);
        vTaskDelay(pdMS_TO_TICKS(10));
    }
}

void keys_start(void)
{
    s_q = xQueueCreate(8, sizeof(key_ev_t));
    xTaskCreate(task, "keys", 2560, NULL, 6, NULL);
}

bool keys_get(key_ev_t *ev, TickType_t wait) { return xQueueReceive(s_q, ev, wait) == pdTRUE; }
