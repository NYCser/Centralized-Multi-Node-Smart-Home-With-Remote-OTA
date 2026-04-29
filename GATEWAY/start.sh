#!/bin/bash
# ════════════════════════════════════════════════════════════════
# SmartHome Gateway — Startup Script (Entry Point)
# Fix BUG Missing Component 1: Không có script khởi động chính
#
# Sử dụng: bash start.sh
# Hoặc cài làm systemd service (xem hướng dẫn phía dưới)
# ════════════════════════════════════════════════════════════════

#!/bin/bash
set -e

# Tự động lấy đường dẫn tuyệt đối của thư mục GATEWAY
GATEWAY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$GATEWAY_DIR/.env"
LOG_DIR="$GATEWAY_DIR/logs"
PID_FILE="/tmp/smarthome_gateway.pid"

# ── 1. Load biến môi trường ──────────────────────────────────────
if [ -f "$ENV_FILE" ]; then
    echo "[START] Loading environment from $ENV_FILE"
    set -a
    source "$ENV_FILE"
    set +a
else
    echo "[START] WARNING: .env not found. Vui lòng kiểm tra lại!"
fi

# ── 2. Tạo thư mục cần thiết ─────────────────────────────────────
mkdir -p "$LOG_DIR"
mkdir -p "$GATEWAY_DIR/scripts"

# ── 3. Kiểm tra Hạ tầng (Redis & MQTT) ───────────────────────────
if ! redis-cli ping > /dev/null 2>&1; then
    echo "[START] Khởi động Redis..."
    sudo systemctl start redis-server || true
fi

if ! mosquitto_sub -h localhost -t test -W 1 > /dev/null 2>&1; then
    echo "[START] Khởi động Mosquitto..."
    sudo systemctl start mosquitto || true
fi

# ── 4. MOUNT THẺ NHỚ SD2 (Quan trọng) ───────────────────────────
# Sử dụng đường dẫn động dựa trên thư mục hiện tại
if [ -f "$GATEWAY_DIR/scripts/mount_sd2.sh" ]; then
    echo "[START] Đang kiểm tra và mount thẻ nhớ SD2..."
    sudo bash "$GATEWAY_DIR/scripts/mount_sd2.sh"
else
    echo "[START] ERROR: Không tìm thấy $GATEWAY_DIR/scripts/mount_sd2.sh"
fi

# ── 5. Khởi động Gateway chính ──────────────────────────────────
echo "[START] ════════════════════════════════════════"
echo "[START]  SmartHome Gateway LIVE"
echo "[START] ════════════════════════════════════════"

cd "$GATEWAY_DIR"
# Chạy Python và ghi log
python3 gateway_main.py 2>&1 | tee -a "$LOG_DIR/gateway.log"
# ════════════════════════════════════════════════════════════════
# CÁCH CÀI SYSTEMD SERVICE (chạy 1 lần khi setup Pi):
#
# sudo tee /etc/systemd/system/smarthome.service > /dev/null << 'EOF'
# [Unit]
# Description=SmartHome Gateway
# After=network.target redis.service mosquitto.service
# Wants=redis.service mosquitto.service
#
# [Service]
# Type=simple
# User=pi
# WorkingDirectory=/home/pi/smarthome_prj/GATEWAY
# EnvironmentFile=/home/pi/smarthome_prj/GATEWAY/.env
# ExecStart=/usr/bin/python3 /home/pi/smarthome_prj/GATEWAY/gateway_main.py
# Restart=always
# RestartSec=5
# StandardOutput=journal
# StandardError=journal
#
# [Install]
# WantedBy=multi-user.target
# EOF
#
# sudo systemctl daemon-reload
# sudo systemctl enable smarthome
# sudo systemctl start smarthome
# sudo journalctl -u smarthome -f   # xem log
# ════════════════════════════════════════════════════════════════