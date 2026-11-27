#pragma once
#include "FreeRTOS.h"
void vTaskDelay(TickType_t ticks);
typedef void *TaskHandle_t;
BaseType_t xTaskCreatePinnedToCore(void (*fn)(void*),const char*,uint32_t,void*,UBaseType_t,void*,BaseType_t);
/* The PEX hold guard task is deliberately not core-pinned, so the model needs it too. */
BaseType_t xTaskCreate(void (*fn)(void*),const char*,uint32_t,void*,UBaseType_t,void*);
/* 0.5.8 的临界区自旋与"把**实际**优先级报进 /api/status"用到这三件事：模型里它们必须存在，
 * 否则 main.c 编不过；而 uxTaskPriorityGet 特意不是常量 0 —— 它回创建时记下的值，好让
 * test_main.c 能断言"页面/台账里那个 exec_prio 是真的从任务里读出来的"。 */
void vTaskSuspendAll(void);
BaseType_t xTaskResumeAll(void);
UBaseType_t uxTaskPriorityGet(const TaskHandle_t);
