# 🏠 SmartHome System User Guide & Troubleshooting

*Hướng dẫn sử dụng hệ thống và tránh các lỗi phổ biến*

---

## 📖 Phần 1: Hiểu về [DISPATCH] BLOCKED (manual_override)

### ❓ Log này có ý nghĩa gì?

```raw
[DISPATCH] BLOCKED (manual_override): schedule → fan_kt_1
[DISPATCH] BLOCKED (manual_override): automation → light_kt_1
```

**Ý nghĩa**: Schedule hoặc Automation bị **chặn tạm thời** vì thiết bị đang ở chế độ **Manual Override** (điều khiển thủ công).

### 🔄 Khi nào log này xuất hiện?

| Hành động | Kết quả |
|-----------|---------|
| Bạn bấm nút trên dashboard web | Fan/Light chuyển sang Manual Mode |
| Bạn gửi lệnh turn_on/turn_off qua API | Device lock trong 5 phút |
| TTL hết hạn (5 phút không có lệnh manual mới) | Auto Mode khôi phục tự động |

### 🎯 Mục đích của chế độ Manual Override

```raw
Tình huống: Bạn bấm tắt fan bằng web
  ↓
Hệ thống set MANUAL_STATE[fan_kt_1] = {mode: "manual", set_at: now}
  ↓
Schedule/Automation bị chặn 5 phút (để tôn trọng lệnh người dùng)
  ↓
Nếu sau 5 phút không có lệnh mới → tự động trở về Auto Mode
  ↓
Schedule/Automation hoạt động trở lại
```

**Lý do**: Ngăn ngừa xung đột giữa lệnh người dùng và lệnh tự động.

---

## ⚠️ Phần 2: Tại sao vẫn thấy BLOCKED nhưng hệ thống không treo?

### ✅ Hệ thống không treo là BÌNH THƯỜNG

```raw
[DISPATCH] BLOCKED (manual_override): schedule → fan_kt_1
[DISPATCH] BLOCKED (manual_override): automation → light_kt_1
[AUTO] Schedules reloaded: 11
[DISPATCH] SENT [manual|p=1]: kitchen_01/fan_kt_1 → turn_off
```

**Giải thích**:
1. ❌ Schedule bị chặn (vì manual mode đang active)
2. ❌ Automation bị chặn (vì manual mode đang active)
3. ✅ Nhưng **lệnh manual bạn gửi VẪNĐƯỢC THỰC HIỆN** (turn_off thành công)

**Hệ thống hoạt động đúng!** Log chỉ thông báo rằng schedule/automation bị tạm ngưng.

---

## 🛠️ Phần 3: Tất cả Log Messages Trong Hệ Thống

### Gateway Main Logs (gateway_main.py)

| Log | Ý nghĩa | Mức độ |
|-----|---------|-------|
| `[MAIN] DB schema applied` | SQLite schema đã tạo | INFO |
| `[MAIN] Gateway LIVE → http://0.0.0.0:5000/` | Gateway khởi động thành công | INFO |
| `[MAIN] All workers started` | Tất cả thread worker sẵn sàng | INFO |

### MessageBus Logs (bridge/message_bus.py)

| Log | Ý nghĩa | Mức độ |
|-----|---------|-------|
| `[BUS] MessageBus started` | Redis connection OK | INFO |
| `[BUS] MQTT connected & subscribed` | MQTT broker ready | INFO |
| `[BUS] Outbound loop started` | MQTT publish loop chạy | INFO |

### Network Watchdog (workers/network_watchdog.py)

| Log | Ý nghĩa | Mức độ |
|-----|---------|-------|
| `[NET] Dual interface detected: Hotspot=wlan0, Uplink=wlan1` | Hai WiFi được phát hiện | INFO |
| `[NET] Initial WiFi status: connected / BETEA / internet=True` | WiFi kết nối, có internet | INFO |
| `[NET] WiFi switch: uplink unavailable, using hotspot` | Chuyển sang hotspot nếu WiFi chính mất | WARNING |
| `[NET] NTP already synchronized via systemd-timesyncd` | Thời gian đã đồng bộ | INFO |

