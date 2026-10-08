import { useEffect, useState } from "react";
import { beijingTime, type TimeRange } from "./externalDeviceData";

export type Preparation = {ready: boolean; stage: string; processed: number};
export type EventBucket = {start: number; end: number; time: number; count: number; synthetic: boolean};
export type EventSeries = {preparation: Preparation; total: number; points: EventBucket[]};
export async function externalRequest<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch("/api/v1/external-devices" + path, {...options, headers: {"Content-Type": "application/json", ...options.headers}});
  const body = await response.json();
  if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : body.detail?.message ?? `HTTP ${response.status}`);
  return body as T;
}

type Field = {key: string; label: string; value: unknown; unit: string};
type Insight = {id: string; received_at: string; synthetic: boolean; valid?: boolean; message_type?: string;
  summary: {valid?: boolean; reason?: string; fields?: Field[]; label?: string; code?: string; note?: string}};
type Page = {items: Insight[]; total: number; incomplete: number; next_cursor: string | null; preparation: Preparation};

function RawRecord({device, id}: {device: string; id: string}) {
  const [open, setOpen] = useState(false);
  const [raw, setRaw] = useState<{record_json: string; raw_payload: string | null} | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    if (!open) return;
    const abort = new AbortController();
    externalRequest<{record_json: string; raw_payload: string | null}>(`/devices/${device}/records/${id}`, {signal: abort.signal})
      .then(setRaw).catch(reason => {if (!abort.signal.aborted) setError(String(reason));});
    return () => abort.abort();
  }, [device, id, open]);
  return <details className="external-original" onToggle={e => setOpen(e.currentTarget.open)}><summary>原始记录</summary>
    {error ? <p role="alert">{error}</p> : raw ? <><pre>{raw.record_json}</pre>{raw.raw_payload != null && <details><summary>rawPayload</summary><pre>{raw.raw_payload}</pre></details>}</> : <p>读取中…</p>}
  </details>;
}

function ReportDetail({device, item}: {device: string; item: Insight}) {
  const [report, setReport] = useState<Insight | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    const abort = new AbortController();
    externalRequest<Insight>(`/devices/${device}/reports/${item.id}`, {signal: abort.signal})
      .then(setReport).catch(reason => {if (!abort.signal.aborted) setError(String(reason));});
    return () => abort.abort();
  }, [device, item.id]);
  if (error) return <p role="alert">{error}</p>;
  if (!report) return <p>读取报告…</p>;
  const fields = report.summary.fields ?? [];
  const detailed = (field: Field) => (typeof field.value === "object" && field.value !== null) || String(field.value).length > 100;
  const highlights = ["sleep_time", "wake_time", "gotobed_time", "getoffbed_time", "sleep_duration", "bed_duration",
    "deep_duration", "light_duration", "wake_duration", "wake_num", "ave_hr", "ave_br", "sleep_score", "respiration_score"];
  const labels = ["入睡时间", "醒来时间", "上床时间", "离床时间", "睡眠时长", "在床时长", "深睡时长", "浅睡时长", "清醒时长", "醒来次数", "平均心率", "平均呼吸率", "睡眠评分", "呼吸评分"];
  const primary = highlights.map((key, index) => fields.find(field => field.key === key) ?? {key, label: labels[index], value: null, unit: "未确认"});
  const extra = fields.filter(field => !highlights.includes(field.key) && !detailed(field));
  const renderFields = (items: Field[]) => <dl className="external-report-values">{items.map(field => <div key={field.key}>
    <dt title={field.key}>{field.label}</dt><dd>{field.value == null ? "—" : String(field.value)}</dd><small>{field.key}</small>
  </div>)}</dl>;
  return <section className="external-report-detail">
    <p className="external-muted">以下保留上游原值；报告字段的单位、时区及枚举尚未确认。未提供的值显示为「—」。</p>
    {renderFields(primary)}
    <details><summary>其他报告字段（{extra.length} 项）</summary>{renderFields(extra)}</details>
    <details><summary>详细序列与分段（{fields.filter(detailed).length} 项）</summary>{fields.filter(detailed).map(field => <details key={field.key} className="external-original">
      <summary>{field.label} · {field.key}{Array.isArray(field.value) ? ` · ${field.value.length} 项` : ""}</summary><pre>{JSON.stringify(field.value, null, 2)}</pre>
    </details>)}</details>
    <RawRecord device={device} id={item.id} />
  </section>;
}

