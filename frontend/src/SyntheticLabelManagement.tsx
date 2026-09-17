import { useEffect, useState } from "react";
import { type Catalog, syntheticRequest, syntheticRoot } from "./SyntheticMotionPage";
import { tr } from "./i18n";

type Preview = {
  rule_id: string; matched_count: number;
  sample_candidates: {candidate_id: string; version_id: string}[];
};
type MappingOptions = {datasets: {name: string; count: number}[];
  values: {value: string; count: number}[]};
type Estimate = {matched_count: number; sample_candidates: {candidate_id: string}[]};

export function SyntheticLabelManagement({isAdmin, section}: {
  isAdmin: boolean; section: "concepts" | "mappings";
}) {
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [code, setCode] = useState(""); const [name, setName] = useState("");
  const [isFall, setIsFall] = useState(false);
  const [editCode, setEditCode] = useState("");
  const [editName, setEditName] = useState("");
  const [editActive, setEditActive] = useState(true);
  const [origin, setOrigin] = useState("babel-1.0");
  const [sourceValue, setSourceValue] = useState("");
  const [sourceDataset, setSourceDataset] = useState("");
  const [targetCode, setTargetCode] = useState("");
  const [preview, setPreview] = useState<Preview | null>(null);
  const [selectedSample, setSelectedSample] = useState(0);
  const [checked, setChecked] = useState<string[]>([]);
  const [options, setOptions] = useState<MappingOptions>({datasets: [], values: []});
  const [estimate, setEstimate] = useState<Estimate | null>(null);
  const [error, setError] = useState("");
  const load = () => syntheticRequest<Catalog>(`${syntheticRoot}/labels`)
    .then(setCatalog).catch(error => setError(String(error)));
  useEffect(() => { load(); }, []);
  useEffect(() => {
    if (section !== "mappings") return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      const query = new URLSearchParams({origin, source_dataset: sourceDataset,
        search: sourceValue, limit: "40"});
      void syntheticRequest<MappingOptions>(`${syntheticRoot}/labels/mapping-options?${query}`,
        {signal: controller.signal}).then(setOptions).catch(reason => {
          if (!controller.signal.aborted) setError(String(reason));
        });
    }, 180);
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, [section, origin, sourceDataset, sourceValue]);
  useEffect(() => {
    if (section !== "mappings" || !sourceValue.trim() || !targetCode) { setEstimate(null); return; }
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      void syntheticRequest<Estimate>(`${syntheticRoot}/labels/mappings/estimate`, {
        method: "POST", signal: controller.signal,
        body: JSON.stringify({origin, source_value: sourceValue,
          source_dataset: sourceDataset || null, target_code: targetCode || ""}),
      }).then(setEstimate).catch(reason => {
        if (!controller.signal.aborted) setError(String(reason));
      });
    }, 280);
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, [section, origin, sourceDataset, sourceValue, targetCode]);
  const addConcept = async () => {
    setError("");
    try {
      await syntheticRequest(`${syntheticRoot}/labels/concepts`, {method: "POST",
        body: JSON.stringify({code, name, is_fall: isFall})});
      setCode(""); setName(""); await load();
    } catch (error) { setError(String(error)); }
  };
  const updateConcept = async () => {
    if (!editCode || !catalog) return;
    setError("");
    try {
      await syntheticRequest(`${syntheticRoot}/labels/concepts/${encodeURIComponent(editCode)}`, {
        method: "PATCH", body: JSON.stringify({expected_revision: catalog.motion_revision,
          name: editName, active: editActive}),
      });
      setEditCode(""); await load();
    } catch (reason) { setError(String(reason)); }
  };
  const addRule = async () => {
    setError("");
    try {
      await syntheticRequest(`${syntheticRoot}/labels/mappings`, {method: "POST",
        body: JSON.stringify({origin, source_value: sourceValue,
          source_dataset: sourceDataset || null, target_code: targetCode})});
      setSourceValue(""); await load();
    } catch (error) { setError(String(error)); }
  };
  const showPreview = async (ruleId: string) => {
    setError("");
    try {
      setPreview(await syntheticRequest(`${syntheticRoot}/labels/mappings/${ruleId}/preview`));
      setSelectedSample(0); setChecked([]);
    } catch (error) { setError(String(error)); }
  };
  const activate = async () => {
    if (!preview) return;
    setError("");
    try {
      await syntheticRequest(`${syntheticRoot}/labels/mappings/${preview.rule_id}/state`, {
        method: "POST", body: JSON.stringify({state: "active", checked_candidate_ids: checked}),
      });
      setPreview(null); await load();
    } catch (error) { setError(String(error)); }
  };
  const sample = preview?.sample_candidates[selectedSample];
  return <section id={section === "concepts" ? "motion-only-concepts" : undefined}
    className="panel synthetic-label-management">
    <div className="panel-title">{section === "concepts"
      ? tr("动捕专用概念", "Motion-only concepts") : tr("合成运动自动映射", "Synthetic motion mappings")}</div>
    {error && <div className="error-banner">{error}</div>}
    {section === "concepts" && <>
      <p>{tr("这里维护仅用于动捕合成的概念；共用概念在上方真实 IMU 概念区维护。历史标签不会改写。", "Manage motion-only concepts here; shared concepts are managed above. Historical labels remain unchanged.")}</p>
      <div className="synthetic-concept-list">{catalog?.concepts.filter(item => item.scope === "motion")
        .map(item => <button key={item.code} className={!item.active ? "inactive" : ""}
          onClick={() => { setEditCode(item.code); setEditName(item.name); setEditActive(item.active); }}>
          {item.name} <small>{item.code} · {item.active ? tr("启用", "Active") : tr("停用", "Inactive")}</small>
        </button>)}</div>
      {isAdmin && editCode && <div className="synthetic-form synthetic-concept-edit">
        <strong>{tr("编辑", "Edit")} {editCode}</strong>
        <input aria-label={tr("概念名称", "Concept name")} value={editName}
          onChange={event => setEditName(event.target.value)} />
        <label><input type="checkbox" checked={editActive}
          onChange={event => setEditActive(event.target.checked)} />{tr("启用", "Active")}</label>
        <button disabled={!editName.trim()} onClick={updateConcept}>{tr("保存概念", "Save concept")}</button>
        <button onClick={() => setEditCode("")}>{tr("取消", "Cancel")}</button>
      </div>}
      {isAdmin && <div className="synthetic-form">
        <input value={code} onChange={event => setCode(event.target.value)} placeholder="code" />
        <input value={name} onChange={event => setName(event.target.value)} placeholder={tr("名称", "Name")} />
        <label><input type="checkbox" checked={isFall} onChange={event => setIsFall(event.target.checked)} />{tr("跌倒", "Fall")}</label>
        <button disabled={!code || !name} onClick={addConcept}>{tr("新增动捕概念", "Add motion concept")}</button>
      </div>}
    </>}
    {section === "mappings" && <>
      <p>{tr("来源词只作证据；规则须精确命中单一整段动作，预览并抽检后才能启用。", "Source words are evidence only. Rules must match one whole-clip action exactly and require sample review before activation.")}</p>
      {catalog?.rules.map(rule => <article key={rule.rule_id}>
        <strong>{rule.origin} · {rule.source_dataset || tr("全部来源", "All sources")} · {rule.source_value} → {rule.target_code}</strong>
        {" · "}{rule.state} <button onClick={() => showPreview(rule.rule_id)}>{tr("预览", "Preview")}</button>
      </article>)}
      {preview && <div className="panel synthetic-preview">
        <strong>{tr("命中", "Matched")} {preview.matched_count}</strong>
        <p>{tr("逐条打开并核对展示样本，然后勾选。", "Open and inspect each displayed sample before checking it.")}</p>
        {sample && <iframe title={tr("映射抽检回放", "Mapping sample replay")}
          src={`${syntheticRoot}/candidates/${encodeURIComponent(sample.candidate_id)}/${sample.version_id}/files/index.html`}
          className="synthetic-frame" />}
        <div className="synthetic-sample-list">{preview.sample_candidates.map((item, index) =>
          <label key={item.candidate_id} className={index === selectedSample ? "active" : ""}>
            <input type="checkbox" checked={checked.includes(item.candidate_id)}
              onChange={event => setChecked(previous => event.target.checked
                ? [...previous, item.candidate_id] : previous.filter(id => id !== item.candidate_id))} />
            <button onClick={() => setSelectedSample(index)}>{tr("样本", "Sample")} {index + 1} · {item.candidate_id}</button>
          </label>)}</div>
        {isAdmin && <button className="primary" disabled={checked.length === 0 || checked.length !== preview.sample_candidates.length}
          onClick={activate}>{tr("发布映射版本", "Publish mapping version")}</button>}
      </div>}
      {isAdmin && <div className="synthetic-form">
        <select value={origin} onChange={event => setOrigin(event.target.value)}>
          <option value="babel-1.0">BABEL act_cat</option>
          <option value="stageii-source-member">GRAB/SOMA action</option>
        </select>
        <input value={sourceDataset} list="synthetic-source-datasets"
          onChange={event => setSourceDataset(event.target.value)}
          placeholder={tr("限定来源（可选，可选或输入）", "Dataset (optional; choose or type)")} />
        <datalist id="synthetic-source-datasets">{options.datasets.map(item =>
          <option key={item.name} value={item.name}>{item.count}</option>)}</datalist>
        <input value={sourceValue} list="synthetic-source-values"
          onChange={event => setSourceValue(event.target.value)}
          placeholder={tr("来源类别：从观测值选或手动输入", "Source value: choose observed or type")} />
        <datalist id="synthetic-source-values">{options.values.map(item =>
          <option key={item.value} value={item.value}>{item.count}</option>)}</datalist>
        <select value={targetCode} onChange={event => setTargetCode(event.target.value)}>
          <option value="">{tr("目标正式标签", "Target formal label")}</option>
          {catalog?.concepts.filter(item => item.active && !item.is_fall).map(item =>
            <option key={item.code} value={item.code}>{item.name}</option>)}
        </select>
        <button disabled={!sourceValue || !targetCode} onClick={addRule}>{tr("创建草稿规则", "Create draft rule")}</button>
      </div>}
      {isAdmin && <div className="synthetic-mapping-evidence">
        <strong>{tr("已观测来源值", "Observed source values")}</strong>
        <div>{options.values.slice(0, 16).map(item => <button key={item.value}
          onClick={() => setSourceValue(item.value)}>{item.value} <small>× {item.count}</small></button>)}</div>
        {sourceValue && <p>{tr("当前精确匹配", "Current exact matches")}: {estimate?.matched_count ?? "…"}
          {estimate?.matched_count === 0 && ` · ${tr("没有匹配候选，请检查来源或拼写", "No matches; check source or spelling")}`}</p>}
      </div>}
    </>}
  </section>;
}
