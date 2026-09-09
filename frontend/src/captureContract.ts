export function requireCurrentDeviceList<T>(value: unknown): T {
  if (
    !value
    || typeof value !== "object"
    || !Array.isArray((value as { cameras?: unknown }).cameras)
    || !Array.isArray((value as { imu_profiles?: unknown }).imu_profiles)
  ) {
    throw new Error(
      "采集页面与后端 API 版本不一致：设备列表缺少当前版本字段。请更新并重启采集服务。",
    );
  }
  return value as T;
}
