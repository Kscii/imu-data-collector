import { useEffect, useMemo, useRef, useState } from "react";
import { tr } from "./i18n";

export type FormalLabel = {
  code: string; name: string; is_fall: boolean;
  origin?: string; verification?: string; mapping_rule_id?: string | null;
};
export type Candidate = {
  candidate_id: string; version_id: string; source_dataset: string;
  lease_token?: string;
  source_member: string; published_at_utc: string;
  label_candidates: {code?: string | null; raw_label?: string | null;
    categories?: string[] | null; kind?: string}[];
  warning_flags: string[]; risk_tier?: string; label: FormalLabel | null;
  decision: "unreviewed" | "pass" | "reject"; revision: number;
};
export type Catalog = {
  taxonomy_id: string; version: string;
  concepts: (FormalLabel & {active: boolean; scope: string})[];
  rules: {rule_id: string; origin: string; source_value: string;
    source_dataset: string | null; target_code: string; state: string}[];
};
export const syntheticRoot = "/api/v1/synthetic";
export const candidateKey = (item: Candidate) => `${item.candidate_id}/${item.version_id}`;
type Summary = {published: number; unreviewed: number; passed: number;
  rejected: number; label_pending: number; latest_published_at_utc: string | null;
  read_only?: boolean; preview_mode?: string | null};

export async function syntheticRequest<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    ...options, headers: {"Content-Type": "application/json", ...(options?.headers ?? {})},
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(
    typeof payload.detail === "string" ? payload.detail : `HTTP ${response.status}`);
  return payload as T;
}

const editable = (target: EventTarget | null) =>
  Boolean((target as HTMLElement | null)?.closest("input,textarea,select,[contenteditable='true']"));

