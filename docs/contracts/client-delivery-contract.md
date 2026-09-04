# CW12EU-T client delivery contract

Status: v2

Canonical repository: `Kscii/imu-data-collector`

Consumers: `Kscii/imu-hdf5-viewer` and authorised client-side audit tools

## 1. Versioning and immutability

The package manifest uses `schema_version = "cw12eu_client_delivery_v2"` and
`contract_version = "2.0.0"`. A package is an immutable derivative of one immutable training
snapshot. A change to required paths, field meaning or synchronization semantics requires a new
major version and a new object prefix. Existing package objects must never be overwritten.

The v2 object layout is:

```text
client-deliveries/<snapshot_id>/v2/manifest.json
client-deliveries/<snapshot_id>/v2/cw12eu-delivery-<snapshot_id>.zip
```

The sidecar beside the ZIP may contain protected operational audit fields such as the generating
team member and generation time. These fields must not be copied into the client package.

## 2. Archive layout

All ZIP members use `ZIP_STORED`, are unencrypted, and use ZIP64 when required.

```text
dataset/cw12eu.h5
recordings/<four-digit-sequence-index>/video.mp4
recordings/<four-digit-sequence-index>/view.json
taxonomies/<taxonomy-id>/<taxonomy-version>.json
manifest.json
README.md
DATASET_CARD.md
SHA256SUMS
```

The package excludes raw BLE notifications, raw sensor counts, capture HDF5 files, mutable review
documents, UniKeys, email addresses and reversible participant mappings. Video may identify a
participant and must be handled under the separately agreed data-use terms.

## 3. Manifest

`manifest.json` is UTF-8 JSON and contains:

- package identity: `schema_version`, `contract_version`, `snapshot_id`,
  `snapshot_content_fingerprint`, and `snapshot_created_at_utc`;
- physical data contract: `hdf5_schema_version = "3.1.0"`, `sampling_rate_hz = 25.0`,
  `coordinate_frame = "sensor_local"`, `gravity_retained = true`, and the ordered six `channels`;
- `video_contains_identifiable_participants = true` and
  `content_hash_verification = "available_not_required_by_viewer"`;
- `taxonomies`, with `taxonomy_id`, `version`, and package-relative `path`;
- `recordings`, with anonymous `participant_id`, identity-neutral `recording_id`,
  `sequence_index`, merged half-open sample bounds, and package-relative video, view, and taxonomy
  paths;
- `files`, whose entries contain `path`, `size_bytes`, `sha256`, `role`, and any applicable
  recording or taxonomy identity.

Paths are relative, unique, contain no `..`, and must resolve to regular archive members. The
manifest does not list itself or `SHA256SUMS` to avoid recursive digests.

## 4. HDF5 and synchronization

`dataset/cw12eu.h5` follows the project HDF5 3.1.0 contract. `/samples` is `float32 [N,6]` in this
fixed order: acceleration x/y/z in m/s², then angular velocity x/y/z in rad/s. `/sequences` maps
merged sample ranges to recordings. `/annotations` stores half-open activity/exclusion intervals
and zero-width onset/impact events.

Each `view.json` identifies its HDF5 `sequence_index`, merged sample bounds, recording-relative
25 Hz sample times, original video media times, annotations, and the exact taxonomy identity.
Video frames and IMU samples are separate clocks and must be related through this frozen mapping;
their indices are never treated as one-to-one frames.

Each taxonomy document uses `schema_version = "cw12eu_activity_taxonomy_v1"` and contains only
`taxonomy_id`, `version`, and `fall`/`non_fall` entries with `code`, `name`, and `active`. Internal
change actors and audit history are excluded.

## 5. Viewer behaviour

The public viewer accepts this complete ZIP or a standalone HDF5 3.1.0 file. It performs local
structural validation but does not automatically recompute content SHA-256. It must clearly state
that content hashes were not checked. A standalone HDF5 has no frozen taxonomy or video, so the
viewer displays stable codes only.

Unknown major versions and legacy v1 packages are rejected with an instruction to regenerate a
v2 package. Unknown optional fields within v2 are ignored. The viewer is read-only and must not
upload, edit or persist client data beyond explicitly labelled transient browser storage.

## 6. Cross-repository synchronization

The viewer repository stores an exact read-only copy of this document plus a lock containing the
canonical collector commit, path, SHA-256 and contract version. Contract updates start in the
collector, then update the viewer copy, parser and fixtures in a paired change. CI rejects a lock
whose digest does not match the copied document.
