import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const app = readFileSync(new URL("./App.tsx", import.meta.url), "utf8");
const settings = readFileSync(new URL("./DeviceConfigurationPages.tsx", import.meta.url), "utf8");
const styles = readFileSync(new URL("./styles.css", import.meta.url), "utf8");

test("采集页保留三步主流程并显式报告陈旧实时通道", () => {
  assert.match(app, /STEP 1 · 本次采集/);
  assert.match(app, /STEP 2 · 画面确认/);
  assert.match(app, /STEP 2 · 信号确认/);
  assert.match(app, /STEP 3 · 执行/);
  assert.match(app, /下方实时值显示为“—”，不会用旧值冒充当前状态/);
  assert.match(app, /disabled=\{interactionBlocked \|\| !liveFresh \|\| busy \|\| anotherSession \|\| !ready\}/);
  assert.match(app, /tierAuthorized/);
  assert.match(styles, /\.capture-action-bar \{ position: sticky/);
});

test("配置页区分工作区、本机快照、团队候选和 Current", () => {
  for (const expected of [
    "配置工作区",
    "保存本机快照",
    "发布为团队候选",
    "恢复跟随 Current",
    "以此快照创建工作区副本",
    "为新物理设备保留 SN",
    "前往“记录与发布”登录",
  ]) {
    assert.ok(settings.includes(expected), `缺少配置交互：${expected}`);
  }
  assert.match(settings, /latestWorkspaceText\.current === submittedText/);
});

test("设备配置以表单为主并保留高级 JSON 逃生口", () => {
  for (const expected of [
    "日常配置使用表单",
    "高级：完整 Snapshot JSON",
    "固件、协议与权限",
    "派生 SI Profile ID（自动计算）",
    "复制到工作区 SI（未验证）",
    "为什么按钮不可用？",
  ]) {
    assert.ok(settings.includes(expected), `缺少表单配置交互：${expected}`);
  }
  assert.doesNotMatch(app, /编辑本机候选换算 JSON/);
  assert.doesNotMatch(settings, /setWorkspace\(parsed\)/);
  assert.match(settings, /尚不是运行时可选设备/);
  assert.match(settings, /choices=\{\[-1, 1\]\}/);
});

test("BLE 发现与连接语义明确分离", () => {
  assert.match(app, /查找附近 IMU/);
  assert.match(app, /仅扫描广播；尚未连接、订阅数据或修改设备登记/);
  assert.match(settings, /不建立 GATT 连接、不订阅数据、不修改 Snapshot，也不会自动登记设备/);
});

test("标注端审批操作保留管理员确认和 revision", () => {
  assert.match(settings, /批准该 Snapshot？批准后可用于正式采集/);
  assert.match(settings, /expected_revision/);
  assert.match(settings, /设为团队 Current/);
  assert.match(settings, /审计事件/);
  assert.match(settings, /下载完整 Snapshot JSON/);
});