### Safety Watchdog (workers/safety_watchdog.py)

| Log | Ý nghĩa | Mức độ | Hành động |
|-----|---------|-------|----------|
| `[WATCHDOG] Safety watchdog started` | Thread giám sát an toàn bắt đầu | INFO | - |
| `[WATCHDOG] DANGER DETECTED kitchen_01: RÒ RỈ KHÍ GAS! 659 ppm` | Phát hiện gas vượt 600 ppm | **CRITICAL** | ⚠️ Bật fan tự động + buzzer |
| `[WATCHDOG] kitchen_01: SAFE — lock released` | Gas trở về bình thường < 400 ppm | INFO | ✅ Hết cảnh báo |
| `[WATCHDOG] kitchen_01 smart_muted (alert read)` | Người dùng đã đọc cảnh báo | INFO | 🔇 Tắt buzzer 180 giây |

### Firebase Sync (workers/firebase_sync.py)

| Log | Ý nghĩa | Mức độ |
|-----|---------|-------|
| `firebase_sync v2.2 starting` | Sync worker khởi động | INFO |
| `[Uplink] Stream started` | Lắng nghe sensor từ Firebase Realtime DB | INFO |
| `[Downlink] CommandDispatcher started` | Lắng nghe commands từ Firestore | INFO |
| `Firestore: Batch flush 72 sensor history rows` | Gửi 72 dòng sensor lên Firestore | INFO |
| `Flushed 72 sensor history rows to Firestore` | Xong rồi | INFO |
| `[AutoSync] Automation rules updated in SQLite from Firestore` | Sync automation từ Firestore xuống | INFO |
| `[AutoSync] Schedules updated in SQLite from Firestore` | Sync schedule từ Firestore xuống | INFO |
| `[Downlink] DISPATCH: UUID [turn_on] → Redis` | Phát commands từ Firestore → Redis | INFO |
| `[Downlink] CLEANUP: Đã xóa lệnh UUID` | Xóa command sau khi gửi thành công | INFO |

### Automation Engine (workers/automation_engine.py)

| Log | Ý nghĩa | Mức độ | Khi nào |
|-----|---------|-------|--------|
| `[AUTO] Cache loaded: 3 rules, 8 schedules` | Nạp lệnh và lịch từ SQLite | INFO | Khởi động |
| `[AUTO] Schedules reloaded: 11` | Reload lịch mới từ Firebase | INFO | Mỗi 30s hoặc khi thay đổi |
| `[AUTO] Physical button detected: fan_kt_1 → MANUAL mode set` | Phát hiện nút vật lý (ESP32) | INFO | Khi bấm nút trên ESP32 |
| `[DISPATCH] SENT [schedule\|p=2]: kitchen_01/fan_kt_1 → turn_on` | Gửi lệnh từ schedule | INFO | ✅ Thành công |
| `[DISPATCH] SENT [automation\|p=3]: kitchen_01/light_kt_1 → turn_on` | Gửi lệnh từ automation | INFO | ✅ Thành công |
| `[DISPATCH] SENT [manual\|p=1]: kitchen_01/fan_kt_1 → turn_off` | Gửi lệnh từ web/API | INFO | ✅ Thành công |
| `[DISPATCH] BLOCKED (manual_override): schedule → fan_kt_1` | Schedule bị chặn | INFO | ⏸️ Vì device ở manual mode |
| `[DISPATCH] BLOCKED (automation): automation → light_kt_1` | Automation bị chặn | INFO | ⏸️ Vì schedule active 60 phút |
| `[DISPATCH] BLOCKED (safety_lock): schedule → kitchen_01/fan_kt_1` | Schedule bị chặn vì cảnh báo | INFO | ⏸️ Gas/Smoke vượt ngưỡng |
| `[DISPATCH] MANUAL_STATE TTL expired for fan_kt_1 → auto mode restored` | Chế độ manual hết hạn | INFO | ✅ Quay lại auto mode |
| `[SCHEDULER] Executed: kitchen_01/fan_kt_1 → turn_on` | Schedule đã chạy | INFO | ✅ Đã gọi MQTT |
| `[AUTO] Enrollment cancelled via Firebase command` | Hủy ghi RFID | INFO | ❌ Người dùng bấm hủy |

