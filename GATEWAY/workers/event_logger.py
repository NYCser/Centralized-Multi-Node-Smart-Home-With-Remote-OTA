# workers/event_logger.py
"""
Centralized Event Logger
════════════════════════
Ghi nhật ký tất cả các event từ hệ thống nhà thông minh vào Firestore system_alerts.

Event Types:
- schedule:          Hẹn lịch tắt thiết bị (thực hiện/bị chặn)
- automation:        Tự động hóa (kích hoạt/bị chặn)
- manual_control:    Điều khiển thủ công
- manual_blocked:    Thông báo automation bị chặn do manual control
- safety_alert:      Cảnh báo an toàn (gas/lửa)
- door_access:       Ra vào cửa (RFID/vân tay)
- rfid_enroll:       Đăng ký thẻ RFID
- wifi_status:       Trạng thái WiFi (connect/disconnect)
- ota_update:        OTA update
- system:            Hệ thống (khác)
"""

import json
import time
from datetime import datetime
from firebase_admin import firestore
from bridge.message_bus import MessageBus


def log_event(event_type: str, message: str, room_id: str = "system", 
              level: str = "info", metadata: dict = None, is_resolved: bool = False):
    """
    Ghi event vào Firestore system_alerts.
    
    Args:
        event_type: schedule | automation | manual_control | safety_alert | door_access | rfid_enroll | wifi_status | ota_update | system
        message: Mô tả event (gợi ý: "{action} | {device} | {reason}")
        room_id: phòng liên quan (mặc định "system")
        level: info | warning | critical
        metadata: dict bổ sung (vd: device_id, action, trigger_reason)
        is_resolved: True nếu đây là event giải quyết/khôi phục
    """
    try:
        fs = firestore.client()
        doc_data = {
            "type": event_type,
            "message": message,
            "room_id": room_id,
            "level": level,
            "timestamp": firestore.SERVER_TIMESTAMP,
            "isResolved": is_resolved,
            "metadata": metadata or {},
        }
        
        # Thêm vào Firestore
        fs.collection("system_alerts").add(doc_data)
        
        # In log để theo dõi
        time_str = datetime.now().strftime("%H:%M:%S")
        level_symbol = {"info": "ℹ", "warning": "⚠", "critical": "🚨"}[level]
        print(f"[{time_str}] {level_symbol} [EVENT] {event_type.upper()}: {message}")
        
        return True
    except Exception as e:
        print(f"[EVENT] Log error: {e}")
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# Helper functions cho các loại event thường gặp
# ═══════════════════════════════════════════════════════════════════════════════

def log_schedule_executed(room_id: str, device_id: str, device_name: str):
    """Hẹn lịch được thực hiện"""
    log_event(
        event_type="schedule",
        message=f"Hẹn lịch tắt thiết bị | {device_name} | Đã thực hiện",
        room_id=room_id,
        level="info",
        metadata={"device_id": device_id, "device_name": device_name, "action": "executed"}
    )


def log_schedule_blocked(room_id: str, device_id: str, device_name: str, reason: str):
    """Hẹn lịch bị chặn (vd: schedule active đang chạy)"""
    log_event(
        event_type="schedule",
        message=f"Hẹn lịch bị chặn | {device_name} | {reason}",
        room_id=room_id,
        level="warning",
        metadata={"device_id": device_id, "device_name": device_name, "reason": reason}
    )


def log_automation_triggered(room_id: str, device_id: str, device_name: str, condition: str):
    """Tự động hóa được kích hoạt"""
    log_event(
        event_type="automation",
        message=f"Tự động hóa kích hoạt | {device_name} | Điều kiện: {condition}",
        room_id=room_id,
        level="info",
        metadata={"device_id": device_id, "condition": condition, "action": "triggered"}
    )


def log_automation_blocked(room_id: str, device_id: str, device_name: str, reason: str):
    """Tự động hóa bị chặn (vd: manual override, safety lock)"""
    log_event(
        event_type="automation",
        message=f"Tự động hóa bị chặn | {device_name} | {reason}",
        room_id=room_id,
        level="warning",
        metadata={"device_id": device_id, "reason": reason}
    )


def log_manual_control(room_id: str, device_id: str, device_name: str, action: str):
    """Điều khiển thiết bị thủ công"""
    log_event(
        event_type="manual_control",
        message=f"Điều khiển thủ công | {device_name} → {action.upper()} | Automation sẽ bị chặn 5 phút",
        room_id=room_id,
        level="info",
        metadata={"device_id": device_id, "device_name": device_name, "action": action}
    )


