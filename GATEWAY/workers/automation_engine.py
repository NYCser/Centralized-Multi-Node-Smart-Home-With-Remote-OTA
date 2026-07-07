"""
workers/automation_engine.py  — v2.7  (SCHEDULE-OFF BLOCKS AUTOMATION)
══════════════════════════════════════════════════════════════════════════════
FIXES trong phiên bản này (so với v2.6):

  [FIX-SCHED-OFF-REBLOCK — BUG CRITICAL]
      ─────────────────────────────────────────────
      Vấn đề: Schedule "turn_off" thực thi thành công, ESP32 tắt quạt,
              nhưng 5 giây sau sensor gửi temperature=34°C > fan_threshold=32°C
              → _try_turn_on() thấy CACHED_DEVICE_STATES=False → bật lại ngay.
              → Quạt không bao giờ tắt được theo schedule khi nhiệt độ còn cao.

      Root cause: MANUAL_STATE chỉ được set khi USER điều khiển thủ công.
              Schedule turn_off không set MANUAL_STATE → automation không biết
              "vừa có schedule tắt" → bật lại theo nhiệt độ.

      Fix: Khi schedule "turn_off" dispatch thành công:
              → Set MANUAL_STATE[device_id] với source="schedule_off"
              → TTL = SCHEDULE_OFF_TTL_S (mặc định 3600s = 1 giờ)
              → Automation bị block trong 1 giờ sau khi schedule tắt thiết bị
              → User vẫn có thể bật lại thủ công bất cứ lúc nào (source="manual")
              → Khi bật lại thủ công: MANUAL_STATE bị replace với source="manual",
                automation lại bị block 5 phút như bình thường

      Tại sao 1 giờ?
              - Schedule thường được đặt để tắt thiết bị vào buổi tối/đêm
              - 1 giờ đủ để ngăn automation bật lại trong khoảng thời gian user
                muốn nghỉ ngơi, ngay cả khi nhiệt độ vẫn cao
              - Sau 1 giờ, nếu nhiệt độ vẫn cao, automation tự hoạt động lại
              - Configurable qua SCHEDULE_OFF_TTL_S

  [Giữ nguyên từ v2.6]
      FIX-SCHED-MANUAL-BLOCK: Schedule không bị block bởi MANUAL_STATE
      FIX-SCHED-LASTRUN-ON-FAIL: Luôn ghi SCHEDULE_LAST_RUN dù dispatch fail
      FIX-AUTO-ONEWAY: Automation chỉ bật thiết bị (1 chiều)
      FIX-TOPIC-1/2, FIX-ACCESS-LOG, FIX-AUTO-1/2, FIX-RFID-1/2,
      FIX C1, D1, BUG-ACK-01, BUG-DEVICE-SYNC-01, BUG-H-01/02, BUG-C-05
"""

import time
import json
import sqlite3
import threading
from datetime import datetime

from bridge.message_bus import MessageBus, CH_INBOUND
from workers import safety_watchdog
from workers import event_logger

DB_PATH            = "/data/smarthome.db"
ENROLLMENT_TIMEOUT = 60
CLOCK_WARN_YEAR    = 2024

MIN_SWITCH_DELAY_S   = 60       # Tối thiểu 60s giữa 2 lần gửi lệnh bật cùng thiết bị
DEVICE_LAST_SWITCH: dict = {}   # device cache_key → datetime lần bật cuối

MANUAL_STATE_TTL_S    = 300     # 5 phút — block automation sau khi user điều khiển thủ công

# [FIX-SCHED-OFF-REBLOCK]
# TTL block automation sau khi schedule turn_off chạy thành công.
# Mặc định 1 giờ — đủ để ngăn automation bật lại khi nhiệt độ vẫn cao
# nhưng user đã muốn tắt thiết bị theo lịch.
# Sau TTL này, nếu nhiệt độ vẫn > threshold, automation sẽ tự bật lại.
SCHEDULE_OFF_TTL_S    = 3600    # 1 giờ

MQTT_TOPIC_ENTRANCE  = "home/entrance_01/command"
MQTT_TOPIC_LIVING    = "home/living_room_01/command"

