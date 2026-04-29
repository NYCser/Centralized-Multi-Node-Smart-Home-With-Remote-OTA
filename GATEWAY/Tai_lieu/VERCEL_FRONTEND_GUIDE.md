# SmartHome Dashboard - Vercel Frontend Setup

## 📱 Overview

This guide helps you deploy a web frontend on Vercel that communicates with your Raspberry Pi API via Cloudflare Tunnel.

```
Vercel (Frontend)              Cloudflare Tunnel         Raspberry Pi (API)
smarthome.vercel.app   ←HTTPS→  api.yourdomain.com   ←Tunnel→  localhost:5000
  (React/Next.js)         fetch()    (DNS/Routing)        (Flask API)
```

---

## 🚀 Quick Start (5 mins)

### Option 1: Use Next.js Template (Recommended)

```bash
# Create Next.js project
npx create-next-app@latest smarthome-dashboard \
  --typescript \
  --tailwind \
  --eslint

cd smarthome-dashboard

# Install dependencies
npm install
# OR if using yarn:
yarn install
```

### Option 2: Use React Template

```bash
# Create React project
npx create-react-app smarthome-dashboard
cd smarthome-dashboard

# Install dependencies
npm install axios react-router-dom
```

---

## 🔧 Configuration

### 1. Environment Variables

**Create `.env.local` in project root:**

```bash
# API endpoint (from Cloudflare Tunnel)
NEXT_PUBLIC_API_URL=https://api.yourdomain.com
# or for Create React App:
REACT_APP_API_URL=https://api.yourdomain.com
```

**Create `.env.production` (for Vercel):**

```bash
NEXT_PUBLIC_API_URL=https://api.yourdomain.com
```

---

## 📦 API Service Layer

### TypeScript Version (Recommended)

**File: `src/services/api.ts`**

```typescript
interface RequestOptions {
  token?: string;
  headers?: Record<string, string>;
}

const API_BASE = process.env.NEXT_PUBLIC_API_URL || 
                 process.env.REACT_APP_API_URL ||
                 'https://api.yourdomain.com';

class ApiService {
  private getHeaders(token?: string) {
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
    };
    if (token) {
      headers['Authorization'] = `Bearer ${token}`;
    }
    return headers;
  }

  async get<T = any>(endpoint: string, options?: RequestOptions): Promise<T> {
    const response = await fetch(`${API_BASE}${endpoint}`, {
      method: 'GET',
      headers: {
        ...this.getHeaders(options?.token),
        ...options?.headers,
      },
      credentials: 'include', // For cookies if needed
    });

    if (!response.ok) {
      const error = await response.text();
      throw new Error(`API Error [${response.status}]: ${error}`);
    }

    return response.json() as Promise<T>;
  }

  async post<T = any>(
    endpoint: string,
    body: any,
    options?: RequestOptions
  ): Promise<T> {
    const response = await fetch(`${API_BASE}${endpoint}`, {
      method: 'POST',
      headers: {
        ...this.getHeaders(options?.token),
        ...options?.headers,
      },
      body: JSON.stringify(body),
      credentials: 'include',
    });

    if (!response.ok) {
      const error = await response.text();
      throw new Error(`API Error [${response.status}]: ${error}`);
    }

    return response.json() as Promise<T>;
  }

  async put<T = any>(
    endpoint: string,
    body: any,
    options?: RequestOptions
  ): Promise<T> {
    const response = await fetch(`${API_BASE}${endpoint}`, {
      method: 'PUT',
      headers: {
        ...this.getHeaders(options?.token),
        ...options?.headers,
      },
      body: JSON.stringify(body),
      credentials: 'include',
    });

    if (!response.ok) {
      const error = await response.text();
      throw new Error(`API Error [${response.status}]: ${error}`);
    }

    return response.json() as Promise<T>;
  }

  async delete<T = any>(endpoint: string, options?: RequestOptions): Promise<T> {
    const response = await fetch(`${API_BASE}${endpoint}`, {
      method: 'DELETE',
      headers: {
        ...this.getHeaders(options?.token),
        ...options?.headers,
      },
      credentials: 'include',
    });

    if (!response.ok) {
      const error = await response.text();
      throw new Error(`API Error [${response.status}]: ${error}`);
    }

    return response.json() as Promise<T>;
  }
}

export const api = new ApiService();

// ═══════════════════════════════════════════════════════════════════
// USAGE EXAMPLES
// ═══════════════════════════════════════════════════════════════════

// Login
export async function login(email: string, password: string) {
  return api.post('/auth/login', { email, password });
}

// Get user profile
export async function getProfile(token: string) {
  return api.get('/users/me', { token });
}

// Get devices
export async function getDevices(token: string) {
  return api.get('/devices', { token });
}

// Control device
export async function controlDevice(
  deviceId: string,
  action: string,
  token: string
) {
  return api.post(`/devices/${deviceId}/control`, { action }, { token });
}
```

