#pragma once
#include "esp_err.h"
enum {GPIO_MODE_INPUT,GPIO_MODE_OUTPUT_OD};
enum {GPIO_FLOATING};
esp_err_t gpio_set_direction(int,int);
esp_err_t gpio_set_pull_mode(int,int);
