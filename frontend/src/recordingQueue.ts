export type QueueRecording = {
  recording_id: string;
  collection_id: string;
  participant_id: string | null;
  annotator_id?: string | null;
  workflow_state?: "unassigned" | "in_progress" | "completed";
  data_tier: "test" | "prod";
  started_at_utc: string;
  duration_ns?: number;
};

export type RecordingFilters = {
  query: string;
  tier: "all" | "test" | "prod";
  participant: string;
  collection: string;
};

export type RecordingQueueKey = "mine" | "unassigned" | "others" | "completed";

export function recordingQueueKey(recording: QueueRecording, actor: string): RecordingQueueKey {
  if (recording.workflow_state === "completed") return "completed";
  if (recording.workflow_state === "in_progress") {
    return recording.annotator_id === actor ? "mine" : "others";
  }
  return "unassigned";
}

export function groupRecordingQueues(
  recordings: QueueRecording[],
  actor: string,
  filters: RecordingFilters,
): Record<RecordingQueueKey, QueueRecording[]> {
  const needle = filters.query.trim().toLowerCase();
  const visible = recordings.filter((recording) => {
    if (filters.tier !== "all" && recording.data_tier !== filters.tier) return false;
    if (filters.participant && recording.participant_id !== filters.participant) return false;
    if (filters.collection && recording.collection_id !== filters.collection) return false;
    return !needle || `${recording.recording_id} ${recording.collection_id} ${recording.participant_id ?? ""} ${recording.annotator_id ?? ""}`.toLowerCase().includes(needle);
  }).sort((left, right) => right.started_at_utc.localeCompare(left.started_at_utc) || right.recording_id.localeCompare(left.recording_id));
  const result: Record<RecordingQueueKey, QueueRecording[]> = {
    mine: [],
    unassigned: [],
    others: [],
    completed: [],
  };
  for (const recording of visible) result[recordingQueueKey(recording, actor)].push(recording);
  return result;
}

export function preferredRecordingId(recordings: QueueRecording[], actor: string): string {
  const groups = groupRecordingQueues(recordings, actor, {
    query: "",
    tier: "all",
    participant: "",
    collection: "",
  });
  return groups.mine[0]?.recording_id
    ?? groups.unassigned[0]?.recording_id
    ?? groups.others[0]?.recording_id
    ?? groups.completed[0]?.recording_id
    ?? "";
}
