export type AnnotationPosition = {
  frame: number;
  frameTimeNs: number;
  timeNs: number;
};

/** Keep a selected interval boundary independent of its nearest preview frame. */
export class AnnotationCursor {
  position: AnnotationPosition = { frame: 0, frameTimeNs: 0, timeNs: 0 };

  reset(): AnnotationPosition {
    return this.moveToFrame(0, 0);
  }

  moveToFrame(frame: number, frameTimeNs: number): AnnotationPosition {
    this.position = { frame, frameTimeNs, timeNs: frameTimeNs };
    return this.position;
  }

  moveToBoundary(frame: number, frameTimeNs: number, timeNs: number): AnnotationPosition {
    this.position = { frame, frameTimeNs, timeNs };
    return this.position;
  }

  observeFrame(frame: number, frameTimeNs: number, paused: boolean, seeking: boolean): AnnotationPosition {
    // A previous seek can still emit events while a newer seek is in flight.
    // Wait for the new frame; marking already uses the synchronous target above.
    if (seeking && frame !== this.position.frame) return this.position;
    if (paused && frame === this.position.frame) return this.position;
    return this.moveToFrame(frame, frameTimeNs);
  }

  releaseBoundary(): AnnotationPosition {
    return this.moveToFrame(this.position.frame, this.position.frameTimeNs);
  }

  mark(target: "start" | "end" | "impact"): number {
    return target === "impact" ? this.position.frameTimeNs : this.position.timeNs;
  }
}

export function annotationSeconds(ns?: number | null): string {
  return ns == null ? "—" : `${(ns / 1e9).toFixed(3)} s`;
}
