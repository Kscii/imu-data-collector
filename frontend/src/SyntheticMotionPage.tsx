import { useEffect, useMemo, useState } from "react";
import { tr } from "./i18n";

type Label = { code: string; name?: string; is_fall?: boolean };
type Candidate = {
  candidate_id: string;
  version_id: string;
  source_dataset: string;
  source_member: string;
  published_at_utc: string;
  label_candidates: Label[];
  warning_flags: string[];
  decision: "unreviewed" | "pass" | "reject";
  revision: number;
};

const root = "/api/v1/synthetic";

async function request<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options?.headers ?? {}) },
  });
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(typeof payload.detail === "string" ? payload.detail : `HTTP ${response.status}`);
  }
  return payload as T;
}

export function SyntheticMotionPage() {
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [selected, setSelected] = useState<string>("");
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState("all");
  const [labelCode, setLabelCode] = useState("");
  const [labelName, setLabelName] = useState("");
  const [isFall, setIsFall] = useState(false);
  const [reason, setReason] = useState("");
  const [working, setWorking] = useState(false);
  const [error, setError] = useState("");
  const [snapshot, setSnapshot] = useState<{ snapshot_id: string; state: string } | null>(null);

  const refresh = async () => {
    const result = await request<{ candidates: Candidate[] }>(`${root}/candidates`);
    setCandidates(result.candidates);
    setSelected(previous => previous || (result.candidates[0]
      ? `${result.candidates[0].candidate_id}/${result.candidates[0].version_id}` : ""));
  };

  useEffect(() => { refresh().catch(err => setError(String(err))); }, []);
  useEffect(() => {
    if (!snapshot || snapshot.state !== "queued") return;
    const timer = window.setInterval(() => {
      request<{ result: { state: string } }>(`${root}/snapshots/${snapshot.snapshot_id}`)
        .then(value => setSnapshot(current => current ? { ...current, state: value.result.state } : null))
        .catch(err => setError(String(err)));
    }, 5000);
    return () => window.clearInterval(timer);
  }, [snapshot?.snapshot_id, snapshot?.state]);

  const visible = useMemo(() => candidates.filter(item => {
    const matches = `${item.source_dataset} ${item.source_member} ${item.candidate_id}`
      .toLowerCase().includes(search.toLowerCase());
    return matches && (filter === "all" || item.decision === filter);
  }), [candidates, filter, search]);
  const current = visible.find(item => `${item.candidate_id}/${item.version_id}` === selected) ?? visible[0];
  const base = current
    ? `${root}/candidates/${encodeURIComponent(current.candidate_id)}/${current.version_id}`
    : "";
  const labels = (current?.label_candidates ?? []).filter(item => item.code);
  useEffect(() => {
    setLabelCode(""); setLabelName(""); setReason("");
    setIsFall(Boolean(labels[0]?.is_fall));
  }, [current?.candidate_id, current?.version_id]);
  const suggested = labels.find(item => item.code === labelCode) ?? labels[0];
  const selectedLabel = (labelCode.trim() || suggested?.code)
    ? { code: labelCode.trim() || suggested!.code,
        name: labelName.trim() || (labelCode.trim() && labelCode.trim() !== suggested?.code ? labelCode.trim() : suggested?.name) || labelCode.trim() || suggested!.code,
        is_fall: isFall }
    : null;

  async function decide(decision: "pass" | "reject") {
    if (!current) return;
    setWorking(true); setError("");
    try {
      await request(`${base}/reviews`, {
        method: "POST",
        body: JSON.stringify({
          decision, expected_revision: current.revision,
          labels: decision === "pass" && selectedLabel ? [selectedLabel] : [],
          reason: decision === "reject" ? reason.trim() : null,
        }),
      });
      await refresh();
      const next = visible.find(item => item.decision === "unreviewed" && item.candidate_id !== current.candidate_id);
      if (next) setSelected(`${next.candidate_id}/${next.version_id}`);
      setReason("");
    } catch (err) { setError(String(err)); }
    finally { setWorking(false); }
  }

  async function createSnapshot() {
    setWorking(true); setError("");
    try {
      const value = await request<{ snapshot_id: string; state: string }>(`${root}/snapshots`, { method: "POST" });
      setSnapshot(value);
    } catch (err) { setError(String(err)); }
    finally { setWorking(false); }
  }

  return <main style={{ display: "grid", gridTemplateColumns: "minmax(260px, 22%) minmax(0, 1fr)", gap: 16, padding: 16 }}>
    <aside className="panel" style={{ maxHeight: "85vh", overflow: "auto" }}>
      <h2>{tr("合成运动审核", "Synthetic motion review")}</h2>
      <p>{candidates.length} {tr("条完整候选", "complete candidates")}</p>
      <input aria-label={tr("搜索候选", "Search candidates")} value={search} onChange={event => setSearch(event.target.value)} placeholder={tr("来源或动作 ID", "Source or clip ID")} />
      <select aria-label={tr("筛选审核状态", "Filter review state")} value={filter} onChange={event => setFilter(event.target.value)}>
        <option value="all">{tr("全部", "All")}</option><option value="unreviewed">{tr("未审核", "Unreviewed")}</option>
        <option value="pass">Pass</option><option value="reject">Reject</option>
      </select>
      <div style={{ display: "grid", gap: 6, marginTop: 12 }}>
        {visible.map(item => <button key={`${item.candidate_id}/${item.version_id}`} type="button"
          className={item.candidate_id === current?.candidate_id && item.version_id === current.version_id ? "active" : ""}
          onClick={() => { setSelected(`${item.candidate_id}/${item.version_id}`); setLabelCode(""); setLabelName(""); setIsFall(false); setReason(""); }}
          style={{ textAlign: "left", overflowWrap: "anywhere" }}>
          <strong>{item.source_dataset}</strong> · {item.decision}<br />{item.candidate_id}
        </button>)}
      </div>
    </aside>
    <section className="panel" style={{ minWidth: 0 }}>
      {error && <div className="error-banner" role="alert">{error}</div>}
      {current ? <>
        <h2>{current.candidate_id}</h2>
        <p>{current.source_member} · {tr("审核状态", "Review state")}: {current.decision} · revision {current.revision}</p>
        {current.warning_flags.length > 0 && <p className="warning-banner">QA: {current.warning_flags.join(", ")}</p>}
        <iframe key={base} title={tr("同源动作与 IMU 回放", "Motion and IMU replay")}
          src={`${base}/files/index.html`} style={{ width: "100%", height: "min(65vh, 760px)", border: "1px solid #555" }} />
        <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center", marginTop: 12 }}>
          {labels.length > 0 && <label>{tr("候选标签", "Suggested label")}
            <select value={suggested?.code ?? ""} onChange={event => {
              const item = labels.find(label => label.code === event.target.value);
              setLabelCode(item?.code ?? ""); setLabelName(item?.name ?? ""); setIsFall(Boolean(item?.is_fall));
            }}>
              {labels.map(item => <option key={item.code} value={item.code}>{item.name ?? item.code}</option>)}
            </select>
          </label>}
          <input aria-label={tr("活动代码", "Activity code")} value={labelCode} onChange={event => setLabelCode(event.target.value)} placeholder={suggested?.code ?? tr("活动代码", "Activity code")} />
          <input aria-label={tr("活动名称", "Activity name")} value={labelName} onChange={event => setLabelName(event.target.value)} placeholder={suggested?.name ?? tr("活动名称", "Activity name")} />
          <label><input type="checkbox" checked={isFall} onChange={event => setIsFall(event.target.checked)} />{tr("跌倒", "Fall")}</label>
          <button disabled={working || !selectedLabel} onClick={() => decide("pass")}>Pass</button>
          <input aria-label={tr("拒绝原因", "Reject reason")} value={reason} onChange={event => setReason(event.target.value)} placeholder={tr("拒绝原因", "Reject reason")} />
          <button disabled={working || !reason.trim()} onClick={() => decide("reject")}>Reject</button>
        </div>
      </> : <p>{tr("当前筛选下没有候选", "No candidates match this filter")}</p>}
      <hr />
      <button disabled={working || !candidates.some(item => item.decision === "pass")} onClick={createSnapshot}>
        {tr("创建当前通过条目的 HDF5 3.3 快照", "Create HDF5 3.3 snapshot from current passes")}
      </button>
      {snapshot && <p role="status">{snapshot.snapshot_id} · {snapshot.state}</p>}
    </section>
  </main>;
}
