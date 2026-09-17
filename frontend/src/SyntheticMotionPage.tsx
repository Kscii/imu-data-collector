import { useEffect, useId, useMemo, useRef, useState } from "react";
import { tr } from "./i18n";
import { SyntheticIMUChart, type IMUReadout, type PlaybackMetadata } from "./SyntheticIMUChart";

export type FormalLabel = {
  code: string; name: string; is_fall: boolean;
  origin?: string; verification?: string; mapping_rule_id?: string | null;
};
export type Candidate = {
  candidate_id: string; version_id: string; source_dataset: string;
  lease_token?: string;
  source_member: string; published_at_utc: string;
  label_candidates: {code?: string | null; raw_label?: string | null;
    name?: string | null; categories?: string[] | null; kind?: string; origin?: string}[];
  warning_flags: string[]; risk_tier?: string; label: FormalLabel | null;
  decision: "unreviewed" | "pass" | "reject"; revision: number;
};
export type Catalog = {
  taxonomy_id: string; version: string; motion_revision: number;
  concepts: (FormalLabel & {active: boolean; scope: string})[];
  rules: {rule_id: string; origin: string; source_value: string;
    source_dataset: string | null; target_code: string; state: string}[];
};
export const syntheticRoot = "/api/v1/synthetic";
export const candidateKey = (item: Candidate) => `${item.candidate_id}/${item.version_id}`;
type Summary = {published: number; unreviewed: number; passed: number;
  rejected: number; label_pending: number; latest_published_at_utc: string | null;
  read_only?: boolean; preview_mode?: string | null};

export async function syntheticRequest<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    ...options, headers: {"Content-Type": "application/json", ...(options?.headers ?? {})},
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(
    typeof payload.detail === "string" ? payload.detail : `HTTP ${response.status}`);
  return payload as T;
}

const editable = (target: EventTarget | null) =>
  Boolean((target as HTMLElement | null)?.closest("input,textarea,select,[contenteditable='true']"));

