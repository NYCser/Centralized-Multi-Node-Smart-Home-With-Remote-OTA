# SmartHome Gateway - Cloudflare Tunnel Public API Setup

## 📊 Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                         EXTERNAL INTERNET                           │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  ┌─────────────────────────┐        ┌──────────────────────────┐  │
│  │   Vercel Frontend       │        │  Cloudflare (DNS + CDN)  │  │
│  │  (Next.js/React app)    │──HTTPS─│  api.yourdomain.com      │  │
│  │  smarthome.vercel.app   │        │  (routes to tunnel)      │  │
│  └─────────────────────────┘        └──────────────────────────┘  │
│                 │                                   │                │
│                 │────────── HTTPS ─────────────────┘                │
│                 │ (fetch from /api/...)            │                │
│                 │                                   │                │
│                 └───────────────────────────────────┼────────────┐   │
│                                                      ▼            │   │
│                                              ┌──────────────┐     │   │
│                                              │  Cloudflare  │     │   │
│                                              │   Tunnel     │     │   │
│                                              │  (ingress)   │     │   │
│                                              └──────────────┘     │   │
│                                                      │            │   │
├──────────────────────────────────────────────────────┼────────────┤   │
│ (Internet ↔ Home Network boundary)                   │            │   │
├──────────────────────────────────────────────────────┼────────────┤   │
│                                                      │            │   │
│  ┌───────────────────────────────────────────────────┘            │   │
│  │                                                                 │   │
│  ▼                                                                 │   │
│ ┌────────────────────────────────────────┐                       │   │
│ │    Raspberry Pi (Home Network)         │                       │   │
│ ├────────────────────────────────────────┤                       │   │
│ │                                        │                       │   │
│ │  gateway_main.py (Flask App)           │                       │   │
│ │  ├─ Port 5000: API Endpoints           │                       │   │
│ │  ├─ Port 5001: SocketIO (real-time)   │                       │   │
│ │  └─ CORS: "*"                         │                       │   │
│ │                                        │                       │   │
│ │  cloudflared daemon                    │                       │   │
│ │  └─ Listens on tunnel                  │                       │   │
│ │  └─ Proxies requests → localhost:5000  │                       │   │
│ │                                        │                       │   │
│ │  Backend Services:                     │                       │   │
│ │  ├─ Mosquitto MQTT (1883)             │                       │   │
│ │  ├─ Redis (6379)                      │                       │   │
│ │  └─ SQLite DB                         │                       │   │
│ └────────────────────────────────────────┘                       │   │
│                                                                    │   │
└────────────────────────────────────────────────────────────────────┘
```

---

## 📋 How Cloudflare Tunnel Works

### The Problem (Why Port Forwarding Fails)
```
Traditional setup (NOT recommended):
┌──────────────┐        ┌──────────────┐        ┌──────────────┐
│ User Browser │──────→ │ Router       │──────→ │ Raspberry Pi │
│              │        │ (Port 8080)  │        │ (Port 5000)  │
└──────────────┘        └──────────────┘        └──────────────┘
Problem: Must expose router port, security risk, DDoS vulnerable
```

### The Solution (Cloudflare Tunnel)
```
Cloudflare Tunnel setup:
┌──────────────┐  1. Request    ┌──────────────┐  2. Route     ┌──────────────┐
│ User Browser │────HTTPS────→  │ Cloudflare   │────Tunnel───→ │ Raspberry Pi  │
│ (anywhere)   │                │ (edge node)  │ (outbound)    │ (cloudflared) │
└──────────────┘                └──────────────┘               └──────────────┘
                                       ↑
                                       │
                    3. cloudflared keeps connection OPEN
                       (Pi initiates connection, not user)
```

### Key Advantages
1. **No Port Forwarding**: Pi initiates outbound connection to Cloudflare
2. **No NAT Traversal**: Works behind any firewall/NAT
3. **DDoS Protection**: Cloudflare absorbs attacks
4. **HTTPS by Default**: Free SSL certificate from Cloudflare
5. **CDN**: Static content cached globally
6. **No IP Exposure**: Your home IP remains hidden

### Connection Flow
```
1. Raspberry Pi runs: cloudflared tunnel run
   ↓
