import { useEffect, useRef, useState } from "react";
import { apiErrorMessage, tr } from "./i18n";
import { directions } from "./calibrationProtocol";

type Profile = { sensor_sn: string; display_name: string; selectable: boolean };
type Face = { description: string; sample: { median_counts: number[] } };
type Session = {
  session_id: string; sensor_sn: string; connected: boolean; ready: boolean;
  faces: Record<string, Face>; conflicts: string[];
  sample: { direction: string | null; reason: string; sample_count: number; median_counts?: number[] };
};
const base = "/api/v1/calibration-orientation";
async function request<T = any>(path: string, method = "GET", body?: unknown): Promise<T> {
  const response = await fetch(path, { method, keepalive: method === "POST" && path.endsWith("/stop"), headers: { "Content-Type": "application/json" }, body: body === undefined ? undefined : JSON.stringify(body) });
  const value = await response.json();
  if (!response.ok) {
    const message = apiErrorMessage(value.detail, response.status, response.statusText);
    const parts = message.split(" / ");
    throw new Error(parts.length === 2 ? tr(parts[0], parts[1]) : message);
  }
  return value;
}
const reasons: Record<string, [string, string]> = {
  waiting_samples: ["保持静止约 2 秒，正在接收样本…", "Keep still for about two seconds; collecting samples…"],
  stale: ["数据已过期，请检查设备连接。", "Data is stale. Check the device connection."],
  disconnected: ["IMU 已断开，请重新连接。", "IMU disconnected. Please reconnect."],
  moving: ["数据波动较大，请将设备放稳。", "The readings are changing. Hold the device still."],
  tilted: ["当前倾斜，无法确定单一朝上方向，请摆正。", "The device is tilted. Place one face upward."],
  saturated: ["原始数据饱和或无效，暂时不能认轴。", "Raw data is saturated or invalid; cannot identify the axis."],
  no_gravity_signal: ["没有明确的重力响应，请检查设备。", "No clear gravity response. Check the device."],
};

