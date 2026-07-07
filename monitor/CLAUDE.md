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
Reads `aes_config.ini` automatically. If that file isn't present, falls back
to a dashboard-downloaded `connector-config-<eventId>.ini` in this folder
(via `find_config_path()`) — same `[aes]`/`[bridge]`/`[dashboard]` shape,
just a per-event filename and `ingest_key`. Errors if more than one
`connector-config-*.ini` is present. Loads `.env` from this folder first,
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
- `playId`: pool/bracket PlayID, passed through from bridge output
- `courtId`: passed through from bridge output
- `sets`: `{"team1": x, "team2": y}` → `{"ft": x, "st": y}`
- `startTime`/`endTime`: UTC ISO 8601 → Eastern local, no offset (via `_eastern_naive`)
- `workTeam`: empty string → `None`
- `hasResult`: from `decided` bool

### `_strip_seed(name)` → bare team name
Strips trailing region/seed suffix ` (XX)` (1–3 uppercase letters) from team names.
Applied to pool standings. e.g. `"Sky High 17 Elite (GL)"` → `"Sky High 17 Elite"`.

### `_server_safe_key(event_id, manual_addition, event_name)` → AES web API event ID string
Derives AES's web-facing event ID string (e.g. `"PTAwMDAwNDUwMjk90"`) from the numeric
`eventId` — this string isn't stored anywhere in the local SchedulerFile binary, it's a
deterministic encoding: zero-pad the ID into `"=0000033281="` (or, for manually-added events,
use the alphanumeric event name instead), base64-encode, strip `=` padding, substitute
`+`→`-`/`/`→`_` for URL-safety, append one length-checksum character. Ported from an
independent reverse-engineering of the same protocol (`gavin-aes-scripts/aes_vsf.py`) and
verified against both his sample data and this project's real event data. `_event_id_key()`
wraps it, pulling `eventId`/`manualAddition`/`name` out of a `tournament_data.json` dict and
returning `None` if `eventId` is missing. Used to populate `aesEventIdKey` in both the delta
and snapshot payloads, alongside the existing numeric `aesEventId`.

### `_pool_payload(p)` → ingest pool shape
Maps a pool dict; computes per-team fields not in `tournament_data.json`:
- `shortName`: sends `fullShortName` (Pool.CompleteShortName, e.g. `"R2G1P5"`) in preference
  to the bare `shortName` (e.g. `"P5"`) — the dashboard displays round/group context
- `divisionId`: passed through from bridge output (`Division.EventDivisionAssignmentID`) —
  required, alongside `courtId`, for the dashboard to create a brand-new Pool row
- `courtId`: first court from `courts` array (required scalar by dashboard schema, and for pool creation)
- `courtName`: first court name
- `courts`: full array passed through (pools can span multiple courts)
- `finishRank`: 1-indexed position in standings array
- `pointRatio`: `ptsFor / ptsAgainst`, or `None` if ptsAgainst == 0
- `goldSpotsCount`: looked up from `gold_spots_map` (second, optional arg — a `{poolId: count}`
  dict built once per snapshot by `_compute_gold_spots()`, see below), NOT read from the
  bridge (which always emits `null` for this field)
- team `name`: seed suffix stripped via `_strip_seed()`

### `_compute_gold_spots(tournament_data)` → `{poolId: goldSpotsCount}`
Computes, for every pool in the file, how many of its finishers are structurally still in
contention for their division's gold bracket — ported from the validated diagnostic script
`aes_gold_contention.py` (see `gold_contention_model.md` in project memory, confirmed correct
against Adam's real data 2026-07-05). This is a **per-pool** count, not a division-wide
constant — a 4-team pool's count varies by round (e.g. Round 1 might keep 3-of-4, Round 2
cuts to 2-of-4), and a first implementation attempt wrongly applied a single division-wide
"total gold bracket size" number to every pool in that division, which Adam caught immediately
(a 4-team pool showing `goldSpotsCount: 16` makes no sense).

Model, per division:
1. Find the Gold bracket via `divisions[].finalPlaces`' `absoluteRank == 1` entry — its logic
   name (`"Winner of R4GoldM15"`) or, once decided, its resolved winner team name — identifies
   which bracket root match produces the division's #1 finisher (`_find_gold_bracket_id()`).
