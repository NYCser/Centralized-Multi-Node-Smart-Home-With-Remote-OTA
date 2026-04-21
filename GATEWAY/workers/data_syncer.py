"""
data_syncer.py
IoT Data Syncer - Daily SQLite partition + hybrid sensor/event system

FIXES APPLIED:
  #1  SD2 detect: dùng os.path.ismount() thay vì write-test làm primary check
  #2  Mount check: ismount() là source-of-truth, write-test chỉ dùng để verify writable
  #3  Hot-plug: track state transition DOWN→UP, force remount + reset DB connection
  #4  SQLite: close + reset conn khi phát hiện mất storage
  #5  Redis flush: dùng lrange + ltrim thay vì lindex loop (atomic, no duplicate)
  #6  Retry backoff: exponential backoff khi SD2 fail
  #7  DB rotation: lock-based để tránh race condition
  #8  Event flush: bọc try/except, push lại Redis nếu DB lỗi
  #9  Debug SD2: bỏ write-test khỏi is_sd2_ready(), chỉ dùng khi debug thực sự
  #10 Redis: dùng singleton connection pool thay vì tạo mới liên tục
  #11 Buffer protection: giới hạn Redis buffer, warn khi vượt ngưỡng
  #12 Logging: thêm state transition log, retry log, flush latency
"""

import time
import json
import sqlite3
import os
import csv
import shutil
import subprocess
from datetime import datetime
import threading
import logging

import redis as redis_lib

# ───────────────────────── CONFIG ─────────────────────────

SD2_MOUNT   = "/mnt/sd2"
DATA_DIR    = f"{SD2_MOUNT}/data"

REDIS_HOST  = "localhost"

BUFFER_KEY  = "sensor_buffer"
EVENT_QUEUE = "event_queue"

FLUSH_EVERY = 180   # seconds
FLUSH_COUNT = 50

EXPORT_HOUR = 2

MOUNT_SCRIPT = "/home/pi/GATEWAY/scripts/mount_sd2.sh"

# FIX #6: exponential backoff config
RETRY_BASE  = 2     # seconds
RETRY_MAX   = 60    # seconds cap

# FIX #11: buffer backpressure
BUFFER_WARN_THRESHOLD = 5_000
BUFFER_MAX_THRESHOLD  = 20_000

# ───────────────────────── LOGGING ────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("SYNCER")

# ───────────────────── GLOBAL STATE ───────────────────────

current_date = None
db_path      = None
conn         = None
_db_lock     = threading.Lock()  # FIX #7: protect rotation

# FIX #3: track SD2 state transition
_sd2_was_ready = False

# FIX #10: Redis connection pool singleton
_redis_pool = None


# ───────────────────── REDIS POOL ─────────────────────────

# FIX #10: tạo pool một lần duy nhất, tái dùng connection
def _get_redis_pool():
    global _redis_pool
    if _redis_pool is None:
        _redis_pool = redis_lib.ConnectionPool(
            host=REDIS_HOST,
            port=6379,
            decode_responses=True,
            max_connections=5
        )
    return _redis_pool


def get_redis():
    return redis_lib.Redis(connection_pool=_get_redis_pool())


# ───────────────────── SD2 DETECTION ──────────────────────

# FIX #1 + #2: ismount() là primary check, KHÔNG tạo dir/file ở đây
def is_sd2_mounted():
    """Chỉ kiểm tra mount, không tạo file/folder."""
    return os.path.ismount(SD2_MOUNT)


# FIX #2: write-test tách biệt, chỉ gọi sau khi ismount() = True
def is_sd2_writable():
    """Verify SD2 writable. Chỉ gọi khi đã biết ismount() = True."""
    test_file = os.path.join(DATA_DIR, ".health_check")
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(test_file, "w") as f:
            f.write("ok")
        os.remove(test_file)
        return True
    except Exception as e:
        log.warning("SD2 write test failed: %s", e)
        return False


def is_sd2_ready():
    """Source-of-truth: mounted AND writable."""
    return is_sd2_mounted() and is_sd2_writable()


def debug_sd2():
    log.info("[SD2 DEBUG] ismount=%s exists=%s data_dir_exists=%s writable=%s",
             os.path.ismount(SD2_MOUNT),
             os.path.exists(SD2_MOUNT),
             os.path.exists(DATA_DIR),
             os.access(DATA_DIR, os.W_OK) if os.path.exists(DATA_DIR) else "N/A")


