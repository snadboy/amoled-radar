// Bring-up: find which GPIO the enclosure's KEY button is on.
//
// Watches every GPIO not already claimed by the panel (0-5), I2C (7/8), touch
// (11/15), USB (12/13), UART0 (16/17), audio (19-23) or flash (24+), and the
// AXP2101's power-key interrupt bits for the PWR button. Prints every change.
#include <stdio.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/gpio.h"
#include "driver/i2c_master.h"
#include "esp_timer.h"

static const int CANDIDATES[] = {6, 9, 10, 14, 18};
#define NCAND (sizeof(CANDIDATES) / sizeof(CANDIDATES[0]))
#define AXP2101_ADDR 0x34
#define AXP_IRQ_STATUS1 0x49   // bit0 PKEY rising, bit1 falling, bit2 long, bit3 short

void app_main(void)
{
    for (int i = 0; i < NCAND; i++) {
        gpio_config_t c = { .pin_bit_mask = 1ULL << CANDIDATES[i], .mode = GPIO_MODE_INPUT,
                            .pull_up_en = GPIO_PULLUP_ENABLE, .pull_down_en = GPIO_PULLDOWN_DISABLE };
        gpio_config(&c);
    }
    i2c_master_bus_handle_t bus = NULL; i2c_master_dev_handle_t pmu = NULL;
    i2c_master_bus_config_t bc = { .i2c_port = I2C_NUM_0, .sda_io_num = 8, .scl_io_num = 7,
                                   .clk_source = I2C_CLK_SRC_DEFAULT, .glitch_ignore_cnt = 7,
                                   .flags.enable_internal_pullup = true };
    bool have_pmu = false;
    if (i2c_new_master_bus(&bc, &bus) == ESP_OK) {
        i2c_device_config_t dc = { .dev_addr_length = I2C_ADDR_BIT_LEN_7, .device_address = AXP2101_ADDR, .scl_speed_hz = 100000 };
        have_pmu = i2c_master_bus_add_device(bus, &dc, &pmu) == ESP_OK && i2c_master_probe(bus, AXP2101_ADDR, 100) == ESP_OK;
    }
    printf("\nBRINGUP buttons: watching GPIO 6,9,10,14,18; AXP2101 %s\n", have_pmu ? "found" : "NOT found");

    int last[NCAND];
    for (int i = 0; i < NCAND; i++) { last[i] = gpio_get_level(CANDIDATES[i]); }
    printf("BRINGUP idle levels:"); for (int i = 0; i < NCAND; i++) printf(" G%d=%d", CANDIDATES[i], last[i]); printf("\n");

    int64_t beat = 0;
    while (1) {
        for (int i = 0; i < NCAND; i++) {
            int v = gpio_get_level(CANDIDATES[i]);
            if (v != last[i]) {
                printf("BRINGUP t=%lldms GPIO%d %s\n", esp_timer_get_time() / 1000, CANDIDATES[i], v ? "released (1)" : "PRESSED (0)");
                last[i] = v;
            }
        }
        if (have_pmu) {
            uint8_t reg = AXP_IRQ_STATUS1, st = 0;
            if (i2c_master_transmit_receive(pmu, &reg, 1, &st, 1, 50) == ESP_OK && (st & 0x0F)) {
                printf("BRINGUP t=%lldms AXP2101 PWR key irq=0x%02x%s%s%s%s\n", esp_timer_get_time() / 1000, st & 0x0F,
                       st & 1 ? " rising" : "", st & 2 ? " falling" : "", st & 4 ? " LONG" : "", st & 8 ? " SHORT" : "");
                uint8_t clr[2] = { AXP_IRQ_STATUS1, (uint8_t)(st & 0x0F) };   // write-1-to-clear
                i2c_master_transmit(pmu, clr, 2, 50);
            }
        }
        if (esp_timer_get_time() - beat > 10000000) { printf("BRINGUP alive\n"); beat = esp_timer_get_time(); }
        vTaskDelay(pdMS_TO_TICKS(10));
    }
}
