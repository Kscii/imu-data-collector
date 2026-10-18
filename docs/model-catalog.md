# Model catalog

The catalog browses, validates, and downloads research artifacts. It does not run
inference in the browser or annotation server, and does not automatically nominate
a current, best, or recommended model.

## Publication types

| Type | Purpose |
| --- | --- |
| Cross-validation experiments | Formal CV or separately identified engineering evidence, per-fold/seed ONNX, metadata, and optional full result bundle |
| Research candidates | Immutable `model.onnx` and `metadata.json` with a fixed threshold/trigger policy and validation-selection evidence |

CV test-fold metrics are audit evidence with `selection_eligible=false`. The default
view does not rank methods by test performance. A user-selected sort does not turn
test metrics into model-selection evidence. Candidate metadata embeds validation
OOF selection, participant-once proof, threshold choice, and trigger-policy choice.

```text
benchmark-model-catalog/experiments/<publication_id>/
benchmark-model-catalog/models/<release_id>/
```

Metadata is published last. Payloads are immutable; platform-owned `state.json`
tracks only `available` or `deprecated`. Deprecation does not delete artifacts.

## Access and publication

The current implementation restricts model viewing to actor `xfan0282` through
`MODEL_VIEWERS` in the annotation API. Being an administrator does not by itself
open this optional module for a new self-hosted identity. This is an existing
integration limitation, not a requirement for ordinary annotation.

Allowlisted publishers use the benchmark CLI and short-lived Google identity tokens
to contact their configured upload broker. The broker validates identity and allowed
object keys, creates restricted resumable sessions, verifies sizes/hashes, and writes
the final metadata. Publishers do not need direct bucket write credentials.
Only administrators restore deprecated publications.

The broker's default model-publishing audience is the Google Cloud SDK client
`32555940559.apps.googleusercontent.com`. Explicitly configure
`cloud.model_publish_google_audiences` or `IMU_MODEL_PUBLISH_GOOGLE_AUDIENCES` for a
changed client rather than disabling audience checks.

## Catalog and downloads

The catalog caches for 60 seconds and supports explicit refresh. Missing objects,
missing hashes, and inconsistent identities appear under `invalid_publications`
instead of becoming downloadable models. Frozen pre-v1 experiments remain auditable
as `legacy_pre_v1`, conservatively treated as test/non-selection evidence.

| Endpoint | Purpose |
| --- | --- |
| `GET /api/v1/model-catalog` | Catalog; supports `refresh=true` and `include_deprecated=true` |
| `GET /api/v1/model-catalog/{experiment\|model}/{id}` | Publication details |
| `GET /api/v1/model-catalog/{experiment\|model}/{id}/marker/download` | Metadata |
| `GET /api/v1/model-catalog/{experiment\|model}/{id}/files/{file_id}/download` | Verified artifact, single-range support, `X-Content-SHA256` |
| `POST /api/v1/model-catalog/{experiment\|model}/{id}/deprecate` | Lifecycle change |

Python ONNX parity is not device/runtime deployment acceptance. Display validation
split, independent evaluation status, `external_runtime`, `device_replay`, and known
limitations explicitly. See the [handoff contract](contracts/annotation-benchmark-contract.md).
