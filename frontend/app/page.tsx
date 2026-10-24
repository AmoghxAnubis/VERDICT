"use client";

import { useEffect, useRef, useState } from "react";
import { fullTime, human, percent, price, short, time } from "../lib/format";
import type { Registry, Round, Snapshot, Source } from "../lib/types";
import { useVerdict } from "../lib/use-verdict";

type View = "Overview" | "Sources" | "History" | "Dependencies" | "Alerts" | "Simulations";
const views: View[] = ["Overview", "Sources", "History", "Dependencies", "Alerts", "Simulations"];
const glyphs: Record<string, string> = {
  Overview: "M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z",
  Sources: "M12 3 3 8l9 5 9-5-9-5z M3 12l9 5 9-5 M3 16l9 5 9-5",
  History: "M3 11a9 9 0 1 1 2 7 M3 4v7h7 M12 7v5l3 2",
  Dependencies: "M12 8V3 M5 21v-5h14v5 M12 12v4 M9 8h6v4H9z",
  Alerts: "M10 3h4l8 17H2L10 3z M12 8v5 M12 16v1",
  Simulations: "M9 3h6 M10 3v7L4 20h16l-6-10V3 M8 15h8",
  Arrow: "M5 12h14 M13 6l6 6-6 6", Refresh: "M20 7v5h-5 M4 17v-5h5 M19 11a7 7 0 0 0-12-5 M5 13a7 7 0 0 0 12 5",
};
function Icon({ name, size = 18 }: { name: string; size?: number }) {
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={glyphs[name] || glyphs.Overview} /></svg>;
}
function Badge({ children, tone = "neutral" }: { children: React.ReactNode; tone?: string }) {
  return <span className={`badge ${tone}`}><i />{children}</span>;
}
function Empty({ title, children }: { title: string; children: React.ReactNode }) {
  return <div className="empty"><span className="empty-symbol">◇</span><h3>{title}</h3><p>{children}</p></div>;
}
function SourceMark({ source }: { source: string }) {
  return <span className={`source-mark mark-${source}`}>{source === "uniswap_v3" ? "U" : human(source).slice(0, 1)}</span>;
}
function Download({ data }: { data: Snapshot }) {
  return <button className="button secondary" onClick={() => {
    const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }));
    const a = document.createElement("a"); a.href = url; a.download = `verdict-${data.assessment?.report_id || "snapshot"}.json`; a.click(); URL.revokeObjectURL(url);
  }}>Export evidence <span aria-hidden="true">↗</span></button>;
}

function PriceChart({ rounds, pinned }: { rounds: Round[]; pinned?: string }) {
  const ordered = [...rounds].reverse();
  const points = ordered.map((r, i) => ({ r, i, value: r.candidate_value == null ? null : Number(r.candidate_value) }));
  const values = points.flatMap(p => p.value == null ? [] : [p.value]);
  if (!values.length) return <Empty title="No diagnostic candidate yet">A retained assessment with allocated source weight will appear here.</Empty>;
  const min = Math.min(...values), max = Math.max(...values);
  const padding = Math.max((max - min) * .18, max * .0002, .01);
  const lower = min - padding, upper = max + padding;
  const x = (i: number) => 60 + i * 650 / Math.max(points.length - 1, 1);
  const y = (v: number) => 218 - (v - lower) * 176 / (upper - lower);
  let path = "", drawing = false;
  points.forEach(p => { if (p.value == null) { drawing = false; return; } path += `${drawing ? "L" : "M"}${x(p.i)} ${y(p.value)} `; drawing = true; });
  const selected = points.find(p => p.r.report_id === pinned);
  return <div className="chart"><svg viewBox="0 0 750 270" role="img" aria-label="Diagnostic candidate history, not a usable published feed">
    <defs><linearGradient id="area" x1="0" x2="0" y1="0" y2="1"><stop offset="0%" stopColor="#57d9c0" stopOpacity=".18" /><stop offset="100%" stopColor="#57d9c0" stopOpacity="0" /></linearGradient></defs>
    {[0, 1, 2, 3].map(i => { const value = upper - (upper - lower) * i / 3; const py = y(value); return <g key={i}><line x1="60" x2="715" y1={py} y2={py} stroke="#27333d" strokeDasharray="3 5" /><text x="2" y={py + 4} fill="#7e919c" fontSize="11">{value.toFixed(max > 1000 ? 0 : 2)}</text></g>; })}
    <path d={path} fill="none" stroke="#63e1c5" strokeWidth="2.6" strokeLinejoin="round" />
    {selected?.value != null && <><line x1={x(selected.i)} x2={x(selected.i)} y1="30" y2="226" stroke="#a6b5bd" strokeDasharray="3 4" /><circle cx={x(selected.i)} cy={y(selected.value)} r="5" fill="#63e1c5" stroke="#14232c" strokeWidth="3" /></>}
    {[0, Math.floor((points.length - 1) / 2), points.length - 1].filter((n, i, a) => a.indexOf(n) === i).map(i => <text key={i} x={x(i)} y="253" textAnchor="middle" fill="#7e919c" fontSize="11">{time(points[i].r.cutoff)}</text>)}
  </svg></div>;
}

