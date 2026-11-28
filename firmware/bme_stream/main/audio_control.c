#include "audio_control.h"

#include "audio_clip.h"
#include "audio_player.h"
#include "board_config.h"
#include "button.h"
#include "esp_log.h"

static const char *TAG = "audio_control";

static void on_button_press(void *arg)
{
    (void)arg;
    const audio_clip_t *clip = audio_clip_default();
    if (clip == NULL) {
        ESP_LOGW(TAG, "no clip available");
        return;
    }
    /* Restarts from the start if it is already playing. */
    audio_player_play(clip);
}

void audio_control_init(void)
{
    ESP_ERROR_CHECK(audio_player_init());
    ESP_ERROR_CHECK(button_init(BUTTON_GPIO, on_button_press, NULL));

    /* Parse the clip now: the log line confirms the embedded WAV is usable
     * before anyone presses the button. */
    const audio_clip_t *clip = audio_clip_default();
    if (clip == NULL) {
        ESP_LOGE(TAG, "no usable audio clip, the button will do nothing");
        return;
    }
    ESP_LOGI(TAG, "press GPIO%d to play (%.1f s clip)",
             BUTTON_GPIO, (double)clip->frames / (double)clip->sample_rate);

#if CONFIG_BME_AUDIO_TEST_ON_BOOT
    ESP_LOGI(TAG, "boot audio test enabled");
    audio_player_play(clip);
#endif
}

void audio_control_trigger(void)
{
    on_button_press(NULL);
}

bool audio_control_playing(void)
{
    return audio_player_busy();
}