2. cloudflared connects to Cloudflare ingress server (outbound, no listening port)
   ↓
3. cloudflared creates secure tunnel to localhost:5000 (Flask API)
   ↓
4. User requests: https://api.yourdomain.com/health
   ↓
5. Request reaches Cloudflare edge (nearest to user)
   ↓
6. Cloudflare routes through tunnel → cloudflared daemon → localhost:5000
   ↓
7. Flask API responds through same tunnel → Cloudflare → User
```

---

## 🚀 Step-by-Step Deployment Guide

### **Phase 1: Prepare Cloudflare (10 mins)**

#### 1.1 Register Free Cloudflare Account
- Go to https://dash.cloudflare.com/sign-up
- Sign up with email (free tier)
- Verify email

#### 1.2 Add Your Domain to Cloudflare
```bash
# Example: yourdomain.com (must own this domain)
# Go to: https://dash.cloudflare.com → Add a Site
# Choose "Free" plan
# Update your domain registrar's nameservers to:
#   ns1.cloudflare.com
#   ns2.cloudflare.com
```

**If you don't have a domain yet:**
- Buy one: Namecheap, GoDaddy, Google Domains (~$1-3/year)
- Or use free domain: freenom.com, duck.dns, no-ip.com

#### 1.3 Create Cloudflare API Token (for authentication)
```bash
# Go to: https://dash.cloudflare.com/profile/api-tokens
# Click "Create Token"
# Use template "Edit Cloudflare Workers"
# Or custom: Account.Access: Edit, Zone.DNS: Edit
# Copy token to safe place (you'll need it on Raspberry Pi)
```

---

### **Phase 2: Install Cloudflare Tunnel on Raspberry Pi (15 mins)**

#### 2.1 Download cloudflared Binary
```bash
# SSH into your Raspberry Pi
ssh pi@<raspberry-pi-ip>

# Download cloudflared (latest release)
cd /home/pi
wget https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm.tgz
# For ARM64 (Pi 4/5): 
# wget https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64.tgz

# Extract
tar xzf cloudflared-linux-arm.tgz

# Make executable
chmod +x cloudflared

# Move to system path
sudo mv cloudflared /usr/local/bin/

# Test installation
cloudflared version
# Expected output: cloudflared version X.X.X ...
```

#### 2.2 Authenticate cloudflared with Cloudflare
```bash
# This opens a browser to authorize cloudflared
cloudflared tunnel login

# You'll see:
# Please open the following URL and log in with your Cloudflare account:
# https://dash.cloudflare.com/argotunnel?...
#
# Click "Authorize" → it downloads certificate to ~/.cloudflared/cert.pem
```

**Verify authentication:**
```bash
ls ~/.cloudflared/
# Should see: cert.pem
```

---

### **Phase 3: Configure Cloudflare Tunnel (10 mins)**

#### 3.1 Create Tunnel Configuration File

Create `/home/pi/.cloudflared/config.yml`:

```yaml
# Cloudflare Tunnel Configuration
# Path: ~/.cloudflared/config.yml

tunnel: smarthome-gateway
credentials-file: /home/pi/.cloudflared/<UUID>.json

ingress:
  # Main API endpoint
  - hostname: api.yourdomain.com
    service: http://localhost:5000
    originRequest:
      httpHostHeader: localhost

  # WebSocket for real-time (SocketIO)
  - hostname: api.yourdomain.com
    service: ws://localhost:5001
    originRequest:
      httpHostHeader: localhost

  # Fallback (if no route matches, use default)
  - service: http_status:404
    statusCode: 404

# Global settings
loglevel: info
logfile: /home/pi/.cloudflared/cloudflared.log

# Connection settings
grace-period: 30s
heartbeat-interval: 30s
max-failed-heartbeats: 5
```

**Note:** Replace `yourdomain.com` with your actual domain.

#### 3.2 Create the Tunnel

```bash
# Create tunnel named "smarthome-gateway"
cloudflared tunnel create smarthome-gateway

