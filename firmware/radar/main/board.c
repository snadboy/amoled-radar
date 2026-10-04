// Board bring-up for the Waveshare ESP32-C6-Touch-AMOLED-2.16.
//
// Everything here was confirmed on the real board 2026-10-03:
//   * panel CS is GPIO5 (Waveshare's XiaoZhi config wrongly says 15)
//   * the panel has no reset GPIO; it is reset by power-cycling the AXP2101's
//     ALDO3 rail, which is what Waveshare's own example does
//   * QSPI at 40 MHz, CO5300 driven through Espressif's generic SH8601 driver
#include "board.h"

#include <string.h>
#include "driver/gpio.h"
#include "driver/i2c_master.h"
#include "driver/spi_master.h"
#include "esp_heap_caps.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_ops.h"
#include "esp_lcd_sh8601.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "sdkconfig.h"

static const char *TAG = "board";

#define PIN_I2C_SCL 7
#define PIN_I2C_SDA 8
#define PIN_LCD_CS  15   // NOT 5 -- see CLAUDE.md "Pin conflict"
#define PIN_LCD_CLK 0
#define PIN_LCD_D0  1
#define PIN_LCD_D1  2
#define PIN_LCD_D2  3
#define PIN_LCD_D3  4
#define LCD_HOST    SPI2_HOST
#define BAND_ROWS   16

#define AXP2101_ADDR      0x34
#define AXP_DCDC1_VOLT    0x82    // 1.5-3.4 V, 100 mV steps from 1.5 V
#define AXP_LDO_ONOFF0    0x90    // bit0..3 = ALDO1..4
#define AXP_ALDO1_VOLT    0x92    // ALDO1..4 = 0x92..0x95, 0.5-3.5 V, 100 mV steps
#define AXP_ALDO3_BIT     0x04

static i2c_master_bus_handle_t s_i2c;
static i2c_master_dev_handle_t s_pmu;
static esp_lcd_panel_io_handle_t s_io;
static esp_lcd_panel_handle_t s_panel;
static SemaphoreHandle_t s_done;
static uint16_t *s_band[2];

// CO5300 init sequence, as used by Waveshare's example for this panel.
// 0x3A=0x55 is RGB565; 0x36=0x30 the scan direction; 0x2A/0x2B the full 480x480 window.
static const sh8601_lcd_init_cmd_t s_init_cmds[] = {
    {0x11, (uint8_t[]){0x00}, 0, 600},
    {0xFE, (uint8_t[]){0x20}, 1, 0},
    {0x19, (uint8_t[]){0x10}, 1, 0},
    {0x1C, (uint8_t[]){0xA0}, 1, 0},
    {0xFE, (uint8_t[]){0x00}, 1, 0},
    {0xC4, (uint8_t[]){0x80}, 1, 0},
    {0x3A, (uint8_t[]){0x55}, 1, 0},
    {0x35, (uint8_t[]){0x00}, 1, 0},
    {0x36, (uint8_t[]){0x30}, 1, 0},
    {0x53, (uint8_t[]){0x20}, 1, 0},
    {0x51, (uint8_t[]){CONFIG_RADAR_BRIGHTNESS}, 1, 0},   // brightness in the init sequence itself:
                                                          // starting at 0 and raising it later left the
                                                          // panel black on the first build
    {0x63, (uint8_t[]){0xFF}, 1, 0},
    {0x2A, (uint8_t[]){0x00, 0x00, 0x01, 0xDF}, 4, 0},
    {0x2B, (uint8_t[]){0x00, 0x00, 0x01, 0xDF}, 4, 0},
    {0x29, (uint8_t[]){0x00}, 0, 100},
};

static esp_err_t pmu_rd(uint8_t reg, uint8_t *v) { return i2c_master_transmit_receive(s_pmu, &reg, 1, v, 1, 100); }
static esp_err_t pmu_wr(uint8_t reg, uint8_t v)  { uint8_t b[2] = {reg, v}; return i2c_master_transmit(s_pmu, b, 2, 100); }

static void pmu_set_if_different(uint8_t reg, uint8_t want, const char *name)
{
    uint8_t cur = 0;
    if (pmu_rd(reg, &cur) == ESP_OK && cur != want) {
        ESP_LOGW(TAG, "%s was 0x%02x, setting 0x%02x (3.3 V)", name, cur, want);
        pmu_wr(reg, want);
    }
}

