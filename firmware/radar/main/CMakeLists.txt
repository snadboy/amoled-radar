idf_component_register(
    SRCS "main.c" "board.c" "jpeg_draw.c" "net.c"
    INCLUDE_DIRS "."
    REQUIRES driver esp_driver_i2c esp_driver_spi esp_driver_gpio esp_lcd esp_timer
             esp_wifi esp_netif esp_event nvs_flash esp_http_client json
)
