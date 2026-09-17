import { useEffect, useMemo, useRef, useState } from "react";
import { tr } from "./i18n";

type Domain = "real" | "synthetic";
type View = "mine" | "claimable" | "completed" | "all";
type WorkItem = {
  key: string; domain: Domain; item_id: string; version_id: string | null;
  title: string; source: string; group: string; status: string; stage: string;
  label_state: string | null; risk: string | null; assignee: string | null;
  duration_s: number | null; published_at_utc: string;
  indexed_at_utc: string | null; claim_paused: boolean; warning_flags?: string[];
};
type Page = {items: WorkItem[]; total: number; next_cursor: string | null};
type Group = {name: string; count: number; paused: number};
type Progress = {published: number; indexed?: number; indexed_at_utc: string | null;
  latest_published_at_utc: string | null; unreviewed?: number; label_pending?: number};

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(path, { ...options, headers: {
    "Content-Type": "application/json", ...(options?.headers ?? {}),
  }});
  const body = await response.json();
  if (!response.ok) throw new Error(typeof body.detail === "string"
    ? body.detail : `HTTP ${response.status}`);
  return body as T;
}

const views: {value: View; zh: string; en: string}[] = [
  {value: "mine", zh: "我的待办", en: "My tasks"},
  {value: "claimable", zh: "待领取", en: "Available"},
  {value: "completed", zh: "已完成", en: "Completed"},
  {value: "all", zh: "全部数据", en: "All data"},
];

function formatDate(value: string | null) {
  return value ? new Date(value).toLocaleString() : tr("尚未完成", "Not yet available");
}

function openItem(item: WorkItem) {
  const url = new URL(location.href);
  if (item.domain === "real") {
    url.searchParams.set("view", "annotate");
    url.searchParams.set("recording", item.item_id);
  } else {
    url.searchParams.set("view", "synthetic");
    url.searchParams.set("candidate", item.item_id);
    url.searchParams.set("version", item.version_id ?? "");
    url.searchParams.set("stage", item.stage === "label" ? "label" : "quality");
  }
  location.assign(url);
}

