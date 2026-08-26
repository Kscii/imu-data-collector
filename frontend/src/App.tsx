import { useEffect, useMemo, useRef, useState } from "react";
import Plot from "./Plot";

type Recording = {
  recording_id: string;
  collection_id: string;
  participant_id: string;
  data_tier: "test" | "prod" | "legacy_unclassified";
  state: string;
  started_at_utc: string;
  duration_ns?: number;
  issues: string[];
  upload_state: string;
};

type Segment = {
  segment_id: string;
  start_ns: number;
  end_ns: number;
  binary_label: "fall" | "non_fall";
  activity_code: string;
  annotator_id: string;
  confidence: number;
  notes: string;
};

type Event = {
  segment_id: string;
  kind: "onset" | "impact";
  time_ns: number;
  source_video_frame: number | null;
  source_imu_sample: number | null;
  annotator_id: string;
};

type Exclusion = {
  exclusion_id: string;
  start_ns: number;
  end_ns: number;
  reason: "sync_tap" | "setup" | "sensor_adjustment" | "sensor_removed" | "quality_issue" | "ambiguous" | "privacy" | "other";
  annotator_id: string;
  notes: string;
};

type AnnotationDocument = {
  taxonomy_id: string;
  taxonomy_version: string;
  revision: number;
  finalized: boolean;
  segments: Segment[];
  events: Event[];
  exclusions: Exclusion[];
};

type Taxonomy = {
  taxonomy_id: string;
  version: string;
  fall: { code: string; display_name_zh: string }[];
  non_fall: { code: string; display_name_zh: string }[];
};

type AppConfig = {
  allowed_unikeys: string[];
  admin_unikeys: string[];
  data_tiers: ("test" | "prod")[];
  default_data_tier: "test" | "prod";
  data_root: string;
  video: { width: number; height: number; requested_fps: number; bitrate: string };
};

type ReviewDocument = {
  schema_version: "1.0.0";
  recording_id: string;
  revision: number;
  workflow: {
    state: "unassigned" | "in_progress" | "submitted" | "accepted" | "exported";
    annotator_id: string | null;
    reviewer_id: string | null;
    review_comment: string;
    updated_at_utc: string | null;
  };
  annotations: AnnotationDocument;
};

type RecordingStatus = {
  capture: string;
  sync: string;
  annotation: string;
  calibration: string;
  export: string;
  review_revision: number;
};

type Camera = {
  camera_id: string;
  device: string;
  product: string;
  interface: string;
  integration: string;
  supports_default_profile: boolean;
  color_capture: boolean;
};

type SyncAnchor = {
  imu_time_ns: number;
  video_time_ns: number;
  label: string;
  role: "start_tap" | "end_tap" | "legacy";
  source_video_frame: number | null;
  source_imu_sample: number | null;
  video_interval_start_ns: number | null;
  imu_interval_start_ns: number | null;
  reviewer_id: string | null;
};
type SyncState = {
  anchors: SyncAnchor[];
  policy: string;
  scale: number;
  offset_ns: number;
  estimated_offset_ns: number;
  applied_offset_ns: number;
  start_offset_ns: number;
  end_offset_ns: number;
  anchor_disagreement_ns: number;
  residual_rms_ns: number;
  residual_upper_bound_ns: number;
  quality: string;
  decision: string;
  recommendation: string;
  estimated_offset_seconds: number;
  applied_offset_seconds: number;
  estimated_offset_video_frames: number | null;
  applied_offset_video_frames: number | null;
  estimated_offset_imu_samples: number | null;
  applied_offset_imu_samples: number | null;
  actual_median_fps: number | null;
  observed_imu_rate_hz: number | null;
};

type FrameTimes = {
  frame_count: number;
  time_ns: number[];
  media_time_ns: number[];
  duration_ns: number[];
  key_frame: boolean[];
};

type SyncWindow = {
  video_frame_index: number;
  video_time_ns: number;
  radius_seconds: number;
  sample_index: number[];
  time_ns: number[];
  time_s: number[];
  raw_counts: number[][];
  trailer: number[][];
  packet_index: number[];
  sample_in_packet: number[];
  candidate_sample_index: number[];
  candidate_peaks: {
    sample_index: number;
    time_ns: number;
    time_s: number;
    video_minus_imu_ms: number;
    expected_offset_residual_ms: number;
    accel_delta_score: number;
    robust_z: number;
    event_robust_z: number;
    strength_rank: number;
    recommendation_score: number;
    recommendation_rank: number;
    selection_basis: "event_onset" | "local_peak" | "timing_projection";
  }[];
  recommendation: {
    algorithm: string;
    sample_index: number | null;
    confidence: "high" | "medium" | "low";
    reason: string;
    expected_video_minus_imu_ns: number | null;
    score_margin_ratio: number | null;
  };
};

type SyncObservation = {
  observation_id: string;
  recording_id: string;
  video_frame_index: number;
  video_time_ns: number;
  imu_sample_index: number;
  imu_time_ns: number;
  label: string;
  reviewer_id: string;
  notes: string;
  selection_mode?: "legacy_manual" | "auto_recommended" | "manual_candidate";
  recommendation_algorithm?: string | null;
  recommended_sample_index?: number | null;
  recommendation_confidence?: "high" | "medium" | "low" | null;
  candidate_strength_rank?: number | null;
  candidate_score?: number | null;
  expected_video_minus_imu_ns?: number | null;
};

type SyncExperimentDocument = {
  schema_version: "1.0.0";
  experiment_id: string;
  revision: number;
  updated_at_utc: string | null;
  observations: SyncObservation[];
  sources: unknown[];
};

const characterizationStages = [
  ["pipeline_smoke_uncontrolled", "链路冒烟（姿态未控制）", "仅验证连接、落盘和报告；不参与任何尺度或方向候选"],
  ["long_static_button_up", "长时静止（按键面朝上）", "建议 30 分钟；用于频率、间隙、零偏和噪声"],
  ["button_face_up", "按键面朝上（+Z）", "建议 60 秒，稳定平放"],
  ["button_face_down", "按键面朝下（-Z）", "建议 60 秒，稳定平放"],
  ["interface_face_up", "接口面朝上（-X）", "建议 60 秒，稳定平放"],
  ["interface_opposite_face_up", "接口反面朝上（+X）", "建议 60 秒，稳定平放"],
  ["pendant_end_up_exploratory", "挂绳端朝上（+Y，探索）", "外壳不稳，仅作探索，手持 30 秒"],
  ["pendant_end_down_exploratory", "挂绳端朝下（-Y，探索）", "外壳不稳，仅作探索，手持 30 秒"],
  ["gyro_x_positive", "绕 +X 正向旋转", "手动匀速转动；只能验证响应与符号候选"],
  ["gyro_x_negative", "绕 +X 反向旋转", "与上一阶段成对"],
  ["gyro_y_positive", "绕 +Y 正向旋转", "手动匀速转动；只能验证响应与符号候选"],
  ["gyro_y_negative", "绕 +Y 反向旋转", "与上一阶段成对"],
  ["gyro_z_positive", "绕 +Z 正向旋转", "手动匀速转动；只能验证响应与符号候选"],
  ["gyro_z_negative", "绕 +Z 反向旋转", "与上一阶段成对"]
] as const;

const LIVE_WINDOW_SECONDS = 120;
const SYNC_EXPERIMENT_ID = "sync_validation_01";
const exclusionLabels: Record<Exclusion["reason"], string> = {
  sync_tap: "同步轻拍",
  setup: "录制设置",
  sensor_adjustment: "佩戴调整",
  sensor_removed: "摘下设备",
  quality_issue: "数据质量异常",
  ambiguous: "无法确认",
  privacy: "隐私排除",
  other: "其他"
};

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) }
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.detail ?? `${response.status} ${response.statusText}`);
  }
  return response.json();
}

function seconds(ns?: number | null) {
  return ns == null ? "—" : `${(ns / 1e9).toFixed(2)} s`;
}

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

function median(values: number[]) {
  if (!values.length) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
}

