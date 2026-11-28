# Project-BME

ESP32-S3 firmware that samples three biomedical channels (EMG / PPG / EDA) and
streams them over BLE, plus two PC viewers that plot the data live.

```
firmware/bme_stream/   ESP-IDF firmware (NimBLE GATT, ~100 frames/s)
host/python/           desktop viewer (PyQt6, custom painter), link self-test
host/web/              browser viewer (Web Bluetooth), no installation
docs/PROTOCOL.md       BLE protocol and frame layout
OPERATION_GUIDELINE.md day-to-day operating checklist
```

## Hardware and pin usage

All three inputs are on **ADC1**. The firmware never uses ADC2, which is
unavailable while Wi-Fi is active.

| Channel | ESP32-S3 pin | ADC1 channel | Notes |
|---|---|---|---|
| EMG | GPIO4 | CH3 | 1 kHz sampling |
| PPG | GPIO5 | CH4 | 100 Hz sampling |
| EDA | GPIO6 | CH5 | 100 Hz sampling |
| GND | any GND | – | common ground with the analog front-end |

Why these pins:

- They are all ADC1 inputs (only GPIO1–GPIO10 can be used, GPIO11–GPIO20 belong
  to ADC2).
- They avoid the strapping pins (GPIO0, GPIO3, GPIO45, GPIO46), the native USB
  pins (GPIO19/20), the UART0 console pins (GPIO43/44) and the flash/PSRAM pins
  (GPIO26–GPIO37).

Electrical limits:

- Input range is 0–3.1 V with 12 dB attenuation. Never exceed 3.3 V.
- The internal ADC is 12-bit and fairly noisy; for real EMG use an external
  front-end (e.g. ADS1299) and only replace `sensor_read()` in
  `main/sensor_adc.c`.
- Readings are converted to `(raw - 2048) * 8`, i.e. ±16384 spans roughly
  ±1.6 V around mid-scale.

Bench mode (default): the pins are left **floating**, so every reading is the
random value a floating ADC input produces. That is enough to prove the whole
chain — BLE link, frame rate, host plotting — without any sensor. Disable
`BME_BENCH_MODE` in menuconfig once the electrodes are wired, so the host stops
labelling the data as `bench`.

Note on the floating readings: on this board the unconnected pins sit close to
0 V, so the traces look like a low noise floor with regular sampling spikes
(ADC sample-and-hold artefacts). That is expected for a floating input and does
not indicate a wiring fault. Saturation reporting is suppressed in bench mode
because there is no real signal to clip.

To change the pins:

```powershell
idf.py -B F:\Espressif\builds\bme_stream menuconfig
# BME acquisition -> EMG/PPG/EDA input pin
```

## Development environment

| Item | Location / version |
|---|---|
| ESP-IDF | `F:\Espressif\esp-idf`, v5.5.6 |
| Toolchain | `F:\Espressif\tools` (xtensa-esp32s3-gcc 14.2.0, ninja, esptool 4.14) |
| Activate script | `F:\Espressif\idf.ps1` |
| Build directory | `F:\Espressif\builds\bme_stream` (kept off OneDrive) |
| Python | `F:\py12` (3.12.5) |

Open a new PowerShell and build:

```powershell
. F:\Espressif\idf.ps1
cd firmware\bme_stream
idf.py -B F:\Espressif\builds\bme_stream set-target esp32s3
idf.py -B F:\Espressif\builds\bme_stream build
idf.py -B F:\Espressif\builds\bme_stream -p COM3 flash monitor
```

> The activation script clears `PYTHONPATH`. The user-level entry
> `F:\py12\Lib\site-packages` used to leak global packages into the ESP-IDF
> virtual environment and broke the dependency check, so it was removed.

## Audio: MAX98357A + button

A press on the button plays `notify.wav` through a MAX98357A I2S amplifier.

| MAX98357A | ESP32-S3 | Notes |
|---|---|---|
| VIN | 5V | a 3 W amp can brown out the USB rail; a separate 5 V supply with common GND is safer |
| GND | GND | |
| BCLK | GPIO15 | bit clock |
| LRC | GPIO16 | word select |
| DIN | GPIO17 | data |
| SD | 3V3 or floating | 3V3 = left channel, floating = (L+R)/2 |
| GAIN | floating | 9 dB |
| + / − | speaker | 4 Ω, 3 W |

Button: `GPIO18` to `GND`, the internal pull-up is enabled. One press plays
the clip from the start; pressing again restarts it. The button is debounced in
software (30 ms) and one press produces exactly one event, holding it does not
repeat.

Each press is also reported to the host over a dedicated event characteristic
(`...1c03`), separately from the sample stream. The desktop viewer draws an
amber dashed line at that instant, scrolling with the waveform; hovering the
line shows `Button pressed · t = 12.34 s`. Markers are kept for the 60 s of
buffered history and cleared by `Clear`.

The audio code is split so each part can be changed alone:

