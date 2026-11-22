/*
 * ESP32-S3 built-in ADC acquisition.
 *
 * Three ADC1 inputs are sampled at 1 kHz and PPG/EDA are decimated 10:1.
 *
 * Default pins (GPIO4 / GPIO5 / GPIO6) are left unconnected on the bench, so
 * the readings are the random values a floating ADC pin produces. That is
 * enough to validate the whole chain without any sensor or signal generator:
 * every frame carries non-zero, changing data and the host can plot it.
 *
 * When real front-ends are ready, wire their outputs to the same pins (or
 * change them in menuconfig). No host-side code needs to change.
 *
 * Note: the internal ADC is noisy and its input range is 0-3.1 V with 12 dB
 * attenuation. For real EMG use an external front-end (e.g. ADS1299) and
 * replace sensor_read() only.
 */
#include "sensor_source.h"

#include "bme_protocol.h"
#include "esp_adc/adc_oneshot.h"
#include "esp_log.h"

static const char *TAG = "sensor_adc";

static adc_oneshot_unit_handle_t s_unit;
static adc_channel_t s_ch_emg;
static adc_channel_t s_ch_ppg;
static adc_channel_t s_ch_eda;
static uint32_t s_tick;
static bool s_bench;

/*
 * ESP32-S3 maps ADC1 GPIO1..GPIO10 straight to channels 0..9.
 * Keeping the mapping local avoids depending on a helper whose name has
 * changed between IDF releases.
 */
static esp_err_t gpio_to_adc1_channel(int gpio, adc_channel_t *channel)
{
    if (gpio < 1 || gpio > 10) {
        ESP_LOGE(TAG, "GPIO%d is not usable as ADC1 input (allowed: GPIO1-GPIO10)", gpio);
        return ESP_ERR_INVALID_ARG;
    }
    *channel = (adc_channel_t)(gpio - 1);
    return ESP_OK;
}

/* 12 bit / 12 dB attenuation: 0-3.1 V, mid-scale is 2048. */
static inline int16_t raw_to_sample(int raw)
{
    int32_t v = ((int32_t)raw - 2048) * 8; /* map to +/-16384, keep headroom */
    if (v > 32767) {
        v = 32767;
    } else if (v < -32768) {
        v = -32768;
    }
    return (int16_t)v;
}

static void read_channel(adc_channel_t ch, int *out)
{
    esp_err_t err = adc_oneshot_read(s_unit, ch, out);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "ADC read on channel %d failed: %s", (int)ch, esp_err_to_name(err));
        *out = 2048;
    }
}

void sensor_init(void)
{
    s_tick = 0;
    s_bench = CONFIG_BME_BENCH_MODE;

    ESP_ERROR_CHECK(gpio_to_adc1_channel(CONFIG_BME_GPIO_EMG, &s_ch_emg));
    ESP_ERROR_CHECK(gpio_to_adc1_channel(CONFIG_BME_GPIO_PPG, &s_ch_ppg));
    ESP_ERROR_CHECK(gpio_to_adc1_channel(CONFIG_BME_GPIO_EDA, &s_ch_eda));

    adc_oneshot_unit_init_cfg_t unit_cfg = {
        .unit_id = ADC_UNIT_1,
        .ulp_mode = ADC_ULP_MODE_DISABLE,
    };
    ESP_ERROR_CHECK(adc_oneshot_new_unit(&unit_cfg, &s_unit));

    adc_oneshot_chan_cfg_t chan_cfg = {
        .atten = ADC_ATTEN_DB_12,
        .bitwidth = ADC_BITWIDTH_12,
    };
    ESP_ERROR_CHECK(adc_oneshot_config_channel(s_unit, s_ch_emg, &chan_cfg));
    ESP_ERROR_CHECK(adc_oneshot_config_channel(s_unit, s_ch_ppg, &chan_cfg));
    ESP_ERROR_CHECK(adc_oneshot_config_channel(s_unit, s_ch_eda, &chan_cfg));

    ESP_LOGI(TAG, "ADC1 inputs: EMG=GPIO%d (CH%d) PPG=GPIO%d (CH%d) EDA=GPIO%d (CH%d)%s",
             CONFIG_BME_GPIO_EMG, (int)s_ch_emg, CONFIG_BME_GPIO_PPG, (int)s_ch_ppg,
             CONFIG_BME_GPIO_EDA, (int)s_ch_eda,
             CONFIG_BME_BENCH_MODE ? " [bench mode, inputs floating]" : "");
}

bool sensor_bench_mode(void)
{
    return CONFIG_BME_BENCH_MODE;
}

void sensor_read(sensor_sample_t *out)
{
    int raw;

    read_channel(s_ch_emg, &raw);
    out->emg = raw_to_sample(raw);
    out->saturated = !s_bench && (raw <= 2 || raw >= 4093);

    out->ppg_new = (s_tick % (BME_EMG_RATE_HZ / BME_PPG_RATE_HZ)) == 0;
    if (out->ppg_new) {
        read_channel(s_ch_ppg, &raw);
        out->ppg = raw_to_sample(raw);
        out->saturated = out->saturated || (!s_bench && (raw <= 2 || raw >= 4093));
    }

    out->eda_new = (s_tick % (BME_EMG_RATE_HZ / BME_EDA_RATE_HZ)) == 0;
    if (out->eda_new) {
        read_channel(s_ch_eda, &raw);
        out->eda = raw_to_sample(raw);
        out->saturated = out->saturated || (!s_bench && (raw <= 2 || raw >= 4093));
    }

    s_tick++;
}
