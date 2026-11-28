# Operation Guideline

Quick operating logic for the BME acquisition board (ESP32-S3) and its PC
viewer. Hardware details and the protocol live in `README.md` and
`docs/PROTOCOL.md`; this file is the day-to-day checklist.

## 1. What the system does

- Samples three channels: **EMG 1 kHz**, **PPG 100 Hz**, **EDA 100 Hz**.
- Streams them over **BLE** as `BME-S3`, roughly 100 frames per second.
- A **button** plays a short notification sound through the MAX98357A amp.
- Two viewers: a Python desktop app (full features) and a browser page.

## 2. Start-up order

1. Power the board (USB from the PC, a charger, or a power bank).
   Sampling and BLE advertising start automatically; nothing to press.
2. If the amp is powered from a separate 5 V supply, switch that on too.
   Keep the grounds connected.
3. On the PC, start the viewer:

```powershell
host\run_host.ps1              # desktop viewer
host\run_host.ps1 --auto-connect   # same, connects immediately
host\run_web.ps1               # browser version (Chrome / Edge)
```

4. Click **Connect** (or rely on `--auto-connect`). The status line turns into
   `Live ...` once frames arrive.

The viewer reconnects by itself after a power cycle, so unplugging the board
and moving it to a power bank needs no manual action.

## 3. Device controls

| Control | Action |
|---|---|
| Button (GPIO18 to GND) | plays the notification clip and marks the moment in the viewer; pressing again restarts it |

There is no power switch and no other on-device control. The board keeps
sampling and advertising until power is removed.

## 4. Viewer controls

| Control | Function |
|---|---|
| Connect / Disconnect | open or close the BLE link |
| Stop sampling | asks the device to stop streaming; the link stays up. Press again (`Resume sampling`) to continue |
| Clear | empties the buffers and the 60 s history |
| Event markers | every button press draws an amber dashed line at that time; hover it to read `Button pressed · t = x.xx s` |
| Window | visible time span: 1, 2, 5, 10 or 30 s |
| Detail | display decimation: Full, 1/2 … 1/32 (1/16 default). Affects the 1 kHz EMG stream; PPG/EDA are capped at 50 Hz |
| Zoom | requested magnification. Each pane is clamped to fit its own signal, so a trace never leaves the view |
| Center | re-centres the panes on the current data |
| Slider | drag left to freeze the view and look back (up to 60 s). The label shows `Delay: x.x s [PAUSED]` |
| Latest | jumps back to the live edge (`Live`) |

Hovering over a pane shows a vertical cursor with the exact time and value.
The time axis is absolute: it starts at 0 when the first sample arrives and the
labels advance as the stream runs.

## 5. Typical tasks

**Watch live signals**
1. Connect.
2. Pick `Window` (5 s is a good default) and `Detail`.
3. Use `Zoom` to inspect a channel; `Center` if a trace drifts out of frame.

**Review the last minute**
1. Drag the slider left; the view freezes at that moment.
2. Drag right, or press `Latest`, to resume live.

**Restart the board**
Unplug and re-power. The viewer reconnects in about two seconds and the
sequence counter restarts without being reported as packet loss.

## 6. Quick troubleshooting

| Symptom | Check |
|---|---|
| `Waiting for BME-S3 to advertise` | board powered? firmware flashed? another app holding the BLE link? |
| No waveform, `Live` shown | press `Stop sampling` twice (stop/resume), or re-flash if the device is silent |
| Waveform clipped at the edges | lower `Zoom`, or press `Center` |
| Trace looks dense / noisy | raise `Detail` (e.g. 1/16 or 1/32); `Full` is raw 1 kHz |
| Scrolling feels delayed | that is the ~0.4 s jitter buffer that absorbs bursty BLE delivery |
| No sound | check amp VIN/GND, `SD` pin, speaker wiring; serial log should show `play ...` then `clip finished` |
| Board resets at high volume | power the amp from a separate 5 V supply with a common ground |

## 7. Where to change settings

| What | Where |
|---|---|
| Pins, volume, debounce | `firmware/bme_stream/main/board_config.h` |
| Notification sound | replace `main/notify.wav` (16 kHz, 16-bit, mono PCM) |
| Boot-time audio self-test | `menuconfig → BME acquisition → BME_AUDIO_TEST_ON_BOOT` |
| ADC inputs / bench mode | `menuconfig → BME acquisition` |
| Build and flash | see `README.md` (`. F:\Espressif\idf.ps1`, then `idf.py -B F:\Espressif\builds\bme_stream -p COM3 flash`) |
