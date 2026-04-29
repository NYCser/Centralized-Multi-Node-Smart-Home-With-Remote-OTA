# SmartHome Gateway - Complete Deployment Guide

## 📋 Complete Step-by-Step Deployment (Estimated: 2-3 hours)

This guide walks you through deploying your entire system from start to finish.

---

## 🎯 Overview

```
BEFORE (Local Only)          AFTER (Public)
┌─────────────────┐          ┌─────────────────┐
│ Raspberry Pi    │          │ Vercel Frontend │ https://smarthome.vercel.app
│ API on 192.x    │          └────────┬────────┘
│ (LAN only)      │                   │
└────────┬────────┘          ┌────────▼────────┐
         │                   │ Cloudflare      │ https://api.yourdomain.com
         │                   │ Tunnel (free)   │
         │                   └────────┬────────┘
         │                            │
         └────────────────────────────┘
                Outbound only
                (No port forwarding)
```

---

## 📦 Prerequisites (Verify Before Starting)

```bash
# 1. SSH into Raspberry Pi
ssh pi@<your-pi-ip>

# 2. Check Python version (must be 3.7+)
python3 --version

# 3. Check Git is installed
git --version

# 4. Check Internet connectivity
curl https://www.google.com -I

# 5. Check MQTT is running
mosquitto_sub -h localhost -t '#' -W 1 -C 1

# 6. Check Redis is running
redis-cli ping  # Should reply: PONG

# 7. Check Flask API is running
curl http://localhost:5000/health

# All checks should pass before continuing!
```

---

## 🚀 Phase 1: Register & Prepare Cloudflare (15 mins)

### Step 1.1: Create Cloudflare Account

```
1. Go: https://dash.cloudflare.com/sign-up
2. Enter email and password
3. Click "Create Account"
4. Verify email
5. Choose "Free" plan when prompted
```

### Step 1.2: Add Your Domain to Cloudflare

```
IF YOU ALREADY OWN A DOMAIN (e.g., yourdomain.com):

1. Login to Cloudflare: https://dash.cloudflare.com
2. Click "Add a Site"
3. Enter: yourdomain.com
4. Choose "Free" plan
5. Cloudflare will show your nameservers:
   - ns1.cloudflare.com
   - ns2.cloudflare.com
6. Go to your domain registrar (GoDaddy, Namecheap, etc.)
7. Update nameservers to above
8. Wait 5-30 minutes for propagation
9. Cloudflare will auto-verify once nameservers are updated


IF YOU DON'T HAVE A DOMAIN YET:

Option A: Buy one (~$1-3/year)
- Registrars: namecheap.com, godaddy.com, google.com/domains
- Choose any domain name you like
- Then follow steps above

Option B: Use a free domain
- freenom.com (free .tk, .ml, .ga domains)
- duck.dns (ddns-style, free)
- no-ip.com (free DynDNS)

FOR THIS GUIDE: We assume yourdomain.com

VERIFY: Nameservers updated
- Check: https://www.whatsmydns.net/
- Enter: yourdomain.com
- All should point to Cloudflare nameservers
```

### Step 1.3: Create Cloudflare API Token

```
1. Login to Cloudflare: https://dash.cloudflare.com/profile/api-tokens
2. Click "Create Token"
3. Use template "Cloudflare Workers" (or custom)
4. Permissions needed:
   ✓ Account.Access: Edit
   ✓ Account.Tunnel: Edit
   ✓ Zone.DNS: Edit
5. Click "Continue to Summary"
6. Click "Create Token"
7. COPY THE TOKEN (you'll need it next)
8. Save to safe place (password manager)

Token looks like: _eJ_zQuYiBLiR0_eJ_zQuYiBLiR...
```

### Step 1.4: Verify Domain

```bash
# On Raspberry Pi, test domain resolution:
nslookup yourdomain.com

# Should show Cloudflare nameservers:
# Non-authoritative answer:
# Name: yourdomain.com
# Address: 1.2.3.4 (some IP)
```

---

## 🏗️ Phase 2: Install Cloudflare Tunnel (20 mins)

### Step 2.1: Download cloudflared

