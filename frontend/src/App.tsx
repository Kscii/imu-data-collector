import { useEffect, useMemo, useRef, useState } from "react";
import Plot, { type PlotMarker, type PlotRegion, type PlotSelectionLabel } from "./Plot";
import { resolveAnnotationShortcut } from "./annotationShortcuts";
import { intervalFollowAnchorIndex, intervalsAtTime } from "./annotationTimeline";
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

type AppTab = "capture" | "characterize" | "annotate" | "calibration" | "taxonomy" | "library" | "datasets";
type AnnotationTaskTab = "sync" | "annotate" | "review" | "data";
type AnnotationSaveState = "idle" | "saving" | "saved" | "error" | "conflict";

const CAPTURE_FORM_KEY = "imu-capture-form-v1";
const FALL_ACTIVITY_COLORS = ["#ef4444", "#f97316", "#e11d48", "#d946ef", "#a855f7", "#f59e0b", "#fb7185", "#c026d3"];
const NON_FALL_ACTIVITY_COLORS = ["#22c55e", "#06b6d4", "#3b82f6", "#84cc16", "#14b8a6", "#0ea5e9", "#6366f1", "#10b981"];

function colorWithAlpha(color: string, alpha: number) {
  const red = Number.parseInt(color.slice(1, 3), 16);
  const green = Number.parseInt(color.slice(3, 5), 16);
  const blue = Number.parseInt(color.slice(5, 7), 16);
  return `rgba(${red}, ${green}, ${blue}, ${alpha})`;
}

function stableActivityColor(binaryLabel: "fall" | "non_fall", code: string, entries: TaxonomyEntry[]) {
  const palette = binaryLabel === "fall" ? FALL_ACTIVITY_COLORS : NON_FALL_ACTIVITY_COLORS;
  const taxonomyIndex = entries.findIndex((entry) => entry.code === code);
  if (taxonomyIndex >= 0) return palette[taxonomyIndex % palette.length];
  const hash = Array.from(code).reduce((value, character) => ((value * 31) + character.charCodeAt(0)) >>> 0, 0);
  return palette[hash % palette.length];
}

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
    ? { annotate: "annotate", calibration: "calibration", taxonomy: "taxonomy", training: "library", datasets: "datasets" }
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
  publish_target?: "disabled" | "local" | "broker" | "direct_gcs";
  index_state?: "not_requested" | "pending" | "indexed" | "rejected";
  index_message?: string;
  manifest_generation?: number | null;
  purpose?: "annotation" | "calibration_evidence";
  h5_path?: string | null;
  mkv_path?: string | null;
  finalization_job?: BackgroundJob | null;
  upload_job?: BackgroundJob | null;
};

type BackgroundJob = {
  kind: "finalize" | "publish";
  state: "queued" | "running" | "waiting_auth" | "retry_wait" | "succeeded" | "failed";
  phase: string;
  attempts: number;
  max_attempts: number;
  progress_bytes?: number;
  total_bytes?: number;
  next_attempt_at_utc?: string | null;
  last_error?: string | null;
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

type CalibrationEvidenceAnalysis = {
  recording_id: string;
  video: {
    frame_count: number;
    recording_time_ns: number[];
    media_time_ns: number[];
  };
  imu: {
    sample_index: number[];
    time_ns: number[];
    time_s: number[];
    raw_counts: number[][];
    values_si: number[][];
    trailer: number[][];
    frame_hex: string[];
  };
  conversion: {
    available: boolean;
    source: "runtime_authoritative_profile";
    profile_id: string | null;
    evidence_sha256: string | null;
    error: string | null;
  };
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

type OrderedAnnotationInterval =
  | { kind: "segment"; key: string; start_ns: number; end_ns: number; segment: Segment }
  | { kind: "exclusion"; key: string; start_ns: number; end_ns: number; exclusion: Exclusion };

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
  revision: number;
  fall: TaxonomyEntry[];
  non_fall: TaxonomyEntry[];
};

type TaxonomyEntry = {
  code: string;
  name: string;
  active: boolean;
  usage_count?: number;
};

type TaxonomyMigrationPreview = {
  taxonomy_version: string;
  binary_label: "fall" | "non_fall";
  source_code: string;
  target_code: string;
  source_active: boolean;
  target_active: boolean;
  affected_recordings: number;
  affected_segments: number;
  recordings: {
    recording_id: string;
    data_tier: "test" | "prod";
    workflow_state: "unassigned" | "in_progress" | "completed";
    review_revision: number;
    annotation_revision: number;
    segment_count: number;
  }[];
  plan_token: string;
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
  publish?: {
    mode: "disabled" | "local" | "broker" | "direct_gcs";
    backend: "local" | "gcs" | "broker";
    bucket?: string | null;
    cloud_configured?: boolean;
  };
};

type CloudStatus = {
  configured: boolean;
  logged_in: boolean;
  email: string | null;
  broker_url: string | null;
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
  benchmark?: {
    snapshot_id: string;
    hdf5_object_key: string;
    hdf5_sha256: string;
    logical_content_sha256: string;
    hdf5_size_bytes: number;
    manifest_object_key: string;
    manifest_sha256: string;
    current_object_key: string;
  } | null;
  created?: boolean;
};

type DatasetCatalogFile = {
  dataset_id: string;
  filename: string;
  size_bytes: number;
  sha256: string;
  logical_content_sha256: string;
  hdf5_schema_version: string;
  sampling_rate_hz: number;
  evaluation_role: "cross_validation" | "training_only";
  sequences: number;
  rows: number;
  annotations: number;
  events?: number;
  segments?: number;
  fall_sequences?: number;
  participants?: number;
  body_locations?: Record<string, number>;
  supervision?: Record<string, number>;
};

type DatasetCatalogSnapshot = {
  kind: "base" | "team";
  snapshot_id: string;
  current: boolean;
  created_at_utc: string;
  contract_version: string;
  manifest_sha256: string;
  source?: Record<string, unknown> | null;
  files: DatasetCatalogFile[];
};

type DatasetCatalogCollection = {
  kind: "base" | "team";
  available: boolean;
  current: DatasetCatalogSnapshot | null;
  history: DatasetCatalogSnapshot[];
  warnings: string[];
};

type DatasetCatalogDocument = {
  schema_version: "imu_dataset_catalog_v1";
  collections: DatasetCatalogCollection[];
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
    export_schema_version?: "1.0.0" | "2.0.0";
    hdf5_schema_version?: string;
    sampling_rate_hz?: number;
    filename?: string;
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

type ImuCandidate = {
  local_device_id: string;
  name: string | null;
};

type ImuBinding = {
  state: "bound" | "unbound";
  device_name: string;
  local_device_id: string | null;
  verified_at_utc: string | null;
};

type DeviceList = {
  cameras: Camera[];
  platform: string;
  imu_binding: ImuBinding;
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

class ApiRequestError extends Error {
  constructor(message: string, readonly status: number, readonly detail?: unknown) {
    super(message);
    this.name = "ApiRequestError";
  }
}

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
        throw new ApiRequestError(apiErrorMessage(detail, response.status, response.statusText), response.status, detail);
      }
      throw new ApiRequestError(apiErrorMessage(detail, response.status, response.statusText), response.status, detail);
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

function isTextEntryTarget(target: EventTarget | null) {
  if (!(target instanceof HTMLElement)) return false;
  return target.isContentEditable || ["INPUT", "SELECT", "TEXTAREA"].includes(target.tagName);
}

const activeJobStates = new Set(["queued", "running", "retry_wait"]);

function jobStateLabel(job: BackgroundJob) {
  const state: Record<BackgroundJob["state"], string> = {
    queued: "等待执行",
    running: "正在执行",
    waiting_auth: "等待 Google 登录",
    retry_wait: "等待自动重试",
    succeeded: "已完成",
    failed: "需要人工重试",
  };
  const phase: Record<string, string> = {
    queued: "排队",
    starting: "准备启动",
    copying_h5: "复制冻结的 H5",
    reconstructing_imu: "重建 IMU 时间轴",
    probing_source_video: "读取原始视频时间轴",
    normalizing_video: "规范化视频时间轴",
    probing_normalized_video: "复核规范化视频",
    writing_h5: "写入视频与同步信息",
    validating: "校验采集制品",
    committing: "原子提交",
    packaging: "生成发布制品",
    auth_required: "等待 Google 登录",
    uploading: "上传对象存储",
    verifying: "校验远端制品",
    completed: "完成",
  };
  const uploadRole = job.phase.startsWith("uploading:")
    ? job.phase.slice("uploading:".length)
    : "";
  return `${state[job.state]} · ${uploadRole ? `上传 ${uploadRole}` : phase[job.phase] ?? job.phase}`;
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
  const [dataTier, setDataTier] = useState<"test" | "prod">(captureForm.dataTier ?? "prod");
  const [captureError, setCaptureError] = useState("");
  const [captureOperation, setCaptureOperation] = useState("");
  const [recordings, setRecordings] = useState<Recording[]>([]);
  const [taxonomy, setTaxonomy] = useState<Taxonomy | null>(null);
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [session, setSession] = useState<Session | null>(null);
  const [cameras, setCameras] = useState<Camera[]>([]);
  const [cameraId, setCameraId] = useState(captureForm.cameraId ?? "");
  const [imuBinding, setImuBinding] = useState<ImuBinding | null>(null);
  const [imuCandidates, setImuCandidates] = useState<ImuCandidate[]>([]);
  const [imuLocalDeviceId, setImuLocalDeviceId] = useState("");
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
    if (annotationApplication && session && tab === "taxonomy" && !session.is_admin) {
      selectTab("annotate");
    }
  }, [annotationApplication, session, tab]);

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
  const refreshCameras = (force = false) =>
    api<DeviceList>(`/api/v1/devices${force ? "?refresh_cameras=true" : ""}`).then((value) => {
      setCameras(value.cameras);
      setImuBinding(value.imu_binding);
      if (value.imu_binding.local_device_id) {
        setImuLocalDeviceId(value.imu_binding.local_device_id);
      }
      setCameraId((current) => {
        if (value.cameras.some((item) => item.camera_id === current)) return current;
        const compatible = value.cameras.filter((item) => item.supports_default_profile && item.color_capture);
        return compatible.find((item) => item.integration === "external")?.camera_id
          ?? compatible[0]?.camera_id
          ?? "";
      });
    }).catch((e) => setCaptureError(e.message));

  const acceptPreviewError = (value: unknown) => {
    const error = value as ApiRequestError;
    const detail = error.detail as { code?: unknown; candidates?: unknown } | undefined;
    if (detail?.code === "imu_multiple_candidates" && Array.isArray(detail.candidates)) {
      const candidates = detail.candidates.filter((item): item is ImuCandidate => (
        Boolean(item)
        && typeof item === "object"
        && typeof (item as ImuCandidate).local_device_id === "string"
      ));
      setImuCandidates(candidates);
      if (candidates.length && !candidates.some((item) => item.local_device_id === imuLocalDeviceId)) {
        setImuLocalDeviceId(candidates[0].local_device_id);
      }
    }
    setCaptureError(error.message);
  };

  const forgetImuBinding = async () => {
    if (captureInteractionBlocked || captureOperation) return;
    try {
      const binding = await api<ImuBinding>("/api/v1/devices/imu-binding", { method: "DELETE" });
      setImuBinding(binding);
      setImuLocalDeviceId("");
      setImuCandidates([]);
      setCaptureError("");
    } catch (error) {
      setCaptureError((error as Error).message);
    }
  };

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
      const recordingsTimer = window.setInterval(() => {
        void refreshRecordings();
        void api<Taxonomy>("/api/v1/taxonomy").then(setTaxonomy).catch((e) => setCaptureError(e.message));
      }, 10_000);
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
        body: active ? undefined : JSON.stringify({
          camera_id: cameraId || null,
          imu_local_device_id: imuLocalDeviceId || null,
        })
      });
      setLive(snapshot);
      if (!active) liveRef.current = { t: [], values: [] };
    } catch (e) {
      acceptPreviewError(e);
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
        body: JSON.stringify({
          camera_id: cameraId || null,
          imu_local_device_id: imuLocalDeviceId || null,
        })
      });
      setLive(snapshot);
    } catch (e) {
      acceptPreviewError(e);
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
    <div className={`app-shell ${annotationApplication && tab === "annotate" ? "annotation-workbench-shell" : ""}`}>
      <header className={annotationApplication && tab === "annotate" ? "workbench-header" : ""}>
        <div>
          <span className="eyebrow">{annotationApplication ? "CW12EU-T · 独立标注" : "CW12EU-T · 本机采集"}</span>
          <h1>{annotationApplication ? "IMU 数据标注平台" : "IMU 数采平台"}</h1>
        </div>
        <div className={`state state-${live.state}`}>{annotationApplication ? session ? `当前登录 ${session.unikey}` : "正在验证身份" : live.session_type === "devices_preview" ? "设备预览" : stateLabel(live.state)}</div>
      </header>
      <nav className={annotationApplication && tab === "annotate" ? "workbench-nav" : ""}>
        {annotationApplication ? <><button className={tab === "annotate" ? "active" : ""} onClick={() => selectTab("annotate")}>标注与同步</button><button className={tab === "calibration" ? "active" : ""} onClick={() => selectTab("calibration")}>设备校准证据</button>{session?.is_admin && <button className={tab === "taxonomy" ? "active" : ""} onClick={() => selectTab("taxonomy")}>标签管理</button>}<button className={tab === "library" ? "active" : ""} onClick={() => selectTab("library")}>训练快照</button><button className={tab === "datasets" ? "active" : ""} onClick={() => selectTab("datasets")}>数据集</button></> : <>
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
          imuBinding={imuBinding}
          imuCandidates={imuCandidates}
          imuLocalDeviceId={imuLocalDeviceId}
          setImuLocalDeviceId={setImuLocalDeviceId}
          forgetImuBinding={forgetImuBinding}
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
      {annotationApplication && tab === "taxonomy" && taxonomy && session?.is_admin && <TaxonomyAdminPage taxonomy={taxonomy} onChanged={setTaxonomy} />}
      {annotationApplication && tab === "library" && session && <TrainingSnapshotsPage session={session} />}
      {annotationApplication && tab === "datasets" && <DatasetCatalogPage />}
      {!annotationApplication && tab === "library" && <CaptureLibrary
        recordings={recordings}
        onChanged={refreshRecordings}
        publishMode={config?.publish?.mode ?? "local"}
        cloudConfigured={Boolean(config?.publish?.cloud_configured)}
      />}
    </div>
  );
}

