import assert from "node:assert/strict";
import test from "node:test";
import { completedCount, referenceAngle, suggestedTrial, type TrialRecord } from "./calibrationProtocol.ts";

test("guide completes all 66 independent trials and allows extra repetitions", () => {
  const trials: TrialRecord[] = [];
  let next = suggestedTrial(trials);
  while (next) { trials.push({ ...next, status: "complete" }); next = suggestedTrial(trials); }
  assert.equal(trials.length, 66);
  assert.deepEqual(trials.slice(0, 6).map(t => [t.axis, t.sign]), [["X", 1], ["X", -1], ["Y", 1], ["Y", -1], ["Z", 1], ["Z", -1]]);
  trials.push({ ...trials[0] });
  assert.equal(completedCount(trials, trials[0]), 4);
  assert.equal(suggestedTrial(trials), null);
});

test("interrupted and excluded trials do not meet minimums; validation stays separate", () => {
  const first = suggestedTrial([])!;
  assert.deepEqual(suggestedTrial([{ ...first, status: "interrupted" }]), first);
  assert.deepEqual(suggestedTrial([{ ...first, status: "complete", excluded: true }]), first);
  assert.equal(completedCount([{ ...first, role: "validation", status: "complete" }], first), 0);
  assert.equal(referenceAngle({ ...first, kind: "gyro", sign: -1 }), -360);
  assert.equal(referenceAngle({ ...first, kind: "gyro", role: "validation" }), 720);
});
