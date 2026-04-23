"""
app/api/routes/all_routes.py  — FIXED
═══════════════════════════════════════
Fixes:
  BUG-01: conn leak trong register() — không close trước khi return 409
  BUG-02: conn leak trong require_auth / require_admin — không close trước return 401/403
  BUG-05: wifi_connect block Flask thread 50s — chuyển sang async với timeout ngắn hơn
  BUG-06: Tạo MQTT client thứ 2 ở module level — loại bỏ, dùng MessageBus singleton
"""

import hashlib, secrets, json, os, time, uuid, sqlite3
from functools import wraps
from datetime import datetime, timedelta
from flask import Blueprint, jsonify, request, send_from_directory
from werkzeug.utils import secure_filename
import redis as redis_lib

DB_PATH        = os.getenv("DB_PATH",     "/data/smarthome.db")
DATA_DB_DIR    = os.getenv("DATA_DB_DIR", "/mnt/sd2/data")
REDIS_HOST     = os.getenv("REDIS_HOST",  "localhost")
SESSION_EXPIRE = 86400
UPLOAD_FOLDER  = os.getenv("UPLOAD_FOLDER", "./firmware")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# FIX BUG-06: Xóa module-level mqtt_client (tạo kết nối MQTT thứ 2 trùng với MessageBus)
# Thay bằng dùng MessageBus singleton khi cần publish MQTT
# mqtt_client = mqtt_lib.Client()  ← ĐÃ XÓA

# Redis connection pool — tránh exhaust connections khi nhiều worker
_redis_pool = redis_lib.ConnectionPool(
    host=REDIS_HOST, port=6379, decode_responses=True,
    max_connections=20
)

def get_r():
    """Lấy Redis client từ connection pool."""
    return redis_lib.Redis(connection_pool=_redis_pool)

# Backward-compat: module-level r cho các route dùng trực tiếp
r = redis_lib.Redis(connection_pool=_redis_pool)


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def get_daily_db_path(date_str: str):
    return os.path.join(DATA_DB_DIR, f"data_{date_str}.db")


def open_daily_db(date_str: str):
    path = get_daily_db_path(date_str)
    if not os.path.isfile(path):
        return None
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()