DEVICE_MAP = {
    "kitchen_01":     {"fan": "fan_kt_1",  "light": "light_kt_1"},
    "living_room_01": {"fan": "fan_lv_1",  "light": "light_lv_1"},
    "bedroom_01":     {"fan": "fan_bd_1",  "light": "light_bd_1"},
}

CACHED_AUTOMATIONS:   dict = {}
CACHED_SCHEDULES:     list = []
CACHED_DEVICE_STATES: dict = {}

# MANUAL_STATE: device_id → {mode, is_on, set_at, source, ttl_s}
# source có thể là: "web", "physical_button", "schedule_off"
# ttl_s: override TTL riêng cho từng entry (None = dùng MANUAL_STATE_TTL_S)
MANUAL_STATE:         dict = {}

ENROLLMENT_STATE:     dict = {"active": False, "start_time": None, "pending_name": ""}
SCHEDULE_LAST_RUN:    dict = {}

PENDING_COMMANDS: dict = {}

_SENSORS_LOCK = threading.RLock()
CACHED_SENSORS: dict = {}


def get_cached_sensors(room_id: str) -> dict:
    with _SENSORS_LOCK:
        return dict(CACHED_SENSORS.get(room_id, {}))


def update_cached_sensors(room_id: str, payload: dict):
    with _SENSORS_LOCK:
        room_cache = CACHED_SENSORS.setdefault(room_id, {})
        room_cache.update(payload)
        safety_watchdog.CACHED_SENSORS[room_id] = dict(room_cache)


SOURCE_PRIORITY = {
    "safety":     0,
    "manual":     1,
    "web":        1,
    "schedule":   2,
    "automation": 3,
}


def _manual_state_expired(device_id: str) -> bool:
    """Kiểm tra MANUAL_STATE có hết TTL chưa. Trả về True nếu hết hạn hoặc không tồn tại."""
    manual = MANUAL_STATE.get(device_id)
    if not manual:
        return True
    set_at = manual.get("set_at")
    if set_at is None:
        return True
    ttl = manual.get("ttl_s", MANUAL_STATE_TTL_S)
    return (datetime.now() - set_at).total_seconds() > ttl


