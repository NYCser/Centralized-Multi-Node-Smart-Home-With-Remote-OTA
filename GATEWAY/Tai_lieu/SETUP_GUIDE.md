# SmartHome Gateway - Setup Native Python (Raspberry Pi)

## Tổng quan

Gateway này được thiết kế để chạy **Native trên Raspberry Pi** mà không cần Docker. Điều này cho phép truy cập trực tiếp vào phần cứng (WiFi, SD card, RTC).

**Kiến trúc:**
```
┌─────────────────────────────────────────┐
│  gateway_main.py (Main Entry Point)     │
├─────────────────────────────────────────┤
│  Bridge Layer: message_bus.py           │  (MQTT ↔ Redis)
│  ├── MessageBus                         │
│  └── Pubsub Channels                    │
├─────────────────────────────────────────┤
│  Workers (Threading):                   │
│  ├── safety_watchdog.py          ⚠️ Priority │
│  ├── automation_engine.py               │
│  ├── data_syncer.py      (→ SD Card)    │
│  └── network_watchdog.py (WiFi + RTC)   │
├─────────────────────────────────────────┤
│  Web API:                               │
│  └── Flask + SocketIO (Port 5000)       │
├─────────────────────────────────────────┤
│  System Services (External):            │
│  ├── Mosquitto MQTT Broker (1883)       │
│  └── Redis (6379)                       │
└─────────────────────────────────────────┘
```

---

## Step-by-step Setup

### **Step 1: Chạy Script Setup Tự động** (Khuyến nghị)

```bash
cd /home/pi/GATEWAY
chmod +x setup.sh
bash setup.sh
```

Script này sẽ:
- ✅ Cài đặt system dependencies (Python, MQTT, Redis, etc.)
- ✅ Tạo virtual environment
- ✅ Cài đặt Python packages từ `requirements.txt`
- ✅ Khởi động Mosquitto MQTT Broker
- ✅ Khởi động Redis Server
- ✅ Kiểm tra các dịch vụ

---

### **Step 2: Manual Setup (Nếu muốn)**

#### 2.1 Update System

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip \
  build-essential libssl-dev libffi-dev python3-dev \
  mosquitto mosquitto-clients redis-server redis-tools \
  network-manager git curl wget
```

#### 2.2 Create Virtual Environment

```bash
cd /home/pi/GATEWAY
python3 -m venv venv
source venv/bin/activate
```

#### 2.3 Install Python Dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

#### 2.4 Setup System Services

```bash
# Mosquitto MQTT Broker
sudo systemctl enable mosquitto
sudo systemctl start mosquitto

# Redis Server
sudo systemctl enable redis-server
sudo systemctl start redis-server

# Create data directory
sudo mkdir -p /data
sudo chown $USER:$USER /data
```

#### 2.5 Test Services

```bash
# Test MQTT (should show connected)
mosquitto_sub -h localhost -t '$SYS/broker/info' -W 1

# Test Redis (should return "PONG")
redis-cli ping

# Check connections from Python
python -c "
import paho.mqtt.client as mqtt
import redis
print('✓ MQTT OK' if mqtt else 'MQTT Failed')
print('✓ Redis OK' if redis else 'Redis Failed')
"
```

---

## Chạy Gateway

### **Option 1: Chạy với Script Tự động** (Dễ nhất)

```bash
cd /home/pi/GATEWAY
chmod +x run.sh
bash run.sh
```

Script sẽ:
- ✅ Activate venv
- ✅ Kiểm tra và khởi động services
- ✅ Chạy `gateway_main.py`

### **Option 2: Chạy Manual**

```bash
cd /home/pi/GATEWAY
source venv/bin/activate

# Make sure services are running
sudo systemctl start mosquitto
sudo systemctl start redis-server

# Run Gateway
python gateway_main.py
```

---

## Truy cập Web Interface

Khi Gateway chạy bình thường, bạn sẽ thấy:

```
[MAIN] Flask API starting on port 5000
```

Truy cập tại:
```
http://<raspberry-pi-ip>:5000
```

Tìm IP Raspberry Pi:
```bash
hostname -I
# hoặc
ip addr show | grep "inet 192"
```

---

## Kiểm tra Logs

### **MQTT Broker Logs**

```bash
# View Mosquitto logs
sudo journalctl -u mosquitto -f
```

### **Redis Logs**

```bash
# View Redis logs
sudo journalctl -u redis-server -f
```

### **Gateway Logs**

```bash
# If running in foreground, logs appear in terminal
# If running in background, use:
tail -f /tmp/gateway.log  # (if configured)
```

### **Monitor Messages**

```bash
# Subscribe to all MQTT messages
mosquitto_sub -h localhost -t 'home/#' -v

