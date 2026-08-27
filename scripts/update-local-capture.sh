#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
frontend_dir="${project_dir}/frontend"
target_dir="${frontend_dir}/dist-capture"
staging_dir="$(mktemp -d "${frontend_dir}/.dist-capture.next.XXXXXX")"

cleanup() {
  if [[ -d "${staging_dir}" && "${staging_dir}" == "${frontend_dir}/.dist-capture.next."* ]]; then
    rm -rf -- "${staging_dir}"
  fi
}
trap cleanup EXIT

cd "${project_dir}"

uv run python - <<'PY'
import json
import sys
import urllib.error
import urllib.request

try:
    with urllib.request.urlopen("http://127.0.0.1:8765/api/v1/health", timeout=1) as response:
        payload = json.load(response)
except (OSError, TimeoutError, urllib.error.URLError):
    raise SystemExit(0)

active_job = (payload.get("background_jobs") or {}).get("active")
if payload.get("state") in {"recording", "finalizing"} or active_job:
    print("当前仍在录制或执行后台收尾/上传，拒绝更新。请等待任务完成后重试。", file=sys.stderr)
    raise SystemExit(2)
PY

echo "同步锁定依赖……"
uv sync --frozen
npm ci --prefix "${frontend_dir}"

echo "在临时目录构建采集页面……"
IMU_FRONTEND_OUT_DIR="${staging_dir}" npm run build:capture --prefix "${frontend_dir}"

expected_build_id="$(uv run python -c 'from imu_data_collector.build_info import CAPTURE_API_BUILD_ID; print(CAPTURE_API_BUILD_ID)')"
actual_build_id="$(uv run python -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["capture_api_build_id"])' "${staging_dir}/build-meta.json")"
if [[ "${actual_build_id}" != "${expected_build_id}" ]]; then
  echo "构建产物版本 ${actual_build_id} 与后端源码版本 ${expected_build_id} 不一致，保留当前服务。" >&2
  exit 1
fi

previous_dir="${frontend_dir}/.dist-capture.previous.$$"
if [[ -e "${previous_dir}" ]]; then
  echo "临时备份路径意外存在：${previous_dir}" >&2
  exit 1
fi
if [[ -d "${target_dir}" ]]; then
  mv "${target_dir}" "${previous_dir}"
fi
if ! mv "${staging_dir}" "${target_dir}"; then
  if [[ -d "${previous_dir}" ]]; then
    mv "${previous_dir}" "${target_dir}"
  fi
  exit 1
fi
if [[ -d "${previous_dir}" ]]; then
  rm -rf -- "${previous_dir}"
fi

echo "重启本机采集服务……"
systemctl --user restart imu-data-collector.service

EXPECTED_BUILD_ID="${expected_build_id}" uv run python - <<'PY'
import json
import os
import sys
import time
import urllib.error
import urllib.request

expected = os.environ["EXPECTED_BUILD_ID"]
deadline = time.monotonic() + 20
last_error = "服务尚未响应"
while time.monotonic() < deadline:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8765/api/v1/config", timeout=1) as response:
            backend = json.load(response)["build_id"]
        with urllib.request.urlopen(
            f"http://127.0.0.1:8765/build-meta.json?t={time.time_ns()}", timeout=1
        ) as response:
            frontend = json.load(response)["capture_api_build_id"]
        if backend == frontend == expected:
            print(f"更新完成：页面与后端版本均为 {expected}")
            raise SystemExit(0)
        last_error = f"页面 {frontend}，后端 {backend}，预期 {expected}"
    except (KeyError, OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError) as error:
        last_error = str(error)
    time.sleep(0.5)
print(f"服务重启后版本验收失败：{last_error}", file=sys.stderr)
raise SystemExit(1)
PY

echo "本机 WebUI：http://127.0.0.1:8765"
