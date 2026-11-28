#include "button.h"

#include "board_config.h"
#include "driver/gpio.h"
#include "esp_check.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

static const char *TAG = "button";

static SemaphoreHandle_t s_edge_sem;
static int s_gpio;
static button_press_cb_t s_callback;
static void *s_callback_arg;

static void IRAM_ATTR button_isr(void *arg)
{
    (void)arg;
    BaseType_t woken = pdFALSE;
    xSemaphoreGiveFromISR(s_edge_sem, &woken);
    if (woken == pdTRUE) {
        portYIELD_FROM_ISR();
    }
}

static void button_task(void *arg)
{
    (void)arg;
    while (1) {
        if (xSemaphoreTake(s_edge_sem, portMAX_DELAY) != pdTRUE) {
            continue;
        }

        /* Still low after the debounce delay: a real press, not contact noise. */
        vTaskDelay(pdMS_TO_TICKS(BUTTON_DEBOUNCE_MS));
        if (gpio_get_level(s_gpio) != 0) {
            continue;
        }

        if (s_callback != NULL) {
            s_callback(s_callback_arg);
        }

        /* Wait for release so holding the button does not repeat the action. */
        while (gpio_get_level(s_gpio) == 0) {
            vTaskDelay(pdMS_TO_TICKS(20));
        }
    }
}

esp_err_t button_init(int gpio_num, button_press_cb_t on_press, void *arg)
{
    s_gpio = gpio_num;
    s_callback = on_press;
    s_callback_arg = arg;

    gpio_config_t io = {
        .pin_bit_mask = 1ULL << gpio_num,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_NEGEDGE, /* press pulls the pin low */
    };
    ESP_RETURN_ON_ERROR(gpio_config(&io), TAG, "gpio_config");

    s_edge_sem = xSemaphoreCreateBinary();
    if (s_edge_sem == NULL) {
        return ESP_ERR_NO_MEM;
    }

    esp_err_t err = gpio_install_isr_service(0);
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
        return err;
    }
    ESP_RETURN_ON_ERROR(gpio_isr_handler_add(gpio_num, button_isr, NULL), TAG, "isr add");

    if (xTaskCreate(button_task, "button", 3072, NULL, 6, NULL) != pdPASS) {
        return ESP_ERR_NO_MEM;
    }

    ESP_LOGI(TAG, "button on GPIO%d, active low", gpio_num);
    return ESP_OK;
}
