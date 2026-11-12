import { useCallback, useEffect, useMemo, useState, type FormEvent, type ReactNode } from "react";

type Metrics = Record<string, unknown>;
type Candidate = {
  model_version: string;
  state: "pending_approval" | "approved" | "rejected";
  created_at: string;
  approved_at?: string | null;
  approved_by?: string | null;
  dataset_version: string;
  manifest: {
    run_id: string;
    primary_metric: string;
    threshold: number;
    candidate_count: number;
    release_gates: Record<string, boolean>;
    holdout_metrics: Metrics;
    validation_metrics: Metrics;
  };
};
type Overview = {
  environment: string;
  schema_version: string;
  user: { email: string; role: string };
  active_model: Candidate | null;
  pending_approval_count: number;
  model_count: number;
  training_mode: string;
  metrics_note: string;
};
type Run = {
  run_id: string;
  status: string;
  created_at: string;
  dataset_version: string;
  selection?: { selected_candidate?: string; selected_validation_score?: number; beats_no_skill_baseline?: boolean };
  candidate_comparison?: Record<string, { validation_metrics?: Metrics }>;
};
type Monitor = {
  request_count: number;
  error_rate: number;
  active_model_version?: string | null;
  drift_signals: unknown[];
  signal_limitations: string[];
};
type Quota = { month: string; limit: number; requests: number; remaining: number };
type Prediction = {
  customer_id: string;
  churn_probability: number;
  risk_band: "low" | "watch" | "high";
  threshold: number;
  model_version: string;
  factors: { feature: string; contribution: number; direction: string }[];
};

type Page = "Overview" | "Score a customer" | "Model registry" | "Training runs" | "Monitoring";

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init);
  const contentType = response.headers.get("content-type") ?? "";
  const data = contentType.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) {
    const detail = typeof data === "object" && data !== null && "detail" in data ? (data as { detail: unknown }).detail : data;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return data as T;
}

function percent(value: unknown, digits = 1): string {
  return typeof value === "number" && Number.isFinite(value) ? `${(value * 100).toFixed(digits)}%` : "—";
}

function score(value: unknown): string {
  return typeof value === "number" && Number.isFinite(value) ? value.toFixed(3) : "—";
}

