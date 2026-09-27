# Bracket standings: `playId` + `teams` in snapshots, and `POST {endpoint}/bracket`

Handoff from the dashboard repo (`aes-tourney-director`, branch `activity-feed`). The dashboard side is built.
This is the connector change that feeds it. It builds on the pool push from `docs/POOL_STANDINGS_PUSH.md`.

## Why

The dashboard's activity feed posts a red **"Finalize standings — Varsity R1XO2"** alert once every match in a
pool, crossover or bracket has a result. The card turns into **"Varsity R1XO2 standings final"**, showing AES's
official order, once staff have finalized the standings in AES Scheduler, which AES signals by filling in
a `FinishRank` for every team. Pools already work because pool payloads carry `finishRank`. Bracket
payloads don't carry the bracket's teams at all, so the dashboard can't tell when a crossover or bracket
has been finalized. Until the connector sends them, brackets get no finalize cards.

The bridge **already outputs everything needed** for each bracket in `tournament_data.json`:
- `bracketId` (the bracket's `Play.PlayID`), which is the same value its matches carry as `playId`;
- `teamAssignments[]`: `{ teamNumber, entrySeed, finishRank, reseedSeed, exitSeed, team }` from
  `Play.Teams`, where `finishRank` is null until staff finalize.

No bridge changes are needed; this is `monitor/aes_monitor.py` only.

## The dashboard contract

### 1. Two new optional fields on each `snapshot.brackets[]` entry

```json
{
  "division": "Varsity", "date": "2026-09-26",
  "bracketFullName": "...", "bracketShortName": "XO2", "bracketFullShortName": "R1XO2",
  "root": { ... },
  "playId": -50054,
  "teams": [
    { "name": "Forest Hills Eastern", "finishRank": null },
    { "name": "Ludington", "finishRank": null }
  ]
}
```

- `playId`: `b['bracketId']`.
- `teams`: one entry per `b['teamAssignments'][]`, with `name` = `_strip_seed(ta['team'])` (the same
  suffix stripping as pool names) and `finishRank` = `ta['finishRank']`, which is null until finalized.
  Send teams in AES's order. The server ignores blank names and placeholder names like "Winner of Match 1"
  (a slot not filled yet), so you don't need to filter them, though skipping blanks is fine.
- Both fields are optional server-side. Without **both**, the bracket just gets no finalize notifications.

### 2. `POST {endpoint}/bracket`: one bracket, when its teams change

This mirrors `/pool` exactly: same auth header, same `observedAt` clock rule, and it returns `200 {ok:true}`,
`400`, `401` or `422`.

```json
{ "aesEventId": "...", "aesEventIdKey": "...", "observedAt": "2026-09-26T18:00:00.000Z", "bracket": { ...one brackets[] entry, with playId + teams... } }
```

The server stores only the bracket's standings from this push; the tree still comes from snapshots.

## Changes in `monitor/aes_monitor.py`

1. **`_bracket_payload(b)`**: add `'playId': b.get('bracketId')` and `'teams': [...]` as described above.
   `_bracket_payload` returns None for playoff brackets and for brackets with no root match yet. Keep that
   for the snapshot. For the push, see step 3, since a bracket's teams can matter even before its root match exists.

2. **`push_bracket(...)` and `_push_bracket_if_changed(...)`**: mirror the existing `push_pool` and
   `_push_pool_if_changed`. POST to `base_url.rstrip('/') + '/bracket'` with the body shown above and the
   label `f"bracket({playId})"`.

3. **Change detection, next to the pool one.** Add a per-connection `last_sent_brackets: {playId: signature}`
   and reset it with `last_sent_pools`. The signature is `json.dumps(teams, sort_keys=True)`; only the team list
   matters. On each `CMD_EVENT_UPDATE`:
   - When the snapshot goes out, set `last_sent_brackets` from every bracket in it, as with pools.
   - Otherwise, for each bracket that `_bracket_payload` returns (non-None), compare signatures and
     `push_bracket` the ones that changed. Use the same `now` as `observedAt`.
   In practice this fires when staff finalize a bracket in Scheduler, or when AES fills a bracket slot
   (a new team name). Both are cheap and rare.
   Wherever the pool push also reacts to a finalization *between* event updates (for example a `FinishData`
   remote entry, type 33282, carrying a Play's new finish ranks), do the same for brackets: check
   the PlayID's play type, update that bracket's cached `teamAssignments[].finishRank`, and push it.

4. **Docs:** add the `/bracket` push and the new bracket fields to `monitor/CLAUDE.md` and `claude.md`'s flow line,
   next to the `/pool` notes.

## Testing

- Point the connector at a local dashboard on the `activity-feed` branch: `npm run dev` plus `npm run sync:dev`.
- Finish a crossover in Scheduler. Within about 2 s the dashboard feed shows a red "Finalize standings — <division> R1XO<n>".
  Finalize that crossover's standings in Scheduler. The connector logs `bracket(<playId>) OK (200)`, and the red card
  turns into "<division> R1XO<n> standings final" with the official order.
- Do the same for a full bracket (e.g. R2Gold). Its finalize card appears once its **last** match has a result.

## Rollout

As with `/pool`: the dashboard needs this change on `prod` before a connector release that calls `/bracket`
ships to venues. An older dashboard answers 404 to the push, which is harmless. Snapshots with the extra fields are
fine against any dashboard version, because unknown fields are ignored.
