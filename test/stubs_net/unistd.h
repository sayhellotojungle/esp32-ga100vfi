#pragma once
#include <stddef.h>

/* Only close() is used from unistd.h (to shut the beacon socket). */
int close(int fd);
