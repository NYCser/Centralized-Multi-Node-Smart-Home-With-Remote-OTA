# workers/email_notifier.py
"""
Email Notifier Worker
══════════════════════
Lắng nghe channel Redis "safety_alert" (gas/lửa) và gửi email cảnh báo
qua Gmail SMTP khi level="critical".

FIX: Config (EMAIL_USER/EMAIL_APP_PASSWORD/EMAIL_TO/COOLDOWN) giờ đọc ĐỘNG
từ workers/gateway_config.py (được đồng bộ từ Firestore system_config/email_alert
qua trang Settings trên web) thay vì hardcode cố định từ .env lúc khởi động.
.env vẫn được dùng làm fallback nếu Firestore chưa có config.

Luồng:
  safety_watchdog._save_alert() → Redis publish "safety_alert"
      → email_notifier nhận → lọc level=critical → gửi email (dùng config mới nhất)

CHỈ gửi email cho gas/lửa/intrusion (level="critical").
Bỏ qua thông báo "đã an toàn" (type="system", level="info") để tránh spam.

Có rate-limit: tối đa 1 email / room / cooldown (lấy từ config động) để tránh
gửi liên tục khi safety_watchdog lưu lại alert mỗi 5 phút (SAFETY_REPEAT_INTERVAL).
"""

import json
import logging
import os
import smtplib
import threading
import time
from datetime import datetime
from email.mime.text import MIMEText

from bridge.message_bus import MessageBus
from workers.gateway_config import get_email_config

# ══════════════════════════════════════════════════════════
# CONFIG TĨNH — những thứ KHÔNG thay đổi qua Settings page
# ══════════════════════════════════════════════════════════

SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT   = int(os.getenv("SMTP_PORT", 587))

ROOM_NAME_MAP = {
    "kitchen_01":     "Phòng bếp",
    "living_room_01": "Phòng khách",
    "bedroom_01":      "Phòng ngủ",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s][EMAIL] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("email_notifier")

_last_sent: dict = {}  # room_id -> timestamp (epoch) lần gửi gần nhất
_lock = threading.Lock()


def _config_ok(cfg: dict) -> bool:
    if not cfg["user"] or not cfg["password"] or not cfg["to"]:
        log.warning(
            "Email chưa được cấu hình đầy đủ (thiếu user/password/to — nguồn hiện tại: %s) "
            "— bỏ qua gửi email. Cấu hình tại trang Settings hoặc .env.",
            cfg["source"],
        )
        return False
    return True


def _build_message(cfg: dict, alert_type: str, room_id: str, message: str, timestamp: str) -> MIMEText:
    room_name = ROOM_NAME_MAP.get(room_id, room_id)
    if alert_type == "fire":
        type_label = "LỬA"
    elif alert_type == "gas":
        type_label = "KHÍ GAS"
    elif alert_type == "intrusion":
        type_label = "ĐỘT NHẬP / THẺ LẠ"
    else:
        type_label = alert_type.upper()

    subject = f"CẢNH BÁO {type_label} - {room_name}"
    body = (
        f"HỆ THỐNG SMART HOME - CẢNH BÁO AN TOÀN\n"
        f"{'='*45}\n\n"
        f"Loại cảnh báo : {type_label}\n"
        f"Phòng         : {room_name} ({room_id})\n"
        f"Chi tiết      : {message}\n"
        f"Thời gian     : {timestamp}\n\n"
        f"{'='*45}\n"
        f"Hệ thống đã tự động: bật quạt thông gió, kích hoạt còi báo động "
        f"và khóa an toàn cho phòng này.\n"
        f"Vui lòng kiểm tra ngay.\n"
    )

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = cfg["user"]
    msg["To"] = cfg["to"]
    return msg


def send_alert_email(alert_type: str, room_id: str, message: str, timestamp: str) -> bool:
    cfg = get_email_config()  # luôn lấy config MỚI NHẤT tại thời điểm gửi
    if not _config_ok(cfg):
        return False

    msg = _build_message(cfg, alert_type, room_id, message, timestamp)
    recipients = [e.strip() for e in cfg["to"].split(",") if e.strip()]

    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=15) as server:
            server.starttls()
            server.login(cfg["user"], cfg["password"])
            server.sendmail(cfg["user"], recipients, msg.as_string())
        log.info("Đã gửi email cảnh báo %s - %s → %s", alert_type, room_id, cfg["to"])
        return True
    except smtplib.SMTPAuthenticationError as e:
        log.error(
            "Lỗi xác thực Gmail (nguồn config: %s) — kiểm tra lại App Password "
            "tại trang Settings hoặc .env: %s", cfg["source"], e
        )
        return False
    except Exception as e:
        log.error("Gửi email thất bại: %s", e)
        return False


def _should_send(room_id: str, cooldown_seconds: int) -> bool:
    """Rate-limit: chỉ gửi 1 email / room / cooldown_seconds (lấy từ config động)."""
    now = time.time()
    with _lock:
        last = _last_sent.get(room_id, 0)
        if now - last < cooldown_seconds:
            return False
        _last_sent[room_id] = now
        return True


def _handle_alert(data: dict):
    alert_type = data.get("type", "")
    level      = data.get("level", "")
    room_id    = data.get("room") or data.get("location") or "unknown"
    message    = data.get("message", "")
    timestamp  = data.get("timestamp", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    if alert_type not in ("gas", "fire", "intrusion"):
        return
    if level != "critical":
        return

    cfg = get_email_config()
    if not _should_send(room_id, cfg["cooldown"]):
        log.debug("Bỏ qua gửi email %s/%s — đang trong cooldown %ds", room_id, alert_type, cfg["cooldown"])
        return

    send_alert_email(alert_type, room_id, message, timestamp)


def run():
    """Entry point — gọi từ gateway_main.py giống các worker khác."""
    cfg = get_email_config()
    if not _config_ok(cfg):
        log.warning(
            "Email notifier khởi động nhưng CHƯA cấu hình — sẽ không gửi được email cho đến khi "
            "có config trong Settings page (Firestore) hoặc .env."
        )

    bus    = MessageBus.get_instance()
    redis  = bus.get_redis()
    pubsub = redis.pubsub()
    pubsub.subscribe("safety_alert")

    log.info("Email Notifier started — lắng nghe channel 'safety_alert' (chỉ gas/lửa/intrusion critical)")

    for msg in pubsub.listen():
        if msg["type"] != "message":
            continue
        try:
            data = json.loads(msg["data"])
            _handle_alert(data)
        except json.JSONDecodeError:
            log.warning("safety_alert payload không phải JSON hợp lệ: %s", str(msg["data"])[:100])
        except Exception as e:
            log.error("Lỗi xử lý safety_alert: %s", e)


if __name__ == "__main__":
    run()