function SourceTable({ data, select }: { data: Snapshot; select: (source: Source) => void }) {
  return <div className="table-scroll"><table><thead><tr><th>Source</th><th>Observed price</th><th>Source state</th><th>Quality</th><th>Voting weight</th><th><span className="sr-only">Details</span></th></tr></thead><tbody>
    {data.sources.map(s => <tr key={s.source}><td><button className="source-name" onClick={() => select(s)}><SourceMark source={s.source} /><span>{human(s.source)}<small>{["chainlink", "pyth"].includes(s.source) ? "Oracle network" : s.source === "uniswap_v3" ? "Decentralized exchange" : "Centralized exchange"}</small></span></button></td>
      <td className="mono">{price(s.value)}</td><td><Badge tone={s.state === "active" ? "good" : s.state === "quarantined" ? "warning" : "neutral"}>{human(s.state)}</Badge></td>
      <td><div className="quality-cell"><span>{percent(s.score_bps)}</span><div className="mini-bar"><i style={{ width: `${(s.score_bps || 0) / 100}%` }} /></div></div></td>
      <td className="mono">{percent(s.weight_bps)}</td><td><button className="icon-button" aria-label={`View ${human(s.source)} details`} onClick={() => select(s)}><Icon name="Arrow" size={16} /></button></td></tr>)}
  </tbody></table></div>;
}

function Gates({ data, compact = false }: { data: Snapshot; compact?: boolean }) {
  const gates = data.assessment?.gates;
  if (!gates) return <Empty title="Awaiting assessment">Availability checks appear after a diagnostic round is retained.</Empty>;
  return <div className={compact ? "gate-list compact" : "gate-list"}>{gates.map(g => <div className="gate" key={g.name}><span className={`gate-icon ${g.passed ? "pass" : "fail"}`}>{g.passed ? "✓" : "!"}</span><div><strong>{human(g.name)}</strong>{!compact && <small>{g.observed == null ? "Evidence unknown" : `${g.observed} ${g.comparison === "gte" ? "≥" : "≤"} ${g.required}`} · {human(g.reason)}</small>}</div><span className={g.passed ? "text-good" : "text-warning"}>{g.passed ? "Pass" : g.reason === "unknown" ? "Unknown" : "Blocked"}</span></div>)}</div>;
}

