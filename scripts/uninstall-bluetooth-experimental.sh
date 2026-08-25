#!/usr/bin/env bash
set -euo pipefail

# 此脚本只回滚本项目安装的 Bluetooth systemd drop-in。
if [[ ${EUID} -ne 0 ]]; then
  echo "错误：请使用 sudo 或 pkexec 运行此脚本。" >&2
  exit 1
fi

target_file="/etc/systemd/system/bluetooth.service.d/experimental.conf"

if [[ -f ${target_file} ]]; then
  rm -- "${target_file}"
fi
systemctl daemon-reload
systemctl restart bluetooth.service

echo "已关闭本项目启用的 bluetoothd 实验接口。"
systemctl show bluetooth.service -p ExecStart -p ActiveState -p SubState
