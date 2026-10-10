import {useEffect, useRef, useState, type SetStateAction} from "react";
import {SyntheticIMUChart} from "./SyntheticIMUChart";
import {intervalDecorations, partAt, partProgress, sourceFrame} from "./annotationPlayback";
import {anchoredNotes, joined, type SemanticFields} from "./semanticEditing";
import {MotionModelPanel} from "./MotionModelPanel";
import {IntervalSemantics} from "./IntervalSemantics";
import {MotionEvidence, type MotionPredictionQuality} from "./MotionEvidence";
import type {ActivityRules} from "./activityRules";
import {durationIssues, postureNames, deriveTransitionEndpoints, reviewIssues, reviewShortcut, type Transition, type ReviewIssue, type ReviewCommand} from "./postureReview";
import "./temporalReview.css";

const cohortBase = typeof location !== "undefined" && location.pathname.startsWith("/calibration/") ? "/calibration" : "";
const root = `${cohortBase}/api/v1/synthetic/motions`;
type Segment = SemanticFields & {segment_id: string; start_ns: number; stop_ns: number; primary_code: string | null;
  auxiliary_code: null; auxiliary_status: string; resolution: string; reason: string | null; fall_event_id: string | null; transition?: Transition | null};
type Fall = {event_id: string; code: string; start_ns: number; stop_ns: number; impact_ns: number | null; evidence: string};
type Candidate = {candidate_id: string; version_id: string};
type Variant = {candidate: Candidate; configuration_id: string;
  configuration: {layout_id: string; profile_id: string; variant: string}};
type Annotation = {candidate: Candidate; clock: {duration_ns: number; source_rate_hz: number};
  segments: Segment[]; fall_events: Fall[]; taxonomy: {version: string; primary: {code: string; name: string; is_fall: boolean}[]}};
type Row = {motion_sha256: string; status: string;
  entry: {candidate: Candidate; source_dataset: string; source_member: string}};
type Detail = Row & {variants: Variant[]; editor_template: Annotation; suggestion: Annotation | null; needs_rule_review?: boolean;
  migration?: {review_required: {segment_id: string; reason: string}[]};
  prediction_quality?: MotionPredictionQuality | null;
  suggestion_quality?: MotionPredictionQuality | null;
  preview?: {models: {baseline: string; candidate: string}; activity_rules_version: string;
    diagnostics?: {start_s: number; stop_s: number; prediction: string | null; reference_labels: string[]}[];
    qualities?: Partial<Record<"baseline" | "candidate", MotionPredictionQuality>>;
    documents: Record<"human" | "baseline" | "candidate", Annotation>};
  legacy_quality: {decision:string;reason:string|null;reviewer:string} | null;
  has_suggestion: boolean; review: {revision_sha256: string} | null; draft: {revision_sha256: string} | null};
type Lease = {token: string; expires_at: number};
const statusNames: Record<string,string> = {inference_pending: "等待自动标注", pending_review: "待检查", accepted: "已通过", rejected: "已拒绝"};
const unknownNames: Record<string,string> = {unrecognized: "无法识别", uncovered: "词表未覆盖（待复核）", uncertain_boundary: "边界不确定", invalid_data: "数据无效"};
const sec = (ns: number) => (ns/1e9).toFixed(2);
class ReviewRequestError extends Error {
  issues: ReviewIssue[];
  constructor(message: string, issues: ReviewIssue[] = []) {super(message); this.issues = issues;}
}

