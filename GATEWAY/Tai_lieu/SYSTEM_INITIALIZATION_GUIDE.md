# SmartHome System - Complete Initialization Guide

## 📋 Tổng Quan Hệ Thống

Hệ thống SmartHome bao gồm 4 thành phần chính:

```
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│   ESP32 Nodes   │    │   Raspberry Pi  │    │   Cloudflare    │    │   Web Frontend  │
│   (Sensors)     │◄──►│   Gateway       │◄──►│   Tunnel        │◄──►│   (Dashboard)   │
│                 │    │   (Backend)     │    │                 │    │                 │
└─────────────────┘    └─────────────────┘    └─────────────────┘    └─────────────────┘
       MQTT               Flask API + Redis        HTTPS Tunnel         React/Vanilla JS
```

### 🏗️ Kiến Trúc Chi Tiết

```
ESP32 Nodes (BEDROOM, KITCHEN, LIVINGROOM)
├── Hardware: DHT11, CCS811, Relays, Buttons
├── Communication: MQTT (home/{room}/sensors, home/{room}/command)
└── OTA: HTTP Update via MQTT commands

Raspberry Pi Gateway
├── Backend: Flask + SocketIO (Port 5000)
├── Database: SQLite + Redis cache
├── Workers: Safety Watchdog, Automation Engine, Data Syncer
├── Services: Mosquitto MQTT, Redis, Cloudflare Tunnel
└── API: REST endpoints + WebSocket realtime

Cloudflare Tunnel
├── Hostname: nhathongminh.crfnetwork.cyou
├── Service: HTTP → localhost:5000
└── Security: HTTPS with Cloudflare protection

Web Frontend
├── Authentication: JWT tokens (localStorage)
├── Realtime: API polling (5s intervals)
├── Dashboard: Room-specific interfaces
└── Control: Device control via REST API
```

---

## 🚀 Khởi Tạo Hệ Thống (Step-by-Step)

### **Bước 1: Chuẩn Bị Hardware**

#### 1.1 Raspberry Pi Setup
```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install dependencies
sudo apt install -y python3 python3-venv python3-pip \
  build-essential libssl-dev libffi-dev python3-dev \
  mosquitto mosquitto-clients redis-server redis-tools \
  network-manager git curl wget jq

# Create data directory
sudo mkdir -p /data
sudo chown $USER:$USER /data
```

#### 1.2 ESP32 Development Environment
```bash
# Install PlatformIO (recommended)
curl -fsSL https://raw.githubusercontent.com/platformio/platformio-core-installer/master/get-platformio.py | python3 -

# Or Arduino IDE
# Download from: https://www.arduino.cc/en/software
# Install ESP32 board support
```

---

### **Bước 2: Setup Backend (Raspberry Pi)**

#### 2.1 Clone Repository
```bash
cd /home/pi
git clone <repository-url> smarthome_prj
cd smarthome_prj/GATEWAY
```

#### 2.2 Virtual Environment & Dependencies
```bash
# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install Python packages
pip install --upgrade pip
pip install -r requirements.txt

# Additional packages (if needed)
pip install bcrypt paho-mqtt flask-cors
```

#### 2.3 Database Setup
```bash
# Initialize database
python -c "
import sqlite3
conn = sqlite3.connect('/data/smarthome.db')
with open('storage/db_schema.sql', 'r') as f:
    conn.executescript(f.read())
conn.close()
print('Database initialized')
"
```

#### 2.4 System Services
```bash
# Start MQTT Broker
sudo systemctl enable mosquitto
sudo systemctl start mosquitto

# Start Redis
sudo systemctl enable redis-server
sudo systemctl start redis-server

# Test services
mosquitto_sub -h localhost -t '$SYS/broker/info' -W 1
redis-cli ping
```

#### 2.5 Cloudflare Tunnel Setup
```bash
# Install cloudflared
cd /home/pi
wget https://github.com/cloudflare/cloudflared/releases/download/2024.4.1/cloudflared-linux-arm.tgz
tar xzf cloudflared-linux-arm.tgz
sudo mv cloudflared /usr/local/bin/

# Authenticate
cloudflared tunnel login

# Create tunnel
cloudflared tunnel create nhathongminh

# Configure tunnel (~/.cloudflared/config.yml)
cat > ~/.cloudflared/config.yml << 'EOF'
tunnel: <tunnel-id>
credentials-file: ~/.cloudflared/<tunnel-id>.json

ingress:
  - hostname: nhathongminh.crfnetwork.cyou
    service: http://localhost:5000
  - hostname: pi.crfnetwork.cyou
    service: ssh://localhost:22
  - service: http_status:404
EOF

# Setup systemd service
sudo cp cloudflared.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable cloudflared.service
sudo systemctl start cloudflared.service
```

---

### **Bước 3: Setup ESP32 Nodes**

