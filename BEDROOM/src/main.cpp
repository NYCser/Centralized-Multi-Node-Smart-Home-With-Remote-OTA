#include <Arduino.h>
#include "Config.hpp"
#include "NetworkManager.hpp"
#include "HardwareControl.hpp"

// Callback khi nhận tin nhắn MQTT
void mqttCallback(char* topic, byte* payload, unsigned int length) {
    String message;
    for (unsigned int i = 0; i < length; i++) {
        message += (char)payload[i];
    }
    // Chuyển việc xử lý sang HardwareControl
    processCommand(String(topic), message);
}

void setup() {
    Serial.begin(115200);
    delay(1000);

    setupNetwork(mqttCallback);

    // 1. Setup phần cứng (Sensors, Pins, FreeRTOS Button Task)
    setupHardware();

    // 2. Đồng bộ dữ liệu cũ nếu có (nếu vừa mới OTA xong thì sẽ có dữ liệu cũ trong buffer)

    Serial.println(">>> BEDROOM NODE READY <<<");
}

void loop() {
    // 1. Duy trì kết nối WiFi + MQTT (non-blocking)
    maintainConnection();

    // 2. Chạy logic cảm biến, gửi dữ liệu, heartbeat
    loopHardware();
}