export function OrientationSetup({ sensorSn, profiles, onSelectSensor, blocked, operators, onCreated }: {
  sensorSn: string; profiles: Profile[]; onSelectSensor: (sn: string) => void; blocked: boolean;
  operators: string[]; onCreated: (experiment: any) => void;
}) {
  const [owner] = useState(() => crypto.randomUUID());
  const [session, setSession] = useState<Session | null>(null);
  const sessionRef = useRef<Session | null>(null);
  const [connecting, setConnecting] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [paused, setPaused] = useState(false);
  const [description, setDescription] = useState("");
  const [expected, setExpected] = useState<string | null>(null);
  const [operator, setOperator] = useState("");
  const [notes, setNotes] = useState("");
  const [editing, setEditing] = useState<string | null>(null);
  const [editedName, setEditedName] = useState("");
  const update = (value: Session | null) => { sessionRef.current = value; setSession(value); };

  useEffect(() => {
    if (!sensorSn || blocked || paused) return;
    let alive = true;
    let currentId: string | null = null;
    let poll: number | undefined;
    let heartbeat: number | undefined;
    let pending = false;
    setConnecting(true); setError("");
    request<Session>(`${base}/start`, "POST", { sensor_sn: sensorSn, owner_id: owner }).then(value => {
      currentId = value.session_id;
      if (!alive) { void request(`${base}/stop`, "POST", { session_id: currentId }).catch(() => {}); return; }
      update(value);
      poll = window.setInterval(async () => {
        if (pending) return;
        pending = true;
        try { const next = await request<Session>(`${base}?session_id=${currentId}`); if (alive) update(next); }
        catch (e) { if (alive) { setError((e as Error).message); update(null); } }
        finally { pending = false; }
      }, 350);
      heartbeat = window.setInterval(() => { void request(`${base}/heartbeat`, "POST", { session_id: currentId }).catch(e => { if (alive) setError(e.message); }); }, 4000);
    }).catch(e => { if (alive) { setError(e.message); update(null); } }).finally(() => { if (alive) setConnecting(false); });
    const unload = () => { if (currentId) void request(`${base}/stop`, "POST", { session_id: currentId }).catch(() => {}); };
    window.addEventListener("pagehide", unload);
    return () => {
      alive = false;
      window.clearInterval(poll); window.clearInterval(heartbeat);
      window.removeEventListener("pagehide", unload);
      unload();
    };
  }, [sensorSn, blocked, paused, attempt, owner]);

  const perform = async (action: () => Promise<void>) => {
    setBusy(true); setError("");
    try { await action(); } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  };
  const disconnect = async () => {
    if (sessionRef.current && Object.keys(sessionRef.current.faces).length && !window.confirm(tr("尚未创建实验。断开会清除本次六面记录，继续吗？", "The experiment has not been created. Disconnect and clear these face records?"))) return;
    await perform(async () => {
      if (sessionRef.current) await request(`${base}/stop`, "POST", { session_id: sessionRef.current.session_id });
      setPaused(true); update(null); setDescription(""); setExpected(null);
    });
  };
  const direction = session?.sample.direction ?? null;
  const locked = blocked || busy || connecting;
  const recordable = Boolean(session?.connected && direction && (!expected || expected === direction));

  return <section className="panel orientation-setup" data-no-localize>
    <div className="panel-heading-row"><div><h2>{tr("1. 连接 IMU，认识外壳方向", "1. Connect the IMU and identify its faces")}</h2><p>{tr("先在这里选设备。连接只用于认轴，不启动摄像头，也不开始正式录制。", "Select the device here. This connects only the IMU; it does not start the camera or a recording.")}</p></div></div>
    <div className="save-row">
      <label>{tr("实验设备", "Experiment device")}<select value={sensorSn} disabled={locked || Boolean(session)} onChange={e => { setPaused(false); onSelectSensor(e.target.value); }}><option value="">{tr("请选择固定 SN…", "Select a device SN…")}</option>{profiles.filter(p => p.selectable).map(p => <option key={p.sensor_sn} value={p.sensor_sn}>{p.sensor_sn} · {p.display_name}</option>)}</select></label>
      {session ? <button disabled={locked} onClick={disconnect}>{tr("断开设备", "Disconnect device")}</button> : <button disabled={locked || !sensorSn} onClick={() => { setPaused(false); setAttempt(v => v + 1); }}>{connecting ? tr("正在连接…", "Connecting…") : tr("连接 / 重试", "Connect / retry")}</button>}
      {session && !session.connected && <button disabled={locked} onClick={() => perform(async () => update(await request(`${base}/start`, "POST", { sensor_sn: sensorSn, owner_id: owner })))}>{tr("重新连接，保留六面记录", "Reconnect and keep face records")}</button>}
    </div>
    {blocked && <p className="warning-banner">{tr("当前页面没有设备控制权，或页面版本需要刷新。请先检查顶部提示。", "This page does not own device control, or needs a refresh. Check the message above.")}</p>}
    {error && <p className="error-banner" role="alert">{error}</p>}
    <div className="orientation-readout" role="status" aria-live="polite">
      <span>{tr("当前朝上方向", "Current upward direction")}</span>
      <strong>{connecting ? tr("连接中…", "Connecting…") : direction ? tr(`原始加速度 ${direction}`, `Raw acceleration ${direction}`) : "—"}</strong>
      <p>{direction ? tr("初步识别。请保持静止，填写朝上这一面的外壳特征。", "Initial identification. Keep still and describe the face pointing upward.") : !sensorSn ? tr("选择设备后自动连接。", "Select a device to connect.") : session ? tr(...(reasons[session.sample.reason] ?? reasons.waiting_samples)) : tr("连接设备后开始识别。", "Connect the device to identify its axes.")}</p>
    </div>
    <div className="save-row">
      <label className="orientation-description">{tr("朝上面的外壳特征", "Feature of the upward face")}<input maxLength={200} value={description} disabled={!session || locked || (!description && !direction)} placeholder={tr("例如：接口所在面", "For example: the connector face")} onChange={e => { if (!description) setExpected(direction); setDescription(e.target.value); if (!e.target.value) setExpected(null); }} /></label>
      <button className="primary" disabled={locked || !recordable || !description.trim()} onClick={() => perform(async () => {
        if (!session || !direction) return;
        const replace = Boolean(session.faces[direction]);
        if (replace && !window.confirm(tr(`已记录 ${direction}，替换其描述和方向证据吗？`, `${direction} is already recorded. Replace its description and evidence?`))) return;
        update(await request(`${base}/faces`, "POST", { session_id: session.session_id, direction: expected ?? direction, description, replace })); setDescription(""); setExpected(null);
      })}>{tr("记录这个面", "Record this face")}</button>
    </div>
    {expected && expected !== direction && <p className="warning-banner">{tr(`你正在填写 ${expected}。请摆回该方向并保持静止，或清空文字后重新填写。`, `You are describing ${expected}. Return to that direction and keep still, or clear the text to start again.`)}</p>}
    <p>{tr(`已记录 ${Object.keys(session?.faces ?? {}).length} / 6 面`, `${Object.keys(session?.faces ?? {}).length} / 6 faces recorded`)}</p>
    <div className="orientation-faces">{directions.map(key => {
      const face = session?.faces[key];
      return <article key={key} className={`${face ? "recorded" : ""} ${key === direction ? "upward" : ""}`}><strong>{key}</strong>{editing === key ? <><input aria-label={tr(`${key} 描述`, `${key} description`)} maxLength={200} value={editedName} onChange={e => setEditedName(e.target.value)} /><button disabled={locked || !editedName.trim()} onClick={() => perform(async () => { update(await request(`${base}/faces`, "PUT", { session_id: session!.session_id, direction: key, description: editedName })); setEditing(null); })}>{tr("保存描述", "Save description")}</button></> : <span>{face?.description ?? tr("尚未记录", "Not recorded")}</span>}{face && <div className="save-row"><button disabled={locked} onClick={() => { setEditing(key); setEditedName(face.description); }}>{tr("修改描述", "Edit description")}</button><button disabled={locked} onClick={() => perform(async () => { if (window.confirm(tr(`清除 ${key} 并重新识别？`, `Clear ${key} and identify it again?`))) { update(await request(`${base}/faces/${encodeURIComponent(key)}?session_id=${session!.session_id}`, "DELETE")); setEditing(null); } })}>{tr("重新识别", "Identify again")}</button></div>}</article>;
    })}</div>
    {Boolean(session?.conflicts.length) && <p className="error-banner">{tr(`正反面记录有冲突，请重新识别：${session!.conflicts.join("、")}`, `Opposite faces conflict. Identify these axes again: ${session!.conflicts.join(", ")}`)}</p>}
    <p className="stage-help">{tr("这里识别的是原始加速度轴，仍需六面实验复核。角速度方向由后续旋转实验验证；方向预览不计入标定次数。", "These are raw accelerometer axes, pending the six-face check. Rotation trials check gyro directions. This preview does not count as calibration trials.")}</p>
    <details><summary>{tr("技术详情：原始计数", "Technical details: raw counts")}</summary><p>{session?.sample.median_counts?.map(v => v.toFixed(1)).join(" / ") ?? "—"}</p><p>{tr("记录：每轴的中位数、样本数和原始接收时间窗口。", "Records: per-axis medians, sample count and the original receive-time window.")}</p></details>
    <h2>{tr("2. 确认人员并创建实验", "2. Confirm the operator and create the experiment")}</h2>
    <label>{tr("实验人员", "Operator")}<select aria-label={tr("实验人员", "Operator")} value={operator} onChange={e => setOperator(e.target.value)} disabled={locked}><option value="">{tr("请选择实际实验人员", "Select the actual operator")}</option>{operators.map(value => <option key={value}>{value}</option>)}</select></label>
    <label>{tr("实验备注", "Experiment notes")}<input maxLength={2000} value={notes} onChange={e => setNotes(e.target.value)} disabled={locked} /></label>
    <button className="primary" disabled={locked || !session?.ready || !operator || editing !== null} onClick={() => perform(async () => {
      const experiment = await request("/api/v1/calibration-experiments", "POST", { sensor_sn: sensorSn, operator_id: operator, directions: Object.fromEntries(Object.entries(session!.faces).map(([axis, face]) => [axis, face.description])), notes, orientation_session_id: session!.session_id });
      update(null); onCreated(experiment);
    })}>{tr("创建实验", "Create experiment")}</button>
    {(!session?.ready || !operator) && <p className="disabled-reason">{tr("记录六个方向并选择实验人员后，即可创建实验。", "Record all six directions and select the operator to create an experiment.")}</p>}
  </section>;
}
