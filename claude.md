# AES Connector — Project Context
# Read this file at the start of any Claude Code session on this project.

## What This Project Does
Reverse-engineered the AES Scheduler (EventScheduler.exe v1.1.0.5090) sync
protocol to give Adam's director dashboard real-time tournament data, replacing
a 3-minute polling interval with ~2-second live updates.

## Repo Location
C:\Git Repos\aes-connector

## Folder Structure
```
aes-connector/
├── bridge/               ← C# project (AESBridge)
│   ├── AESBridge.cs
│   ├── AESBridge.csproj
│   ├── EventScheduler.exe    ← AES dependency (required for build)
│   ├── bin/
│   └── obj/
├── monitor/              ← Python scripts + config
│   ├── aes_monitor.py        ← production monitor
│   ├── aes_test.py           ← one-shot connection test
│   ├── aes_sync_probe.py     ← protocol debugger
│   └── aes_config.ini        ← config (passwords/endpoints via .env)
└── CLAUDE.md
```

## Files
| File | Purpose |
|------|---------|
| monitor/aes_monitor.py | Production monitor — connects to AES, calls AESBridge, POSTs to dashboard |
| monitor/aes_test.py | One-shot connection test — run first to verify AES is reachable |
| monitor/aes_sync_probe.py | Protocol debugger — try commands, log everything |
| monitor/aes_config.ini | Config: AES host/port/password, bridge path, dashboard endpoint/token |
| bridge/AESBridge.cs | C# bridge — deserializes SchedulerFile binary, outputs tournament_data.json |
| bridge/AESBridge.csproj | .NET project (net48, x86) — build with: dotnet build -c Release |
| aes-connector.js | Node.js module — attach to Express app to receive POSTs + broadcast via WS |
| aes-client.js | Browser-side WebSocket client |

## Build
```
cd "C:\Git Repos\aes-connector\bridge"
dotnet build AESBridge.csproj -c Release
# Output: bridge\bin\Release\net48\AESBridge.exe
# Requires: EventScheduler.exe in bridge\ folder
```

## Run
```
cd "C:\Git Repos\aes-connector\monitor"
python aes_monitor.py
# Reads aes_config.ini automatically
```

---

## Protocol — Port 17471 (Sync, USE THIS ONE)

**Architecture:** We connect TO AES (ClientInit). No registration needed —
AES pushes data immediately on connect and on every score change.

**Handshake sequence:**
1. We send 0xA0 (BeginConnectionV1)
2. AES echoes 0xA0
3. AES sends 0x11 + RSA-2048 public key XML
4. We send 0x11 + our RSA-2048 public key XML
5. We send 0x12 + RSA-encrypt(our IV, AES pubkey) + RSA-encrypt(our Key, AES pubkey)
6. AES sends 0x12 + RSA-encrypt(their IV, our pubkey) + RSA-encrypt(their Key, our pubkey)
7. Set up stream cipher (see below)
8. AES sends 0x13 (RequestPassword) — encrypted
9. We send 0x14 + password bytes — encrypted
10. AES sends 0x15 (PasswordValid) + 0xFE (ConnectionInitialized) — encrypted
11. We send 0xFE (ConnectionInitialized) — encrypted
12. Done — AES immediately pushes EventUpdateAttached

**Stream cipher (custom, not AES-the-algorithm):**
- Key: 64 random bytes, IV: 32 random bytes
- XOR each byte with IV[pos], pos++
- When pos reaches 32: IV = SHA256(IV + Key), pos = 0
- Each direction uses its own state (ours for reading, theirs for writing)

**Wire frame format (after handshake, all encrypted):**
```
[1 byte]  NCCommand  (0x21=NoData, 0x22=Binary, 0x23=Object, 0x24=String)
[4 bytes] CustomCommand LE  (SchedulerNetCommand enum value)
[3 bytes] Data length LE
[1 byte]  Flags  (0x01 = gzip compressed)
[N bytes] Payload
```

