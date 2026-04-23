"""
workers/firebase_sync.py  — NEW WORKER
═══════════════════════════════════════
Worker thứ 5 — đồng bộ dữ liệu lên Firebase.
Không dùng Cloudflare (đã bỏ).

Nguyên tắc:
  1. Chỉ worker này được dùng firebase_admin — không module nào khác
  2. Firebase là mirror — SQLite/Redis là source of truth
  3. Lệnh điều khiển: Web → Firestore /commands → worker → Redis → MQTT → ESP32
  4. Khi mất internet: buffer vào Redis List → flush khi reconnect
  5. Throttle RTDB writes: không push quá 1 lần/10s per room (tiết kiệm free tier)

Chỉ start nếu file firebase-service-account.json tồn tại.
"""

import json
import os
import time
import threading
import logging
from datetime import datetime

import redis as redis_lib

logger = logging.getLogger("firebase_sync")
logging.basicConfig(level=logging.INFO, format="[FBSYNC] %(message)s")

# ── Config ────────────────────────────────────────────────
REDIS_HOST      = os.getenv("REDIS_HOST",    "localhost")
FIREBASE_CRED   = os.getenv("FIREBASE_CRED", "/home/pi/GATEWAY/firebase-service-account.json")
FIREBASE_DB_URL = os.getenv("FIREBASE_DB_URL", "")   # Realtime DB URL từ Firebase Console

OFFLINE_QUEUE_KEY = "fb_offline_queue"
OFFLINE_QUEUE_MAX = 300     # max events khi offline (bỏ event cũ nhất nếu tràn)
RTDB_THROTTLE_S   = 10      # min giây giữa 2 lần push sensor lên RTDB
HEARTBEAT_S       = 30      # ghi pi_last_seen mỗi 30s

# ── Redis connection ───────────────────────────────────────
_pool    = redis_lib.ConnectionPool(host=REDIS_HOST, port=6379, decode_responses=True)
_r_cache = None

def get_r():
    global _r_cache
    if _r_cache is None:
        _r_cache = redis_lib.Redis(connection_pool=_pool)
    return _r_cache


# ── Firebase init (lazy, singleton) ───────────────────────
_fb_initialized = False
_fs_client      = None
_rt_db          = None

def _ensure_firebase():
    global _fb_initialized, _fs_client, _rt_db
    if _fb_initialized:
        return True
    try:
        import firebase_admin
        from firebase_admin import credentials, firestore, db as rtdb
        if not firebase_admin._apps:
            cred = credentials.Certificate(FIREBASE_CRED)
            options = {}
            if FIREBASE_DB_URL:
                options["databaseURL"] = FIREBASE_DB_URL
            firebase_admin.initialize_app(cred, options)
        _fs_client      = firestore.client()
        _rt_db          = rtdb
        _fb_initialized = True
        logger.info("Firebase initialized OK")
        return True
    except Exception as e:
        logger.error(f"Firebase init failed: {e}")
        return False


def _get_fs():
    if _ensure_firebase():
        return _fs_client
    return None


def _get_rtdb():
    if _ensure_firebase():
        return _rt_db
    return None


# ── Throttle để tiết kiệm RTDB writes ────────────────────
class SensorThrottle:
    """Chỉ push lên RTDB khi data thực sự thay đổi đáng kể."""
    MIN_INTERVAL  = RTDB_THROTTLE_S
    MIN_TEMP_DIFF = 0.5
    MIN_HUM_DIFF  = 2.0
    MIN_GAS_DIFF  = 20

    def __init__(self):
        self._last_push = {}   # room → timestamp
        self._last_vals = {}   # room → {type: value}

    def should_push(self, room: str, data: dict) -> bool:
        now    = time.time()
        last_t = self._last_push.get(room, 0)

        # Heartbeat: bắt buộc push mỗi 30s
        if now - last_t > 30:
            self._record(room, data, now)
            return True

        if now - last_t < self.MIN_INTERVAL:
            return False

        # Kiểm tra thay đổi đáng kể
        last_v = self._last_vals.get(room, {})
        for key, threshold in [
            ("temperature", self.MIN_TEMP_DIFF),
            ("humidity",    self.MIN_HUM_DIFF),
            ("gas",         self.MIN_GAS_DIFF),
        ]:
            if key in data and key in last_v:
                if abs(float(data[key]) - float(last_v[key])) >= threshold:
                    self._record(room, data, now)
                    return True

        # Boolean sensors: push ngay khi thay đổi
        for key in ("fire_detected",):
            if key in data and data.get(key) != last_v.get(key):
                self._record(room, data, now)
                return True

        return False

    def _record(self, room, data, ts):
        self._last_push[room] = ts
        self._last_vals[room] = dict(data)


