# Dashboard Outbox API (score write-back)

**This is the implementation spec for the dashboard repo (aes-tourney-director).
Nothing in this file is implemented there yet — it's the contract to build
against.** The connector (`aes-sync-connector`) already implements its side:
it polls `GET .../outbox`, sends any returned corrections into AES over the
existing live connection, and acks the result via `POST .../outbox/{id}/ack`.
See `CLAUDE.md`'s "Score Write-Back — connector-side mechanics" section in
that repo if you need to understand how the connector consumes what you
return here, but it isn't necessary to implement this side.

Feature is opt-in on the connector (`[aes] allow_writeback = true` in
`aes_config.ini`, default `false`) — until a director explicitly turns it on
for their event, the connector never calls these endpoints. Scope: score and
outcome corrections only — no work-team reassignment, no seed/bracket edits.

**Check `writebackEnabled` before showing any correction UI.** Every
`/api/ingest/snapshot` payload (the connector's existing full-state push, sent
on connect and every ~3 minutes) includes a `writebackEnabled` boolean
mirroring that connector's `allow_writeback` config value. If it's `false` (or
the connector predates this field and it's simply absent), the connector never
polls `GET .../outbox` at all — any command you queue would sit unacked
forever with the director having no idea their correction was silently
ignored. Gate the "correct this score" UI action on the most recent snapshot's
`writebackEnabled` being `true`, and treat a stale/missing snapshot (no push
in the last ~3-5 minutes) the same as `false` — the connector may be offline
entirely.

## GET /api/ingest/outbox
Auth: `Authorization: Bearer <INGEST_API_KEY>` (same per-event key already used
for `/delta` and `/snapshot` — scopes the request to one event/connector
implicitly, no separate event-id param needed).

Returns pending (not-yet-acked) score-correction commands for this event, oldest
first. Recommend capping the response (e.g. 20) and collapsing to only the
latest command per `matchId` if multiple corrections for the same match are
queued — older ones are superseded, not meaningful to apply in sequence.

Response 200:
```json
{
  "commands": [
    {
      "id": "cmd_abc123",
      "type": "score_correction",
      "matchId": -51376,
      "outcome": "FirstTeamWon",
      "sets": [{"team1": 25, "team2": 10}, {"team1": 25, "team2": 12}],
      "createdAt": "2026-07-10T18:02:11Z",
      "requestedBy": "adam@example.com"
    }
  ]
}
```
- `id`: dashboard-assigned, unique, used for ack.
- `outcome`: optional. One of `Undecided | FirstTeamWon | SecondTeamWon | Tie |
  FirstTeamForfeit | SecondTeamForfeit`. Omit to leave the match's current
  outcome unchanged.
- `sets`: **required if the correction touches scores.** Must be the FULL
  corrected list of sets for the match, in order — not a partial diff. AES's
  own apply logic clears any set slot beyond what's sent, so an incomplete
  list silently wipes later sets. Populate this from the dashboard's own
  current view of the match (itself fed by our `/delta` and `/snapshot`
  pushes), not from a single-field edit. Omit entirely only for an
  outcome-only correction (e.g. converting a match to forfeit without
  touching set scores) — the connector falls back to the last sets it saw
  for that match.
- `requestedBy`: optional, audit/log display only.

## POST /api/ingest/outbox/{id}/ack
Auth: same Bearer key. Sent once per command, after the connector has
attempted to apply it (successfully or not).

Request:
```json
{ "status": "applied", "detail": null }
```
`status` is one of:
- `applied` — sent to AES successfully. Terminal: do not re-return this
  command id from GET again. This does **not** confirm AES's UI updated — AES
  does not echo write-backs back to the sender on the same connection (it only
  rebroadcasts to *other* connected clients), and there is no application-level
  ack in the protocol. The correction should show up in the dashboard's own
  state on the next `/delta` or `/snapshot` push from the connector (worst
  case ~3 minutes later, on AES's own periodic broadcast timer) — treat that
  as the actual confirmation signal, not this ack.
- `rejected` — the connector validated the command as malformed (bad outcome
  value, empty sets on a decided match, etc.). Retrying the identical command
  will fail again; surface this to the director rather than auto-retrying.
- `failed` — transient (AES not connected, subprocess timeout, tournament
  file ID not yet known at connector startup). Safe to leave pending and let
  it come back on a later GET, or resubmit.

Response 200: any body, dashboard just needs to record the ack. Ack delivery
should be idempotent — a duplicate ack for an already-terminal command (e.g.
the connector retried after a network blip) should not error.

Recommend: if a command has been outstanding (returned by GET, no ack seen)
for longer than ~2 minutes, treat it as lost (connector likely restarted
before sending) and include it again in the next GET response.

## Confirmed working (2026-07-10)
The connector side of this was verified live against a running AES test
instance — score corrections apply, outcome/winner flips apply, and
crucially, fields this feature doesn't touch (work-team assignment) survive
unchanged. So the piece you're building here is the only missing link between
a director clicking "fix this score" in the dashboard UI and it actually
landing in AES.
