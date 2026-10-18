# Data formats and interoperability

This document is a reader-oriented reference for other projects consuming IMU Data
Collector files. Reading the formats does not require the collector application,
its database, a Google account, or its Python package. Python examples use only
`h5py`, `numpy`, and the standard library. Obtain files through an authorized export.

Pin this document to a release/commit when implementing an integration. The
[HDF5 3.2 contract](contracts/imu-hdf5-v3.2.md) is normative for derived HDF5;
[the handoff contract](contracts/annotation-benchmark-contract.md) defines published
dataset/model interchange. This guide explains how to consume those formats and
the raw capture/review formats; it does not create a new schema version.

## 1. Identify the artifact before reading it

| Artifact | Version discriminator | Intended consumer |
| --- | --- | --- |
| Raw capture HDF5 | `capture_schema_name="imu_capture_hdf5"`, `capture_schema_version="1.9.0"` | Protocol, timing, calibration, and provenance analysis |
| Capture publication JSON | `schema_version="3.3.0"` for current configuration-bearing publications | Recording importer; describes H5/MKV/MP4 and frozen device/configuration |
| Review JSON | `schema_version="3.0.0"` | Annotation/synchronization integration; private mutable state |
| Training HDF5 | `imu_schema_version="3.2.0"`, `artifact_profile="training_dataset"` | ML pipelines and dataset tools |
| Client HDF5 | `imu_schema_version="3.2.0"`, `artifact_profile="client_delivery"` | Video/sensor/annotation viewers |
| Dataset manifest | `schema_version="imu_benchmark_dataset_manifest_v2"`, `handoff_contract_version="1.0.0"` | Dataset discovery and integrity checking |
| Client-delivery manifest | `schema_version="cw12eu_client_hdf5_delivery_v1"`, `contract_version="1.0.0"` | Delivery/download integrity checking |

