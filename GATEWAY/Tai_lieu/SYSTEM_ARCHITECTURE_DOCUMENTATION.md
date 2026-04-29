# SmartHome System - Comprehensive Documentation

## 1. KIẾN TRÚC HỆ THỐNG

### 1.1 Tổng quan kiến trúc

Hệ thống SmartHome được thiết kế theo kiến trúc phân tán với 4 tầng chính:

```
┌─────────────────────────────────────────────────────────────┐
│                    WEB FRONTEND (Browser)                   │
│  - Dashboard cho từng phòng (bedroom/kitchen/livingroom)    │
│  - Authentication & Authorization                           │
│  - Real-time polling thay Firebase                          │
│  - Responsive UI với ES6 modules                           │
└─────────────────────┬───────────────────────────────────────┘
                      │ HTTPS (Cloudflare Tunnel)
                      ▼
┌─────────────────────────────────────────────────────────────┐
│                 GATEWAY BACKEND (Raspberry Pi)              │
│  ┌─────────────────────────────────────────────────────┐    │
│  │              FLASK API SERVER (Port 5000)           │    │
│  │  - RESTful APIs cho tất cả chức năng               │    │
│  │  - Authentication (JWT-like với localStorage)      │    │
│  │  - CORS enabled cho cross-origin                    │    │
│  │  - SocketIO cho realtime (tương lai)               │    │
│  └─────────────────────┬───────────────────────────────┘    │
│                        │                                     │
│  ┌─────────────────────▼─────────────────────────────┐      │
│  │            MESSAGE BUS (MQTT ↔ Redis)             │      │
│  │  - MQTT inbound: ESP32 → Redis pubsub             │      │
│  │  - MQTT outbound: Redis → ESP32                   │      │
│  │  - Singleton pattern, thread-safe                 │      │
│  └─────────────────────┬─────────────────────────────┘      │
│                        │                                     │
│  ┌─────────────────────▼─────────────────────────────┐      │
│  │              WORKERS (Background Threads)         │      │
│  │  ┌─────────────────────────────────────────────┐  │      │
│  │  │     SAFETY WATCHDOG (Priority High)        │  │      │
│  │  │  - Giám sát Gas/Fire độc lập               │  │      │
│  │  │  - Khóa điều khiển khi nguy hiểm           │  │      │
│  │  │  - Tự động bật quạt/còi báo động           │  │      │
│  │  └─────────────────────────────────────────────┘  │      │
│  │  ┌─────────────────────────────────────────────┐  │      │
│  │  │     AUTOMATION ENGINE                      │  │      │
│  │  │  - Logic IF/THEN từ cảm biến               │  │      │
│  │  │  - Scheduler hẹn giờ (DS3231 sync)         │  │      │
│  │  │  - RFID access control                     │  │      │
│  │  └─────────────────────────────────────────────┘  │      │
│  │  ┌─────────────────────────────────────────────┐  │      │
│  │  │     DATA SYNCER                            │  │      │
│  │  │  - Redis buffer → SQLite batch insert      │  │      │
│  │  │  - Backup to SD2 card                      │  │      │
│  │  └─────────────────────────────────────────────┘  │      │
│  │  ┌─────────────────────────────────────────────┐  │      │
│  │  │     NETWORK WATCHDOG                       │  │      │
│  │  │  - WiFi monitoring & hotspot               │  │      │
│  │  │  - RTC DS3231 synchronization              │  │      │
│  │  └─────────────────────────────────────────────┘  │      │
│  └─────────────────────────────────────────────────────┘      │
│                                                               │
│  EXTERNAL SERVICES:                                           │
│  - Mosquitto MQTT Broker (Port 1883)                          │
│  - Redis Server (Port 6379)                                   │
│  - SQLite Database (/data/smarthome.db)                       │
│  - SD2 Storage (/mnt/sd2)                                     │
└─────────────────────┬─────────────────────────────────────────┘
                      │ MQTT (Local Network)
                      ▼
┌─────────────────────────────────────────────────────────────┐
│                    ESP32 NODES (Edge Devices)               │
│  ┌─────────────────────────────────────────────────────┐    │
│  │              BEDROOM NODE                          │    │
│  │  - DHT11/HDC1080: Temperature/Humidity             │    │
│  │  - CCS811: CO2/eCO2, TVOC                          │    │
│  │  - Relay: Fan, Light control                       │    │
│  │  - Physical buttons + FreeRTOS task                │    │
│  └─────────────────────────────────────────────────────┘    │
│  ┌─────────────────────────────────────────────────────┐    │
│  │              KITCHEN NODE                          │    │
│  │  - Gas sensor (MQ-2/MQ-135)                        │    │
│  │  - Temperature/Humidity                            │    │
│  │  - Relay: Fan, Light, Gas valve                    │    │
│  │  - Buzzer alarm system                             │    │
│  └─────────────────────────────────────────────────────┘    │
│  ┌─────────────────────────────────────────────────────┐    │
│  │              LIVINGROOM NODE                        │    │
│  │  - Temperature/Humidity sensors                     │    │
│  │  - Relay: Fan, Light control                        │    │
│  │  - LCD display for status                           │    │
│  └─────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
```

