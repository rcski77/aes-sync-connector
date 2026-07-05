# Write-Back Editor & Bracket/Seed Data — Research + Scoping Notes

Captured from a research session investigating whether the decompiled
EventScheduler.exe source (`C:\Git Repos\aes-decompiled\`) exposes enough of
the pool/bracket tree, final-place, and seed logic to (a) fix the
`goldSpotsCount` gap and (b) build a hypothetical third-party tool that edits
a `.vsf` file directly and saves it back in a form AES can reopen.

**Status: research/scoping only. No code has been written for the editor
idea. This is reference material for a future decision, not a commitment.**

## Part 1 — Research Findings (confirmed against source)

**Everything is available live over the sync protocol, not just in the
`.vsf` file.** `NetworkControl.cs:549` builds `EventUpdateAttached` from
`OpenSchedulerFile.Save()` — the identical serializer that writes the `.vsf`
file. The two are structurally the same payload.

- **Bracket tree** — `Match.cs`: each match has `FirstTeamSourceMatch` /
  `SecondTeamSourceMatch` (source of its teams) and `WinnerDestinationMatch` /
  `LoserDestinationMatch` (where its result flows next), forming a DAG.
  `Bracket.PlotMatchPositions()` walks this — already what
  `bridge/AESBridge.cs` uses for the `roots`/`topSource`/`bottomSource` tree
  in `tournament_data.json`. Raw tree encoding on disk:
  `Bracket.leftRootMatchIDs[][]` / `rightRootMatchIDs[][]`.

- **Final place (resolves `goldSpotsCount`, see Outstanding Issue #1 in
  CLAUDE.md)** — `Division.cs:92`, public `Division.FinalPlaces` array (no
  reflection needed) of `FinalPlace` entries. Each has a `GroupName`
  ("Gold"/"Silver"/etc., only populated on the group's `StartOfGroup` entry —
  `Division.cs:1064-1077`) and a `Seed`. **The count of `FinalPlace` entries
  whose `GroupName == "Gold"` is the gold-spot count** — a flat,
  director-assigned list, not something computed by backtracing the bracket.
  `Seed` resolves to a team via `Division.GetTeamFromFinalSeed()` →
  `Round.GetTeamFromExitSeed()` → `GetEntrySeedFromExitSeed()`, walking
  rounds backward.

- **Seed logic** — `Play.TeamAssignment` (`Play.cs`): `EntrySeed` → play
  result → `FinishRank` → `ExitSeed` (becomes next round's `EntrySeed`),
  chained round to round. This is what produces the "Seed 1–48" values shown
  in the R1P1–R1P6 UI.

- **Write symmetry** — `SchedulerFile.cs:575-757`: `Save()`/`Load()` are
  fully symmetric (same magic number `660209715` at start and end of the
  stream, no other checksum found). This is what makes the editor idea below
  structurally plausible.

## Part 2 — Write-Back Editor: Feasibility Scoping

### Edit categories: safe vs. risky

**Low-risk (good v1 targets):** score corrections
(`Match.Sets[i].FirstTeamScore`/`SecondTeamScore`, `Match.TypeOfOutcome`) and
cosmetic renames (`Match.FullName`/`ShortName`) — all public setters, leaf
data, nothing else derives from a score value at load time.

**Medium-risk:** court/time reassignment. `ScheduledCourtID` is `internal
set`, reachable only via reflection (same pattern `AESBridge.cs` already uses
for read access). Scheduling-conflict logic likely depends on
`CachingEnabled`-gated recompute paths scattered across `Match.cs`, `Play.cs`,
`Division.cs` — mutating without triggering whatever those normally trigger
risks leaving derived caches stale.

**High-risk (avoid for v1):** bracket tree rewiring
(`leftRootMatchIDs`/`rightRootMatchIDs`), reseeding
(`EntrySeed`/`FinishRank`/`ExitSeed`), adding/removing matches. These are
graph-structural — match IDs are referenced by ID elsewhere
(`FirstTeamSourceMatchID`, bracket arrays), and a one-field edit here can
silently desync the visual bracket from the match graph in ways that only
surface when AES itself next recalculates tournament flow. This is also
exactly where `CachingEnabled`-gated code concentrates most heavily
(especially in `Division.cs`).

### Two architectural options

**(A) File-level editor** — load a real `.vsf` via the actual
`AES.Scheduler.Model` assembly (the same one `AESBridge.cs` already
references at runtime), mutate objects via existing setters/reflection, call
`Save()`, write back. Used only while AES is closed. Strength: no need to
reimplement the binary format — reuses a fully symmetric, already-verified
Save/Load pair. Weakness: AES must stay closed for the whole edit session,
and any invariant invisible from decompiled source (on-open validation,
cache-flag side effects) is a silent-failure risk discovered only when the
director reopens AES.

**(B) Live network write via `RemoteEntryUpdateAttached`** — the connector
already listens on this command, and CLAUDE.md documents the payload shape
from the receive side. CLAUDE.md *asserts* (not yet verified) that the AES
server accepts this from clients and rebroadcasts it. If true, this covers
score/outcome edits with zero file-format risk, works while AES is running,
and — critically — reuses AES's own validation/recompute path since AES
itself processes the update rather than an external tool guessing at
invariants.

**Recommendation: scope/validate (B) before (A).** It covers the single
highest-value, lowest-risk edit category (score correction), carries no
file-corruption exposure, and cheaply resolves an assumption CLAUDE.md
currently states as fact but has never actually tested. (A) remains the
fallback for anything (B) can't reach (court/time, cosmetic fields), funded
only after B is validated or ruled out.

### Unknowns to resolve before committing further to (A)

1. Does EventScheduler.exe validate anything beyond the leading/trailing
   magic number on open (hash, "modified by version" gate, stricter
   recompute-and-compare)?
2. Does `Load()` recompute derived fields (bracket positions, standings,
   `FinalPlaces`) that would silently overwrite manual edits on next open?
3. Is `CachingEnabled` (private-set, off by default, gated across
   Match/Play/Division/Team/Event/Court/User) something `Load()` sets
   internally that a reflection-based mutator would need to replicate —
   does skipping `EnableCaching()` before mutating leave stale caches that
   get silently persisted?
4. `SchedulerFile.Load()` wraps its tail fields (including
   `LastUpdatedTimestamp`) in a try/catch that swallows exceptions — does
   that mean newer-version trailing data gets silently dropped on
   round-trip, i.e. is `Save(Load(x))` even byte-stable for an unmodified
   file?

### Recommended next step, whenever this gets picked back up

A single cheap, reversible experiment answers unknowns 1, 2, and 4 without
further source archaeology, and should gate any real investment in option
(A):

- Add a `--roundtrip <infile> <outfile>` mode to the existing
  `bridge/AESBridge.cs` (reuses the existing project/assembly reference, no
  new project needed): `Load()` a real `.vsf` copy → immediately `Save()`
  with zero mutations → write to `<outfile>`.
- Diff the two files byte-for-byte.
- Separately, reopen `<outfile>` in real AES (on a throwaway copy of a
  tournament file, never the live one) and confirm it opens cleanly with no
  data loss or repair prompts.

Separately, validating (B) would mean a small standalone test sending a
synthetic `RemoteEntryUpdateAttached` payload to a test AES instance and
confirming it's applied and rebroadcast — this does not touch
`AESBridge.cs` and could reuse the monitor's existing wire-frame encoding.

## Key Files Referenced

- `C:\Git Repos\aes-decompiled\EventScheduler\AES\Scheduler\Model\SchedulerFile.cs` — Save/Load symmetry
- `C:\Git Repos\aes-decompiled\EventScheduler\AES\Scheduler\Model\Match.cs` — bracket source/destination links, Set scores
- `C:\Git Repos\aes-decompiled\EventScheduler\AES\Scheduler\Model\Division.cs` — FinalPlaces (gold/silver/bronze), seed resolution
- `C:\Git Repos\aes-decompiled\EventScheduler\AES\Scheduler\Model\Play.cs` — TeamAssignment seed chain
- `C:\Git Repos\aes-decompiled\EventScheduler\AES\Scheduler\Model\Bracket.cs` — PlotMatchPositions, root match ID arrays
- `C:\Git Repos\aes-decompiled\EventScheduler\EventScheduler\Controls\NetworkControl.cs:549` — confirms EventUpdateAttached == Save()
- `bridge/AESBridge.cs` — existing reflection pattern, where `--roundtrip` would go
- `CLAUDE.md` — Outstanding Issues, RemoteEntryUpdate bidirectional note