These version namespaces are independent. Capture manifest **3.3** does not mean
its H5 is schema 3.3. Optional synthetic-motion **HDF5 3.3** is another producer's
contract and is not covered by the real-data 3.2 layout below. External device
JSON/CSV archives also retain their own formats; see
[External device history](external-device-history.md#downloads).

Use attributes, not a filename such as `aligned25.h5`, to identify data.
Partial/finalizing files are not published artifacts. Unknown schema versions or
profiles need explicit support, not a guessed fallback.

## 2. Common conventions

- Strings are UTF-8. HDF5 readers may return bytes; decode them explicitly.
- JSON timestamps are UTC ISO 8601. Recording-relative timestamps are integer
  nanoseconds; retain integer precision when storing or comparing boundaries.
- Intervals are half-open `[start,stop)`. Frame/sample indices are zero-based.
- Acceleration is specific force in m/s² with gravity retained; gyro is rad/s.
- The six derived channels are ordered `ax,ay,az,gx,gy,gz` in the verified
  sensor-local frame, not a world frame. Read the frozen device profile for orientation.
- Raw counts are signed integers, not SI. Missing/unverified SI is NaN, never zero.
- Physical SHA-256 identifies file bytes. `logical_content_sha256` identifies a
  defined logical sequence stream and is not the hash of the file or concatenated tables.
- Raw recordings, exports, and published manifests are immutable. Review/current
  pointers are separate mutable objects; never edit a published H5 in place.

## 3. Raw capture HDF5 1.9.0

A finalized recording normally has a separate H.264 Matroska video. The H5 stores
sensor evidence and video frame metadata, not video bytes. IMU-only calibration
captures can have no video; check `recording_kind` and `video_status`.

### Root attributes

| Attribute | Meaning |
| --- | --- |
| `capture_schema_name`, `capture_schema_version` | Raw-format identity |
| `recording_id`, `collection_id` | Recording and collection identity; modern recording IDs are UTC timestamps |
| `identity_contract_version`, `participant_assignment_source` | Capture identity rules; participant assignment occurs in review |
| `data_tier` | `test` or `prod`; prod is intent, not proof of training eligibility |
| `body_location`, `protocol_id` | Capture context; inspect `imu` metadata for the sensor byte protocol |
| `started_at_utc`, `ended_at_utc`, `duration_ns` | Recording timing and duration |
| `recording_start_monotonic_ns`, `clock_domain` | Host-clock origin/domain |
| `host_os`, `host_os_version`, `host_architecture`, `monotonic_implementation` | Host timing provenance |
| `feature_columns`, `feature_units` | JSON arrays describing derived channels |
| `sensor_sn`, `device_profile_sha256`, `device_registry_revision` | Device interpretation provenance |
| `configuration_snapshot_id`, `configuration_snapshot_sha256`, `configuration_content_sha256` | Frozen complete configuration references |
| `configuration_source`, `configuration_approval_state`, optional `configuration_checked_at_utc` | Configuration authority observed at capture |
| `si_profile_id`, `calibration_profile_id`, `calibration_verified` | SI interpretation and verification status |
| `state`, `recording_kind`, `training_eligible`, `video_status` | Capture/finalization state and purpose |
| Packet/sample/error counts | Diagnostics; do not substitute them for complete validation |

An H5 `state="finalized"` means the writer closed it; it does not prove publication
or training acceptance. Read the publication manifest and review/export status too.

### Sensor datasets

Let P be notifications, B total payload bytes, and N parsed samples.

| Path under `/imu/` | Type / shape | Meaning |
| --- | --- | --- |
| `packets/payload_values` | uint8 `[B]` | Concatenated, unmodified notification bytes |
| `packets/payload_offsets` | int64 `[P+1]` | Notification p is bytes `[offsets[p]:offsets[p+1]]` |
| `packets/receive_time_ns` | int64 `[P]` | Host monotonic callback arrival |
| `packets/fitted_packet_end_time_ns` | int64 `[P]` | Reconstructed packet-end time |
| `packets/fit_residual_ns` | int64 `[P]` | Timing fit residual |
| `packets/sample_count` | uint16 `[P]` | Parsed samples in each notification |
| `packets/parse_valid` | bool `[P]` | Parser result; interpret together with packet kind |
| `packets/packet_kind` | uint8 `[P]` | 1 sample, 2 known auxiliary, 255 unknown/invalid |
| `samples/raw_counts` | int16 `[N,6]` | Raw acceleration/gyro counts |
| `samples/values_si` | float32 `[N,6]` | Derived SI; NaN where conversion is unverified |
| `samples/packet_index` | int64 `[N]` | Source notification index |
| `samples/sample_in_packet` | uint16 `[N]` | Position within that notification |
| `samples/time_monotonic_ns` | int64 `[N]` | Reconstructed sample clock |
| `samples/recording_time_ns` | int64 `[N]` | Sample time minus recording origin |
| `samples/aligned_video_time_ns` | int64 `[N]` | Capture-time compatibility column; not current review alignment |
| `samples/time_quality` | uint8 `[N]` | 1 provisional, 2 packet-fit reconstruction, 3 device-clock reconstruction |
| `samples/trailer` | uint8 `[N,4]`, CW12EU-T only | Uninterpreted original trailer bytes |
| `samples/device_time_ms` | uint64 `[N]`, ABF2 only | Device-local milliseconds, not UTC |
| `samples/device_clock_epoch` | uint32 `[N]`, ABF2 only | Device clock epoch/reset grouping |
| `connection_events/event`, `notes` | UTF-8 `[E]` | Connection diagnostics |
| `connection_events/time_monotonic_ns` | int64 `[E]` | Connection-event time |

Known auxiliary packets can contain zero samples and remain valid raw evidence.
Unknown payloads must remain intact for later analysis. Do not infer a universal
sample layout from the HDF5 extension: the device protocol determines decoding.

The `/imu` attributes freeze byte-protocol, firmware, BLE backend, profile, rate,
fit diagnostics, scale factors, and JSON arrays `accel_bias_counts_json`,
`gyro_bias_counts_json`, `raw_axis_order_json`, `axis_signs_json`.
For a verified profile, conversion subtracts bias in raw-axis space before
reordering/signs, then uses `9.80665 / accel_counts_per_g` and
`(pi/180) / gyro_counts_per_dps`. Do not transplant another device's factors.

### Video and other groups

`/video/frames/` contains equal-length `pts_monotonic_ns`, `recording_time_ns`,
`media_time_ns`, `duration_ns` (int64), and `key_frame` (bool) datasets. Video
attributes describe filename/hash, codec, dimensions, timing, backend, and diagnostics.

`media_time_ns` is for browser seeking. `recording_time_ns` is for annotation;
`pts_monotonic_ns` preserves the host clock. Never align by `frame_index / 30`.
`/sync` and `/annotations` contain capture-time compatibility state. Later review
edits belong to external `review.json`. `/experiment/stages` preserves calibration
stage boundaries and notes where applicable.

### Minimal raw reader

```python
from pathlib import Path
import h5py


def text(value):
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def read_capture(path: Path):
    with h5py.File(path, "r") as f:
        if (text(f.attrs.get("capture_schema_name", "")),
                text(f.attrs.get("capture_schema_version", ""))) != (
                "imu_capture_hdf5", "1.9.0"):
            raise ValueError("Unsupported raw capture schema")
        samples = f["imu/samples"]
        packets = f["imu/packets"]
        offsets = packets["payload_offsets"][:]
        first_packet = b""
        if len(offsets) > 1:
            first_packet = packets["payload_values"][offsets[0]:offsets[1]].tobytes()
        return {
            "time_ns": samples["recording_time_ns"][:],
            "raw_counts": samples["raw_counts"][:],
            "values_si": samples["values_si"][:],  # Preserve NaN; do not fill with zero.
            "first_packet": first_packet,
        }
```

This reads evidence, not a production-eligibility verdict. For large captures,
slice datasets in chunks instead of loading all samples.

## 4. Publication manifest and review JSON

A current capture publication has H5, MKV, and MP4 descriptors with `role`,
`object_key`, `filename`, `size_bytes`, `sha256`, and `content_type`. The JSON also
contains recording/collection IDs, `identity_mode`, data tier, body location,
capture UTC time, duration, source H5 version, software revision, `calibration`,
`sensor`, `configuration`, and `quality_warnings`. The current configuration-bearing
writer emits 3.3.0; legacy paths can emit 3.0/3.1. Do not infer H5 version from it.
The manifest is written last and must agree with the objects and frozen H5 metadata.

Review schema 3.0.0 has these top-level fields:

| Field | Meaning |
| --- | --- |
| `recording_id`, `revision` | Identity and optimistic concurrency revision |
| `sources` | Raw H5/MKV filenames, sizes, and hashes |
| `sync` | Anchors, `policy="conditional_fixed_offset_v1"`, `apply_fixed_offset`, reviewer |
| `annotations` | Taxonomy ID/version, revision, finalized flag, segments/events/exclusions |
| `workflow` | `unassigned`, `in_progress`, or `completed`, owner/last editor/time |
| `participant_assignment` | Private participant selection/confirmation and provenance |
| `active_export` | Current immutable export reference, or null |

An activity segment includes `segment_id`, integer `start_ns`/`end_ns`,
`binary_label` (`fall` or `non_fall`), `activity_code`, `annotator_id`, confidence,
and notes. Exclusions are separate intervals with ID, reason, author, and notes.
Events contain `segment_id`, `kind` (`onset`/`impact`), `time_ns`, source video/IMU
indices, and author. Finalized intervals plus exclusions cover the entire recording
without gaps or overlaps. Each fall has an onset at its start and one impact inside.

Sync anchors retain start/end roles, IMU/video times and indices, preceding sample/
frame boundaries, and reviewer. With confirmed compensation,
`video_time = imu_time + (d_start+d_end)/2`; otherwise the offset is zero.
`scale=1`. See [Synchronization rules](annotation-and-sync.md#synchronization-anchors).
Do not rewrite raw H5 timestamps to apply a review.

An active export records its own schema/HDF5/rate, source review revision, object
key, filename, size, physical/logical hashes, calibration evidence, and creation time.
Follow this reference rather than choosing the newest-looking export filename.
Review can contain operator/participant identities; it is not the anonymous
training-data interchange format.

## 5. Training HDF5 3.2.0

The root contains **exactly** `/samples`, `/sequences`, `/annotations`.
Required attributes include `imu_schema_version`, `artifact_profile`, `dataset_id`,
`sampling_rate_hz=25.0`, `axis_frame="sensor_local"`, `hdf5_compatibility="1.14"`,
`evaluation_role`, `feature_columns`, sequence/sample/annotation counts, and
`logical_content_sha256`.

`/samples` is float32 `[N,6]` in this order:

```text
acceleration_x_mps2, acceleration_y_mps2, acceleration_z_mps2,
angular_velocity_x_rad_s, angular_velocity_y_rad_s, angular_velocity_z_rad_s
```

`/sequences` is a one-dimensional compound dataset:

| Fields | Types |
| --- | --- |
| `sample_start`, `sample_stop` | int64; global half-open sample range |
| `source_file`, `participant_id`, `recording_id`, `body_location` | UTF-8 |
| `activity_code`, `supervision_kind` | UTF-8 |
| `is_fall` | bool |
| `source_sampling_rate_hz` | float64; original rate, not output-grid rate |

Ranges are nonempty, contiguous, and cover all sample rows exactly once. Modern
real-data participant IDs use `cw12eu:subject-NNN`; recording IDs use
`cw12eu:<UTC timestamp>`. A sequence may contain multiple activities/falls; use its
annotation rows for temporal supervision rather than treating it as one event.

`/annotations` is a one-dimensional compound dataset:

| Field | Type / interpretation |
| --- | --- |
| `sequence_index` | int32; row in `/sequences` |
| `kind` | UTF-8: `activity`, `exclude`, `onset`, `impact` |
| `start_sample`, `stop_sample` | int64; **sequence-relative**, not global sample indices |
| `code` | UTF-8 activity or reserved exclusion code |

Activity/exclude ranges cover a temporal sequence without gaps/overlaps. Point
annotations have equal start/stop. Every fall interval has one same-code onset at
its start and one impact strictly inside. Reserved exclude codes are `sync_tap`
and `other`; they are not taxonomy activities. Preserve exclude annotations and
omit excluded ranges/windows from training; do not simply delete rows and collapse time.

At sample k, sequence-relative time is `k/25` seconds. Export interpolates within
the common valid IMU/video range without extrapolating. Interval boundaries/onset
use ceiling to the grid, while impacts use nearest-sample rounding with half-step
ties later. Raw acquisition is not forced to 25 Hz; snapshot merge does not resample
an already derived sequence.

### Portable training reader

```python
from pathlib import Path
import hashlib
import h5py


def text(value):
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def read_training(path: Path, expected_sha256: str):
    # Obtain the expected digest from the trusted immutable manifest.
    with path.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != expected_sha256:
        raise ValueError("File digest mismatch")
    with h5py.File(path, "r") as f:
        if text(f.attrs.get("imu_schema_version", "")) != "3.2.0":
            raise ValueError("Unsupported HDF5 schema")
        if text(f.attrs.get("artifact_profile", "")) != "training_dataset":
            raise ValueError("Expected training_dataset profile")
        if set(f.keys()) != {"samples", "sequences", "annotations"}:
            raise ValueError("Unexpected training root layout")
        if float(f.attrs["sampling_rate_hz"]) != 25.0:
            raise ValueError("Unexpected output rate")
        annotations = f["annotations"][:]
        for index, sequence in enumerate(f["sequences"]):
            start, stop = int(sequence["sample_start"]), int(sequence["sample_stop"])
            x = f["samples"][start:stop]  # float32 [sequence length, 6]
            events = annotations[annotations["sequence_index"] == index]
            # events' start/stop values index x, not the complete /samples array.
            yield text(sequence["recording_id"]), x, events
```

This iterator is a minimal integration example, not a full contract validator.
Production importers should also validate shapes/dtypes, contiguous sequence ranges,
finite SI, annotation coverage/events/codes, counts, and logical conformance before
training. Recompute the logical digest using the contract's ordered sequence-local
canonical metadata and little-endian float32 stream, not a generic file/table hash.

## 6. Client HDF5 3.2.0

The core is identical to training HDF5, with `artifact_profile="client_delivery"`
and exactly two additional root groups, `/media` and `/labels`. Reject this profile
in ordinary benchmark discovery; use an explicit viewer/common-core reader for it.

| Dataset | Layout |
| --- | --- |
| `/media/index` | One compound row per sequence, ordered by `sequence_index` |
| `/media/videos/<i>` | Original MP4 bytes, contiguous uncompressed uint8 `[B]` |
| `/media/timing/<i>` | int64 `[F,2]`: `recording_time_ns`, `media_time_ns` |
| `/labels/catalog` | Compound frozen label definitions |
| `/labels/sequence_versions` | One compound taxonomy assignment per sequence |

Media-index fields: `sequence_index` (int32), `byte_length`, `file_offset`,
`media_duration_ns`, `sample_zero_recording_time_ns`, `sample_zero_media_time_ns`
(int64), and UTF-8 `sha256`, `content_type`, `container`.
The frozen byte range must match the reopened HDF5 dataset's physical offset,
length, and hash, be inside the file, and not overlap another video. Videos cannot
use compression, chunks, external/virtual storage, or variable-length frame arrays.

Both timing columns are strictly increasing and within recording/media ranges.
For sample k in sequence i:

```text
recording_time_ns = media_index[i].sample_zero_recording_time_ns + k * 40_000_000
media_time_ns = piecewise_linear_interpolation(timing[:,0], timing[:,1], recording_time_ns)
```

Only interpolate inside the recorded mapping; do not extrapolate or assume all
frames are exactly 1/30 second apart. A browser may read verified MP4 bytes using
`File.slice(file_offset, file_offset+byte_length)` without loading the entire H5.

Label-catalog fields are UTF-8 `taxonomy_id`, `taxonomy_version`, `code`, `name`,
and bool `is_fall`, `active`. Rows are unique by taxonomy ID/version/code.
Sequence assignments contain int32 `sequence_index` and UTF-8 `taxonomy_id`,
`taxonomy_version`. Resolve activity names against that frozen version, not today's
label list. Reserved exclude codes do not require taxonomy rows.

Deliveries exclude raw BLE/counts, mutable reviews, and reversible participant
mappings, but embedded video can still identify people. Never repack or modify the
file: physical video offsets would become invalid.

## 7. Integrity, downloads, and compatibility

Published manifests declare object keys, byte sizes, content types, and physical
SHA-256. Verify the trusted manifest and downloaded bytes before interpreting them.
A matching filename, HTTP 200, or matching size alone is insufficient. A whole-file
hash is never stored inside that same file.

Dataset `current.json` is a mutable pointer to an immutable snapshot and manifest
hash. Resolve it once and keep that snapshot identity for a reproducible experiment.
Training TARs organize per-recording exports; merged H5 organizes the same sequences.
Neither is a new resampling stage.

HDF5 downloads use `application/x-hdf5`; authenticated downloads support one byte
range (200/206/416), while full downloads may redirect to a short-lived signed URL.
Preserve checksums across transfers and request a new authorized link after expiry.

New training consumers should accept only explicitly implemented schema/profile
combinations. Legacy raw readers may be supported separately; do not relabel older
files as 1.9 or turn 3.1 training data into 3.2 by editing an attribute. For optional
fields, preserve unknown metadata where practical without inventing missing units,
identity, labels, or calibration. Breaking semantics need new artifact identity and
an explicit contract update, not an in-place migration.
