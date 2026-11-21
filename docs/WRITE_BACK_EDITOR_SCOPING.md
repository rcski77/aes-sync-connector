> **Status update (2026-07-10):** Option (B) below is now **implemented**, not
> just scoped — see root CLAUDE.md's "Dashboard Outbox API" / "Score
> Write-Back" sections, `bridge/AESBridge.cs`'s `--encode-remote` mode, and
> `monitor/aes_monitor.py`'s outbox poll/send/ack path. This also resolves
> the two things this doc flagged as unverified assumptions: AES's server
> does accept an inbound `RemoteEntryUpdateAttached` from a client (confirmed
> against `NetworkControl.nc_OnDataReceived` in the decompiled source), and
> it rebroadcasts to *other* connected clients only — not back to the sender
> (so there's no in-protocol write confirmation; the correction surfaces on
> the connector's own next snapshot/delta instead). Option (A) — the
> file-level `.vsf` editor — remains unimplemented and unneeded unless a
> future edit category (court/time reassignment, cosmetic renames) falls
> outside what (B) can reach.

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

## Round-trip test (2026-11-21)

Built as `bridge/VsfEditTool.cs`: `--roundtrip`, `--edit` and `--ids` (usage in
`bridge/CLAUDE.md`). Edits go through AES's own public model, the same calls its
UI makes: `Court.ScheduleMatch` (the grid drag), `SchedulerFile.UpdateWithRemoteEntryData`
with a `MatchData` payload (remote score entry), and the `Team.Name` / `Match.FullName`
setters. The file is written with `SchedulerFile.Save(filename)`, the call AES's own
File > Save makes (`Scheduler.SaveVSF`). Caching is never enabled on a file that gets
saved: AES itself only calls `EnableCaching()` on throwaway copies for its reports.

**Bridge-side results** (one 2026 AAU nationals wave file, format 660209715, 117
teams, 27 courts, 630 matches, before play started):

- **Unknown 4 answered: a load/save is not byte-identical, but the data is.** The
  file keeps its size and only the order of the user records changes. AES holds users in
  a `Hashtable` (`SchedulerFile._Users`), whose order isn't stable, so AES's own saves
  reshuffle them too. Every other byte matches, and an independent parser
  (`gavin-aes-scripts/aes_vsf.py`) sees the same 1,527 user records once sorted. The tool
  reports this as "yes, except user order" and treats it as identical.
- **Edits stay put through a reload.** A team rename, a match rename, a two-set score
  and a move to a free slot on the same court all came back from AES's own loader. The
  independent parser found exactly those changes and nothing else: 4 score values,
  `outcome`, `lastScoreDateTimeEntered`, the two names and `scheduledStart`.
- **A move into an occupied slot is refused.** AES's drag would quietly unschedule the
  match already there; the tool names it and stops instead.

Two smaller files the same day:

- **A 2023 invitational (current format, 24 courts, 483 matches, 12 scored):** the
  unchanged round trip is byte-identical.
- **A finished 2025 one-day event (format 660209713, one version older; 8 courts, 59
  matches, all scored, crossovers and Gold/Silver brackets):** loading converts it up. The
  saved file is 278 bytes bigger, and the independent parser shows only the fields added
  since that version, filled with empty defaults (`workTeamSourceMatchId`,
  `showTieNotification`, `notes`). The converted file is then stable.
  Edits on it: a team rename, a match rename, a move to a *different* court (an empty one),
  and a rescore that **flips the winner of a Gold bracket match**. After reload, AES's own
  team text for the next match shows the new winner in place of the old one: AES works out
  bracket teams from the earlier result each time it loads, it doesn't store them. The
  next match keeps its own result, though, which now belongs to a team that didn't play it.
  AES does the same when a director corrects a score by hand, so an editor should warn when a
  change flips a result that already fed a played match. Exactly those edits changed.

**In the real AES Scheduler (v1.1.0.5090, the copy bundled with the sync client):**

- **Unknown 1: AES opens every file we wrote, with no error, warning or repair prompt.**
  That covers the unchanged 2023 file, the converted 2025 file and the edited 2025 file.
  The welcome screen's counts (teams, divisions, courts, matches, unfinished) match the
  bridge's.
- **The edits show on AES's own screens.** On the Court Schedule grid, Silver Match 2 sits
  on Dunes 2 at 3:00 PM and Dunes 4's 3:00 slot is empty. In Results Entry, Gold M2 reads
  Dunes 14 White def. Teal 25-20 25-18. Gold M4 and the final M5 show White where Teal was,
  with the results those matches already had (the cascade described above). In a second
  copy, a renamed team that was entered in a division shows under its new name in Initial
  Seeds. Match full names (`Match.FullName`) don't appear on any screen we checked. Team
  names come from registration imports, so an editor shouldn't rename teams anyway.
- **Unknowns 2–3: AES keeps our edits through its own save.** Save As from AES produced
  a file with exactly the same data as ours (independent parser: zero differences, same
  size). So AES recomputed nothing on load that overwrote the edits, and skipping
  `EnableCaching()` left nothing stale.

**AES goes online on file open, even when nobody signs in.** Its Network screen
(`NetworkControl.UpdateInterface`) connects to `syncservice.advancedeventsystems.com` and
sends `RequestEventVolleystationIntegration` with the event's id, a read-only question.
Uploading the event (`PostEvent`, `EventUpdateAttached`) needs a sign-in or a started
routed server, so the test files themselves never left the machine. But "AES offline"
can't be achieved just by not signing in: block the test copy's network (a firewall
rule for its exe, or no network at all) before opening test files.

**Conclusion:** writing a `.vsf` through AES's own model and `Save` produces files AES
accepts and keeps, including scores, court and time moves, and an older format converted
up. Option (A) is viable for a web builder's export, as long as it runs where AES's
assembly is available (the connector). Open: reseeds and bracket rewiring (step 3), and
the warning an editor needs before changing a result that already fed a played match.

## Headless host loop (2026-11-21)

A spike for Relay holding an event's `.vsf` on event day instead of the AES Scheduler:
apply court moves, scores, finishes and reseeds, save, and build what AES's publish would
send. Built as `bridge/HostBatch.cs`, `--host-batch <in.vsf> <out.vsf> <changes.json>
[<payload.bin>]` plus `--plays` to pick targets (usage and the JSON shape in
`bridge/CLAUDE.md`). **It never opens a network connection.** Cross-checked with
`tools/vsf_xcheck.py`, which reads both files with the independent parser.

**How each change goes in, all through AES's own model:**

| Change | Call | Same as AES's |
|---|---|---|
| `move` | `Court.ScheduleMatch`, refusing an occupied slot | grid drag (`--edit move`) |
| `score` | `UpdateWithRemoteEntryData`, `MatchData` payload | remote score entry (`--edit score`) |
| `finish` | `UpdateWithRemoteEntryData`, `FinishData` payload `[fileId, 33282, playId, rank or "" per slot]` | remote finish entry; the shape `--encode-finish` builds |
| `reseed` | `Play.TeamAssignment.ReseedSeed` | `ReseedTeamsControl` |
| `reseed-flag` | `Play.TeamAssignment.RankWillBeReseeded` (design change) | the Reseeds list in `EditPoolControl` / `EditBracketControl` |

**What the source says about reseeds** (`ReseedTeamsControl`, `Play.cs`, `Round.cs`):

- A reseed is the one property, `ReseedSeed`. The Reseed screen calls nothing else; it only
  redraws. Its drag-drop clears whichever team already holds the seed, then sets it.
  Correction to earlier notes: the `ReseedSeed` **setter doesn't clear a duplicate, it
  throws** ("You cannot assign a reseed seed to two plays in a given round"). The control
  does the clearing, and so does `--host-batch`.
- `RankWillBeReseeded` is a flag on the **finish position**, not the team: `Teams[i]`'s flag
  means "whoever finishes `i + 1`th gets reseeded". A team is reseedable
  (`Round.ReseedingTeams`) once it has an entry seed and a finish rank at a flagged
  position. The seeds on offer (`Round.ActualReseedingSeeds`) are those teams' normal exit
  seeds, so a reseed only reshuffles seeds among the reseeding teams.
- Until a team has its `ReseedSeed`, its `ExitSeed` is null and the next round's slot shows
  **"Pending Reseed"**. Setting the flag clears every reseed in the round.
- A reseed lives only in the file: no live-protocol payload carries it (the four
  `RemoteEntryUpdateType`s are scores, finishes, tiebreaker seeds and officials).

**Format check.** Every `.vsf` mode (`--host-batch`, `--plays`, `--roundtrip`, `--edit`,
`--ids`) now prints the input's format version and refuses one newer than the referenced
assembly's `SchedulerFile.CURRENT_VERSION`, read from the assembly by reflection (660209715
for v1.1.0.5090). It also refuses a header ≤ 0 and turns any other load error into a clear
message. Tested with a copy whose header was bumped to 660209716: refused with exit 1,
nothing written. (AES's own `Load` throws a bare "File format newer than loader!" at the
first read, so nothing was half-loaded before either. The check gives the clear message
and a non-zero exit.)