_throttle = SensorThrottle()


# ── Offline queue ─────────────────────────────────────────

def _enqueue_offline(event: dict):
    r = get_r()
    try:
        if r.llen(OFFLINE_QUEUE_KEY) >= OFFLINE_QUEUE_MAX:
            r.lpop(OFFLINE_QUEUE_KEY)   # Drop oldest
        r.rpush(OFFLINE_QUEUE_KEY, json.dumps(event))
    except Exception as e:
        logger.warning(f"enqueue_offline failed: {e}")


def _flush_offline_queue():
    """Replay queued events sau khi reconnect."""
    r = get_r()
    count = r.llen(OFFLINE_QUEUE_KEY)
    if count == 0:
        return
    logger.info(f"Flushing {count} offline Firebase events...")
    flushed = 0
    while True:
        raw = r.lpop(OFFLINE_QUEUE_KEY)
        if not raw:
            break
        try:
            event = json.loads(raw)
            _dispatch_firebase_event(event)
            flushed += 1
        except Exception as e:
            logger.error(f"Flush error: {e}")
            r.rpush(OFFLINE_QUEUE_KEY, raw)   # Re-queue
            break
    logger.info(f"Flushed {flushed} events. Remaining: {r.llen(OFFLINE_QUEUE_KEY)}")


def _dispatch_firebase_event(event: dict):
    """Route event đến đúng Firebase collection."""
    fs = _get_fs()
    if not fs:
        raise RuntimeError("Firebase not available")

    etype = event.get("_fb_type")

    if etype == "sensor_snapshot":
        room = event.get("room")
        data = event.get("data", {})
        # Firestore: snapshot per-field (merge để không overwrite field khác)
        fs.collection("rooms").document(room).set(
            {"sensors": data, "meta": {"updated_at": _fs_ts()}},
            merge=True
        )
        # RTDB: live feed
        rtdb = _get_rtdb()
        if rtdb and FIREBASE_DB_URL:
            rtdb.reference(f"live/{room}/sensors").update({
                **data, "ts": int(time.time())
            })

    elif etype == "device_status":
        room      = event.get("room")
        device_id = event.get("device_id")
        is_on     = event.get("is_on")
        fs.collection("rooms").document(room).set(
            {"devices": {device_id: {"is_on": is_on, "updated_at": _fs_ts()}}},
            merge=True
        )

    elif etype == "alert":
        fs.collection("alerts").add({
            "room":        event.get("room"),
            "type":        event.get("type"),
            "message":     event.get("message"),
            "level":       event.get("level", "critical"),
            "is_resolved": False,
            "resolved_at": None,
            "sqlite_id":   event.get("sqlite_id", 0),
            "created_at":  _fs_ts(),
        })

    elif etype == "heartbeat":
        room = event.get("room", "pi")
        for r_id in (event.get("rooms") or ["pi"]):
            fs.collection("rooms").document(r_id).set(
                {"meta": {"pi_last_seen": _fs_ts(), "online": True}},
                merge=True
            )


def _fs_ts():
    """Server timestamp placeholder — firebase_admin dùng firestore.SERVER_TIMESTAMP."""
    try:
        from firebase_admin import firestore
        return firestore.SERVER_TIMESTAMP
    except Exception:
        return datetime.now().isoformat()


# ── Push helpers (safe — buffer offline) ─────────────────

def _safe_push(event: dict):
    """Try push → nếu lỗi network thì enqueue offline."""
    try:
        _dispatch_firebase_event(event)
    except Exception as e:
        logger.warning(f"Firebase push failed ({event.get('_fb_type')}): {e}")
        _enqueue_offline(event)


# ── Listen Firestore /commands → Redis device_commands ───

