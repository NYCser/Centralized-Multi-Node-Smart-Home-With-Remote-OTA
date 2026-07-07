"""
workers/firebase_sync.py  — v2.4  (SCHEDULE SYNC FIX: Firestore → SQLite one-way, no override)
════════════════════════════════════════════════════════════════════════════════════════════════
FIXES trong phiên bản này (so với v2.3):

  [FIX-SCHED-3 — CRITICAL] AutoSync override schedule SQLite bằng giá trị cũ từ Firestore
      ─────────────────────────────────────────────
      Root cause v2.3:
        - AutoSync chạy mỗi 30 giây, pull tất cả docs từ Firestore collection "schedules"
        - Khi SQLite có time='11:15' nhưng Firestore có time='14:30' (giá trị cũ)
          → AutoSync UPDATE SQLite về 14:30 → schedule sai hoàn toàn
        - updatedAt comparison trong v2.3 patch không đủ vì migration lúc startup
          đã set updatedAt = SERVER_TIMESTAMP mới cho tất cả docs → Firestore luôn
          "mới hơn" SQLite dù time cũ hơn

      Fix v2.4:
        - AutoSync CHỈ INSERT device mới chưa có trong SQLite
        - AutoSync KHÔNG UPDATE schedule đã tồn tại trong SQLite
        - Việc UPDATE schedule chỉ xảy ra khi:
            (a) Web dashboard ghi Firestore doc với updatedAt MỚI HƠN gateway_start_time
            (b) AutoSync so sánh updatedAt với GATEWAY_START_TIME — chỉ update nếu
                Firestore doc được tạo/sửa SAU khi gateway start lần này
        - DELETE vẫn hoạt động bình thường (device bị xóa khỏi Firestore → xóa SQLite)

  [FIX-SCHED-4 — HIGH] migrate_schedule_doc_ids chạy lại mỗi startup không cần thiết
      ─────────────────────────────────────────────
      Fix: Chỉ chạy migration nếu thực sự có doc với suffix cũ (_turn_off, _turn_on).
           Nếu collection trống hoặc tất cả doc đã đúng format → skip hoàn toàn,
           không gọi batch.commit() → tiết kiệm Firestore quota.

  [Giữ nguyên từ v2.3]
      FIX-SCHED-1 (1-device-1-schedule key), FIX-SCHED-2 (doc ID migration),
      FIX A1 (Single Writer RTDB), FIX B1 (No force_update),
      On-Change device cache, auto-provisioning, sensor history flush, heartbeat.
"""

import json
import logging
import os
import signal
import sqlite3
import threading
import time
from collections import defaultdict
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
REDIS_HOST   = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT   = int(os.getenv("REDIS_PORT", 6379))
DEFAULT_SQLITE_PATH = "/data/smarthome.db"
FALLBACK_SQLITE_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "storage", "smarthome.db")
)


def _resolve_sqlite_path() -> str:
    configured_path = os.getenv("SQLITE_PATH")
    if configured_path:
        return configured_path

    try:
        os.makedirs(os.path.dirname(DEFAULT_SQLITE_PATH), exist_ok=True)
        with sqlite3.connect(DEFAULT_SQLITE_PATH, timeout=10):
            return DEFAULT_SQLITE_PATH
    except Exception:
        os.makedirs(os.path.dirname(FALLBACK_SQLITE_PATH), exist_ok=True)
        return FALLBACK_SQLITE_PATH


SQLITE_PATH = _resolve_sqlite_path()
PI_OWNER_UID = os.getenv("PI_OWNER_UID", "")

# Thời điểm gateway start — dùng để filter Firestore docs mới hơn
GATEWAY_START_TIME = datetime.now(tz=timezone.utc)

# Redis channels
CHANNEL_DEVICE      = "device_status"
CHANNEL_ALERT       = "safety_alert"
CHANNEL_WIFI        = "wifi_status"
CHANNEL_COMMAND_ACK = "command_ack"

SENSOR_FLUSH_INTERVAL_S = 60

DEFAULT_ROOMS = [
    {"id": "bedroom_01",     "name": "Phòng Ngủ",   "icon": "bed"},
    {"id": "kitchen_01",     "name": "Nhà Bếp",     "icon": "utensils"},
    {"id": "living_room_01", "name": "Phòng Khách", "icon": "sofa"},
]


