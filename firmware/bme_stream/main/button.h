/*
 * Push button input.
 *
 * The ISR only signals a semaphore; debouncing and the callback run in a task
 * so callers can do real work without worrying about ISR context. One physical
 * press produces exactly one callback because the task waits for release.
 */
#pragma once

#include "esp_err.h"

typedef void (*button_press_cb_t)(void *arg);

/* Configures the pin as input with a pull-up and starts the scanner.
 * The button must connect the pin to GND. */
esp_err_t button_init(int gpio_num, button_press_cb_t on_press, void *arg);