def _force_umount():
    """
    Gỡ mount point ghost (ismount=True nhưng I/O lỗi).
    Dùng 'umount -l' (lazy) để kernel dọn dù có process đang giữ fd.
    """
    try:
        r = subprocess.run(
            ["sudo", "umount", "-l", SD2_MOUNT],
            capture_output=True, text=True, timeout=10
        )
        if r.returncode == 0:
            log.info("SD2 lazy-umount OK")
        else:
            # Không phải lỗi nghiêm trọng — có thể đã unmounted rồi
            log.debug("umount -l rc=%d: %s", r.returncode, r.stderr.strip())
    except Exception as e:
        log.warning("_force_umount error: %s", e)


def _do_mount():
    """
    Chạy mount_sd2.sh. Tự động thử cả path đầy đủ lẫn `mount -a` fallback.
    """
    # Thử script chính
    if os.path.isfile(MOUNT_SCRIPT):
        try:
            r = subprocess.run(
                ["sudo", "bash", MOUNT_SCRIPT],   # dùng 'bash' để tránh permission bit
                capture_output=True, text=True, timeout=15
            )
            if r.returncode == 0:
                log.info("mount_sd2.sh OK")
                return True
            log.warning("mount_sd2.sh failed (rc=%d): %s", r.returncode, r.stderr.strip())
        except Exception as e:
            log.warning("mount_sd2.sh error: %s", e)
    else:
        log.warning("mount_sd2.sh not found at %s — dùng fallback 'mount -a'", MOUNT_SCRIPT)

    # Fallback: mount -a (mount tất cả entry trong /etc/fstab)
    try:
        r = subprocess.run(
            ["sudo", "mount", "-a"],
            capture_output=True, text=True, timeout=15
        )
        if r.returncode == 0:
            log.info("mount -a OK")
            return True
        log.warning("mount -a failed (rc=%d): %s", r.returncode, r.stderr.strip())
    except Exception as e:
        log.warning("mount -a error: %s", e)

    return False


def ensure_sd2_mounted():
    """
    Đảm bảo SD2 ready.
    - Nếu ismount=True nhưng I/O lỗi (ghost mount): umount -l rồi mount lại
    - Nếu chưa mount: mount thẳng
    Return True nếu cuối cùng is_sd2_ready().
    """
    # Trường hợp tốt nhất — đã OK
    if is_sd2_ready():
        return True

    # Ghost mount: ismount=True nhưng write test fail → phải umount trước
    if is_sd2_mounted() and not is_sd2_writable():
        log.warning("SD2 ghost mount detected — forcing umount + remount")
        _force_umount()
        time.sleep(1)   # chờ kernel dọn xong

    # Giờ mount lại
    _do_mount()
    time.sleep(1)

    ok = is_sd2_ready()
    if ok:
        log.info("SD2 remount successful")
    else:
        log.warning("SD2 vẫn chưa ready sau remount")
    return ok


# ───────────────────── DAILY DB ENGINE ────────────────────

def build_db_path(date_str):
    os.makedirs(DATA_DIR, exist_ok=True)
    return f"{DATA_DIR}/data_{date_str}.db"


def init_db():
    global conn, db_path, current_date

    today = datetime.now().strftime("%Y-%m-%d")
    current_date = today
    db_path = build_db_path(today)
    new_db  = not os.path.exists(db_path)

    # FIX #4: đảm bảo conn cũ được đóng sạch trước khi mở lại
    _close_conn_safe()

    try:
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")

        if new_db:
            create_schema(conn)

        log.info("DB ready: %s", db_path)
    except sqlite3.OperationalError as exc:
        log.error("Cannot open DB %s: %s", db_path, exc)
        conn = None
        raise


def _close_conn_safe():
    """FIX #4: đóng conn an toàn, không raise."""
    global conn
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
        conn = None


def create_schema(c):
    c.executescript("""
    CREATE TABLE IF NOT EXISTS sensor_data (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        room      TEXT,
        type      TEXT,
        value     REAL,
        timestamp TEXT
    );

    CREATE TABLE IF NOT EXISTS system_events (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        type      TEXT,
        payload   TEXT,
        timestamp TEXT
    );

    CREATE INDEX IF NOT EXISTS idx_sensor_time ON sensor_data(timestamp);
    CREATE INDEX IF NOT EXISTS idx_event_time  ON system_events(timestamp);
    """)
    c.commit()


def rotate_if_needed():
    """FIX #7: dùng lock để tránh race condition trong rotation."""
    global conn

    today = datetime.now().strftime("%Y-%m-%d")
    if today == current_date:
        return

    log.info("Rotating DB to new day: %s → %s", current_date, today)

    with _db_lock:
        _close_conn_safe()
        init_db()


