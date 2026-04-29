"""
workers/firebase_sync.py
========================
Firebase Sync Worker — SmartHome Gateway
=========================================

Nhiệm vụ duy nhất: đồng bộ dữ liệu từ Redis/SQLite lên Firebase Firestore.
Đây là code path DUY NHẤT được phép ghi lên Firebase (INVARIANT 1).

Luồng dữ liệu:
  Redis pubsub (realtime_data / device_status) → Firestore
  SQLite (batch flush) → Firestore sensor_readings

Firestore paths (phải khớp với Web JS):
  rooms/{roomId}/sensors/{type}           ← trạng thái sensor hiện tại
  rooms/{roomId}/devices/{deviceId}       ← trạng thái thiết bị
  sensor_readings/{autoId}               ← lịch sử đọc sensor (cho chart)
  system_alerts/{autoId}                 ← cảnh báo safety_watchdog
  commands/{cmdId}                       ← lệnh từ Web (Pi đọc, xóa sau khi thực thi)
  automations/{userId}_{roomId}          ← cấu hình automation (Pi đọc)
  schedules/{userId}_{roomId}_{deviceId} ← lịch hẹn giờ (Pi đọc)
  system_status/wifi                     ← trạng thái WiFi Pi
  system_status/available_wifi           ← danh sách mạng quét được
"""

import asyncio
import json
import logging
import os
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
#  CONFIG  (override bằng env vars nếu muốn)
# ─────────────────────────────────────────────
FIREBASE_PROJECT_ID   = os.getenv("FIREBASE_PROJECT_ID", "nhathongminh-myhome")
SERVICE_ACCOUNT_FILE  = os.getenv("FIREBASE_SERVICE_ACCOUNT", "/home/pi/smarthome_prj/GATEWAY/firebase-service-account.json")

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))

SQLITE_PATH = os.getenv("SQLITE_PATH", "/home/pi/smarthome_prj/GATEWAY/storage/smarthome.db")

# Channels Redis mà firebase_sync lắng nghe
CHANNEL_SENSOR     = "realtime_data"      # automation_engine publish sensor mới
CHANNEL_DEVICE     = "device_status"      # automation_engine publish trạng thái thiết bị
CHANNEL_ALERT      = "safety_alert"       # safety_watchdog publish cảnh báo
CHANNEL_WIFI       = "wifi_status"        # network_watchdog publish trạng thái WiFi
CHANNEL_COMMAND_ACK = "command_ack"       # Pi publish khi hoàn thành lệnh từ Web

# Batch flush sensor_readings lên Firestore mỗi N giây
SENSOR_FLUSH_INTERVAL = 180  # 3 phút, khớp với data_syncer.py

# Chỉ push snapshot lên Firebase mỗi N giây để tránh vượt free tier
SENSOR_SNAPSHOT_THROTTLE = 10  # giây


# ─────────────────────────────────────────────
#  FIREBASE INIT
# ─────────────────────────────────────────────
def init_firebase() -> firestore.Client:
    """Khởi tạo Firebase Admin SDK một lần duy nhất."""
    if not firebase_admin._apps:
        if os.path.exists(SERVICE_ACCOUNT_FILE):
            cred = credentials.Certificate(SERVICE_ACCOUNT_FILE)
            firebase_admin.initialize_app(cred, {"projectId": FIREBASE_PROJECT_ID})
            logger.info("✅ Firebase khởi tạo từ service account file")
        else:
            # Fallback: dùng Application Default Credentials (ADC)
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
def get_unsynced_readings(limit: int = 500) -> list[dict]:
    """Lấy các bản ghi sensor chưa được sync lên Firebase."""
    try:
        conn = sqlite3.connect(SQLITE_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            """SELECT id, room_id, sensor_type, value, timestamp
               FROM sensor_data
               WHERE firebase_synced = 0
               ORDER BY timestamp ASC
               LIMIT ?""",
            (limit,),
        )
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows
    except Exception as e:
        logger.error("SQLite read error: %s", e)
        return []