def get_current_user(token: str):
    """Tách logic xác thực ra helper riêng — dùng lại trong cả 2 decorator."""
    if not token:
        return None
    cached = r.get(f"session:{token}")
    conn = get_db()
    try:
        if cached:
            row = conn.execute(
                "SELECT id,email,display_name,role FROM users WHERE id=?",
                (int(cached),)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT u.id,u.email,u.display_name,u.role "
                "FROM sessions s JOIN users u ON s.user_id=u.id "
                "WHERE s.token=? AND s.expires_at>?",
                (token, datetime.utcnow().isoformat())
            ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()   # ← luôn close dù result thế nào


# FIX BUG-02: dùng get_current_user() — conn luôn được close trong finally
def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
        user  = get_current_user(token)
        if not user:
            return jsonify({"error": "unauthorized"}), 401
        request.current_user = user
        return f(*args, **kwargs)
    return decorated


def require_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
        user  = get_current_user(token)
        if not user:
            return jsonify({"error": "unauthorized"}), 401
        if user.get("role") != "admin":
            return jsonify({"error": "forbidden"}), 403
        request.current_user = user
        return f(*args, **kwargs)
    return decorated


# ══════════════════════════════════════════════════════════
# 1. AUTH
# ══════════════════════════════════════════════════════════
auth_bp = Blueprint("auth", __name__)

@auth_bp.route("/auth/register", methods=["POST"])
def register():
    data  = request.json or {}
    email = data.get("email", "").lower().strip()
    pw    = data.get("password", "")
    name  = data.get("display_name", email.split("@")[0])
    if not email or not pw:
        return jsonify({"error": "Email và mật khẩu là bắt buộc"}), 400

    conn = get_db()
    try:
        exist = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        if exist:
            # FIX BUG-01: conn.close() được gọi trong finally — không leak nữa
            return jsonify({"error": "Email đã tồn tại"}), 409
        conn.execute(
            "INSERT INTO users(email,password,display_name,role) VALUES(?,?,?,?)",
            (email, hash_pw(pw), name, "user")
        )
        user_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
    finally:
        conn.close()

    # Tạo session ngay sau register
    token = secrets.token_hex(32)
    exp   = (datetime.utcnow() + timedelta(seconds=SESSION_EXPIRE)).isoformat()
    c2 = get_db()
    try:
        c2.execute("INSERT INTO sessions(token,user_id,expires_at) VALUES(?,?,?)",
                   (token, user_id, exp))
        c2.commit()
    finally:
        c2.close()
    r.setex(f"session:{token}", SESSION_EXPIRE, str(user_id))

    return jsonify({"token": token, "user": {
        "id": user_id, "email": email, "display_name": name, "role": "user"
    }})


@auth_bp.route("/auth/login", methods=["POST"])
def login():
    data  = request.json or {}
    email = data.get("email", "").lower().strip()
    pw    = data.get("password", "")

    conn = get_db()
    try:
        user = conn.execute(
            "SELECT * FROM users WHERE email=? AND password=?",
            (email, hash_pw(pw))
        ).fetchone()
        conn.execute(
            "INSERT INTO login_logs (email,success,ip_address,device_hint,user_agent) VALUES (?,?,?,?,?)",
            (email, 1 if user else 0,
             request.remote_addr,
             request.headers.get("User-Agent", "")[:60],
             request.headers.get("User-Agent", ""))
        )
        conn.commit()
        if not user:
            return jsonify({"error": "Sai email hoặc mật khẩu"}), 401
        user = dict(user)
    finally:
        conn.close()

    token = secrets.token_hex(32)
    exp   = (datetime.utcnow() + timedelta(seconds=SESSION_EXPIRE)).isoformat()
    c2 = get_db()
    try:
        c2.execute("INSERT INTO sessions(token,user_id,expires_at) VALUES(?,?,?)",
                   (token, user["id"], exp))
        c2.commit()
    finally:
        c2.close()
    r.setex(f"session:{token}", SESSION_EXPIRE, str(user["id"]))

    return jsonify({"token": token, "user": {
        "id": user["id"], "email": user["email"],
        "display_name": user["display_name"], "role": user["role"]
    }})


@auth_bp.route("/auth/me")
@require_auth
def me():
    return jsonify({"user": request.current_user})


@auth_bp.route("/auth/logout", methods=["POST"])
@require_auth
def logout():
    token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
    conn = get_db()
    try:
        conn.execute("DELETE FROM sessions WHERE token=?", (token,))
        conn.commit()
    finally:
        conn.close()
    r.delete(f"session:{token}")
    return jsonify({"status": "ok"})


# ══════════════════════════════════════════════════════════
# 2. SENSORS & DASHBOARD
# ══════════════════════════════════════════════════════════
sensors_bp = Blueprint("sensors", __name__)

@sensors_bp.route("/latest")
def latest():
    room  = request.args.get("room")
    limit = min(int(request.args.get("limit", 50)), 200)
    if room:
        cached = r.get(f"sensor:{room}")
        if cached:
            return jsonify(json.loads(cached))
    conn = get_db()
    try:
        if room:
            rows = conn.execute(
                "SELECT room,type,value,timestamp FROM sensor_data WHERE room=? ORDER BY id DESC LIMIT ?",
                (room, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT room,type,value,timestamp FROM sensor_data ORDER BY id DESC LIMIT ?",
                (limit,)
            ).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()


@sensors_bp.route("/history")
def history():
    room  = request.args.get("room")
    type_ = request.args.get("type")
    hours = int(request.args.get("hours", 24))
    since = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT room,type,value,timestamp FROM sensor_data "
            "WHERE room=? AND type=? AND timestamp>=? ORDER BY timestamp ASC LIMIT 300",
            (room, type_, since)
        ).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()


@sensors_bp.route("/dashboard")
def dashboard():
    room = request.args.get("room")
    conn = get_db()
    try:
        raw = conn.execute(
            "SELECT type,value FROM sensor_data WHERE room=? ORDER BY id DESC LIMIT 20",
            (room,)
        ).fetchall()
        sensors = {}
        for row in raw:
            if row["type"] not in sensors:
                sensors[row["type"]] = row["value"]

        devices = {
            row["device_id"]: bool(row["is_on"])
            for row in conn.execute(
                "SELECT device_id,is_on FROM device_status WHERE room=?", (room,)
            ).fetchall()
        }
        alerts = [dict(r) for r in conn.execute(
            "SELECT id,message,level,type FROM system_alerts "
            "WHERE room=? AND is_resolved=0 ORDER BY id DESC LIMIT 5",
            (room,)
        ).fetchall()]
        return jsonify({
            "room": room, "sensors": sensors, "devices": devices,
            "alerts": alerts, "timestamp": int(time.time())
        })
    finally:
        conn.close()


@sensors_bp.route("/chart")
def chart():
    room  = request.args.get("room")
    type_ = request.args.get("type")
    hours = int(request.args.get("hours", 24))
    since = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT timestamp,value FROM sensor_data "
            "WHERE room=? AND type=? AND timestamp>=? ORDER BY timestamp ASC LIMIT 300",
            (room, type_, since)
        ).fetchall()
        return jsonify({"room": room, "type": type_, "data": [dict(r) for r in rows]})
    finally:
        conn.close()