### 1.2 Luồng dữ liệu chính

#### Luồng cảm biến (Sensor Data Flow):
```
ESP32 → MQTT → MessageBus → Redis pubsub → DataSyncer → SQLite
       → Automation Engine → Decision Logic → MQTT → ESP32
```

#### Luồng điều khiển (Control Flow):
```
Web UI → API → Redis pubsub → MessageBus → MQTT → ESP32 → Hardware
```

#### Luồng cảnh báo (Alert Flow):
```
ESP32 → MQTT → Safety Watchdog → Database + Notifications → Web UI
```

## 2. CẤU TRÚC THỤ MỤC

```
/home/pi/smarthome_prj/
├── GATEWAY/                          # Backend Raspberry Pi
│   ├── gateway_main.py               # Entry point chính
│   ├── requirements.txt              # Python dependencies
│   ├── setup.sh                      # Auto setup script
│   ├── run.sh                        # Auto run script
│   ├── cloudflared.service           # Systemd service cho tunnel
│   ├── docker-compose.yml            # Alternative Docker setup
│   ├── app/                          # Flask application
│   │   ├── main.py                   # Flask + SocketIO init
│   │   └── api/routes/
│   │       └── all_routes.py         # Tất cả API endpoints
│   ├── bridge/
│   │   └── message_bus.py            # MQTT ↔ Redis bridge
│   ├── workers/                      # Background services
│   │   ├── safety_watchdog.py        # Gas/Fire monitoring
│   │   ├── automation_engine.py      # Logic + Scheduler
│   │   ├── data_syncer.py            # Redis → SQLite sync
│   │   └── network_watchdog.py       # WiFi + RTC
│   ├── storage/
│   │   └── db_schema.sql             # SQLite schema
│   └── scripts/                      # Utility scripts
│       ├── 99-sd2-mount.rules        # SD2 auto-mount
│       └── mount_sd2.sh              # SD2 mount script
├── BEDROOM/                          # ESP32 Bedroom Node
│   ├── platformio.ini                # PlatformIO config
│   ├── src/
│   │   ├── main.cpp                  # Arduino main loop
│   │   ├── Config.hpp                # WiFi/MQTT config
│   │   ├── NetworkManager.hpp        # WiFi + MQTT handling
│   │   └── HardwareControl.hpp       # Sensors + Relays
│   └── test/                         # Unit tests
├── KITCHEN/                          # ESP32 Kitchen Node
│   └── [Tương tự BEDROOM]
├── LIVINGROOM/                       # ESP32 Livingroom Node
│   └── [Tương tự BEDROOM]
└── NhaThongMinh-Web/                 # Frontend Application
    └── NhaThongMinh-Web/
        ├── index.html                 # Login page
        ├── dashboard-bedroom.html     # Bedroom dashboard
        ├── dashboard-kitchen.html     # Kitchen dashboard
        ├── dashboard-livingroom.html  # Livingroom dashboard
        ├── admin.html                 # Admin panel
        ├── settings.html              # Settings page
        ├── notifications.html         # Notifications page
        ├── js/                        # Frontend JavaScript
        │   ├── apiService.js          # API client service
        │   ├── config.js              # API endpoints config
        │   ├── firebase-config.js     # REMOVED - Firebase
        │   ├── roomService.js         # Room management
        │   └── [dashboard-*.js]       # Dashboard logic
        ├── css/                       # Stylesheets
        └── images/                    # Static assets
```