export function SyntheticMotionPage() {
  const [view, setView] = useState<"quality" | "labels">("quality");
  const [items, setItems] = useState<Candidate[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [selected, setSelected] = useState("");
  const [filter, setFilter] = useState("unreviewed");
  const [search, setSearch] = useState("");
  const [highRisk, setHighRisk] = useState(false);
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [labelRevision, setLabelRevision] = useState(0);
  const [chosenCode, setChosenCode] = useState("");
  const [reasons, setReasons] = useState<{code: string; name: string}[]>([]);
  const [reasonCodes, setReasonCodes] = useState<string[]>([]);
  const [reasonNote, setReasonNote] = useState("");
  const [rejectOpen, setRejectOpen] = useState(false);
  const [working, setWorking] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const frame = useRef<HTMLIFrameElement>(null);
  const leases = useRef<string[]>([]);
  const loadGeneration = useRef(0);

  const queueMode = view === "quality" && filter === "unreviewed";
  const claim = async (generation = loadGeneration.current) => {
    const params = new URLSearchParams({search, high_risk: String(highRisk)});
    const value = await syntheticRequest<{candidates: Candidate[]}>(
      `${syntheticRoot}/queue/claim?${params}`, {method: "POST"});
    // A later load may have resumed the same leases. Let stale responses expire
    // instead of releasing tokens that the active view could now own.
    if (generation !== loadGeneration.current) return;
    leases.current.push(...value.candidates.flatMap(item => item.lease_token ? [item.lease_token] : []));
    setItems(value.candidates); setHasMore(false);
    setSelected(value.candidates[0] ? candidateKey(value.candidates[0]) : "");
  };
  const browse = async (offset = 0, generation = loadGeneration.current) => {
    const params = new URLSearchParams({
      decision: view === "labels" ? "pass" : filter,
      search, high_risk: String(highRisk), limit: "100", offset: String(offset),
    });
    if (view === "labels" && filter !== "all")
      params.set("label_state", filter === "unreviewed" ? "pending" : "labeled");
    const value = await syntheticRequest<{candidates: Candidate[]}>(
      `${syntheticRoot}/candidates?${params}`);
    if (generation !== loadGeneration.current) return;
    setItems(previous => offset ? [...previous, ...value.candidates] : value.candidates);
    setHasMore(value.candidates.length === 100);
    if (!offset) setSelected(value.candidates[0] ? candidateKey(value.candidates[0]) : "");
  };
  useEffect(() => {
    syntheticRequest<Catalog>(`${syntheticRoot}/labels`).then(setCatalog).catch(error => setError(String(error)));
    syntheticRequest<{reasons: {code: string; name: string}[]}>(`${syntheticRoot}/rejection-reasons`)
      .then(value => setReasons(value.reasons)).catch(error => setError(String(error)));
    const updateSummary = () => syntheticRequest<Summary>(`${syntheticRoot}/summary`)
      .then(setSummary).catch(error => setError(String(error)));
    void updateSummary();
    const timer = window.setInterval(updateSummary, 20000);
    return () => window.clearInterval(timer);
  }, []);
  useEffect(() => {
    const generation = ++loadGeneration.current;
    setItems([]); setSelected(""); setError("");
    const previous = leases.current.splice(0);
    const load = async () => {
      await Promise.allSettled(previous.map(lease_token => syntheticRequest(
        `${syntheticRoot}/queue/release`, {method: "POST",
          body: JSON.stringify({lease_token})})));
      if (generation !== loadGeneration.current) return;
      await (queueMode ? claim(generation) : browse(0, generation));
    };
    load().catch(error => { if (generation === loadGeneration.current) setError(String(error)); });
    return () => { loadGeneration.current++; };
  }, [view, filter, search, highRisk]);
  useEffect(() => {
    return () => {
      const tokens = leases.current.splice(0);
      for (const lease_token of tokens) void syntheticRequest(
        `${syntheticRoot}/queue/release`, {method: "POST", keepalive: true,
          body: JSON.stringify({lease_token})}).catch(() => undefined);
    };
  }, []);
  useEffect(() => {
    if (!queueMode || summary?.read_only) return;
    const timer = window.setInterval(() => {
      for (const lease_token of leases.current) void syntheticRequest(
        `${syntheticRoot}/queue/renew`, {method: "POST",
          body: JSON.stringify({lease_token})}).catch(error => setError(String(error)));
    }, 300000);
    return () => window.clearInterval(timer);
  }, [queueMode]);
  const visible = useMemo(() => items.filter(item => {
    const status = view === "quality"
      ? filter === "all" || item.decision === filter
      : item.decision === "pass" && (filter === "all"
        || (filter === "unreviewed" ? !item.label : Boolean(item.label)));
    return status && (!highRisk || item.risk_tier === "high")
      && (!search || `${item.source_dataset} ${item.source_member} ${item.candidate_id}`
        .toLowerCase().includes(search.toLowerCase()));
  }), [items, view, filter, highRisk, search]);
  const current = visible.find(item => candidateKey(item) === selected) ?? visible[0];
  const base = current ? `${syntheticRoot}/candidates/${encodeURIComponent(current.candidate_id)}/${current.version_id}` : "";

  useEffect(() => {
    setLabelRevision(0); setChosenCode(current?.label?.code ?? "");
    setReasonCodes([]); setReasonNote(""); setRejectOpen(false);
    if (current) syntheticRequest<{label_revision: {revision: number} | null}>(base)
      .then(value => setLabelRevision(value.label_revision?.revision ?? 0))
      .catch(error => setError(String(error)));
  }, [base]);
  const afterCurrent = async (item: Candidate, skip: boolean) => {
    if (queueMode && item.lease_token) {
      if (skip) await syntheticRequest(`${syntheticRoot}/queue/release`, {
        method: "POST", body: JSON.stringify({lease_token: item.lease_token, skip: true})});
      leases.current = leases.current.filter(token => token !== item.lease_token);
      const remaining = items.filter(row => candidateKey(row) !== candidateKey(item));
      setItems(remaining);
      if (remaining.length) setSelected(candidateKey(remaining[0]));
      else await claim();
      return;
    }
    const index = visible.findIndex(row => candidateKey(row) === candidateKey(item));
    const next = visible[index + 1];
    setSelected(next ? candidateKey(next) : "");
  };
  const decide = async (decision: "pass" | "reject") => {
    if (!current || working || summary?.read_only || (decision === "reject"
        && reasonCodes.length === 0 && !reasonNote.trim())) return;
    setWorking(true); setError("");
    try {
      await syntheticRequest(`${base}/reviews`, {method: "POST", body: JSON.stringify({
        decision, expected_revision: current.revision, labels: [],
        lease_token: current.lease_token,
        reason_codes: decision === "reject" ? reasonCodes : [],
        reason: decision === "reject" ? reasonNote.trim() || null : null,
      })});
      setRejectOpen(false);
      setMessage(decision === "pass" ? tr("质量已通过", "Quality passed") : tr("已拒绝", "Rejected"));
      await afterCurrent(current, false);
      syntheticRequest<Summary>(`${syntheticRoot}/summary`).then(setSummary).catch(() => undefined);
    } catch (error) { setError(String(error)); }
    finally { setWorking(false); }
  };
  const saveLabel = async () => {
    if (!current || !chosenCode || working || summary?.read_only) return;
    setWorking(true); setError("");
    try {
      await syntheticRequest(`${base}/labels`, {method: "POST",
        body: JSON.stringify({code: chosenCode, expected_revision: labelRevision})});
      setMessage(tr("正式标签已保存", "Formal label saved"));
      const next = visible.find(item => candidateKey(item) !== candidateKey(current) && !item.label);
      await browse();
      if (next) setSelected(candidateKey(next));
    } catch (error) { setError(String(error)); }
    finally { setWorking(false); }
  };
  const control = (action: "toggle" | "replay") =>
    frame.current?.contentWindow?.postMessage(
      {type: "imu-synthetic-review-control", action}, location.origin);
  const shortcut = (key: string) => {
    if (working) return;
    if (rejectOpen) {
      if (key === "Escape") setRejectOpen(false);
      if (key === "Enter") void decide("reject");
      return;
    }
    if (key === " ") control("toggle");
    if (key.toLowerCase() === "r") control("replay");
    if (key.toLowerCase() === "n" && current) void afterCurrent(current, queueMode)
      .catch(error => setError(String(error)));
    if (view !== "quality") return;
    if (!queueMode) return;
    if (key.toLowerCase() === "p") void decide("pass");
    if (key.toLowerCase() === "x") setRejectOpen(true);
    if (key.toLowerCase() === "s") {
      if (current) void afterCurrent(current, true).catch(error => setError(String(error)));
      setMessage(tr("已跳过，仍为未审核", "Skipped; still unreviewed"));
    }
  };
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.altKey || event.ctrlKey || event.metaKey || editable(event.target)) return;
      if ([" ", "r", "R", "n", "N", "p", "P", "x", "X", "s", "S"].includes(event.key)
          || (rejectOpen && ["Enter", "Escape"].includes(event.key))) {
        event.preventDefault(); shortcut(event.key);
      }
    };
    const onMessage = (event: MessageEvent) => {
      if (event.origin !== location.origin || event.source !== frame.current?.contentWindow
          || event.data?.type !== "imu-synthetic-review-key"
          || typeof event.data.key !== "string") return;
      shortcut(event.data.key);
    };
    window.addEventListener("keydown", onKey); window.addEventListener("message", onMessage);
    return () => { window.removeEventListener("keydown", onKey); window.removeEventListener("message", onMessage); };
  });

  return <main className="synthetic-workbench">
    <section className="panel synthetic-heading">
      <div><h2>{tr("合成运动", "Synthetic motion")}</h2>
        <p>{tr("先判断动作和 IMU 质量，正式标签在独立流程处理。", "Review motion and IMU quality first; formal labels are handled separately.")}</p>
        {summary && <p>{tr("已发布", "Published")} {summary.published}
          {" · "}{tr("待审核", "Awaiting review")} {summary.unreviewed}
          {" · "}{tr("待标签", "Needs label")} {summary.label_pending}
          {summary.latest_published_at_utc && <> · {tr("最近到达", "Latest arrival")}
            {" "}{new Date(summary.latest_published_at_utc).toLocaleString()}</>}</p>}</div>
      {summary?.read_only && <span className="warning-banner">{tr("只读预览：审核决定不会保存", "Read-only preview: decisions are disabled")}</span>}
      {summary?.preview_mode && <span className="warning-banner">{tr("本地试用：审核决定只保存在本机，不写入云端", "Local trial: decisions stay on this machine")}</span>}
      <nav className="synthetic-tabs">
        <button className={view === "quality" ? "active" : ""} onClick={() => { setView("quality"); setFilter("unreviewed"); setSelected(""); }}>{tr("质量审核", "Quality review")}</button>
        <button className={view === "labels" ? "active" : ""} onClick={() => { setView("labels"); setFilter("unreviewed"); setSelected(""); }}>{tr("标签待办", "Label tasks")}</button>
      </nav>
    </section>
    <aside className="panel synthetic-queue">
      <div className="panel-title">{tr("队列", "Queue")} · {visible.length} / {items.length}</div>
      <select aria-label={tr("状态筛选", "Status filter")} value={filter} onChange={event => { setFilter(event.target.value); setSelected(""); }}>
        <option value="unreviewed">{view === "quality" ? tr("未审核", "Unreviewed") : tr("待标签", "Needs label")}</option>
        <option value="pass">{view === "quality" ? tr("已通过", "Passed") : tr("已标注", "Labeled")}</option>
        {view === "quality" && <option value="reject">{tr("已拒绝", "Rejected")}</option>}
        <option value="all">{tr("全部", "All")}</option>
      </select>
      <label><input type="checkbox" checked={highRisk} onChange={event => setHighRisk(event.target.checked)} />{tr("高风险复审", "High risk")}</label>
      <details><summary>{tr("查找片段", "Find clip")}</summary>
        <input value={search} onChange={event => setSearch(event.target.value)} placeholder={tr("来源或 ID", "Source or ID")} /></details>
      <div className="synthetic-queue-list">{visible.map((item, index) =>
        <button key={candidateKey(item)} className={item === current ? "active" : ""}
          onClick={() => setSelected(candidateKey(item))}>
          <strong>{tr("片段", "Clip")} {index + 1}</strong>
          <span>{item.risk_tier === "high" ? tr("高风险提示", "High-risk hint")
            : item.warning_flags.length ? tr("有 QA 提示", "QA hints") : ""}</span>
        </button>)}</div>
      {hasMore && <button onClick={() => browse(items.length).catch(error => setError(String(error)))}>
        {tr("加载更多", "Load more")}</button>}
    </aside>
    <section className="panel synthetic-review">
      {error && <div className="error-banner" role="alert">{error}</div>}
      {message && <div className="success-banner" role="status">{message}</div>}
      {current ? <>
        <div className="synthetic-review-head"><div>
          <h2>{view === "quality" ? tr("判断这段动作是否可用", "Is this motion usable?") : tr("选择正式活动标签", "Choose a formal activity label")}</h2>
          <span>{current.risk_tier === "high" ? tr("高风险提示", "High-risk hint") : tr("动作与 IMU 同源回放", "Motion and IMU replay")}</span>
        </div><button onClick={() => afterCurrent(current, queueMode).catch(error => setError(String(error)))}>{tr("下一条 N", "Next N")}</button></div>
        {current.warning_flags.length > 0 && <p className="warning-banner">QA · {current.warning_flags.join(", ")}</p>}
        <iframe ref={frame} key={base} title={tr("动作与 IMU 同源回放", "Motion and IMU replay")}
          src={`${base}/files/index.html`} className="synthetic-frame" />
        <div className="synthetic-controls">
          <button onClick={() => control("toggle")}>{tr("播放／暂停 Space", "Play / pause Space")}</button>
          <button onClick={() => control("replay")}>{tr("重播 R", "Replay R")}</button>
          {queueMode ? <>
            <button className="primary" disabled={working || summary?.read_only} onClick={() => decide("pass")}>{tr("通过 P", "Pass P")}</button>
            <button disabled={working || summary?.read_only} onClick={() => setRejectOpen(true)}>{tr("拒绝 X", "Reject X")}</button>
            <button disabled={working} onClick={() => {
              void afterCurrent(current, true).catch(error => setError(String(error)));
              setMessage(tr("已跳过，仍为未审核", "Skipped; still unreviewed"));
            }}>{tr("跳过 S", "Skip S")}</button>
          </> : view === "quality" ? <span>{tr("历史审核结果只读", "Historical review is read only")}</span>
          : <>
            <label>{tr("正式标签", "Formal label")} <select value={chosenCode} onChange={event => setChosenCode(event.target.value)}>
              <option value="">{tr("从受控列表选择", "Choose from managed labels")}</option>
              {catalog?.concepts.filter(item => item.active).map(item =>
                <option key={item.code} value={item.code}>{item.name}{item.is_fall ? tr(" · 跌倒", " · Fall") : ""}</option>)}
            </select></label>
            <button className="primary" disabled={working || !chosenCode || summary?.read_only} onClick={saveLabel}>{tr("保存标签", "Save label")}</button>
            {current.label && <span>{tr("当前", "Current")}: {current.label.name} · {current.label.origin === "auto" ? tr("自动来源", "Automatic source") : tr("人工选择", "Human selected")}</span>}
          </>}
        </div>
        {rejectOpen && <div className="synthetic-reject panel" role="dialog" aria-label={tr("拒绝原因", "Rejection reason")}>
          <strong>{tr("选择拒绝原因", "Choose rejection reasons")}</strong>
          <div className="synthetic-reasons">{reasons.map(reason =>
            <label key={reason.code}><input type="checkbox" checked={reasonCodes.includes(reason.code)}
              onChange={event => setReasonCodes(previous => event.target.checked
                ? [...previous, reason.code] : previous.filter(code => code !== reason.code))} />{reason.name}</label>)}</div>
          <textarea value={reasonNote} onChange={event => setReasonNote(event.target.value)}
            placeholder={tr("补充说明（可选）", "Additional details (optional)")} />
          <div><button className="primary" disabled={working || (reasonCodes.length === 0 && !reasonNote.trim())} onClick={() => decide("reject")}>{tr("确认拒绝 Enter", "Confirm reject Enter")}</button>
            <button onClick={() => setRejectOpen(false)}>{tr("取消", "Cancel")}</button></div>
        </div>}
        <details className="synthetic-details"><summary>{tr("来源与技术详情", "Source and technical details")}</summary>
          <p>{current.source_dataset} · {current.source_member} · {current.candidate_id} · revision {current.revision}</p>
          <strong>{tr("来源候选描述，仅供参考", "Source suggestions, for reference only")}</strong>
          {current.label_candidates.length === 0 ? <p>{tr("来源没有提供标签", "No source labels")}</p>
            : <ul>{current.label_candidates.slice(0, 12).map((label, index) =>
              <li key={index}>{label.kind === "temporal-candidate" ? tr("片段", "Segment") : tr("整段", "Recording")}
                {" · "}{label.raw_label || label.code || tr("空标签", "Empty label")}
                {label.categories?.length ? ` [${label.categories.join(", ")}]` : ""}</li>)}</ul>}
        </details>
      </> : <p>{tr("当前队列没有可处理的片段。", "No clips in this queue.")}</p>}
    </section>
  </main>;
}
