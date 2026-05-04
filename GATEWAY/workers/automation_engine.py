"""
workers/automation_engine.py  — v2.2  (CONFLICT FIX)
══════════════════════════════════════════════════════
FIXES trong phiên bản này:

  [CONFLICT D1 — CRITICAL] Priority Command Queue
      ─────────────────────────────────────────────
      Vấn đề: Schedule, Automation, Manual Web đều gửi MQTT tới cùng 1 relay
              mà không có cơ chế phân quyền cứng.
      Fix: Thêm hàm dispatch_command(source, room_id, device_id, action)
           - source priority: "safety" > "manual" > "schedule" > "automation"
           - Safety lock: từ chối MỌI lệnh ngoại trừ safety action
           - Manual override: MANUAL_STATE[device] block schedule VÀ automation
           - Schedule không ghi MANUAL_STATE nhưng bị block khi device ở manual mode
             (trước đây chỉ block automation, không block schedule)

  [CONFLICT C1 — MEDIUM] Thread-safe CACHED_SENSORS
      ─────────────────────────────────────────────
      Vấn đề: AutomationEngine ghi (MQTT thread) và SafetyWatchdog đọc (loop
              thread riêng) CACHED_SENSORS dict mà không có threading.Lock().
      Fix: Dùng threading.RLock() bảo vệ tất cả đọc/ghi CACHED_SENSORS.
           SafetyWatchdog đọc qua get_cached_sensors(room_id) — không truy cập
           trực tiếp dict nữa.

  [Giữ nguyên từ v2.1]
      BUG-ACK-01, BUG-DEVICE-SYNC-01, BUG-H-01, BUG-H-02, BUG-C-05,
      Smart Manual Override (state-based), Per-room thresholds
"""

import time
import json
import sqlite3
import threading
from datetime import datetime

from bridge.message_bus import MessageBus, CH_INBOUND
from workers import safety_watchdog

DB_PATH            = "/data/smarthome.db"
ENROLLMENT_TIMEOUT = 60
CLOCK_WARN_YEAR    = 2024

HYSTERESIS_OFFSET  = 2.0
MIN_SWITCH_DELAY_S = 30
DEVICE_LAST_SWITCH: dict = {}

# [FIX] MANUAL_STATE_TTL: sau N giây không có lệnh manual mới,
# tự động giải phóng manual override để automation hoạt động lại.
MANUAL_STATE_TTL_S = 300   # 5 phút

DEVICE_MAP = {
    "kitchen_01":     {"fan": "fan_kt_1",  "light": "light_kt_1"},
    "living_room_01": {"fan": "fan_lv_1",  "light": "light_lv_1"},
    "bedroom_01":     {"fan": "fan_bd_1",  "light": "light_bd_1"},
}

CACHED_AUTOMATIONS:   dict = {}
CACHED_SCHEDULES:     list = []
CACHED_DEVICE_STATES: dict = {}

# [v2.1] MANUAL_STATE thay cho MANUAL_CONTROL_CACHE + timestamp
MANUAL_STATE:         dict = {}

ENROLLMENT_STATE:     dict = {"active": False, "start_time": None, "pending_name": ""}
SCHEDULE_LAST_RUN:    dict = {}

# FIX BUG-ACK-01
PENDING_COMMANDS: dict = {}

# ══════════════════════════════════════════════════════
# FIX CONFLICT C1: Thread-safe lock cho CACHED_SENSORS
# ══════════════════════════════════════════════════════
_SENSORS_LOCK = threading.RLock()

# CACHED_SENSORS được truy cập qua hàm get/set bên dưới —
# KHÔNG truy cập trực tiếp từ bên ngoài module này.
# SafetyWatchdog dùng get_cached_sensors() thay vì safety_watchdog.CACHED_SENSORS
CACHED_SENSORS: dict = {}


def get_cached_sensors(room_id: str) -> dict:
    """Thread-safe read cho CACHED_SENSORS (fix C1)."""
    with _SENSORS_LOCK:
        return dict(CACHED_SENSORS.get(room_id, {}))


