import assert from "node:assert/strict";
import test from "node:test";

import { groupRecordingQueues, preferredRecordingId, type QueueRecording } from "./recordingQueue.ts";

const recordings: QueueRecording[] = [
  { recording_id: "done", collection_id: "c2", participant_id: "p2", annotator_id: "me", workflow_state: "completed", data_tier: "prod", started_at_utc: "2026-01-04" },
  { recording_id: "other", collection_id: "c1", participant_id: "p1", annotator_id: "them", workflow_state: "in_progress", data_tier: "prod", started_at_utc: "2026-01-03" },
  { recording_id: "free", collection_id: "c1", participant_id: null, workflow_state: "unassigned", data_tier: "test", started_at_utc: "2026-01-02" },
  { recording_id: "mine", collection_id: "c1", participant_id: "p1", annotator_id: "me", workflow_state: "in_progress", data_tier: "prod", started_at_utc: "2026-01-01" },
];

test("录制只进入四个互斥队列之一", () => {
  const groups = groupRecordingQueues(recordings, "me", { query: "", tier: "all", participant: "", collection: "" });
  assert.deepEqual(groups.mine.map((item) => item.recording_id), ["mine"]);
  assert.deepEqual(groups.unassigned.map((item) => item.recording_id), ["free"]);
  assert.deepEqual(groups.others.map((item) => item.recording_id), ["other"]);
  assert.deepEqual(groups.completed.map((item) => item.recording_id), ["done"]);
});

test("默认录制按队列优先级而非全局时间选择", () => {
  assert.equal(preferredRecordingId(recordings, "me"), "mine");
});

test("过滤条件在分组前统一应用", () => {
  const groups = groupRecordingQueues(recordings, "me", { query: "c1", tier: "prod", participant: "p1", collection: "c1" });
  assert.deepEqual([...groups.mine, ...groups.others].map((item) => item.recording_id), ["mine", "other"]);
  assert.equal(groups.completed.length, 0);
});
