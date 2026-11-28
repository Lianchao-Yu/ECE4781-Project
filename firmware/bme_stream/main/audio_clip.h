/*
 * The embedded audio clip, independent of playback.
 *
 * notify.wav is baked into the firmware by CMake (EMBED_FILES). This module
 * parses its WAV header once and exposes plain PCM samples; swapping the sound
 * means replacing the file, no code changes.
 */
#pragma once

#include <stddef.h>
#include <stdint.h>

typedef struct {
    const int16_t *pcm;   /* mono, signed 16-bit */
    size_t frames;        /* number of samples */
    uint32_t sample_rate; /* Hz */
} audio_clip_t;

/* Returns NULL if the embedded file is missing or not PCM16 mono. */
const audio_clip_t *audio_clip_default(void);
