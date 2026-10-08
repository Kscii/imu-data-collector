import assert from "node:assert/strict";
import {test} from "node:test";
import {anchoredNotes, coalesce, joined, pending} from "./semanticEditing.ts";

const interval = (id: string, start: number, stop: number) => ({segment_id: id, start_ns: start, stop_ns: stop,
  primary_code: "lying", auxiliary_code: null, auxiliary_status: "unrecognized", fall_event_id: null as string | null,
  review_state: "confirmed" as const, pending_reasons: [] as string[], semantic_text: id,
  notes: [{note_id: `n-${id}`, start_ns: start, stop_ns: stop, text: `备注 ${id}`}]});

test("coalescing preserves every note and its original time range", () => {
  const a = interval("a", 0, 10), b = interval("b", 10, 20);
  const merged = coalesce([a, b]);
  assert.equal(merged.length, 1);
  assert.equal(merged[0].notes.length, 4);
  assert.deepEqual(merged[0].notes.map(n => [n.start_ns, n.stop_ns]), [[0, 10], [0, 10], [10, 20], [10, 20]]);
  assert.deepEqual(coalesce(merged), merged);
});
test("split notes keep anchors; edited copies both survive a merge", () => {
  const a = interval("a", 0, 20);
  const left = {...pending(a), stop_ns: 10, notes: anchoredNotes(a)};
  const right = {...pending(a), segment_id: "b", start_ns: 10, semantic_text: "", notes: anchoredNotes(a).map(n => ({...n, text: "更正"}))};
  const merged = joined(left, right);
  assert.equal(merged.notes.filter(n => n.text === "更正").length, 2);
  assert.ok(merged.notes.filter(n => n.text === "更正" || n.text === "备注 a").every(n => n.start_ns === 0 && n.stop_ns === 20));
  assert.equal(left.review_state, "pending");
  assert.equal(coalesce([left, right]).length, 2);
});
test("separate fall event IDs prevent coalescing", () => {
  const a = {...interval("a", 0, 10), primary_code: "forward_fall", fall_event_id: "fall-1"};
  const b = {...interval("b", 10, 20), primary_code: "forward_fall", fall_event_id: "fall-2"};
  assert.equal(coalesce([a, b]).length, 2);
});

test("a changed description after splitting survives a later merge", () => {
  const a = interval("a", 0, 20);
  const left = {...a, notes: anchoredNotes(a), semantic_text: "更正后的语义", stop_ns: 10};
  const merged = joined(left, {...a, segment_id: "right", start_ns: 10});
  assert.ok(merged.notes.some(n => n.text === "原语义：更正后的语义" && n.stop_ns === 10));
  assert.ok(merged.notes.some(n => n.text === "原语义：a" && n.stop_ns === 20));
});
