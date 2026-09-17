import { useEffect, useMemo, useRef, useState } from "react";
import { tr } from "./i18n";

type ArraySpec = {path: string; dtype: string; shape: number[]};
type Manifest = {
  frame_period_s: number; sensor_rate_hz: number;
  layout: {mounts: {sensor_id?: string; mount_id?: string; joint?: string}[]};
  sensors: {specific_force_m_s2: ArraySpec; angular_velocity_rad_s: ArraySpec};
};
type ChartData = {manifest: Manifest; force: Float32Array; gyro: Float32Array;
  samples: number; mounts: number};
export type IMUReadout = {sensor: string; time_s: number; force: number; gyro: number};

const colorForce = "#38bdf8";
const colorGyro = "#f472b6";

async function loadChart(base: string, signal: AbortSignal): Promise<ChartData> {
  const root = `${base}/files/`;
  const response = await fetch(`${root}manifest.json`, {signal});
  if (!response.ok) throw new Error(`manifest HTTP ${response.status}`);
  const manifest = await response.json() as Manifest;
  const a = manifest.sensors.specific_force_m_s2;
  const b = manifest.sensors.angular_velocity_rad_s;
  if (a.shape.length !== 3 || a.shape[2] !== 3 || a.shape[0] < 2 || a.shape[1] < 1
      || a.shape.join(",") !== b.shape.join(",") || !a.dtype.endsWith("f4")
      || !b.dtype.endsWith("f4") || manifest.sensor_rate_hz <= 0
      || manifest.frame_period_s <= 0) throw new Error("IMU chart contract is invalid");
  const get = async (spec: ArraySpec) => {
    const file = await fetch(`${root}${encodeURIComponent(spec.path)}`, {signal});
    if (!file.ok) throw new Error(`${spec.path} HTTP ${file.status}`);
    const bytes = await file.arrayBuffer();
    if (bytes.byteLength !== spec.shape[0] * spec.shape[1] * 3 * 4)
      throw new Error(`${spec.path} length differs`);
    return new Float32Array(bytes);
  };
  const [force, gyro] = await Promise.all([get(a), get(b)]);
  return {manifest, force, gyro, samples: a.shape[0], mounts: a.shape[1]};
}

function magnitudes(data: ChartData, mount: number) {
  const force = new Float32Array(data.samples);
  const gyro = new Float32Array(data.samples);
  for (let i = 0; i < data.samples; i++) {
    const offset = (i * data.mounts + mount) * 3;
    force[i] = Math.hypot(data.force[offset], data.force[offset + 1], data.force[offset + 2]);
    gyro[i] = Math.hypot(data.gyro[offset], data.gyro[offset + 1], data.gyro[offset + 2]);
  }
  return {force, gyro};
}

