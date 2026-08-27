import assert from "node:assert/strict";
import test from "node:test";

import { resolveAnnotationShortcut, type AnnotationShortcutInput } from "./annotationShortcuts.ts";

const base: AnnotationShortcutInput = {
  code: "Comma",
  key: ",",
  shiftKey: false,
  ctrlKey: false,
  metaKey: false,
  altKey: false,
  repeat: false,
  isComposing: false,
  textEntryFocused: false,
};

test("逗号和句号支持一帧与五帧移动", () => {
  assert.deepEqual(resolveAnnotationShortcut(base, false, false), { kind: "step", delta: -1 });
  assert.deepEqual(resolveAnnotationShortcut({ ...base, shiftKey: true, key: "<" }, false, false), { kind: "step", delta: -5 });
  assert.deepEqual(resolveAnnotationShortcut({ ...base, code: "Period", key: "." }, false, false), { kind: "step", delta: 1 });
  assert.deepEqual(resolveAnnotationShortcut({ ...base, code: "Period", key: ">", shiftKey: true }, false, false), { kind: "step", delta: 5 });
});

test("输入控件和输入法组合期间不触发", () => {
  assert.equal(resolveAnnotationShortcut({ ...base, textEntryFocused: true }, true, true), null);
  assert.equal(resolveAnnotationShortcut({ ...base, isComposing: true }, true, true), null);
});

test("标记快捷键服从编辑权限和跌倒类型", () => {
  const impact = { ...base, code: "Digit2", key: "2" };
  assert.equal(resolveAnnotationShortcut(impact, false, true), null);
  assert.equal(resolveAnnotationShortcut(impact, true, false), null);
  assert.deepEqual(resolveAnnotationShortcut(impact, true, true), { kind: "mark", target: "impact" });
  assert.deepEqual(resolveAnnotationShortcut({ ...base, code: "KeyN", key: "n" }, true, false), { kind: "select", target: "non_fall" });
});
