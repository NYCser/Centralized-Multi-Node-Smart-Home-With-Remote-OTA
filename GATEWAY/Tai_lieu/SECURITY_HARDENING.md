# SmartHome Gateway - Security Hardening Guide

## 🔒 Security Threats & Mitigations

Your system is now exposed to the internet via Cloudflare Tunnel. This guide helps you secure it against common threats.

---

## 🎯 Threat Model

### Threats to Consider

```
1. AUTHENTICATION BYPASS
   └─ Attacker guesses weak password or tokens
   └─ Mitigation: Strong password hashing + rate limiting

2. API ABUSE / DDOS
   └─ Attacker makes thousands of requests
   └─ Mitigation: Rate limiting + Cloudflare WAF

3. DATA BREACH
   └─ Attacker gains access to sensor data
   └─ Mitigation: HTTPS encryption + authentication

4. UNAUTHORIZED CONTROL
   └─ Attacker toggles devices without permission
   └─ Mitigation: Token verification + audit logs

5. CROSS-SITE ATTACKS (CSRF)
   └─ Malicious website performs actions on user's behalf
   └─ Mitigation: SameSite cookies + CSRF tokens

6. XSS ATTACKS
   └─ Attacker injects malicious JavaScript
   └─ Mitigation: CSP headers + input sanitization
```

---

## 🔐 Security Improvements (Priority Order)

### 1️⃣ PRIORITY 1: Upgrade Password Hashing

**Current (SHA256 - WEAK):**
```python
import hashlib
password_hash = hashlib.sha256(password.encode()).hexdigest()
```

**Upgrade (bcrypt - STRONG):**
```bash
pip install bcrypt
```

**Update [app/api/routes/all_routes.py](app/api/routes/all_routes.py):**

```python
from bcrypt import hashpw, checkpw, gensalt

def hash_password(password: str) -> str:
    """Hash password with bcrypt (takes ~0.5s - acceptable)"""
    return hashpw(password.encode(), gensalt(rounds=12)).decode()

def verify_password(password: str, hashed: str) -> bool:
    """Verify password against hash"""
    return checkpw(password.encode(), hashed.encode())

# Usage:
hashed = hash_password(user_password)
if verify_password(login_password, hashed):
    # Allow login
```

### 2️⃣ PRIORITY 2: Add Rate Limiting

**Install:**
```bash
pip install Flask-Limiter
```

**Update [gateway_main.py](gateway_main.py):**

```python
from flask_limiter import Limiter
from flask_limiter.util import get_remote_addr

limiter = Limiter(
    app=app,
    key_func=get_remote_addr,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="redis://localhost:6379"
)

app.config['RATELIMIT_STORAGE_URL'] = 'redis://localhost:6379'

# Apply to sensitive endpoints
@app.route('/auth/login', methods=['POST'])
@limiter.limit("5 per minute")  # Max 5 login attempts/minute
def login():
    # ... login logic
    pass

@app.route('/devices/<id>/control', methods=['POST'])
@limiter.limit("60 per minute")  # Max 60 device controls/minute
@require_auth
def control_device(id):
    # ... control logic
    pass
```

### 3️⃣ PRIORITY 3: Add HTTPS Security Headers

**Update [gateway_main.py](gateway_main.py):**

```python
@app.after_request
def security_headers(response):
    # Prevent content-type sniffing
    response.headers['X-Content-Type-Options'] = 'nosniff'
    
    # Prevent clickjacking
    response.headers['X-Frame-Options'] = 'DENY'
    
    # Enable XSS protection
    response.headers['X-XSS-Protection'] = '1; mode=block'
    
    # Force HTTPS
    response.headers['Strict-Transport-Security'] = \
        'max-age=31536000; includeSubDomains; preload'
    
    # Content Security Policy
    response.headers['Content-Security-Policy'] = \
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'"
    
    # Referrer policy
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    
    # Feature policy
    response.headers['Permissions-Policy'] = 'geolocation=(), microphone=(), camera=()'
    
    return response
```

### 4️⃣ PRIORITY 4: Implement CSRF Protection

**Install:**
```bash
pip install Flask-WTF
```

**Update [app/main.py](app/main.py):**

```python
from flask_wtf.csrf import CSRFProtect

csrf = CSRFProtect(app)
app.config['WTF_CSRF_TIME_LIMIT'] = 3600  # 1 hour

# Exempt API endpoints that use token auth
@csrf.exempt
@app.route('/api/*', methods=['POST', 'PUT', 'DELETE'])
def api_endpoints():
    # Token-based auth is already CSRF-safe
    pass
```

**On Frontend, send CSRF token:**
```typescript
async function apiCall(endpoint: string, body: any) {
  const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');
  
  return fetch(`${API_URL}${endpoint}`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-CSRFToken': csrfToken || '',
      'Authorization': `Bearer ${authToken}`
    },
    body: JSON.stringify(body),
  });
}
```

### 5️⃣ PRIORITY 5: Implement Audit Logging