# Monitor Redis pubsub
redis-cli SUBSCRIBE 'mqtt_inbound' 'mqtt_outbound'
```

---

## Chạy Gateway ở Background (Systemd Service)

Tạo systemd service để Gateway tự khởi động:

```bash
sudo cat > /etc/systemd/system/gateway.service << 'EOF'
[Unit]
Description=SmartHome Gateway Service
After=network.target redis-server.service mosquitto.service
Wants=redis-server.service mosquitto.service

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/GATEWAY
ExecStart=/home/pi/GATEWAY/venv/bin/python gateway_main.py
Restart=always
RestartSec=10
StandardOutput=append:/var/log/gateway.log
StandardError=append:/var/log/gateway.log
Environment="PYTHONUNBUFFERED=1"

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable gateway.service
sudo systemctl start gateway.service
```

Check status:
```bash
sudo systemctl status gateway.service
sudo journalctl -u gateway -f
```

---

## Troubleshooting

### **Gateway không start**

```bash
# 1. Check Python packages
source venv/bin/activate
python -c "import redis, paho.mqtt, flask, flask_socketio; print('✓ OK')"

# 2. Check services
sudo systemctl status mosquitto
sudo systemctl status redis-server

# 3. Check ports
netstat -an | grep -E '1883|6379|5000'

# 4. Manual run để thấy error
python gateway_main.py
```

### **MQTT Connection Failed**

```bash
# Restart Mosquitto
sudo systemctl restart mosquitto

# Check config
sudo cat /etc/mosquitto/mosquitto.conf

# Test connection
mosquitto_sub -h localhost -t '#'
```

### **Redis Connection Failed**

```bash
# Restart Redis
sudo systemctl restart redis-server

# Test connection
redis-cli ping
redis-cli INFO
```

### **Permission Denied (WiFi, SD Card)**

Gateway cần quyền `sudo` để quản lý WiFi. Kiểm tra sudo access:

```bash
# Test if sudo works without password
sudo -n systemctl status mosquitto

# If password required, add to sudoers:
sudo visudo
# Add line: pi ALL=(ALL) NOPASSWD: /usr/bin/nmcli, /bin/mount, /bin/umount
```

---

## Project Structure

```
/home/pi/GATEWAY/
├── gateway_main.py          ← Entry point chính
├── requirements.txt         ← Python dependencies
├── setup.sh                ← Tự động setup script
├── run.sh                  ← Tự động run script
│
├── bridge/
│   └── message_bus.py      ← MQTT ↔ Redis bridge (Singleton)
│
├── workers/
│   ├── safety_watchdog.py  ← Giám sát Gas/Fire (ưu tiên cao)
│   ├── automation_engine.py ← Logic IF/THEN + Scheduler
│   ├── data_syncer.py      ← Buffer Redis → SQLite + SD2
│   └── network_watchdog.py ← WiFi + Hotspot + RTC
│
├── app/
│   ├── main.py
│   └── api/routes/all_routes.py  ← REST endpoints + SocketIO
│
└── storage/
    └── db_schema.sql       ← SQLite schema

```

---

## Environment Variables

Bạn có thể override các config bằng environment variables:

```bash
# Set custom data path
export DB_PATH="/mnt/usb/smarthome.db"

# Set custom MQTT broker
export MQTT_BROKER="192.168.1.100"
export REDIS_HOST="192.168.1.100"

# Set custom API port
export API_PORT=8080

python gateway_main.py
```

---

## Notes

- **DB Path**: Mặc định `/data/smarthome.db` → gắn vào `/` filesystem
- **Data Syncer**: Ghi sensor data từ Redis vào SQLite, backup vào SD card (`/mnt/sd2`)
- **Network Watchdog**: Khởi động Hotspot "SmartHome_Hub" + đồng bộ RTC từ NTP
- **Safety Watchdog**: Daemon độc lập, có thể điều khiển WiFi ngay cả khi API down

---

**Ready to run! 🚀**

```bash
cd /home/pi/GATEWAY
bash setup.sh  # First time only
bash run.sh    # Every time to start
```
