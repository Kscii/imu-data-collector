#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source_file="${project_dir}/configs/systemd/imu-data-collector.service"
target_dir="${XDG_CONFIG_HOME:-${HOME}/.config}/systemd/user"
target_file="${target_dir}/imu-data-collector.service"

install -Dm0644 "${source_file}" "${target_file}"
systemctl --user daemon-reload

echo "已安装 ${target_file}"
echo "按需启动或重启：systemctl --user restart imu-data-collector.service"
echo "查看状态：systemctl --user status imu-data-collector.service --no-pager"
