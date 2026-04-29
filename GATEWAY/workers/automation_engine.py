"""
workers/automation_engine.py  — FIXED
═══════════════════════════════════════
FIXES:
  BUG-H-01: process_sensor() chỉ xử lý temperature → bổ sung humidity, co2, gas
  BUG-C-05: scheduler_loop() xóa schedule sau 1 lần chạy (enabled=0)
            Fix: KHÔNG xóa → schedules chạy lại mỗi ngày đúng giờ
            (Chỉ chạy 1 lần mỗi phút: dùng "last_run" tracking tránh chạy 60 lần/phút)
  BUG-H-02: command_listener nhận "roomId" từ Firebase/Web nhưng chỉ đọc "room"
            Fix: chấp nhận cả hai key: data.get("room") or data.get("roomId")
"""

import time
import json
import sqlite3
import threading
from datetime import datetime

from bridge.message_bus import MessageBus, CH_INBOUND
from workers import safety_watchdog   # share CACHED_SENSORS

DB_PATH                  = "/data/smarthome.db"
MANUAL_OVERRIDE_DURATION = 120   # giây
ENROLLMENT_TIMEOUT       = 60    # giây
CLOCK_WARN_YEAR          = 2024  # nếu năm < này → cảnh báo giờ sai

DEVICE_MAP = {
    "kitchen_01":     {"fan": "fan_kt_1",  "light": "light_kt_1"},
    "living_room_01": {"fan": "fan_lv_1",  "light": "light_lv_1"},
    "bedroom_01":     {"fan": "fan_bd_1",  "light": "light_bd_1"},
}

# ── State ──────────────────────────────────────────────────
CACHED_AUTOMATIONS:   dict = {}
CACHED_SCHEDULES:     list = []
CACHED_DEVICE_STATES: dict = {}
MANUAL_CONTROL_CACHE: dict = {}  # {device_id: datetime}
ENROLLMENT_STATE:     dict = {"active": False, "start_time": None, "pending_name": ""}

# FIX BUG-C-05: track schedule execution để tránh chạy nhiều lần trong cùng 1 phút
SCHEDULE_LAST_RUN: dict = {}  # {schedule_id: "HH:MM date"}


# ── DB helpers ────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def load_cache():
    global CACHED_AUTOMATIONS, CACHED_SCHEDULES
    conn = get_db()
    try:
        for row in conn.execute("SELECT * FROM automations WHERE enabled=1").fetchall():
            CACHED_AUTOMATIONS[row["room_id"]] = dict(row)
        CACHED_SCHEDULES = [dict(r) for r in conn.execute("SELECT * FROM schedules WHERE enabled=1").fetchall()]
    finally:
        conn.close()
    print(f"[AUTO] Cache loaded: {len(CACHED_AUTOMATIONS)} rules, {len(CACHED_SCHEDULES)} schedules")


# ── Automation logic ──────────────────────────────────────

def _is_safety_locked(room_id: str) -> bool:
    bus = MessageBus.get_instance()
    return bool(bus.get_redis().exists(f"safety_lock:{room_id}"))


