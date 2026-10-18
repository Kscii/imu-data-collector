# Desktop platforms

Linux source installation and Windows/macOS preview installers share the capture
state machine and data contracts. Hardware support is specific to a tested
host/device combination, not established by a successful package build.

| Platform | BLE / camera | Availability and validation |
| --- | --- | --- |
| Native Linux | Bleak/BlueZ, FFmpeg/V4L2 | Source installation; Arch is the exercised capture baseline |
| Windows 10/11 x64 | Bleak/WinRT, FFmpeg/DirectShow | Preview EXE; Windows 10 has device/workflow evidence; Windows 11 and long-run recovery need separate acceptance |
| macOS 13+ arm64/x86_64 | Bleak/CoreBluetooth, FFmpeg/AVFoundation | Separate preview DMGs; complete physical acceptance remains required on each architecture |
| WSL2 / Android | No supported capture path | Outside the desktop capture support matrix |

See [GitHub Releases](https://github.com/Kscii/imu-data-collector/releases) for exact
assets and checksums. Source tags and `desktop-v*` tags are independent. Preview
installers are not evidence of verified calibration for a replacement sensor.

## Windows

Download the Windows x64 EXE and verify its published SHA-256. The current preview
installer is unsigned and may trigger SmartScreen. It installs for the current user
and includes Python, the frontend, and FFmpeg; no separate runtime install is needed.

The tray app opens `http://127.0.0.1:8765`. Closing the browser leaves the backend
running. Reopen from the tray; stop recording before selecting Exit. It does not
install a Windows Service, start at login, or update automatically.

The separate console entry supports `imu-collector doctor` and
`imu-collector devices`. Tray startup failures are recorded in the user cache's
`logs/tray.log`. WinRT discovery may differ from Linux; a registered public address
can be used directly. Pair in Windows settings only when authentication/access
errors indicate it is needed.

## macOS

Choose the DMG matching Apple Silicon (`arm64`) or Intel (`x86_64`), verify the
architecture-specific checksum, and drag the app into Applications. Preview builds
are ad-hoc signed and not notarized. Use Finder's Open action for first launch and
follow macOS security prompts; do not disable system-wide security controls.

The app runs in the menu bar, not the Dock. Closing its browser tab does not stop
capture. Recording blocks exit until stopped. It requests camera and Bluetooth
permission when needed, but never microphone permission.

If denied, enable the app under **System Settings → Privacy & Security → Camera**
or **Bluetooth**, then fully quit and reopen it. An OS Keychain prompt may block
startup until resolved.

| Data | Default location |
| --- | --- |
| Recordings | `~/IMUData` |
| Device bindings and state | `~/Library/Application Support/imu-data-collector/` |
| Tray log | `~/Library/Caches/imu-data-collector/logs/tray.log` |
| Google refresh token | macOS Keychain |

The menu's data/log directory actions reflect configured paths.
CoreBluetooth uses local UUIDs rather than BLE MAC addresses. Select the SN, scan
its exact advertised name, and choose among duplicates. A binding is saved only
after a successful subscription and parsable notification. Forgetting a binding
does not change calibration or recorded identity.

## Camera and timing behavior

The app selects the highest-resolution compatible mode that declares the target
30 FPS. Browser preview is separately limited near 10 FPS. Linux can verify manual
exposure; Windows/macOS use observed FPS/PTS gates. macOS tests VideoToolbox at
recording startup and falls back to bundled libx264 if unavailable.

Each platform preserves actual video PTS and host-clock mapping in HDF5. It does
not fabricate fixed-rate video. See [Architecture](architecture-and-time.md).

## Validate a new host

Test permissions, exclusive BLE ownership, real six-channel samples, a complete
test H5/MKV recording, finalization, and optional login/upload. Repeat after reboot
and check disconnect/out-of-range/forced-exit recovery. Record OS/architecture,
app version, sensor SN, camera, actual encoder, and failures. Automated CI alone
cannot establish this hardware result.
