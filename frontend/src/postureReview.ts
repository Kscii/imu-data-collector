export type Transition = {from_posture: string | null; to_posture: string | null};
export type ReviewIssue = {code: string; message: string; segment_ids: string[]};
export const postureNames: Record<string, string> = {
  standing: "站姿", sitting: "坐姿", lying: "卧姿", squatting: "蹲姿",
  kneeling: "跪姿", ground_support: "爬撑",
};
export function supportPosture(code: string | null): string | null {
  if (code && code in postureNames) return code;
  return code && ["walking", "running", "stair_up", "stair_down", "bending"].includes(code) ? "standing" : null;
}
export type ReviewCommand = "toggle" | "split" | "merge" | "previous" | "next" | "approve";
type KeyInput = {key: string; ctrlKey?: boolean; metaKey?: boolean; altKey?: boolean;
  shiftKey?: boolean; repeat?: boolean; isComposing?: boolean};
export function reviewShortcut(event: KeyInput, editing = false, onButton = false): ReviewCommand | null {
  if (event.repeat || event.isComposing || event.altKey) return null;
  if ((event.ctrlKey || event.metaKey) && event.key === "Enter" && !event.shiftKey) return "approve";
  if (editing || event.ctrlKey || event.metaKey || event.shiftKey) return null;
  if (event.key === " " && !onButton) return "toggle";
  return ({s: "split", m: "merge", p: "previous", n: "next"} as const)[event.key.toLowerCase() as "s"] ?? null;
}
type Part = {segment_id: string; start_ns: number; stop_ns: number; primary_code: string | null;
  transition?: Transition | null; semantic_text?: string; notes?: {text: string}[]};
export function deriveTransitionEndpoints<T extends Part>(parts: T[]): (T & {transition: Transition | null})[] {
  return parts.map((part, i) => ({...part, transition: part.primary_code === "posture_transition" ? {
    from_posture: supportPosture(parts[i - 1]?.primary_code ?? null),
    to_posture: supportPosture(parts[i + 1]?.primary_code ?? null),
  } : null}));
}
export function reviewIssues(parts: Part[]): ReviewIssue[] {
  const issues: ReviewIssue[] = [];
  const add = (code: string, message: string, ...segments: Part[]) => issues.push({code, message, segment_ids: segments.map(s => s.segment_id)});
  parts.forEach((part, i) => {
    const code = part.primary_code, before = parts[i - 1], after = parts[i + 1];
    const description = [part.semantic_text ?? "", ...(part.notes ?? []).map(n => n.text)].join(" ").trim();
    if (code === "other_activity" && !description) add("other_semantics", `第 ${i + 1} 段其他活动需要填写区间语义。`, part);
    if (code === "posture_transition") {
      const t = part.transition;
      if (!t) { add("transition_fields", `第 ${i + 1} 段缺少转换端点字段。`, part); return; }
      if (t.from_posture && t.from_posture === t.to_posture && !description)
        add("return_transition", `第 ${i + 1} 段起止姿态相同，请说明转移、往返或未完成情况。`, part);
      for (const [endpoint, neighbor] of [[t.from_posture, before], [t.to_posture, after]] as const) {
        const pose = neighbor && supportPosture(neighbor.primary_code);
        if (pose && endpoint && pose !== endpoint)
          add("transition_endpoint", `第 ${i + 1} 段转换端点与相邻姿态矛盾，请纠正或保留未知端点。`, part, neighbor);
      }
    }
    if (before?.primary_code === "posture_transition" && code === "posture_transition")
      add("adjacent_transitions", `第 ${i}、${i + 1} 段转换直接相邻，请合并或根据真实中间姿态切分。`, before, part);
    else if (before?.primary_code && code && before.primary_code in postureNames && code in postureNames && before.primary_code !== code)
      add("missing_transition", `第 ${i}、${i + 1} 段支撑姿态直接变化，请补充真实转换或明确待判范围。`, before, part);
  });
  return issues;
}
