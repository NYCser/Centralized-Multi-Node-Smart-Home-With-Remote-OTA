# System Architecture Diagrams

## 1. Overall System Architecture

```mermaid
graph TB
    subgraph "Web Frontend (Browser)"
        W1[Login Page]
        W2[Dashboard Pages]
        W3[Admin Panel]
        W4[Settings]
        W5[API Service]
    end

    subgraph "Cloudflare Tunnel"
        CF[nhathongminh.crfnetwork.cyou]
    end

    subgraph "Raspberry Pi Gateway"
        subgraph "Flask API Server (Port 5000)"
            API[REST APIs]
            WS[WebSocket Ready]
        end

        subgraph "Message Bus"
            MB[MQTT ↔ Redis Bridge]
        end

        subgraph "Background Workers"
            SW[Safety Watchdog]
            AE[Automation Engine]
            DS[Data Syncer]
            NW[Network Watchdog]
        end

        subgraph "External Services"
            MQTT[Mosquitto Broker:1883]
            REDIS[Redis Server:6379]
            DB[(SQLite Database)]
            SD2[(SD2 Storage)]
        end
    end

    subgraph "ESP32 Edge Nodes"
        subgraph "Bedroom Node"
            B_S[Temp/Hum/CO2 Sensors]
            B_R[Fan/Light Relays]
            B_B[Physical Buttons]
        end

        subgraph "Kitchen Node"
            K_S[Gas/Temp Sensors]
            K_R[Fan/Light/Gas Valve]
            K_A[Buzzer Alarm]
        end

        subgraph "Living Room Node"
            L_S[Temp/Hum Sensors]
            L_R[Fan/Light Relays]
            L_D[LCD Display]
        end
    end

    W5 --> CF
    CF --> API
    API --> MB
    MB --> SW
    MB --> AE
    MB --> DS
    MB --> NW
    MB --> MQTT
    MB --> REDIS
    DS --> DB
    DS --> SD2
    MQTT --> B_S
    MQTT --> B_R
    MQTT --> K_S
    MQTT --> K_R
    MQTT --> L_S
    MQTT --> L_R

    style CF fill:#e1f5fe
    style API fill:#f3e5f5
    style MB fill:#fff3e0
    style SW fill:#ffebee
    style MQTT fill:#e8f5e8
    style REDIS fill:#e8f5e8
    style DB fill:#e8f5e8
```

## 2. Data Flow Architecture

```mermaid
graph LR
    subgraph "Data Sources"
        ESP32[ESP32 Sensors]
        WEB[Web UI Controls]
        AUTO[Automation Rules]
        SCHED[Time Schedules]
    end

    subgraph "Ingestion Layer"
        MQTT_IN[MQTT Inbound]
        API_IN[API Requests]
        TIMER[Scheduler Timer]
    end

    subgraph "Processing Layer"
        BUS[Message Bus]
        WORKERS[Background Workers]
        VALIDATE[Validation & Auth]
    end

    subgraph "Storage Layer"
        REDIS[(Redis Cache)]
        SQLITE[(SQLite DB)]
        SD2[(SD2 Backup)]
    end

    subgraph "Output Layer"
        MQTT_OUT[MQTT Commands]
        API_OUT[API Responses]
        REALTIME[Real-time Updates]
    end

    ESP32 --> MQTT_IN
    WEB --> API_IN
    AUTO --> TIMER
    SCHED --> TIMER

    MQTT_IN --> BUS
    API_IN --> VALIDATE
    TIMER --> WORKERS

    BUS --> WORKERS
    VALIDATE --> WORKERS

    WORKERS --> REDIS
    WORKERS --> SQLITE
    WORKERS --> SD2

    WORKERS --> MQTT_OUT
    WORKERS --> API_OUT
    WORKERS --> REALTIME

    style BUS fill:#fff3e0
    style WORKERS fill:#e3f2fd
    style REDIS fill:#f3e5f5
    style SQLITE fill:#e8f5e8
```

## 3. Safety System Flow