def dispatch_command(bus: MessageBus, source: str, room_id: str,
                     device_id: str, action: str, cmd_id: str = "",
                     extra: dict = None) -> bool:
    priority = SOURCE_PRIORITY.get(source, 99)

    # ── Safety lock: block automation/schedule only; user manual/web commands can still run ───
    if _is_safety_locked(room_id) and source not in ("safety", "manual", "web"):
        event_logger.log_system_message(
            room_id=room_id,
            message=f"Lệnh bị chặn | Hệ thống an toàn bị khóa | Thiết bị: {device_id}",
            level="warning"
        )
        print(f"[DISPATCH] BLOCKED (safety_lock): {source} → {room_id}/{device_id} {action}")
        bus.publish_event("realtime_data", {
            "event":   "command_blocked",
            "room":    room_id,
            "reason":  "safety_lock",
            "message": "Hệ thống đang trong trạng thái khẩn cấp — lệnh bị từ chối!"
        })
        return False
    elif _is_safety_locked(room_id) and source in ("manual", "web"):
        print(f"[DISPATCH] Safety lock bypassed for manual/web: {source} → {room_id}/{device_id} {action}")

    # ── MANUAL_STATE check ───────────────────────────────────────────────────
    #
    # Bảng ưu tiên:
    #   source="manual"/"web"  → set MANUAL_STATE, không bị block
    #   source="schedule"      → KHÔNG bị block, xóa MANUAL_STATE nếu còn
    #   source="automation"    → bị block nếu MANUAL_STATE còn TTL
    #
    # MANUAL_STATE.source có thể là:
    #   "web" / "physical_button" → TTL = MANUAL_STATE_TTL_S (5 phút)
    #   "schedule_off"            → TTL = SCHEDULE_OFF_TTL_S (1 giờ)
    #     → Block automation bật lại sau khi schedule tắt thiết bị
    # ─────────────────────────────────────────────────────────────────────────
    if source in ("schedule", "automation"):
        manual = MANUAL_STATE.get(device_id)
        if manual and manual.get("mode") == "manual":
            if _manual_state_expired(device_id):
                # TTL hết hạn → xóa MANUAL_STATE
                MANUAL_STATE.pop(device_id, None)
                ms = manual.get("source", "?")
                event_logger.log_system_message(
                    room_id=room_id,
                    message=f"✓ Manual state hết hạn ({ms}) | {device_id} → Tự động hoạt động lại",
                    level="info"
                )
                print(f"[DISPATCH] MANUAL_STATE expired (source={ms}) for {device_id}")

            elif source == "automation":
                # Automation bị block — có thể do user thủ công hoặc schedule_off
                ms = manual.get("source", "manual")
                reason = (
                    "Schedule vừa tắt thiết bị (block automation 1 giờ)"
                    if ms == "schedule_off"
                    else "Vừa điều khiển thủ công (tạm dừng 5 phút)"
                )
                event_logger.log_automation_blocked(
                    room_id=room_id,
                    device_id=device_id,
                    device_name=device_id,
                    reason=reason
                )
                print(f"[DISPATCH] BLOCKED (manual_state source={ms}): automation → {device_id}")
                return False

            else:
                # source == "schedule": xóa MANUAL_STATE, thực thi lệnh
                ms = manual.get("source", "?")
                MANUAL_STATE.pop(device_id, None)
                print(
                    f"[DISPATCH] MANUAL_STATE cleared by schedule for {device_id} "
                    f"(was: source={ms})"
                )

    # ── Schedule conflict: block automation nếu schedule vừa chạy ≤60 phút ──
    if source == "automation":
        today_key = datetime.now().strftime("%Y-%m-%d")
        for sched in CACHED_SCHEDULES:
            if not sched.get("enabled") or sched.get("device_id") != device_id:
                continue
            sched_id = sched.get("id", "")
            last_run = SCHEDULE_LAST_RUN.get(sched_id, "")
            if last_run and today_key in last_run:
                sched_time = sched.get("time", "")
                try:
                    now_mins   = datetime.now().hour * 60 + datetime.now().minute
                    h, m       = map(int, sched_time.split(":"))
                    sched_mins = h * 60 + m
                    if abs(now_mins - sched_mins) <= 60:
                        event_logger.log_automation_blocked(
                            room_id=room_id,
                            device_id=device_id,
                            device_name=device_id,
                            reason="Schedule đã chạy trong 60 phút qua"
                        )
                        print(
                            f"[DISPATCH] BLOCKED (schedule_active_60m): "
                            f"automation → {device_id}"
                        )
                        return False
                except Exception:
                    pass

    # ── Gửi lệnh qua MQTT ───────────────────────────────────────────────────
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
        for row in conn.execute("SELECT room, device_id, is_on FROM device_status").fetchall():
            cache_key = f"{row['room']}_{row['device_id']}"
            CACHED_DEVICE_STATES[cache_key] = bool(row["is_on"])
    finally:
        conn.close()
    print(f"[AUTO] Cache loaded: {len(CACHED_AUTOMATIONS)} rules, "
          f"{len(CACHED_SCHEDULES)} schedules, "
          f"{len(CACHED_DEVICE_STATES)} device states")


def _is_safety_locked(room_id: str) -> bool:
    bus = MessageBus.get_instance()
    return bool(bus.get_redis().exists(f"safety_lock:{room_id}"))


