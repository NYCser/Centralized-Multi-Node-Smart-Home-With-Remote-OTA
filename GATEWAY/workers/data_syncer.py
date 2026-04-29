"""
workers/data_syncer.py  — FIXED
══════════════════════════════════════════════════════════════
FIX BUG-C-02: data_syncer không được treo vô hạn khi thiếu SD2.
  Nguyên nhân: SD2Manager.wait_ready() block mãi mãi nếu SD2 không có.
  Fix: Thêm "Degraded Mode" — sau DEGRADED_TIMEOUT giây không thấy SD2,
       tự động chuyển sang ghi vào /data (bộ nhớ trong Pi).
       Khi SD2 xuất hiện trở lại (hot-plug), tự động chuyển về SD2.

  Tất cả các bug v1/v2 đã được fix trong phiên bản gốc được giữ nguyên.
  File này chỉ patch thêm BUG-C-02 lên trên base đã fix.
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
# FIX BUG-C-02: Fallback path khi không có SD2
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

# FIX BUG-C-02: Thời gian chờ SD2 trước khi chuyển Degraded Mode
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
    """
    State machine: UNKNOWN → READY ↔ DEGRADED
    FIX BUG-C-02: Sau DEGRADED_TIMEOUT giây không có SD2,
    chuyển sang DEGRADED và dùng fallback path.
    """

    def __init__(self):
        self._ready        = threading.Event()
        self._degraded     = threading.Event()   # FIX BUG-C-02
        self._lock         = threading.Lock()
        self._degraded_at  = None  # timestamp khi bắt đầu degraded

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
                # FIX BUG-C-02: set degraded mode sau timeout
                self._degraded.set()
        return ok

    def is_degraded(self) -> bool:
        """True khi SD2 không có và đã hết timeout — dùng fallback path."""
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
        """
        FIX BUG-C-02: Trả về đường dẫn data dir phù hợp với state hiện tại.
        - SD2 sẵn sàng: dùng SD2_MOUNT/data
        - Degraded (không có SD2): dùng FALLBACK_DATA_DIR
        """
        if self.is_ready():
            return DATA_DIR
        if self.is_degraded():
            os.makedirs(FALLBACK_DATA_DIR, exist_ok=True)
            return FALLBACK_DATA_DIR
        return DATA_DIR  # chưa xác định, thử dùng SD2


# ══════════════════════════════════════════════════════════
# LAYER 1 — StorageLayer
# ══════════════════════════════════════════════════════════

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS sensor_data (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    room      TEXT NOT NULL,
    type      TEXT NOT NULL,
    value     REAL NOT NULL,
    timestamp TEXT NOT NULL,
    UNIQUE(room, type, timestamp) ON CONFLICT IGNORE
);
CREATE INDEX IF NOT EXISTS idx_sensor_rt ON sensor_data(room, type, timestamp DESC);
CREATE TABLE IF NOT EXISTS system_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    event     TEXT NOT NULL,
    data      TEXT,
    timestamp TEXT NOT NULL
);
"""


