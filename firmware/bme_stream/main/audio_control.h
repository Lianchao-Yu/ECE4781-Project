/*
 * Glue between the button and the player.
 *
 * This is the only place that decides what a press means; the button driver
 * knows nothing about audio and the player knows nothing about buttons.
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

/* Called after a button press has been handled. The argument is the device
 * uptime in milliseconds at the moment of the press. */
typedef void (*audio_press_cb_t)(uint32_t uptime_ms);

/* Initialises the player and the button. `on_press` may be NULL; otherwise it
 * is invoked from the button task, so keep it short (post a message, notify). */
void audio_control_init(audio_press_cb_t on_press);

/* Plays the bundled clip programmatically (same action as a press). */
void audio_control_trigger(void);

/* True while the clip is playing. */
bool audio_control_playing(void);
