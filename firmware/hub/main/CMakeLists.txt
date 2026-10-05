# One firmware, one board per target: the board file and the JPEG decoder (C6/ESP32 ROM
# vs P4 hardware) follow IDF_TARGET.
if(IDF_TARGET STREQUAL "esp32p4")
    set(board_srcs "boards/ws_p4_35.c" "apps/jpeg_draw_p4.c")
    set(board_reqs esp_driver_ledc esp_driver_jpeg esp_driver_uart)
elseif(IDF_TARGET STREQUAL "esp32")
    set(board_srcs "boards/deskradar_esp32.c" "apps/jpeg_draw.c")
    set(board_reqs esp_driver_ledc esp_driver_uart)
else()
    set(board_srcs "boards/ws_c6_216.c" "apps/jpeg_draw.c")
    set(board_reqs esp_driver_usb_serial_jtag)
endif()

idf_component_register(
    SRCS "main.c"
         "core/net.c" "core/store.c" "core/ota.c" "core/keys.c" "core/ui.c" "core/improv.c"
         "apps/aircraft.c" "apps/weather.c" "apps/rdl.c"
         ${board_srcs}
    INCLUDE_DIRS "." "core"
    REQUIRES driver esp_driver_i2c esp_driver_spi esp_driver_gpio esp_lcd esp_timer
             esp_wifi esp_netif esp_event nvs_flash esp_http_client json
             esp_https_ota app_update esp_partition esp_app_format esp_rom
             ${board_reqs}
)
