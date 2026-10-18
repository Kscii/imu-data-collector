# Getting started

The repository contains two applications: a local hardware recorder and an
annotation server. They have separate frontends and ports. This guide starts the
recorder; see [Self-hosting](self-hosting.md) to annotate without capture hardware.

## Requirements

| Component | Requirement |
| --- | --- |
| Runtime | Python 3.12, uv, Node.js 24, npm |
| Capture host | Native Linux with a graphical browser session; Arch is the exercised baseline |
| Bluetooth | BLE adapter, BlueZ, `bluetoothctl`, and access to the system Bluetooth D-Bus service |
| Video | FFmpeg and FFprobe with V4L2 and H.264 support; `v4l2-ctl` from v4l-utils |
| Storage | Default capture free-space gate: 20 GiB; budget additional space for recordings and publishing |
| Optional Google login | An unlocked desktop Secret Service/keyring backend |

Install OS packages through your distribution's package manager. The source
installer does not configure drivers, camera permissions, or Bluetooth groups.
WSL2 and Android are not supported capture hosts. For Windows/macOS builds,
see [Desktop platforms](desktop-platforms.md).

## Install

```bash
git clone https://github.com/Kscii/imu-data-collector.git
cd imu-data-collector
uv sync --frozen
npm ci --prefix frontend
npm run build:capture --prefix frontend
uv run --frozen imu-collector doctor
uv run --frozen imu-collector start
```

Use a release tag instead of `main` if you need a fixed version, and read its
[release notes](https://github.com/Kscii/imu-data-collector/releases). Source
releases and `desktop-v*` installers are separate release tracks.

`start` opens `http://127.0.0.1:8765` after the backend is ready. Keep the terminal
open; `Ctrl+C` stops it. `serve` starts the backend without opening a browser.
Use `?lang=en` to select English. Opening the browser alone does not start capture.

## Configure

The default configuration is [configs/default.yaml](../configs/default.yaml).
Recordings go to `~/IMUData`; publication uses local object storage by default.
Create a private YAML file outside the checkout for overrides:

```yaml
data_root: ~/IMUData
server_host: 127.0.0.1
server_port: 8765
storage:
  backend: local
  root: ~/IMUObjects
publish:
  mode: local
```

```bash
uv run --frozen imu-collector --config /absolute/path/to/capture.yaml start
```

`IMU_COLLECTOR_CONFIG` can select the configuration file;
`IMU_COLLECTOR_DATA_ROOT` overrides the recording directory. Relative reference
paths resolve against the project, not the private YAML directory. Use absolute
paths for private registries, catalogs, and storage.

Local annotation and local capture must share `storage.root` to discover
published recordings. Each application needs its own catalog database.
Cloud upload is optional and requires your own
[upload broker](desktop-upload.md) or a deliberately configured administrator
GCS publisher.

## First recording

1. Select a device SN and compatible camera explicitly.
2. Connect preview and inspect incoming samples, camera FPS, and diagnostics.
3. Choose **test** for onboarding. New browser sessions default to **prod**, which
   still requires approved configuration and verified SI calibration.
4. Start a short recording, then stop and wait for finalization to finish.
5. Check the final H5/MKV pair and publication status in the recording queue.

A listed sensor profile does not validate a different physical unit. Commissioning
sensors remain test-only until their own protocol and calibration evidence are
accepted. See [Capture](capture-guide.md) and
[Device integration](device-registry-and-protocols.md).

## Update and troubleshoot

Finish recording and wait for finalization/upload before updating. Stop the app,
check out the desired version, repeat dependency installation and frontend build,
then restart. The frontend build ID must match the backend source revision.

Use `imu-collector doctor` for missing tools and permissions, and
`imu-collector devices` for diagnostics. If camera access, Bluetooth, or keyring
access fails, resolve that prerequisite before attempting a recording.

The existing `install-user-service.sh` and `update-local-capture.sh` scripts are
maintainer-workstation helpers. They assume `%h/Codes/imu-data-collector`,
`/usr/bin/uv`, and a private GCS configuration. Use the terminal procedure above
for a new installation, or adapt and inspect the service before installing it.