def update_cached_sensors(room_id: str, payload: dict):
    """Thread-safe write cho CACHED_SENSORS (fix C1)."""
    with _SENSORS_LOCK:
        room_cache = CACHED_SENSORS.setdefault(room_id, {})
        room_cache.update(payload)
        # Đồng bộ sang safety_watchdog.CACHED_SENSORS để backward compat
        safety_watchdog.CACHED_SENSORS[room_id] = dict(room_cache)


# ══════════════════════════════════════════════════════
# FIX CONFLICT D1: Centralized Priority Dispatcher
# ══════════════════════════════════════════════════════

# Thứ tự ưu tiên: số nhỏ hơn = ưu tiên cao hơn
SOURCE_PRIORITY = {
    "safety":     0,
    "manual":     1,
    "web":        1,   # Web lệnh thủ công = manual
    "schedule":   2,
    "automation": 3,
}


def dispatch_command(bus: MessageBus, source: str, room_id: str,
                     device_id: str, action: str, cmd_id: str = "",
                     extra: dict = None) -> bool:
    """
    FIX CONFLICT D1: Điểm phát lệnh duy nhất cho tất cả nguồn.

    Luật ưu tiên:
      1. Safety lock → chặn MỌI nguồn ngoại trừ "safety"
      2. "manual"/"web" → được phép, set MANUAL_STATE
      3. "schedule" → bị chặn nếu device đang ở manual mode
      4. "automation" → bị chặn nếu device đang ở manual mode
                         hoặc đang có schedule cho device này

    Returns: True nếu lệnh được gửi, False nếu bị chặn.
    """
    priority = SOURCE_PRIORITY.get(source, 99)

    # ── Rule 1: Safety lock chặn tất cả trừ "safety" ─────────────────
    if _is_safety_locked(room_id) and source != "safety":
        print(f"[DISPATCH] BLOCKED (safety_lock): {source} → {room_id}/{device_id} {action}")
        bus.publish_event("realtime_data", {
            "event":   "command_blocked",
            "room":    room_id,
            "reason":  "safety_lock",
            "message": "Hệ thống đang trong trạng thái khẩn cấp — lệnh bị từ chối!"
        })
        return False

    # ── Rule 2: Schedule/Automation bị chặn khi device ở manual mode ──
    # [FIX] TTL check: tự giải phóng manual override sau MANUAL_STATE_TTL_S giây
    if source in ("schedule", "automation"):
        manual = MANUAL_STATE.get(device_id)
        if manual and manual.get("mode") == "manual":
            set_at = manual.get("set_at")
            if set_at and (datetime.now() - set_at).total_seconds() > MANUAL_STATE_TTL_S:
                MANUAL_STATE.pop(device_id, None)
                print(f"[DISPATCH] MANUAL_STATE TTL expired for {device_id} → auto mode restored")
            else:
                print(f"[DISPATCH] BLOCKED (manual_override): {source} → {device_id}")
                return False

    # ── Rule 3: Automation bị chặn khi có schedule active cho device ──
    # [FIX] Chỉ block automation nếu schedule đó thực sự đã chạy hôm nay
    # (không block vĩnh viễn chỉ vì có schedule entry tồn tại)
    if source == "automation":
        today_key = datetime.now().strftime("%Y-%m-%d")
        for sched in CACHED_SCHEDULES:
            if not sched.get("enabled") or sched.get("device_id") != device_id:
                continue
            sched_id  = sched.get("id", "")
            last_run  = SCHEDULE_LAST_RUN.get(sched_id, "")
            # Chỉ block nếu schedule đã chạy hôm nay (trong 60 phút gần nhất)
            if last_run and today_key in last_run:
                sched_time = sched.get("time", "")
                try:
                    now_mins  = datetime.now().hour * 60 + datetime.now().minute
                    h, m      = map(int, sched_time.split(":"))
                    sched_mins = h * 60 + m
                    if abs(now_mins - sched_mins) <= 60:
                        print(f"[DISPATCH] BLOCKED (schedule_active_60m): automation → {device_id}")
                        return False
                except Exception:
                    pass

    # ── Gửi lệnh MQTT ─────────────────────────────────────────────────
    mqtt_payload = {
        "device": device_id,
        "action": action,
        "source": source,
    }
    if cmd_id:
        mqtt_payload["cmd_id"] = cmd_id
    if extra:
        mqtt_payload.update(extra)

    bus.publish_mqtt(f"home/{room_id}/command", mqtt_payload)
    print(f"[DISPATCH] SENT [{source}|p={priority}]: {room_id}/{device_id} → {action}")
    return True


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def load_cache():
    global CACHED_AUTOMATIONS, CACHED_SCHEDULES
    conn = get_db()
    try:
        for row in conn.execute("SELECT * FROM automations WHERE enabled=1").fetchall():
            CACHED_AUTOMATIONS[row["room_id"]] = dict(row)
        CACHED_SCHEDULES = [dict(r) for r in
                           conn.execute("SELECT * FROM schedules WHERE enabled=1").fetchall()]
    finally:
        conn.close()
    print(f"[AUTO] Cache loaded: {len(CACHED_AUTOMATIONS)} rules, {len(CACHED_SCHEDULES)} schedules")


