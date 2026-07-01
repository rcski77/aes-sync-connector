# AES Bridge — C# Project Context

## What This Does
Loads an AES Scheduler binary payload (SchedulerFile format) by invoking the
`AES.Scheduler.Model` assembly from EventScheduler_Release.exe, then serializes
the tournament data to `tournament_data.json`. Also decodes `RemoteEntryUpdate`
BinaryFormatter payloads via `--remote` flag.

## Build
```bash
# From this directory (bridge/)
"C:\Program Files\dotnet\dotnet.exe" build AESBridge.csproj -c Release
# Output: bin\Release\net48\AESBridge.exe
```
`dotnet` is NOT on the PowerShell PATH — use Git Bash or the full path above.
Always build after editing AESBridge.cs to catch C# string escaping bugs early.

## Usage
```
AESBridge.exe <path-to-binary>           # parse SchedulerFile → tournament_data.json
AESBridge.exe <path-to-binary> --remote  # decode RemoteEntryUpdate String[] payload
```
Output JSON is written next to the input file (or next to AESBridge.exe if piped).

## Project Config (AESBridge.csproj)
- Target: net48 (x86) — must match EventScheduler.exe architecture
- EventScheduler.exe is copied to bin/ as EventScheduler_Release.exe at build time

## Key Files
| File | Purpose |
|------|---------|
| AESBridge.cs | Entire bridge — one file |
| AESBridge.csproj | .NET 4.8 x86 project |
| EventScheduler.exe | AES dependency — required at build and runtime |
| bin/Release/net48/AESBridge.exe | Compiled output |
| bin/Release/net48/EventScheduler_Release.exe | AES assembly loaded at runtime |

---

## Assembly Access Pattern

The EventScheduler assembly has many internal/private members. Two strategies are used:

**Direct access** — for properties confirmed public in the binary:
```csharp
m.FirstTeamText, m.ScoreText, m.Sets, m.IsScheduled, etc.
```

**Reflection** — for internal properties or when the compiled name differs:
```csharp
static T Reflect<T>(object obj, string propName, T fallback = default)
static string RStr(object obj, string propName)  // string shorthand
static int    RInt(object obj, string propName)  // int shorthand
```

When in doubt, use reflection — it won't throw, just returns the fallback.

---

## Known Match Properties (AES.Scheduler.Model.Match)

Confirmed present via reflection dump of EventScheduler_Release.exe:

| Property | Type | Notes |
|---|---|---|
| `FirstTeamText` | string | Team name with seed, e.g. "Sky High 17 Elite (GL)" |
| `SecondTeamText` | string | Same for team 2 |
| `WorkTeamText` | string | Ref/work team; empty string if not assigned |
| `ScoreText` | string | "25-20, 25-18" formatted |
| `Sets` | Match.Set[] | All set slots; FirstTeamScore/SecondTeamScore are nullable int |
| `ScheduledCourtText` | string | "Court 1" or "No Court" |
| `ScheduledCourtID` | int | Internal — access via `RInt(m, "ScheduledCourtID")` |
| `ScheduledStartDateTime` | DateTime | UTC |
| `ScheduledEndDateTime` | DateTime | Computed from start + MatchLength |
| `MatchLength` | int | Minutes |
| `TypeOfOutcome` | Match.OutcomeType | Undecided / FirstTeamWon / SecondTeamWon / Tie / Forfeit variants |
| `FirstTeamWon` | bool | |
| `CompleteShortName` | string | e.g. "R1P1M1" |
| `CompleteFullName` | string | e.g. "Round 1 Pool 1 Match 1" |
| `IsScheduled` | bool | Only scheduled matches are included in output |
| `OwningPlay` | Play | Pool or Bracket this match belongs to |

## Known Pool Properties (AES.Scheduler.Model.Pool)

| Property | Notes |
|---|---|
| `PlayID` | Unique ID |
| `FullName` | e.g. "Pool 1" — accessed via `RStr(pool, "FullName")` |
| `ShortName` | e.g. "P1" — accessed via `RStr(pool, "ShortName")` |
| `CompleteShortName` | e.g. "R1P1" — fallback if FullName/ShortName unavailable |
| `Courts` | Court[] — accessed via reflection; each court has `CourtID` (int) and `Name` (string) |
| `Matches` | Match[] — iterate to compute standings and find date |
| `OwningGroup?.OwningRound?.OwningDivision` | Chain to get division |