def mark_synced(row_ids: list[int]):
    """Đánh dấu các bản ghi đã sync thành công."""
    if not row_ids:
        return
    try:
        conn = sqlite3.connect(SQLITE_PATH, timeout=10)
        conn.execute(
            f"UPDATE sensor_data SET firebase_synced=1 WHERE id IN ({','.join('?'*len(row_ids))})",
            row_ids,
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error("SQLite mark_synced error: %s", e)


# ─────────────────────────────────────────────
#  FIRESTORE WRITE HELPERS
# ─────────────────────────────────────────────
class FirestoreWriter:
    """
    Wrapper quanh Firestore client.
    Tất cả ghi đều đi qua class này để dễ throttle / retry.
    """

    def __init__(self, db: firestore.Client):
        self.db = db
        # throttle: lưu timestamp lần cuối update sensor snapshot
        self._last_sensor_push: Dict[str, float] = {}

    # ── 1. Sensor snapshot (rooms/{roomId}/sensors/{type}) ──────────────
    def update_sensor(self, room_id: str, sensor_type: str, value: float, timestamp: datetime):
        key = f"{room_id}_{sensor_type}"
        now = time.time()
        if now - self._last_sensor_push.get(key, 0) < SENSOR_SNAPSHOT_THROTTLE:
            return  # throttle

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
        logger.debug("📡 Sensor snapshot: %s/%s = %s", room_id, sensor_type, value)

    # ── 2. Sensor history (sensor_readings/{autoId}) ─────────────────────
    def push_sensor_reading(self, room_id: str, sensor_type: str, value: float, ts_iso: str):
        """Push một bản ghi lịch sử lên Firestore (cho biểu đồ)."""
        try:
            ts = datetime.fromisoformat(ts_iso).replace(tzinfo=timezone.utc)
        except ValueError:
            ts = datetime.now(timezone.utc)

        self.db.collection("sensor_readings").add(
            {
                "roomId": room_id,
                "type": sensor_type,
                "value": value,
                "timestamp": ts,
            }
        )

    def batch_push_sensor_readings(self, rows: list[dict]) -> list[int]:
        """Batch write nhiều bản ghi. Trả về danh sách id đã push thành công."""
        if not rows:
            return []

        synced_ids = []
        # Firestore batch tối đa 500 writes
        chunk_size = 499
        for i in range(0, len(rows), chunk_size):
            chunk = rows[i : i + chunk_size]
            batch = self.db.batch()
            for row in chunk:
                try:
                    ts = datetime.fromisoformat(str(row["timestamp"])).replace(tzinfo=timezone.utc)
                except Exception:
                    ts = datetime.now(timezone.utc)

                new_ref = self.db.collection("sensor_readings").document()
                batch.set(
                    new_ref,
                    {
                        "roomId": row["room_id"],
                        "type": row["sensor_type"],
                        "value": float(row["value"]),
                        "timestamp": ts,
                    },
                )
                synced_ids.append(row["id"])
            try:
                batch.commit()
                logger.info("📦 Batch pushed %d sensor readings", len(chunk))
            except Exception as e:
                logger.error("Batch commit failed: %s", e)
                # Không thêm vào synced_ids nếu fail

        return synced_ids

    # ── 3. Device state (rooms/{roomId}/devices/{deviceId}) ─────────────
    def update_device(self, room_id: str, device_id: str, payload: dict):
        ref = (
            self.db.collection("rooms")
            .document(room_id)
            .collection("devices")
            .document(device_id)
        )
        data = {
            "isOn": payload.get("is_on", False),
            "status": payload.get("status", "online"),
            "details": "Đang bật" if payload.get("is_on") else "Đã tắt",
            "updatedAt": firestore.SERVER_TIMESTAMP,
        }
        if "name" in payload:
            data["name"] = payload["name"]
        if "type" in payload:
            data["type"] = payload["type"]

        ref.set(data, merge=True)
        logger.debug("💡 Device update: %s/%s → %s", room_id, device_id, data["isOn"])

    # ── 4. System alerts (system_alerts/{autoId}) ────────────────────────
    def push_alert(self, alert_type: str, message: str, level: str = "warning", location: str = ""):
        self.db.collection("system_alerts").add(
            {
                "type": alert_type,       # 'fire' | 'gas' | 'intrusion' | 'system'
                "message": message,
                "level": level,           # 'critical' | 'warning' | 'info'
                "location": location,
                "isResolved": False,
                "timestamp": firestore.SERVER_TIMESTAMP,
            }
        )
        logger.warning("🚨 Alert pushed: [%s] %s", alert_type, message)

    # ── 5. Command ACK — xóa lệnh sau khi Pi thực thi ───────────────────
    def delete_command(self, cmd_id: str):
        try:
            self.db.collection("commands").document(cmd_id).delete()
            logger.info("🗑️  Command deleted: %s", cmd_id)
        except Exception as e:
            logger.error("Delete command error: %s", e)

    def ack_command(self, cmd_id: str, status: str, result: Optional[Any] = None):
        """Cập nhật trạng thái lệnh (thay vì xóa ngay — để Web biết kết quả)."""
        data: Dict[str, Any] = {"status": status, "ackedAt": firestore.SERVER_TIMESTAMP}
        if result is not None:
            data["result"] = result
        try:
            self.db.collection("commands").document(cmd_id).update(data)
        except Exception as e:
            logger.error("ACK command error: %s", e)

    # ── 6. WiFi status (system_status/wifi) ─────────────────────────────
    def update_wifi_status(self, status: str, ssid: str = "", ip: str = ""):
        self.db.collection("system_status").document("wifi").set(
            {
                "status": status,          # 'connected' | 'disconnected' | 'connecting'
                "current_ssid": ssid,
                "ip": ip,
                "updatedAt": firestore.SERVER_TIMESTAMP,
            },
            merge=True,
        )

    def update_available_wifi(self, networks: list[dict]):
        """Cập nhật danh sách WiFi quét được cho settings.js."""
        self.db.collection("system_status").document("available_wifi").set(
            {
                "networks": networks,     # [{ssid, signal, secured}, ...]
                "last_scan": firestore.SERVER_TIMESTAMP,
            }
        )

    # ── 7. COMMAND LISTENER — đọc lệnh từ Web ───────────────────────────
    def listen_commands(self, on_command_callback):
        """
        Lắng nghe collection commands (onSnapshot).
        Khi có document mới với status='pending', gọi callback.
        Callback nhận: (cmd_id, cmd_data)
        """
        def on_snapshot(col_snapshot, changes, read_time):
            for change in changes:
                if change.type.name in ("ADDED", "MODIFIED"):
                    doc = change.document
                    data = doc.to_dict()
                    if data.get("status") == "pending":
                        logger.info("📥 Command received: %s → %s", doc.id, data)
                        try:
                            on_command_callback(doc.id, data)
                        except Exception as e:
                            logger.error("Command callback error: %s", e)

        col_ref = self.db.collection("commands")
        # watch() trả về Watcher, giữ reference để không bị GC
        watcher = col_ref.on_snapshot(on_snapshot)
        return watcher


# ─────────────────────────────────────────────
#  REDIS SUBSCRIBER THREAD
# ─────────────────────────────────────────────
class RedisSyncThread(threading.Thread):
    """
    Subscribe Redis pubsub và forward lên Firestore.
    Chạy trong thread riêng để không block main loop.
    """

    def __init__(self, writer: FirestoreWriter, redis_client: redis.Redis):
        super().__init__(daemon=True, name="redis-sync")
        self.writer = writer
        self.r = redis_client
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def run(self):
        pubsub = self.r.pubsub()
        pubsub.subscribe(CHANNEL_SENSOR, CHANNEL_DEVICE, CHANNEL_ALERT, CHANNEL_WIFI, CHANNEL_COMMAND_ACK)
        logger.info("🔌 Redis subscriber started (channels: %s, %s, %s, %s, %s)",
                    CHANNEL_SENSOR, CHANNEL_DEVICE, CHANNEL_ALERT, CHANNEL_WIFI, CHANNEL_COMMAND_ACK)

        while not self._stop_event.is_set():
            try:
                message = pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if message and message["type"] == "message":
                    self._handle_message(message["channel"], message["data"])
            except Exception as e:
                logger.error("Redis subscriber error: %s", e)
                time.sleep(2)

        pubsub.unsubscribe()
        logger.info("Redis subscriber stopped")

    def _handle_message(self, channel: str, raw_data: str):
        try:
            payload = json.loads(raw_data)
        except json.JSONDecodeError:
            logger.warning("Invalid JSON on channel %s: %s", channel, raw_data[:100])
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
        """
        Expected payload từ automation_engine:
        {
          "room_id": "abc123",
          "type": "temperature",   # hoặc humidity | co2 | gas
          "value": 28.5,
          "timestamp": "2025-01-01T12:00:00"
        }
        """
        room_id  = p.get("room_id")
        s_type   = p.get("type")
        value    = p.get("value")
        ts_str   = p.get("timestamp", datetime.now().isoformat())

        if not all([room_id, s_type, value is not None]):
            return

        ts = datetime.fromisoformat(ts_str) if ts_str else datetime.now()

        # Cập nhật snapshot (throttled)
        self.writer.update_sensor(room_id, s_type, float(value), ts)

    def _on_device(self, p: dict):
        """
        Expected payload:
        {
          "room_id": "abc123",
          "device_id": "light_1",
          "is_on": true,
          "status": "online",
          "name": "Đèn ngủ",
          "type": "light"
        }
        """
        room_id   = p.get("room_id")
        device_id = p.get("device_id")
        if not room_id or not device_id:
            return

        self.writer.update_device(room_id, device_id, p)

    def _on_alert(self, p: dict):
        """
        Expected payload:
        {
          "type": "gas",           # fire | gas | intrusion | system
          "message": "Phát hiện khí gas!",
          "level": "critical",
          "location": "Nhà bếp"
        }
        """
        self.writer.push_alert(
            alert_type=p.get("type", "system"),
            message=p.get("message", ""),
            level=p.get("level", "warning"),
            location=p.get("location", ""),
        )

    def _on_wifi(self, p: dict):
        """
        Expected payload:
        {
          "status": "connected",
          "ssid": "MyWifi",
          "ip": "192.168.1.100",
          "networks": [{"ssid":"X","signal":80,"secured":true}, ...]
        }
        """
        self.writer.update_wifi_status(
            status=p.get("status", "disconnected"),
            ssid=p.get("ssid", ""),
            ip=p.get("ip", ""),
        )
        if "networks" in p:
            self.writer.update_available_wifi(p["networks"])

    def _on_command_ack(self, p: dict):
        """
        Expected payload:
        {
          "cmd_id": "xyz",
          "status": "done",    # done | error
          "result": ...
        }
        """
        cmd_id = p.get("cmd_id")
        if not cmd_id:
            return

        status = p.get("status", "done")
        if status == "done":
            # Xóa luôn để Firestore sạch (INVARIANT 4: không lưu lịch sử)
            self.writer.delete_command(cmd_id)
        else:
            self.writer.ack_command(cmd_id, status, p.get("result"))


# ─────────────────────────────────────────────
#  COMMAND DISPATCHER
#  Đọc lệnh từ Firestore → publish Redis → automation_engine xử lý
# ─────────────────────────────────────────────
class CommandDispatcher:
    """
    Lắng nghe Firestore /commands và publish sang Redis channel
    để automation_engine hoặc message_bus xử lý.
    """

    REDIS_COMMAND_CHANNEL = "device_commands"

    def __init__(self, writer: FirestoreWriter, redis_client: redis.Redis):
        self.writer = writer
        self.r = redis_client
        self._watcher = None

    def start(self):
        self._watcher = self.writer.listen_commands(self._dispatch)
        logger.info("📋 CommandDispatcher started (watching Firestore /commands)")

    def stop(self):
        if self._watcher:
            self._watcher.unsubscribe()

    def _dispatch(self, cmd_id: str, data: dict):
        """
        Khi Web ghi lệnh vào Firestore /commands/{cmdId} với status='pending',
        ta publish sang Redis để automation_engine nhận và thực thi.
        Sau đó đánh dấu status='processing' để tránh xử lý trùng.
        """
        # Đánh dấu đang xử lý
        self.writer.ack_command(cmd_id, "processing")

        # Build Redis message
        msg = {**data, "cmd_id": cmd_id}
        action = data.get("action", "")

        # Routing theo action type
        if action in ("turn_on", "turn_off", "toggle"):
            channel = self.REDIS_COMMAND_CHANNEL
        elif action in ("add_and_connect",):
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
#  BATCH SENSOR FLUSH LOOP
# ─────────────────────────────────────────────
def run_sensor_flush_loop(writer: FirestoreWriter):
    """
    Mỗi SENSOR_FLUSH_INTERVAL giây, đọc bản ghi chưa sync từ SQLite
    và push batch lên Firestore sensor_readings.
    Chạy trong thread riêng.
    """
    logger.info("🔄 Sensor flush loop started (interval: %ds)", SENSOR_FLUSH_INTERVAL)
    while True:
        time.sleep(SENSOR_FLUSH_INTERVAL)
        try:
            rows = get_unsynced_readings(limit=500)
            if rows:
                logger.info("🔄 Flushing %d sensor readings to Firestore...", len(rows))
                synced_ids = writer.batch_push_sensor_readings(rows)
                mark_synced(synced_ids)
                logger.info("✅ Flushed %d rows", len(synced_ids))
            else:
                logger.debug("No unsynced sensor readings")
        except Exception as e:
            logger.error("Sensor flush error: %s", e)


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────
def main():
    logger.info("=" * 50)
    logger.info("🚀 firebase_sync.py starting — project: %s", FIREBASE_PROJECT_ID)
    logger.info("=" * 50)

    # 1. Init Firebase
    db_client = init_firebase()
    writer = FirestoreWriter(db_client)

    # 2. Init Redis
    r = get_redis()
    try:
        r.ping()
        logger.info("✅ Redis connected at %s:%d", REDIS_HOST, REDIS_PORT)
    except Exception as e:
        logger.critical("❌ Redis connection failed: %s", e)
        raise SystemExit(1)

    # 3. Start Redis subscriber thread
    redis_thread = RedisSyncThread(writer, r)
    redis_thread.start()

    # 4. Start Command Dispatcher (Firestore → Redis)
    dispatcher = CommandDispatcher(writer, r)
    dispatcher.start()

    # 5. Start Sensor Flush loop in background thread
    flush_thread = threading.Thread(
        target=run_sensor_flush_loop,
        args=(writer,),
        daemon=True,
        name="sensor-flush",
    )
    flush_thread.start()

    # 6. Push trạng thái khởi động
    writer.push_alert(
        alert_type="system",
        message="firebase_sync worker started",
        level="info",
        location="Pi Gateway",
    )

    logger.info("✅ All workers running. Ctrl+C to stop.")

    # 7. Keep main thread alive
    try:
        while True:
            time.sleep(30)
            # Heartbeat check
            try:
                r.ping()
            except Exception:
                logger.error("⚠️  Redis heartbeat failed — reconnecting thread...")
                if not redis_thread.is_alive():
                    redis_thread = RedisSyncThread(writer, get_redis())
                    redis_thread.start()
    except KeyboardInterrupt:
        logger.info("Shutting down firebase_sync...")
        redis_thread.stop()
        dispatcher.stop()


if __name__ == "__main__":
    main()