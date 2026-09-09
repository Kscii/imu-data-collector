import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const appSource = readFileSync(new URL("./App.tsx", import.meta.url), "utf8");
const configurationSource = readFileSync(new URL("./DeviceConfigurationPages.tsx", import.meta.url), "utf8");
const i18nSource = readFileSync(new URL("./i18n.ts", import.meta.url), "utf8");

test("英文标注与同步界面的动态帧文本使用整句本地化", () => {
  assert.match(appSource, /tr\("帧", "Frame"\)/);
  assert.match(
    appSource,
    /tr\("设为开始轻拍接触帧", "Set as start-tap contact frame"\)/,
  );
  assert.match(
    appSource,
    /tr\("设为结束轻拍接触帧", "Set as end-tap contact frame"\)/,
  );
  assert.doesNotMatch(appSource, />设为\{syncRole/);
  assert.doesNotMatch(appSource, / · 帧 \{anchor\.source_video_frame/);
});

test("英文数据集页面的统计字段和指纹标题显式本地化", () => {
  for (const expected of [
    'tr("序列", "sequences")',
    'tr("行", "rows")',
    'tr("标注", "annotations")',
    'tr("事件", "events")',
    'tr("区间", "intervals")',
    'tr("参与者", "participants")',
    'tr("文件指纹", "File fingerprints")',
  ]) {
    assert.ok(appSource.includes(expected), `缺少显式本地化：${expected}`);
  }
  assert.doesNotMatch(appSource, /\$\{file\.rows\.toLocaleString\(\)\} 行/);
  assert.doesNotMatch(appSource, /<summary>文件指纹<\/summary>/);
});

test("应用标题、主导航和数据目录状态不依赖 DOM 文本替换", () => {
  for (const expected of [
    'tr("IMU 数据标注平台", "IMU Annotation Platform")',
    'tr("设备校准证据", "Calibration evidence")',
    'tr("训练快照", "Training snapshots")',
    '"当前指针仍使用旧版数据契约。请先验证并激活 HDF5 3.2 快照。"',
  ]) {
    assert.ok(appSource.includes(expected), `缺少显式本地化：${expected}`);
  }
  assert.doesNotMatch(appSource, /collection\.warnings/);
});

test("采集工作台与设备配置入口保持明确可见", () => {
  for (const expected of [
    'tr("多设备 IMU · 本机采集", "Multi-device IMU · Local capture")',
    'tr("设备与设置", "Devices & settings")',
    'tr("设备配置", "Device configuration")',
    '"实时状态不可用"',
    "configuration_snapshot_id:",
  ]) {
    assert.ok(
      appSource.includes(expected) || i18nSource.includes(expected) || configurationSource.includes(expected),
      `缺少采集/配置界面文本：${expected}`,
    );
  }
});
