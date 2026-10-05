// Board support for the Waveshare ESP32-P4-WIFI6-Touch-LCD-3.5, run in landscape.
//
// From Waveshare's BSP (waveshare/esp32_p4_wifi6_touch_lcd_3_5 2.0.2), not from web pages:
//   * ST7796 320x480 IPS over plain SPI (SPI2, mode 3, 80 MHz): MOSI 20, CLK 21, CS 23,
//     DC 26, RST 27; the driver's built-in init + colour inversion (Waveshare's own init
//     table is compiled out); BGR element order
//   * backlight = PWM on GPIO28 (LEDC) -- an LCD, so "screen off" is mostly backlight off
//   * FT6336 touch at 0x38 on I2C SCL 8 / SDA 7, RST 29 (INT 50 unused: polled)
//   * AXP2101 on the same bus (the BSP never touches it; probed for the PWR button only)
//   * buttons: BOOT (GPIO35, the P4's strapping pin) and PWR; there is NO KEY button
//   * WiFi comes from the on-board ESP32-C6 over SDIO (esp_hosted + esp_wifi_remote),
//     so net.c's esp_wifi calls work unchanged
//
// UNVERIFIED until the board is on the desk: the landscape rotation (panel and touch
// flags below) and the BOOT pin. Colour bars at boot show the panel orientation.
#include "board.h"

#include "driver/gpio.h"
#include "driver/i2c_master.h"
#include "driver/ledc.h"
#include "driver/spi_master.h"
#include "esp_heap_caps.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_ops.h"
#include "esp_lcd_st7796.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "sdkconfig.h"

static const char *TAG = "board";

#define W 480                      // landscape: the native panel is 320 x 480
#define H 320
#define BAND_ROWS 40

const board_profile_t BOARD = {
    .board = "ws-p4-lcd-35", .w = W, .h = H, .corner_r = 0,
    .panel = "lcd", .psram = true, .band_rows = BAND_ROWS,
};

// Landscape mapping -- to be confirmed on the hardware (colour bars run top to bottom
// red/green/blue/white; touch the top-left corner and check the log).
#define PANEL_SWAP_XY  1
#define PANEL_MIRROR_X 1
#define PANEL_MIRROR_Y 1
#define TOUCH_SWAP_XY  1           // raw FT6336 coordinates are portrait (x 0..319, y 0..479)
#define TOUCH_MIRROR_X 0
#define TOUCH_MIRROR_Y 1

#define PIN_I2C_SCL 8
#define PIN_I2C_SDA 7
#define PIN_LCD_MOSI 20
#define PIN_LCD_CLK  21
#define PIN_LCD_CS   23
#define PIN_LCD_DC   26
#define PIN_LCD_RST  27
#define PIN_LCD_BL   28
#define PIN_TOUCH_RST 29
#define PIN_BOOT     35
#define LCD_HOST     SPI2_HOST

#define AXP2101_ADDR   0x34
#define AXP_INTEN1     0x40
#define AXP_INTSTS1    0x48
#define AXP_PKEY_SHORT 0x08        // bit 3 of INTEN2 / INTSTS2
#define FT6336_ADDR    0x38

static i2c_master_bus_handle_t s_i2c;
static i2c_master_dev_handle_t s_pmu, s_touch;
static esp_lcd_panel_io_handle_t s_io;
static esp_lcd_panel_handle_t s_panel;
static SemaphoreHandle_t s_done;
static uint16_t *s_band[2];
static bool s_display_on = true, s_have_pmu;
static uint8_t s_level = CONFIG_HUB_BRIGHTNESS;

static bool IRAM_ATTR on_trans_done(esp_lcd_panel_io_handle_t io, esp_lcd_panel_io_event_data_t *ev, void *ctx)
{
    BaseType_t woken = pdFALSE;
    xSemaphoreGiveFromISR(s_done, &woken);
    return woken == pdTRUE;
}

static void backlight(uint8_t level)
{
    ledc_set_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_0, (uint32_t)level * 1023 / 255);
    ledc_update_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_0);
}

