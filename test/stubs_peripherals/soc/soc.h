#pragma once
#include <stdint.h>
void native_reg_write(unsigned reg, uint32_t value);
#define REG_WRITE(reg, value) native_reg_write((reg), (value))