function recommendOffset(
  observations: SyncObservation[],
  recordingId: string,
  videoTimeNs: number
): {
  value: number | null;
  source: "recording_affine" | "recording_offset" | "global_offset" | "none";
} {
  const recording = observations.filter((item) => item.recording_id === recordingId);
  if (recording.length >= 2) {
    const meanImu = recording.reduce((sum, item) => sum + item.imu_time_ns, 0) / recording.length;
    const meanVideo = recording.reduce((sum, item) => sum + item.video_time_ns, 0) / recording.length;
    const covariance = recording.reduce(
      (sum, item) => sum + (item.imu_time_ns - meanImu) * (item.video_time_ns - meanVideo),
      0
    );
    const variance = recording.reduce(
      (sum, item) => sum + (item.imu_time_ns - meanImu) ** 2,
      0
    );
    if (variance > 0) {
      const scale = covariance / variance;
      const offset = meanVideo - scale * meanImu;
      if (Number.isFinite(scale) && scale > 0.98 && scale < 1.02) {
        const predictedImuTime = (videoTimeNs - offset) / scale;
        return {
          value: videoTimeNs - predictedImuTime,
          source: "recording_affine"
        };
      }
    }
  }
  if (recording.length) {
    return {
      value: median(recording.map((item) => item.video_time_ns - item.imu_time_ns)),
      source: "recording_offset"
    };
  }
  if (observations.length) {
    return {
      value: median(observations.map((item) => item.video_time_ns - item.imu_time_ns)),
      source: "global_offset"
    };
  }
  return { value: null, source: "none" };
}

function stateLabel(state: string) {
  const labels: Record<string, string> = {
    idle: "空闲",
    arming: "正在准备",
    recording: "正在录制",
    finalizing: "正在收尾",
    ready: "可用",
    needs_attention: "需要检查",
    failed: "失败"
  };
  return labels[state] ?? state;
}

function issueLabel(issue: string) {
  const labels: Record<string, string> = {
    "synchronization anchors have not been verified": "同步锚点尚未验证",
    "IMU scale calibration has not been verified": "IMU 尺度校准尚未验证"
  };
  return labels[issue] ?? issue;
}

export default function App() {
  const [tab, setTab] = useState<"capture" | "characterize" | "annotate" | "library">("capture");
  const [live, setLive] = useState<any>({ state: "idle", imu: {}, video: {} });
  const [participant, setParticipant] = useState("xfan0282");
  const [collection, setCollection] = useState("xfan0282_test_01");
  const [dataTier, setDataTier] = useState<"test" | "prod">("test");
  const [captureError, setCaptureError] = useState("");
  const [recordings, setRecordings] = useState<Recording[]>([]);
  const [taxonomy, setTaxonomy] = useState<Taxonomy | null>(null);
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [cameras, setCameras] = useState<Camera[]>([]);
  const [cameraId, setCameraId] = useState("");
  const liveRef = useRef<{ t: number[]; values: number[][] }>({ t: [], values: [] });
  const [, redraw] = useState(0);

  const refreshRecordings = () =>
    api<Recording[]>("/api/v1/recordings").then(setRecordings).catch((e) => setCaptureError(e.message));
  const refreshCameras = () =>
    api<{ cameras: Camera[] }>("/api/v1/devices").then((value) => {
      setCameras(value.cameras);
      setCameraId((current) => {
        if (value.cameras.some((item) => item.camera_id === current)) return current;
        const compatible = value.cameras.filter((item) => item.supports_default_profile && item.color_capture);
        return compatible.find((item) => item.integration === "external")?.camera_id
          ?? compatible[0]?.camera_id
          ?? "";
      });
    }).catch((e) => setCaptureError(e.message));

  useEffect(() => {
    api<Taxonomy>("/api/v1/taxonomy").then(setTaxonomy).catch((e) => setCaptureError(e.message));
    api<AppConfig>("/api/v1/config").then((value) => {
      setConfig(value);
      if (!value.allowed_unikeys.includes(participant) && value.allowed_unikeys.length) {
        setParticipant(value.allowed_unikeys[0]);
      }
      setDataTier(value.default_data_tier);
    }).catch((e) => setCaptureError(e.message));
    refreshCameras();
    refreshRecordings();
    const protocol = location.protocol === "https:" ? "wss" : "ws";
    const socket = new WebSocket(`${protocol}://${location.host}/api/v1/live`);
    socket.onmessage = (message) => {
      const payload = JSON.parse(message.data);
      setLive(payload);
      if (payload.imu?.connected && payload.imu?.raw) {
        const data = liveRef.current;
        const nowSeconds = performance.now() / 1000;
        data.t.push(nowSeconds);
        data.values.push(payload.imu.raw);
        while (data.t.length && data.t[0] < nowSeconds - LIVE_WINDOW_SECONDS) {
          data.t.shift();
          data.values.shift();
        }
        liveRef.current = { t: [...data.t], values: [...data.values] };
        redraw((value) => value + 1);
      }
    };
    return () => socket.close();
  }, []);

  const start = async () => {
    setCaptureError("");
    try {
      await api("/api/v1/recordings/start", {
        method: "POST",
        body: JSON.stringify({
          collection_id: collection,
          participant_id: participant,
          data_tier: dataTier,
          body_location: "chest",
          protocol_id: taxonomy?.taxonomy_id ?? "fall_binary_v1",
          camera_id: cameraId || null
        })
      });
      liveRef.current = { t: [], values: [] };
    } catch (e) {
      setCaptureError((e as Error).message);
    }
  };

  const stop = async () => {
    setCaptureError("");
    try {
      await api("/api/v1/recordings/stop", { method: "POST" });
      refreshRecordings();
    } catch (e) {
      setCaptureError((e as Error).message);
    }
  };

  const toggleImuPreview = async () => {
    setCaptureError("");
    try {
      const active = live.session_type === "devices_preview";
      await api(`/api/v1/preflight/${active ? "stop" : "start"}`, {
        method: "POST",
        body: active ? undefined : JSON.stringify({ camera_id: cameraId || null })
      });
      if (!active) liveRef.current = { t: [], values: [] };
    } catch (e) {
      setCaptureError((e as Error).message);
    }
  };

  return (
    <div className="app-shell">
      <header>
        <div>
          <span className="eyebrow">CW12EU-T · 本机采集</span>
          <h1>IMU 数采平台</h1>
        </div>
        <div className={`state state-${live.state}`}>{live.session_type === "devices_preview" ? "设备预览" : stateLabel(live.state)}</div>
      </header>
      <nav>
        <button className={tab === "capture" ? "active" : ""} onClick={() => setTab("capture")}>采集</button>
        <button className={tab === "characterize" ? "active" : ""} onClick={() => setTab("characterize")}>IMU 表征</button>
        <button className={tab === "annotate" ? "active" : ""} onClick={() => setTab("annotate")}>标注</button>
        <button className={tab === "library" ? "active" : ""} onClick={() => { setTab("library"); refreshRecordings(); }}>记录</button>
      </nav>
      {tab === "capture" && captureError && <div className="error-banner">{captureError}</div>}
      {tab === "capture" && (
        <CapturePage
          live={live}
          participant={participant}
          setParticipant={setParticipant}
          collection={collection}
          setCollection={setCollection}
          dataTier={dataTier}
          setDataTier={setDataTier}
          start={start}
          stop={stop}
          chart={liveRef.current}
          allowedUnikeys={config?.allowed_unikeys ?? []}
          cameras={cameras}
          cameraId={cameraId}
          setCameraId={setCameraId}
          refreshCameras={refreshCameras}
          toggleImuPreview={toggleImuPreview}
        />
      )}
      {tab === "characterize" && (
        <CharacterizationPage
          live={live}
          allowedUnikeys={config?.allowed_unikeys ?? []}
          chart={liveRef.current}
        />
      )}
      {tab === "annotate" && taxonomy && (
        <AnnotationPage recordings={recordings} taxonomy={taxonomy} allowedUnikeys={config?.allowed_unikeys ?? []} />
      )}
      {tab === "library" && <Library recordings={recordings} onChanged={refreshRecordings} />}
    </div>
  );
}

