# Capture guide

Install the local application using [Getting started](getting-started.md).
A recording is one uninterrupted start-to-stop session, stored as an H5/MKV pair.
Multiple activities and fall events may belong to the same recording.

## Prepare

- Select the physical device by SN and confirm its protocol/configuration status.
- Use **test** for commissioning or workflow checks. **prod** requires an approved
  configuration and verified SI profile; choosing prod does not bypass quality gates.
- Secure the sensor in the intended orientation and check battery, storage, camera
  framing, and lighting. Arrange consent and safe collection procedures separately.
- Disconnect other BLE clients. The supported devices are treated as single-connection
  peripherals; a phone and two computers must not compete for the same sensor.

Connect preview. Confirm six-axis data, recent packets, parser health, and camera
input FPS. Preview is in memory and does not create recording files. The browser
preview is capped near 10 FPS; it does not represent the recording input rate.

## Record

1. Start recording and verify that IMU samples and video frames are increasing.
2. Tap the sensor housing once with the contact visible in the camera. Do not press
   a function button that may change the Bluetooth state.
3. Wait for the tap response to settle, then perform the planned activities.
4. Before stopping, make a second visible housing tap with the device still secure.
5. Stop and check finalization in the recording/publishing queue.

BLE preview and recording share one connection. A disconnect during recording
stops the session and retains diagnostic partial files; it does not silently join
separate connections. Preview may reconnect independently when no recording is active.

## Finalization and publication

Stopping closes the recording and resumes preview. A background worker then probes
video, preserves frame timestamps, remuxes the MKV, validates the H5, and publishes
complete artifacts. The queue distinguishes local readiness, upload, and annotation
server acceptance.

| State/artifact | Meaning |
| --- | --- |
| `.partial.h5`, `.partial.mkv` | Recording in progress or interrupted; not a completed dataset |
| `.finalizing.h5`, `.finalizing.mkv` | Durable inputs to background finalization |
| `.h5`, `.mkv` | Final raw recording pair |
| `preview.mp4` | Rebuildable browser media |
| `manifest.json` | Written last after artifact validation; publication marker |
| `index-receipts/<recording_id>.json` | Annotation server indexing result |

Finalization and upload failures retain the raw files. The worker retries after
5, 30, and 120 seconds; after the initial attempt and three retries, use the
queue's explicit retry action. Check the reported cause first. An upload success
does not mean the annotation server accepted its schema, calibration, or hashes.

Source installs publish locally by default. Configured cloud clients automatically
publish eligible prod recordings; test uploads require an explicit action.
See [Desktop upload](desktop-upload.md).

## Quality and device limits

Raw packets, counts, timestamps, unknown trailer bytes, and diagnostics are retained.
Candidate conversion factors are not authoritative SI. Unverified SI remains NaN,
and test data cannot become production training data by changing a filename.

Select a camera mode that declares the target 30 FPS. Actual frame timestamps,
input FPS, and warnings matter; the application does not manufacture frames to
meet the nominal rate. Windows/macOS use observed timing rather than claiming
Linux-specific exposure control. Review warnings before treating a recording as
usable evidence.

After a short test, confirm the selected SN/configuration hashes, raw packet count,
video playback, timestamps, and finalization result. Repeat hardware acceptance
when changing sensor, firmware, OS, camera, or driver.

## Next steps

Use [Annotation and synchronization](annotation-and-sync.md) to claim and label
published recordings. [Data formats](data-format.md) describes files for external
consumers; [Architecture](architecture-and-time.md) explains timing and provenance.
