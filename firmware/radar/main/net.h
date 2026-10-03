#pragma once
#include <stddef.h>
#include <stdint.h>
#include "esp_err.h"

esp_err_t net_wifi_connect(int timeout_ms);
// GET url into a fresh heap buffer (caller frees). Fails above max_len.
esp_err_t net_get(const char *url, uint8_t **out, size_t *out_len, size_t max_len);