def _try_turn_on(bus: MessageBus, room_id: str, device_type: str,
                 value: float, threshold: float):
    """
    [FIX-AUTO-ONEWAY] Automation CHỈ bật thiết bị (1 chiều).

    value > threshold  → gửi turn_on NẾU thiết bị chưa bật VÀ không bị block
    value <= threshold → không làm gì

    Block cases:
      - MANUAL_STATE source="web"/"physical_button": 5 phút sau khi user tắt thủ công
      - MANUAL_STATE source="schedule_off": 1 giờ sau khi schedule tắt thiết bị
        (fix chính của v2.7 — ngăn automation bật lại ngay sau schedule turn_off)
    """
    device_id = DEVICE_MAP.get(room_id, {}).get(device_type)
    if not device_id:
        return

    if value <= threshold:
        return

    cache_key  = f"{room_id}_{device_id}"
    current_on = CACHED_DEVICE_STATES.get(cache_key)

    if current_on is True:
        return

    now         = datetime.now()
    last_switch = DEVICE_LAST_SWITCH.get(cache_key)
    if last_switch and (now - last_switch).total_seconds() < MIN_SWITCH_DELAY_S:
        return

    sent = dispatch_command(bus, "automation", room_id, device_id, "turn_on")
    if sent:
        CACHED_DEVICE_STATES[cache_key] = True
        DEVICE_LAST_SWITCH[cache_key]   = now
        _log_automation(
            room_id, f"auto_{device_type}",
            [f"{device_id} → turn_on (value={value:.1f} > threshold={threshold})"],
            f"sensor_{device_type}"
        )


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
    rule = CACHED_AUTOMATIONS.get(room_id)
    if not rule or not rule.get("enabled"):
        return

    bus = MessageBus.get_instance()

    if "temperature" in sensor_data:
        val = float(sensor_data["temperature"])
        if rule.get("fan_threshold"):
            _try_turn_on(bus, room_id, "fan", val, float(rule["fan_threshold"]))
        if rule.get("light_threshold"):
            _try_turn_on(bus, room_id, "light", val, float(rule["light_threshold"]))

    if "humidity" in sensor_data and rule.get("humidity_threshold"):
        val = float(sensor_data["humidity"])
        _try_turn_on(bus, room_id, "fan", val, float(rule["humidity_threshold"]))

    if "co2" in sensor_data:
        val = float(sensor_data["co2"])
        co2_thresh = float(rule.get("co2_threshold") or 0)
        if co2_thresh > 0:
            _try_turn_on(bus, room_id, "fan", val, co2_thresh)


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
    Schedule hẹn giờ bật/tắt thiết bị.

    [FIX-SCHED-OFF-REBLOCK] v2.7:
    Khi schedule "turn_off" thực thi thành công:
      → Set MANUAL_STATE[device_id] với source="schedule_off", ttl=SCHEDULE_OFF_TTL_S (1h)
      → Automation bị block 1 giờ, không thể bật lại thiết bị dù nhiệt độ vẫn cao
      → User vẫn có thể bật lại thủ công bất cứ lúc nào

    Luồng hoàn chỉnh:
      22:00 → schedule turn_off quạt bếp
      22:00 → MANUAL_STATE[fan_kt_1] = {source="schedule_off", ttl=3600}
      22:00 → sensor temp=34°C > 32°C → _try_turn_on() → dispatch BLOCKED
      22:05 → user bật thủ công → MANUAL_STATE replace {source="web", ttl=300}
      22:10 → MANUAL_STATE hết TTL → automation có thể bật lại
      23:00 → MANUAL_STATE schedule_off hết TTL (nếu user không can thiệp)
              → automation tự bật lại nếu temp vẫn > threshold
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

                sched_id = sched.get("id", "")
                run_key  = f"{sched_id}_{current_hhmm}_{today_key}"

                if SCHEDULE_LAST_RUN.get(sched_id) == run_key:
                    continue

                room_id   = sched["room_id"]
                device_id = sched["device_id"]
                action    = sched.get("action", "turn_off")

                sent = dispatch_command(bus, "schedule", room_id, device_id, action)

                # Luôn mark last_run dù thành công hay không → tránh retry loop
                SCHEDULE_LAST_RUN[sched_id] = run_key

                if sent:
                    cache_key = f"{room_id}_{device_id}"

                    if action == "turn_off":
                        CACHED_DEVICE_STATES[cache_key] = False

                        # [FIX-SCHED-OFF-REBLOCK] Block automation 1 giờ sau schedule turn_off.
                        # Lý do: automation dựa vào sensor (nhiệt độ/CO2) không phải ý định user.
                        # Khi user đặt lịch tắt, có nghĩa họ KHÔNG muốn thiết bị bật lại tự động
                        # dù điều kiện sensor vẫn vượt ngưỡng.
                        MANUAL_STATE[device_id] = {
                            "mode":   "manual",
                            "is_on":  False,
                            "set_at": datetime.now(),
                            "source": "schedule_off",
                            "ttl_s":  SCHEDULE_OFF_TTL_S,
                        }
                        print(
                            f"[SCHEDULER] schedule_off block set: {device_id} "
                            f"→ automation blocked {SCHEDULE_OFF_TTL_S}s"
                        )

                    elif action == "turn_on":
                        CACHED_DEVICE_STATES[cache_key] = True
                        # Schedule turn_on: xóa block (nếu có) để automation hoạt động bình thường
                        MANUAL_STATE.pop(device_id, None)

                    event_logger.log_schedule_executed(
                        room_id=room_id,
                        device_id=device_id,
                        device_name=device_id
                    )
                    print(f"[SCHEDULER] Executed: {room_id}/{device_id} → {action}")
                    _log_automation(
                        room_id, "schedule",
                        [f"{device_id} → {action}"],
                        "schedule"
                    )
                else:
                    print(
                        f"[SCHEDULER] DISPATCH FAILED (blocked): "
                        f"{room_id}/{device_id} → {action} "
                        f"(safety lock active?)"
                    )

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
        bus = MessageBus.get_instance()
        r   = bus.get_redis()

        if payload.get("source") == "ota":
            r.publish("ota_status", json.dumps({
                "event":   payload.get("event"),
                "version": payload.get("version", ""),
                "doc_id":  payload.get("doc_id", ""),
                "room_id": room_id,
                "error":   payload.get("error", "")
            }))
            print(f"[AUTO] OTA status routed: {room_id} {payload.get('event')}")
            return

        device_id = payload.get("device") or payload.get("deviceId") or payload.get("device_id")
        is_on     = bool(payload.get("is_on", payload.get("isOn", False)))

        if device_id:
            conn = get_db()
            try:
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                conn.execute(
                    "INSERT OR REPLACE INTO device_status "
                    "(room, device_id, is_on, source, updated_at) VALUES (?,?,?,?,?)",
                    (room_id, device_id, 1 if is_on else 0,
                     payload.get("source", "esp32"), now)
                )
                conn.commit()
            except Exception as e:
                print(f"[AUTO] device_status DB error: {e}")
            finally:
                conn.close()

            bus.publish_event("device_status", {
                "room_id":   room_id,
                "device_id": device_id,
                "is_on":     is_on,
                "status":    "online",
                "name":      payload.get("name", device_id),
                "type":      payload.get("type", "")
            })

            pending_cmd_id = PENDING_COMMANDS.pop(device_id, None)
            if pending_cmd_id:
                r.publish("command_ack", json.dumps({
                    "cmd_id": pending_cmd_id,
                    "status": "done",
                    "result": f"ESP32 confirmed: {device_id} is {'ON' if is_on else 'OFF'}"
                }))
                print(f"[AUTO] ACK sent for cmd {pending_cmd_id}: {device_id} → {is_on}")

            cache_key = f"{room_id}_{device_id}"
            CACHED_DEVICE_STATES[cache_key] = is_on

            src = payload.get("source", "esp32")
            if src in ("button", "physical"):
                MANUAL_STATE[device_id] = {
                    "mode":   "manual",
                    "is_on":  is_on,
                    "set_at": datetime.now(),
                    "source": "physical_button",
                    "ttl_s":  MANUAL_STATE_TTL_S,
                }
                print(f"[AUTO] Physical button detected: {device_id} → MANUAL mode set")

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

    elif category == "enroll":
        _check_enrollment_timeout()
        payload["_is_enroll_result"] = True
        _handle_auth(room_id, payload)


