"""
workers/firebase_sync.py  — FIXED
====================================
FIXES:
  BUG-C-03: Thread leak trong on_snapshot() listener
            Fix: CommandDispatcher dùng threading.Event để stop sạch,
                 RedisSyncThread đã có stop() — gọi đúng khi shutdown.
            Fix: main() đăng ký signal handler để cleanup khi Pi tắt/restart.
  BUG-H-02: CommandDispatcher._dispatch() map "roomId" → "room" trước khi
            publish sang device_commands để automation_engine hiểu đúng.
  BUG-H-03: Khi sync rooms lên Firebase, bổ sung trường "userId" từ Pi config
            để Web JS (roomService.getRoomsFresh) lọc đúng rooms.

INVARIANT 1: Chỉ file này được ghi lên Firebase (không có module nào khác).
"""

import asyncio
import json
import logging
import os
import signal
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import redis

# Firebase Admin SDK
import firebase_admin
from firebase_admin import credentials, firestore

logger = logging.getLogger("firebase_sync")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] firebase_sync: %(message)s",
    datefmt="%H:%M:%S",
)

# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────
FIREBASE_PROJECT_ID   = os.getenv("FIREBASE_PROJECT_ID", "nhathongminh-myhome")
SERVICE_ACCOUNT_FILE  = os.getenv("FIREBASE_SERVICE_ACCOUNT", "/home/pi/smarthome_prj/GATEWAY/firebase-service-account.json")

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))

SQLITE_PATH = os.getenv("SQLITE_PATH", "/data/smarthome.db")

# Channels Redis
CHANNEL_SENSOR      = "realtime_data"
CHANNEL_DEVICE      = "device_status"
CHANNEL_ALERT       = "safety_alert"
CHANNEL_WIFI        = "wifi_status"
CHANNEL_COMMAND_ACK = "command_ack"

SENSOR_FLUSH_INTERVAL    = 180   # 3 phút
SENSOR_SNAPSHOT_THROTTLE = 10    # giây

# BUG-H-03: Owner UID của Pi — tất cả rooms sẽ có userId này
# Đọc từ env hoặc file config, fallback về "pi_default"
PI_OWNER_UID = os.getenv("PI_OWNER_UID", "")


# ─────────────────────────────────────────────
#  FIREBASE INIT
# ─────────────────────────────────────────────
def init_firebase() -> firestore.Client:
    if not firebase_admin._apps:
        if os.path.exists(SERVICE_ACCOUNT_FILE):
            cred = credentials.Certificate(SERVICE_ACCOUNT_FILE)
            firebase_admin.initialize_app(cred, {"projectId": FIREBASE_PROJECT_ID})
            logger.info("✅ Firebase khởi tạo từ service account file")
        else:
            firebase_admin.initialize_app(options={"projectId": FIREBASE_PROJECT_ID})
            logger.warning("⚠️  Service account không tìm thấy, dùng ADC")
    return firestore.client()


# ─────────────────────────────────────────────
#  REDIS HELPER
# ─────────────────────────────────────────────
def get_redis() -> redis.Redis:
    return redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)