**Add to database schema:**
```sql
CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    resource TEXT NOT NULL,
    details TEXT,
    ip_address TEXT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE INDEX idx_audit_user ON audit_logs(user_id);
CREATE INDEX idx_audit_timestamp ON audit_logs(timestamp);
```

**Log all important actions:**
```python
import logging
from datetime import datetime
import sqlite3

def log_audit(user_id, action, resource, details=None):
    """Log action for security audit"""
    conn = sqlite3.connect(DB_PATH)
    ip = request.remote_addr or 'unknown'
    
    conn.execute("""
        INSERT INTO audit_logs (user_id, action, resource, details, ip_address)
        VALUES (?, ?, ?, ?, ?)
    """, (user_id, action, resource, details, ip))
    conn.commit()
    conn.close()

# Usage:
@app.route('/auth/login', methods=['POST'])
def login():
    # ... login logic
    log_audit(user_id, 'LOGIN', 'auth', f'login_successful')
    
@app.route('/devices/<id>/control', methods=['POST'])
@require_auth
def control_device(id):
    # ... control logic
    log_audit(
        request.current_user['id'],
        'DEVICE_CONTROL',
        f'device:{id}',
        f'action={action}'
    )
```

### 6️⃣ PRIORITY 6: Secure Token Management

**Current approach (token expires):**
```python
SESSION_EXPIRE = 86400  # 24 hours
```

**Better approach (shorter expiry + refresh tokens):**
```python
from datetime import datetime, timedelta

# Short-lived access token (15 mins)
ACCESS_TOKEN_EXPIRE = 900

# Long-lived refresh token (7 days)
REFRESH_TOKEN_EXPIRE = 604800

def create_tokens(user_id):
    """Create access + refresh token pair"""
    access_token = secrets.token_urlsafe(32)
    refresh_token = secrets.token_urlsafe(32)
    
    now = datetime.utcnow()
    
    # Store in Redis with expiry
    r.setex(
        f'access:{access_token}',
        ACCESS_TOKEN_EXPIRE,
        str(user_id)
    )
    
    r.setex(
        f'refresh:{refresh_token}',
        REFRESH_TOKEN_EXPIRE,
        str(user_id)
    )
    
    return {
        'access_token': access_token,
        'refresh_token': refresh_token,
        'expires_in': ACCESS_TOKEN_EXPIRE
    }

@app.route('/auth/refresh', methods=['POST'])
def refresh_token():
    """Refresh expired access token"""
    refresh_token = request.json.get('refresh_token')
    user_id = r.get(f'refresh:{refresh_token}')
    
    if not user_id:
        return jsonify({'error': 'invalid_refresh_token'}), 401
    
    tokens = create_tokens(int(user_id))
    return jsonify(tokens)
```

---

## 🛡️ Cloudflare Security Configuration

### 1. Enable Web Application Firewall (WAF)

**Dashboard → Security → WAF Rules:**
- ✅ Enable "Managed Rules"
- ✅ Enable "OWASP ModSecurity Core Rule Set"
- ✅ Set sensitivity to "High"

### 2. Configure Rate Limiting

**Dashboard → Security → Rate Limiting:**

```
Rule 1: Limit login attempts
├─ Path: /auth/login
├─ Limit: 5 requests per minute per IP
└─ Action: Block

Rule 2: Limit device control
├─ Path: /devices/*/control
├─ Limit: 60 requests per minute per IP
└─ Action: Throttle (slow down)

Rule 3: General API
├─ Path: /api/*
├─ Limit: 100 requests per minute per IP
└─ Action: Challenge (CAPTCHA)
```

### 3. Enable Bot Management

**Dashboard → Security → Bot Management:**
- ✅ Enable "Super Bot Fight Mode" (free tier)
- ✅ Block identified bots
- ✅ Challenge suspicious traffic

### 4. Set SSL/TLS Mode

**Dashboard → SSL/TLS → Overview:**
- Set to: "Full (Strict)"
  - Requires valid SSL on origin
  - Validates Raspberry Pi certificate

**Workaround (for self-signed certs on localhost):**
```yaml
# cloudflared.yml - allow self-signed locally
ingress:
  - hostname: api.yourdomain.com
    service: http://localhost:5000
    originRequest:
      noTLSVerify: true  # Allow self-signed localhost
```

### 5. Configure DDoS Sensitivity

**Dashboard → Security → DDoS:**
- Set sensitivity: "High"
- This aggressively blocks suspicious patterns

---

## 🔑 API Key Management (Advanced)

Instead of passwords for API, use API keys:

```python
@app.route('/auth/api-keys', methods=['POST'])
@require_auth
def create_api_key():
    """Create API key for user"""
    key = secrets.token_urlsafe(32)
    hashed = hash_password(key)  # Use bcrypt
    
    user_id = request.current_user['id']
    conn = get_db()
    conn.execute("""
        INSERT INTO api_keys (user_id, key_hash, name, created_at)
        VALUES (?, ?, ?, datetime('now'))
    """, (user_id, hashed, request.json.get('name')))
    conn.commit()
    conn.close()
    
    return jsonify({
        'key': key,  # Only show once!
        'created_at': datetime.utcnow().isoformat()
    })

@app.route('/api/protected', methods=['GET'])
def protected_api():
    """Protected endpoint using API key"""
    key = request.headers.get('X-API-Key')
    
    if not key:
        return jsonify({'error': 'missing_api_key'}), 401
    
    # Find user by key (in production, cache this)
    conn = get_db()
    row = conn.execute("""
        SELECT ak.user_id FROM api_keys ak WHERE ak.key_hash = ?
    """, (hash_password(key),)).fetchone()
    
    if not row:
        return jsonify({'error': 'invalid_api_key'}), 401
    
    return jsonify({'data': '...'})
```

