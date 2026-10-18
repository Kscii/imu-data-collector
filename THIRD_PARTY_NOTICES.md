# Third-party notices

Desktop installers bundle FFmpeg/ffprobe for camera capture, Matroska muxing,
and per-frame timestamp inspection. These components retain their own licenses.

## Windows FFmpeg

- Version: `n8.1.2-44-g7c533d0f86`
- Build: BtbN/FFmpeg-Builds `autobuild-2026-08-20-13-45`
- Archive: `ffmpeg-n8.1.2-44-g7c533d0f86-win64-gpl-8.1.zip`
- SHA-256: `410c82fc0a7d713fd83412138271b8559faa8cf8a74a75eaf541dfca75ea4590`
- Source and license: [FFmpeg](https://ffmpeg.org/)
- Binary source: [BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds)

## macOS builds

DMGs build from pinned sources on matching native runners rather than depending
on Homebrew runtime libraries:

| Component | Pinned source | Archive SHA-256 |
| --- | --- | --- |
| FFmpeg | `n8.1.2`, peeled commit `38b88335f99e76ed89ff3c93f877fdefce736c13` | `9fd092511605bbebafe095ea6d38d9e40f34d12f7386e1258372df8be0576eb7` |
| x264 | `b35605ace3ddf7c1a5d67a2eb553f034aef41d55` | `cd71a7515b0e9a012e1ac9b1f8415bebcaf6fc97d4db32286642ac4c0fbe24f9` |
| NASM (Intel build) | `2.16.03` | `1412a1c760bbd05db026b6c0d1657affd6631cd0a63cddb6f73cc6d4aa616148` |

Source and licensing: [FFmpeg](https://ffmpeg.org/),
[x264](https://code.videolan.org/videolan/x264), [NASM](https://www.nasm.us/).

These FFmpeg builds enable GPL components, including libx264. The repository's MIT
license does not change those license terms. Distributions must retain this notice
and the license files supplied with FFmpeg, x264, and NASM. macOS app resources
include the actual license texts and exact source-version records used by the build.
