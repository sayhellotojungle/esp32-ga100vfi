#pragma once
#include "FreeRTOS.h"
void vTaskDelay(TickType_t ticks);
BaseType_t xTaskCreatePinnedToCore(void (*fn)(void*),const char*,uint32_t,void*,UBaseType_t,void*,BaseType_t);
