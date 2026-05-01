"""
workers/safety_watchdog.py  — FIXED v2 (CONFLICT FIX)
══════════════════════════════════════════════════════
FIXES trong phiên bản này:

  [CONFLICT C1 — MEDIUM] Thread-safe CACHED_SENSORS
      ─────────────────────────────────────────────
      Vấn đề: safety_watchdog đọc CACHED_SENSORS trực tiếp từ dict Python
              trong khi automation_engine đang ghi từ MQTT thread khác.
              Python GIL bảo vệ atomic ops nhưng KHÔNG bảo vệ dict.update()
              đang thực thi giữa chừng.
      Fix: Đọc sensor data qua automation_engine.get_cached_sensors(room_id)
           — hàm này dùng threading.RLock() và trả về copy của dict.
           CACHED_SENSORS trong file này vẫn giữ để backward compat nhưng
           KHÔNG ĐỌC TRỰC TIẾP nữa trong vòng lặp watchdog.

  [Giữ nguyên]
      BUG-10: was_dangerous flag để chặn spam MQTT
"""

import time
import json
import sqlite3
import threading
from datetime import datetime
from bridge.message_bus import MessageBus, CH_INBOUND

# ── Config ────────────────────────────────────────────────
SAFETY_MUTE_TIMEOUT     = 600   # 10 phút
SAFETY_REPEAT_INTERVAL  = 300   # 5 phút → lưu alert lại
WATCHDOG_TICK           = 1.0
GAS_DEFAULT_THRESHOLD   = 600
DB_PATH                 = "/data/smarthome.db"

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
    bus = MessageBus.get_instance()
    conn = get_db()
    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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

    bus.publish_event("realtime_data", {
        "event":   "new_alert", "type": alert_type,
        "room":    room_id, "message": message,
        "level":   "critical", "timestamp": now
    })
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
    # automation_engine phải được import SAU KHI đã load
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
            # Fallback: dùng local dict (ít an toàn hơn nhưng không crash)
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
                    state = SAFETY_STATE.setdefault(room_id, {})
                    state["muted"]     = True
                    state["mute_time"] = datetime.now()
                    _mute_safety_action(bus, room_id)
                    print(f"[WATCHDOG] {room_id} muted by user")
            except Exception as e:
                print(f"[WATCHDOG] mute listener error: {e}")

    threading.Thread(target=listen_mute, daemon=True).start()
    print("[WATCHDOG] Safety watchdog started (thread-safe sensor read)")

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
                    "muted":        False,
                    "mute_time":    None,
                    "last_alert":   None,
                    "was_dangerous": False,
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
                        state["was_dangerous"] = True
                        print(f"[WATCHDOG] DANGER DETECTED {room_id}: {alert_msg}")
                    else:
                        # Đã nguy hiểm rồi — chỉ refresh safety_lock TTL mỗi 30s
                        _set_safety_lock(room_id, True)

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
                        state["was_dangerous"] = False
                        state["last_alert"]    = None
                        state["muted"]         = False
                        bus.publish_event("realtime_data", {
                            "event":     "new_alert",
                            "type":      "system",
                            "room":      room_id,
                            "message":   f"Phòng {room_id} đã an toàn.",
                            "level":     "info",
                            "timestamp": now.strftime("%Y-%m-%d %H:%M:%S")
                        })
                        print(f"[WATCHDOG] {room_id}: SAFE — lock released")

            time.sleep(WATCHDOG_TICK)

        except Exception as e:
            print(f"[WATCHDOG] loop error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    run()