## 3. ĐIỂM CHI TIẾT QUAN TRỌNG

### 3.1 Authentication & Security

- **Backend**: SHA256 password hashing (bcrypt planned)
- **Frontend**: JWT-like token stored in localStorage
- **API**: Bearer token authentication với Authorization header
- **Session**: Redis-backed sessions với 24h expiry
- **CORS**: Fully enabled cho cross-origin requests

### 3.2 Message Bus Architecture

- **Singleton Pattern**: Một instance duy nhất cho toàn hệ thống
- **Thread-safe**: Sử dụng threading.Lock()
- **MQTT Topics**: `home/{room}/{category}` (sensors/status/command)
- **Redis Channels**: 
  - `mqtt_inbound`: ESP32 → Workers
  - `mqtt_outbound`: Workers → ESP32
  - `device_commands`: Web → Automation
  - `realtime_data`: Workers → Web

### 3.3 Safety System Priority

- **Priority Level**: Critical - cao nhất trong hệ thống
- **Independent Operation**: Hoạt động độc lập, không phụ thuộc mạng/web
- **Safety Lock**: Khóa tất cả điều khiển thủ công khi phát hiện nguy hiểm
- **Mute Logic**: 10 phút im lặng sau khi user xác nhận, nhưng báo lại nếu vẫn nguy hiểm
- **Hardware Control**: Tự động bật quạt, còi báo động

### 3.4 Data Persistence Strategy

- **SQLite WAL Mode**: Write-Ahead Logging cho performance
- **Redis Buffer**: Temporary storage khi database busy
- **SD2 Backup**: Automatic export to external SD card
- **Daily Partitions**: Sensor data partitioned by date for performance

### 3.5 Real-time Communication

- **Polling**: Frontend polls API mỗi 5 giây thay Firebase
- **WebSocket Ready**: SocketIO infrastructure sẵn sàng
- **Event-driven**: Redis pubsub cho internal communication
- **Buffer Management**: ESP32 buffers data khi mất kết nối

## 4. HƯỚNG DẪN CHẠY HỆ THỐNG

### 4.1 Chuẩn bị môi trường

```bash
# Cập nhật system
sudo apt update && sudo apt upgrade -y

# Cài đặt dependencies
sudo apt install -y python3 python3-venv python3-pip \
  build-essential libssl-dev libffi-dev python3-dev \
  mosquitto mosquitto-clients redis-server redis-tools \
  network-manager git curl wget netcat-openbsd
```

### 4.2 Chạy Gateway Backend

```bash
cd /home/pi/smarthome_prj/GATEWAY

# Setup (chạy 1 lần)
bash setup.sh

# Chạy gateway
bash run.sh
```

**Setup script sẽ:**
- Tạo Python virtual environment
- Cài đặt tất cả dependencies từ requirements.txt
- Khởi động Mosquitto MQTT broker
- Khởi động Redis server
- Tạo database schema

**Run script sẽ:**
- Activate venv
- Kiểm tra services đang chạy
- Khởi động gateway_main.py với tất cả workers

### 4.3 Chạy ESP32 Nodes

```bash
# Cài đặt PlatformIO
pip install platformio

# Build và upload từng node
cd /home/pi/smarthome_prj/BEDROOM
pio run --target upload

cd /home/pi/smarthome_prj/KITCHEN
pio run --target upload

cd /home/pi/smarthome_prj/LIVINGROOM
pio run --target upload
```

### 4.4 Cấu hình Cloudflare Tunnel

```bash
# Cài đặt cloudflared
wget -q https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64.deb
sudo dpkg -i cloudflared-linux-arm64.deb

# Đăng nhập Cloudflare
cloudflared tunnel login

# Tạo tunnel
cloudflared tunnel create nhathongminh

# Cấu hình tunnel
cat > ~/.cloudflared/config.yml << EOF
tunnel: 2f5be01f-842e-4dcf-a2df-6344b2760503
credentials-file: /home/pi/.cloudflared/2f5be01f-842e-4dcf-a2df-6344b2760503.json
ingress:
  - hostname: nhathongminh.crfnetwork.cyou
    service: http://localhost:5000
  - service: http_status:404
EOF

# Khởi động tunnel
sudo systemctl enable cloudflared.service
sudo systemctl start cloudflared.service
```

