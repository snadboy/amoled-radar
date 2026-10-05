// display-hub firmware: one generic firmware, the hub does the heavy lifting.
//
// Boot: board -> store -> LVGL -> WiFi -> /device/hello (registers this board's
// profile and learns which apps and views the hub offers; cached in NVS for offline
// boots) -> the app the hub says this device starts on (its admin-page settings).
//
// Settings: the state poll carries the device's name (applied live) and a version of
// its boot settings (apps, starting app / city / zoom). When that version changes the
// device restarts to pick them up -- every boot starts from the hub's defaults.
//
// Buttons: PWR = screen off/on, BOOT = next app (hold: identity card), KEY = the active
// app's action.
// The screen also follows the hub (/device/<id>/state: room occupancy and light).
// A screen turned off by hand stays off until PWR: occupancy never wakes it. A
// screen woken by hand in an empty room stays on for 10 minutes.
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include "app.h"
#include "board.h"
#include "cJSON.h"
#include "esp_app_desc.h"
#include "esp_log.h"
#include "esp_netif_sntp.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "improv.h"
#include "keys.h"
#include "net.h"
#include "nvs.h"
#include "ota.h"
#include "sdkconfig.h"
#include "store.h"
#include "ui.h"

static const char *TAG = "hub";

extern const app_t APP_WEATHER, APP_AIRCRAFT;
static const app_t *const APPS[] = { &APP_WEATHER, &APP_AIRCRAFT };
#define NAPPS ((int)(sizeof(APPS) / sizeof(APPS[0])))

#define URL_MAX       256
#define STATE_MS      (30 * 1000)    // until the hub says otherwise (poll_s)
#define OTA_FIRST_MS  (90 * 1000)
#define OTA_MS        (6 * 60 * 60 * 1000)
#define MANUAL_ON_MS  (10 * 60 * 1000)

static char s_id[18], s_name[25];
static uint32_t s_sv;                // boot-settings version this boot started from
static volatile int s_state_ms = STATE_MS;
// A command from the hub (HA's App / City / Identify), picked up by the main loop.
static SemaphoreHandle_t s_cmd_mx;
static struct { bool pending, identify; char app[16], view[24]; } s_cmd;   // view may be "<id>@<dx>,<dy>"
static bool s_enabled[NAPPS];
static int s_cur = -1;
static bool s_on = true, s_manual_off, s_marked;
static int64_t s_manual_on_until;
static volatile bool s_srv_on = true;
static volatile int s_srv_bright = CONFIG_HUB_BRIGHTNESS;
static int s_applied_bright = -1;
static lv_obj_t *s_toast;

static int64_t ms(void) { return esp_timer_get_time() / 1000; }

const char *hub_url(void) { return CONFIG_HUB_SERVER_URL; }

void hub_drawn(void)
{
    if (!s_marked) { s_marked = true; ota_mark_good(); }
}

// ---------------------------------------------------------------- NVS
void hub_nvs_put(const char *k, const char *v)
{
    nvs_handle_t h;
    if (nvs_open("hub", NVS_READWRITE, &h) == ESP_OK) { nvs_set_str(h, k, v); nvs_commit(h); nvs_close(h); }
}

bool hub_nvs_get(const char *k, char *out, size_t sz)
{
    nvs_handle_t h; bool ok = false;
    if (nvs_open("hub", NVS_READONLY, &h) == ESP_OK) { ok = nvs_get_str(h, k, out, &sz) == ESP_OK; nvs_close(h); }
    return ok;
}

static char *nvs_dup(const char *k)
{
    nvs_handle_t h; size_t sz = 0; char *out = NULL;
    if (nvs_open("hub", NVS_READONLY, &h) != ESP_OK) return NULL;
    if (nvs_get_str(h, k, NULL, &sz) == ESP_OK && (out = malloc(sz)) && nvs_get_str(h, k, out, &sz) != ESP_OK) {
        free(out); out = NULL;
    }
    nvs_close(h);
    return out;
}

