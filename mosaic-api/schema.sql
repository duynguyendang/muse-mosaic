-- Muse Mosaic SQLite ledger schema (CSD 12.5, v2.3).
-- Gate columns are copied from the CSD. CREATE TABLE IF NOT EXISTS is used so
-- startup and tests are idempotent; no gate column has been removed or renamed.
-- v2.3 additions: graph_nodes, graph_edges, taxonomy_version on golden_records
-- and decisions, Splink candidate-signal columns on candidate_facts, and
-- suggested_action on steward_queue (taxonomy Accept flow).
-- split_history and policy_store remain files-and-lineage, not tables:
-- splits are recorded via steward status + SPLIT lineage + tombstones,
-- policies and taxonomies are versioned JSON files baked into the image.

CREATE TABLE IF NOT EXISTS graph_nodes (
  node_id TEXT PRIMARY KEY,
  node_type TEXT NOT NULL,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  source TEXT,
  created_at TEXT NOT NULL,
  decision_id TEXT
);

CREATE TABLE IF NOT EXISTS graph_edges (
  edge_id TEXT PRIMARY KEY,
  edge_type TEXT NOT NULL,
  from_node_id TEXT NOT NULL,
  to_node_id TEXT NOT NULL,
  proof TEXT,
  proposer TEXT,
  rule_fired TEXT,
  policy_version TEXT,
  taxonomy_version TEXT,
  decision_id TEXT
);

CREATE TABLE IF NOT EXISTS golden_records (
  golden_id TEXT PRIMARY KEY,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  attributes JSON NOT NULL,
  profile_embedding BLOB,
  embedding_model TEXT,
  embedding_version TEXT,
  embedding_dim INTEGER,
  decision_id TEXT NOT NULL,
  policy_version TEXT NOT NULL,
  taxonomy_version TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(entity_type, entity_id, version)
);

CREATE TABLE IF NOT EXISTS candidate_facts (
  fact_id TEXT PRIMARY KEY,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  attribute TEXT NOT NULL,
  value TEXT NOT NULL,
  source TEXT NOT NULL,
  source_id TEXT NOT NULL,
  authority_rank INTEGER NOT NULL,
  event_time TEXT NOT NULL,
  verified INTEGER NOT NULL,
  ttl_seconds INTEGER NOT NULL,
  embedding BLOB,
  embedding_model TEXT,
  embedding_version TEXT,
  embedding_dim INTEGER,
  splink_match_weight REAL,
  splink_match_probability REAL
);

CREATE TABLE IF NOT EXISTS decisions (
  decision_id TEXT PRIMARY KEY,
  work_id TEXT NOT NULL,
  idempotency_key TEXT UNIQUE NOT NULL,
  payload_hash TEXT NOT NULL,
  rule_fired TEXT,
  verdict TEXT NOT NULL,
  reason_code TEXT,
  policy_version TEXT NOT NULL,
  taxonomy_version TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS steward_queue (
  steward_id TEXT PRIMARY KEY,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  attribute TEXT NOT NULL,
  owner TEXT NOT NULL,
  deputy TEXT,
  deadline TEXT NOT NULL,
  fallback TEXT NOT NULL,
  status TEXT NOT NULL,
  decision_id TEXT NOT NULL,
  suggested_action TEXT
);

CREATE TABLE IF NOT EXISTS tombstones (
  tombstone_id TEXT PRIMARY KEY,
  loser_fact_id TEXT NOT NULL,
  survivor_golden_id TEXT NOT NULL,
  decision_id TEXT NOT NULL,
  reason TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_golden_latest
  ON golden_records(entity_type, entity_id, version DESC);

CREATE INDEX IF NOT EXISTS idx_candidate_entity
  ON candidate_facts(entity_type, entity_id, attribute);

CREATE INDEX IF NOT EXISTS idx_steward_pending
  ON steward_queue(status, entity_type, entity_id);

CREATE INDEX IF NOT EXISTS idx_tombstones_survivor
  ON tombstones(survivor_golden_id);

CREATE INDEX IF NOT EXISTS idx_graph_nodes_entity
  ON graph_nodes(entity_type, entity_id);

CREATE INDEX IF NOT EXISTS idx_graph_edges_from
  ON graph_edges(from_node_id);

CREATE INDEX IF NOT EXISTS idx_graph_edges_decision
  ON graph_edges(decision_id);

-- Agent traces: subagent pipeline trace, Wonder notes, Prompt Guard flags.
-- Append-only audit, keyed by (decision_id, kind). The ledger replays
-- derivations; this table replays what the proposers said and did.
CREATE TABLE IF NOT EXISTS agent_traces (
  decision_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  payload JSON NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (decision_id, kind)
);

CREATE INDEX IF NOT EXISTS idx_agent_traces_decision
  ON agent_traces(decision_id);

-- Derive-output audit for escalated/denied attributes. Phase 2 addition, see
-- README open questions: CSD 12.5 fixes the gate columns of the five tables
-- above (untouched here) but defines no home for per-attribute conflict
-- reasons. Without this table, idempotent replay could not rebuild the
-- conflicts list without calling the trust-engine again. Rows are
-- append-only, keyed by decision_id.
CREATE TABLE IF NOT EXISTS conflicts (
  conflict_id TEXT PRIMARY KEY,
  decision_id TEXT NOT NULL,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  attribute TEXT NOT NULL,
  reason_code TEXT NOT NULL,
  rule_fired TEXT NOT NULL,
  fact_ids JSON NOT NULL,
  policy_version TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_conflicts_decision
  ON conflicts(decision_id);
