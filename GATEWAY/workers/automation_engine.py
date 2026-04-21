"""
workers/automation_engine.py
════════════════════════════
Automation Engine – xử lý:
  1. Logic IF/THEN từ cảm biến (nhiệt độ, gas)
  2. Scheduler hẹn giờ (đọc giờ hệ thống đã sync DS3231)
  3. RFID Enrollment mode với timeout
  4. Lắng nghe lệnh từ Web (device_commands, automation_commands, ...)
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


# ── DB helpers ────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def load_cache():
    global CACHED_AUTOMATIONS, CACHED_SCHEDULES
    conn = get_db()
    for row in conn.execute("SELECT * FROM automations WHERE enabled=1").fetchall():
        CACHED_AUTOMATIONS[row["room_id"]] = dict(row)
    CACHED_SCHEDULES = [dict(r) for r in conn.execute("SELECT * FROM schedules WHERE enabled=1").fetchall()]
    conn.close()
    print(f"[AUTO] Cache loaded: {len(CACHED_AUTOMATIONS)} rules, {len(CACHED_SCHEDULES)} schedules")


# ── Automation logic ──────────────────────────────────────

def _is_safety_locked(room_id: str) -> bool:
    """Kiểm tra xem phòng có đang trong trạng thái khẩn cấp không."""
    bus = MessageBus.get_instance()
    return bool(bus.get_redis().exists(f"safety_lock:{room_id}"))


def _try_control(bus: MessageBus, room_id: str, device_type: str,
                 value: float, threshold: float):
    """Kiểm tra rule và bắn lệnh MQTT nếu cần."""
    if _is_safety_locked(room_id):
        return  # Safety first – không can thiệp

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
        return  # Không đổi trạng thái

    CACHED_DEVICE_STATES[cache_key] = should_on
    action = "turn_on" if should_on else "turn_off"
    bus.publish_mqtt(f"home/{room_id}/command", {
        "device": device_id, "action": action, "source": "automation"
    })

    # Log automation
    _log_automation(room_id, f"auto_{device_type}",
                    [f"{device_id} → {action}"],
                    f"sensor_{device_type}")


def _log_automation(room_id: str, scenario: str, actions: list, triggered_by: str):
    try:
        conn = get_db()
        conn.execute(
            "INSERT INTO automation_logs (room, scenario, actions, triggered_by) VALUES (?,?,?,?)",
            (room_id, scenario, json.dumps(actions), triggered_by)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[AUTO] log error: {e}")


def process_sensor(room_id: str, sensor_data: dict):
    """Chạy logic automation khi nhận được dữ liệu cảm biến."""
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


# ── Scheduler ─────────────────────────────────────────────

def _check_clock_validity() -> bool:
    """Kiểm tra giờ hệ thống có hợp lệ không (đã sync DS3231 chưa)."""
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
    So sánh giờ hệ thống (đã sync DS3231) với bảng schedules.
    Chạy trong thread daemon riêng.
    """
    print("[SCHEDULER] Started")
    while True:
        try:
            if not _check_clock_validity():
                time.sleep(60)
                continue

            now          = datetime.now()
            current_hhmm = now.strftime("%H:%M")
            bus          = MessageBus.get_instance()

            for sched in list(CACHED_SCHEDULES):
                if (sched.get("enabled") and
                        sched.get("time") == current_hhmm and
                        sched.get("device_id")):

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
                    print(f"[SCHEDULER] Executed: {room_id}/{device_id} → {action}")

                    # Xoá schedule sau khi thực thi (one-shot)
                    conn = get_db()
                    conn.execute("UPDATE schedules SET enabled=0 WHERE id=?", (sched["id"],))
                    conn.commit()
                    conn.close()
                    CACHED_SCHEDULES.remove(sched)
                    _log_automation(room_id, "schedule", [f"{device_id} → {action}"], "schedule")

            # Đồng bộ chính xác với đồng hồ
            time.sleep(1.0 - (time.time() % 1.0))

        except Exception as e:
            print(f"[SCHEDULER] error: {e}")
            time.sleep(1)


# ── RFID Enrollment ───────────────────────────────────────

def _enrollment_timeout_watcher():
    """Tự đóng chế độ enrollment sau ENROLLMENT_TIMEOUT giây."""
    while True:
        time.sleep(5)
        state = ENROLLMENT_STATE
        if state["active"] and state["start_time"]:
            elapsed = (datetime.now() - state["start_time"]).total_seconds()
            if elapsed > ENROLLMENT_TIMEOUT:
                state["active"]     = False
                state["start_time"] = None
                bus = MessageBus.get_instance()
                bus.publish_event("realtime_data", {
                    "event":   "enrollment_timeout",
                    "message": "Chế độ nạp thẻ đã hết thời gian (60s)"
                })
                print("[AUTO] Enrollment mode timed out")


threading.Thread(target=_enrollment_timeout_watcher, daemon=True).start()


# ── MQTT Inbound handler ──────────────────────────────────

