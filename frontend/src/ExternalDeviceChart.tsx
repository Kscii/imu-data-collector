import { tr, uiLanguage, isEnglish } from "./i18n";
import { externalLabel, externalUnit, countLabel, formatNumber, stateNames, stateName } from "./externalDeviceText";
import { useEffect, useRef, useState } from "react";
import uPlot from "uplot";
import type { EventBucket } from "./ExternalDeviceInsights";
import { aggregationLabel, beijingTime, durationLabel, type AggregatePoint, type AggregateSeries, type TimeRange } from "./externalDeviceData";

const number = (value: number | null) => value == null ? "—" : formatNumber(value);

export function ExternalDeviceChart({series, range, connectPoints, onConnectPointsChange, onZoom, onInspect, events, onInspectEvent}: {
  series: AggregateSeries; range: TimeRange; onZoom: (range: TimeRange) => void;
  connectPoints: boolean; onConnectPointsChange: (connected: boolean) => void;
  onInspect: (range: TimeRange) => void;
  events: EventBucket[]; onInspectEvent: (range: TimeRange) => void;
}) {
  const host = useRef<HTMLDivElement>(null);
  const callbacks = useRef({onZoom, onInspect, onInspectEvent});
  callbacks.current = {onZoom, onInspect, onInspectEvent};
  const [hover, setHover] = useState<AggregatePoint[]>([]);
  useEffect(() => {
    if (!host.current) return;
    const container = host.current;
    setHover([]);
    const points = series.points.filter(point => !point.synthetic);
    const synthetic = series.points.filter(point => point.synthetic);
    const data: uPlot.AlignedData = [points.map(p => p.time / 1000),
      points.map(p => p.value), points.map(p => p.min), points.map(p => p.max),
      synthetic.map(p => p.value), synthetic.map(p => p.min), synthetic.map(p => p.max)];
    const path = series.method === "mode" ? uPlot.paths.stepped?.({align: 1}) : undefined;
    const noPath = () => null;
    const colors = ["#60a5fa", "#fbbf24"];
    const opts: uPlot.Options = {
      width: Math.max(200, container.clientWidth), height: Math.max(180, container.clientHeight),
      padding: [18, 20, 0, 0], legend: {show: false},
      cursor: {drag: {x: true, y: false, setScale: false}},
      scales: {x: {time: true, range: () => [Date.parse(range.start) / 1000, Date.parse(range.end) / 1000]},
        ...(series.metric === "events" ? {y: {range: () => [0, 1] as [number, number]}} : {})},
      axes: [{stroke: "#93a6bf", grid: {stroke: "#1b2b40"}, space: 110, size: 58,
        values: (_u, ticks) => ticks.map(t => {
          const date = new Date(t * 1000);
          return date.toLocaleDateString(uiLanguage, {timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit"})
            + "\n" + date.toLocaleTimeString(uiLanguage, {timeZone: "Asia/Shanghai", hourCycle: "h23", hour: "2-digit", minute: "2-digit"});
        })}, {stroke: "#93a6bf", grid: {stroke: "#1b2b40"}, size: stateNames[series.metric] ? (isEnglish ? 114 : 78) : 58,
        values: (_u, ticks) => ticks.map(value => stateNames[series.metric]
          ? (Number.isInteger(value) ? stateName(series.metric, String(value)) : "") : number(value))}],
      series: [{}, ...colors.flatMap(color => [
        {stroke: color, width: 1.5, spanGaps: true, paths: connectPoints ? path : noPath,
          points: {show: true, size: 6, width: 1, stroke: "#0c192a", fill: color}},
        {stroke: color + "66", width: 0.5, spanGaps: false, paths: connectPoints ? undefined : noPath,
          points: {show: false}, show: series.method === "mean"},
        {stroke: color + "66", width: 0.5, spanGaps: false, paths: connectPoints ? undefined : noPath,
          points: {show: false}, show: series.method === "mean"},
      ])],
      bands: connectPoints && series.method === "mean" ? [{series: [3, 2], fill: "#60a5fa22"}, {series: [6, 5], fill: "#fbbf2422"}] : [],
      hooks: {
        setCursor: [(u) => {
          const i = u.cursor.idx;
          setHover(i == null ? [] : [points[i], synthetic[i]].filter(p => p?.count));
        }],
        setSelect: [(u) => {
          if (u.select.width < 6) return;
          const start = Math.max(Date.parse(range.start), Math.floor(u.posToVal(u.select.left, "x") * 1000));
          const end = Math.min(Date.parse(range.end), Math.ceil(u.posToVal(u.select.left + u.select.width, "x") * 1000));
          if (end > start) callbacks.current.onZoom({start: new Date(start).toISOString(), end: new Date(end).toISOString()});
          u.setSelect({left: 0, top: 0, width: 0, height: 0}, false);
        }],
      },
    };
    const plot = new uPlot(opts, data, container);
    const markers = events.map(bucket => {
      const button = document.createElement("button");
      button.className = `external-event-marker${bucket.synthetic ? " synthetic" : ""}`;
      button.textContent = bucket.count > 1 ? `△${bucket.count}` : "△";
      button.title = `${beijingTime(bucket.start)} · ${bucket.synthetic ? tr("合成 · ", "Synthetic · ") : ""}${countLabel(bucket.count, "条报警上报", "reported alarm")}; ${tr("点击查看", "Click for details")}`;
      button.setAttribute("aria-label", button.title);
      button.addEventListener("pointerdown", event => event.stopPropagation());
      button.addEventListener("mousedown", event => event.stopPropagation());
      button.addEventListener("click", event => {
        event.stopPropagation();
        callbacks.current.onInspectEvent({start: new Date(bucket.start).toISOString(), end: new Date(bucket.end).toISOString()});
      });
      plot.over.append(button);
      return {button, bucket};
    });
    const positionMarkers = () => markers.forEach(({button, bucket}) => {
      button.style.left = `${Math.max(12, Math.min(plot.over.clientWidth - 12, plot.valToPos(bucket.time / 1000, "x")))}px`;
    });
    positionMarkers();
    let down = 0;
    const press = (event: PointerEvent) => {down = event.clientX;};
    const click = (event: MouseEvent) => {
      if (Math.abs(event.clientX - down) >= 6 || plot.cursor.idx == null) return;
      const p = points[plot.cursor.idx], s = synthetic[plot.cursor.idx];
      if (p?.count || s?.count) callbacks.current.onInspect({start: new Date(p.start).toISOString(), end: new Date(p.end).toISOString()});
    };
    plot.over.addEventListener("pointerdown", press);
    plot.over.addEventListener("click", click);
    const resize = new ResizeObserver(() => {
      plot.setSize({width: Math.max(200, container.clientWidth), height: Math.max(180, container.clientHeight)});
      positionMarkers();
    });
    resize.observe(container);
    return () => {resize.disconnect(); plot.over.removeEventListener("pointerdown", press); plot.over.removeEventListener("click", click); plot.destroy();};
  }, [series, range.start, range.end, connectPoints, events]);
  return <div className="external-chart-wrap">
    <div className="external-chart-note"><span>{series.metric === "events" ? tr("报警上报 · 点击标记查看详情", "Reported alarms · Click a marker for details") : <>{!connectPoints && series.method === "mean" ? tr("平均值", "Mean") : aggregationLabel(series.method)} · {tr(`每 ${durationLabel(series.interval_ms)} 聚合`, `Aggregated every ${durationLabel(series.interval_ms)}`)}</>}</span>
      <div className="external-chart-controls">
        <label className="external-connect-points" title={tr("跨空白时段连接有效数据点，仅影响显示", "Connect valid points across gaps; display only")}>
          <input type="checkbox" checked={connectPoints} onChange={event => onConnectPointsChange(event.target.checked)} />{tr("连接数据点", "Connect points")}
        </label>
        <span><i className="external-dot" />{tr("未标记合成", "Not marked synthetic")} <i className="external-dot synthetic" />{tr("合成", "Synthetic")}</span>
      </div></div>
    <div ref={host} className="external-chart" aria-label={`${externalLabel(series, series.metric)} ${externalUnit(series)}`} />
    <div className="external-chart-readout" aria-live="off">{hover.length ? <>
      <span>{beijingTime(hover[0].start)} — {beijingTime(hover[0].end)}</span>
      {hover.map(p => <span key={String(p.synthetic)} className={p.synthetic ? "synthetic-text" : ""}>
        {p.synthetic ? tr("合成 · ", "Synthetic · ") : ""}<strong>{series.method === "mode" ? stateName(series.metric, String(p.value)) : number(p.value)}</strong>
        {series.method === "mode" ? " · " + Object.entries(p.states).map(([state, count]) => `${stateName(series.metric, state)} ${formatNumber(count / p.count * 100, 0)}%`).join(" / ")
          : tr(` · 均值 ${number(p.mean)} · 范围 ${number(p.min)}–${number(p.max)}`, ` · Mean ${number(p.mean)} · Range ${number(p.min)}–${number(p.max)}`)}
        {` · ${countLabel(p.count, "个样本", "sample")}`}{p.missing ? ` · ${countLabel(p.missing, "个缺失值", "missing value")}` : ""}
      </span>)}
    </> : <span>{tr("悬停查看统计 · 横向拖选放大 · 点击查看该时间段原始记录", "Hover for statistics · Drag horizontally to zoom · Click for original records")}</span>}</div>
  </div>;
}
