# SmartHome System - Complete Log Messages Reference

**Purpose**: Quick lookup for all log patterns and their meanings  
**Last Updated**: May 8, 2026

---

## GATEWAY STARTUP SEQUENCE

### Expected Boot Log Sequence

```raw
===================================================
  SmartHome Gateway — Starting
===================================================

--- GATEWAY INITIALIZATION ---
[MAIN] DB schema applied
[MAIN] DB init complete
[MAIN] Admin account created (SHA256 hash)

--- WORKER STARTUP ---
[BUS] MessageBus started
[BUS] MQTT connected & subscribed
[WATCHDOG] Safety watchdog started (thread-safe sensor read + Firestore alert sync)
[NET] Dual interface detected: Hotspot=wlan0, Uplink=wlan1
[NET] Hotspot 'SmartHome_Hub' activated on wlan0 at 10.42.0.1/24
[SYNCER] Waiting for SD2 (timeout 300s before degraded mode)...
[SYNCER] ✅ SD2 ready at /mnt/sd2/data
[SYNCER] SyncWorker running (storage: /mnt/sd2/data)
[AUTO] Cache loaded: 3 rules, 0 schedules
[firebase_sync] === AUTO-PROVISIONING BẮT ĐẦU ===
[firebase_sync] RTDB: Tạo node live/bedroom_01
[firebase_sync] RTDB: Tạo node live/kitchen_01
[firebase_sync] RTDB: Tạo node live/living_room_01
[firebase_sync] === AUTO-PROVISIONING HOÀN TẤT (3 rooms) ===
[MAIN] FirebaseSync worker started

--- API STARTUP ---
[BRIDGE] Realtime bridge started
[WS] WebSocket server ready
[API] Registered /api: auth
[API] Registered /api: sensors
[API] Registered /api: devices
[API] Registered /api: automation
[API] Registered /api: logs
[API] Registered /api: rfid
[API] Registered /api: wifi
[API] Registered /api: ota
[API] Registered /api: system
[MAIN] All workers started
===================================================
[MAIN] Gateway LIVE → http://0.0.0.0:5000/
[MAIN] API prefix: http://0.0.0.0:5000/api/
===================================================
```

---

## LOG CATEGORIES

### 1. MAIN (gateway_main.py)

| Log Message | Severity | Meaning | Action |
|------------|----------|---------|--------|
| `DB schema applied` | INFO | SQLite schema created/initialized | Normal ✓ |
| `Warning: db_schema.sql not found` | WARN | Schema file missing | Check file path |
| `DB init complete` | INFO | Database ready | Normal ✓ |
| `Admin account created (SHA256 hash)` | INFO | Default admin created | Normal ✓ (first run) |
| `FirebaseSync worker started` | INFO | Firebase sync enabled | Normal ✓ |
| `firebase_sync import error: <error>` | ERROR | Failed to import firebase module | Fix import, reinstall |
| `Firebase cred not found at <path>` | WARN | Firebase disabled | Optional (local-only mode) |
| `All workers started` | INFO | All threads launched | Normal ✓ |
| `Gateway LIVE → http://0.0.0.0:5000/` | INFO | API server listening | Normal ✓ |

---

### 2. BUS (bridge/message_bus.py)

| Log Message | Severity | Meaning | Action |
|------------|----------|---------|--------|
| `MessageBus started` | INFO | Singleton initialized | Normal ✓ |
| `MQTT connected & subscribed` | INFO | MQTT broker connection OK | Normal ✓ |
| `MQTT disconnected (rc=<code>)` | WARN | Broker disconnected | Auto-retry pending |
| `MQTT connect failed (<error>)` | ERROR | Connection attempt failed | Will retry with exponential backoff |
| `inbound parse error: <error>` | ERROR | Malformed MQTT message | Message dropped (no data loss) |
| `Outbound loop started` | INFO | Redis → MQTT forwarder active | Normal ✓ |
| `outbound error: <error>` | ERROR | Failed to publish to MQTT | Message queued (mqtt_pending_queue) |

**Note**: Connection failures auto-recover with 2^n backoff (max 30s wait)

---

### 3. FIREBASE_SYNC (workers/firebase_sync.py)

#### Initialization Phase
```raw
[firebase_sync] Sử dụng Service Account: /path/to/service-account.json
```
✓ Normal — credentials file loaded