**The test file:** a copy of a finished 2025 one-day event (format 660209713, 26 teams, 8
courts, 59 matches, 16 plays: 12s and 13s with two pools into Gold/Silver, 14s with three
pools, a crossover round and Gold/Silver).

**This event has no reseeds.** `--plays` and the independent parser agree: every slot in
every play has `rankWillBeReseeded = false`, every `reseedSeed` is empty, and no round has
reseeding teams. A `reseed` on it is refused ("is not a reseeding team ... this round has
none"). To test the mechanism anyway, a second run first flagged it the way the pool
editor would (the `reseed-flag` change), on a copy only.

**Run 1, the file as it is** (move, score, two finishes):

```
move   match -50073 (14s pool A) Dunes 1 -> Dunes 2, 08:00 (empty court)
score  match -50073 25-21 25-16 (first team won) -> 20-25 22-25 (second team won)
finish 12s pool A (-50010) [2,1,4,3] -> [1,2,4,3]   (swaps 1st and 2nd)
finish 14s pool C (-50088) [3,1,2]   -> [3,2,1]     (swaps 1st and 2nd)
```

Both finishes needed a clearing payload first: AES applies ranks slot by slot and its
setter throws if another slot still holds the rank. The tool detects that and sends
`[.., "", "", ..]` first, like the monitor's `_finish_steps`. Reload: identical to the
edited model (879 values). Independent parser, after removing the 80 format-upgrade paths
that a plain `--roundtrip` of this file also shows: exactly 11 values differ, all of them
listed above (the score's outcome, 4 set values and `lastScoreDateTimeEntered`, the court id,
and 4 finish ranks). What AES now shows:

- 12s Gold (-50028): slot seed 1 now shows the Apex team and seed 4 the CityLights team
  (swapped), in Gold matches -50029, -50030, -50031, work teams included.
- 14s crossover: XO1 (-50111) seed 3 and XO2 (-50115) seed 4 swap teams. Because the
  crossovers' own finish ranks stay on their slots, the cascade reaches the 14s Gold
  bracket (-50128 seeds 2 and 4, matches -50138, -50141) too. That's the same cascade the
  round-trip test saw: a changed upstream result moves teams into slots whose results were
  played by someone else. Not specific to finishes: AES works out every downstream slot from
  per-slot finish ranks each time.
- The score change alone moved nothing: `UpdateWithRemoteEntryData` doesn't auto-finish a
  pool. AES's auto-finish (`EnableAutoFinish`) lives in the GUI (`ResultsEntryControl`,
  `NetworkControl.HandleScoreKioskMatchFromFinishTeam`): when every match has an outcome and
  scores, it sets ranks from `Play.SuggestedFinishOrder`. A host must do that step itself.

