"""
workers/firebase_sync.py  — v2.1  (Thiết kế đề xuất)
═══════════════════════════════════════════════════════
THAY ĐỔI SO VỚI v2.0:

  [A] PHÂN RÃ UPLINK / DOWNLINK STREAM
      ─────────────────────────────────
      Uplink   = UplinkStream  (Producer) — chỉ đẩy dữ liệu lên Firebase.
      Downlink = CommandDispatcher        — chỉ nhận lệnh từ Firestore xuống.
      Hai luồng hoàn toàn độc lập, lỗi ở một luồng không block luồng kia.

  [B] ON-CHANGE DEVICE STATUS (Bỏ Throttle 30s)
      ─────────────────────────────────────────
      FirestoreWriter.update_device() không còn Throttle 30s nữa.
      Thay vào đó dùng cơ chế STATE CACHE:
        - Chỉ ghi lên Firestore khi trạng thái THỰC SỰ THAY ĐỔI.
        - Ví dụ: quạt đang ON → nhận status ON lần nữa → KHÔNG ghi.
        - Khi bật/tắt bằng nút vật lý: ESP32 gửi status → Pi nhận →
          Firestore cập nhật NGAY LẬP TỨC (< 500ms) → Web đồng bộ.
      Lợi ích:
        - Không còn "khoảng tối" 30s → Race condition không còn xảy ra.
        - Số lượng Firestore writes thực tế KHÔNG tăng nhiều vì chỉ
          write khi thay đổi (On = 1 write, Off = 1 write).

  [C] GIỮ NGUYÊN
      ──────────
      - RTDB sensors: push realtime, không throttle (giữ nguyên v2.0)
      - Auto-provisioning, sensor history flush, heartbeat (giữ nguyên)
      - CommandDispatcher: Firestore listener → Redis (giữ nguyên)
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

import firebase_admin
from firebase_admin import credentials, firestore, db as rtdb

logger = logging.getLogger("firebase_sync")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] firebase_sync: %(message)s",
    datefmt="%H:%M:%S",
)

# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────
FIREBASE_PROJECT_ID  = os.getenv("FIREBASE_PROJECT_ID",  "nhathongminh-myhome")
FIREBASE_DB_URL      = os.getenv("FIREBASE_DB_URL",
                                  "https://nhathongminh-myhome-default-rtdb.asia-southeast1.firebasedatabase.app")
SERVICE_ACCOUNT_FILE = os.getenv("FIREBASE_SERVICE_ACCOUNT",
                                  "/home/pi/smarthome_prj/GATEWAY/firebase-service-account.json")
REDIS_HOST  = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT  = int(os.getenv("REDIS_PORT", 6379))
SQLITE_PATH = os.getenv("SQLITE_PATH", "/data/smarthome.db")
PI_OWNER_UID = os.getenv("PI_OWNER_UID", "")

# Redis channels
CHANNEL_SENSOR      = "realtime_data"
CHANNEL_DEVICE      = "device_status"
CHANNEL_ALERT       = "safety_alert"
CHANNEL_WIFI        = "wifi_status"
CHANNEL_COMMAND_ACK = "command_ack"

# v2.1: Không còn DEVICE_STATUS_THROTTLE_S — thay bằng On-Change cache
# DEVICE_STATUS_THROTTLE_S = 30  ← ĐÃ XÓA

SENSOR_FLUSH_INTERVAL_S = 180  # 3 phút: flush lịch sử sensor vào Firestore

DEFAULT_ROOMS = [
    {"id": "bedroom_01",     "name": "Phòng Ngủ",   "icon": "bed"},
    {"id": "kitchen_01",     "name": "Nhà Bếp",     "icon": "utensils"},
    {"id": "living_room_01", "name": "Phòng Khách", "icon": "sofa"},
]

# ─────────────────────────────────────────────
#  FIREBASE INIT
# ─────────────────────────────────────────────
def init_firebase():
    if not firebase_admin._apps:
        options = {"projectId": FIREBASE_PROJECT_ID}
        db_url = FIREBASE_DB_URL or \
            f"https://{FIREBASE_PROJECT_ID}-default-rtdb.asia-southeast1.firebasedatabase.app"
        options["databaseURL"] = db_url

        cred = None
        if os.path.exists(SERVICE_ACCOUNT_FILE):
            try:
                cred = credentials.Certificate(SERVICE_ACCOUNT_FILE)
                logger.info("Sử dụng Service Account: %s", SERVICE_ACCOUNT_FILE)
            except Exception as e:
                logger.error("Lỗi đọc file service account: %s", e)

        try:
            if cred:
                firebase_admin.initialize_app(cred, options)
            else:
                firebase_admin.initialize_app(options=options)
                logger.warning("Dùng ADC (Application Default Credentials).")
        except Exception as e:
            logger.critical("Không thể khởi tạo Firebase: %s", e)
            raise

    return firestore.client(), rtdb


# ─────────────────────────────────────────────
#  REDIS / SQLITE HELPERS
# ─────────────────────────────────────────────
def get_redis() -> redis.Redis:
    return redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)


def get_rooms_from_sqlite() -> list:
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
    try:
        conn = sqlite3.connect(SQLITE_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM devices WHERE room_id=?", (room_id,)).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("Không đọc devices từ SQLite (room=%s): %s", room_id, e)
        return []


def get_unsynced_sensor_readings(limit: int = 500) -> list:
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
            pass
        conn.execute(
            f"UPDATE sensor_data SET firebase_synced=1 WHERE id IN ({','.join('?'*len(row_ids))})",
            row_ids,
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error("SQLite mark_synced error: %s", e)


def json_serializable(obj):
    if hasattr(obj, 'isoformat'):
        return obj.isoformat()
    if hasattr(obj, 'to_datetime'):
        return obj.to_datetime().isoformat()
    return str(obj)


# ─────────────────────────────────────────────
#  AUTO PROVISIONER (giữ nguyên từ v2.0)
# ─────────────────────────────────────────────
class AutoProvisioner:
    def __init__(self, fs_client, rtdb_module, owner_uid: str):
        self.fs        = fs_client
        self.rtdb      = rtdb_module
        self.owner_uid = owner_uid

    def provision_all(self):
        logger.info("=== AUTO-PROVISIONING BẮT ĐẦU ===")
        rooms = get_rooms_from_sqlite() or DEFAULT_ROOMS
        for room in rooms:
            self._provision_rtdb_room(room)
            self._provision_firestore_room(room)
        self._provision_firestore_system_docs()
        logger.info("=== AUTO-PROVISIONING HOÀN TẤT (%d rooms) ===", len(rooms))

    def _provision_rtdb_room(self, room: dict):
        room_id   = room["id"]
        room_name = room.get("name", room_id)
        ref       = self.rtdb.reference(f"live/{room_id}")
        try:
            existing = ref.get()
            if existing is None:
                ref.set({
                    "sensors": {
                        "temperature":   {"value": None, "ts": 0, "unit": "°C"},
                        "humidity":      {"value": None, "ts": 0, "unit": "%"},
                        "gas":           {"value": None, "ts": 0, "unit": "ppm"},
                        "co2":           {"value": None, "ts": 0, "unit": "ppm"},
                        "fire_detected": {"value": False, "ts": 0},
                    },
                    "meta": {"room_name": room_name, "online": False, "last_seen": "", "pi_version": 2},
                })
                logger.info("RTDB: Tạo node live/%s", room_id)
            else:
                self.rtdb.reference(f"live/{room_id}/meta").update({"room_name": room_name})
        except Exception as e:
            logger.error("RTDB provision room %s failed: %s", room_id, e)

    def _provision_firestore_room(self, room: dict):
        room_id   = room["id"]
        room_name = room.get("name", room_id)
        room_type = room_id.rsplit("_", 1)[0].upper()
        devices   = get_devices_from_sqlite(room_id)
        try:
            doc_ref  = self.fs.collection("rooms").document(room_id)
            doc_snap = doc_ref.get()
            room_data = {
                "name": room_name, "icon": room.get("icon", "home"),
                "roomType": room_type, "deviceCount": len(devices),
                "updatedAt": firestore.SERVER_TIMESTAMP,
            }
            if self.owner_uid:
                room_data["userId"] = self.owner_uid
            if not doc_snap.exists:
                room_data["createdAt"] = firestore.SERVER_TIMESTAMP
            doc_ref.set(room_data, merge=True)
            for device in devices:
                self._provision_firestore_device(room_id, device)
        except Exception as e:
            logger.error("Firestore provision room %s failed: %s", room_id, e)

    def _provision_firestore_device(self, room_id: str, device: dict):
        device_id = device["id"]
        try:
            dev_ref  = (self.fs.collection("rooms").document(room_id)
                           .collection("devices").document(device_id))
            dev_snap = dev_ref.get()
            if not dev_snap.exists:
                dev_ref.set({
                    "name": device.get("name", device_id), "type": device.get("type", "unknown"),
                    "isOn": False, "status": "offline", "details": "Chưa kết nối",
                    "createdAt": firestore.SERVER_TIMESTAMP, "updatedAt": firestore.SERVER_TIMESTAMP,
                })
        except Exception as e:
            logger.error("Firestore provision device %s/%s failed: %s", room_id, device_id, e)

    def _provision_firestore_system_docs(self):
        try:
            wifi_ref = self.fs.collection("system_status").document("wifi")
            if not wifi_ref.get().exists:
                wifi_ref.set({"status": "disconnected", "current_ssid": "", "ip": "",
                              "updatedAt": firestore.SERVER_TIMESTAMP})
            avail_ref = self.fs.collection("system_status").document("available_wifi")
            if not avail_ref.get().exists:
                avail_ref.set({"networks": [], "last_scan": firestore.SERVER_TIMESTAMP})
            self.fs.collection("system_status").document("gateway").set({
                "online": True, "version": 2, "startedAt": firestore.SERVER_TIMESTAMP,
                "pi_uid": self.owner_uid or "unknown",
            }, merge=True)
        except Exception as e:
            logger.error("Firestore provision system docs failed: %s", e)


# ─────────────────────────────────────────────
#  RTDB WRITER — sensors realtime (không đổi)
# ─────────────────────────────────────────────
class RTDBWriter:
    def __init__(self, rtdb_module):
        self.rtdb = rtdb_module

    def update_sensor(self, room_id: str, sensor_type: str, value, ts: float):
        if value is None:
            return
        try:
            ts_now = int(time.time())
            self.rtdb.reference(f"live/{room_id}/sensors/{sensor_type}").set({
                "value": value, "ts": ts_now,
                "iso": datetime.fromtimestamp(ts_now, tz=timezone.utc).isoformat(),
                "unit": "°C" if sensor_type == "temperature" else "%",
            })
        except Exception as e:
            logger.error("RTDB update_sensor [%s/%s] error: %s", room_id, sensor_type, e)

    def update_sensor_bulk(self, room_id: str, sensor_dict: dict, ts: float):
        try:
            ts_now = int(time.time())
            ts_iso = datetime.fromtimestamp(ts_now, tz=timezone.utc).isoformat()
            updates = {}
            for sensor_type, value in sensor_dict.items():
                if value is None:
                    continue
                updates[f"sensors/{sensor_type}"] = {
                    "value": value, "ts": ts_now, "iso": ts_iso,
                    "unit": "°C" if sensor_type == "temperature" else "%",
                }
            if not updates:
                return
            updates["meta/last_seen"] = ts_iso
            updates["meta/online"]    = True
            self.rtdb.reference(f"live/{room_id}").update(updates)
        except Exception as e:
            logger.error("RTDB update_sensor_bulk [%s] error: %s", room_id, e)

    def set_room_offline(self, room_id: str):
        try:
            self.rtdb.reference(f"live/{room_id}/meta").update({
                "online": False,
                "last_seen": datetime.now(tz=timezone.utc).isoformat(),
            })
        except Exception as e:
            logger.warning("RTDB set_room_offline [%s] error: %s", room_id, e)

    def heartbeat(self, room_ids: list):
        try:
            self.rtdb.reference("gateway_status").set({
                "online": True,
                "last_seen": datetime.now(tz=timezone.utc).isoformat(),
                "rooms": room_ids,
            })
        except Exception as e:
            logger.warning("RTDB heartbeat error: %s", e)


# ─────────────────────────────────────────────
#  FIRESTORE WRITER
#  v2.1: Bỏ Throttle 30s → On-Change State Cache
# ─────────────────────────────────────────────
class FirestoreWriter:
    """
    Ghi dữ liệu cấu trúc lên Firestore.

    v2.1 THAY ĐỔI QUAN TRỌNG — update_device():
      - Bỏ Throttle 30s (DEVICE_STATUS_THROTTLE_S)
      - Thay bằng On-Change cache: chỉ ghi Firestore khi isOn THỰC SỰ THAY ĐỔI
        hoặc khi status (online/offline) thay đổi.
      - Kết quả: Web cập nhật NGAY khi bật/tắt thiết bị (kể cả bằng nút vật lý),
        đồng thời số Firestore writes KHÔNG tăng đáng kể vì không write trùng.
    """

    def __init__(self, fs_client):
        self.fs = fs_client
        # v2.1: State cache thay cho throttle timer
        # { "room_device_key": {"isOn": bool, "status": str} }
        self._device_state_cache: Dict[str, dict] = {}

    # ── Device Status (On-Change) ──────────────────────────

    def update_device(self, room_id: str, device_id: str, payload: dict):
        """
        [v2.1] Cập nhật trạng thái thiết bị lên Firestore theo On-Change.
        Chỉ ghi khi isOn hoặc status THỰC SỰ THAY ĐỔI — không throttle 30s.

        Điều này giải quyết 2 vấn đề:
          1. Race condition: Web biết trạng thái thực tế NGAY LẬP TỨC (<500ms).
          2. Nút bật/tắt vật lý: ESP32 báo về → cập nhật Firestore trong < 1s.
        """
        key    = f"{room_id}_{device_id}"
        is_on  = bool(payload.get("is_on", payload.get("isOn", False)))
        status = payload.get("status", "online")

        # Lấy state hiện tại từ cache
        cached = self._device_state_cache.get(key)

        # Chỉ ghi nếu có thay đổi (hoặc lần đầu chưa có cache)
        if cached is not None:
            if cached.get("isOn") == is_on and cached.get("status") == status:
                return   # Không thay đổi → bỏ qua, tiết kiệm Firestore write

        try:
            ref = (self.fs.collection("rooms").document(room_id)
                         .collection("devices").document(device_id))
            data = {
                "isOn":      is_on,
                "status":    status,
                "details":   "Đang bật" if is_on else "Đã tắt",
                "updatedAt": firestore.SERVER_TIMESTAMP,
            }
            if "name" in payload:
                data["name"] = payload["name"]
            if "type" in payload:
                data["type"] = payload["type"]

            ref.set(data, merge=True)

            # Cập nhật cache SAU KHI ghi thành công
            self._device_state_cache[key] = {"isOn": is_on, "status": status}
            logger.info("Firestore device [%s/%s] → %s (On-Change write)",
                        room_id, device_id, "ON" if is_on else "OFF")
        except Exception as e:
            logger.error("Firestore update_device [%s/%s] error: %s", room_id, device_id, e)

    def force_update_device(self, room_id: str, device_id: str, payload: dict):
        """
        Force update — bỏ qua On-Change cache.
        Dùng khi nhận lệnh từ Web (cần phản hồi ngay lập tức cho UI).
        """
        key = f"{room_id}_{device_id}"
        self._device_state_cache.pop(key, None)   # Xóa cache để force ghi
        self.update_device(room_id, device_id, payload)

    def invalidate_device_cache(self, room_id: str, device_id: str):
        """Xóa cache cho thiết bị — dùng sau khi biết trạng thái không đáng tin cậy."""
        self._device_state_cache.pop(f"{room_id}_{device_id}", None)

    # ── Alerts ────────────────────────────────────────────

    def push_alert(self, alert_type: str, message: str,
                   level: str = "warning", location: str = ""):
        try:
            self.fs.collection("system_alerts").add({
                "type":       alert_type, "message": message,
                "level":      level,      "location": location,
                "isResolved": False,      "timestamp": firestore.SERVER_TIMESTAMP,
            })
        except Exception as e:
            logger.error("Firestore push_alert error: %s", e)

    # ── Sensor history (batch flush) ──────────────────────

    def batch_push_sensor_history(self, rows: list) -> list:
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
                    "roomId":        row["room_id"],
                    "type":          row["sensor_type"],
                    "value":         float(row["value"]),
                    "timestamp":     ts,
                    "timestamp_iso": ts.isoformat(),
                })
                synced_ids.append(row["id"])
            try:
                batch.commit()
                logger.info("Firestore: Batch flush %d sensor history rows", len(chunk))
            except Exception as e:
                logger.error("Firestore batch commit failed: %s", e)
        return synced_ids

    # ── WiFi status ───────────────────────────────────────

    def update_wifi_status(self, status: str, ssid: str = "", ip: str = ""):
        try:
            self.fs.collection("system_status").document("wifi").set({
                "status": status, "current_ssid": ssid, "ip": ip,
                "updatedAt": firestore.SERVER_TIMESTAMP,
            }, merge=True)
        except Exception as e:
            logger.error("Firestore update_wifi_status error: %s", e)

    def update_available_wifi(self, networks: list):
        try:
            self.fs.collection("system_status").document("available_wifi").set({
                "networks": networks, "last_scan": firestore.SERVER_TIMESTAMP,
            }, merge=True)
        except Exception as e:
            logger.error("Firestore update_available_wifi error: %s", e)

    # ── Commands ──────────────────────────────────────────

    def delete_command(self, cmd_id: str):
        try:
            self.fs.collection("commands").document(cmd_id).delete()
            logger.info("Đã xóa lệnh hoàn tất: %s", cmd_id)
        except Exception as e:
            logger.error("Lỗi xóa lệnh: %s", e)

    def ack_command(self, cmd_id: str, status: str, result=None):
        data: Dict[str, Any] = {"status": status, "ackedAt": firestore.SERVER_TIMESTAMP}
        if result is not None:
            try:
                json.dumps(result)
                data["result"] = result
            except (TypeError, OverflowError):
                data["result"] = (
                    {k: json_serializable(v) for k, v in result.items()}
                    if isinstance(result, dict) else json_serializable(result)
                )
        try:
            self.fs.collection("commands").document(cmd_id).update(data)
        except Exception as e:
            logger.error("Firestore ack_command error: %s", e)

    def listen_commands(self, callback):
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
        return self.fs.collection("commands").on_snapshot(_on_snapshot)

    def sync_rooms_from_sqlite(self, owner_uid: str):
        if not owner_uid:
            logger.warning("PI_OWNER_UID chưa được cấu hình")
            return
        rooms = get_rooms_from_sqlite()
        for room in rooms:
            room_id   = room["id"]
            room_type = room_id.rsplit("_", 1)[0].upper()
            try:
                self.fs.collection("rooms").document(room_id).set({
                    "name": room.get("name", room_id), "icon": room.get("icon", "home"),
                    "roomType": room_type, "userId": owner_uid,
                    "updatedAt": firestore.SERVER_TIMESTAMP,
                }, merge=True)
            except Exception as e:
                logger.error("sync_rooms room %s error: %s", room_id, e)
        logger.info("Synced %d rooms to Firestore (userId=%s)", len(rooms), owner_uid)


# ─────────────────────────────────────────────
#  UPLINK STREAM (Producer)
#  v2.1: Tách rõ thành UplinkStream độc lập
#  Chỉ đẩy dữ liệu lên Firebase — không làm gì khác
# ─────────────────────────────────────────────
class UplinkStream(threading.Thread):
    """
    [v2.1] Uplink Stream — Producer duy nhất đẩy dữ liệu lên Firebase.

    Subscribe các Redis channel:
      - realtime_data  → sensor → RTDB (không throttle)
      - device_status  → On-Change → Firestore
      - safety_alert   → Firestore system_alerts
      - wifi_status    → Firestore system_status
      - command_ack    → Firestore commands (ACK/delete)
      - mqtt_inbound   → sensor bulk → RTDB (trực tiếp từ MQTT envelope)

    Hoàn toàn độc lập với Downlink (CommandDispatcher).
    Nếu Cloud chậm, chỉ Uplink bị trễ — Downlink vẫn nhận lệnh bình thường.
    """

    def __init__(self, rtdb_writer: RTDBWriter,
                 fs_writer: FirestoreWriter,
                 redis_client: redis.Redis):
        super().__init__(daemon=True, name="uplink-stream")
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
            CHANNEL_ALERT,  CHANNEL_WIFI, CHANNEL_COMMAND_ACK,
            "mqtt_inbound",
        )
        logger.info("[Uplink] Stream started — channels: sensor/device/alert/wifi/ack/mqtt")

        while not self._stop_event.is_set():
            try:
                msg = pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if msg and msg["type"] == "message":
                    self._handle(msg["channel"], msg["data"])
            except Exception as e:
                logger.error("[Uplink] Error: %s", e)
                time.sleep(2)

        try:
            pubsub.unsubscribe()
            pubsub.close()
        except Exception:
            pass
        logger.info("[Uplink] Stream stopped cleanly")

    def _handle(self, channel: str, raw: str):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return
        try:
            if channel == CHANNEL_SENSOR:
                self._on_sensor(payload)
            elif channel == "mqtt_inbound":
                self._on_mqtt_inbound(payload)
            elif channel == CHANNEL_DEVICE:
                self._on_device(payload)
            elif channel == CHANNEL_ALERT:
                self._on_alert(payload)
            elif channel == CHANNEL_WIFI:
                self._on_wifi(payload)
            elif channel == CHANNEL_COMMAND_ACK:
                self._on_command_ack(payload)
        except Exception as e:
            logger.error("[Uplink] Handle [%s] error: %s", channel, e)

    def _on_sensor(self, p: dict):
        room_id = p.get("room_id") or p.get("room")
        if not room_id:
            return
        ts = p.get("ts") or time.time()
        if p.get("type") and p.get("value") is not None:
            value = p["value"]
            if value == 0 and (not ts or ts == 0):
                return
            self.rtdb_writer.update_sensor(room_id, p["type"], value, float(ts))
            return
        sensors = p.get("sensors") or p.get("payload") or p.get("data")
        if isinstance(sensors, dict):
            clean = {k: v for k, v in sensors.items() if v is not None}
            if clean:
                self.rtdb_writer.update_sensor_bulk(room_id, clean, float(ts))

    def _on_mqtt_inbound(self, envelope: dict):
        """MQTT envelope từ MessageBus: {topic, payload, ts}"""
        topic   = envelope.get("topic", "")
        payload = envelope.get("payload", {})
        ts      = envelope.get("ts", time.time())
        parts   = topic.split("/")
        if len(parts) < 3:
            return
        room_id  = parts[1]
        category = parts[2]
        if category == "sensors" and isinstance(payload, dict):
            self.rtdb_writer.update_sensor_bulk(room_id, payload, float(ts))

    def _on_device(self, p: dict):
        """
        [v2.1] Device status từ ESP32/AutomationEngine → Firestore On-Change.
        Không còn throttle 30s — cập nhật ngay khi trạng thái thay đổi.
        """
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
#  DOWNLINK STREAM (Consumer)
#  v2.1: CommandDispatcher là Downlink Stream
#  Chỉ nhận lệnh từ Firestore → Redis
# ─────────────────────────────────────────────
class CommandDispatcher:
    """
    [v2.1] Downlink Stream — Consumer nhận lệnh từ Firestore.

    Firestore /commands (on_snapshot listener) → normalize → Redis channel
    Tách biệt hoàn toàn với UplinkStream:
      - Cloud upload chậm không ảnh hưởng nhận lệnh
      - Lệnh điều khiển được xử lý ngay lập tức
    """

    REDIS_CHANNEL = "device_commands"

    def __init__(self, fs_writer: FirestoreWriter, redis_client: redis.Redis, db):
        self.fs_writer = fs_writer
        self.r         = redis_client
        self.fs        = db
        self._watcher  = None

    def start(self):
        self._watcher = self.fs_writer.listen_commands(self._dispatch)
        logger.info("[Downlink] CommandDispatcher started — listening Firestore /commands")

    def stop(self):
        if self._watcher:
            try:
                self._watcher.unsubscribe()
                logger.info("[Downlink] CommandDispatcher stopped cleanly")
            except Exception as e:
                logger.error("[Downlink] Stop error: %s", e)

    def _dispatch(self, cmd_id: str, data: dict):
        action    = data.get("action", "")
        channel   = self.REDIS_CHANNEL
        if action == "add_and_connect":
            channel = "wifi_setup"
        elif action in ("start_register", "cancel_register"):
            channel = "rfid_register"

        room_id   = (data.get("room") or data.get("roomId") or data.get("room_id") or "")
        device_id = (data.get("device_id") or data.get("deviceId") or "")

        msg = {
            "room":      room_id,
            "device_id": device_id,
            "cmd_id":    cmd_id,
            "action":    action,
            "payload":   data.get("payload", {}),
        }

        try:
            self.r.publish(channel, json.dumps(msg))
            logger.info("[Downlink] DISPATCH: %s [%s] → Redis[%s]", cmd_id, action, channel)

            # Cập nhật "optimistic state" ngay lập tức để Web không lag
            # (force_update bỏ qua On-Change cache vì đây là lệnh mới từ user)
            if action in ("turn_on", "turn_off") and room_id and device_id:
                self.fs_writer.force_update_device(room_id, device_id, {
                    "is_on": action == "turn_on", "status": "online",
                })

            # Xóa lệnh khỏi Firestore ngay để tránh dispatch lặp
            self.fs.collection("commands").document(cmd_id).delete()
            logger.info("[Downlink] CLEANUP: Đã xóa lệnh %s", cmd_id)

        except Exception as e:
            logger.error("[Downlink] Dispatch error cho lệnh %s: %s", cmd_id, e)
            try:
                self.fs_writer.ack_command(cmd_id, "error", str(e))
            except Exception:
                pass


# ─────────────────────────────────────────────
#  BACKGROUND LOOPS (giữ nguyên từ v2.0)
# ─────────────────────────────────────────────
def run_sensor_flush_loop(fs_writer: FirestoreWriter, stop_event: threading.Event):
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


def run_heartbeat_loop(rtdb_writer: RTDBWriter, stop_event: threading.Event):
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
    logger.info("firebase_sync v2.1 starting — project: %s", FIREBASE_PROJECT_ID)
    logger.info("Uplink: On-Change (no throttle) | Downlink: Firestore listener")
    logger.info("=" * 55)

    fs_client, rtdb_module = init_firebase()
    fs_writer   = FirestoreWriter(fs_client)
    rtdb_writer = RTDBWriter(rtdb_module)
    r           = get_redis()

    try:
        r.ping()
        logger.info("Redis connected at %s:%d", REDIS_HOST, REDIS_PORT)
    except Exception as e:
        logger.critical("Redis connection failed: %s", e)
        raise SystemExit(1)

    # Auto-provisioning
    logger.info("Chạy auto-provisioning...")
    AutoProvisioner(fs_client, rtdb_module, PI_OWNER_UID).provision_all()

    if PI_OWNER_UID:
        fs_writer.sync_rooms_from_sqlite(PI_OWNER_UID)
    else:
        logger.warning("PI_OWNER_UID chưa set. Set PI_OWNER_UID=<firebase_uid> trong .env.")

    stop_event = threading.Event()

    # [v2.1] Uplink Stream — Producer
    uplink = UplinkStream(rtdb_writer, fs_writer, r)
    uplink.start()

    # [v2.1] Downlink Stream — Consumer (CommandDispatcher)
    dispatcher = CommandDispatcher(fs_writer=fs_writer, redis_client=r, db=fs_client)
    dispatcher.start()

    # Background loops
    threading.Thread(target=run_sensor_flush_loop, args=(fs_writer, stop_event),
                     daemon=True, name="sensor-flush").start()
    threading.Thread(target=run_heartbeat_loop,    args=(rtdb_writer, stop_event),
                     daemon=True, name="heartbeat").start()

    fs_writer.push_alert(
        alert_type="system",
        message="firebase_sync v2.1 started (On-Change device sync, split Uplink/Downlink)",
        level="info", location="Pi Gateway",
    )

    def _shutdown(sig, frame):
        logger.info("Shutting down firebase_sync (signal %d)...", sig)
        stop_event.set()
        uplink.stop()
        dispatcher.stop()
        raise SystemExit(0)

    if threading.current_thread() is threading.main_thread():
        try:
            signal.signal(signal.SIGTERM, _shutdown)
            signal.signal(signal.SIGINT,  _shutdown)
        except ValueError:
            logger.warning("Signal registration failed (not main thread)")

    # Keep-alive loop với Redis health check
    try:
        while True:
            time.sleep(30)
            try:
                r.ping()
            except Exception:
                logger.error("Redis heartbeat failed — attempting reconnect...")
                try:
                    r = get_redis()
                    if not uplink.is_alive():
                        uplink = UplinkStream(rtdb_writer, fs_writer, r)
                        uplink.start()
                except Exception as e:
                    logger.error("Reconnect failed: %s", e)
    except (KeyboardInterrupt, SystemExit):
        stop_event.set()
        uplink.stop()
        dispatcher.stop()
        logger.info("firebase_sync stopped.")


def run():
    """Alias cho gateway_main.py."""
    main()


if __name__ == "__main__":
    main()