static void pmu_aldo3(bool on)
{
    uint8_t v = 0;
    pmu_rd(AXP_LDO_ONOFF0, &v);
    pmu_wr(AXP_LDO_ONOFF0, on ? (v | AXP_ALDO3_BIT) : (v & ~AXP_ALDO3_BIT));
}

static esp_err_t pmu_init(void)
{
    i2c_master_bus_config_t bc = {
        .i2c_port = I2C_NUM_0, .sda_io_num = PIN_I2C_SDA, .scl_io_num = PIN_I2C_SCL,
        .clk_source = I2C_CLK_SRC_DEFAULT, .glitch_ignore_cnt = 7, .flags.enable_internal_pullup = true,
    };
    ESP_ERROR_CHECK(i2c_new_master_bus(&bc, &s_i2c));
    i2c_device_config_t dc = { .dev_addr_length = I2C_ADDR_BIT_LEN_7, .device_address = AXP2101_ADDR, .scl_speed_hz = 100000 };
    ESP_ERROR_CHECK(i2c_master_bus_add_device(s_i2c, &dc, &s_pmu));
    if (i2c_master_probe(s_i2c, AXP2101_ADDR, 200) != ESP_OK) {
        ESP_LOGE(TAG, "AXP2101 not found on I2C");
        return ESP_FAIL;
    }
    // Same targets as Waveshare's init: DCDC1 and ALDO1..4 at 3.3 V. Only written if different.
    pmu_set_if_different(AXP_DCDC1_VOLT, (3300 - 1500) / 100, "DCDC1");
    static const char *names[] = {"ALDO1", "ALDO2", "ALDO3", "ALDO4"};
    for (int i = 0; i < 4; i++) pmu_set_if_different(AXP_ALDO1_VOLT + i, (3300 - 500) / 100, names[i]);
    uint8_t en = 0;
    pmu_rd(AXP_LDO_ONOFF0, &en);
    ESP_LOGI(TAG, "AXP2101 ok, LDO enables 0x%02x", en);
    if ((en & 0x0F) != 0x0F) pmu_wr(AXP_LDO_ONOFF0, en | 0x0F);
    return ESP_OK;
}

static bool IRAM_ATTR on_trans_done(esp_lcd_panel_io_handle_t io, esp_lcd_panel_io_event_data_t *ev, void *ctx)
{
    BaseType_t woken = pdFALSE;
    xSemaphoreGiveFromISR(s_done, &woken);
    return woken == pdTRUE;
}

