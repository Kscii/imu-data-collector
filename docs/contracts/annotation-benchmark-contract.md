# Annotation and benchmark data contract

Status: all three modules are frozen at v1. Canonical specification:
`Kscii/imu-fall-benchmark`. Audience: producer and consumer implementers.

This is an English translation of the pinned upstream specification. It changes
no fields, versions, or artifact semantics. The lock records both the upstream
original's commit/path/SHA-256 and this translation's path/SHA-256. Section 6
explains the source synchronization process and the local translation representation.

The contract defines persisted artifacts exchanged between repositories, not web
APIs, page layouts, databases, or deployment architecture.

## 1. Modules and versions

| Module | Version | Purpose |
| --- | --- | --- |
| `dataset_handoff` | `1.0.0` | Annotation platform publishes training HDF5 to the benchmark |
| `experiment_catalog` | `1.0.0` | Benchmark publishes experiment and per-model ONNX evidence |
| `model_release` | `1.0.0` | Benchmark publishes a fixed candidate model and its validation scope |

Modules evolve independently using SemVer. Before 1.0.0, fields may be removed,
renamed, or reinterpreted without forward compatibility, but specification,
implementation, tests, and locks must change in paired repository PRs.

For v1, readers accept compatible minor/patch versions within the same major:
required fields must be present and unknown optional fields are ignored. After
the first formal publication freezes a module at 1.0.0:

- Patch: clarify wording or fix implementation without changing semantics.
- Minor: add optional fields only; readers ignore unknown optional fields.
- Major: explicit approval for breaking changes, while old-major published data
  remains readable.
- Published objects are immutable; corrections require new IDs and object keys.

`dataset_handoff` and physical HDF5 3.2.0 froze with the first formal dataset.
The [HDF5 contract](imu-hdf5-v3.2.md) alone defines its physical layout.
Experiment and model contracts evolve independently.

## 2. Common rules

- JSON uses UTF-8; times use UTC ISO 8601; SHA-256 uses 64 lowercase hex characters.
- File descriptors contain `filename`, `object_key`, `size_bytes`, `sha256`, and
  `content_type`.
- Upload and verify content first; create immutable `metadata.json` or
  `manifest.json` last as the publication marker.
- Stable IDs allow only `[A-Za-z0-9._-]`; object keys cannot be absolute or contain `..`.
- The platform owns mutable `state.json`, allowing only `available`/`deprecated`.
  It is not benchmark evidence and cannot change immutable metadata.
- Pages browse, explain, and download. They do not run browser ONNX inference or
  automatically designate `current`, `recommended`, or `best`.

## 3. `dataset_handoff` 1.0.0

### 3.1 Artifacts and object layout

```text
benchmark-datasets/team/cw12eu/<snapshot_id>/datasets/cw12eu.h5
benchmark-datasets/team/cw12eu/<snapshot_id>/manifest.json
benchmark-datasets/team/cw12eu/current.json
```

`cw12eu.h5` uses `imu_schema_version=3.2.0` and
`artifact_profile=training_dataset`. Physical HDF5 schema and handoff versions are
independent: changing either does not automatically change the other.

The manifest contains at least:

```json
{
  "schema_version": "imu_benchmark_dataset_manifest_v2",
  "handoff_contract_version": "1.0.0",
  "kind": "team",
  "snapshot_id": "...",
  "files": [
    {
      "filename": "cw12eu.h5",
      "object_key": "benchmark-datasets/team/cw12eu/.../datasets/cw12eu.h5",
      "size_bytes": 1,
      "sha256": "...",
      "content_type": "application/x-hdf5",
      "hdf5_schema_version": "3.2.0",
      "artifact_profile": "training_dataset"
    }
  ]
}
```

`current.json` records `snapshot_id`, `manifest_object`, `manifest_sha256`, and
`handoff_contract_version`. Create and verify the immutable snapshot before switching
Current. Never point Current at an artifact that has not passed
`imu-bench data pull --team-snapshot <snapshot_id>` and `validate-team`.

The snapshot fingerprint includes the handoff version. Sort `recordings` by
`participant_id`, then `recording_id`, and construct:

```json
{"handoff_contract_version": "1.0.0", "recordings": []}
```

Encode UTF-8 JSON with sorted keys and compact separators (`,` and `:`), hash with
SHA-256, and use the first 24 hex characters as `snapshot-<digest>`. Identical
contract/content gives the same ID; a new contract version requires a new ID even
when recordings are unchanged. Never add fields to an old immutable manifest.

### 3.2 HDF5 3.2.0 requirements

- `/samples`: `float32 [N,6]`, 25 Hz SI, ordered `acceleration_x_mps2`,
  `acceleration_y_mps2`, `acceleration_z_mps2`, `angular_velocity_x_rad_s`,
  `angular_velocity_y_rad_s`, `angular_velocity_z_rad_s`.