**Run 2, reseed** (on a flagged copy). First `reseed-flag` rank 1 of 12s pools A and B:
the two winners become the round's reseeding teams with seeds {1, 2}, both slots get a null
exit seed, and 12s Gold slots 1 and 2 (matches -50029, -50030, -50031) now show
"Pending Reseed (1)" / "Pending Reseed (2)", as AES shows before the director reseeds. Then
`reseed` pool A's winner -> seed 2 (by play + slot) and pool B's winner -> seed 1 (by round +
team code). Gold now shows pool B's winner as seed 1 and pool A's as seed 2, the reverse
of the normal placement. Independent parser: exactly the two `rankWillBeReseeded` flags, then
exactly the two `reseedSeed` values.

**Run 3, everything in one batch** (flags, move, score, finish, reseed, `forceResultsImport:
true`): pool A's finish is swapped so Apex wins, then Apex -> seed 2 and pool B's winner ->
seed 1. Gold shows pool B's winner (1) v Apex (2). Reload identical. The independent parser
found exactly 13 values changed: the 11 above minus the 14s finish (not in this batch), plus 2
flags and 2 reseeds.

**Refusals checked:** reseeding a team at an unflagged position, a seed not on offer, a move
into an occupied slot, a duplicate rank, a rank list of the wrong length, and a
newer-format input. Each stops before saving with a message saying why.

