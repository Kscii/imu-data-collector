import assert from "node:assert/strict";
import test from "node:test";
import {reviewIssues, reviewShortcut, supportPosture} from "./postureReview.ts";

test("shortcuts respect typing, IME, repeat and explicit approval chord", () => {
  for (const [key, command] of [["s", "split"], ["m", "merge"], ["p", "previous"], ["n", "next"], [" ", "toggle"]]) {
    assert.equal(reviewShortcut({key}), command);
    assert.equal(reviewShortcut({key}, true), null);
    assert.equal(reviewShortcut({key, repeat: true}), null);
    assert.equal(reviewShortcut({key, isComposing: true}), null);
    assert.equal(reviewShortcut({key, ctrlKey: true}), null);
  }
  assert.equal(reviewShortcut({key: "Enter", ctrlKey: true}, true), "approve");
  assert.equal(reviewShortcut({key: "Enter", metaKey: true}), "approve");
  assert.equal(reviewShortcut({key: "Enter", ctrlKey: true, isComposing: true}), null);
  assert.equal(reviewShortcut({key: "Enter"}), null);
  assert.equal(reviewShortcut({key: " "}, false, true), null);
});

test("legal posture pairs and genuine short state do not require a minimum dwell", () => {
  const poses = ["standing", "sitting", "lying", "squatting", "kneeling", "ground_support"];
  for (const from of poses) for (const to of poses) {
    if (from === to) continue;
    const parts = [from, "posture_transition", to].map((code,i)=>({segment_id:`s${i}`, start_ns:i*40e6, stop_ns:(i+1)*40e6, primary_code:code, transition:code==="posture_transition"?{from_posture:from,to_posture:to}:null}));
    assert.deepEqual(reviewIssues(parts), []);
  }
  assert.equal(supportPosture("walking"), "standing");
  assert.equal(supportPosture("fall"), null);
});

test("adjacent transitions and contradictory endpoints produce actionable issues", () => {
  const parts = ["standing", "posture_transition", "posture_transition", "walking"].map((code,i)=>({segment_id:`s${i}`, start_ns:i*1e9, stop_ns:(i+1)*1e9, primary_code:code, transition:code==="posture_transition"?{from_posture:null,to_posture:null}:null}));
  assert.equal(reviewIssues(parts)[0].code, "adjacent_transitions");
  assert.deepEqual(reviewIssues(parts)[0].segment_ids, ["s1", "s2"]);
  assert.equal(reviewIssues([{...parts[0],primary_code:"other_activity"}])[0].code, "other_semantics");
});
