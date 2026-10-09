import {useLayoutEffect, useRef, useState} from "react";
import type {IntervalNote, SemanticFields} from "./semanticEditing";

type Props = {
  segment: SemanticFields & {start_ns: number; stop_ns: number};
  onChange: (fields: Partial<SemanticFields>) => void;
};

export function IntervalSemantics({segment, onChange}: Props) {
  const notes = segment.notes ?? [];
  // Existing records have append order, not per-note creation timestamps.
  const latest = notes.at(-1);
  const [editingOriginal, setEditingOriginal] = useState(false);
  const current = useRef<HTMLTextAreaElement>(null), focusNew = useRef(false);
  const latestKey = latest?.note_id ?? "semantic";
  useLayoutEffect(() => {
    if (focusNew.current) {current.current?.focus(); focusNew.current = false;}
  }, [latestKey]);
  const updateNote = (id: string, text: string) => onChange({notes: notes.map(n => n.note_id === id ? {...n, text} : n)});
  const range = (n: {start_ns: number; stop_ns: number}) => `${(n.start_ns / 1e9).toFixed(2)}–${(n.stop_ns / 1e9).toFixed(2)} s`;
  const originalVisible = !!latest && (!!segment.semantic_text || editingOriginal);
  const history = notes.slice(0, -1).reverse();
  const historyCount = history.length + Number(originalVisible);
  const historicalNote = (n: IntervalNote, index: number) => <label key={n.note_id}>区间备注 · {range(n)}
    <textarea aria-label={`历史区间备注 ${index + 1}`} rows={2} value={n.text} onChange={e => updateNote(n.note_id, e.target.value)}/>
  </label>;
  return <div className="interval-semantics">
    <label>区间语义{latest && <small>最新一条 · {range(latest)}</small>}
      <textarea ref={current} aria-label="区间语义" rows={2} value={latest ? latest.text : segment.semantic_text ?? ""}
        onChange={e => latest ? updateNote(latest.note_id, e.target.value) : onChange({semantic_text: e.target.value})}
        placeholder="可自由描述动作，无需与列表文字一致"/>
    </label>
    {historyCount > 0 && <details key={latestKey} className="interval-semantics-history">
      <summary>历史语义与备注（{historyCount}）</summary>
      <div>{history.map(historicalNote)}
        {originalVisible && <label>原区间语义 · {range(segment)}
          <textarea aria-label="历史区间语义" rows={2} value={segment.semantic_text ?? ""}
            onFocus={() => setEditingOriginal(true)} onChange={e => {setEditingOriginal(true); onChange({semantic_text: e.target.value});}}/>
        </label>}
      </div>
    </details>}
    <button onClick={() => {
      setEditingOriginal(false);
      focusNew.current = true;
      onChange({notes: [...notes, {note_id: `note-${crypto.randomUUID()}`, start_ns: segment.start_ns, stop_ns: segment.stop_ns, text: ""}]});
    }}>添加区间备注</button>
  </div>;
}
