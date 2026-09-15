import { useEffect, useState } from "react";
import { apiErrorMessage, tr, userVisibleMessage } from "./i18n";
import { DeviceConfigurationAdminPage } from "./DeviceConfigurationPages";
import { completedCount, directions, minimums, referenceAngle, suggestedTrial, type TrialSpec } from "./calibrationProtocol";
import "./calibration.css";

async function request<T = any>(path: string, method = "GET", body?: unknown): Promise<T> {
  const response = await fetch(path, { method, headers: { "Content-Type": "application/json" }, body: body === undefined ? undefined : JSON.stringify(body) });
  if (!response.ok) {
    const value = await response.json().catch(() => ({}));
    throw new Error(apiErrorMessage(value.detail, response.status, response.statusText));
  }
  return response.json();
}
const base = "/api/v1/calibration-experiments";
const number = (value: unknown) => typeof value === "number" ? value.toFixed(4) : "—";
const kindName = (kind: string) => kind === "accel" ? tr("加速度", "Acceleration") : tr("角速度", "Angular velocity");
const roleName = (role: string) => role === "fit" ? tr("拟合", "Fit") : tr("独立验证", "Independent validation");
const direction = (value: { axis: string; sign: number }) => `${value.sign > 0 ? "+" : "-"}${value.axis}`;

function warningText(code: string): string {
  if (code.startsWith("below_recommended_minimum:")) {
    const [, trial, count] = code.split(":");
    const [kind, role, axis] = trial.split("_");
    return `${kindName(kind)} · ${roleName(role)} · ${axis}: ${count} ${tr("次，低于建议数量", "trials; below the recommended minimum")}`;
  }
  const messages: Record<string, [string, string]> = {
    six_fitting_faces_needed_for_axis_mapping: ["需要六个方向的静态拟合数据才能推断轴映射。", "Six static fitting faces are needed to infer the axis mapping."],
    axis_mapping_ambiguous_check_physical_directions: ["轴映射不明确，请检查方向说明和摆放。", "Axis mapping is unclear. Check the physical directions and placement."],
    review_trial_warnings: ["部分试验有警告，请查看逐次结果。", "Some trials have warnings. Review their results."],
    interrupted_capture_review_raw_evidence: ["采集曾中断，请检查原始证据与未完成试验。", "Capture was interrupted. Review raw evidence and incomplete trials."],
    raw_source_unavailable: ["原始文件不可用。", "The raw file is unavailable."],
    no_measurement_samples: ["测量段没有样本。", "The measurement phase has no samples."],
    possible_sensor_saturation: ["原始值可能达到量程上限。", "Raw values may have reached the sensor limit."],
    invalid_device_clock_integral_unavailable: ["设备时间戳异常，无法计算角度积分。", "Device timestamps are invalid; an angle integral is unavailable."],
    possible_missing_samples: ["时间戳存在间隔，可能缺少样本。", "Timestamp gaps suggest missing samples."],
    stationary_bias_windows_unavailable: ["缺少旋转前后的静止数据。", "Stationary data before or after rotation is missing."],
    rotation_axis_or_direction_conflicts_with_shared_mapping: ["旋转轴或方向与当前轴映射不一致。", "The rotation axis or direction conflicts with the current mapping."],
  };
  const pair = messages[code];
  return pair ? tr(...pair) : code;
}

