export type TrialSpec = { kind: "accel" | "gyro"; role: "fit" | "validation"; axis: "X" | "Y" | "Z"; sign: 1 | -1 };
export type TrialRecord = TrialSpec & { status: string; excluded?: boolean };
export const directions = ["+X", "-X", "+Y", "-Y", "+Z", "-Z"] as const;
export const minimums = { accel_fit: 3, accel_validation: 1, gyro_fit: 5, gyro_validation: 2 };

export function completedCount(trials: TrialRecord[], spec: TrialSpec): number {
  return trials.filter(t => t.status === "complete" && !t.excluded && t.kind === spec.kind && t.role === spec.role && t.axis === spec.axis && t.sign === spec.sign).length;
}

/** Round-robin repetitions preserve a complete first six-face mapping set. */
export function suggestedTrial(trials: TrialRecord[]): TrialSpec | null {
  for (const kind of ["accel", "gyro"] as const) {
    for (const role of ["fit", "validation"] as const) {
      for (let repeat = 1; repeat <= minimums[`${kind}_${role}`]; repeat++) {
        for (const axis of ["X", "Y", "Z"] as const) for (const sign of [1, -1] as const) {
          const spec = { kind, role, axis, sign };
          if (completedCount(trials, spec) < repeat) return spec;
        }
      }
    }
  }
  return null;
}

export function referenceAngle(spec: TrialSpec): number | null {
  return spec.kind === "gyro" ? spec.sign * (spec.role === "fit" ? 360 : 720) : null;
}