### API Routes (app/api/routes/*.py)

| Log | Ý nghĩa | Mức độ |
|-----|---------|-------|
| `[API] Registered /api: auth` | Auth endpoint sẵn sàng | INFO |
| `[API] Registered /api: sensors` | Sensor API sẵn sàng | INFO |
| `[API] Registered /api: devices` | Device control API sẵn sàng | INFO |
| `[API] Registered /api: automation` | Automation API sẵn sàng | INFO |

---

## 🚀 Phần 4: Hướng Dẫn Sử Dụng Hệ Thống

### 4.1 Chế độ Hoạt Động Bình Thường

```raw
Lúc 7:00 sáng:
├─ Schedule kick in: Bâtí fan (p=2)
├─ Nếu nhiệt độ > 28°C: Automation cũng bật fan (p=3 - bị chặn vì schedule)
└─ Sau 5 phút: Schedule hết hạn, fan vẫn chạy

Người dùng bấm tắt fan lúc 7:15:
├─ Web send lệnh turn_off (p=1)
├─ MANUAL_STATE[fan_kt_1] được set
├─ Automation bị BLOCKED 5 phút (tôn trọng người dùng)
└─ Sau 5 phút: Fan quay lại Auto Mode (schedule/automation)
```

### 4.2 Khi Nào Hệ Thống Hoạt Động Tối Ưu

✅ **Hoạt động tốt nhất khi**:
- Bấm nút web/app → chờ tối thiểu 5 phút trước khi bấm tiếp
- Đặt schedule vào sáng 6-8 AM, chiều 5-7 PM
- Để automation threshold nhạy cảm (nhiệt độ -5°C, gas +100 ppm)
- Dùng RFID để unlock thay vì ghi thủ công thường xuyên

❌ **Tránh các tình huống này**:
- Bấm liên tiếp (< 1 giây): Sẽ reset MANUAL_STATE TTL, schedule bị chặn lâu
- Xóa schedule trên Firestore mà không restart gateway: Dùng API để xóa
- Thay đổi automation threshold mà không reload: Đợi 30 giây để sync

---

## 🔍 Phần 5: Các Tình Huống Log Khác Nhau

### Tình huống 1: Khởi động bình thường

```raw
2026-05-08 13:11:50 [INFO][SYNCER] SyncWorker starting...
2026-05-08 13:11:50 [INFO][SYNCER] Waiting for SD2 (timeout 300s before degraded mode)...
2026-05-08 13:11:51 [INFO][SYNCER] firebase_sync v2.2 starting
2026-05-08 13:11:51 [INFO][SYNCER] === AUTO-PROVISIONING BẮT ĐẦU ===
[MAIN] DB schema applied
[MAIN] DB init complete
[BUS] MessageBus started
[BUS] MQTT connected & subscribed
[WATCHDOG] Safety watchdog started
[NET] Dual interface detected: Hotspot=wlan0, Uplink=wlan1
[MAIN] FirebaseSync worker started
[MAIN] All workers started
[AUTO] Cache loaded: 3 rules, 8 schedules
[SCHEDULER] Started
[MAIN] Gateway LIVE → http://0.0.0.0:5000/

✅ Tất cả bình thường!
```

### Tình huống 2: Cảnh báo gas

```raw
[WATCHDOG] DANGER DETECTED kitchen_01: RÒ RỈ KHÍ GAS! 659 ppm - Phòng: kitchen_01
│
├─ Hành động tự động:
│  ├─ Bật fan tạo thông gió
│  ├─ Bật buzzer phát cảnh báo
│  ├─ Lock automation/schedule 5 phút
│  └─ Gửi alert qua Firestore + Redis
│
├─ Lệnh bị chặn:
│  ├─ [DISPATCH] BLOCKED (safety_lock): schedule → kitchen_01/fan_kt_1
│  ├─ [DISPATCH] BLOCKED (safety_lock): automation → kitchen_01/light_kt_1
│  └─ Chỉ "safety" source mới được gửi
│
└─ Sau 5 phút (gas < 400 ppm):
   [WATCHDOG] kitchen_01: SAFE — lock released
   ✅ Schedule/Automation quay lại bình thường
```

