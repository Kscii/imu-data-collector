# Multi-Device IMU Data Collection and Annotation Platform

This repository provides an evidence-preserving workflow for collecting, synchronizing,
annotating, and exporting chest-worn IMU data together with reference video. It contains two
independently deployed applications:

- a local capture application that connects to BLE IMUs and cameras; and
- a cloud annotation application that reads published artifacts without initializing capture
  hardware.

The project is designed for research data collection. It is not a medical device or a production
fall-alert system.

An experimental synthetic-motion review pilot also exists in the repository. It remains separate
from the real-device capture and annotation workflow and has not been deployed as a production
module. See [Synthetic motion module (Chinese)](docs/synthetic-motion-module.md) for its current
boundary and implementation backlog.

## Design principles

- **Preserve source evidence.** Raw BLE notifications, raw sensor counts, timestamps, and original
  video timing remain available after processing.
- **Treat timestamps as authoritative.** Capture does not force IMU samples or video frames onto an
  artificial fixed-rate timeline. Strict 25 Hz samples are created only in the validated training
  export layer.
- **Version device meaning.** A project serial number, immutable configuration snapshot, and
  calibration profile define how each recording must be interpreted. BLE names and addresses are
  not device identities.
- **Separate capture from review.** Local capture produces and publishes immutable source
  artifacts. Synchronization, participant assignment, annotation, review, and active exports are
  managed separately.
- **Fail closed for training.** Upload completion alone does not make a recording eligible for
  training. Production tier, verified synchronization, completed annotation, unchanged source
  hashes, and verified SI conversion are all required.

## Data flow

```text
BLE IMU + camera
       |
       v
local HDF5 + MKV -- background validation/finalization --> published capture artifacts
                                                               |
                                                               v
                                              synchronization and annotation
                                                               |
                                                               v
                                           immutable 25 Hz training exports
                                                               |
                                                               v
                                                versioned dataset snapshots
```

The capture HDF5 stores sensor evidence and actual timestamps. The MKV stores the original H.264
video without audio. Publication adds a browser-compatible preview and an immutable manifest.
Annotation state is stored separately in `review.json`, using object generation and review revision
checks to prevent accidental overwrites.

For the complete artifact graph, HDF5 structures, bucket layout, and snapshot formats, read
[Project Data Pipeline and Stored File Formats](docs/data-pipeline.en.md).

## Current support status

| Environment | Status |
| --- | --- |
| Native Linux | Current source-release baseline. Arch Linux is the locally exercised platform. |
| Windows 10/11 x64 | Native backend and installer workflows exist, but the current replacement-sensor release is deferred pending valid device parameters. |
| macOS 13+ | Separate Intel and Apple Silicon packaging exists. Intel real-device BLE, camera, permission, and full-recording acceptance remains pending; the current replacement-sensor release is also deferred. |
| WSL2 | Not supported as a capture environment. |
| Android | Not supported as a capture environment. |

The current source release is **v0.3.0**. It ships Linux source and cloud-service updates; it does
not include a new Windows installer, macOS DMG, or Linux binary bundle. See the
[v0.3.0 release notes (Chinese)](docs/releases/v0.3.0.md) and
[Linux source installation guide](docs/linux-source-install.md).

### Device status

- `IMU-0001-R01` is the validated CW12EU-T profile. The retained profile does not prove that a
  particular physical sample is healthy or available.
- `IMU-0002-R01` is a commissioning-stage `acce&gyro` device. Its 22-byte ABF2 packet structure and
  local millisecond counter are understood, but the supplied conversion coefficients do not match
  the device. It is therefore **test-only**, and official SI arrays remain `NaN`.
- Every preview or recording must explicitly select a project serial number. The application does
  not silently substitute a BLE name, address, or another device's calibration.

## Quick start

### Requirements