# ─────────────────────────────────────────────
#  SQLITE HELPER
# ─────────────────────────────────────────────
def get_unsynced_readings(limit: int = 500) -> list:
    try:
        conn = sqlite3.connect(SQLITE_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        # Fallback: nếu không có cột firebase_synced thì lấy 500 bản ghi mới nhất
        try:
            cur.execute(
                """SELECT id, room as room_id, type as sensor_type, value, timestamp
                   FROM sensor_data
                   WHERE firebase_synced = 0
                   ORDER BY timestamp ASC LIMIT ?""",
                (limit,),
            )
        except Exception:
            # Cột firebase_synced chưa tồn tại trong schema cũ
            cur.execute(
                """SELECT id, room as room_id, type as sensor_type, value, timestamp
                   FROM sensor_data
                   ORDER BY timestamp DESC LIMIT ?""",
                (limit,),
            )
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows
    except Exception as e:
        logger.error("SQLite read error: %s", e)
        return []


def mark_synced(row_ids: list):
    if not row_ids:
        return
    try:
        conn = sqlite3.connect(SQLITE_PATH, timeout=10)
        try:
            conn.execute(
                f"ALTER TABLE sensor_data ADD COLUMN firebase_synced INTEGER DEFAULT 0"
            )
            conn.commit()
        except Exception:
            pass  # cột đã tồn tại
        conn.execute(
            f"UPDATE sensor_data SET firebase_synced=1 WHERE id IN ({','.join('?'*len(row_ids))})",
            row_ids,
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error("SQLite mark_synced error: %s", e)


# ─────────────────────────────────────────────
#  FIRESTORE WRITER
# ─────────────────────────────────────────────
class FirestoreWriter:
    def __init__(self, db: firestore.Client):
        self.db = db
        self._last_sensor_push: Dict[str, float] = {}

    def update_sensor(self, room_id: str, sensor_type: str, value: float, timestamp: datetime):
        key = f"{room_id}_{sensor_type}"
        now = time.time()
        if now - self._last_sensor_push.get(key, 0) < SENSOR_SNAPSHOT_THROTTLE:
            return
        try:
            ref = self.db.collection("rooms").document(room_id).collection("sensors").document(sensor_type)
            ref.set(
                {
                    "type": sensor_type,
                    "value": value,
                    "lastUpdate": firestore.SERVER_TIMESTAMP,
                    "localTimestamp": timestamp.isoformat(),
                },
                merge=True,
            )
            self._last_sensor_push[key] = now
        except Exception as e:
            logger.error("update_sensor error: %s", e)

    def push_sensor_reading(self, room_id: str, sensor_type: str, value: float, ts_iso: str):
        try:
            ts = datetime.fromisoformat(ts_iso).replace(tzinfo=timezone.utc)
        except ValueError:
            ts = datetime.now(timezone.utc)
        self.db.collection("sensor_readings").add(
            {"roomId": room_id, "type": sensor_type, "value": value, "timestamp": ts}
        )

    def batch_push_sensor_readings(self, rows: list) -> list:
        if not rows:
            return []
        synced_ids = []
        chunk_size = 499
        for i in range(0, len(rows), chunk_size):
            chunk = rows[i: i + chunk_size]
            batch = self.db.batch()
            for row in chunk:
                try:
                    ts = datetime.fromisoformat(str(row["timestamp"])).replace(tzinfo=timezone.utc)
                except Exception:
                    ts = datetime.now(timezone.utc)
                new_ref = self.db.collection("sensor_readings").document()
                batch.set(new_ref, {
                    "roomId": row["room_id"],
                    "type":   row["sensor_type"],
                    "value":  float(row["value"]),
                    "timestamp": ts,
                })
                synced_ids.append(row["id"])
            try:
                batch.commit()
                logger.info("📦 Batch pushed %d sensor readings", len(chunk))
            except Exception as e:
                logger.error("Batch commit failed: %s", e)
        return synced_ids

    def update_device(self, room_id: str, device_id: str, payload: dict):
        try:
            ref = (
                self.db.collection("rooms")
                .document(room_id)
                .collection("devices")
                .document(device_id)
            )
            data = {
                "isOn":      payload.get("is_on", False),
                "status":    payload.get("status", "online"),
                "details":   "Đang bật" if payload.get("is_on") else "Đã tắt",
                "updatedAt": firestore.SERVER_TIMESTAMP,
            }
            if "name" in payload:
                data["name"] = payload["name"]
            if "type" in payload:
                data["type"] = payload["type"]
            ref.set(data, merge=True)
        except Exception as e:
            logger.error("update_device error: %s", e)

    def push_alert(self, alert_type: str, message: str, level: str = "warning", location: str = ""):
        try:
            self.db.collection("system_alerts").add({
                "type":       alert_type,
                "message":    message,
                "level":      level,
                "location":   location,
                "isResolved": False,
                "timestamp":  firestore.SERVER_TIMESTAMP,
            })
        except Exception as e:
            logger.error("push_alert error: %s", e)

    def delete_command(self, cmd_id: str):
        try:
            self.db.collection("commands").document(cmd_id).delete()
        except Exception as e:
            logger.error("Delete command error: %s", e)

    def ack_command(self, cmd_id: str, status: str, result: Optional[Any] = None):
        data: Dict[str, Any] = {"status": status, "ackedAt": firestore.SERVER_TIMESTAMP}
        if result is not None:
            data["result"] = result
        try:
            self.db.collection("commands").document(cmd_id).update(data)
        except Exception as e:
            logger.error("ACK command error: %s", e)

    def update_wifi_status(self, status: str, ssid: str = "", ip: str = ""):
        try:
            self.db.collection("system_status").document("wifi").set(
                {
                    "status":       status,
                    "current_ssid": ssid,
                    "ip":           ip,
                    "updatedAt":    firestore.SERVER_TIMESTAMP,
                },
                merge=True,
            )
        except Exception as e:
            logger.error("update_wifi_status error: %s", e)

    def update_available_wifi(self, networks: list):
        try:
            self.db.collection("system_status").document("available_wifi").set(
                {"networks": networks, "last_scan": firestore.SERVER_TIMESTAMP},
                merge=True,
            )
        except Exception as e:
            logger.error("update_available_wifi error: %s", e)

    def listen_commands(self, callback):
        """
        FIX BUG-C-03: Trả về watcher object để caller có thể gọi .unsubscribe()
        khi shutdown — tránh thread leak.
        """
        def _on_snapshot(col_snapshot, changes, read_time):
            for change in changes:
                if change.type.name == "ADDED":
                    data   = change.document.to_dict()
                    cmd_id = change.document.id
                    if data.get("status") == "pending":
                        try:
                            callback(cmd_id, data)
                        except Exception as e:
                            logger.error("Command callback error: %s", e)

        col_ref = self.db.collection("commands")
        watcher = col_ref.on_snapshot(_on_snapshot)
        return watcher

    def sync_rooms_from_sqlite(self, owner_uid: str):
        """
        FIX BUG-H-03: Đồng bộ rooms từ SQLite lên Firestore với trường userId
        để Web JS lọc đúng rooms của user này.
        """
        if not owner_uid:
            logger.warning("PI_OWNER_UID chưa được cấu hình — rooms sẽ không hiển thị trên Web")
            return
        try:
            conn = sqlite3.connect(SQLITE_PATH, timeout=10)
            conn.row_factory = sqlite3.Row
            rooms = conn.execute("SELECT * FROM rooms").fetchall()
            conn.close()
            for room in rooms:
                room_dict = dict(room)
                self.db.collection("rooms").document(room_dict["id"]).set(
                    {
                        "name":      room_dict["name"],
                        "icon":      room_dict.get("icon", "home"),
                        "roomType":  room_dict["id"].rsplit("_", 1)[0].upper(),  # bedroom_01 → BEDROOM
                        "userId":    owner_uid,  # FIX BUG-H-03
                        "updatedAt": firestore.SERVER_TIMESTAMP,
                    },
                    merge=True,
                )
            logger.info("✅ Synced %d rooms to Firebase (userId=%s)", len(rooms), owner_uid)
        except Exception as e:
            logger.error("sync_rooms error: %s", e)


# ─────────────────────────────────────────────
#  REDIS SYNC THREAD
# ─────────────────────────────────────────────
class RedisSyncThread(threading.Thread):
    """
    Subscribe Redis pubsub và forward lên Firestore.
    FIX BUG-C-03: stop() gọi unsubscribe để giải phóng thread đúng cách.
    """
    def __init__(self, writer: FirestoreWriter, redis_client: redis.Redis):
        super().__init__(daemon=True, name="redis-sync")
        self.writer      = writer
        self.r           = redis_client
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def run(self):
        pubsub = self.r.pubsub()
        pubsub.subscribe(CHANNEL_SENSOR, CHANNEL_DEVICE, CHANNEL_ALERT, CHANNEL_WIFI, CHANNEL_COMMAND_ACK)
        logger.info("🔌 Redis subscriber started")

        while not self._stop_event.is_set():
            try:
                message = pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if message and message["type"] == "message":
                    self._handle_message(message["channel"], message["data"])
            except Exception as e:
                logger.error("Redis subscriber error: %s", e)
                time.sleep(2)

        # FIX BUG-C-03: unsubscribe sạch khi stop
        try:
            pubsub.unsubscribe()
            pubsub.close()
        except Exception:
            pass
        logger.info("Redis subscriber stopped cleanly")

    def _handle_message(self, channel: str, raw_data: str):
        try:
            payload = json.loads(raw_data)
        except json.JSONDecodeError:
            return
        try:
            if channel == CHANNEL_SENSOR:
                self._on_sensor(payload)
            elif channel == CHANNEL_DEVICE:
                self._on_device(payload)
            elif channel == CHANNEL_ALERT:
                self._on_alert(payload)
            elif channel == CHANNEL_WIFI:
                self._on_wifi(payload)
            elif channel == CHANNEL_COMMAND_ACK:
                self._on_command_ack(payload)
        except Exception as e:
            logger.error("Handle message error [%s]: %s", channel, e)

    def _on_sensor(self, p: dict):
        room_id = p.get("room_id")
        s_type  = p.get("type")
        value   = p.get("value")
        ts_str  = p.get("timestamp", datetime.now().isoformat())
        if not all([room_id, s_type, value is not None]):
            return
        ts = datetime.fromisoformat(ts_str) if ts_str else datetime.now()
        self.writer.update_sensor(room_id, s_type, float(value), ts)

    def _on_device(self, p: dict):
        room_id   = p.get("room_id")
        device_id = p.get("device_id")
        if not room_id or not device_id:
            return
        self.writer.update_device(room_id, device_id, p)

    def _on_alert(self, p: dict):
        self.writer.push_alert(
            alert_type=p.get("type", "system"),
            message=p.get("message", ""),
            level=p.get("level", "warning"),
            location=p.get("location", ""),
        )

    def _on_wifi(self, p: dict):
        self.writer.update_wifi_status(
            status=p.get("status", "disconnected"),
            ssid=p.get("ssid", ""),
            ip=p.get("ip", ""),
        )
        if "networks" in p:
            self.writer.update_available_wifi(p["networks"])

    def _on_command_ack(self, p: dict):
        cmd_id = p.get("cmd_id")
        if not cmd_id:
            return
        if p.get("status") == "done":
            self.writer.delete_command(cmd_id)
        else:
            self.writer.ack_command(cmd_id, p.get("status", "error"), p.get("result"))


# ─────────────────────────────────────────────
#  COMMAND DISPATCHER
# ─────────────────────────────────────────────
class CommandDispatcher:
    """
    Lắng nghe Firestore /commands và publish sang Redis.
    FIX BUG-C-03: watcher được lưu để gọi .unsubscribe() khi stop().
    FIX BUG-H-02: map "roomId" → "room" trước khi publish device_commands.
    """
    REDIS_COMMAND_CHANNEL = "device_commands"

    def __init__(self, writer: FirestoreWriter, redis_client: redis.Redis):
        self.writer   = writer
        self.r        = redis_client
        self._watcher = None

    def start(self):
        # FIX BUG-C-03: lưu watcher để stop() có thể unsubscribe
        self._watcher = self.writer.listen_commands(self._dispatch)
        logger.info("📋 CommandDispatcher started")

    def stop(self):
        """FIX BUG-C-03: unsubscribe Firestore listener khi shutdown."""
        if self._watcher:
            try:
                self._watcher.unsubscribe()
                logger.info("CommandDispatcher stopped cleanly")
            except Exception as e:
                logger.error("CommandDispatcher stop error: %s", e)

    def _dispatch(self, cmd_id: str, data: dict):
        self.writer.ack_command(cmd_id, "processing")

        # FIX BUG-H-02: normalize field names trước khi publish
        # Web gửi roomId, automation_engine đọc room
        room_id = data.get("roomId") or data.get("room_id") or data.get("room", "")
        msg = {
            **data,
            "room":    room_id,    # automation_engine dùng "room"
            "roomId":  room_id,    # giữ cả hai để backward compat
            "cmd_id":  cmd_id,
        }

        action = data.get("action", "")

        if action in ("turn_on", "turn_off", "toggle"):
            channel = self.REDIS_COMMAND_CHANNEL
        elif action == "add_and_connect":
            channel = "wifi_setup"
        elif action in ("start_register", "cancel_register"):
            channel = "rfid_register"
        else:
            channel = self.REDIS_COMMAND_CHANNEL

        try:
            self.r.publish(channel, json.dumps(msg))
            logger.info("📤 Dispatched cmd '%s' to Redis[%s]", cmd_id, channel)
        except Exception as e:
            logger.error("Dispatch error: %s", e)
            self.writer.ack_command(cmd_id, "error", str(e))


# ─────────────────────────────────────────────
#  BATCH SENSOR FLUSH
# ─────────────────────────────────────────────
def run_sensor_flush_loop(writer: FirestoreWriter, stop_event: threading.Event):
    logger.info("🔄 Sensor flush loop started (interval: %ds)", SENSOR_FLUSH_INTERVAL)
    while not stop_event.is_set():
        time.sleep(SENSOR_FLUSH_INTERVAL)
        if stop_event.is_set():
            break
        try:
            rows = get_unsynced_readings(limit=500)
            if rows:
                synced_ids = writer.batch_push_sensor_readings(rows)
                mark_synced(synced_ids)
                logger.info("✅ Flushed %d rows to Firestore", len(synced_ids))
        except Exception as e:
            logger.error("Sensor flush error: %s", e)


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────
def main():
    logger.info("=" * 50)
    logger.info("🚀 firebase_sync.py starting — project: %s", FIREBASE_PROJECT_ID)
    logger.info("=" * 50)

    db_client = init_firebase()
    writer    = FirestoreWriter(db_client)

    r = get_redis()
    try:
        r.ping()
        logger.info("✅ Redis connected at %s:%d", REDIS_HOST, REDIS_PORT)
    except Exception as e:
        logger.critical("❌ Redis connection failed: %s", e)
        raise SystemExit(1)

    # FIX BUG-H-03: Sync rooms với userId khi khởi động
    if PI_OWNER_UID:
        writer.sync_rooms_from_sqlite(PI_OWNER_UID)
    else:
        logger.warning("PI_OWNER_UID không được cấu hình. Set env PI_OWNER_UID=<firebase_uid> để dashboard hiển thị đúng.")

    stop_event  = threading.Event()
    redis_thread = RedisSyncThread(writer, r)
    redis_thread.start()

    dispatcher = CommandDispatcher(writer, r)
    dispatcher.start()

    flush_thread = threading.Thread(
        target=run_sensor_flush_loop,
        args=(writer, stop_event),
        daemon=True,
        name="sensor-flush",
    )
    flush_thread.start()

    writer.push_alert(
        alert_type="system",
        message="firebase_sync worker started",
        level="info",
        location="Pi Gateway",
    )

    # FIX BUG-C-03: signal handler để cleanup khi Pi tắt/restart
    def _shutdown(sig, frame):
        logger.info("Shutting down firebase_sync (signal %d)...", sig)
        stop_event.set()
        redis_thread.stop()
        dispatcher.stop()
        raise SystemExit(0)

    # signal.signal(signal.SIGTERM, _shutdown)
    # signal.signal(signal.SIGINT,  _shutdown)

    if threading.current_thread() is threading.main_thread():
        try:
            signal.signal(signal.SIGTERM, _shutdown)
            signal.signal(signal.SIGINT, _shutdown)
            print("[SYNCER] Signals registered (MainThread)")
        except ValueError:
            print("[SYNCER] Signal registration failed")
    else:
        print("[SYNCER] Running in sub-thread, skipping signal registration")
    try:
        while True:
            time.sleep(30)
            try:
                r.ping()
            except Exception:
                logger.error("⚠️  Redis heartbeat failed — reconnecting...")
                if not redis_thread.is_alive():
                    redis_thread = RedisSyncThread(writer, get_redis())
                    redis_thread.start()
    except (KeyboardInterrupt, SystemExit):
        stop_event.set()
        redis_thread.stop()
        dispatcher.stop()
        logger.info("firebase_sync stopped.")


# Alias để gateway_main.py gọi `from workers.firebase_sync import run`
def run():
    main()


if __name__ == "__main__":
    main()