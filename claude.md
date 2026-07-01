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
├── .env                  ← secrets (not committed)
└── CLAUDE.md
```

## Files
| File | Purpose |
|------|---------|
| monitor/aes_monitor.py | Production monitor — connects to AES, calls AESBridge, POSTs to dashboard ingest API |
| monitor/aes_test.py | One-shot connection test — run first to verify AES is reachable |
| monitor/aes_sync_probe.py | Protocol debugger — try commands, log everything |
| monitor/aes_config.ini | Config: AES host/port/password, bridge path, dashboard endpoint/key |
| bridge/AESBridge.cs | C# bridge — deserializes SchedulerFile binary, outputs tournament_data.json |
| bridge/AESBridge.csproj | .NET project (net48, x86) — build with: dotnet build -c Release |

## Build
```
cd "C:\Git Repos\aes-connector\bridge"
dotnet build AESBridge.csproj -c Release
# Output: bridge\bin\Release\net48\AESBridge.exe
# Requires: EventScheduler.exe in bridge\ folder
```

**NOTE:** `dotnet` is not on the PowerShell PATH. Use Git Bash or prefix with the full path:
`"C:\Program Files\dotnet\dotnet.exe" build AESBridge.csproj -c Release`

**NOTE:** After any edit to AESBridge.cs, always run dotnet build immediately to catch
C# string escaping bugs — escaped quotes inside interpolated strings corrupt silently.

## Run
```
cd "C:\Git Repos\aes-connector\monitor"
python aes_monitor.py
# Reads aes_config.ini + ../.env automatically
```

## Config

The monitor reads `aes_config.ini` if present; otherwise it falls back to a
dashboard-downloaded `connector-config-<eventId>.ini` in the same folder
(same `[aes]`/`[bridge]`/`[dashboard]` shape — the dashboard now generates
this directly with a per-event `ingest_key`, so no format translation is
needed). See `find_config_path()` in aes_monitor.py.

**aes_config.ini** (in monitor/):
```ini
[aes]
host     = 127.0.0.1
port     = 17471
password = ${AES_PASSWORD}

[bridge]
exe = ..\bridge\bin\Release\net48\AESBridge.exe

[dashboard]
endpoint   = ${DASHBOARD_ENDPOINT}   # base URL up to /api/ingest (no trailing slash)
ingest_key = ${INGEST_API_KEY}
timeout    = 10
```

**.env** (project root — loaded by monitor on startup, checked in monitor/ first then parent):
```
AES_PASSWORD=your_aes_password
DASHBOARD_ENDPOINT=https://your-dashboard.com/api/ingest
INGEST_API_KEY=your_shared_secret
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
- `m.FirstTeamText` / `m.SecondTeamText` / `m.WorkTeamText` — formatted team name with seed
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

---

## tournament_data.json Schema
Written to monitor/ at runtime. This is AESBridge's intermediate output;
the monitor transforms it into the ingest API payload shape before POSTing.

```json
{
  "event": { "name", "eventId", "startDate", "endDate", "lastUpdated" },
  "courts": [{ "courtId", "name" }],
  "matches": [{
    "matchId", "courtId", "courtName", "startTime", "endTime", "matchLength",
    "team1", "team2", "workTeam",   // workTeam is null if not assigned
    "divisionCode", "divisionName", "playName",
    "playType",     // "pool" | "bracket" | "playoff"
    "outcome",      // "Undecided" | "FirstTeamWon" | "SecondTeamWon" | etc.
    "decided",      // bool
    "firstTeamWon", "secondTeamWon",  // bool
    "scoreText",    // "25-20, 25-18"
    "sets": [{ "team1", "team2" }],   // only played sets
    "shortName", "fullName"
  }],
  "pools": [{
    "poolId", "name", "shortName", "divisionCode", "divisionName",
    "courts": [{ "courtId", "name" }],  // from Pool.Courts (reflection); fallback = first match court
    "date",         // "YYYY-MM-DD" from first scheduled match (UTC)
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
      "topSource":    { ...recursive },
      "bottomSource": { ...recursive }
    }]
  }]
}
```

---

## Dashboard Ingest API

The monitor posts to two endpoints on the dashboard server. Auth is
`Authorization: Bearer <INGEST_API_KEY>`. Base URL is set in aes_config.ini `endpoint`.

