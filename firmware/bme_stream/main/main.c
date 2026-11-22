/*
 * Three-channel (EMG/PPG/EDA) BLE acquisition firmware.
 *
 * Timing: one 1 kHz task assembles 10 EMG samples into a frame and pushes it
 * out roughly 100 times per second (31 bytes each). See bme_protocol.h for the
 * frame layout and host/ for the PC-side code.
 */
#include <string.h>

#include "bme_protocol.h"
#include "ble_bme.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs_flash.h"
#include "sensor_source.h"

static const char *TAG = "bme_stream";

#define STATS_INTERVAL_US (5 * 1000 * 1000)

/*
 * Poll the acquisition source on a 1 kHz beat and send one frame per 10 EMG
 * samples. Absolute-time pacing keeps the average rate exact; if the task
 * falls far behind it re-aligns instead of trying to catch up.
 */
static void sample_task(void *arg)
{
    static bme_frame_t frame;
    memset(&frame, 0, sizeof(frame));
    frame.magic0 = BME_MAGIC0;
    frame.magic1 = BME_MAGIC1;
    frame.version = BME_PROTO_VERSION;
    frame.flags = sensor_bench_mode() ? BME_FLAG_BENCH : 0;
    frame.n_emg = BME_EMG_PER_FRAME;

    const int64_t period_us = 1000000 / BME_EMG_RATE_HZ;
    int64_t next_us = esp_timer_get_time();
    int64_t stats_us = next_us;
    uint8_t idx = 0;
    int16_t last_ppg = 0;
    int16_t last_eda = 0;
    uint32_t sent = 0;
    uint32_t skipped = 0;
    uint32_t total = 0;

    while (1) {
        sensor_sample_t s;
        sensor_read(&s);

        frame.emg[idx++] = s.emg;
        if (s.ppg_new) {
            last_ppg = s.ppg;
        }
        if (s.eda_new) {
            last_eda = s.eda;
        }
        if (s.saturated) {
            frame.flags |= BME_FLAG_SATURATED;
        }

        if (idx == BME_EMG_PER_FRAME) {
            frame.ppg = last_ppg;
            frame.eda = last_eda;

            if (ble_bme_stream_enabled() && ble_bme_subscribed()) {
                if (ble_bme_notify(&frame, sizeof(frame))) {
                    sent++;
                } else {
                    skipped++;
                }
            }
            frame.seq++;
            frame.flags &= ~BME_FLAG_SATURATED;
            idx = 0;
            total++;
        }

        next_us += period_us;
        int64_t now = esp_timer_get_time();
        if (now < next_us) {
            int64_t delta = next_us - now;
            if (delta > 2000) {
                vTaskDelay(pdMS_TO_TICKS(delta / 1000 - 1));
            } else {
                taskYIELD();
            }
        } else if (now - next_us > 50000) {
            next_us = now;
        }

        if (now - stats_us >= STATS_INTERVAL_US) {
            stats_us = now;
            ESP_LOGI(TAG, "frames: %lu sent %lu dropped %lu | MTU %u | subscribed %s",
                     (unsigned long)total, (unsigned long)sent, (unsigned long)skipped,
                     ble_bme_mtu(), ble_bme_subscribed() ? "yes" : "no");
        }
    }
}

void app_main(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);

    sensor_init();
    ESP_LOGI(TAG, "acquisition source: internal ADC (%s)",
             sensor_bench_mode() ? "bench mode, inputs floating" : "sensors connected");

    ble_bme_init();

    /* Sampling runs on core 1; the BLE host task stays on core 0. */
    BaseType_t ok = xTaskCreatePinnedToCore(sample_task, "sampler", 4096, NULL, 6, NULL, 1);
    ESP_ERROR_CHECK(ok == pdPASS ? ESP_OK : ESP_FAIL);
}
