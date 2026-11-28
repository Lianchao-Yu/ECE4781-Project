#include "audio_control.h"

#include "audio_clip.h"
#include "audio_player.h"
#include "board_config.h"
#include "button.h"
#include "esp_log.h"
#include "esp_timer.h"

static const char *TAG = "audio_control";
static audio_press_cb_t s_on_press;

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

    /* Report the press after starting playback so the sound is not delayed by
     * a slow subscriber. */
    if (s_on_press != NULL) {
        s_on_press((uint32_t)(esp_timer_get_time() / 1000));
    }
}

void audio_control_init(audio_press_cb_t on_press)
{
    s_on_press = on_press;

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
