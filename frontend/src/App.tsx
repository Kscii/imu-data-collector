import { useEffect, useMemo, useRef, useState } from "react";
import Plot from "./Plot";
import {
  apiErrorMessage,
  isEnglish,
  localizedField,
  tr,
  useDocumentLocalization,
  userVisibleMessage,
} from "./i18n";

document.title = __APP_KIND__ === "annotation"
  ? tr("IMU 数据标注平台", "IMU Annotation Platform")
  : tr("IMU 数采平台", "IMU Data Collector");

type AppTab = "capture" | "characterize" | "annotate" | "calibration" | "library";

const CAPTURE_FORM_KEY = "imu-capture-form-v1";

function defaultCollectionId(participant: string) {
  const now = new Date();
  const date = [now.getFullYear(), now.getMonth() + 1, now.getDate()]
    .map((value, index) => index === 0 ? String(value) : String(value).padStart(2, "0"))
    .join("");
  return `${date}_${participant}_01`;
}

function nextCollectionId(current: string, participant: string) {
  const base = defaultCollectionId(participant).slice(0, -2);
  const match = current.match(new RegExp(`^${base}(\\d{2})$`));
  const ordinal = match ? Math.min(99, Number(match[1]) + 1) : 1;
  return `${base}${String(ordinal).padStart(2, "0")}`;
}

function initialTab(annotationApplication: boolean): AppTab {
  const view = new URLSearchParams(location.search).get("view");
  const mapping: Record<string, AppTab> = annotationApplication
    ? { annotate: "annotate", calibration: "calibration", training: "library" }
    : { capture: "capture", records: "library", diagnostics: "characterize" };
  return (view && mapping[view]) || (annotationApplication ? "annotate" : "capture");
}

function tabView(tab: AppTab) {
  return tab === "library"
    ? (__APP_KIND__ === "annotation" ? "training" : "records")
    : tab === "characterize" ? "diagnostics" : tab;
}

function readCaptureForm() {
  try {
    return JSON.parse(sessionStorage.getItem(CAPTURE_FORM_KEY) ?? "{}") as {
      participant?: string;
      collection?: string;
      dataTier?: "test" | "prod";
      cameraId?: string;
    };
  } catch {
    return {};
  }
}

type Recording = {
  recording_id: string;
  collection_id: string;
  participant_id: string;
  data_tier: "test" | "prod";
  state: string;
  started_at_utc: string;
  duration_ns?: number;
  issues: string[];
  validation_issues?: string[];
  quality_warnings?: string[];
  upload_state: string;
  index_state?: "not_requested" | "pending" | "indexed" | "rejected";
  index_message?: string;
  manifest_generation?: number | null;
  purpose?: "annotation" | "calibration_evidence";
};

type CalibrationEvidence = {
  schema_version: string;
  profile_id: string;
  device: { name: string; address: string; scope_zh: string; scope_en: string };
  coordinate_system: {
    x_positive_zh: string;
    x_positive_en: string;
    y_positive_zh: string;
    y_positive_en: string;
    z_positive_zh: string;
    z_positive_en: string;
    handedness: string;
  };
  calibration: {
    accel_counts_per_g: number;
    gyro_counts_per_dps: number;
    accel_bias_counts_raw: number[];
    gyro_bias_counts_raw: number[];
    raw_axis_order: number[];
    axis_signs: number[];
    status: string;
    conclusion_zh: string;
    conclusion_en: string;
  };
  evidence: {
    recording_id: string;
    kind: string;
    setup_zh: string;
    setup_en: string;
    expected_zh: string;
    expected_en: string;
    observed_zh: string;
    observed_en: string;
    available: boolean;
  }[];
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
  fall: { code: string; display_name_zh: string; display_name_en: string }[];
  non_fall: { code: string; display_name_zh: string; display_name_en: string }[];
};

type AppConfig = {
  application: "capture" | "annotation";
  build_id?: string;
  allowed_unikeys: string[];
  admin_unikeys?: string[];
  data_tiers?: ("test" | "prod")[];
  default_data_tier?: "test" | "prod";
  data_root?: string;
  video?: { width: number; height: number; requested_fps: number; bitrate: string };
  local_actor_id?: string;
  catalog_refresh_interval_s?: number;
};

type Session = {
  unikey: string;
  email: string | null;
  is_admin: boolean;
  auth_mode: "local" | "iap";
};

type TrainingSnapshot = {
  snapshot_id: string;
  created_at_utc: string | null;
  created_by: string | null;
  content_fingerprint: string | null;
  archive_sha256: string;
  archive_size_bytes: number;
  recording_count: number;
  created?: boolean;
};

type ReviewDocument = {
  schema_version: "2.0.0";
  recording_id: string;
  revision: number;
  workflow: {
    state: "unassigned" | "in_progress" | "completed";
    annotator_id: string | null;
    last_editor_id: string | null;
    updated_at_utc: string | null;
  };
  annotations: AnnotationDocument;
  active_export: {
    source_review_revision: number;
    object_key: string;
    sha256: string;
    logical_content_sha256: string;
  } | null;
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
  role: "start_tap" | "end_tap";
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
    recommendation_rank?: number;
    response_cluster_id?: number;
    response_cluster_size?: number;
    selection_basis: "event_onset" | "local_peak" | "timing_projection";
  }[];
  recommendation: {
    algorithm: string;
    sample_index: number | null;
    confidence: "high" | "medium" | "low";
    reason: string;
    expected_video_minus_imu_ns: number | null;
    score_margin_ratio: number | null;
    event_robust_z: number | null;
    timing_residual_ms: number | null;
    distinct_response_count: number;
    response_cluster_window_ms: number;
  };
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
const INITIAL_VIDEO_PREVIEW_OFFSET_NS = 200_000_000;
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

async function api<T>(path: string, init?: RequestInit, timeoutMs = 45_000): Promise<T> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  const abortFromCaller = () => controller.abort();
  init?.signal?.addEventListener("abort", abortFromCaller, { once: true });
  try {
    const response = await fetch(path, {
      ...init,
      signal: controller.signal,
      headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) }
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      const detail = payload.detail;
      if (detail && typeof detail === "object") {
        throw new Error(apiErrorMessage(detail, response.status, response.statusText));
      }
      throw new Error(apiErrorMessage(detail, response.status, response.statusText));
    }
    return response.json();
  } catch (error) {
    if ((error as Error).name === "AbortError") {
      throw new Error(tr(
        `请求超过 ${(timeoutMs / 1000).toFixed(0)} 秒仍未完成，已停止等待`,
        `The request did not finish within ${(timeoutMs / 1000).toFixed(0)} seconds and was cancelled`,
      ));
    }
    throw error;
  } finally {
    window.clearTimeout(timer);
    init?.signal?.removeEventListener("abort", abortFromCaller);
  }
}

const CAPTURE_TAB_LEASE_KEY = "imu-data-collector:capture-tab-lease:v1";
const CAPTURE_TAB_LEASE_TTL_MS = 6_000;

function useCaptureTabLease(enabled: boolean) {
  const tabId = useRef(crypto.randomUUID()).current;
  const [ownsLease, setOwnsLease] = useState(!enabled);

  useEffect(() => {
    if (!enabled) {
      setOwnsLease(true);
      return;
    }
    const channel = typeof BroadcastChannel === "undefined"
      ? null
      : new BroadcastChannel("imu-data-collector:capture-tab");
    const claim = () => {
      const now = Date.now();
      let current: { tabId?: string; expiresAt?: number } = {};
      try {
        current = JSON.parse(localStorage.getItem(CAPTURE_TAB_LEASE_KEY) ?? "{}");
      } catch {
        current = {};
      }
      if (current.tabId === tabId || !current.expiresAt || current.expiresAt <= now) {
        localStorage.setItem(
          CAPTURE_TAB_LEASE_KEY,
          JSON.stringify({ tabId, expiresAt: now + CAPTURE_TAB_LEASE_TTL_MS })
        );
        channel?.postMessage({ type: "lease", tabId });
      }
      let confirmed: { tabId?: string } = {};
      try {
        confirmed = JSON.parse(localStorage.getItem(CAPTURE_TAB_LEASE_KEY) ?? "{}");
      } catch {
        confirmed = {};
      }
      setOwnsLease(confirmed.tabId === tabId);
    };
    const release = () => {
      try {
        const current = JSON.parse(localStorage.getItem(CAPTURE_TAB_LEASE_KEY) ?? "{}");
        if (current.tabId === tabId) localStorage.removeItem(CAPTURE_TAB_LEASE_KEY);
      } catch {
        // 损坏的 lease 会在下一次 claim 时被覆盖。
      }
      channel?.postMessage({ type: "release", tabId });
    };
    const onStorage = (event: StorageEvent) => {
      if (event.key === CAPTURE_TAB_LEASE_KEY) claim();
    };
    const onChannel = () => claim();
    claim();
    const timer = window.setInterval(claim, 2_000);
    window.addEventListener("storage", onStorage);
    window.addEventListener("beforeunload", release);
    channel?.addEventListener("message", onChannel);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener("storage", onStorage);
      window.removeEventListener("beforeunload", release);
      channel?.removeEventListener("message", onChannel);
      release();
      channel?.close();
    };
  }, [enabled, tabId]);

  return ownsLease;
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

