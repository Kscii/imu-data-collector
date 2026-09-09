import assert from "node:assert/strict";
import test from "node:test";

import { requireCurrentDeviceList } from "./captureContract.ts";

test("current device contract accepts arrays", () => {
  const value = { cameras: [], imu_profiles: [], suggested_sensor_sn: "IMU-0003-R01" };
  assert.equal(requireCurrentDeviceList<typeof value>(value), value);
});

test("legacy device contract becomes an actionable mismatch instead of a blank page", () => {
  assert.throws(
    () => requireCurrentDeviceList({ cameras: [], ble: [] }),
    /API 版本不一致/,
  );
});
