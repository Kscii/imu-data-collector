# Client HDF5 delivery

A client delivery binds to one immutable training snapshot. From **Training snapshots**,
request a background build of `cw12eu-client-<snapshot_id>.h5`. Repeating the same
request shares/reuses the same contract-specific job and immutable result.

The file uses `imu_schema_version="3.2.0"`, `artifact_profile="client_delivery"`:

- `/samples`, `/sequences`, `/annotations`: 25 Hz six-axis SI and labels;
- `/media/index`, `/media/videos/*`, `/media/timing/*`: original MP4 bytes and time mappings;
- `/labels/catalog`, `/labels/sequence_versions`: frozen taxonomy names and versions.

It excludes raw BLE, counts, capture H5, mutable review, operator IDs, emails, and
reversible identity mappings. Embedded video can identify participants and still
requires appropriate data authorization.

## Build and verify

The builder reads only frozen training data, video, view mappings, and taxonomy.
It checks disk space, writes a unique temporary file, closes it, and verifies the
core tables, timing, label references, physical video ranges, per-video hashes,
and whole-file hash. It verifies uploaded content before publishing the manifest.

Progress persists as queued/preparing/copying_videos/validating/uploading/finalizing/
ready/failed. A process restart exposes an abandoned job as `interrupted`, allowing
an explicit retry. Failed local partial files are removed; source artifacts remain.

The whole-file SHA-256 lives outside the HDF5: in its manifest, object metadata,
and download header. Full downloads may redirect to a short-lived GCS V4 signed
URL; expiry is release-dependent. HEAD, ranged downloads, and installations without
signing permission use the authenticated application proxy (200/206/416).
Obtain a new link through the platform if a signed URL expires.

Do not run `h5repack` or modify a delivery in place: frozen physical MP4 offsets
would become invalid. Validate the received whole-file digest against the manifest.

## Read elsewhere

The [browser viewer](https://viewer.imu.kscii.tech/) accepts the 3.2 training and
client profiles and reads a selected file locally. Client files include playable
video; training files expose sensor/annotation data without media. The viewer uses
piecewise-linear frame-time mappings, not a fixed video offset. Whole-file hash
checking is an explicit operation for large files.

The viewer does not grant access to hosted datasets. Delivery and retention remain
separate from the viewer/software license.

For independent readers, use [Data formats](data-format.md), the complete
[physical HDF5 contract](contracts/imu-hdf5-v3.2.md), and the
[delivery service contract](contracts/client-delivery-contract.md).