```raw
[firebase_sync] Lỗi đọc file service account: FileNotFoundError
```
⚠ Firebase disabled unless ADC is available

```raw
[firebase_sync] Dùng ADC (Application Default Credentials).
```
ℹ Falling back to Application Default Credentials (e.g., container auth)

```raw
[firebase_sync] Không thể khởi tạo Firebase: PermissionError
```
🔴 CRITICAL — Firebase SDK initialization failed. Check credentials & network.

#### Auto-Provisioning Phase
```raw
[firebase_sync] === AUTO-PROVISIONING BẮT ĐẦU ===
[firebase_sync] RTDB: Tạo node live/bedroom_01
[firebase_sync] === AUTO-PROVISIONING HOÀN TẤT (3 rooms) ===
```
✓ Rooms auto-created in Firebase (runs on every startup as idempotent check)

```raw
[firebase_sync] RTDB provision room bedroom_01 failed: ConnectionError
```
⚠ Could not create RTDB node (may retry on next loop)

```raw
[firebase_sync] Firestore provision device bedroom_01/fan_bd_1 failed: PermissionError
```
🔴 Security rule issue — check firestore.rules

#### Data Sync Phase
```raw
[firebase_sync] Не đọc được rooms từ SQLite: <error> — dùng DEFAULT_ROOMS
```
ℹ SQLite empty → using hardcoded default rooms

```raw
[firebase_sync] Không đọc devices từ SQLite (room=bedroom_01): <error>
```
ℹ Room has no devices — will skip device sync

```raw
[firebase_sync] SQLite read error: <error>
```
⚠ Database access issue — will retry

```raw
[firebase_sync] RTDB update_sensor_bulk [bedroom_01] error: Timeout
```
⚠ Network slow — sensor data delayed to cloud

```raw
[firebase_sync] RTDB set_room_offline [kitchen_01] error: <error>
```
⚠ Could not mark room offline (may be temporary)

---

### 4. AUTOMATION_ENGINE (workers/automation_engine.py)

#### Startup & Caching
```raw
[AUTO] Cache loaded: 3 rules, 5 schedules
```
✓ Automation rules & schedules loaded from SQLite

#### Command Dispatch
```raw
[DISPATCH] SENT [web|p=1]: bedroom_01/fan_bd_1 → turn_on
```
✓ Command successfully dispatched to MQTT

```raw
[DISPATCH] BLOCKED (safety_lock): web → bedroom_01/fan_bd_1 turn_on
```
ℹ Safety lock active — command rejected

```raw
[DISPATCH] BLOCKED (manual_override): automation → fan_bd_1
```
ℹ Device in manual mode (user control) — automation suspended

```raw
[DISPATCH] BLOCKED (schedule_active_60m): automation → light_bd_1
```
ℹ Schedule runs within ±60m — automation blocked to avoid conflict

```raw
[DISPATCH] MANUAL_STATE TTL expired for fan_bd_1 → auto mode restored
```
ℹ 5 minutes passed since manual override — auto-control re-enabled

#### Automation Triggers
```raw
[AUTO] Auto temperature trigger: bedroom_01 → turn_on (26.5, 25.0)
```
✓ Temperature exceeded threshold (value 26.5 > threshold 25.0)

```raw
[ENROLL] bedroom_01 enrollment mode: ACTIVE (timeout: 60s)
```
ℹ RFID enrollment started — waiting for card swipe (60s window)

#### RFID Actions
```raw
[RFID] Card 1A2B3C4D enrolled as "John Doe" in bedroom_01
```
✓ New RFID card registered

```raw
[RFID] Auth ALLOWED: bedroom_01 card 1A2B3C4D (user: John Doe)
```
✓ Card scanned, access granted, relay opened

```raw
[RFID] Auth DENIED: bedroom_01 card UNKNOWN
```
🔴 Unknown card — access denied, logged

---

### 5. SAFETY_WATCHDOG (workers/safety_watchdog.py)

#### Watchdog Startup
```raw
[WATCHDOG] Safety watchdog started (thread-safe sensor read + Firestore alert sync)
```
✓ Monitoring active

#### Danger Detection
```raw
[WATCHDOG] DANGER DETECTED bedroom_01: RÒ RỈ KHÍ GAS! 750 ppm - Phòng: bedroom_01
```
🔴 **CRITICAL** — Gas detected > threshold
- Fan turned on immediately
- Buzzer activated
- Safety lock set (6 min)
- Alert saved to SQLite + Firestore

