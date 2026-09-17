// Preserve extrema of the visible signals in every time bucket. A very short
// boundary spike must remain visible in either full-clip review mode.
export function extremumIndices(force: Float32Array, gyro: Float32Array,
                                samples: number, mounts: number, mount: number,
                                buckets = 1100,
                                mode: "axes" | "magnitude" = "axes"): number[] {
  if (samples <= buckets * 2) return Array.from({length: samples}, (_, index) => index);
  const selected = new Set<number>([0, samples - 1]);
  const size = Math.ceil(samples / buckets);
  for (let start = 0; start < samples; start += size) {
    const end = Math.min(samples, start + size);
    for (let axis = 0; axis < (mode === "axes" ? 6 : 2); axis++) {
      const array = axis < (mode === "axes" ? 3 : 1) ? force : gyro;
      const coordinate = mode === "axes" ? axis % 3 : -1;
      let minimum = Infinity; let maximum = -Infinity;
      let minimumIndex = start; let maximumIndex = start;
      for (let index = start; index < end; index++) {
        const offset = (index * mounts + mount) * 3;
        const value = coordinate >= 0 ? array[offset + coordinate]
          : Math.hypot(array[offset], array[offset + 1], array[offset + 2]);
        if (value < minimum) { minimum = value; minimumIndex = index; }
        if (value > maximum) { maximum = value; maximumIndex = index; }
      }
      selected.add(minimumIndex); selected.add(maximumIndex);
    }
  }
  return [...selected].sort((left, right) => left - right);
}