**PostEvent payload.** `--host-batch ... <payload.bin>` writes the plaintext frame AES's
publish sends (`NetworkControl.UpdateThreadLoop` -> `NetConnection.SendCommand(PostEvent,
object[] { SchedulerFile.Save(), forceResultsImport })`): `0x23`
(`CommandWithObjectData`), `9216` LE, a 3-byte length, a flags byte (gzip via AES's own
`Colib.Util.MiscUtils.CompressData` above 4,096 bytes), then the BinaryFormatter body. On
this file: about 6.9 KB on the wire for a 23.7 KB body. The tool reads it back the way
`ReceiveCommand`/`GetData` would (object[2], same file bytes, same flag) and loads the
embedded file. The Python check confirms the header and finds the saved file byte for byte
inside the decompressed body. Not built: the stream cipher, which is per connection, and
the `AuthenticationCommit` frame (the director's AES email and password) that AES sends
right before.

**Still unknown:**

1. **Whether AES's sync service accepts a PostEvent from something other than the
   Scheduler**, and what it checks (client version, `LastUpdatedTimestamp`, FileID
   ownership, the account). Not testable without connecting, which this spike must not do.
   Note that AES never bumps `LastUpdatedTimestamp` on edit (only `new SchedulerFile()` sets
   it), so the server can't be relying on it to spot changes from the desktop app either.
2. **The handshake and auth to `syncservice.advancedeventsystems.com:18743`** are AES's
   `NetConnection` (same cipher family as port 17471, presumably), but nobody here has
   checked them against that server.
3. **Reseeds on a real reseeded event.** Run 2 used AES's own flag setter on a copy, so the
   mechanics are AES's, but no file the director built with reseeds has been through it. A
   file that needs nothing extra: one with reseeded positions set in the pool or bracket
   editor, finish ranks on those positions, then `reseed` per team.
4. **Auto-finish:** the host needs its own copy of the GUI's `SuggestedFinishOrder` step
   (above). Not built.
5. **Opening a host-written file in the real Scheduler:** this spike's outputs weren't
   opened in AES. The round-trip test showed AES opens model-saved files; reseeded and
   finish-edited files are the same kind, but that's not checked.

## End to end on a director-built test event (2026-11-21)

A hand-built test event (`manualAddition`, event id -50000), made in AES by the director for
this purpose: 4 teams in one pool, all four pool positions flagged for reseeding, a 4-team
Gold bracket, two courts. Hand-built events publish through "Update Now" like any other (AES
hides only "Synchronize Data" for them); the server knows them by the event name
(`ServerSafeKey`), so the event was renamed to something unique before its first post.

1. **Baseline:** the director published the unedited file from AES. AES's public API
   (`/api/event/{key}/courts/{date}/240`) showed the 9 matches on Court 1 and Gold as
   "Pending Reseed (1) v Pending Reseed (4)" and "Pending Reseed (3) v Pending Reseed (2)".
2. **Edit:** `--host-batch` on a copy: scores for all six pool matches, finishes 1–4, reseed
   seeds 2, 1, 4, 3 (the reverse of finishing order for the top two and bottom two), and Gold
   M1 moved to the empty Court 2. Reload identical; the independent parser found exactly those
   values changed (scores, outcomes, score timestamps, 4 finish ranks, 4 reseed seeds, one
   court id); the PostEvent payload contains the saved file byte for byte.
3. **AES:** the director opened the edited file. The Court Schedule shows Gold M1 on Court 2;
   Results Entry shows the six scores and finishes 1–4, all finished; the Reseed screen
   shows Dos 1, Uno 2, Quatro 3, Tres 4 with matching finish ranks and reseed seeds.
4. **Publish with AES's own client** ("Update Now" from the edited file). The public API then
   showed every pool match decided with the right sets, Gold M1 as Dos (1) v Tres (4) on
   Court 2 and M2 as Quatro (3) v Uno (2). The final still reads "Winner of Match 1 v Winner
   of Match 2".

**So a file edited outside AES, reseed included, opens in AES, publishes, and AES's server and
results site show it correctly.** The first reseed on a file a director built works.

5. **Publish without the Scheduler** (`bridge/Publish.cs`, `--publish <file> --key
   <ServerSafeKey>`). A second batch on the published file scored Gold M1 and M2 and moved the
   final to Court 2. The bridge then did what "Update Now" does, using AES's own
   `Colib.Util.NetConnection` from the referenced assembly: open
   `syncservice.advancedeventsystems.com:18743` with "SyncService Protocol v3.1",
   `AuthenticationCommit` with the director's AES email and password (typed by the director at
   the console, password masked, never printed or saved), then `PostEvent` with
   `{ Save(), forceResultsImport: false }`. The server answered `AuthenticationValid` and
   `PostEventSuccessful`; the results site showed both Gold results and the final as Dos (1) v
   Uno (2) on Court 2 at 5:00. The AES Scheduler was closed throughout.

