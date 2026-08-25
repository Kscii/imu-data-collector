#!/usr/bin/env bash
set -euo pipefail

# 此脚本必须通过 sudo 或 pkexec 以 root 身份运行。
if [[ ${EUID} -ne 0 ]]; then
  echo "错误：请使用 sudo 或 pkexec 运行此脚本。" >&2
  exit 1
fi

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
source_file="${project_dir}/configs/systemd/bluetooth-experimental.conf"
target_file="/etc/systemd/system/bluetooth.service.d/experimental.conf"

install -Dm644 "${source_file}" "${target_file}"
systemctl daemon-reload
systemctl restart bluetooth.service

echo "已启用 bluetoothd --experimental：${target_file}"
systemctl show bluetooth.service -p ExecStart -p ActiveState -p SubState
