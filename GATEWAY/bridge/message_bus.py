"""
bridge/message_bus.py
═════════════════════
Internal Message Bus – chuyển tiếp tin nhắn giữa MQTT và Redis.
Đây là "xương sống" của hệ thống, tách biệt hoàn toàn phần thu/phát
khỏi phần xử lý logic.

Luồng:
  ESP32 → MQTT → [on_message] → Redis pubsub "mqtt_inbound"
  Redis pubsub "mqtt_outbound" → [_outbound_loop] → MQTT → ESP32
"""

import json
import threading
import time
import paho.mqtt.client as mqtt
import redis

MQTT_BROKER    = "localhost"
MQTT_PORT      = 1883
REDIS_HOST     = "localhost"
MQTT_TOPIC_SUB = "home/+/+"          # home/{room}/{category}
# [FIX-RFID-1] ESP32 living room gửi enrollment result qua home/{room}/enroll
# MQTT_TOPIC_SUB "home/+/+" đã cover topic này (3 segments = 2 wildcards + 1 prefix)
# Không cần thêm subscription riêng vì wildcard đã match

# Channels Redis nội bộ
CH_INBOUND    = "mqtt_inbound"         # MQTT → các worker đọc
CH_OUTBOUND   = "mqtt_outbound"        # Các worker viết → MQTT
EVENT_QUEUE   = "event_queue"         # Persist event logs for SQLite


class MessageBus:
    """
    Singleton bus kết nối MQTT ↔ Redis.
    Các service khác chỉ cần:
        bus = MessageBus.get_instance()
        bus.publish_mqtt(topic, payload_dict)
    """
    _instance = None
    _lock     = threading.Lock()

    def __init__(self):
        self.r           = redis.Redis(host=REDIS_HOST, port=6379, decode_responses=True)
        self._mqtt       = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self._connected  = False
        self._setup_mqtt()

    @classmethod
    def get_instance(cls) -> "MessageBus":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    # ── MQTT Setup ──────────────────────────────────────────

    def _setup_mqtt(self):
        self._mqtt.on_connect    = self._on_connect
        self._mqtt.on_message    = self._on_message
        self._mqtt.on_disconnect = self._on_disconnect

    def connect(self):
        """Kết nối MQTT và bắt đầu luồng outbound."""
        retry = 0
        while True:
            try:
                self._mqtt.connect(MQTT_BROKER, MQTT_PORT, 60)
                self._mqtt.loop_start()
                threading.Thread(target=self._outbound_loop, daemon=True).start()
                print("[BUS] MessageBus started")
                return
            except Exception as e:
                retry += 1
                wait = min(30, 2 ** retry)
                print(f"[BUS] MQTT connect failed ({e}), retry in {wait}s...")
                time.sleep(wait)

    def _on_connect(self, client, userdata, flags, rc, props=None):
        if rc == 0:
            self._connected = True
            client.subscribe(MQTT_TOPIC_SUB)
            client.subscribe("smarthome/system/#")
            print("[BUS] MQTT connected & subscribed")
        else:
            print(f"[BUS] MQTT connect error: {rc}")

    def _on_disconnect(self, client, userdata, rc, props=None, reason=None):
        self._connected = False
        print(f"[BUS] MQTT disconnected (rc={rc}), will reconnect...")

    def _on_message(self, client, userdata, msg):
        """Nhận từ MQTT → đẩy vào Redis CH_INBOUND để các worker xử lý."""
        try:
            payload = json.loads(msg.payload.decode())
            envelope = {
                "topic":   msg.topic,
                "payload": payload,
                "ts":      time.time()
            }
            self.r.publish(CH_INBOUND, json.dumps(envelope))
        except Exception as e:
            print(f"[BUS] inbound parse error: {e}")

    # ── Outbound: Redis → MQTT ───────────────────────────────

    def _outbound_loop(self):
        """
        Lắng nghe Redis CH_OUTBOUND và gửi xuống MQTT.
        Format message: {"topic": "home/room/command", "payload": {...}}
        """
        pubsub = self.r.pubsub()
        pubsub.subscribe(CH_OUTBOUND)
        print("[BUS] Outbound loop started")
        for message in pubsub.listen():
            if message["type"] != "message":
                continue
            try:
                data    = json.loads(message["data"])
                topic   = data["topic"]
                payload = json.dumps(data["payload"])
                if self._connected:
                    self._mqtt.publish(topic, payload)
                else:
                    # Lưu vào Redis queue để gửi lại khi reconnect
                    self.r.lpush("mqtt_pending_queue", json.dumps(data))
            except Exception as e:
                print(f"[BUS] outbound error: {e}")

    # ── Public API ───────────────────────────────────────────

    def publish_mqtt(self, topic: str, payload: dict):
        """Gửi lệnh xuống thiết bị qua MQTT (thread-safe)."""
        self.r.publish(CH_OUTBOUND, json.dumps({"topic": topic, "payload": payload}))

    def publish_event(self, channel: str, data: dict):
        """Phát sự kiện lên Redis channel và ghi vào queue persistence."""
        payload = json.dumps(data)
        self.r.publish(channel, payload)
        try:
            self.r.rpush(EVENT_QUEUE, payload)
        except Exception as e:
            print(f"[BUS] event queue write failed: {e}")

    def get_redis(self) -> redis.Redis:
        return self.r