function CapturePage(props: any) {
  const {
    live, participant, setParticipant, collection, setCollection, start, stop, chart,
    dataTier, setDataTier, allowedUnikeys, cameras, cameraId, setCameraId, refreshCameras,
    toggleImuPreview
  } = props;
  const active = live.state === "recording" && live.session_type === "capture";
  const devicesPreview = live.session_type === "devices_preview";
  const busy = ["arming", "finalizing"].includes(live.state);
  const anotherSession = live.state === "recording" && live.session_type !== "capture";
  return (
    <main>
      <section className="controls panel">
        <label>数据批次 ID<input value={collection} onChange={(e) => setCollection(e.target.value)} disabled={active || busy} /></label>
        <label>参与者 UniKey<select value={participant} onChange={(e) => setParticipant(e.target.value)} disabled={active || busy}>{allowedUnikeys.map((item: string) => <option value={item} key={item}>{item}</option>)}</select></label>
        <label>数据级别<select value={dataTier} onChange={(e) => setDataTier(e.target.value as "test" | "prod")} disabled={active || busy}><option value="test">test · 永久禁止训练</option><option value="prod">prod · 仍需通过全部质量门禁</option></select></label>
        <label>摄像头<select value={cameraId} onChange={(e) => setCameraId(e.target.value)} disabled={active || busy}>{cameras.map((item: Camera) => <option value={item.camera_id} key={item.camera_id}>{item.product} · {item.device}{item.integration === "external" ? " · 外接" : ""}{item.supports_default_profile && item.color_capture ? " · 推荐" : " · 不兼容"}</option>)}</select></label>
        <button disabled={active || busy} onClick={refreshCameras}>重新扫描摄像头</button>
        <button disabled={active || busy || anotherSession || (!devicesPreview && !cameraId)} onClick={toggleImuPreview}>{devicesPreview ? "释放预览设备" : "连接预览设备"}</button>
        {!active ? <button className="primary" disabled={busy || anotherSession || !cameraId} onClick={start}>开始录制</button> : <button className="danger" onClick={stop}>结束录制</button>}
      </section>
      <section className="metrics">
        <Metric label={live.video?.preview_only ? "预览输出 FPS" : "摄像头实际 FPS"} value={(live.video?.fps ?? 0).toFixed(1)} />
        <Metric label="视频帧" value={live.video?.frame ?? 0} />
        <Metric label="IMU 通知包" value={live.imu?.packet_count ?? 0} />
        <Metric label="IMU 候选样本" value={live.imu?.sample_count ?? 0} />
        <Metric label="IMU 估算频率" value={`${(live.imu?.estimated_sample_rate_hz ?? 0).toFixed(2)} Hz`} />
        <Metric label="最后一包" value={live.imu?.last_packet_age_ms == null ? "—" : `${live.imu.last_packet_age_ms.toFixed(0)} ms 前`} warn={live.imu?.connected && (live.imu?.last_packet_age_ms ?? 0) > 2000} />
        <Metric label="BLE连接" value={live.imu?.connected ? "已连接" : "未连接"} warn={!live.imu?.connected} />
        <Metric label="解析/回调丢弃" value={`${live.imu?.parse_errors ?? 0} / ${live.imu?.callback_drops ?? 0}`} warn={(live.imu?.parse_errors ?? 0) > 0 || (live.imu?.callback_drops ?? 0) > 0} />
        <Metric label="剩余磁盘" value={`${(live.free_disk_gib ?? 0).toFixed(1)} GiB`} />
      </section>
      <section className="capture-grid">
        <div className="panel camera-panel">
          <div className="panel-title">实时画面 · 仅本机{devicesPreview ? " · 预览不落盘" : ""}</div>
          {active || devicesPreview ? <img key={live.session_type} src={`/api/v1/preview.mjpeg?mode=${live.session_type}`} alt="摄像头实时预览" /> : <div className="placeholder">连接预览设备后显示实时画面</div>}
        </div>
        <div className="panel chart-panel">
          <div className="panel-title">IMU 六轴实时曲线 · 最近 120 秒 · 当前为原始计数{devicesPreview ? " · 预览不落盘" : ""}</div>
          <Plot time={chart.t} values={chart.values} />
        </div>
      </section>
      {live.recording?.issues?.length > 0 && <div className="issues"><strong>上一次录制待办（不影响当前设备预览）</strong>{live.recording.issues.map((issue: string) => <div key={issue}>{issueLabel(issue)}</div>)}</div>}
      {live.preview_error && <div className="issues"><div>{live.preview_error}</div></div>}
    </main>
  );
}

function CharacterizationPage({ live, allowedUnikeys, chart }: { live: any; allowedUnikeys: string[]; chart: { t: number[]; values: number[][] } }) {
  const [operator, setOperator] = useState("xfan0282");
  const [stage, setStage] = useState(characterizationStages[0][0]);
  const [notes, setNotes] = useState("");
  const [history, setHistory] = useState<any[]>([]);
  const [error, setError] = useState("");
  const active = live.state === "recording" && live.session_type === "characterization";
  const currentStage = live.characterization?.current_stage;
  const busy = ["arming", "finalizing"].includes(live.state);

  const refresh = () => api<any[]>("/api/v1/characterizations").then(setHistory).catch((e) => setError(e.message));
  useEffect(() => { refresh(); }, []);
  useEffect(() => {
    if (!allowedUnikeys.includes(operator) && allowedUnikeys.length) setOperator(allowedUnikeys[0]);
  }, [allowedUnikeys]);

  const invoke = async (path: string, body?: unknown) => {
    setError("");
    try {
      await api(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });
      if (path === "/api/v1/characterizations/stop") refresh();
    } catch (e) { setError((e as Error).message); }
  };
  const selectedDescription = characterizationStages.find((item) => item[0] === stage);
  return <main>
    {error && <div className="error-banner">{error}</div>}
    <section className="panel characterization-intro">
      <div><div className="panel-title">设备坐标系与结论边界</div><p>+X 指向佩戴者右侧，+Y 指向头部/挂绳端，+Z 指向身体外侧/按键面。这里保存原始数据并生成候选报告；不会把未验证比例写入 SI，也不会进入训练集。</p></div>
      <div className="training-guard">training_eligible = false</div>
    </section>
    <section className="controls panel">
      <label>操作者 UniKey<select value={operator} disabled={active || busy} onChange={(e) => setOperator(e.target.value)}>{allowedUnikeys.map((item) => <option value={item} key={item}>{item}</option>)}</select></label>
      {!active ? <button className="primary" disabled={busy || live.state === "recording"} onClick={() => invoke("/api/v1/characterizations/start", { operator_id: operator, notes })}>开始 IMU-only 表征</button> : <button className="danger" onClick={() => invoke("/api/v1/characterizations/stop")}>结束并生成报告</button>}
    </section>
    <section className="metrics">
      <Metric label="BLE 连接" value={live.imu?.connected ? "已连接" : "未连接"} warn={!live.imu?.connected} />
      <Metric label="通知包" value={live.imu?.packet_count ?? 0} />
      <Metric label="候选样本" value={live.imu?.sample_count ?? 0} />
      <Metric label="回调丢弃" value={live.imu?.callback_drops ?? 0} warn={(live.imu?.callback_drops ?? 0) > 0} />
      <Metric label="当前阶段" value={currentStage?.stage_code ?? "未开始"} />
      <Metric label="训练资格" value="禁止" warn />
    </section>
    <section className="capture-grid">
      <div className="panel">
        <div className="panel-title">分阶段物理实验</div>
        <label>实验阶段<select value={stage} disabled={!active || !!currentStage} onChange={(e) => setStage(e.target.value as typeof stage)}>{characterizationStages.map((item) => <option value={item[0]} key={item[0]}>{item[1]}</option>)}</select></label>
        <p className="stage-help">{selectedDescription?.[2]}</p>
        <label>阶段备注<input value={notes} disabled={!active || !!currentStage} onChange={(e) => setNotes(e.target.value)} placeholder="夹具、摆放或异常说明" /></label>
        <div className="stage-actions">{!currentStage ? <button className="primary" disabled={!active} onClick={() => invoke("/api/v1/characterizations/stages/start", { stage_code: stage, notes })}>开始该阶段</button> : <button onClick={() => invoke("/api/v1/characterizations/stages/stop")}>结束该阶段</button>}</div>
      </div>
      <div className="panel chart-panel"><div className="panel-title">六轴实时原始计数 · 最近 120 秒</div><Plot time={chart.t} values={chart.values} /></div>
    </section>
    <section className="panel library"><div className="panel-title">历史表征报告</div>{history.length === 0 ? <span className="muted">尚无完整报告</span> : history.map((item) => <article key={item.report_path}><div><strong>{item.source_h5}</strong><span>{(item.observed_rate_hz ?? 0).toFixed(5)} Hz · {item.packet_count} 包 · {item.calibration_status}</span></div><div className="state state-needs_attention">仅诊断</div></article>)}</section>
  </main>;
}

function Metric({ label, value, warn = false }: { label: string; value: string | number; warn?: boolean }) {
  return <div className={`metric ${warn ? "warn" : ""}`}><span>{label}</span><strong>{value}</strong></div>;
}

