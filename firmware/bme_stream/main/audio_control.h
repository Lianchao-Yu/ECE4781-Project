/*
 * Glue between the button and the player.
 *
 * This is the only place that decides what a press means; the button driver
 * knows nothing about audio and the player knows nothing about buttons.
 */
#pragma once

#include <stdbool.h>

/* Initialises the player and the button, then arms the press handler. */
void audio_control_init(void);

/* Plays the bundled clip programmatically (same action as a press). */
void audio_control_trigger(void);

/* True while the clip is playing. */
bool audio_control_playing(void);
