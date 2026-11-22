idf_component_register(
    SRCS "main.c" "ble_bme.c" "sensor_adc.c"
    INCLUDE_DIRS "."
    REQUIRES bt nvs_flash esp_adc esp_timer
)
