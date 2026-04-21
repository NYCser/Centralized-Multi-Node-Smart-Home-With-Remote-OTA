"""
gateway_main.py
═══════════════
Entry point khởi động toàn bộ Gateway Local SmartHome.
"""
import os
import sys
import threading
import sqlite3
from flask import Flask, request
from flask_cors import CORS 
import socket

# 1. Import app và socketio từ app/main.py
# Đảm bảo file app/main.py của bạn đã khởi tạo: socketio = SocketIO(app, cors_allowed_origins="*")
from app.main import app, socketio 

# 2. Cấu hình CORS tối đa cho Flask-CORS
CORS(app, resources={r"/*": {"origins": "*"}}, supports_credentials=True)

# 3. Hàm "Force CORS" - Đảm bảo mọi phản hồi đều có giấy phép thông hành
@app.after_request
def add_cors_headers(response):
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS,PATCH')
    return response

# Thêm project root vào path để tránh lỗi import
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Cấu hình môi trường
DB_PATH    = os.getenv("DB_PATH",     "/data/smarthome.db")
REDIS_HOST = os.getenv("REDIS_HOST",  "localhost")
MQTT_HOST  = os.getenv("MQTT_BROKER", "localhost")

# ── 1. Khởi tạo Cơ sở dữ liệu ───────────────────────────────

def init_db():
    schema_path = os.path.join(os.path.dirname(__file__), "storage/db_schema.sql")
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")

    if os.path.exists(schema_path):
        with open(schema_path, "r") as f:
            conn.executescript(f.read())
        print("[MAIN] DB schema applied")
    else:
        print("[MAIN] Warning: db_schema.sql not found")

    # Ensure admin account
    ensure_admin(conn)
    conn.close()

def ensure_admin(conn):
    admin_email = "admin@smarthome.local"
    admin_pw = "admin123"  # Change after first login
    existing = conn.execute("SELECT id FROM users WHERE email=?", (admin_email,)).fetchone()
    if not existing:
        import bcrypt
        pw_hash = bcrypt.hashpw(admin_pw.encode(), bcrypt.gensalt()).decode()
        conn.execute(
            "INSERT INTO users (email, password, display_name, role) VALUES (?, ?, ?, ?)",
            (admin_email, pw_hash, "Administrator", "admin")
        )
        print("[AUTH] Admin account created")

    conn.commit()

# ── 2. Khởi động các dịch vụ nền (Workers) ─────────────────

def start_workers():
    from bridge.message_bus     import MessageBus
    from workers                import safety_watchdog, automation_engine, data_syncer, network_watchdog

    bus = MessageBus.get_instance()
    bus.connect()

    # Chạy các luồng xử lý riêng biệt
    threading.Thread(target=safety_watchdog.run,    name="SafetyWatchdog",  daemon=True).start()
    threading.Thread(target=network_watchdog.run,   name="NetworkWatchdog", daemon=True).start()
    threading.Thread(target=data_syncer.run,        name="DataSyncer",      daemon=True).start()
    
    # Automation Engine thường chứa vòng lặp pub/sub nên để daemon=False nếu nó là luồng chính
    threading.Thread(target=automation_engine.run,  name="AutomationEngine",daemon=False).start()

    print("[MAIN] All workers started")

# ── 3. Cấu hình và chạy API ────────────────────────────────

def start_api():
    from app.api.routes.all_routes import (
        auth_bp, sensors_bp, devices_bp, automation_bp,
        logs_bp, rfid_bp, wifi_bp, ota_bp, system_bp
    )

    # Đăng ký các Blueprint với tiền tố /api để khớp với apiClient.js
    blueprints = [
        auth_bp, sensors_bp, devices_bp, automation_bp,
        logs_bp, rfid_bp, wifi_bp, ota_bp, system_bp
    ]

    for bp in blueprints:
        try:
            # URL ví dụ: http://192.168.1.246:5000/api/auth/login
            app.register_blueprint(bp, url_prefix='/api')

            print(f"[API] Registered: {bp.name}")
        except Exception as e:
            print(f"[API] Error registering {bp.name}: {e}")

    port = int(os.getenv("API_PORT", 5000))
    print("=" * 55)
    ip = socket.gethostbyname(socket.gethostname())
    print(f"[MAIN] Gateway LIVE at: http://{ip}:{port}/")
    print("=" * 55)

    # Chạy server trên 0.0.0.0 để cho phép truy cập từ máy tính khác trong mạng
    #socketio.run(app, host="0.0.0.0", port=port, allow_unsafe_werkzeug=True)
    debug = os.getenv("DEBUG", "0") == "1"
    socketio.run(app, host="0.0.0.0", port=port, debug=debug)

# ── Main Entry ──────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 55)
    print("  SmartHome Gateway – Local Mode (CORS Enabled)")
    print("=" * 55)

    init_db()
    start_workers()
    start_api()