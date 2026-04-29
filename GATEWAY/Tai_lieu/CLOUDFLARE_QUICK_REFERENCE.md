# SmartHome Gateway - Cloudflare Tunnel Quick Reference

## 🚀 Quick Commands

### Installation

```bash
# Download cloudflared
cd /home/pi
wget https://github.com/cloudflare/cloudflared/releases/download/2024.4.1/cloudflared-linux-arm.tgz
tar xzf cloudflared-linux-arm.tgz
sudo mv cloudflared /usr/local/bin/
cloudflared version

# Create tunnel directory
mkdir -p ~/.cloudflared

# Authenticate
cloudflared tunnel login

# Create tunnel
cloudflared tunnel create smarthome-gateway

# Create config (~/.cloudflared/config.yml)
# See: https://github.com/cloudflare/cloudflared/wiki/Configuration

# Setup systemd service
# Copy: cloudflared.service → /etc/systemd/system/
# Then: sudo systemctl daemon-reload && sudo systemctl enable cloudflared.service
```

### Running the Tunnel

```bash
# Manual run (for testing)
cloudflared tunnel run smarthome-gateway

# Start as service
sudo systemctl start cloudflared.service
sudo systemctl status cloudflared.service
sudo systemctl stop cloudflared.service
sudo systemctl restart cloudflared.service

# View logs
sudo journalctl -u cloudflared -f    # Real-time
sudo journalctl -u cloudflared -n 50 # Last 50 lines
tail -f /var/log/cloudflared.log

# Validate config
cloudflared tunnel validate

# List tunnels
cloudflared tunnel list

# Get tunnel info
cloudflared tunnel info smarthome-gateway

# Delete tunnel (if needed)
cloudflared tunnel delete smarthome-gateway
```

### DNS & Routing

```bash
# Create CNAME DNS record
cloudflared tunnel route dns smarthome-gateway api.yourdomain.com

# Or manually in Cloudflare dashboard:
# DNS → Add record
# Type: CNAME
# Name: api
# Target: smarthome-gateway-<UUID>.cfargotunnel.com

# Verify DNS
nslookup api.yourdomain.com
dig api.yourdomain.com

# Check if tunnel is routing traffic
curl https://api.yourdomain.com/health
```

### Monitoring

```bash
# Check if Flask API is running
curl http://localhost:5000/health

# Check if tunnel is connected
curl https://api.yourdomain.com/health

# Test from external network (not home WiFi)
ssh user@external-server
curl https://api.yourdomain.com/health

# Monitor tunnel bandwidth
# → https://dash.cloudflare.com/
# → Select domain → Analytics → Requests

# Monitor tunnel connections
cloudflared tunnel info smarthome-gateway | grep "metrics"
```

### Troubleshooting

```bash
# 1. Check authentication
cloudflared tunnel validate
# Output: ✓ Config is valid

# 2. Check credentials file
ls -la ~/.cloudflared/
# Should see: cert.pem, <UUID>.json, config.yml

# 3. Re-authenticate if needed
cloudflared tunnel login

# 4. Check config syntax
cat ~/.cloudflared/config.yml | grep -E "tunnel:|credentials-file:"

# 5. Test connectivity to Flask
curl -v http://localhost:5000/health

# 6. Check if tunnel is connected
# Look for "Connected to api.yourdomain.com" in logs

# 7. Check DNS propagation
# nslookup api.yourdomain.com
# Should point to cloudflare edge

# 8. If tunnel won't start:
cloudflared tunnel login           # Re-authenticate
cloudflared tunnel validate        # Check config
sudo systemctl restart cloudflared # Restart service

# 9. Check system resources (if tunnel crashes)
top
free -h
df -h
```

### Configuration Template

**File: `~/.cloudflared/config.yml`**

```yaml
tunnel: smarthome-gateway
credentials-file: /home/pi/.cloudflared/<YOUR-UUID>.json

ingress:
  - hostname: api.yourdomain.com
    service: http://localhost:5000
    originRequest:
      httpHostHeader: localhost
      noTLSVerify: true

  - service: http_status:404

loglevel: info
logfile: /home/pi/.cloudflared/cloudflared.log
```

