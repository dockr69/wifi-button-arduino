// WiFi-Button Base-Image — Adafruit Feather ESP32-C6
//
// Generische Universal-Firmware: EINMAL flashen, danach Config per USB-Serial
// ins NVS schreiben (kein Recompile pro Gerät). Betriebslogik identisch zum
// alten Codegen (Deep-Sleep, GPIO-Wake, WiFi-Cache, Repeat, Cooldown, IP-Modi),
// aber alle Werte kommen aus Preferences statt einkompiliert.
//
// Serial-Config-Protokoll (115200), zeilenweise:
//   MAC?            -> "MAC <aa:bb:..>"
//   VER?            -> "VER wbtn <FW>"
//   CFG?            -> aktuelle Config als key=val-Zeilen + "END"
//   SET <key> <val> -> Wert puffern (Preferences)
//   SAVE            -> committen -> "OK saved"
//   CLEAR           -> Config löschen -> "OK cleared"
//   RUN             -> Config-Modus verlassen
// USB angesteckt (Power-on/Reset) => Config-Modus. Batterie-Wake (GPIO/Timer)
// => normaler Betrieb, NVS-Werte.

#include <WiFi.h>
#include <esp_sleep.h>
#include <driver/gpio.h>
#include <sys/time.h>
#include <Preferences.h>
#include "esp_mac.h"

#define FW_VERSION 1
#define MAX_BUTTONS 8

// Hardcoded (Adafruit Feather ESP32-C6): A5/IO2 = RTC-GPIO, Taster gegen GND.
static const int DEFAULT_WAKE_PIN = 2;
static const unsigned long MAINTENANCE_HOLD_MS = 5000;
static const unsigned long MAINTENANCE_MS = 60000;

// ---- Laufzeit-Config (aus NVS) ----
Preferences prefs;
String   cfgSsid, cfgPass, cfgIpMode;
String   cfgIp, cfgGw, cfgSn, cfgDns;
unsigned long cfgWifiTimeoutMs = 10000, cfgHttpTimeoutMs = 3000;
int      cfgRepeatCount = 1;
unsigned long cfgRepeatIntervalMs = 60000;
uint64_t cfgCooldownUs = 0;
String   cfgTxPower = "20dBm";
bool     cfgPowerSave = false;
int      cfgWakePin = DEFAULT_WAKE_PIN;
int      cfgBtnCount = 0;
String   btnHost[MAX_BUTTONS], btnPath[MAX_BUTTONS], btnMethod[MAX_BUTTONS];
int      btnPort[MAX_BUTTONS];
gpio_num_t WAKEUP_PIN = GPIO_NUM_2;

// ---- RTC (überlebt Deep Sleep) ----
RTC_DATA_ATTR int     savedChannel = 0;
RTC_DATA_ATTR uint8_t savedBSSID[6] = {0};
RTC_DATA_ATTR bool    hasCachedWiFi = false;
RTC_DATA_ATTR int     repeatIndex = 0;
RTC_DATA_ATTR uint64_t lastSendUs = 0;
RTC_DATA_ATTR uint32_t cachedIP = 0, cachedGW = 0, cachedSN = 0, cachedDNS = 0;
RTC_DATA_ATTR bool    hasCachedIP = false;

void sendHttpRequest(const char* host, int port, const char* path, const char* method);
void enterDeepSleep();
void enterTimerSleep(uint64_t us);
void runButton();

// ---- TX-Power-Token -> enum ----
wifi_power_t txPowerFromToken(const String& t) {
  if (t == "17dBm")  return WIFI_POWER_17dBm;
  if (t == "15dBm")  return WIFI_POWER_15dBm;
  if (t == "13dBm")  return WIFI_POWER_13dBm;
  if (t == "11dBm")  return WIFI_POWER_11dBm;
  if (t == "8.5dBm") return WIFI_POWER_8_5dBm;
  if (t == "7dBm")   return WIFI_POWER_7dBm;
  if (t == "5dBm")   return WIFI_POWER_5dBm;
  if (t == "2dBm")   return WIFI_POWER_2dBm;
  return WIFI_POWER_19_5dBm;  // "20dBm" / default
}

