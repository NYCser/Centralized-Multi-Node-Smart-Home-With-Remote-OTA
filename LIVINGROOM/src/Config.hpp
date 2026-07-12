#ifndef CONFIG_HPP
#define CONFIG_HPP

// ================= 1. CẤU HÌNH MẠNG (HOTSPOT NỘI BỘ) =================
// Tên Hotspot Raspberry Pi
#define WIFI_SSID "SmartHome_Hub" 
#define WIFI_PASS ""      

// IP Tĩnh của Raspberry Pi (Gateway mặc định của Hotspot Linux)
#define MQTT_SERVER "10.42.0.1"    
#define MQTT_PORT 1883

// ================= 2. ĐỊNH DANH NODE & PHÒNG =================
#define DEVICE_NODE_ID "esp32_living_node"

// Tên phòng (Node này quản lý 2 khu vực)
#define ROOM_LIVING "living_room_01" 
#define ROOM_ENTRANCE "entrance_01"  

// ID thiết bị (Living Room)
#define ID_LIGHT_LIVING "light_lv_1"
#define ID_FAN_LIVING   "fan_lv_1"
// ID cho khóa cửa phòng khách (thiết bị điều khiển từ web)
#define ID_DOOR_LIVING  "door_lock_lv_1"
// ID cho khóa cửa lối vào / entrance panel
#define ID_DOOR_ENTRANCE "door_lock"

// ================= 3. PIN MAP  =================
// --- Living Room ---
#define PIN_LR_LED    27  // Đèn
#define PIN_LR_RELAY  14  // Quạt
#define PIN_LR_DHT    26  // DHT11

// Input Buttons
#define PIN_BTN_LR_LED 12
#define PIN_BTN_LR_FAN 13

// --- Entrance (Door + Auth) ---
#define PIN_DOOR_RELAY 25
#define PIN_RFID_SDA   5
#define PIN_RFID_RST   4
#define PIN_FP_RX      16
#define PIN_FP_TX      17

// RELAY LOGIC FOR DOOR
// true if relay closes the circuit when pin is LOW (common module behavior)
#define DOOR_RELAY_ACTIVE_LOW true

// --- LCD I2C ---
#define PIN_I2C_SDA    21
#define PIN_I2C_SCL    22
#define LCD_ADDR 0x27
#define LCD_COLS 16
#define LCD_ROWS 2

// ================= 4. MQTT TOPICS =================
// Subscribe (Lắng nghe lệnh cho cả 2 phòng)
const char* TOPIC_CMD_LIVING   = "home/" ROOM_LIVING "/command";
const char* TOPIC_CMD_ENTRANCE = "home/" ROOM_ENTRANCE "/command";

// Publish Living Room
const char* TOPIC_SENSORS_LR   = "home/" ROOM_LIVING "/sensors"; 
const char* TOPIC_STATUS_LR    = "home/" ROOM_LIVING "/status";  

// Publish Entrance
const char* TOPIC_STATUS_EN    = "home/" ROOM_ENTRANCE "/status"; 
const char* TOPIC_AUTH_EN      = "home/" ROOM_ENTRANCE "/auth";   
const char* TOPIC_ENROLL_EN    = "home/" ROOM_ENTRANCE "/enroll"; 

#endif