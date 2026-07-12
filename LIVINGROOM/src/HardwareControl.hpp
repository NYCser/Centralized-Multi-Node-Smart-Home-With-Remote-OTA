#ifndef HARDWARE_CONTROL_HPP
#define HARDWARE_CONTROL_HPP

#include <Arduino.h>
#include <ArduinoJson.h>
#include <SPI.h>
#include <Wire.h>
#include <DHT.h>
#include <LiquidCrystal_I2C.h>
#include <MFRC522.h>
#include <Adafruit_Fingerprint.h>
#include <FS.h>
#include <SPIFFS.h>
#include <Preferences.h> 
#include <HTTPClient.h>
#include <vector>
#include "Config.hpp"
#include "NetworkManager.hpp" 

// ================= ĐỐI TƯỢNG PHẦN CỨNG =================
DHT dht(PIN_LR_DHT, DHT11);
LiquidCrystal_I2C lcd(LCD_ADDR, LCD_COLS, LCD_ROWS);
MFRC522 rfid(PIN_RFID_SDA, PIN_RFID_RST);
Adafruit_Fingerprint finger(&Serial2);
Preferences preferences;

// Trạng thái thiết bị Living Room
bool lrLedState = false;
bool lrFanState = false;
bool doorState = false;

// Bộ đệm RAM lưu dữ liệu khi mất mạng
// Lưu tối đa 50 bản tin (khoảng 4-5 phút dữ liệu nếu gửi 5s/lần)
std::vector<String> sensorBuffer; 
const size_t MAX_BUFFER_SIZE = 50; 

// Nút bấm
struct Button {
    uint8_t pin;
    int state;
    int lastState;
    unsigned long lastDebounce;
    void init(uint8_t p) { pin=p; pinMode(pin, INPUT_PULLUP); state=HIGH; lastState=HIGH; }
    bool isPressed() {
        int reading = digitalRead(pin);
        if (reading != lastState) lastDebounce = millis();
        lastState = reading;
        if ((millis() - lastDebounce) > 50) {
            if (reading != state) { state = reading; if (state == LOW) return true; }
        }
        return false;
    }
} btnLed, btnFan;

// Quản lý User
struct UserCredential { String uid; int fp_id; };
std::vector<UserCredential> users;

// Timers
unsigned long lastSensorSend = 0;
unsigned long lastStatusSync = 0;
unsigned long doorOpenTime = 0;
unsigned long msgTimeout = 0;

// OTA Update variables
volatile bool otaInProgress = false;
bool otaAckPending = false;
String otaAckDocId = "";
String otaAckVersion = "";
String otaPendingVersion = "";
String otaPendingDocId = "";
unsigned long otaAckStartMs = 0;
const unsigned long OTA_ACK_TIMEOUT_MS = 15000;
String currentFirmwareVersion = "2.0.0";  // Hardcode version hiện tại

// State Machine
enum SysState { IDLE, DOOR_OPEN, SHOW_MSG, ENROLL_WAIT_RFID, ENROLL_WAIT_FP1, ENROLL_WAIT_REMOVE, ENROLL_WAIT_FP2 };
SysState currentState = IDLE;
String pendingUid = "";
int pendingFpId = -1;

// ================= HÀM HELPER DATABASE =================
void loadUsers() {
    if (!SPIFFS.exists("/users.json")) {
        Serial.println(" [DB] User database not found, creating new.");
        return;
    }
    File f = SPIFFS.open("/users.json", "r");
    DynamicJsonDocument doc(8192);
    deserializeJson(doc, f);
    users.clear();
    for (JsonObject o : doc.as<JsonArray>()) users.push_back({o["uid"].as<String>(), o["fp_id"]});
    f.close();
    Serial.printf(" [DB] Loaded %d users from SPIFFS\n", users.size());
}

void saveUsers() {
    File f = SPIFFS.open("/users.json", "w");
    DynamicJsonDocument doc(8192);
    for (auto &u : users) { JsonObject o = doc.createNestedObject(); o["uid"] = u.uid; o["fp_id"] = u.fp_id; }
    serializeJson(doc, f); f.close();
    Serial.println(" [DB] Users saved to SPIFFS");
}