@sensors_bp.route("/rooms")
def rooms():
    conn = get_db()
    try:
        rows = conn.execute("SELECT id FROM rooms ORDER BY id").fetchall()
        return jsonify([row["id"] for row in rows])
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════
# 3. DEVICES
# ══════════════════════════════════════════════════════════
devices_bp = Blueprint("devices", __name__)

@devices_bp.route("/device_status")
def device_status():
    room = request.args.get("room")
    conn = get_db()
    try:
        if room:
            rows = conn.execute(
                "SELECT room,device_id,is_on,updated_at FROM device_status WHERE room=?",
                (room,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT room,device_id,is_on,updated_at FROM device_status"
            ).fetchall()
        return jsonify([dict(row) for row in rows])
    finally:
        conn.close()


@devices_bp.route("/control", methods=["POST"])
@require_auth
def control():
    data      = request.json or {}
    room      = data.get("room")
    device_id = data.get("device_id")
    is_on     = data.get("is_on", False)

    if not room or not device_id:
        return jsonify({"error": "room và device_id là bắt buộc"}), 400

    if r.exists(f"safety_lock:{room}"):
        return jsonify({"error": "Hệ thống đang trong trạng thái khẩn cấp!", "locked": True}), 423

    r.publish("device_commands", json.dumps({
        "room": room, "device_id": device_id, "is_on": is_on, "source": "web"
    }))
    return jsonify({"status": "ok"})


# ══════════════════════════════════════════════════════════
# 4. AUTOMATION & SCHEDULES
# ══════════════════════════════════════════════════════════
automation_bp = Blueprint("automation", __name__)

@automation_bp.route("/automations", methods=["GET"])
def get_automations():
    conn = get_db()
    try:
        rows = conn.execute("SELECT * FROM automations").fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()


@automation_bp.route("/automations", methods=["POST"])
@require_auth
def upsert_automation():
    data    = request.json or {}
    room_id = data.get("room_id")
    if not room_id:
        return jsonify({"error": "room_id required"}), 400
    conn = get_db()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO automations "
            "(room_id,enabled,fan_threshold,light_threshold,gas_threshold) VALUES (?,?,?,?,?)",
            (room_id, 1 if data.get("enabled", True) else 0,
             data.get("fan_threshold"), data.get("light_threshold"),
             data.get("gas_threshold", 600))
        )
        conn.commit()
    finally:
        conn.close()
    r.publish("automation_commands", json.dumps({"action": "upsert", "room_id": room_id, "rule": data}))
    return jsonify({"status": "ok"})


