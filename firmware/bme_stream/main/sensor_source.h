/*
 * Acquisition source abstraction.
 *
 * The upper layer calls sensor_read() at the EMG rate (1 kHz) and this layer
 * decimates PPG/EDA internally, so channel rates stay locked without any
 * extra timers.
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

typedef struct {
    int16_t emg;      /* valid on every call */
    int16_t ppg;      /* valid only when ppg_new is true */
    int16_t eda;      /* valid only when eda_new is true */
    bool ppg_new;
    bool eda_new;
    bool saturated;
} sensor_sample_t;

/* Configure the ADC inputs (pins come from menuconfig). */
void sensor_init(void);

/* True when inputs are expected to be floating (bench mode, no sensors). */
bool sensor_bench_mode(void);

/* Call at BME_EMG_RATE_HZ; PPG/EDA decimation happens here. */
void sensor_read(sensor_sample_t *out);