#### 3.1 PlatformIO Setup
```bash
# Install PlatformIO Core
curl -fsSL https://raw.githubusercontent.com/platformio/platformio-core-installer/master/get-platformio.py | python3 -

# Initialize projects
cd /home/pi/smarthome_prj/BEDROOM
pio init --board esp32dev

cd /home/pi/smarthome_prj/KITCHEN
pio init --board esp32dev

cd /home/pi/smarthome_prj/LIVINGROOM
pio init --board esp32dev
```

#### 3.2 Configure WiFi & MQTT
```cpp
// Update Config.hpp in each room
#define WIFI_SSID "YourWiFi"
#define WIFI_PASSWORD "YourPassword"
#define MQTT_BROKER "192.168.1.xxx"  // Raspberry Pi IP
#define MQTT_PORT 1883
#define ROOM_ID "bedroom"  // Change for each room
```

#### 3.3 Flash ESP32
```bash
# BEDROOM
cd /home/pi/smarthome_prj/BEDROOM
pio run --target upload

# KITCHEN
cd /home/pi/smarthome_prj/KITCHEN
pio run --target upload

# LIVINGROOM
cd /home/pi/smarthome_prj/LIVINGROOM
pio run --target upload
```

---

### **Bước 4: Setup Frontend**

#### 4.1 Web Server Setup
```bash
# Install Node.js (optional, for development)
curl -fsSL https://deb.nodesrc.com/setup_18.x | sudo -E bash -
sudo apt-get install -y nodejs

# Or serve static files with Python
cd /home/pi/smarthome_prj/NhaThongMinh-Web
python3 -m http.server 8080
```

#### 4.2 Configure API Base URL
```javascript
// Update config.js
export const API_BASE = "https://nhathongminh.crfnetwork.cyou";
```

---

### **Bước 5: Cloudflare DNS Setup**

1. **Login Cloudflare Dashboard**
2. **Go to DNS → Add Record**
3. **Add CNAME Record:**
   ```
   Type: CNAME
   Name: nhathongminh
   Target: <tunnel-id>.cfargotunnel.com
   TTL: Auto
   Proxy Status: Proxied (orange cloud)
   ```

---

### **Bước 6: Khởi Động Hệ Thống**

#### 6.1 Start Backend
```bash
cd /home/pi/smarthome_prj/GATEWAY
source venv/bin/activate
python gateway_main.py
```

#### 6.2 Start Frontend
```bash
cd /home/pi/smarthome_prj/NhaThongMinh-Web
python3 -m http.server 8080 &
```

#### 6.3 Verify System
```bash
# Test API
curl http://localhost:5000/health
curl https://nhathongminh.crfnetwork.cyou/health

# Test MQTT
mosquitto_sub -h localhost -t 'home/#' -v

# Test Web Interface
curl http://localhost:8080
```

---

## 🔧 Cấu Hình Chi Tiết

### **Environment Variables**
```bash
# Backend
export DB_PATH="/data/smarthome.db"
export MQTT_BROKER="localhost"
export REDIS_HOST="localhost"
export API_PORT=5000

# Frontend
export API_BASE="https://nhathongminh.crfnetwork.cyou"
```

### **ESP32 Pin Configuration**

#### BEDROOM Node
```cpp
#define PIN_RELAY_FAN  12
#define PIN_RELAY_LIGHT 13
#define PIN_BUTTON_FAN  14
#define PIN_BUTTON_LIGHT 15
#define PIN_DHT11       4
#define PIN_CCS811_SDA  21
#define PIN_CCS811_SCL  22
```

#### KITCHEN Node
```cpp
#define PIN_RELAY_FAN     12
#define PIN_RELAY_VALVE   13
#define PIN_RELAY_ALARM   14
#define PIN_BUTTON_FAN    15
#define PIN_BUTTON_VALVE  16
#define PIN_BUTTON_ALARM  17
#define PIN_MQ2           4
```

#### LIVINGROOM Node
```cpp
#define PIN_RELAY_LIGHT1 12
#define PIN_RELAY_LIGHT2 13
#define PIN_RELAY_FAN    14
#define PIN_BUTTON_LIGHT1 15
#define PIN_BUTTON_LIGHT2 16
#define PIN_BUTTON_FAN   17
#define PIN_DHT11        4
```

### **Database Schema**
```sql
-- Users table
CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    email TEXT UNIQUE,
    password TEXT,
    display_name TEXT,
    role TEXT DEFAULT 'user',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Sessions table
CREATE TABLE sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER,
    expires_at TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

-- Device status table
CREATE TABLE device_status (
    room TEXT,
    device_id TEXT,
    is_on BOOLEAN,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (room, device_id)
);

-- Sensor data table (daily partitioned)
CREATE TABLE sensor_data (
    timestamp TEXT,
    room TEXT,
    sensor_type TEXT,
    value REAL,
    unit TEXT
);
```

---

## 🐛 Troubleshooting