export function SyntheticIMUChart({base, cursorFrame, onSeek, onReady, onInspect}: {
  base: string; cursorFrame: number; onSeek: (frame: number) => void;
  onReady: (ready: boolean) => void; onInspect: (value: IMUReadout | null) => void;
}) {
  const [data, setData] = useState<ChartData | null>(null);
  const [error, setError] = useState("");
  const [mount, setMount] = useState(0);
  const [zoom, setZoom] = useState<[number, number] | null>(null);
  const [expanded, setExpanded] = useState(false);
  const canvas = useRef<HTMLCanvasElement>(null);
  const drag = useRef<number | null>(null);
  const drawRef = useRef<() => void>(() => undefined);
  const onReadyRef = useRef(onReady);
  onReadyRef.current = onReady;
  const onInspectRef = useRef(onInspect);
  onInspectRef.current = onInspect;

  useEffect(() => {
    const controller = new AbortController();
    setData(null); setError(""); setMount(0); setZoom(null); setExpanded(false);
    onReadyRef.current(false);
    onInspectRef.current(null);
    void loadChart(base, controller.signal).then(value => {
      setData(value);
      onReadyRef.current(true);
    }).catch(reason => {
      if (controller.signal.aborted) return;
      setError(String(reason));
      onReadyRef.current(false);
    });
    return () => controller.abort();
  }, [base]);

  useEffect(() => {
    if (!expanded) return;
    const close = (event: KeyboardEvent) => {
      if (event.key === "Escape") { event.stopPropagation(); setExpanded(false); }
    };
    window.addEventListener("keydown", close, true);
    return () => window.removeEventListener("keydown", close, true);
  }, [expanded]);

  const values = useMemo(() => data ? magnitudes(data, mount) : null, [data, mount]);
  const bounds: [number, number] = zoom ?? [0, data ? data.samples - 1 : 1];
  const cursorSample = data ? Math.max(0, Math.min(data.samples - 1,
    Math.round(cursorFrame * data.manifest.frame_period_s * data.manifest.sensor_rate_hz))) : 0;
  useEffect(() => {
    if (!data || !values) return;
    const sensor = data.manifest.layout.mounts[mount];
    onInspectRef.current({
      sensor: sensor?.sensor_id ?? sensor?.mount_id ?? sensor?.joint ?? `IMU ${mount + 1}`,
      time_s: cursorSample / data.manifest.sensor_rate_hz,
      force: values.force[cursorSample], gyro: values.gyro[cursorSample],
    });
  }, [data, values, cursorSample, mount]);

  drawRef.current = () => {
    const surface = canvas.current;
    if (!surface || !data || !values) return;
    const rect = surface.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const ratio = devicePixelRatio || 1;
    surface.width = Math.round(rect.width * ratio);
    surface.height = Math.round(rect.height * ratio);
    const context = surface.getContext("2d");
    if (!context) return;
    context.scale(ratio, ratio);
    const width = rect.width; const height = rect.height;
    const left = 52; const right = width - 16;
    const laneHeight = (height - 30) / 2;
    context.fillStyle = "#07101d"; context.fillRect(0, 0, width, height);
    context.font = "11px system-ui";
    const lanes = [
      {array: values.force, color: colorForce, top: 4, label: "m/s²"},
      {array: values.gyro, color: colorGyro, top: laneHeight + 16, label: "rad/s"},
    ];
    for (const lane of lanes) {
      const bottom = lane.top + laneHeight - 7;
      let peak = 0;
      for (let i = bounds[0]; i <= bounds[1]; i++) peak = Math.max(peak, lane.array[i]);
      peak = Math.max(peak, .001);
      context.strokeStyle = "#263753"; context.lineWidth = 1;
      context.beginPath(); context.moveTo(left, bottom); context.lineTo(right, bottom);
      context.stroke();
      context.fillStyle = "#9eb0c8";
      context.fillText(lane.label, 6, lane.top + 13);
      context.fillText(peak.toFixed(1), 6, lane.top + 29);
      context.strokeStyle = lane.color; context.lineWidth = 1.25;
      context.beginPath();
      const pixels = Math.max(1, Math.floor(right - left));
      for (let x = 0; x < pixels; x++) {
        const start = Math.min(bounds[1], bounds[0] + Math.floor(
          x / pixels * (bounds[1] - bounds[0] + 1)));
        const stop = Math.min(bounds[1] + 1, bounds[0] + Math.ceil(
          (x + 1) / pixels * (bounds[1] - bounds[0] + 1)));
        let minimum = Infinity; let maximum = 0;
        for (let i = start; i < Math.max(start + 1, stop); i++) {
          minimum = Math.min(minimum, lane.array[i]);
          maximum = Math.max(maximum, lane.array[i]);
        }
        const y1 = bottom - minimum / peak * (laneHeight - 28);
        const y2 = bottom - maximum / peak * (laneHeight - 28);
        context.moveTo(left + x, y1); context.lineTo(left + x, y2);
      }
      context.stroke();
    }
    const second = (index: number) => (index / data.manifest.sensor_rate_hz).toFixed(1);
    context.fillStyle = "#9eb0c8";
    context.fillText(`${second(bounds[0])} s`, left, height - 3);
    context.fillText(`${second(bounds[1])} s`, Math.max(left, right - 55), height - 3);
    if (cursorSample >= bounds[0] && cursorSample <= bounds[1]) {
      const x = left + (cursorSample - bounds[0]) / Math.max(1, bounds[1] - bounds[0]) * (right - left);
      context.strokeStyle = "#facc15"; context.lineWidth = 2;
      context.beginPath(); context.moveTo(x, 4); context.lineTo(x, height - 18); context.stroke();
    }
  };
  useEffect(() => { drawRef.current(); }, [data, values, zoom, cursorFrame, expanded]);
  useEffect(() => {
    if (!canvas.current) return;
    const observer = new ResizeObserver(() => drawRef.current());
    observer.observe(canvas.current);
    return () => observer.disconnect();
  }, [data]);

  const sampleAt = (clientX: number) => {
    if (!canvas.current || !data) return 0;
    const rect = canvas.current.getBoundingClientRect();
    const fraction = Math.max(0, Math.min(1, (clientX - rect.left - 52) / (rect.width - 68)));
    return Math.round(bounds[0] + fraction * (bounds[1] - bounds[0]));
  };
  const finishDrag = (clientX: number) => {
    if (drag.current === null || !data) return;
    const first = drag.current;
    drag.current = null;
    if (Math.abs(clientX - first) > 12) {
      const start = sampleAt(Math.min(first, clientX));
      const end = sampleAt(Math.max(first, clientX));
      if (end - start > 2) setZoom([start, end]);
    } else {
      onSeek(Math.round(sampleAt(clientX) / data.manifest.sensor_rate_hz
        / data.manifest.frame_period_s));
    }
  };
  const name = data?.manifest.layout.mounts[mount];
  return <section className={`synthetic-chart ${expanded ? "synthetic-chart-expanded" : ""}`}>
    <div className="synthetic-chart-header">
      <strong>{tr("IMU 曲线", "IMU traces")}</strong>
      {data && <span className="synthetic-chart-legend">
        <b style={{color: colorForce}}>●</b> {tr("比力幅值", "Specific force")} ·
        <b style={{color: colorGyro}}> ●</b> {tr("角速度幅值", "Angular velocity")}
      </span>}
      {data && data.mounts > 1 && <select aria-label={tr("传感器", "Sensor")}
        value={mount} onChange={event => setMount(Number(event.target.value))}>
        {Array.from({length: data.mounts}, (_, index) => <option key={index} value={index}>
          {data.manifest.layout.mounts[index]?.sensor_id
            ?? data.manifest.layout.mounts[index]?.mount_id
            ?? data.manifest.layout.mounts[index]?.joint ?? `IMU ${index + 1}`}
        </option>)}
      </select>}
      {data && <span className="synthetic-chart-sensor">{name?.sensor_id ?? name?.joint ?? "IMU 1"}</span>}
      <button disabled={!zoom} onClick={() => setZoom(null)}>{tr("全段", "Fit all")}</button>
      <button onClick={() => setExpanded(value => !value)}>
        {expanded ? tr("收起", "Close") : tr("放大曲线", "Expand chart")}</button>
    </div>
    {data ? <canvas ref={canvas} onPointerDown={event => {
      drag.current = event.clientX; event.currentTarget.setPointerCapture(event.pointerId);
    }} onPointerUp={event => finishDrag(event.clientX)}
      aria-label={tr("IMU 曲线：点击定位，拖动选择时间段", "IMU traces: click to seek, drag to zoom")} />
      : <div className="synthetic-chart-empty">{error
        ? tr("独立曲线不可用，预览窗口内的小曲线仍可查看。", "Chart unavailable; use the preview's small chart.")
        : tr("正在读取 IMU 曲线…", "Loading IMU traces…")}</div>}
    {error && <small title={error}>{tr("读取失败", "Load failed")}</small>}
  </section>;
}
