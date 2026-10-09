import { tr } from "./i18n";
import { externalLabel, externalUnit, formatNumber, countLabel, externalError, elapsed, taskLabel, type SyncTask, type Presentation } from "./externalDeviceText";
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
type Metric = Presentation & {key: string; method: string; first_record: string; last_record: string};
type Status = {sync_interval_s: number; worker_heartbeat: string | null; worker_online: boolean; generation: string; task: SyncTask | null;
  paused: boolean; error: string | null; last_discovery: string | null; history_start: string | null; history_start_source: string};
type ExportJob = {id: string; state: string; error: string | null; size_bytes: number | null};
type Row = {id: string; source_id: string; message_type: string; received_at: string; metrics: Record<string, number | null>; synthetic: boolean};
type Page = {records: Row[]; next_cursor: string | null; total: number};
type Detail = {record_json: string; raw_payload: string | null};
const presets: [TimePreset, string, string][] = [["1d", tr("近1日", "1 day"), tr("过去24小时", "Past 24 hours")], ["7d", tr("近1周", "1 week"), tr("过去7天", "Past 7 days")],
  ["30d", tr("近1月", "1 month"), tr("过去30天", "Past 30 days")], ["365d", tr("近1年", "1 year"), tr("过去365天", "Past 365 days")], ["all", tr("全部", "All"), tr("全部已归档时间", "All archived data")]];


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
        if (next.state === "failed") setError(externalError(next.error ?? tr("生成失败，请重试", "Could not prepare the download. Please retry.")));
      } catch (reason) {if (!abort.signal.aborted) setError(externalError(reason));}
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
    } catch (reason) {if (alive.current) setError(externalError(reason));}
    finally {lock.current = false; if (alive.current) setPending(false);}
  };
  const working = pending || job?.state === "pending" || job?.state === "running";
  const title = error ? tr(`${error}；点击重试`, `${error}; click to retry`) : working ? tr("正在准备全量下载…", "Preparing full download…") : device ? tr("下载此设备全部数据", "Download all data for this device") : tr("下载此类型全部设备数据", "Download all data for this device type");
  return <span className="external-download">
    <button className={error ? "download-error" : ""} aria-label={title} title={title} disabled={working} onClick={() => void start()}>{working ? "◌" : error ? tr("重试", "Retry") : "↓"}</button>
    {job?.state === "ready" && <a href={`${base}/exports/${job.id}/download`} download title={tr("下载包已就绪，点击再次下载", "Download ready; click to download again")} aria-label={tr("再次下载", "Download again")}>✓</a>}
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
      .catch(reason => {if (!abort.signal.aborted) setError(externalError(reason));});
    return () => abort.abort();
  }, [device, query]);
  useEffect(() => {
    setDetail(null);
    if (!selected) return;
    const abort = new AbortController();
    request<Detail>(`/devices/${device}/records/${selected}`, {signal: abort.signal})
      .then(value => {if (!abort.signal.aborted) setDetail(value);})
      .catch(reason => {if (!abort.signal.aborted) setError(externalError(reason));});
    return () => abort.abort();
  }, [device, selected]);
  const load = async () => {
    if (!page?.next_cursor || lock.current) return;
    lock.current = true; setMore(true);
    try {
      const next = await request<Page>(`/devices/${device}/records?${query}&cursor=${encodeURIComponent(page.next_cursor)}`, {signal: controller.current.signal});
      if (!controller.current.signal.aborted) setPage(old => ({...next, records: [...(old?.records ?? []), ...next.records]}));
    } catch (reason) {if (!controller.current.signal.aborted) setError(externalError(reason));}
    finally {lock.current = false; if (!controller.current.signal.aborted) setMore(false);}
  };
  return <><p className="external-muted">{beijingTime(range.start)} — {beijingTime(range.end)}</p>
    {error && <p role="alert">{error}</p>}
    {!page ? <p>{tr("正在读取记录…", "Loading records…")}</p> : <>
      <p>{countLabel(page.total, "条原始记录", "original record")}</p>
      <div className="external-record-list">{page.records.map(row => <button key={row.id} className={selected === row.id ? "active" : ""} onClick={() => setSelected(row.id)}>
        <span>{beijingTime(row.received_at)}</span><small><span data-no-localize>{row.message_type}</span>{row.synthetic ? tr(" · 合成", " · Synthetic") : ""} · <span data-no-localize>#{row.source_id}</span></small>
      </button>)}</div>
      {page.next_cursor && <button disabled={more} onClick={() => void load()}>{more ? tr("正在读取…", "Loading…") : tr("加载更多", "Load more")}</button>}
    </>}
    {detail && <section className="external-original"><h4>{tr("原始 JSON", "Original JSON")}</h4><pre>{detail.record_json}</pre>
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
  const eventOnlySeries: AggregateSeries = {metric: "events", label: tr("报警上报", "Reported alarms"), label_en: "Reported alarms", unit: "", unit_en: "", method: "events", interval_ms: 1, count: 0, points: [], coverage: {complete: true, gaps: []}};
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
      } catch (reason) {if (!abort.signal.aborted) setError(externalError(reason));}
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
      } catch (reason) {if (!abort.signal.aborted) setError(externalError(reason));}
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
      .catch(reason => {if (!abort.signal.aborted) setError(externalError(reason));});
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
      .catch(reason => {if (!abort.signal.aborted) setError(externalError(reason));})
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
      if (path === "/settings") setNotice(tr("历史起点已保存，用于新设备和下一次全量重建", "History start saved for new devices and the next full rebuild."));
      else setStatus(previous => previous ? {...previous, task: result} : previous);
      setRebuildOpen(false); setRefresh(n => n + 1);
    }
    catch (reason) {setNotice(externalError(reason));}
    finally {actionLock.current = false; setAction(false);}
  };
  const stale = !status?.worker_online;
  const syncLabel = status?.paused ? tr("同步暂停", "Sync paused") : stale ? tr("未连接同步服务", "Sync service offline") : status?.error ? tr("同步异常", "Sync error") : tr("同步正常", "Sync healthy");
  const openDetails = (tab: DrawerTab) => {setInspectRange(null); setDrawer(tab);};
  const renderDevice = (item: Device) => <div className={`external-device-row ${view.device === item.id ? "active" : ""}`} key={item.id}>
    <button className="external-device-select" onClick={() => {setZoomHistory([]); update({device: item.id, metric: ""});}} data-no-localize title={`${item.display_name}${item.product_key ? ` · ${item.product_key}` : ""}\n${tr("最新数据", "Latest data")} ${beijingTime(item.last_record)}`}>
      <strong>{item.device_no}</strong><small>{item.initial_sync ? `${countLabel(item.total, "条", "record")} · ${taskBusy ? tr("首次同步中", "Initial sync in progress") : tr("首次同步待完成", "Initial sync pending")}` : item.listed ? countLabel(item.total, "条记录", "record") : tr("已归档 · 不在上游列表", "Archived · No longer listed upstream")}</small><small className="external-device-last">{tr(`最近 ${beijingTime(item.last_record)}`, `Latest ${beijingTime(item.last_record)}`)}</small>
    </button><DownloadButton device={item.id} />
  </div>;
  return <section className="external-page">
    <aside className={`external-directory ${directoryOpen ? "open" : ""}`} aria-label={tr("设备列表", "Device list")}>
      <div className="external-directory-title"><strong>{tr("设备", "Devices")}</strong><small>{countLabel(devices.length, "台", "device")}</small><button className="external-mobile-only" onClick={() => setDirectoryOpen(false)}>{tr("关闭", "Close")}</button></div>
      <div className="external-directory-scroll">{(["radar-watch", "mattress"] as const).map(kind => {
        const group = devices.filter(item => item.kind === kind);
        const empty = group.filter(item => item.total === 0 && !item.initial_sync);
        return <section key={kind} className="external-device-group">
          <div className="external-group-title"><span>{kind === "radar-watch" ? tr("手表", "Watch") : tr("床垫", "Mattress")} <small>{group.length}</small></span><DownloadButton kind={kind} /></div>
          {group.filter(item => item.total > 0 || item.initial_sync).map(renderDevice)}
          {empty.length > 0 && <div className="external-empty-devices">
            <button className="external-empty-toggle" aria-expanded={emptyGroupsOpen[kind]} aria-controls={`external-empty-${kind}`}
              onClick={() => setEmptyGroupsOpen(previous => ({...previous, [kind]: !previous[kind]}))}>
              <span aria-hidden="true">{emptyGroupsOpen[kind] ? "▾" : "▸"}</span>{tr(`暂无数据（${empty.length}）`, `No data (${empty.length})`)}
            </button>
            <div id={`external-empty-${kind}`} hidden={!emptyGroupsOpen[kind]}>{empty.map(renderDevice)}</div>
          </div>}
        </section>;
      })}{!devices.length && <p className="external-muted">{tr("等待同步进程发现设备…", "Waiting for device discovery…")}</p>}</div>
      <button className={`external-sync-status ${stale || status?.error || status?.paused ? "warning" : ""}`} onClick={() => openDetails("status")}><i />{syncLabel}<span>{tr("详情 ›", "Details ›")}</span></button>
    </aside>
    {directoryOpen && <button className="external-directory-shade" aria-label={tr("关闭设备列表", "Close device list")} onClick={() => setDirectoryOpen(false)} />}
    <main className="external-workspace">
      <div className="external-toolbar">
        <div className="external-toolbar-top"><button className="external-mobile-only" onClick={() => setDirectoryOpen(true)}>{tr("设备 ☰", "Devices ☰")}</button>
          <div className="external-device-heading"><strong>{device?.device_no ?? tr("外部设备数据", "External device data")}</strong><span>{device?.kind === "radar-watch" ? tr("手表", "Watch") : device ? tr("床垫", "Mattress") : ""}</span></div>
          {isAdmin && <button className="external-refresh-button" disabled={action || taskBusy || stale} title={stale ? tr("请先启动已配置密钥的同步进程", "Start the sync worker with its API key configured.") : tr("更新全部设备：发现新设备并拉取新增数据", "Update all devices: discover new devices and fetch new records")} onClick={() => void perform("/sync")}>{taskBusy ? tr("处理中…", "Processing…") : tr("更新数据", "Update data")}</button>}
          {device?.kind === "mattress" && <button className="external-reports-button" onClick={() => openDetails("reports")}>{tr("睡眠报告", "Sleep reports")}</button>}
          <button className="external-details-button" onClick={() => openDetails(device ? "records" : isAdmin ? "manage" : "status")}>{tr("详情", "Details")}</button>
        </div>
        {(taskBusy || status?.task?.state === "failed" || notice || stale) && <div className="external-operation-line" role="status">
          {status?.task && status.task.state !== "done" && <button onClick={() => openDetails(isAdmin ? "manage" : "status")}>{taskLabel(status.task)}</button>}
          {notice && <span>{notice}</span>}{stale && <span>{tr("未连接同步服务", "Sync service offline")}</span>}
        </div>}
        <div className="external-metric-toolbar" aria-label={tr("指标选择", "Metric selection")}>
          <div className="external-quick-metrics">{(quickMetricKeys[device?.kind ?? ""] ?? []).map(key => metrics.find(item => item.key === key)).filter((item): item is Metric => !!item).map(item =>
            <button key={item.key} className={metric?.key === item.key ? "active" : ""} aria-pressed={metric?.key === item.key} onClick={() => chooseMetric(item.key)}>{externalLabel(item, item.key)}</button>)}</div>
          <label className="external-metric"><select aria-label={tr("更多指标", "More metrics")} title={metric ? `${externalLabel(metric, metric.key)} · ${externalUnit(metric)}` : tr("更多指标", "More metrics")} value={metric && !(quickMetricKeys[device?.kind ?? ""] ?? []).includes(metric.key) ? metric.key : ""} onChange={event => {if (event.target.value) chooseMetric(event.target.value);}} disabled={!metrics.length}>
            <option value="">{tr("更多指标", "More metrics")}</option>{metrics.filter(item => !(quickMetricKeys[device?.kind ?? ""] ?? []).includes(item.key)).map(item => <option key={item.key} value={item.key}>{externalLabel(item, item.key)} · {externalUnit(item)}</option>)}
          </select></label>
          <span className="external-metric-unit">{metric ? externalUnit(metric) : ""}</span>
          {metadata?.device === view.device && !metadata.preparation.ready && <small>{tr("正在整理新增指标…", "Preparing new metrics…")}</small>}
        </div>
        <div className="external-time-toolbar"><div className="external-presets">{presets.map(([key, label, title]) => <button key={key} title={title} className={view.preset === key ? "active" : ""} onClick={() => choosePreset(key)}>{label}</button>)}
          <button className={view.preset === "custom" ? "active" : ""} onClick={() => {setCustomStart(localTimeInput(range.start)); setCustomEnd(localTimeInput(range.end)); setCustomOpen(true);}}>{tr("自定义", "Custom")}</button>
        </div>{zoomHistory.length > 0 && <button className="external-zoom-back" onClick={() => {const previous = zoomHistory.at(-1)!; setZoomHistory(items => items.slice(0, -1)); update(previous);}}>{tr("← 返回上个范围", "← Previous range")}</button>}
        </div>
        <div className="external-latest"><span>{metric ? tr(`${externalLabel(metric, metric.key)}最近上报 · ${beijingTime(metric.last_record)}`, `Latest ${externalLabel(metric, metric.key)} · ${beijingTime(metric.last_record)}`) : tr(`设备最近上报 · ${beijingTime(device?.last_record)}`, `Latest device report · ${beijingTime(device?.last_record)}`)}</span>
          <button disabled={!metric} onClick={latest}>{tr("跳到最新数据", "Jump to latest")}</button>
          {!!events?.total && <button className="external-event-count" onClick={() => openDetails("events")}>△ {countLabel(events.total, "条报警上报", "reported alarm")}</button>}
        </div>
        <div className="external-range-summary"><span>{beijingTime(range.start)} — {beijingTime(range.end)} <small>{tr("北京时间", "Beijing time (UTC+8)")}</small></span>
          <span className="external-badges">{device && !device.history_complete && <button onClick={() => openDetails("status")}>{tr("历史未补齐", "History incomplete")}</button>}
            {series && !series.coverage.complete && <button onClick={() => openDetails("status")}>{tr("部分覆盖", "Partial coverage")}</button>}
            {device && device.synthetic_records > 0 && <button onClick={() => openDetails("status")}>{tr("含合成数据", "Includes synthetic data")}</button>}</span>
        </div>
      </div>
      <div className="external-plot-panel" ref={plotArea}>
        {error ? <div className="external-empty" role="alert"><strong>{tr("暂时无法读取数据", "Unable to load data")}</strong><p>{error}</p><button onClick={() => setRefresh(n => n + 1)}>{tr("重试", "Retry")}</button></div>
          : busy ? <div className="external-empty" role="status">{tr("正在读取趋势…", "Loading chart…")}</div>
          : (series && series.count > 0) || (events?.total ?? 0) > 0 ? <ExternalDeviceChart series={series ?? eventOnlySeries} range={range} onZoom={zoom} connectPoints={connectPoints}
              onConnectPointsChange={connected => {
                setConnectPoints(connected);
                try {localStorage.setItem(connectPointsPreference, String(connected));} catch { /* Keep the preference for this visit if storage is unavailable. */ }
              }}
              events={events?.points ?? noEvents} onInspectEvent={selectedRange => {setInspectRange(selectedRange); setDrawer("events");}}
              onInspect={selectedRange => {setInspectRange(selectedRange); setDrawer("records");}} />
          : <div className="external-empty"><span className="external-empty-icon">⌁</span><strong>{metadata?.ready === false ? tr("正在准备统计索引", "Preparing statistics") : tr("这个时间范围内暂无数据", "No data in this time range")}</strong>
            <p>{metric ? tr(`所选指标：${externalLabel(metric, metric.key)}。可以扩大范围，或定位最近一次记录。`, `Selected metric: ${externalLabel(metric, metric.key)}. Expand the range or jump to the latest data.`) : tr("设备有可视化指标后会显示在这里。", "Charts will appear when metrics are available for this device.")}</p>
            {metric && <button onClick={latest}>{tr("跳到最新数据", "Jump to latest")}</button>}</div>}
      </div>
    </main>
    {customOpen && <div className="external-modal-shade" onClick={() => setCustomOpen(false)}><form className="external-time-dialog" role="dialog" aria-modal="true" aria-label={tr("自定义时间范围", "Custom time range")} onClick={e => e.stopPropagation()} onSubmit={event => {
      event.preventDefault(); const start = new Date(`${customStart}+08:00`), end = new Date(`${customEnd}+08:00`);
      if (!Number.isFinite(+start) || !Number.isFinite(+end) || start >= end) {setNotice(tr("请输入开始早于结束的时间", "The start time must be earlier than the end time.")); return;}
      setNotice(""); setCustomOpen(false); zoom({start: start.toISOString(), end: end.toISOString()});
    }}><h3>{tr("自定义时间范围", "Custom time range")}</h3><label>{tr("开始（北京时间）", "Start (Beijing time, UTC+8)")}<input type="datetime-local" required value={customStart} onChange={e => setCustomStart(e.target.value)} /></label>
      <label>{tr("结束（不含该时刻）", "End (exclusive)")}<input type="datetime-local" required value={customEnd} onChange={e => setCustomEnd(e.target.value)} /></label>
      {notice && <p role="alert">{notice}</p>}<div><button type="button" onClick={() => setCustomOpen(false)}>{tr("取消", "Cancel")}</button><button className="primary" type="submit">{tr("应用范围", "Apply range")}</button></div>
    </form></div>}
    {rebuildOpen && <div className="external-modal-shade" onClick={() => {if (!action) setRebuildOpen(false);}}>
      <div className="external-time-dialog" role="dialog" aria-modal="true" aria-label={tr("确认全量重建", "Confirm full rebuild")} onClick={e => e.stopPropagation()}>
        <h3>{tr("确认全量重建", "Confirm full rebuild")}</h3>
        <p>{tr(`重新拉取全部设备从 ${beijingTime(status?.history_start)} 到发起时的当前时间之间的数据（北京时间）。`, `Fetch all device data again from ${beijingTime(status?.history_start)} to the time this rebuild starts (Beijing time, UTC+8).`)}</p>
        <p>{tr("校验成功后替换当前外部设备数据库，上游不再返回的记录将不在新库中显示。旧库和原始归档会保留，可恢复。", "After validation, the new database replaces the current external-device database. Records no longer returned upstream will be absent. The old database and original archives are retained for recovery.")}</p>
        <p>{tr("执行期间可以继续浏览旧数据；普通更新暂缓。失败时继续使用旧库。", "You can continue browsing existing data during the rebuild. Regular updates are paused. If rebuilding fails, the existing database stays active.")}</p>
        {notice && <p role="alert">{notice}</p>}
        <div><button disabled={action} onClick={() => setRebuildOpen(false)}>{tr("取消", "Cancel")}</button><button className="external-rebuild-button" disabled={action || taskBusy || stale} onClick={() => void perform("/rebuild", {confirmed: true, history_start: status?.history_start})}>{action ? tr("正在提交…", "Submitting…") : tr("确认重建", "Confirm rebuild")}</button></div>
      </div>
    </div>}
    {drawer && (device || drawer !== "records") && <aside className="external-detail-drawer" role="dialog" aria-label={tr("设备详情", "Device details")}>
      <div className="external-drawer-heading"><strong>{device?.device_no ?? tr("外部设备数据", "External device data")}</strong><button aria-label={tr("关闭详情", "Close details")} onClick={() => setDrawer(null)}>✕</button></div>
      <div className="external-drawer-tabs">{(["records", ...(device?.kind === "mattress" ? ["reports"] : []), "events", "fields", "status", ...(isAdmin ? ["manage"] : [])] as DrawerTab[]).map(tab => <button key={tab} className={drawer === tab ? "active" : ""} onClick={() => setDrawer(tab as typeof drawer)}>{({records: tr("记录", "Records"), reports: tr("睡眠", "Sleep"), events: tr("报警", "Alarms"), fields: tr("字段", "Fields"), status: tr("同步", "Sync"), manage: tr("管理", "Manage")})[tab]}</button>)}</div>
      <div className="external-drawer-scroll">
        {drawer === "records" && device && <RecordList key={`${status?.generation}:${device.id}:${(inspectRange ?? range).start}:${(inspectRange ?? range).end}`} device={device.id} range={inspectRange ?? range} />}
        {drawer === "reports" && device && <SleepReports key={`${status?.generation}:${device.id}`} device={device.id} />}
        {drawer === "events" && device && <DeviceEvents key={`${status?.generation}:${device.id}:${(inspectRange ?? range).start}:${(inspectRange ?? range).end}`} device={device.id} range={inspectRange ?? range} />}
        {drawer === "fields" && device && <DeviceFields key={`${status?.generation}:${device.id}`} device={device.id} />}
        {drawer === "status" && status?.task && <p>{taskLabel(status.task)}</p>}
        {drawer === "status" && device && <><dl className="external-facts">
          <dt>{tr("最新数据", "Latest data")}</dt><dd>{beijingTime(device.last_record)}</dd><dt>{tr("最近同步", "Last sync")}</dt><dd>{beijingTime(device.last_success)}</dd>
          <dt>{tr("增量连续覆盖至", "Incremental coverage through")}</dt><dd>{beijingTime(device.synced_until)}</dd><dt>{tr("设备身份", "Device identity")}</dt><dd data-no-localize>{device.product_key ? `${device.product_key} / ` : ""}{device.device_no}</dd>
          <dt>{tr("已归档记录", "Archived records")}</dt><dd>{countLabel(device.total, "条", "record")} · {countLabel(device.synthetic_records, "条合成", "synthetic record")}</dd></dl>
          <p>{!device.history_start ? tr("历史起点等待接口方确认，目前从设备首次发现开始增量同步。", "The history start awaits provider confirmation. Incremental sync currently starts when each device was first discovered.") : device.history_complete ? tr("已完成设备历史起点之后的首次同步。", "Initial sync from the device history start is complete.") : tr("历史补拉进行中。", "Historical data is being fetched.")}</p>
          <p>{syncLabel} · {tr(`设备目录更新于 ${beijingTime(status?.last_discovery)}`, `Device list updated ${beijingTime(status?.last_discovery)}`)}</p>
          {status?.error && <p role="alert">{externalError(status.error)}</p>}{device.last_error && <p role="alert">{externalError(device.last_error.error)}</p>}
          {series && <><h4>{tr("当前图表范围", "Current chart range")}</h4><p>{series.coverage.complete ? tr("已查询完整", "Entire range queried") : tr("尚有未覆盖区间", "Some intervals remain uncovered")}</p>{series.coverage.gaps.slice(0, 100).map(([start, end]) => <p key={start} className="external-muted">{beijingTime(start)} — {beijingTime(end)}</p>)}</>}
        </>}
        {drawer === "manage" && isAdmin && <><h4>{tr("更新全部设备", "Update all devices")}</h4>
          <p>{tr(`发现新设备并拉取历史；已有设备只新增记录。自动更新间隔 ${elapsed(status?.sync_interval_s ?? 900)}。`, `Discover new devices and fetch their history; only add new records for existing devices. Automatic updates run every ${elapsed(status?.sync_interval_s ?? 900)}.`)}</p>
          <button disabled={action || taskBusy || stale} onClick={() => void perform("/sync")}>{tr("更新数据", "Update data")}</button>
          {stale && <p>{tr("未连接同步服务，请先启动已配置密钥的同步进程。", "Sync service offline. Start the sync worker with its API key configured.")}</p>}
          {status?.task && <><p>{taskLabel(status.task)}</p><p className="external-muted">{tr(`查询区间 ${status.task.windows_done}/${status.task.windows_total} · 请求 ${status.task.request_count} 次 · 接收 ${formatNumber(status.task.response_bytes / 1024 / 1024)} MB`, `Intervals ${status.task.windows_done}/${status.task.windows_total} · ${countLabel(status.task.request_count, "次请求", "request")} · Received ${formatNumber(status.task.response_bytes / 1024 / 1024)} MB`)}</p>
            {status.task.finished_at && <p className="external-muted">{tr(`完成于 ${beijingTime(status.task.finished_at)}`, `Finished ${beijingTime(status.task.finished_at)}`)}</p>}</>}
          <h4>{tr("统一历史起点", "Shared history start")}</h4><p>{tr("用于新设备及下一次全量重建。现有设备的日常更新继续使用各自的同步进度。", "Used for new devices and the next full rebuild. Regular updates continue from each existing device’s sync progress.")}</p>
          <label>{tr("开始日期（北京时间）", "Start date (Beijing time, UTC+8)")}<input type="date" value={historyDate} max={beijingDay()} disabled={action || taskBusy} onChange={e => setHistoryDate(e.target.value)} /></label>
          <p className="external-muted">{status?.history_start_source === "provider" ? tr("接口方已确认", "Confirmed by the provider") : tr("用户设定的查询起点，不代表上游最早留存日期", "User-selected query start; this is not the earliest date retained upstream.")}</p>
          <button disabled={action || taskBusy || !historyDate} onClick={() => void perform("/settings", {history_start: dayRange(historyDate).start}, "PUT")}>{tr("保存起点", "Save start date")}</button>
          <h4>{tr("全量重建", "Full rebuild")}</h4><p>{tr("从统一起点重新拉取接口数据，成功后切换新库，保留旧库备份。", "Fetch API data again from the shared start date. Switch to the new database after success and keep a backup of the old one.")}</p>
          <button className="external-rebuild-button" disabled={action || taskBusy || stale || !status?.history_start} onClick={() => {setNotice(""); setRebuildOpen(true);}}>{tr("全量重建…", "Full rebuild…")}</button>
          {notice && <p role="status">{notice}</p>}
        </>}

      </div>
    </aside>}
  </section>;
}
