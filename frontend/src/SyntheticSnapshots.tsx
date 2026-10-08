import { useEffect, useState } from "react";
import { syntheticRequest, syntheticRoot } from "./SyntheticMotionPage";
import { tr } from "./i18n";

type Summary = {
  published: number; unreviewed: number; passed: number; rejected: number;
  snapshot_eligible: number; label_pending: number;
  latest_published_at_utc: string | null; read_only?: boolean;
  preview_mode?: string | null;
};
type Snapshot = {
  snapshot_id: string; created_at_utc: string; candidate_count: number;
  result: {state: string; candidate_count?: number; message?: string; error?: string;
    shards?: {object_key: string; byte_length: number; sha256: string}[]};
};

export function SyntheticSnapshots() {
  const [summary, setSummary] = useState<Summary | null>(null);
  const [snapshots, setSnapshots] = useState<Snapshot[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [configurations,setConfigurations] = useState<{configuration_id:string;layout_id:string;profile_id:string;variant:string}[]>([]);
  const [configuration,setConfiguration] = useState("");
  const refresh = async () => {
    const [counts, list, configs] = await Promise.all([
      syntheticRequest<Summary>(`${syntheticRoot}/motions/summary`),
      syntheticRequest<{snapshots: Snapshot[]}>(`${syntheticRoot}/snapshots`),
      syntheticRequest<{configurations:typeof configurations}>(`${syntheticRoot}/motions/configurations`),
    ]);
    setSummary(counts); setSnapshots(list.snapshots);
    setConfigurations(configs.configurations);
    setConfiguration(old=>old||configs.configurations.find(c=>c.layout_id==="chest-1"&&c.variant==="ideal")?.configuration_id||"");
  };
  useEffect(() => { refresh().catch(error => setError(String(error))); }, []);
  useEffect(() => {
    if (!snapshots.some(item => ["queued", "running"].includes(item.result.state))) return;
    const timer = window.setInterval(() => refresh().catch(error => setError(String(error))), 5000);
    return () => window.clearInterval(timer);
  }, [snapshots]);
  const create = async () => {
    setBusy(true); setError("");
    try {
      await syntheticRequest(`${syntheticRoot}/motions/snapshots`, {method: "POST",body:JSON.stringify({configuration_id:configuration})});
      await refresh();
    } catch (error) { setError(String(error)); }
    finally { setBusy(false); }
  };
  return <section className="panel synthetic-snapshots">
    <div className="panel-title">{tr("合成 IMU · HDF5 3.3", "Synthetic IMU · HDF5 3.3")}</div>
    {error && <div className="error-banner">{error}</div>}
    {summary && <p>{tr("已发布", "Published")} {summary.published}
      {" · "}{tr("质量通过", "Quality passed")} {summary.passed}
      {" · "}{tr("标签待办", "Needs label")} {summary.label_pending}
      {" · "}{tr("可入快照", "Snapshot eligible")} {summary.snapshot_eligible}
      {summary.latest_published_at_utc && <> · {tr("最新到达", "Latest arrival")} {new Date(summary.latest_published_at_utc).toLocaleString()}</>}</p>}
    <div className="save-row">
      <select aria-label="快照 IMU 配置" value={configuration} onChange={e=>setConfiguration(e.target.value)}><option value="">选择 IMU 配置</option>{configurations.map(c=><option key={c.configuration_id} value={c.configuration_id}>{c.layout_id} · {c.profile_id} · {c.variant} · {c.configuration_id.slice(0,8)}</option>)}</select>
      <button className="primary" disabled={busy || !configuration || !summary?.snapshot_eligible || summary?.read_only || Boolean(summary?.preview_mode)}
        onClick={create}>{tr("创建合成训练快照", "Create synthetic training snapshot")}</button>
      <button disabled={busy} onClick={() => refresh().catch(error => setError(String(error)))}>{tr("刷新进度", "Refresh progress")}</button>
    </div>
    {snapshots.map(item => <article key={item.snapshot_id}>
      <strong>{item.snapshot_id}</strong> · {item.result.state}
      {" · "}{item.candidate_count} {tr("条", "clips")}
      {" · "}{new Date(item.created_at_utc).toLocaleString()}
      {(item.result.message || item.result.error) && <p>{item.result.message || item.result.error}</p>}
      {item.result.shards?.map(shard => {
        const filename = shard.object_key.split("/").at(-1) || "";
        return <p key={shard.object_key}><a href={`${syntheticRoot}/snapshots/${item.snapshot_id}/shards/${filename}`}>
          {filename} · {(shard.byte_length / (1024 * 1024)).toFixed(1)} MiB
        </a></p>;
      })}
    </article>)}
  </section>;
}