def handle_inbound(envelope: dict):
    """
    Xử lý tin nhắn đến từ MQTT (qua CH_INBOUND của MessageBus).
    envelope = {"topic": "...", "payload": {...}, "ts": ...}
    """
    topic   = envelope.get("topic", "")
    payload = envelope.get("payload", {})
    parts   = topic.split("/")

    if len(parts) < 3:
        return

    room_id  = parts[1]
    category = parts[2]

    # ── Cảm biến ──────────────────────────────────────────
    if category == "sensors":
        safety_watchdog.CACHED_SENSORS.setdefault(room_id, {}).update(payload)
        
        # Push từng cảm biến vào Redis buffer để data_syncer flush vào SQLite
        bus = MessageBus.get_instance()
        r = bus.get_redis()
        for sensor_type, value in payload.items():
            buffer_item = {
                "room": room_id,
                "type": sensor_type,
                "value": value,
                "timestamp": datetime.now().isoformat()
            }
            r.rpush("sensor_buffer", json.dumps(buffer_item))
        
        process_sensor(room_id, payload)

    # ── Phản hồi trạng thái thiết bị ──────────────────────
    elif category == "status":
        device_id = payload.get("deviceId") or payload.get("device_id")
        is_on     = payload.get("isOn", payload.get("is_on", False))
        if device_id:
            CACHED_DEVICE_STATES[f"{room_id}_{device_id}"] = is_on

    # ── Cảnh báo từ ESP ───────────────────────────────────
    elif category == "alert":
        bus = MessageBus.get_instance()
        bus.publish_event("realtime_data", {
            "event":   "new_alert",
            "type":    payload.get("type", "device"),
            "room":    room_id,
            "message": payload.get("message", "Cảnh báo từ thiết bị"),
            "level":   payload.get("level", "warning"),
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        })

    # ── Xác thực RFID/vân tay ─────────────────────────────
    elif category == "auth":
        _handle_auth(room_id, payload)


def _handle_auth(room_id: str, payload: dict):
    """Kiểm tra thẻ RFID hoặc vân tay."""
    uid = str(payload.get("cardUid") or payload.get("uid") or
              payload.get("fingerprintId", ""))
    bus = MessageBus.get_instance()

    # Chế độ Enrollment đang bật → đăng ký thẻ mới
    if ENROLLMENT_STATE["active"]:
        owner_name = ENROLLMENT_STATE.get("pending_name", "Thẻ mới")
        try:
            conn = get_db()
            conn.execute(
                "INSERT OR REPLACE INTO rfid_cards (uid, owner_name, is_active) VALUES (?,?,1)",
                (uid, owner_name)
            )
            conn.commit()
            conn.close()

            ENROLLMENT_STATE["active"]     = False
            ENROLLMENT_STATE["start_time"] = None

            # Phản hồi LCD của ESP
            bus.publish_mqtt(f"home/{room_id}/command", {
                "action":  "enrollment_success",
                "uid":     uid,
                "message": f"Da luu the: {owner_name}"
            })
            # Thông báo Web
            bus.publish_event("realtime_data", {
                "event":     "enrollment_success",
                "uid":       uid,
                "owner_name": owner_name
            })
            print(f"[AUTH] Enrolled new card: {uid} -> {owner_name}")
        except Exception as e:
            print(f"[AUTH] enrollment error: {e}")
        return

    # Chế độ bình thường → kiểm tra DB
    conn     = get_db()
    card_row = conn.execute(
        "SELECT * FROM rfid_cards WHERE uid=? AND is_active=1", (uid,)
    ).fetchone()
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
        conn.close()
        # Push realtime
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

    """Lắng nghe lệnh từ Web (qua Redis pubsub).
    Thay thế Firebase on_snapshot.
    """
    bus    = MessageBus.get_instance()
    r      = bus.get_redis()
    pubsub = r.pubsub()
    pubsub.subscribe(
        "device_commands", "automation_commands",
        "rfid_commands",   "schedule_commands",
        "alert_commands",  CH_INBOUND
    )
    print("[AUTO] Command listener started")

    for message in pubsub.listen():
        if message["type"] != "message":
            continue
        try:
            channel = message["channel"]
            data    = json.loads(message["data"])

            # ── MQTT inbound từ MessageBus ─────────────────
            if channel == CH_INBOUND:
                handle_inbound(data)

            # ── Lệnh điều khiển thiết bị từ Web ───────────
            elif channel == "device_commands":
                room_id   = data["room"]
                device_id = data["device_id"]
                is_on     = data["is_on"]

                if _is_safety_locked(room_id) and data.get("source") == "web":
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
                    "source": "web"
                })

            # ── Cập nhật automation rule ───────────────────
            elif channel == "automation_commands":
                action  = data.get("action")
                room_id = data.get("room_id")
                if action == "upsert" and room_id:
                    CACHED_AUTOMATIONS[room_id] = data.get("rule", {})
                elif action == "delete" and room_id:
                    CACHED_AUTOMATIONS.pop(room_id, None)

            # ── RFID commands ──────────────────────────────
            elif channel == "rfid_commands":
                action = data.get("action")
                uid    = data.get("uid", "")
                if action == "enroll":
                    ENROLLMENT_STATE["active"]       = True
                    ENROLLMENT_STATE["start_time"]   = datetime.now()
                    ENROLLMENT_STATE["pending_name"] = data.get("owner_name", "Thẻ mới")
                    print(f"[AUTO] Enrollment mode ON (timeout: {ENROLLMENT_TIMEOUT}s)")
                elif action == "add" and uid:
                    pass  # Đã xử lý qua API trực tiếp
                elif action == "delete" and uid:
                    bus.publish_mqtt("home/entrance_01/command", {
                        "action": "delete_user", "uid": uid
                    })

            # ── Schedule commands ──────────────────────────
            elif channel == "schedule_commands":
                if data.get("action") == "reload":
                    conn = get_db()
                    global CACHED_SCHEDULES
                    CACHED_SCHEDULES = [dict(r) for r in
                                       conn.execute("SELECT * FROM schedules WHERE enabled=1").fetchall()]
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