function FormalLabelPicker({concepts, value, onChange, onPlay}: {
  concepts: Catalog["concepts"]; value: string;
  onChange: (value: string) => void; onPlay: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [highlight, setHighlight] = useState(0);
  const root = useRef<HTMLDivElement>(null);
  const id = useId();
  const active = concepts.filter(item => item.active);
  const selected = active.find(item => item.code === value);
  useEffect(() => {
    const outside = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", outside);
    return () => document.removeEventListener("pointerdown", outside);
  }, []);
  return <div className="synthetic-label-picker" ref={root}>
    <span>{tr("正式标签", "Formal label")}</span>
    <button type="button" role="combobox" aria-expanded={open}
      aria-controls={`${id}-list`} aria-activedescendant={open ? `${id}-${highlight}` : undefined}
      aria-label={tr("正式活动标签", "Formal activity label")}
      onClick={() => { setOpen(value => !value); setHighlight(Math.max(0,
        active.findIndex(item => item.code === value))); }}
      onKeyDown={event => {
        if (event.key === " ") {
          event.preventDefault(); event.stopPropagation(); onPlay(); return;
        }
        if (event.key === "Escape" && open) {
          event.preventDefault(); setOpen(false); return;
        }
        if (event.key === "ArrowDown" || event.key === "ArrowUp") {
          event.preventDefault(); setOpen(true);
          setHighlight(index => Math.max(0, Math.min(active.length - 1,
            index + (event.key === "ArrowDown" ? 1 : -1))));
        }
        if (event.key === "Enter") {
          event.preventDefault();
          if (open && active[highlight]) { onChange(active[highlight].code); setOpen(false); }
          else setOpen(true);
        }
      }}>
      {selected?.name ?? tr("从受控列表选择", "Choose from managed labels")}
      <span aria-hidden="true">⌄</span>
    </button>
    {open && <div className="synthetic-label-options" id={`${id}-list`} role="listbox">
      {active.map((item, index) => <div key={item.code} id={`${id}-${index}`} role="option"
        aria-selected={item.code === value} className={index === highlight ? "highlight" : ""}
        onMouseEnter={() => setHighlight(index)}
        onClick={() => { onChange(item.code); setOpen(false); }}>
        {item.name}{item.is_fall ? tr(" · 跌倒", " · Fall") : ""}
      </div>)}
    </div>}
  </div>;
}

function sourceSuggestions(item: Candidate) {
  const labels = item.label_candidates.map(value => ({
    name: value.raw_label || value.name || value.categories?.join(", ") || value.code || "",
    kind: value.kind,
  })).filter(value => value.name);
  return labels.filter((value, index) => labels.findIndex(other =>
    other.name.toLowerCase() === value.name.toLowerCase()) === index);
}

export function SyntheticMotionPage({target = "dev"}: {target?: "dev" | "prod"}) {
  const deepLink = useRef(new URLSearchParams(location.search)).current;
  // A fresh URL avoids reusing a player cached before the bridge was updated.
  const viewerCacheKey = useRef(`${Date.now()}-${Math.random().toString(36).slice(2)}`).current;
  const deepCandidate = deepLink.get("candidate") ?? "";
  const deepVersion = deepLink.get("version") ?? "";
  const restoreKey = useRef(deepCandidate && deepVersion ? `${deepCandidate}/${deepVersion}` : "");
  const [view, setView] = useState<"quality" | "labels">(
    deepLink.get("stage") === "label" ? "labels" : "quality");
  const [items, setItems] = useState<Candidate[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [selected, setSelected] = useState("");
  const [filter, setFilter] = useState(() => {
    const saved = deepLink.get("review_filter");
    return saved && ["unreviewed", "pass", "reject", "all"].includes(saved)
      ? saved : deepCandidate ? "all" : "unreviewed";
  });
  const [search, setSearch] = useState("");
  const [highRisk, setHighRisk] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [labelRevision, setLabelRevision] = useState(0);
  const [chosenCode, setChosenCode] = useState("");
  const [reasons, setReasons] = useState<{code: string; name: string}[]>([]);
  const [reasonCodes, setReasonCodes] = useState<string[]>([]);
  const [reasonNote, setReasonNote] = useState("");
  const [resetNote, setResetNote] = useState("");
  const [rejectOpen, setRejectOpen] = useState(false);
  const [resetOpen, setResetOpen] = useState(false);
  const [working, setWorking] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [cursorFrame, setCursorFrame] = useState(0);
  const [chartReady, setChartReady] = useState(false);
  const [bridgeStatus, setBridgeStatus] = useState<"loading" | "ready" | "lost" | "outdated">("loading");
  const [maxFrame, setMaxFrame] = useState(0);
  const [imuReadout, setImuReadout] = useState<IMUReadout | null>(null);
  const [playbackMetadata, setPlaybackMetadata] = useState<PlaybackMetadata | null>(null);
  const frame = useRef<HTMLIFrameElement>(null);
  const bridgeLastSeen = useRef(0);
  const bridgeVersion = useRef(0);
  const drawerButton = useRef<HTMLButtonElement>(null);
  const drawerSearch = useRef<HTMLInputElement>(null);
  const drawerWasOpened = useRef(false);
  const leases = useRef<string[]>([]);
  const loadGeneration = useRef(0);

  const queueMode = view === "quality" && filter === "unreviewed";
  const refreshSummary = () => syntheticRequest<Summary>(`${syntheticRoot}/summary`)
    .then(setSummary).catch(error => setError(String(error)));
  const release = (lease_token: string, skip = false) =>
    syntheticRequest(`${syntheticRoot}/queue/release`, {method: "POST",
      body: JSON.stringify({lease_token, skip})});
  const claim = async (generation = loadGeneration.current, exact?: Candidate) => {
    const params = new URLSearchParams({search: exact?.candidate_id ?? search,
      high_risk: String(exact ? false : highRisk)});
    const value = await syntheticRequest<{candidates: Candidate[]}>(
      `${syntheticRoot}/queue/claim?${params}`, {method: "POST"});
    // A stale response must not release tokens owned by a newer page load.
    if (generation !== loadGeneration.current) return;
    const restored = exact && value.candidates.find(item => candidateKey(item) === candidateKey(exact));
    if (exact) await Promise.allSettled(value.candidates
      .filter(item => item !== restored && item.lease_token)
      .map(item => release(item.lease_token!)));
    if (generation !== loadGeneration.current) return;
    const claimed = exact ? restored ? [restored] : [] : value.candidates;
    leases.current.push(...claimed.flatMap(item => item.lease_token ? [item.lease_token] : []));
    setItems(exact ? restored ? [restored] : [exact] : value.candidates);
    setHasMore(false);
    setSelected(exact ? candidateKey(exact)
      : value.candidates[0] ? candidateKey(value.candidates[0]) : "");
    if (exact && !restored) setMessage(tr("当前片段尚未领取，可在右侧重新领取", "This clip is not claimed; reclaim it on the right"));
  };
  const browse = async (offset = 0, generation = loadGeneration.current,
                        preferred = "", pinned?: Candidate) => {
    const params = new URLSearchParams({
      decision: view === "labels" ? "pass" : filter,
      search, high_risk: String(highRisk), limit: "100", offset: String(offset),
    });
    if (view === "labels" && filter !== "all")
      params.set("label_state", filter === "unreviewed" ? "pending" : "labeled");
    const value = await syntheticRequest<{candidates: Candidate[]}>(
      `${syntheticRoot}/candidates?${params}`);
    if (generation !== loadGeneration.current) return;
    const page = pinned && !value.candidates.some(item => candidateKey(item) === candidateKey(pinned))
      ? [pinned, ...value.candidates] : value.candidates;
    setItems(previous => offset ? [...previous, ...value.candidates.filter(item =>
      !previous.some(loaded => candidateKey(loaded) === candidateKey(item)))] : page);
    setHasMore(value.candidates.length === 100);
    if (!offset) {
      const next = page.find(item => candidateKey(item) === preferred) ?? page[0];
      setSelected(next ? candidateKey(next) : "");
    }
  };
  useEffect(() => {
    syntheticRequest<Catalog>(`${syntheticRoot}/labels`).then(setCatalog)
      .catch(error => setError(String(error)));
    syntheticRequest<{reasons: {code: string; name: string}[]}>(`${syntheticRoot}/rejection-reasons`)
      .then(value => setReasons(value.reasons)).catch(error => setError(String(error)));
    void refreshSummary();
    const timer = window.setInterval(refreshSummary, 20000);
    return () => window.clearInterval(timer);
  }, []);
  useEffect(() => {
    const generation = ++loadGeneration.current;
    setItems([]); setSelected(""); setError("");
    const previous = leases.current.splice(0);
    const load = async () => {
      await Promise.allSettled(previous.map(token => release(token)));
      if (generation !== loadGeneration.current) return;
      const target = restoreKey.current;
      restoreKey.current = "";
      if (target) {
        const [candidateId, versionId] = target.split("/");
        try {
          const exact = await syntheticRequest<Candidate>(
            `${syntheticRoot}/candidates/${encodeURIComponent(candidateId)}/${encodeURIComponent(versionId)}/entry`);
          if (generation !== loadGeneration.current) return;
          const nextView = view === "labels" && exact.decision !== "pass" ? "quality" : view;
          const matchesFilter = nextView === "labels" ? filter === "all"
            || (filter === "unreviewed" ? !exact.label : Boolean(exact.label))
            : filter === "all" || filter === exact.decision;
          if (nextView !== view || !matchesFilter) {
            restoreKey.current = target;
            if (nextView !== view) setView(nextView);
            setFilter(nextView === "labels" ? exact.label ? "pass" : "unreviewed"
              : exact.decision);
            return;
          }
          if (queueMode) await claim(generation, exact);
          else await browse(0, generation, target, exact);
        } catch (reason) {
          if (generation === loadGeneration.current) setError(
            `${tr("无法恢复当前片段，请从数据管理重新打开", "Could not restore this clip; reopen it from Data management")}: ${String(reason)}`);
        }
        return;
      }
      await (queueMode ? claim(generation) : browse(0, generation));
    };
    load().catch(error => { if (generation === loadGeneration.current) setError(String(error)); });
    return () => { loadGeneration.current++; };
  }, [view, filter, search, highRisk]);
  useEffect(() => {
    return () => {
      const tokens = leases.current.splice(0);
      for (const lease_token of tokens) void syntheticRequest(
        `${syntheticRoot}/queue/release`, {method: "POST", keepalive: true,
          body: JSON.stringify({lease_token})}).catch(() => undefined);
    };
  }, []);
  useEffect(() => {
    if (summary?.read_only) return;
    const timer = window.setInterval(() => {
      for (const lease_token of leases.current) void syntheticRequest(
        `${syntheticRoot}/queue/renew`, {method: "POST",
          body: JSON.stringify({lease_token})}).catch(error => {
        leases.current = leases.current.filter(token => token !== lease_token);
        setItems(previous => previous.map(item => item.lease_token === lease_token
          ? {...item, lease_token: undefined} : item));
        setError(String(error));
      });
    }, 300000);
    return () => window.clearInterval(timer);
  }, [summary?.read_only]);
  useEffect(() => {
    if (drawerOpen) {
      drawerWasOpened.current = true;
      drawerSearch.current?.focus();
    } else if (drawerWasOpened.current) drawerButton.current?.focus();
  }, [drawerOpen]);

  const visible = useMemo(() => items.filter(item => {
    const status = view === "quality"
      ? filter === "all" || item.decision === filter
      : item.decision === "pass" && (filter === "all"
        || (filter === "unreviewed" ? !item.label : Boolean(item.label)));
    return status && (!highRisk || item.risk_tier === "high")
      && (!search || `${item.source_dataset} ${item.source_member} ${item.candidate_id}`
        .toLowerCase().includes(search.toLowerCase()));
  }), [items, view, filter, highRisk, search]);
  const current = visible.find(item => candidateKey(item) === selected) ?? visible[0];
  const base = current ? `${syntheticRoot}/candidates/${encodeURIComponent(current.candidate_id)}/${current.version_id}` : "";
  const canDecide = view === "quality" && Boolean(current?.lease_token) && !summary?.read_only;

  useEffect(() => {
    if (!selected || !current || candidateKey(current) !== selected) return;
    const url = new URL(location.href);
    if (url.searchParams.get("view") !== "synthetic") return;
    url.searchParams.set("candidate", current.candidate_id);
    url.searchParams.set("version", current.version_id);
    url.searchParams.set("stage", view === "labels" ? "label" : "quality");
    url.searchParams.set("review_filter", filter);
    url.searchParams.delete("domain");
    url.searchParams.delete("recording");
    url.searchParams.delete("task");
    history.replaceState({}, "", url);
  }, [selected, current, view, filter]);

  useEffect(() => {
    setLabelRevision(0); setChosenCode(current?.label?.code ?? "");
    setCursorFrame(0); setChartReady(false); setBridgeStatus("loading");
    setMaxFrame(0); bridgeLastSeen.current = Date.now(); bridgeVersion.current = 0;
    setReasonCodes([]); setReasonNote(""); setResetNote("");
    setRejectOpen(false); setResetOpen(false);
    if (current) syntheticRequest<{label_revision: {revision: number} | null}>(base)
      .then(value => setLabelRevision(value.label_revision?.revision ?? 0))
      .catch(error => setError(String(error)));
  }, [base]);
  useEffect(() => {
    if (!base) return;
    const timer = window.setInterval(() => {
      if (Date.now() - bridgeLastSeen.current > 2500)
        setBridgeStatus(current => current === "outdated" ? current : "lost");
    }, 500);
    return () => window.clearInterval(timer);
  }, [base]);
  const selectCandidate = async (item: Candidate) => {
    if (current?.lease_token && !queueMode && candidateKey(current) !== candidateKey(item)) {
      const token = current.lease_token;
      leases.current = leases.current.filter(value => value !== token);
      setItems(previous => previous.map(row => candidateKey(row) === candidateKey(current)
        ? {...row, lease_token: undefined} : row));
      await release(token).catch(error => setError(String(error)));
    }
    setSelected(candidateKey(item)); setDrawerOpen(false);
  };
  const afterCurrent = async (item: Candidate, skip: boolean) => {
    if (item.lease_token) {
      await release(item.lease_token, skip && queueMode);
      leases.current = leases.current.filter(token => token !== item.lease_token);
    }
    if (queueMode) {
      const remaining = items.filter(row => candidateKey(row) !== candidateKey(item));
      setItems(remaining);
      if (remaining.length) setSelected(candidateKey(remaining[0]));
      else await claim();
      return;
    }
    if (item.lease_token) setItems(previous => previous.map(row =>
      candidateKey(row) === candidateKey(item) ? {...row, lease_token: undefined} : row));
    const index = visible.findIndex(row => candidateKey(row) === candidateKey(item));
    const next = visible[index + 1] ?? visible[index - 1];
    setSelected(next ? candidateKey(next) : "");
  };
  const claimReviewed = async () => {
    if (!current || current.decision === "unreviewed" || working || summary?.read_only) return;
    setWorking(true); setError("");
    try {
      const value = await syntheticRequest<{lease_token: string; revision: number}>(
        `${syntheticRoot}/queue/claim-reviewed`, {method: "POST",
          body: JSON.stringify({candidate_id: current.candidate_id,
            version_id: current.version_id, expected_revision: current.revision})});
      leases.current.push(value.lease_token);
      setItems(previous => previous.map(row => candidateKey(row) === candidateKey(current)
        ? {...row, lease_token: value.lease_token} : row));
      setMessage(tr("已领取复审任务", "Review task claimed"));
    } catch (error) {
      setError(String(error));
      await browse(0, loadGeneration.current, candidateKey(current))
        .catch(refreshError => setError(String(refreshError)));
    } finally { setWorking(false); }
  };
  const reclaimCurrent = async () => {
    if (!current || current.decision !== "unreviewed" || working || summary?.read_only) return;
    setWorking(true); setError("");
    try {
      const params = new URLSearchParams({search: current.candidate_id});
      const result = await syntheticRequest<{candidates: Candidate[]}>(
        `${syntheticRoot}/queue/claim?${params}`, {method: "POST"});
      const found = result.candidates.find(item => candidateKey(item) === candidateKey(current));
      await Promise.allSettled(result.candidates.filter(item => item !== found && item.lease_token)
        .map(item => release(item.lease_token!)));
      if (!found) {
        setError(tr("当前片段暂时无法领取，可能已由其他审核员领取", "This clip cannot be claimed now; another reviewer may hold it"));
        return;
      }
      if (found.lease_token) leases.current.push(found.lease_token);
      setItems(previous => previous.map(item => candidateKey(item) === candidateKey(found)
        ? found : item));
      setMessage(tr("已重新领取当前片段", "Current clip reclaimed"));
    } catch (reason) { setError(String(reason)); }
    finally { setWorking(false); }
  };
  const decide = async (decision: "pass" | "reject" | "unreviewed") => {
    if (!current || !canDecide || working || decision === current.decision ||
        (decision === "reject" && !reasonCodes.length && !reasonNote.trim())) return;
    setWorking(true); setError("");
    const item = current;
    const nextIndex = visible.findIndex(row => candidateKey(row) === candidateKey(item));
    const next = visible[nextIndex + 1] ?? visible[nextIndex - 1];
    try {
      await syntheticRequest(`${base}/reviews`, {method: "POST", body: JSON.stringify({
        decision, expected_revision: item.revision, labels: [],
        lease_token: item.lease_token,
        reason_codes: decision === "reject" ? reasonCodes : [],
        reason: decision === "reject" ? reasonNote.trim() || null
          : decision === "unreviewed" ? resetNote.trim() || null : null,
      })});
      leases.current = leases.current.filter(token => token !== item.lease_token);
      setRejectOpen(false); setResetOpen(false);
      setMessage(decision === "pass" ? tr("质量已通过", "Quality passed")
        : decision === "reject" ? tr("已拒绝", "Rejected")
          : tr("已撤回为未审核", "Returned to unreviewed"));
      if (queueMode) {
        const remaining = items.filter(row => candidateKey(row) !== candidateKey(item));
        setItems(remaining);
        if (remaining.length) setSelected(candidateKey(remaining[0]));
        else await claim();
      } else {
        await browse(0, loadGeneration.current, next ? candidateKey(next) : "");
      }
      void refreshSummary();
    } catch (error) {
      setError(String(error));
      leases.current = leases.current.filter(token => token !== item.lease_token);
      if (queueMode) await claim().catch(refreshError => setError(String(refreshError)));
      else await browse(0, loadGeneration.current, candidateKey(item))
        .catch(refreshError => setError(String(refreshError)));
    } finally { setWorking(false); }
  };
  const saveLabel = async () => {
    if (!current || !chosenCode || working || summary?.read_only) return;
    setWorking(true); setError("");
    try {
      await syntheticRequest(`${base}/labels`, {method: "POST",
        body: JSON.stringify({code: chosenCode, expected_revision: labelRevision})});
      setMessage(tr("正式标签已保存", "Formal label saved"));
      const next = visible.find(item => candidateKey(item) !== candidateKey(current) && !item.label);
      await browse(0, loadGeneration.current, next ? candidateKey(next) : "");
      void refreshSummary();
    } catch (error) { setError(String(error)); }
    finally { setWorking(false); }
  };
  const returnFromLabels = async () => {
    if (!current || current.decision !== "pass" || working || summary?.read_only) return;
    const item = current;
    setWorking(true); setError("");
    let token = "";
    try {
      const lease = await syntheticRequest<{lease_token: string}>(
        `${syntheticRoot}/queue/claim-reviewed`, {method: "POST",
          body: JSON.stringify({candidate_id: item.candidate_id,
            version_id: item.version_id, expected_revision: item.revision})});
      token = lease.lease_token;
      await syntheticRequest(`${base}/reviews`, {method: "POST", body: JSON.stringify({
        decision: "unreviewed", expected_revision: item.revision, labels: [],
        lease_token: token, reason_codes: [], reason: resetNote.trim() || null,
      })});
      setResetOpen(false);
      setMessage(tr("已退回质量审核队列顶部，正式标签保留", "Returned to the top of quality review; formal label preserved"));
      const next = visible.find(row => candidateKey(row) !== candidateKey(item));
      await browse(0, loadGeneration.current, next ? candidateKey(next) : "");
      void refreshSummary();
    } catch (reason) {
      setError(String(reason));
      await browse(0, loadGeneration.current, candidateKey(item))
        .catch(refreshError => setError(String(refreshError)));
    } finally {
      if (token) await release(token).catch(() => undefined);
      setWorking(false);
    }
  };
  const control = (action: "toggle" | "replay" | "seek" | "chart-ready",
                   extra: {frame?: number; ready?: boolean} = {}) =>
    frame.current?.contentWindow?.postMessage(
      {type: "imu-synthetic-review-control", action, ...extra}, location.origin);
  useEffect(() => { if (chartReady) control("chart-ready", {ready: true}); }, [chartReady, base]);
  const shortcut = (key: string) => {
    if (working) return;
    if (key === " " && !rejectOpen && !resetOpen) { control("toggle"); return; }
    if (drawerOpen) {
      if (key === "Escape") setDrawerOpen(false);
      return;
    }
    if (resetOpen) {
      if (key === "Escape") setResetOpen(false);
      return;
    }
    if (rejectOpen) {
      if (key === "Escape") setRejectOpen(false);
      if (key === "Enter") void decide("reject");
      return;
    }
    if (key.toLowerCase() === "r") control("replay");
    if (key.toLowerCase() === "n" && current) void afterCurrent(current, queueMode)
      .catch(error => setError(String(error)));
    if (view !== "quality" || !canDecide) return;
    if (key.toLowerCase() === "p" && current?.decision !== "pass") void decide("pass");
    if (key.toLowerCase() === "x" && current?.decision !== "reject") setRejectOpen(true);
    if (key.toLowerCase() === "s" && queueMode) {
      if (current) void afterCurrent(current, true).catch(error => setError(String(error)));
      setMessage(tr("已跳过，仍为未审核", "Skipped; still unreviewed"));
    }
  };
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.altKey || event.ctrlKey || event.metaKey) return;
      if (editable(event.target) && event.key !== "Escape") return;
      if ([" ", "r", "R", "n", "N", "p", "P", "x", "X", "s", "S"].includes(event.key)
          || ((drawerOpen || rejectOpen || resetOpen) && ["Enter", "Escape"].includes(event.key))) {
        event.preventDefault(); shortcut(event.key);
      }
    };
    const onMessage = (event: MessageEvent) => {
      if (event.origin !== location.origin || event.source !== frame.current?.contentWindow
          || !event.data) return;
      if (event.data.type === "imu-synthetic-review-ready") {
        if (event.data.version !== 2) { setBridgeStatus("outdated"); return; }
        bridgeVersion.current = 2; bridgeLastSeen.current = Date.now();
        setBridgeStatus("ready");
        if (Number.isInteger(event.data.maxFrame)) setMaxFrame(event.data.maxFrame);
        if (Number.isInteger(event.data.frame)) setCursorFrame(event.data.frame);
      }
      if (event.data.type === "imu-synthetic-review-frame"
          && bridgeVersion.current === 2 && Number.isInteger(event.data.frame)) {
        bridgeLastSeen.current = Date.now(); setBridgeStatus("ready");
        setCursorFrame(event.data.frame);
      }
      if (event.data.type === "imu-synthetic-review-key"
          && typeof event.data.key === "string") shortcut(event.data.key);
    };
    window.addEventListener("keydown", onKey); window.addEventListener("message", onMessage);
    return () => { window.removeEventListener("keydown", onKey); window.removeEventListener("message", onMessage); };
  });

  return <main className="synthetic-workbench">
    <section className="annotation-workbench-bar synthetic-topbar">
      <button ref={drawerButton} onClick={() => setDrawerOpen(true)}>{tr("审核队列", "Review queue")}</button>
      <a className="button-link" href="?view=data&domain=synthetic">{tr("数据管理", "Data management")}</a>
      <div className="annotation-recording-summary synthetic-current-summary">
        <strong>{current ? tr("当前片段", "Current clip") : tr("没有待处理片段", "No clip selected")}</strong>
        {current && <span>{tr("片段", "Clip")} {visible.indexOf(current) + 1}/{visible.length} · {current.decision === "pass"
          ? tr("已通过", "Passed") : current.decision === "reject"
            ? tr("已拒绝", "Rejected") : tr("未审核", "Unreviewed")}</span>}
      </div>
      <nav className="synthetic-tabs">
        <button className={view === "quality" ? "active" : ""} onClick={() => {
          setView("quality"); setFilter("unreviewed"); setSelected("");
        }}>{tr("质量审核", "Quality review")}</button>
        <button className={view === "labels" ? "active" : ""} onClick={() => {
          setView("labels"); setFilter("unreviewed"); setSelected("");
        }}>{tr("标签待办", "Label tasks")}</button>
      </nav>
      {summary && <span className="synthetic-progress">
        {tr("待审核", "Pending")} {summary.unreviewed} · {tr("待标签", "Labels")} {summary.label_pending}
      </span>}
    </section>
    {target === "dev" && <div className="warning-banner synthetic-mode-banner">
      {tr("测试环境：这里的审核和快照不会进入正式数据", "Test environment: reviews and snapshots do not enter production")}
    </div>}
    {(summary?.read_only || summary?.preview_mode) && <div className="warning-banner synthetic-mode-banner">
      {summary.read_only ? tr("只读预览：审核决定不会保存", "Read-only preview: decisions are disabled")
        : tr("本地试用：决定只保存在本机", "Local trial: decisions stay on this machine")}
    </div>}
    <section className="synthetic-workbench-body">
      {drawerOpen && <>
        <button className="recording-drawer-backdrop" aria-label={tr("关闭审核队列", "Close review queue")}
          onClick={() => setDrawerOpen(false)} />
        <aside className="recording-drawer synthetic-drawer" role="dialog" aria-modal="true"
          aria-label={tr("审核队列", "Review queue")}>
          <div className="recording-drawer-controls">
            <div className="recording-drawer-header"><strong>{tr("选择片段", "Choose clip")}</strong>
              <button onClick={() => setDrawerOpen(false)}>{tr("关闭", "Close")}</button></div>
            <a className="button-link" href="?view=data&domain=synthetic">{tr("在数据管理中搜索全部片段", "Search all clips in Data management")}</a>
            <input ref={drawerSearch} value={search} onChange={event => setSearch(event.target.value)}
              placeholder={tr("搜索来源或片段 ID", "Search source or clip ID")} />
            <div className="recording-queue-tabs synthetic-filter-tabs" role="tablist">
              {[
                ["unreviewed", view === "quality" ? tr("未审核", "Unreviewed") : tr("待标签", "Needs label"), view === "quality" ? summary?.unreviewed : summary?.label_pending],
                ["pass", view === "quality" ? tr("已通过", "Passed") : tr("已标注", "Labeled"), view === "quality" ? summary?.passed : summary ? summary.passed - summary.label_pending : undefined],
                ...(view === "quality" ? [["reject", tr("已拒绝", "Rejected"), summary?.rejected]] : []),
                ["all", tr("全部", "All"), view === "quality" ? summary?.published : summary?.passed],
              ].map(([value, label, count]) => <button key={String(value)}
                className={filter === value ? "active" : ""} role="tab"
                aria-selected={filter === value} onClick={() => setFilter(String(value))}>
                <span>{label}</span><strong>{count ?? "·"}</strong></button>)}
            </div>
            <label className="synthetic-risk-filter"><input type="checkbox" checked={highRisk}
              onChange={event => setHighRisk(event.target.checked)} />{tr("只看高风险提示", "High-risk hints only")}</label>
          </div>
          <div className="recording-drawer-list-wrap">
            <div className="recording-drawer-list synthetic-queue-list">
              {visible.length === 0 && <div className="recording-queue-empty-state">
                <strong>{tr("没有匹配片段", "No matching clips")}</strong>
                <span>{tr("可切换状态或调整搜索条件。", "Change the status or search.")}</span>
              </div>}
              {visible.map((item, index) => <button key={candidateKey(item)}
                className={item === current ? "selected" : ""}
                onClick={() => void selectCandidate(item)}>
                <strong>{tr("片段", "Clip")} {index + 1}</strong>
                <span>{item.risk_tier === "high" ? tr("高风险提示", "High-risk hint")
                  : item.warning_flags.length ? tr("有 QA 提示", "QA hints")
                    : tr("无 QA 提示", "No QA hint")}</span>
              </button>)}
              {hasMore && <button onClick={() => browse(items.length)
                .catch(error => setError(String(error)))}>{tr("加载更多", "Load more")}</button>}
            </div>
          </div>
          <div className="recording-drawer-footer"><span>{tr("当前列表", "Current list")} {visible.length}</span>
            <span>{tr("全部已发布", "Published")} {summary?.published ?? "—"}</span></div>
        </aside>
      </>}
      <section className="panel synthetic-viewer-pane">
        {current ? <>
          <iframe ref={frame} key={base} title={tr("动作与 IMU 同源回放", "Motion and IMU replay")}
            src={`${base}/files/index.html?bridge=2&viewer=${viewerCacheKey}`} className="synthetic-frame"
            onLoad={() => { if (chartReady) control("chart-ready", {ready: true}); }} />
          <SyntheticIMUChart base={base} cursorFrame={cursorFrame}
            onReady={setChartReady} onInspect={setImuReadout}
            onMetadata={setPlaybackMetadata} onSeek={index => {
              if (bridgeStatus !== "ready") return;
              setCursorFrame(index); control("seek", {frame: index});
            }} />
          <div className="synthetic-playback-bar">
            <button disabled={bridgeStatus !== "ready"} onClick={() => control("toggle")}>{tr("播放／暂停 Space", "Play / pause Space")}</button>
            <button disabled={bridgeStatus !== "ready"} onClick={() => control("replay")}>{tr("重播 R", "Replay R")}</button>
            <strong>{bridgeStatus === "ready" ? `source ${cursorFrame}/${maxFrame} · ${imuReadout?.time_s.toFixed(3) ?? "0.000"} s`
              : tr("播放器同步未就绪", "Playback sync unavailable")}</strong>
            <span>{tr("滚轮缩放人物；页面保持固定", "Wheel zooms the model; page stays fixed")}</span>
          </div>
        </> : <div className="synthetic-empty">{tr("当前队列没有可处理的片段。可打开审核队列选择其他状态。", "No clips here. Open the review queue to choose another status.")}</div>}
      </section>
      <section className="synthetic-task-pane">
        <div className="synthetic-task-head">
          <div><span className="panel-title">{view === "quality"
            ? tr("质量审核", "Quality review") : tr("正式标签", "Formal label")}</span>
            <h2>{view === "quality" ? tr("动作与 IMU 是否可用", "Is motion and IMU usable?")
              : tr("选择活动标签", "Choose activity label")}</h2></div>
          {current && <button onClick={() => void afterCurrent(current, queueMode)
            .catch(error => setError(String(error)))}>{tr("下一条 N", "Next N")}</button>}
        </div>
        <div className="synthetic-task-scroll">
          {error && <div className="error-banner" role="alert">{error}</div>}
          {message && <div className="success-banner" role="status">{message}</div>}
          {current && <>
            <div className={`synthetic-decision-state state-${current.decision}`}>
              {current.decision === "pass" ? tr("质量已通过", "Quality passed")
                : current.decision === "reject" ? tr("质量已拒绝", "Quality rejected")
                  : tr("等待质量审核", "Awaiting quality review")}
            </div>
            {current.warning_flags.length > 0 && <div className="warning-banner">
              QA · {current.warning_flags.join(", ")}</div>}
            {bridgeStatus !== "ready" && <div className="warning-banner" role="status">
              {bridgeStatus === "outdated" ? tr("播放器版本过旧，请刷新页面", "Viewer version is outdated; refresh the page")
                : bridgeStatus === "lost" ? tr("播放器通信中断；当前 IMU 时间读数暂停", "Viewer connection lost; IMU time readout is paused")
                  : tr("正在连接播放器…", "Connecting to viewer…")}
            </div>}
            {bridgeStatus === "ready" && imuReadout && <div className="synthetic-imu-readout">
              <strong>{tr("当前播放位置 · IMU", "Current position · IMU")}</strong>
              <span>{imuReadout.sensor} · {imuReadout.time_s.toFixed(2)} s</span>
              <div><span>{tr("比力幅值", "Specific force")} <b>{imuReadout.force.toFixed(2)}</b> m/s²</span>
                <span>{tr("角速度幅值", "Angular velocity")} <b>{imuReadout.gyro.toFixed(2)}</b> rad/s</span></div>
            </div>}
            {view === "quality" && current.label && <p className="synthetic-label-hint">
              {tr("正式标签", "Formal label")}：{current.label.name}</p>}
            {view === "labels" && <>
              <div className="synthetic-source-tags">
                <strong>{tr("来源候选标签（仅供参考）", "Source labels (reference only)")}</strong>
                {sourceSuggestions(current).length ? <div>{sourceSuggestions(current).slice(0, 5)
                  .map((item, index) => <span key={index}>
                    {item.kind === "temporal-candidate" ? tr("片段 · ", "Segment · ") : ""}{item.name}
                  </span>)}
                  {sourceSuggestions(current).length > 5 && <small>
                    +{sourceSuggestions(current).length - 5}</small>}</div>
                  : <small>{tr("来源未提供候选标签", "No source labels provided")}</small>}
              </div>
              <FormalLabelPicker concepts={catalog?.concepts ?? []} value={chosenCode}
                onChange={setChosenCode} onPlay={() => control("toggle")} />
              {current.label && <p className="synthetic-label-hint">
                {tr("当前", "Current")}：{current.label.name} · {current.label.origin === "auto"
                  ? tr("来源映射", "Source mapping") : tr("人工选择", "Human selected")}</p>}
            </>}
            <details className="synthetic-details"><summary>{tr("来源与技术详情", "Source and technical details")}</summary>
              <p>{tr("SMPL+H 运动学回放", "SMPL+H kinematic replay")}
                {playbackMetadata?.frameCount ? ` · ${playbackMetadata.frameCount} ${tr("帧", "frames")}` : ""}
                {playbackMetadata?.layoutId ? ` · ${playbackMetadata.layoutId}` : ""}
                {playbackMetadata?.dmplAvailable !== undefined
                  ? ` · ${playbackMetadata.dmplAvailable ? tr("来源 DMPL", "Source DMPL") : tr("无来源 DMPL", "No source DMPL")}` : ""}
                {playbackMetadata?.qaPassed !== undefined
                  ? ` · ${tr("机器 QA", "Machine QA")} ${playbackMetadata.qaPassed ? tr("通过", "pass") : tr("待核查", "review")}` : ""}</p>
              <p>{current.source_dataset} · {current.source_member} · {current.candidate_id} · revision {current.revision}</p>
              <strong>{tr("来源候选描述，仅供参考", "Source suggestions, for reference only")}</strong>
              {current.label_candidates.length === 0 ? <p>{tr("来源没有提供标签", "No source labels")}</p>
                : <ul>{current.label_candidates.slice(0, 12).map((label, index) =>
                  <li key={index}>{label.kind === "temporal-candidate"
                    ? tr("片段", "Segment") : tr("整段", "Recording")}
                    {" · "}{label.raw_label || label.code || tr("空标签", "Empty label")}
                    {label.categories?.length ? ` [${label.categories.join(", ")}]` : ""}</li>)}</ul>}
            </details>
          </>}
        </div>
        {current && <div className="synthetic-action-bar">
          {view === "quality" ? !current.lease_token
            ? <button className="primary" disabled={working || summary?.read_only}
                onClick={() => void (current.decision === "unreviewed"
                  ? reclaimCurrent() : claimReviewed())}>{current.decision === "unreviewed"
                  ? tr("领取当前片段", "Claim current clip")
                  : tr("领取复审", "Claim re-review")}</button>
            : <>
              {current.decision !== "pass" && <button className="primary"
                disabled={working || !canDecide} onClick={() => void decide("pass")}>
                {tr("通过 P", "Pass P")}</button>}
              {current.decision !== "reject" && <button disabled={working || !canDecide}
                onClick={() => setRejectOpen(true)}>{tr("拒绝 X", "Reject X")}</button>}
              {current.decision !== "unreviewed"
                ? <button disabled={working || !canDecide} onClick={() => setResetOpen(true)}>
                  {tr("撤回为未审核", "Return to unreviewed")}</button>
                : queueMode && <button disabled={working} onClick={() => {
                  void afterCurrent(current, true).catch(error => setError(String(error)));
                  setMessage(tr("已跳过，仍为未审核", "Skipped; still unreviewed"));
                }}>{tr("跳过 S", "Skip S")}</button>}
              {current.decision === "unreviewed" && !queueMode && <button onClick={() => {
                setFilter("unreviewed"); setDrawerOpen(false);
              }}>{tr("进入待审核队列", "Open unreviewed queue")}</button>}
            </>
            : <>
              <button className="primary" disabled={working || !chosenCode || summary?.read_only}
                onClick={() => void saveLabel()}>{tr("保存标签", "Save label")}</button>
              <button disabled={working || summary?.read_only} onClick={() => setResetOpen(true)}>
                {tr("退回质量审核", "Return to quality review")}</button>
            </>}
        </div>}
      </section>
    </section>
    {rejectOpen && <div className="synthetic-dialog-backdrop">
      <div className="synthetic-reject panel" role="dialog" aria-modal="true"
        aria-label={tr("拒绝原因", "Rejection reason")}>
        <strong>{tr("选择拒绝原因", "Choose rejection reasons")}</strong>
        <p>{tr("可多选；若列表不适用，可在下方填写其他原因。", "Select any that apply, or enter another reason below.")}</p>
        <div className="synthetic-reasons">{reasons.map(reason =>
          <label className={reasonCodes.includes(reason.code) ? "selected" : ""} key={reason.code}><input type="checkbox" checked={reasonCodes.includes(reason.code)}
            onChange={event => setReasonCodes(previous => event.target.checked
              ? [...previous, reason.code] : previous.filter(code => code !== reason.code))} />{reason.name}</label>)}</div>
        <label className="synthetic-dialog-field">{tr("其他原因或补充说明", "Other reason or details")}</label>
        <textarea value={reasonNote} onChange={event => setReasonNote(event.target.value)}
          placeholder={tr("补充说明（可选）", "Additional details (optional)")} />
        <div><button className="primary" disabled={working || (!reasonCodes.length && !reasonNote.trim())}
          onClick={() => void decide("reject")}>{tr("确认拒绝 Enter", "Confirm reject Enter")}</button>
          <button onClick={() => setRejectOpen(false)}>{tr("取消", "Cancel")}</button></div>
      </div>
    </div>}
    {resetOpen && <div className="synthetic-dialog-backdrop">
      <div className="synthetic-reject panel" role="dialog" aria-modal="true"
        aria-label={tr("撤回审核结果", "Return review to unreviewed")}>
        <strong>{view === "labels" ? tr("退回质量审核", "Return to quality review")
          : tr("撤回为未审核", "Return to unreviewed")}</strong>
        <p>{tr("将重新进入审核队列。原审核记录和已有快照不会改变；正式标签会保留。", "The clip returns to the review queue. Existing revisions, snapshots, and its formal label remain.")}</p>
        <textarea value={resetNote} onChange={event => setResetNote(event.target.value)}
          placeholder={tr("撤回说明（可选）", "Reason (optional)")} />
        <div><button className="primary" disabled={working}
          onClick={() => void (view === "labels" ? returnFromLabels() : decide("unreviewed"))}>
          {view === "labels" ? tr("确认退回", "Confirm return") : tr("确认撤回", "Confirm return")}</button>
          <button onClick={() => setResetOpen(false)}>{tr("取消", "Cancel")}</button></div>
      </div>
    </div>}
  </main>;
}
