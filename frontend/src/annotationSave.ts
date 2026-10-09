/** A single serial save, including recovery of an ambiguous write response. */
export type AnnotationSaveState =
  | "idle" | "saving" | "refreshing" | "checking" | "saved"
  | "refresh_failed" | "unknown" | "retryable" | "error" | "conflict" | "next_step";

export function saveIsLocked(state: AnnotationSaveState): boolean {
  return state !== "idle" && state !== "saved";
}

export function saveIsBusy(state: AnnotationSaveState): boolean {
  return state === "saving" || state === "refreshing" || state === "checking";
}

export interface PreparedSave<Review> {
  expectedRevision: number;
  write: (signal: AbortSignal) => Promise<unknown>;
  matches: (review: Review) => boolean;
}

export interface SaveFlowOptions<Review extends { revision: number }> {
  initialReview: Review;
  steps: ((review: Review) => PreparedSave<Review>)[];
  readReview: (signal: AbortSignal) => Promise<Review>;
  acceptReview: (review: Review) => void;
  refresh?: (review: Review, signal: AbortSignal) => Promise<void>;
  changed: (state: AnnotationSaveState, error?: unknown) => void;
  completed?: () => void;
}

export class AnnotationSaveFlow<Review extends { revision: number }> {
  state: AnnotationSaveState = "idle";
  private readonly options: SaveFlowOptions<Review>;
  private readonly controller = new AbortController();
  private currentReview: Review;
  private prepared: PreparedSave<Review>;
  private step = 0;
  private acknowledged = false;
  private committed = false;
  private busy = false;
  private disposed = false;

  constructor(options: SaveFlowOptions<Review>) {
    if (!options.steps.length) throw new Error("A save needs at least one step");
    this.options = options;
    this.currentReview = structuredClone(options.initialReview);
    this.prepared = options.steps[0](this.currentReview);
  }

  dispose(): void {
    this.disposed = true;
    this.controller.abort();
  }

  private change(state: AnnotationSaveState, error?: unknown): void {
    if (this.disposed) return;
    this.state = state;
    this.options.changed(state, error);
  }

  /** Synchronous guard also prevents two clicks before React has rendered. */
  private async exclusive(work: () => Promise<boolean>): Promise<boolean> {
    if (this.disposed || this.busy) return false;
    this.busy = true;
    try {
      return await work();
    } finally {
      this.busy = false;
    }
  }

  start(): Promise<boolean> {
    if (this.state !== "idle") return Promise.resolve(false);
    return this.exclusive(() => this.writeSteps());
  }

  /** Checking an unknown result never repeats a write. A separate retry is explicit. */
  recover(): Promise<boolean> {
    return this.exclusive(async () => {
      if (this.state === "saved" || this.state === "conflict") return false;
      const mayWrite = this.state === "retryable" || this.state === "error" || this.state === "next_step";
      if (this.state === "next_step") return this.writeSteps();
      if (await this.check()) return this.finishStep();
      if (!this.disposed && mayWrite && this.state === "retryable") return this.writeSteps();
      return false;
    });
  }

  private async check(): Promise<boolean> {
    this.change(this.acknowledged ? "refreshing" : "checking");
    try {
      const review = await this.options.readReview(this.controller.signal);
      if (this.disposed) return false;
      if (review.revision > this.prepared.expectedRevision && this.prepared.matches(review)) {
        this.currentReview = review;
        this.acknowledged = true;
        this.options.acceptReview(review);
        return true;
      }
      if (review.revision === this.prepared.expectedRevision) {
        this.change(this.acknowledged ? "refresh_failed" : "retryable");
      } else {
        this.change(review.revision < this.prepared.expectedRevision
          ? this.acknowledged ? "refresh_failed" : "unknown"
          : "conflict");
      }
    } catch (error) {
      this.change(this.acknowledged ? "refresh_failed" : "unknown", error);
    }
    return false;
  }

