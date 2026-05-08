# SmartHome Project - Comprehensive System Analysis

**Analysis Date**: May 8, 2026  
**Scope**: Complete workspace analysis (excluding Tai_lieu folder)  
**Status**: All source files extracted and analyzed

---

## TABLE OF CONTENTS
1. [System Architecture](#system-architecture)
2. [Component Interactions](#component-interactions)
3. [File Structure & Modules](#file-structure--modules)
4. [Data Flow Diagrams](#data-flow-diagrams)
5. [Key Functions & Entry Points](#key-functions--entry-points)
6. [Complete Log Messages Reference](#complete-log-messages-reference)
7. [State Management](#state-management)
8. [Database Schema](#database-schema)
9. [API Endpoints](#api-endpoints)
10. [Error Conditions & Edge Cases](#error-conditions--edge-cases)

---

## SYSTEM ARCHITECTURE

### Overview
```raw
┌─────────────────────────────────────────────────────────────────┐
│                    SMARTHOME ARCHITECTURE                       │
└─────────────────────────────────────────────────────────────────┘

                          ┌──────────────────┐
                          │   FIREBASE       │
                          │ - Firestore      │
                          │ - RTDB           │
                          │ - Auth           │
                          │ - Storage        │
                          └────────┬─────────┘
                                   │
                ┌──────────────────┴──────────────────┐
                │                                     │
        ┌───────▼────────────┐              ┌────────▼───────────┐
        │  WEB DASHBOARD     │              │  GATEWAY (Pi)      │
        │ - HTML/CSS/JS      │              │ - Flask API        │
        │ - Firebase Config  │              │ - MQTT Broker      │
        │ - Realtime Updates │              │ - Redis Pub/Sub    │
        │ - Admin Panel      │              │ - SQLite DB        │
        └────────┬───────────┘              └────────┬───────────┘
                 │                                   │
                 │ (HTTPS/SocketIO)    (MQTT 1883)   │
                 │                         ▲         │
                 │                         │         │
        ┌────────┴─────────────────────────┴─────────┴──────────┐
        │                                                       │
   ┌────▼────┐  ┌─────────┐  ┌─────────────┐  ┌──────────────┐
   │ BEDROOM  │  │ KITCHEN │  │ LIVING ROOM │  │ ESP32 Nodes  │
   │ ESP32-01 │  │ESP32-02 │  │  ESP32-03   │  │              │
   ├──────────┤  ├─────────┤  ├─────────────┤  │  Features:   │
   │ - HDC1080│  │- HDC1080│  │ - HDC1080   │  │ - Temp/Hum   │
   │ - CCS811 │  │- CCS811 │  │ - CCS811    │  │ - Gas Sensor │
   │ - Fan    │  │- Fan    │  │ - Fan       │  │ - CO2 Sensor │
   │ - Light  │  │- Light  │  │ - Light     │  │ - Relays     │
   │ - Relays │  │- Relays │  │ - Relays    │  │ - RFID       │
   └──────────┘  └─────────┘  └─────────────┘  └──────────────┘
        │            │             │
        └────────────┴─────────────┘
             MQTT Topics
             home/{room}/{category}
```

### Technology Stack
- **Gateway**: Python 3 (Flask, PubSubClient, Firebase Admin SDK)
- **Firmware**: Arduino C++ (ESP32, ArduinoJson, PubSubClient)
- **Frontend**: HTML5/CSS3/JavaScript (Firebase SDK)
- **Backend**: SQLite (local), Firestore + RTDB (cloud)
- **Messaging**: MQTT 1883 (local), Firebase Realtime DB (cloud)
- **Cache**: Redis (local pub/sub only, no persistence)

---

## COMPONENT INTERACTIONS

### Message Flow Architecture
```raw
ESP32 (Device)
    ↓ MQTT Publish
    │ Topic: home/{room}/{category}
    │ Payload: {"sensor": value, "ts": timestamp, ...}
    ↓
MQTT Broker (Gateway:1883)
    ↓
MessageBus (bridge/message_bus.py)
    ├─ MQTT → Redis CH_INBOUND (on_message)
    │
    ├─ Workers Listen (CH_INBOUND):
    │  ├─ automation_engine.py     (commands, automation)
    │  ├─ safety_watchdog.py       (alerts, safety)
    │  ├─ firebase_sync.py         (RTDB/Firestore)
    │  ├─ data_syncer.py           (SQLite history)
    │  └─ network_watchdog.py      (WiFi status)
    │
    ├─ Workers Publish (CH_OUTBOUND):
    │  ├─ "device_commands"        → Redis
    │  ├─ "realtime_data"          → SocketIO, Web
    │  ├─ "safety_alert"           → Firestore
    │  └─ MQTT (via outbound_loop)
    │
    └─ Redis → MQTT (outbound_loop)
         ↓
    ESP32 (Device Command Processing)
```

### Worker Responsibilities

| Worker | Purpose | Input | Output |
|--------|---------|-------|--------|
| **automation_engine** | Process sensor data, trigger automations | mqtt_inbound, automation rules | device_commands, logs |
| **safety_watchdog** | Monitor gas/fire, apply safety locks | mqtt_inbound, automation rules | safety_alert, alarm commands |
| **firebase_sync** | Sync data to Firebase cloud | mqtt_inbound, device_status, alerts | RTDB, Firestore collections |
| **data_syncer** | Persist sensor history to SD2 | event_queue, redis buffer | CSV exports, daily DBs |
| **network_watchdog** | Maintain WiFi/Hotspot, NTP sync | system checks | wifi_status, network events |

---

## FILE STRUCTURE & MODULES

### GATEWAY/ (Main Server)
```raw
GATEWAY/
├── gateway_main.py                    [ENTRY POINT]
│   └─ Initializes DB, starts workers, launches Flask API
│
├── app/
│   └── main.py                        [Flask app setup]
│       └─ SocketIO config, CORS headers
│
├── app/api/routes/
│   └── all_routes.py                  [ALL API ENDPOINTS]
│       ├─ auth_bp          (login, register, logout, session mgmt)
│       ├─ sensors_bp       (latest, history, dashboard, chart)
│       ├─ devices_bp       (device_status, control)
│       ├─ automation_bp    (CRUD automations, schedules)
│       ├─ logs_bp          (system logs, access logs)
│       ├─ rfid_bp          (RFID card mgmt, enrollment)
│       ├─ wifi_bp          (WiFi status, network config)
│       ├─ ota_bp           (firmware updates)
│       └─ system_bp        (health, clock, safety status)
│
├── bridge/
│   └── message_bus.py                 [MQTT ↔ REDIS BRIDGE]
│       ├─ _setup_mqtt()   (connect, subscribe, on_message)
│       ├─ connect()        (MQTT connect + retry logic)
│       ├─ _outbound_loop() (Redis → MQTT forwarder)
│       └─ publish_mqtt()   (Public API for device commands)
│
├── workers/
│   ├── firebase_sync.py               [CLOUD SYNC WORKER]
│   │   ├─ AutoProvisioner             (auto-create rooms/devices in Firebase)
│   │   ├─ UplinkStream                (sensor readings → RTDB/Firestore)
│   │   ├─ CommandDispatcher           (command listeners → device updates)
│   │   └─ run()                       (main loop)
│   │
│   ├── automation_engine.py           [AUTOMATION LOGIC]
│   │   ├─ dispatch_command()          (priority-based command dispatch)
│   │   ├─ handle_inbound()            (MQTT message processing)
│   │   ├─ handle_automation()         (sensor threshold triggers)
│   │   ├─ scheduler_loop()            (time-based schedules)
│   │   └─ run()                       (main pub/sub loop)
│   │
│   ├── safety_watchdog.py             [SAFETY MONITORING]
│   │   ├─ _save_alert()               (gas/fire detection → DB + Redis)
│   │   ├─ _set_safety_lock()          (safety_lock:{room} Redis key)
│   │   ├─ _trigger_safety_action()    (fan on, alarm, buzzer)
│   │   └─ run()                       (main watch loop)
│   │
│   ├── data_syncer.py                 [PERSISTENCE]
│   │   ├─ SD2Manager                  (SD2 mount management, Degraded Mode)
│   │   ├─ StorageLayer                (daily DB rotation)
│   │   ├─ SyncWorker                  (buffer flush, CSV export)
│   │   ├─ flush_sensor_data()         (→ SQLite)
│   │   ├─ flush_events()              (→ CSV/SQLite)
│   │   └─ run()                       (main sync loop)
│   │
│   └── network_watchdog.py            [NETWORK MANAGEMENT]
│       ├─ detect_interfaces()         (dual WiFi interface setup)
│       ├─ ensure_hotspot()            (AP mode on wlan0)
│       ├─ check_internet()            (ping 8.8.8.8)
│       ├─ get_wifi_status()           (connected SSID via nmcli)
│       ├─ sync_time_ntp()             (NTP sync)
│       └─ run()                       (main watch loop)
│
├── storage/
│   ├── db_schema.sql                  [LOCAL SQLITE SCHEMA]
│   │   └─ Tables: users, sessions, rooms, devices, sensor_data,
│   │          device_status, system_alerts, automations, schedules, etc.
│   └── /                              (daily data_YYYY-MM-DD.db files)
│
├── scripts/
│   ├── mount_sd2.sh                   (SD2 mount helper)
│   ├── 99-sd2-mount.rules             (udev rules)
│   └── sd2-mount@.service             (systemd mount service)
│
├── requirements.txt                   [Python dependencies]
├── .env.example                       [Environment template]
├── start.sh                           [Boot script]
├── setup.sh                           [Initial setup]
└── run.sh                             [Run script]
```

### BEDROOM/, KITCHEN/, LIVINGROOM/ (ESP32 Firmware)
```raw
Each room has identical structure:
├── platformio.ini                     [PlatformIO config]
├── src/
│   ├── main.cpp                       [ENTRY POINT]
│   │   └─ setup() → setupHardware(), setupNetwork()
│   │   └─ loop() → maintainConnection(), loopHardware()
│   │
│   ├── Config.hpp                     [HARDCODED SETTINGS]
│   │   ├─ #define WIFI_SSID "SmartHome_Hub"
│   │   ├─ #define ROOM_BEDROOM "bedroom_01"
│   │   ├─ #define PIN_RELAY_FAN 4, PIN_LIGHT 5
│   │   ├─ #define MQTT_SERVER "10.42.0.1"
│   │   └─ MQTT topics (commands, sensors, status)
│   │
│   ├── NetworkManager.hpp             [WIFI + MQTT]
│   │   ├─ setupNetwork()              (WiFi connect, MQTT setup)
│   │   ├─ maintainConnection()        (WiFi/MQTT reconnect logic)
│   │   ├─ sendMQTT()                  (publish to topic)
│   │   ├─ performOTA()                (firmware update from URL)
│   │   └─ httpUpdate callbacks        (progress, reboot)
│   │
│   └── HardwareControl.hpp            [SENSORS + ACTUATORS]
│       ├─ setupHardware()             (I2C, pins, button task)
│       ├─ loopHardware()              (sensor read, data send, buffer)
│       ├─ processCommand()            (MQTT command handling)
│       ├─ taskButtonMonitor()         (FreeRTOS button task)
│       ├─ sendDeviceStatus()          (relay/light state)
│       ├─ flushSensorBuffer()         (resend buffered data)
│       └─ Sensors: HDC1080 (temp/hum), CCS811 (CO2/TVOC)
│
├── include/ & lib/                    [Arduino libraries]
└── test/                              [Unit tests]
```

### NhaThongMinh-Web/ (Web Dashboard)
```raw
NhaThongMinh-Web/
└── NhaThongMinh-Web/                  [WEB APP ROOT]
    ├── index.html                     [LANDING PAGE]
    ├── admin.html                     [ADMIN DASHBOARD]
    ├── login.html                     [LOGIN/REGISTER]
    ├── dashboard-*.html               [ROOM DASHBOARDS]
    ├── settings.html                  [SETTINGS PAGE]
    ├── notifications.html             [ALERTS/LOGS]
    │
    ├── css/
    │   ├── index.css                  (landing page styles)
    │   ├── login.css                  (auth forms)
    │   ├── admin.css                  (admin UI)
    │   ├── dashboard.css              (room dashboards)
    │   ├── components.css             (reusable components)
    │   └── *.css                      (other pages)
    │
    ├── js/
    │   ├── firebase-config.js         [FIREBASE INIT]
    │   │   ├─ initializeApp()         (Firebase + RTDB setup)
    │   │   └─ Export auth, db, rtdb, collection refs
    │   │
    │   ├── roomService.js             [FIRESTORE API WRAPPER]
    │   │   ├─ getRoomsFresh()         (BUG-H-03: fallback for missing userId)
    │   │   ├─ getDevices()
    │   │   ├─ sendDeviceCommand()     (BUG-H-02: room + roomId)
    │   │   ├─ setAutoMode()           (release manual override)
    │   │   ├─ subscribeSensors()      (realtime listeners)
    │   │   └─ saveAutomation(), saveSchedule()
    │   │
    │   ├── index.js                   [LANDING PAGE LOGIC]
    │   │   ├─ onAuthStateChanged()    (check user login status)
    │   │   ├─ updateUIForLoggedInUser()
    │   │   ├─ updateUIForGuest()
    │   │   ├─ handleAdminClick()      (popup for non-logged users)
    │   │   └─ toggleMobileMenu()
    │   │
    │   ├── login.js                   [AUTH FORMS]
    │   │   ├─ handleLogin()           (Firebase signInWithEmailAndPassword)
    │   │   ├─ handleRegister()        (Firebase createUserWithEmailAndPassword)
    │   │   ├─ saveUserProfile()       (Firestore users collection)
    │   │   └─ Error handling
    │   │
    │   ├── admin.js                   [ADMIN DASHBOARD]
    │   │   ├─ initializeDashboard()   (load rooms, devices, logs)
    │   │   ├─ monitorRealtimeAlerts() (Firestore listener for system_alerts)
    │   │   ├─ renderAlertsList()      (display alerts table)
    │   │   ├─ markAlertResolved()     (update Firestore)
    │   │   └─ Export CSV, etc.
    │   │
    │   ├── dashboard-bedroom.js       [ROOM DASHBOARD]
    │   │   ├─ loadAllData()           (devices, automation, sensors)
    │   │   ├─ loadAndRenderDevices()  (build device toggle UI)
    │   │   ├─ handleDeviceToggle()    (roomService.sendDeviceCommand)
    │   │   ├─ loadAutomationSettings()
    │   │   ├─ saveAutomationSettings()
    │   │   ├─ setupRealTimeListeners()
    │   │   ├─ initializeChartsWithRealData()
    │   │   └─ Chart.js: temp, humidity, CO2
    │   │
    │   ├── dashboard-kitchen.js       [Similar to bedroom]
    │   ├── dashboard-livingroom.js    [Similar to bedroom]
    │   │
    │   ├── settings.js                [USER/SYSTEM SETTINGS]
    │   │   ├─ loadSettings()
    │   │   ├─ saveSettings()
    │   │   └─ WiFi config, etc.
    │   │
    │   └── notifications.js           [ALERTS FULL PAGE]
    │       ├─ setupFullNotificationSystem()
    │       ├─ applyFilters()
    │       └─ renderFullList()
    │
    ├── videos/                        [Demo videos]
    ├── images/                        [UI assets]
    └── flowchart/                     [PlantUML diagrams]
        ├── *.puml                     (system flow diagrams)
        └── *.html                     (rendered diagrams)
```

---

## DATA FLOW DIAGRAMS

### 1. Sensor Reading → Cloud Sync
```raw
ESP32 (Bedroom)
    │ Every 5 seconds:
    │ HDC1080 read temp/hum
    │ CCS811 read CO2/TVOC
    ↓
MQTT Publish (home/bedroom_01/sensors)
    │ {"temperature": 25.5, "humidity": 60, "co2": 420, ...}
    ↓
Gateway MQTT Broker
    │ MessageBus._on_message()
    ↓
Redis CH_INBOUND (mqtt_inbound)
    │ {"topic": "home/bedroom_01/sensors", "payload": {...}, "ts": timestamp}
    ↓
   ┌─────────────────────────────────────────┐
   │ Workers Subscribe (CH_INBOUND)          │
   └─────────────────────────────────────────┘
    ├─ automation_engine.run()
    │   └─ update_cached_sensors()
    │      └─ CACHED_SENSORS[bedroom_01] = {temperature: 25.5, ...}
    │      └─ Check thresholds → trigger automations
    │      └─ Publish "realtime_data" → SocketIO
    │
    ├─ safety_watchdog.run()
    │   └─ get_cached_sensors()
    │   └─ Check gas > threshold
    │   └─ If dangerous → _save_alert() + _trigger_safety_action()
    │
    ├─ firebase_sync.run()
    │   └─ UplinkStream._on_mqtt_inbound()
    │   └─ Parse sensor → mark firebase_synced=0 in SQLite
    │   └─ Update RTDB live/bedroom_01/sensors/temperature
    │   └─ Update RTDB live/bedroom_01/meta/last_seen
    │
    └─ data_syncer.run()
        └─ Flush sensor_buffer
        └─ Write to daily DB (data_2026-05-08.db)
        └─ At 2 AM: CSV export

    Parallel: SocketIO → Web Browser
    Parallel: RTDB → Firebase SDK (Web)
    │
    Web Dashboard (Chrome)
    └─ Chart.js updates in realtime
       └─ Show current temp, humidity, CO2 on dashboard
```

### 2. Device Control Flow (Manual)
```raw
Web UI: User clicks "Fan ON" toggle
    │
    ↓
dashboard-bedroom.js: handleDeviceToggle()
    │
    ↓
roomService.sendDeviceCommand(
    roomId="bedroom_01",
    deviceId="fan_bd_1",
    isOn=true
)
    │
    ↓
Firestore: Add doc to /commands collection
    │ {
    │   action: "turn_on",
    │   room: "bedroom_01",
    │   device: "fan_bd_1",
    │   requestedBy: user.uid,
    │   status: "pending"
    │ }
    │
    ↓
firebase_sync.py: CommandDispatcher listener
    │ Detects new command in /commands
    │ Checks safety_lock, manual_override, schedule
    │ If all clear: dispatch_command("web", room, device, action)
    │ Publish to Redis: device_commands
    │
    ↓
automation_engine.run(): Listens device_commands
    │ dispatch_command() with priority check
    │ Publish MQTT: home/bedroom_01/command
    │ {
    │   device: "fan_bd_1",
    │   action: "turn_on",
    │   source: "web"
    │ }
    │
    ↓
ESP32 (Bedroom): mqttCallback()
    │ Topic: home/bedroom_01/command
    │ Action: turn_on, device: fan_bd_1
    │
    ↓
HardwareControl.processCommand()
    │ digitalWrite(PIN_RELAY_FAN, HIGH)
    │ fanState = true
    │ saveState() → preferences
    │ Send response MQTT: home/bedroom_01/status
    │ {
    │   deviceId: "fan_bd_1",
    │   isOn: true,
    │   source: "esp32"
    │ }
    │
    ↓
Gateway receives: home/bedroom_01/status
    │ automation_engine: Mark device as on in cache
    │ firebase_sync: Update Firestore /rooms/bedroom_01/devices/fan_bd_1
    │
    ↓
Web Dashboard: Realtime Firestore listener
    │ onSnapshot(/rooms/bedroom_01/devices/fan_bd_1)
    │ Update UI: toggle now shows checked
```

### 3. Safety Alert Flow (Gas Detection)
```raw
ESP32 (Kitchen): Gas sensor reads 750 ppm (> threshold 600)
    │
    ↓
MQTT Publish: home/kitchen_01/sensors
    │ {"gas": 750, "temperature": 28, ...}
    │
    ↓
safety_watchdog.run()
    │ get_cached_sensors("kitchen_01") → gas = 750
    │ is_dangerous = (750 > 600) ✓
    │ was_dangerous (prev state) = false
    │ → First time detecting!
    │
    ├─ _trigger_safety_action("kitchen_01", "gas", 750)
    │   ├─ Publish MQTT: home/kitchen_01/command
    │   │   │ {"action": "turn_on", "device": "fan_kt_1", "source": "safety"}
    │   │   │ (Fan forced on)
    │   │   │
    │   │   └─ {"action": "buzz_alarm", "type": "gas", "value": 750, "source": "safety"}
    │   │      (Alarm buzzer)
    │   │
    │   └─ _set_safety_lock("kitchen_01", True)
    │       └─ Redis.setex("safety_lock:kitchen_01", 360, "1")
    │          (Lock for 6 minutes)
    │
    └─ _save_alert("kitchen_01", "gas", "RÒ RỈ KHÍ GAS! 750 ppm - Phòng: kitchen_01")
        ├─ SQLite: INSERT into system_alerts
        ├─ SQLite: INSERT into notifications
        ├─ Redis publish "realtime_data" event
        │   └─ SocketIO → Web: Show alert banner
        │
        └─ Redis publish "safety_alert" channel
            └─ firebase_sync.py: UplinkStream._on_alert()
                ├─ Firestore: /system_alerts/alerts_2026-05-08/kitchen_01_1234567
                │  {
                │    type: "gas",
                │    message: "RÒ RỈ KHÍ GAS! 750 ppm",
                │    level: "critical",
                │    room: "kitchen_01",
                │    timestamp: "2026-05-08T15:30:45Z"
                │  }
                │
                └─ Firestore: /rooms/kitchen_01/meta/last_alert = timestamp

Web Notifications:
    └─ Admin sees red alert banner
    └─ Email notification (if enabled)
    └─ Push notification (if enabled)
```

### 4. Schedule Execution
```raw
Schedule Rule (SQLite):
    │ id=1, room_id="bedroom_01", device_id="light_bd_1",
    │ action="turn_on", time="07:00", enabled=1

automation_engine.scheduler_loop()
    │ Every 1 second:
    │ Check if current_time (HH:MM) matches any schedule
    │
    ├─ 06:59 AM: Waiting...
    ├─ 07:00 AM: MATCH!
    │   │
    │   ├─ Check dispatch_command conditions
    │   │   ├─ Is device in manual mode? NO
    │   │   ├─ Is there active safety_lock? NO
    │   │   ├─ Is schedule active right now? YES (within 1h of 07:00)
    │   │   └─ → Proceed
    │   │
    │   ├─ dispatch_command("schedule", room, device, "turn_on")
    │   │
    │   └─ SCHEDULE_LAST_RUN[sched_id] = "2026-05-08 07:00:00"
    │       (Track to prevent duplicate runs same day)
    │
    └─ Set scheduled device on
```

---

## COMPLETE LOG MESSAGES REFERENCE

### Gateway Logs

#### gateway_main.py
```raw
[MAIN] DB schema applied
[MAIN] Warning: db_schema.sql not found
[MAIN] DB init complete
[MAIN] Admin account created (SHA256 hash)
[MAIN] FirebaseSync worker started
[MAIN] firebase_sync import error: <error>
[MAIN] Firebase cred not found at <path> — FirebaseSync disabled
[MAIN] All workers started
[BRIDGE] Realtime bridge started
[BRIDGE] emit error: <error>
[API] Registered /api: <blueprint_name>
[API] Error registering /api/<blueprint_name>: <error>
[WS] Client connected
[WS] Client disconnected
[MAIN] Gateway LIVE → http://0.0.0.0:5000/
[MAIN] API prefix: http://0.0.0.0:5000/api/
(55 characters banner line)
```

#### bridge/message_bus.py
```raw
[BUS] MessageBus started
[BUS] MQTT connected & subscribed
[BUS] MQTT disconnected (rc=<code>), will reconnect...
[BUS] MQTT connect failed (<error>), retry in <N>s...
[BUS] inbound parse error: <error>
[BUS] Outbound loop started
[BUS] outbound error: <error>
```

#### workers/firebase_sync.py
```raw
[firebase_sync] Sử dụng Service Account: <path>
[firebase_sync] Lỗi đọc file service account: <error>
[firebase_sync] Dùng ADC (Application Default Credentials).
[firebase_sync] Không thể khởi tạo Firebase: <error> → CRITICAL
[firebase_sync] Không đọc được rooms từ SQLite: <error> — dùng DEFAULT_ROOMS
[firebase_sync] Không đọc devices từ SQLite (room=<room_id>): <error>
[firebase_sync] SQLite read error: <error>
[firebase_sync] SQLite mark_synced error: <error>
[firebase_sync] === AUTO-PROVISIONING BẮT ĐẦU ===
[firebase_sync] RTDB: Tạo node live/<room_id>
[firebase_sync] RTDB provision room <room_id> failed: <error>
[firebase_sync] Firestore provision room <room_id> failed: <error>
[firebase_sync] Firestore provision device <room_id>/<device_id> failed: <error>
[firebase_sync] Firestore provision system docs failed: <error>
[firebase_sync] RTDB update_sensor_bulk [<room_id>] error: <error>
[firebase_sync] RTDB set_room_offline [<room_id>] error: <error>
[firebase_sync] RTDB heartbeat error: <error>
[firebase_sync] === AUTO-PROVISIONING HOÀN TẤT (<N> rooms) ===
```

#### workers/automation_engine.py
```raw
[AUTO] Cache loaded: <N> rules, <N> schedules
[DISPATCH] BLOCKED (safety_lock): <source> → <room>/<device> <action>
[DISPATCH] BLOCKED (manual_override): <source> → <device>
[DISPATCH] BLOCKED (schedule_active_60m): automation → <device>
[DISPATCH] SENT [<source>|p=<priority>]: <room>/<device> → <action>
[DISPATCH] MANUAL_STATE TTL expired for <device_id> → auto mode restored
[AUTO] Auto <device_type> trigger: <room_id> → <action> (<value>, <threshold>)
[AUTO] Schedule FIRED: <device_id> → turn_on at <time>
[ENROLL] <room_id> enrollment mode: ACTIVE (timeout: <N>s)
[RFID] Card <uid> enrolled as "<name>" in <room_id>
[RFID] Auth ALLOWED: <room_id> card <uid> (user: <name>)
[RFID] Auth DENIED: <room_id> card <uid>
```

#### workers/safety_watchdog.py
```raw
[WATCHDOG] Safety watchdog started (thread-safe sensor read + Firestore alert sync)
[WATCHDOG] <room_id> muted by user (manual)
[WATCHDOG] <room_id> smart_muted (alert read) — buzzer off for <N>s
[WATCHDOG] DANGER DETECTED <room_id>: PHÁT HIỆN LỬA! | RÒ RỈ KHÍ GAS! <ppm> ppm
[WATCHDOG] <room_id> safety lock refresh (still dangerous)
[WATCHDOG] <room_id> danger CLEARED — lock removed, buzzer off
[WATCHDOG] mute listener error: <error>
```

#### workers/data_syncer.py
```raw
[SYNCER] SD2 write-test failed: <error>
[SYNCER] Mount script failed: <error>
[SYNCER] DB rotated → <path> (mode: <READY|DEGRADED>)
[SYNCER] Flushed <N> sensor rows
[SYNCER] flush_sensor DB error: <error> — will retry next cycle
[SYNCER] flush_events error: <error>
[SYNCER] snapshot error: <error>
[SYNCER] SyncWorker starting...
[SYNCER] Waiting for SD2 (timeout <N>s before degraded mode)...
[SYNCER] ✅ SD2 ready at <path>
[SYNCER] ✅ SD2 hot-plugged — switching back from degraded mode
[SYNCER] CSV exported: <path>
[SYNCER] Export error: <error>
[SYNCER] SyncWorker loop error: <error>
[SYNCER] SyncWorker running (storage: <path>)
[SYNCER] Buffer full (<N>) — dropping sample <room>/<type>
[SYNCER] Buffer warn: <N> items pending flush
```

#### workers/network_watchdog.py
```raw
[NET] Dual interface detected: Hotspot=wlan0, Uplink=wlan1
[NET] Single interface: wlan0 (AP+STA concurrent mode)
[NET] Hotspot 'SmartHome_Hub' activated on wlan0 at 10.42.0.1/24
[NET] Hotspot error on <iface>: <error>
[NET] Internet status: <ONLINE|OFFLINE>
[NET] WiFi Status: ssid=<SSID>, signal=<dBm>, state=<STATE>
[NET] NTP sync successful: <timestamp>
[NET] NTP sync failed: <error>
```

### ESP32 Firmware Logs (Serial Output)

#### NetworkManager.hpp
```raw
--- BAT DAU KET NOI (DIRECT HOTSPOT MODE) ---
Loi cau hinh IP Tinh!
Dang ket noi vao Hotspot: SmartHome_Hub
.........................
KET NOI WIFI THANH CONG!
IP ESP32: <IP>
Gateway: <IP>
Signal: <dBm>
THẤT BẠI. Status: <code>
 Mat Wifi. Dang ket noi lai...
Connecting to MQTT Broker (10.42.0.1)...  Broker Connected!
 Subscribed: home/bedroom_01/command
 Failed, rc=<code>
[OTA] Starting update from: <URL>
[OTA] Disconnecting MQTT...
[OTA] Progress: <N>%
[OTA] FAILED (<code>): <error_string>
[OTA] No update available.
[OTA] OK!
[OTA] Reconnecting MQTT...
```

#### HardwareControl.hpp
```raw
--- BEDROOM HARDWARE SETUP ---
 Button Task Started
CCS811 skipped
 [TASK] Fan Button Pressed!
 [TASK] Light Button Pressed!
[SYNC] Đang đồng bộ <N> bản tin cũ...
[SYNC] Đã đồng bộ xong!
[OFFLINE] Buffered data. Size: <current>/<max>
[OFFLINE] Buffer full, rotating...
```

### Web Frontend Logs (Browser Console)

#### firebase-config.js
```raw
Firebase initialized
```

#### index.js
```raw
Trang chủ đã tải xong.
 Người dùng đã đăng nhập: <email>
Người dùng chưa đăng nhập (Khách)
 Đăng xuất thành công
```

#### login.js
```raw
 Đã đăng nhập: <email>
Login Error: <firebase_error_code>
Register Error: <firebase_error_code>
```

#### roomService.js
```raw
Error getting room details: <error>
Error getting fresh rooms: <error>
Không tìm thấy rooms với userId. Fallback: lấy tất cả rooms.
   → Hãy cấu hình PI_OWNER_UID trong .env của Raspberry Pi.
Rooms loaded (by userId): <count>
Rooms loaded (fallback, all): <count>
Error updating room: <error>
Error getting devices: <error>
Command sent: <ON|OFF> → <room>/<device> [<cmd_id>]
Error sending device command: <error>
Auto mode restored: <room>/<device> [<cmd_id>]
Error setting auto mode: <error>
Error adding device: <error>
Update count error: <error>
Automation saved: <doc_id>
Error saving automation: <error>
```

#### dashboard-*.js
```raw
Lỗi tải dữ liệu: <error>
Lệnh đã gửi: <device_name> <bật|tắt>
Không thể gửi lệnh!
```

#### admin.js / settings.js / notifications.js
(Various Firebase operation logs)

---

## STATE MANAGEMENT

### Critical In-Memory State Variables (Python)

#### automation_engine.py
```python
CACHED_AUTOMATIONS: dict         # {room_id: {rule_data}}
CACHED_SCHEDULES: list           # [{id, room_id, device_id, time, action, enabled}]
CACHED_DEVICE_STATES: dict       # {"room_id_device_id": on|off}
MANUAL_STATE: dict               # {device_id: {"mode": "manual", "set_at": datetime}}
CACHED_SENSORS: dict             # {room_id: {sensor_type: value}}
PENDING_COMMANDS: dict           # {cmd_id: {"ack": timestamp}}
ENROLLMENT_STATE: dict           # {"active": bool, "start_time": timestamp, "pending_name": ""}
SCHEDULE_LAST_RUN: dict          # {sched_id: "YYYY-MM-DD HH:MM"}
DEVICE_LAST_SWITCH: dict         # {"room_device": timestamp}
_SENSORS_LOCK: threading.RLock   # Thread-safe lock for CACHED_SENSORS
```

#### safety_watchdog.py
```python
SAFETY_STATE: dict               # {room_id: {
                                 #   "muted": bool,
                                 #   "mute_time": datetime,
                                 #   "was_dangerous": bool,
                                 #   "last_alert": datetime,
                                 #   "smart_muted": bool,
                                 #   "smart_mute_time": datetime,
                                 #   "last_lock_refresh": datetime
                                 # }}
CACHED_SENSORS: dict             # Same as automation_engine.CACHED_SENSORS
```

#### data_syncer.py
```python
SENSOR_BUFFER: dict              # {room_type: [readings]}  (in-memory)
StorageLayer._conn: sqlite3.Connection
StorageLayer._db_path: str       # Path to daily data_YYYY-MM-DD.db
SD2Manager._ready: threading.Event
SD2Manager._degraded: threading.Event
SD2Manager._degraded_at: timestamp or None
```

### Redis State (Persistent Pub/Sub + Temporary Keys)

```python
# Pub/Sub Channels (ephemeral, no persistence)
"mqtt_inbound"                   # MQTT → workers
"mqtt_outbound"                  # workers → MQTT
"realtime_data"                  # sensor/event broadcasts
"safety_alert"                   # gas/fire alerts for Firebase
"device_commands"                # device control commands
"device_status"                  # device state changes
"alert_commands"                 # mute/unmute alerts
"event_queue"                    # event persistence buffer
"automation_commands"            # automation rule updates
"command_ack"                    # command acknowledgments

# Temporary Keys (setex)
"safety_lock:room_id"            # Set when safety condition detected (TTL ~360s)
"session:token"                  # User session (TTL = SESSION_EXPIRE)
"sensor:room_id"                 # Cached latest sensor data (TTL ~60s)
"mqtt_pending_queue"             # Queued MQTT publishes (no TTL)
"token_blacklist"                # Revoked tokens (TTL = SESSION_EXPIRE)
"active_alert:room_id"           # Current active alert (TTL = SAFETY_MUTE_TIMEOUT * 2)

# No persistence — all pub/sub channels are lost on gateway restart
```

### Firebase Cloud State (Persistent)

```javascript
// Collections & Documents Structure
users/
  {uid}/ -> {email, fullName, createdAt, role}

rooms/
  bedroom_01/          -> {name, icon, userId, createdAt, deviceCount, online}
    devices/
      fan_bd_1/        -> {name, type, status, isOn, icon, details}
      light_bd_1/      -> similar
    sensors/
      temperature/     -> {value, unit, ts}
      humidity/        -> {value, unit, ts}
      co2/             -> {value, unit, ts}
  kitchen_01/          -> similar
  living_room_01/      -> similar

automations/
  {userId}_{room_id}/ -> {roomId, userId, fan_threshold, light_threshold, gas_threshold, ...}

schedules/
  {userId}_{room_id}_{device_id}_{timestamp}/ -> {action, time, enabled, ...}

commands/
  {auto_id}/          -> {action, room, device, requestedBy, status, timestamp}

system_alerts/
  alerts_{date}/
    {room}_{id}/      -> {type, message, level, room, timestamp}

live/ (RTDB)
  bedroom_01/
    sensors/
      temperature/    -> {value, ts, unit}
      humidity/       -> {value, ts, unit}
      gas/            -> {value, ts, unit}
      co2/            -> {value, ts, unit}
      fire_detected/  -> {value, ts}
    meta/
      room_name/      -> string
      online/         -> boolean
      last_seen/      -> ISO timestamp
      pi_version/     -> int
```

### SQLite Local State (Local Storage)

```sql
-- User & Session
users (id, email, password_hash, display_name, role, created_at)
sessions (token, user_id, expires_at)

-- Room Structure
rooms (id, name, icon, created_at)
devices (id, room_id, name, type)

-- Sensor Data (persisted)
sensor_data (id, room, type, value, timestamp, firebase_synced)

-- Device Status (current snapshot)
device_status (room, device_id, is_on, source, updated_at)

-- System State
system_alerts (id, room, type, message, level, is_resolved, timestamp)
notifications (id, type, title, message, is_read, room, created_at)
automations (room_id, enabled, fan_threshold, light_threshold, gas_threshold, co2_threshold)
schedules (id, room_id, device_id, action, time, enabled, last_run, created_at)

-- Access Control
rfid_cards (uid, owner_name, is_active, created_at)
access_logs (id, room, uid, user_name, action, success, timestamp)

-- Firmware
ota_logs (id, room, filename, url, status, triggered_by, timestamp)

-- Audit
login_logs (id, email, success, ip_address, device_hint, user_agent, timestamp)
automation_logs (id, room, scenario, actions, triggered_by, timestamp)
```

---

## DATABASE SCHEMA

### SQLite (Local - /data/smarthome.db)

See GATEWAY/storage/db_schema.sql for complete schema. Key tables:

- **users** - Admin accounts (SHA256 password hash)
- **sessions** - Active login sessions
- **rooms** - Physical rooms (bedroom_01, kitchen_01, living_room_01)
- **devices** - Actuators in each room (fan, light, relay)
- **sensor_data** - Historical sensor readings with firebase_synced flag
- **device_status** - Current on/off state of devices
- **system_alerts** - Gas/fire/system alerts
- **automations** - Threshold rules for automatic control
- **schedules** - Time-based device actions
- **rfid_cards** - Registered RFID card UIDs
- **access_logs** - Entry/exit logs
- **ota_logs** - Firmware update history
- **automation_logs** - Automation trigger history

### Firestore (Cloud)

**Collections**:
- `users/{uid}` - User profile metadata
- `rooms/{room_id}` - Room metadata
- `rooms/{room_id}/devices/{device_id}` - Device configs
- `rooms/{room_id}/sensors/{sensor_type}` - Latest sensor readings
- `automations/{doc_id}` - Automation rules
- `schedules/{doc_id}` - Schedule definitions
- `commands/{cmd_id}` - Command history
- `system_alerts/alerts_YYYY-MM-DD/{alert_id}` - Alert events
- `system_metadata/...` - WiFi networks, system status, etc.

### Firebase RTDB (Realtime Database)

**Structure** (live data):
```raw
live/
  bedroom_01/
    sensors/
      temperature: {value, ts, unit}
      humidity: {value, ts, unit}
      gas: {value, ts, unit}
      co2: {value, ts, unit}
      fire_detected: {value, ts}
    meta/
      room_name, online, last_seen, pi_version
  kitchen_01/ ...
  living_room_01/ ...
```

---

## API ENDPOINTS

### Authentication

| Method | Endpoint | Auth | Purpose |
|--------|----------|------|---------|
| POST | `/auth/register` | None | Create new user account |
| POST | `/auth/login` | None | Login (email + password) |
| GET | `/auth/me` | Required | Get current user info |
| POST | `/auth/logout` | Required | Logout (token blacklist) |

### Sensors & Dashboard

| Method | Endpoint | Auth | Query | Return |
|--------|----------|------|-------|--------|
| GET | `/latest` | Required | room, limit | Recent sensor readings |
| GET | `/history` | Required | room, type, hours | Historical readings (24h default) |
| GET | `/dashboard` | Required | room | Sensor + device + alert snapshot |
| GET | `/chart` | Required | room, type, hours | Chart data (JSON points) |
| GET | `/rooms` | Required | - | List room IDs |

### Devices

| Method | Endpoint | Auth | Body | Purpose |
|--------|----------|------|------|---------|
| GET | `/device_status` | Required | - | Get current device states |
| POST | `/control` | Required | action, room, device_id, is_on | Send device command |

### Automations & Schedules

| Method | Endpoint | Auth | Body |
|--------|----------|------|------|
| GET | `/automations` | Required | - |
| POST | `/automations` | Required | room_id, enabled, fan_threshold, ... |
| DELETE | `/automations/<room_id>` | Required | - |
| GET | `/schedules` | Required | room |
| POST | `/schedules` | Required | room_id, device_id, action, time |
| DELETE | `/schedules/<sched_id>` | Required | - |

### System

| Method | Endpoint | Auth | Purpose |
|--------|----------|------|---------|
| GET | `/health` | None | Gateway health check (for ESP32) |
| GET | `/system/clock` | Required | Server time & timezone |
| GET | `/system/safety_status` | Required | Current safety locks & alerts |
| GET | `/system/snapshots` | Required | System state snapshots |
| GET | `/sd2/status` | Required | SD2 mount status |
| GET | `/data` | Required | Local data directory stats |

### Logs

| Method | Endpoint | Auth | Query |
|--------|----------|------|-------|
| GET | `/logs/latest` | Required | room, limit |
| GET | `/logs/history` | Required | room, hours |
| GET | `/logs/access` | Required | room, hours |

### RFID

| Method | Endpoint | Auth | Body |
|--------|----------|------|------|
| GET | `/rfid/cards` | Required | - |
| POST | `/rfid/enroll` | Required | room_id, timeout_s |
| DELETE | `/rfid/cards/<uid>` | Required | - |

### WiFi

| Method | Endpoint | Auth | Purpose |
|--------|----------|------|---------|
| GET | `/wifi/status` | Required | Current WiFi connection |
| GET | `/wifi/scan_result` | Required | Available WiFi networks |
| POST | `/wifi/connect` | Required | Connect to WiFi network |

### OTA (Firmware)

| Method | Endpoint | Auth | Body |
|--------|----------|------|------|
| POST | `/ota/trigger` | Required | room, firmware_url |
| GET | `/ota/logs` | Required | - |
| GET | `/firmware/<file>` | None | Download firmware file |

---

## ERROR CONDITIONS & EDGE CASES

### 1. Network Failures

**Scenario**: ESP32 WiFi disconnected
```raw
Action:
  ├─ ESP32 detects WiFi.status() != WL_CONNECTED
  ├─ Buffer sensor readings in RAM (sensorBuffer, max 50)
  ├─ Retry WiFi connect every 5 seconds
  ├─ After reconnect, flushSensorBuffer() → send all buffered data
  └─ Data loss: Only loses most recent data if buffer fills

Detection: [OFFLINE] Buffered data messages
Recovery: Automatic upon reconnect
```

**Scenario**: MQTT Broker disconnected
```raw
Action:
  ├─ PubSubClient detects connection lost
  ├─ Queue pending MQTT publishes in Redis mqtt_pending_queue
  ├─ Retry MQTT connect every 5 seconds
  ├─ Upon reconnect, replay queued messages
  └─ Some data loss possible

Detection: [MQTT] Failed, rc=<code>
Recovery: Automatic, with queued message replay
```

**Scenario**: Firebase credentials expired/invalid
```raw
Action:
  ├─ firebase_sync fails to initialize
  ├─ Check SERVICE_ACCOUNT_FILE location
  ├─ Log critical error
  ├─ Fallback: Continue without Firebase sync
  └─ Data accumulates in SQLite, waiting for Firebase to be available

Detection: [firebase_sync] Не гу khởi tạo Firebase: ...
Recovery: Restart gateway with valid credentials, manual sync
```

### 2. Safety System Edge Cases

**Scenario**: Gas reading fluctuates around threshold (599 → 601 → 599)
```raw
Logic (safety_watchdog.py):
  ├─ hysteresis_offset = 2.0 NOT used in watchdog (only in automation)
  ├─ If value > threshold: is_dangerous = True → trigger alarm
  ├─ If value ≤ threshold: is_dangerous = False → clear lock
  ├─ Min 30s between lock refreshes to avoid spam
  └─ RESULT: May trigger multiple times if sensor noisy

Workaround: Increase gas_threshold or add smoothing
```

**Scenario**: User manually mutes alert, then gas is still detected
```raw
Logic:
  1. Gas detected → alarm triggers → user clicks "Tắt còi"
  2. SAFETY_STATE[room]["muted"] = True
  3. Smart mute also triggered: buzzer off for 180 seconds
  4. After 180s, if gas still > threshold → buzzer re-triggers automatically
  5. User can mute again if needed

Edge case: If user keeps clicking mute every 3 minutes while gas
persists, system behaves as expected (allows manual override)
```

**Scenario**: Safety lock active, user tries manual device control
```raw
If dispatch_command(source="web") and safety_lock exists:
  └─ Return 423 Locked error
  └─ UI shows "Hệ thống đang trong trạng thái khẩn cấp!"
  
Only source="safety" commands bypass lock (fan on, alarm)
```

### 3. Schedule & Automation Conflicts

**Scenario**: Schedule active within ±60 minutes AND user tries automation
```raw
Logic (dispatch_command):
  ├─ If source="automation" AND schedule active within 60m of now
  ├─ → BLOCKED (schedule_active_60m)
  └─ Automation waits until schedule window closes

Example: Schedule turn_on fan at 07:00
  ├─ 06:00-08:00 (within 60m) → automations BLOCKED
  ├─ After 08:00 → automations active again
```

**Scenario**: Device in manual mode, automation tries to trigger
```raw
Logic:
  ├─ Check MANUAL_STATE[device_id]
  ├─ If exists AND not TTL expired (5 min) → BLOCKED
  ├─ If TTL expired → auto-clear MANUAL_STATE, allow automation
  
Example:
  1. User manually turns on fan at 15:00 (sets MANUAL_STATE)
  2. 15:00-15:05 → automations blocked for this fan
  3. 15:05+ → MANUAL_STATE expired, automations resume
```

**Scenario**: What counts as manual override?
```raw
Only these sources set MANUAL_STATE:
  ├─ "button" (physical button press on ESP32) ✓ → manual mode
  ├─ "physical" (physical switch)                ✓ → manual mode
  └─ "esp32" (ESP32 confirms automation)        ✗ → NOT manual mode

NOT manual mode from:
  ├─ "web" (web UI)
  ├─ "schedule" (time-based)
  └─ "automation" (sensor-based)

This prevents automation from blocking itself!
```

### 4. RFID & Access Control

**Scenario**: Enrollment timeout
```raw
Setup: POST /rfid/enroll?room=bedroom_01&timeout=60
  ├─ ENROLLMENT_STATE.active = True for 60 seconds
  ├─ ESP32 listens for RFID card swipe (topic: home/bedroom_01/enroll)
  ├─ If card swiped + success=true: enroll & publish rfid_enrollment_result
  ├─ If 60s passes: timeout, return to normal operation
  └─ No additional error—just enrollment mode ends

Edge case: Card swiped after timeout
  ├─ Processed as normal auth (not enrollment)
  ├─ If card not registered → auth denied
```

**Scenario**: RFID auth failure (card not registered)
```raw
ESP32 receives auth response: success=false
  ├─ Log access attempt
  ├─ Publish denied auth to automation_engine
  ├─ No relay trigger
  └─ Increment failed auth counter (for DDoS detection?)
```

### 5. Data Sync & Persistence

**Scenario**: SD2 not mounted at startup
```raw
FIX BUG-C-02 - Degraded Mode:
  ├─ data_syncer.wait_ready() starts with DEGRADED_TIMEOUT = 300s
  ├─ After 5 minutes, switch to degraded mode
  ├─ Use fallback path: /data/sensor_history instead of /mnt/sd2/data
  ├─ Continue syncing to local fallback (no data loss!)
  ├─ If SD2 hot-plugged later, auto-switch back
  └─ Log: [SYNCER] ✅ SD2 hot-plugged — switching back from degraded mode
```

**Scenario**: firebase_synced flag not set properly
```raw
Sensor reading in SQLite:
  ├─ firebase_synced = 0 initially
  ├─ firebase_sync reads & pushes to RTDB
  ├─ Mark firebase_synced = 1 in mark_sensor_readings_synced()
  ├─ If mark fails → reading resynced (idempotent due to unique key)
  └─ No duplicates on retry (UNIQUE on room, type, timestamp)
```

**Scenario**: CSV export at 2 AM fails
```raw
data_syncer export_hour = 2 (configurable)
  ├─ If export fails, continue syncing
  ├─ Retry next day at 2 AM
  ├─ No data loss (still in daily DB)
  └─ Error logged: [SYNCER] Export error: ...
```

### 6. Device & Command Issues

**Scenario**: Command never gets ACK from ESP32
```raw
automation_engine PENDING_COMMANDS:
  ├─ Track command_id + timestamp
  ├─ If no ACK within timeout → log error
  ├─ No retry automatic (design choice)
  ├─ User can resend command manually
  └─ Consider for future: exponential backoff retry
```

**Scenario**: ESP32 status message arrives but device offline in Firestore
```raw
Flow:
  1. ESP32 sends status: home/bedroom_01/status {fan=on}
  2. automation_engine cache updates: fan=on
  3. firebase_sync syncs to Firestore /rooms/.../devices/fan_bd_1 {isOn: true}
  4. Web UI sees isOn=true → reflects actual state ✓
  
No lag after ESP32 confirms action (~200-500ms)
```

### 7. Firebase Conflict Resolution

**CONFLICT A1 - Single Writer for RTDB Sensor**:
```raw
Issue: Two writers to RTDB:
  └─ automation_engine publishes "realtime_data"
  └─ firebase_sync reads "realtime_data" AND "mqtt_inbound"

Fix: firebase_sync ONLY subscribes "mqtt_inbound" (single source)
  ├─ "realtime_data" channel is for SocketIO/Web (no Firebase)
  ├─ Single write path: ESP32 → MQTT → mqtt_inbound → RTDB
  └─ No race condition, no duplicate quota burns
```

**CONFLICT B1 - force_update_device() removed**:
```raw
Issue: Two updates to Firestore devices:
  └─ CommandDispatcher.force_update_device() on dispatch
  └─ UplinkStream updates again after ESP32 confirms

Fix: Removed force_update_device()
  ├─ Only update when ESP32 confirms (device_status channel)
  ├─ Single source of truth
  ├─ No UI flicker from optimistic → actual value mismatch
  └─ Small delay to confirmation (~200-500ms) is acceptable
```

**CONFLICT D1 - Centralized Priority Dispatcher**:
```raw
Issue: Multiple sources publishing device_commands to Redis
  └─ automation_engine, safety_watchdog, API, schedules all competing

Fix: Centralized dispatch_command() function in automation_engine
  ├─ Single decision point for priority & conflicts
  ├─ Priority: safety > manual/web > schedule > automation
  ├─ Blocks lower-priority sources when higher-priority active
  └─ Safety lock blocks everything except safety commands
```

### 8. Performance & Limits

**Buffer Limits**:
```raw
├─ sensorBuffer (ESP32): max 50 readings (~4-5 min @ 5s/reading)
├─ SENSOR_BUFFER (data_syncer): BUFFER_WARN_THRESHOLD = 5000
├─ BUFFER_MAX_THRESHOLD = 20000 → drop oldest samples
├─ Redis memory: unlimited (depends on Pi RAM available)
└─ Firestore: quota-based on plan (pay-as-you-go)
```

**Rate Limiting**:
```raw
├─ ESP32 sensor publish: every 5 seconds (hardcoded)
├─ DEVICE_LAST_SWITCH: MIN_SWITCH_DELAY_S = 30 (hysteresis)
├─ Lock refresh: LOCK_REFRESH_INTERVAL = 30 (safety watchdog)
├─ Heartbeat: every 10 seconds (ESP32)
├─ NTP sync: every 3600 seconds (network_watchdog)
└─ CSV export: once daily at 02:00
```

**Concurrency**:
```raw
├─ CACHED_SENSORS protected by _SENSORS_LOCK (RLock)
├─ Redis connection pool: max 20 connections
├─ SQLite WAL mode: allows concurrent reads
├─ MQTT single-threaded with loop_start()
├─ Each worker in separate daemon thread
└─ No explicit mutex on MANUAL_STATE, SCHEDULE_LAST_RUN (acceptable race)
```

---

## KNOWN ISSUES & LIMITATIONS

### 1. No Retry for Failed Commands
- If command send fails, no automatic retry
- User must manually trigger again
- Proposal: Exponential backoff queue in automation_engine

### 2. Hysteresis Only in Automation, Not Watchdog
- Sensor noise around threshold can cause flapping
- Watchdog: any value > threshold = alert
- Automation: uses hysteresis (prevent flapping only for automation)
- Fix: Apply hysteresis uniformly

### 3. No Deduplication for RFID
- Card can be enrolled multiple times with same UID
- Latest enrollment overwrites previous
- Proposal: Check existing UID before re-enroll

### 4. Manual Override TTL Fixed at 5 Minutes
- No way to explicitly clear MANUAL_STATE
- User must wait 5 minutes or restart gateway
- Proposal: Add `/control?action=set_auto_mode` endpoint ✓ (already exists!)

### 5. Schedule Windows Too Wide (±60 minutes)
- If schedule at 07:00, automation blocked 06:00-08:00
- May be too conservative for fast-changing conditions
- Proposal: Configurable window or remove entirely

### 6. No Firmware Version Tracking
- ESP32 sends no version info to Gateway
- OTA update assumes all nodes are same version
- Proposal: Add version query endpoint to firmware

---

## DEPLOYMENT CHECKLIST

```bash
# 1. Configure Environment
export PI_OWNER_UID="firebase_uid_here"          # From Firebase console
export FIREBASE_PROJECT_ID="nhathongminh-myhome"
export FIREBASE_DB_URL="https://...firebasedatabase.app"

# 2. Install & Start Services
sudo systemctl enable smarthome
sudo systemctl start smarthome
sudo journalctl -u smarthome -f              # Monitor logs

# 3. Verify Components
curl http://localhost:5000/health            # API health check
redis-cli ping                               # Redis connectivity
mosquitto_sub -t 'home/+/+' -v              # Monitor MQTT traffic

# 4. Provision Firebase
firebase deploy --only firestore:rules       # Security rules

# 5. Check SD2 & Backup
df -h /mnt/sd2                               # SD2 mount status
ls -la /data/                                # Local fallback dir

# 6. Test Safety System
# Manually set gas ppm via MQTT test
# Verify alarm triggers, lock set, alert saved
```

---

## SUMMARY

This SmartHome system is a sophisticated distributed IoT platform with:
- **Real-time sensor data** streaming via MQTT
- **Cloud sync** to Firebase for multi-device access
- **Intelligent automation** with priority-based command dispatch
- **Safety-first design** with lockout modes for gas/fire
- **Graceful degradation** when SD2 unavailable
- **Comprehensive logging** for debugging and auditing
- **Scalable architecture** with worker threads and pub/sub messaging

Key strengths:
✓ Single source of truth (RTDB/Firestore) for cloud state
✓ Local persistence (SQLite + CSV) independent of cloud
✓ Conflict resolution (priority dispatcher) for multi-source commands
✓ Thread-safe concurrent access (locks, channels)
✓ OTA firmware updates for remote ESP32 management

Areas for improvement:
- Retry mechanisms for failed commands
- Unified hysteresis for all thresholds
- Firmware version tracking
- Manual override explicit clear endpoint (partially done)
- Configurable schedule window widths
- DDoS protection for API endpoints