static IPAddress parseIp(const String& s) {
  IPAddress ip;
  ip.fromString(s);
  return ip;
}

void loadConfig() {
  prefs.begin("wbtn", true);  // read-only
  cfgSsid = prefs.getString("ssid", "");
  cfgPass = prefs.getString("pass", "");
  cfgIpMode = prefs.getString("ipmode", "static");
  cfgIp  = prefs.getString("ip", "");
  cfgGw  = prefs.getString("gw", "");
  cfgSn  = prefs.getString("sn", "");
  cfgDns = prefs.getString("dns", "");
  cfgWifiTimeoutMs = prefs.getULong("wifitmo", 10000);
  cfgHttpTimeoutMs = prefs.getULong("httptmo", 3000);
  cfgRepeatCount = prefs.getInt("repcnt", 1);
  cfgRepeatIntervalMs = prefs.getULong("repint", 60000);
  cfgCooldownUs = prefs.getULong64("cooldn", 0);
  cfgTxPower = prefs.getString("txpow", "20dBm");
  cfgPowerSave = prefs.getBool("psave", false);
  cfgWakePin = prefs.getInt("wakepin", DEFAULT_WAKE_PIN);
  cfgBtnCount = prefs.getInt("btncnt", 0);
  if (cfgBtnCount > MAX_BUTTONS) cfgBtnCount = MAX_BUTTONS;
  for (int i = 0; i < cfgBtnCount; i++) {
    char k[8];
    snprintf(k, sizeof(k), "b%dhost", i); btnHost[i]   = prefs.getString(k, "");
    snprintf(k, sizeof(k), "b%dport", i); btnPort[i]   = prefs.getInt(k, 80);
    snprintf(k, sizeof(k), "b%dpath", i); btnPath[i]   = prefs.getString(k, "/");
    snprintf(k, sizeof(k), "b%dmeth", i); btnMethod[i] = prefs.getString(k, "GET");
  }
  prefs.end();
  WAKEUP_PIN = (gpio_num_t)cfgWakePin;
}

bool isConfigured() { return cfgSsid.length() > 0; }

String macStr() {
  uint8_t m[6];
  esp_read_mac(m, ESP_MAC_WIFI_STA);
  char b[18];
  snprintf(b, sizeof(b), "%02X:%02X:%02X:%02X:%02X:%02X",
           m[0], m[1], m[2], m[3], m[4], m[5]);
  return String(b);
}

// ---- Serial-Config-Modus ----
void dumpConfig() {
  Serial.printf("ssid=%s\n", cfgSsid.c_str());
  Serial.printf("ipmode=%s\n", cfgIpMode.c_str());
  Serial.printf("ip=%s\n", cfgIp.c_str());
  Serial.printf("gw=%s\n", cfgGw.c_str());
  Serial.printf("sn=%s\n", cfgSn.c_str());
  Serial.printf("dns=%s\n", cfgDns.c_str());
  Serial.printf("wifitmo=%lu\n", cfgWifiTimeoutMs);
  Serial.printf("httptmo=%lu\n", cfgHttpTimeoutMs);
  Serial.printf("repcnt=%d\n", cfgRepeatCount);
  Serial.printf("repint=%lu\n", cfgRepeatIntervalMs);
  Serial.printf("txpow=%s\n", cfgTxPower.c_str());
  Serial.printf("psave=%d\n", cfgPowerSave ? 1 : 0);
  Serial.printf("btncnt=%d\n", cfgBtnCount);
  for (int i = 0; i < cfgBtnCount; i++)
    Serial.printf("b%d=%s %d %s %s\n", i, btnHost[i].c_str(), btnPort[i],
                  btnMethod[i].c_str(), btnPath[i].c_str());
  Serial.println("END");
}

