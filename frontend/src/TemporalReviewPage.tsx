import {useEffect, useRef, useState} from "react";
import {LegacyTemporalReviewPage} from "./LegacyTemporalReviewPage";
import {SyntheticIMUChart} from "./SyntheticIMUChart";
import "./temporalReview.css";
const root = "/api/v1/synthetic/temporal/runs";
type Segment = {segment_id: string; start_ns: number; stop_ns: number; primary_code: string | null;
  auxiliary_code: null; auxiliary_status: string; resolution: string; reason: string | null; fall_event_id: string | null};
type Fall = {event_id: string; code: string; start_ns: number; impact_ns: number; stop_ns: number; evidence: string};
type Annotation = {candidate: {candidate_id: string; version_id: string};
  clock: {duration_ns: number; source_rate_hz: number}; segments: Segment[]; fall_events: Fall[];
  taxonomy: {primary: {code: string; name: string; is_fall: boolean}[]}; [key: string]: unknown};
type Case = {entry: {candidate: {candidate_id: string; version_id: string}; source_dataset: string;
  source_member: string; reference_role: string; clock: {source_rate_hz: number}};
  status: string; proposal: {document: Annotation} | null; proposal_sha256: string | null;
  review: {revision_sha256: string} | null; reviewed_document?: Annotation | null; reference_template?: Annotation};
type Run = {run_id: string; schema: string; count: number};
type Training = {eligible_reviews: number; new_reviews: number;
  jobs: {request: {job_id: string}; state: string; result?: {error?: string} | null}[]};
type Model = {model_id: string; metadata: {labels: string[]; supported_indices: number[]}};
type Metric = {variant: string; partition: string; macro_f1: number | null; identified_precision: number | null;
  known_reference_coverage: number | null; boundary_mae_s: number | null;
  per_class: {code: string; reference_samples: number; precision: number | null; recall: number | null}[]};
type Report = {report: {reference_count: number; development_count?: number; metrics: Metric[]}};
type Snapshot = {snapshot_id: string; state: string; candidate_count: number; result: {shards: {object_key: string}[]} | null};
const names: Record<string, string> = {standing: "站立", walking: "行走", running: "跑步", sitting: "坐姿",
  lying: "躺姿", squatting: "蹲姿", sit_down: "坐下", stand_up: "起立", lie_down: "躺下", get_up: "起身",
  squatting_down: "蹲下", stair_up: "上楼", stair_down: "下楼", bend_or_pick_up: "弯腰／拾物",
  stumble_recovery: "失衡后恢复", jumping: "跳跃", turning: "转身／转向", crawling: "爬行", kneeling: "跪姿",
  forward_fall: "向前跌倒", backward_fall: "向后跌倒", left_lateral_fall: "向左跌倒", right_lateral_fall: "向右跌倒",
  fall_unspecified: "跌倒，方向不明", unrecognized: "无法识别", uncovered: "词表未覆盖", uncertain_boundary: "边界无法确定",
  reference_needed: "待独立标注", accepted: "已通过", rejected: "质量不合格", pending_review: "待检查",
  inference_pending: "待模型处理", queued: "排队中", complete: "已完成", failed: "失败"};