# ─────────────────────────────────────────────
#  FIREBASE INIT
# ─────────────────────────────────────────────
_FIREBASE_INIT_LOCK = threading.Lock()


def init_firebase():
    try:
        firebase_admin.get_app()
        return firestore.client(), rtdb
    except ValueError:
        pass

    with _FIREBASE_INIT_LOCK:
        try:
            firebase_admin.get_app()
            return firestore.client(), rtdb
        except ValueError:
            options = {"projectId": FIREBASE_PROJECT_ID}
            db_url  = FIREBASE_DB_URL or \
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
            except ValueError as e:
                if "default Firebase app already exists" in str(e):
                    logger.warning(
                        "Firebase app đã được khởi tạo đồng thời, tái sử dụng app hiện tại."
                    )
                else:
                    logger.critical("Không thể khởi tạo Firebase: %s", e)
                    raise
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
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    if hasattr(obj, "to_datetime"):
        return obj.to_datetime().isoformat()
    return str(obj)


def _fs_ts_to_datetime(ts_val) -> Optional[datetime]:
    """Convert Firestore timestamp (DatetimeWithNanoseconds hoặc datetime) sang datetime UTC."""
    if ts_val is None:
        return None
    try:
        if hasattr(ts_val, "tzinfo") and ts_val.tzinfo is not None:
            return ts_val.astimezone(timezone.utc)
        if hasattr(ts_val, "timestamp"):
            return datetime.fromtimestamp(ts_val.timestamp(), tz=timezone.utc)
    except Exception:
        pass
    return None


# ─────────────────────────────────────────────
#  [FIX-SCHED-2 + FIX-SCHED-4] FIRESTORE SCHEDULE MIGRATION
#  Chỉ chạy nếu thực sự có doc với suffix cũ — không commit nếu không cần.
# ─────────────────────────────────────────────
def migrate_schedule_doc_ids(fs_client) -> int:
    """
    Gộp tất cả schedule docs của cùng 1 device về 1 doc duy nhất.
    [FIX-SCHED-4]: Skip hoàn toàn nếu không có doc nào còn suffix cũ.
    """
    try:
        all_docs = fs_client.collection("schedules").get()
        if not all_docs:
            logger.info("[Migration] Không có schedule nào trong Firestore — bỏ qua")
            return 0

        # Kiểm tra nhanh: có doc nào còn suffix cũ không?
        has_any_old = any(
            doc.id.endswith("_turn_off") or doc.id.endswith("_turn_on")
            for doc in all_docs
        )
        if not has_any_old:
            logger.info("[Migration] Tất cả doc đã đúng format mới — bỏ qua migration")
            return 0

        # Group theo device key
        groups: Dict[str, list] = defaultdict(list)
        for doc in all_docs:
            doc_id    = doc.id
            data      = doc.to_dict()
            canonical = doc_id
            for suffix in ("_turn_off", "_turn_on"):
                if canonical.endswith(suffix):
                    canonical = canonical[: -len(suffix)]
                    break
            groups[canonical].append((doc_id, data))

        ops_count = 0
        for canonical_id, docs in groups.items():
            has_old_suffix = any(
                did.endswith("_turn_off") or did.endswith("_turn_on")
                for did, _ in docs
            )
            if not has_old_suffix and len(docs) == 1:
                continue

            # Chọn winner: ưu tiên turn_off, fallback doc cuối
            turn_off_docs = [(did, d) for did, d in docs if d.get("action") == "turn_off"]
            winner_id, winner_data = turn_off_docs[0] if turn_off_docs else docs[-1]

            new_data = {
                "roomId":    winner_data.get("roomId")   or winner_data.get("room_id", ""),
                "deviceId":  winner_data.get("deviceId") or winner_data.get("device_id", ""),
                "action":    "turn_off",
                "time":      winner_data.get("time", ""),
                "enabled":   winner_data.get("enabled", True),
                "updatedAt": firestore.SERVER_TIMESTAMP,
            }

            batch   = fs_client.batch()
            new_ref = fs_client.collection("schedules").document(canonical_id)
            batch.set(new_ref, new_data)
            ops_count += 1

            for old_id, _ in docs:
                if old_id != canonical_id:
                    batch.delete(fs_client.collection("schedules").document(old_id))
                    ops_count += 1
                    logger.info("[Migration] Xóa doc cũ: %s", old_id)

            batch.commit()
            logger.info("[Migration] ✓ %s → turn_off @ %s (từ %d doc)",
                        canonical_id, new_data["time"], len(docs))

        logger.info("[Migration] Hoàn tất: %d operations", ops_count)
        return ops_count

    except Exception as e:
        logger.error("[Migration] Lỗi migration schedule: %s", e)
        return 0


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
            doc_ref   = self.fs.collection("rooms").document(room_id)
            doc_snap  = doc_ref.get()
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
                "online":    False,
                "last_seen": datetime.now(tz=timezone.utc).isoformat(),
            })
        except Exception as e:
            logger.warning("RTDB set_room_offline [%s] error: %s", room_id, e)

    def heartbeat(self, room_ids: list):
        try:
            self.rtdb.reference("gateway_status").set({
                "online":    True,
                "last_seen": datetime.now(tz=timezone.utc).isoformat(),
                "rooms":     room_ids,
            })
        except Exception as e:
            logger.warning("RTDB heartbeat error: %s", e)


