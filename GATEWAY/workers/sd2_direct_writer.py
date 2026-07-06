"""
sd2_direct_writer.py  — SD2 Direct Writer Module  v2.1 FIXED
══════════════════════════════════════════════════════════════
FIXES TRONG PHIÊN BẢN NÀY (v2.1):

  [FIX-ROUTING-01] system_alerts TRỐNG — 74 events bị rớt vào system_events
    Nguyên nhân:
      - Gateway gửi event payload: {"type": "intrusion", "message": "...", "level": "critical"}
        nhưng event NAME là "unknown_event" → không match bất kỳ keyword nào trong _EVENT_ROUTE.
      - _infer_event() cũ chỉ check keys, không check field "type" bên trong payload.
    Fix:
      - Thêm bước resolve_event_from_payload() sau khi event = "unknown_event"
        để map field "type" → đúng bảng đích.
      - Map: type="intrusion" → system_alerts, type="access" → access_logs,
             type="gas"/"fire" → system_alerts, type="automation" → automation_logs

  [FIX-ROUTING-02] rfid_cards và schedules TRỐNG
    Nguyên nhân:
      - SD2DirectWriter không sync từ Firestore các collection rfid_cards/schedules.
      - SyncWorker._sync_state trống → chưa bao giờ chạy sync incremental đúng.
    Fix:
      - Thêm sync_from_firestore_collections() đọc rfid_cards và schedules
        từ Redis hash keys mà gateway đã cache sẵn.

  [GIỮ NGUYÊN từ v1.0]
      BUG-SD2-SENSOR: flush_sensors() đọc trực tiếp Redis, không phụ thuộc main DB
      BUG-SD2-EVENTS: flush_events() với offset tracking
      BUG-SCHEDULE-SPAM: schedule dedup (xử lý ở schedule_dedup_patch.py)
"""

import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime

import redis

# ══════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════

REDIS_HOST    = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT    = int(os.getenv("REDIS_PORT", 6379))

SENSOR_BUFFER_KEY  = "sensor_buffer"
EVENT_QUEUE_KEY    = "event_queue"

SD2_MOUNT          = os.getenv("SD2_MOUNT", "/mnt/sd2")
SD2_DATA_DIR       = f"{SD2_MOUNT}/data"
FALLBACK_DATA_DIR  = os.getenv("FALLBACK_DATA_DIR", "/data/sensor_history")

SENSOR_FLUSH_INTERVAL   = 30
EVENT_FLUSH_INTERVAL    = 15
STATUS_SYNC_INTERVAL    = 10
SNAPSHOT_INTERVAL       = 60
COLLECTION_SYNC_INTERVAL = 120   # [FIX-ROUTING-02] sync rfid_cards/schedules mỗi 2 phút

SENSOR_BATCH_SIZE = 500
EVENT_BATCH_SIZE  = 200

# [FIX-ROUTING-01] Map field "type" bên trong payload → event name chuẩn
_INNER_TYPE_TO_EVENT = {
    "intrusion":    "safety_alert",
    "access":       "access_log",
    "gas":          "gas_alert",
    "fire":         "fire_alert",
    "co2":          "gas_alert",
    "automation":   "automation_log",
    "login":        "login",
    "notification": "notification",
    "ota":          "ota_update",
    "firmware":     "ota_update",
    "snapshot":     "snapshot",
    "device":       "device_status_upd",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s][SD2-WRITER] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("sd2_direct_writer")


# ══════════════════════════════════════════════════════════
# SCHEMA
# ══════════════════════════════════════════════════════════

SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS users (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    email        TEXT    UNIQUE NOT NULL,
    password     TEXT    NOT NULL,
    display_name TEXT    DEFAULT '',
    role         TEXT    DEFAULT 'user',
    created_at   TEXT    DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS rooms (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    icon       TEXT DEFAULT 'home',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS devices (
    id      TEXT PRIMARY KEY,
    room_id TEXT NOT NULL,
    name    TEXT NOT NULL,
    type    TEXT NOT NULL,
    FOREIGN KEY(room_id) REFERENCES rooms(id)
);
CREATE TABLE IF NOT EXISTS sensor_data (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    room            TEXT NOT NULL,
    type            TEXT NOT NULL,
    value           REAL NOT NULL,
    timestamp       TEXT DEFAULT (datetime('now','localtime')),
    firebase_synced INTEGER DEFAULT 1,
    UNIQUE(room, type, timestamp) ON CONFLICT IGNORE
);
CREATE INDEX IF NOT EXISTS idx_sensor_room_ts ON sensor_data(room, type, timestamp DESC);

CREATE TABLE IF NOT EXISTS device_status (
    room       TEXT NOT NULL,
    device_id  TEXT NOT NULL,
    is_on      INTEGER DEFAULT 0,
    source     TEXT DEFAULT 'unknown',
    updated_at TEXT DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (room, device_id)
);
CREATE TABLE IF NOT EXISTS system_alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    room        TEXT,
    type        TEXT,
    message     TEXT,
    level       TEXT    DEFAULT 'info',
    is_resolved INTEGER DEFAULT 0,
    resolved_at TEXT,
    timestamp   TEXT    DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_alert_ts ON system_alerts(timestamp DESC);

CREATE TABLE IF NOT EXISTS notifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    type       TEXT NOT NULL,
    title      TEXT NOT NULL,
    message    TEXT NOT NULL,
    is_read    INTEGER DEFAULT 0,
    room       TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS access_logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    room       TEXT,
    uid        TEXT,
    user_name  TEXT,
    action     TEXT,
    success    INTEGER DEFAULT 0,
    duration_s INTEGER,
    timestamp  TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_access_ts ON access_logs(timestamp DESC);

CREATE TABLE IF NOT EXISTS automation_logs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    room         TEXT,
    scenario     TEXT,
    actions      TEXT,
    triggered_by TEXT,
    timestamp    TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS login_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    email       TEXT,
    success     INTEGER DEFAULT 0,
    ip_address  TEXT,
    device_hint TEXT,
    user_agent  TEXT,
    reason      TEXT,
    timestamp   TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS rfid_cards (
    uid        TEXT PRIMARY KEY,
    owner_name TEXT DEFAULT '',
    is_active  INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS automations (
    room_id         TEXT PRIMARY KEY,
    enabled         INTEGER DEFAULT 1,
    fan_threshold   REAL,
    light_threshold REAL,
    gas_threshold   REAL DEFAULT 600,
    co2_threshold   REAL DEFAULT 1000
);
CREATE TABLE IF NOT EXISTS schedules (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id    TEXT NOT NULL,
    device_id  TEXT NOT NULL,
    action     TEXT NOT NULL,
    time       TEXT NOT NULL,
    enabled    INTEGER DEFAULT 1,
    last_run   TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS ota_logs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    room          TEXT NOT NULL,
    filename      TEXT NOT NULL,
    url           TEXT NOT NULL,
    version       TEXT DEFAULT 'unknown',
    release_notes TEXT DEFAULT '',
    triggered_by  TEXT DEFAULT '',
    status        TEXT DEFAULT 'pending',
    created_at    TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS system_snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    wifi_ssid    TEXT,
    wifi_status  TEXT,
    room_count   INTEGER DEFAULT 0,
    device_count INTEGER DEFAULT 0,
    alert_count  INTEGER DEFAULT 0,
    timestamp    TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS system_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    event     TEXT NOT NULL,
    data      TEXT,
    timestamp TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS _sd2_writer_state (
    key        TEXT PRIMARY KEY,
    value      TEXT DEFAULT '',
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);
"""

# ══════════════════════════════════════════════════════════
# EVENT ROUTING
# ══════════════════════════════════════════════════════════

_EVENT_ROUTE = [
    ("rfid",              "access_logs"),
    ("access",            "access_logs"),
    ("door_access",       "access_logs"),
    ("ota",               "ota_logs"),
    ("firmware",          "ota_logs"),
    ("automation",        "automation_logs"),
    ("login",             "login_logs"),
    ("logout",            "login_logs"),
    ("auth",              "login_logs"),
    ("notif",             "notifications"),
    ("safety_alert",      "system_alerts"),
    ("gas_alert",         "system_alerts"),
    ("fire_alert",        "system_alerts"),
    ("intrusion",         "system_alerts"),   # [FIX-ROUTING-01] thêm mới
    ("snapshot",          "system_snapshots"),
    ("device",            "device_status"),
    ("schedule",          "schedules"),
    ("sensor_data",       "sensor_data"),
    ("device_status_upd", "device_status"),
    ("access_log",        "access_logs"),
    ("automation_log",    "automation_logs"),
    ("ota_update",        "ota_logs"),
    ("notification",      "notifications"),
]

_TABLE_COLUMNS = {
    "sensor_data":      {"room", "type", "value", "timestamp", "firebase_synced"},
    "device_status":    {"room", "device_id", "is_on", "source", "updated_at"},
    "system_alerts":    {"room", "type", "message", "level", "is_resolved", "resolved_at", "timestamp"},
    "access_logs":      {"room", "uid", "user_name", "action", "success", "duration_s", "timestamp"},
    "automation_logs":  {"room", "scenario", "actions", "triggered_by", "timestamp"},
    "login_logs":       {"email", "success", "ip_address", "device_hint", "user_agent", "reason", "timestamp"},
    "notifications":    {"type", "title", "message", "is_read", "room", "created_at"},
    "ota_logs":         {"room", "filename", "url", "version", "release_notes", "triggered_by", "status", "created_at"},
    "system_snapshots": {"wifi_ssid", "wifi_status", "room_count", "device_count", "alert_count", "timestamp"},
    "system_events":    {"event", "data", "timestamp"},
    # [FIX-ROUTING-01] field alias cho system_alerts khi đến từ intrusion payload
    # payload: {"type":"intrusion","message":"...","level":"critical","location":"entrance_01"}
    # → map "location" → "room", giữ "type", "message", "level"
}

# Field alias: key trong payload → column trong DB
_FIELD_ALIASES = {
    "location":   "room",
    "room_id":    "room",
    "owner_name": "user_name",
    "user":       "user_name",
    "device":     "device_id",
}

_UPSERT_TABLES = {"device_status", "automations", "rooms", "devices", "rfid_cards"}


# ══════════════════════════════════════════════════════════
# SD2 CONNECTION MANAGER
# ══════════════════════════════════════════════════════════

class SD2Connection:
    def __init__(self):
        self._conn    = None
        self._date    = None
        self._lock    = threading.RLock()
        self._db_path = None

    def _get_data_dir(self) -> str:
        if os.path.ismount(SD2_MOUNT):
            try:
                test = os.path.join(SD2_DATA_DIR, ".health")
                os.makedirs(SD2_DATA_DIR, exist_ok=True)
                open(test, "w").close()
                os.remove(test)
                return SD2_DATA_DIR
            except OSError:
                pass
        os.makedirs(FALLBACK_DATA_DIR, exist_ok=True)
        log.warning("SD2 không mount — dùng fallback: %s", FALLBACK_DATA_DIR)
        return FALLBACK_DATA_DIR

    def _get_db_path(self, date_str: str) -> str:
        return os.path.join(self._get_data_dir(), f"data_{date_str}.db")

    def get(self) -> sqlite3.Connection:
        with self._lock:
            today = datetime.now().strftime("%Y-%m-%d")
            if self._date != today or self._conn is None:
                self._rotate(today)
            return self._conn

    def _rotate(self, today: str):
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
        path = self._get_db_path(today)
        self._db_path = path
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA_SQL)
        self._conn.commit()
        self._date = today
        log.info("SD2 DB: %s", path)

    def lock(self):
        return self._lock


# ══════════════════════════════════════════════════════════
# SD2 DIRECT WRITER
# ══════════════════════════════════════════════════════════

class SD2DirectWriter:

    def __init__(self):
        self.r    = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
        self.db   = SD2Connection()
        self._last_sensor     = 0
        self._last_event      = 0
        self._last_status     = 0
        self._last_snapshot   = 0
        self._last_collection = 0   # [FIX-ROUTING-02]
        self._running         = False

    # ── Sensor flush ──────────────────────────────────────────────────────

    def flush_sensors(self):
        try:
            raw_offset = self._get_state("sensor_read_offset")
            offset = int(raw_offset) if raw_offset else 0

            raw_items = self.r.lrange(SENSOR_BUFFER_KEY, offset, offset + SENSOR_BATCH_SIZE - 1)
            if not raw_items:
                buf_len = self.r.llen(SENSOR_BUFFER_KEY)
                if offset > buf_len:
                    self._set_state("sensor_read_offset", "0")
                return

            rows = []
            for raw in raw_items:
                try:
                    item = json.loads(raw)
                    ts = item.get("ts") or item.get("timestamp") or ""
                    if not ts or isinstance(ts, (int, float)):
                        if isinstance(ts, (int, float)) and ts > 0:
                            ts_sec = int(ts / 1000) if ts > 1e11 else int(ts)
                            ts = datetime.fromtimestamp(ts_sec).strftime("%Y-%m-%d %H:%M:%S")
                        else:
                            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    else:
                        ts = ts.replace("T", " ").split(".")[0]

                    room  = item.get("room") or item.get("room_id") or ""
                    stype = item.get("type") or ""
                    value = item.get("value")
                    if room and stype and value is not None:
                        rows.append((room, stype, float(value), ts, 1))
                except Exception as e:
                    log.debug("Bad sensor item: %s — %s", str(raw)[:80], e)

            if rows:
                with self.db.lock():
                    conn = self.db.get()
                    conn.executemany(
                        "INSERT OR IGNORE INTO sensor_data "
                        "(room, type, value, timestamp, firebase_synced) VALUES (?,?,?,?,?)",
                        rows
                    )
                    conn.commit()
                new_offset = offset + len(raw_items)
                self._set_state("sensor_read_offset", str(new_offset))
                log.info("Sensor flush: +%d rows → SD2", len(rows))

        except Exception as e:
            log.error("flush_sensors error: %s", e)

    # ── Event flush ───────────────────────────────────────────────────────

    def flush_events(self):
        try:
            raw_offset = self._get_state("event_read_offset")
            offset = int(raw_offset) if raw_offset else 0

            raw_items = self.r.lrange(EVENT_QUEUE_KEY, offset, offset + EVENT_BATCH_SIZE - 1)
            if not raw_items:
                buf_len = self.r.llen(EVENT_QUEUE_KEY)
                if offset > buf_len:
                    self._set_state("event_read_offset", "0")
                return

            inserted = 0
            for raw in raw_items:
                try:
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(data, dict):
                        continue

                    ts = data.pop("timestamp", None)
                    if not ts or not isinstance(ts, str):
                        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    else:
                        ts = ts.replace("T", " ").split(".")[0]

                    event = data.pop("event", None) or data.pop("event_type", None)
                    if not event or not isinstance(event, str):
                        event = self._infer_event(data)
                    event = (event or "unknown_event").strip() or "unknown_event"

                    # [FIX-ROUTING-01] Nếu vẫn là unknown_event, thử map từ field "type" bên trong payload
                    if event == "unknown_event":
                        inner_type = str(data.get("type", "")).lower()
                        if inner_type in _INNER_TYPE_TO_EVENT:
                            event = _INNER_TYPE_TO_EVENT[inner_type]
                            log.debug("Resolved unknown_event via inner type='%s' → '%s'", inner_type, event)

                    # Route đến đúng bảng
                    target = "system_events"
                    for keyword, table in _EVENT_ROUTE:
                        if keyword in event.lower():
                            target = table
                            break

                    # Normalize field aliases (vd: location → room)
                    normalized = {}
                    for k, v in data.items():
                        col = _FIELD_ALIASES.get(k, k)
                        if col not in normalized:
                            normalized[col] = v
                    normalized.setdefault("timestamp", ts)
                    normalized.setdefault("created_at", ts)

                    if self._insert_row(target, normalized):
                        inserted += 1
                    else:
                        # Fallback → system_events
                        with self.db.lock():
                            conn = self.db.get()
                            conn.execute(
                                "INSERT INTO system_events (event, data, timestamp) VALUES (?,?,?)",
                                (event, json.dumps(data), ts)
                            )
                            conn.commit()

                except Exception as e:
                    log.debug("flush_events item error: %s", e)

            if inserted > 0 or len(raw_items) > 0:
                new_offset = offset + len(raw_items)
                self._set_state("event_read_offset", str(new_offset))
                if inserted > 0:
                    log.info("Event flush: +%d rows → SD2", inserted)

        except Exception as e:
            log.error("flush_events error: %s", e)

    # ── Device status sync ────────────────────────────────────────────────

    def sync_device_status(self):
        try:
            rows = []
            status_hash = self.r.hgetall("device_status")
            if status_hash:
                for key, val in status_hash.items():
                    try:
                        d = json.loads(val)
                        room      = d.get("room") or d.get("room_id") or ""
                        device_id = d.get("device_id") or d.get("device") or ""
                        is_on     = int(bool(d.get("is_on", d.get("state", False))))
                        source    = d.get("source", "redis")
                        updated   = d.get("updated_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        if room and device_id:
                            rows.append((room, device_id, is_on, source, updated))
                    except Exception:
                        pass

            for k in self.r.scan_iter("device_status:*"):
                try:
                    val = self.r.get(k)
                    if not val:
                        continue
                    parts = k.split(":", 2)
                    if len(parts) < 3:
                        continue
                    _, room, device_id = parts
                    d = json.loads(val)
                    is_on   = int(bool(d.get("is_on", d.get("state", False))))
                    source  = d.get("source", "redis")
                    updated = d.get("updated_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    rows.append((room, device_id, is_on, source, updated))
                except Exception:
                    pass

            if rows:
                with self.db.lock():
                    conn = self.db.get()
                    conn.executemany(
                        "INSERT OR REPLACE INTO device_status "
                        "(room, device_id, is_on, source, updated_at) VALUES (?,?,?,?,?)",
                        rows
                    )
                    conn.commit()

        except Exception as e:
            log.error("sync_device_status error: %s", e)

    # ── [FIX-ROUTING-02] Sync rfid_cards và schedules từ Redis ───────────

    def sync_collections(self):
        """
        Đọc rfid_cards và schedules từ Redis hash (gateway cache sẵn)
        và upsert vào SD2.

        Redis keys expected:
          - "rfid_cards"              → hash: uid → JSON {uid, owner_name, is_active, created_at}
          - "schedules"               → hash: id  → JSON {room_id, device_id, action, time, enabled}
          - "rfid:{uid}"              → string JSON (fallback pattern)
          - "schedule:{id}"           → string JSON (fallback pattern)
          - "auto_sync:schedules"     → string JSON list (từ AutoSync Firestore)
          - "auto_sync:rfid_cards"    → string JSON list
        """
        try:
            rfid_rows = []

            # Pattern 1: hash "rfid_cards"
            rfid_hash = self.r.hgetall("rfid_cards")
            if rfid_hash:
                for uid, val in rfid_hash.items():
                    try:
                        d = json.loads(val) if val.startswith("{") else {"uid": uid, "owner_name": val}
                        rfid_rows.append((
                            d.get("uid", uid),
                            d.get("owner_name", ""),
                            int(d.get("is_active", 1)),
                            d.get("created_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                        ))
                    except Exception:
                        rfid_rows.append((uid, "", 1, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))

            # Pattern 2: individual keys "rfid:{uid}"
            for k in self.r.scan_iter("rfid:*"):
                try:
                    val = self.r.get(k)
                    if not val:
                        continue
                    d = json.loads(val)
                    uid = d.get("uid") or k.split(":", 1)[1]
                    rfid_rows.append((
                        uid,
                        d.get("owner_name", ""),
                        int(d.get("is_active", 1)),
                        d.get("created_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                    ))
                except Exception:
                    pass

            # Pattern 3: auto_sync cache từ Firestore (AutoSync writes này)
            auto_rfid_raw = self.r.get("auto_sync:rfid_cards")
            if auto_rfid_raw:
                try:
                    cards = json.loads(auto_rfid_raw)
                    for d in (cards if isinstance(cards, list) else []):
                        uid = d.get("uid", "")
                        if uid:
                            rfid_rows.append((
                                uid,
                                d.get("owner_name", d.get("ownerName", "")),
                                int(d.get("is_active", d.get("isActive", 1))),
                                d.get("created_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                            ))
                except Exception:
                    pass

            if rfid_rows:
                with self.db.lock():
                    conn = self.db.get()
                    conn.executemany(
                        "INSERT OR REPLACE INTO rfid_cards (uid, owner_name, is_active, created_at) "
                        "VALUES (?,?,?,?)",
                        rfid_rows
                    )
                    conn.commit()
                log.info("[FIX-ROUTING-02] rfid_cards sync: %d cards → SD2", len(rfid_rows))

        except Exception as e:
            log.error("sync_collections rfid error: %s", e)

        # ── Schedules ────────────────────────────────────────────────────
        try:
            sched_rows = []

            # Pattern 1: hash "schedules"
            sched_hash = self.r.hgetall("schedules")
            if sched_hash:
                for sid, val in sched_hash.items():
                    try:
                        d = json.loads(val)
                        sched_rows.append((
                            d.get("room_id", ""),
                            d.get("device_id", ""),
                            d.get("action", ""),
                            d.get("time", ""),
                            int(d.get("enabled", 1)),
                            d.get("last_run", ""),
                            d.get("created_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                        ))
                    except Exception:
                        pass

            # Pattern 2: auto_sync cache từ Firestore
            auto_sched_raw = self.r.get("auto_sync:schedules")
            if auto_sched_raw:
                try:
                    scheds = json.loads(auto_sched_raw)
                    for d in (scheds if isinstance(scheds, list) else []):
                        room_id   = d.get("room_id", d.get("roomId", ""))
                        device_id = d.get("device_id", d.get("deviceId", ""))
                        action    = d.get("action", "")
                        t         = d.get("time", "")
                        if room_id and device_id and action and t:
                            sched_rows.append((
                                room_id, device_id, action, t,
                                int(d.get("enabled", 1)),
                                d.get("last_run", d.get("lastRun", "")),
                                d.get("created_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                            ))
                except Exception:
                    pass

            # Pattern 3: individual keys "schedule:{id}"
            for k in self.r.scan_iter("schedule:*"):
                try:
                    val = self.r.get(k)
                    if not val:
                        continue
                    d = json.loads(val)
                    sched_rows.append((
                        d.get("room_id", ""),
                        d.get("device_id", ""),
                        d.get("action", ""),
                        d.get("time", ""),
                        int(d.get("enabled", 1)),
                        d.get("last_run", ""),
                        d.get("created_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                    ))
                except Exception:
                    pass

            if sched_rows:
                with self.db.lock():
                    conn = self.db.get()
                    conn.executemany(
                        "INSERT OR IGNORE INTO schedules "
                        "(room_id, device_id, action, time, enabled, last_run, created_at) "
                        "VALUES (?,?,?,?,?,?,?)",
                        sched_rows
                    )
                    conn.commit()
                log.info("[FIX-ROUTING-02] schedules sync: %d rows → SD2", len(sched_rows))

        except Exception as e:
            log.error("sync_collections schedules error: %s", e)

    # ── Snapshot ──────────────────────────────────────────────────────────

    def write_snapshot(self):
        try:
            wifi_raw = self.r.get("system_status:wifi")
            wifi = json.loads(wifi_raw) if wifi_raw else {}
            with self.db.lock():
                conn = self.db.get()
                room_count   = conn.execute("SELECT COUNT(*) FROM rooms").fetchone()[0]
                device_count = conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
                alert_count  = conn.execute(
                    "SELECT COUNT(*) FROM system_alerts WHERE is_resolved=0"
                ).fetchone()[0]
                conn.execute(
                    "INSERT INTO system_snapshots "
                    "(wifi_ssid, wifi_status, room_count, device_count, alert_count, timestamp) "
                    "VALUES (?,?,?,?,?,?)",
                    (
                        wifi.get("ssid", ""),
                        wifi.get("status", ""),
                        room_count, device_count, alert_count,
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    )
                )
                conn.commit()
        except Exception as e:
            log.error("write_snapshot error: %s", e)

    # ── Helpers ───────────────────────────────────────────────────────────

    def _insert_row(self, table: str, row: dict) -> bool:
        valid_cols = _TABLE_COLUMNS.get(table)
        if valid_cols:
            row = {k: v for k, v in row.items() if k in valid_cols}
        for mk in ("event", "event_type"):
            row.pop(mk, None)
        if not row:
            return False

        verb = "INSERT OR REPLACE" if table in _UPSERT_TABLES else "INSERT OR IGNORE"
        cols = ", ".join(row.keys())
        vals = ", ".join(["?"] * len(row))
        sql  = f"{verb} INTO {table} ({cols}) VALUES ({vals})"
        try:
            with self.db.lock():
                conn = self.db.get()
                conn.execute(sql, list(row.values()))
                conn.commit()
            return True
        except sqlite3.OperationalError as e:
            log.debug("_insert_row table=%s error: %s | keys=%s", table, e, list(row.keys()))
            return False

    def _infer_event(self, data: dict) -> str:
        """Suy luận event name từ payload keys."""
        keys = set(data.keys())
        if "type" in keys and "value" in keys and ("room" in keys or "room_id" in keys):
            sensor_type = str(data.get("type", "")).lower()
            if sensor_type in ("gas", "co2"):
                return "gas_alert" if float(data.get("value", 0) or 0) > 500 else "sensor_data"
            return "sensor_data"
        if "device_id" in keys and "is_on" in keys:
            return "device_status_update"
        if "uid" in keys or ("action" in keys and ("room" in keys or "room_id" in keys)):
            return "access_log"
        if "scenario" in keys or "actions" in keys:
            return "automation_log"
        if "filename" in keys and "url" in keys:
            return "ota_update"
        if "title" in keys and "message" in keys:
            return "notification"
        return "unknown_event"

    def _get_state(self, key: str) -> str:
        try:
            with self.db.lock():
                conn = self.db.get()
                row = conn.execute(
                    "SELECT value FROM _sd2_writer_state WHERE key=?", (key,)
                ).fetchone()
                return row[0] if row else ""
        except Exception:
            return ""

    def _set_state(self, key: str, value: str):
        try:
            with self.db.lock():
                conn = self.db.get()
                conn.execute(
                    "INSERT OR REPLACE INTO _sd2_writer_state (key, value, updated_at) VALUES (?,?,?)",
                    (key, value, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                )
                conn.commit()
        except Exception as e:
            log.debug("_set_state error: %s", e)

    # ── Main loop ─────────────────────────────────────────────────────────

    def _loop(self):
        log.info("SD2DirectWriter v2.1 started (DB: %s)", self.db._get_data_dir())

        # Reset offset về cuối buffer (không đọc lại history cũ)
        buf_len   = self.r.llen(SENSOR_BUFFER_KEY)
        event_len = self.r.llen(EVENT_QUEUE_KEY)
        self._set_state("sensor_read_offset", str(max(0, buf_len - 100)))
        self._set_state("event_read_offset",  str(max(0, event_len - 50)))
        log.info("Buffer init: sensor=%d, events=%d", buf_len, event_len)

        # [FIX-ROUTING-02] Sync ngay khi khởi động
        self.sync_collections()

        while self._running:
            try:
                now = time.time()

                if now - self._last_sensor >= SENSOR_FLUSH_INTERVAL:
                    self.flush_sensors()
                    self._last_sensor = now

                if now - self._last_event >= EVENT_FLUSH_INTERVAL:
                    self.flush_events()
                    self._last_event = now

                if now - self._last_status >= STATUS_SYNC_INTERVAL:
                    self.sync_device_status()
                    self._last_status = now

                if now - self._last_snapshot >= SNAPSHOT_INTERVAL:
                    self.write_snapshot()
                    self._last_snapshot = now

                # [FIX-ROUTING-02] sync rfid_cards/schedules định kỳ
                if now - self._last_collection >= COLLECTION_SYNC_INTERVAL:
                    self.sync_collections()
                    self._last_collection = now

                time.sleep(5)

            except Exception as e:
                log.error("SD2DirectWriter loop error: %s", e)
                time.sleep(10)

    def start(self) -> "SD2DirectWriter":
        self._running = True
        t = threading.Thread(target=self._loop, name="SD2DirectWriter", daemon=True)
        t.start()
        log.info("SD2DirectWriter thread started")
        return self

    def stop(self):
        self._running = False

    def run_forever(self):
        self._running = True
        try:
            self._loop()
        except KeyboardInterrupt:
            log.info("SD2DirectWriter stopped")


# ══════════════════════════════════════════════════════════
# STANDALONE
# ══════════════════════════════════════════════════════════
if __name__ == "__main__":
    SD2DirectWriter().run_forever()