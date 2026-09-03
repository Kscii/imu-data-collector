import { useEffect, useRef } from "react";
import uPlot from "uplot";
import { tr } from "./i18n";

const defaultColors = ["#ef4444", "#22c55e", "#3b82f6", "#f59e0b", "#a855f7", "#06b6d4"];
const defaultLabels = ["ax", "ay", "az", "gx", "gy", "gz"];
const noMarkers: PlotMarker[] = [];
const noRegions: PlotRegion[] = [];

type Props = {
  time: number[];
  values: number[][];
  cursorTime?: number;
  markers?: PlotMarker[];
  regions?: PlotRegion[];
  selectionLabels?: PlotSelectionLabel[];
  controlledCursor?: boolean;
  showMarkerKey?: boolean;
  height?: number;
  seriesLabels?: string[];
  seriesColors?: string[];
  showReadout?: boolean;
  onSelectTime?: (time: number) => void;
  onSelectLabel?: (key: string) => void;
};

export type PlotSelectionLabel = {
  key: string;
  label: string;
  color?: string;
};

export type PlotMarker = {
  time: number;
  label: string;
  color: string;
  dashed?: boolean;
  showPoints?: boolean;
};

export type PlotRegion = {
  start: number;
  end: number;
  color: string;
  borderColor?: string;
  label?: string;
};

function nearestIndex(values: number[], target: number) {
  if (!values.length) return -1;
  let left = 0;
  let right = values.length;
  while (left < right) {
    const middle = Math.floor((left + right) / 2);
    if (values[middle] < target) left = middle + 1;
    else right = middle;
  }
  if (left === 0) return 0;
  if (left === values.length) return values.length - 1;
  return target - values[left - 1] <= values[left] - target ? left - 1 : left;
}

function alignedData(time: number[], values: number[][], axisCount: number): uPlot.AlignedData {
  const columns = Array.from({ length: axisCount }, (_, axis) =>
    values.map((row) => row[axis] ?? 0)
  );
  return [time, ...columns];
}

function fitTimeRange(plot: uPlot, time: number[]) {
  if (!time.length) return;
  const first = time[0];
  const last = time[time.length - 1];
  if (!Number.isFinite(first) || !Number.isFinite(last)) return;
  const padding = first === last ? Math.max(Math.abs(first) * 0.01, 0.5) : 0;
  plot.setScale("x", { min: first - padding, max: last + padding });
}

function fitValueRange(plot: uPlot, values: number[][], axisCount: number) {
  const finite = values.flatMap((row) => row.slice(0, axisCount)).filter(Number.isFinite);
  if (!finite.length) return;
  const minimum = Math.min(...finite);
  const maximum = Math.max(...finite);
  const span = maximum - minimum;
  const padding = span > 0 ? span * 0.08 : Math.max(Math.abs(maximum) * 0.05, 1);
  plot.setScale("y", { min: minimum - padding, max: maximum + padding });
}

function formatPlotValue(value: number | undefined) {
  if (value === undefined || !Number.isFinite(value)) return "—";
  if (Number.isInteger(value) && Math.abs(value) >= 100) return value.toLocaleString();
  return value.toFixed(3).replace(/\.000$/, "").replace(/(\.\d*?)0+$/, "$1");
}

