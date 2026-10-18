# External device history

This optional module archives watch and mattress history from the read-only
Synerglobal API. It is disabled by default. Authorized members can browse and
download archived data; synchronization settings, updates, and rebuilds require
administrator access. Disappearing upstream devices retain their archived history.

## Browse

Open **External device data** (`?view=external`). Select a device, metric, and time
range. The default is the past week; presets cover 24 hours, 7/30/365 days, or all
history. Selection is retained in URL parameters `device`, `metric`, `range`, and
custom `from`/`to`. Devices with no archived records are grouped separately.

Queries use server `receivedAt` in half-open intervals `[start,end)`. Storage uses
UTC, while display and input use **UTC+8**, regardless of interface language.
Device-reported timestamps are retained separately.

Charts aggregate by visible range and width. Numeric metrics show mean and min/max;
state metrics use modal step values with latest-state tie-breaking. Cumulative
counters use the last value; interval steps are summed. Empty buckets are not zero.
Synthetic and unmarked records remain separate. Optional lines across empty buckets
are visual aids and do not create or interpolate source records.

Known conversions include `distance_km = LK.DTC / 100` and
`energy_kcal = LK.KCAL / 10`; original fields remain available. Unknown units/codes,
including unverified sleep-report fields, are shown as raw values. Missing report
values are not filled with zero, and state counts are not inferred durations.
Watch UALM and mattress WARNING events can be inspected with their original payloads;
DROP is an upstream code, not proof of a real fall.

## Storage and synchronization

Complete upstream responses are immutable gzip JSON archives with manifests and
hashes under `external-device-data/synerglobal/`. Independent SQLite databases index
records, coverage, aggregates, and tasks. Browsing does not call the upstream API.
Raw archives are committed before index transactions and coverage advancement.

The default worker interval is 15 minutes. The configurable history origin defaults
to `2026-09-01 00:00 UTC+8`; it is a requested range, not provider-confirmed absence
of earlier history. New devices backfill from this origin. Existing devices fill
gaps and continue from successfully queried coverage, not the last record time.
An empty successful response advances coverage; a failed interval does not.

Normal updates add unseen identities without overwriting existing records. Upstream
corrections/deletions are reconciled by an explicit full rebuild. Queries begin
with at most one-day windows and split when exceeding the response/time budget
(default 16 MiB / 75 seconds). One worker serializes requests under a process lock.

A full rebuild requires confirmation of the origin and replacement behavior. It
builds a separate generation while the old generation remains readable, then switches
only after complete coverage and SQLite integrity checks. Existing archives and
old generations are retained. Requests and exports bind to one generation.

`external-rebuild-index` instead reconstructs indexes from accepted existing archives;
it does not refetch upstream history. Projection backfills are checkpointed and
transactional. New per-device metrics become visible only when ready, while existing
curves remain readable. Recovery does not overwrite current records with obsolete
archive versions.

## Downloads

A device download exports all its archived records, independent of the chart range.
A type download includes all archived devices of that type, including devices no
longer returned upstream. ZIP files contain `records.json`, `records.csv`, and
`manifest.json`, organized by platform device ID for multi-device exports.

JSON preserves source fields/types and payloads. CSV retains its established columns
and escapes spreadsheet-formula strings; appended metrics are described in the
manifest. The manifest records scope, coverage gaps, synthetic counts, byte sizes,
and SHA-256. Partial coverage is never labeled complete history. Downloads support
a single HTTP byte range and remain authenticated.

## Configure and run

Set `external_devices.enabled: true` in private server YAML. `access: members` is
the default; `access: admins` limits browsing as well. Management stays admin-only.
Put `IMU_SYNERGLOBAL_API_KEY` in a private EnvironmentFile for the worker. Only the
worker needs it; never embed it in frontend configuration or exported files.
The provider endpoint is `https://api.synerglobal.com.au`, with normal TLS checks
and no followed redirects.

```bash
imu-annotation --config /etc/imu-annotation/config.yaml external-sync
imu-annotation --config /etc/imu-annotation/config.yaml external-sync-once
imu-annotation --config /etc/imu-annotation/config.yaml external-rebuild-index
```

`external-sync-once` runs a scheduler cycle and at most one window, not a complete
backfill. Stop the worker before rebuilding an index from archives. The browser's
full rebuild is performed by the running worker. Adapt the service account in
[the worker unit](../configs/systemd/imu-external-devices.service) for self-hosting.

## API reference

All paths below are relative to `/api/v1/external-devices/` and require platform
identity. Device IDs are platform identifiers, mapped to full provider identities.

| Method and path | Purpose |
| --- | --- |
| `GET status`, `GET devices` | Worker/task state and device catalog |
| `GET devices/{id}/calendar?year=&month=` | UTC+8 coverage calendar |
| `GET devices/{id}/records?start=&end=&cursor=&message=` | Paginated raw records |
| `GET devices/{id}/records/{record_id}` | Original record and version metadata |
| `GET devices/{id}/metrics` | Metrics, units, available range, projection preparation |
| `GET devices/{id}/series?start=&end=&metric=&point_budget=` | Aggregated values, counts, synthetic markers, coverage |
| `GET devices/{id}/fields` | Field frequency and interpretation status |
| `GET devices/{id}/reports?valid=&start=&end=&cursor=&limit=` | Sleep-report list |
| `GET devices/{id}/reports/{record_id}` | Report summary and original fields |
| `GET devices/{id}/events?start=&end=&point_budget=` | Alarm buckets |
| `GET devices/{id}/events/records?start=&end=&cursor=&limit=` | Paginated alarms |
| `POST sync` | Admin update; coalesces active equivalent tasks |
| `POST rebuild` | Admin full rebuild with `{confirmed:true,history_start}` |
| `PUT settings` | Admin history-origin update; blocked during active work |
| `POST history` | Provider-confirmed history origin using `start` and `confirmed_by_provider:true` |
| `POST exports` | `{scope:"device",device_id}` or `{scope:"kind",kind:"mattress"/"radar-watch"}` |
| `GET exports/{id}`, `GET exports/{id}/download` | Export status and download |

`point_budget` is 100–2000 target buckets per source series, including empty buckets.
Legacy calendar, default-metric series, and range-export requests remain compatible.
English display metadata uses additional `_en` fields; raw keys, values, and JSON
are never translated or rewritten. External archives are not six-axis training HDF5.