def _listen_firestore_commands():
    """
    Lắng nghe collection /commands trên Firestore.
    Web tạo document → worker nhận → kiểm tra safety_lock → publish Redis.
    """
    fs = _get_fs()
    if not fs:
        logger.warning("Cannot start Firestore command listener — Firebase unavailable")
        return

    r = get_r()

    def on_snapshot(col_snapshot, changes, read_time):
        for change in changes:
            if change.type.name != "ADDED":
                continue
            doc  = change.document
            cmd  = doc.to_dict()
            room = cmd.get("room", "")

            # Safety check — quan trọng nhất
            if r.exists(f"safety_lock:{room}"):
                logger.warning(f"Firebase command blocked — safety lock active: {room}")
                doc.reference.delete()
                continue

            # Validate fields
            if not cmd.get("device_id") or "action" not in cmd:
                doc.reference.delete()
                continue

            is_on = cmd["action"] == "turn_on"
            r.publish("device_commands", json.dumps({
                "room":      room,
                "device_id": cmd["device_id"],
                "is_on":     is_on,
                "source":    "firebase_web",
            }))
            logger.info(f"Command forwarded: {room}/{cmd['device_id']} → {cmd['action']}")
            doc.reference.delete()   # Xóa ngay sau khi xử lý

    try:
        fs.collection("commands").on_snapshot(on_snapshot)
        logger.info("Firestore command listener started")
    except Exception as e:
        logger.error(f"Firestore command listener error: {e}")


# ── Main sync loop ────────────────────────────────────────

def run():
    if not os.path.isfile(FIREBASE_CRED):
        logger.warning(f"Firebase cred not found: {FIREBASE_CRED} — FirebaseSync disabled")
        return

    if not _ensure_firebase():
        logger.error("Firebase init failed — FirebaseSync exiting")
        return

    r = get_r()

    # Flush offline queue từ lần chạy trước (nếu có)
    threading.Thread(target=_flush_offline_queue, daemon=True).start()

    # Lắng nghe commands từ Web qua Firestore
    threading.Thread(target=_listen_firestore_commands, daemon=True).start()

    # Subscribe Redis channels
    pubsub = r.pubsub()
    pubsub.subscribe("mqtt_inbound", "realtime_data", "device_state_changed")

    logger.info("Firebase sync worker running")

    last_heartbeat = 0
    last_offline_flush = 0

    for message in pubsub.listen():
        if message["type"] != "message":
            continue

        now = time.time()

        # Heartbeat mỗi 30s
        if now - last_heartbeat > HEARTBEAT_S:
            last_heartbeat = now
            _safe_push({"_fb_type": "heartbeat"})

        # Retry offline queue mỗi 60s
        if now - last_offline_flush > 60:
            last_offline_flush = now
            threading.Thread(target=_flush_offline_queue, daemon=True).start()

        try:
            channel = message["channel"]
            payload = json.loads(message["data"])

            # ── Sensor data từ ESP32 ──────────────────────
            if channel == "mqtt_inbound":
                topic = payload.get("topic", "")
                data  = payload.get("payload", {})
                parts = topic.split("/")
                if len(parts) >= 3 and parts[2] == "sensors":
                    room = parts[1]
                    # Throttle: không push mỗi giây
                    if _throttle.should_push(room, data):
                        _safe_push({
                            "_fb_type": "sensor_snapshot",
                            "room": room,
                            "data": data,
                        })

            # ── Realtime events (alerts, access, v.v.) ───
            elif channel == "realtime_data":
                event_type = payload.get("event") or payload.get("type")
                room       = payload.get("room", "")

                if event_type in ("new_alert",) and payload.get("level") == "critical":
                    # Chỉ push critical alerts lên Firebase
                    _safe_push({
                        "_fb_type": "alert",
                        "room":     room,
                        "type":     payload.get("type", "system"),
                        "message":  payload.get("message", ""),
                        "level":    payload.get("level", "info"),
                        "sqlite_id": 0,   # data_syncer sẽ có ID thật sau flush
                    })

            # ── Device status thay đổi ───────────────────
            elif channel == "device_state_changed":
                _safe_push({
                    "_fb_type":  "device_status",
                    "room":      payload.get("room"),
                    "device_id": payload.get("device_id"),
                    "is_on":     payload.get("is_on", False),
                })

        except Exception as e:
            logger.error(f"Sync loop error: {e}")


if __name__ == "__main__":
    run()
