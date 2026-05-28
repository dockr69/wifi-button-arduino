/*
 * ESP32-C6 Thread Button
 * Sendet Audio-Trigger per Thread/CoAP an Raspberry Pi (Sonoff Dongle Plus MG24)
 * Deep Sleep ~100µA (2500mAh LiPo = ~2 Jahre)
 */

#include <OpenThread.h>
#include <esp_sleep.h>
#include <driver/gpio.h>

// ==================== KONFIGURATION ====================

// Thread Netzwerk (vom Sonoff Dongle Plus MG24)
const char* THREAD_NETWORK_NAME = "MyThreadNet";
const char* THREAD_NETWORK_KEY  = "0123456789abcdef0123456789abcdef"; // 16 Byte Hex
const uint8_t THREAD_CHANNEL    = 15;
const uint16_t THREAD_PANID     = 0x1a2b;

// Pi Thread Border Router IPv6 (von "ot-ctl ifconfig" am Pi)
const char* COAP_SERVER_ADDR = "fd33:1234:5678:9abc::1"; // Ersetze mit echter IPv6!
const int    COAP_PORT        = 5683;        // Standard CoAP Port
const char* COAP_PATH         = "/trigger";  // CoAP Pfad

// GPIO Konfiguration
const gpio_num_t WAKEUP_PIN      = GPIO_NUM_2;           // Button (GPIO2)
const gpio_num_t LED_PIN         = GPIO_NUM_25;          // LED (optional)
const gpio_num_t POWER_PIN       = GPIO_NUM_20;          // Power-Control (held)

// Timing
const unsigned long WIFI_TIMEOUT_MS = 10000;
const unsigned long MAINTENANCE_MS  = 5000;

// ==================== CODE ====================

void setup() {
    // Power-Control GPIO (STEMMA QT / NeoPixel)
    gpio_set_direction(POWER_PIN, GPIO_MODE_OUTPUT);
    gpio_set_level(POWER_PIN, 0);
    gpio_hold_en(POWER_PIN);

    // Serial starten
    Serial.begin(115200);
    delay(50);

    // Wakeup-Ursache prüfen
    esp_sleep_wakeup_cause_t cause = esp_sleep_get_wakeup_cause();
    if (cause == ESP_SLEEP_WAKEUP_GPIO) {
        Serial.println("=== GPIO WAKEUP - BUTTON PRESSED ===");
    } else {
        Serial.printf("=== UNEXPECTED WAKEUP: %d ===\n", cause);
    }

    // LED initialisieren
    gpio_set_direction(LED_PIN, GPIO_MODE_OUTPUT);
    gpio_set_level(LED_PIN, 1); // Aus (active low)

    // OpenThread initialisieren
    Serial.println("Initializing OpenThread...");
    OpenThread.begin();

    // Dataset erstellen (Panic Network)
    DataSet dataset;
    dataset.initNew();
    dataset.setNetworkName(THREAD_NETWORK_NAME);
    dataset.setChannel(THREAD_CHANNEL);
    dataset.setNetworkKey(THREAD_NETWORK_KEY);
    dataset.setPanid(THREAD_PANID);

    // Dataset commit und starten
    OpenThread.commitDataSet(dataset);
    OpenThread.start();

    // Border Router aktivieren (Pi als BR)
    OpenThread.setBorderRouter(true);
    OpenThread.networkInterfaceUp();

    // Warten bis Thread joined (Role = Child)
    Serial.println("Waiting for Thread network...");
    unsigned long timeout = millis() + 30000; // 30s Timeout
    while (OpenThread.getRole() == OT_DEVICE_ROLE_DISABLED && millis() < timeout) {
        delay(100);
        Serial.print(".");
    }

    // Prüfen ob Thread verbunden
    if (OpenThread.getRole() == OT_DEVICE_ROLE_DISABLED) {
        Serial.println("\nERROR: Thread join failed!");
        Serial.println("Check Pi Sonoff Dongle and network config.");
        enterDeepSleep();
        return;
    }

    // Ausgeschaltetes LED einschalten
    gpio_set_level(LED_PIN, 0); // Ein
    delay(100);
    gpio_set_level(LED_PIN, 1); // Aus

    // Thread-Details ausgeben
    Serial.println("\n=== THREAD STATUS ===");
    Serial.printf("Role: %d\n", OpenThread.getRole());
    Serial.printf("State: %s\n", OpenThread.getState());

    // Eigene IPv6-Adresse ausgeben
    char ipv6Str[45];
    OpenThread.getIpv6Address(MAIN_ADDRESS_TYPE, ipv6Str);
    Serial.printf("My IPv6: %s\n", ipv6Str);

    // CoAP Message an Pi senden
    Serial.println("Sending CoAP request...");
    sendCoapMessage(COAP_SERVER_ADDR, COAP_PORT, COAP_PATH, "test1.mp3");

    // Deep Sleep
    enterDeepSleep();
}

void loop() {
    // Nichts - alles in setup()
}

// ==================== FUNCTIONEN ====================

void sendCoapMessage(const char* addr, int port, const char* path, const char* payload) {
    Serial.printf("CoAP [%s]:%d %s\n", addr, port, path);

    // UDP Socket für CoAP
    WiFiUDP udp;
    if (!udp.beginMulticast(INADDR_ANY, addr)) {
        Serial.println("Failed to start multicast");
        return;
    }

    // CoAP Request bauen (GET Request)
    String coapRequest = "GET ";
    coapRequest += path;
    coapRequest += " HTTP/1.1\r\n";
    coapRequest += "Host: ";
    coapRequest += addr;
    coapRequest += "\r\n";
    coapRequest += "\r\n";

    // Senden
    udp.beginPacket(addr, port);
    udp.write(coapRequest.c_str(), coapRequest.length());
    udp.endPacket();

    Serial.println("CoAP sent!");

    // Antwort abwarten (optional)
    delay(500);
    while (udp.parsePacket()) {
        String response = udp.readString();
        Serial.printf("CoAP Response: %s\n", response.c_str());
    }
}

void enterDeepSleep() {
    Serial.println("\n=== ENTERING DEEP SLEEP ===");
    Serial.flush();

    // LED ausschalten
    gpio_set_level(LED_PIN, 1);

    // WiFi/Thread deaktivieren
    WiFi.disconnect(true);
    WiFi.mode(WIFI_OFF);

    // GPIO2 Wakeup konfigurieren (Button: low-trigger)
    gpio_pullup_en(WAKEUP_PIN);
    gpio_pulldown_dis(WAKEUP_PIN);

    // Deep Sleep mit GPIO Wakeup
    esp_deep_sleep_enable_gpio_wakeup(1ULL << WAKEUP_PIN, ESP_GPIO_WAKEUP_GPIO_LOW);

    // Optional: Timer Wakeup (z.B. alle 1h checken)
    // esp_deep_sleep_enable_timer_wakeup(3600000000ULL); // 1h

    Serial.printf("Sleeping... (Current: ~100uA)\n");
    Serial.flush();

    // Deep Sleep starten
    esp_deep_sleep_start();
}