export default function Plot({ time, values, cursorTime, markers = noMarkers, regions = noRegions, selectionLabels = [], controlledCursor = false, showMarkerKey = true, height = 290, seriesLabels = defaultLabels, seriesColors = defaultColors, showReadout = true, onSelectTime, onSelectLabel }: Props) {
  const host = useRef<HTMLDivElement>(null);
  const plot = useRef<uPlot | null>(null);
  const onSelectTimeRef = useRef(onSelectTime);
  const timeRef = useRef(time);
  const valuesRef = useRef(values);
  const markersRef = useRef(markers);
  const regionsRef = useRef(regions);
  const cursorTimeRef = useRef(cursorTime);
  const colorsRef = useRef(seriesColors);

  timeRef.current = time;
  valuesRef.current = values;
  markersRef.current = markers;
  regionsRef.current = regions;
  cursorTimeRef.current = cursorTime;
  colorsRef.current = seriesColors;

  useEffect(() => {
    onSelectTimeRef.current = onSelectTime;
  }, [onSelectTime]);

  useEffect(() => {
    if (!host.current) return;
    const width = Math.max(480, host.current.clientWidth);
    plot.current = new uPlot(
      {
        width,
        height,
        cursor: { show: !controlledCursor, drag: { x: false, y: false } },
        legend: { show: !controlledCursor },
        scales: { x: { time: false } },
        hooks: {
          draw: [
            (u: uPlot) => {
              const currentTime = timeRef.current;
              const currentValues = valuesRef.current;
              if (!currentTime.length) return;
              const { ctx, bbox } = u;
              const pixelRatio = uPlot.pxRatio;
              ctx.save();
              for (const region of regionsRef.current) {
                if (region.end < currentTime[0] || region.start > currentTime[currentTime.length - 1]) continue;
                const start = Math.max(region.start, currentTime[0]);
                const end = Math.min(region.end, currentTime[currentTime.length - 1]);
                const left = u.valToPos(start, "x", true);
                const right = u.valToPos(end, "x", true);
                ctx.fillStyle = region.color;
                ctx.fillRect(left, bbox.top, Math.max(1, right - left), bbox.height);
                if (region.borderColor) {
                  ctx.fillStyle = region.borderColor;
                  ctx.fillRect(left, bbox.top, Math.max(1, right - left), 4 * pixelRatio);
                }
              }
              for (const marker of markersRef.current) {
                if (marker.time < currentTime[0] || marker.time > currentTime[currentTime.length - 1]) continue;
                const x = Math.round(u.valToPos(marker.time, "x", true));
                ctx.strokeStyle = marker.color;
                ctx.lineWidth = 2 * pixelRatio;
                ctx.setLineDash(marker.dashed ? [6 * pixelRatio, 5 * pixelRatio] : []);
                ctx.beginPath();
                ctx.moveTo(x, bbox.top);
                ctx.lineTo(x, bbox.top + bbox.height);
                ctx.stroke();

                if (marker.showPoints) {
                  const sampleIndex = nearestIndex(currentTime, marker.time);
                  const row = currentValues[sampleIndex] ?? [];
                  ctx.setLineDash([]);
                  row.slice(0, 6).forEach((value, axis) => {
                    const y = u.valToPos(value, "y", true);
                    ctx.beginPath();
                    ctx.fillStyle = colorsRef.current[axis] ?? defaultColors[axis % defaultColors.length];
                    ctx.strokeStyle = "#f8fafc";
                    ctx.lineWidth = 1.25 * pixelRatio;
                    ctx.arc(x, y, 4 * pixelRatio, 0, Math.PI * 2);
                    ctx.fill();
                    ctx.stroke();
                  });
                }
              }
              if (controlledCursor && cursorTimeRef.current !== undefined) {
                const playhead = cursorTimeRef.current;
                if (playhead >= currentTime[0] && playhead <= currentTime[currentTime.length - 1]) {
                  const x = Math.round(u.valToPos(playhead, "x", true));
                  ctx.setLineDash([]);
                  ctx.strokeStyle = "rgba(2, 6, 23, 0.95)";
                  ctx.lineWidth = 6 * pixelRatio;
                  ctx.beginPath();
                  ctx.moveTo(x, bbox.top);
                  ctx.lineTo(x, bbox.top + bbox.height);
                  ctx.stroke();
                  ctx.strokeStyle = "#f8fafc";
                  ctx.lineWidth = 2.25 * pixelRatio;
                  ctx.beginPath();
                  ctx.moveTo(x, bbox.top);
                  ctx.lineTo(x, bbox.top + bbox.height);
                  ctx.stroke();
                  ctx.fillStyle = "#38bdf8";
                  ctx.beginPath();
                  ctx.moveTo(x, bbox.top + 8 * pixelRatio);
                  ctx.lineTo(x - 6 * pixelRatio, bbox.top);
                  ctx.lineTo(x + 6 * pixelRatio, bbox.top);
                  ctx.closePath();
                  ctx.fill();
                }
              }
              ctx.restore();
            }
          ]
        },
        axes: [
          { stroke: "#94a3b8", grid: { stroke: "#253046" } },
          { stroke: "#94a3b8", grid: { stroke: "#253046" } }
        ],
        series: [
          { label: "time" },
          ...seriesLabels.map((label, index) => ({
            label,
            stroke: seriesColors[index] ?? defaultColors[index % defaultColors.length],
            width: 1.4,
            points: { show: false }
          }))
        ]
      },
      alignedData(timeRef.current, valuesRef.current, seriesLabels.length),
      host.current
    );
    fitTimeRange(plot.current, timeRef.current);
    fitValueRange(plot.current, valuesRef.current, seriesLabels.length);
    const observer = new ResizeObserver(() => {
      if (host.current && plot.current) {
        plot.current.setSize({ width: host.current.clientWidth, height });
      }
    });
    observer.observe(host.current);
    const select = (event: MouseEvent) => {
      if (!plot.current || !host.current || !onSelectTimeRef.current) return;
      const bounds = host.current.getBoundingClientRect();
      const relativeX = event.clientX - bounds.left;
      const relativeY = event.clientY - bounds.top;
      const plotLeft = plot.current.bbox.left / uPlot.pxRatio;
      const plotTop = plot.current.bbox.top / uPlot.pxRatio;
      const plotWidth = plot.current.bbox.width / uPlot.pxRatio;
      const plotHeight = plot.current.bbox.height / uPlot.pxRatio;
      if (
        relativeX < plotLeft
        || relativeX > plotLeft + plotWidth
        || relativeY < plotTop
        || relativeY > plotTop + plotHeight
      ) return;
      const canvasX = relativeX * uPlot.pxRatio;
      const value = plot.current.posToVal(canvasX, "x", true);
      const sampleIndex = nearestIndex(timeRef.current, value);
      if (sampleIndex >= 0) onSelectTimeRef.current(timeRef.current[sampleIndex]);
    };
    host.current.addEventListener("click", select);
    return () => {
      host.current?.removeEventListener("click", select);
      observer.disconnect();
      plot.current?.destroy();
      plot.current = null;
    };
  }, [controlledCursor, height, seriesLabels.join("|"), seriesColors.join("|")]);

  useEffect(() => {
    if (!plot.current) return;
    plot.current.batch(() => {
      plot.current?.setData(alignedData(time, values, seriesLabels.length), true);
      if (plot.current) {
        fitTimeRange(plot.current, time);
        fitValueRange(plot.current, values, seriesLabels.length);
      }
    });
  }, [time, values, seriesLabels.length]);

  useEffect(() => {
    if (!plot.current || cursorTime === undefined || !time.length) return;
    if (controlledCursor) plot.current.redraw();
    else plot.current.setCursor({ left: plot.current.valToPos(cursorTime, "x"), top: 0 });
  }, [controlledCursor, cursorTime, time]);

  useEffect(() => {
    plot.current?.redraw();
  }, [markers, regions]);

  const selectedIndex = cursorTime === undefined ? -1 : nearestIndex(time, cursorTime);
  const selectedTime = selectedIndex >= 0 ? time[selectedIndex] : undefined;
  const selectedValues = selectedIndex >= 0 ? values[selectedIndex] ?? [] : [];

  return (
    <div className="plot-with-markers">
      <div className="plot" ref={host} />
      {controlledCursor && showReadout && selectedTime !== undefined && <div className="plot-controlled-readout"><span><strong>time</strong>{selectedTime.toFixed(3)}</span>{seriesLabels.map((label, index) => <span key={label}><strong style={{ color: seriesColors[index] ?? defaultColors[index % defaultColors.length] }}>{label}</strong>{formatPlotValue(selectedValues[index])}</span>)}</div>}
      {selectionLabels.length > 0 && <div className="plot-selection-context"><strong>{tr("标注", "Annotation")}</strong>{selectionLabels.map((item) => <button type="button" key={item.key} style={item.color ? { borderColor: item.color } : undefined} onClick={() => onSelectLabel?.(item.key)}>{item.label}</button>)}</div>}
      {showMarkerKey && markers.length > 0 && <div className="plot-marker-key">{markers.map((marker) => <span key={`${marker.label}-${marker.time}`}><i style={{ background: marker.color }} />{marker.label} · {marker.time.toFixed(3)} s</span>)}</div>}
    </div>
  );
}