---

## 🧪 Security Testing Checklist

- [ ] **Authentication**
  - [ ] Try login with wrong password → 401
  - [ ] Try API without token → 401
  - [ ] Try API with expired token → 401
  - [ ] Token doesn't appear in logs

- [ ] **Authorization**
  - [ ] User can't access other users' data
  - [ ] Non-admin can't delete users
  - [ ] Non-admin can't view audit logs

- [ ] **Rate Limiting**
  - [ ] Make 6 login attempts in 1 minute → blocked
  - [ ] Make 100 device calls in 1 minute → blocked
  - [ ] Legitimate traffic still works

- [ ] **HTTPS/TLS**
  - [ ] `curl https://api.yourdomain.com` works
  - [ ] Invalid certs rejected
  - [ ] HSTS header present: `curl -i ... | grep Strict-Transport-Security`

- [ ] **Headers**
  - [ ] Check for security headers:
    ```bash
    curl -i https://api.yourdomain.com/health | grep -E 'X-|Content-Security|Strict-Transport'
    ```

- [ ] **CORS**
  - [ ] Request from wrong origin → blocked
  - [ ] Request with auth token → allowed

---

## 📊 Monitoring & Alerts

### Check Cloudflare Analytics

**Dashboard → Analytics → Traffic:**
- Monitor requests, errors, threats
- Check if WAF is blocking legitimate traffic

### Check Audit Logs

```python
@app.route('/admin/audit-logs', methods=['GET'])
@require_admin
def get_audit_logs():
    conn = get_db()
    logs = conn.execute("""
        SELECT * FROM audit_logs 
        ORDER BY timestamp DESC 
        LIMIT 1000
    """).fetchall()
    conn.close()
    return jsonify([dict(log) for log in logs])
```

### Monitor Failed Logins

```bash
# Query for suspicious activity
sqlite3 /data/smarthome.db << EOF
SELECT 
    user_id,
    action,
    COUNT(*) as count,
    MIN(timestamp) as first_attempt,
    MAX(timestamp) as last_attempt
FROM audit_logs
WHERE action = 'LOGIN_FAILED'
    AND timestamp > datetime('now', '-1 hour')
GROUP BY user_id, ip_address
HAVING COUNT(*) > 5
ORDER BY count DESC;
EOF
```

---

## 🚨 Incident Response

### If Your System is Compromised

1. **IMMEDIATE:**
   ```bash
   # Disconnect tunnel (blocks all traffic)
   sudo systemctl stop cloudflared.service
   
   # Kill Flask API
   pkill -f "python gateway_main.py"
   ```

2. **ASSESS:**
   - Check audit logs for unauthorized actions
   - Check which devices were controlled
   - Identify compromised user accounts

3. **REMEDIATE:**
   ```bash
   # Reset all user passwords
   sqlite3 /data/smarthome.db << EOF
   UPDATE users SET password_hash = NULL WHERE id != 1;
   EOF
   
   # Revoke all sessions/tokens
   redis-cli FLUSHALL  # WARNING: clears all Redis data!
   
   # Clear audit logs if attacker tampered with them
   # (backup first!)
   ```

4. **RESTORE:**
   - Restore from backup if available
   - Restart services
   - Force all users to change password on next login

---

## ✅ Security Deployment Checklist

Before going to production:

- [ ] Password hashing upgraded to bcrypt
- [ ] Rate limiting enabled on all sensitive endpoints
- [ ] Security headers configured
- [ ] CSRF protection enabled
- [ ] Audit logging implemented
- [ ] Cloudflare WAF enabled
- [ ] Cloudflare Rate Limiting configured
- [ ] DDoS protection enabled
- [ ] SSL/TLS set to "Full (Strict)"
- [ ] CORS restricted to known origins
- [ ] API keys tested and working
- [ ] All secrets removed from code
- [ ] All secrets stored in environment variables
- [ ] Backup strategy in place
- [ ] Monitoring/alerts configured

---

## 📚 Additional Resources

- **OWASP Top 10**: https://owasp.org/Top10/
- **Flask Security**: https://flask.palletsprojects.com/security/
- **Cloudflare Security**: https://www.cloudflare.com/security/
- **Password Hashing**: https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html
- **API Security**: https://cheatsheetseries.owasp.org/cheatsheets/REST_API_Security_Cheat_Sheet.html

---

**Remember: Security is not a one-time task, it's an ongoing process.** 🔒

Review logs regularly and update dependencies to patch vulnerabilities.
