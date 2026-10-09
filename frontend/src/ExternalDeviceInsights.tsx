import { tr, isEnglish, apiErrorMessage } from "./i18n";
import { externalLabel, externalUnit, externalError, countLabel, formatNumber, type Presentation } from "./externalDeviceText";
import { useEffect, useState } from "react";
import { beijingTime, type TimeRange } from "./externalDeviceData";

export type Preparation = {ready: boolean; stage: string; processed: number};
export type EventBucket = {start: number; end: number; time: number; count: number; synthetic: boolean};
export type EventSeries = {preparation: Preparation; total: number; points: EventBucket[]};
export async function externalRequest<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch("/api/v1/external-devices" + path, {...options, headers: {"Content-Type": "application/json", ...options.headers}});
  const body = await response.json().catch(() => null);
  if (!response.ok) throw new Error(apiErrorMessage(body?.detail, response.status, response.statusText));
  if (body == null) throw new Error(tr("接口返回了无效响应，请重试", "The server returned an invalid response. Please retry."));
  return body as T;
}

type Field = Presentation & {key: string; value: unknown};
type Insight = {id: string; received_at: string; synthetic: boolean; valid?: boolean; message_type?: string;
  summary: {valid?: boolean; reason?: string; reason_en?: string; fields?: Field[]; label?: string; code?: string; note?: string; note_en?: string}};
type Page = {items: Insight[]; total: number; incomplete: number; next_cursor: string | null; preparation: Preparation};

function RawRecord({device, id}: {device: string; id: string}) {
  const [open, setOpen] = useState(false);
  const [raw, setRaw] = useState<{record_json: string; raw_payload: string | null} | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    if (!open) return;
    const abort = new AbortController();
    externalRequest<{record_json: string; raw_payload: string | null}>(`/devices/${device}/records/${id}`, {signal: abort.signal})
      .then(setRaw).catch(reason => {if (!abort.signal.aborted) setError(externalError(reason));});
    return () => abort.abort();
  }, [device, id, open]);
  return <details className="external-original" onToggle={e => setOpen(e.currentTarget.open)}><summary>{tr("原始记录", "Original record")}</summary>
    {error ? <p role="alert">{error}</p> : raw ? <><pre>{raw.record_json}</pre>{raw.raw_payload != null && <details><summary>rawPayload</summary><pre>{raw.raw_payload}</pre></details>}</> : <p>{tr("读取中…", "Loading…")}</p>}
  </details>;
}

