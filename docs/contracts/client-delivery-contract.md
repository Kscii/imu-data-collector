# CW12EU-T client HDF5 delivery service contract

Status: 1.0.0

Canonical physical HDF5 contract:
[`imu-hdf5-v3.2.md`](imu-hdf5-v3.2.md)

## 1. Immutable object layout

```text
client-deliveries/<snapshot_id>/hdf5-v1/cw12eu-client-<snapshot_id>.h5
client-deliveries/<snapshot_id>/hdf5-v1/manifest.json
client-deliveries/<snapshot_id>/hdf5-v1/job.json
```

The HDF5 object is uploaded and verified first. The immutable manifest is written last and is the
only ready marker. `job.json` is mutable operational state and is not part of the immutable
delivery. Existing ZIP v2 objects are historical and are not runtime inputs.

## 2. Delivery manifest

The sidecar uses `schema_version = "cw12eu_client_hdf5_delivery_v1"` and
`contract_version = "1.0.0"`. Required fields are:

- source identity: `snapshot_id`, `snapshot_content_fingerprint`,
  `snapshot_created_at_utc`;
- format identity: `hdf5_schema_version = "3.2.0"`,
  `artifact_profile = "client_delivery"`, `content_type = "application/x-hdf5"`;
- counts: `recording_count`, `video_bytes`;
- audit: `generated_at_utc`, `generated_by`;
- artifact descriptor: `artifact_object_key`, `artifact_size_bytes`,
  `artifact_sha256`.

The whole-file digest is external and must agree across the manifest, GCS object metadata and
`X-Content-SHA256` download header. The HDF5 file must not contain its own whole-file digest.

## 3. API

The existing paths retain their meaning:

- `POST /api/v1/training-snapshots/{snapshot_id}/delivery` queues idempotent generation;
- `GET /api/v1/training-snapshots/{snapshot_id}/delivery` reports eligibility, state, schema,
  contract, size and digest;
- `GET|HEAD /api/v1/training-snapshots/{snapshot_id}/delivery/download` returns
  `application/x-hdf5` as `cw12eu-client-<snapshot_id>.h5`.

The status response may include `stage`, `bytes_complete`, `bytes_total`, `progress_percent`,
`started_at_utc`, and `updated_at_utc`. Stages are `queued`, `preparing`, `copying_videos`,
`validating`, `uploading`, `finalizing`, `ready`, `failed`, or `interrupted`. The service persists
job state so a process restart turns an abandoned active job into an explicit retryable
`interrupted` failure.

A full GET may redirect to a short-lived GCS V4 signed URL. HEAD, ranged requests, and deployments
without signing permission use the authenticated application proxy. The proxy supports one RFC
9110 byte range and returns 200, 206 or 416. Published files are immutable. A failed task cleans its
local partial file and may be retried; concurrent requests for the same snapshot share one
background job.

## 4. Privacy and compatibility

The artifact excludes raw BLE notifications, raw counts, mutable reviews, UniKeys, emails and
reversible participant mappings. Embedded videos can identify participants and require separate
authorisation.

Readers accept only HDF5 3.2 profiles defined by the shared physical contract. They do not silently
upgrade HDF5 3.1 or ZIP deliveries. Breaking changes require a new HDF5 schema/profile or delivery
contract major and a new immutable object prefix.
