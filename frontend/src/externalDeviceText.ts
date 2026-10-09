import { isEnglish, tr, uiLanguage, userVisibleMessage } from "./i18n.ts";

export type Presentation = {label: string; label_en?: string; unit: string; unit_en?: string};

/** Select presentation metadata only. Source values must never pass through this helper. */
export function externalLabel(value: {label: string; label_en?: string}, fallback: string): string {
  return isEnglish ? value.label_en ?? fallback : value.label;
}
export function externalUnit(value: {unit: string; unit_en?: string}): string {
  return isEnglish ? value.unit_en ?? tr("未确认", "Unconfirmed") : value.unit;
}
export const formatNumber = (value: number, maximumFractionDigits = 2) =>
  value.toLocaleString(uiLanguage, {maximumFractionDigits});

export function countLabel(value: number, chinese: string, singular: string, plural = singular + "s"): string {
  return tr(`${formatNumber(value)} ${chinese}`, `${formatNumber(value)} ${value === 1 ? singular : plural}`);
}

export function externalError(error: unknown): string {
  return userVisibleMessage(error instanceof Error ? error.message : error);
}

export const stateNames: Record<string, Record<string, string>> = {
  in_bed: {"0": tr("离床", "Out of bed"), "1": tr("在床", "In bed")},
  sleep_stage: {"0": tr("初始化", "Initializing"), "1": tr("清醒", "Awake"), "2": "REM", "3": tr("浅睡", "Light sleep"), "4": tr("深睡", "Deep sleep")},
  moving: {"0": tr("无体动", "No movement"), "1": tr("小体动", "Low movement"), "2": tr("大体动", "High movement")},
  CO: {"0": tr("未充电", "Not charging"), "1": tr("充电中", "Charging"), "2": tr("充电完成", "Fully charged")},
};
export function stateName(metric: string, value: string): string {
  return stateNames[metric]?.[String(Number(value))] ?? (stateNames[metric] ? tr(`未知 (${value})`, `Unknown (${value})`) : value);
}

export function elapsed(seconds: number): string {
  return seconds >= 60
    ? tr(`${Math.floor(seconds / 60)} 分 ${Math.floor(seconds % 60)} 秒`, `${Math.floor(seconds / 60)} min ${Math.floor(seconds % 60)} s`)
    : tr(`${Math.floor(seconds)} 秒`, `${Math.floor(seconds)} s`);
}

export type SyncTask = {id: string; mode: "sync" | "rebuild"; state: "pending" | "running" | "done" | "failed";
  phase: string; devices_done: number; devices_total: number; windows_done: number; windows_total: number;
  elapsed_s: number; new_records: number; request_count: number; response_bytes: number; error: string | null; finished_at: string | null};
export function taskLabel(task: SyncTask): string {
  const name = task.mode === "rebuild" ? tr("重建", "Rebuild") : tr("更新", "Update");
  if (task.state === "failed") return tr(`${name}失败 · ${externalError(task.error ?? "请重试")}`, `${name} failed · ${externalError(task.error ?? "Please retry")}`);
  if (task.state === "done") return tr(
    `${name}完成 · ${task.mode === "rebuild" ? "入库" : "新增"} ${formatNumber(task.new_records)} 条 · ${elapsed(task.elapsed_s)}`,
    `${name} complete · ${countLabel(task.new_records, "条", "record")} ${task.mode === "rebuild" ? "imported" : "added"} · ${elapsed(task.elapsed_s)}`,
  );
  const phase = ({queued: tr("等待执行", "Queued"), discovering: tr("刷新设备列表", "Refreshing device list"),
    fetching: tr(`拉取数据 · ${task.devices_done}/${task.devices_total} 台 · ${task.windows_done}/${task.windows_total} 段`,
      `Fetching data · ${task.devices_done}/${task.devices_total} devices · ${task.windows_done}/${task.windows_total} intervals`),
    publishing: tr("校验并切换", "Validating and publishing")} as Record<string, string>)[task.phase] ?? tr("处理中", "Processing");
  return tr(`${name}中 · ${phase} · ${elapsed(task.elapsed_s)}`, `${name} in progress · ${phase} · ${elapsed(task.elapsed_s)}`);
}