```bash
# SSH into Raspberry Pi
ssh pi@<your-pi-ip>

# Determine CPU architecture
uname -m
# Output: armv7l (32-bit) or aarch64 (64-bit)

# Download cloudflared (choose correct version)

# FOR 32-BIT (Pi 3, older Pi 4):
cd /home/pi
wget https://github.com/cloudflare/cloudflared/releases/download/2024.4.1/cloudflared-linux-arm.tgz
tar xzf cloudflared-linux-arm.tgz

# FOR 64-BIT (Pi 4 with 64-bit OS, Pi 5):
cd /home/pi
wget https://github.com/cloudflare/cloudflared/releases/download/2024.4.1/cloudflared-linux-arm64.tgz
tar xzf cloudflared-linux-arm64.tgz

# Make executable
chmod +x cloudflared

# Move to system path
sudo mv cloudflared /usr/local/bin/

# Test
cloudflared version
# Expected: cloudflared version 2024.4.1 ...

# Create config directory
mkdir -p ~/.cloudflared
```

**Note:** Check latest version at: https://github.com/cloudflare/cloudflared/releases

### Step 2.2: Authenticate cloudflared

```bash
# This will open a browser to authorize
cloudflared tunnel login

# You'll see output like:
# Please open the following URL and log in with your Cloudflare account:
# https://dash.cloudflare.com/argotunnel?token=...
#
# Open this URL in your browser
# Click "Authorize"
# Certificate will be saved to: ~/.cloudflared/cert.pem

# Verify
ls -la ~/.cloudflared/
# Should show: cert.pem (and possibly other files)
```

### Step 2.3: Create Tunnel

```bash
# Create tunnel with name "smarthome-gateway"
cloudflared tunnel create smarthome-gateway

# Output will show:
# Tunnel credentials written to /home/pi/.cloudflared/<UUID>.json
# Created tunnel smarthome-gateway with id <LONG-ID>

# IMPORTANT: Write down or copy the UUID and ID
# UUID looks like: a1b2c3d4-e5f6-7890-abcd-ef1234567890
# ID looks like: smarthome-gateway-a1b2c3d4e5f67890

# Verify tunnel was created
cloudflared tunnel list
# Should show: smarthome-gateway | <UUID> | <ID>

# Find UUID from credentials file
UUID=$(ls ~/.cloudflared/*.json | grep -oE '[a-f0-9-]{36}' | head -1)
echo "Your Tunnel UUID: $UUID"
```

---

## ⚙️ Phase 3: Configure Tunnel (10 mins)

### Step 3.1: Create Configuration File

```bash
# Create config file
nano ~/.cloudflared/config.yml
```

**Paste this (replace yourdomain.com):**

```yaml
# Cloudflare Tunnel Configuration
tunnel: smarthome-gateway
credentials-file: /home/pi/.cloudflared/a1b2c3d4-e5f6-7890-abcd-ef1234567890.json

ingress:
  - hostname: api.yourdomain.com
    path: /*
    service: http://localhost:5000
    originRequest:
      httpHostHeader: localhost
      noTLSVerify: true
      headers:
        add:
          X-Tunnel-Origin: cloudflare

  - service: http_status:404

loglevel: info
logfile: /home/pi/.cloudflared/cloudflared.log
grace-period: 30s
heartbeat-interval: 30s
max-failed-heartbeats: 5
num-workers: 1
```

**IMPORTANT:** Replace:
- `a1b2c3d4-e5f6-7890-abcd-ef1234567890` with YOUR UUID from step 2.3
- `yourdomain.com` with YOUR actual domain

### Step 3.2: Validate Configuration

```bash
# Test config syntax
cloudflared tunnel validate

# Expected output:
# Validating configuration... ✓ Config is valid
```

### Step 3.3: Test Tunnel Manually

```bash
# In one terminal window:
# Make sure Flask API is running
curl http://localhost:5000/health

# In another terminal, start tunnel
cloudflared tunnel run smarthome-gateway

# You should see:
# [2024-04-16T10:30:45Z] INFO Starting tunnel smarthome-gateway
# [2024-04-16T10:30:47Z] INFO Tunnel authenticated successfully
# [2024-04-16T10:30:47Z] INFO Connected to api.yourdomain.com

# Leave this running, test in another terminal
```

