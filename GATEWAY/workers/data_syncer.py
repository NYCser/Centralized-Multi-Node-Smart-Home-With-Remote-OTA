"""
workers/data_syncer.py  — FIXED
══════════════════════════════════════════════════════════════
FIX BUG-C-02: data_syncer không được treo vô hạn khi thiếu SD2.
  Nguyên nhân: SD2Manager.wait_ready() block mãi mãi nếu SD2 không có.
  Fix: Thêm "Degraded Mode" — sau DEGRADED_TIMEOUT giây không thấy SD2,
       tự động chuyển sang ghi vào /data (bộ nhớ trong Pi).
       Khi SD2 xuất hiện trở lại (hot-plug), tự động chuyển về SD2.

FIX BUG-SYNCER-01: flush_events / snapshot lỗi "no such column: firebase_synced"
  Nguyên nhân: DB files tạo bởi version cũ thiếu cột firebase_synced.
               CREATE TABLE IF NOT EXISTS KHÔNG thêm cột mới vào bảng đã có.
  Fix: _init_db() chạy migrations ALTER TABLE sau executescript.
       Idempotent — "duplicate column name" được bắt và bỏ qua.
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
from datetime import datetime

import redis as redis_lib

# ══════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════

SD2_MOUNT    = "/mnt/sd2"
DATA_DIR     = f"{SD2_MOUNT}/data"
FALLBACK_DATA_DIR = os.getenv("FALLBACK_DATA_DIR", "/data/sensor_history")
REDIS_HOST   = "localhost"
BUFFER_KEY   = "sensor_buffer"
EVENT_QUEUE  = "event_queue"
FLUSH_EVERY  = 180
FLUSH_COUNT  = 50
EXPORT_HOUR  = 2
MOUNT_SCRIPT = "/home/pi/GATEWAY/scripts/mount_sd2.sh"

RETRY_BASE  = 2
RETRY_MAX   = 60

BUFFER_WARN_THRESHOLD = 5_000
BUFFER_MAX_THRESHOLD  = 20_000

DEGRADED_TIMEOUT = 300  # 5 phút

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
        self._ready        = threading.Event()
        self._degraded     = threading.Event()
        self._lock         = threading.Lock()
        self._degraded_at  = None

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

CREATE TABLE IF NOT EXISTS users (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    email        TEXT    UNIQUE NOT NULL,
    password     TEXT    NOT NULL,
    display_name TEXT    DEFAULT '',
    role         TEXT    DEFAULT 'user',
    created_at   TEXT    DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS sessions (
    token       TEXT    PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    expires_at  TEXT    NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
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

-- sensor_data: firebase_synced cột BẮT BUỘC phải có.
-- Với DB cũ thiếu cột này, migration trong _init_db() sẽ thêm vào.
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

CREATE TABLE IF NOT EXISTS device_status (
    room       TEXT NOT NULL,
    device_id  TEXT NOT NULL,
    is_on      INTEGER DEFAULT 0,
    source     TEXT    DEFAULT 'unknown',
    updated_at TEXT    DEFAULT (datetime('now','localtime')),
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
CREATE INDEX IF NOT EXISTS idx_alert_unresolved ON system_alerts(is_resolved, timestamp DESC);
CREATE TABLE IF NOT EXISTS notifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    type       TEXT NOT NULL,
    title      TEXT NOT NULL,
    message    TEXT NOT NULL,
    is_read    INTEGER DEFAULT 0,
    room       TEXT    DEFAULT '',
    created_at TEXT    DEFAULT (datetime('now','localtime'))
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
CREATE TABLE IF NOT EXISTS automation_logs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    room         TEXT,
    scenario     TEXT,
    actions      TEXT,
    triggered_by TEXT,
    timestamp    TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS rfid_cards (
    uid        TEXT PRIMARY KEY,
    owner_name TEXT    DEFAULT '',
    is_active  INTEGER DEFAULT 1,
    created_at TEXT    DEFAULT (datetime('now','localtime'))
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
    last_run   TEXT    DEFAULT '',
    created_at TEXT    DEFAULT (datetime('now','localtime'))
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
"""

