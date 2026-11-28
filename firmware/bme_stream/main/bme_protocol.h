/*
 * BME three-channel BLE streaming protocol.
 *
 * Frame layout (little endian, 31 bytes):
 *   offset 0  : magic0      0xA5
 *   offset 1  : magic1      0x5A
 *   offset 2  : version     0x01
 *   offset 3  : flags       bit0 = bench mode (floating inputs, no sensors)
 *                           bit1 = ADC saturated
 *   offset 4  : seq         uint16, incremented per frame, used for loss stats
 *   offset 6  : n_emg       number of EMG samples in this frame (normally 10)
 *   offset 7  : emg[10]     int16 x 10, sampled at 1 kHz
 *   offset 27 : ppg         int16, sampled at 100 Hz
 *   offset 29 : eda         int16, sampled at 100 Hz
 */
#pragma once

#include <assert.h>
#include <stdint.h>

#define BME_MAGIC0 0xA5
#define BME_MAGIC1 0x5A
#define BME_PROTO_VERSION 1

#define BME_EMG_PER_FRAME 10
#define BME_EMG_RATE_HZ 1000
#define BME_PPG_RATE_HZ 100
#define BME_EDA_RATE_HZ 100

#define BME_FLAG_BENCH (1u << 0)
#define BME_FLAG_SATURATED (1u << 1)

typedef struct __attribute__((packed)) {
    uint8_t magic0;
    uint8_t magic1;
    uint8_t version;
    uint8_t flags;
    uint16_t seq;
    uint8_t n_emg;
    int16_t emg[BME_EMG_PER_FRAME];
    int16_t ppg;
    int16_t eda;
} bme_frame_t;

/* Host-side parsers depend on this exact size. */
_Static_assert(sizeof(bme_frame_t) == 31, "bme_frame_t must stay 31 bytes");

/* ---------- GATT UUIDs ----------
 * service       5f9b1c00-3e4a-4d21-9a77-0b1c2d3e4f50
 * data (notify) 5f9b1c01-3e4a-4d21-9a77-0b1c2d3e4f50
 * cmd  (write)  5f9b1c02-3e4a-4d21-9a77-0b1c2d3e4f50
 * device name   BME-S3
 */
#define BME_SERVICE_UUID_STR "5f9b1c00-3e4a-4d21-9a77-0b1c2d3e4f50"
#define BME_TX_UUID_STR "5f9b1c01-3e4a-4d21-9a77-0b1c2d3e4f50"
#define BME_RX_UUID_STR "5f9b1c02-3e4a-4d21-9a77-0b1c2d3e4f50"
#define BME_EVENT_UUID_STR "5f9b1c03-3e4a-4d21-9a77-0b1c2d3e4f50"
#define BME_DEVICE_NAME "BME-S3"

/* ---------- Event packet (notify on the event characteristic) ----------
 *   [0] version (1)
 *   [1] event type: 1 = button pressed
 *   [2..5] device uptime in milliseconds, little endian
 */
#define BME_EVENT_VERSION 1
#define BME_EVENT_BUTTON 1
#define BME_EVENT_PACKET_LEN 6

/* ---------- Command bytes written to the RX characteristic ---------- */
#define BME_CMD_STOP 0x00
#define BME_CMD_START 0x01
#define BME_CMD_WHOAMI 0x02