### Tình huống 3: Điều khiển manual rồi chờ TTL hết

```raw
[DISPATCH] SENT [manual|p=1]: kitchen_01/fan_kt_1 → turn_off  (Người dùng bấm)
│
├─ MANUAL_STATE[fan_kt_1] set (TTL = 300s)
│
├─ Các lệnh bị chặn:
│  ├─ [DISPATCH] BLOCKED (manual_override): automation → fan_kt_1
│  ├─ [DISPATCH] BLOCKED (manual_override): schedule → fan_kt_1
│  └─ Kéo dài ~ 5 phút
│
├─ Khi TTL hết (5 phút):
│  └─ [DISPATCH] MANUAL_STATE TTL expired for fan_kt_1 → auto mode restored
│
└─ Sau đó:
   [DISPATCH] SENT [automation|p=3]: kitchen_01/fan_kt_1 → turn_on  ✅
```

### Tình huống 4: Sync schedules từ Firestore

```raw
2026-05-08 15:14:34 [INFO][SYNCER] [AutoSync] Schedules updated in SQLite from Firestore
│
├─ Firebase_sync 30s timer kick in
├─ Poll Firestore collection "schedules"
├─ So sánh với SQLite:
│  ├─ Mới (Firestore) → INSERT vào SQLite
│  ├─ Thay đổi (action/enabled) → UPDATE
│  └─ Xóa (chỉ trên Firestore) → DELETE từ SQLite
├─ Publish Redis channel "schedule_commands"
│
└─ Automation engine nhận:
   └─ [AUTO] Schedules reloaded: 11   (Reload cache)
```

---

## 🐛 Phần 6: Troubleshooting - Tìm Lỗi

### Lỗi 1: Schedule không chạy

```raw
[AUTO] Schedules reloaded: 11
[DISPATCH] BLOCKED (manual_override): schedule → fan_kt_1
│
❓ Tìm hiểu:
├─ 1. TTL còn bao lâu? Kiểm tra log có "MANUAL_STATE TTL expired" không?
├─ 2. Schedule trong SQLite chưa? Kiểm tra: sqlite3 /home/pi/smarthome_prj/GATEWAY/storage/data.db
│    > SELECT * FROM schedules WHERE device_id='fan_kt_1';
├─ 3. Có cảnh báo safety không? Tìm "DANGER DETECTED" hoặc "safety_lock"
└─ 4. Automation chặn không? Tìm "schedule_active_60m"

🔧 Cách fix:
├─ Gửi API: POST /api/devices/{device_id}/set_auto_mode  (Reset manual state)
├─ Hoặc chờ 5 phút TTL hết
└─ Reload schedule: Redis publish schedule_commands '{"action":"reload"}'
```

### Lỗi 2: Gas alert không tắt

```raw
[WATCHDOG] DANGER DETECTED kitchen_01: RÒ RỈ KHÍ GAS! 659 ppm
│
Sau 10 phút vẫn thấy log cảnh báo
│
❓ Nguyên nhân:
├─ 1. Cảm biến gas kẹt cao (cần hiệu chuẩn)
├─ 2. Ngưỡng 600 ppm quá thấp cho môi trường
└─ 3. Cảm biến bị lỗi (đọc sai giá trị)

🔧 Cách fix:
├─ Hiệu chuẩn cảm biến: Để ngoài 24h, reset code
├─ Tăng ngưỡng: Chỉnh THRESHOLD trong automation config
└─ Kiểm tra: Log từ MQTT có cập nhật không?
```

### Lỗi 3: Hệ thống bị lag/chậm

