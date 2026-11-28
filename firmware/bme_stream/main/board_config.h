/*
 * Board wiring for the audio add-on. Everything the hardware needs in one
 * place, so re-wiring only means editing this header.
 */
#pragma once

/* ---- MAX98357A I2S mono amplifier ------------------------------------
 *   VIN  -> 5V (a 3 W amp on the USB rail can brown out: use a separate
 *           5 V supply with a common GND if the board resets at volume)
 *   GND  -> GND
 *   BCLK -> AUDIO_I2S_BCLK_GPIO
 *   LRC  -> AUDIO_I2S_LRC_GPIO
 *   DIN  -> AUDIO_I2S_DIN_GPIO
 *   SD   -> 3V3 for the left channel, or leave floating for (L+R)/2
 *   GAIN -> leave floating (9 dB)
 *
 * Any GPIO works: the ESP32-S3 routes I2S through the GPIO matrix.
 */
#define AUDIO_I2S_BCLK_GPIO 15
#define AUDIO_I2S_LRC_GPIO 16
#define AUDIO_I2S_DIN_GPIO 17

/* Initial clock; the player reconfigures it if the clip uses another rate. */
#define AUDIO_DEFAULT_SAMPLE_RATE 16000

/* Software volume 0.0-1.0. Keep headroom: the amplifier adds 9 dB. */
#define AUDIO_VOLUME 0.60f

/* ---- push button -----------------------------------------------------
 * Wire the button between BUTTON_GPIO and GND; the internal pull-up is used.
 */
#define BUTTON_GPIO 18
#define BUTTON_DEBOUNCE_MS 30