### JavaScript Version

**File: `src/services/api.js`**

```javascript
const API_BASE = process.env.REACT_APP_API_URL || 'https://api.yourdomain.com';

export const api = {
  async get(endpoint, token) {
    const response = await fetch(`${API_BASE}${endpoint}`, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    });
    if (!response.ok) throw new Error(`API Error: ${response.status}`);
    return response.json();
  },

  async post(endpoint, body, token) {
    const response = await fetch(`${API_BASE}${endpoint}`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(token && { Authorization: `Bearer ${token}` }),
      },
      body: JSON.stringify(body),
    });
    if (!response.ok) throw new Error(`API Error: ${response.status}`);
    return response.json();
  },
};

// Usage:
// const result = await api.post('/auth/login', { email, password });
```

---

## 🔌 Real-time Updates (Socket.IO)

### Setup Socket.IO Client

**File: `src/hooks/useSocket.ts`**

```typescript
'use client'; // For Next.js App Router

import { useEffect, useRef, useCallback } from 'react';
import io, { Socket } from 'socket.io-client';

const SOCKET_URL = process.env.NEXT_PUBLIC_API_URL || 'https://api.yourdomain.com';

export function useSocket(token?: string) {
  const socketRef = useRef<Socket | null>(null);

  useEffect(() => {
    if (!token) return;

    socketRef.current = io(SOCKET_URL, {
      auth: { token },
      transports: ['websocket', 'polling'],
      reconnection: true,
      reconnectionDelay: 1000,
      reconnectionAttempts: 10,
    });

    socketRef.current.on('connect', () => {
      console.log('Connected to API');
    });

    socketRef.current.on('disconnect', () => {
      console.log('Disconnected from API');
    });

    socketRef.current.on('error', (error) => {
      console.error('Socket error:', error);
    });

    return () => {
      socketRef.current?.disconnect();
    };
  }, [token]);

  const emit = useCallback(
    (event: string, data: any) => {
      socketRef.current?.emit(event, data);
    },
    []
  );

  const on = useCallback(
    (event: string, callback: (data: any) => void) => {
      socketRef.current?.on(event, callback);
    },
    []
  );

  return { socket: socketRef.current, emit, on };
}
```

**Usage in Component:**

```typescript
'use client';

import { useSocket } from '@/hooks/useSocket';
import { useEffect, useState } from 'react';

export function SensorDashboard({ token }: { token: string }) {
  const { on } = useSocket(token);
  const [sensorData, setSensorData] = useState<any>({});

  useEffect(() => {
    on('sensor_update', (data) => {
      setSensorData(data);
    });
  }, [on]);

  return (
    <div>
      <h1>Sensor Data</h1>
      <pre>{JSON.stringify(sensorData, null, 2)}</pre>
    </div>
  );
}
```

---

## 🎨 Example Components

