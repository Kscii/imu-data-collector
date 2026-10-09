export type MotionEvidenceItem = {kind: "observation" | "adjustment"; code: string; text: string};
export type MotionPredictionQuality = {intervals: {start_ns: number; stop_ns: number; primary_code: string | null;
  high_confidence_fraction: number; boundary_reliable: boolean; reason?: string; evidence?: MotionEvidenceItem[]}[]};

export function MotionEvidence({items}: {items?: MotionEvidenceItem[]}) {
  if (!items?.length) return null;
  return <details className="motion-evidence"><summary>判断依据</summary>
    <div>{items.slice(0, 3).map((item, i)=><p key={`${item.code}:${i}`}>
      <span>{item.kind === "observation" ? "数据特征" : "算法调整"}：</span>{item.text}
    </p>)}</div>
    <small>说明对应原自动建议，不代表人工确认或确定的因果解释。</small>
  </details>;
}