function AnnotationPage({ recordings, taxonomy, allowedUnikeys }: { recordings: Recording[]; taxonomy: Taxonomy; allowedUnikeys: string[] }) {
  const [selected, setSelected] = useState("");
  const [doc, setDoc] = useState<AnnotationDocument | null>(null);
  const [timeline, setTimeline] = useState<{ time_s: number[]; values: number[][]; unit: string } | null>(null);
  const [frameTimes, setFrameTimes] = useState<FrameTimes | null>(null);
  const [currentFrame, setCurrentFrame] = useState(0);
  const [experiment, setExperiment] = useState<SyncExperimentDocument | null>(null);
  const [experimentWindow, setExperimentWindow] = useState<SyncWindow | null>(null);
  const [experimentImuSample, setExperimentImuSample] = useState<number | null>(null);
  const [experimentLabel, setExperimentLabel] = useState("tap_01");
  const [experimentBusy, setExperimentBusy] = useState(false);
  const [recommendationOffsetSource, setRecommendationOffsetSource] = useState<"formal_anchor" | "recording_affine" | "recording_offset" | "global_offset" | "none">("none");
  const [annotationKind, setAnnotationKind] = useState<"fall" | "non_fall" | "exclude">("non_fall");
  const [activity, setActivity] = useState(taxonomy.non_fall[0].code);
  const [exclusionReason, setExclusionReason] = useState<Exclusion["reason"]>("other");
  const [marks, setMarks] = useState<{ start?: number; end?: number; onset?: number; impact?: number }>({});
  const [currentTime, setCurrentTime] = useState(0);
  const [annotator, setAnnotator] = useState("xfan0282");
  const [sync, setSync] = useState<SyncState | null>(null);
  const [selectedImuTime, setSelectedImuTime] = useState<number | null>(null);
  const [syncRole, setSyncRole] = useState<"start_tap" | "end_tap">("start_tap");
  const [review, setReview] = useState<ReviewDocument | null>(null);
  const [error, setError] = useState("");
  const video = useRef<HTMLVideoElement>(null);

  useEffect(() => {
    if (!selected) return;
    const controller = new AbortController();
    setError("");
    setFrameTimes(null);
    Promise.all([
      api<AnnotationDocument>(`/api/v1/recordings/${selected}/annotations`, { signal: controller.signal }),
      api<{ time_s: number[]; values: number[][]; unit: string }>(`/api/v1/recordings/${selected}/timeline`, { signal: controller.signal }),
      api<SyncState>(`/api/v1/recordings/${selected}/sync`, { signal: controller.signal }),
      api<FrameTimes>(`/api/v1/recordings/${selected}/frame-times`, { signal: controller.signal }),
      api<SyncExperimentDocument>(`/api/v1/sync-experiments/${SYNC_EXPERIMENT_ID}`, { signal: controller.signal }),
      api<ReviewDocument>(`/api/v1/recordings/${selected}/review`, { signal: controller.signal })
    ]).then(([annotations, data, syncState, frames, experimentDocument, reviewDocument]) => {
      setDoc(annotations);
      setTimeline(data);
      setSync(syncState);
      setFrameTimes(frames);
      setExperiment(experimentDocument);
      setReview(reviewDocument);
      setMarks({});
      setCurrentFrame(0);
      setExperimentWindow(null);
      setExperimentImuSample(null);
      setRecommendationOffsetSource("none");
      const count = experimentDocument.observations.filter((item) => item.recording_id === selected).length;
      setExperimentLabel(`tap_${String(count + 1).padStart(2, "0")}`);
      const participant = recordings.find((item) => item.recording_id === selected)?.participant_id;
      if (participant && allowedUnikeys.includes(participant)) setAnnotator(participant);
    }).catch((e) => {
      if ((e as Error).name !== "AbortError") setError((e as Error).message);
    });
    return () => controller.abort();
  }, [selected]);

  useEffect(() => {
    if (!frameTimes?.frame_count || !video.current) return;
    const firstMediaTime = frameTimes.media_time_ns[0] / 1e9;
    video.current.currentTime = firstMediaTime;
    setCurrentFrame(0);
    setCurrentTime(frameTimes.time_ns[0] / 1e9);
  }, [frameTimes]);

  useEffect(() => {
    if (annotationKind === "exclude") return;
    const choices = taxonomy[annotationKind];
    setActivity(choices[0].code);
  }, [annotationKind]);

  const updateVideoPosition = (mediaTime: number) => {
    if (frameTimes) {
      const frame = nearestIndex(frameTimes.media_time_ns, Math.round(mediaTime * 1e9));
      setCurrentFrame(frame);
      setCurrentTime(frameTimes.time_ns[frame] / 1e9);
    }
  };

  const stepFrame = (delta: number) => {
    if (!frameTimes || !video.current) return;
    const target = Math.max(0, Math.min(frameTimes.frame_count - 1, currentFrame + delta));
    video.current.pause();
    video.current.currentTime = frameTimes.media_time_ns[target] / 1e9;
    setCurrentFrame(target);
    setCurrentTime(frameTimes.time_ns[target] / 1e9);
  };

  const loadExperimentWindow = async () => {
    if (!selected || !frameTimes) return;
    setExperimentBusy(true);
    try {
      const confirmedFormalAnchor = sync?.anchors.find((item) => item.role !== syncRole && item.role !== "legacy");
      const prior = confirmedFormalAnchor
        ? { value: confirmedFormalAnchor.video_time_ns - confirmedFormalAnchor.imu_time_ns, source: "formal_anchor" as const }
        : recommendOffset(
          experiment?.observations ?? [],
          selected,
          frameTimes.time_ns[currentFrame]
        );
      const query = new URLSearchParams({ frame_index: String(currentFrame), radius_seconds: "1.5" });
      if (prior.value !== null) query.set("expected_video_minus_imu_ns", String(Math.round(prior.value)));
      const window = await api<SyncWindow>(`/api/v1/recordings/${selected}/sync-window?${query.toString()}`);
      setExperimentWindow(window);
      setExperimentImuSample(window.recommendation.sample_index);
      setRecommendationOffsetSource(prior.source);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setExperimentBusy(false);
    }
  };

  const persistExperiment = async (observations: SyncObservation[]) => {
    if (!experiment) return;
    setExperimentBusy(true);
    try {
      const saved = await api<SyncExperimentDocument>(`/api/v1/sync-experiments/${SYNC_EXPERIMENT_ID}`, {
        method: "PUT",
        body: JSON.stringify({ ...experiment, observations })
      });
      setExperiment(saved);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setExperimentBusy(false);
    }
  };

  const saveExperimentObservation = async () => {
    if (!selected || !experiment || !experimentWindow || experimentImuSample === null) return;
    const localIndex = experimentWindow.sample_index.indexOf(experimentImuSample);
    if (localIndex < 0) return;
    const selectedCandidate = experimentWindow.candidate_peaks.find((item) => item.sample_index === experimentImuSample);
    if (!selectedCandidate) return;
    const existingIds = new Set(experiment.observations.map((item) => item.observation_id));
    let ordinal = 1;
    let observationId = `${selected}_tap_${String(ordinal).padStart(2, "0")}`;
    while (existingIds.has(observationId)) {
      ordinal += 1;
      observationId = `${selected}_tap_${String(ordinal).padStart(2, "0")}`;
    }
    const observation: SyncObservation = {
      observation_id: observationId,
      recording_id: selected,
      video_frame_index: experimentWindow.video_frame_index,
      video_time_ns: experimentWindow.video_time_ns,
      imu_sample_index: experimentImuSample,
      imu_time_ns: experimentWindow.time_ns[localIndex],
      label: experimentLabel || `tap_${String(ordinal).padStart(2, "0")}`,
      reviewer_id: annotator,
      notes: "",
      selection_mode: experimentImuSample === experimentWindow.recommendation.sample_index ? "auto_recommended" : "manual_candidate",
      recommendation_algorithm: experimentWindow.recommendation.algorithm,
      recommended_sample_index: experimentWindow.recommendation.sample_index,
      recommendation_confidence: experimentWindow.recommendation.confidence,
      candidate_strength_rank: selectedCandidate.strength_rank,
      candidate_score: selectedCandidate.recommendation_score,
      expected_video_minus_imu_ns: experimentWindow.recommendation.expected_video_minus_imu_ns
    };
    await persistExperiment([...experiment.observations, observation]);
    setExperimentLabel(`tap_${String(ordinal + 1).padStart(2, "0")}`);
    setExperimentWindow(null);
    setExperimentImuSample(null);
  };

  const confirmFormalAnchor = () => {
    if (!sync || !experimentWindow || experimentImuSample === null || !frameTimes) return;
    const localIndex = experimentWindow.sample_index.indexOf(experimentImuSample);
    if (localIndex < 0) return;
    const anchor: SyncAnchor = {
      imu_time_ns: experimentWindow.time_ns[localIndex],
      video_time_ns: experimentWindow.video_time_ns,
      label: syncRole === "start_tap" ? "开始轻拍" : "结束轻拍",
      role: syncRole,
      source_video_frame: experimentWindow.video_frame_index,
      source_imu_sample: experimentImuSample,
      video_interval_start_ns: frameTimes.time_ns[Math.max(0, experimentWindow.video_frame_index - 1)],
      imu_interval_start_ns: experimentWindow.time_ns[Math.max(0, localIndex - 1)],
      reviewer_id: annotator
    };
    const anchors = [
      ...sync.anchors.filter(
        (item) => item.role !== syncRole && item.role !== "legacy"
      ),
      anchor
    ]
      .sort((a, b) => a.video_time_ns - b.video_time_ns);
    setSync({
      ...sync,
      anchors,
      quality: "draft",
      recommendation: "none",
      decision: "host_only"
    });
    setSyncRole(syncRole === "start_tap" ? "end_tap" : "start_tap");
    setExperimentWindow(null);
    setExperimentImuSample(null);
  };

  const mark = (name: "start" | "end" | "onset" | "impact") => {
    setMarks((existing) => ({ ...existing, [name]: Math.round(currentTime * 1e9) }));
  };

  const addAnnotationInterval = () => {
    if (!doc || marks.start === undefined || marks.end === undefined) return;
    if (annotationKind === "exclude") {
      let ordinal = doc.exclusions.length + 1;
      const existing = new Set(doc.exclusions.map((item) => item.exclusion_id));
      while (existing.has(`exc_${String(ordinal).padStart(3, "0")}`)) ordinal += 1;
      const exclusion: Exclusion = {
        exclusion_id: `exc_${String(ordinal).padStart(3, "0")}`,
        start_ns: marks.start,
        end_ns: marks.end,
        reason: exclusionReason,
        annotator_id: annotator,
        notes: ""
      };
      setDoc({
        ...doc,
        exclusions: [...doc.exclusions, exclusion].sort((a, b) => a.start_ns - b.start_ns)
      });
      setMarks({});
      return;
    }
    let ordinal = doc.segments.length + 1;
    const existing = new Set(doc.segments.map((item) => item.segment_id));
    while (existing.has(`seg_${String(ordinal).padStart(3, "0")}`)) ordinal += 1;
    const segmentId = `seg_${String(ordinal).padStart(3, "0")}`;
    const segment: Segment = {
      segment_id: segmentId,
      start_ns: marks.start,
      end_ns: marks.end,
      binary_label: annotationKind,
      activity_code: activity,
      annotator_id: annotator,
      confidence: 1,
      notes: ""
    };
    const events = [...doc.events];
    if (annotationKind === "fall" && marks.onset !== undefined && marks.impact !== undefined) {
      events.push(
        { segment_id: segmentId, kind: "onset", time_ns: marks.onset, source_video_frame: null, source_imu_sample: null, annotator_id: annotator },
        { segment_id: segmentId, kind: "impact", time_ns: marks.impact, source_video_frame: null, source_imu_sample: null, annotator_id: annotator }
      );
    }
    setDoc({ ...doc, segments: [...doc.segments, segment].sort((a, b) => a.start_ns - b.start_ns), events });
    setMarks({});
  };

  const save = async (finalized: boolean) => {
    if (!doc || !selected) return;
    setError("");
    try {
      const saved = await api<AnnotationDocument>(`/api/v1/recordings/${selected}/annotations`, {
        method: "PUT",
        body: JSON.stringify({ ...doc, revision: doc.revision + 1, finalized })
      });
      setDoc(saved);
      setReview(await api<ReviewDocument>(`/api/v1/recordings/${selected}/review`));
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const changeWorkflow = async (
    action: "assign" | "submit" | "accept" | "reject" | "reopen"
  ) => {
    if (!selected || !review) return;
    const comment = action === "reject" || action === "reopen"
      ? window.prompt(action === "reject" ? "请输入驳回原因" : "请输入重开原因") ?? ""
      : "";
    if ((action === "reject" || action === "reopen") && !comment.trim()) return;
    setError("");
    try {
      const saved = await api<ReviewDocument>(`/api/v1/recordings/${selected}/workflow`, {
        method: "POST",
        body: JSON.stringify({
          action,
          actor_id: annotator,
          expected_revision: review.revision,
          comment
        })
      });
      setReview(saved);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const choices = annotationKind === "exclude" ? [] : taxonomy[annotationKind];
  const saveSync = async (applyFixedOffset = false) => {
    if (!sync || !selected) return;
    setError("");
    try {
      const model = await api<Omit<SyncState, "anchors">>(`/api/v1/recordings/${selected}/sync`, {
        method: "PUT",
        body: JSON.stringify({
          anchors: sync.anchors,
          policy: "conditional_fixed_offset_v1",
          apply_fixed_offset: applyFixedOffset,
          reviewer_id: annotator,
          expected_revision: review?.revision
        })
      });
      const timelineData = await api<{ time_s: number[]; values: number[][]; unit: string }>(`/api/v1/recordings/${selected}/timeline`);
      setSync({ ...sync, ...model });
      setTimeline(timelineData);
      setReview(await api<ReviewDocument>(`/api/v1/recordings/${selected}/review`));
    } catch (e) { setError((e as Error).message); }
  };

  const proposeTapExclusions = () => {
    if (!doc || !sync || !selected) return;
    const duration = recordings.find((item) => item.recording_id === selected)?.duration_ns ?? 0;
    const start = sync.anchors.find((item) => item.role === "start_tap");
    const end = sync.anchors.find((item) => item.role === "end_tap");
    if (!duration || !start || !end) return;
    const proposed = ([
      {
        exclusion_id: "sync_start_guard",
        start_ns: 0,
        end_ns: Math.min(duration, start.video_time_ns + 1_000_000_000),
        reason: "sync_tap",
        annotator_id: annotator,
        notes: "开始轻拍及录制设置保护区"
      },
      {
        exclusion_id: "sync_end_guard",
        start_ns: Math.max(0, end.video_time_ns - 500_000_000),
        end_ns: duration,
        reason: "sync_tap",
        annotator_id: annotator,
        notes: "结束轻拍及停止录制保护区"
      }
    ] satisfies Exclusion[]).filter((item) => item.end_ns > item.start_ns);
    const retained = doc.exclusions.filter(
      (item) => item.exclusion_id !== "sync_start_guard" && item.exclusion_id !== "sync_end_guard"
    );
    setDoc({ ...doc, exclusions: [...retained, ...proposed].sort((a, b) => a.start_ns - b.start_ns) });
  };

  const selectedExperimentTime = experimentWindow && experimentImuSample !== null
    ? experimentWindow.time_s[experimentWindow.sample_index.indexOf(experimentImuSample)]
    : undefined;
  const selectedExperimentIsCandidate = experimentWindow && experimentImuSample !== null
    ? experimentWindow.candidate_sample_index.includes(experimentImuSample)
    : false;
  const selectedExperimentCandidate = experimentWindow?.candidate_peaks.find((item) => item.sample_index === experimentImuSample);
  const recommendedExperimentTime = experimentWindow?.recommendation.sample_index == null
    ? undefined
    : experimentWindow.time_s[
      experimentWindow.sample_index.indexOf(experimentWindow.recommendation.sample_index)
    ];
  const experimentMarkers = selectedExperimentTime === undefined ? [] : [
    {
      time: selectedExperimentTime,
      label: experimentImuSample === experimentWindow?.recommendation.sample_index
        ? "当前选择＝自动推荐"
        : "当前选择",
      color: "#facc15",
      showPoints: true
    },
    ...(recommendedExperimentTime !== undefined && experimentImuSample !== experimentWindow?.recommendation.sample_index
      ? [{
        time: recommendedExperimentTime,
        label: "自动推荐",
        color: "#22d3ee",
        dashed: true
      }]
      : [])
  ];
  const recordingObservations = experiment?.observations.filter((item) => item.recording_id === selected) ?? [];
  const selectedRecording = recordings.find((item) => item.recording_id === selected);
  const durationNs = selectedRecording?.duration_ns ?? 0;
  const coverageIntervals = doc ? [
    ...doc.segments.map((item) => ({ start: item.start_ns, end: item.end_ns })),
    ...doc.exclusions.map((item) => ({ start: item.start_ns, end: item.end_ns }))
  ].sort((a, b) => a.start - b.start) : [];
  const coverageGaps: { start: number; end: number }[] = [];
  let coverageEnd = 0;
  for (const interval of coverageIntervals) {
    if (interval.start > coverageEnd) coverageGaps.push({ start: coverageEnd, end: interval.start });
    coverageEnd = Math.max(coverageEnd, interval.end);
  }
  if (durationNs > coverageEnd) coverageGaps.push({ start: coverageEnd, end: durationNs });
  const uncoveredNs = coverageGaps.reduce((total, item) => total + item.end - item.start, 0);
  const hasFormalAnchors = sync?.anchors.some((item) => item.role === "start_tap")
    && sync.anchors.some((item) => item.role === "end_tap");

  return (
    <main className="annotation-layout">
      <aside className="panel recording-list">
        <div className="panel-title">选择录制</div>
        {recordings.map((recording) => <button key={recording.recording_id} className={selected === recording.recording_id ? "selected" : ""} onClick={() => setSelected(recording.recording_id)}><strong>{recording.participant_id}</strong><span>{recording.recording_id}</span></button>)}
      </aside>
      <section className="annotation-workspace">
        {error && <div className="error-banner">{error}</div>}
        {!selected ? <div className="panel placeholder">从左侧选择一段录制</div> : <>
          <div className="panel video-review">
            <video
              ref={video}
              controls
              tabIndex={0}
              src={`/api/v1/recordings/${selected}/video`}
              onTimeUpdate={(event) => updateVideoPosition(event.currentTarget.currentTime)}
              onSeeked={(event) => updateVideoPosition(event.currentTarget.currentTime)}
              onKeyDown={(event) => {
                if (event.key === "ArrowLeft" || event.key === ",") { event.preventDefault(); stepFrame(-1); }
                if (event.key === "ArrowRight" || event.key === ".") { event.preventDefault(); stepFrame(1); }
                if (event.key.toLowerCase() === "i") { event.preventDefault(); mark("start"); }
                if (event.key.toLowerCase() === "o") { event.preventDefault(); mark("end"); }
                if (event.key === "1" && annotationKind === "fall") { event.preventDefault(); mark("onset"); }
                if (event.key === "2" && annotationKind === "fall") { event.preventDefault(); mark("impact"); }
                if (event.key.toLowerCase() === "f") setAnnotationKind("fall");
                if (event.key.toLowerCase() === "n") setAnnotationKind("non_fall");
                if (event.key.toLowerCase() === "x") setAnnotationKind("exclude");
                if (event.ctrlKey && event.key.toLowerCase() === "s") { event.preventDefault(); save(false); }
              }}
            />
            <div className="frame-controls">
              <button onClick={() => stepFrame(-1)} disabled={!frameTimes || currentFrame <= 0}>上一帧</button>
              <span>零基帧 {currentFrame} / {frameTimes ? frameTimes.frame_count - 1 : "—"}</span>
              <span>录制时间 {frameTimes ? (frameTimes.time_ns[currentFrame] / 1e9).toFixed(9) : "—"} s</span>
              <span>媒体 PTS {frameTimes ? (frameTimes.media_time_ns[currentFrame] / 1e9).toFixed(9) : "—"} s</span>
              <button onClick={() => stepFrame(1)} disabled={!frameTimes || currentFrame >= frameTimes.frame_count - 1}>下一帧</button>
              <button className="primary" onClick={loadExperimentWindow} disabled={!frameTimes || experimentBusy}>此帧是{syncRole === "start_tap" ? "开始" : "结束"}轻拍首次接触</button>
            </div>
            <p className="stage-help">先暂停到手首次接触 IMU 的画面，再逐帧确认。键盘 ←/→ 或 ,/. 可逐帧移动；帧号从 0 开始。</p>
          </div>

          <div className="panel sync-experiment-panel">
            <div className="panel-title">第 1 步 · 开始/结束轻拍同步复核</div>
            <div className="sync-inputs">
              <label>当前锚点<select value={syncRole} onChange={(event) => setSyncRole(event.target.value as "start_tap" | "end_tap")}><option value="start_tap">开始轻拍</option><option value="end_tap">结束轻拍</option></select></label>
              <span>视频人工选择首次接触帧；程序推荐 IMU 首次响应，仍需人工确认。</span>
            </div>
            {!experimentWindow ? <div className="placeholder compact">逐帧停在轻拍首次接触画面，再点击上方按钮加载附近 IMU 样本</div> : <>
              <div className="sync-inputs">
                <span>视频帧 {experimentWindow.video_frame_index}</span>
                <span>视频 PTS {(experimentWindow.video_time_ns / 1e9).toFixed(9)} s</span>
                <span>IMU {experimentImuSample === null ? "未选择" : `样本 ${experimentImuSample} · ${selectedExperimentTime?.toFixed(9)} s`}</span>
              </div>
              <div className={`recommendation-card confidence-${experimentWindow.recommendation.confidence}`}>
                <strong>自动推荐：{experimentWindow.recommendation.sample_index === null ? "无候选" : `样本 ${experimentWindow.recommendation.sample_index}`}</strong>
                <span>置信度：{experimentWindow.recommendation.confidence === "high" ? "高" : experimentWindow.recommendation.confidence === "medium" ? "中" : "低"}</span>
                <span>时间先验：{recommendationOffsetSource === "formal_anchor" ? "本条已确认的另一端锚点" : recommendationOffsetSource === "recording_affine" ? "历史实验本条线性拟合" : recommendationOffsetSource === "recording_offset" ? "历史实验本条固定偏移" : recommendationOffsetSource === "global_offset" ? "历史实验跨录制偏移" : "无"}</span>
                <span>{experimentWindow.recommendation.reason}</span>
              </div>
              <Plot
                time={experimentWindow.time_s}
                values={experimentWindow.raw_counts}
                cursorTime={selectedExperimentTime}
                markers={experimentMarkers}
                height={280}
                onSelectTime={(time) => {
                  const candidates = experimentWindow.candidate_sample_index;
                  if (candidates.length) {
                    const candidateTimes = candidates.map((sampleIndex) => {
                      const localIndex = experimentWindow.sample_index.indexOf(sampleIndex);
                      return experimentWindow.time_s[localIndex];
                    });
                    setExperimentImuSample(candidates[nearestIndex(candidateTimes, time)]);
                  } else {
                    const localIndex = nearestIndex(experimentWindow.time_s, time);
                    setExperimentImuSample(experimentWindow.sample_index[localIndex]);
                  }
                }}
              />
              <div className="candidate-peaks"><span>轻拍响应候选：</span>{experimentWindow.candidate_peaks.map((candidate) => <button key={candidate.sample_index} className={experimentImuSample === candidate.sample_index ? "selected" : ""} onClick={() => setExperimentImuSample(candidate.sample_index)}>样本 {candidate.sample_index} · {candidate.time_s.toFixed(3)} s · {candidate.selection_basis === "event_onset" ? "首响应" : candidate.selection_basis === "local_peak" ? "局部峰" : "时间投影"} · 强度#{candidate.strength_rank} · 偏移 {candidate.video_minus_imu_ms.toFixed(1)} ms</button>)}</div>
              <div className="save-row">
                <button className="primary" disabled={experimentImuSample === null || !selectedExperimentIsCandidate || experimentBusy} onClick={confirmFormalAnchor}>确认为{syncRole === "start_tap" ? "开始" : "结束"}轻拍</button>
                <button disabled={experimentBusy} onClick={() => { setExperimentWindow(null); setExperimentImuSample(null); }}>取消</button>
              </div>
              {selectedExperimentCandidate && <p className="stage-help">当前候选：事件显著性 {selectedExperimentCandidate.event_robust_z.toFixed(1)} · 本样本突变 {selectedExperimentCandidate.robust_z.toFixed(1)} · 推荐分数 {selectedExperimentCandidate.recommendation_score.toFixed(3)} · 推荐排名 #{selectedExperimentCandidate.recommendation_rank} · {selectedExperimentCandidate.selection_basis === "event_onset" ? "事件首个明显响应" : selectedExperimentCandidate.selection_basis === "timing_projection" ? "时间模型代表样本" : "局部突变峰"}</p>}
              {experimentImuSample !== null && !selectedExperimentIsCandidate && <p className="stage-help warning-text">当前选择不是加速度突变候选，请点击曲线主峰或上方候选按钮。</p>}
              {experimentWindow.recommendation.confidence === "low" && <p className="stage-help warning-text">自动推荐置信度低，请逐个比较候选；程序不会自动保存。</p>}
            </>}
            <div className="anchor-list">
              {sync?.anchors.filter((item) => item.role !== "legacy").map((anchor) => <div key={anchor.role}>
                <span>{anchor.role === "start_tap" ? "开始轻拍" : "结束轻拍"} · 帧 {anchor.source_video_frame ?? "—"} ({seconds(anchor.video_time_ns)}) ↔ 样本 {anchor.source_imu_sample ?? "—"} ({seconds(anchor.imu_time_ns)})</span>
                <button onClick={() => setSync({ ...sync, anchors: sync.anchors.filter((item) => item.role !== anchor.role), quality: "draft", recommendation: "none", decision: "host_only" })}>删除</button>
              </div>)}
            </div>
            <details>
              <summary>高级信息 · 历史同步实验观察</summary>
              <div className="observation-list">
                {recordingObservations.length === 0 ? <span className="muted">本条没有历史实验观察</span> : recordingObservations.map((observation) => <div key={observation.observation_id}><span>{observation.label} · 帧 {observation.video_frame_index} ↔ 样本 {observation.imu_sample_index}</span></div>)}
              </div>
            </details>
          </div>

          <div className="panel"><div className="panel-title">完整录制 IMU · {timeline?.unit} · {timeline?.time_s.length ?? 0} 个显示点 · 正式标注与同步复核</div>{timeline && <Plot time={timeline.time_s} values={timeline.values} cursorTime={currentTime} height={250} onSelectTime={setSelectedImuTime} />}</div>
          <div className="panel sync-panel">
            <div className="panel-title">同步结论 · 条件式固定偏移</div>
            <p className="stage-help">时间比例固定为 1.0；只有估计偏移至少 0.1 秒且首尾差不超过 0.1 秒时，才建议平移 IMU。原始主机时间永不覆盖。</p>
            <div className={`recommendation-card confidence-${sync?.quality === "verified" ? "high" : "low"}`}>
              <strong>{sync?.quality === "verified" ? "同步已验证" : sync?.quality === "awaiting_confirmation" ? "等待确认固定偏移" : sync?.quality === "needs_review" ? "需要重新检查锚点" : sync?.quality === "rejected" ? "同步已拒绝" : "尚未评估"}</strong>
              <span>估计偏移：{sync ? `${sync.estimated_offset_seconds >= 0 ? "+" : ""}${sync.estimated_offset_seconds.toFixed(3)} 秒` : "—"}</span>
              <span>约 {sync?.estimated_offset_video_frames == null ? "—" : `${sync.estimated_offset_video_frames >= 0 ? "+" : ""}${sync.estimated_offset_video_frames.toFixed(2)} 个视频帧`}</span>
              <span>约 {sync?.estimated_offset_imu_samples == null ? "—" : `${sync.estimated_offset_imu_samples >= 0 ? "+" : ""}${sync.estimated_offset_imu_samples.toFixed(2)} 个 IMU 样本`}</span>
              <span>首尾差：{sync ? `${(sync.anchor_disagreement_ns / 1e9).toFixed(3)} 秒` : "—"}</span>
              <span>已应用：{sync ? `${sync.applied_offset_seconds >= 0 ? "+" : ""}${sync.applied_offset_seconds.toFixed(3)} 秒` : "—"}</span>
              <span>“+”表示把 IMU 时间轴向后移动；视频帧数按本条实际 FPS 估算。</span>
            </div>
            <div className="save-row">
              <button className="primary" disabled={!hasFormalAnchors} onClick={() => saveSync(false)}>评估并保存主机时间</button>
              {sync?.recommendation === "apply_fixed_offset" && <button className="danger" onClick={() => saveSync(true)}>确认应用固定偏移</button>}
              <button disabled={!hasFormalAnchors || !doc} onClick={proposeTapExclusions}>生成轻拍排除区</button>
            </div>
            <details>
              <summary>高级同步数据</summary>
              <p className="stage-help">开始偏移 {sync ? seconds(sync.start_offset_ns) : "—"}；结束偏移 {sync ? seconds(sync.end_offset_ns) : "—"}；残差 RMS {sync && Number.isFinite(sync.residual_rms_ns) ? `${(sync.residual_rms_ns / 1e6).toFixed(2)} ms` : "—"}；策略 {sync?.policy ?? "—"}。</p>
            </details>
          </div>
          <div className="panel annotation-controls">
            <div className="panel-title">第 2 步 · 区间与跌倒事件标注</div>
            <div className="time-readout">当前 {currentTime.toFixed(3)} s · 帧 {currentFrame}</div>
            <label>标注者 UniKey<select value={annotator} onChange={(e) => setAnnotator(e.target.value)}>{allowedUnikeys.map((item) => <option value={item} key={item}>{item}</option>)}</select></label>
            <div className="mark-buttons"><button onClick={() => mark("start")}>标记区间开始（I）</button><button onClick={() => mark("end")}>标记区间结束（O）</button><button disabled={annotationKind !== "fall"} onClick={() => mark("onset")}>标记跌倒起始（1）</button><button disabled={annotationKind !== "fall"} onClick={() => mark("impact")}>标记撞击时刻（2）</button></div>
            <div className="marks"><span>动作开始 {seconds(marks.start)}</span><span>动作结束 {seconds(marks.end)}</span><span>跌倒起始 {seconds(marks.onset)}</span><span>撞击时刻 {seconds(marks.impact)}</span></div>
            <div className="segment-form"><select value={annotationKind} onChange={(e) => setAnnotationKind(e.target.value as typeof annotationKind)}><option value="non_fall">非跌倒 · 进入训练</option><option value="fall">跌倒 · 进入训练</option><option value="exclude">明确排除 · 不训练</option></select>{annotationKind === "exclude" ? <select value={exclusionReason} onChange={(e) => setExclusionReason(e.target.value as Exclusion["reason"])}>{Object.entries(exclusionLabels).map(([value, display]) => <option value={value} key={value}>{display}</option>)}</select> : <select value={activity} onChange={(e) => setActivity(e.target.value)}>{choices.map((item) => <option value={item.code} key={item.code}>{item.display_name_zh} · {item.code}</option>)}</select>}<button className="primary" onClick={addAnnotationInterval}>添加区间</button></div>
            {annotationKind === "fall" && <p className="stage-help">fall 从首次明确失衡开始，经过撞击，到落地后身体大动作停止并稳定。准备阶段和稳定后的自然状态另标 non_fall。</p>}
          </div>
          {doc && <div className="panel segment-table">
            <div className="panel-title">第 3 步 · 全时间轴覆盖与完成检查</div>
            <div className="coverage-track" aria-label="标注覆盖时间轴">
              {durationNs > 0 && doc.segments.map((segment) => <span key={segment.segment_id} className={`coverage-block coverage-${segment.binary_label}`} title={`${segment.segment_id} ${seconds(segment.start_ns)} → ${seconds(segment.end_ns)}`} style={{ left: `${segment.start_ns / durationNs * 100}%`, width: `${(segment.end_ns - segment.start_ns) / durationNs * 100}%` }} />)}
              {durationNs > 0 && doc.exclusions.map((item) => <span key={item.exclusion_id} className="coverage-block coverage-exclude" title={`${exclusionLabels[item.reason]} ${seconds(item.start_ns)} → ${seconds(item.end_ns)}`} style={{ left: `${item.start_ns / durationNs * 100}%`, width: `${(item.end_ns - item.start_ns) / durationNs * 100}%` }} />)}
              {durationNs > 0 && <span className="coverage-cursor" style={{ left: `${Math.max(0, Math.min(100, currentTime * 1e9 / durationNs * 100))}%` }} />}
            </div>
            <div className={`coverage-summary ${uncoveredNs > 0 ? "warning-text" : "success-text"}`}>{uncoveredNs > 0 ? `仍有 ${(uncoveredNs / 1e9).toFixed(3)} 秒未标注；草稿允许，完成时禁止。` : "全程已由 fall、non_fall 或 exclude 覆盖。"}</div>
            {coverageGaps.length > 0 && <div className="gap-list">{coverageGaps.slice(0, 10).map((gap, index) => <button key={`${gap.start}-${gap.end}`} onClick={() => { setMarks({ start: gap.start, end: gap.end }); if (video.current && frameTimes) video.current.currentTime = frameTimes.media_time_ns[nearestIndex(frameTimes.time_ns, gap.start)] / 1e9; }}>空白 {index + 1}：{seconds(gap.start)} → {seconds(gap.end)}</button>)}</div>}
            <div className="panel-title subheading">训练区间</div>
            {doc.segments.map((segment) => <div className="segment-row" key={segment.segment_id}><span>{segment.segment_id}</span><strong>{segment.binary_label === "fall" ? "跌倒" : "非跌倒"}</strong><span>{segment.activity_code}</span><span>{seconds(segment.start_ns)} → {seconds(segment.end_ns)}</span><button onClick={() => setDoc({ ...doc, segments: doc.segments.filter((item) => item.segment_id !== segment.segment_id), events: doc.events.filter((event) => event.segment_id !== segment.segment_id) })}>删除</button></div>)}
            <div className="panel-title subheading">明确排除区间</div>
            {doc.exclusions.map((item) => <div className="segment-row" key={item.exclusion_id}><span>{item.exclusion_id}</span><strong>排除</strong><span>{exclusionLabels[item.reason]}</span><span>{seconds(item.start_ns)} → {seconds(item.end_ns)}</span><button onClick={() => setDoc({ ...doc, exclusions: doc.exclusions.filter((candidate) => candidate.exclusion_id !== item.exclusion_id) })}>删除</button></div>)}
            <div className="save-row"><button onClick={() => save(false)}>保存草稿（Ctrl+S）</button><button className="primary" disabled={uncoveredNs > 0 || sync?.quality !== "verified"} onClick={() => save(true)}>完成并验证标注</button><span>修订 {doc.revision} · {doc.finalized ? "已定稿" : "草稿"} · 同步 {sync?.quality ?? "missing"}</span></div>
          </div>}
          {review && <div className="panel workflow-panel">
            <div className="panel-title">第 4 步 · 提交与异人审核</div>
            <p>当前状态：<strong>{review.workflow.state}</strong> · review revision {review.revision} · 标注者 {review.workflow.annotator_id ?? "未分配"} · 审核者 {review.workflow.reviewer_id ?? "未分配"}</p>
            {review.workflow.review_comment && <p className="stage-help">审核意见：{review.workflow.review_comment}</p>}
            <div className="save-row">
              <button disabled={!['unassigned', 'in_progress'].includes(review.workflow.state)} onClick={() => changeWorkflow("assign")}>以当前 UniKey 领取</button>
              <button className="primary" disabled={review.workflow.state !== "in_progress" || review.workflow.annotator_id !== annotator || !doc?.finalized} onClick={() => changeWorkflow("submit")}>提交审核</button>
              <button className="primary" disabled={review.workflow.state !== "submitted" || review.workflow.annotator_id === annotator} onClick={() => changeWorkflow("accept")}>审核通过</button>
              <button disabled={review.workflow.state !== "submitted" || review.workflow.annotator_id === annotator} onClick={() => changeWorkflow("reject")}>驳回</button>
              <button disabled={!['accepted', 'exported'].includes(review.workflow.state)} onClick={() => changeWorkflow("reopen")}>管理员重开</button>
            </div>
          </div>}
        </>}
      </section>
    </main>
  );
}

function Library({ recordings, onChanged }: { recordings: Recording[]; onChanged: () => void }) {
  const [statuses, setStatuses] = useState<Record<string, RecordingStatus>>({});
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [incomplete, setIncomplete] = useState<{ relative_path: string; size_bytes: number; reason: string }[]>([]);

  const refreshStatuses = async () => {
    const entries = await Promise.all(recordings.map(async (recording) => [
      recording.recording_id,
      await api<RecordingStatus>(`/api/v1/recordings/${recording.recording_id}/status`)
    ] as const));
    setStatuses(Object.fromEntries(entries));
  };

  useEffect(() => {
    let active = true;
    refreshStatuses().catch((e) => { if (active) setError(e.message); });
    return () => { active = false; };
  }, [recordings]);

  const createPackage = async (recordingId: string) => {
    setError("");
    setMessage("");
    try {
      const estimate = await api<{ estimated_bytes: number }>(`/api/v1/recordings/${recordingId}/capture-package/estimate`);
      const gib = estimate.estimated_bytes / 1024 ** 3;
      if (!window.confirm(`预计需要 ${gib.toFixed(2)} GiB 额外空间，继续生成无压缩源包？`)) return;
      const result = await api<{ path: string }>(`/api/v1/recordings/${recordingId}/capture-package`, { method: "POST" });
      setMessage(`源包已生成：${result.path}`);
    } catch (e) { setError((e as Error).message); }
  };

  const exportAligned = async (recordingId: string) => {
    const status = statuses[recordingId];
    if (!status) return;
    setError("");
    setMessage("");
    try {
      const result = await api<{ path: string }>(`/api/v1/recordings/${recordingId}/aligned30`, {
        method: "POST",
        body: JSON.stringify({ expected_revision: status.review_revision })
      });
      setMessage(`30 Hz 训练文件已生成：${result.path}`);
      await refreshStatuses();
    } catch (e) { setError((e as Error).message); }
  };

  const deleteRecording = async (recordingId: string) => {
    const confirmation = window.prompt(`永久删除不可恢复。请输入完整 recording_id：\n${recordingId}`);
    if (confirmation === null) return;
    setError("");
    try {
      await api(`/api/v1/recordings/${recordingId}`, {
        method: "DELETE",
        body: JSON.stringify({ confirmation })
      });
      setMessage(`已永久删除本地录制：${recordingId}`);
      onChanged();
    } catch (e) { setError((e as Error).message); }
  };

  const scanIncomplete = async () => {
    setError("");
    try {
      setIncomplete(await api("/api/v1/maintenance/incomplete"));
    } catch (e) { setError((e as Error).message); }
  };

  const quarantine = async (relativePath: string) => {
    setError("");
    try {
      await api("/api/v1/maintenance/quarantine", {
        method: "POST",
        body: JSON.stringify({ relative_path: relativePath })
      });
      await scanIncomplete();
    } catch (e) { setError((e as Error).message); }
  };

  const rebuild = async () => {
    setError("");
    try {
      const result = await api<{ imported: number; skipped: number }>("/api/v1/maintenance/rebuild-catalog", { method: "POST" });
      setMessage(`目录重建完成：导入 ${result.imported}，跳过 ${result.skipped}`);
      onChanged();
    } catch (e) { setError((e as Error).message); }
  };

  const createRelease = async () => {
    setError("");
    try {
      const result = await api<{ path: string }>("/api/v1/training-releases", { method: "POST" });
      setMessage(`不可变训练发布已生成：${result.path}`);
    } catch (e) { setError((e as Error).message); }
  };

  return <main>
    {error && <div className="error-banner">{error}</div>}
    {message && <div className="success-banner">{message}</div>}
    <section className="panel library">
      <div className="panel-title">本地录制与独立状态</div>
      {recordings.map((recording) => {
        const status = statuses[recording.recording_id];
        return <article key={recording.recording_id}>
          <div><strong>{recording.recording_id}</strong><span>{recording.collection_id} · {recording.participant_id} · {recording.data_tier} · {seconds(recording.duration_ns)}</span></div>
          <div className="status-grid">
            <span>采集 {status?.capture ?? recording.state}</span>
            <span>同步 {status?.sync ?? "—"}</span>
            <span>标注 {status?.annotation ?? "—"}</span>
            <span>校准 {status?.calibration ?? "—"}</span>
            <span>导出 {status?.export ?? "—"}</span>
          </div>
          <div className="save-row">
            <button onClick={() => createPackage(recording.recording_id)}>估算并生成源包</button>
            <button onClick={() => exportAligned(recording.recording_id)}>导出 aligned30.h5</button>
            <button className="danger" onClick={() => deleteRecording(recording.recording_id)}>永久删除</button>
          </div>
          {recording.issues.length > 0 && <ul>{recording.issues.map((issue) => <li key={issue}>{issueLabel(issue)}</li>)}</ul>}
        </article>;
      })}
    </section>
    <section className="panel library">
      <div className="panel-title">本地维护</div>
      <div className="save-row"><button onClick={scanIncomplete}>扫描不完整文件</button><button onClick={rebuild}>从目录重建索引</button><button onClick={createRelease}>生成训练发布 TAR</button></div>
      {incomplete.length === 0 ? <span className="muted">尚未扫描，或未发现不完整文件。</span> : incomplete.map((item) => <article key={item.relative_path}><div><strong>{item.relative_path}</strong><span>{item.reason} · {(item.size_bytes / 1024 ** 2).toFixed(2)} MiB</span></div><button onClick={() => quarantine(item.relative_path)}>隔离</button></article>)}
    </section>
  </main>;
}