### Step 3.4: Test from External Network

```bash
# From different computer/network (NOT your home WiFi):
curl https://api.yourdomain.com/health

# Expected response:
# {"status": "ok", ...}

# If you get 404 or timeout:
# 1. Check Flask API is running
# 2. Check DNS: nslookup api.yourdomain.com
# 3. Check tunnel logs for errors
```

---

## 🔧 Phase 4: Setup DNS Records (5 mins)

### Step 4.1: Create CNAME Record

```bash
# Option 1: Via Cloudflare Dashboard

# 1. Login: https://dash.cloudflare.com/
# 2. Select domain: yourdomain.com
# 3. Go to: DNS
# 4. Click "Add record"
# 5. Fill in:
#    Type: CNAME
#    Name: api (subdomain)
#    Target: smarthome-gateway-<YOUR-ID>.cfargotunnel.com
#    TTL: Auto
#    Proxy: Proxied (orange cloud)
# 6. Click "Save"


# Option 2: Via cloudflared CLI
cloudflared tunnel route dns smarthome-gateway api.yourdomain.com

# This automatically creates the CNAME record
```

### Step 4.2: Verify DNS

```bash
# Check DNS propagation (may take 5-10 mins)
nslookup api.yourdomain.com

# Expected output:
# Non-authoritative answer:
# Name: api.yourdomain.com
# Address: <some-IP> (Cloudflare edge)
# Or CNAME: smarthome-gateway-xxx.cfargotunnel.com

# Test with curl
curl https://api.yourdomain.com/health

# If still not working:
# Wait another 5-10 minutes
# Clear DNS cache: sudo systemctl restart systemd-resolved
# Try again
```

---

## 🔐 Phase 5: Run Tunnel as Service (10 mins)

### Step 5.1: Create Systemd Service File

```bash
# Create service file
sudo nano /etc/systemd/system/cloudflared.service
```

**Paste this:**

```ini
[Unit]
Description=Cloudflare Tunnel - SmartHome Gateway Public API
After=network.target
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

[Install]
WantedBy=multi-user.target
```

**Save and exit** (Ctrl+X, Y, Enter)

### Step 5.2: Enable and Start Service

```bash
# Reload systemd
sudo systemctl daemon-reload

# Enable on boot
sudo systemctl enable cloudflared.service

# Start tunnel
sudo systemctl start cloudflared.service

# Check status
sudo systemctl status cloudflared.service
# Should show: active (running)

# View logs
sudo journalctl -u cloudflared -f

# Wait 10 seconds for "Connected to api.yourdomain.com"
# Then Ctrl+C to exit logs

# Test from external network
curl https://api.yourdomain.com/health
```

---

## 🌐 Phase 6: Deploy Frontend on Vercel (30 mins)

### Step 6.1: Create Frontend Project

**On your local computer (not Pi):**

```bash
# Create Next.js project
npx create-next-app@latest smarthome-dashboard \
  --typescript \
  --tailwind \
  --eslint

cd smarthome-dashboard
```

### Step 6.2: Setup Environment Variables

**Create `.env.local`:**

```bash
NEXT_PUBLIC_API_URL=https://api.yourdomain.com
```

### Step 6.3: Create API Service Layer

**File: `src/services/api.ts`**

```typescript
const API_BASE = process.env.NEXT_PUBLIC_API_URL || 'https://api.yourdomain.com';

export const api = {
  async get(endpoint: string, token?: string) {
    const headers: Record<string, string> = {};
    if (token) headers['Authorization'] = `Bearer ${token}`;
    
    const response = await fetch(`${API_BASE}${endpoint}`, { headers });
    if (!response.ok) throw new Error(`API ${response.status}`);
    return response.json();
  },

  async post(endpoint: string, body: any, token?: string) {
    const headers: Record<string, string> = { 'Content-Type': 'application/json' };
    if (token) headers['Authorization'] = `Bearer ${token}`;
    
    const response = await fetch(`${API_BASE}${endpoint}`, {
      method: 'POST',
      headers,
      body: JSON.stringify(body),
    });
    if (!response.ok) throw new Error(`API ${response.status}`);
    return response.json();
  },
};
```