export function CalibrationReport({ report }: { report: any }) {
  const c = report.candidate;
  return <section className="panel calibration-report">
    <h2>{tr("候选配置与验证结果", "Candidate configuration and validation")}</h2>
    <p>{tr("加速度和角速度各使用一个三轴共用尺度。独立验证直接使用此配置的尺度与固定零偏。", "Acceleration and angular velocity each use one shared scale for all three axes. Independent validation uses these scales and fixed biases.")}</p>
    <div className="calibration-summary">
      <span>Acceleration: <strong>{number(c.accel_counts_per_g)}</strong> counts/g</span>
      <span>Gyroscope: <strong>{number(c.gyro_counts_per_dps)}</strong> counts/(°/s)</span>
      <span>{tr("状态：未验证", "Status: unverified")}</span>
    </div>
    <p>{tr("原始通道顺序（从 0 开始）", "Raw channel order (starting at 0)")}: {report.axis_mapping_available ? c.raw_axis_order.join(", ") : "—"} · {tr("正负号", "Signs")}: {report.axis_mapping_available ? c.axis_signs.join(", ") : "—"}</p>
    <p>{tr("加速度零偏", "Acceleration bias")}: {c.accel_counts_per_g != null ? c.accel_bias_counts.map(number).join(", ") : "—"} counts · {tr("陀螺仪零偏", "Gyroscope bias")}: {report.gyro_bias_available ? c.gyro_bias_counts.map(number).join(", ") : "—"} counts</p>
    <p>{tr("每轴加速度尺度（诊断）", "Acceleration scales by axis (diagnostic)")}: {Object.entries(report.accel_axis_counts_per_g).map(([axis, value]) => `${axis}: ${number(value)}`).join(" · ") || "—"}</p>
    <div className="calibration-directions">{directions.map(key => <span key={key}><strong>{key}</strong>: {report.directions[key]}</span>)}</div>
    <details open={report.warnings.length < 6}><summary>{tr("警告与不足项", "Warnings and missing trials")} ({report.warnings.length})</summary><ul>{report.warnings.map((warning: string, i: number) => <li key={i}>{warningText(warning)}</li>)}</ul></details>
    <div className="calibration-table-scroll"><table><thead><tr><th>#</th><th>{tr("试验", "Trial")}</th><th>{tr("用途", "Use")}</th><th>{tr("状态", "Status")}</th><th>{tr("样本", "Samples")}</th><th>{tr("结果 / 误差", "Result / error")}</th><th>{tr("警告", "Warnings")}</th></tr></thead><tbody>
      {report.trials.map((t: any, i: number) => <tr key={t.trial_id}><td>{i + 1}</td><td>{kindName(t.kind)} {direction(t)}</td><td>{roleName(t.role)}</td><td>{t.excluded ? tr("已排除", "Excluded") : t.status === "complete" ? tr("完成", "Complete") : tr("未完成", "Incomplete")}</td><td>{t.result.sample_count ?? "—"}</td><td>{t.kind === "accel" ? `${number(t.result.vector_error_g)} g` : `${number(t.result.angle_deg)}° / ${number(t.result.angle_error_deg)}°`}</td><td>{t.warnings.map(warningText).join(" ")}</td></tr>)}
    </tbody></table></div>
  </section>;
}

