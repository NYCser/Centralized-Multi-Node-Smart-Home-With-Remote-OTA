"""
workers/automation_engine.py  — v2.1
══════════════════════════════════════
THAY ĐỔI SO VỚI v2.0:

  [B] SMART MANUAL OVERRIDE (State-based thay vì đếm ngược 120s)
      ─────────────────────────────────────────────────────────
      Trước đây (v2.0):
        - Người dùng bật quạt thủ công → MANUAL_CONTROL_CACHE[device] = now()
        - Automation ngừng tác động 120 giây (đếm ngược cứng)
        - Sau 120s: Automation lại bật quạt nếu nhiệt độ vẫn cao (OK)
        - Nhưng: Người dùng tắt quạt để ngủ, 2 phút sau quạt tự bật lại (BAD)

      Bây giờ (v2.1):
        - Người dùng điều khiển thủ công → device vào trạng thái "Manual" (User-Locked)
          MANUAL_STATE[device_id] = {"mode": "manual", "is_on": True/False, ...}
        - Automation KHÔNG được tác động khi device đang ở mode "manual"
        - Người dùng chuyển về mode "auto" trên Web → Automation tiếp tục
        - Nếu người dùng không chuyển, trạng thái manual được giữ cho đến khi:
          a) User bấm nút "Auto" trên Web (ưu tiên)
          b) Điều kiện an toàn nguy hiểm (safety_lock override)
        - Lợi ích: Không còn bực bội vì quạt tự bật lại sau 2 phút.

  [C] PER-ROOM THRESHOLDS từ DB (Dynamic thay vì Hard-code)
      ─────────────────────────────────────────────────────
      _try_control() giờ đọc threshold từ CACHED_AUTOMATIONS (SQLite bảng
      automations) thay vì dùng hằng số cứng. Mỗi phòng có thể có ngưỡng
      nhiệt độ khác nhau qua Web Settings → POST /automations.

  (Giữ nguyên: BUG-ACK-01, BUG-DEVICE-SYNC-01, BUG-H-01, BUG-C-05, BUG-H-02)
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

# [v2.1] Xóa MANUAL_OVERRIDE_DURATION — không còn đếm ngược 120s
# MANUAL_OVERRIDE_DURATION = 120  ← ĐÃ XÓA

DEVICE_MAP = {
    "kitchen_01":     {"fan": "fan_kt_1",  "light": "light_kt_1"},
    "living_room_01": {"fan": "fan_lv_1",  "light": "light_lv_1"},
    "bedroom_01":     {"fan": "fan_bd_1",  "light": "light_bd_1"},
}

CACHED_AUTOMATIONS:   dict = {}
CACHED_SCHEDULES:     list = []
CACHED_DEVICE_STATES: dict = {}

# [v2.1] MANUAL_STATE thay cho MANUAL_CONTROL_CACHE + timestamp
# { device_id: {"mode": "manual"|"auto", "is_on": bool, "set_at": datetime} }
# mode="manual" → Automation bị vô hiệu hóa cho thiết bị này cho đến khi user chọn "auto"
# mode="auto"   → Automation hoạt động bình thường
MANUAL_STATE:         dict = {}

ENROLLMENT_STATE:     dict = {"active": False, "start_time": None, "pending_name": ""}
SCHEDULE_LAST_RUN:    dict = {}

# FIX BUG-ACK-01: Track pending commands để gửi ACK khi ESP32 confirm
# { device_id: cmd_id } — xóa sau khi nhận status từ ESP32
PENDING_COMMANDS: dict = {}


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
    if _is_safety_locked(room_id):
        return

    device_id = DEVICE_MAP.get(room_id, {}).get(device_type)
    if not device_id:
        return

    for sched in CACHED_SCHEDULES:
        if sched.get("enabled") and sched.get("device_id") == device_id:
            return

    # [v2.1] Smart Manual Override: kiểm tra mode thay vì đếm ngược thời gian
    manual = MANUAL_STATE.get(device_id)
    if manual and manual.get("mode") == "manual":
        return  # User đang ở mode manual → Automation không can thiệp

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

    CACHED_DEVICE_STATES[cache_key] = should_on
    DEVICE_LAST_SWITCH[cache_key]   = now
    action = "turn_on" if should_on else "turn_off"
    bus.publish_mqtt(f"home/{room_id}/command", {
        "device": device_id, "action": action, "source": "automation"
    })
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
    """FIX BUG-C-05: Không set enabled=0 sau khi chạy."""
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

                run_key = f"{sched['id']}_{current_hhmm}_{today_key}"
                if SCHEDULE_LAST_RUN.get(sched["id"]) == run_key:
                    continue

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
                # Schedule override không đặt manual state — vẫn theo schedule
                SCHEDULE_LAST_RUN[sched["id"]]  = run_key

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

        cached = safety_watchdog.CACHED_SENSORS.setdefault(room_id, {})
        cached.update(payload)

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

        device_id = payload.get("device")
        is_on     = bool(payload.get("is_on", False))

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

            # FIX BUG-DEVICE-SYNC-01: Publish device_status để firebase_sync
            # cập nhật Firestore → Web toggle button đúng trạng thái
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
    FIX BUG-H-02: Chấp nhận cả "room" và "roomId".
    FIX BUG-ACK-01: Lưu cmd_id vào PENDING_COMMANDS[device_id]
                    để gửi ACK khi ESP32 xác nhận.
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

                # [v2.1] Smart Manual Override: đặt mode="manual" cho thiết bị
                # Automation sẽ không tác động cho đến khi user chuyển về "auto"
                MANUAL_STATE[device_id] = {
                    "mode":   "manual",
                    "is_on":  data.get("is_on", False),
                    "set_at": datetime.now(),
                    "source": data.get("source", "web"),
                }

                # FIX BUG-ACK-01: Lưu cmd_id để gửi ACK khi ESP32 confirm
                if cmd_id:
                    PENDING_COMMANDS[device_id] = cmd_id

                bus.publish_mqtt(f"home/{room_id}/command", {
                    "device": device_id,
                    "action": "turn_on" if is_on else "turn_off",
                    "source": data.get("source", "web"),
                    "cmd_id": cmd_id
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
    load_cache()
    threading.Thread(target=scheduler_loop,   daemon=True).start()
    threading.Thread(target=command_listener, daemon=False).start()


if __name__ == "__main__":
    run()