## Known Bracket Properties (AES.Scheduler.Model.Bracket)

| Property | Notes |
|---|---|
| `PlayID` | Unique ID |
| `CompleteShortName` / `CompleteFullName` | |
| `IsPlayoff` | bool |
| `Notes` | string |
| `Matches` | Match[] |
| `PlotMatchPositions()` | Returns `List<MatchPlacement>` with X/Y layout for bracket tree |

## MatchPlacement Structure
Returned by `Bracket.PlotMatchPositions()`:
- `Match` — the match at this node
- `X`, `Y` — float layout coordinates (nullable)
- `Reversed` — bool
- `DoubleCapped` — bool, true for leaf nodes (first-round matches)
- `TopSource`, `BottomSource` — recursive MatchPlacement references

Root nodes are those not referenced as TopSource or BottomSource by any other placement.

---

## tournament_data.json Output Schema

```json
{
  "event":   { "name", "eventId", "startDate", "endDate", "lastUpdated" },
  "courts":  [{ "courtId", "name" }],
  "matches": [{
    "matchId", "courtId", "courtName",
    "startTime", "endTime",   // UTC ISO 8601
    "matchLength",            // minutes
    "team1", "team2", "workTeam",
    "divisionCode", "divisionName", "playName", "playType",
    "outcome", "decided", "firstTeamWon",
    "scoreText",
    "sets": [{ "team1", "team2" }],   // only played sets (both scores present)
    "shortName", "fullName"
  }],
  "pools": [{
    "poolId",
    "name",      // Pool.FullName (reflection), e.g. "Pool 1"; fallback: CompleteFullName
    "shortName", // Pool.ShortName (reflection), e.g. "P1"; fallback: CompleteShortName
    "divisionCode", "divisionName",
    "courts": [{ "courtId", "name" }],  // Pool.Courts (reflection); fallback: single entry from first match
    "date",        // "YYYY-MM-DD" UTC, from first scheduled match
    "standings": [{ "team", "wins", "losses", "setsWon", "setsLost", "ptsFor", "ptsAgainst" }]
  }],
  "brackets": [{
    "bracketId", "name", "shortName", "fullName",
    "divisionCode", "divisionName", "isPlayoff", "notes",
    "matchCount", "decided",
    "roots": [{
      "x", "y", "reversed", "doubleCapped",
      "match": {
        "matchId", "shortName", "fullName", "team1", "team2",
        "courtId", "courtName", "startTime", "endTime",
        "outcome", "decided", "firstTeamWon", "scoreText",
        "sets": [{ "team1", "team2" }]   // ALL slots incl. unplayed (null values)
      },
      "topSource": { ...recursive },
      "bottomSource": { ...recursive }
    }]
  }]
}
```

Note: bracket match `sets` include ALL set slots (including unplayed, with null scores)
so the renderer knows the maximum set count. Top-level match `sets` only include played sets.

---

## Standings Computation
Pool standings are computed by AESBridge from match results (not read from AES directly):
- Win/loss from `m.TypeOfOutcome`
- Sets won/lost from comparing `s.FirstTeamScore` vs `s.SecondTeamScore` per set
- Points for/against summed across all sets
- Sorted: wins desc → set differential desc → point differential desc

`finishRank` and `pointRatio` are computed by the monitor's `_pool_payload()`, not here.

---

## Outstanding / Known Gaps
- `goldSpotsCount` — number of teams advancing to gold bracket — not yet found in assembly.
  Investigate properties on `Pool.OwningGroup` or `Pool.OwningRound`.
- Pool standings `team` names include seed suffix (e.g. "(GL)") — stripped by `_strip_seed()` in monitor.
- `workTeam` is an empty string when not assigned; monitor coerces to null.
- Full AES web API event ID string (e.g. `PTAwMDAwNDUwMjk90`) is not present in the SchedulerFile
  binary — only the numeric `eventId` integer is available locally.