- Native Linux with a graphical browser session
- Python 3.12 and [`uv`](https://docs.astral.sh/uv/)
- Node.js 24 and npm
- BlueZ, `bluetoothctl`, and access to the system D-Bus Bluetooth service
- FFmpeg/FFprobe with V4L2 input and H.264 encoding
- `v4l2-ctl` from v4l-utils and permission to access the selected camera

Install and start the capture application from the repository root:

```bash
uv sync --frozen
npm ci --prefix frontend
npm run build:capture --prefix frontend
uv run --frozen imu-collector doctor
uv run --frozen imu-collector start
```

`start` opens `http://127.0.0.1:8765` after the API becomes healthy. Keep the terminal running while
using the camera, BLE device, and Web UI. Press `Ctrl+C` to stop the service. Use
`uv run --frozen imu-collector serve` to start the same local service without opening a browser.
The capture UI supports `?lang=en` and `?lang=zh-CN`.

The default configuration stores recordings under `~/IMUData` and uses local publication. It does
not grant access to the team's cloud. Team uploads require an authorized account, a separately
provisioned Desktop OAuth client ID, and the upload broker; no client secret or GCS service-account
key belongs in a desktop installation.

The local API should remain bound to localhost unless privacy and authentication controls have been
designed for the intended network. For installation, update, and optional team-login instructions,
follow the [Linux source installation guide](docs/linux-source-install.md).

## Applications and commands

The Python package installs three entry points:

| Entry point | Purpose |
| --- | --- |
| `imu-collector` | Local capture Web UI, hardware diagnostics, recording, finalization, and publication |
| `imu-annotation` | Separate annotation and dataset-management service |
| `imu-upload-broker` | Team infrastructure for OAuth token exchange and resumable cloud upload |

Useful read-only diagnostics include:

```bash
uv run --frozen imu-collector doctor
uv run --frozen imu-collector devices
uv run --frozen imu-collector probe-gatt
uv run --frozen imu-collector probe-imu --seconds 15
uv run --frozen imu-collector probe-video --seconds 20 --camera-id '<stable-camera-id>'
uv run --frozen imu-collector validate /path/to/recording.h5
```

`characterize-imu` produces diagnostic-only IMU HDF5 files under `~/IMUData/_diagnostics/` and
marks them as ineligible for training. Device configuration migration and maintainer operations are
documented separately rather than presented as first-install steps.

## Stored artifacts

| Artifact | Role |
| --- | --- |
| `.partial.h5` / `.partial.mkv` | Incomplete or interrupted capture; never valid training input |
| Finalized `.h5` | Raw notifications, sensor samples, configuration evidence, and timestamps |
| Finalized `.mkv` | Original H.264 reference video with actual frame timing |
| `preview.mp4` | Re-muxed browser preview generated without re-encoding |
| `manifest.json` | Immutable publication manifest written after capture artifacts are complete |
| `review.json` | Mutable, revision-controlled synchronization, annotation, assignment, and active-export state |
| `aligned.h5` | Per-recording 25 Hz training export created only after all eligibility gates pass |
| Dataset snapshot | Immutable collection of active exports, frozen taxonomy references, manifests, and checksums |

Raw media is intentionally excluded from Git and Git LFS.

## Documentation

Start with these English documents:

- [Project data pipeline and stored formats](docs/data-pipeline.en.md)
- [Linux source installation](docs/linux-source-install.md)

Detailed operational and contract documentation is currently maintained primarily in Chinese:

- [Architecture and time semantics](docs/architecture-and-time.md)
- [Device configuration Snapshot v2](docs/device-configuration-v2.md)
- [Device registry and protocol integration](docs/device-registry-and-protocols.md)
- [Desktop platform support](docs/desktop-platforms.md)
- [Desktop OAuth and upload broker](docs/desktop-upload.md)
- [Synchronization and annotation contract](docs/annotation-and-sync.md)
- [Data lifecycle and server configuration](docs/data-lifecycle.md)
- [Client delivery format](docs/client-delivery.md)
- [Production acceptance and monitoring](docs/production-acceptance.md)
- [Pre-collection checklist](docs/pre-collection-checklist.md)

The complete documentation set is under [`docs/`](docs/), and outstanding engineering work is
tracked in [`TODO.md`](TODO.md).

## Data integrity, privacy, and safety

- Original bytes, raw counts, and timestamps must never be overwritten by resampled or converted
  values.
- Artifact SHA-256 values are verified across manifests, indexes, caches, exports, and snapshots.
- A recording's `data_tier` attribute is authoritative; filenames and directory names do not define
  training eligibility.
- Unknown packet types are retained as evidence and block production publication. Known auxiliary
  packets remain stored but do not become sensor samples.
- Participant identity mappings, OAuth credentials, IAP tokens, and cloud service credentials must
  not be committed or exposed to browsers and training datasets.
- Human fall collection requires a separate safety, informed-consent, and privacy protocol.

Report security issues privately as described in [`SECURITY.md`](SECURITY.md). Do not attach
participant video, raw HDF5 files, authentication tokens, or cloud credentials to a public issue.

## License

This project is released under the [MIT License](LICENSE).
