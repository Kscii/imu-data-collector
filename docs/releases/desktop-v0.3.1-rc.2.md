# Desktop calibration preview — 0.3.1-rc.2

Windows x64 and macOS 13+ (Apple Silicon and Intel) preview for collecting calibration
evidence from the unchanged `acce&gyro` device. This release is a prerelease and
does not replace the stable Latest release.

- Guided six-face acceleration and known-angle gyro experiments, with separate
  fitting and validation trials, repeatable measurements and physical-axis descriptions.
- Candidate scales, raw-axis biases, mapping diagnostics and independent validation
  errors. Candidates remain unverified and are not activated for production.
- Local evidence and versioned reports; direct team-cloud upload and report/download
  access under Device configuration → Calibration experiments.
- Windows bootstrap byte-hash consistency, platform-aware BLE tests, and recovery
  from an immediately cancelled stage on coarse Windows clocks.

[Installation and experiment guide](https://github.com/Kscii/imu-data-collector/blob/desktop-v0.3.1-rc.2/docs/calibration-experiments.md)

Choose the installer for your CPU and check the matching SHA-256 file. Windows
installers are unsigned. macOS apps are ad-hoc signed, not notarized; follow the
guide's per-app opening instructions if macOS blocks the download.

Physical BLE acquisition and calibration accuracy still require tests with the
actual device on teammates' computers. Automated/package checks do not substitute
for those experiments.
