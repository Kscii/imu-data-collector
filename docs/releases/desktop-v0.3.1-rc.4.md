# Desktop capture quality policy preview — 0.3.1-rc.4

This preview keeps raw timestamps authoritative while allowing otherwise valid
recordings with degraded video continuity to reach the annotation platform.

- A video frame gap above 200 ms and production video span FPS below 27 are now
  publication-allowed quality warnings. Their thresholds and messages are unchanged.
- IMU timing, parse, calibration and monotonicity gates remain blocking. Missing,
  unreadable or hash-inconsistent H5/MKV artifacts also remain blocking.
- Manifest 3.3 carries capture quality warnings to Data management and the annotation
  workbench. Annotators may exclude an action interval as `quality_issue` or
  `ambiguous` when the frozen video does not support a reliable label.
- The MP4 preview remains a stream-copy proxy. It preserves source PTS gaps and does
  not synthesize replacement frames.
- Historical, unuploaded recordings are re-evaluated only when every saved blocker is
  one of the explicitly reclassified quality rules. Other historical conclusions are
  left untouched.

The release contains unsigned Windows x64 and ad-hoc-signed macOS Apple Silicon and
Intel installers. It does not change device configuration approval, SI calibration,
or the requirement that production training exports use verified calibration,
identity, synchronization and finalized annotation coverage.
