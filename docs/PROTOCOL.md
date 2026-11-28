# BME three-channel BLE protocol

Device: ESP32-S3, BLE peripheral (NimBLE)
Device name: `BME-S3`

## GATT layout

| Purpose | UUID | Properties |
|---|---|---|
| Service | `5f9b1c00-3e4a-4d21-9a77-0b1c2d3e4f50` | primary |
| Data stream | `5f9b1c01-3e4a-4d21-9a77-0b1c2d3e4f50` | notify |
| Commands | `5f9b1c02-3e4a-4d21-9a77-0b1c2d3e4f50` | write, write-no-response |
| Events | `5f9b1c03-3e4a-4d21-9a77-0b1c2d3e4f50` | notify |

## Sample rates

| Channel | Rate | Notes |
|---|---|---|
| EMG | 1000 Hz | 10 samples per frame |
| PPG | 100 Hz | 1 sample per frame |
| EDA | 100 Hz | 1 sample per frame, host applies a low-pass filter |

A frame is 31 bytes and is sent about 100 times per second, roughly 3.1 kB/s.

## Frame layout (little endian, fixed 31 bytes)

| Offset | Size | Type | Field | Description |
|---|---|---|---|---|
| 0 | 1 | uint8 | magic0 | always `0xA5` |
| 1 | 1 | uint8 | magic1 | always `0x5A` |
| 2 | 1 | uint8 | version | currently `0x01` |
| 3 | 1 | uint8 | flags | bit0 = bench mode (floating inputs), bit1 = ADC saturated |
| 4 | 2 | uint16 | seq | incremented per frame, used for loss statistics |
| 6 | 1 | uint8 | n_emg | EMG samples in this frame, normally 10 |
| 7 | 20 | int16[10] | emg | 10 EMG samples |
| 27 | 2 | int16 | ppg | latest PPG sample |
| 29 | 2 | int16 | eda | latest EDA sample |

Values come from the 12-bit ADC as `(raw - 2048) * 8`, so ±16384 corresponds
to roughly ±1.6 V around mid-scale (12 dB attenuation, 0–3.1 V range).

### Python parser

```python
import struct
FRAME = struct.Struct("<BBBBHB10h2h")
buf = bytearray()

def feed(data):
    buf.extend(data)
    while True:
        i = buf.find(b"\xA5\x5A")
        if i < 0 or len(buf) - i < FRAME.size:
            return
        del buf[:i]
        magic0, magic1, ver, flags, seq, n, *rest = FRAME.unpack(bytes(buf[:FRAME.size]))
        emg, ppg, eda = rest[:10], rest[10], rest[11]
        del buf[:FRAME.size]
```

## Commands
Write one byte to the command characteristic (`...1c02`):

| Value | Meaning |
|---|---|
| `0x00` | stop streaming |
| `0x01` | start streaming (default after boot) |
| `0x02` | WHOAMI (logs a line on the device) |

## Event packets

The event characteristic (`...1c03`) carries short notifications that are not
part of the sample stream, so a button press never disturbs a frame. Currently
only one event exists: the user pressed the button.

| Offset | Size | Type | Field | Description |
|---|---|---|---|---|
| 0 | 1 | uint8 | version | `0x01` |
| 1 | 1 | uint8 | type | `0x01` = button pressed |
| 2 | 4 | uint32 | uptime_ms | device uptime at the press, little endian |

The packet is 6 bytes. The device only sends it when a central is subscribed to
the event characteristic; older firmware simply does not expose it.

Hosts place the marker on their own sample timeline: when the notification
arrives, record the current absolute sample index. BLE latency adds only a few
milliseconds of offset, which is below one pixel at normal zoom.

## MTU requirement

One frame is 31 bytes, so the ATT payload must hold at least 31 bytes, i.e.
**MTU >= 34**. The firmware requests MTU 247 and Windows, macOS, Android and
iOS all negotiate at least that. If the MTU is too small the firmware drops the
frame and logs a warning instead of sending truncated data.

## Host-side notes

- After a disconnect the device advertises again; reconnecting is enough.
- Sequence numbers wrap at 16 bits: `gap = (seq - last_seq) & 0xFFFF`. A gap
  larger than a few thousand frames means the device rebooted, not that data
  was lost.
- EDA is best displayed with a 0.2–0.5 s moving average; PPG can be shown raw.
- CSV recording expands each frame into 1 ms rows for EMG and holds the latest
  PPG/EDA value for the whole frame.
