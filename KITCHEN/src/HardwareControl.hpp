#ifndef HARDWARE_CONTROL_HPP
#define HARDWARE_CONTROL_HPP

#include <Arduino.h>
#include <ArduinoJson.h>
#include <Wire.h>
#include <LiquidCrystal_I2C.h>
#include <DHT.h>
#include <Adafruit_INA219.h>
#include <Preferences.h>
#include <HTTPClient.h>
#include <vector> // [MỚI] Thư viện mảng động cho Buffer
#include "Config.hpp"
#include "NetworkManager.hpp"

// ================= ĐỐI TƯỢNG PHẦN CỨNG =================
LiquidCrystal_I2C lcd(LCD_ADDR, LCD_COLS, LCD_ROWS);
DHT dht(PIN_DHT, DHT11);
Adafruit_INA219 ina219;
Preferences preferences; 

// Biến cảm biến
float valTemp = 0;
float valHum = 0;
int valGas = 0;
float valPowerMW = 0;
bool isFire = false;
bool hasINA219 = false;

// Trạng thái thiết bị
bool fanState = false;   
bool lightState = false; 

// Biến Báo động
bool isAlarming = false; 
bool isMuted = false; 
unsigned long lastMuteTime = 0; 
bool sentAlert = false; 

// Timers không chặn
unsigned long lastSafetyCheck = 0;
unsigned long lastSensorSend = 0;
unsigned long lastStatusSync = 0;
unsigned long lastBuzzerToggle = 0;
bool buzzerState = false;

// [MỚI] Bộ đệm RAM lưu dữ liệu khi mất mạng
// Lưu tối đa 50 bản tin (khoảng 4-5 phút dữ liệu nếu gửi 5s/lần)
std::vector<String> sensorBuffer; 
const size_t MAX_BUFFER_SIZE = 50;

// OTA Update variables
volatile bool otaInProgress = false;
bool otaAckPending = false;
String otaAckDocId = "";
String otaAckVersion = "";
unsigned long otaAckStartMs = 0;
const unsigned long OTA_ACK_TIMEOUT_MS = 15000;
String currentFirmwareVersion = "2.0.0";  // Hardcode version hiện tại 

// ================= HÀM HELPER =================

void controlBuzzer(bool on) {
    #ifdef BUZZER_ACTIVE_LOW
        digitalWrite(PIN_BUZZER, on ? LOW : HIGH);
    #else
        digitalWrite(PIN_BUZZER, on ? HIGH : LOW);
    #endif
}

void sendDeviceStatus(String docId, bool isOn) {
    // Ưu tiên gửi ngay trạng thái thiết bị nếu có mạng
    if (client.connected()) {
        StaticJsonDocument<200> doc;
        doc["deviceId"] = docId;
        doc["isOn"] = isOn;
        String out; serializeJson(doc, out);
        sendMQTT(TOPIC_STATUS, out);
    }
}

void saveState() {
    preferences.begin("kitchen_state", false);
    preferences.putBool("fan", fanState);
    preferences.putBool("light", lightState);
    preferences.end();
}

void sendAlertOnce(String type, String msg) {
    // Cảnh báo khẩn cấp (Cháy/Gas) nên cố gửi ngay
    if (client.connected()) {
        StaticJsonDocument<256> doc;
        doc["type"] = type; 
        doc["message"] = msg;
        String out; serializeJson(doc, out);
        sendMQTT(TOPIC_ALERT, out);
    }
}

// [MỚI] Hàm xả bộ đệm (Gửi dữ liệu cũ lên Gateway)
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
        statusDoc["room_id"]  = ROOM_KITCHEN;
        statusDoc["version"]  = version;
        statusDoc["doc_id"]   = docId;
        String out; serializeJson(statusDoc, out);
        sendMQTT(TOPIC_STATUS, out);
    }

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
                errDoc["room_id"] = ROOM_KITCHEN;
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
                ota_pref.begin("ota_state", false);
                ota_pref.putString("last_version", version);
                ota_pref.putString("doc_id",       docId);
                ota_pref.putBool("just_updated",   true);
                ota_pref.end();
            }
            Serial.println("[OTA] SUCCESS — Rebooting now!");
            // httpUpdate.update() sẽ tự gọi ESP.restart()
            break;
    }
}

