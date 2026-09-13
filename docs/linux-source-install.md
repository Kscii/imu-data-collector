# Linux source installation — v0.3.0

This release provides source code for the native Linux capture application and updates the
hosted annotation service and upload broker. It does not include a Linux executable bundle,
Windows installer or macOS DMG.

**Windows/macOS releases are deferred because the client has not yet supplied usable parameters
for the replacement sensor. The supplied coefficients do not match the current device. Linux
support does not resolve this: `IMU-0002-R01` remains test-only until corrected documentation or
a matching device is supplied and verified.**

## Requirements

- Native Linux with a graphical browser session; Arch is the locally exercised platform.
- Python 3.12, `uv`, Node.js 24 and npm. Use the checked-in `uv.lock` and `frontend/package-lock.json`.
- BlueZ and `bluetoothctl`, a BLE adapter, and permission to access the system D-Bus Bluetooth service.
- FFmpeg and FFprobe with V4L2 input and H.264 encoding, plus `v4l2-ctl` from v4l-utils.
- Permission to use the selected camera. Google login additionally requires an unlocked
  Secret Service/keyring backend in the desktop session.

Install missing system dependencies using your distribution's package manager. This source
release does not install drivers, change Bluetooth configuration, add users to groups or set
up automatic services. Linux distributions and hardware not exercised in the release checks
remain unverified; WSL2 is not a supported capture environment.

## Install and start

Download and extract the source archive for the `v0.3.0` release, or clone the release tag:

```bash
git clone --branch v0.3.0 --depth 1 https://github.com/Kscii/imu-data-collector.git
cd imu-data-collector
uv sync --frozen
npm ci --prefix frontend
npm run build:capture --prefix frontend
uv run --frozen imu-collector doctor
uv run --frozen imu-collector start
```

`start` opens `http://127.0.0.1:8765` after the API is ready. Keep the terminal open and use
`Ctrl+C` to stop. `serve` starts the same API without automatically opening a browser. The
default configuration stores recordings in `~/IMUData` and uses local publication; it does
not upload to the team's cloud. The page supports `?lang=en` and `?lang=zh-CN`.

Select the device SN explicitly. For the replacement `IMU-0002-R01`, select **test**. Its
raw 22-byte packets, counts and device clock can be recorded, but official `values_si` remain
NaN. Candidate conversion values are diagnostic only. Do not substitute coefficients from
another sensor, approve a configuration, or change team Current as an installation step.

The original CW12EU-T sample was reported faulty; a retained software profile is not evidence
that this physical unit is available for collection. Do not use that profile for the replacement.

## Optional team cloud login

Obtain the team's **Desktop OAuth client ID** from the maintainer. It is not a client secret.
Generate a separate local configuration using a new output directory:

```bash
uv run --frozen python scripts/prepare_desktop_config.py \
  --output build/linux-team-config \
  --broker-url https://upload.imu.kscii.tech \
  --oauth-client-id '<team-desktop-client-id>.apps.googleusercontent.com'
uv run --frozen imu-collector --config build/linux-team-config/default.yaml start
```

The helper replaces its output directory; do not point it at your source configuration or
recordings. Sign in from the capture UI using an authorized team account. Credentials remain
in the OS keyring; do not add tokens, client secrets or GCS service-account keys to the source.
Test uploads require an explicit user action and cannot become completed training exports.

## Updating and troubleshooting

Finish recording and all finalization/upload jobs before changing a running installation.
Stop the application, obtain the intended release, rerun `uv sync --frozen`, `npm ci --prefix
frontend` and `npm run build:capture --prefix frontend`, then start it again. Backend and
frontend build IDs must match. Rebuilding the frontend is required after source changes.

The repository's `install-user-service.sh` and `update-local-capture.sh` are existing
maintainer-workstation helpers. Their service assumes `%h/Codes/imu-data-collector`,
`/usr/bin/uv` and a private `%h/.config/imu-data-collector/gcs.yaml`; they are not a portable
first-install procedure. The terminal startup above does not depend on those paths.

If `doctor` reports missing tools, camera access fails, Bluetooth is unavailable, or the
keyring is locked, resolve that reported prerequisite before collection. Keep the API bound
to localhost. See [platform details](desktop-platforms.md),
[device configuration](device-configuration-v2.md), and
[the data pipeline](data-pipeline.en.md) for the underlying contracts.
