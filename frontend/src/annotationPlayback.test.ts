import {test} from "node:test";
import assert from "node:assert/strict";
import {partAt, sourceFrame, partProgress, intervalDecorations} from "./annotationPlayback.ts";

test("half-open boundaries and final extent identify exactly one part", () => {
  const parts = [{start_ns: 0, stop_ns: 120_000_000}, {start_ns: 120_000_000, stop_ns: 1_200_000_000},
    {start_ns: 1_200_000_000, stop_ns: 2_000_000_000}];
  assert.equal(partAt(parts, 119_999_999), 0);
  assert.equal(partAt(parts, 120_000_000), 1);
  assert.equal(partAt(parts, 1_200_000_000), 2);
  assert.equal(partAt(parts, 2_000_000_000), 2);
  assert.equal(partAt(parts, -1), -1);
  assert.equal(partAt([], 0), -1);
});

test("selecting a 25 Hz boundary never seeks into the previous part at 120 Hz", () => {
  const start = 120_000_000;
  assert.equal(sourceFrame(start, 120, true), 15);
  assert.equal(sourceFrame(start, 25, true), 3);
  assert.ok(sourceFrame(start, 120, true) / 120 * 1e9 >= start);
  assert.equal(sourceFrame(1_200_000_000, 120, true), 144);
});

test("saving a coalesced interval preserves selection by its original time", () => {
  const merged = [{start_ns: 0, stop_ns: 400_000_000}, {start_ns: 400_000_000, stop_ns: 2_000_000_000}];
  assert.equal(partAt(merged, 1_200_000_000), 1);
  assert.deepEqual(partProgress(merged[1], 800_000_000), {elapsed: 400_000_000, duration: 1_600_000_000, fraction: .25});
});

test("very short intervals keep exact time geometry and pending boundary style", () => {
  const parts = [{start_ns: 0, stop_ns: 40_000_000}, {start_ns: 40_000_000, stop_ns: 10_000_000_000, review_state: "pending"}];
  const {regions, markers} = intervalDecorations(parts, 1);
  assert.equal(regions[0].end, .04);
  assert.equal(regions[1].start, .04);
  assert.equal(markers[0].dashed, true);
  assert.equal(regions[1].label, "2");
});