@automation_bp.route("/automations/<room_id>", methods=["DELETE"])
@require_auth
def del_automation(room_id):
    conn = get_db()
    try:
        conn.execute("DELETE FROM automations WHERE room_id=?", (room_id,))
        conn.commit()
    finally:
        conn.close()
    r.publish("automation_commands", json.dumps({"action": "delete", "room_id": room_id}))
    return jsonify({"status": "ok"})


@automation_bp.route("/schedules", methods=["GET"])
def get_schedules():
    room = request.args.get("room")
    conn = get_db()
    try:
        if room:
            rows = conn.execute(
                "SELECT * FROM schedules WHERE enabled=1 AND room_id=?", (room,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM schedules WHERE enabled=1").fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()


@automation_bp.route("/schedules", methods=["POST"])
@require_auth
def add_schedule():
    data = request.json or {}
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO schedules(room_id,device_id,action,time,enabled) VALUES(?,?,?,?,1)",
            (data["room_id"], data["device_id"], data["action"], data["time"])
        )
        conn.commit()
    finally:
        conn.close()
    r.publish("schedule_commands", json.dumps({"action": "reload"}))
    return jsonify({"status": "ok"})


@automation_bp.route("/schedules/<int:sid>", methods=["DELETE"])
@require_auth
def del_schedule(sid):
    conn = get_db()
    try:
        conn.execute("DELETE FROM schedules WHERE id=?", (sid,))
        conn.commit()
    finally:
        conn.close()
    r.publish("schedule_commands", json.dumps({"action": "reload"}))
    return jsonify({"status": "ok"})


# ══════════════════════════════════════════════════════════
# 5. LOGS & NOTIFICATIONS
# ══════════════════════════════════════════════════════════
logs_bp = Blueprint("logs", __name__)

def _date_filter(col):
    date_from = request.args.get("date_from", "")
    date_to   = request.args.get("date_to", "")
    clauses, params = [], []
    if date_from:
        clauses.append(f"{col}>=?"); params.append(date_from)
    if date_to:
        clauses.append(f"{col}<=?"); params.append(date_to + " 23:59:59")
    return clauses, params


@logs_bp.route("/logs/feed")
@require_auth
def logs_feed():
    log_type = request.args.get("type", "all")
    status   = request.args.get("status", "all")
    room     = request.args.get("room", "")
    limit    = min(int(request.args.get("limit", 50)), 200)
    offset   = int(request.args.get("offset", 0))

    conn   = get_db()
    result = []
    try:
        if log_type in ("all", "login"):
            dc, dp = _date_filter("timestamp")
            where  = ["1=1"] + dc; params = list(dp)
            if status == "success": where.append("success=1")
            elif status == "failed": where.append("success=0")
            for row in conn.execute(
                f"SELECT * FROM login_logs WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ? OFFSET ?",
                params + [limit, offset]
            ).fetchall():
                result.append({
                    "id": f"login_{row['id']}", "log_type": "login",
                    "title": "LOGIN LOGS", "subtitle": "LỊCH SỬ ĐĂNG NHẬP",
                    "detail": f"{row['email']} | IP:{row['ip_address']}",
                    "status": "success" if row["success"] else "failed",
                    "status_label": "Thành công" if row["success"] else "Thất bại",
                    "room": "", "timestamp": row["timestamp"],
                    "meta": {"email": row["email"], "ip": row["ip_address"]}
                })

        if log_type in ("all", "access"):
            dc, dp = _date_filter("timestamp")
            where  = ["1=1"] + dc; params = list(dp)
            if room: where.append("room=?"); params.append(room)
            if status == "success": where.append("success=1")
            elif status == "failed": where.append("success=0")
            _amap = {"open_door": "Mở cửa", "attempt_failed": "Từ chối", "enroll": "Nạp thẻ"}
            for row in conn.execute(
                f"SELECT * FROM access_logs WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ? OFFSET ?",
                params + [limit, offset]
            ).fetchall():
                result.append({
                    "id": f"access_{row['id']}", "log_type": "access",
                    "title": "ACCESS LOGS",
                    "subtitle": f"RA VÀO CỬA - {_amap.get(row['action'], row['action'])}",
                    "detail": f"{row['user_name']} | Phòng:{row['room']} | UID:{row['uid']}",
                    "status": "success" if row["success"] else "failed",
                    "status_label": "Vào" if row["success"] else "Từ chối",
                    "room": row["room"], "timestamp": row["timestamp"],
                    "meta": {"uid": row["uid"], "user_name": row["user_name"]}
                })

        if log_type in ("all", "automation"):
            dc, dp = _date_filter("timestamp")
            where  = ["1=1"] + dc; params = list(dp)
            if room: where.append("room=?"); params.append(room)
            for row in conn.execute(
                f"SELECT * FROM automation_logs WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ? OFFSET ?",
                params + [limit, offset]
            ).fetchall():
                try:
                    acts = " | ".join(json.loads(row["actions"])[:3])
                except Exception:
                    acts = str(row["actions"])[:80]
                result.append({
                    "id": f"auto_{row['id']}", "log_type": "automation",
                    "title": "AUTOMATION LOGS", "subtitle": "TỰ ĐỘNG HÓA",
                    "detail": f"{row['scenario']} | {acts}",
                    "status": "info", "status_label": "Thực thi",
                    "room": row["room"], "timestamp": row["timestamp"],
                    "meta": {"scenario": row["scenario"], "triggered_by": row["triggered_by"]}
                })

        if log_type in ("all", "gas_alert", "fire_alert", "alert"):
            dc, dp = _date_filter("timestamp")
            where  = ["1=1"] + dc; params = list(dp)
            if log_type == "gas_alert":   where.append("type='gas'")
            elif log_type == "fire_alert": where.append("type='fire'")
            else:                          where.append("type IN ('gas','fire')")
            if room: where.append("room=?"); params.append(room)
            if status == "resolved":    where.append("is_resolved=1")
            elif status == "unresolved": where.append("is_resolved=0")
            for row in conn.execute(
                f"SELECT * FROM system_alerts WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ? OFFSET ?",
                params + [limit, offset]
            ).fetchall():
                is_gas = row["type"] == "gas"
                result.append({
                    "id": f"alert_{row['id']}",
                    "log_type": "gas_alert" if is_gas else "fire_alert",
                    "title": "GAS ALERT" if is_gas else "FIRE ALERT",
                    "subtitle": f" {row['message']}",
                    "detail": f"Phòng:{row['room']} | Mức:{row['level']}",
                    "status": "resolved" if row["is_resolved"] else "unresolved",
                    "status_label": "Đã xử lý" if row["is_resolved"] else "Chưa xử lý",
                    "room": row["room"], "timestamp": row["timestamp"],
                    "meta": {"type": row["type"], "level": row["level"],
                             "is_resolved": bool(row["is_resolved"])}
                })

        if log_type in ("all", "system_alert"):
            dc, dp = _date_filter("timestamp")
            where  = ["type NOT IN ('gas','fire')"] + dc; params = list(dp)
            if room: where.append("room=?"); params.append(room)
            if status == "resolved":    where.append("is_resolved=1")
            elif status == "unresolved": where.append("is_resolved=0")
            for row in conn.execute(
                f"SELECT * FROM system_alerts WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ? OFFSET ?",
                params + [limit, offset]
            ).fetchall():
                result.append({
                    "id": f"sys_{row['id']}", "log_type": "system_alert",
                    "title": "SYSTEM ALERT", "subtitle": f"{row['message']}",
                    "detail": f"Phòng:{row['room']} | Mức:{row['level']}",
                    "status": "resolved" if row["is_resolved"] else "unresolved",
                    "status_label": "Đã xử lý" if row["is_resolved"] else "Chưa xử lý",
                    "room": row["room"], "timestamp": row["timestamp"],
                    "meta": {"type": row["type"], "level": row["level"]}
                })
    finally:
        conn.close()

    result.sort(key=lambda x: x.get("timestamp") or "", reverse=True)
    return jsonify({"total": len(result), "limit": limit, "offset": offset, "items": result[:limit]})


@logs_bp.route("/alerts")
def get_alerts():
    room  = request.args.get("room")
    limit = int(request.args.get("limit", 20))
    conn  = get_db()
    try:
        if room:
            rows = conn.execute(
                "SELECT * FROM system_alerts WHERE room=? ORDER BY id DESC LIMIT ?", (room, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM system_alerts ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()


@logs_bp.route("/alerts/<int:aid>/resolve", methods=["POST"])
@require_auth
def resolve_alert(aid):
    conn = get_db()
    try:
        row = conn.execute("SELECT room FROM system_alerts WHERE id=?", (aid,)).fetchone()
        if not row:
            return jsonify({"error": "not found"}), 404
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "UPDATE system_alerts SET is_resolved=1, resolved_at=? WHERE id=?", (now, aid)
        )
        conn.commit()
        room = row["room"]
    finally:
        conn.close()
    r.publish("alert_commands", json.dumps({"action": "mute", "room_id": room}))
    return jsonify({"status": "ok"})


@logs_bp.route("/notifications")
@require_auth
def get_notifications():
    limit  = int(request.args.get("limit", 20))
    unread = request.args.get("unread") == "1"
    conn   = get_db()
    try:
        if unread:
            rows = conn.execute(
                "SELECT * FROM notifications WHERE is_read=0 ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM notifications ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        count = conn.execute(
            "SELECT COUNT(*) as n FROM notifications WHERE is_read=0"
        ).fetchone()["n"]
        return jsonify({"items": [dict(r) for r in rows], "unread_count": count})
    finally:
        conn.close()


@logs_bp.route("/notifications/read_all", methods=["POST"])
@require_auth
def read_all():
    conn = get_db()
    try:
        conn.execute("UPDATE notifications SET is_read=1")
        conn.commit()
    finally:
        conn.close()
    return jsonify({"status": "ok"})


@logs_bp.route("/access_logs")
@require_auth
def access_logs():
    room  = request.args.get("room")
    limit = int(request.args.get("limit", 50))
    conn  = get_db()
    try:
        if room:
            rows = conn.execute(
                "SELECT * FROM access_logs WHERE room=? ORDER BY id DESC LIMIT ?", (room, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM access_logs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()


@logs_bp.route("/login_logs")
@require_auth
def login_logs():
    limit  = min(int(request.args.get("limit", 50)), 500)
    offset = int(request.args.get("offset", 0))
    conn   = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM login_logs ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)
        ).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════
# 6. RFID
# ══════════════════════════════════════════════════════════
rfid_bp = Blueprint("rfid", __name__)

@rfid_bp.route("/rfid_cards")
@require_auth
def get_rfid():
    conn = get_db()
    try:
        rows = conn.execute("SELECT * FROM rfid_cards ORDER BY created_at DESC").fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()


@rfid_bp.route("/rfid_cards", methods=["POST"])
@require_auth
def add_rfid():
    data = request.json or {}
    uid  = data.get("uid")
    if not uid:
        return jsonify({"error": "uid required"}), 400
    conn = get_db()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO rfid_cards(uid,owner_name,is_active) VALUES(?,?,1)",
            (uid, data.get("owner_name", ""))
        )
        conn.commit()
    finally:
        conn.close()
    r.publish("rfid_commands", json.dumps({
        "action": "add", "uid": uid, "owner_name": data.get("owner_name", "")
    }))
    return jsonify({"status": "ok"})


@rfid_bp.route("/rfid_cards/<uid>", methods=["DELETE"])
@require_auth
def del_rfid(uid):
    conn = get_db()
    try:
        conn.execute("DELETE FROM rfid_cards WHERE uid=?", (uid,))
        conn.commit()
    finally:
        conn.close()
    r.publish("rfid_commands", json.dumps({"action": "delete", "uid": uid}))
    return jsonify({"status": "ok"})


@rfid_bp.route("/rfid/enroll", methods=["POST"])
@require_auth
def rfid_enroll():
    data = request.json or {}
    r.publish("rfid_commands", json.dumps({
        "action": "enroll", "owner_name": data.get("owner_name", "Thẻ mới")
    }))
    return jsonify({"status": "enrollment_started", "timeout": 60})


# ══════════════════════════════════════════════════════════
# 7. WIFI
# ══════════════════════════════════════════════════════════
wifi_bp = Blueprint("wifi", __name__)

@wifi_bp.route("/wifi/status")
def wifi_status():
    raw = r.get("system_status:wifi")
    return jsonify(json.loads(raw) if raw else {"status": "unknown", "ssid": "N/A"})


@wifi_bp.route("/wifi/scan", methods=["POST"])
@require_auth
def wifi_scan():
    r.set("wifi_scan_status", "scanning")
    r.publish("wifi_scan_trigger", "1")
    return jsonify({"status": "scanning_started"})


@wifi_bp.route("/wifi/scan_result")
def wifi_scan_result():
    status  = r.get("wifi_scan_status") or "idle"
    result_ = r.get("wifi_scan_result")
    return jsonify({"status": status, "networks": json.loads(result_) if result_ else []})


@wifi_bp.route("/wifi/connect", methods=["POST"])
@require_auth
def wifi_connect():
    """
    FIX BUG-05: Không block Flask thread 50s.
    Publish command → trả về request_id ngay → client poll /wifi/connect_result.
    """
    data   = request.json or {}
    req_id = str(uuid.uuid4())
    r.publish("wifi_commands", json.dumps({
        "ssid":       data.get("ssid"),
        "password":   data.get("password", ""),
        "request_id": req_id
    }))
    # Trả về ngay — client dùng /wifi/connect_result?id=req_id để poll
    return jsonify({"status": "connecting", "request_id": req_id})


@wifi_bp.route("/wifi/connect_result")
def wifi_connect_result():
    """Client poll endpoint để lấy kết quả wifi connect (không block Flask)."""
    req_id = request.args.get("id", "")
    result = r.get(f"wifi_cmd:{req_id}")
    if result:
        r.delete(f"wifi_cmd:{req_id}")
        return jsonify(json.loads(result))
    return jsonify({"status": "pending"})


# ══════════════════════════════════════════════════════════
# 8. OTA FIRMWARE
# FIX BUG-06: Dùng MessageBus.publish_mqtt thay mqtt_client trực tiếp
# ══════════════════════════════════════════════════════════
ota_bp = Blueprint("ota", __name__)

@ota_bp.route("/ota/upload", methods=["POST"])
@require_admin
def ota_upload():
    if "file" not in request.files:
        return jsonify({"error": "no file"}), 400
    f        = request.files["file"]
    filename = secure_filename(f.filename)
    f.save(os.path.join(UPLOAD_FOLDER, filename))
    return jsonify({"status": "uploaded", "filename": filename})


@ota_bp.route("/ota/update", methods=["POST"])
@require_admin
def ota_trigger():
    data     = request.json or {}
    room     = data.get("room")
    filename = data.get("file")
    if not room or not filename:
        return jsonify({"error": "room và file là bắt buộc"}), 400

    url = f"http://{request.host}/firmware/{filename}"

    # FIX BUG-06: Dùng MessageBus thay mqtt_client riêng
    from bridge.message_bus import MessageBus
    MessageBus.get_instance().publish_mqtt(f"home/{room}/command", {
        "action": "ota_update", "url": url
    })

    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO ota_logs(room,filename,url,triggered_by) VALUES(?,?,?,?)",
            (room, filename, url, request.current_user.get("email", ""))
        )
        conn.commit()
    finally:
        conn.close()
    return jsonify({"status": "OTA sent", "url": url})