bool userExists(String uid) { for(auto &u:users) if(u.uid == uid) return true; return false; }
String getUidByFp(int fid) { for(auto &u:users) if(u.fp_id == fid) return u.uid; return ""; }
int getNextFpId() { for(int i=1; i<=127; i++) { bool used=false; for(auto &u:users) if(u.fp_id==i) used=true; if(!used) return i; } return -1; }

// ================= HÀM GIAO DIỆN & ĐIỀU KHIỂN =================
void showMsg(String l1, String l2, int timeout = 0) {
    lcd.clear(); lcd.setCursor(0,0); lcd.print(l1); lcd.setCursor(0,1); lcd.print(l2);
    if(timeout > 0) { currentState = SHOW_MSG; msgTimeout = millis() + timeout; }
}

void resetToIdle() {
    currentState = IDLE;
    showMsg("System Ready", "Scan Card/Finger");
}

void setDoorRelay(bool open) {
    bool level = DOOR_RELAY_ACTIVE_LOW ? (open ? LOW : HIGH) : (open ? HIGH : LOW);
    digitalWrite(PIN_DOOR_RELAY, level);
    Serial.printf(" [DOOR] RELAY %s -> %s\n", open ? "OPEN" : "CLOSE", level == LOW ? "LOW" : "HIGH");
}

String getPayloadDevice(JsonDocument &doc) {
    if (doc.containsKey("device")) return doc["device"].as<String>();
    if (doc.containsKey("deviceId")) return doc["deviceId"].as<String>();
    if (doc.containsKey("device_id")) return doc["device_id"].as<String>();
    return String("");
}

String getPayloadAction(JsonDocument &doc) {
    if (doc.containsKey("action")) return doc["action"].as<String>();
    if (doc.containsKey("command")) return doc["command"].as<String>();
    return String("");
}

bool isDoorDevice(const String &device) {
    String id = device;
    id.toLowerCase();
    return id == ID_DOOR_LIVING || id == "door_lock" || id.endsWith("door") || id.indexOf("door") >= 0;
}

void sendStatus(String id, bool state, const char* topic = TOPIC_STATUS_LR) {
    if (!client.connected()) {
        Serial.println(" [STATUS] MQTT disconnected, skip status publish");
        return;
    }
    StaticJsonDocument<200> doc;
    doc["device"] = id;
    doc["deviceId"] = id;
    doc["device_id"] = id;
    doc["isOn"] = state;
    if (id == ID_DOOR_LIVING || isDoorDevice(id)) {
        doc["type"] = "door";
        doc["name"] = "Door";
    } else if (id == ID_LIGHT_LIVING) {
        doc["type"] = "light";
        doc["name"] = "Light";
    } else if (id == ID_FAN_LIVING) {
        doc["type"] = "fan";
        doc["name"] = "Fan";
    }
    String out; serializeJson(doc, out);
    Serial.printf(" [STATUS] Publishing %s=%s to %s\n", id.c_str(), state ? "true" : "false", topic);
    sendMQTT(topic, out);
}

// Hàm xả bộ đệm (Gửi dữ liệu cũ lên Gateway)
void flushSensorBuffer() {
    if (sensorBuffer.empty()) return;

    Serial.printf("[SYNC] Đang đồng bộ %d bản tin cũ...\n", sensorBuffer.size());
    
    // Gửi lần lượt các bản tin cũ
    for (const String& payload : sensorBuffer) {
        // Lưu ý: Topic cảm biến phòng khách phải khớp với Config
        // Giả sử Config định nghĩa TOPIC_SENSORS_LR cho phòng khách
        #ifdef TOPIC_SENSORS_LR 
            sendMQTT(TOPIC_SENSORS_LR, payload);
        #else
            sendMQTT("home/living_room_01/sensors", payload);
        #endif
        delay(50); // Delay nhẹ để tránh nghẽn mạng
    }
    
    // Xóa sạch bộ đệm sau khi gửi xong
    sensorBuffer.clear();
    Serial.println(" [SYNC] Đã đồng bộ xong!");
}