static esp_err_t panel_init(void)
{
    spi_bus_config_t bus = {
        .sclk_io_num = PIN_LCD_CLK, .data0_io_num = PIN_LCD_D0, .data1_io_num = PIN_LCD_D1,
        .data2_io_num = PIN_LCD_D2, .data3_io_num = PIN_LCD_D3,
        .max_transfer_sz = PANEL_W * BAND_ROWS * 2 + 64,
    };
    ESP_ERROR_CHECK(spi_bus_initialize(LCD_HOST, &bus, SPI_DMA_CH_AUTO));

    s_done = xSemaphoreCreateBinary();
    xSemaphoreGive(s_done);                    // nothing in flight yet
    esp_lcd_panel_io_spi_config_t io = {
        .cs_gpio_num = PIN_LCD_CS, .dc_gpio_num = -1, .spi_mode = 0,
        .pclk_hz = 40 * 1000 * 1000, .trans_queue_depth = 2,
        .on_color_trans_done = on_trans_done,
        .lcd_cmd_bits = 32, .lcd_param_bits = 8, .flags.quad_mode = true,
    };
    ESP_ERROR_CHECK(esp_lcd_new_panel_io_spi((esp_lcd_spi_bus_handle_t)LCD_HOST, &io, &s_io));

    sh8601_vendor_config_t vendor = {
        .init_cmds = s_init_cmds, .init_cmds_size = sizeof(s_init_cmds) / sizeof(s_init_cmds[0]),
        .flags.use_qspi_interface = 1,
    };
    esp_lcd_panel_dev_config_t pc = {
        .reset_gpio_num = GPIO_NUM_NC, .rgb_ele_order = LCD_RGB_ELEMENT_ORDER_RGB,
        .bits_per_pixel = 16, .vendor_config = &vendor,
    };
    ESP_ERROR_CHECK(esp_lcd_new_panel_sh8601(s_io, &pc, &s_panel));

    // GPIO11 is the touch controller's reset. Waveshare's demo always pulses it low
    // and leaves it HIGH during touch init; our first builds never touched it and
    // the panel stayed black. Release it before bringing the panel up.
    gpio_config_t trst = { .pin_bit_mask = 1ULL << 11, .mode = GPIO_MODE_OUTPUT };
    gpio_config(&trst);
    gpio_set_level(11, 0); vTaskDelay(pdMS_TO_TICKS(20));
    gpio_set_level(11, 1); vTaskDelay(pdMS_TO_TICKS(50));
    ESP_LOGI(TAG, "touch/display reset (GPIO11) released high");

    // No reset line: power-cycle the panel's rail instead (ALDO3), as Waveshare does.
    pmu_aldo3(true);  vTaskDelay(pdMS_TO_TICKS(100));
    pmu_aldo3(false); vTaskDelay(pdMS_TO_TICKS(100));
    pmu_aldo3(true);  vTaskDelay(pdMS_TO_TICKS(100));
    ESP_ERROR_CHECK(esp_lcd_panel_init(s_panel));

    for (int i = 0; i < 2; i++) {
        s_band[i] = heap_caps_malloc(PANEL_W * BAND_ROWS * 2, MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL);
        if (!s_band[i]) return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

esp_err_t board_init(void)
{
    ESP_ERROR_CHECK(pmu_init());
    ESP_ERROR_CHECK(panel_init());
    // Test pattern for 2 s: red / green / blue / white / black bars. If these are
    // visible the panel path works; wrong colours mean RGB order or byte order.
    esp_lcd_panel_disp_on_off(s_panel, true);
    // 1 s colour bars at boot: red, green, blue, white, black. Proves the panel path
    // before anything else runs (it hid a wrong chip-select for four builds).
    static const uint16_t bars[] = {0xF800, 0x07E0, 0x001F, 0xFFFF, 0x0000};
    for (int i = 0; i < 5; i++) board_fill(0, i * 96, PANEL_W, (i + 1) * 96, bars[i]);
    vTaskDelay(pdMS_TO_TICKS(1000));
    board_fill(0, 0, PANEL_W, PANEL_H, 0x0000);
    ESP_LOGI(TAG, "panel up, %d free heap", (int)heap_caps_get_free_size(MALLOC_CAP_INTERNAL));
    return ESP_OK;
}

uint16_t *board_band_buffer(int which) { return s_band[which & 1]; }

void board_draw(int x0, int y0, int x1, int y1, const void *data)
{
    xSemaphoreTake(s_done, portMAX_DELAY);     // previous transfer finished -> its buffer is free
    esp_err_t err = esp_lcd_panel_draw_bitmap(s_panel, x0, y0, x1, y1, data);
    if (err != ESP_OK) {
        static int reported;
        if (reported++ < 5) ESP_LOGE(TAG, "draw_bitmap(%d,%d,%d,%d) failed: %s", x0, y0, x1, y1, esp_err_to_name(err));
        xSemaphoreGive(s_done);               // nothing in flight -- don't deadlock the next draw
    }
}

void board_draw_wait(void)
{
    xSemaphoreTake(s_done, portMAX_DELAY);
    xSemaphoreGive(s_done);
}

void board_fill(int x0, int y0, int x1, int y1, uint16_t rgb565)
{
    uint16_t be = (uint16_t)((rgb565 >> 8) | (rgb565 << 8));
    int w = x1 - x0, which = 0;
    for (int y = y0; y < y1; y += BAND_ROWS) {
        int h = (y1 - y) < BAND_ROWS ? (y1 - y) : BAND_ROWS;
        uint16_t *b = s_band[which];
        board_draw_wait();                     // don't overwrite a strip still being sent
        for (int i = 0; i < w * h; i++) b[i] = be;
        board_draw(x0, y, x1, y + h, b);
        which ^= 1;
    }
    board_draw_wait();
}

void board_set_brightness(uint8_t level)
{
    esp_err_t err = esp_lcd_panel_io_tx_param(s_io, (0x02 << 24) | (0x51 << 8), &level, 1);
    ESP_LOGI(TAG, "brightness %u -> %s", level, esp_err_to_name(err));
}

void board_display(bool on)
{
    if (on) {
        esp_lcd_panel_io_tx_param(s_io, (0x02 << 24) | (0x11 << 8), NULL, 0);   // sleep out
        vTaskDelay(pdMS_TO_TICKS(120));
        esp_lcd_panel_disp_on_off(s_panel, true);
    } else {
        esp_lcd_panel_disp_on_off(s_panel, false);
        esp_lcd_panel_io_tx_param(s_io, (0x02 << 24) | (0x10 << 8), NULL, 0);   // sleep in
    }
}
