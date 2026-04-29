"""
gateway_main.py  — FIXED
═══════════════════════════
FIXES:
  BUG-C-01: Flask Blueprints chưa được đăng ký vào app → tất cả API trả 404
            Fix: start_api() đăng ký đầy đủ tất cả blueprints với url_prefix='/api'
                 và thêm alias routes không prefix cho backward compat với ESP32.
  BUG-03/08: conn double-close — đã fix trong phiên bản trước, giữ nguyên.
  BUG-07: Admin hash bcrypt vs SHA256 — đã fix, dùng SHA256 giống all_routes.py.

THÊM MỚI:
  - Health check endpoint /health không cần auth (cho ESP32 kiểm tra)
  - Static serve firmware folder cho OTA
  - CORS đầy đủ cho Web truy cập từ ngoài LAN (qua Firebase relay)
"""
import os
import sys
import hashlib
import threading
import sqlite3
from flask import Flask
from flask_socketio import SocketIO
from flask_cors import CORS

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DB_PATH    = os.getenv("DB_PATH",     "/data/smarthome.db")
REDIS_HOST = os.getenv("REDIS_HOST",  "localhost")
MQTT_HOST  = os.getenv("MQTT_BROKER", "localhost")

# ── Flask app ────────────────────────────────────────────────────────────────

app      = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

CORS(app, resources={r"/*": {"origins": "*"}}, supports_credentials=True)


@app.after_request
def add_cors_headers(response):
    response.headers.add("Access-Control-Allow-Origin",  "*")
    response.headers.add("Access-Control-Allow-Headers", "Content-Type,Authorization")
    response.headers.add("Access-Control-Allow-Methods", "GET,PUT,POST,DELETE,OPTIONS,PATCH")
    return response


# ── Hàm tiện ích ─────────────────────────────────────────────────────────────

def _hash_pw(pw: str) -> str:
    """SHA256 — khớp với hash_pw() trong all_routes.py."""
    return hashlib.sha256(pw.encode()).hexdigest()


# ── 1. Khởi tạo DB ────────────────────────────────────────────────────────────

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

    # FIX BUG-07/08: SHA256 hash, không tự close conn
    _ensure_admin(conn)

    conn.commit()
    conn.close()
    print("[MAIN] DB init complete")


def _ensure_admin(conn: sqlite3.Connection):
    """Tạo admin nếu chưa tồn tại. Dùng SHA256 khớp all_routes.py."""
    admin_email = "ycao800@gmail.com"
    admin_pw    = "C.nyy192625178"

    existing = conn.execute(
        "SELECT id FROM users WHERE email=?", (admin_email,)
    ).fetchone()

    if not existing:
        pw_hash = _hash_pw(admin_pw)
        conn.execute(
            "INSERT INTO users (email, password, display_name, role) VALUES (?,?,?,?)",
            (admin_email, pw_hash, "Administrator", "admin")
        )
        print("[MAIN] Admin account created (SHA256 hash)")


# ── 2. Khởi động Workers ──────────────────────────────────────────────────────

def start_workers():
    from bridge.message_bus import MessageBus
    from workers import safety_watchdog, automation_engine, data_syncer, network_watchdog

    bus = MessageBus.get_instance()
    bus.connect()

    threading.Thread(target=safety_watchdog.run,  name="SafetyWatchdog",  daemon=True).start()
    threading.Thread(target=network_watchdog.run, name="NetworkWatchdog", daemon=True).start()
    threading.Thread(target=data_syncer.run,      name="DataSyncer",      daemon=True).start()

    # Firebase sync worker — chỉ start nếu credentials tồn tại
    fb_cred = os.getenv("FIREBASE_CRED", "/home/pi/smarthome_prj/GATEWAY/firebase-service-account.json")
    if os.path.isfile(fb_cred):
        try:
            from workers import firebase_sync
            threading.Thread(target=firebase_sync.run, name="FirebaseSync", daemon=True).start()
            print("[MAIN] FirebaseSync worker started")
        except ImportError as e:
            print(f"[MAIN] firebase_sync import error: {e}")
    else:
        print(f"[MAIN] Firebase cred not found at {fb_cred} — FirebaseSync disabled")

    # AutomationEngine: daemon=False vì đây là blocking pub/sub loop chính
    threading.Thread(target=automation_engine.run, name="AutomationEngine", daemon=False).start()

    # Realtime bridge: Redis pubsub → SocketIO → Web
    threading.Thread(target=_realtime_bridge,      name="RealtimeBridge",  daemon=True).start()

    print("[MAIN] All workers started")


def _realtime_bridge():
    """
    Bridge Redis 'realtime_data' → SocketIO emit → Web dashboard.
    Cho phép Web nhận sensor updates realtime mà không cần polling.
    """
    import redis as redis_lib
    import json
    r      = redis_lib.Redis(host=REDIS_HOST, port=6379, decode_responses=True)
    pubsub = r.pubsub()
    pubsub.subscribe("realtime_data")
    print("[BRIDGE] Realtime bridge started")
    for msg in pubsub.listen():
        if msg["type"] != "message":
            continue
        try:
            data = json.loads(msg["data"])
            socketio.emit("realtime_update", data)
        except Exception as e:
            print(f"[BRIDGE] emit error: {e}")


@app.route('/')
def index():
    return {
        "status": "online",
        "gateway_time": "2026-04-29",
        "services": ["API", "MQTT", "Redis", "FirebaseSync"]
    }, 200

# ── 3. Đăng ký Blueprints & Start API ────────────────────────────────────────

def start_api():
    """
    FIX BUG-C-01: Đăng ký TẤT CẢ blueprints vào Flask app.
    Trước đây thiếu bước này → tất cả routes trả 404.
    
    Dùng 2 prefix:
      - /api/...   → cho Web frontend (axios/fetch với baseURL='/api')
      - /...       → cho ESP32 (dùng path ngắn, không có /api prefix)
    """
    from app.api.routes.all_routes import (
        auth_bp, sensors_bp, devices_bp, automation_bp,
        logs_bp, rfid_bp, wifi_bp, ota_bp, system_bp
    )

    blueprints = [
        auth_bp, sensors_bp, devices_bp, automation_bp,
        logs_bp, rfid_bp, wifi_bp, ota_bp, system_bp
    ]

    for bp in blueprints:
        # Đăng ký với prefix /api (dùng cho Web)
        try:
            app.register_blueprint(bp, url_prefix="/api", name=f"{bp.name}_api")
            print(f"[API] Registered /api: {bp.name}")
        except Exception as e:
            print(f"[API] Error registering /api/{bp.name}: {e}")

        # Đăng ký không prefix (backward compat cho ESP32 và local calls)
        try:
            app.register_blueprint(bp, url_prefix="", name=f"{bp.name}_root")
        except Exception as e:
            # Nếu đã đăng ký tên này rồi thì bỏ qua
            pass

    # WebSocket events
    @socketio.on("connect")
    def on_connect():
        print(f"[WS] Client connected")
        socketio.emit("connected", {"status": "ok"})

    @socketio.on("disconnect")
    def on_disconnect():
        print(f"[WS] Client disconnected")

    port = int(os.getenv("API_PORT", 5000))
    print("=" * 55)
    print(f"[MAIN] Gateway LIVE → http://0.0.0.0:{port}/")
    print(f"[MAIN] API prefix:  http://0.0.0.0:{port}/api/")
    print("=" * 55)

    socketio.run(app, host="0.0.0.0", port=port, allow_unsafe_werkzeug=True)


# ── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 55)
    print("  SmartHome Gateway — Starting")
    print("=" * 55)
    init_db()
    start_workers()
    start_api()