### Step 6.4: Create Sample Pages

**File: `src/app/login/page.tsx`**

```typescript
'use client';

import { useState } from 'react';
import { useRouter } from 'next/navigation';
import { api } from '@/services/api';

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');

  const handleLogin = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      const result = await api.post('/auth/login', { email, password });
      localStorage.setItem('authToken', result.token);
      router.push('/dashboard');
    } catch (err: any) {
      setError(err.message);
    }
  };

  return (
    <div className="flex h-screen items-center justify-center">
      <div className="w-96 space-y-4 rounded bg-white p-8 shadow">
        <h1 className="text-2xl font-bold">SmartHome Login</h1>
        {error && <p className="text-red-600">{error}</p>}
        
        <form onSubmit={handleLogin} className="space-y-4">
          <input
            type="email"
            placeholder="Email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className="w-full rounded border p-2"
            required
          />
          <input
            type="password"
            placeholder="Password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="w-full rounded border p-2"
            required
          />
          <button
            type="submit"
            className="w-full rounded bg-blue-600 p-2 text-white font-bold hover:bg-blue-700"
          >
            Login
          </button>
        </form>
      </div>
    </div>
  );
}
```

### Step 6.5: Push to GitHub

```bash
# Initialize git
git init
git add .
git commit -m "Initial commit"

# Create repository on github.com

# Add remote
git remote add origin https://github.com/<YOUR-USERNAME>/smarthome-dashboard.git
git branch -M main
git push -u origin main
```

### Step 6.6: Deploy on Vercel

```
1. Go: https://vercel.com/new
2. Connect GitHub account
3. Select: smarthome-dashboard
4. Click "Import"
5. Set environment variable:
   - Name: NEXT_PUBLIC_API_URL
   - Value: https://api.yourdomain.com
6. Click "Deploy"
7. Wait ~2 minutes for deployment
8. Click "Visit" to see your app
```

### Step 6.7: Test Frontend

```bash
# Open in browser
https://smarthome-dashboard.vercel.app

# Or if you set up custom domain
https://smarthome.yourdomain.com

# Try to login with test credentials
# Should connect to your API via tunnel
```

---

## 🧪 Phase 7: Verify Everything Works (15 mins)

### Checklist

```bash
# 1. Raspberry Pi side
sudo systemctl status cloudflared.service      # ✓ running
sudo systemctl status gateway.service          # ✓ running  (if using systemd)
curl http://localhost:5000/health              # ✓ 200 OK
curl http://localhost:5001/health              # ✓ 200 OK (if WebSocket)

# 2. Internet side
curl https://api.yourdomain.com/health         # ✓ 200 OK (from external network)
nslookup api.yourdomain.com                    # ✓ resolves

# 3. Vercel side
open https://smarthome-dashboard.vercel.app    # ✓ loads
# Try login in browser
# Should connect to https://api.yourdomain.com

# 4. Cloudflare
open https://dash.cloudflare.com
# Check Analytics → Traffic
# Should see requests to api.yourdomain.com

# 5. Logs
sudo journalctl -u cloudflared -n 50           # Check for errors
tail -f /var/log/cloudflared.log               # Real-time tunnel logs
```

---

## 🔒 Phase 8: Security Hardening (30 mins)

### Quick Security Setup

```bash
# 1. Upgrade password hashing (see SECURITY_HARDENING.md)
pip install bcrypt

# 2. Add rate limiting (see SECURITY_HARDENING.md)
pip install Flask-Limiter

# 3. Enable Cloudflare WAF
# → Dashboard → Security → WAF
# → Enable "Managed Rules"

# 4. Enable rate limiting in Cloudflare
# → Dashboard → Security → Rate Limiting
# Create rules for /auth/login and /devices/*/control

# 5. Restrict CORS to your frontend
# In gateway_main.py:
# CORS(app, origins=['https://smarthome-dashboard.vercel.app'])

# 6. Review security headers (in gateway_main.py):
# @app.after_request
# def add_security_headers(response):
#     response.headers['Strict-Transport-Security'] = 'max-age=31536000'
#     return response
```