### POST /api/ingest/delta
Fires on every `CMD_REMOTE_ENTRY_UPDATE` **with set scores present**. Winner-only
updates (outcome set but no sets yet) are suppressed. Undecided + no sets passes
through (signals a score was cleared). Single match only.
```json
{
  "aesEventId": "33281",
  "match": {
    "matchId": -51376,
    "division": "17 Open",
    "courtId": -64759,
    "courtName": "North 14",
    "startTime": "2025-06-28T12:30:00",   // local Eastern, no offset
    "endTime":   "2025-06-28T13:30:00",
    "team1": "Sky High 17 Elite",          // seed suffix stripped
    "team2": "COLAVOL 17 Black",
    "workTeam": "414 - 17 Outlaws",        // null if not assigned
    "hasResult": true,
    "outcome": "FirstTeamWon",
    "sets": [{ "ft": 25, "st": 10 }, { "ft": 25, "st": 12 }]
  }
}
```

**Note:** `aesEventId` is the numeric `eventId` integer from the SchedulerFile (e.g. `"33281"`).
The full AES web API string ID (e.g. `PTAwMDAwNDUwMjk90`) is not present in the local binary
and cannot be read by the connector. The dashboard must store and match on the numeric ID.

### POST /api/ingest/snapshot
Fires on first connect, then throttled to every 3 minutes (SNAPSHOT_INTERVAL = 180s).
Full tournament state — dashboard upserts everything and deletes absences.
```json
{
  "aesEventId": "33281",
  "snapshotTime": "2025-06-28T17:00:00Z",
  "matches": [ ...same match shape as delta... ],
  "pools": [{
    "playId": 11111,
    "division": "17 Open",
    "name": "Pool 1",        // FullName from assembly, e.g. "Pool 1"
    "shortName": "P1",       // ShortName from assembly, e.g. "P1"
    "courtId": -64759,       // first court ID (required by dashboard schema)
    "courtName": "North 14", // first court name
    "courts": [{ "courtId": -64759, "name": "North 14" }],  // full array (pools can span multiple courts)
    "date": "2025-06-28",
    "goldSpotsCount": null,
    "teams": [{
      "name": "Sky High 17 Elite",   // seed suffix stripped
      "matchesWon": 3, "matchesLost": 0,
      "setsWon": 6, "setsLost": 1,
      "pointRatio": 1.42,   // ptsFor/ptsAgainst, null if no points against
      "finishRank": 1       // 1-indexed from standings order
    }]
  }]
}
```

**Time handling:** AESBridge emits UTC ISO 8601. The monitor converts to Eastern local
time (no offset) using `zoneinfo.ZoneInfo('America/New_York')`, with a fixed -5h fallback
if zoneinfo is unavailable.

---

## Production Deployment Architecture
```
Windows laptop (runs AES Scheduler)
────────────────────────────────────
EventScheduler.exe
  │ port 17471
  │
aes_monitor.py  ←── aes_config.ini ←── .env
  │ subprocess
  ▼
AESBridge.exe
  │ writes monitor/tournament_data.json (intermediate)
  │
  ├── on RemoteEntryUpdate → POST /api/ingest/delta   (immediate)
  └── on EventUpdate (throttled 3 min) → POST /api/ingest/snapshot

Dashboard server (aes-tourney-director, Node.js/Express)
─────────────────────────────────────────────────────────
POST /api/ingest/delta     ← upserts single match result
POST /api/ingest/snapshot  ← upserts all matches + pools, deletes absences
Auth: Authorization: Bearer <INGEST_API_KEY>
```

---

## Outstanding Issues
1. **goldSpotsCount always null** — the number of teams advancing from each pool to gold
   bracket is not yet extracted from the AES assembly. Needs investigation via reflection
   to find the right property on Pool or its OwningGroup/OwningRound.

2. **aesEventId mismatch** — the connector sends the numeric `eventId` integer. The dashboard
   `Event` table must store this numeric ID (in addition to or instead of the web API string
   like `PTAwMDAwNDUwMjk90`) for the ingest endpoints to match successfully.

## Context
- Adam: tournament director, 20yr experience, ~150 events/yr, Midwest-focused
- Dashboard: Node.js/Express app in separate repo (aes-tourney-director), director-facing
- No tablets used — only main AES UI for score entry
- Future: bidirectional score submission possible via RemoteEntryUpdateAttached
  (server on port 17471 accepts it from clients, applies + rebroadcasts)