function time(value?: string): string {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

function App() {
  const [page, setPage] = useState<Page>("Overview");
  const [overview, setOverview] = useState<Overview | null>(null);
  const [models, setModels] = useState<Candidate[]>([]);
  const [runs, setRuns] = useState<Run[]>([]);
  const [monitor, setMonitor] = useState<Monitor | null>(null);
  const [quota, setQuota] = useState<Quota | null>(null);
  const [busy, setBusy] = useState(true);
  const [notice, setNotice] = useState<{ kind: "error" | "success"; text: string } | null>(null);
  const [prediction, setPrediction] = useState<Prediction | null>(null);
  const [predictionBusy, setPredictionBusy] = useState(false);
  const [candidateManifest, setCandidateManifest] = useState<File | null>(null);
  const [candidateArtifact, setCandidateArtifact] = useState<File | null>(null);
  const [uploadBusy, setUploadBusy] = useState(false);
  const [decisionTarget, setDecisionTarget] = useState<Candidate | null>(null);
  const [decision, setDecision] = useState<"approve" | "reject" | "rollback">("approve");
  const [decisionReason, setDecisionReason] = useState("");
  const [decisionBusy, setDecisionBusy] = useState(false);
  const [form, setForm] = useState({
    customer_id: "SYN-DEMO-0001",
    snapshot_date: "2025-01-31",
    tenure_months: 12,
    contract_type: "Month-to-month",
    monthly_charges: 78.5,
    total_charges: 942,
    internet_service: "Fiber optic",
    tech_support: false,
    usage_gb_30d: 128,
    support_calls_90d: 3,
    payment_method: "Electronic check",
    autopay_enabled: false,
  });

  const refresh = useCallback(async () => {
    setBusy(true);
    setNotice(null);
    const overviewResult = await api<Overview>("/v1/overview").then((value) => {
      setOverview(value);
      return value;
    });
    const role = overviewResult.user.role;
    const operationsAllowed = ["ml_operator", "approver", "admin"].includes(role);
    if (operationsAllowed) {
      const [modelResult, runsResult, monitorResult] = await Promise.allSettled([
        api<Candidate[]>("/v1/models"),
        api<Run[]>("/v1/training-runs"),
        api<Monitor>("/v1/monitoring/summary"),
      ]);
      if (modelResult.status === "fulfilled") setModels(modelResult.value);
      if (runsResult.status === "fulfilled") setRuns(runsResult.value);
      if (monitorResult.status === "fulfilled") setMonitor(monitorResult.value);
    } else {
      setModels([]);
      setRuns([]);
      setMonitor(null);
    }
    try {
      setQuota(await api<Quota>("/v1/quota"));
    } catch {
      setQuota(null);
    }
    setBusy(false);
  }, []);

  useEffect(() => {
    refresh().catch((error: unknown) => {
      setBusy(false);
      setNotice({ kind: "error", text: error instanceof Error ? error.message : "Could not connect to the project API." });
    });
  }, [refresh]);

  const activeModel = overview?.active_model ?? null;
  const pendingModels = useMemo(() => models.filter((model) => model.state === "pending_approval"), [models]);
  const canOperate = ["ml_operator", "approver", "admin"].includes(overview?.user.role ?? "");
  const canApprove = ["approver", "admin"].includes(overview?.user.role ?? "");

  async function submitPrediction(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setPredictionBusy(true);
    setPrediction(null);
    setNotice(null);
    try {
      const result = await api<Prediction>("/v1/predictions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ customer: form }),
      });
      setPrediction(result);
      void refresh();
    } catch (error) {
      setNotice({ kind: "error", text: error instanceof Error ? error.message : "The snapshot could not be scored." });
    } finally {
      setPredictionBusy(false);
    }
  }

  async function uploadCandidate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!candidateManifest || !candidateArtifact) {
      setNotice({ kind: "error", text: "Choose the candidate manifest and model artifact first." });
      return;
    }
    setUploadBusy(true);
    setNotice(null);
    const formData = new FormData();
    formData.append("manifest_json", await candidateManifest.text());
    formData.append("artifact", candidateArtifact);
    try {
      await api("/v1/models/candidates", { method: "POST", body: formData });
      setNotice({ kind: "success", text: "Candidate registered. A separate approver must review it before it can serve predictions." });
      setCandidateManifest(null);
      setCandidateArtifact(null);
      await refresh();
    } catch (error) {
      setNotice({ kind: "error", text: error instanceof Error ? error.message : "Candidate upload failed." });
    } finally {
      setUploadBusy(false);
    }
  }

  async function submitDecision(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!decisionTarget || decisionReason.trim().length < 8) return;
    setDecisionBusy(true);
    setNotice(null);
    const path = decision === "rollback"
      ? "/v1/models/rollback"
      : `/v1/models/${encodeURIComponent(decisionTarget.model_version)}/approval`;
    const body = decision === "rollback"
      ? { model_version: decisionTarget.model_version, reason: decisionReason }
      : { decision, reason: decisionReason };
    try {
      await api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      setNotice({ kind: "success", text: decision === "approve" ? "Model approved and promoted." : decision === "reject" ? "Candidate rejected and recorded." : "Approved model restored." });
      setDecisionTarget(null);
      setDecisionReason("");
      await refresh();
    } catch (error) {
      setNotice({ kind: "error", text: error instanceof Error ? error.message : "The model decision could not be saved." });
    } finally {
      setDecisionBusy(false);
    }
  }

  const nav: { label: Page; icon: string; group: string }[] = [
    { label: "Overview", icon: "◫", group: "WORKSPACE" },
    { label: "Score a customer", icon: "⌕", group: "WORKSPACE" },
    { label: "Model registry", icon: "◇", group: "GOVERNANCE" },
    { label: "Training runs", icon: "▤", group: "GOVERNANCE" },
    { label: "Monitoring", icon: "⌁", group: "GOVERNANCE" },
  ];

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">C<span>+</span></div>
          <div><strong>Churn Intelligence</strong><small>TELECOM ANALYTICS</small></div>
        </div>
        <div className="side-label">PLATFORM</div>
        {nav.map((item, index) => (
          <div key={item.label}>
            {(index === 0 || item.group !== nav[index - 1].group) && <div className="nav-group">{item.group}</div>}
            <button className={`nav-item ${page === item.label ? "selected" : ""}`} onClick={() => setPage(item.label)}>
              <span className="nav-icon">{item.icon}</span><span>{item.label}</span>
              {item.label === "Model registry" && pendingModels.length > 0 && <span className="nav-count">{pendingModels.length}</span>}
            </button>
          </div>
        ))}
        <div className="sidebar-bottom">
          <div className="env-pill"><span className="status-dot" />{overview?.environment ?? "local"} environment</div>
          <div className="user-chip"><div className="avatar">{overview?.user.email?.slice(0, 1).toUpperCase() ?? "C"}</div><div className="user-copy"><strong>{overview?.user.email ?? "Connecting…"}</strong><small>{overview?.user.role ?? ""}</small></div><span className="more">···</span></div>
        </div>
      </aside>

      <main className="main-area">
        <header className="topbar">
          <div className="breadcrumbs"><span>Platform</span><b>/</b><strong>{page}</strong></div>
          <div className="top-actions">
            <span className="synthetic-tag"><i /> Synthetic demo</span>
            <button className="icon-button" aria-label="Refresh dashboard" onClick={() => void refresh()} title="Refresh">↻</button>
            <button className="help-button" title="Project information">?</button>
          </div>
        </header>

        <div className="page-content">
          {notice && <div className={`notice ${notice.kind}`} role="status"><span>{notice.kind === "error" ? "!" : "✓"}</span><p>{notice.text}</p><button onClick={() => setNotice(null)} aria-label="Dismiss">×</button></div>}
          {busy && !overview ? <div className="loading-card"><span className="spinner" /> Connecting to the project API…</div> : null}
          {!busy && !overview && <section className="connect-card"><div className="connect-icon">⌁</div><h2>Dashboard API is not connected</h2><p>Start the local API, or open the protected project link after deployment.</p><code>uvicorn churn_platform.api:app --port 8000</code></section>}

          {page === "Overview" && overview && <OverviewPage overview={overview} activeModel={activeModel} pending={pendingModels.length} monitor={monitor} quota={quota} models={models} go={setPage} />}
          {page === "Score a customer" && <section className="page-section">
            <PageHeading eyebrow="PREDICTION WORKSPACE" title="Score a customer snapshot" subtitle="Enter an approved synthetic snapshot. The service rejects real identifiers and training labels." />
            {!activeModel && <div className="empty-state warning"><span>!</span><div><strong>No approved model is active.</strong><p>Register a candidate in Model registry, then have an approver promote it before scoring.</p></div></div>}
            <div className="score-layout">
              <form className="panel score-form" onSubmit={submitPrediction}>
                <div className="panel-heading"><div><h3>Snapshot details</h3><p>Schema version {overview?.schema_version ?? "1.0.0"}</p></div><span className="small-badge">SYNTHETIC ONLY</span></div>
                <div className="form-grid">
                  <Field label="Synthetic customer key"><input value={form.customer_id} onChange={(e) => setForm({ ...form, customer_id: e.target.value })} required /></Field>
                  <Field label="Snapshot date"><input type="date" value={form.snapshot_date} onChange={(e) => setForm({ ...form, snapshot_date: e.target.value })} required /></Field>
                  <Field label="Tenure (months)"><input type="number" min="0" value={form.tenure_months} onChange={(e) => setForm({ ...form, tenure_months: Number(e.target.value) })} /></Field>
                  <Field label="Contract type"><select value={form.contract_type} onChange={(e) => setForm({ ...form, contract_type: e.target.value })}><option>Month-to-month</option><option>One year</option><option>Two year</option></select></Field>
                  <Field label="Monthly charges"><input type="number" min="0" step="0.01" value={form.monthly_charges} onChange={(e) => setForm({ ...form, monthly_charges: Number(e.target.value) })} /></Field>
                  <Field label="Total charges"><input type="number" min="0" step="0.01" value={form.total_charges} onChange={(e) => setForm({ ...form, total_charges: Number(e.target.value) })} /></Field>
                  <Field label="Internet service"><select value={form.internet_service} onChange={(e) => setForm({ ...form, internet_service: e.target.value })}><option>Fiber optic</option><option>DSL</option><option>None</option></select></Field>
                  <Field label="Data usage (GB / 30 days)"><input type="number" min="0" step="0.1" value={form.usage_gb_30d} onChange={(e) => setForm({ ...form, usage_gb_30d: Number(e.target.value) })} /></Field>
                  <Field label="Support calls (90 days)"><input type="number" min="0" value={form.support_calls_90d} onChange={(e) => setForm({ ...form, support_calls_90d: Number(e.target.value) })} /></Field>
                  <Field label="Payment method"><select value={form.payment_method} onChange={(e) => setForm({ ...form, payment_method: e.target.value })}><option>Electronic check</option><option>Mailed check</option><option>Bank transfer (automatic)</option><option>Credit card (automatic)</option></select></Field>
                </div>
                <div className="toggle-row">
                  <label><input type="checkbox" checked={form.tech_support} onChange={(e) => setForm({ ...form, tech_support: e.target.checked })} /><span className="toggle-ui" /><span>Tech support enabled</span></label>
                  <label><input type="checkbox" checked={form.autopay_enabled} onChange={(e) => setForm({ ...form, autopay_enabled: e.target.checked })} /><span className="toggle-ui" /><span>Automatic payments</span></label>
                </div>
                <div className="form-footer"><span>Only the approved v1 schema is accepted. Customer IDs must start with SYN-.</span><button className="primary-button" disabled={predictionBusy || !activeModel}>{predictionBusy ? "Scoring…" : "Run prediction"}<span>→</span></button></div>
              </form>
              <div className="panel result-panel">
                <div className="panel-heading"><div><h3>Prediction result</h3><p>Model behavior explanation</p></div><span className="result-spark">✳</span></div>
                {prediction ? <>
                  <div className="risk-result"><div className={`risk-score ${prediction.risk_band}`}><strong>{percent(prediction.churn_probability, 1)}</strong><span>churn probability</span></div><span className={`risk-pill ${prediction.risk_band}`}>{prediction.risk_band} risk</span></div>
                  <div className="threshold-line"><span>Review threshold</span><strong>{percent(prediction.threshold, 1)}</strong></div>
                  <div className="result-divider" />
                  <div className="panel-heading compact"><div><h3>Factors influencing this score</h3><p>Reference substitution · not causal</p></div></div>
                  <div className="factor-list">{prediction.factors.length ? prediction.factors.map((factor) => <div className="factor-row" key={factor.feature}><span className={`factor-dot ${factor.contribution > 0 ? "up" : "down"}`} /><span className="factor-name">{factor.feature.replaceAll("_", " ")}</span><span className={factor.contribution > 0 ? "factor-up" : "factor-down"}>{factor.contribution > 0 ? "+" : ""}{(factor.contribution * 100).toFixed(1)} pts</span></div>) : <div className="quiet-note">No reference explanation is available for this model.</div>}</div>
                  <div className="model-footnote">{prediction.model_version} · {prediction.customer_id}</div>
                </> : <div className="result-empty"><div className="result-illustration">◎</div><strong>Results appear here</strong><p>Run a prediction to see the score, review threshold, and model-behavior factors.</p></div>}
              </div>
            </div>
          </section>}

          {page === "Model registry" && <section className="page-section">
            <PageHeading eyebrow="MODEL GOVERNANCE" title="Model registry" subtitle="Every candidate is versioned, checksummed, and held for a separate human decision." action={<button className="outline-button" onClick={() => void refresh()}>↻ Refresh</button>} />
            <div className="registry-summary"><div className="panel stat-card"><span className="stat-label">ACTIVE MODEL</span><strong>{activeModel?.model_version ?? "None"}</strong><small>{activeModel ? `Approved ${time(activeModel.approved_at ?? undefined)}` : "No model is serving predictions"}</small></div><div className="panel stat-card"><span className="stat-label">CANDIDATES AWAITING REVIEW</span><strong>{pendingModels.length.toString().padStart(2, "0")}</strong><small>Approval is required before use</small></div><div className="panel stat-card"><span className="stat-label">MODEL ARTIFACTS</span><strong>{models.length.toString().padStart(2, "0")}</strong><small>Stored with checksum and lineage</small></div></div>
            {canOperate && <form className="panel upload-panel" onSubmit={uploadCandidate}>
              <div className="panel-heading"><div><h3>Register a trained candidate</h3><p>Upload a local run from the artifacts/models folder. Only synthetic training output is accepted.</p></div><span className="upload-glyph">↑</span></div>
              <div className="upload-controls"><label className="file-picker"><span>01</span><div><strong>{candidateManifest?.name ?? "Choose manifest.json"}</strong><small>Candidate lineage and release gates</small></div><input type="file" accept="application/json,.json" onChange={(e) => setCandidateManifest(e.target.files?.[0] ?? null)} /></label><label className="file-picker"><span>02</span><div><strong>{candidateArtifact?.name ?? "Choose model.joblib"}</strong><small>SHA-256 checked before registration</small></div><input type="file" accept=".joblib,application/octet-stream" onChange={(e) => setCandidateArtifact(e.target.files?.[0] ?? null)} /></label><button className="primary-button" disabled={uploadBusy}>{uploadBusy ? "Registering…" : "Register candidate"}<span>→</span></button></div>
              <div className="upload-note">Training runs locally. Only synthetic-data artifacts are uploaded to Cloudflare D1/R2; APAC location hints do not guarantee data residency.</div>
            </form>}
            <div className="panel table-panel"><div className="panel-heading"><div><h3>Version history</h3><p>Review candidate metrics and promotion state</p></div><span className="table-count">{models.length} models</span></div>
              {models.length ? <div className="table-scroll"><table><thead><tr><th>MODEL VERSION</th><th>STATUS</th><th>PR-AUC</th><th>ROC-AUC</th><th>THRESHOLD</th><th>RELEASE GATES</th><th>CREATED</th><th /></tr></thead><tbody>{models.map((model) => {
                const metrics = model.manifest.holdout_metrics as Record<string, unknown> | undefined;
                const gatesPass = model.manifest.release_gates?.automated_gates_pass === true;
                return <tr key={model.model_version}><td><strong className="mono">{model.model_version}</strong><small>{model.dataset_version}</small></td><td><span className={`status-pill ${model.state}`}>{model.state.replaceAll("_", " ")}</span></td><td>{score(metrics?.pr_auc)}</td><td>{score(metrics?.roc_auc)}</td><td>{percent(model.manifest.threshold, 1)}</td><td><span className={`gate-pill ${gatesPass ? "pass" : "blocked"}`}>{gatesPass ? "All passed" : "Review required"}</span></td><td>{time(model.created_at)}</td><td className="actions-cell">{canApprove && model.state === "pending_approval" && <><button className="text-action approve" onClick={() => { setDecision("approve"); setDecisionTarget(model); }}>Approve</button><button className="text-action reject" onClick={() => { setDecision("reject"); setDecisionTarget(model); }}>Reject</button></>}{canApprove && model.state === "approved" && model.model_version !== activeModel?.model_version && <button className="text-action" onClick={() => { setDecision("rollback"); setDecisionTarget(model); }}>Restore</button>}</td></tr>;
              })}</tbody></table></div> : <div className="table-empty"><div className="empty-model-icon">◇</div><strong>No model candidates yet</strong><p>Generate synthetic data, run the local training workflow, then register its manifest and model artifact here.</p><code>churn-platform generate --rows 5000<br />churn-platform train --dataset data/generated/dataset.csv</code></div>}
            </div>
            <div className="notice inline-notice"><span>i</span><p>Approval is blocked until the candidate compares at least three models, beats the no-skill baseline on validation, and passes its automated checks. An approver's reason is recorded with each decision.</p></div>
          </section>}

          {page === "Training runs" && <section className="page-section">
            <PageHeading eyebrow="EXPERIMENT TRACKING" title="Training runs" subtitle="Chronological partitions, validation-based selection, and one final holdout evaluation." />
            <div className="panel table-panel"><div className="panel-heading"><div><h3>Recent runs</h3><p>Local reports and runs linked to registered candidates</p></div><span className="table-count">{runs.length} runs</span></div>
              {runs.length ? <div className="table-scroll"><table><thead><tr><th>RUN</th><th>STATUS</th><th>SELECTED CANDIDATE</th><th>VALIDATION PR-AUC</th><th>DATASET VERSION</th><th>CREATED</th></tr></thead><tbody>{runs.map((run) => <tr key={run.run_id}><td className="mono">{run.run_id}</td><td><span className={`status-pill ${run.status === "completed" ? "approved" : "pending_approval"}`}>{run.status.replaceAll("_", " ")}</span></td><td>{run.selection?.selected_candidate ?? "None beat baseline"}</td><td>{score(run.selection?.selected_validation_score)}</td><td className="mono">{run.dataset_version}</td><td>{time(run.created_at)}</td></tr>)}</tbody></table></div> : <div className="empty-state padded"><span>▤</span><div><strong>No training reports found.</strong><p>Start with the two commands shown in Model registry. Metrics are synthetic and do not represent actual telecom performance.</p></div></div>}
            </div>
            <div className="run-process-grid"><div className="panel process-card"><span className="process-number">01</span><div><strong>Generate data</strong><p>Reproducible synthetic snapshots with a versioned manifest.</p></div><span className="process-state complete">READY</span></div><div className="panel process-card"><span className="process-number">02</span><div><strong>Compare candidates</strong><p>Time-aware split; validation PR-AUC and capacity review.</p></div><span className="process-state">LOCAL</span></div><div className="panel process-card"><span className="process-number">03</span><div><strong>Approve release</strong><p>A different project approver records the decision.</p></div><span className="process-state">GATED</span></div></div>
          </section>}

          {page === "Monitoring" && <section className="page-section">
            <PageHeading eyebrow="SERVICE HEALTH" title="Monitoring" subtitle="Service activity is recorded. Drift and model quality need representative input and delayed outcome labels." action={<button className="outline-button" onClick={() => void refresh()}>↻ Refresh</button>} />
            <div className="monitor-grid"><div className="panel monitor-stat"><span className="stat-label">API REQUESTS · THIS MONTH</span><strong>{monitor?.request_count ?? "—"}</strong><small>Application-side events</small></div><div className="panel monitor-stat"><span className="stat-label">SERVER ERROR RATE</span><strong>{monitor ? percent(monitor.error_rate, 2) : "—"}</strong><small>HTTP 5xx requests</small></div><div className="panel monitor-stat"><span className="stat-label">ACTIVE MODEL</span><strong className="model-short">{monitor?.active_model_version ?? "None"}</strong><small>Serving version</small></div><div className="panel monitor-stat"><span className="stat-label">REQUEST QUOTA</span><strong>{quota ? `${quota.remaining.toLocaleString()} left` : "—"}</strong><small>{quota ? `${quota.requests.toLocaleString()} of ${quota.limit.toLocaleString()} · resets ${quota.month}` : "Available after cloud deployment"}</small></div></div>
            <div className="monitor-lower"><div className="panel signal-panel"><div className="panel-heading"><div><h3>Data & model signals</h3><p>Current monitored inputs</p></div><span className="small-badge muted">INITIAL DEMO</span></div><div className="signal-row"><div className="signal-icon green">✓</div><div><strong>Schema validation</strong><p>Unsupported fields and target leakage are rejected at scoring.</p></div><span className="signal-status">ENFORCED</span></div><div className="signal-row"><div className="signal-icon blue">⌁</div><div><strong>Feature drift</strong><p>No representative production baseline has been registered.</p></div><span className="signal-status neutral">NOT BASELINED</span></div><div className="signal-row"><div className="signal-icon amber">◷</div><div><strong>Delayed-label performance</strong><p>Requires outcome labels after the 30-day prediction window.</p></div><span className="signal-status neutral">WAITING</span></div></div><div className="panel signal-panel signal-note"><div className="panel-heading"><div><h3>Signal limitations</h3><p>Read before interpreting monitoring</p></div><span className="info-mark">i</span></div><ul>{(monitor?.signal_limitations ?? ["Synthetic demo only. No real customer data is collected.", "No business lift or churn reduction is claimed.", "Production performance needs representative labelled data."]).map((item) => <li key={item}>{item}</li>)}</ul></div></div>
          </section>}
        </div>
      </main>

      {decisionTarget && <div className="modal-backdrop" role="presentation" onClick={() => setDecisionTarget(null)}><form className="decision-modal" onSubmit={submitDecision} onClick={(event) => event.stopPropagation()}><div className="modal-kicker">MODEL GOVERNANCE</div><h2>{decision === "approve" ? "Approve model promotion?" : decision === "reject" ? "Reject candidate?" : "Restore this model?"}</h2><p><span className="mono">{decisionTarget.model_version}</span> · {decisionTarget.dataset_version}</p>{decision === "approve" && !decisionTarget.manifest.release_gates.automated_gates_pass && <div className="modal-warning">Automated release gates have not passed. This candidate cannot be promoted.</div>}<label className="field-label">Decision reason<textarea value={decisionReason} onChange={(event) => setDecisionReason(event.target.value)} minLength={8} maxLength={2000} required placeholder="Record the evidence or reason for this decision…" /></label><div className="modal-footer"><button type="button" className="outline-button" onClick={() => setDecisionTarget(null)}>Cancel</button><button className="primary-button" disabled={decisionBusy || decisionReason.trim().length < 8 || (decision === "approve" && !decisionTarget.manifest.release_gates.automated_gates_pass)}>{decisionBusy ? "Saving…" : decision === "approve" ? "Approve & promote" : decision === "reject" ? "Reject candidate" : "Restore model"}<span>→</span></button></div></form></div>}
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return <label className="field-label">{label}{children}</label>;
}

