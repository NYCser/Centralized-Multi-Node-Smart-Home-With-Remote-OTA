"""
workers/safety_watchdog.py  — FIXED v3
══════════════════════════════════════════════════════
FIXES trong phiên bản này:

  [FIX-ALERT-1 — CRITICAL] _save_alert không publish lên "safety_alert" channel
      ─────────────────────────────────────────────────────────────────────────
      Vấn đề: UplinkStream trong firebase_sync.py subscribe "safety_alert" channel
              để push cảnh báo lên Firestore system_alerts, nhưng _save_alert()
              chỉ publish lên "realtime_data" (dùng cho SocketIO/Web), KHÔNG publish
              lên "safety_alert" → cảnh báo gas/fire không bao giờ vào Firestore.
      Fix: _save_alert() thêm bus.get_redis().publish("safety_alert", ...) sau khi
           lưu SQLite, để UplinkStream bắt được và push lên Firestore.

  [FIX-ALERT-2 — MEDIUM] Cơ chế an toàn khi safety lock tự refresh
      ─────────────────────────────────────────────────────────────────────────
      Vấn đề: Khi is_dangerous == True và was_dangerous == True, _set_safety_lock()
              được gọi mỗi 1s (WATCHDOG_TICK) thay vì mỗi 30s như comment.
              Gây spam Redis setex không cần thiết.
      Fix: Thêm biến last_lock_refresh tracking 30s per room.

  [Giữ nguyên từ v2]
      BUG-10: was_dangerous flag
      FIX C1: Thread-safe CACHED_SENSORS qua get_cached_sensors()
"""

import time
import json
import sqlite3
import threading
from datetime import datetime
from bridge.message_bus import MessageBus, CH_INBOUND
from workers import event_logger

# ── Config ────────────────────────────────────────────────
SAFETY_MUTE_TIMEOUT     = 600   # 10 phút — mute manual qua API vẫn giữ nguyên
# [SMART-MUTE] Khi user "đã đọc" thông báo trên Web → buzzer tắt 3 phút
# Sau 3 phút nếu vẫn còn nguy hiểm → buzzer kêu lại
SMART_MUTE_DURATION     = 180   # 3 phút (user đọc thông báo → buzzer tắt)
SAFETY_REPEAT_INTERVAL  = 300   # 5 phút → lưu alert lại (không spam DB)
WATCHDOG_TICK           = 1.0
GAS_DEFAULT_THRESHOLD   = 600
DB_PATH                 = "/data/smarthome.db"
LOCK_REFRESH_INTERVAL   = 30    # [FIX-ALERT-2] Chỉ refresh safety_lock mỗi 30s

DEVICE_MAP = {
    "kitchen_01":     {"fan": "fan_kt_1",  "light": "light_kt_1"},
    "living_room_01": {"fan": "fan_lv_1",  "light": "light_lv_1"},
    "bedroom_01":     {"fan": "fan_bd_1",  "light": "light_bd_1"},
}

# ── State ─────────────────────────────────────────────────
SAFETY_STATE: dict   = {}

# CACHED_SENSORS: vẫn giữ để backward compat với code khác có thể import
# NHƯNG safety watchdog KHÔNG đọc trực tiếp — dùng get_cached_sensors() thay thế
CACHED_SENSORS: dict = {}


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _save_alert(room_id: str, alert_type: str, message: str):
    """
    [FIX-ALERT-1] Lưu alert vào SQLite + publish lên CẢ HAI channel:
      1. "realtime_data"  → SocketIO → Web dashboard (hiển thị ngay)
      2. "safety_alert"   → UplinkStream → Firestore system_alerts (lưu lịch sử Firebase)
    Trước đây chỉ publish "realtime_data" → Firestore không bao giờ có dữ liệu gas/fire.
    """
    bus = MessageBus.get_instance()
    conn = get_db()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn.execute(
            "INSERT INTO system_alerts (room,type,message,level,timestamp) VALUES (?,?,?,?,?)",
            (room_id, alert_type, message, "critical", now)
        )
        conn.execute(
            "INSERT INTO notifications (type,title,message,room,created_at) VALUES (?,?,?,?,?)",
            (alert_type.upper() + "_ALERT",
             "GAS ALERT" if alert_type == "gas" else "FIRE ALERT",
             message, room_id, now)
        )
        conn.commit()
    finally:
        conn.close()

    # Publish lên "realtime_data" cho SocketIO/Web
    bus.publish_event("realtime_data", {
        "event":   "new_alert", "type": alert_type,
        "room":    room_id, "message": message,
        "level":   "critical", "timestamp": now
    })

    # [FIX-ALERT-1] Publish lên "safety_alert" cho UplinkStream → Firestore
    # Format phù hợp với UplinkStream._on_alert() trong firebase_sync.py
    alert_payload = json.dumps({
        "type":     alert_type,
        "message":  message,
        "level":    "critical",
        "room":     room_id,
        "location": room_id,
        "timestamp": now
    })
    try:
        bus.get_redis().publish("safety_alert", alert_payload)
    except Exception as e:
        print(f"[WATCHDOG] safety_alert publish error: {e}")

    try:
        event_logger.log_safety_alert(room_id=room_id, alert_type=alert_type, message=message)
    except Exception as e:
        print(f"[WATCHDOG] event_logger safety log error: {e}")

    # Lưu active alert vào Redis để /system/safety_status endpoint trả về
    bus.get_redis().setex(
        f"active_alert:{room_id}",
        SAFETY_MUTE_TIMEOUT * 2,
        json.dumps({"type": alert_type, "message": message, "timestamp": now})
    )