```raw
[WATCHDOG] DANGER DETECTED living_room_01: PHÁT HIỆN LỬA! Phòng: living_room_01
```
🔴 **CRITICAL** — Fire detected
- Same response as gas (fan on, buzzer, lock)

#### Muting & Recovery
```raw
[WATCHDOG] bedroom_01 muted by user (manual)
```
ℹ User clicked "Tắt còi" button — buzzer off (10 min)

```raw
[WATCHDOG] kitchen_01 smart_muted (alert read) — buzzer off for 180s
```
ℹ User marked alert as read — buzzer off (3 min), auto-resume if condition persists

```raw
[WATCHDOG] bedroom_01 danger CLEARED — lock removed, buzzer off
```
✓ Gas/fire level returned to normal — lock released

#### Mute Listener
```raw
[WATCHDOG] mute listener error: <error>
```
⚠ Redis channel listener failed — will retry

---

### 6. DATA_SYNCER (workers/data_syncer.py)

#### SD2 Status
```raw
[SYNCER] SD2 write-test failed: PermissionError
```
⚠ SD2 mounted but not writable — check permissions

```raw
[SYNCER] Mount script failed: Timeout
```
⚠ SD2 mount attempt failed — will retry

```raw
[SYNCER] ✅ SD2 ready at /mnt/sd2/data
```
✓ SD2 mounted & writable

```raw
[SYNCER] Waiting for SD2 (timeout 300s before degraded mode)...
```
ℹ Checking for SD2, will wait 5 minutes before degraded mode

```raw
[SYNCER] ✅ SD2 hot-plugged — switching back from degraded mode
```
✓ SD2 hot-plugged detected — back to normal operation

#### Buffer & Persistence
```raw
[SYNCER] Buffer warn: 5000 items pending flush
```
⚠ Memory buffer getting full — flush happening

```raw
[SYNCER] Buffer full (20000) — dropping sample bedroom_01/temperature
```
🔴 **Data loss** — memory buffer full, oldest sample dropped

```raw
[SYNCER] Flushed 1,245 sensor rows
```
✓ Batch write to SQLite successful

```raw
[SYNCER] flush_sensor DB error: DatabaseLockedError — will retry next cycle
```
⚠ SQLite locked (concurrent access) — will retry

#### Daily Exports
```raw
[SYNCER] CSV exported: /mnt/sd2/data/export_2026-05-08.csv
```
✓ Daily CSV export completed at 2 AM

```raw
[SYNCER] Export error: FileNotFoundError
```
⚠ Export destination issue — data remains in DB

#### Database Rotation
```raw
[SYNCER] DB rotated → /mnt/sd2/data/data_2026-05-08.db (mode: READY)
```
✓ Daily database rotation at midnight

```raw
[SYNCER] SyncWorker running (storage: /data/sensor_history)
```
ℹ Degraded mode — using local fallback instead of SD2

---

### 7. NETWORK_WATCHDOG (workers/network_watchdog.py)

#### Interface Detection
```raw
[NET] Dual interface detected: Hotspot=wlan0, Uplink=wlan1
```
✓ Two WiFi adapters — Hotspot on wlan0, internet on wlan1 (isolated)

```raw
[NET] Single interface: wlan0 (AP+STA concurrent mode)
```
ℹ One WiFi adapter — running both AP & STA on same interface

#### Hotspot Setup
```raw
[NET] Hotspot 'SmartHome_Hub' activated on wlan0 at 10.42.0.1/24
```
✓ Access point active — ESP32 can connect

```raw
[NET] Hotspot error on wlan0: PermissionError
```
🔴 Cannot create hotspot — check nmcli permissions & driver support

#### Internet Status
```raw
[NET] Internet status: ONLINE
[NET] WiFi Status: ssid=MyWiFi, signal=-45dBm, state=CONNECTED
```
✓ Internet connection active

```raw
[NET] WiFi Status: ssid=, signal=0dBm, state=DISCONNECTED
```
⚠ Internet offline — will retry connection

#### Time Sync
```raw
[NET] NTP sync successful: 2026-05-08T15:30:45Z
```
✓ System time synchronized

```raw
[NET] NTP sync failed: Timeout
```
⚠ NTP server unreachable — will retry next hour

---

## ESP32 FIRMWARE LOGS (Serial Monitor)