# Output:
# Tunnel credentials written to /home/pi/.cloudflared/<UUID>.json
# Created tunnel smarthome-gateway with id <tunnel-id>
# Note: Copy this tunnel ID to config.yml
```

**Update config.yml with the UUID:**
```bash
# Find UUID from credentials file
cat ~/.cloudflared/*.json | grep "Tunnel ID"
# Or: ls ~/.cloudflared/ | grep -E '^[a-f0-9-]{36}\.json'

# Copy UUID to config.yml tunnel parameter
# tunnel: <UUID-from-above>
```

---

### **Phase 4: Create DNS Records (5 mins)**

#### 4.1 Map Domain to Cloudflare Tunnel

```bash
# Cloudflare Dashboard:
# https://dash.cloudflare.com → Your Domain → DNS

# Add CNAME record:
Name:  api
Type:  CNAME
Target: <tunnel-id>.cfargotunnel.com
TTL:   Auto (or 1 hour)
Proxy:  Proxied (orange cloud icon)

# Example:
# api.yourdomain.com → smarthome-gateway-12345678.cfargotunnel.com
```

**Or use command line:**
```bash
cloudflared tunnel route dns smarthome-gateway api.yourdomain.com
# Output: Created CNAME smarthome-gateway.cfargotunnel.com
```

---

### **Phase 5: Start the Tunnel (5 mins)**

#### 5.1 Run Tunnel Manually (Testing)

```bash
# First, ensure Flask API is running
cd /home/pi/GATEWAY
source venv/bin/activate
python gateway_main.py &

# In another terminal, start tunnel
cloudflared tunnel run smarthome-gateway

# Expected output:
# [2024-04-16 10:30:45] INFO Starting tunnel smarthome-gateway
# [2024-04-16 10:30:47] INFO Tunnel authenticated successfully
# [2024-04-16 10:30:47] INFO tunnel server listening on 127.0.0.1:8200
# [2024-04-16 10:30:47] INFO Connected to api.yourdomain.com
```

#### 5.2 Test from External Network

```bash
# From any computer NOT on your home WiFi:
curl https://api.yourdomain.com/health

# Expected response:
# {"status": "ok", ...}

# Or test in browser:
# https://api.yourdomain.com/health
```

#### 5.3 Run as Systemd Service (Auto-Start)

Create `/etc/systemd/system/cloudflared.service`:

```ini
[Unit]
Description=Cloudflare Tunnel for SmartHome Gateway
After=network.target gateway.service
Wants=gateway.service

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi
ExecStart=/usr/local/bin/cloudflared tunnel run smarthome-gateway
Restart=always
RestartSec=10
StandardOutput=append:/var/log/cloudflared.log
StandardError=append:/var/log/cloudflared.log
Environment="CLOUDFLARED_ORIGIN_CERT=/home/pi/.cloudflared/cert.pem"

[Install]
WantedBy=multi-user.target
```

**Enable service:**
```bash
sudo systemctl daemon-reload
sudo systemctl enable cloudflared.service
sudo systemctl start cloudflared.service

# Check status
sudo systemctl status cloudflared.service
sudo journalctl -u cloudflared -f  # tail logs
```

---

## 🌐 Frontend Integration (Vercel + Next.js/React)

### **Architecture**

```
Vercel Frontend                  Cloudflare Tunnel
(smarthome.vercel.app)          (api.yourdomain.com)
       │                                  │
       └──────── Fetch API ──────────────┘
         (HTTPS, same origin compatible)
```

### **Setup Instructions**

#### 1. Create Frontend Repository

```bash
# Create Next.js project (recommended for Vercel)
npx create-next-app@latest smarthome-dashboard
cd smarthome-dashboard

# Or React
npx create-react-app smarthome-dashboard
cd smarthome-dashboard
```

#### 2. Configure API Base URL

**Create `.env.local`:**
```bash
REACT_APP_API_URL=https://api.yourdomain.com
# or for Next.js:
NEXT_PUBLIC_API_URL=https://api.yourdomain.com
```

#### 3. Create API Service Layer

**`src/services/api.ts` (React/Next.js):**
```typescript
const API_BASE_URL = process.env.REACT_APP_API_URL || 
                     process.env.NEXT_PUBLIC_API_URL || 
                     'https://api.yourdomain.com';

export const apiService = {
  async get(endpoint: string, token?: string) {
    const headers: any = { 'Content-Type': 'application/json' };
    if (token) headers['Authorization'] = `Bearer ${token}`;
    
    const response = await fetch(`${API_BASE_URL}${endpoint}`, { headers });
    if (!response.ok) throw new Error(`API error: ${response.status}`);
    return response.json();
  },

  async post(endpoint: string, body: any, token?: string) {
    const headers: any = { 'Content-Type': 'application/json' };
    if (token) headers['Authorization'] = `Bearer ${token}`;
    
    const response = await fetch(`${API_BASE_URL}${endpoint}`, {
      method: 'POST',
      headers,
      body: JSON.stringify(body),
    });
    if (!response.ok) throw new Error(`API error: ${response.status}`);
    return response.json();
  },
};

// Usage:
// const data = await apiService.get('/health', authToken);
// const result = await apiService.post('/login', { email, password });
```

#### 4. WebSocket Connection (Real-time)

**Real-time updates via Socket.IO:**
```typescript
import io from 'socket.io-client';

const socket = io('https://api.yourdomain.com', {
  auth: { token: authToken },
  secure: true,
  reconnection: true,
});

socket.on('sensor_update', (data) => {
  console.log('Sensor data:', data);
});
```

#### 5. Deploy to Vercel

**Option A: GitHub Integration (Recommended)**
```bash
# Push to GitHub
git init
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/yourusername/smarthome-dashboard.git
git push -u origin main
```

Then:
1. Go to https://vercel.com/import
2. Select GitHub repository
3. Set environment variable: `NEXT_PUBLIC_API_URL=https://api.yourdomain.com`
4. Deploy (Vercel auto-deploys on push)

**Option B: Direct CLI**
```bash
npm i -g vercel
vercel
# Follow prompts to deploy
```

---

## 🔒 Security Configuration

### **CORS Setup (Already in Your Code)**

Your `gateway_main.py` already has good CORS, but verify:

```python
from flask_cors import CORS

CORS(app, resources={r"/*": {"origins": "*"}}, supports_credentials=True)

@app.after_request
def add_cors_headers(response):
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS,PATCH')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    return response
```

**For production, restrict to your frontend:**
```python
CORS(app, 
     resources={r"/*": {"origins": ["https://smarthome.vercel.app"]}},
     supports_credentials=True,
     allow_headers=['Content-Type', 'Authorization'],
     methods=['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS', 'PATCH']
)
```

### **Additional Security Headers**

Add to `gateway_main.py`:

```python
@app.after_request
def security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    return response
```

### **Authentication Best Practices**

✅ Already implemented in your code:
- Token-based authentication (Bearer tokens)
- Redis session caching
- Admin role checking
- Password hashing (SHA256)

⚠️ **Improvements needed:**
```python
# Use bcrypt instead of SHA256 for password hashing
from werkzeug.security import generate_password_hash, check_password_hash

# Better token generation
import secrets
token = secrets.token_urlsafe(32)  # Instead of hash-based

# Add rate limiting
from flask_limiter import Limiter
limiter = Limiter(app, key_func=lambda: request.remote_addr)

@app.route('/login', methods=['POST'])
@limiter.limit("5 per minute")  # 5 login attempts per minute
def login():
    # ...
```

### **Cloudflare Security Settings**

In Cloudflare Dashboard:
1. **SSL/TLS**: Set to "Full" or "Full (Strict)"
2. **Security Level**: "High" to challenge suspicious traffic
3. **WAF (Web Application Firewall)**: Enable free ruleset
4. **DDoS Protection**: Enabled by default
5. **Rate Limiting**: Enable to prevent abuse
   - Path: `/api/*`
   - Limit: 100 requests per 10 seconds
   - Action: Challenge

---

## 📝 Environment Variables Checklist

**On Raspberry Pi (`.env` or export):**
```bash
# Tunnel
export CLOUDFLARE_TUNNEL_ID="smarthome-gateway"

# API
export FLASK_ENV="production"
export API_PORT=5000
export SECRET_KEY="very-secure-random-string"

# Database
export DB_PATH="/data/smarthome.db"

# Services
export MQTT_BROKER="localhost"
export REDIS_HOST="localhost"

# CORS (restrict to your frontend)
export ALLOWED_ORIGINS="https://smarthome.vercel.app,https://api.yourdomain.com"
```

**On Vercel (Project Settings → Environment):**
```
NEXT_PUBLIC_API_URL=https://api.yourdomain.com
```

---

## 🧪 Testing Checklist

- [ ] Cloudflare tunnel running: `sudo systemctl status cloudflared`
- [ ] Flask API responding: `curl http://localhost:5000/health`
- [ ] Tunnel connected: `cloudflared tunnel info smarthome-gateway`
- [ ] DNS resolves: `nslookup api.yourdomain.com`
- [ ] HTTPS working: `curl https://api.yourdomain.com/health`
- [ ] CORS headers present: Check browser console for errors
- [ ] Frontend loads from Vercel: `https://smarthome.vercel.app`
- [ ] API calls work from frontend: Test login/fetch in DevTools
- [ ] WebSocket real-time: Check Socket.IO connection in DevTools
- [ ] From external network: Test on mobile hotspot

---

## 🔧 Troubleshooting

### **Tunnel Not Connecting**
```bash
# Check tunnel status
cloudflared tunnel info smarthome-gateway

# Check logs
sudo journalctl -u cloudflared -f

# Restart tunnel
sudo systemctl restart cloudflared.service

# Test API is running
curl http://localhost:5000/health
```

### **DNS Not Resolving**
```bash
# Flush DNS cache
sudo systemctl restart systemd-resolved

# Verify CNAME record
nslookup api.yourdomain.com
# Should point to *.cfargotunnel.com

# Check Cloudflare dashboard for DNS records
```

### **CORS Errors in Frontend**
```javascript
// Check browser console:
// 1. Are CORS headers present?
// 2. Is origin domain in ALLOWED_ORIGINS?
// 3. Is HTTP method in allowed methods?

// Test with curl:
curl -H "Origin: https://smarthome.vercel.app" \
  -H "Access-Control-Request-Method: GET" \
  -H "Access-Control-Request-Headers: Authorization" \
  -X OPTIONS https://api.yourdomain.com/health
```

### **SSL Certificate Errors**
```bash
# Cloudflare provides automatic SSL
# If still getting errors:
# 1. Wait 5-10 minutes for DNS propagation
# 2. Clear browser cache (Ctrl+Shift+Del)
# 3. Verify domain is in Cloudflare DNS
```

---

## 📚 Additional Resources

- **Cloudflare Tunnel Docs**: https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/install-and-setup/tunnel-guide/
- **Next.js Deployment**: https://nextjs.org/docs/deployment/vercel
- **Socket.IO Client**: https://socket.io/docs/v4/client-initialization/
- **Flask-CORS**: https://flask-cors.readthedocs.io/
- **Cloudflare Dashboard**: https://dash.cloudflare.com/

---

## ✅ Summary: What You Get

```
Before:
├─ API only accessible on LAN (192.168.x.x)
└─ No external access without port forwarding

After:
├─ ✅ Public HTTPS endpoint: https://api.yourdomain.com
├─ ✅ Frontend hosted on Vercel CDN globally
├─ ✅ No port forwarding needed
├─ ✅ DDoS protection via Cloudflare
├─ ✅ Free SSL certificates
├─ ✅ Real-time updates via WebSocket
├─ ✅ Secure authentication (tokens)
└─ ✅ Works on any network (phone hotspot, office, etc.)
```

**Cost**: FREE (Cloudflare free + Vercel free)
**Setup Time**: ~45 minutes
**Maintenance**: Minimal (tunnel auto-restarts on reboot)

---

Happy deploying! 🚀
