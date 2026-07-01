# AES Monitor — Python Project Context

## What This Does
Connects to AES Scheduler on port 17471 using a custom encrypted protocol,
receives real-time tournament updates, invokes AESBridge.exe to parse them,
and POSTs structured data to the dashboard ingest API.

## Run
```
cd "C:\Git Repos\aes-connector\monitor"
python aes_monitor.py
```
Reads `aes_config.ini` automatically. Loads `.env` from this folder first,
then falls back to the parent directory (project root).

## Scripts
| File | Purpose |
|------|---------|
| aes_monitor.py | Production monitor — run this |
| aes_test.py | One-shot connection test — verifies AES is reachable and password works |
| aes_sync_probe.py | Protocol debugger — sends raw commands, logs all responses |

## Config

**aes_config.ini:**
```ini
[aes]
host       = 127.0.0.1
port       = 17471
password   = ${AES_PASSWORD}

[bridge]
exe = ..\bridge\bin\Release\net48\AESBridge.exe   # relative to monitor/

[dashboard]
endpoint   = ${DASHBOARD_ENDPOINT}   # base URL, e.g. https://host/api/ingest
ingest_key = ${INGEST_API_KEY}
timeout    = 10
```

**.env** (project root):
```
AES_PASSWORD=your_aes_password
DASHBOARD_ENDPOINT=https://your-dashboard.com/api/ingest
INGEST_API_KEY=your_shared_secret
```

---

## AES Protocol (Port 17471)

We connect TO AES (ClientInit). Full handshake detail is in the root CLAUDE.md.
Short version: RSA key exchange → XOR stream cipher → password auth → data stream.

**Dependencies:** `pycryptodome` (`pip install pycryptodome`) for RSA.

**Key classes/functions:**
| Symbol | Purpose |
|---|---|
| `CS` | Cipher state — holds key, IV, position |
| `xor(data, state)` | Applies the custom stream cipher in-place, advances state |
| `client_init(sock, password)` | Full handshake — returns an encrypted `Chan` for reading |
| `Chan` | Wrapper over socket + cipher state; `.rcmd()`, `.rint()`, `.rdata()` |
| `recv_n(sock, n)` | Blocking read of exactly n bytes |

**Message loop:** After handshake, each message is:
```
ncCommand (1 byte) + customCommand (4 bytes LE) + dataLen (3 bytes LE) + flags (1 byte) + payload
```
All bytes are decrypted via the inbound cipher state before parsing.

**Commands handled:**
| Command | Value | Action |
|---|---|---|
| `CMD_EVENT_UPDATE` | 16400 | Parse via AESBridge → throttled snapshot POST |
| `CMD_REMOTE_ENTRY_UPDATE` | 16640 | Decode via AESBridge --remote → immediate delta POST |
| 16896 / 16897 / 16898 / 17153 | auto-print heartbeats | silently ignored |

---

## AESBridge Invocation

```python
call_bridge(raw_bytes, bridge_exe)
# Writes raw bytes to monitor/scheduler_file.bin
# Runs AESBridge.exe <path>
# Reads monitor/tournament_data.json → returns dict or None
```

```python
decode_remote_entry(raw_bytes, bridge_exe)
# Writes raw bytes to monitor/remote_entry.bin
# Runs AESBridge.exe <path> --remote
# Returns parsed dict with "values": [...] or None
```

Runtime artifacts written to the monitor/ directory:
- `scheduler_file.bin` — last SchedulerFile binary received
- `remote_entry.bin` — last RemoteEntryUpdate binary received
- `tournament_data.json` — last parsed tournament state

---

## Dashboard Ingest

Two endpoints, both authenticated with `Authorization: Bearer <INGEST_API_KEY>`.
Base URL comes from `aes_config.ini [dashboard] endpoint`.

### Delta — POST {base_url}/delta
Fired on every `CMD_REMOTE_ENTRY_UPDATE` **that has set scores**. Two cases are suppressed/allowed:
- **Winner-only** (outcome set, sets empty) → suppressed; wait for the follow-up with scores
- **Undecided + sets empty** → passes through; signals a score was cleared in AES

Builds payload from:
1. The decoded remote entry (match ID, outcome code, set scores)
2. Match metadata looked up from `prev_data` (team names, courtId, court, times, division)

```python
push_delta(entry_obj, tournament_data, base_url, ingest_key, timeout)
```

### Snapshot — POST {base_url}/snapshot
Fired on first connect and then every `SNAPSHOT_INTERVAL = 180` seconds.
`last_snapshot` resets to `None` on each new TCP connection, guaranteeing a
snapshot is always sent on connect.

```python
push_snapshot(tournament_data, base_url, ingest_key, timeout)
```

Both functions spawn a daemon thread so they don't block the receive loop.

---

## Payload Builders

### `_match_payload(m)` → ingest match shape
Maps a `tournament_data.json` match dict:
- `courtId`: passed through from bridge output
- `sets`: `{"team1": x, "team2": y}` → `{"ft": x, "st": y}`
- `startTime`/`endTime`: UTC ISO 8601 → Eastern local, no offset (via `_eastern_naive`)
- `workTeam`: empty string → `None`
- `hasResult`: from `decided` bool

### `_strip_seed(name)` → bare team name
Strips trailing region/seed suffix ` (XX)` (1–3 uppercase letters) from team names.
Applied to pool standings. e.g. `"Sky High 17 Elite (GL)"` → `"Sky High 17 Elite"`.

### `_pool_payload(p)` → ingest pool shape
Maps a pool dict; computes per-team fields not in `tournament_data.json`:
- `courtId`: first court from `courts` array (required scalar by dashboard schema)
- `courtName`: first court name
- `courts`: full array passed through (pools can span multiple courts)
- `finishRank`: 1-indexed position in standings array
- `pointRatio`: `ptsFor / ptsAgainst`, or `None` if ptsAgainst == 0
- `goldSpotsCount`: always `None` (not yet available from AES assembly)
- team `name`: seed suffix stripped via `_strip_seed()`

### `_eastern_naive(iso_str)` → "YYYY-MM-DDTHH:MM:SS"
Converts UTC ISO 8601 string to Eastern local time without offset.
Uses `zoneinfo.ZoneInfo('America/New_York')` (Python 3.9+); falls back to
fixed -5h offset if zoneinfo is unavailable (no DST awareness in fallback).

---

## Reconnect Behavior
The main loop wraps the connection in a `while True` with a 5-second retry delay.
On each new connection:
- `last_snapshot` resets → snapshot fires on first `CMD_EVENT_UPDATE`
- `prev_data` persists across reconnects so the diff still works after a brief disconnect

---

## Console Output
```
══════════════════════════════════════════════════════════════
  UPDATE #3  14:23:07  —  1,510,603 bytes
──────────────────────────────────────────────────────────────
  Event:   2025 Midwest Qualifier
  Matches: 184 total  |  47 decided  |  137 pending

  ── Changes ──
  FINAL  [North 14] R1P1M3  Sky High vs COLAVOL  →  25-10, 25-12  (Sky High wins)
──────────────────────────────────────────────────────────────
  [14:23:07]  ENTRY  [North 14] R1P1M3  Sky High vs COLAVOL  →  25-10, 25-12  →  Sky High wins
```

---

## Outstanding / Known Gaps
- **goldSpotsCount** always `None` — see bridge/CLAUDE.md for investigation notes.
- **aesEventId** — connector sends numeric `eventId` integer. Dashboard `Event` table must
  store this numeric ID for ingest endpoint matching (web API string ID is not in the local binary).
