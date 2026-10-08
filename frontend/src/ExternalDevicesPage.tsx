import { useEffect, useMemo, useRef, useState } from "react";
import { DeviceEvents, DeviceFields, SleepReports, externalRequest as request, type EventSeries, type Preparation } from "./ExternalDeviceInsights";
import { ExternalDeviceChart } from "./ExternalDeviceChart";
import { beijingDay, beijingTime, dayRange, externalViewUrl, localTimeInput, readExternalView,
  resolveExternalRange, chooseExternalMetric, metricPreferenceKey, quickMetricKeys, latestMetricRange, type AggregateSeries, type ExternalView, type TimePreset, type TimeRange } from "./externalDeviceData";
import "./externalDevices.css";

type DrawerTab = "records" | "reports" | "events" | "fields" | "status" | "manage";
const base = "/api/v1/external-devices";
const noEvents: EventSeries["points"] = [];
const connectPointsPreference = "imu-external-connect-points-v1";
type Device = {id: string; kind: "radar-watch" | "mattress"; device_no: string; display_name: string;
  product_key: string | null; listed: boolean; first_seen: string; first_record: string | null;
  total: number; last_record: string | null; last_success: string | null; synthetic_records: number;
  history_start: string | null; history_complete: boolean; initial_sync?: boolean; synced_until: string;
  last_error: {start: string; end: string; error: string} | null};
type Metric = {key: string; label: string; unit: string; method: string; first_record: string; last_record: string};
type SyncTask = {id: string; mode: "sync" | "rebuild"; state: "pending" | "running" | "done" | "failed";
  phase: string; devices_done: number; devices_total: number; windows_done: number; windows_total: number;
  elapsed_s: number; new_records: number; request_count: number; response_bytes: number; error: string | null; finished_at: string | null};
type Status = {sync_interval_s: number; worker_heartbeat: string | null; worker_online: boolean; generation: string; task: SyncTask | null;
  paused: boolean; error: string | null; last_discovery: string | null; history_start: string | null; history_start_source: string};
const elapsed = (seconds: number) => seconds >= 60 ? `${Math.floor(seconds / 60)} 分 ${Math.floor(seconds % 60)} 秒` : `${Math.floor(seconds)} 秒`;
function taskLabel(task: SyncTask) {
  const name = task.mode === "rebuild" ? "重建" : "更新";
  if (task.state === "failed") return `${name}失败 · ${task.error ?? "请重试"}`;
  if (task.state === "done") return `${name}完成 · ${task.mode === "rebuild" ? "入库" : "新增"} ${task.new_records.toLocaleString()} 条 · ${elapsed(task.elapsed_s)}`;
  const phase = ({queued: "等待执行", discovering: "刷新设备列表", fetching: `拉取数据 · ${task.devices_done}/${task.devices_total} 台 · ${task.windows_done}/${task.windows_total} 段`, publishing: "校验并切换"} as Record<string, string>)[task.phase] ?? "处理中";
  return `${name}中 · ${phase} · ${elapsed(task.elapsed_s)}`;
}
type ExportJob = {id: string; state: string; error: string | null; size_bytes: number | null};
type Row = {id: string; source_id: string; message_type: string; received_at: string; metrics: Record<string, number | null>; synthetic: boolean};
type Page = {records: Row[]; next_cursor: string | null; total: number};
type Detail = {record_json: string; raw_payload: string | null};
const presets: [TimePreset, string, string][] = [["1d", "近1日", "过去24小时"], ["7d", "近1周", "过去7天"],
  ["30d", "近1月", "过去30天"], ["365d", "近1年", "过去365天"], ["all", "全部", "全部已归档时间"]];


function download(id: string) {
  const link = document.createElement("a");
  link.href = `${base}/exports/${id}/download`; link.download = `external-data-${id}.zip`;
  document.body.append(link); link.click(); link.remove();
}

