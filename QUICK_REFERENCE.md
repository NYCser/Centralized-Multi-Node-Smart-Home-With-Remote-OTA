# ⚡ Quick Reference Card - SmartHome System

*Tham chiếu nhanh các log, lệnh, và cách fix*

---

## 🟢 NORMAL OPERATION LOGS

```raw
[MAIN] Gateway LIVE → http://0.0.0.0:5000/          ✅ Gateway sẵn sàng
[BUS] MQTT connected & subscribed                   ✅ MQTT OK
[AUTO] Cache loaded: 3 rules, 8 schedules           ✅ Automation/schedule load OK
[WATCHDOG] Safety watchdog started                  ✅ Cảnh báo gas OK
[NET] Dual interface detected: Hotspot=wlan0        ✅ WiFi OK
[DISPATCH] SENT [manual|p=1]: fan_kt_1 → turn_off   ✅ Lệnh sent thành công
[DISPATCH] SENT [automation|p=3]: light → turn_on   ✅ Automation chạy OK
[SCHEDULER] Executed: fan_kt_1 → turn_on            ✅ Schedule chạy OK
```

---

## 🟡 WARNING/INFO LOGS (Có Thể Ignore)

```raw
[DISPATCH] BLOCKED (manual_override): schedule → fan_kt_1
  → Manual mode active, schedule bị chặn 5 phút (BÌNH THƯỜNG)

[DISPATCH] MANUAL_STATE TTL expired for fan_kt_1 → auto mode restored
  → TTL hết, quay lại auto mode (BÌNH THƯỜNG)

[AUTO] Schedules reloaded: 11
  → Reload schedule từ Firebase (BÌNH THƯỜNG, mỗi 30s)

[WATCHDOG] kitchen_01 smart_muted (alert read)
  → Người dùng đọc alert, buzzer tắt 3 phút (BÌNH THƯỜNG)

[NET] WiFi switch: uplink unavailable, using hotspot
  → WiFi chính mất, chuyển sang hotspot (OK, tự động)
```

---

## 🔴 ERROR/CRITICAL LOGS (Cần Chú Ý)

```raw
[WATCHDOG] DANGER DETECTED kitchen_01: RÒ RỈ KHÍ GAS! 659 ppm
  ⚠️ Gas vượt 600 ppm
  ✅ Tự động: Bật fan, phát buzzer
  → Chờ gas hạ, alert sẽ tự tắt

[DISPATCH] BLOCKED (safety_lock): schedule → kitchen_01/fan_kt_1
  ⚠️ Safety lock active (có cảnh báo)
  → Schedule/automation tạm dừng
  ✅ Tự động hết sau 5 phút hoặc khi gas hạ

[DISPATCH] BLOCKED (schedule_active_60m): automation → device_id
  ⚠️ Schedule active trong vòng 60 phút
  → Automation bị chặn để tránh xung đột
  → Tự động hết sau 60 phút

[Errno 111] Connection refused
  ⚠️ Không kết nối được Redis/MQTT
  🔧 FIX: systemctl restart redis-server
```

---

## 🔧 EMERGENCY FIX COMMANDS

### 1. Reset Manual State (Ngay Lập Tức)

```bash
# Via API
curl -X POST http://192.168.1.131:5000/api/devices/fan_kt_1/set_auto_mode

# Via Redis
redis-cli FLUSHDB
```

### 2. Clear Schedule (Ngay Lập Tức)

```bash
# Backup trước
cp /home/pi/smarthome_prj/GATEWAY/storage/data.db data.db.bak

# Xem schedule
sqlite3 /home/pi/smarthome_prj/GATEWAY/storage/data.db
> SELECT * FROM schedules;

# Xóa duplicate
> DELETE FROM schedules WHERE enabled=0;

# Xóa tất cả schedule của 1 device
> DELETE FROM schedules WHERE device_id='fan_kt_1';
```

### 3. Restart Gateway Services

```bash
# Restart tất cả
sudo systemctl restart gateway.service

# Hoặc restart riêng
pkill -f automation_engine.py
pkill -f firebase_sync.py

cd /home/pi/smarthome_prj/GATEWAY && ./start.sh
```

