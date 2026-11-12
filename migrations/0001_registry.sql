CREATE TABLE IF NOT EXISTS models (
    model_version TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    dataset_version TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending_approval', 'approved', 'rejected')),
    manifest_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    approved_at TEXT,
    approved_by TEXT,
    candidate_count INTEGER NOT NULL DEFAULT 0,
    release_gates_pass INTEGER NOT NULL DEFAULT 0 CHECK (release_gates_pass IN (0, 1))
);

CREATE TABLE IF NOT EXISTS platform_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    active_model_version TEXT REFERENCES models(model_version)
);
INSERT OR IGNORE INTO platform_state (id, active_model_version) VALUES (1, NULL);

CREATE TABLE IF NOT EXISTS approval_events (
    event_id TEXT PRIMARY KEY,
    model_version TEXT NOT NULL REFERENCES models(model_version),
    actor TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('approve', 'reject', 'rollback')),
    reason TEXT NOT NULL,
    previous_model_version TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_events (
    event_id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    method TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    actor TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS api_events_created_at_idx ON api_events(created_at);
CREATE INDEX IF NOT EXISTS models_state_created_at_idx ON models(state, created_at);

CREATE TABLE IF NOT EXISTS monthly_usage (
    month TEXT PRIMARY KEY,
    requests INTEGER NOT NULL DEFAULT 0 CHECK (requests >= 0)
);
