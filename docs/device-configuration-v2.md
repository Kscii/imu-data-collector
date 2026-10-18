# Device configuration snapshots

Device snapshots describe how to interpret sensor data. They never replace raw
bytes or change existing recordings. Snapshot content uses schema `2.0`.

## Configuration layers

| Layer | Location | Behavior |
| --- | --- | --- |
| Complete device snapshot | `device-config/v2/snapshots/<cfg-id>.json` | Immutable |
| Approval review | `device-config/v2/reviews/<cfg-id>.json` | Candidate, approved, or revoked; revision-controlled |
| Current snapshot pointer | `device-config/v2/current.json` | Compare-and-swap updates |
| Audit events | `device-config/v2/events/` | Append-only |
| Local workspace | User data `device-config/workspace.json` | Editable, auto-saved |
| Local snapshots | User data `device-config/local/` | Immutable, test-only |
| Validated cache / last known good | User cache `device-config/` | Atomically replaced after validation |
| Local BLE bindings | User data `device-bindings.json` | Rebuildable connection hints |

Host paths, camera selection, ports, OAuth, storage, identities, and secrets belong
to private runtime configuration. Activity labels have their own versioned taxonomy.
Neither belongs in a publishable device snapshot.

## Snapshot contents

A snapshot contains the complete device collection, not a patch. Each device
includes SN/asset revision, previous revision, discovery identity, optional public
address, service UUIDs, GATT fingerprint, firmware evidence, reviewed `protocol_id`,
expected rate and evidence status, permitted data tiers, audit-document path, and
an SI profile with scales, raw biases, axis mapping, method, and evidence hashes.

Protocol parsers ship with the application; snapshots cannot download executable
code. Unknown snapshot schemas are rejected in favor of the last validated cache.
An unknown protocol disables only that device within an otherwise recognized snapshot.

## Identity and production gates

`IMU-NNNN-RNN` identifies one physical asset and hardware/firmware interpretation.
A change affecting bytes, timing, range, or units needs the next revision. Recalibration
alone creates a new `si-…` profile in a new snapshot. The SI ID is derived from the SN
and normalized SI content. The broker reserves asset/revision numbers atomically;
numbers may be skipped but never reused.

Production requires an approved snapshot, a prod-enabled device, and verified SI.
Local and candidate snapshots are test-only. Revoked snapshots retain historical
references but cannot start new sessions. The exact, code-reviewed bootstrap lock
is the explicit offline exception; the annotation server must derive the same
bootstrap identity before accepting it.

## Local workflow

The app follows the most recently validated Current, falling back to the Git
bootstrap when no cache exists. An explicit snapshot choice persists and is not
changed by background refresh. Restore-following-Current and device/snapshot
switching require idle, released hardware.

Use the structured workspace to edit values; it auto-saves after about 900 ms.
Freezing a local snapshot does not submit it for review. Publishing a candidate is
an authenticated broker operation. Nearby-device discovery scans advertisements
for about five seconds without connecting or registering a sensor.

Candidate SI may be used for diagnostics and stored as non-authoritative test
metadata. Copying a candidate into a workspace clears authoritative evidence,
sets `verified=false`, and restricts it to test. It never approves the device.

## Review and evidence

Members can submit snapshots and reserve SNs. Administrators approve candidates,
revoke non-current approved snapshots, and change Current. Duplicate content is
rejected even if the title differs. Approval and Current changes use optimistic
locking; refresh before retrying a conflict. Snapshots are never overwritten.

Capture HDF5 `1.9.0` and configuration-bearing manifest `3.2.0`/`3.3.0` freeze the snapshot ID/hash, content hash,
source, approval observed at capture, last-check time, SN, device-profile hash,
and SI profile ID. Export evaluates approval at `captured_at_utc` from append-only
events rather than substituting today's status. Older HDF5 `1.8.0` and manifests
`3.0/3.1` remain readable without inventing v2 references.

## Bootstrap maintenance

`configs/device-config-bootstrap.lock.json` pins registry and calibration-evidence
inputs plus the complete derived snapshot. Input/hash disagreement disables it.
Do not regenerate it merely to tidy documentation or rename an audit path.

```bash
uv run imu-collector migrate-device-configuration-v2
```

This previews a migration. Inspect the plan token, output, and snapshot before
using the reported explicit apply confirmation. Applying writes a local lock;
`cloud_objects_written` must be zero. Cloud submission/approval stays separate.

Validate offline fallback, invalid hashes, unknown schema/protocol, all approval
gates, manual selection, concurrent changes, captured hashes, and old-data reading
when changing configuration behavior. Real hardware acceptance remains separate.