2. Backward BFS from that bracket (`_build_gold_ancestors()`): for every `entrySeed` of every
   play in the frontier, find the play in the nearest earlier round that could have produced
   that value as one of its own exits (`_find_source_play()`, skipping rounds that don't touch
   the seed — AES's own pass-through behavior). Every play reached this way is a "gold
   ancestor."
3. For each pool, each finisher slot's next-round destination (`_find_dest_play()`, forward
   search by `entrySeed == entrySeed`) is checked against the gold-ancestor set
   (`_compute_gold_spots()`); the count of slots landing in that set is the pool's
   `goldSpotsCount`.

**Uses `entrySeed`, not `exitSeed`, on both sides of the search** — this was a second bug fix
(2026-07-07) after a freshly-started live event showed `goldSpotsCount: 0` for every single
pool. A pool's own `exitSeed` values require its standings to be final (AES only assigns which
*physical team* gets which exit seed once the pool's matches are decided), so on a Round 1 pool
with nothing played yet, every `exitSeed` is `null` — the original exitSeed-based version
treated "no data yet" the same as "confirmed zero," silently emitting `0` instead of `null`.
The fix: a play's own exitSeed *multiset* is always the same set of values as its own entrySeed
multiset (barring reseeds, out of scope here) — AES only ever redistributes the seed numbers a
play already received among its own finishers, never introduces new ones. `entrySeed` is
structural (assigned when the bracket/schedule is built), so both `_find_source_play()` (backward)
and the per-pool loop in `_compute_gold_spots()` (forward) now key off `entrySeed` throughout,
making the whole computation available before a single match is played. Verified against real
data both ways: pool `-60263`'s `entrySeed`s `[14,49,76,111]` and `exitSeed`s `[14,76,49,111]`
are the same set just permuted, and re-running the computation with `exitSeed`/`finishRank`
manually blanked out (simulating an unplayed pool) still produced the same correct count.
Only a pool with **no** `entrySeed` at all yet (e.g. a bracket slot AES hasn't structurally
seeded) stays unset in the result map, which `_pool_payload()` then sends as `null`.

Uses `roundIndex`, `groupShortName` (not currently used but kept from the diagnostic script
for parity), and `teamAssignments` (`entrySeed`/`finishRank`) already emitted by
`PoolJson()`/`BracketJson()` in the bridge — no bridge changes were needed. Called once per
snapshot in `push_snapshot()`, passed into every `_pool_payload()` call as `gold_spots_map`.

### `_bracket_payload(b)` / `_bracket_node_payload(node)` → ingest brackets[] shape
Maps a `tournament_data.json` bracket dict (already walked via `Bracket.PlotMatchPositions()`
in the bridge) to the dashboard's `docs/bracket-ingest-spec.md` shape:
- Returns `None` (caller skips the entry) for pool-owned tiebreaker brackets (`b['isPlayoff']`
  — see `IsFromPlayoffBracket` in bridge/CLAUDE.md) and for brackets with no root match yet
  (AES hasn't seeded that round)
- `bracketFullName`: `fullName` (Bracket.CompleteFullName, e.g. `"Round 4 Championship Division"`)
- `bracketShortName`: `shortName` (Bracket.ShortName, bare, e.g. `"Gold"`) — this required a
  bridge fix (see bridge/CLAUDE.md's Bracket properties table): `BracketJson()` used to reflect
  a nonexistent `"Name"` property and bind `bracket.CompleteShortName` to the `shortName` key,
  so both `name` and `shortName` always emitted the round-prefixed `"R4Gold"` instead of the
  bare `"Gold"` — confirmed by diffing against a real web-API response for the same bracket.
  Fixed by reflecting `FullName`/`ShortName` for `name`/`shortName` and adding a new
  `fullShortName` field for the round-prefixed `CompleteShortName` value.
- `date`: Eastern date (via `_eastern_naive`, date portion only) of the **root** (final) match's
  `startTime` — not the earliest match in the tree
- `root`: recursively built via `_bracket_node_payload()`, which walks `topSource`/`bottomSource`
  all the way to leaf (first-round) matches, not just final + semis
- `secondTeamWon`: not a field the bridge emits for bracket match nodes — derived here as
  `outcome == 'SecondTeamWon'`

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
- ~~**goldSpotsCount**~~ — **Resolved.** Computed per-pool by `_compute_gold_spots()`, ported
  from `monitor/aes_gold_contention.py` — see that function's note above.
- ~~**aesEventId**~~ — **Resolved.** The connector now also sends `aesEventIdKey` (the derived
  AES web API string ID) alongside the numeric `aesEventId` — see `_server_safe_key()` above.