static esp_err_t i2c_init(void)
{
    i2c_master_bus_config_t bc = {
        .i2c_port = I2C_NUM_0, .sda_io_num = PIN_I2C_SDA, .scl_io_num = PIN_I2C_SCL,
        .clk_source = I2C_CLK_SRC_DEFAULT, .glitch_ignore_cnt = 7, .flags.enable_internal_pullup = true,
    };
    ESP_ERROR_CHECK(i2c_new_master_bus(&bc, &s_i2c));
    i2c_device_config_t tc = { .dev_addr_length = I2C_ADDR_BIT_LEN_7, .device_address = FT6336_ADDR, .scl_speed_hz = 400000 };
    ESP_ERROR_CHECK(i2c_master_bus_add_device(s_i2c, &tc, &s_touch));
    gpio_config_t trst = { .pin_bit_mask = 1ULL << PIN_TOUCH_RST, .mode = GPIO_MODE_OUTPUT };
    gpio_config(&trst);
    gpio_set_level(PIN_TOUCH_RST, 0); vTaskDelay(pdMS_TO_TICKS(20));
    gpio_set_level(PIN_TOUCH_RST, 1); vTaskDelay(pdMS_TO_TICKS(200));
    if (i2c_master_probe(s_i2c, FT6336_ADDR, 200) != ESP_OK) ESP_LOGW(TAG, "FT6336 touch not answering");

    // PWR: the AXP2101 latches short presses, as on the C6 board. Optional here.
    s_have_pmu = i2c_master_probe(s_i2c, AXP2101_ADDR, 200) == ESP_OK;
    if (s_have_pmu) {
        i2c_device_config_t pc = { .dev_addr_length = I2C_ADDR_BIT_LEN_7, .device_address = AXP2101_ADDR, .scl_speed_hz = 100000 };
        ESP_ERROR_CHECK(i2c_master_bus_add_device(s_i2c, &pc, &s_pmu));
        for (int i = 0; i < 3; i++) { uint8_t b[2] = { AXP_INTEN1 + i, i == 1 ? AXP_PKEY_SHORT : 0 }; i2c_master_transmit(s_pmu, b, 2, 100); }
        for (int i = 0; i < 3; i++) { uint8_t b[2] = { AXP_INTSTS1 + i, 0xFF }; i2c_master_transmit(s_pmu, b, 2, 100); }
    }
    ESP_LOGI(TAG, "AXP2101 %s", s_have_pmu ? "found: PWR button enabled" : "not found: no PWR button");
    gpio_config_t b = { .pin_bit_mask = 1ULL << PIN_BOOT, .mode = GPIO_MODE_INPUT, .pull_up_en = GPIO_PULLUP_ENABLE };
    gpio_config(&b);
    return ESP_OK;
}