### Startup Sequence
```raw
--- BAT DAU KET NOI (DIRECT HOTSPOT MODE) ---
```
Boot begins

```raw
Dang ket noi vao Hotspot: SmartHome_Hub
.........................
KET NOI WIFI THANH CONG!
IP ESP32: 10.42.0.51
Gateway: 10.42.0.1
Signal: -45dBm
```
✓ WiFi connected successfully

```raw
Loi cau hinh IP Tinh!
```
⚠ Static IP config failed — falling back to DHCP

```raw
THẤT BẠI. Status: <code>
```
🔴 WiFi connection failed (status code reference:
- 0 = WL_IDLE_STATUS
- 1 = WL_NO_SSID_AVAIL (AP not found)
- 2 = WL_SCAN_COMPLETED
- 3 = WL_CONNECTED ✓
- 4 = WL_CONNECT_FAILED (wrong password)
- 5 = WL_CONNECTION_LOST
- 6 = WL_DISCONNECTED)

### MQTT Connection
```raw
Connecting to MQTT Broker (10.42.0.1)...  Broker Connected!
 Subscribed: home/bedroom_01/command
```
✓ MQTT connected & subscribed to command topic

```raw
 Mat Wifi. Dang ket noi lai...
```
ℹ WiFi lost — reconnecting

```raw
 Failed, rc=<code>
```
⚠ MQTT connection failed (PubSubClient error codes)

### Button Events
```raw
 [TASK] Fan Button Pressed!
 [TASK] Light Button Pressed!
```
✓ Physical button detected

### Sensor Buffering
```raw
[SYNC] Đang đồng bộ <N> bản tin cũ...
[SYNC] Đã đồng bộ xong!
```
✓ Buffered sensor data flushed on reconnect

```raw
[OFFLINE] Buffered data. Size: 15/50
```
ℹ Buffering sensor data (no internet)

```raw
[OFFLINE] Buffer full, rotating...
```
⚠ RAM buffer full (50 readings) — dropping oldest to make room

### OTA Updates
```raw
[OTA] Starting update from: http://gateway.local/firmware/bedroom_v2.bin
```
ℹ OTA initiated

```raw
[OTA] Progress: 25%
[OTA] Progress: 50%
[OTA] Progress: 100%
```
ℹ Download progress

```raw
[OTA] FAILED (5): No buffer space available
```
🔴 OTA failed — not enough RAM, may retry

```raw
[OTA] No update available.
```
ℹ Firmware already latest

```raw
[OTA] OK!
```
✓ OTA successful (device will reboot)

```raw
>>> BEDROOM NODE READY <<<
```
✓ Startup complete, ready for commands

---

## WEB FRONTEND LOGS (Browser Console)

### Authentication
```raw
 Đã đăng nhập: user@example.com
```
✓ User authenticated (onAuthStateChanged fired)

```raw
Người dùng chưa đăng nhập (Khách)
```
ℹ No user session

```raw
 Đăng xuất thành công
```
✓ Logout successful

### Room & Device Management
```raw
Rooms loaded (by userId): 3
Rooms loaded (fallback, all): 3
```
ℹ Rooms fetched from Firestore (BUG-H-03 fallback path)

```raw
Không tìm thấy rooms với userId. Fallback: lấy tất cả rooms.
   → Hãy cấu hình PI_OWNER_UID trong .env của Raspberry Pi.
```
⚠ PI_OWNER_UID not configured — using fallback (all rooms visible)

### Device Control
```raw
Command sent: ON → bedroom_01/fan_bd_1 [cmd-abc-123]
```
✓ Device command dispatched to /commands collection

```raw
Lệnh đã gửi: Quạt ngủ bật
```
✓ User feedback shown

```raw
Không thể gửi lệnh!
```
🔴 Command failed (network error or validation)

### Errors
```raw
Error getting room details: TypeError: Cannot read property 'data' of null
```
🔴 Room document missing or permission denied

```raw
Error sending device command: PermissionError
```
🔴 Firestore security rule violation

```raw
Login Error: auth/invalid-credential
Register Error: auth/email-already-in-use
```
⚠ Authentication error (invalid login or duplicate email)

---

## COMMON LOG PATTERNS

### Success Patterns (Look For ✓)
```raw
✓ "Gateway LIVE" at startup
✓ "DB schema applied"
✓ "MQTT connected"
✓ "SD2 ready"
✓ "Auto-provisioning completed"
✓ "All workers started"
✓ "Command sent" in dispatch logs
✓ "RTDB updated" in firebase_sync
```

