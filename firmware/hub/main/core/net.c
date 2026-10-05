#include "net.h"

#include <stdio.h>
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
static volatile bool s_have_creds;
#define GOT_IP BIT0

static void on_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        if (s_have_creds) esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupClearBits(s_ev, GOT_IP);
        if (s_have_creds) { ESP_LOGW(TAG, "wifi disconnected, retrying"); esp_wifi_connect(); }
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *e = (ip_event_got_ip_t *)data;
        ESP_LOGI(TAG, "got ip " IPSTR, IP2STR(&e->ip_info.ip));
        xEventGroupSetBits(s_ev, GOT_IP);
    }
}

// WiFi credentials live on the board (ESP-IDF keeps the station config in NVS), set
// once by the install page over USB (improv.c) -- not in the firmware, which carries
// no secrets. A build with CONFIG_HUB_WIFI_SSID set (developer builds) still seeds them.
bool net_init(void)
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
    esp_netif_set_hostname(sta, "display-hub");
    wifi_init_config_t ic = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&ic));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, on_event, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, on_event, NULL));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    wifi_config_t wc = {0};
    esp_wifi_get_config(WIFI_IF_STA, &wc);
    if (!wc.sta.ssid[0] && CONFIG_HUB_WIFI_SSID[0]) {            // developer build: seed the board
        strlcpy((char *)wc.sta.ssid, CONFIG_HUB_WIFI_SSID, sizeof(wc.sta.ssid));
        strlcpy((char *)wc.sta.password, CONFIG_HUB_WIFI_PASSWORD, sizeof(wc.sta.password));
        wc.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;
        esp_wifi_set_config(WIFI_IF_STA, &wc);
    }
    s_have_creds = wc.sta.ssid[0] != 0;
    ESP_ERROR_CHECK(esp_wifi_start());
    if (s_have_creds) ESP_LOGI(TAG, "joining \"%s\"", (char *)wc.sta.ssid);
    else ESP_LOGW(TAG, "no WiFi set up yet -- waiting for the install page (Improv over USB)");
    return s_have_creds;
}

bool net_connected(void) { return xEventGroupGetBits(s_ev) & GOT_IP; }

esp_err_t net_wait(int timeout_ms)
{
    EventBits_t b = xEventGroupWaitBits(s_ev, GOT_IP, pdFALSE, pdTRUE, pdMS_TO_TICKS(timeout_ms));
    return (b & GOT_IP) ? ESP_OK : ESP_ERR_TIMEOUT;
}

esp_err_t net_wifi_set(const char *ssid, const char *pass, int timeout_ms)
{
    wifi_config_t wc = {0};
    strlcpy((char *)wc.sta.ssid, ssid, sizeof(wc.sta.ssid));
    strlcpy((char *)wc.sta.password, pass, sizeof(wc.sta.password));
    wc.sta.threshold.authmode = pass[0] ? WIFI_AUTH_WPA2_PSK : WIFI_AUTH_OPEN;
    ESP_LOGI(TAG, "new WiFi \"%s\" from the install page", ssid);
    s_have_creds = false;                    // no retry storm while switching networks
    esp_wifi_disconnect();
    xEventGroupClearBits(s_ev, GOT_IP);
    esp_err_t e = esp_wifi_set_config(WIFI_IF_STA, &wc);      // saved to NVS
    if (e != ESP_OK) return e;
    s_have_creds = true;
    esp_wifi_connect();
    return net_wait(timeout_ms);
}

esp_err_t net_fetch(const char *url, uint8_t **out, size_t *out_len, size_t max_len, int *status_out)
{
    *out = NULL; *out_len = 0;
    if (status_out) *status_out = 0;
    esp_http_client_config_t cfg = { .url = url, .timeout_ms = 15000, .buffer_size = 4096, .keep_alive_enable = true };
    esp_http_client_handle_t h = esp_http_client_init(&cfg);
    if (!h) return ESP_FAIL;
    esp_err_t err = esp_http_client_open(h, 0);
    if (err != ESP_OK) { esp_http_client_cleanup(h); return err; }
    int64_t cl = esp_http_client_fetch_headers(h);
    int status = esp_http_client_get_status_code(h);
    if (status_out) *status_out = status;
    if (status != 200 || cl <= 0 || (size_t)cl > max_len) {
        if (status != 304) ESP_LOGW(TAG, "GET %s -> status %d, length %lld", url, status, cl);
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

esp_err_t net_stream(const char *url, net_sink_t sink, void *ctx, size_t *total)
{
    *total = 0;
    esp_http_client_config_t cfg = { .url = url, .timeout_ms = 20000, .buffer_size = 4096 };
    esp_http_client_handle_t h = esp_http_client_init(&cfg);
    if (!h) return ESP_FAIL;
    esp_err_t err = esp_http_client_open(h, 0);
    if (err != ESP_OK) { esp_http_client_cleanup(h); return err; }
    int64_t cl = esp_http_client_fetch_headers(h);
    if (esp_http_client_get_status_code(h) != 200 || cl <= 0) {
        esp_http_client_close(h); esp_http_client_cleanup(h);
        return ESP_FAIL;
    }
    // One buffer per download: the weather and aircraft sync tasks stream at the same
    // time (a single static buffer had each one flashing the other's bytes).
    uint8_t *buf = malloc(4096);
    if (!buf) { esp_http_client_close(h); esp_http_client_cleanup(h); return ESP_ERR_NO_MEM; }
    size_t got = 0;
    err = ESP_OK;
    while (got < (size_t)cl) {
        int n = esp_http_client_read(h, (char *)buf, 4096);
        if (n <= 0) { err = ESP_FAIL; break; }
        if ((err = sink(ctx, buf, (size_t)n)) != ESP_OK) break;
        got += (size_t)n;
    }
    free(buf);
    esp_http_client_close(h); esp_http_client_cleanup(h);
    *total = got;
    if (got != (size_t)cl) ESP_LOGW(TAG, "stream %s: %u of %lld bytes (%s)", url, (unsigned)got, cl, esp_err_to_name(err));
    return (err == ESP_OK && got == (size_t)cl) ? ESP_OK : ESP_FAIL;
}

esp_err_t net_get(const char *url, uint8_t **out, size_t *out_len, size_t max_len)
{
    return net_fetch(url, out, out_len, max_len, NULL);
}

void net_ip(char out[16])
{
    esp_netif_ip_info_t ip = {0};
    esp_netif_get_ip_info(esp_netif_get_handle_from_ifkey("WIFI_STA_DEF"), &ip);
    snprintf(out, 16, IPSTR, IP2STR(&ip.ip));
}

void net_mac(char out[18])
{
    uint8_t m[6] = {0};
    esp_wifi_get_mac(WIFI_IF_STA, m);
    snprintf(out, 18, "%02x:%02x:%02x:%02x:%02x:%02x", m[0], m[1], m[2], m[3], m[4], m[5]);
}