export function DataManagementPage({isAdmin, syntheticEnabled}: {
  isAdmin: boolean; syntheticEnabled: boolean;
}) {
  const [domain, setDomain] = useState<Domain>(() =>
    syntheticEnabled && new URLSearchParams(location.search).get("domain") === "synthetic"
      ? "synthetic" : "real");
  const [view, setView] = useState<View>("mine");
  const [searchInput, setSearchInput] = useState("");
  const [search, setSearch] = useState("");
  const [group, setGroup] = useState("");
  const [status, setStatus] = useState("");
  const [risk, setRisk] = useState("");
  const [labelState, setLabelState] = useState("");
  const [tier, setTier] = useState("");
  const [items, setItems] = useState<WorkItem[]>([]);
  const [total, setTotal] = useState(0);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [groups, setGroups] = useState<Group[]>([]);
  const [progress, setProgress] = useState<Progress | null>(null);
  const [counts, setCounts] = useState<Partial<Record<View, number>>>({});
  const [busy, setBusy] = useState(false);
  const [pauseBusy, setPauseBusy] = useState(false);
  const [error, setError] = useState("");
  const [scrollTop, setScrollTop] = useState(0);
  const list = useRef<HTMLDivElement>(null);
  const generation = useRef(0);

  useEffect(() => {
    const timer = window.setTimeout(() => setSearch(searchInput.trim()), 250);
    return () => window.clearTimeout(timer);
  }, [searchInput]);

  const params = useMemo(() => {
    const value = new URLSearchParams({domain, view, limit: "50"});
    if (search) value.set("search", search);
    if (group) value.set("group", group);
    if (status) value.set("status", status);
    if (domain === "synthetic") {
      if (risk) value.set("risk", risk);
      if (labelState) value.set("label_state", labelState);
    } else if (tier) value.set("tier", tier);
    return value;
  }, [domain, view, search, group, status, risk, labelState, tier]);

  const load = async (cursor: string | null, current: number) => {
    const query = new URLSearchParams(params);
    if (cursor) query.set("cursor", cursor);
    setBusy(true); setError("");
    try {
      const page = await request<Page>(`/api/v1/work-items?${query}`);
      if (generation.current !== current) return;
      setItems(previous => cursor ? [...previous, ...page.items] : page.items);
      setTotal(page.total); setNextCursor(page.next_cursor);
    } catch (reason) {
      if (generation.current === current) setError(String(reason));
    } finally {
      if (generation.current === current) setBusy(false);
    }
  };

  useEffect(() => {
    const current = ++generation.current;
    setItems([]); setTotal(0); setNextCursor(null); setScrollTop(0);
    list.current?.scrollTo({top: 0});
    void load(null, current);
  }, [params.toString()]);

  useEffect(() => {
    let live = true;
    const refresh = async () => {
      try {
        const [groupResult, progressResult] = await Promise.all([
          request<{groups: Group[]}>(`/api/v1/work-items/groups?domain=${domain}`),
          request<Progress>(`/api/v1/work-items/progress?domain=${domain}`),
        ]);
        if (live) { setGroups(groupResult.groups); setProgress(progressResult); }
      } catch (reason) { if (live) setError(String(reason)); }
    };
    void refresh();
    const timer = window.setInterval(refresh, 20_000);
    return () => { live = false; window.clearInterval(timer); };
  }, [domain]);

  useEffect(() => {
    let live = true;
    const base = new URLSearchParams(params);
    base.delete("limit");
    void Promise.all(views.map(async item => {
      const query = new URLSearchParams(base);
      query.set("view", item.value); query.set("limit", "1");
      const response = await request<Page>(`/api/v1/work-items?${query}`);
      return [item.value, response.total] as const;
    })).then(values => { if (live) setCounts(Object.fromEntries(values)); })
      .catch(reason => { if (live) setError(String(reason)); });
    return () => { live = false; };
  }, [params.toString()]);

  const pauseGroup = async () => {
    const selected = groups.find(item => item.name === group);
    if (!selected || pauseBusy) return;
    setPauseBusy(true); setError("");
    try {
      await request("/api/v1/work-items/claim-pause", {method: "POST", body: JSON.stringify({
        domain, group, paused: !selected.paused,
      })});
      setGroups(previous => previous.map(item => item.name === group
        ? {...item, paused: selected.paused ? 0 : 1} : item));
      void load(null, ++generation.current);
    } catch (reason) { setError(String(reason)); }
    finally { setPauseBusy(false); }
  };

  const rowHeight = 84;
  const start = Math.max(0, Math.floor(scrollTop / rowHeight) - 6);
  const end = Math.min(items.length, start + 28);
  const selectedGroup = groups.find(item => item.name === group);
  return <main className="data-manager">
    <section className="panel data-manager-head">
      <div><div className="panel-title">{tr("数据管理", "Data management")}</div>
        <h2>{tr("先处理待办，再查找全部数据", "Work the queue, then search the catalog")}</h2>
        <p>{tr("这里统一管理真实 IMU 录制与动捕合成片段；打开条目后进入各自的审核工作台。", "Manage real IMU recordings and synthetic clips here, then open them in their review workbenches.")}</p></div>
      <div className="data-manager-progress">
        <strong>{progress ? `${progress.indexed ?? progress.published}/${progress.published}` : "—"}</strong>
        <span>{tr("已索引／已发布", "Indexed / published")}</span>
        <small>{tr("目录更新", "Catalog updated")} · {formatDate(progress?.indexed_at_utc ?? null)}</small>
        <small>{tr("最新数据", "Latest data")} · {formatDate(progress?.latest_published_at_utc ?? null)}</small>
      </div>
    </section>
    <section className="panel data-manager-main">
      <div className="data-manager-domains">
        <button className={domain === "real" ? "active" : ""} onClick={() => {
          const url = new URL(location.href); url.searchParams.delete("domain");
          history.replaceState({}, "", url);
          setDomain("real"); setGroup(""); setStatus(""); setRisk(""); setLabelState("");
        }}>{tr("真实 IMU", "Real IMU")}</button>
        {syntheticEnabled && <button className={domain === "synthetic" ? "active" : ""} onClick={() => {
          const url = new URL(location.href); url.searchParams.set("domain", "synthetic");
          history.replaceState({}, "", url);
          setDomain("synthetic"); setGroup(""); setStatus(""); setTier("");
        }}>{tr("合成运动", "Synthetic motion")}</button>}
      </div>
      <nav className="data-manager-views" aria-label={tr("任务视图", "Task views")}>
        {views.map(item => <button key={item.value} className={view === item.value ? "active" : ""}
          onClick={() => setView(item.value)}>{tr(item.zh, item.en)} <b>{counts[item.value] ?? "·"}</b></button>)}
      </nav>
      <div className="data-manager-filters">
        <input aria-label={tr("搜索来源、参与者或 ID", "Search source, participant or ID")}
          value={searchInput} onChange={event => setSearchInput(event.target.value)}
          placeholder={tr("搜索来源、参与者或 ID", "Search source, participant or ID")} />
        <input aria-label={tr("来源或批次", "Source or batch")} list="data-manager-groups"
          value={group} onChange={event => setGroup(event.target.value)}
          placeholder={domain === "real" ? tr("采集批次", "Collection") : tr("动捕来源", "Motion source")} />
        <datalist id="data-manager-groups">{groups.map(item =>
          <option key={item.name} value={item.name}>{item.count}</option>)}</datalist>
        <select aria-label={tr("状态", "Status")} value={status}
          onChange={event => setStatus(event.target.value)}>
          <option value="">{tr("全部状态", "All statuses")}</option>
          {(domain === "real" ? ["unassigned", "in_progress", "completed"]
            : ["unreviewed", "pass", "reject"]).map(value =>
            <option value={value} key={value}>{value}</option>)}</select>
        {domain === "real" ? <select aria-label={tr("数据级别", "Data tier")} value={tier}
          onChange={event => setTier(event.target.value)}>
          <option value="">{tr("全部级别", "All tiers")}</option>
          <option value="prod">{tr("正式", "Production")}</option>
          <option value="test">{tr("测试", "Test")}</option>
        </select> : <>
          <select aria-label={tr("风险", "Risk")} value={risk}
            onChange={event => setRisk(event.target.value)}>
            <option value="">{tr("全部风险", "All risks")}</option>
            <option value="high">{tr("高风险", "High risk")}</option>
            <option value="ordinary">{tr("常规", "Ordinary")}</option>
            <option value="unknown">{tr("未评估", "Unknown")}</option></select>
          <select aria-label={tr("标签状态", "Label status")} value={labelState}
            onChange={event => setLabelState(event.target.value)}>
            <option value="">{tr("全部标签", "All labels")}</option>
            <option value="pending">{tr("待标签", "Needs label")}</option>
            <option value="labeled">{tr("已标签", "Labeled")}</option>
          </select>
        </>}
        {isAdmin && selectedGroup && <button className={selectedGroup.paused ? "primary" : ""}
          disabled={pauseBusy} onClick={() => void pauseGroup()}>
          {selectedGroup.paused ? tr("恢复新领取", "Resume claiming")
            : tr("暂停新领取", "Pause claiming")}</button>}
      </div>
      {error && <div className="error-banner" role="alert">{error}</div>}
      <div className="data-manager-result-head"><strong>{tr("匹配条目", "Matching items")} · {total}</strong>
        <span>{tr("仅逐条审核；批次暂停不会撤销已领取任务", "Review items individually; a paused batch keeps existing claims")}</span></div>
      <div ref={list} className="data-manager-list" onScroll={event =>
        setScrollTop(event.currentTarget.scrollTop)}>
        {items.length === 0 && !busy && <div className="data-manager-empty">
          {view === "mine" ? tr("目前没有我的待办；可切到“待领取”或“全部数据”。", "No tasks assigned to you. Try Available or All data.")
            : tr("当前筛选没有匹配数据。", "No items match these filters.")}</div>}
        <div style={{height: start * rowHeight}} />
        {items.slice(start, end).map(item => <button key={item.key} className="data-manager-row"
          onClick={() => openItem(item)}>
          <div><strong>{item.title}</strong><span>{item.source} · {item.group}</span></div>
          <div><span>{item.item_id}</span>
            <small>{item.duration_s != null ? `${item.duration_s.toFixed(1)} s · ` : ""}{formatDate(item.published_at_utc)}</small></div>
          <div className="data-manager-row-state"><b>{item.stage === "quality" ? tr("质量审核", "Quality")
            : item.stage === "label" ? tr("正式标签", "Label")
              : item.stage === "annotation" ? tr("标注与同步", "Annotation") : tr("已完成", "Done")}</b>
            <span>{item.status}{item.risk === "high" ? " · QA 高风险" : ""}
              {item.claim_paused ? ` · ${tr("暂停领取", "Claim paused")}` : ""}</span></div>
        </button>)}
        <div style={{height: Math.max(0, items.length - end) * rowHeight}} />
      </div>
      <div className="data-manager-footer"><span>{tr("已加载", "Loaded")} {items.length}/{total}</span>
        {nextCursor && <button disabled={busy} onClick={() => void load(nextCursor, generation.current)}>
          {busy ? tr("正在加载…", "Loading…") : tr("加载下一页", "Load next page")}</button>}</div>
    </section>
  </main>;
}
