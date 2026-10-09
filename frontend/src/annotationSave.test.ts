import assert from "node:assert/strict";
import test from "node:test";
import {
  AnnotationSaveFlow, annotationContentMatches, syncContentMatches, saveIsLocked,
  type AnnotationSaveState, type PreparedSave,
} from "./annotationSave.ts";

type Review = { revision: number; value: string; complete?: boolean };
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function fixture(options: {
  write?: () => Promise<unknown>;
  read?: () => Promise<Review>;
  refresh?: () => Promise<void>;
  extraSteps?: ((base: Review) => PreparedSave<Review>)[];
} = {}) {
  let writes = 0, reads = 0, completed = 0;
  const states: AnnotationSaveState[] = [], accepted: Review[] = [];
  const flow = new AnnotationSaveFlow<Review>({
    initialReview: { revision: 4, value: "old" },
    steps: [base => ({
      expectedRevision: base.revision,
      write: async () => { writes++; return options.write?.(); },
      matches: value => value.value === "submitted",
    }), ...(options.extraSteps ?? [])],
    readReview: async () => { reads++; return options.read ? options.read() : { revision: 5, value: "submitted" }; },
    acceptReview: review => accepted.push(review),
    refresh: options.refresh,
    changed: state => states.push(state),
    completed: () => { completed++; },
  });
  return { flow, states, accepted, counts: () => ({ writes, reads, completed }) };
}

test("正常保存完成复查后才解锁；同一操作只完成一次", async () => {
  const f = fixture();
  assert.equal(await f.flow.start(), true);
  assert.equal(f.flow.state, "saved");
  assert.equal(saveIsLocked(f.flow.state), false);
  await f.flow.recover(); await f.flow.start();
  assert.deepEqual(f.counts(), { writes: 1, reads: 1, completed: 1 });
});

test("已写入但 review 查询失败，恢复只读，不重发 PUT", async () => {
  let fail = true;
  const f = fixture({ read: async () => {
    if (fail) throw new Error("read timeout");
    return { revision: 5, value: "submitted" };
  } });
  await f.flow.start();
  assert.equal(f.flow.state, "refresh_failed");
  fail = false;
  assert.equal(await f.flow.recover(), true);
  assert.equal(f.counts().writes, 1);
});

test("已收到写入成功时，陈旧复查结果不能允许重发或降级成保存未知", async () => {
  let revision = 3;
  const f = fixture({ read: async () => ({ revision, value: "submitted" }) });
  await f.flow.start(); assert.equal(f.flow.state, "refresh_failed");
  revision = 4;
  await f.flow.recover(); assert.equal(f.flow.state, "refresh_failed");
  revision = 5;
  await f.flow.recover(); assert.equal(f.flow.state, "saved");
  assert.equal(f.counts().writes, 1);
});

test("PUT 响应丢失但云端已提交，自动核对后确认成功", async () => {
  const f = fixture({ write: async () => { throw new Error("lost body"); } });
  assert.equal(await f.flow.start(), true);
  assert.ok(f.states.includes("checking"));
  assert.equal(f.counts().writes, 1);
});

test("结果未知时每次恢复只核对；确认原版本后另一次操作才重试原写入", async () => {
  let failRead = true, committed = false;
  const expectedRevisions: number[] = [];
  let writes = 0;
  const flow = new AnnotationSaveFlow<Review>({
    initialReview: { revision: 4, value: "old" },
    steps: [base => ({ expectedRevision: base.revision,
      write: async () => {
        expectedRevisions.push(base.revision); writes++;
        if (writes === 1) throw new Error("offline");
        committed = true;
      }, matches: r => r.value === "submitted",
    })],
    readReview: async () => {
      if (failRead) throw new Error("offline");
      return { revision: committed ? 5 : 4, value: committed ? "submitted" : "old" };
    }, acceptReview: () => {}, changed: () => {},
  });
  await flow.start(); assert.equal(flow.state, "unknown");
  await flow.recover(); assert.equal(writes, 1);
  failRead = false;
  await flow.recover(); assert.equal(flow.state, "retryable"); assert.equal(writes, 1);
  await flow.recover(); assert.equal(flow.state, "saved");
  assert.deepEqual(expectedRevisions, [4, 4]);
});

test("重试前再次核对，原请求迟到提交后不会重复写入", async () => {
  let committed = false;
  const f = fixture({ write: async () => { throw new Error("timeout"); },
    read: async () => ({ revision: committed ? 5 : 4, value: committed ? "submitted" : "old" }),
  });
  await f.flow.start(); assert.equal(f.flow.state, "retryable");
  committed = true;
  await f.flow.recover(); assert.equal(f.flow.state, "saved");
  assert.equal(f.counts().writes, 1);
});

test("真实冲突保留本地内容，不接受云端不同内容，也不重发写入", async () => {
  const f = fixture({ write: async () => { throw { status: 409 }; },
    read: async () => ({ revision: 6, value: "someone else's edit" }),
  });
  await f.flow.start(); await f.flow.recover();
  assert.equal(f.flow.state, "conflict");
  assert.equal(f.accepted.length, 0); assert.equal(f.counts().writes, 1);
});

test("明确的校验错误保留错误状态，不自动重试", async () => {
  const f = fixture({ write: async () => { throw { status: 422 }; } });
  await f.flow.start(); assert.equal(f.flow.state, "error");
  assert.deepEqual(f.counts(), { writes: 1, reads: 0, completed: 0 });
});