### 4.5 Truy cập Web Interface

1. **Local Access**: `http://raspberry-pi-ip:5000`
2. **Public Access**: `https://nhathongminh.crfnetwork.cyou`

**Default Admin Account:**
- Email: `admin@smarthome.local`
- Password: `admin123`

## 5. CẤU TRÚC API

### 5.1 Authentication APIs

```javascript
// Login
POST /auth/login
{
  "email": "user@example.com",
  "password": "password123"
}

// Register (Admin only)
POST /auth/register
{
  "email": "newuser@example.com", 
  "password": "password123",
  "display_name": "New User"
}

// Logout
POST /auth/logout
Headers: Authorization: Bearer <token>
```

### 5.2 Sensor APIs

```javascript
// Latest sensor data
GET /latest?room=bedroom_01

// Historical data
GET /history?room=bedroom_01&type=temperature&hours=24

// Dashboard data (sensors + devices + alerts)
GET /dashboard?room=bedroom_01

// Chart data for graphs
GET /chart?room=bedroom_01&type=temperature&hours=24

// List all rooms
GET /rooms
```

### 5.3 Device Control APIs

```javascript
// Get device status
GET /device_status?room=bedroom_01

// Control device
POST /control
Headers: Authorization: Bearer <token>
{
  "room": "bedroom_01",
  "device_id": "fan_bd_1", 
  "is_on": true
}
```

### 5.4 Automation APIs

```javascript
// Get automation rules
GET /automations

// Update automation rule
POST /automations
Headers: Authorization: Bearer <token>
{
  "room_id": "bedroom_01",
  "enabled": true,
  "fan_threshold": 28.0,
  "light_threshold": 25.0,
  "gas_threshold": 600
}

// Delete automation rule
DELETE /automations/bedroom_01
Headers: Authorization: Bearer <token>

// Get schedules
GET /schedules?room=bedroom_01

// Add schedule
POST /schedules
Headers: Authorization: Bearer <token>
{
  "room_id": "bedroom_01",
  "device_id": "fan_bd_1",
  "action": "turn_on",
  "time": "18:00"
}

// Delete schedule
DELETE /schedules/123
Headers: Authorization: Bearer <token>
```

### 5.5 Logs & Notifications APIs

```javascript
// Unified logs feed
GET /logs/feed?type=all&limit=50&offset=0
Headers: Authorization: Bearer <token>

// Get alerts
GET /alerts?room=bedroom_01

// Resolve alert
POST /alerts/123/resolve
Headers: Authorization: Bearer <token>

// Get notifications
GET /notifications?unread=1
Headers: Authorization: Bearer <token>

// Mark all as read
POST /notifications/read_all
Headers: Authorization: Bearer <token>

// Access logs
GET /access_logs?room=bedroom_01
Headers: Authorization: Bearer <token>

// Login logs
GET /login_logs?limit=50&offset=0
Headers: Authorization: Bearer <token>
```

### 5.6 RFID APIs

```javascript
// Get RFID cards
GET /rfid_cards
Headers: Authorization: Bearer <token>

// Add RFID card
POST /rfid_cards
Headers: Authorization: Bearer <token>
{
  "uid": "ABC123",
  "owner_name": "Nguyen Van A"
}

// Delete RFID card
DELETE /rfid_cards/ABC123
Headers: Authorization: Bearer <token>

// Start enrollment mode
POST /rfid/enroll
Headers: Authorization: Bearer <token>
{
  "owner_name": "New Card"
}
```

### 5.7 WiFi APIs

```javascript
// Get WiFi status
GET /wifi/status

// Scan WiFi networks
POST /wifi/scan
Headers: Authorization: Bearer <token>

// Get scan results
GET /wifi/scan_result

// Connect to WiFi
POST /wifi/connect
Headers: Authorization: Bearer <token>
{
  "ssid": "MyWiFi",
  "password": "password123"
}
```

### 5.8 OTA APIs

