import { useEffect, useMemo, useRef, useState } from "react";
import Plot from "./Plot";
import { tr } from "./i18n";
import { extremumIndices } from "./syntheticTraceSampling";

type ArraySpec = {path: string; dtype: string; shape: number[]};
type Manifest = {
  frame_period_s: number; sensor_rate_hz: number; frame_count?: number;
  layout: {layout_id?: string; mounts: {sensor_id?: string; mount_id?: string; joint?: string}[]};
  dynamic_shape?: {source_available?: boolean}; qa?: {passed?: boolean};
  sensors: {specific_force_m_s2: ArraySpec; angular_velocity_rad_s: ArraySpec};
};
type ChartData = {manifest: Manifest; force: Float32Array; gyro: Float32Array;
  samples: number; mounts: number};
export type IMUReadout = {sensor: string; time_s: number; force: number; gyro: number};
export type PlaybackMetadata = {frameCount?: number; layoutId?: string;
  dmplAvailable?: boolean; qaPassed?: boolean};

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

export function SyntheticIMUChart({base, cursorFrame, onSeek, onReady, onInspect, onMetadata}: {
  base: string; cursorFrame: number; onSeek: (frame: number) => void;
  onReady: (ready: boolean) => void; onInspect: (value: IMUReadout | null) => void;
  onMetadata?: (value: PlaybackMetadata | null) => void;
}) {
  const [data, setData] = useState<ChartData | null>(null);
  const [error, setError] = useState("");
  const [mount, setMount] = useState(0);
  const [viewMode, setViewMode] = useState<"magnitude" | "axes">("magnitude");
  const onReadyRef = useRef(onReady);
  const onInspectRef = useRef(onInspect);
  const onMetadataRef = useRef(onMetadata);
  onReadyRef.current = onReady;
  onInspectRef.current = onInspect;
  onMetadataRef.current = onMetadata;

  useEffect(() => {
    const controller = new AbortController();
    setData(null); setError(""); setMount(0);
    onReadyRef.current(false); onInspectRef.current(null); onMetadataRef.current?.(null);
    void loadChart(base, controller.signal).then(value => {
      setData(value); onReadyRef.current(true);
      onMetadataRef.current?.({frameCount: value.manifest.frame_count,
        layoutId: value.manifest.layout.layout_id,
        dmplAvailable: value.manifest.dynamic_shape?.source_available,
        qaPassed: value.manifest.qa?.passed});
    }).catch(reason => {
      if (controller.signal.aborted) return;
      setError(String(reason)); onReadyRef.current(false);
    });
    return () => controller.abort();
  }, [base]);

  const cursorSample = data ? Math.max(0, Math.min(data.samples - 1,
    Math.round(cursorFrame * data.manifest.frame_period_s * data.manifest.sensor_rate_hz))) : 0;
  useEffect(() => {
    if (!data) return;
    const sensor = data.manifest.layout.mounts[mount];
    const offset = (cursorSample * data.mounts + mount) * 3;
    onInspectRef.current({
      sensor: sensor?.sensor_id ?? sensor?.mount_id ?? sensor?.joint ?? `IMU ${mount + 1}`,
      time_s: cursorSample / data.manifest.sensor_rate_hz,
      force: Math.hypot(data.force[offset], data.force[offset + 1], data.force[offset + 2]),
      gyro: Math.hypot(data.gyro[offset], data.gyro[offset + 1], data.gyro[offset + 2]),
    });
  }, [data, cursorSample, mount]);

  const traces = useMemo(() => {
    if (!data) return null;
    const indices = extremumIndices(data.force, data.gyro, data.samples, data.mounts,
      mount, 1100, viewMode);
    return {
      time: indices.map(index => index / data.manifest.sensor_rate_hz),
      values: indices.map(index => {
        const offset = (index * data.mounts + mount) * 3;
        return viewMode === "magnitude"
          ? [Math.hypot(data.force[offset], data.force[offset + 1], data.force[offset + 2]),
            Math.hypot(data.gyro[offset], data.gyro[offset + 1], data.gyro[offset + 2])]
          : [data.force[offset], data.force[offset + 1], data.force[offset + 2],
            data.gyro[offset], data.gyro[offset + 1], data.gyro[offset + 2]];
      }),
    };
  }, [data, mount, viewMode]);
  const selectTime = (time: number) => {
    if (data) onSeek(Math.round(time / data.manifest.frame_period_s));
  };
  const name = data?.manifest.layout.mounts[mount];
  return <section className="synthetic-chart synthetic-six-axis">
    <div className="synthetic-chart-header">
      <strong>{tr("IMU 曲线 · 全片段", "IMU traces · full clip")}</strong>
      <span>{tr("点击曲线定位播放", "Click a trace to seek")}</span>
      {data && data.mounts > 1 && <select aria-label={tr("传感器", "Sensor")}
        value={mount} onChange={event => setMount(Number(event.target.value))}>
        {Array.from({length: data.mounts}, (_, index) => <option key={index} value={index}>
          {data.manifest.layout.mounts[index]?.sensor_id
            ?? data.manifest.layout.mounts[index]?.mount_id
            ?? data.manifest.layout.mounts[index]?.joint ?? `IMU ${index + 1}`}
        </option>)}
      </select>}
      {data && <span className="synthetic-chart-sensor">{name?.sensor_id ?? name?.joint ?? "IMU 1"}</span>}
      <div className="synthetic-chart-mode" role="group" aria-label={tr("曲线视图", "Trace view")}>
        <button type="button" className={viewMode === "magnitude" ? "active" : ""}
          aria-pressed={viewMode === "magnitude"} onClick={() => setViewMode("magnitude")}
        >{tr("总量 · 2 条", "Magnitudes · 2")}</button>
        <button type="button" className={viewMode === "axes" ? "active" : ""}
          aria-pressed={viewMode === "axes"} onClick={() => setViewMode("axes")}
        >{tr("分轴 · 6 条", "Axes · 6")}</button>
      </div>
    </div>
    {traces && data ? <div className="synthetic-combined-trace">
      {viewMode === "magnitude" ? <div className="synthetic-trace-legend">
        <span style={{color: "#38bdf8"}}>{tr("总加速度（比力模长）· 左轴 m/s²", "Total acceleration (specific-force norm) · left m/s²")}</span>
        <span style={{color: "#f472b6"}}>{tr("总角速度 · 右轴 rad/s", "Total angular velocity · right rad/s")}</span>
      </div> : <div className="synthetic-trace-legend">
        <span>{tr("左轴：比力 m/s²", "Left: force m/s²")}</span>
        <span style={{color: "#ef4444"}}>ax</span><span style={{color: "#22c55e"}}>ay</span>
        <span style={{color: "#3b82f6"}}>az</span>
        <span>{tr("右轴：角速度 rad/s", "Right: angular rate rad/s")}</span>
        <span style={{color: "#f59e0b"}}>gx</span><span style={{color: "#a855f7"}}>gy</span>
        <span style={{color: "#06b6d4"}}>gz</span>
      </div>}
      <Plot time={traces.time} values={traces.values}
        cursorTime={cursorSample / data.manifest.sensor_rate_hz} controlledCursor
        seriesLabels={viewMode === "magnitude" ? ["|a|", "|ω|"]
          : ["ax", "ay", "az", "gx", "gy", "gz"]}
        seriesColors={viewMode === "magnitude" ? ["#38bdf8", "#f472b6"]
          : ["#ef4444", "#22c55e", "#3b82f6", "#f59e0b", "#a855f7", "#06b6d4"]}
        splitAfter={viewMode === "magnitude" ? 1 : 3} height={190} showReadout={false}
        showMarkerKey={false} cursorColor="#facc15" scrubOnDrag snapSelection={false}
        onSelectTime={selectTime} />
    </div> : <div className="synthetic-chart-empty">{error
      ? tr("IMU 曲线读取失败，请检查片段文件。", "IMU traces failed to load; check the clip files.")
      : tr("正在读取 IMU 曲线…", "Loading IMU traces…")}</div>}
    {error && <small title={error}>{tr("读取失败", "Load failed")}</small>}
  </section>;
}
