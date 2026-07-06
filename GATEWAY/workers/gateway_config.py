# workers/gateway_config.py
"""
Gateway Remote Config — Đồng bộ cấu hình động từ Firestore
════════════════════════════════════════════════════════════
Cho phép người dùng thay đổi cấu hình (hiện tại: Email Alert)
qua trang Settings trên web, thay vì phải sửa .env + restart gateway.

Firestore doc: system_config/email_alert
    { emailUser, emailAppPassword, emailTo, cooldownSeconds, updatedAt }

Cách dùng ở module khác:
    from workers.gateway_config import get_email_config
    cfg = get_email_config()
    cfg["user"], cfg["password"], cfg["to"], cfg["cooldown"]

Poll mỗi POLL_INTERVAL giây (mặc định 300s = 5 phút).
Fallback: nếu Firestore chưa có doc / lỗi mạng → giữ nguyên giá trị cũ
đã load lần trước, hoặc giá trị từ .env nếu chưa từng load được lần nào.
"""

import logging
import os
import threading
import time

import firebase_admin
from firebase_admin import credentials, firestore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s][CONFIG-SYNC] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("gateway_config")

POLL_INTERVAL = int(os.getenv("CONFIG_POLL_INTERVAL_SECONDS", 300))  # 5 phút

_lock = threading.Lock()
_FIREBASE_INIT_LOCK = threading.Lock()

# Giá trị khởi tạo lấy từ .env — dùng làm fallback nếu Firestore chưa có config
# hoặc gateway mất mạng lúc khởi động.
_email_config = {
    "user":     os.getenv("EMAIL_USER", ""),
    "password": os.getenv("EMAIL_APP_PASSWORD", ""),
    "to":       os.getenv("EMAIL_TO", ""),
    "cooldown": int(os.getenv("EMAIL_COOLDOWN_SECONDS", 600)),
    "source":   "env",   # "env" hoặc "firestore" — biết đang dùng nguồn nào để debug
}


def _get_firestore_client():
    """Tái sử dụng firebase_admin app đã init (từ firebase_sync.py) nếu có,
    tránh lỗi 'default app already exists'."""
    try:
        firebase_admin.get_app()
        return firestore.client()
    except ValueError:
        pass

    with _FIREBASE_INIT_LOCK:
        try:
            firebase_admin.get_app()
            return firestore.client()
        except ValueError:
            try:
                key_path = os.getenv("FIREBASE_KEY_PATH")
                if key_path and os.path.exists(key_path):
                    cred = credentials.Certificate(key_path)
                    firebase_admin.initialize_app(cred)
                else:
                    firebase_admin.initialize_app()
            except Exception as e:
                log.error("Không thể khởi tạo Firestore client: %s", e)
                return None

            return firestore.client()


def get_email_config() -> dict:
    """Trả về bản sao config email hiện tại (thread-safe)."""
    with _lock:
        return dict(_email_config)


def _apply_firestore_data(data: dict, doc_ref):
    with _lock:
        if data.get("emailUser"):
            _email_config["user"] = data["emailUser"]
        if data.get("emailAppPassword"):
            _email_config["password"] = data["emailAppPassword"]
        if data.get("emailTo"):
            _email_config["to"] = data["emailTo"]
        if data.get("cooldownSeconds"):
            _email_config["cooldown"] = int(data["cooldownSeconds"])
        _email_config["source"] = "firestore"

    log.info(
        "Đã cập nhật config email từ Firestore — user=%s, to=%s, cooldown=%ds",
        _email_config["user"], _email_config["to"], _email_config["cooldown"],
    )

    # Ghi lastPolledAt để web hiển thị "Gateway đã nhận cấu hình lúc..."
    try:
        doc_ref.update({"lastPolledAt": firestore.SERVER_TIMESTAMP})
    except Exception as e:
        log.warning("Không ghi được lastPolledAt: %s", e)


def _poll_loop():
    db = _get_firestore_client()
    if db is None:
        log.error("Config sync dừng — không kết nối được Firestore. Dùng config .env cố định.")
        return

    doc_ref = db.collection("system_config").document("email_alert")

    while True:
        try:
            snap = doc_ref.get()
            if snap.exists:
                _apply_firestore_data(snap.to_dict(), doc_ref)
            else:
                log.info("Chưa có system_config/email_alert trên Firestore — dùng config .env tạm thời.")
        except Exception as e:
            log.error("Lỗi poll Firestore config: %s — giữ nguyên config cũ.", e)

        time.sleep(POLL_INTERVAL)


def run():
    """Entry point — gọi từ gateway_main.py giống các worker khác (chạy trong thread riêng)."""
    log.info("Config Sync Worker starting — poll mỗi %ds", POLL_INTERVAL)
    _poll_loop()


if __name__ == "__main__":
    run()