```javascript
// Upload firmware
POST /ota/upload
Headers: Authorization: Bearer <admin_token>
Content-Type: multipart/form-data
file: firmware.bin

// Trigger OTA update
POST /ota/update
Headers: Authorization: Bearer <admin_token>
{
  "room": "bedroom_01",
  "file": "firmware_v1.2.bin"
}
```

### 5.9 System APIs

```javascript
// Health check
GET /health

// SD2 status
GET /sd2/status

// Export data to SD2
POST /sd2/export_now
Headers: Authorization: Bearer <token>

// Get system clock
GET /system/clock

// Get system snapshots
GET /system/snapshots?hours=24

// Get safety status
GET /system/safety_status

// Get daily data
GET /data?date=2024-01-15&type=sensor
GET /data?dates=2024-01-15,2024-01-16&type=event
```

## 6. CẤU TRÚC DATABASE

### 6.1 SQLite Schema Overview

```sql
-- Users and authentication
CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    password TEXT NOT NULL,
    display_name TEXT DEFAULT '',
    role TEXT DEFAULT 'user',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

-- House structure
CREATE TABLE rooms (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    icon TEXT DEFAULT 'home',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE devices (
    id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    FOREIGN KEY(room_id) REFERENCES rooms(id)
);

-- Sensor data (time-series)
CREATE TABLE sensor_data (
    id INTEGER PRIMARY KEY,
    room TEXT NOT NULL,
    type TEXT NOT NULL,
    value REAL NOT NULL,
    timestamp TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX idx_sensor_room_ts ON sensor_data(room, type, timestamp DESC);

-- Device states (current status)
CREATE TABLE device_status (
    room TEXT NOT NULL,
    device_id TEXT NOT NULL,
    is_on INTEGER DEFAULT 0,
    source TEXT DEFAULT 'unknown',
    updated_at TEXT DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (room, device_id)
);

-- System alerts
CREATE TABLE system_alerts (
    id INTEGER PRIMARY KEY,
    room TEXT,
    type TEXT,
    message TEXT,
    level TEXT DEFAULT 'info',
    is_resolved INTEGER DEFAULT 0,
    resolved_at TEXT,
    timestamp TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX idx_alert_unresolved ON system_alerts(is_resolved, timestamp DESC);

-- Notifications
CREATE TABLE notifications (
    id INTEGER PRIMARY KEY,
    type TEXT NOT NULL,
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    is_read INTEGER DEFAULT 0,
    room TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

-- Access control logs
CREATE TABLE login_logs (
    id INTEGER PRIMARY KEY,
    email TEXT,
    success INTEGER DEFAULT 0,
    ip_address TEXT,
    device_hint TEXT,
    user_agent TEXT,
    reason TEXT,
    timestamp TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE access_logs (
    id INTEGER PRIMARY KEY,
    room TEXT,
    uid TEXT,
    user_name TEXT,
    action TEXT,
    success INTEGER DEFAULT 0,
    duration_s INTEGER,
    timestamp TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX idx_access_room_ts ON access_logs(room, timestamp DESC);

-- Automation logs
CREATE TABLE automation_logs (
    id INTEGER PRIMARY KEY,
    room TEXT,
    scenario TEXT,
    actions TEXT,
    triggered_by TEXT,
    timestamp TEXT DEFAULT (datetime('now','localtime'))
);

-- RFID cards
CREATE TABLE rfid_cards (
    uid TEXT PRIMARY KEY,
    owner_name TEXT DEFAULT '',
    is_active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

-- Automation rules
CREATE TABLE automations (
    room_id TEXT PRIMARY KEY,
    enabled INTEGER DEFAULT 1,
    fan_threshold REAL,
    light_threshold REAL,
    gas_threshold REAL DEFAULT 600
);

-- Time schedules
CREATE TABLE schedules (
    id INTEGER PRIMARY KEY,
    room_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    action TEXT NOT NULL,
    time TEXT NOT NULL,
    enabled INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

-- OTA firmware logs
CREATE TABLE ota_logs (
    id INTEGER PRIMARY KEY,
    room TEXT,
    filename TEXT,
    url TEXT,
    status TEXT DEFAULT 'sent',
    triggered_by TEXT,
    timestamp TEXT DEFAULT (datetime('now','localtime'))
);

-- System monitoring
CREATE TABLE system_snapshots (
    id INTEGER PRIMARY KEY,
    wifi_ssid TEXT,
    wifi_status TEXT,
    room_count INTEGER DEFAULT 0,
    device_count INTEGER DEFAULT 0,
    alert_count INTEGER DEFAULT 0,
    timestamp TEXT DEFAULT (datetime('now','localtime'))
);
```

