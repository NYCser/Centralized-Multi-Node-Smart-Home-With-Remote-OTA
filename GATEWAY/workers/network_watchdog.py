"""
workers/network_watchdog.py  — FIXED v2
═══════════════════════════════════════════
FIXES:
  BUG-RTC-01: Loại bỏ hoàn toàn phụ thuộc RTC DS3231.
              Hệ thống dùng NTP-only — Raspberry Pi OS đã tích hợp NTP daemon
              (systemd-timesyncd hoặc ntp/chrony). Không cần đọc/ghi RTC phần cứng.

  BUG-WIFI-SYNC-01: Sau khi WiFi scan xong, publish kết quả lên Redis channel
              "wifi_status" để firebase_sync.py đẩy lên Firestore
              (system_status/available_wifi) → settings.js hiển thị đúng.

  BUG-WIFI-SYNC-02: Sau khi connect WiFi thành công, publish trạng thái
              lên "wifi_status" channel để cập nhật Firestore system_status/wifi.

  BUG-NET-01:  get_wifi_status() kiểm tra trạng thái hiện tại và publish
              lên Redis mỗi CHECK_EVERY giây → dashboard luôn cập nhật đúng.
"""

import time
import json
import subprocess
import threading
from datetime import datetime

import redis as redis_lib

REDIS_HOST   = "localhost"
HOTSPOT_SSID = "SmartHome_Hub"
HOTSPOT_PASS = ""               # Mạng mở
HOTSPOT_IP   = "10.42.0.1/24"
INTERFACE    = "wlan0"
CHECK_EVERY  = 30               # giây kiểm tra network
NTP_SYNC_EVERY = 3600           # Sync NTP mỗi 1 giờ (khi có internet)


def get_redis():
    return redis_lib.Redis(host=REDIS_HOST, port=6379, decode_responses=True)


# ── Hotspot ────────────────────────────────────────────────