void openDoor(String method, String uid) {
    setDoorRelay(true);
    doorState = true;
    sendStatus(ID_DOOR_ENTRANCE, true, TOPIC_STATUS_EN);
    sendStatus(ID_DOOR_LIVING, true); // Preserve living room status sync too
    showMsg("Access Granted", "Welcome " + uid);
    currentState = DOOR_OPEN; 
    doorOpenTime = millis();
    
    // [DEBUG]
    Serial.printf(" [DOOR] OPEN by %s (User: %s)\n", method.c_str(), uid.c_str());
    
    // Gửi sự kiện mở cửa — gồm "method" để Pi biết rfid hay finger
    if(client.connected()) {
        StaticJsonDocument<256> doc;
        doc["cardUid"] = uid;
        doc["action"]  = "entry";
        doc["success"] = true;
        doc["method"]  = method;   // [FIX-A] "rfid" | "finger" | "remote"
        String out; serializeJson(doc, out);
        sendMQTT(TOPIC_AUTH_EN, out);
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
        statusDoc["room_id"]  = ROOM_LIVING;
        statusDoc["version"]  = version;
        statusDoc["doc_id"]   = docId;
        String out; serializeJson(statusDoc, out);
        sendMQTT(TOPIC_STATUS_LR, out);
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
                errDoc["room_id"] = ROOM_LIVING;
                errDoc["version"] = version;
                errDoc["doc_id"]  = docId;
                errDoc["error"]   = httpUpdate.getLastErrorString();
                String out; serializeJson(errDoc, out);
                sendMQTT(TOPIC_STATUS_LR, out);
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
    Serial.begin(115200);
    Serial.println("--- LIVING ROOM HARDWARE SETUP ---");

    if(!SPIFFS.begin(true)){ Serial.println(" SPIFFS Mount Failed"); }
    loadUsers();
    
    preferences.begin("living_state", true);
    lrLedState = preferences.getBool("led", false);
    lrFanState = preferences.getBool("fan", false);
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
        otaAckStartMs = 0; // Start timer only after MQTT is connected
        Serial.printf("[OTA] Reboot after OTA! New version: %s, ack pending\n", currentFirmwareVersion.c_str());
    } else {
        ota_pref.end();
    }

    pinMode(PIN_LR_LED, OUTPUT); digitalWrite(PIN_LR_LED, lrLedState ? HIGH : LOW);
    pinMode(PIN_LR_RELAY, OUTPUT); digitalWrite(PIN_LR_RELAY, lrFanState ? HIGH : LOW);
    pinMode(PIN_DOOR_RELAY, OUTPUT);
    setDoorRelay(false);
    doorState = false;

    btnLed.init(PIN_BTN_LR_LED);
    btnFan.init(PIN_BTN_LR_FAN);

    Wire.begin(PIN_I2C_SDA, PIN_I2C_SCL);
    dht.begin();
    lcd.init(); lcd.backlight();
    
    SPI.begin(); 
    rfid.PCD_Init();
    
    Serial2.begin(57600, SERIAL_8N1, PIN_FP_RX, PIN_FP_TX);
    if(finger.verifyPassword()) Serial.println("Fingerprint Sensor Found");
    else Serial.println("Fingerprint Sensor NOT FOUND");
    
    resetToIdle();
    Serial.println(" Hardware Ready");
}

