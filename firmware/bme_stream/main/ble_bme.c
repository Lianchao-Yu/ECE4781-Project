/*
 * NimBLE GATT service: one notify characteristic streams the three channels,
 * one write characteristic accepts host commands.
 */
#include "ble_bme.h"

#include <string.h>

#include "bme_protocol.h"
#include "esp_log.h"
#include "host/ble_hs.h"
#include "host/util/util.h"
#include "nimble/nimble_port.h"
#include "nimble/nimble_port_freertos.h"
#include "services/gap/ble_svc_gap.h"
#include "services/gatt/ble_svc_gatt.h"

static const char *TAG = "ble_bme";

/*
 * 128-bit UUIDs are stored least-significant byte first in NimBLE, so the
 * initializer bytes are the UUID string read backwards.
 */
static const ble_uuid128_t s_svc_uuid =
    BLE_UUID128_INIT(0x50, 0x4f, 0x3e, 0x2d, 0x1c, 0x0b, 0x77, 0x9a, 0x21, 0x4d, 0x4a, 0x3e, 0x00,
                     0x1c, 0x9b, 0x5f);
static const ble_uuid128_t s_tx_uuid =
    BLE_UUID128_INIT(0x50, 0x4f, 0x3e, 0x2d, 0x1c, 0x0b, 0x77, 0x9a, 0x21, 0x4d, 0x4a, 0x3e, 0x01,
                     0x1c, 0x9b, 0x5f);
static const ble_uuid128_t s_rx_uuid =
    BLE_UUID128_INIT(0x50, 0x4f, 0x3e, 0x2d, 0x1c, 0x0b, 0x77, 0x9a, 0x21, 0x4d, 0x4a, 0x3e, 0x02,
                     0x1c, 0x9b, 0x5f);

static uint16_t s_tx_val_handle;
static volatile uint16_t s_conn_handle = BLE_HS_CONN_HANDLE_NONE;
static volatile bool s_subscribed;
static volatile bool s_stream_enabled = true;
static volatile bool s_synced;
static volatile unsigned s_mtu;
static uint8_t s_own_addr_type;
static bool s_mtu_warned;

static int gatt_access(uint16_t conn_handle, uint16_t attr_handle,
                       struct ble_gatt_access_ctxt *ctxt, void *arg);

static const struct ble_gatt_svc_def s_services[] = {
    {
        .type = BLE_GATT_SVC_TYPE_PRIMARY,
        .uuid = &s_svc_uuid.u,
        .characteristics =
            (struct ble_gatt_chr_def[]){
                {
                    .uuid = &s_tx_uuid.u,
                    .access_cb = gatt_access,
                    .flags = BLE_GATT_CHR_F_NOTIFY,
                    .val_handle = &s_tx_val_handle,
                },
                {
                    .uuid = &s_rx_uuid.u,
                    .access_cb = gatt_access,
                    .flags = BLE_GATT_CHR_F_WRITE | BLE_GATT_CHR_F_WRITE_NO_RSP,
                },
                {0},
            },
    },
    {0},
};

static int gap_event(struct ble_gap_event *event, void *arg);

static void advertise(void)
{
    struct ble_hs_adv_fields fields;
    struct ble_gap_adv_params params;
    const char *name = ble_svc_gap_device_name();

    memset(&fields, 0, sizeof(fields));
    fields.flags = BLE_HS_ADV_F_DISC_GEN | BLE_HS_ADV_F_BREDR_UNSUP;
    fields.uuids128 = (ble_uuid128_t *)&s_svc_uuid;
    fields.num_uuids128 = 1;
    fields.uuids128_is_complete = 1;
    int rc = ble_gap_adv_set_fields(&fields);
    if (rc != 0) {
        ESP_LOGE(TAG, "failed to set adv fields, rc=%d", rc);
        return;
    }

    /* Name goes into the scan response so the main packet stays under 31 bytes. */
    memset(&fields, 0, sizeof(fields));
    fields.name = (uint8_t *)name;
    fields.name_len = strlen(name);
    fields.name_is_complete = 1;
    ble_gap_adv_rsp_set_fields(&fields);

    memset(&params, 0, sizeof(params));
    params.conn_mode = BLE_GAP_CONN_MODE_UND;
    params.disc_mode = BLE_GAP_DISC_MODE_GEN;
    params.itvl_min = BLE_GAP_ADV_ITVL_MS(100);
    params.itvl_max = BLE_GAP_ADV_ITVL_MS(150);

    rc = ble_gap_adv_start(s_own_addr_type, NULL, BLE_HS_FOREVER, &params, gap_event, NULL);
    if (rc != 0) {
        ESP_LOGE(TAG, "failed to start advertising, rc=%d", rc);
    } else {
        ESP_LOGI(TAG, "advertising as %s", name);
    }
}

