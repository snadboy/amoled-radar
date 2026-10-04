// OTA from the LAN hub, on this firmware's channel (/firmware/<channel>.json|.bin). The firmware binary embeds the WiFi password, so
// it is published only to the server's local volume, never to GitHub.
//
// Rollback is enabled: a freshly installed image boots "pending verify" and is
// only kept once ota_mark_good() runs after it has drawn a frame. If it crashes
// or hangs before that, the bootloader returns to the previous image.
#include "ota.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "cJSON.h"
#include "esp_app_desc.h"
#include "esp_http_client.h"
#include "esp_https_ota.h"
#include "esp_log.h"
#include "esp_ota_ops.h"
#include "esp_system.h"
#include "net.h"
#include "sdkconfig.h"

static const char *TAG = "ota";

void ota_mark_good(void)
{
    esp_ota_img_states_t st;
    const esp_partition_t *run = esp_ota_get_running_partition();
    if (esp_ota_get_state_partition(run, &st) == ESP_OK && st == ESP_OTA_IMG_PENDING_VERIFY) {
        esp_ota_mark_app_valid_cancel_rollback();
        ESP_LOGI(TAG, "new firmware %s confirmed good", esp_app_get_description()->version);
    }
}

bool ota_check(void)
{
    char url[160];
    uint8_t *js; size_t len;
    snprintf(url, sizeof(url), "%s/firmware/%s.json", CONFIG_HUB_SERVER_URL, CONFIG_HUB_FW_CHANNEL);
    if (net_get(url, &js, &len, 4096) != ESP_OK) return false;     // nothing published
    cJSON *j = cJSON_Parse((char *)js);
    free(js);
    const cJSON *v = j ? cJSON_GetObjectItem(j, "version") : NULL;
    const char *mine = esp_app_get_description()->version;
    bool newer = cJSON_IsString(v) && strcmp(v->valuestring, mine) != 0;
    if (newer) ESP_LOGW(TAG, "update %s -> %s", mine, v->valuestring);
    cJSON_Delete(j);
    if (!newer) return false;

    snprintf(url, sizeof(url), "%s/firmware/%s.bin", CONFIG_HUB_SERVER_URL, CONFIG_HUB_FW_CHANNEL);
    esp_http_client_config_t hc = { .url = url, .timeout_ms = 30000, .keep_alive_enable = true };
    esp_https_ota_config_t oc = { .http_config = &hc };
    esp_err_t e = esp_https_ota(&oc);
    if (e == ESP_OK) {
        ESP_LOGW(TAG, "update installed, restarting");
        esp_restart();
    }
    ESP_LOGE(TAG, "update failed: %s", esp_err_to_name(e));
    return false;
}
