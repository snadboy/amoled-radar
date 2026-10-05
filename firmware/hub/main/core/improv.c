// Improv WiFi over the serial console (https://www.improv-wifi.com/serial/). The hub's
// install page (ESP Web Tools) flashes a board, then sends the WiFi name and password
// over the same USB cable -- so WiFi credentials never live in the firmware or the repo.
// Always running, so a board can also be moved to another network later.
//
// Packet: "IMPROV" | version 1 | type | length | data | checksum (sum of all before) | '\n'
#include "improv.h"

#include <string.h>
#include "esp_app_desc.h"
#include "esp_log.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "net.h"
#include "sdkconfig.h"
#if CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG
#include "driver/usb_serial_jtag.h"
#include "driver/usb_serial_jtag_vfs.h"
#else
#include "driver/uart.h"
#include "driver/uart_vfs.h"
#endif

static const char *TAG = "improv";

enum { T_STATE = 1, T_ERROR = 2, T_RPC = 3, T_RESULT = 4 };
enum { ST_READY = 2, ST_PROVISIONING = 3, ST_PROVISIONED = 4 };
enum { E_NONE = 0, E_INVALID = 1, E_UNKNOWN_CMD = 2, E_CONNECT = 3 };
enum { C_WIFI = 1, C_STATE = 2, C_INFO = 3, C_SCAN = 4 };

static const char *s_url = "";

// ---------------------------------------------------------------- transport
static void io_init(void)
{
#if CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG
    usb_serial_jtag_driver_config_t c = { .rx_buffer_size = 512, .tx_buffer_size = 1024 };
    usb_serial_jtag_driver_install(&c);
    usb_serial_jtag_vfs_use_driver();          // logs share the driver, so writes don't collide
#else
    uart_driver_install(UART_NUM_0, 1024, 1024, 0, NULL, 0);
    uart_vfs_dev_use_driver(UART_NUM_0);
#endif
}

static int io_read(uint8_t *b, int n, int ms)
{
#if CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG
    return usb_serial_jtag_read_bytes(b, n, pdMS_TO_TICKS(ms));
#else
    return uart_read_bytes(UART_NUM_0, b, n, pdMS_TO_TICKS(ms));
#endif
}

static void io_write(const uint8_t *b, int n)  // one call per packet: logs can't land inside it
{
#if CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG
    usb_serial_jtag_write_bytes(b, n, pdMS_TO_TICKS(200));
#else
    uart_write_bytes(UART_NUM_0, b, n);
#endif
}

// ---------------------------------------------------------------- packets
static void send(uint8_t type, const uint8_t *data, int len)
{
    uint8_t p[300];
    if (len > (int)sizeof(p) - 12) return;
    memcpy(p, "IMPROV", 6);
    p[6] = 1; p[7] = type; p[8] = (uint8_t)len;
    memcpy(p + 9, data, len);
    uint8_t sum = 0;
    for (int i = 0; i < 9 + len; i++) sum += p[i];
    p[9 + len] = sum; p[10 + len] = '\n';
    io_write(p, 11 + len);
}

static void send_state(uint8_t st) { send(T_STATE, &st, 1); }
static void send_error(uint8_t e) { send(T_ERROR, &e, 1); }

// RPC result: command, total length, then length-prefixed strings.
static void send_result(uint8_t cmd, const char *const *strs, int n)
{
    uint8_t d[280]; int len = 2;
    for (int i = 0; i < n; i++) {
        int l = strlen(strs[i]);
        if (len + 1 + l > (int)sizeof(d)) break;
        d[len++] = (uint8_t)l; memcpy(d + len, strs[i], l); len += l;
    }
    d[0] = cmd; d[1] = (uint8_t)(len - 2);
    send(T_RESULT, d, len);
}