### 6.2 Database Optimization

- **WAL Mode**: `PRAGMA journal_mode = WAL` cho concurrent reads/writes
- **Indexes**: Strategic indexes trên timestamp và room columns
- **Foreign Keys**: Enabled với `PRAGMA foreign_keys = ON`
- **Partitioning**: Daily data partitioning cho sensor_data
- **Connection Pooling**: SQLite connection reuse với proper cleanup

## 7. LUỒNG HOẠT ĐỘNG CHI TIẾT

### 7.1 Luồng khởi động hệ thống

```
1. gateway_main.py start
   ├── init_db() - Tạo SQLite schema + admin user
   ├── start_workers() - Khởi động 4 background threads
   │   ├── Safety Watchdog (daemon=True)
   │   ├── Network Watchdog (daemon=True) 
   │   ├── Data Syncer (daemon=True)
   │   └── Automation Engine (daemon=False - main logic)
   └── start_api() - Khởi động Flask server
       └── Đăng ký tất cả blueprints (url_prefix='')
```

### 7.2 Luồng dữ liệu cảm biến

```
ESP32 Node                     MessageBus                  Workers
   │                               │                         │
   │ 1. Read sensors (DHT11,       │                         │
   │    CCS811, etc.)              │                         │
   │                               │                         │
   ├─── 2. Publish MQTT ─────────► │                         │
   │ "home/bedroom_01/sensors"     │                         │
   │ {temperature: 25.5, ...}      │                         │
   │                               │                         │
   │                               ├─── 3. MQTT callback ──► │
   │                               │     → Redis pubsub      │
   │                               │     "mqtt_inbound"      │
   │                               │                         │
   │                               │                         ├─── 4. Automation Engine
   │                               │                         │     → Process sensor data
   │                               │                         │     → Check thresholds
   │                               │                         │     → Send control commands
   │                               │                         │
   │                               │                         ├─── 5. Data Syncer
   │                               │                         │     → Buffer to Redis
   │                               │                         │     → Batch insert SQLite
   │                               │                         │     → Export to SD2
   │                               │                         │
   │                               │                         └─── 6. Safety Watchdog
   │                               │                               → Check gas/fire
   │                               │                               → Trigger alarms
```

### 7.3 Luồng điều khiển thiết bị

```
Web UI                          API                         MessageBus                  ESP32
   │                               │                             │                         │
   │ 1. User clicks button        │                             │                         │
   │    (Fan ON/OFF)               │                             │                         │
   │                               │                             │                         │
   ├─── 2. POST /control ────────► │                             │                         │
   │ {room, device_id, is_on}      │                             │                         │
   │                               │                             │                         │
   │                               ├─── 3. Validate auth ─────► │                         │
   │                               │     Check safety lock      │                         │
   │                               ├─── 4. Publish Redis ─────► │                         │
   │                               │     "device_commands"      │                         │
   │                               │                             │                         │
   │                               │                             ├─── 5. Automation Engine
   │                               │                             │     → Receive command
   │                               │                             │     → Check safety lock
   │                               │                             │     → Manual override cache
   │                               │                             │     → Publish MQTT outbound
   │                               │                             │
   │                               │                             ├─── 6. MessageBus ─────► │
   │                               │                             │     → MQTT publish      │
   │                               │                             │     "home/room/command" │
   │                               │                             │                         │
   │                               │                             │                         ├─── 7. ESP32 receives
   │                               │                             │                         │     → Parse command
   │                               │                             │                         │     → Control relay
   │                               │                             │                         │     → Send status back
```

### 7.4 Luồng cảnh báo an toàn