function ReportDetail({device, item}: {device: string; item: Insight}) {
  const [report, setReport] = useState<Insight | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    const abort = new AbortController();
    externalRequest<Insight>(`/devices/${device}/reports/${item.id}`, {signal: abort.signal})
      .then(setReport).catch(reason => {if (!abort.signal.aborted) setError(externalError(reason));});
    return () => abort.abort();
  }, [device, item.id]);
  if (error) return <p role="alert">{error}</p>;
  if (!report) return <p>{tr("读取报告…", "Loading report…")}</p>;
  const fields = report.summary.fields ?? [];
  const detailed = (field: Field) => (typeof field.value === "object" && field.value !== null) || String(field.value).length > 100;
  const highlights = ["sleep_time", "wake_time", "gotobed_time", "getoffbed_time", "sleep_duration", "bed_duration",
    "deep_duration", "light_duration", "wake_duration", "wake_num", "ave_hr", "ave_br", "sleep_score", "respiration_score"];
  const labels = [tr("入睡时间", "Sleep onset"), tr("醒来时间", "Wake time"), tr("上床时间", "Bedtime"), tr("离床时间", "Time out of bed"), tr("睡眠时长", "Sleep duration"), tr("在床时长", "Time in bed"), tr("深睡时长", "Deep sleep duration"), tr("浅睡时长", "Light sleep duration"), tr("清醒时长", "Awake duration"), tr("醒来次数", "Awakenings"), tr("平均心率", "Average heart rate"), tr("平均呼吸率", "Average respiratory rate"), tr("睡眠评分", "Sleep score"), tr("呼吸评分", "Respiration score")];
  const primary = highlights.map((key, index) => fields.find(field => field.key === key) ?? {key, label: labels[index], label_en: labels[index], value: null, unit: tr("未确认", "Unconfirmed"), unit_en: "Unconfirmed"});
  const extra = fields.filter(field => !highlights.includes(field.key) && !detailed(field));
  const renderFields = (items: Field[]) => <dl className="external-report-values">{items.map(field => <div key={field.key}>
    <dt title={field.key} data-no-localize>{externalLabel(field, field.key)}</dt><dd data-no-localize>{field.value == null ? "—" : String(field.value)}</dd><small data-no-localize>{field.key}</small>
  </div>)}</dl>;
  return <section className="external-report-detail">
    <p className="external-muted">{tr("以下保留上游原值；报告字段的单位、时区及枚举尚未确认。未提供的值显示为「—」。", "Upstream values are preserved. Report units, timezone and codes are unconfirmed. Missing values appear as “—”.")}</p>
    {renderFields(primary)}
    <details><summary>{tr(`其他报告字段（${extra.length} 项）`, `Other report fields (${extra.length})`)}</summary>{renderFields(extra)}</details>
    <details><summary>{tr(`详细序列与分段（${fields.filter(detailed).length} 项）`, `Detailed series and segments (${fields.filter(detailed).length})`)}</summary>{fields.filter(detailed).map(field => <details key={field.key} className="external-original">
      <summary><span data-no-localize>{externalLabel(field, field.key)} · {field.key}</span>{Array.isArray(field.value) ? ` · ${countLabel(field.value.length, "项", "item")}` : ""}</summary><pre>{JSON.stringify(field.value, null, 2)}</pre>
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
      }).catch(reason => {if (!abort.signal.aborted) setError(externalError(reason));})
      .finally(() => {if (!abort.signal.aborted) setLoading(false);});
    void load();
    return () => {abort.abort(); clearTimeout(timer);};
  }, [device, category, query, cursor]);
  return <>
    {error && <p role="alert">{error}</p>}
    {!page ? <p>{tr("读取中…", "Loading…")}</p> : !page.preparation.ready ? <p role="status">{tr("正在整理已有归档，完成后显示。", "Preparing existing archives. Results will appear when ready.")}</p> : <>
      {!page.items.length && <p className="external-muted">{category === "reports" ? tr("暂无此类睡眠报告", "No sleep reports in this category") : tr("这个范围内暂无报警上报", "No reported alarms in this range")}</p>}
      {page.items.map(item => <section key={item.id} className="external-insight-card">
        <button className="external-insight-heading" aria-expanded={selected === item.id} onClick={() => setSelected(previous => previous === item.id ? "" : item.id)}>
          <span>{selected === item.id ? "▾" : "▸"} {beijingTime(item.received_at)}</span>
          <small className={item.synthetic ? "synthetic-text" : ""}>{item.synthetic ? tr("合成 · ", "Synthetic · ") : ""}{category === "reports" ? (item.valid ? tr("睡眠报告", "Sleep report") : isEnglish ? item.summary.reason_en ?? "Invalid or incomplete report" : item.summary.reason) : <><span data-no-localize>{item.message_type} · {item.summary.code ?? tr("未提供代码", "No code supplied")}</span></>}</small>
        </button>
        {selected === item.id && (category === "reports" ? <ReportDetail key={item.id} device={device} item={item} /> : <>
          <p>{isEnglish ? item.summary.note_en ?? "Original upstream code; event meaning is unconfirmed" : item.summary.note}{item.summary.code && <span data-no-localize> ({item.summary.code})</span>}{tr("。", ".")}</p><RawRecord device={device} id={item.id} />
        </>)}
      </section>)}
      {page.next_cursor && <button disabled={loading} onClick={() => setCursor(page.next_cursor!)}>{loading ? tr("读取中…", "Loading…") : tr("加载更多", "Load more")}</button>}
    </>}
  </>;
}

