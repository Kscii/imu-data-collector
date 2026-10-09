import {useEffect, useState} from "react";
import {motionApi} from "./ProductionMotionPage";

type Model = {model_id: string; release_kind: string; validation?: unknown};
type Registry = {models: Model[]; active: {model: Model} | null; active_sha256: string | null;
  batch_control?: {mode: string; deadline_unix?: number} | null; batch_control_sha256?: string | null;
  jobs: {state: string; count: number}[]; discovery_errors?: number;
  timing?: {completed: number; seconds: number; mean_seconds: number}};
type Configuration = {configuration_id: string; layout_id: string; profile_id: string; variant: string};
type Snapshot = {snapshot_id: string; candidate_count: number; state: string;
  result: {shards?: {object_key: string}[]; error?: string} | null};
const root = "/api/v1/synthetic/motions";

export function MotionModelPanel({onClose}: {onClose: () => void}) {
  const [registry,setRegistry]=useState<Registry|null>(null),[configurations,setConfigurations]=useState<Configuration[]>([]);
  const [configuration,setConfiguration]=useState(""),[snapshots,setSnapshots]=useState<Snapshot[]>([]);
  const [error,setError]=useState(""),[notice,setNotice]=useState(""),[busy,setBusy]=useState(false);
  const [previews,setPreviews]=useState<{preview_id:string;title:string}[]>([]);
  const refresh=async()=>{
    const [models,configs,exports,comparisons]=await Promise.all([motionApi<Registry>(root+"/models"),
      motionApi<{configurations:Configuration[]}>(root+"/configurations"),motionApi<{snapshots:Snapshot[]}>(root+"/snapshots"),motionApi<{previews:{preview_id:string;title:string}[]}>(root+"/previews")]);
    setPreviews(comparisons.previews);setRegistry(models);setConfigurations(configs.configurations);setSnapshots(exports.snapshots);
    setConfiguration(old=>old || configs.configurations.find(c=>c.layout_id==="chest-1"&&c.variant==="ideal")?.configuration_id || "");
  };
  useEffect(()=>{const update=()=>void refresh().catch(e=>setError(String(e)));update();const timer=setInterval(update,10000);return()=>clearInterval(timer);},[]);
  const act=async(call:()=>Promise<unknown>,message:string)=>{
    setBusy(true);setError("");try{await call();await refresh();setNotice(message);}catch(e){setError(String(e));}finally{setBusy(false);}
  };
  return <div className="primary-overlay"><section role="dialog" aria-modal="true" aria-label="模型与快照" className="primary-details panel">
    <header><strong>生产模型与快照</strong><button onClick={onClose}>关闭</button></header><div className="primary-details-scroll">
    {error&&<p role="alert" className="primary-error">{error}</p>}{notice&&<p role="status">{notice}</p>}
    {previews.length>0&&<><h3>候选预览 · 只读</h3>{previews.map(p=><p key={p.preview_id}><a href={`?view=synthetic&lang=zh-CN&motion_preview=${encodeURIComponent(p.preview_id)}`}>{p.title}</a></p>)}</>}
    <h3>正式模型</h3><p>当前：{registry?.active?.model.model_id??"尚未启用"}</p>
    <p>模型启用与批处理分开控制；人工草稿和已通过版本保留，新结果作为差异建议。</p>
    {registry?.models.map(m=><div className="primary-model" key={m.model_id}><strong>{m.model_id}</strong>
      <small>{m.release_kind==="existing-baseline"?"现有基线；独立参考评估待完成":"候选模型；启用时核对独立验证门槛"}</small>
      <button disabled={busy||registry.active?.model.model_id===m.model_id} onClick={()=>void act(()=>motionApi(root+"/models/"+m.model_id+"/activate",{expected_active_sha256:registry.active_sha256}),"正式模型已启用；批处理按独立计划执行。")}>{registry.active?.model.model_id===m.model_id?"当前模型":"启用此模型"}</button>
      {m.validation!=null&&<details><summary>验收报告</summary><pre>{JSON.stringify(m.validation,null,2)}</pre></details>}</div>)}
    <p>批处理：{({sample:"100 条试跑",full:"有时限全量",incremental:"增量处理",paused:"已暂停"} as Record<string,string>)[registry?.batch_control?.mode ?? "paused"]}{registry?.batch_control?.deadline_unix ? ` · 截止 ${new Date(registry.batch_control.deadline_unix*1000).toLocaleString()}` : ""}</p>
    {registry?.batch_control && registry.batch_control.mode!=="paused" && <button disabled={busy} onClick={()=>void act(()=>motionApi(root+"/batch",{control:{schema:"imu.motion_batch.v1",mode:"paused"},expected_control_sha256:registry.batch_control_sha256}),"批处理已暂停领取新任务。")}>暂停批处理</button>}
    <p>{registry?.jobs.map(j=>(({queued:"排队",running:"运行",complete:"完成",failed:"失败",superseded:"已替代"} as Record<string,string>)[j.state]??j.state)+" "+j.count).join(" · ")||"暂无推理任务"}</p>
    {!!registry?.discovery_errors&&<p role="alert" className="primary-error">{registry.discovery_errors} 个数据登记错误，已隔离；请检查后台错误记录。</p>}
    {registry?.timing && registry.timing.completed>0&&<p>已测 {registry.timing.completed} 条后台任务，平均 {registry.timing.mean_seconds.toFixed(2)} 秒／条（含结果保存，不含首次特征准备）。</p>}
    {!!registry?.jobs.some(j=>j.state==="failed")&&<button disabled={busy} onClick={()=>void act(()=>motionApi(root+"/models/retry-failed",{}),"失败任务已重新排队。")}>重试失败任务</button>}
    <h3>增量训练快照 · HDF5 3.3</h3><p>冻结当前已通过录制的可用区间；未知区间继续屏蔽。同一快照只选择一种 IMU 配置。</p>
    <select aria-label="快照IMU配置" value={configuration} onChange={e=>setConfiguration(e.target.value)}><option value="">选择配置</option>
      {configurations.map(c=><option key={c.configuration_id} value={c.configuration_id}>{c.layout_id} · {c.profile_id} · {c.variant} · {c.configuration_id.slice(0,8)}</option>)}</select>
    <button disabled={busy||!configuration} onClick={()=>void act(()=>motionApi(root+"/snapshots",{configuration_id:configuration}),"增量快照请求已冻结。")}>生成当前配置快照</button>
    {snapshots.map(s=><p key={s.snapshot_id}>{s.candidate_count} 条 · {s.state==="complete"?"已完成":s.state==="failed"?"失败":"等待构建"} {s.result?.error}
      {s.result?.shards?.map(f=><a key={f.object_key} href={"/api/v1/synthetic/snapshots/"+s.snapshot_id+"/shards/"+f.object_key.split("/").pop()}>{f.object_key.split("/").pop()}</a>)}</p>)}
    </div></section></div>;
}
