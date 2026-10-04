idf_component_register(
    SRCS "main.c"
         "core/net.c" "core/store.c" "core/ota.c" "core/keys.c" "core/ui.c"
         "boards/ws_c6_216.c"
         "apps/aircraft.c" "apps/weather.c" "apps/rdl.c" "apps/jpeg_draw.c"
    INCLUDE_DIRS "." "core"
    REQUIRES driver esp_driver_i2c esp_driver_spi esp_driver_gpio esp_lcd esp_timer
             esp_wifi esp_netif esp_event nvs_flash esp_http_client json
             esp_https_ota app_update esp_partition esp_app_format esp_rom
)