```mermaid
stateDiagram-v2
    [*] --> Monitoring

    state Monitoring as "Continuous Monitoring"
    state Alert as "Danger Detected"
    state Lock as "Safety Lock Active"
    state Mute as "User Muted"
    state Normal as "Normal Operation"

    Monitoring --> Alert: Gas > threshold\nOR Fire detected
    Alert --> Lock: Set safety lock
    Lock --> Hardware: Turn on fan\nSound alarm
    Hardware --> Alert: Send alert to DB
    Alert --> Web: Push notification
    Web --> User: Show red alert
    User --> Mute: User clicks mute
    Mute --> Timer: 10 min countdown
    Timer --> Check: Still dangerous?
    Check --> Alert: Yes → Re-alert
    Check --> Normal: No → Clear lock
    Normal --> Monitoring

    note right of Lock
        All manual controls
        are blocked
    end note

    note right of Mute
        Alarm silenced but
        monitoring continues
    end note
```

## 4. Device Control Flow

```mermaid
sequenceDiagram
    participant U as User
    participant W as Web UI
    participant A as API Server
    participant R as Redis
    participant M as Message Bus
    participant E as ESP32

    U->>W: Click device button
    W->>A: POST /control
    A->>A: Validate auth token
    A->>A: Check safety lock
    alt Safety locked
        A-->>W: Error: Safety lock active
    else Not locked
        A->>R: Publish device_commands
        R->>M: Command received
        M->>M: Check automation conflicts
        M->>M: Update manual override cache
        M->>E: MQTT publish command
        E->>E: Execute hardware control
        E->>M: Send status update
        M->>R: Publish realtime update
        R->>A: Update via polling
        A-->>W: Success response
        W->>U: Update UI state
    end
```

## 5. Sensor Data Pipeline

```mermaid
flowchart TD
    A[ESP32 Sensors] --> B{Connected?}
    B -->|Yes| C[Send MQTT]
    B -->|No| D[Buffer in RAM]
    D --> E{Buffer full?}
    E -->|Yes| F[Rotate buffer]
    E -->|No| G[Continue buffering]

    C --> H[Message Bus]
    H --> I[Redis pubsub]
    I --> J[Data Syncer]
    J --> K{Batch ready?}
    K -->|No| L[Accumulate]
    K -->|Yes| M[SQLite Insert]
    M --> N[Commit transaction]
    N --> O[Export to SD2]
    O --> P[Daily partition]

    D --> Q{Reconnected?}
    Q -->|Yes| R[Flush buffer]
    R --> C

    style A fill:#e3f2fd
    style J fill:#fff3e0
    style M fill:#e8f5e8
    style O fill:#f3e5f5
```

## 6. Authentication Flow

```mermaid
sequenceDiagram
    participant U as User
    participant W as Web UI
    participant A as API Server
    participant R as Redis
    participant DB as SQLite

    U->>W: Enter credentials
    W->>A: POST /auth/login
    A->>DB: Query user by email
    DB-->>A: User data + hash
    A->>A: Verify password hash
    alt Invalid
        A->>DB: Log failed attempt
        A-->>W: Error response
    else Valid
        A->>A: Generate session token
        A->>DB: Insert session
        A->>R: Cache session (24h)
        A-->>W: Token + user data
        W->>W: Store token in localStorage
        W-->>U: Redirect to dashboard
    end

    Note over W,A: Subsequent requests include Bearer token
```

## 7. Automation Engine Flow

```mermaid
flowchart TD
    A[Sensor Data] --> B[Automation Engine]
    B --> C{Enabled rules?}
    C -->|No| D[Skip]
    C -->|Yes| E[Check thresholds]

    E --> F{Temperature > fan_threshold?}
    F -->|Yes| G[Send fan ON]
    F -->|No| H{Temperature < fan_threshold - 2?}
    H -->|Yes| I[Send fan OFF]

    E --> J{Gas > gas_threshold?}
    J -->|Yes| K[Safety Watchdog]
    K --> L[Trigger alarm]

    E --> M{Current time matches schedule?}
    M -->|Yes| N[Execute scheduled action]

    G --> O[Log automation]
    I --> O
    N --> O

    O --> P[Publish realtime update]
    P --> Q[Web UI updates]

    style B fill:#e3f2fd
    style K fill:#ffebee
    style O fill:#fff3e0
```

## 8. Database Schema Relationships