void handleSet(const String& rest) {
  int sp = rest.indexOf(' ');
  if (sp < 0) { Serial.println("ERR set"); return; }
  String key = rest.substring(0, sp);
  String val = rest.substring(sp + 1);
  prefs.begin("wbtn", false);
  // Typed keys -> richtige Preferences-Typen
  if (key == "wifitmo" || key == "httptmo" || key == "repint")
    prefs.putULong(key.c_str(), (uint32_t)val.toInt());
  else if (key == "cooldn")
    prefs.putULong64(key.c_str(), strtoull(val.c_str(), NULL, 10));
  else if (key == "repcnt" || key == "wakepin" || key == "btncnt" ||
           key.endsWith("port"))
    prefs.putInt(key.c_str(), (int)val.toInt());
  else if (key == "psave")
    prefs.putBool(key.c_str(), val.toInt() != 0);
  else
    prefs.putString(key.c_str(), val);
  prefs.end();
  Serial.println("OK");
}

// Liest die Config (NVS) erneut, läuft die Serial-Kommandoschleife solange USB
// verbunden ist. Rückgabe true = RUN (sofort in den Betrieb).
bool configMode() {
  Serial.printf("CFG? WBTN FW=%d configured=%d\n", FW_VERSION, isConfigured() ? 1 : 0);
  unsigned long lastActivity = millis();
  String line;
  for (;;) {
    // USB weg -> Config-Modus verlassen (verhindert Batterie-Dauerlauf bei
    // unkonfigurierten Boards). Mit USB bleibt er offen, bis SAVE/RUN/Idle.
    if (!Serial) return false;

    while (Serial.available()) {
      char c = (char)Serial.read();
      lastActivity = millis();
      if (c == '\n' || c == '\r') {
        line.trim();
        if (line.length()) {
          if (line == "MAC?") {
            Serial.printf("MAC %s\n", macStr().c_str());
          } else if (line == "VER?") {
            Serial.printf("VER wbtn %d\n", FW_VERSION);
          } else if (line == "CFG?") {
            dumpConfig();
          } else if (line == "SAVE") {
            loadConfig();
            Serial.println("OK saved");
          } else if (line == "CLEAR") {
            prefs.begin("wbtn", false); prefs.clear(); prefs.end();
            loadConfig();
            Serial.println("OK cleared");
          } else if (line == "RUN") {
            Serial.println("OK run");
            return true;
          } else if (line.startsWith("SET ")) {
            handleSet(line.substring(4));
          } else {
            Serial.println("ERR unknown");
          }
        }
        line = "";
      } else if (line.length() < 256) {
        line += c;
      }
    }
    // Idle-Timeout nur für bereits konfigurierte Geräte (dann schlafen).
    if (isConfigured() && millis() - lastActivity > MAINTENANCE_MS) return false;
    delay(10);
  }
}

void setup() {
  // STEMMA QT / NeoPixel aus + über Deep Sleep halten
  gpio_set_direction(GPIO_NUM_20, GPIO_MODE_OUTPUT);
  gpio_set_level(GPIO_NUM_20, 0);
  gpio_hold_en(GPIO_NUM_20);

  Serial.begin(115200);
  delay(50);
  loadConfig();

  esp_sleep_wakeup_cause_t cause = esp_sleep_get_wakeup_cause();

  if (cause != ESP_SLEEP_WAKEUP_GPIO && cause != ESP_SLEEP_WAKEUP_TIMER) {
    // Power-on / Reset / USB: Config-Fenster.
    delay(200);  // kurz auf USB-CDC-Host warten
    if (Serial || !isConfigured()) {
      bool runNow = configMode();
      if (!runNow) {
        if (!isConfigured()) { enterDeepSleep(); }  // nichts zu tun
        enterDeepSleep();  // konfiguriert: auf echten Tastendruck warten
      }
      // RUN: fällt in runButton() durch (Test-Sendung)
    } else {
      enterDeepSleep();
    }
  }
  runButton();
}

void loop() {}