---

## 📊 Phase 9: Monitor & Maintain

### Daily Checks

```bash
# Check tunnel is connected
sudo systemctl status cloudflared.service

# Check for errors
sudo journalctl -u cloudflared -n 20

# Check API is responsive
curl https://api.yourdomain.com/health

# Check Cloudflare dashboard
# → https://dash.cloudflare.com/
# → Check security and traffic
```

### Weekly Checks

```bash
# Update packages
sudo apt update && sudo apt upgrade -y

# Check disk space
df -h

# Backup database
cp /data/smarthome.db /backup/smarthome.db.$(date +%Y%m%d)

# Review audit logs
sqlite3 /data/smarthome.db "SELECT * FROM audit_logs LIMIT 20;"
```

### Monthly Checks

- Review failed login attempts
- Check Cloudflare WAF blocks (false positives?)
- Update cloudflared binary
- Rotate API keys
- Test backup restore process

---

## 🚨 Troubleshooting

### Tunnel not connecting

```bash
# Check authentication
cloudflared tunnel validate

# Check credentials file exists
ls ~/.cloudflared/

# Re-authenticate if needed
cloudflared tunnel login

# Check logs
sudo journalctl -u cloudflared -f
```

### DNS not resolving

```bash
# Wait 5-10 minutes
# Clear cache
sudo systemctl restart systemd-resolved

# Verify CNAME exists
nslookup api.yourdomain.com

# Check Cloudflare dashboard
# → DNS → Should show CNAME record
```

### API calls from frontend failing

```bash
# Check from browser console:
fetch('https://api.yourdomain.com/health')
  .then(r => r.json())
  .then(console.log)
  .catch(console.error)

# Check CORS headers:
curl -i https://api.yourdomain.com/health | grep -i access-control

# Update CORS in gateway_main.py if needed
```

### High latency/slow responses

```bash
# Check Pi CPU/memory
top

# Check network connectivity
ping 8.8.8.8
mtr google.com

# Check tunnel bandwidth usage
# → Cloudflare Dashboard → Analytics

# Optimize Flask app or reduce worker threads
```

---

## ✅ Final Checklist

Before declaring success:

- [ ] Domain registered and pointing to Cloudflare
- [ ] Cloudflare tunnel installed and authenticated
- [ ] Tunnel running as systemd service
- [ ] DNS record (CNAME) created
- [ ] Tunnel accessible from public internet
- [ ] Frontend deployed on Vercel
- [ ] Frontend loads and can call API
- [ ] Login works end-to-end
- [ ] Device control works end-to-end
- [ ] Real-time updates working
- [ ] Security headers configured
- [ ] Cloudflare WAF enabled
- [ ] No console errors in browser
- [ ] No errors in tunnel logs
- [ ] Response times acceptable (<500ms)
- [ ] All components auto-restart on reboot

---

## 🎉 Deployment Complete!

Your SmartHome Gateway is now:
- ✅ Publicly accessible at https://api.yourdomain.com
- ✅ Secured via Cloudflare
- ✅ Behind no port forwarding (safe)
- ✅ With global CDN frontend on Vercel
- ✅ With real-time updates via WebSocket
- ✅ With authentication and audit logging

### What Users See

**From home WiFi:**
```bash
curl https://api.yourdomain.com/health
# ✓ Works (same as before, just over internet)
```

**From phone hotspot:**
```bash
curl https://api.yourdomain.com/health
# ✓ Works (different network)
```

**From office WiFi:**
```bash
# Open browser: https://smarthome-dashboard.vercel.app
# ✓ Loads and can control devices (from anywhere in world)
```

---

## 📞 Support

If you encounter issues:

1. **Check logs first:**
   ```bash
   sudo journalctl -u cloudflared -f
   tail -f /var/log/cloudflared.log
   curl http://localhost:5000/health
   ```

2. **Common issues:** See "Troubleshooting" section above

3. **Documentation:**
   - Cloudflare Tunnel: https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/
   - Next.js/Vercel: https://nextjs.org/docs/deployment/vercel
   - Flask: https://flask.palletsprojects.com/

---

**Congratulations! Your system is now production-ready.** 🚀
