export type IntervalNote = {note_id: string; start_ns: number; stop_ns: number; text: string};
export type SemanticFields = {semantic_text?: string; review_state?: "pending" | "confirmed" | "excluded";
  pending_reasons?: string[]; notes?: IntervalNote[]};
type Part = SemanticFields & {segment_id: string; start_ns: number; stop_ns: number; primary_code: string | null;
  auxiliary_code: string | null; auxiliary_status: string; fall_event_id: string | null};

function appendNote(notes: Map<string, IntervalNote>, note: IntervalNote) {
  let id = note.note_id, copy = 2;
  while (notes.has(id)) {
    const old = notes.get(id)!;
    if (old.start_ns === note.start_ns && old.stop_ns === note.stop_ns && old.text === note.text) return;
    id = `${note.note_id}-${copy++}`;
  }
  notes.set(id, {...note, note_id: id});
}

// Original anchors remain intact even if a later boundary edit/split changes the interval.
export function anchoredNotes(part: Part): IntervalNote[] {
  const notes = new Map((part.notes ?? []).map(n => [n.note_id, n]));
  if (part.semantic_text?.trim())
    appendNote(notes, {note_id: `semantic-${part.segment_id}`, start_ns: part.start_ns, stop_ns: part.stop_ns, text: `原语义：${part.semantic_text}`});
  return [...notes.values()];
}
export function joined<T extends Part>(a: T, b: T): T {
  const notes = new Map(anchoredNotes(a).map(n => [n.note_id, n]));
  for (const note of anchoredNotes(b)) appendNote(notes, note);
  return {...a, stop_ns: b.stop_ns, semantic_text: "", notes: [...notes.values()]};
}
export function coalesce<T extends Part>(parts: T[]): T[] {
  const result: T[] = [];
  for (const part of parts) {
    const previous = result[result.length - 1];
    if (previous && previous.review_state === "confirmed" && part.review_state === "confirmed" && previous.stop_ns === part.start_ns &&
      previous.primary_code === part.primary_code && previous.auxiliary_code === part.auxiliary_code &&
      previous.auxiliary_status === part.auxiliary_status && previous.fall_event_id === part.fall_event_id)
      result[result.length - 1] = joined(previous, part);
    else result.push(part);
  }
  return result;
}
export function pending<T extends Part>(part: T, reasons = ["boundary"]): T {
  if (!part.review_state) return part;
  return {...part, review_state: "pending", pending_reasons: reasons, fall_event_id: null};
}