def _try_control(bus: MessageBus, room_id: str, device_type: str,
                 value: float, threshold: float):
    """Kiểm tra rule và bắn lệnh MQTT nếu cần."""
    if _is_safety_locked(room_id):
        return

    device_id = DEVICE_MAP.get(room_id, {}).get(device_type)
    if not device_id:
        return

    # Kiểm tra schedule đang chạy
    for sched in CACHED_SCHEDULES:
        if sched.get("enabled") and sched.get("device_id") == device_id:
            return

    # Kiểm tra manual override
    last_manual = MANUAL_CONTROL_CACHE.get(device_id)
    if last_manual and (datetime.now() - last_manual).total_seconds() < MANUAL_OVERRIDE_DURATION:
        return

    should_on = value > threshold
    cache_key = f"{room_id}_{device_id}"
    if CACHED_DEVICE_STATES.get(cache_key) == should_on:
        return

    CACHED_DEVICE_STATES[cache_key] = should_on
    action = "turn_on" if should_on else "turn_off"
    bus.publish_mqtt(f"home/{room_id}/command", {
        "device": device_id, "action": action, "source": "automation"
    })
    _log_automation(room_id, f"auto_{device_type}",
                    [f"{device_id} → {action}"],
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
    """
    FIX BUG-H-01: Xử lý TẤT CẢ loại cảm biến, không chỉ temperature.
    - temperature → fan_threshold, light_threshold
    - humidity    → fan_threshold (quạt khi ẩm cao)
    - co2         → fan_threshold (quạt khi CO2 cao)
    - gas         → đã có safety_watchdog xử lý riêng, không xử lý ở đây
    """
    rule = CACHED_AUTOMATIONS.get(room_id)
    if not rule or not rule.get("enabled"):
        return

    bus = MessageBus.get_instance()

    # Nhiệt độ → quạt + đèn
    if "temperature" in sensor_data:
        val = float(sensor_data["temperature"])
        if rule.get("fan_threshold"):
            _try_control(bus, room_id, "fan", val, float(rule["fan_threshold"]))
        if rule.get("light_threshold"):
            _try_control(bus, room_id, "light", val, float(rule["light_threshold"]))

    # Độ ẩm → quạt (nếu humidity cao thì bật quạt)
    if "humidity" in sensor_data and rule.get("fan_threshold"):
        val = float(sensor_data["humidity"])
        _try_control(bus, room_id, "fan", val, float(rule["fan_threshold"]))

    # CO2 → quạt (nếu CO2 cao thì bật quạt thông gió)
    if "co2" in sensor_data and rule.get("fan_threshold"):
        val = float(sensor_data["co2"])
        # CO2 threshold thường cao hơn (ppm), dùng riêng nếu có, fallback fan_threshold * 10
        co2_thresh = float(rule.get("co2_threshold") or float(rule["fan_threshold"]) * 10)
        _try_control(bus, room_id, "fan", val, co2_thresh)


# ── Scheduler ─────────────────────────────────────────────

def _check_clock_validity() -> bool:
    if datetime.now().year < CLOCK_WARN_YEAR:
        bus = MessageBus.get_instance()
        bus.publish_event("realtime_data", {
            "event":   "system_warning",
            "message": "Giờ hệ thống chưa được đồng bộ! Kiểm tra module RTC DS3231.",
            "level":   "warning"
        })
        return False
    return True


def scheduler_loop():
    """
    FIX BUG-C-05: KHÔNG set enabled=0 sau khi chạy.
    Thay vào đó, dùng SCHEDULE_LAST_RUN để đảm bảo mỗi lịch chỉ chạy 1 lần/phút.
    Schedules có thể lặp lại mỗi ngày đúng giờ.
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
                if sched.get("time") != current_hhmm:
                    continue
                if not sched.get("device_id"):
                    continue

                # FIX BUG-C-05: kiểm tra đã chạy trong phút này chưa
                run_key = f"{sched['id']}_{current_hhmm}_{today_key}"
                if SCHEDULE_LAST_RUN.get(sched["id"]) == run_key:
                    continue  # Đã chạy trong phút này rồi, bỏ qua

                room_id   = sched["room_id"]
                device_id = sched["device_id"]
                action    = sched["action"]

                if _is_safety_locked(room_id):
                    print(f"[SCHEDULER] {room_id} is safety-locked, skip schedule")
                    continue

                bus.publish_mqtt(f"home/{room_id}/command", {
                    "device": device_id,
                    "action": action,
                    "source": "schedule"
                })
                MANUAL_CONTROL_CACHE[device_id] = datetime.now()

                # FIX BUG-C-05: ghi nhận đã chạy trong phút này (KHÔNG disabled)
                SCHEDULE_LAST_RUN[sched["id"]] = run_key

                print(f"[SCHEDULER] Executed: {room_id}/{device_id} → {action}")
                _log_automation(room_id, "schedule", [f"{device_id} → {action}"], "schedule")

            # Dọn cache SCHEDULE_LAST_RUN mỗi 24h để tránh memory leak
            if len(SCHEDULE_LAST_RUN) > 1000:
                SCHEDULE_LAST_RUN.clear()

            time.sleep(1.0 - (time.time() % 1.0))

        except Exception as e:
            print(f"[SCHEDULER] error: {e}")
            time.sleep(1)


# ── RFID Enrollment ───────────────────────────────────────

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


# ── MQTT inbound handler ──────────────────────────────────

def handle_inbound(envelope: dict):
    topic   = envelope.get("topic", "")
    payload = envelope.get("payload", {})

    parts = topic.split("/")
    if len(parts) < 3:
        return

    room_id  = parts[1]
    category = parts[2]

    # ── Dữ liệu cảm biến ──────────────────────────────────
    if category == "sensors":
        bus = MessageBus.get_instance()
        r   = bus.get_redis()

        # Cập nhật CACHED_SENSORS cho safety_watchdog
        cached = safety_watchdog.CACHED_SENSORS.setdefault(room_id, {})
        cached.update(payload)

        # Lưu vào Redis snapshot
        r.setex(f"sensor:{room_id}", 300, json.dumps(payload))

        # Lưu vào SQLite
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

        # Automation logic
        process_sensor(room_id, payload)

        # Push lên Firebase via publish
        for s_type, value in payload.items():
            if isinstance(value, (int, float)):
                bus.publish_event("realtime_data", {
                    "room_id":   room_id,
                    "type":      s_type,
                    "value":     value,
                    "timestamp": datetime.now().isoformat()
                })

    # ── Trạng thái thiết bị phản hồi từ ESP32 ─────────────
    elif category == "status":
        bus = MessageBus.get_instance()
        r   = bus.get_redis()

        device_id = payload.get("device")
        is_on     = bool(payload.get("is_on", False))

        if device_id:
            # Cập nhật SQLite device_status
            conn = get_db()
            try:
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                conn.execute(
                    "INSERT OR REPLACE INTO device_status (room, device_id, is_on, source, updated_at) VALUES (?,?,?,?,?)",
                    (room_id, device_id, 1 if is_on else 0, payload.get("source", "esp32"), now)
                )
                conn.commit()
            except Exception as e:
                print(f"[AUTO] device_status DB error: {e}")
            finally:
                conn.close()

            # Push lên Firebase
            bus.publish_event("device_status", {
                "room_id":   room_id,
                "device_id": device_id,
                "is_on":     is_on,
                "status":    "online",
                "name":      payload.get("name", device_id),
                "type":      payload.get("type", "")
            })

    # ── Cảnh báo từ thiết bị ─────────────────────────────
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

    # ── Xác thực RFID/vân tay ─────────────────────────────
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
            print(f"[AUTH] Enrolled new card: {uid} -> {owner_name}")
        except Exception as e:
            print(f"[AUTH] enrollment error: {e}")
        return

    conn     = get_db()
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
        bus.publish_mqtt(f"home/{room_id}/command", {
            "action": "access_denied"
        })
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
                 f"{'thanh cong' if success else 'that bai'} {user_name} - {room_id}", room_id, now)
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


# ── Redis command listener ────────────────────────────────

def command_listener():
    """
    FIX BUG-H-02: Chấp nhận cả "room" và "roomId" từ Web/Firebase
    để tránh lệnh bị bỏ qua do sai tên trường.
    """
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

                if not room_id or not device_id:
                    print(f"[AUTO] device_commands: missing room or device_id: {data}")
                    continue

                if _is_safety_locked(room_id) and data.get("source") in ("web", None):
                    print(f"[AUTO] {room_id} is safety-locked, web command blocked")
                    bus.publish_event("realtime_data", {
                        "event":   "command_blocked",
                        "room":    room_id,
                        "reason":  "safety_lock",
                        "message": "Hệ thống đang trong trạng thái khẩn cấp!"
                    })
                    continue

                MANUAL_CONTROL_CACHE[device_id] = datetime.now()
                bus.publish_mqtt(f"home/{room_id}/command", {
                    "device": device_id,
                    "action": "turn_on" if is_on else "turn_off",
                    "source": data.get("source", "web"),
                    "cmd_id": data.get("cmd_id", "")
                })

            elif channel == "automation_commands":
                action  = data.get("action")
                room_id = data.get("room_id")
                if action == "upsert" and room_id:
                    CACHED_AUTOMATIONS[room_id] = data.get("rule", {})
                elif action == "delete" and room_id:
                    CACHED_AUTOMATIONS.pop(room_id, None)

            elif channel == "rfid_commands":
                action = data.get("action")
                uid    = data.get("uid", "")
                if action == "enroll":
                    ENROLLMENT_STATE["active"]       = True
                    ENROLLMENT_STATE["start_time"]   = datetime.now()
                    ENROLLMENT_STATE["pending_name"] = data.get("owner_name", "Thẻ mới")
                    print(f"[AUTO] Enrollment mode ON (timeout: {ENROLLMENT_TIMEOUT}s)")
                elif action == "delete" and uid:
                    bus.publish_mqtt("home/entrance_01/command", {
                        "action": "delete_user", "uid": uid
                    })

            elif channel == "rfid_register":
                # Lệnh từ Firebase qua firebase_sync (start_register / cancel_register)
                action = data.get("action")
                if action == "start_register":
                    ENROLLMENT_STATE["active"]       = True
                    ENROLLMENT_STATE["start_time"]   = datetime.now()
                    ENROLLMENT_STATE["pending_name"] = data.get("owner_name", "Thẻ mới")
                    print("[AUTO] Enrollment started via Firebase command")
                elif action == "cancel_register":
                    ENROLLMENT_STATE["active"]     = False
                    ENROLLMENT_STATE["start_time"] = None
                    print("[AUTO] Enrollment cancelled via Firebase command")

            elif channel == "schedule_commands":
                if data.get("action") == "reload":
                    conn = get_db()
                    global CACHED_SCHEDULES
                    try:
                        CACHED_SCHEDULES = [dict(r) for r in
                                           conn.execute("SELECT * FROM schedules WHERE enabled=1").fetchall()]
                    finally:
                        conn.close()
                    print(f"[AUTO] Schedules reloaded: {len(CACHED_SCHEDULES)}")

        except Exception as e:
            print(f"[AUTO] command_listener error: {e}")


def run():
    """Entry point – khởi động automation engine."""
    load_cache()
    threading.Thread(target=scheduler_loop,    daemon=True).start()
    threading.Thread(target=command_listener,  daemon=False).start()  # blocking


if __name__ == "__main__":
    run()