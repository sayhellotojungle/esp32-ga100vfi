// ESP32-S3 Arduino entry point; use an Arduino-ESP32 3.x core that bundles
// ESP-IDF >=5.3. This source revision has not been cross-compiled here.
// ESP32S3 Dev Module: FlashSize=16M, PSRAM=opi, USB CDC On Boot=Disabled.
// Run python tools/sync_arduino.py after editing shared main/*.c or *.h.
// The local partitions.csv provides two OTA application slots.
extern "C" void ga_app_main(void);
void setup() { ga_app_main(); } // creates the hardware-owner task and returns
void loop() { delay(1000); }
