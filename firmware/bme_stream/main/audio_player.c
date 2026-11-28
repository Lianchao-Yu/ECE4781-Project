/*
 * I2S playback task.
 *
 * Callers never block: play/stop only post a request and wake the task, which
 * streams the clip in small chunks. A new request aborts the current clip
 * between chunks, so pressing the button twice restarts the sound immediately.
 */
#include "audio_player.h"

#include <string.h>

#include "board_config.h"
#include "driver/i2s_std.h"
#include "esp_check.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

#define CHUNK_FRAMES 512

static const char *TAG = "audio_player";

static i2s_chan_handle_t s_tx;
static SemaphoreHandle_t s_lock;   /* guards s_pending */
static SemaphoreHandle_t s_wake;   /* new request for the task */
static const audio_clip_t *s_pending;
static volatile bool s_abort;
static volatile bool s_busy;
static uint32_t s_rate;

static esp_err_t set_sample_rate(uint32_t rate)
{
    if (rate == s_rate) {
        return ESP_OK;
    }
    i2s_std_clk_config_t clk = I2S_STD_CLK_DEFAULT_CONFIG(rate);
    i2s_channel_disable(s_tx);
    esp_err_t err = i2s_channel_reconfig_std_clock(s_tx, &clk);
    ESP_RETURN_ON_ERROR(i2s_channel_enable(s_tx), TAG, "re-enable I2S");
    if (err == ESP_OK) {
        s_rate = rate;
    }
    return err;
}

static void stream_clip(const audio_clip_t *clip)
{
    if (set_sample_rate(clip->sample_rate) != ESP_OK) {
        ESP_LOGE(TAG, "cannot set %lu Hz", (unsigned long)clip->sample_rate);
        return;
    }

    /* Drop whatever was queued in the DMA buffers so the clip starts clean. */
    i2s_channel_disable(s_tx);
    ESP_ERROR_CHECK(i2s_channel_enable(s_tx));

    static int16_t buf[CHUNK_FRAMES * 2]; /* interleaved L/R */
    const float volume = AUDIO_VOLUME;

    for (size_t off = 0; off < clip->frames && !s_abort; off += CHUNK_FRAMES) {
        size_t n = clip->frames - off;
        if (n > CHUNK_FRAMES) {
            n = CHUNK_FRAMES;
        }

        for (size_t i = 0; i < n; i++) {
            int32_t v = (int32_t)((float)clip->pcm[off + i] * volume);
            if (v > 32767) {
                v = 32767;
            } else if (v < -32768) {
                v = -32768;
            }
            buf[2 * i] = (int16_t)v;      /* left  */
            buf[2 * i + 1] = (int16_t)v;  /* right */
        }

        size_t written = 0;
        esp_err_t err = i2s_channel_write(s_tx, buf, n * 2 * sizeof(int16_t),
                                          &written, portMAX_DELAY);
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "i2s write failed: %s", esp_err_to_name(err));
            break;
        }
    }
}

static void audio_task(void *arg)
{
    (void)arg;
    while (1) {
        xSemaphoreTake(s_wake, portMAX_DELAY);

        /* Drain requests until one clip finishes without being interrupted. */
        for (;;) {
            xSemaphoreTake(s_lock, portMAX_DELAY);
            const audio_clip_t *clip = s_pending;
            s_pending = NULL;
            xSemaphoreGive(s_lock);

            if (clip == NULL) {
                s_busy = false;
                break;
            }

            s_abort = false;
            s_busy = true;
            stream_clip(clip);

            if (!s_abort) {
                s_busy = false;
                ESP_LOGI(TAG, "clip finished");
                break;
            }
        }
    }
}

esp_err_t audio_player_init(void)
{
    s_lock = xSemaphoreCreateMutex();
    s_wake = xSemaphoreCreateBinary();
    if (s_lock == NULL || s_wake == NULL) {
        return ESP_ERR_NO_MEM;
    }

    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    chan_cfg.auto_clear = true; /* silence instead of repeating old data */
    ESP_RETURN_ON_ERROR(i2s_new_channel(&chan_cfg, &s_tx, NULL), TAG, "new channel");

    i2s_std_config_t std_cfg = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(AUDIO_DEFAULT_SAMPLE_RATE),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT,
                                                        I2S_SLOT_MODE_STEREO),
        .gpio_cfg = {
            .mclk = I2S_GPIO_UNUSED, /* MAX98357A derives its clock from BCLK */
            .bclk = AUDIO_I2S_BCLK_GPIO,
            .ws = AUDIO_I2S_LRC_GPIO,
            .dout = AUDIO_I2S_DIN_GPIO,
            .din = I2S_GPIO_UNUSED,
            .invert_flags = {false, false, false},
        },
    };
    ESP_RETURN_ON_ERROR(i2s_channel_init_std_mode(s_tx, &std_cfg), TAG, "std mode");
    ESP_RETURN_ON_ERROR(i2s_channel_enable(s_tx), TAG, "enable");
    s_rate = AUDIO_DEFAULT_SAMPLE_RATE;

    if (xTaskCreate(audio_task, "audio", 4096, NULL, 5, NULL) != pdPASS) {
        return ESP_ERR_NO_MEM;
    }

    ESP_LOGI(TAG, "I2S ready: BCLK=%d LRC=%d DIN=%d",
             AUDIO_I2S_BCLK_GPIO, AUDIO_I2S_LRC_GPIO, AUDIO_I2S_DIN_GPIO);
    return ESP_OK;
}

esp_err_t audio_player_play(const audio_clip_t *clip)
{
    if (clip == NULL || clip->frames == 0) {
        return ESP_ERR_INVALID_ARG;
    }

    xSemaphoreTake(s_lock, portMAX_DELAY);
    s_pending = clip;
    xSemaphoreGive(s_lock);

    ESP_LOGI(TAG, "play %u samples @ %lu Hz", (unsigned)clip->frames,
             (unsigned long)clip->sample_rate);

    s_abort = true; /* interrupt the clip that is streaming, if any */
    xSemaphoreGive(s_wake);
    return ESP_OK;
}

void audio_player_stop(void)
{
    xSemaphoreTake(s_lock, portMAX_DELAY);
    s_pending = NULL;
    xSemaphoreGive(s_lock);

    s_abort = true;
    xSemaphoreGive(s_wake);
}

bool audio_player_busy(void)
{
    return s_busy;
}