def _handle_auth(room_id: str, payload: dict):
    uid = str(payload.get("cardUid") or payload.get("uid") or
              payload.get("fingerprintId", ""))
    bus = MessageBus.get_instance()

    is_success = payload.get("success", True)

    if ENROLLMENT_STATE["active"]:
        if not uid:
            print(f"[AUTH] Enrollment: no UID in payload")
            return

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

            bus.publish_mqtt(MQTT_TOPIC_ENTRANCE, {
                "action":  "enrollment_success",
                "uid":     uid,
                "message": f"Da luu the: {owner_name}"
            })
            bus.publish_event("realtime_data", {
                "event":      "enrollment_success",
                "uid":        uid,
                "owner_name": owner_name
            })
            bus.publish_event("rfid_enrollment_result", {
                "status":      "success",
                "result_type": "combo",
                "value":       uid,
                "owner_name":  owner_name,
                "timestamp":   datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })
            bus.publish_event("safety_alert", {
                "type":     "access",
                "message":  f"Đã đăng ký thẻ mới: {uid} — {owner_name}",
                "level":    "info",
                "location": room_id,
            })
            print(f"[AUTH] Enrolled new card: {uid} -> {owner_name}")
        except Exception as e:
            print(f"[AUTH] enrollment error: {e}")
            bus.publish_event("rfid_enrollment_result", {
                "status":  "error",
                "message": str(e),
                "value":   uid,
            })
        return

    if not uid:
        return

    if not is_success:
        _log_access(room_id, uid, "Khách lạ", "attempt_failed", False)
        print(f"[AUTH] {room_id}: DENIED (ESP32 reported) -> {uid}")
        return

    conn = get_db()
    try:
        card_row = conn.execute(
            "SELECT * FROM rfid_cards WHERE uid=? AND is_active=1", (uid,)
        ).fetchone()
    finally:
        conn.close()

    method = payload.get("method", "rfid").upper()

    if card_row:
        owner = card_row["owner_name"]
        _log_access(room_id, uid, owner, "open_door", True, method=method)
        event_logger.log_door_access(
            room_id=room_id, uid=uid, owner_name=owner,
            access_type="granted", method=method
        )
        print(f"[AUTH] {room_id}: GRANTED -> {owner} (method={method})")
    else:
        _log_access(room_id, uid, "Khách lạ", "attempt_failed", False, method=method)
        event_logger.log_door_access(
            room_id=room_id, uid=uid, owner_name="Khách lạ",
            access_type="denied", method=method
        )
        print(f"[AUTH] {room_id}: DENIED -> {uid} (method={method})")