| File | Responsibility |
|---|---|
| `main/board_config.h` | pins, volume, debounce time — re-wiring only touches this |
| `main/audio_player.c/.h` | I2S channel, chunked playback, abort/restart |
| `main/audio_clip.c/.h` | parses the embedded WAV into PCM samples |
| `main/button.c/.h` | GPIO interrupt, debounce, press callback |
| `main/audio_control.c/.h` | decides that a press means "play the clip" |
| `main/notify.wav` | the sound itself, baked into the image by `EMBED_FILES` |

To change the sound, replace `main/notify.wav` with any **16 kHz, 16-bit,
mono PCM** WAV and rebuild. No code changes are needed; the parser validates
the header and logs an error if the format does not match.

For a wiring check without touching the button, enable
`BME_AUDIO_TEST_ON_BOOT` in `menuconfig → BME acquisition`: the clip plays once
at boot and the serial log shows `play ... / clip finished`.

```powershell
. F:\Espressif\idf.ps1
cd firmware\bme_stream
idf.py -B F:\Espressif\builds\bme_stream build flash
```

`main.c` only calls `audio_control_init()`; everything else lives in the
modules above.

## PC applications

Command-line link check (prints frame rate, loss, per-channel ranges):

```powershell
& F:\Espressif\venvs\bme-host\Scripts\python.exe host\python\ble_selftest.py
```

Desktop viewer with live plots, history scrubbing and event markers:

```powershell
host\run_host.ps1
# or connect automatically on startup:
host\run_host.ps1 --auto-connect
```

Browser viewer (Chrome / Edge, no installation):

```powershell
host\run_web.ps1
```

The desktop viewer creates its virtual environment at
`F:\Espressif\venvs\bme-host` on first run.

## Standalone power (no USB tether)

Data travels over BLE, USB only supplies power, so the board can run from a
charger or power bank:

1. Connect the viewer first (button, or `--auto-connect`).
2. Unplug USB from the PC and plug it into a standalone supply. The board
   reboots once and the BLE link drops.
3. The viewer rescans and reconnects by itself within about two seconds.

After a reboot the frame counter restarts at zero; the host detects this and
resets its sequence baseline instead of reporting thousands of lost frames.
Flashing still requires the USB connection.

## Measured on hardware

- 100.5–101 frames/s, 31 bytes per frame, about 3.1 kB/s
- Negotiated ATT MTU 247, 0 % frame loss in a 5 s capture
- End-to-end latency from sampling to host typically below 30 ms
- Reconnect after a power cycle: about 2 s

## Display controls

The desktop viewer never rescales on its own, so the trace stays still while
data flows:

| Control | Meaning |
|---|---|
| Window | visible time span: 1, 2, 5, 10 or 30 s |
| Detail | display decimation: Full, 1/2 … 1/32 (1/16 by default). Scales the 1 kHz EMG stream; PPG/EDA are capped at 50 Hz, above their useful bandwidth |
| Zoom | requested Y span, 1x = ADC full scale (3.2 V), up to 20x. Each pane clamps to the finest span that still shows its own signal, so a high zoom never pushes a trace out of view |
| Center | re-centres the panes on the current data |
| Stop sampling | tells the device to stop streaming (BLE link stays up); press again to resume |
| Slider | right end = live; drag left to freeze the view and review the last 60 s |
| Latest | jumps back to the live edge |

The X axis is absolute time since the first sample: it starts at 0 and the tick
labels advance as the stream runs. When the slider is moved off the right end
the view freezes at that moment (the label shows `[PAUSED]`) while acquisition
and buffering continue; moving it back to the right resumes live drawing.

Axes carry units: time in seconds, amplitude in millivolts. The conversion from
raw counts is `mV = counts * 3.1 / 4095 / 8`, so 1 count ≈ 0.095 mV.

## Display performance

Windows delivers BLE notifications in bursts: measured gaps were p50 0.45 ms,
p95 58 ms and up to 118 ms, even though the average is exactly 100 frames/s.
Rendering the newest sample directly therefore produced stop-and-jump motion.
The viewer now keeps a playback clock that advances at the measured sample rate
and trails the newest sample by roughly 0.3–0.5 s, so the jitter buffer absorbs
the bursts and the trace scrolls at a constant speed. The cost is that latency.

The panes are painted directly with `QPainter`. When a window holds more samples
than pixels, each pixel column is reduced to its min/max and drawn as a single
vertical segment, so every spike survives while only ~1 line per pixel is
rasterised; below that the trace is a plain polyline. The frame timer is
single-shot and re-armed from inside the frame, so a slow frame can never queue
up repaints and freeze the UI.



## Board

| Item | Value |
|---|---|
| Chip | ESP32-S3 (QFN56, rev v0.2), 8 MB PSRAM, 16 MB flash |
| MAC | `C0:4E:30:36:A7:50` |
| Serial port | COM3 (USB-Serial/JTAG) |
| Original firmware backup | `F:\Espressif\backups\esp32s3_16MB_20261120.bin` |

Restore the original firmware:

```powershell
. F:\Espressif\idf.ps1
# 2 Mbaud is much faster than 921600 over USB-Serial/JTAG
esptool.py -p COM3 -b 2000000 write_flash 0 F:\Espressif\backups\esp32s3_16MB_20261120.bin
```
