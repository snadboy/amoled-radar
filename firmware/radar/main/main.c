// Desk radar -- phase A: prove the pipeline on real hardware and measure it.
//
// Fetches the chosen view's loop from the server over HTTP and draws every frame,
// logging fetch time and decode+draw time separately. Decode+draw is the number
// that matters: phase B serves frames from flash, so fetch time drops out.
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "board.h"
#include "cJSON.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "jpeg_draw.h"
#include "net.h"
#include "sdkconfig.h"

static const char *TAG = "radar";
#define URL_MAX 160
#define MAX_JPEG (160 * 1024)

static int64_t ms(void) { return esp_timer_get_time() / 1000; }

static void show_status(const char *city)
{
    char url[URL_MAX];
    uint8_t *jpg; size_t len;
    snprintf(url, sizeof(url), "%s/c/%s/status.jpg", CONFIG_RADAR_SERVER_URL, city);
    if (net_get(url, &jpg, &len, MAX_JPEG) == ESP_OK) {
        jpeg_draw(jpg, len, 0, PANEL_H - 56, 256);
        free(jpg);
    }
}

void app_main(void)
{
    ESP_ERROR_CHECK(board_init());
    if (net_wifi_connect(30000) != ESP_OK) {
        ESP_LOGE(TAG, "no WiFi -- check RADAR_WIFI_SSID/PASSWORD and that it's 2.4 GHz");
        board_set_brightness(CONFIG_RADAR_BRIGHTNESS);
        board_fill(0, 0, PANEL_W, PANEL_H, 0xF800);        // red: no network
        return;
    }
    const char *city = CONFIG_RADAR_CITY;
    char url[URL_MAX];
    bool lit = false;
    int64_t status_at = 0;

    for (;;) {
        uint8_t *mj; size_t mlen;
        snprintf(url, sizeof(url), "%s/c/%s/manifest.json", CONFIG_RADAR_SERVER_URL, city);
        if (net_get(url, &mj, &mlen, 32 * 1024) != ESP_OK) {
            ESP_LOGW(TAG, "manifest unavailable, retrying in 10 s");
            vTaskDelay(pdMS_TO_TICKS(10000));
            continue;
        }
        cJSON *m = cJSON_Parse((char *)mj);
        free(mj);
        int frames = m ? cJSON_GetObjectItem(m, "frames")->valueint : 0;
        cJSON_Delete(m);
        ESP_LOGI(TAG, "%s: %d frames", city, frames);

        int64_t sum_fetch = 0, sum_draw = 0, worst_draw = 0;
        for (int i = 0; i < frames; i++) {
            if (ms() - status_at > 60000) { show_status(city); status_at = ms(); }
            uint8_t *jpg; size_t len;
            snprintf(url, sizeof(url), "%s/c/%s/frame/%d.jpg", CONFIG_RADAR_SERVER_URL, city, i);
            int64_t t0 = ms();
            if (net_get(url, &jpg, &len, MAX_JPEG) != ESP_OK) continue;
            int64_t t1 = ms();
            jpeg_draw(jpg, len, 0, 0, 256);
            board_draw_wait();
            int64_t t2 = ms();
            free(jpg);
            if (!lit) { ESP_LOGI(TAG, "first frame drawn"); lit = true; }
            sum_fetch += t1 - t0; sum_draw += t2 - t1;
            if (t2 - t1 > worst_draw) worst_draw = t2 - t1;
        }
        if (frames) {
            ESP_LOGI(TAG, "TIMING loop of %d: fetch avg %lld ms, decode+draw avg %lld ms (worst %lld) -> %.1f fps from flash",
                     frames, sum_fetch / frames, sum_draw / frames, worst_draw, 1000.0 * frames / (double)sum_draw);
        }
        vTaskDelay(pdMS_TO_TICKS(1500));                   // dwell on the latest frame
    }
}
