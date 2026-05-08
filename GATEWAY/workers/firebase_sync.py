"""
workers/firebase_sync.py  — v2.2  (CONFLICT FIX)
═════════════════════════════════════════════════
FIXES trong phiên bản này:

  [CONFLICT A1 — HIGH] Single Writer cho RTDB Sensor
      ─────────────────────────────────────────────
      Vấn đề: Cả _on_sensor() (từ "realtime_data") lẫn _on_mqtt_inbound()
              (từ "mqtt_inbound") đều gọi rtdb_writer.update_sensor_bulk()
              → mỗi reading ESP32 bị ghi 2 lần lên RTDB, tốn quota Firebase
                và tạo race condition giữa 2 lần ghi.
      Fix: UplinkStream CHỈ subscribe "mqtt_inbound" làm nguồn dữ liệu sensor.
           _on_sensor() (từ "realtime_data") KHÔNG ghi RTDB nữa — chỉ xử lý
           các sự kiện khác (alert, metadata).
           Luồng chuẩn: ESP32 → MQTT → MessageBus → mqtt_inbound → UplinkStream → RTDB
           automation_engine.publish_event("realtime_data") chỉ dùng cho SocketIO/Web,
           không ghi Firebase.

  [CONFLICT B1 — HIGH] Bỏ force_update_device() trong CommandDispatcher
      ─────────────────────────────────────────────────────────────────
      Vấn đề: CommandDispatcher.force_update_device() ghi Firestore ngay khi
              dispatch lệnh, trong khi FirebaseSync cũng ghi lại sau khi nhận
              feedback từ ESP32 qua "device_status" channel.
              Hai lần ghi với giá trị có thể KHÁC NHAU → UI nhấp nháy.
      Fix: Xóa force_update_device() khỏi _dispatch().
           Luồng chuẩn duy nhất:
             ESP32 → MQTT status → automation_engine → "device_status" → FirebaseSync → Firestore
           Web UI cập nhật sau khi ESP32 confirm (~200-500ms), không cập nhật optimistic.

  [Giữ nguyên từ v2.1]
      On-Change device status cache (bỏ throttle 30s), auto-provisioning,
      sensor history flush, heartbeat, CommandDispatcher Firestore listener.
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
# FIX A1: Bỏ "realtime_data" khỏi sensor path — chỉ dùng "mqtt_inbound" cho sensor
CHANNEL_DEVICE      = "device_status"
CHANNEL_ALERT       = "safety_alert"
CHANNEL_WIFI        = "wifi_status"
CHANNEL_COMMAND_ACK = "command_ack"
# CHANNEL_SENSOR = "realtime_data"  ← KHÔNG subscribe nữa cho RTDB write

SENSOR_FLUSH_INTERVAL_S = 60  # FIX: giảm từ 180s để biểu đồ có data nhanh hơn

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
#  AUTO PROVISIONER
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
#  RTDB WRITER
# ─────────────────────────────────────────────
class RTDBWriter:
    def __init__(self, rtdb_module):
        self.rtdb = rtdb_module

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
# ─────────────────────────────────────────────
class FirestoreWriter:
    """
    v2.2: Bỏ force_update_device() — FirebaseSync là writer DUY NHẤT
    cho device status, chỉ khi nhận feedback từ ESP32 qua "device_status".
    """

    def __init__(self, fs_client):
        self.fs = fs_client
        # On-Change cache: chỉ ghi Firestore khi trạng thái thực sự thay đổi
        self._device_state_cache: Dict[str, dict] = {}

    # def update_device(self, room_id: str, device_id: str, payload: dict):
    #     """
    #     [v2.2 - FIXED] Cập nhật trạng thái thiết bị lên Firestore theo On-Change.
    #     - Tự động gán 'type' nếu bị rỗng để Dashboard nhận diện được thiết bị.
    #     - Tự động gán 'name' thân thiện dựa trên ID nếu thiếu.
    #     """
    #     key    = f"{room_id}_{device_id}"
    #     is_on  = bool(payload.get("is_on", payload.get("isOn", False)))
    #     status = payload.get("status", "online")

    #     cached = self._device_state_cache.get(key)
    #     if cached is not None:
    #         if cached.get("isOn") == is_on and cached.get("status") == status:
    #             return   # Không thay đổi → bỏ qua

    #     try:
    #         ref = (self.fs.collection("rooms").document(room_id)
    #                      .collection("devices").document(device_id))
            
    #         # Khởi tạo data cơ bản
    #         data = {
    #             "isOn":      is_on,
    #             "status":    status,
    #             "details":   "Đang bật" if is_on else "Đã tắt",
    #             "updatedAt": firestore.SERVER_TIMESTAMP,
    #         }

    #         # --- LOGIC FIX TYPE: Tránh bị rỗng khiến Dashboard không hiện ---
    #         dev_type = payload.get("type")
    #         if not dev_type or dev_type == "":
    #             # Tự suy luận type từ device_id (Ví dụ: fan_bd_1 -> fan)
    #             if "fan" in device_id.lower():
    #                 dev_type = "fan"
    #             elif "light" in device_id.lower():
    #                 dev_type = "light"
    #             else:
    #                 dev_type = "unknown"
    #         data["type"] = dev_type

    #         # --- LOGIC FIX NAME: Hiển thị tên tiếng Việt thân thiện ---
    #         dev_name = payload.get("name")
    #         if not dev_name or dev_name == "":
    #             # Nếu không có tên, đặt tên theo loại thiết bị
    #             if dev_type == "fan":
    #                 dev_name = "Quạt"
    #             elif dev_type == "light":
    #                 dev_name = "Đèn"
    #             else:
    #                 dev_name = device_id # Giữ nguyên ID nếu không xác định được
    #         data["name"] = dev_name

    #         # Thực hiện ghi đè dữ liệu có merge
    #         ref.set(data, merge=True)
            
    #         # Cập nhật cache để tránh ghi lặp lại liên tục
    #         self._device_state_cache[key] = {"isOn": is_on, "status": status}
            
    #         logger.info("Firestore device [%s/%s] → %s | Name: %s | Type: %s (Fixed)",
    #                     room_id, device_id, ("ON" if is_on else "OFF"), dev_name, dev_type)
                        
    #     except Exception as e:
    #         logger.error("Firestore update_device [%s/%s] error: %s", room_id, device_id, e)

    def update_device(self, room_id: str, device_id: str, payload: dict):
        """
        [v2.2 - FIXED NAME] Cập nhật trạng thái và ép tên tiếng Việt thân thiện.
        - Ưu tiên gán name là 'Quạt' hoặc 'Đèn' dựa vào ID.
        - Đảm bảo 'type' luôn có giá trị để hiển thị danh sách thiết bị.
        """
        key    = f"{room_id}_{device_id}"
        is_on  = bool(payload.get("is_on", payload.get("isOn", False)))
        status = payload.get("status", "online")

        # Kiểm tra cache để tránh ghi trùng lặp dữ liệu cũ
        cached = self._device_state_cache.get(key)
        if cached is not None:
            if cached.get("isOn") == is_on and cached.get("status") == status:
                return   

        try:
            ref = (self.fs.collection("rooms").document(room_id)
                         .collection("devices").document(device_id))
            
            # 1. Khởi tạo data cơ bản
            data = {
                "isOn":      is_on,
                "status":    status,
                "details":   "Đang bật" if is_on else "Đã tắt",
                "updatedAt": firestore.SERVER_TIMESTAMP,
            }

            # 2. Xử lý TYPE (Mechanical necessity để hiện icon trên Dashboard)
            dev_type = payload.get("type")
            if not dev_type or dev_type == "":
                if "fan" in device_id.lower():
                    dev_type = "fan"
                elif "light" in device_id.lower():
                    dev_type = "light"
                else:
                    dev_type = "unknown"
            data["type"] = dev_type

            # 3. Xử lý NAME (Ép hiển thị tiếng Việt như image_17a667.png)
            # Thay vì ưu tiên payload["name"], ta ưu tiên nhận diện qua ID để fix lỗi ID hiện lên UI
            if dev_type == "fan":
                dev_name = "Quạt"
            elif dev_type == "light":
                dev_name = "Đèn"
            else:
                # Nếu không phải quạt/đèn, mới lấy từ payload hoặc dùng device_id gốc
                dev_name = payload.get("name") or device_id 
            
            data["name"] = dev_name

            # 4. Thực hiện ghi đè lên Firestore
            ref.set(data, merge=True)
            
            # Cập nhật cache
            self._device_state_cache[key] = {"isOn": is_on, "status": status}
            
            logger.info("Firestore sync [%s/%s] -> %s | Name: %s | Type: %s",
                        room_id, device_id, ("ON" if is_on else "OFF"), dev_name, dev_type)
                        
        except Exception as e:
            logger.error("Firestore update_device [%s/%s] error: %s", room_id, device_id, e)

    def invalidate_device_cache(self, room_id: str, device_id: str):
        self._device_state_cache.pop(f"{room_id}_{device_id}", None)

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

    def delete_command(self, cmd_id: str):
        try:
            self.fs.collection("commands").document(cmd_id).delete()
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
                # FIX: Bắt cả ADDED và MODIFIED
                # settings.js dùng updateDoc (MODIFIED) cho cancel_register
                if change.type.name in ("ADDED", "MODIFIED"):
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
#  UPLINK STREAM (Producer) — FIX A1
#  Chỉ subscribe "mqtt_inbound" cho sensor RTDB.
#  "realtime_data" không còn trigger RTDB write.
# ─────────────────────────────────────────────
class UplinkStream(threading.Thread):
    """
    [v2.2] FIX A1: Single Writer Pattern cho RTDB sensor.

    Trước đây subscribe cả "realtime_data" VÀ "mqtt_inbound" → 2 lần ghi/reading.
    Bây giờ:
      - Sensor RTDB: CHỈ từ "mqtt_inbound" (MessageBus MQTT envelope)
      - "realtime_data": chỉ dùng cho alert, device status thông qua SocketIO —
        KHÔNG ghi RTDB sensor nữa.

    Channels:
      - mqtt_inbound    → sensor → RTDB (single writer, không trùng lặp)
      - device_status   → On-Change → Firestore (feedback từ ESP32)
      - safety_alert    → Firestore system_alerts
      - wifi_status     → Firestore system_status/wifi
      - command_ack     → Firestore commands (ACK/delete)
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

    def is_alive(self):
        return super().is_alive()

    def run(self):
        pubsub = self.r.pubsub()
        # FIX A1: Không subscribe "realtime_data" cho sensor RTDB
        pubsub.subscribe(
            "mqtt_inbound",             # Sensor realtime → RTDB (single writer)
            CHANNEL_DEVICE,             # device_status → Firestore (On-Change)
            CHANNEL_ALERT,              # safety_alert → Firestore alerts
            CHANNEL_WIFI,               # wifi_status → Firestore
            CHANNEL_COMMAND_ACK,        # command_ack → Firestore
            "rfid_enrollment_result",   # [FIX] Enrollment result → Firestore commands/entrance_register
        )
        logger.info("[Uplink] Stream started (FIX A1: mqtt_inbound only for RTDB sensor)")

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
            if channel == "mqtt_inbound":
                # FIX A1: Đây là nguồn SENSOR DUY NHẤT ghi lên RTDB
                self._on_mqtt_inbound(payload)
            elif channel == CHANNEL_DEVICE:
                self._on_device(payload)
            elif channel == CHANNEL_ALERT:
                self._on_alert(payload)
            elif channel == CHANNEL_WIFI:
                self._on_wifi(payload)
            elif channel == CHANNEL_COMMAND_ACK:
                self._on_command_ack(payload)
            elif channel == "rfid_enrollment_result":
                self._on_rfid_enrollment_result(payload)
        except Exception as e:
            logger.error("[Uplink] Handle [%s] error: %s", channel, e)

    def _on_mqtt_inbound(self, envelope: dict):
        """
        FIX A1: MQTT envelope từ MessageBus là nguồn SENSOR DUY NHẤT ghi RTDB.
        Format: {topic: "home/{room}/sensors", payload: {...sensor data...}, ts: float}
        """
        topic   = envelope.get("topic", "")
        payload = envelope.get("payload", {})
        ts      = envelope.get("ts", time.time())
        parts   = topic.split("/")
        if len(parts) < 3:
            return
        room_id  = parts[1]
        category = parts[2]
        if category == "sensors" and isinstance(payload, dict):
            clean = {k: v for k, v in payload.items() if v is not None}
            if clean:
                self.rtdb_writer.update_sensor_bulk(room_id, clean, float(ts))
                logger.debug("[Uplink] RTDB sensor write: %s → %s", room_id, list(clean.keys()))

    def _on_device(self, p: dict):
        """
        [v2.2] Device status từ ESP32 feedback → Firestore On-Change.
        Đây là writer DUY NHẤT cho device status — không còn force_update từ Dispatcher.
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

    def _on_rfid_enrollment_result(self, p: dict):
        """
        [FIX] Ghi kết quả enrollment về Firestore commands/entrance_register.
        settings.js đang listen onSnapshot doc này để hiển thị kết quả cho user.
        """
        try:
            self.fs_writer.fs.collection("commands").document("entrance_register").set({
                "status":      p.get("status", "success"),
                "result_type": p.get("result_type", "rfid"),
                "value":       p.get("value", ""),
                "owner_name":  p.get("owner_name", ""),
                "timestamp":   firestore.SERVER_TIMESTAMP,
            }, merge=True)
            logger.info("[Uplink] RFID enrollment result written to Firestore: %s", p.get("value"))
        except Exception as e:
            logger.error("[Uplink] RFID enrollment result write error: %s", e)


