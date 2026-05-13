#ifndef CONFIG_HPP
#define CONFIG_HPP

// ================= 1. CẤU HÌNH MẠNG (HOTSPOT NỘI BỘ) =================
// Tên Hotspot Raspberry Pi
#define WIFI_SSID "SmartHome_Hub" 
#define WIFI_PASS ""      

// IP Tĩnh của Raspberry Pi (Gateway mặc định của Hotspot Linux)
#define MQTT_SERVER "10.42.0.1"    
#define MQTT_PORT 1883

// ================= 2. ĐỊNH DANH NODE =================
#define DEVICE_NODE_ID "esp32_kitchen_node"
#define ROOM_KITCHEN "kitchen_01"

// ID thiết bị trên Firestore
#define ID_FAN_KITCHEN   "fan_kt_1"   
#define ID_LIGHT_KITCHEN "light_kt_1" 

// ================= 3. PIN MAP (PHẦN CỨNG BẾP) =================
// Output
#define PIN_RELAY_FAN 17  // Relay Quạt bếp
#define PIN_LIGHT     5   // Đèn bếp
#define PIN_BUZZER    32  // Còi báo động

// Input Sensors
#define PIN_GAS_MQ6   34  // Analog Input
#define PIN_FIRE_D0   25  // Digital Input
#define PIN_DHT       4   // DHT11/22

// I2C cho LCD
#define PIN_I2C_SDA   21
#define PIN_I2C_SCL   22

// Cấu hình LCD
// #define LCD_ADDR 0x27
// #define LCD_COLS 16
// #define LCD_ROWS 2

// Ngưỡng cảnh báo Gas (Giữ nguyên tùy chỉnh của bạn)
#define GAS_THRESHOLD 1500

// Cấu hình Buzzer (True nếu còi kêu ở mức thấp - Low Trigger)
#define BUZZER_ACTIVE_LOW true 

// Cấu hình LCD
#define LCD_ADDR 0x27
#define LCD_COLS 16
#define LCD_ROWS 2

// ================= 4. MQTT TOPICS =================
const char* TOPIC_CMD = "home/" ROOM_KITCHEN "/command";
const char* TOPIC_SENSORS = "home/" ROOM_KITCHEN "/sensors"; 
const char* TOPIC_STATUS  = "home/" ROOM_KITCHEN "/status";  
const char* TOPIC_ALERT   = "home/" ROOM_KITCHEN "/alert";   

#endif