### 4. View Real-time Log

```bash
# Tất cả log
tail -f /home/pi/smarthome_prj/GATEWAY/logs/gateway.log

# Chỉ BLOCKED
tail -f /home/pi/smarthome_prj/GATEWAY/logs/gateway.log | grep BLOCKED

# Chỉ ERROR
tail -f /home/pi/smarthome_prj/GATEWAY/logs/gateway.log | grep ERROR

# DISPATCH + AUTO
grep -E "\[DISPATCH\]|\[AUTO\]" /home/pi/smarthome_prj/GATEWAY/logs/gateway.log | tail -20
```

---

## 📊 SYSTEM STATUS CHECK

### Kiểm tra Gateway Sống

```bash
curl http://192.168.1.131:5000/api/system/status

# Expected:
{
  "status": "running",
  "uptime": 3600,
  "workers_active": 5
}
```

### Kiểm tra MQTT Connection

```bash
mosquitto_sub -h localhost -t "home/+/state" -v

# Nếu có message → MQTT OK
# Nếu không → MQTT mất
```

### Kiểm tra Redis

```bash
redis-cli PING
# Response: PONG → Redis OK

redis-cli INFO stats | grep total_commands_processed
# Thấy số → Redis hoạt động
```

### Kiểm tra Database

```bash
sqlite3 /home/pi/smarthome_prj/GATEWAY/storage/data.db

> .tables
# automations, schedules, sensor_history, ...

> SELECT COUNT(*) FROM automations;
> SELECT COUNT(*) FROM schedules;
# Xem số record hiện tại
```

---

## 🎯 PRIORITY DISPATCH TABLE

| Source | Priority | Max Block Time | Khi Nào |
|--------|----------|------------------|---------|
| **Safety** | 0 (Cao nhất) | - | Gas/Smoke alert |
| **Manual/Web** | 1 | - | Người dùng bấn web |
| **Schedule** | 2 | 60 phút | Giờ cố định |
| **Automation** | 3 (Thấp) | 5 phút | Sensor trigger |

**Ý nghĩa**: Cao nhất = được ưu tiên gửi

---

## 📱 COMMON SCENARIOS

### Scenario 1: Bấn Web Rồi Schedule Không Chạy

```raw
Bạn: Bấn "Turn OFF" lúc 14:00
  ↓
Log: [DISPATCH] SENT [manual|p=1]: fan_kt_1 → turn_off
     [DISPATCH] BLOCKED (manual_override): schedule → fan_kt_1
  ↓
Chờ: 5 phút (14:05)
  ↓
Log: [DISPATCH] MANUAL_STATE TTL expired for fan_kt_1 → auto mode restored
  ↓
Bây giờ: Schedule/Automation hoạt động lại ✅

🔧 Nếu chưa hết: Bấn "Set Auto Mode" button
```

### Scenario 2: Gas Alert Liên Tục

```raw
Log: [WATCHDOG] DANGER DETECTED kitchen_01: RÒ RỈ KHÍ GAS! 659 ppm

Kiểm tra:
1. Cảm biến kẹt cao? Hiệu chuẩn 24h
2. Ngưỡng quá thấp? Thêm gas nấu ăn thử
3. Cảm biến lỗi? Check giá trị từ MQTT

🔧 FIX:
   - Hiệu chuẩn: Để ngoài trời 24h
   - Reset: esp_tool.py erase_flash + upload lại
```

### Scenario 3: Hệ Thống Lag/Chậm

```raw
Điểu kiện:
- Bấn web nhưng chậm 10 giây mới response
- Log DISPATCH liên tục reload

Nguyên nhân:
1. Schedule quá nhiều (> 50)
2. Firestore slow
3. Redis memory cao
4. CPU 100%

🔧 FIX:
   - Xóa schedule cũ (SQLite cleanup)
   - Tăng SYNC_INTERVAL từ 30s → 60s
   - Restart gateway: systemctl restart
```

### Scenario 4: Manual Mode Lock Quá Lâu

