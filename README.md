# AES Sync Connector

Bridges AES Scheduler (EventScheduler.exe) to a live tournament director dashboard by connecting to its real-time sync protocol on port 17471. Delivers score updates in ~2 seconds instead of the 3-minute polling interval.

## How It Works

```
EventScheduler.exe (port 17471)
        │
        ▼
  aes_monitor.exe          ← Python — handles encrypted AES protocol
        │ subprocess
        ▼
  AESBridge.exe            ← C# — deserializes proprietary binary format
        │
        ├── score entered → POST /api/ingest/delta    (immediate)
        └── on connect / every 3 min → POST /api/ingest/snapshot
```

On connect, AES pushes a full tournament snapshot. Every subsequent score entry fires a delta within ~2 seconds. The monitor reconnects automatically if the connection drops.

## Requirements

- Windows (runs alongside AES Scheduler on the tournament laptop)
- Python 3.9+ on PATH
- .NET Framework 4.8 (pre-installed on Windows 10/11)
- AES Scheduler (EventScheduler.exe) — required at build time

## Build

```bat
build.bat
```

Builds everything in one step:
1. Compiles `AESBridge.exe` (.NET 4.8)
2. Bundles `aes_monitor.exe` with PyInstaller
3. Assembles `dist\` with all required files

On first build, `dist\aes_config.ini` is created from the template. Fill in your values before deploying.

> **Note:** Requires Python 3.9+ and .NET SDK on PATH. `build.bat` will look for `dotnet` on PATH first, then fall back to `C:\Program Files\dotnet\dotnet.exe`.

## Deploy

Copy the contents of `dist\` to the tournament laptop:

```
aes-sync\
├── aes_monitor.exe
├── AESBridge.exe
├── EventScheduler_Release.exe
└── aes_config.ini
```

Double-click `aes_monitor.exe` to run. No Python installation required on the target machine.

## Configuration

Edit `aes_config.ini` — no rebuild needed after changes, just restart the exe.

```ini
[aes]
host     = 127.0.0.1        ; AES Scheduler host (127.0.0.1 if running on same machine)
port     = 17471
password = your_aes_password

[bridge]
exe = AESBridge.exe         ; path relative to aes_monitor.exe

[dashboard]
endpoint   = https://your-dashboard.com/api/ingest
ingest_key = your_api_key
timeout    = 30

; Cloudflare Access service token (optional — only needed if tunnel is Access-protected)
cf_client_id     =
cf_client_secret =
```

## Cloudflare Tunnel Setup

If your dashboard is behind a Cloudflare tunnel, two settings are required in the Cloudflare dashboard:

1. **Security → Bots** — disable **Bot Fight Mode**
2. **Security → WAF → Custom Rules** — add a rule:
   - Match: `URI Path starts with /api/ingest/`
   - Action: Skip
   - Components to skip: **All managed rules** + **Browser Integrity Check**

The ingest endpoints are protected by the bearer token in `aes_config.ini`, so disabling Cloudflare bot checks on those paths is safe.

## Dashboard API

The monitor posts to two endpoints. Auth: `Authorization: Bearer <ingest_key>`.

| Endpoint | Trigger | Payload |
|---|---|---|
| `POST /api/ingest/delta` | Every score entry with set scores | Single match result |
| `POST /api/ingest/snapshot` | On connect, then every 3 minutes | Full tournament state |

See `CLAUDE.md` for full payload schemas.

## Development

```
aes-sync-connector\
├── monitor\
│   ├── aes_monitor.py       ← production monitor
│   ├── aes_test.py          ← one-shot connection test
│   ├── aes_sync_probe.py    ← protocol debugger
│   └── aes_config.ini       ← local dev config (not committed)
├── bridge\
│   ├── AESBridge.cs         ← C# bridge source
│   ├── AESBridge.csproj
│   └── EventScheduler.exe   ← AES dependency (required for build)
├── build.bat                ← builds dist\
└── requirements.txt
```

Run the monitor directly during development:

```bat
cd monitor
python aes_monitor.py
```

Use `aes_test.py` to verify AES connectivity before running the full monitor.
