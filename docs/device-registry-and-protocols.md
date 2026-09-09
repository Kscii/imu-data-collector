# IMU device registry and protocol integration

> v0.3.0 已把运行权威迁移到完整的
> [设备配置 Snapshot v2](device-configuration-v2.md)。本文仍保留协议接入与旧 v1 Git 输入的
> 说明；`configs/imu-devices.yaml` 现在是生成安装包 bootstrap lock 的迁移输入，不再是团队
> Current 或审批状态的唯一权威。

## Identity model

The authoritative device identity is a project SN, not an advertised BLE name, address, or
platform-local CoreBluetooth UUID. SNs use `IMU-NNNN-RNN`:

- `IMU-NNNN` identifies one physical asset.
- `R01`, `R02`, ... identify firmware/data-contract revisions of that asset.
- A firmware change that may alter bytes, timing, units, or ranges receives a new revision SN.
  The old profile is retained and the new profile names it in `supersedes_sn`.

The Git bootstrap input is [`configs/imu-devices.yaml`](../configs/imu-devices.yaml), and its exact
v2 migration is frozen in `configs/device-config-bootstrap.lock.json`. A capture freezes the
selected configuration Snapshot/content hashes, `sensor_sn`, SI Profile ID and resolved device
profile hash into HDF5 1.9. Manifest 3.2 repeats that reference.

There is no implicit default device. The capture UI must send a selectable SN before preview or
recording starts; the backend rejects a missing, retired, or unknown SN before it opens BLE. A
firmware/data-contract change receives a new SN even when the physical enclosure is unchanged.

## Lifecycle and production gate

The legacy v1 input uses three lifecycle states:

- `commissioning`: available for preview, diagnostics, and `test` captures only;
- `verified`: may enable `prod` only when it references reviewed calibration evidence;
- `retired`: retained for provenance but cannot be selected for a new session.

The backend enforces the gate. A frontend choice cannot turn a commissioning device into a
production device. Candidate conversion coefficients are displayed in diagnostics only;
unverified capture `values_si` remains NaN and raw packet bytes/counts remain authoritative.

## Local onboarding

The capture page can scan recognized advertisements. New permanent SNs and revisions are reserved
centrally from the “设备与设置” page, then added to the local v2 workspace. Local immutable
Snapshots and team candidates are test-only until an administrator approves the team Snapshot.

A local or candidate Snapshot can be used for protocol verification and test capture. Candidate
values can drive the live diagnostic plot and are frozen as non-authoritative metadata, but
canonical `values_si` remains NaN. Production authority requires an approved Snapshot and verified
SI Profile; the capture client cannot grant either authority.

Unknown writable characteristics must not be exercised during onboarding. A successful
connection is accepted only after a notification matches the selected code-reviewed parser.

## Local platform bindings

`device-bindings.json` is schema 2 and maps each SN to a platform-local identifier. This mainly
supports macOS CoreBluetooth UUIDs. Bindings are connection accelerators, not device identity;
name, notification characteristic, and parser signature are checked again when connecting.

## HDF5 protocol storage

Capture schema 1.9 keeps the old 16-byte CW12EU contract unchanged and adds exact configuration
Snapshot provenance. For
`acce_gyro_abf0_v1`, every 22-byte ABF2 notification is one sample and stores:

- six little-endian signed 16-bit raw counts;
- the full little-endian unsigned 64-bit device-local millisecond counter;
- the fixed `0D 0A` suffix (preserved in the raw packet payload).

The device counter is not world time. It is mapped to host monotonic time per uninterrupted
clock epoch. Test captures retain reset epochs for diagnosis; a reset blocks production
validation. The current observed rate is approximately 50 Hz, not 25 Hz.

## Legacy v1 distribution and v2 replacement

The following v1 commands remain for transition and older clients:

```bash
imu-collector --config /path/to/private.yaml publish-device-registry
```

The publisher bundles every referenced calibration-evidence file, then creates an immutable object
at `device-registry/v1/snapshots/<sha256>/registry.json`, reads it back, validates the schema and
hash, then compare-and-swaps `device-registry/v1/current.json`. It never edits an existing
snapshot. `registry_revision` must increase whenever content changes; publishing a lower revision
or different content under the same revision is rejected.

A desktop client fetches `current` and its referenced snapshot from the existing OAuth upload
broker. Both endpoints require the same authenticated team identity as uploads; the desktop never
receives GCS credentials. Startup continues immediately with the bundled or last-known-good (LKG)
registry and refreshes only while capture, preview, and background finalization are idle.

An administrator or diagnostic host can also refresh directly from its configured object store:

```bash
imu-collector --config /path/to/private.yaml refresh-device-registry \
  --output /path/to/cache/imu-devices.json
```

A refresh validates the pointer, snapshot hash, document hash, revision, and every referenced
calibration-evidence hash before activating the registry. Calibration-evidence paths are immutable:
an existing path with different content is rejected, so a new calibration uses a new per-SN/revision
path. Startup revalidates the complete LKG rather than trusting a previously written JSON file. A
failed download or validation leaves the previous LKG untouched and the UI reports the failure.
Promotion of a new profile still happens through reviewed Git changes before publication.

New clients use `device-config/v2`: members submit full immutable candidates through the OAuth
broker; administrators approve/revoke and set Current in the annotation UI. See the v2 document
for the object keys, local/LKG behavior, bootstrap migration and acceptance gates.

## Acceptance checklist for a new revision

1. Record advertisement identity, address type, complete GATT surface, and no-write boundary.
2. Preserve representative raw notifications and prove frame boundaries/byte order.
3. Measure rate from a device clock when available; distinguish it from callback burst timing.
4. Verify device-clock reset and wrap behaviour, including an actual power cycle.
5. Perform multi-orientation acceleration and controlled gyro characterization.
6. Add reviewed calibration evidence and firmware identity to the profile.
7. Run parser, capture, manifest, production-gate, and frontend tests.
8. Only then set lifecycle to `verified` and independently enable `prod_capture_enabled`.
