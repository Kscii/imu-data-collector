# 第三方软件说明

桌面安装包会随附 FFmpeg/ffprobe，用于摄像头采集、Matroska 封装和逐帧时间戳读取。

- 项目：FFmpeg
- 版本：`n8.1.2-44-g7c533d0f86`
- Windows 构建：BtbN/FFmpeg-Builds `autobuild-2026-08-20-13-45`
- 归档：`ffmpeg-n8.1.2-44-g7c533d0f86-win64-gpl-8.1.zip`
- SHA-256：`410c82fc0a7d713fd83412138271b8559faa8cf8a74a75eaf541dfca75ea4590`
- 上游源码与许可证：https://ffmpeg.org/
- 二进制构建来源：https://github.com/BtbN/FFmpeg-Builds

macOS DMG 不使用 Homebrew 运行时库，而是在对应原生 Runner 上从以下锁定源码构建：

- FFmpeg 标签：`n8.1.2`，peeled commit：`38b88335f99e76ed89ff3c93f877fdefce736c13`
- FFmpeg 源码归档 SHA-256：`9fd092511605bbebafe095ea6d38d9e40f34d12f7386e1258372df8be0576eb7`
- x264 commit：`b35605ace3ddf7c1a5d67a2eb553f034aef41d55`
- x264 源码归档 SHA-256：`cd71a7515b0e9a012e1ac9b1f8415bebcaf6fc97d4db32286642ac4c0fbe24f9`
- x264 上游与许可证：https://code.videolan.org/videolan/x264
- Intel 构建使用 NASM `2.16.03`，源码归档 SHA-256：`1412a1c760bbd05db026b6c0d1657affd6631cd0a63cddb6f73cc6d4aa616148`
- NASM 上游与许可证：https://www.nasm.us/

该构建启用了 GPL 组件（包括本项目当前使用的 libx264）。安装包中的 FFmpeg 仍受其自身
许可证约束；本仓库的 MIT 许可证不会改变 FFmpeg 的许可条件。发布安装包时必须同时保留
本文件以及 FFmpeg/x264/NASM 源码随附的许可证文件。macOS 应用包会在资源目录保留构建中实际使用的许可
证文本与精确源码版本记录。
