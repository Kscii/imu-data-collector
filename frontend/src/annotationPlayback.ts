export type TimedPart = {start_ns: number; stop_ns: number; review_state?: string};

// Playback uses source time, never the downsampled signal index.
export function partAt(parts: TimedPart[], timeNs: number): number {
  if (!parts.length || !Number.isFinite(timeNs)) return -1;
  if (timeNs === parts[parts.length - 1].stop_ns) return parts.length - 1;
  return parts.findIndex(p => p.start_ns <= timeNs && timeNs < p.stop_ns);
}

export function sourceFrame(timeNs: number, rate: number, intervalStart = false): number {
  const frame = timeNs / 1e9 * rate;
  return Math.max(0, intervalStart ? Math.ceil(frame - 1e-9) : Math.round(frame));
}

export function partProgress(part: TimedPart, timeNs: number) {
  const duration = part.stop_ns - part.start_ns;
  const elapsed = Math.max(0, Math.min(duration, timeNs - part.start_ns));
  return {elapsed, duration, fraction: duration > 0 ? elapsed / duration : 0};
}

export function intervalDecorations(parts: TimedPart[], active: number) {
  return {
    regions: parts.map((part, i) => ({start: part.start_ns / 1e9, end: part.stop_ns / 1e9,
      color: i === active ? "rgba(96,165,250,.20)" : i % 2 ? "rgba(148,163,184,.08)" : "rgba(148,163,184,.03)",
      borderColor: i === active ? "#60a5fa" : part.review_state === "pending" ? "#9b8b65" : undefined,
      label: String(i + 1)})),
    markers: parts.slice(1).map((part, i) => ({time: part.start_ns / 1e9,
      label: `区间 ${i + 2}`, color: "rgba(148,163,184,.55)", dashed: part.review_state === "pending"})),
  };
}