- `/sequences`: `sample_start`, `sample_stop`, `source_file`, `participant_id`,
  `recording_id`, `body_location`, `activity_code`, `is_fall`, `supervision_kind`,
  `source_sampling_rate_hz`.
- `participant_id` is a stable anonymous `cw12eu:subject-NNN`. UniKeys, emails,
  names, and reversible variants are prohibited in training HDF5, manifests,
  snapshot fingerprints, and benchmark logs. The private append-only mapping
  belongs to the annotation server and is outside this handoff.
- `recording_id` is `cw12eu:<UTC timestamp>`. Recording IDs in source filenames and
  object keys cannot contain UniKeys.
- `/annotations`: `sequence_index`, `kind`, `start_sample`, `stop_sample`, `code`.
- `activity`/`exclude` use half-open `[start_sample,stop_sample)` ranges;
  `onset`/`impact` are points with equal start and stop.
- A temporal sequence can have zero, one, or multiple non-overlapping fall activities.
- Every finalized fall interval has exactly one same-code onset at its start and
  one same-code impact strictly inside it.
- `is_fall` is true exactly when a valid fall interval exists. Multiple falls do not
  become artificial recordings; count them individually through the event table.

HDF5 3.1 and handoff 0.x objects are immutable historical evidence, not current
runtime inputs. Readers must not silently migrate, supplement, or activate them.
Current writers/readers accept 3.2.0 / 1.0.0.

Raw BLE, counts, video, synchronization reviews, label management, and calibration
evidence stay with the annotation platform. Benchmark training consumes only SI
`training_dataset` HDF5 and cannot infer/rewrite raw evidence from it. The separate
`client_delivery` profile must not enter benchmark dataset catalogs.

## 4. `experiment_catalog` 1.0.0

### 4.1 Object layout

```text
benchmark-model-catalog/experiments/<publication_id>/onnx/<artifact_id>.onnx
benchmark-model-catalog/experiments/<publication_id>/result.tar.gz   # optional
benchmark-model-catalog/experiments/<publication_id>/metadata.json
benchmark-model-catalog/experiments/<publication_id>/state.json      # platform-owned
```

Existing `benchmark-results/...` result v1 remains immutable complete computation
evidence. This separate catalog may reference result ID, manifest hash, and bundle
hash without overwriting or upgrading the old result manifest.

Pre-freeze `imu_experiment_catalog_v0 / 0.1.0` publications remain identifiable through
the management API as read-only `legacy_pre_v1`. Missing `metric_split` or
`selection_eligible` means `metric_split="test"`, `selection_eligible=false`;
do not backfill original metadata. Republish v1 under a new ID/prefix. The specified
formal migration ID is
`formal_baseline_temporal_core_onnx_v1-bfef2ab3d903-catalog-v1`.
After remote v1 validation, mark the old publication deprecated and hide it from the
ordinary page, while retaining management audit access and every original artifact.

Metadata root fields:

- `schema_version="imu_experiment_catalog_v1"`, major `contract_version=1`;
- `publication_id`, `run_id`, `experiment_id`, `evidence_level`, `created_at_utc`;
- `source`, `data`, `evaluation_fingerprint`, `scheduled_jobs`;
- `methods`: per-method mean and sample standard deviation, for evidence only;
- `artifacts`: per-fold/seed model input/output, metrics, decisions, runtime evidence,
  and ONNX descriptor;
- `result_evidence`: existing result manifest/bundle references; optional direct
  full-TAR download through `result_bundle`;
- `known_limitations`.

All CV test-fold aggregate metrics explicitly use `metric_split="test"` and
`selection_eligible=false`. They cannot select a model, threshold, or trigger policy.
Formal candidates need independent validation OOF selection proving each development
participant appears once. Its required summary must be embedded in published model
metadata, not left only in an unpublished local file.

Each artifact records:

- `artifact_id`, `model_id`, `training_recipe`, `fold`, `seed`, `backend`;
- `input`/`output`: shapes, dtypes, channels, units, rate, preprocessing location;
- `metrics.window`, `metrics.event`, and every alarm policy's metrics;
- `decision.score_threshold`: value, selection method/split, comparator `>=`;
- all evaluated `decision.trigger_policies`: `required_positive_windows`,
  `lookback_windows`, `consecutive`, `cooldown_seconds`, `reference_policy`,
  `validation_pareto`;
- `decision.anchor="window_end"`;
- `parity` and ONNX/runtime version evidence;
- the `onnx` file descriptor.

Distinguish `formal_cv` from `engineering`; an engineering preflight is not a formal
model comparison. Downloads may include each ONNX, metadata, and result TAR, but
must not recommend a fold or default-sort/highlight winners by test metrics.
User-selected metric sorting must keep the test/non-selection meaning visible.