// ---------------------------------------------------------------- hello
static cJSON *hello(void)
{
    char url[URL_MAX];
    snprintf(url, sizeof(url), "%s/device/hello?id=%s&board=%s&w=%d&h=%d&r=%d&panel=%s&psram=%d&slot=%u&fw=%s",
             hub_url(), s_id, BOARD.board, BOARD.w, BOARD.h, BOARD.corner_r, BOARD.panel, BOARD.psram,
             (unsigned)store_slot_capacity(), esp_app_get_description()->version);
    for (int attempt = 0; attempt < 3; attempt++) {
        uint8_t *js; size_t len;
        if (net_get(url, &js, &len, 16 * 1024) == ESP_OK) {
            cJSON *j = cJSON_Parse((char *)js);
            if (cJSON_IsArray(cJSON_GetObjectItem(j, "apps"))) { hub_nvs_put("hello", (char *)js); free(js); return j; }
            cJSON_Delete(j); free(js);
        }
        vTaskDelay(pdMS_TO_TICKS(2000));
    }
    char *cached = nvs_dup("hello");                     // offline: what the hub said last time
    cJSON *j = cached ? cJSON_Parse(cached) : NULL;
    free(cached);
    ESP_LOGW(TAG, "hub unreachable -- %s", j ? "using the cached app list" : "no app list yet");
    return j;
}

// Enable the apps the hub offers, hand each its views, and tell the store which
// bundles are still wanted.
static void setup_apps(cJSON *h)
{
    static char keys[16][16];
    int nkeys = 0;
    const cJSON *list = h ? cJSON_GetObjectItem(h, "apps") : NULL, *a;
    for (int i = 0; i < NAPPS; i++) {
        const cJSON *views = NULL;
        cJSON_ArrayForEach(a, list) {
            const cJSON *id = cJSON_GetObjectItem(a, "id");
            if (cJSON_IsString(id) && !strcmp(id->valuestring, APPS[i]->id)) views = cJSON_GetObjectItem(a, "views");
        }
        if (!views && list) continue;                   // the hub doesn't offer this app
        s_enabled[i] = true;
        APPS[i]->init(views);
        const cJSON *v;
        cJSON_ArrayForEach(v, views) {
            const cJSON *vid = cJSON_GetObjectItem(v, "id");
            if (cJSON_IsString(vid) && nkeys < 16)
                snprintf(keys[nkeys++], sizeof(keys[0]), "%c:%.13s", APPS[i]->store_prefix, vid->valuestring);
        }
    }
    if (list) store_set_views((const char (*)[16])keys, nkeys);
}

// ---------------------------------------------------------------- apps
static void toast(const char *text)
{
    ui_lock(0);
    if (!s_toast) {
        s_toast = lv_label_create(lv_layer_top());
        lv_obj_set_style_bg_color(s_toast, lv_color_black(), 0);
        lv_obj_set_style_bg_opa(s_toast, LV_OPA_80, 0);
        lv_obj_set_style_radius(s_toast, LV_RADIUS_CIRCLE, 0);
        lv_obj_set_style_pad_hor(s_toast, 22, 0);
        lv_obj_set_style_pad_ver(s_toast, 10, 0);
        lv_obj_set_style_text_font(s_toast, &lv_font_montserrat_26, 0);
        lv_obj_set_style_text_color(s_toast, lv_color_white(), 0);
        lv_obj_align(s_toast, LV_ALIGN_CENTER, 0, 0);
    }
    lv_label_set_text(s_toast, text);
    lv_obj_remove_flag(s_toast, LV_OBJ_FLAG_HIDDEN);
    lv_anim_delete(s_toast, NULL);                 // a toast still fading out
    lv_obj_set_style_opa(s_toast, LV_OPA_COVER, 0);   // the last fade left it transparent
    lv_obj_fade_out(s_toast, 400, 1200);
    ui_unlock();
}

static void switch_to(int i, bool announce)
{
    if (i == s_cur) return;
    if (s_cur >= 0) APPS[s_cur]->leave();
    s_cur = i;
    APPS[i]->enter();
    if (announce && !APPS[i]->raw) toast(APPS[i]->name);   // LVGL is paused under a raw app
    ESP_LOGI(TAG, "app -> %s", APPS[i]->id);
}

