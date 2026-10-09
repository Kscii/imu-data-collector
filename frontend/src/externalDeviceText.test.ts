import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import test from "node:test";

for (const lang of ["en", "zh-CN"]) test(`external display helpers use ${lang} without altering time ranges or raw metadata`, () => {
  const source = `
    globalThis.window = {location: {search: '?lang=${lang}'}};
    const text = await import(${JSON.stringify(new URL("./externalDeviceText.ts", import.meta.url).href)});
    const data = await import(${JSON.stringify(new URL("./externalDeviceData.ts", import.meta.url).href)});
    const raw = {label: '心率', label_en: 'Heart rate', unit: '原值', unit_en: 'Raw value', value: '原始中文'};
    const task = {mode: 'sync', state: 'done', new_records: 1, elapsed_s: 65, phase: 'fetching', devices_done: 1, devices_total: 2, windows_done: 2, windows_total: 3};
    console.log(JSON.stringify({label: text.externalLabel(raw, 'HR'), unit: text.externalUnit(raw), raw,
      states: Object.values(text.stateNames).flatMap(Object.values), unknown: text.stateName('CO', '9'),
      count: [0,1,2].map(n => text.countLabel(n, '条记录', 'record')), durations: [1000,60000,120000,3600000,86400000].map(data.durationLabel),
      done: text.taskLabel(task), failed: text.taskLabel({...task,state:'failed',error:'新数据库完整性校验失败'}),
      phases: ['queued','discovering','fetching','publishing','unknown'].map(phase => text.taskLabel({...task,state:'running',phase})),
      coverage: ['complete','partial','failed','future','unqueried'].map(s=>data.coverageLabel(s,0)),
      aggregation: ['mean','mode','last','sum'].map(data.aggregationLabel),
      time: data.beijingTime('2025-12-31T16:00:00Z'),
      range: data.dayRange('2026-01-01'), input: data.localTimeInput('2025-12-31T16:00:00Z'),
      missing: text.externalLabel({label:'未来指标'},'future_metric'),
      error: text.externalError(new Error('未知上游错误')),
    }));`;
  const value = JSON.parse(execFileSync(process.execPath, ["--experimental-strip-types", "--input-type=module", "-e", source], {encoding: "utf8"}));
  assert.deepEqual(value.range, {start:"2025-12-31T16:00:00.000Z",end:"2026-01-01T16:00:00.000Z"});
  assert.equal(value.input, "2026-01-01T00:00");
  assert.match(value.time, /00:00:00/);
  assert.equal(value.raw.value, "原始中文");
  assert.equal(value.raw.label, "心率");
  if (lang === "en") {
    assert.equal(value.label, "Heart rate"); assert.equal(value.unit, "Raw value");
    assert.deepEqual(value.count, ["0 records", "1 record", "2 records"]);
    assert.deepEqual(value.durations, ["1 second", "1 minute", "2 minutes", "1 hour", "1 day"]);
    assert.equal(value.done, "Update complete · 1 record added · 1 min 5 s");
    assert.match(value.failed, /integrity check/);
    assert.equal(value.missing, "future_metric");
    assert.equal(value.unknown, "Unknown (9)");
    delete value.raw;
    assert.doesNotMatch(JSON.stringify(value), /[\u3400-\u9fff]/u);
  } else {
    assert.equal(value.label, "心率"); assert.equal(value.unit, "原值");
    assert.equal(value.unknown, "未知 (9)"); assert.match(value.done, /更新完成/);
  }
});

test("every external API and worker Chinese error has a specific English translation", () => {
  const strings = new Set<string>();
  for (const module of ["api", "runtime", "worker"]) {
    const source = readFileSync(new URL(`../../src/imu_data_collector/external_device_${module}.py`, import.meta.url), "utf8");
    for (const match of source.matchAll(/"([^"\n]*[\u3400-\u9fff][^"\n]*)"/gu)) strings.add(match[1]);
  }
  const output = execFileSync(process.execPath, ["--experimental-strip-types", "--input-type=module", "-e", `
    globalThis.window = {location: {search: '?lang=en'}};
    const {translateText} = await import(${JSON.stringify(new URL("./i18n.ts", import.meta.url).href)});
    console.log(JSON.stringify(${JSON.stringify([...strings])}.map(translateText)));`], {encoding:"utf8"});
  assert.ok(strings.size >= 25);
  assert.doesNotMatch(output, /[\u3400-\u9fff]/u);
});
