import assert from "node:assert/strict";
import test from "node:test";

import { AnnotationCursor, annotationSeconds } from "./annotationCursor.ts";

test("E then I keeps the exact 6.600 s boundary while previewing frame 390 at 6.601 s", () => {
  const cursor = new AnnotationCursor();
  cursor.moveToBoundary(390, 6_601_000_000, 6_600_000_000);
  assert.equal(cursor.mark("start"), 6_600_000_000);
  cursor.observeFrame(390, 6_601_000_000, true, true);
  cursor.observeFrame(390, 6_601_000_000, true, false);
  assert.equal(cursor.mark("start"), 6_600_000_000);
  assert.equal(cursor.mark("impact"), 6_601_000_000);
  cursor.moveToBoundary(627, 10_568_000_000, 10_568_000_000);
  assert.equal(cursor.mark("end"), 10_568_000_000);
});

test("boundaries on either side of a frame and outside the first/last frame stay exact", () => {
  const cursor = new AnnotationCursor();
  for (const [frameNs, boundaryNs] of [[6_584_000_000, 6_585_000_001], [6_601_000_000, 6_600_000_000], [17_000_000, 0], [354_331_000_000, 354_923_421_885]]) {
    cursor.moveToBoundary(1, frameNs, boundaryNs);
    cursor.observeFrame(1, frameNs, true, false);
    assert.equal(cursor.mark("end"), boundaryNs);
  }
});

test("the latest seek is available immediately and old in-flight frame events cannot replace it", () => {
  const cursor = new AnnotationCursor();
  cursor.moveToBoundary(390, 6_601_000_000, 6_600_000_000);
  cursor.moveToBoundary(627, 10_568_000_000, 10_567_000_000);
  cursor.observeFrame(390, 6_601_000_000, true, true);
  assert.equal(cursor.mark("end"), 10_567_000_000);
  cursor.observeFrame(627, 10_568_000_000, true, false);
  assert.equal(cursor.mark("end"), 10_567_000_000);
});

test("frame steps, playback, native scrubbing and recording changes release exact boundaries", () => {
  const cursor = new AnnotationCursor();
  const boundary = () => cursor.moveToBoundary(390, 6_601_000_000, 6_600_000_000);
  boundary();
  cursor.moveToFrame(391, 6_617_000_000);
  assert.equal(cursor.mark("start"), 6_617_000_000);
  boundary();
  cursor.observeFrame(390, 6_601_000_000, false, false);
  assert.equal(cursor.mark("start"), 6_601_000_000);
  boundary();
  cursor.releaseBoundary();
  assert.equal(cursor.mark("start"), 6_601_000_000);
  boundary();
  cursor.observeFrame(400, 6_768_000_000, true, false);
  assert.equal(cursor.mark("start"), 6_768_000_000);
  boundary();
  cursor.reset();
  assert.equal(cursor.mark("start"), 0);
});

test("interval labels distinguish the reported one-millisecond gap", () => {
  assert.equal(annotationSeconds(6_600_000_000), "6.600 s");
  assert.equal(annotationSeconds(6_601_000_000), "6.601 s");
  assert.equal(annotationSeconds(undefined), "—");
});