class StorageLayer:
    def __init__(self, sd2: SD2Manager):
        self._sd2     = sd2
        self._conn    = None
        self._db_path = None
        self._db_lock = threading.RLock()  # FIX v2-BUG-D: RLock tránh deadlock
        self._date    = None

    def _get_db_path(self, date_str: str) -> str:
        data_dir = self._sd2.get_data_dir()  # FIX BUG-C-02
        os.makedirs(data_dir, exist_ok=True)
        return os.path.join(data_dir, f"data_{date_str}.db")

    def _init_db(self, conn: sqlite3.Connection):
        conn.executescript(SCHEMA_SQL)
        conn.commit()

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
        db_path      = self._get_db_path(today)
        self._db_path = db_path
        self._conn   = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._init_db(self._conn)
        self._date = today
        log.info("DB rotated → %s (mode: %s)",
                 db_path,
                 "degraded/fallback" if self._sd2.is_degraded() else "sd2")

    def insert_sensors(self, rows: list):
        """rows: list of (room, type, value, timestamp)"""
        with self._db_lock:
            conn = self.get_conn()
            conn.executemany(
                "INSERT OR IGNORE INTO sensor_data (room, type, value, timestamp) VALUES (?,?,?,?)",
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

    def export_csv(self, date_str: str, export_dir: str) -> str:
        """FIX v2-BUG-E: dùng _db_lock khi đọc để tránh data race."""
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
            writer.writerow(["id", "room", "type", "value", "timestamp"])
            writer.writerows(rows)
        return out_path

    def backup_db(self, date_str: str, backup_dir: str) -> str:
        """FIX v2-BUG-F: dùng db_path tương ứng ngày, không phải global."""
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
        """FIX v2-BUG-G: atomic pipeline."""
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
        """FIX v2-BUG-B: lấy đúng count items, không bị race."""
        raw = self.r.lrange(BUFFER_KEY, 0, count - 1)
        return raw

    def trim_sensors(self, count: int):
        """FIX v2-BUG-B: trim đúng số đã đọc."""
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
        """FIX v2-BUG-C: re-queue giữ nguyên thứ tự."""
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
        self._last_export = -1  # giờ xuất CSV lần cuối

    # ── Public API ─────────────────────────────────────────

    def push_sensor(self, room: str, s_type: str, value: float):
        self.buf.push_sensor(room, s_type, value)

    def log_event(self, event: str, data: dict):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            self.store.insert_event(event, data, ts)
        except Exception as e:
            log.error("log_event error: %s", e)

    # ── Core flush ─────────────────────────────────────────

    def flush_sensor(self):
        raw_items = self.buf.read_sensors(FLUSH_COUNT)
        if not raw_items:
            return
        read_count = len(raw_items)

        rows = []
        for raw in raw_items:
            try:
                item = json.loads(raw)
                rows.append((item["room"], item["type"], item["ts"] ,float(item["value"])))
            except Exception as e:
                log.warning("Bad sensor item: %s — %s", raw[:60], e)

        if rows:
            try:
                self.store.insert_sensors(rows)
                # FIX v2-BUG-B: trim chính xác số đã đọc
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
                data = json.loads(raw)
                ts   = data.pop("timestamp", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                event = data.pop("event", "unknown")
                self.store.insert_event(event, data, ts)
            except Exception as e:
                log.error("flush_events error: %s", e)
                failed.append(raw)
        if failed:
            # FIX v2-BUG-C: re-queue giữ đúng thứ tự
            self.buf.requeue_events(failed)

    def snapshot(self):
        try:
            wifi_raw = self.r.get("system_status:wifi")
            wifi     = json.loads(wifi_raw) if wifi_raw else {}
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.store.insert_event("system_snapshot", {
                "wifi_ssid":   wifi.get("ssid", ""),
                "wifi_status": wifi.get("status", ""),
                "storage_mode": "degraded" if self.sd2.is_degraded() else "sd2",
            }, ts)
        except Exception as e:
            log.error("snapshot error: %s", e)

    # ── Main loop ─────────────────────────────────────────

    def run(self):
        log.info("SyncWorker starting...")

        # FIX BUG-C-02: không block vô hạn — chờ tối đa DEGRADED_TIMEOUT
        log.info("Waiting for SD2 (timeout %ds before degraded mode)...", DEGRADED_TIMEOUT)
        sd2_ready = self.sd2.wait_ready(timeout=DEGRADED_TIMEOUT)
        if sd2_ready:
            log.info("✅ SD2 ready at %s", DATA_DIR)
        else:
            # Thử mount một lần
            if not self.sd2.try_mount():
                self.sd2.is_ready()  # force state update → set degraded
                if self.sd2.is_degraded():
                    log.warning(
                        "⚠️  SD2 không có sau %ds — chạy DEGRADED MODE, "
                        "ghi vào fallback: %s",
                        DEGRADED_TIMEOUT, FALLBACK_DATA_DIR
                    )
                    # Thông báo cho Web
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

                # Kiểm tra SD2 hot-plug (có thể cắm vào sau)
                if not self.sd2.is_ready() and not self.sd2.is_degraded():
                    self.sd2.try_mount()

                # Nếu đang degraded nhưng SD2 vừa xuất hiện → chuyển về SD2
                if self.sd2.is_degraded() and self.sd2.is_mounted():
                    if self.sd2.is_writable():
                        self.sd2._degraded.clear()
                        self.sd2._degraded_at = None
                        log.info("✅ SD2 hot-plugged — switching back from degraded mode")

                # Flush sensor mỗi FLUSH_EVERY giây
                if now - self._last_flush >= FLUSH_EVERY:
                    self.flush_sensor()
                    self.flush_events()
                    self.snapshot()
                    self._last_flush = now

                # Export CSV mỗi ngày lúc EXPORT_HOUR
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


# ── Public API cho các module khác ──────────────────────────

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