def _log_access(room_id, uid, user_name, action, success, method: str = "RFID"):
    try:
        conn = get_db()
        now  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            conn.execute(
                "INSERT INTO access_logs (room, uid, user_name, action, success, timestamp) "
                "VALUES (?,?,?,?,?,?)",
                (room_id, uid, user_name, action, 1 if success else 0, now)
            )
            conn.commit()
        finally:
            conn.close()

        bus = MessageBus.get_instance()
        bus.publish_event("realtime_data", {
            "event":     "access_log",
            "room":      room_id,
            "uid":       uid,
            "user_name": user_name,
            "success":   success,
            "timestamp": now
        })

        if success:
            method_vn   = "Vân tay" if "FINGER" in method.upper() else "Thẻ RFID"
            alert_msg   = f"Mở cửa thành công | {user_name} ({uid}) | Phương thức: {method_vn} | {now}"
            alert_level = "info"
            alert_type  = "access"
        else:
            alert_msg   = f"🚨 Cảnh báo đột nhập! Thẻ/vân tay chưa đăng ký: {uid} tại {room_id} lúc {now}"
            alert_level = "critical"
            alert_type  = "intrusion"

        bus.publish_event("safety_alert", {
            "type":     alert_type,
            "message":  alert_msg,
            "level":    alert_level,
            "location": room_id,
        })
    except Exception as e:
        print(f"[AUTH] log error: {e}")


