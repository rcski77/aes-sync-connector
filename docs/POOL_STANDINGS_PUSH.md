# Per-pool standings push (`POST {endpoint}/pool`)

Handoff from the dashboard repo (`aes-tourney-director`, branch `ingest-pool-push`). The dashboard
side is built; this is the connector change that makes it do anything.

## Why

Pool standings reach the dashboard only in the full snapshot, which `monitor()` throttles to
`SNAPSHOT_INTERVAL = 180` s. Deltas (`/delta`) carry matches only. So after a pool's last result, the
dashboard shows the match as final right away, but its standings, tiebreaker badge
("playoff required" etc.) and pool flyout stay stale for up to 3 minutes.

The connector already has fresh standings far sooner. `CMD_EVENT_UPDATE` (16400) arrives within
about 2 s of a score entry, carrying the full SchedulerFile, and the bridge recomputes every pool's `standings`
from it. We just don't send them until the next snapshot. The fix is to send only the pools
whose standings changed, on every event update.

## The dashboard contract

`POST {endpoint}/pool`: same base URL, `Authorization: Bearer <ingest_key>` and Cloudflare Access
headers as `/delta` and `/snapshot`. It returns `200 {ok:true}` once queued, `400` for a missing field,
`401` for a bad key, and `422` for an unknown event.

```json
{
  "aesEventId":    "12345",
  "aesEventIdKey": "<same as snapshot>",
  "observedAt":    "2026-07-03T14:02:17.123Z",
  "pool":          { ...exactly one _pool_payload(p, gold_spots_map) dict... }
}
```

- `pool` is **exactly** what `_pool_payload()` already builds for `snapshot.pools[]`: same
  function and same rules (roster order, `finishRank` null until AES confirms, no placeholder teams). Reuse it; don't
  build a second shape.
- `observedAt` is when these standings were read (UTC ISO 8601). **Use the same clock as
  `snapshotTime`.** The dashboard stores the observed time with each pool's standings. A pool or snapshot
  write observed *earlier* than what's stored is ignored for standings, so an in-flight snapshot
  can't roll back a newer push.
- The endpoint is optional server-side: older connector builds keep working through snapshots only.
  A dashboard that doesn't have the endpoint yet (`prod` until it's promoted) answers `404`. That's
  harmless, since `_post` just logs it, but see "Rollout" below.

Full server-side docs are in `aes-tourney-director/docs/ingest-api.md` under "`POST /api/ingest/pool`" and
"Pool Standings".

## Changes in `monitor/aes_monitor.py`

1. **`push_pool(pool_payload, tournament_data, observed_at, base_url, ingest_key, timeout, cf_headers)`**,
   next to `push_snapshot`. It builds the body above (`aesEventId` / `aesEventIdKey` exactly as
   `push_snapshot` derives them) and POSTs to `base_url.rstrip('/') + '/pool'` on a daemon thread through
   `_post(..., label=f"pool({playId})")`.

2. **Change detection.** Keep a per-connection dict `last_sent_pools: {playId: signature}`, reset
   alongside `last_snapshot = None` at the top of the reconnect loop. The signature is a stable
   `json.dumps(..., sort_keys=True)` of the fields that matter to standings:
   `teams` (the whole list: name, matchesWon/Lost, setsWon/Lost, pointRatio, finishRank, exitSeed)
   plus `goldSpotsCount`. Don't include `date`/`courts`/format fields, or cosmetic churn would trigger pushes.

3. **In the `CMD_EVENT_UPDATE` branch**, after `curr` is parsed and when `curr and base_url`:
   - Take one `now = datetime.now(timezone.utc)` and use it for both the snapshot's `snapshotTime` and
     every pool's `observedAt` from this update. Build them from the same `curr`, so they describe the same moment.
   - Compute `gold_spots_map = _compute_gold_spots(curr)` once. `push_snapshot` computes it internally
     today, so either pass it in or let `push_snapshot` accept `now`/the map. Don't compute it twice per update if
     it's easy to avoid.
   - **If a snapshot is due on this update** (first connect or 180 s elapsed): push the snapshot as
     today, then set `last_sent_pools` to the signatures of every pool in it. The snapshot already
     delivered them, so don't also push them individually.
   - **Otherwise**, for each pool in `curr['pools']`, build `_pool_payload(p, gold_spots_map)` and
     compare its signature to `last_sent_pools.get(playId)`. For each pool that differs, call `push_pool(...)` and update
     `last_sent_pools[playId]`. Pools whose signature is unchanged send nothing.
   - Skip pools with no `poolId` (`playId` None) or with an empty `teams` list. The server
     ignores empty teams anyway, but don't spend a request on it.
   - Log a single line per update that pushed anything, e.g. `pushed standings for 2 pool(s): P3, P7`.

4. **Timestamp precision.** Give `snapshotTime` millisecond precision (`...%S.` + ms + `Z`, or
   `isoformat(timespec='milliseconds')` with `Z`) and use the same formatter for `observedAt`.
   Today it's whole seconds. The dashboard's comparison is "older than stored → skip", so equal timestamps are
   fine, but consistent precision keeps the ordering exact.

Nothing changes in `bridge/`, since the standings are already in `tournament_data.json`.

## Edge cases to keep in mind

- **Bursts:** AES can send several event updates in a row. The signature check means each real change
  goes out once, so no debounce is needed.
- **Reconnect:** `last_sent_pools` resets and the first update sends a snapshot, which re-baselines every pool.
- **Standings that change back** (a score correction) differ from the last-sent signature, so they get pushed. That's correct.
- **Thread ordering:** pushes run on daemon threads and can arrive out of order. That's fine, because
  the server orders by `observedAt`, not arrival.

## Docs to update in this repo

- `monitor/CLAUDE.md`: the `CMD_EVENT_UPDATE` row (currently "Parse via AESBridge → throttled
  snapshot POST") should also mention the per-pool push on change. Add a short section on the `/pool` payload,
  the signature fields and the `observedAt`/`snapshotTime` clock rule.
- `claude.md`: the flow diagram line `on EventUpdate (throttled 3 min) → POST /api/ingest/snapshot`
  should gain `+ changed pools → POST /api/ingest/pool (every update)`.

## Testing

- The python side has no automated tests. Run it against a real or local AES Scheduler with the dashboard
  on `dev` (or a local dashboard at `http://localhost:3000/api/ingest` running `npm run dev` and `npm run sync:dev` from the
  `ingest-pool-push` branch).
- Enter a pool result in Scheduler. Within a few seconds the connector should log `pool(<playId>) OK (200)`
  for just that pool. The dashboard's sync log shows `[ingest:pool] SUCCESS event=… pool=…`, and the grid's
  tiebreaker badge and pool flyout update without waiting for the snapshot.
- Enter a score in a *different* pool and confirm only that pool is pushed. On an idle event update,
  nothing should be pushed.

## Rollout

The dashboard endpoint is on branch `ingest-pool-push` → `dev` first, then `prod`. A connector release
that calls `/pool` against a `prod` that doesn't have it yet just logs `HTTP 404` per changed pool, and
snapshots keep working. It's still best to promote the dashboard change to `prod` before shipping this connector
release to venues.