def _is_safety_locked(room_id: str) -> bool:
    bus = MessageBus.get_instance()
    return bool(bus.get_redis().exists(f"safety_lock:{room_id}"))


def _try_control(bus: MessageBus, room_id: str, device_type: str,
                 value: float, threshold: float):
    """
    FIX D1: Dùng dispatch_command() thay vì gọi publish_mqtt() trực tiếp.
    dispatch_command() sẽ tự kiểm tra priority và manual state.
    """
    device_id = DEVICE_MAP.get(room_id, {}).get(device_type)
    if not device_id:
        return

    cache_key    = f"{room_id}_{device_id}"
    current_on   = CACHED_DEVICE_STATES.get(cache_key)

    threshold_off = threshold - HYSTERESIS_OFFSET
    if current_on is True:
        should_on = value > threshold_off
    elif current_on is False:
        should_on = value > threshold
    else:
        should_on = value > threshold

    if current_on == should_on:
        return

    now = datetime.now()
    last_switch = DEVICE_LAST_SWITCH.get(cache_key)
    if last_switch and (now - last_switch).total_seconds() < MIN_SWITCH_DELAY_S:
        return

    action = "turn_on" if should_on else "turn_off"
    sent = dispatch_command(bus, "automation", room_id, device_id, action)
    if sent:
        CACHED_DEVICE_STATES[cache_key] = should_on
        DEVICE_LAST_SWITCH[cache_key]   = now
        _log_automation(room_id, f"auto_{device_type}",
                        [f"{device_id} → {action} (value={value:.1f}, thresh={threshold})"],
                        f"sensor_{device_type}")


def _log_automation(room_id: str, scenario: str, actions: list, triggered_by: str):
    try:
        conn = get_db()
        try:
            conn.execute(
                "INSERT INTO automation_logs (room, scenario, actions, triggered_by) VALUES (?,?,?,?)",
                (room_id, scenario, json.dumps(actions), triggered_by)
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"[AUTO] log error: {e}")


def process_sensor(room_id: str, sensor_data: dict):
    """FIX BUG-H-01: Xử lý temperature, humidity, co2."""
    rule = CACHED_AUTOMATIONS.get(room_id)
    if not rule or not rule.get("enabled"):
        return

    bus = MessageBus.get_instance()

    if "temperature" in sensor_data:
        val = float(sensor_data["temperature"])
        if rule.get("fan_threshold"):
            _try_control(bus, room_id, "fan", val, float(rule["fan_threshold"]))
        if rule.get("light_threshold"):
            _try_control(bus, room_id, "light", val, float(rule["light_threshold"]))

    if "humidity" in sensor_data and rule.get("fan_threshold"):
        val = float(sensor_data["humidity"])
        _try_control(bus, room_id, "fan", val, float(rule["fan_threshold"]))

    if "co2" in sensor_data and rule.get("fan_threshold"):
        val = float(sensor_data["co2"])
        co2_thresh = float(rule.get("co2_threshold") or float(rule["fan_threshold"]) * 10)
        _try_control(bus, room_id, "fan", val, co2_thresh)


def _check_clock_validity() -> bool:
    if datetime.now().year < CLOCK_WARN_YEAR:
        bus = MessageBus.get_instance()
        bus.publish_event("realtime_data", {
            "event":   "system_warning",
            "message": "Giờ hệ thống chưa đồng bộ! Kiểm tra kết nối Internet/NTP.",
            "level":   "warning"
        })
        return False
    return True