static int gap_event(struct ble_gap_event *event, void *arg)
{
    switch (event->type) {
    case BLE_GAP_EVENT_CONNECT:
        if (event->connect.status == 0) {
            s_conn_handle = event->connect.conn_handle;
            ESP_LOGI(TAG, "connected, handle=%d", s_conn_handle);
        } else {
            ESP_LOGW(TAG, "connect failed, status=%d", event->connect.status);
            advertise();
        }
        break;

    case BLE_GAP_EVENT_DISCONNECT:
        ESP_LOGI(TAG, "disconnected, reason=%d", event->disconnect.reason);
        s_conn_handle = BLE_HS_CONN_HANDLE_NONE;
        s_subscribed = false;
        s_mtu = 0;
        advertise();
        break;

    case BLE_GAP_EVENT_SUBSCRIBE:
        if (event->subscribe.attr_handle == s_tx_val_handle) {
            s_subscribed = event->subscribe.cur_notify;
            ESP_LOGI(TAG, "data subscription %s", s_subscribed ? "on" : "off");
        }
        break;

    case BLE_GAP_EVENT_MTU:
        s_mtu = event->mtu.value;
        ESP_LOGI(TAG, "MTU=%u", s_mtu);
        break;

    case BLE_GAP_EVENT_ADV_COMPLETE:
        advertise();
        break;

    default:
        break;
    }
    return 0;
}

static void on_sync(void)
{
    int rc = ble_hs_util_ensure_addr(0);
    if (rc != 0) {
        ESP_LOGE(TAG, "ensure_addr failed, rc=%d", rc);
        return;
    }
    rc = ble_hs_id_infer_auto(0, &s_own_addr_type);
    if (rc != 0) {
        ESP_LOGE(TAG, "infer_auto failed, rc=%d", rc);
        return;
    }
    s_synced = true;
    advertise();
}

static void on_reset(int reason)
{
    ESP_LOGW(TAG, "host reset, reason=%d", reason);
    s_synced = false;
}

static void host_task(void *param)
{
    nimble_port_run();
    nimble_port_freertos_deinit();
}

static int gatt_access(uint16_t conn_handle, uint16_t attr_handle,
                       struct ble_gatt_access_ctxt *ctxt, void *arg)
{
    switch (ctxt->op) {
    case BLE_GATT_ACCESS_OP_WRITE_CHR: {
        uint8_t cmd = 0;
        if (OS_MBUF_PKTLEN(ctxt->om) < 1) {
            return BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN;
        }
        os_mbuf_copydata(ctxt->om, 0, 1, &cmd);
        switch (cmd) {
        case BME_CMD_START:
            s_stream_enabled = true;
            ESP_LOGI(TAG, "cmd: start streaming");
            break;
        case BME_CMD_STOP:
            s_stream_enabled = false;
            ESP_LOGI(TAG, "cmd: stop streaming");
            break;
        case BME_CMD_WHOAMI:
            ESP_LOGI(TAG, "cmd: whoami");
            break;
        default:
            ESP_LOGW(TAG, "unknown command 0x%02x", cmd);
            break;
        }
        return 0;
    }
    case BLE_GATT_ACCESS_OP_READ_CHR: {
        uint8_t status[2] = {(uint8_t)(s_stream_enabled ? 1 : 0), (uint8_t)s_mtu};
        return os_mbuf_append(ctxt->om, status, sizeof(status)) == 0 ? 0
                                                                    : BLE_ATT_ERR_INSUFFICIENT_RES;
    }
    default:
        return BLE_ATT_ERR_UNLIKELY;
    }
}

void ble_bme_init(void)
{
    esp_err_t err = nimble_port_init();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "nimble_port_init failed: %s", esp_err_to_name(err));
        return;
    }

    ble_hs_cfg.sync_cb = on_sync;
    ble_hs_cfg.reset_cb = on_reset;

    ble_svc_gap_init();
    ble_svc_gatt_init();
    ble_svc_gap_device_name_set(BME_DEVICE_NAME);

    int rc = ble_gatts_count_cfg(s_services);
    if (rc == 0) {
        rc = ble_gatts_add_svcs(s_services);
    }
    if (rc != 0) {
        ESP_LOGE(TAG, "failed to register GATT services, rc=%d", rc);
        return;
    }

    nimble_port_freertos_init(host_task);
}

bool ble_bme_ready(void)
{
    return s_synced;
}

bool ble_bme_subscribed(void)
{
    return s_conn_handle != BLE_HS_CONN_HANDLE_NONE && s_subscribed;
}

bool ble_bme_stream_enabled(void)
{
    return s_stream_enabled;
}

unsigned ble_bme_mtu(void)
{
    return s_mtu;
}

bool ble_bme_notify(const void *data, size_t len)
{
    if (s_conn_handle == BLE_HS_CONN_HANDLE_NONE || !s_subscribed) {
        return false;
    }
    /* ATT payload is MTU-3; refuse to send instead of letting the stack truncate. */
    if (s_mtu != 0 && len > s_mtu - 3) {
        if (!s_mtu_warned) {
            ESP_LOGW(TAG, "MTU=%u too small for a %u byte frame, host must request a bigger MTU",
                     s_mtu, (unsigned)len);
            s_mtu_warned = true;
        }
        return false;
    }

    struct os_mbuf *om = ble_hs_mbuf_from_flat(data, (uint16_t)len);
    if (om == NULL) {
        return false;
    }
    /* The stack takes ownership of om and frees it on both paths. */
    int rc = ble_gatts_notify_custom(s_conn_handle, s_tx_val_handle, om);
    return rc == 0;
}
