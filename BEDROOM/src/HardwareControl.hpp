#ifndef HARDWARE_CONTROL_HPP
#define HARDWARE_CONTROL_HPP

#include <Arduino.h>
#include <ArduinoJson.h>
#include <Wire.h>
#include <Preferences.h> 
#include <vector> 
#include "Adafruit_CCS811.h"
#include "ClosedCube_HDC1080.h"
#include "Config.hpp"
#include "NetworkManager.hpp"
#include <nvs_flash.h>

// ================= ĐỐI TƯỢNG PHẦN CỨNG =================
Adafruit_CCS811 ccs;
ClosedCube_HDC1080 hdc1080;
Preferences preferences; 

// Biến chia sẻ giữa các luồng (volatile để an toàn luồng)
volatile bool fanState = false;
volatile bool lightState = false;

// Cờ báo hiệu (Flag) cho Main Loop
volatile bool fanChanged = false;
volatile bool lightChanged = false;

float valTemp = 0; float valHum = 0;
uint16_t valCO2 = 0; uint16_t valTVOC = 0;
bool hasCCS = false;

// Bộ đệm RAM lưu dữ liệu khi mất mạng
// Lưu tối đa 50 bản tin (khoảng 4-5 phút dữ liệu nếu gửi 5s/lần)
std::vector<String> sensorBuffer; 
const size_t MAX_BUFFER_SIZE = 50; 
// OTA Update variables
volatile bool otaInProgress = false;
bool otaAckPending = false;
String otaAckDocId = "";
String otaAckVersion = "";
String otaPendingVersion = "";
String otaPendingDocId = "";
unsigned long otaAckStartMs = 0;
const unsigned long OTA_ACK_TIMEOUT_MS = 60000;
String currentFirmwareVersion = "2.1.0";  // Hardcode version hiện tại
// ================= HÀM HELPER =================
void sendDeviceStatus(String docId, bool isOn) {
    // Trạng thái nút bấm ưu tiên gửi ngay nếu có mạng
    if (client.connected()) {
        StaticJsonDocument<200> doc;
        doc["deviceId"] = docId;
        doc["isOn"] = isOn;
        String out; serializeJson(doc, out);
        sendMQTT(TOPIC_STATUS, out);
    }
}

void saveState() {
    preferences.begin("bedroom_state", false); 
    preferences.putBool("fan", fanState);
    preferences.putBool("light", lightState);
    preferences.end();
}

// Hàm xả bộ đệm (Gửi dữ liệu cũ lên Gateway)
void flushSensorBuffer() {
    if (sensorBuffer.empty()) return;

    Serial.printf("[SYNC] Đang đồng bộ %d bản tin cũ...\n", sensorBuffer.size());
    
    // Gửi lần lượt các bản tin cũ
    for (const String& payload : sensorBuffer) {
        sendMQTT(TOPIC_SENSORS, payload);
        delay(50); // Delay nhẹ để tránh nghẽn mạng
    }
    
    // Xóa sạch bộ đệm sau khi gửi xong
    sensorBuffer.clear();
    Serial.println("[SYNC] Đã đồng bộ xong!");
}

// ================= TASK XỬ LÝ NÚT NHẤN =================
void taskButtonMonitor(void * parameter) {
    pinMode(PIN_BTN_FAN, INPUT_PULLUP);
    pinMode(PIN_BTN_LIGHT, INPUT_PULLUP);

    int lastFanState = HIGH;
    int lastLightState = HIGH;

    for (;;) { 
        // --- XỬ LÝ NÚT QUẠT ---
        int currentFan = digitalRead(PIN_BTN_FAN);
        if (lastFanState == HIGH && currentFan == LOW) { 
            fanState = !fanState; 
            digitalWrite(PIN_RELAY_FAN, fanState ? HIGH : LOW); 
            fanChanged = true; 
            Serial.println(" [TASK] Fan Button Pressed!");
        }
        lastFanState = currentFan;

        // --- XỬ LÝ NÚT ĐÈN ---
        int currentLight = digitalRead(PIN_BTN_LIGHT);
        if (lastLightState == HIGH && currentLight == LOW) { 
            lightState = !lightState;
            digitalWrite(PIN_LIGHT, lightState ? HIGH : LOW); 
            lightChanged = true;
            Serial.println(" [TASK] Light Button Pressed!");
        }
        lastLightState = currentLight;

        vTaskDelay(50 / portTICK_PERIOD_MS); 
    }
}

