-- ═══════════════════════════════════════════════════════════
-- SmartHome Local - SQLite Schema  (UPDATED)
-- ═══════════════════════════════════════════════════════════

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ──────────────────────────────────────────────────────────
-- 1. NGƯỜI DÙNG & PHIÊN
-- ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS users (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    email        TEXT    UNIQUE NOT NULL,
    password     TEXT    NOT NULL,               -- sha256
    display_name TEXT    DEFAULT '',
    role         TEXT    DEFAULT 'user',         -- user | admin
    created_at   TEXT    DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS sessions (
    token       TEXT    PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    expires_at  TEXT    NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

-- ──────────────────────────────────────────────────────────
-- 2. CẤU TRÚC NHÀ
-- ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS rooms (
    id          TEXT    PRIMARY KEY,
    name        TEXT    NOT NULL,
    icon        TEXT    DEFAULT 'home',
    created_at  TEXT    DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS devices (
    id          TEXT    PRIMARY KEY,
    room_id     TEXT    NOT NULL,
    name        TEXT    NOT NULL,
    type        TEXT    NOT NULL,
    FOREIGN KEY(room_id) REFERENCES rooms(id)
);

-- ──────────────────────────────────────────────────────────
-- 3. DỮ LIỆU CẢM BIẾN
-- Thêm firebase_synced để firebase_sync.py biết cái nào chưa push
-- ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sensor_data (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    room             TEXT    NOT NULL,
    type             TEXT    NOT NULL,
    value            REAL    NOT NULL,
    timestamp        TEXT    DEFAULT (datetime('now','localtime')),
    firebase_synced  INTEGER DEFAULT 0   -- 0=chưa sync, 1=đã sync lên Firebase
);
CREATE INDEX IF NOT EXISTS idx_sensor_room_ts  ON sensor_data(room, type, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_sensor_unsynced ON sensor_data(firebase_synced) WHERE firebase_synced=0;

-- ──────────────────────────────────────────────────────────
-- 4. TRẠNG THÁI THIẾT BỊ (realtime snapshot)
-- ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS device_status (
    room        TEXT    NOT NULL,
    device_id   TEXT    NOT NULL,
    is_on       INTEGER DEFAULT 0,
    source      TEXT    DEFAULT 'unknown',
    updated_at  TEXT    DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (room, device_id)
);

-- ──────────────────────────────────────────────────────────
-- 5. CẢNH BÁO HỆ THỐNG
-- ──────────────────────────────────────────────────────────
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

-- ──────────────────────────────────────────────────────────
-- 6. THÔNG BÁO
-- ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS notifications (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    type        TEXT    NOT NULL,
    title       TEXT    NOT NULL,
    message     TEXT    NOT NULL,
    is_read     INTEGER DEFAULT 0,
    room        TEXT    DEFAULT '',
    created_at  TEXT    DEFAULT (datetime('now','localtime'))
);

-- ──────────────────────────────────────────────────────────
-- 7. LOG ĐĂNG NHẬP
-- ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS login_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    email       TEXT,
    success     INTEGER DEFAULT 0,
    ip_address  TEXT,
    device_hint TEXT,
    user_agent  TEXT,
    reason      TEXT,
    timestamp   TEXT    DEFAULT (datetime('now','localtime'))
);

-- ──────────────────────────────────────────────────────────
-- 8. LOG RA VÀO CỬA RFID
-- ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS access_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    room        TEXT,
    uid         TEXT,
    user_name   TEXT,
    action      TEXT,
    success     INTEGER DEFAULT 0,
    duration_s  INTEGER,
    timestamp   TEXT    DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_access_room_ts ON access_logs(room, timestamp DESC);

-- ──────────────────────────────────────────────────────────
-- 9. LOG TỰ ĐỘNG HÓA
-- ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS automation_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    room        TEXT,
    scenario    TEXT,
    actions     TEXT,
    triggered_by TEXT,
    timestamp   TEXT    DEFAULT (datetime('now','localtime'))
);

-- ──────────────────────────────────────────────────────────
-- 10. THẺ RFID
-- ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS rfid_cards (
    uid         TEXT    PRIMARY KEY,
    owner_name  TEXT    DEFAULT '',
    is_active   INTEGER DEFAULT 1,
    created_at  TEXT    DEFAULT (datetime('now','localtime'))
);

-- ──────────────────────────────────────────────────────────
-- 11. RULES TỰ ĐỘNG HÓA
-- Thêm co2_threshold (fix BUG-H-01)
-- ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS automations (
    room_id         TEXT    PRIMARY KEY,
    enabled         INTEGER DEFAULT 1,
    fan_threshold   REAL,
    light_threshold REAL,
    gas_threshold   REAL    DEFAULT 600,
    co2_threshold   REAL    DEFAULT 1000   -- ppm, thêm mới cho BUG-H-01
);

-- ──────────────────────────────────────────────────────────
-- 12. LỊCH HẸN GIỜ
-- FIX BUG-C-05: enabled=1 mặc định, KHÔNG set 0 sau khi chạy
-- Dùng last_run TEXT để track lần chạy cuối (HH:MM YYYY-MM-DD)
-- ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS schedules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id     TEXT    NOT NULL,
    device_id   TEXT    NOT NULL,
    action      TEXT    NOT NULL,
    time        TEXT    NOT NULL,               -- HH:MM
    enabled     INTEGER DEFAULT 1,
    last_run    TEXT    DEFAULT '',             -- FIX BUG-C-05: track lần chạy
    created_at  TEXT    DEFAULT (datetime('now','localtime'))
);

-- ──────────────────────────────────────────────────────────
-- 13. OTA FIRMWARE
-- ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ota_logs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    room         TEXT NOT NULL,
    filename     TEXT NOT NULL,
    url          TEXT NOT NULL,
    version      TEXT DEFAULT 'unknown',
    release_notes TEXT DEFAULT '',
    triggered_by TEXT DEFAULT '',
    status       TEXT DEFAULT 'pending',
    created_at   DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- ──────────────────────────────────────────────────────────
-- 14. SYSTEM SNAPSHOTS
-- ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS system_snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    wifi_ssid    TEXT,
    wifi_status  TEXT,
    room_count   INTEGER DEFAULT 0,
    device_count INTEGER DEFAULT 0,
    alert_count  INTEGER DEFAULT 0,
    timestamp    TEXT    DEFAULT (datetime('now','localtime'))
);

