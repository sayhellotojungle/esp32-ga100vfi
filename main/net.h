// WiFi (STA + always-on SoftAP fallback), web status/campaign/console server
// on :80, NVS-backed config. Zero-configuration boot: with no SSID saved the
// board still serves everything from 192.168.4.1.
#pragma once
#include <stdbool.h>

void net_init(void);                 // non-blocking bring-up + http server
bool net_sta_up(void);               // got an IP on the LAN
// "@W [ssid pass]" handler (from UART/web). NULL/empty args = show state.
void net_wifi_cmd(const char *args);