// ================= SETUP =================
inline void setupHardware() {
    Serial.println("--- KITCHEN HARDWARE SETUP ---");
    
    preferences.begin("kitchen_state", true); 
    fanState = preferences.getBool("fan", false);
    lightState = preferences.getBool("light", false); 
    preferences.end();

    // Kiểm tra nếu vừa OTA xong → gửi báo cáo về Gateway
    Preferences ota_pref;
    ota_pref.begin("ota_state", true);
    bool justUpdated = ota_pref.getBool("just_updated", false);
    if (justUpdated) {
        currentFirmwareVersion = ota_pref.getString("last_version", "unknown");
        String docId           = ota_pref.getString("doc_id", "");
        ota_pref.end();

        // Xóa flag để không gửi lại lần sau
        ota_pref.begin("ota_state", false);
        ota_pref.putBool("just_updated", false);
        ota_pref.end();

        Serial.printf("[OTA] Reboot after OTA! New version: %s\n",
                      currentFirmwareVersion.c_str());

        otaAckPending = true;
        otaAckDocId = docId;
        otaAckVersion = currentFirmwareVersion;
        otaAckStartMs = millis();
        Serial.printf("[OTA] Reboot after OTA! New version: %s, ack pending\n", currentFirmwareVersion.c_str());
    } else {
        ota_pref.end();
    }

    pinMode(PIN_RELAY_FAN, OUTPUT);
    pinMode(PIN_LIGHT, OUTPUT);
    pinMode(PIN_BUZZER, OUTPUT);
    controlBuzzer(false); 

    digitalWrite(PIN_RELAY_FAN, fanState ? HIGH : LOW);
    digitalWrite(PIN_LIGHT, lightState ? HIGH : LOW); 

    pinMode(PIN_GAS_MQ6, INPUT);
    pinMode(PIN_FIRE_D0, INPUT_PULLUP);

    Wire.begin(PIN_I2C_SDA, PIN_I2C_SCL);
    dht.begin();
    lcd.init(); lcd.backlight();
    
    if (!ina219.begin()) {
        Serial.println("INA219 not found");
        hasINA219 = false;
    } else { hasINA219 = true; }

    lcd.setCursor(0, 0); lcd.print("SMART KITCHEN");
    lcd.setCursor(0, 1); lcd.print("System Ready");
    delay(2000); lcd.clear();
}

// ================= LOGIC AN TOÀN (CHẠY NHANH) =================
void checkSafety() {
    valGas = analogRead(PIN_GAS_MQ6);
    isFire = (digitalRead(PIN_FIRE_D0) == LOW); 
    
    bool gasDetected = (valGas > GAS_THRESHOLD);
    bool danger = (isFire || gasDetected);

    // Logic Reset Mute
    if (isMuted && (millis() - lastMuteTime > 10000)) {
        isMuted = false;
        Serial.println("[INFO] Auto Unmute (Time limit reached)");
    }

    // Xử lý Báo động
    if (danger) {
        if (!isAlarming) Serial.println(" [ALARM] SYSTEM TRIGGERED!");
        
        isAlarming = true;
        
        if (!sentAlert) {
            if (isFire) Serial.println(" [ALARM] FIRE DETECTED at Digital Pin!");
            if (gasDetected) Serial.printf("[ALARM] GAS LEAK DETECTED! Level: %d > Threshold: %d\n", valGas, GAS_THRESHOLD);

            String msg = isFire ? "CHÁY TẠI BẾP!" : " RÒ RỈ KHÍ GAS!";
            String type = isFire ? "fire" : "gas";
            sendAlertOnce(type, msg);
            sentAlert = true;
            
            if (gasDetected && !fanState) {
                Serial.println("[AUTO] Turning on Fan for safety!");
                fanState = true;
                digitalWrite(PIN_RELAY_FAN, HIGH);
                saveState();
                sendDeviceStatus(ID_FAN_KITCHEN, true);
            }
        }

        // Logic còi
        if (!isMuted) {
            if (millis() - lastBuzzerToggle > 200) { 
                lastBuzzerToggle = millis();
                buzzerState = !buzzerState;
                controlBuzzer(buzzerState);
            }
        } else {
            controlBuzzer(false); 
        }

        // LCD
        lcd.setCursor(0, 0); 
        lcd.print(isFire ? "!! FIRE ALARM !!" : "!! GAS LEAK !!");
        lcd.setCursor(0, 1); 
        lcd.print(isMuted ? " (MUTED)      " : "  EVACUATE!   ");

    } else {
        // Hết nguy hiểm
        if (isAlarming) {
            Serial.println("[SAFE] Alarm Cleared. System Safe.");
            isAlarming = false;
            sentAlert = false;
            isMuted = false;
            controlBuzzer(false);
            lcd.clear();
            sendAlertOnce("system", "Nhà bếp đã an toàn.");
        }
    }
}