function PageHeading({ eyebrow, title, subtitle, action }: { eyebrow: string; title: string; subtitle: string; action?: ReactNode }) {
  return <div className="page-heading"><div><div className="eyebrow">{eyebrow}</div><h1>{title}</h1><p>{subtitle}</p></div>{action}</div>;
}

function OverviewPage({ overview, activeModel, pending, monitor, quota, models, go }: {
  overview: Overview; activeModel: Candidate | null; pending: number; monitor: Monitor | null; quota: Quota | null; models: Candidate[]; go: (page: Page) => void;
}) {
  const holdout = activeModel?.manifest.holdout_metrics as Record<string, unknown> | undefined;
  const scoreCards = [
    { label: "ACTIVE MODEL", value: activeModel ? "Serving" : "Not set", note: activeModel?.model_version ?? "Awaiting approval", icon: "◇", tone: activeModel ? "green" : "amber" },
    { label: "PENDING REVIEWS", value: String(pending).padStart(2, "0"), note: "Human approval required", icon: "◷", tone: "blue" },
    { label: "SYNTHETIC HOLDOUT PR-AUC", value: score(holdout?.pr_auc), note: activeModel ? "Demonstration metric only" : "No approved model", icon: "⌁", tone: "purple" },
    { label: "REQUESTS LEFT", value: quota ? quota.remaining.toLocaleString() : "—", note: quota ? `of ${quota.limit.toLocaleString()} this month` : "Monthly app quota", icon: "↗", tone: "green" },
  ];
  return <section className="page-section">
    <PageHeading eyebrow="TELECOM CHURN · MODEL OPERATIONS" title="Good to see you" subtitle="A clear view of the approved model, candidate reviews, and project activity." action={<span className="date-stamp">{new Date().toLocaleDateString(undefined, { weekday: "long", month: "short", day: "numeric" })}</span>} />
    <div className="demo-banner"><div className="demo-symbol">✳</div><div><strong>Synthetic environment</strong><p>All customer-like snapshots are generated. Scores and model metrics are for workflow demonstration only.</p></div><span className="demo-schema">SCHEMA {overview.schema_version}</span></div>
    <div className="stat-grid">{scoreCards.map((card) => <div className="panel stat-card" key={card.label}><div className="stat-top"><span className="stat-label">{card.label}</span><span className={`stat-icon ${card.tone}`}>{card.icon}</span></div><strong>{card.value}</strong><small>{card.note}</small></div>)}</div>
    <div className="overview-grid">
      <div className="panel model-summary"><div className="panel-heading"><div><div className="eyebrow">MODEL STATUS</div><h3>{activeModel ? "Active model" : "No approved model yet"}</h3></div><span className={`status-pill ${activeModel ? "approved" : "pending_approval"}`}>{activeModel ? "approved" : "awaiting review"}</span></div>
        {activeModel ? <><div className="active-version"><div className="model-emblem">M</div><div><strong>{activeModel.model_version}</strong><small>Version linked to {activeModel.dataset_version}</small></div><span className="active-live"><i />LIVE</span></div><div className="model-metrics"><Metric label="Holdout PR-AUC" value={score(holdout?.pr_auc)} /><Metric label="Holdout ROC-AUC" value={score(holdout?.roc_auc)} /><Metric label="Decision threshold" value={percent(activeModel.manifest.threshold)} /></div><div className="model-summary-footer"><span>Approved {time(activeModel.approved_at ?? undefined)}</span><button className="text-action" onClick={() => go("Model registry")}>View registry →</button></div></> : <div className="empty-model"><div className="empty-model-icon">◇</div><p>Training is run locally. Register a candidate and have an approver review it before the model can serve predictions.</p><button className="outline-button" onClick={() => go("Model registry")}>Open model registry <span>→</span></button></div>}
      </div>
      <div className="panel activity-panel"><div className="panel-heading"><div><div className="eyebrow">OPERATIONS</div><h3>Project activity</h3></div><button className="subtle-button" onClick={() => go("Monitoring")}>Monitoring →</button></div>
        <div className="activity-metric"><div><span>API requests this month</span><small>Project service activity</small></div><strong>{monitor?.request_count ?? 0}</strong></div>
        <div className="activity-bar"><span style={{ width: `${Math.min(100, (monitor?.request_count ?? 0) / Math.max(1, quota?.limit ?? 5000) * 100)}%` }} /></div>
        <div className="activity-foot"><span>{quota ? `${quota.remaining.toLocaleString()} requests remain` : "Local demo · no cloud quota"}</span><span>{monitor ? percent(monitor.error_rate, 2) + " server errors" : "No errors recorded"}</span></div>
        <div className="activity-divider" />
        <div className="quick-link"><span className="quick-icon score">⌕</span><div><strong>Try a synthetic score</strong><small>Explore how an approved model behaves</small></div><button onClick={() => go("Score a customer")} aria-label="Score a customer">→</button></div>
        <div className="quick-link"><span className="quick-icon review">◷</span><div><strong>Candidate reviews</strong><small>{pending ? `${pending} waiting for a decision` : "No pending review"}</small></div><button onClick={() => go("Model registry")} aria-label="Open candidate reviews">→</button></div>
      </div>
    </div>
    <div className="bottom-grid">
      <div className="panel bottom-panel"><div className="panel-heading"><div><div className="eyebrow">GOVERNANCE</div><h3>Review queue</h3></div><button className="subtle-button" onClick={() => go("Model registry")}>All models →</button></div>
        {pending ? models.filter((model) => model.state === "pending_approval").slice(0, 3).map((model) => <div className="queue-row" key={model.model_version}><span className="queue-dot" /><div><strong>{model.model_version}</strong><small>{model.manifest.candidate_count} candidates · {model.manifest.release_gates.automated_gates_pass ? "checks passed" : "automated gate blocked"}</small></div><span className="queue-date">{time(model.created_at)}</span></div>) : <div className="quiet-note spacious">No candidates are waiting for approval.</div>}
      </div>
      <div className="panel bottom-panel"><div className="panel-heading"><div><div className="eyebrow">WORKFLOW</div><h3>Project readiness</h3></div><span className="readiness-badge">IN PROGRESS</span></div><div className="readiness-list"><div><span className="check-circle done">✓</span><span>Synthetic data & schema</span><small>Ready</small></div><div><span className="check-circle done">✓</span><span>Time-aware training workflow</span><small>Ready</small></div><div><span className="check-circle done">✓</span><span>Cloud identity & API</span><small>Live</small></div></div></div>
    </div>
  </section>;
}

function Metric({ label, value }: { label: string; value: string }) {
  return <div><span>{label}</span><strong>{value}</strong></div>;
}

export default App;