function CalibrationEvidencePage() {
  const [profile, setProfile] = useState<CalibrationEvidence | null>(null);
  const [selected, setSelected] = useState("");
  const [analysis, setAnalysis] = useState<CalibrationEvidenceAnalysis | null>(null);
  const [analysisError, setAnalysisError] = useState("");
  const [currentTime, setCurrentTime] = useState(0);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const video = useRef<HTMLVideoElement>(null);

  useEffect(() => {
    api<CalibrationEvidence>("/api/v1/calibration-evidence")
      .then((value) => {
        setProfile(value);
        setSelected(value.evidence.find((item) => item.available)?.recording_id ?? "");
      })
      .catch((value) => setError((value as Error).message));
  }, []);

  useEffect(() => {
    if (!selected) return;
    const controller = new AbortController();
    setAnalysis(null);
    setAnalysisError("");
    setCurrentTime(0);
    api<CalibrationEvidenceAnalysis>(`/api/v1/calibration-evidence/${selected}/analysis`, { signal: controller.signal })
      .then(setAnalysis)
      .catch((value) => {
        if ((value as Error).name !== "AbortError") setAnalysisError((value as Error).message);
      });
    return () => controller.abort();
  }, [selected]);

  if (error) return <main><div className="error-banner">{error}</div></main>;
  if (!profile) return <main><section className="panel">正在读取校准证据…</section></main>;
  const current = profile.evidence.find((item) => item.recording_id === selected);
  const copyId = async () => {
    if (!current) return;
    await navigator.clipboard.writeText(current.recording_id);
    setMessage(`已复制 ${current.recording_id}`);
  };
  const selectRecordingTime = (time: number) => {
    setCurrentTime(time);
    if (!analysis || !video.current || !analysis.video.recording_time_ns.length) return;
    const index = nearestIndex(
      analysis.video.recording_time_ns,
      Math.round(time * 1e9),
    );
    video.current.currentTime = analysis.video.media_time_ns[index] / 1e9;
  };
  const updateTimeFromVideo = () => {
    if (!analysis || !video.current || !analysis.video.media_time_ns.length) return;
    const index = nearestIndex(
      analysis.video.media_time_ns,
      Math.round(video.current.currentTime * 1e9),
    );
    setCurrentTime(analysis.video.recording_time_ns[index] / 1e9);
  };
  const currentSample = analysis
    ? nearestIndex(analysis.imu.time_s, currentTime)
    : -1;
  const rawRow = currentSample >= 0 ? analysis?.imu.raw_counts[currentSample] ?? [] : [];
  const siRow = currentSample >= 0 ? analysis?.imu.values_si[currentSample] ?? [] : [];
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
          <div className="panel calibration-video">
            <div className="panel-title">{tr("证据视频 · 与两条 IMU 曲线共享时间轴", "Evidence video · shared timeline with both IMU plots")}</div>
            <video ref={video} key={current.recording_id} controls preload="metadata" src={`/api/v1/calibration-evidence/${current.recording_id}/video`} onTimeUpdate={updateTimeFromVideo} onSeeked={updateTimeFromVideo} />
          </div>
          {analysisError && <div className="error-banner">{analysisError}</div>}
          {!analysis && !analysisError && <div className="panel">{tr("正在读取证据时间轴…", "Loading evidence timeline…")}</div>}
          {analysis && <>
            <div className="panel calibration-plot-panel">
              <div className="panel-title">{tr("完整证据 IMU · 原始计数（不可变）", "Full evidence IMU · raw counts (immutable)")}</div>
              <Plot time={analysis.imu.time_s} values={analysis.imu.raw_counts} cursorTime={currentTime} controlledCursor height={250} onSelectTime={selectRecordingTime} />
            </div>
            <div className="panel calibration-plot-panel">
              <div className="panel-title">{tr("完整证据 IMU · SI（按当前权威校准档案即时推导）", "Full evidence IMU · SI (derived at view time using the current authoritative profile)")}</div>
              {analysis.conversion.available
                ? <Plot time={analysis.imu.time_s} values={analysis.imu.values_si} cursorTime={currentTime} controlledCursor height={250} onSelectTime={selectRecordingTime} />
                : <div className="warning-banner">{tr("SI 暂不可用；原始计数和视频仍可复核。", "SI is currently unavailable; raw counts and video remain available for review.")} {userVisibleMessage(analysis.conversion.error)}</div>}
            </div>
            <div className="panel calibration-sample-detail">
              <div className="panel-title">{tr("当前样本精确值", "Exact values at current sample")}</div>
              <div className="calibration-sample-grid">
                <span><strong>{tr("录制时间", "Recording time")}</strong>{currentSample >= 0 ? `${analysis.imu.time_s[currentSample].toFixed(6)} s` : "—"}</span>
                <span><strong>{tr("样本序号", "Sample index")}</strong>{currentSample >= 0 ? analysis.imu.sample_index[currentSample] : "—"}</span>
                <span className="calibration-hex"><strong>{tr("BLE 帧（HEX）", "BLE frame (HEX)")}</strong><code>{currentSample >= 0 ? analysis.imu.frame_hex[currentSample] : "—"}</code></span>
                <span><strong>{tr("原始计数", "Raw counts")}</strong><code>{rawRow.length ? rawRow.map((value, index) => `${["a1", "a2", "a3", "g1", "g2", "g3"][index]}=${value}`).join(", ") : "—"}</code></span>
                <span><strong>SI</strong><code>{siRow.length ? siRow.map((value, index) => `${["ax", "ay", "az", "gx", "gy", "gz"][index]}=${value.toFixed(6)}`).join(", ") : "—"}</code></span>
              </div>
              <p className="stage-help">{tr("原始计数和 HEX 来自不可变证据 H5；SI 不回写原文件，始终由页面所示的当前权威 profile 推导。", "Raw counts and HEX come from the immutable evidence H5. SI is never written back and is always derived from the current authoritative profile shown by this page.")}</p>
            </div>
          </>}
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
    toggleImuPreview, retryPreview, imuBinding, imuCandidates, imuLocalDeviceId,
    setImuLocalDeviceId, forgetImuBinding, captureOperation, interactionBlocked, ownsCaptureTab
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
        <button disabled={interactionBlocked || active || busy} onClick={() => refreshCameras(true)}>重新扫描摄像头</button>
        {imuCandidates.length > 1 && <label>IMU 设备<select value={imuLocalDeviceId} onChange={(e) => setImuLocalDeviceId(e.target.value)} disabled={interactionBlocked || active || busy}>{imuCandidates.map((item: ImuCandidate) => <option key={item.local_device_id} value={item.local_device_id}>{item.name || "CW12EU-T"} · {item.local_device_id}</option>)}</select></label>}
        {imuBinding?.state === "bound" && <button disabled={interactionBlocked || active || busy || devicesPreview} onClick={forgetImuBinding}>忘记已绑定 IMU</button>}
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
      {live.preview_error && <div className="issues"><div>{userVisibleMessage(live.preview_error)}</div>{live.device?.error?.hint && <div>{userVisibleMessage(live.device.error.hint)}</div>}</div>}
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

function TaxonomyAdminPage({ taxonomy, onChanged }: { taxonomy: Taxonomy; onChanged: (value: Taxonomy) => void }) {
  const [definition, setDefinition] = useState<Taxonomy>(taxonomy);
  const [editingCode, setEditingCode] = useState("");
  const [binaryLabel, setBinaryLabel] = useState<"fall" | "non_fall">("non_fall");
  const [code, setCode] = useState("");
  const [name, setName] = useState("");
  const [migrationSource, setMigrationSource] = useState(taxonomy.non_fall[0]?.code ?? "");
  const [migrationTarget, setMigrationTarget] = useState("");
  const [migrationPreview, setMigrationPreview] = useState<TaxonomyMigrationPreview | null>(null);
  const [migrationConfirmation, setMigrationConfirmation] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");

  const refresh = async () => {
    const value = await api<Taxonomy>("/api/v1/taxonomy/admin");
    setDefinition(value);
    onChanged(value);
    return value;
  };

  useEffect(() => {
    refresh().catch((value) => setError((value as Error).message));
  }, []);

  const createActivity = async () => {
    setError("");
    setMessage("");
    setBusy("create");
    try {
      await api("/api/v1/taxonomy/activities", {
        method: "POST",
        body: JSON.stringify({
          expected_version: definition.version,
          binary_label: binaryLabel,
          code: code.trim(),
          name: name.trim(),
        })
      });
      await refresh();
      setCode("");
      setName("");
      setMessage(tr("活动标签已新增", "Activity label added"));
    } catch (value) {
      setError((value as Error).message);
    } finally {
      setBusy("");
    }
  };

  const updateActivity = async (entry: TaxonomyEntry, changes: { name?: string; active?: boolean }): Promise<boolean> => {
    setError("");
    setMessage("");
    setBusy(entry.code);
    try {
      await api(`/api/v1/taxonomy/activities/${encodeURIComponent(entry.code)}`, {
        method: "PATCH",
        body: JSON.stringify({ expected_version: definition.version, ...changes })
      });
      await refresh();
      setMessage(tr(`活动标签已更新：${entry.code}`, `Activity label updated: ${entry.code}`));
      return true;
    } catch (value) {
      setError((value as Error).message);
      if (value instanceof ApiRequestError && value.status === 409) {
        try {
          await refresh();
        } catch (refreshError) {
          setError(`${(value as Error).message}；${tr("刷新最新标签失败", "Failed to refresh the latest labels")}: ${(refreshError as Error).message}`);
        }
      }
      return false;
    } finally {
      setBusy("");
    }
  };

  const deleteActivity = async (entry: TaxonomyEntry) => {
    if (!window.confirm(tr(`永久删除从未使用的活动标签 ${entry.code}？`, `Permanently delete the unused activity label ${entry.code}?`))) return;
    setError("");
    setMessage("");
    setBusy(entry.code);
    try {
      await api(`/api/v1/taxonomy/activities/${encodeURIComponent(entry.code)}?expected_version=${encodeURIComponent(definition.version)}`, { method: "DELETE" });
      await refresh();
      setMessage(tr(`活动标签已删除：${entry.code}`, `Activity label deleted: ${entry.code}`));
    } catch (value) {
      setError((value as Error).message);
    } finally {
      setBusy("");
    }
  };

  const allEntries = [
    ...definition.fall.map((entry) => ({ ...entry, binaryLabel: "fall" as const })),
    ...definition.non_fall.map((entry) => ({ ...entry, binaryLabel: "non_fall" as const })),
  ];
  const selectedSource = allEntries.find((entry) => entry.code === migrationSource);
  const migrationTargets = allEntries.filter((entry) => (
    entry.binaryLabel === selectedSource?.binaryLabel
    && entry.code !== migrationSource
    && entry.active
  ));

  useEffect(() => {
    if (!selectedSource) {
      setMigrationSource(allEntries[0]?.code ?? "");
      return;
    }
    if (!migrationTargets.some((entry) => entry.code === migrationTarget)) {
      setMigrationTarget(migrationTargets[0]?.code ?? "");
    }
    setMigrationPreview(null);
    setMigrationConfirmation("");
  }, [migrationSource, definition.version]);

  const previewMigration = async () => {
    if (!migrationSource || !migrationTarget) return;
    setError("");
    setMessage("");
    setBusy("migration-preview");
    try {
      const value = await api<TaxonomyMigrationPreview>("/api/v1/taxonomy/migrations/preview", {
        method: "POST",
        body: JSON.stringify({
          expected_version: definition.version,
          source_code: migrationSource,
          target_code: migrationTarget,
        }),
      });
      setMigrationPreview(value);
      setMigrationConfirmation("");
    } catch (value) {
      setError((value as Error).message);
    } finally {
      setBusy("");
    }
  };

  const applyMigration = async () => {
    if (!migrationPreview) return;
    setError("");
    setMessage("");
    setBusy("migration-apply");
    try {
      const result = await api<{
        migrated: unknown[];
        conflicts: unknown[];
        failed: unknown[];
        remaining_usage: number;
        taxonomy: Taxonomy;
      }>("/api/v1/taxonomy/migrations/apply", {
        method: "POST",
        body: JSON.stringify({
          expected_version: migrationPreview.taxonomy_version,
          source_code: migrationPreview.source_code,
          target_code: migrationPreview.target_code,
          plan_token: migrationPreview.plan_token,
          confirmation: migrationConfirmation,
        }),
      });
      setDefinition(result.taxonomy);
      onChanged(result.taxonomy);
      setMigrationPreview(null);
      setMigrationConfirmation("");
      setMessage(tr(
        `迁移完成：成功 ${result.migrated.length} 条，冲突 ${result.conflicts.length} 条，失败 ${result.failed.length} 条`,
        `Migration finished: ${result.migrated.length} succeeded, ${result.conflicts.length} conflicted, ${result.failed.length} failed`,
      ));
    } catch (value) {
      setError((value as Error).message);
    } finally {
      setBusy("");
    }
  };

  const taxonomyGroup = (label: "fall" | "non_fall") => {
    const active = definition[label].filter((entry) => entry.active);
    const inactive = definition[label].filter((entry) => !entry.active);
    const taxonomyRow = (entry: TaxonomyEntry) => <TaxonomyAdminRow
      key={entry.code}
      entry={entry}
      editing={editingCode === entry.code}
      editBlocked={editingCode !== "" && editingCode !== entry.code}
      busy={busy !== ""}
      onStartEdit={() => setEditingCode(entry.code)}
      onCancelEdit={() => setEditingCode("")}
      onSaveName={async (target, nextName) => {
        const updated = await updateActivity(target, { name: nextName });
        if (updated) setEditingCode("");
        return updated;
      }}
      onToggle={updateActivity}
      onDelete={deleteActivity}
    />;
    return <section className="panel taxonomy-group" key={label}>
      <div className="panel-title">{label === "fall" ? tr("跌倒标签", "Fall labels") : tr("非跌倒标签", "Non-fall labels")}</div>
      {active.map(taxonomyRow)}
      {inactive.length > 0 && <details className="taxonomy-inactive-group">
        <summary>{tr(`已停用标签 · ${inactive.length}`, `Inactive labels · ${inactive.length}`)}</summary>
        {inactive.map(taxonomyRow)}
      </details>}
    </section>;
  };

  return <main className="taxonomy-admin">
    {error && <div className="error-banner">{error}</div>}
    {message && <div className="success-banner">{message}</div>}
    <section className="panel taxonomy-intro">
      <div><div className="panel-title">活动标签管理</div><strong>当前版本 {definition.version}</strong><p className="stage-help">code 是不可修改的机器标识，name 是标注页面使用的可修改显示名称。历史版本和训练快照保持不可变。</p></div>
      <span className="state">仅管理员</span>
    </section>
    <section className="panel taxonomy-create">
      <div className="panel-title">新增活动标签</div>
      <div className="taxonomy-create-fields">
        <label>标签类型<select value={binaryLabel} onChange={(event) => setBinaryLabel(event.target.value as "fall" | "non_fall")}><option value="non_fall">非跌倒</option><option value="fall">跌倒</option></select></label>
        <label>稳定 code<input value={code} onChange={(event) => setCode(event.target.value)} placeholder="例如 stair_climbing" /></label>
        <label>{tr("显示名称", "Display name")}<input value={name} onChange={(event) => setName(event.target.value)} placeholder={tr("例如 上楼梯", "For example, Stair climbing")} /></label>
        <button className="primary" disabled={busy !== "" || !code.trim() || !name.trim()} onClick={createActivity}>新增标签</button>
      </div>
    </section>
    {(["fall", "non_fall"] as const).map(taxonomyGroup)}
    <section className="panel taxonomy-migration">
      <div className="panel-title">{tr("历史标签迁移", "Historical label migration")}</div>
      <p className="stage-help">{tr("预览会冻结当前 taxonomy 版本和受影响 review 修订。执行后仅替换同一跌倒类型的当前标注，自动停用源标签，并为已完成正式数据生成新的活动导出；旧导出和快照不改写。", "Preview freezes the current taxonomy version and affected review revisions. Apply replaces the current label only within the same binary class, disables the source, and creates a new active export for completed production data. Old exports and snapshots remain unchanged.")}</p>
      <div className="taxonomy-migration-fields">
        <label>{tr("源标签", "Source label")}<select value={migrationSource} onChange={(event) => setMigrationSource(event.target.value)}>{allEntries.map((entry) => <option value={entry.code} key={entry.code}>{entry.binaryLabel} · {entry.name} ({entry.code}){entry.active ? "" : tr(" · 已停用", " · inactive")}</option>)}</select></label>
        <label>{tr("目标标签", "Target label")}<select value={migrationTarget} onChange={(event) => { setMigrationTarget(event.target.value); setMigrationPreview(null); setMigrationConfirmation(""); }}>{migrationTargets.map((entry) => <option value={entry.code} key={entry.code}>{entry.name} ({entry.code})</option>)}</select></label>
        <button disabled={busy !== "" || !migrationSource || !migrationTarget} onClick={previewMigration}>{tr("预览影响范围", "Preview impact")}</button>
      </div>
      {migrationPreview && <div className="taxonomy-migration-preview">
        <strong>{tr(`影响 ${migrationPreview.affected_recordings} 条录制、${migrationPreview.affected_segments} 个区间`, `${migrationPreview.affected_recordings} recordings and ${migrationPreview.affected_segments} intervals affected`)}</strong>
        <span>{tr("测试与正式数据都会迁移；已完成录制会保持完成状态。", "Both test and production data will be migrated; completed recordings remain completed.")}</span>
        {migrationPreview.recordings.length > 0 && <details><summary>{tr("查看受影响录制", "Show affected recordings")}</summary><div className="taxonomy-migration-recordings">{migrationPreview.recordings.map((item) => <code key={item.recording_id}>{item.recording_id} · {item.data_tier} · {item.workflow_state} · {item.segment_count}</code>)}</div></details>}
        <label>{tr("二次确认", "Confirmation")}<input value={migrationConfirmation} onChange={(event) => setMigrationConfirmation(event.target.value)} placeholder={`MIGRATE ${migrationPreview.source_code} TO ${migrationPreview.target_code}`} /></label>
        <button className="danger" disabled={busy !== "" || migrationConfirmation !== `MIGRATE ${migrationPreview.source_code} TO ${migrationPreview.target_code}`} onClick={applyMigration}>{tr("执行全部当前数据迁移", "Migrate all current data")}</button>
      </div>}
    </section>
  </main>;
}

function TaxonomyAdminRow({ entry, editing, editBlocked, busy, onStartEdit, onCancelEdit, onSaveName, onToggle, onDelete }: {
  entry: TaxonomyEntry;
  editing: boolean;
  editBlocked: boolean;
  busy: boolean;
  onStartEdit: () => void;
  onCancelEdit: () => void;
  onSaveName: (entry: TaxonomyEntry, name: string) => Promise<boolean>;
  onToggle: (entry: TaxonomyEntry, changes: { name?: string; active?: boolean }) => Promise<boolean>;
  onDelete: (entry: TaxonomyEntry) => Promise<void>;
}) {
  const [draftName, setDraftName] = useState(entry.name);
  const nameInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!editing) setDraftName(entry.name);
  }, [entry.name, editing]);

  useEffect(() => {
    if (!editing) return;
    const frame = window.requestAnimationFrame(() => {
      nameInput.current?.focus();
      nameInput.current?.select();
    });
    return () => window.cancelAnimationFrame(frame);
  }, [editing]);

  const normalizedName = draftName.trim();
  const canSave = !busy && normalizedName !== "" && normalizedName !== entry.name;
  const saveName = async () => {
    if (!canSave) return;
    await onSaveName(entry, normalizedName);
  };
  const cancelEdit = () => {
    if (busy) return;
    setDraftName(entry.name);
    onCancelEdit();
  };

  return <div className={`taxonomy-row ${entry.active ? "" : "taxonomy-row-inactive"}`}>
    {editing
      ? <div className="taxonomy-name-editor">
        <input
          ref={nameInput}
          aria-label={tr(`${entry.code} 的显示名称`, `Display name for ${entry.code}`)}
          value={draftName}
          disabled={busy}
          onChange={(event) => setDraftName(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              void saveName();
            } else if (event.key === "Escape") {
              event.preventDefault();
              cancelEdit();
            }
          }}
        />
        <button className="primary" disabled={!canSave} onClick={() => void saveName()}>{tr("保存", "Save")}</button>
        <button disabled={busy} onClick={cancelEdit}>{tr("取消", "Cancel")}</button>
      </div>
      : <button
        type="button"
        className="taxonomy-name-tag"
        disabled={busy || editBlocked}
        title={editBlocked
          ? tr("请先完成当前名称编辑", "Finish the current name edit first")
          : tr("点击编辑名称", "Click to edit name")}
        onClick={onStartEdit}
      >
        <span>{entry.name}</span>
        <span className="taxonomy-name-edit-icon" aria-hidden="true">✎</span>
      </button>}
    <code>{entry.code}</code>
    <span>{entry.usage_count ?? 0} 个区间</span>
    <span className={`state ${entry.active ? "state-ready" : "state-needs_attention"}`}>{entry.active ? "启用" : "已停用"}</span>
    <div className="taxonomy-row-actions">
      <button disabled={busy} onClick={() => onToggle(entry, { active: !entry.active })}>{entry.active ? "停用" : "恢复"}</button>
      {(entry.usage_count ?? 0) === 0
        ? <button className="danger" disabled={busy} onClick={() => onDelete(entry)}>永久删除</button>
        : <span className="taxonomy-delete-rule" title={tr("已被历史标注引用，保留 code 才能继续解释旧数据", "Referenced by historical annotations; keep the code so old data remains interpretable")}>{tr("已使用，仅可停用", "In use; disable only")}</span>}
    </div>
  </div>;
}