// ================= OTA UPDATE HANDLER =================
void performOTA(const String& url, const String& version, const String& docId) {
    if (otaInProgress) {
        Serial.println("[OTA] Already in progress, ignoring.");
        return;
    }
    otaInProgress = true;

    Serial.printf("[OTA] Starting OTA update from: %s\n", url.c_str());
    Serial.printf("[OTA] Target version: %s\n", version.c_str());

    // Gửi status 'starting' về Gateway trước khi flash
    if (client.connected()) {
        StaticJsonDocument<256> statusDoc;
        statusDoc["source"]   = "ota";
        statusDoc["event"]    = "ota_start";
        statusDoc["room_id"]  = ROOM_BEDROOM;
        statusDoc["version"]  = version;
        statusDoc["doc_id"]   = docId;
        String out; serializeJson(statusDoc, out);
        sendMQTT(TOPIC_STATUS, out);
    }

    otaPendingVersion = version;
    otaPendingDocId = docId;

    // Callback tiến trình download
    httpUpdate.onStart([]() {
        Serial.println("[OTA] HTTP Update started");
    });
    httpUpdate.onProgress([](int cur, int total) {
        Serial.printf("[OTA] Progress: %d/%d bytes (%.0f%%)\n",
                      cur, total, (float)cur/total*100);
    });
    httpUpdate.onEnd([]() {
        Serial.println("[OTA] Download complete. Rebooting...");
        Preferences ota_pref;
        if (ota_pref.begin("ota_state", false)) {
            ota_pref.putString("last_version", otaPendingVersion);
            ota_pref.putString("doc_id", otaPendingDocId);
            ota_pref.putBool("just_updated", true);
            ota_pref.end();
            Serial.println("[OTA] Marker saved to NVS before reboot");
        } else {
            Serial.println("[OTA] WARNING: unable to open ota_state namespace for NVS marker");
        }
    });
    httpUpdate.onError([](int err) {
        Serial.printf("[OTA] Error: %d\n", err);
    });

    // Thực hiện OTA
    WiFiClient wifiClient;
    t_httpUpdate_return ret = httpUpdate.update(wifiClient, url);

    switch (ret) {
        case HTTP_UPDATE_FAILED:
            Serial.printf("[OTA] FAILED: (%d) %s\n",
                          httpUpdate.getLastError(),
                          httpUpdate.getLastErrorString().c_str());
            // Gửi báo lỗi về Gateway
            if (client.connected()) {
                StaticJsonDocument<256> errDoc;
                errDoc["source"]  = "ota";
                errDoc["event"]   = "ota_failed";
                errDoc["room_id"] = ROOM_BEDROOM;
                errDoc["version"] = version;
                errDoc["doc_id"]  = docId;
                errDoc["error"]   = httpUpdate.getLastErrorString();
                String out; serializeJson(errDoc, out);
                sendMQTT(TOPIC_STATUS, out);
            }
            otaInProgress = false;
            break;

        case HTTP_UPDATE_NO_UPDATES:
            Serial.println("[OTA] No update available");
            otaInProgress = false;
            break;

        case HTTP_UPDATE_OK:
            // Sẽ tự reboot — code sau đây không chạy được
            // Nhưng ghi vào Preferences để sau reboot biết mình vừa OTA
            {
                Preferences ota_pref;
                bool ota_ok = ota_pref.begin("ota_state", false);
                if (!ota_ok) {
                    Serial.println("[OTA] WARNING: unable to open ota_state namespace for OTA success marker");
                } else {
                    ota_pref.putString("last_version", version);
                    ota_pref.putString("doc_id",       docId);
                    ota_pref.putBool("just_updated",   true);
                    ota_pref.end();
                }
            }
            Serial.println("[OTA] SUCCESS — Rebooting now!");
            // httpUpdate.update() sẽ tự gọi ESP.restart()
            break;
    }
}

