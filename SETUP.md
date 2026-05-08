# SmartHome Gateway — Setup & Deploy Guide

## Tổng quan các bug đã fix

| Bug | File | Mô tả | Status |
|-----|------|-------|--------|
| BUG-C-01 | `gateway_main.py` | Flask Blueprints chưa đăng ký → API 404 | ✅ Fixed |
| BUG-C-02 | `data_syncer.py` | Treo vô hạn khi không có SD2 → Degraded Mode | ✅ Fixed |
| BUG-C-03 | `firebase_sync.py` | Thread leak khi restart → cleanup đúng cách | ✅ Fixed |
| BUG-C-04 | `safety_watchdog.py` | Spam Redis mỗi giây → đã fix ở phiên bản trước | ✅ Đã có |
| BUG-C-05 | `automation_engine.py` | Schedule bị tắt sau 1 lần chạy | ✅ Fixed |
| BUG-H-01 | `automation_engine.py` | Automation chỉ xử lý temperature | ✅ Fixed |
| BUG-H-02 | `automation_engine.py`, `roomService.js` | Field roomId vs room mismatch | ✅ Fixed |
| BUG-H-03 | `firebase_sync.py`, `roomService.js` | Dashboard trống do thiếu userId | ✅ Fixed |
| BUG-07 | `gateway_main.py` | Admin hash bcrypt vs SHA256 | ✅ Đã có |
| SEC-01 | `firestore.rules` | Không có Firestore security rules | ✅ Added |
| MISSING-1 | `start.sh` | Không có startup script | ✅ Added |
| MISSING-3 | `.env.example` | Biến môi trường hardcode trong code | ✅ Added |

---

## Bước 1: Cài đặt trên Raspberry Pi

```bash
cd /home/pi/smarthome_prj/GATEWAY

# Copy file fixed vào đúng vị trí
cp gateway_main.py ./
cp workers/automation_engine.py ./workers/
cp workers/firebase_sync.py ./workers/
cp workers/data_syncer.py ./workers/
cp storage/db_schema.sql ./storage/
cp start.sh ./
cp .env.example ./.env

chmod +x start.sh
```

## Bước 2: Cấu hình PI_OWNER_UID (QUAN TRỌNG — fix BUG-H-03)

Dashboard Admin bị trắng vì Pi không biết gán `userId` nào cho rooms khi sync lên Firebase.

**Lấy Firebase UID của tài khoản chủ nhà:**
1. Vào [Firebase Console](https://console.firebase.google.com) → Project → Authentication → Users
2. Tìm email của chủ nhà → copy cột **UID**
3. Mở `.env` trên Pi, thêm dòng:
```bash
PI_OWNER_UID=abc123xyz_firebase_uid_here
```

**Sau khi cấu hình, khởi động lại Gateway** — firebase_sync.py sẽ tự sync rooms với userId đúng.

## Bước 3: Deploy Firestore Security Rules

```bash
# Cài Firebase CLI (nếu chưa có)
npm install -g firebase-tools
firebase login

# Deploy rules
firebase deploy --only firestore:rules --project nhathongminh-14261
```

## Bước 4: Khởi động

```bash
# Test chạy thủ công trước
cd /home/pi/smarthome_prj/GATEWAY
bash start.sh

# Sau khi test OK → cài systemd service (chạy tự động khi boot)
sudo tee /etc/systemd/system/smarthome.service > /dev/null << 'EOF'
[Unit]
Description=SmartHome Gateway
After=network.target redis.service mosquitto.service
Wants=redis.service mosquitto.service

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/smarthome_prj/GATEWAY
EnvironmentFile=/home/pi/smarthome_prj/GATEWAY/.env
ExecStart=/usr/bin/python3 /home/pi/smarthome_prj/GATEWAY/gateway_main.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable smarthome
sudo systemctl start smarthome
sudo journalctl -u smarthome -f   # xem log realtime
```

## Bước 5: Deploy Web (Firestore JS)

Cập nhật file `js/roomService.js` bằng phiên bản fixed.

Kiểm tra trong browser console sau khi login:
- Không còn "Chưa có phòng" trên admin dashboard
- Click thiết bị → ESP32 nhận lệnh (kiểm tra Serial Monitor)

---

## Luồng dữ liệu hoàn chỉnh

```
ESP32 → MQTT → MessageBus → Redis(CH_INBOUND)
                                    ↓
                          automation_engine (process_sensor)
                          ├── temperature → quạt/đèn
                          ├── humidity   → quạt       [BUG-H-01 fixed]
                          └── co2        → quạt       [BUG-H-01 fixed]
                                    ↓
                          Redis(realtime_data) + SQLite
                                    ↓
                          firebase_sync → Firestore
                                    ↓
                          Web (realtime via onSnapshot)

Web → Firestore /commands (roomId + room) [BUG-H-02 fixed]
                    ↓
         firebase_sync (CommandDispatcher)
                    ↓ normalize roomId→room
         Redis(device_commands)
                    ↓
         automation_engine → MQTT → ESP32
```

## Kiểm tra hệ thống

```bash
# Kiểm tra API hoạt động
curl http://localhost:5000/api/health
curl http://localhost:5000/health

# Kiểm tra Redis
redis-cli subscribe realtime_data   # xem sensor updates realtime

# Kiểm tra MQTT
mosquitto_sub -h localhost -t "home/#" -v   # xem tất cả ESP32 messages

# Kiểm tra database
sqlite3 /data/smarthome.db "SELECT * FROM sensor_data ORDER BY id DESC LIMIT 10;"
sqlite3 /data/smarthome.db "SELECT * FROM schedules;"
```