import { useEffect, useRef } from "react";
import uPlot from "uplot";

const colors = ["#ef4444", "#22c55e", "#3b82f6", "#f59e0b", "#a855f7", "#06b6d4"];
const labels = ["ax", "ay", "az", "gx", "gy", "gz"];
const noMarkers: PlotMarker[] = [];

type Props = {
  time: number[];
  values: number[][];
  cursorTime?: number;
  markers?: PlotMarker[];
  height?: number;
  onSelectTime?: (time: number) => void;
};

export type PlotMarker = {
  time: number;
  label: string;
  color: string;
  dashed?: boolean;
  showPoints?: boolean;
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

function alignedData(time: number[], values: number[][]): uPlot.AlignedData {
  const columns = Array.from({ length: 6 }, (_, axis) =>
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

function fitValueRange(plot: uPlot, values: number[][]) {
  const finite = values.flatMap((row) => row.slice(0, 6)).filter(Number.isFinite);
  if (!finite.length) return;
  const minimum = Math.min(...finite);
  const maximum = Math.max(...finite);
  const span = maximum - minimum;
  const padding = span > 0 ? span * 0.08 : Math.max(Math.abs(maximum) * 0.05, 1);
  plot.setScale("y", { min: minimum - padding, max: maximum + padding });
}

export default function Plot({ time, values, cursorTime, markers = noMarkers, height = 290, onSelectTime }: Props) {
  const host = useRef<HTMLDivElement>(null);
  const plot = useRef<uPlot | null>(null);
  const onSelectTimeRef = useRef(onSelectTime);
  const timeRef = useRef(time);
  const valuesRef = useRef(values);
  const markersRef = useRef(markers);

  timeRef.current = time;
  valuesRef.current = values;
  markersRef.current = markers;

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
        cursor: { drag: { x: true, y: false } },
        scales: { x: { time: false } },
        ...(markersRef.current.length > 0 ? {
          hooks: {
            draw: [
              (u: uPlot) => {
                const currentTime = timeRef.current;
                const currentValues = valuesRef.current;
                if (!currentTime.length || !markersRef.current.length) return;
                const { ctx, bbox } = u;
                const pixelRatio = uPlot.pxRatio;
                ctx.save();
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
                      ctx.fillStyle = colors[axis];
                      ctx.strokeStyle = "#f8fafc";
                      ctx.lineWidth = 1.25 * pixelRatio;
                      ctx.arc(x, y, 4 * pixelRatio, 0, Math.PI * 2);
                      ctx.fill();
                      ctx.stroke();
                    });
                  }
                }
                ctx.restore();
              }
            ]
          }
        } : {}),
        axes: [
          { stroke: "#94a3b8", grid: { stroke: "#253046" } },
          { stroke: "#94a3b8", grid: { stroke: "#253046" } }
        ],
        series: [
          { label: "time" },
          ...labels.map((label, index) => ({
            label,
            stroke: colors[index],
            width: 1.4,
            points: { show: false }
          }))
        ]
      },
      alignedData(timeRef.current, valuesRef.current),
      host.current
    );
    fitTimeRange(plot.current, timeRef.current);
    fitValueRange(plot.current, valuesRef.current);
    const observer = new ResizeObserver(() => {
      if (host.current && plot.current) {
        plot.current.setSize({ width: host.current.clientWidth, height });
      }
    });
    observer.observe(host.current);
    const select = (event: MouseEvent) => {
      if (!plot.current || !host.current || !onSelectTimeRef.current) return;
      const bounds = host.current.getBoundingClientRect();
      const value = plot.current.posToVal(event.clientX - bounds.left, "x");
      onSelectTimeRef.current(value);
    };
    host.current.addEventListener("click", select);
    return () => {
      host.current?.removeEventListener("click", select);
      observer.disconnect();
      plot.current?.destroy();
      plot.current = null;
    };
  }, [height]);

  useEffect(() => {
    if (!plot.current) return;
    plot.current.batch(() => {
      plot.current?.setData(alignedData(time, values), true);
      if (plot.current) {
        fitTimeRange(plot.current, time);
        fitValueRange(plot.current, values);
      }
    });
  }, [time, values]);

  useEffect(() => {
    if (plot.current && cursorTime !== undefined && time.length) {
      plot.current.setCursor({ left: plot.current.valToPos(cursorTime, "x"), top: 0 });
    }
  }, [cursorTime, time]);

  useEffect(() => {
    plot.current?.redraw();
  }, [markers]);

  return (
    <div className="plot-with-markers">
      <div className="plot" ref={host} />
      {markers.length > 0 && <div className="plot-marker-key">{markers.map((marker) => <span key={`${marker.label}-${marker.time}`}><i style={{ background: marker.color }} />{marker.label} · {marker.time.toFixed(3)} s</span>)}</div>}
    </div>
  );
}