static void rpc(const uint8_t *d, int len)
{
    if (len < 2 || d[1] != len - 2) { send_error(E_INVALID); return; }
    uint8_t cmd = d[0];
    const uint8_t *a = d + 2; int alen = len - 2;
    if (cmd == C_WIFI) {
        if (alen < 1 || a[0] + 2 > alen || a[0] + 1 + a[a[0] + 1] + 1 > alen) { send_error(E_INVALID); return; }
        char ssid[33] = {0}, pass[65] = {0};
        memcpy(ssid, a + 1, a[0] < 32 ? a[0] : 32);
        int pl = a[a[0] + 1];
        memcpy(pass, a + a[0] + 2, pl < 64 ? pl : 64);
        send_error(E_NONE);
        send_state(ST_PROVISIONING);
        if (net_wifi_set(ssid, pass, 20000) == ESP_OK) {
            send_state(ST_PROVISIONED);
            const char *r[] = { s_url };
            send_result(C_WIFI, r, 1);
            ESP_LOGI(TAG, "WiFi set up from the install page");
        } else {
            send_error(E_CONNECT);
            send_state(ST_READY);
        }
    } else if (cmd == C_STATE) {
        if (net_connected()) { send_state(ST_PROVISIONED); const char *r[] = { s_url }; send_result(C_STATE, r, 1); }
        else send_state(ST_READY);
    } else if (cmd == C_INFO) {
#if CONFIG_IDF_TARGET_ESP32P4
        const char *chip = "ESP32-P4";
#elif CONFIG_IDF_TARGET_ESP32
        const char *chip = "ESP32";
#else
        const char *chip = "ESP32-C6";
#endif
        const char *r[] = { "Display Hub", esp_app_get_description()->version, chip, "Display" };
        send_result(C_INFO, r, 4);
    } else if (cmd == C_SCAN) {
        uint16_t n = 0;
        if (esp_wifi_scan_start(NULL, true) == ESP_OK) esp_wifi_scan_get_ap_num(&n);
        if (n > 20) n = 20;
        static wifi_ap_record_t recs[20];   // static: 1.6 KB on this task's stack overflowed it
        esp_wifi_scan_get_ap_records(&n, recs);
        for (int i = 0; i < n; i++) {
            if (!recs[i].ssid[0]) continue;
            char rssi[8]; snprintf(rssi, sizeof(rssi), "%d", recs[i].rssi);
            const char *r[] = { (const char *)recs[i].ssid, rssi, recs[i].authmode == WIFI_AUTH_OPEN ? "NO" : "YES" };
            send_result(C_SCAN, r, 3);
        }
        send_result(C_SCAN, NULL, 0);           // end of list
    } else {
        send_error(E_UNKNOWN_CMD);
    }
}

static void task(void *arg)
{
    static uint8_t buf[320];
    int have = 0;
    for (;;) {
        int n = io_read(buf + have, sizeof(buf) - have, 100);
        if (n <= 0) continue;
        have += n;
        for (;;) {                               // find and handle every complete packet
            int start = -1;
            for (int i = 0; i + 6 <= have; i++) if (!memcmp(buf + i, "IMPROV", 6)) { start = i; break; }
            if (start < 0) { int keep = have < 5 ? have : 5; memmove(buf, buf + have - keep, keep); have = keep; break; }
            if (start) { memmove(buf, buf + start, have - start); have -= start; }
            if (have < 9) break;
            int len = buf[8];
            if (have < 10 + len) break;
            uint8_t sum = 0;
            for (int i = 0; i < 9 + len; i++) sum += buf[i];
            if (buf[6] == 1 && sum == buf[9 + len] && buf[7] == T_RPC) rpc(buf + 9, len);
            else if (buf[6] == 1 && sum != buf[9 + len]) send_error(E_INVALID);
            memmove(buf, buf + 10 + len, have - 10 - len); have -= 10 + len;
        }
        if (have == sizeof(buf)) have = 0;     // garbage: start over
    }
}

void improv_start(const char *url)
{
    s_url = url;
    io_init();
    xTaskCreate(task, "improv", 8192, NULL, 5, NULL);   // joins WiFi and logs from here
}