# ─────────────────────────────────────────────
#  DOWNLINK STREAM — FIX B1
#  Xóa force_update_device() khỏi _dispatch()
# ─────────────────────────────────────────────
class CommandDispatcher:
    """
    [v2.2] FIX B1: Bỏ force_update_device() trong _dispatch().

    Luồng chuẩn duy nhất:
      Web → Firestore/commands → CommandDispatcher → Redis "device_commands"
      → automation_engine → MQTT → ESP32
      → ESP32 gửi status về → automation_engine → "device_status"
      → UplinkStream → Firestore (update_device On-Change)

    Không còn ghi Firestore TRƯỚC khi ESP32 confirm → không nhấp nháy.
    Web UI cập nhật sau khi ESP32 confirm (~200-500ms delay nhỏ nhưng chính xác).
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
            except Exception as e:
                logger.error("[Downlink] Stop error: %s", e)

    def _dispatch(self, cmd_id: str, data: dict):
        action    = data.get("action", "")
        channel   = self.REDIS_CHANNEL

        # [FIX-WIFI-CHANNEL] Các action wifi đều đi qua "wifi_setup":
        #   - "add_and_connect": kết nối WiFi mới (từ settings.js modal)
        #   - "scan_wifi":       trigger scan danh sách WiFi xung quanh
        # network_watchdog.py subscribe "wifi_setup" và xử lý cả 2 action này.
        if action in ("add_and_connect", "scan_wifi"):
            channel = "wifi_setup"
        elif action in ("start_register", "cancel_register"):
            channel = "rfid_register"
        elif action == "smart_mute_alert":
            # [SMART-MUTE] Web gửi smart_mute qua Firestore commands
            # → forward lên Redis alert_commands để safety_watchdog xử lý
            channel = "alert_commands"

        room_id   = (data.get("room") or data.get("roomId") or data.get("room_id") or "")
        device_id = (data.get("device_id") or data.get("deviceId") or "")

        # [SMART-MUTE] Điều chỉnh payload cho alert_commands channel
        if action == "smart_mute_alert":
            msg = {
                "action":     "smart_mute",
                "room_id":    room_id,
                "alert_type": data.get("alert_type", "gas"),
            }
        else:
            msg = {
                "room":       room_id,
                "device_id":  device_id,
                "cmd_id":     cmd_id,
                "action":     action,
                "is_on":      data.get("isOn", action == "turn_on"),
                "source":     "web",
                "payload":    data.get("payload", {}),
                "owner_name": data.get("owner_name", "Thẻ mới"),
                "target":     data.get("target", ""),
                "ssid":       data.get("ssid", ""),
                "password":   data.get("password", ""),
            }

        try:
            self.r.publish(channel, json.dumps(msg))
            logger.info("[Downlink] DISPATCH: %s [%s] → Redis[%s]", cmd_id, action, channel)

            # FIX B1: KHÔNG còn gọi force_update_device() ở đây.

            # FIX RFID: Với lệnh RFID (entrance_register doc), KHÔNG xóa doc.
            # settings.js dùng onSnapshot trên commands/entrance_register để nhận kết quả.
            # Nếu xóa → listener nhận doc-not-found → UI không update.
            # Giải pháp: chỉ update status=dispatched để tránh re-dispatch,
            # giữ doc sống để nhận enrollment result từ Pi.
            if action in ("start_register", "cancel_register"):
                try:
                    self.fs.collection("commands").document(cmd_id).update({
                        "status": "dispatched",
                    })
                    logger.info("[Downlink] RFID cmd %s → dispatched (kept for onSnapshot)", cmd_id)
                except Exception as ex:
                    logger.warning("[Downlink] Could not mark dispatched: %s", ex)
            else:
                # smart_mute_alert và device commands thường: xóa ngay
                self.fs.collection("commands").document(cmd_id).delete()
                logger.info("[Downlink] CLEANUP: Đã xóa lệnh %s", cmd_id)

        except Exception as e:
            logger.error("[Downlink] Dispatch error cho lệnh %s: %s", cmd_id, e)
            try:
                self.fs_writer.ack_command(cmd_id, "error", str(e))
            except Exception:
                pass


# ─────────────────────────────────────────────
#  BACKGROUND LOOPS
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


def run_automation_schedule_sync_loop(fs_client, r: redis.Redis, stop_event: threading.Event):
    """
    [FIX] Downlink sync: Firestore automations/schedules → SQLite → Redis notify.

    Vấn đề cũ: Web ghi automation/schedule lên Firestore trực tiếp qua roomService.js,
    nhưng automation_engine chỉ đọc từ SQLite. Không có gì sync Firestore → SQLite
    nên ngưỡng nhiệt độ và lịch hẹn giờ không bao giờ có tác dụng.

    Fix: Loop này poll Firestore mỗi 30s, so sánh với SQLite, sync nếu có thay đổi,
    sau đó publish Redis channel để automation_engine reload cache.
    """
    logger.info("[AutoSync] Automation/Schedule sync loop started (interval: 30s)")
    SYNC_INTERVAL = 30

    def get_sqlite_conn():
        import sqlite3 as _sqlite3
        conn = _sqlite3.connect(SQLITE_PATH, timeout=10)
        conn.row_factory = _sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    while not stop_event.is_set():
        stop_event.wait(timeout=SYNC_INTERVAL)
        if stop_event.is_set():
            break
        try:
            # ── 1. Sync Automations ──────────────────────────────────────
            auto_docs = fs_client.collection("automations").get()
            conn = get_sqlite_conn()
            changed_auto = False
            try:
                seen_rooms = set()
                for doc_snap in auto_docs:
                    data    = doc_snap.to_dict()
                    room_id = data.get("roomId") or data.get("room_id", "")
                    if not room_id:
                        continue
                    seen_rooms.add(room_id)
                    # Map Web field names → SQLite column names
                    fan_thresh   = data.get("fanThreshold")   or data.get("fan_threshold")
                    light_thresh = data.get("lightThreshold") or data.get("light_threshold")
                    gas_thresh   = data.get("gasThreshold")   or data.get("gas_threshold")   or 600
                    co2_thresh   = data.get("co2Threshold")   or data.get("co2_threshold")   or 1000
                    enabled      = 1 if data.get("enabled", True) else 0

                    # Upsert vào SQLite (chỉ update nếu có sự thay đổi thực sự)
                    row = conn.execute(
                        "SELECT fan_threshold, light_threshold, gas_threshold, co2_threshold, enabled FROM automations WHERE room_id=?",
                        (room_id,)
                    ).fetchone()
                    if row is None:
                        conn.execute(
                            "INSERT INTO automations (room_id, enabled, fan_threshold, light_threshold, gas_threshold, co2_threshold) VALUES (?,?,?,?,?,?)",
                            (room_id, enabled, fan_thresh, light_thresh, gas_thresh, co2_thresh)
                        )
                        changed_auto = True
                    else:
                        if (row["fan_threshold"]   != fan_thresh   or
                            row["light_threshold"] != light_thresh or
                            row["gas_threshold"]   != gas_thresh   or
                            row["co2_threshold"]   != co2_thresh   or
                            row["enabled"]         != enabled):
                            conn.execute(
                                "UPDATE automations SET enabled=?, fan_threshold=?, light_threshold=?, gas_threshold=?, co2_threshold=? WHERE room_id=?",
                                (enabled, fan_thresh, light_thresh, gas_thresh, co2_thresh, room_id)
                            )
                            changed_auto = True

                # Delete automations no longer in Firestore
                for row in conn.execute("SELECT room_id FROM automations").fetchall():
                    if row["room_id"] not in seen_rooms:
                        conn.execute("DELETE FROM automations WHERE room_id=?", (row["room_id"],))
                        changed_auto = True

                conn.commit()
            finally:
                conn.close()
            if changed_auto:
                logger.info("[AutoSync] Automation rules updated in SQLite from Firestore")
                # Notify automation_engine để reload CACHED_AUTOMATIONS
                r.publish("automation_commands", __import__("json").dumps({"action": "reload_all"}))

            # ── 2. Sync Schedules ────────────────────────────────────────

            # ── 2. Sync Schedules ────────────────────────────────────────
            sched_docs = fs_client.collection("schedules").get()
            conn = get_sqlite_conn()
            changed_sched = False
            try:
                # Lấy tất cả schedules hiện có trong SQLite (key = room+device+time)
                existing = {}
                for row in conn.execute("SELECT * FROM schedules WHERE enabled=1").fetchall():
                    k = f"{row['room_id']}_{row['device_id']}_{row['time']}"
                    existing[k] = dict(row)

                seen_keys = set()
                for doc_snap in sched_docs:
                    data      = doc_snap.to_dict()
                    room_id   = data.get("roomId")   or data.get("room_id", "")
                    device_id = data.get("deviceId") or data.get("device_id", "")
                    time_val  = data.get("time", "")
                    action    = data.get("action", "turn_on")
                    enabled   = 1 if data.get("enabled", True) else 0

                    if not room_id or not device_id or not time_val:
                        continue

                    key = f"{room_id}_{device_id}_{time_val}"
                    seen_keys.add(key)

                    if key not in existing:
                        conn.execute(
                            "INSERT INTO schedules (room_id, device_id, action, time, enabled) VALUES (?,?,?,?,?)",
                            (room_id, device_id, action, time_val, enabled)
                        )
                        changed_sched = True
                    elif existing[key]["action"] != action or existing[key]["enabled"] != enabled:
                        conn.execute(
                            "UPDATE schedules SET action=?, enabled=? WHERE room_id=? AND device_id=? AND time=?",
                            (action, enabled, room_id, device_id, time_val)
                        )
                        changed_sched = True

                # Delete schedules no longer in Firestore
                for key in existing:
                    if key not in seen_keys:
                        room_id, device_id, time_val = key.split('_', 2)
                        conn.execute(
                            "DELETE FROM schedules WHERE room_id=? AND device_id=? AND time=?",
                            (room_id, device_id, time_val)
                        )
                        changed_sched = True

                conn.commit()
            finally:
                conn.close()
            if changed_sched:
                logger.info("[AutoSync] Schedules updated in SQLite from Firestore")
                # Notify automation_engine để reload CACHED_SCHEDULES
                r.publish("schedule_commands", __import__("json").dumps({"action": "reload"}))

        except Exception as e:
            logger.error("[AutoSync] Sync loop error: %s", e)


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────
def main():
    logger.info("=" * 60)
    logger.info("firebase_sync v2.2 starting — project: %s", FIREBASE_PROJECT_ID)
    logger.info("FIX A1: Single Writer RTDB (mqtt_inbound only)")
    logger.info("FIX B1: No force_update — ESP32 feedback is source of truth")
    logger.info("=" * 60)

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

    logger.info("Chạy auto-provisioning...")
    AutoProvisioner(fs_client, rtdb_module, PI_OWNER_UID).provision_all()

    if PI_OWNER_UID:
        fs_writer.sync_rooms_from_sqlite(PI_OWNER_UID)
    else:
        logger.warning("PI_OWNER_UID chưa set. Set PI_OWNER_UID=<firebase_uid> trong .env.")

    stop_event = threading.Event()

    uplink = UplinkStream(rtdb_writer, fs_writer, r)
    uplink.start()

    dispatcher = CommandDispatcher(fs_writer=fs_writer, redis_client=r, db=fs_client)
    dispatcher.start()

    threading.Thread(target=run_sensor_flush_loop, args=(fs_writer, stop_event),
                     daemon=True, name="sensor-flush").start()
    threading.Thread(target=run_heartbeat_loop,    args=(rtdb_writer, stop_event),
                     daemon=True, name="heartbeat").start()
    # [FIX] Sync automation rules và schedules từ Firestore → SQLite
    threading.Thread(target=run_automation_schedule_sync_loop, args=(fs_client, r, stop_event),
                     daemon=True, name="auto-sched-sync").start()

    fs_writer.push_alert(
        alert_type="system",
        message="firebase_sync v2.2 started (FIX A1: single RTDB writer, FIX B1: no force_update)",
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
    main()


if __name__ == "__main__":
    main()