```raw
[AUTO] Schedules reloaded: 11
[AUTO] Schedules reloaded: 11
[AUTO] Schedules reloaded: 11  ← Reload liên tục!
│
❓ Nguyên nhân:
├─ 1. Schedules quá nhiều (> 50) → tìm "seen_keys"
├─ 2. Firestore mỗi lần sync lại ghi hết → DELETE/INSERT liên tục
├─ 3. CPU cao vì reload cache quá thường xuyên
└─ 4. Redis publish bị block

🔧 Cách fix:
├─ Bảo trì Firestore: Xóa schedules cũ/trùng lặp
├─ Thêm throttle logic: Chỉ reload nếu thực sự có thay đổi
├─ Tăng SYNC_INTERVAL từ 30s lên 60s (firebase_sync.py)
└─ Kiểm tra Redis memory: redis-cli INFO memory
```

---

## 📱 Phần 7: Cách Điều Khiển Hệ Thống Tối Ưu

### ✅ Quy tắc vàng

**1. Bấm nút web → Chờ 5 phút trước lần tiếp theo**
```raw
Bấm turn_off → Chờ 5 phút → Mới bấm lại
Không được bấm liên tiếp < 1 giây
```

**2. Đặt schedule vào giờ đơn lẻ**
```raw
✅ 6:00 AM, 7:00 AM, 5:00 PM
❌ 6:15 AM, 6:30 AM, 5:45 PM (nếu quá nhiều)
```

**3. Để automation threshold hợp lý**
```raw
✅ Gas 600 ppm (gần tiêu chuẩn WHO)
✅ Nhiệt độ 28°C
❌ Gas 800 ppm (bạn không an toàn)
❌ Nhiệt độ 25°C (quá nhạy, fan bật suốt)
```

**4. Khi thay đổi setting, reload gateway**
```raw
Đổi automation rule → Đợi 30s (Firebase sync)
Hoặc restart gateway: sudo systemctl restart gateway.service
```

**5. Sử dụng RFID thay vì ghi thủ công**
```raw
✅ RFID → Unlock nhanh, không set manual mode
❌ Ghi thủ công mỗi ngày → Manual mode lock 5 phút
```

---

## 🔧 Phần 8: Các Lệnh Debug Hữu Ích

### Kiểm tra schedule trong SQLite

```bash
sqlite3 /home/pi/smarthome_prj/GATEWAY/storage/data.db

# Xem tất cả schedule
SELECT * FROM schedules WHERE enabled=1;

# Xem schedule của fan kitchen
SELECT * FROM schedules WHERE device_id='fan_kt_1';

# Xóa schedule cũ
DELETE FROM schedules WHERE device_id='fan_kt_1' AND time='12:00';
```

### Xem trạng thái Redis

```bash
redis-cli

# Xem MANUAL_STATE
GET device:fan_kt_1:manual_state

# Xem pending commands
LLEN device_commands

# Clear all
FLUSHDB
```

### Xem log realtime

```bash
# Gateway log
tail -f /home/pi/smarthome_prj/GATEWAY/logs/gateway.log

# Filter chỉ BLOCKED
tail -f /home/pi/smarthome_prj/GATEWAY/logs/gateway.log | grep BLOCKED

# Filter automation
grep -E "\[AUTO\]|\[DISPATCH\]" /home/pi/smarthome_prj/GATEWAY/logs/gateway.log
```

### Restart workers

```bash
# Restart gateway
sudo systemctl restart gateway.service

# Kill firebase_sync
pkill -f firebase_sync.py

# Kill automation engine
pkill -f automation_engine.py

# Restart all
cd /home/pi/smarthome_prj/GATEWAY && ./start.sh
```

---

## ✨ Kết Luận

Log `[DISPATCH] BLOCKED (manual_override)` **hoàn toàn bình thường** và chỉ thể hiện:
- ✅ Hệ thống **đang bảo vệ** lệnh người dùng
- ✅ Schedule/Automation bị **tạm ngưng** để tôn trọng bạn
- ✅ Sau 5 phút sẽ tự động **hoạt động lại**

**Hệ thống không treo = HỆ THỐNG HOẠT ĐỘNG ĐÚNG!** 🎉

Nếu có câu hỏi, kiểm tra **[LOG_REFERENCE.md](./LOG_REFERENCE.md)** và **[COMPREHENSIVE_ANALYSIS.md](./COMPREHENSIVE_ANALYSIS.md)** để hiểu sâu hơn.
