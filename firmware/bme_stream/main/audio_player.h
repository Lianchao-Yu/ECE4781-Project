/*
 * I2S output for the MAX98357A.
 *
 * The chip is a mono amplifier, but its DAC takes a normal stereo I2S stream
 * and either averages L+R (SD floating) or plays one slot (SD strapped). This
 * driver always sends the sample in both slots, so the output level is the same
 * whichever way SD is wired.
 */
#pragma once

#include <stdbool.h>

#include "audio_clip.h"
#include "esp_err.h"

/* Configures I2S and starts the playback task. */
esp_err_t audio_player_init(void);

/* Plays a clip from the beginning. A clip already playing is restarted. */
esp_err_t audio_player_play(const audio_clip_t *clip);

/* Stops the current clip; the amplifier outputs silence afterwards. */
void audio_player_stop(void);

/* True while samples are being streamed. */
bool audio_player_busy(void);