```
ESP32                          Safety Watchdog                Database                    Web UI
   │                                 │                           │                           │
   │ 1. Detect gas/fire             │                           │                           │
   │    via sensors                 │                           │                           │
   │                                 │                           │                           │
   ├─── 2. Send sensor data ──────► │                           │                           │
   │ MQTT "home/kitchen/sensors"    │                           │                           │
   │ {gas: 800, fire_detected: 0}   │                           │                           │
   │                                 │                           │                           │
   │                                 ├─── 3. Monitor sensors ─► │                           │
   │                                 │     Check thresholds     │                           │
   │                                 │                           │                           │
   │                                 ├─── 4. Danger detected ─► │                           │
   │                                 │     → Set safety lock    │                           │
   │                                 │     → Send alarm MQTT    │                           │
   │                                 │     → Save alert DB      │                           │
   │                                 │     → Push notification  │                           │
   │                                 │                           │                           │
   │                                 │                           ├─── 5. Insert alert ───► │
   │                                 │                           │     system_alerts       │                           │
   │                                 │                           │                           │
   │                                 │                           ├─── 6. Insert notification
   │                                 │                           │     notifications       │                           │
   │                                 │                           │                           │
   │                                 │                           ├─── 7. Publish realtime ─►
   │                                 │                           │     "realtime_data"     │                           │
   │                                 │                           │                           │
   │                                 │                           ├─── 8. Web receives ───► │
   │                                 │                           │     → Show red alert    │                           │
   │                                 │                           │     → Block controls    │                           │
   │                                 │                           │     → Sound alarm       │                           │
   │                                 │                           │                           │
   │                                 ├─── 9. Send hardware ───► │                           │
   │                                 │     control commands     │                           │
   │                                 │                           │                           │
   ├─── 10. ESP32 executes ────────► │                           │                           │
   │     → Turn on fan              │                           │                           │
   │     → Sound buzzer             │                           │                           │
   │     → Lock manual controls     │                           │                           │
```

### 7.5 Luồng automation

```
Sensor Data → Automation Engine → Decision Logic → Control Commands

1. Sensor input từ ESP32
2. Automation Engine nhận qua Redis pubsub
3. Kiểm tra rules trong database
4. So sánh với thresholds (nhiệt độ, gas)
5. Nếu vượt ngưỡng → gửi lệnh MQTT
6. ESP32 thực thi → thay đổi trạng thái
7. Log automation action
8. Push realtime update lên Web
```

### 7.6 Luồng scheduler

```
System Clock → Scheduler Loop → Time Check → Device Control

1. Đồng bộ giờ từ DS3231 RTC
2. Chạy vòng lặp mỗi giây
3. So sánh thời gian hiện tại với schedules
4. Khi đến giờ → gửi lệnh điều khiển
5. Kiểm tra safety lock trước khi thực thi
6. Log scheduler action
7. Tự động xóa schedule one-shot
```

## 8. CHỨC NĂNG VÀ TÍNH NĂNG HỆ THỐNG

### 8.1 Tính năng chính

#### **1. Multi-room Smart Home Control**
- **Bedroom**: Temperature/Humidity, CO2/TVOC monitoring, Fan/Light control
- **Kitchen**: Gas leak detection, Temperature monitoring, Safety valve control
- **Living Room**: Climate control, Lighting automation
- **Real-time Monitoring**: 5-second polling cho tất cả sensor data
- **Device Control**: Relay-based switching với physical button backup

#### **2. Advanced Safety System**
- **Gas Detection**: MQ-2/MQ-135 sensors với configurable thresholds
- **Fire Detection**: Flame sensors với immediate response
- **Safety Lock**: Automatic lockdown khi phát hiện nguy hiểm
- **Alarm System**: Buzzer alerts với mute functionality
- **Emergency Ventilation**: Auto fan activation khi gas detected

#### **3. Intelligent Automation**
- **IF/THEN Rules**: Temperature-based fan/light control
- **Time Scheduling**: Daily on/off schedules cho devices
- **Manual Override**: Temporary override với timeout
- **Safety Integration**: Automation respects safety locks

#### **4. Access Control System**
- **RFID Cards**: UID-based access control
- **Enrollment Mode**: Easy card registration
- **Access Logging**: Detailed entry/exit logs
- **Door Control**: Magnetic lock integration

