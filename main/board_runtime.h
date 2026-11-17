#pragma once
#include <stdbool.h>
/* Maintenance reserves the same hardware owner lock as command execution. */
bool board_is_busy(void);
bool board_begin_maintenance(void);
void board_end_maintenance(void);
void board_status_json(char *out, int capacity);
