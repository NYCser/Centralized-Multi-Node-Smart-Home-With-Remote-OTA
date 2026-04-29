"""
gateway_main.py  — FIXED
═══════════════════════════
Fixes:
  BUG-03/08: conn double-close (ensure_admin nhận conn nhưng tự close, sau đó init_db close lần nữa)
  BUG-07 [CRITICAL]: Admin tạo bằng bcrypt nhưng login dùng SHA256 → login luôn fail
          Giải pháp: thống nhất dùng SHA256 (hashlib) giống all_routes.py
                    HOẶC dùng bcrypt toàn bộ — ta chọn SHA256 vì all_routes đã dùng SHA256
"""
import os
import sys
import hashlib
import threading
import sqlite3
from flask import Flask, request
from flask_cors import CORS

from app.main import app, socketio

from workers.firebase_sync import main as firebase_sync_main
import threading

t = threading.Thread(target=firebase_sync_main, daemon=True)
t.start()

CORS(app, resources={r"/*": {"origins": "*"}}, supports_credentials=True)

@app.after_request
def add_cors_headers(response):
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS,PATCH')
    return response

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DB_PATH    = os.getenv("DB_PATH",     "/data/smarthome.db")
REDIS_HOST = os.getenv("REDIS_HOST",  "localhost")
MQTT_HOST  = os.getenv("MQTT_BROKER", "localhost")


def _hash_pw(pw: str) -> str:
    """SHA256 — phải khớp với hash_pw() trong all_routes.py."""
    return hashlib.sha256(pw.encode()).hexdigest()


# ── 1. Khởi tạo DB ──────────────────────────────────────────────────────────

def init_db():
    schema_path = os.path.join(os.path.dirname(__file__), "storage/db_schema.sql")
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    if os.path.exists(schema_path):
        with open(schema_path, "r") as f:
            conn.executescript(f.read())
        print("[MAIN] DB schema applied")
    else:
        print("[MAIN] Warning: db_schema.sql not found")

    # FIX BUG-07/08: ensure_admin KHÔNG tự close conn — init_db quản lý lifecycle
    _ensure_admin(conn)

    conn.commit()
    conn.close()          # ← chỉ close 1 lần duy nhất ở đây
    print("[MAIN] DB init complete")


def _ensure_admin(conn: sqlite3.Connection):
    """
    FIX BUG-07: Dùng SHA256 (hashlib) để hash password admin,
    giống hệt hash_pw() trong all_routes.py.
    FIX BUG-08: Không tự gọi conn.close() — do caller quản lý.
    """
    admin_email = "admin@smarthome.local"
    admin_pw    = "admin123"

    existing = conn.execute(
        "SELECT id FROM users WHERE email=?", (admin_email,)
    ).fetchone()

    if not existing:
        pw_hash = _hash_pw(admin_pw)   # ← SHA256, khớp all_routes.py
        conn.execute(
            "INSERT INTO users (email, password, display_name, role) VALUES (?,?,?,?)",
            (admin_email, pw_hash, "Administrator", "admin")
        )
        print("[MAIN] Admin account created (SHA256 hash)")
    # KHÔNG conn.commit() hoặc conn.close() ở đây — init_db làm


# ── 2. Khởi động Workers ────────────────────────────────────────────────────

def start_workers():
    from bridge.message_bus import MessageBus
    from workers import safety_watchdog, automation_engine, data_syncer, network_watchdog

    bus = MessageBus.get_instance()
    bus.connect()

    threading.Thread(target=safety_watchdog.run,   name="SafetyWatchdog",  daemon=True).start()
    threading.Thread(target=network_watchdog.run,  name="NetworkWatchdog", daemon=True).start()
    threading.Thread(target=data_syncer.run,       name="DataSyncer",      daemon=True).start()

    # Firebase sync worker — chỉ start nếu credentials tồn tại
    fb_cred = os.getenv("FIREBASE_CRED", "/home/pi/GATEWAY/firebase-service-account.json")
    if os.path.isfile(fb_cred):
        try:
            from workers import firebase_sync
            threading.Thread(target=firebase_sync.run, name="FirebaseSync", daemon=True).start()
            print("[MAIN] FirebaseSync worker started")
        except ImportError:
            print("[MAIN] firebase_sync not found — skipping (run without Firebase)")
    else:
        print(f"[MAIN] Firebase cred not found at {fb_cred} — FirebaseSync disabled")

    # AutomationEngine: daemon=False vì nó là blocking pub/sub loop
    threading.Thread(target=automation_engine.run, name="AutomationEngine", daemon=False).start()

    print("[MAIN] All workers started")


# ── 3. Start API ─────────────────────────────────────────────────────────────

def start_api():
    from app.api.routes.all_routes import (
        auth_bp, sensors_bp, devices_bp, automation_bp,
        logs_bp, rfid_bp, wifi_bp, ota_bp, system_bp
    )

    for bp in [auth_bp, sensors_bp, devices_bp, automation_bp,
               logs_bp, rfid_bp, wifi_bp, ota_bp, system_bp]:
        try:
            app.register_blueprint(bp, url_prefix='')
            print(f"[API] Registered: {bp.name}")
        except Exception as e:
            print(f"[API] Error registering {bp.name}: {e}")

    port = int(os.getenv("API_PORT", 5000))
    print("=" * 55)
    print(f"[MAIN] Gateway LIVE → http://0.0.0.0:{port}/")
    print("=" * 55)

    # allow_unsafe_werkzeug=True chỉ dùng dev — production dùng gunicorn
    socketio.run(app, host="0.0.0.0", port=port, allow_unsafe_werkzeug=True)


# ── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 55)
    print("  SmartHome Gateway — Starting")
    print("=" * 55)
    init_db()
    start_workers()
    start_api()
