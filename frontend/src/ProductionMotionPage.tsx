import {useEffect, useRef, useState} from "react";
import {SyntheticIMUChart} from "./SyntheticIMUChart";
import {intervalDecorations, partAt, partProgress, sourceFrame} from "./annotationPlayback";
import {anchoredNotes, joined, type SemanticFields} from "./semanticEditing";
import {MotionModelPanel} from "./MotionModelPanel";
import type {ActivityRules} from "./activityRules";
import "./temporalReview.css";

const root = "/api/v1/synthetic/motions";
type Segment = SemanticFields & {segment_id: string; start_ns: number; stop_ns: number; primary_code: string | null;
  auxiliary_code: null; auxiliary_status: string; resolution: string; reason: string | null; fall_event_id: string | null};
type Fall = {event_id: string; code: string; start_ns: number; stop_ns: number; impact_ns: number; evidence: string};
type Candidate = {candidate_id: string; version_id: string};
type Variant = {candidate: Candidate; configuration_id: string;
  configuration: {layout_id: string; profile_id: string; variant: string}};
type Annotation = {candidate: Candidate; clock: {duration_ns: number; source_rate_hz: number};
  segments: Segment[]; fall_events: Fall[]; taxonomy: {primary: {code: string; name: string; is_fall: boolean}[]}};
type Row = {motion_sha256: string; status: string;
  entry: {candidate: Candidate; source_dataset: string; source_member: string}};
type Detail = Row & {variants: Variant[]; editor_template: Annotation; suggestion: Annotation | null;
  legacy_quality: {decision:string;reason:string|null;reviewer:string} | null;
  has_suggestion: boolean; review: {revision_sha256: string} | null; draft: {revision_sha256: string} | null};
type Lease = {token: string; expires_at: number};
const statusNames: Record<string,string> = {inference_pending: "等待自动标注", pending_review: "待检查", accepted: "已通过", rejected: "已拒绝"};
const unknownNames: Record<string,string> = {unrecognized: "无法识别", uncovered: "词表未覆盖", uncertain_boundary: "边界不确定"};
const sec = (ns: number) => (ns/1e9).toFixed(2);