def _set_safety_lock(room_id: str, locked: bool):
    bus = MessageBus.get_instance()
    key = f"safety_lock:{room_id}"
    if locked:
        bus.get_redis().setex(key, SAFETY_REPEAT_INTERVAL + 60, "1")
    else:
        bus.get_redis().delete(key)
    bus.publish_event("realtime_data", {
        "event": "safety_lock", "room": room_id, "locked": locked
    })


def _trigger_safety_action(bus: MessageBus, room_id: str, alert_type: str, value: float):
    """
    FIX BUG-10: Chỉ gọi khi trạng thái THAY ĐỔI (was_dangerous flag).
    FIX D1: Safety action dùng source="safety" — vượt qua mọi lock khác.
    """
    fan_id = DEVICE_MAP.get(room_id, {}).get("fan")
    if fan_id:
        bus.publish_mqtt(f"home/{room_id}/command", {
            "device": fan_id, "action": "turn_on", "source": "safety"
        })
    bus.publish_mqtt(f"home/{room_id}/command", {
        "action": "buzz_alarm", "type": alert_type, "value": value, "source": "safety"
    })
    _set_safety_lock(room_id, True)


def _mute_safety_action(bus: MessageBus, room_id: str):
    bus.publish_mqtt(f"home/{room_id}/command", {"action": "mute_alarm"})


