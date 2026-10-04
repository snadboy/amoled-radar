#pragma once
#include <stdbool.h>
#include "freertos/FreeRTOS.h"
#include "board.h"

// Button gestures from the board's three buttons (BTN_BOOT, BTN_KEY, BTN_PWR).
// BOOT and KEY: SHORT on release, or LONG once held KEYS_LONG_MS (fires while still
// held; the release after it is swallowed). PWR: SHORT only -- the PMU latches it.
typedef enum { KEY_SHORT, KEY_LONG } key_type_t;
typedef struct { int btn; key_type_t type; } key_ev_t;

#define KEYS_LONG_MS 700

void keys_start(void);
bool keys_get(key_ev_t *ev, TickType_t wait);