// ================= SETUP =================
inline void setupHardware() {
    Serial.println("--- BEDROOM HARDWARE SETUP ---");

    esp_err_t err = nvs_flash_init();
    bool ota_restore_needed = false;
    String ota_restore_version = "";
    String ota_restore_doc_id = "";

    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        Preferences backup_pref;
        if (backup_pref.begin("ota_state", true)) {
            ota_restore_needed = backup_pref.getBool("just_updated", false);
            ota_restore_version = backup_pref.getString("last_version", "");
            ota_restore_doc_id = backup_pref.getString("doc_id", "");
            backup_pref.end();
        }

        nvs_flash_erase();
        nvs_flash_init();

        if (ota_restore_needed) {
            Preferences restore_pref;
            if (restore_pref.begin("ota_state", false)) {
                restore_pref.putBool("just_updated", true);
                restore_pref.putString("last_version", ota_restore_version);
                restore_pref.putString("doc_id", ota_restore_doc_id);
                restore_pref.end();
                Serial.println("[NVS] OTA state restored after erase");
            }
        }
    }

    // 1. Khôi phục trạng thái
    preferences.begin("bedroom_state", true); 
    fanState = preferences.getBool("fan", false);   
    lightState = preferences.getBool("light", false);
    preferences.end();
    
    // Kiểm tra nếu vừa OTA xong → gửi báo cáo về Gateway
    Preferences ota_pref;
    bool ota_ok = ota_pref.begin("ota_state", true);
    if (!ota_ok) {
        Serial.println("[OTA] WARNING: unable to open ota_state namespace");
    }
    bool justUpdated = ota_ok && ota_pref.getBool("just_updated", false);
    if (justUpdated) {
        currentFirmwareVersion = ota_pref.getString("last_version", "unknown");
        String docId           = ota_pref.getString("doc_id", "");
        ota_pref.end();

        // Xóa flag để không gửi lại lần sau
        ota_ok = ota_pref.begin("ota_state", false);
        if (!ota_ok) {
            Serial.println("[OTA] WARNING: unable to reopen ota_state namespace for ack cleanup");
        } else {
            ota_pref.putBool("just_updated", false);
            ota_pref.end();
        }

        Serial.printf("[OTA] Reboot after OTA! New version: %s\n",
                      currentFirmwareVersion.c_str());

        otaAckPending = true;
        otaAckDocId = docId;
        otaAckVersion = currentFirmwareVersion;
        otaAckStartMs = 0; // Start timer only after MQTT is actually connected
        Serial.printf("[OTA] Reboot after OTA! New version: %s, ack pending\n", currentFirmwareVersion.c_str());
    } else if (ota_ok) {
        ota_pref.end();
    }
    
    // 2. Cấu hình Output
    pinMode(PIN_RELAY_FAN, OUTPUT);
    pinMode(PIN_LIGHT, OUTPUT);
    digitalWrite(PIN_RELAY_FAN, fanState ? HIGH : LOW); 
    digitalWrite(PIN_LIGHT, lightState ? HIGH : LOW); 
    
    // 3. Tạo Task nút nhấn
    xTaskCreate(taskButtonMonitor, "ButtonTask", 2048, NULL, 1, NULL);
    Serial.println(" Button Task Started");

    // 4. Khởi động cảm biến
    Wire.begin(PIN_I2C_SDA, PIN_I2C_SCL);
    hdc1080.begin(0x40);
    if(ccs.begin()){ hasCCS = true; while(!ccs.available()); } 
    else { hasCCS = false; Serial.println("CCS811 skipped"); }
}

