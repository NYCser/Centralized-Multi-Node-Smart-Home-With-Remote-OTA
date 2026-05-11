# workers/ota_manager.py
"""
OTA Manager Worker
══════════════════
Luồng:
  [Dev] POST /api/ota/upload
    → Redis 'ota_commands' {action: new_firmware, ...}
    → [ota_manager] ghi Firestore ota_notices/{room}
    → [Web] onSnapshot → hiện popup cho user
    → [User] nhấn Cập nhật → Firestore ota_notices/{room}.status=confirmed
    → [ota_manager] detect change → gửi MQTT home/{room}/command
    → [ESP32] tải firmware từ Pi HTTP server → flash → reboot
    → [ESP32] gửi MQTT home/{room}/status {source: ota, version: x.y.z}
    → [ota_manager] cập nhật Firestore ota_notices/{room}.status=done
"""

import json, time, threading, os
from datetime import datetime
from bridge.message_bus import MessageBus
from firebase_admin import firestore, db
from google.cloud.firestore_v1 import FieldFilter
import firebase_admin


def _wait_for_firebase(timeout=30):
    """Chờ Firebase được initialize (bởi firebase_sync.py) với timeout."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            firebase_admin.get_app()
            print("[OTA] Firebase app is ready")
            return True
        except ValueError:
            time.sleep(1)
    print("[OTA] WARNING: Firebase not initialized after 30s, continuing anyway...")
    return False


def run():
    # Chờ Firebase initialize (bởi firebase_sync.py)
    _wait_for_firebase()
    
    bus   = MessageBus.get_instance()
    redis = bus.get_redis()
    print("[OTA] OTA Manager started")

    # Luồng 1: Nhận lệnh từ Redis (upload mới / user confirm)
    threading.Thread(target=_redis_listener,
                     args=(bus,), daemon=True).start()

    # Luồng 2: Poll Firestore để bắt user confirm
    # (Firestore Python SDK không hỗ trợ on_snapshot tốt trong thread)
    _firestore_poller(bus)


def _redis_listener(bus):
    """Nhận lệnh new_firmware từ ota_upload() endpoint."""
    redis  = bus.get_redis()
    pubsub = redis.pubsub()
    pubsub.subscribe("ota_commands", "ota_status") 
    for msg in pubsub.listen():
        if msg["type"] != "message": continue
        try:
            data    = json.loads(msg["data"])
            channel = msg["channel"]

            if channel == "ota_commands":
                if data.get("action") == "new_firmware":
                    _write_ota_notice(bus, data)

            elif channel == "ota_status":
                _handle_esp32_ota_event(data)

        except Exception as e:
            print(f"[OTA] redis listener error: {e}")

def _handle_esp32_ota_event(data: dict):
    """Cập nhật Firestore ota_notices dựa trên event từ ESP32."""
    event  = data.get('event')
    doc_id = data.get('doc_id', '')
    if not doc_id: return

    fs  = firestore.client()
    ref = fs.collection('ota_notices').document(doc_id)

    if event == 'ota_done':
        ref.update({
            'status':    'done',
            'version':   data.get('version', ''),
            'error':     '',
            'updatedAt': firestore.SERVER_TIMESTAMP
        })
        # Cập nhật RTDB firmware_versions
        _update_rtdb_version(data.get('room_id'), data.get('version'))
        print(f"[OTA] {doc_id}: DONE v{data.get('version')}")

    elif event == 'ota_failed':
        snap = ref.get()
        if snap.exists and snap.to_dict().get('status') != 'done':
            ref.update({
                'status':    'failed',
                'error':     data.get('error', 'ESP32 reported failure'),
                'updatedAt': firestore.SERVER_TIMESTAMP
            })
        print(f"[OTA] {doc_id}: FAILED — {data.get('error')}")


def _update_rtdb_version(room_id: str, version: str):
    try:
        db.reference(f'firmware_versions/{room_id}').update({
            'version':     version,
            'last_update': int(time.time())
        })
    except Exception as e:
        print(f'[OTA] RTDB version update error: {e}')


def _write_ota_notice(bus, data: dict):
    """
    Ghi notice lên Firestore ota_notices/{room}.
    Web onSnapshot bắt được và hiện popup cho user.
    """
    try:
        fs = firestore.client()
        room = data["room"]
        doc_id = f"ota_{room}_{int(time.time())}"

        fs.collection("ota_notices").document(doc_id).set({
            "room":          room,
            "filename":      data["filename"],
            "url":           data["url"],
            "version":       data["version"],
            "releaseNotes":  data.get("release_notes", ""),
            "status":        "pending",   # pending→confirmed→flashing→done/failed
            "createdAt":     firestore.SERVER_TIMESTAMP,
            "updatedAt":     firestore.SERVER_TIMESTAMP,
        })

        # Lưu doc_id vào Redis để poller biết cần theo dõi
        bus.get_redis().lpush("ota_pending_notices", json.dumps({
            "doc_id": doc_id, "room": room,
            "url": data["url"], "version": data["version"]
        }))
        print(f"[OTA] Notice written: {doc_id} for room={room}")
    except Exception as e:
        print(f"[OTA] write notice error: {e}")


def _firestore_poller(bus):
    """
    Poll Firestore ota_notices mỗi 10s để bắt user confirm.
    Khi status=confirmed → gửi MQTT xuống ESP32.
    """
    redis = bus.get_redis()

    while True:
        try:
            fs = firestore.client()  # Move inside loop để retry nếu Firebase chưa ready
            docs = fs.collection("ota_notices") \
                     .where(filter=FieldFilter("status", "in", ["confirmed"])) \
                     .stream()

            for doc_snap in docs:
                doc_id = doc_snap.id
                data   = doc_snap.to_dict()
                room   = data["room"]
                url    = data["url"]
                ver    = data["version"]

                print(f"[OTA] User confirmed update for {room} v{ver}")

                # Đổi status → flashing NGAY để tránh dispatch lặp
                doc_snap.reference.update({
                    "status": "flashing",
                    "updatedAt": firestore.SERVER_TIMESTAMP
                })

                # Gửi MQTT lệnh OTA xuống ESP32 node
                _dispatch_ota(bus, room, url, ver, doc_id)

        except Exception as e:
            print(f"[OTA] poller error: {e}")

        time.sleep(10)  # Poll mỗi 10 giây


def _dispatch_ota(bus, room: str, url: str, version: str, doc_id: str):
    """Gửi MQTT lệnh ota_update xuống ESP32 node."""
    payload = {
        "action":  "ota_update",
        "url":     url,
        "version": version,
        "doc_id":  doc_id   # ESP32 gửi lại doc_id trong status callback
    }
    bus.publish_mqtt(f"home/{room}/command", payload)
    print(f"[OTA] MQTT sent to {room}: ota_update → {url}")

    # Timeout guard: nếu 10 phút không có status callback → đánh dấu failed
    def _timeout_guard():
        time.sleep(600)
        try:
            doc_ref = firestore.client().collection("ota_notices").document(doc_id)
            snap    = doc_ref.get()
            if snap.exists and snap.to_dict().get("status") == "flashing":
                doc_ref.update({
                    "status": "failed",
                    "error": "Timeout: ESP32 did not respond within 10 minutes",
                    "updatedAt": firestore.SERVER_TIMESTAMP
                })
                print(f"[OTA] Timeout for {doc_id}")
        except Exception as ex:
            print(f"[OTA] timeout guard error: {ex}")

    threading.Thread(target=_timeout_guard, daemon=True).start()