function stateLabel(state: string) {
  const labels: Record<string, string> = {
    idle: "空闲",
    arming: "正在准备",
    recording: "正在录制",
    finalizing: "正在收尾",
    ready: "可用",
    needs_attention: "需要检查",
    failed: "失败",
    connected: "已连接",
    connecting: "正在连接",
    reconnecting: "正在重连",
    releasing: "正在释放",
    error: "异常"
  };
  return labels[state] ?? state;
}

function tierLabel(tier: "test" | "prod") {
  return tier === "prod" ? "正式数据" : "测试数据";
}

function issueLabel(issue: string) {
  const residualWarning = issue.match(
    /^IMU packet timestamp maximum residual is ([0-9.]+) ms; warning threshold is 200 ms$/
  );
  if (residualWarning) {
    return tr(
      `IMU 包时间戳最大残差为 ${residualWarning[1]} ms，超过 200 ms 警告阈值`,
      `IMU packet timestamp maximum residual is ${residualWarning[1]} ms, above the 200 ms warning threshold`,
    );
  }
  const labels: Record<string, string> = {
    "synchronization anchors have not been verified": "同步锚点尚未验证",
    "IMU scale calibration has not been verified": "IMU 尺度校准尚未验证",
    "IMU packet timestamp maximum residual exceeds 0.5 seconds": "IMU 包时间戳最大残差超过 0.5 秒",
  };
  return labels[issue] ?? userVisibleMessage(issue);
}

export default function App() {
  useDocumentLocalization();
  const annotationApplication = __APP_KIND__ === "annotation";
  const ownsCaptureTab = useCaptureTabLease(!annotationApplication);
  const diagnosticsVisible = new URLSearchParams(location.search).has("diagnostics")
    || new URLSearchParams(location.search).get("view") === "diagnostics";
  const [captureForm] = useState(readCaptureForm);
  const [tab, setTab] = useState<AppTab>(() => initialTab(annotationApplication));
  const [live, setLive] = useState<any>({ state: "idle", imu: {}, video: {} });
  const [participant, setParticipant] = useState(captureForm.participant ?? "xfan0282");
  const [collection, setCollection] = useState(
    captureForm.collection ?? defaultCollectionId(captureForm.participant ?? "xfan0282")
  );
  const [dataTier, setDataTier] = useState<"test" | "prod">(captureForm.dataTier ?? "test");
  const [captureError, setCaptureError] = useState("");
  const [captureOperation, setCaptureOperation] = useState("");
  const [recordings, setRecordings] = useState<Recording[]>([]);
  const [taxonomy, setTaxonomy] = useState<Taxonomy | null>(null);
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [session, setSession] = useState<Session | null>(null);
  const [cameras, setCameras] = useState<Camera[]>([]);
  const [cameraId, setCameraId] = useState(captureForm.cameraId ?? "");
  const liveRef = useRef<{ t: number[]; values: number[][] }>({ t: [], values: [] });
  const [, redraw] = useState(0);
  const versionMismatch = !annotationApplication
    && config !== null
    && config.build_id !== __CAPTURE_API_BUILD_ID__;
  const captureInteractionBlocked = !ownsCaptureTab || versionMismatch;

  const selectTab = (next: AppTab) => {
    setTab(next);
    const url = new URL(location.href);
    url.searchParams.set("view", tabView(next));
    history.pushState({}, "", url);
  };

  useEffect(() => {
    const onPopState = () => setTab(initialTab(annotationApplication));
    window.addEventListener("popstate", onPopState);
    const url = new URL(location.href);
    if (!url.searchParams.has("view")) {
      url.searchParams.set("view", tabView(tab));
      history.replaceState({}, "", url);
    }
    return () => window.removeEventListener("popstate", onPopState);
  }, [annotationApplication]);

  useEffect(() => {
    if (annotationApplication) return;
    sessionStorage.setItem(
      CAPTURE_FORM_KEY,
      JSON.stringify({ participant, collection, dataTier, cameraId })
    );
  }, [annotationApplication, participant, collection, dataTier, cameraId]);

  const refreshRecordings = async () => {
    try {
      const value = await api<Recording[]>("/api/v1/recordings");
      setRecordings(value);
      return value;
    } catch (e) {
      setCaptureError((e as Error).message);
      return [];
    }
  };
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
      if (!captureForm.dataTier && value.default_data_tier) {
        setDataTier(value.default_data_tier);
      }
    }).catch((e) => setCaptureError(e.message));
    if (annotationApplication) {
      api<Session>("/api/v1/session").then(setSession).catch((e) => setCaptureError(e.message));
      refreshRecordings();
      const recordingsTimer = window.setInterval(refreshRecordings, 10_000);
      return () => window.clearInterval(recordingsTimer);
    }
    refreshRecordings();
    let disposed = false;
    let socket: WebSocket | null = null;
    let reconnectTimer: number | null = null;
    const acceptLive = (payload: any) => {
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
    const protocol = location.protocol === "https:" ? "wss" : "ws";
    const connect = () => {
      if (disposed) return;
      socket = new WebSocket(`${protocol}://${location.host}/api/v1/live`);
      socket.onmessage = (message) => acceptLive(JSON.parse(message.data));
      socket.onclose = () => {
        if (!disposed) reconnectTimer = window.setTimeout(connect, 1000);
      };
    };
    api<any>("/api/v1/health").then(acceptLive).catch(() => undefined);
    connect();
    return () => {
      disposed = true;
      if (reconnectTimer !== null) window.clearTimeout(reconnectTimer);
      socket?.close();
    };
  }, []);

  useEffect(() => {
    if (!annotationApplication && ownsCaptureTab) refreshCameras();
  }, [annotationApplication, ownsCaptureTab]);

  const start = async () => {
    if (captureInteractionBlocked || captureOperation) return;
    setCaptureError("");
    setCaptureOperation("starting_recording");
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
    } catch (e) {
      setCaptureError((e as Error).message);
    } finally {
      setCaptureOperation("");
    }
  };

  const stop = async () => {
    if (captureInteractionBlocked || captureOperation) return;
    setCaptureError("");
    setCaptureOperation("stopping_recording");
    try {
      await api("/api/v1/recordings/stop", { method: "POST" });
      refreshRecordings();
    } catch (e) {
      setCaptureError((e as Error).message);
    } finally {
      setCaptureOperation("");
    }
  };

  const toggleImuPreview = async () => {
    if (captureInteractionBlocked || captureOperation) return;
    setCaptureError("");
    const active = live.session_type === "devices_preview";
    setCaptureOperation(active ? "releasing_preview" : "connecting_preview");
    try {
      const snapshot = await api<any>(`/api/v1/preflight/${active ? "stop" : "start"}`, {
        method: "POST",
        body: active ? undefined : JSON.stringify({ camera_id: cameraId || null })
      });
      setLive(snapshot);
      if (!active) liveRef.current = { t: [], values: [] };
    } catch (e) {
      setCaptureError((e as Error).message);
    } finally {
      setCaptureOperation("");
    }
  };

  const retryPreview = async () => {
    if (captureInteractionBlocked || captureOperation) return;
    setCaptureError("");
    setCaptureOperation("connecting_preview");
    try {
      const snapshot = await api<any>("/api/v1/preflight/start", {
        method: "POST",
        body: JSON.stringify({ camera_id: cameraId || null })
      });
      setLive(snapshot);
    } catch (e) {
      setCaptureError((e as Error).message);
    } finally {
      setCaptureOperation("");
    }
  };

  const changeCamera = async (nextCameraId: string) => {
    if (captureInteractionBlocked || captureOperation) return;
    setCaptureError("");
    if (live.session_type !== "devices_preview") {
      setCameraId(nextCameraId);
      return;
    }
    setCaptureOperation("switching_camera");
    try {
      const snapshot = await api<any>("/api/v1/preflight/camera", {
        method: "POST",
        body: JSON.stringify({ camera_id: nextCameraId })
      });
      setCameraId(nextCameraId);
      setLive(snapshot);
    } catch (e) {
      setCaptureError((e as Error).message);
    } finally {
      setCaptureOperation("");
    }
  };

  return (
    <div className="app-shell">
      <header>
        <div>
          <span className="eyebrow">{annotationApplication ? "CW12EU-T · 独立标注" : "CW12EU-T · 本机采集"}</span>
          <h1>{annotationApplication ? "IMU 数据标注平台" : "IMU 数采平台"}</h1>
        </div>
        <div className={`state state-${live.state}`}>{annotationApplication ? session ? `当前登录 ${session.unikey}` : "正在验证身份" : live.session_type === "devices_preview" ? "设备预览" : stateLabel(live.state)}</div>
      </header>
      <nav>
        {annotationApplication ? <><button className={tab === "annotate" ? "active" : ""} onClick={() => selectTab("annotate")}>标注与同步</button><button className={tab === "calibration" ? "active" : ""} onClick={() => selectTab("calibration")}>设备校准证据</button><button className={tab === "library" ? "active" : ""} onClick={() => selectTab("library")}>训练快照</button></> : <>
          <button className={tab === "capture" ? "active" : ""} onClick={() => selectTab("capture")}>采集</button>
          <button className={tab === "library" ? "active" : ""} onClick={() => { selectTab("library"); refreshRecordings(); }}>记录与发布</button>
          {diagnosticsVisible && <button className={tab === "characterize" ? "active" : ""} onClick={() => selectTab("characterize")}>IMU 诊断</button>}
        </>}
      </nav>
      {annotationApplication && captureError && <div className="error-banner">{captureError}</div>}
      {tab === "capture" && captureError && <div className="error-banner">{captureError}</div>}
      {!annotationApplication && versionMismatch && <div className="error-banner">采集页面与后端 API 版本不一致：页面 {__CAPTURE_API_BUILD_ID__}，后端 {config?.build_id ?? "旧版未报告"}。源码更新后请在项目根目录运行 <code>./scripts/update-local-capture.sh</code>；普通的 systemctl 重启不会重新构建页面。</div>}
      {!annotationApplication && !ownsCaptureTab && <div className="warning-banner">另一个标签页正在控制本机采集设备。本页保持只读；关闭另一个页面后最多等待 6 秒即可接管。</div>}
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
          changeCamera={changeCamera}
          refreshCameras={refreshCameras}
          toggleImuPreview={toggleImuPreview}
          retryPreview={retryPreview}
          captureOperation={captureOperation}
          interactionBlocked={captureInteractionBlocked}
          ownsCaptureTab={ownsCaptureTab}
        />
      )}
      {tab === "characterize" && (
        <CharacterizationPage
          live={live}
          allowedUnikeys={config?.allowed_unikeys ?? []}
          chart={liveRef.current}
          interactionBlocked={captureInteractionBlocked}
        />
      )}
      {annotationApplication && tab === "annotate" && taxonomy && session && (
        <AnnotationPage recordings={recordings.filter((item) => item.purpose !== "calibration_evidence")} taxonomy={taxonomy} session={session} onChanged={refreshRecordings} />
      )}
      {annotationApplication && tab === "calibration" && <CalibrationEvidencePage />}
      {annotationApplication && tab === "library" && session && <TrainingSnapshotsPage session={session} />}
      {!annotationApplication && tab === "library" && <CaptureLibrary recordings={recordings} onChanged={refreshRecordings} />}
    </div>
  );
}

