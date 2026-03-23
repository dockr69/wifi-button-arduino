#include <WiFi.h>
#include <HTTPClient.h>
#include <esp_sleep.h>
#include <driver/gpio.h>


// ---- Konfiguration ----
const char* WIFI_SSID     = "pos2.4";
const char* WIFI_PASSWORD = "posadm1n";

const IPAddress STATIC_IP(192, 168, 2, 125);
const IPAddress GATEWAY(192, 168, 2, 1);
const IPAddress SUBNET(255, 255, 255, 0);
const IPAddress DNS(8, 8, 8, 8);

const char* HTTP_URL = "http://192.168.2.250/cgi-bin/index.cgi?webif-pass=St@25rten&spotrequest=test1.mp3";

const gpio_num_t WAKEUP_PIN       = GPIO_NUM_2;
const gpio_num_t PERIPHERAL_POWER  = GPIO_NUM_20;

const unsigned long WIFI_TIMEOUT_MS = 5000;
const unsigned long HTTP_TIMEOUT_MS = 3000;
const unsigned long MAINTENANCE_MS  = 5000;

// ---- Setup (runs once on every wake) ----
void setup() {
  // Peripheral power off + hold (persists through deep sleep on ESP32-C6)a
  gpio_set_direction(PERIPHERAL_POWER, GPIO_MODE_OUTPUT);
  gpio_set_level(PERIPHERAL_POWER, 0);
  gpio_hold_en(PERIPHERAL_POWER);

  // Log wakeup cause
  esp_sleep_wakeup_cause_t cause = esp_sleep_get_wakeup_cause();
  if (cause == ESP_SLEEP_WAKEUP_GPIO || cause == ESP_SLEEP_WAKEUP_EXT0) {
    Serial.begin(115200);
    Serial.println("GPIO wakeup - button pressed");
  } else {
    Serial.begin(115200);
    Serial.printf("Other wakeup cause: %d\n", cause);
  }

  // Static IP (skips DHCP — saves ~1s)
  WiFi.config(STATIC_IP, GATEWAY, SUBNET, DNS);
  WiFi.setTxPower(WIFI_POWER_20dBm);
  WiFi.setSleep(false);  // power_save_mode: NONE
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  Serial.println("Connecting WiFi...");

  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < WIFI_TIMEOUT_MS) {
    delay(10);
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("WiFi OK (%lu ms), sending HTTP request\n", millis() - start);
    sendHttpRequest();
    enterDeepSleep();
  } else {
    Serial.println("WiFi failed, maintenance window");
    delay(MAINTENANCE_MS);
    enterDeepSleep();
  }
}

void loop() {
  // Never reached — device sleeps after setup()
}

// ---- HTTP Request ----
void sendHttpRequest() {
  HTTPClient http;
  http.setConnectTimeout(HTTP_TIMEOUT_MS);
  http.setTimeout(HTTP_TIMEOUT_MS);
  http.setUserAgent("esphome/device");
  http.begin(HTTP_URL);

  int code = http.GET();
  if (code > 0) {
    Serial.printf("HTTP OK: %d\n", code);
  } else {
    Serial.printf("HTTP failed: %s\n", http.errorToString(code).c_str());
  }
  http.end();
}

// ---- Deep Sleep ----
void enterDeepSleep() {
  Serial.println("Sleeping...");
  Serial.flush();

  WiFi.disconnect(true);
  WiFi.mode(WIFI_OFF);

  // Wakeup on GPIO2 LOW (button press pulls to GND)
  esp_deep_sleep_enable_gpio_wakeup(1ULL << WAKEUP_PIN, ESP_GPIO_WAKEUP_GPIO_LOW);

  esp_deep_sleep_start();
}