### Warning Patterns (Investigate ⚠)
```raw
⚠ "MQTT disconnected" — network issue?
⚠ "Buffer warn" — too many sensors buffered
⚠ "SD2 write-test failed" — permissions?
⚠ "Firebase cred not found" — credentials missing?
⚠ "BLOCKED" dispatch messages — manual override active?
⚠ "NTP sync failed" — time may be wrong
⚠ "Fallback: lấy tất cả rooms" — PI_OWNER_UID not set
```

### Critical Patterns (Action Required 🔴)
```raw
🔴 "[firebase_sync] CRITICAL: Không thể khởi tạo Firebase"
🔴 "[WATCHDOG] DANGER DETECTED"
🔴 "[SYNCER] Buffer full — dropping"
🔴 "[DISPATCH] BLOCKED (safety_lock)" + manual action needed
🔴 "PermissionError" in Firestore/RTDB operations
🔴 "DatabaseLockedError" repeated frequently
```

---

## DEBUG COMMANDS

### Check Gateway Status
```bash
# API health
curl http://localhost:5000/health

# MQTT traffic
mosquitto_sub -t 'home/+/+' -v

# Redis channels
redis-cli
SUBSCRIBE mqtt_inbound
SUBSCRIBE device_commands
PSUBSCRIBE realtime_data

# SQLite database
sqlite3 /data/smarthome.db
SELECT * FROM automations;
SELECT COUNT(*) FROM sensor_data;
```

### View Logs
```bash
# Real-time gateway logs
sudo journalctl -u smarthome -f

# ESP32 serial logs
picocom /dev/ttyUSB0 -b 115200

# Cloud sync logs
tail -f /var/log/firebase_sync.log
```

### Trigger Events for Testing
```bash
# Publish test sensor data
mosquitto_pub -h 10.42.0.1 -t 'home/bedroom_01/sensors' \
  -m '{"temperature":28,"humidity":60,"gas":750}'

# Publish device command
mosquitto_pub -h 10.42.0.1 -t 'home/bedroom_01/command' \
  -m '{"device":"fan_bd_1","action":"turn_on"}'
```

---

## TIMESTAMP FORMATS

| Source | Format | Example |
|--------|--------|---------|
| SQLite | `YYYY-MM-DD HH:MM:SS` | `2026-05-08 15:30:45` |
| Firebase RTDB | ISO 8601 | `2026-05-08T15:30:45.123Z` |
| Firestore | Timestamp | `Timestamp(seconds=1715181045, nanoseconds=123000000)` |
| MQTT payload | Unix timestamp | `"ts": 1715181045` |
| Python logging | `HH:MM:SS` (in logs) | `15:30:45 [INFO]` |
| ESP32 Serial | Millis since boot | `[1234567]` |

---

## Troubleshooting Guide

### Gateway Won't Start
```raw
Check:
1. Python dependencies: pip install -r requirements.txt
2. SQLite path: ls -la /data/
3. Redis running: redis-cli ping
4. MQTT broker: mosquitto -v
5. Log output: sudo journalctl -u smarthome
```

### Sensors Not Appearing in Dashboard
```raw
Check:
1. ESP32 connected to WiFi: Serial monitor shows "KET NOI WIFI THANH CONG!"
2. MQTT messages arriving: mosquitto_sub -t 'home/+/+'
3. automation_engine processing: grep "[AUTO]" in gateway logs
4. firebase_sync syncing: grep "RTDB update" in gateway logs
5. Firestore rules: Check firestore.rules permissions
```

### Commands Not Executing
```raw
Check:
1. Safety lock active?: grep "BLOCKED (safety_lock)" in logs
2. Manual override active?: grep "MANUAL_STATE" in logs
3. Schedule blocking?: grep "schedule_active_60m" in logs
4. MQTT publish succeeds?: Check [DISPATCH] SENT messages
5. ESP32 receiving?: Check Serial: "Subscribed: home/*/command"
```

### Device Offline
```raw
Check:
1. WiFi signal: ESP32 Serial: "Signal: -XdBm"
2. MQTT broker accepting?: [BUS] MQTT connected?
3. Network watchdog restarting hotspot?: [NET] logs
4. Firestore metadata: Check live/{room}/meta/online flag
5. Last seen time: Compare with current time in live/{room}/meta/last_seen
```