// ---- Normalbetrieb (GPIO/Timer-Wake oder RUN) ----
void runButton() {
  esp_sleep_wakeup_cause_t cause = esp_sleep_get_wakeup_cause();

  if (cause == ESP_SLEEP_WAKEUP_GPIO) {
    if (repeatIndex > 0) {
      Serial.printf("GPIO wake mid-sequence (after %d sends) - CANCEL\n", repeatIndex);
      repeatIndex = 0;
      enterDeepSleep();
    }
    Serial.println("GPIO wakeup - button pressed");

    gpio_pullup_en(WAKEUP_PIN);
    unsigned long pressStart = millis();
    while (gpio_get_level(WAKEUP_PIN) == 0 && millis() - pressStart < MAINTENANCE_HOLD_MS) {
      delay(20);
    }
    if (gpio_get_level(WAKEUP_PIN) == 0) {
      Serial.printf("Long press - MAINTENANCE %lu ms (USB live)\n", MAINTENANCE_MS);
      unsigned long relStart = millis();
      while (gpio_get_level(WAKEUP_PIN) == 0 && millis() - relStart < 30000) { delay(50); }
      delay(MAINTENANCE_MS);
      enterDeepSleep();
    }

    if (cfgCooldownUs > 0 && lastSendUs > 0) {
      struct timeval tv; gettimeofday(&tv, NULL);
      uint64_t nowUs = (uint64_t)tv.tv_sec * 1000000ULL + tv.tv_usec;
      if (nowUs > lastSendUs && (nowUs - lastSendUs) < cfgCooldownUs) {
        Serial.printf("Cooldown active - %llu ms remaining\n",
                      (cfgCooldownUs - (nowUs - lastSendUs)) / 1000);
        enterDeepSleep();
      }
    }
  } else if (cause == ESP_SLEEP_WAKEUP_TIMER) {
    Serial.printf("Timer wakeup - repeat %d/%d\n", repeatIndex + 1, cfgRepeatCount);
  } else {
    Serial.printf("Other wakeup cause: %d\n", cause);
    repeatIndex = 0;
  }

  if (!isConfigured()) {
    Serial.println("Keine Config -> Maintenance");
    delay(MAINTENANCE_MS);
    enterDeepSleep();
  }

  WiFi.persistent(false);
  if (cfgIpMode == "static") {
    WiFi.config(parseIp(cfgIp), parseIp(cfgGw), parseIp(cfgSn), parseIp(cfgDns));
  } else if (cfgIpMode == "dhcp_cache" && hasCachedIP) {
    WiFi.config(IPAddress(cachedIP), IPAddress(cachedGW),
                IPAddress(cachedSN), IPAddress(cachedDNS));
  }

  WiFi.setTxPower(txPowerFromToken(cfgTxPower));
  WiFi.setSleep(cfgPowerSave);
  WiFi.mode(WIFI_STA);
  WiFi.setMinSecurity(WIFI_AUTH_WPA2_PSK);
  WiFi.setScanMethod(WIFI_FAST_SCAN);
  WiFi.setSortMethod(WIFI_CONNECT_AP_BY_SIGNAL);

  if (hasCachedWiFi && savedChannel > 0) {
    Serial.printf("Fast connect CH %d\n", savedChannel);
    WiFi.begin(cfgSsid.c_str(), cfgPass.c_str(), savedChannel, savedBSSID);
  } else {
    Serial.println("Full scan");
    WiFi.begin(cfgSsid.c_str(), cfgPass.c_str());
  }

  Serial.println("Connecting WiFi...");
  unsigned long start = millis();
  bool cacheRetried = false;
  while (WiFi.status() != WL_CONNECTED && millis() - start < cfgWifiTimeoutMs) {
    if (!cacheRetried && hasCachedWiFi && millis() - start > 4000) {
      Serial.println("Cache miss - full scan");
      cacheRetried = true; hasCachedWiFi = false;
      WiFi.disconnect();
      WiFi.begin(cfgSsid.c_str(), cfgPass.c_str());
    }
    delay(100);
  }

  if (WiFi.status() == WL_CONNECTED) {
    savedChannel = WiFi.channel();
    memcpy(savedBSSID, WiFi.BSSID(), 6);
    hasCachedWiFi = true;
    if (cfgIpMode == "dhcp_cache" && !hasCachedIP) {
      cachedIP = (uint32_t)WiFi.localIP();   cachedGW = (uint32_t)WiFi.gatewayIP();
      cachedSN = (uint32_t)WiFi.subnetMask(); cachedDNS = (uint32_t)WiFi.dnsIP();
      hasCachedIP = true;
    }
    Serial.printf("WiFi OK (%lu ms), IP %s\n", millis() - start,
                  WiFi.localIP().toString().c_str());

    if (repeatIndex == 0) {
      struct timeval tv; gettimeofday(&tv, NULL);
      lastSendUs = (uint64_t)tv.tv_sec * 1000000ULL + tv.tv_usec;
    }

    for (int i = 0; i < cfgBtnCount; i++)
      sendHttpRequest(btnHost[i].c_str(), btnPort[i], btnPath[i].c_str(),
                      btnMethod[i].c_str());

    repeatIndex++;
    if (repeatIndex < cfgRepeatCount) {
      Serial.printf("Repeat in %lu ms (%d/%d)\n", cfgRepeatIntervalMs, repeatIndex, cfgRepeatCount);
      enterTimerSleep((uint64_t)cfgRepeatIntervalMs * 1000ULL);
    } else {
      repeatIndex = 0;
      enterDeepSleep();
    }
  } else {
    Serial.printf("WiFi FAILED after %lu ms\n", millis() - start);
    hasCachedWiFi = false; savedChannel = 0; repeatIndex = 0;
    if (cfgIpMode == "dhcp_cache") hasCachedIP = false;
    delay(MAINTENANCE_MS);
    enterDeepSleep();
  }
}

