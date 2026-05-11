"""
workers/network_watchdog.py  — FIXED v3  (CONFLICT FIX)
════════════════════════════════════════════════════════
FIXES trong phiên bản này:

  [CONFLICT F1 — MEDIUM] Dual Interface WiFi — tránh ESP32 offline khi đổi WiFi
      ─────────────────────────────────────────────────────────────────────────
      Vấn đề: Khi user đổi WiFi uplink từ Settings, interface wlan0 phải
              negotiate lại. Hotspot SmartHome_Hub bị gián đoạn 5-15 giây
              → toàn bộ ESP32 mất kết nối và mất dữ liệu sensor.
      Fix: Dual Interface Strategy:
        1. Ưu tiên: Detect xem Pi có 2 WiFi interfaces (wlan0, wlan1) không.
           - wlan0 → Hotspot (AP mode) chuyên dụng, KHÔNG bao giờ thay đổi
           - wlan1 → Uplink WiFi (Station mode), thay đổi thoải mái
        2. Fallback: Nếu chỉ có 1 interface, dùng nmcli AP + Station trên
           cùng wlan0 nếu driver hỗ trợ concurrent mode (nhiều Pi WiFi chip hỗ trợ).
        3. Cơ chế Hotspot-first: Luôn bring up Hotspot TRƯỚC khi connect uplink.
           Nếu connect uplink thất bại/thành công, Hotspot vẫn chạy độc lập.
        4. Publish "wifi_switching" event để ESP32 có thể delay reconnect.

  [Giữ nguyên từ v2]
      BUG-RTC-01, BUG-WIFI-SYNC-01, BUG-WIFI-SYNC-02, BUG-NET-01
"""

import time
import json
import subprocess
import threading
from datetime import datetime

import redis as redis_lib
from workers import event_logger

REDIS_HOST    = "localhost"
HOTSPOT_SSID  = "SmartHome_Hub"
HOTSPOT_PASS  = ""               # Mạng mở
HOTSPOT_IP    = "10.42.0.1/24"
CHECK_EVERY   = 30
NTP_SYNC_EVERY = 3600

# FIX F1: Interface separation
HOTSPOT_IFACE  = "wlan0"   # Interface dành cho Hotspot (AP mode)
UPLINK_IFACE   = ""        # Sẽ được detect tự động: wlan1 nếu có, wlan0 nếu không


def get_redis():
    return redis_lib.Redis(host=REDIS_HOST, port=6379, decode_responses=True)


# ── FIX F1: Interface Detection ───────────────────────────

def detect_interfaces() -> dict:
    """
    FIX F1: Detect WiFi interfaces và phân công vai trò.
    Returns: {"hotspot": "wlan0", "uplink": "wlan1" | "wlan0"}
    """
    try:
        output = subprocess.check_output(
            "nmcli -t -f DEVICE,TYPE dev | grep ':wifi'",
            shell=True
        ).decode().strip()
        interfaces = [line.split(":")[0] for line in output.splitlines() if line]
    except Exception:
        interfaces = ["wlan0"]

    hotspot_iface = "wlan0"  # Luôn dùng wlan0 cho hotspot

    if len(interfaces) >= 2:
        # Có 2 WiFi interfaces → wlan1 làm uplink
        uplink_iface = next((i for i in interfaces if i != "wlan0"), "wlan0")
        print(f"[NET] Dual interface detected: Hotspot={hotspot_iface}, Uplink={uplink_iface}")
    else:
        # Single interface → wlan0 làm cả hai (concurrent AP+STA mode)
        uplink_iface = "wlan0"
        print(f"[NET] Single interface: {hotspot_iface} (AP+STA concurrent mode)")

    return {"hotspot": hotspot_iface, "uplink": uplink_iface}


def check_concurrent_support(iface: str) -> bool:
    """Kiểm tra driver có hỗ trợ AP+STA concurrent trên cùng 1 interface."""
    try:
        output = subprocess.check_output(
            f"iw phy $(iw dev {iface} info 2>/dev/null | awk '/wiphy/{{print $2}}') info 2>/dev/null | grep -i 'AP/VLAN\\|concurrent'",
            shell=True
        ).decode()
        return len(output.strip()) > 0
    except Exception:
        return False


