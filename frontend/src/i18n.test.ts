import assert from "node:assert/strict";
import test from "node:test";
import { requireCurrentDeviceList } from "./captureContract.ts";

// The production module chooses a language once per page load.
Object.defineProperty(globalThis, "window", {
  configurable: true,
  value: { location: { search: "?lang=en" } },
});
const english = await import("./i18n.ts?test=en");
window.location.search = "?lang=zh-CN";
const chinese = await import("./i18n.ts?test=zh");

test("page language controls explicit text and bilingual evidence fields", () => {
  assert.equal(english.tr("保存", "Save"), "Save");
  assert.equal(chinese.tr("保存", "Save"), "保存");
  const evidence = { summary_zh: "原始中文证据", summary_en: "English evidence" };
  assert.equal(english.localizedField(evidence, "summary"), "English evidence");
  assert.equal(chinese.localizedField(evidence, "summary"), "原始中文证据");
  assert.equal(english.localizedField({ summary_zh: "原文" }, "summary"), "");
});

test("known API errors retain the translated cause and recovery hint", () => {
  const detail = {
    code: "camera_busy",
    message: "摄像头预览进程已退出",
    hint: "检查摄像头是否被其他程序占用",
  };
  assert.equal(english.apiErrorMessage(detail, 409, "Conflict"),
    "The camera preview process has exited. Check whether another application is using the camera.");
  assert.equal(chinese.apiErrorMessage(detail, 409, "Conflict"),
    "摄像头预览进程已退出；检查摄像头是否被其他程序占用");
  assert.equal(detail.message, "摄像头预览进程已退出");
});

test("unknown errors have an English fallback while retaining the diagnostic code", () => {
  const message = english.apiErrorMessage({
    code: "new_device_error", message: "尚未翻译的底层错误", hint: "未知提示",
  }, 409, "Conflict");
  assert.match(message, /Refresh and try again/);
  assert.match(message, /new_device_error/);
  assert.doesNotMatch(message, /[\u3400-\u9fff]/u);
  assert.match(english.apiErrorMessage(null, 401, "Unauthorized"), /Sign in/);
  assert.match(english.apiErrorMessage(null, 403, "Forbidden"), /permission/);
  assert.match(english.userVisibleMessage("未知底层错误"), /server logs/);
});

test("validation arrays preserve field locations and translate known validators", () => {
  const detail = [{ loc: ["body", "content", "devices", 0, "revision"],
    msg: "Value error, revision 必须与 sensor_sn 一致" }];
  assert.equal(english.apiErrorMessage(detail, 422, "Unprocessable Entity"),
    "body.content.devices.0.revision: Value error, revision must match sensor_sn");
  assert.match(english.apiErrorMessage([{ msg: "未知校验错误" }], 422, ""), /Check the supplied fields/);
});

test("the device contract mismatch is translated as a complete actionable message", () => {
  let message = "";
  try { requireCurrentDeviceList({ cameras: [] }); }
  catch (error) { message = (error as Error).message; }
  const translated = english.userVisibleMessage(message);
  assert.match(translated, /device list lacks fields/);
  assert.match(translated, /Update and restart the capture service/);
  assert.doesNotMatch(translated, /[\u3400-\u9fff]/u);
});