### Systemd Service Template

**File: `/etc/systemd/system/cloudflared.service`**

```ini
[Unit]
Description=Cloudflare Tunnel
After=network.target

[Service]
Type=simple
User=pi
ExecStart=/usr/local/bin/cloudflared tunnel run smarthome-gateway
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

---

## 🔑 Important URLs

| Service | URL | Purpose |
|---------|-----|---------|
| Cloudflare Dashboard | https://dash.cloudflare.com | Manage tunnel, DNS, WAF |
| Tunnel Documentation | https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/ | Official docs |
| API Endpoint | https://api.yourdomain.com | Your public API |
| Frontend | https://smarthome-dashboard.vercel.app | Web dashboard |
| GitHub Releases | https://github.com/cloudflare/cloudflared/releases | Download cloudflared |

---

## 📊 Status Verification

### Everything Working ✅

```bash
# All should return 200/PONG
curl http://localhost:5000/health           # Flask API (local)
redis-cli ping                               # Redis (should say PONG)
mosquitto_sub -h localhost -t '$SYS/broker' # MQTT (should respond)

# All should show "active (running)"
sudo systemctl status cloudflared
sudo systemctl status gateway.service  # if using systemd
sudo systemctl status mosquitto
sudo systemctl status redis-server

# Should show tunnel connected
sudo journalctl -u cloudflared -n 5 | grep "Connected"

# Should resolve to Cloudflare edge
nslookup api.yourdomain.com
```

### Testing Tunnel Connection

```bash
# From Raspberry Pi (local test)
curl -v https://api.yourdomain.com/health

# From external network (SSH to another server)
ssh user@external-server "curl https://api.yourdomain.com/health"

# From phone on different network
# Open browser: https://api.yourdomain.com/health
# Should see JSON response
```

---

## 🔒 Security Quick Checks

```bash
# Check security headers are present
curl -I https://api.yourdomain.com/health | grep -E 'X-|Content-Security|Strict'

# Check CORS headers
curl -H "Origin: https://smarthome.vercel.app" \
  -H "Access-Control-Request-Method: GET" \
  -X OPTIONS https://api.yourdomain.com/health

# Check SSL/TLS certificate
openssl s_client -connect api.yourdomain.com:443

# Check if DDoS protection is enabled
# https://dash.cloudflare.com → Security → DDoS
# Should show: "Under Attack Mode: On"

# Check WAF rules
# https://dash.cloudflare.com → Security → WAF
# Should show: "Managed Rules: On"
```

---

## 🆘 Emergency Commands

### If System is Compromised

```bash
# 1. Immediately disconnect tunnel
sudo systemctl stop cloudflared.service

# 2. Kill API
pkill -f "python gateway_main.py"

# 3. Kill all sessions (careful!)
redis-cli FLUSHALL

# 4. Reset all user passwords
sqlite3 /data/smarthome.db << EOF
UPDATE users SET password_hash = NULL WHERE id != 1;
EOF

# 5. Review audit logs
sqlite3 /data/smarthome.db "SELECT * FROM audit_logs ORDER BY timestamp DESC LIMIT 100;"

# 6. Restart services after fixing
sudo systemctl start cloudflared.service
python gateway_main.py &
```

### Restart Everything

```bash
# Kill all services
sudo systemctl stop cloudflared.service
pkill -f "python gateway_main.py"
sudo systemctl stop mosquitto
sudo systemctl stop redis-server

# Restart all
sudo systemctl start redis-server
sudo systemctl start mosquitto
python /home/pi/GATEWAY/gateway_main.py &
sudo systemctl start cloudflared.service

# Verify
sudo systemctl status cloudflared.service
curl https://api.yourdomain.com/health
```

---

## 📈 Performance Tuning

```bash
# Check CPU/Memory usage
top
# Pi should use <10% CPU, <30% RAM for idle tunnel

# Check network bandwidth
# Dashboard → Analytics → Requests
# Total bandwidth and request count

