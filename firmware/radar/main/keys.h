#pragma once
#include <stdbool.h>
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"

// The enclosure's KEY button (GPIO10, active low).
typedef enum {
    KEY_SHORT,        // released before the long-press threshold
    KEY_HOLD_START,   // held 250 ms: show "Hold to turn off" and start its bar
    KEY_HOLD_CANCEL,  // released between 250 ms and the threshold (a SHORT follows)
    KEY_LONG,         // held KEY_LONG_MS: fires once, while still held
} key_event_t;

#define KEY_LONG_MS 1000
#define KEY_HINT_MS 250

void keys_start(void);
bool keys_get(key_event_t *ev, TickType_t wait);
bool keys_held(void);