void sendHttpRequest(const char* host, int port, const char* path, const char* method) {
  Serial.printf("HTTP %s %s:%d%s\n", method, host, port, path);
  unsigned long t0 = millis();
  WiFiClient client;
  if (client.connect(host, port, cfgHttpTimeoutMs)) {
    client.printf("%s %s HTTP/1.0\r\nHost: %s\r\nConnection: close\r\n\r\n",
                  method, path, host);
    client.flush();
    delay(100);
    client.stop();
    Serial.printf("HTTP sent (%lu ms)\n", millis() - t0);
  } else {
    Serial.printf("HTTP connect failed (%lu ms)\n", millis() - t0);
  }
}

void enterDeepSleep() {
  Serial.printf("Sleeping (uptime %lu ms)\n", millis());
  WiFi.disconnect(true);
  WiFi.mode(WIFI_OFF);
  unsigned long relStart = millis();
  while (gpio_get_level(WAKEUP_PIN) == 0 && millis() - relStart < 5000) { delay(20); }
  Serial.flush();
  gpio_pullup_en(WAKEUP_PIN);
  gpio_pulldown_dis(WAKEUP_PIN);
  esp_deep_sleep_enable_gpio_wakeup(1ULL << WAKEUP_PIN, ESP_GPIO_WAKEUP_GPIO_LOW);
  esp_deep_sleep_start();
}

void enterTimerSleep(uint64_t us) {
  Serial.printf("Timer sleep %llu us\n", us);
  WiFi.disconnect(true);
  WiFi.mode(WIFI_OFF);
  unsigned long relStart = millis();
  while (gpio_get_level(WAKEUP_PIN) == 0 && millis() - relStart < 5000) { delay(20); }
  Serial.flush();
  gpio_pullup_en(WAKEUP_PIN);
  gpio_pulldown_dis(WAKEUP_PIN);
  esp_deep_sleep_enable_gpio_wakeup(1ULL << WAKEUP_PIN, ESP_GPIO_WAKEUP_GPIO_LOW);
  esp_sleep_enable_timer_wakeup(us);
  esp_deep_sleep_start();
}