### Login Component (Next.js)

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
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const handleLogin = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError('');

    try {
      const { token, user } = await api.post('/auth/login', {
        email,
        password,
      });

      // Store token in localStorage
      localStorage.setItem('authToken', token);
      localStorage.setItem('user', JSON.stringify(user));

      // Redirect to dashboard
      router.push('/dashboard');
    } catch (err: any) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen flex items-center justify-center bg-gray-100">
      <div className="bg-white p-8 rounded shadow-lg w-96">
        <h1 className="text-2xl font-bold mb-6">SmartHome Login</h1>

        {error && <div className="text-red-600 mb-4">{error}</div>}

        <form onSubmit={handleLogin}>
          <input
            type="email"
            placeholder="Email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className="w-full p-2 mb-4 border rounded"
            required
          />
          <input
            type="password"
            placeholder="Password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="w-full p-2 mb-4 border rounded"
            required
          />
          <button
            type="submit"
            disabled={loading}
            className="w-full bg-blue-600 text-white p-2 rounded font-bold hover:bg-blue-700 disabled:opacity-50"
          >
            {loading ? 'Logging in...' : 'Login'}
          </button>
        </form>
      </div>
    </div>
  );
}
```

### Device List Component

**File: `src/app/dashboard/page.tsx`**

```typescript
'use client';

import { useEffect, useState } from 'react';
import { api } from '@/services/api';
import { useSocket } from '@/hooks/useSocket';

interface Device {
  id: string;
  name: string;
  type: string;
  status: 'on' | 'off';
}

