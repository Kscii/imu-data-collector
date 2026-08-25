import { useEffect, useMemo, useRef, useState } from "react";
import Plot from "./Plot";

type Recording = {
  recording_id: string;
  collection_id: string;
  participant_id: string;
  state: string;
  started_at_utc: string;
  duration_ns?: number;
  issues: string[];
  upload_state: string;
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

type AnnotationDocument = {
  taxonomy_id: string;
  taxonomy_version: string;
  revision: number;
  finalized: boolean;
  segments: Segment[];
  events: Event[];
};

type Taxonomy = {
  taxonomy_id: string;
  version: string;
  fall: { code: string; display_name_zh: string }[];
  non_fall: { code: string; display_name_zh: string }[];
};

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) }
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.detail ?? `${response.status} ${response.statusText}`);
  }
  return response.json();
}

function seconds(ns?: number | null) {
  return ns == null ? "—" : `${(ns / 1e9).toFixed(2)} s`;
}

function stateLabel(state: string) {
  const labels: Record<string, string> = {
    idle: "空闲",
    arming: "正在准备",
    recording: "正在录制",
    finalizing: "正在收尾",
    ready: "可用",
    needs_attention: "需要检查",
    failed: "失败"
  };
  return labels[state] ?? state;
}

export default function App() {
  const [tab, setTab] = useState<"capture" | "annotate" | "library">("capture");
  const [live, setLive] = useState<any>({ state: "idle", imu: {}, video: {} });
  const [participant, setParticipant] = useState("xfan0282");
  const [collection, setCollection] = useState("pilot_v1");
  const [error, setError] = useState("");
  const [recordings, setRecordings] = useState<Recording[]>([]);
  const [taxonomy, setTaxonomy] = useState<Taxonomy | null>(null);
  const liveRef = useRef<{ t: number[]; values: number[][] }>({ t: [], values: [] });
  const [, redraw] = useState(0);

  const refreshRecordings = () =>
    api<Recording[]>("/api/v1/recordings").then(setRecordings).catch((e) => setError(e.message));

  useEffect(() => {
    api<Taxonomy>("/api/v1/taxonomy").then(setTaxonomy).catch((e) => setError(e.message));
    refreshRecordings();
    const protocol = location.protocol === "https:" ? "wss" : "ws";
    const socket = new WebSocket(`${protocol}://${location.host}/api/v1/live`);
    socket.onmessage = (message) => {
      const payload = JSON.parse(message.data);
      setLive(payload);
      if (payload.state === "recording" && payload.imu?.raw) {
        const data = liveRef.current;
        data.t.push(performance.now() / 1000);
        data.values.push(payload.imu.raw);
        if (data.t.length > 240) {
          data.t.shift();
          data.values.shift();
        }
        redraw((value) => value + 1);
      }
    };
    return () => socket.close();
  }, []);

  const start = async () => {
    setError("");
    try {
      await api("/api/v1/recordings/start", {
        method: "POST",
        body: JSON.stringify({
          collection_id: collection,
          participant_id: participant,
          body_location: "chest",
          protocol_id: taxonomy?.taxonomy_id ?? "fall_binary_v1"
        })
      });
      liveRef.current = { t: [], values: [] };
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const stop = async () => {
    setError("");
    try {
      await api("/api/v1/recordings/stop", { method: "POST" });
      refreshRecordings();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <div className="app-shell">
      <header>
        <div>
          <span className="eyebrow">CW12EU-T · 本机采集</span>
          <h1>IMU 数采平台</h1>
        </div>
        <div className={`state state-${live.state}`}>{stateLabel(live.state)}</div>
      </header>
      <nav>
        <button className={tab === "capture" ? "active" : ""} onClick={() => setTab("capture")}>采集</button>
        <button className={tab === "annotate" ? "active" : ""} onClick={() => setTab("annotate")}>标注</button>
        <button className={tab === "library" ? "active" : ""} onClick={() => { setTab("library"); refreshRecordings(); }}>记录</button>
      </nav>
      {error && <div className="error-banner">{error}</div>}
      {tab === "capture" && (
        <CapturePage
          live={live}
          participant={participant}
          setParticipant={setParticipant}
          collection={collection}
          setCollection={setCollection}
          start={start}
          stop={stop}
          chart={liveRef.current}
        />
      )}
      {tab === "annotate" && taxonomy && (
        <AnnotationPage recordings={recordings} taxonomy={taxonomy} onError={setError} />
      )}
      {tab === "library" && <Library recordings={recordings} />}
    </div>
  );
}

function CapturePage(props: any) {
  const { live, participant, setParticipant, collection, setCollection, start, stop, chart } = props;
  const active = live.state === "recording";
  const busy = ["arming", "finalizing"].includes(live.state);
  return (
    <main>
      <section className="controls panel">
        <label>数据批次 ID<input value={collection} onChange={(e) => setCollection(e.target.value)} disabled={active || busy} /></label>
        <label>参与者 UniKey<input value={participant} onChange={(e) => setParticipant(e.target.value.toLowerCase())} disabled={active || busy} /></label>
        {!active ? <button className="primary" disabled={busy} onClick={start}>开始录制</button> : <button className="danger" onClick={stop}>结束录制</button>}
      </section>
      <section className="metrics">
        <Metric label="摄像头实际 FPS" value={(live.video?.fps ?? 0).toFixed(1)} />
        <Metric label="视频帧" value={live.video?.frame ?? 0} />
        <Metric label="IMU 通知包" value={live.imu?.packet_count ?? 0} />
        <Metric label="IMU 候选样本" value={live.imu?.sample_count ?? 0} />
        <Metric label="BLE连接" value={live.imu?.connected ? "已连接" : "未连接"} warn={!live.imu?.connected} />
        <Metric label="剩余磁盘" value={`${(live.free_disk_gib ?? 0).toFixed(1)} GiB`} />
      </section>
      <section className="capture-grid">
        <div className="panel camera-panel">
          <div className="panel-title">录制画面 · 仅本机</div>
          {active ? <img src="/api/v1/preview.mjpeg" alt="摄像头实时预览" /> : <div className="placeholder">开始录制后显示实时预览</div>}
        </div>
        <div className="panel chart-panel">
          <div className="panel-title">IMU 六轴实时曲线 · 当前为原始计数</div>
          <Plot time={chart.t} values={chart.values} />
        </div>
      </section>
      {live.recording?.issues?.length > 0 && <div className="issues">{live.recording.issues.map((issue: string) => <div key={issue}>{issue}</div>)}</div>}
    </main>
  );
}

function Metric({ label, value, warn = false }: { label: string; value: string | number; warn?: boolean }) {
  return <div className={`metric ${warn ? "warn" : ""}`}><span>{label}</span><strong>{value}</strong></div>;
}

function AnnotationPage({ recordings, taxonomy, onError }: { recordings: Recording[]; taxonomy: Taxonomy; onError: (message: string) => void }) {
  const [selected, setSelected] = useState("");
  const [doc, setDoc] = useState<AnnotationDocument | null>(null);
  const [timeline, setTimeline] = useState<{ time_s: number[]; values: number[][]; unit: string } | null>(null);
  const [label, setLabel] = useState<"fall" | "non_fall">("non_fall");
  const [activity, setActivity] = useState(taxonomy.non_fall[0].code);
  const [marks, setMarks] = useState<{ start?: number; end?: number; onset?: number; impact?: number }>({});
  const [currentTime, setCurrentTime] = useState(0);
  const video = useRef<HTMLVideoElement>(null);

  useEffect(() => {
    if (!selected) return;
    Promise.all([
      api<AnnotationDocument>(`/api/v1/recordings/${selected}/annotations`),
      api<{ time_s: number[]; values: number[][]; unit: string }>(`/api/v1/recordings/${selected}/timeline`)
    ]).then(([annotations, data]) => { setDoc(annotations); setTimeline(data); setMarks({}); }).catch((e) => onError(e.message));
  }, [selected]);

  useEffect(() => {
    const choices = taxonomy[label];
    setActivity(choices[0].code);
  }, [label]);

  const mark = (name: "start" | "end" | "onset" | "impact") => {
    setMarks((existing) => ({ ...existing, [name]: Math.round((video.current?.currentTime ?? 0) * 1e9) }));
  };

  const addSegment = () => {
    if (!doc || marks.start === undefined || marks.end === undefined) return;
    const recording = recordings.find((item) => item.recording_id === selected)!;
    const segmentId = `seg_${String(doc.segments.length + 1).padStart(3, "0")}`;
    const segment: Segment = {
      segment_id: segmentId,
      start_ns: marks.start,
      end_ns: marks.end,
      binary_label: label,
      activity_code: activity,
      annotator_id: recording.participant_id,
      confidence: 1,
      notes: ""
    };
    const events = [...doc.events];
    if (label === "fall" && marks.onset !== undefined && marks.impact !== undefined) {
      events.push(
        { segment_id: segmentId, kind: "onset", time_ns: marks.onset, source_video_frame: null, source_imu_sample: null, annotator_id: recording.participant_id },
        { segment_id: segmentId, kind: "impact", time_ns: marks.impact, source_video_frame: null, source_imu_sample: null, annotator_id: recording.participant_id }
      );
    }
    setDoc({ ...doc, segments: [...doc.segments, segment].sort((a, b) => a.start_ns - b.start_ns), events });
    setMarks({});
  };

  const save = async (finalized: boolean) => {
    if (!doc || !selected) return;
    try {
      const saved = await api<AnnotationDocument>(`/api/v1/recordings/${selected}/annotations`, {
        method: "PUT",
        body: JSON.stringify({ ...doc, revision: doc.revision + 1, finalized })
      });
      setDoc(saved);
    } catch (e) {
      onError((e as Error).message);
    }
  };

  const choices = taxonomy[label];
  return (
    <main className="annotation-layout">
      <aside className="panel recording-list">
        <div className="panel-title">选择录制</div>
        {recordings.map((recording) => <button key={recording.recording_id} className={selected === recording.recording_id ? "selected" : ""} onClick={() => setSelected(recording.recording_id)}><strong>{recording.participant_id}</strong><span>{recording.recording_id}</span></button>)}
      </aside>
      <section className="annotation-workspace">
        {!selected ? <div className="panel placeholder">从左侧选择一段录制</div> : <>
          <div className="panel video-review"><video ref={video} controls src={`/api/v1/recordings/${selected}/video`} onTimeUpdate={(e) => setCurrentTime(e.currentTarget.currentTime)} /></div>
          <div className="panel"><div className="panel-title">同步 IMU · {timeline?.unit}</div>{timeline && <Plot time={timeline.time_s} values={timeline.values} cursorTime={currentTime} height={250} />}</div>
          <div className="panel annotation-controls">
            <div className="time-readout">当前 {currentTime.toFixed(3)} s</div>
            <div className="mark-buttons"><button onClick={() => mark("start")}>标记动作开始</button><button onClick={() => mark("end")}>标记动作结束</button><button onClick={() => mark("onset")}>标记跌倒起始</button><button onClick={() => mark("impact")}>标记撞击时刻</button></div>
            <div className="marks"><span>动作开始 {seconds(marks.start)}</span><span>动作结束 {seconds(marks.end)}</span><span>跌倒起始 {seconds(marks.onset)}</span><span>撞击时刻 {seconds(marks.impact)}</span></div>
            <div className="segment-form"><select value={label} onChange={(e) => setLabel(e.target.value as any)}><option value="non_fall">non_fall</option><option value="fall">fall</option></select><select value={activity} onChange={(e) => setActivity(e.target.value)}>{choices.map((item) => <option value={item.code} key={item.code}>{item.display_name_zh} · {item.code}</option>)}</select><button className="primary" onClick={addSegment}>添加区间</button></div>
          </div>
          {doc && <div className="panel segment-table"><div className="panel-title">动作区间</div>{doc.segments.map((segment) => <div className="segment-row" key={segment.segment_id}><span>{segment.segment_id}</span><strong>{segment.binary_label === "fall" ? "跌倒" : "非跌倒"}</strong><span>{segment.activity_code}</span><span>{seconds(segment.start_ns)} → {seconds(segment.end_ns)}</span><button onClick={() => setDoc({ ...doc, segments: doc.segments.filter((item) => item.segment_id !== segment.segment_id), events: doc.events.filter((event) => event.segment_id !== segment.segment_id) })}>删除</button></div>)}<div className="save-row"><button onClick={() => save(false)}>保存草稿</button><button className="primary" onClick={() => save(true)}>完成并验证标注</button><span>修订 {doc.revision} · {doc.finalized ? "已定稿" : "草稿"}</span></div></div>}
        </>}
      </section>
    </main>
  );
}

function Library({ recordings }: { recordings: Recording[] }) {
  return <main><section className="panel library"><div className="panel-title">本地录制</div>{recordings.map((recording) => <article key={recording.recording_id}><div><strong>{recording.recording_id}</strong><span>{recording.collection_id} · {recording.participant_id} · {seconds(recording.duration_ns)}</span></div><div className={`state state-${recording.state}`}>{stateLabel(recording.state)}</div>{recording.issues.length > 0 && <ul>{recording.issues.map((issue) => <li key={issue}>{issue}</li>)}</ul>}</article>)}</section></main>;
}
