"""
schedule_dedup_patch.py — Fix BUG-SCHEDULE-SPAM
══════════════════════════════════════════════════════════════
VẤN ĐỀ:
    Từ log 13:26:00 trở đi, schedule → fan_kt_1 bị BLOCKED hàng chục lần/phút:
      [DISPATCH] BLOCKED (manual_override): schedule → fan_kt_1
    
    Nguyên nhân kép:
      1. 19 schedules active, nhiều schedule trùng device_id trong cùng phút.
      2. Scheduler loop không có dedup window → cùng 1 schedule trigger nhiều lần.
      3. manual_override TTL (5 phút) chưa hết mà schedule vẫn cố chạy liên tục.

CÁCH TÍCH HỢP:
    Thay vì sửa thẳng vào dispatcher, dùng wrapper mixin:

    Trong file auto_scheduler.py (hoặc tương đương), thay:
        dispatcher.send(room, device_id, action, source="schedule")
    Bằng:
        from schedule_dedup_patch import schedule_send_once
        schedule_send_once(dispatcher, room, device_id, action, schedule_id)

    Hoặc thêm decorator @dedup_schedule vào hàm schedule runner.
"""

import logging
import time
from datetime import datetime
from threading import Lock

log = logging.getLogger("schedule_dedup")


class ScheduleDeduplicator:
    """
    Đảm bảo mỗi (device_id, action, minute_window) chỉ được dispatch một lần.
    Thread-safe, in-memory (không cần DB).
    """

    DEDUP_WINDOW_SECONDS = 55  # Trong cùng 1 phút, chỉ gửi 1 lần

    def __init__(self):
        self._lock    = Lock()
        self._history: dict[str, float] = {}  # key → last_sent_ts
        self._cleanup_interval = 300           # Dọn history 5 phút/lần
        self._last_cleanup = time.time()

    def _make_key(self, device_id: str, action: str) -> str:
        minute_bucket = datetime.now().strftime("%Y-%m-%d %H:%M")
        return f"{device_id}:{action}:{minute_bucket}"

    def should_send(self, device_id: str, action: str) -> bool:
        """
        Trả về True nếu nên gửi lệnh này.
        False nếu đã gửi trong cùng DEDUP_WINDOW_SECONDS gần nhất.
        """
        key = self._make_key(device_id, action)
        now = time.time()

        with self._lock:
            # Cleanup cũ định kỳ
            if now - self._last_cleanup > self._cleanup_interval:
                expired = [
                    k for k, ts in self._history.items()
                    if now - ts > self._cleanup_interval
                ]
                for k in expired:
                    del self._history[k]
                self._last_cleanup = now

            last = self._history.get(key, 0)
            if now - last < self.DEDUP_WINDOW_SECONDS:
                return False

            self._history[key] = now
            return True

    def record_blocked(self, device_id: str, action: str, reason: str = "manual_override"):
        """
        Ghi nhận lệnh bị block. Nếu bị block, KHÔNG reset timer
        (tránh schedule tiếp tục spam sau khi TTL hết).
        """
        log.debug("Schedule dedup: %s → %s blocked by %s (suppressed)", device_id, action, reason)


# Singleton
_dedup = ScheduleDeduplicator()


def schedule_send_once(dispatcher, room: str, device_id: str, action: str,
                       schedule_id: int = 0) -> bool:
    """
    Wrapper gửi lệnh schedule với dedup.
    
    Dùng thay cho:
        dispatcher.send(room, device_id, action, source="schedule")
    
    Trả về True nếu lệnh được gửi, False nếu bị dedup hoặc blocked.
    """
    if not _dedup.should_send(device_id, action):
        log.debug("Schedule dedup: skip %s → %s (already sent this minute)", device_id, action)
        return False

    try:
        result = dispatcher.send(room, device_id, action, source="schedule")
        if result is False:  # manual_override
            _dedup.record_blocked(device_id, action)
            return False
        log.info("Schedule dispatched: %s/%s → %s (id=%d)", room, device_id, action, schedule_id)
        return True
    except Exception as e:
        log.error("schedule_send_once error: %s", e)
        return False


def patch_schedule_runner(schedule_runner_func):
    """
    Decorator để patch vào hàm chạy schedules hiện tại.
    
    Dùng:
        @patch_schedule_runner  
        def run_schedules(self):
            ...
    """
    def wrapper(self, *args, **kwargs):
        # Inject dedup vào self nếu chưa có
        if not hasattr(self, "_schedule_dedup"):
            self._schedule_dedup = ScheduleDeduplicator()
        return schedule_runner_func(self, *args, **kwargs)
    return wrapper