#### **5. Comprehensive Monitoring**
- **Sensor Dashboard**: Real-time graphs và historical data
- **System Health**: WiFi, MQTT, Redis status monitoring
- **Alert Management**: Categorized alerts với resolution tracking
- **Data Export**: SD2 card backup với daily partitions

#### **6. User Management**
- **Authentication**: Secure login với session management
- **Role-based Access**: Admin/User permissions
- **Activity Logging**: Login attempts và access logs
- **Profile Management**: User information và preferences

#### **7. Network & Connectivity**
- **WiFi Hotspot**: Raspberry Pi làm router cho ESP32 nodes
- **Cloudflare Tunnel**: Secure public access without port forwarding
- **MQTT Communication**: Reliable device-to-gateway messaging
- **Offline Buffering**: ESP32 buffers data khi mất kết nối

#### **8. Firmware Management**
- **OTA Updates**: Over-the-air firmware updates
- **Version Control**: Firmware upload và deployment
- **Update Logging**: OTA success/failure tracking
- **Rollback Support**: Version management

### 8.2 Tính năng kỹ thuật

#### **Reliability & Resilience**
- **Redundant Storage**: SQLite + Redis + SD2 backup
- **Service Monitoring**: Automatic restart của critical services
- **Data Integrity**: WAL mode, transactions, foreign keys
- **Error Recovery**: Graceful handling của network failures

#### **Performance Optimization**
- **Asynchronous Processing**: Threading cho background tasks
- **Connection Pooling**: Efficient database connections
- **Buffering Strategy**: Redis buffers cho high-throughput data
- **Query Optimization**: Strategic indexes và partitioning

#### **Security Features**
- **API Authentication**: JWT-like tokens với expiration
- **CORS Protection**: Configurable cross-origin access
- **Input Validation**: Comprehensive API input sanitization
- **Access Logging**: Detailed audit trails

#### **Scalability Design**
- **Modular Architecture**: Independent workers và services
- **Message Bus Pattern**: Decoupled communication
- **Database Sharding**: Daily partitions cho time-series data
- **API Versioning**: RESTful design với future extensibility

#### **Monitoring & Debugging**
- **Health Endpoints**: System status APIs
- **Log Aggregation**: Unified logging system
- **Performance Metrics**: System snapshots và statistics
- **Real-time Alerts**: WebSocket-ready notifications

### 8.3 Đặc điểm hệ thống

#### **Edge Computing**
- ESP32 nodes process data locally
- Reduced latency cho real-time responses
- Offline operation capability
- Distributed intelligence

#### **Cloud Integration Ready**
- Cloudflare Tunnel cho secure remote access
- RESTful APIs cho third-party integration
- Webhook support (planned)
- IoT platform compatibility

#### **Energy Efficiency**
- Smart scheduling reduces unnecessary operation
- Sensor-based automation minimizes waste
- Low-power ESP32 microcontrollers
- Efficient polling strategies

#### **User Experience**
- Responsive web interface
- Real-time updates without page refresh
- Intuitive dashboards per room
- Mobile-friendly design

#### **Maintenance & Support**
- Automated setup scripts
- Comprehensive documentation
- Self-healing services
- Remote firmware updates

## 9. KẾT LUẬN

Hệ thống SmartHome này là một giải pháp IoT hoàn chỉnh với kiến trúc phân tán, an toàn cao và dễ mở rộng. Sự kết hợp giữa ESP32 edge devices, Raspberry Pi gateway, và modern web frontend tạo nên một hệ thống thông minh, đáng tin cậy cho các ứng dụng nhà ở.

**Điểm mạnh chính:**
- **Safety First**: Hệ thống an toàn với safety watchdog priority
- **Reliability**: Redundant storage và error recovery
- **Scalability**: Modular design cho easy expansion
- **User-friendly**: Intuitive interface và automated setup
- **Cost-effective**: Open-source components với low cost

**Ứng dụng thực tế:**
- Nhà ở thông minh
- Văn phòng nhỏ
- Greenhouse monitoring
- Industrial safety systems
- Elderly care facilities

Hệ thống sẵn sàng cho production deployment với comprehensive monitoring, logging, và maintenance capabilities.</content>
<parameter name="filePath">/home/pi/smarthome_prj/SYSTEM_ARCHITECTURE_DOCUMENTATION.md