function CalibrationEvidencePage() {
  const [profile, setProfile] = useState<CalibrationEvidence | null>(null);
  const [selected, setSelected] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    api<CalibrationEvidence>("/api/v1/calibration-evidence")
      .then((value) => {
        setProfile(value);
        setSelected(value.evidence.find((item) => item.available)?.recording_id ?? "");
      })
      .catch((value) => setError((value as Error).message));
  }, []);

  if (error) return <main><div className="error-banner">{error}</div></main>;
  if (!profile) return <main><section className="panel">正在读取校准证据…</section></main>;
  const current = profile.evidence.find((item) => item.recording_id === selected);
  const copyId = async () => {
    if (!current) return;
    await navigator.clipboard.writeText(current.recording_id);
    setMessage(`已复制 ${current.recording_id}`);
  };
  return <main>
    {message && <div className="success-banner">{message}</div>}
    <section className="panel">
      <div className="panel-title">设备专属工程校准档案</div>
      <p><strong>{profile.profile_id}</strong> · {profile.device.name} · {profile.device.address}</p>
      <p className="stage-help">{localizedField(profile.device, "scope")}</p>
      <div className="status-grid">
        <span>加速度尺度 {profile.calibration.accel_counts_per_g} counts/g</span>
        <span>角速度尺度 {profile.calibration.gyro_counts_per_dps} counts/(°/s)</span>
        <span>原始加速度零偏 [{profile.calibration.accel_bias_counts_raw.join(", ")}]</span>
        <span>原始角速度零偏 [{profile.calibration.gyro_bias_counts_raw.join(", ")}]</span>
      </div>
      <p>{localizedField(profile.calibration, "conclusion")}</p>
      <p className="stage-help">{tr("目标右手坐标系", "Target right-handed coordinate system")}：+X {localizedField(profile.coordinate_system, "x_positive")}；+Y {localizedField(profile.coordinate_system, "y_positive")}；+Z {localizedField(profile.coordinate_system, "z_positive")}。</p>
    </section>
    <section className="annotation-layout calibration-layout">
      <aside className="panel calibration-evidence-sidebar">
        <div className="panel-title">证据录制（{profile.evidence.length}）</div>
        <div className="calibration-evidence-list">
          {profile.evidence.map((item) => <button key={item.recording_id} className={selected === item.recording_id ? "selected" : ""} disabled={!item.available} onClick={() => setSelected(item.recording_id)}><strong>{item.kind === "accel_static_face" ? "六面静态" : item.kind === "gyro_rotation" ? "360° 旋转" : "动态校验"}</strong><span>{item.recording_id}</span><span>{item.available ? "证据可用" : "制品缺失"}</span></button>)}
        </div>
      </aside>
      <section className="calibration-evidence-detail">
        {current && <>
          <div className="panel calibration-video"><video key={current.recording_id} controls preload="metadata" src={`/api/v1/calibration-evidence/${current.recording_id}/video`} /></div>
          <div className="panel">
            <div className="panel-title">已确认实验语义</div>
            <p><strong>实验：</strong>{localizedField(current, "setup")}</p>
            <p><strong>预期：</strong>{localizedField(current, "expected")}</p>
            <p><strong>观测：</strong>{localizedField(current, "observed")}</p>
            <div className="save-row"><button onClick={copyId}>复制录制 ID</button><a className="button-link" href={`/api/v1/calibration-evidence/${current.recording_id}/capture-h5/download`} download>下载原始 H5</a></div>
            <p className="stage-help">本页只用于理解设备坐标、物理尺度与证据来源，不进入动作标注队列，也不生成训练样本。</p>
          </div>
        </>}
      </section>
    </section>
  </main>;
}

