"""
workers/firebase_sync.py  — REFACTORED v2
==========================================

KIẾN TRÚC PHÂN TẦNG DỮ LIỆU (Hybrid Storage):
  ┌─────────────────────────────────────────────────────────────────────┐
  │  Realtime Database (RTDB)  ← dữ liệu "nóng" (sensor realtime)      │
  │  Path: live/{room_id}/sensors/{type}                                │
  │  → Dashboard đọc qua onValue() — latency < 500ms                   │
  │  → Không throttle — push mỗi khi nhận từ ESP32                     │
  ├─────────────────────────────────────────────────────────────────────┤
  │  Firestore  ← dữ liệu "tĩnh/cấu trúc"                              │
  │  - rooms/{id}              ← thông tin phòng (tên, icon, userId)    │
  │  - rooms/{id}/devices/{id} ← trạng thái thiết bị                   │
  │  - system_alerts           ← cảnh báo gas/fire                     │
  │  - system_status/wifi      ← trạng thái WiFi                       │
  │  - system_status/available_wifi ← danh sách WiFi quét              │
  │  - commands/{id}           ← lệnh từ Web (Web ghi, Pi xóa)         │
  │  Throttle: 30s cho device_status để tiết kiệm writes               │
  └─────────────────────────────────────────────────────────────────────┘

AUTO-PROVISIONING:
  Khi khởi động, hệ thống tự tạo cấu trúc dữ liệu trên Firebase
  nếu chưa tồn tại — không cần setup thủ công trên Firebase Console.
  - RTDB: Tạo nodes live/{room_id} cho tất cả rooms trong SQLite
  - Firestore: Tạo documents rooms/{id} với thông tin từ SQLite

INVARIANT 1: Chỉ file này được ghi lên Firebase.
"""

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
from firebase_admin import credentials, firestore, db as rtdb

logger = logging.getLogger("firebase_sync")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] firebase_sync: %(message)s",
    datefmt="%H:%M:%S",
)

# ─────────────────────────────────────────────
#  CONFIG — đọc từ .env hoặc dùng default
# ─────────────────────────────────────────────
FIREBASE_PROJECT_ID  = os.getenv("FIREBASE_PROJECT_ID",  "nhathongminh-myhome")
FIREBASE_DB_URL      = os.getenv("FIREBASE_DB_URL",       "")  # https://<project>.firebaseio.com
SERVICE_ACCOUNT_FILE = os.getenv("FIREBASE_SERVICE_ACCOUNT",
                                  "/home/pi/smarthome_prj/GATEWAY/firebase-service-account.json")

REDIS_HOST  = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT  = int(os.getenv("REDIS_PORT", 6379))
SQLITE_PATH = os.getenv("SQLITE_PATH", "/data/smarthome.db")

# Redis channels
CHANNEL_SENSOR      = "realtime_data"
CHANNEL_DEVICE      = "device_status"
CHANNEL_ALERT       = "safety_alert"
CHANNEL_WIFI        = "wifi_status"
CHANNEL_COMMAND_ACK = "command_ack"

# Throttle — chỉ áp dụng cho Firestore device_status (không áp dụng RTDB)
DEVICE_STATUS_THROTTLE_S = 30    # giây: tránh burn Firestore writes cho trạng thái thiết bị

# Sensor history flush interval (SQLite → Firestore sensor_readings history)
SENSOR_FLUSH_INTERVAL_S = 180    # 3 phút: flush lịch sử sensor vào Firestore

# BUG-H-03 fix: Owner UID để gán cho rooms
PI_OWNER_UID = os.getenv("PI_OWNER_UID", "")

# Danh sách rooms seed — fallback nếu SQLite chưa có data
DEFAULT_ROOMS = [
    {"id": "bedroom_01",     "name": "Phòng Ngủ",   "icon": "bed"},
    {"id": "kitchen_01",     "name": "Nhà Bếp",     "icon": "utensils"},
    {"id": "living_room_01", "name": "Phòng Khách", "icon": "sofa"},
]

# ─────────────────────────────────────────────
#  FIREBASE INIT
# ─────────────────────────────────────────────
def init_firebase():
    """
    Khởi tạo Firebase Admin SDK với cả Firestore và Realtime Database.
    Trả về (firestore_client, rtdb_module).
    """
    if not firebase_admin._apps:
        options = {"projectId": FIREBASE_PROJECT_ID}
        if FIREBASE_DB_URL:
            options["databaseURL"] = FIREBASE_DB_URL
        else:
            # Tự suy ra URL nếu không set — convention của Firebase
            options["databaseURL"] = f"https://{FIREBASE_PROJECT_ID}-default-rtdb.firebaseio.com"
            logger.warning(
                "FIREBASE_DB_URL chưa set — tự suy ra: %s", options["databaseURL"]
            )

        if os.path.exists(SERVICE_ACCOUNT_FILE):
            cred = credentials.Certificate(SERVICE_ACCOUNT_FILE)
            firebase_admin.initialize_app(cred, options)
            logger.info("Firebase khởi tạo từ service account file")
        else:
            firebase_admin.initialize_app(options=options)
            logger.warning("Service account không tìm thấy — dùng ADC (Application Default Credentials)")

    fs_client = firestore.client()
    logger.info("Firestore client sẵn sàng")
    logger.info("RTDB client sẵn sàng (databaseURL: %s)", FIREBASE_DB_URL or "auto")
    return fs_client, rtdb