function DownloadButton({device, kind}: {device?: string; kind?: string}) {
  const [job, setJob] = useState<ExportJob | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const lock = useRef(false);
  const alive = useRef(true);
  const delivered = useRef("");
  useEffect(() => {alive.current = true; return () => {alive.current = false;};}, []);
  useEffect(() => {
    if (!job || !["pending", "running"].includes(job.state)) return;
    const abort = new AbortController();
    let timer = 0;
    const poll = async () => {
      try {
        const next = await request<ExportJob>(`/exports/${job.id}`, {signal: abort.signal});
        if (abort.signal.aborted) return;
        setJob(next);
        if (next.state === "ready" && delivered.current !== next.id) {delivered.current = next.id; download(next.id);}
        if (next.state === "failed") setError(next.error ?? "生成失败，请重试");
      } catch (reason) {if (!abort.signal.aborted) setError(String(reason));}
      if (!abort.signal.aborted) timer = window.setTimeout(() => void poll(), 1500);
    };
    timer = window.setTimeout(() => void poll(), 500);
    return () => {abort.abort(); clearTimeout(timer);};
  }, [job?.id, job?.state]);
  const start = async () => {
    if (lock.current) return;
    lock.current = true; setPending(true); setError("");
    try {
      const result = await request<ExportJob>("/exports", {method: "POST", body: JSON.stringify(
        device ? {scope: "device", device_id: device} : {scope: "kind", kind})});
      if (alive.current) setJob(result);
    } catch (reason) {if (alive.current) setError(String(reason));}
    finally {lock.current = false; if (alive.current) setPending(false);}
  };
  const working = pending || job?.state === "pending" || job?.state === "running";
  const title = error ? `${error}；点击重试` : working ? "正在准备全量下载…" : device ? "下载此设备全部数据" : "下载此类型全部设备数据";
  return <span className="external-download">
    <button className={error ? "download-error" : ""} aria-label={title} title={title} disabled={working} onClick={() => void start()}>{working ? "◌" : error ? "重试" : "↓"}</button>
    {job?.state === "ready" && <a href={`${base}/exports/${job.id}/download`} download title="下载包已就绪，点击再次下载" aria-label="再次下载">✓</a>}
  </span>;
}

function RecordList({device, range}: {device: string; range: TimeRange}) {
  const [page, setPage] = useState<Page | null>(null);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [selected, setSelected] = useState("");
  const [error, setError] = useState("");
  const [more, setMore] = useState(false);
  const lock = useRef(false);
  const controller = useRef(new AbortController());
  const query = new URLSearchParams(range).toString();
  useEffect(() => {
    const abort = new AbortController(); controller.current = abort;
    request<Page>(`/devices/${device}/records?${query}`, {signal: abort.signal})
      .then(value => {if (!abort.signal.aborted) setPage(value);})
      .catch(reason => {if (!abort.signal.aborted) setError(String(reason));});
    return () => abort.abort();
  }, [device, query]);
  useEffect(() => {
    setDetail(null);
    if (!selected) return;
    const abort = new AbortController();
    request<Detail>(`/devices/${device}/records/${selected}`, {signal: abort.signal})
      .then(value => {if (!abort.signal.aborted) setDetail(value);})
      .catch(reason => {if (!abort.signal.aborted) setError(String(reason));});
    return () => abort.abort();
  }, [device, selected]);
  const load = async () => {
    if (!page?.next_cursor || lock.current) return;
    lock.current = true; setMore(true);
    try {
      const next = await request<Page>(`/devices/${device}/records?${query}&cursor=${encodeURIComponent(page.next_cursor)}`, {signal: controller.current.signal});
      if (!controller.current.signal.aborted) setPage(old => ({...next, records: [...(old?.records ?? []), ...next.records]}));
    } catch (reason) {if (!controller.current.signal.aborted) setError(String(reason));}
    finally {lock.current = false; if (!controller.current.signal.aborted) setMore(false);}
  };
  return <><p className="external-muted">{beijingTime(range.start)} — {beijingTime(range.end)}</p>
    {error && <p role="alert">{error}</p>}
    {!page ? <p>正在读取记录…</p> : <>
      <p>{page.total.toLocaleString()} 条原始记录</p>
      <div className="external-record-list">{page.records.map(row => <button key={row.id} className={selected === row.id ? "active" : ""} onClick={() => setSelected(row.id)}>
        <span>{beijingTime(row.received_at)}</span><small>{row.message_type}{row.synthetic ? " · 合成" : ""} · #{row.source_id}</small>
      </button>)}</div>
      {page.next_cursor && <button disabled={more} onClick={() => void load()}>{more ? "正在读取…" : "加载更多"}</button>}
    </>}
    {detail && <section className="external-original"><h4>原始 JSON</h4><pre>{detail.record_json}</pre>
      {detail.raw_payload != null && <details><summary>rawPayload</summary><pre>{detail.raw_payload}</pre></details>}</section>}
  </>;
}

