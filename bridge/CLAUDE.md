# AES Bridge — C# Project Context

## What This Does
Loads an AES Scheduler binary payload (SchedulerFile format) by invoking the
`AES.Scheduler.Model` assembly from EventScheduler_Release.exe, then serializes
the tournament data to `tournament_data.json`. Also decodes `RemoteEntryUpdate`
BinaryFormatter payloads via `--remote` flag, and encodes a score correction
into that same wire format via `--encode-remote` (the write-back direction —
see "Encoding (--encode-remote)" below and root CLAUDE.md's "Dashboard Outbox
API").

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
AESBridge.exe --encode-remote <outFile> <fileIdGuid> <matchId> <outcome> <workTeamNumber> <typeOfWorkTeam> [<t1> <t2> ...]
                                          # encode a score correction (write-back direction)
```
Output JSON is written next to the input file (or next to AESBridge.exe if piped).
`--encode-remote` writes raw BinaryFormatter bytes to `<outFile>` instead (binary,
not JSON — the caller sends it straight over the wire, never through stdout/`text=True`
subprocess capture, which would corrupt it).

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

## Encoding (`--encode-remote`) — the write-back direction

`EncodeRemoteEntry(args)` builds a `string[]` matching AES's own
`Match.GetMatchSerialization()` shape and `BinaryFormatter().Serialize()`s it
to a file — the exact inverse of `DecodeRemoteEntry`. No custom
`SerializationBinder` is needed here (binders are deserialization-only; a bare
`string[]`/`System.String` round-trips identically regardless of which
process wrote it).

**Why the validation matters:** AES's own apply logic
(`SchedulerFile.UpdateWithRemoteEntryData`, in the decompiled source — see
root CLAUDE.md's "Score Write-Back") has **no try/catch anywhere**. A
malformed payload throws on AES's live network-receive thread, not in our
process. `ValidateEncodeArgs` is the only thing standing between a bad
outbox command and a crash in the director's live AES session:

| Check | Why (maps to a specific crash in `UpdateWithRemoteEntryData`) |
|---|---|
| `args.Length >= 6` | `strArray[4]`/`[5]` are read unconditionally before the set-pairs loop — `IndexOutOfRangeException` on a short array |
| `(trailing) % 2 == 0` | The set-pairs loop reads two array slots per iteration; an odd trailing count throws on the dangling final element |
| `Guid.TryParse(fileId)` | Not itself a crash risk (a bad GUID just never matches `SchedulerFile.FileID` and no-ops) — rejected anyway as a caller-bug signal |
| `int.TryParse(matchId)` | `int.Parse` throws `FormatException` on non-numeric |
| `int.TryParse(outcome)`, range `0-5` | `Match.OutcomeType` has 6 members; out-of-range doesn't throw (C# enum casts aren't validated) but corrupts `TypeOfOutcome` silently — rejected for data hygiene |
| `int.TryParse(workTeamNumber)` | `int.Parse` throws on non-numeric; no enum bound, plain int index |
| `int.TryParse(typeOfWorkTeam)`, range `0-9` | `Match.WorkTeamType` has exactly 10 members |
| each set score `int.TryParse`, range `0-199` | `int.Parse` throws on non-numeric; the upper bound is a defensive sanity clamp, not an AES constraint |
| decided outcome (`!= 0`) requires ≥1 set pair | Not a crash risk — a "decided, zero sets" match is unambiguously bad data, cheap to catch here |

The encoder has no live `Match`/`SchedulerFile` loaded in this mode, so it
**cannot** verify "does this include every currently-scored set for this
match" — `UpdateWithRemoteEntryData` clears any `Sets` slot beyond what's
supplied, so sending a partial list silently wipes later sets. That invariant
is the caller's job (`monitor/aes_monitor.py`'s `_merge_for_encode`, see
monitor/CLAUDE.md), not this validation.

---

## Known Match Properties (AES.Scheduler.Model.Match)

Confirmed present via reflection dump of EventScheduler_Release.exe:

| Property | Type | Notes |
|---|---|---|
| `FirstTeamText` | string | Team name with seed, e.g. "Sky High 17 Elite (GL)" |
| `SecondTeamText` | string | Same for team 2 |
| `WorkTeamText` | string | Ref/work team; empty string if not assigned |
| `WorkTeamNumber` | int | Public. Raw index paired with `TypeOfWorkTeam` — emitted in JSON as `workTeamNumber` alongside the formatted `workTeam` text, needed (unmodified) by write-back's `--encode-remote` so a score-only correction doesn't clear an assigned work team |
| `TypeOfWorkTeam` | Match.WorkTeamType | Public. Emitted as `typeOfWorkTeam` (enum name string, same style as `outcome`). 10 members: None, NextHigher, NextLower, PreviousWinner, PreviousLoser, InternalAbsolute, ExternalAbsolute, CustomText, AnotherMatchWinner, AnotherMatchLoser (int values 0-9) |
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
| `OwningPlay` | Play | Pool or Bracket this match belongs to; `OwningPlay.PlayID` (via reflection, `RInt`) is the same ID as `pool.PlayID`/`bracket.PlayID` — used as `matches[].playId`, **except** when `OwningPlay` is a pool's own tiebreaker bracket (see below) |
| `IsFromPlayoffBracket` | bool | Public, computed as `OwningPlay is Bracket && ((Bracket)OwningPlay).IsPlayoff`. True for matches generated by a **pool's own internal tiebreaker bracket** (`Pool.PlayoffBracket`, used to resolve 2-3 way stat ties via head-to-head mini-bracket play) — NOT a division's actual gold/playoff bracket, despite the name. Same flag AES's own `Play.TeamStats`/`Division.TeamStats` use to exclude these from official stats. Used in `ComputeStandings()` to exclude tiebreaker matches. |

**`playId` for tiebreaker matches:** `MatchJson()` special-cases matches whose
`OwningPlay` is a pool's tiebreaker bracket (`OwningPlay is Bracket b && b.IsPlayoff`):
instead of using that bracket's own `PlayID` (a separate, unrelated ID — e.g. a
pool with `poolId -50754` had its tiebreaker bracket at `PlayID -50755`), the
match's `playId` is set to `b.OwningPool.PlayID` (public, no reflection needed).
Without this, a tiebreaker match's `playId` would never match any pool's `poolId`,
so downstream consumers that group matches by `playId == pool.poolId` (e.g. a
dashboard's "match results" list for a pool) would silently drop the tiebreaker
result even though it's fully present and decided in the data.

## Known Play Properties (AES.Scheduler.Model.Play — shared base of Pool and Bracket)

Match-format config lives here, not on `Division` or `Match` — one format per
pool/bracket, shared by every match inside it (`Play.SetCount`'s setter pushes
the value down to `match.SetCount` for every `Match` in the play). All public,
no reflection needed:

| Property | Notes |
|---|---|
| `TypeOfMatches` | `Play.MatchType` enum: `BestOf` or `NumberOfSets` |
| `SetCount` | int — total set slots. Forced odd when `TypeOfMatches == BestOf` |
| `PointsToWinNormalSet` | int, e.g. `25` |
| `PointsToWinDecidingSet` | int, e.g. `15` — same as `PointsToWinNormalSet` if no deciding-set difference is configured |
| `MatchDescription` | string, AES's own formatted summary — `"2 of 3 to 25(15)"` (BestOf) or `"3 Sets to 25"` (NumberOfSets); reused verbatim as `matchFormat` |

Emitted on `pools[]`/`brackets[]` (one per play), not on `matches[]` — see
schema below.

## Known Pool Properties (AES.Scheduler.Model.Pool)

| Property | Notes |
|---|---|
| `PlayID` | Unique ID |
| `FullName` | e.g. "Pool 1" — accessed via `RStr(pool, "FullName")` |
| `ShortName` | e.g. "P1" — accessed via `RStr(pool, "ShortName")` |
| `CompleteShortName` | e.g. "R1P1" — fallback if FullName/ShortName unavailable |
| `Courts` | Court[] — accessed via reflection; each court has `CourtID` (int) and `Name` (string) |
| `Matches` | Match[] — iterate to compute standings and find date |
| `OwningGroup?.OwningRound?.OwningDivision` | Chain to get division; `Division.EventDivisionAssignmentID` (public, no reflection) is the numeric `divisionId` sent in `pools[]` |

## Known Bracket Properties (AES.Scheduler.Model.Bracket)

| Property | Notes |
|---|---|
| `PlayID` | Unique ID |
| `FullName` | Bare name, e.g. `"Championship Division"` — internal, accessed via `RStr(bracket, "FullName")`; fallback `CompleteFullName` |
| `ShortName` | Bare short name, e.g. `"Gold"` — internal, accessed via `RStr(bracket, "ShortName")`; fallback `CompleteShortName` |
| `CompleteShortName` | Round-prefixed, e.g. `"R4Gold"` |
| `CompleteFullName` | Round-prefixed, e.g. `"Round 4 Championship Division"` |
| `IsPlayoff` | bool |
| `Notes` | string |
| `Matches` | Match[] |
| `PlotMatchPositions()` | Returns `List<MatchPlacement>` with X/Y layout for bracket tree |

**Verified against a real web-API response for the same bracket** (`PlayId -61004`, event 45032):
`FullName: "Championship Division"`, `ShortName: "Gold"`, `CompleteShortName: "R4Gold"`,
`CompleteFullName: "Round 4 Championship Division"`. Bracket is a `Play` subclass (same base
as Pool), so it exposes the same bare-vs-complete distinction Pool does — see `FullName`/
`ShortName` in the Pool table above.

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
  "event":   { "name", "eventId", "startDate", "endDate", "lastUpdated", "fileId", "manualAddition" },
  // fileId: SchedulerFile.FileID (Guid), one per tournament file — required as payload
  // slot [0] when building a write-back RemoteEntryUpdate; see "Encoding (--encode-remote)"
  "courts":  [{ "courtId", "name" }],
  "matches": [{
    "matchId", "courtId", "courtName",
    "startTime", "endTime",   // UTC ISO 8601
    "matchLength",            // minutes
    "workTeamNumber", "typeOfWorkTeam",  // raw fields behind the formatted "workTeam" text below
    "team1", "team2", "workTeam",
    "divisionCode", "divisionName", "playId", "playName", "playType",
    "outcome", "decided", "firstTeamWon",
    "scoreText",
    "sets": [{ "team1", "team2" }],   // only played sets (both scores present)
    "shortName", "fullName"
  }],
  "pools": [{
    "poolId",
    "name",          // Pool.FullName (reflection), e.g. "Pool 1"; fallback: CompleteFullName
    "shortName",     // Pool.ShortName (reflection), e.g. "P1"; fallback: CompleteShortName
    "fullShortName", // Pool.CompleteShortName always, e.g. "R2G1P5" (Round+Group+Pool) — dashboard prefers this
    "divisionCode", "divisionName",
    "divisionId",  // Division.EventDivisionAssignmentID, int|null
    "courts": [{ "courtId", "name" }],  // Pool.Courts (reflection); fallback: single entry from first match
    "date",        // "YYYY-MM-DD" UTC, from first scheduled match
    "goldSpotsCount", // always null here — bridge doesn't compute this, see goldSpotsCount note below
    "matchFormat", "typeOfMatches", "setCount",
    "pointsToWinNormalSet", "pointsToWinDecidingSet",  // see "Known Play Properties" above
    "standings": [{ "team", "wins", "losses", "setsWon", "setsLost", "ptsFor", "ptsAgainst", "finishRank" }]
  }],
  "brackets": [{
    "bracketId",
    "name",          // Bracket.FullName (reflection), e.g. "Championship Division"; fallback: CompleteFullName
    "shortName",     // Bracket.ShortName (reflection), e.g. "Gold"; fallback: CompleteShortName
    "fullName",      // Bracket.CompleteFullName always, e.g. "Round 4 Championship Division"
    "fullShortName", // Bracket.CompleteShortName always, e.g. "R4Gold" (Round+bracket short name)
    "divisionCode", "divisionName", "isPlayoff", "notes",
    "matchFormat", "typeOfMatches", "setCount",
    "pointsToWinNormalSet", "pointsToWinDecidingSet",  // same shape/meaning as pools[] above
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
Pool standings stats (wins/losses/sets/points) are computed by AESBridge from match results
(not read from AES directly):
- `pool.Matches` concatenates the pool's own round-robin matches with any matches from its
  internal tiebreaker bracket (`Pool.PlayoffBracket`) — the latter are excluded via
  `m.IsFromPlayoffBracket` before computing stats, mirroring AES's own internal filtering
- Win/loss from `m.TypeOfOutcome`
- Sets won/lost from comparing `s.FirstTeamScore` vs `s.SecondTeamScore` per set
- Points for/against summed across all sets

**Order** is NOT our own win/set-diff/point-diff sort (that's only used as a lookup source,
never emitted in that order) — and, as of 2026-07, it's also no longer sorted by AES's own
`Play.TeamAssignment.FinishRank` either. `PoolJson()` used to re-sort by `FinishRank` so the
connector's order would track AES's real finish order instead of our computed fallback, but
*any* rank-based ordering reorders the array as a pool plays out, and the dashboard writes
`teams` straight to the UI with no re-sort of its own — so a rank-sorted array made pool-
flyout rows visibly reshuffle mid-pool for connector-fed events (see
`connector-pool-fields-update.md` in the dashboard repo). `PoolJson()` now emits `standings`
in `pool.Teams`' own roster order (the same never-reordered array used to build
`teamAssignments`), looking up each team's computed stats by `TeamText` and attaching AES's
real `FinishRank` (nullable — until AES has ranked that team) as a per-team `finishRank`
field. That field is a display value only; it does not drive array order.

The monitor's `_pool_payload()` reads `finishRank` straight from each `standings` entry
(no longer derived positionally from array index).

**Bug fix (2026-07-08) — placeholder team leaking into standings for pools with a 2-set
playoff/tiebreaker:** `ComputeStandings()` used to register roster candidates (`Reg()`) from
`allMatches` (round-robin + `Pool.PlayoffBracket` matches together) before filtering
`IsFromPlayoffBracket` matches out of the win/loss computation. When AES has scheduled a
pool's tiebreaker but its first match isn't decided yet, the second tiebreaker match's
`FirstTeamText`/`SecondTeamText` is a placeholder like `"Winner of Match 1"` — that text got
registered into `teams`, and since it's absent from `pool.Teams` (the real roster), `PoolJson()`'s
"team present in standings but not in `pool.Teams`" fallback (meant to catch genuinely
unexpected cases) re-appended it as a bogus all-zero standings row. Fixed by registering roster
candidates from `regularMatches` (post-`IsFromPlayoffBracket`-filter) instead of `allMatches` —
see `docs/ingest-api.md` in aes-tourney-director for the server-side description of this same
placeholder-row quirk (that doc's guidance was to build `teams` from the roster and drop
placeholder entries; this fix makes the bridge's own "roster" computation actually exclude them
at the source instead of relying on the dashboard's name-pattern backstop filter).

---

## Outstanding / Known Gaps
- `goldSpotsCount` — the bridge always emits `null` for this field; it is NOT computed here.
  It's a per-pool value (how many of that pool's finishers are still structurally in
  contention for the division's gold bracket), computed downstream by the monitor's
  `_compute_gold_spots()` using `roundIndex`/`groupShortName`/`teamAssignments` (already
  emitted by `PoolJson()`/`BracketJson()`) plus `divisions[].finalPlaces` and
  `brackets[].roots`. See monitor/CLAUDE.md and `gold_contention_model.md` in project memory
  for the model — a first attempt here wrongly computed a division-wide bracket-size
  constant instead of a per-pool contention count; don't repeat that mistake.
- Pool standings `team` names include seed suffix (e.g. "(GL)") — stripped by `_strip_seed()` in monitor.
- `workTeam` is an empty string when not assigned; monitor coerces to null.
- The AES web API event ID string (e.g. `PTAwMDAwNDUwMjk90`) is not stored in the SchedulerFile
  binary, but the monitor now derives it from the numeric `eventId` (see `_server_safe_key()` in
  monitor/CLAUDE.md) using the new `manualAddition` field on the `event` block. No further
  assembly access needed for this.
