import {useEffect, useRef, useState} from "react";
import {SyntheticIMUChart} from "./SyntheticIMUChart";
import "./temporalReview.css";

const root = "/api/v1/synthetic/temporal";
type Segment = {segment_id: string; start_ns: number; stop_ns: number; primary_code: string | null;
  auxiliary_code: string | null; auxiliary_status: string; resolution: string; reason: string | null;
  fall_event_id: string | null};
type Fall = {event_id: string; code: string; start_ns: number; impact_ns: number; stop_ns: number; evidence: string};
type Document = {schema: string; candidate: {candidate_id: string; version_id: string};
  clock: {duration_ns: number; source_rate_hz: number}; auxiliary_mode: string;
  taxonomy: {primary: {code: string; name: string; is_fall: boolean}[]; auxiliary: {code: string; name: string}[]};
  segments: Segment[]; fall_events: Fall[]; [key: string]: unknown};
type Case = {entry: {candidate: {candidate_id: string; version_id: string}; source_dataset: string;
  source_member: string; reference_role: string; clock: {source_rate_hz: number}}; status: string;
  proposal: {document: Document; attempt: number} | null; proposal_sha256: string | null;
  review: {revision_sha256: string} | null; reference_template?: Document;
  reference?: {document: Document} | null};
type Run = {run_id: string; phase: string; count: number};
type Snapshot = {snapshot_id: string; state: string; candidate_count: number;
  result: {shards: {object_key: string; byte_length: number}[]} | null};
type Metric = {variant: string; partition: string; primary_accuracy: number | null;
  auxiliary_accuracy: number | null; boundary_mae_s: number | null; boundary_p90_s: number | null;
  auxiliary_boundary_mae_s: number | null; auxiliary_boundary_p90_s: number | null;
  impact_mae_s: number | null; missed_segments: number; extra_segments: number};
type Report = {sha256: string; report: {status: string; reference_count: number; metrics: Metric[];
  notes: string[]}; approval: unknown};
const names: Record<string, string> = {
  standing: "站立", walking: "行走", running: "跑步", sitting: "坐姿", lying: "躺姿", squatting: "蹲姿",
  sit_down: "坐下", stand_up: "起立", lie_down: "躺下", get_up: "起身", squatting_down: "蹲下",
  stair_up: "上楼", stair_down: "下楼", bend_or_pick_up: "弯腰／拾物", stumble_recovery: "失衡后恢复",
  jumping: "跳跃", turning: "转身／转向", crawling: "爬行", kneeling: "跪姿", forward_fall: "向前跌倒",
  backward_fall: "向后跌倒", left_lateral_fall: "向左跌倒", right_lateral_fall: "向右跌倒",
  fall_unspecified: "跌倒，方向不明", backward_movement: "向后移动", sideways_movement: "侧向移动",
  waving: "挥手", clapping: "拍手", pointing: "指向", reaching: "伸手", stretching: "伸展",
  kicking: "踢", punching: "出拳", throwing: "投掷", catching: "接取", carrying_object: "搬持物体",
  lifting_object: "举起物体", placing_object: "放置物体", unrecognized: "无法识别", uncovered: "词表未覆盖",
  uncertain_boundary: "边界无法确定", reference_needed: "待独立参考标注", accepted: "已通过", rejected: "质量不合格",
  pending_review: "待检查", inference_pending: "待模型处理", retry_pending: "待自动重试", retry_exhausted: "待确认排除",
};
const reasons: Record<string, string> = {primary_class: "主类别错误", boundary: "起止边界错误",
  missing_activity: "漏标动作", auxiliary: "辅助标签错误", missing_impact: "撞击点错误或缺失",
  insufficient_evidence: "证据不足"};