test("同步曲线刷新失败只重复必要复查，不重复保存", async () => {
  let fail = true;
  const f = fixture({ refresh: async () => { if (fail) throw new Error("timeline timeout"); } });
  await f.flow.start(); assert.equal(f.flow.state, "refresh_failed");
  fail = false;
  await f.flow.recover(); assert.equal(f.flow.state, "saved");
  assert.equal(f.counts().writes, 1);
});

test("连续点击开始或恢复不会同时提交两个写入", async () => {
  const pending = deferred<void>();
  const f = fixture({ write: () => pending.promise });
  const first = f.flow.start();
  assert.equal(await f.flow.start(), false); assert.equal(await f.flow.recover(), false);
  assert.equal(f.counts().writes, 1);
  pending.resolve(); await first;
});

test("切换录制时废弃操作，迟到响应不会更新页面或发起下一步", async () => {
  const pending = deferred<Review>();
  const f = fixture({ read: () => pending.promise });
  const operation = f.flow.start();
  await Promise.resolve(); await Promise.resolve();
  f.flow.dispose(); const count = f.states.length;
  pending.resolve({ revision: 5, value: "submitted" }); await operation;
  assert.equal(f.states.length, count); assert.equal(f.accepted.length, 0);
  assert.equal(f.counts().completed, 0);
});

test("完成流程中第一阶段已保存，恢复后只继续下一阶段", async () => {
  let failRead = true, finalWrites = 0;
  const f = fixture({ read: async () => {
    if (failRead) throw new Error("read timeout");
    return { revision: finalWrites ? 6 : 5, value: "submitted", complete: finalWrites > 0 };
  }, extraSteps: [base => ({
    expectedRevision: base.revision,
    write: async () => { assert.equal(base.revision, 5); finalWrites++; },
    matches: r => Boolean(r.complete),
  })] });
  await f.flow.start(); failRead = false;
  await f.flow.recover(); assert.equal(f.flow.state, "next_step");
  assert.equal(finalWrites, 0);
  await f.flow.recover(); assert.equal(f.flow.state, "saved");
  assert.equal(f.counts().writes, 1); assert.equal(finalWrites, 1);
});

test("完成请求响应丢失后核对完成状态，不重复完成或定稿", async () => {
  let finalWrites = 0;
  const f = fixture({ read: async () => ({ revision: finalWrites ? 6 : 5, value: "submitted", complete: finalWrites > 0 }),
    extraSteps: [base => ({ expectedRevision: base.revision,
      write: async () => { finalWrites++; throw new Error("lost completion response"); },
      matches: r => Boolean(r.complete),
    })],
  });
  assert.equal(await f.flow.start(), true);
  assert.equal(f.counts().writes, 1); assert.equal(finalWrites, 1);
});

const annotation = {
  revision: 2, finalized: false, taxonomy_version: "old",
  segments: [{ segment_id: "s1", start_ns: 10, end_ns: 30, binary_label: "fall", activity_code: "fall_forward", confidence: 1, notes: "", annotator_id: "actor" }],
  events: [{ segment_id: "s1", kind: "impact", time_ns: 20, annotator_id: "actor", source_video_frame: null, source_imu_sample: null }],
  exclusions: [],
};
test("核对标注忽略分类表版本、派生 onset 和帧索引，但检查用户内容及 revision", () => {
  const saved = { ...structuredClone(annotation), taxonomy_version: "new", events: [
    { ...annotation.events[0], source_video_frame: 10, source_imu_sample: 20 },
    { ...annotation.events[0], kind: "onset", time_ns: 10 },
  ] };
  assert.equal(annotationContentMatches(annotation, saved, "actor"), true);
  for (const changed of [
    { ...saved, revision: 3 }, { ...saved, finalized: true },
    { ...saved, segments: [{ ...saved.segments[0], notes: "different" }] },
    { ...saved, events: [{ ...saved.events[0], time_ns: 21 }] },
    { ...saved, segments: [{ ...saved.segments[0], annotator_id: "other" }] },
  ]) assert.equal(annotationContentMatches(annotation, changed, "actor"), false);
  assert.equal(annotationContentMatches(annotation, {}, "actor"), false);
});

test("核对同步区分锚点和应用偏移设置，服务端身份归一化不引起误报", () => {
  const expected = { policy: "conditional_fixed_offset_v1", apply_fixed_offset: false, reviewer_id: "old", anchors: [
    { role: "start_tap", label: "tap", imu_time_ns: 100, video_time_ns: 110, source_video_frame: 1, source_imu_sample: 2, video_interval_start_ns: 90, imu_interval_start_ns: 90, reviewer_id: "old" },
  ] };
  const actual = { ...expected, reviewer_id: "actor", anchors: expected.anchors.map(a => ({ ...a, reviewer_id: "actor" })) };
  assert.equal(syncContentMatches(expected, actual, "actor"), true);
  assert.equal(syncContentMatches(expected, { ...actual, apply_fixed_offset: true }, "actor"), false);
  assert.equal(syncContentMatches(expected, { ...actual, anchors: [] }, "actor"), false);
  assert.equal(syncContentMatches(expected, {}, "actor"), false);
});