static void next_app(void)
{
    for (int k = 1; k <= NAPPS; k++) {
        int i = (s_cur + k) % NAPPS;
        if (s_enabled[i]) {
            if (i == s_cur) { if (!APPS[i]->raw) toast(APPS[i]->name); }   // only one app: still acknowledge BOOT
            else switch_to(i, true);
            return;
        }
    }
}

static int first_app(cJSON *h)
{
    const cJSON *def = h ? cJSON_GetObjectItem(h, "default_app") : NULL;
    int pick = -1;
    for (int i = 0; i < NAPPS && pick < 0; i++)
        if (s_enabled[i] && cJSON_IsString(def) && !strcmp(def->valuestring, APPS[i]->id)) pick = i;
    for (int i = 0; i < NAPPS && pick < 0; i++) if (s_enabled[i]) pick = i;
    return pick;
}

// ---------------------------------------------------------------- identity
// "Which board is this?" -- its hub name, MAC, IP and firmware, at boot and on a
// BOOT hold. A raw app is stepped out of the way so LVGL can draw the card.
static void identify(int dur_ms)
{
    const app_t *a = s_cur >= 0 ? APPS[s_cur] : NULL;
    bool raw = a && a->raw;
    if (raw) a->leave();
    char ip[16], detail[96];
    net_ip(ip);
    snprintf(detail, sizeof(detail), "%s\n%s  -  fw %s", s_id, ip, esp_app_get_description()->version);
    ui_lock(0);
    lv_obj_t *card = lv_obj_create(lv_layer_top());
    lv_obj_remove_style_all(card);
    lv_obj_set_size(card, LV_SIZE_CONTENT, LV_SIZE_CONTENT);
    lv_obj_set_style_bg_color(card, lv_color_hex(0x101418), 0);
    lv_obj_set_style_bg_opa(card, LV_OPA_COVER, 0);
    lv_obj_set_style_border_color(card, lv_color_hex(0x3fb7a0), 0);
    lv_obj_set_style_border_width(card, 2, 0);
    lv_obj_set_style_radius(card, 20, 0);
    lv_obj_set_style_pad_all(card, 20, 0);
    lv_obj_set_style_pad_row(card, 8, 0);
    lv_obj_set_flex_flow(card, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_flex_align(card, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_t *t = lv_label_create(card);
    lv_label_set_text(t, s_name);
    lv_obj_set_style_text_font(t, &lv_font_montserrat_26, 0);
    lv_obj_set_style_text_color(t, lv_color_white(), 0);
    lv_obj_t *d = lv_label_create(card);
    lv_label_set_text(d, detail);
    lv_obj_set_style_text_align(d, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_set_style_text_font(d, &lv_font_montserrat_16, 0);
    lv_obj_set_style_text_color(d, lv_color_hex(0xa8b0b8), 0);
    lv_obj_center(card);
    ui_unlock();
    vTaskDelay(pdMS_TO_TICKS(dur_ms));
    ui_lock(0);
    lv_obj_delete(card);
    ui_unlock();
    if (raw) a->enter();
}

// ---------------------------------------------------------------- screen
static void apply_brightness(void)
{
    if (s_srv_bright != s_applied_bright) { board_set_brightness((uint8_t)s_srv_bright); s_applied_bright = s_srv_bright; }
}

static void screen(bool on, bool manual)
{
    if (on == s_on) return;
    s_on = on;
    if (!on) {
        APPS[s_cur]->screen(false);
        ui_pause(true);
        board_display(false);
        s_manual_off = manual;
    } else {
        board_display(true);
        s_manual_off = false;
        if (manual && !s_srv_on) s_manual_on_until = ms() + MANUAL_ON_MS;
        s_applied_bright = -1; apply_brightness();
        if (!APPS[s_cur]->raw) ui_pause(false);
        APPS[s_cur]->screen(true);
    }
    ESP_LOGI(TAG, "screen %s (%s)", on ? "on" : "off", manual ? "PWR" : on ? "room occupied" : "room empty");
}

static void poll_state(void)
{
    char url[URL_MAX]; uint8_t *js; size_t len;
    const app_t *a = s_cur >= 0 ? APPS[s_cur] : NULL;
    snprintf(url, sizeof(url), "%s/device/%s/state?app=%s&view=%s&on=%d&bright=%d", hub_url(), s_id,
             a ? a->id : "", a ? a->current() : "", s_on, s_applied_bright < 0 ? 0 : s_applied_bright);
    if (net_get(url, &js, &len, 4096) != ESP_OK) return;
    cJSON *d = cJSON_Parse((char *)js);
    free(js);
    const cJSON *disp = cJSON_GetObjectItem(d, "display"), *br = cJSON_GetObjectItem(d, "brightness");
    if (cJSON_IsString(disp)) s_srv_on = strcmp(disp->valuestring, "off") != 0;
    if (cJSON_IsNumber(br) && br->valueint > 0 && br->valueint < 256) s_srv_bright = br->valueint;
    const cJSON *nm = cJSON_GetObjectItem(d, "name"), *sv = cJSON_GetObjectItem(d, "sv");
    if (cJSON_IsString(nm) && strcmp(nm->valuestring, s_name)) {         // renamed on the admin page: live
        strlcpy(s_name, nm->valuestring, sizeof(s_name));
        ESP_LOGI(TAG, "renamed: %s", s_name);
    }
    uint32_t v = cJSON_IsNumber(sv) ? (uint32_t)sv->valuedouble : 0;
    const cJSON *ps = cJSON_GetObjectItem(d, "poll_s");
    if (cJSON_IsNumber(ps) && ps->valueint >= 2 && ps->valueint <= 120) s_state_ms = ps->valueint * 1000;
    const cJSON *cmd = cJSON_GetObjectItem(d, "cmd");
    if (cJSON_IsObject(cmd)) {
        if (cJSON_IsTrue(cJSON_GetObjectItem(cmd, "restart"))) {
            ESP_LOGW(TAG, "restart requested by the hub");
            cJSON_Delete(d); vTaskDelay(pdMS_TO_TICKS(200)); esp_restart();
        }
        const cJSON *ca = cJSON_GetObjectItem(cmd, "app"), *cv = cJSON_GetObjectItem(cmd, "view");
        xSemaphoreTake(s_cmd_mx, portMAX_DELAY);
        strlcpy(s_cmd.app, cJSON_IsString(ca) ? ca->valuestring : "", sizeof(s_cmd.app));
        strlcpy(s_cmd.view, cJSON_IsString(cv) ? cv->valuestring : "", sizeof(s_cmd.view));
        s_cmd.identify = cJSON_IsTrue(cJSON_GetObjectItem(cmd, "identify"));
        s_cmd.pending = true;
        xSemaphoreGive(s_cmd_mx);
    }
    cJSON_Delete(d);
    if (v && v != s_sv) {                // apps / starting points changed: start over from them
        ESP_LOGW(TAG, "boot settings changed (%lu -> %lu): restarting", (unsigned long)s_sv, (unsigned long)v);
        vTaskDelay(pdMS_TO_TICKS(200));
        esp_restart();
    }
}

static void bg_task(void *arg)
{
    int64_t state_at = 0, ota_at = 0;
    for (;;) {
        int64_t now = ms();
        if (!state_at || now - state_at > s_state_ms) { poll_state(); state_at = ms(); }
        if (now > OTA_FIRST_MS && (!ota_at || now - ota_at > OTA_MS)) { ota_at = ms(); ota_check(); }
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}

// A board with no WiFi saved: say how to set it up, and wait. The install page sends the
// credentials over USB (improv.c); then boot carries on as normal.
static void wifi_setup_screen(void)
{
    char mac[18], txt[300];
    net_mac(mac);
    snprintf(txt, sizeof(txt), "Set up WiFi\n\n#a8b0b8 Plug this display into a computer by USB, open#\n%s/install\n"
             "#a8b0b8 and click Install, then Connect to WiFi.#\n\n#7f8a94 %s#", CONFIG_HUB_ADMIN_URL, mac);
    ui_lock(0);
    lv_obj_t *l = lv_label_create(lv_layer_top());
    lv_label_set_recolor(l, true);
    lv_label_set_text(l, txt);
    lv_obj_set_width(l, BOARD.w - 40);
    lv_label_set_long_mode(l, LV_LABEL_LONG_MODE_WRAP);
    lv_obj_set_style_text_align(l, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_set_style_text_font(l, &lv_font_montserrat_18, 0);
    lv_obj_set_style_text_color(l, lv_color_white(), 0);
    lv_obj_center(l);
    ui_unlock();
    while (net_wait(60000) != ESP_OK) {}
    ui_lock(0); lv_obj_delete(l); ui_unlock();
}

// A command from the hub, on the main task (it owns app switching and the identity card).
static void run_command(void)
{
    xSemaphoreTake(s_cmd_mx, portMAX_DELAY);
    char app[16], view[24]; bool ident = s_cmd.identify;
    strlcpy(app, s_cmd.app, sizeof(app)); strlcpy(view, s_cmd.view, sizeof(view));
    s_cmd.pending = false;
    xSemaphoreGive(s_cmd_mx);
    ESP_LOGI(TAG, "hub command: app=%s view=%s identify=%d", app, view, ident);
    for (int i = 0; i < NAPPS && app[0]; i++)
        if (s_enabled[i] && !strcmp(APPS[i]->id, app)) switch_to(i, true);
    if (view[0] && s_cur >= 0) APPS[s_cur]->show(view);
    if (ident && s_on) identify(4000);
}

// ---------------------------------------------------------------- main
void app_main(void)
{
    s_cmd_mx = xSemaphoreCreateMutex();
    ESP_ERROR_CHECK(board_init());
    keys_start();
    ESP_ERROR_CHECK(store_init());
    ui_init();
    ESP_LOGI(TAG, "firmware %s on %s, hub %s", esp_app_get_description()->version, BOARD.board, hub_url());
    improv_start(CONFIG_HUB_ADMIN_URL);                 // WiFi set-up from the install page, any time
    if (!net_init()) wifi_setup_screen();               // nothing saved yet: wait for the install page
    else if (net_wait(30000) != ESP_OK) ESP_LOGW(TAG, "WiFi not up yet -- using cached data, still retrying");
    net_mac(s_id);
    setenv("TZ", CONFIG_HUB_TZ, 1); tzset();
    esp_sntp_config_t sc = ESP_NETIF_SNTP_DEFAULT_CONFIG("pool.ntp.org");
    esp_netif_sntp_init(&sc);

    cJSON *h = hello();
    const cJSON *nm = cJSON_GetObjectItem(h, "name");
    strlcpy(s_name, cJSON_IsString(nm) ? nm->valuestring : "Display", sizeof(s_name));
    const cJSON *sv = cJSON_GetObjectItem(h, "sv");
    s_sv = cJSON_IsNumber(sv) ? (uint32_t)sv->valuedouble : 0;
    identify(3000);                                     // which board is this?
    setup_apps(h);
    int first = first_app(h);
    cJSON_Delete(h);
    if (first < 0) { ESP_LOGE(TAG, "no app to run"); first = 0; s_enabled[0] = true; APPS[0]->init(NULL); }
    switch_to(first, false);
    xTaskCreate(bg_task, "bg", 6144, NULL, 4, NULL);

    for (;;) {
        key_ev_t ev;
        if (keys_get(&ev, pdMS_TO_TICKS(200))) {
            static const char *names[] = { "BOOT", "KEY", "PWR" };
            ESP_LOGI(TAG, "button %s %s", names[ev.btn], ev.type == KEY_SHORT ? "short" : "long");
            if (ev.btn == BTN_PWR) screen(!s_on, true);
            else if (!s_on) screen(true, true);                 // any button wakes a dark screen
            else if (ev.btn == BTN_BOOT) { if (ev.type == KEY_SHORT) next_app(); else identify(4000); }
            else APPS[s_cur]->key(ev);
        }
        if (s_cmd.pending) run_command();
        if (s_on && !s_srv_on && ms() > s_manual_on_until) screen(false, false);
        else if (!s_on && !s_manual_off && s_srv_on) screen(true, false);
        if (s_on) apply_brightness();
    }
}