function InsightList({device, category, query = "", onIncomplete}: {device: string; category: "reports" | "events"; query?: string; onIncomplete?: (count: number) => void}) {
  const [page, setPage] = useState<Page | null>(null);
  const [error, setError] = useState("");
  const [cursor, setCursor] = useState("");
  const [loading, setLoading] = useState(false);
  const [selected, setSelected] = useState("");
  useEffect(() => {
    const abort = new AbortController(); let timer = 0;
    setLoading(true);
    const load = () => externalRequest<Page>(`/devices/${device}/${category === "reports" ? "reports" : "events/records"}?${query}&cursor=${encodeURIComponent(cursor)}`, {signal: abort.signal})
      .then(next => {
        if (abort.signal.aborted) return;
        setError(""); setPage(previous => ({...next, items: cursor ? [...(previous?.items ?? []), ...next.items] : next.items}));
        onIncomplete?.(next.incomplete);
        if (!next.preparation.ready) timer = window.setTimeout(load, 2000);
      }).catch(reason => {if (!abort.signal.aborted) setError(String(reason));})
      .finally(() => {if (!abort.signal.aborted) setLoading(false);});
    void load();
    return () => {abort.abort(); clearTimeout(timer);};
  }, [device, category, query, cursor]);
  return <>
    {error && <p role="alert">{error}</p>}
    {!page ? <p>读取中…</p> : !page.preparation.ready ? <p role="status">正在整理已有归档，完成后显示。</p> : <>
      {!page.items.length && <p className="external-muted">{category === "reports" ? "暂无此类睡眠报告" : "这个范围内暂无报警上报"}</p>}
      {page.items.map(item => <section key={item.id} className="external-insight-card">
        <button className="external-insight-heading" aria-expanded={selected === item.id} onClick={() => setSelected(previous => previous === item.id ? "" : item.id)}>
          <span>{selected === item.id ? "▾" : "▸"} {beijingTime(item.received_at)}</span>
          <small className={item.synthetic ? "synthetic-text" : ""}>{item.synthetic ? "合成 · " : ""}{category === "reports" ? (item.valid ? "睡眠报告" : item.summary.reason) : `${item.message_type} · ${item.summary.code ?? "未提供代码"}`}</small>
        </button>
        {selected === item.id && (category === "reports" ? <ReportDetail key={item.id} device={device} item={item} /> : <>
          <p>{item.summary.note}{item.summary.code ? `（${item.summary.code}）` : ""}。</p><RawRecord device={device} id={item.id} />
        </>)}
      </section>)}
      {page.next_cursor && <button disabled={loading} onClick={() => setCursor(page.next_cursor!)}>{loading ? "读取中…" : "加载更多"}</button>}
    </>}
  </>;
}

export function SleepReports({device}: {device: string}) {
  const [incomplete, setIncomplete] = useState(0);
  const [open, setOpen] = useState(false);
  return <><p className="external-muted">全部已归档报告，按接收时间从新到旧排列（北京时间）。</p>
    <InsightList device={device} category="reports" query="valid=true" onIncomplete={setIncomplete} />
    <details className="external-incomplete-reports" onToggle={event => setOpen(event.currentTarget.open)}>
      <summary>无效或不完整报告（{incomplete}）</summary>
      {open && <InsightList device={device} category="reports" query="valid=false" />}
    </details>
  </>;
}

export function DeviceEvents({device, range}: {device: string; range: TimeRange}) {
  return <><p className="external-muted">{beijingTime(range.start)} — {beijingTime(range.end)} · 北京时间</p>
    <p className="external-muted">时间采用接口接收时间。报警代码保留原值；合成上报单独标记。</p>
    <InsightList device={device} category="events" query={new URLSearchParams(range).toString()} />
  </>;
}

type RawField = {path: string; message: string; label: string; unit: string; count: number; statuses: Record<string, number>};
const statusNames: Record<string, string> = {projected: "已解析", missing: "空值", invalid: "格式未解析", raw: "保留原值", report: "报告原值", event: "事件原值"};
export function DeviceFields({device}: {device: string}) {
  const [result, setResult] = useState<{preparation: Preparation; fields: RawField[]} | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    const abort = new AbortController(); let timer = 0;
    const load = () => externalRequest<{preparation: Preparation; fields: RawField[]}>(`/devices/${device}/fields`, {signal: abort.signal})
      .then(value => {if (!abort.signal.aborted) {setResult(value); if (!value.preparation.ready) timer = window.setTimeout(load, 2000);}})
      .catch(reason => {if (!abort.signal.aborted) setError(String(reason));});
    void load(); return () => {abort.abort(); clearTimeout(timer);};
  }, [device]);
  return <>{error ? <p role="alert">{error}</p> : !result ? <p>读取中…</p> : !result.preparation.ready ? <p>正在整理已有归档…</p> : <>
    <p className="external-muted">按设备与消息类型统计当前采用的记录。未知字段保留原值，未推断单位或医学含义。</p>
    {[...new Set(result.fields.map(field => field.message))].map(message => <details key={message} open className="external-field-group"><summary>{message}</summary>
      {result.fields.filter(field => field.message === message).map(field => <div className="external-field" key={field.path}>
        <code>{field.path}</code><span>{field.label} · {field.unit}</span>
        <small>出现 {field.count.toLocaleString()} 次 · {Object.entries(field.statuses).map(([status, count]) => `${statusNames[status] ?? status} ${count.toLocaleString()}`).join(" / ")}</small>
      </div>)}
    </details>)}
  </>}</>;
}
