# Documentation

Start with [Getting started](getting-started.md) for capture or
[Self-hosting](self-hosting.md) for a local annotation workspace or private server.
The [hosted platform](https://imu.kscii.tech/) requires an allowlisted account.

## Install and deploy

| Guide | Contents |
| --- | --- |
| [Getting started](getting-started.md) | Dependencies, source installation, configuration, and first launch |
| [Desktop platforms](desktop-platforms.md) | Linux, Windows, and macOS support, installers, permissions, and limitations |
| [Self-hosting](self-hosting.md) | Local annotation, storage, and deployment choices |
| [GCP deployment](gcp-deployment.md) | Server sizing, VM, GCS, HTTPS, IAP, and service setup |
| [Desktop upload](desktop-upload.md) | Google OAuth and the upload broker |
| [Operations](operations.md) | Updates, health checks, backups, and recovery |
| [Access and privacy](access-and-privacy.md) | Allowlisting, roles, participant identity, and data access |

## Use the platform

| Guide | Contents |
| --- | --- |
| [Capture](capture-guide.md) | Preview, recording, quality checks, finalization, and publishing |
| [Annotation and synchronization](annotation-and-sync.md) | Anchors, intervals, shortcuts, labels, and completion |
| [Calibration experiments](calibration-experiments.md) | Guided raw-data experiments and candidate calibration reports |
| [Client delivery](client-delivery.md) | Video-enabled HDF5 exports and the browser viewer |
| [Synthetic motion](synthetic-motion-module.md) | Candidate review, activity labels, and snapshot requests |
| [External device history](external-device-history.md) | Optional history worker, charts, downloads, and recovery |
| [Model catalog](model-catalog.md) | Experiment evidence and research model releases |

## Technical reference

- [Data formats and interoperability](data-format.md) — standalone reader reference and Python examples
- [Data pipeline and stored formats](data-pipeline.md)
- [Architecture and timestamps](architecture-and-time.md)
- [Device registry and protocol integration](device-registry-and-protocols.md)
- [Device configuration snapshots](device-configuration-v2.md)
- [CW12EU-T protocol evidence](device-facts.md) and [coordinate system](device-coordinate-system.md)
- [IMU-0002-R01 protocol audit](device-audits/IMU-0002-R01-2026-09-08.md)
- [HDF5 3.2 contract](contracts/imu-hdf5-v3.2.md)
- [Dataset and model handoff contract](contracts/annotation-benchmark-contract.md)
- [Client delivery service contract](contracts/client-delivery-contract.md)

For development, see [Contributing](../CONTRIBUTING.md). Published version history
and installer notes are available in
[GitHub Releases](https://github.com/Kscii/imu-data-collector/releases).