function DependencyGraph({ registry }: { registry: Registry | null }) {
  const [selected, setSelected] = useState<string | null>(null);
  if (!registry) return <Empty title="No round-linked dependency review">The selected assessment will pin its exact registry version here.</Empty>;
  const nodes = registry.nodes;
  const groups = [nodes.filter(n => n.kind === "source"), nodes.filter(n => ["market", "feed", "protocol"].includes(n.kind)), nodes.filter(n => !["source", "market", "feed", "protocol", "unknown"].includes(n.kind)), nodes.filter(n => n.kind === "unknown")];
  const height = Math.max(440, ...groups.map(g => g.length * 48 + 70));
  const positions = new Map<string, { x: number; y: number }>();
  groups.forEach((group, col) => group.forEach((node, i) => positions.set(node.id, { x: 40 + col * 290, y: 60 + i * (height - 110) / Math.max(group.length - 1, 1) })));
  const focused = nodes.find(n => n.id === selected);
  return <><div className="graph-legend"><span><i className="legend-dot" />Recorded dependency</span><span><i className="legend-dot amber" />Unresolved upstream</span><span>Click a node to inspect</span></div><div className="graph-scroll"><svg width="1160" height={height} viewBox={`0 0 1160 ${height}`} aria-label="Recorded source dependency graph" role="group">
    {registry.edges.map((edge, i) => { const a = positions.get(edge.downstream), b = positions.get(edge.upstream); if (!a || !b) return null; const related = selected == null || [edge.downstream, edge.upstream].includes(selected); return <path key={i} d={`M${a.x + 180} ${a.y + 16} C${a.x + 230} ${a.y + 16},${b.x - 55} ${b.y + 16},${b.x} ${b.y + 16}`} fill="none" stroke={edge.relationship === "unknown" ? "#b78849" : "#4b7378"} strokeWidth={related && selected ? 2.5 : 1} strokeDasharray={edge.relationship === "unknown" ? "4 4" : undefined} opacity={related ? .7 : .15} />; })}
    {nodes.map(node => { const p = positions.get(node.id)!; return <g key={node.id} transform={`translate(${p.x} ${p.y})`} role="button" tabIndex={0} aria-label={`Inspect ${node.id}`} onClick={() => setSelected(node.id)} onKeyDown={e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setSelected(node.id); } }} className="graph-node"><rect width="180" height="33" rx="6" fill={node.id === selected ? "#224b47" : "#152630"} stroke={node.kind === "unknown" ? "#87683d" : "#35525e"} /><circle cx="13" cy="16" r="3" fill={node.kind === "unknown" ? "#dfb877" : "#71d6c2"} /><text x="24" y="21" fill="#d4e0e5" fontSize="11">{node.id.length > 24 ? node.id.slice(0, 23) + "…" : node.id}</text></g>; })}
  </svg></div><div className="graph-inspection"><strong>{focused?.id || `Registry · ${registry.version}`}</strong><p>{focused?.description || "Known relationships remain visible. Missing relationships and unknown upstreams do not establish independent domains."}</p>{focused && <small>Evidence references: {focused.evidence_ids.join(", ")}</small>}</div></>;
}

function SourceDialog({ source, data, close }: { source: Source | null; data: Snapshot; close: () => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => { if (source) ref.current?.showModal(); else ref.current?.close(); }, [source]);
  return <dialog ref={ref} className="source-dialog" onCancel={close} onClick={e => { if (e.target === ref.current) close(); }} aria-label={source ? `${human(source.source)} source details` : "Source details"}>
    {source && <div className="drawer-inner"><div className="drawer-heading"><span className="eyebrow">SOURCE EXPLORER</span><button className="icon-button" onClick={close} aria-label="Close source details">✕</button></div><div className="drawer-source"><SourceMark source={source.source} /><div><h2>{human(source.source)}</h2><Badge tone={source.state === "active" ? "good" : "warning"}>{human(source.state)}</Badge></div></div><p className="subtle">Pinned to {fullTime(data.as_of)}</p><div className="drawer-price">{price(source.value)}<small>Observed ETH / USD price</small></div><dl className="detail-list"><div><dt>Allocated voting weight</dt><dd>{percent(source.weight_bps)}</dd></div><div><dt>Diagnostic source quality</dt><dd>{percent(source.score_bps)}</dd></div><div><dt>Observation freshness</dt><dd>{human(source.availability)}</dd></div><div><dt>Age at cutoff</dt><dd>{source.age_seconds == null ? "Unknown" : `${Number(source.age_seconds).toFixed(2)} s`}</dd></div><div><dt>Peer comparison</dt><dd>{human(source.comparison || "unscored")}</dd></div><div><dt>Peer median</dt><dd>{price(source.peer_median)}</dd></div><div><dt>Deviation threshold</dt><dd>{price(source.deviation_threshold)}</dd></div></dl>
      <h3>Decision evidence</h3><ul className="reason-list">{[...source.reasons, ...(source.score_reasons || [])].filter((r, i, a) => a.indexOf(r) === i).map(r => <li key={r}>{human(r)}</li>)}</ul>{!source.reasons.length && <p className="subtle">No current source exclusions. Publication gates still apply.</p>}<h3>Unresolved upstreams</h3><p className="subtle">{source.unknown_dependencies.length ? source.unknown_dependencies.join(", ") : "None declared in this graph. Independence remains unproven."}</p><h3>Retained identifiers</h3><dl className="id-list"><dt>Observation</dt><dd>{source.observation_id || "None selected"}</dd><dt>Assessment</dt><dd>{data.assessment?.report_id || "No assessment"}</dd><dt>Scoring report</dt><dd>{data.scoring_report_id || "No scoring report"}</dd></dl>{data.scoring_report_id && <a className="text-link" href={`/api/verdict/scores/${data.scoring_report_id}?dataset=${encodeURIComponent(data.dataset)}`} target="_blank" rel="noreferrer">Open complete scoring evidence ↗</a>}
    </div>}
  </dialog>;
}