export function ExternalDevicesPage({isAdmin}: {isAdmin: boolean}) {
  const [view, setView] = useState(() => readExternalView(location.search));
  const viewRef = useRef(view); viewRef.current = view;
  const [devices, setDevices] = useState<Device[]>([]);
  const [status, setStatus] = useState<Status | null>(null);
  const [metadata, setMetadata] = useState<{device: string; ready: boolean; preparation: Preparation; metrics: Metric[]} | null>(null);
  const [events, setEvents] = useState<EventSeries | null>(null);
  const [series, setSeries] = useState<AggregateSeries | null>(null);
  const [clock, setClock] = useState(Date.now());
  const [refresh, setRefresh] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [directoryOpen, setDirectoryOpen] = useState(false);
  const [emptyGroupsOpen, setEmptyGroupsOpen] = useState({"radar-watch": false, mattress: false});
  const [connectPoints, setConnectPoints] = useState(() => {
    try {return localStorage.getItem(connectPointsPreference) !== "false";}
    catch {return true;}
  });
  const [customOpen, setCustomOpen] = useState(false);
  const [customStart, setCustomStart] = useState("");
  const [customEnd, setCustomEnd] = useState("");
  const [drawer, setDrawer] = useState<DrawerTab | null>(null);
  const [inspectRange, setInspectRange] = useState<TimeRange | null>(null);
  const [zoomHistory, setZoomHistory] = useState<ExternalView[]>([]);
  const [budget, setBudget] = useState(800);
  const [historyDate, setHistoryDate] = useState("");
  const [rebuildOpen, setRebuildOpen] = useState(false);
  const [action, setAction] = useState(false);
  const [notice, setNotice] = useState("");
  const actionLock = useRef(false);
  const plotArea = useRef<HTMLDivElement>(null);
  const device = devices.find(item => item.id === view.device);
  const metrics = metadata?.device === view.device ? metadata.metrics : [];
  const metric = metrics.find(item => item.key === view.metric);
  const range = useMemo(() => resolveExternalRange(view, clock, device?.first_record ?? device?.first_seen),
    [view.preset, view.from, view.to, clock, device?.first_record, device?.first_seen]);
  const eventOnlySeries: AggregateSeries = {metric: "events", label: "报警上报", unit: "", method: "events", interval_ms: 1, count: 0, points: [], coverage: {complete: true, gaps: []}};
  const update = (patch: Partial<ExternalView>, replace = false) => {
    const next = {...viewRef.current, ...patch};
    viewRef.current = next; setView(next);
    history[replace ? "replaceState" : "pushState"]({}, "", externalViewUrl(location.href, next));
    if (!replace) {setDrawer(null); setInspectRange(null); setDirectoryOpen(false);}
  };
  useEffect(() => {
    const pop = () => {const next = readExternalView(location.search); viewRef.current = next; setView(next); setDrawer(null); setZoomHistory([]);};
    const escape = (event: KeyboardEvent) => {if (event.key === "Escape") {setDrawer(null); setDirectoryOpen(false); setCustomOpen(false); setRebuildOpen(false);}};
    window.addEventListener("popstate", pop); window.addEventListener("keydown", escape);
    return () => {window.removeEventListener("popstate", pop); window.removeEventListener("keydown", escape);};
  }, []);
  useEffect(() => {
    const abort = new AbortController(); let inFlight = false; let timer = 0;
    const load = async () => {
      if (inFlight || document.hidden) return;
      inFlight = true; clearTimeout(timer);
      let interval = 10000;
      try {
        const [directory, sync] = await Promise.all([
          request<{devices: Device[]; generation: string}>("/devices", {signal: abort.signal}), request<Status>("/status", {signal: abort.signal})]);
        if (!abort.signal.aborted) {
          if (directory.generation !== sync.generation) {interval = 500; return;}
          setDevices(directory.devices); setStatus(sync);
          if (sync.task && ["pending", "running"].includes(sync.task.state)) interval = 2000;
        }
      } catch (reason) {if (!abort.signal.aborted) setError(String(reason));}
      finally {inFlight = false; if (!abort.signal.aborted) timer = window.setTimeout(() => void load(), interval);}
    };
    void load();
    document.addEventListener("visibilitychange", load);
    return () => {abort.abort(); clearTimeout(timer); document.removeEventListener("visibilitychange", load);};
  }, [refresh]);
  useEffect(() => {
    if (devices.length && !devices.some(item => item.id === view.device)) {
      update({device: (devices.find(item => item.total > 0) ?? devices[0]).id}, true);
    }
    if (status && !devices.length && view.device) update({device: ""}, true);
  }, [devices, view.device]);
  useEffect(() => {
    if (device?.total === 0) setEmptyGroupsOpen(previous => ({...previous, [device.kind]: true}));
  }, [device?.id, device?.kind, device?.total]);
  useEffect(() => {
    if (!view.device) return;
    const abort = new AbortController();
    let timer = 0;
    setClock(Date.now());
    const load = async () => {
      try {
        const result = await request<{ready: boolean; preparation: Preparation; metrics: Metric[]}>(`/devices/${view.device}/metrics`, {signal: abort.signal});
        if (abort.signal.aborted) return;
        setMetadata({...result, device: view.device});
        if (!result.ready || !result.preparation.ready) timer = window.setTimeout(() => void load(), 2000);
      } catch (reason) {if (!abort.signal.aborted) setError(String(reason));}
    };
    void load();
    return () => {abort.abort(); clearTimeout(timer);};
  }, [view.device, device?.last_success, status?.generation, refresh]);
  useEffect(() => {
    if (!metrics.length || metric || !device) return;
    if (!metadata?.preparation.ready && ["Amp_value", "CO", "distance_km", "STTIME", "energy_kcal"].includes(view.metric)) return;
    let remembered = "";
    try {remembered = localStorage.getItem(metricPreferenceKey(device.kind)) ?? "";} catch { /* Storage may be disabled. */ }
    update({metric: chooseExternalMetric(device.kind, metrics.map(item => item.key), view.metric, remembered)}, true);
  }, [metadata, view.metric, device?.kind]);
  const chooseMetric = (key: string) => {
    if (device) try {localStorage.setItem(metricPreferenceKey(device.kind), key);} catch { /* Keep URL state available. */ }
    update({metric: key});
  };
  useEffect(() => {
    setEvents(null);
    if (!view.device) return;
    const abort = new AbortController();
    request<EventSeries>(`/devices/${view.device}/events?${new URLSearchParams({...range, point_budget: String(budget)})}`, {signal: abort.signal})
      .then(value => {if (!abort.signal.aborted) setEvents(value);})
      .catch(reason => {if (!abort.signal.aborted) setError(String(reason));});
    return () => abort.abort();
  }, [view.device, range.start, range.end, budget, device?.last_success, status?.generation, metadata?.preparation.ready, refresh]);
  useEffect(() => {
    if (!plotArea.current) return;
    let timer = 0;
    const resize = new ResizeObserver(entries => {
      clearTimeout(timer); const width = entries[0].contentRect.width;
      timer = window.setTimeout(() => setBudget(Math.max(100, Math.min(2000, Math.round(width / 100) * 100))), 180);
    });
    resize.observe(plotArea.current);
    return () => {clearTimeout(timer); resize.disconnect();};
  }, []);
  useEffect(() => {
    setSeries(null); setError("");
    if (!view.device || !metric) {setBusy(false); return;}
    const abort = new AbortController(); setBusy(true);
    request<AggregateSeries>(`/devices/${view.device}/series?${new URLSearchParams({...range, metric: metric.key, point_budget: String(budget)})}`, {signal: abort.signal})
      .then(result => {if (!abort.signal.aborted) setSeries(result);})
      .catch(reason => {if (!abort.signal.aborted) setError(String(reason));})
      .finally(() => {if (!abort.signal.aborted) setBusy(false);});
    return () => abort.abort();
  }, [view.device, metric?.key, range.start, range.end, budget, device?.last_success, status?.generation, refresh]);
  const choosePreset = (preset: TimePreset) => {setClock(Date.now()); setZoomHistory([]); update({preset});};
  const zoom = (next: TimeRange) => {setZoomHistory(previous => [...previous, view]); update({preset: "custom", from: next.start, to: next.end});};
  const latest = () => {
    if (!metric) return;
    zoom(latestMetricRange(metric.last_record));
  };
  useEffect(() => {if (status?.history_start) setHistoryDate(beijingDay(new Date(status.history_start)));}, [status?.history_start]);
  const taskBusy = Boolean(status?.task && ["pending", "running"].includes(status.task.state));
  const perform = async (path: string, body?: object, method = "POST") => {
    if (actionLock.current) return;
    actionLock.current = true; setAction(true); setNotice("");
    try {
      const result = await request<SyncTask>(path, {method, ...(body ? {body: JSON.stringify(body)} : {})});
      if (path === "/settings") setNotice("历史起点已保存，用于新设备和下一次全量重建");
      else setStatus(previous => previous ? {...previous, task: result} : previous);
      setRebuildOpen(false); setRefresh(n => n + 1);
    }
    catch (reason) {setNotice(String(reason));}
    finally {actionLock.current = false; setAction(false);}
  };
  const stale = !status?.worker_online;
  const syncLabel = status?.paused ? "同步暂停" : stale ? "未连接同步服务" : status?.error ? "同步异常" : "同步正常";
  const openDetails = (tab: DrawerTab) => {setInspectRange(null); setDrawer(tab);};
  const renderDevice = (item: Device) => <div className={`external-device-row ${view.device === item.id ? "active" : ""}`} key={item.id}>
    <button className="external-device-select" onClick={() => {setZoomHistory([]); update({device: item.id, metric: ""});}} title={`${item.display_name}${item.product_key ? ` · ${item.product_key}` : ""}\n最新数据 ${beijingTime(item.last_record)}`}>
      <strong>{item.device_no}</strong><small>{item.initial_sync ? `${item.total.toLocaleString()} 条 · ${taskBusy ? "首次同步中" : "首次同步待完成"}` : item.listed ? `${item.total.toLocaleString()} 条记录` : "已归档 · 不在上游列表"}</small><small className="external-device-last">最近 {beijingTime(item.last_record)}</small>
    </button><DownloadButton device={item.id} />
  </div>;
  return <section className="external-page">
    <aside className={`external-directory ${directoryOpen ? "open" : ""}`} aria-label="设备列表">
      <div className="external-directory-title"><strong>设备</strong><small>{devices.length} 台</small><button className="external-mobile-only" onClick={() => setDirectoryOpen(false)}>关闭</button></div>
      <div className="external-directory-scroll">{(["radar-watch", "mattress"] as const).map(kind => {
        const group = devices.filter(item => item.kind === kind);
        const empty = group.filter(item => item.total === 0 && !item.initial_sync);
        return <section key={kind} className="external-device-group">
          <div className="external-group-title"><span>{kind === "radar-watch" ? "手表" : "床垫"} <small>{group.length}</small></span><DownloadButton kind={kind} /></div>
          {group.filter(item => item.total > 0 || item.initial_sync).map(renderDevice)}
          {empty.length > 0 && <div className="external-empty-devices">
            <button className="external-empty-toggle" aria-expanded={emptyGroupsOpen[kind]} aria-controls={`external-empty-${kind}`}
              onClick={() => setEmptyGroupsOpen(previous => ({...previous, [kind]: !previous[kind]}))}>
              <span aria-hidden="true">{emptyGroupsOpen[kind] ? "▾" : "▸"}</span>暂无数据（{empty.length}）
            </button>
            <div id={`external-empty-${kind}`} hidden={!emptyGroupsOpen[kind]}>{empty.map(renderDevice)}</div>
          </div>}
        </section>;
      })}{!devices.length && <p className="external-muted">等待同步进程发现设备…</p>}</div>
      <button className={`external-sync-status ${stale || status?.error || status?.paused ? "warning" : ""}`} onClick={() => openDetails("status")}><i />{syncLabel}<span>详情 ›</span></button>
    </aside>
    {directoryOpen && <button className="external-directory-shade" aria-label="关闭设备列表" onClick={() => setDirectoryOpen(false)} />}
    <main className="external-workspace">
      <div className="external-toolbar">
        <div className="external-toolbar-top"><button className="external-mobile-only" onClick={() => setDirectoryOpen(true)}>设备 ☰</button>
          <div className="external-device-heading"><strong>{device?.device_no ?? "外部设备数据"}</strong><span>{device?.kind === "radar-watch" ? "手表" : device ? "床垫" : ""}</span></div>
          {isAdmin && <button className="external-refresh-button" disabled={action || taskBusy || stale} title={stale ? "请先启动已配置密钥的同步进程" : "更新全部设备：发现新设备并拉取新增数据"} onClick={() => void perform("/sync")}>{taskBusy ? "处理中…" : "更新数据"}</button>}
          {device?.kind === "mattress" && <button className="external-reports-button" onClick={() => openDetails("reports")}>睡眠报告</button>}
          <button className="external-details-button" onClick={() => openDetails(device ? "records" : isAdmin ? "manage" : "status")}>详情</button>
        </div>
        {(taskBusy || status?.task?.state === "failed" || notice || stale) && <div className="external-operation-line" role="status">
          {status?.task && status.task.state !== "done" && <button onClick={() => openDetails(isAdmin ? "manage" : "status")}>{taskLabel(status.task)}</button>}
          {notice && <span>{notice}</span>}{stale && <span>未连接同步服务</span>}
        </div>}
        <div className="external-metric-toolbar" aria-label="指标选择">
          <div className="external-quick-metrics">{(quickMetricKeys[device?.kind ?? ""] ?? []).map(key => metrics.find(item => item.key === key)).filter((item): item is Metric => !!item).map(item =>
            <button key={item.key} className={metric?.key === item.key ? "active" : ""} aria-pressed={metric?.key === item.key} onClick={() => chooseMetric(item.key)}>{item.label}</button>)}</div>
          <label className="external-metric"><select aria-label="更多指标" value={metric && !(quickMetricKeys[device?.kind ?? ""] ?? []).includes(metric.key) ? metric.key : ""} onChange={event => {if (event.target.value) chooseMetric(event.target.value);}} disabled={!metrics.length}>
            <option value="">更多指标</option>{metrics.filter(item => !(quickMetricKeys[device?.kind ?? ""] ?? []).includes(item.key)).map(item => <option key={item.key} value={item.key}>{item.label} · {item.unit}</option>)}
          </select></label>
          <span className="external-metric-unit">{metric?.unit}</span>
          {metadata?.device === view.device && !metadata.preparation.ready && <small>正在整理新增指标…</small>}
        </div>
        <div className="external-time-toolbar"><div className="external-presets">{presets.map(([key, label, title]) => <button key={key} title={title} className={view.preset === key ? "active" : ""} onClick={() => choosePreset(key)}>{label}</button>)}
          <button className={view.preset === "custom" ? "active" : ""} onClick={() => {setCustomStart(localTimeInput(range.start)); setCustomEnd(localTimeInput(range.end)); setCustomOpen(true);}}>自定义</button>
        </div>{zoomHistory.length > 0 && <button className="external-zoom-back" onClick={() => {const previous = zoomHistory.at(-1)!; setZoomHistory(items => items.slice(0, -1)); update(previous);}}>← 返回上个范围</button>}
        </div>
        <div className="external-latest"><span>{metric ? `${metric.label}最近上报 · ${beijingTime(metric.last_record)}` : `设备最近上报 · ${beijingTime(device?.last_record)}`}</span>
          <button disabled={!metric} onClick={latest}>跳到最新数据</button>
          {!!events?.total && <button className="external-event-count" onClick={() => openDetails("events")}>△ {events.total} 条报警上报</button>}
        </div>
        <div className="external-range-summary"><span>{beijingTime(range.start)} — {beijingTime(range.end)} <small>北京时间</small></span>
          <span className="external-badges">{device && !device.history_complete && <button onClick={() => openDetails("status")}>历史未补齐</button>}
            {series && !series.coverage.complete && <button onClick={() => openDetails("status")}>部分覆盖</button>}
            {device && device.synthetic_records > 0 && <button onClick={() => openDetails("status")}>含合成数据</button>}</span>
        </div>
      </div>
      <div className="external-plot-panel" ref={plotArea}>
        {error ? <div className="external-empty" role="alert"><strong>暂时无法读取数据</strong><p>{error}</p><button onClick={() => setRefresh(n => n + 1)}>重试</button></div>
          : busy ? <div className="external-empty" role="status">正在读取趋势…</div>
          : (series && series.count > 0) || (events?.total ?? 0) > 0 ? <ExternalDeviceChart series={series ?? eventOnlySeries} range={range} onZoom={zoom} connectPoints={connectPoints}
              onConnectPointsChange={connected => {
                setConnectPoints(connected);
                try {localStorage.setItem(connectPointsPreference, String(connected));} catch { /* Keep the preference for this visit if storage is unavailable. */ }
              }}
              events={events?.points ?? noEvents} onInspectEvent={selectedRange => {setInspectRange(selectedRange); setDrawer("events");}}
              onInspect={selectedRange => {setInspectRange(selectedRange); setDrawer("records");}} />
          : <div className="external-empty"><span className="external-empty-icon">⌁</span><strong>{metadata?.ready === false ? "正在准备统计索引" : "这个时间范围内暂无数据"}</strong>
            <p>{metric ? `所选指标：${metric.label}。可以扩大范围，或定位最近一次记录。` : "设备有可视化指标后会显示在这里。"}</p>
            {metric && <button onClick={latest}>跳到最新数据</button>}</div>}
      </div>
    </main>
    {customOpen && <div className="external-modal-shade" onClick={() => setCustomOpen(false)}><form className="external-time-dialog" role="dialog" aria-modal="true" aria-label="自定义时间范围" onClick={e => e.stopPropagation()} onSubmit={event => {
      event.preventDefault(); const start = new Date(`${customStart}+08:00`), end = new Date(`${customEnd}+08:00`);
      if (!Number.isFinite(+start) || !Number.isFinite(+end) || start >= end) {setNotice("请输入开始早于结束的时间"); return;}
      setNotice(""); setCustomOpen(false); zoom({start: start.toISOString(), end: end.toISOString()});
    }}><h3>自定义时间范围</h3><label>开始（北京时间）<input type="datetime-local" required value={customStart} onChange={e => setCustomStart(e.target.value)} /></label>
      <label>结束（不含该时刻）<input type="datetime-local" required value={customEnd} onChange={e => setCustomEnd(e.target.value)} /></label>
      {notice && <p role="alert">{notice}</p>}<div><button type="button" onClick={() => setCustomOpen(false)}>取消</button><button className="primary" type="submit">应用范围</button></div>
    </form></div>}
    {rebuildOpen && <div className="external-modal-shade" onClick={() => {if (!action) setRebuildOpen(false);}}>
      <div className="external-time-dialog" role="dialog" aria-modal="true" aria-label="确认全量重建" onClick={e => e.stopPropagation()}>
        <h3>确认全量重建</h3>
        <p>重新拉取全部设备从 <strong>{beijingTime(status?.history_start)}</strong> 到发起时的当前时间之间的数据（北京时间）。</p>
        <p>校验成功后替换当前外部设备数据库，上游不再返回的记录将不在新库中显示。旧库和原始归档会保留，可恢复。</p>
        <p>执行期间可以继续浏览旧数据；普通更新暂缓。失败时继续使用旧库。</p>
        {notice && <p role="alert">{notice}</p>}
        <div><button disabled={action} onClick={() => setRebuildOpen(false)}>取消</button><button className="external-rebuild-button" disabled={action || taskBusy || stale} onClick={() => void perform("/rebuild", {confirmed: true, history_start: status?.history_start})}>{action ? "正在提交…" : "确认重建"}</button></div>
      </div>
    </div>}
    {drawer && (device || drawer !== "records") && <aside className="external-detail-drawer" role="dialog" aria-label="设备详情">
      <div className="external-drawer-heading"><strong>{device?.device_no ?? "外部设备数据"}</strong><button aria-label="关闭详情" onClick={() => setDrawer(null)}>✕</button></div>
      <div className="external-drawer-tabs">{(["records", ...(device?.kind === "mattress" ? ["reports"] : []), "events", "fields", "status", ...(isAdmin ? ["manage"] : [])] as DrawerTab[]).map(tab => <button key={tab} className={drawer === tab ? "active" : ""} onClick={() => setDrawer(tab as typeof drawer)}>{({records: "记录", reports: "睡眠", events: "报警", fields: "字段", status: "同步", manage: "管理"})[tab]}</button>)}</div>
      <div className="external-drawer-scroll">
        {drawer === "records" && device && <RecordList key={`${status?.generation}:${device.id}:${(inspectRange ?? range).start}:${(inspectRange ?? range).end}`} device={device.id} range={inspectRange ?? range} />}
        {drawer === "reports" && device && <SleepReports key={`${status?.generation}:${device.id}`} device={device.id} />}
        {drawer === "events" && device && <DeviceEvents key={`${status?.generation}:${device.id}:${(inspectRange ?? range).start}:${(inspectRange ?? range).end}`} device={device.id} range={inspectRange ?? range} />}
        {drawer === "fields" && device && <DeviceFields key={`${status?.generation}:${device.id}`} device={device.id} />}
        {drawer === "status" && status?.task && <p>{taskLabel(status.task)}</p>}
        {drawer === "status" && device && <><dl className="external-facts">
          <dt>最新数据</dt><dd>{beijingTime(device.last_record)}</dd><dt>最近同步</dt><dd>{beijingTime(device.last_success)}</dd>
          <dt>增量连续覆盖至</dt><dd>{beijingTime(device.synced_until)}</dd><dt>设备身份</dt><dd>{device.product_key ? `${device.product_key} / ` : ""}{device.device_no}</dd>
          <dt>已归档记录</dt><dd>{device.total.toLocaleString()} 条 · 合成 {device.synthetic_records.toLocaleString()} 条</dd></dl>
          <p>{!device.history_start ? "历史起点等待接口方确认，目前从设备首次发现开始增量同步。" : device.history_complete ? "已完成设备历史起点之后的首次同步。" : "历史补拉进行中。"}</p>
          <p>{syncLabel} · 设备目录更新于 {beijingTime(status?.last_discovery)}</p>
          {status?.error && <p role="alert">{status.error}</p>}{device.last_error && <p role="alert">{device.last_error.error}</p>}
          {series && <><h4>当前图表范围</h4><p>{series.coverage.complete ? "已查询完整" : "尚有未覆盖区间"}</p>{series.coverage.gaps.slice(0, 100).map(([start, end]) => <p key={start} className="external-muted">{beijingTime(start)} — {beijingTime(end)}</p>)}</>}
        </>}
        {drawer === "manage" && isAdmin && <><h4>更新全部设备</h4>
          <p>发现新设备并拉取历史；已有设备只新增记录。自动更新间隔 {elapsed(status?.sync_interval_s ?? 900)}。</p>
          <button disabled={action || taskBusy || stale} onClick={() => void perform("/sync")}>更新数据</button>
          {stale && <p>未连接同步服务，请先启动已配置密钥的同步进程。</p>}
          {status?.task && <><p>{taskLabel(status.task)}</p><p className="external-muted">查询区间 {status.task.windows_done}/{status.task.windows_total} · 请求 {status.task.request_count} 次 · 接收 {(status.task.response_bytes / 1024 / 1024).toFixed(2)} MB</p>
            {status.task.finished_at && <p className="external-muted">完成于 {beijingTime(status.task.finished_at)}</p>}</>}
          <h4>统一历史起点</h4><p>用于新设备及下一次全量重建。现有设备的日常更新继续使用各自的同步进度。</p>
          <label>开始日期（北京时间）<input type="date" value={historyDate} max={beijingDay()} disabled={action || taskBusy} onChange={e => setHistoryDate(e.target.value)} /></label>
          <p className="external-muted">{status?.history_start_source === "provider" ? "接口方已确认" : "用户设定的查询起点，不代表上游最早留存日期"}</p>
          <button disabled={action || taskBusy || !historyDate} onClick={() => void perform("/settings", {history_start: dayRange(historyDate).start}, "PUT")}>保存起点</button>
          <h4>全量重建</h4><p>从统一起点重新拉取接口数据，成功后切换新库，保留旧库备份。</p>
          <button className="external-rebuild-button" disabled={action || taskBusy || stale || !status?.history_start} onClick={() => {setNotice(""); setRebuildOpen(true);}}>全量重建…</button>
          {notice && <p role="status">{notice}</p>}
        </>}

      </div>
    </aside>}
  </section>;
}
