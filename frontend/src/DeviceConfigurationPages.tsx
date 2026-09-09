import { useEffect, useMemo, useRef, useState } from "react";

type ConfigurationStatus = {
  schema_version: string;
  selected_snapshot_id: string;
  selected_snapshot_sha256: string;
  selected_content_sha256: string;
  selected_state: string;
  selected_source: string;
  current_snapshot_id: string | null;
  current_checked_at_utc?: string | null;
  update_available: boolean;
  manually_pinned: boolean;
  workspace_path: string;
  local_root: string;
  cache_root: string;
};

type SnapshotSummary = {
  snapshot_id: string;
  snapshot_sha256: string;
  content_sha256: string;
  name: string;
  description: string;
  publisher: string;
  published_at_utc: string;
  device_count: number;
  state: string;
  source?: string;
  current: boolean;
  selected?: boolean;
  review_revision?: number | null;
};

type Triple = [number, number, number];

type ConfigurationEvidence = {
  recording_id: string;
  kind: string;
  summary_zh: string;
  summary_en: string;
};

type CandidateConversion = {
  accel_counts_per_g: number | null;
  gyro_counts_per_dps: number | null;
  accel_bias_counts: Triple;
  gyro_bias_counts: Triple;
  raw_axis_order: Triple;
  axis_signs: Triple;
  evidence_status: string;
};

type ConfigurationDevice = {
  sensor_sn: string;
  hardware_asset_id: string;
  revision: number;
  supersedes_sn: string | null;
  lifecycle: "active" | "retired";
  display_name: string;
  protocol_id: string;
  expected_rate_hz: number;
  expected_rate_status: string;
  allowed_data_tiers: ("test" | "prod")[];
  identity: {
    advertised_name: string;
    public_address: string | null;
    address_type: string;
    advertised_service_uuid: string | null;
    gatt_fingerprint_sha256: string | null;
  };
  firmware: {
    version: string;
    evidence_status: string;
    artifact_sha256: string | null;
  };
  si_profile: {
    profile_id: string;
    verified: boolean;
    accel_counts_per_g: number | null;
    gyro_counts_per_dps: number | null;
    accel_bias_counts: Triple;
    gyro_bias_counts: Triple;
    raw_axis_order: Triple;
    axis_signs: Triple;
    method: string;
    evidence_sha256: string | null;
    coordinate_system: Record<string, string>;
    evidence: ConfigurationEvidence[];
  };
  audit_document: string | null;
};

type ConfigurationSubmission = {
  name: string;
  description: string;
  base_snapshot_id: string | null;
  client_build: string;
  content: { schema_version: "2.0"; devices: ConfigurationDevice[] };
};

type SnapshotDetail = {
  snapshot: ConfigurationSubmission & {
    snapshot_id: string;
    snapshot_sha256: string;
    content_sha256: string;
    publisher: string;
    published_at_utc: string;
  };
  state?: string;
  selected?: boolean;
  current?: boolean;
  review?: {
    state: string;
    revision: number;
    updated_by: string;
    updated_at_utc: string;
  };
  review_generation?: number;
  events?: Record<string, unknown>[];
  linked_recordings?: string[];
};

type RuntimeConfiguration = {
  device_registry_path?: string;
  activity_taxonomy_path?: string;
  configuration_root?: string;
  configuration_cache_root?: string;
  editable?: boolean;
  restart_required_for_changes?: boolean;
};

type SettingsImuProfile = {
  sensor_sn: string;
  display_name: string;
  candidate_conversion: CandidateConversion | null;
  candidate_conversion_source: "registry" | "local_override" | null;
};

type BleCandidate = {
  local_device_id: string;
  name: string | null;
  address: string;
  rssi: number;
  service_uuids: string[];
  matched_sensor_sns: string[];
  registration_state: "registered" | "unregistered";
};

type BleScanSummary = {
  requested: boolean;
  adapter_state: string;
  target_name: string | null;
  target_address: string | null;
  target_found: boolean;
  elapsed_ms: number;
  error: unknown;
};

type CaptureSettingsProps = {
  interactionBlocked: boolean;
  runtimeConfiguration?: RuntimeConfiguration | null;
  imuProfiles: SettingsImuProfile[];
  bleCandidates: BleCandidate[];
  bleScan: BleScanSummary | null;
  onScanBle: () => Promise<unknown> | void;
  onConfigurationChanged: () => Promise<unknown> | void;
  onOpenPublishing: () => void;
};

type CloudStatus = {
  configured: boolean;
  logged_in: boolean;
  email: string | null;
  broker_url: string | null;
};

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = payload?.detail;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail ?? payload));
  }
  return payload as T;
}

function shortHash(value?: string | null) {
  return value ? `${value.slice(0, 12)}…` : "—";
}

