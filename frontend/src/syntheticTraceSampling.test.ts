import assert from "node:assert/strict";
import test from "node:test";

import {extremumIndices} from "./syntheticTraceSampling.ts";

test("full-clip downsampling preserves a short spike on every IMU axis", () => {
  const samples = 3000;
  const force = new Float32Array(samples * 3);
  const gyro = new Float32Array(samples * 3);
  const spikes = [123, 579, 1002, 1444, 2251, 2998];
  spikes.forEach((index, axis) => {
    (axis < 3 ? force : gyro)[index * 3 + axis % 3] = axis % 2 ? -50 : 50;
  });
  const indices = extremumIndices(force, gyro, samples, 1, 0, 20);
  for (const index of spikes) assert.ok(indices.includes(index), `missing spike at ${index}`);
  assert.equal(indices[0], 0);
  assert.equal(indices.at(-1), samples - 1);
  assert.ok(indices.length < samples / 10);
});

test("total-signal downsampling preserves a norm spike that no single axis selects", () => {
  const samples = 3000;
  const force = new Float32Array(samples * 3);
  const gyro = new Float32Array(samples * 3);
  for (const array of [force, gyro]) {
    array[20 * 3] = 10;
    array[30 * 3 + 1] = 10;
    array[40 * 3 + 2] = 10;
    array.set([8, 8, 8], 55 * 3);
  }
  const axisIndices = extremumIndices(force, gyro, samples, 1, 0, 20, "axes");
  const totalIndices = extremumIndices(force, gyro, samples, 1, 0, 20, "magnitude");
  assert.equal(axisIndices.includes(55), false);
  assert.equal(totalIndices.includes(55), true);
  assert.ok(totalIndices.length < axisIndices.length);
});