@ota_bp.route("/firmware/<filename>")
def serve_firmware(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)


# ══════════════════════════════════════════════════════════
# 9. SYSTEM
# ══════════════════════════════════════════════════════════
system_bp = Blueprint("system", __name__)

@system_bp.route("/health")
def health():
    return jsonify({"status": "ok", "timestamp": datetime.utcnow().isoformat()})


@system_bp.route("/sd2/status")
def sd2_status():
    mount = "/mnt/sd2"
    is_ok = os.path.ismount(mount)
    files, db_sz = [], 0
    if is_ok:
        exp = f"{mount}/exports"
        if os.path.isdir(exp):
            files = sorted(os.listdir(exp), reverse=True)[:20]
        db_p = f"{mount}/smarthome_backup.db"
        if os.path.isfile(db_p):
            db_sz = round(os.path.getsize(db_p) / 1024, 1)
    return jsonify({"mounted": is_ok, "backup_db_size_kb": db_sz, "recent_exports": files})


@system_bp.route("/data")
def daily_data():
    date_param  = request.args.get("date", "")
    dates_param = request.args.get("dates", "")
    data_type   = request.args.get("type", "sensor").lower()
    table_map   = {"sensor": "sensor_data", "event": "system_events", "events": "system_events"}
    table_name  = table_map.get(data_type)
    if not table_name:
        return jsonify({"error": "type must be sensor or event"}), 400

    if dates_param:
        requested_dates = [d.strip() for d in dates_param.split(",") if d.strip()]
    elif date_param:
        requested_dates = [date_param.strip()]
    else:
        return jsonify({"error": "date or dates parameter is required"}), 400

    result = []
    for date_str in requested_dates:
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            continue
        conn = open_daily_db(date_str)
        if not conn:
            continue
        try:
            rows = conn.execute(
                f"SELECT * FROM {table_name} ORDER BY timestamp ASC"
            ).fetchall()
            for row in rows:
                item = dict(row)
                item["date"] = date_str
                result.append(item)
        finally:
            conn.close()

    result.sort(key=lambda x: x.get("timestamp") or "")
    return jsonify({"items": result, "count": len(result), "dates": requested_dates})