# ─────────────────────────────────────────────
#  FIRESTORE WRITER
# ─────────────────────────────────────────────
class FirestoreWriter:
    def __init__(self, fs_client):
        self.fs = fs_client
        self._device_state_cache: Dict[str, dict] = {}

    def update_device(self, room_id: str, device_id: str, payload: dict):
        key    = f"{room_id}_{device_id}"
        is_on  = bool(payload.get("is_on", payload.get("isOn", False)))
        status = payload.get("status", "online")

        cached = self._device_state_cache.get(key)
        if cached is not None:
            if cached.get("isOn") == is_on and cached.get("status") == status:
                return

        try:
            ref = (self.fs.collection("rooms").document(room_id)
                       .collection("devices").document(device_id))

            dev_type = payload.get("type")
            if not dev_type:
                if "fan"   in device_id.lower(): dev_type = "fan"
                elif "light" in device_id.lower(): dev_type = "light"
                else: dev_type = "unknown"

            if dev_type == "fan":
                dev_name = "Quạt"
            elif dev_type == "light":
                dev_name = "Đèn"
            else:
                dev_name = payload.get("name") or device_id

            data = {
                "isOn":      is_on,
                "status":    status,
                "details":   "Đang bật" if is_on else "Đã tắt",
                "type":      dev_type,
                "name":      dev_name,
                "updatedAt": firestore.SERVER_TIMESTAMP,
            }
            ref.set(data, merge=True)
            self._device_state_cache[key] = {"isOn": is_on, "status": status}
            logger.info("Firestore sync [%s/%s] → %s | Name: %s | Type: %s",
                        room_id, device_id, ("ON" if is_on else "OFF"), dev_name, dev_type)
        except Exception as e:
            logger.error("Firestore update_device [%s/%s] error: %s", room_id, device_id, e)

    def invalidate_device_cache(self, room_id: str, device_id: str):
        self._device_state_cache.pop(f"{room_id}_{device_id}", None)

    def push_alert(self, alert_type: str, message: str,
                   level: str = "warning", location: str = ""):
        try:
            self.fs.collection("system_alerts").add({
                "type":       alert_type, "message":   message,
                "level":      level,      "location":  location,
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
#  UPLINK STREAM
# ─────────────────────────────────────────────
class UplinkStream(threading.Thread):
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
            "mqtt_inbound",
            CHANNEL_DEVICE,
            CHANNEL_ALERT,
            CHANNEL_WIFI,
            CHANNEL_COMMAND_ACK,
            "rfid_enrollment_result",
        )
        logger.info("[Uplink] Stream started")

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

    def _on_rfid_enrollment_result(self, p: dict):
        uid         = p.get("value", "")
        status      = p.get("status", "success")
        owner_name  = p.get("owner_name", "Thẻ mới")
        result_type = p.get("result_type", "combo")
        try:
            self.fs_writer.fs.collection("commands").document("entrance_register").set({
                "status":      status,
                "result_type": result_type,
                "value":       uid,
                "owner_name":  owner_name,
                "timestamp":   firestore.SERVER_TIMESTAMP,
            }, merge=True)
            if status == "success" and uid:
                self.fs_writer.fs.collection("rfid_cards").document(uid).set({
                    "uid":        uid,
                    "name":       owner_name,
                    "owner_name": owner_name,
                    "createdAt":  firestore.SERVER_TIMESTAMP,
                    "is_active":  True,
                    "has_finger": True,
                }, merge=True)
                try:
                    self.fs_writer.fs.collection("system_alerts").add({
                        "type":       "rfid_enroll",
                        "message":    f"Đăng ký thành công | Người: {owner_name} | UID: {uid} | Thẻ + Vân tay",
                        "room_id":    "living_room_01",
                        "level":      "info",
                        "isResolved": False,
                        "timestamp":  firestore.SERVER_TIMESTAMP,
                        "metadata":   {"uid": uid, "owner_name": owner_name, "result_type": result_type},
                    })
                except Exception as ae:
                    logger.warning("[Uplink] Enrollment alert write failed: %s", ae)
            logger.info("[Uplink] RFID enrollment result written: %s", uid)
        except Exception as e:
            logger.error("[Uplink] RFID enrollment result write error: %s", e)


# ─────────────────────────────────────────────
#  DOWNLINK STREAM
# ─────────────────────────────────────────────
class CommandDispatcher:
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
        action  = data.get("action", "")
        channel = self.REDIS_CHANNEL

        if action in ("add_and_connect", "scan_wifi"):
            channel = "wifi_setup"
        elif action in ("start_register", "cancel_register"):
            channel = "rfid_register"
        elif action == "delete_rfid":
            channel = "rfid_commands"
        elif action == "clear_all_rfid":
            channel = "rfid_commands"
        elif action == "smart_mute_alert":
            channel = "alert_commands"

        room_id   = data.get("room") or data.get("roomId") or data.get("room_id") or ""
        device_id = data.get("device_id") or data.get("deviceId") or ""

        if action == "smart_mute_alert":
            msg = {"action": "smart_mute", "room_id": room_id,
                   "alert_type": data.get("alert_type", "gas")}
        elif action == "delete_rfid":
            msg = {"action": "delete", "uid": data.get("uid", "")}
        elif action == "clear_all_rfid":
            msg = {"action": "clear_all"}
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
                "uid":        data.get("uid", ""),
            }

        try:
            self.r.publish(channel, json.dumps(msg))
            logger.info("[Downlink] DISPATCH: %s [%s] → Redis[%s]", cmd_id, action, channel)

            if action in ("start_register", "cancel_register"):
                try:
                    self.fs.collection("commands").document(cmd_id).update({"status": "dispatched"})
                except Exception as ex:
                    logger.warning("[Downlink] Could not mark dispatched: %s", ex)
            else:
                self.fs.collection("commands").document(cmd_id).delete()

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
    [v2.4] FIX-SCHED-3: AutoSync KHÔNG override schedule đã có trong SQLite.

    Logic mới:
      - INSERT: device chưa có trong SQLite → insert từ Firestore (bình thường)
      - UPDATE: CHỈ update nếu Firestore doc có updatedAt SAU GATEWAY_START_TIME
                → tức là user đã thay đổi schedule trên web SAU KHI gateway start
      - DELETE: device không còn trong Firestore → xóa khỏi SQLite (bình thường)

    Điều này đảm bảo:
      - Schedule được set thủ công (sqlite3 UPDATE) không bị AutoSync ghi đè
      - Schedule set qua web dashboard vẫn sync đúng (vì web ghi updatedAt mới)
    """
    logger.info("[AutoSync] Automation/Schedule sync loop started (interval: 30s)")
    logger.info("[AutoSync] GATEWAY_START_TIME = %s", GATEWAY_START_TIME.isoformat())
    SYNC_INTERVAL = 30

    def get_sqlite_conn():
        conn = sqlite3.connect(SQLITE_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    while not stop_event.is_set():
        stop_event.wait(timeout=SYNC_INTERVAL)
        if stop_event.is_set():
            break
        try:
            # ── 1. Sync Automations (không đổi) ─────────────────────────
            auto_docs    = fs_client.collection("automations").get()
            conn         = get_sqlite_conn()
            changed_auto = False
            try:
                seen_rooms = set()
                for doc_snap in auto_docs:
                    data    = doc_snap.to_dict()
                    room_id = data.get("roomId") or data.get("room_id", "")
                    if not room_id:
                        continue
                    seen_rooms.add(room_id)
                    fan_thresh   = data.get("fanThreshold")   or data.get("fan_threshold")
                    light_thresh = data.get("lightThreshold") or data.get("light_threshold")
                    gas_thresh   = data.get("gasThreshold")   or data.get("gas_threshold")   or 600
                    co2_thresh   = data.get("co2Threshold")   or data.get("co2_threshold")   or 1000
                    enabled      = 1 if data.get("enabled", True) else 0

                    row = conn.execute(
                        "SELECT fan_threshold, light_threshold, gas_threshold, co2_threshold, enabled "
                        "FROM automations WHERE room_id=?", (room_id,)
                    ).fetchone()
                    if row is None:
                        conn.execute(
                            "INSERT INTO automations (room_id, enabled, fan_threshold, light_threshold, gas_threshold, co2_threshold) "
                            "VALUES (?,?,?,?,?,?)",
                            (room_id, enabled, fan_thresh, light_thresh, gas_thresh, co2_thresh)
                        )
                        changed_auto = True
                    elif (row["fan_threshold"]   != fan_thresh   or
                          row["light_threshold"] != light_thresh or
                          row["gas_threshold"]   != gas_thresh   or
                          row["co2_threshold"]   != co2_thresh   or
                          row["enabled"]         != enabled):
                        conn.execute(
                            "UPDATE automations SET enabled=?, fan_threshold=?, light_threshold=?, "
                            "gas_threshold=?, co2_threshold=? WHERE room_id=?",
                            (enabled, fan_thresh, light_thresh, gas_thresh, co2_thresh, room_id)
                        )
                        changed_auto = True

                for row in conn.execute("SELECT room_id FROM automations").fetchall():
                    if row["room_id"] not in seen_rooms:
                        conn.execute("DELETE FROM automations WHERE room_id=?", (row["room_id"],))
                        changed_auto = True

                conn.commit()
            finally:
                conn.close()

            if changed_auto:
                logger.info("[AutoSync] Automation rules updated in SQLite from Firestore")
                r.publish("automation_commands", json.dumps({"action": "reload_all"}))

            # ── 2. Sync Schedules — FIX-SCHED-3 ─────────────────────────
            sched_docs    = fs_client.collection("schedules").get()
            conn          = get_sqlite_conn()
            changed_sched = False
            try:
                # Đọc SQLite hiện tại
                existing: Dict[str, dict] = {}
                for row in conn.execute("SELECT * FROM schedules WHERE enabled=1").fetchall():
                    k = f"{row['room_id']}__{row['device_id']}"
                    existing[k] = dict(row)

                # Group Firestore docs theo device — lấy doc mới nhất per device
                fs_by_device: Dict[str, dict] = {}
                for doc_snap in sched_docs:
                    data      = doc_snap.to_dict()
                    room_id   = data.get("roomId")   or data.get("room_id", "")
                    device_id = data.get("deviceId") or data.get("device_id", "")
                    time_val  = data.get("time", "")
                    action    = data.get("action", "turn_off")
                    enabled   = 1 if data.get("enabled", True) else 0
                    updated_at = _fs_ts_to_datetime(
                        data.get("updatedAt") or data.get("createdAt")
                    )

                    if not room_id or not device_id or not time_val:
                        continue

                    dev_key = f"{room_id}__{device_id}"

                    # Nếu đã có doc này rồi → giữ doc mới nhất theo updatedAt
                    if dev_key in fs_by_device:
                        existing_updated = fs_by_device[dev_key].get("updatedAt")
                        if existing_updated and updated_at:
                            if updated_at <= existing_updated:
                                continue  # doc cũ hơn, bỏ qua
                        elif fs_by_device[dev_key].get("action") == "turn_off":
                            continue  # fallback: giữ turn_off

                    fs_by_device[dev_key] = {
                        "room_id":    room_id,
                        "device_id":  device_id,
                        "time":       time_val,
                        "action":     action,
                        "enabled":    enabled,
                        "updatedAt":  updated_at,
                    }

                seen_keys = set(fs_by_device.keys())

                for dev_key, fs_data in fs_by_device.items():
                    room_id    = fs_data["room_id"]
                    device_id  = fs_data["device_id"]
                    time_val   = fs_data["time"]
                    action     = fs_data["action"]
                    enabled    = fs_data["enabled"]
                    updated_at = fs_data["updatedAt"]  # datetime UTC hoặc None

                    if dev_key not in existing:
                        # Device chưa có trong SQLite → INSERT (luôn luôn)
                        conn.execute(
                            "INSERT INTO schedules (room_id, device_id, action, time, enabled) "
                            "VALUES (?,?,?,?,?)",
                            (room_id, device_id, action, time_val, enabled)
                        )
                        changed_sched = True
                        logger.info("[AutoSync] Schedule INSERT: %s/%s → %s @ %s",
                                    room_id, device_id, action, time_val)
                    else:
                        old = existing[dev_key]
                        # [FIX-SCHED-3] Chỉ UPDATE nếu Firestore doc được sửa SAU gateway start
                        # → user đã thay đổi trên web sau khi gateway chạy
                        is_new_from_web = (
                            updated_at is not None and
                            updated_at > GATEWAY_START_TIME
                        )
                        if is_new_from_web:
                            if (old["time"]    != time_val or
                                old["action"]  != action   or
                                old["enabled"] != enabled):
                                conn.execute(
                                    "UPDATE schedules SET action=?, time=?, enabled=? "
                                    "WHERE room_id=? AND device_id=?",
                                    (action, time_val, enabled, room_id, device_id)
                                )
                                changed_sched = True
                                logger.info(
                                    "[AutoSync] Schedule UPDATE (web): %s/%s → %s @ %s (was %s @ %s)",
                                    room_id, device_id, action, time_val,
                                    old["action"], old["time"]
                                )
                        else:
                            # Doc cũ hơn gateway start → bỏ qua, không override SQLite
                            logger.debug(
                                "[AutoSync] Schedule SKIP (old doc): %s/%s fs=%s sqlite=%s",
                                room_id, device_id, time_val, old["time"]
                            )

                # [PATCH] DELETE tắt tạm thời — chỉ DELETE nếu Firestore có ít nhất 1 doc hợp lệ
                if len(fs_by_device) > 0:
                    for dev_key in existing:
                        if dev_key not in seen_keys:
                            parts     = dev_key.split("__", 1)
                            room_id   = parts[0]
                            device_id = parts[1] if len(parts) > 1 else ""
                            conn.execute(
                                "DELETE FROM schedules WHERE room_id=? AND device_id=?",
                                (room_id, device_id)
                            )
                            changed_sched = True
                            logger.info("[AutoSync] Schedule DELETE: %s/%s (không còn trong Firestore)",
                                        room_id, device_id)
                else:
                    logger.warning("[AutoSync] Schedule DELETE skipped — Firestore trả về 0 docs (quota/empty)")

                conn.commit()
            finally:
                conn.close()

            if changed_sched:
                logger.info("[AutoSync] Schedules updated in SQLite from Firestore")
                r.publish("schedule_commands", json.dumps({"action": "reload"}))

        except Exception as e:
            logger.error("[AutoSync] Sync loop error: %s", e)


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────
def main():
    logger.info("=" * 60)
    logger.info("firebase_sync v2.4 starting — project: %s", FIREBASE_PROJECT_ID)
    logger.info("FIX-SCHED-3: AutoSync không override schedule SQLite bằng Firestore doc cũ")
    logger.info("FIX-SCHED-4: Migration skip nếu không có doc suffix cũ")
    logger.info("FIX A1: Single Writer RTDB | FIX B1: No force_update")
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

    # Migration — chỉ chạy nếu có doc suffix cũ (FIX-SCHED-4)
    logger.info("Chạy migration Firestore schedule doc IDs...")
    ops = migrate_schedule_doc_ids(fs_client)
    logger.info("Migration xong: %d operations", ops)

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
    threading.Thread(target=run_heartbeat_loop, args=(rtdb_writer, stop_event),
                     daemon=True, name="heartbeat").start()
    threading.Thread(target=run_automation_schedule_sync_loop, args=(fs_client, r, stop_event),
                     daemon=True, name="auto-sched-sync").start()

    fs_writer.push_alert(
        alert_type="system",
        message="firebase_sync v2.4 started (FIX-SCHED-3: no override, FIX-SCHED-4: skip migration)",
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