static esp_err_t panel_init(void)
{
    spi_bus_config_t bus = {
        .sclk_io_num = PIN_LCD_CLK, .mosi_io_num = PIN_LCD_MOSI, .miso_io_num = GPIO_NUM_NC,
        .quadwp_io_num = GPIO_NUM_NC, .quadhd_io_num = GPIO_NUM_NC,
        .max_transfer_sz = W * BAND_ROWS * 2 + 64,
    };
    ESP_ERROR_CHECK(spi_bus_initialize(LCD_HOST, &bus, SPI_DMA_CH_AUTO));
    s_done = xSemaphoreCreateBinary();
    xSemaphoreGive(s_done);
    esp_lcd_panel_io_spi_config_t io = {
        .dc_gpio_num = PIN_LCD_DC, .cs_gpio_num = PIN_LCD_CS, .pclk_hz = 80 * 1000 * 1000,
        .lcd_cmd_bits = 8, .lcd_param_bits = 8, .spi_mode = 3, .trans_queue_depth = 2,
        .on_color_trans_done = on_trans_done,
    };
    ESP_ERROR_CHECK(esp_lcd_new_panel_io_spi((esp_lcd_spi_bus_handle_t)LCD_HOST, &io, &s_io));
    esp_lcd_panel_dev_config_t pc = {
        .reset_gpio_num = PIN_LCD_RST, .rgb_ele_order = LCD_RGB_ELEMENT_ORDER_BGR, .bits_per_pixel = 16,
    };
    ESP_ERROR_CHECK(esp_lcd_new_panel_st7796(s_io, &pc, &s_panel));
    ESP_ERROR_CHECK(esp_lcd_panel_reset(s_panel));
    ESP_ERROR_CHECK(esp_lcd_panel_init(s_panel));
    esp_lcd_panel_invert_color(s_panel, true);
    esp_lcd_panel_swap_xy(s_panel, PANEL_SWAP_XY);
    esp_lcd_panel_mirror(s_panel, PANEL_MIRROR_X, PANEL_MIRROR_Y);

    ledc_timer_config_t lt = { .speed_mode = LEDC_LOW_SPEED_MODE, .duty_resolution = LEDC_TIMER_10_BIT,
                               .timer_num = LEDC_TIMER_0, .freq_hz = 5000, .clk_cfg = LEDC_AUTO_CLK };
    ESP_ERROR_CHECK(ledc_timer_config(&lt));
    ledc_channel_config_t lc = { .gpio_num = PIN_LCD_BL, .speed_mode = LEDC_LOW_SPEED_MODE, .channel = LEDC_CHANNEL_0,
                                 .timer_sel = LEDC_TIMER_0, .duty = 0 };
    ESP_ERROR_CHECK(ledc_channel_config(&lc));

    for (int i = 0; i < 2; i++) {
        s_band[i] = heap_caps_malloc(W * BAND_ROWS * 2, MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL);
        if (!s_band[i]) return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

esp_err_t board_init(void)
{
    ESP_ERROR_CHECK(i2c_init());
    ESP_ERROR_CHECK(panel_init());
    esp_lcd_panel_disp_on_off(s_panel, true);
    // 1 s colour bars, top to bottom red / green / blue / white / black: proves the
    // panel path AND shows the orientation (and RGB vs BGR) at a glance.
    static const uint16_t bars[] = {0xF800, 0x07E0, 0x001F, 0xFFFF, 0x0000};
    for (int i = 0; i < 5; i++) board_fill(0, i * H / 5, W, (i + 1) * H / 5, bars[i]);
    backlight(s_level);
    vTaskDelay(pdMS_TO_TICKS(1000));
    board_fill(0, 0, W, H, 0x0000);
    ESP_LOGI(TAG, "panel up, %d KB internal + %d KB PSRAM free",
             (int)heap_caps_get_free_size(MALLOC_CAP_INTERNAL) / 1024, (int)heap_caps_get_free_size(MALLOC_CAP_SPIRAM) / 1024);
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
        xSemaphoreGive(s_done);
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
        board_draw_wait();
        for (int i = 0; i < w * h; i++) b[i] = be;
        board_draw(x0, y, x1, y + h, b);
        which ^= 1;
    }
    board_draw_wait();
}

void board_set_brightness(uint8_t level)
{
    s_level = level;
    if (s_display_on) backlight(level);
}

void board_display(bool on)
{
    if (on) {
        esp_lcd_panel_disp_sleep(s_panel, false);
        esp_lcd_panel_disp_on_off(s_panel, true);
        backlight(s_level);
    } else {
        backlight(0);
        esp_lcd_panel_disp_on_off(s_panel, false);
        esp_lcd_panel_disp_sleep(s_panel, true);
    }
    s_display_on = on;
}

bool board_touch(int *x, int *y)
{
    if (!s_display_on) return false;
    uint8_t reg = 0x02, d[5];                 // TD_STATUS, P1_XH, P1_XL, P1_YH, P1_YL
    if (i2c_master_transmit_receive(s_touch, &reg, 1, d, sizeof(d), 50) != ESP_OK) return false;
    if ((d[0] & 0x0F) == 0) return false;
    int tx = ((d[1] & 0x0F) << 8) | d[2], ty = ((d[3] & 0x0F) << 8) | d[4];   // portrait 320 x 480
    if (TOUCH_SWAP_XY) { int t = tx; tx = ty; ty = t; }
    if (TOUCH_MIRROR_X) tx = W - 1 - tx;
    if (TOUCH_MIRROR_Y) ty = H - 1 - ty;
    *x = tx < 0 ? 0 : tx >= W ? W - 1 : tx;
    *y = ty < 0 ? 0 : ty >= H ? H - 1 : ty;
    return true;
}

bool board_button_down(int btn)
{
    if (btn == BTN_BOOT) return gpio_get_level(PIN_BOOT) == 0;
    return false;                              // no KEY on this board: its actions are on touch
}

bool board_pwr_pressed(void)
{
    if (!s_have_pmu) return false;
    uint8_t reg = AXP_INTSTS1 + 1, st = 0;
    if (i2c_master_transmit_receive(s_pmu, &reg, 1, &st, 1, 100) != ESP_OK || !(st & AXP_PKEY_SHORT)) return false;
    uint8_t b[2] = { AXP_INTSTS1 + 1, AXP_PKEY_SHORT };
    i2c_master_transmit(s_pmu, b, 2, 100);
    return true;
}
