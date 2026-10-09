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

// Vite's parser also sees canvas strings and imperative DOM attributes, unlike a DOM-only scan.
import { parseAst } from "vite";
import { readdirSync } from "node:fs";

function untranslatedExternalText(source: string): string[] {
  const failures: string[] = [];
  const visit = (node: any, chineseArgument = false) => {
    if (!node || typeof node !== "object") return;
    if (Array.isArray(node)) { node.forEach(child => visit(child, chineseArgument)); return; }
    const text = node.type === "TemplateElement" ? node.value.raw
      : ["Literal", "JSXText"].includes(node.type) ? node.value : null;
    if (!chineseArgument && typeof text === "string" && /[\u3400-\u9fff]/u.test(text)) {
      failures.push(`line ${source.slice(0, node.start).split("\n").length}: ${text.trim()}`);
    }
    if (node.type === "CallExpression" && ["tr", "countLabel"].includes(node.callee?.name)) {
      const chineseIndex = node.callee.name === "tr" ? 0 : 1;
      const english = node.arguments[chineseIndex + 1];
      if (!english || (english.type === "Literal" && !english.value)) failures.push("Missing English argument");
      node.arguments.forEach((arg: unknown, index: number) => visit(arg, index === chineseIndex));
      return;
    }
    for (const child of Object.values(node)) {
      if (child && typeof child === "object") visit(child, chineseArgument);
    }
  };
  visit(parseAst(source, {lang: "tsx"}));
  return failures;
}

test("external UI literals, templates and accessibility attributes require explicit English", () => {
  const directory = new URL("./", import.meta.url);
  const files = readdirSync(directory).filter(file => /^(ExternalDevice.*\.tsx|externalDevice.*\.ts)$/.test(file) && !file.endsWith(".test.ts"));
  assert.ok(files.length >= 5);
  for (const file of files) {
    assert.deepEqual(untranslatedExternalText(readFileSync(new URL(file, directory), "utf8")), [], file);
  }
});

test("external translation guard detects JSX, tooltip, template and English-argument omissions", () => {
  for (const source of [
    '<button>更新数据</button>', '<button title="下载数据" />',
    'button.setAttribute("aria-label", "报警")', 'const title = `共有 ${count} 条记录`',
    'tr("中文", "仍为中文")', 'tr("中文", "")', 'tr("中文")',
  ]) assert.ok(untranslatedExternalText(source).length, source);
  assert.deepEqual(untranslatedExternalText('tr(`共有 ${count} 条记录`, `${count} records`)'), []);
  assert.deepEqual(untranslatedExternalText('countLabel(count, "条记录", "record")'), []);
});
