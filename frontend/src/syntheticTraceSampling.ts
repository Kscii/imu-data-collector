// Preserve both extrema of each IMU axis in every time bucket. A very short
// boundary spike must remain visible in the full-clip review chart.
export function extremumIndices(force: Float32Array, gyro: Float32Array,
                                samples: number, mounts: number, mount: number,
                                buckets = 1100): number[] {
  if (samples <= buckets * 2) return Array.from({length: samples}, (_, index) => index);
  const selected = new Set<number>([0, samples - 1]);
  const size = Math.ceil(samples / buckets);
  for (let start = 0; start < samples; start += size) {
    const end = Math.min(samples, start + size);
    for (let axis = 0; axis < 6; axis++) {
      const array = axis < 3 ? force : gyro;
      const coordinate = axis % 3;
      let minimum = Infinity; let maximum = -Infinity;
      let minimumIndex = start; let maximumIndex = start;
      for (let index = start; index < end; index++) {
        const value = array[(index * mounts + mount) * 3 + coordinate];
        if (value < minimum) { minimum = value; minimumIndex = index; }
        if (value > maximum) { maximum = value; maximumIndex = index; }
      }
      selected.add(minimumIndex); selected.add(maximumIndex);
    }
  }
  return [...selected].sort((left, right) => left - right);
}
