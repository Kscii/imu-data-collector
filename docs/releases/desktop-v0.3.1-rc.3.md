# Desktop calibration preview — 0.3.1-rc.3

Identify the IMU housing faces before collecting calibration trials, then save
and select the resulting configuration for local testing. This preview includes
Windows 10/11 x64 and macOS 13+ installers for Apple Silicon and Intel.

- Open **Devices & settings → IMU calibration experiment**. Select the device,
  hold a face upward, describe it, and record each of the six directions.
- Live identification uses raw acceleration counts without assuming a known
  conversion coefficient. Moving, tilted, stale or saturated observations do
  not produce an accepted direction. The helper opens only the IMU and creates
  no capture files; it does not count as a calibration trial.
- Reconnect after a BLE interruption while keeping the six face records.
  Creating the experiment saves their observation summaries. Leaving the page
  or losing its heartbeat releases the preview connection.
- Settings distinguish the active configuration from the editable draft.
  Advanced parameters are collapsed; **Save and use for local testing** saves
  and selects a fixed local version. Drafts remain separate from team review.
- Fix configuration switching after a completed calibration capture, invalid
  draft recovery, and narrow-screen layouts. A fitted axis mapping that conflicts
  with the newly recorded raw-axis faces blocks usable candidate scales.

[中文操作说明](https://github.com/Kscii/imu-data-collector/blob/desktop-v0.3.1-rc.3/docs/local-calibration-setup.zh-CN.md)
· [English installation and experiment guide](https://github.com/Kscii/imu-data-collector/blob/desktop-v0.3.1-rc.3/docs/calibration-experiments.md)

Choose the installer for your CPU and verify it against **SHA256SUMS.txt**.
Windows installers are unsigned. macOS apps are ad-hoc signed, not notarized;
follow the guide's per-app opening instructions if macOS blocks the download.
Close the previous app before installing and reopening the update.

This is a prerelease. Candidate coefficients remain unverified and test-only
until the calibration evidence and independent validation are reviewed. Physical
BLE acquisition, housing directions and calibration accuracy still require
experiments with the actual device on the target computer. Automated tests and
package checks do not replace those experiments.