```raw
Bạn: Bấn turn_off lúc 15:00
Bấn lại lúc 15:01 (1 phút sau)
Bấn lại lúc 15:02
...
Bấn lại lúc 15:04

Kết quả: TTL vẫn reset, manual mode lock tới 15:09 ❌

🔧 FIX (Suggestion):
   - Đợi TTL hết (5 phút)
   - Hoặc bấn "Set Auto Mode" button ngay
   - (Đặc biệt: Không bấn liên tiếp < 1 giây)
```

---

## 🚀 DEPLOYMENT CHECKLIST

- [ ] Gateway service enabled: `sudo systemctl enable gateway.service`
- [ ] Redis running: `redis-cli PING` → PONG
- [ ] MQTT broker OK: `mosquitto -v` in logs
- [ ] Firebase credentials: `/home/pi/smarthome_prj/GATEWAY/firebase-service-account.json`
- [ ] Database initialized: `data.db` exists with 3 tables
- [ ] SD2 mount: `/mnt/sd2` writable
- [ ] WiFi: Both wlan0 (hotspot) + wlan1 (uplink) connected
- [ ] NTP time: `timedatectl status` shows synchronized
- [ ] Web interface: `curl http://192.168.1.131:5000` → 200 OK
- [ ] MQTT topics: `mosquitto_sub -t "home/#"` receives messages

---

## 📞 SUPPORT INFO

| Issue | Check | Fix |
|-------|-------|-----|
| Gateway won't start | `sudo systemctl status gateway.service` | Check logs, restart redis |
| No WiFi | `iwconfig` | Check SSID/password |
| Gas alert stuck | MQTT topics | Check sensor publish |
| Schedule not running | `SELECT * FROM schedules;` | Check enabled=1 |
| Manual block too long | Redis MANUAL_STATE | Run reset API |
| Performance slow | Check CPU/Memory | Cleanup schedules |

---

## 💾 FILE LOCATIONS

```raw
/home/pi/smarthome_prj/
├── GATEWAY/
│   ├── logs/gateway.log                  ← Log chính
│   ├── storage/
│   │   ├── data.db                       ← SQLite (automations, schedules)
│   │   └── db_schema.sql                 ← Schema
│   ├── workers/
│   │   ├── automation_engine.py          ← Dispatch + scheduler
│   │   ├── firebase_sync.py              ← Cloud sync
│   │   ├── safety_watchdog.py            ← Gas alert
│   │   └── network_watchdog.py           ← WiFi monitor
│   ├── bridge/message_bus.py             ← MQTT + Redis
│   └── app/api/routes/                   ← API endpoints
├── NhaThongMinh-Web/                     ← Web UI
│   └── NhaThongMinh-Web/js/
│       ├── dashboard*.js                 ← Room control
│       ├── firebase-config.js            ← Firebase connection
│       └── roomService.js                ← API calls
├── BEDROOM/KITCHEN/LIVINGROOM/           ← ESP32 firmware
│   └── src/main.cpp                      ← Firmware code
└── SYSTEM_USER_GUIDE.md                  ← 📖 Tài liệu này!
```

---

## 🎓 Học Thêm

| Tài liệu | Nội dung | File |
|----------|---------|------|
| **User Guide** | Hướng dẫn sử dụng đầy đủ | SYSTEM_USER_GUIDE.md |
| **Manual Override** | Chi tiết fix + optimization | MANUAL_OVERRIDE_SOLUTION.md |
| **Full Analysis** | Kiến trúc, code, workflow | COMPREHENSIVE_ANALYSIS.md |
| **Log Reference** | Tất cả log + troubleshooting | LOG_REFERENCE.md |

---

## ✨ QUICK TIPS

1. **Luôn chờ 5 phút** sau khi bấn web trước khi bấn lại
2. **Gas alert = bình thường**, tự động xử lý
3. **Check log** trước khi restart: `tail -f logs/gateway.log`
4. **Backup database** trước khi xóa schedule: `cp data.db data.db.bak`
5. **Monitoring**: Cài tool như `htop` để check CPU/Memory
6. **Firestore cleanup**: Xóa schedule cũ mỗi tháng

---

**Last Updated**: 2026-05-08  
**System Version**: SmartHome v2.2  
**Status**: ✅ Stable & Optimized