export function SleepReports({device}: {device: string}) {
  const [incomplete, setIncomplete] = useState(0);
  const [open, setOpen] = useState(false);
  return <><p className="external-muted">{tr("全部已归档报告，按接收时间从新到旧排列（北京时间）。", "All archived reports, newest received first (Beijing time, UTC+8).")}</p>
    <InsightList device={device} category="reports" query="valid=true" onIncomplete={setIncomplete} />
    <details className="external-incomplete-reports" onToggle={event => setOpen(event.currentTarget.open)}>
      <summary>{tr(`无效或不完整报告（${incomplete}）`, `Invalid or incomplete reports (${incomplete})`)}</summary>
      {open && <InsightList device={device} category="reports" query="valid=false" />}
    </details>
  </>;
}

export function DeviceEvents({device, range}: {device: string; range: TimeRange}) {
  return <><p className="external-muted">{beijingTime(range.start)} — {beijingTime(range.end)} · {tr("北京时间", "Beijing time (UTC+8)")}</p>
    <p className="external-muted">{tr("时间采用接口接收时间。报警代码保留原值；合成上报单独标记。", "Times are API receipt times. Alarm codes are preserved; synthetic reports are marked separately.")}</p>
    <InsightList device={device} category="events" query={new URLSearchParams(range).toString()} />
  </>;
}

type RawField = Presentation & {path: string; message: string; count: number; statuses: Record<string, number>};
const statusNames: Record<string, string> = {projected: tr("已解析", "Parsed"), missing: tr("空值", "Missing value"), invalid: tr("格式未解析", "Unparsed format"), raw: tr("保留原值", "Raw value retained"), report: tr("报告原值", "Raw report value"), event: tr("事件原值", "Raw event value")};
export function DeviceFields({device}: {device: string}) {
  const [result, setResult] = useState<{preparation: Preparation; fields: RawField[]} | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    const abort = new AbortController(); let timer = 0;
    const load = () => externalRequest<{preparation: Preparation; fields: RawField[]}>(`/devices/${device}/fields`, {signal: abort.signal})
      .then(value => {if (!abort.signal.aborted) {setResult(value); if (!value.preparation.ready) timer = window.setTimeout(load, 2000);}})
      .catch(reason => {if (!abort.signal.aborted) setError(externalError(reason));});
    void load(); return () => {abort.abort(); clearTimeout(timer);};
  }, [device]);
  return <>{error ? <p role="alert">{error}</p> : !result ? <p>{tr("读取中…", "Loading…")}</p> : !result.preparation.ready ? <p>{tr("正在整理已有归档…", "Preparing existing archives…")}</p> : <>
    <p className="external-muted">{tr("按设备与消息类型统计当前采用的记录。未知字段保留原值，未推断单位或医学含义。", "Counts cover the currently accepted records by device and message type. Unknown fields retain their original values; units and medical meaning are not inferred.")}</p>
    {[...new Set(result.fields.map(field => field.message))].map(message => <details key={message} open className="external-field-group"><summary data-no-localize>{message}</summary>
      {result.fields.filter(field => field.message === message).map(field => <div className="external-field" key={field.path}>
        <code>{field.path}</code><span><span data-no-localize>{externalLabel(field, field.path)}</span> · {externalUnit(field)}</span>
        <small>{tr(`出现 ${formatNumber(field.count)} 次`, `${countLabel(field.count, "次", "occurrence")}`)} · {Object.entries(field.statuses).map(([status, count]) => `${statusNames[status] ?? status} ${formatNumber(count)}`).join(" / ")}</small>
      </div>)}
    </details>)}
  </>}</>;
}
