#include <WiFi.h>
#include <esp_sleep.h>
#include <driver/gpio.h>

// ---- Konfiguration ----
const char* WIFI_SSID     = "TPPP";
const char* WIFI_PASSWORD = "";

const IPAddress STATIC_IP(192, 168, 2, 123);
const IPAddress GATEWAY(192, 168, 2, 1);
const IPAddress SUBNET(255, 255, 255, 0);
const IPAddress DNS(8, 8, 8, 8);

const char* HTTP_HOST_0 = "192.168.2.175";
const int   HTTP_PORT_0 = 80;
const char* HTTP_PATH_0 = "/cgi-bin/index.cgi?webif-pass=1&spotrequest=test1.mp3";

const gpio_num_t WAKEUP_PIN = GPIO_NUM_2;

const unsigned long WIFI_TIMEOUT_MS = 10000;
const unsigned long HTTP_TIMEOUT_MS = 3000;
const unsigned long MAINTENANCE_MS  = 5000;

// WiFi cache survives deep sleep
RTC_DATA_ATTR int savedChannel = 0;
RTC_DATA_ATTR uint8_t savedBSSID[6] = {0};
RTC_DATA_ATTR bool hasCachedWiFi = false;

void sendHttpRequest(const char* host, int port, const char* path, const char* method);
void enterDeepSleep();

// ---- Setup (runs once on every wake) ----
void setup() {
  // STEMMA QT / NeoPixel power off + hold through deep sleep
  gpio_set_direction(GPIO_NUM_20, GPIO_MODE_OUTPUT);
  gpio_set_level(GPIO_NUM_20, 0);
  gpio_hold_en(GPIO_NUM_20);

  Serial.begin(115200);
  delay(50);

  esp_sleep_wakeup_cause_t cause = esp_sleep_get_wakeup_cause();
  if (cause == ESP_SLEEP_WAKEUP_GPIO) {
    Serial.println("GPIO wakeup - button pressed");
  } else {
    Serial.printf("Other wakeup cause: %d\n", cause);
  }

  WiFi.persistent(false);
  WiFi.config(STATIC_IP, GATEWAY, SUBNET, DNS);
  WiFi.setTxPower(WIFI_POWER_19_5dBm);
  WiFi.setSleep(false);
  WiFi.mode(WIFI_STA);
  WiFi.setMinSecurity(WIFI_AUTH_WPA2_PSK);
  WiFi.setScanMethod(WIFI_FAST_SCAN);
  WiFi.setSortMethod(WIFI_CONNECT_AP_BY_SIGNAL);

  // Use cached BSSID + channel if available (skips scan)
  if (hasCachedWiFi && savedChannel > 0) {
    Serial.printf("Fast connect: CH %d, BSSID %02X:%02X:%02X:%02X:%02X:%02X\n",
      savedChannel, savedBSSID[0], savedBSSID[1], savedBSSID[2],
      savedBSSID[3], savedBSSID[4], savedBSSID[5]);
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD, savedChannel, savedBSSID);
  } else {
    Serial.println("No cache, full scan");
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  }

  Serial.println("Connecting WiFi...");

  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < WIFI_TIMEOUT_MS) {
    // Cached connect failed? Retry with full scan
    if (hasCachedWiFi && millis() - start > 4000 && WiFi.status() != WL_CONNECTED) {
      Serial.println("Cache miss - fallback to full scan");
      hasCachedWiFi = false;
      WiFi.disconnect();
      WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    }
    delay(100);
    if ((millis() - start) % 1000 < 100) {
      Serial.printf("  WiFi status: %d (%lu ms)\n", WiFi.status(), millis() - start);
    }
  }

  if (WiFi.status() == WL_CONNECTED) {
    savedChannel = WiFi.channel();
    memcpy(savedBSSID, WiFi.BSSID(), 6);
    hasCachedWiFi = true;

    Serial.printf("WiFi OK (%lu ms), IP: %s, CH: %d\n",
      millis() - start, WiFi.localIP().toString().c_str(), savedChannel);
    sendHttpRequest(HTTP_HOST_0, HTTP_PORT_0, HTTP_PATH_0, "GET");
    enterDeepSleep();
  } else {
    Serial.printf("WiFi FAILED after %lu ms, status: %d\n", millis() - start, WiFi.status());
    hasCachedWiFi = false;
    savedChannel = 0;
    Serial.println("Maintenance window");
    delay(MAINTENANCE_MS);
    enterDeepSleep();
  }
}

void loop() {
}

// ---- HTTP Fire-and-Forget ----
void sendHttpRequest(const char* host, int port, const char* path, const char* method) {
  Serial.printf("HTTP %s %s%s\n", method, host, path);
  unsigned long httpStart = millis();

  WiFiClient client;
  if (client.connect(host, port, HTTP_TIMEOUT_MS)) {
    client.printf("%s %s HTTP/1.0\r\n"
                  "Host: %s\r\n"
                  "Connection: close\r\n\r\n",
                  method, path, host);
    client.flush();
    delay(100);
    client.stop();
    Serial.printf("HTTP sent (%lu ms)\n", millis() - httpStart);
  } else {
    Serial.printf("HTTP connect failed (%lu ms)\n", millis() - httpStart);
  }
}

// ---- Deep Sleep (ESP32-C6) ----
void enterDeepSleep() {
  Serial.printf("Sleeping after %lu ms total uptime\n", millis());
  Serial.flush();

  WiFi.disconnect(true);
  WiFi.mode(WIFI_OFF);

  gpio_pullup_en(WAKEUP_PIN);
  gpio_pulldown_dis(WAKEUP_PIN);

  esp_deep_sleep_enable_gpio_wakeup(1ULL << WAKEUP_PIN, ESP_GPIO_WAKEUP_GPIO_LOW);

  esp_deep_sleep_start();
}
