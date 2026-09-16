import { useEffect, useState } from "react";
import { type Catalog, syntheticRequest, syntheticRoot } from "./SyntheticMotionPage";
import { tr } from "./i18n";

type Preview = {
  rule_id: string; matched_count: number;
  sample_candidates: {candidate_id: string; version_id: string}[];
};

export function SyntheticLabelManagement({isAdmin}: {isAdmin: boolean}) {
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [section, setSection] = useState<"concepts" | "scopes" | "mappings">("concepts");
  const [code, setCode] = useState(""); const [name, setName] = useState("");
  const [isFall, setIsFall] = useState(false);
  const [origin, setOrigin] = useState("babel-1.0");
  const [sourceValue, setSourceValue] = useState("");
  const [sourceDataset, setSourceDataset] = useState("");
  const [targetCode, setTargetCode] = useState("");
  const [preview, setPreview] = useState<Preview | null>(null);
  const [selectedSample, setSelectedSample] = useState(0);
  const [checked, setChecked] = useState<string[]>([]);
  const [error, setError] = useState("");
  const load = () => syntheticRequest<Catalog>(`${syntheticRoot}/labels`)
    .then(setCatalog).catch(error => setError(String(error)));
  useEffect(() => { load(); }, []);
  const addConcept = async () => {
    setError("");
    try {
      await syntheticRequest(`${syntheticRoot}/labels/concepts`, {method: "POST",
        body: JSON.stringify({code, name, is_fall: isFall})});
      setCode(""); setName(""); await load();
    } catch (error) { setError(String(error)); }
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
  return <section className="panel synthetic-label-management">
    <div className="panel-title">{tr("合成运动标签管理", "Synthetic motion label management")}</div>
    {error && <div className="error-banner">{error}</div>}
    <nav className="synthetic-tabs">
      <button className={section === "concepts" ? "active" : ""} onClick={() => setSection("concepts")}>{tr("概念库", "Concepts")}</button>
      <button className={section === "scopes" ? "active" : ""} onClick={() => setSection("scopes")}>{tr("适用场景", "Scopes")}</button>
      <button className={section === "mappings" ? "active" : ""} onClick={() => setSection("mappings")}>{tr("自动映射", "Mappings")}</button>
    </nav>
    {section === "concepts" && <>
      <p>{tr("共用概念由真实 IMU 标签管理维护；动捕专用概念不会出现在真实 IMU 标注选择器。", "Shared concepts come from the real IMU taxonomy; motion-only concepts stay out of the real IMU picker.")}</p>
      <div className="synthetic-concept-list">{catalog?.concepts.map(item =>
        <span key={item.code}>{item.name} <small>{item.code} · {item.scope}</small></span>)}</div>
      {isAdmin && <div className="synthetic-form">
        <input value={code} onChange={event => setCode(event.target.value)} placeholder="code" />
        <input value={name} onChange={event => setName(event.target.value)} placeholder={tr("名称", "Name")} />
        <label><input type="checkbox" checked={isFall} onChange={event => setIsFall(event.target.checked)} />{tr("跌倒", "Fall")}</label>
        <button disabled={!code || !name} onClick={addConcept}>{tr("新增动捕概念", "Add motion concept")}</button>
      </div>}
    </>}
    {section === "scopes" && <p>{tr(
      "真实 IMU 保持现有跌倒与日常活动集合；合成运动可使用共用概念和动捕专用概念。两个页面使用同一代码和显示名称，历史标签版本不改写。",
      "Real IMU keeps its fall and daily-activity set. Synthetic motion can use shared and motion-only concepts. Both pages use the same codes and names; history is unchanged.")}</p>}
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
        <input value={sourceDataset} onChange={event => setSourceDataset(event.target.value)}
          placeholder={tr("限定来源（可选）", "Dataset (optional)")} />
        <input value={sourceValue} onChange={event => setSourceValue(event.target.value)}
          placeholder={tr("来源类别精确值", "Exact source category")} />
        <select value={targetCode} onChange={event => setTargetCode(event.target.value)}>
          <option value="">{tr("目标正式标签", "Target formal label")}</option>
          {catalog?.concepts.filter(item => item.active && !item.is_fall).map(item =>
            <option key={item.code} value={item.code}>{item.name}</option>)}
        </select>
        <button disabled={!sourceValue || !targetCode} onClick={addRule}>{tr("创建草稿规则", "Create draft rule")}</button>
      </div>}
    </>}
  </section>;
}