## 5. `model_release` 1.0.0

### 5.1 Two-file payload

```text
benchmark-model-catalog/models/<release_id>/model.onnx
benchmark-model-catalog/models/<release_id>/metadata.json
```

These are exactly the two immutable payload files. A platform-owned `state.json`
sidecar may coexist but is not model identity or training evidence.
Metadata is the final publication marker and contains:

- `schema_version="imu_model_release_v1"`, major `contract_version=1`;
- `release_id`, `model_code`, `name`, `created_at_utc`;
- `release_stage="research_candidate"`, without best/recommended/product claims;
- `source.selection_evidence` and `source.final_training`, separating validation-only
  selection from full final refit;
- `data`: training/validation snapshots and fingerprints;
- `input`, `output`, `preprocessing`;
- `windowing`: two-second windows, a release-specific positive
  `inference_interval_seconds`, `anchor="window_end"`, and sequence/gap resets;
  inference cadence equals the selection evidence's source stride;
- `decision`: fixed `score_threshold`, one fixed `trigger_policy`, and
  `status="provisional_validation_derived"`;
- `metrics`, `validation`, `known_limitations`;
- `verification.golden_fixtures`: at least stationary, ADL-like, and impact-like
  synthetic inputs without real participant data, expected `fall_score`, and tolerance;
- `model`: complete descriptor for `model.onnx`.

Embed these auditable selection summaries directly in `source.selection_evidence`:

- `source_run_id`, source commit, `model_id`, `training_recipe`, data/split fingerprint;
- `selection_scope="validation_only_oof"`, `metric_split="validation_oof"`,
  `selection_eligible=true`;
- positive `source_stride_seconds`, exactly matching
  `windowing.inference_interval_seconds`; separately record the training stride as
  `windowing.training_stride_seconds` rather than substituting one for the other;
- participant proof: `status="PASS"`, total participant count,
  `appearances_per_participant=1`, per-validation-fold counts, assignment SHA-256;
- threshold/trigger selection methods and deterministic tie-breaks; numeric results
  belong in `metrics`;
- immutable object key, size, and SHA-256 for any additional selection artifact.
  A local path alone is invalid; an external artifact cannot replace the embedded summary.

`source.final_training` records clean source commit, seed, fixed-epoch source,
training scope, and actual epochs. Final refit uses the already selected
validation-only configuration and cannot retune against test metrics. Metrics
explicitly state `metric_split="validation_oof"`, `selection_eligible=true`, and
`final_model_independently_evaluated=false`; selection performance is not independent
accuracy of the final-refit model.

The ONNX input is unnormalized SI `float32 [batch,50,6]`, ordered `ax,ay,az,gx,gy,gz`,
25 Hz, `sensor_local`, with gravity retained. Training mean/scale are embedded in
the graph. Output `fall_score` is uncalibrated and must not be called a probability.

Before publication, run ONNX checker and Python ONNX Runtime parity across all final
training windows. Verify metadata SHA-256, byte size, dtype, shape, and input/output
names against the actual ONNX. Execute the embedded golden fixtures instead of
trusting a `PASS` string. `external_runtime` and `device_replay` may be `not_tested`;
Python parity does not establish Android, Core ML, Windows, or embedded readiness.

A release fixes a threshold/trigger policy, not product safety certification or
real-world fall-detection performance. Display release stage, metric split,
independent evaluation, external runtime, replay, and limitations. Give `not_tested`
a different visual/text state from `PASS`. Do not label a research candidate a final,
best, or recommended model.

## 6. Cross-repository synchronization

The canonical history stays in the benchmark repository. Change normative semantics
there first with the producer, then synchronize the pinned source commit, consumer,
lock, and tests in the annotation repository. Either side may propose a specification
change; neither may independently change the exchanged contract.

This repository maintains an English translation instead of a byte-identical source
copy. Its lock explicitly distinguishes the two representations:

```json
{
  "upstream_repository": "Kscii/imu-fall-benchmark",
  "upstream_commit": "<40-hex>",
  "upstream_path": "docs/contracts/annotation-benchmark-contract.zh-CN.md",
  "upstream_sha256": "<64-hex-of-upstream-original>",
  "representation": "english_translation",
  "canonical_path": "docs/contracts/annotation-benchmark-contract.md",
  "sha256": "<64-hex-of-this-translation>",
  "module_versions": {
    "dataset_handoff": "1.0.0",
    "experiment_catalog": "1.0.0",
    "model_release": "1.0.0"
  }
}
```

Translation corrections may clarify English without changing contract semantics.
For an upstream update, verify the original at the pinned commit, update the
translation completely, and record both digests. Do not present the translated
bytes as an identical upstream copy. The separately pinned English HDF5 contract
continues to use its existing byte-identical lock.
