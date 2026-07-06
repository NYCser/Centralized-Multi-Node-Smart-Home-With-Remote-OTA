"""
workers/data_syncer.py  — v2.4  (BUG FIXES)
══════════════════════════════════════════════════════════════
FIXES TRONG PHIÊN BẢN NÀY (v2.4 so với v2.3):

  [FIX-ACCESS-ACTION]  Bug 2
      _FIELD_ALIASES thiếu "access_type" → "action"
      Khi Redis event gửi field access_type="granted"/"denied",
      data_syncer không map sang cột "action" trong access_logs
      → toàn bộ 38 records action=NULL.
      Fix: thêm "access_type": "action" vào _FIELD_ALIASES.

  [FIX-DEVICE-SOURCE]  Bug 3
      device_status.source='unknown' vì Firebase event thiếu field source.
      Fix: trong insert_routed_event, nếu table=device_status và
      normalized không có "source", tự điền "firebase_sync".

  [FIX-HISTORY-LEAK]   Bug 5
      _sync_one_table dùng today_start filter trên ts_col nhưng
      ts_col="timestamp" trong access_logs = thời điểm event xảy ra
      (có thể là ngày cũ 24/6), không phải ngày insert vào main DB.
      Kết quả: historical data từ ngày cũ vẫn leak vào SD2 DB hôm nay
      vì last_id=0 → lấy tất cả id > 0.
      Fix: bỏ today_start filter khỏi incremental tables (đã có last_id
      làm watermark đủ rồi). today_start chỉ giữ cho device_status
      (upsert theo updated_at, không có id).

  [GIỮ NGUYÊN từ v2.3]
      FIX-SCHEMA-TRIM: bỏ sessions/login_logs/notifications/_schedule_exec_log
      FIX-SD2-WRITER:  SD2DirectWriter start đúng chỗ trong SyncWorker.run()
      FIX-FLUSH-01:    flush_events NOT NULL constraint
      FIX-SYNC-01:     debug log main DB
      FIX-SYNC-02:     tăng LIMIT 500→1000
"""

import csv
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timedelta

import redis as redis_lib
from workers.sd2_direct_writer import SD2DirectWriter

# ══════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════

SD2_MOUNT         = "/mnt/sd2"
DATA_DIR          = f"{SD2_MOUNT}/data"
FALLBACK_DATA_DIR = os.getenv("FALLBACK_DATA_DIR", "/data/sensor_history")
MAIN_DB_PATH      = os.getenv("DB_PATH", "/home/pi/smarthome_prj/GATEWAY/storage/smarthome.db")
REDIS_HOST        = "localhost"
BUFFER_KEY        = "sensor_buffer"
EVENT_QUEUE       = "event_queue"
FLUSH_EVERY       = 180
SYNC_FROM_MAIN_EVERY = 60
EXPORT_HOUR       = 2
MOUNT_SCRIPT      = "/home/pi/GATEWAY/scripts/mount_sd2.sh"

RETRY_BASE  = 2
RETRY_MAX   = 60

BUFFER_WARN_THRESHOLD = 5_000
BUFFER_MAX_THRESHOLD  = 20_000

DEGRADED_TIMEOUT = 300

SYNC_BATCH_LIMIT = 1000

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s][SYNCER] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("data_syncer")


# ══════════════════════════════════════════════════════════
# LAYER 2 — SD2Manager (với Degraded Mode)
# ══════════════════════════════════════════════════════════

