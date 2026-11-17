idf_component_register(
    SRCS "main.c" "pmbus.c" "glitch_plan.c"
         "console.c" "oracle.c" "campaign.c" "net.c" "protocol.c"
    INCLUDE_DIRS "."
    PRIV_REQUIRES esp_driver_i2c esp_timer esp_hw_support
                  nvs_flash esp_wifi esp_netif esp_event esp_http_server app_update
)