def run():
    bus   = MessageBus.get_instance()
    redis = bus.get_redis()

    # Import ở đây để tránh circular import
    import importlib

    def _get_sensors(room_id: str) -> dict:
        """
        FIX C1: Đọc sensor data qua thread-safe API của automation_engine.
        Fallback về CACHED_SENSORS local nếu import thất bại.
        """
        try:
            ae = importlib.import_module("workers.automation_engine")
            return ae.get_cached_sensors(room_id)
        except Exception:
            return dict(CACHED_SENSORS.get(room_id, {}))

    def listen_mute():
        pubsub = redis.pubsub()
        pubsub.subscribe("alert_commands")
        for msg in pubsub.listen():
            if msg["type"] != "message":
                continue
            try:
                data    = json.loads(msg["data"])
                action  = data.get("action")
                room_id = data.get("room_id")
                if action == "mute" and room_id:
                    # Mute thủ công (từ nút Tắt còi trên Web)
                    state = SAFETY_STATE.setdefault(room_id, {})
                    state["muted"]     = True
                    state["mute_time"] = datetime.now()
                    _mute_safety_action(bus, room_id)
                    print(f"[WATCHDOG] {room_id} muted by user (manual)")
                elif action == "smart_mute" and room_id:
                    # [SMART-MUTE] User đánh dấu đã đọc thông báo trên Web
                    # → tắt buzzer 3 phút, sau đó tự động kêu lại nếu còn nguy hiểm
                    state = SAFETY_STATE.setdefault(room_id, {})
                    state["smart_muted"]     = True
                    state["smart_mute_time"] = datetime.now()
                    _mute_safety_action(bus, room_id)
                    print(f"[WATCHDOG] {room_id} smart_muted (alert read) — buzzer off for {SMART_MUTE_DURATION}s")
            except Exception as e:
                print(f"[WATCHDOG] mute listener error: {e}")

    threading.Thread(target=listen_mute, daemon=True).start()
    print("[WATCHDOG] Safety watchdog started (thread-safe sensor read + Firestore alert sync)")

    while True:
        try:
            now  = datetime.now()
            conn = get_db()
            try:
                rules = conn.execute("SELECT * FROM automations WHERE enabled=1").fetchall()
            finally:
                conn.close()

            for rule_row in rules:
                rule    = dict(rule_row)
                room_id = rule["room_id"]

                # FIX C1: Dùng _get_sensors() thay vì đọc trực tiếp CACHED_SENSORS
                sensors = _get_sensors(room_id)

                state   = SAFETY_STATE.setdefault(room_id, {
                    "muted":             False,
                    "mute_time":         None,
                    "last_alert":        None,
                    "was_dangerous":     False,
                    "last_lock_refresh": None,  # [FIX-ALERT-2]
                    "smart_muted":       False,  # [SMART-MUTE] user đánh dấu đã đọc
                    "smart_mute_time":   None,   # [SMART-MUTE] thời điểm user đọc thông báo
                })

                gas_threshold = float(rule.get("gas_threshold") or GAS_DEFAULT_THRESHOLD)
                current_gas   = float(sensors.get("gas", 0))
                is_fire       = bool(sensors.get("fire_detected", False))
                is_dangerous  = (current_gas > gas_threshold) or is_fire
                alert_type    = "fire" if is_fire else "gas"
                alert_msg     = (
                    f"PHÁT HIỆN LỬA! Phòng: {room_id}" if is_fire
                    else f"RÒ RỈ KHÍ GAS! {current_gas:.0f} ppm - Phòng: {room_id}"
                )

                if is_dangerous:
                    # FIX BUG-10: Chỉ trigger hardware khi LẦN ĐẦU phát hiện nguy hiểm
                    if not state.get("was_dangerous"):
                        _trigger_safety_action(bus, room_id, alert_type, current_gas)
                        state["was_dangerous"]     = True
                        state["last_lock_refresh"] = now
                        # Reset smart_mute khi phát hiện nguy hiểm MỚI
                        state["smart_muted"]       = False
                        state["smart_mute_time"]   = None
                        # Log safety alert
                        event_logger.log_safety_alert(
                            room_id=room_id,
                            alert_type=alert_type,
                            value=current_gas,
                            message=alert_msg
                        )
                        print(f"[WATCHDOG] DANGER DETECTED {room_id}: {alert_msg}")
                    else:
                        # [FIX-ALERT-2] Đã nguy hiểm — refresh safety_lock mỗi 30s
                        last_refresh = state.get("last_lock_refresh")
                        if last_refresh is None or (now - last_refresh).total_seconds() >= LOCK_REFRESH_INTERVAL:
                            _set_safety_lock(room_id, True)
                            state["last_lock_refresh"] = now

                    # [SMART-MUTE] Kiểm tra xem buzzer có đang bị tắt tạm thời không
                    is_smart_muted = state.get("smart_muted", False)
                    if is_smart_muted:
                        smart_mute_time = state.get("smart_mute_time")
                        smart_elapsed = (now - smart_mute_time).total_seconds() if smart_mute_time else 9999
                        if smart_elapsed >= SMART_MUTE_DURATION:
                            # Hết 3 phút → bật buzzer lại nếu còn nguy hiểm
                            state["smart_muted"]     = False
                            state["smart_mute_time"] = None
                            bus.publish_mqtt(f"home/{room_id}/command", {
                                "action": "buzz_alarm",
                                "type":   alert_type,
                                "value":  current_gas,
                                "source": "safety",
                                "reason": "smart_mute_expired"
                            })
                            bus.publish_event("realtime_data", {
                                "event":     "new_alert",
                                "type":      alert_type,
                                "room":      room_id,
                                "message":   f"[Nhắc lại] {alert_msg}",
                                "level":     "critical",
                                "timestamp": now.strftime("%Y-%m-%d %H:%M:%S")
                            })
                            print(f"[WATCHDOG] {room_id}: smart_mute expired — buzzer reactivated")

                    # Lưu alert theo interval
                    if state.get("muted"):
                        mute_time = state.get("mute_time")
                        elapsed   = (now - mute_time).total_seconds() if mute_time else 9999
                        if elapsed > SAFETY_MUTE_TIMEOUT:
                            state["muted"] = False
                            print(f"[WATCHDOG] {room_id}: mute timeout, re-alerting")
                        last = state.get("last_alert")
                        if last and (now - last).total_seconds() > SAFETY_REPEAT_INTERVAL:
                            _save_alert(room_id, alert_type, alert_msg)
                            state["last_alert"] = now
                    else:
                        last = state.get("last_alert")
                        if not last or (now - last).total_seconds() > SAFETY_REPEAT_INTERVAL:
                            _save_alert(room_id, alert_type, alert_msg)
                            state["last_alert"] = now

                else:
                    # Hết nguy hiểm
                    if state.get("was_dangerous"):
                        _set_safety_lock(room_id, False)
                        state["was_dangerous"]     = False
                        state["last_alert"]        = None
                        state["muted"]             = False
                        state["last_lock_refresh"] = None
                        state["smart_muted"]       = False   # [SMART-MUTE] reset
                        state["smart_mute_time"]   = None

                        # Log safety resolved
                        event_logger.log_safety_resolved(room_id=room_id, alert_type=alert_type)

                        # Thông báo an toàn cho Web (realtime_data)
                        bus.publish_event("realtime_data", {
                            "event":     "new_alert",
                            "type":      "system",
                            "room":      room_id,
                            "message":   f"Phòng {room_id} đã an toàn.",
                            "level":     "info",
                            "timestamp": now.strftime("%Y-%m-%d %H:%M:%S")
                        })
                        # [FIX-ALERT-1] Cũng push "safe" notification lên Firestore
                        try:
                            bus.get_redis().publish("safety_alert", json.dumps({
                                "type":     "system",
                                "message":  f"Phòng {room_id} đã an toàn.",
                                "level":    "info",
                                "room":     room_id,
                                "location": room_id,
                                "timestamp": now.strftime("%Y-%m-%d %H:%M:%S")
                            }))
                        except Exception:
                            pass
                        print(f"[WATCHDOG] {room_id}: SAFE — lock released")

            time.sleep(WATCHDOG_TICK)

        except Exception as e:
            print(f"[WATCHDOG] loop error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    run()