class SD2Manager:
    def __init__(self):
        self._ready       = threading.Event()
        self._degraded    = threading.Event()
        self._lock        = threading.Lock()
        self._degraded_at = None

    def is_mounted(self) -> bool:
        return os.path.ismount(SD2_MOUNT)

    def is_writable(self) -> bool:
        test_file = os.path.join(DATA_DIR, ".health_check")
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(test_file, "w") as f:
                f.write("ok")
            os.remove(test_file)
            return True
        except OSError as e:
            log.warning("SD2 write-test failed: %s", e)
            return False

    def is_ready(self) -> bool:
        ok = self.is_mounted() and self.is_writable()
        if ok:
            self._ready.set()
            self._degraded.clear()
            self._degraded_at = None
        else:
            self._ready.clear()
            if self._degraded_at is None:
                self._degraded_at = time.time()
            elif time.time() - self._degraded_at > DEGRADED_TIMEOUT:
                self._degraded.set()
        return ok

    def is_degraded(self) -> bool:
        return self._degraded.is_set()

    def wait_ready(self, timeout: float = None) -> bool:
        return self._ready.wait(timeout=timeout)

    def try_mount(self) -> bool:
        with self._lock:
            if not os.path.exists(MOUNT_SCRIPT):
                return False
            try:
                subprocess.run(["bash", MOUNT_SCRIPT], timeout=30, check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return self.is_ready()
            except Exception as e:
                log.warning("Mount script failed: %s", e)
                return False

    def get_data_dir(self) -> str:
        if self.is_ready():
            return DATA_DIR
        if self.is_degraded():
            os.makedirs(FALLBACK_DATA_DIR, exist_ok=True)
            return FALLBACK_DATA_DIR
        return DATA_DIR


# ══════════════════════════════════════════════════════════
# SCHEMA
# ══════════════════════════════════════════════════════════

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- ── Cấu hình phòng & thiết bị ────────────────────────────
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

-- ── User (cần cho access log) ─────────────────────────────
CREATE TABLE IF NOT EXISTS users (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    email        TEXT    UNIQUE NOT NULL,
    password     TEXT    NOT NULL,
    display_name TEXT    DEFAULT '',
    role         TEXT    DEFAULT 'user',
    created_at   TEXT    DEFAULT (datetime('now','localtime'))
);

-- ── Sensor (core data) ────────────────────────────────────
CREATE TABLE IF NOT EXISTS sensor_data (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    room            TEXT NOT NULL,
    type            TEXT NOT NULL,
    value           REAL NOT NULL,
    timestamp       TEXT DEFAULT (datetime('now','localtime')),
    firebase_synced INTEGER DEFAULT 0,
    UNIQUE(room, type, timestamp) ON CONFLICT IGNORE
);
CREATE INDEX IF NOT EXISTS idx_sensor_room_ts  ON sensor_data(room, type, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_sensor_unsynced ON sensor_data(firebase_synced) WHERE firebase_synced=0;

-- ── Trạng thái thiết bị ───────────────────────────────────
CREATE TABLE IF NOT EXISTS device_status (
    room       TEXT NOT NULL,
    device_id  TEXT NOT NULL,
    is_on      INTEGER DEFAULT 0,
    source     TEXT    DEFAULT 'unknown',
    updated_at TEXT    DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (room, device_id)
);

-- ── Cảnh báo hệ thống ────────────────────────────────────
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
CREATE INDEX IF NOT EXISTS idx_alert_unresolved ON system_alerts(is_resolved, timestamp DESC);

-- ── Log truy cập cửa (RFID / vân tay) ───────────────────
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
CREATE INDEX IF NOT EXISTS idx_access_room_ts ON access_logs(room, timestamp DESC);

-- ── Log automation ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS automation_logs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    room         TEXT,
    scenario     TEXT,
    actions      TEXT,
    triggered_by TEXT,
    timestamp    TEXT DEFAULT (datetime('now','localtime'))
);

-- ── Log OTA ───────────────────────────────────────────────
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

-- ── Cấu hình automation & schedule (giữ dù 0 rows) ──────
CREATE TABLE IF NOT EXISTS automations (
    room_id           TEXT PRIMARY KEY,
    enabled           INTEGER DEFAULT 1,
    fan_threshold     REAL,
    light_threshold   REAL,
    gas_threshold     REAL DEFAULT 600,
    co2_threshold     REAL DEFAULT 1000
);
CREATE TABLE IF NOT EXISTS schedules (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id    TEXT NOT NULL,
    device_id  TEXT NOT NULL,
    action     TEXT NOT NULL,
    time       TEXT NOT NULL,
    enabled    INTEGER DEFAULT 1,
    last_run   TEXT    DEFAULT '',
    created_at TEXT    DEFAULT (datetime('now','localtime'))
);

-- ── Thẻ RFID (giữ dù 0 rows — cần cho access control) ───
CREATE TABLE IF NOT EXISTS rfid_cards (
    uid        TEXT PRIMARY KEY,
    owner_name TEXT    DEFAULT '',
    is_active  INTEGER DEFAULT 1,
    created_at TEXT    DEFAULT (datetime('now','localtime'))
);

-- ── Snapshot hệ thống ────────────────────────────────────
CREATE TABLE IF NOT EXISTS system_snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    wifi_ssid    TEXT,
    wifi_status  TEXT,
    room_count   INTEGER DEFAULT 0,
    device_count INTEGER DEFAULT 0,
    alert_count  INTEGER DEFAULT 0,
    timestamp    TEXT DEFAULT (datetime('now','localtime'))
);