export async function motionApi<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(path, body === undefined ? {} : {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  const value = await response.json();
  if (!response.ok) throw new Error(typeof value.detail === "string" ? value.detail : value.detail?.message ?? `HTTP ${response.status}`);
  return value;
}

export function ProductionMotionPage({isAdmin}: {isAdmin: boolean}) {
  const [rows, setRows] = useState<Row[]>([]), [total, setTotal] = useState(0), [offset, setOffset] = useState(0);
  const [filter, setFilter] = useState("all"), [search, setSearch] = useState("");
  const [current, setCurrent] = useState<Detail | null>(null), [editor, setEditor] = useState<Annotation | null>(null);
  const [lease, setLease] = useState<Lease | null>(null), [part, setPart] = useState(0), [queue, setQueue] = useState(false);
  const [busy, setBusy] = useState(false), [error, setError] = useState(""), [notice, setNotice] = useState("");
  const [models, setModels] = useState(false), [difference, setDifference] = useState(false), [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState(""), [rejectScope, setRejectScope] = useState("motion");
  const [frameIndex, setFrameIndex] = useState(0), [playing, setPlaying] = useState(false);
  const [plotBounds, setPlotBounds] = useState({left: 50, right: 50});
  const [rules, setRules] = useState<ActivityRules | null>(null);
  const iframe = useRef<HTMLIFrameElement>(null), list = useRef<HTMLDivElement>(null);
  const follow = useRef(true), chartReady = useRef(false), loading = useRef(0);
  const session = useRef<{mid: string; token: string} | null>(null);
  const editorRef = useRef(editor); editorRef.current = editor;
  const currentRef = useRef(current); currentRef.current = current;
  const id = current?.entry.candidate;
  const base = id ? `/api/v1/synthetic/candidates/${id.candidate_id}/${id.version_id}` : "";
  const casePath = current && id ? `${root}/${current.motion_sha256}/variants/${id.candidate_id}/${id.version_id}` : "";
  const cacheKey = (d: Detail) => `motion-draft:${d.motion_sha256}:${d.entry.candidate.candidate_id}:${d.entry.candidate.version_id}`;
  const rate = editor?.clock.source_rate_hz ?? 25, cursor = Math.round(frameIndex/rate*1e9);
  const duration = editor?.clock.duration_ns ?? 0, active = partAt(editor?.segments ?? [], Math.min(cursor, duration));
  const activeSegment = editor?.segments[active], segment = editor?.segments[part];
  const progress = activeSegment ? partProgress(activeSegment, cursor) : null;
  const names = Object.fromEntries(editor?.taxonomy.primary.map(t => [t.code, rules?.primary.find(r=>r.code===t.code)?.name_zh ?? t.name]) ?? []);
  const definition = rules?.primary.find(r=>r.code===segment?.primary_code);
  const label = (s: Segment) => s.primary_code ? names[s.primary_code] ?? s.primary_code : unknownNames[s.resolution] ?? "无法识别";
  const usable = editor?.segments.reduce((n, s) => {
    const fall = editor.taxonomy.primary.find(t=>t.code===s.primary_code)?.is_fall;
    const event = editor.fall_events.find(e=>e.event_id===s.fall_event_id||e.event_id===`fall-${s.segment_id}`);
    const eligible = s.primary_code && (!fall || (event && event.impact_ns>=event.start_ns && event.impact_ns<event.stop_ns));
    return n+(eligible ? s.stop_ns-s.start_ns : 0);
  }, 0) ?? 0;
  const dirty = !!editor && JSON.stringify(editor) !== JSON.stringify(current?.editor_template);
  const control = (action: string, fields = {}) => iframe.current?.contentWindow?.postMessage({type: "imu-synthetic-review-control", action, ...fields}, location.origin);
  const pause = () => {follow.current = false; setPlaying(false); control("pause");};
  const seek = (ns: number, boundary = false) => {setPlaying(false); control("seek", {frame: sourceFrame(ns, rate, boundary)});};
  const selectPart = (index: number) => {pause(); setPart(index); seek(editor!.segments[index].start_ns, true);};
  const toggle = () => {follow.current = true; control("toggle");};
  const persist = () => {
    const d = currentRef.current, e = editorRef.current;
    if (d && e && JSON.stringify(e) !== JSON.stringify(d.editor_template)) {
      localStorage.setItem(cacheKey(d), JSON.stringify({document: e, draft_sha256: d.draft?.revision_sha256 ?? null,
        review_sha256: d.review?.revision_sha256 ?? null}));
    }
  };
  const fetchRows = async (nextOffset = offset) => {
    const data = await motionApi<{cases: Row[]; total: number}>(`${root}?limit=30&offset=${nextOffset}&status=${filter}&search=${encodeURIComponent(search)}`);
    setRows(data.cases); setTotal(data.total); setOffset(nextOffset);
    return data.cases;
  };
  const open = async (mid: string, variant?: Candidate) => {
    const sequence = ++loading.current;
    setBusy(true); setError(""); setNotice(""); setLease(null);
    pause();
    try {
      persist();
      if (session.current && session.current.mid !== mid) {
        await motionApi(`${root}/${session.current.mid}/release`, {token: session.current.token});
        session.current = null;
      }
      const savedToken = session.current?.mid === mid ? session.current.token : sessionStorage.getItem(`motion-lease:${mid}`);
      let claimed: Lease | null = null;
      try {
        claimed = await motionApi<Lease>(`${root}/${mid}/claim`, {token: savedToken ?? null});
        session.current = {mid, token: claimed.token}; sessionStorage.setItem(`motion-lease:${mid}`, claimed.token);
      } catch (e) {setError(`${String(e)}；当前以只读方式查看。`);}
      const suffix = variant ? `/variants/${variant.candidate_id}/${variant.version_id}` : "";
      const data = await motionApi<Detail>(`${root}/${mid}${suffix}`);
      if (sequence !== loading.current) return;
      let value = structuredClone(data.editor_template);
      const local = localStorage.getItem(cacheKey(data));
      if (local) {
        const cached = JSON.parse(local);
        if (cached.draft_sha256 === (data.draft?.revision_sha256 ?? null) && cached.review_sha256 === (data.review?.revision_sha256 ?? null)) {
          value = cached.document; setNotice("已恢复本地未提交草稿。");
        } else setError("存在基于旧审核版本的本地草稿，未覆盖服务器结果；可下载本地草稿进行核对。");
      }
      setCurrent(data); setEditor(value); setLease(claimed); setPart(0); setFrameIndex(0); setPlaying(false);
      if (data.entry.candidate.candidate_id === currentRef.current?.entry.candidate.candidate_id &&
          data.entry.candidate.version_id === currentRef.current?.entry.candidate.version_id) control("seek", {frame: 0});
      chartReady.current = false; follow.current = true; setQueue(false); setDifference(false);
    } catch (e) {setError(String(e));} finally {if (sequence === loading.current) setBusy(false);}
  };
  useEffect(() => {void fetchRows(0).then(async items => {
    const query = new URLSearchParams(location.search), candidate_id = query.get("candidate"), version_id = query.get("version");
    if (candidate_id && version_id) {
      const found = await motionApi<{motion_sha256:string}>(`${root}/lookup/${encodeURIComponent(candidate_id)}/${encodeURIComponent(version_id)}`);
      await open(found.motion_sha256, {candidate_id,version_id});
    } else if (items[0]) await open(items[0].motion_sha256);
  }).catch(e => setError(String(e)));}, []);
  useEffect(() => {void motionApi<{temporal?:ActivityRules}>("/api/v1/synthetic/labels").then(v=>setRules(v.temporal??null)).catch(e=>setError(String(e)));}, []);
  useEffect(() => {
    const timer = setInterval(() => {
      const s = session.current;
      if (s) void motionApi<Lease>(`${root}/${s.mid}/claim`, {token: s.token}).then(setLease).catch(e => {setLease(null); setError(String(e));});
    }, 60000);
    return () => {clearInterval(timer); const s = session.current; if (s) void motionApi(`${root}/${s.mid}/release`, {token: s.token});};
  }, []);
  useEffect(() => {try {persist();} catch {setError("本地草稿存储失败，请保持页面并保存草稿。");}}, [editor]);
  useEffect(() => {
    const listener = (event: MessageEvent) => {
      if (event.origin !== location.origin || event.source !== iframe.current?.contentWindow) return;
      if (["imu-synthetic-review-frame", "imu-synthetic-review-ready"].includes(event.data?.type) && Number.isInteger(event.data.frame)) setFrameIndex(event.data.frame);
      if (typeof event.data?.playing === "boolean") setPlaying(event.data.playing);
      if (event.data?.type === "imu-synthetic-review-ready") control("chart-ready", {ready: chartReady.current});
      if (event.data?.type === "imu-synthetic-review-key" && event.data.key === " ") toggle();
    };
    const keyboard = (event: KeyboardEvent) => {
      if (event.code === "Space" && !(event.target as HTMLElement)?.closest("input,textarea,select,button,[contenteditable=true]") && !queue && !models && !difference && !rejecting) {event.preventDefault(); toggle();}
    };
    addEventListener("message", listener); addEventListener("keydown", keyboard);
    return () => {removeEventListener("message", listener); removeEventListener("keydown", keyboard);};
  }, [base, queue, models, difference, rejecting]);
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
  const boundary = (side: "start_ns" | "stop_ns", ns: number) => {
    if (!editor || !Number.isFinite(ns)) return;
    const point = Math.round(ns/40000000)*40000000, i = side === "start_ns" ? part-1 : part;
    if (i < 0 || i >= editor.segments.length-1 || point <= editor.segments[i].start_ns || point >= editor.segments[i+1].stop_ns) return;
    setEditor({...editor, segments: editor.segments.map((s,n) => n === i ? {...s,stop_ns:point} : n === i+1 ? {...s,start_ns:point} : s)});
  };
  const finalized = () => {
    if (!editor) throw new Error("没有可保存的区间");
    const doc = structuredClone(editor), events = new Map<string,Fall>();
    for (const s of doc.segments) {
      const fall = doc.taxonomy.primary.find(t => t.code === s.primary_code)?.is_fall;
      const event = doc.fall_events.find(e => e.event_id === s.fall_event_id || e.event_id === `fall-${s.segment_id}`);
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
    if (!current || !lease) return;
    setBusy(true); setError(""); pause();
    try {
      const priorStart = segment?.start_ns ?? 0;
      const result = await motionApi<Detail>(`${casePath}/${decision ? "review" : "draft"}`, {
        document: finalized(), lease_token: lease.token, expected_draft_sha256: current.draft?.revision_sha256 ?? null,
        expected_review_sha256: current.review?.revision_sha256 ?? null, decision, scope: rejectScope, reason});
      localStorage.removeItem(cacheKey(current)); setCurrent(result); setEditor(structuredClone(result.editor_template));
      setPart(Math.max(0,partAt(result.editor_template.segments,priorStart))); setRejecting(false);
      setNotice(decision === "pass" ? "整条录制已通过；未知区间继续屏蔽。" : decision === "reject" ? "质量决定已保存。" : "草稿已保存，正式审核版本未改变。");
      await fetchRows();
    } catch (e) {setError(String(e));} finally {setBusy(false);}
  };
  const neighbor = async (delta: number) => {
    const index = rows.findIndex(r => r.motion_sha256 === current?.motion_sha256), next = index+delta;
    if (rows[next]) {await open(rows[next].motion_sha256); return;}
    const target = offset+delta*30;
    if (target < 0 || target >= total) return;
    const items = await fetchRows(target);
    const item = delta > 0 ? items[0] : items.at(-1);
    if (item) await open(item.motion_sha256);
  };
  const selectedIndex = rows.findIndex(r => r.motion_sha256 === current?.motion_sha256);
  const isFall = editor?.taxonomy.primary.find(t => t.code === segment?.primary_code)?.is_fall;
  const fall = editor?.fall_events.find(e => e.event_id === segment?.fall_event_id || e.event_id === `fall-${segment?.segment_id}`);
  return <main className="primary-workbench" data-no-localize>
    <header className="primary-toolbar panel"><button onClick={() => setQueue(true)}>审核队列 · {total} 条</button><strong>动捕标注</strong>
      <span className="primary-task" title={current?.entry.source_member}>{current?.entry.source_dataset} · {current?.entry.source_member}</span>
      <button disabled={busy || offset+selectedIndex <= 0} onClick={() => void neighbor(-1)}>上一条</button>
      <button disabled={busy || offset+selectedIndex+1 >= total} onClick={() => void neighbor(1)}>下一条</button>
      {isAdmin && <button onClick={() => {pause();setModels(true);}}>模型与快照</button>}</header>
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
        aria-current={i===active ? "step" : undefined} aria-label={`第 ${i+1} 段 ${label(s)}`} title={`${i+1}. ${label(s)} · ${sec(s.start_ns)}–${sec(s.stop_ns)} 秒`} onClick={() => selectPart(i)}>{i+1}</button>)}</div>
      <div className="primary-playback"><button onClick={toggle}>{playing ? "暂停" : "播放"}</button><input type="range" aria-label="播放位置" min={0} max={duration/1e9} step={1/rate} value={Math.min(cursor,duration)/1e9} onChange={e => {follow.current=true;seek(Number(e.target.value)*1e9);}}/><span>{sec(cursor)} / {sec(duration)} 秒</span></div>
    </section><aside className="primary-editor panel"><header><strong>检查与纠正</strong><span>{statusNames[current.status]}</span>
      <small>可用标签 {sec(usable)} / {sec(duration)} 秒 · 未知区间不用于监督训练</small>
      {current.variants.length > 1 && <select aria-label="IMU版本" disabled={busy} value={`${id?.candidate_id}/${id?.version_id}`} onChange={e => {const v=current.variants.find(v=>`${v.candidate.candidate_id}/${v.candidate.version_id}`===e.target.value);if(v)void open(current.motion_sha256,v.candidate);}}>
        {current.variants.map(v=><option key={v.candidate.version_id} value={`${v.candidate.candidate_id}/${v.candidate.version_id}`}>{v.configuration.layout_id} · {v.configuration.profile_id} · {v.configuration.variant}</option>)}</select>}
      {current.has_suggestion && <button onClick={() => {pause();setDifference(true);}}>查看新模型建议</button>}</header>
      <div className="primary-editor-scroll">{current.legacy_quality && <details className="primary-definition"><summary>历史质量审核：{({pass:"通过",reject:"拒绝",unreviewed:"未审核"} as Record<string,string>)[current.legacy_quality.decision]??current.legacy_quality.decision}</summary><p>{current.legacy_quality.reason??"未填写原因"}</p><small>审核者 {current.legacy_quality.reviewer} · 区间请在此重新检查</small></details>}<div ref={list} className="primary-segments" aria-label="活动区间">{editor.segments.map((s,i)=><button key={s.segment_id} aria-current={i===active ? "step" : undefined} className={`${i===part ? "selected" : ""} ${i===active ? "playing" : ""}`} onClick={()=>selectPart(i)}><span>{i+1}. {label(s)}</span><small>{sec(s.start_ns)}–{sec(s.stop_ns)} s</small></button>)}</div>
      {segment && <fieldset className="primary-fields motion-fields" disabled={!lease || busy} onFocusCapture={e=>{if((e.target as HTMLElement).matches("input,textarea,select"))pause();}}>
        <label>主活动<select aria-label="主活动" value={segment.primary_code ?? segment.resolution} onChange={e=>{const code=e.target.value,known=editor.taxonomy.primary.find(t=>t.code===code);change({primary_code:known?code:null,resolution:known?"identified":code,reason:known?null:unknownNames[code],fall_event_id:null});}}>
          {editor.taxonomy.primary.map(t=><option key={t.code} value={t.code}>{names[t.code]}</option>)}{Object.entries(unknownNames).map(([code,name])=><option key={code} value={code}>{name}</option>)}</select></label>
        {definition && <details className="primary-definition"><summary>类别定义与切分说明</summary><p>{definition.definition}</p><p>包括：{definition.includes}</p><p>不包括：{definition.excludes}</p><p>切分：{definition.boundaries}</p><p>{definition.example}</p></details>}
        <div className="primary-boundaries">{(["start_ns","stop_ns"] as const).map((side,i)=><label key={side}>{i ? "结束（秒）" : "开始（秒）"}<input type="number" aria-label={i ? "结束秒" : "开始秒"} step="0.04" value={segment[side]/1e9} disabled={i ? part===editor.segments.length-1 : part===0} onChange={e=>boundary(side,Number(e.target.value)*1e9)}/></label>)}</div>
        <div className="primary-edit-buttons"><button onClick={()=>{const point=Math.round(cursor/40000000)*40000000;if(point<=segment.start_ns||point>=segment.stop_ns){setError("请先将播放位置移到所选区间内部。");return;}setEditor({...editor,segments:editor.segments.flatMap((s,i)=>i===part?[{...s,notes:anchoredNotes(s),stop_ns:point},{...s,notes:anchoredNotes(s),segment_id:`segment-${crypto.randomUUID()}`,start_ns:point}]:[s])});}}>在播放位置切分</button>
          <button disabled={part===editor.segments.length-1} onClick={()=>{const next=editor.segments[part+1];if(segment.fall_event_id&&next.fall_event_id&&segment.fall_event_id!==next.fall_event_id){setError("不能合并两次独立跌倒。");return;}setEditor({...editor,segments:editor.segments.flatMap((s,i)=>i===part?[joined(s,next)]:i===part+1?[]:[s])});}}>合并下一段</button></div>
        {isFall && <label>撞击时刻（秒；缺失时该区间屏蔽）<input type="number" step="0.04" value={fall ? fall.impact_ns/1e9 : ""} onChange={e=>{const event_id=segment.fall_event_id??`fall-${segment.segment_id}`;setEditor({...editor,fall_events:[...editor.fall_events.filter(f=>f.event_id!==event_id),{event_id,code:segment.primary_code!,start_ns:segment.start_ns,stop_ns:segment.stop_ns,impact_ns:Math.round(Number(e.target.value)*1e9),evidence:"human replay inspection"}]});}}/></label>}
        <label>区间语义<textarea aria-label="区间语义" rows={2} value={segment.semantic_text ?? ""} onChange={e=>change({semantic_text:e.target.value})} placeholder="可自由描述动作，无需与列表文字一致"/></label>
        {segment.notes?.map((n,i)=><label key={n.note_id}>区间备注 · {sec(n.start_ns)}–{sec(n.stop_ns)} s<textarea aria-label={`区间备注 ${i+1}`} rows={2} value={n.text} onChange={e=>change({notes:segment.notes!.map((v,j)=>i===j?{...v,text:e.target.value}:v)})}/></label>)}
        <button onClick={()=>change({notes:[...(segment.notes??[]),{note_id:`note-${crypto.randomUUID()}`,start_ns:segment.start_ns,stop_ns:segment.stop_ns,text:""}]})}>添加区间备注</button>
      </fieldset>}</div><footer className="primary-actions"><div className="primary-save-status" role="status">{notice || (dirty ? "有未保存修改" : "检查完毕后通过整条；提交后保持当前位置")}</div>
        <div><button disabled={busy||!lease} onClick={()=>void save()}>保存草稿</button><button className="primary-save" disabled={busy||!lease} onClick={()=>void save("pass")}>通过整条录制</button></div>
        <button disabled={busy||!lease} onClick={()=>{pause();setRejecting(true);}}>质量不合格</button>
        {!lease && <button disabled={busy} onClick={()=>void open(current.motion_sha256,id)}>重新领取编辑</button>}
      </footer></aside></div>}
    {queue && <div className="primary-overlay"><section role="dialog" aria-modal="true" aria-label="审核队列" className="primary-drawer panel"><header><strong>审核队列 · {total} 条</strong><button onClick={()=>setQueue(false)}>关闭</button></header>
      <form onSubmit={e=>{e.preventDefault();void fetchRows(0).catch(e=>setError(String(e)));}}><select aria-label="审核状态" value={filter} onChange={e=>setFilter(e.target.value)}><option value="all">全部</option>{Object.entries(statusNames).map(([v,n])=><option key={v} value={v}>{n}</option>)}</select><input aria-label="搜索录制" value={search} onChange={e=>setSearch(e.target.value)} placeholder="来源或文件名"/><button>筛选</button></form>
      <div className="primary-queue-list">{rows.map((r,i)=><button key={r.motion_sha256} disabled={busy} className={current?.motion_sha256===r.motion_sha256?"selected":""} onClick={()=>void open(r.motion_sha256)}><strong>{offset+i+1}. {r.entry.source_dataset}</strong><span>{statusNames[r.status]}</span><small>{r.entry.source_member}</small></button>)}</div>
      <footer><button disabled={offset===0} onClick={()=>void fetchRows(offset-30)}>上一页</button><button disabled={offset+rows.length>=total} onClick={()=>void fetchRows(offset+30)}>下一页</button></footer></section></div>}
    {rejecting && <div className="primary-overlay"><section role="dialog" aria-modal="true" aria-label="质量不合格" className="primary-drawer panel"><header><strong>质量不合格</strong><button onClick={()=>setRejecting(false)}>取消</button></header><label>问题范围<select value={rejectScope} onChange={e=>setRejectScope(e.target.value)}><option value="motion">动捕本身：影响所有 IMU 版本</option><option value="variant">当前 IMU 版本：其他版本保留</option></select></label><label>原因<textarea value={reason} onChange={e=>setReason(e.target.value)}/></label><button disabled={busy||!reason.trim()} onClick={()=>void save("reject")}>保存拒绝决定</button></section></div>}
    {difference && current?.suggestion && <div className="primary-overlay"><section role="dialog" aria-modal="true" aria-label="模型差异" className="primary-details panel"><header><strong>新模型建议 · 原人工版本仍有效</strong><button onClick={()=>setDifference(false)}>关闭</button></header><div className="primary-details-scroll"><table><thead><tr><th>当前标注</th><th>新模型建议</th></tr></thead><tbody><tr>{[editor,current.suggestion].map((d,i)=><td key={i}>{d?.segments.map(s=><p key={s.segment_id}>{sec(s.start_ns)}–{sec(s.stop_ns)} s · {label(s)}</p>)}</td>)}</tr></tbody></table></div><button disabled={!lease||busy} onClick={()=>{const suggested=structuredClone(current.suggestion!);for(const s of suggested.segments){const notes=new Map<string,NonNullable<Segment["notes"]>[number]>();for(const old of editor!.segments)if(old.start_ns<s.stop_ns&&old.stop_ns>s.start_ns)for(const n of anchoredNotes(old))notes.set(n.note_id,n);s.notes=[...notes.values()];}setEditor(suggested);setPart(0);setDifference(false);setNotice("新建议已载入编辑区；通过整条后才替换正式人工版本。");}}>载入建议到草稿，保留区间备注</button></section></div>}
    {models && isAdmin && <MotionModelPanel onClose={()=>setModels(false)}/>}
  </main>;
}