def scheduler_loop():
    """
    FIX D1: Scheduler dùng dispatch_command() — tự động bị block
            khi device đang ở manual mode (không cần kiểm tra thủ công).
    FIX BUG-C-05: Không set enabled=0 sau khi chạy.
    """
    print("[SCHEDULER] Started")
    while True:
        try:
            if not _check_clock_validity():
                time.sleep(60)
                continue

            now          = datetime.now()
            current_hhmm = now.strftime("%H:%M")
            today_key    = now.strftime("%Y-%m-%d")
            bus          = MessageBus.get_instance()

            for sched in list(CACHED_SCHEDULES):
                if not sched.get("enabled"):
                    continue
                # FIX: Normalize time từ schedule để so sánh đúng với current_hhmm
                # Web có thể lưu "9:05" (không có leading zero) → normalize về "09:05"
                sched_time_raw = sched.get("time", "")
                try:
                    h, m = sched_time_raw.split(":")
                    sched_time_norm = f"{int(h):02d}:{int(m):02d}"
                except Exception:
                    sched_time_norm = sched_time_raw
                if sched_time_norm != current_hhmm:
                    continue
                if not sched.get("device_id"):
                    continue

                run_key = f"{sched['id']}_{current_hhmm}_{today_key}"
                if SCHEDULE_LAST_RUN.get(sched["id"]) == run_key:
                    continue

                room_id   = sched["room_id"]
                device_id = sched["device_id"]
                action    = sched["action"]

                # FIX D1: dispatch_command tự kiểm tra safety_lock và manual_state
                sent = dispatch_command(bus, "schedule", room_id, device_id, action)
                if sent:
                    SCHEDULE_LAST_RUN[sched["id"]] = run_key
                    print(f"[SCHEDULER] Executed: {room_id}/{device_id} → {action}")
                    _log_automation(room_id, "schedule", [f"{device_id} → {action}"], "schedule")

            if len(SCHEDULE_LAST_RUN) > 1000:
                SCHEDULE_LAST_RUN.clear()

            time.sleep(1.0 - (time.time() % 1.0))

        except Exception as e:
            print(f"[SCHEDULER] error: {e}")
            time.sleep(1)


def _check_enrollment_timeout():
    if (ENROLLMENT_STATE["active"] and
            ENROLLMENT_STATE.get("start_time") and
            (datetime.now() - ENROLLMENT_STATE["start_time"]).total_seconds() > ENROLLMENT_TIMEOUT):
        ENROLLMENT_STATE["active"]     = False
        ENROLLMENT_STATE["start_time"] = None
        print("[AUTO] Enrollment timeout — mode OFF")
        MessageBus.get_instance().publish_event("realtime_data", {
            "event": "enrollment_timeout", "message": "Hết thời gian đăng ký thẻ"
        })


