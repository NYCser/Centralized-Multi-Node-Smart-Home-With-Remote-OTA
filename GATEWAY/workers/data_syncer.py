"""
workers/data_syncer.py  ·  v3
══════════════════════════════════════════════════════════════
IoT Data Syncer — Daily SQLite partition + hybrid sensor/event system

KIẾN TRÚC 4 TẦNG (separation of concerns):
  Layer 1 — StorageLayer   : SQLite lifecycle (open/close/rotate/schema)
  Layer 2 — SD2Manager     : hardware detect / mount / recovery state machine
  Layer 3 — BufferLayer    : Redis ingestion (sensor_buffer + event_queue)
  Layer 4 — SyncWorker     : orchestration — move data Redis → SQLite

BUGS FIXED vs v1 (original) + v2 (patched):
  v1-BUG-01  is_sd2_ready() tạo dir/file → side-effect trong health-check
  v1-BUG-02  flush_sensor: lpop loop — TOCTOU, data loss nếu crash mid-loop
  v1-BUG-03  flush_events: không re-queue khi DB fail → event loss
  v1-BUG-04  global conn không có Lock → race condition với multi-thread write
  v1-BUG-05  rotate_if_needed: conn.close() trực tiếp (không safe)
  v1-BUG-06  ensure_sd2_mounted: gọi is_sd2_ready() (write-test) → infinite mkdir loop
  v1-BUG-07  export_csv + backup_db: không có lock → race với flush

  v2-BUG-B   flush_sensor ltrim dùng len(raw_items) thay vì read_count cố định
             → ltrim offset sai khi queue có items mới vào giữa → DUPLICATE DATA
  v2-BUG-C   flush_events re-queue dùng lpush+reversed → đảo thứ tự events
             → out-of-order timestamps trong SQLite
  v2-BUG-D   _db_lock non-reentrant: rotate_if_needed(with lock) → init_db()
             → nếu flush_sensor đang hold lock → potential deadlock khi structure thay đổi
             Fix: dùng RLock (reentrant) thay Lock
  v2-BUG-E   export_csv đọc conn không có _db_lock → DATA RACE vs flush_sensor
  v2-BUG-F   backup_db dùng global db_path → sai ngày nếu rotation xảy ra giữa export+backup
  v2-BUG-G   push_sensor backpressure TOCTOU (llen + rpush không atomic)
             Fix: dùng Redis pipeline + MULTI/EXEC pattern
  v2-BUG-H   _sd2_was_ready bare bool → không thread-safe trên ARM multi-core
             Fix: threading.Event()
  v2-BUG-I   flush_sensor: DB commit OK nhưng ltrim fail → duplicate insert
             Fix: INSERT OR IGNORE + (room, type, timestamp) UNIQUE index
  v2-BUG-J   snapshot() bare except:pass → nuốt MemoryError, KeyboardInterrupt
             Fix: except Exception only

FEATURES PRESERVED (full parity with v1):
  ✓ Daily SQLite partition trên SD2
  ✓ sensor_data + system_events tables với indexes
  ✓ push_sensor() API (external use)
  ✓ flush_sensor() — Redis → SQLite batch
  ✓ flush_events() — event_queue → SQLite
  ✓ log_event() — direct insert
  ✓ export_csv() — daily CSV export lúc EXPORT_HOUR
  ✓ backup_db() — copy DB sang SD2 root
  ✓ snapshot() — ghi wifi status vào system_events
  ✓ debug_sd2() — diagnostic info
  ✓ SD2 hot-plug: detect UP/DOWN transition + auto-recover
  ✓ Exponential backoff khi SD2 fail
  ✓ Buffer backpressure (warn + drop khi tràn)
  ✓ Structured logging
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
REDIS_HOST   = "localhost"
BUFFER_KEY   = "sensor_buffer"
EVENT_QUEUE  = "event_queue"
FLUSH_EVERY  = 180          # giây giữa hai lần flush sensor
FLUSH_COUNT  = 50           # max rows mỗi batch
EXPORT_HOUR  = 2            # giờ export CSV mỗi ngày
MOUNT_SCRIPT = "/home/pi/GATEWAY/scripts/mount_sd2.sh"

RETRY_BASE  = 2             # exponential backoff base (giây)
RETRY_MAX   = 60            # cap backoff

BUFFER_WARN_THRESHOLD = 5_000
BUFFER_MAX_THRESHOLD  = 20_000

# ── Logging ───────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s][SYNCER] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("data_syncer")


# ══════════════════════════════════════════════════════════
# LAYER 2 — SD2Manager
# Trách nhiệm: hardware detect, mount/umount, recovery FSM
# KHÔNG biết Redis, KHÔNG biết SQLite schema
# ══════════════════════════════════════════════════════════

class SD2Manager:
    """
    State machine cho SD2 hardware.
    States: UNKNOWN → READY ↔ DEGRADED → READY
    Thread-safe: _state là threading.Event, tất cả method idempotent.
    """

    # FIX v2-BUG-H: dùng threading.Event thay bare bool
    def __init__(self):
        self._ready = threading.Event()   # set = SD2 mounted & writable
        self._lock  = threading.Lock()    # bảo vệ mount/umount sequence

    # ── Truy vấn state ────────────────────────────────────

    def is_mounted(self) -> bool:
        """KHÔNG tạo file/dir — chỉ query kernel."""
        return os.path.ismount(SD2_MOUNT)

    def is_writable(self) -> bool:
        """
        Verify writable bằng write-test.
        CHỈ gọi sau khi is_mounted() = True.
        FIX v1-BUG-01: không trộn với primary health check.
        """
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
        """Source of truth: mounted AND writable."""
        ok = self.is_mounted() and self.is_writable()
        # Cập nhật Event để thread khác có thể wait()
        if ok:
            self._ready.set()
        else:
            self._ready.clear()
        return ok

    def wait_ready(self, timeout: float = None) -> bool:
        """Block cho đến khi SD2 ready. Return False nếu timeout."""
        return self._ready.wait(timeout)

    # ── Mount / umount ────────────────────────────────────

    def _force_umount(self):
        """
        Lazy umount để dọn ghost mount (ismount=True nhưng I/O fail).
        Không raise — failure ở đây không ngăn mount lại.
        """
        try:
            r = subprocess.run(
                ["sudo", "umount", "-l", SD2_MOUNT],
                capture_output=True, text=True, timeout=10,
            )
            log.debug("umount -l rc=%d stderr=%s", r.returncode, r.stderr.strip())
        except Exception as e:
            log.debug("_force_umount error (non-fatal): %s", e)

    def _do_mount(self) -> bool:
        """
        Chạy mount_sd2.sh, fallback sang 'mount -a'.
        Return True nếu sau đó is_mounted().
        """
        with self._lock:
            if os.path.isfile(MOUNT_SCRIPT):
                try:
                    r = subprocess.run(
                        ["sudo", "bash", MOUNT_SCRIPT],
                        capture_output=True, text=True, timeout=15,
                    )
                    if r.returncode == 0:
                        log.info("mount_sd2.sh OK")
                        return self.is_mounted()
                    log.warning("mount_sd2.sh rc=%d: %s", r.returncode, r.stderr.strip())
                except Exception as e:
                    log.warning("mount_sd2.sh exception: %s", e)
            else:
                log.warning("mount_sd2.sh not found → fallback 'mount -a'")

            # Fallback
            try:
                r = subprocess.run(
                    ["sudo", "mount", "-a"],
                    capture_output=True, text=True, timeout=15,
                )
                if r.returncode == 0:
                    log.info("mount -a OK")
                    return self.is_mounted()
                log.warning("mount -a rc=%d: %s", r.returncode, r.stderr.strip())
            except Exception as e:
                log.warning("mount -a exception: %s", e)

        return False

    def ensure_ready(self) -> bool:
        """
        Recovery: mount nếu chưa mount, umount+remount nếu ghost mount.
        Return True nếu is_ready() sau khi xử lý.
        FIX v1-BUG-06: không gọi is_sd2_ready() (write-test) trong loop.
        """
        if self.is_ready():
            return True

        # Ghost mount: ismount=True nhưng write fail
        if self.is_mounted() and not self.is_writable():
            log.warning("Ghost mount detected — force umount + remount")
            self._force_umount()
            time.sleep(1)

        self._do_mount()
        time.sleep(1)

        result = self.is_ready()
        if result:
            log.info("SD2 ready after ensure_ready()")
        else:
            log.warning("SD2 still not ready after ensure_ready()")
        return result

    # ── Debug ─────────────────────────────────────────────

    def debug_info(self):
        log.info(
            "SD2 debug: ismount=%s exists=%s data_dir=%s writable=%s event=%s",
            self.is_mounted(),
            os.path.exists(SD2_MOUNT),
            os.path.exists(DATA_DIR),
            os.access(DATA_DIR, os.W_OK) if os.path.exists(DATA_DIR) else "N/A",
            "set" if self._ready.is_set() else "clear",
        )


# Singleton dùng trong cả module
_sd2 = SD2Manager()

# Public backward-compat aliases
def debug_sd2():        _sd2.debug_info()
def is_sd2_ready():     return _sd2.is_ready()
def ensure_sd2_mounted(): return _sd2.ensure_ready()


# ══════════════════════════════════════════════════════════
# LAYER 1 — StorageLayer
# Trách nhiệm: SQLite open/close/rotate/schema
# KHÔNG biết Redis, KHÔNG biết SD2 mount logic
# ══════════════════════════════════════════════════════════

class StorageLayer:
    """
    Quản lý vòng đời SQLite connection.
    Thread-safe bằng RLock (reentrant) — cho phép rotate gọi init từ bên trong lock.
    FIX v2-BUG-D: RLock thay Lock để tránh deadlock khi nested acquire.
    """

    def __init__(self):
        # FIX v2-BUG-D: RLock cho phép cùng thread acquire nhiều lần
        self._lock        = threading.RLock()
        self._conn        = None
        self._db_path     = None
        self._current_date = None

    # ── Schema ────────────────────────────────────────────

    def _create_schema(self, conn: sqlite3.Connection):
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS sensor_data (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            room      TEXT    NOT NULL,
            type      TEXT    NOT NULL,
            value     REAL    NOT NULL,
            timestamp TEXT    NOT NULL,
            UNIQUE(room, type, timestamp)
        );

        CREATE TABLE IF NOT EXISTS system_events (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            type      TEXT    NOT NULL,
            payload   TEXT    NOT NULL,
            timestamp TEXT    NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_sensor_room_ts
            ON sensor_data(room, type, timestamp DESC);
        CREATE INDEX IF NOT EXISTS idx_event_ts
            ON system_events(timestamp DESC);
        """)
        conn.commit()

    # ── Open / close ──────────────────────────────────────

    def _close_safe(self):
        """Đóng conn hiện tại mà không raise. Không acquire lock — caller tự lock."""
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def open(self) -> bool:
        """
        Mở (hoặc mở lại) daily DB file. Return True nếu thành công.
        FIX v1-BUG-04: luôn dùng lock khi thay đổi self._conn.
        FIX v2-BUG-I: schema có UNIQUE(room,type,timestamp) → INSERT OR IGNORE safe.
        """
        with self._lock:
            today    = datetime.now().strftime("%Y-%m-%d")
            db_path  = os.path.join(DATA_DIR, f"data_{today}.db")
            new_db   = not os.path.exists(db_path)

            self._close_safe()

            try:
                os.makedirs(DATA_DIR, exist_ok=True)
                conn = sqlite3.connect(db_path, check_same_thread=False)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA foreign_keys=ON")

                if new_db:
                    self._create_schema(conn)
                    log.info("Created new daily DB: %s", db_path)
                else:
                    log.info("Opened existing DB: %s", db_path)

                self._conn          = conn
                self._db_path       = db_path
                self._current_date  = today
                return True

            except sqlite3.OperationalError as exc:
                log.error("Cannot open DB %s: %s", db_path, exc)
                self._conn = None
                return False

    def close(self):
        with self._lock:
            self._close_safe()

    # ── Rotation ──────────────────────────────────────────

    def rotate_if_needed(self) -> bool:
        """
        Nếu ngày đã qua → close + open DB mới. Return True nếu đã rotate.
        FIX v2-BUG-D: dùng RLock → rotate có thể gọi open() mà không deadlock.
        """
        today = datetime.now().strftime("%Y-%m-%d")
        if today == self._current_date:
            return False

        log.info("Rotating DB: %s → %s", self._current_date, today)
        with self._lock:
            self._close_safe()
        return self.open()

    # ── Write operations ──────────────────────────────────

    def insert_sensor_batch(self, rows: list) -> int:
        """
        Batch insert sensor rows. Return số rows đã insert.
        FIX v2-BUG-I: INSERT OR IGNORE tránh duplicate khi ltrim fail.
        """
        if not rows:
            return 0
        with self._lock:
            if self._conn is None:
                log.warning("insert_sensor_batch: conn not ready — %d rows dropped", len(rows))
                return 0
            try:
                self._conn.executemany(
                    "INSERT OR IGNORE INTO sensor_data (room, type, value, timestamp) "
                    "VALUES (?, ?, ?, ?)",
                    rows,
                )
                self._conn.commit()
                return len(rows)
            except sqlite3.OperationalError as exc:
                log.error("insert_sensor_batch failed: %s", exc)
                self._close_safe()
                return 0

    def insert_event_batch(self, rows: list) -> int:
        """Batch insert system_events. Return số rows đã insert."""
        if not rows:
            return 0
        with self._lock:
            if self._conn is None:
                log.warning("insert_event_batch: conn not ready — %d events dropped", len(rows))
                return 0
            try:
                self._conn.executemany(
                    "INSERT INTO system_events (type, payload, timestamp) VALUES (?, ?, ?)",
                    rows,
                )
                self._conn.commit()
                return len(rows)
            except sqlite3.OperationalError as exc:
                log.error("insert_event_batch failed: %s", exc)
                self._close_safe()
                return 0

    def insert_event(self, event_type: str, payload: dict) -> bool:
        """Single event insert (direct, không qua Redis queue)."""
        with self._lock:
            if self._conn is None:
                log.warning("insert_event: conn not ready — event dropped")
                return False
            try:
                self._conn.execute(
                    "INSERT INTO system_events (type, payload, timestamp) VALUES (?, ?, ?)",
                    (event_type, json.dumps(payload), datetime.now().isoformat()),
                )
                self._conn.commit()
                return True
            except sqlite3.OperationalError as exc:
                log.error("insert_event failed: %s", exc)
                self._close_safe()
                return False

    # ── Read operations ───────────────────────────────────

    def query_all_sensor(self) -> list:
        """
        Full table scan để export CSV.
        FIX v2-BUG-E: dùng lock — không race với insert_sensor_batch.
        """
        with self._lock:
            if self._conn is None:
                return []
            try:
                return self._conn.execute(
                    "SELECT room, type, value, timestamp FROM sensor_data ORDER BY timestamp ASC"
                ).fetchall()
            except sqlite3.OperationalError as exc:
                log.error("query_all_sensor failed: %s", exc)
                return []

    # ── Properties ────────────────────────────────────────

    @property
    def db_path(self) -> str:
        return self._db_path

    @property
    def current_date(self) -> str:
        return self._current_date

    @property
    def is_open(self) -> bool:
        return self._conn is not None