```mermaid
erDiagram
    users ||--o{ sessions : has
    users ||--o{ login_logs : generates
    users ||--o{ ota_logs : triggers

    rooms ||--o{ devices : contains
    rooms ||--o{ sensor_data : monitors
    rooms ||--o{ device_status : controls
    rooms ||--o{ system_alerts : alerts
    rooms ||--o{ notifications : notifies
    rooms ||--o{ access_logs : logs
    rooms ||--o{ automation_logs : automates
    rooms ||--o{ automations : rules
    rooms ||--o{ schedules : schedules

    devices ||--o{ device_status : status
    devices ||--o{ schedules : scheduled

    automations ||--o{ automation_logs : logs

    rfid_cards ||--o{ access_logs : used

    system_alerts ||--o{ notifications : creates

    style users fill:#e3f2fd
    style rooms fill:#f3e5f5
    style devices fill:#fff3e0
```

## 9. API Architecture

```mermaid
graph TB
    subgraph "API Blueprints"
        AUTH[auth_bp<br/>/auth/*]
        SENSORS[sensors_bp<br/>/sensors/*]
        DEVICES[devices_bp<br/>/devices/*]
        AUTO[automation_bp<br/>/automation/*]
        LOGS[logs_bp<br/>/logs/*]
        RFID[rfid_bp<br/>/rfid/*]
        WIFI[wifi_bp<br/>/wifi/*]
        OTA[ota_bp<br/>/ota/*]
        SYSTEM[system_bp<br/>/system/*]
    end

    subgraph "Middleware"
        CORS[CORS Headers]
        AUTH_M[Auth Decorator]
        ADMIN_M[Admin Decorator]
    end

    subgraph "Data Access"
        REDIS[(Redis Cache)]
        SQLITE[(SQLite DB)]
        MQTT[MQTT Client]
    end

    subgraph "External"
        WEB[Web Frontend]
        ESP32[ESP32 Nodes]
        CF[Cloudflare]
    end

    WEB --> CF
    CF --> CORS
    ESP32 --> MQTT

    CORS --> AUTH_M
    CORS --> ADMIN_M

    AUTH_M --> AUTH
    AUTH_M --> SENSORS
    AUTH_M --> DEVICES
    AUTH_M --> AUTO
    AUTH_M --> LOGS
    AUTH_M --> RFID
    AUTH_M --> WIFI
    AUTH_M --> OTA

    ADMIN_M --> OTA

    AUTH --> REDIS
    SENSORS --> SQLITE
    DEVICES --> REDIS
    AUTO --> SQLITE
    LOGS --> SQLITE
    RFID --> SQLITE
    WIFI --> MQTT
    OTA --> MQTT
    SYSTEM --> SQLITE

    style AUTH fill:#e3f2fd
    style CORS fill:#fff3e0
    style REDIS fill:#f3e5f5
```

## 10. Deployment Architecture

```mermaid
graph TB
    subgraph "Development"
        DEV[Developer Machine]
        GIT[Git Repository]
        PIO[PlatformIO]
    end

    subgraph "Raspberry Pi (Production)"
        RP[Raspberry Pi 4B]
        GW[Gateway Software]
        CF_T[Cloudflare Tunnel]
        HOTSPOT[WiFi Hotspot]
    end

    subgraph "ESP32 Nodes"
        ESP_B[Bedroom ESP32]
        ESP_K[Kitchen ESP32]
        ESP_L[Living Room ESP32]
    end

    subgraph "Cloud Services"
        CF_C[Cloudflare Edge]
        DNS[DNS: nhathongminh.crfnetwork.cyou]
    end

    subgraph "User Access"
        BROWSER[Web Browser]
        MOBILE[Mobile Device]
    end

    DEV --> GIT
    GIT --> RP
    PIO --> ESP_B
    PIO --> ESP_K
    PIO --> ESP_L

    GW --> CF_T
    GW --> HOTSPOT

    HOTSPOT --> ESP_B
    HOTSPOT --> ESP_K
    HOTSPOT --> ESP_L

    CF_T --> CF_C
    CF_C --> DNS

    DNS --> BROWSER
    DNS --> MOBILE

    style RP fill:#e3f2fd
    style CF_C fill:#e1f5fe
    style HOTSPOT fill:#fff3e0
```</content>
<parameter name="filePath">/home/pi/smarthome_prj/SYSTEM_DIAGRAMS.md