**Key SchedulerNetCommand values:**
```
EventUpdateAttached      = 16400   // Full SchedulerFile — parse with AESBridge
RemoteEntryUpdateAttached= 16640   // Score delta — String[] via BinaryFormatter
FinishedPlaysAttached    = 16896   // Auto-print heartbeat — ignore
PrintableMatchesAttached = 16897   // Auto-print heartbeat — ignore
PrintablePlaysAttached   = 16898   // Auto-print heartbeat — ignore
AutoPrintMatchesAttached = 17153   // Auto-print heartbeat — ignore
PlasmaDisplayModeSelected= 12288   // Only needed for display protocol (port 19211)
```

**Timing:**
- Score entered in main AES UI → EventUpdateAttached within ~2 seconds
- Also on 2-minute heartbeat timer
- RemoteEntryUpdateAttached arrives alongside EventUpdate (within ~2s of score entry)
- Auto-print batch (16896/16897/16898/17153) fires every ~60 seconds — ignore

---

## Protocol — Port 19211 (Display, DON'T USE)
- AES connects TO US (ServerInit) after seeing our UDP beacon on port 19212
- Hardcoded password: "Display Protocol v3.0"
- Only pushes every ~2 minutes — not suitable for real-time dashboard

---

## RemoteEntryUpdateAttached Payload Format
BinaryFormatter-serialized String[] — decoded via AESBridge.exe --remote flag:
```
[0] File GUID          e.g. "10b81d5d-989d-4500-964c-699e9ecba383"
[1] Event ID           e.g. "33281"
[2] Match ID           e.g. "-51376"  (negative int = manually added match)
[3] OutcomeType        "1"=FirstTeamWon, "2"=SecondTeamWon, "3"=Tie,
                       "4"=FirstTeamForfeit, "5"=SecondTeamForfeit
[4] Match type         "1"=BestOf
[5] Max set count      e.g. "5"
[6+] Set score pairs   team1score, team2score per set played
```
Example: [guid, 33281, -51376, 1, 1, 5, 25, 10, 25, 12] = team1 won 25-10 25-12

---

## SchedulerFile Binary Format
Custom BinaryWriter format (NOT BinaryFormatter). Magic: 660209715 (0x2754AFC3).
AESBridge.exe calls SchedulerFile.Load(bytes) from EventScheduler_Release.exe
and outputs tournament_data.json.

**Key property names in EventScheduler assembly:**
- `m.FirstTeamText` / `m.SecondTeamText` — formatted team name with seed
- `m.ScoreText` — "25-20, 25-18" formatted string
- `m.Sets` — Match.Set[] where set.FirstTeamScore / set.SecondTeamScore are nullable int
- `m.ScheduledCourtText` — "Court 1" or "No Court"
- `m.CompleteShortName` / `m.CompleteFullName`
- `m.TypeOfOutcome` — Match.OutcomeType enum
- `m.ScheduledEndDateTime` — computed from start + MatchLength
- `m.IsScheduled` — bool
- `div.CodeAlias` / `div.DescriptionAlias` — division code and name
- `bracket.IsPlayoff` — bool
- `bracket.Notes` — string
- `bracket.PlotMatchPositions()` — returns List<MatchPlacement> with X/Y layout
- `ScheduledCourtID` is internal — use reflection or match ScheduledCourtText

**IMPORTANT — C# string escaping bug:**
When editing AESBridge.cs with Python scripts, escaped quotes inside
C# interpolated strings frequently get corrupted.
Always run `dotnet build` after edits to catch syntax errors early.

---

