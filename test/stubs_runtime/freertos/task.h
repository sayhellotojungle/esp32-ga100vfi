#pragma once
#include "FreeRTOS.h"
void vTaskDelay(TickType_t ticks);
BaseType_t xTaskCreatePinnedToCore(void (*fn)(void*),const char*,uint32_t,void*,UBaseType_t,void*,BaseType_t);
/* The PEX hold guard task is deliberately not core-pinned, so the model needs it too. */
BaseType_t xTaskCreate(void (*fn)(void*),const char*,uint32_t,void*,UBaseType_t,void*);
