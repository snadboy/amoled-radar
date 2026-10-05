// Board support for a "DeskRadar" build (github.com/arvis91/deskradar): an ESP32-WROOM
// devkit (classic ESP32, 4 MB flash, no PSRAM, CH340 USB-serial) wired to a 1.28"
// round GC9A01 240x240 SPI LCD. No touch, no PMU; the only button is BOOT (GPIO0).
//
// Wiring (the project's pinout): SCL 18, SDA/MOSI 23, RES 4, DC 2, CS 15,
// BLK 21 -- or BLK tied to 3.3 V, in which case brightness does nothing.
#include "board.h"

#include "driver/gpio.h"
#include "driver/ledc.h"
#include "driver/spi_master.h"
#include "esp_heap_caps.h"
#include "esp_lcd_gc9a01.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_ops.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "sdkconfig.h"

static const char *TAG = "board";

#define W 240
#define H 240
#define BAND_ROWS 20

const board_profile_t BOARD = {
    .board = "deskradar-esp32-gc9a01", .w = W, .h = H, .corner_r = W / 2,   // round glass
    .panel = "lcd", .psram = false, .band_rows = BAND_ROWS, .one_button = true,
};

#define PIN_LCD_CLK 18
#define PIN_LCD_MOSI 23
#define PIN_LCD_RST 4
#define PIN_LCD_DC  2
#define PIN_LCD_CS  15
#define PIN_LCD_BL  21
#define PIN_BOOT    0
#define LCD_HOST    SPI3_HOST     // VSPI: 18/23 are its native pins

static esp_lcd_panel_io_handle_t s_io;
static esp_lcd_panel_handle_t s_panel;
static SemaphoreHandle_t s_done;
static uint16_t *s_band[2];
static uint8_t s_level = CONFIG_HUB_BRIGHTNESS;

static bool IRAM_ATTR on_trans_done(esp_lcd_panel_io_handle_t io, esp_lcd_panel_io_event_data_t *ev, void *ctx)
{
    BaseType_t woken = pdFALSE;
    xSemaphoreGiveFromISR(s_done, &woken);
    return woken == pdTRUE;
}

static void backlight_init(void)
{
    ledc_timer_config_t t = { .speed_mode = LEDC_LOW_SPEED_MODE, .duty_resolution = LEDC_TIMER_8_BIT,
                              .timer_num = LEDC_TIMER_0, .freq_hz = 5000, .clk_cfg = LEDC_AUTO_CLK };
    ledc_timer_config(&t);
    ledc_channel_config_t c = { .gpio_num = PIN_LCD_BL, .speed_mode = LEDC_LOW_SPEED_MODE,
                                .channel = LEDC_CHANNEL_0, .timer_sel = LEDC_TIMER_0, .duty = 0 };
    ledc_channel_config(&c);
}

static void backlight(uint8_t level)
{
    ledc_set_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_0, level);
    ledc_update_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_0);
}

static esp_err_t panel_init(void)
{
    spi_bus_config_t bus = {
        .sclk_io_num = PIN_LCD_CLK, .mosi_io_num = PIN_LCD_MOSI, .miso_io_num = -1,
        .quadwp_io_num = -1, .quadhd_io_num = -1,
        .max_transfer_sz = W * BAND_ROWS * 2 + 64,
    };
    ESP_ERROR_CHECK(spi_bus_initialize(LCD_HOST, &bus, SPI_DMA_CH_AUTO));

    s_done = xSemaphoreCreateBinary();
    xSemaphoreGive(s_done);                    // nothing in flight yet
    esp_lcd_panel_io_spi_config_t io = {
        .cs_gpio_num = PIN_LCD_CS, .dc_gpio_num = PIN_LCD_DC, .spi_mode = 0,
        .pclk_hz = 40 * 1000 * 1000, .trans_queue_depth = 2,
        .on_color_trans_done = on_trans_done,
        .lcd_cmd_bits = 8, .lcd_param_bits = 8,
    };
    ESP_ERROR_CHECK(esp_lcd_new_panel_io_spi((esp_lcd_spi_bus_handle_t)LCD_HOST, &io, &s_io));

    esp_lcd_panel_dev_config_t pc = {
        .reset_gpio_num = PIN_LCD_RST, .rgb_ele_order = LCD_RGB_ELEMENT_ORDER_BGR, .bits_per_pixel = 16,
    };
    ESP_ERROR_CHECK(esp_lcd_new_panel_gc9a01(s_io, &pc, &s_panel));
    ESP_ERROR_CHECK(esp_lcd_panel_reset(s_panel));
    ESP_ERROR_CHECK(esp_lcd_panel_init(s_panel));
    // As Espressif's GC9A01 example: these modules need inverted colour and a mirrored X.
    esp_lcd_panel_invert_color(s_panel, true);
    esp_lcd_panel_mirror(s_panel, true, false);

    for (int i = 0; i < 2; i++) {
        s_band[i] = heap_caps_malloc(W * BAND_ROWS * 2, MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL);
        if (!s_band[i]) return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

esp_err_t board_init(void)
{
    backlight_init();
    ESP_ERROR_CHECK(panel_init());
    gpio_config_t b = { .pin_bit_mask = 1ULL << PIN_BOOT, .mode = GPIO_MODE_INPUT, .pull_up_en = GPIO_PULLUP_ENABLE };
    gpio_config(&b);
    esp_lcd_panel_disp_on_off(s_panel, true);
    // 1 s colour bars at boot: red, green, blue, white, black (proves colour order/inversion).
    static const uint16_t bars[] = {0xF800, 0x07E0, 0x001F, 0xFFFF, 0x0000};
    for (int i = 0; i < 5; i++) board_fill(0, i * H / 5, W, (i + 1) * H / 5, bars[i]);
    backlight(s_level);
    vTaskDelay(pdMS_TO_TICKS(1000));
    board_fill(0, 0, W, H, 0x0000);
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

void board_set_brightness(uint8_t level) { s_level = level; backlight(level); }

void board_display(bool on)
{
    if (on) {
        esp_lcd_panel_io_tx_param(s_io, 0x11, NULL, 0);    // sleep out
        vTaskDelay(pdMS_TO_TICKS(120));
        esp_lcd_panel_disp_on_off(s_panel, true);
        backlight(s_level);
    } else {
        backlight(0);
        esp_lcd_panel_disp_on_off(s_panel, false);
        esp_lcd_panel_io_tx_param(s_io, 0x10, NULL, 0);    // sleep in
    }
}

bool board_touch(int *x, int *y) { return false; }

// The one button is reported as KEY (the app's button); keys.c turns a long press
// into BOOT (next app) on one-button boards.
bool board_button_down(int btn) { return btn == BTN_KEY && gpio_get_level(PIN_BOOT) == 0; }

bool board_pwr_pressed(void) { return false; }
