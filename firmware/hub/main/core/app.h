#pragma once
// An app owns the screen while it is active. The core (main.c) handles WiFi, the
// hub hello, the screen policy, OTA, PWR (screen) and BOOT (next app); every other
// button event goes to the active app.
#include <stdbool.h>
#include <stddef.h>
#include "cJSON.h"
#include "keys.h"

typedef struct {
    const char *id;               // the hub's app id ("aircraft", "weather")
    const char *name;             // shown when switching to it
    char store_prefix;            // flash store keys are "<prefix>:<view>"
    bool raw;                     // draws to the panel itself: the core keeps LVGL paused while it's active
    void (*init)(const cJSON *views);   // once at boot, with this app's views from /device/hello
    void (*enter)(void);          // becomes the active app (LVGL apps unhide their screen)
    void (*leave)(void);
    void (*key)(key_ev_t ev);
    void (*screen)(bool on);      // the active app's screen went on/off (stop/resume fetching)
    const char *(*current)(void); // the view it shows now (reported to the hub -> HA)
    void (*show)(const char *view);   // switch to a view now (from HA); unknown ids are ignored
} app_t;

// Services the core offers apps.
const char *hub_url(void);        // e.g. http://192.168.86.135:8098
void hub_drawn(void);             // the active app drew real content: confirms a fresh OTA image
bool hub_nvs_get(const char *key, char *out, size_t size);   // the core's NVS namespace
void hub_nvs_put(const char *key, const char *value);
