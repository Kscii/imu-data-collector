import { useEffect, useMemo, useRef, useState } from "react";
import { apiErrorMessage, isEnglish, localizedField, tr, userVisibleMessage } from "./i18n";

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
  captureSensorSn: string;
  onOpenCalibration: () => void;
  registerLeaveGuard: (guard: (() => Promise<boolean>) | null) => void;
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
    throw Object.assign(new Error(apiErrorMessage(detail, response.status, response.statusText)), {
      status: response.status,
      detail,
    });
  }
  return payload as T;
}

function shortHash(value?: string | null) {
  return value ? `${value.slice(0, 12)}…` : "—";
}

function dateLabel(value?: string | null) {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString(isEnglish ? "en" : "zh-CN");
}

function stateLabel(state: string) {
  return ({
    local: tr("本机快照", "Local snapshot"),
    candidate: tr("待审批", "Pending review"),
    approved: tr("已批准", "Approved"),
    revoked: tr("已撤销", "Revoked"),
    verified: tr("SI 已验证", "SI verified"),
    unverified: tr("SI 未验证", "SI unverified"),
    active: tr("使用中", "Active"),
    retired: tr("已退役", "Retired"),
    unsupported: tr("协议不受支持", "Unsupported protocol"),
    loading: tr("读取中", "Loading"),
    unknown: tr("状态未知", "Unknown state"),
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
  captureSensorSn,
  onOpenCalibration,
  registerLeaveGuard,
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
  const pendingSave = useRef<Promise<unknown>>(Promise.resolve());
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
        const requestedSn = new URLSearchParams(location.search).get("device") || captureSensorSn;
        setSelectedDeviceSn(
          value.content.devices.find((item) => item.sensor_sn === requestedSn)?.sensor_sn
            ?? value.content.devices[0]?.sensor_sn
            ?? "",
        );
      }),
      requestJson<{ protocol_ids: string[]; configuration_schema_versions: string[] }>("/api/v1/device-capabilities").then(setCapabilities),
      requestJson<CloudStatus>("/api/v1/cloud/status").then(setCloud),
    ]).catch((caught) => setError(userVisibleMessage((caught as Error).message)));
  }, []);

  const saveText = (submittedText: string) => {
    const operation = pendingSave.current.catch(() => {}).then(async () => {
      if (submittedText === loadedText.current || submittedText !== latestWorkspaceText.current) return;
      const parsed = JSON.parse(submittedText) as ConfigurationSubmission;
      setWorkspaceState("saving");
      const saved = await requestJson<ConfigurationSubmission>("/api/v1/configuration/workspace", {
        method: "PUT", body: JSON.stringify(parsed),
      });
      const canonical = JSON.stringify(saved, null, 2);
      loadedText.current = canonical;
      if (latestWorkspaceText.current === submittedText) {
        latestWorkspaceText.current = canonical; setWorkspace(saved); setWorkspaceText(canonical);
        setWorkspaceState("saved"); setError("");
      }
    });
    pendingSave.current = operation;
    return operation;
  };
  const flushWorkspace = async (): Promise<boolean> => {
    try {
      await pendingSave.current.catch(() => {});
      while (latestWorkspaceText.current !== loadedText.current) {
        await saveText(latestWorkspaceText.current);
      }
      return true;
    } catch (caught) {
      setWorkspaceState("invalid");
      setError(tr("草稿尚未保存，请修正后再离开：", "The draft has not been saved. Fix it before leaving: ") + userVisibleMessage((caught as Error).message));
      return false;
    }
  };
  useEffect(() => {
    registerLeaveGuard(flushWorkspace);
    const beforeUnload = (event: BeforeUnloadEvent) => {
      if (latestWorkspaceText.current !== loadedText.current) { event.preventDefault(); event.returnValue = ""; }
    };
    window.addEventListener("beforeunload", beforeUnload);
    return () => { registerLeaveGuard(null); window.removeEventListener("beforeunload", beforeUnload); };
  }, []);
  useEffect(() => {
    if (!workspaceText && !loadedText.current) return;
    if (workspaceText === loadedText.current) {
      setWorkspaceState("saved");
      setError("");
      return;
    }
    try { JSON.parse(workspaceText); setWorkspaceState("dirty"); }
    catch { setWorkspaceState("invalid"); return; }
    const timer = window.setTimeout(() => {
      void saveText(workspaceText).catch(caught => {
        if (latestWorkspaceText.current === workspaceText) {
          setWorkspaceState("invalid"); setError(userVisibleMessage((caught as Error).message));
        }
      });
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
    if (!(await flushWorkspace())) return;
    setBusy(name);
    setError("");
    setMessage("");
    try {
      const value = await operation();
      const nextStatus = await refresh();
      const detailId = ["select", "reset", "refresh", "local"].includes(name)
        ? nextStatus.selected_snapshot_id
        : detail?.snapshot.snapshot_id;
      if (detailId) setDetail(await requestJson<SnapshotDetail>(`/api/v1/configuration/snapshots/${encodeURIComponent(detailId)}`));
      await onConfigurationChanged();
      setMessage(typeof success === "function" ? success(value) : success);
      return value;
    } catch (caught) {
      setError(userVisibleMessage((caught as Error).message));
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
      setError(userVisibleMessage((caught as Error).message));
    }
  };

  const copySnapshotToWorkspace = async () => {
    if (!detail || !(await flushWorkspace())) return;
    if (workspace && JSON.stringify(workspace.content) !== JSON.stringify(detail.snapshot.content)
      && !window.confirm(tr("当前草稿与此配置不同。用选中配置替换整个草稿？", "The draft differs from this configuration. Replace the entire draft?"))) return;
    const draft: ConfigurationSubmission = {
      name: tr(`${detail.snapshot.name} · 工作副本`, `${detail.snapshot.name} · Working copy`),
      description: detail.snapshot.description,
      base_snapshot_id: detail.snapshot.snapshot_id,
      client_build: workspace?.client_build ?? detail.snapshot.client_build,
      content: detail.snapshot.content,
    };
    replaceWorkspace(draft);
    setSelectedDeviceSn(draft.content.devices.find(d => d.sensor_sn === captureSensorSn)?.sensor_sn ?? draft.content.devices[0]?.sensor_sn ?? "");
    setMessage(tr("已创建可编辑工作区副本；原快照保持不变，字段校验通过后自动保存。", "Editable workspace copy created. The source snapshot is unchanged; fields are autosaved after validation."));
  };

  const reserveAsset = async () => {
    const reservation = await run(
      "asset",
      () => requestJson<{ sensor_sn: string; hardware_asset_id: string; revision: number }>("/api/v1/configuration/identities/assets", { method: "POST" }),
      (value) => tr(`已永久保留 ${value.sensor_sn} 并加入工作区。请完成设备字段后再保存快照。`, `Permanently reserved ${value.sensor_sn} and added it to the workspace. Complete the device fields before saving a snapshot.`),
    );
    if (!reservation) return;
    const device: ConfigurationDevice = {
      sensor_sn: reservation.sensor_sn,
      hardware_asset_id: reservation.hardware_asset_id,
      revision: reservation.revision,
      supersedes_sn: null,
      lifecycle: "active",
      display_name: tr("待配置的新 IMU", "New IMU awaiting configuration"),
      identity: {
        advertised_name: tr("待填写", "To be filled in"),
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
      setError(tr(`请先选择 ${source.hardware_asset_id} 的最新 revision；旧 revision 不能直接跳级。`, `Select the latest revision of ${source.hardware_asset_id} first; older revisions cannot skip directly to a new revision.`));
      return;
    }
    const reservation = await run(
      "revision",
      () => requestJson<{ sensor_sn: string; hardware_asset_id: string; revision: number; supersedes_sn: string }>("/api/v1/configuration/identities/revisions", { method: "POST", body: JSON.stringify({ hardware_asset_id: source.hardware_asset_id }) }),
      (value) => tr(`已永久保留 ${value.sensor_sn} 并建立新 firmware revision 草稿。`, `Permanently reserved ${value.sensor_sn} and created a new firmware revision draft.`),
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
    ? tr("正在读取团队 broker 与登录状态。", "Loading team broker and sign-in status.")
    : !cloud.configured
    ? tr("当前安装尚未配置团队 broker 与桌面 OAuth Client ID。SN 只能由中心分配，不能生成本机临时号。", "The team broker and desktop OAuth client ID are not configured. SNs must be allocated centrally; local temporary identifiers cannot be generated.")
    : !cloud.logged_in
      ? tr("团队 broker 已配置，但尚未完成 Google 登录。登录后才可永久保留 SN。", "The team broker is configured, but Google sign-in is incomplete. Sign in to reserve a permanent SN.")
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
      tr("本机候选 SI 已保存；它只用于屏幕诊断与 test 证据标记，不具备正式 SI 权限。", "Local candidate SI saved. It is for on-screen diagnostics and test evidence only, without production SI authorization."),
    );
    setCandidateDirty(false);
  };

  const clearCandidate = async () => {
    if (!selectedDevice) return;
    await run(
      "clear-candidate",
      () => requestJson(`/api/v1/devices/imu-candidates/${encodeURIComponent(selectedDevice.sensor_sn)}`, { method: "DELETE" }),
      tr("已清除本机候选 SI。", "Local candidate SI cleared."),
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
    setMessage(tr("候选系数已复制到工作区 SI，并被强制标记为未验证、仅允许 test。保存或发布快照不会自动批准它。", "Candidate coefficients copied to workspace SI as unverified and test-only. Saving or publishing a snapshot does not approve them automatically."));
  };

  const scanBle = async () => {
    setBusy("ble-scan");
    setError("");
    try {
      await onScanBle();
    } catch (caught) {
      setError(userVisibleMessage((caught as Error).message));
    } finally {
      setBusy("");
    }
  };

  return <main className="settings-page" data-no-localize>
    <section className="settings-heading">
      <div>
        <span className="eyebrow">{tr("本机设备与配置", "LOCAL DEVICES & CONFIGURATION")}</span>
        <h2>{tr("设备与设置", "Devices & settings")}</h2>
        <p>{tr("低频配置与诊断集中在这里；采集页只保留当场必须操作的内容。", "Low-frequency configuration and diagnostics live here; the capture page keeps only the controls needed during a session.")}</p>
      </div>
      <div className="config-summary-card">
        <span>{tr("采集正在使用的配置", "Configuration used for capture")}</span>
        <strong>{status?.selected_source === "bootstrap" ? tr("应用附带的初始配置", "Configuration included with the app") : snapshots.find(item => item.snapshot_id === status?.selected_snapshot_id)?.name ?? tr("正在读取…", "Loading…")}</strong>
        <div>
          <StatusPill state={status?.selected_state ?? "loading"} />
          {status?.manually_pinned && <span className="config-pill">{tr("人工固定", "Manually pinned")}</span>}
        </div>
        <small>{cloud?.logged_in
          ? tr(`团队身份：${cloud.email ?? tr("已登录", "Signed in")}`, `Team identity: ${cloud.email ?? "Signed in"}`)
          : cloud?.configured ? tr("团队身份：未登录", "Team identity: signed out") : tr("团队 broker：未配置", "Team broker: not configured")}</small>
      </div>
    </section>

    <section className="panel settings-calibration-entry"><div><strong>{tr("需要测量新设备的单位系数？", "Need to measure a new device's conversion factors?")}</strong><p>{tr("连接 IMU → 识别外壳方向 → 做实验 → 生成配置草稿。", "Connect the IMU → identify its faces → run trials → create a configuration draft.")}</p><small>{tr("当前采集设备：", "Capture device: ")}{captureSensorSn || tr("尚未选择", "Not selected")}</small></div><button className="primary" onClick={onOpenCalibration}>{tr("IMU 标定实验", "IMU calibration experiment")}</button></section>
    {interactionBlocked && <p className="warning-banner">{tr("切换配置前，请先结束录制并释放预览设备。草稿仍可编辑和保存。", "Finish recording and release preview devices before switching configurations. You can still edit and save drafts.")}</p>}
    {error && <div className="error-banner">{error}</div>}
    {message && <div className="success-banner">{message}</div>}
    {status?.update_available && <div className="warning-banner">{tr("团队推荐版本已更新，本机仍保持你选择的配置。准备好后可点“使用团队推荐版本”。", "The team recommendation has changed. This computer keeps your selection until you choose Use the team recommendation.")}</div>}
    {cloud?.configured && !cloud.logged_in && <div className="warning-banner">{tr("远端配置操作需要团队登录。本机工作区和本机快照仍可使用。", "Remote configuration actions require team sign-in. The local workspace and local snapshots remain available.")}<button onClick={onOpenPublishing}>{tr("前往“记录与发布”登录", "Open Records & publishing to sign in")}</button>
    </div>}
    {cloud && !cloud.configured && <div className="warning-banner">{tr("当前安装未配置团队 broker；本机工作区、候选 SI 和本机快照仍可使用，中心 SN 与团队候选不可用。", "No team broker is configured. Local workspaces, candidate SI, and local snapshots remain available; central SN allocation and team candidates are unavailable.")}</div>}

    <nav className="settings-tabs" aria-label={tr("设备设置区域", "Device settings sections")}>
      <button className={tab === "snapshots" ? "active" : ""} onClick={() => setTab("snapshots")}>{tr("配置管理", "Configurations")}</button>
      <button className={tab === "devices" ? "active" : ""} onClick={() => setTab("devices")}>{tr("设备与标定参数", "Devices & calibration")}</button>
      <button className={tab === "runtime" ? "active" : ""} onClick={() => setTab("runtime")}>{tr("连接与诊断", "Connection & diagnostics")}</button>
    </nav>

    {tab === "snapshots" && <section className="settings-split">
      <div className="panel config-list-panel">
        <div className="panel-heading-row">
          <div><div className="panel-title">{tr("可用的配置版本", "Available configuration versions")}</div><p>{tr("每个版本保存一份固定配置。“正在使用”表示本机已选用；“团队推荐”表示团队建议使用。", "Each version is a fixed configuration. In use means selected here; Team recommendation means suggested by the team.")}</p></div>
          <button
            disabled={interactionBlocked || Boolean(busy) || remoteUnavailable}
            title={interactionBlocked ? tr("先结束录制并释放预览设备", "Finish recording and release preview devices first") : remoteUnavailable ? remoteReason : busy ? tr("正在处理，请稍候", "An operation is in progress") : ""}
            onClick={() => run("refresh", () => requestJson("/api/v1/configuration/refresh", { method: "POST" }), tr("已刷新团队配置缓存", "Team configuration cache refreshed"))}
          >{busy === "refresh" ? tr("刷新中…", "Refreshing…") : tr("刷新团队配置", "Refresh team configuration")}</button>
        </div>
        <div className="config-snapshot-list">
          {snapshots.length === 0 && <div className="placeholder compact">{tr("本机尚无可用快照", "No snapshot is available locally")}</div>}
          {snapshots.map((item) => <button
            key={item.snapshot_id}
            className={detail?.snapshot.snapshot_id === item.snapshot_id ? "selected" : ""}
            onClick={() => openSnapshot(item.snapshot_id)}
          >
            <span><strong>{item.name}</strong><StatusPill state={item.state} /></span>

            <small>{tr(`${item.device_count} 台设备 · ${dateLabel(item.published_at_utc)}`, `${item.device_count} devices · ${dateLabel(item.published_at_utc)}`)}</small>
            <span className="config-flags">{item.current && <b>{tr("团队推荐", "Team recommendation")}</b>}{item.selected && <b>{tr("正在使用", "In use")}</b>}</span>
          </button>)}
        </div>
      </div>

      <div className="panel config-detail-panel">
        <div className="panel-title">{detail ? detail.snapshot.name : tr("配置版本详情", "Configuration version")}</div>
        {!detail ? <div className="placeholder compact">{tr("选择左侧版本，查看内容并决定是否使用。", "Select a version to review and use it.")}</div> : <>
          <details className="technical-details"><summary>{tr("技术详情：版本标识与校验值", "Technical details: version IDs and checksums")}</summary><dl className="config-definition-list">
            <div><dt>Snapshot ID</dt><dd><code>{detail.snapshot.snapshot_id}</code></dd></div>
            <div><dt>{tr("状态", "Status")}</dt><dd><StatusPill state={detail.state ?? detail.review?.state ?? "unknown"} /></dd></div>
            <div><dt>Snapshot SHA-256</dt><dd title={detail.snapshot.snapshot_sha256}>{shortHash(detail.snapshot.snapshot_sha256)}</dd></div>
            <div><dt>{tr("内容 SHA-256", "Content SHA-256")}</dt><dd title={detail.snapshot.content_sha256}>{shortHash(detail.snapshot.content_sha256)}</dd></div>
            <div><dt>{tr("设备", "Devices")}</dt><dd>{detail.snapshot.content.devices.length}</dd></div>
            <div><dt>{tr("发布者 / 时间", "Publisher / time")}</dt><dd>{detail.snapshot.publisher} · {dateLabel(detail.snapshot.published_at_utc)}</dd></div>
          </dl></details>
          <p><StatusPill state={detail.state ?? detail.review?.state ?? "unknown"} /> · {detail.selected ? tr("本机正在使用", "In use on this computer") : tr("尚未在本机启用", "Not selected on this computer")}</p>
          <p>{detail.snapshot.description || tr("无说明", "No description")}</p>
          <div className="save-row">
            <button
              disabled={interactionBlocked || Boolean(busy) || detail.state === "revoked" || detail.selected}
              title={interactionBlocked ? tr("先结束录制并释放预览设备", "Finish recording and release preview devices first") : detail.selected ? tr("本机已经在使用此配置", "This configuration is already in use") : detail.state === "revoked" ? tr("此版本已撤销，不能再次启用", "This version was revoked and cannot be selected") : busy ? tr("正在处理，请稍候", "An operation is in progress") : ""}
              onClick={() => run("select", () => requestJson("/api/v1/configuration/select", {
                method: "POST",
                body: JSON.stringify({ snapshot_id: detail.snapshot.snapshot_id }),
              }), tr(`已选择 ${detail.snapshot.snapshot_id}`, `Selected ${detail.snapshot.snapshot_id}`))}
            >{busy === "select" ? tr("切换中…", "Switching…") : detail.selected ? tr("正在使用", "In use") : tr("使用这个配置", "Use this configuration")}</button>
            <button
              disabled={interactionBlocked || Boolean(busy) || !status?.manually_pinned || !status.current_snapshot_id}
              title={interactionBlocked ? tr("先结束录制并释放预览设备", "Finish recording and release preview devices first") : !status?.current_snapshot_id ? tr("团队尚未指定推荐版本", "The team has not set a recommendation") : !status.manually_pinned ? tr("本机已在跟随团队推荐", "This computer already follows the recommendation") : busy ? tr("正在处理，请稍候", "An operation is in progress") : ""}
              onClick={() => run("reset", () => requestJson("/api/v1/configuration/reset-current", { method: "POST" }), tr("已恢复跟随团队 Current", "Now following team Current"))}
            >{tr("使用团队推荐版本", "Use the team recommendation")}</button>
            <button disabled={Boolean(busy)} onClick={copySnapshotToWorkspace}>{tr("复制为可编辑草稿", "Copy to an editable draft")}</button>
          </div>
          {detail.state !== "approved" && <div className="warning-banner">{tr("此版本仅可测试，不能用于正式采集。提交团队审核并获批后，还需确认设备参数已验证且允许正式采集。", "This version is for testing only. Production requires team approval, verified device parameters and production permission.")}</div>}
        </>}
      </div>

      <div className="panel config-workspace-panel">
        <div className="panel-heading-row">
          <div><div className="panel-title">{tr("正在编辑的草稿", "Draft being edited")}</div><p>{tr("这里的修改会自动保存为草稿。点击“保存并用于本机测试”才会切换本机配置；提交团队审核不会自动启用正式采集。", "Edits are saved as a draft. Save and use for local testing to apply it here. Submitting for review does not enable production capture.")}</p></div>
          <span className={`workspace-state workspace-${workspaceState}`}>{({
            loading: tr("读取中", "Loading"),
            saved: tr("已保存草稿，尚未应用", "Draft saved; not applied"),
            dirty: tr("等待自动保存", "Waiting to autosave"),
            invalid: tr("字段或高级 JSON 错误", "Invalid fields or advanced JSON"),
            saving: tr("保存中", "Saving"),
          })[workspaceState]}</span>
        </div>
        <div className="config-form-grid">
          <label>{tr("配置名称", "Configuration name")}<input
            value={workspace?.name ?? ""}
            maxLength={120}
            onChange={(event) => patchWorkspace((next) => { next.name = event.target.value; })}
          /></label>

          <label className="wide">{tr("变更说明", "Change description")}<textarea
            value={workspace?.description ?? ""}
            maxLength={2000}
            onChange={(event) => patchWorkspace((next) => { next.description = event.target.value; })}
          /></label>
        </div>
        <details className="technical-details advanced-json">
          <summary>{tr("高级：完整 Snapshot JSON", "Advanced: complete snapshot JSON")}</summary>
          <div className="advanced-json-body">
            <p>{tr("仅用于批量迁移或排障。直接修改仍经过同一套 schema 校验；无效内容不会覆盖上一次有效工作区。", "For bulk migration or troubleshooting. Direct edits undergo the same schema validation; invalid content does not overwrite the last valid workspace.")}</p>
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
            disabled={interactionBlocked || Boolean(busy) || workspaceState !== "saved"}
            title={interactionBlocked ? tr("先结束录制并释放预览设备", "Finish recording and release preview devices first") : workspaceState !== "saved" ? tr("请等待草稿保存并修正错误", "Wait for the draft to save and fix any errors") : ""}
            onClick={() => workspace && run("local", async () => {
              const saved = await requestJson<{ snapshot: { snapshot_id: string } }>("/api/v1/configuration/local-snapshots", { method: "POST", body: JSON.stringify(workspace) });
              try { await requestJson("/api/v1/configuration/select", { method: "POST", body: JSON.stringify({ snapshot_id: saved.snapshot.snapshot_id }) }); }
              catch (e) { await refresh(); throw new Error(tr("配置版本已保存，但未切换：", "Version saved, but not selected: ") + (e as Error).message); }
              return saved;
            }, tr("已保存并用于本机测试；正式采集仍需团队审批", "Saved and selected for local testing; production still requires team approval"))}
          >{busy === "local" ? tr("保存中…", "Saving…") : tr("保存并用于本机测试", "Save and use for local testing")}</button>
          <button
            className="primary"
            disabled={Boolean(busy) || workspaceState !== "saved" || remoteUnavailable}
            title={remoteUnavailable ? remoteReason : busy ? tr("正在处理，请稍候", "An operation is in progress") : ""}
            onClick={() => workspace && run("publish", () => requestJson("/api/v1/configuration/publish", {
              method: "POST",
              body: JSON.stringify(workspace),
            }), tr("已提交团队候选，等待管理员审批", "Team candidate submitted for administrator review"))}
          >{busy === "publish" ? tr("提交中…", "Submitting…") : tr("提交团队审核", "Submit for team review")}</button>
        </div>
        {remoteUnavailable && <p className="disabled-reason">{tr("团队候选当前不可用：", "Team candidates are currently unavailable: ")}{remoteReason} {cloud?.configured
            ? <button onClick={onOpenPublishing}>{tr("前往“记录与发布”登录", "Open Records & publishing to sign in")}</button>
            : <button onClick={() => setTab("runtime")}>{tr("查看配置位置", "View configuration location")}</button>}
        </p>}
      </div>
    </section>}

    {tab === "devices" && <section className="settings-split device-editor-layout">
      <div className="panel config-list-panel">
        <div className="panel-title">{tr(`草稿中的设备（${devices.length}）`, `Devices in this draft (${devices.length})`)}</div>
        <p>{tr("选择设备以编辑草稿；不会改变当前采集设备。", "Select a device to edit its draft; this does not switch the capture device.")}</p>
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
          <button disabled={Boolean(busy) || remoteUnavailable} title={remoteUnavailable ? remoteReason : busy ? tr("正在处理，请稍候", "An operation is in progress") : ""} onClick={reserveAsset}>{tr("为新物理设备保留 SN", "Reserve SN for new hardware")}</button>
          <button disabled={Boolean(busy) || remoteUnavailable || !selectedDevice || !selectedIsLatestRevision} title={remoteUnavailable ? remoteReason : !selectedIsLatestRevision ? tr("只能从该物理资产的最新 revision 继续分配", "A new revision can only be allocated from this hardware asset's latest revision") : ""} onClick={reserveRevision}>{tr("为所选设备保留新 revision", "Reserve revision for selected device")}</button>
        </div>
        {remoteUnavailable && <div className="disabled-reason">
          <strong>{tr("为什么按钮不可用？", "Why are these buttons disabled?")}</strong><span>{remoteReason}</span>
          {cloud?.configured
            ? <button onClick={onOpenPublishing}>{tr("前往“记录与发布”登录", "Open Records & publishing to sign in")}</button>
            : <button onClick={() => setTab("runtime")}>{tr("查看 broker 配置位置", "View broker configuration location")}</button>}
        </div>}
      </div>

      <div className="panel config-detail-panel device-form-panel">
        <div className="panel-heading-row">
          <div><div className="panel-title">{tr("设备档案", "Device profile")}</div><p>{tr("固定 SN、资产号和 revision 由中心生成，只读；其余字段直接编辑工作区。", "Permanent SNs, asset IDs, and revisions are allocated centrally and are read-only. Edit other fields directly in the workspace.")}</p></div>
          {selectedDevice && <StatusPill state={selectedDevice.lifecycle} />}
        </div>
        {!selectedDevice ? <div className="placeholder compact">{tr("工作区内没有设备", "The workspace contains no devices")}</div> : <>
          <section className="form-section">
            <h3>{tr("身份与 BLE", "Identity and BLE")}</h3>
            <div className="config-form-grid">
              <label>{tr("固定 SN", "Permanent SN")}<input value={selectedDevice.sensor_sn} readOnly /></label>
              <label>{tr("物理资产 / revision", "Hardware asset / revision")}<input value={`${selectedDevice.hardware_asset_id} / R${String(selectedDevice.revision).padStart(2, "0")}`} readOnly /></label>
              <label>{tr("显示名称", "Display name")}<input value={selectedDevice.display_name} onChange={(event) => patchDevice((next) => { next.display_name = event.target.value; })} /></label>
              <label>{tr("状态", "Status")}<select value={selectedDevice.lifecycle} onChange={(event) => patchDevice((next) => { next.lifecycle = event.target.value as "active" | "retired"; })}><option value="active">{tr("使用中", "Active")}</option><option value="retired">{tr("已退役", "Retired")}</option></select></label>
              <label>{tr("广播名", "Advertised name")}<input value={selectedDevice.identity.advertised_name} onChange={(event) => patchDevice((next) => { next.identity.advertised_name = event.target.value; })} /></label>
              <label>{tr("公共地址", "Public address")}<input value={selectedDevice.identity.public_address ?? ""} placeholder={tr("留空表示不固定", "Leave blank to allow any address")} onChange={(event) => patchDevice((next) => { next.identity.public_address = nullable(event.target.value); })} /></label>
              <label>{tr("地址类型", "Address type")}<select value={selectedDevice.identity.address_type} onChange={(event) => patchDevice((next) => { next.identity.address_type = event.target.value; })}><option value="public">public</option><option value="random">random</option><option value="unknown">unknown</option></select></label>
              <label>{tr("广播 Service UUID", "Advertised service UUID")}<input value={selectedDevice.identity.advertised_service_uuid ?? ""} placeholder={tr("可留空", "Optional")} onChange={(event) => patchDevice((next) => { next.identity.advertised_service_uuid = nullable(event.target.value); })} /></label>
              <label className="wide">{tr("GATT 指纹 SHA-256", "GATT fingerprint SHA-256")}<input value={selectedDevice.identity.gatt_fingerprint_sha256 ?? ""} placeholder={tr("64 位小写十六进制；未测量可留空", "64 lowercase hexadecimal characters; leave blank if not measured")} onChange={(event) => patchDevice((next) => { next.identity.gatt_fingerprint_sha256 = nullable(event.target.value); })} /></label>
            </div>
          </section>

          <section className="form-section">
            <h3>{tr("固件、协议与权限", "Firmware, protocol, and permissions")}</h3>
            <div className="config-form-grid">
              <label>{tr("固件版本", "Firmware version")}<input value={selectedDevice.firmware.version} onChange={(event) => patchDevice((next) => { next.firmware.version = event.target.value; })} /></label>
              <label>{tr("固件证据状态", "Firmware evidence status")}<input value={selectedDevice.firmware.evidence_status} onChange={(event) => patchDevice((next) => { next.firmware.evidence_status = event.target.value; })} /></label>
              <label className="wide">{tr("固件制品 SHA-256", "Firmware artifact SHA-256")}<input value={selectedDevice.firmware.artifact_sha256 ?? ""} placeholder={tr("未取得固件证据可留空", "Leave blank if firmware evidence is unavailable")} onChange={(event) => patchDevice((next) => { next.firmware.artifact_sha256 = nullable(event.target.value); })} /></label>
              <label>{tr("协议", "Protocol")}<select value={selectedDevice.protocol_id} onChange={(event) => patchDevice((next) => { next.protocol_id = event.target.value; })}>
                <option value="pending_protocol_selection">{tr("待确认（仅 test，运行时禁用）", "Pending selection (test only; disabled at runtime)")}</option>
                {capabilities?.protocol_ids.map((protocol) => <option key={protocol} value={protocol}>{protocol}</option>)}
                {capabilities && !capabilities.protocol_ids.includes(selectedDevice.protocol_id) && selectedDevice.protocol_id !== "pending_protocol_selection" && <option value={selectedDevice.protocol_id}>{selectedDevice.protocol_id}{tr("（当前不支持）", " (currently unsupported)")}</option>}
              </select></label>
              <label>{tr("预期采样率（Hz）", "Expected sample rate (Hz)")}<input type="number" min="0.001" max="1000" step="any" value={selectedDevice.expected_rate_hz} onChange={(event) => patchDevice((next) => { next.expected_rate_hz = Number(event.target.value); })} /></label>
              <label className="wide">{tr("采样率证据状态", "Sample rate evidence status")}<input value={selectedDevice.expected_rate_status} onChange={(event) => patchDevice((next) => { next.expected_rate_status = event.target.value; })} /></label>
              <label className="checkbox-label"><input type="checkbox" checked={selectedDevice.allowed_data_tiers.includes("test")} onChange={(event) => patchDevice((next) => { next.allowed_data_tiers = event.target.checked ? Array.from(new Set([...next.allowed_data_tiers, "test"])) : next.allowed_data_tiers.filter((tier) => tier !== "test"); })} />{tr("允许 test", "Allow test")}</label>
              <label className="checkbox-label"><input type="checkbox" checked={selectedDevice.allowed_data_tiers.includes("prod")} disabled={!selectedDevice.si_profile.verified} title={!selectedDevice.si_profile.verified ? tr("必须先完成并验证 SI，才能允许 prod", "Complete and verify SI before allowing prod") : ""} onChange={(event) => patchDevice((next) => { next.allowed_data_tiers = event.target.checked ? Array.from(new Set([...next.allowed_data_tiers, "prod"])) : next.allowed_data_tiers.filter((tier) => tier !== "prod"); })} />{tr("允许 prod", "Allow prod")}</label>
              <label className="wide">{tr("审计文档路径", "Audit document path")}<input value={selectedDevice.audit_document ?? ""} placeholder={tr("例如 ../docs/device-audits/…", "For example, ../docs/device-audits/…")} onChange={(event) => patchDevice((next) => { next.audit_document = nullable(event.target.value); })} /></label>
            </div>
          </section>

          <section className="form-section si-profile-card">
            <p>{tr("正在编辑此设备的草稿参数。实验生成的结果会填在这里；确认独立验证结果后，才能声明已验证。团队审批是之后的独立步骤。", "These are this device's draft parameters. Experiment results appear here. Review independent validation before marking them verified; team approval is a separate later step.")}</p>
            <button onClick={onOpenCalibration}>{tr("通过标定实验生成参数", "Generate parameters with a calibration experiment")}</button>
            <StatusPill state={selectedDevice.si_profile.verified ? "verified" : "unverified"} />
            <div className="config-form-grid">
              <label>{tr("加速度 counts/g", "Acceleration counts/g")}<input type="number" min="0" step="any" value={selectedDevice.si_profile.accel_counts_per_g ?? ""} onChange={(event) => patchDevice((next) => { next.si_profile.accel_counts_per_g = nullableNumber(event.target.value); })} /></label>
              <label>{tr("角速度 counts/(°/s)", "Angular velocity counts/(°/s)")}<input type="number" min="0" step="any" value={selectedDevice.si_profile.gyro_counts_per_dps ?? ""} onChange={(event) => patchDevice((next) => { next.si_profile.gyro_counts_per_dps = nullableNumber(event.target.value); })} /></label>
            </div>
            <details className="technical-details">
              <summary>{tr("高级：偏置、轴映射与验证证据", "Advanced: biases, axis mapping and verification evidence")}</summary>
              <p><span>{tr("派生 SI Profile ID（自动计算）", "Derived SI profile ID (automatic)")}</span><code>{selectedDevice.si_profile.profile_id}</code></p>
              <div className="config-form-grid">
              <label className="wide">{tr("校准方法", "Calibration method")}<input value={selectedDevice.si_profile.method} onChange={(event) => patchDevice((next) => { next.si_profile.method = event.target.value; })} /></label>
              <label className="wide">{tr("证据 SHA-256", "Evidence SHA-256")}<input value={selectedDevice.si_profile.evidence_sha256 ?? ""} placeholder={tr("验证后必须填写 64 位小写十六进制", "Verified profiles require 64 lowercase hexadecimal characters")} onChange={(event) => patchDevice((next) => { next.si_profile.evidence_sha256 = nullable(event.target.value); })} /></label>
              <label className="checkbox-label wide"><input
                type="checkbox"
                checked={selectedDevice.si_profile.verified}
                disabled={!selectedDevice.si_profile.accel_counts_per_g || !selectedDevice.si_profile.gyro_counts_per_dps || !selectedDevice.si_profile.evidence_sha256}
                title={tr("只有两个尺度和证据 SHA 齐全时才可验证", "Both scale factors and the evidence SHA are required for verification")}
                onChange={(event) => patchDevice((next) => {
                  next.si_profile.verified = event.target.checked;
                  if (!event.target.checked) next.allowed_data_tiers = next.allowed_data_tiers.filter((tier) => tier !== "prod");
                })}
              />{tr("标记为已经验证（不会自动授予 prod；需另行勾选）", "Mark as verified (does not automatically enable prod; select it separately)")}</label>
            </div>
            <div className="triple-grid">
              <TripleFields label={tr("加速度 bias（raw counts）", "Accelerometer bias (raw counts)")} value={selectedDevice.si_profile.accel_bias_counts} onChange={(value) => patchDevice((next) => { next.si_profile.accel_bias_counts = value; })} />
              <TripleFields label={tr("角速度 bias（raw counts）", "Gyroscope bias (raw counts)")} value={selectedDevice.si_profile.gyro_bias_counts} onChange={(value) => patchDevice((next) => { next.si_profile.gyro_bias_counts = value; })} />
              <TripleFields label={tr("原始轴顺序（0/1/2）", "Raw axis order (0/1/2)")} value={selectedDevice.si_profile.raw_axis_order} choices={[0, 1, 2]} onChange={(value) => patchDevice((next) => { next.si_profile.raw_axis_order = value; })} />
              <TripleFields label={tr("轴方向（仅 -1/1）", "Axis signs (-1/1 only)")} value={selectedDevice.si_profile.axis_signs} choices={[-1, 1]} onChange={(value) => patchDevice((next) => { next.si_profile.axis_signs = value; })} />
            </div>
            <details className="nested-details">
              <summary>{tr("坐标系与证据摘要（", "Coordinate system and evidence summaries (")}{selectedDevice.si_profile.evidence.length}{tr("条）", " entries)")}</summary>
              <p className="stage-help">{tr("常用坐标方向可直接编辑；逐条证据的完整内容可在“高级 JSON”中复核，避免在日常表单中误删长证据链。", "Edit common axis directions directly. Review complete evidence in Advanced JSON to avoid accidentally removing detailed provenance in the routine form.")}</p>
              <div className="config-form-grid">
                {["handedness", "x_positive_zh", "x_positive_en", "y_positive_zh", "y_positive_en", "z_positive_zh", "z_positive_en"].map((key) => <label className={key === "handedness" ? "" : "wide"} key={key}>{key}<input value={selectedDevice.si_profile.coordinate_system[key] ?? ""} onChange={(event) => patchDevice((next) => {
                  if (event.target.value) next.si_profile.coordinate_system[key] = event.target.value;
                  else delete next.si_profile.coordinate_system[key];
                })} /></label>)}
              </div>
              <div className="evidence-summary-list">
                {selectedDevice.si_profile.evidence.map((item, index) => <article key={`${item.recording_id}-${index}`}><code>{item.recording_id}</code><span>{item.kind}</span><small>{localizedField(item, "summary") || item.summary_zh || item.summary_en || tr("无摘要", "No summary")}</small></article>)}
              </div>
            </details>
            </details>
          </section>

          <details className="form-section candidate-editor"><summary>{tr("高级：手动测试系数", "Advanced: manual test coefficients")}</summary>
            <div className="panel-heading-row">
              <div><h3>{tr("本机候选 SI", "Local candidate SI")}</h3><p>{tr("候选值用于屏幕诊断；录制 H5 仍保存原始帧，并明确记录候选非权威属性。它不会自动成为正式 SI。", "Candidate values are used for on-screen diagnostics. Capture H5 retains raw frames and explicitly marks the candidate as non-authoritative; it does not automatically become production SI.")}</p></div>
              {selectedRuntimeProfile?.candidate_conversion_source && <span className="config-pill config-pill-unverified">{selectedRuntimeProfile.candidate_conversion_source === "local_override" ? tr("本机覆盖", "Local override") : tr("设备档案候选", "Device profile candidate")}</span>}
            </div>
            {!selectedRuntimeProfile ? <div className="warning-banner">{tr("该设备目前只存在于工作区，尚不是运行时可选设备。本机候选存储按运行时 SN 管理；请直接编辑上方工作区 SI，或先保存并选择包含该设备的本机快照。", "This device exists only in the workspace and is not selectable at runtime. Local candidates use runtime SNs. Edit workspace SI above, or save and select a local snapshot containing this device first.")}</div> : !candidateDraft ? <div className="placeholder compact"><span>{tr("当前设备没有候选 SI。", "This device has no candidate SI.")}<button onClick={() => {
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
            }}>{tr("创建候选表单", "Create candidate form")}</button></span></div> : <>
              <div className="config-form-grid">
                <label>{tr("加速度 counts/g", "Acceleration counts/g")}<input type="number" step="any" value={candidateDraft.accel_counts_per_g ?? ""} onChange={(event) => { setCandidateDraft({ ...candidateDraft, accel_counts_per_g: nullableNumber(event.target.value) }); setCandidateDirty(true); }} /></label>
                <label>{tr("角速度 counts/(°/s)", "Angular velocity counts/(°/s)")}<input type="number" step="any" value={candidateDraft.gyro_counts_per_dps ?? ""} onChange={(event) => { setCandidateDraft({ ...candidateDraft, gyro_counts_per_dps: nullableNumber(event.target.value) }); setCandidateDirty(true); }} /></label>
                <label className="wide">{tr("候选证据状态", "Candidate evidence status")}<input value={candidateDraft.evidence_status} onChange={(event) => { setCandidateDraft({ ...candidateDraft, evidence_status: event.target.value }); setCandidateDirty(true); }} /></label>
              </div>
              <div className="triple-grid">
                <TripleFields label={tr("加速度 bias", "Accelerometer bias")} value={candidateDraft.accel_bias_counts} onChange={(value) => { setCandidateDraft({ ...candidateDraft, accel_bias_counts: value }); setCandidateDirty(true); }} />
                <TripleFields label={tr("角速度 bias", "Gyroscope bias")} value={candidateDraft.gyro_bias_counts} onChange={(value) => { setCandidateDraft({ ...candidateDraft, gyro_bias_counts: value }); setCandidateDirty(true); }} />
                <TripleFields label={tr("原始轴顺序", "Raw axis order")} value={candidateDraft.raw_axis_order} choices={[0, 1, 2]} onChange={(value) => { setCandidateDraft({ ...candidateDraft, raw_axis_order: value }); setCandidateDirty(true); }} />
                <TripleFields label={tr("轴方向", "Axis signs")} value={candidateDraft.axis_signs} choices={[-1, 1]} onChange={(value) => { setCandidateDraft({ ...candidateDraft, axis_signs: value }); setCandidateDirty(true); }} />
              </div>
              <div className="save-row">
                <button className="primary" disabled={!candidateDirty || Boolean(busy)} onClick={saveCandidate}>{busy === "candidate" ? tr("保存中…", "Saving…") : tr("保存本机候选", "Save local candidate")}</button>
                {selectedRuntimeProfile?.candidate_conversion_source === "local_override" && <>
                  <a className="button-link" href={`/api/v1/devices/imu-candidates/${selectedDevice.sensor_sn}/export`} download>{tr("导出候选 YAML", "Export candidate YAML")}</a>
                  <button disabled={Boolean(busy)} onClick={clearCandidate}>{tr("清除本机候选", "Clear local candidate")}</button>
                </>}
                <button disabled={candidateDirty || workspaceState === "invalid"} title={candidateDirty ? tr("请先保存候选，确保复制的是明确版本", "Save the candidate first so the copied version is explicit") : tr("复制后仍为未验证、仅 test", "The copy remains unverified and test-only")} onClick={promoteCandidate}>{tr("复制到工作区 SI（未验证）", "Copy to workspace SI (unverified)")}</button>
              </div>
            </>}
          </details>
        </>}
      </div>
    </section>}

    {tab === "runtime" && <section className="settings-split runtime-layout">
      <div className="panel config-detail-panel">
        <div className="panel-title">{tr("运行时文件与边界", "Runtime files and boundaries")}</div>
        <dl className="config-definition-list">
          <div><dt>{tr("设备 v1 引导文件", "Device v1 bootstrap file")}</dt><dd><code>{runtimeConfiguration?.device_registry_path ?? "—"}</code></dd></div>
          <div><dt>{tr("动作标签", "Activity taxonomy")}</dt><dd><code>{runtimeConfiguration?.activity_taxonomy_path ?? "—"}</code></dd></div>
          <div><dt>{tr("v2 本机工作区", "v2 local workspace")}</dt><dd><code>{runtimeConfiguration?.configuration_root ?? status?.workspace_path ?? "—"}</code></dd></div>
          <div><dt>{tr("团队缓存 / LKG", "Team cache / LKG")}</dt><dd><code>{runtimeConfiguration?.configuration_cache_root ?? status?.cache_root ?? "—"}</code></dd></div>
          <div><dt>{tr("团队 broker", "Team broker")}</dt><dd><code>{cloud?.broker_url ?? tr("未配置", "Not configured")}</code></dd></div>
          <div><dt>{tr("团队身份", "Team identity")}</dt><dd>{cloud?.logged_in ? cloud.email ?? tr("已登录", "Signed in") : tr("未登录", "Signed out")}</dd></div>
          <div><dt>{tr("支持 Snapshot schema", "Supported snapshot schemas")}</dt><dd>{capabilities?.configuration_schema_versions.join(", ") ?? "—"}</dd></div>
          <div><dt>{tr("支持协议", "Supported protocols")}</dt><dd>{capabilities?.protocol_ids.join(", ") ?? "—"}</dd></div>
        </dl>
        {!cloud?.configured && <div className="warning-banner">{tr("要启用中心 SN、团队候选和缓存刷新，请在采集服务启动配置中设置", "To enable central SN allocation, team candidates, and cache refresh, set")}<code>cloud.broker_url</code>{tr("与", "and")}<code>cloud.google_oauth_client_id</code>{tr("，然后重启采集服务。发布模式可继续保持 disabled。", " in the capture service startup configuration, then restart it. Publishing mode may remain disabled.")}</div>}
      </div>

      <div className="panel config-detail-panel">
        <div className="panel-heading-row">
          <div><div className="panel-title">{tr("附近 BLE IMU", "Nearby BLE IMUs")}</div><p>{tr("“查找附近 IMU”执行一次约 5 秒的广播发现。它不建立 GATT 连接、不订阅数据、不修改 Snapshot，也不会自动登记设备。", "Find nearby IMUs scans advertisements for about 5 seconds. It does not establish GATT connections, subscribe to data, modify snapshots, or register devices.")}</p></div>
          <button disabled={interactionBlocked || busy === "ble-scan"} onClick={scanBle}>{busy === "ble-scan" ? tr("查找中（约 5 秒）…", "Scanning (about 5 seconds)…") : tr("查找附近 IMU", "Find nearby IMUs")}</button>
        </div>
        {bleScan?.requested ? <dl className="config-definition-list">
          <div><dt>{tr("适配器状态", "Adapter status")}</dt><dd>{bleScan.adapter_state}</dd></div>
          <div><dt>{tr("耗时", "Elapsed time")}</dt><dd>{bleScan.elapsed_ms.toFixed(0)} ms</dd></div>
          <div><dt>{tr("目标是否发现", "Target found")}</dt><dd>{bleScan.target_found ? tr("是", "Yes") : tr("否", "No")}</dd></div>
          <div><dt>{tr("扫描错误", "Scan error")}</dt><dd>{bleScan.error ? apiErrorMessage(bleScan.error, 503, "Service Unavailable") : tr("无", "None")}</dd></div>
        </dl> : <div className="placeholder compact">{tr("尚未在本次运行中执行 BLE 发现", "No BLE discovery has run in this session")}</div>}
        <div className="ble-result-list">
          {bleCandidates.map((item) => <article key={item.local_device_id}>
            <div><strong>{item.name || tr("未命名 IMU", "Unnamed IMU")}</strong><StatusPill state={item.registration_state === "registered" ? "active" : "unverified"} /></div>
            <code>{item.local_device_id}</code>
            <span>{item.address} · RSSI {item.rssi} dBm</span>
            <small>{tr("匹配 SN：", "Matching SNs: ")}{item.matched_sensor_sns.join(", ") || tr("未登记", "Not registered")}</small>
            <small>Services：{item.service_uuids.join(", ") || tr("未广播", "Not advertised")}</small>
          </article>)}
        </div>
        {bleScan?.requested && bleCandidates.length === 0 && !bleScan.error && <div className="warning-banner">{tr("本次未发现符合名称或 Service 过滤条件的 IMU。设备可能未广播、距离过远，或系统蓝牙权限/适配器状态需要检查。", "No IMU matched the name or service filters. Check whether the device is advertising and within range, and check Bluetooth permissions and adapter status.")}</div>}
      </div>

      <div className="panel config-detail-panel">
        <div className="panel-title">{tr("故障排查原则", "Troubleshooting rules")}</div>
        <ul className="diagnostic-checklist">
          <li>{tr("未知 Snapshot schema：拒绝更新，继续使用上一份有效缓存。", "Unknown snapshot schema: reject the update and keep the last valid cache.")}</li>
          <li>{tr("未知 protocol ID：只禁用对应设备，其余 Snapshot 仍可使用。", "Unknown protocol ID: disable only that device; the rest of the snapshot remains usable.")}</li>
          <li>{tr("本机或待审批快照：仅允许 test；不会写成正式权威 SI。", "Local or pending snapshots are test-only and never become authoritative SI.")}</li>
          <li>{tr("revoked：历史录制仍保留引用，新预览与录制不可选用。", "Revoked: historical references remain, but the snapshot cannot be used for new previews or recordings.")}</li>
          <li>{tr("底层 YAML、标签与服务启动配置仍是只读运行文件，修改后需要重启。", "Low-level YAML, taxonomy, and service startup settings remain read-only runtime files and require a restart after changes.")}</li>
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
    refresh().catch((caught) => setError(userVisibleMessage((caught as Error).message)));
  }, []);

  const choose = async (snapshotId: string) => {
    setSelected(snapshotId);
    setError("");
    try {
      setDetail(await requestJson<SnapshotDetail>(`/api/v1/device-config/snapshots/${encodeURIComponent(snapshotId)}`));
    } catch (caught) {
      setError(userVisibleMessage((caught as Error).message));
    }
  };

  const act = async (action: "approve" | "revoke" | "current") => {
    if (!detail || !summary) return;
    const question = action === "approve"
      ? tr("批准该 Snapshot？批准后可用于正式采集。", "Approve this snapshot? It can then authorize production capture.")
      : action === "revoke"
        ? tr("撤销该 Snapshot？撤销后不能开始新的采集。", "Revoke this snapshot? It cannot be used to start new captures afterwards.")
        : tr("将该 Snapshot 设为团队 Current？", "Set this snapshot as team Current?");
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
      setMessage(action === "approve" ? tr("已批准 Snapshot", "Snapshot approved") : action === "revoke" ? tr("已撤销 Snapshot", "Snapshot revoked") : tr("已更新团队 Current", "Team Current updated"));
      await refresh();
    } catch (caught) {
      const text = (caught as Error).message;
      const failure = caught as Error & { status?: number; detail?: unknown };
      if (/revision|并发|冲突|更新/.test(JSON.stringify(failure.detail ?? ""))) {
        setError(tr("配置状态已被其他管理员更新。本页已重新读取最新 revision，请核对后再操作。", "Another administrator changed the configuration state. The latest revision has been loaded; review it before trying again."));
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
  return <main className="settings-page" data-no-localize>
    <section className="settings-heading">
      <div><span className="eyebrow">TEAM DEVICE GOVERNANCE</span><h2>{tr("设备配置审批", "Device configuration review")}</h2><p>{tr("Snapshot 内容不可变；审批状态和团队 Current 独立记录并保留审计事件。", "Snapshot content is immutable. Review state and team Current are recorded independently with audit events.")}</p></div>
      <div className="config-summary-card"><span>{tr("团队 Current", "Team Current")}</span><strong>{summary?.current?.snapshot_id ?? tr("尚未设置", "Not set")}</strong><small>{canManage ? tr("管理员可审批与切换", "Administrators can review and switch") : tr("只读查看", "Read-only")}</small></div>
    </section>
    {error && <div className="error-banner">{error}</div>}
    {message && <div className="success-banner">{message}</div>}
    <section className="settings-split admin-config-layout">
      <div className="panel config-list-panel">
        <div className="panel-heading-row"><div><div className="panel-title">{tr("全部团队快照", "All team snapshots")}</div><p>{tr("候选 → 已批准 → 已撤销，不允许回退或删除。", "Candidate → approved → revoked. Transitions cannot be reversed and records cannot be deleted.")}</p></div><button onClick={() => refresh().catch((caught) => setError(userVisibleMessage((caught as Error).message)))}>{tr("刷新", "Refresh")}</button></div>
        <div className="config-snapshot-list">{summary?.snapshots.map((item) => <button className={selected === item.snapshot_id ? "selected" : ""} key={item.snapshot_id} onClick={() => choose(item.snapshot_id)}><span><strong>{item.name}</strong><StatusPill state={item.state} /></span><code>{item.snapshot_id}</code><small>{item.publisher} · {tr(`${item.device_count} 台`, `${item.device_count} devices`)}</small><span className="config-flags">{item.current && <b>Current</b>}</span></button>)}</div>
      </div>
      <div className="panel config-detail-panel">
        <div className="panel-heading-row"><div><div className="panel-title">{detail?.snapshot.name ?? tr("Snapshot 详情", "Snapshot details")}</div><p>{detail?.snapshot.description}</p></div>{detail?.review && <StatusPill state={detail.review.state} />}</div>
        {detail && <>
          <dl className="config-definition-list">
            <div><dt>Snapshot ID</dt><dd><code>{detail.snapshot.snapshot_id}</code></dd></div>
            <div><dt>Snapshot SHA-256</dt><dd title={detail.snapshot.snapshot_sha256}>{shortHash(detail.snapshot.snapshot_sha256)}</dd></div>
            <div><dt>{tr("内容 SHA-256", "Content SHA-256")}</dt><dd title={detail.snapshot.content_sha256}>{shortHash(detail.snapshot.content_sha256)}</dd></div>
            <div><dt>{tr("基于 Snapshot", "Based on snapshot")}</dt><dd><code>{detail.snapshot.base_snapshot_id ?? tr("无", "None")}</code></dd></div>
            <div><dt>{tr("发布者 / 时间", "Publisher / time")}</dt><dd>{detail.snapshot.publisher} · {dateLabel(detail.snapshot.published_at_utc)}</dd></div>
            <div><dt>{tr("审批 revision", "Review revision")}</dt><dd>{detail.review?.revision ?? "—"}</dd></div>
            <div><dt>{tr("关联录制", "Linked recordings")}</dt><dd>{detail.linked_recordings?.length ?? 0}</dd></div>
          </dl>
          <div className="save-row">
            <button onClick={downloadSnapshot}>{tr("下载完整 Snapshot JSON", "Download complete snapshot JSON")}</button>
            {canManage && <>
              <button className="primary" disabled={Boolean(busy) || detail.review?.state !== "candidate"} onClick={() => act("approve")}>{busy === "approve" ? tr("批准中…", "Approving…") : tr("批准", "Approve")}</button>
              <button className="danger" disabled={Boolean(busy) || detail.review?.state !== "approved" || summary?.current?.snapshot_id === detail.snapshot.snapshot_id} onClick={() => act("revoke")}>{busy === "revoke" ? tr("撤销中…", "Revoking…") : tr("撤销", "Revoke")}</button>
              <button disabled={Boolean(busy) || detail.review?.state !== "approved" || summary?.current?.snapshot_id === detail.snapshot.snapshot_id} onClick={() => act("current")}>{busy === "current" ? tr("更新中…", "Updating…") : tr("设为团队 Current", "Set as team Current")}</button>
            </>}
          </div>
          <div className="config-device-table">
            <div className="panel-title">{tr("包含的设备", "Included devices")}</div>
            {devices.map((device) => <article key={device.sensor_sn}><div><strong>{device.sensor_sn}</strong><span>{device.display_name}</span></div><div><code>{device.protocol_id}</code><span>{device.expected_rate_hz} Hz</span></div><div><StatusPill state={device.si_profile.verified ? "verified" : "unverified"} /><span>{device.allowed_data_tiers.join(" / ")}</span></div></article>)}
          </div>
          <details className="technical-details"><summary>{tr(`审计事件（${eventRows.length}）`, `Audit events (${eventRows.length})`)}</summary><pre>{JSON.stringify(eventRows, null, 2)}</pre></details>
        </>}
      </div>
    </section>
  </main>;
}

export type { BleCandidate, BleScanSummary, ConfigurationStatus, RuntimeConfiguration };