**The whole chain now works without the Scheduler: edit the file through AES's model, then
publish it with AES's own connection class.** It still runs AES's assembly (from the
connector, on the director's machine). Not tested: a real (non hand-built) event, a server
that has moved to a newer protocol, and what the server does when two clients publish the
same event.

## Key Files Referenced

- `C:\Git Repos\aes-decompiled\EventScheduler\AES\Scheduler\Model\SchedulerFile.cs` — Save/Load symmetry
- `C:\Git Repos\aes-decompiled\EventScheduler\AES\Scheduler\Model\Match.cs` — bracket source/destination links, Set scores
- `C:\Git Repos\aes-decompiled\EventScheduler\AES\Scheduler\Model\Division.cs` — FinalPlaces (gold/silver/bronze), seed resolution
- `C:\Git Repos\aes-decompiled\EventScheduler\AES\Scheduler\Model\Play.cs` — TeamAssignment seed chain
- `C:\Git Repos\aes-decompiled\EventScheduler\AES\Scheduler\Model\Bracket.cs` — PlotMatchPositions, root match ID arrays
- `C:\Git Repos\aes-decompiled\EventScheduler\EventScheduler\Controls\NetworkControl.cs:549` — confirms EventUpdateAttached == Save()
- `bridge/AESBridge.cs` — existing reflection pattern, where `--roundtrip` would go
- `CLAUDE.md` — Outstanding Issues, RemoteEntryUpdate bidirectional note
