# Architecture and timestamps

The local capture process owns BLE and camera hardware. The separate annotation
service reads published objects and never initializes capture hardware. A shared
React codebase produces separate capture and annotation frontend builds.

## Three data layers

| Layer | Responsibility | Mutable state |
| --- | --- | --- |
| Capture | Preserve BLE bytes/counts, host receive times, and actual video PTS | In-progress local files only |
| Annotation | Review synchronization, participant, intervals, and task ownership | Version-checked `review.json` |
| Export | Produce validated SI training data and immutable snapshots | Job progress and explicit current pointers |

A nominal 30 FPS camera or 25 Hz sensor does not guarantee uniform arrival times.
BLE batching, USB scheduling, encoding, and dropped frames make actual timestamps
essential. Raw capture does not insert samples or frames to hide those effects.

## Device ownership

Preview and recording share one BLE owner. Preview is in memory; the recording
boundary controls H5 writes. Starting/stopping a recording does not create another
BLE client. Connection operations are single-flight and carry operation IDs so a
late scan result cannot revive a released session.

Device states are independent of recording states. Preview reconnects with 1/2/4
second backoff, but recording and characterization stop on a lost connection and
retain interrupted evidence. IMU and camera health/retry state are independent.

A camera may require FFmpeg to restart when switching between preview and recording.
The session-level preview hub keeps the same stream and last JPEG through that
transition. Released or absent preview channels return HTTP 409 rather than an
indefinitely empty successful stream.

## Clock domains

| Time | Meaning |
| --- | --- |
| UTC recording ID / creation time | Human/audit identity; not the alignment clock |
| Host monotonic nanoseconds | Capture clock unaffected by wall-clock corrections |
| IMU receive time | Host callback arrival of a complete notification |
| Reconstructed sample time | Protocol-specific estimate, with quality diagnostics |
| Recording-relative time | Monotonic time minus the recording origin |
| Media time | Zero-based MKV/MP4 browser playback time |
| Training sample index | Integer position on a strict 25 Hz derived grid |

Linux video uses V4L2 monotonic PTS. Windows/macOS map their source PTS to the host
clock at FFmpeg startup. Finalization first preserves original frame timestamps in
H5, then stream-copies the MKV to a zero-based media timeline. Relative frame
intervals must remain identical. H.264 uses variable-frame-rate timestamps without
fabricated fixed-rate frames.

For the batched CW12EU-T protocol, packet-end samples are associated with receive
times and the full recording is fitted to reconstruct sample timing. Raw arrival
times and fit residuals remain available. For the ABF2 protocol, the recorded device
millisecond counter is additional evidence, not UTC and not a shared video clock.

Packet-fit maximum residual up to 200 ms is normal; 200–500 ms is a warning; above
500 ms blocks publication. RMS above 100 ms, packet gaps above 2 seconds, callback
drops, or parser failures remain blocking conditions. These gates do not rewrite
the source evidence.

## Synchronization

Two visible housing taps check whether start/end offsets agree. Formal review uses
only a confirmed fixed offset and `scale=1`; it does not infer drift from two points.
See [Annotation and synchronization](annotation-and-sync.md) for thresholds and
exact boundary behavior.

`imu/samples/recording_time_ns` stays raw. Embedded `sync/`, `annotations/`, and
`aligned_video_time_ns` are capture-time compatibility state; the external review is
authoritative for later edits. Independent synchronization experiments must not
silently become formal review decisions.

## Integrity and persistence

One recording is one H5/MKV pair. Finalization, publication, indexing, annotation,
and export are separate durable stages. Object content is written and verified
before its immutable manifest. Hashes are checked against actual artifacts; byte
size alone is not sufficient.

`review.json` updates use revision and object-generation preconditions. Exports
include the review revision and content digest in their immutable keys. Reopening a
review invalidates its active export without mutating old exports. Snapshot IDs
include the handoff contract version and sorted recording content.

SQLite catalogs index object storage; they do not replace raw files, reviews,
immutable manifests, or private identity/configuration backups.

See [Data pipeline](data-pipeline.md) for the stage diagram and
[Data formats](data-format.md) for an application-independent reader reference.