## tournament_data.json Schema
```json
{
  "event": { "name", "eventId", "startDate", "endDate", "lastUpdated" },
  "courts": [{ "courtId", "name" }],
  "matches": [{
    "matchId", "courtId", "courtName", "startTime", "endTime", "matchLength",
    "team1", "team2", "divisionCode", "divisionName", "playName",
    "playType",     // "pool" | "bracket" | "playoff"
    "outcome",      // "Undecided" | "FirstTeamWon" | "SecondTeamWon" | etc.
    "decided",      // bool
    "firstTeamWon", "secondTeamWon",  // bool
    "scoreText",    // "25-20, 25-18"
    "sets": [{ "team1", "team2" }],   // only played sets
    "shortName", "fullName"
  }],
  "pools": [{
    "poolId", "name", "divisionCode", "divisionName",
    "standings": [{ "team", "wins", "losses", "setsWon", "setsLost", "ptsFor", "ptsAgainst" }]
  }],
  "brackets": [{
    "bracketId", "name", "shortName", "fullName",
    "divisionCode", "divisionName", "isPlayoff", "notes",
    "matchCount", "decided",
    "roots": [{
      "x", "y",           // float — layout coordinates from PlotMatchPositions()
      "reversed",         // bool
      "doubleCapped",     // bool — true for leaf nodes (first-round matches)
      "match": {
        "matchId", "shortName", "fullName", "team1", "team2",
        "courtId", "courtName", "startTime", "endTime",
        "outcome", "decided", "firstTeamWon", "secondTeamWon",
        "scoreText", "sets": [{ "team1", "team2" }]  // ALL set slots incl. unplayed (null)
      },
      "topSource":    { ...recursive, same structure },
      "bottomSource": { ...recursive, same structure }
    }]
  }]
}
```

---

## Production Deployment Architecture
```
Windows laptop (runs AES Scheduler)        Dashboard server (Node.js)
────────────────────────────────────       ──────────────────────────
EventScheduler.exe                         server.js
  │ port 17471                               const { attachAES } = require('./aes-connector')
  │                                          attachAES(app, server)
aes_monitor.py  ←── aes_config.ini          │
  │ subprocess                               ├── POST /api/aes-update  ← receives from monitor
  ▼                                          ├── GET  /api/aes-data    ← current state
AESBridge.exe                               ├── GET  /api/aes-status  ← health check
  │ writes                                   └── WS   /aes-live        ← browser clients
  ▼
tournament_data.json
  │ HTTP POST → X-AES-Token header
  └──────────────────────────────────────►  /api/aes-update
                                            stores in memory, broadcasts via WebSocket
                                                     │
                                            Browser clients (aes-client.js)
                                            window.AES.getMatches() etc.
```

**aes_config.ini:**
```ini
[aes]
host     = 127.0.0.1
port     = 17471
password = your_password_here

[bridge]
exe = bin\Release\net48\AESBridge.exe

[dashboard]
endpoint = https://your-dashboard.com/api/aes-update
token    = your_shared_secret
timeout  = 10
```

**Dashboard integration (2 lines):**
```javascript
const { attachAES } = require('./aes-connector')
attachAES(app, server)   // app = Express app, server = http.Server
```
Set env var: `AES_TOKEN=your_shared_secret` (must match aes_config.ini token)

---

## Outstanding Issues
1. **EventUpdate diff shows "no score changes"** even when scores change.
   aes_monitor.py calls AESBridge, reads tournament_data.json, diffs vs prev_data.
   Suspect: path mismatch between where bridge writes JSON and where monitor reads it,
   or prev_data being set to None after a failed bridge call.
   The RemoteEntryUpdate decoder DOES work correctly and shows score changes immediately.

2. **aes_connector.py is outdated** — still uses port 19211 display protocol.
   Should be rewritten to match aes_monitor.py (port 17471, ClientInit, config file).

## Context
- Adam: tournament director, 20yr experience, ~150 events/yr, Midwest-focused
- Dashboard: Node.js/Express, director-facing operational tool
- No tablets used — only main AES UI for score entry
- Future: bidirectional score submission possible via RemoteEntryUpdateAttached
  (server on port 17471 accepts it from clients, applies + rebroadcasts)