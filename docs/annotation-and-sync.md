# Annotation and synchronization

The annotation app reads published recordings. Raw H5/MKV/MP4 artifacts remain
immutable; synchronization, labels, ownership, and the active export are stored
in `review.json`.

## Workflow

1. Open a recording and claim the task. Another member can explicitly take ownership.
2. In **Sync**, confirm the participant and select the start/end synchronization anchors.
3. In **Annotate**, label activity and excluded intervals; mark each fall impact.
4. In **Data**, resolve coverage gaps and validation errors, then complete the task.
5. Download the resulting training export or include it in an immutable snapshot.

Only the current owner edits an active task. Completed recordings are read-only;
the owner or an administrator can reopen them. Reopening clears the active-export
pointer without changing old immutable exports or snapshots. The recording review
stores its current revision and last editor, not a complete edit history.

## Synchronization anchors

Pair the first video frame showing finger-to-housing contact with the first clear
IMU response to the same tap. The largest peak is not necessarily the event start.
Each anchor retains exact nanoseconds, source frame/sample indices, preceding
frame/sample times, and reviewer identity. Discrete time intervals define a
conservative uncertainty bound.

For the start and end anchors:

```text
d_start = video_start - imu_start
d_end = video_end - imu_end
estimated_offset = (d_start + d_end) / 2
anchor_disagreement = abs(d_end - d_start)
```

| Condition | Result |
| --- | --- |
| Disagreement greater than 100 ms | Reselect anchors; do not compensate |
| Consistent anchors, absolute offset below 100 ms | Preserve the host clock; applied offset is zero |
| Consistent anchors, absolute offset at least 100 ms | Suggest a fixed offset requiring confirmation |
| Conservative error bound greater than 500 ms | Reject finalization |

The normal uncertainty target is at most 200 ms. The model fixes `scale=1`; two
anchors do not justify fitting arbitrary clock drift. The selected transformation
is `video_time = imu_time + offset`. It is applied when reading or exporting data,
never by rewriting raw timestamps.

## Intervals and exact boundaries

Every nanosecond from the start to the end of a finalized recording must belong
to exactly one half-open interval `[start_ns, end_ns)`:

- `fall`: a fall activity;
- `non_fall`: an activity eligible for training;
- `excluded`: a non-training interval with a reason.

Overlaps, gaps, and out-of-range boundaries block completion, including gaps shorter
than a video frame. Exclusion reasons include synchronization taps, preparation,
sensor adjustment/removal, quality, ambiguity, privacy, and other.

When navigating to an interval boundary or gap, the annotation cursor retains its
exact nanosecond value while video shows the nearest frame. **I/O** use that exact
boundary. Playback, scrubbing, and frame stepping return the cursor to real video
frame time. Synchronization and impact markers always use real frames. Displayed
milliseconds are a presentation choice, not the stored precision.

## Shortcuts

| Key | Action |
| --- | --- |
| `,` / `.` | Previous / next frame |
| `Shift+,` / `Shift+.` | Back / forward five frames |
| `I` / `O` | Interval start / end |
| `E` | Navigate to the selected interval end |
| `2` | Mark fall impact |
| `F` / `N` / `X` | Fall / non-fall / excluded |
| `Ctrl+S` / `Command+S` | Save draft |

Shortcuts are disabled while editing text, selecting options, or composing input.
Structural changes save immediately; use the draft status indicator for remaining
unsaved work. A conflict requires reading the current revision before retrying.

## Fall events

A fall interval begins at the first clear loss of balance and ends after the
post-impact posture has stabilized. Its onset is derived from the exact interval
start. Mark one impact strictly inside each fall interval: the first major contact
with the ground, mat, or support. An IMU peak can help inspection but does not replace
video review. Multiple falls retain separate segment IDs and events.

## Versioned labels

Labels have a stable `code`, mutable display `name`, immutable fall/non-fall type,
and enabled status. Display names are user data and need not match the UI language.
All allowlisted members can create, rename, disable, restore, and migrate labels.
Used labels can be disabled; only unused labels can be permanently deleted.

Each change writes an immutable taxonomy version. Incomplete reviews use current
labels when saved; completed reviews and exports retain their frozen version.
The checked-in activity YAML seeds an empty store and does not overwrite online edits.

Migration between codes of the same type requires a preview freezing taxonomy and
review revisions. It updates current reviews with optimistic locking, disables the
source label, and regenerates active exports for completed recordings. Historical
exports and snapshots remain unchanged. Conflicts are reported per recording.

## Completion and export

Completion requires prod data, verified synchronization, confirmed participant,
full interval coverage, all fall impacts, verified calibration, approved capture
configuration, and unchanged source hashes. It automatically writes an immutable
HDF5 training export on the common valid IMU/video time range.

The 25 Hz grid uses linear interpolation without extrapolation. Interval boundaries
and onset use ceiling; impact uses the nearest grid sample, with a half-step rounded
later. Snapshot construction packages/merges those exports without resampling again.
See [Data formats](data-format.md) and the [HDF5 contract](contracts/imu-hdf5-v3.2.md).