# ══════════════════════════════════════════════════════════
# SMART ROUTER
# ══════════════════════════════════════════════════════════
_EVENT_ROUTE: list[tuple[str, str]] = [
    ("rfid",         "access_logs"),
    ("access",       "access_logs"),
    ("ota",          "ota_logs"),
    ("firmware",     "ota_logs"),
    ("automation",   "automation_logs"),
    ("login",        "login_logs"),
    ("logout",       "login_logs"),
    ("auth",         "login_logs"),
    ("notif",        "notifications"),
    ("alert",        "system_alerts"),
    ("warning",      "system_alerts"),
    ("snapshot",     "system_snapshots"),
    ("device",       "device_status"),
    ("schedule",     "schedules"),
]

_META_KEYS = frozenset({"event", "event_type"})


# ══════════════════════════════════════════════════════════
# LAYER 1 — StorageLayer
# ══════════════════════════════════════════════════════════

class StorageLayer:
    # ── FIX BUG-SYNCER-01: danh sách migration ─────────────────────────────
    # Mỗi entry là 1 câu ALTER TABLE. Chạy lần lượt sau executescript().
    # Idempotent: "duplicate column name" → bỏ qua, không coi là lỗi.
    # Thêm migration mới vào CUỐI danh sách này khi nâng cấp schema.
    _MIGRATIONS: list[str] = [
        # v2 → v3: thêm firebase_synced cho firebase_sync.py
        "ALTER TABLE sensor_data ADD COLUMN firebase_synced INTEGER DEFAULT 0",
        # v3 → v4: thêm co2_threshold cho automation engine
        "ALTER TABLE automations ADD COLUMN co2_threshold REAL DEFAULT 1000",
        # v3 → v4: thêm last_run cho schedule tracker
        "ALTER TABLE schedules ADD COLUMN last_run TEXT DEFAULT ''",
    ]

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
        """
        FIX BUG-SYNCER-01: 2 bước:
          1. executescript(SCHEMA_SQL) — tạo bảng mới nếu chưa có (idempotent).
          2. Chạy từng migration trong _MIGRATIONS — thêm cột còn thiếu vào DB
             cũ (idempotent: duplicate column name bị bắt và bỏ qua).
        """
        # Bước 1: tạo schema đầy đủ
        conn.executescript(SCHEMA_SQL)
        conn.commit()

        # Bước 2: migrations — an toàn với DB đã tồn tại từ version cũ
        for sql in self._MIGRATIONS:
            try:
                conn.execute(sql)
                conn.commit()
                log.info("[MIGRATION] Applied: %s", sql[:70])
            except sqlite3.OperationalError as exc:
                err_lower = str(exc).lower()
                if "duplicate column name" in err_lower:
                    # Cột đã có — migration đã chạy trước đó, bỏ qua bình thường
                    pass
                elif "no such table" in err_lower:
                    # Bảng chưa có — schema mới sẽ tạo; retry sau executescript
                    log.warning("[MIGRATION] Table missing for '%s': %s", sql[:50], exc)
                else:
                    # Lỗi thật — log rõ nhưng không crash worker
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

    def insert_sensors(self, rows: list):
        """rows: list of (room, type, timestamp, value)"""
        with self._db_lock:
            conn = self.get_conn()
            conn.executemany(
                "INSERT OR IGNORE INTO sensor_data (room, type, timestamp, value) VALUES (?,?,?,?)",
                rows
            )
            conn.commit()

    def insert_event(self, event: str, data: dict, timestamp: str):
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
            columns      = ", ".join(row.keys())
            placeholders = ", ".join(["?"] * len(row))
            sql = f"INSERT INTO {table} ({columns}) VALUES ({placeholders})"
            try:
                conn.execute(sql, list(row.values()))
                conn.commit()
                return True
            except sqlite3.OperationalError as e:
                log.warning("insert_routed_event table=%s schema mismatch: %s", table, e)
                return False

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
        self._last_flush  = 0
        self._last_export = -1

    def push_sensor(self, room: str, s_type: str, value: float):
        self.buf.push_sensor(room, s_type, value)

    def log_event(self, event: str, data: dict):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            self.store.insert_event(event, data, ts)
        except Exception as e:
            log.error("log_event error: %s", e)

    def flush_sensor(self):
        raw_items = self.buf.read_sensors(FLUSH_COUNT)
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
                    log.debug("Sanitized missing ts for %s/%s → %s",
                              item.get("room"), item.get("type"), ts)
                else:
                    if isinstance(ts, (int, float)):
                        ts_sec = int(ts / 1000) if ts > 1e11 else int(ts)
                        if ts_sec < 1577836800:
                            ts_sec = int(time.time())
                            log.debug("Sanitized invalid ts %s → now", ts)
                        ts = datetime.fromtimestamp(ts_sec).strftime("%Y-%m-%d %H:%M:%S")

                # (room, type, timestamp, value) — matches insert_sensors()
                rows.append((item["room"], item["type"], ts, float(item["value"])))
            except Exception as e:
                log.warning("Bad sensor item: %s — %s", raw[:80], e)

        if rows:
            try:
                self.store.insert_sensors(rows)
                self.buf.trim_sensors(read_count)
                log.info("Flushed %d sensor rows", len(rows))
            except Exception as e:
                log.error("flush_sensor DB error: %s — will retry next cycle", e)

    def flush_events(self):
        items = self.buf.read_events(100)
        if not items:
            return
        failed = []
        for raw in items:
            try:
                data  = json.loads(raw)
                ts    = data.pop("timestamp", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                event = data.pop("event", "unknown")

                for k in _META_KEYS:
                    data.pop(k, None)

                target_table = None
                event_lower  = event.lower()
                for keyword, table in _EVENT_ROUTE:
                    if keyword in event_lower:
                        target_table = table
                        break

                if target_table is None:
                    target_table = "system_alerts"
                    log.debug("flush_events: unknown event '%s' → system_alerts", event)

                row = dict(data)
                if "timestamp" not in row:
                    row["timestamp"] = ts

                ok = self.store.insert_routed_event(target_table, row)
                if not ok:
                    log.warning(
                        "flush_events: schema mismatch for table '%s', "
                        "falling back to system_events for event '%s'",
                        target_table, event
                    )
                    self.store.insert_event(event, data, ts)

            except Exception as e:
                log.error("flush_events error: %s", e)
                failed.append(raw)
        if failed:
            self.buf.requeue_events(failed)

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

    def run(self):
        log.info("SyncWorker starting...")
        log.info("Waiting for SD2 (timeout %ds before degraded mode)...", DEGRADED_TIMEOUT)
        sd2_ready = self.sd2.wait_ready(timeout=DEGRADED_TIMEOUT)
        if sd2_ready:
            log.info(" SD2 ready at %s", DATA_DIR)
        else:
            if not self.sd2.try_mount():
                self.sd2.is_ready()
                if self.sd2.is_degraded():
                    log.warning(
                        "SD2 không có sau %ds — chạy DEGRADED MODE, "
                        "ghi vào fallback: %s",
                        DEGRADED_TIMEOUT, FALLBACK_DATA_DIR
                    )
                    self.r.publish("realtime_data", json.dumps({
                        "event":   "system_warning",
                        "message": "Thẻ nhớ SD2 không có. Đang ghi dữ liệu vào bộ nhớ trong.",
                        "level":   "warning"
                    }))

        log.info("SyncWorker running (storage: %s)",
                 "degraded/fallback" if self.sd2.is_degraded() else "sd2")

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

                current_hour = datetime.now().hour
                if current_hour == EXPORT_HOUR and self._last_export != current_hour:
                    yesterday = datetime.now().strftime("%Y-%m-%d")
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


# ── Public API ──────────────────────────────────────────────

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