def handle_inbound(envelope: dict):
    topic   = envelope.get("topic", "")
    payload = envelope.get("payload", {})

    parts = topic.split("/")
    if len(parts) < 3:
        return

    room_id  = parts[1]
    category = parts[2]

    if category == "sensors":
        bus = MessageBus.get_instance()
        r   = bus.get_redis()

        # FIX C1: Dùng update_cached_sensors() thay vì ghi trực tiếp vào dict
        update_cached_sensors(room_id, payload)

        r.setex(f"sensor:{room_id}", 300, json.dumps(payload))

        conn = get_db()
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for s_type, value in payload.items():
                if isinstance(value, (int, float)):
                    conn.execute(
                        "INSERT INTO sensor_data (room, type, value, timestamp) VALUES (?,?,?,?)",
                        (room_id, s_type, float(value), now)
                    )
            conn.commit()
        except Exception as e:
            print(f"[AUTO] sensor DB write error: {e}")
        finally:
            conn.close()

        process_sensor(room_id, payload)

        for s_type, value in payload.items():
            if isinstance(value, (int, float)):
                bus.publish_event("realtime_data", {
                    "room_id":   room_id,
                    "type":      s_type,
                    "value":     value,
                    "timestamp": datetime.now().isoformat()
                })

    elif category == "status":
        """
        FIX BUG-DEVICE-SYNC-01: Khi nhận status từ ESP32:
          1. Lưu SQLite device_status
          2. Publish "device_status" channel → firebase_sync cập nhật Firestore devices
          3. FIX BUG-ACK-01: Gửi command_ack nếu có pending command cho device này
        """
        bus = MessageBus.get_instance()
        r   = bus.get_redis()

        device_id = payload.get("device") or payload.get("deviceId") or payload.get("device_id")
        is_on     = bool(payload.get("is_on", payload.get("isOn", False)))

        if device_id:
            conn = get_db()
            try:
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                conn.execute(
                    "INSERT OR REPLACE INTO device_status (room, device_id, is_on, source, updated_at) VALUES (?,?,?,?,?)",
                    (room_id, device_id, 1 if is_on else 0,
                     payload.get("source", "esp32"), now)
                )
                conn.commit()
            except Exception as e:
                print(f"[AUTO] device_status DB error: {e}")
            finally:
                conn.close()

            # FIX BUG-DEVICE-SYNC-01 + FIX B1: Publish device_status để firebase_sync
            # cập nhật Firestore — đây là writer DUY NHẤT, không dùng force_update nữa
            bus.publish_event("device_status", {
                "room_id":   room_id,
                "device_id": device_id,
                "is_on":     is_on,
                "status":    "online",
                "name":      payload.get("name", device_id),
                "type":      payload.get("type", "")
            })

            # FIX BUG-ACK-01: Nếu đây là response của một web command,
            # publish command_ack để firebase_sync xóa command khỏi Firestore
            pending_cmd_id = PENDING_COMMANDS.pop(device_id, None)
            if pending_cmd_id:
                r.publish("command_ack", json.dumps({
                    "cmd_id": pending_cmd_id,
                    "status": "done",
                    "result": f"ESP32 confirmed: {device_id} is {'ON' if is_on else 'OFF'}"
                }))
                print(f"[AUTO] ACK sent for cmd {pending_cmd_id}: {device_id} → {is_on}")

            # Update local state cache
            cache_key = f"{room_id}_{device_id}"
            CACHED_DEVICE_STATES[cache_key] = is_on

            # [FIX] Khi ESP32 báo trạng thái từ nút vật lý (source="esp32" hoặc "button"),
            # cập nhật MANUAL_STATE để automation không ghi đè lên hành động vật lý.
            src = payload.get("source", "esp32")
            if src in ("esp32", "button", "physical"):
                MANUAL_STATE[device_id] = {
                    "mode":   "manual",
                    "is_on":  is_on,
                    "set_at": datetime.now(),
                    "source": "physical_button",
                }

    elif category == "alert":
        bus = MessageBus.get_instance()
        bus.publish_event("realtime_data", {
            "event":     "new_alert",
            "type":      payload.get("type", "device"),
            "room":      room_id,
            "message":   payload.get("message", "Cảnh báo từ thiết bị"),
            "level":     payload.get("level", "warning"),
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        })

    elif category == "auth":
        _check_enrollment_timeout()
        _handle_auth(room_id, payload)


def _handle_auth(room_id: str, payload: dict):
    uid = str(payload.get("cardUid") or payload.get("uid") or
              payload.get("fingerprintId", ""))
    bus = MessageBus.get_instance()

    if ENROLLMENT_STATE["active"]:
        owner_name = ENROLLMENT_STATE.get("pending_name", "Thẻ mới")
        try:
            conn = get_db()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO rfid_cards (uid, owner_name, is_active) VALUES (?,?,1)",
                    (uid, owner_name)
                )
                conn.commit()
            finally:
                conn.close()

            ENROLLMENT_STATE["active"]     = False
            ENROLLMENT_STATE["start_time"] = None

            bus.publish_mqtt(f"home/{room_id}/command", {
                "action":  "enrollment_success",
                "uid":     uid,
                "message": f"Da luu the: {owner_name}"
            })
            bus.publish_event("realtime_data", {
                "event":      "enrollment_success",
                "uid":        uid,
                "owner_name": owner_name
            })
            # [FIX] Ghi kết quả đăng ký về Firestore commands/entrance_register
            # để settings.js nhận được thông báo thành công qua onSnapshot listener
            bus.publish_event("rfid_enrollment_result", {
                "status":      "success",
                "result_type": "rfid",
                "value":       uid,
                "owner_name":  owner_name,
                "timestamp":   datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })
            print(f"[AUTH] Enrolled new card: {uid} -> {owner_name}")
        except Exception as e:
            print(f"[AUTH] enrollment error: {e}")
        return

    conn = get_db()
    try:
        card_row = conn.execute(
            "SELECT * FROM rfid_cards WHERE uid=? AND is_active=1", (uid,)
        ).fetchone()
    finally:
        conn.close()

    if card_row:
        owner = card_row["owner_name"]
        bus.publish_mqtt(f"home/{room_id}/command", {
            "action": "open_door", "message": f"Xin chao {owner}"
        })
        _log_access(room_id, uid, owner, "open_door", True)
        print(f"[AUTH] {room_id}: GRANTED -> {owner}")
    else:
        bus.publish_mqtt(f"home/{room_id}/command", {"action": "access_denied"})
        _log_access(room_id, uid, "Khách lạ", "attempt_failed", False)
        print(f"[AUTH] {room_id}: DENIED -> {uid}")