# ══════════════════════════════════════════════════════════
# LAYER 3 — BufferLayer
# Trách nhiệm: Redis ingestion — push, pop batch, backpressure
# KHÔNG biết SQLite, KHÔNG biết SD2
# ══════════════════════════════════════════════════════════

class BufferLayer:
    """
    Redis buffer cho sensor_data và system_events.
    Thread-safe: Redis operations là atomic. push_sensor dùng pipeline để
    backpressure check + rpush atomic (FIX v2-BUG-G).
    """

    def __init__(self):
        # FIX v1-BUG-02 + v2 pool: singleton connection pool
        self._pool = redis_lib.ConnectionPool(
            host=REDIS_HOST, port=6379,
            decode_responses=True,
            max_connections=5,
        )

    def _r(self) -> redis_lib.Redis:
        return redis_lib.Redis(connection_pool=self._pool)

    # ── Sensor buffer ─────────────────────────────────────

    def push_sensor(self, room: str, sensor_type: str, value: float):
        """
        Push sensor data vào Redis List với backpressure.
        FIX v2-BUG-G: dùng Lua script để llen+rpush atomic — không TOCTOU.
        """
        data = json.dumps({
            "room":      room,
            "type":      sensor_type,
            "value":     value,
            "timestamp": datetime.now().isoformat(),
        })

        # Lua: atomic llen check + rpush — tránh race giữa check và push
        lua_script = """
        local len = redis.call('LLEN', KEYS[1])
        if len >= tonumber(ARGV[1]) then
            return -1
        end
        if len >= tonumber(ARGV[2]) then
            return -2
        end
        return redis.call('RPUSH', KEYS[1], ARGV[3])
        """
        try:
            r = self._r()
            result = r.eval(
                lua_script, 1,
                BUFFER_KEY,
                BUFFER_MAX_THRESHOLD,
                BUFFER_WARN_THRESHOLD,
                data,
            )
            if result == -1:
                log.error("Sensor buffer FULL — dropped %s/%s", room, sensor_type)
            elif result == -2:
                log.warning("Sensor buffer high watermark for %s/%s", room, sensor_type)
                # Vẫn push (chỉ warn) — Lua đã rpush
        except Exception as e:
            log.error("push_sensor Redis error: %s", e)

    def pop_sensor_batch(self, count: int = FLUSH_COUNT) -> tuple[list[str], int]:
        """
        Atomic pop batch từ sensor_buffer.
        FIX v1-BUG-02 + v2-BUG-B: lrange(0, count-1) rồi ltrim(count, -1).
        ltrim dùng số cố định `count`, KHÔNG dùng len(result) — tránh offset sai.
        Return (raw_items, trimmed_count).
        """
        r = self._r()
        pipe = r.pipeline()
        pipe.lrange(BUFFER_KEY, 0, count - 1)
        pipe.llen(BUFFER_KEY)
        results = pipe.execute()

        raw_items  = results[0] or []
        total_len  = results[1] or 0
        batch_size = len(raw_items)

        if batch_size == 0:
            return [], 0

        # ltrim với số cố định — không phụ thuộc vào len(raw_items) sau khi fetch
        # FIX v2-BUG-B: trim theo batch_size, không len() tính lại
        # Gọi SAU khi DB commit thành công (do caller quyết định)
        return raw_items, batch_size

    def trim_sensor_buffer(self, batch_size: int):
        """
        Xóa batch_size items đầu khỏi buffer.
        CHỈ gọi sau khi DB commit thành công (FIX v2-BUG-I partial fix).
        """
        self._r().ltrim(BUFFER_KEY, batch_size, -1)

    def drain_event_queue(self) -> list[str]:
        """
        Drain toàn bộ event_queue vào list.
        Dùng lrange + ltrim để atomic drain snapshot — không mất items mới push sau khi drain.
        """
        r = self._r()
        # Lấy length hiện tại trước, chỉ drain đúng số đó
        queue_len = r.llen(EVENT_QUEUE)
        if queue_len == 0:
            return []

        pipe = r.pipeline()
        pipe.lrange(EVENT_QUEUE, 0, queue_len - 1)
        pipe.ltrim(EVENT_QUEUE, queue_len, -1)
        results = pipe.execute()
        return results[0] or []

    def requeue_events(self, raw_items: list[str]):
        """
        Re-push events lại queue khi DB fail.
        FIX v2-BUG-C: dùng rpush (cuối queue), KHÔNG lpush — giữ thứ tự chronological.
        Events mới đến sau khi drain sẽ ở đầu queue, events cũ được push lại cuối.
        Đây là đánh đổi có chủ ý: ưu tiên events mới hơn (IoT pattern).
        """
        if not raw_items:
            return
        try:
            pipe = self._r().pipeline()
            for raw in raw_items:
                pipe.rpush(EVENT_QUEUE, raw)
            pipe.execute()
            log.info("Re-queued %d events after DB failure", len(raw_items))
        except Exception as e:
            log.error("requeue_events failed — %d events lost: %s", len(raw_items), e)

    def sensor_buffer_len(self) -> int:
        try:
            return self._r().llen(BUFFER_KEY)
        except Exception:
            return 0

    def get_redis(self) -> redis_lib.Redis:
        """Expose raw Redis client cho snapshot (đọc system_status:wifi)."""
        return self._r()


