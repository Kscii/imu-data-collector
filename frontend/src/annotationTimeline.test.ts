import assert from "node:assert/strict";
import test from "node:test";

import {
  intervalFollowAnchorIndex,
  intervalsAtTime,
  type TimelineInterval,
} from "./annotationTimeline.ts";

const adjacent: TimelineInterval[] = [
  { key: "walking", start_ns: 10, end_ns: 20 },
  { key: "lying", start_ns: 20, end_ns: 30 },
];

test("相邻区间的公共边界只属于后一个区间", () => {
  assert.deepEqual(intervalsAtTime(adjacent, 19).map((item) => item.key), ["walking"]);
  assert.deepEqual(intervalsAtTime(adjacent, 20).map((item) => item.key), ["lying"]);
  assert.deepEqual(intervalsAtTime(adjacent, 30), []);
});

test("空白时间不命中区间，异常重叠会被完整报告", () => {
  assert.deepEqual(intervalsAtTime(adjacent, 5), []);
  const overlapping = [...adjacent, { key: "invalid", start_ns: 15, end_ns: 25 }];
  assert.deepEqual(
    intervalsAtTime(overlapping, 20).map((item) => item.key),
    ["lying", "invalid"],
  );
});

test("区间列表以当前项的前一项作为跟随锚点", () => {
  const intervals = [
    { key: "first", start_ns: 0, end_ns: 10 },
    { key: "second", start_ns: 10, end_ns: 20 },
    { key: "third", start_ns: 20, end_ns: 30 },
  ];
  assert.equal(intervalFollowAnchorIndex(intervals, "first"), 0);
  assert.equal(intervalFollowAnchorIndex(intervals, "second"), 0);
  assert.equal(intervalFollowAnchorIndex(intervals, "third"), 1);
  assert.equal(intervalFollowAnchorIndex(intervals, "missing"), -1);
});