function CapturePage(props: any) {
  const {
    live, participant, setParticipant, collection, setCollection, start, stop, chart,
    dataTier, setDataTier, allowedUnikeys, cameras, cameraId, changeCamera, refreshCameras,
    toggleImuPreview, retryPreview, captureOperation, interactionBlocked, ownsCaptureTab
  } = props;
  const [previewRetry, setPreviewRetry] = useState(0);
  const active = live.state === "recording" && live.session_type === "capture";
  const devicesPreview = live.session_type === "devices_preview";
  const monitoringRequested = Boolean(live.monitoring_requested);
  const previewStreamId = live.video?.stream_id ?? 0;
  useEffect(() => setPreviewRetry(0), [previewStreamId]);
  const busy = ["arming", "finalizing"].includes(live.state) || Boolean(captureOperation);
  const anotherSession = live.state === "recording" && live.session_type !== "capture";
  const previewButtonLabel = captureOperation === "releasing_preview"
    ? "正在释放…"
    : captureOperation === "connecting_preview"
      ? "正在连接…"
      : devicesPreview
        ? "释放预览设备"
        : "连接预览设备";
  return (
    <main>
      <section className="controls panel">
        <label>采集场次 ID（自动）<input value={collection} readOnly disabled={interactionBlocked || active || busy} /></label>
        <button disabled={interactionBlocked || active || busy} onClick={() => setCollection(nextCollectionId(collection, participant))}>下一个采集场次</button>
        <label>参与者 UniKey<select value={participant} onChange={(e) => { const next = e.target.value; setParticipant(next); setCollection(defaultCollectionId(next)); }} disabled={interactionBlocked || active || busy}>{allowedUnikeys.map((item: string) => <option value={item} key={item}>{item}</option>)}</select></label>
        <label>数据级别<select value={dataTier} onChange={(e) => setDataTier(e.target.value as "test" | "prod")} disabled={interactionBlocked || active || busy}><option value="test">测试数据（不进入训练）</option><option value="prod">正式数据（需通过质量门禁）</option></select></label>
        <label>摄像头<select value={cameraId} onChange={(e) => changeCamera(e.target.value)} disabled={interactionBlocked || active || busy}>{cameras.map((item: Camera) => <option value={item.camera_id} key={item.camera_id}>{isEnglish && /[\u3400-\u9fff]/u.test(item.product) ? "Camera" : item.product} · {item.device}{item.integration === "external" ? " · 外接" : ""}{item.supports_default_profile && item.color_capture ? " · 推荐" : " · 不兼容"}</option>)}</select></label>
        <button disabled={interactionBlocked || active || busy} onClick={refreshCameras}>重新扫描摄像头</button>
        <button disabled={interactionBlocked || active || busy || anotherSession || (!devicesPreview && !cameraId)} onClick={toggleImuPreview}>{previewButtonLabel}</button>
        {monitoringRequested && live.device?.state === "error" && <button disabled={interactionBlocked || active || busy} onClick={retryPreview}>重试失败设备</button>}
        {!active ? <button className="primary" disabled={interactionBlocked || busy || anotherSession || !cameraId} onClick={start}>{captureOperation === "starting_recording" ? "正在准备…" : "开始录制"}</button> : <button className="danger" disabled={interactionBlocked || busy} onClick={stop}>{captureOperation === "stopping_recording" ? "正在结束…" : "结束录制"}</button>}
      </section>
      <section className="metrics">
        <Metric label="摄像头输入实时 FPS" value={(live.video?.source_fps ?? live.video?.fps ?? 0).toFixed(1)} warn={(live.video?.source_fps ?? live.video?.fps ?? 0) > 0 && (live.video?.source_fps ?? live.video?.fps ?? 0) < 29} />
        <Metric label={`浏览器预览实时 FPS（上限 ${live.video?.preview_fps_limit ?? 10}）`} value={(live.video?.preview_fps ?? 0).toFixed(1)} />
        <Metric label="视频帧" value={live.video?.frame ?? 0} />
        <Metric label="IMU 通知包" value={live.imu?.packet_count ?? 0} />
        <Metric label="IMU 样本" value={live.imu?.sample_count ?? 0} />
        <Metric label="IMU 估算频率" value={`${(live.imu?.estimated_sample_rate_hz ?? 0).toFixed(2)} Hz`} />
        <Metric label="最后一包" value={live.imu?.last_packet_age_ms == null ? "—" : `${live.imu.last_packet_age_ms.toFixed(0)} ms 前`} warn={live.imu?.connected && (live.imu?.last_packet_age_ms ?? 0) > 2000} />
        <Metric label="BLE 连接" value={live.imu?.connected ? "已连接" : "未连接"} warn={!live.imu?.connected} />
        <Metric label="设备状态" value={stateLabel(live.device?.state ?? "—")} warn={["error", "reconnecting"].includes(live.device?.state)} />
        <Metric label="解析/回调丢弃" value={`${live.imu?.parse_errors ?? 0} / ${live.imu?.callback_drops ?? 0}`} warn={(live.imu?.parse_errors ?? 0) > 0 || (live.imu?.callback_drops ?? 0) > 0} />
        <Metric label="剩余磁盘" value={`${(live.free_disk_gib ?? 0).toFixed(1)} GiB`} />
      </section>
      <section className="capture-grid">
        <div className="panel camera-panel">
          <div className="panel-title">实时画面 · 仅本机{devicesPreview ? " · 预览不落盘" : ""}</div>
          {ownsCaptureTab && monitoringRequested && previewStreamId > 0 ? <div className="preview-stage"><img key={previewStreamId} src={`/api/v1/preview.mjpeg?stream=${previewStreamId}&retry=${previewRetry}`} onError={() => { if (previewRetry < 3) window.setTimeout(() => setPreviewRetry((value) => Math.min(value + 1, 3)), 500); }} alt="摄像头实时预览" />{live.video?.transition && <div className="preview-overlay">摄像头正在切换，暂时保留最后一帧…</div>}{previewRetry >= 3 && <div className="preview-overlay preview-overlay-error"><span>浏览器预览流连续失败 3 次</span><button onClick={() => setPreviewRetry(0)}>重试画面</button></div>}</div> : <div className="placeholder">{ownsCaptureTab ? "连接预览设备后显示实时画面" : "设备由另一个标签页预览"}</div>}
        </div>
        <div className="panel chart-panel">
          <div className="panel-title">IMU 六轴实时曲线 · 最近 120 秒 · 当前为原始计数{devicesPreview ? " · 预览不落盘" : ""}</div>
          <Plot time={chart.t} values={chart.values} />
        </div>
      </section>
      {[...(live.recording?.issues ?? []), ...(live.recording?.validation_issues ?? [])].length > 0 && <div className="issues"><strong>上一次录制待办（不影响当前设备预览）</strong>{[...(live.recording?.issues ?? []), ...(live.recording?.validation_issues ?? [])].map((issue: string) => <div key={issue}>{issueLabel(issue)}</div>)}</div>}
      {(live.recording?.quality_warnings ?? []).length > 0 && <div className="warning-banner"><strong>上一次录制质量警告（允许发布）</strong>{live.recording.quality_warnings.map((warning: string) => <div key={warning}>{issueLabel(warning)}</div>)}</div>}
      {live.preview_error && <div className="issues"><div>{userVisibleMessage(live.preview_error)}</div></div>}
      {live.video?.camera_control_errors?.length > 0 && <div className="warning-banner">摄像头固定曝光未完全生效：{live.video.camera_control_errors.map(userVisibleMessage).join("；")}</div>}
      {live.device?.state === "reconnecting" && <div className="warning-banner">预览设备已断开，正在进行第 {live.device?.reconnect_attempt ?? 0} / 3 次自动重连。</div>}
    </main>
  );
}

function CharacterizationPage({ live, allowedUnikeys, chart, interactionBlocked }: { live: any; allowedUnikeys: string[]; chart: { t: number[]; values: number[][] }; interactionBlocked: boolean }) {
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
      <label>操作者 UniKey<select value={operator} disabled={interactionBlocked || active || busy} onChange={(e) => setOperator(e.target.value)}>{allowedUnikeys.map((item) => <option value={item} key={item}>{item}</option>)}</select></label>
      {!active ? <button className="primary" disabled={interactionBlocked || busy || live.state === "recording"} onClick={() => invoke("/api/v1/characterizations/start", { operator_id: operator, notes })}>开始 IMU-only 表征</button> : <button className="danger" disabled={interactionBlocked} onClick={() => invoke("/api/v1/characterizations/stop")}>结束并生成报告</button>}
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
        <label>实验阶段<select value={stage} disabled={interactionBlocked || !active || !!currentStage} onChange={(e) => setStage(e.target.value as typeof stage)}>{characterizationStages.map((item) => <option value={item[0]} key={item[0]}>{item[1]}</option>)}</select></label>
        <p className="stage-help">{selectedDescription?.[2]}</p>
        <label>阶段备注<input value={notes} disabled={interactionBlocked || !active || !!currentStage} onChange={(e) => setNotes(e.target.value)} placeholder="夹具、摆放或异常说明" /></label>
        <div className="stage-actions">{!currentStage ? <button className="primary" disabled={interactionBlocked || !active} onClick={() => invoke("/api/v1/characterizations/stages/start", { stage_code: stage, notes })}>开始该阶段</button> : <button disabled={interactionBlocked} onClick={() => invoke("/api/v1/characterizations/stages/stop")}>结束该阶段</button>}</div>
      </div>
      <div className="panel chart-panel"><div className="panel-title">六轴实时原始计数 · 最近 120 秒</div><Plot time={chart.t} values={chart.values} /></div>
    </section>
    <section className="panel library"><div className="panel-title">历史表征报告</div>{history.length === 0 ? <span className="muted">尚无完整报告</span> : history.map((item) => <article key={item.report_path}><div><strong>{item.source_h5}</strong><span>{(item.observed_rate_hz ?? 0).toFixed(5)} Hz · {item.packet_count} 包 · {item.calibration_status}</span></div><div className="state state-needs_attention">仅诊断</div></article>)}</section>
  </main>;
}

function Metric({ label, value, warn = false }: { label: string; value: string | number; warn?: boolean }) {
  return <div className={`metric ${warn ? "warn" : ""}`}><span>{label}</span><strong>{value}</strong></div>;
}