export default function DashboardPage() {
  const token = typeof window !== 'undefined' ? localStorage.getItem('authToken') : null;
  const [devices, setDevices] = useState<Device[]>([]);
  const [loading, setLoading] = useState(true);

  const { on } = useSocket(token || undefined);

  useEffect(() => {
    if (!token) return;

    // Load devices
    api.get('/devices', { token })
      .then(setDevices)
      .catch(console.error)
      .finally(() => setLoading(false));

    // Listen for real-time updates
    on('device_update', (updatedDevice) => {
      setDevices((prev) =>
        prev.map((d) => (d.id === updatedDevice.id ? updatedDevice : d))
      );
    });
  }, [token, on]);

  const handleToggle = async (deviceId: string) => {
    if (!token) return;
    try {
      await api.post(`/devices/${deviceId}/toggle`, {}, { token });
    } catch (error) {
      console.error('Failed to toggle device:', error);
    }
  };

  if (loading) return <div className="p-8">Loading...</div>;

  return (
    <div className="p-8">
      <h1 className="text-3xl font-bold mb-6">SmartHome Dashboard</h1>

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        {devices.map((device) => (
          <div
            key={device.id}
            className="border rounded p-4 shadow hover:shadow-lg transition"
          >
            <h2 className="text-xl font-bold">{device.name}</h2>
            <p className="text-gray-600">Type: {device.type}</p>
            <p className="text-lg mb-4">
              Status: <span className={device.status === 'on' ? 'text-green-600' : 'text-gray-600'}>
                {device.status}
              </span>
            </p>
            <button
              onClick={() => handleToggle(device.id)}
              className="w-full bg-blue-600 text-white p-2 rounded hover:bg-blue-700"
            >
              Toggle
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}
```

---

## 🚀 Deploy to Vercel

### Step 1: Push Code to GitHub

```bash
git init
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/yourusername/smarthome-dashboard.git
git branch -M main
git push -u origin main
```

### Step 2: Deploy on Vercel

#### Option A: Web Dashboard (Easiest)

1. Go to https://vercel.com/new
2. Connect GitHub account
3. Select `smarthome-dashboard` repository
4. Set environment variables:
   - Key: `NEXT_PUBLIC_API_URL`
   - Value: `https://api.yourdomain.com`
5. Click "Deploy"

#### Option B: CLI

```bash
# Install Vercel CLI
npm i -g vercel

# Deploy
vercel
# → Follow prompts
# → Set environment variable: NEXT_PUBLIC_API_URL=https://api.yourdomain.com
```

### Step 3: Verify Deployment

```bash
# Visit your deployed app
open https://smarthome-dashboard.vercel.app

# Or your custom domain if configured
open https://smarthome.yourdomain.com
```

---

## 🔐 Security Best Practices

### 1. Store Tokens Securely

❌ **Bad:**
```typescript
// Storing in localStorage exposes to XSS
localStorage.setItem('authToken', token);
```

✅ **Better:**
```typescript
// Use httpOnly cookies (requires backend to set)
// Frontend: doesn't access token directly
// Backend: automatically sends token with requests
```

**Backend fix (Flask):**
```python
from flask import make_response

response = make_response(jsonify({'user': user_data}))
response.set_cookie(
    'authToken',
    token,
    httpOnly=True,      # Not accessible from JS
    secure=True,        # Only HTTPS
    sameSite='Strict',  # CSRF protection
    max_age=86400       # 24 hours
)
return response
```

### 2. CORS Headers

Your Flask API should restrict to frontend domain:

```python
CORS(app, 
     origins=['https://smarthome.vercel.app', 'https://smarthome.yourdomain.com'],
     supports_credentials=True
)
```

### 3. Environment Variables

Never commit secrets:

```bash
# .env.local (never commit)
NEXT_PUBLIC_API_URL=https://api.yourdomain.com

# .env.production (in Vercel dashboard, not git)
# Set same variables in Vercel project settings
```

### 4. Rate Limiting

Add on frontend:

```typescript
class RateLimiter {
  private requests: Record<string, number[]> = {};

  check(key: string, limit: number, window: number): boolean {
    const now = Date.now();
    this.requests[key] ??= [];
    this.requests[key] = this.requests[key].filter(t => now - t < window);
    
    if (this.requests[key].length >= limit) return false;
    
    this.requests[key].push(now);
    return true;
  }
}

const limiter = new RateLimiter();

async function apiCall(endpoint: string) {
  if (!limiter.check('api', 10, 1000)) {
    throw new Error('Rate limited');
  }
  return api.get(endpoint, { token });
}
```

---

## 🧪 Testing

### Test API Connectivity

```bash
# From your frontend project
npm run dev

# In browser console:
fetch('https://api.yourdomain.com/health')
  .then(r => r.json())
  .then(console.log)
  .catch(console.error)
```

### Test Authentication Flow

```typescript
// In browser console:
const login = async () => {
  const res = await fetch('https://api.yourdomain.com/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email: 'test@example.com', password: 'password' })
  });
  const data = await res.json();
  console.log(data);
};
login();
```

### Check CORS Headers

```bash
curl -i https://api.yourdomain.com/health
# Look for: Access-Control-Allow-Origin: *
```

---

## 📚 Troubleshooting

### "Failed to fetch from API"

**Check:**
1. Is API running? `curl http://localhost:5000/health`
2. Is tunnel connected? `sudo systemctl status cloudflared`
3. Is domain configured? `nslookup api.yourdomain.com`
4. Are CORS headers present? Check browser DevTools Network tab

### "Vercel build fails"

```bash
# Run local build to debug
npm run build

# Check for:
# - Missing environment variables
# - TypeScript errors: npm run type-check
# - Import errors: npm ls
```

### "WebSocket not connecting"

```typescript
// Check in browser console:
// Should see messages like:
// "Connected to API"
// "Socket connected successfully"

// If not, verify:
// 1. Socket.IO endpoint is correct
// 2. Auth token is valid
// 3. Network tab shows WebSocket handshake
```

---

## ✅ Deployment Checklist

- [ ] `.env.local` has `NEXT_PUBLIC_API_URL=https://api.yourdomain.com`
- [ ] API service layer created and tested
- [ ] Login component works
- [ ] Vercel environment variables set
- [ ] GitHub repository connected to Vercel
- [ ] Initial deploy successful
- [ ] Test API calls from deployed frontend
- [ ] Socket.IO real-time updates working
- [ ] CORS errors fixed
- [ ] No console errors
- [ ] Response times acceptable

---

## 📖 Resources

- Next.js Docs: https://nextjs.org/docs
- React Docs: https://react.dev
- Socket.IO Client: https://socket.io/docs/v4/client-initialization/
- Vercel Docs: https://vercel.com/docs
- Environment Variables: https://vercel.com/docs/projects/environment-variables

---

Happy deploying! 🎉