export async function motionApi<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(path, body === undefined ? {} : {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  const value = await response.json();
  if (!response.ok) throw new ReviewRequestError(typeof value.detail === "string" ? value.detail : value.detail?.message ?? `HTTP ${response.status}`, value.detail?.issues ?? []);
  return value;
}

export function ProductionMotionPage({isAdmin}: {isAdmin: boolean}) {
  const previewId = new URLSearchParams(location.search).get("motion_preview");
  const [comparison, setComparison] = useState<"human" | "baseline" | "candidate">("candidate");
  const [rows, setRows] = useState<Row[]>([]), [total, setTotal] = useState(0), [offset, setOffset] = useState(0);
  const [filter, setFilter] = useState(new URLSearchParams(location.search).get("status") ?? "all"), [search, setSearch] = useState("");
  const [current, setCurrent] = useState<Detail | null>(null), [editor, setStoredEditor] = useState<Annotation | null>(null);
  const setEditor = (update: SetStateAction<Annotation | null>) => setStoredEditor(previous => {
    const next = typeof update === "function" ? update(previous) : update;
    return next && !previewId && ["3.0.0", "4.0.0"].includes(next.taxonomy.version)
      ? {...next, segments: deriveTransitionEndpoints(next.segments)} : next;
  });
  const [lease, setLease] = useState<Lease | null>(null), [part, setPart] = useState(0), [queue, setQueue] = useState(false);
  const [busy, setBusy] = useState(false), [error, setError] = useState(""), [notice, setNotice] = useState("");
  const [models, setModels] = useState(false), [difference, setDifference] = useState(false), [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState(""), [rejectScope, setRejectScope] = useState("motion");
  const [issues, setIssues] = useState<ReviewIssue[]>([]);
  const [frameIndex, setFrameIndex] = useState(0), [playing, setPlaying] = useState(false);
  const [viewerReady, setViewerReady] = useState(false);
  const [plotBounds, setPlotBounds] = useState({left: 50, right: 50});
  const [rules, setRules] = useState<ActivityRules | null>(null);
  const iframe = useRef<HTMLIFrameElement>(null), list = useRef<HTMLDivElement>(null);
  const actions = useRef<(command: ReviewCommand) => void>(() => {});
  const submissionInFlight = useRef(false);
  const navigationAnchor = useRef<{previous?: string; next?: string; position: number} | null>(null);
  const follow = useRef(true), chartReady = useRef(false), loading = useRef(0);
  const initialSeek = useRef<number | null>(null);
  const session = useRef<{mid: string; token: string} | null>(null);
  const editorRef = useRef(editor); editorRef.current = editor;
  const currentRef = useRef(current); currentRef.current = current;
  const id = current?.entry.candidate;
  const base = id ? `${cohortBase}/api/v1/synthetic/candidates/${id.candidate_id}/${id.version_id}` : "";
  const casePath = current && id ? `${root}/${current.motion_sha256}/variants/${id.candidate_id}/${id.version_id}` : "";
  const cacheKey = (d: Detail) => `motion-draft:${cohortBase ? "calibration:" : ""}${d.motion_sha256}:${d.entry.candidate.candidate_id}:${d.entry.candidate.version_id}`;
  const rate = editor?.clock.source_rate_hz ?? 25, cursor = Math.round(frameIndex/rate*1e9);
  const duration = editor?.clock.duration_ns ?? 0, active = partAt(editor?.segments ?? [], Math.min(cursor, duration));
  const activeSegment = editor?.segments[active], segment = editor?.segments[part];
  const progress = activeSegment ? partProgress(activeSegment, cursor) : null;
  const names = Object.fromEntries(editor?.taxonomy.primary.map(t => [t.code, rules?.primary.find(r=>r.code===t.code)?.name_zh ?? t.name]) ?? []);
  const label = (s: Segment) => s.primary_code === "posture_transition" && s.transition
    ? `转换：${postureNames[s.transition.from_posture ?? ""] ?? "未知"}→${postureNames[s.transition.to_posture ?? ""] ?? "未知"}`
    : s.primary_code ? names[s.primary_code] ?? s.primary_code : unknownNames[s.resolution] ?? "无法识别";
  const usable = editor?.segments.reduce((n, s) => {
    const fall = editor.taxonomy.primary.find(t=>t.code===s.primary_code)?.is_fall;
    const event = editor.fall_events.find(e=>e.event_id===s.fall_event_id||e.event_id===`fall-${s.segment_id}`);
    const eligible = s.primary_code && (["3.0.0", "4.0.0"].includes(editor.taxonomy.version) || !fall || (event && event.impact_ns !== null && event.impact_ns>s.start_ns && event.impact_ns<s.stop_ns));
    return n+(eligible ? s.stop_ns-s.start_ns : 0);
  }, 0) ?? 0;
  const dirty = !previewId && !!editor && JSON.stringify(editor) !== JSON.stringify(current?.editor_template);
  const displayedQuality = current?.preview ? (comparison === "human" ? null : current.preview.qualities?.[comparison]) : dirty ? null : current?.prediction_quality;
  const quality = displayedQuality?.intervals.find(q=>q.start_ns===segment?.start_ns && q.stop_ns===segment?.stop_ns && q.primary_code===segment?.primary_code);
  const control = (action: string, fields = {}) => iframe.current?.contentWindow?.postMessage({type: "imu-synthetic-review-control", action, ...fields}, location.origin);
  const pause = () => {follow.current = false; setPlaying(false); control("pause");};
  const seek = (ns: number, boundary = false) => {if (!viewerReady) return; setPlaying(false); control("seek", {frame: sourceFrame(ns, rate, boundary)});};
  const selectPart = (index: number) => {pause(); setPart(index); seek(editor!.segments[index].start_ns, true);};
  const toggle = () => {if (!viewerReady) return; follow.current = true; control("toggle");};
  const persist = () => {
    if (previewId) return;
    const d = currentRef.current, e = editorRef.current;
    if (d && e && JSON.stringify(e) !== JSON.stringify(d.editor_template)) {
      localStorage.setItem(cacheKey(d), JSON.stringify({document: e, draft_sha256: d.draft?.revision_sha256 ?? null,
        review_sha256: d.review?.revision_sha256 ?? null}));
    }
  };
  const fetchRows = async (nextOffset = offset) => {
    if (previewId) {
      const data = await motionApi<{cases: Row[]; total: number}>(`${root}/previews/${encodeURIComponent(previewId)}`);
      const filtered = data.cases.filter(r => `${r.entry.source_dataset} ${r.entry.source_member}`.toLowerCase().includes(search.toLowerCase()));
      const page = filtered.slice(nextOffset, nextOffset+30);
      setRows(page); setTotal(filtered.length); setOffset(nextOffset);
      return page;
    }
    const data = await motionApi<{cases: Row[]; total: number}>(`${root}?limit=30&offset=${nextOffset}&status=${filter}&search=${encodeURIComponent(search)}`);
    setRows(data.cases); setTotal(data.total); setOffset(nextOffset);
    return data.cases;
  };
  const open = async (mid: string, variant?: Candidate, initialSeconds?: number) => {
    const sequence = ++loading.current;
    setBusy(true); setError(""); setIssues([]); setNotice(""); setLease(null); setViewerReady(false);
    pause();
    initialSeek.current = null;
    try {
      persist();
      if (session.current && session.current.mid !== mid) {
        await motionApi(`${root}/${session.current.mid}/release`, {token: session.current.token});
        session.current = null;
      }
      const savedToken = session.current?.mid === mid ? session.current.token : sessionStorage.getItem(`motion-lease:${cohortBase ? "calibration:" : ""}${mid}`);
      let claimed: Lease | null = null;
      try { if (!previewId) {
        claimed = await motionApi<Lease>(`${root}/${mid}/claim`, {token: savedToken ?? null});
        session.current = {mid, token: claimed.token}; sessionStorage.setItem(`motion-lease:${cohortBase ? "calibration:" : ""}${mid}`, claimed.token);
      }
      } catch (e) {setError(`${String(e)}；当前以只读方式查看。`);}
      const suffix = variant ? `/variants/${variant.candidate_id}/${variant.version_id}` : "";
      const data = await motionApi<Detail>(previewId ? `${root}/previews/${encodeURIComponent(previewId)}/${mid}` : `${root}/${mid}${suffix}`);
      if (sequence !== loading.current) return;
      let value = structuredClone(data.preview?.documents[comparison] ?? data.editor_template);
      const local = previewId ? null : localStorage.getItem(cacheKey(data));
      if (local) {
        const cached = JSON.parse(local);
        if (cached.document?.taxonomy?.version === value.taxonomy.version && cached.draft_sha256 === (data.draft?.revision_sha256 ?? null) && cached.review_sha256 === (data.review?.revision_sha256 ?? null)) {
          value = cached.document; setNotice("已恢复本地未提交草稿。");
        } else setError("存在基于旧审核版本的本地草稿，未覆盖服务器结果；可下载本地草稿进行核对。");
      }
      navigationAnchor.current = null;
      setCurrent(data); setEditor(value); setLease(claimed); setPart(0); setFrameIndex(0); setPlaying(false); setViewerReady(false);
      if (initialSeconds !== undefined && Number.isFinite(initialSeconds) && initialSeconds >= 0) {
        initialSeek.current = Math.min(initialSeconds * 1e9, value.clock.duration_ns);
      }
      if (data.entry.candidate.candidate_id === currentRef.current?.entry.candidate.candidate_id &&
          data.entry.candidate.version_id === currentRef.current?.entry.candidate.version_id) control("seek", {frame: 0});
      chartReady.current = false; follow.current = true; setQueue(false); setDifference(false);
    } catch (e) {setError(String(e));} finally {if (sequence === loading.current) setBusy(false);}
  };
  useEffect(() => {void fetchRows(0).then(async items => {
    const query = new URLSearchParams(location.search), candidate_id = query.get("candidate"), version_id = query.get("version");
    if (candidate_id && version_id) {
      const found = await motionApi<{motion_sha256:string}>(`${root}/lookup/${encodeURIComponent(candidate_id)}/${encodeURIComponent(version_id)}`);
      const time = query.get("time");
      await open(found.motion_sha256, {candidate_id,version_id}, time === null ? undefined : Number(time));
    } else if (items[0]) await open(items[0].motion_sha256);
  }).catch(e => setError(String(e)));}, []);
  useEffect(() => {void motionApi<ActivityRules>(`${root}/activity-rules?version=${encodeURIComponent(editor?.taxonomy.version ?? "4.0.0")}`).then(setRules).catch(e=>setError(String(e)));}, [editor?.taxonomy.version]);
  useEffect(() => {
    if (viewerReady && initialSeek.current !== null) {
      const target = initialSeek.current; initialSeek.current = null;
      follow.current = true; seek(target);
    }
  }, [viewerReady]);
  useEffect(() => {
    const timer = setInterval(() => {
      const s = session.current;
      if (s) void motionApi<Lease>(`${root}/${s.mid}/claim`, {token: s.token}).then(setLease).catch(e => {setLease(null); setError(String(e));});
    }, 60000);
    return () => {clearInterval(timer); const s = session.current; if (s) void motionApi(`${root}/${s.mid}/release`, {token: s.token});};
  }, []);
  useEffect(() => {try {persist();} catch {setError("本地暂存失败，请保持页面并下载草稿备份。");}}, [editor]);
  useEffect(() => {
    const listener = (event: MessageEvent) => {
      if (event.origin !== location.origin || event.source !== iframe.current?.contentWindow) return;
      if (["imu-synthetic-review-frame", "imu-synthetic-review-ready"].includes(event.data?.type) && Number.isInteger(event.data.frame)) setFrameIndex(event.data.frame);
      if (typeof event.data?.playing === "boolean") setPlaying(event.data.playing);
      if (event.data?.type === "imu-synthetic-review-ready") {
        setViewerReady(event.data.ready === true);
        control("chart-ready", {ready: chartReady.current});
      }
      if (event.data?.type === "imu-synthetic-review-key") {
        const command = reviewShortcut(event.data);
        if (command) actions.current(command);
      }
    };
    const keyboard = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement;
      const command = reviewShortcut(event, !!target?.closest("input,textarea,select,[contenteditable=true]"), !!target?.closest("button,summary"));
      if (command) {event.preventDefault(); actions.current(command);}
    };
    addEventListener("message", listener); addEventListener("keydown", keyboard);
    return () => {removeEventListener("message", listener); removeEventListener("keydown", keyboard);};
  }, [base, queue, models, difference, rejecting, viewerReady]);
  useEffect(() => {
    if (follow.current && active >= 0) setPart(active);
    const container = list.current, item = container?.children[active] as HTMLElement | undefined;
    if (playing && follow.current && container && item) {
      const area = container.getBoundingClientRect(), bounds = item.getBoundingClientRect();
      if (bounds.top < area.top) container.scrollTop -= area.top-bounds.top;
      else if (bounds.bottom > area.bottom) container.scrollTop += bounds.bottom-area.bottom;
    }
  }, [active, playing]);
  const change = (fields: Partial<Segment>) => setEditor(old => old && ({...old, segments: old.segments.map((s,i) => i === part ? {...s,...fields} : s)}));
  const split = () => {
    if (!editor || !segment || !lease || busy || previewId || !viewerReady) return;
    pause(); setIssues([]);
    const point = Math.round(cursor / 40000000) * 40000000;
    if (point <= segment.start_ns || point >= segment.stop_ns) {setError("请先将播放位置移到所选区间内部。"); return;}
    if (!rules) {setError("分类规则尚未加载，请稍后重试。"); return;}
    const minimum = rules.segmentation?.minimum_interval_ns ?? 40000000;
    if (point-segment.start_ns < minimum || segment.stop_ns-point < minimum) {
      setError(`切分后每段至少需要 ${(minimum/1e9).toFixed(2)} 秒，请移动播放位置。`); return;
    }
    setEditor({...editor, segments: editor.segments.flatMap((s, i) => i === part ? [
      {...s, notes: anchoredNotes(s), stop_ns: point},
      {...s, notes: anchoredNotes(s), segment_id: `segment-${crypto.randomUUID()}`, start_ns: point},
    ] : [s])});
  };
  const merge = () => {
    if (!editor || !segment || !lease || busy || previewId || !viewerReady) return;
    const next = editor.segments[part + 1]; if (!next) return;
    pause(); setIssues([]);
    if (segment.primary_code === "fall" && next.primary_code === "fall" &&
        (!segment.fall_event_id || !next.fall_event_id || segment.fall_event_id !== next.fall_event_id)) {
      setError("不能合并两次独立跌倒。"); return;
    }
    const value = joined(segment, next);
    setEditor({...editor, segments: editor.segments.flatMap((s,i) => i === part ? [value] : i === part+1 ? [] : [s])});
  };
  const finalized = () => {
    if (!editor) throw new Error("没有可保存的区间");
    if (!rules) throw new Error("分类规则尚未加载，请稍后重试。");
    const doc = structuredClone(editor), events = new Map<string,Fall>();
    for (const s of doc.segments) {
      const fall = doc.taxonomy.primary.find(t => t.code === s.primary_code)?.is_fall;
      const event = doc.fall_events.find(e => e.event_id === s.fall_event_id || e.event_id === `fall-${s.segment_id}`)
        ?? (fall && ["3.0.0", "4.0.0"].includes(doc.taxonomy.version) ? {event_id: `fall-${s.segment_id}`, code: s.primary_code!, start_ns: s.start_ns, stop_ns: s.stop_ns, impact_ns: null, evidence: "Human recording review; impact unlocalized"} : undefined);
      s.review_state = s.primary_code && (!fall || event) ? "confirmed" : "pending";
      s.pending_reasons = s.review_state === "confirmed" ? [] : [fall ? "boundary" : "class"];
      s.fall_event_id = fall && event ? event.event_id : null;
      if (fall && event) {
        const previous = events.get(event.event_id);
        if (previous && (previous.code !== s.primary_code || previous.stop_ns !== s.start_ns)) throw new Error("同一次跌倒必须是相邻、同类别区间；请核对撞击时刻。");
        events.set(event.event_id, {...event,code:s.primary_code!,start_ns:previous?.start_ns??s.start_ns,stop_ns:s.stop_ns});
      }
    }
    return {...doc,fall_events:[...events.values()]};
  };
  const save = async (decision?: "pass" | "reject") => {
    if (previewId || !current || !lease || submissionInFlight.current) return;
    const found = decision === "pass" && editor ? reviewIssues(editor.segments) : [];
    if (decision !== "reject" && editor && rules?.segmentation) found.push(...durationIssues(editor.segments, rules.segmentation.minimum_interval_ns));
    if (decision === "pass" && editor) for (const event of editor.fall_events) {
      const parts = editor.segments.filter(s => s.primary_code === "fall" && (s.fall_event_id === event.event_id || `fall-${s.segment_id}` === event.event_id));
      if (parts.length && event.impact_ns !== null && !(event.impact_ns > parts[0].start_ns && event.impact_ns < parts.at(-1)!.stop_ns))
        found.push({code:"impact_range",message:"撞击时刻必须在跌倒区间内部；无法确定时请留空。",segment_ids:parts.map(s=>s.segment_id)});
    }
    setIssues(found);
    if (found.length) {pause(); const at = editor!.segments.findIndex(s => s.segment_id === found[0].segment_ids[0]); if (at >= 0) selectPart(at); return;}
    submissionInFlight.current = true;
    setBusy(true); setError(""); pause();
    try {
      const priorStart = segment?.start_ns ?? 0;
      const result = await motionApi<Detail>(`${casePath}/${decision ? "review" : "draft"}`, {
        document: finalized(), activity_rules_version: rules?.version, lease_token: lease.token, expected_draft_sha256: current.draft?.revision_sha256 ?? null,
        expected_review_sha256: current.review?.revision_sha256 ?? null, decision, scope: rejectScope, reason});
      localStorage.removeItem(cacheKey(current)); setCurrent(result); setEditor(structuredClone(result.editor_template));
      setPart(Math.max(0,partAt(result.editor_template.segments,priorStart))); setRejecting(false);
      const index = rows.findIndex(r => r.motion_sha256 === current.motion_sha256);
      if (index >= 0) navigationAnchor.current = {previous: rows[index-1]?.motion_sha256, next: rows[index+1]?.motion_sha256, position: offset+index};
      setNotice(decision === "pass" ? "整条录制已通过；待判区间保持屏蔽，跌倒分类与撞击定位分别使用。" : decision === "reject" ? "质量决定已保存。" : "草稿已保存，正式审核版本未改变。");
      await fetchRows();
    } catch (e) {
      if (e instanceof ReviewRequestError && e.issues.length) {
        setIssues(e.issues);
        const at = editor!.segments.findIndex(s=>s.segment_id===e.issues[0].segment_ids[0]); if(at>=0)selectPart(at);
      } else setError(String(e));
    } finally {submissionInFlight.current = false; setBusy(false);}
  };
  const neighbor = async (delta: number) => {
    const index = rows.findIndex(r => r.motion_sha256 === current?.motion_sha256), next = index+delta;
    if (index < 0 && navigationAnchor.current) {
      const anchor = navigationAnchor.current, targetId = delta < 0 ? anchor.previous : anchor.next;
      if (targetId) {await open(targetId); return;}
      const position = anchor.position + (delta < 0 ? -1 : 0);
      if (position >= 0 && position < total) {const page=await fetchRows(position); if(page[0])await open(page[0].motion_sha256);}
      return;
    }
    if (rows[next]) {await open(rows[next].motion_sha256); return;}
    const target = offset+delta*30;
    if (target < 0 || target >= total) return;
    const items = await fetchRows(target);
    const item = delta > 0 ? items[0] : items.at(-1);
    if (item) await open(item.motion_sha256);
  };
  const selectedIndex = rows.findIndex(r => r.motion_sha256 === current?.motion_sha256);
  actions.current = command => {
    if (busy || queue || models || difference || rejecting || submissionInFlight.current) return;
    if (command === "toggle") toggle();
    else if (command === "split") split();
    else if (command === "merge") merge();
    else if (command === "approve") void save("pass");
    else void neighbor(command === "previous" ? -1 : 1).catch(e=>setError(String(e)));
  };
  const isFall = editor?.taxonomy.primary.find(t => t.code === segment?.primary_code)?.is_fall;
  const fall = editor?.fall_events.find(e => e.event_id === segment?.fall_event_id || e.event_id === `fall-${segment?.segment_id}`);
  return <main className={`primary-workbench posture-workbench${previewId ? " motion-comparison" : ""}`} data-no-localize>
    <header className="primary-toolbar panel"><button onClick={() => setQueue(true)}>审核队列 · {total} 条</button><strong>{cohortBase ? "独立校准标注" : "动捕标注"}</strong>
      <span className="primary-task" title={current?.entry.source_member}>{current?.entry.source_dataset} · {current?.entry.source_member}</span>
      <button disabled={busy || (selectedIndex < 0 && navigationAnchor.current ? navigationAnchor.current.position <= 0 : offset+selectedIndex <= 0)} onClick={() => void neighbor(-1)}>上一条 <kbd>P</kbd></button>
      <button disabled={busy || (selectedIndex < 0 && navigationAnchor.current ? navigationAnchor.current.position >= total : offset+selectedIndex+1 >= total)} onClick={() => void neighbor(1)}>下一条 <kbd>N</kbd></button>
      <details className="primary-shortcuts"><summary>快捷键</summary><div><table><tbody>
        <tr><th><kbd>空格</kbd></th><td>播放／暂停</td></tr><tr><th><kbd>S</kbd></th><td>在播放位置切分</td></tr>
        <tr><th><kbd>M</kbd></th><td>合并下一段</td></tr><tr><th><kbd>P</kbd> / <kbd>N</kbd></th><td>上一条／下一条录制</td></tr>
        <tr><th><kbd>Ctrl/⌘ Enter</kbd></th><td>校验并通过整条录制</td></tr>
      </tbody></table><small>输入文字时停用单键操作；通过后保持当前位置。</small></div></details>
      {isAdmin && !previewId && !cohortBase && <button onClick={() => {pause();setModels(true);}}>模型与快照</button>}</header>
    {previewId && <div className="primary-notice motion-preview-banner" role="status">
      <strong>候选预览 · 只读</strong>
      <label>查看 <select aria-label="比较标注来源" value={comparison} disabled={!current?.preview || busy} onChange={e=>{
        const selected=e.target.value as typeof comparison; pause(); setComparison(selected);
        const doc=current?.preview?.documents[selected]; if(doc){setEditor(structuredClone(doc));setPart(Math.max(0,partAt(doc.segments,cursor)));}
      }}><option value="human">人工结果</option><option value="baseline">基线对照</option><option value="candidate">候选模型</option></select></label>
      <span>{comparison === "human" ? "冻结的人工审核" : current?.preview?.models[comparison]} · 规则 {current?.preview?.activity_rules_version}</span>
      <a href="/trial">查看本轮说明</a>
    </div>}
    {error && <div className="primary-notice primary-error" role="alert"><span>{error}</span>
      <button onClick={() => {const raw=current ? localStorage.getItem(cacheKey(current)) : null; const url=URL.createObjectURL(new Blob([raw ?? JSON.stringify(editor)],{type:"application/json"}));const a=document.createElement("a");a.href=url;a.download="motion-draft.json";a.click();URL.revokeObjectURL(url);}}>下载草稿</button>
      <button onClick={() => setError("")}>×</button></div>}
    {!current && <section className="panel">{busy ? "加载中…" : "暂没有已完成上传的动捕与 IMU；后台标注完成后即可检查。"}<button onClick={() => void fetchRows().then(r => r[0] && open(r[0].motion_sha256))}>刷新队列</button></section>}
    {current && editor && <div className="primary-body"><section className="primary-viewer panel">
      <iframe key={base} ref={iframe} src={`${base}/files/index.html?bridge=2`} title="动捕与IMU回放" onLoad={() => control("chart-ready",{ready:chartReady.current})}/>
      <SyntheticIMUChart compact base={base} cursorFrame={frameIndex} cursorTime={cursor/1e9} duration={duration/1e9}
        {...intervalDecorations(editor.segments, active)} onInspect={() => {}} onPlotBounds={b => setPlotBounds(old => old.left === b.left && old.right === b.right ? old : b)}
        onReady={ready => {chartReady.current=ready;control("chart-ready",{ready});}} onSeek={index => {follow.current=true;seek(index/rate*1e9);}}/>
      <div className="primary-time-context">{activeSegment && progress && <><strong>第 {active+1} 段 · {label(activeSegment)}</strong><span>{sec(activeSegment.start_ns)}–{sec(activeSegment.stop_ns)} 秒</span><span>段内 {sec(progress.elapsed)} / {sec(progress.duration)} 秒（{Math.round(progress.fraction*100)}%）</span></>}</div>
      <div className="primary-timeline" style={{marginLeft:plotBounds.left+7,marginRight:plotBounds.right+7}}>{editor.segments.map((s,i) => <button key={s.segment_id}
        style={{width:`${100*(s.stop_ns-s.start_ns)/duration}%`}} className={`${i%2 ? "alternate" : ""} ${!s.primary_code ? "unknown" : ""} ${i===active ? "playing" : ""}`}
        disabled={!viewerReady || busy} aria-current={i===active ? "step" : undefined} aria-label={`第 ${i+1} 段 ${label(s)}`} title={`${i+1}. ${label(s)} · ${sec(s.start_ns)}–${sec(s.stop_ns)} 秒`} onClick={() => selectPart(i)}>{i+1}</button>)}</div>
      <div className="primary-playback"><button disabled={!viewerReady || busy} onClick={toggle}>{!viewerReady ? "加载回放…" : playing ? "暂停" : "播放"}</button><input type="range" disabled={!viewerReady || busy} aria-label="播放位置" min={0} max={duration/1e9} step={1/rate} value={Math.min(cursor,duration)/1e9} onChange={e => {follow.current=true;seek(Number(e.target.value)*1e9);}}/><span>{sec(cursor)} / {sec(duration)} 秒</span></div>
    </section><aside className="primary-editor panel"><header><strong>{previewId ? "区间对照" : "检查与纠正"}</strong><span>{previewId ? "未启用候选模型" : (current.needs_rule_review ? "规则已更新，待复核" : statusNames[current.status])}</span>
      <small>类别覆盖 {sec(usable)} / {sec(duration)} 秒 · 待判区间保持监督屏蔽</small>
      {!previewId && current.variants.length > 1 && <select aria-label="IMU版本" disabled={busy} value={`${id?.candidate_id}/${id?.version_id}`} onChange={e => {const v=current.variants.find(v=>`${v.candidate.candidate_id}/${v.candidate.version_id}`===e.target.value);if(v)void open(current.motion_sha256,v.candidate);}}>
        {current.variants.map(v=><option key={v.candidate.version_id} value={`${v.candidate.candidate_id}/${v.candidate.version_id}`}>{v.configuration.layout_id} · {v.configuration.profile_id} · {v.configuration.variant}</option>)}</select>}
      {current.has_suggestion && <button onClick={() => {pause();setDifference(true);}}>查看自动标注建议</button>}
      {!!current.preview?.diagnostics?.length && <details key={casePath} className="motion-errors"><summary>候选短区间差异 · {current.preview.diagnostics.length} 处</summary><div>
        {current.preview.diagnostics.map((item,i)=><button key={i} disabled={!viewerReady || busy} onClick={()=>{follow.current=true;seek(item.start_s*1e9);}}>
          {item.start_s.toFixed(2)}–{item.stop_s.toFixed(2)} 秒 · {names[item.prediction ?? ""] ?? "无法识别"} / 人工：{item.reference_labels.map(code=>names[code]??code).join("、")}
        </button>)}
      </div></details>}</header>
      <div className="primary-editor-scroll"><div ref={list} className="primary-segments" aria-label="活动区间">{editor.segments.map((s,i)=><button key={s.segment_id} disabled={!viewerReady || busy} aria-current={i===active ? "step" : undefined} className={`${i===part ? "selected" : ""} ${i===active ? "playing" : ""}`} onClick={()=>selectPart(i)}><span>{i+1}. {label(s)}</span><small>{sec(s.start_ns)}–{sec(s.stop_ns)} s</small></button>)}</div>
      {segment && <fieldset className="primary-fields motion-fields" disabled={!lease || busy} onFocusCapture={e=>{if((e.target as HTMLElement).matches("input,textarea,select"))pause();}}>
        {issues.length > 0 && <div className="primary-validation" role="alert"><strong>请修改以下标注后再通过</strong>{issues.map((issue,i)=><button key={i} type="button" onClick={()=>{const at=editor.segments.findIndex(s=>s.segment_id===issue.segment_ids[0]);if(at>=0)selectPart(at);}}>{issue.message}</button>)}</div>}
        {current.legacy_quality && <details className="primary-definition"><summary>历史质量审核：{({pass:"通过",reject:"拒绝",unreviewed:"未审核"} as Record<string,string>)[current.legacy_quality.decision]??current.legacy_quality.decision}</summary><p>{current.legacy_quality.reason??"未填写原因"}</p><small>审核者 {current.legacy_quality.reviewer} · 区间请在此重新检查</small></details>}
        <label>主活动<select aria-label="主活动" value={segment.primary_code ?? segment.resolution} onChange={e=>{const code=e.target.value,known=editor.taxonomy.primary.find(t=>t.code===code);change({primary_code:known?code:null,resolution:known?"identified":code,reason:known?null:unknownNames[code],fall_event_id:null, transition:code==="posture_transition"?{from_posture:null,to_posture:null}:null});setIssues([]);}}>
          {editor.taxonomy.primary.map(t=><option key={t.code} value={t.code}>{names[t.code]}</option>)}{Object.entries(unknownNames).filter(([code])=>["3.0.0", "4.0.0"].includes(editor.taxonomy.version) ? code !== "uncovered" || segment.resolution === code : code !== "invalid_data").map(([code,name])=><option key={code} value={code}>{name}</option>)}</select></label>
        {quality && <small role="status">{quality.reason === "experimental_fall" ? "跌倒实验建议，尚未完成独立验证" : quality.high_confidence_fraction < 1 ? "低可信候选，请检查类别与切点" : quality.boundary_reliable ? "高可信候选" : "类别候选，边界待核"}</small>}
        {!previewId && <div className="primary-edit-buttons"><button onClick={split} disabled={!viewerReady}>在播放位置切分 <kbd>S</kbd></button>
          <button disabled={part===editor.segments.length-1 || !viewerReady} onClick={merge}>合并下一段 <kbd>M</kbd></button></div>}
        {isFall && <label>撞击时刻（秒，可留空）<input type="number" step="0.04" value={fall?.impact_ns != null ? fall.impact_ns/1e9 : ""} onChange={e=>{const event_id=segment.fall_event_id??`fall-${segment.segment_id}`;const events=editor.fall_events.filter(f=>f.event_id!==event_id);if(e.target.value!==""){const impact_ns=Math.round(Number(e.target.value)*25)*40000000;if(Number.isFinite(impact_ns))events.push({event_id,code:segment.primary_code!,start_ns:segment.start_ns,stop_ns:segment.stop_ns,impact_ns,evidence:"human replay inspection"});}setEditor({...editor,fall_events:events});}}/></label>}
        {!!(!previewId || segment.semantic_text || segment.notes?.length) && <IntervalSemantics key={`${casePath}:${segment.segment_id}`} segment={segment} onChange={change}/>}
      </fieldset>}{quality?.evidence?.length ? <MotionEvidence key={`${casePath}:${comparison}:${segment?.segment_id}`} items={quality.evidence}/> : null}</div><footer className="primary-actions" hidden={!!previewId}><div className="primary-save-status" role="status">{notice || (dirty ? "修改已在本机暂存，尚未通过" : "检查完毕后通过整条；提交后保持当前位置")}{duration > usable && <small> · {sec(duration-usable)} 秒待判，保持屏蔽</small>}</div>
        <div><button className="primary-save" disabled={busy||!lease} onClick={()=>void save("pass")}>通过整条录制 <kbd>Ctrl/⌘ Enter</kbd></button></div>
        <button disabled={busy||!lease} onClick={()=>{pause();setRejecting(true);}}>质量不合格</button>
        {!lease && <button disabled={busy} onClick={()=>void open(current.motion_sha256,id)}>重新领取编辑</button>}
      </footer></aside></div>}
    {queue && <div className="primary-overlay"><section role="dialog" aria-modal="true" aria-label="审核队列" className="primary-drawer panel"><header><strong>审核队列 · {total} 条</strong><button onClick={()=>setQueue(false)}>关闭</button></header>
      <form onSubmit={e=>{e.preventDefault();void fetchRows(0).catch(e=>setError(String(e)));}}><select aria-label="审核状态" disabled={!!previewId} value={filter} onChange={e=>setFilter(e.target.value)}><option value="all">全部</option>{Object.entries(statusNames).map(([v,n])=><option key={v} value={v}>{n}</option>)}</select><input aria-label="搜索录制" value={search} onChange={e=>setSearch(e.target.value)} placeholder="来源或文件名"/><button>筛选</button></form>
      <div className="primary-queue-list">{rows.map((r,i)=><button key={r.motion_sha256} disabled={busy} className={current?.motion_sha256===r.motion_sha256?"selected":""} onClick={()=>void open(r.motion_sha256)}><strong>{offset+i+1}. {r.entry.source_dataset}</strong><span>{statusNames[r.status]}</span><small>{r.entry.source_member}</small></button>)}</div>
      <footer><button disabled={offset===0} onClick={()=>void fetchRows(offset-30)}>上一页</button><button disabled={offset+rows.length>=total} onClick={()=>void fetchRows(offset+30)}>下一页</button></footer></section></div>}
    {rejecting && <div className="primary-overlay"><section role="dialog" aria-modal="true" aria-label="质量不合格" className="primary-drawer panel"><header><strong>质量不合格</strong><button onClick={()=>setRejecting(false)}>取消</button></header><label>问题范围<select value={rejectScope} onChange={e=>setRejectScope(e.target.value)}><option value="motion">动捕本身：影响所有 IMU 版本</option><option value="variant">当前 IMU 版本：其他版本保留</option></select></label><label>原因<textarea value={reason} onChange={e=>setReason(e.target.value)}/></label><button disabled={busy||!reason.trim()} onClick={()=>void save("reject")}>保存拒绝决定</button></section></div>}
    {difference && current?.suggestion && <div className="primary-overlay"><section role="dialog" aria-modal="true" aria-label="模型差异" className="primary-details panel"><header><strong>自动标注建议 · 当前版本保留</strong><button onClick={()=>setDifference(false)}>关闭</button></header><div className="primary-details-scroll"><table><thead><tr><th>当前标注</th><th>自动标注建议</th></tr></thead><tbody><tr>{[editor,current.suggestion].map((d,i)=><td key={i}>{d?.segments.map(s=><div key={s.segment_id}><p>{sec(s.start_ns)}–{sec(s.stop_ns)} s · {label(s)}</p>{i===1 && <MotionEvidence items={current.suggestion_quality?.intervals.find(q=>q.start_ns===s.start_ns && q.stop_ns===s.stop_ns && q.primary_code===s.primary_code)?.evidence}/>}</div>)}</td>)}</tr></tbody></table></div><button disabled={!lease||busy} onClick={()=>{const suggested=structuredClone(current.suggestion!);for(const s of suggested.segments){const notes=new Map<string,NonNullable<Segment["notes"]>[number]>();for(const old of editor!.segments)if(old.start_ns<s.stop_ns&&old.stop_ns>s.start_ns)for(const n of anchoredNotes(old))notes.set(n.note_id,n);s.notes=[...notes.values()];}setEditor(suggested);setPart(0);setDifference(false);setNotice("新建议已载入编辑区；检查后通过整条录制。");}}>载入建议到草稿，保留区间备注</button></section></div>}
    {models && isAdmin && <MotionModelPanel onClose={()=>setModels(false)}/>}
  </main>;
}