def log_manual_control_alert(room_id: str, device_id: str, device_name: str):
    """Thông báo automation bị chặn do manual control"""
    log_event(
        event_type="manual_blocked",
        message=f"⏸ Automation tạm dừng | {device_name} | Vì vừa điều khiển thủ công (5 phút)",
        room_id=room_id,
        level="warning",
        metadata={"device_id": device_id, "device_name": device_name}
    )


def log_safety_alert(room_id: str, alert_type: str, value: float = None, message: str = None):
    """Cảnh báo an toàn (gas/lửa)"""
    if message is None:
        if alert_type == "gas":
            message = f"Cảnh báo khí gas phát hiện | CO2: {value} ppm | HỆ THỐNG BỊ KHÓA"
        elif alert_type == "fire":
            message = f"Cảnh báo lửa phát hiện | Nhiệt độ: {value}°C | HỆ THỐNG BỊ KHÓA"
        else:
            message = f"Cảnh báo an toàn | {alert_type}"
    
    log_event(
        event_type="safety_alert",
        message=message,
        room_id=room_id,
        level="critical",
        metadata={"alert_type": alert_type, "value": value}
    )


def log_safety_resolved(room_id: str, alert_type: str):
    """Cảnh báo an toàn được xử lý (hệ thống bình thường trở lại)"""
    log_event(
        event_type="safety_alert",
        message=f"✓ Cảnh báo {alert_type.upper()} được xử lý | Hệ thống bình thường",
        room_id=room_id,
        level="info",
        is_resolved=True,
        metadata={"alert_type": alert_type, "resolved": True}
    )


def log_door_access(room_id: str, uid: str, owner_name: str, access_type: str, method: str):
    """Ra vào cửa (RFID/vân tay)"""
    access_symbol = "🔓" if access_type == "granted" else "🚫"
    message_map = {
        "granted": f"{access_symbol} Cấp quyền vào | Người: {owner_name} | Phương thức: {method}",
        "denied": f"{access_symbol} Từ chối vào | UID: {uid} | Phương thức: {method}",
        "unknown": f"{access_symbol} Thẻ không xác định | UID: {uid}",
    }
    
    log_event(
        event_type="door_access",
        message=message_map.get(access_type, "Ra vào cửa"),
        room_id=room_id,
        level="info" if access_type == "granted" else "warning",
        metadata={"uid": uid, "access_type": access_type, "method": method}
    )


def log_rfid_enrollment(room_id: str, uid: str, owner_name: str):
    """Đăng ký thẻ RFID mới"""
    log_event(
        event_type="rfid_enroll",
        message=f"Đăng ký thẻ RFID | Người: {owner_name}",
        room_id=room_id,
        level="info",
        metadata={"uid": uid, "owner_name": owner_name}
    )


def log_wifi_status(room_id: str, status: str, ssid: str = "", signal_strength: int = None):
    """Trạng thái WiFi"""
    if status == "connected":
        message = f"✓ Kết nối WiFi thành công | {ssid or 'SmartHome_Hub'} | Tín hiệu: {signal_strength}%"
        level = "info"
    else:  # disconnected
        message = f"✗ Mất kết nối WiFi | {ssid or 'SmartHome_Hub'}"
        level = "warning"
    
    log_event(
        event_type="wifi_status",
        message=message,
        room_id=room_id,
        level=level,
        metadata={"status": status, "ssid": ssid, "signal_strength": signal_strength}
    )


def log_ota_update(room_id: str, version: str, status: str, error: str = ""):
    """OTA update"""
    if status == "started":
        message = f"🔄 OTA update bắt đầu | Phiên bản: {version}"
        level = "info"
    elif status == "completed":
        message = f"✓ OTA update thành công | Phiên bản: {version}"
        level = "info"
    elif status == "failed":
        message = f"✗ OTA update thất bại | Lỗi: {error}"
        level = "critical"
    else:
        message = f"OTA update | {status}"
        level = "warning"
    
    log_event(
        event_type="ota_update",
        message=message,
        room_id=room_id,
        level=level,
        metadata={"version": version, "status": status, "error": error}
    )


def log_temperature_threshold_triggered(room_id: str, device_id: str, device_name: str, 
                                        current_temp: float, threshold: float):
    """Nhiệt độ vượt ngưỡng → tự động bật thiết bị"""
    log_event(
        event_type="automation",
        message=f"Tự động hóa theo nhiệt độ | {device_name} tự bật | Nhiệt độ: {current_temp}°C > {threshold}°C",
        room_id=room_id,
        level="info",
        metadata={
            "device_id": device_id,
            "current_temp": current_temp,
            "threshold": threshold,
            "trigger": "temperature"
        }
    )


def log_system_message(room_id: str, message: str, level: str = "info"):
    """Tin nhắn hệ thống tổng quát"""
    log_event(
        event_type="system",
        message=message,
        room_id=room_id,
        level=level
    )