// ================= LOGIC MÔI TRƯỜNG (CHẠY CHẬM & CÓ BUFFER) =================
void handleEnvironment() {
    float t = dht.readTemperature();
    float h = dht.readHumidity();
    
    if (hasINA219) valPowerMW = ina219.getPower_mW();
    else valPowerMW = 0;

    if (isnan(t)) t = 0; 
    if (isnan(h)) h = 0;
    valTemp = t; valHum = h;

    // Cập nhật LCD (Local)
    if (!isAlarming) {
        lcd.setCursor(0, 0);
        lcd.printf("T:%.0fC H:%.0f%% G:%d ", valTemp, valHum, valGas);
        lcd.setCursor(0, 1);
        lcd.printf("L:%s F:%s     ", lightState ? "ON" : "OF", fanState ? "ON" : "OF");
    }

    // Đóng gói JSON
    StaticJsonDocument<300> doc;
    doc["temperature"] = valTemp; 
    doc["humidity"] = valHum;
    doc["gas"] = valGas;
    doc["power"] = valPowerMW;
    doc["fire_detected"] = isFire;
    
    String out; serializeJson(doc, out);

    // [LOGIC GỬI MẠNG HOẶC LƯU BUFFER]
    if (client.connected()) {
        // [CÓ MẠNG]: Xả hàng tồn kho trước (nếu có)
        if (!sensorBuffer.empty()) {
            flushSensorBuffer();
        }
        // Gửi dữ liệu hiện tại
        sendMQTT(TOPIC_SENSORS, out);
        
        Serial.printf("[DATA] Gas: %d | Fire: %d | Temp: %.1f\n", valGas, isFire, valTemp);
    } 
    else {
        // [MẤT MẠNG]: Lưu vào RAM Buffer
        if (sensorBuffer.size() < MAX_BUFFER_SIZE) {
            sensorBuffer.push_back(out);
            Serial.printf("[OFFLINE] Buffered data. Size: %d/%d\n", sensorBuffer.size(), MAX_BUFFER_SIZE);
        } else {
            // Buffer đầy: Xóa cái cũ nhất để nhét cái mới
            sensorBuffer.erase(sensorBuffer.begin());
            sensorBuffer.push_back(out);
            Serial.println("[OFFLINE] Buffer full, rotating...");
        }
    }
}

// ================= MAIN LOOP HARDWARE =================
inline void loopHardware() {
    if (millis() - lastSafetyCheck > 100) {
        lastSafetyCheck = millis();
        checkSafety();
    }

    if (millis() - lastSensorSend > 5000) {
        lastSensorSend = millis();
        handleEnvironment();
    }

    if (otaAckPending) {
        if (client.connected()) {
            Serial.println("[OTA] Sending reboot completion ack to Gateway");
            StaticJsonDocument<256> ackDoc;
            ackDoc["source"]  = "ota";
            ackDoc["event"]   = "ota_done";
            ackDoc["room_id"] = ROOM_KITCHEN;
            ackDoc["version"] = otaAckVersion;
            ackDoc["doc_id"]  = otaAckDocId;
            String out; serializeJson(ackDoc, out);
            sendMQTT(TOPIC_STATUS, out);
            otaAckPending = false;
            otaAckDocId = "";
            otaAckVersion = "";
            Serial.println("[OTA] ACK sent to Gateway");
        } else if (millis() - otaAckStartMs > OTA_ACK_TIMEOUT_MS) {
            Serial.println("[OTA] ACK pending timeout, clearing state");
            otaAckPending = false;
        }
    }

    if (millis() - lastStatusSync > 10000) {
        lastStatusSync = millis();
        if (client.connected()) {
            sendDeviceStatus(ID_FAN_KITCHEN, fanState);
            sendDeviceStatus(ID_LIGHT_KITCHEN, lightState);
        }
    }
}

// ================= XỬ LÝ LỆNH TỪ GATEWAY =================
inline void processCommand(String topic, String payload) {
    StaticJsonDocument<512> doc;
    DeserializationError error = deserializeJson(doc, payload);
    if (error) return;

    String action = doc["action"] | ""; 
    String device = doc["device"] | ""; 
    bool state = (action == "turn_on");

    Serial.printf("[CMD] %s -> %s\n", device.c_str(), action.c_str());

    if (action == "mute_alarm") {
        isMuted = true;
        lastMuteTime = millis(); 
        Serial.println("[CMD] ALARM MUTED by User");
        return; 
    }

    if (action == "unmute") {
        isMuted = false;
        Serial.println("[CMD] UNMUTE received from Gateway");
        return;
    }

    if (device == ID_FAN_KITCHEN) {
        fanState = state; 
        digitalWrite(PIN_RELAY_FAN, fanState ? HIGH : LOW);
        saveState();      
        sendDeviceStatus(ID_FAN_KITCHEN, fanState); 
    }
    else if (device == ID_LIGHT_KITCHEN) {
        lightState = state;
        digitalWrite(PIN_LIGHT, lightState ? HIGH : LOW);
        saveState();
        sendDeviceStatus(ID_LIGHT_KITCHEN, lightState);
    }
    else if (action == "ota_update") {
        String url     = doc["url"]     | "";
        String version = doc["version"] | "unknown";
        String docId   = doc["doc_id"]  | "";

        if (url.length() > 0) {
            Serial.println("[CMD] OTA Update received!");
            // Chạy OTA trong task riêng để không block loop
            struct OtaParams { String url; String ver; String docId; };
            OtaParams* p = new OtaParams{url, version, docId};

            xTaskCreate([](void* arg) {
                OtaParams* p = (OtaParams*)arg;
                performOTA(p->url, p->ver, p->docId);
                delete p;
                vTaskDelete(NULL);
            }, "OTATask", 8192, p, 5, NULL);
        }
    }
}

#endif
