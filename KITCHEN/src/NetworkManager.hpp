#ifndef NETWORK_MANAGER_HPP
#define NETWORK_MANAGER_HPP

#include <WiFi.h>
#include <PubSubClient.h>
#include <HTTPUpdate.h>
#include "Config.hpp"

WiFiClient espClient;
PubSubClient client(espClient);

typedef void (*MsgCallback)(char*, uint8_t*, unsigned int);

unsigned long lastReconnectAttempt = 0;

// ================= SETUP NETWORK =================

void setupNetwork(MsgCallback callback) {
    Serial.println("\n--- BAT DAU KET NOI (DIRECT HOTSPOT MODE) ---");

    WiFi.disconnect(true);
    delay(100);
    WiFi.mode(WIFI_STA);
    WiFi.setSleep(false);

    // IP tĩnh — không chờ DHCP
    IPAddress local_IP(10, 42, 0, 52);
    IPAddress gateway(10, 42, 0, 1);
    IPAddress subnet(255, 255, 255, 0);

    if (!WiFi.config(local_IP, gateway, subnet)) {
        Serial.println("Loi cau hinh IP Tinh!");
    }

    Serial.print("Dang ket noi vao Hotspot: ");
    Serial.println(WIFI_SSID);

    if (String(WIFI_PASS) == "") WiFi.begin(WIFI_SSID, NULL);
    else                         WiFi.begin(WIFI_SSID, WIFI_PASS);

    int retries = 0;
    while (WiFi.status() != WL_CONNECTED && retries < 40) {
        delay(500);
        Serial.print(".");
        retries++;
    }

    if (WiFi.status() == WL_CONNECTED) {
        Serial.println("\nKET NOI WIFI THANH CONG!");
        Serial.print("IP ESP32: "); Serial.println(WiFi.localIP());
        Serial.print("Gateway:  "); Serial.println(WiFi.gatewayIP());
        Serial.print("Signal:   "); Serial.println(WiFi.RSSI());
    } else {
        Serial.print("\nTHAT BAI. Status: ");
        Serial.println(WiFi.status());
    }

    client.setServer(MQTT_SERVER, MQTT_PORT);
    client.setCallback(callback);
    client.setSocketTimeout(15);
    client.setKeepAlive(15);
}

// ================= MAINTAIN CONNECTION =================

inline void maintainConnection() {
    // 1. Kiểm tra WiFi trước
    if (WiFi.status() != WL_CONNECTED) {
        long now = millis();
        if (now - lastReconnectAttempt > 5000) {
            lastReconnectAttempt = now;
            Serial.println(" [WIFI] Mat ket noi. Dang thu lai...");
            
            // Chỉ gọi WiFi.begin() nếu thực sự không còn đang trong quá trình kết nối
            // Điều này giúp tránh việc reset kết nối liên tục
            WiFi.disconnect();
            if (String(WIFI_PASS) == "") WiFi.begin(WIFI_SSID, NULL);
            else                         WiFi.begin(WIFI_SSID, WIFI_PASS);
        }
        return; // Thoát sớm, không thử kết nối MQTT nếu WiFi chưa xong
    }

    // 2. Nếu WiFi đã OK, kiểm tra MQTT
    if (!client.connected()) {
        long now = millis();
        if (now - lastReconnectAttempt > 5000) {
            lastReconnectAttempt = now;

            String clientId = String(DEVICE_NODE_ID) + "_" + String(random(0xffff), HEX);
            Serial.print(" [MQTT] Dang ket noi Broker (" + String(MQTT_SERVER) + ")... ");

            if (client.connect(clientId.c_str())) {
                Serial.println("Thanh cong!");
                client.subscribe(TOPIC_CMD);
                Serial.println(" [MQTT] Da dang ky topic: " + String(TOPIC_CMD));
            } else {
                Serial.print("That bai, rc=");
                Serial.println(client.state());
            }
        }
    } else {
        // 3. Nếu mọi thứ đều ổn, duy trì vòng lặp MQTT
        client.loop();
    }
}

// ================= SEND MQTT =================

inline void sendMQTT(const char* topic, String jsonString) {
    if (client.connected()) {
        client.publish(topic, jsonString.c_str());
    }
}

// ================= OTA UPDATE =================
// Node Kitchen không có LCD nên log OTA qua Serial.
// Nếu sau này thêm LCD, chỉ cần bỏ comment các dòng lcd.*

void performOTA(String url) {
    Serial.println("[OTA] Starting update from: " + url);
    Serial.println("[OTA] Disconnecting MQTT...");

    // Ngắt MQTT trước để giải phóng socket
    client.disconnect();
    delay(200);

    // Callback tiến trình — log % qua Serial
    httpUpdate.onProgress([](int cur, int total) {
        if (total > 0) {
            int pct = (cur * 100) / total;
            Serial.printf("[OTA] Progress: %d%%\n", pct);
        }
    });

    httpUpdate.setLedPin(LED_BUILTIN, LOW);
    httpUpdate.rebootOnUpdate(true); // Tự reboot sau khi flash thành công

    WiFiClient otaClient;
    t_httpUpdate_return ret = httpUpdate.update(otaClient, url);

    // Chỉ chạy đến đây nếu OTA thất bại (thành công → đã reboot)
    switch (ret) {
        case HTTP_UPDATE_FAILED:
            Serial.printf("[OTA] FAILED (%d): %s\n",
                httpUpdate.getLastError(),
                httpUpdate.getLastErrorString().c_str());
            break;

        case HTTP_UPDATE_NO_UPDATES:
            Serial.println("[OTA] No update available.");
            break;

        case HTTP_UPDATE_OK:
            // Không bao giờ đến đây nếu rebootOnUpdate=true
            Serial.println("[OTA] OK!");
            break;
    }

    // Reconnect MQTT sau khi OTA thất bại
    Serial.println("[OTA] Reconnecting MQTT...");
    lastReconnectAttempt = 0;
}

#endif