export default function Dashboard() {
  const [view, setView] = useState<View>("Overview"), [dataset, setDataset] = useState("live");
  const [datasets, setDatasets] = useState<string[]>(["live"]), [pinned, setPinned] = useState<string | null>(null);
  const [selected, setSelected] = useState<Source | null>(null), [refresh, setRefresh] = useState(0);
  const [playing, setPlaying] = useState(false), [search, setSearch] = useState("");
  const { data, status, error } = useVerdict(dataset, pinned, refresh);
  const historyRef = useRef<Round[]>([]);
  if (data) historyRef.current = data.history;
  useEffect(() => {
    let active = true;
    fetch("/api/verdict/datasets", { cache: "no-store" }).then(r => r.ok ? r.json() : ["live"]).then(list => { if (active) setDatasets(list); }).catch(() => {});
    return () => { active = false; };
  }, [refresh, dataset]);
  useEffect(() => {
    if (!playing) return;
    const timer = setInterval(() => {
      setPinned(current => {
        const rounds = [...historyRef.current].reverse();
        const index = rounds.findIndex(r => r.report_id === current);
        if (index >= rounds.length - 1) { setPlaying(false); return current; }
        return rounds[index + 1]?.report_id || null;
      });
    }, 1500);
    return () => clearInterval(timer);
  }, [playing]);
  function switchDataset(value: string) { setDataset(value); setPinned(null); setSelected(null); setPlaying(false); }
  const replay = dataset !== "live", assessment = data?.assessment;
  const activeSources = data?.sources.filter(s => s.weight_bps > 0).length || 0;
  const failed = assessment?.gates.filter(g => !g.passed) || [];
  const orderedHistory = [...(data?.history || [])].reverse();
  const replayIndex = pinned ? orderedHistory.findIndex(r => r.report_id === pinned) : orderedHistory.length - 1;
  return <div className="app-shell">
    <a className="skip-link" href="#main">Skip to content</a>
    <aside className="sidebar"><a className="brand" href="/" aria-label="VERDICT home"><span className="brand-symbol">V</span>VERDICT<span className="brand-dot">.</span></a><div className="workspace"><span className="workspace-icon">◈</span><div>Oracle intelligence<small>ETH / USD workspace</small></div></div><p className="nav-label">WORKSPACE</p><nav aria-label="Main navigation">{views.map(v => <button key={v} aria-current={view === v ? "page" : undefined} className={view === v ? "nav-item active" : "nav-item"} onClick={() => { setView(v); setSelected(null); }}><Icon name={v} /><span>{v}</span>{v === "Alerts" && failed.length > 0 && <span className="nav-count">{failed.length}</span>}</button>)}</nav><div className="sidebar-bottom"><div className="node-info"><span className="node-orb" /><div>Development node<small>Publication disabled</small></div></div><a href="http://127.0.0.1:8000/docs" target="_blank" rel="noreferrer" className="docs-link">API documentation <span>↗</span></a><div className="version">VERDICT CORE <span>v0.1</span></div></div></aside>
    <div className="main-shell"><header className="topbar"><div className="breadcrumb">Workspace <span>/</span> <strong>{view}</strong></div><div className="connection"><span className={status === "Connected" ? "connection-dot connected" : "connection-dot"} />{status}<span className="topbar-divider" /><span className="mono">ETH-USD</span></div></header>
      <main id="main"><div className="page-heading"><div><div className="eyebrow">THE TRUST LAYER FOR OFF-CHAIN DATA</div><h1>{view === "Overview" ? "Feed overview" : view === "Simulations" ? "Simulation lab" : view === "Dependencies" ? "Dependency explorer" : view === "Alerts" ? "Availability & alerts" : view === "History" ? "Round history" : "Data sources"}</h1><p className="page-description">{view === "Overview" ? "Every source. Every decision. The evidence behind the price." : view === "Simulations" ? "Explore retained scenarios without writing to the live feed." : "Inspect the evidence behind each diagnostic round."}</p></div><div className="heading-actions"><button className="icon-button bordered" aria-label="Refresh dashboard" onClick={() => setRefresh(n => n + 1)}><Icon name="Refresh" /></button>{data && <Download data={data} />}</div></div>
      <div className="dataset-bar"><div className="mode-switch" aria-label="Data mode"><button className={!replay ? "selected" : ""} onClick={() => switchDataset("live")}><i />Live observations</button><button className={replay ? "selected replay" : ""} disabled={datasets.length === 1} onClick={() => switchDataset(datasets.find(d => d.startsWith("replay:adv-synthetic-")) || datasets.find(d => d !== "live") || "live")}><Icon name="Simulations" size={14} />Synthetic replay</button></div>{replay && <label className="dataset-select"><span className="sr-only">Replay scenario</span><select value={dataset} onChange={e => switchDataset(e.target.value)}>{datasets.filter(d => d !== "live").map(d => <option value={d} key={d}>{human(d.replace("replay:adv-synthetic-", ""))}</option>)}</select></label>}<span className="cutoff">{data ? `${replay ? "Replay cutoff" : "Assessment as of"} ${time(data.as_of)} UTC` : "Loading evidence…"}</span></div>
      {replay && <div className="replay-banner"><Icon name="Simulations" /><strong>Synthetic replay</strong><span>Test policies and fixture data · isolated from live observations · publication disabled</span></div>}
      {error && <div className="error-banner" role="alert">{error}<button className="text-link" onClick={() => setRefresh(n => n + 1)}>Retry now</button></div>}
      {!data && !error && <div className="loading" role="status"><span className="spinner" />Loading retained evidence…</div>}
      {data && <>
        {(view === "Overview" || view === "Simulations") && <>
          {view === "Simulations" && <section className="card simulation-controls"><div><span className="eyebrow">REPLAY CONTROLS</span><h2>{replay ? human(dataset.replace("replay:adv-synthetic-", "")) : "Choose a synthetic replay above"}</h2><p>Scrub actual retained assessments to inspect attack, exclusion, and recovery.</p></div>{replay && orderedHistory.length > 0 && <div className="playback"><button className="button primary" onClick={() => { if (!playing && replayIndex >= orderedHistory.length - 1) setPinned(orderedHistory[0].report_id); setPlaying(v => !v); }}>{playing ? "Pause replay" : "Play replay"}</button><label><span className="sr-only">Replay round</span><input type="range" min="0" max={orderedHistory.length - 1} value={Math.max(0, replayIndex)} onChange={e => { setPlaying(false); setPinned(orderedHistory[Number(e.target.value)].report_id); }} /></label><span className="mono">{Math.max(0, replayIndex) + 1} / {orderedHistory.length}</span></div>}</section>}
          <div className="metrics-grid"><section className="metric-card feed-metric"><div className="metric-label">ETH / USD <Badge tone="warning">Unavailable</Badge></div><div className="metric-value">—<span>USD</span></div><p>Usable feed value withheld</p><small>Independent upstream evidence required</small></section><section className="metric-card"><div className="metric-label">Diagnostic candidate <span className="metric-icon">↗</span></div><div className="metric-value smaller">{price(assessment?.candidate_value)}</div><p>Weighted median · for inspection only</p><small>{assessment ? `${short(assessment.report_id)} · retained round` : "No retained assessment"}</small></section><section className="metric-card"><div className="metric-label">Contributing sources <span className="metric-icon">◈</span></div><div className="metric-value">{activeSources}<span>/ 6</span></div><p>Sources with allocated weight</p><small>{data.sources.filter(s => s.state === "quarantined").length} quarantined · {data.sources.filter(s => s.availability === "fresh").length} fresh at cutoff</small></section><section className="metric-card"><div className="metric-label">Diagnostic quality <span className="metric-icon">◷</span></div><div className="metric-value">{assessment ? (assessment.quality_score_bps / 100).toFixed(1) : "—"}<span>{assessment ? "/ 100" : ""}</span></div><p>Uncalibrated quality indicator</p><small>Does not imply probability of truth</small></section></div>
          <div className="overview-grid"><section className="card chart-card"><div className="card-heading"><div><h2>Diagnostic price history</h2><p>Retained candidates · USD · UTC</p></div><span className="chart-key"><i />Weighted median</span></div><PriceChart rounds={data.history} pinned={assessment?.report_id} /><div className="chart-footer"><span>◷ {data.history.length} retained rounds</span><span>Usable feed remains unavailable</span></div></section><section className="card availability-card"><div className="card-heading"><div><h2>Publication readiness</h2><p>Every availability gate must pass</p></div><span className="lock-icon">◇</span></div><div className="readiness-status"><span className="gate-icon fail">!</span><div><strong>Output withheld</strong><small>{assessment ? `${failed.length} of ${assessment.gates.length} checks blocked` : "No assessment retained"}</small></div></div>{assessment ? <><div className="gate-progress">{assessment.gates.map(g => <i key={g.name} title={human(g.name)} className={g.passed ? "passed" : ""} />)}</div><div className="blocking-summary">{failed.slice(0, 3).map(g => <div key={g.name}><span>{human(g.name)}</span><span className="text-warning">{g.reason === "unknown" ? "Unknown" : "Blocked"}</span></div>)}</div><button className="text-link" onClick={() => setView("Alerts")}>Inspect availability checks <Icon name="Arrow" size={14} /></button></> : <p className="subtle">Source observations alone cannot establish a usable feed. Retain a scored, weighted assessment to inspect availability.</p>}</section></div>
          <section className="card source-card"><div className="card-heading"><div><h2>Source intelligence <span className="count-chip">6</span></h2><p>Quality, allocation, and exclusions at the selected cutoff</p></div><button className="text-link" onClick={() => setView("Sources")}>Explore sources <Icon name="Arrow" size={14} /></button></div><SourceTable data={data} select={setSelected} /></section>
        </>}
        {view === "Sources" && <section className="card"><div className="card-heading"><div><h2>Six providers. Explicit evidence.</h2><p>Select a source to inspect its comparison, weight, and exclusion reasons.</p></div><Badge>{data.mode}</Badge></div><SourceTable data={data} select={setSelected} /><div className="card-note">Provider count does not establish independent upstream domains. Source quality measures agreement and availability.</div></section>}
        {view === "History" && <section className="card"><div className="card-heading"><div><h2>Immutable assessment history</h2><p>Open any retained round without changing the live selection.</p></div><label className="search"><span className="sr-only">Filter round history</span><input value={search} onChange={e => setSearch(e.target.value)} placeholder="Filter by round ID…" /></label></div>{data.history.length ? <div className="table-scroll"><table><thead><tr><th>Cutoff · UTC</th><th>Assessment</th><th>Diagnostic candidate</th><th>Quality</th><th>Usable output</th><th /></tr></thead><tbody>{data.history.filter(r => r.report_id.includes(search)).map(r => <tr key={r.report_id} className={pinned === r.report_id ? "selected-row" : ""}><td className="mono">{time(r.cutoff)}</td><td className="mono" title={r.report_id}>{short(r.report_id)}</td><td className="mono">{price(r.candidate_value)}</td><td>{percent(r.quality_score_bps)}</td><td><Badge tone="warning">Withheld</Badge></td><td><button className="text-link" onClick={() => { setPinned(r.report_id); setView("Overview"); }}>Inspect round ↗</button></td></tr>)}</tbody></table></div> : <Empty title="No retained assessments">History will appear when the diagnostic pipeline saves a complete assessment.</Empty>}</section>}
        {view === "Dependencies" && <section className="card"><div className="card-heading"><div><h2>Follow the dependency paths</h2><p>Economic, delivery, chain, conversion, and unresolved relationships</p></div><Badge tone="warning">Independence unknown</Badge></div><DependencyGraph key={data.registry?.registry_id || "empty"} registry={data.registry} /></section>}
        {view === "Alerts" && <div className="alerts-grid"><section className="card"><div className="card-heading"><div><h2>Availability checks</h2><p>Quality alone never overrides missing evidence.</p></div><Badge tone="warning">{failed.length} blocked</Badge></div><Gates data={data} /></section><section className="card"><div className="card-heading"><div><h2>Source decisions</h2><p>Current exclusions and recovery requirements</p></div></div>{data.sources.filter(s => s.reasons.length).map(s => <button key={s.source} className="source-alert" onClick={() => setSelected(s)}><SourceMark source={s.source} /><div><strong>{human(s.source)}</strong><p>{s.reasons.map(human).join(" · ")}</p></div><Icon name="Arrow" size={16} /></button>)}{data.sources.every(s => !s.reasons.length) && <Empty title="No current source exclusions">The selected round still requires every publication gate to pass.</Empty>}</section></div>}
        {pinned && <div className="pinned-bar"><span>Inspecting retained round <code>{short(pinned)}</code> · {fullTime(data.as_of)}</span><button className="text-link" onClick={() => { setPinned(null); setPlaying(false); }}>Return to latest round ↗</button></div>}
        <footer className="page-footer"><span><span className="brand-mini">V</span> Evidence before confidence.</span><span>ETH-USD · {replay ? "Isolated synthetic dataset" : "Live dataset"} · Publication disabled</span></footer>
        <SourceDialog source={selected} data={data} close={() => setSelected(null)} />
      </>}
      </main></div>
  </div>;
}
