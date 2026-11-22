#pragma once

#include <stdbool.h>
#include <stddef.h>

/* Initialize NimBLE, register the GATT service and start advertising. */
void ble_bme_init(void);

/* True once the host stack has synced and advertising is possible. */
bool ble_bme_ready(void);

/* True while a central is connected and subscribed to the data characteristic. */
bool ble_bme_subscribed(void);

/* Streaming switch, controlled by the START/STOP commands (default on). */
bool ble_bme_stream_enabled(void);

/* Negotiated ATT MTU, zero while disconnected. */
unsigned ble_bme_mtu(void);

/* Push one frame; returns true when the notification was queued. */
bool ble_bme_notify(const void *data, size_t len);
