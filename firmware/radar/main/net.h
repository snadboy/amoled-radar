#pragma once
#include <stddef.h>
#include <stdint.h>
#include "esp_err.h"

esp_err_t net_wifi_connect(int timeout_ms);
// GET url into a fresh heap buffer (caller frees). Fails above max_len.
esp_err_t net_get(const char *url, uint8_t **out, size_t *out_len, size_t max_len);
// GET url and hand the body to `sink` in chunks (e.g. straight into flash).
// Returns the byte count in *total. Fails on non-200 or a short body.
typedef esp_err_t (*net_sink_t)(void *ctx, const uint8_t *data, size_t n);
esp_err_t net_stream(const char *url, net_sink_t sink, void *ctx, size_t *total);