function AnnotationPage({ recordings, taxonomy, session, onChanged }: { recordings: Recording[]; taxonomy: Taxonomy; session: Session; onChanged: () => Promise<Recording[]> }) {
  const [selected, setSelected] = useState("");
  const [doc, setDoc] = useState<AnnotationDocument | null>(null);
  const [timeline, setTimeline] = useState<{ time_s: number[]; values: number[][]; unit: string } | null>(null);
  const [frameTimes, setFrameTimes] = useState<FrameTimes | null>(null);
  const [currentFrame, setCurrentFrame] = useState(0);
  const [experimentWindow, setExperimentWindow] = useState<SyncWindow | null>(null);
  const [experimentImuSample, setExperimentImuSample] = useState<number | null>(null);
  const [experimentBusy, setExperimentBusy] = useState(false);
  const [recommendationOffsetSource, setRecommendationOffsetSource] = useState<"formal_anchor" | "none">("none");
  const [annotationKind, setAnnotationKind] = useState<"fall" | "non_fall" | "exclude">("non_fall");
  const [activity, setActivity] = useState(taxonomy.non_fall[0].code);
  const [exclusionReason, setExclusionReason] = useState<Exclusion["reason"]>("other");
  const [marks, setMarks] = useState<{ start?: number; end?: number; impact?: number }>({});
  const [currentTime, setCurrentTime] = useState(0);
  const annotator = session.unikey;
  const [sync, setSync] = useState<SyncState | null>(null);
  const [selectedImuTime, setSelectedImuTime] = useState<number | null>(null);
  const [syncRole, setSyncRole] = useState<"start_tap" | "end_tap">("start_tap");
  const [review, setReview] = useState<ReviewDocument | null>(null);
  const [status, setStatus] = useState<RecordingStatus | null>(null);
  const [deleteArmed, setDeleteArmed] = useState(false);
  const [deleteConfirmation, setDeleteConfirmation] = useState("");
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [indexBusy, setIndexBusy] = useState(false);
  const [indexMessage, setIndexMessage] = useState("");
  const [copiedRecordingId, setCopiedRecordingId] = useState("");
  const [error, setError] = useState("");
  const video = useRef<HTMLVideoElement>(null);

  useEffect(() => {
    if (!selected && recordings.length) setSelected(recordings[0].recording_id);
  }, [recordings, selected]);

  useEffect(() => {
    if (!selected) return;
    const controller = new AbortController();
    setError("");
    setDoc(null);
    setTimeline(null);
    setSync(null);
    setReview(null);
    setStatus(null);
    setFrameTimes(null);
    Promise.all([
      api<AnnotationDocument>(`/api/v1/recordings/${selected}/annotations`, { signal: controller.signal }),
      api<{ time_s: number[]; values: number[][]; unit: string }>(`/api/v1/recordings/${selected}/timeline`, { signal: controller.signal }),
      api<SyncState>(`/api/v1/recordings/${selected}/sync`, { signal: controller.signal }),
      api<FrameTimes>(`/api/v1/recordings/${selected}/frame-times`, { signal: controller.signal }),
      api<ReviewDocument>(`/api/v1/recordings/${selected}/review`, { signal: controller.signal }),
      api<RecordingStatus>(`/api/v1/recordings/${selected}/status`, { signal: controller.signal })
    ]).then(([annotations, data, syncState, frames, reviewDocument, recordingStatus]) => {
      setDoc(annotations);
      setTimeline(data);
      setSync(syncState);
      setFrameTimes(frames);
      setReview(reviewDocument);
      setStatus(recordingStatus);
      setMarks({});
      setCurrentFrame(0);
      setExperimentWindow(null);
      setExperimentImuSample(null);
      setRecommendationOffsetSource("none");
      setDeleteArmed(false);
      setDeleteConfirmation("");
    }).catch((e) => {
      if ((e as Error).name !== "AbortError") setError((e as Error).message);
    });
    return () => controller.abort();
  }, [selected]);

  useEffect(() => {
    if (!frameTimes?.frame_count || !video.current) return;
    const previewFrame = nearestIndex(
      frameTimes.media_time_ns,
      frameTimes.media_time_ns[0] + INITIAL_VIDEO_PREVIEW_OFFSET_NS
    );
    video.current.currentTime = frameTimes.media_time_ns[previewFrame] / 1e9;
    setCurrentFrame(previewFrame);
    setCurrentTime(frameTimes.time_ns[previewFrame] / 1e9);
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
      const confirmedFormalAnchor = sync?.anchors.find((item) => item.role !== syncRole);
      const prior = confirmedFormalAnchor
        ? { value: confirmedFormalAnchor.video_time_ns - confirmedFormalAnchor.imu_time_ns, source: "formal_anchor" as const }
        : { value: null, source: "none" as const };
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

  const confirmFormalAnchor = () => {
    if (!canEdit || !sync || !experimentWindow || experimentImuSample === null || !frameTimes) return;
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
        (item) => item.role !== syncRole
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

  const mark = (name: "start" | "end" | "impact") => {
    if (!canEdit) return;
    setMarks((existing) => ({ ...existing, [name]: Math.round(currentTime * 1e9) }));
  };

  const jumpToRecordingTime = (timeNs: number) => {
    if (!frameTimes || !video.current) return;
    const frame = nearestIndex(frameTimes.time_ns, timeNs);
    video.current.pause();
    video.current.currentTime = frameTimes.media_time_ns[frame] / 1e9;
    setCurrentFrame(frame);
    setCurrentTime(frameTimes.time_ns[frame] / 1e9);
  };

  const setSegmentImpact = (segment: Segment) => {
    if (!canEdit || !doc) return;
    const timeNs = Math.round(currentTime * 1e9);
    if (timeNs <= segment.start_ns || timeNs >= segment.end_ns) {
      setError("撞击时刻必须严格位于该跌倒区间内");
      return;
    }
    const retained = doc.events.filter(
      (event) => !(event.segment_id === segment.segment_id && event.kind === "impact")
    );
    setDoc({
      ...doc,
      finalized: false,
      events: [
        ...retained,
        {
          segment_id: segment.segment_id,
          kind: "impact" as const,
          time_ns: timeNs,
          source_video_frame: null,
          source_imu_sample: null,
          annotator_id: annotator
        }
      ].sort((a, b) => a.time_ns - b.time_ns)
    });
    setError("");
  };

  const addAnnotationInterval = () => {
    if (!canEdit || !doc || marks.start === undefined || marks.end === undefined) return;
    if (marks.end <= marks.start) {
      setError("区间结束必须晚于区间开始");
      return;
    }
    if (
      annotationKind === "fall"
      && marks.impact !== undefined
      && (marks.impact <= marks.start || marks.impact >= marks.end)
    ) {
      setError("撞击时刻必须严格位于该跌倒区间内");
      return;
    }
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
    if (annotationKind === "fall") {
      events.push({ segment_id: segmentId, kind: "onset", time_ns: marks.start, source_video_frame: null, source_imu_sample: null, annotator_id: annotator });
      if (marks.impact !== undefined) {
        events.push({ segment_id: segmentId, kind: "impact", time_ns: marks.impact, source_video_frame: null, source_imu_sample: null, annotator_id: annotator });
      }
    }
    setDoc({ ...doc, segments: [...doc.segments, segment].sort((a, b) => a.start_ns - b.start_ns), events });
    setMarks({});
  };

  const save = async (finalized: boolean) => {
    if (!doc || !selected || !review || review.workflow.state !== "in_progress" || review.workflow.annotator_id !== annotator) return;
    setError("");
    try {
      const saved = await api<AnnotationDocument>(`/api/v1/recordings/${selected}/annotations`, {
        method: "PUT",
        body: JSON.stringify({
          expected_revision: review.revision,
          document: { ...doc, revision: doc.revision + 1, finalized }
        })
      });
      setDoc(saved);
      setReview(await api<ReviewDocument>(`/api/v1/recordings/${selected}/review`));
      setStatus(await api<RecordingStatus>(`/api/v1/recordings/${selected}/status`));
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const changeWorkflow = async (
    action: "assign" | "reopen"
  ) => {
    if (!selected || !review) return;
    if (
      action === "assign"
      && review.workflow.state === "in_progress"
      && review.workflow.annotator_id !== annotator
      && !window.confirm(`该任务当前由 ${review.workflow.annotator_id} 领取。确认接管吗？`)
    ) return;
    setError("");
    try {
      const saved = await api<ReviewDocument>(`/api/v1/recordings/${selected}/workflow`, {
        method: "POST",
        body: JSON.stringify({
          action,
          expected_revision: review.revision,
          comment: ""
        })
      });
      setReview(saved);
      setStatus(await api<RecordingStatus>(`/api/v1/recordings/${selected}/status`));
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const finalizeAndComplete = async () => {
    if (!selected || !review || !doc || !canEdit) return;
    setError("");
    try {
      const annotations = await api<AnnotationDocument>(`/api/v1/recordings/${selected}/annotations`, {
        method: "PUT",
        body: JSON.stringify({
          expected_revision: review.revision,
          document: { ...doc, revision: doc.revision + 1, finalized: true }
        })
      });
      setDoc(annotations);
      const finalizedReview = await api<ReviewDocument>(`/api/v1/recordings/${selected}/review`);
      const completed = await api<ReviewDocument>(`/api/v1/recordings/${selected}/workflow`, {
        method: "POST",
        body: JSON.stringify({
          action: "complete",
          expected_revision: finalizedReview.revision,
          comment: ""
        })
      });
      setReview(completed);
      setStatus(await api<RecordingStatus>(`/api/v1/recordings/${selected}/status`));
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const permanentlyDeleteRecording = async () => {
    if (!selected || deleteConfirmation !== `DELETE ${selected}`) return;
    setDeleteBusy(true);
    setError("");
    try {
      await api(`/api/v1/recordings/${selected}`, {
        method: "DELETE",
        body: JSON.stringify({ confirmation: deleteConfirmation })
      });
      const refreshed = await onChanged();
      setSelected(refreshed[0]?.recording_id ?? "");
      setDoc(null);
      setReview(null);
      setTimeline(null);
      setDeleteArmed(false);
      setDeleteConfirmation("");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setDeleteBusy(false);
    }
  };

  const refreshBucket = async () => {
    setIndexBusy(true);
    setIndexMessage("");
    setError("");
    try {
      const result = await api<{ imported: number; unchanged: number; skipped: number; issues: { recording_id: string; code: string; message: string }[] }>(
        "/api/v1/index/refresh",
        { method: "POST" }
      );
      await onChanged();
      const firstIssue = result.issues[0];
      setIndexMessage(
        `扫描完成：新增或更新 ${result.imported} 条，未变化 ${result.unchanged} 条，跳过异常 ${result.skipped} 条`
        + (firstIssue ? `；${firstIssue.recording_id} [${firstIssue.code}] ${firstIssue.message}` : "")
      );
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setIndexBusy(false);
    }
  };

  const copyRecordingId = async () => {
    if (!selected) return;
    try {
      await navigator.clipboard.writeText(selected);
      setCopiedRecordingId(selected);
      window.setTimeout(() => setCopiedRecordingId(""), 1800);
    } catch {
      setError("浏览器未允许写入剪贴板；可以直接选中上方录制 ID 复制");
    }
  };

  const choices = annotationKind === "exclude" ? [] : taxonomy[annotationKind];
  const saveSync = async (applyFixedOffset = false) => {
    if (!sync || !selected || !review || review.workflow.state !== "in_progress" || review.workflow.annotator_id !== annotator) return;
    setError("");
    try {
      const model = await api<Omit<SyncState, "anchors">>(`/api/v1/recordings/${selected}/sync`, {
        method: "PUT",
        body: JSON.stringify({
          expected_revision: review.revision,
          document: {
            anchors: sync.anchors,
            policy: "conditional_fixed_offset_v1",
            apply_fixed_offset: applyFixedOffset,
            reviewer_id: annotator
          }
        })
      });
      const timelineData = await api<{ time_s: number[]; values: number[][]; unit: string }>(`/api/v1/recordings/${selected}/timeline`);
      setSync({ ...sync, ...model });
      setTimeline(timelineData);
      setReview(await api<ReviewDocument>(`/api/v1/recordings/${selected}/review`));
    } catch (e) { setError((e as Error).message); }
  };

  const proposeTapExclusions = () => {
    if (!canEdit || !doc || !sync || !selected) return;
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
  const fallWithoutImpactCount = doc?.segments.filter(
    (segment) => segment.binary_label === "fall"
      && !doc.events.some((event) => event.segment_id === segment.segment_id && event.kind === "impact")
  ).length ?? 0;
  const hasFormalAnchors = sync?.anchors.some((item) => item.role === "start_tap")
    && sync.anchors.some((item) => item.role === "end_tap");
  const canEdit = review?.workflow.state === "in_progress"
    && review.workflow.annotator_id === annotator;
  const canClaim = review?.workflow.state === "unassigned";
  const canTakeOver = review?.workflow.state === "in_progress"
    && review.workflow.annotator_id !== annotator;
  const canReopen = review?.workflow.state === "completed"
    && (review.workflow.annotator_id === annotator || session.is_admin);
  const editDisabledReason = !review
    ? "正在读取任务状态"
    : review.workflow.state === "unassigned"
      ? "请先领取任务，领取后即可标记轻拍和动作区间"
      : review.workflow.state === "completed"
        ? "该任务已经完成；负责人或管理员重开后才能修改"
        : review.workflow.annotator_id !== annotator
          ? `任务当前由 ${review.workflow.annotator_id} 负责；接管后才能修改`
          : "";

  return (
    <main className="annotation-layout">
      <aside className="panel recording-list">
        <div className="panel-title">选择录制</div>
        <div className="recording-list-actions">
          {session.is_admin && <button onClick={refreshBucket} disabled={indexBusy}>{indexBusy ? "正在刷新…" : "刷新录制列表"}</button>}
          <button onClick={copyRecordingId} disabled={!selected}>{copiedRecordingId === selected ? "已复制" : "复制当前 ID"}</button>
        </div>
        {selected && <code className="selected-recording-id" title="可以直接框选复制">{selected}</code>}
        {indexMessage && <span className="index-message">{indexMessage}</span>}
        {recordings.map((recording) => <button key={recording.recording_id} className={selected === recording.recording_id ? "selected" : ""} onClick={() => setSelected(recording.recording_id)}><strong>{recording.participant_id} · {tierLabel(recording.data_tier)}</strong><span>{recording.recording_id}</span></button>)}
      </aside>
      <section className="annotation-workspace">
        {error && <div className="error-banner">{error}</div>}
        {!selected ? <div className="panel placeholder">从左侧选择一段录制</div> : <>
          {review && <div className={`panel task-gate ${canEdit ? "task-gate-ready" : ""}`}>
            <div>
              <div className="panel-title">任务状态 · {review.workflow.state === "unassigned" ? "未领取" : review.workflow.state === "in_progress" ? "标注中" : "已完成"}</div>
              <strong>{canEdit ? "你正在负责此任务，可以进行同步和标注。" : editDisabledReason}</strong>
              <details>
                <summary>技术详情</summary>
                <p className="stage-help">review revision {review.revision} · 当前负责人 {review.workflow.annotator_id ?? "无"} · 最后编辑者 {review.workflow.last_editor_id ?? "无"}</p>
              </details>
            </div>
            <div className="save-row">
              {(canClaim || canTakeOver) && <button className="primary" onClick={() => changeWorkflow("assign")}>{canTakeOver ? `接管 ${review.workflow.annotator_id} 的任务` : "领取并开始标注"}</button>}
              {canReopen && <button onClick={() => changeWorkflow("reopen")}>重开并继续修改</button>}
            </div>
          </div>}
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
              <button className="primary" title={editDisabledReason} onClick={loadExperimentWindow} disabled={!canEdit || !frameTimes || experimentBusy}>此帧是{syncRole === "start_tap" ? "开始" : "结束"}轻拍首次接触</button>
            </div>
            {!canEdit && <p className="stage-help warning-text">{editDisabledReason}</p>}
            <details><summary>视频与快捷键说明</summary><p className="stage-help">页面从约 0.2 秒处开始预览以避开摄像头启动过渡；原始第 0 帧仍完整保留。暂停到手首次接触 IMU 的画面后，可用键盘 ←/→ 或 ,/. 逐帧移动；帧号从 0 开始，所有同步和标注均使用真实逐帧时间戳。</p></details>
          </div>

          <div className="panel sync-panel">
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
                <span>{recommendationOffsetSource === "formal_anchor" ? "时间先验：本条已确认的另一端锚点" : "时间先验：共同主机时钟 0 ms"}</span>
                <span>实体响应 {experimentWindow.recommendation.distinct_response_count} 个 · 显著性 {experimentWindow.recommendation.event_robust_z?.toFixed(1) ?? "—"} · 时间残差 {experimentWindow.recommendation.timing_residual_ms?.toFixed(1) ?? "—"} ms · 区分比 {experimentWindow.recommendation.score_margin_ratio?.toFixed(2) ?? "唯一响应"}</span>
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
                <button className="primary" disabled={!canEdit || experimentImuSample === null || !selectedExperimentIsCandidate || experimentBusy} onClick={confirmFormalAnchor}>确认为{syncRole === "start_tap" ? "开始" : "结束"}轻拍</button>
                <button disabled={experimentBusy} onClick={() => { setExperimentWindow(null); setExperimentImuSample(null); }}>取消</button>
              </div>
              {selectedExperimentCandidate && <p className="stage-help">当前候选：事件显著性 {selectedExperimentCandidate.event_robust_z.toFixed(1)} · 本样本突变 {selectedExperimentCandidate.robust_z.toFixed(1)} · 推荐分数 {selectedExperimentCandidate.recommendation_score.toFixed(3)} · {selectedExperimentCandidate.recommendation_rank ? `独立响应排名 #${selectedExperimentCandidate.recommendation_rank}` : `同一响应簇 #${selectedExperimentCandidate.response_cluster_id ?? "—"} 的辅助峰`} · {selectedExperimentCandidate.selection_basis === "event_onset" ? "事件首个明显响应" : selectedExperimentCandidate.selection_basis === "timing_projection" ? "时间模型代表样本" : "局部突变峰"}</p>}
              {experimentImuSample !== null && !selectedExperimentIsCandidate && <p className="stage-help warning-text">当前选择不是加速度突变候选，请点击曲线主峰或上方候选按钮。</p>}
              {experimentWindow.recommendation.confidence === "low" && <p className="stage-help warning-text">自动推荐置信度低，请逐个比较候选；程序不会自动保存。</p>}
            </>}
            <div className="anchor-list">
              {sync?.anchors.map((anchor) => <div key={anchor.role}>
                <span>{anchor.role === "start_tap" ? "开始轻拍" : "结束轻拍"} · 帧 {anchor.source_video_frame ?? "—"} ({seconds(anchor.video_time_ns)}) ↔ 样本 {anchor.source_imu_sample ?? "—"} ({seconds(anchor.imu_time_ns)})</span>
                <button disabled={!canEdit} onClick={() => setSync({ ...sync, anchors: sync.anchors.filter((item) => item.role !== anchor.role), quality: "draft", recommendation: "none", decision: "host_only" })}>删除</button>
              </div>)}
            </div>
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
              <button className="primary" disabled={!canEdit || !hasFormalAnchors} onClick={() => saveSync(false)}>评估并保存主机时间</button>
              {sync?.recommendation === "apply_fixed_offset" && <button className="danger" disabled={!canEdit} onClick={() => saveSync(true)}>确认应用固定偏移</button>}
              <button disabled={!canEdit || !hasFormalAnchors || !doc} onClick={proposeTapExclusions}>生成轻拍排除区</button>
            </div>
            <details>
              <summary>高级同步数据</summary>
              <p className="stage-help">开始偏移 {sync ? seconds(sync.start_offset_ns) : "—"}；结束偏移 {sync ? seconds(sync.end_offset_ns) : "—"}；残差 RMS {sync && Number.isFinite(sync.residual_rms_ns) ? `${(sync.residual_rms_ns / 1e6).toFixed(2)} ms` : "—"}；策略 {sync?.policy ?? "—"}。</p>
            </details>
          </div>
          <div className="panel annotation-controls">
            <div className="panel-title">第 2 步 · 区间与跌倒事件标注</div>
            <div className="time-readout">当前 {currentTime.toFixed(3)} s · 帧 {currentFrame}</div>
            <div className="status-grid"><span>当前标注者 {annotator}</span><span>身份由登录会话验证，不可在页面切换</span></div>
            <div className="mark-buttons"><button disabled={!canEdit} onClick={() => mark("start")}>标记区间开始（I）</button><button disabled={!canEdit} onClick={() => mark("end")}>标记区间结束（O）</button><button disabled={!canEdit || annotationKind !== "fall"} onClick={() => mark("impact")}>标记撞击时刻（2）</button></div>
            <div className="marks"><span>区间开始／跌倒起始 {seconds(marks.start)}</span><span>区间结束 {seconds(marks.end)}</span><span>撞击时刻 {seconds(marks.impact)}</span></div>
          <div className="segment-form"><select disabled={!canEdit} value={annotationKind} onChange={(e) => setAnnotationKind(e.target.value as typeof annotationKind)}><option value="non_fall">非跌倒 · 进入训练</option><option value="fall">跌倒 · 进入训练</option><option value="exclude">明确排除 · 不训练</option></select>{annotationKind === "exclude" ? <select disabled={!canEdit} value={exclusionReason} onChange={(e) => setExclusionReason(e.target.value as Exclusion["reason"])}>{Object.entries(exclusionLabels).map(([value, display]) => <option value={value} key={value}>{display}</option>)}</select> : <select disabled={!canEdit} value={activity} onChange={(e) => setActivity(e.target.value)}>{choices.map((item) => <option value={item.code} key={item.code}>{isEnglish ? item.display_name_en : item.display_name_zh} · {item.code}</option>)}</select>}<button className="primary" disabled={!canEdit} onClick={addAnnotationInterval}>添加区间</button></div>
            {annotationKind === "fall" && <p className="stage-help">fall 从首次明确失衡开始，经过撞击，到落地后身体大动作停止并稳定。跌倒起始由区间起点自动派生；每个跌倒区间必须各自标记一个撞击时刻。准备阶段和稳定后的自然状态另标 non_fall。</p>}
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
            {doc.segments.map((segment) => {
              const impact = doc.events.find((event) => event.segment_id === segment.segment_id && event.kind === "impact");
              return <div className="segment-row" key={segment.segment_id}>
                <span>{segment.segment_id}</span>
                <strong>{segment.binary_label === "fall" ? "跌倒" : "非跌倒"}</strong>
                <span>{segment.activity_code}</span>
                <span>{seconds(segment.start_ns)} → {seconds(segment.end_ns)}</span>
                {segment.binary_label === "fall" && <span className={impact ? "success-text" : "warning-text"}>撞击 {seconds(impact?.time_ns)}</span>}
                {segment.binary_label === "fall" && impact && <button onClick={() => jumpToRecordingTime(impact.time_ns)}>跳转撞击帧</button>}
                {segment.binary_label === "fall" && <button disabled={!canEdit} onClick={() => setSegmentImpact(segment)}>{impact ? "当前帧重设撞击" : "当前帧设为撞击"}</button>}
                {segment.binary_label === "fall" && impact && <button disabled={!canEdit} onClick={() => setDoc({ ...doc, finalized: false, events: doc.events.filter((event) => !(event.segment_id === segment.segment_id && event.kind === "impact")) })}>清除撞击</button>}
                <button disabled={!canEdit} onClick={() => setDoc({ ...doc, segments: doc.segments.filter((item) => item.segment_id !== segment.segment_id), events: doc.events.filter((event) => event.segment_id !== segment.segment_id) })}>删除区间</button>
              </div>;
            })}
            <div className="panel-title subheading">明确排除区间</div>
            {doc.exclusions.map((item) => <div className="segment-row" key={item.exclusion_id}><span>{item.exclusion_id}</span><strong>排除</strong><span>{exclusionLabels[item.reason]}</span><span>{seconds(item.start_ns)} → {seconds(item.end_ns)}</span><button disabled={!canEdit} onClick={() => setDoc({ ...doc, exclusions: doc.exclusions.filter((candidate) => candidate.exclusion_id !== item.exclusion_id) })}>删除</button></div>)}
            {fallWithoutImpactCount > 0 && <div className="coverage-summary warning-text">仍有 {fallWithoutImpactCount} 个跌倒区间没有撞击时刻；草稿允许，完成时禁止。</div>}
            <div className="save-row"><button disabled={!canEdit} onClick={() => save(false)}>保存草稿（Ctrl+S）</button><button className="primary" disabled={!canEdit || selectedRecording?.data_tier !== "prod" || uncoveredNs > 0 || fallWithoutImpactCount > 0 || sync?.quality !== "verified"} onClick={finalizeAndComplete}>完成标注并生成训练 H5</button><span>{doc.finalized ? "标注已定稿" : "草稿"} · {sync?.quality === "verified" ? "同步已验证" : "同步待验证"}</span></div>
            {selectedRecording?.data_tier !== "prod" && <p className="stage-help warning-text">测试数据可以保存草稿和下载原始 H5，但不会完成为训练数据。</p>}
          </div>}
          {review && <div className="panel workflow-panel">
            <div className="panel-title">第 4 步 · 下载数据</div>
            <p className="stage-help">原始 H5 和当前标注快照始终可以下载；正式数据完成后自动生成统一 30 Hz 的训练 H5。</p>
            <details><summary>技术状态</summary><div className="status-grid"><span>数据级别 {selectedRecording?.data_tier === "prod" ? "正式数据" : "测试数据"}</span><span>校准 {status?.calibration === "verified" ? "已验证" : "未验证"}</span><span>导出 {status?.export === "exported" ? "已生成" : "未生成"}</span></div></details>
            <div className="save-row">
              <a className="button-link" href={`/api/v1/recordings/${selected}/capture-h5/download`} download>下载原始 capture.h5</a>
              <a className="button-link" href={`/api/v1/recordings/${selected}/review/download`} download>下载 review.json</a>
              {selectedRecording?.data_tier === "prod" && status?.export === "exported" && <a className="button-link primary" href={`/api/v1/recordings/${selected}/aligned30/download`} download>下载 aligned30.h5</a>}
            </div>
            {selectedRecording?.data_tier !== "prod" && <p className="stage-help warning-text">当前是测试数据：可以下载原始 capture.h5 和 review.json，用于联调和结构展示，但不会生成训练 H5 或进入训练快照。</p>}
            <div className="danger-zone">
              <strong>永久删除整条录制</strong>
              <p className="stage-help">删除会立即从标注列表隐藏，并清除这条录制的原始文件、预览、标注和当前导出。已经生成的自包含训练快照不受影响；对象存储仍按存储桶策略提供 7 天软删除恢复窗口。</p>
              {!deleteArmed
                ? <button className="danger" onClick={() => setDeleteArmed(true)}>开始永久删除</button>
                : <>
                  <label>二次确认：请输入 <code>DELETE {selected}</code><input value={deleteConfirmation} onChange={(event) => setDeleteConfirmation(event.target.value)} /></label>
                  <div className="save-row">
                    <button className="danger" disabled={deleteBusy || deleteConfirmation !== `DELETE ${selected}`} onClick={permanentlyDeleteRecording}>确认删除并立即隐藏</button>
                    <button disabled={deleteBusy} onClick={() => { setDeleteArmed(false); setDeleteConfirmation(""); }}>取消</button>
                  </div>
                </>}
            </div>
          </div>}
        </>}
      </section>
    </main>
  );
}

function TrainingSnapshotsPage({ session }: { session: Session }) {
  const [snapshots, setSnapshots] = useState<TrainingSnapshot[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");

  const refresh = async () => {
    setSnapshots(await api<TrainingSnapshot[]>("/api/v1/training-snapshots"));
  };

  useEffect(() => {
    refresh().catch((value) => setError((value as Error).message));
  }, []);

  const createSnapshot = async () => {
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const result = await api<TrainingSnapshot>("/api/v1/training-snapshots", { method: "POST" });
      setMessage(result.created
        ? `已创建训练快照 ${result.snapshot_id}`
        : `内容没有变化，继续使用已有快照 ${result.snapshot_id}`);
      await refresh();
    } catch (value) {
      setError((value as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const deleteSnapshot = async (snapshotId: string) => {
    const confirmation = window.prompt(`清理后需要重新构建才能下载。请输入：\nDELETE ${snapshotId}`);
    if (confirmation === null) return;
    setBusy(true);
    setError("");
    setMessage("");
    try {
      await api(`/api/v1/training-snapshots/${snapshotId}`, {
        method: "DELETE",
        body: JSON.stringify({ confirmation })
      });
      setMessage(`已清理训练快照 ${snapshotId}`);
      await refresh();
    } catch (value) {
      setError((value as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return <main>
    {error && <div className="error-banner">{error}</div>}
    {message && <div className="success-banner">{message}</div>}
    <section className="panel library">
      <div className="panel-title">训练快照</div>
      <p className="stage-help">点击时冻结当前所有已完成的正式数据。相同内容复用同一个 TAR；构建期间的后续标注变化不会改变本次快照。</p>
      <div className="save-row">
        <button className="primary" disabled={busy} onClick={createSnapshot}>{busy ? "正在处理…" : "生成当前训练快照"}</button>
        <button disabled={busy} onClick={() => refresh().catch((value) => setError((value as Error).message))}>刷新列表</button>
        <span>当前操作者 {session.unikey} · 所有成员可生成和下载，管理员可清理历史快照</span>
      </div>
      {snapshots.length === 0 ? <span className="muted">目前还没有训练快照。</span> : <>
        <SnapshotRow snapshot={snapshots[0]} current session={session} busy={busy} onDelete={deleteSnapshot} />
        {snapshots.length > 1 && <details className="snapshot-history">
          <summary>历史快照（{snapshots.length - 1}）</summary>
          {snapshots.slice(1).map((snapshot) => <SnapshotRow key={snapshot.snapshot_id} snapshot={snapshot} session={session} busy={busy} onDelete={deleteSnapshot} />)}
        </details>}
      </>}
    </section>
  </main>;
}

function SnapshotRow({ snapshot, current = false, session, busy, onDelete }: { snapshot: TrainingSnapshot; current?: boolean; session: Session; busy: boolean; onDelete: (snapshotId: string) => void }) {
  return <article className={current ? "current-snapshot" : ""}>
    <div>
      <strong>{current ? "当前训练快照" : "历史训练快照"} · {snapshot.snapshot_id}</strong>
      <span>{snapshot.recording_count} 条录制 · {(snapshot.archive_size_bytes / 1024 ** 2).toFixed(2)} MiB · 创建者 {snapshot.created_by ?? "未知"}</span>
      <details><summary>校验信息</summary><span>SHA-256 {snapshot.archive_sha256}</span></details>
    </div>
    <div className="save-row">
      <a className="button-link primary" href={`/api/v1/training-snapshots/${snapshot.snapshot_id}/download`} download>下载 TAR</a>
      {session.is_admin && <button className="danger" disabled={busy} onClick={() => onDelete(snapshot.snapshot_id)}>清理快照</button>}
    </div>
  </article>;
}

function CaptureLibrary({ recordings, onChanged }: { recordings: Recording[]; onChanged: () => void }) {
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState("");
  const [incomplete, setIncomplete] = useState<{ relative_path: string; size_bytes: number; reason: string }[]>([]);

  useEffect(() => {
    const pending = recordings.filter((recording) => recording.index_state === "pending");
    if (pending.length === 0) return;
    let active = true;
    const poll = async () => {
      await Promise.allSettled(
        pending.map((recording) =>
          api(`/api/v1/recordings/${recording.recording_id}/publish/status`)
        )
      );
      if (active) onChanged();
    };
    poll();
    const timer = window.setInterval(poll, 3_000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [recordings.map((item) => `${item.recording_id}:${item.index_state}`).join("|")]);

  const publish = async (recording: Recording) => {
    setError("");
    setMessage("");
    setBusy(recording.recording_id);
    try {
      const estimate = await api<{ estimated_bytes: number }>(`/api/v1/recordings/${recording.recording_id}/publish/estimate`);
      const gib = estimate.estimated_bytes / 1024 ** 3;
      if (!window.confirm(`将生成浏览代理并发布 H5、原始 MKV、代理 MP4 和 manifest。\n预计读取或上传约 ${gib.toFixed(2)} GiB，继续吗？`)) return;
      await api(`/api/v1/recordings/${recording.recording_id}/publish`, { method: "POST" });
      setMessage(`已上传 Bucket，正在等待标注端接收：${recording.recording_id}`);
      onChanged();
    } catch (e) {
      setError((e as Error).message);
      onChanged();
    } finally {
      setBusy("");
    }
  };

  const deleteRecording = async (recordingId: string) => {
    const confirmation = window.prompt(`这只删除本机副本；已经发布到云端的录制不会被删除。请输入完整 recording_id：\n${recordingId}`);
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
    try { setIncomplete(await api("/api/v1/maintenance/incomplete")); }
    catch (e) { setError((e as Error).message); }
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

  return <main>
    {error && <div className="error-banner">{error}</div>}
    {message && <div className="success-banner">{message}</div>}
    <section className="panel library">
      <div className="panel-title">本地录制与手动发布</div>
      <p className="stage-help">这里只负责确认采集结果并交给标注存储；同步、标注和训练快照在独立标注平台完成。</p>
      {recordings.map((recording) => <article key={recording.recording_id}>
        <div><strong>{recording.recording_id}</strong><span>{recording.collection_id} · {recording.participant_id} · {tierLabel(recording.data_tier)} · {seconds(recording.duration_ns)}</span></div>
        <div className="status-grid">
          <span>采集 {stateLabel(recording.state)}</span>
          <span>{["uploaded", "published"].includes(recording.upload_state) ? "已上传 Bucket" : `上传 ${recording.upload_state}`}</span>
          {recording.index_state === "indexed" && <span>标注端已接收</span>}
          {recording.index_state === "pending" && <span>等待标注端接收</span>}
          {recording.index_state === "rejected" && <span className="warning-text">标注端拒绝</span>}
        </div>
        {recording.index_message && <p className={recording.index_state === "rejected" ? "warning-text" : "stage-help"}>{recording.index_message}</p>}
        <div className="save-row">
          <button className="primary" disabled={recording.state !== "ready" || busy === recording.recording_id} onClick={() => publish(recording)}>{busy === recording.recording_id ? "正在发布…" : ["uploaded", "published"].includes(recording.upload_state) ? "重新校验发布" : "估算并发布"}</button>
          <button className="danger" disabled={Boolean(busy)} onClick={() => deleteRecording(recording.recording_id)}>永久删除</button>
        </div>
        {[...recording.issues, ...(recording.validation_issues ?? [])].length > 0 && <ul>{[...recording.issues, ...(recording.validation_issues ?? [])].map((issue) => <li key={issue}>{issueLabel(issue)}</li>)}</ul>}
        {(recording.quality_warnings ?? []).length > 0 && <ul className="warning-text">{recording.quality_warnings?.map((warning) => <li key={warning}>质量警告（允许发布）：{issueLabel(warning)}</li>)}</ul>}
      </article>)}
    </section>
    <section className="panel library">
      <div className="panel-title">本地维护</div>
      <div className="save-row"><button onClick={scanIncomplete}>扫描不完整文件</button><button onClick={rebuild}>从目录重建索引</button></div>
      {incomplete.length === 0 ? <span className="muted">尚未扫描，或未发现不完整文件。</span> : incomplete.map((item) => <article key={item.relative_path}><div><strong>{item.relative_path}</strong><span>{item.reason} · {(item.size_bytes / 1024 ** 2).toFixed(2)} MiB</span></div><button onClick={() => quarantine(item.relative_path)}>隔离</button></article>)}
    </section>
  </main>;
}