function AnnotationPage({ recordings, taxonomy, session, onChanged }: { recordings: Recording[]; taxonomy: Taxonomy; session: Session; onChanged: () => Promise<Recording[]> }) {
  const [selected, setSelected] = useState("");
  const [recordingDrawerOpen, setRecordingDrawerOpen] = useState(false);
  const [recordingQuery, setRecordingQuery] = useState("");
  const [taskTab, setTaskTab] = useState<AnnotationTaskTab>("sync");
  const [locatedIntervalKey, setLocatedIntervalKey] = useState("");
  const [reloadNonce, setReloadNonce] = useState(0);
  const [saveState, setSaveState] = useState<AnnotationSaveState>("idle");
  const [saveMessage, setSaveMessage] = useState("");
  const [doc, setDoc] = useState<AnnotationDocument | null>(null);
  const [timeline, setTimeline] = useState<{ time_s: number[]; values: number[][]; unit: string } | null>(null);
  const [frameTimes, setFrameTimes] = useState<FrameTimes | null>(null);
  const [currentFrame, setCurrentFrame] = useState(0);
  const [experimentWindow, setExperimentWindow] = useState<SyncWindow | null>(null);
  const [experimentImuSample, setExperimentImuSample] = useState<number | null>(null);
  const [experimentBusy, setExperimentBusy] = useState(false);
  const [recommendationOffsetSource, setRecommendationOffsetSource] = useState<"formal_anchor" | "none">("none");
  const [annotationKind, setAnnotationKind] = useState<"fall" | "non_fall" | "exclude">("non_fall");
  const [activity, setActivity] = useState(taxonomy.non_fall.find((item) => item.active)?.code ?? taxonomy.non_fall[0].code);
  const [exclusionReason, setExclusionReason] = useState<Exclusion["reason"]>("other");
  const [marks, setMarks] = useState<{ start?: number; end?: number; impact?: number }>({});
  const [currentTime, setCurrentTime] = useState(0);
  const annotator = session.unikey;
  const [sync, setSync] = useState<SyncState | null>(null);
  const [syncRole, setSyncRole] = useState<"start_tap" | "end_tap">("start_tap");
  const [review, setReview] = useState<ReviewDocument | null>(null);
  const [status, setStatus] = useState<RecordingStatus | null>(null);
  const [recordingTaxonomy, setRecordingTaxonomy] = useState<Taxonomy>(taxonomy);
  const [deleteArmed, setDeleteArmed] = useState(false);
  const [deleteConfirmation, setDeleteConfirmation] = useState("");
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [indexBusy, setIndexBusy] = useState(false);
  const [indexMessage, setIndexMessage] = useState("");
  const [copiedRecordingId, setCopiedRecordingId] = useState("");
  const [error, setError] = useState("");
  const video = useRef<HTMLVideoElement>(null);
  const intervalListRef = useRef<HTMLDivElement>(null);
  const retrySaveRef = useRef<(() => void) | null>(null);
  const locateTimerRef = useRef<number | null>(null);

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
    setLocatedIntervalKey("");
    setSaveState("idle");
    setSaveMessage("");
    retrySaveRef.current = null;
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
      setTaskTab(
        reviewDocument.workflow.state === "completed"
          ? "annotate"
          : syncState.quality === "verified"
            ? "annotate"
            : "sync"
      );
    }).catch((e) => {
      if ((e as Error).name !== "AbortError") setError((e as Error).message);
    });
    return () => controller.abort();
  }, [selected, reloadNonce]);

  useEffect(() => {
    const pending = saveState === "saving" || saveState === "error" || saveState === "conflict";
    if (!pending) return;
    const warn = (event: BeforeUnloadEvent) => event.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [saveState]);

  useEffect(() => () => {
    if (locateTimerRef.current !== null) window.clearTimeout(locateTimerRef.current);
  }, []);

  useEffect(() => {
    if (!doc || doc.taxonomy_version === taxonomy.version) {
      setRecordingTaxonomy(taxonomy);
      return;
    }
    const controller = new AbortController();
    api<Taxonomy>(`/api/v1/taxonomy?version=${encodeURIComponent(doc.taxonomy_version)}`, { signal: controller.signal })
      .then(setRecordingTaxonomy)
      .catch((value) => {
        if ((value as Error).name !== "AbortError") setError((value as Error).message);
      });
    return () => controller.abort();
  }, [doc?.taxonomy_version, taxonomy.version]);

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
    const choices = taxonomy[annotationKind].filter((item) => item.active);
    if (!choices.some((item) => item.code === activity)) {
      setActivity(choices[0]?.code ?? "");
    }
  }, [annotationKind, taxonomy.version]);

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
    if (!canMutate || !sync || !experimentWindow || experimentImuSample === null || !frameTimes) return;
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
    const nextSync = {
      ...sync,
      anchors,
      quality: "draft",
      recommendation: "none",
      decision: "host_only"
    };
    setSync(nextSync);
    setSyncRole(syncRole === "start_tap" ? "end_tap" : "start_tap");
    setExperimentWindow(null);
    setExperimentImuSample(null);
    void persistSync(nextSync);
  };

  const mark = (name: "start" | "end" | "impact") => {
    if (!canMutate) return;
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

  const registerSaveFailure = (value: unknown) => {
    const requestError = value as Error;
    const conflict = value instanceof ApiRequestError && value.status === 409;
    setSaveState(conflict ? "conflict" : "error");
    setSaveMessage(requestError.message);
    setError(requestError.message);
  };

  const persistAnnotations = async (nextDocument: AnnotationDocument, finalized = false) => {
    if (!selected || !review || review.workflow.state !== "in_progress" || review.workflow.annotator_id !== annotator || saveState === "saving") return false;
    const pendingDocument = { ...nextDocument, finalized };
    retrySaveRef.current = () => void persistAnnotations(pendingDocument, finalized);
    setDoc(pendingDocument);
    setSaveState("saving");
    setSaveMessage("");
    setError("");
    try {
      const saved = await api<AnnotationDocument>(`/api/v1/recordings/${selected}/annotations`, {
        method: "PUT",
        body: JSON.stringify({
          expected_revision: review.revision,
          document: { ...pendingDocument, revision: doc ? doc.revision + 1 : pendingDocument.revision + 1 }
        })
      });
      const [updatedReview, updatedStatus] = await Promise.all([
        api<ReviewDocument>(`/api/v1/recordings/${selected}/review`),
        api<RecordingStatus>(`/api/v1/recordings/${selected}/status`)
      ]);
      setDoc(saved);
      setReview(updatedReview);
      setStatus(updatedStatus);
      setSaveState("saved");
      retrySaveRef.current = null;
      return true;
    } catch (value) {
      registerSaveFailure(value);
      return false;
    }
  };

  const reloadCurrentRecording = () => {
    setSaveState("idle");
    setSaveMessage("");
    setError("");
    retrySaveRef.current = null;
    setReloadNonce((value) => value + 1);
  };

  const setSegmentImpact = (segment: Segment) => {
    if (!canMutate || !doc) return;
    const timeNs = Math.round(currentTime * 1e9);
    if (timeNs <= segment.start_ns || timeNs >= segment.end_ns) {
      setError("撞击时刻必须严格位于该跌倒区间内");
      return;
    }
    const retained = doc.events.filter(
      (event) => !(event.segment_id === segment.segment_id && event.kind === "impact")
    );
    const nextDocument = {
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
    };
    void persistAnnotations(nextDocument);
    setError("");
  };

  const addAnnotationInterval = () => {
    if (!canMutate || !doc || marks.start === undefined || marks.end === undefined) return;
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
      const nextDocument = {
        ...doc,
        exclusions: [...doc.exclusions, exclusion].sort((a, b) => a.start_ns - b.start_ns)
      };
      void persistAnnotations(nextDocument);
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
    const nextDocument = { ...doc, segments: [...doc.segments, segment].sort((a, b) => a.start_ns - b.start_ns), events };
    void persistAnnotations(nextDocument);
    setMarks({});
  };

  const save = async (finalized: boolean) => {
    if (!doc || !selected || !review || review.workflow.state !== "in_progress" || review.workflow.annotator_id !== annotator) return;
    await persistAnnotations(doc, finalized);
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
      setSaveState("idle");
      setSaveMessage("");
      if (action === "reopen") setTaskTab(sync?.quality === "verified" ? "annotate" : "sync");
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const finalizeAndComplete = async () => {
    if (!selected || !review || !doc || !canMutate) return;
    setSaveState("saving");
    setSaveMessage("");
    retrySaveRef.current = () => void finalizeAndComplete();
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
      setSaveState("saved");
      retrySaveRef.current = null;
      setTaskTab("annotate");
    } catch (value) {
      registerSaveFailure(value);
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

  const choices = annotationKind === "exclude" ? [] : taxonomy[annotationKind].filter((item) => item.active);
  const persistSync = async (nextSync: SyncState, applyFixedOffset = false) => {
    if (!selected || !review || review.workflow.state !== "in_progress" || review.workflow.annotator_id !== annotator || saveState === "saving") return false;
    setSync(nextSync);
    retrySaveRef.current = () => void persistSync(nextSync, applyFixedOffset);
    setSaveState("saving");
    setSaveMessage("");
    setError("");
    try {
      const model = await api<Omit<SyncState, "anchors">>(`/api/v1/recordings/${selected}/sync`, {
        method: "PUT",
        body: JSON.stringify({
          expected_revision: review.revision,
          document: {
            anchors: nextSync.anchors,
            policy: "conditional_fixed_offset_v1",
            apply_fixed_offset: applyFixedOffset,
            reviewer_id: annotator
          }
        })
      });
      const timelineData = await api<{ time_s: number[]; values: number[][]; unit: string }>(`/api/v1/recordings/${selected}/timeline`);
      const savedSync = { ...nextSync, ...model };
      setSync(savedSync);
      setTimeline(timelineData);
      const updatedReview = await api<ReviewDocument>(`/api/v1/recordings/${selected}/review`);
      setReview(updatedReview);
      setSaveState("saved");
      retrySaveRef.current = null;
      if (savedSync.quality === "verified") setTaskTab("annotate");
      return true;
    } catch (value) {
      registerSaveFailure(value);
      return false;
    }
  };

  const saveSync = async (applyFixedOffset = false) => {
    if (!sync) return;
    await persistSync(sync, applyFixedOffset);
  };

  const proposeTapExclusions = () => {
    if (!canMutate || !doc || !sync || !selected) return;
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
    const nextDocument = { ...doc, exclusions: [...retained, ...proposed].sort((a, b) => a.start_ns - b.start_ns) };
    void persistAnnotations(nextDocument);
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
  const saveLocked = saveState === "saving" || saveState === "error" || saveState === "conflict";
  const canMutate = canEdit && !saveLocked;
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
  const displayTaxonomy = review?.workflow.state === "completed" ? recordingTaxonomy : taxonomy;
  const activityDisplay = (code: string) => {
    const entry = [...displayTaxonomy.fall, ...displayTaxonomy.non_fall].find((item) => item.code === code);
    if (!entry) return code;
    return `${entry.name}${entry.active ? "" : tr(" · 已停用", " · inactive")}`;
  };
  const filteredRecordings = recordings.filter((recording) => {
    const needle = recordingQuery.trim().toLowerCase();
    if (!needle) return true;
    return `${recording.recording_id} ${recording.participant_id}`.toLowerCase().includes(needle);
  });
  const orderedAnnotationIntervals: OrderedAnnotationInterval[] = doc ? [
    ...doc.segments.map((segment) => ({
      kind: "segment" as const,
      key: `segment:${segment.segment_id}`,
      start_ns: segment.start_ns,
      end_ns: segment.end_ns,
      segment,
    })),
    ...doc.exclusions.map((exclusion) => ({
      kind: "exclusion" as const,
      key: `exclusion:${exclusion.exclusion_id}`,
      start_ns: exclusion.start_ns,
      end_ns: exclusion.end_ns,
      exclusion,
    })),
  ].sort((left, right) => left.start_ns - right.start_ns || left.end_ns - right.end_ns || left.key.localeCompare(right.key)) : [];
  const currentTimeNs = Math.round(currentTime * 1e9);
  const matchingAnnotationIntervals = intervalsAtTime(orderedAnnotationIntervals, currentTimeNs);
  const activeAnnotationInterval = matchingAnnotationIntervals.length === 1 ? matchingAnnotationIntervals[0] : undefined;
  const currentAnnotationLabels: PlotSelectionLabel[] = matchingAnnotationIntervals.length > 1
    ? [{ key: "annotation-conflict", label: tr(`区间重叠错误 · ${matchingAnnotationIntervals.length}`, `Overlapping intervals · ${matchingAnnotationIntervals.length}`), color: "#ef4444" }]
    : activeAnnotationInterval
      ? [activeAnnotationInterval.kind === "segment"
        ? {
          key: activeAnnotationInterval.key,
          label: `${activeAnnotationInterval.segment.binary_label === "fall" ? tr("跌倒", "Fall") : tr("非跌倒", "Non-fall")} · ${activityDisplay(activeAnnotationInterval.segment.activity_code)}`,
          color: stableActivityColor(activeAnnotationInterval.segment.binary_label, activeAnnotationInterval.segment.activity_code, displayTaxonomy[activeAnnotationInterval.segment.binary_label]),
        }
        : {
          key: activeAnnotationInterval.key,
          label: `${tr("排除", "Excluded")} · ${exclusionLabels[activeAnnotationInterval.exclusion.reason]}`,
          color: "#94a3b8",
        }]
      : [];
  const timelineRegions: PlotRegion[] = doc ? [
    ...doc.segments.map((segment) => {
      const borderColor = stableActivityColor(segment.binary_label, segment.activity_code, displayTaxonomy[segment.binary_label]);
      return {
        start: segment.start_ns / 1e9,
        end: segment.end_ns / 1e9,
        color: colorWithAlpha(borderColor, 0.18),
        borderColor,
        label: activityDisplay(segment.activity_code),
      };
    }),
    ...doc.exclusions.map((item) => ({
      start: item.start_ns / 1e9,
      end: item.end_ns / 1e9,
      color: "rgba(148, 163, 184, 0.14)",
      borderColor: "#94a3b8",
      label: exclusionLabels[item.reason],
    }))
  ] : [];
  const visibleActivityLegend = Array.from(new Map(
    timelineRegions
      .filter((region) => region.label && region.borderColor)
      .map((region) => [region.label, { label: region.label as string, color: region.borderColor as string }])
  ).values());
  const timelineMarkers: PlotMarker[] = doc ? doc.events
    .filter((event) => event.kind === "impact")
    .map((event) => ({
      time: event.time_ns / 1e9,
      label: "撞击",
      color: "#facc15",
      dashed: true
    })) : [];

  const scrollIntervalIntoFollowPosition = (key: string, behavior: ScrollBehavior = "smooth") => {
    const container = intervalListRef.current;
    const anchorIndex = intervalFollowAnchorIndex(orderedAnnotationIntervals, key);
    if (!container || anchorIndex < 0) return;
    const anchorKey = orderedAnnotationIntervals[anchorIndex].key;
    window.requestAnimationFrame(() => window.requestAnimationFrame(() => {
      const anchor = document.getElementById(`annotation-${anchorKey}`);
      if (!anchor || !intervalListRef.current) return;
      const listRect = intervalListRef.current.getBoundingClientRect();
      const anchorRect = anchor.getBoundingClientRect();
      intervalListRef.current.scrollTo({
        top: Math.max(0, intervalListRef.current.scrollTop + anchorRect.top - listRect.top),
        behavior,
      });
    }));
  };

  const selectAnnotationInterval = (item: OrderedAnnotationInterval, target: "start" | "end" = "start") => {
    jumpToRecordingTime(target === "start" ? item.start_ns : item.end_ns);
  };

  const jumpToSelectedIntervalEnd = () => {
    if (activeAnnotationInterval) selectAnnotationInterval(activeAnnotationInterval, "end");
  };

  const locateAnnotationInterval = (key: string) => {
    if (key === "annotation-conflict") {
      setTaskTab("review");
      return;
    }
    setTaskTab("annotate");
    setLocatedIntervalKey(key);
    if (locateTimerRef.current !== null) window.clearTimeout(locateTimerRef.current);
    scrollIntervalIntoFollowPosition(key);
    locateTimerRef.current = window.setTimeout(() => setLocatedIntervalKey(""), 1200);
  };

  useEffect(() => {
    if (taskTab !== "annotate" || !activeAnnotationInterval) return;
    scrollIntervalIntoFollowPosition(activeAnnotationInterval.key);
  }, [taskTab, activeAnnotationInterval?.key, orderedAnnotationIntervals.length]);

  const removeSyncAnchor = (role: SyncAnchor["role"]) => {
    if (!canMutate || !sync) return;
    const nextSync = {
      ...sync,
      anchors: sync.anchors.filter((item) => item.role !== role),
      quality: "draft" as const,
      recommendation: "none" as const,
      decision: "host_only" as const
    };
    void persistSync(nextSync);
  };

  const clearSegmentImpact = (segmentId: string) => {
    if (!canMutate || !doc) return;
    void persistAnnotations({
      ...doc,
      finalized: false,
      events: doc.events.filter((event) => !(event.segment_id === segmentId && event.kind === "impact"))
    });
  };

  const removeSegment = (segmentId: string) => {
    if (!canMutate || !doc) return;
    void persistAnnotations({
      ...doc,
      finalized: false,
      segments: doc.segments.filter((item) => item.segment_id !== segmentId),
      events: doc.events.filter((event) => event.segment_id !== segmentId)
    });
  };

  const removeExclusion = (exclusionId: string) => {
    if (!canMutate || !doc) return;
    void persistAnnotations({
      ...doc,
      finalized: false,
      exclusions: doc.exclusions.filter((item) => item.exclusion_id !== exclusionId)
    });
  };

  useEffect(() => {
    const handleShortcut = (event: KeyboardEvent) => {
      const action = resolveAnnotationShortcut(
        {
          code: event.code,
          key: event.key,
          shiftKey: event.shiftKey,
          ctrlKey: event.ctrlKey,
          metaKey: event.metaKey,
          altKey: event.altKey,
          repeat: event.repeat,
          isComposing: event.isComposing,
          textEntryFocused: isTextEntryTarget(event.target),
        },
        canEdit,
        annotationKind === "fall",
      );
      if (!action) return;
      event.preventDefault();
      if (action.kind === "step") stepFrame(action.delta);
      else if (action.kind === "jump_selected_end") jumpToSelectedIntervalEnd();
      else if (action.kind === "mark") mark(action.target);
      else if (action.kind === "select") setAnnotationKind(action.target);
      else if (saveState === "error") retrySaveRef.current?.();
      else if (saveState !== "conflict") void save(false);
    };
    window.addEventListener("keydown", handleShortcut);
    return () => window.removeEventListener("keydown", handleShortcut);
  }, [annotationKind, canEdit, currentFrame, currentTime, frameTimes, doc, review, saveState, selected]);

  return (
    <main className="annotation-workbench">
      <section className="annotation-workbench-bar">
        <button onClick={() => setRecordingDrawerOpen(true)}>录制列表</button>
        <div className="annotation-recording-summary">
          <strong>{selectedRecording ? `${selectedRecording.participant_id} · ${tierLabel(selectedRecording.data_tier)}` : "未选择录制"}</strong>
          {selected && <code title={selected}>{selected}</code>}
        </div>
        {review && <span className={`state state-${review.workflow.state === "completed" ? "ready" : review.workflow.state === "in_progress" ? "in_progress" : "needs_attention"}`}>
          {review.workflow.state === "unassigned" ? "未领取" : review.workflow.state === "in_progress" ? `标注中 · ${review.workflow.annotator_id}` : "已完成"}
        </span>}
        <span className={`save-indicator save-${saveState}`}>
          {saveState === "saving" ? "正在保存…" : saveState === "saved" ? "已保存" : saveState === "error" ? "保存失败" : saveState === "conflict" ? "版本冲突" : "无待保存修改"}
        </span>
        {(canClaim || canTakeOver) && <button className="primary" onClick={() => changeWorkflow("assign")}>{canTakeOver ? "接管任务" : "领取任务"}</button>}
        {canReopen && <button onClick={() => changeWorkflow("reopen")}>重开任务</button>}
        {(saveState === "error" || saveState === "conflict") && <button className="danger" onClick={saveState === "conflict" ? reloadCurrentRecording : () => retrySaveRef.current?.()}>{saveState === "conflict" ? "重新载入" : "重试保存"}</button>}
      </section>

      {recordingDrawerOpen && <>
        <button className="recording-drawer-backdrop" aria-label="关闭录制列表" onClick={() => setRecordingDrawerOpen(false)} />
        <aside className="recording-drawer">
          <div className="recording-drawer-header"><strong>选择录制</strong><button onClick={() => setRecordingDrawerOpen(false)}>关闭</button></div>
          <input autoFocus placeholder="搜索录制 ID 或参与者" value={recordingQuery} onChange={(event) => setRecordingQuery(event.target.value)} />
          <div className="recording-list-actions">
            {session.is_admin && <button onClick={refreshBucket} disabled={indexBusy}>{indexBusy ? "正在刷新…" : "刷新 Bucket"}</button>}
            <button onClick={copyRecordingId} disabled={!selected}>{copiedRecordingId === selected ? "已复制" : "复制当前 ID"}</button>
          </div>
          {indexMessage && <span className="index-message">{indexMessage}</span>}
          <div className="recording-drawer-list">
            {filteredRecordings.map((recording) => <button key={recording.recording_id} className={selected === recording.recording_id ? "selected" : ""} disabled={saveLocked} onClick={() => { setSelected(recording.recording_id); setRecordingDrawerOpen(false); }}><strong>{recording.participant_id} · {tierLabel(recording.data_tier)}</strong><span>{recording.recording_id}</span></button>)}
          </div>
        </aside>
      </>}

      {!selected ? <div className="panel placeholder">从“录制列表”选择一条录制</div> : <section className="annotation-workbench-body">
        <section className="annotation-media-pane">
          <div className="panel video-review workbench-video">
            <video
              ref={video}
              controls
              tabIndex={0}
              aria-keyshortcuts=", . Shift+, Shift+. I O 2 F N X E Control+S"
              src={`/api/v1/recordings/${selected}/video`}
              onTimeUpdate={(event) => updateVideoPosition(event.currentTarget.currentTime)}
              onSeeked={(event) => updateVideoPosition(event.currentTarget.currentTime)}
            />
            <div className="frame-controls workbench-frame-controls">
              <button title="Shift+," onClick={() => stepFrame(-5)} disabled={!frameTimes || currentFrame <= 0}>−5</button>
              <button title="," onClick={() => stepFrame(-1)} disabled={!frameTimes || currentFrame <= 0}>−1</button>
              <strong>{currentTime.toFixed(3)} s</strong>
              <span>{tr("帧", "Frame")} {currentFrame} / {frameTimes ? frameTimes.frame_count - 1 : "—"}</span>
              <button title="." onClick={() => stepFrame(1)} disabled={!frameTimes || currentFrame >= frameTimes.frame_count - 1}>+1</button>
              <button title="Shift+." onClick={() => stepFrame(5)} disabled={!frameTimes || currentFrame >= frameTimes.frame_count - 1}>+5</button>
              {taskTab === "sync" && <button className="primary" title={editDisabledReason || tr("选择当前视频帧并分析附近 IMU 响应", "Select the current video frame and analyze the nearby IMU response")} onClick={loadExperimentWindow} disabled={!canMutate || !frameTimes || experimentBusy}>{syncRole === "start_tap" ? tr("设为开始轻拍接触帧", "Set as start-tap contact frame") : tr("设为结束轻拍接触帧", "Set as end-tap contact frame")}</button>}
              <details className="shortcut-help"><summary>{tr("快捷键", "Shortcuts")}</summary><span>{tr(",/. 一帧 · Shift+,/. 五帧 · I/O 区间 · 2 撞击 · F/N/X 类型 · E 所选区间结尾 · Ctrl+S 重试保存", ",/. one frame · Shift+,/. five frames · I/O interval · 2 impact · F/N/X type · E selected interval end · Ctrl+S retry save")}</span></details>
            </div>
          </div>

          <div className="panel full-timeline-panel workbench-timeline">
            <div className="timeline-heading"><div><span>{tr("完整录制 IMU", "Full-recording IMU")}</span><small>{timeline?.unit ?? "—"} · {timeline?.time_s.length ?? 0} {tr("个显示点", "display points")}</small></div><div className="timeline-heading-actions">{visibleActivityLegend.length > 0 && <details className="timeline-activity-legend"><summary>{tr("动作颜色", "Activity colors")} · {visibleActivityLegend.length}</summary><div>{visibleActivityLegend.map((item) => <span key={item.label}><i style={{ background: item.color }} />{item.label}</span>)}</div></details>}{timelineMarkers.length > 0 && <details className="timeline-marker-legend"><summary>{tr("撞击标记", "Impact markers")} · {timelineMarkers.length}</summary><div>{timelineMarkers.map((marker) => <button type="button" key={`${marker.label}-${marker.time}`} onClick={() => jumpToRecordingTime(Math.round(marker.time * 1e9))}><i style={{ background: marker.color }} />{tr("撞击", "Impact")} · {marker.time.toFixed(3)} s</button>)}</div></details>}<strong>{currentTime.toFixed(3)} s</strong></div></div>
            {timeline && <Plot time={timeline.time_s} values={timeline.values} cursorTime={currentTime} markers={timelineMarkers} regions={timelineRegions} selectionLabels={currentAnnotationLabels} controlledCursor showMarkerKey={false} height={190} onSelectTime={(time) => jumpToRecordingTime(Math.round(time * 1e9))} onSelectLabel={locateAnnotationInterval} />}
          </div>
        </section>

        <section className="annotation-tools-pane">
          <nav className="annotation-task-tabs" aria-label="标注任务">
            {(["sync", "annotate", "review", "data"] as AnnotationTaskTab[]).map((item) => <button key={item} className={taskTab === item ? "active" : ""} onClick={() => setTaskTab(item)}>{item === "sync" ? "1 同步" : item === "annotate" ? "2 标注" : item === "review" ? "3 检查" : "4 数据"}</button>)}
          </nav>

          <div className={`annotation-task-scroll ${taskTab === "annotate" ? "annotation-task-scroll-annotate" : ""}`}>
            {error && <div className="error-banner">{error}</div>}
            {saveMessage && saveState !== "saved" && <p className="stage-help warning-text">{saveMessage}</p>}
            {review && !canEdit && <div className="task-notice"><strong>{editDisabledReason}</strong></div>}

            {taskTab === "sync" && <>
              <div className="panel compact-panel">
                <div className="panel-title">轻拍同步复核</div>
                <div className="sync-inputs">
                  <label>当前锚点<select value={syncRole} onChange={(event) => setSyncRole(event.target.value as "start_tap" | "end_tap")}><option value="start_tap">开始轻拍</option><option value="end_tap">结束轻拍</option></select></label>
                  <span>逐帧选择视频中的首次接触，再确认附近的 IMU 首次响应。</span>
                </div>
                {!experimentWindow ? <div className="placeholder compact">先在左侧逐帧定位，再点击“设为轻拍接触帧”</div> : <>
                  <div className={`recommendation-card confidence-${experimentWindow.recommendation.confidence}`}>
                    <strong>推荐样本 {experimentWindow.recommendation.sample_index ?? "—"} · 置信度 {experimentWindow.recommendation.confidence === "high" ? "高" : experimentWindow.recommendation.confidence === "medium" ? "中" : "低"}</strong>
                    <span>视频帧 {experimentWindow.video_frame_index} · IMU {experimentImuSample ?? "未选择"}</span>
                    <span>{experimentWindow.recommendation.reason}</span>
                  </div>
                  <Plot time={experimentWindow.time_s} values={experimentWindow.raw_counts} cursorTime={selectedExperimentTime} markers={experimentMarkers} height={220} onSelectTime={(time) => {
                    const candidates = experimentWindow.candidate_sample_index;
                    if (candidates.length) {
                      const candidateTimes = candidates.map((sampleIndex) => experimentWindow.time_s[experimentWindow.sample_index.indexOf(sampleIndex)]);
                      setExperimentImuSample(candidates[nearestIndex(candidateTimes, time)]);
                    } else {
                      setExperimentImuSample(experimentWindow.sample_index[nearestIndex(experimentWindow.time_s, time)]);
                    }
                  }} />
                  <div className="candidate-peaks">{experimentWindow.candidate_peaks.map((candidate) => <button key={candidate.sample_index} className={experimentImuSample === candidate.sample_index ? "selected" : ""} onClick={() => setExperimentImuSample(candidate.sample_index)}>#{candidate.sample_index} · {candidate.time_s.toFixed(3)} s · 强度 {candidate.strength_rank}</button>)}</div>
                  <div className="save-row"><button className="primary" disabled={!canMutate || experimentImuSample === null || !selectedExperimentIsCandidate || experimentBusy} onClick={confirmFormalAnchor}>确认{syncRole === "start_tap" ? "开始" : "结束"}锚点</button><button onClick={() => { setExperimentWindow(null); setExperimentImuSample(null); }}>取消</button></div>
                  {selectedExperimentCandidate && <details><summary>候选技术数据</summary><p className="stage-help">事件显著性 {selectedExperimentCandidate.event_robust_z.toFixed(1)} · 样本突变 {selectedExperimentCandidate.robust_z.toFixed(1)} · 推荐分数 {selectedExperimentCandidate.recommendation_score.toFixed(3)} · 时间先验 {recommendationOffsetSource === "formal_anchor" ? "已确认锚点" : "共同主机时钟"}</p></details>}
                </>}
                <div className="anchor-list">{sync?.anchors.map((anchor) => <div key={anchor.role}><button onClick={() => jumpToRecordingTime(anchor.video_time_ns)}>{anchor.role === "start_tap" ? tr("开始", "Start") : tr("结束", "End")} · {tr("帧", "Frame")} {anchor.source_video_frame ?? "—"} · {seconds(anchor.video_time_ns)}</button><button disabled={!canMutate} onClick={() => removeSyncAnchor(anchor.role)}>{tr("删除", "Delete")}</button></div>)}</div>
              </div>
              <div className="panel compact-panel">
                <div className="panel-title">同步结论</div>
                <div className={`recommendation-card confidence-${sync?.quality === "verified" ? "high" : "low"}`}>
                  <strong>{sync?.quality === "verified" ? "同步已验证" : sync?.quality === "awaiting_confirmation" ? "等待确认固定偏移" : sync?.quality === "needs_review" ? "需要重新检查锚点" : "尚未评估"}</strong>
                  <span>估计偏移 {sync ? `${sync.estimated_offset_seconds >= 0 ? "+" : ""}${sync.estimated_offset_seconds.toFixed(3)} s` : "—"} · 首尾差 {sync ? `${(sync.anchor_disagreement_ns / 1e9).toFixed(3)} s` : "—"}</span>
                </div>
                <div className="save-row"><button className="primary" disabled={!canMutate || !hasFormalAnchors} onClick={() => saveSync(false)}>评估并保存</button>{sync?.recommendation === "apply_fixed_offset" && <button className="danger" disabled={!canMutate} onClick={() => saveSync(true)}>应用固定偏移</button>}<button disabled={!canMutate || !hasFormalAnchors || !doc} onClick={proposeTapExclusions}>生成轻拍排除区</button></div>
                <details><summary>同步规则与技术数据</summary><p className="stage-help">原始主机时间永不覆盖。时间比例固定为 1.0；仅当偏移明显且首尾一致时才建议固定平移。开始 {sync ? seconds(sync.start_offset_ns) : "—"} · 结束 {sync ? seconds(sync.end_offset_ns) : "—"} · RMS {sync && Number.isFinite(sync.residual_rms_ns) ? `${(sync.residual_rms_ns / 1e6).toFixed(2)} ms` : "—"}。</p></details>
              </div>
            </>}

            {taskTab === "annotate" && <div className="annotation-tab-layout">
              <div className="panel compact-panel annotation-controls">
                <div className="panel-title">创建区间</div>
                <div className="time-readout">{currentTime.toFixed(3)} s · {tr("帧", "frame")} {currentFrame}</div>
                <div className="mark-buttons"><button disabled={!canMutate} onClick={() => mark("start")}>起点 I</button><button disabled={!canMutate} onClick={() => mark("end")}>终点 O</button><button disabled={!canMutate || annotationKind !== "fall"} onClick={() => mark("impact")}>撞击 2</button></div>
                <div className="marks"><span>{tr("起", "Start")} {seconds(marks.start)}</span><span>{tr("止", "End")} {seconds(marks.end)}</span><span>{tr("撞击", "Impact")} {seconds(marks.impact)}</span></div>
                <div className="segment-form"><select disabled={!canMutate} value={annotationKind} onChange={(event) => setAnnotationKind(event.target.value as typeof annotationKind)}><option value="non_fall">非跌倒 · 训练</option><option value="fall">跌倒 · 训练</option><option value="exclude">明确排除</option></select>{annotationKind === "exclude" ? <select disabled={!canMutate} value={exclusionReason} onChange={(event) => setExclusionReason(event.target.value as Exclusion["reason"])}>{Object.entries(exclusionLabels).map(([value, display]) => <option value={value} key={value}>{display}</option>)}</select> : <select disabled={!canMutate} value={activity} onChange={(event) => setActivity(event.target.value)}>{choices.map((item) => <option value={item.code} key={item.code}>{item.name}</option>)}</select>}<button className="primary" disabled={!canMutate || marks.start === undefined || marks.end === undefined} onClick={addAnnotationInterval}>添加并保存</button></div>
                <details><summary>标注规范</summary><p className="stage-help">跌倒区间从首次明确失衡开始，到落地后身体大动作停止并稳定。区间起点同时表示 onset；每个跌倒区间必须有且仅有一个撞击时刻。准备阶段和稳定后的自然状态标为 non_fall。</p></details>
              </div>
              {doc && <section className="panel compact-panel interval-list-panel">
                <header className="interval-list-summary"><span>{tr("已标区间", "Annotated intervals")} · {orderedAnnotationIntervals.length}</span><small>{tr("按时间排序", "Chronological")}</small></header>
                <div className="interval-list-body" ref={intervalListRef}>
                  {orderedAnnotationIntervals.map((item) => {
                    if (item.kind === "exclusion") {
                      const exclusion = item.exclusion;
                      return <article id={`annotation-${item.key}`} className={`interval-card interval-exclude ${activeAnnotationInterval?.key === item.key ? "interval-selected" : ""} ${locatedIntervalKey === item.key ? "interval-located" : ""}`} key={item.key}>
                        <button className="interval-jump" onClick={() => selectAnnotationInterval(item)}><strong>{tr("排除", "Excluded")} · {exclusionLabels[exclusion.reason]}</strong><span>{seconds(exclusion.start_ns)} → {seconds(exclusion.end_ns)}</span></button>
                        <div className="interval-actions"><button onClick={() => selectAnnotationInterval(item, "start")}>{tr("到开头", "Go to start")}</button><button onClick={() => selectAnnotationInterval(item, "end")}>{tr("到结尾", "Go to end")} E</button><button disabled={!canMutate} onClick={() => removeExclusion(exclusion.exclusion_id)}>{tr("删除标注", "Delete annotation")}</button></div>
                      </article>;
                    }
                    const segment = item.segment;
                    const impact = doc.events.find((event) => event.segment_id === segment.segment_id && event.kind === "impact");
                    const intervalColor = stableActivityColor(segment.binary_label, segment.activity_code, displayTaxonomy[segment.binary_label]);
                    return <article id={`annotation-${item.key}`} className={`interval-card interval-${segment.binary_label} ${activeAnnotationInterval?.key === item.key ? "interval-selected" : ""} ${locatedIntervalKey === item.key ? "interval-located" : ""}`} style={{ borderLeftColor: intervalColor }} key={item.key}>
                      <button className="interval-jump" onClick={() => selectAnnotationInterval(item)}><strong>{segment.binary_label === "fall" ? tr("跌倒", "Fall") : tr("非跌倒", "Non-fall")} · {activityDisplay(segment.activity_code)}</strong><span>{seconds(segment.start_ns)} → {seconds(segment.end_ns)}</span></button>
                      {segment.binary_label === "fall" && <span className={impact ? "success-text" : "warning-text"}>{tr("撞击", "Impact")} {seconds(impact?.time_ns)}</span>}
                      <div className="interval-actions"><button onClick={() => selectAnnotationInterval(item, "start")}>{tr("到开头", "Go to start")}</button><button onClick={() => selectAnnotationInterval(item, "end")}>{tr("到结尾", "Go to end")} E</button>{segment.binary_label === "fall" && impact && <button onClick={() => jumpToRecordingTime(impact.time_ns)}>{tr("查看撞击", "View impact")}</button>}{segment.binary_label === "fall" && <button disabled={!canMutate} onClick={() => setSegmentImpact(segment)}>{impact ? tr("重设撞击", "Reset impact") : tr("设为撞击", "Set impact")}</button>}{segment.binary_label === "fall" && impact && <button disabled={!canMutate} onClick={() => clearSegmentImpact(segment.segment_id)}>{tr("清除撞击", "Clear impact")}</button>}<button disabled={!canMutate} onClick={() => removeSegment(segment.segment_id)}>{tr("删除标注", "Delete annotation")}</button></div>
                    </article>;
                  })}
                  <div className="interval-list-tail" aria-hidden="true" />
                </div>
              </section>}
            </div>}

            {taskTab === "review" && doc && <div className="panel compact-panel review-panel">
              <div className="panel-title">完整性检查</div>
              <div className="coverage-track" aria-label="标注覆盖时间轴">{durationNs > 0 && doc.segments.map((segment) => <span key={segment.segment_id} className={`coverage-block coverage-${segment.binary_label}`} title={`${segment.segment_id} ${seconds(segment.start_ns)} → ${seconds(segment.end_ns)}`} style={{ left: `${segment.start_ns / durationNs * 100}%`, width: `${(segment.end_ns - segment.start_ns) / durationNs * 100}%` }} />)}{durationNs > 0 && doc.exclusions.map((item) => <span key={item.exclusion_id} className="coverage-block coverage-exclude" title={`${exclusionLabels[item.reason]} ${seconds(item.start_ns)} → ${seconds(item.end_ns)}`} style={{ left: `${item.start_ns / durationNs * 100}%`, width: `${(item.end_ns - item.start_ns) / durationNs * 100}%` }} />)}{durationNs > 0 && <span className="coverage-cursor" style={{ left: `${Math.max(0, Math.min(100, currentTime * 1e9 / durationNs * 100))}%` }} />}</div>
              <div className={`coverage-summary ${uncoveredNs > 0 ? "warning-text" : "success-text"}`}>{uncoveredNs > 0 ? `未覆盖 ${(uncoveredNs / 1e9).toFixed(3)} s` : "全时间轴已覆盖"} · {fallWithoutImpactCount > 0 ? `${fallWithoutImpactCount} 个跌倒缺少撞击` : "所有跌倒均有撞击"} · {sync?.quality === "verified" ? "同步已验证" : "同步未验证"}</div>
              {coverageGaps.length > 0 && <div className="gap-list">{coverageGaps.map((gap, index) => <button key={`${gap.start}-${gap.end}`} onClick={() => { setMarks({ start: gap.start, end: gap.end }); jumpToRecordingTime(gap.start); setTaskTab("annotate"); }}>空白 {index + 1} · {seconds(gap.start)}–{seconds(gap.end)}</button>)}</div>}
              {review?.workflow.state === "completed"
                ? <div className="success-banner compact-banner">任务已完成；可在“数据”中下载当前导出，重开后才能修改。</div>
                : <div className="save-row"><button className="primary" disabled={!canMutate || selectedRecording?.data_tier !== "prod" || uncoveredNs > 0 || fallWithoutImpactCount > 0 || sync?.quality !== "verified"} onClick={finalizeAndComplete}>完成标注并生成训练 H5</button></div>}
              {selectedRecording?.data_tier !== "prod" && <p className="stage-help warning-text">测试数据允许保存和下载，但不会完成为训练数据。</p>}
              <details><summary>任务技术详情</summary><p className="stage-help">review revision {review?.revision ?? "—"} · 负责人 {review?.workflow.annotator_id ?? "无"} · 最后编辑者 {review?.workflow.last_editor_id ?? "无"} · taxonomy {doc.taxonomy_version}</p></details>
            </div>}

            {taskTab === "data" && review && <div className="panel compact-panel workflow-panel">
              <div className="panel-title">下载与录制管理</div>
              <div className="status-grid"><span>{selectedRecording?.data_tier === "prod" ? "正式数据" : "测试数据"}</span><span>校准 {status?.calibration === "verified" ? "已验证" : "未验证"}</span><span>导出 {status?.export === "exported" ? "已生成" : "未生成"}</span></div>
              <div className="download-grid"><a className="button-link" href={`/api/v1/recordings/${selected}/capture-h5/download`} download>原始 capture.h5</a><a className="button-link" href={`/api/v1/recordings/${selected}/review/download`} download>标注 review.json</a>{selectedRecording?.data_tier === "prod" && status?.export === "exported" && <a className="button-link primary" href={`/api/v1/recordings/${selected}/aligned/download`} download>训练 aligned.h5</a>}</div>
              <details className="danger-zone"><summary>删除整条录制</summary><p className="stage-help">删除会立即隐藏原始文件、预览、标注和当前导出；存储桶仍按策略保留软删除恢复窗口。</p>{!deleteArmed ? <button className="danger" onClick={() => setDeleteArmed(true)}>开始删除</button> : <><label>输入 <code>DELETE {selected}</code><input value={deleteConfirmation} onChange={(event) => setDeleteConfirmation(event.target.value)} /></label><div className="save-row"><button className="danger" disabled={deleteBusy || deleteConfirmation !== `DELETE ${selected}`} onClick={permanentlyDeleteRecording}>确认删除</button><button disabled={deleteBusy} onClick={() => { setDeleteArmed(false); setDeleteConfirmation(""); }}>取消</button></div></>}</details>
            </div>}
          </div>

          <footer className="annotation-save-bar">
            <span>{canEdit ? "结构化修改会立即保存" : editDisabledReason}</span>
            <span>{doc?.finalized ? "已定稿" : "草稿"} · {sync?.quality === "verified" ? "同步已验证" : "同步待验证"}</span>
            <button disabled={!canEdit || saveState === "saving" || saveState === "conflict"} onClick={() => saveState === "error" ? retrySaveRef.current?.() : void save(false)}>保存 / 重试（Ctrl+S）</button>
          </footer>
        </section>
      </section>}
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
        ? tr(`已创建训练快照 ${result.snapshot_id}`, `Created training snapshot ${result.snapshot_id}`)
        : tr(
          `内容没有变化，继续使用已有快照 ${result.snapshot_id}`,
          `Content is unchanged; reusing existing snapshot ${result.snapshot_id}`,
        ));
      await refresh();
    } catch (value) {
      setError((value as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const deleteSnapshot = async (snapshotId: string) => {
    const confirmation = window.prompt(tr(
      `清理后平台将不再提供 TAR；已经发布的不可变 benchmark H5 不受影响。请输入：\nDELETE ${snapshotId}`,
      `After cleanup, the platform will no longer provide the TAR. The published immutable benchmark H5 is unaffected. Enter:\nDELETE ${snapshotId}`,
    ));
    if (confirmation === null) return;
    setBusy(true);
    setError("");
    setMessage("");
    try {
      await api(`/api/v1/training-snapshots/${snapshotId}`, {
        method: "DELETE",
        body: JSON.stringify({ confirmation })
      });
      setMessage(tr(`已清理训练快照 ${snapshotId}`, `Deleted training snapshot ${snapshotId}`));
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
      <p className="stage-help">点击时冻结当前所有已完成的正式数据，同时生成逐录制 TAR、合并 cw12eu.h5，并原子更新 benchmark 的团队当前版本指针。相同内容会复用同一个不可变快照。</p>
      <div className="save-row">
        <button className="primary" disabled={busy} onClick={createSnapshot}>{busy ? "正在处理…" : "生成当前训练快照"}</button>
        <button disabled={busy} onClick={() => refresh().catch((value) => setError((value as Error).message))}>刷新列表</button>
        <span>{tr(
          `当前操作者 ${session.unikey} · 所有成员可生成和下载，管理员可清理历史快照`,
          `Current operator ${session.unikey} · All members can create and download snapshots; administrators can delete historical snapshots`,
        )}</span>
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
      <details><summary>校验信息</summary><span>TAR SHA-256 {snapshot.archive_sha256}</span>{snapshot.benchmark && <span> · HDF5 SHA-256 {snapshot.benchmark.hdf5_sha256} · current {snapshot.benchmark.current_object_key}</span>}</details>
    </div>
    <div className="save-row">
      <a className="button-link primary" href={`/api/v1/training-snapshots/${snapshot.snapshot_id}/download`} download>下载 TAR</a>
      {snapshot.benchmark && <a className="button-link" href={`/api/v1/training-snapshots/${snapshot.snapshot_id}/benchmark-h5/download`} download>下载 benchmark H5</a>}
      {session.is_admin && <button className="danger" disabled={busy} onClick={() => onDelete(snapshot.snapshot_id)}>清理快照</button>}
    </div>
  </article>;
}

function formatDatasetBytes(sizeBytes: number) {
  if (sizeBytes >= 1024 ** 3) return `${(sizeBytes / 1024 ** 3).toFixed(2)} GiB`;
  return `${(sizeBytes / 1024 ** 2).toFixed(2)} MiB`;
}

function DatasetCatalogPage() {
  const [catalog, setCatalog] = useState<DatasetCatalogDocument | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const refresh = async () => {
    setBusy(true);
    setError("");
    try {
      setCatalog(await api<DatasetCatalogDocument>("/api/v1/dataset-catalog"));
    } catch (value) {
      setError((value as Error).message);
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => { void refresh(); }, []);

  return <main>
    {error && <div className="error-banner">{error}</div>}
    <section className="panel dataset-catalog">
      <div className="dataset-catalog-heading">
        <div>
          <div className="panel-title">{tr("只读数据集目录", "Read-only dataset catalog")}</div>
          <p className="stage-help">{tr("这里展示 benchmark 当前版本和不可变历史版本。网页下载适合检查单个文件；正式训练仍推荐在 benchmark 仓库运行", "This page shows the current benchmark version and immutable historical versions. Web downloads are suitable for inspecting individual files; for formal training, run")} <code>./benchmark data pull</code> {tr("，由命令统一校验并原子激活。", "in the benchmark repository so the command can verify and atomically activate the snapshot.")}</p>
        </div>
        <button disabled={busy} onClick={refresh}>{busy ? tr("正在刷新…", "Refreshing…") : tr("刷新目录", "Refresh catalog")}</button>
      </div>
      {!catalog && !error && <span className="muted">{tr("正在读取数据集目录…", "Loading dataset catalog…")}</span>}
      {catalog?.collections.map((collection) => <DatasetCollection key={collection.kind} collection={collection} />)}
    </section>
  </main>;
}

function DatasetCollection({ collection }: { collection: DatasetCatalogCollection }) {
  const title = collection.kind === "base" ? tr("公共交叉验证数据", "Public cross-validation data") : tr("团队训练数据", "Team training data");
  return <section className="dataset-collection">
    <div className="dataset-collection-heading">
      <div>
        <h2>{title}</h2>
        <span>{collection.kind === "base" ? "cross_validation" : "training_only"}</span>
      </div>
      <span className={`dataset-availability ${collection.available ? "available" : ""}`}>{collection.available ? tr("当前版本可用", "Current version available") : tr("尚未发布", "Not published")}</span>
    </div>
    {collection.warnings.map((warning) => <div className="warning-banner compact-banner" key={warning}>{warning}</div>)}
    {collection.current && <DatasetSnapshot snapshot={collection.current} />}
    {collection.history.length > 0 && <details className="snapshot-history dataset-history">
      <summary>{tr("历史版本", "Historical versions")}（{collection.history.length}）</summary>
      {collection.history.map((snapshot) => <DatasetSnapshot key={snapshot.snapshot_id} snapshot={snapshot} />)}
    </details>}
  </section>;
}

function DatasetSnapshot({ snapshot }: { snapshot: DatasetCatalogSnapshot }) {
  const base = `/api/v1/dataset-catalog/${snapshot.kind}/${encodeURIComponent(snapshot.snapshot_id)}`;
  return <article className={`dataset-snapshot ${snapshot.current ? "current-snapshot" : ""}`}>
    <div className="dataset-snapshot-heading">
      <div>
        <strong>{snapshot.current ? tr("当前版本", "Current version") : tr("历史版本", "Historical version")} · {snapshot.snapshot_id}</strong>
        <span>{snapshot.files.length} {tr("个 H5", "H5 files")} · schema 3.1.0 · 25 Hz · {new Date(snapshot.created_at_utc).toLocaleString()}</span>
      </div>
      <a className="button-link" href={`${base}/manifest/download`} download>{tr("下载 manifest", "Download manifest")}</a>
    </div>
    <details className="dataset-checks"><summary>{tr("版本校验信息", "Version verification")}</summary><code>contract {snapshot.contract_version}</code><code>manifest SHA-256 {snapshot.manifest_sha256}</code></details>
    <div className="dataset-file-grid">
      {snapshot.files.map((file) => <DatasetFile key={file.dataset_id} snapshot={snapshot} file={file} />)}
    </div>
  </article>;
}

function DatasetFile({ snapshot, file }: { snapshot: DatasetCatalogSnapshot; file: DatasetCatalogFile }) {
  const counts = [
    `${file.sequences.toLocaleString()} ${tr("序列", "sequences")}`,
    `${file.rows.toLocaleString()} ${tr("行", "rows")}`,
    `${file.annotations.toLocaleString()} ${tr("标注", "annotations")}`,
    `${(file.events ?? 0).toLocaleString()} ${tr("事件", "events")}`,
    `${(file.segments ?? 0).toLocaleString()} ${tr("区间", "intervals")}`,
    `${(file.fall_sequences ?? 0).toLocaleString()} ${tr("跌倒序列", "fall sequences")}`,
    `${(file.participants ?? 0).toLocaleString()} ${tr("参与者", "participants")}`,
  ];
  const locations = Object.entries(file.body_locations ?? {}).map(([name, count]) => `${name} ${count}`).join(" · ") || "—";
  const supervision = Object.entries(file.supervision ?? {}).map(([name, count]) => `${name} ${count}`).join(" · ") || "—";
  const url = `/api/v1/dataset-catalog/${snapshot.kind}/${encodeURIComponent(snapshot.snapshot_id)}/${encodeURIComponent(file.dataset_id)}/download`;
  return <section className="dataset-file">
    <div className="dataset-file-heading"><strong>{file.dataset_id}</strong><span>{formatDatasetBytes(file.size_bytes)}</span></div>
    <p>{counts.join(" · ")}</p>
    <p>{tr("位置", "Locations")} {locations}</p>
    <p>{tr("监督", "Supervision")} {supervision} · {file.evaluation_role}</p>
    <details><summary>{tr("文件指纹", "File fingerprints")}</summary><code>SHA-256 {file.sha256}</code><code>logical {file.logical_content_sha256}</code></details>
    <a className="button-link primary" href={url} download>{tr("下载", "Download")} {file.filename}</a>
  </section>;
}

function CaptureLibrary({ recordings, onChanged, publishMode, cloudConfigured }: {
  recordings: Recording[];
  onChanged: () => void;
  publishMode: "disabled" | "local" | "broker" | "direct_gcs";
  cloudConfigured: boolean;
}) {
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState("");
  const [incomplete, setIncomplete] = useState<{ relative_path: string; size_bytes: number; reason: string }[]>([]);
  const [cloud, setCloud] = useState<CloudStatus | null>(null);
  const [pendingPublish, setPendingPublish] = useState<Recording | null>(null);

  const refreshCloud = async () => {
    if (!cloudConfigured) return;
    setCloud(await api<CloudStatus>("/api/v1/cloud/status"));
  };

  useEffect(() => {
    if (!cloudConfigured) return;
    refreshCloud().catch((value) => setError((value as Error).message));
    const timer = window.setInterval(() => {
      refreshCloud().catch(() => undefined);
    }, 3_000);
    return () => window.clearInterval(timer);
  }, [cloudConfigured]);

  const loginCloud = async (recording?: Recording) => {
    setError("");
    if (recording) setPendingPublish(recording);
    const popup = window.open("about:blank", "imu-google-oauth", "popup,width=560,height=720");
    try {
      const result = await api<{ authorization_url: string }>("/api/v1/cloud/oauth/start", { method: "POST" });
      if (popup) popup.location.href = result.authorization_url;
      else window.location.href = result.authorization_url;
    } catch (value) {
      popup?.close();
      if (recording) setPendingPublish(null);
      setError((value as Error).message);
    }
  };

  useEffect(() => {
    const receiveOAuthResult = (event: MessageEvent) => {
      if (event.origin !== window.location.origin || event.data !== "imu-oauth-success") return;
      refreshCloud().then(onChanged).catch((value) => setError((value as Error).message));
    };
    window.addEventListener("message", receiveOAuthResult);
    return () => window.removeEventListener("message", receiveOAuthResult);
  }, [cloudConfigured]);

  const logoutCloud = async () => {
    setError("");
    try {
      setCloud(await api<CloudStatus>("/api/v1/cloud/logout", { method: "POST" }));
    } catch (value) {
      setError((value as Error).message);
    }
  };

  useEffect(() => {
    const pending = recordings.filter((recording) => recording.index_state === "pending");
    const hasActiveJobs = recordings.some((recording) =>
      [recording.finalization_job, recording.upload_job]
        .some((job) => job && activeJobStates.has(job.state))
    );
    if (pending.length === 0 && !hasActiveJobs) return;
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
  }, [recordings.map((item) => `${item.recording_id}:${item.index_state}:${item.finalization_job?.state}:${item.finalization_job?.phase}:${item.upload_job?.state}:${item.upload_job?.phase}`).join("|")]);

  const queuePublish = async (recording: Recording) => {
    setError("");
    setMessage("");
    setBusy(recording.recording_id);
    try {
      const estimate = await api<{ estimated_bytes: number }>(`/api/v1/recordings/${recording.recording_id}/publish/estimate`);
      const gib = estimate.estimated_bytes / 1024 ** 3;
      if (!window.confirm(`将生成浏览代理并发布 H5、原始 MKV、代理 MP4 和 manifest。\n预计读取或上传约 ${gib.toFixed(2)} GiB，继续吗？`)) return;
      const result = await api<{ auth_required?: boolean }>(`/api/v1/recordings/${recording.recording_id}/publish`, { method: "POST" });
      if (result.auth_required) {
        setPendingPublish(recording);
        await loginCloud(recording);
        return;
      }
      setMessage(publishMode === "local"
        ? `已加入本机归档队列：${recording.recording_id}`
        : `已加入后台上传队列：${recording.recording_id}`);
      onChanged();
    } catch (e) {
      setError((e as Error).message);
      onChanged();
    } finally {
      setBusy("");
    }
  };

  const publish = async (recording: Recording) => {
    if (publishMode === "broker" && !cloud?.logged_in) {
      await loginCloud(recording);
      return;
    }
    await queuePublish(recording);
  };

  useEffect(() => {
    if (!cloud?.logged_in || !pendingPublish) return;
    const recording = pendingPublish;
    setPendingPublish(null);
    queuePublish(recording).catch((value) => setError((value as Error).message));
  }, [cloud?.logged_in, pendingPublish?.recording_id]);

  const uploadStateLabel = (recording: Recording) => {
    if (recording.upload_state === "stored_local") return "仅保存在本机";
    if (recording.upload_state === "auth_required") return "等待 Google 登录";
    if (["uploaded", "published"].includes(recording.upload_state)) {
      return ["broker", "direct_gcs"].includes(recording.publish_target ?? "")
        ? "已上传团队 Bucket"
        : "已归档到本机";
    }
    const labels: Record<string, string> = {
      not_requested: "尚未发布",
      queued: "等待上传",
      uploading: "正在上传",
      retry_wait: "等待自动重试",
      failed: "上传失败",
    };
    return labels[recording.upload_state] ?? `发布 ${recording.upload_state}`;
  };

  const retryFinalization = async (recording: Recording) => {
    setError("");
    setMessage("");
    setBusy(recording.recording_id);
    try {
      await api(`/api/v1/recordings/${recording.recording_id}/finalization/retry`, {
        method: "POST"
      });
      setMessage(`已加入后台收尾队列：${recording.recording_id}`);
      onChanged();
    } catch (e) {
      setError((e as Error).message);
      onChanged();
    } finally {
      setBusy("");
    }
  };

  const copyRecordingId = async (recordingId: string) => {
    try {
      await navigator.clipboard.writeText(recordingId);
      setMessage(`已复制录制 ID：${recordingId}`);
    } catch (e) {
      setError((e as Error).message);
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
      <div className="panel-title">本地录制与后台处理</div>
      <p className="stage-help">这里只负责确认采集结果并交给标注存储；同步、标注和训练快照在独立标注平台完成。</p>
      {publishMode === "local" && <div className="info-banner">本地开发模式：发布只归档到本机，不会上传团队 Bucket。</div>}
      {publishMode === "broker" && !cloudConfigured && <div className="error-banner">团队上传尚未配置，请更新桌面客户端。</div>}
      {publishMode === "broker" && cloudConfigured && <div className="save-row">
        <strong>{cloud?.logged_in ? `${tr("团队云端已登录", "Signed in to team cloud")}${cloud.email ? ` · ${cloud.email}` : ""}` : "发布到团队云端前需要登录"}</strong>
        {cloud?.logged_in
          ? <button onClick={logoutCloud}>退出云端账号</button>
          : <button className="primary" onClick={() => loginCloud()}>使用 Google 账号登录</button>}
      </div>}
      {recordings.map((recording) => <article key={recording.recording_id}>
        <div><strong>{recording.recording_id}</strong><span>{recording.collection_id} · {recording.participant_id} · {tierLabel(recording.data_tier)} · {seconds(recording.duration_ns)}</span></div>
        <div className="status-grid">
          <span>采集 {stateLabel(recording.state)}</span>
          <span>{uploadStateLabel(recording)}</span>
          {recording.index_state === "indexed" && <span>标注端已接收</span>}
          {recording.index_state === "pending" && <span>等待标注端接收</span>}
          {recording.index_state === "rejected" && <span className="warning-text">标注端拒绝</span>}
        </div>
        {recording.finalization_job && <p className={recording.finalization_job.state === "failed" ? "warning-text" : "stage-help"}>后台收尾：{jobStateLabel(recording.finalization_job)} · 尝试 {recording.finalization_job.attempts}/{recording.finalization_job.max_attempts}{recording.finalization_job.last_error ? ` · ${recording.finalization_job.last_error}` : ""}</p>}
        {recording.upload_job && <p className={recording.upload_job.state === "failed" ? "warning-text" : "stage-help"}>后台上传：{jobStateLabel(recording.upload_job)} · 尝试 {recording.upload_job.attempts}/{recording.upload_job.max_attempts}{(recording.upload_job.total_bytes ?? 0) > 0 ? ` · ${Math.min(100, 100 * (recording.upload_job.progress_bytes ?? 0) / (recording.upload_job.total_bytes ?? 1)).toFixed(0)}%` : ""}{recording.upload_job.last_error ? ` · ${recording.upload_job.last_error}` : ""}</p>}
        {recording.index_message && <p className={recording.index_state === "rejected" ? "warning-text" : "stage-help"}>{recording.index_message}</p>}
        <div className="save-row">
          {recording.h5_path?.endsWith(".partial.h5") && recording.mkv_path?.endsWith(".partial.mkv") && !activeJobStates.has(recording.finalization_job?.state ?? "") && <button className="primary" disabled={busy === recording.recording_id} onClick={() => retryFinalization(recording)}>{busy === recording.recording_id ? "正在提交…" : "重新收尾"}</button>}
          {publishMode !== "disabled" && recording.state === "ready" && !["uploaded", "published"].includes(recording.upload_state) && !activeJobStates.has(recording.upload_job?.state ?? "") && <button className="primary" disabled={busy === recording.recording_id} onClick={() => publish(recording)}>{busy === recording.recording_id ? "正在提交…" : recording.upload_job?.state === "failed" ? "重新上传" : publishMode === "local" ? "归档到本机" : recording.data_tier === "prod" ? "立即加入上传队列" : "估算并上传"}</button>}
          <button onClick={() => copyRecordingId(recording.recording_id)}>复制录制 ID</button>
          <button className="danger" disabled={Boolean(busy) || [recording.finalization_job, recording.upload_job].some((job) => job && activeJobStates.has(job.state))} onClick={() => deleteRecording(recording.recording_id)}>永久删除</button>
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