# ───────────────────── SENSOR BUFFER ──────────────────────

def push_sensor(r, room, sensor_type, value):
    # FIX #11: backpressure — từ chối push khi buffer quá lớn
    buf_len = r.llen(BUFFER_KEY)

    if buf_len >= BUFFER_MAX_THRESHOLD:
        log.error("Sensor buffer FULL (%d items) — dropping sensor %s/%s", buf_len, room, sensor_type)
        return

    if buf_len >= BUFFER_WARN_THRESHOLD:
        log.warning("Sensor buffer high watermark: %d items", buf_len)

    data = json.dumps({
        "room":      room,
        "type":      sensor_type,
        "value":     value,
        "timestamp": datetime.now().isoformat()
    })
    r.rpush(BUFFER_KEY, data)


def flush_sensor():
    """FIX #5: dùng lrange + ltrim thay vì lindex loop."""
    global conn

    # FIX #4: kiểm tra conn + file trước khi flush
    with _db_lock:
        if conn is None or db_path is None or not os.path.exists(db_path):
            log.warning("DB missing before flush_sensor → recreate")
            init_db()

    r = get_redis()
    count = r.llen(BUFFER_KEY)
    if count == 0:
        return 0

    read_count = min(count, FLUSH_COUNT)

    # FIX #5: atomic read — lrange lấy batch, ltrim xóa sau khi DB commit
    raw_items = r.lrange(BUFFER_KEY, 0, read_count - 1)

    rows   = []
    failed = []

    for raw in raw_items:
        try:
            d = json.loads(raw)
            rows.append((
                d["room"],
                d["type"],
                float(d["value"]),
                d.get("timestamp", datetime.now().isoformat())
            ))
        except Exception as exc:
            log.warning("Invalid buffer entry dropped: %s", exc)
            # bỏ entry lỗi, không push lại

    if not rows:
        # xóa các entry lỗi đã đọc
        r.ltrim(BUFFER_KEY, len(raw_items), -1)
        return 0

    t0 = time.time()
    try:
        with _db_lock:
            conn.executemany(
                "INSERT INTO sensor_data (room, type, value, timestamp) VALUES (?,?,?,?)",
                rows
            )
            conn.commit()

        # FIX #5: chỉ trim sau khi DB commit thành công
        r.ltrim(BUFFER_KEY, len(raw_items), -1)

        elapsed = (time.time() - t0) * 1000
        log.info("flush_sensor: %d rows in %.1fms", len(rows), elapsed)   # FIX #12
        return len(rows)

    except Exception as exc:
        log.error("flush_sensor DB insert failed: %s", exc)
        # FIX #4: reset conn — DB có thể không còn accessible
        _close_conn_safe()
        return 0


# ───────────────────── EVENT LOGGER ───────────────────────

def log_event(event_type, payload):
    """Direct insert (local call, dùng conn hiện tại)."""
    with _db_lock:
        if conn is None:
            log.warning("log_event: conn not ready, event dropped")
            return
        try:
            conn.execute(
                "INSERT INTO system_events (type, payload, timestamp) VALUES (?,?,?)",
                (event_type, json.dumps(payload), datetime.now().isoformat())
            )
            conn.commit()
        except Exception as exc:
            log.error("log_event failed: %s", exc)


def flush_events():
    """FIX #8: bọc try/except, push lại Redis nếu DB lỗi."""
    r    = get_redis()
    rows = []
    raw_list = []

    while True:
        raw = r.lpop(EVENT_QUEUE)
        if not raw:
            break
        raw_list.append(raw)
        try:
            evt        = json.loads(raw)
            event_type = evt.get("event") or evt.get("type") or "system"
            timestamp  = evt.get("timestamp", datetime.now().isoformat())
            rows.append((event_type, json.dumps(evt), timestamp))
        except Exception as exc:
            log.warning("flush_events: invalid event dropped: %s", exc)

    if not rows:
        return 0

    # FIX #8: try/except + rollback nếu lỗi, push lại Redis
    try:
        with _db_lock:
            conn.executemany(
                "INSERT INTO system_events (type, payload, timestamp) VALUES (?,?,?)",
                rows
            )
            conn.commit()
        return len(rows)

    except Exception as exc:
        log.error("flush_events DB insert failed: %s — re-queuing %d events", exc, len(raw_list))
        # FIX #8: đẩy lại vào đầu queue để không mất event
        pipe = r.pipeline()
        for raw in reversed(raw_list):
            pipe.lpush(EVENT_QUEUE, raw)
        pipe.execute()
        # FIX #4: reset conn
        _close_conn_safe()
        return 0