-- ── Event log tổng hợp ───────────────────────────────────
CREATE TABLE IF NOT EXISTS system_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    event     TEXT NOT NULL,
    data      TEXT,
    timestamp TEXT NOT NULL
);

-- ── Internal: sync state & SD2 writer state ──────────────
CREATE TABLE IF NOT EXISTS _sync_state (
    table_name TEXT PRIMARY KEY,
    last_id    INTEGER DEFAULT 0,
    last_sync  TEXT    DEFAULT ''
);
CREATE TABLE IF NOT EXISTS _sd2_writer_state (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);
"""

# ── Migration: thêm cột mới vào schema cũ ────────────────
_MIGRATIONS: list[str] = [
    "ALTER TABLE sensor_data ADD COLUMN firebase_synced INTEGER DEFAULT 0",
    "ALTER TABLE automations ADD COLUMN co2_threshold REAL DEFAULT 1000",
    "ALTER TABLE schedules ADD COLUMN last_run TEXT DEFAULT ''",
]

# ── Bảng cần sync từ main DB vào SD2 ─────────────────────
_SYNC_TABLES = [
    # (table,            id_col,  ts_col)
    ("sensor_data",      "id",    "timestamp"),
    ("device_status",    None,    "updated_at"),
    ("system_alerts",    "id",    "timestamp"),
    ("access_logs",      "id",    "timestamp"),
    ("automation_logs",  "id",    "timestamp"),
    ("ota_logs",         "id",    "created_at"),
    ("system_events",    "id",    "timestamp"),
    # Config tables (full replace, không dùng incremental)
    ("rooms",            None,    None),
    ("devices",          None,    None),
    ("automations",      None,    None),
    ("schedules",        None,    None),
    ("rfid_cards",       None,    None),
    ("users",            None,    None),
]

# ── Route event → table ───────────────────────────────────
_EVENT_ROUTE: list[tuple[str, str]] = [
    ("rfid",              "access_logs"),
    ("access",            "access_logs"),
    ("door_access",       "access_logs"),
    ("access_log",        "access_logs"),
    ("ota",               "ota_logs"),
    ("firmware",          "ota_logs"),
    ("ota_update",        "ota_logs"),
    ("automation",        "automation_logs"),
    ("automation_log",    "automation_logs"),
    ("safety_alert",      "system_alerts"),
    ("gas_alert",         "system_alerts"),
    ("fire_alert",        "system_alerts"),
    ("snapshot",          "system_snapshots"),
    ("device",            "device_status"),
    ("device_status_upd", "device_status"),
    ("schedule",          "schedules"),
    ("sensor_data",       "sensor_data"),
]

_META_KEYS = frozenset({"event", "event_type"})

# ── Column whitelist cho từng bảng ───────────────────────
_TABLE_COLUMNS: dict[str, set] = {
    "sensor_data":      {"room", "type", "value", "timestamp", "firebase_synced"},
    "system_alerts":    {"room", "type", "message", "level", "is_resolved", "resolved_at", "timestamp"},
    "system_events":    {"event", "data", "timestamp"},
    "access_logs":      {"room", "uid", "user_name", "action", "success", "duration_s", "timestamp"},
    "automation_logs":  {"room", "scenario", "actions", "triggered_by", "timestamp"},
    "ota_logs":         {"room", "filename", "url", "version", "release_notes", "triggered_by", "status", "created_at"},
    "device_status":    {"room", "device_id", "is_on", "source", "updated_at"},
    "schedules":        {"room_id", "device_id", "action", "time", "enabled", "last_run", "created_at"},
    "system_snapshots": {"wifi_ssid", "wifi_status", "room_count", "device_count", "alert_count", "timestamp"},
}

# [FIX-ACCESS-ACTION] Bug 2: thêm "access_type" → "action"
# Redis event gửi access_type="granted"/"denied" thay vì field "action"
_FIELD_ALIASES: dict[str, str] = {
    "location":      "room",
    "room_id":       "room",
    "is_on":         "is_on",
    "fire_detected": "value",
    "isResolved":    "is_resolved",
    "owner_name":    "user_name",
    "user":          "user_name",
    "device":        "device_id",
    "updated_at":    "updated_at",
    "access_type":   "action",      # [FIX-ACCESS-ACTION] "granted"/"denied" → action
}

# Các bảng incremental dùng last_id làm watermark — KHÔNG filter theo ngày
# vì ts_col là thời điểm event xảy ra (có thể là ngày cũ).
# [FIX-HISTORY-LEAK] Bug 5: chỉ device_status (upsert, không có id) mới cần today_start.
_INCREMENTAL_TABLES = frozenset({
    "sensor_data", "system_alerts", "access_logs",
    "automation_logs", "ota_logs", "system_events",
})


# ══════════════════════════════════════════════════════════
# LAYER 1 — StorageLayer
# ══════════════════════════════════════════════════════════

class StorageLayer:
    def __init__(self, sd2: SD2Manager):
        self._sd2     = sd2
        self._conn    = None
        self._db_path = None
        self._db_lock = threading.RLock()
        self._date    = None

    def _get_db_path(self, date_str: str) -> str:
        data_dir = self._sd2.get_data_dir()
        os.makedirs(data_dir, exist_ok=True)
        return os.path.join(data_dir, f"data_{date_str}.db")

    def _init_db(self, conn: sqlite3.Connection):
        conn.executescript(SCHEMA_SQL)
        conn.commit()
        for sql in _MIGRATIONS:
            try:
                conn.execute(sql)
                conn.commit()
            except sqlite3.OperationalError as exc:
                err_lower = str(exc).lower()
                if "duplicate column name" in err_lower:
                    pass
                elif "no such table" in err_lower:
                    log.warning("[MIGRATION] Table missing for '%s': %s", sql[:50], exc)
                else:
                    log.error("[MIGRATION] Failed: '%s' → %s", sql[:70], exc)

    def get_conn(self) -> sqlite3.Connection:
        with self._db_lock:
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
        db_path       = self._get_db_path(today)
        self._db_path = db_path
        self._conn    = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._init_db(self._conn)
        self._date = today
        log.info("DB rotated → %s (mode: %s)",
                 db_path,
                 "degraded/fallback" if self._sd2.is_degraded() else "sd2")

    def get_db_path_for_date(self, date_str: str) -> str:
        return self._get_db_path(date_str)

    def insert_sensors(self, rows: list):
        with self._db_lock:
            conn = self.get_conn()
            conn.executemany(
                "INSERT OR IGNORE INTO sensor_data (room, type, timestamp, value) VALUES (?,?,?,?)",
                rows
            )
            conn.commit()

    def insert_event(self, event: str, data: dict, timestamp: str):
        if not event or not isinstance(event, str):
            event = "unknown_event"
        with self._db_lock:
            conn = self.get_conn()
            conn.execute(
                "INSERT INTO system_events (event, data, timestamp) VALUES (?,?,?)",
                (event, json.dumps(data), timestamp)
            )
            conn.commit()

    def insert_routed_event(self, table: str, row: dict) -> bool:
        with self._db_lock:
            conn = self.get_conn()
            normalized = {}
            for k, v in row.items():
                alias = _FIELD_ALIASES.get(k, k)
                if alias not in normalized:
                    normalized[alias] = v

            valid_cols = _TABLE_COLUMNS.get(table)
            if valid_cols:
                normalized = {k: v for k, v in normalized.items() if k in valid_cols}

            for mk in _META_KEYS:
                normalized.pop(mk, None)

            # [FIX-DEVICE-SOURCE] Bug 3: điền default source khi sync từ Firebase
            if table == "device_status" and "source" not in normalized:
                normalized["source"] = "firebase_sync"

            if not normalized:
                log.warning("insert_routed_event: empty row after normalization for table=%s", table)
                return False

            columns      = ", ".join(normalized.keys())
            placeholders = ", ".join(["?"] * len(normalized))
            verb = (
                "INSERT OR REPLACE"
                if table in ("device_status", "automations", "rooms", "devices", "rfid_cards")
                else "INSERT OR IGNORE"
            )
            sql = f"{verb} INTO {table} ({columns}) VALUES ({placeholders})"
            try:
                conn.execute(sql, list(normalized.values()))
                conn.commit()
                return True
            except sqlite3.OperationalError as e:
                log.warning("insert_routed_event table=%s schema mismatch: %s | row_keys=%s",
                            table, e, list(normalized.keys()))
                return False

    def get_sync_state(self, table_name: str) -> int:
        with self._db_lock:
            conn = self.get_conn()
            row = conn.execute(
                "SELECT last_id FROM _sync_state WHERE table_name=?", (table_name,)
            ).fetchone()
            return row["last_id"] if row else 0

    def set_sync_state(self, table_name: str, last_id: int):
        with self._db_lock:
            conn = self.get_conn()
            conn.execute(
                "INSERT OR REPLACE INTO _sync_state (table_name, last_id, last_sync) VALUES (?,?,?)",
                (table_name, last_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            )
            conn.commit()

    def upsert_config_rows(self, table: str, rows: list, columns: list):
        if not rows:
            return
        with self._db_lock:
            conn = self.get_conn()
            placeholders = ", ".join(["?"] * len(columns))
            sql = f"INSERT OR REPLACE INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
            try:
                conn.executemany(sql, rows)
                conn.commit()
                log.debug("[SYNC] upsert %d rows → %s", len(rows), table)
            except Exception as e:
                log.error("[SYNC] upsert_config_rows table=%s error: %s", table, e)

    def upsert_device_status(self, rows: list, columns: list):
        if not rows:
            return
        with self._db_lock:
            conn = self.get_conn()
            placeholders = ", ".join(["?"] * len(columns))
            sql = f"INSERT OR REPLACE INTO device_status ({', '.join(columns)}) VALUES ({placeholders})"
            try:
                conn.executemany(sql, rows)
                conn.commit()
            except Exception as e:
                log.error("[SYNC] upsert_device_status error: %s", e)

    def insert_incremental(self, table: str, rows: list, columns: list):
        if not rows:
            return
        with self._db_lock:
            conn = self.get_conn()
            placeholders = ", ".join(["?"] * len(columns))
            sql = f"INSERT OR IGNORE INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
            try:
                conn.executemany(sql, rows)
                conn.commit()
                log.debug("[SYNC] inserted %d rows → %s", len(rows), table)
            except Exception as e:
                log.error("[SYNC] insert_incremental table=%s error: %s", table, e)

    def export_csv(self, date_str: str, export_dir: str) -> str:
        db_path = self._get_db_path(date_str)
        if not os.path.exists(db_path):
            raise FileNotFoundError(f"No DB for {date_str}")
        os.makedirs(export_dir, exist_ok=True)
        out_path = os.path.join(export_dir, f"sensor_{date_str}.csv")
        with self._db_lock:
            conn = sqlite3.connect(db_path)
            try:
                rows = conn.execute("SELECT * FROM sensor_data ORDER BY timestamp").fetchall()
            finally:
                conn.close()
        with open(out_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["id", "room", "type", "value", "timestamp", "firebase_synced"])
            writer.writerows(rows)
        return out_path

    def backup_db(self, date_str: str, backup_dir: str) -> str:
        db_path = self._get_db_path(date_str)
        os.makedirs(backup_dir, exist_ok=True)
        dst = os.path.join(backup_dir, f"backup_{date_str}.db")
        shutil.copy2(db_path, dst)
        return dst


# ══════════════════════════════════════════════════════════
# LAYER 3 — BufferLayer
# ══════════════════════════════════════════════════════════

class BufferLayer:
    def __init__(self, redis_client: redis_lib.Redis):
        self.r = redis_client

    def push_sensor(self, room: str, s_type: str, value: float, timestamp: str = None):
        ts = timestamp or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        item = json.dumps({"room": room, "type": s_type, "value": value, "ts": ts})
        with self.r.pipeline() as pipe:
            while True:
                try:
                    pipe.watch(BUFFER_KEY)
                    cur_len = pipe.llen(BUFFER_KEY)
                    if cur_len >= BUFFER_MAX_THRESHOLD:
                        log.warning("Buffer full (%d) — dropping sample %s/%s", cur_len, room, s_type)
                        pipe.reset()
                        return
                    if cur_len >= BUFFER_WARN_THRESHOLD:
                        log.warning("Buffer warn: %d items pending flush", cur_len)
                    pipe.multi()
                    pipe.rpush(BUFFER_KEY, item)
                    pipe.execute()
                    break
                except redis_lib.WatchError:
                    continue

    def read_sensors(self, count: int) -> list:
        return self.r.lrange(BUFFER_KEY, 0, count - 1)

    def trim_sensors(self, count: int):
        self.r.ltrim(BUFFER_KEY, count, -1)

    def read_events(self, count: int) -> list:
        items = []
        for _ in range(count):
            item = self.r.lpop(EVENT_QUEUE)
            if item is None:
                break
            items.append(item)
        return items

    def requeue_events(self, items: list):
        if items:
            self.r.lpush(EVENT_QUEUE, *reversed(items))


# ══════════════════════════════════════════════════════════
# LAYER 4 — SyncWorker
# ══════════════════════════════════════════════════════════

class SyncWorker:
    def __init__(self):
        self.r      = redis_lib.Redis(host=REDIS_HOST, port=6379, decode_responses=True)
        self.sd2    = SD2Manager()
        self.buf    = BufferLayer(self.r)
        self.store  = StorageLayer(self.sd2)
        self._last_flush      = 0
        self._last_sync_main  = 0
        self._last_export     = -1
        self._sd2_writer      = None

    def push_sensor(self, room: str, s_type: str, value: float):
        self.buf.push_sensor(room, s_type, value)

    def log_event(self, event: str, data: dict):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            self.store.insert_event(event, data, ts)
        except Exception as e:
            log.error("log_event error: %s", e)

    def flush_sensor(self):
        raw_items = self.buf.read_sensors(50)
        if not raw_items:
            return
        read_count = len(raw_items)
        rows = []
        for raw in raw_items:
            try:
                item = json.loads(raw)
                ts = item.get("ts")
                if not ts or ts == 0:
                    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                else:
                    if isinstance(ts, (int, float)):
                        ts_sec = int(ts / 1000) if ts > 1e11 else int(ts)
                        if ts_sec < 1577836800:
                            ts_sec = int(time.time())
                        ts = datetime.fromtimestamp(ts_sec).strftime("%Y-%m-%d %H:%M:%S")
                rows.append((item["room"], item["type"], ts, float(item["value"])))
            except Exception as e:
                log.warning("Bad sensor item: %s — %s", raw[:80], e)

        if rows:
            try:
                self.store.insert_sensors(rows)
                self.buf.trim_sensors(read_count)
                log.info("Flushed %d sensor rows from Redis buffer", len(rows))
            except Exception as e:
                log.error("flush_sensor DB error: %s", e)

    def flush_events(self):
        items = self.buf.read_events(100)
        if not items:
            return
        failed = []
        for raw in items:
            try:
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError as je:
                    log.warning("[FIX-FLUSH-01] Invalid JSON in event_queue, dropping: %s — %s",
                                raw[:100], je)
                    continue

                if not isinstance(data, dict):
                    log.warning("[FIX-FLUSH-01] Non-dict event item dropped: %s", str(data)[:100])
                    continue

                ts = data.pop("timestamp", None)
                if not ts or not isinstance(ts, str):
                    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                else:
                    ts = ts.replace("T", " ").split(".")[0]

                event = data.pop("event", None)
                if not event or not isinstance(event, str):
                    event = data.pop("event_type", None)

                for k in list(_META_KEYS):
                    data.pop(k, None)

                if not event or not isinstance(event, str):
                    event = self._infer_event_from_payload(data)

                event = (event or "unknown_event").strip() or "unknown_event"

                target_table = None
                event_lower  = event.lower()
                for keyword, table in _EVENT_ROUTE:
                    if keyword in event_lower:
                        target_table = table
                        break

                if target_table is None:
                    target_table = "system_events"
                    log.debug("flush_events: unknown event '%s' → system_events", event)

                row = dict(data)
                if "timestamp" not in row:
                    row["timestamp"] = ts

                ok = self.store.insert_routed_event(target_table, row)
                if not ok:
                    self.store.insert_event(event, data, ts)

            except Exception as e:
                log.error("flush_events unexpected error: %s | raw=%s", e, raw[:100])
                failed.append(raw)

        if failed:
            self.buf.requeue_events(failed)

    def _infer_event_from_payload(self, data: dict) -> str:
        keys = set(data.keys())
        if "type" in keys and "value" in keys and "room_id" in keys:
            sensor_type = str(data.get("type", "")).lower()
            if sensor_type in ("gas", "co2"):
                return "gas_alert" if float(data.get("value", 0) or 0) > 500 else "sensor_data"
            return "sensor_data"
        if "device_id" in keys and "is_on" in keys:
            return "device_status_update"
        if "uid" in keys or ("action" in keys and "room_id" in keys):
            return "access_log"
        if "scenario" in keys or "actions" in keys:
            return "automation_log"
        if "filename" in keys and "url" in keys:
            return "ota_update"
        return "unknown_event"

    def snapshot(self):
        try:
            wifi_raw = self.r.get("system_status:wifi")
            wifi     = json.loads(wifi_raw) if wifi_raw else {}
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            row = {
                "wifi_ssid":   wifi.get("ssid", ""),
                "wifi_status": wifi.get("status", ""),
                "timestamp":   ts,
            }
            ok = self.store.insert_routed_event("system_snapshots", row)
            if not ok:
                self.store.insert_event("system_snapshot", {
                    **row,
                    "storage_mode": "degraded" if self.sd2.is_degraded() else "sd2",
                }, ts)
        except Exception as e:
            log.error("snapshot error: %s", e)

    def _open_main_db(self):
        if not os.path.exists(MAIN_DB_PATH):
            return None
        conn = sqlite3.connect(f"file:{MAIN_DB_PATH}?mode=ro", uri=True,
                               check_same_thread=False, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def sync_from_main_db(self):
        main_conn = self._open_main_db()
        if main_conn is None:
            log.warning("[SYNC] Main DB không tồn tại tại %s", MAIN_DB_PATH)
            return

        today       = datetime.now().strftime("%Y-%m-%d")
        today_start = f"{today} 00:00:00"

        try:
            total_main = main_conn.execute("SELECT COUNT(*) FROM sensor_data").fetchone()[0]
            today_main = main_conn.execute(
                "SELECT COUNT(*) FROM sensor_data WHERE timestamp >= ?", (today_start,)
            ).fetchone()[0]
            log.info("[SYNC DEBUG] main DB sensor_data: total=%d rows, today=%d rows",
                     total_main, today_main)
        except Exception as e:
            log.warning("[SYNC DEBUG] Không đọc được sensor_data từ main DB: %s", e)

        try:
            total_synced = 0
            for table, id_col, ts_col in _SYNC_TABLES:
                try:
                    synced = self._sync_one_table(main_conn, table, id_col, ts_col, today_start)
                    if synced > 0:
                        total_synced += synced
                        log.info("[SYNC] %s: +%d rows synced", table, synced)
                except Exception as e:
                    log.error("[SYNC] Error syncing table %s: %s", table, e)

            if total_synced > 0:
                log.info("[SYNC] Total synced from main DB: %d rows", total_synced)
            else:
                log.debug("[SYNC] No new rows to sync from main DB")
        finally:
            try:
                main_conn.close()
            except Exception:
                pass

    def _sync_one_table(self, main_conn, table: str, id_col, ts_col, today_start: str) -> int:
        # Config tables: full replace, không dùng incremental
        if id_col is None and ts_col is None:
            try:
                rows_main = main_conn.execute(f"SELECT * FROM {table}").fetchall()
            except Exception as e:
                log.debug("[SYNC] %s not found in main DB: %s", table, e)
                return 0
            if not rows_main:
                return 0
            columns = list(rows_main[0].keys())
            data    = [tuple(r) for r in rows_main]
            self.store.upsert_config_rows(table, data, columns)
            return len(data)

        # device_status: upsert theo updated_at (không có id)
        # Dùng today_start vì chỉ muốn trạng thái hiện tại, không phải lịch sử
        if id_col is None and ts_col is not None:
            try:
                rows_main = main_conn.execute(
                    f"SELECT * FROM {table} WHERE {ts_col} >= ?", (today_start,)
                ).fetchall()
            except Exception as e:
                log.debug("[SYNC] %s query error: %s", table, e)
                return 0
            if not rows_main:
                return 0
            columns = list(rows_main[0].keys())
            data    = [tuple(r) for r in rows_main]
            self.store.upsert_device_status(data, columns)
            return len(data)

        # [FIX-HISTORY-LEAK] Bug 5: incremental tables chỉ dùng last_id làm watermark.
        # KHÔNG filter theo today_start vì ts_col = thời điểm event xảy ra (có thể ngày cũ).
        # Ví dụ: access_logs timestamp=2026-06-24 14:48:36 vẫn được insert vào main DB ngày 27/6
        # → nếu filter timestamp >= today_start thì bỏ sót; nếu không filter thì leak đúng.
        # Giải pháp: last_id watermark đã đủ để tránh duplicate — bỏ today_start filter.
        last_id = self.store.get_sync_state(table)
        try:
            rows_main = main_conn.execute(
                f"SELECT * FROM {table} WHERE {id_col} > ? "
                f"ORDER BY {id_col} ASC LIMIT {SYNC_BATCH_LIMIT}",
                (last_id,)
            ).fetchall()
        except Exception as e:
            log.debug("[SYNC] %s query error: %s", table, e)
            return 0

        if not rows_main:
            log.debug("[SYNC] %s: no new rows (last_id=%d)", table, last_id)
            return 0

        columns     = list(rows_main[0].keys())
        data        = [tuple(r) for r in rows_main]
        self.store.insert_incremental(table, data, columns)
        new_last_id = rows_main[-1][id_col]
        self.store.set_sync_state(table, new_last_id)
        log.debug("[SYNC] %s: +%d rows (last_id: %d → %d)", table, len(data), last_id, new_last_id)
        return len(data)

    # ── Main loop ──────────────────────────────────────────────────────────

    def run(self):
        log.info("SyncWorker starting...")
        log.info("Waiting for SD2 (timeout %ds before degraded mode)...", DEGRADED_TIMEOUT)
        sd2_ready = self.sd2.wait_ready(timeout=DEGRADED_TIMEOUT)
        if sd2_ready:
            log.info("SD2 ready at %s", DATA_DIR)
        else:
            if not self.sd2.try_mount():
                self.sd2.is_ready()
                if self.sd2.is_degraded():
                    log.warning(
                        "SD2 không có sau %ds — chạy DEGRADED MODE, ghi vào fallback: %s",
                        DEGRADED_TIMEOUT, FALLBACK_DATA_DIR
                    )
                    self.r.publish("realtime_data", json.dumps({
                        "event":   "system_warning",
                        "message": "Thẻ nhớ SD2 không có. Đang ghi dữ liệu vào bộ nhớ trong.",
                        "level":   "warning"
                    }))

        log.info("SyncWorker running (storage: %s)",
                 "degraded/fallback" if self.sd2.is_degraded() else "sd2")

        # [FIX-SD2-WRITER] Khởi động SD2DirectWriter đúng chỗ — trong run() của SyncWorker
        self._sd2_writer = SD2DirectWriter().start()
        log.info("SD2DirectWriter started")

        # Chạy sync lần đầu ngay khi startup để fill dữ liệu hôm nay
        try:
            log.info("[SYNC] Initial sync from main DB on startup...")
            self.sync_from_main_db()
        except Exception as e:
            log.error("[SYNC] Initial sync error: %s", e)

        while True:
            try:
                now = time.time()

                if not self.sd2.is_ready() and not self.sd2.is_degraded():
                    self.sd2.try_mount()

                if self.sd2.is_degraded() and self.sd2.is_mounted():
                    if self.sd2.is_writable():
                        self.sd2._degraded.clear()
                        self.sd2._degraded_at = None
                        log.info("SD2 hot-plugged — switching back from degraded mode")

                if now - self._last_flush >= FLUSH_EVERY:
                    self.flush_sensor()
                    self.flush_events()
                    self.snapshot()
                    self._last_flush = now

                if now - self._last_sync_main >= SYNC_FROM_MAIN_EVERY:
                    self.sync_from_main_db()
                    self._last_sync_main = now

                current_hour = datetime.now().hour
                if current_hour == EXPORT_HOUR and self._last_export != current_hour:
                    yesterday  = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
                    export_dir = os.path.join(self.sd2.get_data_dir(), "exports")
                    try:
                        out = self.store.export_csv(yesterday, export_dir)
                        log.info("CSV exported: %s", out)
                        self.store.backup_db(yesterday, self.sd2.get_data_dir())
                    except Exception as e:
                        log.error("Export error: %s", e)
                    self._last_export = current_hour

                time.sleep(5)

            except Exception as e:
                log.error("SyncWorker loop error: %s", e)
                time.sleep(10)


# ── Public API ─────────────────────────────────────────────

_worker: SyncWorker = None


def _get_worker() -> SyncWorker:
    global _worker
    if _worker is None:
        _worker = SyncWorker()
    return _worker


def push_sensor(room: str, s_type: str, value: float):
    _get_worker().push_sensor(room, s_type, value)


def log_event(event: str, data: dict):
    _get_worker().log_event(event, data)


def run():
    worker = SyncWorker()
    global _worker
    _worker = worker
    worker.run()


if __name__ == "__main__":
    run()