def _log_access(room_id, uid, user_name, action, success):
    try:
        conn = get_db()
        now  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            conn.execute(
                "INSERT INTO access_logs (room, uid, user_name, action, success, timestamp) VALUES (?,?,?,?,?,?)",
                (room_id, uid, user_name, action, 1 if success else 0, now)
            )
            conn.execute(
                "INSERT INTO notifications (type, title, message, room, created_at) VALUES (?,?,?,?,?)",
                ("access", "ACCESS LOGS",
                 f"{'thanh cong' if success else 'that bai'} {user_name} - {room_id}",
                 room_id, now)
            )
            conn.commit()
        finally:
            conn.close()
        MessageBus.get_instance().publish_event("realtime_data", {
            "event":     "access_log",
            "room":      room_id,
            "user_name": user_name,
            "success":   success,
            "timestamp": now
        })
    except Exception as e:
        print(f"[AUTH] log error: {e}")


def command_listener():
    """
    FIX D1: Tất cả device commands đi qua dispatch_command().
    FIX BUG-H-02: Chấp nhận cả "room" và "roomId".
    FIX BUG-ACK-01: Lưu cmd_id vào PENDING_COMMANDS[device_id].
    """
    # FIX SyntaxError: global phải khai báo TRƯỚC mọi thao tác với biến trong hàm
    global CACHED_AUTOMATIONS, CACHED_SCHEDULES

    bus    = MessageBus.get_instance()
    r      = bus.get_redis()
    pubsub = r.pubsub()
    pubsub.subscribe(
        "device_commands", "automation_commands",
        "rfid_commands",   "schedule_commands",
        "alert_commands",  CH_INBOUND,
        "rfid_register",   "wifi_setup"
    )
    print("[AUTO] Command listener started")

    for message in pubsub.listen():
        if message["type"] != "message":
            continue
        try:
            channel = message["channel"]
            data    = json.loads(message["data"])

            if channel == CH_INBOUND:
                handle_inbound(data)

            elif channel == "device_commands":
                # FIX BUG-H-02: chấp nhận cả "room" lẫn "roomId"
                room_id   = data.get("room") or data.get("roomId") or data.get("room_id", "")
                device_id = data.get("device_id") or data.get("deviceId", "")
                is_on     = data.get("is_on", False)
                cmd_id    = data.get("cmd_id", "")
                action    = data.get("action", "")

                if not room_id or not device_id:
                    print(f"[AUTO] device_commands: missing room or device_id: {data}")
                    continue

                # [v2.1] Lệnh chuyển về Auto mode từ Web
                if action == "set_auto_mode":
                    MANUAL_STATE.pop(device_id, None)
                    print(f"[AUTO] {device_id} → Auto mode (Automation re-enabled)")
                    bus.publish_event("realtime_data", {
                        "event":     "auto_mode_restored",
                        "room":      room_id,
                        "device_id": device_id,
                        "message":   f"{device_id} đã trở về chế độ tự động"
                    })
                    continue

                # [v2.1] Smart Manual Override: đặt mode="manual" cho thiết bị
                MANUAL_STATE[device_id] = {
                    "mode":   "manual",
                    "is_on":  data.get("is_on", False),
                    "set_at": datetime.now(),
                    "source": data.get("source", "web"),
                }

                # FIX BUG-ACK-01: Lưu cmd_id để gửi ACK khi ESP32 confirm
                if cmd_id:
                    PENDING_COMMANDS[device_id] = cmd_id

                # FIX D1: Dùng dispatch_command() — tự kiểm tra safety_lock
                mqtt_action = "turn_on" if is_on else "turn_off"
                dispatch_command(bus, "manual", room_id, device_id, mqtt_action,
                                 cmd_id=cmd_id,
                                 extra={"source": data.get("source", "web")})

            elif channel == "automation_commands":
                action  = data.get("action")
                room_id = data.get("room_id")
                if action == "upsert" and room_id:
                    CACHED_AUTOMATIONS[room_id] = data.get("rule", {})
                elif action == "delete" and room_id:
                    CACHED_AUTOMATIONS.pop(room_id, None)
                elif action == "reload_all":
                    # [FIX] Reload toàn bộ automation rules từ SQLite
                    conn = get_db()
                    try:
                        CACHED_AUTOMATIONS = {}
                        for row in conn.execute("SELECT * FROM automations WHERE enabled=1").fetchall():
                            CACHED_AUTOMATIONS[row["room_id"]] = dict(row)
                    finally:
                        conn.close()
                    print(f"[AUTO] CACHED_AUTOMATIONS reloaded: {len(CACHED_AUTOMATIONS)} rules")

            elif channel == "rfid_commands":
                action = data.get("action")
                uid    = data.get("uid", "")
                if action == "enroll":
                    ENROLLMENT_STATE["active"]       = True
                    ENROLLMENT_STATE["start_time"]   = datetime.now()
                    ENROLLMENT_STATE["pending_name"] = data.get("owner_name", "Thẻ mới")
                    print(f"[AUTO] Enrollment mode ON (timeout: {ENROLLMENT_TIMEOUT}s)")
                    # [FIX] Gửi MQTT tới ESP32 phòng khách (nơi có RFID reader)
                    # để ESP32 biết chuyển sang trạng thái chờ quét thẻ đăng ký
                    bus.publish_mqtt("home/living_room_01/command", {
                        "action":     "enroll",
                        "target":     "entrance",
                        "owner_name": data.get("owner_name", "Thẻ mới"),
                        "timeout":    ENROLLMENT_TIMEOUT,
                    })
                elif action == "delete" and uid:
                    bus.publish_mqtt("home/living_room_01/command", {
                        "action": "delete_user", "uid": uid
                    })

            elif channel == "rfid_register":
                action = data.get("action")
                if action == "start_register":
                    ENROLLMENT_STATE["active"]       = True
                    ENROLLMENT_STATE["start_time"]   = datetime.now()
                    ENROLLMENT_STATE["pending_name"] = data.get("owner_name", "Thẻ mới")
                    print("[AUTO] Enrollment started via Firebase command")
                    # [FIX] Gửi MQTT tới ESP32 để chuyển sang enrollment mode
                    bus.publish_mqtt("home/living_room_01/command", {
                        "action":     "enroll",
                        "target":     "entrance",
                        "owner_name": data.get("owner_name", "Thẻ mới"),
                        "timeout":    ENROLLMENT_TIMEOUT,
                    })
                elif action == "cancel_register":
                    ENROLLMENT_STATE["active"]     = False
                    ENROLLMENT_STATE["start_time"] = None
                    print("[AUTO] Enrollment cancelled via Firebase command")
                    # [FIX] Thông báo ESP32 hủy enrollment mode
                    bus.publish_mqtt("home/living_room_01/command", {
                        "action": "cancel_enroll",
                    })

            elif channel == "schedule_commands":
                if data.get("action") == "reload":
                    conn = get_db()
                    try:
                        CACHED_SCHEDULES = [dict(r) for r in
                                           conn.execute("SELECT * FROM schedules WHERE enabled=1").fetchall()]
                    finally:
                        conn.close()
                    print(f"[AUTO] Schedules reloaded: {len(CACHED_SCHEDULES)}")

        except Exception as e:
            print(f"[AUTO] command_listener error: {e}")


def run():
    load_cache()
    threading.Thread(target=scheduler_loop,   daemon=True).start()
    threading.Thread(target=command_listener, daemon=False).start()


if __name__ == "__main__":
    run()