const label = (code: string | null) => code ? names[code] ?? code : "无";
const seconds = (ns: number) => (ns / 1e9).toFixed(3);
async function api<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(path, body === undefined ? {} : {method: "POST",
    headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  const value = await response.json();
  if (!response.ok) throw new Error(typeof value.detail === "string" ? value.detail : `HTTP ${response.status}`);
  return value;
}

export function LegacyTemporalReviewPage() {
  const [runs, setRuns] = useState<Run[]>([]);
  const [runId, setRunId] = useState(new URLSearchParams(location.search).get("temporal_run") ?? "");
  const [cases, setCases] = useState<Case[]>([]);
  const [offset, setOffset] = useState(0);
  const [total, setTotal] = useState(0);
  const [selected, setSelected] = useState(0);
  const [editor, setEditor] = useState<Document | null>(null);
  const [failure, setFailure] = useState<string[]>([]);
  const [failedSegments, setFailedSegments] = useState<string[]>([]);
  const [note, setNote] = useState("");
  const [ack, setAck] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [cursorFrame, setCursorFrame] = useState(0);
  const [report, setReport] = useState<Report | null>(null);
  const [snapshots, setSnapshots] = useState<Snapshot[]>([]);
  const [auxMode, setAuxMode] = useState("temporal");
  const frame = useRef<HTMLIFrameElement>(null);
  const chartReady = useRef(false);
  const current = cases[selected];
  const id = current?.entry.candidate;
  const casePath = id ? `${root}/runs/${runId}/cases/${id.candidate_id}/${id.version_id}` : "";
  const base = id ? `/api/v1/synthetic/candidates/${id.candidate_id}/${id.version_id}` : "";
  const independent = current?.status === "reference_needed";
  const document = independent ? editor : current?.proposal?.document;
  const cursorNs = Math.round(cursorFrame / (current?.entry.clock.source_rate_hz ?? 25) * 1e9);
  const control = (action: string, fields = {}) => frame.current?.contentWindow?.postMessage(
    {type: "imu-synthetic-review-control", action, ...fields}, location.origin);
  const refresh = async () => {
    const result = await api<{cases: Case[]; total: number}>(`${root}/runs/${runId}?offset=${offset}&limit=30`);
    setCases(result.cases); setTotal(result.total);
    const exported = await api<{snapshots: Snapshot[]}>(`${root}/runs/${runId}/snapshots`);
    setSnapshots(exported.snapshots);
  };
  useEffect(() => {
    api<{runs: Run[]}>(`${root}/runs`).then(value => {
      setRuns(value.runs); setRunId(prior => prior || value.runs[0]?.run_id || "");
    }).catch(e => setError(String(e)));
  }, []);
  useEffect(() => {
    if (!runId) return;
    let live = true;
    setCases([]); setSelected(0); setReport(null); setSnapshots([]);
    api<{cases: Case[]; total: number}>(`${root}/runs/${runId}?offset=${offset}&limit=30`)
      .then(value => {if (live) {setCases(value.cases); setTotal(value.total);}})
      .catch(e => {if (live) setError(String(e));});
    api<{snapshots: Snapshot[]}>(`${root}/runs/${runId}/snapshots`)
      .then(value => {if (live) setSnapshots(value.snapshots);})
      .catch(e => {if (live) setError(String(e));});
    return () => {live = false;};
  }, [runId, offset]);
  useEffect(() => {
    let draft = current?.reference_template ? structuredClone(current.reference_template) : null;
    if (draft) {
      try {
        const saved = localStorage.getItem(`temporal-reference:${casePath}`);
        if (saved) draft = JSON.parse(saved) as Document;
      } catch { /* The server validates every submitted reference. */ }
    }
    setEditor(draft);
    setFailure([]); setFailedSegments([]); setNote(""); setAck(false); setCursorFrame(0);
    chartReady.current = false;
  }, [casePath, current?.status]);
  useEffect(() => {
    if (editor && independent && editor.candidate.candidate_id === id?.candidate_id
        && editor.candidate.version_id === id?.version_id) {
      try {localStorage.setItem(`temporal-reference:${casePath}`, JSON.stringify(editor));}
      catch {setError("浏览器无法暂存参考草稿，请保持当前页面并完成保存。");}
    }
  }, [editor, casePath, independent, id]);
  useEffect(() => {
    const onMessage = (event: MessageEvent) => {
      if (event.origin !== location.origin || event.source !== frame.current?.contentWindow) return;
      if (["imu-synthetic-review-frame", "imu-synthetic-review-ready"].includes(event.data?.type)
          && Number.isInteger(event.data.frame)) setCursorFrame(event.data.frame);
      if (event.data?.type === "imu-synthetic-review-ready" && chartReady.current) {
        control("chart-ready", {ready: true});
      }
      if (event.data?.type === "imu-synthetic-review-key" && event.data.key === " ") control("toggle");
    };
    addEventListener("message", onMessage);
    return () => removeEventListener("message", onMessage);
  }, [casePath]);
  const act = async (operation: () => Promise<unknown>, success: string) => {
    setBusy(true); setError(""); setMessage("");
    try {await operation(); await refresh(); setMessage(success);}
    catch (e) {setError(String(e));}
    finally {setBusy(false);}
  };
  const review = (decision: string) => act(() => api(`${casePath}/review`, {
    decision, proposal_sha256: current.proposal_sha256,
    expected_review_sha256: current.review?.revision_sha256 ?? null,
    reason_codes: decision === "reject" ? ["motion_quality"] : failure,
    failed_segment_ids: failedSegments, reason: note, acknowledge_exclusions: ack,
  }), decision === "pass" ? "已保存人工通过记录。" : decision === "reject" ? "已排除质量不合格片段。" : "已退回自动处理队列。");
  const change = (index: number, fields: Partial<Segment>) => setEditor(old => old && ({...old,
    segments: old.segments.map((s, i) => i === index ? {...s, ...fields} : s)}));
  const changePrimary = (index: number, value: string) => {
    const known = editor?.taxonomy.primary.find(t => t.code === value);
    change(index, known ? {primary_code: value, resolution: "identified", reason: null,
      fall_event_id: known.is_fall ? (editor?.segments[index].primary_code === value
        ? editor.segments[index].fall_event_id : null) ?? `fall-${crypto.randomUUID()}` : null}
      : {primary_code: null, resolution: value, reason: names[value], auxiliary_code: null,
        auxiliary_status: "unrecognized", fall_event_id: null});
  };
  const saveReference = () => act(async () => {
    if (!editor) return;
    const grouped = new Map<string, Segment[]>();
    for (const segment of editor.segments) if (segment.fall_event_id) {
      grouped.set(segment.fall_event_id, [...(grouped.get(segment.fall_event_id) ?? []), segment]);
    }
    const events = [...grouped].map(([event_id, parts]) => {
      const old = editor.fall_events.find(e => e.event_id === event_id);
      if (!old) throw new Error("请为每次跌倒填写主要撞击时刻。");
      return {...old, event_id, code: parts[0].primary_code!, start_ns: parts[0].start_ns,
        stop_ns: parts[parts.length - 1].stop_ns};
    });
    const saved = await api(`${casePath}/reference`, {...editor, fall_events: events});
    localStorage.removeItem(`temporal-reference:${casePath}`);
    return saved;
  }, "独立参考已冻结，现在可以查看模型建议。");
  const split = (index: number) => setEditor(old => {
    if (!old) return old;
    const s = old.segments[index];
    const point = cursorNs > s.start_ns && cursorNs < s.stop_ns ? cursorNs : Math.floor((s.start_ns + s.stop_ns) / 2);
    return {...old, segments: old.segments.flatMap((row, i) => i === index
      ? [{...row, stop_ns: point}, {...row, segment_id: `segment-${crypto.randomUUID()}`, start_ns: point}] : [row])};
  });

  return <main className="temporal-workbench" data-no-localize>
    <header className="panel temporal-toolbar">
      <strong>自动区间标注</strong>
      <select aria-label="选择试验任务" value={runId} onChange={e => {setRunId(e.target.value); setOffset(0);}}>
        {!runs.length && <option value="">暂无已发布任务</option>}
        {runs.map(r => <option key={r.run_id} value={r.run_id}>{r.run_id} · {r.count} 段</option>)}
      </select>
      <button disabled={!runId || busy} onClick={() => void act(refresh, "已刷新")}>刷新</button>
      <button disabled={!runId || busy} onClick={() => void act(async () => {
        setReport(await api<Report>(`${root}/runs/${runId}/report`));
      }, "已读取试验报告")}>查看试验报告</button>
      <button disabled={!runId || busy} onClick={() => void act(() => api(`${root}/runs/${runId}/snapshots`, {}),
        "已冻结快照请求，等待工作进程生成。")}>导出已通过标注</button>
    </header>
    {error && <p className="temporal-error" role="alert">{error}</p>}
    {message && <p role="status">{message}</p>}
    <div className="temporal-columns">
      <aside className="panel temporal-queue" aria-label="标注队列">
        <p>{total} 段 · 当前 {offset + 1}–{Math.min(offset + cases.length, total)}</p>
        {cases.map((item, i) => <button key={item.entry.candidate.candidate_id} className={i === selected ? "selected" : ""}
          onClick={() => setSelected(i)}><strong>{offset + i + 1}. {item.entry.source_dataset}</strong>
          <span>{label(item.status)}</span><small>{item.entry.source_member}</small></button>)}
        <div className="temporal-toolbar"><button disabled={!offset} onClick={() => setOffset(Math.max(0, offset - 30))}>上一页</button>
          <button disabled={offset + cases.length >= total} onClick={() => setOffset(offset + 30)}>下一页</button></div>
      </aside>
      <section className="panel temporal-main">
        {current && <>
          <h2>{independent ? "独立参考标注" : "检查模型标注"} · {label(current.status)}</h2>
          {independent && <p>先根据回放标记区间和撞击点。参考保存前隐藏模型建议；只有这组参考任务允许手工编辑。</p>}
          <iframe key={base} ref={frame} src={`${base}/files/index.html?bridge=2`} title="动捕与IMU回放" className="temporal-frame"
            onLoad={() => {if (chartReady.current) control("chart-ready", {ready: true});}} />
          <SyntheticIMUChart base={base} cursorFrame={cursorFrame}
            onReady={ready => {chartReady.current = ready; control("chart-ready", {ready});}} onInspect={() => {}} onMetadata={() => {}}
            onSeek={index => control("seek", {frame: index})} />
          <div className="temporal-toolbar"><button onClick={() => control("toggle")}>播放／暂停</button>
            <button onClick={() => control("replay")}>从头播放</button><span>当前位置 {seconds(cursorNs)} 秒</span></div>
          {document && <>
            <div className="temporal-track" aria-label="主活动时间轴">{document.segments.map(s => <button key={s.segment_id}
              className={`${s.resolution !== "identified" ? "excluded" : s.fall_event_id ? "fall" : "activity"}`}
              style={{flexGrow: Math.max(0, s.stop_ns - s.start_ns)}} title={`${seconds(s.start_ns)}–${seconds(s.stop_ns)} ${label(s.primary_code ?? s.resolution)}`}
              onClick={() => control("seek", {frame: Math.round(s.start_ns / 1e9 * current.entry.clock.source_rate_hz)})}>
              {label(s.primary_code ?? s.resolution)}</button>)}</div>
            <div className="temporal-table-wrap"><table><thead><tr><th>开始（秒）</th><th>结束（秒）</th><th>主活动</th><th>辅助动作</th><th>{independent ? "分段／撞击" : "标记问题"}</th></tr></thead>
              <tbody>{document.segments.map((s, i) => <tr key={s.segment_id} className={s.start_ns <= cursorNs && cursorNs < s.stop_ns ? "current" : ""}>
                <td>{independent ? <input aria-label={`第${i + 1}段开始`} type="number" step="0.001" value={s.start_ns / 1e9}
                  onChange={e => change(i, {start_ns: Math.round(Number(e.target.value) * 1e9)})} /> : seconds(s.start_ns)}</td>
                <td>{independent ? <input aria-label={`第${i + 1}段结束`} type="number" step="0.001" value={s.stop_ns / 1e9}
                  onChange={e => change(i, {stop_ns: Math.round(Number(e.target.value) * 1e9)})} /> : seconds(s.stop_ns)}</td>
                <td>{independent ? <select aria-label={`第${i + 1}段主活动`} value={s.primary_code ?? s.resolution} onChange={e => changePrimary(i, e.target.value)}>
                  {document.taxonomy.primary.map(t => <option key={t.code} value={t.code}>{label(t.code)}</option>)}
                  {["unrecognized", "uncovered", "uncertain_boundary"].map(v => <option key={v} value={v}>{label(v)}</option>)}</select>
                  : <>{label(s.primary_code ?? s.resolution)}{s.reason && <small>{s.reason}</small>}</>}</td>
                <td>{independent ? <select aria-label={`第${i + 1}段辅助动作`} disabled={s.resolution !== "identified"}
                  value={s.auxiliary_code ?? s.auxiliary_status} onChange={e => change(i,
                    ["none", "unrecognized"].includes(e.target.value) ? {auxiliary_code: null, auxiliary_status: e.target.value}
                      : {auxiliary_code: e.target.value, auxiliary_status: "identified"})}>
                  <option value="none">无</option><option value="unrecognized">无法识别</option>
                  {document.taxonomy.auxiliary.map(t => <option key={t.code} value={t.code}>{label(t.code)}</option>)}</select>
                  : s.auxiliary_status === "unrecognized" ? "辅助无法识别" : label(s.auxiliary_code)}</td>
                <td>{independent ? <><button onClick={() => split(i)}>在当前位置拆分</button>
                  {i < document.segments.length - 1 && <button onClick={() => setEditor(old => old && ({...old,
                    segments: old.segments.flatMap((row, j) => j === i
                      ? [{...row, stop_ns: old.segments[i + 1].stop_ns}] : j === i + 1 ? [] : [row])}))}>合并下一段</button>}
                  {s.fall_event_id && <label>主要撞击（秒）<input type="number" step="0.001"
                    value={(document.fall_events.find(e => e.event_id === s.fall_event_id)?.impact_ns ?? s.start_ns) / 1e9}
                    onChange={e => setEditor(old => old && ({...old,
                      fall_events: [...old.fall_events.filter(f => f.event_id !== s.fall_event_id),
                        {event_id: s.fall_event_id!, code: s.primary_code!, start_ns: s.start_ns, stop_ns: s.stop_ns,
                          impact_ns: Math.round(Number(e.target.value) * 1e9), evidence: "Independent human replay observation"}]}))} /></label>}</>
                  : <input type="checkbox" aria-label={`第${i + 1}段有问题`} checked={failedSegments.includes(s.segment_id)}
                    onChange={e => setFailedSegments(old => e.target.checked ? [...old, s.segment_id] : old.filter(x => x !== s.segment_id))} />}</td>
              </tr>)}</tbody></table></div>
            {!independent && document.fall_events.map(event => <p key={event.event_id}>跌倒：{seconds(event.start_ns)}–{seconds(event.stop_ns)} 秒；主要撞击：{seconds(event.impact_ns)} 秒</p>)}
          </>}
          {independent ? <button className="primary" disabled={busy || !editor} onClick={() => void saveReference()}>保存独立参考</button>
            : current.proposal && <div className="temporal-review-actions">
              {document?.segments.some(s => s.resolution !== "identified") && <label><input type="checkbox" checked={ack}
                onChange={e => setAck(e.target.checked)} /> 我已检查无法识别／排除区间，同意这些区间不用于训练</label>}
              <div className="temporal-toolbar">{Object.entries(reasons).map(([code, text]) => <label key={code}>
                <input type="checkbox" checked={failure.includes(code)} onChange={e => setFailure(old => e.target.checked ? [...old, code] : old.filter(x => x !== code))} />{text}</label>)}</div>
              <textarea aria-label="退回补充说明" placeholder="补充说明（可选）" value={note} onChange={e => setNote(e.target.value)} />
              <div className="temporal-toolbar"><button className="primary" disabled={busy} onClick={() => void review("pass")}>动作与标注正确</button>
                <button disabled={busy || !failure.length} onClick={() => void review("return")}>退回自动处理</button>
                <button disabled={busy} onClick={() => void review("reject")}>动作质量不合格</button></div>
            </div>}
        </>}
        {!current && <p>发布试验任务后，这里会显示待标注片段。</p>}
      </section>
    </div>
    {!!snapshots.length && <section className="panel"><h2>已冻结的 HDF5 3.3 快照</h2>
      {snapshots.map(s => <div key={s.snapshot_id}><p>{s.snapshot_id} · {s.candidate_count} 段 · {s.result ? "已生成" : s.state === "failed" ? "生成失败，请查看工作进程日志后重试" : "等待生成，稍后刷新"}</p>
        {s.result && <><a href={`${root}/runs/${runId}/snapshots/${s.snapshot_id}/manifest`} download={`${s.snapshot_id}-result.json`}>下载清单</a>
          {s.result.shards.map(shard => {const name = shard.object_key.split("/").at(-1)!;
            return <a key={name} className="button-link" href={`${root}/runs/${runId}/snapshots/${s.snapshot_id}/shards/${name}`} download={name}>{name}</a>;})}</>}
      </div>)}
    </section>}
    {report && <section className="panel"><h2>试验报告</h2><p>独立参考：{report.report.reference_count} 段 · {report.report.status === "ready" ? "可复核" : "等待参考标注"}</p>
      <table><thead><tr><th>方案／分组</th><th>主活动正确率</th><th>辅助正确率</th><th>主边界平均／P90（秒）</th><th>辅助边界平均／P90（秒）</th><th>impact 误差（秒）</th><th>漏段／多段</th></tr></thead>
        <tbody>{report.report.metrics.map(m => <tr key={`${m.variant}-${m.partition}`}><td>{m.variant} / {m.partition}</td>
          {[m.primary_accuracy, m.auxiliary_accuracy].map((x, i) => <td key={i}>{x == null ? "未测得" : `${(x * 100).toFixed(1)}%`}</td>)}
          <td>{m.boundary_mae_s?.toFixed(3) ?? "未测得"} / {m.boundary_p90_s?.toFixed(3) ?? "未测得"}</td>
          <td>{m.auxiliary_boundary_mae_s?.toFixed(3) ?? "未测得"} / {m.auxiliary_boundary_p90_s?.toFixed(3) ?? "未测得"}</td>
          <td>{m.impact_mae_s?.toFixed(3) ?? "未测得"}</td><td>{m.missed_segments} / {m.extra_segments}</td></tr>)}</tbody></table>
      {report.report.notes.map((n, i) => <p key={i}>{n}</p>)}
      <div className="temporal-toolbar"><select aria-label="全量辅助标签模式" value={auxMode} onChange={e => setAuxMode(e.target.value)}>
        <option value="temporal">主辅都精确分段</option><option value="segment_summary">辅助只作主区间摘要</option></select>
        <button disabled={busy || report.report.status !== "ready" || !!report.approval} onClick={() => void act(
          () => api(`${root}/runs/${runId}/approve`, {report_sha256: report.sha256, auxiliary_mode: auxMode}),
          "已确认试验报告及全量标注模式。")}>确认报告，允许全量生成草稿</button></div>
    </section>}
  </main>;
}