// ================= LOOP LOGIC =================
inline void loopHardware() {
    // 1. Nút bấm vật lý
    if (btnLed.isPressed()) {
        lrLedState = !lrLedState;
        digitalWrite(PIN_LR_LED, lrLedState);
        preferences.begin("living_state", false); preferences.putBool("led", lrLedState); preferences.end();
        sendStatus(ID_LIGHT_LIVING, lrLedState);
        Serial.printf("[MANUAL] LED Button Pressed -> %s\n", lrLedState ? "ON" : "OFF");
    }
    if (btnFan.isPressed()) {
        lrFanState = !lrFanState;
        digitalWrite(PIN_LR_RELAY, lrFanState);
        preferences.begin("living_state", false); preferences.putBool("fan", lrFanState); preferences.end();
        sendStatus(ID_FAN_LIVING, lrFanState);
        Serial.printf(" [MANUAL] Fan Button Pressed -> %s\n", lrFanState ? "ON" : "OFF");
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
                ackDoc["room_id"] = ROOM_LIVING;
                ackDoc["version"] = otaAckVersion;
                ackDoc["doc_id"]  = otaAckDocId;
                String out; serializeJson(ackDoc, out);
                sendMQTT(TOPIC_STATUS_LR, out);
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

    // 2. State Machine (Cửa & Đăng ký - GIỮ NGUYÊN)
    if (currentState == IDLE) {
        // Quét thẻ
        if (rfid.PICC_IsNewCardPresent() && rfid.PICC_ReadCardSerial()) {
            String uid = "";
            for (byte i=0; i<rfid.uid.size; i++) uid += String(rfid.uid.uidByte[i] < 0x10 ? "0" : "") + String(rfid.uid.uidByte[i], HEX);
            rfid.PICC_HaltA(); rfid.PCD_StopCrypto1();
            
            Serial.println(" [RFID] Card Scanned: " + uid);
            
            if (userExists(uid)) openDoor("rfid", uid);
            else { 
                Serial.println(" [ACCESS] Denied (Invalid Card)");
                showMsg("Access Denied", "Invalid Card", 2000); 
                StaticJsonDocument<200> doc; doc["success"]=false; doc["cardUid"]=uid; String out; serializeJson(doc,out); sendMQTT(TOPIC_AUTH_EN, out);
            }
        }
        // Quét vân tay
        else if (finger.getImage() == FINGERPRINT_OK) {
            if (finger.image2Tz() == FINGERPRINT_OK && finger.fingerFastSearch() == FINGERPRINT_OK) {
                String uid = getUidByFp(finger.fingerID);
                Serial.printf(" [FINGER] Matched ID #%d -> UID: %s\n", finger.fingerID, uid.c_str());
                if(uid != "") openDoor("finger", uid);
            } else {
                Serial.println(" [ACCESS] Denied (Finger not found)");
                showMsg("Access Denied", "Bad Finger", 2000);
            }
        }
    } 
    else if (currentState == DOOR_OPEN) {
        if (millis() - doorOpenTime > 5000) { 
            setDoorRelay(false);
            doorState = false;
            sendStatus(ID_DOOR_ENTRANCE, false, TOPIC_STATUS_EN);
            sendStatus(ID_DOOR_LIVING, false);
            Serial.println(" [DOOR] Closed");
            resetToIdle();
        }
    }
    else if (currentState == SHOW_MSG) {
        if (millis() > msgTimeout) resetToIdle();
    }
    else if (currentState == ENROLL_WAIT_RFID) {
        if (rfid.PICC_IsNewCardPresent() && rfid.PICC_ReadCardSerial()) {
            pendingUid = "";
            for (byte i=0; i<rfid.uid.size; i++) pendingUid += String(rfid.uid.uidByte[i] < 0x10 ? "0" : "") + String(rfid.uid.uidByte[i], HEX);
            rfid.PICC_HaltA(); rfid.PCD_StopCrypto1();
            
            Serial.println(" [ENROLL] Card Scanned: " + pendingUid);
            
            if (userExists(pendingUid)) {
                Serial.println(" [ENROLL] Card already exists!");
                showMsg("Error", "Card Exists!", 2000);
            } else {
                pendingFpId = getNextFpId();
                if(pendingFpId == -1) showMsg("Error", "Mem Full", 2000);
                else {
                    currentState = ENROLL_WAIT_FP1;
                    Serial.printf(" [ENROLL] Waiting for Finger 1 (ID: %d)\n", pendingFpId);
                    showMsg("Place Finger", "ID: " + String(pendingFpId));
                }
            }
        }
    }
    else if (currentState == ENROLL_WAIT_FP1) {
        if (finger.getImage() == FINGERPRINT_OK && finger.image2Tz(1) == FINGERPRINT_OK) {
            Serial.println(" [ENROLL] Finger 1 OK. Remove finger.");
            showMsg("Remove Finger", "Wait...");
            currentState = ENROLL_WAIT_REMOVE;
        }
    }
    else if (currentState == ENROLL_WAIT_REMOVE) {
        if (finger.getImage() == FINGERPRINT_NOFINGER) {
            Serial.println(" [ENROLL] Waiting for Finger 2...");
            showMsg("Place Again", "Confirm");
            currentState = ENROLL_WAIT_FP2;
        }
    }
    else if (currentState == ENROLL_WAIT_FP2) {
        if (finger.getImage() == FINGERPRINT_OK && finger.image2Tz(2) == FINGERPRINT_OK) {
            if (finger.createModel() == FINGERPRINT_OK && finger.storeModel(pendingFpId) == FINGERPRINT_OK) {
                users.push_back({pendingUid, pendingFpId});
                saveUsers();
                
                Serial.println(" [ENROLL] Success! User Saved.");
                
                StaticJsonDocument<200> doc; doc["uid"]=pendingUid; doc["status"]="success";
                String out; serializeJson(doc,out); sendMQTT(TOPIC_ENROLL_EN, out);
                showMsg("Enroll Success", "Saved!", 2000);
            } else {
                Serial.println(" [ENROLL] Store Failed (Mismatch)");
                showMsg("Error", "Store Failed", 2000);
            }
        }
    }

    // 3. Sensor Update (5s) - [ĐÃ CẬP NHẬT LOGIC BUFFER]
    if (millis() - lastSensorSend > 5000) {
        lastSensorSend = millis();
        float t = dht.readTemperature();
        float h = dht.readHumidity();
        if(!isnan(t)) {
            StaticJsonDocument<200> doc; 
            doc["temperature"]=t; 
            doc["humidity"]=h;
            String out; serializeJson(doc,out);
            
            // Logic Buffer
            if (client.connected()) {
                // Có mạng -> Gửi dữ liệu tồn (nếu có) + Dữ liệu mới
                if (!sensorBuffer.empty()) flushSensorBuffer();
                
                #ifdef TOPIC_SENSORS_LR
                    sendMQTT(TOPIC_SENSORS_LR, out);
                #else
                    sendMQTT("home/living_room_01/sensors", out);
                #endif

                Serial.printf(" [DATA] Temp: %.1f | Hum: %.1f\n", t, h);
            } else {
                // Mất mạng -> Lưu vào Buffer
                if (sensorBuffer.size() < MAX_BUFFER_SIZE) {
                    sensorBuffer.push_back(out);
                    Serial.printf(" [OFFLINE] Buffered. Size: %d\n", sensorBuffer.size());
                } else {
                    sensorBuffer.erase(sensorBuffer.begin());
                    sensorBuffer.push_back(out);
                    Serial.println(" [OFFLINE] Buffer full, rotating...");
                }
            }
        }
    }

    // 4. Status Sync (10s)
    if (millis() - lastStatusSync > 10000) {
        lastStatusSync = millis();
        // Chỉ gửi Status nếu có mạng (ưu tiên hiển thị thực tế)
        if (client.connected()) {
            sendStatus(ID_LIGHT_LIVING, lrLedState);
            sendStatus(ID_FAN_LIVING, lrFanState);
            sendStatus(ID_DOOR_LIVING, doorState);
            sendStatus(ID_DOOR_ENTRANCE, doorState, TOPIC_STATUS_EN);
        }
    }
}

// ================= XỬ LÝ LỆNH ONLINE =================
inline void processCommand(String topic, String payload) {
    StaticJsonDocument<512> doc;
    DeserializationError error = deserializeJson(doc, payload);
    if (error) return;

String device = getPayloadDevice(doc);
    String action = getPayloadAction(doc);

    Serial.printf(" [CMD] topic=%s payload=%s\n", topic.c_str(), payload.c_str());
    Serial.printf(" [CMD] parsed device=%s action=%s\n", device.c_str(), action.c_str());

    // Lệnh cho Phòng Khách
    if (topic == TOPIC_CMD_LIVING) {
        bool state = (action == "turn_on");
        if (device == ID_LIGHT_LIVING || device == "light_lv_1") {
            lrLedState = state;
            digitalWrite(PIN_LR_LED, state);
            preferences.begin("living_state", false); preferences.putBool("led", state); preferences.end();
            sendStatus(ID_LIGHT_LIVING, state);
        }
        else if (device == ID_FAN_LIVING || device == "fan_lv_1") {
            lrFanState = state;
            digitalWrite(PIN_LR_RELAY, state);
            preferences.begin("living_state", false); preferences.putBool("fan", state); preferences.end();
            sendStatus(ID_FAN_LIVING, state);
        }
        else if (device == ID_DOOR_LIVING || device == "door_lock" || device == "door_lock_lv_1" || isDoorDevice(device)) {
            // Các action có thể là "turn_on"/"turn_off" hoặc "open"/"close"
            if (action == "turn_on" || action == "open" || action == "unlock" || action == "open_door") {
                setDoorRelay(true);
                doorState = true;
                currentState = DOOR_OPEN;
                doorOpenTime = millis();
                sendStatus(ID_DOOR_ENTRANCE, true, TOPIC_STATUS_EN);
                sendStatus(ID_DOOR_LIVING, true);
                Serial.println(" [CMD] Remote door OPEN (living room)");
            } else if (action == "turn_off" || action == "close" || action == "lock" || action == "close_door") {
                setDoorRelay(false);
                doorState = false;
                sendStatus(ID_DOOR_ENTRANCE, false, TOPIC_STATUS_EN);
                sendStatus(ID_DOOR_LIVING, false);
                resetToIdle();
                Serial.println(" [CMD] Remote door CLOSED (living room)");
            } else if (action == "") {
                Serial.println(" [CMD] Door command missing action, checking payload fallback");
                if (device.equalsIgnoreCase("door_lock_lv_1") || device.equalsIgnoreCase("door_lock")) {
                    setDoorRelay(true);
                    doorState = true;
                    currentState = DOOR_OPEN;
                    doorOpenTime = millis();
                    sendStatus(ID_DOOR_ENTRANCE, true, TOPIC_STATUS_EN);
                    sendStatus(ID_DOOR_LIVING, true);
                    Serial.println(" [CMD] Remote door OPEN by fallback");
                }
            } else {
                Serial.printf(" [CMD] Unknown door action: %s\n", action.c_str());
            }
        }
        else if (action == "ota_update") {
            // OTA command routed to living room node
            String url     = doc["url"]     | "";
            String version = doc["version"] | "unknown";
            String docId   = doc["doc_id"]  | "";

            if (url.length() > 0) {
                Serial.println("[CMD] OTA Update (living) received!");
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
    // Lệnh cho Lối Vào (Cửa/Đăng ký)
    else if (topic == TOPIC_CMD_ENTRANCE) {
        if (action == "turn_on" || action == "open" || action == "open_door" || action == "unlock") {
            openDoor("remote", "admin");
        }
        else if (action == "turn_off" || action == "close" || action == "close_door" || action == "lock") {
            setDoorRelay(false);
            doorState = false;
            sendStatus(ID_DOOR_ENTRANCE, false, TOPIC_STATUS_EN);
            resetToIdle();
            Serial.println(" [CMD] Remote door CLOSED (entrance)");
        }
        else if (action == "enroll") {
            currentState = ENROLL_WAIT_RFID;
            Serial.println(" [CMD] Start Enrollment Mode");
            showMsg("Enroll Mode", "Scan New Card");
        }
        else if (action == "delete_user") {
            String uid = doc["uid"];
            Serial.println(" [CMD] Delete User: " + uid);
            int fpDel = -1;
            for(auto it=users.begin(); it!=users.end(); ) { 
                if(it->uid == uid) { fpDel = it->fp_id; it = users.erase(it); } else ++it; 
            }
            if(fpDel != -1) { 
                finger.deleteModel(fpDel); saveUsers(); 
                showMsg("User Deleted", uid, 2000); 
                Serial.println(" [CMD] Deleted Successfully");
                // [FIX-C] Gửi xác nhận xóa về Pi
                if(client.connected()) {
                    StaticJsonDocument<200> ack;
                    ack["event"]  = "user_deleted";
                    ack["uid"]    = uid;
                    ack["success"] = true;
                    String out; serializeJson(ack, out);
                    sendMQTT(TOPIC_AUTH_EN, out);
                }
            } else {
                Serial.println(" [CMD] Delete: UID not found in SPIFFS");
            }
        }
        // [FIX-C] Xóa toàn bộ users trong SPIFFS — dùng khi cần reset
        else if (action == "clear_all_users") {
            Serial.println(" [CMD] Clearing ALL users from SPIFFS...");
            // Xóa toàn bộ fingerprint models
            for(auto &u : users) {
                if(u.fp_id > 0) {
                    finger.deleteModel(u.fp_id);
                    Serial.printf(" [CMD] Deleted FP model #%d\n", u.fp_id);
                }
            }
            users.clear();
            saveUsers();
            showMsg("All Users", "Cleared!", 2000);
            Serial.println(" [CMD] All users cleared from SPIFFS & Sensor");
            // Gửi xác nhận về Pi
            if(client.connected()) {
                StaticJsonDocument<200> ack;
                ack["event"]   = "all_users_cleared";
                ack["success"] = true;
                String out; serializeJson(ack, out);
                sendMQTT(TOPIC_AUTH_EN, out);
            }
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
}

#endif