### **Backend Issues**

#### Gateway Won't Start
```bash
# Check Python imports
source venv/bin/activate
python -c "import redis, paho.mqtt, flask, flask_cors; print('OK')"

# Check services
sudo systemctl status mosquitto redis-server

# Check ports
netstat -an | grep -E '1883|6379|5000'

# Manual run
python gateway_main.py 2>&1 | tee debug.log
```

#### MQTT Connection Failed
```bash
# Restart MQTT
sudo systemctl restart mosquitto

# Test connection
mosquitto_pub -h localhost -t 'test' -m 'hello'
mosquitto_sub -h localhost -t 'test' -W 1
```

#### Redis Connection Failed
```bash
# Restart Redis
sudo systemctl restart redis-server

# Test connection
redis-cli ping
redis-cli INFO | head -10
```

### **ESP32 Issues**

#### Upload Failed
```bash
# Check USB connection
lsusb | grep -i esp32

# Check serial port
pio device list

# Manual upload
pio run --target upload --upload-port /dev/ttyUSB0
```

#### MQTT Connection Failed
```bash
# Check ESP32 IP
# Monitor serial output
pio device monitor

# Test MQTT from Pi
mosquitto_pub -h localhost -t 'home/bedroom/command' -m '{"action":"ping"}'
```

### **Frontend Issues**

#### API Connection Failed
```bash
# Test local API
curl http://localhost:5000/health

# Test tunnel
curl https://nhathongminh.crfnetwork.cyou/health

# Check CORS
curl -H "Origin: http://localhost:8080" \
     -H "Access-Control-Request-Method: GET" \
     -X OPTIONS http://localhost:5000/health
```

#### Authentication Failed
```bash
# Register test user
curl -X POST http://localhost:5000/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@smarthome.local","password":"admin123"}'

# Login
curl -X POST http://localhost:5000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@smarthome.local","password":"admin123"}'
```

### **Tunnel Issues**

#### DNS Not Resolving
```bash
# Check DNS
nslookup nhathongminh.crfnetwork.cyou

# Check tunnel status
cloudflared tunnel info nhathongminh

# Check service
sudo systemctl status cloudflared.service
```

---

## 📊 Monitoring & Logs

### **System Monitoring**
```bash
# MQTT messages
mosquitto_sub -h localhost -t 'home/#' -v

# Redis pubsub
redis-cli SUBSCRIBE 'mqtt_inbound' 'mqtt_outbound'

# System logs
sudo journalctl -u gateway.service -f
sudo journalctl -u cloudflared.service -f
```

### **Performance Monitoring**
```bash
# CPU/Memory usage
top -p $(pgrep -f gateway_main.py)

# Network connections
netstat -an | grep -E '1883|6379|5000'

# Disk usage
df -h /data /mnt/sd2
```

---

## 🔒 Security Considerations

### **Network Security**
- Use strong WiFi passwords
- Enable WPA3 if possible
- Consider VPN for remote access
- Use HTTPS via Cloudflare

### **API Security**
- JWT tokens with expiration
- Admin-only endpoints protected
- Input validation on all endpoints
- CORS properly configured

### **Device Security**
- Unique MQTT client IDs
- Secure OTA updates
- Physical access control
- Firmware integrity checks

---

## 📚 Reference

### **API Endpoints**
- `GET /health` - Health check
- `POST /auth/register` - User registration
- `POST /auth/login` - User login
- `GET /latest?room={room}` - Latest sensor data
- `POST /control` - Device control
- `GET /dashboard?room={room}` - Dashboard data

### **MQTT Topics**
- `home/{room}/sensors` - Sensor data publishing
- `home/{room}/command` - Device control commands
- `home/{room}/status` - Device status updates

### **File Structure**
```
/home/pi/smarthome_prj/
├── GATEWAY/                    # Backend
│   ├── gateway_main.py
│   ├── app/api/routes/
│   └── storage/db_schema.sql
├── BEDROOM/                    # ESP32
├── KITCHEN/                    # ESP32
├── LIVINGROOM/                 # ESP32
└── NhaThongMinh-Web/          # Frontend
    ├── js/
    ├── css/
    └── *.html
```

---

## 🎯 Quick Start Commands

```bash
# One-time setup
cd /home/pi/smarthome_prj/GATEWAY
bash setup.sh

# Start system
cd /home/pi/smarthome_prj/GATEWAY
bash run.sh

# Start frontend (separate terminal)
cd /home/pi/smarthome_prj/NhaThongMinh-Web
python3 -m http.server 8080

# Access system
# Backend API: http://localhost:5000
# Frontend: http://localhost:8080
# Public API: https://nhathongminh.crfnetwork.cyou
```

---

**System Ready! 🚀**

For support, check logs and refer to troubleshooting section above.</content>
<parameter name="filePath">/home/pi/smarthome_prj/SYSTEM_INITIALIZATION_GUIDE.md