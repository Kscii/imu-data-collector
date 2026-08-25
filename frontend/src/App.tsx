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

type AnnotationDocument = {
  taxonomy_id: string;
  taxonomy_version: string;
  revision: number;
  finalized: boolean;
  segments: Segment[];
  events: Event[];
};

type Taxonomy = {
  taxonomy_id: string;
  version: string;
  fall: { code: string; display_name_zh: string }[];
  non_fall: { code: string; display_name_zh: string }[];
};

type AppConfig = {
  allowed_unikeys: string[];
  data_tiers: ("test" | "prod")[];
  default_data_tier: "test" | "prod";
  data_root: string;
  video: { width: number; height: number; requested_fps: number; bitrate: string };
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

type SyncAnchor = { imu_time_ns: number; video_time_ns: number; label: string };
type SyncState = {
  anchors: SyncAnchor[];
  scale: number;
  offset_ns: number;
  residual_rms_ns: number;
  quality: string;
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
  const [error, setError] = useState("");
  const [recordings, setRecordings] = useState<Recording[]>([]);
  const [taxonomy, setTaxonomy] = useState<Taxonomy | null>(null);
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [cameras, setCameras] = useState<Camera[]>([]);
  const [cameraId, setCameraId] = useState("");
  const liveRef = useRef<{ t: number[]; values: number[][] }>({ t: [], values: [] });
  const [, redraw] = useState(0);

  const refreshRecordings = () =>
    api<Recording[]>("/api/v1/recordings").then(setRecordings).catch((e) => setError(e.message));
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
    }).catch((e) => setError(e.message));

  useEffect(() => {
    api<Taxonomy>("/api/v1/taxonomy").then(setTaxonomy).catch((e) => setError(e.message));
    api<AppConfig>("/api/v1/config").then((value) => {
      setConfig(value);
      if (!value.allowed_unikeys.includes(participant) && value.allowed_unikeys.length) {
        setParticipant(value.allowed_unikeys[0]);
      }
      setDataTier(value.default_data_tier);
    }).catch((e) => setError(e.message));
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
    setError("");
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
      setError((e as Error).message);
    }
  };

  const stop = async () => {
    setError("");
    try {
      await api("/api/v1/recordings/stop", { method: "POST" });
      refreshRecordings();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const toggleImuPreview = async () => {
    setError("");
    try {
      const active = live.session_type === "devices_preview";
      await api(`/api/v1/preflight/${active ? "stop" : "start"}`, {
        method: "POST",
        body: active ? undefined : JSON.stringify({ camera_id: cameraId || null })
      });
      if (!active) liveRef.current = { t: [], values: [] };
    } catch (e) {
      setError((e as Error).message);
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
      {error && <div className="error-banner">{error}</div>}
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
          onError={setError}
        />
      )}
      {tab === "annotate" && taxonomy && (
        <AnnotationPage recordings={recordings} taxonomy={taxonomy} allowedUnikeys={config?.allowed_unikeys ?? []} onError={setError} />
      )}
      {tab === "library" && <Library recordings={recordings} />}
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

function CharacterizationPage({ live, allowedUnikeys, chart, onError }: { live: any; allowedUnikeys: string[]; chart: { t: number[]; values: number[][] }; onError: (message: string) => void }) {
  const [operator, setOperator] = useState("xfan0282");
  const [stage, setStage] = useState(characterizationStages[0][0]);
  const [notes, setNotes] = useState("");
  const [history, setHistory] = useState<any[]>([]);
  const active = live.state === "recording" && live.session_type === "characterization";
  const currentStage = live.characterization?.current_stage;
  const busy = ["arming", "finalizing"].includes(live.state);

  const refresh = () => api<any[]>("/api/v1/characterizations").then(setHistory).catch((e) => onError(e.message));
  useEffect(() => { refresh(); }, []);
  useEffect(() => {
    if (!allowedUnikeys.includes(operator) && allowedUnikeys.length) setOperator(allowedUnikeys[0]);
  }, [allowedUnikeys]);

  const invoke = async (path: string, body?: unknown) => {
    try {
      await api(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });
      if (path === "/api/v1/characterizations/stop") refresh();
    } catch (e) { onError((e as Error).message); }
  };
  const selectedDescription = characterizationStages.find((item) => item[0] === stage);
  return <main>
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

function AnnotationPage({ recordings, taxonomy, allowedUnikeys, onError }: { recordings: Recording[]; taxonomy: Taxonomy; allowedUnikeys: string[]; onError: (message: string) => void }) {
  const [selected, setSelected] = useState("");
  const [doc, setDoc] = useState<AnnotationDocument | null>(null);
  const [timeline, setTimeline] = useState<{ time_s: number[]; values: number[][]; unit: string } | null>(null);
  const [label, setLabel] = useState<"fall" | "non_fall">("non_fall");
  const [activity, setActivity] = useState(taxonomy.non_fall[0].code);
  const [marks, setMarks] = useState<{ start?: number; end?: number; onset?: number; impact?: number }>({});
  const [currentTime, setCurrentTime] = useState(0);
  const [annotator, setAnnotator] = useState("xfan0282");
  const [sync, setSync] = useState<SyncState | null>(null);
  const [selectedImuTime, setSelectedImuTime] = useState<number | null>(null);
  const [anchorLabel, setAnchorLabel] = useState("tap");
  const video = useRef<HTMLVideoElement>(null);

  useEffect(() => {
    if (!selected) return;
    Promise.all([
      api<AnnotationDocument>(`/api/v1/recordings/${selected}/annotations`),
      api<{ time_s: number[]; values: number[][]; unit: string }>(`/api/v1/recordings/${selected}/timeline`),
      api<SyncState>(`/api/v1/recordings/${selected}/sync`)
    ]).then(([annotations, data, syncState]) => {
      setDoc(annotations); setTimeline(data); setSync(syncState); setMarks({});
      const participant = recordings.find((item) => item.recording_id === selected)?.participant_id;
      if (participant && allowedUnikeys.includes(participant)) setAnnotator(participant);
    }).catch((e) => onError(e.message));
  }, [selected]);

  useEffect(() => {
    const choices = taxonomy[label];
    setActivity(choices[0].code);
  }, [label]);

  const mark = (name: "start" | "end" | "onset" | "impact") => {
    setMarks((existing) => ({ ...existing, [name]: Math.round((video.current?.currentTime ?? 0) * 1e9) }));
  };

  const addSegment = () => {
    if (!doc || marks.start === undefined || marks.end === undefined) return;
    const segmentId = `seg_${String(doc.segments.length + 1).padStart(3, "0")}`;
    const segment: Segment = {
      segment_id: segmentId,
      start_ns: marks.start,
      end_ns: marks.end,
      binary_label: label,
      activity_code: activity,
      annotator_id: annotator,
      confidence: 1,
      notes: ""
    };
    const events = [...doc.events];
    if (label === "fall" && marks.onset !== undefined && marks.impact !== undefined) {
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
    try {
      const saved = await api<AnnotationDocument>(`/api/v1/recordings/${selected}/annotations`, {
        method: "PUT",
        body: JSON.stringify({ ...doc, revision: doc.revision + 1, finalized })
      });
      setDoc(saved);
    } catch (e) {
      onError((e as Error).message);
    }
  };

  const choices = taxonomy[label];
  const addAnchor = () => {
    if (!sync || selectedImuTime === null) return;
    const anchor = {
      imu_time_ns: Math.round(selectedImuTime * 1e9),
      video_time_ns: Math.round(currentTime * 1e9),
      label: anchorLabel || "tap"
    };
    setSync({ ...sync, anchors: [...sync.anchors, anchor] });
  };
  const saveSync = async () => {
    if (!sync || !selected) return;
    try {
      const model = await api<Omit<SyncState, "anchors">>(`/api/v1/recordings/${selected}/sync`, {
        method: "PUT", body: JSON.stringify({ anchors: sync.anchors })
      });
      const timelineData = await api<{ time_s: number[]; values: number[][]; unit: string }>(`/api/v1/recordings/${selected}/timeline`);
      setSync({ ...sync, ...model });
      setTimeline(timelineData);
    } catch (e) { onError((e as Error).message); }
  };
  return (
    <main className="annotation-layout">
      <aside className="panel recording-list">
        <div className="panel-title">选择录制</div>
        {recordings.map((recording) => <button key={recording.recording_id} className={selected === recording.recording_id ? "selected" : ""} onClick={() => setSelected(recording.recording_id)}><strong>{recording.participant_id}</strong><span>{recording.recording_id}</span></button>)}
      </aside>
      <section className="annotation-workspace">
        {!selected ? <div className="panel placeholder">从左侧选择一段录制</div> : <>
          <div className="panel video-review"><video ref={video} controls src={`/api/v1/recordings/${selected}/video`} onTimeUpdate={(e) => setCurrentTime(e.currentTarget.currentTime)} /></div>
          <div className="panel"><div className="panel-title">同步 IMU · {timeline?.unit} · 点击曲线选择锚点</div>{timeline && <Plot time={timeline.time_s} values={timeline.values} cursorTime={currentTime} height={250} onSelectTime={setSelectedImuTime} />}</div>
          <div className="panel sync-panel">
            <div className="panel-title">IMU—视频同步锚点</div>
            <p className="stage-help">在视频中停到可见同步动作，再点击 IMU 曲线对应峰值并添加。建议录制开头、中间、结尾各一个；至少两个才能估计漂移。</p>
            <div className="sync-inputs"><span>IMU {selectedImuTime === null ? "未选择" : `${selectedImuTime.toFixed(3)} s`}</span><span>视频 {currentTime.toFixed(3)} s</span><input value={anchorLabel} onChange={(e) => setAnchorLabel(e.target.value)} placeholder="锚点标签" /><button disabled={selectedImuTime === null} onClick={addAnchor}>添加锚点</button></div>
            <div className="anchor-list">{sync?.anchors.map((anchor, index) => <div key={`${anchor.imu_time_ns}-${index}`}><span>{index + 1}. {anchor.label} · IMU {seconds(anchor.imu_time_ns)} ↔ 视频 {seconds(anchor.video_time_ns)}</span><button onClick={() => setSync({ ...sync, anchors: sync.anchors.filter((_, itemIndex) => itemIndex !== index) })}>删除</button></div>)}</div>
            <div className="save-row"><button className="primary" disabled={!sync || sync.anchors.length < 2} onClick={saveSync}>拟合并保存同步</button><span>quality: {sync?.quality ?? "missing"} · scale: {sync?.scale?.toFixed(8) ?? "—"} · offset: {sync ? seconds(sync.offset_ns) : "—"} · RMS: {sync && Number.isFinite(sync.residual_rms_ns) ? `${(sync.residual_rms_ns / 1e6).toFixed(2)} ms` : "—"}</span></div>
          </div>
          <div className="panel annotation-controls">
            <div className="time-readout">当前 {currentTime.toFixed(3)} s</div>
            <label>标注者 UniKey<select value={annotator} onChange={(e) => setAnnotator(e.target.value)}>{allowedUnikeys.map((item) => <option value={item} key={item}>{item}</option>)}</select></label>
            <div className="mark-buttons"><button onClick={() => mark("start")}>标记动作开始</button><button onClick={() => mark("end")}>标记动作结束</button><button onClick={() => mark("onset")}>标记跌倒起始</button><button onClick={() => mark("impact")}>标记撞击时刻</button></div>
            <div className="marks"><span>动作开始 {seconds(marks.start)}</span><span>动作结束 {seconds(marks.end)}</span><span>跌倒起始 {seconds(marks.onset)}</span><span>撞击时刻 {seconds(marks.impact)}</span></div>
            <div className="segment-form"><select value={label} onChange={(e) => setLabel(e.target.value as any)}><option value="non_fall">non_fall</option><option value="fall">fall</option></select><select value={activity} onChange={(e) => setActivity(e.target.value)}>{choices.map((item) => <option value={item.code} key={item.code}>{item.display_name_zh} · {item.code}</option>)}</select><button className="primary" onClick={addSegment}>添加区间</button></div>
          </div>
          {doc && <div className="panel segment-table"><div className="panel-title">动作区间</div>{doc.segments.map((segment) => <div className="segment-row" key={segment.segment_id}><span>{segment.segment_id}</span><strong>{segment.binary_label === "fall" ? "跌倒" : "非跌倒"}</strong><span>{segment.activity_code}</span><span>{seconds(segment.start_ns)} → {seconds(segment.end_ns)}</span><button onClick={() => setDoc({ ...doc, segments: doc.segments.filter((item) => item.segment_id !== segment.segment_id), events: doc.events.filter((event) => event.segment_id !== segment.segment_id) })}>删除</button></div>)}<div className="save-row"><button onClick={() => save(false)}>保存草稿</button><button className="primary" onClick={() => save(true)}>完成并验证标注</button><span>修订 {doc.revision} · {doc.finalized ? "已定稿" : "草稿"}</span></div></div>}
        </>}
      </section>
    </main>
  );
}

function Library({ recordings }: { recordings: Recording[] }) {
  return <main><section className="panel library"><div className="panel-title">本地录制</div>{recordings.map((recording) => <article key={recording.recording_id}><div><strong>{recording.recording_id}</strong><span>{recording.collection_id} · {recording.participant_id} · {recording.data_tier} · {seconds(recording.duration_ns)}</span></div><div className={`state state-${recording.state}`}>{stateLabel(recording.state)}</div>{recording.issues.length > 0 && <ul>{recording.issues.map((issue) => <li key={issue}>{issue}</li>)}</ul>}</article>)}</section></main>;
}