def command_listener():
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
                room_id   = data.get("room") or data.get("roomId") or data.get("room_id", "")
                device_id = data.get("device_id") or data.get("deviceId", "")
                is_on     = data.get("is_on", False)
                cmd_id    = data.get("cmd_id", "")
                action    = data.get("action", "")

                if not room_id or not device_id:
                    print(f"[AUTO] device_commands: missing room or device_id: {data}")
                    continue

                if action == "set_auto_mode":
                    MANUAL_STATE.pop(device_id, None)
                    print(f"[AUTO] {device_id} → Auto mode restored")
                    bus.publish_event("realtime_data", {
                        "event":     "auto_mode_restored",
                        "room":      room_id,
                        "device_id": device_id,
                        "message":   f"{device_id} đã trở về chế độ tự động"
                    })
                    continue

                # Điều khiển thủ công → override MANUAL_STATE (kể cả đang schedule_off)
                MANUAL_STATE[device_id] = {
                    "mode":   "manual",
                    "is_on":  is_on,
                    "set_at": datetime.now(),
                    "source": data.get("source", "web"),
                    "ttl_s":  MANUAL_STATE_TTL_S,
                }

                if cmd_id:
                    PENDING_COMMANDS[device_id] = cmd_id

                mqtt_action = "turn_on" if is_on else "turn_off"
                event_logger.log_manual_control(
                    room_id=room_id,
                    device_id=device_id,
                    device_name=device_id,
                    action=mqtt_action
                )
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
                    conn = get_db()
                    try:
                        CACHED_AUTOMATIONS = {}
                        for row in conn.execute(
                                "SELECT * FROM automations WHERE enabled=1").fetchall():
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
                    bus.publish_mqtt(MQTT_TOPIC_ENTRANCE, {
                        "action":     "enroll",
                        "owner_name": data.get("owner_name", "Thẻ mới"),
                        "timeout":    ENROLLMENT_TIMEOUT,
                    })
                    print(f"[AUTO] Enrollment MQTT sent to {MQTT_TOPIC_ENTRANCE}")

                elif action == "delete" and uid:
                    bus.publish_mqtt(MQTT_TOPIC_ENTRANCE, {
                        "action": "delete_user",
                        "uid":    uid,
                    })
                    print(f"[AUTO] delete_user MQTT sent to {MQTT_TOPIC_ENTRANCE}: {uid}")
                    try:
                        conn = get_db()
                        try:
                            conn.execute("DELETE FROM rfid_cards WHERE uid=?", (uid,))
                            conn.commit()
                        finally:
                            conn.close()
                        print(f"[AUTO] rfid_cards deleted from SQLite: {uid}")
                    except Exception as e:
                        print(f"[AUTO] rfid_cards delete error: {e}")

                elif action == "clear_all":
                    bus.publish_mqtt(MQTT_TOPIC_ENTRANCE, {"action": "clear_all_users"})
                    print(f"[AUTO] clear_all_users MQTT sent to {MQTT_TOPIC_ENTRANCE}")
                    try:
                        conn = get_db()
                        try:
                            deleted = conn.execute("DELETE FROM rfid_cards").rowcount
                            conn.commit()
                        finally:
                            conn.close()
                        print(f"[AUTO] Cleared {deleted} users from SQLite rfid_cards")
                        bus.publish_event("realtime_data", {
                            "event":   "all_users_cleared",
                            "message": f"Đã xóa {deleted} thẻ khỏi hệ thống",
                        })
                    except Exception as e:
                        print(f"[AUTO] clear_all_users SQLite error: {e}")

            elif channel == "rfid_register":
                action = data.get("action")
                if action == "start_register":
                    ENROLLMENT_STATE["active"]       = True
                    ENROLLMENT_STATE["start_time"]   = datetime.now()
                    ENROLLMENT_STATE["pending_name"] = data.get("owner_name", "Thẻ mới")
                    print("[AUTO] Enrollment started via Firebase command")
                    bus.publish_mqtt(MQTT_TOPIC_ENTRANCE, {
                        "action":     "enroll",
                        "owner_name": data.get("owner_name", "Thẻ mới"),
                        "timeout":    ENROLLMENT_TIMEOUT,
                    })
                    print(f"[AUTO] Enrollment MQTT sent to {MQTT_TOPIC_ENTRANCE}")

                elif action == "cancel_register":
                    ENROLLMENT_STATE["active"]     = False
                    ENROLLMENT_STATE["start_time"] = None
                    print("[AUTO] Enrollment cancelled via Firebase command")
                    bus.publish_mqtt(MQTT_TOPIC_ENTRANCE, {"action": "cancel_enroll"})

            elif channel == "schedule_commands":
                if data.get("action") == "reload":
                    conn = get_db()
                    try:
                        CACHED_SCHEDULES = [dict(r) for r in
                                            conn.execute(
                                                "SELECT * FROM schedules WHERE enabled=1"
                                            ).fetchall()]
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