// ================= LOGIC LOOP (CÓ LOGIC BUFFER) =================
inline void loopHardware() {
    // A. Xử lý lưu Flash khi nút bấm thay đổi
    if (fanChanged) {
        fanChanged = false; saveState(); sendDeviceStatus(ID_FAN_BEDROOM, fanState);
    }
    if (lightChanged) {
        lightChanged = false; saveState(); sendDeviceStatus(ID_LIGHT_BEDROOM, lightState);
    }

    // B. Gửi dữ liệu cảm biến định kỳ (5s)
    static unsigned long lastSensorSend = 0;
    if (millis() - lastSensorSend > 5000) {
        lastSensorSend = millis();

        // 1. Đọc dữ liệu
        float t = hdc1080.readTemperature();
        float h = hdc1080.readHumidity();
        if (hasCCS && ccs.available() && !ccs.readData()) {
            valCO2 = ccs.geteCO2(); valTVOC = ccs.getTVOC(); ccs.setEnvironmentalData(h, t);
        }
        
        // 2. Đóng gói JSON
        StaticJsonDocument<256> doc;
        doc["temperature"] = isnan(t)?0:t; 
        doc["humidity"] = isnan(h)?0:h;
        doc["co2"] = valCO2; 
        doc["tvoc"] = valTVOC;
        String out; serializeJson(doc, out);

        // 3. Kiểm tra kết nối để quyết định Gửi hay Lưu
        if (client.connected()) {
            // [CÓ MẠNG]: Xả hàng tồn kho trước (nếu có)
            if (!sensorBuffer.empty()) {
                flushSensorBuffer();
            }
            // Gửi dữ liệu hiện tại
            sendMQTT(TOPIC_SENSORS, out);
        } 
        else {
            // [MẤT MẠNG]: Lưu vào RAM Buffer
            if (sensorBuffer.size() < MAX_BUFFER_SIZE) {
                sensorBuffer.push_back(out);
                Serial.printf("[OFFLINE] Buffered data. Size: %d/%d\n", sensorBuffer.size(), MAX_BUFFER_SIZE);
            } else {
                // Buffer đầy: Xóa cái cũ nhất (đầu tiên) để nhét cái mới vào
                sensorBuffer.erase(sensorBuffer.begin());
                sensorBuffer.push_back(out);
                Serial.println(" [OFFLINE] Buffer full, rotating...");
            }
        }
    }

    if (otaAckPending) {
        if (client.connected()) {
            if (otaAckStartMs == 0) {
                otaAckStartMs = millis();
                Serial.println("[OTA] MQTT ready, starting ACK timer");
            }
            if (millis() - otaAckStartMs > 2000) {
                Serial.println("[OTA] Sending reboot completion ack to Gateway");
                StaticJsonDocument<256> ackDoc;
                ackDoc["source"]  = "ota";
                ackDoc["event"]   = "ota_done";
                ackDoc["room_id"] = ROOM_BEDROOM;
                ackDoc["version"] = otaAckVersion;
                ackDoc["doc_id"]  = otaAckDocId;
                String out; serializeJson(ackDoc, out);
                sendMQTT(TOPIC_STATUS, out);
                otaAckPending = false;
                otaAckDocId = "";
                otaAckVersion = "";
                Serial.println("[OTA] ACK sent to Gateway");
            }
        } else if (otaAckStartMs != 0 && millis() - otaAckStartMs > OTA_ACK_TIMEOUT_MS) {
            Serial.println("[OTA] ACK pending timeout, clearing state");
            otaAckPending = false;
        }
    }

    // C. Heartbeat trạng thái (10s)
    static unsigned long lastStatusSync = 0;
    if (millis() - lastStatusSync > 10000) { 
        lastStatusSync = millis();
        if (client.connected()) {
             sendDeviceStatus(ID_FAN_BEDROOM, fanState);
             sendDeviceStatus(ID_LIGHT_BEDROOM, lightState);
        }
    }
}

// ================= XỬ LÝ LỆNH TỪ GATEWAY =================
inline void processCommand(String topic, String payload) {
    StaticJsonDocument<512> doc;
    DeserializationError error = deserializeJson(doc, payload);
    if (error) {
        Serial.printf("[CMD] Invalid JSON payload: %s\n", error.c_str());
        return;
    }

    String action = doc["action"] | "";
    String device = doc["device"] | "";
    bool state = (action == "turn_on");

    Serial.printf("CMD: %s -> %s\n", device.c_str(), action.c_str());

    if (action == "ota_update") {
        String url     = doc["url"]     | "";
        String version = doc["version"] | "unknown";
        String docId   = doc["doc_id"]  | "";

        if (url.length() > 0) {
            Serial.println("[CMD] OTA Update received!");
            struct OtaParams { String url; String ver; String docId; };
            OtaParams* p = new OtaParams{url, version, docId};

            xTaskCreate([](void* arg) {
                OtaParams* p = (OtaParams*)arg;
                performOTA(p->url, p->ver, p->docId);
                delete p;
                vTaskDelete(NULL);
            }, "OTATask", 8192, p, 5, NULL);
        } else {
            Serial.println("[CMD] ota_update missing url");
        }
    }
    else if (device == ID_FAN_BEDROOM && (action == "turn_on" || action == "turn_off")) {
        fanState = state;
        digitalWrite(PIN_RELAY_FAN, fanState ? HIGH : LOW);
        saveState();
        sendDeviceStatus(ID_FAN_BEDROOM, fanState);
    }
    else if (device == ID_LIGHT_BEDROOM && (action == "turn_on" || action == "turn_off")) {
        lightState = state;
        digitalWrite(PIN_LIGHT, lightState ? HIGH : LOW);
        saveState();
        sendDeviceStatus(ID_LIGHT_BEDROOM, lightState);
    }
    else {
        Serial.println("[CMD] Unknown device/action");
    }
}

#endif
