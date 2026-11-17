#pragma once
#include "esp_err.h"
enum { GPIO_MODE_INPUT, GPIO_MODE_OUTPUT_OD };
enum { GPIO_FLOATING, GPIO_PULLDOWN_ONLY, GPIO_PULLUP_ONLY };
esp_err_t gpio_set_direction(int pin, int mode);
esp_err_t gpio_set_level(int pin, int level);
esp_err_t gpio_set_pull_mode(int pin, int pull);
int gpio_get_level(int pin);
