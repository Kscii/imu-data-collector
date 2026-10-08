import assert from "node:assert/strict";
import test from "node:test";
import { beijingDay, chartColumns, coverageLabel, dayRange } from "./externalDeviceData.ts";

test("Beijing calendar uses fixed UTC+8 across year and UTC day boundaries", () => {
  assert.equal(beijingDay(new Date("2025-12-31T16:00:00Z")), "2026-01-01");
  assert.deepEqual(dayRange("2026-01-01"), {
    start: "2025-12-31T16:00:00.000Z", end: "2026-01-01T16:00:00.000Z",
  });
});

test("chart keeps zero, null, and synthetic measurements separate", () => {
  assert.deepEqual(chartColumns([[1000, 0, false], [2000, null, false], [3000, 70, true]]),
    [[1, 2, 3], [0, null, null], [null, null, 70]]);
});

test("empty queried days differ from unknown and failed coverage", () => {
  assert.equal(coverageLabel("complete", 0), "已查询，无记录");
  assert.equal(coverageLabel("unqueried", 0), "尚未查询");
  assert.equal(coverageLabel("failed", 0), "有同步失败区间");
});

test("presets are rolling durations and custom URL state roundtrips", async () => {
  const {readExternalView, resolveExternalRange, externalViewUrl} = await import("./externalDeviceData.ts");
  const clock = Date.parse("2026-10-08T12:00:00Z");
  const view = readExternalView("?view=external&device=bed&metric=HeartRate");
  assert.equal(view.preset, "7d");
  assert.deepEqual(resolveExternalRange(view, clock), {start: "2026-10-01T12:00:00.000Z", end: "2026-10-08T12:00:00.000Z"});
  for (const [preset, days] of [["1d", 1], ["30d", 30], ["365d", 365]] as const)
    assert.equal(Date.parse(resolveExternalRange({...view, preset}, clock).start), clock - days * 86400_000);
  const custom = {...view, preset: "custom" as const, from: "2026-10-01T16:00:00.000Z", to: "2026-10-02T16:00:00.000Z"};
  const url = externalViewUrl("http://localhost/?lang=zh-CN", custom);
  assert.equal(url.searchParams.get("lang"), "zh-CN");
  assert.deepEqual(readExternalView(url.search), custom);
  assert.deepEqual(resolveExternalRange(custom, clock), {start: custom.from, end: custom.to});
  assert.equal(externalViewUrl(url.href, {...custom, preset: "7d"}).searchParams.has("from"), false);
});

test("bad ranges fall back safely and all history starts at the earliest record", async () => {
  const {readExternalView, resolveExternalRange} = await import("./externalDeviceData.ts");
  for (const query of ["?range=bad", "?range=custom&from=wrong&to=no", "?range=custom&from=2026-02-02&to=2026-01-01"])
    assert.equal(readExternalView(query).preset, "7d");
  const range = resolveExternalRange(readExternalView("?range=all"), Date.parse("2026-10-08T12:00:00Z"), "2026-01-01T01:02:03.004Z");
  assert.equal(range.start, "2026-01-01T01:02:03.004Z");
});

import { chooseExternalMetric, latestMetricRange, metricPreferenceKey } from "./externalDeviceData.ts";
test("explicit URL metric wins; device type remembers selection; unavailable values fall back", () => {
  const available = ["HR", "ST", "KCAL", "energy_kcal"];
  assert.equal(chooseExternalMetric("radar-watch", available, "KCAL", "ST"), "KCAL");
  assert.equal(chooseExternalMetric("radar-watch", available, "", "ST"), "ST");
  assert.equal(chooseExternalMetric("radar-watch", available, "", "missing"), "HR");
  assert.equal(chooseExternalMetric("mattress", ["HeartRate", "sleep_stage"], "", "sleep_stage"), "sleep_stage");
  assert.notEqual(metricPreferenceKey("mattress"), metricPreferenceKey("radar-watch"));
  assert.equal(chooseExternalMetric("mattress", [], "", ""), "");
});
test("jump to latest covers 24 hours and includes the final sample in a half-open range", () => {
  const last = "2026-10-01T10:55:00.000Z";
  const range = latestMetricRange(last);
  assert.equal(Date.parse(range.end), Date.parse(last) + 1);
  assert.equal(Date.parse(range.end) - Date.parse(range.start), 86400_000);
});
