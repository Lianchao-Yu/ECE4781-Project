idf_component_register(
    SRCS "main.c" "ble_bme.c" "sensor_adc.c"
         "audio_player.c" "audio_clip.c" "button.c" "audio_control.c"
    INCLUDE_DIRS "."
    REQUIRES bt nvs_flash esp_adc esp_timer driver
    # Baked into the firmware; audio_clip.c parses it at runtime.
    EMBED_FILES "notify.wav"
)