-- ──────────────────────────────────────────────────────────
-- 15. SEED DATA MẶC ĐỊNH
-- Admin password: admin123 (SHA256)
-- ──────────────────────────────────────────────────────────
INSERT OR IGNORE INTO users (email, password, display_name, role)
VALUES ('ycao800@gmail.com',
        '240be518fabd2724ddb6f04eeb1da5967448d7e831c08c8fa822809f74c720a9', -- C.nyy192625178 SHA256
        'Admin', 'admin');

INSERT OR IGNORE INTO rooms (id, name, icon) VALUES
    ('bedroom_01',    'Phòng Ngủ',  'bed'),
    ('kitchen_01',    'Nhà Bếp',    'utensils'),
    ('living_room_01','Phòng Khách','sofa');

INSERT OR IGNORE INTO devices (id, room_id, name, type) VALUES
    ('fan_bd_1',   'bedroom_01',     'Quạt',  'fan'),
    ('light_bd_1', 'bedroom_01',     'Đèn',   'light'),
    ('fan_kt_1',   'kitchen_01',     'Quạt',  'fan'),
    ('light_kt_1', 'kitchen_01',     'Đèn',   'light'),
    ('fan_lv_1',   'living_room_01', 'Quạt',  'fan'),
    ('light_lv_1', 'living_room_01', 'Đèn',   'light');

INSERT OR IGNORE INTO automations (room_id, enabled, fan_threshold, gas_threshold, co2_threshold)
VALUES
    ('bedroom_01',     1, 30.0, 600, 1000),
    ('kitchen_01',     1, 32.0, 600, 1000),
    ('living_room_01', 1, 30.0, 600, 1000);