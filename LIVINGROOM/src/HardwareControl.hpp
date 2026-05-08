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

void sendStatus(String id, bool state) {
    if(!client.connected()) return;
    StaticJsonDocument<200> doc;
    doc["deviceId"] = id; doc["isOn"] = state;
    String out; serializeJson(doc, out);
    sendMQTT(TOPIC_STATUS_LR, out);
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
    digitalWrite(PIN_DOOR_RELAY, HIGH);
    showMsg("Access Granted", "Welcome " + uid);
    currentState = DOOR_OPEN; 
    doorOpenTime = millis();
    
    // [DEBUG]
    Serial.printf(" [DOOR] OPEN by %s (User: %s)\n", method.c_str(), uid.c_str());
    
    // Gửi sự kiện mở cửa (Quan trọng nên cố gửi ngay, không buffer log này)
    if(client.connected()) {
        StaticJsonDocument<256> doc;
        doc["cardUid"] = uid; doc["action"] = "entry"; doc["success"] = true;
        String out; serializeJson(doc, out);
        sendMQTT(TOPIC_AUTH_EN, out);
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

    pinMode(PIN_LR_LED, OUTPUT); digitalWrite(PIN_LR_LED, lrLedState ? HIGH : LOW);
    pinMode(PIN_LR_RELAY, OUTPUT); digitalWrite(PIN_LR_RELAY, lrFanState ? HIGH : LOW);
    pinMode(PIN_DOOR_RELAY, OUTPUT); digitalWrite(PIN_DOOR_RELAY, LOW);

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
            digitalWrite(PIN_DOOR_RELAY, LOW);
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
        }
    }
}

// ================= XỬ LÝ LỆNH ONLINE =================
inline void processCommand(String topic, String payload) {
    StaticJsonDocument<512> doc;
    DeserializationError error = deserializeJson(doc, payload);
    if (error) return;

    String device = doc["device"]; 
    String action = doc["action"]; 

    Serial.printf(" [CMD] %s -> %s\n", device.c_str(), action.c_str());

    // Lệnh cho Phòng Khách
    if (topic == TOPIC_CMD_LIVING) {
        bool state = (action == "turn_on");
        if (device == ID_LIGHT_LIVING) {
            lrLedState = state;
            digitalWrite(PIN_LR_LED, state);
            preferences.begin("living_state", false); preferences.putBool("led", state); preferences.end();
            sendStatus(ID_LIGHT_LIVING, state);
        }
        else if (device == ID_FAN_LIVING) {
            lrFanState = state;
            digitalWrite(PIN_LR_RELAY, state);
            preferences.begin("living_state", false); preferences.putBool("fan", state); preferences.end();
            sendStatus(ID_FAN_LIVING, state);
        }
    }
    // Lệnh cho Lối Vào (Cửa/Đăng ký)
    else if (topic == TOPIC_CMD_ENTRANCE) {
        if (action == "open" || action == "open_door") openDoor("remote", "admin");
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
            }
        }
    }
}

#endif