# Optimize number of workers
# In config.yml: num-workers: 1  # Keep low for Pi
```

---

## 🔄 Updating cloudflared

```bash
# Check current version
cloudflared version

# Download latest
ARCH=arm  # or arm64 for 64-bit
URL=$(curl -s https://api.github.com/repos/cloudflare/cloudflared/releases/latest | grep -oP '"browser_download_url": "\K[^"]*linux-'$ARCH'\.tgz')
wget $URL
tar xzf cloudflared-linux-$ARCH.tgz

# Backup old version
sudo cp /usr/local/bin/cloudflared /usr/local/bin/cloudflared.bak

# Install new version
sudo mv cloudflared /usr/local/bin/
sudo systemctl restart cloudflared.service

# Verify
cloudflared version
sudo systemctl status cloudflared.service
curl https://api.yourdomain.com/health
```

---

## 📚 Cheat Sheet References

### Common Curl Commands

```bash
# Test API endpoint
curl https://api.yourdomain.com/health

# Test with authentication
curl -H "Authorization: Bearer YOUR_TOKEN" \
  https://api.yourdomain.com/user/profile

# POST request
curl -X POST \
  -H "Content-Type: application/json" \
  -d '{"email":"test@example.com"}' \
  https://api.yourdomain.com/endpoint

# Verbose output (useful for debugging)
curl -v https://api.yourdomain.com/health

# Check response headers only
curl -I https://api.yourdomain.com/health
```

### Cloudflare Dashboard Navigation

1. Login: https://dash.cloudflare.com
2. Select domain: yourdomain.com
3. **DNS**: Add/manage DNS records
4. **Security**: WAF, rate limiting, DDoS settings
5. **Workers**: Serverless functions (if using)
6. **Analytics**: Traffic, requests, errors
7. **SSL/TLS**: Certificate status and settings
8. **Caching**: Cache rules and purging

---

## ❓ FAQ

**Q: How do I change the API port?**
```yaml
# In config.yml
- hostname: api.yourdomain.com
  service: http://localhost:8000  # Change from 5000 to 8000
```

**Q: How do I add HTTPS to WebSocket?**
```yaml
# In config.yml
- hostname: api.yourdomain.com
  path: /socket.io/*
  service: wss://localhost:5001  # Use wss:// for secure WebSocket
```

**Q: How do I add multiple domains?**
```yaml
# In config.yml
ingress:
  - hostname: api.yourdomain.com
    service: http://localhost:5000
  
  - hostname: api2.yourdomain.com
    service: http://localhost:5001
```

**Q: How do I add authentication to the tunnel?**
```yaml
# In config.yml - Cloudflare handles authentication via Access
# But you still need app-level auth for your API
# Use the require_auth decorator in Flask
```

**Q: How often should I update cloudflared?**
- Check monthly for security updates
- Update immediately if Cloudflare releases critical patch

**Q: What happens if my internet goes down?**
- Tunnel disconnects
- Users get "connection refused"
- Automatic reconnect when internet returns

**Q: Can I run multiple tunnels?**
- Yes, create multiple: `cloudflared tunnel create tunnel-2`
- Each can route different subdomains/services

---

## 📞 Getting Help

**If something isn't working:**

1. **Check logs first:**
   ```bash
   sudo journalctl -u cloudflared -f
   tail -f ~/.cloudflared/cloudflared.log
   ```

2. **Test components independently:**
   ```bash
   curl http://localhost:5000/health           # Flask API
   curl https://api.yourdomain.com/health      # Via tunnel
   nslookup api.yourdomain.com                 # DNS
   ```

3. **Verify configuration:**
   ```bash
   cloudflared tunnel validate
   cat ~/.cloudflared/config.yml
   ```

4. **Consult documentation:**
   - Cloudflare Tunnel: https://developers.cloudflare.com/cloudflare-one/
   - cloudflared CLI: cloudflared --help
   - Tunnel troubleshooting: https://developers.cloudflare.com/cloudflare-one/troubleshooting/

---

**Last updated:** April 2024  
**Status:** Production Ready ✅
