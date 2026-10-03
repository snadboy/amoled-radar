#include "net.h"

#include <stdlib.h>
#include <string.h>
#include "esp_event.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "nvs_flash.h"
#include "sdkconfig.h"

static const char *TAG = "net";
static EventGroupHandle_t s_ev;
#define GOT_IP BIT0

static void on_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupClearBits(s_ev, GOT_IP);
        ESP_LOGW(TAG, "wifi disconnected, retrying");
        esp_wifi_connect();
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *e = (ip_event_got_ip_t *)data;
        ESP_LOGI(TAG, "got ip " IPSTR, IP2STR(&e->ip_info.ip));
        xEventGroupSetBits(s_ev, GOT_IP);
    }
}

esp_err_t net_wifi_connect(int timeout_ms)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase();
        nvs_flash_init();
    }
    s_ev = xEventGroupCreate();
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_t *sta = esp_netif_create_default_wifi_sta();
    esp_netif_set_hostname(sta, "desk-radar");
    wifi_init_config_t ic = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&ic));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, on_event, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, on_event, NULL));
    wifi_config_t wc = {0};
    strlcpy((char *)wc.sta.ssid, CONFIG_RADAR_WIFI_SSID, sizeof(wc.sta.ssid));
    strlcpy((char *)wc.sta.password, CONFIG_RADAR_WIFI_PASSWORD, sizeof(wc.sta.password));
    wc.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wc));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_LOGI(TAG, "joining \"%s\"", CONFIG_RADAR_WIFI_SSID);
    EventBits_t b = xEventGroupWaitBits(s_ev, GOT_IP, pdFALSE, pdTRUE, pdMS_TO_TICKS(timeout_ms));
    return (b & GOT_IP) ? ESP_OK : ESP_ERR_TIMEOUT;
}

esp_err_t net_get(const char *url, uint8_t **out, size_t *out_len, size_t max_len)
{
    *out = NULL; *out_len = 0;
    esp_http_client_config_t cfg = { .url = url, .timeout_ms = 15000, .buffer_size = 4096, .keep_alive_enable = true };
    esp_http_client_handle_t h = esp_http_client_init(&cfg);
    if (!h) return ESP_FAIL;
    esp_err_t err = esp_http_client_open(h, 0);
    if (err != ESP_OK) { esp_http_client_cleanup(h); return err; }
    int64_t cl = esp_http_client_fetch_headers(h);
    int status = esp_http_client_get_status_code(h);
    if (status != 200 || cl <= 0 || (size_t)cl > max_len) {
        ESP_LOGW(TAG, "GET %s -> status %d, length %lld", url, status, cl);
        esp_http_client_close(h); esp_http_client_cleanup(h);
        return ESP_FAIL;
    }
    uint8_t *buf = malloc((size_t)cl + 1);
    if (!buf) { esp_http_client_close(h); esp_http_client_cleanup(h); return ESP_ERR_NO_MEM; }
    size_t got = 0;
    while (got < (size_t)cl) {
        int n = esp_http_client_read(h, (char *)buf + got, (int)((size_t)cl - got));
        if (n <= 0) break;
        got += (size_t)n;
    }
    esp_http_client_close(h); esp_http_client_cleanup(h);
    if (got != (size_t)cl) { free(buf); return ESP_FAIL; }
    buf[got] = 0;                                // handy for JSON
    *out = buf; *out_len = got;
    return ESP_OK;
}