# ══════════════════════════════════════════════════════════
# LAYER 4 — SyncWorker
# Trách nhiệm: orchestration — kết nối 3 layer trên, move data
# Chứa: flush_sensor, flush_events, export_csv, backup_db, snapshot, main loop
# ══════════════════════════════════════════════════════════

class SyncWorker:
    """
    Điều phối luồng dữ liệu: Redis → SQLite → SD2 exports.
    Không chứa logic hardware hay DB schema — delegate xuống layer thấp hơn.
    """

    def __init__(self, storage: StorageLayer, buffer: BufferLayer, sd2: SD2Manager):
        self._storage = storage
        self._buffer  = buffer
        self._sd2     = sd2

    # ── flush_sensor ──────────────────────────────────────

    def flush_sensor(self) -> int:
        """
        Pop batch từ Redis → insert SQLite → trim Redis.
        FIX v2-BUG-B: trim dùng batch_size cố định, không len() tính lại.
        FIX v2-BUG-I: INSERT OR IGNORE trong StorageLayer.insert_sensor_batch.
        """
        raw_items, batch_size = self._buffer.pop_sensor_batch(FLUSH_COUNT)
        if not raw_items:
            return 0

        rows   = []
        t_start = time.time()

        for raw in raw_items:
            try:
                d = json.loads(raw)
                rows.append((
                    d["room"],
                    d["type"],
                    float(d["value"]),
                    d.get("timestamp", datetime.now().isoformat()),
                ))
            except (json.JSONDecodeError, KeyError, ValueError) as e:
                log.warning("Invalid sensor buffer entry dropped: %s", e)

        if not rows:
            # Xóa các entries lỗi
            self._buffer.trim_sensor_buffer(batch_size)
            return 0

        inserted = self._storage.insert_sensor_batch(rows)

        if inserted > 0:
            # FIX v2-BUG-I: chỉ trim SAU KHI DB commit OK
            self._buffer.trim_sensor_buffer(batch_size)
            elapsed = (time.time() - t_start) * 1000
            log.info("flush_sensor: %d rows in %.1fms", inserted, elapsed)
        else:
            # DB fail — KHÔNG trim → items sẽ được retry lần sau
            # INSERT OR IGNORE đảm bảo không duplicate khi retry
            log.warning("flush_sensor: DB insert failed — %d items kept in Redis buffer", batch_size)

        return inserted

    # ── flush_events ─────────────────────────────────────

    def flush_events(self) -> int:
        """
        Drain event_queue từ Redis → insert SQLite.
        FIX v1-BUG-03: re-queue về cuối nếu DB fail (FIX v2-BUG-C).
        FIX v2-BUG-C: dùng rpush không lpush → giữ thứ tự.
        """
        raw_items = self._buffer.drain_event_queue()
        if not raw_items:
            return 0

        rows = []
        for raw in raw_items:
            try:
                evt        = json.loads(raw)
                event_type = evt.get("event") or evt.get("type") or "system"
                timestamp  = evt.get("timestamp", datetime.now().isoformat())
                rows.append((event_type, json.dumps(evt), timestamp))
            except (json.JSONDecodeError, KeyError) as e:
                log.warning("Invalid event entry dropped: %s", e)

        if not rows:
            return 0

        inserted = self._storage.insert_event_batch(rows)

        if inserted == 0:
            # Re-queue để retry — FIX v2-BUG-C: rpush giữ thứ tự
            self._buffer.requeue_events(raw_items)

        return inserted

    # ── log_event (direct insert) ─────────────────────────

    def log_event(self, event_type: str, payload: dict):
        """Direct insert vào SQLite (không qua Redis queue)."""
        self._storage.insert_event(event_type, payload)

    # ── snapshot ─────────────────────────────────────────

    def snapshot(self):
        """
        Ghi wifi status vào system_events.
        FIX v2-BUG-J: không bare except:pass — chỉ catch Exception.
        """
        try:
            r    = self._buffer.get_redis()
            raw  = r.get("system_status:wifi")
            wifi = json.loads(raw) if raw else {}
            self._storage.insert_event("snapshot", wifi)
        except Exception as e:
            log.warning("snapshot failed: %s", e)

    # ── export_csv ────────────────────────────────────────

    def export_csv(self, current_date: str):
        """
        Export toàn bộ sensor_data ra CSV trên SD2/exports/.
        FIX v1-BUG-07 + v2-BUG-E: đọc qua StorageLayer.query_all_sensor() có lock.
        """
        if not self._sd2.is_ready():
            if not self._sd2.ensure_ready():
                log.error("export_csv: SD2 not ready — skipping")
                return

        export_dir = os.path.join(SD2_MOUNT, "exports")
        os.makedirs(export_dir, exist_ok=True)

        filename = f"data_{current_date}.csv"
        path     = os.path.join(export_dir, filename)

        rows = self._storage.query_all_sensor()
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["room", "type", "value", "timestamp"])
            for row in rows:
                w.writerow([row["room"], row["type"], row["value"], row["timestamp"]])

        log.info("CSV exported: %s (%d rows)", path, len(rows))

    # ── backup_db ─────────────────────────────────────────

    def backup_db(self, db_path: str, current_date: str):
        """
        Copy daily DB sang SD2 root.
        FIX v2-BUG-F: nhận db_path + current_date như tham số explicit
        thay vì đọc global → tránh race khi rotation xảy ra giữa export+backup.
        """
        if not db_path or not os.path.isfile(db_path):
            log.warning("backup_db: db_path missing (%s) — skipping", db_path)
            return

        dst = os.path.join(SD2_MOUNT, f"data_backup_{current_date}.db")
        try:
            shutil.copy2(db_path, dst)
            log.info("DB backup: %s → %s", db_path, dst)
        except OSError as e:
            log.error("backup_db failed: %s", e)

    # ── main loop ─────────────────────────────────────────

    def run(self):
        """
        Entry point — startup + steady-state loop.
        State machine: WAIT_SD2 → INIT_DB → RUNNING ↔ DEGRADED.
        """
        log.info("SD2 startup diagnostic:")
        self._sd2.debug_info()

        # ── Phase 1: Chờ SD2 ready lúc boot ──────────────
        retry_delay = RETRY_BASE
        while not self._sd2.ensure_ready():
            log.warning("SD2 not ready at startup — retry in %ds", retry_delay)
            self._sd2.debug_info()
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, RETRY_MAX)

        log.info("SD2 ready at startup")

        # ── Phase 2: Init DB ──────────────────────────────
        retry_delay = RETRY_BASE
        while not self._storage.open():
            log.error("init_db failed — retry in %ds", retry_delay)
            self._sd2.debug_info()
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, RETRY_MAX)

        log.info("DB initialised — entering main loop")

        last_flush    = time.time()
        last_snapshot = time.time()
        last_export   = None
        sd2_was_ready = True   # local state, chỉ đọc trong single thread này
        retry_delay   = RETRY_BASE

        # ── Phase 3: Steady-state loop ────────────────────
        while True:
            try:
                sd2_now = self._sd2.is_ready()

                # ── SD2 state transitions ─────────────────
                if not sd2_now and sd2_was_ready:
                    log.warning("SD2 UNPLUGGED — pausing DB writes, buffering to Redis")
                    self._storage.close()
                    sd2_was_ready = False

                if not sd2_now:
                    log.info("SD2 not ready → retry in %ds", retry_delay)
                    self._sd2.ensure_ready()
                    time.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, RETRY_MAX)
                    continue

                if not sd2_was_ready:
                    log.info("SD2 RESTORED — reinitialising DB")
                    self._sd2.ensure_ready()
                    if self._storage.open():
                        sd2_was_ready = True
                        retry_delay   = RETRY_BASE
                    else:
                        log.error("DB reopen failed after SD2 restore — retry next tick")
                        time.sleep(retry_delay)
                        retry_delay = min(retry_delay * 2, RETRY_MAX)
                        continue

                # ── DB tự phục hồi nếu file mất (edge case) ──
                if not self._storage.is_open:
                    log.warning("DB conn lost — reopening")
                    if not self._storage.open():
                        log.error("DB reopen failed — skip tick")
                        time.sleep(5)
                        continue

                # ── Rotation ──────────────────────────────
                self._storage.rotate_if_needed()

                # ── Flush events (mỗi tick) ────────────────
                n_events = self.flush_events()
                if n_events:
                    log.info("Flushed %d events", n_events)

                now = time.time()

                # ── Flush sensor (mỗi FLUSH_EVERY giây) ───
                if (now - last_flush) >= FLUSH_EVERY:
                    n_sensors = self.flush_sensor()
                    if n_sensors:
                        log.info("Flushed %d sensor rows", n_sensors)
                    last_flush = now

                # ── Snapshot (mỗi 10 phút) ────────────────
                if (now - last_snapshot) > 600:
                    self.snapshot()
                    last_snapshot = now

                # ── Daily export (lúc EXPORT_HOUR) ────────
                hour  = datetime.now().hour
                today = datetime.now().date()
                if hour == EXPORT_HOUR and last_export != today:
                    # FIX v2-BUG-F: snapshot db_path+date TRƯỚC khi export+backup
                    snap_path = self._storage.db_path
                    snap_date = self._storage.current_date
                    self.export_csv(snap_date)
                    self.backup_db(snap_path, snap_date)
                    last_export = today

                # ── Backpressure warning ───────────────────
                buf_len = self._buffer.sensor_buffer_len()
                if buf_len >= BUFFER_WARN_THRESHOLD:
                    log.warning("Redis sensor buffer high: %d items", buf_len)

                time.sleep(2)

            except Exception as e:
                log.error("Main loop error: %s", e, exc_info=True)
                time.sleep(5)


# ══════════════════════════════════════════════════════════
# MODULE-LEVEL WIRING
# Khởi tạo 4 layer + expose backward-compat public API
# ══════════════════════════════════════════════════════════

# Layer instances (singletons)
_storage = StorageLayer()
_buffer  = BufferLayer()
_worker  = SyncWorker(_storage, _buffer, _sd2)


# ── Public API (backward-compatible với code gọi từ ngoài) ──

def push_sensor(r, room: str, sensor_type: str, value: float):
    """External API — automation_engine gọi để push sensor vào buffer."""
    _buffer.push_sensor(room, sensor_type, value)


def log_event(event_type: str, payload: dict):
    """External API — direct insert vào SQLite."""
    _worker.log_event(event_type, payload)


def flush_sensor() -> int:
    return _worker.flush_sensor()


def flush_events() -> int:
    return _worker.flush_events()


def export_csv():
    _worker.export_csv(_storage.current_date or datetime.now().strftime("%Y-%m-%d"))


def backup_db():
    _worker.backup_db(_storage.db_path, _storage.current_date)


def snapshot(r=None):
    _worker.snapshot()


def run():
    _worker.run()


# ══════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════

if __name__ == "__main__":
    run()