function dateLabel(value?: string | null) {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

function stateLabel(state: string) {
  return ({
    local: "本机快照",
    candidate: "待审批",
    approved: "已批准",
    revoked: "已撤销",
    verified: "SI 已验证",
    unverified: "SI 未验证",
    active: "使用中",
    retired: "已退役",
    unsupported: "协议不受支持",
    loading: "读取中",
    unknown: "状态未知",
  } as Record<string, string>)[state] ?? state;
}

function StatusPill({ state }: { state: string }) {
  return <span className={`config-pill config-pill-${state}`}>{stateLabel(state)}</span>;
}

function nullable(value: string) {
  const trimmed = value.trim();
  return trimmed === "" ? null : trimmed;
}

function nullableNumber(value: string) {
  if (value.trim() === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function TripleFields({ label, value, onChange, integer = false, choices }: {
  label: string;
  value: Triple;
  onChange: (value: Triple) => void;
  integer?: boolean;
  choices?: number[];
}) {
  return <fieldset className="triple-fields"><legend>{label}</legend>{value.map((item, index) => <label key={index}>{["X", "Y", "Z"][index]}{choices
    ? <select value={item} onChange={(event) => {
      const next = [...value] as Triple;
      next[index] = Number(event.target.value);
      onChange(next);
    }}>{choices.map((choice) => <option key={choice} value={choice}>{choice}</option>)}</select>
    : <input type="number" step={integer ? 1 : "any"} value={item} onChange={(event) => {
      const next = [...value] as Triple;
      next[index] = Number(event.target.value);
      onChange(next);
    }} />}</label>)}</fieldset>;
}

function newUnverifiedSi(): ConfigurationDevice["si_profile"] {
  return {
    profile_id: `si-${"0".repeat(24)}`,
    verified: false,
    accel_counts_per_g: null,
    gyro_counts_per_dps: null,
    accel_bias_counts: [0, 0, 0],
    gyro_bias_counts: [0, 0, 0],
    raw_axis_order: [0, 1, 2],
    axis_signs: [1, 1, 1],
    method: "unverified_new_revision",
    evidence_sha256: null,
    coordinate_system: {},
    evidence: [],
  };
}

export function CaptureSettingsPage({
  interactionBlocked,
  runtimeConfiguration,
  imuProfiles,
  bleCandidates,
  bleScan,
  onScanBle,
  onConfigurationChanged,
  onOpenPublishing,
}: CaptureSettingsProps) {
  const [tab, setTab] = useState<"snapshots" | "devices" | "runtime">(() => {
    const section = new URLSearchParams(location.search).get("section");
    return section === "devices" || section === "runtime" ? section : "snapshots";
  });
  const [status, setStatus] = useState<ConfigurationStatus | null>(null);
  const [snapshots, setSnapshots] = useState<SnapshotSummary[]>([]);
  const [detail, setDetail] = useState<SnapshotDetail | null>(null);
  const [workspace, setWorkspace] = useState<ConfigurationSubmission | null>(null);
  const [workspaceText, setWorkspaceText] = useState("");
  const [workspaceState, setWorkspaceState] = useState<"loading" | "saved" | "dirty" | "invalid" | "saving">("loading");
  const [selectedDeviceSn, setSelectedDeviceSn] = useState("");
  const [candidateDraft, setCandidateDraft] = useState<CandidateConversion | null>(null);
  const [candidateDirty, setCandidateDirty] = useState(false);
  const [capabilities, setCapabilities] = useState<{ protocol_ids: string[]; configuration_schema_versions: string[] } | null>(null);
  const [cloud, setCloud] = useState<CloudStatus | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const loadedText = useRef("");
  const latestWorkspaceText = useRef("");

  const refresh = async () => {
    const [nextStatus, nextSnapshots] = await Promise.all([
      requestJson<ConfigurationStatus>("/api/v1/configuration/status"),
      requestJson<SnapshotSummary[]>("/api/v1/configuration/snapshots"),
    ]);
    setStatus(nextStatus);
    setSnapshots(nextSnapshots);
    return nextStatus;
  };

  useEffect(() => {
    Promise.all([
      refresh().then((nextStatus) => requestJson<SnapshotDetail>(
        `/api/v1/configuration/snapshots/${encodeURIComponent(nextStatus.selected_snapshot_id)}`,
      ).then(setDetail)),
      requestJson<ConfigurationSubmission>("/api/v1/configuration/workspace").then((value) => {
        const text = JSON.stringify(value, null, 2);
        setWorkspace(value);
        setWorkspaceText(text);
        loadedText.current = text;
        latestWorkspaceText.current = text;
        setWorkspaceState("saved");
        const requestedSn = new URLSearchParams(location.search).get("device");
        setSelectedDeviceSn(
          value.content.devices.find((item) => item.sensor_sn === requestedSn)?.sensor_sn
            ?? value.content.devices[0]?.sensor_sn
            ?? "",
        );
      }),
      requestJson<{ protocol_ids: string[]; configuration_schema_versions: string[] }>("/api/v1/device-capabilities").then(setCapabilities),
      requestJson<CloudStatus>("/api/v1/cloud/status").then(setCloud),
    ]).catch((caught) => setError((caught as Error).message));
  }, []);

  useEffect(() => {
    if (!workspaceText || workspaceText === loadedText.current) return;
    let parsed: ConfigurationSubmission;
    try {
      parsed = JSON.parse(workspaceText) as ConfigurationSubmission;
      setWorkspaceState("dirty");
    } catch {
      setWorkspaceState("invalid");
      return;
    }
    const timer = window.setTimeout(async () => {
      const submittedText = workspaceText;
      setWorkspaceState("saving");
      try {
        const saved = await requestJson<ConfigurationSubmission>("/api/v1/configuration/workspace", {
          method: "PUT",
          body: JSON.stringify(parsed),
        });
        const canonical = JSON.stringify(saved, null, 2);
        loadedText.current = canonical;
        if (latestWorkspaceText.current === submittedText) {
          latestWorkspaceText.current = canonical;
          setWorkspace(saved);
          setWorkspaceText(canonical);
          setWorkspaceState("saved");
          setError("");
        }
      } catch (caught) {
        if (latestWorkspaceText.current === submittedText) {
          setWorkspaceState("invalid");
          setError((caught as Error).message);
        }
      }
    }, 900);
    return () => window.clearTimeout(timer);
  }, [workspaceText]);

  const replaceWorkspace = (next: ConfigurationSubmission) => {
    const text = JSON.stringify(next, null, 2);
    latestWorkspaceText.current = text;
    setWorkspace(next);
    setWorkspaceText(text);
    setWorkspaceState("dirty");
  };

  const patchWorkspace = (change: (next: ConfigurationSubmission) => void) => {
    if (!workspace) return;
    const next = JSON.parse(JSON.stringify(workspace)) as ConfigurationSubmission;
    change(next);
    replaceWorkspace(next);
  };

  const patchDevice = (change: (next: ConfigurationDevice) => void) => {
    patchWorkspace((next) => {
      const device = next.content.devices.find((item) => item.sensor_sn === selectedDeviceSn)
        ?? next.content.devices[0];
      if (device) change(device);
    });
  };

  const run = async <T,>(name: string, operation: () => Promise<T>, success: string | ((value: T) => string)) => {
    setBusy(name);
    setError("");
    setMessage("");
    try {
      const value = await operation();
      const nextStatus = await refresh();
      const detailId = ["select", "reset", "refresh"].includes(name)
        ? nextStatus.selected_snapshot_id
        : detail?.snapshot.snapshot_id;
      if (detailId) setDetail(await requestJson<SnapshotDetail>(`/api/v1/configuration/snapshots/${encodeURIComponent(detailId)}`));
      await onConfigurationChanged();
      setMessage(typeof success === "function" ? success(value) : success);
      return value;
    } catch (caught) {
      setError((caught as Error).message);
      return undefined;
    } finally {
      setBusy("");
    }
  };

  const openSnapshot = async (snapshotId: string) => {
    setError("");
    try {
      setDetail(await requestJson<SnapshotDetail>(`/api/v1/configuration/snapshots/${encodeURIComponent(snapshotId)}`));
    } catch (caught) {
      setError((caught as Error).message);
    }
  };

  const copySnapshotToWorkspace = () => {
    if (!detail) return;
    const draft: ConfigurationSubmission = {
      name: `${detail.snapshot.name} · 工作副本`,
      description: detail.snapshot.description,
      base_snapshot_id: detail.snapshot.snapshot_id,
      client_build: workspace?.client_build ?? detail.snapshot.client_build,
      content: detail.snapshot.content,
    };
    replaceWorkspace(draft);
    setSelectedDeviceSn(draft.content.devices[0]?.sensor_sn ?? "");
    setMessage("已创建可编辑工作区副本；原快照保持不变，字段校验通过后自动保存。");
  };

  const reserveAsset = async () => {
    const reservation = await run(
      "asset",
      () => requestJson<{ sensor_sn: string; hardware_asset_id: string; revision: number }>("/api/v1/configuration/identities/assets", { method: "POST" }),
      (value) => `已永久保留 ${value.sensor_sn} 并加入工作区。请完成设备字段后再保存快照。`,
    );
    if (!reservation) return;
    const device: ConfigurationDevice = {
      sensor_sn: reservation.sensor_sn,
      hardware_asset_id: reservation.hardware_asset_id,
      revision: reservation.revision,
      supersedes_sn: null,
      lifecycle: "active",
      display_name: "待配置的新 IMU",
      identity: {
        advertised_name: "待填写",
        public_address: null,
        address_type: "public",
        advertised_service_uuid: null,
        gatt_fingerprint_sha256: null,
      },
      firmware: { version: "unknown", evidence_status: "pending_inspection", artifact_sha256: null },
      protocol_id: "pending_protocol_selection",
      expected_rate_hz: 1,
      expected_rate_status: "pending_measurement",
      allowed_data_tiers: ["test"],
      si_profile: newUnverifiedSi(),
      audit_document: null,
    };
    patchWorkspace((next) => next.content.devices.push(device));
    setSelectedDeviceSn(device.sensor_sn);
  };

  const reserveRevision = async () => {
    const source = workspace?.content.devices.find((item) => item.sensor_sn === selectedDeviceSn);
    if (!source) return;
    const latestRevision = Math.max(...(workspace?.content.devices
      .filter((item) => item.hardware_asset_id === source.hardware_asset_id)
      .map((item) => item.revision) ?? [source.revision]));
    if (source.revision !== latestRevision) {
      setError(`请先选择 ${source.hardware_asset_id} 的最新 revision；旧 revision 不能直接跳级。`);
      return;
    }
    const reservation = await run(
      "revision",
      () => requestJson<{ sensor_sn: string; hardware_asset_id: string; revision: number; supersedes_sn: string }>("/api/v1/configuration/identities/revisions", { method: "POST", body: JSON.stringify({ hardware_asset_id: source.hardware_asset_id }) }),
      (value) => `已永久保留 ${value.sensor_sn} 并建立新 firmware revision 草稿。`,
    );
    if (!reservation) return;
    const nextDevice = JSON.parse(JSON.stringify(source)) as ConfigurationDevice;
    nextDevice.sensor_sn = reservation.sensor_sn;
    nextDevice.hardware_asset_id = reservation.hardware_asset_id;
    nextDevice.revision = reservation.revision;
    nextDevice.supersedes_sn = reservation.supersedes_sn;
    nextDevice.display_name = `${source.display_name} · R${String(reservation.revision).padStart(2, "0")}`;
    nextDevice.firmware = { version: "unknown", evidence_status: "pending_new_firmware_inspection", artifact_sha256: null };
    nextDevice.expected_rate_status = "pending_new_firmware_measurement";
    nextDevice.allowed_data_tiers = ["test"];
    nextDevice.si_profile = newUnverifiedSi();
    patchWorkspace((next) => {
      const previous = next.content.devices.find((item) => item.sensor_sn === source.sensor_sn);
      if (previous) previous.lifecycle = "retired";
      next.content.devices.push(nextDevice);
    });
    setSelectedDeviceSn(nextDevice.sensor_sn);
  };

  const devices = workspace?.content.devices ?? [];
  const selectedDevice = devices.find((item) => item.sensor_sn === selectedDeviceSn) ?? devices[0];
  const selectedRuntimeProfile = imuProfiles.find((item) => item.sensor_sn === selectedDevice?.sensor_sn) ?? null;
  const selectedIsLatestRevision = Boolean(selectedDevice && selectedDevice.revision === Math.max(
    ...devices.filter((item) => item.hardware_asset_id === selectedDevice.hardware_asset_id).map((item) => item.revision),
  ));
  const remoteUnavailable = cloud === null || !cloud.configured || !cloud.logged_in;
  const remoteReason = cloud === null
    ? "正在读取团队 broker 与登录状态。"
    : !cloud.configured
    ? "当前安装尚未配置团队 broker 与桌面 OAuth Client ID。SN 只能由中心分配，不能生成本机临时号。"
    : !cloud.logged_in
      ? "团队 broker 已配置，但尚未完成 Google 登录。登录后才可永久保留 SN。"
      : "";

  useEffect(() => {
    const profile = imuProfiles.find((item) => item.sensor_sn === selectedDeviceSn);
    setCandidateDraft(profile?.candidate_conversion ? JSON.parse(JSON.stringify(profile.candidate_conversion)) : null);
    setCandidateDirty(false);
  }, [selectedDeviceSn, selectedRuntimeProfile?.candidate_conversion_source, JSON.stringify(selectedRuntimeProfile?.candidate_conversion ?? null)]);

  const saveCandidate = async () => {
    if (!selectedDevice || !candidateDraft) return;
    await run(
      "candidate",
      () => requestJson(`/api/v1/devices/imu-candidates/${encodeURIComponent(selectedDevice.sensor_sn)}`, { method: "PUT", body: JSON.stringify(candidateDraft) }),
      "本机候选 SI 已保存；它只用于屏幕诊断与 test 证据标记，不具备正式 SI 权限。",
    );
    setCandidateDirty(false);
  };

  const clearCandidate = async () => {
    if (!selectedDevice) return;
    await run(
      "clear-candidate",
      () => requestJson(`/api/v1/devices/imu-candidates/${encodeURIComponent(selectedDevice.sensor_sn)}`, { method: "DELETE" }),
      "已清除本机候选 SI。",
    );
    setCandidateDraft(null);
    setCandidateDirty(false);
  };

  const promoteCandidate = () => {
    if (!candidateDraft || !selectedDevice) return;
    patchDevice((device) => {
      device.si_profile = {
        ...device.si_profile,
        verified: false,
        accel_counts_per_g: candidateDraft.accel_counts_per_g,
        gyro_counts_per_dps: candidateDraft.gyro_counts_per_dps,
        accel_bias_counts: candidateDraft.accel_bias_counts,
        gyro_bias_counts: candidateDraft.gyro_bias_counts,
        raw_axis_order: candidateDraft.raw_axis_order,
        axis_signs: candidateDraft.axis_signs,
        method: `local_candidate:${candidateDraft.evidence_status}`,
        evidence_sha256: null,
        evidence: [],
      };
      device.allowed_data_tiers = ["test"];
    });
    setMessage("候选系数已复制到工作区 SI，并被强制标记为未验证、仅允许 test。保存或发布快照不会自动批准它。");
  };

  const scanBle = async () => {
    setBusy("ble-scan");
    setError("");
    try {
      await onScanBle();
    } catch (caught) {
      setError((caught as Error).message);
    } finally {
      setBusy("");
    }
  };

  return <main className="settings-page">
    <section className="settings-heading">
      <div>
        <span className="eyebrow">DEVICE CONFIGURATION · SCHEMA 2.0</span>
        <h2>设备与设置</h2>
        <p>低频配置与诊断集中在这里；采集页只保留当场必须操作的内容。</p>
      </div>
      <div className="config-summary-card">
        <span>采集正在使用</span>
        <strong>{status?.selected_snapshot_id ?? "正在读取…"}</strong>
        <div>
          <StatusPill state={status?.selected_state ?? "loading"} />
          {status?.manually_pinned && <span className="config-pill">人工固定</span>}
        </div>
        <small>{cloud?.logged_in
          ? `团队身份：${cloud.email ?? "已登录"}`
          : cloud?.configured ? "团队身份：未登录" : "团队 broker：未配置"}</small>
      </div>
    </section>

    {error && <div className="error-banner">{error}</div>}
    {message && <div className="success-banner">{message}</div>}
    {status?.update_available && <div className="warning-banner">
      团队 Current 已变化，但本机仍保持人工选择。确认后可“恢复跟随 Current”，不会在采集中途自动切换。
    </div>}
    {cloud?.configured && !cloud.logged_in && <div className="warning-banner">
      远端配置操作需要团队登录。本机工作区和本机快照仍可使用。 <button onClick={onOpenPublishing}>前往“记录与发布”登录</button>
    </div>}
    {cloud && !cloud.configured && <div className="warning-banner">
      当前安装未配置团队 broker；本机工作区、候选 SI 和本机快照仍可使用，中心 SN 与团队候选不可用。
    </div>}

    <nav className="settings-tabs" aria-label="设备设置区域">
      <button className={tab === "snapshots" ? "active" : ""} onClick={() => setTab("snapshots")}>配置快照</button>
      <button className={tab === "devices" ? "active" : ""} onClick={() => setTab("devices")}>设备档案与 SI</button>
      <button className={tab === "runtime" ? "active" : ""} onClick={() => setTab("runtime")}>诊断与运行环境</button>
    </nav>

    {tab === "snapshots" && <section className="settings-split">
      <div className="panel config-list-panel">
        <div className="panel-heading-row">
          <div><div className="panel-title">本机可用快照</div><p>快照不可变；选择仅影响之后的新预览与录制。</p></div>
          <button
            disabled={Boolean(busy) || remoteUnavailable}
            title={remoteUnavailable ? remoteReason : ""}
            onClick={() => run("refresh", () => requestJson("/api/v1/configuration/refresh", { method: "POST" }), "已刷新团队配置缓存")}
          >{busy === "refresh" ? "刷新中…" : "刷新团队配置"}</button>
        </div>
        <div className="config-snapshot-list">
          {snapshots.length === 0 && <div className="placeholder compact">本机尚无可用快照</div>}
          {snapshots.map((item) => <button
            key={item.snapshot_id}
            className={detail?.snapshot.snapshot_id === item.snapshot_id ? "selected" : ""}
            onClick={() => openSnapshot(item.snapshot_id)}
          >
            <span><strong>{item.name}</strong><StatusPill state={item.state} /></span>
            <code>{item.snapshot_id}</code>
            <small>{item.device_count} 台设备 · {dateLabel(item.published_at_utc)}</small>
            <span className="config-flags">{item.current && <b>Current</b>}{item.selected && <b>正在使用</b>}</span>
          </button>)}
        </div>
      </div>

      <div className="panel config-detail-panel">
        <div className="panel-title">{detail ? detail.snapshot.name : "快照详情"}</div>
        {!detail ? <div className="placeholder compact">选择左侧快照查看哈希、设备与切换条件</div> : <>
          <dl className="config-definition-list">
            <div><dt>Snapshot ID</dt><dd><code>{detail.snapshot.snapshot_id}</code></dd></div>
            <div><dt>状态</dt><dd><StatusPill state={detail.state ?? detail.review?.state ?? "unknown"} /></dd></div>
            <div><dt>Snapshot SHA-256</dt><dd title={detail.snapshot.snapshot_sha256}>{shortHash(detail.snapshot.snapshot_sha256)}</dd></div>
            <div><dt>内容 SHA-256</dt><dd title={detail.snapshot.content_sha256}>{shortHash(detail.snapshot.content_sha256)}</dd></div>
            <div><dt>设备</dt><dd>{detail.snapshot.content.devices.length}</dd></div>
            <div><dt>发布者 / 时间</dt><dd>{detail.snapshot.publisher} · {dateLabel(detail.snapshot.published_at_utc)}</dd></div>
          </dl>
          <p>{detail.snapshot.description || "无说明"}</p>
          <div className="save-row">
            <button
              disabled={interactionBlocked || Boolean(busy) || detail.state === "revoked" || detail.selected}
              onClick={() => run("select", () => requestJson("/api/v1/configuration/select", {
                method: "POST",
                body: JSON.stringify({ snapshot_id: detail.snapshot.snapshot_id }),
              }), `已选择 ${detail.snapshot.snapshot_id}`)}
            >{busy === "select" ? "切换中…" : detail.selected ? "正在使用" : "用于之后的采集"}</button>
            <button
              disabled={interactionBlocked || Boolean(busy) || !status?.manually_pinned}
              onClick={() => run("reset", () => requestJson("/api/v1/configuration/reset-current", { method: "POST" }), "已恢复跟随团队 Current")}
            >恢复跟随 Current</button>
            <button disabled={Boolean(busy)} onClick={copySnapshotToWorkspace}>以此快照创建工作区副本</button>
          </div>
          {detail.state !== "approved" && <div className="warning-banner">
            该快照不具备正式采集权限；本机快照和待审批快照仅可用于 test，revoked 快照不可新选用。
          </div>}
        </>}
      </div>

      <div className="panel config-workspace-panel">
        <div className="panel-heading-row">
          <div><div className="panel-title">配置工作区</div><p>日常配置使用表单；字段校验通过后自动保存。生成快照后内容才冻结。</p></div>
          <span className={`workspace-state workspace-${workspaceState}`}>{({
            loading: "读取中",
            saved: "已自动保存",
            dirty: "等待自动保存",
            invalid: "字段或高级 JSON 错误",
            saving: "保存中",
          })[workspaceState]}</span>
        </div>
        <div className="config-form-grid">
          <label>快照名称<input
            value={workspace?.name ?? ""}
            maxLength={120}
            onChange={(event) => patchWorkspace((next) => { next.name = event.target.value; })}
          /></label>
          <label>基于 Snapshot<input value={workspace?.base_snapshot_id ?? ""} readOnly title="由“以此快照创建工作区副本”确定" /></label>
          <label className="wide">变更说明<textarea
            value={workspace?.description ?? ""}
            maxLength={2000}
            onChange={(event) => patchWorkspace((next) => { next.description = event.target.value; })}
          /></label>
        </div>
        <details className="technical-details advanced-json">
          <summary>高级：完整 Snapshot JSON</summary>
          <div className="advanced-json-body">
            <p>仅用于批量迁移或排障。直接修改仍经过同一套 schema 校验；无效内容不会覆盖上一次有效工作区。</p>
            <textarea
              className="config-json-editor"
              spellCheck={false}
              value={workspaceText}
              onChange={(event) => {
                latestWorkspaceText.current = event.target.value;
                setWorkspaceText(event.target.value);
              }}
            />
          </div>
        </details>
        <div className="save-row">
          <button
            disabled={Boolean(busy) || workspaceState !== "saved"}
            onClick={() => workspace && run("local", () => requestJson("/api/v1/configuration/local-snapshots", {
              method: "POST",
              body: JSON.stringify(workspace),
            }), "已保存不可变的本机快照")}
          >{busy === "local" ? "保存中…" : "保存本机快照"}</button>
          <button
            className="primary"
            disabled={Boolean(busy) || workspaceState !== "saved" || remoteUnavailable}
            title={remoteUnavailable ? remoteReason : ""}
            onClick={() => workspace && run("publish", () => requestJson("/api/v1/configuration/publish", {
              method: "POST",
              body: JSON.stringify(workspace),
            }), "已提交团队候选，等待管理员审批")}
          >{busy === "publish" ? "提交中…" : "发布为团队候选"}</button>
        </div>
        {remoteUnavailable && <p className="disabled-reason">
          团队候选当前不可用：{remoteReason} {cloud?.configured
            ? <button onClick={onOpenPublishing}>前往“记录与发布”登录</button>
            : <button onClick={() => setTab("runtime")}>查看配置位置</button>}
        </p>}
      </div>
    </section>}

    {tab === "devices" && <section className="settings-split device-editor-layout">
      <div className="panel config-list-panel">
        <div className="panel-title">工作区设备（{devices.length}）</div>
        <div className="config-snapshot-list">
          {devices.map((item) => <button
            key={item.sensor_sn}
            className={selectedDevice?.sensor_sn === item.sensor_sn ? "selected" : ""}
            onClick={() => setSelectedDeviceSn(item.sensor_sn)}
          >
            <span><strong>{item.sensor_sn}</strong><StatusPill state={item.lifecycle} /></span>
            <small>{item.display_name}</small><code>{item.protocol_id}</code>
          </button>)}
        </div>
        <div className="save-row vertical">
          <button disabled={Boolean(busy) || remoteUnavailable} title={remoteUnavailable ? remoteReason : ""} onClick={reserveAsset}>为新物理设备保留 SN</button>
          <button disabled={Boolean(busy) || remoteUnavailable || !selectedDevice || !selectedIsLatestRevision} title={remoteUnavailable ? remoteReason : !selectedIsLatestRevision ? "只能从该物理资产的最新 revision 继续分配" : ""} onClick={reserveRevision}>为所选设备保留新 revision</button>
        </div>
        {remoteUnavailable && <div className="disabled-reason">
          <strong>为什么按钮不可用？</strong><span>{remoteReason}</span>
          {cloud?.configured
            ? <button onClick={onOpenPublishing}>前往“记录与发布”登录</button>
            : <button onClick={() => setTab("runtime")}>查看 broker 配置位置</button>}
        </div>}
      </div>

      <div className="panel config-detail-panel device-form-panel">
        <div className="panel-heading-row">
          <div><div className="panel-title">设备档案</div><p>固定 SN、资产号和 revision 由中心生成，只读；其余字段直接编辑工作区。</p></div>
          {selectedDevice && <StatusPill state={selectedDevice.lifecycle} />}
        </div>
        {!selectedDevice ? <div className="placeholder compact">工作区内没有设备</div> : <>
          <section className="form-section">
            <h3>身份与 BLE</h3>
            <div className="config-form-grid">
              <label>固定 SN<input value={selectedDevice.sensor_sn} readOnly /></label>
              <label>物理资产 / revision<input value={`${selectedDevice.hardware_asset_id} / R${String(selectedDevice.revision).padStart(2, "0")}`} readOnly /></label>
              <label>显示名称<input value={selectedDevice.display_name} onChange={(event) => patchDevice((next) => { next.display_name = event.target.value; })} /></label>
              <label>状态<select value={selectedDevice.lifecycle} onChange={(event) => patchDevice((next) => { next.lifecycle = event.target.value as "active" | "retired"; })}><option value="active">使用中</option><option value="retired">已退役</option></select></label>
              <label>广播名<input value={selectedDevice.identity.advertised_name} onChange={(event) => patchDevice((next) => { next.identity.advertised_name = event.target.value; })} /></label>
              <label>公共地址<input value={selectedDevice.identity.public_address ?? ""} placeholder="留空表示不固定" onChange={(event) => patchDevice((next) => { next.identity.public_address = nullable(event.target.value); })} /></label>
              <label>地址类型<select value={selectedDevice.identity.address_type} onChange={(event) => patchDevice((next) => { next.identity.address_type = event.target.value; })}><option value="public">public</option><option value="random">random</option><option value="unknown">unknown</option></select></label>
              <label>广播 Service UUID<input value={selectedDevice.identity.advertised_service_uuid ?? ""} placeholder="可留空" onChange={(event) => patchDevice((next) => { next.identity.advertised_service_uuid = nullable(event.target.value); })} /></label>
              <label className="wide">GATT 指纹 SHA-256<input value={selectedDevice.identity.gatt_fingerprint_sha256 ?? ""} placeholder="64 位小写十六进制；未测量可留空" onChange={(event) => patchDevice((next) => { next.identity.gatt_fingerprint_sha256 = nullable(event.target.value); })} /></label>
            </div>
          </section>

          <section className="form-section">
            <h3>固件、协议与权限</h3>
            <div className="config-form-grid">
              <label>固件版本<input value={selectedDevice.firmware.version} onChange={(event) => patchDevice((next) => { next.firmware.version = event.target.value; })} /></label>
              <label>固件证据状态<input value={selectedDevice.firmware.evidence_status} onChange={(event) => patchDevice((next) => { next.firmware.evidence_status = event.target.value; })} /></label>
              <label className="wide">固件制品 SHA-256<input value={selectedDevice.firmware.artifact_sha256 ?? ""} placeholder="未取得固件证据可留空" onChange={(event) => patchDevice((next) => { next.firmware.artifact_sha256 = nullable(event.target.value); })} /></label>
              <label>协议<select value={selectedDevice.protocol_id} onChange={(event) => patchDevice((next) => { next.protocol_id = event.target.value; })}>
                <option value="pending_protocol_selection">待确认（仅 test，运行时禁用）</option>
                {capabilities?.protocol_ids.map((protocol) => <option key={protocol} value={protocol}>{protocol}</option>)}
                {capabilities && !capabilities.protocol_ids.includes(selectedDevice.protocol_id) && selectedDevice.protocol_id !== "pending_protocol_selection" && <option value={selectedDevice.protocol_id}>{selectedDevice.protocol_id}（当前不支持）</option>}
              </select></label>
              <label>预期采样率（Hz）<input type="number" min="0.001" max="1000" step="any" value={selectedDevice.expected_rate_hz} onChange={(event) => patchDevice((next) => { next.expected_rate_hz = Number(event.target.value); })} /></label>
              <label className="wide">采样率证据状态<input value={selectedDevice.expected_rate_status} onChange={(event) => patchDevice((next) => { next.expected_rate_status = event.target.value; })} /></label>
              <label className="checkbox-label"><input type="checkbox" checked={selectedDevice.allowed_data_tiers.includes("test")} onChange={(event) => patchDevice((next) => { next.allowed_data_tiers = event.target.checked ? Array.from(new Set([...next.allowed_data_tiers, "test"])) : next.allowed_data_tiers.filter((tier) => tier !== "test"); })} />允许 test</label>
              <label className="checkbox-label"><input type="checkbox" checked={selectedDevice.allowed_data_tiers.includes("prod")} disabled={!selectedDevice.si_profile.verified} title={!selectedDevice.si_profile.verified ? "必须先完成并验证 SI，才能允许 prod" : ""} onChange={(event) => patchDevice((next) => { next.allowed_data_tiers = event.target.checked ? Array.from(new Set([...next.allowed_data_tiers, "prod"])) : next.allowed_data_tiers.filter((tier) => tier !== "prod"); })} />允许 prod</label>
              <label className="wide">审计文档路径<input value={selectedDevice.audit_document ?? ""} placeholder="例如 ../docs/device-audits/…" onChange={(event) => patchDevice((next) => { next.audit_document = nullable(event.target.value); })} /></label>
            </div>
          </section>

          <section className="form-section si-profile-card">
            <div><span>派生 SI Profile ID（自动计算）</span><strong>{selectedDevice.si_profile.profile_id}</strong></div>
            <StatusPill state={selectedDevice.si_profile.verified ? "verified" : "unverified"} />
            <div className="config-form-grid">
              <label>加速度 counts/g<input type="number" min="0" step="any" value={selectedDevice.si_profile.accel_counts_per_g ?? ""} onChange={(event) => patchDevice((next) => { next.si_profile.accel_counts_per_g = nullableNumber(event.target.value); })} /></label>
              <label>角速度 counts/(°/s)<input type="number" min="0" step="any" value={selectedDevice.si_profile.gyro_counts_per_dps ?? ""} onChange={(event) => patchDevice((next) => { next.si_profile.gyro_counts_per_dps = nullableNumber(event.target.value); })} /></label>
              <label className="wide">校准方法<input value={selectedDevice.si_profile.method} onChange={(event) => patchDevice((next) => { next.si_profile.method = event.target.value; })} /></label>
              <label className="wide">证据 SHA-256<input value={selectedDevice.si_profile.evidence_sha256 ?? ""} placeholder="验证后必须填写 64 位小写十六进制" onChange={(event) => patchDevice((next) => { next.si_profile.evidence_sha256 = nullable(event.target.value); })} /></label>
              <label className="checkbox-label wide"><input
                type="checkbox"
                checked={selectedDevice.si_profile.verified}
                disabled={!selectedDevice.si_profile.accel_counts_per_g || !selectedDevice.si_profile.gyro_counts_per_dps || !selectedDevice.si_profile.evidence_sha256}
                title="只有两个尺度和证据 SHA 齐全时才可验证"
                onChange={(event) => patchDevice((next) => {
                  next.si_profile.verified = event.target.checked;
                  if (!event.target.checked) next.allowed_data_tiers = next.allowed_data_tiers.filter((tier) => tier !== "prod");
                })}
              />标记为已经验证（不会自动授予 prod；需另行勾选）</label>
            </div>
            <div className="triple-grid">
              <TripleFields label="加速度 bias（raw counts）" value={selectedDevice.si_profile.accel_bias_counts} onChange={(value) => patchDevice((next) => { next.si_profile.accel_bias_counts = value; })} />
              <TripleFields label="角速度 bias（raw counts）" value={selectedDevice.si_profile.gyro_bias_counts} onChange={(value) => patchDevice((next) => { next.si_profile.gyro_bias_counts = value; })} />
              <TripleFields label="原始轴顺序（0/1/2）" value={selectedDevice.si_profile.raw_axis_order} choices={[0, 1, 2]} onChange={(value) => patchDevice((next) => { next.si_profile.raw_axis_order = value; })} />
              <TripleFields label="轴方向（仅 -1/1）" value={selectedDevice.si_profile.axis_signs} choices={[-1, 1]} onChange={(value) => patchDevice((next) => { next.si_profile.axis_signs = value; })} />
            </div>
            <details className="nested-details">
              <summary>坐标系与证据摘要（{selectedDevice.si_profile.evidence.length} 条）</summary>
              <p className="stage-help">常用坐标方向可直接编辑；逐条证据的完整内容可在“高级 JSON”中复核，避免在日常表单中误删长证据链。</p>
              <div className="config-form-grid">
                {["handedness", "x_positive_zh", "x_positive_en", "y_positive_zh", "y_positive_en", "z_positive_zh", "z_positive_en"].map((key) => <label className={key === "handedness" ? "" : "wide"} key={key}>{key}<input value={selectedDevice.si_profile.coordinate_system[key] ?? ""} onChange={(event) => patchDevice((next) => {
                  if (event.target.value) next.si_profile.coordinate_system[key] = event.target.value;
                  else delete next.si_profile.coordinate_system[key];
                })} /></label>)}
              </div>
              <div className="evidence-summary-list">
                {selectedDevice.si_profile.evidence.map((item, index) => <article key={`${item.recording_id}-${index}`}><code>{item.recording_id}</code><span>{item.kind}</span><small>{item.summary_zh || item.summary_en || "无摘要"}</small></article>)}
              </div>
            </details>
          </section>

          <section className="form-section candidate-editor">
            <div className="panel-heading-row">
              <div><h3>本机候选 SI</h3><p>候选值用于屏幕诊断；录制 H5 仍保存原始帧，并明确记录候选非权威属性。它不会自动成为正式 SI。</p></div>
              {selectedRuntimeProfile?.candidate_conversion_source && <span className="config-pill config-pill-unverified">{selectedRuntimeProfile.candidate_conversion_source === "local_override" ? "本机覆盖" : "设备档案候选"}</span>}
            </div>
            {!selectedRuntimeProfile ? <div className="warning-banner">该设备目前只存在于工作区，尚不是运行时可选设备。本机候选存储按运行时 SN 管理；请直接编辑上方工作区 SI，或先保存并选择包含该设备的本机快照。</div> : !candidateDraft ? <div className="placeholder compact"><span>当前设备没有候选 SI。 <button onClick={() => {
              setCandidateDraft({
                accel_counts_per_g: null,
                gyro_counts_per_dps: null,
                accel_bias_counts: [0, 0, 0],
                gyro_bias_counts: [0, 0, 0],
                raw_axis_order: [0, 1, 2],
                axis_signs: [1, 1, 1],
                evidence_status: "local_operator_candidate_unverified",
              });
              setCandidateDirty(true);
            }}>创建候选表单</button></span></div> : <>
              <div className="config-form-grid">
                <label>加速度 counts/g<input type="number" step="any" value={candidateDraft.accel_counts_per_g ?? ""} onChange={(event) => { setCandidateDraft({ ...candidateDraft, accel_counts_per_g: nullableNumber(event.target.value) }); setCandidateDirty(true); }} /></label>
                <label>角速度 counts/(°/s)<input type="number" step="any" value={candidateDraft.gyro_counts_per_dps ?? ""} onChange={(event) => { setCandidateDraft({ ...candidateDraft, gyro_counts_per_dps: nullableNumber(event.target.value) }); setCandidateDirty(true); }} /></label>
                <label className="wide">候选证据状态<input value={candidateDraft.evidence_status} onChange={(event) => { setCandidateDraft({ ...candidateDraft, evidence_status: event.target.value }); setCandidateDirty(true); }} /></label>
              </div>
              <div className="triple-grid">
                <TripleFields label="加速度 bias" value={candidateDraft.accel_bias_counts} onChange={(value) => { setCandidateDraft({ ...candidateDraft, accel_bias_counts: value }); setCandidateDirty(true); }} />
                <TripleFields label="角速度 bias" value={candidateDraft.gyro_bias_counts} onChange={(value) => { setCandidateDraft({ ...candidateDraft, gyro_bias_counts: value }); setCandidateDirty(true); }} />
                <TripleFields label="原始轴顺序" value={candidateDraft.raw_axis_order} choices={[0, 1, 2]} onChange={(value) => { setCandidateDraft({ ...candidateDraft, raw_axis_order: value }); setCandidateDirty(true); }} />
                <TripleFields label="轴方向" value={candidateDraft.axis_signs} choices={[-1, 1]} onChange={(value) => { setCandidateDraft({ ...candidateDraft, axis_signs: value }); setCandidateDirty(true); }} />
              </div>
              <div className="save-row">
                <button className="primary" disabled={!candidateDirty || Boolean(busy)} onClick={saveCandidate}>{busy === "candidate" ? "保存中…" : "保存本机候选"}</button>
                {selectedRuntimeProfile?.candidate_conversion_source === "local_override" && <>
                  <a className="button-link" href={`/api/v1/devices/imu-candidates/${selectedDevice.sensor_sn}/export`} download>导出候选 YAML</a>
                  <button disabled={Boolean(busy)} onClick={clearCandidate}>清除本机候选</button>
                </>}
                <button disabled={candidateDirty || workspaceState === "invalid"} title={candidateDirty ? "请先保存候选，确保复制的是明确版本" : "复制后仍为未验证、仅 test"} onClick={promoteCandidate}>复制到工作区 SI（未验证）</button>
              </div>
            </>}
          </section>
        </>}
      </div>
    </section>}

    {tab === "runtime" && <section className="settings-split runtime-layout">
      <div className="panel config-detail-panel">
        <div className="panel-title">运行时文件与边界</div>
        <dl className="config-definition-list">
          <div><dt>设备 v1 引导文件</dt><dd><code>{runtimeConfiguration?.device_registry_path ?? "—"}</code></dd></div>
          <div><dt>动作标签</dt><dd><code>{runtimeConfiguration?.activity_taxonomy_path ?? "—"}</code></dd></div>
          <div><dt>v2 本机工作区</dt><dd><code>{runtimeConfiguration?.configuration_root ?? status?.workspace_path ?? "—"}</code></dd></div>
          <div><dt>团队缓存 / LKG</dt><dd><code>{runtimeConfiguration?.configuration_cache_root ?? status?.cache_root ?? "—"}</code></dd></div>
          <div><dt>团队 broker</dt><dd><code>{cloud?.broker_url ?? "未配置"}</code></dd></div>
          <div><dt>团队身份</dt><dd>{cloud?.logged_in ? cloud.email ?? "已登录" : "未登录"}</dd></div>
          <div><dt>支持 Snapshot schema</dt><dd>{capabilities?.configuration_schema_versions.join(", ") ?? "—"}</dd></div>
          <div><dt>支持协议</dt><dd>{capabilities?.protocol_ids.join(", ") ?? "—"}</dd></div>
        </dl>
        {!cloud?.configured && <div className="warning-banner">
          要启用中心 SN、团队候选和缓存刷新，请在采集服务启动配置中设置 <code>cloud.broker_url</code> 与 <code>cloud.google_oauth_client_id</code>，然后重启采集服务。发布模式可继续保持 disabled。
        </div>}
      </div>

      <div className="panel config-detail-panel">
        <div className="panel-heading-row">
          <div><div className="panel-title">附近 BLE IMU</div><p>“查找附近 IMU”执行一次约 5 秒的广播发现。它不建立 GATT 连接、不订阅数据、不修改 Snapshot，也不会自动登记设备。</p></div>
          <button disabled={interactionBlocked || busy === "ble-scan"} onClick={scanBle}>{busy === "ble-scan" ? "查找中（约 5 秒）…" : "查找附近 IMU"}</button>
        </div>
        {bleScan?.requested ? <dl className="config-definition-list">
          <div><dt>适配器状态</dt><dd>{bleScan.adapter_state}</dd></div>
          <div><dt>耗时</dt><dd>{bleScan.elapsed_ms.toFixed(0)} ms</dd></div>
          <div><dt>目标是否发现</dt><dd>{bleScan.target_found ? "是" : "否"}</dd></div>
          <div><dt>扫描错误</dt><dd>{bleScan.error ? JSON.stringify(bleScan.error) : "无"}</dd></div>
        </dl> : <div className="placeholder compact">尚未在本次运行中执行 BLE 发现</div>}
        <div className="ble-result-list">
          {bleCandidates.map((item) => <article key={item.local_device_id}>
            <div><strong>{item.name || "未命名 IMU"}</strong><StatusPill state={item.registration_state === "registered" ? "active" : "unverified"} /></div>
            <code>{item.local_device_id}</code>
            <span>{item.address} · RSSI {item.rssi} dBm</span>
            <small>匹配 SN：{item.matched_sensor_sns.join(", ") || "未登记"}</small>
            <small>Services：{item.service_uuids.join(", ") || "未广播"}</small>
          </article>)}
        </div>
        {bleScan?.requested && bleCandidates.length === 0 && !bleScan.error && <div className="warning-banner">
          本次未发现符合名称或 Service 过滤条件的 IMU。设备可能未广播、距离过远，或系统蓝牙权限/适配器状态需要检查。
        </div>}
      </div>

      <div className="panel config-detail-panel">
        <div className="panel-title">故障排查原则</div>
        <ul className="diagnostic-checklist">
          <li>未知 Snapshot schema：拒绝更新，继续使用上一份有效缓存。</li>
          <li>未知 protocol ID：只禁用对应设备，其余 Snapshot 仍可使用。</li>
          <li>本机或待审批快照：仅允许 test；不会写成正式权威 SI。</li>
          <li>revoked：历史录制仍保留引用，新预览与录制不可选用。</li>
          <li>底层 YAML、标签与服务启动配置仍是只读运行文件，修改后需要重启。</li>
        </ul>
      </div>
    </section>}
  </main>;
}

export function DeviceConfigurationAdminPage({ canManage }: { canManage: boolean }) {
  const [summary, setSummary] = useState<{
    snapshots: SnapshotSummary[];
    current: { snapshot_id: string } | null;
    current_revision: number;
  } | null>(null);
  const [selected, setSelected] = useState("");
  const [detail, setDetail] = useState<SnapshotDetail | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");

  const refresh = async () => {
    const value = await requestJson<{
      snapshots: SnapshotSummary[];
      current: { snapshot_id: string } | null;
      current_revision: number;
    }>("/api/v1/device-config/snapshots");
    setSummary(value);
    const wanted = selected || value.current?.snapshot_id || value.snapshots[0]?.snapshot_id || "";
    setSelected(wanted);
    if (wanted) {
      setDetail(await requestJson<SnapshotDetail>(`/api/v1/device-config/snapshots/${encodeURIComponent(wanted)}`));
    }
  };

  useEffect(() => {
    refresh().catch((caught) => setError((caught as Error).message));
  }, []);

  const choose = async (snapshotId: string) => {
    setSelected(snapshotId);
    setError("");
    try {
      setDetail(await requestJson<SnapshotDetail>(`/api/v1/device-config/snapshots/${encodeURIComponent(snapshotId)}`));
    } catch (caught) {
      setError((caught as Error).message);
    }
  };

  const act = async (action: "approve" | "revoke" | "current") => {
    if (!detail || !summary) return;
    const question = action === "approve"
      ? "批准该 Snapshot？批准后可用于正式采集。"
      : action === "revoke"
        ? "撤销该 Snapshot？撤销后不能开始新的采集。"
        : "将该 Snapshot 设为团队 Current？";
    if (!window.confirm(question)) return;
    setBusy(action);
    setError("");
    setMessage("");
    try {
      const path = action === "current"
        ? "/api/v1/device-config/current"
        : `/api/v1/device-config/snapshots/${detail.snapshot.snapshot_id}/${action}`;
      const body = action === "current"
        ? { snapshot_id: detail.snapshot.snapshot_id, expected_revision: summary.current_revision }
        : { expected_revision: detail.review?.revision ?? 1 };
      await requestJson(path, { method: "POST", body: JSON.stringify(body) });
      setMessage(action === "approve" ? "已批准 Snapshot" : action === "revoke" ? "已撤销 Snapshot" : "已更新团队 Current");
      await refresh();
    } catch (caught) {
      const text = (caught as Error).message;
      if (/revision|并发|冲突|更新/.test(text)) {
        setError("配置状态已被其他管理员更新。本页已重新读取最新 revision，请核对后再操作。");
        await refresh().catch(() => undefined);
      } else {
        setError(text);
      }
    } finally {
      setBusy("");
    }
  };

  const downloadSnapshot = () => {
    if (!detail) return;
    const blob = new Blob([`${JSON.stringify(detail.snapshot, null, 2)}\n`], {
      type: "application/json",
    });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `${detail.snapshot.snapshot_id}.json`;
    link.click();
    URL.revokeObjectURL(url);
  };

  const devices = detail?.snapshot.content.devices ?? [];
  const eventRows = useMemo(() => detail?.events ?? [], [detail]);
  return <main className="settings-page">
    <section className="settings-heading">
      <div><span className="eyebrow">TEAM DEVICE GOVERNANCE</span><h2>设备配置审批</h2><p>Snapshot 内容不可变；审批状态和团队 Current 独立记录并保留审计事件。</p></div>
      <div className="config-summary-card"><span>团队 Current</span><strong>{summary?.current?.snapshot_id ?? "尚未设置"}</strong><small>{canManage ? "管理员可审批与切换" : "只读查看"}</small></div>
    </section>
    {error && <div className="error-banner">{error}</div>}
    {message && <div className="success-banner">{message}</div>}
    <section className="settings-split admin-config-layout">
      <div className="panel config-list-panel">
        <div className="panel-heading-row"><div><div className="panel-title">全部团队快照</div><p>候选 → 已批准 → 已撤销，不允许回退或删除。</p></div><button onClick={() => refresh().catch((caught) => setError((caught as Error).message))}>刷新</button></div>
        <div className="config-snapshot-list">{summary?.snapshots.map((item) => <button className={selected === item.snapshot_id ? "selected" : ""} key={item.snapshot_id} onClick={() => choose(item.snapshot_id)}><span><strong>{item.name}</strong><StatusPill state={item.state} /></span><code>{item.snapshot_id}</code><small>{item.publisher} · {item.device_count} 台</small><span className="config-flags">{item.current && <b>Current</b>}</span></button>)}</div>
      </div>
      <div className="panel config-detail-panel">
        <div className="panel-heading-row"><div><div className="panel-title">{detail?.snapshot.name ?? "Snapshot 详情"}</div><p>{detail?.snapshot.description}</p></div>{detail?.review && <StatusPill state={detail.review.state} />}</div>
        {detail && <>
          <dl className="config-definition-list">
            <div><dt>Snapshot ID</dt><dd><code>{detail.snapshot.snapshot_id}</code></dd></div>
            <div><dt>Snapshot SHA-256</dt><dd title={detail.snapshot.snapshot_sha256}>{shortHash(detail.snapshot.snapshot_sha256)}</dd></div>
            <div><dt>内容 SHA-256</dt><dd title={detail.snapshot.content_sha256}>{shortHash(detail.snapshot.content_sha256)}</dd></div>
            <div><dt>基于 Snapshot</dt><dd><code>{detail.snapshot.base_snapshot_id ?? "无"}</code></dd></div>
            <div><dt>发布者 / 时间</dt><dd>{detail.snapshot.publisher} · {dateLabel(detail.snapshot.published_at_utc)}</dd></div>
            <div><dt>审批 revision</dt><dd>{detail.review?.revision ?? "—"}</dd></div>
            <div><dt>关联录制</dt><dd>{detail.linked_recordings?.length ?? 0}</dd></div>
          </dl>
          <div className="save-row">
            <button onClick={downloadSnapshot}>下载完整 Snapshot JSON</button>
            {canManage && <>
              <button className="primary" disabled={Boolean(busy) || detail.review?.state !== "candidate"} onClick={() => act("approve")}>{busy === "approve" ? "批准中…" : "批准"}</button>
              <button className="danger" disabled={Boolean(busy) || detail.review?.state !== "approved" || summary?.current?.snapshot_id === detail.snapshot.snapshot_id} onClick={() => act("revoke")}>{busy === "revoke" ? "撤销中…" : "撤销"}</button>
              <button disabled={Boolean(busy) || detail.review?.state !== "approved" || summary?.current?.snapshot_id === detail.snapshot.snapshot_id} onClick={() => act("current")}>{busy === "current" ? "更新中…" : "设为团队 Current"}</button>
            </>}
          </div>
          <div className="config-device-table">
            <div className="panel-title">包含的设备</div>
            {devices.map((device) => <article key={device.sensor_sn}><div><strong>{device.sensor_sn}</strong><span>{device.display_name}</span></div><div><code>{device.protocol_id}</code><span>{device.expected_rate_hz} Hz</span></div><div><StatusPill state={device.si_profile.verified ? "verified" : "unverified"} /><span>{device.allowed_data_tiers.join(" / ")}</span></div></article>)}
          </div>
          <details className="technical-details"><summary>审计事件（{eventRows.length}）</summary><pre>{JSON.stringify(eventRows, null, 2)}</pre></details>
        </>}
      </div>
    </section>
  </main>;
}

export type { BleCandidate, BleScanSummary, ConfigurationStatus, RuntimeConfiguration };