export function CalibrationExperimentPage({ sensorSn, allowedUnikeys, interactionBlocked, legacy }: { sensorSn: string; allowedUnikeys: string[]; interactionBlocked: boolean; legacy?: React.ReactNode }) {
  const [history, setHistory] = useState<any[]>([]);
  const [experiment, setExperiment] = useState<any>(null);
  const [operator, setOperator] = useState("");
  const [descriptions, setDescriptions] = useState<Record<string, string>>(Object.fromEntries(directions.map(key => [key, ""])));
  const [notes, setNotes] = useState("");
  const [spec, setSpec] = useState<TrialSpec>({ kind: "accel", role: "fit", axis: "X", sign: 1 });
  const [trialNotes, setTrialNotes] = useState("");
  const [report, setReport] = useState<any>(null);
  const [digest, setDigest] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [cloud, setCloud] = useState<any>(null);
  const [exclusionReason, setExclusionReason] = useState("");
  const selectedId = experiment?.experiment_id;
  const active = Boolean(experiment?.active);
  const running = experiment?.trials.some((trial: any) => trial.status === "running");
  const locked = busy || interactionBlocked;
  const currentTrial = experiment?.trials.find((trial: any) => trial.status === "running");
  const refresh = () => request<any[]>(base).then(setHistory);
  useEffect(() => { refresh().catch(e => setError(e.message)); request("/api/v1/cloud/status").then(setCloud).catch(() => {}); }, []);
  useEffect(() => {
    if (!selectedId) return;
    let alive = true;
    const update = () => request(`${base}/${selectedId}`).then(value => { if (alive) setExperiment(value); }).catch(e => { if (alive) setError(e.message); });
    const timer = window.setInterval(update, 700);
    return () => { alive = false; window.clearInterval(timer); };
  }, [selectedId]);
  const invoke = async (action: () => Promise<void>) => {
    setBusy(true); setError(""); setMessage("");
    try { await action(); } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  };
  const select = (value: any) => { setExperiment(value); setReport(null); setDigest(""); setSpec(suggestedTrial(value.trials) ?? spec); };
  const endpoint = `${base}/${selectedId}`;
  const capture = (command: string) => invoke(async () => { await request(`${endpoint}/${command}`, "POST"); setExperiment(await request(endpoint)); await refresh(); });
  const next = experiment ? suggestedTrial(experiment.trials) : null;
  const count = experiment ? completedCount(experiment.trials, spec) : 0;
  const phaseText = experiment?.phase === "settle" ? tr("保持静止，等待稳定", "Keep still and let the sensor settle") : experiment?.phase === "before" ? tr("保持静止，测量旋转前零偏", "Keep still; measuring the starting bias") : experiment?.phase === "after" ? tr("保持静止，测量旋转后零偏", "Keep still; measuring the ending bias") : currentTrial?.kind === "gyro" ? tr("现在旋转，完成后点击下方按钮", "Rotate now, then press the button below") : tr("保持静止，正在采样", "Keep still; collecting samples");

  return <main className="calibration-page">
    <section className="panel"><h2>{tr("引导式 IMU 标定实验", "Guided IMU calibration")}</h2><p>{tr("六面静态实验估计加速度尺度；已知角度旋转估计角速度尺度。先完成拟合，再使用独立试验验证。数量是最低建议，可以继续增加重复。", "Six static faces estimate the acceleration scale. Known-angle rotations estimate the gyroscope scale. Fit first, then use separate trials to validate. The suggested counts are minimums; you can add more trials.")}</p></section>
    {error && <div className="error-banner">{userVisibleMessage(error)}</div>}{message && <div className="panel">{message}</div>}
    <section className="panel"><label>{tr("已有实验", "Existing experiment")}<select disabled={locked || active} value={selectedId ?? ""} onChange={e => { const value = history.find(item => item.experiment_id === e.target.value); if (value) select(value); else setExperiment(null); }}><option value="">{tr("新建实验", "New experiment")}</option>{history.map(item => <option key={item.experiment_id} value={item.experiment_id}>{item.sensor_sn} · {item.operator_id} · {new Date(item.created_at_utc).toLocaleString()}</option>)}</select></label>
      {!experiment ? <><p>{tr("当前设备", "Selected device")}: <strong>{sensorSn || tr("请先在设备设置中选择设备", "Select a device in settings first")}</strong></p><label>{tr("实验人员", "Operator")}<select value={operator} onChange={e => setOperator(e.target.value)}><option value="">{tr("请选择实际实验人员", "Select the actual operator")}</option>{allowedUnikeys.map(value => <option key={value}>{value}</option>)}</select></label>
        <p>{tr("先按右手坐标系定义三个正方向，再描述六个方向对应的外壳特征。静态实验中，指定的正/负方向朝上；正向旋转遵循右手规则。", "Define the three positive axes using a right-handed coordinate system, then describe the six housing directions. For a static trial, point the selected signed direction upward. Positive rotation follows the right-hand rule.")}</p>
        <div className="calibration-directions">{directions.map(key => <label key={key}>{key}<input maxLength={200} value={descriptions[key]} onChange={e => setDescriptions({ ...descriptions, [key]: e.target.value })} placeholder={tr("外壳特征，例如接口面", "Housing feature, for example the connector face")} /></label>)}</div>
        <label>{tr("实验备注", "Experiment notes")}<input maxLength={2000} value={notes} onChange={e => setNotes(e.target.value)} /></label><button className="primary" disabled={locked || !sensorSn || !operator || directions.some(key => !descriptions[key].trim())} onClick={() => invoke(async () => { select(await request(base, "POST", { sensor_sn: sensorSn, operator_id: operator, directions: descriptions, notes })); await refresh(); })}>{tr("创建实验", "Create experiment")}</button></> : <><p><strong>{experiment.sensor_sn}</strong> · {experiment.operator_id} · {experiment.trials.length} {tr("次记录", "trial records")}</p><div className="calibration-directions">{directions.map(key => <span key={key}><strong>{key}</strong>: {experiment.directions[key]}</span>)}</div><div className="save-row"><button disabled={locked} onClick={() => capture(active ? "stop" : "start")}>{active ? tr("结束本段采集，可稍后继续", "Finish this capture; continue later") : tr("连接设备并开始 / 继续", "Connect and start / continue")}</button><button disabled={locked || active} onClick={() => invoke(async () => { const value = await request(`${endpoint}/report`, "POST"); setReport(value.report); setDigest(value.sha256); await refresh(); })}>{tr("计算并保存报告", "Calculate and save report")}</button><button disabled={locked || running} onClick={() => invoke(async () => { setReport(await request(`${endpoint}/preview`)); setDigest(""); })}>{tr("预览结果 / 核对轴映射", "Preview results / check axis mapping")}</button></div></>}
    </section>
    {experiment && <section className="panel"><h2>{tr("逐次实验", "Trials")}</h2><p>{tr("最低建议：六面静态 3 轮拟合 + 1 轮验证；各轴各方向 5 次 360° 拟合 + 2 次 720° 验证。", "Suggested minimum: three six-face fitting rounds and one validation round; five 360° fitting turns and two 720° validation turns for each axis and direction.")}</p><div className="save-row">
      <label>{tr("实验", "Experiment")}<select disabled={locked || running} value={spec.kind} onChange={e => setSpec({ ...spec, kind: e.target.value as TrialSpec["kind"] })}><option value="accel">{kindName("accel")}</option><option value="gyro">{kindName("gyro")}</option></select></label>
      <label>{tr("用途", "Use")}<select disabled={locked || running} value={spec.role} onChange={e => setSpec({ ...spec, role: e.target.value as TrialSpec["role"] })}><option value="fit">{roleName("fit")}</option><option value="validation">{roleName("validation")}</option></select></label>
      <label>{tr("方向", "Direction")}<select disabled={locked || running} value={direction(spec)} onChange={e => setSpec({ ...spec, axis: e.target.value[1] as TrialSpec["axis"], sign: e.target.value[0] === "+" ? 1 : -1 })}>{directions.map(key => <option key={key}>{key}</option>)}</select></label>
      <button disabled={locked || running || !next} onClick={() => next && setSpec(next)}>{next ? tr("选择下一项建议试验", "Select the next suggested trial") : tr("已完成建议数量，可继续追加", "Suggested counts reached; more trials are welcome")}</button>
    </div><p>{tr("已完成 / 最低建议", "Completed / suggested minimum")}: {count} / {minimums[`${spec.kind}_${spec.role}`]} · {experiment.directions[direction(spec)]}</p>
    <p>{spec.kind === "accel" ? tr("让指定方向竖直朝上。按开始后，先静止 5 秒，再测量 10 秒；完成后可休息或重新摆放。", "Point the selected direction straight upward. After Start, keep still for five seconds, then for ten seconds of measurement. Rest or change position after the trial ends.") : tr(`绕穿过设备中心的 ${spec.axis} 轴旋转 ${Math.abs(referenceAngle(spec)!)}°。开始后先静止 5 秒，看到旋转提示后按所选方向转动；完成后按按钮，再静止 5 秒。用角度标记或夹具确认圈数。`, `Rotate ${Math.abs(referenceAngle(spec)!)}° around the ${spec.axis} axis through the device centre. After Start, keep still for five seconds. Rotate in the selected direction when prompted, press Finish rotation, then keep still for five seconds. Use angle marks or a fixture to check the turn count.`)}</p>
    <label>{tr("本次备注", "Trial notes")}<input disabled={locked || running} value={trialNotes} maxLength={2000} onChange={e => setTrialNotes(e.target.value)} /></label>
    {running ? <div className="calibration-phase" role="status" aria-live="polite"><strong>{phaseText}</strong>{experiment.remaining_seconds != null && <span>{Math.ceil(experiment.remaining_seconds)} s</span>}{experiment.phase === "measure" && currentTrial?.kind === "gyro" && <button className="primary" disabled={locked} onClick={() => capture("rotation-finished")}>{tr("旋转完成", "Finish rotation")}</button>}<button disabled={locked} onClick={() => capture("cancel-trial")}>{tr("中止本次，保留记录", "Cancel trial and keep its record")}</button></div> : <button className="primary" disabled={locked || !active} onClick={() => invoke(async () => { setReport(null); setDigest(""); setExperiment(await request(`${endpoint}/trials`, "POST", { ...spec, notes: trialNotes })); })}>{tr("开始本次试验", "Start this trial")}</button>}
    <details><summary>{tr("全部操作记录与排除说明", "All trial records and exclusions")}</summary><label>{tr("排除或恢复的原因", "Reason for excluding or restoring a trial")}<input value={exclusionReason} maxLength={1000} onChange={e => setExclusionReason(e.target.value)} /></label>{experiment.trials.map((trial: any, i: number) => <p key={trial.trial_id}>#{i + 1} · {kindName(trial.kind)} {direction(trial)} · {roleName(trial.role)} · {trial.status} · {trial.notes} {trial.exclusion_reason && `· ${trial.exclusion_reason}`} <button disabled={locked || active || !exclusionReason.trim()} onClick={() => invoke(async () => { setExperiment(await request(`${endpoint}/trials/${trial.trial_id}/exclusion`, "PUT", { excluded: !trial.excluded, reason: exclusionReason })); setReport(null); setDigest(""); })}>{trial.excluded ? tr("恢复使用", "Include again") : tr("排除但保留原始记录", "Exclude and keep the raw record")}</button></p>)}</details>
    </section>}
    {report && <><CalibrationReport report={report} />{!digest && <p>{tr("当前为预览；结束本段采集并保存报告后可导出或上传。", "This is a preview. Finish the capture and save a report before exporting or uploading.")}</p>}</>}
    {experiment && !active && <section className="panel"><h2>{tr("保存与共享", "Save and share")}</h2><div className="save-row"><a className="button-link" href={`${endpoint}/candidate.yaml`}>{tr("下载候选 YAML", "Download candidate YAML")}</a><button disabled={locked} onClick={() => invoke(async () => { await request(`${endpoint}/copy-to-workspace`, "POST"); setMessage(tr("已复制到本地配置工作区，状态仍为未验证。", "Copied to the local configuration workspace; it remains unverified.")); })}>{tr("复制到配置工作区", "Copy to configuration workspace")}</button><button disabled={locked || experiment.upload?.state === "uploading"} onClick={() => capture("upload")}>{tr("上传实验 / 重试", "Upload experiment / retry")}</button><button disabled={locked} onClick={() => invoke(async () => { const value = await request("/api/v1/cloud/oauth/start", "POST"); window.open(value.authorization_url, "_blank", "noopener"); })}>{tr("Google 登录", "Sign in with Google")}</button></div><p>{tr("上传使用已有团队账号；离线实验无需登录。", "Upload uses your existing team account. Offline experiments do not require sign-in.")} {cloud?.logged_in ? tr("当前已登录。", "Currently signed in.") : ""}</p>{experiment.upload && <p role="status">{experiment.upload.state} · {number((experiment.upload.completed_bytes ?? 0) / 1048576)} / {number((experiment.upload.total_bytes ?? 0) / 1048576)} MiB {experiment.upload.error && userVisibleMessage(experiment.upload.error)}</p>}
      <details><summary>{tr("已保存的报告版本", "Saved report versions")}</summary>{experiment.reports.map((item: any) => <p key={item.sha256}><a href={`${endpoint}/reports/${item.sha256}`} download>{new Date(item.created_at_utc).toLocaleString()} · {item.sha256.slice(0, 12)} · JSON</a></p>)}</details>
    </section>}
    {legacy && !active && <details><summary>{tr("旧版表征工具与历史报告", "Legacy characterization and historical reports")}</summary>{legacy}</details>}
  </main>;
}

export function CalibrationDeviceManagement({ canManage }: { canManage: boolean }) {
  const [tab, setTab] = useState("configuration");
  return <><nav><button className={tab === "configuration" ? "active" : ""} onClick={() => setTab("configuration")}>{tr("设备配置", "Device configuration")}</button><button className={tab === "experiments" ? "active" : ""} onClick={() => setTab("experiments")}>{tr("标定实验", "Calibration experiments")}</button></nav>{tab === "configuration" ? <DeviceConfigurationAdminPage canManage={canManage} /> : <CloudCalibrationExperiments />}</>;
}

function CloudCalibrationExperiments() {
  const [items, setItems] = useState<any[]>([]);
  const [selected, setSelected] = useState<any>(null);
  const [report, setReport] = useState<any>(null);
  const [error, setError] = useState("");
  const refresh = () => request<any[]>(base).then(values => setItems(values.sort((a, b) => (b.uploaded_at_utc ?? b.created_at_utc).localeCompare(a.uploaded_at_utc ?? a.created_at_utc)))).catch(e => setError(e.message));
  useEffect(() => { refresh(); }, []);
  return <main className="calibration-page"><section className="panel"><h2>{tr("团队标定实验", "Team calibration experiments")}</h2><button onClick={refresh}>{tr("刷新", "Refresh")}</button>{error && <p className="error-banner">{userVisibleMessage(error)}</p>}{items.length === 0 && <p>{tr("尚无已完成上传的实验。", "No experiment uploads have been completed yet.")}</p>}{items.map(item => <p key={`${item.experiment_id}/${item.report_sha256}`}><button onClick={async () => { setError(""); setReport(null); setSelected(item); try { setReport(await request(`${base}/${item.experiment_id}/reports/${item.report_sha256}`)); } catch (e) { setError((e as Error).message); } }}>{item.sensor_sn} · {item.operator_id} · {new Date(item.uploaded_at_utc ?? item.created_at_utc).toLocaleString()} · {item.report_sha256.slice(0, 12)}</button></p>)}</section>{report && <CalibrationReport report={report} />}{selected && <section className="panel"><h2>{tr("下载实验文件", "Download experiment files")}</h2><p>{tr("实验人员", "Operator")}: {selected.operator_id} · {tr("上传人员", "Uploaded by")}: {selected.uploader}</p>{selected.files.map((file: any) => <p key={file.file_id}><a href={`${base}/${selected.experiment_id}/reports/${selected.report_sha256}/files/${file.file_id}`}>{file.file_id}</a> · {number(file.size_bytes / 1048576)} MiB</p>)}</section>}</main>;
}