  private async writeSteps(): Promise<boolean> {
    while (!this.disposed) {
      this.change("saving");
      try {
        await this.prepared.write(this.controller.signal);
        if (this.disposed) return false;
        this.acknowledged = true;
      } catch (error) {
        if (this.disposed) return false;
        const status = (error as { status?: number } | null)?.status;
        if (status && status >= 400 && status < 500 && status !== 408 && status !== 409) {
          this.change("error", error);
          return false;
        }
        // A timeout, lost body, 5xx or 409 may follow an already committed write.
        if (await this.check()) return this.finishStep();
        return false;
      }
      if (!await this.check()) return false;
      if (this.step + 1 === this.options.steps.length) return this.finishStep();
      this.advance();
    }
    return false;
  }

  private advance(): void {
    this.step += 1;
    this.prepared = this.options.steps[this.step](this.currentReview);
    this.acknowledged = false;
  }

  private async finishStep(): Promise<boolean> {
    if (this.step + 1 < this.options.steps.length) {
      this.advance();
      this.change("next_step");
      return false;
    }
    this.change("refreshing");
    try {
      await this.options.refresh?.(this.currentReview, this.controller.signal);
      if (this.disposed) return false;
      this.change("saved");
      if (!this.committed) {
        this.committed = true;
        this.options.completed?.();
      }
      return true;
    } catch (error) {
      this.change("refresh_failed", error);
      return false;
    }
  }
}

type JsonObject = Record<string, unknown>;
function object(value: unknown): JsonObject {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Invalid save document");
  return value as JsonObject;
}
function list(value: unknown): JsonObject[] {
  if (!Array.isArray(value)) throw new Error("Invalid save list");
  return value.map(object);
}
function fields(value: JsonObject, keys: string[]): unknown[] {
  return keys.map(key => value[key]);
}
function ordered(rows: unknown[][]): unknown[][] {
  return rows.sort((a, b) => JSON.stringify(a).localeCompare(JSON.stringify(b)));
}

/** Compare user-owned content, not taxonomy rewrites or H5-derived event sources. */
export function annotationContentMatches(expected: unknown, actual: unknown, actor: string): boolean {
  const project = (value: unknown, submitted: boolean) => {
    const d = object(value);
    if (!Number.isInteger(d.revision) || typeof d.finalized !== "boolean") throw new Error("Invalid annotation revision");
    const editor = (row: JsonObject) => submitted ? actor : row.annotator_id;
    return [d.revision, d.finalized,
      ordered(list(d.segments).map(row => [...fields(row, ["segment_id", "start_ns", "end_ns", "binary_label", "activity_code", "confidence", "notes"]), editor(row)])),
      ordered(list(d.events).filter(row => row.kind !== "onset").map(row => [...fields(row, ["segment_id", "kind", "time_ns"]), editor(row)])),
      ordered(list(d.exclusions).map(row => [...fields(row, ["exclusion_id", "start_ns", "end_ns", "reason", "notes"]), editor(row)])),
    ];
  };
  try { return JSON.stringify(project(expected, true)) === JSON.stringify(project(actual, false)); }
  catch { return false; }
}

export function syncContentMatches(expected: unknown, actual: unknown, actor: string): boolean {
  const project = (value: unknown, submitted: boolean) => {
    const d = object(value);
    if (typeof d.apply_fixed_offset !== "boolean" || typeof d.policy !== "string") throw new Error("Invalid sync policy");
    return [d.policy, d.apply_fixed_offset, submitted ? actor : d.reviewer_id,
      ordered(list(d.anchors).map(row => [...fields(row, ["role", "label", "imu_time_ns", "video_time_ns", "source_video_frame", "source_imu_sample", "video_interval_start_ns", "imu_interval_start_ns"]), submitted ? actor : row.reviewer_id])),
    ];
  };
  try { return JSON.stringify(project(expected, true)) === JSON.stringify(project(actual, false)); }
  catch { return false; }
}
