import { tr, uiLanguage } from "./i18n.ts";
import { countLabel, type Presentation } from "./externalDeviceText.ts";
export type ExternalPoint = [number, number | null, boolean];

export function beijingDay(value = new Date()): string {
  return new Date(value.getTime() + 8 * 3600_000).toISOString().slice(0, 10);
}

export function dayRange(day: string): {start: string; end: string} {
  const start = new Date(`${day}T00:00:00+08:00`);
  if (!Number.isFinite(start.getTime())) throw new Error("Invalid date");
  return {start: start.toISOString(), end: new Date(start.getTime() + 86400_000).toISOString()};
}

export function chartColumns(points: ExternalPoint[]): [number[], (number | null)[], (number | null)[]] {
  return [points.map(p => p[0] / 1000), points.map(p => p[2] ? null : p[1]),
    points.map(p => p[2] ? p[1] : null)];
}

export function beijingTime(value: string | number | null | undefined): string {
  if (value == null) return "—";
  return new Date(value).toLocaleString(uiLanguage, {timeZone: "Asia/Shanghai", hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit"});
}

export function coverageLabel(state: string, count: number): string {
  if (state === "complete") return count ? tr("已同步", "Synced") : tr("已查询，无记录", "Queried; no records");
  if (state === "partial") return tr("部分覆盖", "Partial coverage");
  if (state === "failed") return tr("有同步失败区间", "Some intervals failed to sync");
  if (state === "future") return tr("未来日期", "Future date");
  return tr("尚未查询", "Not yet queried");
}

export type TimePreset = "1d" | "7d" | "30d" | "365d" | "all" | "custom";
export type ExternalView = {device: string; metric: string; preset: TimePreset; from?: string; to?: string};
export type TimeRange = {start: string; end: string};
export type AggregatePoint = {start: number; end: number; time: number; value: number | null;
  synthetic: boolean; count: number; missing: number; min: number | null; max: number | null;
  mean: number | null; states: Record<string, number>};
export type AggregateSeries = Presentation & {metric: string; method: string;
  interval_ms: number; count: number; points: AggregatePoint[];
  coverage: {complete: boolean; gaps: string[][]}};

export function readExternalView(search: string): ExternalView {
  const params = new URLSearchParams(search);
  let preset = params.get("range") as TimePreset;
  if (!["1d", "7d", "30d", "365d", "all", "custom"].includes(preset)) preset = "7d";
  const from = params.get("from") ?? undefined, to = params.get("to") ?? undefined;
  if (preset === "custom" && (!from || !to || !Number.isFinite(Date.parse(from))
    || !Number.isFinite(Date.parse(to)) || Date.parse(from) >= Date.parse(to))) preset = "7d";
  return {device: params.get("device") ?? "", metric: params.get("metric") ?? "", preset,
    ...(preset === "custom" ? {from, to} : {})};
}

export function externalViewUrl(current: string, view: ExternalView): URL {
  const url = new URL(current);
  url.searchParams.set("view", "external");
  for (const key of ["device", "metric"] as const) {
    if (view[key]) url.searchParams.set(key, view[key]); else url.searchParams.delete(key);
  }
  url.searchParams.set("range", view.preset);
  for (const [key, value] of [["from", view.from], ["to", view.to]]) {
    if (view.preset === "custom" && value) url.searchParams.set(key!, value); else url.searchParams.delete(key!);
  }
  return url;
}

export function resolveExternalRange(view: ExternalView, clock: number, first?: string | null): TimeRange {
  if (view.preset === "custom" && view.from && view.to) return {start: view.from, end: view.to};
  const days = {"1d": 1, "7d": 7, "30d": 30, "365d": 365, all: 7, custom: 7}[view.preset];
  const start = view.preset === "all" && first ? Date.parse(first) : clock - days * 86400_000;
  return {start: new Date(start).toISOString(), end: new Date(Math.max(clock, start + 1)).toISOString()};
}

export function localTimeInput(value: string): string {
  return new Date(Date.parse(value) + 8 * 3600_000).toISOString().slice(0, 16);
}

export function aggregationLabel(method: string): string {
  return ({mean: tr("平均值 · 阴影为最小/最大值", "Mean · Shading shows min/max"), mode: tr("主要状态 · 悬停查看样本占比", "Most frequent state · Hover for sample proportions"),
    last: tr("时间段末值", "Last value in interval"), sum: tr("时间段合计", "Interval total")} as Record<string,string>)[method] ?? method;
}

export function durationLabel(ms: number): string {
  if (ms >= 86400_000) return countLabel(ms / 86400_000, "天", "day");
  if (ms >= 3600_000) return countLabel(ms / 3600_000, "小时", "hour");
  if (ms >= 60000) return countLabel(ms / 60000, "分钟", "minute");
  return countLabel(ms / 1000, "秒", "second");
}

export const quickMetricKeys: Record<string, string[]> = {
  mattress: ["HeartRate", "RespiratoryRate", "in_bed", "sleep_stage"],
  "radar-watch": ["HR", "SPO", "ST", "energy_kcal"],
};
export const metricPreferenceKey = (kind: string) => `imu-external-metric-v1-${kind}`;
export function chooseExternalMetric(kind: string, available: string[], explicit: string, remembered: string): string {
  const aliases: Record<string, string> = {HR: "HeartRate", HeartRate: "HR", BRR: "RespiratoryRate", RespiratoryRate: "BRR"};
  return [explicit, remembered, aliases[explicit], ...(quickMetricKeys[kind] ?? []), ...available]
    .find(key => key && available.includes(key)) ?? "";
}
export function latestMetricRange(last: string): TimeRange {
  const end = Date.parse(last) + 1;
  return {start: new Date(end - 86400_000).toISOString(), end: new Date(end).toISOString()};
}