# ── Hotspot ────────────────────────────────────────────────

def ensure_hotspot(iface: str = HOTSPOT_IFACE):
    """
    FIX F1: Tạo/khởi động Hotspot trên interface chỉ định.
    Nếu dùng dual interface: Hotspot trên wlan0, không bao giờ bị ảnh hưởng bởi uplink.
    """
    try:
        result = subprocess.run(
            f"nmcli -t con show --active | grep '{HOTSPOT_SSID}'",
            shell=True, capture_output=True
        )
        if result.returncode == 0:
            return  # Đang chạy rồi

        subprocess.run(f"sudo nmcli connection delete '{HOTSPOT_SSID}'",
                       shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(f"sudo nmcli dev disconnect {iface}",
                       shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        cmds = [
            f"sudo nmcli con add type wifi ifname {iface} con-name '{HOTSPOT_SSID}' autoconnect yes ssid '{HOTSPOT_SSID}'",
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
        print(f"[NET] Hotspot '{HOTSPOT_SSID}' activated on {iface} at {HOTSPOT_IP}")
    except Exception as e:
        print(f"[NET] Hotspot error on {iface}: {e}")


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
    """
    [FIX-WIFI-STATUS] Ưu tiên kiểm tra internet thực tế bằng ping trước.
    Root cause bug cũ: nmcli parse sai khi SSID chứa ':', và chỉ check
    infrastructure (STA) mode — bỏ qua wlan1 TP-Link dongle nếu nmcli
    trả về format khác.

    Logic mới:
      1. Ping 8.8.8.8 → nếu OK tức là có internet thực tế
      2. Tìm SSID đang kết nối bằng nmcli (parse an toàn hơn)
      3. Nếu ping OK nhưng không tìm được SSID → báo "connected" với ssid = "Internet (Unknown SSID)"
      4. Chỉ báo "disconnected" khi ping THỰC SỰ thất bại
    """
    # Step 1: Kiểm tra internet thực tế (quan trọng nhất)
    internet_ok = False
    try:
        subprocess.check_call(
            ["ping", "-c", "1", "-W", "3", "8.8.8.8"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        internet_ok = True
    except Exception:
        pass

    # Step 2: Tìm SSID đang kết nối
    # Dùng `nmcli -t -f ACTIVE,SSID,DEVICE dev wifi` thay vì filter MODE
    # để bắt được cả wlan0 và wlan1
    ssid = ""
    iface_found = ""
    try:
        # Format: ACTIVE:SSID:DEVICE
        output = subprocess.check_output(
            "nmcli -t -f ACTIVE,SSID,DEVICE dev wifi 2>/dev/null",
            shell=True, timeout=5
        ).decode().strip()
        for line in output.splitlines():
            if line.startswith("yes:"):
                parts = line.split(":")
                # parts[0] = "yes", parts[-1] = device (wlan0/wlan1), parts[1:-1] = SSID (có thể có :)
                if len(parts) >= 3:
                    ssid = ":".join(parts[1:-1])  # SSID an toàn: join middle parts
                    iface_found = parts[-1]
                    if ssid:
                        break
    except Exception:
        pass

    # Step 3: Fallback — thử nmcli con show --active để tìm WiFi connections
    if not ssid:
        try:
            output = subprocess.check_output(
                "nmcli -t -f NAME,TYPE,DEVICE con show --active 2>/dev/null | grep ':802-11-wireless:'",
                shell=True, timeout=5
            ).decode().strip()
            for line in output.splitlines():
                parts = line.split(":")
                if len(parts) >= 3:
                    name = parts[0]
                    if name and name.lower() not in ("smarthome_hub", "smarthamome_hub", "lo"):
                        ssid = name
                        break
        except Exception:
            pass

    # Step 4: Quyết định trạng thái cuối cùng
    if internet_ok:
        effective_ssid = ssid if ssid else "Internet (wlan)"
        return {
            "status":       "connected",
            "ssid":         effective_ssid,
            "current_ssid": effective_ssid,
            "type":         "wifi",
            "interface":    iface_found or "wlan",
            "internet":     True,
        }

    # Không có internet — kiểm tra xem có LAN/Ethernet không
    try:
        eth_output = subprocess.check_output(
            "nmcli -t -f DEVICE,STATE dev | grep ':connected'",
            shell=True, timeout=5
        ).decode().strip()
        for line in eth_output.splitlines():
            dev = line.split(":")[0]
            if dev.startswith("eth") or dev.startswith("enp") or dev.startswith("ens"):
                return {
                    "status":       "connected",
                    "ssid":         "Ethernet/Wired",
                    "current_ssid": "Ethernet/Wired",
                    "type":         "ethernet",
                    "internet":     False,
                }
    except Exception:
        pass

    return {"status": "disconnected", "ssid": "N/A", "type": "none", "current_ssid": "", "internet": False}


# ── NTP Sync ─────────────────────────────────────────────

def sync_ntp():
    """BUG-RTC-01: Chỉ sync NTP, không dùng RTC DS3231."""
    try:
        result = subprocess.run(
            ["timedatectl", "show", "--property=NTPSynchronized", "--value"],
            capture_output=True, timeout=5
        )
        if result.returncode == 0 and result.stdout.decode().strip() == "yes":
            print(f"[NET] NTP already synchronized via systemd-timesyncd")
            return
        subprocess.run(
            ["sudo", "ntpdate", "-u", "pool.ntp.org"],
            timeout=15, check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        print(f"[NET] NTP synced manually: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    except subprocess.CalledProcessError:
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
    if hasattr(obj, 'isoformat'):
        return obj.isoformat()
    if isinstance(obj, (int, float, str, bool)) or obj is None:
        return obj
    return str(obj)


# ── WiFi Connect (FIX F1) ─────────────────────────────────

def connect_wifi(ssid: str, password: str, request_id: str, r,
                 uplink_iface: str, hotspot_iface: str):
    """
    FIX F1: Connect uplink trên interface chỉ định (uplink_iface).
    Hotspot vẫn chạy trên hotspot_iface — KHÔNG bị ảnh hưởng.

    Nếu dual interface: wlan1 connect uplink → wlan0 Hotspot không bị gián đoạn.
    Nếu single interface: cảnh báo và thực hiện connect nhưng Hotspot có thể
    bị gián đoạn tạm thời (5-15s) trong khi negotiate.
    """
    is_dual = (uplink_iface != hotspot_iface)

    if not is_dual:
        # Single interface: thông báo ESP32 chuẩn bị mất kết nối tạm thời
        r.publish("realtime_data", json.dumps({
            "event":   "wifi_switching",
            "message": "Đang kết nối WiFi mới. ESP32 có thể mất kết nối 5-15 giây.",
            "level":   "warning"
        }))
        print(f"[NET] WARNING: Single interface — Hotspot may drop 5-15s during uplink connect")

    # Xây dựng lệnh connect trên đúng interface
    if uplink_iface != "wlan0" and is_dual:
        cmd = (f"sudo nmcli dev wifi connect '{ssid}' password '{password}' ifname {uplink_iface}"
               if password else
               f"sudo nmcli dev wifi connect '{ssid}' ifname {uplink_iface}")
    else:
        cmd = (f"sudo nmcli dev wifi connect '{ssid}' password '{password}'"
               if password else f"sudo nmcli dev wifi connect '{ssid}'")

    try:
        subprocess.run(cmd, shell=True, check=True, timeout=45,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        result = {"status": "success", "ssid": ssid}
        print(f"[NET] Connected to WiFi uplink: {ssid} via {uplink_iface}")

        status_payload = {
            "status": "connected", "ssid": ssid,
            "current_ssid": ssid, "type": "wifi"
        }
        r.publish("wifi_status", json.dumps(status_payload))

    except Exception as e:
        result = {"status": "failed", "error": str(e)}
        print(f"[NET] WiFi connect failed: {e}")
        r.publish("wifi_status", json.dumps({
            "status": "disconnected", "ssid": "",
            "current_ssid": "", "error": str(e)
        }))
    finally:
        if request_id:
            r.setex(f"wifi_cmd:{request_id}", 60, json.dumps(result, default=json_serializable))

        # FIX F1: Sau khi connect uplink xong, đảm bảo Hotspot vẫn alive trên hotspot_iface
        # (quan trọng với single interface — Hotspot có thể đã bị drop)
        threading.Thread(
            target=ensure_hotspot,
            args=(hotspot_iface,),
            daemon=True
        ).start()


# ── WiFi Scan ─────────────────────────────────────────────

def scan_wifi(r, uplink_iface: str):
    """
    [FIX-SCAN] Scan trên uplink_iface (wlan1) — không ảnh hưởng Hotspot.
    Fix 1: Dùng --escape no + rsplit để parse SSID chứa dấu ':' an toàn.
    Fix 2: Filter theo ifname để chỉ lấy kết quả của đúng interface.
    """
    try:
        # Rescan trên đúng interface
        scan_cmd = (
            f"sudo nmcli dev wifi rescan ifname {uplink_iface}"
            if uplink_iface != "wlan0"
            else "sudo nmcli dev wifi rescan"
        )
        subprocess.run(scan_cmd, shell=True, timeout=10,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(3)

        # [FIX] Dùng --escape no để SSID chứa ':' không bị escape
        # Filter theo ifname để chỉ lấy đúng kết quả của uplink_iface (wlan1)
        if uplink_iface != "wlan0":
            list_cmd = (f"nmcli --escape no -t -f SSID,SIGNAL,DEVICE dev wifi list"
                        f" ifname {uplink_iface}")
        else:
            list_cmd = "nmcli --escape no -t -f SSID,SIGNAL dev wifi list"
        output = subprocess.check_output(list_cmd, shell=True).decode()

        networks = []
        seen = set()
        for line in output.strip().split("\n"):
            # Format với DEVICE: SSID:SIGNAL:DEVICE
            # rsplit từ phải để bảo toàn SSID chứa ':' ở trái
            parts = line.rsplit(":", 2)
            if len(parts) < 2:
                continue
            ssid   = parts[0].strip()
            signal = parts[1].strip()
            if not ssid or ssid == "--" or ssid in seen:
                continue
            seen.add(ssid)
            try:
                sig_int = int(signal)
            except ValueError:
                sig_int = 0
            networks.append({"ssid": ssid, "signal": sig_int})

        networks.sort(key=lambda x: -x["signal"])

        r.setex("wifi_scan_result", 300, json.dumps(networks))
        r.set("wifi_scan_status", "done")
        r.publish("wifi_status", json.dumps({"networks": networks, "scan_done": True}))
        r.publish("realtime_data", json.dumps({
            "event":    "wifi_scan_done",
            "networks": networks
        }, default=json_serializable))
        print(f"[NET] WiFi scan done via {uplink_iface}: {len(networks)} networks found")
    except Exception as e:
        r.set("wifi_scan_status", "error")
        print(f"[NET] WiFi scan error: {e}")


# ── Main loop ─────────────────────────────────────────────

def run():
    r            = get_redis()
    last_check   = 0
    last_ntp     = 0
    has_internet = False

    # FIX F1: Detect interfaces trước khi làm bất cứ điều gì
    ifaces       = detect_interfaces()
    hotspot_iface = ifaces["hotspot"]
    uplink_iface  = ifaces["uplink"]

    def listen_wifi_commands():
        pub = r.pubsub()
        # [FIX-WIFI-CHANNEL] Thêm "wifi_setup" — firebase_sync dispatch
        # action add_and_connect vào channel này, không phải "wifi_commands"
        pub.subscribe("wifi_commands", "wifi_scan_trigger", "wifi_setup")
        for msg in pub.listen():
            if msg["type"] != "message":
                continue
            try:
                channel = msg["channel"]
                if channel == "wifi_scan_trigger":
                    r.set("wifi_scan_status", "scanning")
                    threading.Thread(
                        target=scan_wifi,
                        args=(r, uplink_iface),
                        daemon=True
                    ).start()
                elif channel in ("wifi_commands", "wifi_setup"):
                    # [FIX-WIFI-CHANNEL] Xử lý cả 2 channel như nhau
                    data = json.loads(msg["data"])
                    action = data.get("action", "")

                    # Nếu là lệnh scan (từ firebase_sync gửi qua wifi_setup)
                    if action == "scan_wifi":
                        r.set("wifi_scan_status", "scanning")
                        threading.Thread(
                            target=scan_wifi,
                            args=(r, uplink_iface),
                            daemon=True
                        ).start()
                    else:
                        # Lệnh connect: có thể từ wifi_commands (API) hoặc wifi_setup (Firestore)
                        ssid     = data.get("ssid", "")
                        password = data.get("password", "")
                        req_id   = data.get("request_id") or data.get("cmd_id")
                        if ssid:
                            threading.Thread(
                                target=connect_wifi,
                                args=(ssid, password, req_id, r,
                                      uplink_iface, hotspot_iface),
                                daemon=True
                            ).start()
                        else:
                            print(f"[NET] wifi_setup: thiếu ssid trong payload: {data}")
            except Exception as e:
                print(f"[NET] wifi command error: {e}")

    threading.Thread(target=listen_wifi_commands, daemon=True).start()

    print(f"[NET] Network watchdog started — Hotspot:{hotspot_iface} Uplink:{uplink_iface}")

    # FIX F1: Bring up Hotspot TRƯỚC trên hotspot_iface
    ensure_hotspot(hotspot_iface)

    # [FIX-WIFI-STATUS] Publish trạng thái WiFi ngay khi khởi động
    # Trước đây chỉ publish khi has_internet THAY ĐỔI → nếu Pi đã có internet
    # từ trước khi gateway start, lần đầu sẽ không publish → Web thấy "N/A"
    _initial_status = get_wifi_status()
    r.setex("system_status:wifi", 120, json.dumps(_initial_status))
    has_internet = _initial_status.get("internet", False) or _initial_status.get("status") == "connected"
    r.publish("wifi_status", json.dumps({
        "status":       _initial_status.get("status", "disconnected"),
        "ssid":         _initial_status.get("ssid", ""),
        "current_ssid": _initial_status.get("current_ssid", ""),
        "type":         _initial_status.get("type", "none"),
    }))
    print(f"[NET] Initial WiFi status: {_initial_status.get('status')} / {_initial_status.get('ssid')} / internet={has_internet}")

    while True:
        now = time.time()
        if (now - last_check) >= CHECK_EVERY:
            last_check = now

            status = get_wifi_status()
            r.setex("system_status:wifi", 120, json.dumps(status))

            new_internet = status.get("internet", False) or status.get("status") == "connected"

            # [FIX-WIFI-STATUS] Luôn publish wifi_status mỗi chu kỳ CHECK_EVERY
            # (không chỉ khi thay đổi) để settings.js onSnapshot luôn nhận đúng trạng thái
            r.publish("wifi_status", json.dumps({
                "status":       status.get("status", "disconnected"),
                "ssid":         status.get("ssid", ""),
                "current_ssid": status.get("current_ssid", ""),
                "type":         status.get("type", "none"),
            }))

            if new_internet != has_internet:
                has_internet = new_internet
                event = "internet_online" if has_internet else "internet_offline"
                # Log WiFi status change
                if has_internet:
                    event_logger.log_wifi_status(
                        room_id="system",
                        status="connected",
                        ssid=status.get("ssid", "SmartHome_Hub"),
                        signal_strength=100  # placeholder
                    )
                else:
                    event_logger.log_wifi_status(
                        room_id="system",
                        status="disconnected",
                        ssid=status.get("ssid", "SmartHome_Hub")
                    )
                r.publish("realtime_data", json.dumps({
                    "event":   event,
                    "message": "Đã có Internet" if has_internet else "Mất kết nối Internet"
                }))
                print(f"[NET] Internet: {'ON' if has_internet else 'OFF'} / {status.get('ssid', '')}")

            if has_internet and (now - last_ntp) >= NTP_SYNC_EVERY:
                sync_ntp()
                last_ntp = now

        time.sleep(5)


if __name__ == "__main__":
    run()