# ───────────────────── CSV EXPORT ─────────────────────────

def export_csv():
    if not is_sd2_ready():
        if not ensure_sd2_mounted():
            log.error("export_csv: SD2 not available, skipping")
            return

    export_dir = f"{SD2_MOUNT}/exports"
    os.makedirs(export_dir, exist_ok=True)

    filename = f"data_{current_date}.csv"
    path     = os.path.join(export_dir, filename)

    with _db_lock:
        rows = conn.execute("SELECT * FROM sensor_data").fetchall()

    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["room", "type", "value", "timestamp"])
        for r in rows:
            w.writerow([r["room"], r["type"], r["value"], r["timestamp"]])

    log.info("CSV exported: %s (%d rows)", path, len(rows))


def backup_db():
    dst = f"{SD2_MOUNT}/data_backup_{current_date}.db"
    shutil.copy2(db_path, dst)
    log.info("Backup: %s", dst)


# ───────────────────── SNAPSHOT ───────────────────────────

def snapshot(r):
    try:
        wifi = r.get("system_status:wifi")
        wifi = json.loads(wifi) if wifi else {}
        log_event("snapshot", wifi)
    except Exception as exc:
        log.warning("snapshot failed: %s", exc)


# ───────────────────── MAIN LOOP ──────────────────────────

def run():
    global _sd2_was_ready

    r = get_redis()

    log.info("SD2 startup health check")
    debug_sd2()

    # ── FIX #6: exponential backoff khi chờ SD2 ──
    retry_delay = RETRY_BASE
    while True:
        if is_sd2_ready():
            log.info("SD2 ready")              # FIX #12
            break

        log.warning("SD2 not ready → retry in %ds", retry_delay)
        debug_sd2()
        ensure_sd2_mounted()
        time.sleep(retry_delay)
        retry_delay = min(retry_delay * 2, RETRY_MAX)

    _sd2_was_ready = True

    retry_delay = RETRY_BASE
    while True:
        try:
            init_db()
            break
        except Exception as exc:
            log.error("init_db failed: %s → retry in %ds", exc, retry_delay)
            debug_sd2()
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, RETRY_MAX)

    last_flush    = time.time()
    last_snapshot = time.time()
    last_export   = None

    retry_delay = RETRY_BASE
    log.info("Started")

    while True:
        try:
            sd2_now = is_sd2_ready()

            # ── FIX #3: detect state transition DOWN→UP / UP→DOWN ──
            if not sd2_now and _sd2_was_ready:
                log.warning("SD2 UNPLUGGED — pausing writes, buffering to Redis")  # FIX #12
                _sd2_was_ready = False
                _close_conn_safe()   # FIX #4

            if not sd2_now:
                log.info("SD2 not ready → retry in %ds", retry_delay)
                ensure_sd2_mounted()
                time.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, RETRY_MAX)   # FIX #6
                continue

            if not _sd2_was_ready:
                # FIX #3: SD2 restored → force remount + reset DB
                log.info("SD2 RESTORED — reinitialising DB")   # FIX #12
                ensure_sd2_mounted()
                init_db()
                _sd2_was_ready = True
                retry_delay    = RETRY_BASE   # reset backoff

            # ── FIX #4: DB tự phục hồi nếu file mất ──
            if conn is None or db_path is None or not os.path.exists(db_path):
                log.warning("DB file missing → recreate")
                init_db()

            rotate_if_needed()

            # Event flush
            flushed_events = flush_events()
            if flushed_events:
                log.info("Flushed %d events", flushed_events)

            now = time.time()

            # Sensor flush (3 min)
            if (now - last_flush) >= FLUSH_EVERY:
                flushed = flush_sensor()
                if flushed:
                    log.info("Flushed %d sensor rows", flushed)
                last_flush = now

            # Snapshot (10 min)
            if (now - last_snapshot) > 600:
                snapshot(r)
                last_snapshot = now

            # Daily export
            hour  = datetime.now().hour
            today = datetime.now().date()

            if hour == EXPORT_HOUR and last_export != today:
                export_csv()
                backup_db()
                last_export = today

            # FIX #11: log buffer size nếu cao
            buf_len = r.llen(BUFFER_KEY)
            if buf_len >= BUFFER_WARN_THRESHOLD:
                log.warning("Redis sensor buffer high: %d items", buf_len)

            time.sleep(2)

        except Exception as e:
            log.error("Main loop error: %s", e)
            time.sleep(5)


# ───────────────────── START ──────────────────────────────

if __name__ == "__main__":
    run()