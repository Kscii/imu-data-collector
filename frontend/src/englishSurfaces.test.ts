import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const appSource = readFileSync(new URL("./App.tsx", import.meta.url), "utf8");

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