@system_bp.route("/sd2/export_now", methods=["POST"])
@require_auth
def sd2_export():
    r.publish("log_commands", json.dumps({
        "action": "export_now", "user": request.current_user.get("email", "")
    }))
    return jsonify({"status": "processing"})


@system_bp.route("/system/clock")
def system_clock():
    now      = datetime.now()
    is_valid = now.year >= 2024
    return jsonify({
        "datetime":  now.strftime("%Y-%m-%d %H:%M:%S"),
        "timestamp": int(now.timestamp()),
        "is_valid":  is_valid,
        "warning":   None if is_valid else "Giờ hệ thống chưa đồng bộ! Kiểm tra module RTC DS3231."
    })


@system_bp.route("/system/snapshots")
def system_snapshots():
    hours = int(request.args.get("hours", 24))
    since = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    conn  = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM system_snapshots WHERE timestamp>=? ORDER BY timestamp ASC LIMIT 200",
            (since,)
        ).fetchall()
        return jsonify([dict(r) for r in rows])
    except Exception:
        return jsonify([])
    finally:
        conn.close()


@system_bp.route("/system/safety_status")
def safety_status():
    keys   = r.keys("active_alert:*")
    alerts = {}
    for key in keys:
        room_id = key.replace("active_alert:", "")
        raw     = r.get(key)
        if raw:
            try:
                alerts[room_id] = json.loads(raw)
            except Exception:
                pass
    return jsonify({"active_alerts": alerts})