const label = (code: string | null) => code ? names[code] ?? code : "无法识别";
const secs = (ns: number) => (ns / 1e9).toFixed(2);
const percent = (n: number | null | undefined) => n == null ? "—" : `${(100 * n).toFixed(1)}%`;
async function api<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(path, body === undefined ? {} : {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  const value = await response.json();
  if (!response.ok) throw new Error(typeof value.detail === "string" ? value.detail : `HTTP ${response.status}`);
  return value;
}
export function TemporalReviewPage() {
  const [runs, setRuns] = useState<Run[]>([]), [error, setError] = useState("");
  const requested = new URLSearchParams(location.search).get("temporal_run");
  useEffect(() => {void api<{runs: Run[]}>(root).then(r => setRuns(r.runs)).catch(e => setError(String(e)));}, []);
  const selected = runs.find(r => r.run_id === requested) ?? runs[0];
  if (!selected) return <section className="panel">{error || "暂无标注任务"}</section>;
  return selected.schema === "imu.temporal_run.v2" ? <PrimaryReviewPage run={selected} runs={runs} /> : <LegacyTemporalReviewPage />;
}
function PrimaryReviewPage({run, runs}: {run: Run; runs: Run[]}) {
  const path = `${root}/${run.run_id}`;
  const [cases, setCases] = useState<Case[]>([]), [selected, setSelected] = useState(0), [part, setPart] = useState(0);
  const [editor, setEditor] = useState<Annotation | null>(null), [queue, setQueue] = useState(false), [details, setDetails] = useState(false);
  const [editorKey, setEditorKey] = useState("");
  const [busy, setBusy] = useState(false), [error, setError] = useState(""), [notice, setNotice] = useState("");
  const [ack, setAck] = useState(false), [note, setNote] = useState(""), [cursorFrame, setCursorFrame] = useState(0);
  const [training, setTraining] = useState<Training | null>(null), [models, setModels] = useState<Model[]>([]);
  const [report, setReport] = useState<Report | null>(null), [snapshots, setSnapshots] = useState<Snapshot[]>([]);
  const frame = useRef<HTMLIFrameElement>(null), chartReady = useRef(false), began = useRef(Date.now());
  const current = cases[selected], id = current?.entry.candidate;
  const casePath = id ? `${path}/cases/${id.candidate_id}/${id.version_id}` : "";
  const base = id ? `/api/v1/synthetic/candidates/${id.candidate_id}/${id.version_id}` : "";
  const independent = current?.status === "reference_needed";
  const original = independent ? current?.reference_template : current?.reviewed_document ?? current?.proposal?.document;
  const draftKey = `primary-draft:${casePath}:${independent ? "reference" : current?.proposal_sha256}:${current?.review?.revision_sha256 ?? "none"}`;
  const dirty = editor && JSON.stringify(editor) !== JSON.stringify(original);
  const segment = editor?.segments[part], sourceRate = current?.entry.clock.source_rate_hz ?? 25;
  const cursorNs = Math.round(cursorFrame / sourceRate * 1e9), duration = editor?.clock.duration_ns ?? 0;
  const excluded = editor?.segments.some(s => s.resolution !== "identified");
  const control = (action: string, fields = {}) => frame.current?.contentWindow?.postMessage({type: "imu-synthetic-review-control", action, ...fields}, location.origin);
  const seek = (ns: number) => control("seek", {frame: Math.round(ns / 1e9 * sourceRate)});
  const refresh = async () => {const data = await api<{cases: Case[]}>(`${path}?limit=100`); setCases(data.cases);};
  const refreshDetails = async () => {
    const [t, m, r, s] = await Promise.all([api<Training>(`${path}/training`), api<{models: Model[]}>(`${path}/models`),
      api<Report>(`${path}/report`), api<{snapshots: Snapshot[]}>(`${path}/snapshots`)]);
    setTraining(t); setModels(m.models); setReport(r); setSnapshots(s.snapshots);
  };
  useEffect(() => {void refresh().catch(e => setError(String(e)));}, [path]);
  useEffect(() => {
    if (!details) return;
    const update = () => void refreshDetails().catch(e => setError(String(e)));
    update(); const timer = setInterval(update, 10000); return () => clearInterval(timer);
  }, [details, path]);
  useEffect(() => {
    let draft = original ? structuredClone(original) : null;
    try {const saved = localStorage.getItem(draftKey); if (saved) draft = JSON.parse(saved) as Annotation;} catch { /* Use server copy. */ }
    setEditor(draft); setEditorKey(draftKey); setPart(0); setAck(false); setNote(""); setCursorFrame(0); began.current = Date.now(); chartReady.current = false;
  }, [draftKey, current?.status]);
  useEffect(() => {
    if (!editor || editorKey !== draftKey || editor.candidate.candidate_id !== id?.candidate_id) return;
    try {localStorage.setItem(draftKey, JSON.stringify(editor));} catch {setError("无法暂存本地草稿，请保持页面并保存。");}
  }, [editor, editorKey, draftKey, id?.candidate_id]);
  useEffect(() => {
    const listener = (e: MessageEvent) => {
      if (e.origin !== location.origin || e.source !== frame.current?.contentWindow) return;
      if (["imu-synthetic-review-frame", "imu-synthetic-review-ready"].includes(e.data?.type) && Number.isInteger(e.data.frame)) setCursorFrame(e.data.frame);
      if (e.data?.type === "imu-synthetic-review-ready") control("chart-ready", {ready: chartReady.current});
      if (e.data?.type === "imu-synthetic-review-key" && e.data.key === " ") control("toggle");
    };
    const keyboard = (e: KeyboardEvent) => {
      if (e.code === "Space" && !(e.target instanceof HTMLElement && e.target.closest("input,select,textarea,button")) && !details && !queue) {
        e.preventDefault(); control("toggle");}
    };
    addEventListener("message", listener); addEventListener("keydown", keyboard);
    return () => {removeEventListener("message", listener); removeEventListener("keydown", keyboard);};
  }, [casePath, details, queue]);
  const act = async (operation: () => Promise<unknown>, message: string) => {
    setBusy(true); setError(""); setNotice("");
    try {await operation(); await refresh(); if (details) await refreshDetails(); setNotice(message);}
    catch (e) {setError(String(e));} finally {setBusy(false);}
  };
  const change = (fields: Partial<Segment>) => setEditor(old => old && ({...old, segments: old.segments.map((s, i) => i === part ? {...s, ...fields} : s)}));
  const changePrimary = (code: string) => {
    const known = editor?.taxonomy.primary.find(t => t.code === code);
    change(known ? {primary_code: code, resolution: "identified", reason: null,
      fall_event_id: known.is_fall ? (segment?.primary_code === code ? segment.fall_event_id : null) ?? `fall-${crypto.randomUUID()}` : null}
      : {primary_code: null, resolution: code, reason: label(code), fall_event_id: null});
  };
  const boundary = (side: "start_ns" | "stop_ns", value: number) => {
    if (!editor || !segment || !Number.isFinite(value)) return;
    const point = Math.round(value / 40_000_000) * 40_000_000, i = side === "start_ns" ? part - 1 : part;
    if (i < 0 || i >= editor.segments.length - 1 || point <= editor.segments[i].start_ns || point >= editor.segments[i + 1].stop_ns) return;
    setEditor({...editor, segments: editor.segments.map((s, n) => n === i ? {...s, stop_ns: point} : n === i + 1 ? {...s, start_ns: point} : s)});
  };
  const split = () => {
    if (!editor || !segment) return;
    const point = Math.round(cursorNs / 40_000_000) * 40_000_000;
    if (point <= segment.start_ns || point >= segment.stop_ns) {setError("先将播放位置移到所选区间内部。"); return;}
    setEditor({...editor, segments: editor.segments.flatMap((s, i) => i === part ? [{...s, stop_ns: point},
      {...s, segment_id: `segment-${crypto.randomUUID()}`, start_ns: point}] : [s])});
  };
  const merge = () => {
    if (!editor || !segment || part >= editor.segments.length - 1) return;
    setEditor({...editor, segments: editor.segments.flatMap((s, i) => i === part ? [{...s, stop_ns: editor.segments[i + 1].stop_ns}] : i === part + 1 ? [] : [s])});
  };
  const finalized = () => {
    if (!editor) throw new Error("没有可保存的区间");
    const events = new Map<string, Segment[]>();
    for (const s of editor.segments) if (s.fall_event_id) events.set(s.fall_event_id, [...(events.get(s.fall_event_id) ?? []), s]);
    return {...editor, fall_events: [...events].map(([event_id, parts]) => {
      const event = editor.fall_events.find(e => e.event_id === event_id);
      if (!event) throw new Error("请填写跌倒撞击时刻。");
      return {...event, code: parts[0].primary_code!, start_ns: parts[0].start_ns, stop_ns: parts[parts.length - 1].stop_ns};
    })};
  };
  const save = (decision = "pass") => void act(async () => {
    if (independent) await api(`${casePath}/reference`, finalized());
    else await api(`${casePath}/${decision === "pass" && (dirty || current.reviewed_document) ? "correction" : "review"}`, {
      decision, proposal_sha256: current.proposal_sha256, expected_review_sha256: current.review?.revision_sha256 ?? null,
      reason_codes: decision === "reject" ? ["motion_quality"] : decision === "return" ? ["insufficient_evidence"] : [],
      failed_segment_ids: [], reason: note, acknowledge_exclusions: ack, review_seconds: Math.min(86400, (Date.now() - began.current) / 1000),
      corrected_document: decision === "pass" && (dirty || current.reviewed_document) ? finalized() : null,
    });
    localStorage.removeItem(draftKey);
  }, independent ? "独立参考已冻结，可以查看模型建议。" : decision === "pass" ? "人工审核已保存。" : "处理结果已保存。");
  const fall = segment?.fall_event_id ? editor?.fall_events.find(f => f.event_id === segment.fall_event_id) : null;
  return <main className="primary-workbench" data-no-localize>
    <header className="primary-toolbar panel"><button onClick={() => setQueue(true)}>片段队列 · {selected + 1}/{cases.length || run.count}</button>
      <strong>主活动标注</strong><span className="primary-task" title={current?.entry.source_member}>{current?.entry.source_dataset} · {current?.entry.source_member}</span>
      <button disabled={busy || selected === 0} onClick={() => setSelected(s => s - 1)}>上一段</button>
      <button disabled={busy || selected >= cases.length - 1} onClick={() => setSelected(s => s + 1)}>下一段</button><button onClick={() => setDetails(true)}>报告与模型</button></header>
    {(error || notice) && <div className={`primary-notice ${error ? "primary-error" : ""}`} role={error ? "alert" : "status"}><span>{error || notice}</span>
      <button aria-label="关闭提示" onClick={() => {setError(""); setNotice("");}}>×</button></div>}
    {current && <div className="primary-body"><section className="primary-viewer panel">
      <iframe key={base} ref={frame} src={`${base}/files/index.html?bridge=2`} title="动捕与IMU回放" onLoad={() => control("chart-ready", {ready: chartReady.current})} />
      <SyntheticIMUChart compact base={base} cursorFrame={cursorFrame} onInspect={() => {}}
        onReady={ready => {chartReady.current = ready; control("chart-ready", {ready});}} onSeek={index => control("seek", {frame: index})} />
      <div className="primary-timeline" aria-label="活动时间轴">{editor?.segments.map((s, i) => <button key={s.segment_id}
        title={`${label(s.primary_code ?? s.resolution)} ${secs(s.start_ns)}–${secs(s.stop_ns)} 秒`} style={{flexGrow: s.stop_ns - s.start_ns}}
        className={`${s.primary_code ? "identified" : "unknown"} ${i === part ? "selected" : ""}`} onClick={() => {setPart(i); seek(s.start_ns);}}>{label(s.primary_code ?? s.resolution)}</button>)}</div>
      <div className="primary-playback"><button onClick={() => control("toggle")}>播放／暂停</button><button onClick={() => control("replay")}>重播</button>
        <input type="range" aria-label="播放位置" min={0} max={duration / 1e9} step={1 / sourceRate} value={Math.min(cursorNs, duration) / 1e9}
          onChange={e => seek(Number(e.target.value) * 1e9)} /><span>{secs(cursorNs)} / {secs(duration)} 秒</span></div></section>
      <aside className="primary-editor panel"><header><strong>{independent ? "独立参考标注" : "检查与纠正"}</strong><span>{label(current.status)}</span>
        <small>{independent ? "先看回放并标注；保存后显示模型结果。" : "核对动作和时间边界，必要时直接修改。"}</small></header>
        <div className="primary-editor-scroll"><div className="primary-segments" aria-label="活动区间">{editor?.segments.map((s, i) => <button key={s.segment_id}
          className={part === i ? "selected" : ""} onClick={() => {setPart(i); seek(s.start_ns);}}><span>{i + 1}. {label(s.primary_code ?? s.resolution)}</span>
          <small>{secs(s.start_ns)}–{secs(s.stop_ns)} s</small></button>)}</div>
          {segment && <div className="primary-fields"><label>主活动<select aria-label="主活动" value={segment.primary_code ?? segment.resolution} onChange={e => changePrimary(e.target.value)}>
            {editor?.taxonomy.primary.map(t => <option key={t.code} value={t.code}>{label(t.code)}</option>)}
            {["unrecognized", "uncovered", "uncertain_boundary"].map(c => <option key={c} value={c}>{label(c)}</option>)}</select></label>
            <div className="primary-boundaries">{(["start_ns", "stop_ns"] as const).map((key, i) => <label key={key}>{i ? "结束（秒）" : "开始（秒）"}
              <input aria-label={i ? "结束秒" : "开始秒"} type="number" step="0.04" min="0" max={duration / 1e9} disabled={i ? part === editor!.segments.length - 1 : part === 0}
                value={segment[key] / 1e9} onChange={e => boundary(key, Number(e.target.value) * 1e9)} /></label>)}</div>
            <div className="primary-edit-buttons"><button onClick={split}>在播放位置切分</button><button disabled={part === editor!.segments.length - 1} onClick={merge}
              title="合并后采用当前区间的类别">合并下一段</button></div>
            {segment.fall_event_id && <label>主要撞击（秒）<input type="number" step="0.04" min={segment.start_ns / 1e9} max={segment.stop_ns / 1e9}
              value={fall?.impact_ns == null ? "" : fall.impact_ns / 1e9} onChange={e => setEditor(old => old && ({...old,
                fall_events: [...old.fall_events.filter(f => f.event_id !== segment.fall_event_id), {event_id: segment.fall_event_id!, code: segment.primary_code!,
                  start_ns: segment.start_ns, stop_ns: segment.stop_ns, impact_ns: Math.round(Number(e.target.value) * 1e9), evidence: "human replay inspection"}]}))} /></label>}
            <label>备注（可选）<textarea rows={2} value={note} onChange={e => setNote(e.target.value)} placeholder="动作含义或回放质量问题" /></label></div>}
        </div><footer className="primary-actions">{excluded && <label><input type="checkbox" checked={ack} onChange={e => setAck(e.target.checked)} />确认无法识别的区间不用于训练</label>}
          <button className="primary-save" disabled={busy || !editor || (!!excluded && !ack)} onClick={() => save()}>{busy ? "保存中…" : independent ? "保存独立参考" : dirty ? "保存纠正并通过" : "通过当前标注"}</button>
          {!independent && <div><button disabled={busy || !current.proposal} onClick={() => save("return")}>暂不通过</button>
            <button disabled={busy || !current.proposal} onClick={() => save("reject")}>片段质量不合格</button></div>}</footer></aside></div>}
    {queue && <div className="primary-overlay" onClick={() => setQueue(false)}><section role="dialog" aria-modal="true" aria-label="片段队列" className="primary-drawer panel" onClick={e => e.stopPropagation()}>
      <header><strong>片段队列</strong><button onClick={() => setQueue(false)}>关闭</button></header><select aria-label="选择试验任务" value={run.run_id}
        onChange={e => {const url = new URL(location.href); url.searchParams.set("temporal_run", e.target.value); location.assign(url);}}>
        {runs.map(r => <option key={r.run_id} value={r.run_id}>{r.run_id}</option>)}</select><p>开发 20 段 · 独立验证 20 段</p>
      <div className="primary-queue-list">{cases.map((c, i) => <button key={c.entry.candidate.candidate_id} className={i === selected ? "selected" : ""}
        onClick={() => {setSelected(i); setQueue(false);}}><strong>{i + 1}. {c.entry.source_dataset} · {c.entry.reference_role === "validation" ? "验证" : "开发"}</strong>
        <span>{label(c.status)}</span><small>{c.entry.source_member}</small></button>)}</div></section></div>}
    {details && <div className="primary-overlay" onClick={() => setDetails(false)}><section role="dialog" aria-modal="true" aria-label="报告与模型" className="primary-details panel" onClick={e => e.stopPropagation()}>
      <header><strong>报告与模型</strong><button onClick={() => setDetails(false)}>关闭</button></header><div className="primary-details-scroll">
      <h3>人工检查</h3><p>开发已通过 {training?.eligible_reviews ?? 0}/20 · 独立参考 {report?.report.reference_count ?? 0}/20</p><p>验证参考不参与训练。样本不足的类别继续保留为覆盖缺口。</p>
      <div className="primary-details-actions"><button disabled={busy || !training?.eligible_reviews || !training.new_reviews || training.jobs.some(j => ["queued", "running"].includes(j.state))}
        onClick={() => void act(() => api(`${path}/training`, {}), "训练任务已提交；生成候选模型后在这里查看。")}>用已确认样本训练新模型</button>
        <button onClick={() => void act(refreshDetails, "报告已刷新")}>刷新</button></div>
      {training?.jobs.map(j => <p key={j.request.job_id}>{j.request.job_id} · {j.state === "running" ? "训练中" : label(j.state)} {j.result?.error && <span className="primary-error">{j.result.error}</span>}</p>)}
      <h3>模型下载</h3>{models.map(m => <div key={m.model_id} className="primary-model"><strong>{m.model_id}</strong><a href={`${path}/models/${m.model_id}/model-bundle.zip`}>完整模型包</a>
        <a href={`${path}/models/${m.model_id}/model.onnx`}>ONNX</a><small>训练覆盖 {m.metadata.supported_indices.length}/24 类；这些是弱标签训练覆盖，准确率待独立验证。</small></div>)}
      <h3>独立验证比较</h3>{!report?.report.metrics.some(m => m.partition === "validation") ? <p>等待人工独立参考，当前没有可报告的准确率。</p> :
        <div className="primary-metrics"><table><thead><tr><th>方案</th><th>已识别精度</th><th>覆盖率</th><th>Macro F1</th><th>边界误差</th></tr></thead><tbody>
          {report.report.metrics.filter(m => m.partition === "validation").map(m => <tr key={m.variant}><td>{m.variant}</td><td>{percent(m.identified_precision)}</td>
            <td>{percent(m.known_reference_coverage)}</td><td>{percent(m.macro_f1)}</td><td>{m.boundary_mae_s == null ? "—" : `${m.boundary_mae_s.toFixed(2)} s`}</td></tr>)}</tbody></table>
          {report.report.metrics.filter(m => m.partition === "validation").map(m => <details key={m.variant}><summary>{m.variant} · 逐类精度与召回</summary>
            {m.per_class.map(c => <p key={c.code}>{label(c.code)} · {c.reference_samples ? `${(c.reference_samples / 25).toFixed(1)} s，精度 ${percent(c.precision)}，召回 ${percent(c.recall)}` : "无验证样本"}</p>)}</details>)}</div>}
      <h3>导出人工通过的标注</h3><button disabled={busy || !cases.some(c => c.status === "accepted")} onClick={() => void act(() => api(`${path}/snapshots`, {}), "已请求生成正式 HDF5 3.3 快照。")}>生成 HDF5 3.3 快照</button>
      {snapshots.map(s => <p key={s.snapshot_id}>{s.candidate_count} 段 · {label(s.state)} {s.result?.shards.map(f => <a key={f.object_key}
        href={`${path}/snapshots/${s.snapshot_id}/shards/${f.object_key.split("/").pop()}`}>{f.object_key.split("/").pop()}</a>)}</p>)}</div></section></div>}
  </main>;
}
