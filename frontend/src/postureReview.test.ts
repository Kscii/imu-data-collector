import assert from "node:assert/strict";
import test from "node:test";
import {deriveTransitionEndpoints, postureNames, reviewIssues, reviewShortcut, supportPosture} from "./postureReview.ts";

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

test("automatic endpoints recompute edits, splits and merges without changing notes or input", () => {
  const parts = ["walking", "posture_transition", "kneeling"].map((code, i) => ({
    segment_id: `s${i}`, start_ns: i * 1e9, stop_ns: (i + 1) * 1e9, primary_code: code,
    transition: code === "posture_transition" ? {from_posture: "lying", to_posture: "sitting"} : null,
    semantic_text: "跪下", notes: [{text: "保留", start_ns: 0, stop_ns: 1e9}],
  }));
  const frozen = structuredClone(parts), derived = deriveTransitionEndpoints(parts);
  assert.deepEqual(derived[1].transition, {from_posture: "standing", to_posture: "kneeling"});
  assert.deepEqual(parts, frozen);
  assert.deepEqual(derived[1].notes, parts[1].notes);
  assert.equal(derived[1].semantic_text, "跪下");
  derived[2].primary_code = "ground_support";
  assert.equal(deriveTransitionEndpoints(derived)[1].transition?.to_posture, "ground_support");
  const split = deriveTransitionEndpoints([derived[0], {...derived[1], stop_ns: 1.5e9}, {...derived[1], segment_id: "split", start_ns: 1.5e9}, derived[2]]);
  assert.deepEqual(split[1].transition, {from_posture: "standing", to_posture: null});
  assert.deepEqual(split[2].transition, {from_posture: null, to_posture: "ground_support"});
  assert.equal(reviewIssues(split)[0].code, "adjacent_transitions");
  assert.deepEqual(deriveTransitionEndpoints([split[0], {...split[1], stop_ns: 2e9}, split[3]])[1].transition, {from_posture: "standing", to_posture: "ground_support"});
});

test("only immediate support states determine endpoints; boundaries and other activities remain unknown", () => {
  for (const code of [...Object.keys(postureNames), "walking", "running", "stair_up", "stair_down", null, "fall", "other_activity", "jumping", "bending", "stumble_recovery"]) {
    const parts = ["posture_transition", code, "posture_transition"].map((primary_code, i) => ({segment_id: `s${i}`, start_ns: i, stop_ns: i + 1, primary_code}));
    const result = deriveTransitionEndpoints(parts);
    assert.deepEqual(result[0].transition, {from_posture: null, to_posture: supportPosture(code)});
    assert.deepEqual(result[2].transition, {from_posture: supportPosture(code), to_posture: null});
  }
  const parts = ["lying", null, "posture_transition", "walking"].map((primary_code, i) => ({segment_id: `s${i}`, start_ns: i, stop_ns: i + 1, primary_code}));
  assert.deepEqual(deriveTransitionEndpoints(parts)[2].transition, {from_posture: null, to_posture: "standing"});
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

test("bending remains a primary activity while either adjacent endpoint uses standing", () => {
  const parts = ["bending", "posture_transition", "kneeling", "posture_transition", "bending"].map((primary_code, i) => ({
    segment_id: `s${i}`, start_ns: i * 1e9, stop_ns: (i + 1) * 1e9, primary_code,
  }));
  const derived = deriveTransitionEndpoints(parts);
  assert.deepEqual(derived[1].transition, {from_posture: "standing", to_posture: "kneeling"});
  assert.deepEqual(derived[3].transition, {from_posture: "kneeling", to_posture: "standing"});
  assert.deepEqual(derived.map(s => s.primary_code), parts.map(s => s.primary_code));
  assert.deepEqual(reviewIssues(derived), []);
  const changed = deriveTransitionEndpoints(derived.map((s, i) => i === 0 ? {...s, primary_code: "other_activity"} : s));
  assert.equal(changed[1].transition?.from_posture, null);
  assert.equal(deriveTransitionEndpoints(changed.map((s, i) => i === 0 ? {...s, primary_code: "bending"} : s))[1].transition?.from_posture, "standing");
});

test("adjacent transitions and contradictory endpoints produce actionable issues", () => {
  const parts = ["standing", "posture_transition", "posture_transition", "walking"].map((code,i)=>({segment_id:`s${i}`, start_ns:i*1e9, stop_ns:(i+1)*1e9, primary_code:code, transition:code==="posture_transition"?{from_posture:null,to_posture:null}:null}));
  assert.equal(reviewIssues(parts)[0].code, "adjacent_transitions");
  assert.deepEqual(reviewIssues(parts)[0].segment_ids, ["s1", "s2"]);
  assert.equal(reviewIssues([{...parts[0],primary_code:"other_activity"}])[0].code, "other_semantics");
});