def ensure_hotspot():
    """Tạo / khởi động lại Hotspot nếu chưa active."""
    try:
        result = subprocess.run(
            f"nmcli -t con show --active | grep '{HOTSPOT_SSID}'",
            shell=True, capture_output=True
        )
        if result.returncode == 0:
            return  # Đang chạy rồi

        subprocess.run(f"sudo nmcli connection delete '{HOTSPOT_SSID}'",
                       shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(f"sudo nmcli dev disconnect {INTERFACE}",
                       shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        cmds = [
            f"sudo nmcli con add type wifi ifname {INTERFACE} con-name '{HOTSPOT_SSID}' autoconnect yes ssid '{HOTSPOT_SSID}'",
            f"sudo nmcli con modify '{HOTSPOT_SSID}' 802-11-wireless.mode ap",
            f"sudo nmcli con modify '{HOTSPOT_SSID}' 802-11-wireless.band bg",
            f"sudo nmcli con modify '{HOTSPOT_SSID}' 802-11-wireless.channel 6",
            f"sudo nmcli con modify '{HOTSPOT_SSID}' remove wifi-sec",
            f"sudo nmcli con modify '{HOTSPOT_SSID}' ipv4.addresses {HOTSPOT_IP}",
            f"sudo nmcli con modify '{HOTSPOT_SSID}' ipv4.method manual",
            f"sudo nmcli con modify '{HOTSPOT_SSID}' connection.autoconnect-priority 100",
            f"sudo nmcli con up '{HOTSPOT_SSID}'",
        ]
        for cmd in cmds:
            subprocess.run(cmd, shell=True, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"[NET] Hotspot '{HOTSPOT_SSID}' activated at {HOTSPOT_IP}")
    except Exception as e:
        print(f"[NET] Hotspot error: {e}")


# ── Internet check ────────────────────────────────────────

def check_internet() -> bool:
    try:
        subprocess.check_call(
            ["ping", "-c", "1", "-W", "3", "8.8.8.8"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        return True
    except Exception:
        return False


def get_wifi_status() -> dict:
    """Lấy trạng thái kết nối WiFi hiện tại."""
    try:
        cmd    = "nmcli -t -f ACTIVE,SSID,MODE dev wifi | grep '^yes' | grep ':infrastructure'"
        output = subprocess.check_output(cmd, shell=True).decode().strip()
        if output:
            ssid = output.split(":")[1]
            return {"status": "connected", "ssid": ssid, "type": "wifi", "current_ssid": ssid}
    except Exception:
        pass

    # Kiểm tra Ethernet
    try:
        subprocess.check_call(
            ["ping", "-c", "1", "-W", "3", "8.8.8.8"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        return {"status": "connected", "ssid": "Ethernet/Wired", "type": "ethernet",
                "current_ssid": "Ethernet/Wired"}
    except Exception:
        pass

    return {"status": "disconnected", "ssid": "N/A", "type": "none", "current_ssid": ""}


# ── NTP Sync (BUG-RTC-01: Không dùng RTC DS3231) ─────────

def sync_ntp():
    """
    FIX BUG-RTC-01: Chỉ sync NTP khi có Internet.
    Raspberry Pi OS (Raspbian Bullseye/Bookworm) đã có systemd-timesyncd
    tự động sync khi có mạng. Hàm này là fallback thủ công nếu cần.
    KHÔNG cần đọc/ghi RTC DS3231 nữa.
    """
    try:
        # Thử dùng timedatectl (systemd) trước — chuẩn nhất trên Pi OS hiện đại
        result = subprocess.run(
            ["timedatectl", "show", "--property=NTPSynchronized", "--value"],
            capture_output=True, timeout=5
        )
        if result.returncode == 0 and result.stdout.decode().strip() == "yes":
            print(f"[NET] NTP already synchronized via systemd-timesyncd")
            return

        # Fallback: force sync với ntpdate nếu systemd-timesyncd không active
        subprocess.run(
            ["sudo", "ntpdate", "-u", "pool.ntp.org"],
            timeout=15, check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        print(f"[NET] NTP synced manually: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    except subprocess.CalledProcessError:
        # ntpdate không có → thử chronyc
        try:
            subprocess.run(["sudo", "chronyc", "makestep"],
                           timeout=10, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print(f"[NET] NTP synced via chrony")
        except Exception:
            pass
    except Exception as e:
        print(f"[NET] NTP sync warning (non-critical): {e}")


def json_serializable(obj):
    """
    Xử lý các kiểu dữ liệu không mặc định cho JSON (đặc biệt là từ Firestore).
    """
    # Kiểm tra nếu là đối tượng Datetime của Firestore hoặc Datetime chuẩn
    if hasattr(obj, 'isoformat'):
        return obj.isoformat()
    
    # Nếu là các kiểu dữ liệu cơ bản thì giữ nguyên
    if isinstance(obj, (int, float, str, bool)) or obj is None:
        return obj
        
    # Trường hợp cuối cùng: ép về string để tránh lỗi crash hệ thống
    return str(obj)

# ── WiFi Connect ──────────────────────────────────────────

def connect_wifi(ssid: str, password: str, request_id: str, r):
    """Kết nối vào WiFi nhà (uplink)."""
    cmd = (f"sudo nmcli dev wifi connect '{ssid}' password '{password}'"
           if password else f"sudo nmcli dev wifi connect '{ssid}'")
    try:
        subprocess.run(cmd, shell=True, check=True, timeout=45,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        result = {"status": "success", "ssid": ssid}
        print(f"[NET] Connected to WiFi: {ssid}")

        # FIX BUG-WIFI-SYNC-02: Publish wifi status lên Redis → firebase_sync cập nhật Firestore
        status_payload = {
            "status":       "connected",
            "ssid":         ssid,
            "current_ssid": ssid,
            "type":         "wifi"
        }
        r.publish("wifi_status", json.dumps(status_payload))

    except Exception as e:
        result = {"status": "failed", "error": str(e)}
        print(f"[NET] WiFi connect failed: {e}")

        # Publish disconnect status
        r.publish("wifi_status", json.dumps({
            "status":       "disconnected",
            "ssid":         "",
            "current_ssid": "",
            "error":        str(e)
        }))
    finally:
        if request_id:
            r.setex(f"wifi_cmd:{request_id}", 60, json.dumps(result, default=json_serializable)) # Thêm default vào đây
        # Khởi động lại Hotspot sau khi kết nối uplink
        threading.Thread(target=ensure_hotspot, daemon=True).start()


# ── WiFi Scan ─────────────────────────────────────────────

def scan_wifi(r):
    """Quét mạng WiFi xung quanh và publish lên Firestore qua Redis."""
    try:
        subprocess.run("sudo nmcli dev wifi rescan",
                       shell=True, timeout=10,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(3)
        output = subprocess.check_output(
            "nmcli -t -f SSID,SIGNAL dev wifi list", shell=True
        ).decode()
        networks = []
        seen = set()
        for line in output.strip().split("\n"):
            parts = line.split(":")
            if len(parts) >= 2 and parts[0] and parts[0] not in seen:
                seen.add(parts[0])
                networks.append({
                    "ssid":   parts[0],
                    "signal": int(parts[1]) if parts[1].isdigit() else 0
                })
        networks.sort(key=lambda x: -x["signal"])

        r.setex("wifi_scan_result", 300, json.dumps(networks))
        r.set("wifi_scan_status", "done")

        # FIX BUG-WIFI-SYNC-01: Publish lên wifi_status channel để firebase_sync
        # cập nhật Firestore system_status/available_wifi → settings.js hiển thị
        r.publish("wifi_status", json.dumps({
            "networks": networks,
            "scan_done": True
        }))

        # Push lên realtime_data cho Web WebSocket
        r.publish("realtime_data", json.dumps({
            "event":    "wifi_scan_done",
            "networks": networks
        }, default=json_serializable))
        print(f"[NET] WiFi scan done: {len(networks)} networks")
    except Exception as e:
        r.set("wifi_scan_status", "error")
        print(f"[NET] WiFi scan error: {e}")


# ── Main loop ─────────────────────────────────────────────

def run():
    r            = get_redis()
    last_check   = 0
    last_ntp     = 0
    has_internet = False

    # Lắng nghe lệnh WiFi từ Web
    def listen_wifi_commands():
        pub = r.pubsub()
        pub.subscribe("wifi_commands", "wifi_scan_trigger")
        for msg in pub.listen():
            if msg["type"] != "message":
                continue
            try:
                channel = msg["channel"]
                if channel == "wifi_scan_trigger":
                    r.set("wifi_scan_status", "scanning")
                    threading.Thread(target=scan_wifi, args=(r,), daemon=True).start()
                elif channel == "wifi_commands":
                    data = json.loads(msg["data"])
                    threading.Thread(
                        target=connect_wifi,
                        args=(data.get("ssid"), data.get("password", ""),
                              data.get("request_id"), r),
                        daemon=True
                    ).start()
            except Exception as e:
                print(f"[NET] wifi command error: {e}")

    threading.Thread(target=listen_wifi_commands, daemon=True).start()

    # FIX BUG-RTC-01: Không cần read_rtc_to_system() nữa.
    # systemd-timesyncd tự đồng bộ NTP khi có mạng.
    # Nếu mới khởi động mà chưa có mạng, giờ hệ thống vẫn đúng từ lần sync trước.
    print("[NET] Network watchdog started (NTP-only, no RTC)")

    # Đảm bảo Hotspot active
    ensure_hotspot()

    while True:
        now = time.time()
        if (now - last_check) >= CHECK_EVERY:
            last_check = now

            status = get_wifi_status()
            r.setex("system_status:wifi", 120, json.dumps(status))

            new_internet = check_internet()
            if new_internet != has_internet:
                has_internet = new_internet
                event = "internet_online" if has_internet else "internet_offline"
                r.publish("realtime_data", json.dumps({
                    "event":   event,
                    "message": "Đã có Internet" if has_internet else "Mất kết nối Internet"
                }))
                print(f"[NET] Internet: {'ON' if has_internet else 'OFF'}")

                # FIX BUG-WIFI-SYNC-02: Publish wifi status khi trạng thái thay đổi
                r.publish("wifi_status", json.dumps({
                    "status":       status.get("status", "disconnected"),
                    "ssid":         status.get("ssid", ""),
                    "current_ssid": status.get("current_ssid", ""),
                    "type":         status.get("type", "none")
                }))

            # FIX BUG-RTC-01: NTP sync thủ công mỗi giờ khi có Internet (không ghi RTC)
            if has_internet and (now - last_ntp) >= NTP_SYNC_EVERY:
                sync_ntp()
                last_ntp = now

        time.sleep(5)


if __name__ == "__main__":
    run()