# ─────────────────────────────────────────────
#  REDIS
# ─────────────────────────────────────────────
def get_redis() -> redis.Redis:
    return redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)


# ─────────────────────────────────────────────
#  SQLITE HELPERS
# ─────────────────────────────────────────────
def get_rooms_from_sqlite() -> list:
    """Lấy danh sách rooms từ SQLite để auto-provision."""
    try:
        conn = sqlite3.connect(SQLITE_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM rooms").fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("Không đọc được rooms từ SQLite: %s — dùng DEFAULT_ROOMS", e)
        return DEFAULT_ROOMS


def get_devices_from_sqlite(room_id: str) -> list:
    """Lấy danh sách devices của 1 room từ SQLite."""
    try:
        conn = sqlite3.connect(SQLITE_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM devices WHERE room_id=?", (room_id,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("Không đọc devices từ SQLite (room=%s): %s", room_id, e)
        return []


def get_unsynced_sensor_readings(limit: int = 500) -> list:
    """Lấy sensor data chưa sync lên Firebase (dùng cột firebase_synced)."""
    try:
        conn = sqlite3.connect(SQLITE_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """SELECT id, room as room_id, type as sensor_type, value, timestamp
                   FROM sensor_data WHERE firebase_synced = 0
                   ORDER BY timestamp ASC LIMIT ?""",
                (limit,),
            ).fetchall()
        except Exception:
            # Fallback nếu cột firebase_synced chưa có
            rows = conn.execute(
                """SELECT id, room as room_id, type as sensor_type, value, timestamp
                   FROM sensor_data ORDER BY timestamp DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error("SQLite read error: %s", e)
        return []


def mark_sensor_readings_synced(row_ids: list):
    if not row_ids:
        return
    try:
        conn = sqlite3.connect(SQLITE_PATH, timeout=10)
        try:
            conn.execute("ALTER TABLE sensor_data ADD COLUMN firebase_synced INTEGER DEFAULT 0")
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
#  AUTO PROVISIONER
#  Tự tạo cấu trúc Firebase khi lần đầu chạy
# ─────────────────────────────────────────────
class AutoProvisioner:
    """
    Đảm bảo cấu trúc dữ liệu tồn tại trên Firebase trước khi
    các writer bắt đầu push data.

    RTDB structure được tạo:
        live/
          {room_id}/
            sensors/
              temperature: {value, ts, unit}
              humidity:    {value, ts, unit}
              gas:         {value, ts, unit}
              co2:         {value, ts, unit}
              fire_detected: {value, ts}
            meta/
              online: false
              last_seen: ""
              room_name: "..."

    Firestore structure được tạo:
        rooms/{room_id}
          name, icon, roomType, userId, createdAt, deviceCount
          → subcollection devices/{device_id}
              name, type, isOn, status, details
    """

    def __init__(self, fs_client, rtdb_module, owner_uid: str):
        self.fs       = fs_client
        self.rtdb     = rtdb_module
        self.owner_uid = owner_uid

    def provision_all(self):
        """Entry point — gọi khi khởi động."""
        logger.info("=== AUTO-PROVISIONING BẮT ĐẦU ===")
        rooms = get_rooms_from_sqlite()
        if not rooms:
            rooms = DEFAULT_ROOMS
            logger.warning("SQLite trống — dùng DEFAULT_ROOMS để provision")

        for room in rooms:
            self._provision_rtdb_room(room)
            self._provision_firestore_room(room)

        self._provision_firestore_system_docs()
        logger.info("=== AUTO-PROVISIONING HOÀN TẤT (%d rooms) ===", len(rooms))

    # ── RTDB Provisioning ──────────────────────────────────

    def _provision_rtdb_room(self, room: dict):
        """
        Tạo node RTDB live/{room_id} nếu chưa tồn tại.
        Nếu đã có rồi thì không overwrite (chỉ set fields còn thiếu).
        """
        room_id   = room["id"]
        room_name = room.get("name", room_id)
        ref       = self.rtdb.reference(f"live/{room_id}")

        try:
            existing = ref.get()
            if existing is None:
                # Node chưa tồn tại — tạo mới với placeholder values
                sensor_defaults = {
                    "temperature":  {"value": None, "ts": 0, "unit": "°C"},
                    "humidity":     {"value": None, "ts": 0, "unit": "%"},
                    "gas":          {"value": None, "ts": 0, "unit": "ppm"},
                    "co2":          {"value": None, "ts": 0, "unit": "ppm"},
                    "fire_detected": {"value": False, "ts": 0},
                }
                ref.set({
                    "sensors": sensor_defaults,
                    "meta": {
                        "room_name": room_name,
                        "online":    False,
                        "last_seen": "",
                        "pi_version": 2,
                    },
                })
                logger.info("RTDB: Tạo node live/%s", room_id)
            else:
                # Node đã tồn tại — chỉ cập nhật meta.room_name nếu thay đổi
                meta_ref = self.rtdb.reference(f"live/{room_id}/meta")
                meta_ref.update({"room_name": room_name})
                logger.info("RTDB: Node live/%s đã tồn tại — bỏ qua provision", room_id)
        except Exception as e:
            logger.error("RTDB provision room %s failed: %s", room_id, e)

    # ── Firestore Provisioning ────────────────────────────

    def _provision_firestore_room(self, room: dict):
        """
        Tạo document Firestore rooms/{room_id} với thông tin cơ bản.
        Merge=True để không xóa dữ liệu đã có.
        """
        room_id   = room["id"]
        room_name = room.get("name", room_id)
        room_type = room_id.rsplit("_", 1)[0].upper()   # bedroom_01 → BEDROOM

        devices = get_devices_from_sqlite(room_id)

        try:
            doc_ref  = self.fs.collection("rooms").document(room_id)
            doc_snap = doc_ref.get()

            room_data = {
                "name":        room_name,
                "icon":        room.get("icon", "home"),
                "roomType":    room_type,
                "deviceCount": len(devices),
                "updatedAt":   firestore.SERVER_TIMESTAMP,
            }
            if self.owner_uid:
                room_data["userId"] = self.owner_uid

            if not doc_snap.exists:
                room_data["createdAt"] = firestore.SERVER_TIMESTAMP
                logger.info("Firestore: Tạo rooms/%s", room_id)

            doc_ref.set(room_data, merge=True)

            # Provision từng device
            for device in devices:
                self._provision_firestore_device(room_id, device)

        except Exception as e:
            logger.error("Firestore provision room %s failed: %s", room_id, e)

    def _provision_firestore_device(self, room_id: str, device: dict):
        """Tạo subcollection rooms/{room_id}/devices/{device_id} nếu chưa có."""
        device_id = device["id"]
        try:
            dev_ref  = (self.fs.collection("rooms").document(room_id)
                           .collection("devices").document(device_id))
            dev_snap = dev_ref.get()
            if not dev_snap.exists:
                dev_ref.set({
                    "name":      device.get("name", device_id),
                    "type":      device.get("type", "unknown"),
                    "isOn":      False,
                    "status":    "offline",
                    "details":   "Chưa kết nối",
                    "createdAt": firestore.SERVER_TIMESTAMP,
                    "updatedAt": firestore.SERVER_TIMESTAMP,
                })
                logger.info("  Firestore: Tạo device %s/%s", room_id, device_id)
        except Exception as e:
            logger.error("Firestore provision device %s/%s failed: %s", room_id, device_id, e)

    def _provision_firestore_system_docs(self):
        """Tạo các system documents cần thiết."""
        try:
            # system_status/wifi
            wifi_ref = self.fs.collection("system_status").document("wifi")
            if not wifi_ref.get().exists:
                wifi_ref.set({
                    "status":       "disconnected",
                    "current_ssid": "",
                    "ip":           "",
                    "updatedAt":    firestore.SERVER_TIMESTAMP,
                })
                logger.info("Firestore: Tạo system_status/wifi")

            # system_status/available_wifi
            avail_ref = self.fs.collection("system_status").document("available_wifi")
            if not avail_ref.get().exists:
                avail_ref.set({
                    "networks":  [],
                    "last_scan": firestore.SERVER_TIMESTAMP,
                })
                logger.info("Firestore: Tạo system_status/available_wifi")

            # system_status/gateway
            gw_ref = self.fs.collection("system_status").document("gateway")
            gw_ref.set({
                "online":     True,
                "version":    2,
                "startedAt":  firestore.SERVER_TIMESTAMP,
                "pi_uid":     self.owner_uid or "unknown",
            }, merge=True)

        except Exception as e:
            logger.error("Firestore provision system docs failed: %s", e)


# ─────────────────────────────────────────────
#  RTDB WRITER — dữ liệu "nóng" (sensors)
# ─────────────────────────────────────────────
class RTDBWriter:
    """
    Ghi sensor data realtime lên Firebase Realtime Database.
    Không throttle — push ngay khi nhận từ ESP32 qua Redis.
    Path: live/{room_id}/sensors/{sensor_type}
    """

    def __init__(self, rtdb_module):
        self.rtdb = rtdb_module

    def update_sensor(self, room_id: str, sensor_type: str, value, ts: float):
        """
        Push sensor reading lên RTDB.
        Được gọi mỗi khi nhận message từ Redis channel realtime_data.
        Không throttle vì RTDB free tier cho phép nhiều writes hơn Firestore.
        """
        try:
            ref = self.rtdb.reference(f"live/{room_id}/sensors/{sensor_type}")
            ref.set({
                "value": value,
                # "ts":    int(ts * 1000),   # milliseconds epoch
                "ts": int(time.time()),  # dùng timestamp hiện tại để tránh đồng bộ hóa thời gian giữa Pi và Firebase
                "iso":   datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
            })
        except Exception as e:
            logger.error("RTDB update_sensor [%s/%s] error: %s", room_id, sensor_type, e)

    def update_sensor_bulk(self, room_id: str, sensor_dict: dict, ts: float):
        """
        Push nhiều sensor cùng lúc cho 1 room (atomic update).
        Hiệu quả hơn gọi update_sensor nhiều lần.
        """
        try:
            ts_ms  = int(ts * 1000)
            ts_iso = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
            updates = {}
            for sensor_type, value in sensor_dict.items():
                updates[f"sensors/{sensor_type}"] = {
                    "value": value,
                    "ts":    ts_ms,
                    "iso":   ts_iso,
                }
            updates["meta/last_seen"] = ts_iso
            updates["meta/online"]    = True
            ref = self.rtdb.reference(f"live/{room_id}")
            ref.update(updates)
        except Exception as e:
            logger.error("RTDB update_sensor_bulk [%s] error: %s", room_id, e)

    def set_room_offline(self, room_id: str):
        """Đánh dấu room offline trên RTDB (khi mất kết nối với ESP32)."""
        try:
            self.rtdb.reference(f"live/{room_id}/meta").update({
                "online":    False,
                "last_seen": datetime.now(tz=timezone.utc).isoformat(),
            })
        except Exception as e:
            logger.warning("RTDB set_room_offline [%s] error: %s", room_id, e)

    def heartbeat(self, room_ids: list):
        """Cập nhật trạng thái online của gateway lên RTDB."""
        try:
            now_iso = datetime.now(tz=timezone.utc).isoformat()
            self.rtdb.reference("gateway_status").set({
                "online":    True,
                "last_seen": now_iso,
                "rooms":     room_ids,
            })
        except Exception as e:
            logger.warning("RTDB heartbeat error: %s", e)


# ─────────────────────────────────────────────
#  FIRESTORE WRITER — dữ liệu "tĩnh/cấu trúc"
# ─────────────────────────────────────────────
class FirestoreWriter:
    """
    Ghi dữ liệu cấu trúc lên Firestore.
    Áp dụng throttle cho device_status để tiết kiệm writes.
    KHÔNG ghi sensor realtime (đã chuyển sang RTDB).
    """

    def __init__(self, fs_client):
        self.fs = fs_client
        # Throttle tracker: {room_device_key: last_push_timestamp}
        self._device_last_push: Dict[str, float] = {}

    # ── Device Status ──────────────────────────────────────

    def update_device(self, room_id: str, device_id: str, payload: dict):
        """
        Cập nhật trạng thái thiết bị lên Firestore.
        Throttle 30s để tiết kiệm writes — device status không cần sub-second.
        """
        key = f"{room_id}_{device_id}"
        now = time.time()
        if now - self._device_last_push.get(key, 0) < DEVICE_STATUS_THROTTLE_S:
            return

        try:
            ref = (self.fs.collection("rooms").document(room_id)
                         .collection("devices").document(device_id))
            is_on = payload.get("is_on", False)
            data  = {
                "isOn":      is_on,
                "status":    payload.get("status", "online"),
                "details":   "Đang bật" if is_on else "Đã tắt",
                "updatedAt": firestore.SERVER_TIMESTAMP,
            }
            if "name" in payload:
                data["name"] = payload["name"]
            if "type" in payload:
                data["type"] = payload["type"]

            ref.set(data, merge=True)
            self._device_last_push[key] = now
        except Exception as e:
            logger.error("Firestore update_device [%s/%s] error: %s", room_id, device_id, e)

    def force_update_device(self, room_id: str, device_id: str, payload: dict):
        """Force update bỏ qua throttle — dùng khi có command từ Web."""
        self._device_last_push.pop(f"{room_id}_{device_id}", None)
        self.update_device(room_id, device_id, payload)

    # ── Alerts ────────────────────────────────────────────

    def push_alert(self, alert_type: str, message: str,
                   level: str = "warning", location: str = ""):
        try:
            self.fs.collection("system_alerts").add({
                "type":       alert_type,
                "message":    message,
                "level":      level,
                "location":   location,
                "isResolved": False,
                "timestamp":  firestore.SERVER_TIMESTAMP,
            })
        except Exception as e:
            logger.error("Firestore push_alert error: %s", e)

    # ── Sensor history (batch flush từ SQLite) ────────────

    def batch_push_sensor_history(self, rows: list) -> list:
        """
        Flush lịch sử sensor từ SQLite vào Firestore sensor_readings.
        Dùng batched writes để tối ưu — chunk 499 rows/batch.
        Trả về list ID đã sync thành công.
        """
        if not rows:
            return []
        synced_ids = []
        chunk_size = 499
        for i in range(0, len(rows), chunk_size):
            chunk = rows[i: i + chunk_size]
            batch = self.fs.batch()
            for row in chunk:
                try:
                    ts = datetime.fromisoformat(str(row["timestamp"])).replace(tzinfo=timezone.utc)
                except Exception:
                    ts = datetime.now(timezone.utc)
                new_ref = self.fs.collection("sensor_readings").document()
                batch.set(new_ref, {
                    "roomId":    row["room_id"],
                    "type":      row["sensor_type"],
                    "value":     float(row["value"]),
                    "timestamp": ts,
                })
                synced_ids.append(row["id"])
            try:
                batch.commit()
                logger.info("Firestore: Batch flush %d sensor history rows", len(chunk))
            except Exception as e:
                logger.error("Firestore batch commit failed: %s", e)
        return synced_ids

    # ── System status ─────────────────────────────────────

    def update_wifi_status(self, status: str, ssid: str = "", ip: str = ""):
        try:
            self.fs.collection("system_status").document("wifi").set({
                "status":       status,
                "current_ssid": ssid,
                "ip":           ip,
                "updatedAt":    firestore.SERVER_TIMESTAMP,
            }, merge=True)
        except Exception as e:
            logger.error("Firestore update_wifi_status error: %s", e)

    def update_available_wifi(self, networks: list):
        try:
            self.fs.collection("system_status").document("available_wifi").set({
                "networks":  networks,
                "last_scan": firestore.SERVER_TIMESTAMP,
            }, merge=True)
        except Exception as e:
            logger.error("Firestore update_available_wifi error: %s", e)

    # ── Commands ──────────────────────────────────────────

    def delete_command(self, cmd_id: str):
        try:
            self.fs.collection("commands").document(cmd_id).delete()
        except Exception as e:
            logger.error("Firestore delete_command error: %s", e)

    def ack_command(self, cmd_id: str, status: str, result=None):
        data: Dict[str, Any] = {
            "status":  status,
            "ackedAt": firestore.SERVER_TIMESTAMP,
        }
        if result is not None:
            data["result"] = result
        try:
            self.fs.collection("commands").document(cmd_id).update(data)
        except Exception as e:
            logger.error("Firestore ack_command error: %s", e)

    def listen_commands(self, callback):
        """
        Lắng nghe /commands mới từ Web trên Firestore.
        Trả về watcher để caller có thể .unsubscribe() khi shutdown.
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

        watcher = self.fs.collection("commands").on_snapshot(_on_snapshot)
        return watcher

    def sync_rooms_from_sqlite(self, owner_uid: str):
        """Đồng bộ rooms từ SQLite lên Firestore với trường userId (fix BUG-H-03)."""
        if not owner_uid:
            logger.warning("PI_OWNER_UID chưa được cấu hình — rooms sẽ không hiển thị đúng")
            return
        rooms = get_rooms_from_sqlite()
        for room in rooms:
            room_id   = room["id"]
            room_type = room_id.rsplit("_", 1)[0].upper()
            try:
                self.fs.collection("rooms").document(room_id).set({
                    "name":      room.get("name", room_id),
                    "icon":      room.get("icon", "home"),
                    "roomType":  room_type,
                    "userId":    owner_uid,
                    "updatedAt": firestore.SERVER_TIMESTAMP,
                }, merge=True)
            except Exception as e:
                logger.error("sync_rooms room %s error: %s", room_id, e)
        logger.info("Synced %d rooms to Firestore (userId=%s)", len(rooms), owner_uid)


# ─────────────────────────────────────────────
#  REDIS SYNC THREAD
#  Subscribe Redis → route lên RTDB hoặc Firestore
# ─────────────────────────────────────────────
class RedisSyncThread(threading.Thread):
    """
    Subscribe Redis pubsub và route messages:
    - Sensor data      → RTDBWriter  (không throttle, realtime)
    - Device status    → FirestoreWriter (throttle 30s)
    - Alerts           → FirestoreWriter
    - WiFi status      → FirestoreWriter
    - Command ACK      → FirestoreWriter
    """

    def __init__(self, rtdb_writer: RTDBWriter,
                 fs_writer: FirestoreWriter,
                 redis_client: redis.Redis):
        super().__init__(daemon=True, name="redis-sync")
        self.rtdb_writer = rtdb_writer
        self.fs_writer   = fs_writer
        self.r           = redis_client
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def run(self):
        pubsub = self.r.pubsub()
        pubsub.subscribe(
            CHANNEL_SENSOR, CHANNEL_DEVICE,
            CHANNEL_ALERT,  CHANNEL_WIFI, CHANNEL_COMMAND_ACK
        )
        logger.info("Redis subscriber started (channels: %s)",
                    [CHANNEL_SENSOR, CHANNEL_DEVICE, CHANNEL_ALERT, CHANNEL_WIFI])

        while not self._stop_event.is_set():
            try:
                msg = pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if msg and msg["type"] == "message":
                    self._handle(msg["channel"], msg["data"])
            except Exception as e:
                logger.error("Redis subscriber error: %s", e)
                time.sleep(2)

        try:
            pubsub.unsubscribe()
            pubsub.close()
        except Exception:
            pass
        logger.info("Redis subscriber stopped cleanly")

    def _handle(self, channel: str, raw: str):
        try:
            payload = json.loads(raw)
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
            logger.error("Handle message [%s] error: %s", channel, e)

    def _on_sensor(self, p: dict):
        """
        Sensor data → RTDB (realtime, không throttle).
        Hỗ trợ 2 format:
          1. {room_id, type, value, timestamp}       — từ automation_engine
          2. {event, room, sensors:{type:value,...}} — từ mqtt_inbound bridge
        """
        room_id = p.get("room_id") or p.get("room")
        ts      = p.get("ts") or time.time()

        # Format 1: single sensor
        if p.get("type") and p.get("value") is not None:
            s_type = p["type"]
            value  = p["value"]
            self.rtdb_writer.update_sensor(room_id, s_type, value, float(ts))
            return

        # Format 2: sensors dict (bulk update từ mqtt envelope)
        sensors = p.get("sensors") or p.get("payload") or p.get("data")
        if isinstance(sensors, dict) and room_id:
            self.rtdb_writer.update_sensor_bulk(room_id, sensors, float(ts))
            return

    def _on_device(self, p: dict):
        room_id   = p.get("room_id") or p.get("room")
        device_id = p.get("device_id")
        if room_id and device_id:
            self.fs_writer.update_device(room_id, device_id, p)

    def _on_alert(self, p: dict):
        self.fs_writer.push_alert(
            alert_type=p.get("type", "system"),
            message=p.get("message", ""),
            level=p.get("level", "warning"),
            location=p.get("location", p.get("room", "")),
        )

    def _on_wifi(self, p: dict):
        self.fs_writer.update_wifi_status(
            status=p.get("status", "disconnected"),
            ssid=p.get("ssid", ""),
            ip=p.get("ip", ""),
        )
        if "networks" in p:
            self.fs_writer.update_available_wifi(p["networks"])

    def _on_command_ack(self, p: dict):
        cmd_id = p.get("cmd_id")
        if not cmd_id:
            return
        if p.get("status") == "done":
            self.fs_writer.delete_command(cmd_id)
        else:
            self.fs_writer.ack_command(cmd_id, p.get("status", "error"), p.get("result"))


# ─────────────────────────────────────────────
#  MQTT INBOUND BRIDGE
#  mqtt_inbound → RTDB sensor realtime
# ─────────────────────────────────────────────
class MqttInboundBridge(threading.Thread):
    """
    Subscribe kênh mqtt_inbound (MQTT → Redis từ MessageBus).
    Phân tích topic home/{room}/{category} và forward sensor data lên RTDB.
    Tách riêng khỏi RedisSyncThread để không block.
    """

    def __init__(self, rtdb_writer: RTDBWriter, redis_client: redis.Redis):
        super().__init__(daemon=True, name="mqtt-bridge")
        self.rtdb_writer = rtdb_writer
        self.r           = redis_client
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def run(self):
        pubsub = self.r.pubsub()
        pubsub.subscribe("mqtt_inbound")
        logger.info("MQTT inbound bridge started")

        while not self._stop_event.is_set():
            try:
                msg = pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if msg and msg["type"] == "message":
                    self._handle(msg["data"])
            except Exception as e:
                logger.error("MQTT bridge error: %s", e)
                time.sleep(1)

        try:
            pubsub.unsubscribe()
            pubsub.close()
        except Exception:
            pass

    def _handle(self, raw: str):
        """
        Envelope từ MessageBus:
        {"topic": "home/{room}/sensors", "payload": {temp, hum, gas, ...}, "ts": float}
        """
        try:
            envelope = json.loads(raw)
        except Exception:
            return

        topic   = envelope.get("topic", "")
        payload = envelope.get("payload", {})
        ts      = envelope.get("ts", time.time())

        parts = topic.split("/")
        # Expected: ["home", "{room_id}", "{category}"]
        if len(parts) < 3:
            return

        room_id  = parts[1]
        category = parts[2]

        if category == "sensors" and isinstance(payload, dict):
            # Bulk update tất cả sensor readings lên RTDB — realtime
            self.rtdb_writer.update_sensor_bulk(room_id, payload, float(ts))


# ─────────────────────────────────────────────
#  COMMAND DISPATCHER
#  Firestore /commands → Redis device_commands
# ─────────────────────────────────────────────
class CommandDispatcher:
    """
    Lắng nghe Firestore /commands và publish sang Redis.
    BUG-C-03 fix: watcher được lưu để gọi .unsubscribe() khi stop().
    BUG-H-02 fix: normalize roomId/room_id → room trước khi publish.
    """

    REDIS_CHANNEL = "device_commands"

    def __init__(self, fs_writer: FirestoreWriter, redis_client: redis.Redis):
        self.fs_writer = fs_writer
        self.r         = redis_client
        self._watcher  = None

    def start(self):
        self._watcher = self.fs_writer.listen_commands(self._dispatch)
        logger.info("CommandDispatcher started")

    def stop(self):
        if self._watcher:
            try:
                self._watcher.unsubscribe()
                logger.info("CommandDispatcher stopped cleanly")
            except Exception as e:
                logger.error("CommandDispatcher stop error: %s", e)

    def _dispatch(self, cmd_id: str, data: dict):
        self.fs_writer.ack_command(cmd_id, "processing")

        # Normalize field names — Web gửi roomId, Pi đọc room
        room_id = (data.get("room") or data.get("roomId")
                   or data.get("room_id") or "")
        device_id = (data.get("device_id") or data.get("deviceId") or "")

        msg = {
            **data,
            "room":      room_id,
            "roomId":    room_id,
            "room_id":   room_id,
            "device_id": device_id,
            "deviceId":  device_id,
            "cmd_id":    cmd_id,
        }

        action  = data.get("action", "")
        channel = self.REDIS_CHANNEL

        if action in ("turn_on", "turn_off", "toggle"):
            channel = self.REDIS_CHANNEL
        elif action == "add_and_connect":
            channel = "wifi_setup"
        elif action in ("start_register", "cancel_register"):
            channel = "rfid_register"

        try:
            self.r.publish(channel, json.dumps(msg))
            logger.info("Dispatched cmd '%s' [%s] → Redis[%s]", cmd_id, action, channel)

            # Nếu là lệnh device → force update Firestore device status ngay
            if action in ("turn_on", "turn_off") and room_id and device_id:
                self.fs_writer.force_update_device(room_id, device_id, {
                    "is_on":  action == "turn_on",
                    "status": "online",
                })
        except Exception as e:
            logger.error("Dispatch error: %s", e)
            self.fs_writer.ack_command(cmd_id, "error", str(e))


# ─────────────────────────────────────────────
#  SENSOR HISTORY FLUSH LOOP
#  SQLite sensor_data (firebase_synced=0) → Firestore sensor_readings
# ─────────────────────────────────────────────
def run_sensor_flush_loop(fs_writer: FirestoreWriter, stop_event: threading.Event):
    """
    Định kỳ flush lịch sử sensor từ SQLite lên Firestore sensor_readings.
    Đây là dữ liệu HISTORY (cho chart) — khác với RTDB là realtime snapshot.
    """
    logger.info("Sensor history flush loop started (interval: %ds)", SENSOR_FLUSH_INTERVAL_S)
    while not stop_event.is_set():
        stop_event.wait(timeout=SENSOR_FLUSH_INTERVAL_S)
        if stop_event.is_set():
            break
        try:
            rows = get_unsynced_sensor_readings(limit=500)
            if rows:
                synced_ids = fs_writer.batch_push_sensor_history(rows)
                mark_sensor_readings_synced(synced_ids)
                logger.info("Flushed %d sensor history rows to Firestore", len(synced_ids))
        except Exception as e:
            logger.error("Sensor flush error: %s", e)


# ─────────────────────────────────────────────
#  HEARTBEAT LOOP
#  Cập nhật trạng thái gateway lên RTDB
# ─────────────────────────────────────────────
def run_heartbeat_loop(rtdb_writer: RTDBWriter, stop_event: threading.Event):
    """Gửi heartbeat lên RTDB mỗi 30s để Web biết Pi đang online."""
    rooms = [r["id"] for r in get_rooms_from_sqlite()]
    while not stop_event.is_set():
        try:
            rtdb_writer.heartbeat(rooms)
        except Exception as e:
            logger.warning("Heartbeat error: %s", e)
        stop_event.wait(timeout=30)


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────
def main():
    logger.info("=" * 55)
    logger.info("firebase_sync v2 starting — project: %s", FIREBASE_PROJECT_ID)
    logger.info("Storage: RTDB=sensors (hot) | Firestore=rooms/devices/alerts (structured)")
    logger.info("=" * 55)

    # 1. Khởi tạo Firebase (Firestore + RTDB)
    fs_client, rtdb_module = init_firebase()

    fs_writer   = FirestoreWriter(fs_client)
    rtdb_writer = RTDBWriter(rtdb_module)

    # 2. Kết nối Redis
    r = get_redis()
    try:
        r.ping()
        logger.info("Redis connected at %s:%d", REDIS_HOST, REDIS_PORT)
    except Exception as e:
        logger.critical("Redis connection failed: %s", e)
        raise SystemExit(1)

    # 3. AUTO-PROVISIONING — tạo cấu trúc Firebase nếu chưa có
    logger.info("Chạy auto-provisioning...")
    provisioner = AutoProvisioner(fs_client, rtdb_module, PI_OWNER_UID)
    provisioner.provision_all()

    # 4. Sync rooms với userId (BUG-H-03)
    if PI_OWNER_UID:
        fs_writer.sync_rooms_from_sqlite(PI_OWNER_UID)
    else:
        logger.warning(
            "PI_OWNER_UID chưa set. Rooms sẽ hiển thị fallback (tất cả rooms). "
            "Set PI_OWNER_UID=<firebase_uid> trong .env để filter đúng."
        )

    # 5. Khởi động các thread
    stop_event = threading.Event()

    redis_thread = RedisSyncThread(rtdb_writer, fs_writer, r)
    redis_thread.start()

    mqtt_bridge = MqttInboundBridge(rtdb_writer, r)
    mqtt_bridge.start()

    dispatcher = CommandDispatcher(fs_writer, r)
    dispatcher.start()

    flush_thread = threading.Thread(
        target=run_sensor_flush_loop,
        args=(fs_writer, stop_event),
        daemon=True, name="sensor-flush",
    )
    flush_thread.start()

    heartbeat_thread = threading.Thread(
        target=run_heartbeat_loop,
        args=(rtdb_writer, stop_event),
        daemon=True, name="heartbeat",
    )
    heartbeat_thread.start()

    # 6. Push startup alert lên Firestore
    fs_writer.push_alert(
        alert_type="system",
        message="firebase_sync v2 started (RTDB+Firestore hybrid)",
        level="info",
        location="Pi Gateway",
    )

    # 7. Signal handler cho graceful shutdown
    def _shutdown(sig, frame):
        logger.info("Shutting down firebase_sync (signal %d)...", sig)
        stop_event.set()
        redis_thread.stop()
        mqtt_bridge.stop()
        dispatcher.stop()
        try:
            rtdb_writer.heartbeat.__func__  # just flush last heartbeat
        except Exception:
            pass
        raise SystemExit(0)

    if threading.current_thread() is threading.main_thread():
        try:
            signal.signal(signal.SIGTERM, _shutdown)
            signal.signal(signal.SIGINT,  _shutdown)
            logger.info("Signal handlers registered")
        except ValueError:
            logger.warning("Signal registration failed (not main thread)")

    # 8. Main keep-alive loop với Redis health check
    try:
        while True:
            time.sleep(30)
            try:
                r.ping()
            except Exception:
                logger.error("Redis heartbeat failed — attempting reconnect...")
                try:
                    r = get_redis()
                    if not redis_thread.is_alive():
                        redis_thread = RedisSyncThread(rtdb_writer, fs_writer, r)
                        redis_thread.start()
                    if not mqtt_bridge.is_alive():
                        mqtt_bridge = MqttInboundBridge(rtdb_writer, r)
                        mqtt_bridge.start()
                except Exception as e:
                    logger.error("Reconnect failed: %s", e)
    except (KeyboardInterrupt, SystemExit):
        stop_event.set()
        redis_thread.stop()
        mqtt_bridge.stop()
        dispatcher.stop()
        logger.info("firebase_sync stopped.")


def run():
    """Alias cho gateway_main.py: from workers.firebase_sync import run"""
    main()


if __name__ == "__main__":
    main()
